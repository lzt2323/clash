#!/usr/bin/env bash
set -euo pipefail

readonly ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/clash-cli-test.XXXXXXXX")"
readonly CLI="${TEST_DIR}/clash"
readonly CANONICAL_TEST_DIR="$(cd -- "$TEST_DIR" && pwd)"
readonly CALLS="${TEST_DIR}/calls"
trap 'rm -rf -- "$TEST_DIR"' EXIT

# Never inherit the developer/deployment state or .env during CLI contract tests.
cp "${ROOT_DIR}/clash" "$CLI"

passes=0

fail() {
    printf 'not ok - %s\n' "$1" >&2
    exit 1
}

pass() {
    passes=$((passes + 1))
    printf 'ok - %s\n' "$1"
}

assert_contains() {
    local value="$1"
    local expected="$2"
    local description="$3"
    [[ "$value" == *"$expected"* ]] || fail "$description"
    pass "$description"
}

assert_calls() {
    local expected="$1"
    local description="$2"
    local actual
    actual="$(<"$CALLS")"
    [[ "$actual" == "$expected" ]] || {
        printf 'expected calls:\n%s\nactual calls:\n%s\n' "$expected" "$actual" >&2
        fail "$description"
    }
    pass "$description"
}

make_stub() {
    local path="$1"
    local label="$2"
    printf '%s\n' \
        '#!/usr/bin/env bash' \
        "printf '${label}:%s\\n' \"\$*\" >>\"\$CLASH_TEST_CALLS\"" >"$path"
    chmod 0755 "$path"
}

configure_stub="${TEST_DIR}/configure"
doctor_stub="${TEST_DIR}/doctor"
dashboard_stub="${TEST_DIR}/dashboard"
install_stub="${TEST_DIR}/install"
connectivity_stub="${TEST_DIR}/connectivity"
shell_integration_stub="${TEST_DIR}/shell-integration"
runtime_stub="${TEST_DIR}/runtime"
start_stub="${TEST_DIR}/start"
restart_stub="${TEST_DIR}/restart"
shutdown_stub="${TEST_DIR}/shutdown"
selector_stub="${TEST_DIR}/selector"
shell_stub="${TEST_DIR}/shell"
mvp_stub="${TEST_DIR}/mvp.py"
upgrade_stub="${TEST_DIR}/upgrade.py"
config_file="${TEST_DIR}/config.yaml"
log_file="${TEST_DIR}/mihomo.log"
make_stub "$configure_stub" "configure"
make_stub "$dashboard_stub" "dashboard"
make_stub "$install_stub" "install"
make_stub "$connectivity_stub" "connectivity"
make_stub "$shell_integration_stub" "shell-integration"
make_stub "$runtime_stub" "runtime"
make_stub "$start_stub" "start"
make_stub "$restart_stub" "restart"
make_stub "$shutdown_stub" "shutdown"
make_stub "$selector_stub" "selector"
cat >"$mvp_stub" <<'PYTHON'
import os
import sys
with open(os.environ['CLASH_TEST_CALLS'], 'a') as calls:
    calls.write('mvp:' + ' '.join(sys.argv[1:]) + '\n')
if sys.argv[1:] == ['ensure']:
    print('内核已就绪')
    sys.exit(int(os.environ.get('CLASH_TEST_ENSURE_STATUS', '0')))
PYTHON
cp "$mvp_stub" "$upgrade_stub"
printf '%s\n' \
    '#!/usr/bin/env bash' \
    'printf "doctor:%s:%s\n" "${DOCTOR_ALLOW_MISSING_MIHOMO:-0}" "$*" >>"$CLASH_TEST_CALLS"' \
    >"$doctor_stub"
chmod 0755 "$doctor_stub"
printf '%s\n' \
    '#!/usr/bin/env bash' \
    'printf "shell:%s|%s\n" "$http_proxy" "$all_proxy" >>"$CLASH_TEST_CALLS"' \
    >"$shell_stub"
chmod 0755 "$shell_stub"
printf '%s\n' 'proxies: []' >"$config_file"
printf '%s\n' 'first line' 'last line' >"$log_file"

run_cli() {
    CLASH_TEST_CALLS="$CALLS" \
    CLASH_CONFIGURE_SCRIPT="$configure_stub" \
    CLASH_DOCTOR_SCRIPT="$doctor_stub" \
    CLASH_DASHBOARD_SCRIPT="$dashboard_stub" \
    CLASH_INSTALL_SCRIPT="$install_stub" \
    CLASH_CONNECTIVITY_SCRIPT="$connectivity_stub" \
    CLASH_SHELL_INTEGRATION_SCRIPT="$shell_integration_stub" \
    CLASH_RUNTIME_SCRIPT="$runtime_stub" \
    CLASH_START_SCRIPT="$start_stub" \
    CLASH_RESTART_SCRIPT="$restart_stub" \
    CLASH_SHUTDOWN_SCRIPT="$shutdown_stub" \
    CLASH_SELECTOR_SCRIPT="$selector_stub" \
    CLASH_MVP_SCRIPT="$mvp_stub" \
    CLASH_UPGRADE_SCRIPT="$upgrade_stub" \
    MIHOMO_BINARY="/bin/true" \
    MIHOMO_CONFIG="$config_file" \
    MIHOMO_LOG="$log_file" \
    "$CLI" "$@"
}

bash -n "$CLI"
pass "CLI has valid Bash syntax"

output="$(run_cli help)"
assert_contains "$output" "clash off" "help documents daily proxy commands"
assert_contains "$output" "clash start" "help documents core startup"
assert_contains "$output" "clash stop" "help documents whole-core shutdown"
assert_contains "$output" "clash menu --plain" "help documents text fallback"
assert_contains "$output" 'eval "$(./clash on)"' "help documents parent-shell fallback"

: >"$CALLS"
run_cli start >/dev/null
assert_calls "start:" "start delegates to the safe start workflow"

: >"$CALLS"
run_cli init 'https://example.invalid/sub?token=secret' >/dev/null
assert_calls $'configure:subscription https://example.invalid/sub?token=secret\ndoctor:1:\nstart:\nconnectivity:\ndashboard:' \
    "subscription init configures, validates, starts, and shows dashboard guidance"

: >"$CALLS"
run_cli init --config '/tmp/profile with spaces.yaml' >/dev/null
assert_calls $'configure:config /tmp/profile with spaces.yaml\ndoctor:1:\nstart:\nconnectivity:\ndashboard:' \
    "local init preserves a path with spaces and shows dashboard guidance"

: >"$CALLS"
run_cli status >/dev/null
assert_calls "mvp:status" "status delegates to managed state summary"

: >"$CALLS"
run_cli stop >/dev/null
assert_calls "shutdown:" "stop delegates to shutdown wrapper"

: >"$CALLS"
run_cli restart >/dev/null
assert_calls "restart:" "restart delegates to restart wrapper"

: >"$CALLS"
run_cli menu >/dev/null
assert_calls "mvp:menu" "menu delegates to managed menu"

: >"$CALLS"
run_cli menu --plain >/dev/null
assert_calls "mvp:menu --plain" "plain menu option reaches managed backend"
if run_cli menu --invalid >/dev/null 2>&1; then
    fail "menu rejects unknown options"
fi
pass "menu rejects unknown options"

: >"$CALLS"
run_cli >/dev/null
assert_calls "mvp:menu" "no arguments opens the menu"

: >"$CALLS"
run_cli ensure >/dev/null
assert_calls "mvp:ensure" "ensure delegates to managed startup"

: >"$CALLS"
run_cli mode global >/dev/null
assert_calls "selector:mode global" "mode delegates to selector"

: >"$CALLS"
run_cli switch 'hong kong' PROXY >/dev/null
assert_calls "selector:switch hong kong PROXY" \
    "switch preserves fuzzy query and group"

: >"$CALLS"
run_cli test --url 'https://example.invalid/generate_204' >/dev/null
assert_calls "connectivity:--url https://example.invalid/generate_204" \
    "test delegates to connectivity check"

: >"$CALLS"
run_cli shell-init --shell bash >/dev/null
assert_calls "shell-integration:install --shell bash" \
    "shell-init installs user-level integration"

: >"$CALLS"
run_cli version >/dev/null
assert_calls "runtime:version" "version delegates to runtime"


for program_command in upgrade rollback recover app-version; do
    : >"$CALLS"
    run_cli "$program_command" >/dev/null
    assert_calls "mvp:--root $CANONICAL_TEST_DIR $program_command" "$program_command delegates to application upgrader"
done
: >"$CALLS"
run_cli upgrade --archive '/tmp/release package.tar.gz' --sha256 abc >/dev/null
assert_calls "mvp:--root $CANONICAL_TEST_DIR upgrade --archive /tmp/release package.tar.gz --sha256 abc" "offline upgrade preserves archive arguments"
: >"$CALLS"
run_cli upgrade --check >/dev/null
assert_calls "mvp:--root $CANONICAL_TEST_DIR upgrade --check" "upgrade check reaches application updater"
: >"$CALLS"
run_cli update >/dev/null
assert_calls "install:" "update continues to update only the core"

output="$(CLASH_HTTP_PROXY='http://127.0.0.1:17890' run_cli env)"
assert_contains "$output" 'export http_proxy=http://127.0.0.1:17890' \
    "env emits the configured HTTP proxy"
(
    unset http_proxy
    eval "$output"
    [[ "$http_proxy" == "http://127.0.0.1:17890" ]]
) || fail "env output can be evaluated"
pass "env output can be evaluated"

on_notice="${TEST_DIR}/on-notice"
: >"$CALLS"
output="$(run_cli on 2>"$on_notice")"
assert_calls "mvp:ensure" "on verifies core readiness before emitting environment"
[[ "$output" != *"内核已就绪"* ]] || fail "on stdout only contains shell code"
pass "on stdout only contains shell code"
assert_contains "$output" "export http_proxy=" "on emits eval-able proxy exports"
assert_contains "$(<"$on_notice")" 'eval "$(./clash on)"' \
    "on explains how to modify the parent shell"

if output="$(CLASH_TEST_ENSURE_STATUS=9 run_cli on 2>/dev/null)"; then
    fail "on fails if startup fails"
fi
[[ -z "$output" ]] || fail "failed on emits no environment"
pass "failed on emits no environment"

: >"$CALLS"
output="$(run_cli off 2>/dev/null)"
assert_calls "" "off does not stop or contact the core"
assert_contains "$output" "unset http_proxy" "off emits proxy cleanup"

: >"$CALLS"
CLASH_SHELL="$shell_stub" run_cli shell
assert_calls $'mvp:ensure\nshell:http://127.0.0.1:7890|socks5h://127.0.0.1:7891' \
    "shell starts a child process with proxy variables"

output="$(run_cli logs)"
assert_contains "$output" "last line" "logs reads the configured log"

: >"$CALLS"
run_cli dashboard --host server.example --user alice >/dev/null
assert_calls "dashboard:--host server.example --user alice" \
    "dashboard forwards guidance options"

: >"$CALLS"
run_cli doctor >/dev/null
assert_calls "doctor:0:" "doctor delegates to the diagnostic script"

: >"$CALLS"
run_cli install >/dev/null
assert_calls "install:" "install delegates to the pinned installer"

if run_cli unknown >/dev/null 2>&1; then
    fail "unknown command fails"
fi
pass "unknown command fails"

# Old config files may carry a stale API secret or proxy address. The new
# managed entry points must not evaluate them before loading managed state.
isolated_dir="${TEST_DIR}/isolated"
mkdir -p "$isolated_dir"
cp "$CLI" "$isolated_dir/clash"
printf '%s\n' 'exit 42' >"$isolated_dir/.env"
CLASH_TEST_CALLS="$CALLS" CLASH_MVP_SCRIPT="$mvp_stub" \
    "$isolated_dir/clash" status >/dev/null || fail "managed status ignores legacy .env"
pass "managed status ignores legacy .env"
output="$("$isolated_dir/clash" env)" || fail "managed env ignores legacy .env"
assert_contains "$output" 'export http_proxy=' "managed env ignores legacy .env"

mkdir -p "$isolated_dir/runtime/mvp"
printf '%s\n' '{}' >"$isolated_dir/runtime/mvp/state.json"
for legacy_command in init mode switch; do
    : >"$CALLS"
    if output="$(CLASH_TEST_CALLS="$CALLS" CLASH_MVP_SCRIPT="$mvp_stub" \
        CLASH_START_SCRIPT="$start_stub" CLASH_RESTART_SCRIPT="$restart_stub" \
        CLASH_SELECTOR_SCRIPT="$selector_stub" CLASH_CONFIGURE_SCRIPT="$configure_stub" \
        "$isolated_dir/clash" "$legacy_command" 2>&1)"; then
        fail "managed state rejects legacy $legacy_command"
    fi
    [[ "$output" == *"请运行 clash 菜单"* ]] || fail "managed $legacy_command gives migration guidance"
    assert_calls "" "managed state blocks legacy $legacy_command before any mutation"
done
: >"$CALLS"
for service_command in start restart stop stop; do
    CLASH_TEST_CALLS="$CALLS" CLASH_MVP_SCRIPT="$mvp_stub" \
        CLASH_START_SCRIPT="$start_stub" CLASH_RESTART_SCRIPT="$restart_stub" \
        CLASH_SHUTDOWN_SCRIPT="$shutdown_stub" \
        "$isolated_dir/clash" "$service_command" >/dev/null || fail "managed $service_command remains available"
done
assert_calls $'mvp:start\nmvp:restart\nmvp:stop\nmvp:stop' \
    "managed service commands consistently delegate to backend including repeated stop"

# Packaged installs must use their own isolated interpreter and never silently
# fall back to the host if that runtime is missing.
printf '%s\n' '{}' >"$isolated_dir/.clash-install.json"
if output="$(CLASH_MVP_SCRIPT="$mvp_stub" "$isolated_dir/clash" status 2>&1)"; then
    fail "managed package refuses missing private Python"
fi
assert_contains "$output" '独立 Python 缺失' "managed package gives repair guidance for missing Python"
mkdir -p "$isolated_dir/python/bin" "$isolated_dir/probe"
{
    printf '%s\n' '#!/bin/bash' 'export CLASH_TEST_PRIVATE_CALLED=1'
    printf 'exec %q "$@"\n' "$(command -v python3)"
} >"$isolated_dir/python/bin/python3"
chmod 0755 "$isolated_dir/python/bin/python3"
printf '%s\n' 'VALUE = "local-import-works"' >"$isolated_dir/probe/local_dependency.py"
cat >"$isolated_dir/probe/check.py" <<'PYTHON'
import os
import sys
from local_dependency import VALUE
assert sys.flags.ignore_environment == 1
assert sys.flags.no_user_site == 1
assert sys.dont_write_bytecode
assert sys.flags.isolated == 0
assert os.environ['CLASH_TEST_PRIVATE_CALLED'] == '1'
assert os.environ['PYTHONHOME'] == '/invalid-python-home'
assert os.environ['PYTHONPATH'] == '/invalid-python-path'
print(VALUE + ':' + ' '.join(sys.argv[1:]))
PYTHON
output="$(PYTHONHOME=/invalid-python-home PYTHONPATH=/invalid-python-path \
    CLASH_MVP_SCRIPT="$isolated_dir/probe/check.py" "$isolated_dir/clash" status)"
assert_contains "$output" 'local-import-works:status' \
    "private Python ignores host Python settings while retaining sibling imports"
output="$(PYTHONHOME=/invalid-python-home PYTHONPATH=/invalid-python-path \
    CLASH_UNINSTALL_SCRIPT="$isolated_dir/probe/check.py" "$isolated_dir/clash" uninstall --purge)"
assert_contains "$output" 'local-import-works:--purge' "uninstall uses private isolated interpreter and forwards purge"
output="$(PYTHONHOME=/invalid-python-home PYTHONPATH=/invalid-python-path \
    CLASH_UNINSTALL_SCRIPT="$isolated_dir/probe/check.py" "$isolated_dir/clash" uninstall)"
[[ "$output" == 'local-import-works:' ]] || fail "uninstall preserves data by default"
pass "uninstall preserves data by default"
rm "$isolated_dir/runtime/mvp/state.json"
: >"$CALLS"
for service_command in start stop restart; do
    CLASH_TEST_CALLS="$CALLS" CLASH_MVP_SCRIPT="$mvp_stub" \
        CLASH_START_SCRIPT="$start_stub" CLASH_RESTART_SCRIPT="$restart_stub" \
        CLASH_SHUTDOWN_SCRIPT="$shutdown_stub" \
        "$isolated_dir/clash" "$service_command" >/dev/null || fail "fresh package uses managed $service_command"
done
assert_calls $'mvp:start\nmvp:stop\nmvp:restart' \
    "complete installation without subscriptions uses managed lifecycle and ignores legacy env"
if CLASH_UNINSTALL_SCRIPT="$isolated_dir/probe/check.py" "$isolated_dir/clash" uninstall --invalid >/dev/null 2>&1; then
    fail "uninstall rejects unknown options"
fi
pass "uninstall rejects unknown options"

printf '\n%d CLI tests passed\n' "$passes"
