#!/usr/bin/env python3
"""Black-box MVP contracts against a compiled CLI and real Linux Mihomo.
Python is test tooling only. Production is also exercised with PATH empty.
CLASH_RUST_BINARY=/absolute/clash MIHOMO_BINARY=/absolute/mihomo python3 tests/integration.py
"""
import hashlib
import fcntl
import http.server
import io
import json
import os
import platform
from pathlib import Path
import pty
import select
import signal
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import termios
import threading
import time
import unittest
import urllib.request
import urllib.parse

BINARY = Path(os.environ.get('CLASH_RUST_BINARY', 'target/debug/clash')).resolve()
CORE = Path(os.environ['MIHOMO_BINARY']).resolve() if os.environ.get('MIHOMO_BINARY') else None
PROXY_KEYS = ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY')


def free_ports():
    sockets = [socket.socket() for _ in range(3)]
    try:
        for sock in sockets:
            sock.bind(('127.0.0.1', 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


class Fixture(http.server.BaseHTTPRequestHandler):
    sources = {}
    slow_started = threading.Event()

    def body_for(self, target):
        # HTTP proxies receive absolute-form targets; tunneled traffic uses paths.
        parsed = urllib.parse.urlsplit(target)
        path = parsed.path + ('?' + parsed.query if parsed.query else '')
        return self.sources.get(path, b'UPSTREAM_OK')

    def do_GET(self):
        if self.path == '/slow':
            self.send_response(200)
            self.send_header('Content-Length', '100')
            self.end_headers()
            self.wfile.write(b'x')
            self.wfile.flush()
            self.slow_started.set()
            time.sleep(1.5)
            try:
                self.wfile.write(b'x' * 99)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        body = self.body_for(self.path)
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):
        self.send_response(200)
        self.end_headers()
        self.connection.settimeout(3)
        try:
            request = self.rfile.readline(8192)
            if not request.startswith(b'GET '):
                return
            while self.rfile.readline(8192) not in (b'\r\n', b'\n', b''):
                pass
            path = request.split()[1].decode()
            body = self.body_for(path)
            self.wfile.write(('HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body)).encode() + body)
            self.wfile.flush()
        except OSError:
            pass

    def log_message(self, *_):
        pass


class Harness:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir()
        (self.root / 'bin').mkdir()
        if CORE:
            (self.root / 'bin/mihomo').write_bytes(CORE.read_bytes())
            (self.root / 'bin/mihomo').chmod(0o755)
        self.env = {k: v for k, v in os.environ.items() if k not in PROXY_KEYS and not k.startswith('CLASH_OPERATION_LOCK_')}
        self.env.pop('MIHOMO_BINARY', None)
        ports = free_ports()
        self.env.update(CLASH_HTTP_PROXY=f'http://127.0.0.1:{ports[0]}',
                        CLASH_SOCKS_PROXY=f'socks5h://127.0.0.1:{ports[1]}',
                        MIHOMO_API_URL=f'http://127.0.0.1:{ports[2]}')
        self.command = [str(BINARY), '--root', str(self.root), 'worker']

    def worker(self, action, args=(), ok=True, env=None):
        result = subprocess.run(self.command, input=json.dumps({'action': action, 'args': list(args)}),
                                env=env or self.env, text=True, capture_output=True, timeout=50)
        payload = json.loads(result.stdout)
        if ok and (result.returncode or not payload.get('ok')):
            raise AssertionError(f'{action}: {payload}')
        if not ok and (not result.returncode or payload.get('ok')):
            raise AssertionError(f'{action} unexpectedly succeeded')
        return payload.get('result') if ok else payload

    def state(self):
        return json.loads((self.root / 'runtime/mvp/state.json').read_text())

    def api(self, path):
        request = urllib.request.Request(self.env['MIHOMO_API_URL'] + path,
                                        headers={'Authorization': 'Bearer ' + self.state()['secret']})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
            return json.load(response)

    def traffic(self):
        request = urllib.request.Request('http://fixture.invalid/proof')
        request.set_proxy(self.env['CLASH_HTTP_PROXY'].split('://')[1], 'http')
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
            return response.read()

    def cli(self, *args, **kwargs):
        return subprocess.run([str(BINARY), '--root', str(self.root), *args], env=kwargs.pop('env', self.env),
                              text=True, capture_output=True, timeout=50, **kwargs)


class WorkerContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clash-rust-protocol-')
        self.addCleanup(self.temp.cleanup)
        self.h = Harness(Path(self.temp.name) / 'app')

    def test_rejects_invalid_actions_arguments_and_oversized_requests(self):
        for action, args in [('_load', []), ('add', ['one']), ('use', [None]),
                             ('delays', [['node'] * 101]), ('set_mode', ['wrong'])]:
            payload = self.h.worker(action, args, ok=False)
            self.assertNotIn('Traceback', json.dumps(payload))
        result = subprocess.run(self.h.command, input='x' * 65537 + '\n', env=self.h.env,
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)['ok'])

    def test_status_and_environment_work_without_path_or_python(self):
        env = dict(self.h.env, PATH='/nonexistent')
        self.assertEqual(self.h.worker('status', env=env)['core_state'], 'stopped')
        result = self.h.cli('env', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('export http_proxy=', result.stdout)
        self.assertNotIn('Python', result.stdout)
        self.assertEqual(self.h.cli('off', env=env).returncode, 0)

    def test_non_interactive_menu_fails_promptly(self):
        result = self.h.cli('menu')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('交互终端', result.stderr)

    def test_foreign_pid_is_never_signalled(self):
        target = self.h.root / 'runtime/mihomo.pid'
        target.parent.mkdir(parents=True)
        target.write_text(str(os.getpid()))
        self.h.worker('stop', ok=False)
        self.assertFalse(target.exists())
        os.kill(os.getpid(), 0)


@unittest.skipUnless(CORE and sys.platform == 'linux', 'requires real Linux Mihomo')
class RealChain(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clash-rust-chain-')
        self.addCleanup(self.temp.cleanup)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        Fixture.sources = {'/one': self.provider('Local One'), '/two': self.provider('本地 二'),
                           '/bad': b'not: a provider', '/reserved': self.provider('AUTO')}
        Fixture.slow_started.clear()
        self.h = Harness(Path(self.temp.name) / 'app')
        self.addCleanup(lambda: self.h.worker('stop'))

    def provider(self, name):
        return json.dumps({'proxies': [{'name': name, 'type': 'http', 'server': '127.0.0.1',
                                       'port': self.server.server_port}]}).encode()

    def add(self):
        self.h.worker('add', ['Main', self.url + '/one'])
        return self.h.state()['active_id']

    def test_delete_only_subscription_stops_core_and_allows_adding_again(self):
        identity = self.add()
        self.h.worker('ensure')
        self.h.worker('remove', [identity])
        self.assertFalse(self.h.worker('status')['running'])
        self.assertIsNone(self.h.state()['active_id'])
        self.assertEqual(self.h.state()['subscriptions'], [])
        self.assertFalse((self.h.root / 'conf/config.yaml').exists())
        self.add()
        self.h.worker('ensure')
        self.assertEqual(self.h.traffic(), b'UPSTREAM_OK')

    def test_delete_active_subscription_switches_to_remaining_subscription(self):
        first = self.add()
        self.h.worker('add', ['Backup', self.url + '/two'])
        second = self.h.state()['subscriptions'][1]['id']
        self.h.worker('ensure')
        self.h.worker('remove', [first])
        self.assertEqual(self.h.state()['active_id'], second)
        self.assertTrue(self.h.worker('status')['running'])
        self.assertEqual(self.h.api('/providers/proxies/subscription')['proxies'][0]['name'], '本地 二')
        self.assertEqual(self.h.traffic(), b'UPSTREAM_OK')

    def test_backup_restores_running_configuration_and_different_controller_secret(self):
        self.add()
        self.h.worker('select', ['Local One'])
        self.h.worker('ensure')
        original = self.h.state()
        name = self.h.worker('backup_create')
        self.h.worker('set_mode', ['global'])
        Fixture.sources['/one'] = self.provider('Changed')
        self.h.worker('update', [original['active_id']])
        self.h.worker('backup_restore', [name])
        self.assertEqual(self.h.state()['mode'], 'rule')
        self.assertEqual(self.h.api('/proxies/PROXY')['now'], 'Local One')
        self.assertEqual(self.h.traffic(), b'UPSTREAM_OK')
        other = Harness(Path(self.temp.name) / 'backup-source')
        other.worker('add', ['Other', self.url + '/two'])
        name = other.worker('backup_create')
        source = other.root / 'runtime/mvp/backups' / name
        (self.h.root / 'runtime/mvp/backups' / name).write_bytes(source.read_bytes())
        self.h.worker('backup_restore', [name])
        self.assertNotEqual(self.h.state()['secret'], original['secret'])
        self.assertEqual(self.h.state()['secret'], other.state()['secret'])
        self.assertTrue(self.h.worker('status')['running'])
        self.assertEqual(self.h.traffic(), b'UPSTREAM_OK')

    def test_bulk_update_retains_failed_subscription_backs_up_and_skips_before_due(self):
        self.add()
        self.h.worker('add', ['Backup', self.url + '/two'])
        self.h.worker('ensure')
        original = self.h.state()['subscriptions'][0]
        Fixture.sources['/one'] = b'bad: provider'
        Fixture.sources['/two'] = self.provider('Updated Backup')
        self.h.worker('update_all', ok=False)
        self.assertEqual(self.h.state()['subscriptions'][0], original)
        self.assertEqual(self.h.state()['subscriptions'][1]['nodes'], ['Updated Backup'])
        self.assertTrue(self.h.worker('backup_list'))
        status = json.loads(self.h.cli('subscription', 'status').stdout)
        self.assertEqual(status['last_run']['failed'], 1)
        self.assertNotIn(self.url, json.dumps(status))
        self.assertTrue(self.h.worker('status')['running'])
        settings = self.h.root / 'runtime/mvp/settings.json'
        settings.write_text(json.dumps({'version': 1, 'update_interval_secs': 21600,
                                       'backup_before_update': True, 'backup_keep': 10}))
        before = (self.h.root / 'runtime/mvp/state.json').read_bytes()
        self.assertIn('尚未到', self.h.worker('update_due'))
        self.assertEqual((self.h.root / 'runtime/mvp/state.json').read_bytes(), before)

    def run_scenario(self, h):
        h.worker('add', ['Main', self.url + '/one'])
        first = h.state()['active_id']
        h.worker('add', ['Backup', self.url + '/two'])
        second = h.state()['subscriptions'][1]['id']
        self.assertEqual(h.state()['active_id'], first)
        h.worker('select', ['Local One'])
        h.worker('ensure')
        self.assertTrue(h.worker('status')['running'])
        self.assertEqual(h.api('/proxies/PROXY')['now'], 'Local One')
        h.worker('set_mode', ['global'])
        self.assertEqual(h.api('/configs')['mode'], 'global')
        self.assertEqual(h.api('/proxies/GLOBAL')['now'], 'PROXY')
        self.assertEqual(h.traffic(), b'UPSTREAM_OK')
        h.worker('use', [second])
        h.worker('select', ['本地 二'])
        self.assertEqual(h.api('/proxies/PROXY')['now'], '本地 二')
        self.assertEqual(h.traffic(), b'UPSTREAM_OK')
        h.worker('use', [first])
        self.assertEqual(h.api('/proxies/PROXY')['now'], 'Local One')
        before = (h.root / 'runtime/mvp/state.json').read_bytes()
        config = (h.root / 'conf/config.yaml').read_bytes()
        h.worker('edit', [first, self.url + '/bad'], ok=False)
        self.assertEqual((h.root / 'runtime/mvp/state.json').read_bytes(), before)
        self.assertEqual((h.root / 'conf/config.yaml').read_bytes(), config)
        self.assertEqual(h.traffic(), b'UPSTREAM_OK')
        h.worker('remove', ['missing'], ok=False)
        h.worker('add', ['Main', self.url + '/two'], ok=False)
        h.worker('add', ['Other', self.url + '/one'], ok=False)
        h.worker('add', ['Reserved', self.url + '/reserved'], ok=False)
        Fixture.sources['/one'] = self.provider('Replacement')
        h.worker('update', [first])
        self.assertEqual(h.api('/proxies/PROXY')['now'], 'AUTO')
        h.worker('select', ['Replacement'])
        h.worker('restart')
        self.assertEqual(h.api('/proxies/PROXY')['now'], 'Replacement')
        self.assertEqual(h.traffic(), b'UPSTREAM_OK')
        h.worker('stop')
        self.assertFalse(h.worker('status')['running'])
        h.worker('ensure')
        self.assertEqual(h.api('/proxies/PROXY')['now'], 'Replacement')
        h.worker('remove', [second])
        self.assertEqual(len(h.state()['subscriptions']), 1)
        env = dict(h.env, PATH='/nonexistent')
        self.assertTrue(h.worker('status', env=env)['running'])
        status = h.worker('status')
        self.assertNotIn('url', status['subscriptions'][0])
        self.assertNotIn('cache', status['subscriptions'][0])
        h.worker('stop')
        return (status['name'], status['mode'], status['node'], status['subscriptions'][0]['nodes'])

    def test_real_traffic_switch_update_restart_and_offline_runtime(self):
        self.run_scenario(self.h)

    def test_cancel_before_commit_preserves_configuration(self):
        self.add()
        state = (self.h.root / 'runtime/mvp/state.json').read_bytes()
        config = (self.h.root / 'conf/config.yaml').read_bytes()
        process = subprocess.Popen(self.h.command, env=self.h.env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        process.stdin.write(json.dumps({'action': 'edit', 'args': [self.h.state()['active_id'], self.url + '/slow']}) + '\n')
        process.stdin.flush()
        self.assertTrue(Fixture.slow_started.wait(5))
        process.send_signal(signal.SIGINT)
        output, _ = process.communicate(timeout=10)
        self.assertTrue(json.loads(output)['cancelled'])
        self.assertEqual((self.h.root / 'runtime/mvp/state.json').read_bytes(), state)
        self.assertEqual((self.h.root / 'conf/config.yaml').read_bytes(), config)
        self.assertFalse((self.h.root / 'runtime/mvp/transaction.json').exists())

    def test_subscription_fetch_uses_shell_proxy_but_controller_remains_direct(self):
        self.h.env.update(http_proxy=self.url, https_proxy=self.url, no_proxy='', NO_PROXY='')
        self.h.worker('add', ['Proxied', 'http://subscription.invalid/one'])
        self.h.worker('ensure')
        self.assertTrue(self.h.worker('status')['running'])
        self.assertEqual(self.h.traffic(), b'UPSTREAM_OK')

    def test_interrupted_transaction_recovers_old_selection(self):
        self.add()
        self.h.worker('ensure')
        old = self.h.state()
        config = (self.h.root / 'conf/config.yaml').read_text()
        record = {'state': old, 'config': config, 'running': True, 'had_state': True}
        journal = self.h.root / 'runtime/mvp/transaction.json'
        journal.write_text(json.dumps(record))
        self.h.worker('set_mode', ['global'])
        self.assertFalse(journal.exists())
        self.assertEqual(self.h.api('/configs')['mode'], 'global')
        self.assertEqual(self.h.traffic(), b'UPSTREAM_OK')

    def test_occupied_port_and_corrupt_cache_do_not_start_or_stop_core(self):
        self.add()
        port = int(self.h.env['CLASH_HTTP_PROXY'].rsplit(':', 1)[1])
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', port))
            listener.listen()
            self.h.worker('ensure', ok=False)
        self.h.worker('ensure')
        item = self.h.state()['subscriptions'][0]
        (self.h.root / 'conf' / item['cache']).write_bytes(b'damaged')
        self.h.worker('restart', ok=False)
        self.assertTrue(self.h.worker('status')['running'])

    def test_plain_menu_adds_subscription_hides_url_and_applies_mode(self):
        master, slave = pty.openpty()
        env = dict(self.h.env, TERM='xterm-256color')
        def session():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        process = subprocess.Popen([str(BINARY), '--root', str(self.h.root), 'menu', '--plain'],
                                   env=env, stdin=slave, stdout=slave, stderr=slave, preexec_fn=session)
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        def cleanup():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.addCleanup(cleanup)
        output = bytearray()
        history = bytearray()
        def wait_for(value):
            deadline = time.monotonic() + 5
            expected = value.encode()
            while expected not in output and time.monotonic() < deadline:
                if select.select([master], [], [], .05)[0]:
                    chunk = os.read(master, 65536)
                    output.extend(chunk)
                    history.extend(chunk)
            self.assertIn(expected, output)
            output.clear()
        wait_for('请输入编号')
        os.write(master, b'3\n')
        wait_for('输入编号查看详情')
        os.write(master, b'a\n')
        wait_for('为订阅起一个名称')
        os.write(master, '文本订阅\n'.encode())
        wait_for('请输入订阅地址')
        time.sleep(.1)  # Wait until the prompt has switched the tty to hidden input.
        Fixture.sources['/one?token=PLAIN_SECRET'] = self.provider('Plain Node')
        os.write(master, (self.url + '/one?token=PLAIN_SECRET\r').encode())
        wait_for('已添加并设为当前订阅')
        self.assertNotIn(b'PLAIN_SECRET', history)
        os.write(master, b'0\n')
        wait_for('3 订阅 / 4 诊断 / 0 退出')
        os.write(master, b'2\n')
        wait_for('默认规则')
        os.write(master, b'2\n')
        wait_for('模式已保存')
        os.write(master, b'0\n')
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(self.h.state()['mode'], 'global')
        self.assertEqual(self.h.state()['subscriptions'][0]['name'], '文本订阅')

    @unittest.skipUnless(os.environ.get('MIHOMO_TEST_ARCHIVE'), 'optional offline core archive')
    def test_core_installs_offline_without_path_and_then_reuses_existing_file(self):
        (self.h.root / 'bin/mihomo').unlink()
        env = dict(self.h.env, PATH='/nonexistent', MIHOMO_ARCHIVE=os.environ['MIHOMO_TEST_ARCHIVE'])
        result = self.h.cli('install', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.h.root / 'bin/mihomo'
        before = target.stat().st_mtime_ns
        env['MIHOMO_ARCHIVE'] = '/does/not/exist'
        result = self.h.cli('install', env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('无需下载', result.stdout)
        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_import_and_upgrade_running_install_preserve_traffic_and_core(self):
        self.add()
        prefix = Path(self.temp.name) / 'migrated'
        installed = prefix / 'clash'
        result = subprocess.run([str(BINARY), 'self-install', '--prefix', str(prefix),
                                 '--import-data', str(self.h.root), '--core', str(CORE), '--no-shell'],
                                env=self.h.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((prefix / 'runtime/mvp/state.json').read_bytes(),
                         (self.h.root / 'runtime/mvp/state.json').read_bytes())
        def cli(*args):
            return subprocess.run([str(installed), *args], env=dict(self.h.env, PATH='/nonexistent'),
                                  capture_output=True, text=True, timeout=30)
        self.addCleanup(lambda: cli('stop'))
        self.assertEqual(cli('start').returncode, 0)
        self.assertEqual(self.h.traffic(), b'UPSTREAM_OK')
        archive = Path(self.temp.name) / 'thin-update.tar.gz'
        info = json.loads(subprocess.check_output([str(BINARY), '--build-info']))
        with tarfile.open(archive, 'w:gz', format=tarfile.USTAR_FORMAT) as tar:
            for name, body in [('clash', BINARY.read_bytes()), ('BUILD.json', json.dumps({
                    'implementation': 'rust', 'version': info['version'], 'architecture': info['arch']}).encode()),
                               ('LICENSE', b'project license'), ('licenses/dependency/LICENSE', b'dependency license')]:
                entry = tarfile.TarInfo(name)
                entry.size = len(body)
                tar.addfile(entry, io.BytesIO(body))
        before = (prefix / 'bin/mihomo').read_bytes()
        result = cli('upgrade', '--archive', str(archive), '--sha256', hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((prefix / 'bin/mihomo').read_bytes(), before)
        self.assertEqual(self.h.traffic(), b'UPSTREAM_OK')
        self.assertIn('运行中', cli('status').stdout)


class Distribution(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clash-rust-install-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.prefix = self.root / 'installed'
        self.env = dict(os.environ, HOME=str(self.root / 'home'), SHELL='/bin/bash')
        self.env['PATH'] = os.environ.get('PATH', '')
        Path(self.env['HOME']).mkdir()
        self.install()

    def install(self):
        result = subprocess.run([str(BINARY), 'self-install', '--prefix', str(self.prefix), '--skip-core', '--no-shell'],
                                env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def cli(self, *args):
        return subprocess.run([str(self.prefix / 'clash'), *args], env=self.env, capture_output=True, text=True, timeout=20)

    def fake_crontab(self):
        bin_dir = self.root / 'fake-bin'
        bin_dir.mkdir()
        program = bin_dir / 'crontab'
        program.write_text('#!' + sys.executable + '\n' + '''import os, sys
from pathlib import Path
p = Path(os.environ['TEST_CRONTAB_FILE'])
if sys.argv[1] == '-l':
    print(p.read_text(), end='')
elif os.environ.get('TEST_CRONTAB_FAIL'):
    sys.exit(1)
else:
    p.write_text(sys.stdin.read())
''')
        program.chmod(0o755)
        contents = self.root / 'crontab'
        contents.write_text('# original\n0 1 * * * /bin/true\n')
        self.env.update(TEST_CRONTAB_FILE=str(contents), PATH=str(bin_dir) + ':' + self.env['PATH'])
        return contents

    def test_auto_update_configures_only_its_cron_job_and_uninstall_removes_it(self):
        cron = self.fake_crontab()
        original = cron.read_text()
        for _ in range(2):
            result = self.cli('subscription', 'auto-update', '6h')
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(cron.read_text().count('subscription update-due'), 1)
        self.assertIn(original, cron.read_text())
        self.assertEqual(json.loads(self.cli('subscription', 'status').stdout)['settings']['update_interval_secs'], 21600)
        self.env['TEST_CRONTAB_FAIL'] = '1'
        self.assertNotEqual(self.cli('subscription', 'auto-update', '12h').returncode, 0)
        self.assertEqual(json.loads(self.cli('subscription', 'status').stdout)['settings']['update_interval_secs'], 21600)
        self.env.pop('TEST_CRONTAB_FAIL')
        self.assertNotEqual(self.cli('subscription', 'auto-update', '1m').returncode, 0)
        self.assertEqual(self.cli('uninstall').returncode, 0)
        self.assertEqual(cron.read_text(), original)

    def test_backup_retention_private_files_and_invalid_archive_rejection(self):
        self.assertEqual(self.cli('backup', 'keep', '1').returncode, 0)
        first = self.cli('backup', 'create').stdout.strip()
        second = self.cli('backup', 'create').stdout.strip()
        self.assertNotEqual(first, second)
        self.assertEqual(json.loads(self.cli('backup', 'list').stdout), [second])
        folder = self.prefix / 'runtime/mvp/backups'
        self.assertEqual((folder / second).stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(self.cli('backup', 'restore', '../../escape').returncode, 0)
        malicious = folder / 'backup-malicious.tar.gz'
        with tarfile.open(malicious, 'w:gz') as tar:
            entry = tarfile.TarInfo('../escape')
            entry.size = 4
            tar.addfile(entry, io.BytesIO(b'evil'))
        self.assertNotEqual(self.cli('backup', 'restore', malicious.name).returncode, 0)
        self.assertFalse((self.prefix / 'runtime/mvp/escape').exists())

    def archive(self, malicious=False):
        target = self.root / 'update.tar.gz'
        info = json.loads(subprocess.check_output([str(BINARY), '--build-info']))
        with tarfile.open(target, 'w:gz', format=tarfile.USTAR_FORMAT) as archive:
            for name, data in [('clash', BINARY.read_bytes()), ('BUILD.json', json.dumps({
                'implementation': 'rust', 'version': info['version'], 'architecture': info['arch']}).encode()),
                               ('LICENSE', b'project license'),
                               ('licenses/dependency/LICENSE', b'dependency license')]:
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
            if malicious:
                entry = tarfile.TarInfo('conf/config.yaml')
                entry.size = 4
                archive.addfile(entry, io.BytesIO(b'evil'))
        return target, hashlib.sha256(target.read_bytes()).hexdigest()

    def test_archive_install_preserves_licenses_and_rejects_invalid_packages(self):
        archive, checksum = self.archive()
        prefix = self.root / 'from-archive'
        command = [str(BINARY), 'self-install', '--prefix', str(prefix), '--skip-core', '--no-shell',
                   '--archive', str(archive)]
        for options in ([], ['--sha256', '0' * 64]):
            result = subprocess.run(command + options, env=self.env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(prefix.exists())
        result = subprocess.run(command + ['--sha256', checksum], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((prefix / 'licenses/dependency/LICENSE').read_bytes(), b'dependency license')
        self.assertEqual((prefix / 'LICENSE').read_bytes(), b'project license')
        malicious, checksum = self.archive(malicious=True)
        other = self.root / 'rejected-archive'
        result = subprocess.run([str(BINARY), 'self-install', '--prefix', str(other), '--skip-core', '--no-shell',
                                 '--archive', str(malicious), '--sha256', checksum],
                                env=self.env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(other.exists())

    def bootstrap(self, prefix, *args, env=None):
        installer = Path(__file__).resolve().parents[2] / 'install.sh'
        return subprocess.run(['/bin/bash', str(installer), '--prefix', str(prefix), '--no-shell', *args],
                              env=env or self.env, text=True, capture_output=True, timeout=40)

    @unittest.skipUnless(sys.platform == 'linux' and platform.machine() == 'x86_64', 'Linux x86_64 bootstrap')
    def test_bootstrap_offline_install_and_repeat_upgrade_preserve_data(self):
        archive, checksum = self.archive()
        prefix = self.root / 'bootstrap install with spaces'
        result = self.bootstrap(prefix, '--archive', str(archive), '--skip-core')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(prefix.exists())
        result = self.bootstrap(prefix, '--archive', str(archive), '--sha256', '0' * 64, '--skip-core')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(prefix.exists())
        archive.with_suffix(archive.suffix + '.sha256').write_text(checksum + '  update.tar.gz\n')
        result = self.bootstrap(prefix, '--archive', str(archive), '--skip-core')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((prefix / 'python').exists())
        self.assertTrue((prefix / 'licenses/dependency/LICENSE').is_file())
        (prefix / 'conf').mkdir()
        (prefix / 'conf/keep').write_text('unchanged')
        result = self.bootstrap(prefix, '--archive', str(archive))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((prefix / 'conf/keep').read_text(), 'unchanged')
        self.assertTrue(prefix.with_name('.' + prefix.name + '.rust-previous').is_dir())
        malicious, bad_sum = self.archive(malicious=True)
        rejected = self.root / 'invalid bootstrap'
        result = self.bootstrap(rejected, '--archive', str(malicious), '--sha256', bad_sum, '--skip-core')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(rejected.exists())

    @unittest.skipUnless(sys.platform == 'linux' and platform.machine() == 'x86_64', 'Linux x86_64 bootstrap')
    def test_bootstrap_https_download_checksum_and_transport_rules(self):
        archive, checksum = self.archive()
        cert, key = self.root / 'cert.pem', self.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-addext', 'subjectAltName=IP:127.0.0.1,DNS:localhost',
                        '-keyout', str(key), '-out', str(cert)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        name = 'clash-linux-rust-x86_64-unknown-linux-gnu.tar.gz'
        files = {'/' + name: archive.read_bytes(), '/' + name + '.sha256': (checksum + '  ' + name + '\n').encode()}
        requests = []

        class Release(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                if self.path.startswith('/redirect/'):
                    self.send_response(302)
                    self.send_header('Location', 'http://127.0.0.1/blocked')
                    self.end_headers()
                    return
                body = files.get(self.path)
                self.send_response(200 if body is not None else 404)
                body = body or b'not found'
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Release)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'https://127.0.0.1:{server.server_port}'
            env = {k: v for k, v in self.env.items() if k not in PROXY_KEYS}
            env.update(CLASH_INSTALL_BASE_URL=base, CURL_CA_BUNDLE=str(cert))
            prefix = self.root / 'downloaded'
            result = self.bootstrap(prefix, '--skip-core', env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(requests, ['/' + name, '/' + name + '.sha256'])
            self.assertTrue((prefix / 'licenses/dependency/LICENSE').is_file())
            rejected = self.root / 'rejected-download'
            files['/' + name + '.sha256'] = ('0' * 64 + '  ' + name + '\n').encode()
            result = self.bootstrap(rejected, '--skip-core', env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(rejected.exists())
            for url in (base.replace('https:', 'http:'), base + '/redirect'):
                result = self.bootstrap(rejected, '--skip-core', env=dict(env, CLASH_INSTALL_BASE_URL=url))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(rejected.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_install_contains_no_python_and_refuses_existing_directory(self):
        self.assertFalse((self.prefix / 'python').exists())
        result = subprocess.run([str(BINARY), 'self-install', '--prefix', str(self.prefix), '--skip-core'],
                                env=self.env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.cli('status').returncode, 0)

    def test_shell_integration_and_proxy_cleanup(self):
        self.assertEqual(self.cli('shell-init', '--shell', 'bash').returncode, 0)
        self.assertEqual(self.cli('shell-init', '--shell', 'bash').returncode, 0)
        rc = (Path(self.env['HOME']) / '.bashrc').read_text()
        self.assertEqual(rc.count('# >>> clash_linux shell integration >>>'), 1)
        script = 'source "$1"; export http_proxy=old ALL_PROXY=old; clash off; test -z "${http_proxy+x}"; test -z "${ALL_PROXY+x}"'
        result = subprocess.run(['/bin/bash', '-c', script, 'bash', str(self.prefix / 'env.sh')], env=self.env, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.cli('shell-uninstall', '--shell', 'bash').returncode, 0)
        self.assertNotIn('# >>> clash_linux shell integration >>>', (Path(self.env['HOME']) / '.bashrc').read_text())

    def test_offline_upgrade_preserves_data_and_core_then_rolls_back(self):
        (self.prefix / 'conf').mkdir()
        (self.prefix / 'conf/custom').write_text('keep')
        (self.prefix / 'bin').mkdir(exist_ok=True)
        (self.prefix / 'bin/mihomo').write_text('core unchanged')
        archive, checksum = self.archive()
        result = self.cli('upgrade', '--archive', str(archive), '--sha256', checksum)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.prefix / 'conf/custom').read_text(), 'keep')
        self.assertEqual((self.prefix / 'bin/mihomo').read_text(), 'core unchanged')
        result = self.cli('rollback')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.prefix / 'conf/custom').read_text(), 'keep')
        self.assertEqual((self.prefix / 'bin/mihomo').read_text(), 'core unchanged')

    def test_rejects_bad_checksum_and_archive_data_overwrite(self):
        archive, checksum = self.archive(malicious=True)
        original = (self.prefix / 'clash').read_bytes()
        for value in ('0' * 64, checksum):
            result = self.cli('upgrade', '--archive', str(archive), '--sha256', value)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual((self.prefix / 'clash').read_bytes(), original)
            self.assertFalse((self.prefix / 'conf/config.yaml').exists())

    def test_uninstall_preserves_data_and_reinstall_restores_it(self):
        (self.prefix / 'conf').mkdir()
        (self.prefix / 'conf/custom').write_text('keep')
        result = self.cli('uninstall')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.prefix / 'clash').exists())
        self.assertEqual((self.prefix / 'conf/custom').read_text(), 'keep')
        self.install()
        self.assertEqual((self.prefix / 'conf/custom').read_text(), 'keep')
        result = self.cli('uninstall', '--purge')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.prefix.exists())

    def test_purge_also_removes_owned_rollback_backup(self):
        archive, checksum = self.archive()
        result = self.cli('upgrade', '--archive', str(archive), '--sha256', checksum)
        self.assertEqual(result.returncode, 0, result.stderr)
        backup = self.prefix.with_name('.' + self.prefix.name + '.rust-previous')
        self.assertTrue(backup.exists())
        result = self.cli('uninstall', '--purge')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(backup.exists())

    def test_recover_restores_install_when_interrupted_between_directory_renames(self):
        backup = self.prefix.with_name('.' + self.prefix.name + '.rust-previous')
        stage = self.root / '.clash-rust-upgrade-interrupted'
        stage.mkdir()
        journal = self.prefix.with_name('.' + self.prefix.name + '.upgrade.json')
        journal.write_text(json.dumps({'format': 1, 'implementation': 'rust', 'root': str(self.prefix),
                                       'stage': str(stage), 'previous': str(backup), 'running': False}))
        self.prefix.rename(backup)
        result = subprocess.run([str(BINARY), '--root', str(self.prefix), 'recover'],
                                env=self.env, text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.prefix / 'clash').exists())
        self.assertFalse(journal.exists())
        self.assertEqual(self.cli('status').returncode, 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
