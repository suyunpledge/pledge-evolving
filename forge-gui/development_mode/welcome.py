"""M2 欢迎页：未打开项目时中央区的占位视图（编辑器与布局域）。

VS Code 类工作台的惯例：没有打开任何项目时，中央编辑区不显示空的
工具栏与标签页，而是一页「欢迎」——品牌标题、打开项目入口、近期
项目列表。本组件即该视图。

* :class:`WelcomeView` —— 纯展示组件，**不持有业务方法**：打开项目经
  ``on_open`` 回调交还 workbench（复用既有 ``choose_project`` 目录对话
  框），近期项目行经 ``on_recent`` 回调打开对应路径；近期列表由
  ``recent_provider`` 惰性提供（workbench 侧即 ``state.value('recent')``），
  本类不直接读状态层，便于离线测试与复用。

挂载与切换在 ``app.py``：``_build_welcome`` 建视图，``_sync_welcome``
按 ``self.source`` 二态切换中央区（无项目 → 欢迎页；有项目 → 编辑器
本体）。切换只发生在 editor_frame 内的两个子帧之间（pack/pack_forget），
**不新增 pane 挂载路径**——apply_layout 的增量挂载逻辑（51f6f82 保护
清单）与 M1-fix 的 ``_settle_mount`` 冲刷均不受影响。
"""
import tkinter as tk
from tkinter import ttk

from gui_theme import C, FONT_BRAND, FONT_SECTION, FONT_UI, FONT_UI_MED


class WelcomeView(ttk.Frame):
    """欢迎页：品牌标题 + [打开项目] + Recent Projects。

    参数
    ----
    on_open:
        「打开项目」按钮回调（无参数）——workbench 的 ``choose_project``。
    on_recent:
        近期项目行回调（接收路径字符串）——workbench 的 ``open_project``。
    recent_provider:
        近期项目列表的惰性提供者（返回路径字符串列表）。
    """

    def __init__(self, master, *, on_open, on_recent, recent_provider, **kw):
        super().__init__(master, **kw)
        self._on_open = on_open
        self._on_recent = on_recent
        self._recent_provider = recent_provider
        self.recent_paths = []
        self._build()

    # ── 公开访问器（测试 / 验收探针用，不暴露私有结构）──────────────
    @property
    def open_button(self):
        """「打开项目」按钮（内容层代表控件）。"""
        return self._open_button

    def recent_count(self):
        """当前渲染的近期项目行数（空列表时为空态提示，不计入）。"""
        return len(self.recent_paths)

    # ── 构建 ──────────────────────────────────────────────────────
    def _build(self):
        # ttk.Frame 的背景由主题的 TFrame 样式提供（apply_ttk_theme 设为
        # C['bg']），不能给 ttk 控件传 bg；内部的 tk.Label 才需要显式 bg。
        self.configure(padding=(48, 40, 48, 24))
        tk.Label(self, text='FORGE Coding', font=FONT_BRAND, bg=C['bg'],
                 fg=C['accent_text']).pack(anchor='w')
        tk.Label(self, text='打开一个项目，开始编码与 Agent 协作',
                 font=FONT_UI, bg=C['bg'], fg=C['subtext']).pack(
                     anchor='w', pady=(4, 20))
        ttk.Button(self, text='打开项目', command=self._open).pack(anchor='w')
        self._open_button = self.winfo_children()[-1]
        tk.Label(self, text='近期项目', font=FONT_SECTION, bg=C['bg'],
                 fg=C['text']).pack(anchor='w', pady=(28, 8))
        self._recent_box = tk.Frame(self, bg=C['bg'])
        self._recent_box.pack(anchor='w', fill='x')
        self.refresh()

    def refresh(self):
        """按 recent_provider 重绘近期项目列表（每次显示前调用）。"""
        for child in self._recent_box.winfo_children():
            child.destroy()
        try:
            recent = [str(p) for p in (self._recent_provider() or [])]
        except Exception:
            recent = []
        self.recent_paths = recent
        if not recent:
            tk.Label(self._recent_box, text='暂无项目记录', font=FONT_UI,
                     bg=C['bg'], fg=C['ter']).pack(anchor='w')
            return
        for path in recent:
            label = tk.Label(self._recent_box, text=path, font=FONT_UI,
                             bg=C['bg'], fg=C['link'], cursor='hand2',
                             anchor='w')
            label.pack(anchor='w', fill='x', pady=2)
            label.bind('<Button-1>', lambda _e, p=path: self._open_recent(p))
            label.bind('<Enter>', lambda _e, w=label: self._hover(w, True))
            label.bind('<Leave>', lambda _e, w=label: self._hover(w, False))

    # ── 交互 ──────────────────────────────────────────────────────
    def _open(self):
        if self._on_open is not None:
            self._on_open()

    def _open_recent(self, path):
        if self._on_recent is not None:
            self._on_recent(path)

    def _hover(self, label, entering):
        try:
            label.configure(fg=C['text'] if entering else C['link'])
        except tk.TclError:
            pass  # 已销毁
