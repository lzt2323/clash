#!/usr/bin/env python3
"""Exercise the real curses UI through a PTY and a synthetic JSON worker."""
import codecs
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import tempfile
import termios
import time
import unicodedata
import unittest

ROOT = Path(__file__).resolve().parents[1]


class Screen:
    """Small ANSI screen reader for assertions, not a production renderer."""
    def __init__(self, rows, cols):
        self.row = self.col = 0
        self.pending = ""
        self.resize(rows, cols)

    def resize(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.cells = [[" "] * cols for _ in range(rows)]
        self.row = min(self.row, rows - 1)
        self.col = min(self.col, cols - 1)

    def feed(self, text):
        self.pending += text
        while self.pending:
            char = self.pending[0]
            if char == "\x1b":
                if len(self.pending) < 2:
                    return
                if self.pending[1] == "[":
                    end = next((i for i in range(2, len(self.pending))
                                if "@" <= self.pending[i] <= "~"), None)
                    if end is None:
                        return
                    self.csi(self.pending[2:end], self.pending[end])
                    self.pending = self.pending[end + 1:]
                    continue
                if self.pending[1] in "()":
                    if len(self.pending) < 3:
                        return
                    self.pending = self.pending[3:]
                    continue
                self.pending = self.pending[2:]
                continue
            self.pending = self.pending[1:]
            if char == "\r":
                self.col = 0
            elif char == "\n":
                self.row = min(self.row + 1, self.rows - 1)
            elif char == "\b":
                self.col = max(0, self.col - 1)
            elif ord(char) >= 32 and not unicodedata.combining(char):
                width = 2 if unicodedata.east_asian_width(char) in "WF" else 1
                if self.col >= self.cols:
                    self.col = 0
                    self.row = min(self.row + 1, self.rows - 1)
                self.cells[self.row][self.col] = char
                if width == 2 and self.col + 1 < self.cols:
                    self.cells[self.row][self.col + 1] = ""
                self.col += width

    def csi(self, params, final):
        if params.startswith("?"):
            if params == "?1049" and final == "h":
                self.resize(self.rows, self.cols)
            return
        try:
            values = [int(p or "0") for p in params.split(";")]
        except ValueError:
            return
        amount = values[0] or 1
        if final in "Hf":
            self.row = min(max(values[0] - 1, 0), self.rows - 1)
            self.col = min(max((values[1] if len(values) > 1 else 1) - 1, 0), self.cols - 1)
        elif final == "A":
            self.row = max(0, self.row - amount)
        elif final == "B":
            self.row = min(self.rows - 1, self.row + amount)
        elif final == "C":
            self.col = min(self.cols - 1, self.col + amount)
        elif final == "D":
            self.col = max(0, self.col - amount)
        elif final == "G":
            self.col = min(self.cols - 1, amount - 1)
        elif final == "d":
            self.row = min(self.rows - 1, amount - 1)
        elif final == "J":
            if values[0] in (2, 3):
                self.cells = [[" "] * self.cols for _ in range(self.rows)]
            elif values[0] == 0:
                self.cells[self.row][min(self.col, self.cols):] = [" "] * (self.cols - min(self.col, self.cols))
                for row in range(self.row + 1, self.rows):
                    self.cells[row] = [" "] * self.cols
        elif final == "K":
            start = 0 if values[0] in (1, 2) else min(self.col, self.cols)
            end = min(self.col + 1, self.cols) if values[0] == 1 else self.cols
            self.cells[self.row][start:end] = [" "] * (end - start)

    @property
    def text(self):
        return "\n".join("".join(row) for row in self.cells)


class Terminal:
    def __init__(self, directory, cols=80, rows=24, slow=None, no_color=False):
        self.directory = directory
        self.calls_file = directory / "calls.jsonl"
        self.state_file = directory / "state.json"
        self.termios_file = directory / "restored.json"
        self.state_file.write_text(json.dumps({
            "running": True, "active": "main", "name": "DEMO_MAIN", "mode": "rule",
            "node": "AUTO", "shell_proxy": "未开启", "subscriptions": [
                {"id": "main", "name": "DEMO_MAIN", "nodes": ["HK_TEST", "香港节点", "Tokyo"], "selected": "AUTO", "updated": "2026-10-03"},
                {"id": "backup", "name": "DEMO_BACKUP", "nodes": ["Backup"], "selected": "AUTO", "updated": "2026-10-03"},
            ],
        }), encoding="utf-8")
        worker = directory / "worker.py"
        worker.write_text('''import json, os, sys, signal, time
from pathlib import Path
request = json.loads(sys.stdin.readline())
with open(os.environ['TEST_WORKBENCH_CALLS'], 'a') as log:
    log.write(json.dumps(request) + '\\n')
path = Path(os.environ['TEST_WORKBENCH_STATE'])
state = json.loads(path.read_text())
action, args = request['action'], request.get('args', [])
cancelled = False
def cancel(signum, frame):
    global cancelled
    cancelled = True
    with open(os.environ['TEST_WORKBENCH_CALLS'], 'a') as log:
        log.write(json.dumps({'action': 'signal', 'args': [action]}) + '\\n')
signal.signal(signal.SIGINT, cancel)
if action == 'status':
    result = state
elif action == 'select':
    state['node'] = args[0]
    result = '节点已应用'
elif action == 'set_mode':
    state['mode'] = args[0]
    result = '模式已应用'
elif action == 'stop':
    state['running'] = False
    result = '核心已停止'
elif action in ('start', 'ensure', 'restart'):
    state['running'] = True
    result = '核心已启动'
elif action == 'delays_all':
    result = {name: 42 for name in ['AUTO'] + state['subscriptions'][0]['nodes']}
elif action == 'diagnose':
    result = ['本地模拟诊断正常']
else:
    result = '操作已完成'
slow = json.loads(os.environ.get('TEST_WORKBENCH_SLOW', '{}')).get(action)
if slow:
    if slow == 'committed':
        path.write_text(json.dumps(state))
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        time.sleep(.03)
    if cancelled and slow != 'committed':
        print(json.dumps({'ok': False, 'cancelled': True, 'error': '已取消'}))
        sys.exit(0)
path.write_text(json.dumps(state))
print(json.dumps({'ok': True, 'result': result}, ensure_ascii=False))
''', encoding="utf-8")
        self.master, self.slave = pty.openpty()
        self.original = termios.tcgetattr(self.slave)
        self.screen = Screen(rows, cols)
        self.raw = bytearray()
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.set_size(cols, rows)
        env = dict(os.environ, TERM="xterm-256color", PYTHONDONTWRITEBYTECODE="1",
                   CLASH_WORKBENCH_WORKER=str(worker), TEST_WORKBENCH_ROOT=str(directory),
                   TEST_WORKBENCH_CALLS=str(self.calls_file), TEST_WORKBENCH_STATE=str(self.state_file),
                   TEST_WORKBENCH_TERMIOS=str(self.termios_file),
                   TEST_WORKBENCH_SLOW=json.dumps(slow or {}))
        if no_color:
            env['NO_COLOR'] = '1'
        code = "import os,sys,json,termios; sys.path.insert(0,sys.argv[1]); from terminal_mvp import Manager; from terminal_workbench import run; result=run(Manager(root=os.environ['TEST_WORKBENCH_ROOT'])); open(os.environ['TEST_WORKBENCH_TERMIOS'],'w').write(json.dumps(termios.tcgetattr(0)[3])); sys.exit(result or 0)"
        def session():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        self.process = subprocess.Popen([os.sys.executable, "-B", "-c", code, str(ROOT / "scripts")],
                                        stdin=self.slave, stdout=self.slave, stderr=self.slave,
                                        env=env, preexec_fn=session)

    def set_size(self, cols, rows):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.screen.resize(rows, cols)
        if hasattr(self, "process"):
            self.process.send_signal(signal.SIGWINCH)

    def drain(self, duration=.15):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            readable, _, _ = select.select([self.master], [], [], min(.05, max(0, deadline - time.monotonic())))
            if readable:
                try:
                    data = os.read(self.master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        return
                    raise
                if not data:
                    return
                self.raw.extend(data)
                self.screen.feed(self.decoder.decode(data))

    def send(self, keys):
        os.write(self.master, keys.encode() if isinstance(keys, str) else keys)
        self.drain(.2)

    def calls(self, action=None):
        calls = [json.loads(line) for line in self.calls_file.read_text().splitlines()] if self.calls_file.exists() else []
        return [call for call in calls if action is None or call["action"] == action]

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            self.drain(.05)
            if self.process.poll() is not None:
                raise AssertionError("UI exited early: " + self.screen.text)
        if not predicate():
            raise AssertionError("UI condition timed out: " + self.screen.text)
        self.drain(.2)

    def ready(self):
        self.wait_for(lambda: bool(self.calls("status")))
        self.wait_for(lambda: "节点" in self.screen.text)

    def quit(self, key="q"):
        self.send(key)
        self.wait_exit()
        self.drain(.1)

    def wait_exit(self, timeout=5):
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            self.drain(.05)
        self.process.wait(timeout=.1)

    def close(self):
        if self.process.poll() is None:
            # Keep draining while asking it to exit: a PTY output queue can fill.
            self.send('q')
            try:
                self.wait_exit(3)
            except subprocess.TimeoutExpired:
                pass
        os.close(self.master)
        os.close(self.slave)
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)


class WorkbenchPTYTests(unittest.TestCase):
    def terminal(self, cols=80, rows=24, **options):
        directory = tempfile.TemporaryDirectory(prefix="clash-workbench-pty-")
        self.addCleanup(directory.cleanup)
        terminal = Terminal(Path(directory.name), cols, rows, **options)
        self.addCleanup(terminal.close)
        terminal.ready()
        return terminal

    def assert_restored(self, terminal):
        self.assertEqual(terminal.process.returncode, 0)
        # macOS invalidates the slave after its controlling session exits.
        # Capture the real terminal flags inside that session, after run returns.
        current = json.loads(terminal.termios_file.read_text())
        flags = termios.ECHO | termios.ICANON
        self.assertEqual(current & flags, terminal.original[3] & flags)
        self.assertIn(b"\x1b[?1049h", terminal.raw)
        self.assertIn(b"\x1b[?1049l", terminal.raw)

    def test_full_screen_at_both_widths_and_quit_restores_terminal(self):
        for cols in (80, 40):
            with self.subTest(cols=cols):
                terminal = self.terminal(cols)
                self.assertIn("节点", terminal.screen.text)
                self.assertIn("订阅", terminal.screen.text)
                terminal.quit()
                self.assert_restored(terminal)

    def test_arrows_browse_only_and_enter_applies_node(self):
        terminal = self.terminal()
        terminal.send(b"\x1bOB")
        self.assertFalse(terminal.calls("select"))
        terminal.send("\r")
        terminal.wait_for(lambda: bool(terminal.calls("select")))
        self.assertEqual(terminal.calls("select")[-1]["args"], ["HK_TEST"])
        terminal.quit()

    def test_navigation_focus_does_not_apply_mode(self):
        terminal = self.terminal()
        terminal.send(b"\t\x1bOB\r")
        self.assertFalse(terminal.calls("set_mode"))
        terminal.send("\r")
        terminal.wait_for(lambda: bool(terminal.calls("set_mode")))
        terminal.quit()

    def test_left_right_keys_change_regions_and_right_opens_focused_task(self):
        for width in (40, 80):
            terminal = self.terminal(cols=width)
            terminal.send(b"\x1bOD\x1bOB\x1bOC")
            self.assertFalse(terminal.calls("set_mode"))
            self.assertIn("模式", terminal.screen.text)
            terminal.send("\r")
            terminal.wait_for(lambda: bool(terminal.calls("set_mode")))
            self.assertEqual(terminal.calls("set_mode")[-1]["args"], ["rule"])
            terminal.quit()

    def test_navigation_arrows_refresh_content_before_entering_it(self):
        for width in (40, 80):
            with self.subTest(width=width):
                terminal = self.terminal(cols=width)
                terminal.send(b"\x1bOD\x1bOB")
                terminal.wait_for(lambda: "私网直连" in terminal.screen.text)
                self.assertIn("预览", terminal.screen.text)
                self.assertNotIn("HK_TEST", terminal.screen.text)
                terminal.send(b"\x1bOB")
                terminal.wait_for(lambda: "DEMO_BACKUP" in terminal.screen.text)
                terminal.send(b"\x1bOB")
                terminal.wait_for(lambda: "重新检测" in terminal.screen.text)
                self.assertFalse(terminal.calls("diagnose"))
                terminal.send(b"\x1bOA")
                terminal.wait_for(lambda: "DEMO_BACKUP" in terminal.screen.text)
                self.assertFalse(terminal.calls("select"))
                self.assertFalse(terminal.calls("set_mode"))
                self.assertFalse(terminal.calls("use"))
                terminal.send("\t")
                self.assertIn("DEMO_BACKUP", terminal.screen.text)
                self.assertFalse(terminal.calls("use"))
                terminal.quit()

    def test_all_delay_request_ignores_search(self):
        terminal = self.terminal()
        terminal.send("/NO_MATCH\r")
        terminal.send("t")
        terminal.wait_for(lambda: bool(terminal.calls("delays_all")))
        self.assertEqual(terminal.calls("delays_all")[-1]["args"], [])
        terminal.wait_for(lambda: "全部节点测速完成" in terminal.screen.text)
        terminal.quit()

    def test_stop_cancel_never_calls_backend(self):
        terminal = self.terminal()
        terminal.send("x")
        terminal.send("\r")
        self.assertFalse(terminal.calls("stop"))
        terminal.send("x")
        terminal.send(b"\x1b")
        self.assertFalse(terminal.calls("stop"))
        terminal.quit()

    def test_subscription_url_is_masked_and_cancel_does_not_edit(self):
        terminal = self.terminal()
        terminal.send("3e")
        token = "SECRET_42"
        terminal.send("https://x.invalid/?token=" + token)
        self.assertNotIn(token, terminal.raw.decode("utf-8", "replace"))
        self.assertNotIn(token, terminal.screen.text)
        self.assertIn("*", terminal.screen.text)
        terminal.send(b"\x1b")
        self.assertFalse(terminal.calls("edit"))
        terminal.quit()

    def test_resize_preserves_keyboard_operation_and_eof_restores_terminal(self):
        terminal = self.terminal()
        terminal.set_size(40, 24)
        terminal.drain(.3)
        self.assertIn("节点", terminal.screen.text)
        terminal.quit("\x04")
        self.assert_restored(terminal)

    def test_escape_cancels_safe_job_and_keeps_navigation_responsive(self):
        terminal = self.terminal(slow={'delays_all': 'cancellable'})
        terminal.send('t')
        terminal.wait_for(lambda: bool(terminal.calls('delays_all')))
        terminal.send('2')
        self.assertIn('模式', terminal.screen.text)
        terminal.send(b'\x1b2')  # Following input disambiguates bare Esc immediately.
        terminal.wait_for(lambda: bool(terminal.calls('signal')))
        terminal.wait_for(lambda: '已取消' in terminal.screen.text)
        self.assertFalse(terminal.calls('set_mode'))
        terminal.quit()
        self.assert_restored(terminal)

    def test_quit_waits_for_committed_job_and_preserves_actual_result(self):
        terminal = self.terminal(slow={'select': 'committed'})
        terminal.send(b'\x1bOB\r')
        terminal.wait_for(lambda: bool(terminal.calls('select')))
        terminal.send('q')
        self.assertIsNone(terminal.process.poll())
        terminal.wait_exit()
        terminal.drain(.1)
        self.assertEqual(json.loads(terminal.state_file.read_text())['node'], 'HK_TEST')
        self.assertTrue(terminal.calls('signal'))
        self.assert_restored(terminal)

    def test_no_color_uses_no_colored_foreground_codes(self):
        terminal = self.terminal(no_color=True)
        import re
        codes = re.findall(rb'\x1b\[([0-9;]*)m', bytes(terminal.raw))
        for code in codes:
            params = [int(number or 0) for number in code.split(b';')]
            # xterm's terminfo startup/reset emits its default white foreground.
            self.assertFalse(any(30 <= number <= 36 or 90 <= number <= 97 or number == 38
                                 for number in params), code)
        terminal.quit()


if __name__ == "__main__":
    unittest.main(verbosity=2)
