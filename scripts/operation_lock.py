#!/usr/bin/env python3
"""Cross-version lifecycle lock, stored outside the replaceable installation."""
import argparse
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import sys


class LockError(RuntimeError):
    pass


def lock_path(root):
    root = Path(os.path.abspath(root))
    return root.parent / ('.' + root.name + '.operation.lock')


def inherited_fd(root, exclusive=False):
    """An environment flag alone never exempts a process from locking."""
    try:
        if os.environ.get('CLASH_OPERATION_LOCK_ROOT') != str(Path(os.path.abspath(root))):
            return None
        mode = os.environ.get('CLASH_OPERATION_LOCK_MODE')
        if mode not in ('shared', 'exclusive') or (exclusive and mode != 'exclusive'):
            return None
        fd = int(os.environ['CLASH_OPERATION_LOCK_FD'])
        existing, expected = os.fstat(fd), lock_path(root).lstat()
        if not stat.S_ISREG(expected.st_mode) or (existing.st_dev, existing.st_ino) != (expected.st_dev, expected.st_ino):
            return None
        # Also acquire/assert the advertised kernel lock: a forged environment
        # with an open but unlocked descriptor must not bypass exclusion.
        fcntl.flock(fd, (fcntl.LOCK_EX if mode == 'exclusive' else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        return fd
    except (KeyError, ValueError, OSError):
        return None


@contextmanager
def operation_lock(root, exclusive=False):
    root = Path(os.path.abspath(root))
    inherited = inherited_fd(root, exclusive)
    if inherited is not None:
        if not exclusive and os.environ.get('CLASH_OPERATION_LOCK_MODE') != 'exclusive' and (root.parent / ('.' + root.name + '.upgrade.json')).exists():
            raise LockError('存在未完成的升级，请先运行 clash recover。')
        yield inherited
        return
    path = lock_path(root)
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise LockError('操作锁不是普通文件，请检查安装目录。')
        try:
            fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LockError('安装、升级、卸载或菜单正在使用此目录，请先退出其他菜单并稍后重试。') from None
    except OSError:
        raise LockError('无法创建安装操作锁，请检查安装目录权限。') from None
    keys = ('CLASH_OPERATION_LOCK_FD', 'CLASH_OPERATION_LOCK_ROOT', 'CLASH_OPERATION_LOCK_MODE')
    previous = {key: os.environ.get(key) for key in keys}
    os.set_inheritable(fd, True)
    os.environ.update(dict(zip(keys, (str(fd), str(root), 'exclusive' if exclusive else 'shared'))))
    try:
        journal = root.parent / ('.' + root.name + '.upgrade.json')
        if not exclusive and journal.exists():
            raise LockError('存在未完成的升级，请先运行 clash recover；如命令缺失，使用最新版安装器 --recover。')
        yield fd
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        os.close(fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--exclusive', action='store_true')
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ['--']:
        command = command[1:]
    if not command:
        parser.error('缺少命令')
    try:
        with operation_lock(args.root, args.exclusive) as fd:
            # exec preserves terminal signals and avoids an extra surviving parent.
            os.execvpe(command[0], command, os.environ)
    except (LockError, OSError) as exc:
        print('错误：' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
