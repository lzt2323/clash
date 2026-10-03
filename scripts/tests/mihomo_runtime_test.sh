#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
readonly TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/mihomo-runtime-test.XXXXXXXX")"
trap 'rm -rf -- "$TEST_DIR"' EXIT

assert_eq() {
    [[ "$1" == "$2" ]] || {
        printf 'expected <%s>, got <%s>\n' "$1" "$2" >&2
        exit 1
    }
}

fake_binary="${TEST_DIR}/mihomo"
config="${TEST_DIR}/config.yaml"
printf '%s\n' '#!/usr/bin/env bash' 'printf "%s\n" "$*"' >"$fake_binary"
chmod 0755 "$fake_binary"
printf '%s\n' 'proxies: []' >"$config"

output="$(
    MIHOMO_BINARY="$fake_binary" \
    MIHOMO_CONFIG_DIR="$TEST_DIR" \
    MIHOMO_CONFIG="$config" \
    "${PROJECT_DIR}/scripts/mihomo_runtime.sh" validate
)"
assert_eq "-t -d ${TEST_DIR} -f ${config}" "$output"

output="$(MIHOMO_ARCH=arm64 "${PROJECT_DIR}/scripts/install_mihomo.sh" --print-url)"
assert_eq \
    "https://github.com/MetaCubeX/mihomo/releases/download/v1.19.29/mihomo-linux-arm64-v1.19.29.gz" \
    "$output"

offline_payload="${TEST_DIR}/offline-mihomo"
offline_archive="${TEST_DIR}/offline-mihomo.gz"
install_dir="${TEST_DIR}/install"
cache_dir="${TEST_DIR}/cache"
printf '%s\n' '#!/usr/bin/env bash' 'printf "offline mihomo %s\n" "$*"' \
    >"$offline_payload"
chmod 0755 "$offline_payload"
gzip -c -- "$offline_payload" >"$offline_archive"
offline_sha="$(sha256_file="$offline_archive"; sha256sum "$sha256_file" | awk '{print $1}')"

MIHOMO_ARCHIVE="$offline_archive" \
MIHOMO_SHA256="$offline_sha" \
MIHOMO_INSTALL_DIR="$install_dir" \
MIHOMO_CACHE_DIR="$cache_dir" \
"${PROJECT_DIR}/scripts/install_mihomo.sh" >/dev/null
[[ -x "${install_dir}/mihomo" ]]

rm -f -- "${install_dir}/mihomo"
MIHOMO_SHA256="$offline_sha" \
MIHOMO_INSTALL_DIR="$install_dir" \
MIHOMO_CACHE_DIR="$cache_dir" \
MIHOMO_RELEASE_BASE_URL='https://127.0.0.1.invalid' \
"${PROJECT_DIR}/scripts/install_mihomo.sh" >/dev/null
[[ -x "${install_dir}/mihomo" ]]

printf '%s\n' "mihomo runtime self-test: PASS"
