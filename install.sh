#!/usr/bin/env bash
# Thin bootstrap: downloads a verified Rust release, then delegates installation to it.
set -euo pipefail
umask 077

RELEASE_TAG=rust-v0.5.0
REPOSITORY=${CLASH_INSTALL_REPOSITORY:-lzt2323/clash}

fail() { printf '错误：%s\n' "$*" >&2; exit 1; }
usage() {
    cat <<'HELP'
Clash Linux 一键安装器
用法：bash install.sh [选项]
  --prefix DIRECTORY   安装目录，默认 ~/.local/share/clash-linux-rust
  --archive FILE       使用本地 Rust 发行包；默认从 GitHub Releases 下载
  --sha256 HASH        本地包的 SHA-256；省略时读取 FILE.sha256
  --core FILE          复用现有 Mihomo 核心（仅新安装）
  --import-data DIR    显式导入旧版订阅（仅新安装）
  --skip-core          暂不安装核心（仅新安装）
  --no-shell           不修改 shell 配置
  --help               显示帮助
再次运行会调用现有 Rust 升级流程，保留订阅和核心。
HELP
}

main() {
    local prefix=${HOME:?缺少 HOME}/.local/share/clash-linux-rust
    local archive='' checksum='' no_shell=0 install_options=0
    local -a install_args=()
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --help|-h) usage; return ;;
            --prefix|--archive|--sha256|--core|--import-data)
                [ "$#" -ge 2 ] && [ -n "$2" ] || fail "选项 $1 需要参数。"
                case "$1" in
                    --prefix) prefix=$2 ;;
                    --archive) archive=$2 ;;
                    --sha256) checksum=$2 ;;
                    *) install_args+=("$1" "$2"); install_options=1 ;;
                esac
                shift 2 ;;
            --no-shell) no_shell=1; shift ;;
            --skip-core) install_args+=("$1"); install_options=1; shift ;;
            *) fail "未知选项：$1。请运行 bash install.sh --help。" ;;
        esac
    done
    [ "$(uname -s)" = Linux ] || fail '此安装器仅支持 Linux。'
    case "$(uname -m)" in
        x86_64|amd64) local target=x86_64-unknown-linux-gnu ;;
        *) fail '当前提供的一键安装包仅支持 Linux x86_64 / amd64。' ;;
    esac
    for command in tar gzip sha256sum mktemp wc chmod getconf; do
        command -v "$command" >/dev/null || fail "缺少命令：$command。"
    done
    getconf GNU_LIBC_VERSION >/dev/null 2>&1 || fail '此发行包需要 glibc，暂不支持 Alpine / musl 系统。'
    local work
    work=$(mktemp -d "${TMPDIR:-/tmp}/clash-linux-install.XXXXXXXX")
    # Keep cleanup traps scoped to this installation attempt.
    (
        trap 'rm -rf -- "$work"' EXIT
        trap 'exit 130' INT
        trap 'exit 143' TERM
        local name=clash-linux-rust-${target}.tar.gz
        if [ -z "$archive" ]; then
            [ -z "$checksum" ] || fail '--sha256 只用于 --archive 本地安装。'
            command -v curl >/dev/null || fail '缺少 curl，请先安装 curl。'
            [[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || fail 'GitHub 仓库名无效。'
            local base=${CLASH_INSTALL_BASE_URL:-https://github.com/${REPOSITORY}/releases/download/${RELEASE_TAG}}
            [[ "$base" == https://* && "$base" != *$'\n'* && "${base#https://}" != *@* ]] || fail '发行地址必须使用不含账号密码的 HTTPS URL。'
            archive=$work/$name
            printf '正在获取 Clash Linux %s…\n' "$RELEASE_TAG"
            curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fLsS \
                --retry 2 --connect-timeout 20 --max-time 180 --max-filesize 134217728 \
                "${base%/}/$name" -o "$archive" || fail '发行包下载失败，请检查网络或改用 --archive。'
            curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fLsS \
                --retry 2 --connect-timeout 20 --max-time 60 --max-filesize 4096 \
                "${base%/}/$name.sha256" -o "$archive.sha256" || fail '校验文件下载失败，安装已停止。'
        fi
        [ -f "$archive" ] || fail '本地发行包不存在。'
        [ "$(wc -c < "$archive")" -le 134217728 ] || fail '发行包超过 128 MB，安装已停止。'
        if [ -z "$checksum" ]; then
            [ -f "$archive.sha256" ] || fail '缺少 .sha256 文件，请提供 --sha256 HASH。'
            read -r checksum _ < "$archive.sha256" || fail '校验文件为空或格式无效。'
        fi
        [[ "$checksum" =~ ^[[:xdigit:]]{64}$ ]] || fail 'SHA-256 格式无效。'
        local actual
        read -r actual _ < <(sha256sum -- "$archive")
        [ "${actual,,}" = "${checksum,,}" ] || fail 'SHA-256 不匹配，安装已停止。'
        # Only the bootstrap executable is streamed out; native Rust validates and
        # extracts the whole archive, including licenses, in its private staging directory.
        (ulimit -f 32768; tar -xOzf "$archive" -- clash > "$work/clash") || fail '无法读取发行包中的 Rust 程序。'
        [ -s "$work/clash" ] || fail '发行包没有 Rust 程序。'
        chmod 700 "$work/clash"
        if [ -f "$prefix/.clash-rust-install.json" ]; then
            [ "$install_options" -eq 0 ] || fail '--core / --skip-core / --import-data 仅用于新安装。'
            "$work/clash" --root "$prefix" upgrade --archive "$archive" --sha256 "$checksum"
            if [ "$no_shell" -eq 0 ]; then "$prefix/clash" shell-init; fi
        else
            if [ "$no_shell" -eq 1 ]; then install_args+=(--no-shell); fi
            "$work/clash" self-install --prefix "$prefix" --archive "$archive" \
                --sha256 "$checksum" "${install_args[@]}"
        fi
        printf '\n安装完成。当前终端执行：\nsource %q\nclash\n' "$prefix/env.sh"
    )
}

main "$@"
