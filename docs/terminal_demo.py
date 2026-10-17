#!/usr/bin/env python3
"""Record the Linux CLI in an isolated PTY, or render that capture as a GIF.

Record: python3 docs/terminal_demo.py record --binary /path/clash --core /path/mihomo --output demo.json
Render: python3 docs/terminal_demo.py render --capture demo.json --output docs/assets/terminal-demo.gif
Rendering needs Pillow and pyte; neither is an application dependency.
"""
import argparse
import codecs
import copy
import fcntl
import http.server
import json
import os
from pathlib import Path
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
COLUMNS, ROWS = 96, 24
BACKGROUND, FOREGROUND = '#101b24', '#f2f4ed'
ACCENT, MUTED = '#7dd3d4', '#a9bbc4'
CUES = [
    ('01', '打开工作台', '在任意目录输入 clash'),
    ('02', '查看订阅', '按 3，查看已保存的订阅'),
    ('03', '选择节点', '按 1 返回节点页，↓ 选择，Enter 应用'),
    ('04', '返回终端', '按 q 退出工作台'),
    ('05', '开启终端代理', '运行 clash on，开启当前终端代理'),
    ('06', '确认已开启', '运行 clash status，查看核心与代理状态'),
]


class Recording:
    def __init__(self, command, env, directory):
        self.events = []
        self.output = ''
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.master, self.slave = pty.openpty()
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack('HHHH', ROWS, COLUMNS, 0, 0))

        def session():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        self.started = time.monotonic()
        self.process = subprocess.Popen(command, env=env, cwd=directory, stdin=self.slave,
                                        stdout=self.slave, stderr=self.slave, preexec_fn=session)

    def event(self, kind, data):
        self.events.append([round(time.monotonic() - self.started, 3), kind, data])

    def drain(self, duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if select.select([self.master], [], [], min(.04, max(0, deadline - time.monotonic())))[0]:
                try:
                    block = os.read(self.master, 65536)
                except OSError:
                    if self.process.poll() is not None:
                        return
                    raise
                if not block:
                    return
                text = self.decoder.decode(block)
                self.output += text
                self.event('output', text)

    def wait_for(self, text, timeout=12):
        deadline = time.monotonic() + timeout
        def visible():
            return re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', self.output)
        while text not in visible() and time.monotonic() < deadline:
            self.drain(.05)
            if self.process.poll() is not None:
                raise RuntimeError('The demo shell exited before the expected screen.')
        if text not in visible():
            raise RuntimeError('Expected terminal output was not observed: ' + text)

    def cue(self, index):
        self.event('cue', index)

    def send(self, keys):
        os.write(self.master, keys.encode())
        self.drain(.12)

    def type_command(self, command):
        for character in command:
            self.send(character)
        self.drain(.45)
        self.send('\r')

    def close(self):
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            self.process.wait(timeout=5)
        os.close(self.master)
        os.close(self.slave)


def record(args):
    if sys.platform != 'linux':
        raise RuntimeError('Recording requires Linux and a real Mihomo core.')
    os.environ['CLASH_RUST_BINARY'] = str(args.binary.resolve())
    os.environ['MIHOMO_BINARY'] = str(args.core.resolve())
    sys.path.insert(0, str(ROOT / 'rust/tests'))
    from integration import Fixture, Harness

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    names = ['香港 01', '东京 02', '新加坡 03']
    Fixture.sources = {'/demo': json.dumps({'proxies': [
        {'name': name, 'type': 'http', 'server': '127.0.0.1', 'port': server.server_port}
        for name in names]}).encode()}
    try:
        with tempfile.TemporaryDirectory(prefix='clash-readme-demo-') as directory:
            home = Path(directory) / 'home'
            home.mkdir()
            app = Harness(Path(directory) / 'app')
            recording = None
            try:
                app.env.update(HOME=str(home), SHELL='/bin/bash', TERM='xterm-256color', LC_ALL='C.UTF-8')
                shutil.copy2(args.binary, app.root / 'clash')
                app.worker('add', ['日常订阅', f'http://127.0.0.1:{server.server_port}/demo'])
                app.worker('ensure')
                result = app.cli('shell-init', '--shell', 'bash')
                if result.returncode:
                    raise RuntimeError(result.stderr)
                with (home / '.bashrc').open('a') as rc:
                    rc.write("\nPS1='\\[\\e[36m\\]linux\\[\\e[0m\\]:~$ '\nPROMPT_COMMAND=''\n")
                recording = Recording(['/bin/bash', '--noprofile', '--rcfile', str(home / '.bashrc'), '-i'],
                                      app.env, home)
                recording.cue(0)
                recording.wait_for(':~$ ')
                recording.drain(.8)
                recording.type_command('clash')
                recording.wait_for('日常订阅')
                recording.drain(1.6)
                recording.cue(1)
                recording.send('3')
                recording.drain(1.9)
                recording.cue(2)
                recording.send('1')
                recording.drain(.6)
                recording.send('\x1b[B')
                recording.drain(.6)
                recording.send('\r')
                deadline = time.monotonic() + 10
                while app.state()['subscriptions'][0]['selected'] != names[0] and time.monotonic() < deadline:
                    recording.drain(.1)
                assert app.state()['subscriptions'][0]['selected'] == names[0], 'Node selection did not commit.'
                recording.drain(1.8)
                recording.cue(3)
                recording.send('q')
                recording.drain(.8)
                recording.cue(4)
                recording.type_command('clash on')
                recording.wait_for('已在当前终端开启代理')
                recording.drain(1)
                recording.cue(5)
                recording.type_command('clash status')
                recording.wait_for('核心：运行中')
                recording.wait_for('当前终端代理：已开启')
                recording.drain(3.2)
                captured = list(recording.events)
                duration = round(time.monotonic() - recording.started, 3)
                recording.send('clash off\r')
                recording.send('exit\r')
                recording.process.wait(timeout=5)
                assert recording.process.returncode == 0
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps({'columns': COLUMNS, 'rows': ROWS,
                    'duration': duration, 'events': captured}, ensure_ascii=False))
                print('Captured real CLI, selected node, enabled shell proxy and verified status:', args.output)
            finally:
                if recording:
                    recording.close()
                app.worker('stop')
    finally:
        server.shutdown()
        server.server_close()


def render(args):
    import pyte
    from PIL import Image, ImageDraw, ImageFont

    class Screen(pyte.Screen):
        # pyte does not switch alternate buffers; preserve the shell across the TUI.
        primary = None

        def set_mode(self, *modes, **kwargs):
            if kwargs.get('private') and 1049 in modes and self.primary is None:
                self.primary = copy.deepcopy({key: value for key, value in self.__dict__.items() if key != 'primary'})
                self.reset()
            super().set_mode(*modes, **kwargs)

        def reset_mode(self, *modes, **kwargs):
            if kwargs.get('private') and 1049 in modes and self.primary is not None:
                self.__dict__.update(self.primary)
                self.primary = None
            super().reset_mode(*modes, **kwargs)

    capture = json.loads(args.capture.read_text())
    screen = Screen(capture['columns'], capture['rows'])
    stream = pyte.Stream(screen)
    latin = ImageFont.truetype(str(args.latin_font), 19)
    chinese = ImageFont.truetype(str(args.cjk_font), 20)
    title_font = ImageFont.truetype(str(args.cjk_font), 23)
    colors = {'default': FOREGROUND, 'black': BACKGROUND, 'red': '#e78e8e', 'green': '#a6d69b',
              'brown': '#efbd70', 'blue': '#94b8ee', 'magenta': '#c7a7dd', 'cyan': ACCENT,
              'white': FOREGROUND, 'brightblack': MUTED, 'brightwhite': '#ffffff'}
    cell_width, cell_height, padding, top = 11, 26, 26, 68
    width = capture['columns'] * cell_width + padding * 2
    footer = top + capture['rows'] * cell_height + 16
    height = footer + 96

    def color(value, default):
        return default if value == 'default' else colors.get(value, '#' + value)

    def frame(cue):
        image = Image.new('RGB', (width, height), BACKGROUND)
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=18, outline='#34444e', width=2)
        draw.text((padding, 20), '>_', font=latin, fill=ACCENT)
        draw.text((padding + 36, 17), 'Clash Linux', font=title_font, fill=FOREGROUND)
        draw.text((width - padding - 58, 22), '0.5.0', font=latin, fill=MUTED)
        draw.line((padding, 54, width - padding, 54), fill='#34444e')
        for row in range(screen.lines):
            for column in range(screen.columns):
                char = screen.buffer[row][column]
                if not char.data:
                    continue
                foreground, background = color(char.fg, FOREGROUND), color(char.bg, BACKGROUND)
                if char.reverse:
                    foreground, background = background, foreground
                x, y = padding + column * cell_width, top + row * cell_height
                wide = unicodedata.east_asian_width(char.data[0]) in 'WF'
                if background != BACKGROUND:
                    draw.rectangle((x, y, x + cell_width * (2 if wide else 1) - 1, y + cell_height - 1), fill=background)
                if char.data != ' ':
                    draw.text((x, y + (3 if wide else 0)), char.data, font=chinese if wide else latin,
                              fill=foreground, stroke_width=0)
        if not screen.cursor.hidden:
            x, y = padding + screen.cursor.x * cell_width, top + screen.cursor.y * cell_height
            draw.rectangle((x, y + 3, x + cell_width - 2, y + cell_height - 4), fill=ACCENT)
        number, title, instruction = CUES[cue]
        draw.line((padding, footer, width - padding, footer), fill='#34444e')
        draw.text((padding, footer + 16), number + ' / 06', font=latin, fill=ACCENT)
        draw.text((padding + 112, footer + 11), title, font=title_font, fill=FOREGROUND)
        draw.text((padding + 112, footer + 45), instruction, font=chinese, fill=MUTED)
        return image

    frames, cue, event_index = [], 0, 0
    events = capture['events']
    origin = next(at for at, kind, _ in events if kind == 'output')
    for tick in range(int((capture['duration'] - origin) * 8) + 1):
        at = origin + tick / 8
        while event_index < len(events) and events[event_index][0] <= at:
            _, kind, data = events[event_index]
            if kind == 'output':
                stream.feed(data)
            else:
                cue = data
            event_index += 1
        frames.append(frame(cue))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # One palette prevents antialiased glyphs from changing color between frames.
    samples = Image.new('RGB', (width, height * 3))
    for index, source in enumerate((frames[0], frames[len(frames) // 2], frames[-1])):
        samples.paste(source, (0, index * height))
    palette = samples.quantize(colors=128)
    frames = [image.quantize(palette=palette, dither=Image.Dither.NONE) for image in frames]
    frames[0].save(args.output, save_all=True, append_images=frames[1:], loop=0,
                   duration=125, optimize=True, disposal=1)
    frames[len(frames) // 2].save(args.output.with_suffix('.png'))
    print('Rendered', len(frames), 'samples;', width, '×', height, ';', args.output.stat().st_size, 'bytes')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    capture = commands.add_parser('record')
    capture.add_argument('--binary', type=Path, required=True)
    capture.add_argument('--core', type=Path, required=True)
    capture.add_argument('--output', type=Path, required=True)
    gif = commands.add_parser('render')
    gif.add_argument('--capture', type=Path, required=True)
    gif.add_argument('--output', type=Path, required=True)
    gif.add_argument('--latin-font', type=Path, default=Path('/System/Library/Fonts/Menlo.ttc'))
    gif.add_argument('--cjk-font', type=Path, default=Path('/System/Library/Fonts/STHeiti Light.ttc'))
    args = parser.parse_args()
    (record if args.command == 'record' else render)(args)


if __name__ == '__main__':
    main()
