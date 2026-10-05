#!/usr/bin/env python3
"""Transactional, manifest based upgrades of marked standalone installations.

The journal and flock live beside the installation, so renaming a release does
not invalidate either. An interrupted switch always restores the old release;
only a durable committed journal permits completing the new release.
"""
import argparse
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).absolute().parent))
from operation_lock import operation_lock, LockError

SERVER = 'https://download.getplus.dpdns.org'
GITHUB = 'https://github.com/lzt2323/clash/releases/download'
RESERVED = {'.clash-install.json', '.clash-data.json', 'BUILD.json'}


class UpgradeError(RuntimeError):
    pass


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', value):
        raise UpgradeError('版本必须是 vX.Y.Z 格式的正式版本。')
    return tuple(map(int, value[1:].split('.')))


def safe_name(name):
    if (not isinstance(name, str) or not name or '\\' in name or '\x00' in name
            or name.startswith('/') or any(p in ('', '.', '..') for p in name.split('/'))):
        raise UpgradeError('文件清单或压缩包包含不安全路径。')
    return name


def data_path(name):
    return (name.split('/')[0] in ('conf', 'runtime', 'logs')
            or name in ('.env', 'env.sh', '.clash-data.json'))


def read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise UpgradeError('缺少有效的本地安装标记或事务清单。')
    try:
        result = json.loads(path.read_text())
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError):
        raise UpgradeError('安装标记或事务清单损坏。') from None


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.upgrade-write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def tree_files(root):
    """Refuse links and special files even in user data; never follow them."""
    result = set()
    def fail(error):
        raise error
    for base, directories, files in os.walk(root, followlinks=False, onerror=fail):
        for name in directories + files:
            path = Path(base) / name
            mode = path.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise UpgradeError('安装目录含符号链接或特殊文件，请先移走后重试。')
            if stat.S_ISREG(mode):
                result.add(path.relative_to(root).as_posix())
    return result


def build_info(root, installed=False, prefix=None):
    if root.is_symlink() or not root.is_dir():
        raise UpgradeError('安装目录或备份必须是实体目录。')
    build = read_json(root / 'BUILD.json')
    version(build.get('version'))
    files = build.get('files')
    if not isinstance(files, list) or not files:
        raise UpgradeError('程序文件清单无效。')
    for name in files:
        safe_name(name)
        if data_path(name) or name in RESERVED - {'BUILD.json'}:
            raise UpgradeError('程序清单不得占用用户数据路径。')
    if len(set(files)) != len(files):
        raise UpgradeError('程序清单存在重复文件。')
    if not {'clash', 'python/bin/python3', 'bin/mihomo', 'scripts/mihomo_runtime.sh'}.issubset(files):
        raise UpgradeError('发行包缺少必要程序。')
    if build.get('architecture') not in ('amd64', 'arm64'):
        raise UpgradeError('不支持此安装架构。')
    if type(build.get('data_format', 1)) is not int or build.get('data_format', 1) != 1:
        raise UpgradeError('暂不支持此数据格式，未执行迁移。')
    actual = tree_files(root)
    if installed:
        marker = read_json(root / '.clash-install.json')
        if (type(marker.get('format')) is not int or marker['format'] != 1
                or marker.get('prefix') != str(prefix or root)
                or marker.get('version') != build['version']):
            raise UpgradeError('安装标记与路径或版本不匹配；源码目录请先迁移为正式安装。')
        if not set(files).issubset(actual):
            raise UpgradeError('已安装程序文件缺失，请先恢复安装。')
    elif actual != set(files) | {'BUILD.json'}:
        raise UpgradeError('发行包文件与 BUILD.json 清单不一致。')
    return build


def digest(path):
    result = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def extract(archive, target, expected):
    if not isinstance(expected, str) or not re.fullmatch('[a-fA-F0-9]{64}', expected):
        raise UpgradeError('需要有效 SHA-256 摘要。')
    if digest(archive) != expected.lower():
        raise UpgradeError('安装包 SHA-256 校验失败。')
    with tarfile.open(archive, 'r:gz') as source:
        members = source.getmembers()
        seen, size = set(), 0
        for member in members:
            name = safe_name(member.name.rstrip('/'))
            if (name in seen or name.split('/')[0] != 'clash-linux'
                    or not (member.isfile() or member.isdir())):
                raise UpgradeError('压缩包含重复路径、链接或特殊文件。')
            seen.add(name)
            relative = '/'.join(name.split('/')[1:])
            if relative and (data_path(relative) or relative in RESERVED - {'BUILD.json'}):
                raise UpgradeError('发行包不得包含用户数据或安装标记。')
            size += member.size
            if len(seen) > 100000 or size > 4 * 1024 ** 3:
                raise UpgradeError('发行包展开大小超过安全上限。')
        if shutil.disk_usage(target.parent).free < size + 32 * 1024 ** 2:
            raise UpgradeError('磁盘空间不足，未停止核心。')
        for member in members:
            path = target.joinpath(*PurePosixPath(member.name).parts[1:])
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as incoming, path.open('xb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                path.chmod(member.mode & 0o777)


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlsplit(newurl).scheme != 'https':
            raise UpgradeError('下载重定向必须使用 HTTPS。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, destination=None, limit=1024 * 1024):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password:
        raise UpgradeError('下载源必须使用无凭据的 HTTPS 地址。')
    request = urllib.request.Request(url, headers={'User-Agent': 'clash-linux-upgrade'})
    with urllib.request.build_opener(HTTPSRedirect()).open(request, timeout=30) as response:
        if destination:
            with open(destination, 'wb') as stream:
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 1024 ** 3:
                        raise UpgradeError('下载包大小超过安全上限。')
                    stream.write(chunk)
            return None
        content = response.read(limit + 1)
        if len(content) > limit:
            raise UpgradeError('下载元数据过大。')
        return content


def latest(server, github):
    if server:
        try:
            metadata = json.loads(fetch(server.rstrip('/') + '/latest.json'))
            if not isinstance(metadata, dict):
                raise UpgradeError('元数据必须是对象。')
            version(metadata.get('version'))
            if metadata.get('format') != 1 or metadata.get('data_format', 1) != 1:
                raise UpgradeError('主站升级元数据格式不支持。')
            assets = metadata.get('assets', {})
            if not isinstance(assets, dict) or any(not isinstance(asset, dict) for asset in assets.values()):
                raise UpgradeError('发行包元数据无效。')
            for arch, asset in assets.items():
                if (arch not in ('amd64', 'arm64') or asset.get('name') != 'clash-linux-%s.tar.gz' % arch
                        or not isinstance(asset.get('sha256'), str)
                        or not re.fullmatch('[a-fA-F0-9]{64}', asset['sha256'])):
                    raise UpgradeError('发行包元数据无效。')
            if 'min_upgrade_version' in metadata:
                version(metadata['min_upgrade_version'])
            return metadata
        except (OSError, ValueError, UpgradeError):
            pass
    match = re.fullmatch(r'https://github.com/([^/]+)/([^/]+)/releases/download/?', github or '')
    if not match:
        raise UpgradeError('主站元数据不可用，且 GitHub 下载基址无法用于版本发现；可指定 --version。')
    try:
        release = json.loads(fetch('https://api.github.com/repos/%s/%s/releases/latest' % match.groups()))
        if not isinstance(release, dict):
            raise UpgradeError('GitHub 元数据无效。')
        tag = release.get('tag_name')
        version(tag)
        return {'version': tag}
    except (OSError, ValueError, UpgradeError):
        raise UpgradeError('无法查询最新版本；可使用 --version 或离线安装包。') from None


def download(version_name, architecture, directory, server, github, metadata=None):
    filename = 'clash-linux-%s.tar.gz' % architecture
    expected = ((metadata or {}).get('assets', {}).get(architecture, {}).get('sha256'))
    target = directory / 'package.tar.gz'
    bases = ([server.rstrip('/') + '/releases/' + version_name] if server else [])
    bases += ([github.rstrip('/') + '/' + version_name] if github else [])
    for base in bases:
        try:
            sums = fetch(base + '/SHA256SUMS').decode('ascii')
            matches = [line.split()[0] for line in sums.splitlines()
                       if len(line.split()) == 2 and line.split()[1].lstrip('*') == filename]
            if len(matches) != 1 or not re.fullmatch('[a-fA-F0-9]{64}', matches[0]):
                continue
            checksum = matches[0].lower()
            if expected and checksum != expected.lower():
                continue
            fetch(base + '/' + filename, target)
            if digest(target) == checksum:
                return target, checksum
        except (OSError, ValueError, UpgradeError):
            continue
    raise UpgradeError('主源及同版本 GitHub 安装包均不可用或校验失败。')


def legacy_busy(root):
    """Legacy TUIs hold no lifetime lock. Refuse visible Linux workers."""
    proc = Path('/proc')
    if not proc.is_dir():
        return
    for path in proc.glob('[0-9]*/cmdline'):
        if path.parent.name == str(os.getpid()):
            continue
        try:
            args = path.read_bytes().split(b'\0')
        except OSError:
            continue
        for arg in args:
            text = os.fsdecode(arg)
            if not text.startswith('/') and text.endswith('.py'):
                try:
                    text = str((path.parent / 'cwd').resolve() / text)
                except OSError:
                    continue
            if text in [str(root / 'scripts' / name) for name in
                        ('terminal_menu.py', 'terminal_workbench.py', 'terminal_mvp.py')]:
                raise UpgradeError('检测到仍在运行的 TUI 或状态操作，请退出菜单后重试。')


@contextmanager
def state_lock(root):
    directory = root / 'runtime/mvp'
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / 'lock'
    if lock_path.is_symlink():
        raise UpgradeError('状态锁不能是符号链接。')
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpgradeError('另一个状态操作正在进行，请稍后重试。') from None
        if (directory / 'transaction.json').exists():
            raise UpgradeError('存在未完成配置事务，请先运行 clash 恢复后再升级。')
        legacy_busy(root)
        yield


class Upgrader:
    def __init__(self, root, runner=subprocess.run, checkpoint=None):
        self.root = Path(os.path.abspath(root))
        if (self.root == Path.home().resolve() or len(self.root.parts) < 3
                or self.root.resolve() != self.root or self.root.is_symlink()):
            raise UpgradeError('安装路径不安全。')
        self.parent = self.root.parent
        self.journal = self.parent / ('.' + self.root.name + '.upgrade.json')
        self.previous = self.parent / ('.' + self.root.name + '.previous')
        self.runner = runner
        self.checkpoint = checkpoint or (lambda phase: None)

    def run(self, command, check=True, overrides=None, inherit_lock=True):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('MIHOMO_', 'CLASH_RUNTIME_', 'PYTHON'))}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        env.update(overrides or {})
        descriptors = ()
        if not inherit_lock:
            for key in ('CLASH_OPERATION_LOCK_FD', 'CLASH_OPERATION_LOCK_ROOT', 'CLASH_OPERATION_LOCK_MODE'):
                env.pop(key, None)
        try:
            descriptor = int(env.get('CLASH_OPERATION_LOCK_FD', '-1'))
            if descriptor >= 0:
                os.fstat(descriptor)
                descriptors = (descriptor,)
        except (ValueError, OSError):
            pass
        result = self.runner([str(arg) for arg in command], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=env, timeout=90, pass_fds=descriptors)
        if check and result.returncode:
            raise UpgradeError('程序或核心健康检查失败；请检查本地日志。')
        return result.returncode

    def runtime_environment(self):
        config_path = self.root / 'conf/config.yaml'
        env = {'MIHOMO_BINARY': str(self.root / 'bin/mihomo'),
               'MIHOMO_CONFIG': str(config_path), 'MIHOMO_CONFIG_DIR': str(self.root / 'conf'),
               'MIHOMO_RUNTIME_DIR': str(self.root / 'runtime'),
               'MIHOMO_LOG': str(self.root / 'logs/mihomo.log')}
        if config_path.is_file():
            content = config_path.read_text()
            try:
                config = json.loads(content)
                if not isinstance(config, dict):
                    raise ValueError()
            except ValueError:
                # Legacy YAML uses ordinary top-level scalar controller/secret
                # fields. Never evaluate YAML tags or execute the user's .env.
                config = {}
                for line in content.splitlines():
                    match = re.match(r'^(external-controller|secret):\s*(.*?)\s*$', line)
                    if match:
                        scalar = match[2].split(' #', 1)[0].strip()
                        if scalar.startswith('"'):
                            try:
                                scalar = json.loads(scalar)
                            except ValueError:
                                continue
                        elif scalar.startswith("'") and scalar.endswith("'"):
                            scalar = scalar[1:-1].replace("''", "'")
                        config[match[1]] = scalar
            controller = config.get('external-controller', '127.0.0.1:9090')
            endpoint = urllib.parse.urlsplit('http://' + str(controller))
            if endpoint.hostname not in ('127.0.0.1', 'localhost', '0.0.0.0', '::1') or not endpoint.port:
                raise UpgradeError('升级健康检查需要有效的本地控制器地址。')
            env['MIHOMO_API_URL'] = 'http://127.0.0.1:%s' % endpoint.port
            secret = config.get('secret')
            if isinstance(secret, str):
                env['MIHOMO_API_SECRET'] = secret
        return env

    def runtime(self, action, check=True):
        return self.run(['bash', self.root / 'scripts/mihomo_runtime.sh', action],
                        check, self.runtime_environment(), inherit_lock=False)

    def restore_selection(self):
        if not (self.root / 'runtime/mvp/state.json').is_file():
            return
        self.run([self.root / 'python/bin/python3', '-E', '-s', '-B', '-c',
                  'import sys;sys.path.insert(0,sys.argv[1]+\"/scripts\");'
                  'from terminal_mvp import Manager;m=Manager(root=sys.argv[1]);'
                  'm._restore_selection(m._load())', self.root],
                 overrides=self.runtime_environment())

    def preflight(self, candidate):
        for name in ('clash', 'python/bin/python3', 'bin/mihomo', 'scripts/mihomo_runtime.sh'):
            if not os.access(candidate / name, os.X_OK):
                raise UpgradeError('安装包必要程序不可执行。')
        self.run([candidate / 'python/bin/python3', '-E', '-s', '-B', '-c', 'import ssl,curses,fcntl'])
        self.run([candidate / 'bin/mihomo', '-v'])
        self.run(['bash', candidate / 'clash', 'help'])
        self.run([candidate / 'python/bin/python3', '-E', '-s', '-B', '-c',
                  'import pathlib,sys; [compile(p.read_text(), str(p), \"exec\") for p in pathlib.Path(sys.argv[1]).glob(\"*.py\")]',
                  candidate / 'scripts'])

    def preserve(self, source, target, old):
        application = set(old['files']) | RESERVED
        user_directories = set()
        program_directories = {parent.as_posix() for name in application
                               for parent in PurePosixPath(name).parents if str(parent) != '.'}
        for name in sorted(tree_files(source)):
            if name in application and not data_path(name):
                continue
            # PID and state lock files are ephemeral, never copy lock identity.
            if name in ('runtime/mihomo.pid', 'runtime/mihomo.pid.new', 'runtime/mvp/lock'):
                continue
            origin, destination = source / name, target / name
            if destination.exists() or any(p.is_file() for p in destination.parents if p != target):
                raise UpgradeError('用户文件与新版程序冲突：' + name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(origin, destination)
            user_directories.update(parent.as_posix() for parent in PurePosixPath(name).parents
                                    if str(parent) != '.')
        # Empty user directories should survive too.
        for base, directories, _ in os.walk(source, topdown=False):
            for name in directories:
                relative = (Path(base) / name).relative_to(source)
                destination = target / relative
                if destination.is_file():
                    raise UpgradeError('用户目录与新版程序冲突：' + relative.as_posix())
                if not destination.exists():
                    destination.mkdir(parents=True, exist_ok=True)
                if (data_path(relative.as_posix()) or relative.as_posix() in user_directories
                        or relative.as_posix() not in program_directories):
                    # User files may rely on ancestor 0700 for confidentiality.
                    # Apply directory modes after copying all files.
                    shutil.copystat(source / relative, destination)

    def health(self, running):
        self.run(['bash', self.root / 'clash', 'help'])
        self.run([self.root / 'python/bin/python3', '-E', '-s', '-B', '-c', 'import ssl,curses,fcntl'])
        if (self.root / 'conf/config.yaml').exists():
            self.runtime('validate')
        if running:
            self.runtime('start')
            self.runtime('health')
            self.restore_selection()

    def _journal_record(self):
        record = read_json(self.journal)
        work = Path(record.get('work', ''))
        if (type(record.get('format')) is not int or record.get('format') != 1 or record.get('root') != str(self.root)
                or record.get('phase') not in ('prepared', 'switching', 'committed')
                or type(record.get('running')) is not bool
                or work.parent != self.parent or not work.name.startswith('.' + self.root.name + '.upgrade-work-')
                or work.is_symlink() or work.resolve() != work or not work.is_dir()):
            raise UpgradeError('升级事务路径或格式无效，拒绝自动恢复。')
        return record, work

    def _clear_transaction(self, work):
        # Keep a replayable journal and its work directory until every retired
        # tree (which may contain private data) has been removed. Only an empty
        # directory can be orphaned by the final journal-unlink/rmdir gap.
        for child in work.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        fsync_directory(work)
        self.checkpoint('work-cleared')
        self.journal.unlink()
        fsync_directory(self.parent)
        work.rmdir()
        fsync_directory(self.parent)

    def _finish(self, work):
        old = work / 'old'
        if old.exists():
            build_info(old, installed=True, prefix=self.root)
            if self.previous.exists():
                build_info(self.previous, installed=True, prefix=self.root)
                discarded = work / 'discarded'
                if discarded.exists():
                    raise UpgradeError('备份收尾目录冲突，请保留事务并人工检查。')
                # Never partly delete the named previous release. A crash after
                # this rename can replay finalization without parsing a tree
                # interrupted halfway through rmtree.
                os.replace(self.previous, discarded)
                fsync_directory(work)
                fsync_directory(self.parent)
                self.checkpoint('previous-retired')
            os.replace(old, self.previous)
            fsync_directory(work)
            fsync_directory(self.parent)
            self.checkpoint('backup-published')
        self._clear_transaction(work)

    def recover_external(self):
        """Re-enter recovery after a process exit, including old releases.

        Internal failure recovery already holds its original state lock. Public
        recovery must acquire the old in-tree locks as well as the stable lock,
        because a pre-v0.2.0 process does not know the latter exists.
        """
        if not os.path.lexists(self.journal):
            return self.recover()
        _, work = self._journal_record()
        legacy_busy(self.root)
        with ExitStack() as locks:
            for tree in (self.root, work / 'old'):
                if tree.exists():
                    build_info(tree, installed=True, prefix=self.root)
                    locks.enter_context(state_lock(tree))
            return self.recover()

    def recover(self):
        if not os.path.lexists(self.journal):
            return '没有待恢复的升级事务。'
        record, work = self._journal_record()
        if record['phase'] == 'committed':
            build_info(self.root, installed=True)
            self._finish(work)
            return '已完成上次升级的收尾。'
        old = work / 'old'
        if old.exists():
            build_info(old, installed=True, prefix=self.root)
            if self.root.exists():
                build_info(self.root, installed=True)
                if self.runtime('running', check=False) == 0:
                    self.runtime('stop')
                rejected = work / 'rejected'
                if rejected.exists():
                    raise UpgradeError('恢复目录冲突，请保留事务目录并人工检查。')
                os.replace(self.root, rejected)
            os.replace(old, self.root)
            fsync_directory(self.parent)
        else:
            build_info(self.root, installed=True)
        if record['phase'] == 'switching' and record['running']:
            if self.runtime('running', check=False) != 0:
                self.runtime('start')
            self.runtime('health')
            self.restore_selection()
        self._clear_transaction(work)
        return '已恢复上次升级前的版本和核心状态。'

    def switch(self, work, candidate, old_build, new_build):
        running = self.runtime('running', check=False) == 0
        record = {'format': 1, 'root': str(self.root), 'work': str(work),
                  'running': running, 'phase': 'prepared'}
        # Journal before the first copy of private data. Before switching, the
        # live installation remains authoritative and recovery only discards
        # the incomplete candidate.
        write_json(self.journal, record)
        try:
            self.checkpoint('prepared')
            self.preserve(self.root, candidate, old_build)
            self.checkpoint('data-copied')
            marker = read_json(self.root / '.clash-install.json')
            marker['version'] = new_build['version']
            write_json(candidate / '.clash-install.json', marker)
            candidate.chmod(stat.S_IMODE(self.root.stat().st_mode))
            # Flush staged file contents and directory entries before the
            # write-ahead switching record authorizes stopping the old core.
            for name in tree_files(candidate):
                with open(candidate / name, 'rb') as stream:
                    os.fsync(stream.fileno())
            for base, _, _ in os.walk(candidate, topdown=False):
                fsync_directory(base)
            record['phase'] = 'switching'
            write_json(self.journal, record)
            if running:
                self.runtime('stop')
            self.checkpoint('stopped')
            os.replace(self.root, work / 'old')
            fsync_directory(self.parent)
            fsync_directory(work)
            self.checkpoint('old-renamed')
            os.replace(candidate, self.root)
            fsync_directory(self.parent)
            fsync_directory(work)
            self.checkpoint('new-renamed')
            self.health(running)
            self.checkpoint('healthy')
            record['phase'] = 'committed'
            write_json(self.journal, record)
        except Exception:
            self.recover()
            raise
        self.checkpoint('committed')
        self._finish(work)
        return new_build['version']

    def execute(self, action='upgrade', check=False, version_name=None, archive=None, sha256=None,
                server=SERVER, github=GITHUB):
        readonly = action == 'app-version' or check
        with operation_lock(self.root, exclusive=not readonly):
            if readonly and self.journal.exists():
                raise UpgradeError('存在未完成升级，请先运行 clash recover 或安装器 --recover。')
            if action == 'recover':
                return self.recover_external()
            if self.journal.exists():
                self.recover_external()
            current = build_info(self.root, installed=True)
            if action == 'app-version':
                return current['version']
            if check:
                if archive or action != 'upgrade':
                    raise UpgradeError('--check 仅用于在线版本检查。')
                latest_version = version_name or latest(server, github)['version']
                return '当前 %s；最新 %s；%s' % (current['version'], latest_version,
                    '可升级' if version(latest_version) > version(current['version']) else '已是最新版本')
            with state_lock(self.root):
                if self.previous.is_symlink():
                    raise UpgradeError('上一版本备份不能是符号链接。')
                if self.previous.exists():
                    build_info(self.previous, installed=True, prefix=self.root)
                metadata = None
                if action == 'upgrade' and not archive and not version_name:
                    metadata = latest(server, github)
                    version_name = metadata['version']
                if version_name:
                    version(version_name)
                if action == 'upgrade' and version_name == current['version']:
                    return '当前已是 ' + version_name
                if version_name and version(version_name) < version(current['version']):
                    raise UpgradeError('升级不允许降级；请使用 clash rollback。')
                work = Path(tempfile.mkdtemp(prefix='.' + self.root.name + '.upgrade-work-', dir=self.parent))
                candidate = work / 'candidate'
                try:
                    if action == 'rollback':
                        previous = build_info(self.previous, installed=True, prefix=self.root)
                        candidate.mkdir()
                        for name in set(previous['files']) | {'BUILD.json'}:
                            destination = candidate / name
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(self.previous / name, destination)
                    else:
                        if archive:
                            if not sha256:
                                raise UpgradeError('离线升级需要 --archive 和 --sha256。')
                        else:
                            if sha256:
                                raise UpgradeError('--sha256 只能与 --archive 一起使用。')
                            archive, sha256 = download(version_name, current['architecture'], work, server, github, metadata)
                        candidate.mkdir()
                        extract(Path(archive), candidate, sha256)
                    proposed = build_info(candidate)
                    if proposed['architecture'] != current['architecture']:
                        raise UpgradeError('安装包架构与当前安装不符。')
                    host = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine())
                    if host and proposed['architecture'] != host:
                        raise UpgradeError('安装包架构与当前机器不符。')
                    if action == 'upgrade':
                        if version_name and proposed['version'] != version_name:
                            raise UpgradeError('安装包版本与指定版本不符。')
                        if proposed['version'] == current['version']:
                            return '当前已是 ' + proposed['version']
                        if version(proposed['version']) < version(current['version']):
                            raise UpgradeError('安装包不是更新版本；降级请使用 rollback。')
                        if version(current['version']) < version(proposed.get('min_upgrade_version', 'v0.1.0')):
                            raise UpgradeError('当前版本低于最低支持升级版本。')
                    if proposed.get('data_format', 1) != current.get('data_format', 1):
                        raise UpgradeError('数据格式不兼容，拒绝升级或回滚。')
                    if (self.root in Path(sys.executable).absolute().parents
                            and proposed.get('python') != current.get('python')):
                        raise UpgradeError('Python 运行时版本有变化；请从独立下载的新安装器执行 --upgrade，'
                                           '或从独立发行目录运行 rollback。')
                    self.preflight(candidate)
                    result = self.switch(work, candidate, current, proposed)
                    return ('回滚完成：' if action == 'rollback' else '升级完成：') + result
                finally:
                    if not self.journal.exists() and work.exists():
                        shutil.rmtree(work)


def main():
    parser = argparse.ArgumentParser(description='完整工作台升级与事务恢复；保留配置、订阅和用户文件。')
    parser.add_argument('action', nargs='?', default='upgrade', choices=('upgrade', 'rollback', 'recover', 'app-version'))
    parser.add_argument('--root', default=str(Path(__file__).absolute().parents[1]))
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--version', dest='version_name')
    parser.add_argument('--archive')
    parser.add_argument('--sha256')
    parser.add_argument('--server', default=SERVER)
    parser.add_argument('--github', default=GITHUB)
    args = vars(parser.parse_args())
    root = args.pop('root')
    try:
        print(Upgrader(root).execute(**args))
        return 0
    except (UpgradeError, OSError, ValueError, subprocess.SubprocessError, tarfile.TarError, RuntimeError) as error:
        print('升级未完成：' + (str(error) if isinstance(error, (UpgradeError, LockError)) else
              '本地文件、锁或进程操作失败；如存在未完成事务，请运行 clash recover 或安装器 --recover。'), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
