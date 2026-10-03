#!/usr/bin/env bash
set -euo pipefail

api_url="${MIHOMO_API_URL:-http://127.0.0.1:9090}"
secret="${MIHOMO_API_SECRET:-${CLASH_SECRET:-}}"
timeout="${SUBSCRIPTION_READY_TIMEOUT:-30}"
interval="${SUBSCRIPTION_READY_INTERVAL:-1}"

usage() {
	cat <<EOF
Usage: $0 [--api-url URL] [--secret SECRET] [--timeout SECONDS] [--interval SECONDS]

Wait until Mihomo's "subscription" proxy-provider has completed an update and
contains at least one proxy.

Environment defaults:
  MIHOMO_API_URL, MIHOMO_API_SECRET (or CLASH_SECRET)
  SUBSCRIPTION_READY_TIMEOUT, SUBSCRIPTION_READY_INTERVAL
EOF
}

die() {
	local code=$1
	shift
	printf 'ERROR: %s\n' "$*" >&2
	exit "$code"
}

require_value() {
	[[ -n "${2-}" ]] || die 2 "$1 requires a non-empty value"
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--api-url|--secret|--timeout|--interval)
			[[ $# -ge 2 ]] || die 2 "$1 requires a value"
			option=$1
			value=$2
			shift 2
			case "$option" in
				--api-url) api_url=$value ;;
				--secret) secret=$value ;;
				--timeout) timeout=$value ;;
				--interval) interval=$value ;;
			esac
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			die 2 "unknown option: $1"
			;;
	esac
done

require_value "--api-url" "$api_url"
[[ "$api_url" != *$'\n'* && "$api_url" != *$'\r'* &&
	"$api_url" =~ ^https?://[^[:space:]]+$ ]] ||
	die 2 "--api-url must be an http:// or https:// URL without whitespace"
[[ "$secret" != *$'\n'* && "$secret" != *$'\r'* ]] ||
	die 2 "--secret must not contain line breaks"
[[ "$timeout" =~ ^[0-9]+$ ]] ||
	die 2 "--timeout must be a non-negative integer"
[[ "$interval" =~ ^[1-9][0-9]*$ ]] ||
	die 2 "--interval must be a positive integer"
command -v curl >/dev/null 2>&1 || die 2 "curl is required"
if ! command -v jq >/dev/null 2>&1 && ! command -v python3 >/dev/null 2>&1; then
	die 2 "jq or python3 is required to parse the Mihomo API response"
fi

work_dir=$(mktemp -d "${TMPDIR:-/tmp}/mihomo-subscription-check.XXXXXXXX")
response_file="${work_dir}/response.json"
auth_file=''
cleanup() {
	rm -rf -- "$work_dir"
}
trap cleanup EXIT HUP INT TERM

curl_auth_args=()
if [[ -n "$secret" ]]; then
	auth_file="${work_dir}/authorization.header"
	umask 077
	printf 'Authorization: Bearer %s\n' "$secret" >"$auth_file"
	curl_auth_args+=(--header "@${auth_file}")
fi

parse_provider() {
	if command -v jq >/dev/null 2>&1; then
		jq -er '
			(.updatedAt // "") as $updated |
			(.proxies // null) as $proxies |
			if ($proxies | type) != "array" then error("missing proxies")
			else $updated, ($proxies | length | tostring)
			end
		' "$response_file" 2>/dev/null
	else
		python3 - "$response_file" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as stream:
        provider = json.load(stream)
    proxies = provider["proxies"]
    updated = provider.get("updatedAt", "")
    if not isinstance(proxies, list) or not isinstance(updated, str):
        raise ValueError
    if any(character in updated for character in "\t\r\n"):
        raise ValueError
except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
    raise SystemExit(1)
print(updated)
print(len(proxies))
PY
	fi
}

endpoint="${api_url%/}/providers/proxies/subscription"
deadline=$((SECONDS + timeout))
last_state=api

while :; do
	set +e
	http_code=$(
		curl --noproxy '*' --silent \
			--connect-timeout 2 --max-time 3 \
			--output "$response_file" --write-out '%{http_code}' \
			"${curl_auth_args[@]}" "$endpoint"
	)
	curl_status=$?
	set -e

	if [[ "$curl_status" -ne 0 ]]; then
		last_state=api
	elif [[ "$http_code" == 404 ]]; then
		last_state=missing
	elif [[ "$http_code" =~ ^2[0-9][0-9]$ ]]; then
		if provider_state=$(parse_provider); then
			updated_at=${provider_state%%$'\n'*}
			proxy_count=${provider_state#*$'\n'}
			if [[ -z "$updated_at" || "$updated_at" == 0001-* ]]; then
				last_state=pending
			elif [[ ! "$updated_at" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+Z?$ ]]; then
				last_state=invalid
			elif [[ ! "$proxy_count" =~ ^[0-9]+$ ]]; then
				last_state=invalid
			elif (( proxy_count == 0 )); then
				last_state=empty
			else
				printf 'Subscription is ready (%s proxies, updated %s).\n' \
					"$proxy_count" "$updated_at"
				exit 0
			fi
		else
			last_state=invalid
		fi
	else
		last_state=api
	fi

	(( SECONDS >= deadline )) && break
	sleep "$interval"
done

case "$last_state" in
	api)
		die 10 "Mihomo API is unreachable or rejected the readiness request"
		;;
	missing)
		die 11 'Mihomo provider "subscription" does not exist'
		;;
	empty)
		die 12 'Mihomo provider "subscription" updated but contains no proxies'
		;;
	pending)
		die 13 'Mihomo provider "subscription" has not completed its first update'
		;;
	*)
		die 14 'Mihomo provider returned an invalid readiness response'
		;;
esac
