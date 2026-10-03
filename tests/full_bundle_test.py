#!/usr/bin/env python3
"""Linux release acceptance: no system Python, real core, uninstall and restore."""
import argparse
import hashlib
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import tarfile
import tempfile
import threading


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Fixture(http.server.BaseHTTPRequestHandler):
    def do_CONNECT(self):
        self.send_response(200)
        self.end_headers()
        self.connection.settimeout(5)
        try:
            if not self.rfile.readline(8192).startswith(b'GET '):
                return
            while self.rfile.readline(8192) not in (b'\r\n', b'\n', b''):
                pass
            self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Length: 15\r\nConnection: close\r\n\r\nBUNDLE_PROXY_OK')
            self.wfile.flush()
        except OSError:
            pass

    def do_GET(self):
        if self.path.startswith('/sub'):
            body = json.dumps({'proxies': [{'name': 'Local test', 'type': 'http',
                                'server': '127.0.0.1', 'port': self.server.server_port}]}).encode()
        else:
            body = b'BUNDLE_PROXY_OK'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--installer', type=Path, default=Path(__file__).resolve().parents[1] / 'install.sh')
    args = parser.parse_args()
    digest = hashlib.sha256(args.archive.read_bytes()).hexdigest()
    with tarfile.open(args.archive) as package:
        version = json.load(package.extractfile('clash-linux/BUILD.json'))['version']
    with tempfile.TemporaryDirectory(prefix='clash-bundle-acceptance-') as temp:
        home = Path(temp)
        root = home / '.local/share/clash-linux'
        fake = home / 'fake-bin'; fake.mkdir()
        for name in ('python', 'python3', 'pip', 'pip3'):
            path = fake / name
            path.write_text('#!/bin/sh\necho SYSTEM_PYTHON_USED >&2\nexit 99\n')
            path.chmod(0o755)
        env = dict(os.environ, HOME=str(home), SHELL='/bin/bash',
                   PATH=str(fake) + ':/usr/local/bin:/usr/bin:/bin',
                   PYTHONHOME='/nonexistent-poison', PYTHONPATH='/nonexistent-poison',
                   CLASH_HTTP_PROXY='http://127.0.0.1:' + str(port()),
                   CLASH_SOCKS_PROXY='socks5h://127.0.0.1:' + str(port()),
                   MIHOMO_API_URL='http://127.0.0.1:' + str(port()))
        for key in ('http_proxy','https_proxy','all_proxy','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY',
                    'CLASH_RUNTIME_SCRIPT','MIHOMO_BINARY','MIHOMO_RUNTIME_DIR','MIHOMO_CONFIG'):
            env.pop(key, None)
        env['no_proxy'] = env['NO_PROXY'] = '127.0.0.1,localhost'

        def run(argv, **kwargs):
            result = subprocess.run([str(x) for x in argv], env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120, **kwargs)
            if result.returncode:
                raise RuntimeError('Bundle acceptance command failed: ' + result.stdout + result.stderr)
            return result

        install = ['bash', args.installer.resolve(), '--version', version,
                   '--archive', args.archive.resolve(), '--sha256', digest]
        run(install)
        assert root.joinpath('python/bin/python3').is_file()
        run([root / 'clash', 'status'])
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            worker = [root / 'python/bin/python3', '-E', '-s', '-B', root / 'scripts/terminal_mvp.py', 'worker']
            for name in ('First', 'Second'):
                result = run(worker, input=json.dumps({'action': 'add', 'args': [name, 'http://127.0.0.1:%s/sub%s' % (server.server_port, name)]}))
                assert json.loads(result.stdout)['ok'], 'subscription import failed'
            result = run(['bash', '--noprofile', '--norc', '-c',
                          'source "$1/env.sh"; clash start && clash on && '
                          'test "$http_proxy" = "$CLASH_HTTP_PROXY" && '
                          'curl --fail --silent --max-time 10 --noproxy "" http://fixture.invalid/proof && '
                          'clash off && test -z "${http_proxy:-}" && clash stop && clash start',
                          'bundle-test', root])
            assert 'BUNDLE_PROXY_OK' in result.stdout
            state = (root / 'runtime/mvp/state.json').read_bytes()
            run(install)  # Repeat install must preserve live state.
            assert (root / 'runtime/mvp/state.json').read_bytes() == state
            run(['bash', '--noprofile', '--norc', '-c',
                 'source "$1/env.sh"; clash uninstall && ! declare -F clash >/dev/null', 'bundle-test', root])
            assert not (root / 'python').exists()
            assert not (root / 'runtime/mihomo.pid').exists()
            assert (root / 'runtime/mvp/state.json').read_bytes() == state
            run(install)
            assert (root / 'runtime/mvp/state.json').read_bytes() == state
            run([root / 'clash', 'start'])
            run([root / 'clash', 'uninstall', '--purge'])
            assert not (root / 'conf').exists()
            assert not (root / 'runtime/mvp').exists()
            assert not (home / '.local/bin/clash').exists()
            assert 'clash_linux shell integration' not in (home / '.bashrc').read_text()
            print('PASS: private Python, two subscriptions, core/shell/traffic, repeated install, uninstall, restore, purge')
        finally:
            if (root / 'clash').exists():
                subprocess.run([str(root / 'clash'), 'stop'], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            server.shutdown(); server.server_close()


if __name__ == '__main__':
    main()
