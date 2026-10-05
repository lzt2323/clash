#!/usr/bin/env python3
"""Fault injection and format tests for the upgrade transaction engine."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('clash_upgrade', ROOT / 'scripts/upgrade.py')
u = importlib.util.module_from_spec(spec)
spec.loader.exec_module(u)
ARCH = {'arm64': 'arm64', 'aarch64': 'arm64'}.get(platform.machine(), 'amd64')


class PowerLoss(BaseException):
    pass


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='upgrade-test-')
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name).resolve()
        self.root = self.parent / 'install'
        self.make_release(self.root, 'v0.1.1', installed=True)
        self.put(self.root, 'conf/config.yaml', 'secret: preserved')
        self.put(self.root, 'runtime/mvp/state.json', 'old-state')
        self.put(self.root, '.env', 'MY_KEY=private')
        self.put(self.root, 'env.sh', 'generated env')
        self.put(self.root, 'user/note', 'personal note')
        self.source = self.parent / 'clash-linux'
        self.make_release(self.source, 'v0.2.0')
        self.archive = self.pack()
        self.running = False
        self.calls = []
        self.fail_health = False
        self.engine = u.Upgrader(self.root, runner=self.run_command)

    def put(self, root, name, content):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def make_release(self, root, version, installed=False):
        root.mkdir()
        files = ['clash', 'python/bin/python3', 'bin/mihomo', 'scripts/mihomo_runtime.sh', 'LICENSE']
        for name in files:
            self.put(root, name, 'application ' + version).chmod(0o755)
        self.put(root, 'BUILD.json', json.dumps({'version': version, 'architecture': ARCH,
            'files': files, 'data_format': 1, 'min_upgrade_version': 'v0.1.0'}))
        if installed:
            self.put(root, '.clash-install.json', json.dumps({'format': 1, 'version': version, 'prefix': str(root)}))

    def pack(self):
        archive = self.parent / 'release.tar.gz'
        with tarfile.open(archive, 'w:gz') as tf:
            tf.add(self.source, arcname='clash-linux')
        return archive

    def run_command(self, argv, **kwargs):
        action = argv[-1]
        self.calls.append(action)
        result = 0
        if action == 'running':
            result = int(not self.running)
        elif action == 'stop':
            self.running = False
        elif action == 'start':
            self.running = True
        elif action == 'health' and self.fail_health:
            self.fail_health = False
            result = 1
        return subprocess.CompletedProcess(argv, result)

    def upgrade(self, **kw):
        return self.engine.execute(archive=self.archive, sha256=u.digest(self.archive), **kw)

    def installed_version(self):
        return json.loads((self.root / 'BUILD.json').read_text())['version']

    def test_upgrade_preserves_files_stopped_and_rollback_preserves_new_data(self):
        self.upgrade()
        self.assertEqual(self.installed_version(), 'v0.2.0')
        self.assertFalse(self.running)
        self.assertNotIn('start', self.calls)
        for name, expected in [('user/note', 'personal note'), ('.env', 'MY_KEY=private'),
                               ('env.sh', 'generated env'), ('runtime/mvp/state.json', 'old-state')]:
            self.assertEqual((self.root / name).read_text(), expected)
        self.put(self.root, 'runtime/mvp/state.json', 'new selection')
        self.put(self.root, 'user/new', 'after upgrade')
        self.engine.execute('rollback')
        self.assertEqual(self.installed_version(), 'v0.1.1')
        self.assertEqual((self.root / 'runtime/mvp/state.json').read_text(), 'new selection')
        self.assertEqual((self.root / 'user/new').read_text(), 'after upgrade')

    def test_private_directory_modes_survive_upgrade_and_rollback(self):
        self.root.chmod(0o700)
        (self.root / 'conf').chmod(0o700)
        (self.root / 'runtime').chmod(0o700)
        (self.root / 'user').chmod(0o700)
        (self.root / 'conf/config.yaml').chmod(0o644)
        self.upgrade()
        for path in (self.root, self.root / 'conf', self.root / 'runtime', self.root / 'user'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.engine.execute('rollback')
        self.assertEqual((self.root / 'conf').stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / 'conf/config.yaml').stat().st_mode & 0o777, 0o644)

    def test_running_core_restored_on_upgrade_and_rollback(self):
        self.running = True
        self.upgrade()
        self.assertTrue(self.running)
        self.assertEqual(self.calls.count('stop'), 1)
        self.assertEqual(self.calls.count('start'), 1)
        self.engine.execute('rollback')
        self.assertTrue(self.running)

    def test_health_failure_automatically_restores_old_core_and_data(self):
        self.running = True
        self.fail_health = True
        with self.assertRaises(u.UpgradeError):
            self.upgrade()
        self.assertEqual(self.installed_version(), 'v0.1.1')
        self.assertTrue(self.running)
        self.assertFalse(self.engine.journal.exists())

    def test_power_loss_at_every_boundary_recovers_deterministically(self):
        # Each subcase gets an independent installation and backup history.
        for phase in ('prepared', 'data-copied', 'stopped', 'old-renamed', 'new-renamed',
                      'healthy', 'committed', 'backup-published', 'work-cleared'):
            with self.subTest(phase=phase):
                temporary = self.parent / phase
                self.make_release(temporary, 'v0.1.1', installed=True)
                self.put(temporary, 'conf/config.yaml', 'fixture')
                def crash(current):
                    if current == phase:
                        raise PowerLoss()
                engine = u.Upgrader(temporary, runner=self.run_command, checkpoint=crash)
                self.running = True
                with self.assertRaises(PowerLoss):
                    engine.execute(archive=self.archive, sha256=u.digest(self.archive))
                self.assertTrue(engine.journal.exists())
                engine.checkpoint = lambda unused: None
                engine.execute('recover')
                build = json.loads((temporary / 'BUILD.json').read_text())
                self.assertEqual(build['version'], 'v0.2.0' if phase in ('committed', 'backup-published', 'work-cleared') else 'v0.1.1')
                self.assertTrue(self.running)
                self.assertFalse(engine.journal.exists())

    def test_power_loss_retiring_previous_backup_can_finish(self):
        self.upgrade()
        self.make_release(self.parent / 'third', 'v0.3.0')
        third = self.parent / 'third.tar.gz'
        with tarfile.open(third, 'w:gz') as stream:
            stream.add(self.parent / 'third', arcname='clash-linux')
        def crash(phase):
            if phase == 'previous-retired':
                raise PowerLoss()
        self.engine.checkpoint = crash
        with self.assertRaises(PowerLoss):
            self.engine.execute(archive=third, sha256=u.digest(third))
        self.assertEqual(self.installed_version(), 'v0.3.0')
        self.engine.checkpoint = lambda phase: None
        self.engine.execute('recover')
        self.assertEqual(u.read_json(self.engine.previous / 'BUILD.json')['version'], 'v0.2.0')
        self.assertFalse(self.engine.journal.exists())

    def test_public_recovery_refuses_busy_legacy_state_lock(self):
        def crash(phase):
            if phase == 'old-renamed':
                raise PowerLoss()
        self.engine.checkpoint = crash
        with self.assertRaises(PowerLoss):
            self.upgrade()
        record = u.read_json(self.engine.journal)
        legacy_lock = Path(record['work']) / 'old/runtime/mvp/lock'
        self.engine.checkpoint = lambda phase: None
        with open(legacy_lock, 'a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            with self.assertRaisesRegex(u.UpgradeError, '另一个状态操作'):
                self.engine.execute('recover')
            self.assertTrue(self.engine.journal.exists())
            self.assertFalse(self.root.exists())
        self.engine.execute('recover')
        self.assertEqual(self.installed_version(), 'v0.1.1')

    def test_automatic_recovery_refuses_busy_current_state_lock(self):
        def crash(phase):
            if phase == 'prepared':
                raise PowerLoss()
        self.engine.checkpoint = crash
        with self.assertRaises(PowerLoss):
            self.upgrade()
        self.engine.checkpoint = lambda phase: None
        with open(self.root / 'runtime/mvp/lock', 'a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            with self.assertRaisesRegex(u.UpgradeError, '另一个状态操作'):
                self.upgrade()
        self.engine.execute('recover')
        self.assertFalse(self.engine.journal.exists())

    def test_bad_checksum_does_not_stop_core(self):
        self.running = True
        with self.assertRaises(u.UpgradeError):
            self.engine.execute(archive=self.archive, sha256='0' * 64)
        self.assertNotIn('stop', self.calls)
        self.assertEqual(self.installed_version(), 'v0.1.1')

    def test_user_path_collision_is_rejected(self):
        self.put(self.root, 'NEW', 'user content')
        self.put(self.source, 'NEW', 'new app file')
        build = u.read_json(self.source / 'BUILD.json')
        build['files'].append('NEW')
        u.write_json(self.source / 'BUILD.json', build)
        self.pack()
        with self.assertRaisesRegex(u.UpgradeError, '冲突'):
            self.upgrade()
        self.assertEqual((self.root / 'NEW').read_text(), 'user content')

    def test_symlink_user_data_refused(self):
        (self.root / 'conf/external').symlink_to(self.parent)
        with self.assertRaises(u.UpgradeError):
            self.upgrade()
        self.assertFalse(self.calls)

    def test_missing_marker_refused(self):
        (self.root / '.clash-install.json').unlink()
        with self.assertRaises(u.UpgradeError):
            self.upgrade()
        self.assertFalse(self.calls)

    def test_wrong_arch_and_data_format_refused(self):
        for change in ({'architecture': 'arm64' if ARCH == 'amd64' else 'amd64'}, {'data_format': 2},
                       {'min_upgrade_version': 'v0.1.2'}):
            with self.subTest(change=change):
                build = u.read_json(self.source / 'BUILD.json')
                build.update({'architecture': ARCH, 'data_format': 1, 'min_upgrade_version': 'v0.1.0'})
                build.update(change)
                u.write_json(self.source / 'BUILD.json', build)
                self.pack()
                with self.assertRaises(u.UpgradeError):
                    self.upgrade()
                self.assertNotIn('stop', self.calls)

    def test_state_lock_busy_refused(self):
        path = self.root / 'runtime/mvp/lock'
        with open(path, 'w') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            with self.assertRaises(u.UpgradeError):
                self.upgrade()
        self.assertFalse(self.calls)

    def test_pending_configuration_transaction_refused(self):
        self.put(self.root, 'runtime/mvp/transaction.json', '{}')
        with self.assertRaises(u.UpgradeError):
            self.upgrade()
        self.assertFalse(self.calls)

    def test_tar_traversal_and_links_rejected(self):
        for name, kind in [('clash-linux/../escape', tarfile.REGTYPE),
                           ('clash-linux/link', tarfile.SYMTYPE)]:
            with self.subTest(name=name):
                with tarfile.open(self.archive, 'w:gz') as tf:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = '/tmp'
                    tf.addfile(member)
                with self.assertRaises(u.UpgradeError):
                    self.upgrade()
                self.assertFalse(self.calls)

    def test_check_does_not_download_or_stop_core(self):
        with patch.object(u, 'latest', return_value={'version': 'v0.2.0'}), patch.object(u, 'download') as download:
            result = self.engine.execute(check=True)
        self.assertIn('可升级', result)
        download.assert_not_called()
        self.assertFalse(self.calls)

    def test_native_runtime_change_requires_independent_bootstrap(self):
        current = u.read_json(self.root / 'BUILD.json')
        current['python'] = '3.11.0'
        u.write_json(self.root / 'BUILD.json', current)
        proposed = u.read_json(self.source / 'BUILD.json')
        proposed['python'] = '3.12.0'
        u.write_json(self.source / 'BUILD.json', proposed)
        self.pack()
        with patch.object(u.sys, 'executable', str(self.root / 'python/bin/python3')):
            with self.assertRaisesRegex(u.UpgradeError, '独立'):
                self.upgrade()
        self.assertNotIn('stop', self.calls)
        self.assertEqual(self.installed_version(), 'v0.1.1')

    def test_runtime_daemon_never_inherits_upgrade_lock(self):
        calls = []
        def runner(argv, **kwargs):
            calls.append(kwargs)
            return subprocess.CompletedProcess(argv, 0)
        engine = u.Upgrader(self.root, runner=runner)
        with u.operation_lock(self.root, exclusive=True):
            engine.runtime('start')
            engine.run(['bash', self.root / 'clash', 'help'])
        self.assertEqual(calls[0]['pass_fds'], ())
        self.assertNotIn('CLASH_OPERATION_LOCK_FD', calls[0]['env'])
        self.assertTrue(calls[1]['pass_fds'])

    def test_runtime_uses_saved_controller_and_secret_not_foreign_environment(self):
        self.put(self.root, 'conf/config.yaml', json.dumps({
            'external-controller': '127.0.0.1:19999', 'secret': 'saved secret'}))
        with patch.dict(os.environ, {'MIHOMO_API_URL': 'http://foreign:9000',
                                      'MIHOMO_BINARY': '/unrelated/core'}):
            environment = self.engine.runtime_environment()
        self.assertEqual(environment['MIHOMO_API_URL'], 'http://127.0.0.1:19999')
        self.assertEqual(environment['MIHOMO_API_SECRET'], 'saved secret')
        self.assertEqual(environment['MIHOMO_BINARY'], str(self.root / 'bin/mihomo'))

    def test_empty_user_directory_collision_is_refused(self):
        (self.root / 'NEW').mkdir()
        self.put(self.source, 'NEW', 'program')
        build = u.read_json(self.source / 'BUILD.json')
        build['files'].append('NEW')
        u.write_json(self.source / 'BUILD.json', build)
        self.pack()
        with self.assertRaisesRegex(u.UpgradeError, '冲突'):
            self.upgrade()
        self.assertTrue((self.root / 'NEW').is_dir())

    def test_check_can_run_while_legacy_state_mutation_lock_is_held(self):
        with open(self.root / 'runtime/mvp/lock', 'w') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            self.assertIn('可升级', self.engine.execute(check=True, version_name='v0.2.0'))

    def test_invalid_metadata_falls_back_to_github_latest(self):
        responses = [json.dumps({'format': 1, 'version': 'v0.2.0', 'assets': []}).encode(),
                     json.dumps({'tag_name': 'v0.2.0'}).encode()]
        with patch.object(u, 'fetch', side_effect=responses):
            self.assertEqual(u.latest(u.SERVER, u.GITHUB)['version'], 'v0.2.0')

    def test_fallback_download_is_fixed_to_requested_version(self):
        checksum = u.digest(self.archive)
        urls = []
        def fetch(url, destination=None, limit=1024*1024):
            urls.append(url)
            if url.startswith('https://primary'):
                raise OSError('unavailable')
            if url.endswith('SHA256SUMS'):
                return (checksum + '  clash-linux-' + ARCH + '.tar.gz\n').encode()
            destination.write_bytes(self.archive.read_bytes())
        with patch.object(u, 'fetch', side_effect=fetch):
            file, digest = u.download('v0.2.0', ARCH, self.parent, 'https://primary', 'https://secondary')
        self.assertEqual(digest, checksum)
        self.assertTrue(all('/v0.2.0/' in url for url in urls))


if __name__ == '__main__':
    unittest.main()
