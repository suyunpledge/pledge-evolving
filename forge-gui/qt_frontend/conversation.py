"""Bounded gateway tool loop with desktop approvals and no action replay."""
import copy
import json
from pathlib import Path

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
# 回合上限与核心 loop 同源：loop.maxSteps（见 forge.loop_limits）；读不到配置时用框架默认值。
MAX_TURNS = 12
BUDGET_NOTES_MIN = 6   # 与核心 loop 一致：更小的预算不插提醒
# 只附在当次请求尾部、不写入 messages：前缀缓存不变，续跑时也不残留过期提醒。
BUDGET_NOTES = {
    2: '本轮还剩 2 次模型回合（含本次）。请集中完成最重要的剩余工作，把互不依赖的工具调用合并到本次一起发出，不要再开新的探索。',
    1: '这是本轮最后一次模型回合。请直接给出结论；如必须调用工具，只做让工作区保持一致所需的最少操作，之后会请你不带工具地汇报进度。',
}
BUDGET_SUMMARY = ('本轮工具调用次数已用完，之后的工具调用不会再执行。请不调用任何工具，用几行简短文字说明：'
                  '已完成什么、工作区当前状态、还剩什么，以及用户让你继续时的下一步。')


def budget_lead(limit):
    return f'已达本轮 {limit} 次工具调用上限，以下是进度小结；点「继续」可接着做。\n\n'


def turn_limit(home):
    """Work 模式的回合上限：与 CLI / VS Code / 网关同一套解析（不套用厂商步数提示，
    因为这里的模型由界面选择，不一定是配置里的首发路由）。"""
    try:
        from forge.config import load_config
        from forge.loop_limits import resolve_loop_limits
        return resolve_loop_limits(load_config(Path(home)), use_hint=False).max_steps
    except Exception:
        return MAX_TURNS

WORK_CONTEXT = ('当前已切换至 Work，继续处理同一条用户消息。Work 工具定义已随本轮请求提供，'
                '直接调用列表中的匹配工具。读取项目文件时优先使用已提供的 forge_read_range 或 '
                'forge_read_file。forge_tool_search 只用于发现尚未加载的工具。')


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


def run(client, messages, *, model, effort, cancel, authorize, chunk, checkpoint, status, next_round,
        receipt=None, system_context='', mcp=None, temperature=0.7, tool_filter=None, tools_enabled=True,
        chat_mcp=None, enter_work=None, max_turns=MAX_TURNS):
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
    max_turns = max(1, int(max_turns))
    for turn in range(max_turns):
        if cancel.is_set():
            raise GenerationCancelled('已停止生成')
        guidance = (CHAT_CONTEXT if tools_enabled else '当前处于 Chat 模式，请直接回复。') if chat_mode else ''
        context = '\n'.join(part for part in (system_context, guidance) if part)
        current = [ChatMessage('system', context), *messages] if context else list(messages)
        # 能走到倒数第二轮说明前面一直在调工具；首轮最多是第 1 轮，不会误触。
        note = BUDGET_NOTES.get(max_turns - turn) if max_turns >= BUDGET_NOTES_MIN else None
        if note:
            current.append(ChatMessage('user', note))
        result = client.stream_chat(current, model=model, reasoning_effort=effort, temperature=temperature,
                                    on_chunk=chunk, cancel_event=cancel, tools=tools)
        if not result.tool_calls:
            return result
        if authorize is None and enter_work is None:
            raise GatewayError('当前连接没有工具执行权限。')
        calls = prepare_calls(result.tool_calls)
        messages.append(ChatMessage('assistant', result.text, tool_calls=calls,
                                    reasoning_content=result.reasoning_content))
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
                        if not chat_mode or enter_work is None or switch_reason or turn == max_turns - 1:
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
            body = json.dumps(response, ensure_ascii=False)
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
    limit_error = GatewayError(f'已达 {max_turns} 轮工具调用上限。已有执行记录已保存。')
    if cancel.is_set():
        raise GenerationCancelled('已停止生成')
    context = '\n'.join(part for part in (system_context, BUDGET_SUMMARY) if part)
    status('工具调用已达上限，正在整理进度…')
    lead = [True]
    lead_text = budget_lead(max_turns)

    def summary_chunk(piece):
        if lead[0]:
            chunk(lead_text)
            lead[0] = False
        chunk(piece)
    try:
        # 工具声明照旧带上（历史里有工具调用的协议需要）；模型仍发起调用也一律不执行。
        result = client.stream_chat([ChatMessage('system', context), *messages], model=model,
                                    reasoning_effort=effort, temperature=temperature,
                                    on_chunk=summary_chunk, cancel_event=cancel, tools=tools)
    except GatewayError:
        raise limit_error from None
    if cancel.is_set():
        raise GenerationCancelled('已停止生成')
    if result.tool_calls or not (result.text or '').strip():
        raise limit_error
    if lead[0]:
        chunk(lead_text)
    result.text = lead_text + result.text.strip()
    result.tool_calls = []
    result.budget_exhausted = True
    return result
