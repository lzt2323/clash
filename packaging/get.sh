#!/usr/bin/env bash
# Small bootstrap: fetch the installer from the same fixed release as its packages.
set -euo pipefail
umask 077
version=v0.2.0
server=https://download.getplus.dpdns.org
github=https://github.com/lzt2323/clash/releases/download
arguments=("$@")
while (($#)); do
    case "$1" in
        --version|--server|--github)
            (($# >= 2)) || { printf '选项 %s 缺少参数\n' "$1" >&2; exit 2; }
            case "$1" in --version) version=$2 ;; --server) server=$2 ;; --github) github=$2 ;; esac
            shift 2 ;;
        --prefix|--archive|--sha256|--shell) (($# >= 2)) || exit 2; shift 2 ;;
        --no-shell|--upgrade|--recover) shift ;;
        -h|--help)
            printf '用法：bash get.sh [--prefix DIR] [--version VERSION] [--server HTTPS_BASE] [--github HTTPS_BASE] [--no-shell] [--upgrade|--recover]\n'
            exit 0 ;;
        *) printf '未知选项：%s\n' "$1" >&2; exit 2 ;;
    esac
done
[[ "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][a-zA-Z0-9.-]+)?$ ]] || { printf '版本格式错误\n' >&2; exit 2; }
for base in "$server" "$github"; do
    [[ -z "$base" || "$base" == https://* ]] || { printf '下载源必须使用 HTTPS\n' >&2; exit 2; }
    [[ "$base" != *$'\n'* && "$base" != *'?'* && "$base" != *'#'* ]] || exit 2
done
command -v curl >/dev/null 2>&1 || { printf '下载安装入口需要 curl\n' >&2; exit 2; }
temporary=$(mktemp -d)
trap 'rm -rf -- "$temporary"' EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM
fetch() {
    curl --fail --location --silent --show-error --connect-timeout 10 --max-time 60 \
        --proto '=https' --proto-redir '=https' --output "$temporary/install.sh" "$1"
}
downloaded=0
if [[ -n "$server" ]] && fetch "${server%/}/releases/$version/install.sh"; then downloaded=1; fi
if ((!downloaded)); then
    printf '主安装入口不可用，尝试同版本 GitHub Release...\n' >&2
    [[ -n "$github" ]] && fetch "${github%/}/$version/install.sh" || { printf '安装入口下载失败\n' >&2; exit 1; }
fi
bash "$temporary/install.sh" "${arguments[@]}"
