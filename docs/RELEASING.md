# Rust 发行

默认 GitHub 发行仓库：[lzt2323/clash](https://github.com/lzt2323/clash)。Rust 版本使用 `rust-vX.Y.Z` 独立 tag。按用户要求，Releases 只保留最新公开版本，并将其设置为 Latest；历史源码 tag 与 Git 提交保留。

## 准备与检查

1. 对齐 `rust/Cargo.toml`、`Cargo.lock`、`install.sh` 中固定的 tag，以及使用文档的版本。
2. 在 Linux x86_64 上完成单元、集成、安装器与 PTY 测试，按 `rust/README.md` 生成 GNU 包。
3. 收集以下文件，核对 SHA-256：

```text
install.sh
clash-linux-rust-x86_64-unknown-linux-gnu.tar.gz
clash-linux-rust-x86_64-unknown-linux-gnu.tar.gz.sha256
clash-linux-rust-x86_64-unknown-linux-gnu-offline.tar.gz
clash-linux-rust-x86_64-unknown-linux-gnu-offline.tar.gz.sha256
SHA256SUMS
```

不要把真实订阅、配置、服务器日志或凭据放入发行包。两种包均包含原始依赖许可证；精简包首次安装下载核心，完整离线包额外包含固定校验的 Mihomo 压缩文件及原始许可证。构建时获取核心，目标机器安装时不联网。CI 和发行工作流使用 `sudo unshare --net` 验证完整包的解压安装、归档安装、升级和损坏拒绝。

## GitHub 草稿

维护者明确批准推送和发行后，将已验证源码同步到仓库，并手动创建对应 Rust tag。进入 Actions → **Prepare Rust release draft**，输入已存在的 tag。Workflow 会核对版本、构建、测试和打包，然后使用 `--verify-tag --draft --latest=false` 创建草稿。它不会自动创建 tag，也不会直接公开发布。

检查草稿文件和版本后，由维护者发布并设置为 Latest。确认新发行文件完整后，按授权清理旧 Release。此后验证真实下载及 README 的一键命令，再移除 README 中“尚待发布”的说明。

当前已公开的 [0.5.0 发行版](https://github.com/lzt2323/clash/releases/tag/rust-v0.5.0) 安装入口：

```bash
curl -fsSL https://github.com/lzt2323/clash/releases/download/rust-v0.5.0/install.sh | bash
```

后续版本须先公开对应 Release 并上传 `install.sh`，再更新 README 安装地址。升级程序的在线 JSON 源是独立配置，未随一键安装器启用；现有用户可以重复运行上述安装器或使用 `clash upgrade --archive FILE --sha256 HASH`。
