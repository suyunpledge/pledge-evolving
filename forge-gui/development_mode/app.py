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

from gui_theme import (C, apply_ttk_theme, attach_tooltip, style_editor,
    FONT_CAPTION, FONT_MONO_SM, FONT_TITLE, FONT_SECTION, FONT_UI, FONT_UI_MED,
    setup_code_tags, highlight_python)
from ui_icons import IconButton
try:  # E1（UX 审查）：与 InputCard 同款可选行内 IME 守卫（默认关）
    from ime_inline import InlineIME
except Exception:  # pragma: no cover - 非 Windows / 缺 ctypes
    InlineIME = None
from forge.development.state import ModeState, WorkMode
from forge.development.editor import Document
from forge.development.runtime import DevelopmentRuntime
from forge.development.evidence import TaskEvidence
from forge.secrets import redact, install_tk_exception_redaction
from .bottom_panel import ToolPanel, count_problems, count_tests, highlight_diff_lines
from .editor_area import EditorArea, save_document_as
from .explorer import ExplorerPanel
from .layout import NavigationBar, StatusBar, PRESETS
from .welcome import WelcomeView
from .project import WorkspaceSource

# M7：状态栏「语言」字段的扩展名映射（编辑器当前文档 → 语言名）。
# 只列本项目真实会出现的类型；未命中显示 Plain（不猜）。
_LANGUAGE_BY_SUFFIX = {
    '.py': 'Python', '.pyw': 'Python', '.md': 'Markdown',
    '.json': 'JSON', '.toml': 'TOML', '.yaml': 'YAML', '.yml': 'YAML',
    '.txt': 'Plain Text', '.cfg': 'INI', '.ini': 'INI', '.sh': 'Shell',
    '.js': 'JavaScript', '.ts': 'TypeScript', '.html': 'HTML',
    '.css': 'CSS', '.xml': 'XML', '.csv': 'CSV',
}


class DevelopmentWorkbench(ttk.Frame):
    def __init__(self, parent, state, *, runtime=None, hosted=False,
                 workspace_request=None):
        super().__init__(parent)
        self.state = state
        # hosted（2026-10-16 双导航栏去重，10-17 修正）：被 forge_gui_v2
        # 宿主内嵌时为 True——宿主顶栏在 Coding 下整条收起，本工作台导航栏
        # 成为唯一一行导航（品牌 + Work/Coding + 动作项全保留）；Work/Coding
        # 点击经 workspace_request 回调转发宿主顶层工作空间切换。独立运行
        # （run_development.py / 测试）保持完整形态与既有语义。
        self._hosted = bool(hosted)
        self._workspace_request = workspace_request
        self.runtime = runtime or DevelopmentRuntime(state, emit=self.enqueue_event)
        self._owns_runtime = runtime is None
        self._previous_emit = self.runtime.emit
        self.runtime.emit = self.enqueue_event if self._owns_runtime else self._forward_event
        self.results = queue.Queue()
        self._closed = False
        self._after_ids = set()
        self._loading = False
        self._jobs = 0
        self._submission_active = False
        self._active_submission = None
        self._queued_send_id = None
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
        self._review_request = 0
        self._compare_path = None
        self.expanded = set()
        self.model_ids = ['']
        self.catalog_rows = []
        self._permission = 'read-only'
        self._session_to_restore = self.state.value('session')
        self._resume_project = self.state.value('project')
        self._build()
        self._status_notice_at = float('-inf')
        self._status_background_write = False
        self.status.trace_add('write', self._status_written)
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
        # M1：顶部导航栏（FORGE / Work / Coding / Project / Run / Settings）。
        # 品牌字从 header 上移到导航栏；header 只留模式下拉与操作按钮。
        # 导航项回调走 _nav_navigate（本文件新增方法），布局组件本体在
        # layout.py——app.py 这里只做挂载，不实现导航栏内部结构。
        # hosted 形态：Work/Coding 点击经 workspace_request 转发宿主切换。
        # 2026-10-17 导航统一：Coding 嵌入宿主时不再绘制自己的顶栏——
        # 宿主顶栏（forge_gui_v2._build_topbar）是唯一一行顶导航。
        # self.nav 仍保留为属性（forward_event / set_active_mode 等需要
        # 引用 nav 实例做协调），但不 pack。
        self.nav = NavigationBar(self, on_navigate=self._nav_navigate,
                                 hosted=self._hosted,
                                 workspace_request=self._workspace_request)
        if not self._hosted:
            self.nav.pack(fill='x')
        else:
            # 嵌入宿主时：内部 Work/Coding 切换走 _workspace_request 回调
            # 宿主层；高亮由 forge_gui_v2._apply_workspace_chrome 同步
            # （本类 _build 内不再 bind_mode / pack_forget）。
            self.nav.set_active_mode('development')
        # R2：header 精简——品牌/模式/项目行全部并入 NavigationBar，header 零控件挂载
        # NavigationBar 已含：品牌火花+FORGE+Work/Coding+Project/Run/Settings
        # 模式切换走 nav.bind_mode + _nav_navigate；项目操作走 _build_project 内绑定
        # self.mode / self.preset 仍保留（向下兼容）
        self.mode = tk.StringVar(value=self.state.mode.value)
        if self._hosted and self._workspace_request is not None:
            # hosted：高亮不跟随 ModeState（那是独立运行语义），由宿主
            # 经 set_active_mode 直设（见 forge_gui_v2 的 chrome 应用）。
            pass
        else:
            self.nav.bind_mode(self.mode)
        # M7：头部「在线」灰点状态迁入导航栏右端（R2 头部精简后原
        # header 行删除；数据源纪律不变——runtime 无 peers 数据，
        # 固定「离线」不伪造计数，_sync_online_status 一处可控）。
        # hosted 形态不建（宿主侧有真实的 gateway 状态展示，静态占位点
        # 反而是噪音）；_sync_online_status 用 getattr 守卫，缺属性安全。
        if not self._hosted:
            online = tk.Frame(self.nav, bg=C['bg'])
            online.pack(side='right', padx=(0, 4))
            self.online_dot = tk.Canvas(online, width=8, height=8, bg=C['bg'],
                                        bd=0, highlightthickness=0)
            self.online_dot.pack(side='left', padx=(0, 4))
            self.online_dot.create_oval(1, 1, 7, 7, outline='', fill=C['ter'],
                                        tags='dot')
            self.online_var = tk.StringVar(value='离线')
            tk.Label(online, textvariable=self.online_var, font=FONT_CAPTION,
                     bg=C['bg'], fg=C['subtext']).pack(side='left')
            attach_tooltip(online, '在线状态：当前无在线连接数据源')
        self.preset = tk.StringVar(value=self.state.layout['preset'])
        # status_bar / vertical / panes 从原 L160+ 位置顺延——修改后从原 L156 起
        # 语义重叠（两个入口做同一件事）。toggle_focus() 保留（测试契约与
        # 状态机不变），focus 预设内可用 toggle_focus 的状态字段切换，或
        # 再次选择预设复位。
        # M1：外层竖向 Panedwindow——三栏 panes 与全宽底部工具面板并列，
        # 底部面板因此跨 project/editor/agent 三栏全宽（原输出 Notebook
        # 只占中央栏）。sash 比例仍走 state.layout['output']，语义变为
        # 「上部区域占比」，旧持久化值（默认 .72）直接兼容。
        # 注意：这仍是同一条挂载路径——三栏的增量挂载逻辑在 apply_layout
        # 内原样保留，底部面板只按 IDE 展开/收起做在装/不在装二态切换
        # （_sync_bottom_panel），不新增第二条增量挂载路径。
        self.status = tk.StringVar(value='未连接 · 只读 · 无执行任务')
        # M1：多字段状态栏（Git/Agent/Model/Project）+ 右侧瞬态消息。
        # self.status 的语义与文案零变化，只是从独占一行变为右侧一条。
        # 入 Packing 序列必须早于竖向 pane：Tk 在总自然高度超限时从序列
        # 末尾开始牺牲 Slave——状态栏若垫后会被压成 1px 挤出视线（实测
        # 1200x760），提前入队则让位的是 expand 的竖向 pane（其内部再由
        # log_tabs 次级让位），chrome 永不被挤掉。
        self.status_bar = StatusBar(self, status_var=self.status)
        self.status_bar.pack(side='bottom', fill='x')
        self.vertical = ttk.Panedwindow(self, orient='vertical')
        self.vertical.pack(fill='both', expand=True, padx=8)
        self.panes = ttk.Panedwindow(self.vertical, orient='horizontal')
        self.vertical.add(self.panes, weight=4)
        # M5：底部面板四标签 + 真实计数徽标。徽标数据源经 provider
        # 注入（面板不读 app 私有结构）：差异 = Git 变更清单行数
        # （explorer 的 changes Treeview，一行一个变更文件）、测试 /
        # 问题 = 当前任务 evidence 的只读投影（Codex 域，只消费）。
        # provider 均为懒求值——构造时 self.changes 尚未建立
        # （_build_project 在 156 行之后），取不到返回 0。
        self.bottom = ToolPanel(
            self.vertical,
            changes_provider=lambda: len(self.changes.get_children())
            if getattr(self, 'changes', None) else 0,
            tests_provider=lambda: count_tests(self.task_evidence),
            problems_provider=lambda: count_problems(self.task_evidence),
        )
        self.vertical.add(self.bottom, weight=1)
        # 既有调用点契约保留：self.output / self.output_views 指向底部
        # 面板内的 Notebook 与页面字典（write_output、output.select 等
        # 既有方法零改动）。
        self.output = self.bottom.notebook
        self.output_views = self.bottom.views
        self.panes.bind('<ButtonRelease-1>', lambda e: self.capture_layout())
        self.vertical.bind('<ButtonRelease-1>', lambda e: self.capture_layout())
        self._last_pane_width = 0
        self.panes.bind('<Configure>', self._panes_resized)
        self.project_frame = ttk.Frame(self.panes, width=230)
        self.editor_frame = ttk.Frame(self.panes, width=650)
        self.agent_frame = ttk.Frame(self.panes, width=420)
        self._build_project()
        self._build_editor()
        self._build_agent()
        # M2：欢迎页（无项目时中央区的占位视图）。必须在 _build_editor
        # 之后建——它复用 editor_frame 作父容器，与编辑器本体二态切换。
        self._build_welcome()

    def _nav_navigate(self, key):
        """M1 导航栏回调：模式项复刻 switch_mode 的布局捕获语义，动作项
        委托既有方法。本方法只做分发，不改被委托方法的任何行为。"""
        if key in ('general', 'development'):
            self.capture_layout()
            self.state.switch(key)
        elif key == 'project':
            self.choose_project()
        elif key == 'run':
            self.run_tests()
        elif key == 'settings':
            self.model_settings()

    def _build_project(self):
        # M3：左栏重构为「48px 活动栏 + 文件面板」——文件树 / Git 变更 /
        # 搜索 / 文件操作的实现移入 explorer.py 的 ExplorerPanel（示例图
        # 左区结构 + Git 状态语义化 + 文件类型图标）。本方法只做挂载与
        # 契约转交，不实现面板内部结构：
        #   * self.tree / self.changes 仍是 Treeview（指向面板内控件）；
        #   * self.search / self.diff_view 仍是 app 侧 StringVar
        #     （面板的 Entry/Combobox 绑同一变量，refresh_project /
        #     show_diff 的读取路径不变）；
        #   * self.expanded 仍是 app 侧状态（open_project 会重绑定它，
        #     故经 expanded_getter 注入而非传引用）。
        # v0.2 起活动栏扩到 9 图标（任务 / 知识 / 演化 / 审批），对应
        # 侧边栏 6 Tab（app 侧用 install_* 注入真实视图）。
        self.search = tk.StringVar()
        self.diff_view = tk.StringVar(value='combined')
        self.explorer = ExplorerPanel(
            self.project_frame,
            on_open_file=self.open_file,
            on_open_tree=self.open_tree,
            on_open_changed=self.open_changed,
            on_show_diff=self._explorer_show_diff,
            on_expand=self.expand_tree,
            on_refresh=self.refresh_project,
            on_run=self.run_tests,
            boundary_provider=lambda: self.source.boundary if self.source else None,
            expanded_getter=lambda: self.expanded,
            search_var=self.search,
            diff_var=self.diff_view,
            status_var=self.status,
            on_show_tasks=self._show_tasks_tab,
            on_show_knowledge=self._show_knowledge_tab,
            on_show_evolution=self._show_evolution_tab,
            on_show_approvals=self._show_approvals_tab,
        )
        self.explorer.pack(fill='both', expand=True)
        self.tree = self.explorer.tree
        self.changes = self.explorer.changes

    def _build_editor(self):
        # M2：编辑器本体收进 editor_body 子帧——未打开项目时中央区让给
        # 欢迎页（welcome.py 的 WelcomeView，见 _build_welcome /
        # _sync_welcome 二态切换），打开项目后切回。控件父子与既有属性
        # 契约（self.tabs / self.document_state 等）不变，调用方无感。
        body = ttk.Frame(self.editor_frame)
        self.editor_body = body
        body.pack(fill='both', expand=True)
        bar = ttk.Frame(body)
        bar.pack(fill='x', pady=4)
        # W3：编辑器工具栏改走统一按钮规范（尺寸/反馈/提示定版）。
        from .buttons import dev_button
        dev_button(bar, text='保存', command=self.save_current, kind='primary',
                   tooltip='保存当前文档（Ctrl+S）').pack(side='left')
        dev_button(bar, text='重新载入', command=self.reload_current, kind='ghost',
                   tooltip='从磁盘重读当前文档').pack(side='left', padx=4)
        dev_button(bar, text='关闭标签', command=self.close_current, kind='ghost',
                   tooltip='关闭当前标签页').pack(side='left')
        state_bar = ttk.Frame(body)
        state_bar.pack(fill='x', pady=(0, 4))
        dev_button(state_bar, text='编码', command=self.open_encoding, kind='quiet',
                   tooltip='查看/切换文件编码').pack(side='left')
        actions = ttk.Menubutton(state_bar, text='文件审查')
        actions.pack(side='left', padx=4)
        menu = tk.Menu(actions, tearoff=False, bg=C['bg'], fg=C['text'])
        menu.add_command(label='比较磁盘与草稿', command=self.compare_external)
        menu.add_command(label='安全合并到草稿（不保存）', command=self.merge_external)
        menu.add_separator()
        menu.add_command(label='检查点差异 / 恢复此文件', command=self.checkpoints)
        actions.configure(menu=menu)
        dev_button(state_bar, text='附加给 Agent', command=self.attach_current, kind='accent_soft',
                   tooltip='把当前文档附加进下一条任务（发送前保护 Secret）').pack(side='right')
        self.document_state = ttk.Label(body, text='Ctrl+S 保存 · 尚无打开文件', padding=(4, 0, 4, 4))
        self.document_state.pack(fill='x')
        # M1：输出 Notebook 已上移到全宽底部工具面板（bottom_panel.py 的
        # ToolPanel，见 _build）。编辑器栏不再有竖向 Panedwindow，文档
        # 标签页直接占满剩余空间。
        # M4：标签页与右侧操作按钮位（示例图：分屏 / 更多）同一行——
        # Notebook 占左侧弹性区，两个图标按钮固定右侧。
        tab_row = ttk.Frame(body)
        tab_row.pack(fill='both', expand=True)
        # 视觉一致性：编辑器标签页此前用默认 TNotebook（padding/font 走
        # 主题默认值），与底部工具面板、Agent 面板、Explorer 的
        # Coding.TNotebook 不一致——同一界面里两套 tab 度量。统一到
        # Coding.TNotebook（padding=(10,5) + FONT_SMALL）。
        self.tabs = ttk.Notebook(tab_row, style='Coding.TNotebook')
        self.tabs.bind('<<NotebookTabChanged>>', lambda event: self.show_document_state())
        # 右侧图标按钮帧必须先于 Notebook pack：Tk 打包器在腔体被超订时
        # 不重算分配——先 pack 的 slave 拿满请求，后 pack 的 slave 恒得
        # 1x1 且不映射（与 M1 底部面板 height=6、editor_area 分屏重 pack
        # 同属打包器配额问题）。Notebook 内含默认 80x24 请求的 Text
        # （约 648px），会超订 tab_row 腔体（1200x760 下约 468px）；
        # 若 Notebook 先 pack，actions 整帧不可见。side 决定视觉位置、
        # pack 次序决定超订让位优先级：小帧优先，expand 的 Notebook
        # 吸收赤字。
        actions = ttk.Frame(tab_row)
        actions.pack(side='right', fill='y')
        split = IconButton(actions, icon='sidebar', bg=C['bg'], fg=C['ter'],
                           relief='flat', bd=0, cursor='hand2',
                           activebackground=C['hover'],
                           activeforeground=C['text'],
                           command=self.split_current)
        split.pack(side='right', anchor='n', padx=(4, 0), pady=2)
        attach_tooltip(split, '分屏：同文件只读参考视图')
        more = IconButton(actions, icon='more', bg=C['bg'], fg=C['ter'],
                          relief='flat', bd=0, cursor='hand2',
                          activebackground=C['hover'],
                          activeforeground=C['text'],
                          command=self._editor_more_menu)
        more.pack(side='right', anchor='n', padx=(4, 0), pady=2)
        attach_tooltip(more, '更多编辑器操作')
        self.more_button = more
        self.tabs.pack(side='left', fill='both', expand=True)
        # v0.4 产品化收紧：编辑区底部状态行 + 面包屑（合在 body 末尾）
        # — 22px 高，左侧面包屑（项目 / 路径），右侧 行列 / 语言 / 修改状态。
        self._build_editor_status_bar(body)
        self.write_output('测试', '内置 shell_exec 在 Secret 隔离中不可用。这里只展示真实执行或拒绝事件；没有结构化结果时不推算通过数。\n')
        # M6：Review Focus 的中央 Git Diff 只读视图——editor_frame 内
        # 第三态（欢迎页 / 编辑器本体 / 本视图），与另两个子帧共享父
        # 容器，切换只做 pack/pack_forget（_sync_central_view），不销毁
        # 任何控件（任务书：共享同一套工作区组件）。差异文本经
        # runtime.inspect_diff 取（与 show_diff 同一执行路径，不重新
        # 实现 diff 逻辑），增删行着色复用 bottom_panel.highlight_diff_lines
        # （单一真源）。滚动条先于 expand 正文 pack（M4 打包器超订
        # 坍缩教训：后 pack 的 slave 在腔体超订时恒 1x1 不映射）。
        self.review_view = ttk.Frame(self.editor_frame)
        head = ttk.Frame(self.review_view)
        head.pack(fill='x')
        self.review_title = ttk.Label(head, text='Git Diff · 未选择文件',
                                      font=FONT_UI_MED, padding=(2, 3))
        self.review_title.pack(side='left')
        self.review_hint = ttk.Label(
            head, text='从左侧「Git 变更」列表选择文件查看差异',
            foreground=C['ter'], font=FONT_CAPTION)
        self.review_hint.pack(side='left', padx=10)
        diff_body = ttk.Frame(self.review_view)
        diff_body.pack(fill='both', expand=True)
        # 先创建（未 pack）、后定 pack 次序：滚动条先 pack、expand 正文
        # 最后（M4 打包器超订坍缩教训——后 pack 的 slave 在腔体超订时
        # 恒 1x1 不映射；创建次序不影响分配，pack 次序才决定让位优先级）。
        self.review_text = tk.Text(diff_body, wrap='none', font=FONT_MONO_SM,
                                   state='disabled')
        style_editor(self.review_text)
        # 差异着色 tag（与底部 Diff 页同语义同色值；着色函数共用
        # bottom_panel.highlight_diff_lines，tag 注册在各视图构建时
        # 各自完成——同一真源、两份注册）。
        self.review_text.tag_configure('diff_add', foreground=C['ok'])
        self.review_text.tag_configure('diff_del', foreground=C['error'])
        self.review_text.tag_configure('diff_hunk', foreground=C['accent'])
        sy = ttk.Scrollbar(diff_body, command=self.review_text.yview)
        sx = ttk.Scrollbar(diff_body, orient='horizontal',
                           command=self.review_text.xview)
        sy.pack(side='right', fill='y')
        sx.pack(side='bottom', fill='x')
        self.review_text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.review_text.pack(side='left', fill='both', expand=True)

    # ── v0.4 编辑区底部状态行 + 面包屑 ────────────────────────
    # 22px 高的细条；左：面包屑（项目名 / 路径），右：行列 / 语言 / 修改状态。
    # 不进入底部工具面板的 Tab 集合（这是 *编辑器* 状态，不是输出状态）。
    # 文档切换时由 _refresh_editor_status() 主动调用。
    def _build_editor_status_bar(self, body):
        bar = tk.Frame(body, bg=C['surface2'], height=22)
        bar.pack_propagate(False)
        # 面包屑区（左）
        self._crumb = tk.Label(bar, text='未打开文件', font=FONT_CAPTION,
                               bg=C['surface2'], fg=C['ter'], anchor='w')
        self._crumb.pack(side='left', padx=(10, 0), pady=3)
        # 状态组（右）—— 行列 / 语言 / 修改状态
        self._pos = tk.Label(bar, text='— : —', font=FONT_CAPTION,
                            bg=C['surface2'], fg=C['text'], anchor='e', width=12)
        self._pos.pack(side='right', padx=(10, 0), pady=3)
        sep1 = tk.Frame(bar, bg=C['border'], width=1)
        sep1.pack(side='right', fill='y', pady=4)
        self._lang = tk.Label(bar, text='Plain', font=FONT_CAPTION,
                             bg=C['surface2'], fg=C['text'], anchor='e', width=10)
        self._lang.pack(side='right', padx=(10, 0), pady=3)
        sep2 = tk.Frame(bar, bg=C['border'], width=1)
        sep2.pack(side='right', fill='y', pady=4)
        self._modified = tk.Label(bar, text=' ', font=FONT_CAPTION,
                                   bg=C['surface2'], fg=C['ok'], anchor='e', width=8)
        self._modified.pack(side='right', padx=(10, 0), pady=3)
        # 必须先于 tabs 之后 pack；tabs expand=True 会吃掉剩余空间，但状态条
        # 是固定 22px（pack_propagate=False），Tk 打包器在腔体超订时按 pack
        # 顺序从末尾牺牲 expand=1——本条 expand=0 不会被牺牲。
        bar.pack(side='bottom', fill='x', before=self.tabs)

    def _refresh_editor_status(self, *, crumb=None, line=None, col=None,
                               language=None, modified=None):
        """推一次编辑器底部状态。文档切换 / 编辑时由调用方驱动。"""
        if crumb is not None:
            try:
                self._crumb.configure(text=crumb, fg=C['text'] if crumb else C['ter'])
            except tk.TclError:
                pass
        if line is not None or col is not None:
            try:
                self._pos.configure(text=f'{line or 0} : {col or 0}')
            except tk.TclError:
                pass
        if language is not None:
            try:
                self._lang.configure(text=language or 'Plain')
            except tk.TclError:
                pass
        if modified is not None:
            try:
                self._modified.configure(
                    text=('● 已修改' if modified else '已保存'),
                    fg=(C['warn'] if modified else C['ok']))
            except tk.TclError:
                pass

    def _build_welcome(self):
        """M2：欢迎页挂载 + 初始可见性。

        无项目时中央区（editor_frame）显示欢迎页；打开项目后由
        _project_loaded 调 _sync_welcome 切回编辑器本体。视图本体在
        welcome.py，这里只做挂载与切换，不实现欢迎页内部结构。
        """
        self.welcome = WelcomeView(
            self.editor_frame,
            on_open=self.choose_project,
            on_recent=self.open_project,
            recent_provider=lambda: self.state.value('recent'),
        )
        self._sync_welcome()

    def _sync_welcome(self):
        """M2：按 self.source 二态切换中央区——无项目 → 欢迎页；
        有项目 → 编辑器本体（editor_body）。只做 editor_frame 内两个
        子帧的 pack/pack_forget，不触碰三栏 pane 的增量挂载路径。

        与 M1-fix 的 _settle_mount 同一原则：pack 切换后立即冲刷
        idle 任务，让新显示子帧的内容分层映射同步落地——否则内容
        子树排在 Tk 的延迟重排队列里，依赖外部事件循环逐个排空
        （实测每层可滞后数百 ms 甚至不收敛），欢迎页/编辑器会长时间
        停在不可见状态。update_idletasks 只服务 idle 回调，不派发
        窗口事件，无自激风险。

        M6：中央区增第三态（Review Focus 的 Git Diff 只读视图），
        本方法保留 M2 签名与调用点，实际切换逻辑委派
        _sync_central_view（三态真源）。"""
        self._sync_central_view()

    def _sync_central_view(self):
        """M6：中央区三态切换——无项目 → 欢迎页（welcome.py）；
        Review Focus 预设 → Git Diff 只读视图（review_view）；其余
        → 编辑器本体（editor_body）。只做 editor_frame 内三个子帧的
        pack/pack_forget 与 pack 后 idle 冲刷（原则同 M2），不触碰
        三栏 pane 的增量挂载路径，不销毁任何控件（打开的文件 / 未保存
        编辑随 editor_body 原样保留——它只是不可见，未被销毁）。
        """
        welcome = getattr(self, 'welcome', None)
        body = getattr(self, 'editor_body', None)
        review = getattr(self, 'review_view', None)
        if welcome is None or body is None or review is None:
            return
        for widget in (welcome, body, review):
            widget.pack_forget()
        if self.source is None:
            # 先 pack 再 refresh：子控件必须在父控件已获几何管理器后
            # 创建/挂载，否则整树映射时它们停留在 unmapped（实测：欢迎页
            # 已 mapped 而近期项目行仍 1x1 未映射，点击不触发）。
            welcome.pack(fill='both', expand=True)
            welcome.refresh()
            welcome.update_idletasks()
        elif self._review_active():
            review.pack(fill='both', expand=True)
            review.update_idletasks()
        else:
            body.pack(fill='both', expand=True)
            body.update_idletasks()

    def _review_active(self):
        """当前是否处于 Review Focus 且有项目（中央 Git Diff 视图的显式
        条件；preset 从 state 读，不持有第二份布局状态）。"""
        try:
            return (self.source is not None
                    and self.state.layout['preset'] == 'review')
        except (AttributeError, KeyError):
            return False

    def _apply_preset_emphasis(self):
        """M6：预设内容侧重——共享组件、增量切换，不销毁不重建
        （任务书：三种模式共享同一套工作区组件）。

        Review Focus：左栏切「Git 变更」清单（修改文件列表）、中央切
        Git Diff 只读视图、右栏切「任务 / 子 Agent」页（审查结果输出
        所在页）、底部切「测试」页。其余预设：中央回编辑器本体、左栏
        回文件树（底部/右栏保持用户自选，不重置）。
        """
        if self._closed:
            return
        preset = self.state.layout['preset']
        explorer = getattr(self, 'explorer', None)
        bottom = getattr(self, 'bottom', None)
        if preset == 'review':
            if explorer is not None:
                explorer.select_view('git')
            self._sync_central_view()
            workflow = getattr(self, '_workflow_tab', None)
            if workflow is not None:
                self.log_tabs.select(workflow)
            if bottom is not None:
                bottom.select('测试')
        else:
            if explorer is not None:
                explorer.select_view('files')
            self._sync_central_view()

    def _explorer_show_diff(self, event=None):
        """M6：Explorer「Git 变更」选择/视图切换的统一入口——Review
        Focus 下差异渲染到中央 Git Diff 只读视图，其余预设走既有
        show_diff（底部 Diff 页，含操作来源/审批记录头）。本方法只做
        分发，两条路径的取数执行路径同一（runtime.inspect_diff）。
        """
        if self._review_active():
            selected = self.changes.selection()
            if selected:
                self._render_review_diff(self.source.root / selected[0])
                return
        self.show_diff(event)

    def _render_review_diff(self, path):
        """M6：把 path 的 Git 差异渲染进中央只读视图（异步取数，
        单调请求号防过期回包——与 show_diff 的 _diff_request 同范式）。
        """
        self._review_request += 1
        request = self._review_request
        source = self.source
        view = self.diff_view.get()

        def done(value, error):
            if error or request != self._review_request or self._closed:
                return
            self._write_review_diff(path, value)

        self.work(lambda: self.runtime.inspect_diff(source, path, view), done)

    def _write_review_diff(self, path, text):
        """M6：中央 Git Diff 视图的内容写入（只读 Text 临时切 normal，
        写完还原 disabled；增删行着色经 bottom_panel.highlight_diff_lines
        单一真源）。标题显示相对路径——空结果不伪造内容。"""
        try:
            relative = Path(path).resolve().relative_to(
                self.source.root.resolve()).as_posix()
        except (ValueError, OSError):
            relative = str(path)
        self.review_title.configure(text=f'Git Diff · {relative}')
        self.review_hint.configure(
            text='修改前后对比 · 新增 / 删除行按 Git 语义着色'
            if text and text.strip() else '该文件暂无差异内容')
        widget = self.review_text
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', text or '')
        widget.configure(state='disabled')
        highlight_diff_lines(widget)

    def _build_agent(self):
        from .agent_panel import AgentPanel, TaskInput
        from gui_theme import button_feedback
        from ui_icons import IconButton
        # v0.3：status_row 默认不显示——概念图里没有「Agent · 未运行」
        # 与「权限 read-only」这两个老 UI 元素；状态信息由 RuntimeContext
        # 顶部的「权限矩阵」徽标承担（「工作区可写」绿标 / 「只读」灰标）。
        # task_label / perm_badge 仍保留为属性，供 _sync_perm_badge 与
        # 任务态写入用，但 status_row 不 pack。
        status_row = ttk.Frame(self.agent_frame)
        self.task_label = ttk.Label(status_row, text='Agent · 未运行', wraplength=260)
        self.perm_badge = ttk.Label(status_row, text='权限 read-only', foreground=C['subtext'])
        self._sync_perm_badge()

        # B2（UX 审查）：完整事件是开发者调试通道——AgentPanel 二轮起由
        # agent_panel.AgentPanel 统一承载三标签与事件页挂载。
        # v0.2 起：标题栏新增「审批」入口、4-Tab（对话/任务/子 Agent/完整事件）
        # 上方插入「运行时上下文」组件（权限矩阵 + 模型路由）。
        self.agent_panel = AgentPanel(self.agent_frame, open_file=self.open_agent_file,
            view_diff=self.view_agent_diff, revert=self.revert_agent_change,
            pending=self.resolve_agent_approval, configure_model=self.agent_model_settings,
            refresh=self.refresh_agent_versions, new_conversation=self._new_agent_conversation,
            history=self.restore_session, settings=self.model_settings, view_changes=self._view_agent_changes,
            on_show_approvals=self._show_approvals_tab,
            history_source=lambda: (self.runtime._resume_evidence or {}).get('path'))
        self.log_tabs = self.agent_panel.notebook
        self.chat = self.agent_panel.chat
        self.workflow = self.agent_panel.workflow
        # 注入运行时上下文（权限矩阵 + 模型路由）—— 失败时静默退化为
        # 旧 chrome（保留旧 `_sync_perm_badge` 路径），不影响核心 run。
        self._runtime_ctx = self._safe_mount_runtime_context()
        event_content = self.agent_panel.mount_events(self.output)
        self.events = event_content.text
        # v0.2：把「完整事件」Tab 的 holder 也挂到 app（与 self.events
        # 同源；旧测试通过 .master 拿 holder）。
        self._events_holder = self.agent_panel._events_holder
        self._events_tab = event_content
        self.dev_events = tk.BooleanVar(value=True)
        # M6：审查结果输出页引用（AgentPanel 版任务页）。
        self._workflow_tab = self.agent_panel.notebook.tabs()[1]
        # v0.4：状态条起点 = idle（等任务接入 done / running 切换）
        self.set_agent_status('idle', '就绪 · 等待任务')

        self.editor_contexts = []
        # ── 会话组 ──────────────────────────────────────────────
        # context_label 的创建移到 W2 统一输入容器（input_bar）内，
        # 附件提示贴着输入行而非栏底。
        self.input = TaskInput(self.agent_frame, height=4, wrap='word', undo=True, font=FONT_UI)
        style_editor(self.input)
        # v0.3：input 留在 agent_frame 顶层（与原契约一致，避免 pack
        # 重新 parent 带来的隐患）；send_row 只放发送按钮，input 与之
        # 并列填满 agent_frame 底部。
        # E1（UX 审查）：Enter 发送、Shift+Enter 换行（与主对话一致），
        # 输入区旁明示键位；IME 组合确认不触发发送（守卫方式与
        # InputCard 相同：行内 IME 默认关闭，系统候选窗路径 Tk 不向
        # 绑定投递组合期按键）。
        self._inline_ime = (InlineIME(self.input)
                            if InlineIME and os.environ.get("FORGE_INLINE_IME") == "1"
                            else None)
        self.input.bind('<Return>', self._enter_key)
        self.input.bind('<Control-Return>', lambda e: (self.send(), 'break')[-1])

        # v0.3：老「继承配置」区块（row / row2 / row3 / row4 /
        # task_sep / model_sep）已全部删除——它们的职责被 RuntimeContext
        # 吸收：
        #   * 模型 / fallback / MOA        →  RuntimeContext.模型路由
        #   * 规划 / 角色 / 任务操作        →  RuntimeContext.任务控制
        #   * 权限 read-only / workspace-write →  RuntimeContext.权限矩阵.Sandbox
        # 老属性的引用（self.model_choice / self.planning / self.permission /
        # self.delegate_button / self.cancel_button / self.run_model / etc.）
        # 保留为 None / 兼容占位，外部测试与 selftest 仍能 import；RuntimeContext
        # 通过 self._runtime_ctx 暴露（self._runtime_ctx.get_task() /
        # .get_routing() / .get_policy()）。
        self.model_choice = None
        self.planning = tk.StringVar(value='low')
        self.permission = tk.StringVar(value='read-only')
        # R-fix：三个旧 chrome 哨兵改为轻量兼容对象——v0.3 起 UI 由
        # RuntimeContext 接管，但旧测试契约（.cget('text')）与既有
        # getattr 防御读取路径继续可用。delegate/cancel 绑到 agent_role
        # 切换时的按钮状态语义；run_model 为隐藏 Label（纯数据投影）。
        self.delegate_button = tk.Label(self, text='创建子 Agent')
        self.cancel_button = tk.Label(self, text='取消')
        self.run_model = tk.Label(self, text='模型 · 继承配置')
        # 不 pack：v0.3 起 chrome 由 RuntimeContext 负责，这些对象仅
        # 作为兼容投影存在（winfo 不可见，cget/configure 契约有效）。
        self.task_role = tk.StringVar(value='default')
        self.agent_role = tk.StringVar(value='agent')
        # R-fix：role_choice 哨兵(None) 别名到 agent_role 真源——v0.3 起
        # 角色下拉由 TaskControls 接管，旧 .get/.set 契约与读取路径保留
        # （原哨兵在挂载后被赋值，会覆写挂载点别名，故直接在此别名）。
        self.role_choice = self.agent_role
        # permission trace 仍保留：RuntimeContext.Sandbox 同步触发徽标更新。
        import weakref
        owner = weakref.ref(self)
        def permission_display(*args):
            bench = owner()
            if bench is not None and not bench._closed:
                try:
                    bench._sync_perm_badge()
                except Exception:
                    pass
        self.permission.trace_add('write', permission_display)

        # ── 预存消息队列（上限 3，任务完成后自动发送）────────────
        self._pending_prompts: list[dict] = []
        # ── W2：统一输入容器 ──
        # 底部输入区收敛为一个 self.input_bar（side='bottom' 全宽容器）：
        #   queue_label（有队列时才显）/ context_label（有附件时才显）
        #   → hint_label → input + send 同一行。
        # 动机：此前 input 与 send_row 是 agent_frame 底部两个并列的
        # side='bottom' slave，同一行关系靠两个 slave 的分配默契维持；
        # 容器化后同行关系由 input_row 内 pack 显式声明，缩放行为可断言。
        # 契约保留：self.input / self.send_button 属性名不变，hint /
        # queue / context 的显示逻辑（_sync_queue_label / attach）不变。
        self.input_bar = ttk.Frame(self.agent_frame)
        self._queue_label = ttk.Label(self.input_bar, text='', padding=(4, 1),
                                      foreground=C['accent2'])
        # context_label 父级改为 input_bar：附件信息仍贴在输入行上方。
        self.context_label = ttk.Label(self.input_bar, text='未附加编辑器内容', padding=4)
        hint_label = ttk.Label(self.input_bar,
                               text='Enter 发送 · Shift+Enter 换行 · Ctrl+Enter 亦可',
                               foreground=C.get('muted', '#777'))
        # 输入行：input 吃满剩余宽度，send 固定在右——同一行内布局。
        input_row = ttk.Frame(self.input_bar)
        self.send_button = IconButton(input_row, text='发送', command=self.send,
            bg=C['accent'], fg=C['text'], activebackground=C['accent_hover'],
            bd=0, padx=12, pady=5, highlightthickness=1)
        button_feedback(self.send_button, kind='primary')
        self.send_button.pack(side='right', padx=(6, 0))
        # input 用 in_= 挂进 input_row（master 不变，契约零改动）；
        # send 先 right 占位，input 再 left+expand 吃满剩余宽。
        self.input.pack(in_=input_row, side='left', fill='x', expand=True,
                        padx=(6, 0), pady=4)
        # 容器内部（side='bottom'：先 pack 者贴底）：input_row 先 pack
        # 占底，hint 其次在其上；queue/context 动态显示时经
        # before=hint 插到 hint 之前（即输入行正上方）。
        input_row.pack(side='bottom', fill='x')
        self._input_row = input_row  # 输入行（send/input 所在）
        self._input_hint = hint_label  # queue/context 的 before 锚点
        hint_label.pack(side='bottom', fill='x', padx=8)
        # 容器本身作为 agent_frame 底部唯一输入组（取代原 3 个并列 slave）。
        self.input_bar.pack(side='bottom', fill='x')
        # context_label 默认隐藏——只在用户附加了编辑器内容时由 _attach
        # 主动 pack_forget 显出来（仍走 agent_frame 底部）。
        # v0.3：status_row 不再 pack（task_label / perm_badge 信息已迁
        # 入 RuntimeContext.权限矩阵徽标）。
        self.agent_panel.pack(fill='both', expand=True)

    def _agent_role_selected(self, event=None):
        # 角色变更同步到 task_role（保留旧 self.task_role 写入路径，
        # 旧 RunSnapshot 仍读它）。RuntimeContext.角色 同步反向。
        if getattr(self, 'task_role', None) is not None:
            self.task_role.set('default' if self.agent_role.get() == 'agent' else self.agent_role.get())

        # R-fix：RuntimeContext.角色下拉反向同步（best-effort，UI 一致性）。
        ctx = getattr(self, '_runtime_ctx', None)
        if ctx is not None:
            try:
                ctx.set_role(self.agent_role.get())
            except Exception:
                pass
    def _agent_model_selected(self, event=None):
        if getattr(self, 'agent_role', None) is not None:
            self.agent_role.set('agent' if self.task_role.get() == 'default' else self.task_role.get())
        if self.runtime.busy:
            return  # Next-task selection does not rewrite the running-model badge.
        # v0.3：model_choice 已被 RuntimeContext 接管；下拉索引沿用
        # self.model_ids 与 self.runtime.catalog 即可。
        if getattr(self, 'model_choice', None) is None:
            return
        index = self.model_choice.current()
        pair = self.runtime.catalog.get(self.model_ids[index]) if index > 0 else None
        snapshot = self.state.task_snapshot(explicit_model=pair, role=self.task_role.get())
        model = snapshot.model or getattr(getattr(self.runtime.bridge, 'router', None), 'primary', None)
        if getattr(self, 'run_model', None) is not None:
            self.run_model.configure(text='模型 · ' + (' / '.join(model) if model else '继承配置'))
        if event is not None:
            self.input.focus_set()

    def _enter_key(self, event):
        """E1：Enter=发送，Shift+Enter=换行；IME 组合期不拦截。"""
        if event.state & 0x0001:        # Shift 按下 → 走默认换行
            return None
        ime = self._inline_ime
        if ime is not None and ime.composing:
            return None                 # 组合确认交给输入法
        self.send()
        return 'break'


    def _new_agent_conversation(self):
        if (not self.runtime.ready or self.runtime.busy or self._loading or self._jobs
                or self._submission_active or self._pending_prompts):
            self.status.set('请先打开项目并等待当前任务与后台操作结束，再创建新对话')
            return
        runtime = self.runtime
        def create():
            if not runtime._lane.acquire(False):
                raise RuntimeError('当前任务尚未结束')
            try:
                import uuid
                from forge.session import Session
                old = runtime.bridge.agent.session
                path = old.path.parent / (uuid.uuid4().hex + '.jsonl')
                fresh = Session(path, meta={'cwd': str(runtime.bridge.workspace)})
                with fresh:
                    fresh.sync()
                old.close()
                runtime.bridge.agent.session = fresh
                runtime.bridge.history = []
                runtime.bridge.last_result = None
                runtime.evidence = TaskEvidence()
                runtime.last_task_id = ''
                runtime.last_snapshot = None
                runtime._resume_evidence = None
                self.state.remember_session(path)
                return path
            finally:
                runtime._lane.release()
        def created(path, error):
            if error:
                return
            self.chat.clear()
            self.agent_panel._cards.clear()
            self.task_evidence = TaskEvidence()
            self.agent_panel.task_context = {}
            self._retry_request = None
            self._file_actions.clear()
            self._permission = 'read-only'
            self.permission.set('read-only')
            self.render_evidence()
            self.status.set('已创建新对话 · 原 Session 保留在历史中')
            self.input.focus_set()
        self.work(create, created)


    def _view_agent_changes(self, evidence=None):
        evidence = evidence or self.task_evidence
        changes = list(evidence.changes.values())
        if changes:
            preview = '\n\n'.join('文件：' + str(c['path']) + '\n' +
                (c.get('diff') if c.get('diff') is not None else '此变更没有安全文本预览') for c in changes)
            self.view_agent_diff({'diff': preview, 'diff_truncated': any(c.get('diff_truncated') for c in changes)})
        elif evidence.diffs:
            self.view_agent_diff(evidence.diffs[-1])


    def _editor_status_fields(self):
        """M7 编辑器状态字段的当前值（供 _refresh_status_fields 并集推送）。"""
        if getattr(self, 'status_bar', None) is None:
            return {}
        row = self.current_row()
        fields = {}
        if row is None:
            fields.update(cursor='—', encoding='—', eol='—', language='—')
        else:
            document = row['document']
            area = row.get('area')
            cursor = '行 1, 列 1'
            if area is not None:
                try:
                    line, column = area.text.index('insert').split('.')
                    cursor = f'行 {line}, 列 {int(column) + 1}'
                except (tk.TclError, ValueError):
                    pass
            fields.update(cursor=cursor, encoding=document.encoding.upper(),
                          eol={'\r\n': 'CRLF', '\r': 'CR'}.get(document.newline, 'LF'),
                          language=_LANGUAGE_BY_SUFFIX.get(document.path.suffix.lower(), 'Plain'))
        try:
            fields.update(warnings=0, problems=count_problems(self.task_evidence))
        except Exception:
            fields.update(warnings=0, problems=0)
        return fields


    def _sync_editor_status(self):
        """M7：推送编辑器状态字段（光标 / 编码 / 换行 / 语言 / 警告 /
        问题）到状态栏。数据源全部真实：当前 Document 的 encoding /
        newline 属性、扩展名→语言映射、TaskEvidence 失败计数（与底
        部面板 Problems 徽标同源 count_problems）。无打开文件时
        光标/编码/换行/语言显示占位，警告与问题显示 0（无独立诊断
        器，不伪造）。
        """
        bar = getattr(self, 'status_bar', None)
        if bar is None:
            return
        row = self.current_row()
        if row is None:
            bar.update(cursor='—', encoding='—', eol='—', language='—')
            self._refresh_editor_status(crumb='', line=0, col=0,
                                         language='Plain', modified=False)
        else:
            document = row['document']
            area = row.get('area')
            cursor = '行 1, 列 1'
            line_num = col_num = 0
            if area is not None:
                try:
                    line, column = area.text.index('insert').split('.')
                    line_num = int(line)
                    col_num = int(column) + 1
                    cursor = f'行 {line}, 列 {col_num}'
                except (tk.TclError, ValueError):
                    pass
            lang = _LANGUAGE_BY_SUFFIX.get(document.path.suffix.lower(), 'Plain')
            bar.update(cursor=cursor, encoding=document.encoding.upper(),
                       eol={'\\r\\n': 'CRLF', '\\r': 'CR'}.get(
                           document.newline, 'LF'),
                       language=lang)
            # v0.4：底部状态行（面包屑 + 行列 + 语言 + 修改状态）
            project_root = self.source.root.name if self.source else '未打开项目'
            rel = None
            try:
                rel = document.path.relative_to(self.source.root) if self.source else None
            except ValueError:
                rel = document.path.name
            crumb = f'{project_root}  /  {rel if rel else document.path.name}'
            self._refresh_editor_status(
                crumb=crumb, line=line_num, col=col_num,
                language=lang, modified=(document.status in ('modified', 'conflict')))
        bar.update(warnings=0, problems=count_problems(self.task_evidence))


    def _on_editor_cursor(self, row):
        """M7：编辑器光标移动回调——只更新光标字段（轻量路径，不
        走整个 _sync_editor_status 的文档/证据读取）。row 为标签行
        字典（闭包在 EditorArea 构造前绑定，避免 area 未赋值）。"""
        bar = getattr(self, 'status_bar', None)
        if bar is None:
            return
        area = row.get('area')
        if area is None:
            return
        try:
            line, column = area.text.index('insert').split('.')
        except (tk.TclError, ValueError):
            return
        bar.update(cursor=f'行 {line}, 列 {int(column) + 1}')


    def _sync_ready_status(self):
        """M7：就绪态字段 + 圆点。数据源 = runtime 真实状态：运行中
        （busy）→ 黄点「运行中」；Agent 就绪 → 绿点「就绪」；否则
        灰点「未就绪」。不持有第二份状态，全部现读。
        """
        bar = getattr(self, 'status_bar', None)
        if bar is None:
            return
        try:
            if self.runtime.busy:
                bar.update(ready='运行中')
                bar.set_indicator('warn')
            elif self.runtime.ready:
                bar.update(ready='就绪')
                bar.set_indicator('ok')
            else:
                bar.update(ready='未就绪')
                bar.set_indicator('ter')
        except Exception:
            pass


    def _sync_online_status(self):
        """M7：头部在线状态。数据源 = runtime 在线连接状态——当前
        runtime 无 peers/presence 概念（无真实数据），按任务书纪律
        固定显示「离线」+ 灰点，不伪造计数。接入真实数据源时只改
        本方法（圆点 fill 与文案一处）。
        """
        dot = getattr(self, 'online_dot', None)
        var = getattr(self, 'online_var', None)
        if dot is None or var is None:
            return
        try:
            dot.itemconfigure('dot', fill=C['ter'])
            var.set('离线')
        except tk.TclError:
            pass

    def _toggle_events_tab(self):
        # v0.2：事件视图统一在 AgentPanel 的「完整事件」Tab，不再放底部
        # Notebook。`dev_events` 仍保留为开关语义，但 v0.2 改控 AgentPanel
        # 第 4 个 Tab 的可见性（不再操作 self.output）。
        if self.dev_events.get():
            self.agent_panel.notebook.add(self._events_holder, text='完整事件')
        else:
            # 已 add 过则隐藏；首次切换时可能未 add，容错跳过。
            try:
                self.agent_panel.notebook.hide(self._events_holder)
            except tk.TclError:
                pass

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
        # 空态引导是一次性的：首次写入真实内容前先抹掉（bottom_panel 的
        # EMPTY_HINTS 用 'empty_hint' tag 标记）。没有该 tag 的控件
        # （对话 / 事件页）tag_ranges 返回空，本分支不触发。
        if widget.tag_ranges('empty_hint'):
            widget.delete('1.0', 'end')
        chunk = str(redact(text))
        widget.insert('end', chunk)
        self._paint_result_lines(widget, chunk)
        widget.configure(state='disabled')
        widget.see('end')

    def _paint_result_lines(self, widget, chunk):
        """测试/执行结果行着色：PASS 绿、FAIL/ERROR 红（对齐概念图）。"""
        try:
            tag_name = 'forge_pass'
            if tag_name not in widget.tag_names():
                widget.tag_configure(tag_name, foreground=C.get('ok', '#48B89A'))
            fail_tag = 'forge_fail'
            if fail_tag not in widget.tag_names():
                widget.tag_configure(fail_tag, foreground=C.get('error', '#E57373'))
            start = widget.index('end-1c linestart')
            # 只处理本次新插入区域的行
            line_count = chunk.count('\n') + 1
            for i in range(line_count):
                line_start = widget.index(f'end-1c -{i} lines linestart')
                line_end = widget.index(f'{line_start} lineend')
                line_text = widget.get(line_start, line_end)
                stripped = line_text.strip()
                if (stripped.startswith('PASS') or stripped == 'OK' or
                        (stripped.startswith('test') and stripped.endswith(' ... ok'))):
                    widget.tag_add(tag_name, line_start, line_end)
                elif (stripped.startswith(('FAIL', 'ERROR', 'Error', 'error')) or
                        (stripped.startswith('test') and stripped.endswith((' ... FAIL', ' ... ERROR')))):
                    widget.tag_add(fail_tag, line_start, line_end)
        except Exception:
            pass  # 着色失败不影响内容输出

    def write_output(self, name, text, *, clear=False):
        # v0.2 页改名兼容：旧页名（Diff/终端）经 bottom 面板别名表映射
        # 到新页（差异），保持既有调用点零改动。
        from .bottom_panel import PAGE_ALIASES
        widget = self.output_views[PAGE_ALIASES.get(name, name)]
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
        try:
            threading.Thread(target=run, daemon=True, name='forge-development-io').start()
        except BaseException:
            self._jobs -= 1
            raise

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
        if (self.runtime.busy or self._loading or self._submission_active
                or getattr(self, '_pending_prompts', ())):
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
        self.send_button.configure(state='normal')
        if error:
            self._submission_notice('项目载入失败：' + error, error=True)
            return
        self.source, self.runtime, ready = value
        # M2：项目已载入，中央区从欢迎页切回编辑器本体。
        self._sync_welcome()
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
        # RuntimeContext 在 v0.3 接管 model 列表——同步给它即可。
        if self._runtime_ctx is not None:
            try:
                model_labels = [row['label'] for row in ready['models']]
                # primary 是 RoutingChain 的主模型下拉——如果当前 primary
                # 不在新列表里，回落第一个。
                current_routing = self._runtime_ctx.get_routing()
                primary = current_routing.get('primary', '')
                options = ['继承配置', *model_labels]
                self._runtime_ctx.set_model_options(options)
                self._safe_set_routing_primary(primary if primary in options else '继承配置')
            except Exception:
                pass
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

    # ── v0.4 Agent 状态条 wiring ─────────────────────────────────
    # 单一入口：app 任何地方需要更新 Agent 顶部状态条都走这里。
    # state ∈ {idle, running, waiting, error}，last_action 是可选时间戳。
    # 缺 AgentPanel（独立运行 / 未 hosted）时静默 no-op。
    def set_agent_status(self, state, text=None, last_action=None):
        panel = getattr(self, 'agent_panel', None)
        if panel is None or not hasattr(panel, 'set_status_state'):
            return
        try:
            panel.set_status_state(state, text or '', last_action=last_action)
        except tk.TclError:
            pass

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
            if getattr(self, '_runtime_ctx', None) is not None:
                self._runtime_ctx.set_sandbox(value)
        except Exception:
            pass

    # ── v0.2 新 chrome 接线 ──────────────────────────────────────

    def _safe_mount_runtime_context(self):
        """挂载 RuntimeContext 组件到 AgentPanel 顶部。

        失败时返回 None（保留旧 chrome）；失败原因写到状态栏但不阻断 run。
        """
        try:
            from .policy_widgets import RuntimeContext
            policy_kwargs = self._policy_matrix_kwargs()
            routing_kwargs = self._routing_chain_kwargs()
            task_kwargs = self._task_controls_kwargs()
            return self.agent_panel.mount_runtime_context(
                policy_kwargs=policy_kwargs,
                routing_kwargs=routing_kwargs,
                task_kwargs=task_kwargs,
            )
        except Exception as exc:
            self._safe_status(f'运行时上下文不可用：{exc}', level='warn')
            return None

    def _task_controls_kwargs(self):
        """任务控制（规划 / 角色 / 操作按钮）初始 kwargs。

        操作按钮的回调全连到 app 现有方法（review / restore_session /
        run_tests / cancel_task / retry_agent_task / agent_model_settings
        / send(delegate=True)）。这些方法**不**会因为 RuntimeContext 接管
        UI 而失效——它们继续是 app 的事件源。
        """
        return dict(
            planning=(getattr(self, 'planning', None).get()
                      if getattr(self, 'planning', None) is not None else 'low'),
            role=(getattr(self, 'agent_role', None).get()
                  if getattr(self, 'agent_role', None) is not None else 'agent'),
            on_planning_change=self._on_planning_change,
            on_role_change=self._on_role_change,
            on_review=self.review,
            on_restore=self.restore_session,
            on_delegate=lambda: self.send(delegate=True),
            on_retry=self.retry_agent_task,
            on_cancel=self.cancel_task,
            on_run_tests=self.run_tests,
        )

    def _on_planning_change(self, value):
        if getattr(self, 'planning', None) is not None:
            try:
                self.planning.set(value)
            except Exception:
                pass

    def _on_role_change(self, value):
        # 角色变更同步到 agent_role（兼容旧 self.agent_role 读取路径）。
        if getattr(self, 'agent_role', None) is not None:
            try:
                self.agent_role.set(value)
                self.task_role.set('default' if value == 'agent' else value)
            except Exception:
                pass

    def _policy_matrix_kwargs(self):
        """从当前 workbench 状态构造 PolicyMatrix 初始值（best-effort）。"""
        mode = 'read-only'
        sandbox = 'read-only'
        read_scope = 'workspace'
        secret_scope = 'isolated'
        try:
            perm = getattr(self, 'permission', None)
            if perm is not None:
                value = perm.get() if hasattr(perm, 'get') else str(perm)
                if value == 'workspace-write':
                    sandbox = 'workspace-write'
                elif value == 'read-only':
                    sandbox = 'read-only'
                else:
                    sandbox = 'workspace-write'
        except Exception:
            pass
        mode = 'read-only' if sandbox == 'read-only' else 'acceptEdits'
        return dict(mode=mode, sandbox=sandbox, read_scope=read_scope, host_managed=True,
                    secret_scope=secret_scope,
                    on_change=self._on_policy_change)

    def _routing_chain_kwargs(self):
        primary, fallback, moa = self._collect_routing()
        # 主模型选项 = 当前 catalog 的所有 model id；与顶部下拉共享。
        options = self._collect_model_options()
        return dict(primary=primary, fallback=fallback, moa=moa,
                    model_options=options, host_managed=True,
                    on_change=self._on_routing_change)

    def _collect_routing(self):
        """从 self.state / ModeState 抽 routing（best-effort）。"""
        try:
            state = getattr(self, 'state', None)
            if state is not None:
                value = state.value('routing') if hasattr(state, 'value') else None
                if isinstance(value, dict):
                    return (value.get('primary', '') or '',
                            value.get('fallback', '') or '',
                            value.get('moa', 'off') or 'off')
        except Exception:
            pass
        return ('', '', 'off')

    def _collect_model_options(self):
        """从 catalog 收集 model id 列表（与 self.model_choice 一致）。"""
        try:
            model_choice = getattr(self, 'model_choice', None)
            if model_choice is not None:
                values = model_choice['values'] if isinstance(model_choice, dict) else None
                if values is None and hasattr(model_choice, 'cget'):
                    values = model_choice.cget('values')
                if values:
                    return tuple(values)
        except Exception:
            pass
        return ()

    def _on_policy_change(self, *, mode, sandbox, read_scope, secret_scope):
        """RuntimeContext 矩阵变更 → 写回 self.permission 与状态栏。"""
        # 同步状态栏徽标（B1 修正回归点）。
        if sandbox not in {'read-only', 'workspace-write'}:
            self._sync_perm_badge()
            self._submission_notice('Coding 任务仅支持只读或工作区写入，未启用危险全访问。', error=True)
            return
        if mode in {'read-only', 'plan'}:
            sandbox = 'read-only'
        if hasattr(self, 'permission'):
            try:
                self.permission.set(sandbox)
                self._sync_perm_badge()
            except Exception:
                pass
        # 不在此处直接改 policy.mode——架构审查 P0-2 / P1-7 修复要求
        # app 在保存路径做强校验；这里只做即时展示。

    def _on_routing_change(self, *, primary, fallback, moa):
        """RuntimeContext 路由变更 → 写回顶部 model_choice 与状态栏。"""
        if primary and hasattr(self, 'model_var'):
            try:
                self.model_var.set(primary)
            except Exception:
                pass

    def _safe_set_routing_primary(self, primary):
        """更新 RuntimeContext.模型路由.primary（不触发 on_change 反馈）。"""
        if getattr(self, '_runtime_ctx', None) is None:
            return
        try:
            self._runtime_ctx.set_routing_primary(primary or '')
        except Exception:
            pass

    def _show_tasks_tab(self):
        """活动栏/标题栏点「任务」——侧边栏切到任务 Tab（暂无独立实现）。"""
        if getattr(self, 'explorer', None) is None:
            return
        try:
            self.explorer._activity.select('tasks')
        except Exception:
            pass

    def _show_knowledge_tab(self):
        if getattr(self, 'explorer', None) is None:
            return
        try:
            self.explorer._activity.select('knowledge')
            self._ensure_knowledge_view()
        except Exception:
            pass

    def _show_evolution_tab(self):
        if getattr(self, 'explorer', None) is None:
            return
        try:
            self.explorer._activity.select('evolution')
            self._ensure_evolution_view()
        except Exception:
            pass

    def _show_approvals_tab(self):
        """活动栏/Agent 标题栏点「审批」——切到底部面板的「审批」Tab。"""
        if getattr(self, 'bottom', None) is None:
            return
        try:
            self.bottom.notebook.select(
                self.bottom.notebook.tabs()[4])  # 「审批」= 第 5 个（index 4）
        except Exception:
            try:
                self.bottom.select('审批')
            except Exception:
                pass

    def _ensure_knowledge_view(self):
        """注入知识库视图（懒构造，KnowledgeBase 在缺包时退化为提示）。"""
        if getattr(self, '_knowledge_view', None) is not None:
            return
        try:
            from .knowledge_view import KnowledgeView
            view = KnowledgeView(self.explorer._knowledge_tab, home=self.home)
        except Exception:
            from .knowledge_view import KnowledgePlaceholder
            view = KnowledgePlaceholder(self.explorer._knowledge_tab)
        self._knowledge_view = view
        try:
            self.explorer.install_knowledge_view(view)
        except Exception:
            pass

    def _ensure_evolution_view(self):
        if getattr(self, '_evolution_view', None) is not None:
            return
        try:
            from .evolution_view import EvolutionView
            view = EvolutionView(self.explorer._evolution_tab, home=self.home)
        except Exception:
            from .evolution_view import EvolutionPlaceholder
            view = EvolutionPlaceholder(self.explorer._evolution_tab)
        self._evolution_view = view
        try:
            self.explorer.install_evolution_view(view)
        except Exception:
            pass

    def _safe_status(self, message, *, level='info'):
        try:
            self._set_status(message, level)
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
        self._refresh_after_id = self.later(delay_ms, _fire)

    def refresh_project(self):
        if not self.source:
            return
        if self._scan_busy:
            self._scan_pending = True
            return
        self._scan_busy = True
        source, expanded, search = self.source, set(self.expanded), self.search.get()

        def _scan_done(value, error):
            self._project_scan(value, error)
            # M5：扫描完成即徽标数据（Git 变更文件数）的到位点——异步
            # 扫描完成后同步底部面板计数徽标，避免开箱看到 0（标签切换
            # 时面板另有一次自行刷新，双路径不互斥）。
            self.bottom.refresh_badges()
        self.work(lambda: source.scan(expanded, search), _scan_done)

    def _status_written(self, *_):
        """Track semantic notices from every existing status writer on the Tk thread."""
        if not self._status_background_write:
            self._status_notice_at = time.monotonic()

    def _set_project_status(self, text):
        # A delayed scan must not erase a refusal/error/save notice just shown.
        # Background writes do not renew the notice's two-second window.
        if time.monotonic() - self._status_notice_at < 2.0:
            return
        self._status_background_write = True
        try:
            self.status.set(text)
        finally:
            self._status_background_write = False

    def _project_scan(self, value, error):
        self._scan_busy = False
        if not error:

            # M3：文件树 / Git 变更清单的渲染移入 explorer.ExplorerPanel
            # （Git 状态语义化 + 文件类型图标）；选择恢复、展开态、过滤
            # 语义与既有实现一致。F-1 修复：语义提示经 _set_project_status
            # 写入（2 秒保护窗），扫描完成不覆盖用户提示。
            self.explorer.populate(value, self.source.root,
                                    changes_selection=getattr(self, '_changed_selection', ()))
            self._set_project_status(f"{value['branch']} · {self.source.root.name} · {len(value['status'])} 个 Git 变更 · {self.permission.get()}")
            # M1：多字段状态栏的 Git/项目字段随扫描结果同步（扫描是
            # 打开项目与刷新的共同完成点，一处接线覆盖两种入口）。
            self._refresh_status_fields(value)
        if self._scan_pending:
            self._scan_pending = False
            self.refresh_project()

    def _refresh_status_fields(self, scan=None):
        """M1：把 Git / Agent / Model / Project 四字段推送到多字段状态栏。

        单一推送点（_project_scan 完成回调）。Agent/Model 取运行时与
        状态层的真实值，不读 Agent 栏控件（Codex 域）——模型取
        state.value('roles') 的 default 角色在 catalog 里的标签，
        与「模型角色」窗口同源。任何一步取不到都退回占位，不抛错。
        """
        bar = getattr(self, 'status_bar', None)
        if bar is None:
            return
        git, project = '未打开项目', '—'
        if self.source:
            project = self.source.root.name
            branch = (scan or {}).get('branch') or '非 Git 仓库'
            changed = len((scan or {}).get('status') or {})
            git = f'{branch} · {changed} 处变更' if changed else f'{branch} · 干净'
        agent = '—'
        try:
            if self.runtime.ready:
                agent = self.runtime.bridge.agent.name
        except Exception:
            pass
        model = '继承'
        try:
            pair = tuple((self.state.value('roles') or {}).get('default') or ())
            if pair:
                for row in self.catalog_rows:
                    if self.runtime.catalog.get(row['id']) == pair:
                        model = row['label']
                        break
        except Exception:
            pass

        # v0.2 状态栏扩展字段：能力计数 / 布局名
        capability = self._capability_count_text()
        layout = self._layout_name_text()
        bar.update(git=git, agent=agent, model=model, project=project,
                   capability=capability, layout=layout,
                   **self._editor_status_fields())

    def _capability_count_text(self):
        """能力数：builtin + contrib + plugin 三类合计。

        真实值在 _init_tools/build_agent 完成后可读；这里取 best-effort
        —— CapabilityLibrary / Marketplace 都可能不可用，回落 "未就绪"。
        """
        try:
            from forge.tools import BUILTIN_TOOL_NAMES
            builtin = len(BUILTIN_TOOL_NAMES)
        except Exception:
            builtin = 0
        contrib = 0
        try:
            from forge.registry import ModuleRegistry
            # 避免实际 mount（只读 count）；保守取 catalog_rows 长度。
            contrib = len(getattr(self, 'catalog_rows', []) or [])
        except Exception:
            pass
        plugin = 0
        try:
            from pathlib import Path
            from forge.home import forge_home
            m_dir = Path(forge_home()) / 'plugins'
            if m_dir.is_dir():
                plugin = sum(1 for c in m_dir.iterdir() if c.is_dir())
        except Exception:
            pass
        if not (builtin or contrib or plugin):
            # 启动早期（catalog_rows 还没填，plugins 目录可能未建）——
            # 直接报"未就绪"而不是 "—"，避免状态栏出现无意义的占位
            return '未就绪'
        return f'{builtin} (builtin) + {contrib} (contrib) + {plugin} (plugin)'

    def _layout_name_text(self):
        try:
            value = self.state.layout
            preset = value.get('preset') if isinstance(value, dict) else None
        except Exception:
            preset = None
        if not preset:
            return '默认'
        # 中文映射
        names = {'agent': 'Agent 优先', 'editor': '编辑器优先',
                 'focus': '专注'}
        return names.get(preset, preset)

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
        # M4：编辑器本体换为 EditorArea（editor_area.py）——行号槽 /
        # 当前行高亮 / 搜索替换 / gutter 修改标记 / 分屏参考镜像 /
        # Python 语法高亮。row['widget'] 契约不变：EditorArea.text
        # 就是那个 Text，既有 save/reload/close/attach 全部无感；
        # 行为回调（修改 / Ctrl+S）经构造参数注入。
        frame = ttk.Frame(self.tabs)
        row = {'document': document, 'frame': frame, 'widget': None,
               'saving': False, 'area': None}
        area = EditorArea(frame, document,
                          on_change=lambda _a: self.document_changed(row),
                          on_save=self.save_current,
                          # M7：光标移动 → 状态栏「行 n, 列 n」字段。
                          # 闭包捕获 row（构造前已绑定）而非 area（构造
                          # 返回值，参数表达式里尚不存在——默认参数写法
                          # 会 UnboundLocalError）。
                          on_cursor=lambda: self._on_editor_cursor(row))
        area.pack(fill='both', expand=True)
        row['widget'] = area.text
        row['area'] = area
        # d56caab 语法高亮保留：EditorArea 的 Text 同样上色。
        setup_code_tags(area.text, bg=C.get('code_bg'))
        if document.path.suffix.lower() == '.py':
            highlight_python(area.text)

        self.documents[key] = row
        self.tabs.add(frame, text=document.path.name)
        self.tabs.select(frame)
        self.state.remember_tabs(self.documents)
        # M7：新标签成为当前文档——编码/换行/语言/光标全量刷新一次
        # （on_cursor 只推光标字段；set_text 的首帧通知早于此赋值，
        # 这里补全量推送保证四字段与新文档一致）。
        self._sync_editor_status()

    def current_row(self):
        selected = self.tabs.select()
        return next((r for r in self.documents.values() if str(r['frame']) == selected), None)

    def document_changed(self, row):
        widget = row['widget']
        if widget.edit_modified() and not row['saving']:
            row['document'].update(widget.get('1.0', 'end-1c'))
            widget.edit_modified(False)
            if row['document'].path.suffix.lower() == '.py':
                highlight_python(widget)
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
            # M7：无打开文件——编辑器状态组回占位。
            self._sync_editor_status()
            return
        d = row['document']
        labels = {'clean': '未修改', 'modified': '未保存', 'saved': '保存成功', 'failed': '保存失败', 'conflict': '外部变更冲突'}
        self.document_state.configure(text=labels[d.status] + ' · ' + d.encoding + ' · Ctrl+S 保存')
        # M7：标签切换——编码/换行/语言/光标随新当前文档刷新。
        self._sync_editor_status()

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
                # M4：保存后基线变化——刷新 gutter 修改标记。
                area = row.get('area')
                if area is not None:
                    area.refresh_markers()
                self.refresh_project()
        self.work(row['document'].save, complete)

    def save_as_current(self):
        """M4：另存为——当前缓冲区写到用户选择的新路径，并把文档改绑
        到新文件（VS Code 语义：另存为后当前标签编辑新文件）。

        经 boundary 校验（项目外 / .git 内拒绝）；目标已被其它标签
        打开时拒绝（避免两个标签同一路径）；成功后 documents 按键
        迁移、remember_tabs 持久化新路径、刷新文件树。
        """
        row = self.current_row()
        if not row or row['saving']:
            return
        self.document_changed(row)
        document = row['document']
        path = filedialog.asksaveasfilename(
            parent=self, initialdir=str(self.source.root),
            initialfile=document.path.name, title='另存为')
        if not path:
            return
        try:
            target = self.source.boundary.path(path)
        except Exception as exc:
            self.status.set(redact(str(exc)))
            return
        target_key = str(target)
        if target_key in self.documents and self.documents[target_key] is not row:
            self.status.set('目标文件已在标签页中打开：' + target.name)
            return
        row['saving'] = True
        row['widget'].configure(state='disabled')

        def complete(value, error):
            row['saving'] = False
            row['widget'].configure(state='normal')
            if error:
                self.status.set('另存为失败：' + redact(str(error)))
                self.update_tab(row)
                return
            old_key = next((k for k, r in self.documents.items() if r is row),
                           None)
            new_key = str(row['document'].path)
            if old_key is not None and old_key != new_key:
                del self.documents[old_key]
                self.documents[new_key] = row
            area = row.get('area')
            if area is not None:
                area.refresh_all()
            self.update_tab(row)
            self.status.set('已另存为 ' + row['document'].path.name)
            self.state.remember_tabs(self.documents)
            self.refresh_project()
        self.work(lambda: save_document_as(self.source.boundary, document,
                                           target), complete)

    def split_current(self):
        """M4：分屏按钮——切出当前文档的只读参考镜像（同文件双视图）。"""
        row = self.current_row()
        if row and row.get('area') is not None:
            row['area'].toggle_reference()
        else:
            self.status.set('没有打开的文档可分屏')

    def _editor_more_menu(self):
        """M4：标签页右侧「更多」按钮——真实动作菜单。"""
        row = self.current_row()
        if not row:
            self.status.set('尚无打开的文件')
            return
        menu = tk.Menu(self, tearoff=False, bg=C['bg'], fg=C['text'])
        menu.add_command(label='另存为…', command=self.save_as_current)
        menu.add_command(label='保存', command=self.save_current)
        menu.add_command(label='重新载入', command=self.reload_current)
        menu.add_command(label='关闭标签', command=self.close_current)
        menu.add_separator()
        menu.add_command(label='复制文件路径', command=self._copy_current_path)
        x = self.more_button.winfo_rootx()
        y = self.more_button.winfo_rooty() + self.more_button.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _copy_current_path(self):
        row = self.current_row()
        if not row:
            return
        self.clipboard_clear()
        self.clipboard_append(str(row['document'].path))
        self.status.set('已复制路径 ' + row['document'].path.name)

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
                # M4：重载后同步高亮 / 行号槽 / 修改标记 / 镜像。
                area = row.get('area')
                if area is not None:
                    area.refresh_all()
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
        # M7：关闭后同步文档状态与状态栏编辑器字段——关的是最后一个
        # 标签时 Notebook 不产生 <<NotebookTabChanged>>，需显式刷新
        # （还有标签时该事件也会触发，双保险幂等）。
        self.show_document_state()

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
        # M4：缓冲区整体替换后同步高亮 / 行号槽 / 修改标记 / 镜像。
        area = row.get('area')
        if area is not None:
            area.refresh_all()
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

    def _sync_queue_label(self):
        """队列标签：空队列隐藏；有预存则显示计数与首条摘要。"""
        n = len(getattr(self, '_pending_prompts', ()))
        label = getattr(self, '_queue_label', None)
        if label is None:
            return
        if not n:
            label.pack_forget()
            return
        first = self._pending_prompts[0]['prompt']
        first = redact(first)
        summary = (first[:24] + '…') if len(first) > 24 else first
        label.configure(text=f'⏳ 预存 {n}/3：{summary}' + (' 等' if n > 1 else ''))
        # 输入行正上方（before=hint：side='bottom' 列表中 hint 在
        # input_row 之上，插到 hint 前即贴着输入行）
        try:
            label.pack(side='bottom', fill='x', padx=8, pady=(2, 0),
                       before=self._input_hint)
        except tk.TclError:
            pass

    def _submission_notice(self, text, *, error=False):
        """A send outcome must be visible next to the draft, not only in a status bar."""
        text = redact(str(text))
        self.status.set(text)
        self.log_tabs.select(self.chat.master)
        self.chat.message('无法发送' if error else '发送状态', text)
        # A queued/refused draft is a notification, not a state transition of
        # the Agent already running. Its status remains owned by execution events.

    def _update_send_button(self):
        pending = self._submission_active or self.runtime.busy or bool(self._pending_prompts)
        self.send_button.configure(state='disabled' if self._loading else 'normal',
                                   text='排队发送' if pending else '发送')

    def _schedule_queued_send(self):
        self._update_send_button()
        if self._closed or not self._pending_prompts or self._queued_send_id is not None:
            return
        def advance():
            self._queued_send_id = None
            if self.runtime.busy or self._loading or self._submission_active:
                self._schedule_queued_send()
                return
            request = self._pending_prompts.pop(0)
            self._sync_queue_label()
            self._dispatch(request['prompt'], request['delegate'], request=request, clear_input=False)
        self._queued_send_id = self.later(300, advance)

    def send(self, *, delegate=False):
        prompt = self.input.get('1.0', 'end-1c').strip()
        if not prompt:
            return
        if not self.runtime.ready or self._loading:
            self._submission_notice('项目正在载入，请稍后发送。' if self._loading else
                'Agent 尚未就绪，请先打开项目；载入失败时检查模型配置后重新打开。', error=True)
            return
        try:
            request = self._capture_submission(prompt, delegate)
        except Exception as exc:
            self._submission_notice('任务未创建：' + str(exc), error=True)
            return
        # busy → 预存队列（上限 3，FIFO）；任务完成回调后自动发送。
        if self.runtime.busy or self._submission_active or self._pending_prompts:
            if len(self._pending_prompts) >= 3:
                self._submission_notice('排队已满（3/3），草稿已保留；请等当前任务完成。', error=True)
                return
            self._pending_prompts.append(request)
            self.input.delete('1.0', 'end')
            self.editor_contexts = []
            self.context_label.configure(text='未附加编辑器内容')
            self._sync_queue_label()
            self._submission_notice(f'已排队（{len(self._pending_prompts)}/3），当前任务结束后发送。')
            self._schedule_queued_send()
            return
        self._dispatch(prompt, delegate, request=request)

    def _capture_submission(self, prompt, delegate):
        """Freeze validated next-task choices on the Tk thread before consuming a draft."""
        if len(prompt.encode('utf-8')) > 16384:
            raise ValueError('任务描述不能超过 16 KiB；请缩短描述或附加文件。')
        # v0.3：模型来源由 RuntimeContext.模型路由.primary 决定——没有
        # model_choice 时直接回落 RoutingChain 的 primary。
        model = None
        if getattr(self, '_runtime_ctx', None) is not None:
            primary_label = self._runtime_ctx.get_routing().get('primary', '') or ''
            if primary_label not in ('', '继承配置'):
                row = next((r for r in self.catalog_rows if r.get('label') == primary_label), None)
                model = self.runtime.catalog.get(row['id']) if row else None
                if model is None:
                    raise ValueError('所选模型已失效，请重新选择模型或重新打开项目。')
        if model is None and getattr(self, 'model_choice', None) is not None:
            index = self.model_choice.current()
            model = self.runtime.catalog.get(self.model_ids[index]) if index > 0 else None
        permission, planning = self.permission.get(), self.planning.get()
        from forge.planning import planning_level
        planning_level(planning)
        contexts = []
        for path in self.editor_contexts:
            row = self.documents.get(path)
            if row:
                self.document_changed(row)
                contexts.append({'path': path, 'content': row['widget'].get('1.0', 'end-1c')})
        from forge.vscode_bridge import MAX_CONTEXT
        if len(contexts) > 4 or sum(len(c['content'].encode('utf-8')) for c in contexts) > MAX_CONTEXT:
            raise ValueError('最多附加四个文件，合计不能超过 128 KiB；请减少附加内容。')
        role = self.task_role.get()
        snapshot = self.state.task_snapshot(explicit_model=model, role=role, permission=permission)
        return dict(prompt=prompt, delegate=delegate, model=model, permission=permission,
                    planning=planning, role=role, contexts=contexts, snapshot=snapshot,
                    generation=self._generation, cancellation=threading.Event())

    def _dispatch(self, prompt, delegate, *, request=None, clear_input=True):
        try:
            request = request if request is not None else self._capture_submission(prompt, delegate)
            if request['generation'] != self._generation:
                raise ValueError('项目已变化，任务未执行；请在原项目重新提交。')
        except Exception as exc:
            self._submission_notice(str(exc), error=True)
            self._schedule_queued_send()
            return
        model, permission, planning = (request[k] for k in ('model', 'permission', 'planning'))
        role, contexts, snapshot = (request[k] for k in ('role', 'contexts', 'snapshot'))
        # v0.3：run_model 老 label 已被 RuntimeContext 取代；只在兼容占位
        # 存在时更新。
        if getattr(self, 'run_model', None) is not None:
            try:
                self.run_model.configure(
                    text='运行模型：' + (' / '.join(snapshot.model) if snapshot.model else '全局路由'))
            except Exception:
                pass
        self.agent_panel.task_context = {'planning': planning if snapshot.mode == WorkMode.DEVELOPMENT else 'none', 'permission': snapshot.permission,
                                            'model': snapshot.model}
        # 重复的 run_model.configure 删除（已在上面安全更新过）。
        self._submission_active = True
        self._active_submission = request
        self._update_send_button()
        # delegate_button 已被 RuntimeContext 接管；状态由 RuntimeContext
        # 任务控制行内部管理（默认 enabled，不再 disable）。
        self.task_evidence = TaskEvidence()
        self.render_evidence()
        if clear_input:
            self.input.delete('1.0', 'end')
        # v0.4：派发即切到 running 态——done 回调里再切终态。
        self.set_agent_status('running', f'运行中 · 任务 #{self._generation}')
        if clear_input:
            self.editor_contexts = []
            self.context_label.configure(text='未附加编辑器内容')
        self._retry_request = {'prompt': prompt, 'delegate': delegate}
        self.task_label.configure(text='Agent · 请求执行')
        self._submission_notice('已提交，正在创建 Agent 任务…')
        def done(value, error):
            self._submission_active = False
            self._active_submission = None
            self._update_send_button()
            if not error and not isinstance(value, dict):
                error = '运行时没有返回有效任务结果；请检查完整事件。'
            # delegate_button 在 v0.3 已被 RuntimeContext 接管；同步更新
            # 其 disabled 状态。
            if getattr(self, '_runtime_ctx', None) is not None:
                try:
                    task = self._runtime_ctx.get_task_controls()
                    # 找到「创建子 Agent」按钮，遍历 TaskControls 的子控件
                    # 直接通过属性路径不可达；走 on_delegate 间接校验。
                    # 这里只重置状态：RuntimeContext 按钮默认总是可点。
                except Exception:
                    pass
            self.task_label.configure(text='Agent · 失败' if error else 'Agent · ' + str(value.get('stopped', '完成')))
            # v0.4：done 回调切终态。error → error；否则按 stopped 文本切
            # idle / running（running 表示停在某个中间状态，idle 表示
            # 干净完成——这里用「完成」即可视作 idle）。
            if error:
                self.set_agent_status('error', f'失败 · {str(error)[:80]}')
            else:
                stopped = str(value.get('stopped', '完成'))
                self.set_agent_status('idle', f'就绪 · 上次任务：{stopped}')
            if error:
                if not self.input.get('1.0', 'end-1c').strip():
                    self.input.insert('1.0', prompt)
            elif value.get('text'):
                self.chat.message('Coding Agent', value['text'])
            if error:
                self.chat.message('执行失败', error)
            else:
                self.chat.message('任务结果', '状态：' + str(value.get('outcome', value.get('stopped', '未知'))))
            self.refresh_agent_versions()
            self.refresh_project()
            self.refresh_documents()
            self._schedule_queued_send()
        try:
            self.work(lambda: self.runtime.run(prompt, explicit_model=model, permission=permission,
                                            planning=planning, contexts=contexts, role=role, snapshot=snapshot,
                                            delegate=delegate, cancellation=request['cancellation']), done)
        except Exception as exc:
            self._submission_active = False
            self._active_submission = None
            if clear_input and not self.input.get('1.0', 'end-1c').strip():
                self.input.insert('1.0', prompt)
            self._submission_notice('任务未启动：' + str(exc), error=True)
            self._schedule_queued_send()

    def render_evidence(self):
        # One idle update per event burst; events themselves are never dropped.
        if getattr(self, '_agent_render_pending', False):
            return
        self._agent_render_pending = True
        def render():
            self._agent_render_pending = False
            if (self.runtime.last_snapshot is None and self.runtime._resume_evidence
                    and self.task_evidence.task_id
                    and self.task_evidence.task_id == self.runtime._resume_evidence.get('task_id')):
                self.agent_panel.task_context = {'recovered': True}
                plan = next((e.get('plan', {}) for e in reversed(self.runtime._resume_evidence['events'])
                                if e.get('type') == 'task_plan' and e.get('task_id') == self.task_evidence.task_id
                                and not e.get('depth', 0)), {})
                self.agent_panel.task_context['planning'] = plan.get('level')
            main = next((r for r in self.task_evidence.runs.values() if not r.get('depth', 0) and r.get('model')), None)
            if main:
                # run_model 已被 RuntimeContext 取代；不再更新老 label。
                if getattr(self, 'run_model', None) is not None:
                    self.run_model.configure(text='实际模型 · ' + str(main['model']))
            elif not self.task_evidence.task_id:
                self._agent_model_selected()
            self.agent_panel.render(self.task_evidence)
            self.task_label.configure(text='Agent · ' + self.task_evidence.phase)
        self.later(0, render)

    def handle_event(self, event, error=None):
        if event.get('event') == 'user':
            self.chat.message('你', event.get('display', ''), timestamp=event.get('ts'))
            return
        data = event.get('data', event)
        if data.get('type') == 'development_task_started' and data.get('task_id') != self.task_evidence.task_id:
            self.task_evidence = TaskEvidence()
            context = self.agent_panel.task_context
            if context.get('recovered') or context.get('task_id'):
                self.agent_panel.task_context = {}
            self.agent_panel.task_context['task_id'] = data.get('task_id')
        same_task = (not self.task_evidence.task_id or not data.get('task_id')
                        or data['task_id'] == self.task_evidence.task_id)
        if same_task and data.get('type') == 'task_plan' and not data.get('depth', 0):
            self.agent_panel.task_context['planning'] = data.get('plan', {}).get('level')
        elif same_task and data.get('type') == 'development_recovery':
            self.agent_panel.task_context = {}
        self.task_evidence.apply(data)

        self.render_evidence()
        self.agent_panel.events.append_event(data)
        # M7：就绪态与问题计数随关键事件实时刷新——任务开始/结束 →
        # runtime 忙闲（状态栏 ready 字段 + 圆点）；失败记录类事件 →
        # 底部 Problems 徽标与状态栏 problems 字段（同源
        # count_problems，与扫描周期刷新合并去重）。
        if data.get('type') in {'development_task_started',
                                'development_task_finished', 'test_started',
                                'test_result', 'review_started',
                                'review_result', 'review_error',
                                'review_cancelled', 'tool_completed'}:
            self._sync_ready_status()
            bottom = getattr(self, 'bottom', None)
            if bottom is not None:
                bottom.refresh_badges()
            bar = getattr(self, 'status_bar', None)
            if bar is not None:
                bar.update(problems=count_problems(self.task_evidence))
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
        queued = len(self._pending_prompts)
        for request in self._pending_prompts:
            request['cancellation'].set()
        self._pending_prompts.clear()
        self._sync_queue_label()
        if self._queued_send_id is not None:
            self.after_cancel(self._queued_send_id)
            self._after_ids.discard(self._queued_send_id)
            self._queued_send_id = None
        if self._active_submission is not None:
            self._active_submission['cancellation'].set()
        if self.runtime.cancel() or self._submission_active:
            self._submission_notice('已请求取消，排队任务已清除；等待当前模型或工具返回。已写入文件会保留。')
        elif queued:
            self._submission_notice(f'已取消 {queued} 条排队任务，当前草稿保留。')
        else:
            self._submission_notice('当前没有可取消的任务。')
        self._update_send_button()

    def run_tests(self):
        if (self.runtime.busy or self._loading or self._submission_active or self._pending_prompts
                or self.state.mode != WorkMode.DEVELOPMENT):
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
        if (self.runtime.busy or not self.runtime.ready or self._loading
                or self._submission_active or self._pending_prompts):
            self.status.set('请先完成任务')
            return
        self.work(self.runtime.review, lambda v, e: self.write_output('输出', json.dumps(v, ensure_ascii=False, indent=2) + '\n') if not e else None)

    def restore_session(self, path=None):
        if (not self.runtime.ready or self.runtime.busy or self._loading
                or self._submission_active or self._pending_prompts):
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
        task_row = ttk.Frame(window, padding=(12, 4))
        task_row.pack(fill='x')
        ttk.Label(task_row, text='后续任务角色', width=27).pack(side='left')
        ttk.Combobox(task_row, textvariable=self.task_role, values=['default', 'complex', 'simple'],
                        state='readonly', width=30).pack(side='left')
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

    def agent_model_settings(self, selected_name=None):
        if not self.runtime.ready:
            self.status.set('请先打开项目，读取已配置模型')
            return
        parent_name = self.runtime.bridge.agent.name
        if selected_name in {parent_name, 'Review Agent'}:
            self.model_settings()
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
        name.set(selected_name or parent + '/sub1')
        model = ttk.Combobox(window, values=['继承角色', *[r['label'] for r in self.catalog_rows]],
                                state='readonly', width=44)
        model.pack(anchor='w', padx=12, pady=6)
        def show(event=None):
            pair = tuple(self.state.value('agent_models').get(name.get()) or ())
            index = next((i + 1 for i, row in enumerate(self.catalog_rows)
                                if self.runtime.catalog[row['id']] == pair), 0)
            model.current(index)
            if pair and not index:
                feedback.configure(text='配置已失效：模型不在当前目录中；请选择或恢复继承')
            else:
                feedback.configure(text='下次任务来源：独立配置' if pair else '下次任务来源：继承子 Agent 角色 / 主模型')
        feedback = ttk.Label(window, text='', padding=12)
        feedback.pack(anchor='w')
        name.bind('<<ComboboxSelected>>', show)
        name.bind('<FocusOut>', show)
        show()
        def save():
            try:
                if name.get().strip() in {parent, 'Review Agent'}:
                    raise ValueError('主 Agent / Review Agent 请使用模型角色配置；此弹窗只配置真实子 Agent 名称')
                index = model.current()
                pair = self.runtime.catalog[self.catalog_rows[index - 1]['id']] if index > 0 else None
                self.state.configure_agent_model(name.get().strip(), pair)
            except (ValueError, KeyError) as exc:
                feedback.configure(text=redact(str(exc)))
                return
            feedback.configure(text='已保存 · 当前任务不变，下次真实 spawn 时生效')
            self.status.set('子 Agent 独立模型已保存')
        ttk.Button(window, text='保存', command=save).pack(anchor='e', padx=12, pady=12)

    def open_agent_file(self, path):
        if not self.source:
            self.status.set('请先打开项目')
            return
        import re
        path = re.sub(r':\d+(?:-\d+)?$', '', path)
        try:
            self.open_file(self.source.boundary.path(path))
        except (ValueError, PermissionError, OSError) as exc:
            self.status.set(redact(str(exc)))

    def view_agent_diff(self, change):
        diff = change.get('diff')
        if diff is not None and change.get('diff_truncated'):
            diff += '\n[安全 Diff 预览已截断；可在 Git 面板查看当前工作区实况]\n'
        self.write_output('Diff', diff if diff is not None else
            '没有安全的文本预览；此状态不代表补丁已应用。请查看执行详情或工作区 Git Diff。', clear=True)
        self.bottom.select('Diff')  # v0.2：按页名 select（ToolPanel.select 兼容旧别名，内部选中 notebook）

    def revert_agent_change(self, change):
        if change.get('state') != 'written' or not change.get('checkpoint') or change.get('revision_changed'):
            self.status.set('此版本无法直接恢复，请重新审查检查点')
            return
        def previewed(preview, error):
            if error:
                return
            if preview['expected'] != change.get('revision'):
                self.status.set('磁盘已变化，拒绝恢复旧卡片版本')
                return
            from .bottom_panel import PAGE_ALIASES
            page = PAGE_ALIASES.get('Diff', 'Diff')
            self.write_output('Diff', preview['diff'], clear=True)
            self.bottom.select(page)
            self.restore_reviewed_checkpoint(preview)
        self.work(lambda: self.runtime.checkpoint_preview(change['checkpoint'], change['path']), previewed)

    def resolve_agent_approval(self, change):
        self.agent_panel.notebook.select(0)
        self.status.set('该动作未执行。通过权限控件明确授权后，用重试提交新任务；未知动作不会自动重放。')
        self.input.focus_set()

    def retry_agent_task(self):
        request = getattr(self, '_retry_request', None)
        if not request or self.runtime.busy or self._loading or self._submission_active or self._pending_prompts:
            self.status.set('没有可重试任务，或仍在执行')
            return
        if self.task_evidence.phase in {'interrupted', 'unknown'} or any(
                r['status'] == 'unknown' for r in self.task_evidence.actions.values()):
            self.status.set('上次结果未知，请先检查磁盘和 Diff，再明确输入新任务')
            return
        draft = self.input.get('1.0', 'end-1c').strip()
        if draft and draft != request['prompt']:
            self.status.set('输入框已有草稿，未覆盖；请自行发送')
            return
        if not draft:
            self.input.insert('1.0', request['prompt'])
        self.send(delegate=request['delegate'])

    def refresh_agent_versions(self):
        if not self.runtime.ready or self.runtime.busy or not self.runtime.evidence.changes:
            return
        self.work(self.runtime.refresh_change_states, lambda value, error: None)

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
        if preset == 'review':
            # M6：review 预设的分数存独立槽 review_fractions（集成层
            # 81acf1b 解锁的字段；裁决 2：避免与 agent/editor 共享
            # fractions 互相污染）。无存档用 PRESETS 默认权重；写入时
            # fractions 与 review_fractions 同值——place_sashes 读
            # fractions，capture_layout 在 review 下把用户拖拽结果
            # 同时写回两个键。
            layout = self.state.layout
            fractions = list(layout.get('review_fractions')
                             or PRESETS['review']['fractions'])
            self.state.set_layout(preset='review', fractions=fractions,
                                  review_fractions=fractions, visible=True)
        elif preset in PRESETS:
            fractions = list(PRESETS[preset]['fractions'])
            self.state.set_layout(preset=preset, fractions=fractions, visible=True)
        else:
            fractions = [.17, .49] if preset == 'agent' else [.17, .76]
            self.state.set_layout(preset=preset, fractions=fractions, visible=True)
        self.apply_layout()
        label = PRESETS.get(preset, {}).get('label', preset)
        self.status.set(f'布局预设已切换 · {label}')

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
        elif layout['preset'] in PRESETS:
            # M6：wanted 集合由 layout.py 的 PRESETS 表驱动（三预设
            # 共享同一套组件，差异只在权重与内容侧重）。
            frames = {'project': self.project_frame, 'editor': self.editor_frame,
                      'agent': self.agent_frame}
            panels = [frames[key] for key in PRESETS[layout['preset']]['wanted']]
        else:
            panels = [self.project_frame, self.editor_frame, self.agent_frame]
        wanted = list(panels)
        # panes.panes() 返回的是窗口路径名**字符串**，与控件对象比较恒为
        # False。那会让下面两处判断同时失效：「已在集合则 continue」永不
        # 命中、「不在集合才 forget」对每个在装面板都成立——结果是每次
        # apply_layout 都走全量 forget+add（51f6f82 要消除的闪白路径
        # 复活），且 insert 的目标位计数恒为 0，三个面板依次插到队首，
        # 顺序反转成 agent|editor|project；fractions 的「sash0=project
        # 右界」语义随之错位，agent 栏被压到 20%（实测 1200px 下仅
        # 236px，栏内模型组横向放不下）。先统一换算成控件对象再比较。
        live = [self.panes.nametowidget(name) for name in self.panes.panes()]
        # 保序移除：只摘不在目标集合里的
        mounted = False
        for pane in live:
            if pane not in wanted:
                self.panes.forget(pane)
                mounted = True
        # forget 后必须重读：此时 live 里还留着刚被摘掉的面板，len(live)
        # 会大于真实 slave 数。若不重读，下面的 idx < len(live) 会拿过期
        # 上界去 insert，而 insert 只接受 0 <= pos < 真实 slave 数——
        # focus 预设连续 forget 到 slave 归零时就会抛 Slave index 0 out
        # of bounds（toggle_focus 从 editor 切到 agent 即触发）。
        live = [self.panes.nametowidget(name) for name in self.panes.panes()]
        # 增量挂载：逐个补挂 wanted 中缺失的面板。每挂一个都重读
        # 实际列表——insert 的 pos 按当前实际列表计位：目标位置 =
        # 实际列表中、wanted 里排在它前面的面板个数。
        # 注意 ttk::panedwindow 的 insert 只接受 0 <= pos < n（n 为当前
        # pane 数），pos == n 会抛 Slave index out of bounds；要追加到
        # 队尾必须走 add。原代码写 min(idx, len(live)) 本意是「越界贴边」，
        # 但贴出的上界值本身非法，只因 idx 恒为 0 而从未触发。
        for panel in wanted:
            if panel in live:
                continue
            idx = sum(1 for m in wanted[:wanted.index(panel)] if m in live)
            if idx < len(live):
                self.panes.insert(idx, panel, weight=1)
            else:
                self.panes.add(panel, weight=1)
            live = [self.panes.nametowidget(name) for name in self.panes.panes()]
            mounted = True
        # M1-fix（验收打回项 test_focus_toggle_contract_survives_button_removal
        # 环境敏感回归）：ttk::panedwindow 的 add/insert **不同步映射 slave**
        # ——slave 的映射发生在随之排队的延迟重排里。若紧随其后的事件循环
        # 预算被 OS 窗口突发占满（无控制台/后台会话实测：step() 的 16 次
        # dooneevent 全部耗在其他事件上、队列未排空），重排不执行，新挂载的
        # 面板保持 winfo_ismapped()==0——toggle_focus 后 agent 栏连带
        # input 全部 unmapped，而几何完全正确（高度停在最后一次成功重排的
        # 值，如 427）。基线同路径事件少、16 预算够用，只表现为偶发抖动；
        # M1 的竖向 pane + 底部面板同步 + 额外 sash 写入拉长了事件级联，
        # 把抖动变成后台会话必现。挂载集合发生变化时立即冲刷 idle 任务
        # （只服务 idle 回调、不派发窗口事件，不触发 Configure 自激环），
        # 让重排在 apply_layout 返回前同步落地——映射状态不再依赖外部
        # 事件循环预算。见 _settle_mount。
        # M1：底部工具面板随 IDE 展开/收起显隐。与三栏的增量挂载是两条
        # 独立路径——这里只做「在装/不在装」二态切换（vertical 内固定
        # panes+bottom 两个 slave），不碰 forget/insert 增量逻辑。
        if self._sync_bottom_panel(show):
            mounted = True
        if mounted:
            self._settle_mount()
        self.later(0, self.place_sashes)
        # M6：预设内容侧重——仅在实际切换预设时执行一次（preset 未变
        # 则不重置用户手动切过的视图/标签页；模式切换、IDE 收起/展开
        # 等路径不重复触发）。
        preset = layout['preset']
        if preset != getattr(self, '_emphasized_preset', None):
            self._emphasized_preset = preset
            self._apply_preset_emphasis()

    def _settle_mount(self):
        """把挂载变化的物理效果（slave 映射与重排）同步落地。

        根因与边界见 apply_layout 内注释：panedwindow add/insert 的
        slave 映射是延迟的，后台/无控制台会话里事件预算可能被 OS 窗口
        突发吃满，导致新挂载面板停留在 unmapped。update_idletasks 只
        服务 idle 回调（含 Tk 的延迟重排），不派发窗口事件，因此不会
        触发 _panes_resized→place_sashes 自激环；_settling 旗标防嵌套
        冲刷（重入 apply_layout 时，外层这一次冲刷会一并服务其重排）。
        """
        if getattr(self, '_settling', False):
            return
        self._settling = True
        try:
            self.update_idletasks()
        finally:
            self._settling = False

    def _sync_bottom_panel(self, show):
        """M1：IDE 收起时把底部工具面板移出外层竖向 pane，展开时补回。

        收起态下用户只看 Agent 栏，底部工具面板应当让位（与原结构
        一致：原输出 Notebook 在 editor_frame 内，editor_frame 不在装
        时它同样不可见）。补回用 add 追加到队尾，保持 [panes, bottom]
        的固定次序。同样注意 panes() 返回字符串，须 nametowidget 换算。
        返回 True 表示本次确实改变了挂载集合（调用方据此冲刷重排）。
        """
        if getattr(self, '_closed', False):
            return False
        changed = False
        try:
            live = [self.vertical.nametowidget(name)
                    for name in self.vertical.panes()]
            if show:
                if self.bottom not in live:
                    self.vertical.add(self.bottom, weight=1)
                    changed = True
            elif self.bottom in live:
                self.vertical.forget(self.bottom)
                changed = True
        except (tk.TclError, AttributeError):
            return False  # 构造早期 / 已销毁：无底部面板可同步
        return changed

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
            # M1：外层竖向 pane 固定 [三栏, 底部面板] 两个 slave；底部
            # 面板被收起（IDE 折叠）时只剩一个 slave，此时没有 sash 可写。
            if len(self.vertical.panes()) == 2 and self.vertical.winfo_height() > 40:
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
                # M6：review 预设下用户的 sash 拖拽结果同时写回独立槽
                # review_fractions（切到其他预设会覆盖 fractions，独立
                # 槽保证再切回 review 时用户的调整不丢失——81acf1b 裁决 2）。
                if self.state.layout['preset'] == 'review':
                    changes['review_fractions'] = fractions
        if height > 50 and len(self.vertical.panes()) == 2:
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


