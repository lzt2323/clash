#!/usr/bin/env python3
"""Mirror a published GitHub release and advance the download site's entry points."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.request

from release_metadata import metadata


def sha(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def mirror(version, web, backup):
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('Expected stable release version')
    web, backup = Path(web).resolve(), Path(backup).absolute()
    if not web.is_dir() or not (web / 'releases').is_dir():
        raise ValueError('Expected existing download site')
    if backup.exists() or backup.is_symlink():
        raise ValueError('Choose a new backup directory')
    with urllib.request.urlopen('https://api.github.com/repos/lzt2323/clash/releases/tags/' + version,
                                timeout=30) as response:
        release = json.load(response)
    if release['tag_name'] != version or release['draft'] or release['prerelease']:
        raise ValueError('Expected published stable release')
    assets = {asset['name']: asset for asset in release['assets']}
    names = ['clash-linux-amd64.tar.gz', 'clash-linux-arm64.tar.gz',
             'SHA256SUMS', 'latest.json', 'install.sh', 'get.sh',
             'clash-linux-source-' + version + '.tar.gz']
    if not set(names) <= set(assets):
        raise ValueError('Release assets are incomplete')
    destination = web / 'releases' / version
    if destination.exists():
        raise ValueError('Refusing to replace an immutable release')
    stage = Path(tempfile.mkdtemp(prefix='.' + version + '-', dir=destination.parent))
    try:
        for name in names:
            asset = assets[name]
            url = 'https://github.com/lzt2323/clash/releases/download/' + version + '/' + name
            if asset['browser_download_url'] != url:
                raise ValueError('Unexpected asset URL')
            subprocess.run(['curl', '-fLsS', '--retry', '3', '--connect-timeout', '15', '--max-time', '300',
                            '--proto', '=https', '--proto-redir', '=https', url, '-o', str(stage / name)], check=True)
            if ((stage / name).stat().st_size != asset['size']
                    or asset.get('digest') != 'sha256:' + sha(stage / name)):
                raise ValueError('GitHub asset digest mismatch: ' + name)
            (stage / name).chmod(0o644)
            print('Verified:', name, flush=True)
        if metadata(stage, version) != json.loads((stage / 'latest.json').read_text()):
            raise ValueError('latest.json does not describe the verified bundles')
        if '\nVERSION=' + version + '\n' not in (stage / 'install.sh').read_text():
            raise ValueError('Installer default differs from release')
        if '\nversion=' + version + '\n' not in (stage / 'get.sh').read_text():
            raise ValueError('Bootstrap default differs from release')
        entry_names = ('install.sh', 'get.sh', 'index.html', 'latest.json')
        for name in entry_names:
            if (web / name).is_symlink() or ((web / name).exists() and not (web / name).is_file()):
                raise ValueError('Site entry is not a regular file: ' + name)
        backup.mkdir(mode=0o700)
        for name in entry_names:
            if (web / name).exists():
                shutil.copy2(web / name, backup / name)
        stage.chmod(0o755)
        os.replace(stage, destination)
        index = (web / 'index.html').read_text()
        index = re.sub(r'v[0-9]+\.[0-9]+\.[0-9]+', version, index)
        index = index.replace('任务导航即时预览。已有安装的升级限制见', '支持保留配置升级与回滚。已有安装请参阅')
        # Publish complete version directory first and discovery metadata last.
        try:
            for name in ('install.sh', 'get.sh', 'index.html', 'latest.json'):
                temporary = web / ('.' + name + '.publish-tmp')
                if name == 'index.html':
                    temporary.write_text(index)
                else:
                    shutil.copyfile(destination / name, temporary)
                temporary.chmod(0o644)
                os.replace(temporary, web / name)
        except Exception:
            for name in entry_names:
                if (backup / name).exists():
                    shutil.copy2(backup / name, web / name)
                elif (web / name).exists():
                    (web / name).unlink()
            raise
        print('Published:', version, 'Entry backup:', backup, flush=True)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', required=True)
    parser.add_argument('--web-root', type=Path, required=True)
    parser.add_argument('--backup-dir', type=Path, required=True)
    args = parser.parse_args()
    mirror(args.version, args.web_root, args.backup_dir)


if __name__ == '__main__':
    main()
