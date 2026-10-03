#!/usr/bin/env bash
set -euo pipefail

readonly ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly SELECTOR="${ROOT_DIR}/scripts/clash_proxy-selector.sh"
readonly TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/selector-switch-test.XXXXXXXX")"
readonly REQUEST_LOG="${TEST_DIR}/requests.log"
trap 'rm -rf -- "$TEST_DIR"' EXIT

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
    local value=$1
    local expected=$2
    local description=$3

    [[ "$value" == *"$expected"* ]] || {
        printf 'expected output to contain <%s>:\n%s\n' "$expected" "$value" >&2
        fail "$description"
    }
    pass "$description"
}

mkdir -p "${TEST_DIR}/bin"
curl_stub="${TEST_DIR}/bin/curl"
cat >"$curl_stub" <<'STUB'
#!/usr/bin/env bash
set -euo pipefail

url="${!#}"
printf '%s\n' "$*" >>"$SELECTOR_REQUEST_LOG"

if [[ "$*" == *"-XPUT"* ]]; then
    exit 0
fi
if [[ "$url" == */providers/proxies/*/healthcheck* ]]; then
    exit 0
fi
if [[ "$url" == */providers/proxies ]]; then
    if [[ "${SELECTOR_EMPTY_PROVIDER_HISTORY:-0}" == 1 ]]; then
        cat <<'JSON'
{
  "providers": {
    "subscription": {
      "vehicleType": "HTTP",
      "proxies": [
        {"name": "Provider Los Angeles", "history": []},
        {"name": "Provider Tokyo", "history": []}
      ]
    }
  }
}
JSON
        exit 0
    fi
    cat <<'JSON'
{
  "providers": {
    "subscription": {
      "vehicleType": "HTTP",
      "proxies": [
        {
          "name": "Provider Los Angeles",
          "history": [{"time": "2026-01-01T00:00:00Z", "delay": 0}]
        },
        {
          "name": "Provider Tokyo",
          "history": [{"time": "2026-01-01T00:00:00Z", "delay": 123}]
        }
      ]
    }
  }
}
JSON
    exit 0
fi

if [[ "$url" == */proxies ]]; then
    cat <<'JSON'
{
  "proxies": {
    "🚀 Proxy Group": {
      "type": "Selector",
      "now": "🇭🇰 Hong Kong 01",
      "all": [
        "🇭🇰 Hong Kong 01",
        "🇺🇸 New York Fast",
        "Tokyo Premium"
      ]
    },
    "Provider AUTO": {
      "type": "URLTest",
      "now": "Provider Tokyo",
      "all": ["Provider Los Angeles", "Provider Tokyo"]
    },
    "🇭🇰 Hong Kong 01": {
      "type": "VLESS",
      "history": [{"time": "2026-01-01T00:00:00Z", "delay": 42}]
    },
    "🇺🇸 New York Fast": {
      "type": "VLESS",
      "history": []
    },
    "Tokyo Premium": {
      "type": "VLESS",
      "history": [{"time": "2026-01-01T00:00:00Z", "delay": 88}]
    }
  }
}
JSON
    exit 0
fi

printf 'unexpected request: %s\n' "$*" >&2
exit 1
STUB
chmod 0755 "$curl_stub"

run_selector() {
    PATH="${TEST_DIR}/bin:${PATH}" \
    SELECTOR_REQUEST_LOG="$REQUEST_LOG" \
    CLASH_API_URL="http://127.0.0.1:19090" \
    "$SELECTOR" "$@"
}

run_selector_without_provider_history() {
    PATH="${TEST_DIR}/bin:${PATH}" \
    SELECTOR_REQUEST_LOG="$REQUEST_LOG" \
    SELECTOR_EMPTY_PROVIDER_HISTORY=1 \
    CLASH_API_URL="http://127.0.0.1:19090" \
    "$SELECTOR" "$@"
}

: >"$REQUEST_LOG"
output="$(printf '1\n' | run_selector switch hong '🚀 Proxy Group')"
assert_contains "$output" "🇭🇰 Hong Kong 01" "fuzzy search is case-insensitive"
assert_contains "$output" "42ms" "switch shows an existing delay"
assert_contains "$output" "已切换为：🇭🇰 Hong Kong 01" "matching node is switched"

request_log="$(<"$REQUEST_LOG")"
assert_contains "$request_log" "proxies/%F0%9F%9A%80%20Proxy%20Group" \
    "group with Emoji and spaces is URL-encoded"
assert_contains "$request_log" '"name":"🇭🇰 Hong Kong 01"' \
    "PUT payload preserves Emoji and spaces"
if [[ "$request_log" == *"/delay?"* ]]; then
    fail "switch must not trigger delay measurements"
fi
pass "switch does not trigger delay measurements"

: >"$REQUEST_LOG"
output="$(printf '1\n' | run_selector switch 'NEW YORK' '🚀 Proxy Group')"
assert_contains "$output" "🇺🇸 New York Fast" "query with spaces matches case-insensitively"
assert_contains "$output" "未测速" "node without history is marked untested"

: >"$REQUEST_LOG"
output="$(printf '1\n' | run_selector switch '🇺🇸' '🚀 Proxy Group')"
assert_contains "$output" "🇺🇸 New York Fast" "Emoji query filters nodes"

: >"$REQUEST_LOG"
output="$(printf '1\n2\n' | run_selector switch)"
assert_contains "$output" "🇺🇸 New York Fast" "switch without query lists nodes directly"
assert_contains "$output" "已切换为：🇺🇸 New York Fast" \
    "switch without query selects by number"

: >"$REQUEST_LOG"
output="$(run_selector switch '🚀 Proxy Group' 'Tokyo Premium')"
assert_contains "$output" "已切换为：Tokyo Premium" \
    "legacy switch GROUP NODE syntax remains compatible"

: >"$REQUEST_LOG"
if run_selector switch singapore '🚀 Proxy Group' >"${TEST_DIR}/no-match.out" 2>&1; then
    fail "no-match query fails"
fi
assert_contains "$(<"${TEST_DIR}/no-match.out")" "没有找到匹配" \
    "no-match query has a clear error"

: >"$REQUEST_LOG"
output="$(printf '2\n' | run_selector switch '' 'Provider AUTO')"
assert_contains "$output" "123ms    Provider Tokyo" \
    "switch reuses provider delay history"
assert_contains "$output" "未测速 Provider Los Angeles" \
    "provider zero delay remains untested in switch"
request_log="$(<"$REQUEST_LOG")"
if [[ "$request_log" == *"/healthcheck?"* || "$request_log" == *"/delay?"* ]]; then
    fail "switch must only read provider history"
fi
pass "switch reads provider history without starting another measurement"

: >"$REQUEST_LOG"
output="$(printf '2\n' | run_selector_without_provider_history switch '' 'Provider AUTO')"
assert_contains "$output" "未测速 Provider Tokyo" \
    "fresh startup without provider history is shown accurately"

: >"$REQUEST_LOG"
output="$(run_selector delay 'Provider AUTO')"
assert_contains "$output" "123ms" \
    "provider-backed node uses provider health-check history"
assert_contains "$output" "失败" \
    "provider-backed zero delay is reported as failure, not timeout"
request_log="$(<"$REQUEST_LOG")"
assert_contains "$request_log" \
    "providers/proxies/subscription/healthcheck?timeout=5000&url=https%3A%2F%2Fcp.cloudflare.com%2Fgenerate_204" \
    "provider-backed group uses Mihomo provider health-check API"
if [[ "$request_log" == *"Provider%20Tokyo/delay?"* ]]; then
    fail "provider-backed node must not use the unavailable top-level proxy delay API"
fi
pass "provider-backed nodes avoid the unavailable top-level delay API"

printf '\n%d selector switch tests passed\n' "$passes"
