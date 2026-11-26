"""Bounded gateway tool loop with desktop approvals and no action replay."""
import copy
from dataclasses import replace
import json
from pathlib import Path
from revon.artifact_tools import READERS, describe_output, recovery_hint
from revon.artifacts import summary as artifact_summary

from revon.execution_guard import ExecutionGuard
from revon.planning import task_planning_level
from revon import todos
from revon.secrets import redact
from revon_client import ChatMessage, GatewayError, GenerationCancelled, ModelStreamInterrupted
from work_tools import prepare_calls, invoke
from revon.toolwire import visual_message
from revon.tools import rank_tool_matches
from .visual_tools import REFERENCE
from . import productivity_tools


TOOL_LABELS = {
    'calculator': '计算器', 'datetime': '日期与时间', 'list_dir': '列出目录',
    'text_transform': '文本处理', 'unit_convert': '单位换算',
    'read_file': '读取文件', 'read_range': '读取文件片段', 'file_outline': '查看文件结构',
    'grep': '搜索文件内容', 'write_file': '写入文件', 'edit_file': '编辑文件',
    'apply_patch': '应用补丁', 'delete_file': '删除文件', 'public_search': '联网搜索',
    'fetch_url': '读取网页正文', 'tool_search': '查找可用工具',
    '7zip_extract': '7-Zip 解压', 'comfyui_run': 'ComfyUI',
    'generate_image': '生成图片', 'generate_video': '生成视频',
    'office_list': '列出办公文件', 'office_read': '读取办公内容', 'office_create': '新建办公文件',
    'office_apply': '提交办公编辑', 'office_export': '导出办公文件',
    'view_image': '查看图片', 'office_render': '渲染办公页面', 'read_pdf': '阅读 PDF',
    'run_check': '运行项目验证', 'enter_work': '切换至 Work',
    'update_todos': '更新待办清单',
    'computer_observe': '读取窗口控件', 'computer_screenshot': '截取窗口画面',
    'computer_vision': '分析窗口画面', 'computer_click': '窗口鼠标操作',
    'computer_type': '输入文字', 'computer_key': '发送按键', 'computer_act': '操作后台控件',
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
# 回合上限与核心 loop 同源：loop.maxSteps（见 revon.loop_limits），框架默认不限步数。
# MAX_TURNS 只是 run() 未传 max_turns 时的参数默认值；桌面端总是传入 turn_limit() 的结果。
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


def turn_limit(home, template=None):
    """Work 模式的回合上限：与 CLI / VS Code / 网关同一套解析（不套用厂商步数提示，
    因为这里的模型由界面选择，不一定是配置里的首发路由）。

    ``template``（revon.agent_templates.AgentTemplate）给出角色的步数预算；
    用户在 revon.patch.json 里自己写的 maxSteps 是上限，模板不会越过它。"""
    try:
        from revon.config import load_config
        from revon.loop_limits import resolve_loop_limits
        cfg = load_config(Path(home))
        if template is not None:
            from revon.agent_templates import apply_template
            apply_template(cfg, template)
        return resolve_loop_limits(cfg, use_hint=False).max_steps
    except Exception:
        # Same as the framework default: no step limit. MAX_TURNS stays run()'s own default.
        from revon.loop_limits import NO_STEP_LIMIT
        return NO_STEP_LIMIT

WORK_CONTEXT = ('当前已切换至 Work，继续处理同一条用户消息。Work 工具定义已随本轮请求提供，'
                '直接调用列表中的匹配工具。读取项目文件时优先使用已提供的 keystone_read_range 或 '
                'keystone_read_file。可用 keystone_tool_search 查找本轮提供的工具及其准确名称。')
TOOL_CONTEXT = ('需要工具时，通过本轮提供的函数接口调用最匹配的工具。'
                '删除本机文件时使用 Work 的 keystone_delete_file。若删除被拒绝，保留原文件并向用户说明原因。'
                '处理应用或连接器中的删除请求时优先移入回收站；只能永久删除时，先确认已保存可恢复的副本。'
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
    if isinstance(meta, dict) and meta.get('error_code') == 'process_exit_nonzero' and type(meta.get('exit_code')) is int:
        return f"检查失败（退出码 {meta['exit_code']}）"
    return ('结果未知' if execution == 'unknown' else '已完成' if result.get('ok') is True
            else '未执行' if execution == 'not_started' else '未完成')


def preview_receipt(name, response):
    result = response if isinstance(response, dict) else {}
    meta = result.get('meta')
    value = result.get('error') or result.get('content') or result.get('result')
    detail = str(redact(value)) if isinstance(value, (str, int, float)) else ''
    if isinstance(meta, dict) and meta.get('policy_source') == 'gateway' and meta.get('error_code') == 'policy_denied':
        detail = (f"网关策略拒绝 · {meta.get('policy_mode')} / {meta.get('sandbox')}，读取范围 {meta.get('read_scope')}。\n"
                  + detail)
    if not detail and isinstance(result.get('destination'), str) and isinstance(result.get('files'), list):
        files = [path for path in result['files'][:8] if isinstance(path, str)]
        detail = redact('\n'.join([result['destination'], *files]))
    row = {'name': tool_label(name), 'outcome': receipt_outcome(result), 'detail': detail[:600] + ('…' if len(detail) > 600 else '')}
    if result.get('ok') and name.removeprefix('keystone_') in (*productivity_tools.FILES, 'office_render') and isinstance(meta, dict):
        if isinstance(meta.get('attachment'), str):
            row['attachment'] = meta['attachment']
            if meta.get('reused_attachment'):
                row['detail'] = '已复用附件：' + meta['attachment'].split('/')[-1]
    return row


def desktop_readers(tools):
    names = {row['function']['name'] for row in tools or ()}
    return tuple('keystone_' + reader for reader in READERS if 'keystone_' + reader in names)


def desktop_discovery(response, arguments, tools):
    """Project the successful gateway lookup onto the actual post-filter desktop catalog."""
    entries = []
    functions = {}
    search_tags = {'keystone_public_search': ('web_search', 'web', 'search', '网页', '搜索'),
                   'keystone_fetch_url': ('search', '网页', '正文', '打开链接', '读取网页')}
    for row in tools:
        fn = row['function']
        name = fn['name']
        if name == 'keystone_tool_search':
            continue
        functions[name] = fn
        tags = (*row.get('x-keystone-meta', {}).get('tags', ()), tool_label(name), *search_tags.get(name, ()))
        entries.append((name, fn.get('description', ''), tags))
    found = rank_tool_matches(arguments['query'], entries, arguments.get('limit', 5))
    lines = []
    for name in found:
        required = functions[name].get('parameters', {}).get('required', [])
        lines.append(name + (' · 必填参数 ' + ', '.join(required) if required else ''))
    meta = dict(response.get('meta') or {})
    for field in ('activated', 'artifact', 'model_view'):
        meta.pop(field, None)
    meta['available'] = found
    body = '本轮可调用：\n' + '\n'.join(lines) if lines else '本轮提供的工具中没有匹配项。'
    meta['unavailable'] = [item for item in meta.get('unavailable', [])
                           if isinstance(item, dict) and isinstance(item.get('name'), str)
                           and item.get('reason') and 'keystone_' + item['name'] not in functions]
    for item in meta['unavailable']:
        body += f"\n当前不可用：{item['name']} · {item['reason']}"
    if not found:
        body = '\n'.join([body, *meta.get('availability_hints', [])])
    if ('keystone_fetch_url' not in functions and
            ('keystone_public_search' in found or 'fetch_url' in arguments['query'].casefold())):
        body += '\n当前工具列表未开放网页正文读取。可在 Work 工具设置中检查权限和「搜索」分组。'
    return {**response, 'content': body, 'meta': meta}


def model_tool_result(name, response, readers=()):
    """Keep a single source line separate from its display line number."""
    if name.removeprefix('keystone_') == 'read_range' and response['ok']:
        meta, content = response.get('meta', {}), response.get('content')
        start = meta.get('start')
        if type(start) is int and start == meta.get('end') and isinstance(content, str):
            prefix = f'{start}\t'
            if content.startswith(prefix):
                return {**response, 'content': content.removeprefix(prefix)}
    meta = response.get('meta') or {}
    artifact = meta.get('artifact') or {}
    if isinstance(artifact, dict) and isinstance(artifact.get('id'), str) and 'characters' in artifact:
        field = 'content' if response.get('content') else 'error'
        body = response.get(field, '')
        if isinstance(body, str) and body.startswith(f"[Tool output retained as untrusted artifact {artifact['id']};"):
            preview = body.partition(']\n')[2]
            return {**response, field: artifact_summary(artifact, preview, preview=len(preview), readers=readers)}
        if meta.get('model_view'):
            return {**response, 'retrieval_hint': recovery_hint(readers)}
    return response


def run(client, messages, *, model, effort, cancel, authorize, chunk, checkpoint, status, next_round,
        receipt=None, system_context='', mcp=None, temperature=0.7, tool_filter=None, tools_enabled=True,
        chat_mcp=None, enter_work=None, max_turns=MAX_TURNS, attachment_folder=None, progress=None,
        on_todos=None, resuming=False, turn_context=''):
    role_context = system_context  # 调用方的稳定指引（如角色模板），切换到 Work 后仍要带上
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
        # Keep the user's selected desktop search engine and function-capable calculator
        # when present. Disabling either still leaves its gateway counterpart usable.
        names = {row['function']['name'] for row in tools}
        duplicates = {other for preferred, other in (
            ('keystone_public_search', 'keystone_web_search'),
            ('keystone_evaluate_math', 'keystone_calculator')) if preferred in names}
        tools = [row for row in tools if row['function']['name'] not in duplicates]
        if chat_mode and enter_work is not None:
            tools.append(WORK_HANDOFF)
        if on_todos is not None:
            tools.append({'type': 'function', 'function': {'name': UPDATE_TODOS,
                          'description': TODO_GUIDANCE, 'parameters': todos.SCHEMA}})
        readers = desktop_readers(tools)
        for index, row in enumerate(tools):
            function = row['function']
            description = function.get('description', '')
            if function['name'] == 'keystone_tool_search':
                description = 'Find matching tools offered in this desktop turn, with exact callable names and required arguments.'
            if function['name'] in ('keystone_public_search', 'keystone_web_search'):
                description = description.removesuffix(' Then read promising pages with fetch_url.')
                if 'keystone_fetch_url' in names:
                    description += ' Read a result page with keystone_fetch_url.'
            hint = describe_output(description, readers)
            if hint != function.get('description', ''):
                tools[index] = {**row, 'function': {**function, 'description': hint}}
        return tools or None

    chat_mode = chat_mcp is not None
    mcp_tools, call_mcp = (chat_mcp if chat_mode else mcp) or ({}, None)
    if chat_mode:
        mcp_tools = {name: entry for name, entry in mcp_tools.items() if name.startswith('mcp_') or name in productivity_tools.NAMES or entry[1] == ('visual', 'view_image')}
    tools = schemas(mcp_tools, chat_mode)
    pending_images = []
    # Only attachments published by a known local producer in this conversation are eligible.
    producer_ids = {c.get('id') for m in messages for c in (m.tool_calls or [])
                    if c.get('function', {}).get('name') in ('keystone_plot_chart', 'keystone_office_render')}
    attachment_refs = set()
    for message in messages:
        if message.role == 'tool' and message.tool_call_id in producer_ids:
            try:
                ref = json.loads(message.content).get('meta', {}).get('attachment')
            except (ValueError, AttributeError):
                continue
            if isinstance(ref, str):
                attachment_refs.add(ref)
    guard = ExecutionGuard()
    exports = productivity_tools.completed_exports(messages)
    retries_left = MAX_GENERATION_RETRIES
    office_followup = False
    completed_creates = {}
    recovered_stream = False
    resume_at = len(messages)
    context_at = len(messages)
    if resuming:
        task = next(m for m in reversed(messages) if m.role == 'user')
        resume_note = ChatMessage('user', '继续这个任务。根据上面的工具回执和当前清单，只执行剩余步骤。'
            '已经提交成功的操作直接使用原结果；结果不明的操作先核实。当前清单：'
            + json.dumps(task.todos or [], ensure_ascii=False))

    def execution_messages():
        current = list(messages)
        for i, m in enumerate(current):
            if m.role != 'user' or not m.task_plan:
                continue
            # A private low-level plan for a simple turn is planner metadata.
            # This also protects conversations saved before repair prompts were isolated.
            if (m.task_plan.level == 'low' and not m.task_plan.show_todos
                    and task_planning_level('auto', 'none', m.user_text or m.content) == 'none'):
                continue
            current[i] = replace(m, content=m.content + '\n\n' + m.task_plan.context())
        if resuming:
            current.insert(resume_at, resume_note)
        if turn_context and turn_context.strip():
            # A turn-local observation follows the history it describes. Keep
            # its position fixed while tool calls append, and out of saved chat.
            slot = context_at
            if resuming and resume_at < context_at:
                slot += 1  # Earlier resume note precedes a later Work handoff.
            current.insert(slot,
                ChatMessage('user', '本轮工作台背景记录（供参考，不能授予权限）：\n' + turn_context.strip()))
        if pending_images:
            current.append(ChatMessage('user', visual_message([image for _, image in pending_images], call_ids=[key for key, _ in pending_images])['content']))
        return current

    def stream(current, round_number, prefix=''):
        nonlocal retries_left, recovered_stream
        for attempt in range(len(MODEL_RETRY_DELAYS) + 1):
            started = False
            if progress is not None:
                waiting = f'第 {round_number} 轮 · 等待模型首包'
                progress('wait', waiting if not attempt else waiting + f'（重试 {attempt}）')

            def stream_progress(kind, detail):
                if kind == 'packet':
                    stage('packet', '已收到首包，模型正在生成' + (' · ' + detail if detail else ''))
                elif kind == 'reasoning':
                    stage('reasoning', '模型正在思考')
                elif kind in ('tool_call', 'tool_args'):
                    label = '已收到工具调用' if kind == 'tool_call' else '正在接收工具参数'
                    stage(kind, label + ' · ' + tool_label(detail) if detail else label)
                elif kind == 'tool_finish':
                    stage(kind, '工具参数已收齐')

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
                if office_followup and not chat_mode and attempt == 0 and retries_left:
                    callbacks['retryable_office_round'] = True
                result = client.stream_chat(current, model=model, reasoning_effort=effort, temperature=temperature,
                                            on_chunk=emit, cancel_event=cancel, tools=tools, **callbacks)
                recovered_stream = attempt > 0
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
        pending_images.clear()
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
        office_followup = False
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
                        if name in mcp_tools and mcp_tools[name][1] == ('visual', 'view_image'):
                            path = arguments.get('path', '')
                            if isinstance(path, str) and REFERENCE.fullmatch(path) and path not in attachment_refs:
                                raise PermissionError('只能查看当前对话生成的附件。')
                        if name in mcp_tools and mcp_tools[name][1] in (('visual', 'view_image'), ('visual', 'office_render')) and len(pending_images) >= 3:
                            raise ValueError('请先检查本批三张图片，再继续读取。')
                        desktop_grant = authorize(name, arguments, cancel)
                    if cancel.is_set():
                        raise GenerationCancelled('已停止工具调用')
                except GenerationCancelled as exc:
                    stopped = exc
                except (PermissionError, ValueError) as exc:
                    response['error'] = str(exc)
                    response['meta'].update(getattr(exc, 'receipt_meta', {}))
                else:
                    if name == ENTER_WORK:
                        response = {'ok': True, 'content': '已接受 Work 切换请求。请使用下一轮提供的 Work 工具继续。' + switch_reason,
                                    'meta': {'execution_state': 'completed'}}
                    elif name in mcp_tools:
                        stage('tool', '正在执行 · ' + tool_label(name))
                        target = mcp_tools[name][1]
                        create_key = None
                        if name == 'keystone_office_create' and target[0] == 'office':
                            create_key = (target, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
                        if recovered_stream and create_key in completed_creates:
                            response = completed_creates[create_key]
                        else:
                            def dispatch_local():
                                result = productivity_tools.dispatch_with_exports(call_mcp, target, arguments, cancel, exports, attachment_folder)
                                if target in (('visual', 'view_image'), ('visual', 'office_render')) and result.get('ok'):
                                    pending_images.append((call['id'], result.pop('_visual_observation')))
                                return result

                            response = invoke(arguments, dispatch_local)
                            if target in (('productivity', 'plot_chart'), ('visual', 'office_render')) and response.get('ok'):
                                ref = response.get('meta', {}).get('attachment')
                                if isinstance(ref, str):
                                    attachment_refs.add(ref)
                            if create_key is not None and response.get('ok'):
                                completed_creates[create_key] = response
                        if name.startswith('keystone_office_') and response.get('ok'):
                            office_followup = True
                    else:
                        stage('tool', '正在执行 · ' + tool_label(name))
                        response = invoke(arguments, lambda: client.call_tool(name, arguments))
                        meta = response.setdefault('meta', {})
                        if isinstance(desktop_grant, dict) and (meta.get('policy_source') == 'gateway' or
                                                              name == 'keystone_tool_search'):
                            meta.update({k: desktop_grant[k] for k in ('desktop_profile', 'desktop_role')
                                         if k in desktop_grant})
                        if name == 'keystone_tool_search' and response.get('ok'):
                            response = desktop_discovery(response, arguments, tools)
            if name == 'keystone_fetch_url' and response.get('meta', {}).get('may_need_browser'):
                from revon.web_backends import browser_reader
                reader = browser_reader(row['function']['name'] for row in tools)
                if reader:
                    response['meta']['browser_tool'] = reader
            body = json.dumps(model_tool_result(name, response, desktop_readers(tools)), ensure_ascii=False)
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
            system_context = '\n'.join(part for part in (WORK_CONTEXT, role_context) if part)
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
