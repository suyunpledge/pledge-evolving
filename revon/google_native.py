"""Translate revon's chat messages to Gemini generateContent and back."""
import json
import re
import uuid
from urllib.parse import urlsplit


def api_base(address):
    address = address.rstrip('/')
    path = urlsplit(address).path
    if path.endswith('/openai'):
        raise ValueError('Google 原生协议需要 Gemini API 地址，请去掉末尾的 /openai。')
    if '/models/' in path or path.endswith('/models'):
        raise ValueError('请填写 Gemini API 的 Base URL，并在模型字段中单独填写模型 ID。')
    if not re.search(r'/v\d+(?:beta\d*|alpha\d*)?$', path):
        address += '/v1beta'
    return address


def endpoint(address, model, stream=False):
    name = model.removeprefix('models/')
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]*', name):
        raise ValueError('请输入有效的 Gemini 模型 ID。')
    verb = 'streamGenerateContent?alt=sse' if stream else 'generateContent'
    return f'{api_base(address)}/models/{name}:{verb}'


def _content(value):
    if isinstance(value, str):
        return [{'text': value}] if value else []
    parts = []
    for block in value or []:
        if block.get('type') == 'text':
            parts.append({'text': block['text']})
        elif block.get('type') == 'image_url':
            match = re.fullmatch(r'data:(image/[\w.+-]+);base64,([A-Za-z0-9+/=]+)', block['image_url']['url'])
            if not match:
                raise ValueError('Google 原生协议的图片输入须为内联图片。')
            parts.append({'inlineData': {'mimeType': match[1], 'data': match[2]}})
        else:
            raise ValueError('Google 原生协议无法处理此消息内容类型。')
    return parts


def to_request(payload):
    system, turns, calls = [], [], {}
    for msg in payload.get('messages', []):
        role = msg['role']
        if role in ('system', 'developer'):
            system.extend(_content(msg.get('content')))
            continue
        if role == 'tool':
            name, native_id = calls[msg['tool_call_id']]
            result = {'name': name, 'response': {'result': msg.get('content', '')}}
            if native_id:
                result['id'] = native_id
            parts = [{'functionResponse': result}]
        else:
            parts = _content(msg.get('content'))
            for item in msg.get('tool_calls') or []:
                fn = item['function']
                meta = (item.get('extra_content') or {}).get('google') or {}
                call = {'name': fn['name'], 'args': json.loads(fn.get('arguments') or '{}')}
                if meta.get('call_id'):
                    call['id'] = meta['call_id']
                part = {'functionCall': call}
                if meta.get('thought_signature'):
                    part['thoughtSignature'] = meta['thought_signature']
                parts.append(part)
                calls[item['id']] = fn['name'], call.get('id')
        wire_role = 'model' if role == 'assistant' else 'user'
        if parts:
            if turns and turns[-1]['role'] == wire_role:
                turns[-1]['parts'].extend(parts)
            else:
                turns.append({'role': wire_role, 'parts': parts})
    result = {'contents': turns}
    if system:
        result['systemInstruction'] = {'parts': system}
    declarations = []
    for tool in payload.get('tools') or []:
        fn = tool['function']
        declaration = {'name': fn['name'], 'description': fn.get('description', '')}
        if fn.get('parameters'):
            declaration['parametersJsonSchema'] = fn['parameters']
        declarations.append(declaration)
    if declarations:
        result['tools'] = [{'functionDeclarations': declarations}]
        choice = payload.get('tool_choice', 'auto')
        if isinstance(choice, dict):
            setting = {'mode': 'ANY', 'allowedFunctionNames': [choice['function']['name']]}
        else:
            setting = {'mode': {'auto': 'AUTO', 'none': 'NONE', 'required': 'ANY'}[choice]}
        result['toolConfig'] = {'functionCallingConfig': setting}
    options = {dst: payload[src] for src, dst in (
        ('temperature', 'temperature'), ('max_tokens', 'maxOutputTokens'), ('top_p', 'topP'),
        ('max_completion_tokens', 'maxOutputTokens')) if src in payload}
    fmt = payload.get('response_format') or {}
    if fmt.get('type') in ('json_object', 'json_schema'):
        options['responseMimeType'] = 'application/json'
        if fmt.get('type') == 'json_schema':
            options['responseJsonSchema'] = fmt['json_schema']['schema']
    if options:
        result['generationConfig'] = options
    return result


def _choice(raw):
    if raw.get('error'):
        raise ValueError('Gemini returned an error: ' + str(raw['error'].get('message', 'unknown error')))
    candidate = next(iter(raw.get('candidates') or []), {})
    parts = (candidate.get('content') or {}).get('parts', [])
    message = {'role': 'assistant', 'content': ''.join(p.get('text', '') for p in parts if not p.get('thought'))}
    calls = []
    for part in parts:
        fn = part.get('functionCall')
        if fn:
            meta = {}
            if fn.get('id'):
                meta['call_id'] = fn['id']
            if part.get('thoughtSignature'):
                meta['thought_signature'] = part['thoughtSignature']
            call = {'id': fn.get('id') or 'call_' + uuid.uuid4().hex, 'type': 'function',
                    'function': {'name': fn['name'], 'arguments': json.dumps(fn.get('args', {}), ensure_ascii=False)}}
            if meta:
                call['extra_content'] = {'google': meta}
            calls.append(call)
    if calls:
        message['tool_calls'] = calls
    reason = candidate.get('finishReason') or (raw.get('promptFeedback') or {}).get('blockReason')
    finish = ('stop' if reason == 'STOP' else 'length' if reason == 'MAX_TOKENS' else 'content_filter' if reason else None)
    if finish == 'stop' and calls:
        finish = 'tool_calls'
    return message, finish


def _usage(raw):
    data = raw.get('usageMetadata') or {}
    result = {'prompt_tokens': data.get('promptTokenCount', 0),
              'completion_tokens': data.get('candidatesTokenCount', 0) + data.get('thoughtsTokenCount', 0),
              'total_tokens': data.get('totalTokenCount', 0)}
    if 'cachedContentTokenCount' in data:
        result['prompt_tokens_details'] = {'cached_tokens': data['cachedContentTokenCount']}
    if 'thoughtsTokenCount' in data:
        result['completion_tokens_details'] = {'reasoning_tokens': data['thoughtsTokenCount']}
    return result


def to_chat(raw, model):
    message, finish = _choice(raw)
    if not finish:
        raise ValueError('Incomplete Gemini response')
    if finish not in ('stop', 'tool_calls'):
        message.pop('tool_calls', None)
    return {'model': model, 'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
            'usage': _usage(raw)}


def _events(response):
    lines = []
    for line in response:
        line = line.rstrip(b'\r\n')
        if line.startswith(b'data:'):
            lines.append(line[5:].lstrip(b' '))
        elif not line and lines:
            yield json.loads(b'\n'.join(lines))
            lines.clear()
    if lines:
        raise OSError('Incomplete Gemini SSE event')


def stream_chat(response, model):
    pending_calls, finished = [], False
    for raw in _events(response):
        delta, reason = _choice(raw)
        pending_calls.extend(delta.pop('tool_calls', []))
        if reason in ('stop', 'tool_calls') and pending_calls:
            delta['tool_calls'] = [dict(call, index=index) for index, call in enumerate(pending_calls)]
            reason = 'tool_calls'
        if reason:
            pending_calls.clear()
        finished |= reason is not None
        chunk = {'model': model, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}]}
        if 'usageMetadata' in raw:
            chunk['usage'] = _usage(raw)
        yield b'data: ' + json.dumps(chunk, ensure_ascii=False).encode() + b'\n\n'
    if not finished:
        raise OSError('Gemini stream ended before finishReason')
    yield b'data: [DONE]\n\n'
