#!/usr/bin/env python3
"""Offline end-to-end upgrade acceptance using disposable homes and tiny bundles.

The installer, launcher, upgrade engine, menu and runtime commands are real.
Only Python packaging, Linux platform probes and the Mihomo executable are
fixtures, so this suite also runs on a developer's macOS workstation. Real
Linux/Mihomo binary acceptance belongs to full_bundle_test.py.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class UpgradeIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory(prefix='clash-upgrade-fixtures-')
        cls.base = Path(cls.fixture.name).resolve()
        cls.arch = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}[platform.machine()]
        cls.tools = cls.base / 'tools'
        cls.tools.mkdir()
        probes = {
            'uname': '#!/bin/sh\ncase "$1" in -s) echo Linux;; -m) echo ' + platform.machine() + ';; *) exit 1;; esac\n',
            'getconf': '#!/bin/sh\necho "glibc 2.35"\n',
            'python': '#!/bin/sh\necho SYSTEM_PYTHON_USED >&2\nexit 97\n',
            'python3': '#!/bin/sh\necho SYSTEM_PYTHON_USED >&2\nexit 97\n',
            'curl': '#!/bin/sh\necho UNEXPECTED_NETWORK >&2\nexit 98\n',
        }
        for name, body in probes.items():
            path = cls.tools / name
            path.write_text(body)
            path.chmod(0o755)
        cls.packages = {}
        for version in ('v0.1.1', 'v0.2.0', 'v0.2.1'):
            package = cls.base / version / 'clash-linux'
            package.mkdir(parents=True)
            for path in [ROOT / 'clash', ROOT / 'LICENSE'] + list((ROOT / 'scripts').glob('*.py')) + list((ROOT / 'scripts').glob('*.sh')):
                relative = path.relative_to(ROOT)
                (package / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, package / relative)
            # A legacy bundle intentionally has no upgrade command or engine.
            if version == 'v0.1.1':
                (package / 'clash').write_text('#!/usr/bin/env bash\nset -eu\n'
                    'root=$(cd -- "$(dirname -- "$0")" && pwd)\n'
                    'if [[ "${1:-}" == shell-init ]]; then shift; exec "$root/scripts/shell_integration.sh" install "$@"; fi\n'
                    'echo "legacy clash: unknown command" >&2\nexit 1\n')
                (package / 'scripts/upgrade.py').unlink(missing_ok=True)
            (package / 'python/bin').mkdir(parents=True)
            (package / 'python/bin/python3').write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
            (package / 'bin').mkdir()
            (package / 'bin/mihomo').write_text('#!/bin/sh\ncase "$1" in -v) echo "Mihomo fixture";; -t) exit 0;; *) exit 1;; esac\n')
            (package / 'release-note.txt').write_text(version + '\n')
            for path in [package / 'clash', package / 'python/bin/python3', package / 'bin/mihomo']:
                path.chmod(0o755)
            files = sorted(str(path.relative_to(package)) for path in package.rglob('*') if path.is_file())
            (package / 'BUILD.json').write_text(json.dumps({'version': version, 'architecture': cls.arch,
                'python': 'fixture', 'data_format': 1, 'min_upgrade_version': 'v0.1.0', 'files': files}))
            archive = cls.base / (version + '.tar.gz')
            with tarfile.open(archive, 'w:gz') as stream:
                stream.add(package, arcname='clash-linux')
            cls.packages[version] = (archive, hashlib.sha256(archive.read_bytes()).hexdigest())

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='clash-upgrade-home-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.root = self.home / '.local/share/clash-linux'
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(('CLASH_', 'MIHOMO_')) and key.lower() not in
                    ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}
        self.env.update(HOME=str(self.home), SHELL='/bin/bash', PATH=str(self.tools) + ':' + os.environ['PATH'],
                        PYTHONHOME='/poison-system-python-home', PYTHONPATH='/poison-system-python-path', TERM='dumb')

    def command(self, argv, success=True, **kwargs):
        result = subprocess.run([str(arg) for arg in argv], env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, **kwargs)
        output = result.stdout + result.stderr
        if success:
            self.assertEqual(result.returncode, 0, output)
        else:
            self.assertNotEqual(result.returncode, 0, output)
        self.assertNotIn('SYSTEM_PYTHON_USED', output)
        self.assertNotIn('UNEXPECTED_NETWORK', output)
        return result

    def install(self, version='v0.1.1', upgrade=False, **kwargs):
        archive, digest = self.packages[version]
        args = ['bash', ROOT / 'install.sh', '--prefix', self.root, '--version', version,
                '--archive', archive, '--sha256', digest, '--no-shell']
        if upgrade:
            args.append('--upgrade')
        return self.command(args, **kwargs)

    def upgrade(self, version='v0.2.1', **kwargs):
        archive, digest = self.packages[version]
        return self.command([self.root / 'clash', 'upgrade', '--version', version,
                             '--archive', archive, '--sha256', digest], **kwargs)

    def seed_data(self):
        state = {'version': 1, 'mode': 'global', 'secret': 'fixture-secret', 'active_id': 'fixture',
                 'subscriptions': [{'id': 'fixture', 'name': 'My subscription',
                    'url': 'https://subscription.invalid/?token=private', 'selected': 'Tokyo 02',
                    'nodes': ['Tokyo 01', 'Tokyo 02'], 'cache': 'fixture.yaml'}]}
        self.data = {'runtime/mvp/state.json': json.dumps(state).encode(),
                     'conf/config.yaml': b'{"secret":"fixture-secret","rules":["MATCH,DIRECT"]}',
                     'conf/mvp-providers/fixture.yaml': b'proxies: []\n',
                     '.env': b'# legacy settings must be preserved\n',
                     'notes/custom.txt': b'personal notes\n',
                     'scripts/my-hook.sh': b'# user hook outside the program manifest\n',
                     'logs/mihomo.log': b'previous log\n'}
        for relative, contents in self.data.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
            path.chmod(0o600)
        self.directory_modes = {'.': 0o700, 'conf': 0o700, 'conf/mvp-providers': 0o700,
                                'runtime': 0o700, 'runtime/mvp': 0o700, 'notes': 0o750}
        for relative, mode in self.directory_modes.items():
            (self.root / relative).chmod(mode)

    def assert_preserved(self):
        for relative, contents in self.data.items():
            self.assertEqual((self.root / relative).read_bytes(), contents, relative)
            self.assertEqual((self.root / relative).stat().st_mode & 0o777, 0o600, relative)

        for relative, mode in self.directory_modes.items():
            self.assertEqual((self.root / relative).stat().st_mode & 0o777, mode, relative)

    def assert_version(self, version):
        self.assertEqual(json.loads((self.root / 'BUILD.json').read_text())['version'], version)
        self.assertEqual(json.loads((self.root / '.clash-install.json').read_text())['version'], version)

    def test_legacy_installer_bootstrap_preserves_user_data_and_stopped_core(self):
        self.install()
        self.seed_data()
        self.command([self.root / 'clash', 'shell-init', '--shell', 'bash'])
        shell_files = {p: p.read_bytes() for p in (self.home / '.bashrc', self.home / '.local/bin/clash', self.root / 'env.sh')}
        self.command([self.root / 'clash', 'upgrade', '--check'], success=False)
        self.install('v0.2.0', upgrade=True)
        self.assert_version('v0.2.0')
        self.assert_preserved()
        self.assertFalse((self.root / 'runtime/mihomo.pid').exists())
        status = self.command([self.root / 'clash', 'status']).stdout
        self.assertIn('Tokyo 02', status)
        self.assertIn('global', status)
        self.assertIn('v0.2.0', self.command([self.root / 'clash', 'app-version']).stdout)
        for path, contents in shell_files.items():
            self.assertEqual(path.read_bytes(), contents, str(path))
        self.command([self.home / '.local/bin/clash', 'status'])

    def test_installed_upgrade_and_rollback_keep_new_user_changes(self):
        self.install('v0.2.0')
        self.seed_data()
        self.upgrade()
        self.assert_version('v0.2.1')
        self.assert_preserved()
        self.data['notes/custom.txt'] = b'edited after upgrading\n'
        self.data['notes/new.txt'] = b'created after upgrading\n'
        state = json.loads(self.data['runtime/mvp/state.json'])
        state['mode'] = 'direct'
        self.data['runtime/mvp/state.json'] = json.dumps(state).encode()
        for relative, contents in self.data.items():
            (self.root / relative).write_bytes(contents)
            (self.root / relative).chmod(0o600)
        self.command([self.root / 'clash', 'rollback'])
        self.assert_version('v0.2.0')
        self.assert_preserved()

    def test_same_version_skips_without_replacing_local_program_edits(self):
        self.install('v0.2.0')
        self.seed_data()
        local_program = self.root / 'release-note.txt'
        local_program.write_text('local edit\n')
        self.upgrade('v0.2.0')
        self.assertEqual(local_program.read_text(), 'local edit\n')
        self.assert_preserved()

    def test_same_version_offline_package_without_version_is_noop(self):
        self.install('v0.2.0')
        self.seed_data()
        archive, checksum = self.packages['v0.2.0']
        self.command([self.root / 'clash', 'upgrade', '--archive', archive, '--sha256', checksum])
        self.assert_version('v0.2.0')
        self.assert_preserved()

    def test_broken_candidate_cli_rolls_back_before_returning_failure(self):
        self.install('v0.2.0')
        self.seed_data()
        candidate = self.home / 'bad-bundle/clash-linux'
        shutil.copytree(self.base / 'v0.2.1/clash-linux', candidate)
        (candidate / 'scripts/terminal_mvp.py').write_text('def broken syntax\n')
        archive = self.home / 'bad.tar.gz'
        with tarfile.open(archive, 'w:gz') as stream:
            stream.add(candidate, arcname='clash-linux')
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.command([self.root / 'clash', 'upgrade', '--archive', archive, '--sha256', checksum], success=False)
        self.assert_recovered('v0.2.0')

    def test_failed_new_core_config_validation_automatically_restores_old_version(self):
        self.install('v0.2.0')
        self.seed_data()
        candidate = self.home / 'bad-core/clash-linux'
        shutil.copytree(self.base / 'v0.2.1/clash-linux', candidate)
        (candidate / 'bin/mihomo').write_text('#!/bin/sh\ncase "$1" in -v) echo "Mihomo fixture";; *) exit 42;; esac\n')
        archive = self.home / 'bad-core.tar.gz'
        with tarfile.open(archive, 'w:gz') as stream:
            stream.add(candidate, arcname='clash-linux')
        checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
        self.command([self.root / 'clash', 'upgrade', '--archive', archive, '--sha256', checksum], success=False)
        self.assert_recovered('v0.2.0')

    def test_bad_checksum_leaves_program_and_data_untouched(self):
        self.install('v0.2.0')
        self.seed_data()
        archive, _ = self.packages['v0.2.1']
        result = self.command([self.root / 'clash', 'upgrade', '--version', 'v0.2.1',
                               '--archive', archive, '--sha256', '0' * 64], success=False)
        self.assertIn('SHA', result.stdout + result.stderr)
        self.assert_version('v0.2.0')
        self.assert_preserved()

    def test_user_file_conflicting_with_new_manifest_rejects_upgrade(self):
        self.install()
        self.seed_data()
        (self.root / 'scripts/upgrade.py').write_text('personal code, not managed\n')
        self.install('v0.2.0', upgrade=True, success=False)
        self.assert_version('v0.1.1')
        self.assertEqual((self.root / 'scripts/upgrade.py').read_text(), 'personal code, not managed\n')
        self.assert_preserved()

    def crash_upgrade(self, phase):
        archive, checksum = self.packages['v0.2.1']
        code = ("import os, sys; sys.path.insert(0, sys.argv[1]); from upgrade import Upgrader; "
                "Upgrader(sys.argv[2], checkpoint=lambda phase: os._exit(73) if phase == sys.argv[5] else None)"
                ".execute(version_name='v0.2.1', archive=sys.argv[3], sha256=sys.argv[4])")
        result = self.command([self.root / 'python/bin/python3', '-E', '-s', '-B', '-c', code,
                               self.root / 'scripts', self.root, archive, checksum, phase], success=False)
        self.assertEqual(result.returncode, 73, result.stderr)
        self.assertTrue((self.root.parent / '.clash-linux.upgrade.json').is_file())

    def assert_recovered(self, version):
        self.assert_version(version)
        self.assert_preserved()
        self.assertFalse((self.root.parent / '.clash-linux.upgrade.json').exists())
        self.assertEqual(list(self.root.parent.glob('.clash-linux.upgrade-work-*')), [])
        self.command([self.root / 'clash', 'status'])

    def test_crash_between_renames_restores_via_installer_recovery(self):
        self.install('v0.2.0')
        self.seed_data()
        self.crash_upgrade('old-renamed')
        self.assertFalse(self.root.exists())
        stale = self.root.parent / '.clash-linux.install-lock'
        stale.mkdir()
        (stale / 'owner.pid').write_text('99999999\n')
        archive, checksum = self.packages['v0.2.1']
        self.command(['bash', ROOT / 'install.sh', '--recover', '--prefix', self.root,
                      '--version', 'v0.2.1', '--archive', archive, '--sha256', checksum, '--no-shell'])
        self.assert_recovered('v0.2.0')
        self.assertFalse(stale.exists())

    def test_live_installer_pid_lock_is_not_reclaimed(self):
        self.install('v0.2.0')
        self.seed_data()
        lock = self.root.parent / '.clash-linux.install-lock'
        lock.mkdir()
        owner = str(os.getpid()) + '\n'
        (lock / 'owner.pid').write_text(owner)
        archive, checksum = self.packages['v0.2.1']
        self.command(['bash', ROOT / 'install.sh', '--recover', '--prefix', self.root,
                      '--version', 'v0.2.1', '--archive', archive, '--sha256', checksum, '--no-shell'], success=False)
        self.assertEqual((lock / 'owner.pid').read_text(), owner)
        self.assert_version('v0.2.0')
        self.assert_preserved()

    def test_crash_after_new_rename_blocks_normal_cli_until_recovered(self):
        self.install('v0.2.0')
        self.seed_data()
        self.crash_upgrade('new-renamed')
        self.assert_version('v0.2.1')
        self.command([self.root / 'clash', 'status'], success=False)
        self.command([self.root / 'clash', 'recover'])
        self.assert_recovered('v0.2.0')

    def test_crash_after_commit_finishes_new_version_and_retains_rollback(self):
        self.install('v0.2.0')
        self.seed_data()
        self.crash_upgrade('committed')
        self.command([self.root / 'clash', 'recover'])
        self.assert_recovered('v0.2.1')
        self.command([self.root / 'clash', 'rollback'])
        self.assert_recovered('v0.2.0')

    def test_crash_replacing_previous_backup_preserves_the_correct_rollback(self):
        self.install('v0.1.1')
        self.seed_data()
        self.install('v0.2.0', upgrade=True)
        self.crash_upgrade('previous-retired')
        self.command([self.root / 'clash', 'recover'])
        self.assert_recovered('v0.2.1')
        self.command([self.root / 'clash', 'rollback'])
        self.assert_recovered('v0.2.0')

    def test_crash_after_staging_cleanup_keeps_recovery_record_until_safe(self):
        self.install('v0.2.0')
        self.seed_data()
        self.crash_upgrade('work-cleared')
        journal = json.loads((self.root.parent / '.clash-linux.upgrade.json').read_text())
        self.assertEqual(list(Path(journal['work']).iterdir()), [])
        self.command([self.root / 'clash', 'recover'])
        self.assert_recovered('v0.2.1')
        self.command([self.root / 'clash', 'rollback'])
        self.assert_recovered('v0.2.0')

    def test_purge_removes_private_data_from_previous_version_too(self):
        self.install('v0.2.0')
        self.seed_data()
        self.upgrade()
        previous = self.root.parent / '.clash-linux.previous'
        self.assertTrue((previous / 'runtime/mvp/state.json').exists())
        self.command([self.root / 'clash', 'uninstall', '--purge'])
        self.assertFalse(previous.exists())
        self.assertFalse((self.root / 'runtime/mvp').exists())
        self.assertFalse((self.root / 'conf').exists())
        self.assertEqual((self.root / 'notes/custom.txt').read_bytes(), self.data['notes/custom.txt'])

    def test_user_symlink_rejects_upgrade_without_touching_external_target(self):
        self.install('v0.2.0')
        self.seed_data()
        outside = self.home / 'external.txt'
        outside.write_bytes(b'external user data\n')
        link = self.root / 'notes/external-link'
        link.symlink_to(outside)
        self.upgrade(success=False)
        self.assertTrue(link.is_symlink())
        self.assertEqual(outside.read_bytes(), b'external user data\n')
        self.assert_version('v0.2.0')
        self.assert_preserved()

    def test_exclusive_operation_lock_rejects_cli_and_bootstrap(self):
        self.install('v0.2.0')
        self.seed_data()
        lock_path = self.root.parent / ('.' + self.root.name + '.operation.lock')
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.upgrade(success=False)
            self.install('v0.2.1', upgrade=True, success=False)
        self.assert_version('v0.2.0')
        self.assert_preserved()

    def test_active_plain_menu_blocks_upgrade_until_exit(self):
        self.install('v0.2.0')
        self.seed_data()
        menu = subprocess.Popen([str(self.root / 'clash'), 'menu', '--plain'], env=self.env,
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            lock_path = self.root.parent / ('.' + self.root.name + '.operation.lock')
            deadline = time.monotonic() + 5
            locked = False
            while time.monotonic() < deadline and menu.poll() is None:
                if lock_path.exists():
                    with lock_path.open('a') as probe:
                        try:
                            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            locked = True
                            break
                time.sleep(0.02)
            self.assertTrue(locked, 'menu did not hold a stable operation lock')
            self.upgrade(success=False)
            self.assert_version('v0.2.0')
        finally:
            menu.communicate('q\n', timeout=5)
        self.upgrade()
        self.assert_version('v0.2.1')
        self.assert_preserved()


if __name__ == '__main__':
    unittest.main()
