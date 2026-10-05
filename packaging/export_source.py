#!/usr/bin/env python3
"""Export an allowlisted, history-free source tree and reproducible source tarball."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile
import tempfile
from urllib.parse import parse_qsl, urlsplit

ROOT_FILES = ('clash', 'install.sh', 'start.sh', 'restart.sh', 'shutdown.sh',
              'README.md', 'UPGRADE.md', 'LICENSE', '.gitignore', '.env.example',
              'packaging/third-party/mihomo-v1.19.29-NOTICES.txt',
              'packaging/third-party/mihomo-v1.19.29-index.json',
              'temp/mihomo_config.template.yaml', 'temp/templete_config.yaml')
TREE_SUFFIXES = {'scripts': {'.py', '.sh'}, 'tests': {'.py', '.sh'},
                 'packaging': {'.py', '.sh', '.json'},
                 '.github/workflows': {'.yml', '.yaml'}, 'checksums': {'.sha256'}}
FIXTURE_SUFFIXES = {'.yaml', '.yml', '.json', '.txt'}
CREDENTIAL_KEYS = {'token', 'access_token', 'auth', 'authorization', 'password',
                   'secret', 'key', 'apikey', 'api_key', 'uuid'}


class ExportError(RuntimeError):
    pass


def example_host(host):
    return (host in {'localhost', '127.0.0.1', '::1', 'example', 'example.com',
                     'example.org', 'example.net'} or
            any(host.endswith(suffix) for suffix in
                ('.invalid', '.test', '.example', '.example.com', '.example.org', '.example.net')))


def inspect_text(relative, data):
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError:
        raise ExportError('Non-text source refused: ' + relative) from None
    if '\x00' in text:
        raise ExportError('Binary source refused: ' + relative)
    # Error messages intentionally identify a file only, never the matched value.
    if re.search(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----', text):
        raise ExportError('Private key material refused: ' + relative)
    if re.search(r'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[A-Z0-9]{16})\b', text):
        raise ExportError('Credential-like material refused: ' + relative)
    for match in re.finditer(r'https?://[^\s\x00<>"\'`]+', text):
        url = match.group(0).rstrip('),.;]')
        # Source code contains shell/Python formatting expressions, not URLs.
        if any(marker in url for marker in ('${', '{', '%s', '%d', '%(')):
            continue
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or '').lower()
            sensitive = any(key.lower() in CREDENTIAL_KEYS and value
                            for key, value in parse_qsl(parsed.query))
        except ValueError:
            continue
        if not example_host(host) and (parsed.username or parsed.password or sensitive):
            raise ExportError('Credential-bearing URL refused: ' + relative)
    return data


def selected_files(root):
    selected = set()
    for name in ROOT_FILES:
        path = root / name
        if not path.is_file() or path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root):
            raise ExportError('Required regular source missing or linked: ' + name)
        selected.add(name)
    for tree, suffixes in TREE_SUFFIXES.items():
        base = root / tree
        if base.is_symlink():
            raise ExportError('Linked source directory refused: ' + tree)
        if not base.exists():
            continue
        for directory, names, files in os.walk(base, followlinks=False):
            for name in list(names):
                candidate = Path(directory) / name
                if candidate.is_symlink():
                    raise ExportError('Linked source directory refused: ' + candidate.relative_to(root).as_posix())
                if name.startswith('.') or name == '__pycache__':
                    names.remove(name)
            for name in files:
                candidate = Path(directory) / name
                relative = candidate.relative_to(root).as_posix()
                allowed = candidate.suffix in suffixes
                if relative.startswith('tests/fixtures/'):
                    allowed = allowed or candidate.suffix in FIXTURE_SUFFIXES
                if not allowed or name.startswith('.'):
                    continue
                if candidate.is_symlink() or not candidate.is_file():
                    raise ExportError('Linked or special source refused: ' + relative)
                selected.add(relative)
    return sorted(selected)


def source_bytes(root, relative):
    data = (root / relative).read_bytes()
    # Retain the legacy template for compatibility, but never redistribute its
    # historical hard-coded controller credential as a usable default.
    if relative == 'temp/templete_config.yaml':
        data = re.sub(rb'(?m)^secret:[^\r\n]*', b"secret: ''", data)
    return inspect_text(relative, data)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def export_source(root, output, archive=None):
    root, output = Path(root).resolve(), Path(output).absolute()
    if output.is_symlink():
        raise ExportError('Output must not be a symbolic link.')
    output = output.resolve()
    if output == root or output in root.parents:
        raise ExportError('Output must not replace the source tree or an ancestor.')
    if root in output.parents and output.relative_to(root).parts[0] != 'runtime':
        raise ExportError('In-tree exports must be under runtime/.')
    archive = Path(archive).absolute() if archive else output.with_name(output.name + '.tar.gz')
    if archive.is_symlink():
        raise ExportError('Archive must not be a symbolic link.')
    archive = archive.resolve()
    if archive == output or output in archive.parents:
        raise ExportError('Archive must be outside the exported tree.')
    if archive == root or archive in root.parents:
        raise ExportError('Invalid archive destination.')
    if root in archive.parents and archive.relative_to(root).parts[0] not in {'runtime', 'dist'}:
        raise ExportError('In-tree archives must be under runtime/ or dist/.')
    manifest = output.with_name(output.name + '.export.json')
    if archive == manifest or archive.is_dir():
        raise ExportError('Archive conflicts with the export manifest or a directory.')
    if manifest.is_symlink():
        raise ExportError('Export manifest must not be linked.')
    if output.exists():
        if not output.is_dir() or not manifest.is_file():
            raise ExportError('Refusing to replace an unowned output directory.')
        try:
            old = json.loads(manifest.read_text())
            if old['source'] != str(root) or old['output'] != str(output):
                raise ValueError()
            actual = {p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()}
            if any(p.is_symlink() for p in output.rglob('*')) or (output / '.git').exists():
                raise ValueError()
            if actual != set(old['files']):
                raise ValueError()
            if any(digest((output / name).read_bytes()) != value for name, value in old['files'].items()):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            raise ExportError('Output has local edits or is not an owned export; choose another directory.') from None
    names = selected_files(root)
    contents = {name: source_bytes(root, name) for name in names}
    modes = {name: 0o755 if (root / name).stat().st_mode & stat.S_IXUSR else 0o644 for name in names}
    output.parent.mkdir(parents=True, exist_ok=True)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.source-export-', dir=output.parent) as temporary:
        staging = Path(temporary) / 'tree'
        staging.mkdir()
        for name, data in contents.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(modes[name])
        staged_archive = Path(temporary) / 'source.tar.gz'
        with staged_archive.open('wb') as raw:
            with gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode='w') as bundle:
                    for name, data in contents.items():
                        info = tarfile.TarInfo('clash-linux-source/' + name)
                        info.size, info.mode, info.mtime = len(data), modes[name], 0
                        bundle.addfile(info, io.BytesIO(data))
        if output.exists():
            shutil.rmtree(output)
        staging.replace(output)
        # copy + replace also supports an archive destination on another volume.
        with tempfile.NamedTemporaryFile(dir=archive.parent, prefix='.source-archive-', delete=False) as handle:
            archive_temp = Path(handle.name)
        try:
            shutil.copyfile(staged_archive, archive_temp)
            archive_temp.chmod(0o644)
            archive_temp.replace(archive)
        finally:
            archive_temp.unlink(missing_ok=True)
    result = {'source': str(root), 'output': str(output), 'archive': str(archive),
              'files': {name: digest(data) for name, data in contents.items()}}
    manifest.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parents[1]
    parser.add_argument('--output', type=Path, default=root / 'runtime/source-export')
    parser.add_argument('--archive', type=Path)
    args = parser.parse_args()
    try:
        result = export_source(root, args.output, args.archive)
    except (ExportError, OSError) as exc:
        parser.exit(1, 'Source export failed: %s\n' % exc)
    print('Exported %d source files to %s\nArchive: %s' %
          (len(result['files']), result['output'], result['archive']))


if __name__ == '__main__':
    main()
