#!/usr/bin/env bash
set -u

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
failures=0
test_dir=$(mktemp -d "${TMPDIR:-/tmp}/clash-shell-test.XXXXXX") || exit 1
trap 'rm -rf "$test_dir"' EXIT
project_dir="$test_dir/project with spaces and 'quote"
home_dir="$test_dir/home"
calls_file="$test_dir/calls"
mkdir -p "$project_dir/scripts" "$home_dir"
cp -- "$ROOT_DIR/scripts/shell_integration.sh" "$project_dir/scripts/"
chmod 0755 "$project_dir/scripts/shell_integration.sh"

pass() { printf 'ok - %s\n' "$1"; }
fail() { printf 'not ok - %s\n' "$1" >&2; failures=$((failures + 1)); }

assert_equal() {
	local actual=$1 expected=$2 message=$3
	if [[ "$actual" == "$expected" ]]; then pass "$message"; else
		fail "$message (expected '$expected', got '$actual')"
	fi
}

cat >"$project_dir/clash" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$CLASH_TEST_CALLS"
case "${1:-}" in
	ensure) exit "${CLASH_TEST_ENSURE_STATUS:-0}" ;;
	start|restart) exit "${CLASH_TEST_SERVICE_STATUS:-0}" ;;
	env)
		[[ ${CLASH_TEST_ENV_STATUS:-0} -eq 0 ]] || exit "$CLASH_TEST_ENV_STATUS"
		cat <<'VARS'
export http_proxy=http://127.0.0.1:17890
export https_proxy=http://127.0.0.1:17890
export HTTP_PROXY=http://127.0.0.1:17890
export HTTPS_PROXY=http://127.0.0.1:17890
export all_proxy=socks5h://127.0.0.1:17890
export ALL_PROXY=socks5h://127.0.0.1:17890
export no_proxy=127.0.0.1,localhost
export NO_PROXY=127.0.0.1,localhost
VARS
		;;
	stop) exit "${CLASH_TEST_STOP_STATUS:-0}" ;;
	uninstall) exit "${CLASH_TEST_UNINSTALL_STATUS:-0}" ;;
esac
EOF
chmod 0755 "$project_dir/clash"

export CLASH_TEST_CALLS="$calls_file"
integration="$project_dir/scripts/shell_integration.sh"
eval "$("$integration" print)"

clash on >/dev/null
assert_equal "${http_proxy:-}" 'http://127.0.0.1:17890' \
	'clash on changes the current shell'
assert_equal "${ALL_PROXY:-}" 'socks5h://127.0.0.1:17890' \
	'clash on sets uppercase proxy variables'
assert_equal "$(cat "$calls_file")" $'ensure\nenv' \
	'clash on ensures readiness before applying environment'

: >"$calls_file"
export http_proxy=http://old.example:1234
CLASH_TEST_ENSURE_STATUS=9 clash on >/dev/null 2>"$test_dir/on-error"
assert_equal "$?" 9 'failed on preserves startup exit status'
assert_equal "$http_proxy" http://old.example:1234 'failed startup preserves existing proxy'
assert_equal "$(cat "$calls_file")" ensure 'failed startup does not request environment'

: >"$calls_file"
CLASH_TEST_ENV_STATUS=8 clash on >/dev/null 2>"$test_dir/env-error"
assert_equal "$?" 8 'failed environment read preserves exit status'
assert_equal "$http_proxy" http://old.example:1234 'failed environment read preserves proxy'

: >"$calls_file"

export ftp_proxy=old socks_proxy=old rsync_proxy=old
clash off >"$test_dir/off-output"
assert_equal "$(cat "$test_dir/off-output")" '已关闭当前终端代理；核心状态保持不变。' \
	'off does not claim the core is running'
assert_equal "$(cat "$calls_file")" "" \
	'clash off never stops or contacts the core'
if [[ -z ${http_proxy+x} && -z ${ALL_PROXY+x} && -z ${ftp_proxy+x} &&
	-z ${socks_proxy+x} && -z ${rsync_proxy+x} ]]; then
	pass 'clash off clears all conventional proxy variables'
else
	fail 'clash off clears all conventional proxy variables'
fi

for service_command in start restart; do
	: >"$calls_file"
	clash "$service_command" >/dev/null
	assert_equal "$?" 0 "$service_command preserves CLI success"
	assert_equal "${http_proxy+x}" "" "$service_command does not enable a disabled shell"
	assert_equal "$(cat "$calls_file")" "$service_command" "$service_command does not request proxy environment"
	export http_proxy=http://old.example:1234 all_proxy=socks5h://old.example:1235
	clash "$service_command" >/dev/null
	assert_equal "$http_proxy|$all_proxy" 'http://old.example:1234|socks5h://old.example:1235' \
		"$service_command preserves existing shell proxy values"
	CLASH_TEST_SERVICE_STATUS=11 clash "$service_command" >/dev/null 2>&1
	assert_equal "$?" 11 "failed $service_command preserves exit status"
	assert_equal "$http_proxy|$all_proxy" 'http://old.example:1234|socks5h://old.example:1235' \
		"failed $service_command preserves existing proxy values"
	clash off >/dev/null
done

clash status 'argument with spaces' >/dev/null
if grep -Fxq 'status argument with spaces' "$calls_file"; then
	pass 'other commands transparently reach CLI at a path containing spaces'
else
	fail 'other commands transparently reach CLI at a path containing spaces'
fi

eval "$("$integration" print)"
clash on >/dev/null
CLASH_TEST_STOP_STATUS=0 clash stop >"$test_dir/stop-success"
stop_status=$?
assert_equal "$stop_status" 0 'clash stop returns success from CLI'
[[ -z ${http_proxy+x} ]] && pass 'successful stop clears current shell proxy' ||
	fail 'successful stop clears current shell proxy'
if grep -Fq '所有使用它的会话' "$test_dir/stop-success" &&
	grep -Fq '其他 SSH 会话' "$test_dir/stop-success"; then
	pass 'stop explains cross-session impact and environment limit'
else
	fail 'stop explains cross-session impact and environment limit'
fi
clash stop >/dev/null
assert_equal "$?" 0 'repeated stop returns success when backend is already stopped'
assert_equal "${http_proxy+x}" "" 'repeated stop keeps current proxy disabled'

clash on >/dev/null
CLASH_TEST_STOP_STATUS=7 clash stop >/dev/null 2>"$test_dir/stop-error"
stop_status=$?
assert_equal "$stop_status" 7 'failed stop preserves CLI exit status'
[[ -z ${http_proxy+x} ]] && pass 'failed stop still clears current shell proxy' ||
	fail 'failed stop still clears current shell proxy'
if grep -Fq '已清除当前终端代理变量' "$test_dir/stop-error"; then
	pass 'failed stop explains cleanup semantics'
else
	fail 'failed stop explains cleanup semantics'
fi

HOME="$home_dir" SHELL=/bin/bash "$integration" install >/dev/null
HOME="$home_dir" SHELL=/bin/bash "$integration" install >/dev/null
marker_count=$(grep -Fc '# >>> clash_linux shell integration >>>' "$home_dir/.bashrc")
assert_equal "$marker_count" 1 'bash install is idempotent'
if grep -Fq 'project with spaces' "$home_dir/.bashrc" &&
	grep -Fq 'shell_integration.sh' "$home_dir/.bashrc"; then
	pass 'installed loader safely quotes a complex project path'
else
	fail 'installed loader safely quotes a complex project path'
fi

shim_path="$home_dir/.local/bin/clash"
if [[ -x "$shim_path" ]]; then
	pass 'shell-init installs a clash command shim'
else
	fail 'shell-init installs a clash command shim'
fi

other_dir="$test_dir/other-project"
mkdir -p "$other_dir"
: >"$calls_file"
HOME="$home_dir" PATH="$home_dir/.local/bin:$PATH" CLASH_TEST_CALLS="$calls_file" \
	bash -c 'cd "$1" && clash status "from elsewhere"' _ "$other_dir" >/dev/null
if grep -Fxq 'status from elsewhere' "$calls_file"; then
	pass 'installed clash command works outside the project directory'
else
	fail 'installed clash command works outside the project directory'
fi

HOME="$home_dir" SHELL=/bin/bash bash -c \
	'source "$HOME/.bashrc"; type clash >/dev/null; clash on >/dev/null; [[ "$http_proxy" == http://127.0.0.1:17890 ]]'
[[ $? -eq 0 ]] && pass 'installed bash loader activates wrapper' ||
	fail 'installed bash loader activates wrapper'

HOME="$home_dir" SHELL=/bin/zsh "$integration" install --shell zsh >/dev/null
[[ -s "$home_dir/.zshrc" ]] && pass 'explicit zsh install writes user zsh rc' ||
	fail 'explicit zsh install writes user zsh rc'

if command -v zsh >/dev/null 2>&1; then
	HOME="$home_dir" SHELL="$(command -v zsh)" zsh -c \
		'source "$HOME/.zshrc"; clash on >/dev/null; [[ "$http_proxy" == http://127.0.0.1:17890 ]] || exit 1; clash off >/dev/null; [[ -z ${http_proxy+x} ]]'
	[[ $? -eq 0 ]] && pass 'installed zsh wrapper toggles the current shell' ||
		fail 'installed zsh wrapper toggles the current shell'
fi


HOME="$home_dir" SHELL=/bin/bash "$integration" uninstall >/dev/null
if grep -Fq '# >>> clash_linux shell integration >>>' "$home_dir/.bashrc"; then
	fail 'uninstall removes managed loader'
else
	pass 'uninstall removes managed loader'
fi

[[ ! -e "$shim_path" ]] && pass 'uninstall removes owned shim' || fail 'uninstall removes owned shim'

# A generic marker is shared by old installs. Ownership is the exact loader
# body, so unrelated project blocks and user commands must survive.
foreign_project="$test_dir/foreign-project"
foreign_home="$test_dir/foreign-home"
mkdir -p "$foreign_project/scripts" "$foreign_home"
cp "$integration" "$foreign_project/scripts/shell_integration.sh"
HOME="$foreign_home" "$foreign_project/scripts/shell_integration.sh" install --shell bash >/dev/null
cat "$foreign_home/.bashrc" >>"$home_dir/.bashrc"
printf '%s\n' 'export USER_KEEP=yes' >>"$home_dir/.bashrc"
HOME="$home_dir" "$integration" install --shell bash >/dev/null
HOME="$home_dir" "$integration" uninstall --shell bash >/dev/null
if grep -Fq "foreign-project/scripts" "$home_dir/.bashrc" && grep -Fq 'USER_KEEP=yes' "$home_dir/.bashrc" &&
    ! grep -Fq 'project with spaces' "$home_dir/.bashrc"; then
    pass 'uninstall removes only this project block and retains foreign blocks and user content'
else
    fail 'uninstall removes only this project block and retains foreign blocks and user content'
fi
cp "$foreign_home/.local/bin/clash" "$shim_path"
cp "$home_dir/.bashrc" "$test_dir/rc-before-conflict"
HOME="$home_dir" "$integration" install --shell bash >/dev/null 2>"$test_dir/conflict-error"
[[ $? -ne 0 ]] && pass 'install refuses another project shim' || fail 'install refuses another project shim'
cmp -s "$home_dir/.bashrc" "$test_dir/rc-before-conflict" && pass 'shim conflict leaves rc untouched' || fail 'shim conflict leaves rc untouched'
HOME="$home_dir" "$integration" uninstall --shell bash >/dev/null
cmp -s "$shim_path" "$foreign_home/.local/bin/clash" && pass 'uninstall preserves foreign shim' || fail 'uninstall preserves foreign shim'

for test_shell in bash zsh; do
    command -v "$test_shell" >/dev/null 2>&1 || continue
    CLASH_TEST_INTEGRATION="$integration" "$test_shell" -c '
        eval "$("$CLASH_TEST_INTEGRATION" print)"
        clash on >/dev/null
        CLASH_TEST_UNINSTALL_STATUS=13 clash uninstall >/dev/null 2>&1
        [[ $? -eq 13 && "$http_proxy" == http://127.0.0.1:17890 ]] || exit 1
        typeset -f clash >/dev/null || exit 1
        clash uninstall --purge >/dev/null || exit 1
        [[ -z ${http_proxy+x} && -z ${_CLASH_LINUX_CLI+x} ]] || exit 1
        typeset -f clash >/dev/null && exit 1
        typeset -f _clash_apply_proxy_env >/dev/null && exit 1
        exit 0
    '
    [[ $? -eq 0 ]] && pass "$test_shell uninstall clears bindings only after success" || fail "$test_shell uninstall clears bindings only after success"
done

printf '\nShell integration: %d failed\n' "$failures"
[[ $failures -eq 0 ]]
