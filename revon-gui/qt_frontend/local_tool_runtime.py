"""Local Work tools with host-owned dependency installation and bounded I/O."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import platform
import shutil
import re
import stat
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from revon.secret_http import open_authenticated
from revon.secrets import redact

from .archive_reader import ArchiveError, library_path, open_archive

COMFY = 'http://127.0.0.1:8188'
COMFY_RELEASE = 'https://github.com/Comfy-Org/ComfyUI/releases/download/v0.45.0/'
COMFY_ASSETS = {
    'nvidia': ('ComfyUI_windows_portable_nvidia.7z', 2071479423,
               '60c12e8c7af778047ad0413722dd3dc0f6ca773a41cbf5cb229ee9bc92c44a6f'),
    'cpu': ('ComfyUI_windows_portable_nvidia.7z', 2071479423,
            '60c12e8c7af778047ad0413722dd3dc0f6ca773a41cbf5cb229ee9bc92c44a6f'),
    'amd': ('ComfyUI_windows_portable_amd.7z', 1688825308,
            '636f41a1975ad40cf226868ae06a62f7f5bb157532cc10cf3475ff54547e063e'),
}
COMFY_DIRECTORIES = {'nvidia': 'ComfyUI_windows_portable', 'cpu': 'ComfyUI_windows_portable',
                     'amd': 'ComfyUI_windows_portable_amd'}
WINDOWS_PORTABLE = os.name == 'nt' and platform.machine().lower() in {'amd64', 'x86_64'} and library_path().is_file()
MEDIA = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif', '.bmp', '.tif', '.tiff',
         '.mp4', '.webm', '.mov', '.wav', '.mp3', '.flac', '.ogg', '.m4a'}
MIB = 1024 * 1024


class ToolFailure(Exception):
    def __init__(self, message, state='not_started', **details):
        super().__init__(message)
        self.state = state
        self.details = details


def _check(cancel, deadline):
    if cancel.is_set():
        raise ToolFailure('已取消操作。')
    if time.monotonic() >= deadline:
        raise ToolFailure('本地工具操作超时。')


def _linked(path):
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def workspace_path(workspace, raw):
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096:
        raise ToolFailure('请提供有效的工作区路径。')
    candidate = Path(raw)
    try:
        root = Path(workspace).resolve(strict=True)
        path = candidate if candidate.is_absolute() else root / candidate
        relative = path.relative_to(root)
        if not relative.parts or any(p.casefold() in {'..', '.git', '.revon', '.revon-development', '.keystone', '.keystone-development', '.forge', '.forge-development'} for p in relative.parts):
            raise ValueError
        for part in relative.parts:
            _check_component(part)
        for p in (path, *path.parents):
            if p == root:
                break
            try:
                if _linked(p):
                    raise ValueError
            except FileNotFoundError:
                pass
        if not path.resolve().is_relative_to(root):
            raise ValueError
        return path
    except (ValueError, OSError, RuntimeError):
        raise ToolFailure('路径必须位于工作区内，且不能经过链接或保留目录。') from None


def _check_component(part):
    if (part in {'', '.', '..'} or part.endswith((' ', '.')) or
            any(ord(c) < 32 or c in '<>:"|?*' for c in part) or
            PureWindowsPath(part).is_reserved()):
        raise ValueError('invalid path component')


@contextmanager
def _new_output(workspace, destination):
    dest = workspace_path(workspace, destination)
    if not dest.parent.is_dir() or dest.exists():
        raise ToolFailure('请选择尚不存在的输出目录，并确保其父目录已存在。')
    with tempfile.TemporaryDirectory(prefix='.revon-tool-', dir=dest.parent) as tmp:
        staged = Path(tmp) / 'output'
        staged.mkdir()
        yield staged, dest
        if workspace_path(workspace, destination) != dest or dest.exists():
            raise ToolFailure('输出目录已发生变化，请选择其他目录。')
        try:
            staged.rename(dest)
        except OSError:
            raise ToolFailure('无法发布输出目录，请检查写入权限和目录状态。') from None


def _extract_archive(path, output, cancel, *, max_entries, max_bytes, memory, seconds):
    deadline = time.monotonic() + seconds
    names, rows, total = set(), [], 0
    running = lambda: not cancel.is_set() and time.monotonic() < deadline

    with open_archive(path, running, memory) as archive:
        for entry in archive.entries(max_entries):
            _check(cancel, deadline)
            member = entry.name.replace(chr(92), '/')
            if entry.folder:
                member = member.rstrip('/')
                if member == '.':
                    continue
            if member.startswith('./'):
                member = member[2:]
            parts = tuple(member.split('/'))
            try:
                for part in parts:
                    _check_component(part)
            except ValueError:
                raise ToolFailure('归档包含不可用的文件名。') from None
            key = tuple(part.casefold() for part in parts)
            if key in names or any(part in {'.git', '.revon', '.revon-development', '.keystone', '.keystone-development', '.forge', '.forge-development'} for part in key):
                raise ToolFailure('归档包含重名条目或保留目录。')
            names.add(key)
            total += entry.size
            if total > max_bytes:
                raise ToolFailure('归档解压大小超出单次操作上限。')
            rows.append((entry, parts))
        files = []
        for entry, parts in rows:
            _check(cancel, deadline)
            target = output.joinpath(*parts)
            if entry.folder:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with target.open('xb') as stream:
                for chunk in archive.chunks(entry):
                    _check(cancel, deadline)
                    size += len(chunk)
                    if size > entry.size:
                        raise ToolFailure('解压内容与归档目录的大小不符。')
                    stream.write(chunk)
            if size != entry.size:
                raise ToolFailure('解压内容与归档目录的大小不符。')
            files.append('/'.join(parts))
        _check(cancel, deadline)
        return files


def _download(base, asset, directory, cancel, progress):
    name, size, digest = asset
    target = directory / name
    if shutil.disk_usage(directory).free < size:
        raise ToolFailure('本地依赖目录的可用空间不足。')
    deadline = time.monotonic() + 3600
    total, last = 0, -1
    checksum = hashlib.sha256()
    request = Request(base + name, headers={'User-Agent': 'revon desktop dependency installer'})
    with urlopen(request, timeout=20) as response, target.open('xb') as out:
        parsed = urlsplit(response.url)
        if parsed.scheme != 'https' or parsed.hostname not in {'github.com', 'release-assets.githubusercontent.com'}:
            raise ToolFailure('下载地址不属于官方发布源。')
        while True:
            _check(cancel, deadline)
            chunk = response.read(MIB)
            if not chunk:
                break
            total += len(chunk)
            if total > size:
                raise ToolFailure('下载大小和官方发布包不符。')
            checksum.update(chunk)
            out.write(chunk)
            percent = total * 100 // size
            if percent != last:
                progress(f'正在下载 {name}', percent)
                last = percent
    if total != size or checksum.hexdigest() != digest:
        raise ToolFailure('下载校验失败，请重试。')
    return target


def schemas():
    fields = {'path': {'type': 'string'}, 'destination': {'type': 'string'}}
    rows = (
        ('7zip_extract', 'Extract a workspace .7z, ZIP or TAR archive using revon’s integrated decoder. Specify a new '
         'destination directory inside the workspace. Return the actual written paths.', fields),
        ('comfyui_run', 'Run an existing workspace ComfyUI workflow exported in API format on the local service. '
         'The workflow must have a file output node and its models installed. Optionally set a text node by giving both '
         'prompt_node and prompt. Choose a new workspace destination. revon asks the human before downloading ComfyUI. '
         'Return actual saved media paths. Never resubmit a job whose outcome is unknown.',
         {**fields, 'prompt_node': {'type': 'string'}, 'prompt': {'type': 'string'}}),
    )
    return {f'keystone_{name}': ({'type': 'function', 'function': {
        'name': f'keystone_{name}', 'description': description,
        'parameters': {'type': 'object', 'required': ['path', 'destination'],
                       'properties': properties, 'additionalProperties': False},
    }}, ('local', name)) for name, description, properties in rows}


class LocalToolRuntime:
    def __init__(self, home, choose, progress):
        self.root = Path(home) / 'local-tools'
        self.choose, self.progress = choose, progress
        self._comfy_process = None

    def extract(self, workspace, args, cancel):
        archive = workspace_path(workspace, args['path'])
        if not archive.is_file():
            raise ToolFailure('没有找到要解压的归档文件。')
        with _new_output(workspace, args['destination']) as (stage, destination):
            self.progress('正在解压归档…', -1)
            files = _extract_archive(archive, stage, cancel, max_entries=2000, max_bytes=2 * 1024 * MIB,
                                     memory=2 * 1024 * MIB, seconds=240)
        return {'destination': str(destination), 'files': files[:100], 'file_count': len(files)}

    @staticmethod
    def _request(path, *, data=None, timeout=10):
        body = json.dumps(data, allow_nan=False).encode() if data is not None else None
        req = Request(COMFY + path, data=body, headers={'Content-Type': 'application/json'})
        return open_authenticated(req, timeout=timeout, proxy='')

    def _json(self, path, **kwargs):
        with self._request(path, **kwargs) as response:
            payload = response.read(4 * MIB + 1)
        if len(payload) > 4 * MIB:
            raise ToolFailure('ComfyUI API 响应过大。')
        return json.loads(payload)

    def _online(self):
        try:
            info = self._json('/system_stats', timeout=2)
            return isinstance(info, dict) and isinstance(info.get('system'), dict) and isinstance(info.get('devices'), list)
        except (OSError, ValueError, ToolFailure):
            return False

    def _managed_comfy(self):
        for mode, directory in COMFY_DIRECTORIES.items():
            root = self.root / f'comfyui-v0.45.0-{mode}' / directory
            python = root / 'python_embeded' / 'python.exe'
            if python.is_file() and (root / 'ComfyUI' / 'main.py').is_file():
                return mode, root, python
        return None

    def _install_comfy(self, mode, cancel):
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f'comfyui-v0.45.0-{mode}'
        with tempfile.TemporaryDirectory(prefix='.install-', dir=self.root) as tmp:
            stage = Path(tmp)
            archive = _download(COMFY_RELEASE, COMFY_ASSETS[mode], stage, cancel, self.progress)
            output = stage / 'files'
            output.mkdir()
            self.progress('正在解压 ComfyUI…', -1)
            _extract_archive(archive, output, cancel, max_entries=120000, max_bytes=16 * 1024 * MIB,
                             memory=8 * 1024 * MIB, seconds=1800)
            candidate = output / COMFY_DIRECTORIES[mode]
            if not ((candidate / 'python_embeded' / 'python.exe').is_file()
                    and (candidate / 'ComfyUI' / 'main.py').is_file()):
                raise ToolFailure('未找到 ComfyUI 便携版启动文件。')
            try:
                output.rename(target)
            except OSError:
                if not (target / candidate.name / 'ComfyUI' / 'main.py').is_file():
                    raise

    def _ensure_comfy(self, cancel):
        if self._online():
            return
        managed = self._managed_comfy()
        if managed is None:
            choice = self.choose('comfyui', cancel)
            if not choice:
                raise ToolFailure('已跳过下载，工作流未提交。')
            if choice == 'existing' and self._online():
                return
            if not WINDOWS_PORTABLE or choice not in COMFY_ASSETS:
                raise ToolFailure('未连接 ComfyUI。请先启动本机 8188 端口的服务，或选择下载便携版。')
            self._install_comfy(choice, cancel)
            managed = self._managed_comfy()
        if managed is None:
            raise ToolFailure('ComfyUI 未完成安装。')
        mode, root, python = managed
        _check(cancel, time.monotonic() + 1)
        if self._comfy_process is None or self._comfy_process.poll() is not None:
            flags = ['--cpu'] if mode == 'cpu' else []
            self.progress('正在启动本机 ComfyUI…', -1)
            env = {key: val for key, val in os.environ.items()
                   if not (key.upper().startswith(('REVON_', 'KEYSTONE_', 'FORGE_', 'PYTHON'))
                           or key.upper().endswith(('_KEY', '_TOKEN', '_SECRET')))}
            self._comfy_process = subprocess.Popen(
                [str(python), '-s', 'ComfyUI/main.py', '--listen', '127.0.0.1', '--port', '8188',
                 '--windows-standalone-build', *flags], cwd=root, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic() + 120
        while not self._online():
            _check(cancel, deadline)
            if self._comfy_process.poll() is not None:
                raise ToolFailure('ComfyUI 启动失败，请检查便携版配置、驱动和端口 8188。')
            cancel.wait(1)

    def comfy(self, workspace, args, cancel):
        path = workspace_path(workspace, args['path'])
        with path.open('rb') as source:
            raw = source.read(2 * MIB + 1)
        if len(raw) > 2 * MIB:
            raise ToolFailure('工作流 JSON 超过 2 MiB。')
        graph = json.loads(raw.decode('utf-8-sig'))
        if (not isinstance(graph, dict) or not graph or not all(
                isinstance(node, dict) and isinstance(node.get('class_type'), str)
                and isinstance(node.get('inputs'), dict) for node in graph.values())):
            raise ToolFailure('请使用 ComfyUI 导出的 API 格式工作流 JSON。')
        if 'prompt' in args or 'prompt_node' in args:
            node = graph.get(args.get('prompt_node'), {})
            inputs = node.get('inputs', {})
            text = args.get('prompt')
            if not isinstance(text, str) or len(text) > 20000 or not isinstance(inputs.get('text'), str):
                raise ToolFailure('请同时给出提示词和包含文字输入的节点 ID。')
            inputs['text'] = text
        body = {'prompt': graph}
        json.dumps(body, allow_nan=False)
        prompt_id = ''
        try:
            with _new_output(workspace, args['destination']) as (stage, destination):
                self._ensure_comfy(cancel)
                _check(cancel, time.monotonic() + 1)
                prompt_id = self._queue_comfy(body)
                self.progress('正在等待 ComfyUI 完成工作流…', -1)
                outputs = self._wait_comfy(prompt_id, cancel)
                self.progress('正在保存 ComfyUI 产物…', -1)
                files = self._save_media(outputs, stage, cancel)
        except (OSError, ValueError, TypeError, ToolFailure) as exc:
            if prompt_id and not (isinstance(exc, ToolFailure) and exc.state == 'completed'):
                raise ToolFailure('ComfyUI 已接收工作流。revon 未取回结果，请在 ComfyUI 中检查该任务。',
                                  'unknown', prompt_id=prompt_id) from None
            raise
        return {'prompt_id': prompt_id, 'destination': str(destination), 'files': files, 'file_count': len(files)}

    def _queue_comfy(self, body):
        try:
            queued = self._json('/prompt', data=body)
        except HTTPError as exc:
            if 400 <= exc.code < 500:
                detail = ''
                try:
                    server = json.loads(exc.read(64 * 1024))
                    error = server.get('error')
                    messages = [str(error.get('message', ''))] if isinstance(error, dict) else []
                    for key, node in server.get('node_errors', {}).items():
                        messages.extend(f"节点 {key}: {row.get('details') or row.get('message', '')}"
                                        for row in node.get('errors', []) if isinstance(row, dict))
                    detail = ' '.join(str(redact(' '.join(filter(None, messages)))).split())[:480]
                except (OSError, ValueError, AttributeError, TypeError):
                    pass
                hint = 'ComfyUI 拒绝工作流，请检查节点和模型配置。'
                raise ToolFailure(hint + (' ' + detail if detail else '')) from None
            raise ToolFailure('ComfyUI 提交结果未知，请先检查它的队列。', 'unknown') from None
        except (OSError, ValueError, ToolFailure):
            raise ToolFailure('ComfyUI 提交结果未知，请先检查它的队列。', 'unknown') from None
        prompt_id = queued.get('prompt_id', '') if isinstance(queued, dict) else ''
        if not isinstance(prompt_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', prompt_id):
            raise ToolFailure('ComfyUI 未返回有效任务 ID，请检查队列。', 'unknown')
        return prompt_id

    def _wait_comfy(self, prompt_id, cancel):
        deadline = time.monotonic() + 900
        while True:
            _check(cancel, deadline)
            history = self._json('/history/' + prompt_id)
            job = history.get(prompt_id) if isinstance(history, dict) else None
            if isinstance(job, dict):
                status = job.get('status')
                status = status if isinstance(status, dict) else {}
                if status.get('status_str') == 'error':
                    raise ToolFailure('ComfyUI 工作流执行失败，请查看任务详情。', 'completed', prompt_id=prompt_id)
                if status.get('completed') or job.get('outputs'):
                    outputs = job.get('outputs')
                    return outputs if isinstance(outputs, dict) else {}
            cancel.wait(1)

    def _save_media(self, outputs, stage, cancel):
        files = []
        total = 0
        for node in outputs.values():
            if not isinstance(node, dict):
                continue
            for kind in ('images', 'gifs', 'video', 'audio'):
                entries = node.get(kind)
                if not isinstance(entries, list):
                    continue
                for output in entries:
                    if not isinstance(output, dict) or output.get('type') not in {'output', 'temp'}:
                        continue
                    filename = output.get('filename')
                    subfolder = output.get('subfolder', '')
                    if not isinstance(filename, str) or not isinstance(subfolder, str):
                        continue
                    _check_component(filename)
                    suffix = Path(filename).suffix.lower()
                    if '/' in filename or '\\' in filename or suffix not in MEDIA:
                        continue
                    if len(files) >= 32:
                        raise ToolFailure('ComfyUI 产物超过单次下载上限。')
                    query = urlencode({'filename': filename, 'subfolder': subfolder, 'type': output['type']})
                    name = f'{len(files) + 1:02d}-{filename}'
                    deadline = time.monotonic() + 180
                    with self._request('/view?' + query) as response, (stage / name).open('xb') as out:
                        while chunk := response.read(MIB):
                            _check(cancel, deadline)
                            total += len(chunk)
                            if total > 512 * MIB:
                                raise ToolFailure('ComfyUI 产物超过 512 MiB。')
                            out.write(chunk)
                    files.append(name)
        if not files:
            raise ToolFailure('工作流已结束，但没有可导出的媒体文件。请检查保存节点。')
        return files

    def call(self, name, workspace, args, cancel):
        try:
            if not all(isinstance(args.get(k), str) and args[k].strip() for k in ('path', 'destination')):
                raise ToolFailure('请提供工作区内的输入路径和输出目录。')
            run = {'7zip_extract': self.extract, 'comfyui_run': self.comfy}.get(name)
            if run is None:
                raise ToolFailure('未知的本地工具。')
            value = run(workspace, args, cancel)
            return {'ok': True, **value, 'meta': {'execution_state': 'completed'}}
        except ToolFailure as exc:
            return {'ok': False, 'error': str(exc), **exc.details, 'meta': {'execution_state': exc.state}}
        except ArchiveError as exc:
            return {'ok': False, 'error': str(exc), 'meta': {'execution_state': 'not_started'}}
        except (OSError, ValueError, TypeError, RecursionError, KeyError):
            return {'ok': False, 'error': '本地工具未完成，请检查文件、依赖和工作区状态。',
                    'meta': {'execution_state': 'not_started'}}
        finally:
            self.progress('', -1)

    def shutdown(self):
        proc = self._comfy_process
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(2)
            except subprocess.TimeoutExpired:
                proc.kill()
