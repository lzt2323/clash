#!/usr/bin/env python3
"""Offline subscription transactions; never starts Mihomo or fetches a URL."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import socket
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("terminal_mvp", ROOT / "scripts/terminal_mvp.py")
mvp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mvp)


class SubscriptionTransactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="clash-mvp-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.manager = mvp.Manager(root=self.root)
        self.nodes = ["香港 01", "Tokyo 02"]
        self.fetch = self.start_patch("_fetch", return_value=b"synthetic provider")
        self.validate = self.start_patch("_validate_provider", side_effect=lambda content: list(self.nodes))
        self.start_patch("_validate_config", return_value=None)
        self.runtime = self.start_patch("_runtime", side_effect=self.successful_runtime)
        self.start_patch("_running", return_value=False)
        self.start_patch("_healthy", return_value=False)

    def start_patch(self, name, **kwargs):
        p = patch.object(self.manager, name, **kwargs)
        self.addCleanup(p.stop)
        return p.start()

    @staticmethod
    def successful_runtime(command, check=True):
        return subprocess.CompletedProcess([command], 0, stdout="synthetic runtime", stderr="")

    def state(self):
        return copy.deepcopy(self.manager._load())

    def add(self, name="主订阅", url="https://example.invalid/one"):
        self.manager.add(name, url)
        return next(item["id"] for item in self.state()["subscriptions"] if item["name"] == name)

    def subscription(self, identifier):
        return next(item for item in self.state()["subscriptions"] if item["id"] == identifier)

    def assert_unchanged_on_failure(self, operation):
        before = self.state()
        persisted = self.manager.state_path.read_bytes()
        with self.assertRaises(Exception):
            operation()
        self.assertEqual(self.state(), before)
        self.assertEqual(self.manager.state_path.read_bytes(), persisted)

    def test_selection_waits_for_provider_after_controller_becomes_ready(self):
        self.add()
        state = self.state()
        replies = [mvp.Error('HTTP 404'), {'proxies': []},
                   {'proxies': [{'name': name} for name in self.nodes]}, {}, {}]
        with patch.object(self.manager, '_api', side_effect=replies) as api, patch.object(mvp.time, 'sleep'):
            self.manager._restore_selection(state)
        self.assertEqual(api.call_count, 5)
        self.assertEqual(api.call_args_list[-2].args[:3], ('/proxies/PROXY', 'PUT', {'name': 'AUTO'}))
        self.assertEqual(api.call_args_list[-1].args[:3], ('/proxies/GLOBAL', 'PUT', {'name': 'PROXY'}))

    def test_occupied_proxy_port_is_detected_before_start(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            self.manager.http_url = 'http://127.0.0.1:%s' % listener.getsockname()[1]
            with self.assertRaises(mvp.Error):
                self.manager._check_ports()
        self.runtime.assert_not_called()

    def test_malformed_url_is_a_recoverable_error(self):
        for url in ('https://[invalid', 'https://example.invalid/\nheader', 'https://example.invalid:bad'):
            with self.subTest(url=url), self.assertRaises(mvp.Error):
                mvp.Manager._fetch(self.manager, url)

    def test_restart_checks_cache_before_stopping_core(self):
        identifier = self.add()
        cache = self.manager.conf_dir / self.subscription(identifier)['cache']
        cache.write_bytes(b'damaged')
        self.runtime.reset_mock()
        with self.assertRaises(mvp.Error):
            self.manager.restart()
        self.runtime.assert_not_called()

    def test_first_add_becomes_active_and_state_survives_new_manager(self):
        identifier = self.add()
        self.assertEqual(self.state()["active_id"], identifier)
        self.assertEqual(self.subscription(identifier)["nodes"], self.nodes)
        self.assertEqual(mvp.Manager(root=self.root)._load(), self.state())

    def test_duplicate_name_is_rejected(self):
        self.add()
        self.assert_unchanged_on_failure(lambda: self.manager.add("主订阅", "https://example.invalid/other"))

    def test_duplicate_url_is_rejected(self):
        self.add()
        self.assert_unchanged_on_failure(lambda: self.manager.add("重复链接", "https://example.invalid/one"))

    def test_failed_first_add_does_not_create_active_state_or_config(self):
        with patch.object(self.manager, "_validate_config", side_effect=mvp.Error("synthetic config failure")):
            with self.assertRaises(mvp.Error):
                self.add()
        self.assertFalse(self.manager.state_path.exists())
        self.assertFalse(self.manager.config.exists())
        self.assertFalse(self.manager.journal.exists())

    def test_download_failure_does_not_add_or_replace_subscription(self):
        identifier = self.add()
        self.fetch.side_effect = RuntimeError("synthetic download failure")
        self.assert_unchanged_on_failure(lambda: self.manager.add("新订阅", "https://example.invalid/two"))
        self.assert_unchanged_on_failure(lambda: self.manager.update(identifier))
        self.assert_unchanged_on_failure(lambda: self.manager.edit(identifier, "https://example.invalid/new"))

    def test_validation_failure_preserves_existing_source_and_nodes(self):
        identifier = self.add()
        self.validate.side_effect = RuntimeError("synthetic invalid provider")
        self.assert_unchanged_on_failure(lambda: self.manager.update(identifier))
        self.assert_unchanged_on_failure(lambda: self.manager.edit(identifier, "https://example.invalid/new"))

    def test_inactive_add_and_remove_preserve_active_subscription(self):
        active = self.add()
        self.runtime.reset_mock()
        other = self.add("备用订阅", "https://example.invalid/two")
        self.assertEqual(self.state()["active_id"], active)
        self.manager.remove(other)
        self.assertEqual(self.state()["active_id"], active)
        self.assertEqual(len(self.state()["subscriptions"]), 1)
        self.runtime.assert_not_called()

    def test_active_subscription_cannot_be_removed(self):
        active = self.add()
        self.assert_unchanged_on_failure(lambda: self.manager.remove(active))

    def test_switching_subscription_restores_its_selected_node(self):
        active = self.add()
        self.manager.select("Tokyo 02")
        other = self.add("备用订阅", "https://example.invalid/two")
        self.manager.use(other)
        self.manager.select("香港 01")
        self.manager.use(active)
        self.assertEqual(self.state()["active_id"], active)
        self.assertEqual(self.subscription(active)["selected"], "Tokyo 02")

    def test_update_preserves_selected_node_when_still_available(self):
        identifier = self.add()
        self.manager.select("Tokyo 02")
        self.nodes = ["Tokyo 02", "Singapore 03"]
        self.manager.update(identifier)
        self.assertEqual(self.subscription(identifier)["selected"], "Tokyo 02")
        self.assertEqual(self.subscription(identifier)["nodes"], self.nodes)

    def test_update_recovers_auto_when_selected_node_disappears(self):
        identifier = self.add()
        self.manager.select("Tokyo 02")
        self.nodes = ["Singapore 03"]
        message = self.manager.update(identifier)
        self.assertEqual(self.subscription(identifier)["selected"], "AUTO")
        self.assertIn("自动", message)

    def test_unknown_node_and_mode_do_not_modify_state(self):
        self.add()
        self.assert_unchanged_on_failure(lambda: self.manager.select("not a node"))
        self.assert_unchanged_on_failure(lambda: self.manager.set_mode("invalid-mode"))

    def test_source_edit_validates_before_commit(self):
        identifier = self.add()
        self.manager.edit(identifier, "https://example.invalid/new")
        self.assertEqual(self.subscription(identifier)["url"], "https://example.invalid/new")
        self.fetch.assert_called_with("https://example.invalid/new")

    def test_config_validation_failure_rolls_back_active_switch(self):
        active = self.add()
        other = self.add("备用订阅", "https://example.invalid/two")
        with patch.object(self.manager, "_validate_config", side_effect=RuntimeError("synthetic config failure")):
            self.assert_unchanged_on_failure(lambda: self.manager.use(other))
        self.assertEqual(self.state()["active_id"], active)

    def test_live_reload_failure_restores_config_and_state_without_restart(self):
        active = self.add()
        self.manager.select("Tokyo 02")
        other = self.add("备用订阅", "https://example.invalid/two")
        config_before = self.manager.config.read_bytes()
        self.runtime.reset_mock()
        with patch.object(self.manager, "_running", return_value=True), \
                patch.object(self.manager, "_healthy", return_value=True), \
                patch.object(self.manager, "_reload", side_effect=[mvp.Error("synthetic reload failure"), None]) as reload:
            self.assert_unchanged_on_failure(lambda: self.manager.use(other))
        self.assertEqual(self.manager.config.read_bytes(), config_before)
        self.assertEqual(reload.call_args_list[-1].args[0]["active_id"], active)
        self.assertEqual(reload.call_args_list[-1].args[0]["subscriptions"][0]["selected"], "Tokyo 02")
        self.assertFalse(self.manager.journal.exists())
        self.runtime.assert_not_called()

    def test_interrupted_transaction_is_recovered_before_next_operation(self):
        active = self.add()
        old = self.state()
        config_before = self.manager.config.read_text()
        other = self.add("备用订阅", "https://example.invalid/two")
        old = self.state()
        self.manager._atomic(self.manager.journal, json.dumps({
            "state": old, "config": config_before, "running": False, "had_state": True,
        }))
        interrupted = copy.deepcopy(old)
        interrupted["active_id"] = other
        self.manager._save(interrupted)
        self.manager._atomic(self.manager.config, "synthetic interrupted config")
        self.manager.use(active)
        self.assertEqual(self.state(), old)
        self.assertEqual(self.manager.config.read_text(), config_before)
        self.assertFalse(self.manager.journal.exists())

    def test_failed_rollback_keeps_journal_until_next_successful_recovery(self):
        active = self.add()
        other = self.add("备用订阅", "https://example.invalid/two")
        before = self.state()
        config_before = self.manager.config.read_bytes()
        with patch.object(self.manager, "_running", return_value=True), \
                patch.object(self.manager, "_healthy", return_value=True), \
                patch.object(self.manager, "_reload", side_effect=mvp.Error("synthetic API failure")):
            self.assert_unchanged_on_failure(lambda: self.manager.use(other))
        self.assertTrue(self.manager.journal.exists())
        self.assertEqual(self.manager.journal.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.config.read_bytes(), config_before)
        with patch.object(self.manager, "_healthy", return_value=True), \
                patch.object(self.manager, "_reload", return_value=None) as reload:
            self.manager.use(active)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.manager.journal.exists())
        reload.assert_called_once_with(before)

    def test_interrupted_live_transaction_can_recover_an_unhealthy_core(self):
        active = self.add()
        before = self.state()
        old_config = self.manager.config.read_text()
        self.manager._atomic(self.manager.journal, json.dumps({
            "state": before, "config": old_config, "running": True, "had_state": True,
        }))
        self.manager._atomic(self.manager.config, "synthetic interrupted config")
        self.runtime.reset_mock()
        with patch.object(self.manager, "_running", return_value=True), \
                patch.object(self.manager, "_healthy", return_value=False), \
                patch.object(self.manager, "_reload", return_value=None) as reload:
            self.manager.use(active)
        self.assertEqual([call.args[0] for call in self.runtime.call_args_list], ["stop", "start"])
        self.assertEqual(self.state(), before)
        self.assertEqual(self.manager.config.read_text(), old_config)
        self.assertFalse(self.manager.journal.exists())
        reload.assert_called_once_with(before)

    def test_private_files_and_public_status_do_not_expose_credentials(self):
        self.add()
        state = self.state()
        self.assertEqual(self.manager.state_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.config.stat().st_mode & 0o777, 0o600)
        public = json.dumps(self.manager.status(), ensure_ascii=False)
        self.assertNotIn(state["secret"], public)
        self.assertNotIn(state["subscriptions"][0]["url"], public)

    def test_generated_config_binds_locally_and_declares_private_routes(self):
        self.add()
        config = json.loads(self.manager.config.read_text())
        self.assertFalse(config["allow-lan"])
        self.assertEqual(config["bind-address"], "127.0.0.1")
        self.assertEqual(config["external-controller"], "127.0.0.1:9090")
        self.assertIn("IP-CIDR,192.168.0.0/16,DIRECT,no-resolve", config["rules"])
        self.assertEqual(config["rules"][-1], "MATCH,PROXY")

    def test_runtime_uses_managed_json_secret_and_temporary_runtime_directory(self):
        self.add()
        config = json.loads(self.manager.config.read_text())
        with patch.dict(mvp.os.environ, {"MIHOMO_API_SECRET": "synthetic stale legacy secret"}), \
                patch.object(mvp.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            # Call the real adapter; the transaction fixture otherwise replaces it.
            mvp.Manager._runtime(self.manager, "health")
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["MIHOMO_API_SECRET"], config["secret"])
        self.assertEqual(env["MIHOMO_RUNTIME_DIR"], str(self.root / "runtime"))
        self.assertEqual(env["MIHOMO_CONFIG"], str(self.manager.config))


if __name__ == "__main__":
    unittest.main(verbosity=2)
