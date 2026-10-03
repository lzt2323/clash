#!/usr/bin/env bash
set -u

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
FIXTURE_DIR="$ROOT_DIR/tests/fixtures"
GENERATOR=${GENERATOR:-"$ROOT_DIR/scripts/generate_mihomo_config.sh"}
failures=0
passes=0
skips=0

pass() {
	printf 'ok - %s\n' "$1"
	passes=$((passes + 1))
}

fail() {
	printf 'not ok - %s\n' "$1" >&2
	failures=$((failures + 1))
}

skip() {
	printf 'ok - %s # SKIP %s\n' "$1" "$2"
	skips=$((skips + 1))
}

assert_contains() {
	local file=$1
	local pattern=$2
	local description=$3
	if grep -Eq -- "$pattern" "$file"; then
		pass "$description"
	else
		fail "$description"
	fi
}

test_shell_syntax() {
	local file
	local found=0
	while IFS= read -r -d '' file; do
		found=1
		if bash -n "$file"; then
			pass "bash syntax: ${file#"$ROOT_DIR/"}"
		else
			fail "bash syntax: ${file#"$ROOT_DIR/"}"
		fi
	done < <(find "$ROOT_DIR" \
		-path "$ROOT_DIR/.git" -prune -o \
		-path "$ROOT_DIR/tools" -prune -o \
		-path "$ROOT_DIR/runtime" -prune -o \
		-path "$ROOT_DIR/conf" -prune -o \
		-path "$ROOT_DIR/research-checkout" -prune -o \
		-type f -name '*.sh' -print0)
	[[ $found -eq 1 ]] || fail 'bash syntax: no shell scripts found'
}

test_fixture_contracts() {
	local fixture
	for fixture in provider-only.yaml local-profile.yaml; do
		if [[ -s "$FIXTURE_DIR/$fixture" ]]; then
			pass "fixture exists: $fixture"
		else
			fail "fixture exists: $fixture"
		fi
	done
	assert_contains "$FIXTURE_DIR/provider-only.yaml" '^dns:' \
		'provider-only fixture keeps keys before proxy-providers'
	assert_contains "$FIXTURE_DIR/provider-only.yaml" '^proxy-providers:' \
		'provider-only fixture has proxy-providers'
	assert_contains "$FIXTURE_DIR/provider-only.yaml" '^# fixture-marker: provider-only-tail$' \
		'provider-only fixture has a tail truncation marker'
	assert_contains "$FIXTURE_DIR/local-profile.yaml" '^# fixture-marker: local-profile-head$' \
		'local profile has a head truncation marker'
	assert_contains "$FIXTURE_DIR/local-profile.yaml" '^# fixture-marker: local-profile-tail$' \
		'local profile has a tail truncation marker'
}

test_generator_provider() {
	local work_dir output url secret user_agent download_proxy
	work_dir=$(mktemp -d "${TMPDIR:-/tmp}/clash-linux-test.XXXXXX") || {
		fail 'create temporary test directory'
		return
	}
	output="$work_dir/provider.yaml"
	url='https://example.invalid/sub?token=a:b&label="quoted"#fragment'
	secret='s3cret: "quotes" # hash'
	user_agent='clash-verge/v-test "quoted"'
	download_proxy='http://127.0.0.1:17890'

	if "$GENERATOR" provider \
		--url "$url" \
		--output "$output" \
		--secret "$secret" \
		--user-agent "$user_agent" \
		--download-proxy "$download_proxy" \
		--provider-path './providers/test subscription.yaml' \
		--interval 7200 \
		--size-limit 2097152 \
		--health-check-url 'https://example.invalid/generate_204?x=a:b' \
		--health-check-interval 600 \
		--health-check-timeout 7000 \
		--external-ui './dashboard/public'; then
		pass 'provider generator exits successfully'
	else
		fail 'provider generator exits successfully'
		return
	fi

	[[ -s "$output" ]] && pass 'provider generator creates output' ||
		fail 'provider generator creates output'
	assert_contains "$output" '^proxy-providers:' 'provider output has proxy-providers'
	assert_contains "$output" '^[[:space:]]+type:[[:space:]]+http([[:space:]]|$)' \
		'provider type is http'
	assert_contains "$output" '^[[:space:]]+interval:[[:space:]]+7200([[:space:]]|$)' \
		'provider update interval is preserved'
	assert_contains "$output" '^[[:space:]]+size-limit:[[:space:]]+2097152([[:space:]]|$)' \
		'provider size limit is preserved'
	if grep -Fq -- "$user_agent" "$output"; then
		pass 'provider User-Agent is preserved'
	else
		fail 'provider User-Agent is preserved'
	fi
	if grep -Fq -- "$download_proxy" "$output"; then
		pass 'provider download proxy is preserved'
	else
		fail 'provider download proxy is preserved'
	fi
	assert_contains "$output" '^[[:space:]]+path:[[:space:]]+.*test subscription\.yaml' \
		'provider cache path is preserved'
	assert_contains "$output" '^[[:space:]]+health-check:' \
		'provider health check is present'
	assert_contains "$output" '^[[:space:]]+timeout:[[:space:]]+7000([[:space:]]|$)' \
		'provider health-check timeout is preserved'
	assert_contains "$output" '^[[:space:]]+use:' \
		'proxy group consumes provider with use'

	# These metacharacters break YAML when emitted as an unquoted plain scalar.
	# Exact string presence also guards against shell evaluation or lossy escaping.
	if grep -Fq -- "$url" "$output"; then
		pass 'subscription URL survives YAML escaping'
	else
		fail 'subscription URL survives YAML escaping'
	fi
	if grep -Fq -- "$secret" "$output"; then
		pass 'secret survives YAML escaping'
	else
		fail 'secret survives YAML escaping'
	fi
	if grep -Eq '^[[:space:]]*url:[[:space:]]*["'\'']' "$output"; then
		pass 'subscription URL is emitted as a quoted YAML scalar'
	else
		fail 'subscription URL is emitted as a quoted YAML scalar'
	fi
	if grep -Eq '^[[:space:]]*secret:[[:space:]]*["'\'']' "$output"; then
		pass 'secret is emitted as a quoted YAML scalar'
	else
		fail 'secret is emitted as a quoted YAML scalar'
	fi

	rm -f "$output"
	rmdir "$work_dir"
}

test_generator_local_profiles() {
	local work_dir fixture output
	work_dir=$(mktemp -d "${TMPDIR:-/tmp}/clash-linux-test.XXXXXX") || {
		fail 'create temporary test directory'
		return
	}
	for fixture in provider-only.yaml local-profile.yaml; do
		output="$work_dir/$fixture"
		if "$GENERATOR" local-profile \
			--input "$FIXTURE_DIR/$fixture" \
			--output "$output"; then
			pass "local-profile accepts $fixture"
		else
			fail "local-profile accepts $fixture"
			continue
		fi
		if cmp -s "$FIXTURE_DIR/$fixture" "$output"; then
			pass "local-profile does not truncate $fixture"
		else
			fail "local-profile does not truncate $fixture"
		fi
	done
	rm -f "$work_dir/provider-only.yaml" "$work_dir/local-profile.yaml"
	rmdir "$work_dir"
}

test_shell_syntax
test_fixture_contracts

if [[ -x "$GENERATOR" ]]; then
	test_generator_provider
	test_generator_local_profiles
else
	skip 'provider generator contract' \
		'scripts/generate_mihomo_config.sh is not present/executable yet'
	skip 'local-profile no-truncation contract' \
		'scripts/generate_mihomo_config.sh is not present/executable yet'
fi

if bash "$ROOT_DIR/tests/ux_tools_test.sh"; then
	pass 'doctor and dashboard offline tests'
else
	fail 'doctor and dashboard offline tests'
fi

if bash "$ROOT_DIR/tests/shell_integration_test.sh"; then
	pass 'persistent shell integration tests'
else
	fail 'persistent shell integration tests'
fi

if bash "$ROOT_DIR/tests/cli_test.sh"; then
	pass 'CLI command tests'
else
	fail 'CLI command tests'
fi

if bash "$ROOT_DIR/tests/selector_switch_test.sh"; then
	pass 'fast fuzzy selector tests'
else
	fail 'fast fuzzy selector tests'
fi

if bash "$ROOT_DIR/scripts/tests/subscription_health_test.sh"; then
	pass 'subscription readiness and connectivity tests'
else
	fail 'subscription readiness and connectivity tests'
fi

if python3 -B "$ROOT_DIR/scripts/tests/mihomo_lifecycle_test.py"; then
	pass 'Mihomo startup lifecycle and secret handling tests'
else
	fail 'Mihomo startup lifecycle and secret handling tests'
fi

if python3 -B "$ROOT_DIR/tests/terminal_worker_test.py"; then
	pass 'workbench worker protocol and cancellation tests'
else
	fail 'workbench worker protocol and cancellation tests'
fi

if python3 -B "$ROOT_DIR/tests/workbench_pty_test.py"; then
	pass 'curses workbench real PTY behavior tests'
else
	fail 'curses workbench real PTY behavior tests'
fi

if python3 -B "$ROOT_DIR/tests/terminal_workbench_test.py"; then
	pass 'workbench layout and keyboard unit tests'
else
	fail 'workbench layout and keyboard unit tests'
fi

for suite in uninstall package export_source; do
    if python3 -B "$ROOT_DIR/tests/${suite}_test.py"; then
        pass "$suite tests"
    else
        fail "$suite tests"
    fi
done
if bash "$ROOT_DIR/tests/installer_test.sh"; then
    pass 'one-click installer tests'
else
    fail 'one-click installer tests'
fi

printf '\n%d passed, %d skipped, %d failed\n' "$passes" "$skips" "$failures"
[[ $failures -eq 0 ]]
