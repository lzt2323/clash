#!/usr/bin/env python3
"""Menu behavior tests with a fake manager; never starts a proxy process."""

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location(
    "terminal_menu", Path(__file__).resolve().parents[1] / "scripts" / "terminal_menu.py"
)
menu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(menu)


class FakeManager:
    def __init__(self, count=12):
        self.calls = []
        self.state = {
            "running": True, "active": "a", "name": "测试订阅", "mode": "rule",
            "node": "AUTO", "shell_proxy": False,
            "subscriptions": [
                {"id": "a", "name": "测试订阅", "nodes": ["AUTO", "PROXY"] +
                 ["香港节点 %02d" % n for n in range(count)], "selected": "AUTO", "updated": "今天"},
                {"id": "b", "name": "备用订阅", "nodes": ["日本"], "selected": "日本"},
            ],
        }

    def status(self):
        return self.state

    def select(self, node):
        self.calls.append(("select", node))
        return "已选择 " + node

    def set_mode(self, mode):
        self.calls.append(("mode", mode))
        return "已设置模式"

    def delays_all(self):
        nodes = ["AUTO"] + [n for n in self.state["subscriptions"][0]["nodes"] if n not in ("AUTO", "PROXY")]
        return self.delays(nodes)

    def delays(self, nodes):
        self.calls.append(("delays", list(nodes)))
        return dict.fromkeys(nodes, 31)

    def add(self, name, url):
        self.calls.append(("add", name, url))
        return "已添加"

    def remove(self, identifier):
        self.calls.append(("remove", identifier))
        self.state["subscriptions"] = [item for item in self.state["subscriptions"] if item["id"] != identifier]
        return "已删除"

    def diagnose(self):
        self.calls.append(("diagnose",))
        return ["内核正常", "出站正常"]

    def restart(self):
        self.calls.append(("restart",))
        return "核心已重启。"

    def stop(self):
        self.calls.append(("stop",))
        return "核心已停止。请运行 clash off。"


class MenuTest(unittest.TestCase):
    def drive(self, manager, answers, secret="https://example.test/subscribe?token=secret"):
        output = io.StringIO()
        with mock.patch.object(menu.sys.stdin, "isatty", return_value=True), \
                mock.patch.object(output, "isatty", return_value=True), \
                mock.patch("builtins.input", side_effect=answers), \
                mock.patch.object(menu.getpass, "getpass", return_value=secret), \
                contextlib.redirect_stdout(output):
            result = menu.run(manager)
        return result, output.getvalue()

    def test_non_tty_refuses_without_prompting(self):
        with mock.patch.object(menu.sys.stdin, "isatty", return_value=False), \
                mock.patch("builtins.input") as read, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(menu.run(FakeManager()), 2)
        read.assert_not_called()

    def test_eof_inside_submenu_exits_everywhere(self):
        result, output = self.drive(FakeManager(), ["3", "1", EOFError()])
        self.assertEqual(result, 0)
        self.assertIn("输入已结束", output)

    def test_interrupt_cancels_operation_then_can_exit(self):
        manager = FakeManager()
        result, output = self.drive(manager, ["1", "s", KeyboardInterrupt(), "0"])
        self.assertEqual(result, 0)
        self.assertIn("已取消当前操作", output)
        self.assertEqual(manager.calls, [])

    def test_paging_search_and_no_implicit_measurement(self):
        manager = FakeManager()
        self.drive(manager, ["1", "n", "s", "11", "1", "0"])
        self.assertEqual(manager.calls, [("select", "香港节点 11")])

    def test_manual_measurement_includes_all_nodes_despite_search(self):
        manager = FakeManager()
        _, output = self.drive(manager, ["1", "n", "s", "11", "t", "0", "0"])
        measured = manager.calls[0]
        self.assertEqual(measured[0], "delays")
        self.assertEqual(len(measured[1]), 13)
        self.assertNotIn("PROXY", measured[1])
        self.assertIn("自动选择", output)
        self.assertIn("31 ms", output)

    def test_auto_is_available_when_provider_contains_only_nodes(self):
        manager = FakeManager()
        manager.state["subscriptions"][0]["nodes"] = ["香港"]
        _, output = self.drive(manager, ["1", "1", "0"])
        self.assertEqual(manager.calls, [("select", "AUTO")])
        self.assertIn("自动选择", output)

    def test_add_hides_secret(self):
        manager = FakeManager()
        _, output = self.drive(manager, ["3", "a", "新订阅", "0", "0"])
        self.assertEqual(manager.calls[0], ("add", "新订阅", "https://example.test/subscribe?token=secret"))
        self.assertNotIn("example.test", output)
        self.assertNotIn("token=secret", output)

    def test_active_subscription_delete_is_blocked(self):
        manager = FakeManager()
        _, output = self.drive(manager, ["3", "1", "4", "0", "0", "0"])
        self.assertIn("请先使用其他订阅", output)
        self.assertEqual(manager.calls, [])

    def test_delete_requires_confirmation(self):
        manager = FakeManager()
        self.drive(manager, ["3", "2", "4", "n", "4", "y", "0", "0"])
        self.assertEqual(manager.calls, [("remove", "b")])

    def test_long_unicode_output_fits_40_columns_and_scrubs_url(self):
        manager = FakeManager()
        manager.state["name"] = "测试订阅" * 30
        manager.state["node"] = "e\u0301" + "香港节点" * 30
        with mock.patch.object(menu.shutil, "get_terminal_size", return_value=os.terminal_size((40, 24))):
            _, output = self.drive(manager, ["0"])
        self.assertTrue(all(menu._width(line) <= 40 for line in output.splitlines()))
        self.assertNotIn("\x1b", menu._clean("\x1b[31mhttp://example.test/token?secret=x"))
        self.assertNotIn("secret=x", menu._clean("http://example.test/token?secret=x"))

    def test_status_failure_still_allows_exit(self):
        manager = FakeManager()
        manager.status = mock.Mock(side_effect=RuntimeError("接口暂时不可用"))
        result, output = self.drive(manager, ["0"])
        self.assertEqual(result, 0)
        self.assertIn("状态暂不可读", output)

    def test_diagnostics_entering_only_checks_and_recheck_is_read_only(self):
        manager = FakeManager()
        self.drive(manager, ["4", "1", "0", "0"])
        self.assertEqual(manager.calls, [("diagnose",), ("diagnose",)])

    def test_recovery_requires_explicit_confirmation(self):
        manager = FakeManager()
        _, output = self.drive(manager, ["4", "2", "n", "3", "", "0", "0"])
        self.assertEqual(manager.calls, [("diagnose",)])
        self.assertIn("影响其他终端", output)
        self.assertIn("clash off", output)

    def test_confirmed_restart_and_stop_execute_once(self):
        manager = FakeManager()
        self.drive(manager, ["4", "2", "y", "3", "y", "0", "0"])
        self.assertEqual(manager.calls, [("diagnose",), ("restart",), ("stop",)])

    def test_cancel_restart_confirmation_never_restarts(self):
        manager = FakeManager()
        _, output = self.drive(manager, ["4", "2", KeyboardInterrupt(), "0"])
        self.assertEqual(manager.calls, [("diagnose",)])
        self.assertIn("已取消当前操作", output)

    def test_failed_diagnostic_still_allows_recovery(self):
        manager = FakeManager()
        manager.diagnose = mock.Mock(side_effect=RuntimeError("核心接口异常"))
        _, output = self.drive(manager, ["4", "2", "y", "0", "0"])
        self.assertEqual(manager.calls, [("restart",)])
        self.assertIn("检测未完成", output)


if __name__ == "__main__":
    unittest.main()
