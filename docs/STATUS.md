# 当前进度

## 2026-10-17 · README 终端 GIF 展示

### 已完成

- 首屏静态工作台改为从 Bash 输入 clash 开始的 GIF：打开工作台 → 查看订阅 → 选择香港 01 并保存 → q 返回 → clash on → clash status。保留小猫 logo、安装 Tip、常用 CLI 和首次添加订阅文字 / 图示。
- 新增 `docs/assets/terminal-demo.gif` 与静态 PNG。picture 的 prefers-reduced-motion 条件选择 PNG；HTML 分步指南继续提供手动步骤与暂停。
- 新增维护工具 `docs/terminal_demo.py`，复用集成测试的本地订阅服务，在 Linux 真实 Rust 0.5.0 与 Mihomo v1.19.29 中录制独立 HOME / 随机端口的 PTY；结束时关闭测试核心并清理临时安装。渲染仅依赖文档工具 Pillow / pyte，不增加应用运行依赖。

### 验证与集成

- 真实录制验证节点选择已提交、当前 shell 代理开启、status 显示运行中；Bash 退出码为 0。原始记录不含真实订阅、服务器地址、账号或私有路径，未加入 Git。
- GIF 全部帧可解码，1108 × 804，36 个合并帧，循环约 17.6 秒，144,386 字节；静态 PNG 格式与尺寸正确。已逐段检查命令输入、订阅页、节点高亮、返回 shell 和最终状态。
- Chrome 本地 README 确认 GIF 成功加载、中文可读；390 px 视口的页面宽度为 390，无横向溢出。picture 的 PNG 资源与媒体条件已检查，未修改系统动态效果偏好。
- GitHub main 已同步演示资产与 README，提交 `dc56ebc`。Chrome 已验证线上 GIF 完整加载，尺寸正确；GitHub 保留 picture 的减少动态效果条件与静态资源地址。
- HTML 资源链接、Python AST 语法、六个分镜、捕获结果与隐私扫描、Git diff 检查通过。仅修改文档 / 资产，未改 Rust 代码，未重复业务回归。
- 本轮 GIF 展示已完成，补齐本记录并合入本地 main。发行包仍为 0.5.0，不创建新 Release；后续交互变化时使用维护工具重新录制。

## 2026-10-17 · Releases 仅保留最新版

- 按用户要求，将 `rust-v0.5.0` 设置为 Latest，删除旧的 `v0.2.0`、`v0.1.1`、`v0.1.0` Release 及其发行附件；历史源码 tag 与提交保留。
- GitHub API 已确认 Releases 仅剩一个公开版本 `rust-v0.5.0`，Latest API 指向相同版本，4 个最新附件完整保留。
- 发布维护说明同步为仅保留最新公开 Release。本轮未修改业务代码，无需重复 Rust 测试；执行 API 状态断言与 Git diff 检查。
- 最新源码此前已同步远程 main，本次继续推送维护记录；旧版本清理任务已完成，无剩余事项。

## 2026-10-17 · GitHub Rust 发行已公开

### 已完成与验证

- GitHub 登录和仓库权限已确认；将已验证的 Rust 源码快照同步至 `lzt2323/clash` 的 main，公开提交 `d77c2f5`，保留远程旧历史，未强推。本地开发历史与历史发行仓库原有历史不同，使用独立干净 clone 同步源码。
- 已按用户授权发布 [Clash Linux 0.5.0](https://github.com/lzt2323/clash/releases/tag/rust-v0.5.0)，tag 指向上述源码；不覆盖旧 Python release。4 个资产为安装器、GNU 包、独立校验和与 SHA256SUMS。GitHub 资产 digest、大小与本地产物一致。
- 已在无 GitHub 认证的 HTTPS 下载中获取全部 4 个公开文件，与本地逐字节比较并核对 SHA-256，全部通过。GNU 包哈希保持 `be3f7fb32431fb2b0e73d1316a65673a4bf351757a1a32f9a53654dc8a2cb44a`。
- 将从公开链接取得的文件复制到测试服务器的独立 HOME，通过离线安装器安装真实 Mihomo 与管理程序。版本为 Clash Linux 0.5.0 / Mihomo v1.19.29；新 Bash 在 /tmp 能直接找到 clash，全部许可证保留。测试目录为既有隔离工作区下 `public-oneclick-OxFdOHlx`，未改动用户原安装。
- 服务器直连 GitHub 发生连接超时 / TLS 意外中断；随后使用服务器已有的本地代理，仅为测试进程设置 HTTPS_PROXY，在新隔离 HOME 完成公开脚本 → 在线下载 GNU 包与校验和 → 安装 / shell 集成 → 新 Bash 调用 clash 的全流程。管理程序为 0.5.0，核心复用已校验归档。成功目录为 `public-online-proxy-vBuT22lY`。未修改现有代理、安装或用户 shell；直连网络问题仍在。

### CI 与剩余事项

- 首轮 Actions `39010627500` 的未发布 x86_64 musl 试验包在核心安装时 SIGSEGV；ARM 被 fail-fast 取消。已将检查目标改为当前发行使用的 GNU，并保留 ARM64 GNU 检查。后续 [GNU CI 39010856006](https://github.com/lzt2323/clash/actions/runs/39010856006) 两组全部成功：x86_64 为 24 单元 + 27 集成 + 14 PTY，ARM64 为 24 单元 + 25 集成 + 14 PTY。CI 未设置可选离线核心归档，因此各跳过 1 项；ARM 另跳过仅适用 x86_64 安装器的 2 项。此前服务器验证的 66 项包含离线核心合约，无跳过。两组发行打包和许可证收集成功；YAML / Bash 语法和 Git diff 检查通过。决策见 DECISIONS.md。
- 当前公开安装包仍只支持已实测的 Linux x86_64 glibc。musl 问题待单独复现与修复；ARM 不能仅凭构建成功宣称已支持。
- README 已移除待发布说明，Linux 表格提供实际安装包 / SHA-256 链接；一键安装与常用 CLI 已上线。Chrome 已检查 GitHub 实际渲染的表格、Tip、命令与 12 项常用 CLI，截图已保存。相对链接、标题锚点、SVG XML 和 Git diff 检查通过。
- 公开安装后的新交互 Bash 在 /tmp 输入 clash，真实 PTY 已确认工作台标题与空订阅提示；按 q 返回 shell，Bash 退出码为 0。验证脚本先等待真实提示符，并移除 ANSI 控制序列后匹配文字。
- 本次公开发行与文档任务已完成。后续按实际需求排查 musl、评估 ARM64 正式安装包；当前不扩大发行平台承诺。

## 2026-10-17 · Rust 0.5.0 一键安装与常用 CLI

以下为公开发布前的实现与验证记录；当前发布结果以上节为准。

### 已完成

- README 将独立“订阅自动更新与备份”章节替换为 12 项常用 CLI 命令，突出 `clash on / off / status` 等日常操作。进阶维护保留在完整说明中；Linux 安装表下新增 GitHub TIP，说明默认目录、无需 sudo 和首次 source。
- 新增根目录 `install.sh`。默认从历史发行仓库 `lzt2323/clash` 的独立 `rust-v0.5.0` 下载 Linux x86_64 glibc 薄包，HTTPS / SHA-256 校验后调用 Rust 安装；支持本地包、显式校验和、自定义目录和已有 Rust 安装升级。
- 原生 `self-install --archive / --sha256` 复用受限解包和暂存安装，保留全部依赖许可证。原 self-install 用法兼容；版本升为 0.5.0，无新 Rust 依赖。
- 新增手动 GitHub release workflow，只对已有 tag 创建草稿；不自动创建 tag 或公开发布。维护说明见 `docs/RELEASING.md`。
- README 与演示版本同步 0.5.0。线上下载 URL 标明发布后才可用，未声称未发布链接已验证。

### 验证

- macOS Cargo fmt、Clippy all-targets（warnings 为错误）、24 项单元测试通过。macOS 集成 14 项通过，14 项 Linux 合约由服务器验证；PTY 14 项通过。
- Linux x86_64 的 24 项单元、28 项集成、14 项 PTY 全部通过，无跳过，共 66 项自动化合约。
- 新合约覆盖原生发行包安装及许可证保留、错误 SHA 拒绝、配置写入路径拒绝；Bash 离线安装、路径含空格、重复升级保留数据；真实本地 HTTPS 下载、校验失败和 HTTP / HTTPS→HTTP 重定向拒绝。
- 在服务器独立 HOME 实测一条命令安装管理程序、真实 Mihomo 与 Bash 集成；新 Bash 在 /tmp 能直接调用 clash 并显示 0.5.0。未改动现有用户安装与 shell。
- Bash 语法、两个 workflow 的 YAML 语法、发行包清单内全部文件哈希、SHA256SUMS、README 本地链接及锚点、Git diff 检查通过。Workflow 尚未在 GitHub 执行。
- Chrome 本地渲染检查 Linux 安装表、TIP、一键命令和常用命令表。该预览模拟 GitHub 提示块排版，不作为 GitHub 线上渲染证据。

### 发行与下一步

- 本地产物位于 `rust/dist/`：GNU 压缩包、独立 `.sha256`、install.sh 和 SHA256SUMS，均被 Git 忽略。
- 管理程序 3,810,520 字节，压缩包 1,819,615 字节。压缩包 SHA-256：`be3f7fb32431fb2b0e73d1316a65673a4bf351757a1a32f9a53654dc8a2cb44a`。
- 完整安装试验目录：`/data1/ftp_data/lanzetian/.clash-rust-mvp-20261017-6FeHRD/oneclick-full-qhdi4ff1`，独立目录权限 0700，不含真实订阅。
- GitHub CLI 当前未登录，已请求用户在本机执行 `gh auth login` 后回复；未推送、创建 tag 或公开发行。下一步同步源码与发行文件到 `lzt2323/clash`，公开对应 Rust release 后验证真实 URL，再移除 README 的待发布说明。
- 其他架构与 Alpine / musl 不在当前一键安装包的支持范围。

## 2026-10-17 · 小猫 logo 调整

- 按用户最新要求，改为只包含猫脸的图形 logo，撤下此前 ASCII 字符和 Linux 元素组合。
- 同步 SVG、240 × 240 PNG、README 替代文本、HTML 页头与首屏图片，以及项目和视觉规范。历史设计决策保留，并补充本次取代说明。
- Chrome 已检查矢量图和指南首屏显示，桌面页面无横向溢出；SVG XML、PNG 文件签名及尺寸、HTML 两处内嵌图与源文件一致性、旧字符图案清理和 Git diff 检查通过。
- 仅修改图形与文档，未修改业务代码，未运行业务回归测试。改动保存在本地 main，不推送或发布。

## 2026-10-17 · Clash Linux 产品文档与视觉指南

用户确认产品名为 **Clash Linux**。保留 `clash` 命令、现有默认安装路径和 0.4.0 版本；本轮临时命名改动已撤回。`clash-linux-rust-mvp` 继续作为服务器测试目录名。

### 已完成

- 根 README 按用户提供的 `README_GUIDELINE.md` 重写，面向预编译包使用者，覆盖安装、添加订阅、选节点、`clash on`、自动更新、备份与常见问题。开发构建命令保留在开发文档中。
- 设计 ASCII 小猫 logo，结合 Linux 风格胸腹、脚掌与终端提示符；提供 SVG 与 PNG。新增工作台总览和邻接首次任务的 SVG 教程图。
- `docs/guide.html` 提供 6 步首次配置和 4 步更新 / 备份教程，支持步骤选择、前后切换、播放 / 暂停、键盘操作和减少动态效果设置；内嵌 logo，无网络依赖，可离线打开。
- 新增 `docs/brand-guidelines.md`，记录产品命名、logo、颜色、字体和演示规则；公开资产使用示例节点，不包含真实订阅或服务器凭据。
- 补齐 CLI 帮助顶部的产品名与第 5 页说明。未修改 Rust 业务逻辑、数据格式、命令名或依赖。

### 本轮验证

- Python-Markdown 3.9 与 Chrome 本地渲染 README，检查首页 logo、总览、首次任务图与正文顺序；导航锚点可跳转。未将本地预览称为 GitHub 线上渲染。
- Chrome 验证 HTML 的全部 10 个步骤、左右键、播放与 Esc 暂停；无页面脚本错误。375 / 768 / 1024 / 1440 px 实际视口的 document scrollWidth 均等于视口宽度。
- 检查正文、按钮与终端主要颜色对，文字对比度均超过 4.5:1；SVG XML、相对文件链接、Markdown 标题锚点、HTML ID 和 Git diff 检查通过。
- 使用用户提供的真实订阅，在独立测试目录以真实 Bash / PTY 完成：打开 TUI → 隐藏输入添加 → 加载 6 个节点 → 选节点 → 查看设置 → q 退出 → `clash on / status / off / stop`。
- 实测状态显示核心运行、当前终端代理开启；订阅 URL 与访问令牌未出现在 PTY 输出中。现有安装和真实订阅状态未修改，未开启真实 cron 任务。
- 成功的私有测试目录为 `/data1/ftp_data/lanzetian/.clash-readme-20261017-6bytiqmr/`，权限 0700。测试核心已停止，未将配置或原始终端输出提交到仓库。
- Cargo fmt 通过。本轮为文档与帮助文字变更，未重复运行上一轮已验证的 63 项业务合约；本次新增证据是上述浏览器检查及真实订阅首次使用流程。

### 集成与剩余事项

- 已建立本地主分支 `main`，从基线 `a166887` 快进合并 `docs/clash-linux-product` 至 `717bff1`，包含此前 Rust 实现、订阅维护与备份、旧代码清理及本轮产品文档。已检查各功能分支均为 main 的祖先。
- 当前没有配置远程仓库，本轮不推送、不发布、不创建 tag。
- Rust 公开下载源仍未发布，README 如实说明；不使用旧 Python 下载链接或未验证的安装链接。ARM64 / musl 的实机验证和公开发行仍待后续授权与验证。

下方是此前实现、清理和部署的历史记录；当前产品文档与集成状态以本节为准。

## 2026-10-17 · 唯一 Rust 应用与仓库清理

开发分支：`refactor/rust-only`，基于已验证的 Rust 0.4.0（`b6c2663`）。

### 已完成

- 删除旧 Python / Shell 管理应用、旧安装和打包流程、旧测试、Dashboard、subconverter、预置旧核心、旧图片与重复校验文件。保留唯一 Rust 应用、当前测试、打包工具、核心校验和及许可证。
- 清理旧构建 / 许可证下载缓存、研究 checkout、本地 Cargo target；仅保留当前 Rust 薄发行包。项目工作目录约 25 MB，其中 Git 历史约 22 MB；未改写历史。
- 旧应用未提交源码与原 README / gitignore 已保存到仓库外 `../clash_linux-legacy-backup-20261017-133918/legacy-source.tar.gz`。旧 runtime 记录与可能的订阅数据移至该目录的 `runtime/`，未提交至 Git；恢复目录权限 0700，源码归档权限 0600。
- 保留 `rust/` 结构，更新根 README、项目范围、模块职责、架构决策和忽略规则；移除旧版发布 workflow。
- `output.rs` 统一公共显示与错误脱敏，CLI、文本菜单、维护任务不再依赖 TUI 的内部函数。重构提交：`3a8ece4`。
- 删除可选的 Python MVP 对照测试入口；保留真实流量行为测试。新增 worker 响应取消与脱敏断言。未新增 Rust 依赖。

### 当前行为

Rust 0.4.0 支持多订阅管理、节点选择与测速、三种模式、核心生命周期、TUI / 文本菜单、shell 集成、安装 / 升级 / 回滚 / 恢复 / 卸载。

允许确认删除当前或最后一个订阅；最后一项删除后停止核心、清除托管配置，之后可重新添加。第 5 页提供自动更新间隔、立即更新、更新前备份、快照恢复和保留份数。自动更新默认关闭，显式开启后通过当前用户 cron 每 5 分钟检查到期状态，不引入常驻进程。

更新失败保留原订阅数据并继续其他项。备份为 0600，恢复前创建当前快照；控制密钥变化时在事务内重启核心。实际安装的 `conf/`、`runtime/mvp/` 和 `bin/mihomo` 必须保留，不属于源码清理对象。

### 验证证据

| 检查 | 结果 |
| --- | --- |
| macOS Cargo fmt / Clippy（all-targets，warnings 为错误） | 通过 |
| Rust 单元测试（macOS 与 Linux x86_64） | 各 24 / 24 通过 |
| Linux 真实核心 / CLI / 安装集成测试 | 25 / 25 有通过证据：首轮通过 24 项，补齐归档参数后单独通过离线核心安装项 |
| macOS 无需 Linux 核心的集成测试 | 13 / 13 通过，另 12 项由 Linux 验证 |
| PTY 交互测试（macOS 与 Linux） | 各 14 / 14 通过 |
| Python 维护脚本编译、shell 模板语法、Git diff 检查 | 通过 |
| 清理范围、仓库外源码归档和旧订阅文件保留 | 通过 |
| 当前 Rust 薄包生成、清单与 SHA-256 | 通过 |

当前共有 63 项不同测试合约。真实核心测试使用本地 HTTP 订阅 / 代理与固定 Mihomo v1.19.29，无真实订阅；cron 测试使用隔离替身。

最新清理后发行包：`rust/dist/clash-linux-rust-x86_64-unknown-linux-gnu.tar.gz`，1,818,533 字节，管理程序 3,809,016 字节。SHA-256：`5bf50ced3ee900e5686b52087c044711c1ee75ffc9e67fb3f0d15055d47a77ef`。产物被 Git 忽略，无 Python 或 Mihomo。

### 服务器部署

用户退出旧菜单后，已将 `/data1/ftp_data/lanzetian/clash-linux-rust-mvp` 从 Rust 0.3.0 升级至 0.4.0，保留订阅与核心。部署使用前次已验证的 0.4.0 包（SHA-256 `876ae3b8375b69cb37c4311948bb5e91cc754a7644105f24cbddb9b7045efa33`）；本次纯职责重构的新构建在隔离测试目录验证，未再次覆盖真实安装。

- 升级后的完整订阅状态与升级前快照逐项一致；未下载真实订阅数据。
- 已将 root 的 shim 和 Bash 集成从旧 Python 安装改为上述 Rust 安装。旧 Python 安装及其数据仍保留在服务器，不在本次本地源码清理范围。
- 新交互 Bash 在 `/tmp` 执行 `clash`，已用真实 PTY 验证打开 Rust TUI、进入第 5 页、按 q 正常退出。
- 已保留升级前配置快照 `runtime/mvp/backups/backup-01792215281200293231-a76e6cfd.tar.gz` 和同级 `.clash-linux-rust-mvp.rust-previous` 回滚安装。
- 原 Bash 配置与 shim 备份位于隔离测试目录的 `shell-backup-v040/`。
- 当前已打开的旧终端需执行 `source /data1/ftp_data/lanzetian/clash-linux-rust-mvp/env.sh`；新 Bash 会话直接输入 `clash`。
- 服务器 crontab 可用且 cron 服务 active，未代用户开启自动更新。

服务器隔离测试目录：`/data1/ftp_data/lanzetian/.clash-rust-mvp-20261017-6FeHRD/`。

### 剩余事项与下一步

- Linux x86_64 GNU 已实机验证。ARM64 / musl 有 CI 构建配置，尚无实机运行证据。
- Rust 公开发行源尚未发布；支持离线升级及显式设置 `CLASH_RUST_RELEASE_URL` 的专用元数据。未推送、合并或创建 release tag。
- 已有回滚备份时，下一次升级要求先将它移到其他位置，避免覆盖。
- 后续按实际需求验证 ARM64 / musl，并在获得发布授权后配置 Rust 发行源。当前无需继续拆分安装或 TUI 模块。

0.3.0 的迁移对照、0.4.0 功能开发记录与原始源码保留在 Git 历史中。
