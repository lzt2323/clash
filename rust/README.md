# Clash Linux · 使用与开发

独立 Rust 管理程序，当前版本 0.5.0。日常运行不需要 Python、Rust、Cargo、Node.js 或额外 TUI 动态库；Mihomo 保持独立。

## 一键安装

GitHub 发行仓库为 `lzt2323/clash`。`rust-v0.5.0` 公开发布后可使用：

```bash
curl -fsSL https://github.com/lzt2323/clash/releases/download/rust-v0.5.0/install.sh | bash
```

安装器仅支持 Linux x86_64 glibc，需要 Bash、curl、tar、gzip、sha256sum、getconf 等系统命令；无需 Python、jq 或编译工具。首次默认安装到 `~/.local/share/clash-linux-rust`，核心独立安装。当前终端按输出执行 `source`；以后新 Bash / Zsh 会话直接使用 `clash`。

自定义目录或使用本地发行包：

```bash
bash install.sh --prefix "$HOME/apps/clash-linux"
bash install.sh --archive /path/clash-linux-rust-x86_64-unknown-linux-gnu.tar.gz
```

本地安装默认读取压缩包旁的 `.sha256`，也可显式传入 `--sha256 HASH`。`--core /path/mihomo`、`--import-data /old/install`、`--skip-core` 仅用于新安装；`--no-shell` 可跳过 shell 集成。已安装时再次运行会复用现有升级 / 回滚机制，不覆盖已有回滚备份。

下载源可用 `CLASH_INSTALL_REPOSITORY=owner/repo` 切换 GitHub 仓库，或使用 `CLASH_INSTALL_BASE_URL=https://.../rust-v0.5.0` 指定包含同名包与校验文件的 HTTPS 目录。下载和重定向均要求 HTTPS；校验失败时不会执行包内程序。

原生程序也支持 `./clash self-install --archive FILE --sha256 HASH`，从经过校验的发行包安装全部许可证与元数据。安装器只负责下载和引导，配置与生命周期逻辑仍由 Rust 执行。

## 安装与运行

下载对应架构的 Rust 发行包，校验 SHA-256 并解压，在解压目录执行：

```bash
./clash self-install --prefix "$HOME/.local/share/clash-linux-rust"
```

安装器首次下载并校验固定版本 Mihomo，生成 shell 集成。当前窗口按输出的路径执行：

```bash
source ~/.local/share/clash-linux-rust/env.sh
clash
```

默认端口：HTTP 7890、SOCKS 7891、控制接口 9090，仅监听本机。已有其他 Clash 实例时请使用另一组端口，并在日常会话持续设置 `CLASH_HTTP_PROXY`、`CLASH_SOCKS_PROXY`、`MIHOMO_API_URL`；不要同时让两个管理程序操作同一数据目录。

可选安装参数：

- `--core /absolute/path/mihomo`：复用经过版本检查的本地核心。
- `--no-shell`：不修改 shell 配置；安装后可直接运行安装目录中的 `clash`。
- `--skip-core`：只安装管理程序，稍后运行 `clash install`。
- `--import-data /old/install`：显式导入原 MVP 的订阅、缓存和配置。来源须无进行中的菜单或事务；先停止旧核心，新安装再启动。原目录保持原样。

离线核心安装支持 `MIHOMO_ARCHIVE=/path/mihomo-linux-amd64-v1.19.29.gz`。固定压缩包必须通过内置 SHA-256 校验。`clash install` / `update` 在已有相同版本时复用，不重复下载。仅更新管理程序的发行包不含核心。

Bash/Zsh 的当前环境只能由 shell 改变；独立程序 `on/off` 输出环境脚本。未安装集成时使用 `eval "$(/path/to/clash on)"`。新 SSH 会话单独运行 `clash on`；`off` 保留核心运行，`stop` 影响所有依赖此核心的会话。

## MVP 交互

- 分栏工作台与原 MVP 使用相同的任务和快捷键。70 列以上为左右分栏，窄屏为顶部任务导航；最小 30 列、18 行。
- 左右键 / Tab 切换区域；导航区上下键即时预览，不执行后台操作；Enter 进入内容区，内容区 Enter 执行。
- `1` 节点：上下选择、`/` 搜索、`t` 全部测速（搜索不缩小测速范围）。
- `2` 模式：规则 / 全局 / 直连；规则默认私网直连，其他走代理。
- `3` 订阅：`a` 添加、Enter 使用、`u` 更新、`e` 编辑地址、`d` 删除；首个自动使用，后续添加不切换。删除当前订阅会切换到其他订阅；删除最后一项会停止核心、清空托管配置，之后可重新添加。确认默认取消。
- `4` 诊断：重新检测、确认后重启 / 停止。
- `5` 设置与备份：配置自动更新间隔、立即更新全部订阅、创建 / 恢复备份、切换更新前备份和保留份数。
- 全局 `s` 启动、`x` 停止、`r` 刷新、`?` 帮助、Esc 取消并返回导航、`q` 等待安全结束后退出。
- 删除 / 停止 / 重启确认默认取消。订阅 URL 隐藏输入，并通过 stdin 传递给工作进程，不放进命令参数。
- `clash menu --plain` 提供数字文本菜单；TERM=dumb 自动选择文本菜单。

配置和 `runtime/mvp/state.json` 保持原 MVP v1 格式，JSON 也是合法 YAML。订阅 YAML 由临时隔离的 Mihomo 校验。管理器使用内容寻址缓存、原子文件替换、操作锁和事务日志；提交前取消保留原配置，进入提交后完成或回退。核心 PID 必须匹配 `/proc/PID/exe`，不对其他进程发送停止信号。

## 自动更新与配置备份

```bash
clash subscription auto-update 6h   # 每 6 小时更新全部订阅
clash subscription auto-update off  # 关闭定时更新
clash subscription update          # 立即更新全部订阅
clash subscription status          # 设置及最近更新结果
clash backup create                # 输出生成的备份名称
clash backup list
clash backup restore backup-时间-标识.tar.gz
clash backup auto on               # 更新前自动备份，默认开启
clash backup keep 10               # 默认保留最近 10 份，范围 1–100
```

定时更新默认关闭。间隔支持 `30m` / `6h` / `1d`，裸数字表示小时，允许 5 分钟至 30 天。启用时写入当前用户 crontab 中属于本安装目录的独立标记块，保留其他任务；系统 cron 服务每 5 分钟执行一次到期检查，之后可关闭终端。此可选功能需要系统已有 `crontab` 命令和运行中的 cron 服务。手动更新、备份及其他功能无需 cron。调度语法参见 [crontab 手册](https://man7.org/linux/man-pages/man5/crontab.5.html)。

启用后首次更新在下一次检查时执行，随后按间隔更新；进程锁避免重复更新。某个订阅失败会保留该订阅原数据，并继续处理其他订阅，结果记录在 `runtime/mvp/update-status.json`。核心停止时更新只保存配置，不自动启动核心。卸载会移除本安装的定时任务并关闭自动更新设置。

备份保存于 `runtime/mvp/backups/`，包含订阅地址、节点选择、模式、控制密钥和引用的 provider 缓存，权限为 0600。恢复先备份当前配置，再校验并事务性应用备份；沿用当前监听端口。恢复涉及控制接口密钥变化时，会重启正在运行的核心。自动更新间隔和 cron 注册不随配置备份恢复，避免导入备份时意外启用系统任务。

完成 `shell-init` 后，新开终端在任意目录输入 `clash` 即进入 TUI，无需执行脚本。已有窗口先 `source /安装目录/env.sh`；`clash on` 还会启动核心并开启该窗口的代理。

## 管理程序升级

```bash
clash app-version
clash upgrade --check
clash upgrade --archive /path/clash-linux-rust-TARGET.tar.gz --sha256 HASH
clash rollback
clash recover
clash uninstall          # 保留 conf 和 runtime/mvp
clash uninstall --purge  # 删除当前安装及数据
```

Rust 发行源尚未公开发布。在线检查 / 升级可用 `CLASH_RUST_RELEASE_URL=https://.../latest.json` 配置专用元数据；不会使用现有 Python 发行源。元数据格式：

```json
{"format":1,"implementation":"rust","version":"0.5.0","assets":{"x86_64":{"url":"https://example.com/release.tar.gz","sha256":"64位SHA-256"}}}
```

升级保留配置、订阅、Mihomo 和运行状态。旧管理程序保存在安装目录同级的 `.安装目录名.rust-previous`；回滚使用当前数据。已有备份时下一次升级会要求先将备份移走，避免静默丢弃备份。恢复失败的版本保留在同级 `.clash-rust-upgrade-*.failed` 目录中，供检查。

Rust 安装标记独立于原 Python 标记；旧 Python 包不能作为 Rust 升级包直接覆盖。旧版 init、完整 profile 转换和 Web dashboard 不在本次终端 MVP 迁移范围内。

## 开发与验证

在 rust 目录中：

```bash
cargo build --locked --release
cargo fmt -- --check
cargo clippy --all-targets -- -D warnings
cargo test --locked
CLASH_RUST_BINARY="$PWD/target/release/clash" python3 -B tests/integration.py
CLASH_RUST_BINARY="$PWD/target/release/clash" python3 -B tests/pty_test.py
```

Linux 上设置 `MIHOMO_BINARY=/path/mihomo`，integration.py 会使用真实核心、本地订阅和本地代理测试流量、切换、恢复、取消及端口冲突；不会使用真实订阅。PTY 测试覆盖 14 项键盘、窄屏、隐藏输入、取消、终端恢复及设置页交互合约，执行真实 Rust 程序。

Python 仅为开发测试 / 打包工具，不进入发行包。测试包含 PATH 为空时运行状态、环境变量和真实核心的检查。

```bash
python3 package.py --binary target/release/clash --target x86_64-unknown-linux-gnu
```

打包包含 Rust 依赖原始许可证、版本及 SHA-256 清单，不包含 Python 或 Mihomo。维护者使用 musl 工具链可生成静态 Linux 管理程序；GNU 产物需要兼容的 glibc。ARM64 与 musl 的实机结果以仓库的 `docs/STATUS.md` 为准，不能仅根据 CI 配置宣称已验证。

## 源码职责

`manager.rs` 负责订阅与配置事务；`lifecycle.rs` 管理核心进程；`maintenance.rs` 处理定时更新；`backup.rs` 保存与恢复配置；`distribution.rs` 处理安装和升级。`ui.rs` 与 `plain.rs` 分别提供 TUI 和文本菜单；`output.rs` 统一显示文本及敏感信息过滤，CLI 和后台任务无需依赖 TUI 模块。

`target/` 和 `dist/` 均为可重新生成的构建产物。实际安装中的 `conf/`、`runtime/mvp/` 和 `bin/mihomo` 是配置、订阅、备份与核心文件，不属于旧源码清理范围。
