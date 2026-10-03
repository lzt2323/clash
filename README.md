# clash_linux

面向 Linux / SSH 终端的 Mihomo 代理管理工具。通过全屏工作台管理订阅、节点和模式，通过 `clash start` / `clash stop` 控制核心，通过 `clash on` / `clash off` 控制当前终端的代理环境。

## 快速开始

支持 Linux glibc 的 x86_64 / ARM64。安装包自带 Mihomo 和独立 Python，用户无需 Git、pip 或系统 Python。需要系统基础命令 Bash、curl、tar、gzip、sha256sum。暂不支持 Alpine / musl。

```bash
curl -fsSL https://download.getplus.dpdns.org/install.sh -o /tmp/clash-install.sh && bash /tmp/clash-install.sh
source ~/.local/share/clash-linux/env.sh
clash
```

安装器优先从下载服务器获取完整包，失败或 SHA256 校验不通过时尝试同版本 GitHub Release。默认安装到 `~/.local/share/clash-linux`，Python 位于其中的 `python/`，不修改系统 Python、pip、PYTHONPATH 或 Python 的 PATH。仅在当前用户的 shell 配置中加入 Clash 入口，并创建 `~/.local/bin/clash`。

若下载服务器无法访问，安装脚本也可从 GitHub 获取：

```bash
curl -fsSL https://github.com/lzt2323/clash/releases/download/v0.1.0/install.sh -o /tmp/clash-install.sh && bash /tmp/clash-install.sh --server ''
source ~/.local/share/clash-linux/env.sh
```

可用 `--prefix /absolute/path` 指定目录、`--shell zsh` 接入 Zsh、`--no-shell` 跳过 shell 配置。安装器拒绝覆盖无标记目录或其他 Clash 的命令入口。重复安装同版本只重新接入 shell；MVP 暂不支持跨版本升级。

首次安装不需要连接 GitHub，只要能访问主下载服务器即可。国内网络能否连接服务器和订阅提供方取决于实际线路；域名配置本身不保证可达。也可提前下载对应架构的完整包与校验值，在离线机器执行：

```bash
bash install.sh --archive clash-linux-amd64.tar.gz --sha256 <SHA256SUMS中的校验值>
```

离线安装能准备程序和核心；首次添加在线订阅仍需要能访问订阅地址。

第一次配置：按 `3` 进入订阅管理，按 `a` 添加订阅，输入名称并按回车，再粘贴订阅地址并按回车提交。地址只显示星号。首个订阅自动设为当前订阅。

按 `1` 选择节点或自动选择，按 `2` 选择规则、全局或直连；方向键浏览、回车应用。完成后按 `q` 退出工作台，开启当前终端代理：

```bash
clash on
clash status

# 用完后清除当前终端代理变量。
clash off
```

`clash on` 会先确保核心和控制接口就绪，成功后才设置代理环境。`clash off` 清除当前终端的代理变量，不会改变核心状态。其他 SSH 会话各自管理自己的环境变量。

## 卸载与重装

```bash
clash uninstall          # 停止本安装的核心，移除程序和 shell 入口，保留订阅与配置
clash uninstall --purge  # 同时清除本安装的订阅与配置
```

默认保留 `conf/` 和 `runtime/mvp/`，在原目录安装相同版本即可恢复。卸载只清理安装清单中的程序文件；未知文件保留。操作进行中或存在未完成事务时会拒绝卸载。其他已打开的 SSH 会话仍需自行清除代理变量。

## 工作台怎么用

顶栏显示核心、当前终端代理、当前订阅、模式和节点。宽屏使用左侧任务导航和右侧列表；窄屏把任务导航移到顶部。列表中的 `>` 表示光标，`*` 表示正在使用的项目，移动光标不会改变设置。

| 输入 | 操作 |
| --- | --- |
| `1` / `2` / `3` / `4` | 节点 / 模式 / 订阅 / 诊断 |
| `←` / `→`，或 `Tab` | 左键返回任务导航；右键进入所选任务内容；Tab 切换焦点 |
| `↑` / `↓`，或 `j` / `k` | 浏览；按 `Enter` 应用所选项目 |
| `Esc` | 取消输入、关闭弹窗、返回任务导航；忙时请求安全取消 |
| `s` / `x` | 启动核心 / 确认停止核心 |
| `?` | 查看键盘帮助 |
| `q` / `Ctrl+D` | 退出；有后台操作时等待安全结束 |

节点列表使用 `/` 搜索，`n` / `p` 或 `PageDown` / `PageUp` 翻页，`t` 手动测速当前订阅的全部节点（包含自动选择项），不受分页或搜索筛选影响。搜索按回车应用筛选，不会选择节点；清空搜索可恢复全部节点。

订阅列表中 `a` 添加、`Enter` 使用、`u` 更新、`e` 编辑地址、`d` 删除。可以保存多个订阅，每次使用一个。正在使用的订阅不能删除，先切到另一份订阅再删除；切回订阅时恢复该订阅之前选择的节点。地址输入隐藏，`Ctrl+U` 清空当前字段。

诊断页中 `r` 重新检测、`R` 确认重启核心，也可以选择项目后按回车。停止、重启和删除弹窗默认选中取消；用 `Tab` 切到确认后按回车，或按 `y` 确认。停止和重启会影响所有使用该核心的终端；从工作台停止后，退出并运行 `clash off` 清除当前终端的代理变量。

下载、测速和配置操作在后台执行，等待时仍可浏览任务和列表。取消会等操作到达安全边界：提交前可以取消，配置提交或核心启停已经开始时会完成并返回真实结果。

工作台适配 80 列和 40 列终端；至少需要 30 列、18 行。长名称按显示宽度截断，支持中文，不需要鼠标。设置 `NO_COLOR=1` 可关闭颜色。`TERM=dumb`、缺少 `curses` 或显式运行 `clash menu --plain` 时使用逐行文本菜单；文本菜单沿用数字操作和 `0` 返回。没有交互式终端时菜单退出并给出提示，脚本使用明确命令。

## 命令和作用范围

| 命令 | 作用 |
| --- | --- |
| `clash` / `clash menu` | 打开终端工作台 |
| `clash menu --plain` | 打开逐行文本菜单 |
| `clash start` | 启动核心，不修改当前终端代理变量 |
| `clash stop` | 停止核心；通过 shell 集成运行时也清除当前终端代理变量 |
| `clash restart` | 重启核心，不修改当前终端代理变量 |
| `clash on` | 确保内核就绪，并开启当前终端代理 |
| `clash off` | 清除当前终端代理变量 |
| `clash status` | 查看当前状态 |
| `clash shell-init --shell bash` | 安装 Bash 集成和用户级命令入口 |
| `clash shell-init --shell zsh` | 安装 Zsh 集成和用户级命令入口 |
| `clash uninstall [--purge]` | 卸载；默认保留数据，--purge 同时删除数据 |
| `clash help` | 查看帮助 |

安装后，`clash` 是当前 shell 中的包装函数，可以改变当前终端的环境变量。直接运行 `./clash` 是子进程，不能改变父 shell；未安装集成时可以临时执行：

```bash
eval "$(./clash on)"
eval "$(./clash off)"
```

`clash start` 启动核心后，其他程序可以显式连接本机代理端口。`clash on` 则同时确保核心就绪并设置当前 shell 的代理变量。`clash stop` 影响所有使用该核心的终端；其他 SSH 会话仍需各自执行 `clash off`。只有遵循 HTTP / HTTPS / SOCKS 代理环境变量的程序会使用这些设置。菜单中的模式影响已经接入核心的流量；当前版本不自动接管所有系统程序。

规则模式的默认规则是私网地址直连，其余流量交给所选代理；全局模式将已接入内核的流量交给所选代理；直连模式将已接入内核的流量直接访问。默认规则不包含完整的国内外网站分流规则库。

## 当前版本范围

本次终端 MVP 提供多订阅保存与单订阅使用、节点选择和恢复、规则/全局/直连模式、手动测速、状态和诊断。订阅下载与配置验证失败时保留已有状态；配置应用失败时回滚，避免把失败配置当作已生效。

新菜单的状态默认保存在 `runtime/mvp/`，节点缓存保存在 `conf/mvp-providers/`。删除订阅会移除列表中的记录，旧节点缓存暂时保留用于回退。订阅地址、节点凭据和控制密码属于本机私密数据，不要公开这些目录、订阅地址或包含 token 的截图。默认代理和控制接口面向本机使用。

项目保留旧命令与脚本用于兼容已有部署。新安装按本文流程使用新菜单。检测到已有 `conf/config.yaml` 时，首次添加会要求先备份并移走旧配置，不会直接覆盖它。迁移前先停止旧实例，并保留旧 `.env` 和配置备份。

创建新菜单状态后，`clash init/mode/switch` 会拒绝执行，避免旧命令造成配置漂移；`start/stop/restart` 使用新状态管理核心，配置操作使用工作台。旧脚本只用于旧部署，请勿与新菜单混用。

此阶段不包含 TUN、透明代理、开机自启、完整 DNS/规则编辑、流量和连接管理、局域网共享，也不承诺完整桌面客户端的所有功能。订阅必须是 Mihomo 可校验的 Provider/YAML 节点格式；复杂远程完整配置的规则、DNS 等设置不由本菜单导入。

## 开发验证

在 Linux 上运行离线测试：

```bash
python3 -B tests/terminal_mvp_test.py
python3 -B tests/terminal_menu_test.py
bash tests/run.sh
```

新测试使用临时目录和模拟的订阅下载、配置校验、核心调用，不访问真实订阅，也不启停真实代理服务。`tests/run.sh` 包含 worker JSON 协议与真实 PTY 工作台检查，验证全屏、40/80 列、焦点和键盘路由、隐藏地址、取消与终端恢复；可单独运行 `python3 -B tests/workbench_pty_test.py`。Linux CI 执行这些检查。离线检查不等同于真实 Linux 核心启动、订阅兼容性或实际代理网络验收。

真实内核集成测试使用本地模拟订阅和本地 HTTP 上游，运行隔离的 Mihomo 实例，不使用用户的订阅或服务：

```bash
MIHOMO_BINARY=/absolute/path/to/mihomo python3 -B tests/terminal_mvp_integration.py
```

2026-10-03 已在 Ubuntu 24.04 x86_64 服务器上完成生产运行脚本的真实集成测试：启动、重启、节点恢复、订阅切换、失败回退和本地代理流量均通过。另用三份真实订阅验证下载与解析、实际出站、出口变化、40 列终端菜单和父 shell 的 on/off 行为。测试使用独立目录和本机端口，结束后停止测试核心。Linux CI 也已配置相同的自动化检查，远端 CI 运行结果仍以其报告为准。

同日，新分栏工作台已完成 Ubuntu 24.04 的真实 80/40 列 PTY 验收：订阅浏览、停止默认取消、窗口缩放、退出和终端恢复均通过。进程命令验证了 start 不修改 shell 环境、on 后代理请求返回 HTTP 204、off 保留核心、stop 停止核心并可重复执行。测试前后的订阅状态文件一致，结束时恢复测试前的核心运行状态。Linux 聚合检查 60 项通过、0 失败，真实核心集成检查通过；GitHub CI 配置已更新，但尚未推送触发。

本轮实测修复了控制接口早于节点加载就绪的问题，并补充启动执行窗口、超时/中断清理和控制密码不出现在 curl 参数中的检查。离线 CLI 测试使用独立目录，不继承已有订阅状态。macOS 上的真实内核测试使用专用生命周期适配；Linux 实测使用生产脚本。

项目的目标运行环境为 Linux。macOS 系统 Bash 版本与 GNU 命令差异可能使旧 Linux 脚本无法直接在 macOS 上运行。
