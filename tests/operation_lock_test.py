#!/usr/bin/env python3
"""Lifecycle locks survive directory swaps and cooperate with inherited FDs."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from operation_lock import LockError, operation_lock


class OperationLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'app'
        self.root.mkdir()

    def child(self, exclusive, pass_fd=None):
        code = ('import sys; sys.path.insert(0, sys.argv[1]); '
                'from operation_lock import operation_lock; '
                'ctx=operation_lock(sys.argv[2], sys.argv[3]=="1"); '
                'ctx.__enter__(); ctx.__exit__(None,None,None)')
        return subprocess.run([sys.executable, '-c', code, str(ROOT / 'scripts'),
                               str(self.root), '1' if exclusive else '0'],
                              pass_fds=() if pass_fd is None else (pass_fd,),
                              capture_output=True).returncode

    def test_shared_readers_coexist_but_block_upgrade(self):
        with operation_lock(self.root):
            self.assertEqual(self.child(False), 0)
            self.assertNotEqual(self.child(True), 0)
        self.assertEqual(self.child(True), 0)

    def test_exclusive_blocks_new_reader_and_writer(self):
        with operation_lock(self.root, exclusive=True):
            self.assertNotEqual(self.child(False), 0)
            self.assertNotEqual(self.child(True), 0)

    def test_bootstrap_exclusive_lock_inheritance(self):
        with operation_lock(self.root, exclusive=True) as fd:
            self.assertEqual(self.child(True, fd), 0)
            self.assertEqual(self.child(False, fd), 0)

    def test_lock_survives_root_rename(self):
        with operation_lock(self.root):
            self.root.rename(self.root.with_name('previous'))
            self.root.mkdir()
            self.assertNotEqual(self.child(True), 0)

    def test_symlink_lock_rejected(self):
        target = Path(self.tmp.name) / 'target'
        target.write_text('do not touch')
        (self.root.parent / '.app.operation.lock').symlink_to(target)
        with self.assertRaises(LockError):
            with operation_lock(self.root):
                pass
        self.assertEqual(target.read_text(), 'do not touch')

    def test_pending_upgrade_blocks_normal_operations(self):
        journal = self.root.parent / '.app.upgrade.json'
        journal.write_text('{}')
        self.assertNotEqual(self.child(False), 0)
        self.assertEqual(self.child(True), 0)

    def test_environment_bypass_without_fd_cannot_skip_lock(self):
        with operation_lock(self.root, exclusive=True):
            self.assertNotEqual(self.child(False), 0)


if __name__ == '__main__':
    unittest.main()
