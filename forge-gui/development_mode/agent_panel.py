"""Coding Agent views over TaskEvidence; no execution or global link handlers."""
import json
import re
import tkinter as tk
from tkinter import ttk

from chat_widgets import parse_blocks, inline_segments
from gui_theme import C, FONT_UI, FONT_UI_BOLD, FONT_SECTION, FONT_MONO_SM, style_editor
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
    def __init__(self, parent, open_file):
        super().__init__(parent, height=6, wrap='word', font=FONT_UI)
        style_editor(self)
        self.configure(state='disabled')
        self.open_file = open_file
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

    def insert(self, index, chars, *tags):
        # Existing recovery adapter appends whole historical messages to Text.
        match = re.fullmatch(r'\n(user|assistant)\n([\s\S]*)\n', str(chars)) if not tags else None
        if match:
            return self.message(match[1], match[2])
        return super().insert(index, chars, *tags)

    def message(self, role, body):
        self.configure(state='normal')
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


class AgentPanel(ttk.Frame):
    def __init__(self, parent, *, open_file, view_diff, revert, pending, configure_model, refresh):
        super().__init__(parent)
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill='both', expand=True)
        conversation, tasks, agents = (ttk.Frame(self.notebook) for _ in range(3))
        self.tasks_page = tasks
        for frame, label in zip((conversation, tasks, agents), ('Conversation', 'Tasks', 'Agents')):
            self.notebook.add(frame, text=label)
        self.chat = Conversation(conversation, open_file)
        sb = ttk.Scrollbar(conversation, command=self.chat.yview)
        sb.pack(side='right', fill='y')
        self.chat.configure(yscrollcommand=sb.set)
        self.chat.pack(fill='both', expand=True)
        self.tasks_toolbar = ttk.Frame(tasks)
        self.tasks_toolbar.pack(side='bottom', fill='x')
        task_body = ttk.Frame(tasks)
        task_body.pack(fill='both', expand=True)
        self.workflow = readonly(task_body)
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
        self.rows = {}
        self._evidence = None
        self._summary = None
        self._cards = {}
        self._callbacks = dict(view=view_diff, open_file=open_file, revert=revert, pending=pending)
        self._last_task = None
        self.events = None

    def mount_events(self, notebook):
        """WorkBuddy container seam: attach once without rebuilding its layout."""
        if self.events is None:
            self.events = AgentEvents(notebook)
            notebook.add(self.events, text='Agent Events')
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
        lines += ['○ ' + str(task) for task in evidence.plan]
        lines += ['', '真实工具步骤：'] + [f"{r.get('tool', '')} · {r['status']}" for r in list(evidence.actions.values())[-100:]]
        lines += ['', '测试：'] + [f"{r['status']} · {r.get('command', '')} · 退出码 {r.get('exit_code', '未提供')}" for r in evidence.tests[-20:]]
        lines += ['', f'工作区 Diff 快照：{len(evidence.diffs)}（可能包含其他来源）']
        replace(self.workflow, '\n'.join(lines))
        if self._last_task != evidence.task_id:
            self._summary = None
            self._last_task = evidence.task_id
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
