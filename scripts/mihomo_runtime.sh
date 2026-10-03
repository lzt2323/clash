#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly RUNTIME_DIR="${MIHOMO_RUNTIME_DIR:-${PROJECT_DIR}/runtime}"
readonly PID_FILE="${RUNTIME_DIR}/mihomo.pid"

default_mihomo_binary() {
    local bundled=''

    if [[ -x "${PROJECT_DIR}/bin/mihomo" ]]; then
        printf '%s\n' "${PROJECT_DIR}/bin/mihomo"
        return
    fi

    case "$(uname -m 2>/dev/null || true)" in
        x86_64|amd64) bundled="${PROJECT_DIR}/bin/clash-linux-amd64" ;;
        aarch64|arm64) bundled="${PROJECT_DIR}/bin/clash-linux-arm64" ;;
        armv7l|armv7|arm32v7) bundled="${PROJECT_DIR}/bin/clash-linux-armv7" ;;
    esac
    if [[ -n "$bundled" && -x "$bundled" ]]; then
        printf '%s\n' "$bundled"
    else
        printf '%s\n' "${PROJECT_DIR}/bin/mihomo"
    fi
}

readonly DEFAULT_BINARY="$(default_mihomo_binary)"

MIHOMO_BINARY="${MIHOMO_BINARY:-$DEFAULT_BINARY}"
MIHOMO_CONFIG_DIR="${MIHOMO_CONFIG_DIR:-${PROJECT_DIR}/conf}"
MIHOMO_CONFIG="${MIHOMO_CONFIG:-${MIHOMO_CONFIG_DIR}/config.yaml}"
MIHOMO_LOG="${MIHOMO_LOG:-${PROJECT_DIR}/logs/mihomo.log}"
MIHOMO_API_URL="${MIHOMO_API_URL:-http://127.0.0.1:9090}"
MIHOMO_START_TIMEOUT="${MIHOMO_START_TIMEOUT:-15}"
MIHOMO_STOP_TIMEOUT="${MIHOMO_STOP_TIMEOUT:-10}"

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

require_binary() {
    [[ -x "$MIHOMO_BINARY" ]] ||
        die "Mihomo is not installed at ${MIHOMO_BINARY}; run scripts/install_mihomo.sh"
}

read_pid() {
    local pid
    [[ -f "$PID_FILE" ]] || return 1
    IFS= read -r pid <"$PID_FILE"
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    printf '%s\n' "$pid"
}

pid_is_mihomo() {
    local pid="$1"
    local expected actual

    kill -0 "$pid" 2>/dev/null || return 1
    [[ -e "/proc/${pid}/exe" ]] || return 1
    expected="$(readlink -f -- "$MIHOMO_BINARY")"
    actual="$(readlink -f -- "/proc/${pid}/exe")"
    [[ "$actual" == "$expected" ]]
}

api_request() (
    local secret auth_file='' request_timeout=${1:-3} connect_timeout=2
    (( request_timeout < connect_timeout )) && connect_timeout=$request_timeout
    local curl_args=(
        --fail --silent --show-error
        --connect-timeout "$connect_timeout" --max-time "$request_timeout" --noproxy '*'
    )
    trap '[[ -z "$auth_file" ]] || rm -f -- "$auth_file"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' HUP TERM
    secret="${MIHOMO_API_SECRET:-}"
    if [[ -z "$secret" && -r "$MIHOMO_CONFIG" ]]; then
        secret="$(
            awk '
                /^secret:[[:space:]]*/ {
                    sub(/^secret:[[:space:]]*/, "")
                    sub(/[[:space:]]+#.*$/, "")
                    print
                    exit
                }
            ' "$MIHOMO_CONFIG"
        )"
        if [[ "$secret" == \"*\" && "$secret" == *\" ]]; then
            secret="${secret:1:${#secret}-2}"
        elif [[ "$secret" == \'*\' && "$secret" == *\' ]]; then
            secret="${secret:1:${#secret}-2}"
        fi
    fi
    if [[ -n "$secret" ]]; then
        auth_file=$(umask 077; mktemp "${TMPDIR:-/tmp}/clash-api-auth.XXXXXX") || return 1
        printf 'Authorization: Bearer %s\n' "$secret" >"$auth_file"
        curl_args+=(--header "@${auth_file}")
    fi
    curl "${curl_args[@]}" "${MIHOMO_API_URL%/}/version"
)

command_binary() {
    require_binary
    printf '%s\n' "$MIHOMO_BINARY"
}

command_validate() {
    require_binary
    [[ -r "$MIHOMO_CONFIG" ]] || die "configuration is not readable: $MIHOMO_CONFIG"
    "$MIHOMO_BINARY" -t -d "$MIHOMO_CONFIG_DIR" -f "$MIHOMO_CONFIG"
}

command_health() {
    local pid
    pid="$(read_pid)" || die "Mihomo is not running (PID file missing or invalid)"
    pid_is_mihomo "$pid" || die "PID file does not identify the configured Mihomo binary"
    api_request >/dev/null || die "Mihomo process is alive but API is unhealthy: $MIHOMO_API_URL"
    printf 'Mihomo is healthy (pid %s, API %s).\n' "$pid" "$MIHOMO_API_URL"
}

command_version() {
    require_binary
    "$MIHOMO_BINARY" -v
}

cleanup_start() {
    local owned_pid=$1 deadline
    [[ "$owned_pid" =~ ^[1-9][0-9]*$ ]] || return 0
    # This PID is the child created by this invocation, including before exec.
    # Stored PIDs continue to require pid_is_mihomo before they are signalled.
    if kill -0 "$owned_pid" 2>/dev/null; then
        kill -TERM "$owned_pid" 2>/dev/null || true
        deadline=$((SECONDS + MIHOMO_STOP_TIMEOUT))
        while kill -0 "$owned_pid" 2>/dev/null && (( SECONDS < deadline )); do
            sleep 0.1
        done
        if kill -0 "$owned_pid" 2>/dev/null; then
            kill -KILL "$owned_pid" 2>/dev/null || true
        fi
    fi
    wait "$owned_pid" 2>/dev/null || true
    if [[ "$(read_pid 2>/dev/null || true)" == "$owned_pid" ]]; then
        rm -f -- "$PID_FILE"
    fi
    rm -f -- "${PID_FILE}.new"
}

command_start() {
    local pid deadline remaining

    require_binary
    [[ "$MIHOMO_START_TIMEOUT" =~ ^[1-9][0-9]*$ ]] ||
        die "MIHOMO_START_TIMEOUT must be a positive integer"
    [[ "$MIHOMO_STOP_TIMEOUT" =~ ^[1-9][0-9]*$ ]] ||
        die "MIHOMO_STOP_TIMEOUT must be a positive integer"
    if pid="$(read_pid 2>/dev/null)" && pid_is_mihomo "$pid"; then
        die "Mihomo is already running with pid $pid"
    fi
    rm -f -- "$PID_FILE"
    command_validate
    mkdir -p -- "$RUNTIME_DIR" "$(dirname -- "$MIHOMO_LOG")"

    nohup "$MIHOMO_BINARY" -d "$MIHOMO_CONFIG_DIR" -f "$MIHOMO_CONFIG" \
        >>"$MIHOMO_LOG" 2>&1 &
    pid=$!
    # Capture the numeric child PID now; function locals unwind before EXIT traps.
    trap "cleanup_start $pid" EXIT
    trap 'exit 130' INT
    trap 'exit 143' HUP TERM
    printf '%s\n' "$pid" >"${PID_FILE}.new"
    mv -f -- "${PID_FILE}.new" "$PID_FILE"

    deadline=$((SECONDS + MIHOMO_START_TIMEOUT))
    while (( SECONDS < deadline )); do
        if ! kill -0 "$pid" 2>/dev/null; then
            die "Mihomo exited during startup; inspect $MIHOMO_LOG"
        fi
        remaining=$((deadline - SECONDS))
        (( remaining > 0 )) || break
        (( remaining <= 3 )) || remaining=3
        # Allow the forked child to exec the binary before checking its identity.
        if pid_is_mihomo "$pid" && api_request "$remaining" >/dev/null 2>&1 && (( SECONDS <= deadline )); then
            trap - EXIT HUP INT TERM
            printf 'Mihomo started (pid %s).\n' "$pid"
            return
        fi
        sleep 0.1
    done
    die "Mihomo API did not become healthy within ${MIHOMO_START_TIMEOUT}s"
}

command_stop() {
    local pid elapsed

    [[ "$MIHOMO_STOP_TIMEOUT" =~ ^[1-9][0-9]*$ ]] ||
        die "MIHOMO_STOP_TIMEOUT must be a positive integer"
    if ! pid="$(read_pid 2>/dev/null)"; then
        printf '%s\n' "Mihomo is not running."
        return
    fi
    if ! pid_is_mihomo "$pid"; then
        rm -f -- "$PID_FILE"
        die "removed stale PID file; refusing to signal unrelated pid $pid"
    fi

    kill -TERM "$pid"
    for ((elapsed = 0; elapsed < MIHOMO_STOP_TIMEOUT; elapsed++)); do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm -f -- "$PID_FILE"
            printf '%s\n' "Mihomo stopped."
            return
        fi
        sleep 1
    done

    printf 'Mihomo did not stop after %ss; sending KILL.\n' "$MIHOMO_STOP_TIMEOUT" >&2
    kill -KILL "$pid"
    rm -f -- "$PID_FILE"
    printf '%s\n' "Mihomo stopped."
}

usage() {
    cat <<EOF
Usage: $0 {binary|validate|start|stop|running|health|version}

Environment:
  MIHOMO_BINARY        Binary path (default: $DEFAULT_BINARY)
  MIHOMO_CONFIG        Config file (default: ${PROJECT_DIR}/conf/config.yaml)
  MIHOMO_CONFIG_DIR    Mihomo data directory (default: ${PROJECT_DIR}/conf)
  MIHOMO_RUNTIME_DIR   Directory containing the managed PID file
  MIHOMO_LOG           Log file (default: ${PROJECT_DIR}/logs/mihomo.log)
  MIHOMO_API_URL       Controller URL (default: http://127.0.0.1:9090)
  MIHOMO_API_SECRET    Controller secret, if configured
  MIHOMO_START_TIMEOUT Startup health timeout in seconds
  MIHOMO_STOP_TIMEOUT  Graceful shutdown timeout in seconds
EOF
}

case "${1:-}" in
    binary) command_binary ;;
    validate) command_validate ;;
    start) command_start ;;
    stop) command_stop ;;
    health) command_health ;;
    running) pid="$(read_pid)" && pid_is_mihomo "$pid" ;;
    version) command_version ;;
    *) usage >&2; exit 2 ;;
esac
