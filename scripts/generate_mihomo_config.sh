#!/usr/bin/env bash

set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly DEFAULT_TEMPLATE="${PROJECT_DIR}/temp/mihomo_config.template.yaml"

usage() {
  cat <<'EOF'
Generate a candidate Mihomo configuration and atomically install it.

Usage:
  generate_mihomo_config.sh provider --url URL --output FILE [OPTIONS]
  generate_mihomo_config.sh local --input FILE --output FILE
  generate_mihomo_config.sh remote-profile --url URL --output FILE

Modes:
  provider        Build a complete Mihomo config using an HTTP proxy-provider.
  local           Copy a complete local Mihomo YAML profile without truncation
                  ("local-profile" is accepted as a descriptive alias).
  remote-profile  Reserved interface; remote full-profile download is not
                  implemented yet and exits with an explicit error.

Provider options:
  --url URL                    Subscription URL (http or https).
  --output FILE                Destination candidate config.
  --secret VALUE               REST API secret (default: empty).
  --user-agent VALUE           Subscription User-Agent
                               (default: clash-verge/v2.5.2).
  --download-proxy VALUE       Optional proxy/group used to fetch the provider.
                               (--proxy is accepted as a short alias.)
  --provider-path PATH         Provider cache path
                               (default: ./providers/subscription.yaml).
  --interval SECONDS           Provider update interval (default: 3600).
  --size-limit BYTES           Provider response limit (default: 10485760).
  --health-check-url URL       Health-check URL
                               (default: https://www.gstatic.com/generate_204).
  --health-check-interval SEC  Health-check interval (default: 300).
  --health-check-timeout MS    Health-check timeout (default: 5000).
  --port PORT                  HTTP proxy port (default: 7890).
  --socks-port PORT            SOCKS5 proxy port (default: 7891).
  --redir-port PORT            Redir proxy port (default: 7892).
  --controller ADDRESS         REST controller (default: 0.0.0.0:9090).
  --external-ui PATH           Dashboard directory relative to Mihomo home
                               (default: ./ui).
                               (--dashboard-dir is accepted as an alias.)
  --template FILE              Alternate generator template.
  -h, --help                   Show this help.

All dynamic YAML strings are emitted as YAML single-quoted scalars. Quotes in
URLs, query strings, secrets, paths, and User-Agent values are escaped safely.
EOF
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

reject_line_breaks() {
  local label=$1 value=$2
  [[ "$value" != *$'\n'* && "$value" != *$'\r'* ]] ||
    die "${label} must not contain line breaks"
}

yaml_quote() {
  local value=$1
  reject_line_breaks "YAML value" "$value"
  value=${value//\'/\'\'}
  printf "'%s'" "$value"
}

require_value() {
  local option=$1 value=${2-}
  [[ -n "$value" ]] || die "${option} requires a non-empty value"
}

validate_positive_integer() {
  local option=$1 value=$2
  [[ "$value" =~ ^[1-9][0-9]*$ ]] ||
    die "${option} must be a positive integer"
}

validate_port() {
  local option=$1 value=$2
  validate_positive_integer "$option" "$value"
  (( value <= 65535 )) || die "${option} must be between 1 and 65535"
}

validate_http_url() {
  local option=$1 value=$2
  reject_line_breaks "$option" "$value"
  [[ "$value" =~ ^https?://[^[:space:]]+$ ]] ||
    die "${option} must be a non-empty http:// or https:// URL without whitespace"
}

make_candidate() {
  local output=$1 output_dir output_name
  output_dir=$(dirname -- "$output")
  output_name=$(basename -- "$output")
  [[ -d "$output_dir" ]] || die "output directory does not exist: ${output_dir}"
  mktemp "${output_dir}/.${output_name}.tmp.XXXXXX"
}

write_runtime() {
  printf 'port: %s\n' "$port"
  printf 'socks-port: %s\n' "$socks_port"
  printf 'redir-port: %s\n' "$redir_port"
  printf 'allow-lan: true\n'
  printf 'external-controller: %s\n' "$(yaml_quote "$controller")"
  printf 'secret: %s\n' "$(yaml_quote "$secret")"
  printf 'external-ui: %s\n' "$(yaml_quote "$external_ui")"
}

write_provider() {
  printf 'proxy-providers:\n'
  printf '  subscription:\n'
  printf '    type: http\n'
  printf '    url: %s\n' "$(yaml_quote "$url")"
  printf '    path: %s\n' "$(yaml_quote "$provider_path")"
  printf '    interval: %s\n' "$interval"
  printf '    size-limit: %s\n' "$size_limit"
  printf '    header:\n'
  printf '      User-Agent:\n'
  printf '        - %s\n' "$(yaml_quote "$user_agent")"
  if [[ -n "$download_proxy" ]]; then
    printf '    proxy: %s\n' "$(yaml_quote "$download_proxy")"
  fi
  printf '    health-check:\n'
  printf '      enable: true\n'
  printf '      url: %s\n' "$(yaml_quote "$health_check_url")"
  printf '      interval: %s\n' "$health_check_interval"
  printf '      timeout: %s\n' "$health_check_timeout"
  printf '      lazy: true\n'
}

render_provider_config() {
  local template=$1 candidate=$2 line
  local runtime_marker=0 provider_marker=0

  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      '# @generate-runtime@')
        ((runtime_marker += 1))
        write_runtime
        ;;
      '# @generate-provider@')
        ((provider_marker += 1))
        write_provider
        ;;
      *)
        printf '%s\n' "$line"
        ;;
    esac
  done < "$template" > "$candidate"

  [[ "$runtime_marker" -eq 1 ]] ||
    die "template must contain exactly one # @generate-runtime@ marker"
  [[ "$provider_marker" -eq 1 ]] ||
    die "template must contain exactly one # @generate-provider@ marker"
}

[[ $# -gt 0 ]] || {
  usage >&2
  exit 2
}

mode=$1
shift

if [[ "$mode" == '-h' || "$mode" == '--help' ]]; then
  usage
  exit 0
fi

url=''
input=''
output=''
secret=''
user_agent='clash-verge/v2.5.2'
download_proxy=''
provider_path='./providers/subscription.yaml'
interval=3600
size_limit=10485760
health_check_url='https://www.gstatic.com/generate_204'
health_check_interval=300
health_check_timeout=5000
port=7890
socks_port=7891
redir_port=7892
controller='0.0.0.0:9090'
external_ui='./ui'
template=$DEFAULT_TEMPLATE

while [[ $# -gt 0 ]]; do
  case "$1" in
    --url|--input|--output|--secret|--user-agent|--download-proxy|--proxy|\
    --provider-path|--interval|--size-limit|--health-check-url|\
    --health-check-interval|--health-check-timeout|--port|--socks-port|--redir-port|\
    --controller|--external-ui|--dashboard-dir|--template)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      option=$1
      value=$2
      shift 2
      case "$option" in
        --url) url=$value ;;
        --input) input=$value ;;
        --output) output=$value ;;
        --secret) secret=$value ;;
        --user-agent) user_agent=$value ;;
        --download-proxy|--proxy) download_proxy=$value ;;
        --provider-path) provider_path=$value ;;
        --interval) interval=$value ;;
        --size-limit) size_limit=$value ;;
        --health-check-url) health_check_url=$value ;;
        --health-check-interval) health_check_interval=$value ;;
        --health-check-timeout) health_check_timeout=$value ;;
        --port) port=$value ;;
        --socks-port) socks_port=$value ;;
        --redir-port) redir_port=$value ;;
        --controller) controller=$value ;;
        --external-ui|--dashboard-dir) external_ui=$value ;;
        --template) template=$value ;;
      esac
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1 (use --help for usage)"
      ;;
  esac
done

require_value "--output" "$output"
reject_line_breaks "--output" "$output"

candidate=''
cleanup() {
  if [[ -n "$candidate" && -e "$candidate" ]]; then
    rm -f -- "$candidate"
  fi
}
trap cleanup EXIT HUP INT TERM

case "$mode" in
  provider)
    require_value "--url" "$url"
    [[ -z "$input" ]] || die "--input is only valid in local-profile mode"
    [[ -r "$template" && -f "$template" ]] ||
      die "template is not a readable regular file: ${template}"

    validate_http_url "--url" "$url"
    validate_http_url "--health-check-url" "$health_check_url"
    require_value "--user-agent" "$user_agent"
    require_value "--provider-path" "$provider_path"
    require_value "--controller" "$controller"
    require_value "--external-ui" "$external_ui"
    for item in "$secret" "$user_agent" "$download_proxy" "$provider_path" \
      "$controller" "$external_ui"; do
      reject_line_breaks "option value" "$item"
    done
    validate_positive_integer "--interval" "$interval"
    validate_positive_integer "--size-limit" "$size_limit"
    validate_positive_integer "--health-check-interval" "$health_check_interval"
    validate_positive_integer "--health-check-timeout" "$health_check_timeout"
    validate_port "--port" "$port"
    validate_port "--socks-port" "$socks_port"
    validate_port "--redir-port" "$redir_port"

    candidate=$(make_candidate "$output")
    render_provider_config "$template" "$candidate"
    ;;
  local|local-profile)
    require_value "--input" "$input"
    [[ -z "$url" ]] || die "--url is not valid in local mode"
    [[ -f "$input" && -r "$input" ]] ||
      die "input is not a readable regular file: ${input}"
    [[ -s "$input" ]] || die "input profile is empty: ${input}"

    candidate=$(make_candidate "$output")
    cp -- "$input" "$candidate"
    ;;
  remote-profile)
    require_value "--url" "$url"
    validate_http_url "--url" "$url"
    die "remote-profile mode is reserved but downloading full profiles is not implemented; download the YAML explicitly and use local"
    ;;
  -h|--help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    die "unknown mode: ${mode}"
    ;;
esac

chmod 600 "$candidate"
mv -f -- "$candidate" "$output"
candidate=''
printf 'Wrote Mihomo configuration: %s\n' "$output"
