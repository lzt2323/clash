# Rust 发行

默认 GitHub 发行仓库：[lzt2323/clash](https://github.com/lzt2323/clash)。Rust 版本使用 `rust-vX.Y.Z` 独立 tag，不覆盖旧 Python 发行文件。

## 准备与检查

1. 对齐 `rust/Cargo.toml`、`Cargo.lock`、`install.sh` 中固定的 tag，以及使用文档的版本。
2. 在 Linux x86_64 上完成单元、集成、安装器与 PTY 测试，按 `rust/README.md` 生成 GNU 包。
3. 收集以下文件，核对 SHA-256：

```text
install.sh
clash-linux-rust-x86_64-unknown-linux-gnu.tar.gz
clash-linux-rust-x86_64-unknown-linux-gnu.tar.gz.sha256
SHA256SUMS
```

不要把真实订阅、配置、服务器日志或凭据放入发行包。原生程序的包包含原始依赖许可证；Mihomo 独立下载。

## GitHub 草稿

维护者明确批准推送和发行后，将已验证源码同步到仓库，并手动创建对应 Rust tag。进入 Actions → **Prepare Rust release draft**，输入已存在的 tag。Workflow 会核对版本、构建、测试和打包，然后使用 `--verify-tag --draft --latest=false` 创建草稿。它不会自动创建 tag，也不会直接公开发布。

检查草稿文件和版本后，由维护者发布。此后验证真实下载及 README 的一键命令，再移除 README 中“尚待发布”的说明。

当前 0.5.0 入口：

```bash
curl -fsSL https://github.com/lzt2323/clash/releases/download/rust-v0.5.0/install.sh | bash
```

该地址只有在对应 Release 公开并上传 `install.sh` 后可用。升级程序的在线 JSON 源是独立配置，未随一键安装器启用；现有用户可以重复运行上述安装器或使用 `clash upgrade --archive FILE --sha256 HASH`。
