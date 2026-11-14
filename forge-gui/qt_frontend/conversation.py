"""Bounded gateway tool loop with desktop approvals and no action replay."""
import copy
import json

from forge.execution_guard import ExecutionGuard
from forge_client import ChatMessage, GatewayError, GenerationCancelled
from work_tools import prepare_calls, invoke


def run(client, messages, *, model, effort, cancel, authorize, chunk, checkpoint, status, next_round):
    tools = None
    if authorize is not None:
        try:
            tools = [row for row in client.list_tools(timeout=5)
                     if isinstance(row, dict) and isinstance(row.get('function'), dict)]
        except GatewayError:
            status('网关未开放工具，正在生成对话…')
    guard = ExecutionGuard()
    for turn in range(8):
        if cancel.is_set():
            raise GenerationCancelled('已停止生成')
        result = client.stream_chat(messages, model=model, reasoning_effort=effort,
                                    on_chunk=chunk, cancel_event=cancel, tools=tools)
        if not result.tool_calls:
            return result
        if authorize is None:
            raise GatewayError('当前连接没有工具执行权限。')
        calls = prepare_calls(result.tool_calls)
        messages.append(ChatMessage('assistant', result.text, tool_calls=calls,
                                    reasoning_content=result.reasoning_content))
        stopped = None
        for call in calls:
            name = call['function']['name']
            arguments = json.loads(call['function']['arguments'])
            response = {'ok': False, 'error': '批次已停止，此操作未执行。',
                        'meta': {'execution_state': 'not_started'}}
            if cancel.is_set():
                stopped = GenerationCancelled('已停止工具调用')
            if stopped is None:
                status('正在检查权限：' + name)
                try:
                    authorize(name, arguments, cancel)
                    if cancel.is_set():
                        raise GenerationCancelled('已停止工具调用')
                except GenerationCancelled as exc:
                    stopped = exc
                except (PermissionError, ValueError) as exc:
                    response['error'] = str(exc)
                else:
                    status('正在执行：' + name)
                    response = invoke(arguments, lambda: client.call_tool(name, arguments))
            body = json.dumps(response, ensure_ascii=False)
            messages.append(ChatMessage('tool', body, tool_call_id=call['id']))
            if response.get('meta', {}).get('execution_state') == 'unknown':
                stopped = GatewayError('工具执行结果未知，已停止。请先检查目标状态；不会自动重试此操作。')
            elif guard.observe(name, arguments, response['ok'], body):
                stopped = GatewayError('工具重复调用没有进展，已停止。')
        # A complete assistant/tool batch is saved even on cancellation or a
        # lost tool receipt. Retrying generation cannot replay its side effects.
        checkpoint(copy.deepcopy(messages))
        next_round()
        if stopped is not None:
            raise stopped
        status('工具已返回，正在生成回复…')
    raise GatewayError('已达 8 轮工具调用上限。已有执行记录已保存。')
