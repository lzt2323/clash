#!/usr/bin/env python3
"""Small terminal manager. Subscription YAML is parsed by Mihomo, never by a shell."""
import copy
import fcntl
import functools
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class Error(RuntimeError):
    pass


class OperationCancelled(Error):
    pass


def mutation(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(self.directory / 'lock', 'a') as lock:
            os.chmod(lock.name, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Error('另一个操作正在进行，请稍后重试。') from None
            self._recover()
            self._checkpoint()
            return method(self, *args, **kwargs)
    return wrapped


class Manager:
    def __init__(self, root=None):
        self.cancel_requested = lambda: False
        self.root = Path(root or Path(__file__).resolve().parent.parent).resolve()
        self.directory = self.root / 'runtime' / 'mvp'
        self.state_path = self.directory / 'state.json'
        self.journal = self.directory / 'transaction.json'
        self.conf_dir = self.root / 'conf'
        self.config = self.conf_dir / 'config.yaml'
        self.runtime = os.environ.get('CLASH_RUNTIME_SCRIPT', str(self.root / 'scripts/mihomo_runtime.sh'))
        self.api_url = os.environ.get('MIHOMO_API_URL', 'http://127.0.0.1:9090').rstrip('/')
        self.http_url = os.environ.get('CLASH_HTTP_PROXY', 'http://127.0.0.1:7890')
        self.socks_url = os.environ.get('CLASH_SOCKS_PROXY', 'socks5h://127.0.0.1:7891')
        for url, scheme in [(self.api_url, 'http'), (self.http_url, 'http'), (self.socks_url, 'socks5h')]:
            parsed = urllib.parse.urlsplit(url)
            if parsed.hostname != '127.0.0.1' or parsed.scheme != scheme or not parsed.port or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
                raise Error('MVP 仅支持 127.0.0.1 上的代理和控制端口。请检查环境设置。')
        self.local_http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _checkpoint(self):
        if self.cancel_requested():
            raise OperationCancelled("已取消，原设置未修改。")

    def _load(self):
        if not self.state_path.exists():
            return {'version': 1, 'active_id': None, 'mode': 'rule', 'secret': secrets.token_hex(24), 'subscriptions': []}
        try:
            state = json.loads(self.state_path.read_text())
            if state['version'] != 1 or not isinstance(state['subscriptions'], list):
                raise ValueError()
            return state
        except (ValueError, KeyError):
            raise Error('订阅记录损坏，请保留 runtime/mvp 目录并恢复备份。') from None

    @staticmethod
    def _atomic(path, data):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temp = tempfile.mkstemp(prefix='.mvp-', dir=str(path.parent))
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data if isinstance(data, bytes) else data.encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def _save(self, state):
        self._atomic(self.state_path, json.dumps(state, ensure_ascii=False, indent=2))

    def _runtime(self, command, check=True):
        env = dict(os.environ, MIHOMO_CONFIG=str(self.config), MIHOMO_CONFIG_DIR=str(self.conf_dir),
                   MIHOMO_RUNTIME_DIR=str(self.root / 'runtime'), MIHOMO_API_URL=self.api_url,
                   MIHOMO_LOG=os.environ.get('MIHOMO_LOG', str(self.root / 'logs/mihomo.log')))
        # Read the secret from the config, not an unrelated legacy .env value.
        env.pop('MIHOMO_API_SECRET', None)
        if self.config.exists():
            try:
                env['MIHOMO_API_SECRET'] = json.loads(self.config.read_text())['secret']
            except (ValueError, KeyError):
                pass
        result = subprocess.run([self.runtime, command], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, timeout=40)
        if check and result.returncode:
            raise Error({'start': '核心启动失败，请检查端口占用或运行日志。',
                         'stop': '核心停止失败，请检查运行状态。',
                         'binary': '缺少 Mihomo 内核，请先运行 ./clash install。'}.get(command, '核心操作失败，请运行诊断。'))
        return result

    def _running(self):
        return self._runtime('running', check=False).returncode == 0

    def _healthy(self):
        return self._runtime('health', check=False).returncode == 0

    def _api(self, path, method='GET', data=None, state=None, timeout=5):
        state = state or self._load()
        req = urllib.request.Request(self.api_url + path,
                                     data=None if data is None else json.dumps(data).encode(), method=method,
                                     headers={'Authorization': 'Bearer ' + state['secret'], 'Content-Type': 'application/json'})
        try:
            with self.local_http.open(req, timeout=timeout) as response:
                body = response.read()
            return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            raise Error('控制接口操作失败（HTTP %s），请检查核心状态。' % exc.code) from None
        except (OSError, ValueError, http.client.HTTPException):
            raise Error('控制接口连接失败，请检查核心状态。') from None

    def _fetch(self, url):
        if any(ord(char) <= 32 or ord(char) == 127 for char in url):
            raise Error('订阅链接不能包含空格或控制字符。')
        try:
            parsed = urllib.parse.urlsplit(url)
            parsed.port
        except ValueError:
            raise Error('订阅链接格式有误，请重新粘贴。') from None
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise Error('请输入 http:// 或 https:// 订阅链接。')
        self._checkpoint()
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'clash-linux-mvp/1.0'})
            with urllib.request.urlopen(request, timeout=20) as response:
                if urllib.parse.urlsplit(response.url).scheme not in ('http', 'https'):
                    raise Error('订阅重定向地址不受支持。')
                chunks, size = [], 0
                deadline = time.monotonic() + 30
                while size <= 10 * 1024 * 1024:
                    self._checkpoint()
                    if time.monotonic() > deadline:
                        raise Error('订阅下载超时，原订阅未修改。')
                    chunk = response.read1(min(65536, 10 * 1024 * 1024 + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                body = b''.join(chunks)
                self._checkpoint()
            if not body or len(body) > 10 * 1024 * 1024:
                raise Error('订阅为空或超过 10 MB，请检查链接。')
            return body
        except (OSError, ValueError, http.client.HTTPException):
            raise Error('订阅下载失败，请检查链接和网络；原订阅未修改。') from None

    def _validate_provider(self, body):
        """Isolated loopback-only core: no proxy listeners or automatic health checks."""
        binary = self._runtime('binary').stdout.strip()
        with tempfile.TemporaryDirectory(prefix='clash-check-') as tmp:
            base = Path(tmp)
            (base / 'nodes.yaml').write_bytes(body)
            os.chmod(base / 'nodes.yaml', 0o600)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            secret = secrets.token_hex(24)
            config = {'external-controller': '127.0.0.1:' + str(port), 'secret': secret,
                      'allow-lan': False, 'log-level': 'silent', 'ipv6': False,
                      'proxy-providers': {'subscription': {'type': 'file', 'path': './nodes.yaml',
                                                          'health-check': {'enable': False}}},
                      'proxy-groups': [{'name': 'CHECK', 'type': 'select', 'use': ['subscription']}],
                      'rules': ['MATCH,CHECK']}
            (base / 'config.yaml').write_text(json.dumps(config))
            with open(base / 'check.log', 'wb') as log:
                process = subprocess.Popen([binary, '-d', tmp, '-f', str(base / 'config.yaml')], stdout=log, stderr=log)
                try:
                    deadline = time.monotonic() + 10
                    while process.poll() is None and time.monotonic() < deadline:
                        self._checkpoint()
                        try:
                            req = urllib.request.Request('http://127.0.0.1:%s/providers/proxies/subscription' % port,
                                                         headers={'Authorization': 'Bearer ' + secret})
                            with self.local_http.open(req, timeout=.5) as response:
                                payload = json.load(response)
                            names = [item['name'] for item in payload.get('proxies', [])]
                            if names:
                                if len(names) != len(set(names)) or any(n in ('AUTO', 'PROXY', 'GLOBAL', 'DIRECT', 'REJECT', 'CHECK') for n in names):
                                    raise Error('节点名称重复或使用保留名称（AUTO / PROXY 等），请更换订阅格式。')
                                return names
                        except (OSError, ValueError, KeyError):
                            pass
                        time.sleep(.1)
                    raise Error('订阅没有可用节点，或格式不受支持。需要 Clash/Mihomo YAML 格式。')
                finally:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()

    def _validate_config(self, path):
        binary = self._runtime('binary').stdout.strip()
        result = subprocess.run([binary, '-t', '-d', str(self.conf_dir), '-f', str(path)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        if result.returncode:
            raise Error('配置校验失败，原配置未修改。')

    @staticmethod
    def _item(state, identity=None):
        identity = identity or state['active_id']
        for item in state['subscriptions']:
            if item['id'] == identity:
                return item
        raise Error('还没有可用订阅，请在 clash 菜单中添加。')

    def _config_text(self, state):
        item = self._item(state)
        cache = self.conf_dir / item['cache']
        if not cache.is_file() or hashlib.sha256(cache.read_bytes()).hexdigest() != cache.stem:
            raise Error('订阅缓存缺失或损坏，请在订阅菜单中更新此订阅。')
        config = {'port': urllib.parse.urlsplit(self.http_url).port,
                  'socks-port': urllib.parse.urlsplit(self.socks_url).port,
                  'allow-lan': False, 'bind-address': '127.0.0.1', 'mode': state['mode'],
                  'log-level': 'warning', 'ipv6': True,
                  'external-controller': urllib.parse.urlsplit(self.api_url).netloc, 'secret': state['secret'],
                  'profile': {'store-selected': False},
                  'proxy-providers': {'subscription': {'type': 'file', 'path': item['cache'],
                                                      'health-check': {'enable': False}}},
                  'proxy-groups': [{'name': 'PROXY', 'type': 'select', 'proxies': ['AUTO'], 'use': ['subscription']},
                                   {'name': 'AUTO', 'type': 'url-test', 'use': ['subscription'],
                                    'url': 'https://www.gstatic.com/generate_204', 'interval': 600, 'lazy': True}],
                  'rules': ['DOMAIN,localhost,DIRECT', 'IP-CIDR,127.0.0.0/8,DIRECT,no-resolve',
                            'IP-CIDR,10.0.0.0/8,DIRECT,no-resolve', 'IP-CIDR,172.16.0.0/12,DIRECT,no-resolve',
                            'IP-CIDR,192.168.0.0/16,DIRECT,no-resolve', 'IP-CIDR,169.254.0.0/16,DIRECT,no-resolve',
                            'IP-CIDR6,::1/128,DIRECT,no-resolve', 'IP-CIDR6,fc00::/7,DIRECT,no-resolve',
                            'IP-CIDR6,fe80::/10,DIRECT,no-resolve', 'MATCH,PROXY']}
        return json.dumps(config, ensure_ascii=False, indent=2)

    def _restore_selection(self, state):
        item = self._item(state)
        # The controller can serve /version before its file provider is ready.
        # Wait for the expected provider snapshot before restoring either group.
        deadline = time.monotonic() + 8
        while True:
            try:
                payload = self._api('/providers/proxies/subscription', state=state, timeout=1)
                actual = [p['name'] for p in payload.get('proxies', [])]
                if set(actual) == set(item['nodes']):
                    break
            except Error:
                pass
            if time.monotonic() >= deadline:
                raise Error('节点未正确加载，配置应用失败。')
            time.sleep(.1)
        node = item['selected']
        self._api('/proxies/PROXY', 'PUT', {'name': node}, state)
        self._api('/proxies/GLOBAL', 'PUT', {'name': 'PROXY'}, state)

    def _reload(self, state):
        self._api('/configs?force=true', 'PUT', {'path': str(self.config)}, state)
        self._restore_selection(state)

    def _recover(self):
        if not self.journal.exists():
            return
        record = json.loads(self.journal.read_text())
        old = record['state']
        if record['config'] is None:
            self.config.unlink(missing_ok=True)
        else:
            self._atomic(self.config, record['config'])
        if record['running']:
            if not self._healthy():
                if self._running():
                    self._runtime('stop')
                self._runtime('start')
            self._reload(old)
        if record['had_state']:
            self._save(old)
        else:
            self.state_path.unlink(missing_ok=True)
        self.journal.unlink()

    def _apply(self, old, new):
        self.conf_dir.mkdir(parents=True, exist_ok=True)
        candidate = self.conf_dir / '.mvp-candidate.yaml'
        self._atomic(candidate, self._config_text(new))
        try:
            self._validate_config(candidate)
            running = self._running()
            if running and not self._healthy():
                raise Error('核心正在运行，但接口不可用。请先诊断，原配置未修改。')
            record = {'state': old, 'config': self.config.read_text() if self.config.exists() else None,
                      'running': running, 'had_state': self.state_path.exists()}
            self._checkpoint()
            self._atomic(self.journal, json.dumps(record, ensure_ascii=False))
            try:
                self._atomic(self.config, candidate.read_bytes())
                if running:
                    self._reload(new)
                self._save(new)
                self.journal.unlink()
            except BaseException:
                try:
                    self._recover()
                except Exception:
                    raise Error('切换失败，回退尚未完成。旧记录已保留，请检查核心后重试。') from None
                raise
        finally:
            candidate.unlink(missing_ok=True)

    def _prepare(self, state, name, url, identity=None):
        name, url = name.strip(), url.strip()
        if not name or len(name) > 60 or any(ord(c) < 32 for c in name):
            raise Error('名称需要 1–60 个字符，不能包含控制字符。')
        for item in state['subscriptions']:
            if item['id'] != identity and item['url'] == url:
                raise Error('这个订阅链接已经保存。')
            if item['id'] != identity and item['name'] == name:
                raise Error('名称已经使用，请换一个名称。')
        body = self._fetch(url)
        self._checkpoint()
        nodes = self._validate_provider(body)
        self._checkpoint()
        identity = identity or uuid.uuid4().hex
        relative = 'mvp-providers/%s/%s.yaml' % (identity, hashlib.sha256(body).hexdigest())
        self._atomic(self.conf_dir / relative, body)
        return {'id': identity, 'name': name, 'url': url, 'cache': './' + relative,
                'nodes': nodes, 'selected': 'AUTO', 'updated': time.strftime('%Y-%m-%d %H:%M')}

    @mutation
    def add(self, name, url):
        old = self._load()
        # An existing full config needs explicit migration, never overwrite it silently.
        if not old['subscriptions'] and self.config.exists():
            raise Error('检测到旧配置。请先备份并移走 conf/config.yaml，再使用新订阅菜单。')
        new = copy.deepcopy(old)
        item = self._prepare(new, name, url)
        new['subscriptions'].append(item)
        if not new['active_id']:
            new['active_id'] = item['id']
            self._apply(old, new)
            return '已添加并设为当前订阅。退出菜单后运行 clash on。'
        self._checkpoint()
        self._save(new)
        return '已添加，当前订阅未改变。'

    @mutation
    def use(self, identity):
        old = self._load()
        self._item(old, identity)
        if old['active_id'] == identity:
            return '已经在使用这个订阅。'
        new = copy.deepcopy(old)
        new['active_id'] = identity
        self._apply(old, new)
        return '已切换订阅，当前终端开关保持不变。'

    def _edit(self, identity, url=None):
        old = self._load()
        item = self._item(old, identity)
        replacement = self._prepare(old, item['name'], url or item['url'], identity)
        selected = item['selected']
        replacement['selected'] = selected if selected == 'AUTO' or selected in replacement['nodes'] else 'AUTO'
        new = copy.deepcopy(old)
        new['subscriptions'] = [replacement if p['id'] == identity else p for p in new['subscriptions']]
        if identity == old['active_id']:
            self._apply(old, new)
        else:
            self._checkpoint()
            self._save(new)
        return '订阅已更新。' + ('原节点已消失，已恢复自动选择。' if selected != replacement['selected'] else '')

    @mutation
    def update(self, identity):
        return self._edit(identity)

    @mutation
    def edit(self, identity, url):
        return self._edit(identity, url)

    @mutation
    def remove(self, identity):
        state = self._load()
        self._item(state, identity)
        if state['active_id'] == identity:
            raise Error('不能删除当前订阅，请先切换到其他订阅。')
        state['subscriptions'] = [p for p in state['subscriptions'] if p['id'] != identity]
        self._checkpoint()
        self._save(state)
        # Keep immutable caches until explicit cleanup; no live provider file is removed.
        return '订阅记录已删除。'

    @mutation
    def select(self, node):
        old = self._load()
        new = copy.deepcopy(old)
        item = self._item(new)
        if node != 'AUTO' and node not in item['nodes']:
            raise Error('节点不存在，请重新打开列表。')
        item['selected'] = node
        self._apply(old, new)
        return '节点已保存。' + ('' if self._running() else '下次 clash start 或 clash on 时生效。')

    @mutation
    def set_mode(self, mode):
        if mode not in ('rule', 'global', 'direct'):
            raise Error('模式只能是 rule / global / direct。')
        old = self._load()
        new = copy.deepcopy(old)
        new['mode'] = mode
        self._apply(old, new)
        return '模式已保存。'

    def _check_ports(self):
        ports = [urllib.parse.urlsplit(url).port for url in (self.http_url, self.socks_url, self.api_url)]
        if len(set(ports)) != len(ports):
            raise Error('HTTP、SOCKS 和控制接口必须使用不同端口。')
        for port in ports:
            with socket.socket() as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(('127.0.0.1', port))
                except OSError:
                    raise Error('端口 %s 已被占用，请先停止占用该端口的程序。' % port) from None

    @mutation
    def ensure(self):
        state = self._load()
        self._item(state)
        if self._healthy():
            return '核心已就绪。'
        if self._running():
            raise Error('核心正在运行但接口异常，请在菜单中诊断。')
        self._check_ports()
        self._atomic(self.config, self._config_text(state))
        self._validate_config(self.config)
        self._checkpoint()
        self._runtime('start')
        try:
            self._restore_selection(state)
        except Exception:
            self._runtime('stop', check=False)
            raise
        return '核心已启动。'

    def status(self):
        state = self._load()
        active = next((p for p in state['subscriptions'] if p['id'] == state['active_id']), {})
        try:
            running = self._healthy()
            core_state = 'running' if running else ('unhealthy' if self._running() else 'stopped')
        except (OSError, subprocess.SubprocessError):
            running, core_state = False, 'unknown'
        proxies = [os.environ.get(key) for key in ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY')]
        expected = [self.http_url] * 4 + [self.socks_url] * 2
        shell = '已开启' if proxies == expected else ('未开启' if not any(proxies) else '部分设置或其他代理')
        if proxies == expected and not running:
            shell = '已设置，但核心未就绪'
        return {'running': running, 'core_state': core_state, 'active': state['active_id'], 'name': active.get('name', '未添加'),
                'mode': state['mode'], 'node': active.get('selected', 'AUTO'), 'shell_proxy': shell,
                'subscriptions': [{k: v for k, v in p.items() if k not in ('url', 'cache')} for p in state['subscriptions']]}

    @mutation
    def restart(self):
        state = self._load()
        self._item(state)
        candidate = self.conf_dir / '.mvp-restart.yaml'
        self._atomic(candidate, self._config_text(state))
        try:
            self._validate_config(candidate)
            self._checkpoint()
            self._runtime('stop')
            self._check_ports()
            self._atomic(self.config, candidate.read_bytes())
            self._runtime('start')
            try:
                self._restore_selection(state)
            except Exception:
                self._runtime('stop', check=False)
                raise
        finally:
            candidate.unlink(missing_ok=True)
        return '核心已重启。'

    @mutation
    def stop(self):
        self._runtime('stop')
        return '核心已停止，所有依赖该核心的代理连接已断开。'

    def delays_all(self):
        # Resolve in the worker so large subscriptions never cross the request-size limit.
        item = self._item(self._load())
        nodes = ['AUTO'] + [name for name in item['nodes'] if name not in ('AUTO', 'PROXY')]
        return self.delays(nodes)

    def delays(self, nodes):
        if not self._healthy():
            raise Error('请先启动核心（s 或 clash start），再测试延迟。')
        result = {}
        for node in nodes:
            self._checkpoint()
            try:
                endpoint = '/proxies/AUTO/delay?' if node == 'AUTO' else '/providers/proxies/subscription/' + urllib.parse.quote(node, safe='') + '/healthcheck?'
                path = endpoint + urllib.parse.urlencode({'timeout': 2000, 'url': 'https://www.gstatic.com/generate_204'})
                result[node] = self._api(path, timeout=3).get('delay', '失败')
            except Error:
                result[node] = '超时/失败'
        self._checkpoint()
        return result

    def diagnose(self):
        self._checkpoint()
        status = self.status()
        lines = ['核心接口：' + ('正常' if status['running'] else '未运行或异常'), '当前终端代理：' + status['shell_proxy']]
        if self.journal.exists():
            lines.append('上次操作中断：下一次修改将尝试回退。')
        if not status['running']:
            lines.append('下一步：按 s 或运行 clash start；失败时检查 logs/mihomo.log。')
            return lines
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPHandler())
            request = urllib.request.Request('http://www.gstatic.com/generate_204')
            request.set_proxy(urllib.parse.urlsplit(self.http_url).netloc, 'http')
            with opener.open(request, timeout=8) as response:
                lines.append('代理出站：' + ('正常' if response.status == 204 else '返回非预期状态'))
        except OSError:
            lines.append('代理出站：失败；尝试切换节点或更新订阅。')
        self._checkpoint()
        return lines


def worker(manager):
    """One bounded JSON request; credentials travel over stdin, never argv."""
    cancelled = [False]
    def cancel(_signum, _frame):
        cancelled[0] = True
    previous = {sig: signal.signal(sig, cancel) for sig in (signal.SIGINT, signal.SIGTERM)}
    manager.cancel_requested = lambda: cancelled[0]
    try:
        raw = sys.stdin.buffer.readline(65537)
        if len(raw) > 65536:
            raise Error('操作请求过长。')
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise Error('无效的操作请求。')
        actions = {'status': 0, 'add': 2, 'use': 1, 'update': 1, 'edit': 2,
                   'remove': 1, 'select': 1, 'set_mode': 1, 'delays': 1,
                   'diagnose': 0, 'ensure': 0, 'restart': 0, 'stop': 0, 'delays_all': 0}
        action, args = request.get('action'), request.get('args', [])
        if not isinstance(action, str) or action not in actions or not isinstance(args, list) or len(args) != actions[action]:
            raise Error('无效的操作请求。')
        if action == 'delays':
            if not isinstance(args[0], list) or len(args[0]) > 100 or not all(isinstance(n, str) and len(n) <= 4096 for n in args[0]):
                raise Error('无效的节点列表。')
        elif not all(isinstance(arg, str) and len(arg) <= 16384 for arg in args):
            raise Error('无效的操作参数。')
        manager._checkpoint()
        result = getattr(manager, action)(*args)
        payload = {'ok': True, 'result': result}
    except OperationCancelled as exc:
        payload = {'ok': False, 'error': str(exc), 'cancelled': True}
    except Error as exc:
        payload = {'ok': False, 'error': str(exc)}
    except Exception:
        # Library exceptions can contain a subscription URL or API credential.
        payload = {'ok': False, 'error': '操作失败，请检查安装、文件权限及核心状态。'}
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return 0 if payload['ok'] else 1


def main():
    try:
        manager = Manager()
        command = sys.argv[1] if len(sys.argv) > 1 else 'menu'
        if command == 'worker':
            return worker(manager)
        if command == 'menu':
            if sys.argv[2:] not in ([], ['--plain']):
                raise Error('用法：clash menu [--plain]')
            if sys.argv[2:] == ['--plain'] or os.environ.get('TERM', 'dumb') in ('dumb', ''):
                import terminal_menu
                return terminal_menu.run(manager) or 0
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                print('菜单需要交互终端；查看状态请运行 clash status。', file=sys.stderr)
                return 2
            try:
                import terminal_workbench
            except ImportError:
                import terminal_menu
                return terminal_menu.run(manager) or 0
            return terminal_workbench.run(manager) or 0
        if command == 'status':
            from terminal_menu import _clean
            status = manager.status()
            print('核心：' + {'running': '运行中', 'stopped': '已停止', 'unhealthy': '进程运行中，接口异常', 'unknown': '无法检查'}.get(status['core_state'], '未知'))
            print('当前终端代理：' + status['shell_proxy'])
            print('订阅：' + _clean(status['name']))
            print('模式：' + status['mode'] + ' / 节点：' + _clean(status['node']))
        elif command in ('ensure', 'start', 'stop', 'restart'):
            operation = manager.ensure if command in ('ensure', 'start') else getattr(manager, command)
            print(operation(), file=sys.stderr if command == 'ensure' else sys.stdout)
        else:
            raise Error('未知操作，请运行 clash 打开菜单。')
        return 0
    except (Error, OSError, subprocess.SubprocessError) as exc:
        print('错误：' + (str(exc) if isinstance(exc, Error) else '本地核心或文件操作失败，请检查安装和文件权限。'), file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print('\n已取消。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
