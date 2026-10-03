#!/usr/bin/env bash
set -euo pipefail

readonly PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ -r "${PROJECT_DIR}/.env" ]]; then
	# shellcheck disable=SC1091
	source "${PROJECT_DIR}/.env"
fi

"${PROJECT_DIR}/scripts/mihomo_runtime.sh" stop
printf '\n服务关闭成功，如当前终端已开启系统代理，请执行：proxy_off\n\n'
