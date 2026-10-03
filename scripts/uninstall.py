#!/usr/bin/env python3
"""Remove only a marked installation and its manifest-listed application files."""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile


class UninstallError(RuntimeError):
    pass


def _exists(path):
    return os.path.lexists(path)


def _ancestors(root, path):
    """Never traverse a link inside the installation, including broken links."""
    relative = path.relative_to(root)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise UninstallError('安装目录内存在符号链接路径，请先检查并移走该链接。')
        if _exists(current) and not current.is_dir():
            raise UninstallError('安装目录结构异常，未执行删除。')


def _json_file(root, name):
    path = root / name
    _ancestors(root, path)
    if not _exists(path) or not stat.S_ISREG(path.lstat().st_mode):
        raise UninstallError('缺少有效安装标记或安装清单，拒绝卸载。')
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError()
        result = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError):
        raise UninstallError('安装标记或安装清单损坏，拒绝卸载。') from None


def _validate(root):
    root = Path(os.path.abspath(root))
    forbidden = {Path('/'), Path.home().resolve(), Path('/home'), Path('/root'),
                 Path('/usr'), Path('/usr/local'), Path('/etc'), Path('/var'), Path('/tmp')}
    if root in forbidden or root.is_symlink() or root.resolve() != root or not root.is_dir():
        raise UninstallError('安装路径不安全，拒绝卸载。')
    marker = _json_file(root, '.clash-install.json')
    if (type(marker.get('format')) is not int or marker['format'] != 1
            or marker.get('prefix') != str(root) or not isinstance(marker.get('version'), str)
            or not marker['version']):
        raise UninstallError('安装标记与当前目录不匹配，拒绝卸载。')
    build = _json_file(root, 'BUILD.json')
    if build.get('version') != marker['version'] or not isinstance(build.get('files'), list):
        raise UninstallError('安装清单与版本标记不匹配，拒绝卸载。')
    paths = []
    for name in build['files']:
        if not isinstance(name, str) or not name or '\\' in name or '\x00' in name:
            raise UninstallError('安装清单包含无效路径，拒绝卸载。')
        relative = PurePosixPath(name)
        if relative.is_absolute() or any(part in ('', '.', '..') for part in name.split('/')):
            raise UninstallError('安装清单包含越界路径，拒绝卸载。')
        path = root.joinpath(*relative.parts)
        _ancestors(root, path)
        if _exists(path) and not path.is_symlink() and path.is_dir():
            raise UninstallError('安装清单必须逐文件列出，拒绝目录递归删除。')
        if relative.parts[0] == 'conf' or relative.parts[:2] == ('runtime', 'mvp'):
            continue  # Data is controlled exclusively by --purge.
        if name == '.clash-data.json':
            continue
        paths.append(path)
    if not {'clash', 'scripts/uninstall.py'}.issubset(set(build['files'])):
        raise UninstallError('安装清单缺少程序入口，拒绝卸载。')
    for name in ('env.sh', '.clash-install.json', 'BUILD.json'):
        path = root / name
        if _exists(path) and not path.is_symlink() and not path.is_file():
            raise UninstallError('安装文件类型异常，拒绝卸载。')
        paths.append(path)
    return root, marker, set(paths)


@contextmanager
def _local_environment():
    keys = ('CLASH_RUNTIME_SCRIPT', 'MIHOMO_BINARY', 'MIHOMO_LOG',
            'MIHOMO_API_URL', 'CLASH_HTTP_PROXY', 'CLASH_SOCKS_PROXY')
    previous = {key: os.environ.pop(key) for key in keys if key in os.environ}
    try:
        yield
    finally:
        for key in keys:
            os.environ.pop(key, None)
        os.environ.update(previous)


def _remove_tree(path):
    """Only called for the two explicitly requested data roots; never follows links."""
    if not _exists(path):
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
        return
    for child in path.iterdir():
        _remove_tree(child)
    path.rmdir()


def _write_data_marker(root, marker):
    destination = root / '.clash-data.json'
    if _exists(destination) and (destination.is_symlink() or not destination.is_file()):
        raise UninstallError('数据标记路径异常，未执行卸载。')
    descriptor, temporary = tempfile.mkstemp(prefix='.clash-data-', dir=str(root))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump({'format': 1, 'version': marker['version'], 'prefix': str(root)}, stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if _exists(temporary):
            os.unlink(temporary)


def uninstall(root=None, purge=False, manager_factory=None, shell_runner=subprocess.run):
    root, marker, paths = _validate(root or Path(__file__).absolute().parents[1])
    data = root / 'runtime/mvp'
    for directory in (root / 'runtime', data):
        _ancestors(root, directory)
        if _exists(directory) and (directory.is_symlink() or not directory.is_dir()):
            raise UninstallError('运行目录不是本地普通目录，拒绝卸载。')
    lock_path = data / 'lock'
    _ancestors(root, lock_path)
    if _exists(lock_path) and (lock_path.is_symlink() or not lock_path.is_file()):
        raise UninstallError('操作锁路径异常，拒绝卸载。')
    # Check all sensitive paths before shell cleanup or application deletion.
    if _exists(root / '.clash-data.json'):
        if not stat.S_ISREG((root / '.clash-data.json').lstat().st_mode):
            raise UninstallError('数据标记路径异常，拒绝卸载。')
    script = root / 'scripts/shell_integration.sh'
    _ancestors(root, script)
    if not _exists(script) or not stat.S_ISREG(script.lstat().st_mode):
        raise UninstallError('缺少本项目 shell 清理脚本，拒绝卸载。')
    data.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UninstallError('另一个操作正在进行，稍后重试卸载。') from None
        if _exists(data / 'transaction.json'):
            raise UninstallError('存在未完成的配置事务，请先在 clash 中恢复状态后重试卸载。')
        pid = root / 'runtime/mihomo.pid'
        if _exists(pid):
            if pid.is_symlink() or not pid.is_file():
                raise UninstallError('核心 PID 文件路径异常，拒绝卸载。')
            for name in ('scripts/mihomo_runtime.sh', 'bin/mihomo',
                         'bin/clash-linux-amd64', 'bin/clash-linux-arm64',
                         'bin/clash-linux-armv7', 'conf/config.yaml'):
                path = root / name
                _ancestors(root, path)
                if path.is_symlink():
                    raise UninstallError('核心文件指向安装目录之外，拒绝卸载。')
            with _local_environment():
                if manager_factory is None:
                    from terminal_mvp import Manager
                    manager_factory = Manager
                manager = manager_factory(root=root)
                manager.runtime = str(root / 'scripts/mihomo_runtime.sh')
                try:
                    result = manager._runtime('stop')
                    if getattr(result, 'returncode', 0):
                        raise RuntimeError()
                except Exception:
                    raise UninstallError('核心停止失败，未删除程序。请检查 clash status 后重试。') from None
        for shell in ('bash', 'zsh'):
            result = shell_runner(['bash', str(script), 'uninstall', '--shell', shell],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if result.returncode:
                raise UninstallError('shell 集成清理失败，未删除程序。')
        if not purge:
            _write_data_marker(root, marker)
        # Remove files only, then remove empty directories. Unknown files survive.
        directories = set()
        for path in sorted(paths, key=lambda value: len(value.parts), reverse=True):
            _ancestors(root, path)
            if _exists(path):
                path.unlink()
            directories.update(parent for parent in path.parents if parent != root and root in parent.parents)
        if purge:
            _remove_tree(root / 'conf')
            _remove_tree(data)
            if _exists(root / '.clash-data.json'):
                (root / '.clash-data.json').unlink()
        for directory in sorted(directories | {root / 'runtime'}, key=lambda value: len(value.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
    remaining = [path.name for path in root.iterdir() if path.name not in ('conf', 'runtime', '.clash-data.json')]
    messages = ['卸载完成。' + ('订阅数据已清除。' if purge else '订阅数据已保留，可在原目录重新安装。')]
    if remaining:
        messages.append('目录中仍有未列入安装清单的文件，已保留，请自行检查。')
    return messages


def main():
    parser = argparse.ArgumentParser(description='卸载本安装；默认保留订阅数据。')
    parser.add_argument('--purge', action='store_true', help='同时清除 conf 和 runtime/mvp 订阅数据')
    args = parser.parse_args()
    try:
        for message in uninstall(purge=args.purge):
            print(message)
        return 0
    except (UninstallError, OSError, subprocess.SubprocessError):
        # Do not expose file contents or a library exception containing credentials.
        error = sys.exc_info()[1]
        print('卸载失败：' + (str(error) if isinstance(error, UninstallError) else '本地文件或进程操作失败，未继续删除。'), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
