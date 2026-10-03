#!/usr/bin/env python3
"""Worker input boundaries and cancellation around atomic configuration commits."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch
from terminal_mvp_test import SubscriptionTransactions, mvp


class WorkerContract(unittest.TestCase):
    def call(self, manager, request):
        output = io.StringIO()
        stream = io.TextIOWrapper(io.BytesIO(json.dumps(request).encode()))
        with patch.object(sys, 'stdin', stream), redirect_stdout(output):
            code = mvp.worker(manager)
        return code, json.loads(output.getvalue())

    def test_private_methods_and_wrong_arguments_are_rejected(self):
        manager = mvp.Manager()
        for request in ({'action': '_load'}, {'action': 'add', 'args': ['one']},
                        {'action': 'use', 'args': [None]}, {'action': 'delays', 'args': [['x'] * 101]},
                        [], {'action': []}):
            with self.subTest(request=request):
                code, result = self.call(manager, request)
                self.assertEqual(code, 1)
                self.assertFalse(result['ok'])

    def test_library_errors_are_redacted(self):
        manager = mvp.Manager()
        with patch.object(manager, 'status', side_effect=OSError('https://secret.invalid/?token=PRIVATE')):
            code, result = self.call(manager, {'action': 'status'})
        self.assertEqual(code, 1)
        self.assertNotIn('PRIVATE', result['error'])

    def test_success_preserves_structured_result(self):
        manager = mvp.Manager()
        with patch.object(manager, 'status', return_value={'core_state': 'stopped'}):
            code, result = self.call(manager, {'action': 'status'})
        self.assertEqual(code, 0)
        self.assertEqual(result, {'ok': True, 'result': {'core_state': 'stopped'}})

    def test_all_delay_request_resolves_more_than_100_nodes_in_backend(self):
        manager = mvp.Manager()
        nodes = ["node-%d" % index for index in range(250)]
        state = {"active_id": "a", "subscriptions": [{"id": "a", "nodes": nodes}]}
        with patch.object(manager, '_load', return_value=state), patch.object(manager, 'delays', return_value={}) as delays:
            code, result = self.call(manager, {'action': 'delays_all'})
        self.assertEqual(code, 0)
        self.assertTrue(result['ok'])
        delays.assert_called_once_with(['AUTO'] + nodes)

    def test_cancel_returns_explicit_flag(self):
        manager = mvp.Manager()
        with patch.object(manager, 'diagnose', side_effect=mvp.OperationCancelled('已取消')):
            code, result = self.call(manager, {'action': 'diagnose'})
        self.assertEqual(code, 1)
        self.assertTrue(result['cancelled'])


class CancellationBoundaries(SubscriptionTransactions):
    def test_cancel_before_commit_preserves_files(self):
        self.add()
        before_state = self.manager.state_path.read_bytes()
        before_config = self.manager.config.read_bytes()
        def validated(_):
            self.manager.cancel_requested = lambda: True
        with patch.object(self.manager, '_validate_config', side_effect=validated):
            with self.assertRaises(mvp.OperationCancelled):
                self.manager.select('Tokyo 02')
        self.assertEqual(self.manager.state_path.read_bytes(), before_state)
        self.assertEqual(self.manager.config.read_bytes(), before_config)
        self.assertFalse(self.manager.journal.exists())

    def test_cancel_during_commit_finishes_consistently(self):
        self.add()
        atomic = self.manager._atomic
        def interrupted(path, data):
            atomic(path, data)
            if path == self.manager.journal:
                self.manager.cancel_requested = lambda: True
        with patch.object(self.manager, '_atomic', side_effect=interrupted):
            result = self.manager.select('Tokyo 02')
        self.assertIn('已保存', result)
        self.assertEqual(self.manager._item(self.state())['selected'], 'Tokyo 02')
        self.assertFalse(self.manager.journal.exists())

    def test_stop_is_safe_to_repeat(self):
        self.manager.stop()
        self.manager.stop()
        self.assertEqual([c.args[0] for c in self.runtime.call_args_list], ['stop', 'stop'])

    def test_stale_shell_environment_is_visible(self):
        expected = dict.fromkeys(('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY'), self.manager.http_url)
        expected.update(dict.fromkeys(('all_proxy', 'ALL_PROXY'), self.manager.socks_url))
        with patch.dict(os.environ, expected):
            status = self.manager.status()
        self.assertEqual(status['core_state'], 'stopped')
        self.assertEqual(status['shell_proxy'], '已设置，但核心未就绪')


if __name__ == '__main__':
    # Keep inherited fixture helpers without rerunning their transaction tests.
    suite = unittest.TestSuite()
    suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(WorkerContract))
    for name in CancellationBoundaries.__dict__:
        if name.startswith('test_'):
            suite.addTest(CancellationBoundaries(name))
    sys.exit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
