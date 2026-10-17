#!/usr/bin/env python3
"""Build a thin Rust-only release archive; Python is required only by maintainers."""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parent


def sha256(body):
    return hashlib.sha256(body).hexdigest()


def package(binary, output, target):
    metadata = json.loads(subprocess.check_output([
        os.environ.get('CARGO', 'cargo'), 'metadata', '--locked', '--format-version', '1',
        '--filter-platform', target, '--manifest-path', str(ROOT / 'Cargo.toml')], text=True))
    root_package = next(p for p in metadata['packages'] if p['id'] == metadata['resolve']['root'])
    nodes = {node['id']: node for node in metadata['resolve']['nodes']}
    included = set()
    pending = [metadata['resolve']['root']]
    while pending:
        package_id = pending.pop()
        if package_id in included:
            continue
        included.add(package_id)
        pending.extend(dependency['pkg'] for dependency in nodes[package_id]['deps'])
    arch = target.split('-')[0]
    if arch not in ('x86_64', 'aarch64'):
        raise RuntimeError('Only Linux x86_64 and aarch64 releases are supported')
    files = {'clash': binary.read_bytes(), 'README.md': (ROOT / 'README.md').read_bytes(),
             'LICENSE': (ROOT.parent / 'LICENSE').read_bytes()}
    notices = ['Clash Linux Rust', '', 'Mihomo is a separately downloaded program, not bundled in this archive.',
               'Mihomo source: https://github.com/MetaCubeX/mihomo/tree/v1.19.29',
               'The following Rust dependency sources and license texts accompany this program.', '']
    for dependency in sorted(metadata['packages'], key=lambda p: (p['name'], p['version'])):
        if dependency['id'] == root_package['id'] or dependency['id'] not in included:
            continue
        base = Path(dependency['manifest_path']).parent
        candidates = {p for p in base.iterdir() if p.is_file() and p.name.upper().startswith(
            ('LICENSE', 'LICENCE', 'COPYING', 'NOTICE', 'COPYRIGHT', 'UNLICENSE'))}
        for directory in ('licenses', 'LICENSES'):
            if (base / directory).is_dir():
                candidates.update(p for p in (base / directory).rglob('*') if p.is_file())
        if dependency.get('license_file'):
            candidates.add(base / dependency['license_file'])
        if not candidates:
            raise RuntimeError('Missing original license text: ' + dependency['name'])
        name = dependency['name'] + '-' + dependency['version']
        notices.append(f"{name}: {dependency.get('license') or 'see license files'}")
        notices.append(dependency.get('repository') or f'https://crates.io/crates/{dependency["name"]}/{dependency["version"]}')
        for path in sorted(candidates):
            files['licenses/' + name + '/' + path.relative_to(base).as_posix()] = path.read_bytes()
    files['THIRD-PARTY-NOTICES.txt'] = ('\n'.join(notices) + '\n').encode()
    build = {'format': 1, 'implementation': 'rust', 'version': root_package['version'],
             'architecture': arch, 'target': target, 'data_format': 1,
             'files': {name: sha256(body) for name, body in sorted(files.items())}}
    files['BUILD.json'] = json.dumps(build, ensure_ascii=False, indent=2).encode()
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'clash-linux-rust-{target}.tar.gz'
    with archive.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as tar:
            for name, body in sorted(files.items()):
                entry = tarfile.TarInfo(name)
                entry.size = len(body)
                entry.mode = 0o755 if name == 'clash' else 0o644
                tar.addfile(entry, io.BytesIO(body))
    checksum = sha256(archive.read_bytes())
    archive.with_suffix(archive.suffix + '.sha256').write_text(checksum + '  ' + archive.name + '\n')
    print(f'{archive}: {archive.stat().st_size} bytes; binary {binary.stat().st_size} bytes')
    print('SHA-256: ' + checksum)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    parser.add_argument('--target', required=True)
    args = parser.parse_args()
    package(args.binary.resolve(), args.output.resolve(), args.target)
