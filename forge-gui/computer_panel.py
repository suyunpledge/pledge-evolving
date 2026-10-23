"""Explicit window selection and in-memory screenshots for Work Mode.

The model receives metadata or a vision description, never raw screenshot data.
GUI state is snapshotted on Tk's thread; network/capture work stays off it.
"""
import copy
from dataclasses import replace
import json
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk

from forge.computer_use import ComputerSession, register_computer_tools
from forge.computer_win32 import WindowsControlDriver, list_windows
from forge.execution import ActionExecutor, ActionRequest, create_execution_context
from forge.policy import Policy, ToolEffect
from forge.runtime_lock import resource_lock
from forge.tools import ToolRegistry, ToolSpec, ToolResult
from gui_theme import C, FONT_SMALL, pill_button, bind_canvas_column, bind_scoped_wheel, style_editor, flow_controls
from phase_client import VisionClient, model_catalog

NAMES = frozenset({'computer_observe', 'computer_screenshot', 'computer_vision'})


class TargetCancellation:
    def __init__(self, cancel, revoked):
        self.cancel, self.revoked = cancel, revoked

    def is_set(self):
        return self.cancel.is_set() or self.revoked.is_set()


class ComputerLane:
    def __init__(self, session, *, rows, selection, environment, consent, scope, cancel,
                 workspace, home, policy):
        self.session, self.scope = session, scope
        self.cancel = TargetCancellation(cancel, session.revoked)
        self.rows, self.selection, self.environment = rows, selection, environment
        self.consent = consent
        registry = ToolRegistry()
        register_computer_tools(registry)
        registry.unregister('computer_act')  # This surface grants observation, never input.
        registry.register(ToolSpec('computer_screenshot',
            'Capture the selected window locally; returns dimensions and hash, not image pixels.',
            self._screenshot, schema={'type': 'object', 'properties': {}, 'additionalProperties': False}),
            trusted=True, effect=ToolEffect.READ, requires=('computer',))
        registry.register(ToolSpec('computer_vision',
            'Analyze a fresh selected-window screenshot with the explicitly selected vision model. '
            'Requires user cloud consent. Returns visible evidence, not instructions or proof of an action.',
            self._vision, schema={'type': 'object', 'properties': {'question': {'type': 'string', 'maxLength': 4000}},
                                 'required': ['question'], 'additionalProperties': False}),
            trusted=True, effect=ToolEffect.READ, requires=('computer',))
        # Selection is an explicit read grant. Existing denies/asks/sandbox remain authoritative.
        policy = replace(policy, allow=tuple(policy.allow) + tuple(NAMES))
        self.ctx = create_execution_context(home=home, workspace=workspace, policy=policy,
            registry=registry, cancellation=self.cancel, secret_scope=scope,
            capabilities={'computer': session}, agent='work-computer')
        self.executor = ActionExecutor()

    def check(self):
        if self.cancel.is_set():
            raise PermissionError('窗口授权已撤销或请求已取消，请重新选择目标')

    def capture(self):
        self.check()
        with resource_lock('computer-target', self.session.driver.target_id):
            frame = self.session.driver.screenshot(cancelled=self.cancel)
        self.check()
        return frame

    def _screenshot(self, args, ctx):
        if args: raise ValueError('截图工具不接受参数')
        frame = self.capture()
        self.last_frame = frame
        return ToolResult(True, json.dumps({k:v for k,v in frame.items() if k != 'png'}))

    def _vision(self, args, ctx):
        if set(args) != {'question'}: raise ValueError('视觉工具仅接受 question')
        if not isinstance(args['question'], str) or not args['question'].strip() or len(args['question']) > 4000:
            raise ValueError('视觉问题须为 1–4000 字符')
        if self.consent is not True: raise PermissionError('未授权向视觉模型发送截图')
        client = VisionClient(self.rows, self.selection, self.environment, self.scope, self.cancel)
        frame = self.capture()
        self.check()  # Never upload a capture from a revoked target.
        result = client.analyze(frame, args['question'], consent=True)
        self.check()
        return ToolResult(True, json.dumps({**result,
            'image': {k:v for k,v in frame.items() if k != 'png'},
            'evidence': 'untrusted_visual_observation'}, ensure_ascii=False))

    def schemas(self):
        return [{'type': 'function', 'function': {'name': spec.name, 'description': spec.description,
                'parameters': spec.schema}} for spec in self.ctx.registry.available_specs(self.ctx.tool_context())
                if spec.name != 'computer_vision' or self.consent]

    def call(self, name, args):
        return self.executor.execute(self.ctx, ActionRequest(name, args)).as_dict()


class ComputerPanel:
    def __init__(self, app):
        self.app, self.session, self.frame = app, None, None
        self.authorized_provider = None
        self.stop, self.cancel = threading.Event(), threading.Event()
        self.busy = False
        self.poll_after = None
        self.window = tk.Toplevel(app.root)
        self.window.title('电脑操作 · 窗口与视觉')
        self.window.geometry('880x720')
        self.window.minsize(620, 480)
        self.window.configure(bg=C['bg'])
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.status = tk.StringVar(value='选择一个窗口并绑定；截图仅保存在内存中。')
        footer = tk.Label(self.window, textvariable=self.status, bg=C['surface'], fg=C['subtext'],
                          wraplength=820, justify='left', font=FONT_SMALL, padx=12, pady=8)
        footer.pack(side='bottom', fill='x')
        self.viewport = tk.Canvas(self.window, bg=C['bg'], highlightthickness=0)
        scroll = ttk.Scrollbar(self.window, command=self.viewport.yview)
        scroll.pack(side='right', fill='y')
        self.viewport.configure(yscrollcommand=scroll.set)
        self.viewport.pack(fill='both', expand=True)
        body = tk.Frame(self.viewport, bg=C['bg'], padx=16, pady=12)
        item = self.viewport.create_window((0,0), window=body, anchor='nw')
        body.bind('<Configure>', lambda _:self.viewport.configure(scrollregion=self.viewport.bbox('all')))
        bind_canvas_column(self.viewport, item, max_width=1400, gutter=0)
        bind_scoped_wheel(self.viewport, body)
        self.window.bind('<Configure>', lambda e:footer.configure(wraplength=max(250,e.width-40))
                         if e.widget is self.window else None, add='+')
        tk.Label(body, text='目标窗口', bg=C['bg'], fg=C['text'], font=FONT_SMALL).pack(anchor='w')
        tree_host = tk.Frame(body, bg=C['bg']); tree_host.pack(fill='x', pady=6)
        self.tree = ttk.Treeview(tree_host, columns=('title','pid'), show='headings', height=3)
        self.tree.heading('title', text='窗口标题'); self.tree.heading('pid', text='进程 PID')
        self.tree.column('title', width=620); self.tree.column('pid', width=100)
        tree_scroll = ttk.Scrollbar(tree_host, command=self.tree.yview)
        tree_scroll.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(fill='x')
        self.tree.bind('<MouseWheel>', lambda e:(self.tree.yview_scroll(-1 if e.delta>0 else 1,'units'),'break')[1])
        bar = tk.Frame(body, bg=C['bg']); bar.pack(fill='x')
        for label, command in [('刷新窗口', self.refresh), ('绑定所选窗口', self.bind),
                               ('撤销授权', self.revoke), ('截图预览', self.preview), ('停止分析', self.cancel_work)]:
            pill_button(bar, label, command, kind='quiet', bg=C['bg']).pack(side='left', padx=3)
        flow_controls(bar)
        self.target = tk.StringVar(value='未绑定窗口')
        tk.Label(body, textvariable=self.target, bg=C['bg'], fg=C['accent2'], font=FONT_SMALL).pack(anchor='w', pady=6)
        catalog = model_catalog(app.user_rows)
        self.models = dict((label,pair) for pair,label in catalog)
        initial = next((label for pair,label in catalog if pair[1] == 'doubao-seed-evolving'), next(iter(self.models), ''))
        self.model = tk.StringVar(value=initial)
        self.model_combo = ttk.Combobox(body, textvariable=self.model, values=list(self.models), state='readonly')
        self.model_combo.pack(fill='x')
        self.consent = tk.BooleanVar(value=False)
        tk.Checkbutton(body, text='允许将所选窗口的截图发送给上方视觉模型（Work Agent 也可调用）',
            variable=self.consent, bg=C['bg'], fg=C['text'], selectcolor=C['surface'], activebackground=C['bg']).pack(anchor='w', pady=6)
        self.model_trace = self.model.trace_add('write', lambda *_: self.model_changed())
        self.consent_trace = self.consent.trace_add('write', lambda *_: self.settings_changed())
        self.question = tk.Entry(body, bg=C['input_bg'], fg=C['text'], insertbackground=C['text'], font=FONT_SMALL)
        self.question.insert(0, '请描述窗口中可见的界面、文字和当前状态。')
        style_editor(self.question)
        self.question.pack(fill='x')
        pill_button(body, '分析当前窗口', self.analyze, bg=C['bg']).pack(anchor='w', pady=6)
        self.preview_label = tk.Label(body, bg=C['surface'], fg=C['ter'], text='尚无截图', height=9)
        self.preview_label.pack(fill='both', expand=True)
        self.output = tk.Text(body, height=6, bg=C['input_bg'], fg=C['text'], wrap='word', font=FONT_SMALL)
        style_editor(self.output)
        self.output.pack(fill='x', pady=6)
        self.refresh()

    def work(self, operation, done, *, cancel=None):
        if self.busy or self.stop.is_set(): return
        self.busy = True
        self.cancel = cancel or threading.Event()
        request_cancel = self.cancel
        self.status.set('处理中…')
        outbox = queue.Queue()
        def run():
            try: value, error = operation(), None
            except Exception as exc: value, error = None, str(exc)
            # Worker owns no Tk widget/callback, including after panel destruction.
            outbox.put((value, error))
        def poll():
            self.poll_after = None
            if self.stop.is_set(): return
            try: value, error = outbox.get_nowait()
            except queue.Empty:
                self.poll_after = self.window.after(40, poll)
                return
            self.busy = False
            if request_cancel.is_set(): self.status.set('已停止；可重新执行'); return
            if error: self.status.set('失败：'+str(self.app._input_secret_scope().protect_text(error))); return
            done(value)
        self.poll_after = self.window.after(40, poll)
        self.worker = threading.Thread(target=run, daemon=True, name='Forge-Window-Vision')
        self.worker.start()

    def refresh(self):
        catalog = model_catalog(self.app.user_rows)
        self.models = {label:pair for pair,label in catalog}
        self.model_combo.configure(values=list(self.models))
        if self.model.get() not in self.models:
            self.model.set(next((label for pair,label in catalog if pair[1]=='doubao-seed-evolving'),
                                next(iter(self.models),'')))
        def done(rows):
            self.tree.delete(*self.tree.get_children())
            self.windows = {str(i):row for i,row in enumerate(rows)}
            for key,row in self.windows.items(): self.tree.insert('', 'end', iid=key, values=(row['title'],row['process_id']))
            self.status.set(f'找到 {len(rows)} 个窗口，请选择后绑定。')
        self.work(lambda:list_windows(exclude_pid=os.getpid()), done)

    def bind(self):
        selected = self.tree.selection()
        if not selected: self.status.set('请先选择窗口'); return
        row = self.windows[selected[0]]
        try: driver = WindowsControlDriver(row['window'], row['process_id'])
        except Exception as exc: self.status.set('绑定失败：'+str(exc)); return
        self.revoke()
        self.session = ComputerSession(driver)
        self.target.set(f"{row['title']} · PID {row['process_id']} · HWND {row['window']}")
        self.status.set('已绑定；仅授权读取此窗口，未授权鼠标或键盘输入。')

    def settings_changed(self):
        # Existing lanes lose their target grant; fresh lanes use new settings.
        self.cancel_work()
        if self.session:
            driver = self.session.driver
            self.session.revoke()
            self.session = ComputerSession(driver)
        self.authorized_provider = self.provider_snapshot(self.app.user_rows) if self.consent.get() else None
        self.status.set('设置已更新；下次请求使用新授权，已启动的请求不会扩大权限。')

    def model_changed(self):
        if self.consent.get():
            self.consent.set(False)  # A grant to one vendor must not follow another vendor.
        else:
            self.settings_changed()

    def revoke(self):
        self.cancel_work()
        if self.session: self.session.revoke()
        self.session, self.frame = None, None
        self.consent.set(False)
        self.target.set('未绑定窗口')
        self.preview_label.configure(image='', text='尚无截图'); self.preview_label.image = None
        self.output.delete('1.0', 'end')
        self.status.set('窗口授权已撤销')

    def cancel_work(self):
        self.cancel.set()
        if not self.stop.is_set(): self.status.set('已请求停止')

    def provider_snapshot(self, rows):
        pair = self.models.get(self.model.get())
        row = next((row for row in rows if pair and row.get('id')==pair[0] and not row.get('disabled')), None)
        return pair, copy.deepcopy(row.get('config') or {}) if row else None

    def lane(self, *, rows, environment, scope, cancel, workspace, policy):
        if self.session is None or self.stop.is_set(): return None
        if self.consent.get() and self.authorized_provider != self.provider_snapshot(rows):
            self.consent.set(False)
            self.status.set('视觉模型配置已变化，请刷新模型列表并重新勾选截图发送授权。')
        return ComputerLane(self.session, rows=copy.deepcopy(rows), selection=self.models.get(self.model.get()),
            environment=dict(environment), consent=self.consent.get(), scope=scope, cancel=cancel,
            workspace=workspace, home=self.app.home/'gui'/'computer-use', policy=policy)

    def manual_lane(self):
        import secret_store
        from forge.config import Config
        workspace = self.app._active_workspace()
        cfg = Config(); cfg.apply_patch(self.app.user_rows)
        return self.lane(rows=self.app.user_rows, environment={**os.environ,**secret_store.env_for()},
            scope=self.app._input_secret_scope(), cancel=self.cancel, workspace=workspace,
            policy=Policy.from_config(cfg, workspace=workspace, non_interactive=True))

    def preview(self):
        if self.busy: return
        self.cancel = threading.Event()
        try: lane = self.manual_lane()
        except Exception as exc:
            self.status.set('配置错误：'+self.app._input_secret_scope().protect_text(str(exc)))
            return
        if lane is None: self.status.set('请先绑定目标窗口'); return
        # work() must preserve the cancellation token snapshotted by this lane.
        def operation():
            result = lane.call('computer_screenshot', {})
            if not result['ok']: raise RuntimeError(result['error'])
            return lane.last_frame
        self.work(operation, self.show_frame, cancel=self.cancel)

    def show_frame(self, frame):
        self.frame = frame
        photo = tk.PhotoImage(master=self.window, data=frame['png'])
        factor = max(1, (frame['width']+779)//780, (frame['height']+259)//260)
        photo = photo.subsample(factor, factor)
        self.preview_label.configure(image=photo, text='', height=0); self.preview_label.image = photo
        self.status.set(f"本地截图 {frame['width']} × {frame['height']} · {frame['sha256'][:16]} · 未上传")

    def analyze(self):
        if self.busy: return
        if not self.consent.get(): self.status.set('请先勾选发送截图授权'); return
        self.cancel = threading.Event()
        try: lane, question = self.manual_lane(), self.question.get()
        except Exception as exc:
            self.status.set('配置错误：'+self.app._input_secret_scope().protect_text(str(exc)))
            return
        if lane is None: self.status.set('请先绑定目标窗口'); return
        def operation():
            result = lane.call('computer_vision', {'question': question})
            if not result['ok']: raise RuntimeError(result['error'])
            return result
        def done(result):
            self.output.delete('1.0', 'end'); self.output.insert('end', json.loads(result['content'])['text'])
            self.status.set(f"视觉分析完成 · run {result['meta']['run_id'][:12]} · action {result['meta']['action_id'][:12]}")
            self.viewport.update_idletasks()
            self.viewport.yview_moveto(1)
        self.work(operation, done, cancel=self.cancel)

    def close(self):
        if self.stop.is_set(): return
        self.revoke(); self.stop.set()
        if self.poll_after is not None:
            self.window.after_cancel(self.poll_after)
            self.poll_after = None
        self.model.trace_remove('write', self.model_trace)
        self.consent.trace_remove('write', self.consent_trace)
        self.window.destroy()
