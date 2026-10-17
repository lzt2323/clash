# Clash Linux 视觉指导

产品名为 **Clash Linux**，终端命令为 `clash`。`clash-linux-rust-mvp` 仅是测试安装目录，不作为对外产品名。产品用途：在 Linux / SSH 终端管理订阅、节点与代理。

## Logo

[logo.svg](assets/logo.svg) 是矢量源文件，[logo.png](assets/logo.png) 可用于头像与不支持 SVG 的场景。图形是一只浅青色的小猫：竖耳、圆眼、胡须和微笑，搭配深色圆角底板。只保留小猫形象。

图标最小显示宽度 32 px，四周至少保留图标宽度 1/8 的空白。缩放时保持比例，不添加阴影、渐变或动画。浅色与暗色页面均保留原图的深色底板。

## 颜色与字体

| 用途 | 色值 | 使用位置 |
| --- | --- | --- |
| 终端底色 | `#101b24` | 图标、工作台演示 |
| 页面底色 | `#f4f2eb` | 使用指南 |
| 页面文字 | `#182b35` | 标题、正文 |
| 深青 | `#23646b` | 链接、主按钮 |
| 浅青 | `#7dd3d4` | 小猫、暗色终端内的当前任务 |
| 琥珀 | `#efbd70` | 按键提示 |
| 终端文字 | `#f2f4ed` | 暗色工作台正文 |

正文使用系统无衬线字体，终端与命令使用系统等宽字体。无需在线字体或图标服务。正文 16 px 起、行高 1.6；终端示意 14 px 起，移动端可上下排列。状态同时用文字说明，不能只用颜色。

## 文案与演示

开头说明产品类别和用途，随后给出获取、安装和首次使用。正文称“订阅”“节点”“当前终端代理”，不使用发行目录名代替产品名。运行时无需 Python / Rust / Cargo；开发依赖只出现在开发说明。

README 首屏展示终端 GIF，从输入 `clash` 开始，依次查看订阅、选择节点、退出工作台、开启当前终端代理和查看状态。关键结果停留，按键触发的界面变化保持即时，不添加缩放或滑入效果。使用系统减少动态效果偏好时显示静态 PNG；下方保留文字步骤和添加订阅教程。

[HTML 分步指南](guide.html) 默认手动前进，提供播放 / 暂停、上一步 / 下一步和键盘操作；开启减少动态效果时禁止自动播放。播放在焦点进入或页面离开前台时停止。

演示中的名称和节点是示例，不放真实订阅链接、访问令牌、服务器账号或地址。教程先展示按键，再展示状态变化；添加首个订阅后还需 `clash on` 才能开启当前终端代理。不得把示意数据称为性能测试结果。

## 资产维护

`assets/terminal-demo.gif` 是 README 首屏演示，`assets/terminal-demo.png` 是减少动态效果时的静态版本；`assets/first-use.svg` 是添加订阅教学图。HTML 使用同一组颜色与文案，无外部依赖，可离线打开。变更交互时应对照 `rust/src/ui.rs` 和 PTY 测试校核按键，再检查桌面、窄屏与键盘浏览。

维护者使用 [terminal_demo.py](terminal_demo.py) 重新录制和导出。`record` 在 Linux 中运行真实 Rust 程序、Mihomo 和现有测试的本地订阅替身，创建并清理独立 HOME 与随机端口；不读取真实安装或订阅。`render` 使用 Pillow 与 pyte 解析 PTY 输出，保存 GIF 和静态 PNG。两者仅为文档工具依赖，安装应用无需这些工具。

```bash
python3 docs/terminal_demo.py record --binary /path/clash --core /path/mihomo --output /tmp/clash-demo.json
python3 docs/terminal_demo.py render --capture /tmp/clash-demo.json --output docs/assets/terminal-demo.gif
```

导出默认使用 macOS 的 Menlo 和黑体；其他系统可通过 `--latin-font` / `--cjk-font` 提供本机等宽和中文字体。输出采用固定调色板，避免字形闪烁；发布前检查首帧、节点保存、代理开启、循环衔接与隐私信息。原始 PTY 记录保留在忽略的工作目录，不随仓库发布。
