#!/usr/bin/env bash
set -euo pipefail

readonly PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly RUNTIME="${PROJECT_DIR}/scripts/mihomo_runtime.sh"

if [[ -r "${PROJECT_DIR}/.env" ]]; then
	# shellcheck disable=SC1091
	source "${PROJECT_DIR}/.env"
fi

"$RUNTIME" stop
"$RUNTIME" start
