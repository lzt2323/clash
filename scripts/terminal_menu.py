"""Small, dependency-free SSH menu for the terminal Clash workflow."""

import getpass
import re
import shutil
import sys
import unicodedata
import warnings


PAGE_SIZE = 8
MODE_NAMES = {"rule": "规则", "global": "全局", "direct": "直连"}


def _clean(value):
    text = str(value)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"https?://[^\s<>\"']+", "[地址已隐藏]", text, flags=re.I)
    return "".join(char for char in text if not unicodedata.category(char).startswith("C"))


def _width(text):
    return sum(
        0 if unicodedata.combining(char) else
        2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        for char in text
    )


def _columns():
    return max(4, shutil.get_terminal_size((80, 24)).columns)


def _clip(value, limit):
    text = _clean(value)
    if _width(text) <= limit:
        return text
    suffix = "..." if limit >= 3 else ""
    result = ""
    for char in text:
        if _width(result + char) > limit - len(suffix):
            break
        result += char
    return result + suffix


def _say(value=""):
    # Wrap by terminal cells rather than bytes or Python string length.
    for raw_line in str(value).split("\n"):
        line = ""
        for char in _clean(raw_line):
            if line and _width(line + char) > _columns():
                print(line, flush=True)
                line = ""
            line += char
        print(line, flush=True)


def _read(prompt="请输入编号"):
    _say(prompt)
    return input("> ").strip()


def _secret():
    _say("请输入订阅地址（输入内容隐藏）")
    # Never fall back to echoing a subscription token.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass("> ").strip()
        except getpass.GetPassWarning as exc:
            raise RuntimeError("终端不支持隐藏输入，请使用正常的 SSH 交互终端。") from exc


def _node_name(name):
    return "自动选择" if name == "AUTO" else name


def _active_subscription(state):
    return next((item for item in state.get("subscriptions", [])
                 if str(item.get("id")) == str(state.get("active"))), None)


def _header(state):
    _say()
    _say("Clash 终端菜单")
    _say("核心：" + ("就绪" if state.get("running") else "未运行或接口异常"))
    shell_proxy = state.get("shell_proxy")
    if isinstance(shell_proxy, str):
        shell_label = shell_proxy
    else:
        shell_label = "已设置" if shell_proxy else "未设置"
    _say("当前终端代理：" + shell_label)
    _say("当前订阅：" + str(state.get("name") or "未配置"))
    mode = str(state.get("mode") or "rule").lower()
    _say("模式：" + MODE_NAMES.get(mode, mode))
    _say("节点：" + str(_node_name(state.get("node") or "未选择")
                        if state.get("active") else "未选择"))


def _delay_label(value):
    if value is None:
        return "失败"
    if isinstance(value, dict):
        value = value.get("delay", value.get("error", "失败"))
    if isinstance(value, (int, float)):
        return str(value) + " ms" if value > 0 else "失败"
    return _clean(value)


def _nodes(manager):
    query = ""
    page = 0
    measured = {}
    while True:
        state = manager.status()
        subscription = _active_subscription(state)
        if not subscription:
            _say("请先在订阅管理中添加并使用订阅。")
            return
        nodes = ["AUTO"] + [name for name in subscription.get("nodes", [])
                            if name not in ("AUTO", "PROXY")]
        visible = [name for name in nodes
                   if query.casefold() in str(_node_name(name)).casefold()]
        pages = max(1, (len(visible) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages - 1)
        entries = visible[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        _say()
        _say("选择节点  第 %d/%d 页" % (page + 1, pages))
        if query:
            _say("搜索：" + query)
        current = state.get("node") or subscription.get("selected")
        for index, node in enumerate(entries, 1):
            marker = "[当前] " if node == current else ""
            label = "%d %s%s" % (index, marker, _node_name(node))
            _say(_clip(label, _columns()))
            if node in measured:
                _say("  延迟：" + _delay_label(measured[node]))
        if not entries:
            _say("没有匹配的节点。")
        _say("n 下一页 / p 上一页")
        _say("s 搜索 / t 全部测速 / 0 返回")
        choice = _read()
        if choice == "0":
            return
        if choice.lower() == "n":
            page = min(page + 1, pages - 1)
        elif choice.lower() == "p":
            page = max(0, page - 1)
        elif choice.lower() == "s":
            query = _read("输入搜索词，留空显示全部")
            page = 0
        elif choice.lower() == "t":
            if nodes:
                _say("正在测速全部节点，Ctrl+C 取消。")
                measured.update(manager.delays_all())
        elif choice.isdecimal() and 1 <= int(choice) <= len(entries):
            _say(manager.select(entries[int(choice) - 1]))
            return
        else:
            _say("无效选择，请输入本页编号或菜单命令。")


def _modes(manager):
    while True:
        _say("选择模式")
        _say("1 规则（按当前配置分流）")
        _say("2 全局（全部交给所选代理）")
        _say("3 直连（已接入的流量直接访问）")
        _say("模式只影响接入内核的流量。")
        _say("默认规则：私网直连，其他走代理。")
        _say("0 返回")
        choice = _read()
        if choice == "0":
            return
        if choice in ("1", "2", "3"):
            _say(manager.set_mode({"1": "rule", "2": "global", "3": "direct"}[choice]))
            return
        _say("无效选择，请输入 0 到 3。")


def _add_subscription(manager):
    name = _read("为订阅起一个名称（0 返回）")
    if name == "0":
        return
    if not name:
        _say("订阅名称不能为空。")
        return
    url = _secret()
    if not url:
        _say("订阅地址为空，已取消添加。")
        return
    _say("正在读取订阅，Ctrl+C 取消。")
    _say(manager.add(name, url))


def _subscription_detail(manager, subscription_id):
    while True:
        state = manager.status()
        item = next((subscription for subscription in state.get("subscriptions", [])
                     if str(subscription.get("id")) == str(subscription_id)), None)
        if not item:
            _say("订阅已不存在。")
            return
        active = str(state.get("active")) == str(subscription_id)
        _say()
        _say("订阅：" + str(item.get("name", "未命名")))
        _say("状态：" + ("正在使用" if active else "未使用"))
        _say("节点数：" + str(len(item.get("nodes", []))))
        _say("更新时间：" + str(item.get("updated") or "未更新"))
        _say("1 使用 / 2 更新")
        _say("3 编辑地址 / 4 删除 / 0 返回")
        choice = _read()
        if choice == "0":
            return
        if choice == "1":
            _say(manager.use(subscription_id))
        elif choice == "2":
            _say("正在更新订阅，Ctrl+C 取消。")
            _say(manager.update(subscription_id))
        elif choice == "3":
            url = _secret()
            if not url:
                _say("地址为空，已取消编辑。")
            else:
                _say(manager.edit(subscription_id, url))
        elif choice == "4":
            if active:
                _say("此订阅正在使用。请先使用其他订阅，再删除此订阅。")
                continue
            if _read("确认删除此订阅？输入 y 确认，其余取消").lower() == "y":
                _say(manager.remove(subscription_id))
                return
            _say("已取消删除。")
        else:
            _say("无效选择，请输入 0 到 4。")


def _subscriptions(manager):
    page = 0
    while True:
        state = manager.status()
        subscriptions = state.get("subscriptions", [])
        pages = max(1, (len(subscriptions) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(page, pages - 1)
        entries = subscriptions[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        _say()
        _say("订阅管理  第 %d/%d 页" % (page + 1, pages))
        for index, item in enumerate(entries, 1):
            active = str(item.get("id")) == str(state.get("active"))
            _say(_clip("%d %s%s" % (index, item.get("name", "未命名"),
                                   " [当前]" if active else ""), _columns()))
        if not subscriptions:
            _say("尚未添加订阅。首个订阅会自动使用。")
        _say("a 添加 / n 下一页 / p 上一页")
        _say("输入编号查看详情 / 0 返回")
        choice = _read()
        if choice == "0":
            return
        if choice.lower() == "a":
            _add_subscription(manager)
        elif choice.lower() == "n":
            page = min(page + 1, pages - 1)
        elif choice.lower() == "p":
            page = max(0, page - 1)
        elif choice.isdecimal() and 1 <= int(choice) <= len(entries):
            _subscription_detail(manager, entries[int(choice) - 1]["id"])
        else:
            _say("无效选择，请输入本页编号或菜单命令。")


def _diagnostics(manager):
    def check():
        _say("正在检查连接，Ctrl+C 取消。")
        try:
            for result in manager.diagnose():
                _say(result)
        except RuntimeError as exc:
            _say("检测未完成：" + str(exc))

    check()
    while True:
        _say()
        _say("连接诊断与恢复")
        _say("1 重新检测 / 2 重启核心")
        _say("3 停止核心 / 0 返回")
        choice = _read()
        if choice == "0":
            return
        if choice == "1":
            check()
        elif choice in ("2", "3"):
            if choice == "2":
                _say("重启核心会中断正在使用它的连接，也会影响其他终端。")
                prompt = "确认重启核心？输入 y 确认，其余取消"
            else:
                _say("停止核心会断开其他使用它的终端。")
                _say("停止后请退出菜单并运行 clash off，清理当前终端代理。")
                prompt = "确认停止核心？输入 y 确认，其余取消"
            if _read(prompt).lower() != "y":
                _say("已取消操作。")
                continue
            try:
                _say(manager.restart() if choice == "2" else manager.stop())
            except RuntimeError as exc:
                _say("操作未完成：" + str(exc))
        else:
            _say("无效选择，请输入 0 到 3。")


def run(manager):
    """Run an interactive menu. EOF exits everywhere; Ctrl+C cancels an operation."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        _say("菜单需要交互终端。请在 SSH 终端运行 clash，或使用 clash help 查看命令。")
        return 2
    try:
        while True:
            try:
                try:
                    state = manager.status()
                except RuntimeError as exc:
                    _say("状态暂不可读：" + str(exc))
                    state = {}
                _header(state)
                _say("1 节点 / 2 模式")
                _say("3 订阅 / 4 诊断 / 0 退出")
                choice = _read()
                if choice == "0":
                    _say("已退出菜单。")
                    return 0
                if choice == "1":
                    _nodes(manager)
                elif choice == "2":
                    _modes(manager)
                elif choice == "3":
                    _subscriptions(manager)
                elif choice == "4":
                    _diagnostics(manager)
                else:
                    _say("无效选择，请输入 0 到 4。")
            except KeyboardInterrupt:
                _say("\n已取消当前操作，返回主菜单。")
            except RuntimeError as exc:
                _say("操作未完成：" + str(exc))
    except EOFError:
        _say("\n输入已结束，已退出菜单。")
        return 0
