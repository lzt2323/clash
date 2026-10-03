#!/usr/bin/env bash
set -u

PROJECT_DIR=${DOCTOR_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
ENV_FILE=${DOCTOR_ENV_FILE:-"$PROJECT_DIR/.env"}
OS_RELEASE=${DOCTOR_OS_RELEASE:-/etc/os-release}
required_failures=0
warnings=0

ok() {
	printf '[ OK ] %s\n' "$1"
}

warn() {
	printf '[WARN] %s\n' "$1"
	warnings=$((warnings + 1))
}

bad() {
	printf '[FAIL] %s\n' "$1"
	required_failures=$((required_failures + 1))
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

forced_missing() {
	local name=$1 item
	for item in ${DOCTOR_FORCE_MISSING:-}; do
		[[ "$item" == "$name" ]] && return 0
	done
	return 1
}

has_command() {
	! forced_missing "$1" && command -v "$1" >/dev/null 2>&1
}

bundled_mihomo_binary() {
	case "${DOCTOR_UNAME_M:-$(uname -m 2>/dev/null || true)}" in
		x86_64|amd64) printf '%s\n' "$PROJECT_DIR/bin/clash-linux-amd64" ;;
		aarch64|arm64) printf '%s\n' "$PROJECT_DIR/bin/clash-linux-arm64" ;;
		armv7l|armv7|arm32v7) printf '%s\n' "$PROJECT_DIR/bin/clash-linux-armv7" ;;
	esac
}

dependency_hint() {
	local packages=$1 id='' like='' manager=''
	if [[ -r "$OS_RELEASE" ]]; then
		id=$(sed -n 's/^ID=//p' "$OS_RELEASE" | head -n 1 | tr -d '"')
		like=$(sed -n 's/^ID_LIKE=//p' "$OS_RELEASE" | head -n 1 | tr -d '"')
	fi
	case " $id $like " in
		*' debian '*|*' ubuntu '*) manager="sudo apt update && sudo apt install -y $packages" ;;
		*' fedora '*) manager="sudo dnf install -y $packages" ;;
		*' rhel '*|*' centos '*|*' rocky '*|*' almalinux '*)
			manager="sudo dnf install -y $packages  # 老系统可将 dnf 换成 yum"
			;;
		*' arch '*) manager="sudo pacman -S --needed $packages" ;;
		*' alpine '*) manager="sudo apk add $packages" ;;
		*' suse '*|*' opensuse '*) manager="sudo zypper install $packages" ;;
	esac
	if [[ -z "$manager" ]]; then
		if has_command apt-get; then
			manager="sudo apt update && sudo apt install -y $packages"
		elif has_command dnf; then
			manager="sudo dnf install -y $packages"
		elif has_command yum; then
			manager="sudo yum install -y $packages"
		elif has_command pacman; then
			manager="sudo pacman -S --needed $packages"
		elif has_command apk; then
			manager="sudo apk add $packages"
		elif has_command zypper; then
			manager="sudo zypper install $packages"
		else
			manager="请使用当前发行版的软件包管理器安装：$packages"
		fi
	fi
	printf '%s' "$manager"
}

check_platform() {
	local kernel arch
	kernel=${DOCTOR_UNAME_S:-$(uname -s 2>/dev/null || true)}
	arch=${DOCTOR_UNAME_M:-$(uname -m 2>/dev/null || true)}
	if [[ "$kernel" == Linux ]]; then
		ok "操作系统：Linux"
	else
		bad "仅支持 Linux，当前为 ${kernel:-unknown}"
	fi
	case "$arch" in
		x86_64|amd64) ok "CPU 架构：amd64 ($arch)" ;;
		aarch64|arm64) ok "CPU 架构：arm64 ($arch)" ;;
		armv7l|armv7) ok "CPU 架构：armv7 ($arch)" ;;
		*) bad "暂不支持 CPU 架构：${arch:-unknown}" ;;
	esac
}

check_dependencies() {
	local name package missing_required=() missing_required_packages=()
	local missing_optional=()
	for name in curl gzip install openssl; do
		if has_command "$name"; then
			ok "必需命令：$name"
		else
			bad "缺少必需命令：$name"
			missing_required+=("$name")
			case "$name" in
				install) package=coreutils ;;
				*) package=$name ;;
			esac
			missing_required_packages+=("$package")
		fi
	done
	if has_command jq; then
		ok '可选命令：jq（终端菜单 JSON 解析）'
	else
		warn '缺少可选命令 jq：内核运行不受影响，但终端菜单部分功能不可用'
		missing_optional+=(jq)
	fi
	if ((${#missing_required[@]})); then
		printf '       安装建议：%s\n' \
			"$(dependency_hint "${missing_required_packages[*]}")"
	fi
	if ((${#missing_optional[@]})); then
		printf '       可选安装：%s\n' "$(dependency_hint "${missing_optional[*]}")"
	fi
}

check_mihomo() {
	local configured candidate=''
	configured=$(env_value MIHOMO_BINARY 2>/dev/null || true)
	if [[ -n "$configured" ]]; then
		[[ "$configured" == /* ]] || configured="$PROJECT_DIR/$configured"
		candidate=$configured
	elif [[ -x "$PROJECT_DIR/bin/mihomo" ]]; then
		candidate="$PROJECT_DIR/bin/mihomo"
	elif [[ -x "$(bundled_mihomo_binary)" ]]; then
		candidate="$(bundled_mihomo_binary)"
	elif has_command mihomo; then
		candidate=$(command -v mihomo)
	fi
	if [[ -n "$candidate" && -x "$candidate" ]]; then
		ok "Mihomo：$candidate"
		"$candidate" -v 2>/dev/null | head -n 1 | sed 's/^/       /' || true
	elif [[ "${DOCTOR_ALLOW_MISSING_MIHOMO:-0}" == 1 ]]; then
		warn '尚未安装 Mihomo；init 将在下一步安装项目固定版本'
	else
		bad '未找到可执行的 Mihomo'
		printf '       安装方式：./clash install（或 bash scripts/install_mihomo.sh）\n'
	fi
}

check_env() {
	local mode mode_num url config
	if [[ ! -e "$ENV_FILE" ]]; then
		warn "尚未创建 .env：先运行 ./clash init URL 或 ./clash init --config FILE"
		return
	fi
	if [[ ! -f "$ENV_FILE" || ! -r "$ENV_FILE" ]]; then
		bad ".env 不是可读普通文件：$ENV_FILE"
		return
	fi
	if mode=$(stat -c '%a' "$ENV_FILE" 2>/dev/null); then
		mode_num=$((8#$mode))
		if ((mode_num & 077)); then
			bad ".env 权限过宽（$mode）；其中可能含订阅 token/Secret，请执行 chmod 600 .env"
		else
			ok ".env 权限：$mode"
		fi
	else
		warn '无法读取 .env 权限；请确认仅当前用户可读写'
	fi

	url=$(env_value CLASH_URL 2>/dev/null || true)
	config=$(env_value CLASH_CONFIG_FILE 2>/dev/null || true)
	if [[ -n "$url" && -n "$config" ]]; then
		bad '.env 来源冲突：CLASH_URL 与 CLASH_CONFIG_FILE 只能设置一个'
	elif [[ -n "$url" ]]; then
		ok '配置来源：URL provider'
	elif [[ -n "$config" ]]; then
		if [[ "$config" != /* ]]; then
			config="$PROJECT_DIR/$config"
		fi
		if [[ -r "$config" && -s "$config" ]]; then
			ok "配置来源：本地 YAML ($config)"
		else
			bad "本地配置不存在、不可读或为空：$config"
		fi
	else
		warn '.env 尚未设置 CLASH_URL 或 CLASH_CONFIG_FILE'
	fi
}

add_port() {
	local value=$1 existing
	[[ "$value" =~ ^[0-9]+$ ]] || return
	((value >= 1 && value <= 65535)) || return
	for existing in "${ports[@]-}"; do
		[[ "$existing" == "$value" ]] && return
	done
	ports+=("$value")
}

check_ports() {
	local controller api_url port key listening='' own_pid=''
	local ports=()
	for port in 7890 7891 7892; do add_port "$port"; done
	controller=$(env_value MIHOMO_CONTROLLER 2>/dev/null || true)
	api_url=$(env_value MIHOMO_API_URL 2>/dev/null || true)
	[[ "$controller" =~ :([0-9]+)$ ]] && add_port "${BASH_REMATCH[1]}"
	if [[ "$api_url" =~ ^https?://(\[[^]]+\]|[^/:]+):([0-9]+) ]]; then
		add_port "${BASH_REMATCH[2]}"
	fi
	for key in MIXED_PORT HTTP_PORT SOCKS_PORT REDIR_PORT; do
		port=$(env_value "$key" 2>/dev/null || true)
		add_port "$port"
	done

	if [[ -n ${DOCTOR_PORTS_OUTPUT+x} ]]; then
		listening=$DOCTOR_PORTS_OUTPUT
	elif has_command ss; then
		listening=$(ss -ltn 2>/dev/null || true)
	elif has_command netstat; then
		listening=$(netstat -ltn 2>/dev/null || true)
	else
		warn '缺少 ss/netstat，跳过端口占用检查'
		return
	fi
	if [[ -r "$PROJECT_DIR/runtime/mihomo.pid" ]]; then
		read -r own_pid <"$PROJECT_DIR/runtime/mihomo.pid" || own_pid=''
		[[ "$own_pid" =~ ^[0-9]+$ ]] && kill -0 "$own_pid" 2>/dev/null ||
			own_pid=''
	fi
	for port in "${ports[@]}"; do
		if awk -v port="$port" '
			$0 ~ ("(^|[^0-9])" port "([^0-9]|$)") { found=1 }
			END { exit !found }
		' <<<"$listening"; then
			if [[ -n "$own_pid" ]]; then
				ok "端口 $port 已监听（当前 Mihomo PID $own_pid 正在运行）"
			else
				bad "端口 $port 已被监听；启动前请停止冲突服务或修改配置"
			fi
		else
			ok "端口 $port 可用"
		fi
	done
}

usage() {
	cat <<'EOF'
Usage: doctor.sh

离线检查系统、依赖、Mihomo、.env 配置与默认监听端口。
退出码：0=必需项就绪（可含可选警告），1=存在必需项失败，2=参数错误。
EOF
}

if (($#)); then
	case "$1" in
		-h|--help) usage; exit 0 ;;
		*) usage >&2; exit 2 ;;
	esac
fi

printf 'clash_linux 环境诊断（不会修改系统或访问网络）\n\n'
check_platform
check_dependencies
check_mihomo
check_env
check_ports
printf '\n结果：%d 个必需问题，%d 个可选警告。\n' "$required_failures" "$warnings"
((required_failures == 0))
