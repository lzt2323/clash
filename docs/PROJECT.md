# Clash Linux

产品名为 **Clash Linux**，命令为 `clash`。`clash-linux-rust-mvp` 仅用于测试安装目录命名。

在 Linux / SSH 终端管理 Mihomo：多订阅、节点选择、测速、规则 / 全局 / 直连模式和当前 shell 的代理开关。

## 技术与目标

应用采用 Rust，Ratatui / Crossterm 提供终端交互，ureq / rustls 负责 HTTP，Serde 保存兼容的 JSON 状态。Mihomo 是独立代理核心。

- 发布预编译管理程序；目标机器无需 Python、Rust、Cargo 或前端构建环境。
- 保留 Mihomo 作为独立代理核心，并复用版本一致的本地核心。
- 保持分栏工作台、文本菜单、快捷键、订阅数据格式和代理环境变量语义兼容。
- 配置修改必须验证、原子保存，并支持失败和中断后的恢复；取消不能留下半完成配置。
- Rust 实现在 `rust/`，是仓库唯一应用实现。Python 仅用于开发测试与发行打包；Shell 模板仅用于修改当前终端环境。
- 不保存旧核心、Dashboard、subconverter 或旧构建产物；许可证和固定核心校验和须保留。
- 自动测试使用本地模拟订阅和代理，无需真实订阅；真实安装的升级须保留用户数据。

## 范围

日常 MVP 功能包括订阅增删改、更新和切换、节点搜索和选择、全部测速、模式切换、启动 / 停止 / 重启、连接诊断、shell 集成、核心安装，以及 Rust 发行包的安装和升级 / 回滚 / 卸载。

0.4.0 增加可确认删除当前 / 最后一个订阅、可选 cron 定时更新、更新结果记录、配置备份与恢复及保留份数设置。通过 shell 集成在任意目录输入 `clash` 进入 TUI；自动更新默认关闭，只有用户显式开启才注册系统任务。

旧版完整 YAML 的 `init` 和其他历史管理入口不作为迁移入口；现有数据须明确识别，禁止静默覆盖。

## 当前验证状态

Rust MVP 已在 Linux x86_64 上完成真实核心流量、终端交互、安装 / 升级 / 恢复验证。实现与使用说明见 `rust/README.md`，具体证据与未验证平台见 `docs/STATUS.md`。

## 产品文档与视觉

根 README 面向安装预编译包的终端使用者，首屏 GIF 从输入 `clash` 开始展示工作台、节点选择和当前终端代理；文字教程从添加订阅到 `clash on` 完成首次任务。完整使用与开发说明保留在 `rust/README.md`。Rust 0.5.0 已通过 GitHub Releases 公开分发，README 提供已校验的安装包与一键安装入口。

`docs/guide.html` 是离线分步教程，覆盖订阅、节点、当前终端代理、定时更新与备份。Logo 采用简洁的小猫图形；图形和配色规范见 `docs/brand-guidelines.md`。公开资产只使用示例节点，不包含真实订阅与服务器凭据。

## 一键安装与分发

0.5.0 提供根目录 `install.sh`：通过 `lzt2323/clash` 的 GitHub Releases 获取固定 Rust tag 的 Linux x86_64 glibc 薄包，校验后调用原生安装 / 升级。安装器不依赖 Python、jq 或 Rust 工具链。`self-install --archive / --sha256` 复用既有受限解包与暂存安装流程，安装全部许可证。

发行使用独立的 `rust-vX.Y.Z` tag。手动 release workflow 只针对已存在的 tag 创建草稿；不自动创建 tag 或公开发行。README 以常用 CLI 为日常入口，定时更新与备份放入完整说明。
