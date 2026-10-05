#!/usr/bin/env python3
"""Generate latest.json from the exact verified bundles being published."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile


def metadata(directory, version):
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('latest metadata requires a stable release version')
    directory = Path(directory)
    checks = {}
    for line in (directory / 'SHA256SUMS').read_text().splitlines():
        checksum, name = line.split()
        if name in checks or not re.fullmatch(r'[0-9a-f]{64}', checksum):
            raise ValueError('Invalid or duplicate checksum')
        checks[name] = checksum
    assets = {}
    compatibility = None
    for arch in ('amd64', 'arm64'):
        name = 'clash-linux-' + arch + '.tar.gz'
        archive = directory / name
        digest = hashlib.sha256()
        with archive.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        if digest.hexdigest() != checks.get(name):
            raise ValueError('Bundle checksum mismatch: ' + name)
        with tarfile.open(archive) as bundle:
            build = json.load(bundle.extractfile('clash-linux/BUILD.json'))
        if build['version'] != version or build['architecture'] != arch:
            raise ValueError('Bundle version/architecture mismatch: ' + name)
        current = (build['data_format'], build['min_upgrade_version'])
        if current != (1, 'v0.1.0') or compatibility not in (None, current):
            raise ValueError('Unsupported or inconsistent compatibility metadata')
        compatibility = current
        assets[arch] = {'name': name, 'sha256': digest.hexdigest(), 'size': archive.stat().st_size}
    return {'format': 1, 'version': version, 'data_format': compatibility[0],
            'min_upgrade_version': compatibility[1], 'assets': assets,
            'notes_url': 'https://github.com/lzt2323/clash/releases/tag/' + version}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    result = metadata(args.directory, args.version)
    destination = args.directory / 'latest.json'
    temporary = destination.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(result, indent=2) + '\n')
    temporary.replace(destination)
    print('Verified metadata: ' + str(destination))


if __name__ == '__main__':
    main()
