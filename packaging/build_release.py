#!/usr/bin/env python3
"""Build credential-free Linux bundles using pinned standalone Python and Mihomo."""
import argparse
import gzip
import hashlib
import io
import json
import os
import platform
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    sha = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(chunk)
    return sha.hexdigest()


def download(url, expected, cache):
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists() and digest(cache) == expected:
        return cache
    temporary = cache.with_name(cache.name + '.partial')
    subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error',
                    '--retry', '3', '--connect-timeout', '15', '--max-time', '300',
                    '--proto', '=https', '--proto-redir', '=https', url, '-o', str(temporary)], check=True)
    if digest(temporary) != expected:
        temporary.unlink(missing_ok=True)
        raise RuntimeError('Upstream SHA-256 mismatch: ' + cache.name)
    temporary.replace(cache)
    return cache


def extract_python(archive, target):
    target = Path(target).resolve()
    if os.path.lexists(target / 'python'):
        raise RuntimeError('Python extraction requires an empty runtime directory')
    with tarfile.open(archive) as tf:
        members = tf.getmembers()
        names, links = set(), set()
        for item in members:
            parts = PurePosixPath(item.name).parts
            if (not parts or parts[0] != 'python' or '..' in parts or item.name.startswith('/')
                    or '\\' in item.name or '\x00' in item.name):
                raise RuntimeError('Unexpected Python archive path')
            name = '/'.join(parts)
            if name in names:
                raise RuntimeError('Duplicate Python archive path')
            names.add(name)
            if not (item.isfile() or item.isdir() or item.issym() or item.islnk()):
                raise RuntimeError('Unsupported Python archive member')
            if item.issym() or item.islnk():
                links.add(name)
                destination = (target / item.name).parent / item.linkname if item.issym() else target / item.linkname
                destination.resolve().relative_to(target.resolve() / 'python')
        for item in members:
            if any(parent.as_posix() in links for parent in PurePosixPath(item.name).parents):
                raise RuntimeError('Archive member traverses a link')
        # Materialize ordinary files first. No archive link can redirect writes.
        for item in members:
            path = target / item.name
            if item.isdir():
                path.mkdir(parents=True, exist_ok=True)
            elif item.isfile():
                path.parent.mkdir(parents=True, exist_ok=True)
                with tf.extractfile(item) as source, path.open('xb') as destination:
                    shutil.copyfileobj(source, destination)
                path.chmod(item.mode & 0o777)
        for item in members:
            if item.islnk():
                path = target / item.name
                destination = target / item.linkname
                if not destination.is_file() or destination.is_symlink():
                    raise RuntimeError('Hardlink target must be an extracted ordinary file')
                path.parent.mkdir(parents=True, exist_ok=True)
                os.link(destination, path)
        for item in members:
            if item.issym():
                path = target / item.name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(item.linkname)
        for name in links:
            path = target / name
            path.resolve(strict=True).relative_to(target / 'python')
            if not path.is_file():
                raise RuntimeError('Python archive links must resolve to runtime files')


def extract_licenses(archive, target):
    """Install original dependency notices omitted by install_only archives."""
    directory = target / 'python/licenses'
    directory.mkdir(parents=True, exist_ok=True)
    copied = set()
    with tarfile.open(archive) as tf:
        for item in tf.getmembers():
            parts = PurePosixPath(item.name).parts
            if (len(parts) != 2 or not item.isfile()
                    or not (parts[1] == 'LICENSE' or parts[1] == 'python-licenses.rst'
                            or parts[1].startswith('LICENSE.') and parts[1].endswith('.txt'))):
                continue
            with tf.extractfile(item) as source, (directory / parts[1]).open('xb') as dest:
                shutil.copyfileobj(source, dest)
            copied.add(parts[1])
    required = {'LICENSE', 'LICENSE.cpython.txt', 'LICENSE.libffi.txt',
                'LICENSE.ncurses.txt', 'LICENSE.openssl-3.txt', 'LICENSE.zlib.txt',
                'LICENSE.sqlite.txt', 'LICENSE.bzip2.txt', 'LICENSE.liblzma.txt',
                'LICENSE.tcl.txt', 'python-licenses.rst'}
    if not required.issubset(copied):
        raise RuntimeError('Upstream Python dependency license set is incomplete')


def validate_runtime(stage, arch=None):
    python = stage / 'python/bin/python3'
    if not python.is_file() or not os.access(python, os.X_OK):
        raise RuntimeError('Private Python executable is missing')
    # PBS links many C extensions into the interpreter; _curses need not be a .so.
    if not list((stage / 'python/lib').glob('python*/curses/__init__.py')):
        raise RuntimeError('Private Python curses package is missing')
    if not list((stage / 'python/lib').glob('python*/LICENSE.txt')):
        raise RuntimeError('Private Python license is missing')
    native = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
    if sys.platform == 'linux' and arch == native:
        subprocess.run([str(python), '-E', '-s', '-B', '-c',
                        'import curses, _curses, fcntl, ssl, json; assert ssl.OPENSSL_VERSION'],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def build(arch, version, cache, output):
    if sys.platform != 'linux':
        raise RuntimeError('Release bundles must be built on Linux (case-sensitive filesystem required)')
    lock = json.loads((ROOT / 'packaging/runtime-lock.json').read_text())
    py = lock['python']['assets'][arch]
    py_url = 'https://github.com/astral-sh/python-build-standalone/releases/download/%s/%s' % (
        lock['python']['release'], urllib.parse.quote(py['name']))
    py_archive = download(py_url, py['sha256'], cache / py['name'])
    licenses = lock['python']['licenses']
    license_archive = download(licenses['url'], licenses['sha256'], cache / licenses['name'])
    core_version = lock['mihomo']['version']
    core_name = 'mihomo-linux-%s-%s.gz' % (arch, core_version)
    hashes = {line.split()[1]: line.split()[0] for line in
              (ROOT / ('checksums/mihomo-%s.sha256' % core_version)).read_text().splitlines() if line.strip()}
    core_url = 'https://github.com/MetaCubeX/mihomo/releases/download/%s/%s' % (core_version, core_name)
    core_archive = download(core_url, hashes[core_name], cache / core_name)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='clash-build-') as temp:
        stage = Path(temp) / 'clash-linux'
        stage.mkdir()
        extract_python(py_archive, stage)
        extract_licenses(license_archive, stage)
        validate_runtime(stage, arch)
        files = ['clash', 'start.sh', 'restart.sh', 'shutdown.sh', 'README.md', 'UPGRADE.md', 'LICENSE', 'install.sh']
        files += [str(p.relative_to(ROOT)) for p in sorted((ROOT / 'scripts').glob('*'))
                  if p.is_file() and p.suffix in ('.py', '.sh')]
        files += [str(p.relative_to(ROOT)) for p in sorted((ROOT / 'checksums').glob('*.sha256'))]
        files += ['temp/mihomo_config.template.yaml', 'temp/templete_config.yaml']
        notices = 'packaging/third-party/mihomo-%s-NOTICES.txt' % core_version
        notice_index = 'packaging/third-party/mihomo-%s-index.json' % core_version
        index = json.loads((ROOT / notice_index).read_text())
        if index['version'] != core_version or index['notice_sha256'] != digest(ROOT / notices):
            raise RuntimeError('Mihomo notice index does not match its version or text')
        if not any(binary['name'] == core_name and binary['sha256'] == hashes[core_name]
                   for binary in index.get('binaries', [])):
            raise RuntimeError('Mihomo notice index does not cover this pinned core asset')
        files += [notices, notice_index]
        for name in files:
            dest = stage / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, dest)
            if name in ('temp/templete_config.yaml', 'temp/mihomo_config.template.yaml'):
                dest.write_text(re.sub(r'(?m)^secret\s*:.*$', 'secret: ""', dest.read_text()))
        (stage / 'bin').mkdir()
        with gzip.open(core_archive, 'rb') as source, (stage / 'bin/mihomo').open('wb') as dest:
            shutil.copyfileobj(source, dest)
        (stage / 'bin/mihomo').chmod(0o755)
        (stage / 'licenses/mihomo').mkdir(parents=True)
        shutil.copy2(stage / 'LICENSE', stage / 'licenses/mihomo/LICENSE.txt')
        # Original runtime and dependency licenses accompany both architectures.
        (stage / 'THIRD-PARTY-NOTICES.txt').write_text(
            'Mihomo %s\nSource: https://github.com/MetaCubeX/mihomo/tree/%s\n'
            'Corresponding source archive: https://github.com/MetaCubeX/mihomo/archive/refs/tags/%s.tar.gz\n'
            'Python %s (python-build-standalone %s)\n'
            'Source/build instructions: https://github.com/astral-sh/python-build-standalone/tree/%s\n'
            'Python original license: python/lib/python*/LICENSE.txt\n'
            'Python build/dependency notices: python/licenses/ (includes optional upstream components).\n'
            'Python license source archive: %s\n'
            'Mihomo license: licenses/mihomo/LICENSE.txt (GNU GPL version 3).\n' %
            (core_version, core_version, core_version, lock['python']['version'],
             lock['python']['release'], lock['python']['release'], licenses['url']))
        with (stage / 'THIRD-PARTY-NOTICES.txt').open('a') as notice:
            notice.write('Mihomo dependency and copied-code original notices: %s\n'
                         'Versioned modules, hashes and source index: %s\n' % (notices, notice_index))
        members = sorted(p for p in stage.rglob('*') if p.is_file())
        for path in members:
            path.resolve().relative_to(stage.resolve())
        metadata = {'format': 1, 'version': version, 'architecture': arch,
                    'data_format': 1, 'min_upgrade_version': 'v0.1.0',
                    'python': lock['python']['version'], 'mihomo': core_version,
                    'files': [p.relative_to(stage).as_posix() for p in members],
                    'upstream': {'python_sha256': py['sha256'], 'mihomo_sha256': hashes[core_name],
                                 'python_licenses_sha256': licenses['sha256']}}
        (stage / 'BUILD.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        members.append(stage / 'BUILD.json')
        archive = output / ('clash-linux-%s.tar.gz' % arch)
        temporary_archive = archive.with_name(archive.name + '.partial')
        with temporary_archive.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as compressed, tarfile.open(fileobj=compressed, mode='w', dereference=True, format=tarfile.GNU_FORMAT) as tf:
            for path in sorted(members):
                name = 'clash-linux/' + path.relative_to(stage).as_posix()
                info = tf.gettarinfo(str(path), name)
                # Dereference even hardlinks, producing only ordinary file entries.
                info.type = tarfile.REGTYPE
                info.linkname = ''
                info.size = path.stat().st_size
                info.uid = info.gid = 0
                info.uname = info.gname = ''
                info.mtime = 0
                with path.open('rb') as stream:
                    tf.addfile(info, stream)
        temporary_archive.replace(archive)
        checksum = digest(archive)
        (output / (archive.name + '.sha256')).write_text(checksum + '  ' + archive.name + '\n')
        # Regenerate the manifest without mutating files from another version.
        lines = []
        for asset in sorted(output.glob('clash-linux-*.tar.gz')):
            lines.append(digest(asset) + '  ' + asset.name)
        (output / 'SHA256SUMS').write_text('\n'.join(lines) + '\n')
        print('%s  %s bytes  sha256:%s' % (archive, archive.stat().st_size, checksum), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--arch', choices=('amd64', 'arm64'), required=True)
    parser.add_argument('--version', default='v0.2.0')
    parser.add_argument('--cache', type=Path, default=ROOT / 'runtime/build-cache')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?', args.version):
        parser.error('invalid version')
    build(args.arch, args.version, args.cache.resolve(),
          (args.output or ROOT / 'runtime/releases' / args.version).resolve())


if __name__ == '__main__':
    main()
