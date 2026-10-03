"""Responsive curses workbench. All manager operations run in a worker process."""

import curses
import json
import locale
import os
from pathlib import Path
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata


PAGES = ("节点", "模式", "订阅", "诊断")
MODES = (("rule", "规则", "私网直连，其他走代理。"),
         ("global", "全局", "接入核心的流量全部交给所选代理。"),
         ("direct", "直连", "接入核心的流量直接访问。"))
MUTATIONS = {"add", "use", "update", "edit", "remove", "select", "set_mode",
             "ensure", "restart", "stop"}


def clean(value):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(value))
    text = re.sub(r"https?://[^\s<>\"']+", "[地址已隐藏]", text, flags=re.I)
    return "".join(c for c in text if not unicodedata.category(c).startswith("C"))


def cell_width(text):
    return sum(0 if unicodedata.combining(c) or unicodedata.category(c) in ("Mn", "Me")
               else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in text)


def clip(value, width):
    text = clean(value)
    if cell_width(text) <= width:
        return text
    suffix = "..." if width >= 3 else ""
    result = ""
    for c in text:
        if cell_width(result + c) > width - len(suffix):
            break
        result += c
    return result + suffix


def wrap(value, width):
    lines, line = [], ""
    for paragraph in str(value).split("\n"):
        for c in clean(paragraph):
            if line and cell_width(line + c) > width:
                lines.append(line)
                line = ""
            line += c
        lines.append(line)
        line = ""
    return lines


def node_label(name):
    return "自动选择" if name == "AUTO" else clean(name)


def layout(height, width):
    wide = width >= 70
    top = 4 if wide else 8
    body_bottom = height - 5
    list_start = top + (3 if wide else 2)
    show_detail = body_bottom - list_start + 1 >= 7
    detail_top = body_bottom - 4 if show_detail else body_bottom + 1
    return {"wide": wide, "nav": 19 if wide else 0, "top": top,
            "bottom": body_bottom, "detail": detail_top, "list": list_start,
            "rows": max(1, min(8, detail_top - list_start)),
            "show_detail": show_detail, "small": height < 18 or width < 30}


class TerminalClosed(Exception):
    pass


def terminal_closed():
    try:
        descriptor = sys.stdin.fileno()
        if not os.isatty(descriptor):
            return True
        if hasattr(select, "poll"):
            probe = select.poll()
            probe.register(descriptor, select.POLLHUP | select.POLLERR | select.POLLNVAL)
            return any(mask & (select.POLLHUP | select.POLLERR | select.POLLNVAL)
                       for _, mask in probe.poll(0))
    except (OSError, ValueError):
        return True
    return False


class Worker:
    """One request, one process; file-backed output prevents full-pipe deadlocks."""

    def __init__(self, manager, action, args):
        root = Path(getattr(manager, "root", Path(__file__).resolve().parent.parent))
        script = Path(os.environ.get("CLASH_WORKBENCH_WORKER", str(root / "scripts/terminal_mvp.py")))
        if not script.exists() and "CLASH_WORKBENCH_WORKER" not in os.environ:
            script = Path(__file__).with_name("terminal_mvp.py")
        self.action, self.args = action, args
        self.started = time.monotonic()
        self.output = tempfile.TemporaryFile(mode="w+b")
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-E", "-s", "-B", str(script), "worker"], cwd=str(root),
                stdin=subprocess.PIPE, stdout=self.output, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            self.output.close()
            raise RuntimeError("无法启动后台操作，请检查 Python 和项目文件。") from None
        try:
            payload = (json.dumps({"action": action, "args": args}, ensure_ascii=False) + "\n").encode()
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except (OSError, BrokenPipeError):
            pass  # poll() reports the worker's response or a safe protocol error.
        finally:
            self.process.stdin.close()
        self.finished = False
        self.cancel_requested = False

    def cancel(self):
        if self.finished or self.cancel_requested:
            return
        self.cancel_requested = True
        try:
            self.process.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass

    def poll(self):
        if self.finished or self.process.poll() is None:
            return None
        self.finished = True
        try:
            self.output.seek(0)
            payload = json.loads(self.output.read().decode("utf-8"))
            if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
                raise ValueError()
            if payload["ok"] and self.process.returncode:
                raise ValueError()
            return payload
        except (ValueError, UnicodeError):
            return {"ok": False, "error": "后台操作未正常完成，请运行诊断检查状态。"}
        finally:
            self.output.close()


class Workbench:
    def __init__(self, screen, manager):
        self.screen, self.manager = screen, manager
        self.state = None
        self.page, self.nav_page, self.focus = 0, 0, "list"
        self.indices = [0, 0, 0, 0]
        self.query = ""
        self.delays = {}
        self.diagnostics = ["进入诊断后可检查连接并恢复核心。"]
        self.message, self.error = "正在读取状态...", False
        self.job, self.job_label = None, ""
        self.refresh_needed, self.diagnose_needed = False, False
        self.quit_requested, self.running = False, True
        self.dialog = None
        self.dirty = True
        self.color = {}
        self.unicode = "utf" in locale.getpreferredencoding(False).lower()
        self.size = self.screen.getmaxyx()
        self.geom = layout(*self.size)
        self.job_second = -1

    def setup(self):
        self.screen.keypad(True)
        self.screen.timeout(80)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if "NO_COLOR" not in os.environ and curses.has_colors():
            try:
                curses.start_color()
                curses.use_default_colors()
                for index, (name, foreground) in enumerate(
                    (("accent", curses.COLOR_CYAN), ("good", curses.COLOR_GREEN),
                     ("warn", curses.COLOR_YELLOW), ("error", curses.COLOR_RED)), 1
                ):
                    curses.init_pair(index, foreground, -1)
                    self.color[name] = curses.color_pair(index)
            except curses.error:
                self.color = {}
        self.submit("status", [], "读取状态")

    def notice(self, text, error=False):
        self.message, self.error, self.dirty = clean(text), error, True

    def submit(self, action, args, label):
        if self.job:
            self.notice("当前操作仍在进行，可继续浏览，请勿重复提交。")
            return False
        try:
            self.job = Worker(self.manager, action, args)
        except RuntimeError as exc:
            self.notice(exc, True)
            return False
        self.job_label, self.job_second = label, -1
        self.dirty = True
        return True

    def poll(self):
        if self.job:
            envelope = self.job.poll()
            second = int(time.monotonic() - self.job.started)
            if second != self.job_second:
                self.job_second, self.dirty = second, True
            if envelope is not None:
                action = self.job.action
                cancellation_requested = self.job.cancel_requested
                self.job = None
                if not envelope["ok"]:
                    cancelled = envelope.get("cancelled") is True
                    if cancelled:
                        self.refresh_needed, self.diagnose_needed = False, False
                    self.notice("已取消当前操作。" if cancelled else envelope.get("error", "操作失败，请重试。"), not cancelled)
                    if action == "diagnose":
                        self.diagnostics = [self.message]
                else:
                    result = envelope.get("result")
                    if action == "status":
                        if isinstance(result, dict):
                            previous = (self.state or {}).get("active")
                            self.state = result
                            if previous != result.get("active"):
                                self.delays = {}
                                self.indices[0] = 0
                            if self.message == "正在读取状态..." or cancellation_requested:
                                self.notice("状态已读取。选择项目后按 Enter 执行。")
                        else:
                            self.notice("状态返回格式异常，请重试。", True)
                    elif action in ("delays", "delays_all"):
                        if isinstance(result, dict):
                            self.delays.update(result)
                        self.notice("全部节点测速完成。")
                    elif action == "diagnose":
                        self.diagnostics = result if isinstance(result, list) else [clean(result)]
                        self.notice("检测完成。")
                    else:
                        if action in ("use", "update", "edit"):
                            self.delays = {}
                        self.notice(result or "操作已完成。")
                        if action in MUTATIONS:
                            self.refresh_needed = True
                    self.dirty = True
                if self.quit_requested:
                    self.running = False
                    return
        if not self.job:
            if self.refresh_needed:
                self.refresh_needed = False
                self.submit("status", [], "刷新状态")
            elif self.diagnose_needed:
                self.diagnose_needed = False
                self.submit("diagnose", [], "检查连接")

    def active(self):
        state = self.state or {}
        return next((item for item in state.get("subscriptions", [])
                     if str(item.get("id")) == str(state.get("active"))), None)

    def items(self):
        state = self.state or {}
        if self.page == 0:
            subscription = self.active()
            if not subscription:
                return []
            names = ["AUTO"] + [name for name in subscription.get("nodes", [])
                                if name not in ("AUTO", "PROXY")]
            return [name for name in names
                    if self.query.casefold() in node_label(name).casefold()]
        if self.page == 1:
            return list(MODES)
        if self.page == 2:
            return state.get("subscriptions", [])
        return [("diagnose", "重新检测"), ("restart", "重启核心"), ("stop", "停止核心")]

    def selection(self):
        items = self.items()
        index = min(self.indices[self.page], max(0, len(items) - 1))
        self.indices[self.page] = index
        return items[index] if items else None

    def visible(self):
        items = self.items()
        self.selection()
        index, rows = self.indices[self.page], self.geom["rows"]
        offset = (index // rows) * rows
        return offset, items[offset:offset + rows]

    def switch(self, page):
        self.page, self.nav_page, self.focus = page, page, "list"
        self.dirty = True
        if page == 3:
            if self.job:
                self.diagnose_needed = True
            else:
                self.submit("diagnose", [], "检查连接")

    def move(self, amount):
        if self.focus == "nav":
            self.nav_page = (self.nav_page + amount) % len(PAGES)
        else:
            self.indices[self.page] = max(0, min(len(self.items()) - 1,
                                                self.indices[self.page] + amount))
        self.dirty = True

    def confirm(self, title, text, action, args):
        if self.job:
            self.notice("当前操作仍在进行，请完成后再执行此操作。")
            return
        self.dialog = {"kind": "confirm", "title": title, "text": text,
                       "action": action, "args": args, "yes": False}
        self.dirty = True

    def stop_dialog(self):
        self.confirm("停止核心", "会断开其他使用核心的终端。停止后请退出菜单并运行 clash off。",
                     "stop", [])

    def restart_dialog(self):
        self.confirm("重启核心", "会中断正在使用核心的连接，也会影响其他终端。",
                     "restart", [])

    def form(self, kind, identity=None):
        if self.job:
            self.notice("当前操作仍在进行，请稍后再输入。")
            return
        self.dialog = {"kind": kind, "title": {"search": "搜索节点", "add": "添加订阅", "edit": "编辑订阅地址"}[kind],
                       "field": "name" if kind == "add" else "search" if kind == "search" else "url",
                       "value": self.query if kind == "search" else "", "name": "", "id": identity,
                       "error": ""}
        self.dirty = True

    def activate(self):
        if self.focus == "nav":
            self.switch(self.nav_page)
            return
        item = self.selection()
        if item is None:
            self.notice("请按 3 进入订阅管理，再按 a 添加订阅。")
            return
        if self.page == 0:
            self.submit("select", [item], "选择节点")
        elif self.page == 1:
            self.submit("set_mode", [item[0]], "设置模式")
        elif self.page == 2:
            self.submit("use", [item["id"]], "使用订阅")
        elif item[0] == "diagnose":
            self.submit("diagnose", [], "检查连接")
        elif item[0] == "restart":
            self.restart_dialog()
        else:
            self.stop_dialog()

    def dialog_key(self, key):
        dialog = self.dialog
        if key in ("\x1b", "\x03"):
            self.dialog = None
            self.notice("已取消输入。")
            return
        if dialog["kind"] == "confirm":
            if key in ("\t", curses.KEY_LEFT, curses.KEY_RIGHT, "h", "l"):
                dialog["yes"] = not dialog["yes"]
            elif key in ("y", "Y") or key in ("\n", "\r", curses.KEY_ENTER) and dialog["yes"]:
                if self.submit(dialog["action"], dialog["args"], dialog["title"]):
                    self.dialog = None
            elif key in ("n", "N", "\n", "\r", curses.KEY_ENTER):
                self.dialog = None
                self.notice("已取消操作。")
            self.dirty = True
            return
        if key in ("\n", "\r", curses.KEY_ENTER):
            value = dialog["value"].strip()
            if dialog["kind"] == "search":
                self.query, self.indices[0], self.dialog = value, 0, None
            elif dialog["field"] == "name":
                if not value:
                    dialog["error"] = "名称不能为空。"
                elif len(value) > 60:
                    dialog["error"] = "名称最多 60 个字符。"
                else:
                    dialog["name"], dialog["field"], dialog["value"], dialog["error"] = value, "url", "", ""
            elif not value:
                dialog["error"] = "订阅地址不能为空。"
            else:
                action = "add" if dialog["kind"] == "add" else "edit"
                args = [dialog["name"], value] if action == "add" else [dialog["id"], value]
                if self.submit(action, args, "添加订阅" if action == "add" else "更新地址"):
                    self.dialog = None
        elif key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            dialog["value"] = dialog["value"][:-1]
            dialog["error"] = ""
        elif key == "\x15":
            dialog["value"], dialog["error"] = "", ""
        elif isinstance(key, str) and key.isprintable():
            limit = 4096 if dialog["field"] == "url" else 120
            if len(dialog["value"]) < limit:
                dialog["value"] += key
                dialog["error"] = ""
        self.dirty = True

    def key(self, key):
        if key == curses.KEY_RESIZE:
            self.dirty = True
            return
        if key == "\x04":
            self.dialog = None
            key = "q"
        if self.dialog:
            self.dialog_key(key)
            return
        if key in ("q", "Q"):
            if self.job:
                self.quit_requested = True
                self.job.cancel()
                self.notice("正在取消，等待当前操作结束后退出。")
            else:
                self.running = False
        elif key in ("\x1b", "\x03"):
            self.focus, self.nav_page = "nav", self.page
            if self.job:
                self.job.cancel()
                self.notice("正在取消，等待当前操作结束；可继续浏览。")
            else:
                self.notice("已返回任务导航。")
        elif key in ("1", "2", "3", "4"):
            self.switch(int(key) - 1)
        elif key == curses.KEY_LEFT:
            if self.focus != "nav":
                self.focus, self.nav_page = "nav", self.page
        elif key == curses.KEY_RIGHT:
            if self.focus == "nav":
                self.switch(self.nav_page)
        elif key == "\t":
            self.focus = "nav" if self.focus == "list" else "list"
            self.nav_page = self.page
        elif key in (curses.KEY_UP, "k"):
            self.move(-1)
        elif key in (curses.KEY_DOWN, "j"):
            self.move(1)
        elif key in (curses.KEY_NPAGE, "n") and self.focus == "list":
            self.move(self.geom["rows"])
        elif key in (curses.KEY_PPAGE, "p") and self.focus == "list":
            self.move(-self.geom["rows"])
        elif key in (curses.KEY_HOME, "g") and self.focus == "list":
            self.indices[self.page] = 0
        elif key == curses.KEY_END and self.focus == "list":
            self.indices[self.page] = max(0, len(self.items()) - 1)
        elif key in ("\n", "\r", curses.KEY_ENTER):
            self.activate()
        elif key == "/" and self.page == 0:
            self.form("search")
        elif key == "t" and self.page == 0:
            if self.active():
                self.submit("delays_all", [], "全部节点测速")
        elif key == "s":
            self.submit("ensure", [], "启动核心")
        elif key == "x":
            self.stop_dialog()
        elif key == "r":
            self.submit("diagnose" if self.page == 3 else "status", [], "检查连接" if self.page == 3 else "刷新状态")
        elif key == "R" and self.page == 3:
            self.restart_dialog()
        elif self.page == 2 and key in ("a", "u", "e", "d"):
            item = self.selection()
            if key == "a":
                self.form("add")
            elif item:
                if key == "u":
                    self.submit("update", [item["id"]], "更新订阅")
                elif key == "e":
                    self.form("edit", item["id"])
                elif str(item["id"]) == str((self.state or {}).get("active")):
                    self.notice("此订阅正在使用，请先使用其他订阅再删除。", True)
                else:
                    self.confirm("删除订阅", "将删除订阅“%s”的记录。" % clean(item.get("name", "")), "remove", [item["id"]])
        elif key == "?":
            self.dialog = {"kind": "help", "title": "键盘帮助"}
        self.dirty = True

    def put(self, y, x, value, attr=0, width=None):
        height, columns = self.size
        if y < 0 or y >= height or x < 0 or x >= columns:
            return
        available = columns - x - (1 if y == height - 1 else 0)
        available = min(available, width) if width is not None else available
        if available <= 0:
            return
        try:
            self.screen.addstr(y, x, clip(value, available), attr)
        except curses.error:
            pass

    def line(self, y, x, width):
        self.put(y, x, ("─" if self.unicode else "-") * max(0, width), curses.A_DIM, width)

    def text_lines(self, y, x, text, width, height, attr=0):
        for index, line in enumerate(wrap(text, max(1, width))[:max(0, height)]):
            self.put(y + index, x, line, attr, width)

    def delay_text(self, node):
        value = self.delays.get(node)
        if value is None:
            return "未测速"
        if isinstance(value, dict):
            value = value.get("delay", value.get("error", "失败"))
        return "%s ms" % value if isinstance(value, (int, float)) and value > 0 else clean(value)

    def header(self):
        state = self.state or {}
        _, width = self.size
        self.put(0, 1, "CLASH  终端工作台", curses.A_BOLD | self.color.get("accent", 0))
        core_state = state.get("core_state", "running" if state.get("running") else "unknown")
        ready = "读取中" if self.state is None else {
            "running": "运行中", "stopped": "已停止", "unhealthy": "接口异常", "unknown": "未知"
        }.get(core_state, "未知")
        shell = state.get("shell_proxy", "读取中")
        mode = dict((code, name) for code, name, _ in MODES).get(state.get("mode"), "读取中")
        name = clean(state.get("name", "读取中"))
        node = node_label(state.get("node", "未选择")) if state.get("active") else "未选择"
        if self.geom["wide"]:
            self.put(1, 1, "核心 %s   当前终端 %s   模式 %s" % (ready, shell, mode))
            self.put(2, 1, "订阅 %s   当前节点 %s" % (clip(name, max(12, width // 3)), node))
            self.line(3, 0, width)
        else:
            self.put(1, 1, "核心：" + ready)
            self.put(2, 1, "当前终端：" + str(shell))
            self.put(3, 1, "订阅：" + name)
            self.put(4, 1, "模式 %s  节点 %s" % (mode, node))
            for index, page in enumerate(PAGES):
                row, column = 5 + index // 2, 1 + (index % 2) * (width // 2)
                selected = self.nav_page == index if self.focus == "nav" else self.page == index
                attr = curses.A_REVERSE | curses.A_BOLD if selected else 0
                self.put(row, column, "%d %s" % (index + 1, page), attr, width // 2 - 1)
            self.line(7, 0, width)

    def navigation(self):
        if not self.geom["wide"]:
            return
        top, bottom = self.geom["top"], self.geom["bottom"]
        self.put(top, 1, "任务", curses.A_BOLD)
        for index, page in enumerate(PAGES):
            focused = self.focus == "nav" and self.nav_page == index
            marker = ">" if focused else "*" if self.page == index else " "
            attr = curses.A_REVERSE | curses.A_BOLD if focused else self.color.get("accent", 0) if self.page == index else 0
            self.put(top + 2 + index, 1, "%s %d %s" % (marker, index + 1, page), attr, 16)
        if bottom - top >= 11:
            self.put(top + 8, 1, "核心操作", curses.A_BOLD)
            self.put(top + 9, 1, "s 启动")
            self.put(top + 10, 1, "x 停止")
        for y in range(top, bottom + 1):
            self.put(y, self.geom["nav"] - 1, "│" if self.unicode else "|", curses.A_DIM)

    def content(self):
        height, width = self.size
        x = self.geom["nav"] + 1
        available = width - x - 1
        top, detail = self.geom["top"], self.geom["detail"]
        offset, items = self.visible()
        total = len(self.items())
        index = self.indices[self.page]
        self.put(top, x, PAGES[self.page], curses.A_BOLD | self.color.get("accent", 0), available)
        summary = "搜索：" + self.query if self.page == 0 and self.query else "共 %d 项   第 %d/%d 页" % (total, offset // self.geom["rows"] + 1, max(1, (total + self.geom["rows"] - 1) // self.geom["rows"]))
        self.put(top + 1, x, summary, curses.A_DIM, available)
        if self.geom["wide"]:
            self.line(top + 2, x, available)
        if not items:
            text = "没有匹配的节点，按 / 修改搜索。" if self.active() and self.page == 0 else "尚未添加订阅，按 3 再按 a 添加。"
            self.text_lines(self.geom["list"], x, text, available, self.geom["rows"])
        state = self.state or {}
        for position, item in enumerate(items):
            focused = self.focus == "list" and offset + position == index
            prefix = "> " if focused else "  "
            attr = curses.A_REVERSE | curses.A_BOLD if focused else 0
            if self.page == 0:
                current = item == state.get("node")
                label = prefix + ("* " if current else "  ") + node_label(item)
                if available >= 42:
                    label = clip(label, available - 14)
                    label += " " * max(1, available - 14 - cell_width(label)) + self.delay_text(item)
            elif self.page == 1:
                label = prefix + ("* " if item[0] == state.get("mode") else "  ") + item[1]
            elif self.page == 2:
                current = str(item["id"]) == str(state.get("active"))
                label = prefix + ("* " if current else "  ") + clean(item.get("name", "未命名"))
            else:
                label = prefix + item[1]
            if focused:
                label = clip(label, available)
                label += " " * max(0, available - cell_width(label))
            self.put(self.geom["list"] + position, x, label, attr, available)
        if not self.geom["show_detail"]:
            return
        self.line(detail, x, available)
        self.put(detail, x, " 所选项目 ", curses.A_BOLD, available)
        selected = self.selection()
        if self.page == 0 and selected is not None:
            body = "%s\n延迟：%s\nEnter 使用  / 搜索  t 全部测速" % (node_label(selected), self.delay_text(selected))
        elif self.page == 1 and selected is not None:
            body = "%s\n%s\n模式只影响接入核心的流量。" % (selected[1], selected[2])
        elif self.page == 2 and selected is not None:
            body = "%s · %s 个节点\n更新时间：%s\nEnter 使用  u 更新  e 编辑  d 删除" % (clean(selected.get("name", "")), len(selected.get("nodes", [])), clean(selected.get("updated", "未更新")))
        elif self.page == 3:
            body = "\n".join(clean(line) for line in self.diagnostics)
        else:
            body = "先添加订阅，再选择节点。\n启动核心不会修改当前终端代理。"
        self.text_lines(detail + 1, x, body, available, max(0, self.geom["bottom"] - detail))

    def footer(self):
        height, width = self.size
        y = height - 4
        if self.job:
            message = "[进行中 %ds] %s；可浏览，q 等待完成后退出" % (self.job_second, self.job_label)
            if self.job.cancel_requested:
                message = "正在取消，等待安全结束" + ("后退出。" if self.quit_requested else "；可继续浏览。")
            attr = self.color.get("warn", 0)
        else:
            message, attr = self.message, self.color.get("error", 0) if self.error else 0
        self.put(y, 1, message, attr, width - 2)
        if width >= 70:
            context = ("Enter 使用  / 搜索  t 全测" if self.page == 0 else
                       "Enter 应用模式" if self.page == 1 else
                       "Enter 使用  a 添加  u 更新  e 编辑  d 删除" if self.page == 2 else
                       "Enter 执行  r 重新检测  R 重启")
            self.put(y + 1, 1, "←→/Tab 区域  ↑↓/jk 浏览  " + context, width=width - 2)
            self.put(y + 2, 1, "1-4 任务  s 启动  x 停止  Esc 返回  ? 帮助  q 退出", width=width - 2)
        else:
            context = "/ 搜索 t 全测" if self.page == 0 else "a 添 u 更 e 改 d 删" if self.page == 2 else "r 检测 R 重启" if self.page == 3 else ""
            self.put(y + 1, 1, "↑↓ 选 Enter 执行 " + context, width=width - 2)
            self.put(y + 2, 1, "1-4页 s启动 x停止 Esc返 q退 ?帮助", width=width - 2)

    def render_dialog(self):
        dialog = self.dialog
        height, width = self.size
        box_width = min(58, width - 4)
        box_height = min(12 if dialog["kind"] == "help" else 10, height - 4)
        top, left = (height - box_height) // 2, (width - box_width) // 2
        for row in range(top, top + box_height):
            self.put(row, left, " " * box_width, curses.A_NORMAL, box_width)
        self.line(top, left, box_width)
        self.line(top + box_height - 1, left, box_width)
        self.put(top + 1, left + 2, dialog["title"], curses.A_BOLD | self.color.get("accent", 0), box_width - 4)
        inner = box_width - 4
        if dialog["kind"] == "confirm":
            self.text_lines(top + 3, left + 2, dialog["text"], inner, box_height - 6)
            self.put(top + box_height - 3, left + 2, "[取消]", curses.A_REVERSE if not dialog["yes"] else 0, inner)
            self.put(top + box_height - 3, left + 12, "[确认]", curses.A_REVERSE if dialog["yes"] else 0, inner - 10)
            self.put(top + box_height - 2, left + 2, "Tab 切换  Enter 执行  Esc 取消", width=inner)
        elif dialog["kind"] == "help":
            help_text = "1-4 切换任务；←→/Tab 切换区域\n↑↓ 或 j/k 浏览，Enter 执行\n/ 搜索；t 全部节点测速\na 添加，u 更新，e 改地址，d 删除\ns 启动核心；x 停止核心\nq 退出；Esc 返回或关闭\n* 当前使用，> 光标所选\n启动核心后，clash on 启用终端代理"
            self.text_lines(top + 2, left + 2, help_text, inner, box_height - 3)
        else:
            field = dialog["field"]
            prompt = "订阅名称" if field == "name" else "订阅地址（隐藏）" if field == "url" else "搜索词（留空显示全部）"
            self.put(top + 3, left + 2, prompt, width=inner)
            displayed = "*" * min(len(dialog["value"]), inner - 2) if field == "url" else clean(dialog["value"])
            # Show the tail of long inputs without ever drawing the real URL.
            while displayed and cell_width(displayed) > inner - 2:
                displayed = displayed[1:]
            self.put(top + 4, left + 2, "> " + displayed, curses.A_REVERSE, inner)
            self.put(top + 6, left + 2, dialog.get("error", ""), self.color.get("error", 0), inner)
            self.put(top + box_height - 2, left + 2, "Enter 继续  Esc 取消  Ctrl+U 清空", width=inner)

    def render(self):
        self.size = self.screen.getmaxyx()
        self.geom = layout(*self.size)
        self.screen.erase()
        if self.geom["small"]:
            self.put(0, 0, "CLASH", curses.A_BOLD)
            self.text_lines(2, 0, "终端较小，请扩大至至少 30 列、18 行。q 退出。", self.size[1] - 1, max(0, self.size[0] - 4))
            if self.job:
                self.put(self.size[0] - 2, 0, "当前操作仍在进行。")
        else:
            self.header()
            self.navigation()
            self.content()
            self.footer()
            if self.dialog:
                self.render_dialog()
        self.screen.refresh()
        self.dirty = False

    def loop(self):
        self.setup()
        try:
            while self.running:
                self.poll()
                if self.screen.getmaxyx() != self.size:
                    self.dirty = True
                if not self.running:
                    break
                if self.dirty:
                    self.render()
                try:
                    key = self.screen.get_wch()
                except curses.error:
                    if terminal_closed():
                        raise TerminalClosed()
                    continue
                except KeyboardInterrupt:
                    key = "\x03"
                if self.dialog and self.dialog["kind"] == "help" and key not in (curses.KEY_RESIZE, "\x04"):
                    self.dialog = None
                    self.dirty = True
                    continue
                self.key(key)
        finally:
            # Even a lost SSH terminal must leave the worker to finish/roll back safely.
            if self.job:
                self.job.cancel()
                for number in (signal.SIGHUP, signal.SIGTERM):
                    signal.signal(number, signal.SIG_IGN)
                while True:
                    try:
                        self.job.process.wait()
                        self.job.poll()
                        break
                    except KeyboardInterrupt:
                        continue
                self.job = None
        return 0


def run(manager):
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("工作台需要交互终端。请在 SSH 终端运行 clash，或使用 clash help 查看命令。")
        return 2
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    previous = {}
    def close_terminal(_signal, _frame):
        raise TerminalClosed()
    for number in (signal.SIGHUP, signal.SIGTERM):
        previous[number] = signal.signal(number, close_terminal)
    try:
        return curses.wrapper(lambda screen: Workbench(screen, manager).loop())
    except TerminalClosed:
        return 0
    except curses.error:
        print("当前终端无法打开工作台。请检查 TERM 设置，或运行 clash menu --plain 使用纯文字菜单。", file=sys.stderr)
        return 2
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
