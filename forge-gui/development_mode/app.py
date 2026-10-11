"""Development workbench UI. Only queued callbacks touch Tcl/Tk."""
from pathlib import Path
import json
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from gui_theme import C, apply_ttk_theme, style_editor, FONT_MONO_SM
from forge.development.state import ModeState, WorkMode
from forge.development.editor import Document
from forge.development.runtime import DevelopmentRuntime
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
        self._scan_busy = False
        self._scan_pending = False
        self.source = None
        self.documents = {}
        self._file_actions = {}
        self.expanded = set()
        self.model_ids = ['']
        self.catalog_rows = []
        self._permission = 'read-only'
        self._session_to_restore = self.state.value('session')
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
        ttk.Label(header, text='FORGE / Development', font=('Segoe UI', 13, 'bold')).pack(side='left')
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
        ttk.Button(options, text='专注：编辑器 / Agent', command=self.toggle_focus).pack(side='left', padx=4)
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
        ttk.Label(tools, text='项目', font=('Segoe UI', 11, 'bold')).pack(side='left')
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
        self.write_output('测试', '这里只展示真实工具事件，不推算通过数。命令审批仍由 Forge Policy 决定。\n')

    def _build_agent(self):
        self.task_label = ttk.Label(self.agent_frame, text='Agent · 未运行', padding=(8, 4), wraplength=320)
        self.task_label.pack(fill='x')
        log_tabs = ttk.Notebook(self.agent_frame)
        log_tabs.pack(fill='both', expand=True)
        dialogue = ttk.Frame(log_tabs)
        events = ttk.Frame(log_tabs)
        log_tabs.add(dialogue, text='对话')
        log_tabs.add(events, text='真实任务事件')
        self.chat = self._readonly_text(dialogue, wrap='word')
        self.events = self._readonly_text(events, wrap='word')
        self.editor_contexts = []
        self.context_label = ttk.Label(self.agent_frame, text='未附加编辑器内容', padding=4)
        self.context_label.pack(fill='x')
        self.input = tk.Text(self.agent_frame, height=5, wrap='word', undo=True, font=('Segoe UI', 11))
        style_editor(self.input)
        self.input.pack(fill='x', padx=6, pady=4)
        self.input.bind('<Control-Return>', lambda e: (self.send(), 'break')[-1])
        row = ttk.Frame(self.agent_frame)
        row.pack(fill='x', padx=6, pady=4)
        self.model_choice = ttk.Combobox(row, values=['继承配置'], state='readonly', width=24)
        self.model_choice.current(0)
        self.model_choice.bind('<<ComboboxSelected>>', lambda event: self.input.focus_set())
        self.model_choice.pack(side='left', fill='x', expand=True)
        self.send_button = ttk.Button(row, text='发送', command=self.send)
        self.send_button.pack(side='right', padx=(4, 0))
        row2 = ttk.Frame(self.agent_frame)
        row2.pack(fill='x', padx=6, pady=(0, 6))
        self.planning = tk.StringVar(value='low')
        ttk.Combobox(row2, textvariable=self.planning, values=['none', 'low', 'medium', 'high'], state='readonly', width=8).pack(side='left')
        self.permission = tk.StringVar(value='read-only')
        perms = ttk.Combobox(row2, textvariable=self.permission, values=['read-only', 'workspace-write'], state='readonly', width=16)
        perms.pack(side='left', padx=4)
        perms.bind('<<ComboboxSelected>>', self.permission_changed)
        ttk.Button(row2, text='复审', command=self.review).pack(side='right')
        ttk.Button(row2, text='恢复', command=self.restore_session).pack(side='right', padx=4)
        row3 = ttk.Frame(self.agent_frame)
        row3.pack(fill='x', padx=6, pady=(0, 6))
        ttk.Label(row3, text='任务角色').pack(side='left')
        self.task_role = tk.StringVar(value='default')
        ttk.Combobox(row3, textvariable=self.task_role, values=['default', 'complex', 'simple'],
                     state='readonly', width=10).pack(side='left', padx=4)
        self.run_model = ttk.Label(row3, text='模型：继承', wraplength=180)
        self.run_model.pack(side='left', fill='x', expand=True)

    def _readonly_text(self, parent, **kw):
        text = tk.Text(parent, font=('Segoe UI', 11), state='disabled', **kw)
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
            if old.ready and self._owns_runtime:
                runtime = DevelopmentRuntime(self.state, emit=self.enqueue_event)
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
        self.expanded = {str(self.source.root)}
        self._scan_busy = False
        self._external_busy = False
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
            for path, status in value['status'].items():
                self.changes.insert('', 'end', iid=path, text=status + ' ' + path)
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
        if selected:
            path = self.source.root / selected[0]
            source = self.source
            evidence = self._file_actions.get(str(path.resolve()))
            heading = ('真实操作来源 / 审批记录：\n' + json.dumps(evidence, ensure_ascii=False, indent=2)
                       if evidence else '尚无本会话 Agent 操作记录；Git 变更不自动归因于 Agent。') + '\n\n'
            self.work(lambda: source.diff(path), lambda value, error: self.write_output('Diff', heading + value, clear=True) if not error else None)
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

    def _check_external(self):
        if self._closed:
            return
        if self.documents and not self._external_busy:
            self._external_busy = True
            rows = [r for r in self.documents.values() if not r['saving']]
            def check():
                return [(str(r['document'].path), r['document'].check_external()) for r in rows]
            def checked(value, error):
                self._external_busy = False
                for key, changed in value or []:
                    if changed and key in self.documents:
                        self.update_tab(self.documents[key])
                        self.status.set('外部变更：' + Path(key).name + '；缓冲区保留，请比较或重新载入')
            self.work(check, checked)
        self._external_poll = self.later(3000, self._check_external)

    def attach_current(self):
        row = self.current_row()
        if row:
            self.document_changed(row)
            # Trusted bridge virtualizes before any provider payload is constructed.
            self.editor_contexts = [{'path': str(row['document'].path), 'content': row['document'].text}]
            self.context_label.configure(text='附加：' + row['document'].path.name + ' · 发送前保护 Secret')
            self.input.focus_set()

    def permission_changed(self, event=None):
        value = self.permission.get()
        if value == 'workspace-write' and not messagebox.askyesno('授权后续任务',
                '允许后续任务修改此工作区内文件？命令审批、插件权限及 Secret 边界仍保留。正在运行的任务不改变权限。', parent=self):
            self.permission.set(self._permission)
            return
        self._permission = value

    def send(self):
        prompt = self.input.get('1.0', 'end-1c').strip()
        if not prompt or not self.runtime.ready or self.runtime.busy or self._loading:
            self.status.set('请打开项目，或等待当前任务结束')
            return
        index = self.model_choice.current()
        model = self.runtime.catalog.get(self.model_ids[index]) if index > 0 else None
        permission, planning, contexts = self.permission.get(), self.planning.get(), list(self.editor_contexts)
        role = self.task_role.get()
        snapshot = self.state.task_snapshot(explicit_model=model, role=role, permission=permission)
        self.run_model.configure(text='运行模型：' + (' / '.join(snapshot.model) if snapshot.model else '全局路由'))
        self.send_button.configure(state='disabled')
        self.input.delete('1.0', 'end')
        self.editor_contexts = []
        self.context_label.configure(text='未附加编辑器内容')
        self.task_label.configure(text='Agent · 正在执行真实任务')
        def done(value, error):
            self.send_button.configure(state='normal')
            self.task_label.configure(text='Agent · 失败' if error else 'Agent · ' + str(value.get('stopped', '完成')))
            if error:
                if not self.input.get('1.0', 'end-1c').strip():
                    self.input.insert('1.0', prompt)
            elif value.get('text'):
                self.append(self.chat, '\nForge\n' + value['text'] + '\n')
            self.refresh_project()
        self.work(lambda: self.runtime.run(prompt, explicit_model=model, permission=permission,
                                          planning=planning, contexts=contexts, role=role, snapshot=snapshot), done)

    def handle_event(self, event, error=None):
        if event.get('event') == 'user':
            self.append(self.chat, '\n你\n' + event.get('display', '') + '\n')
            return
        data = event.get('data', event)
        self.append(self.events, json.dumps(data, ensure_ascii=False, indent=2) + '\n')
        kind = data.get('type', '')
        self.task_label.configure(text='Agent · ' + kind + ' · ' + str(data.get('tool', data.get('phase', ''))))
        if kind == 'tool_call':
            path = data.get('args', {}).get('path')
            if path and self.source:
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
            self.refresh_project()
        elif kind in {'model_error', 'review_error', 'subagent_error'}:
            self.write_output('问题', json.dumps(data, ensure_ascii=False) + '\n')

    def review(self):
        if self.runtime.busy or not self.runtime.ready or self._loading:
            self.status.set('请先完成任务')
            return
        self.work(self.runtime.review, lambda v, e: self.write_output('输出', json.dumps(v, ensure_ascii=False, indent=2) + '\n') if not e else None)

    def restore_session(self):
        if not self.runtime.ready or self.runtime.busy or self._loading:
            self.status.set('请先打开原项目，并等待任务结束')
            return
        path = filedialog.askopenfilename(parent=self, initialdir=self.state.home / 'sessions', filetypes=[('Forge Session', '*.jsonl')])
        if not path:
            return
        def restored(value, error):
            if not error:
                for message in value['history']:
                    self.append(self.chat, '\n' + message['role'] + '\n' + message['content'] + '\n')
                for event in value['events']:
                    self.append(self.events, json.dumps(event, ensure_ascii=False) + '\n')
                self.write_output('输出', json.dumps(value['workflow'], ensure_ascii=False, indent=2) + '\n')
                self.status.set('已恢复真实对话；未知操作不重放。请输入检查或继续任务。')
        self.work(lambda: self.runtime.restore_history(path), restored)

    def model_settings(self):
        window = tk.Toplevel(self)
        window.title('Development 模型角色')
        window.geometry('620x360')
        ttk.Label(window, text='仅选择已配置模型；任务运行时修改只影响后续任务。', padding=12).pack(anchor='w')
        choices = ['继承', *[r['label'] for r in self.catalog_rows]]
        labels = {'default': '默认编程', 'complex': '复杂任务', 'review': '代码复审', 'simple': '简单任务', 'subagent': '子 Agent（接口预留，未执行）'}
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

    def switch_mode(self):
        self.capture_layout()
        self.state.switch(self.mode.get())

    def _mode_changed(self, mode):
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
        for pane in list(self.panes.panes()):
            self.panes.forget(pane)
        layout = self.state.layout
        show = self.state.mode == WorkMode.DEVELOPMENT and layout['visible']
        if not show:
            panels = [self.agent_frame]
        elif layout['preset'] == 'focus':
            panels = [self.editor_frame if layout['focus'] == 'editor' else self.agent_frame]
        else:
            panels = [self.project_frame, self.editor_frame, self.agent_frame]
        for panel in panels:
            self.panes.add(panel, weight=1)
        self.later(0, self.place_sashes)

    def place_sashes(self):
        if self._closed:
            return
        if len(self.panes.panes()) == 3:
            width = self.panes.winfo_width()
            for i, fraction in enumerate(self.state.layout['fractions']):
                position = int(width * fraction)
                if self.panes.sashpos(i) != position:
                    self.panes.sashpos(i, position)
        if self.vertical.winfo_height() > 40:
            position = int(self.vertical.winfo_height() * self.state.layout['output'])
            if self.vertical.sashpos(0) != position:
                self.vertical.sashpos(0, position)

    def _panes_resized(self, event):
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
        for name in ('mode', 'preset', 'search', 'status', 'planning', 'permission', 'task_role'):
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
