"""Install and run Keystone's fixed local MCP presets over stdio."""
import json
import os
from pathlib import Path
import platform
import re
import shutil
import time

from PySide6.QtCore import QLockFile, QProcess, QProcessEnvironment

from .mcp_http import LEGACY, MAX_BODY, McpHttp, RpcError


# The package and binary names are fixed here. The UI supplies only settings.
LOCAL = {
    'obsidian': ('obsidian-mcp', '2.0.1', 'obsidian-mcp', 22),
    'musicbrainz': ('@unclick/musicbrainz-mcp', '0.1.0', 'musicbrainz-mcp', 20),
    'discogs': ('discogs-mcp-server', '0.5.7', 'discogs-mcp-server', 20),
    'square': ('square-mcp-server', '0.1.2', 'square-mcp-server', 22),
    'feishu': ('@larksuiteoapi/lark-mcp', '0.5.1', 'lark-mcp', 20),
    'wecom': ('@wecom/cli', '1.3.4', 'wecom-cli', 18),
    'yuque': ('yuque-mcp', '1.0.0', 'yuque-mcp', 18),
    'minimax-cn': ('minimax-mcp-js', '0.0.18', 'minimax-mcp-js', 20),
}
OBSIDIAN_READ_TOOLS = frozenset({'obsidian_list_vaults', 'obsidian_read_note', 'obsidian_search_vault'})


def vault_path(value):
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_dir() or not (path / '.obsidian').is_dir():
        raise ValueError('请选择包含 .obsidian 的 Obsidian 仓库目录。')
    return str(path)


def _process(program, args, root, values=None):
    env = QProcessEnvironment()
    allowed = {'path', 'systemroot', 'windir', 'comspec', 'temp', 'tmp', 'lang', 'lc_all',
               'systemdrive', 'pathext', 'https_proxy', 'http_proxy', 'no_proxy', 'node_extra_ca_certs'}
    for key, value in os.environ.items():
        if key.lower() in allowed:
            env.insert(key, value)
    for key in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA'):
        env.insert(key, str(root))
    for key, value in (values or {}).items():
        env.insert(key, value)
    process = QProcess()
    process.setProgram(str(program))
    process.setArguments(list(args))
    process.setWorkingDirectory(str(root))
    process.setProcessEnvironment(env)
    process.start()
    if not process.waitForStarted(5000):
        raise RuntimeError('无法启动本机 MCP 进程。')
    return process


def _stop(process):
    if process.state() != QProcess.ProcessState.NotRunning:
        process.closeWriteChannel()
        if not process.waitForFinished(300):
            process.kill()
            process.waitForFinished(2000)


def _run(program, args, root, cancel, timeout, *, env=None, failure=None, on_line=None):
    process = _process(program, args, root, env)
    failure = failure or '本机 MCP 安装失败，请检查 npm 网络和 Node.js 版本后重试。'
    deadline = time.monotonic() + timeout
    output = bytearray()
    consumed = 0
    try:
        while process.state() != QProcess.ProcessState.NotRunning:
            if cancel.is_set() or time.monotonic() >= deadline:
                raise RuntimeError('本机连接操作已取消或超时。')
            process.waitForFinished(100)
            output.extend(bytes(process.readAllStandardOutput()))
            while on_line and (end := output.find(b'\n', consumed)) >= 0:
                on_line(output[consumed:end].decode('utf-8', 'replace').strip())
                consumed = end + 1
            if len(output) > MAX_BODY:
                raise RuntimeError('本机安装程序输出超出限制。')
            process.readAllStandardError()
        output.extend(bytes(process.readAllStandardOutput()))
        if len(output) > MAX_BODY:
            raise RuntimeError('本机程序输出超出限制。')
        if on_line:
            for line in output[consumed:].decode('utf-8', 'replace').splitlines():
                on_line(line.strip())
        if cancel.is_set():
            raise RuntimeError('本机连接操作已取消。')
        if process.exitStatus() != QProcess.ExitStatus.NormalExit or process.exitCode():
            raise RuntimeError(failure)
        return output.decode('utf-8', 'replace').strip()
    finally:
        _stop(process)


def _entry(root, package, version, binary):
    folder = root / 'node_modules' / package
    try:
        metadata = json.loads((folder / 'package.json').read_text(encoding='utf-8'))
        if metadata['name'] != package or metadata['version'] != version:
            return None
        entry = (folder / metadata['bin'][binary]).resolve()
        return entry if entry.is_relative_to(folder.resolve()) and entry.is_file() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _wecom_binary(root, version):
    machine = platform.machine().lower()
    arch = {'amd64': 'x64', 'x86_64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(machine)
    system = {'windows': 'win32', 'darwin': 'darwin', 'linux': 'linux'}.get(platform.system().lower())
    if not system or not arch or (system == 'win32' and arch != 'x64'):
        raise RuntimeError('企业微信官方 CLI 暂不支持此系统架构。')
    package = f'@wecom/cli-{system}-{arch}'
    folder = root / 'node_modules' / package
    binary = folder / 'bin' / ('wecom-cli.exe' if system == 'win32' else 'wecom-cli')
    try:
        metadata = json.loads((folder / 'package.json').read_text(encoding='utf-8'))
        if (metadata['name'] == package and metadata['version'] == version and binary.is_file()
                and binary.resolve().is_relative_to(folder.resolve())):
            return package, binary
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return package, None


def local_command(home, key, cancel):
    package, version, binary, minimum = LOCAL[key]
    root = home / 'gui' / 'qt' / 'mcp-local' / key
    root.mkdir(parents=True, exist_ok=True)
    node = shutil.which('node')
    if node is None and os.name == 'nt':
        candidate = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'nodejs' / 'node.exe'
        if candidate.is_file():
            node = str(candidate)
    if node is None:
        raise RuntimeError(f'此服务需要 Node.js {minimum} 或更高版本。')
    installed_version = re.match(r'v(\d+)\.', _run(node, ['--version'], root, cancel, 5))
    if not installed_version or int(installed_version[1]) < minimum:
        raise RuntimeError(f'此服务需要 Node.js {minimum} 或更高版本。')
    def installed():
        if key == 'wecom':
            return _wecom_binary(root, version)[1]
        return _entry(root, package, version, binary)

    entry = installed()
    if entry:
        return node, entry, root
    npm_candidates = [Path(node).parent / 'node_modules/npm/bin/npm-cli.js',
                      Path(node).parent.parent / 'lib/node_modules/npm/bin/npm-cli.js']
    if os.name != 'nt':
        npm_on_path = shutil.which('npm')
        if npm_on_path:
            npm_candidates.append(Path(npm_on_path).resolve())
    npm = next((path for path in npm_candidates if path.is_file()), None)
    if npm is None:
        raise RuntimeError('找不到 Node.js 自带的 npm。')
    lock = QLockFile(str(root / '.install.lock'))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        raise RuntimeError('另一个 Keystone 窗口正在安装此服务。')
    try:
        entry = installed()
        if entry is None:
            install_package = _wecom_binary(root, version)[0] if key == 'wecom' else package
            _run(node, [str(npm), 'install', '--prefix', str(root), '--save-exact',
                        '--ignore-scripts', '--no-audit', '--no-fund', '--omit=optional', '--fetch-retries', '1',
                        '--fetch-timeout', '30000', '--cache', str(root.parent / 'npm-cache'),
                        '--registry', 'https://registry.npmjs.org/', f'{install_package}@{version}'], root, cancel, 210)
            entry = installed()
        if entry is None:
            raise RuntimeError('安装完成后找不到 MCP 服务入口。')
        return node, entry, root
    finally:
        lock.unlock()


class McpStdio(McpHttp):
    def __init__(self, program, args, root, cancel, *, env=None, allowed_tools=None):
        self.cancel = cancel
        self.allowed_tools = allowed_tools
        self.version = LEGACY[0]
        self.counter = 0
        self.buffer = b''
        self.process = _process(program, args, root, env)
        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def _discover(self):
        return self._post('tools/list')

    def _post(self, method, params=None, *, notification=False):
        self.counter += 1
        identifier = self.counter
        row = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            row['params'] = params
        if not notification:
            row['id'] = identifier
        if self.cancel.is_set():
            raise RuntimeError('本机 MCP 请求已取消。')
        if self.process.write(json.dumps(row).encode() + b'\n') < 0:
            raise RuntimeError('本机 MCP 进程未接受请求。')
        if notification:
            return {}
        deadline = time.monotonic() + 35
        remaining = MAX_BODY
        while not self.cancel.is_set() and time.monotonic() < deadline:
            line, separator, rest = self.buffer.partition(b'\n')
            if not separator:
                if self.process.state() == QProcess.ProcessState.NotRunning:
                    break
                self.process.waitForReadyRead(100)
                data = bytes(self.process.readAllStandardOutput())
                self.process.readAllStandardError()
                remaining -= len(data)
                if remaining < 0:
                    raise ValueError('本机 MCP 响应超过 2 MiB。')
                self.buffer += data
                continue
            self.buffer = rest
            try:
                reply = json.loads(line)
            except (UnicodeDecodeError, ValueError):
                continue  # Some stdio servers emit startup text.
            if not isinstance(reply, dict) or reply.get('jsonrpc') != '2.0' or reply.get('id') != identifier:
                continue
            if isinstance(reply.get('error'), dict):
                code = reply['error'].get('code')
                raise RpcError(code if isinstance(code, int) else '未知错误')
            if isinstance(reply.get('result'), dict):
                return reply['result']
            raise ValueError('本机 MCP 返回了无效的结果。')
        raise RuntimeError('本机 MCP 请求已取消、超时或进程已退出。')

    def close(self):
        _stop(self.process)
