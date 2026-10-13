"""M1 全宽底部工具面板容器（编辑器与布局域）。

原输出 Notebook（Diff / 终端 / 测试 / 问题 / 输出五页）此前挂在
``editor_frame`` 内的竖向 Panedwindow 上，只占中央一栏的宽度；
M1 把它上移到 workbench 的**外层竖向 Panedwindow**（与三栏
``panes`` 并列），底部面板因此天然跨三栏全宽，与任务书 §M1 的
「全宽底部工具面板」结构一致。

迁移原则（本期不新增、不删除任何输出页）：

* 五页的名称、Text 配置（``wrap='none'`` / ``FONT_MONO_SM`` /
  ``height=8`` / 双向滚动条 / 禁用编辑）与 ``_build_editor`` 原实现
  逐项一致——只是换了父容器；
* ``app.py`` 以 ``self.output = bottom.notebook`` 与
  ``self.output_views = bottom.views`` 两个别名保留既有调用点契约，
  ``write_output`` / ``self.output.select(0)`` 等**零改动**；
* M5 将在此容器内重整为 Terminal / Output / Problems / Git Diff
  四标签，并按 Phase 1 §8.3 的交接契约暴露 ``agent_events_frame``
  供 Codex 挂载 Agent Events 内容；本期不预留该属性（不造死代码）。
"""
import tkinter as tk
from tkinter import ttk

from gui_theme import FONT_MONO_SM, style_editor

# 与 _build_editor 原实现完全一致的输出页集合（顺序即 Tab 顺序）。
PAGES = ('Diff', '终端', '测试', '问题', '输出')


class ToolPanel(ttk.Frame):
    """全宽底部工具面板：承载输出 Notebook 的容器。

    作为外层竖向 Panedwindow 的第二个 slave（第一个是三栏 ``panes``），
    由 ``place_sashes`` 按 ``state.layout['output']`` 定位 sash——
    该字段语义相应变为「上部区域占比」（旧值 .72 等直接兼容）。
    """

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill='both', expand=True)
        self.views = {}
        for name in PAGES:
            self._add_page(name)

    def _add_page(self, name):
        page = ttk.Frame(self.notebook)
        self.notebook.add(page, text=name)
        # height=6（原 editor_frame 内窄栏版本为 8）：底部面板现在是全宽
        # 容器，与三栏共享竖向空间；8 行的自然高度在 760px 级窗口上会把
        # 状态栏/竖向 pane 挤变形（Tk 从 Packing 序列末尾牺牲 Slave）。
        # sash 可拖，持久化比例照常，6 行只是自然尺寸下限而非显示上限。
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

    def select(self, name):
        """按页名选中输出页（``self.output.select(0)`` 的语义化入口）。"""
        for index, page_name in enumerate(PAGES):
            if page_name == name:
                self.notebook.select(index)
                return True
        return False
