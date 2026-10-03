#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
readonly TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/configure-env-test.XXXXXXXX")"
trap 'rm -rf -- "$TEST_DIR"' EXIT

fail() {
	printf 'configure_env_test: FAIL: %s\n' "$*" >&2
	exit 1
}

assert_eq() {
	local expected=$1 actual=$2 label=$3
	[[ "$expected" == "$actual" ]] ||
		fail "${label}: expected <${expected}>, got <${actual}>"
}

mkdir -p "$TEST_DIR/project/scripts" "$TEST_DIR/config dir"
cp -- "${PROJECT_DIR}/scripts/configure_env.sh" "$TEST_DIR/project/scripts/"
chmod 0755 "$TEST_DIR/project/scripts/configure_env.sh"
config_file="$TEST_DIR/config dir/profile's config.yaml"
printf '%s\n' 'proxies: []' >"$config_file"

subscription_url="https://example.invalid/sub?token=o'hara&client=mihomo"
"$TEST_DIR/project/scripts/configure_env.sh" subscription "$subscription_url" >/dev/null
env_file="$TEST_DIR/project/.env"

assert_eq 600 "$(stat -c '%a' "$env_file")" '.env permissions'
# shellcheck disable=SC1090
source "$env_file"
assert_eq "$subscription_url" "$CLASH_URL" 'subscription URL'
assert_eq '' "$CLASH_CONFIG_FILE" 'subscription clears config file'
assert_eq provider "$SUBSCRIPTION_MODE" 'subscription mode'
assert_eq '127.0.0.1:9090' "$MIHOMO_CONTROLLER" 'loopback controller'
[[ "$CLASH_SECRET" =~ ^[a-f0-9]{64}$ ]] || fail 'generated secret format'
first_secret=$CLASH_SECRET

chmod 0644 "$env_file"
"$TEST_DIR/project/scripts/configure_env.sh" config "$config_file" >/dev/null
assert_eq 600 "$(stat -c '%a' "$env_file")" 'rewritten .env permissions'
unset CLASH_URL CLASH_CONFIG_FILE SUBSCRIPTION_MODE CLASH_SECRET MIHOMO_CONTROLLER
# shellcheck disable=SC1090
source "$env_file"
assert_eq '' "$CLASH_URL" 'config clears subscription URL'
assert_eq "$config_file" "$CLASH_CONFIG_FILE" 'absolute config path'
assert_eq local-profile "$SUBSCRIPTION_MODE" 'config mode'
assert_eq "$first_secret" "$CLASH_SECRET" 'secret persists across reconfiguration'

before=$(sha256sum "$env_file")
if "$TEST_DIR/project/scripts/configure_env.sh" subscription 'file:///not-http' >/dev/null 2>&1; then
	fail 'invalid subscription URL was accepted'
fi
assert_eq "$before" "$(sha256sum "$env_file")" 'invalid URL leaves .env unchanged'
if "$TEST_DIR/project/scripts/configure_env.sh" config "$TEST_DIR/missing.yaml" >/dev/null 2>&1; then
	fail 'missing config file was accepted'
fi
assert_eq "$before" "$(sha256sum "$env_file")" 'missing config leaves .env unchanged'

if grep -Fq 'change-me' "${PROJECT_DIR}/.env.example"; then
	fail '.env.example contains change-me'
fi
grep -Fq "export MIHOMO_CONTROLLER='127.0.0.1:9090'" \
	"${PROJECT_DIR}/.env.example" || fail '.env.example controller is not loopback'
grep -Fq '${MIHOMO_CONTROLLER:-127.0.0.1:9090}' \
	"${PROJECT_DIR}/start.sh" || fail 'start.sh controller fallback is not loopback'

printf '%s\n' 'configure_env security self-test: PASS'
