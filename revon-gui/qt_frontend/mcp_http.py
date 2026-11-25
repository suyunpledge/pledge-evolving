"""User-configured Streamable HTTP MCP endpoints for the desktop connector page."""
from contextlib import contextmanager
import json
import re
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

from revon.http_transport import open_response
from revon.secrets import redact

MODERN = '2026-07-28'
LEGACY = ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05')
MAX_BODY = 2 * 1024 * 1024


def endpoint_url(value):
    url = value.strip()
    try:
        parsed = urlsplit(url)
        loopback = parsed.hostname in {'localhost', '127.0.0.1', '::1'}
        allowed = parsed.scheme == 'https' or parsed.scheme == 'http' and loopback
        if (not allowed or not parsed.hostname or parsed.username is not None or parsed.fragment
                or parsed.query or re.search(r'[\s\\]', url) or len(url) > 2048 or redact(url) != url):
            raise ValueError
        parsed.port  # Validate numeric range before handing the URL to urllib.
    except ValueError:
        raise ValueError('请输入 HTTPS MCP 地址，或使用本机 HTTP 地址。凭据请填入令牌栏。') from None
    return url


class RpcError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f'MCP 请求失败（{code}）')


class McpHttp:
    def __init__(self, url, token, cancel, *, plain=False, credential_header='Authorization',
                 credential_prefix='Bearer ', allowed_tools=None, query_auth=None, fixed_headers=None):
        self.url = endpoint_url(url)
        self.cancel = cancel
        self.version = MODERN
        self.session = None
        self.counter = 0
        self.plain = plain
        self.allowed_tools = allowed_tools
        self.headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
        self.headers.update(fixed_headers or {})
        if token:
            if any(c in token for c in '\r\n'):
                raise ValueError('令牌必须为单行文本。')
            if query_auth:
                if urlsplit(self.url).scheme != 'https':
                    raise ValueError('查询参数鉴权需要 HTTPS。')
                field, extras = query_auth
                self.url = urlsplit(self.url)._replace(query=urlencode({**extras, field: token})).geturl()
            else:
                self.headers[credential_header] = credential_prefix + token

    @contextmanager
    def _response(self, request):
        deadline = threading.Event()
        timer = threading.Timer(10, deadline.set)
        timer.daemon = True

        class Cancel:
            def is_set(inner):
                return self.cancel.is_set() or deadline.is_set()

        timer.start()
        try:
            with open_response(request, timeout=10, cancel_event=Cancel()) as response:
                yield response
        finally:
            timer.cancel()

    def _post(self, method, params=None, *, notification=False):
        self.counter += 1
        identifier = self.counter
        params = dict(params or {})
        headers = dict(self.headers)
        if self.version == MODERN and not self.plain:
            headers.update({'MCP-Protocol-Version': MODERN, 'Mcp-Method': method})
            if method == 'tools/call':
                headers['Mcp-Name'] = params['name']
            params['_meta'] = {'io.modelcontextprotocol/protocolVersion': MODERN,
                               'io.modelcontextprotocol/clientInfo': {'name': 'revon', 'version': '1.0'},
                               'io.modelcontextprotocol/clientCapabilities': {}}
        elif self.version != MODERN and method != 'initialize':
            headers['MCP-Protocol-Version'] = self.version
            if self.session:
                headers['Mcp-Session-Id'] = self.session
        body = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notification:
            body['id'] = identifier
        request = Request(self.url, json.dumps(body).encode('utf-8'), headers, method='POST')
        with self._response(request) as response:
            if notification:
                return {}
            if method == 'initialize':
                session = response.headers.get('Mcp-Session-Id')
                if session and (len(session) > 512 or not re.fullmatch(r'[\x21-\x7e]+', session)):
                    raise ValueError('MCP 返回了无效的会话标识。')
                self.session = session
            if response.headers.get_content_type() == 'text/event-stream':
                reply = self._read_sse(response, identifier)
            else:
                raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise ValueError('MCP 响应超过 2 MiB。')
                reply = json.loads(raw)
        if not isinstance(reply, dict) or reply.get('jsonrpc') != '2.0' or reply.get('id') != identifier:
            raise ValueError('MCP 返回的响应与请求不匹配。')
        if isinstance(reply.get('error'), dict):
            code = reply['error'].get('code')
            raise RpcError(code if isinstance(code, int) else '未知错误')
        result = reply.get('result')
        if not isinstance(result, dict):
            raise ValueError('MCP 返回了无效的结果。')
        return result

    @staticmethod
    def _read_sse(response, identifier):
        data, remaining = [], MAX_BODY
        while remaining > 0:
            line = response.readline(remaining + 1)
            remaining -= len(line)
            if not line or remaining < 0:
                break
            if line.startswith(b'data:'):
                data.append(line[5:].strip())
            elif data and not line.strip():
                reply = json.loads(b'\n'.join(data))
                if isinstance(reply, dict) and reply.get('id') == identifier:
                    return reply
                data.clear()
        raise ValueError('MCP 流未在大小限制内返回完整响应。')

    def _initialize(self):
        self.version = LEGACY[0]
        result = self._post('initialize', {'protocolVersion': self.version, 'capabilities': {},
                                          'clientInfo': {'name': 'revon', 'version': '1.0'}})
        version = result.get('protocolVersion')
        if version not in LEGACY:
            raise ValueError('此 MCP 协议版本暂不支持。')
        self.version = version
        self._post('notifications/initialized', notification=True)

    def tools(self):
        tools, cursors = {}, set()
        result = self._discover()
        for _ in range(12):
            rows = result.get('tools')
            if not isinstance(rows, list):
                raise ValueError('MCP 工具清单格式无效。')
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = row.get('name')
                if (isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', name)
                        and (self.allowed_tools is None or name in self.allowed_tools)):
                    schema = row.get('inputSchema')
                    tools[name] = {'name': name, 'description': str(row.get('description') or '')[:1000],
                                   'schema': schema if isinstance(schema, dict) else {}}
            cursor = result.get('nextCursor')
            if not cursor or len(tools) >= 500:
                return redact(list(tools.values())[:500])
            if not isinstance(cursor, str) or cursor in cursors or len(cursor) > 2048:
                raise ValueError('MCP 工具清单分页无效。')
            cursors.add(cursor)
            result = self._post('tools/list', {'cursor': cursor})
        return redact(list(tools.values())[:500])

    def _discover(self):
        try:
            first = self._post('tools/list')
        except RpcError as exc:
            if exc.code not in {-32022, -32601, -32600}:
                raise
            self._initialize()
            first = self._post('tools/list')
        except HTTPError as exc:
            if exc.code not in {400, 404, 405, 406, 415}:
                raise
            self._initialize()
            first = self._post('tools/list')
        return first

    def invoke(self, name, arguments):
        if name not in {tool['name'] for tool in self.tools()}:
            raise ValueError('服务器当前没有发布该工具，请刷新工具清单。')
        result = self._post('tools/call', {'name': name, 'arguments': arguments})
        if result.get('resultType') == 'input_required':
            raise RuntimeError('此工具要求额外输入，当前连接器暂不支持此交互。')
        return redact(result)

    def close(self):
        if self.session and not self.cancel.is_set():
            headers = {**self.headers, 'MCP-Protocol-Version': self.version, 'Mcp-Session-Id': self.session}
            try:
                with open_response(Request(self.url, headers=headers, method='DELETE'), timeout=2,
                                   cancel_event=self.cancel):
                    pass
            except Exception:
                pass  # Some servers do not support explicit session termination.
