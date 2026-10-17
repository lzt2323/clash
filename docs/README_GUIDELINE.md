# README Design and Writing Guidelines

## Purpose and scope

Create a concise product README that helps people understand the application, download it, and complete a first useful task.

For desktop and mobile applications, the default reader uses a compiled application. For libraries and developer tools, adapt the entry path to their actual audience. Do not apply a source-build onboarding pattern to an end-user application.

The twelve rules below include a requirement, an annotated mature-project reference, a negative example, a positive example, and an acceptance check. Treat the examples as concrete guidance, not a fixed template.

The negative PickDrop excerpts come from the README reviewed earlier on October 17, 2026 and the user's three supplied screenshots. They describe that reviewed version, not necessarily the latest working file. Positive PickDrop examples are proposed editorial rewrites, not quotations from the reference projects. Download placeholders illustrate layout only and must be replaced with verified links before publication.

## 1. Address the person using the application

**Requirement.** Build the main reading path around understanding, downloading, opening, and using the product. Put development prerequisites and source-build commands in contributor documentation. A terminal command that runs an already-installed CLI is a usage instruction; compiling its source is a development instruction.

**Mature reference and analysis.** [LocalSend's Download section](https://github.com/localsend/localsend#download) presents distribution channels separately from its later source-compilation instructions. This separation gives application users a relevant starting point. Borrow the separation, not the ambiguous use of “Getting Started” for compilation elsewhere in that README.

**Negative example — reviewed PickDrop README:**

> macOS / Windows：准备 Git、Node.js 24 或更新版本、pnpm 11.19.0、仓库指定的 Rust 工具链，以及 Tauri 对应系统的构建依赖。

Under a user-facing Quick Start, this turns trying a file-transfer app into setting up a development environment.

**Positive example — proposed rewrite:**

> 在需要互传的设备上安装拾传，打开应用后加入同一个群组。

Keep source instructions behind a separate “Developer Guide” link.

**Acceptance check.** Can an application user reach the first useful task without encountering a compiler, repository clone, SDK, or development dependency?

## 2. Write a short, professional introduction

**Requirement.** Establish the category, primary task, and essential operating condition in one or two sentences. Prefer precise nouns and direct verbs. Use a friendly tagline only when it adds to a factual description. Do not put a tour of every supported device into the opening paragraph.

**Mature reference and analysis.** [LocalSend's opening](https://github.com/localsend/localsend#readme) identifies the app, shared content, nearby devices, and local-network context together. [uv's opening](https://github.com/astral-sh/uv#readme) identifies its tool category immediately. Their useful pattern is fast classification; their adjectives and technical positioning are not wording to copy automatically.

**Negative example — reviewed PickDrop README:**

> 把电脑、手机和 Linux 终端加入同一个群：从 Mac 发一份文件，在 Windows 或手机上接收，也可以把服务器上的文件直接发到群里。文字和历史保存在本地，文件由接收端按需下载。

This combines an invitation, a platform tour, an example, storage behavior, and download semantics in one paragraph. The reader must extract the product definition.

**Positive example — proposed rewrite:**

> 拾传是一款局域网文件传输工具，支持在电脑、手机和 Linux 终端之间发送文件与文字。

Explain persistent groups and local history in the relevant feature sections.

**Acceptance check.** After reading the opening once, can someone name the product category, main task, and network context?

## 3. Describe features as useful actions

**Requirement.** Give each core feature a short label and one concrete user outcome. Keep implementation details, edge-case guarantees, and test assertions out of headline feature copy. Include them later when they affect a user's decision or action.

**Mature reference and analysis.** [bat's feature sections](https://github.com/sharkdp/bat#syntax-highlighting) associate named capabilities with corresponding examples. That pairing makes the capability inspectable. It does not require every product to adopt bat's technical vocabulary.

**Negative example — reviewed PickDrop README:**

> 确认之后再发送。选择、拖入或粘贴的附件先进入草稿。确认发送后才发布文件消息，发送期间切群不会改变目标。

The last sentence is a valid behavioral guarantee, but it reads like a regression-test requirement inside the main feature list.

**Positive example — proposed rewrite:**

> **确认发送**：文件拖入后先保存为草稿，检查群组后再发送。

Another feature can say:

> **按需接收**：在需要的设备上下载文件，接收后可打开、另存或分享。

**Acceptance check.** Does each feature explain a useful action without making the reader learn internal states, protocol terminology, or edge-case handling?

## 4. Make the platform table a download selector

**Requirement.** Use platform information to answer “Can I run it, which package do I choose, and where do I download it?” Include architecture, minimum operating-system version, and installation differences only when they help select or install a package. Link to verified distributions. Do not use client frameworks or build formats as substitutes for downloads.

**Mature reference and analysis.** [LocalSend's download and compatibility tables](https://github.com/localsend/localsend#download) connect platform choices to distribution channels and requirements. The useful organizing principle is the user's next action, not the application's implementation stack.

**Negative example — reviewed PickDrop README:**

| 平台 | 客户端 | 获取与运行 |
| --- | --- | --- |
| macOS | Tauri 桌面应用 | 从源码运行或构建桌面预览包 |
| Windows | Tauri 桌面应用 | 从源码运行或构建 NSIS 安装包 |
| Android | Tauri 手机应用 | 从源码运行或构建 ARM64 APK |

The table is organized, but none of its rows provides the expected download action. “Tauri” does not help someone choose a package.

**Positive example — proposed table structure:**

| 平台 | 下载 | 安装 |
| --- | --- | --- |
| macOS | [已核实的 Mac 安装包链接] | 按对应安装包的实际流程安装 |
| Windows | [已核实的 Windows 安装程序链接] | 打开安装程序，按向导完成安装 |
| Android | [已核实的 APK 链接] | 在手机上打开 APK 并安装 |

Add a Linux binary row only when an appropriate package is actually available. Replace every placeholder and confirm each installation instruction before publishing.

**Acceptance check.** Does every downloadable platform row contain a working destination and enough information to choose the right package?

## 5. Describe the version users can actually obtain

**Requirement.** Align the displayed version, feature claims, demonstration, and download destination with the same distributable release. Keep source version numbers, migration progress, CI distinctions, and platform acceptance logs in development or release documentation. Briefly mention a release limitation when it changes what users can obtain or use.

**Mature reference and analysis.** [Flameshot](https://github.com/flameshot-org/flameshot#installation) separates prebuilt-package installation from compilation. Its release and development-build links identify different acquisition routes. Borrow that explicit routing rather than turning release engineering into introductory copy.

**Negative example — reviewed PickDrop README:**

> 当前源码为 0.6.0-alpha.15，处于 Rust / Tauri 迁移预览阶段。以下是当前主线的实现范围，具体平台验收记录见工程状态。

The paragraph reports implementation progress before telling a user what application they can install.

**Positive example — proposed wording pattern:**

> 下载对应平台的已发布版本，更新内容见发布说明。

If a demonstrated preview genuinely has no published package, say once:

> 当前预览版尚未提供安装包，已发布版本见下载页。

Do not describe that older release as supporting unverified preview features. Do not invent a new release or redirect ordinary users to compilation to conceal a packaging gap.

**Acceptance check.** Do the showcased capabilities and package links describe a coherent, obtainable version?

## 6. Make Quick Start teach the first useful task

**Requirement.** Put installation before operational onboarding. After installation, teach one complete task through a short ordered sequence. Name the essential controls and finish with a visible result. Keep optional workflows and platform-specific branches outside the main sequence.

**Mature reference and analysis.** [FastAPI's example](https://github.com/fastapi/fastapi#example) follows creation, execution, and a concrete response check. Transfer that sequence of observable progress to application interactions; do not transfer its source-code setup to an application audience.

**Negative example — reviewed PickDrop README:**

```sh
git clone https://github.com/lzt2323/shichuan.git
cd shichuan
pnpm install --frozen-lockfile
pnpm start
```

These commands prepare a development build. They do not teach an installed-app user how to transfer a file.

**Positive example — proposed first-use tutorial:**

1. 在同一可信局域网内打开两台设备上的拾传。
2. 在第一台设备创建群组，邀请第二台设备加入并批准申请。
3. 将文件拖入群聊，点击 **发送**。
4. 在第二台设备接收文件，保存后打开。

> 第二台设备能打开收到的文件，即完成首次传输。

Retain one brief note that devices holding the file must stay reachable until reception finishes. Put detailed network troubleshooting in its own section.

**Acceptance check.** Does the sequence begin in an installed application and end with the user's intended outcome?

## 7. Allow explanatory animation without production commentary

**Requirement.** Use an explanatory animation when it communicates the main capability clearly. Live-device capture is not required. Do not replace an effective animation merely to make it a real recording. Do not automatically add “not a real recording,” production-method notes, timing disclaimers, or animation-source links to the product narrative.

Keep claims accurate: an explanatory animation must not be presented as a measured benchmark, an actual test result, or proof of unimplemented behavior. Avoid making those claims in the first place.

**Mature reference and analysis.** [Excalidraw's showcase](https://github.com/excalidraw/excalidraw#readme) communicates creative possibilities through visual output. [VHS](https://github.com/charmbracelet/vhs#readme) presents a generated animation as its product artifact. The transferable principle is to let the visual demonstrate the capability. VHS's source link serves its code-to-animation product; it is not a universal requirement for an app's hero animation.

**Negative example — reviewed PickDrop README:**

> 功能示意动画，非实机录屏。界面经过简化，时间经过压缩，不代表实测速度。流程为：加入同群 → 文件暂存 → 确认发送 → 各端确认接收、下载并校验 → 手机回复文字。Linux 展示的是终端客户端。

The caption repeats the visible story and explains production choices. No performance measurement is being offered, so this much commentary competes with understanding the product.

**Positive example — proposed caption:**

> 在电脑、手机和终端之间发送文件与文字。

Omit the caption when the animation and nearby introduction already communicate that clearly. Keep production details in the asset documentation if maintainers need them.

**Acceptance check.** Does the visual explain the capability without surrounding commentary that exists only to explain how the media was made?

## 8. Put tutorial animation beside the interaction it teaches

**Requirement.** Distinguish the overview demonstration from a task tutorial. Use a short teaching animation where a gesture, pairing sequence, or sequence of UI states is harder to understand through text alone. Place it next to the relevant instructions, after installation. Simple actions do not need animations, and tutorials do not all need recordings.

**Mature reference and analysis.** [Flameshot's Preview](https://github.com/flameshot-org/flameshot#preview) uses an animated usage example. [VHS's tutorial](https://github.com/charmbracelet/vhs#tutorial) places an output example alongside its creation steps. Borrow the proximity between instruction and demonstration, while selecting a task-sized example for your own interface.

**Negative example — structure derived from the reviewed PickDrop README:**

> One four-platform overview animation at the top, followed by written group-joining and file-transfer instructions with no visual attached to either task.

This is not a demand for more media everywhere. The gap appears when the introductory animation does not teach a specific interaction that new users struggle to perform.

**Positive example — proposed placement and storyboard:**

```text
## 开始使用

### 连接另一台设备
[Short tutorial animation]
创建群组 → 显示邀请入口 → 另一台申请加入 → 批准申请

### 发送文件
[Optional short tutorial animation, if the interaction needs it]
拖入文件 → 检查草稿 → 点击发送 → 接收端保存并打开
```

Use these as storyboard instructions, not literal placeholders in the published README. Pair the animation with short textual steps so it remains understandable without playback.

**Acceptance check.** Is every tutorial visual adjacent to the exact task it teaches, and does it show the controls or state changes needed to reproduce that task?

## 9. Use restrained, readable text hierarchy

**Requirement.** Use descriptive section headings, short paragraphs, concise feature labels, and numbered lists for actual sequences. Give each paragraph one purpose. Reserve bold text for essential actions or distinctions. Use a table when readers are choosing or comparing, not merely to make prose look structured.

**Mature reference and analysis.** [bat](https://github.com/sharkdp/bat#how-to-use) places short task descriptions next to matching commands. [FastAPI](https://github.com/fastapi/fastapi#example) divides its example into recognizable stages. Their hierarchy helps readers locate an action without reading a dense paragraph first.

**Negative example — reviewed PickDrop README:**

> 在需要的设备上接收。查看传输状态，下载后校验文件大小与 SHA-256，再打开、另存为或交给系统分享。

The sentence combines a user action, an internal integrity mechanism, and several follow-up actions. Its heading also functions as a sentence rather than a compact feature label.

**Positive example — proposed feature formatting:**

> **按需接收**：在需要的设备上下载文件，接收后可打开、另存或分享。

In the tutorial, use a numbered action such as “点击 **接收**，选择保存位置。” Explain automatic integrity checks only where useful; do not turn them into an extra manual task.

**Acceptance check.** Can readers find the relevant task by scanning headings and bold labels? Does formatting clarify the prose rather than compensate for an overloaded sentence?

## 10. Keep only decision-relevant limitations in the main path

**Requirement.** Keep conditions that determine whether users can install, connect, or use the app. Place each condition near the affected action. Move protocol explanations, migration implementation, acceptance records, and testing distinctions to linked technical documentation. Do not delete a material limitation merely to shorten the page.

**Mature reference and analysis.** [LocalSend's compatibility and setup sections](https://github.com/localsend/localsend#download) address installation and connection conditions. Its protocol details occupy a separate section. Borrow the separation between conditions for use and technical explanation.

**Negative example — reviewed PickDrop README:**

> 当前应用使用预览标识 app.pickdrop.tauri.preview 和隔离数据目录。升级、导入旧数据前请阅读迁移说明。

The identifier and directory implementation obscure the question an existing user needs answered: what happens to their previous data?

**Positive example — editorial pattern, conditional on verified migration behavior:**

> 升级前请阅读迁移指南，确认旧聊天记录和文件的保留方式。

For first use, keep the relevant operating condition concise:

> 仅用于可信局域网；当前传输未加密。接收完成前，请保持持有文件的设备在线。

Do not reuse this limitation wording for a different product without checking its behavior.

**Acceptance check.** Does every visible limitation help a reader decide, act, or preserve data? Is necessary technical depth available through a focused link?

## 11. Keep navigation focused on user destinations

**Requirement.** Prioritize download, first use, documentation, and help. Keep maintainer-oriented asset instructions and repository maps out of the hero navigation. Offer one useful alternative media format when needed; do not display every production asset as a competing destination.

**Mature reference and analysis.** [Excalidraw's opening links](https://github.com/excalidraw/excalidraw#readme) include the editor and documentation, offering a direct way to experience or learn the product. Adapt navigation to the actions your readers actually need.

**Negative example — reviewed PickDrop README:**

> 观看清晰版视频 · 18 秒 · 查看静态图 · 动画源码与制作说明

The first links serve viewers, but the production-source link serves maintainers. Giving them equal prominence makes the media block busier without improving first use.

**Positive example — proposed navigation:**

> 下载安装 · 开始使用 · 常见问题

Under the animation, retain only “观看视频” if the inline version needs a clearer alternative. Provide an accessible static or textual fallback without turning all asset formats into prominent navigation.

**Acceptance check.** Does each prominent link advance understanding, acquisition, usage, or help? Can a production-only link move to contributor documentation?

## 12. Review usefulness and accuracy before declaring completion

**Requirement.** Review the rendered document as a user journey. Verify package destinations, platform claims, version alignment, UI terminology, internal links, and media readability. Do not treat the presence of headings, tables, and a GIF as evidence that the README is good. Report verification to the requester without appending a development audit to the product copy.

**Mature reference and analysis.** [FastAPI's explicit response check](https://github.com/fastapi/fastapi#example) gives users an observable outcome. [Flameshot's separate installation and compilation sections](https://github.com/flameshot-org/flameshot#installation) distinguish trying the app from building it. These support two different checks: can the reader obtain it, and can they recognize success?

**Negative example — deliberately synthetic review statement:**

> The README includes an overview, animation, platform table, Quick Start, limitations, and a license. Therefore it meets the guideline.

This would pass the earlier reviewed document while overlooking its source-build onboarding, non-actionable platform table, and verbose media commentary.

**Positive example — required review questions:**

1. Can a reader explain the product after the opening?
2. Can they obtain a compatible published application?
3. Does first use start inside that application?
4. Are difficult interactions taught at the relevant step?
5. Can they recognize the completed task?
6. Does every prominent paragraph or link help them understand, decide, act, or get help?

If a package is unavailable or a link is unverified, report that gap accurately. Do not manufacture a download, claim an unrun test passed, or silently substitute compilation for app installation.

**Acceptance check.** All six questions have concrete answers, and the verification report separates checked facts from proposed wording or unverified items.

## Recommended composition for an application README

```text
Project name
Short positioning statement
Download / First Use / Help links
Main capability animation or showcase

Key features, when the overview needs supporting detail
Download and installation
First useful task, with a teaching animation where helpful
Common questions and important limitations
Support, contribution link, and license
```

For a small app, combine sections when that makes the page easier to use. Do not add a section just to match this outline. Source builds belong in the developer guide. A library or developer tool needs a different entry path, selected in Rule 1.

## How to apply the references

| Reference | Borrow | Do not copy automatically |
| --- | --- | --- |
| [LocalSend](https://github.com/localsend/localsend#readme) | Application-oriented downloads and compatibility | Every distribution channel, sponsor section, or source-build heading |
| [Flameshot](https://github.com/flameshot-org/flameshot#readme) | Usage preview and separate installation/compilation paths | Its extensive CLI and desktop-environment instructions |
| [FastAPI](https://github.com/fastapi/fastapi#example) | A complete task and explicit success condition | Developer setup as onboarding for an end-user application |
| [bat](https://github.com/sharkdp/bat#readme) | Specific capability labels paired with examples | Technical vocabulary irrelevant to the target audience |
| [Excalidraw](https://github.com/excalidraw/excalidraw#readme) | Meaningful visual output and a clear experience destination | Branding assets or a requirement for live recording |
| [VHS](https://github.com/charmbracelet/vhs#readme) | Animation that explains an output or task | Source-production commentary for unrelated applications |
| [uv](https://github.com/astral-sh/uv#readme) | Immediate category identification and supported claims | Performance promises without measurements |

References were inspected on October 17, 2026. The observations are scoped to the named patterns; a mature project's complete README is not a universal template. Explanations of why those patterns help are editorial analysis, not measured usability results.
