"""Bounded gateway tool loop with desktop approvals and no action replay."""
import copy
import json

from forge.execution_guard import ExecutionGuard
from forge.secrets import redact
from forge_client import ChatMessage, GatewayError, GenerationCancelled
from work_tools import prepare_calls, invoke


TOOL_LABELS = {
    'calculator': '计算器', 'datetime': '日期与时间', 'list_dir': '列出目录',
    'text_transform': '文本处理', 'unit_convert': '单位换算',
    'read_file': '读取文件', 'read_range': '读取文件片段', 'file_outline': '查看文件结构',
    'grep': '搜索文件内容', 'write_file': '写入文件', 'edit_file': '编辑文件',
    'apply_patch': '应用补丁', 'delete_file': '删除文件', 'public_search': '联网搜索',
    '7zip_extract': '7-Zip 解压', 'comfyui_run': 'ComfyUI',
    'generate_image': '生成图片', 'generate_video': '生成视频',
    'enter_work': '切换至 Work',
}

ENTER_WORK = 'forge_enter_work'
WORK_HANDOFF = {'type': 'function', 'function': {
    'name': ENTER_WORK,
    'description': 'Switch this conversation to Work when the user task requires operating their computer, '
                   'accessing project files, running code, or another Work tool. '
                   'Continue the same user request after switching. Existing permissions still apply.',
    'parameters': {'type': 'object', 'properties': {
        'reason': {'type': 'string', 'description': 'Brief user-facing reason for switching, in the user language.'}},
        'required': ['reason'], 'additionalProperties': False},
}}
CHAT_CONTEXT = ('当前处于 Chat 模式。普通对话和现有 MCP 工具能完成的任务留在 Chat。'
                '如果当前用户任务确实需要操作电脑、读写项目文件、运行代码或使用其他 Work 工具，'
                '调用 forge_enter_work 并说明原因，切换后继续同一任务。仅解释步骤或给出建议时不切换。')
WORK_CONTEXT = ('当前已切换至 Work，继续处理同一条用户消息。Work 工具定义已随本轮请求提供，'
                '直接调用列表中的匹配工具。读取项目文件时优先使用已提供的 forge_read_range 或 '
                'forge_read_file。forge_tool_search 只用于发现尚未加载的工具。')
TOOL_CONTEXT = ('需要工具时，通过本轮提供的函数接口调用最匹配的工具。'
                '收到工具结果后按用户要求回答，若还缺信息就继续调用所需工具。'
                '单行读取结果的行号在元数据中，引用原文时保留内容本身。')


def tool_label(name):
    return TOOL_LABELS.get(name.removeprefix('forge_'), name or '工具调用')


def tool_schemas(gateway, local):
    rows = {}
    for row in (*gateway, *(entry[0] for entry in local.values())):
        function = row.get('function') if isinstance(row, dict) else None
        name = function.get('name') if isinstance(function, dict) else None
        if isinstance(name, str) and name:
            rows[name] = row
    return list(rows.values())


def preview_receipt(name, response):
    result = response if isinstance(response, dict) else {}
    meta = result.get('meta')
    execution = meta.get('execution_state') if isinstance(meta, dict) else None
    outcome = ('结果未知' if execution == 'unknown' else '已完成' if result.get('ok') is True
               else '未执行' if execution == 'not_started' else '未完成')
    value = result.get('error') or result.get('content') or result.get('result')
    detail = str(redact(value)) if isinstance(value, (str, int, float)) else ''
    if not detail and isinstance(result.get('destination'), str) and isinstance(result.get('files'), list):
        files = [path for path in result['files'][:8] if isinstance(path, str)]
        detail = redact('\n'.join([result['destination'], *files]))
    return {'name': tool_label(name), 'outcome': outcome, 'detail': detail[:600] + ('…' if len(detail) > 600 else '')}


def model_tool_result(name, response):
    """Keep a single source line separate from its display line number."""
    if name.removeprefix('forge_') == 'read_range' and response['ok']:
        meta, content = response.get('meta', {}), response.get('content')
        start = meta.get('start')
        if type(start) is int and start == meta.get('end') and isinstance(content, str):
            prefix = f'{start}\t'
            if content.startswith(prefix):
                return {**response, 'content': content.removeprefix(prefix)}
    return response


def run(client, messages, *, model, effort, cancel, authorize, chunk, checkpoint, status, next_round,
        receipt=None, system_context='', mcp=None, temperature=0.7, tool_filter=None, tools_enabled=True,
        chat_mcp=None, enter_work=None):
    def schemas(local, chat_mode):
        if not tools_enabled:
            return None
        gateway_tools = []
        if not chat_mode and authorize is not None:
            try:
                gateway_tools = client.list_tools(timeout=5)
            except GatewayError:
                status('网关未开放工具，正在生成对话…')
        tools = tool_schemas(gateway_tools, local) if authorize is not None else []
        tools = [row for row in tools if row['function']['name'] != ENTER_WORK]
        if tool_filter is not None:
            tools = [row for row in tools if tool_filter(row['function'].get('name'))]
        if chat_mode and enter_work is not None:
            tools.append(WORK_HANDOFF)
        return tools or None

    chat_mode = chat_mcp is not None
    mcp_tools, call_mcp = (chat_mcp if chat_mode else mcp) or ({}, None)
    if chat_mode:
        mcp_tools = {name: entry for name, entry in mcp_tools.items() if name.startswith('mcp_')}
    tools = schemas(mcp_tools, chat_mode)
    guard = ExecutionGuard()
    for turn in range(8):
        if cancel.is_set():
            raise GenerationCancelled('已停止生成')
        guidance = (CHAT_CONTEXT if tools_enabled else '当前处于 Chat 模式，请直接回复。') if chat_mode else ''
        context = '\n'.join(part for part in (system_context, guidance, TOOL_CONTEXT if tools else '') if part)
        current = [ChatMessage('system', context), *messages] if context else messages
        result = client.stream_chat(current, model=model, reasoning_effort=effort, temperature=temperature,
                                    on_chunk=chunk, cancel_event=cancel, tools=tools)
        if not result.tool_calls:
            return result
        if authorize is None and enter_work is None:
            raise GatewayError('当前连接没有工具执行权限。')
        calls = prepare_calls(result.tool_calls, model=getattr(result, 'model', '') or model)
        available = {row['function']['name'] for row in tools or []}
        for call in calls:
            function = call['function']
            name = function['name']
            if name not in available and f'forge_{name}' in available:
                function['name'] = f'forge_{name}'
        messages.append(ChatMessage('assistant', result.text, tool_calls=calls,
                                    reasoning_content=result.reasoning_content,
                                    responses_replay=getattr(result, 'responses_replay', None)))
        stopped, switch_reason = None, ''
        for call in calls:
            name = call['function']['name']
            arguments = json.loads(call['function']['arguments'])
            response = {'ok': False, 'error': '批次已停止，此操作未执行。',
                        'meta': {'execution_state': 'not_started'}}
            if cancel.is_set():
                stopped = GenerationCancelled('已停止工具调用')
            if stopped is None:
                status('正在检查权限：' + tool_label(name))
                try:
                    if not tools_enabled:
                        raise PermissionError('当前工具设置或权限不允许此调用。')
                    if name == ENTER_WORK:
                        if not chat_mode or enter_work is None or switch_reason or turn == 7:
                            raise PermissionError('当前回合不能切换模式。')
                        reason = arguments.get('reason')
                        if set(arguments) != {'reason'} or not isinstance(reason, str) or not 0 < len(reason.strip()) <= 240:
                            raise ValueError('请提供简短的切换原因。')
                        switch_reason = str(redact(reason.strip()))
                    else:
                        if chat_mode and name not in mcp_tools:
                            raise PermissionError('该工具需要先进入 Work。')
                        if authorize is None or (tool_filter is not None and not tool_filter(name)):
                            raise PermissionError('当前工具设置或权限不允许此调用。')
                        if name not in available:
                            raise ValueError(f'工具 {name} 未在本轮提供。请从当前工具列表选择并使用准确名称。')
                        authorize(name, arguments, cancel)
                    if cancel.is_set():
                        raise GenerationCancelled('已停止工具调用')
                except GenerationCancelled as exc:
                    stopped = exc
                except (PermissionError, ValueError) as exc:
                    response['error'] = str(exc)
                else:
                    if name == ENTER_WORK:
                        response = {'ok': True, 'content': '已接受 Work 切换请求。请使用下一轮提供的 Work 工具继续。' + switch_reason,
                                    'meta': {'execution_state': 'completed'}}
                    elif name in mcp_tools:
                        status('正在执行：' + tool_label(name))
                        target = mcp_tools[name][1]
                        response = invoke(arguments, lambda: call_mcp(target, arguments, cancel))
                    else:
                        status('正在执行：' + tool_label(name))
                        response = invoke(arguments, lambda: client.call_tool(name, arguments))
            body = json.dumps(model_tool_result(name, response), ensure_ascii=False)
            messages.append(ChatMessage('tool', body, tool_call_id=call['id']))
            if receipt is not None:
                receipt(name, response)
            if response.get('meta', {}).get('execution_state') == 'unknown':
                if name in {'forge_generate_image', 'forge_generate_video'} and response.get('job_id'):
                    stopped = GatewayError(f"生成结果未取回，请先检查厂商任务 {response['job_id']}。")
                else:
                    stopped = GatewayError('工具执行结果未知，已停止。请先检查目标状态；不会自动重试此操作。')
            elif guard.observe(name, arguments, response['ok'], body):
                stopped = GatewayError('工具重复调用没有进展，已停止。')
        # A complete assistant/tool batch is saved even on cancellation or a
        # lost tool receipt. Retrying generation cannot replay its side effects.
        checkpoint(copy.deepcopy(messages))
        next_round()
        if stopped is not None:
            raise stopped
        if switch_reason:
            if cancel.is_set():
                raise GenerationCancelled('已停止生成')
            work_context = enter_work(switch_reason, messages)
            system_context = '\n'.join(part for part in (WORK_CONTEXT, work_context) if part)
            chat_mode = False
            mcp_tools, call_mcp = mcp or ({}, None)
            tools = schemas(mcp_tools, chat_mode)
        status('工具已返回，正在生成回复…')
    raise GatewayError('已达 8 轮工具调用上限。已有执行记录已保存。')
