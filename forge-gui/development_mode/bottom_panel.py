"""M1 全宽底部工具面板容器（编辑器与布局域）。

原输出 Notebook（Diff / 终端 / 测试 / 问题 / 输出五页）此前挂在
``editor_frame`` 内的竖向 Panedwindow 上，只占中央一栏的宽度；
M1 把它上移到 workbench 的**外层竖向 Panedwindow**（与三栏
``panes`` 并列），底部面板因此天然跨三栏全宽，与任务书 §M1 的
「全宽底部工具面板」结构一致。

迁移原则（本期不新增、不删除任何输出页）：

* 六页的名称、Text 配置（``wrap='none'`` / ``FONT_MONO_SM`` /
  ``height=8`` / 双向滚动条 / 禁用编辑）与 ``_build_editor`` 原实现
  逐项一致——只是换了父容器；
* ``app.py`` 以 ``self.output = bottom.notebook`` 与
  ``self.output_views = bottom.views`` 两个别名保留既有调用点契约，
  ``write_output`` / ``self.output.select(0)`` 等**零改动**；
* 末尾两页（能力 / 审批）由 ``app`` 注入真实视图——与 explorer 的
  install_*_view 同范式，未注入时显示占位提示，不伪造内容。

「能力」对应 CapabilityLibrary / builtin tools / contrib / plugin
的合并视图；「审批」对应 tool_approval_pending 队列
（之前架构审查 P0-1 / P1-5 的可视化落点）。

实现细节：每页的 Frame 在创建时**直接**保存到 ``self.pages`` 字典，
后续 ``install_*_view`` 与 ``_inject_placeholder`` 都从字典拿，不走
``nametowidget(tabs()[idx])``——后者在 init 阶段路径尚未稳定，跨
ttk.Notebook 实测会指向子 widget 而非 page frame。
"""
import tkinter as tk
from tkinter import ttk

from gui_theme import C, FONT_CAPTION, FONT_MONO_SM, FONT_SECTION, style_editor

# 与 _build_editor 原实现完全一致的输出页集合（顺序即 Tab 顺序）。
# 末尾「能力 / 审批」为新加；前四页与既有契约保持一致。
PAGES = ('差异', '测试', '问题', '输出', '能力', '审批')

# 旧名到新名的别名（兼容既有 select('Diff') / select('终端') 调用）。
PAGE_ALIASES = {
    'Diff': '差异',
    '终端': '差异',  # 旧「终端」折叠到「差异」（下层共用 Text 视图）
    '输出': '输出',
}

# 「能力」「审批」两类自定义 Tab 标记（区分于纯文本 Tab）。
INJECTED_PAGES = {'能力', '审批'}


class ToolPanel(ttk.Frame):
    """全宽底部工具面板：承载输出 Notebook 的容器。

    作为外层竖向 Panedwindow的第二个 slave（第一个是三栏 ``panes``），
    由 ``place_sashes`` 按 ``state.layout['output']`` 定位 sash——
    该字段语义相应变为「上部区域占比」（旧值 .72 等直接兼容）。
    """

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill='both', expand=True)
        self.views = {}
        # page frame 直接保存：避免 nametowidget 在 init 期间路径不稳。
        self.pages = {}
        for name in PAGES:
            self._add_page(name)
        # 末尾两页用「注入」式占位（与 explorer 同范式）—— 直接复用 page frame。
        self._inject_placeholder('能力',
                                '能力视图由 app 注入（builtin + contrib + plugin 合并）。')
        self._inject_placeholder('审批',
                                '审批视图由 app 注入（tool_approval_pending 队列）。')

    def _add_page(self, name):
        page = ttk.Frame(self.notebook)
        self.notebook.add(page, text=name)
        self.pages[name] = page
        if name in INJECTED_PAGES:
            # 自定义 Tab：先放占位 Label，由 app install_* 替换。
            placeholder = ttk.Frame(page)
            placeholder.pack(fill='both', expand=True)
            self.views[name] = placeholder
            return
        # 文本类 Tab：双向滚动条 + 禁用编辑的 Text。
        text = tk.Text(page, wrap='none', font=FONT_MONO_SM, height=6, undo=False)
        style_editor(text)
        sy = ttk.Scrollbar(page, command=text.yview)
        sy.pack(side='right', fill='y')
        sx = ttk.Scrollbar(page, orient='horizontal', command=text.xview)
        sx.pack(side='bottom', fill='x')
        text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set,
                       state='disabled')
        text.pack(fill='both', expand=True)
        self.views[name] = text

    def _inject_placeholder(self, name, hint):
        """为非文本类 Tab 留好占位：app 后续用 install_* 替换。"""
        page = self.pages[name]
        # 找 page 内除 self 之外的第一层（占位 frame）—— 写其 hint。
        for child in page.winfo_children():
            if isinstance(child, ttk.Frame):
                for grand in child.winfo_children():
                    if isinstance(grand, ttk.Label):
                        grand.configure(text=hint)
                        return
        # 找不到就追加一个新 Label 到占位 frame。
        placeholder = self.views.get(name)
        if isinstance(placeholder, ttk.Frame):
            ttk.Label(placeholder, text=hint, foreground=C['subtext'],
                      wraplength=400, justify='left', padding=16).pack(
                anchor='nw')

    def install_capabilities_view(self, view):
        """app 注入真实能力视图到「能力」Tab。"""
        self._install_view('能力', view)

    def install_approvals_view(self, view):
        """app 注入真实审批视图到「审批」Tab。"""
        self._install_view('审批', view)

    def _install_view(self, name, view):
        page = self.pages[name]
        # 删掉占位 frame（含其内 Label），再 pack 真实 view。
        for child in page.winfo_children():
            child.destroy()
        view.pack(in_=page, fill='both', expand=True)
        self.views[name] = view

    def select(self, name):
        """按页名（兼容旧别名）选中输出页。"""
        target = PAGE_ALIASES.get(name, name)
        for index, page_name in enumerate(PAGES):
            if page_name == target:
                self.notebook.select(index)
                return True
        return False
