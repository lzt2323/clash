#!/usr/bin/env bash
set -euo pipefail

proxy_url="${CLASH_HTTP_PROXY:-http://127.0.0.1:7890}"
test_url="${CONNECTIVITY_TEST_URL:-https://cp.cloudflare.com/generate_204}"
timeout="${CONNECTIVITY_CHECK_TIMEOUT:-10}"

usage() {
	cat <<EOF
Usage: $0 [--proxy URL] [--url URL] [--timeout SECONDS]

Verify outbound connectivity through a local HTTP proxy.

Environment defaults:
  CLASH_HTTP_PROXY, CONNECTIVITY_TEST_URL, CONNECTIVITY_CHECK_TIMEOUT
EOF
}

die() {
	printf 'ERROR: %s\n' "$*" >&2
	exit 1
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--proxy|--url|--timeout)
			[[ $# -ge 2 && -n "$2" ]] || die "$1 requires a non-empty value"
			option=$1
			value=$2
			shift 2
			case "$option" in
				--proxy) proxy_url=$value ;;
				--url) test_url=$value ;;
				--timeout) timeout=$value ;;
			esac
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			die "unknown option: $1"
			;;
	esac
done

[[ "$proxy_url" != *$'\n'* && "$proxy_url" != *$'\r'* &&
	"$proxy_url" =~ ^http://(127\.0\.0\.1|localhost|\[::1\]):([1-9][0-9]*)/?$ ]] ||
	die "--proxy must be a local HTTP proxy URL"
proxy_port=${BASH_REMATCH[2]}
(( proxy_port <= 65535 )) || die "--proxy port must be between 1 and 65535"
[[ "$test_url" != *$'\n'* && "$test_url" != *$'\r'* &&
	"$test_url" =~ ^https?://[^[:space:]]+$ ]] ||
	die "--url must be an http:// or https:// URL without whitespace"
[[ "$timeout" =~ ^[1-9][0-9]*$ ]] ||
	die "--timeout must be a positive integer"
command -v curl >/dev/null 2>&1 || die "curl is required"

set +e
http_code=$(
	curl --silent --output /dev/null --write-out '%{http_code}' \
		--connect-timeout "$timeout" --max-time "$timeout" \
		--proxy "$proxy_url" "$test_url"
)
curl_status=$?
set -e

if [[ "$curl_status" -ne 0 ]]; then
	die "connectivity through the local HTTP proxy failed (curl exit ${curl_status})"
fi
if [[ ! "$http_code" =~ ^[23][0-9][0-9]$ ]]; then
	die "connectivity through the local HTTP proxy failed (HTTP ${http_code})"
fi

printf 'Connectivity through the local HTTP proxy succeeded (HTTP %s).\n' "$http_code"
