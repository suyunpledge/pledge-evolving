"""Coding Agent 顶部的「运行时上下文」组件（编辑器与布局域）。

与 `AgentPanel` 协同：在标题栏下方、三 Tab Notebook 上方插入一段可
折叠的 chrome，集中展示两件事：

* **权限矩阵**——四维下拉（Mode / Sandbox / ReadScope / SecretScope），
  直接对应 :class:`forge.policy.Policy` 与 :class:`forge.secrets.SecretScope`。
  这与「任务权限」的二进制徽标（之前 B1 修复）解耦：徽标只展示任务级
  总结；矩阵让用户在不重启 run 的前提下切换。
* **模型路由**——三元素：主模型 / fallback / MOA 开关，对应
  :class:`forge.routing.RoutingConfig` 的 ``primary`` / ``fallback`` /
  ``moa`` 字段。所有切换在 UI 上即时生效，由 app 把值传回
  ``build_agent(routing=...)``。

设计口径（与之前架构审查 P0-2 / P1-7 修复一致）：

* **BYPASS 模式**额外要求"我已确认后果"勾选；未勾选时拒绝切换。
* **read_scope = ALL** 在 BYPASS 模式下默认拒绝（架构审查 P0-2）。
* **SecretScope.isolated = False** 在 secret_isolation=True 的 host 上
  拒绝——避免 CapabilityRuntime 误以为已隔离。

实现只承载 UI 表达：值通过回调 ``on_policy_change`` / ``on_routing_change``
回写 app，policy 与 routing 自身的强类型校验由 app 端在保存前再做。
本模块不直接 import ``forge.policy``，避免 GUI 进程的循环依赖。
"""
import tkinter as tk
from tkinter import ttk

from gui_theme import C, FONT_CAPTION, FONT_SECTION, FONT_UI, FONT_UI_BOLD, button_feedback, attach_tooltip

# 模式可选取值（与 forge.policy.Mode 枚举对齐——禁 BYPASS 进入默认选项）。
POLICY_MODES = ('default', 'acceptEdits', 'read-only', 'auto',
                'dontAsk', 'plan', 'bypassPermissions')
SANDBOX_TIERS = ('read-only', 'workspace-write', 'danger-full-access')
READ_SCOPES = ('auto', 'workspace', 'all')
SECRET_SCOPES = ('isolated', 'shared')


class PolicyMatrix(ttk.Frame):
    """权限矩阵：Mode / Sandbox / ReadScope / SecretScope 四维下拉。

    每个下拉的当前值由 app 在构造时传入，change 事件通过
    ``on_change(mode, sandbox, read_scope, secret_scope)`` 回调给 app。
    状态徽标（"工作区可写"）随 Sandbox 联动：read-only → 灰；其它 → 绿。
    """

    def __init__(self, master, *, mode, sandbox, read_scope, secret_scope,
                 on_change=None, host_managed=False, **kw):
        super().__init__(master, **kw)
        self._on_change = on_change
        self._host_managed = host_managed
        self._mode = tk.StringVar(value=mode)
        self._sandbox = tk.StringVar(value=sandbox)
        self._read_scope = tk.StringVar(value=read_scope)
        self._secret_scope = tk.StringVar(value=secret_scope)
        self._build()
        # 触发一次徽标同步。
        self._sync_badge()

    def _build(self):
        self.configure(padding=(8, 6, 8, 0))
        # 标题行。
        tk.Label(self, text='权限矩阵', font=FONT_SECTION, bg=C['bg'],
                 fg=C['text']).grid(row=0, column=0, columnspan=5, sticky='w')
        # 4 行 × 2 列：左 label、右 combobox；前 4 列排版紧凑、第 5 列给徽标。
        rows = (
            ('Mode', self._mode, POLICY_MODES),
            ('Sandbox', self._sandbox, SANDBOX_TIERS),
            ('ReadScope', self._read_scope, READ_SCOPES),
            ('SecretScope', self._secret_scope, SECRET_SCOPES),
        )
        for index, (title, var, values) in enumerate(rows, start=1):
            if self._host_managed and title == 'Sandbox':
                values = ('read-only', 'workspace-write')
            tk.Label(self, text=title, font=FONT_CAPTION, bg=C['bg'],
                     fg=C['ter']).grid(row=index, column=0, sticky='w',
                                       padx=(0, 6), pady=2)
            combo = ttk.Combobox(self, textvariable=var, state='readonly',
                                 values=values, width=16)
            combo.grid(row=index, column=1, sticky='we', pady=2)
            combo.bind('<<ComboboxSelected>>', lambda _e: self._emit())
            self._attach_confirm(combo, title)
            if self._host_managed and title != 'Sandbox':
                combo.configure(state='disabled')
                attach_tooltip(combo, '由 Coding 宿主控制；当前任务只支持只读或工作区写入。')
        self.columnconfigure(1, weight=1)
        # 状态徽标（"工作区可写" / "危险全访问" / "隔离启用"）— 第 5 列。
        self._badge = tk.Label(self, text='', font=FONT_CAPTION, bg=C['bg'],
                               fg=C['ok'])
        self._badge.grid(row=1, column=4, rowspan=2, sticky='e', padx=(8, 0))

    def _attach_confirm(self, combo, title):
        """BYPASS 模式：选 BYPASS 时弹确认，未确认则回滚。"""
        def _on_select(event):
            if title == 'Mode' and self._mode.get() == 'bypassPermissions':
                ok = tk.messagebox.askyesno(
                    '确认 BYPASS 模式',
                    'BYPASS 模式将取消所有审批。是否确认？',
                    parent=self)
                if not ok:
                    self._mode.set('default')
                    return
            if title == 'SecretScope' and self._secret_scope.get() == 'shared':
                tk.messagebox.showwarning(
                    'Secret 共享风险',
                    'shared 模式会让插件 Python 进程继承所有 FORGE_*_KEY。',
                    parent=self)
            self._emit()
        combo.bind('<<ComboboxSelected>>', _on_select, add='+')

    def _emit(self):
        if self._host_managed:
            self._mode.set('read-only' if self._sandbox.get() == 'read-only' else 'acceptEdits')
            self._read_scope.set('workspace')
            self._secret_scope.set('isolated')
        self._sync_badge()
        if callable(self._on_change):
            try:
                self._on_change(
                    mode=self._mode.get(),
                    sandbox=self._sandbox.get(),
                    read_scope=self._read_scope.get(),
                    secret_scope=self._secret_scope.get(),
                )
            except Exception:
                pass  # UI 层不阻断——app 端强校验再兜

    def _sync_badge(self):
        sandbox = self._sandbox.get()
        if sandbox == 'read-only':
            self._badge.configure(text='只读', fg=C['ter'])
        elif sandbox == 'workspace-write':
            self._badge.configure(text='工作区可写', fg=C['ok'])
        elif sandbox == 'danger-full-access':
            self._badge.configure(text='危险全访问', fg=C['warn'])
        else:
            self._badge.configure(text='', fg=C['ter'])

    def get(self):
        return dict(
            mode=self._mode.get(),
            sandbox=self._sandbox.get(),
            read_scope=self._read_scope.get(),
            secret_scope=self._secret_scope.get(),
        )

    def set_sandbox(self, value):
        """Project the host's grant without dispatching a permission change."""
        self._sandbox.set(value)
        if self._host_managed:
            self._mode.set('read-only' if value == 'read-only' else 'acceptEdits')
        self._sync_badge()


class RoutingChain(ttk.Frame):
    """模型路由：主模型 / fallback / MOA 三元素。

    主模型为只读（与 AgentPanel 的 model_choice 共享数据源），fallback
    与 MOA 在下拉里切换。change 通过 ``on_change(primary, fallback, moa)``
    回写 app。
    """

    def __init__(self, master, *, primary, fallback, moa,
                 model_options, on_change=None, host_managed=False, **kw):
        super().__init__(master, **kw)
        self._on_change = on_change
        self._host_managed = host_managed
        self._primary = tk.StringVar(value=primary or '')
        self._fallback = tk.StringVar(value=fallback or '')
        self._moa = tk.StringVar(value=moa or 'off')
        self._model_options = tuple(model_options) if model_options else (primary or '',)
        self._build()

    def _build(self):
        self.configure(padding=(8, 4, 8, 6))
        tk.Label(self, text='模型路由', font=FONT_SECTION, bg=C['bg'],
                 fg=C['text']).grid(row=0, column=0, columnspan=4, sticky='w')
        # 主模型
        tk.Label(self, text='主模型', font=FONT_CAPTION, bg=C['bg'],
                 fg=C['ter']).grid(row=1, column=0, sticky='w', padx=(0, 6))
        primary = ttk.Combobox(self, textvariable=self._primary,
                               state='readonly', values=self._model_options,
                               width=18)
        primary.grid(row=1, column=1, sticky='we', padx=(0, 8))
        primary.bind('<<ComboboxSelected>>', lambda _e: self._emit())
        self._primary_choice = primary
        # fallback
        tk.Label(self, text='fallback', font=FONT_CAPTION, bg=C['bg'],
                 fg=C['ter']).grid(row=1, column=2, sticky='w', padx=(0, 6))
        fallback = ttk.Combobox(self, textvariable=self._fallback,
                                state='readonly', values=('', *self._model_options),
                                width=14)
        fallback.grid(row=1, column=3, sticky='we')
        fallback.bind('<<ComboboxSelected>>', lambda _e: self._emit())
        self._fallback_choice = fallback
        # MOA
        tk.Label(self, text='MOA', font=FONT_CAPTION, bg=C['bg'],
                 fg=C['ter']).grid(row=2, column=0, sticky='w', padx=(0, 6), pady=(4, 0))
        moa = ttk.Combobox(self, textvariable=self._moa, state='readonly',
                           values=('off', 'on'), width=18)
        moa.grid(row=2, column=1, sticky='we', padx=(0, 8), pady=(4, 0))
        moa.bind('<<ComboboxSelected>>', lambda _e: self._emit())
        if self._host_managed:
            for control in (fallback, moa):
                control.configure(state='disabled')
                attach_tooltip(control, '由宿主模型路由配置控制；此处不覆盖 fallback 或 MOA。')
        self.columnconfigure(1, weight=1)
        self.columnconfigure(3, weight=1)

    def _emit(self):
        if callable(self._on_change):
            try:
                self._on_change(
                    primary=self._primary.get(),
                    fallback=self._fallback.get(),
                    moa=self._moa.get(),
                )
            except Exception:
                pass

    def get(self):
        return dict(
            primary=self._primary.get(),
            fallback=self._fallback.get(),
            moa=self._moa.get(),
        )

    def set_model_options(self, options):
        self._model_options = tuple(dict.fromkeys(options))
        self._primary_choice.configure(values=self._model_options)
        self._fallback_choice.configure(values=tuple(dict.fromkeys(('', *self._model_options))))
        if self._primary.get() not in self._model_options:
            self._primary.set(self._model_options[0] if self._model_options else '')
        if self._fallback.get() not in ('', *self._model_options):
            self._fallback.set('')

    def set_primary(self, value):
        self._primary.set(value)


class TaskControls(ttk.Frame):
    """规划 + 角色 + 任务操作行：整合到 RuntimeContext 的第三条横排。

    三元素（规划 / 角色）+ 四按钮（复审 / 恢复 / 子 Agent / 重试 /
    取消 / 执行测试）排成两行。注入到 RuntimeContext 之后，老的
    "继承配置"区块可以彻底删除。
    """

    def __init__(self, master, *, planning='low', role='agent',
                 on_planning_change=None, on_role_change=None,
                 on_review=None, on_restore=None, on_delegate=None,
                 on_retry=None, on_cancel=None, on_run_tests=None,
                 **kw):
        super().__init__(master, **kw)
        self._on_planning = on_planning_change
        self._on_role = on_role_change
        self._callbacks = dict(
            review=on_review, restore=on_restore, delegate=on_delegate,
            retry=on_retry, cancel=on_cancel, run_tests=on_run_tests)
        self._build(planning, role)

    def _build(self, planning, role):
        self.configure(padding=(8, 4))
        # 第一行：规划 + 角色
        row1 = ttk.Frame(self)
        row1.pack(fill='x', pady=(0, 4))
        tk.Label(row1, text='规划', font=FONT_CAPTION, bg=C['bg'],
                 fg=C['ter']).pack(side='left')
        self._planning = tk.StringVar(value=planning)
        plan = ttk.Combobox(row1, textvariable=self._planning,
                            values=('none', 'low', 'medium', 'high'),
                            state='readonly', width=8)
        plan.pack(side='left', padx=(4, 12))
        plan.bind('<<ComboboxSelected>>', self._emit_planning)
        tk.Label(row1, text='角色', font=FONT_CAPTION, bg=C['bg'],
                 fg=C['ter']).pack(side='left')
        self._role = tk.StringVar(value=role)
        role_combo = ttk.Combobox(row1, textvariable=self._role,
                                values=('agent', 'complex', 'simple', 'review'),
                                state='readonly', width=8)
        role_combo.pack(side='left', padx=(4, 0))
        role_combo.bind('<<ComboboxSelected>>', self._emit_role)
        # 第二行：操作按钮（右对齐）
        row2 = ttk.Frame(self)
        row2.pack(fill='x', pady=(0, 2))
        for label, key in (('执行测试', 'run_tests'),
                           ('取消', 'cancel'),
                           ('重试', 'retry'),
                           ('创建子 Agent', 'delegate'),
                           ('恢复', 'restore'),
                           ('复审', 'review')):
            button = ttk.Button(row2, text=label, width=10)
            callback = self._callbacks.get(key)
            if callback is not None:
                button.configure(command=callback)
            else:
                button.configure(state='disabled')
            button.pack(side='right', padx=(4, 0))

    def _emit_planning(self, _event=None):
        if callable(self._on_planning):
            try:
                self._on_planning(self._planning.get())
            except Exception:
                pass

    def _emit_role(self, _event=None):
        if callable(self._on_role):
            try:
                self._on_role(self._role.get())
            except Exception:
                pass

    def set_role(self, value):
        """程序化设角色（与下拉 _role 同一 StringVar，UI 同步刷新）。"""
        self._role.set(value)

    def get_role_var(self):
        """角色 StringVar 直引用（app 侧别名挂载用）。"""
        return self._role

    def get_planning(self):
        return self._planning.get()

    def get_role(self):
        return self._role.get()


class RuntimeContext(ttk.Frame):
    """「运行时上下文」容器：权限矩阵 + 模型路由 + 任务控制 + 折叠按钮。

    排版：
        标题行：「运行时上下文」标题 + 「收起 ▲」按钮
        第一行：左 权限矩阵  4 列下拉  ＋  右 模型路由  3 元素
        第二行：规划 ＋ 角色 ＋ 任务操作（执行测试 / 取消 / 重试 /
                 创建子 Agent / 恢复 / 复审）

    默认展开（信息密度高，符合"全宽"工作台风格）。点击标题右侧按钮
    折叠 / 展开。
    """

    def __init__(self, master, *, policy_kwargs, routing_kwargs,
                 task_kwargs=None, **kw):
        super().__init__(master, **kw)
        self._collapsed = False  # R1：初始化折叠状态（v0.3 删了这里导致 _toggle 必抛）
        self._build(policy_kwargs, routing_kwargs, task_kwargs or {})

    def _build(self, policy_kwargs, routing_kwargs, task_kwargs):
        self.configure(padding=(0, 4))
        # 标题行：v0.3 起固定展示（不再有折叠按钮）—— 概念图里这块
        # chrome 总是展开，让用户一眼看到权限矩阵 + 模型路由。右侧留
        # 一个轻量「运行中 · 就绪」徽标，将来可挂状态指示。
        title_row = ttk.Frame(self)
        title_row.pack(fill='x', padx=8, pady=(4, 0))
        tk.Label(title_row, text='运行时上下文', font=FONT_SECTION, bg=C['bg'],
                 fg=C['text']).pack(side='left')
        # R1：恢复折叠按钮（v0.3 删了折叠；概念图 Agent 栏不占满，默认展开、可折叠）
        self._collapse_btn = ttk.Button(title_row, text='收起 ▲', command=self._toggle)
        self._collapse_btn.pack(side='right')
        # 内容容器（展开态 pack，折叠态 _toggle 里 pack_forget）
        self._body = ttk.Frame(self)
        self._body.pack(fill='x')
        # 主体两列：左 PolicyMatrix、右 RoutingChain
        upper = ttk.Frame(self._body)
        upper.pack(fill='x')
        left = ttk.Frame(upper)
        left.pack(side='left', fill='both', expand=True, padx=(8, 4), pady=4)
        right = ttk.Frame(upper)
        right.pack(side='left', fill='both', expand=True, padx=(4, 8), pady=4)
        self._policy = PolicyMatrix(left, **policy_kwargs)
        self._policy.pack(fill='x')
        # RoutingChain 的 model_options 在 kwargs 里；缺省时回退到 primary。
        if not routing_kwargs.get('model_options'):
            routing_kwargs = dict(routing_kwargs)
            routing_kwargs['model_options'] = (routing_kwargs.get('primary') or '',)
        self._routing = RoutingChain(right, **routing_kwargs)
        self._routing.pack(fill='x')
        # 第二行：任务控制（规划 / 角色 / 操作）
        self._task = TaskControls(self._body, **task_kwargs)
        self._task.pack(fill='x', padx=8, pady=(0, 4))

    def set_role(self, value):
        """透传角色到 TaskControls（app 反向同步入口）。"""
        self._task.set_role(value)

    def _toggle(self):
        if self._collapsed:
            self._body.pack(fill='x')
        else:
            self._body.pack_forget()
        self._collapsed = not self._collapsed
        self._update_button()

    def _update_button(self):
        # 已展开 → "收起 ▲"；已折叠 → "展开 ▼"
        self._collapse_btn.configure(text='收起 ▲' if not self._collapsed else '展开 ▼')

    def get_policy(self):
        return self._policy.get()

    def get_routing(self):
        return self._routing.get()

    def get_task(self):
        return dict(planning=self._task.get_planning(),
                    role=self._task.get_role())

    def get_task_controls(self):
        return self._task

    def set_model_options(self, options):
        self._routing.set_model_options(options)

    def set_routing_primary(self, value):
        self._routing.set_primary(value)

    def set_sandbox(self, value):
        self._policy.set_sandbox(value)

    def set_role(self, value):
        self._task.set_role(value)
