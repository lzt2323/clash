#!/usr/bin/env python3
"""Real Mihomo + local subscription and upstream fixtures; no real credentials/network.
Linux uses the production lifecycle script. macOS substitutes only /proc lifecycle.
Run: MIHOMO_BINARY=/path/to/mihomo python3 tests/terminal_mvp_integration.py
"""
import contextlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('terminal_mvp', ROOT / 'scripts/terminal_mvp.py')
mvp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mvp)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class Fixture(http.server.BaseHTTPRequestHandler):
    sources = {}

    def do_GET(self):
        body = self.sources.get(self.path, b'UPSTREAM_OK')
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):
        self.send_response(200)
        self.end_headers()
        self.connection.settimeout(3)
        try:
            line = self.rfile.readline(8192)
            if not line.startswith(b'GET '):
                return
            while self.rfile.readline(8192) not in (b'\r\n', b'\n', b''):
                pass
            self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\nUPSTREAM_OK')
            self.wfile.flush()
        except OSError:
            pass

    def log_message(self, *_):
        pass


class NativeTestManager(mvp.Manager):
    """macOS-only test adapter. Config, provider, API and traffic are all real."""
    process = None

    def _runtime(self, command, check=True):
        binary = os.environ['MIHOMO_BINARY']
        result = subprocess.CompletedProcess([command], 0, stdout=binary, stderr='')
        if command == 'binary':
            return result
        if command == 'running':
            result.returncode = 0 if self.process and self.process.poll() is None else 1
        elif command == 'health':
            try:
                if not self.process or self.process.poll() is not None:
                    raise mvp.Error('stopped')
                self._api('/version')
            except mvp.Error:
                result.returncode = 1
        elif command == 'start':
            self.process = subprocess.Popen([binary, '-d', str(self.conf_dir), '-f', str(self.config)],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                try:
                    self._api('/version')
                    return result
                except mvp.Error:
                    time.sleep(.1)
            raise mvp.Error('isolated core did not start')
        elif command == 'stop':
            if self.process and self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=5)
        else:
            raise AssertionError(command)
        return result


@unittest.skipUnless(os.environ.get('MIHOMO_BINARY'), 'set MIHOMO_BINARY to run real engine checks')
class RealChain(unittest.TestCase):
    def test_subscription_switch_traffic_and_rollback(self):
        temporary = tempfile.TemporaryDirectory(prefix='clash-chain-')
        self.addCleanup(temporary.cleanup)
        with contextlib.nullcontext(temporary.name) as tmp:
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            upstream = server.server_port
            def provider(name):
                return json.dumps({'proxies': [{'name': name, 'type': 'http', 'server': '127.0.0.1', 'port': upstream}]}).encode()
            Fixture.sources = {'/one': provider('Local One'), '/two': provider('本地 二'), '/bad': b'not: a provider'}
            env = {'CLASH_HTTP_PROXY': 'http://127.0.0.1:' + str(free_port()),
                   'CLASH_SOCKS_PROXY': 'socks5h://127.0.0.1:' + str(free_port()),
                   'MIHOMO_API_URL': 'http://127.0.0.1:' + str(free_port()),
                   'CLASH_RUNTIME_SCRIPT': str(ROOT / 'scripts/mihomo_runtime.sh')}
            with patch.dict(os.environ, env):
                cls = mvp.Manager if platform.system() == 'Linux' else NativeTestManager
                manager = cls(tmp)
                self.addCleanup(lambda: manager._runtime('stop', check=False))
                manager.add('Main', 'http://127.0.0.1:%s/one' % upstream)
                first = manager._load()['active_id']
                manager.add('Backup', 'http://127.0.0.1:%s/two' % upstream)
                second = manager._load()['subscriptions'][1]['id']
                manager.select('Local One')
                manager.ensure()
                self.assertTrue(manager.status()['running'])
                self.assertEqual(manager._api('/proxies/PROXY')['now'], 'Local One')
                manager.set_mode('global')
                self.assertEqual(manager._api('/proxies/GLOBAL')['now'], 'PROXY')
                self.assertEqual(manager._api('/configs')['mode'], 'global')
                def traffic():
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    req = urllib.request.Request('http://fixture.invalid/proof')
                    req.set_proxy(manager.http_url.split('://', 1)[1], 'http')
                    with opener.open(req, timeout=3) as response:
                        return response.read()
                self.assertEqual(traffic(), b'UPSTREAM_OK')
                manager.use(second)
                manager.select('本地 二')
                self.assertEqual(manager._api('/proxies/PROXY')['now'], '本地 二')
                self.assertEqual(traffic(), b'UPSTREAM_OK')
                manager.use(first)
                self.assertEqual(manager._api('/proxies/PROXY')['now'], 'Local One')
                before = manager.state_path.read_bytes()
                config = manager.config.read_bytes()
                with self.assertRaises(mvp.Error):
                    manager.edit(first, 'http://127.0.0.1:%s/bad' % upstream)
                self.assertEqual(manager.state_path.read_bytes(), before)
                self.assertEqual(manager.config.read_bytes(), config)
                self.assertEqual(traffic(), b'UPSTREAM_OK')
                real_reload = manager._reload
                calls = []
                def fail_once(state):
                    calls.append(state['active_id'])
                    if len(calls) == 1:
                        raise mvp.Error('synthetic runtime reload failure')
                    return real_reload(state)
                with patch.object(manager, '_reload', side_effect=fail_once):
                    with self.assertRaises(mvp.Error):
                        manager.use(second)
                self.assertEqual(calls, [second, first])
                self.assertEqual(manager.state_path.read_bytes(), before)
                self.assertEqual(manager.config.read_bytes(), config)
                self.assertEqual(traffic(), b'UPSTREAM_OK')
                Fixture.sources['/one'] = provider('Replacement')
                manager.update(first)
                self.assertEqual(manager._api('/proxies/PROXY')['now'], 'AUTO')
                manager.select('Replacement')
                self.assertEqual(traffic(), b'UPSTREAM_OK')
                manager.restart()
                self.assertEqual(manager._api('/proxies/PROXY')['now'], 'Replacement')
                self.assertEqual(traffic(), b'UPSTREAM_OK')
                manager.stop()
                self.assertFalse(manager.status()['running'])
                manager.ensure()
                self.assertEqual(manager._api('/proxies/PROXY')['now'], 'Replacement')
                self.assertEqual(traffic(), b'UPSTREAM_OK')
                manager.stop()


if __name__ == '__main__':
    unittest.main(verbosity=2)
