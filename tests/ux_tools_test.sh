#!/usr/bin/env bash
set -u

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
failures=0
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/clash-ux-test.XXXXXX") || exit 1
trap 'rm -rf "$work_dir"' EXIT
fake_bin="$work_dir/bin"
project="$work_dir/project"
mkdir -p "$fake_bin" "$project/bin"

pass() { printf 'ok - %s\n' "$1"; }
fail() { printf 'not ok - %s\n' "$1" >&2; failures=$((failures + 1)); }

assert_contains() {
	local text=$1 pattern=$2 message=$3
	if grep -Fq -- "$pattern" <<<"$text"; then pass "$message"; else fail "$message"; fi
}

assert_not_contains() {
	local text=$1 pattern=$2 message=$3
	if grep -Fq -- "$pattern" <<<"$text"; then fail "$message"; else pass "$message"; fi
}

for command_name in curl gzip install openssl jq ss; do
	ln -s /bin/true "$fake_bin/$command_name"
done
ln -s /bin/true "$project/bin/mihomo"

cat >"$project/.env" <<'EOF'
export CLASH_URL='https://example.invalid/sub?token=test'
export CLASH_SECRET='never-print-this-secret'
export MIHOMO_API_URL='http://127.0.0.1:19090'
export MIHOMO_CONTROLLER='0.0.0.0:19090'
EOF
chmod 600 "$project/.env"

doctor_output=$(PATH="$fake_bin:/usr/bin:/bin" \
	DOCTOR_PROJECT_DIR="$project" DOCTOR_PORTS_OUTPUT='' \
	bash "$ROOT_DIR/scripts/doctor.sh" 2>&1)
doctor_status=$?
[[ $doctor_status -eq 0 ]] && pass 'doctor returns 0 when required checks pass' ||
	fail 'doctor returns 0 when required checks pass'
assert_contains "$doctor_output" '配置来源：URL provider' 'doctor detects URL source'
assert_contains "$doctor_output" '端口 19090 可用' 'doctor checks custom API port'
assert_not_contains "$doctor_output" 'never-print-this-secret' \
	'doctor does not reveal Secret'

missing_output=$(PATH="$fake_bin:/usr/bin:/bin" \
	DOCTOR_PROJECT_DIR="$project" DOCTOR_PORTS_OUTPUT='' \
	DOCTOR_FORCE_MISSING='install' \
	bash "$ROOT_DIR/scripts/doctor.sh" 2>&1)
missing_status=$?
[[ $missing_status -eq 1 ]] && pass 'doctor returns 1 for missing required command' ||
	fail 'doctor returns 1 for missing required command'
assert_contains "$missing_output" 'coreutils' \
	'doctor maps the install command to its package name'

chmod 644 "$project/.env"
permission_output=$(PATH="$fake_bin:/usr/bin:/bin" \
	DOCTOR_PROJECT_DIR="$project" DOCTOR_PORTS_OUTPUT='' \
	bash "$ROOT_DIR/scripts/doctor.sh" 2>&1)
permission_status=$?
[[ $permission_status -eq 1 ]] && pass 'doctor rejects broadly readable .env' ||
	fail 'doctor rejects broadly readable .env'
assert_contains "$permission_output" 'chmod 600 .env' \
	'doctor gives safe .env permission remediation'
chmod 600 "$project/.env"

cat >>"$project/.env" <<'EOF'
export CLASH_CONFIG_FILE='profile.yaml'
EOF
conflict_output=$(PATH="$fake_bin:/usr/bin:/bin" \
	DOCTOR_PROJECT_DIR="$project" DOCTOR_PORTS_OUTPUT='' \
	bash "$ROOT_DIR/scripts/doctor.sh" 2>&1)
conflict_status=$?
[[ $conflict_status -eq 1 ]] && pass 'doctor returns 1 for required configuration failure' ||
	fail 'doctor returns 1 for required configuration failure'
assert_contains "$conflict_output" '来源冲突' 'doctor explains source conflict'

sed -i '/CLASH_CONFIG_FILE/d' "$project/.env"
port_output=$(PATH="$fake_bin:/usr/bin:/bin" \
	DOCTOR_PROJECT_DIR="$project" \
	DOCTOR_PORTS_OUTPUT='LISTEN 0 128 127.0.0.1:19090 0.0.0.0:*' \
	bash "$ROOT_DIR/scripts/doctor.sh" 2>&1)
port_status=$?
[[ $port_status -eq 1 ]] && pass 'doctor returns 1 for occupied required port' ||
	fail 'doctor returns 1 for occupied required port'
assert_contains "$port_output" '端口 19090 已被监听' 'doctor reports occupied custom API port'

dashboard_output=$(DASHBOARD_ENV_FILE="$project/.env" USER='fallback-user' \
	bash "$ROOT_DIR/scripts/dashboard.sh" \
	--host server.example --user alice --local-port 29090)
dashboard_status=$?
[[ $dashboard_status -eq 0 ]] && pass 'dashboard guidance exits successfully' ||
	fail 'dashboard guidance exits successfully'
assert_contains "$dashboard_output" \
	'ssh -N -L 29090:127.0.0.1:19090 alice@server.example' \
	'dashboard prints copyable SSH forwarding command'
assert_contains "$dashboard_output" 'http://127.0.0.1:29090/ui' \
	'dashboard prints local-only URL'
assert_contains "$dashboard_output" 'Secret：never-print-this-secret' \
	'dashboard prints Secret directly for copy-paste'
assert_contains "$dashboard_output" '不要截图、不要发到聊天里、不要提交到 Git' \
	'dashboard warns that Secret is sensitive'

printf '\nUX tools: %d failed\n' "$failures"
[[ $failures -eq 0 ]]
