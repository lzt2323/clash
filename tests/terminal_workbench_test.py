#!/usr/bin/env python3
"""Workbench navigation, rendering and worker protocol tests without a live core."""

import curses
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import tempfile
import time
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location(
    "terminal_workbench", Path(__file__).resolve().parents[1] / "scripts/terminal_workbench.py"
)
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


class Screen:
    def __init__(self, height=24, width=80):
        self.size = (height, width)
        self.calls = []

    def getmaxyx(self):
        return self.size

    def addstr(self, y, x, text, attr=0):
        if y >= self.size[0] or x + ui.cell_width(text) > self.size[1]:
            raise AssertionError("render outside terminal")
        self.calls.append((y, x, text, attr))

    def erase(self):
        self.calls = []

    def refresh(self):
        pass

    def keypad(self, _enabled):
        pass

    def timeout(self, _timeout):
        pass


class Job:
    def __init__(self, action="select", envelope=None):
        self.action = action
        self.started = time.monotonic()
        self.cancel_requested = False
        self.envelope = envelope
        self.cancel_count = 0
        self.process = mock.Mock()

    def poll(self):
        return self.envelope

    def cancel(self):
        self.cancel_count += 1
        self.cancel_requested = True


def application(height=24, width=80):
    app = ui.Workbench(Screen(height, width), mock.Mock(root=Path("/tmp")))
    app.state = {
        "running": True, "core_state": "running", "active": "a", "name": "我的订阅",
        "mode": "rule", "node": "AUTO", "shell_proxy": "已开启",
        "subscriptions": [
            {"id": "a", "name": "我的订阅", "nodes": ["香港 %02d" % n for n in range(20)], "selected": "AUTO", "updated": "今天"},
            {"id": "b", "name": "备用订阅", "nodes": ["日本"], "selected": "AUTO", "updated": "昨天"},
        ],
    }
    return app


class WorkbenchTest(unittest.TestCase):
    def test_unicode_width_clipping_and_secret_redaction(self):
        self.assertEqual(ui.cell_width("香港e\u0301"), 5)
        for width in range(1, 40):
            self.assertLessEqual(ui.cell_width(ui.clip("香港" * 20, width)), width)
        cleaned = ui.clean("\x1b[31mhttps://example.test/?token=never_echo\x00")
        self.assertNotIn("never_echo", cleaned)
        self.assertNotIn("\x1b", cleaned)

    def test_short_and_narrow_layout_never_overlaps_details(self):
        for height in (18, 19, 20, 24, 40):
            for width in (30, 40, 60, 70, 80, 120):
                geom = ui.layout(height, width)
                self.assertGreaterEqual(geom["list"], geom["top"] + 2)
                self.assertLessEqual(geom["list"] + geom["rows"], geom["detail"])
                self.assertLessEqual(geom["list"] + geom["rows"] - 1, geom["bottom"])
                self.assertLess(geom["bottom"], height - 4)

    def test_render_all_tasks_at_40_and_80_columns(self):
        for height, width in ((18, 40), (24, 40), (24, 80)):
            app = application(height, width)
            for page in range(4):
                app.page = page
                app.render()
                self.assertTrue(app.screen.calls)
                self.assertTrue(all("\x1b" not in text for _, _, text, _ in app.screen.calls))

    def test_resize_reflows_from_sidebar_to_tabs(self):
        app = application()
        app.render()
        self.assertTrue(app.geom["wide"])
        app.screen.size = (24, 40)
        app.key(curses.KEY_RESIZE)
        app.render()
        self.assertFalse(app.geom["wide"])
        texts = [text for _, _, text, _ in app.screen.calls]
        self.assertIn("1 节点", texts)

    def test_browsing_does_not_select_and_current_is_separate(self):
        app = application()
        app.submit = mock.Mock()
        app.key("j")
        app.render()
        self.assertEqual(app.selection(), "香港 00")
        app.submit.assert_not_called()
        texts = [text for _, _, text, _ in app.screen.calls]
        self.assertTrue(any("* 自动选择" in text for text in texts))
        self.assertTrue(any(text.startswith(">   香港 00") for text in texts))
        app.key("\n")
        app.submit.assert_called_once_with("select", ["香港 00"], "选择节点")

    def test_task_numbers_never_select_node(self):
        app = application()
        app.submit = mock.Mock()
        app.key("2")
        self.assertEqual(app.page, 1)
        app.submit.assert_not_called()
        app.key("3")
        self.assertEqual(app.page, 2)
        app.submit.assert_not_called()

    def test_tab_navigation_enter_changes_task_only(self):
        app = application()
        app.submit = mock.Mock()
        app.key("\t")
        app.key("j")
        self.assertEqual(app.focus, "nav")
        app.key("\n")
        self.assertEqual(app.page, 1)
        self.assertEqual(app.focus, "list")
        app.submit.assert_not_called()

    def test_navigation_previews_content_without_backend_work(self):
        for width in (40, 80):
            with self.subTest(width=width):
                app = application(width=width)
                app.submit = mock.Mock()
                app.key(curses.KEY_LEFT)
                for key, page, label in (
                    (curses.KEY_DOWN, 1, "规则"),
                    ("j", 2, "备用订阅"),
                    (curses.KEY_DOWN, 3, "重新检测"),
                    ("j", 0, "自动选择"),
                    (curses.KEY_UP, 3, "重新检测"),
                    ("k", 2, "备用订阅"),
                ):
                    app.key(key)
                    self.assertTrue(app.dirty)
                    self.assertEqual((app.page, app.nav_page, app.focus), (page, page, "nav"))
                    app.render()
                    content = [text for y, x, text, _ in app.screen.calls
                               if y >= app.geom["list"] and y < app.geom["detail"]
                               and x == app.geom["nav"] + 1]
                    self.assertTrue(any(label in text for text in content), content)
                    self.assertTrue(any("预览" in text for _, _, text, _ in app.screen.calls))
                app.submit.assert_not_called()
                self.assertFalse(app.diagnose_needed)

    def test_entering_preview_keeps_selection_and_does_not_apply_it(self):
        for key in (curses.KEY_RIGHT, "\t", "\n"):
            with self.subTest(key=key):
                app = application()
                app.submit = mock.Mock()
                app.indices = [12, 2, 1, 0]
                app.key(curses.KEY_LEFT)
                app.key("j")
                app.key(key)
                self.assertEqual((app.page, app.focus), (1, "list"))
                self.assertEqual(app.selection()[0], "direct")
                app.submit.assert_not_called()
                app.key(curses.KEY_LEFT)
                app.key("k")
                self.assertEqual(app.indices, [12, 2, 1, 0])
                self.assertEqual(app.visible()[0], 8)

    def test_busy_navigation_previews_without_queuing_diagnostics(self):
        app = application()
        app.job = Job()
        app.submit = mock.Mock()
        app.key(curses.KEY_LEFT)
        app.key("k")
        self.assertEqual((app.page, app.focus), (3, "nav"))
        app.submit.assert_not_called()
        self.assertFalse(app.diagnose_needed)
        app.key("\t")
        self.assertTrue(app.diagnose_needed)
        self.assertEqual(app.focus, "list")

    def test_entering_diagnostic_preview_starts_check_once(self):
        for key in (curses.KEY_RIGHT, "\t", "\n"):
            with self.subTest(key=key):
                app = application()
                app.submit = mock.Mock()
                app.key(curses.KEY_LEFT)
                app.key("k")
                app.submit.assert_not_called()
                app.key(key)
                app.submit.assert_called_once_with("diagnose", [], "检查连接")

    def test_auto_is_present_without_provider_auto(self):
        app = application()
        self.assertEqual(app.items()[0], "AUTO")

    def test_search_filters_without_network_request(self):
        app = application()
        app.submit = mock.Mock()
        app.key("/")
        for char in "19":
            app.key(char)
        app.key("\n")
        self.assertEqual(app.items(), ["香港 19"])
        app.submit.assert_not_called()

    def test_left_right_switch_regions_without_applying_items(self):
        for width in (40, 80):
            app = application(width=width)
            app.submit = mock.Mock()
            app.key("j")
            app.key(curses.KEY_LEFT)
            self.assertEqual(app.focus, "nav")
            app.key("j")
            app.key(curses.KEY_LEFT)
            self.assertEqual(app.nav_page, 1)
            app.key(curses.KEY_RIGHT)
            self.assertEqual((app.page, app.focus), (1, "list"))
            app.key(curses.KEY_RIGHT)
            app.submit.assert_not_called()
            app.key("\n")
            app.submit.assert_called_once_with("set_mode", ["rule"], "设置模式")

    def test_manual_delay_ignores_page_and_search(self):
        for query in ("", "19", "no match"):
            app = application()
            app.submit = mock.Mock()
            app.key("n")
            app.query = query
            app.key("t")
            app.submit.assert_called_once_with("delays_all", [], "全部节点测速")

    def test_url_form_hides_text_and_handles_letters_as_input(self):
        app = application()
        app.submit = mock.Mock(return_value=True)
        app.key("3")
        app.key("a")
        for char in "新增订阅":
            app.key(char)
        app.key("\n")
        secret = "https://example.test/subscribe?token=never_echo&q=sx123"
        for char in secret:
            app.key(char)
        app.render()
        rendered = "\n".join(text for _, _, text, _ in app.screen.calls)
        self.assertNotIn("never_echo", rendered)
        self.assertNotIn("example.test", rendered)
        self.assertIn("***", rendered)
        self.assertTrue(app.running)
        app.submit.assert_not_called()
        app.key("\n")
        app.submit.assert_called_once_with("add", ["新增订阅", secret], "添加订阅")

    def test_stop_default_enter_and_escape_both_cancel(self):
        app = application()
        app.submit = mock.Mock()
        app.key("x")
        self.assertFalse(app.dialog["yes"])
        self.assertIn("clash off", app.dialog["text"])
        app.key("\n")
        self.assertIsNone(app.dialog)
        app.key("x")
        app.key("\x1b")
        app.submit.assert_not_called()

    def test_stop_tab_enter_confirms_once(self):
        app = application()
        app.submit = mock.Mock(return_value=True)
        app.key("x")
        app.key("\t")
        app.key("\n")
        app.submit.assert_called_once_with("stop", [], "停止核心")

    def test_active_subscription_delete_blocked_inactive_confirmed(self):
        app = application()
        app.submit = mock.Mock(return_value=True)
        app.key("3")
        app.key("d")
        self.assertIsNone(app.dialog)
        self.assertIn("正在使用", app.message)
        app.key("j")
        app.key("d")
        self.assertFalse(app.dialog["yes"])
        app.key("y")
        app.submit.assert_called_once_with("remove", ["b"], "删除订阅")

    def test_updated_subscription_discards_old_delay_results(self):
        app = application()
        app.delays = {"香港 00": 42}
        app.job = Job("update", {"ok": True, "result": "订阅已更新。"})
        app.submit = mock.Mock()
        app.poll()
        self.assertEqual(app.delays, {})
        app.submit.assert_called_once_with("status", [], "刷新状态")

    def test_busy_job_rejects_duplicates_and_escape_requests_cancel(self):
        app = application()
        app.job = Job()
        with mock.patch.object(ui, "Worker") as worker:
            self.assertFalse(app.submit("select", ["AUTO"], "切换"))
            worker.assert_not_called()
        app.key("\x1b")
        self.assertEqual(app.job.cancel_count, 1)
        self.assertIn("正在取消", app.message)
        self.assertNotIn("已取消", app.message)

    def test_cancelled_response_keeps_notice_without_refresh(self):
        app = application()
        app.job = Job(envelope={"ok": False, "cancelled": True, "error": "取消"})
        app.job.cancel_requested = True
        app.refresh_needed, app.diagnose_needed = True, True
        app.submit = mock.Mock()
        app.poll()
        self.assertEqual(app.message, "已取消当前操作。")
        app.submit.assert_not_called()

    def test_quit_waits_for_real_worker_result(self):
        app = application()
        app.job = Job()
        app.key("q")
        self.assertTrue(app.running)
        self.assertTrue(app.quit_requested)
        self.assertTrue(app.job.cancel_requested)
        app.job.envelope = {"ok": True, "result": "节点已保存。"}
        app.poll()
        self.assertFalse(app.running)
        self.assertEqual(app.message, "节点已保存。")

    def test_control_d_exits_even_from_hidden_form(self):
        app = application()
        app.form("edit", "a")
        app.key("\x04")
        self.assertFalse(app.running)
        self.assertIsNone(app.dialog)

    def test_core_status_distinguishes_stopped_unhealthy_and_unknown(self):
        app = application()
        for state, word in (("stopped", "已停止"), ("unhealthy", "接口异常"), ("unknown", "未知")):
            app.state["core_state"] = state
            app.render()
            self.assertTrue(any(word in text for _, _, text, _ in app.screen.calls))

    def test_no_color_uses_transparent_default_rendering(self):
        app = application()
        app.submit = mock.Mock()
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}), \
                mock.patch.object(ui.curses, "curs_set"), \
                mock.patch.object(ui.curses, "init_pair") as pair:
            app.setup()
        pair.assert_not_called()
        self.assertEqual(app.color, {})
        app.submit.assert_called_once_with("status", [], "读取状态")

    def test_ui_exception_cancels_and_waits_for_worker(self):
        app = application()
        job = Job()
        app.job = job
        app.setup = mock.Mock()
        app.screen.get_wch = mock.Mock(side_effect=RuntimeError("terminal failure"))
        with mock.patch.object(ui.signal, "signal"), self.assertRaises(RuntimeError):
            app.loop()
        self.assertEqual(job.cancel_count, 1)
        job.process.wait.assert_called_once()
        self.assertIsNone(app.job)


class WorkerTest(unittest.TestCase):
    def test_worker_ignores_host_python_settings_and_user_site(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scripts, poison, home = root / "scripts", root / "poison", root / "home"
            scripts.mkdir()
            poison.mkdir()
            version = "%s.%s" % ui.sys.version_info[:2]
            # Default user site locations on Linux and macOS, both fake HOME.
            user_sites = [home / ".local/lib" / ("python" + version) / "site-packages",
                          home / "Library/Python" / version / "lib/python/site-packages"]
            for user_site in user_sites:
                user_site.mkdir(parents=True)
                (user_site / "usercustomize.py").write_text("print('unexpected user customization')\n")
                (user_site / "only_from_user_site.py").write_text("VALUE = 'untrusted'\n")
            (poison / "sitecustomize.py").write_text("print('unexpected path customization')\n")
            (poison / "only_from_pythonpath.py").write_text("VALUE = 'untrusted'\n")
            (scripts / "sibling.py").write_text("VALUE = 'local sibling'\n")
            script = scripts / "stub.py"
            script.write_text("""import importlib.util, json, os, site, sys
from sibling import VALUE
sys.stdin.readline()
print(json.dumps({'ok': True, 'result': {
    'sibling': VALUE,
    'flags': [sys.flags.ignore_environment, sys.flags.no_user_site, sys.flags.dont_write_bytecode, sys.flags.isolated],
    'user_site': site.ENABLE_USER_SITE,
    'path_module': importlib.util.find_spec('only_from_pythonpath') is not None,
    'user_module': importlib.util.find_spec('only_from_user_site') is not None,
    'pythonhome': os.environ.get('PYTHONHOME')
}}))
""")
            environment = {"CLASH_WORKBENCH_WORKER": str(script), "HOME": str(home),
                           "PYTHONPATH": str(poison), "PYTHONHOME": "/invalid-python-home"}
            with mock.patch.dict(os.environ, environment):
                worker = ui.Worker(mock.Mock(root=root), "status", [])
            worker.process.wait(timeout=5)
            envelope = worker.poll()
            self.assertTrue(envelope["ok"], envelope)
            self.assertEqual(envelope["result"], {
                "sibling": "local sibling", "flags": [1, 1, 1, 0], "user_site": False,
                "path_module": False, "user_module": False, "pythonhome": "/invalid-python-home"})
            self.assertFalse((scripts / "__pycache__").exists())

    def test_large_json_output_does_not_deadlock_pipe(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "stub.py"
            script.write_text("import json, sys\nr = json.loads(sys.stdin.readline())\nprint(json.dumps({'ok': True, 'result': 'x' * 200000}))\n")
            with mock.patch.dict(os.environ, {"CLASH_WORKBENCH_WORKER": str(script)}):
                worker = ui.Worker(mock.Mock(root=Path(directory)), "status", [])
            self.assertEqual(os.getsid(worker.process.pid), worker.process.pid)
            worker.process.wait(timeout=5)
            envelope = worker.poll()
            self.assertTrue(envelope["ok"])
            self.assertEqual(len(envelope["result"]), 200000)
            self.assertTrue(worker.output.closed)

    def test_cancel_signals_only_worker_pid_once(self):
        worker = ui.Worker.__new__(ui.Worker)
        worker.finished = False
        worker.cancel_requested = False
        worker.process = mock.Mock()
        worker.cancel()
        worker.cancel()
        worker.process.send_signal.assert_called_once_with(signal.SIGINT)

    def test_invalid_worker_response_is_safe_error(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "stub.py"
            script.write_text("print('https://example.test/private?token=never_echo')\n")
            with mock.patch.dict(os.environ, {"CLASH_WORKBENCH_WORKER": str(script)}):
                worker = ui.Worker(mock.Mock(root=Path(directory)), "status", [])
            worker.process.wait(timeout=5)
            envelope = worker.poll()
            self.assertFalse(envelope["ok"])
            self.assertNotIn("never_echo", envelope["error"])


if __name__ == "__main__":
    unittest.main()
