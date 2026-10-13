"""Development workbench UI. Only queued callbacks touch Tcl/Tk."""
from pathlib import Path
import hashlib
import json
import os
import queue
import threading
import time
import sys
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from gui_theme import (C, apply_ttk_theme, style_editor, FONT_MONO_SM,
    FONT_TITLE, FONT_SECTION, FONT_UI)
try:  # E1（UX 审查）：与 InputCard 同款可选行内 IME 守卫（默认关）
    from ime_inline import InlineIME
except Exception:  # pragma: no cover - 非 Windows / 缺 ctypes
    InlineIME = None
from forge.development.state import ModeState, WorkMode
from forge.development.editor import Document
from forge.development.runtime import DevelopmentRuntime
from forge.development.evidence import TaskEvidence
from forge.secrets import redact, install_tk_exception_redaction
from .project import WorkspaceSource


class DevelopmentWorkbench(ttk.Frame):
    def __init__(self, parent, state, *, runtime=None):
        super().__init__(parent)
        self.state = state
        self.runtime = runtime or DevelopmentRuntime(state, emit=self.enqueue_event)
        self._owns_runtime = runtime is None
        self._previous_emit = self.runtime.emit
        self.runtime.emit = self.enqueue_event if self._owns_runtime else self._forward_event
        self.results = queue.Queue()
        self._closed = False
        self._after_ids = set()
        self._loading = False
        self._jobs = 0
        self._generation = 0
        self._poll = None
        self._external_poll = None
        self._external_busy = False
        self._external_pending = False
        self._scan_busy = False
        self._scan_pending = False
        self.source = None
        self.documents = {}
        self._file_actions = {}
        self.task_evidence = TaskEvidence()
        self._diff_request = 0
        self._compare_path = None
        self.expanded = set()
        self.model_ids = ['']
        self.catalog_rows = []
        self._permission = 'read-only'
        self._session_to_restore = self.state.value('session')
        self._resume_project = self.state.value('project')
        self._build()
        self._unsubscribe = self.state.subscribe(self._mode_changed)
        self.bind('<Destroy>', self._destroyed, add='+')
        self._poll = self.later(40, self._drain)
        self._external_poll = self.later(3000, self._check_external)
        self.later(0, self.apply_layout)

    def later(self, delay, callback):
        if self._closed:
            return None
        identifier = None
        def invoke():
            self._after_ids.discard(identifier)
            if not self._closed:
                callback()
        identifier = self.after_idle(invoke) if delay == 0 else self.after(delay, invoke)
        self._after_ids.add(identifier)
        return identifier

    def _forward_event(self, event):
        self._previous_emit(event)
        self.enqueue_event(event)

    def _build(self):
        header = ttk.Frame(self, padding=(12, 8))
        header.pack(fill='x')
        ttk.Label(header, text='FORGE / Development', font=FONT_TITLE).pack(side='left')
        self.mode = tk.StringVar(value=self.state.mode.value)
        combo = ttk.Combobox(header, textvariable=self.mode, values=['general', 'development'], state='readonly', width=14)
        combo.pack(side='left', padx=12)
        combo.bind('<<ComboboxSelected>>', lambda e: self.switch_mode())
        ttk.Button(header, text='收起 / 展开 IDE', command=self.toggle_ide).pack(side='right')
        ttk.Button(header, text='模型角色', command=self.model_settings).pack(side='right', padx=4)
        options = ttk.Frame(self, padding=(12, 0, 12, 6))
        options.pack(fill='x')
        self.project_button = ttk.Button(options, text='打开项目', command=self.choose_project)
        self.project_button.pack(side='left')
        ttk.Button(options, text='近期项目', command=self.recent_projects).pack(side='left', padx=4)
        self.preset = tk.StringVar(value=self.state.layout['preset'])
        presets = ttk.Combobox(options, textvariable=self.preset, values=['agent', 'editor', 'focus'], state='readonly', width=12)
        presets.pack(side='left')
        presets.bind('<<ComboboxSelected>>', self.change_preset)
        # B4（UX 审查）：移除「专注：编辑器 / Agent」按钮——与预设值 focus
        # 语义重叠（两个入口做同一件事）。toggle_focus() 保留（测试契约与
        # 状态机不变），focus 预设内可用 toggle_focus 的状态字段切换，或
        # 再次选择预设复位。
        self.path_label = ttk.Label(options, text='打开项目后可开始；模式切换不会改变任务权限', anchor='w')
        self.path_label.pack(side='left', fill='x', expand=True, padx=8)
        self.panes = ttk.Panedwindow(self, orient='horizontal')
        self.panes.pack(fill='both', expand=True, padx=8)
        self.project_frame = ttk.Frame(self.panes, width=230)
        self.editor_frame = ttk.Frame(self.panes, width=650)
        self.agent_frame = ttk.Frame(self.panes, width=420)
        self.panes.bind('<ButtonRelease-1>', lambda e: self.capture_layout())
        self._last_pane_width = 0
        self.panes.bind('<Configure>', self._panes_resized)
        self._build_project()
        self._build_editor()
        self._build_agent()
        self.status = tk.StringVar(value='未连接 · 只读 · 无执行任务')
        ttk.Label(self, textvariable=self.status, padding=(12, 6)).pack(fill='x')

    def _build_project(self):
        tools = ttk.Frame(self.project_frame)
        tools.pack(fill='x', pady=4)
        ttk.Label(tools, text='项目', font=FONT_SECTION).pack(side='left')
        ttk.Button(tools, text='刷新', command=self.refresh_project).pack(side='right')
        self.search = tk.StringVar()
        search = ttk.Entry(self.project_frame, textvariable=self.search)
        search.pack(fill='x', pady=(0, 6))
        search.bind('<Return>', lambda e: self.refresh_project())
        ttk.Label(self.project_frame, text='文件名搜索 · Enter', foreground=C['subtext']).pack(anchor='w')
        notebook = ttk.Notebook(self.project_frame)
        notebook.pack(fill='both', expand=True)
        files = ttk.Frame(notebook)
        git = ttk.Frame(notebook)
        notebook.add(files, text='文件')
        notebook.add(git, text='Git 变更')
        self.diff_view = tk.StringVar(value='combined')
        diff_choice = ttk.Combobox(git, textvariable=self.diff_view, state='readonly',
                                  values=['combined', 'working', 'staged'], width=18)
        diff_choice.pack(fill='x', pady=4)
        diff_choice.bind('<<ComboboxSelected>>', self.show_diff)
        self.tree = ttk.Treeview(files, show='tree', selectmode='browse')
        scroll = ttk.Scrollbar(files, command=self.tree.yview)
        scroll.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<Double-1>', self.open_tree)
        self.tree.bind('<Return>', self.open_tree)
        self.tree.bind('<<TreeviewOpen>>', lambda e: self.expand_tree(True))
        self.tree.bind('<<TreeviewClose>>', lambda e: self.expand_tree(False))
        self.changes = ttk.Treeview(git, show='tree', selectmode='browse')
        self.changes.pack(fill='both', expand=True)
        self.changes.bind('<<TreeviewSelect>>', self.show_diff)
        self.changes.bind('<Double-1>', lambda e: self.open_changed())

    def _build_editor(self):
        bar = ttk.Frame(self.editor_frame)
        bar.pack(fill='x', pady=4)
        ttk.Button(bar, text='保存', command=self.save_current).pack(side='left')
        ttk.Button(bar, text='重新载入', command=self.reload_current).pack(side='left', padx=4)
        ttk.Button(bar, text='关闭标签', command=self.close_current).pack(side='left')
        state_bar = ttk.Frame(self.editor_frame)
        state_bar.pack(fill='x', pady=(0, 4))
        ttk.Button(state_bar, text='编码', command=self.open_encoding).pack(side='left')
        actions = ttk.Menubutton(state_bar, text='文件审查')
        actions.pack(side='left', padx=4)
        menu = tk.Menu(actions, tearoff=False, bg=C['bg'], fg=C['text'])
        menu.add_command(label='比较磁盘与草稿', command=self.compare_external)
        menu.add_command(label='安全合并到草稿（不保存）', command=self.merge_external)
        menu.add_separator()
        menu.add_command(label='检查点差异 / 恢复此文件', command=self.checkpoints)
        actions.configure(menu=menu)
        ttk.Button(state_bar, text='附加给 Agent', command=self.attach_current).pack(side='right')
        self.document_state = ttk.Label(self.editor_frame, text='Ctrl+S 保存 · 尚无打开文件', padding=(4, 0, 4, 4))
        self.document_state.pack(fill='x')
        self.vertical = ttk.Panedwindow(self.editor_frame, orient='vertical')
        self.vertical.pack(fill='both', expand=True)
        self.tabs = ttk.Notebook(self.vertical)
        self.tabs.bind('<<NotebookTabChanged>>', lambda event: self.show_document_state())
        self.output = ttk.Notebook(self.vertical)
        self.vertical.add(self.tabs, weight=4)
        self.vertical.add(self.output, weight=1)
        self.vertical.bind('<ButtonRelease-1>', lambda e: self.capture_layout())
        self.output_views = {}
        for name in ('Diff', '终端', '测试', '问题', '输出'):
            page = ttk.Frame(self.output)
            self.output.add(page, text=name)
            text = tk.Text(page, wrap='none', font=FONT_MONO_SM, height=8, undo=False)
            style_editor(text)
            sy = ttk.Scrollbar(page, command=text.yview)
            sy.pack(side='right', fill='y')
            sx = ttk.Scrollbar(page, orient='horizontal', command=text.xview)
            sx.pack(side='bottom', fill='x')
            text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set, state='disabled')
            text.pack(fill='both', expand=True)
            self.output_views[name] = text
        self.write_output('测试', '内置 shell_exec 在 Secret 隔离中不可用。这里只展示真实执行或拒绝事件；没有结构化结果时不推算通过数。\n')

    def _build_agent(self):
        # B1（UX 审查）：语义分组重排——会话（状态/日志/输入/发送）、任务
        # （规划/权限/复审/恢复/子Agent/取消/测试）、模型（选择/独立模型/
        # 角色/回显）三区，用分隔线划界；此前 14 个可交互元素无层级平铺，
        # 语义相近控件（模型类 4 处）散在各行。
        status_row = ttk.Frame(self.agent_frame)
        status_row.pack(fill='x', padx=8, pady=(6, 0))
        self.task_label = ttk.Label(status_row, text='Agent · 未运行', padding=(0, 4), wraplength=260)
        self.task_label.pack(side='left', fill='x', expand=True)
        # P0-D1（UX 审查）：权限常驻标识——安全边界随时可见。
        # W5 修正：徽标父容器从工作台顶层改回 Agent 栏状态行（此前错挂
        # self，渲染到分栏之外的窗口底部）。
        self.perm_badge = ttk.Label(status_row, text='权限 read-only',
                                    foreground=C.get('subtext', '#888'))
        self.perm_badge.pack(side='right')
        self._sync_perm_badge()
        # B2（UX 审查）：完整事件是开发者调试通道，与用户对话/任务并列
        # 一等 tab 提高了认知负荷——默认隐藏，开发者开关按需恢复；数据
        # 始终继续写入隐藏页，开关切换不丢历史。
        self.dev_events = tk.BooleanVar(value=False)
        ttk.Checkbutton(status_row, text='事件', variable=self.dev_events,
                        command=self._toggle_events_tab).pack(side='right', padx=(0, 8))
        log_tabs = ttk.Notebook(self.agent_frame)
        log_tabs.pack(fill='both', expand=True)
        self.log_tabs = log_tabs
        dialogue = ttk.Frame(log_tabs)
        log_tabs.add(dialogue, text='对话')
        workflow = ttk.Frame(log_tabs)
        log_tabs.add(workflow, text='任务 / 子 Agent')
        events = ttk.Frame(log_tabs)
        log_tabs.add(events, text='完整事件')
        self._events_tab = events
        log_tabs.hide(events)
        self.workflow = self._readonly_text(workflow, wrap='word')
        self.chat = self._readonly_text(dialogue, wrap='word')
        self.events = self._readonly_text(events, wrap='word')
        self.editor_contexts = []
        # ── 会话组 ──────────────────────────────────────────────
        self.context_label = ttk.Label(self.agent_frame, text='未附加编辑器内容', padding=4)
        self.input = tk.Text(self.agent_frame, height=5, wrap='word', undo=True, font=FONT_UI)
        style_editor(self.input)
        # E1（UX 审查）：Enter 发送、Shift+Enter 换行（与主对话一致），
        # 输入区旁明示键位；IME 组合确认不触发发送（守卫方式与
        # InputCard 相同：行内 IME 默认关闭，系统候选窗路径 Tk 不向
        # 绑定投递组合期按键）。
        self._inline_ime = (InlineIME(self.input)
                            if InlineIME and os.environ.get("FORGE_INLINE_IME") == "1"
                            else None)
        self.input.bind('<Return>', self._enter_key)
        self.input.bind('<Control-Return>', lambda e: (self.send(), 'break')[-1])
        hint_label = ttk.Label(self.agent_frame, text='Enter 发送 · Shift+Enter 换行 · Ctrl+Enter 亦可',
                               foreground=C.get('muted', '#777'))
        row = ttk.Frame(self.agent_frame)
        self.model_choice = ttk.Combobox(row, values=['继承配置'], state='readonly', width=24)
        self.model_choice.current(0)
        self.model_choice.bind('<<ComboboxSelected>>', lambda event: self.input.focus_set())
        self.model_choice.pack(side='left', fill='x', expand=True)
        self.send_button = ttk.Button(row, text='发送', command=self.send)
        self.send_button.pack(side='right', padx=(4, 0))
        # ── 任务组 ──────────────────────────────────────────────
        task_sep = ttk.Separator(self.agent_frame)
        row2 = ttk.Frame(self.agent_frame)
        self.planning = tk.StringVar(value='low')
        ttk.Label(row2, text='规划').pack(side='left')
        ttk.Combobox(row2, textvariable=self.planning, values=['none', 'low', 'medium', 'high'], state='readonly', width=7).pack(side='left')
        ttk.Label(row2, text='权限').pack(side='left', padx=(8, 0))
        self.permission = tk.StringVar(value='read-only')
        perms = ttk.Combobox(row2, textvariable=self.permission, values=['read-only', 'workspace-write'], state='readonly', width=15)
        perms.pack(side='left')
        perms.bind('<<ComboboxSelected>>', self._sync_perm_badge)
        perms.bind('<<ComboboxSelected>>', self.permission_changed)
        ttk.Button(row2, text='复审', command=self.review).pack(side='right')
        ttk.Button(row2, text='恢复', command=self.restore_session).pack(side='right', padx=4)
        row3 = ttk.Frame(self.agent_frame)
        self.delegate_button = ttk.Button(row3, text='请求子 Agent', command=lambda: self.send(delegate=True))
        self.delegate_button.pack(side='left')
        ttk.Button(row3, text='执行测试', command=self.run_tests).pack(side='right', padx=4)
        self.cancel_button = ttk.Button(row3, text='取消', command=self.cancel_task)
        self.cancel_button.pack(side='right')
        # ── 模型组 ──────────────────────────────────────────────
        model_sep = ttk.Separator(self.agent_frame)
        row4 = ttk.Frame(self.agent_frame)
        ttk.Label(row4, text='任务角色').pack(side='left')
        self.task_role = tk.StringVar(value='default')
        ttk.Combobox(row4, textvariable=self.task_role, values=['default', 'complex', 'simple'],
                     state='readonly', width=9).pack(side='left', padx=4)
        ttk.Button(row4, text='独立模型', command=self.agent_model_settings).pack(side='left', padx=4)
        self.run_model = ttk.Label(row4, text='模型：继承', wraplength=150)
        self.run_model.pack(side='left', fill='x', expand=True)
        # ── P0-L1 容器重组（仲裁裁决 3；只动容器 pack，控件与回调未改）────
        # 溢出根因：中部 log_tabs 是 agent_frame 内唯一 expand，但其后 8 个
        # 固定组仍按 side=top 顺序堆叠——窗口高度不足时 Tk 从序列尾部开始
        # 裁剪，任务组（规划/权限/复审/恢复/子Agent/测试/取消）与模型组
        # （任务角色/独立模型/模型回显）连同两条分隔线被推出窗口下缘之外
        # （实测 1440x900 默认尺寸下 5/11 个子控件 rooty 已达 903~1020）。
        # 修法：固定组改 side='bottom' 先占位——Tk 的 bottom 系列按 pack
        # 顺序自栏底向上排列，故按 row4 → context_label 逆序 pack，视觉顺序
        # 与原布局逐行一致；log_tabs 保持唯一 expand 吃掉中部剩余空间，
        # 从结构上消除溢出，而非靠调大 minsize 掩盖。
        # 控件创建顺序与父子关系均未改动（Tab 遍历序不变，Codex 域契约不变）。
        for slave, kw in (
            (row4, dict(fill='x', padx=6, pady=(0, 6))),        # 模型组（贴栏底）
            (model_sep, dict(fill='x', padx=6, pady=2)),
            (row3, dict(fill='x', padx=6, pady=(0, 4))),        # 任务组
            (row2, dict(fill='x', padx=6, pady=(0, 4))),
            (task_sep, dict(fill='x', padx=6, pady=2)),
            (row, dict(fill='x', padx=6, pady=4)),              # 模型选择 + 发送
            (hint_label, dict(fill='x', padx=8)),               # 键位提示
            (self.input, dict(fill='x', padx=6, pady=4)),       # 输入框
            (self.context_label, dict(fill='x')),               # 附加内容（贴 log_tabs）
        ):
            slave.pack(side='bottom', **kw)

    def _enter_key(self, event):
        """E1：Enter=发送，Shift+Enter=换行；IME 组合期不拦截。"""
        if event.state & 0x0001:        # Shift 按下 → 走默认换行
            return None
        ime = self._inline_ime
        if ime is not None and ime.composing:
            return None                 # 组合确认交给输入法
        self.send()
        return 'break'

    def _toggle_events_tab(self):
        """B2：开发者事件开关——隐藏页恢复显示 / 再次隐藏，不丢数据。"""
        try:
            if self.dev_events.get():
                self.log_tabs.add(self._events_tab, text='完整事件')
            else:
                self.log_tabs.hide(self._events_tab)
        except tk.TclError:
            pass  # 销毁竞态：视图已不在，真相仍留在 runtime 事件流里

    def _readonly_text(self, parent, **kw):
        # B1：请求高度降为 6 行——fill/expand 下实际显示仍由分配决定，
        # 只降低 Notebook 的最小请求（Text 默认 24 行 ≈400px，760px 窗口
        # 里会把 Agent 栏底部控件挤出映射）。
        text = tk.Text(parent, font=FONT_UI, state='disabled', height=6, **kw)
        style_editor(text)
        sb = ttk.Scrollbar(parent, command=text.yview)
        sb.pack(side='right', fill='y')
        text.configure(yscrollcommand=sb.set)
        text.pack(fill='both', expand=True)
        return text

    def append(self, widget, text):
        widget.configure(state='normal')
        widget.insert('end', str(redact(text)))
        widget.configure(state='disabled')
        widget.see('end')

    def write_output(self, name, text, *, clear=False):
        widget = self.output_views[name]
        if clear:
            widget.configure(state='normal')
            widget.delete('1.0', 'end')
            widget.configure(state='disabled')
        self.append(widget, text)

    def work(self, operation, callback, *, generation=None):
        generation = self._generation if generation is None else generation
        self._jobs += 1
        results = self.results
        def run():
            try:
                value = operation()
                results.put((generation, callback, value, None, True))
            except Exception as exc:
                results.put((generation, callback, None, redact(f'{type(exc).__name__}: {exc}'), True))
        threading.Thread(target=run, daemon=True, name='forge-development-io').start()

    def enqueue_event(self, event):
        self.results.put((self._generation, self.handle_event, redact(event), None, False))

    def _drain(self):
        if self._closed:
            return
        deadline = time.monotonic() + .012
        for _ in range(80):
            try:
                generation, callback, value, error, job = self.results.get_nowait()
            except queue.Empty:
                break
            if job:
                self._jobs -= 1
            if generation != self._generation:
                continue
            if error:
                self.status.set(error)
                self.write_output('问题', error + '\n')
            try:
                callback(value, error)
            except Exception as exc:
                self.status.set(redact(str(exc)))
            if time.monotonic() >= deadline:
                break
        self._poll = self.later(40, self._drain)

    def choose_project(self):
        selected = filedialog.askdirectory(parent=self, title='打开开发项目')
        if selected:
            self.open_project(selected)

    def recent_projects(self):
        dialog = tk.Toplevel(self)
        dialog.title('近期开发项目')
        for path in self.state.value('recent'):
            ttk.Button(dialog, text=path, command=lambda p=path: (dialog.destroy(), self.open_project(p))).pack(fill='x', padx=12, pady=4)
        if not self.state.value('recent'):
            ttk.Label(dialog, text='暂无项目记录', padding=16).pack()

    def open_project(self, path):
        if self.runtime.busy or self._loading:
            self.status.set('任务运行中，项目保持不变')
            return
        if any(row['document'].dirty for row in self.documents.values()):
            # P0-D2（UX 审查）：阻塞用户流程的条件必须模态可见——
            # 状态栏是易失通道，下一条消息即覆盖，用户看不到就会
            # 困惑「为什么点了没反应」。
            from tkinter import messagebox as _mb
            _mb.showinfo('无法切换项目', '请先保存或关闭未保存的标签页，再切换项目。', parent=self)
            self.status.set('请先保存或关闭未保存标签，再切换项目')
            return
        if self.source and Path(path).resolve() == self.source.root:
            self.refresh_project()
            return
        if self.source and not self._owns_runtime and self.runtime.ready:
            self.status.set('项目切换由宿主负责；保留借用的 Agent 与会话')
            return
        self._generation += 1
        self._loading = True
        self.project_button.configure(state='disabled')
        self.send_button.configure(state='disabled')
        self.status.set('正在载入项目与现有 Forge Runtime…')
        old = self.runtime
        def load():
            source = WorkspaceSource(path)
            home = self.state.home
            if home.is_relative_to(source.root):
                # Host layout preferences may live in the checkout; execution
                # stores remain outside it, distinct for every host/worktree.
                identity = hashlib.sha256(str(home).encode('utf-8')).hexdigest()[:16]
                home = source.root.parent / ('.forge-development-' + identity)
            if self._owns_runtime and (old.ready or old.home != home):
                runtime = DevelopmentRuntime(self.state, emit=self.enqueue_event, runtime_home=home)
            else:
                runtime = old
            try:
                result = runtime.initialize(source.root)
            except Exception:
                if runtime is not old:
                    runtime.close()
                raise
            if runtime is not old:
                old.close()
            return source, runtime, result
        self.work(load, self._project_loaded)

    def _project_loaded(self, value, error):
        self._loading = False
        self.project_button.configure(state='normal')
        self.send_button.configure(state='normal')
        if error:
            return
        self.source, self.runtime, ready = value
        # An earlier workspace grant does not apply to a different project.
        self._permission = 'read-only'
        self.permission.set('read-only')
        for row in self.documents.values():
            self.tabs.forget(row['frame'])
            row['frame'].destroy()
        self.documents.clear()
        self._file_actions.clear()
        self._changed_selection = ()
        self.task_evidence = TaskEvidence()
        self.render_evidence()
        self.expanded = {str(self.source.root)}
        self._scan_busy = False
        self._external_busy = False
        self._external_pending = False
        self.editor_contexts = []
        self.context_label.configure(text='未附加编辑器内容')
        self.catalog_rows = ready['models']
        self.model_ids = ['', *[row['id'] for row in ready['models']]]
        self.model_choice.configure(values=['继承配置', *[row['label'] for row in ready['models']]])
        self.model_choice.current(0)
        self.path_label.configure(text=str(self.source.root))
        self.status.set('项目已载入 · 只读 · 模式策略从后续任务生效')
        self.refresh_project()
        for path in self.state.value('tabs'):
            if Path(path).is_relative_to(self.source.root):
                self.open_file(path)
        previous = self._session_to_restore
        self._session_to_restore = None
        if previous and self._resume_project and Path(self._resume_project).resolve() == self.source.root:
            if Path(previous).exists() and Path(previous).resolve() != self.runtime.bridge.agent.session.path.resolve():
                self.restore_session(previous)

    def _sync_perm_badge(self):
        """P0-D1：当前权限常驻显示；高于 read-only 时高亮。"""
        perm = getattr(self, 'permission', None)
        if perm is None:
            return
        value = perm.get() if hasattr(perm, 'get') else str(perm)
        try:
            from gui_theme import C as _C
            hot = (value != 'read-only')
            self.perm_badge.configure(
                text=f'权限 {value}',
                foreground=_C.get('error', '#e66') if hot else _C.get('subtext', '#888'))
        except Exception:
            pass

    def _schedule_refresh(self, delay_ms: int = 800, *, documents: bool = False):
        """P0-A2：合并密集事件的刷新（目录/文档扫描）。"""
        self._refresh_due_docs = getattr(self, '_refresh_due_docs', False) or documents
        if getattr(self, '_refresh_after_id', None):
            return
        def _fire():
            self._refresh_after_id = None
            docs, self._refresh_due_docs = self._refresh_due_docs, False
            self.refresh_project()
            if docs:
                self.refresh_documents()
        self._refresh_after_id = self.after(delay_ms, _fire)

    def refresh_project(self):
        if not self.source:
            return
        if self._scan_busy:
            self._scan_pending = True
            return
        self._scan_busy = True
        source, expanded, search = self.source, set(self.expanded), self.search.get()
        self.work(lambda: source.scan(expanded, search), self._project_scan)

    def _project_scan(self, value, error):
        self._scan_busy = False
        if not error:
            selected = self.tree.selection()
            self.tree.delete(*self.tree.get_children())
            for node in value['nodes']:
                path = node['path']
                if path == str(self.source.root):
                    continue
                parent = str(Path(path).parent)
                parent = parent if self.tree.exists(parent) and not self.search.get() else ''
                relative = Path(path).relative_to(self.source.root).as_posix()
                marker = value['status'].get(relative, '')
                self.tree.insert(parent, 'end', iid=path, text=(marker + ' ' + node['name']).strip(),
                                 open=path in self.expanded, values=('dir' if node['is_dir'] else 'file',))
                if node['is_dir'] and path not in self.expanded:
                    self.tree.insert(path, 'end', text='…')
            for item in selected:
                if self.tree.exists(item):
                    self.tree.selection_set(item)
            self.changes.delete(*self.changes.get_children())
            changed_selection = getattr(self, '_changed_selection', ())
            for path, status in value['status'].items():
                self.changes.insert('', 'end', iid=path, text=status + ' ' + path)
            for path in changed_selection:
                if self.changes.exists(path):
                    self.changes.selection_set(path)
            self.status.set(f"{value['branch']} · {self.source.root.name} · {len(value['status'])} 个 Git 变更 · {self.permission.get()}")
        if self._scan_pending:
            self._scan_pending = False
            self.refresh_project()

    def expand_tree(self, opened):
        path = self.tree.focus()
        if path and self.tree.exists(path):
            self.expanded.add(path) if opened else self.expanded.discard(path)
            self.refresh_project()

    def open_tree(self, event=None):
        selection = self.tree.selection()
        if selection and self.tree.item(selection[0], 'values') == ('file',):
            self.open_file(selection[0])
        return 'break'

    def open_changed(self):
        selected = self.changes.selection()
        if selected:
            self.open_file(self.source.root / selected[0])

    def show_diff(self, event=None):
        selected = self.changes.selection()
        self._changed_selection = selected
        if selected:
            self._diff_request += 1
            request = self._diff_request
            path = self.source.root / selected[0]
            source = self.source
            evidence = self._file_actions.get(str(path.resolve()))
            heading = ('真实操作来源 / 审批记录：\n' + json.dumps(evidence, ensure_ascii=False, indent=2)
                       if evidence else '尚无本会话 Agent 操作记录；Git 变更不自动归因于 Agent。') + '\n\n'
            view = self.diff_view.get()
            self.work(lambda: self.runtime.inspect_diff(source, path, view), lambda value, error: self.write_output('Diff', heading + value, clear=True)
                      if not error and request == self._diff_request else None)
            self.output.select(0)

    def open_file(self, path, *, encoding=None):
        if not self.source:
            return
        try:
            path = str(self.source.boundary.path(path))
        except Exception as exc:
            self.status.set(redact(str(exc)))
            return
        if path in self.documents:
            self.tabs.select(self.documents[path]['frame'])
            return
        boundary = self.source.boundary
        self.work(lambda: Document.open(boundary, path, encoding=encoding), self._document_loaded)

    def open_encoding(self):
        if not self.source:
            return
        path = filedialog.askopenfilename(parent=self, initialdir=self.source.root)
        if not path:
            return
        encoding = simpledialog.askstring('文件编码', '明确指定编码（例如 gb18030 / latin-1）。不使用替换解码。', parent=self)
        if encoding:
            self.open_file(path, encoding=encoding.strip())

    def _document_loaded(self, document, error):
        if error:
            # No replacement decoding silently corrupting source. Explicit user choice instead.
            return
        key = str(document.path)
        if key in self.documents:
            self.tabs.select(self.documents[key]['frame'])
            return
        frame = ttk.Frame(self.tabs)
        widget = tk.Text(frame, wrap='none', font=FONT_MONO_SM, undo=True)
        style_editor(widget)
        sy = ttk.Scrollbar(frame, command=widget.yview)
        sx = ttk.Scrollbar(frame, orient='horizontal', command=widget.xview)
        sy.pack(side='right', fill='y')
        sx.pack(side='bottom', fill='x')
        widget.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        widget.pack(fill='both', expand=True)
        widget.insert('1.0', document.text)
        widget.edit_modified(False)
        row = {'document': document, 'frame': frame, 'widget': widget, 'saving': False}
        self.documents[key] = row
        self.tabs.add(frame, text=document.path.name)
        self.tabs.select(frame)
        widget.bind('<<Modified>>', lambda e, r=row: self.document_changed(r))
        widget.bind('<Control-s>', lambda e: (self.save_current(), 'break')[-1])
        self.state.remember_tabs(self.documents)

    def current_row(self):
        selected = self.tabs.select()
        return next((r for r in self.documents.values() if str(r['frame']) == selected), None)

    def document_changed(self, row):
        widget = row['widget']
        if widget.edit_modified() and not row['saving']:
            row['document'].update(widget.get('1.0', 'end-1c'))
            widget.edit_modified(False)
            self.update_tab(row)

    def update_tab(self, row):
        d = row['document']
        prefix = '! ' if d.status == 'conflict' else '* ' if d.dirty else ''
        self.tabs.tab(row['frame'], text=prefix + d.path.name)
        self.show_document_state()

    def show_document_state(self):
        row = self.current_row()
        if not row:
            self.document_state.configure(text='Ctrl+S 保存 · 尚无打开文件')
            return
        d = row['document']
        labels = {'clean': '未修改', 'modified': '未保存', 'saved': '保存成功', 'failed': '保存失败', 'conflict': '外部变更冲突'}
        self.document_state.configure(text=labels[d.status] + ' · ' + d.encoding + ' · Ctrl+S 保存')

    def save_current(self):
        row = self.current_row()
        if not row or row['saving']:
            return
        self.document_changed(row)
        row['saving'] = True
        row['widget'].configure(state='disabled')
        def complete(value, error):
            row['saving'] = False
            row['widget'].configure(state='normal')
            self.update_tab(row)
            if not error:
                self.status.set('已保存 ' + row['document'].path.name)
                self.refresh_project()
        self.work(row['document'].save, complete)

    def reload_current(self):
        row = self.current_row()
        if not row or row['saving']:
            return
        if row['document'].dirty and not messagebox.askyesno('重新载入', '丢弃此标签尚未保存的编辑，读取磁盘版本？', parent=self):
            return
        row['saving'] = True
        row['widget'].configure(state='disabled')
        def complete(value, error):
            row['saving'] = False
            row['widget'].configure(state='normal')
            if not error:
                row['widget'].delete('1.0', 'end')
                row['widget'].insert('1.0', row['document'].text)
                row['widget'].edit_modified(False)
                self.update_tab(row)
        self.work(row['document'].reload, complete)

    def close_current(self):
        row = self.current_row()
        if not row or row['saving']:
            return
        self.document_changed(row)
        if row['document'].dirty and not messagebox.askyesno('关闭文件', '丢弃此标签尚未保存的编辑？保存请先按 Ctrl+S。', parent=self):
            return
        del self.documents[str(row['document'].path)]
        self.tabs.forget(row['frame'])
        row['frame'].destroy()
        self.state.remember_tabs(self.documents)

    def _replace_buffer(self, row):
        widget = row['widget']
        caret, y, x = widget.index('insert'), widget.yview()[0], widget.xview()[0]
        widget.delete('1.0', 'end')
        widget.insert('1.0', row['document'].text)
        widget.mark_set('insert', caret)
        widget.yview_moveto(y)
        widget.xview_moveto(x)
        widget.edit_reset()
        widget.edit_modified(False)
        self.update_tab(row)

    def _reconcile_external(self, value, error):
        self._external_busy = False
        for key, row, baseline, observation in value or []:
            if self.documents.get(key) is not row or row['saving']:
                continue
            if row['document'].digest != baseline:
                # Save/reload/merge overtook this read. Never restore an obsolete
                # observation over a newer editor baseline; schedule a fresh read.
                self._external_pending = True
                continue
            # A user may have typed since the worker started. Read Tcl only here.
            row['document'].update(row['widget'].get('1.0', 'end-1c'))
            outcome = row['document'].reconcile(observation)
            if outcome == 'reloaded':
                self._replace_buffer(row)
                self.status.set('已刷新磁盘版本：' + Path(key).name)
            elif outcome == 'conflict':
                self.update_tab(row)
                self.status.set('外部变更：' + Path(key).name + ' · 草稿保留，可比较磁盘或安全合并')
            if self._compare_path == key:
                self._compare_path = None
                self.write_output('Diff', row['document'].external_diff(buffer=True), clear=True)
                self.output.select(0)
        if self._external_pending:
            self._external_pending = False
            self.refresh_documents()

    def refresh_documents(self):
        if self._external_busy:
            self._external_pending = True
            return
        rows = [(key, r, r['document'].digest) for key, r in self.documents.items() if not r['saving']]
        if rows:
            self._external_busy = True
            self.work(lambda: [(key, row, digest, row['document'].inspect_external()) for key, row, digest in rows],
                      self._reconcile_external)

    def _check_external(self):
        if self._closed:
            return
        if self._external_poll in self._after_ids:
            self.after_cancel(self._external_poll)
            self._after_ids.discard(self._external_poll)
        self.refresh_documents()
        self._external_poll = self.later(3000, self._check_external)

    def compare_external(self):
        row = self.current_row()
        if row:
            self.document_changed(row)
            self._compare_path = str(row['document'].path)
            self.refresh_documents()

    def merge_external(self):
        row = self.current_row()
        if row and not row['saving']:
            self.document_changed(row)
            try:
                row['document'].merge_external()
            except Exception as exc:
                self.status.set(redact(str(exc)))
                self.compare_external()
                return
            self._replace_buffer(row)
            self.status.set('无重叠改动已合并到草稿 · 尚未保存，Ctrl+S 前仍检查磁盘版本')

    def attach_current(self):
        row = self.current_row()
        if row:
            self.document_changed(row)
            # Trusted bridge virtualizes before any provider payload is constructed.
            path = str(row['document'].path)
            if path not in self.editor_contexts:
                self.editor_contexts.append(path)
            self.context_label.configure(text='附加：' + row['document'].path.name + ' · 发送前保护 Secret')
            self.input.focus_set()

    def permission_changed(self, event=None):
        value = self.permission.get()
        if value == 'workspace-write' and not messagebox.askyesno('授权后续任务',
                '允许后续任务修改此工作区内文件？命令审批、插件权限及 Secret 边界仍保留。正在运行的任务不改变权限。', parent=self):
            self.permission.set(self._permission)
            return
        self._permission = value

    def send(self, *, delegate=False):
        prompt = self.input.get('1.0', 'end-1c').strip()
        if not prompt or not self.runtime.ready or self.runtime.busy or self._loading:
            self.status.set('请打开项目，或等待当前任务结束')
            return
        index = self.model_choice.current()
        model = self.runtime.catalog.get(self.model_ids[index]) if index > 0 else None
        permission, planning = self.permission.get(), self.planning.get()
        contexts = []
        for path in self.editor_contexts:
            row = self.documents.get(path)
            if row:
                self.document_changed(row)
                contexts.append({'path': path, 'content': row['widget'].get('1.0', 'end-1c')})
        role = self.task_role.get()
        snapshot = self.state.task_snapshot(explicit_model=model, role=role, permission=permission)
        self.run_model.configure(text='运行模型：' + (' / '.join(snapshot.model) if snapshot.model else '全局路由'))
        self.send_button.configure(state='disabled')
        self.delegate_button.configure(state='disabled')
        self.task_evidence = TaskEvidence()
        self.render_evidence()
        self.input.delete('1.0', 'end')
        self.editor_contexts = []
        self.context_label.configure(text='未附加编辑器内容')
        self.task_label.configure(text='Agent · 正在执行 · 0s')
        # P0-A1（UX 审查）：不确定阶段的存活信号——稳定文案 + 已用时间，
        # 事件类型不再覆盖本行（详见 handle_event）。
        self._task_started_at = time.monotonic()
        self._pulse_phase = 0
        if getattr(self, '_pulse_after_id', None):
            try:
                self.after_cancel(self._pulse_after_id)
            except Exception:
                pass
        def _tick_pulse():
            if not getattr(self, '_task_started_at', None):
                return
            elapsed = int(time.monotonic() - self._task_started_at)
            self._pulse_phase = (self._pulse_phase + 1) % 4
            glyphs = ['◔', '◑', '◕', '●']
            self.task_label.configure(text=f'Agent · 正在执行 {glyphs[self._pulse_phase]} · {elapsed}s')
            self._pulse_after_id = self.after(1000, _tick_pulse)
        _tick_pulse()
        def done(value, error):
            self.send_button.configure(state='normal')
            self.delegate_button.configure(state='normal')
            self._task_started_at = None
            if getattr(self, '_pulse_after_id', None):
                try:
                    self.after_cancel(self._pulse_after_id)
                except Exception:
                    pass
            self.task_label.configure(text='Agent · 失败' if error else 'Agent · ' + str(value.get('stopped', '完成')))
            if error:
                if not self.input.get('1.0', 'end-1c').strip():
                    self.input.insert('1.0', prompt)
            elif value.get('text'):
                self.append(self.chat, '\nForge\n' + value['text'] + '\n')
            self.refresh_project()
            self.refresh_documents()
        self.work(lambda: self.runtime.run(prompt, explicit_model=model, permission=permission,
                                          planning=planning, contexts=contexts, role=role, snapshot=snapshot,
                                          delegate=delegate), done)

    def render_evidence(self):
        view = self.task_evidence
        lines = ['任务：' + (view.task_id or '尚未创建'), '任务状态：' + view.phase, '', '计划（不由文本推断完成）：']
        lines.extend('• ' + str(task) for task in view.plan)
        lines.extend(['', 'Agent / 实际执行状态：'])
        lines.extend(f"{row['agent']} · {row['status']} · {row['run_id'][:8]} · {row.get('model', '')}"
                     for row in view.agents.values())
        lines.extend(['', '真实工具步骤（最近 100 条；完整日志保留）：'])
        lines.extend(f"{r.get('agent', 'forge')} / {r.get('tool', '')} · {r['status']}"
                     for r in list(view.actions.values())[-100:])
        lines.extend(['', '测试 / 命令记录：'])
        lines.extend(f"{r['status']} · {r['command']} · 退出码 {r.get('exit_code', '未提供')}"
                     for r in view.tests[-20:])
        lines.extend(['', f'Diff 快照：{len(view.diffs)}（工作区实况，包含其他来源的修改）'])
        lines.extend(['', '复审：'])
        lines.extend(f"{r['status']} · {(r.get('review') or {}).get('model', '')} · {r.get('message_id', '')[:8]}"
                     for r in list(view.reviews.values())[-10:])
        # P0-A2（UX 审查）：增量渲染——行数只增时 append 差量（保留
        # 滚动位置与用户选区），回缩才全量重绘。每 20 次强制全量一次，
        # 防止浮点误差累积。
        body = redact('\n'.join(lines))
        self._evidence_renders = getattr(self, '_evidence_renders', 0) + 1
        current = self.workflow.get('1.0', 'end - 1c')
        if (len(lines) >= getattr(self, '_evidence_lines', 0)
                and self._evidence_renders % 20 != 0
                and current):
            # 只追加差量：找当前末行在主体中的对齐点
            keep = current.count('\n')  # 现有行数
            if keep <= len(lines):
                tail = '\n'.join(lines[keep:])
                self.workflow.configure(state='normal')
                if tail:
                    self.workflow.insert('end', ('\n' if keep > 0 else '') + tail)
                self.workflow.configure(state='disabled')
                self._evidence_lines = len(lines)
                return
        self.workflow.configure(state='normal')
        self.workflow.delete('1.0', 'end')
        self.workflow.insert('1.0', body)
        self.workflow.configure(state='disabled')
        self._evidence_lines = len(lines)

    def handle_event(self, event, error=None):
        if event.get('event') == 'user':
            self.append(self.chat, '\n你\n' + event.get('display', '') + '\n')
            return
        data = event.get('data', event)
        if data.get('type') == 'development_task_started' and data.get('task_id') != self.task_evidence.task_id:
            self.task_evidence = TaskEvidence()
        self.task_evidence.apply(data)
        if data.get('type') in {'workflow_state', 'task_plan', 'tool_started', 'tool_completed', 'tool_call', 'usage',
                                'development_task_started', 'development_task_finished', 'test_started', 'test_result', 'git_diff',
                                'review_started', 'review_result', 'review_error', 'review_cancelled'}:
            self.render_evidence()
        raw = json.dumps(data, ensure_ascii=False, indent=2)
        if len(raw) > 16384:
            raw = raw[:16384] + '\n[大事件预览已截断；完整记录保留在原有 Session 日志]\n'
        self.append(self.events, raw + '\n')
        kind = data.get('type', '')
        # P0-A1：事件类型不再覆盖存活信号行——工具/阶段动态已在证据
        # 面板与输出区呈现，此处覆盖只制造开发者词汇闪烁。
        if kind == 'tool_call':
            paths = [data.get('args', {}).get('path')]
            blocks = data.get('args', {}).get('patches') or data.get('args', {}).get('edits') or []
            paths.extend(p.get('path') for p in blocks if isinstance(p, dict))
            for path in paths if data.get('ok') and data.get('tool') in {'write_file', 'edit_file', 'apply_patch', 'edit_config', 'delete_file'} else []:
                if not path or not self.source:
                    continue
                try:
                    self._file_actions[str(self.source.boundary.path(path))] = data
                except (ValueError, PermissionError, OSError):
                    pass
            self.write_output('输出', json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            if data.get('tool') == 'shell_exec':
                self.write_output('终端', str(data.get('args', {}).get('command', '')) + '\n' + str(data.get('result', '')) + '\n')
                # Without a structured test event, retain output without inventing counts.
                self.write_output('测试', json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            if not data.get('ok', True):
                self.write_output('问题', str(data.get('result', '')) + '\n')
            # P0-A2：事件密集期目录扫描 800ms 合并（数百事件 × 全扫描
            # 是滚动卡顿主因之一；复用 _scan_pending 的合并语义）。
            self._schedule_refresh(800)
            if data.get('ok') and data.get('tool') in {'write_file', 'edit_file', 'apply_patch', 'edit_config', 'delete_file'}:
                self._schedule_refresh(800, documents=True)
        elif kind in {'model_error', 'review_error', 'subagent_error'}:
            self.write_output('问题', json.dumps(data, ensure_ascii=False) + '\n')
        elif kind == 'test_result':
            heading = json.dumps({k: v for k, v in data.items() if k != 'output'}, ensure_ascii=False, indent=2)
            output = data.get('output', '')
            self.write_output('测试', heading + '\n' + output[:16384] +
                              ('\n[预览已截断，请查看上述 artifact]\n' if len(output) > 16384 else '\n'))

    def cancel_task(self):
        if self.runtime.cancel():
            self.status.set('已请求取消；等待当前模型请求或工具返回，之后停止执行。已写入文件会保留。')
        else:
            self.status.set('当前没有可取消的任务')

    def run_tests(self):
        if self.runtime.busy or self._loading or self.state.mode != WorkMode.DEVELOPMENT:
            self.status.set('请在开发模式完成当前任务后执行测试')
            return
        snap = self.runtime.last_snapshot
        if (not snap or snap.permission != 'workspace-write' or snap.mode != WorkMode.DEVELOPMENT
                or self.permission.get() != 'workspace-write'):
            self.status.set('请先完成一个明确授予 workspace-write 的开发任务')
            return
        directory = simpledialog.askstring('测试目录', '相对于项目的 unittest 测试目录：', initialvalue='tests', parent=self)
        if not directory:
            return
        try:
            target = self.source.boundary.path(directory)
            if not target.is_dir():
                raise ValueError('测试目录不存在')
        except (ValueError, PermissionError, OSError) as exc:
            self.status.set(str(exc))
            return
        argv = [sys.executable, '-m', 'unittest', 'discover', '-s', str(target), '-v']
        if not messagebox.askyesno('信任并执行项目测试',
                '此操作将以当前账户执行项目代码，具有文件和网络访问能力。仅在信任该项目时继续。\n'
                '命令：' + ' '.join(argv) + '\n\n是否执行此命令？', parent=self):
            return
        self.work(lambda: self.runtime.execute_tests(argv, trusted=True),
                  lambda value, error: (self.refresh_project(), self.refresh_documents()) if not error else None)

    def review(self):
        if self.runtime.busy or not self.runtime.ready or self._loading:
            self.status.set('请先完成任务')
            return
        self.work(self.runtime.review, lambda v, e: self.write_output('输出', json.dumps(v, ensure_ascii=False, indent=2) + '\n') if not e else None)

    def restore_session(self, path=None):
        if not self.runtime.ready or self.runtime.busy or self._loading:
            self.status.set('请先打开原项目，并等待任务结束')
            return
        path = path or filedialog.askopenfilename(parent=self, initialdir=self.runtime.home / 'sessions', filetypes=[('Forge Session', '*.jsonl')])
        if not path:
            return
        def restored(value, error):
            if not error:
                self._permission = 'read-only'
                self.permission.set('read-only')
                for widget in (self.chat, self.events):
                    widget.configure(state='normal')
                    widget.delete('1.0', 'end')
                    widget.configure(state='disabled')
                self.task_evidence = TaskEvidence.from_events(value['events'], recovered=True)
                self.render_evidence()
                self._file_actions.clear()
                for path, event in self.task_evidence.changed.items():
                    try:
                        self._file_actions[str(self.source.boundary.path(path))] = event
                    except (ValueError, PermissionError, OSError):
                        pass
                for message in value['history']:
                    self.append(self.chat, '\n' + message['role'] + '\n' + message['content'] + '\n')
                previews = []
                for event in value['events'][-500:]:
                    preview = dict(event)
                    if preview.get('type') == 'session_meta' and 'session_id' in preview:
                        # Public rollout identity must not be scanned as an HTTP
                        # credential when the JSON is rendered as plain text.
                        preview['rollout_id'] = preview.pop('session_id')
                    raw = json.dumps(preview, ensure_ascii=False)
                    previews.append(raw[:16384] + (' [大事件预览截断]' if len(raw) > 16384 else ''))
                self.append(self.events, '恢复最近 500 条事件预览；完整原日志：' + value['path'] + '\n'
                            + '\n'.join(previews) + '\n')
                self.write_output('输出', json.dumps(value['workflow'], ensure_ascii=False, indent=2) + '\n')
                self.status.set('已恢复真实对话；未知操作不重放。请输入检查或继续任务。')
                if value['incomplete_tail']:
                    self.write_output('问题', '旧日志末尾写入中断，原日志已保留；未知操作必须重新核实。\n')
        self.work(lambda: self.runtime.restore_history(path), restored)

    def model_settings(self):
        window = tk.Toplevel(self)
        window.title('Development 模型角色')
        window.geometry('620x360')
        ttk.Label(window, text='仅选择已配置模型；任务运行时修改只影响后续任务。', padding=12).pack(anchor='w')
        choices = ['继承', *[r['label'] for r in self.catalog_rows]]
        labels = {'default': '默认编程', 'complex': '复杂任务', 'review': '代码复审', 'simple': '简单任务', 'subagent': '子 Agent 默认模型'}
        controls = {}
        roles = self.state.value('roles')
        for role in self.state.ROLES:
            row = ttk.Frame(window, padding=(12, 4))
            row.pack(fill='x')
            ttk.Label(row, text=labels[role], width=27).pack(side='left')
            combo = ttk.Combobox(row, values=choices, state='readonly', width=30)
            combo.pack(side='left')
            index = next((n + 1 for n, r in enumerate(self.catalog_rows) if self.runtime.catalog.get(r['id']) == tuple(roles.get(role) or ())), 0)
            combo.current(index)
            controls[role] = combo
        def save():
            for role, combo in controls.items():
                index = combo.current()
                pair = self.runtime.catalog[self.catalog_rows[index - 1]['id']] if index > 0 else None
                self.state.configure_role(role, pair)
            self.status.set('Development 模型角色已保存')
            window.destroy()
        ttk.Button(window, text='保存', command=save).pack(anchor='e', padx=12, pady=12)

    def agent_model_settings(self):
        if not self.runtime.ready:
            self.status.set('请先打开项目，读取已配置模型')
            return
        window = tk.Toplevel(self)
        window.title('子 Agent 独立模型')
        window.geometry('560x320')
        ttk.Label(window, text='配置实际运行名称，例如 forge/sub1；不创建第二套 Agent。\n'
                  '显式任务指定 > 独立配置 > 子 Agent 默认 > 继承父模型。', padding=12).pack(anchor='w')
        names = set(self.state.value('agent_models')) | set(self.task_evidence.agents)
        parent = self.runtime.bridge.agent.name
        names.discard(parent)
        names.add(parent + '/sub1')
        name = ttk.Combobox(window, values=sorted(names), width=44)
        name.pack(anchor='w', padx=12, pady=6)
        name.set(parent + '/sub1')
        model = ttk.Combobox(window, values=['继承角色', *[r['label'] for r in self.catalog_rows]],
                             state='readonly', width=44)
        model.pack(anchor='w', padx=12, pady=6)
        def show(event=None):
            pair = tuple(self.state.value('agent_models').get(name.get()) or ())
            model.current(next((i + 1 for i, row in enumerate(self.catalog_rows)
                                if self.runtime.catalog[row['id']] == pair), 0))
        name.bind('<<ComboboxSelected>>', show)
        name.bind('<FocusOut>', show)
        show()
        feedback = ttk.Label(window, text='', padding=12)
        feedback.pack(anchor='w')
        def save():
            try:
                index = model.current()
                pair = self.runtime.catalog[self.catalog_rows[index - 1]['id']] if index > 0 else None
                self.state.configure_agent_model(name.get().strip(), pair)
            except (ValueError, KeyError) as exc:
                feedback.configure(text=redact(str(exc)))
                return
            feedback.configure(text='已保存 · 当前任务不变，下次真实 spawn 时生效')
            self.status.set('子 Agent 独立模型已保存')
        ttk.Button(window, text='保存', command=save).pack(anchor='e', padx=12, pady=12)

    def checkpoints(self):
        row = self.current_row()
        if not row or not self.runtime.ready or self.runtime.busy or self._loading:
            self.status.set('打开目标文件，等待任务结束后审查检查点')
            return
        path = str(row['document'].path)
        def loaded(points, error):
            if error:
                return
            window = tk.Toplevel(self)
            window.title('检查点审查 · ' + Path(path).name)
            window.geometry('560x300')
            ttk.Label(window, text='仅恢复此文件；先查看差异，再确认。\n'
                      '保留工作区权限、Secret 和版本检查；写入使用原工具的系统换行。', padding=12).pack(anchor='w')
            combo = ttk.Combobox(window, values=[p['commit'][:8] + ' · ' + p['label'] for p in points],
                                  state='readonly', width=52)
            combo.pack(padx=12, pady=8, fill='x')
            if points:
                combo.current(0)
            else:
                ttk.Label(window, text='暂无真实写入检查点', padding=12).pack()
            reviewed = {'value': None}
            restore = ttk.Button(window, text='恢复已审查版本', state='disabled')
            def previewed(value, failure):
                if not window.winfo_exists():
                    return
                if failure:
                    return
                selected = combo.current()
                if selected < 0 or points[selected]['commit'] != value['commit']:
                    return
                reviewed['value'] = value
                self.write_output('Diff', value['diff'] or '此文件与检查点相同', clear=True)
                self.output.select(0)
                restore.configure(state='normal')
            def preview():
                if combo.current() >= 0:
                    restore.configure(state='disabled')
                    self.work(lambda commit=points[combo.current()]['commit']:
                              self.runtime.checkpoint_preview(commit, path), previewed)
            def selection_changed(event=None):
                reviewed['value'] = None
                restore.configure(state='disabled')
            combo.bind('<<ComboboxSelected>>', selection_changed)
            ttk.Button(window, text='查看差异', command=preview).pack(anchor='w', padx=12, pady=6)
            restore.configure(command=lambda: self.restore_reviewed_checkpoint(reviewed['value']))
            restore.pack(anchor='e', padx=12, pady=8)
        self.work(self.runtime.checkpoint_list, loaded)

    def restore_reviewed_checkpoint(self, preview):
        if not preview or self.runtime.busy or self._loading:
            return
        row = self.documents.get(preview['path'])
        if row:
            self.document_changed(row)
            if row['document'].dirty or row['saving']:
                self.status.set('目标标签有未保存编辑，请先保存或关闭草稿；回滚不会覆盖草稿')
                return
        if self.permission.get() != 'workspace-write':
            self.status.set('当前为只读；恢复文件需要先明确授权工作区写入')
            return
        if not messagebox.askyesno('恢复已审查版本', '仅将 ' + Path(preview['path']).name +
                ' 恢复到检查点 ' + preview['commit'][:8] + '？磁盘版本变化会拒绝写入。', parent=self):
            return
        permission = self.permission.get()
        def restored(value, error):
            if not error:
                if not value['ok']:
                    self.write_output('问题', str(value['error']) + '\n')
                self.status.set('已恢复审查版本' if value['ok'] else '恢复被拒绝，文件保留')
                self.refresh_project()
                self.refresh_documents()
        self.work(lambda: self.runtime.restore_checkpoint(preview['commit'], preview['path'],
                  preview['expected'], permission=permission), restored)

    def switch_mode(self):
        self.capture_layout()
        self.state.switch(self.mode.get())

    def _mode_changed(self, mode):
        self._permission = 'read-only'
        self.permission.set('read-only')
        self.mode.set(mode.value)
        self.apply_layout()

    def toggle_ide(self):
        self.capture_layout()
        self.state.set_layout(visible=not self.state.layout['visible'])
        self.apply_layout()

    def toggle_focus(self):
        self.state.set_layout(preset='focus', visible=True,
                              focus='agent' if self.state.layout['focus'] == 'editor' else 'editor')
        self.preset.set('focus')
        self.apply_layout()

    def change_preset(self, event=None):
        preset = self.preset.get()
        fractions = [.17, .49] if preset == 'agent' else [.17, .76]
        self.state.set_layout(preset=preset, fractions=fractions, visible=True)
        self.apply_layout()

    def apply_layout(self):
        if self._closed:
            return
        # 闪烁修复：增量挂载——只摘除应移除的面板、只挂载新增的面板。
        # 全量 forget+add 会让真机窗口整块重排（用户可见的闪白与
        # 下方白色框闪烁）；面板集合不变时保持挂载关系不动。
        layout = self.state.layout
        show = self.state.mode == WorkMode.DEVELOPMENT and layout['visible']
        if not show:
            panels = [self.agent_frame]
        elif layout['preset'] == 'focus':
            panels = [self.editor_frame if layout['focus'] == 'editor' else self.agent_frame]
        else:
            panels = [self.project_frame, self.editor_frame, self.agent_frame]
        wanted = list(panels)
        # 保序移除：只摘不在目标集合里的
        for pane in list(self.panes.panes()):
            if pane not in wanted:
                self.panes.forget(pane)
        # 增量挂载：逐个补挂 wanted 中缺失的面板。每挂一个都重读
        # 实际列表——insert 的 pos 按当前实际列表计位：目标位置 =
        # 实际列表中、wanted 里排在它前面的面板个数。
        for panel in wanted:
            if panel in self.panes.panes():
                continue
            idx = sum(1 for m in wanted[:wanted.index(panel)]
                      if m in self.panes.panes())
            live = self.panes.panes()
            if live:
                self.panes.insert(min(idx, len(live)), panel, weight=1)
            else:
                self.panes.add(panel, weight=1)
        self.later(0, self.place_sashes)

    def place_sashes(self):
        if self._closed or getattr(self, '_placing_sashes', False):
            return
        self._placing_sashes = True
        try:
            # 闪烁修复：sashpos 写入会触发 <Configure>→_panes_resized→
            # 再排 place_sashes 的自激环。写入期间挂旗标让 resized 忽略
            # 自触发事件；位置比较加 2px 容差（int 取整抖动不再构成
            # 「位置不同」的假差异，环在源头断开）。
            if len(self.panes.panes()) == 3:
                width = self.panes.winfo_width()
                for i, fraction in enumerate(self.state.layout['fractions']):
                    position = int(width * fraction)
                    if abs(self.panes.sashpos(i) - position) > 2:
                        self.panes.sashpos(i, position)
            if self.vertical.winfo_height() > 40:
                position = int(self.vertical.winfo_height() * self.state.layout['output'])
                if abs(self.vertical.sashpos(0) - position) > 2:
                    self.vertical.sashpos(0, position)
        finally:
            root = self.winfo_toplevel()
            root.after_idle(lambda: setattr(self, '_placing_sashes', False))

    def _panes_resized(self, event):
        # 闪烁修复：place_sashes 写 sashpos 期间触发的 Configure 是
        # 自激回声，不响应（旗标由 after_idle 复位）。
        if getattr(self, '_placing_sashes', False):
            return
        if event.width != self._last_pane_width:
            self._last_pane_width = event.width
            self.later(0, self.place_sashes)

    def capture_layout(self):
        changes = {}
        width, height = self.panes.winfo_width(), self.vertical.winfo_height()
        if len(self.panes.panes()) == 3 and width > 50:
            fractions = [self.panes.sashpos(i) / width for i in range(2)]
            if .05 <= fractions[0] < fractions[1] <= .95:
                changes['fractions'] = fractions
        if height > 50:
            changes['output'] = min(.9, max(.15, self.vertical.sashpos(0) / height))
        if changes:
            self.state.set_layout(**changes)

    def can_close(self):
        if self._loading or self._jobs:
            self.status.set('后台操作尚未完成，完成后可安全退出')
            return False
        if self.runtime.busy:
            messagebox.showinfo('任务运行中', '关闭会中断执行。请等待当前任务完成；切换模式仍可继续使用。', parent=self)
            return False
        if any(row['saving'] for row in self.documents.values()):
            return False
        if any(row['document'].dirty for row in self.documents.values()):
            return messagebox.askyesno('未保存内容', '退出将丢失未保存缓冲区。确认退出？', parent=self)
        return True

    def _destroyed(self, event):
        if event.widget is not self:
            return
        self._closed = True
        self._unsubscribe()
        for timer in tuple(self._after_ids):
            if timer:
                try:
                    self.after_cancel(timer)
                except tk.TclError:
                    pass
        self._after_ids.clear()
        if self._owns_runtime and not self.runtime.busy:
            self.runtime.close()
            self.runtime.emit = lambda event: None
        elif not self._owns_runtime:
            self.runtime.emit = self._previous_emit
        # Release host-owned Tcl variables on the Tk thread while its interpreter exists.
        for name in ('mode', 'preset', 'search', 'status', 'planning', 'permission', 'task_role', 'diff_view'):
            if hasattr(self, name):
                setattr(self, name, None)


def launch(home, project=None):
    state = ModeState(home)
    root = tk.Tk()
    root.title('Forge — Development Mode (isolated)')
    root.geometry('1440x900')
    root.minsize(760, 540)
    root.configure(bg=C['bg'])
    apply_ttk_theme(root)
    install_tk_exception_redaction(root)
    app = DevelopmentWorkbench(root, state)
    app.pack(fill='both', expand=True)
    def close():
        if app.can_close():
            app.capture_layout()
            root.destroy()
    root.protocol('WM_DELETE_WINDOW', close)
    path = project or state.value('project')
    if path and Path(path).is_dir():
        app.open_project(path)
    root.mainloop()
