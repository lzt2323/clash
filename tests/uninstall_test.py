#!/usr/bin/env python3
"""Offline uninstall safety tests; no real shell profile or core is touched."""
import fcntl
import importlib.util
import json
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('clash_uninstall', ROOT / 'scripts/uninstall.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='clash-uninstall-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve() / 'install'
        self.root.mkdir()
        self.files = ['BUILD.json', 'clash', 'scripts/uninstall.py',
                      'scripts/shell_integration.sh', 'scripts/mihomo_runtime.sh',
                      'python/bin/python3', 'bin/mihomo']
        for name in self.files:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('program')
        self.marker = {'format': 1, 'version': 'test-v1', 'prefix': str(self.root)}
        self.write('.clash-install.json', self.marker)
        self.build = {'version': 'test-v1', 'files': self.files}
        self.write('BUILD.json', self.build)
        for name in ('conf/config.yaml', 'conf/providers/provider.yaml', 'runtime/mvp/state.json'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('private fixture data')
        self.root.joinpath('env.sh').write_text('generated environment')
        self.calls = []
        self.stop_fail = False
        self.shell_fail = False
        self.shim = Path(self.temporary.name) / 'unrelated-clash'
        self.shim.write_text('another installation')
        owner = self
        class FakeManager:
            def __init__(self, root):
                owner.calls.append(('manager', str(root)))
                self.runtime = 'not-the-runtime'
            def _runtime(self, action):
                owner.calls.append(('runtime', action, self.runtime,
                                    os.environ.get('CLASH_RUNTIME_SCRIPT'),
                                    os.environ.get('MIHOMO_BINARY')))
                if owner.stop_fail:
                    raise RuntimeError('https://example.invalid/?secret=HIDDEN')
                return subprocess.CompletedProcess([], 0)
        self.manager = FakeManager

    def write(self, name, data):
        self.root.joinpath(name).write_text(json.dumps(data))

    def shell(self, argv, **kwargs):
        self.calls.append(('shell', argv[-1], argv[1]))
        return subprocess.CompletedProcess(argv, int(self.shell_fail))

    def uninstall(self, purge=False):
        return module.uninstall(self.root, purge=purge, manager_factory=self.manager,
                                shell_runner=self.shell)

    def assert_program_present(self):
        self.assertTrue((self.root / 'clash').is_file())
        self.assertTrue((self.root / '.clash-install.json').is_file())
        self.assertTrue((self.root / 'python/bin/python3').is_file())

    def test_missing_marker_is_refused_without_deletion(self):
        (self.root / '.clash-install.json').unlink()
        with self.assertRaises(module.UninstallError):
            self.uninstall()
        self.assertTrue((self.root / 'clash').exists())
        self.assertFalse(self.calls)

    def test_purge_removes_valid_rollback_backup(self):
        backup = self.root.parent / ('.' + self.root.name + '.previous')
        shutil.copytree(self.root, backup)
        self.uninstall(purge=True)
        self.assertFalse(backup.exists())

    def test_default_preserves_rollback_backup_and_explains_it(self):
        backup = self.root.parent / ('.' + self.root.name + '.previous')
        shutil.copytree(self.root, backup)
        messages = self.uninstall()
        self.assertTrue(backup.exists())
        self.assertTrue(any('回滚备份' in message for message in messages))

    def test_purge_refuses_foreign_backup_before_removing_install(self):
        backup = self.root.parent / ('.' + self.root.name + '.previous')
        shutil.copytree(self.root, backup)
        marker = json.loads((backup / '.clash-install.json').read_text())
        marker['prefix'] = '/some/other/installation'
        (backup / '.clash-install.json').write_text(json.dumps(marker))
        with self.assertRaises(module.UninstallError):
            self.uninstall(purge=True)
        self.assertTrue((self.root / 'clash').exists())
        self.assertTrue(backup.exists())

    def test_purge_refuses_symlink_backup_without_following(self):
        backup = self.root.parent / ('.' + self.root.name + '.previous')
        external = self.root.parent / 'external'
        external.mkdir()
        (external / 'secret').write_text('kept')
        backup.symlink_to(external, target_is_directory=True)
        with self.assertRaises(module.UninstallError):
            self.uninstall(purge=True)
        self.assertTrue((self.root / 'clash').exists())
        self.assertEqual((external / 'secret').read_text(), 'kept')

    def test_build_manifest_without_self_entry_is_accepted(self):
        self.write('BUILD.json', dict(self.build, files=[name for name in self.files
                                                      if name != 'BUILD.json']))
        self.uninstall()
        self.assertFalse((self.root / 'BUILD.json').exists())
        self.assertFalse((self.root / 'clash').exists())

    def test_wrong_prefix_and_marker_format_are_refused(self):
        for change in ({'prefix': '/'}, {'prefix': str(self.root / '../install')},
                       {'prefix': str(self.root.parent)}, {'format': True}):
            with self.subTest(change=change):
                self.write('.clash-install.json', dict(self.marker, **change))
                with self.assertRaises(module.UninstallError):
                    self.uninstall()
                self.assert_program_present()
                self.assertFalse(self.calls)

    def test_root_home_and_symlink_installation_are_refused(self):
        link = self.root.parent / 'linked-install'
        link.symlink_to(self.root, target_is_directory=True)
        for root in (Path('/'), Path.home(), link):
            with self.subTest(root=root):
                with self.assertRaises(module.UninstallError):
                    module.uninstall(root, manager_factory=self.manager, shell_runner=self.shell)
        self.assert_program_present()

    def test_stop_failure_prevents_shell_cleanup_and_deletion(self):
        (self.root / 'runtime/mihomo.pid').write_text('12345\n')
        self.stop_fail = True
        with self.assertRaises(module.UninstallError) as error:
            self.uninstall()
        self.assertNotIn('HIDDEN', str(error.exception))
        self.assert_program_present()
        self.assertFalse([call for call in self.calls if call[0] == 'shell'])
        self.assertFalse((self.root / '.clash-data.json').exists())

    def test_stop_uses_only_owned_runtime_and_ignores_external_overrides(self):
        from unittest.mock import patch
        (self.root / 'runtime/mihomo.pid').write_text('12345\n')
        with patch.dict(os.environ, {'CLASH_RUNTIME_SCRIPT': '/unrelated/runtime',
                                    'MIHOMO_BINARY': '/unrelated/core'}):
            self.uninstall()
            self.assertEqual(os.environ['MIHOMO_BINARY'], '/unrelated/core')
        call = next(call for call in self.calls if call[0] == 'runtime')
        self.assertEqual(call[1:3], ('stop', str(self.root / 'scripts/mihomo_runtime.sh')))
        self.assertIsNone(call[3])
        self.assertIsNone(call[4])

    def test_default_preserves_data_and_unknown_files_without_touching_other_shim(self):
        self.root.joinpath('python/user-note.txt').write_text('retain')
        self.root.joinpath('notes.txt').write_text('retain')
        messages = self.uninstall()
        self.assertFalse((self.root / 'clash').exists())
        self.assertFalse((self.root / 'python/bin/python3').exists())
        self.assertFalse((self.root / 'env.sh').exists())
        self.assertFalse((self.root / '.clash-install.json').exists())
        self.assertEqual((self.root / 'conf/config.yaml').read_text(), 'private fixture data')
        self.assertTrue((self.root / 'runtime/mvp/state.json').exists())
        self.assertTrue((self.root / 'python/user-note.txt').exists())
        self.assertTrue((self.root / 'notes.txt').exists())
        self.assertEqual(self.shim.read_text(), 'another installation')
        self.assertEqual(json.loads((self.root / '.clash-data.json').read_text()), self.marker)
        self.assertIn('已保留', messages[-1])
        self.assertFalse([call for call in self.calls if call[0] == 'runtime'])
        self.assertEqual([call[1] for call in self.calls if call[0] == 'shell'], ['bash', 'zsh'])

    def test_purge_removes_data_and_keeps_unknown_user_files(self):
        self.root.joinpath('notes.txt').write_text('retain')
        self.write('.clash-data.json', self.marker)
        self.uninstall(purge=True)
        self.assertFalse((self.root / 'conf').exists())
        self.assertFalse((self.root / 'runtime/mvp').exists())
        self.assertFalse((self.root / '.clash-data.json').exists())
        self.assertEqual((self.root / 'notes.txt').read_text(), 'retain')
        self.assertTrue(self.root.exists())

    def test_external_symlink_file_is_unlinked_without_following_it(self):
        outside = Path(self.temporary.name) / 'external-file'
        outside.write_text('never remove')
        binary = self.root / 'python/bin/python3'
        binary.unlink()
        binary.symlink_to(outside)
        self.uninstall()
        self.assertEqual(outside.read_text(), 'never remove')
        self.assertFalse(os.path.lexists(binary))

    def test_external_data_symlink_is_not_followed_during_purge(self):
        outside = Path(self.temporary.name) / 'external-data'
        outside.mkdir()
        outside.joinpath('preserve.txt').write_text('never remove')
        self.root.joinpath('conf/external-link').symlink_to(outside, target_is_directory=True)
        self.uninstall(purge=True)
        self.assertEqual(outside.joinpath('preserve.txt').read_text(), 'never remove')

    def test_symlink_ancestor_in_manifest_is_refused_before_any_mutation(self):
        binary_directory = self.root / 'bin'
        binary_directory.joinpath('mihomo').unlink()
        binary_directory.rmdir()
        outside = Path(self.temporary.name) / 'external-bin'
        outside.mkdir()
        outside.joinpath('mihomo').write_text('never remove')
        binary_directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(module.UninstallError):
            self.uninstall()
        self.assertFalse(self.calls)
        self.assertEqual(outside.joinpath('mihomo').read_text(), 'never remove')
        self.assertTrue((self.root / 'clash').exists())

    def test_busy_lock_is_refused_without_stopping_or_deleting(self):
        with open(self.root / 'runtime/mvp/lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(module.UninstallError):
                self.uninstall()
        self.assertFalse(self.calls)
        self.assert_program_present()

    def test_pending_transaction_is_refused_without_stopping_or_deleting(self):
        self.root.joinpath('runtime/mvp/transaction.json').write_text('pending transaction')
        with self.assertRaises(module.UninstallError):
            self.uninstall()
        self.assertFalse(self.calls)
        self.assert_program_present()

    def test_manifest_traversal_and_directory_entries_are_refused(self):
        for entry in ('../external', '/tmp/external', 'scripts/../notes.txt', 'python'):
            with self.subTest(entry=entry):
                self.write('BUILD.json', dict(self.build, files=self.files + [entry]))
                with self.assertRaises(module.UninstallError):
                    self.uninstall()
                self.assert_program_present()
                self.assertFalse(self.calls)

    def test_shell_failure_keeps_application_files(self):
        self.shell_fail = True
        with self.assertRaises(module.UninstallError):
            self.uninstall()
        self.assert_program_present()

    def test_real_shell_cleanup_preserves_other_installation_loader_and_shim(self):
        from unittest.mock import patch
        home = Path(self.temporary.name) / 'fake-home'
        home.mkdir()
        other = Path(self.temporary.name) / 'other-install'
        other.joinpath('scripts').mkdir(parents=True)
        source = ROOT.joinpath('scripts/shell_integration.sh').read_text()
        self.root.joinpath('scripts/shell_integration.sh').write_text(source)
        other.joinpath('scripts/shell_integration.sh').write_text(source)
        other.joinpath('clash').write_text('#!/bin/sh\nexit 0\n')
        other.joinpath('clash').chmod(0o755)
        self.root.joinpath('clash').chmod(0o755)
        with patch.dict(os.environ, {'HOME': str(home)}):
            for prefix in (self.root, other):
                if prefix == other:
                    # Install refuses to overwrite an unrelated shim. Simulate
                    # an explicitly replaced command entry, keeping both loaders.
                    home.joinpath('.local/bin/clash').unlink()
                result = subprocess.run(['bash', str(prefix / 'scripts/shell_integration.sh'),
                                         'install', '--shell', 'bash'],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            rc = home / '.bashrc'
            self.assertIn(str(self.root), rc.read_text())
            self.assertIn(str(other), rc.read_text())
            module.uninstall(self.root, manager_factory=self.manager)
        self.assertNotIn(str(self.root), rc.read_text())
        self.assertIn(str(other), rc.read_text())
        self.assertIn(str(other), home.joinpath('.local/bin/clash').read_text())


if __name__ == '__main__':
    unittest.main(verbosity=2)
