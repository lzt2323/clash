#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
readonly TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/subscription-health-test.XXXXXXXX")"
trap 'rm -rf -- "$TEST_DIR"' EXIT

fail() {
	printf 'subscription_health_test: FAIL: %s\n' "$*" >&2
	exit 1
}

assert_contains() {
	local value=$1 expected=$2 label=$3
	[[ "$value" == *"$expected"* ]] ||
		fail "${label}: expected output to contain <${expected}>"
}

fake_bin="${TEST_DIR}/bin"
mkdir -p "$fake_bin"
curl_calls="${TEST_DIR}/curl.calls"

cat >"${fake_bin}/curl" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$CURL_STUB_CALLS"
output=''
while [[ $# -gt 0 ]]; do
	case "$1" in
		--output)
			output=$2
			shift 2
			;;
		--write-out|--connect-timeout|--max-time|--header|--noproxy|--proxy)
			shift 2
			;;
		*)
			shift
			;;
	esac
done
case "${CURL_STUB_SCENARIO:-ready}" in
	ready)
		printf '%s' '{"updatedAt":"2026-07-23T12:00:00Z","url":"https://subscription.invalid/?token=never-print","proxies":[{"name":"a"},{"name":"b"}]}' >"$output"
		printf 200
		;;
	eventual)
		count=0
		[[ -f "$CURL_STUB_COUNTER" ]] && count=$(<"$CURL_STUB_COUNTER")
		count=$((count + 1))
		printf '%s\n' "$count" >"$CURL_STUB_COUNTER"
		if (( count == 1 )); then
			printf '%s' '{"updatedAt":"","proxies":[]}' >"$output"
		else
			printf '%s' '{"updatedAt":"2026-07-23T12:00:01Z","proxies":[{"name":"a"}]}' >"$output"
		fi
		printf 200
		;;
	missing)
		printf '%s' '{}' >"$output"
		printf 404
		;;
	empty)
		printf '%s' '{"updatedAt":"2026-07-23T12:00:00Z","proxies":[]}' >"$output"
		printf 200
		;;
	pending)
		printf '%s' '{"updatedAt":"","proxies":[{"name":"a"}]}' >"$output"
		printf 200
		;;
	api)
		exit 7
		;;
	connect-ok)
		printf 204
		;;
	connect-http-error)
		printf 503
		;;
	connect-fail)
		exit 28
		;;
esac
STUB
chmod 0755 "${fake_bin}/curl"

run_subscription() {
	PATH="${fake_bin}:/usr/bin:/bin" \
		CURL_STUB_CALLS="$curl_calls" \
		CURL_STUB_COUNTER="${TEST_DIR}/curl.counter" \
		CURL_STUB_SCENARIO="$1" \
		MIHOMO_API_SECRET='do-not-print-this-secret' \
		"$PROJECT_DIR/scripts/subscription_check.sh" \
		--api-url 'http://127.0.0.1:19090' --timeout "${2:-0}"
}

: >"$curl_calls"
output=$(run_subscription ready 2>&1)
assert_contains "$output" '2 proxies' 'ready provider proxy count'
assert_contains "$output" '2026-07-23T12:00:00Z' 'ready provider update time'
if grep -Fq 'do-not-print-this-secret' "$curl_calls" ||
	grep -Fq 'do-not-print-this-secret' <<<"$output"; then
	fail 'Secret leaked to output or curl arguments'
fi
if grep -Fq 'token=never-print' <<<"$output"; then
	fail 'subscription URL leaked from the API response'
fi

rm -f "${TEST_DIR}/curl.counter"
output=$(run_subscription eventual 3 2>&1)
assert_contains "$output" '1 proxies' 'provider becomes ready during polling'

for scenario in api missing empty pending; do
	set +e
	output=$(run_subscription "$scenario" 2>&1)
	status=$?
	set -e
	[[ "$status" -ne 0 ]] || fail "${scenario} scenario unexpectedly succeeded"
	case "$scenario" in
		api) assert_contains "$output" 'API is unreachable' 'API failure category' ;;
		missing) assert_contains "$output" 'does not exist' 'missing provider category' ;;
		empty) assert_contains "$output" 'contains no proxies' 'empty provider category' ;;
		pending) assert_contains "$output" 'first update' 'pending update category' ;;
	esac
	if grep -Fq 'do-not-print-this-secret' <<<"$output"; then
		fail "${scenario} output leaked Secret"
	fi
done

run_connectivity() {
	PATH="${fake_bin}:/usr/bin:/bin" \
		CURL_STUB_CALLS="$curl_calls" CURL_STUB_SCENARIO="$1" \
		"$PROJECT_DIR/scripts/connectivity_check.sh" \
		--proxy http://127.0.0.1:17890 \
		--url https://example.invalid/generate_204 --timeout 2
}

output=$(run_connectivity connect-ok 2>&1)
assert_contains "$output" 'succeeded' 'connectivity success'
for scenario in connect-http-error connect-fail; do
	set +e
	output=$(run_connectivity "$scenario" 2>&1)
	status=$?
	set -e
	[[ "$status" -ne 0 ]] || fail "${scenario} unexpectedly succeeded"
	assert_contains "$output" 'failed' "${scenario} failure"
done

if PATH="${fake_bin}:/usr/bin:/bin" \
	CURL_STUB_CALLS="$curl_calls" CURL_STUB_SCENARIO=connect-ok \
	"$PROJECT_DIR/scripts/connectivity_check.sh" \
	--proxy 'http://example.com:7890' \
	--url https://example.invalid/generate_204 >/dev/null 2>&1; then
	fail 'non-local proxy was accepted'
fi

grep -Fq 'elif [[ "$mode" == provider ]]' "$PROJECT_DIR/start.sh" ||
	fail 'start.sh does not gate subscription readiness on provider mode'

printf '%s\n' 'subscription and connectivity self-test: PASS'
