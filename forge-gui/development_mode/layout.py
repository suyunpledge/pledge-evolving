"""M1 骨架 chrome：顶部导航栏 + 多字段状态栏（编辑器与布局域）。

本文件只放「骨架 chrome」两类组件：

* :class:`NavigationBar` —— 顶部导航栏（FORGE / Work / Coding /
  Project / Run / Settings）。模式项（Work/Coding）的激活态经
  StringVar trace 跟随 ``workbench.mode``，与 header 里的模式下拉
  **共享同一真源**，不引入第二套模式状态；动作项经 ``on_navigate``
  回调交还 workbench 执行，本类不持有业务方法，便于离线测试。
* :class:`StatusBar` —— 多字段状态栏（Git / Agent / Model /
  Project + 右侧瞬态消息）。瞬态消息沿用 workbench 既有的
  ``self.status`` StringVar（既有测试契约
  ``assertIn('等待', app.status.get())`` 依赖它），本栏只把它从
  「独占一行」改为「右侧一条」，文案与行为零变化。

三栏与底部工具面板的挂载仍走 ``app.py`` 的 ``apply_layout`` 增量
路径（51f6f82 保护清单：增量挂载 / 防重入旗标 / 2px 容差），本文件
**不引入第二条挂载路径**。

M6 的四预设定义（各 preset 的 wanted 集合与权重、review 的
fractions 语义）见下方 :data:`PRESETS`，供 ``apply_layout`` 调用。
"""
import tkinter as tk
from tkinter import ttk

from gui_theme import C, FONT_BRAND, FONT_CAPTION, FONT_UI, FONT_UI_BOLD, FONT_UI_MED

# 字段值截断上限：项目路径等可能很长，无上限会撑爆状态栏布局。
_VALUE_LIMIT = 48

# M6 三布局预设（任务书 §M6）：各预设的可见面板（wanted）与默认 sash
# 权重（fractions = [project 右界, editor 右界] 占 panes 宽的比例）。
#
# * Agent Focus（'agent'）：Agent 对话区扩大（右栏 51%），文件树紧凑
#   （17%），编辑器居中——任务状态/对话是焦点；
# * Code Focus（'editor'）：编辑器占主要空间（中栏 59%），文件树展开，
#   Agent 面板缩小（24%）——写代码是焦点；
# * Review Focus（'review'）：中央 Git Diff（中栏 52% 最大），左侧
#   修改文件列表（19%），右侧审查结果（29%，保留模型组所需宽度
#   ≥340px @1200）——审查是焦点。三预设 wanted 集合相同（同一套
#   工作区组件，不得三套 IDE），差异在权重与内容侧重（app 侧
#   _apply_preset_emphasis）。
#
# 'focus' 不在此表：它是二栏单视图模式（toggle_focus 驱动，wanted 由
# layout['focus'] 动态决定），既有的行为与测试契约原样保留。
PRESETS = {
    'agent': {'label': 'Agent Focus', 'wanted': ('project', 'editor', 'agent'),
              'fractions': (.17, .49)},
    'editor': {'label': 'Code Focus', 'wanted': ('project', 'editor', 'agent'),
               'fractions': (.17, .76)},
    'review': {'label': 'Review Focus', 'wanted': ('project', 'editor', 'agent'),
               'fractions': (.19, .71)},
}


def _clip(text):
    text = str(text)
    return text if len(text) <= _VALUE_LIMIT else text[:_VALUE_LIMIT - 1] + '…'


class NavigationBar(ttk.Frame):
    """顶部导航栏：品牌 + 模式切换（Work/Coding）+ 动作（Project/Run/Settings）。

    用 ``tk.Label`` 而非 ttk 按钮实现导航项：Label 的前景/字体可
    逐状态精确控制（激活 = accent_text + 加粗 + 底部 2px 指示条），
    不依赖 clam 主题的 Button 状态映射，跨主题表现确定。
    """

    MODES = (('general', 'Work'), ('development', 'Coding'))
    ACTIONS = (('project', 'Project'), ('run', 'Run'), ('settings', 'Settings'))

    def __init__(self, master, on_navigate, **kw):
        super().__init__(master, **kw)
        self._on_navigate = on_navigate
        self._mode_var = None
        self._mode_items = {}      # mode value -> (label, indicator)
        self._items = {}           # 全部可导航键 -> label（含模式项）
        self._active = None
        self._build()

    # ── 公开访问器（测试 / 验收探针用，不暴露私有结构）──────────────
    def item(self, key):
        """取任意导航项的 Label（键不存在返回 None）。"""
        return self._items.get(key)

    @property
    def active_mode(self):
        """当前激活的模式值（'general' / 'development' / None）。"""
        return self._active

    # ── 构建 ──────────────────────────────────────────────────────
    def _build(self):
        self.configure(padding=(12, 6, 12, 4))
        tk.Label(self, text='FORGE', font=FONT_BRAND, bg=C['bg'],
                 fg=C['accent_text']).pack(side='left', padx=(0, 14))
        for value, title in self.MODES:
            cell = tk.Frame(self, bg=C['bg'])
            cell.pack(side='left', padx=(0, 4))
            label = tk.Label(cell, text=title, font=FONT_UI_MED, bg=C['bg'],
                             fg=C['subtext'], cursor='hand2', padx=8, pady=4)
            label.pack(side='top')
            indicator = tk.Frame(cell, bg=C['bg'], height=2)
            indicator.pack(side='top', fill='x')
            label.bind('<Button-1>', lambda _e, key=value: self._navigate(key))
            label.bind('<Enter>', lambda _e, w=label: self._hover(w, True))
            label.bind('<Leave>', lambda _e, w=label: self._hover(w, False))
            self._mode_items[value] = (label, indicator)
            self._items[value] = label
        self._separator()
        for key, title in self.ACTIONS:
            label = tk.Label(self, text=title, font=FONT_UI_MED, bg=C['bg'],
                             fg=C['subtext'], cursor='hand2', padx=8, pady=4)
            label.pack(side='left')
            label.bind('<Button-1>', lambda _e, k=key: self._navigate(k))
            label.bind('<Enter>', lambda _e, w=label: self._hover(w, True))
            label.bind('<Leave>', lambda _e, w=label: self._hover(w, False))
            self._items[key] = label
        # 底部发丝线：把导航栏与下方 header 行在视觉上分层。
        tk.Frame(self, bg=C['border'], height=1).pack(side='bottom', fill='x')

    def _separator(self):
        tk.Frame(self, bg=C['border'], width=1).pack(
            side='left', fill='y', padx=8, pady=4)

    # ── 交互 ──────────────────────────────────────────────────────
    def _navigate(self, key):
        if self._on_navigate is not None:
            self._on_navigate(key)

    def _hover(self, label, entering):
        if self._active is not None and label is self._mode_items[self._active][0]:
            return  # 激活项不响应悬停换色（激活态优先）
        try:
            label.configure(fg=C['text'] if entering else C['subtext'])
        except tk.TclError:
            pass

    def bind_mode(self, var):
        """把导航栏的激活态绑到模式 StringVar（与 header 下拉同源）。"""
        self._mode_var = var
        var.trace_add('write', self._on_mode_write)
        self._apply_active(var.get())

    def _on_mode_write(self, *_args):
        try:
            self._apply_active(self._mode_var.get())
        except (tk.TclError, AttributeError):
            pass  # 窗口已销毁后的迟到回调，静默忽略

    def _apply_active(self, mode):
        if mode not in self._mode_items:
            return
        self._active = mode
        for value, (label, indicator) in self._mode_items.items():
            active = value == mode
            try:
                label.configure(fg=C['accent_text'] if active else C['subtext'],
                                font=FONT_UI_BOLD if active else FONT_UI_MED)
                indicator.configure(bg=C['accent'] if active else C['bg'])
            except tk.TclError:
                pass


class StatusBar(ttk.Frame):
    """多字段状态栏：Git / Agent / Model / Project + 右侧瞬态消息。

    瞬态消息（``status_var``，即 workbench 的 ``self.status``）保持
    既有行为：右对齐、由各处 ``self.status.set(...)`` 驱动。四字段
    由 :meth:`update` 推送，未打开项目时显示占位 ``—``。

    右侧扩展（v0.2 起）：
    * 「能力」—— capability_count = "19 (builtin) + 6 (contrib) + 3 (plugin)"
    * 「布局」—— layout_name（来自 state.layout['preset']，agent / editor / focus）

    能力 / 布局也支持 update() 推送，键名 ``capability`` / ``layout``；
    旧键名（git / agent / model / project）保持不变。
    """

    FIELDS = (('git', 'Git'), ('agent', 'Agent'),
              ('model', 'Model'), ('project', 'Project'))
    # 右侧扩展字段（与左侧字段同样支持 update()）。
    RIGHT_FIELDS = (('capability', '能力'), ('layout', '布局'))

    def __init__(self, master, status_var, **kw):
        super().__init__(master, **kw)
        self._vars = {}
        self._build(status_var)

    def _build(self, status_var):
        self.configure(padding=(12, 4, 12, 5))
        tk.Frame(self, bg=C['border'], height=1).pack(fill='x')
        row = tk.Frame(self, bg=C['bg'])
        row.pack(fill='x', pady=(3, 0))
        for key, title in self.FIELDS:
            var = tk.StringVar(value='—')
            self._vars[key] = var
            cell = tk.Frame(row, bg=C['bg'])
            cell.pack(side='left', padx=(0, 20))
            tk.Label(cell, text=title, font=FONT_CAPTION, bg=C['bg'],
                     fg=C['ter']).pack(side='left')
            tk.Label(cell, textvariable=var, font=FONT_CAPTION, bg=C['bg'],
                     fg=C['text']).pack(side='left', padx=(4, 0))
        spacer = tk.Frame(row, bg=C['bg'])
        spacer.pack(side='left', expand=True, fill='x')
        # 右侧扩展字段：能力 / 布局 —— 在瞬态消息之前显示。
        for key, title in reversed(self.RIGHT_FIELDS):
            var = tk.StringVar(value='—')
            self._vars[key] = var
            cell = tk.Frame(row, bg=C['bg'])
            cell.pack(side='right')
            tk.Label(cell, text=title, font=FONT_CAPTION, bg=C['bg'],
                     fg=C['ter']).pack(side='left')
            tk.Label(cell, textvariable=var, font=FONT_CAPTION, bg=C['bg'],
                     fg=C['text']).pack(side='left', padx=(4, 8))
        tk.Label(row, textvariable=status_var, anchor='e', font=FONT_CAPTION,
                 bg=C['bg'], fg=C['subtext']).pack(side='right')

    def update(self, **fields):
        """推送字段值；未知键忽略，超长值截断（保护栏宽）。"""
        for key, value in fields.items():
            var = self._vars.get(key)
            if var is None:
                continue
            try:
                var.set(_clip(value))
            except tk.TclError:
                pass  # 已销毁

    def value(self, key):
        """取某字段当前显示值（测试 / 验收探针用）。"""
        var = self._vars.get(key)
        return var.get() if var is not None else None
