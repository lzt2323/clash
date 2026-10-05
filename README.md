# Clash Linux

在 Linux / SSH 终端里使用 Clash。**命令行快速开关代理，终端菜单管理订阅和节点。**

- 保存多个订阅，随时切换其中一个使用。
- 选择节点、全部测速，切换规则 / 全局 / 直连模式。
- 自带 Mihomo 和独立 Python，无需手动安装 Python，不影响系统环境。

## 安装

支持 Linux glibc（x86_64 / ARM64），暂不支持 Alpine。需要 Bash、curl、tar、gzip、sha256sum。

```bash
curl -fsSL https://download.getplus.dpdns.org/install.sh \
  -o /tmp/clash-install.sh && bash /tmp/clash-install.sh
```

安装成功后，在当前窗口加载并打开菜单：

```bash
source ~/.local/share/clash-linux/env.sh
clash
```

`source` 只需在安装后的当前窗口执行一次，以后新开 SSH 会话可直接使用 `clash`。

默认安装到 `~/.local/share/clash-linux`。安装包优先从下载服务器获取，失败时自动尝试 GitHub；主站无法访问时，可从 [GitHub Release](https://github.com/lzt2323/clash/releases/latest) 下载 `install.sh` 后运行。

当前发行版为 **v0.2.0**，支持保留配置和订阅升级、失败恢复及手动回滚。

## 升级已有安装

已经安装 v0.1.0 / v0.1.1 的用户，先退出 Clash 菜单，用新安装器完成首次升级：

```bash
curl -fsSL https://download.getplus.dpdns.org/install.sh \
  -o /tmp/clash-install.sh && bash /tmp/clash-install.sh --upgrade
```

自定义安装目录需追加 `--prefix /你的安装目录`。升级期间正在运行的核心会短暂重启；原本停止的核心保持停止。首次升级之后使用：

```bash
clash app-version       # 查看工作台版本
clash upgrade --check   # 只检查更新
clash upgrade           # 更新整个程序，保留订阅、节点选择和配置
clash rollback          # 恢复上一版本，保留当前兼容数据
```

`clash update` / `clash version` 仍只针对 Mihomo 核心。旧源码目录没有安装标记，升级器会拒绝覆盖。离线升级、故障恢复和备份说明见 [升级指南](UPGRADE.md)。

## 第一次使用

1. 运行 `clash`，按 `3` 进入订阅管理，再按 `a` 添加自己的订阅链接。
2. 按 `1` 选择节点，回车确认；需要切换模式时按 `2`。
3. 按 `q` 退出菜单，执行 `clash on`，即可让当前终端的 curl、Git 等命令使用代理。

菜单中用 **← → / Tab** 切换区域；在左侧按 **↑ ↓** 选择任务，右侧会立即显示对应内容，按 **→ / Tab / Enter** 进入内容区后，再用 **↑ ↓** 浏览、**Enter** 执行。`t` 测速当前订阅的全部节点，`?` 查看帮助。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `clash` | 打开终端菜单 |
| `clash on` | 启动核心并开启当前终端代理 |
| `clash off` | 关闭当前终端代理，核心继续运行 |
| `clash start` | 启动核心 |
| `clash stop` | 停止核心，并清除当前终端代理 |
| `clash restart` | 重启核心 |
| `clash status` | 查看状态 |

**每个新 SSH 会话需要单独执行 `clash on` 开启代理。** 本工具通过终端代理变量工作，不会自动接管所有系统流量。

## 卸载

```bash
clash uninstall          # 卸载程序，保留订阅与配置
clash uninstall --purge  # 同时删除订阅与配置
```

保留数据后，在原目录安装相同版本即可恢复使用。

---

[下载](https://download.getplus.dpdns.org) · [反馈问题](https://github.com/lzt2323/clash/issues) · [GPL-3.0 许可证](LICENSE)
