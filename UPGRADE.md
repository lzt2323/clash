# Linux 程序升级指南

v0.2.0 起支持完整程序升级、回滚与中断恢复。适用于带 `.clash-install.json` 的 Linux glibc amd64 / arm64 正式安装；无标记源码目录不自动覆盖。

## v0.1.0 / v0.1.1 首次升级

先退出所有正在使用该安装目录的 Clash 菜单，然后运行：

```bash
curl -fsSL https://download.getplus.dpdns.org/install.sh \
  -o /tmp/clash-install.sh && bash /tmp/clash-install.sh --upgrade
```

默认目录为 `~/.local/share/clash-linux`。使用自定义目录时追加 `--prefix /绝对路径`。安装器下载并校验新版包后，使用包内独立 Python 执行迁移，无需安装系统 Python；已有 shell 接入不变。

普通安装命令仍拒绝跨版本覆盖，必须显式指定 `--upgrade`；同版本重装仍只修复 shell 接入。

## 日常升级

```bash
clash app-version                       # 工作台版本
clash upgrade --check                   # 当前版本、最新版本、是否可升级
clash upgrade                           # 升级到当前稳定版本
clash upgrade --version v0.2.0           # 固定版本；相同版本直接返回
clash rollback                          # 恢复上一版程序，保留当前兼容数据
```

`clash version` 和 `clash update` 仍针对 Mihomo 核心，与完整程序升级分开。升级器拒绝用 `upgrade` 降级；回退使用 `rollback`。不会后台自动下载或安装。

回滚到 v0.1.x 后，旧程序没有 `upgrade` 命令，再次升级需使用上面的新安装器。若未来发行包更换独立 Python 版本，程序内升级会提示改用独立安装器，避免切换目录时破坏当前解释器的运行环境。

主站 `latest.json` 用于版本发现，失败时回退到 GitHub latest Release。版本确定后，安装包与 SHA256SUMS 均从同一固定版本目录获取，主源失败会尝试 GitHub 的同版本包。

## 保留与运行状态

- 保留订阅、节点选择、模式、`conf`、`runtime/mvp`、`.env`、日志和非程序清单中的用户文件，并保留文件权限。用户文件与新版程序冲突时拒绝升级。
- 拒绝安装树中的符号链接和特殊文件，避免迁移越过安装目录；遇到这种情况请先检查并处理对应文件。
- 先完成下载、摘要/清单/架构检查和程序预检，再进入切换。原来运行的核心会短暂停止并启动，原来停止的核心保持停止；核心启动和健康检查失败时恢复旧版。
- 安装、升级、卸载、菜单和状态修改协调使用安装目录外的锁；目录被占用时退出其他菜单、等待操作结束后重试。旧版菜单另做 Linux 进程检查。
- 手动回滚保留升级后新产生的兼容用户数据，不用历史备份覆盖当前订阅。当前仅支持数据格式 1；未来不兼容格式会拒绝迁移或回滚。

升级器保留一个上一版本目录：安装根目录的同级 `.目录名.previous`，其中含历史配置和订阅，应视作私密备份。新的成功升级/回滚会替换这个备份。普通卸载保留它；`clash uninstall --purge` 会在验证归属后将它与当前用户数据一并删除。

## 离线升级

从指定 Release 下载对应架构安装包，并从该 Release 的 SHA256SUMS 取得摘要：

```bash
clash upgrade --archive /path/clash-linux-amd64.tar.gz --sha256 <SHA256>
```

旧版首次离线升级使用新版安装器：

```bash
bash install.sh --upgrade --version v0.2.0 \
  --archive /path/clash-linux-amd64.tar.gz --sha256 <SHA256>
```

ARM64 使用 `clash-linux-arm64.tar.gz`。只用 GitHub 下载可添加 `--server ''`；镜像选项必须使用 HTTPS。

## 中断恢复

升级事务记录位于安装根目录同级的 `.目录名.upgrade.json`。尚未提交的切换恢复旧版本和原运行状态；已提交的切换完成备份收尾。发现未完成事务时，普通操作会提示恢复：

```bash
clash recover
```

如果进程被强杀或断电时安装目录恰好处于切换间隙，`clash` 可能暂时不存在。重新下载新版安装器，使用包内 Python 恢复：

```bash
bash /tmp/clash-install.sh --recover --prefix /原安装目录
```

不要删除事务记录、`.目录名.upgrade-work-*` 或 `.目录名.previous` 来绕过报错。恢复失败时保留这些文件，先检查磁盘、权限和核心日志。离线恢复同样可以追加 `--version`、`--archive` 和 `--sha256`。

## 发布维护

每次发布使用新版本号，保留旧版本安装包。GitHub Actions 在 AMD64 与 ARM64 原生 Linux 上构建，执行旧版安装、升级、回滚、核心运行和卸载验收后发布。`packaging/release_metadata.py` 从核对后的双架构包生成 `latest.json`。

下载服务器使用：

```bash
python3 packaging/mirror_release.py --version v0.2.0 \
  --web-root /srv/clash-downloads --backup-dir /安全路径/发布前入口备份
```

该工具核对 GitHub 资产摘要、安装包清单和 latest 元数据，先发布完整版本目录，再替换安装入口，最后发布 latest.json。已存在的版本目录不会覆盖；发布入口保留备份。

HTTPS 与 SHA-256 用于传输及文件完整性校验，目前没有独立签名信任链。后续可引入签名元数据、过期时间和防回退机制，参考 [TUF](https://theupdateframework.io/docs/overview/)。[GitHub Release API](https://docs.github.com/en/rest/releases/releases) 提供版本及资产元数据，[固定版本与 latest 链接](https://docs.github.com/en/repositories/releasing-projects-on-github/linking-to-releases) 用于发布和发现。
