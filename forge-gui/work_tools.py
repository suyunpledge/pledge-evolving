"""Work chat tool boundary; no UI, permission grants or automatic retries."""
import json
from forge import toolwire
from forge.secrets import redact


def prepare_calls(rows):
    """Validate the complete native batch before dispatching any call."""
    if not isinstance(rows, list):
        raise ValueError('Tool calls must be a list')
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('function'), dict):
            raise ValueError('Invalid tool call function')
    calls = toolwire.parse_tool_calls({'tool_calls': rows})
    normalized = toolwire.normalize_native_calls([c.to_raw() for c in calls], 'openai')
    return toolwire.assistant_message('', normalized, 'openai')['tool_calls']


def invoke(args, dispatch):
    """A lost/malformed receipt is unknown, never a safely retryable failure."""
    try:
        json.dumps(args, allow_nan=False)
        valid = isinstance(args, dict) and not args.get('_truncated') and '_raw' not in args
    except (TypeError, ValueError, RecursionError):
        valid = False
    if not valid:
        return {'ok': False, 'error': '参数不完整，请重新生成完整 JSON 对象。',
                'meta': {'execution_state': 'not_started', 'error_code': 'invalid_arguments'}}
    try:
        response = dispatch()
        if not isinstance(response, dict) or type(response.get('ok')) is not bool:
            raise ValueError('工具返回值缺少布尔 ok')
        response = json.loads(json.dumps(response, allow_nan=False))
        meta = response.get('meta')
        if meta is not None and not isinstance(meta, dict):
            raise ValueError('工具返回值 meta 必须是对象')
        response['meta'] = meta or {}
        response['meta'].setdefault('execution_state', 'completed' if response['ok'] else 'unknown')
        if response['meta']['execution_state'] == 'unknown':
            response['ok'] = False
        return redact(response)
    except Exception as exc:
        return {'ok': False, 'error': redact(f'{type(exc).__name__}: {exc}'),
                'meta': {'execution_state': 'unknown', 'error_code': 'tool_outcome_unknown',
                         'run_id': getattr(exc, 'run_id', ''), 'action_id': getattr(exc, 'action_id', '')}}
