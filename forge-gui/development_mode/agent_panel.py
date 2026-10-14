"""Coding Agent views over TaskEvidence; no execution or global link handlers."""
import json
import re
from collections import defaultdict, deque
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from chat_widgets import parse_blocks, inline_segments
from gui_theme import C, FONT_UI, FONT_UI_BOLD, FONT_SECTION, FONT_MONO_SM, style_editor, button_feedback, attach_tooltip
from ui_icons import IconButton
from forge.secrets import redact


def readonly(parent, **options):
    text = tk.Text(parent, height=6, font=FONT_UI, wrap='word', **options)
    style_editor(text)
    scroll = ttk.Scrollbar(parent, command=text.yview)
    scroll.pack(side='right', fill='y')
    text.configure(yscrollcommand=scroll.set, state='disabled')
    text.pack(fill='both', expand=True)
    return text


def replace(text, body):
    body = str(redact(body))
    if text.get('1.0', 'end-1c') == body:
        return
    position = text.yview()
    text.configure(state='normal')
    text.delete('1.0', 'end')
    text.insert('1.0', body)
    text.configure(state='disabled')
    text.yview_moveto(position[0])


class Conversation(tk.Text):
    """Shared Markdown parser + Text tags; link callbacks belong to this instance."""
    def __init__(self, parent, open_file, history_source=None):
        super().__init__(parent, height=6, wrap='word', font=FONT_UI)
        style_editor(self)
        self.configure(state='disabled')
        self.open_file = open_file
        self.history_source = history_source
        self._history_path = None
        self._history_times = defaultdict(deque)
        self.tag_configure('timestamp', foreground=C['comment'], spacing1=6)
        self.tag_configure('heading', font=FONT_SECTION, foreground=C['text'])
        self.tag_configure('b', font=FONT_UI_BOLD)
        self.tag_configure('code', font=FONT_MONO_SM, background=C['code_bg'])
        self.tag_configure('quote', foreground=C['subtext'], lmargin1=12, lmargin2=12)
        self._links = 0
        self._windows = []
        self._last_width = 0
        self.bind('<Configure>', self._fit_cards)

    def _fit_cards(self, event):
        if event.width == self._last_width:
            return
        self._last_width = event.width
        for widget in self._windows:
            if widget.winfo_exists() and hasattr(widget, 'fit'):
                widget.fit(max(100, event.width - 24))

    def clear(self):
        self.configure(state='normal')
        self.delete('1.0', 'end')
        self.configure(state='disabled')

    def delete(self, index1, index2=None):
        if index1 == '1.0' and index2 == 'end':
            self._clear_cards()
        return super().delete(index1, index2)

    def _clear_cards(self):
        for widget in self._windows:
            if widget.winfo_exists():
                widget.destroy()
        self._windows.clear()
        self._history_path = None
        self._history_times.clear()

    def insert(self, index, chars, *tags):
        # Existing recovery adapter appends whole historical messages to Text.
        match = re.fullmatch(r'\n(user|assistant)\n([\s\S]*)\n', str(chars)) if not tags else None
        if match:
            return self.message(match[1], match[2], timestamp=self._historical_time(match[1], match[2]), historical=True)
        return super().insert(index, chars, *tags)

    def _historical_time(self, role, body):
        # Read only the already-authorized recovery journal, once per restore.
        # Older logs without timestamps remain explicitly unknown.
        path = self.history_source() if self.history_source else None
        if path and path != self._history_path:
            self._history_path = path
            self._history_times.clear()
            from forge.loop import sanitise_child_output
            try:
                with Path(path).open('rb') as stream:
                    raw = stream.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    return None
                nested = 0
                messages = deque(maxlen=100)
                for line in raw.splitlines():
                    try:
                        event = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(event, dict):
                        continue
                    kind = event.get('type')
                    if kind == 'subagent_spawn':
                        nested += 1
                    elif kind in {'subagent_result', 'subagent_error'}:
                        nested = max(0, nested - 1)
                    elif kind == 'development_recovery':
                        messages.clear()
                        for message in event.get('history', []):
                            if isinstance(message, dict) and message.get('role') in {'user', 'assistant'}:
                                messages.append((message['role'], str(redact(message.get('content', ''))), message.get('ts')))
                    elif kind in {'user_message', 'assistant_message'} and not event.get('depth', 0) and ('depth' in event or not nested):
                        messages.append(('user' if kind == 'user_message' else 'assistant',
                            sanitise_child_output(str(redact(event.get('content', '')))), event.get('ts')))
                for name, content, ts in messages:
                    self._history_times[(name, content)].append(ts)
            except (OSError, ValueError):
                return None
        times = self._history_times.get((role, body))
        return times.popleft() if times else None

    def message(self, role, body, *, timestamp=None, historical=False):
        self.configure(state='normal')
        if timestamp is None and not historical:
            timestamp = datetime.now().timestamp()  # actual message display time
        try:
            stamp = datetime.fromtimestamp(float(timestamp)).strftime('%H:%M')
        except (TypeError, ValueError, OverflowError, OSError):
            stamp = '时间未记录'
        self.insert('end', '\n' + stamp + '\n', 'timestamp')
        self.insert('end', '\n' + str(redact(role)) + '\n', 'heading')
        for block in parse_blocks(str(redact(body))):
            kind, value = block['type'], block.get('text', '')
            if kind == 'hr':
                self.insert('end', '────────\n')
            elif kind in {'code', 'table'}:
                self.insert('end', value + '\n', 'code')
            else:
                prefix = {'li': '• ', 'oli': block.get('num', '') + '. ',
                          'todo': '✓ ' if block.get('done') else '○ ', 'quote': '› '}.get(kind, '')
                self.insert('end', prefix)
                for segment, tag in inline_segments(value):
                    for part in re.split(r'(\[[^\]]+\]\([^\s)]+\)|[\w./\\-]+\.[\w]+(?::\d+)?)', segment):
                        if not part:
                            continue
                        match = re.fullmatch(r'\[([^\]]+)\]\(([^)]+)\)', part)
                        path = match[2] if match else part
                        if (match or re.fullmatch(r'[\w./\\-]+\.[\w]+(?::\d+)?', part)) and '://' not in path:
                            self._links += 1
                            link = 'file' + str(self._links)
                            self.tag_configure(link, foreground=C['link'], underline=True)
                            self.tag_bind(link, '<Button-1>', lambda e, p=path: self.open_file(p))
                            self.tag_bind(link, '<Enter>', lambda e: self.configure(cursor='hand2'))
                            self.tag_bind(link, '<Leave>', lambda e: self.configure(cursor='xterm'))
                            self.insert('end', match[1] if match else part, (tag or '', link))
                        else:
                            self.insert('end', part, 'heading' if kind == 'h' else 'quote' if kind == 'quote' else tag or '')
                self.insert('end', '\n')
        self.configure(state='disabled')
        self.see('end')

    def card(self, widget):
        self.configure(state='normal')
        self.insert('end', '\n')
        self.window_create('end', window=widget)
        self.insert('end', '\n')
        self._windows.append(widget)
        if hasattr(widget, 'fit'):
            widget.fit(max(100, self.winfo_width() - 24))
        self.configure(state='disabled')


class ToolSummary(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=6)
        self.summary = ttk.Label(self, text='工具步骤', font=FONT_UI)
        self.summary.pack(anchor='w')
        self.button = ttk.Button(self, text='View Details', command=self.toggle)
        self.button.pack(anchor='w')
        self.detail_frame = ttk.Frame(self)
        self.detail = readonly(self.detail_frame, width=38)
        self.expanded = False

    def toggle(self):
        self.expanded = not self.expanded
        if self.expanded:
            self.detail_frame.pack(fill='both', expand=True)
        else:
            self.detail_frame.pack_forget()
        self.button.configure(text='Hide Details' if self.expanded else 'View Details')

    def fit(self, width):
        self.summary.configure(wraplength=width)
        from tkinter import font
        char = max(1, font.Font(root=self, font=FONT_UI).measure('0'))
        self.detail.configure(width=max(10, (width - 20) // char))

    def update_actions(self, actions):
        counts = {}
        for row in actions:
            counts[row['status']] = counts.get(row['status'], 0) + 1
        self.summary.configure(text='工具 · ' + ' / '.join(f'{k}: {v}' for k, v in counts.items()))
        replace(self.detail, '\n'.join(f"{r.get('agent', 'forge')} · {r.get('tool', '')} · {r['status']}\n"
            + str(r.get('result', ''))[:1500] for r in actions[-100:]))


class DiffCard(ttk.Frame):
    def __init__(self, parent, change, *, view, open_file, revert, pending):
        super().__init__(parent, padding=6)
        self.change = change
        self.title = ttk.Label(self, wraplength=310, font=FONT_UI_BOLD)
        self.title.pack(anchor='w')
        self.state_label = ttk.Label(self, wraplength=310)
        self.state_label.pack(anchor='w')
        self.row = ttk.Frame(self)
        self.row.pack(fill='x')
        self.buttons = {}
        self._width = 260
        self.callbacks = dict(view=view, open=open_file, revert=revert, pending=pending)
        self.update_change(change)

    def update_change(self, change):
        self.change = dict(change)
        self.title.configure(text='Agent Modified: ' + change['path'])
        state = change['state']
        label = {'pending': '待审批 · 未写入', 'written': '已写入工作区', 'committed': '已提交 Git'}.get(state, state)
        counts = ('行数预览不可用' if change.get('added') is None else
                  f"+ {change['added']} lines / - {change['removed']} lines")
        if change.get('revision_changed'):
            label += ' · 当前文件版本已变化'
        self.state_label.configure(text=label + '\n' + counts)
        for button in self.buttons.values():
            button.destroy()
        self.buttons.clear()
        operations = [('view', 'View Diff'), ('open', 'Open File')]
        if state == 'pending':
            operations.append(('pending', '处理授权 / 重试'))
        elif state == 'written' and change.get('checkpoint') and change.get('before_revision') is not None and not change.get('revision_changed'):
            operations.append(('revert', 'Revert'))
        for key, label in operations:
            button = ttk.Button(self.row, text=label, command=lambda k=key: self.callbacks[k](self.change))
            self.buttons[key] = button
        self.fit(self._width)

    def fit(self, width):
        self._width = width
        self.title.configure(wraplength=width)
        self.state_label.configure(wraplength=width)
        # Wrap real actions into rows rather than clipping Revert in narrow panes.
        columns = max(1, width // 110)
        for index, button in enumerate(self.buttons.values()):
            button.grid(row=index // columns, column=index % columns, sticky='w', padx=(0, 4), pady=2)


class AgentEvents(ttk.Frame):
    """Embeddable bottom-tab content; consumes the host's existing event stream."""
    def __init__(self, parent):
        super().__init__(parent)
        self.text = readonly(self)

    def append_event(self, event):
        text = json.dumps(redact(event), ensure_ascii=False, indent=2)
        self.text.configure(state='normal')
        self.text.insert('end', text[:16384] + ('\n[预览截断；完整日志见 Session]' if len(text) > 16384 else '') + '\n')
        self.text.configure(state='disabled')


class TaskInput(tk.Text):
    """An overlay placeholder, never inserted into the task sent to Runtime."""
    def __init__(self, parent, **options):
        super().__init__(parent, **options)
        style_editor(self)
        self.placeholder = tk.Label(self, text='输入任务描述，按 Enter 发送，\nShift+Enter 换行 …',
            bg=self.cget('background'), fg=C['placeholder'], font=FONT_UI, justify='left')
        self.placeholder.bind('<Button-1>', lambda event: self.focus_set())
        self.bind('<<Modified>>', self._placeholder_changed, add='+')
        self._placeholder_changed()

    def _placeholder_changed(self, event=None):
        if self.get('1.0', 'end-1c'):
            self.placeholder.place_forget()
        else:
            self.placeholder.place(x=6, y=6)
        if self.edit_modified():
            self.edit_modified(False)


class PlanningCard(ttk.Frame):
    LEVELS = {'none': '无 (None)', 'low': '低 (Low)', 'medium': '中等 (Medium)', 'high': '高 (High)'}

    def __init__(self, parent, view_changes):
        super().__init__(parent, padding=8)
        ttk.Label(self, text='任务规划', font=FONT_UI_BOLD).pack(anchor='w')
        self.level = ttk.Label(self, foreground=C['subtext'])
        self.level.pack(anchor='w')
        self.authorization = ttk.Label(self, wraplength=280)
        self.authorization.pack(anchor='w')
        self.permission = ttk.Label(self, foreground=C['subtext'])
        self.permission.pack(anchor='w')
        self.view = ttk.Button(self, text='查看变更', command=view_changes, state='disabled')
        self.view.pack(anchor='w', pady=(4, 0))
        self._view_changes = view_changes
        self._display = None

    def fit(self, width):
        self.authorization.configure(wraplength=width)

    def update_plan(self, evidence, context):
        level = self.LEVELS.get(context.get('planning'), '级别未记录')
        permission = context.get('permission')
        label = {'read-only': '只读', 'workspace-write': '工作区写'}.get(permission, '权限未记录')
        pending = any(r['status'] == 'awaiting_approval' for r in evidence.actions.values())
        denied = any(r.get('authorization') == 'deny' for r in evidence.actions.values())
        auth = ('待逐动作审批' if pending else '动作被拒绝' if denied else
                '已授权工作区写入' if permission == 'workspace-write' else
                '只读授权' if permission == 'read-only' else '未提交授权')
        if context.get('recovered'):
            auth = '历史授权记录 · 当前恢复为只读'
        enabled = bool(self._view_changes and (evidence.changes or evidence.diffs))
        display = (level, auth, label, enabled)
        if display == self._display:
            return
        self._display = display
        self.level.configure(text=level)
        self.permission.configure(text='权限 · ' + label)
        self.authorization.configure(text='授权 · ' + auth)
        self.view.configure(state='normal' if enabled else 'disabled')


class AgentPanel(ttk.Frame):
    def __init__(self, parent, *, open_file, view_diff, revert, pending, configure_model, refresh,
                 new_conversation=None, history=None, settings=None, view_changes=None, history_source=None,
                 on_show_approvals=None):
        super().__init__(parent)
        titlebar = ttk.Frame(self, padding=(8, 6))
        titlebar.pack(fill='x')
        self.title = ttk.Label(titlebar, text='AI Agent', font=FONT_UI_BOLD)
        self.title.pack(side='left', fill='x', expand=True)
        self.header_buttons = {}
        for key, icon, label, callback in (
                ('new', 'plus', '新对话', new_conversation),
                ('history', 'clock', '历史', history), ('settings', 'config', '设置', settings),
                ('approvals', 'check', '审批', on_show_approvals)):
            button = IconButton(titlebar, icon=icon, icon_size=16, text='', command=callback,
                bg=C['bg'], fg=C['subtext'], bd=0, padx=7, pady=5, takefocus=True, highlightthickness=1,
                state='normal' if callback else 'disabled')
            button_feedback(button)
            attach_tooltip(button, label)
            button.pack(side='left', padx=2)
            self.header_buttons[key] = button
        # 运行时上下文（权限矩阵 + 模型路由）—— 4-Tab 之上、标题之下
        self._runtime_context = None  # 由 app 调 mount_runtime_context 注入
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill='both', expand=True)
        # 4 个 Tab：对话 / 任务 / 子 Agent / 完整事件
        conversation, tasks, agents, events = (ttk.Frame(self.notebook) for _ in range(4))
        self.tasks_page = tasks
        for frame, label in zip((conversation, tasks, agents, events),
                                ('对话', '任务', '子 Agent', '完整事件')):
            self.notebook.add(frame, text=label)
        self.chat = Conversation(conversation, open_file, history_source)
        sb = ttk.Scrollbar(conversation, command=self.chat.yview)
        sb.pack(side='right', fill='y')
        self.chat.configure(yscrollcommand=sb.set)
        self.chat.pack(fill='both', expand=True)
        self.tasks_toolbar = ttk.Frame(tasks)
        self.tasks_toolbar.pack(side='bottom', fill='x')
        task_body = ttk.Frame(tasks)
        task_body.pack(fill='both', expand=True)
        self.workflow = readonly(task_body)
        for tag, token in (('pending', 'subtext'), ('running', 'warn'), ('completed', 'ok')):
            self.workflow.tag_configure(tag, foreground=C[token])
        tools = ttk.Frame(agents)
        tools.pack(fill='x')
        ttk.Button(tools, text='模型配置', command=lambda: configure_model(self.selected_name())).pack(side='left')
        ttk.Button(tools, text='核实 Git 版本', command=refresh).pack(side='right')
        self.tree = ttk.Treeview(agents, show='tree headings', columns=('state',), height=3)
        self.tree.heading('#0', text='Agent')
        self.tree.heading('state', text='执行状态')
        self.tree.column('#0', width=180)
        self.tree.column('state', width=100)
        self.tree.pack(fill='x')
        detail = ttk.Frame(agents)
        detail.pack(fill='both', expand=True)
        self.detail = readonly(detail)
        self.detail.configure(height=3)
        self.tree.bind('<<TreeviewSelect>>', self.show_agent)
        # 完整事件 Tab：与底部 mount_events 共享同一 AgentEvents 实例
        # （避免重复构建），但通过这里的 notebook 也直接可见。
        self._events_holder = events
        self.rows = {}
        self._evidence = None
        self._summary = None
        self._cards = {}
        self._callbacks = dict(view=view_diff, open_file=open_file, revert=revert, pending=pending)
        self._last_task = None
        self.events = None
        self.task_context = {}
        self._planning_card = None
        self._view_changes = view_changes

    def mount_runtime_context(self, **kwargs):
        """app 注入 RuntimeContext 组件（权限矩阵 + 模型路由）。"""
        if self._runtime_context is not None:
            self._runtime_context.destroy()
        from .policy_widgets import RuntimeContext
        self._runtime_context = RuntimeContext(self, **kwargs)
        # 插在 titlebar 与 notebook 之间
        self._runtime_context.pack(fill='x', before=self.notebook)
        return self._runtime_context

    def mount_events(self, notebook):
        """WorkBuddy container seam: attach once without rebuilding its layout.

        同时把同一个 AgentEvents 实例挂到 AgentPanel 的「完整事件」Tab
        （line 360 的 self._events_holder）—— 这之前是塞到底部 Notebook 的
        「变更事件」Tab，v0.2 起统一到本面板内。
        """
        if self.events is None:
            self.events = AgentEvents(self._events_holder)
            self._events_holder.notebook_inner = self._events_holder  # placeholder
            self.events.pack(fill='both', expand=True, in_=self._events_holder)
        return self.events

    def selected_name(self):
        selected = self.tree.selection()
        return self.rows.get(selected[0], {}).get('agent') if selected else None

    def show_agent(self, event=None):
        selected = self.tree.selection()
        if not selected:
            return
        row = self.rows.get(selected[0], {})
        name, run = row.get('agent', ''), row.get('run_id', '')
        model = row.get('model') or row.get('selected_model') or '继承宿主路由；尚无 usage 证据'
        actions = [r for r in self._evidence.actions.values() if r.get('agent', 'forge') == name and r.get('run_id', '') == run]
        replace(self.detail, '\n'.join([
            '名称：' + name, '运行：' + run, '任务：' + row.get('task_id', ''),
            '任务内容：' + str(row.get('task', '')), '模型：' + str(model),
            '配置来源（当前运行）：' + str(row.get('model_source', '无来源证据')),
            '状态：' + str(row.get('status', 'unknown')),
            '工具权限上限：' + str(row.get('permission', '未提供；不推断授权')),
            '已暴露工具（仍逐动作授权）：' + ', '.join(row.get('tool_surface', [])),
            '执行历史：', *[f"{r.get('tool', '')} · {r['status']} · {r.get('authorization', '')}" for r in actions[-50:]],
            '输出：' + str(row.get('output', ''))]))

    def render(self, evidence):
        self._evidence = evidence
        lines = ['任务：' + (evidence.task_id or '尚未创建'), '任务状态：' + evidence.phase,
                 '', '计划（未提供阶段事件时不推断完成）：']
        lines += ['☐ ' + str(task) for task in evidence.plan]
        marks = {'running': '◐', 'completed': '☑', 'not_started': '☐', 'awaiting_approval': '☐'}
        lines += ['', '真实工具步骤：'] + [f"{marks.get(r['status'], '!')} {r.get('tool', '')} · {r['status']}" for r in list(evidence.actions.values())[-100:]]
        lines += ['', '测试：'] + [f"{r['status']} · {r.get('command', '')} · 退出码 {r.get('exit_code', '未提供')}" for r in evidence.tests[-20:]]
        lines += ['', f'工作区 Diff 快照：{len(evidence.diffs)}（可能包含其他来源）']
        replace(self.workflow, '\n'.join(lines))
        for number, line in enumerate(lines, 1):
            tag = {'☐': 'pending', '◐': 'running', '☑': 'completed'}.get(line[:1])
            if tag:
                self.workflow.tag_add(tag, f'{number}.0', f'{number}.end')
        if self._last_task != evidence.task_id:
            self._summary = None
            self._last_task = evidence.task_id
            self._planning_card = None
        if evidence.task_id or evidence.plan:
            if self._planning_card is None or not self._planning_card.winfo_exists():
                callback = (lambda e=evidence: self._view_changes(e)) if self._view_changes else None
                self._planning_card = PlanningCard(self.chat, callback)
                self.chat.card(self._planning_card)
            # Configuration events are frozen task evidence, never the next-task controls.
            context = dict(self.task_context)
            main = next((c for c in evidence.configurations.values() if c.get('role') in {'default', 'complex', 'simple'}), {})
            if main.get('permission'):
                context['permission'] = main['permission']
            self._planning_card.update_plan(evidence, context)
        if evidence.actions:
            if self._summary is None or not self._summary.winfo_exists():
                self._summary = ToolSummary(self.chat)
                self.chat.card(self._summary)
            self._summary.update_actions(list(evidence.actions.values()))
        for key, change in evidence.changes.items():
            card = self._cards.get(key)
            if card is None or not card.winfo_exists():
                card = DiffCard(self.chat, change, **self._callbacks)
                self.chat.card(card)
                self._cards[key] = card
            elif card.change != change:
                card.update_change(change)
        selection = self.tree.selection()
        # Stable IDs come from full name + run identity, not display names.
        import hashlib
        keys = {key: hashlib.sha256(repr(key).encode()).hexdigest() for key in evidence.runs}
        desired = set(keys.values())
        for identifier in list(self.rows):
            if identifier not in desired:
                if self.tree.exists(identifier):
                    self.tree.delete(identifier)
                self.rows.pop(identifier)
        for key, row in sorted(evidence.runs.items(), key=lambda item: item[1].get('depth', 0)):
            identifier = keys[key]
            parent = next((keys[k] for k, r in evidence.runs.items() if r.get('run_id') == row.get('parent_run_id') and k != key), '')
            if parent and not self.tree.exists(parent):
                parent = ''
            values = (row.get('status', 'unknown'),)
            if not self.tree.exists(identifier):
                self.tree.insert(parent, 'end', iid=identifier, text=row.get('agent', ''), values=values, open=True)
            else:
                self.tree.item(identifier, text=row.get('agent', ''), values=values)
                if self.tree.parent(identifier) != parent:
                    self.tree.move(identifier, parent, 'end')
            self.rows[identifier] = row
        if selection and self.tree.exists(selection[0]):
            self.tree.selection_set(selection)
        elif self.rows:
            self.tree.selection_set(next(iter(self.rows)))
        self.show_agent()
