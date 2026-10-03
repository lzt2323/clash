#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
readonly INSTALL_DIR="${MIHOMO_INSTALL_DIR:-${PROJECT_DIR}/bin}"
readonly INSTALL_PATH="${INSTALL_DIR}/mihomo"
TEMP_DIR=""

VERSION="${MIHOMO_VERSION:-v1.19.29}"
VERSION="v${VERSION#v}"
readonly CHECKSUM_FILE="${PROJECT_DIR}/checksums/mihomo-${VERSION}.sha256"

if [[ -n "${MIHOMO_CACHE_DIR:-}" ]]; then
    CACHE_DIR="$MIHOMO_CACHE_DIR"
elif [[ -n "${XDG_CACHE_HOME:-}" ]]; then
    CACHE_DIR="${XDG_CACHE_HOME}/clash-linux"
elif [[ -n "${HOME:-}" ]]; then
    CACHE_DIR="${HOME}/.cache/clash-linux"
else
    CACHE_DIR="${PROJECT_DIR}/temp/cache"
fi

CURL_TRANSPORT_ARGS=(
    --fail --silent --show-error --location
    --connect-timeout 15 --max-time 300
)
if [[ -n "${MIHOMO_DOWNLOAD_PROXY:-}" ]]; then
    CURL_TRANSPORT_ARGS+=(--proxy "$MIHOMO_DOWNLOAD_PROXY")
fi

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

cleanup() {
    if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
        rm -rf -- "$TEMP_DIR"
    fi
}

need_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

release_arch() {
    case "${MIHOMO_ARCH:-$(uname -m)}" in
        x86_64|amd64)
            printf '%s\n' "amd64"
            ;;
        aarch64|arm64)
            printf '%s\n' "arm64"
            ;;
        armv7l|armv7|arm32v7)
            printf '%s\n' "armv7"
            ;;
        *)
            die "unsupported Linux architecture: ${MIHOMO_ARCH:-$(uname -m)}"
            ;;
    esac
}

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$1" | awk '{print $1}'
    else
        die "sha256sum or shasum is required to verify the download"
    fi
}

official_digest() {
    local api_url="$1"
    local asset_name="$2"
    local release_json="$3"

    if ! curl "${CURL_TRANSPORT_ARGS[@]}" \
        --connect-timeout 10 --max-time 30 \
        -H "Accept: application/vnd.github+json" \
        -H "X-GitHub-Api-Version: 2022-11-28" \
        "$api_url" -o "$release_json"; then
        return 1
    fi

    awk -v asset_name="$asset_name" '
        index($0, "\"name\": \"" asset_name "\"") {
            found = 1
            next
        }
        found && match($0, /"digest": "sha256:[[:xdigit:]]+"/) {
                digest = substr($0, RSTART, RLENGTH)
                sub(/^"digest": "sha256:/, "", digest)
                sub(/"$/, "", digest)
                print tolower(digest)
                exit
        }
    ' "$release_json"
}

bundled_digest() {
    local asset_name=$1
    [[ -r "$CHECKSUM_FILE" ]] || return 1
    awk -v asset_name="$asset_name" '$2 == asset_name { print tolower($1); exit }' \
        "$CHECKSUM_FILE"
}

install_mihomo() {
    local arch asset_name base_url api_url download_url temp_dir archive candidate
    local expected_sha actual_sha cache_path source_kind

    need_command curl
    need_command cp
    need_command gzip
    need_command install
    [[ "$(uname -s)" == "Linux" ]] || die "Mihomo installer supports Linux only"

    arch="$(release_arch)"
    asset_name="mihomo-linux-${arch}-${VERSION}.gz"
    base_url="${MIHOMO_RELEASE_BASE_URL:-https://github.com/MetaCubeX/mihomo/releases/download}"
    api_url="${MIHOMO_RELEASE_API_URL:-https://api.github.com/repos/MetaCubeX/mihomo/releases/tags/${VERSION}}"
    download_url="${base_url%/}/${VERSION}/${asset_name}"

    if [[ "${1:-}" == "--print-url" ]]; then
        printf '%s\n' "$download_url"
        return
    fi
    [[ $# -eq 0 ]] || die "usage: $0 [--print-url]"

    temp_dir="$(mktemp -d "${TMPDIR:-/tmp}/mihomo-install.XXXXXXXX")"
    TEMP_DIR="$temp_dir"
    trap cleanup EXIT
    archive="${temp_dir}/${asset_name}"
    candidate="${temp_dir}/mihomo"
    cache_path="${CACHE_DIR}/${asset_name}"

    expected_sha="${MIHOMO_SHA256:-}"
    if [[ -z "$expected_sha" ]]; then
        expected_sha="$(bundled_digest "$asset_name" || true)"
    fi
    if [[ -z "$expected_sha" ]]; then
        expected_sha="$(official_digest "$api_url" "$asset_name" "${temp_dir}/release.json" || true)"
    fi
    expected_sha="${expected_sha#sha256:}"
    if [[ -n "$expected_sha" && ! "$expected_sha" =~ ^[[:xdigit:]]{64}$ ]]; then
        die "MIHOMO_SHA256 or published digest is not a valid SHA-256 value"
    fi

    source_kind=''
    if [[ -n "${MIHOMO_ARCHIVE:-}" ]]; then
        [[ -f "$MIHOMO_ARCHIVE" && -r "$MIHOMO_ARCHIVE" ]] ||
            die "MIHOMO_ARCHIVE is not a readable file: ${MIHOMO_ARCHIVE}"
        cp -- "$MIHOMO_ARCHIVE" "$archive"
        source_kind=local
        printf 'Using local Mihomo archive: %s\n' "$MIHOMO_ARCHIVE"
    elif [[ -f "$cache_path" && -r "$cache_path" && -n "$expected_sha" ]]; then
        actual_sha="$(sha256_file "$cache_path")"
        if [[ "${actual_sha,,}" == "${expected_sha,,}" ]]; then
            cp -- "$cache_path" "$archive"
            source_kind=cache
            printf 'Using verified Mihomo cache: %s\n' "$cache_path"
        else
            printf 'WARNING: ignoring corrupt Mihomo cache: %s\n' "$cache_path" >&2
        fi
    fi

    if [[ -z "$source_kind" ]]; then
        printf 'Downloading Mihomo %s for linux/%s...\n' "$VERSION" "$arch"
        if ! curl "${CURL_TRANSPORT_ARGS[@]}" \
            --retry 3 --retry-delay 2 \
            "$download_url" -o "$archive"; then
            die "download failed: ${download_url}
Set MIHOMO_DOWNLOAD_PROXY, provide MIHOMO_ARCHIVE, or copy a verified archive to ${cache_path}"
        fi
        source_kind=download
    fi

    if [[ -n "$expected_sha" ]]; then
        actual_sha="$(sha256_file "$archive")"
        [[ "${actual_sha,,}" == "${expected_sha,,}" ]] ||
            die "SHA-256 mismatch for ${asset_name}"
        printf 'Verified SHA-256: %s\n' "${actual_sha,,}"
    else
        printf '%s\n' \
            "WARNING: no trusted digest is available for ${asset_name}." \
            "Set MIHOMO_SHA256 when using a custom version or offline archive." >&2
    fi

    if [[ "$source_kind" != cache && -n "$expected_sha" ]]; then
        mkdir -p -- "$CACHE_DIR"
        install -m 0600 "$archive" "${cache_path}.new"
        mv -f -- "${cache_path}.new" "$cache_path"
        printf 'Cached verified archive: %s\n' "$cache_path"
    fi

    gzip -dc -- "$archive" >"$candidate"
    chmod 0755 "$candidate"
    "$candidate" -v >/dev/null

    mkdir -p -- "$INSTALL_DIR"
    install -m 0755 "$candidate" "${INSTALL_PATH}.new"
    mv -f -- "${INSTALL_PATH}.new" "$INSTALL_PATH"
    printf 'Installed %s\n' "$INSTALL_PATH"
}

install_mihomo "$@"
