"""Small trusted HTTP boundary: endpoint auth, no redirects, stream redaction."""
from __future__ import annotations
import copy
import json
import time
import urllib.request
from .secrets import StreamingRedactor, redact


class NoCredentialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise PermissionError('Authenticated HTTP redirects are disabled')


def open_authenticated(request, *, timeout, proxy=None):
    # Never inherit a global urllib opener with HTTP wire debugging enabled.
    handlers = [NoCredentialRedirect(), urllib.request.HTTPHandler(debuglevel=0),
                urllib.request.HTTPSHandler(debuglevel=0)]
    if proxy is not None:
        handlers.append(urllib.request.ProxyHandler({'http': proxy, 'https': proxy} if proxy else {}))
    return urllib.request.build_opener(*handlers).open(request, timeout=timeout)


def iter_model_sse(response, *, expected_choices=1):
    """Drain trailing usage, but do not turn a completed answer into a timeout.

An unfinished text/reasoning/tool stream still raises. The same POST is never
replayed and partial tool arguments are never promoted to a complete response.
"""
    from .request_timing import is_timeout, STREAM_FINISH_GRACE
    finished = set()
    expected = expected_choices if type(expected_choices) is int and expected_choices > 0 else 1
    completed = False
    finish_deadline = None
    while True:
        if finish_deadline is not None:
            remaining = finish_deadline - time.monotonic()
            if remaining <= 0:
                return
            # stdlib HTTPResponse exposes its buffered SocketIO here. Only
            # shorten this completed response's read; no global socket setting.
            sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            if sock is not None:
                previous = sock.gettimeout()
                sock.settimeout(min(previous, remaining) if previous is not None else remaining)
        try:
            line = response.readline(1024 * 1024 + 1)
        except (OSError, urllib.error.URLError) as exc:
            if completed and is_timeout(exc):
                return
            raise
        if not line:
            return
        if len(line) > 1024 * 1024:
            raise ValueError('Upstream SSE line exceeds 1 MiB')
        terminal = False
        if line.startswith(b'data:'):
            raw = line[5:].strip()
            terminal = raw == b'[DONE]'
            if not terminal:
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    event = None
                if isinstance(event, dict):
                    terminal = event.get('type') == 'message_stop'
                    choices = event.get('choices')
                    for choice in choices if isinstance(choices, list) else []:
                        if not isinstance(choice, dict):
                            continue
                        index = choice.get('index', 0)
                        if (type(index) is int and 0 <= index < expected
                                and isinstance(choice.get('finish_reason'), str) and choice['finish_reason']):
                            finished.add(index)
                    completed = completed or len(finished) == expected
                    if completed and finish_deadline is None:
                        finish_deadline = time.monotonic() + STREAM_FINISH_GRACE
        yield line
        if terminal:
            return


def redact_response(data):
    """Keep JSON/JSONL framing intact while masking scalar credential values."""
    text = data.decode('utf-8', 'replace') if isinstance(data, bytes) else str(data)
    try:
        return json.dumps(redact(json.loads(text)), ensure_ascii=False).encode('utf-8')
    except ValueError:
        lines = text.splitlines()
        try:
            return ('\n'.join(json.dumps(redact(json.loads(line)), ensure_ascii=False)
                              for line in lines if line.strip()) + '\n').encode('utf-8')
        except ValueError:
            return redact(text).encode('utf-8')


class SecretSSEFilter:
    """Redact logical content across SSE frames, not just consecutive bytes."""
    def __init__(self):
        self._channels = {}

    def _text(self, event, path, channel):
        node = event
        for key in path[:-1]:
            node = node[key]
        raw = node.get(path[-1])
        if not isinstance(raw, str):
            return
        # Templates contain only protocol metadata and an empty content slot.
        key = channel
        previous = self._channels.get(key)
        stream = previous[0] if previous else StreamingRedactor()
        node[path[-1]] = stream.feed(raw)
        template = copy.deepcopy(event)
        for choice in template.get('choices', []):
            choice['delta'] = {}
            choice['finish_reason'] = None
        if path[0] == 'choices':
            delta = template['choices'][path[1]]['delta']
            if path[3] == 'tool_calls':
                tool = event['choices'][path[1]]['delta']['tool_calls'][path[4]]
                delta['tool_calls'] = [{'index': tool.get('index', path[4]), 'function': {'arguments': ''}}]
                path = (*path[:4], 0, *path[5:])
            else:
                delta[path[-1]] = ''
        elif path[0] == 'delta':
            template['delta'] = {'type': (event.get('delta') or {}).get('type', 'text_delta'), path[-1]: ''}
        target = template
        for part in path[:-1]: target = target[part]
        target[path[-1]] = ''
        self._channels[key] = (stream, template, path)

    def finish(self, channels=None):
        out = []
        for key in list(self._channels):
            if channels is not None and key not in channels:
                continue
            stream, template, path = self._channels.pop(key)
            tail = stream.feed('', final=True)
            if not tail:
                continue
            event = copy.deepcopy(template)
            node = event
            for key in path[:-1]: node = node[key]
            node[path[-1]] = tail
            # A synthetic content flush must not duplicate a finish reason.
            for choice in event.get('choices', []): choice['finish_reason'] = None
            event.pop('usage', None)
            out.append('data: ' + json.dumps(redact(event), ensure_ascii=False) + '\n\n')
        return ''.join(out).encode('utf-8')

    def feed(self, line: bytes) -> bytes:
        if len(line) > 1024 * 1024:
            raise ValueError('Upstream SSE line exceeds 1 MiB')
        text = line.decode('utf-8', 'replace')
        if not text.startswith('data:'):
            return redact(text).encode('utf-8')
        raw = text[5:].strip()
        if raw == '[DONE]':
            return self.finish() + b'data: [DONE]\n\n'
        try:
            event = json.loads(raw)
        except ValueError:
            # Malformed frames do not get an unchecked output bypass.
            return b''
        if not isinstance(event, dict): return b''
        if not isinstance(event.get('choices', []), list): return b''
        if any(not isinstance(c, dict) for c in event.get('choices', [])): return b''
        terminal = event.get('type') in {'message_stop', 'content_block_stop', 'error'}
        finished_choices = {c.get('index', i) for i, c in enumerate(event.get('choices', []))
                            if c.get('finish_reason')}
        for index, choice in enumerate(event.get('choices', [])):
            delta = choice.get('delta') or {}
            for field in ('content', 'reasoning_content'):
                if isinstance(delta.get(field), str):
                    self._text(event, ('choices', index, 'delta', field), ('choice', choice.get('index', index), field))
            for ti, tool in enumerate(delta.get('tool_calls') or []):
                if isinstance((tool.get('function') or {}).get('arguments'), str):
                    self._text(event, ('choices', index, 'delta', 'tool_calls', ti, 'function', 'arguments'),
                               ('tool', choice.get('index', index), tool.get('index', ti)))
        delta = event.get('delta') or {}
        for field in ('text', 'thinking', 'partial_json'):
            if isinstance(delta.get(field), str): self._text(event, ('delta', field), ('block', event.get('index', 0), field))
        if finished_choices:
            # Current delta precedes its buffered suffix; the finish marker
            # must follow both. Usage stays on the terminal event exactly once.
            ending = copy.deepcopy(event)
            for choice in ending.get('choices', []):
                choice['delta'] = {}
            current = copy.deepcopy(event)
            for choice in current.get('choices', []):
                choice['finish_reason'] = None
            current.pop('usage', None)
            channels = {key for key in self._channels
                        if key[0] in {'choice', 'tool'} and key[1] in finished_choices}
            prefix = ('data: ' + json.dumps(redact(current), ensure_ascii=False) + '\n\n').encode('utf-8')
            suffix = self.finish(channels)
            end = ('data: ' + json.dumps(redact(ending), ensure_ascii=False) + '\n\n').encode('utf-8')
            return prefix + suffix + end
        flushed = self.finish() if terminal else b''
        return flushed + ('data: ' + json.dumps(redact(event), ensure_ascii=False) + '\n\n').encode('utf-8')
