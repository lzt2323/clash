#!/usr/bin/env python3
"""Verify a generated full package; CI runs this inside an isolated network namespace."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

archive = Path(sys.argv[1]).resolve()
checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
assert archive.with_suffix(archive.suffix + '.sha256').read_text().split()[0] == checksum
with tempfile.TemporaryDirectory(prefix='clash-offline-') as directory:
    root = Path(directory)
    source = root / 'package'
    source.mkdir()
    with tarfile.open(archive) as package:
        for member in package:
            path = Path(member.name)
            assert member.isfile() and not path.is_absolute() and '..' not in path.parts
            target = source / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(package.extractfile(member).read())
            target.chmod(member.mode)
    build = json.loads((source / 'BUILD.json').read_text())
    assert build['offline'] is True
    for name, expected in build['files'].items():
        assert hashlib.sha256((source / name).read_bytes()).hexdigest() == expected, name
    assert (source / 'licenses/mihomo/LICENSE').stat().st_size > 0
    expected_core = gzip.decompress((source / 'mihomo.gz').read_bytes())
    home = root / 'home'
    home.mkdir()
    # A missing environment archive proves the bundled file takes precedence.
    env = dict(os.environ, HOME=str(home), SHELL='/bin/bash', MIHOMO_ARCHIVE=str(root / 'absent.gz'))
    binary = source / 'clash'
    for method in ('extracted', 'archive'):
        prefix = root / (method + ' install with spaces')
        command = [str(binary), 'self-install', '--prefix', str(prefix), '--no-shell']
        if method == 'archive':
            command += ['--archive', str(archive), '--sha256', checksum]
        subprocess.run(command, env=env, check=True, timeout=30)
        assert (prefix / 'bin/mihomo').read_bytes() == expected_core
        assert (prefix / 'licenses/mihomo/LICENSE').is_file()
        subprocess.run([str(prefix / 'bin/mihomo'), '-v'], check=True, timeout=5)
        # Upgrade must preserve the existing core and data even with a full package.
        (prefix / 'conf').mkdir(exist_ok=True)
        (prefix / 'conf/keep').write_text('keep')
        subprocess.run([str(prefix / 'clash'), 'upgrade', '--archive', str(archive),
                        '--sha256', checksum], env=env, check=True, timeout=30)
        assert (prefix / 'bin/mihomo').read_bytes() == expected_core
        assert (prefix / 'conf/keep').read_text() == 'keep'
        subprocess.run([str(prefix / 'clash'), 'uninstall', '--purge'], env=env, check=True, timeout=15)
        assert not prefix.exists()
    for content in (None, b'corrupt'):
        bundled = source / 'mihomo.gz'
        if content is None:
            bundled.unlink()
        else:
            bundled.write_bytes(content)
        prefix = root / 'rejected'
        result = subprocess.run([str(binary), 'self-install', '--prefix', str(prefix), '--no-shell'],
                                env=env, capture_output=True, text=True, timeout=15)
        assert result.returncode != 0, result.stdout
        assert ('离线包缺少' if content is None else 'SHA-256') in result.stderr, result.stderr
        assert not prefix.exists()
print('Offline extraction, archive installation, upgrade, purge and corrupt/missing core checks passed.')
