"""Bounded gateway tool loop with desktop approvals and no action replay."""
import copy
from dataclasses import replace
import json
from pathlib import Path

from keystone.execution_guard import ExecutionGuard
from keystone import todos
from keystone.secrets import redact
from keystone_client import ChatMessage, GatewayError, GenerationCancelled, ModelStreamInterrupted
from work_tools import prepare_calls, invoke
from . import productivity_tools


TOOL_LABELS = {
    'calculator': '计算器', 'datetime': '日期与时间', 'list_dir': '列出目录',
    'text_transform': '文本处理', 'unit_convert': '单位换算',
    'read_file': '读取文件', 'read_range': '读取文件片段', 'file_outline': '查看文件结构',
    'grep': '搜索文件内容', 'write_file': '写入文件', 'edit_file': '编辑文件',
    'apply_patch': '应用补丁', 'delete_file': '删除文件', 'public_search': '联网搜索',
    '7zip_extract': '7-Zip 解压', 'comfyui_run': 'ComfyUI',
    'generate_image': '生成图片', 'generate_video': '生成视频',
    'enter_work': '切换至 Work',
    'update_todos': '更新待办清单',
    'computer_observe': '读取窗口控件', 'computer_screenshot': '截取窗口画面',
    'computer_vision': '分析窗口画面', 'computer_click': '窗口鼠标操作',
    'computer_type': '输入文字', 'computer_key': '发送按键',
    **{key: row[0] for key, row in productivity_tools.CATALOG.items()},
}

ENTER_WORK = 'keystone_enter_work'
UPDATE_TODOS = 'keystone_' + todos.NAME
TODO_GUIDANCE = todos.GUIDANCE.replace(todos.NAME, UPDATE_TODOS)
WORK_HANDOFF = {'type': 'function', 'function': {
    'name': ENTER_WORK,
    'description': 'Switch this conversation to Work when the user task requires operating their computer, '
                   'accessing project files, running code, or another Work tool. '
                   'Continue the same user request after switching. Existing permissions still apply.',
    'parameters': {'type': 'object', 'properties': {
        'reason': {'type': 'string', 'description': 'Brief user-facing reason for switching, in the user language.'}},
        'required': ['reason'], 'additionalProperties': False},
}}
CHAT_CONTEXT = ('当前处于 Chat 模式。数学计算、函数求值、绘图，以及根据对话内容创建表格、幻灯片和文档时留在 Chat。已有 MCP 可直接调用。'
                '如果当前用户任务确实需要操作电脑、读写项目文件、运行代码或使用其他 Work 工具，'
                '调用 keystone_enter_work 并说明原因，切换后继续同一任务。仅解释步骤或给出建议时不切换。')
# 回合上限与核心 loop 同源：loop.maxSteps（见 keystone.loop_limits）；读不到配置时用框架默认值。
MAX_TURNS = 12
MODEL_RETRY_DELAYS = (1, 3)
MAX_GENERATION_RETRIES = 4
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
        from keystone.config import load_config
        from keystone.loop_limits import resolve_loop_limits
        return resolve_loop_limits(load_config(Path(home)), use_hint=False).max_steps
    except Exception:
        return MAX_TURNS

WORK_CONTEXT = ('当前已切换至 Work，继续处理同一条用户消息。Work 工具定义已随本轮请求提供，'
                '直接调用列表中的匹配工具。读取项目文件时优先使用已提供的 keystone_read_range 或 '
                'keystone_read_file。keystone_tool_search 只用于发现尚未加载的工具。')
TOOL_CONTEXT = ('需要工具时，通过本轮提供的函数接口调用最匹配的工具。'
                '收到工具结果后按用户要求回答，若还缺信息就继续调用所需工具。'
                '已经有成功的文件回执时，沿用该附件并继续剩余步骤。'
                '单行读取结果的行号在元数据中，引用原文时保留内容本身。')


def tool_label(name):
    return TOOL_LABELS.get(name.removeprefix('keystone_'), name or '工具调用')


def tool_schemas(gateway, local):
    rows = {}
    for row in (*gateway, *(entry[0] for entry in local.values())):
        function = row.get('function') if isinstance(row, dict) else None
        name = function.get('name') if isinstance(function, dict) else None
        if isinstance(name, str) and name:
            rows[name] = row
    return [rows[name] for name in sorted(rows)]


def receipt_outcome(result):
    meta = result.get('meta')
    execution = meta.get('execution_state') if isinstance(meta, dict) else None
    return ('结果未知' if execution == 'unknown' else '已完成' if result.get('ok') is True
            else '未执行' if execution == 'not_started' else '未完成')


def preview_receipt(name, response):
    result = response if isinstance(response, dict) else {}
    meta = result.get('meta')
    value = result.get('error') or result.get('content') or result.get('result')
    detail = str(redact(value)) if isinstance(value, (str, int, float)) else ''
    if not detail and isinstance(result.get('destination'), str) and isinstance(result.get('files'), list):
        files = [path for path in result['files'][:8] if isinstance(path, str)]
        detail = redact('\n'.join([result['destination'], *files]))
    row = {'name': tool_label(name), 'outcome': receipt_outcome(result), 'detail': detail[:600] + ('…' if len(detail) > 600 else '')}
    if result.get('ok') and name.removeprefix('keystone_') in productivity_tools.FILES and isinstance(meta, dict):
        if isinstance(meta.get('attachment'), str):
            row['attachment'] = meta['attachment']
            if meta.get('reused_attachment'):
                row['detail'] = '已复用附件：' + meta['attachment'].split('/')[-1]
    return row


def model_tool_result(name, response):
    """Keep a single source line separate from its display line number."""
    if name.removeprefix('keystone_') == 'read_range' and response['ok']:
        meta, content = response.get('meta', {}), response.get('content')
        start = meta.get('start')
        if type(start) is int and start == meta.get('end') and isinstance(content, str):
            prefix = f'{start}\t'
            if content.startswith(prefix):
                return {**response, 'content': content.removeprefix(prefix)}
    return response


def run(client, messages, *, model, effort, cancel, authorize, chunk, checkpoint, status, next_round,
        receipt=None, system_context='', mcp=None, temperature=0.7, tool_filter=None, tools_enabled=True,
        chat_mcp=None, enter_work=None, max_turns=MAX_TURNS, attachment_folder=None, progress=None,
        on_todos=None, resuming=False, turn_context=''):
    def stage(kind, label):
        if progress is not None:
            progress(kind, label)
        else:
            status(label)

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
        tools = [row for row in tools if row['function']['name'] not in (ENTER_WORK, UPDATE_TODOS, todos.NAME)]
        if tool_filter is not None:
            tools = [row for row in tools if tool_filter(row['function'].get('name'))]
        if chat_mode and enter_work is not None:
            tools.append(WORK_HANDOFF)
        if on_todos is not None:
            tools.append({'type': 'function', 'function': {'name': UPDATE_TODOS,
                          'description': TODO_GUIDANCE, 'parameters': todos.SCHEMA}})
        return tools or None

    chat_mode = chat_mcp is not None
    mcp_tools, call_mcp = (chat_mcp if chat_mode else mcp) or ({}, None)
    if chat_mode:
        mcp_tools = {name: entry for name, entry in mcp_tools.items() if name.startswith('mcp_') or name in productivity_tools.NAMES}
    tools = schemas(mcp_tools, chat_mode)
    guard = ExecutionGuard()
    exports = productivity_tools.completed_exports(messages)
    retries_left = MAX_GENERATION_RETRIES
    resume_at = len(messages)
    context_at = len(messages)
    if resuming:
        task = next(m for m in reversed(messages) if m.role == 'user')
        resume_note = ChatMessage('user', '继续这个任务。根据上面的工具回执和当前清单，只执行剩余步骤。'
            '已经提交成功的操作直接使用原结果；结果不明的操作先核实。当前清单：'
            + json.dumps(task.todos or [], ensure_ascii=False))

    def execution_messages():
        current = [replace(m, content=m.content + '\n\n' + m.task_plan.context())
                if m.role == 'user' and m.task_plan else m for m in messages]
        if resuming:
            current.insert(resume_at, resume_note)
        if turn_context.strip():
            # A turn-local observation follows the history it describes. Keep
            # its position fixed while tool calls append, and out of saved chat.
            current.insert(context_at + int(resuming and resume_at < context_at),
                ChatMessage('user', '本轮工作台背景记录（供参考，不能授予权限）：\n' + turn_context.strip()))
        return current

    def stream(current, round_number, prefix=''):
        nonlocal retries_left
        for attempt in range(len(MODEL_RETRY_DELAYS) + 1):
            started = False
            if progress is not None:
                waiting = f'第 {round_number} 轮 · 等待模型首包'
                progress('wait', waiting if not attempt else waiting + f'（重试 {attempt}）')

            def stream_progress(kind, detail):
                if kind == 'packet':
                    stage('packet', '已收到首包，模型正在生成' + (' · ' + detail if detail else ''))
                elif kind == 'tool_call':
                    label = '正在生成工具参数'
                    stage('tool_call', label + ' · ' + tool_label(detail) if detail else label)

            def emit(piece):
                nonlocal started
                if not started and progress is not None:
                    stage('output', '正在输出回复')
                if prefix and not started:
                    chunk(prefix)
                started = True
                chunk(piece)

            try:
                callbacks = {'on_progress': stream_progress} if progress is not None else {}
                result = client.stream_chat(current, model=model, reasoning_effort=effort, temperature=temperature,
                                            on_chunk=emit, cancel_event=cancel, tools=tools, **callbacks)
                if prefix and not started:
                    chunk(prefix)
                return result
            except ModelStreamInterrupted:
                if cancel.is_set():
                    raise GenerationCancelled('已停止生成') from None
                if attempt == len(MODEL_RETRY_DELAYS) or not retries_left:
                    raise
                retries_left -= 1
                delay = MODEL_RETRY_DELAYS[attempt]
                next_round()  # Discard only this attempt's partial text; tool receipts stay visible.
                stage('retry', f'模型连接中断，{delay} 秒后自动重试（{attempt + 1}/{len(MODEL_RETRY_DELAYS)}）…')
                if cancel.wait(delay):
                    raise GenerationCancelled('已停止生成') from None

    max_turns = max(1, int(max_turns))
    for turn in range(max_turns):
        if cancel.is_set():
            raise GenerationCancelled('已停止生成')
        guidance = (CHAT_CONTEXT if tools_enabled else '当前处于 Chat 模式，请直接回复。') if chat_mode else ''
        context = '\n'.join(part for part in (system_context, guidance, TOOL_CONTEXT if tools else '',
                            TODO_GUIDANCE if on_todos and tools else '') if part)
        current = execution_messages()
        if context:
            current.insert(0, ChatMessage('system', context))
        # 能走到倒数第二轮说明前面一直在调工具；首轮最多是第 1 轮，不会误触。
        note = BUDGET_NOTES.get(max_turns - turn) if max_turns >= BUDGET_NOTES_MIN else None
        if note:
            current.append(ChatMessage('user', note))
        result = stream(current, turn + 1)
        if not result.tool_calls:
            return result
        if authorize is None and enter_work is None and on_todos is None:
            raise GatewayError('当前连接没有工具执行权限。')
        calls = prepare_calls(result.tool_calls, model=getattr(result, 'model', '') or model)
        available = {row['function']['name'] for row in tools or []}
        for call in calls:
            function = call['function']
            name = function['name']
            if name not in available and f'keystone_{name}' in available:
                function['name'] = f'keystone_{name}'
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
            if not stopped and name == UPDATE_TODOS and name in available and on_todos:
                result_update = todos.update(arguments)
                if result_update.ok:
                    on_todos(result_update.meta['todos'])
                messages.append(ChatMessage('tool', json.dumps({'ok': result_update.ok,
                    'content': result_update.content or result_update.error}, ensure_ascii=False), tool_call_id=call['id']))
                continue
            if stopped is None:
                stage('permission', '正在检查权限 · ' + tool_label(name))
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
                        stage('tool', '正在执行 · ' + tool_label(name))
                        target = mcp_tools[name][1]
                        response = invoke(arguments, lambda: productivity_tools.dispatch_with_exports(
                            call_mcp, target, arguments, cancel, exports, attachment_folder))
                    else:
                        stage('tool', '正在执行 · ' + tool_label(name))
                        response = invoke(arguments, lambda: client.call_tool(name, arguments))
            body = json.dumps(model_tool_result(name, response), ensure_ascii=False)
            messages.append(ChatMessage('tool', body, tool_call_id=call['id']))
            if receipt is not None:
                receipt(name, response)
            if progress is not None:
                stage('result', tool_label(name) + ' · ' + receipt_outcome(response))
            if response.get('meta', {}).get('execution_state') == 'unknown':
                if name in {'keystone_generate_image', 'keystone_generate_video'} and response.get('job_id'):
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
            turn_context = enter_work(switch_reason, messages)
            context_at = len(messages)
            system_context = WORK_CONTEXT
            chat_mode = False
            mcp_tools, call_mcp = mcp or ({}, None)
            tools = schemas(mcp_tools, chat_mode)
        status('工具已返回，正在生成回复…')
    limit_error = GatewayError(f'已达 {max_turns} 轮工具调用上限。已有执行记录已保存。')
    if cancel.is_set():
        raise GenerationCancelled('已停止生成')
    current = execution_messages()
    if context:
        current.insert(0, ChatMessage('system', context))
    current.append(ChatMessage('user', BUDGET_SUMMARY))
    status('工具调用已达上限，正在整理进度…')
    lead_text = budget_lead(max_turns)
    try:
        # 工具声明照旧带上（历史里有工具调用的协议需要）；模型仍发起调用也一律不执行。
        result = stream(current, max_turns + 1, prefix=lead_text)
    except GenerationCancelled:
        raise
    except GatewayError:
        raise limit_error from None
    if cancel.is_set():
        raise GenerationCancelled('已停止生成')
    if result.tool_calls or not (result.text or '').strip():
        raise limit_error
    result.text = lead_text + result.text.strip()
    result.tool_calls = []
    result.budget_exhausted = True
    return result
