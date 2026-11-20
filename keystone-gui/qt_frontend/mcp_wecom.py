"""Expose documented enterprise WeChat CLI methods through Keystone's connector API."""

import json
import re
import uuid
from urllib.parse import parse_qs, urlsplit

from keystone.secrets import redact
from .mcp_local import _run, local_command


class WeCom:
    def __init__(self, home, profile, cancel):
        if not re.fullmatch(r'[a-f0-9]{32}', profile):
            raise ValueError('企业微信本地授权配置无效。')
        _, self.program, self.root = local_command(home, 'wecom', cancel)
        self.folder = self.root / 'accounts' / profile
        self.env = {'WECOM_CLI_CONFIG_DIR': str(self.folder)}
        self.cancel = cancel
        self._available = None

    @classmethod
    def connect(cls, home, cancel, notify):
        profile = uuid.uuid4().hex
        client = cls(home, profile, cancel)
        try:
            client.folder.mkdir(parents=True, exist_ok=True)
            published = False

            def scan_url(line):
                nonlocal published
                if published or not line.startswith('https://work.weixin.qq.com/ai/qc/gen?') or len(line) > 2048:
                    return
                url = urlsplit(line)
                params = parse_qs(url.query, max_num_fields=8)
                if not url.fragment and params.get('scode'):
                    published = True
                    notify(line)

            client._command(['auth', 'init', '--noninteractive', '--no-browser'], timeout=315,
                            on_line=scan_url, failure='企业微信授权未完成。请在浏览器中扫码并确认企业机器人绑定。')
            if client._command(['auth', 'show', '--status']) != 'authorized':
                raise PermissionError('企业微信授权未完成。')
            return profile, client.tools()
        except Exception:
            try:
                cls.disconnect(home, profile)
            except OSError:
                pass
            raise

    @staticmethod
    def disconnect(home, profile):
        if re.fullmatch(r'[a-f0-9]{32}', profile):
            directory = home / 'gui' / 'qt' / 'mcp-local' / 'wecom' / 'accounts' / profile
            for name in ('credentials.enc', '.encryption_key'):
                (directory / name).unlink(missing_ok=True)

    def _command(self, args, *, timeout=35, on_line=None, failure='企业微信服务请求失败。请检查网络或重新授权。'):
        return _run(self.program, args, self.root, self.cancel, timeout,
                    env=self.env, failure=failure, on_line=on_line)

    def _read_catalog(self):
        rows = json.loads(self._command(['schema', 'list']))
        if not isinstance(rows, list):
            raise ValueError('企业微信返回的工具目录格式无效。')
        entries = {}
        for service in rows:
            if not isinstance(service, dict):
                continue
            prefix = service.get('name')
            methods = service.get('methods')
            if not isinstance(prefix, str) or not isinstance(methods, list):
                continue
            for method in methods:
                if not isinstance(method, dict) or not isinstance(method.get('name'), str):
                    continue
                name = prefix + '.' + method['name']
                if len(name) > 128 or not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_-]*(?:\.[a-zA-Z0-9_][a-zA-Z0-9_-]*)+', name):
                    continue
                if prefix in {'auth', 'cache', 'help', 'schema'}:
                    continue
                description = str(method.get('description') or '')[:780]
                entries[name] = {'name': name, 'description': description,
                                 'schema': {'type': 'object', 'additionalProperties': True}}
                if len(entries) >= 500:
                    return entries
        return entries

    def tools(self):
        if self._available is None:
            self._available = self._read_catalog()
        return redact(list(self._available.values()))

    def schema(self, name):
        self.tools()
        if name not in self._available:
            raise ValueError('企业微信未公布此工具，请重新获取工具。')
        row = json.loads(self._command(['schema', 'get', name]))
        if not isinstance(row, dict):
            raise ValueError('企业微信工具参数格式无效。')
        return redact({key: row.get(key) for key in ('request', 'schemas')})

    def invoke(self, name, arguments):
        self.tools()
        if name not in self._available:
            raise ValueError('企业微信未公布此工具，请重新获取工具。')
        body = json.dumps(arguments, ensure_ascii=False, allow_nan=False)
        if len(body) > 12000:
            raise ValueError('企业微信参数超过本机命令长度限制。')
        response = self._command([*name.split('.'), '--json=' + body])
        try:
            result = json.loads(response)
        except ValueError:
            raise ValueError('企业微信工具未返回有效的 JSON。') from None
        return redact({'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}]})

    def close(self):
        pass
