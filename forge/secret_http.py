"""Small trusted HTTP boundary: endpoint auth, no redirects, stream redaction."""
from __future__ import annotations
import copy
import json
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

    def finish(self):
        out = []
        for stream, template, path in self._channels.values():
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
        self._channels.clear()
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
        terminal = terminal or any(c.get('finish_reason') for c in event.get('choices', []))
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
        flushed = self.finish() if terminal else b''
        return flushed + ('data: ' + json.dumps(redact(event), ensure_ascii=False) + '\n\n').encode('utf-8')
