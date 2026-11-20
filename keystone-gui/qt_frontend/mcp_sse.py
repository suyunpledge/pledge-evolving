"""Legacy HTTP + SSE transport for the official Baidu Netdisk MCP."""

import json
import re
import threading
from urllib.parse import urljoin, urlsplit
from urllib.request import Request

from keystone.http_transport import open_response
from .mcp_http import MAX_BODY, McpHttp, RpcError, endpoint_url


class McpSse(McpHttp):
    def __init__(self, url, token, cancel):
        super().__init__(url, token, cancel, query_auth=('access_token', {}))
        self._stream = None
        self._connection = None
        self._timer = None

    def _events(self):
        event, data, remaining = '', [], MAX_BODY
        while remaining > 0:
            line = self._stream.readline(remaining + 1)
            remaining -= len(line)
            if not line or remaining < 0:
                break
            key, value = line.rstrip(b'\r\n').partition(b':')[::2]
            if key == b'event':
                event = value.lstrip().decode('ascii', 'replace')
            elif key == b'data':
                data.append(value[1:] if value.startswith(b' ') else value)
            elif not line.strip():
                if data:
                    yield event or 'message', b'\n'.join(data)
                event, data = '', []
        raise RuntimeError('百度网盘 MCP 事件流已断开或返回超限。')

    def _post_address(self, target):
        source = urlsplit(self.url)
        if not target or len(target) > 2048 or re.search(r'[\s\\]', target):
            raise ValueError('百度网盘 MCP 返回了无效的会话地址。')
        address = urlsplit(urljoin(self.url, target))
        if ((address.scheme, address.netloc) != (source.scheme, source.netloc)
                or address.fragment or not address.path):
            raise ValueError('百度网盘 MCP 返回了不同来源的会话地址。')
        endpoint_url(address._replace(query='').geturl())
        return address.geturl()

    def _discover(self):
        if self._stream is None:
            request = Request(self.url, headers={'Accept': 'text/event-stream'})
            deadline = threading.Event()
            upstream = self.cancel

            class Cancel:
                def is_set(inner):
                    return upstream.is_set() or deadline.is_set()

            timer = threading.Timer(75, deadline.set)
            timer.daemon = True
            timer.start()
            self._timer = timer
            context = open_response(request, timeout=20, cancel_event=Cancel())
            self._stream = context.__enter__()
            self._connection = context
            if self._stream.headers.get_content_type() != 'text/event-stream':
                raise ValueError('百度网盘未返回 MCP 事件流。')
            for event, data in self._events():
                if event == 'endpoint':
                    self._message_url = self._post_address(data.decode().strip())
                    break
            self._initialize()
        return self._post('tools/list')

    def _post(self, method, params=None, *, notification=False):
        self.counter += 1
        identifier = self.counter
        body = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            body['params'] = params
        if not notification:
            body['id'] = identifier
        request = Request(self._message_url, json.dumps(body).encode(), method='POST',
                          headers={'Content-Type': 'application/json'})
        with self._response(request):
            pass
        if notification:
            return {}
        for event, data in self._events():
            if event != 'message':
                continue
            reply = json.loads(data)
            if not isinstance(reply, dict) or reply.get('id') != identifier:
                continue
            if reply.get('jsonrpc') != '2.0':
                raise ValueError('百度网盘 MCP 返回了无效的 JSON-RPC 消息。')
            if isinstance(reply.get('error'), dict):
                code = reply['error'].get('code')
                raise RpcError(code if isinstance(code, int) else '未知错误')
            if isinstance(reply.get('result'), dict):
                return reply['result']
            raise ValueError('百度网盘 MCP 返回了无效的结果。')

    def close(self):
        if self._timer is not None:
            self._timer.cancel()
        if self._connection is not None:
            connection, self._connection = self._connection, None
            self._stream = None
            try:
                connection.__exit__(None, None, None)
            except Exception:
                pass
