#!/usr/bin/env bash

set -u

Server_Dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
api_url="${CLASH_API_URL:-${MIHOMO_API_URL:-http://127.0.0.1:9090}}"
config_file="${CLASH_CONFIG_PATH:-${Server_Dir}/conf/config.yaml}"

load_secret() {
    if [[ -n "${CLASH_SECRET:-}" ]]; then
        printf '%s' "$CLASH_SECRET"
        return
    fi

    if [[ -r "$config_file" ]]; then
        sed -n "s/^[[:space:]]*secret:[[:space:]]*['\"]\\{0,1\\}\\([^'\"]*\\)['\"]\\{0,1\\}[[:space:]]*$/\\1/p" "$config_file" | head -n 1
    fi
}

Secret=$(load_secret)

auth_args=()
if [[ -n "$Secret" ]]; then
    auth_args=(-H "Authorization: Bearer ${Secret}")
fi

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "缺少依赖命令：$1"
        exit 1
    fi
}

api_get() {
    curl --noproxy 127.0.0.1,localhost -fsS "${auth_args[@]}" "$api_url/$1"
}

api_patch() {
    curl --noproxy 127.0.0.1,localhost -fsS -XPATCH "${auth_args[@]}" -H "Content-Type: application/json" "$api_url/$1" -d "$2" >/dev/null
}

api_put() {
    curl --noproxy 127.0.0.1,localhost -fsS -XPUT "${auth_args[@]}" -H "Content-Type: application/json" "$api_url/$1" -d "$2" >/dev/null
}

urlencode() {
    jq -nr --arg v "$1" '$v|@uri'
}

read_choice() {
    local prompt=$1
    local max=$2
    local choice

    read -r -p "$prompt" choice
    if ! [[ "$choice" =~ ^[0-9]+$ ]] || (( choice < 1 || choice > max )); then
        echo "无效的选择！" >&2
        return 1
    fi

    printf '%s' "$choice"
}

get_proxies_json() {
    local response
    if ! response=$(api_get "proxies"); then
        echo "无法连接 Clash API：$api_url" >&2
        echo "请确认 Clash 已启动，external-controller 为 9090，并且 secret 正确。" >&2
        return 1
    fi
    printf '%s' "$response"
}

usage() {
    cat <<'EOF'
用法:
  clash_proxy-selector.sh                 打开交互菜单
  clash_proxy-selector.sh menu            打开交互菜单
  clash_proxy-selector.sh status          查看当前模式和策略组节点
  clash_proxy-selector.sh mode [模式]     查看或切换模式：Rule / Global / Direct
  clash_proxy-selector.sh groups          列出可切换策略组
  clash_proxy-selector.sh nodes [策略组]  列出策略组节点
  clash_proxy-selector.sh switch [关键词] [策略组]
                                      模糊搜索并直接选择节点
  clash_proxy-selector.sh delay [策略组]  测试策略组内节点延迟
  clash_proxy-selector.sh set <策略组> <节点名>
                                      切换指定策略组到指定节点

环境变量:
  CLASH_API_URL       Clash API 地址，默认 http://127.0.0.1:9090
  CLASH_SECRET        Clash API Secret，默认从 conf/config.yaml 读取
  CLASH_DELAY_URL     测速 URL，默认 https://cp.cloudflare.com/generate_204
  CLASH_DELAY_TIMEOUT 测速超时毫秒，默认 5000
  CLASH_DELAY_PARALLEL 测速并发数，默认 8
EOF
}

show_status() {
    local config_json proxies_json

    config_json=$(api_get "configs") || {
        echo "无法读取当前 Clash 配置。"
        return 1
    }
    proxies_json=$(get_proxies_json) || return 1

    echo "========== 当前状态 =========="
    echo "模式：$(jq -r '.mode // "unknown"' <<<"$config_json")"
    echo
    jq -r '
        .proxies
        | to_entries[]
        | select(.value.all and (.value.all | length > 0))
        | "\(.key): \(.value.now // "-")"
    ' <<<"$proxies_json"
    echo "=============================="
}

normalize_mode() {
    case "$1" in
        [Rr]ule) printf 'Rule' ;;
        [Gg]lobal) printf 'Global' ;;
        [Dd]irect) printf 'Direct' ;;
        *) return 1 ;;
    esac
}

set_mode() {
    local mode=$1

    if ! mode=$(normalize_mode "$mode"); then
        echo "无效模式：$1，可用模式：Rule / Global / Direct"
        return 1
    fi

    if api_patch "configs" "{\"mode\":\"${mode}\"}"; then
        echo "代理模式已更新为：$mode"
    else
        echo "代理模式更新失败。"
        return 1
    fi
}

select_mode() {
    local modes=("Rule" "Global" "Direct")
    local i mode_index mode

    echo "========== 代理模式 =========="
    for i in "${!modes[@]}"; do
        printf "%d. %s\n" "$((i + 1))" "${modes[$i]}"
    done
    echo "=============================="

    mode_index=$(read_choice "请选择代理模式（输入编号）：" "${#modes[@]}") || return
    mode="${modes[$((mode_index - 1))]}"

    set_mode "$mode"
}

list_groups() {
    local proxies_json i current

    proxies_json=$(get_proxies_json) || return
    mapfile -t groups < <(
        jq -r '
            .proxies
            | to_entries[]
            | select(.value.all and (.value.all | length > 0))
            | .key
        ' <<<"$proxies_json"
    )

    if (( ${#groups[@]} == 0 )); then
        echo "没有找到可选择的策略组。"
        return 1
    fi

    for i in "${!groups[@]}"; do
        current=$(jq -r --arg group "${groups[$i]}" '.proxies[$group].now // "-"' <<<"$proxies_json")
        printf "%d. %s 当前：%s\n" "$((i + 1))" "${groups[$i]}" "$current"
    done
}

list_nodes() {
    local group=${1:-}
    local proxies_json current i

    proxies_json=$(get_proxies_json) || return
    if [[ -z "$group" ]]; then
        selected_group=""
        select_group "$proxies_json" || return
        group=$selected_group
    fi

    if ! jq -e --arg group "$group" '.proxies[$group].all and (.proxies[$group].all | length > 0)' <<<"$proxies_json" >/dev/null; then
        echo "未找到策略组或该策略组不可切换：$group"
        return 1
    fi

    current=$(jq -r --arg group "$group" '.proxies[$group].now // "-"' <<<"$proxies_json")
    mapfile -t nodes < <(jq -r --arg group "$group" '.proxies[$group].all[]' <<<"$proxies_json")

    for i in "${!nodes[@]}"; do
        if [[ "${nodes[$i]}" == "$current" ]]; then
            printf "%d. %s *\n" "$((i + 1))" "${nodes[$i]}"
        else
            printf "%d. %s\n" "$((i + 1))" "${nodes[$i]}"
        fi
    done
}

set_proxy_by_name() {
    local group=$1
    local node=$2
    local proxies_json encoded_group payload

    proxies_json=$(get_proxies_json) || return
    if ! jq -e --arg group "$group" --arg node "$node" '.proxies[$group].all | index($node)' <<<"$proxies_json" >/dev/null; then
        echo "未找到策略组或节点：$group -> $node"
        return 1
    fi

    encoded_group=$(urlencode "$group")
    payload=$(jq -cn --arg name "$node" '{name:$name}')

    if api_put "proxies/${encoded_group}" "$payload"; then
        echo "策略组 ${group} 已切换为：${node}"
    else
        echo "节点切换失败。"
        return 1
    fi
}

write_delay_results() {
    local tmp_dir=$1
    local proxies_json=$2
    shift 2
    local -a requested_nodes=("$@")

    local delay_url=${CLASH_DELAY_URL:-https://cp.cloudflare.com/generate_204}
    local timeout=${CLASH_DELAY_TIMEOUT:-5000}
    local parallel=${CLASH_DELAY_PARALLEL:-8}
    local encoded_delay_url node encoded_node encoded_provider response delay i running
    local provider provider_json refreshed_provider_json
    local -a matched_providers
    local -A providers_to_check=()

    if ! [[ "$parallel" =~ ^[0-9]+$ ]] || (( parallel < 1 )); then
        parallel=8
    fi

    encoded_delay_url=$(urlencode "$delay_url")

    provider_json=$(api_get "providers/proxies" 2>/dev/null || printf '{"providers":{}}')
    for node in "${requested_nodes[@]}"; do
        if ! jq -e --arg node "$node" '.proxies | has($node)' \
            <<<"$proxies_json" >/dev/null; then
            mapfile -t matched_providers < <(
                jq -r --arg node "$node" '
                    .providers
                    | to_entries[]
                    | select(any(.value.proxies[]?; .name == $node))
                    | .key
                ' <<<"$provider_json"
            )
            for provider in "${matched_providers[@]}"; do
                providers_to_check["$provider"]=1
            done
        fi
    done

    for provider in "${!providers_to_check[@]}"; do
        encoded_provider=$(urlencode "$provider")
        api_get "providers/proxies/${encoded_provider}/healthcheck?timeout=${timeout}&url=${encoded_delay_url}" \
            >/dev/null 2>&1 || true
    done
    refreshed_provider_json=$(api_get "providers/proxies" 2>/dev/null || true)
    [[ -z "$refreshed_provider_json" ]] || provider_json=$refreshed_provider_json

    for i in "${!requested_nodes[@]}"; do
        node=${requested_nodes[$i]}
        if ! jq -e --arg node "$node" '.proxies | has($node)' \
            <<<"$proxies_json" >/dev/null; then
            delay=$(jq -r --arg node "$node" '
                [
                    .providers[].proxies[]?
                    | select(.name == $node)
                    | (.history // [])[]?
                    | select((.delay // 0) > 0)
                    | .delay
                ]
                | if length > 0 then "\(last)ms" else "失败" end
            ' <<<"$provider_json")
            printf '%s' "$delay" >"${tmp_dir}/${i}"
            continue
        fi
        (
            encoded_node=$(urlencode "$node")
            if response=$(api_get "proxies/${encoded_node}/delay?timeout=${timeout}&url=${encoded_delay_url}" 2>/dev/null); then
                delay=$(jq -r '.delay // empty' <<<"$response")
                if [[ -n "$delay" ]]; then
                    printf "%s" "${delay}ms"
                else
                    printf "%s" "失败"
                fi
            else
                printf "%s" "超时"
            fi > "${tmp_dir}/${i}"
        ) &

        while true; do
            running=$(jobs -pr | wc -l)
            (( running < parallel )) && break
            sleep 0.05
        done
    done

    wait
}

show_delay() {
    local group=${1:-}
    local delay_url=${2:-}
    local proxies_json current node delay
    local tmp_dir i

    proxies_json=$(get_proxies_json) || return
    if [[ -z "$group" ]]; then
        selected_group=""
        select_group "$proxies_json" || return
        group=$selected_group
    fi

    if [[ -n "$delay_url" ]]; then
        CLASH_DELAY_URL="$delay_url"
    fi

    if ! jq -e --arg group "$group" '.proxies[$group].all and (.proxies[$group].all | length > 0)' <<<"$proxies_json" >/dev/null; then
        echo "未找到策略组或该策略组不可测速：$group"
        return 1
    fi

    current=$(jq -r --arg group "$group" '.proxies[$group].now // "-"' <<<"$proxies_json")
    mapfile -t nodes < <(jq -r --arg group "$group" '.proxies[$group].all[]' <<<"$proxies_json")
    tmp_dir=$(mktemp -d)

    echo "========== ${group} 节点延迟 =========="
    write_delay_results "$tmp_dir" "$proxies_json" "${nodes[@]}"

    for i in "${!nodes[@]}"; do
        delay=$(cat "${tmp_dir}/${i}")
        if [[ "${nodes[$i]}" == "$current" ]]; then
            printf "%-8s %s *\n" "$delay" "${nodes[$i]}"
        else
            printf "%-8s %s\n" "$delay" "${nodes[$i]}"
        fi
    done

    rm -rf "$tmp_dir"
    echo "======================================"
}

select_group() {
    local proxies_json=$1
    local i current group_index

    mapfile -t groups < <(
        jq -r '
            .proxies
            | to_entries[]
            | select(.value.all and (.value.all | length > 0))
            | .key
        ' <<<"$proxies_json"
    )

    if (( ${#groups[@]} == 0 )); then
        echo "没有找到可选择的策略组。"
        return 1
    fi

    echo "========== 策略组 =========="
    for i in "${!groups[@]}"; do
        current=$(jq -r --arg group "${groups[$i]}" '.proxies[$group].now // "-"' <<<"$proxies_json")
        printf "%d. %s 当前：%s\n" "$((i + 1))" "${groups[$i]}" "$current"
    done
    echo "============================"

    group_index=$(read_choice "请选择策略组（输入编号）：" "${#groups[@]}") || return 1
    selected_group="${groups[$((group_index - 1))]}"
}

existing_delay() {
    local proxies_json=$1
    local provider_json=$2
    local node=$3

    jq -rs --arg node "$node" '
        .[0] as $proxies
        | .[1] as $providers
        | [
            ($proxies.proxies[$node].history // [])[]?,
            (
                $providers.providers[].proxies[]?
                | select(.name == $node)
                | (.history // [])[]?
            )
            | select((.delay // 0) > 0)
            | .delay
        ]
        | if length > 0 then "\(last)ms" else "未测速" end
    ' < <(printf '%s\n%s\n' "$proxies_json" "$provider_json")
}

switch_proxy() {
    local query=${1:-}
    local requested_group=${2:-}
    local proxies_json provider_json selected_group current node node_index selected_node i delay
    local lower_query
    local -a nodes matched_nodes

    proxies_json=$(get_proxies_json) || return
    provider_json=$(api_get "providers/proxies" 2>/dev/null || printf '{"providers":{}}')
    if [[ -n "$requested_group" ]]; then
        selected_group=$requested_group
    else
        mapfile -t groups < <(
            jq -r '
                .proxies
                | to_entries[]
                | select(.value.all and (.value.all | length > 0))
                | .key
            ' <<<"$proxies_json"
        )
        if (( ${#groups[@]} == 1 )); then
            selected_group=${groups[0]}
        else
            selected_group=""
            select_group "$proxies_json" || return
        fi
    fi

    if ! jq -e --arg group "$selected_group" \
        '.proxies[$group].all and (.proxies[$group].all | length > 0)' \
        <<<"$proxies_json" >/dev/null; then
        echo "未找到策略组或该策略组不可切换：$selected_group"
        return 1
    fi

    mapfile -t nodes < <(jq -r --arg group "$selected_group" '.proxies[$group].all[]' <<<"$proxies_json")
    current=$(jq -r --arg group "$selected_group" '.proxies[$group].now // "-"' <<<"$proxies_json")
    lower_query=${query,,}
    matched_nodes=()
    for node in "${nodes[@]}"; do
        if [[ -z "$query" || "${node,,}" == *"$lower_query"* ]]; then
            matched_nodes+=("$node")
        fi
    done

    if (( ${#matched_nodes[@]} == 0 )); then
        printf '没有找到匹配“%s”的节点（策略组：%s）。\n' "$query" "$selected_group" >&2
        return 1
    fi

    echo "========== ${selected_group} 节点 =========="
    if [[ -n "$query" ]]; then
        printf '过滤：%s\n' "$query"
    fi
    for i in "${!matched_nodes[@]}"; do
        delay=$(existing_delay "$proxies_json" "$provider_json" "${matched_nodes[$i]}")
        if [[ "${matched_nodes[$i]}" == "$current" ]]; then
            printf "%d. %-8s %s *\n" "$((i + 1))" "$delay" "${matched_nodes[$i]}"
        else
            printf "%d. %-8s %s\n" "$((i + 1))" "$delay" "${matched_nodes[$i]}"
        fi
    done
    echo "==========================================="
    echo "提示：这里只显示已有延迟；需要重新测速请运行 delay 命令。"

    node_index=$(read_choice "请选择代理节点（输入编号）：" "${#matched_nodes[@]}") || return
    selected_node="${matched_nodes[$((node_index - 1))]}"
    set_proxy_by_name "$selected_group" "$selected_node"
}

switch_command() {
    local first=${1:-}
    local second=${2:-}
    local proxies_json

    # Backward compatibility: older releases accepted `switch GROUP NODE`
    # as an undocumented alias of `set GROUP NODE`.
    if [[ -n "$first" && -n "$second" ]]; then
        proxies_json=$(get_proxies_json) || return
        if jq -e --arg group "$first" --arg node "$second" \
            '(.proxies[$group].all // []) | index($node)' \
            <<<"$proxies_json" >/dev/null; then
            set_proxy_by_name "$first" "$second"
            return
        fi
    fi

    switch_proxy "$first" "$second"
}

select_proxy() {
    switch_proxy
}

show_menu() {
    echo "========== Clash 代理配置 =========="
    echo "1. 查看当前状态"
    echo "2. 选择代理模式（Rule / Global / Direct）"
    echo "3. 选择策略组节点"
    echo "4. 测试节点延迟"
    echo "5. 退出"
    echo "===================================="
}

interactive_menu() {
    while true; do
        show_menu
        read -r -p "请选择操作（输入编号）：" choice
        case "$choice" in
            1) show_status ;;
            2) select_mode ;;
            3) select_proxy ;;
            4) show_delay ;;
            5) break ;;
            *) echo "无效的选择！" ;;
        esac
        echo
    done
}

main() {
    require_cmd curl
    require_cmd jq

    case "${1:-menu}" in
        menu) interactive_menu ;;
        status) show_status ;;
        mode)
            if [[ $# -eq 1 ]]; then
                api_get "configs" | jq -r '.mode // "unknown"'
            else
                set_mode "$2"
            fi
            ;;
        groups) list_groups ;;
        nodes) list_nodes "${2:-}" ;;
        delay) show_delay "${2:-}" "${3:-}" ;;
        set)
            if [[ $# -lt 3 ]]; then
                echo "用法：$0 set <策略组> <节点名>"
                return 1
            fi
            set_proxy_by_name "$2" "$3"
            ;;
        switch)
            if [[ $# -gt 3 ]]; then
                echo "用法：$0 switch [关键词] [策略组]"
                return 1
            fi
            switch_command "${2:-}" "${3:-}"
            ;;
        -h|--help|help) usage ;;
        *)
            echo "未知命令：$1"
            usage
            return 1
            ;;
    esac
}

main "$@"
