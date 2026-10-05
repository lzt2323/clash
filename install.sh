#!/usr/bin/env bash
# Install a verified, self-contained release without modifying the system Python.
set -euo pipefail
umask 077

VERSION=v0.1.1
SERVER=https://download.getplus.dpdns.org
GITHUB=https://github.com/lzt2323/clash/releases/download
PREFIX=${HOME:-}/.local/share/clash-linux
ARCHIVE=''
DIGEST=''
NO_SHELL=0
SHELL_NAME=${SHELL:-bash}
SHELL_NAME=${SHELL_NAME##*/}
case "$SHELL_NAME" in bash|zsh) ;; *) SHELL_NAME=bash ;; esac
TXN=''
LOCK=''
KIND=''
COMMITTED=0
NEW_TARGET=0
OLD_TARGET=0
SHELL_TOUCHED=0
ENV_TOUCHED=0
RC_EXISTED=0
SHIM_EXISTED=0
ENV_EXISTED=0
LOCAL_EXISTED=0
BIN_EXISTED=0

die() { printf '错误：%s\n' "$*" >&2; exit 1; }
quote() { local text=$1; text=${text//\'/\'\\\'\'}; printf "'%s'" "$text"; }
usage() {
    cat <<'EOF'
用法：bash install.sh [选项]
  --prefix DIR        安装到指定目录（默认 ~/.local/share/clash-linux）
  --version VERSION   固定发行版本（默认 v0.1.1）
  --server HTTPS_BASE 主下载源；--server '' 只用 GitHub
  --github HTTPS_BASE GitHub Release 下载基址
  --archive FILE --sha256 HASH  使用已下载的离线包
  --shell bash|zsh     接入当前用户的指定 shell
  --no-shell          不修改 shell 配置或命令入口
EOF
}

while (($#)); do
    case "$1" in
        --prefix|--version|--server|--github|--archive|--sha256|--shell)
            (($# >= 2)) || die "选项 $1 缺少参数"
            option=$1 value=$2; shift 2
            case "$option" in
                --prefix) PREFIX=$value ;; --version) VERSION=$value ;;
                --server) SERVER=$value ;; --github) GITHUB=$value ;;
                --archive) ARCHIVE=$value ;; --sha256) DIGEST=$value ;;
                --shell) SHELL_NAME=$value ;;
            esac ;;
        --no-shell) NO_SHELL=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知选项：$1" ;;
    esac
done

[[ -n ${HOME:-} && "$HOME" == /* && "$HOME" != / ]] || die 'HOME 必须是当前用户的家目录'
[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][a-zA-Z0-9.-]+)?$ ]] || die '版本格式应为 v0.1.0'
[[ "$SHELL_NAME" == bash || "$SHELL_NAME" == zsh ]] || die '仅支持 --shell bash 或 zsh'
[[ "$(uname -s)" == Linux ]] || die '一键包仅支持 Linux glibc 系统'
case "$(uname -m)" in x86_64|amd64) ARCH=amd64 ;; aarch64|arm64) ARCH=arm64 ;; *) die '仅支持 Linux amd64 / arm64' ;; esac
libc=''
if command -v getconf >/dev/null 2>&1; then libc=$(getconf GNU_LIBC_VERSION 2>/dev/null || true); fi
if [[ "$libc" != glibc* ]] && command -v ldd >/dev/null 2>&1; then libc=$(ldd --version 2>&1 || true); fi
[[ "$libc" == *glibc* || "$libc" == *GLIBC* || "$libc" == *'GNU libc'* ]] || die '需要 glibc；暂不支持 Alpine / musl'
for command_name in tar gzip sha256sum mktemp mkdir mv cp rm chmod cat sort uniq tr; do
    command -v "$command_name" >/dev/null 2>&1 || die "缺少基础命令：$command_name"
done
[[ -n "$PREFIX" && "$PREFIX" != *$'\n'* && "$PREFIX" != *$'\r'* ]] || die '安装路径不能为空或包含换行'
[[ "$PREFIX" == /* ]] || PREFIX="$PWD/$PREFIX"
PREFIX=${PREFIX%/}
[[ -n "$PREFIX" && "$PREFIX" != / && "${PREFIX##*/}" != . && "${PREFIX##*/}" != .. ]] || die '不能安装到根目录或点目录'
[[ ! -L "$PREFIX" ]] || die '安装目录不能是符号链接'
parent=${PREFIX%/*}; [[ -n "$parent" ]] || parent=/
name=${PREFIX##*/}
mkdir -p -- "$parent"
parent=$(cd -- "$parent" && pwd -P)
PREFIX="$parent/$name"
[[ "$PREFIX" != "$HOME" ]] || die '不能直接安装到家目录根'
if [[ -e "$PREFIX" ]]; then
    [[ -d "$PREFIX" && ! -L "$PREFIX" ]] || die '目标不是实体目录，拒绝覆盖'
    [[ -f "$PREFIX/.clash-install.json" || -f "$PREFIX/.clash-data.json" ]] || die '目标已有文件但没有本项目安装/数据标记，拒绝覆盖'
    [[ ! -L "$PREFIX/.clash-install.json" && ! -L "$PREFIX/.clash-data.json" ]] || die '安装标记不能是符号链接'
fi
LOCK="$parent/.${name}.install-lock"
mkdir -- "$LOCK" 2>/dev/null || die '同一路径的安装正在进行，或存在安装锁；请先检查'

restore_file() {
    local destination=$1 backup=$2 existed=$3
    rm -f -- "$destination" || return 1
    if ((existed)); then cp -p -- "$backup" "$destination"; fi
}
cleanup() {
    local code=$? rollback_failed=0
    trap - EXIT HUP INT TERM
    set +e
    if ((!COMMITTED)) && [[ -n "$TXN" ]]; then
        if ((SHELL_TOUCHED)); then
            restore_file "$RC" "$TXN/rc.backup" "$RC_EXISTED" || rollback_failed=1
            restore_file "$SHIM" "$TXN/shim.backup" "$SHIM_EXISTED" || rollback_failed=1
            ((BIN_EXISTED)) || rmdir -- "$HOME/.local/bin" 2>/dev/null || true
            ((LOCAL_EXISTED)) || rmdir -- "$HOME/.local" 2>/dev/null || true
        fi
        if ((ENV_TOUCHED)) && [[ "$KIND" == installed ]]; then
            restore_file "$PREFIX/env.sh" "$TXN/env.backup" "$ENV_EXISTED" || rollback_failed=1
        fi
        if ((NEW_TARGET)); then rm -rf -- "$PREFIX" || rollback_failed=1; fi
        if ((OLD_TARGET)); then mv -- "$TXN/old" "$PREFIX" || rollback_failed=1; fi
    fi
    if ((rollback_failed)); then
        printf '错误：自动回滚未能完成，原数据及 shell 备份保留在：%s\n' "$TXN" >&2
        code=1
    else
        [[ -z "$TXN" ]] || rm -rf -- "$TXN"
    fi
    [[ -z "$LOCK" ]] || rmdir -- "$LOCK" 2>/dev/null || true
    exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
TXN=$(mktemp -d "$parent/.clash-install.XXXXXX")
FILE="clash-linux-$ARCH.tar.gz"

verify_hash() {
    local actual
    [[ "$2" =~ ^[a-fA-F0-9]{64}$ ]] || return 1
    actual=$(sha256sum -- "$1"); actual=${actual%% *}
    [[ "$actual" == "$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')" ]]
}
checksum_for() {
    local hash member extra found=''
    while read -r hash member extra; do
        member=${member#\*}
        if [[ "$member" == "$FILE" && "$hash" =~ ^[a-fA-F0-9]{64}$ && -z "$extra" ]]; then
            [[ -z "$found" ]] || return 1
            found=$hash
        fi
    done <"$1"
    [[ -n "$found" ]] || return 1
    printf '%s' "$found"
}
download() {
    curl --fail --location --silent --show-error --connect-timeout 10 --max-time 300 \
        --proto '=https' --proto-redir '=https' --output "$2" "$1"
}
fetch_source() {
    local base=$1 hash
    rm -f -- "$TXN/SHA256SUMS" "$TXN/package.tar.gz"
    download "$base/SHA256SUMS" "$TXN/SHA256SUMS" || return 1
    hash=$(checksum_for "$TXN/SHA256SUMS") || return 1
    download "$base/$FILE" "$TXN/package.tar.gz" || return 1
    verify_hash "$TXN/package.tar.gz" "$hash"
}
if [[ -n "$ARCHIVE" ]]; then
    [[ -f "$ARCHIVE" && "$DIGEST" =~ ^[a-fA-F0-9]{64}$ ]] || die '离线安装需要 --archive FILE --sha256 HASH'
    cp -- "$ARCHIVE" "$TXN/package.tar.gz"
    verify_hash "$TXN/package.tar.gz" "$DIGEST" || die '离线包 SHA256 校验失败，未修改安装目录'
else
    [[ -z "$DIGEST" ]] || die '--sha256 只能与 --archive 一起使用'
    command -v curl >/dev/null 2>&1 || die '在线安装需要 curl'
    for base in "$SERVER" "$GITHUB"; do
        [[ -z "$base" || "$base" == https://* ]] || die '下载源必须使用 HTTPS'
        [[ "$base" != *$'\n'* && "$base" != *'?'* && "$base" != *'#'* ]] || die '下载基址格式错误'
    done
    fetched=0
    if [[ -n "$SERVER" ]]; then
        printf '正在从主下载源获取 %s / %s...\n' "$VERSION" "$ARCH"
        if fetch_source "${SERVER%/}/releases/$VERSION"; then fetched=1; fi
    fi
    if ((!fetched)); then
        printf '主源不可用或校验失败，尝试同版本 GitHub Release...\n'
        [[ -n "$GITHUB" ]] && fetch_source "${GITHUB%/}/$VERSION" || die '所有下载源均不可用或校验失败'
    fi
fi

tar -tzf "$TXN/package.tar.gz" >"$TXN/members" || die '压缩包损坏，未修改安装目录'
tar -tvzf "$TXN/package.tar.gz" >"$TXN/types" || die '无法读取压缩包成员'
while IFS= read -r member; do
    [[ "$member" =~ ^clash-linux(/[a-zA-Z0-9._+@=-]+)*/?$ ]] || die '压缩包包含不安全或不支持的成员路径'
    [[ "/${member%/}/" != *'/../'* && "/${member%/}/" != *'/./'* ]] || die '压缩包包含路径穿越'
    case "$member" in
        clash-linux/conf|clash-linux/conf/*|clash-linux/runtime|clash-linux/runtime/*|clash-linux/.env|clash-linux/.clash-install.json|clash-linux/.clash-data.json)
            die '发行包不得包含用户配置、运行数据或安装标记' ;;
    esac
done <"$TXN/members"
while IFS= read -r entry; do
    [[ "${entry:0:1}" == - || "${entry:0:1}" == d ]] || die '压缩包包含链接或特殊文件，拒绝解包'
done <"$TXN/types"
sort "$TXN/members" | uniq -d >"$TXN/duplicates"
[[ ! -s "$TXN/duplicates" ]] || die '压缩包包含重复成员'
mkdir -- "$TXN/unpack"
tar -xzf "$TXN/package.tar.gz" -C "$TXN/unpack" --no-same-owner --no-same-permissions || die '解包失败'
candidate="$TXN/unpack/clash-linux"
for required in clash scripts/shell_integration.sh python/bin/python3 bin/mihomo; do
    [[ -f "$candidate/$required" && -x "$candidate/$required" && ! -L "$candidate/$required" ]] || die "发行包缺少可执行文件：$required"
done
[[ -f "$candidate/LICENSE" && -f "$candidate/BUILD.json" ]] || die '发行包缺少许可证或构建清单'
PYTHON="$candidate/python/bin/python3"
"$PYTHON" -E -s -B -c 'import ssl, curses, fcntl' || die '包内 Python 运行检查失败；需要兼容的 Linux glibc 系统'
KIND=$("$PYTHON" -E -s -B - "$candidate" "$PREFIX" "$VERSION" "$ARCH" <<'PY'
import json, pathlib, sys
candidate, prefix, version, architecture = sys.argv[1:]
candidate, prefix = pathlib.Path(candidate), pathlib.Path(prefix)
try:
    build = json.loads((candidate / 'BUILD.json').read_text())
    assert build['version'] == version and build['architecture'] == architecture
    files = build['files']
    assert isinstance(files, list) and len(set(files)) == len(files)
    actual = {str(p.relative_to(candidate)) for p in candidate.rglob('*') if p.is_file()}
    assert set(files) == actual - {'BUILD.json'}
    if prefix.exists():
        markers = [name for name in ('.clash-install.json', '.clash-data.json') if (prefix / name).is_file()]
        assert len(markers) == 1
        record = json.loads((prefix / markers[0]).read_text())
        assert record['format'] == 1 and record['prefix'] == str(prefix)
        if record['version'] != version:
            sys.exit('错误：已安装/保留的数据属于其他版本；MVP 暂不支持升级，请保留原目录。')
        print('installed' if markers[0] == '.clash-install.json' else 'preserved')
    else:
        print('new')
except (AssertionError, KeyError, TypeError, ValueError, OSError):
    sys.exit('错误：构建清单或安装标记无效，拒绝覆盖。')
PY
) || die '包或目标目录的元数据检查失败'

shell_preflight() {
    local content expected line inside=0 block=''
    RC="$HOME/.$SHELL_NAME"; [[ "$SHELL_NAME" == bash ]] && RC="$HOME/.bashrc" || RC="$HOME/.zshrc"
    SHIM="$HOME/.local/bin/clash"
    for path in "$HOME/.local" "$HOME/.local/bin"; do
        [[ ! -L "$path" && (! -e "$path" || -d "$path") ]] || die "shell 命令目录不安全：$path"
    done
    [[ -d "$HOME/.local" ]] && LOCAL_EXISTED=1
    [[ -d "$HOME/.local/bin" ]] && BIN_EXISTED=1
    for path in "$RC" "$SHIM"; do
        [[ ! -L "$path" && (! -e "$path" || -f "$path") ]] || die "shell 入口不是普通文件：$path"
    done
    if [[ -f "$SHIM" ]]; then
        expected=$(printf '#!/usr/bin/env sh\nexec %s "$@"\n' "$(quote "$PREFIX/clash")")
        [[ "$(cat -- "$SHIM")" == "$expected" ]] || die '现有 clash 命令来自其他安装；请先处理冲突或使用 --no-shell'
        SHIM_EXISTED=1; cp -p -- "$SHIM" "$TXN/shim.backup"
    fi
    if [[ -f "$RC" ]]; then
        RC_EXISTED=1; cp -p -- "$RC" "$TXN/rc.backup"
        while IFS= read -r line || [[ -n "$line" ]]; do
            if [[ "$line" == '# >>> clash_linux shell integration >>>' ]]; then
                ((inside == 0)) || die 'shell 配置中的项目标记重复或损坏'
                inside=1; block=''
            elif [[ "$line" == '# <<< clash_linux shell integration <<<' ]]; then
                ((inside == 1)) || die 'shell 配置中的项目标记损坏'
                [[ "$block" == *"$(quote "$PREFIX/scripts/shell_integration.sh")"* ]] || die 'shell 已接入其他 Clash 目录，请先处理冲突或使用 --no-shell'
                inside=0
            elif ((inside)); then block+="$line"$'\n'; fi
        done <"$RC"
        ((inside == 0)) || die 'shell 配置中的项目标记缺少结束位置'
    fi
}
((NO_SHELL)) || shell_preflight

if [[ "$KIND" == installed ]]; then
    [[ -x "$PREFIX/clash" && -x "$PREFIX/python/bin/python3" ]] || die '现有安装不完整，请保留目录并检查'
    [[ ! -L "$PREFIX/env.sh" && (! -e "$PREFIX/env.sh" || -f "$PREFIX/env.sh") ]] || die '现有 env.sh 不是普通文件'
    if [[ -f "$PREFIX/env.sh" ]]; then ENV_EXISTED=1; cp -p -- "$PREFIX/env.sh" "$TXN/env.backup"; fi
    ENV_TOUCHED=1
    printf '相同版本已安装，仅重新接入 shell；配置和运行数据保持不变。\n'
else
    if [[ "$KIND" == preserved ]]; then
        for entry in "$PREFIX"/.[!.]* "$PREFIX"/..?* "$PREFIX"/*; do
            [[ -e "$entry" || -L "$entry" ]] || continue
            [[ "${entry##*/}" != .clash-data.json ]] || continue
            [[ ! -e "$candidate/${entry##*/}" && ! -L "$candidate/${entry##*/}" ]] || die '保留目录含有与发行包冲突的文件，拒绝覆盖'
            case "${entry##*/}" in conf|runtime) [[ -d "$entry" && ! -L "$entry" ]] || die '保留的数据目录必须是实体目录' ;; esac
            cp -a -- "$entry" "$candidate/"
        done
        mv -- "$PREFIX" "$TXN/old"; OLD_TARGET=1
    fi
    mv -- "$candidate" "$PREFIX"; NEW_TARGET=1
fi

"$PREFIX/python/bin/python3" -E -s -B - "$PREFIX" "$VERSION" "$KIND" <<'PY'
import json, pathlib, shlex, sys
prefix, version, kind = sys.argv[1:]
root = pathlib.Path(prefix)
if kind != 'installed':
    (root / '.clash-install.json').write_text(json.dumps({'format': 1, 'version': version, 'prefix': prefix}) + '\n')
script = shlex.quote(str(root / 'scripts/shell_integration.sh'))
(root / 'env.sh').write_text('# Load clash into this shell. No Python PATH changes.\n'
                           'if [ -x ' + script + ' ]; then\n'
                           '    eval "$(' + script + ' print)"\n'
                           'fi\n')
PY
chmod 0600 "$PREFIX/.clash-install.json"
chmod 0644 "$PREFIX/env.sh"
if ((!NO_SHELL)); then
    SHELL_TOUCHED=1
    "$PREFIX/clash" shell-init --shell "$SHELL_NAME" || die 'shell 接入失败；安装和已有 shell 文件已回滚'
fi
COMMITTED=1
printf '\n安装完成：%s\n' "$PREFIX"
printf '当前终端请执行：source %s\n' "$(quote "$PREFIX/env.sh")"
printf '随后运行 clash 打开工作台，再用 clash on 开启当前终端代理。\n'
