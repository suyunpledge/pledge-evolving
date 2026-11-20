"""Provider-specific text-to-image and text-to-video APIs used by Work chat."""
import base64
import json
from http.client import HTTPException
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlencode
from urllib.request import Request
import uuid

from keystone.http_transport import RequestCancelled, open_response
from keystone.secrets import redact
from keystone.web_backends import check_public
from provider_catalog import BY_KEY
from .local_tool_runtime import MIB, ToolFailure, _check, _new_output, workspace_path

OPENROUTER_BASE = BY_KEY['openrouter'].plans[0].base_url
QINIU_BASE = BY_KEY['qiniu'].plans[0].base_url
PRESETS = {
    'openai-images': ('image', 'GPT Image / OpenAI 兼容', 'https://api.openai.com/v1', 'gpt-image-1.5'),
    'seedream': ('image', '火山方舟 · Seedream', 'https://ark.cn-beijing.volces.com/api/v3',
                 'doubao-seedream-5-0-flash-260915'),
    'flux': ('image', 'Black Forest Labs · FLUX', 'https://api.bfl.ai/v1', 'flux-2-pro-preview'),
    'wan-image': ('image', '阿里云百炼 · Wan 图片', 'https://dashscope.aliyuncs.com/api/v1', 'wan2.7-image'),
    'seedance': ('video', '火山方舟 · Seedance', 'https://ark.cn-beijing.volces.com/api/v3',
                 'doubao-seedance-2-0-260128'),
    'wan-video': ('video', '阿里云百炼 · Wan 视频', 'https://dashscope.aliyuncs.com/api/v1', 'wan2.7-t2v'),
    'openrouter-image': ('image', 'OpenRouter · 图片', OPENROUTER_BASE, 'bytedance-seed/seedream-4.5'),
    'openrouter-video': ('video', 'OpenRouter · 视频', OPENROUTER_BASE, 'google/veo-3.1'),
    'qiniu-image': ('image', '七牛云 · 可灵图片', QINIU_BASE, 'kling-v2'),
    'qiniu-video': ('video', '七牛云 · 可灵视频', QINIU_BASE, 'kling-video-o1'),
}
ROUTES = {
    'openai-images': '/images/generations',
    'seedream': '/images/generations',
    'wan-image': '/services/aigc/multimodal-generation/generation',
    'seedance': '/contents/generations/tasks',
    'wan-video': '/services/aigc/video-generation/video-synthesis',
    'openrouter-image': '/images',
    'openrouter-video': '/videos',
    'qiniu-image': '/images/generations',
    'qiniu-video': '/videos',
}
LOOPBACK = {'127.0.0.1', 'localhost', '::1'}
BFL_HOSTS = {'api.bfl.ai', 'api.eu.bfl.ai', 'api.us.bfl.ai'}


def locator(value, *, query=False):
    if (not isinstance(value, str) or len(value) > 4096
            or any(ch.isspace() or ord(ch) < 32 or ch == chr(92) for ch in value)):
        raise ValueError('接口或产物地址无效。')
    url = urlsplit(value)
    port = url.port or (443 if url.scheme == 'https' else 80)
    if (url.scheme not in {'https', 'http'} or not url.hostname or url.username is not None
            or url.password is not None or url.fragment or (url.query and not query)):
        raise ValueError('请输入有效的 HTTP(S) 地址，地址内不能包含账号或密钥。')
    return url, (url.scheme, url.hostname, port)


def api_base(value, protocol, model):
    value = value.strip().rstrip('/')
    url, _ = locator(value)
    if redact(value) != value or "SECRET_REF" in value:
        raise ValueError("请将 API 密钥填在密钥字段。")
    if url.scheme != 'https' and url.hostname not in LOOPBACK:
        raise ValueError('外部模型接口需使用 HTTPS。')
    path = ('/' + model) if protocol == 'flux' else ROUTES[protocol]
    return value[:-len(path)] if value.endswith(path) else value


def tool_set():
    rows = {}
    for kind in ('image', 'video'):
        name = 'keystone_generate_' + kind
        rows[name] = ({'type': 'function', 'function': {
            'name': name,
            'description': (f'Generate a {kind} from a text prompt using the user-selected model. '
                            'If no model is configured, Keystone opens model setup and waits for the user. '
                            'Result files are written in a new workspace directory. '
                            'Report the actual files. If setup was skipped or the outcome is unknown, do not resubmit.'),
            'parameters': {'type': 'object', 'properties': {
                'prompt': {'type': 'string', 'description': 'The scene, content, and style to generate.'},
                'destination': {'type': 'string', 'description': 'Optional new folder inside the workspace.'},
            }, 'required': ['prompt'], 'additionalProperties': False},
        }}, ('media', kind))
    return rows


def prepare(kind, workspace, arguments):
    prompt = arguments.get('prompt')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 12000:
        raise ToolFailure('请输入有效的生成描述（最多 12000 字符）。')
    if redact(prompt) != prompt or 'SECRET_REF' in prompt:
        raise ToolFailure('生成描述中不能包含 Keystone 密钥。')
    default = f'generated-{kind}-{uuid.uuid4().hex[:10]}'
    destination = arguments.get('destination', default)
    path = workspace_path(workspace, destination)
    if path.exists() or not path.parent.is_dir():
        raise ToolFailure('请选择尚未创建、父目录已存在的工作区文件夹。')
    return prompt.strip(), destination


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return value if isinstance(value, list) else []


def _encoding(header, kind):
    if kind == 'video':
        return '.mp4' if header[4:8] == b'ftyp' else '.webm' if header.startswith(b'\x1a\x45\xdf\xa3') else ''
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if header.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if header.startswith(b'RIFF') and header[8:12] == b'WEBP':
        return '.webp'
    if header.startswith((b'GIF87a', b'GIF89a')):
        return '.gif'
    return ''


class MediaGeneration:
    def __init__(self, profile, key, cancel, status):
        self.protocol = profile['protocol']
        self.base = profile['url']
        self.model = profile['model']
        self.kind = PRESETS[self.protocol][0]
        self.cancel, self.status = cancel, status
        self.job_id = ''
        self.submitted = False
        self.deadline = time.monotonic() + 900
        self.headers = {'Accept': 'application/json'}
        if key:
            self.headers.update({'x-key': key} if self.protocol == 'flux' else {'Authorization': 'Bearer ' + key})

    def _json(self, url, body=None, *, headers=None):
        _check(self.cancel, self.deadline)
        values = {**self.headers, **(headers or {})}
        if body is not None:
            values['Content-Type'] = 'application/json'
        req = Request(url, data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                      headers=values)
        try:
            limit = (48 if body is not None else 2) * MIB
            data = bytearray()
            with open_response(req, timeout=140 if body is not None else 20, cancel_event=self.cancel) as response:
                while chunk := response.read(min(MIB, limit - len(data) + 1)):
                    _check(self.cancel, self.deadline)
                    data.extend(chunk)
                    if len(data) > limit:
                        raise ValueError('Response too large')
            obj = json.loads(data)
            if not isinstance(obj, dict):
                raise ValueError('Expected an object')
        except HTTPError as exc:
            if body is not None and exc.code in {400, 401, 403, 404, 413, 422, 429}:
                self.submitted = False
                try:
                    detail = json.loads(exc.read(8192))
                    error = detail.get('error') or detail
                    message = error.get('message', '') if isinstance(error, dict) else ''
                    message = str(redact(str(message)))[:600]
                except (OSError, ValueError, HTTPException):
                    message = ''
                finally:
                    exc.close()
                raise ToolFailure(f'厂商拒绝请求（HTTP {exc.code}）。' + (message or '请检查模型、密钥或配额。')) from None
            raise
        return obj

    def _submit(self, prompt):
        _check(self.cancel, self.deadline)
        p = self.protocol
        route = '/' + self.model if p == 'flux' else ROUTES[p]
        body = {'model': self.model, 'prompt': prompt}
        headers = None
        if p == 'openai-images':
            body['n'] = 1
        elif p == 'seedream':
            body.update(size='2K', response_format='b64_json')
            if self.model.startswith(('doubao-seedream-4-', 'doubao-seedream-5-0-lite-', 'doubao-seedream-5-0-260')):
                body['sequential_image_generation'] = 'disabled'
        elif p == 'seedance':
            body = {'model': self.model, 'content': [{'type': 'text', 'text': prompt}]}
        elif p == 'wan-video':
            body = {'model': self.model, 'input': {'prompt': prompt}}
            headers = {'X-DashScope-Async': 'enable'}
        elif p == 'wan-image':
            body = {'model': self.model, 'input': {'messages': [
                {'role': 'user', 'content': [{'text': prompt}]},
            ]}}
        elif p == 'flux':
            body = {'prompt': prompt, 'width': 1024, 'height': 1024}
        elif p == 'qiniu-video' and self.model == 'kling-video-o1':
            body['mode'] = 'pro'
        self.submitted = True
        return self._json(self.base + route, body, headers=headers)

    def _wait(self, reply):
        p = self.protocol
        if p == 'wan-video':
            raw_id = _dict(reply.get('output')).get('task_id')
        else:
            raw_id = reply.get('task_id' if p == 'qiniu-image' else 'id')
        if not isinstance(raw_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,160}', raw_id):
            raise ValueError('No job ID')
        self.job_id = raw_id
        if p in {'seedance', 'openrouter-video', 'qiniu-video'}:
            url = self.base + ROUTES[p] + '/' + raw_id
        elif p == 'wan-video':
            url = self.base + '/tasks/' + raw_id
        elif p == 'qiniu-image':
            url = self.base + '/images/tasks/' + raw_id
        else:
            polling = reply.get('polling_url') or (self.base + '/get_result?' + urlencode({'id': raw_id}))
            if not isinstance(polling, str):
                raise ValueError('Unknown polling URL')
            url = urljoin(self.base + '/', polling)
            _, origin = locator(url, query=True)
            _, source = locator(self.base)
            if origin != source and not (origin[0] == source[0] == 'https' and origin[2] == source[2] == 443
                                         and origin[1] in BFL_HOSTS and source[1] in BFL_HOSTS):
                raise ValueError('Polling domain mismatch')
        self.status('正在等待厂商完成生成…')
        while True:
            result = self._json(url)
            row = _dict(result.get('output')) if p == 'wan-video' else result
            state = str(row.get('task_status') or row.get('status') or '').lower()
            if state in {'ready', 'succeeded', 'succeed', 'completed'}:
                if p == 'seedance':
                    return [{'url': _dict(row.get('content')).get('video_url')}]
                if p == 'wan-video':
                    return [{'url': row.get('video_url')}]
                if p == 'qiniu-image':
                    return _list(row.get('data'))
                if p == 'qiniu-video':
                    return _list(_dict(row.get('task_result')).get('videos'))
                if p == 'openrouter-video':
                    count = len(_list(row.get('unsigned_urls'))) or 1
                    if count > 4:
                        raise ValueError('Too many generated videos')
                    return [{'openrouter_index': index} for index in range(count)]
                return [{'url': _dict(row.get('result')).get('sample')}]
            if state in {'failed', 'error', 'canceled', 'cancelled', 'expired', 'moderated',
                         'request moderated', 'content moderated', 'task not found', 'unknown'}:
                raise ToolFailure('厂商未能完成生成，请查看平台任务记录。', 'completed', job_id=raw_id)
            if state not in {'pending', 'running', 'queued', 'submitted', 'processing',
                             'inprogress', 'in progress', 'in_progress'}:
                raise ValueError('Unknown status')
            self.cancel.wait(2 if p == 'flux' else 8)

    def _assets(self, reply):
        if self.protocol in {'seedance', 'wan-video', 'flux', 'openrouter-video', 'qiniu-image', 'qiniu-video'}:
            return self._wait(reply)
        if self.protocol == 'wan-image':
            return [{'url': row.get('image')} for choice in _list(_dict(reply.get('output')).get('choices'))
                    for row in _list(_dict(_dict(choice).get('message')).get('content'))
                    if isinstance(row, dict) and row.get('image')]
        return _list(reply.get('data'))

    def _save(self, asset, stage, index):
        encoded = asset.get('b64_json')
        max_bytes = (300 if self.kind == 'video' else 24) * MIB
        if isinstance(encoded, str) and self.kind == 'image':
            if len(encoded) > (max_bytes * 4 // 3) + 4:
                raise ValueError('Too much image data')
            data = base64.b64decode(encoded, validate=True)
            extension = _encoding(data[:32], self.kind)
            if not extension or len(data) > max_bytes:
                raise ValueError('Invalid image data')
            path = stage / f'{self.kind}-{index}{extension}'
            path.write_bytes(data)
            return path.name
        if self.protocol == 'openrouter-video':
            # OpenRouter proxies video bytes at its authenticated content endpoint.
            url = f'{self.base}/videos/{self.job_id}/content?index={asset["openrouter_index"]}'
            request = Request(url, headers={**self.headers, 'Accept': 'video/*'})
        else:
            url = asset.get('url')
            _, origin = locator(url, query=True)
            _, source = locator(self.base)
            if origin != source or origin[1] not in LOOPBACK:
                if origin[0] != 'https':
                    raise ValueError('CDN results must use HTTPS')
                check_public(url)
            # Signed asset URLs are fetched without the provider key.
            request = Request(url)
        _check(self.cancel, self.deadline)
        with open_response(request, timeout=30, cancel_event=self.cancel) as stream:
            expected = stream.headers.get('Content-Length')
            expected = int(expected) if expected is not None else None
            if expected is not None and not 0 < expected <= max_bytes:
                raise ValueError('Invalid media size')
            initial = stream.read(32)
            extension = _encoding(initial, self.kind)
            if not extension:
                raise ValueError('Unexpected media format')
            path = stage / f'{self.kind}-{index}{extension}'
            total = len(initial)
            with path.open('xb') as out:
                out.write(initial)
                while chunk := stream.read(MIB):
                    _check(self.cancel, self.deadline)
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError('Media size limit')
                    out.write(chunk)
            if expected is not None and total != expected:
                raise ValueError('Incomplete media download')
        return path.name

    def run(self, workspace, prompt, destination):
        try:
            with _new_output(workspace, destination) as (stage, folder):
                self.status('正在向生成模型提交…')
                reply = self._submit(prompt)
                assets = self._assets(reply)
                if not 1 <= len(assets) <= 4:
                    raise ValueError('Missing or excessive media')
                self.status('正在保存生成结果…')
                files = [self._save(_dict(asset), stage, idx) for idx, asset in enumerate(assets, 1)]
                _check(self.cancel, self.deadline)
        except (ToolFailure, OSError, ValueError, TypeError, HTTPException, RequestCancelled) as exc:
            if isinstance(exc, ToolFailure) and (exc.state == 'completed' or not self.submitted):
                raise
            if self.submitted:
                raise ToolFailure('生成请求可能已提交，请检查厂商任务记录后再决定是否重新生成。',
                                  'unknown', **({'job_id': self.job_id} if self.job_id else {})) from None
            raise ToolFailure('未提交生成请求，请检查接口配置。') from None
        return {'ok': True, 'kind': self.kind, 'model': self.model,
                'files': [str(Path(folder) / name) for name in files],
                'meta': {'execution_state': 'completed'}}
