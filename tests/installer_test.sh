#!/usr/bin/env bash
# Every installer subprocess gets a disposable HOME; no real shell files are touched.
set -euo pipefail
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
TASK_TEST_DIR=$(mktemp -d)
TASK_TEST_DIR=$(cd -- "$TASK_TEST_DIR" && pwd -P)
trap 'rm -rf -- "$TASK_TEST_DIR"' EXIT
REAL_PYTHON=$(command -v python3)
REAL_MV=$(command -v mv)
mkdir -p "$TASK_TEST_DIR/tools" "$TASK_TEST_DIR/package/clash-linux/scripts" \
    "$TASK_TEST_DIR/package/clash-linux/python/bin" "$TASK_TEST_DIR/package/clash-linux/bin"

cat >"$TASK_TEST_DIR/tools/uname" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in -s) printf '%s\n' Linux ;; -m) printf '%s\n' "${FAKE_ARCH:-x86_64}" ;; *) exit 1 ;; esac
EOF
cat >"$TASK_TEST_DIR/tools/getconf" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "${FAKE_LIBC:-glibc 2.35}"
EOF
cat >"$TASK_TEST_DIR/tools/ldd" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "${FAKE_LIBC:-glibc 2.35}"
EOF
cat >"$TASK_TEST_DIR/tools/python3" <<'EOF'
#!/usr/bin/env bash
printf 'system python used\n' >>"$SYSTEM_PYTHON_POISON"
exit 97
EOF
printf '#!/usr/bin/env bash\nif [[ "${TEST_FAIL_RESTORE:-0}" == 1 && "${2:-}" == */old ]]; then exit 1; fi\nexec %q "$@"\n' \
    "$REAL_MV" >"$TASK_TEST_DIR/tools/mv"
cp "$TASK_TEST_DIR/tools/python3" "$TASK_TEST_DIR/tools/python"
cat >"$TASK_TEST_DIR/tools/curl" <<'EOF'
#!/usr/bin/env bash
out='' url=''
while (($#)); do
    case "$1" in --output|-o) out=$2; shift 2 ;; *) url=$1; shift ;; esac
done
printf '%s\n' "$url" >>"$DOWNLOAD_LOG"
case "$url" in
    https://primary.invalid/*)
        [[ "${SOURCE_CASE:-404}" != 404 ]] || exit 22
        case "$url" in
            */SHA256SUMS) cp "$PACKAGE_SUMS" "$out" ;;
            */clash-linux-amd64.tar.gz) printf 'broken primary\n' >"$out" ;;
            *) exit 22 ;;
        esac ;;
    https://fallback.invalid/v0.1.0/SHA256SUMS) cp "$PACKAGE_SUMS" "$out" ;;
    https://fallback.invalid/v0.1.0/clash-linux-amd64.tar.gz) cp "$PACKAGE_ARCHIVE" "$out" ;;
    https://fallback.invalid/v0.1.0/install.sh) cp "$INSTALLER_SOURCE" "$out" ;;
    *) exit 22 ;;
esac
EOF
chmod 0755 "$TASK_TEST_DIR/tools/"*
cat >"$TASK_TEST_DIR/package/clash-linux/clash" <<'EOF'
#!/usr/bin/env bash
set -eu
directory=$(cd -- "$(dirname -- "$0")" && pwd)
if [[ "${1:-}" == shell-init ]]; then
    shift
    if [[ "${TEST_FAIL_SHELL:-0}" == 1 ]]; then
        printf 'changed rc\n' >"$HOME/.bashrc"
        mkdir -p "$HOME/.local/bin"
        printf 'changed shim\n' >"$HOME/.local/bin/clash"
        exit 1
    fi
    exec "$directory/scripts/shell_integration.sh" install "$@"
fi
printf 'fake clash\n'
EOF
cp "$ROOT/scripts/shell_integration.sh" "$TASK_TEST_DIR/package/clash-linux/scripts/"
cat >"$TASK_TEST_DIR/package/clash-linux/bin/mihomo" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
printf '#!/usr/bin/env bash\nprintf "%%s\\n" "$*" >>"$PRIVATE_PYTHON_LOG"\nexec %q "$@"\n' \
    "$REAL_PYTHON" >"$TASK_TEST_DIR/package/clash-linux/python/bin/python3"
printf 'Fixture license\n' >"$TASK_TEST_DIR/package/clash-linux/LICENSE"
chmod 0755 "$TASK_TEST_DIR/package/clash-linux/clash" "$TASK_TEST_DIR/package/clash-linux/bin/mihomo" \
    "$TASK_TEST_DIR/package/clash-linux/python/bin/python3" "$TASK_TEST_DIR/package/clash-linux/scripts/shell_integration.sh"
"$REAL_PYTHON" -E -s -B - "$TASK_TEST_DIR/package/clash-linux" <<'PY'
import json, pathlib, sys
root = pathlib.Path(sys.argv[1])
files = sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.is_file())
(root / 'BUILD.json').write_text(json.dumps({'version': 'v0.1.0', 'architecture': 'amd64', 'python': 'fixture', 'files': files}))
PY
COPYFILE_DISABLE=1 tar -czf "$TASK_TEST_DIR/package.tar.gz" -C "$TASK_TEST_DIR/package" clash-linux
hash=$(sha256sum "$TASK_TEST_DIR/package.tar.gz"); hash=${hash%% *}
printf '%s  clash-linux-amd64.tar.gz\n' "$hash" >"$TASK_TEST_DIR/SHA256SUMS"
passed=0
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
pass() { passed=$((passed + 1)); }
case_home() { mkdir -p "$TASK_TEST_DIR/homes/$1"; printf '%s' "$TASK_TEST_DIR/homes/$1"; }
invoke() {
    local test_home=$1; shift
    env HOME="$test_home" SHELL=/bin/bash PATH="$TASK_TEST_DIR/tools:$PATH" \
        SYSTEM_PYTHON_POISON="$TASK_TEST_DIR/poison" PRIVATE_PYTHON_LOG="$TASK_TEST_DIR/python.log" \
        DOWNLOAD_LOG="$TASK_TEST_DIR/download.log" PACKAGE_SUMS="$TASK_TEST_DIR/SHA256SUMS" \
        PACKAGE_ARCHIVE="$TASK_TEST_DIR/package.tar.gz" INSTALLER_SOURCE="$ROOT/install.sh" \
        PYTHONHOME=/not-a-python-home PYTHONPATH=/not-a-python-path \
        bash "$ROOT/install.sh" --version v0.1.0 "$@"
}
offline() { local test_home=$1; shift; invoke "$test_home" --archive "$TASK_TEST_DIR/package.tar.gz" --sha256 "$hash" "$@"; }
reject() { if "$@" >"$TASK_TEST_DIR/rejected.out" 2>&1; then fail 'operation unexpectedly succeeded'; fi; }
contains() { [[ "$(cat -- "$1")" == *"$2"* ]] || fail "missing expected text: $2"; }
marker() {
    "$REAL_PYTHON" -E -s -B - "$1" "$2" "$3" <<'PY'
import json, pathlib, sys
prefix, filename, version = sys.argv[1:]
(pathlib.Path(prefix) / filename).write_text(json.dumps({'format': 1, 'version': version, 'prefix': prefix}))
PY
}

test_home=$(case_home offline)
prefix="$test_home/.local/share/clash-linux"
offline "$test_home" --no-shell >"$TASK_TEST_DIR/output"
[[ -x "$prefix/python/bin/python3" && -f "$prefix/.clash-install.json" ]] || fail 'offline install incomplete'
[[ ! -e "$test_home/.bashrc" && ! -e "$test_home/.local/bin/clash" ]] || fail '--no-shell touched shell files'
[[ ! -e "$TASK_TEST_DIR/poison" && ! -e "$TASK_TEST_DIR/download.log" ]] || fail 'offline used system Python or curl'
contains "$TASK_TEST_DIR/python.log" '-E -s -B -c import ssl, curses, fcntl'
contains "$prefix/env.sh" "$prefix/scripts/shell_integration.sh"
contains "$TASK_TEST_DIR/output" 'source '
pass

mkdir -p "$prefix/conf" "$prefix/runtime/mvp"
printf 'local code comment\n' >>"$prefix/clash"
printf 'user config\n' >"$prefix/conf/config.yaml"
printf 'user state\n' >"$prefix/runtime/mvp/state.json"
cp "$prefix/clash" "$TASK_TEST_DIR/original-cli"
offline "$test_home" --no-shell >/dev/null
cmp "$prefix/clash" "$TASK_TEST_DIR/original-cli" || fail 'same version overwrote executable'
contains "$prefix/conf/config.yaml" 'user config'
contains "$prefix/runtime/mvp/state.json" 'user state'
pass

preserved_home=$(case_home preserved)
preserved="$preserved_home/app"
mkdir -p "$preserved/conf/mvp-providers" "$preserved/runtime/mvp"
printf 'kept config\n' >"$preserved/conf/config.yaml"
printf 'kept provider\n' >"$preserved/conf/mvp-providers/provider.yaml"
printf 'kept state\n' >"$preserved/runtime/mvp/state.json"
printf 'keep unrelated note\n' >"$preserved/note.txt"
marker "$preserved" .clash-data.json v0.1.0
offline "$preserved_home" --prefix "$preserved" --no-shell >/dev/null
contains "$preserved/conf/config.yaml" 'kept config'
contains "$preserved/conf/mvp-providers/provider.yaml" 'kept provider'
contains "$preserved/runtime/mvp/state.json" 'kept state'
contains "$preserved/note.txt" 'keep unrelated note'
[[ ! -e "$preserved/.clash-data.json" && -f "$preserved/.clash-install.json" ]] || fail 'preservation marker not replaced'
pass

marker "$prefix" .clash-install.json v9.0.0
reject offline "$test_home" --no-shell
contains "$TASK_TEST_DIR/rejected.out" '请使用 --upgrade'
contains "$prefix/conf/config.yaml" 'user config'
marker "$prefix" .clash-install.json v0.1.0
pass

foreign_home=$(case_home foreign)
mkdir -p "$foreign_home/app"
printf 'not ours\n' >"$foreign_home/app/file"
reject offline "$foreign_home" --prefix "$foreign_home/app" --no-shell
contains "$foreign_home/app/file" 'not ours'
pass

ln -s "$foreign_home/app" "$foreign_home/link"
reject offline "$foreign_home" --prefix "$foreign_home/link" --no-shell
[[ -L "$foreign_home/link" ]] || fail 'prefix link modified'
pass

bad_home=$(case_home bad)
reject invoke "$bad_home" --archive "$TASK_TEST_DIR/package.tar.gz" --sha256 "$(printf '%064d' 0)" --no-shell
[[ ! -e "$bad_home/.local/share/clash-linux" ]] || fail 'bad digest installed target'
pass

for kind in symlink hardlink traversal; do
    "$REAL_PYTHON" -E -s -B - "$TASK_TEST_DIR/$kind.tar.gz" "$kind" <<'PY'
import io, tarfile, sys
destination, kind = sys.argv[1:]
with tarfile.open(destination, 'w:gz') as archive:
    item = tarfile.TarInfo('clash-linux/bad' if kind != 'traversal' else 'clash-linux/../../outside')
    if kind in ('symlink', 'hardlink'):
        item.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
        item.linkname = '/tmp/outside'
        archive.addfile(item)
    else:
        item.size = 3
        archive.addfile(item, io.BytesIO(b'bad'))
PY
    bad_hash=$(sha256sum "$TASK_TEST_DIR/$kind.tar.gz"); bad_hash=${bad_hash%% *}
    reject invoke "$bad_home" --archive "$TASK_TEST_DIR/$kind.tar.gz" --sha256 "$bad_hash" --no-shell
    [[ ! -e "$bad_home/.local/share/clash-linux" ]] || fail 'dangerous archive created target'
    pass
done

network_home=$(case_home fallback)
invoke "$network_home" --server https://primary.invalid --github https://fallback.invalid --no-shell >/dev/null
contains "$TASK_TEST_DIR/download.log" 'https://primary.invalid/releases/v0.1.0/SHA256SUMS'
contains "$TASK_TEST_DIR/download.log" 'https://fallback.invalid/v0.1.0/SHA256SUMS'
contains "$TASK_TEST_DIR/download.log" 'https://fallback.invalid/v0.1.0/clash-linux-amd64.tar.gz'
pass

corrupt_home=$(case_home corruptfallback)
SOURCE_CASE=bad invoke "$corrupt_home" --server https://primary.invalid --github https://fallback.invalid --no-shell >/dev/null
[[ -f "$corrupt_home/.local/share/clash-linux/.clash-install.json" ]] || fail 'corrupt primary did not fall back'
pass

conflict_home=$(case_home conflict)
mkdir -p "$conflict_home/.local/bin"
printf '# existing unrelated rc\n' >"$conflict_home/.bashrc"
printf '# other clash installation\n' >"$conflict_home/.local/bin/clash"
reject offline "$conflict_home"
contains "$conflict_home/.bashrc" 'existing unrelated rc'
contains "$conflict_home/.local/bin/clash" 'other clash installation'
[[ ! -e "$conflict_home/.local/share/clash-linux" ]] || fail 'shell conflict left installation'
pass

rollback_home=$(case_home rollback)
mkdir -p "$rollback_home/.local/bin"
printf '# original rc\n' >"$rollback_home/.bashrc"
rollback_prefix="$rollback_home/app"
printf '#!/usr/bin/env sh\nexec '\''%s'\'' "$@"\n' "$rollback_prefix/clash" >"$rollback_home/.local/bin/clash"
cp "$rollback_home/.bashrc" "$TASK_TEST_DIR/rc.before"
cp "$rollback_home/.local/bin/clash" "$TASK_TEST_DIR/shim.before"
TEST_FAIL_SHELL=1 reject offline "$rollback_home" --prefix "$rollback_prefix"
cmp "$rollback_home/.bashrc" "$TASK_TEST_DIR/rc.before" || fail 'shell failure changed existing rc'
cmp "$rollback_home/.local/bin/clash" "$TASK_TEST_DIR/shim.before" || fail 'shell failure changed existing shim'
[[ ! -e "$rollback_prefix" ]] || fail 'shell failure left new package'
pass

rollback_data_home=$(case_home rollbackdata)
rollback_data="$rollback_data_home/app"
mkdir -p "$rollback_data/conf" "$rollback_data/runtime/mvp"
printf 'keep on failure\n' >"$rollback_data/conf/config.yaml"
printf 'keep runtime\n' >"$rollback_data/runtime/mvp/state.json"
marker "$rollback_data" .clash-data.json v0.1.0
TEST_FAIL_SHELL=1 reject offline "$rollback_data_home" --prefix "$rollback_data"
contains "$rollback_data/conf/config.yaml" 'keep on failure'
contains "$rollback_data/runtime/mvp/state.json" 'keep runtime'
[[ -f "$rollback_data/.clash-data.json" && ! -f "$rollback_data/.clash-install.json" ]] || fail 'rollback lost data marker'
pass

failed_restore_home=$(case_home failedrestore)
failed_restore="$failed_restore_home/app"
mkdir -p "$failed_restore/conf"
printf 'last copy of user data\n' >"$failed_restore/conf/config.yaml"
marker "$failed_restore" .clash-data.json v0.1.0
TEST_FAIL_SHELL=1 TEST_FAIL_RESTORE=1 reject offline "$failed_restore_home" --prefix "$failed_restore"
contains "$TASK_TEST_DIR/rejected.out" '自动回滚未能完成'
backup_candidates=("$failed_restore_home"/.clash-install.*/old/conf/config.yaml)
[[ ${#backup_candidates[@]} == 1 && -f "${backup_candidates[0]}" ]] || fail 'failed rollback discarded its data backup'
contains "${backup_candidates[0]}" 'last copy of user data'
pass

shell_home=$(case_home shell)
offline "$shell_home" --shell bash >"$TASK_TEST_DIR/shell.out"
contains "$shell_home/.bashrc" '# >>> clash_linux shell integration >>>'
[[ -x "$shell_home/.local/bin/clash" ]] || fail 'shell shim missing'
shell_prefix="$shell_home/.local/share/clash-linux"
env HOME="$shell_home" bash -c 'source "$1/env.sh"; declare -F clash >/dev/null; clash help' _ "$shell_prefix" >/dev/null
offline "$shell_home" --shell bash >/dev/null
[[ ! -e "$TASK_TEST_DIR/poison" ]] || fail 'system Python was used'
pass

bootstrap_home=$(case_home bootstrap)
env HOME="$bootstrap_home" SHELL=/bin/bash PATH="$TASK_TEST_DIR/tools:$PATH" \
    SYSTEM_PYTHON_POISON="$TASK_TEST_DIR/poison" PRIVATE_PYTHON_LOG="$TASK_TEST_DIR/python.log" \
    DOWNLOAD_LOG="$TASK_TEST_DIR/download.log" PACKAGE_SUMS="$TASK_TEST_DIR/SHA256SUMS" \
    PACKAGE_ARCHIVE="$TASK_TEST_DIR/package.tar.gz" INSTALLER_SOURCE="$ROOT/install.sh" \
    bash "$ROOT/packaging/get.sh" --version v0.1.0 --server https://primary.invalid --github https://fallback.invalid \
    --archive "$TASK_TEST_DIR/package.tar.gz" --sha256 "$hash" --no-shell >/dev/null 2>&1
[[ -f "$bootstrap_home/.local/share/clash-linux/.clash-install.json" ]] || fail 'bootstrap fallback failed'
contains "$TASK_TEST_DIR/download.log" 'https://fallback.invalid/v0.1.0/install.sh'
pass

musl_home=$(case_home musl)
FAKE_LIBC=musl reject offline "$musl_home" --no-shell
contains "$TASK_TEST_DIR/rejected.out" 'Alpine / musl'
[[ ! -e "$musl_home/.local/share/clash-linux" ]] || fail 'musl was accepted'
pass

printf 'not a gzip archive\n' >"$TASK_TEST_DIR/broken.tar.gz"
broken_hash=$(sha256sum "$TASK_TEST_DIR/broken.tar.gz"); broken_hash=${broken_hash%% *}
reject invoke "$bad_home" --archive "$TASK_TEST_DIR/broken.tar.gz" --sha256 "$broken_hash" --no-shell
contains "$TASK_TEST_DIR/rejected.out" '压缩包损坏'
pass

cp -a "$TASK_TEST_DIR/package" "$TASK_TEST_DIR/wrong-build"
"$REAL_PYTHON" -E -s -B - "$TASK_TEST_DIR/wrong-build/clash-linux/BUILD.json" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
build = json.loads(path.read_text())
build['architecture'] = 'arm64'
path.write_text(json.dumps(build))
PY
COPYFILE_DISABLE=1 tar -czf "$TASK_TEST_DIR/wrong-build.tar.gz" -C "$TASK_TEST_DIR/wrong-build" clash-linux
wrong_hash=$(sha256sum "$TASK_TEST_DIR/wrong-build.tar.gz"); wrong_hash=${wrong_hash%% *}
reject invoke "$bad_home" --archive "$TASK_TEST_DIR/wrong-build.tar.gz" --sha256 "$wrong_hash" --no-shell
contains "$TASK_TEST_DIR/rejected.out" '清单'
pass

[[ ! -e "$TASK_TEST_DIR/poison" ]] || fail 'installer invoked a system Python'
printf 'Installer: %s isolated checks passed.\n' "$passed"
