#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
ENV_FILE=${DASHBOARD_ENV_FILE:-"$PROJECT_DIR/.env"}
remote_host='<server-host>'
remote_user=${USER:-'<user>'}
local_port=''

usage() {
	cat <<'EOF'
用法：dashboard.sh [--host HOST] [--user USER] [--local-port PORT]

打印 Dashboard 地址、Secret 和 SSH 端口转发命令。
脚本不会建立 SSH 连接；Secret 只会显示在当前终端。
EOF
}

die() {
	printf '错误：%s\n' "$*" >&2
	exit 2
}

env_value() {
	local key=$1 line value
	[[ -r "$ENV_FILE" ]] || return 1
	line=$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$ENV_FILE" |
		tail -n 1) || return 1
	value=${line#*=}
	value=${value#"${value%%[![:space:]]*}"}
	value=${value%"${value##*[![:space:]]}"}
	if [[ ${#value} -ge 2 ]]; then
		if [[ ${value:0:1} == "'" && ${value: -1} == "'" ]] ||
			[[ ${value:0:1} == '"' && ${value: -1} == '"' ]]; then
			value=${value:1:${#value}-2}
		fi
	fi
	printf '%s' "$value"
}

validate_port() {
	[[ "$1" =~ ^[0-9]+$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535))
}

while (($#)); do
	case "$1" in
		--host|--user|--local-port)
			(($# >= 2)) || die "$1 需要参数"
			case "$1" in
				--host) remote_host=$2 ;;
				--user) remote_user=$2 ;;
				--local-port) local_port=$2 ;;
			esac
			shift 2
			;;
		-h|--help) usage; exit 0 ;;
		*) die "未知参数：$1" ;;
	esac
done

[[ "$remote_host" != *[[:space:]]* ]] || die '--host 不能包含空白字符'
[[ "$remote_user" != *[[:space:]]* ]] || die '--user 不能包含空白字符'

api_url=$(env_value MIHOMO_API_URL 2>/dev/null || true)
controller=$(env_value MIHOMO_CONTROLLER 2>/dev/null || true)
remote_port=''
if [[ "$controller" =~ :([0-9]+)$ ]]; then
	remote_port=${BASH_REMATCH[1]}
elif [[ "$api_url" =~ ^https?://(\[[^]]+\]|[^/:]+):([0-9]+) ]]; then
	remote_port=${BASH_REMATCH[2]}
else
	remote_port=9090
fi
validate_port "$remote_port" || die "无效的 controller/API 端口：$remote_port"
local_port=${local_port:-$remote_port}
validate_port "$local_port" || die "无效的本地转发端口：$local_port"

secret_state='Secret：未检测到 .env；请先运行 clash init。'
if [[ -r "$ENV_FILE" ]]; then
	secret_value=$(env_value CLASH_SECRET 2>/dev/null || true)
	if [[ -n "$secret_value" ]]; then
		secret_state="Secret：${secret_value}"
	else
		secret_state='Secret：.env 中未设置 CLASH_SECRET；启动流程可能生成 Secret，请检查启动输出。'
	fi
fi

cat <<EOF
控制面板地址：
  http://127.0.0.1:${local_port}/ui

${secret_state}

如果这是远程服务器，请在你的电脑上执行 SSH 转发：
  ssh -N -L ${local_port}:127.0.0.1:${remote_port} ${remote_user}@${remote_host}

提示：Secret 相当于控制面板密码，只在当前终端显示。不要截图、不要发到聊天里、不要提交到 Git。
EOF
