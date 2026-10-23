"""M1 全宽底部工具面板容器 + M5 四标签与真实计数徽标（编辑器与布局域）。

原输出 Notebook（Diff / 终端 / 测试 / 问题 / 输出五页）此前挂在
``editor_frame`` 内的竖向 Panedwindow 上，只占中央一栏的宽度；
M1 把它上移到 workbench 的**外层竖向 Panedwindow**（与三栏
``panes`` 并列），底部面板因此天然跨三栏全宽，与任务书 §M1 的
「全宽底部工具面板」结构一致。

迁移原则（M1，本期不新增、不删除任何输出页）：

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
PAGES = ('差异', '测试', '问题', '输出', '终端', '能力', '审批')

# 旧名到新名的别名（兼容既有 select('Diff') / select('终端') 调用）。
PAGE_ALIASES = {
    'Diff': '差异',
    '终端': '终端',  # R4：终端恢复独立页（不再折叠进差异）
    '输出': '输出',
}

# 「能力」「审批」两类自定义 Tab 标记（区分于纯文本 Tab）。
INJECTED_PAGES = {'能力', '审批'}

# 空态引导文案：未写入真实内容前显示，避免底部面板在无任务时呈现为
# 一大片纯黑空洞（"毛坯房"观感）。首次写入真实内容时由 app.append()
# 按 'empty_hint' tag 抹除——提示是一次性的，不会与新内容混排。
EMPTY_HINTS = {
    '差异': '从左侧「Git 变更」选择文件，或运行一个任务后，这里显示真实差异。',
    '测试': '运行任务或「执行测试」后显示真实测试结果；没有结构化结果时不推算通过数。',
    '问题': '暂无失败记录。工具失败、测试失败与审查失败会汇总到这里。',
    '输出': '工具调用与任务输出会实时显示在这里。',
    '终端': '这里只展示真实执行或被策略拒绝的命令记录。',
}


def count_tests(evidence):
    """测试徽标数据源：当前任务的测试记录数。

    ``evidence`` 为 ``forge.development.evidence.TaskEvidence``
    （Codex 域，只读投影——本模块不写、不改其结构）。任务切换时
    evidence 重建，计数随之归零，语义即「当前任务的测试记录」。
    """
    return len(getattr(evidence, 'tests', ()) or ())


def count_problems(evidence):
    """问题徽标数据源：当前任务中失败的记录数。

    失败的工具执行 + 失败的测试 + 失败的审查（TaskEvidence 只读
    投影）。任务书 Problems = 语法错误 / 诊断警告 / 测试失败 /
    运行异常——本项目无独立诊断器（不伪造），以运行时真实失败
    记录计数；某类能力不存在时该项为 0。
    """
    actions = getattr(evidence, 'actions', {}) or {}
    tests = getattr(evidence, 'tests', ()) or ()
    reviews = getattr(evidence, 'reviews', {}) or {}
    total = sum(1 for row in actions.values() if row.get('status') == 'failed')
    total += sum(1 for row in tests if row.get('status') == 'failed')
    total += sum(1 for row in reviews.values() if row.get('status') == 'failed')
    return total


def _empty_state_card(parent, title, hint, *, bg=None, wrap=360, rely=0.5):
    """居中空态卡（v0.5 视觉规范）：图标点 + 标题 + 副文案。

    替代原来的单行左上角灰字——底部面板空态是「毛坯房」观感的
    重灾区：一行 9px 灰字贴在纯黑面板左上角，既无层级也无引导。
    新空态：居中 16px 间距的竖排（视觉锚点为 ICONS 位图图标），
    文案色调 subtext/ter 两级，保持克制。

    ``bg`` 允许宿主面板用自身底色（如侧栏 sidebar 而非 code_bg）；
    ``wrap``/``rely`` 供窄栏宿主收窄文案宽、上移锚点。
    """
    from ui_icons import icon_image
    base = bg or C['code_bg']
    box = tk.Frame(parent, bg=base)
    # place 居中悬浮：page 内 Text 已 pack(fill both expand) 吃满腔体，
    # 再 pack 会挤进底部残区（unmapped）。空态是「覆盖层」语义——
    # place(relx=.5, rely=.5, anchor='c') 悬浮在 Text 上方，Text 保持
    # 映射（滚动条/着色 tag 不动），app.append() 到达真实内容时销毁本卡。
    box.place(relx=0.5, rely=rely, anchor='c')
    icon_holder = tk.Frame(box, bg=base, width=32, height=32)
    icon_holder.pack(pady=(0, 6))
    icon_holder.pack_propagate(False)
    img = icon_image(icon_holder, 'files', size=24, fg=C['ter'])
    if img is not None:
        icon_lbl = tk.Label(icon_holder, image=img, bg=base, bd=0)
        icon_lbl.image = img
        icon_lbl.pack(expand=True)
    tk.Label(box, text=title, font=FONT_SECTION, bg=base,
             fg=C['subtext']).pack()
    tk.Label(box, text=hint, font=FONT_CAPTION, bg=base,
             fg=C['ter'], wraplength=wrap, justify='center').pack(pady=(4, 0))
    return box


def highlight_diff_lines(text):
    """差异文本增删行着色（单一真源，M6 起中央 Git Diff 视图共用）。

    逐行扫描：``@@`` hunk 头上 accent、``+`` 行（非 ``+++``）上 ok、
    ``-`` 行（非 ``---``）上 error；非差异内容（无 +/- 行）不染色。
    只读既有文本内容打 tag，不改内容；Text 处于 disabled 态时着色前
    临时切 normal、结束后还原。调用方负责保证 tag 已配置（Diff 页与
    review 视图均在构建时注册 diff_add / diff_del / diff_hunk）。
    """
    if text is None:
        return
    try:
        text.tag_configure('diff_hunk', foreground=C['accent2'])
        if not text.get('1.0', 'end-1c').strip():
            return
        text.configure(state='normal')
        for tag in ('diff_add', 'diff_del', 'diff_hunk'):
            text.tag_remove(tag, '1.0', 'end')
        last = int(text.index('end-1c').split('.')[0])
        for number in range(1, last + 1):
            start, end = f'{number}.0', f'{number}.end'
            line = text.get(start, end)
            if line.startswith('@@'):
                text.tag_add('diff_hunk', start, end)
            elif line.startswith('+') and not line.startswith('+++'):
                text.tag_add('diff_add', start, end)
            elif line.startswith('-') and not line.startswith('---'):
                text.tag_add('diff_del', start, end)
        text.configure(state='disabled')
    except tk.TclError:
        pass

# 带动态计数徽标的页（示例图：差异(3) / 测试(17) / 问题(0)）——M5 交付嫁接。
BADGED = ('差异', '测试', '问题')
# BADGED 页的 Tab 基名（徽标文案 = f"{base} ({count})"）。
TAB_LABELS = {'差异': '差异', '测试': '测试', '问题': '问题'}


class ToolPanel(ttk.Frame):
    """全宽底部工具面板：四标签 Notebook + 真实计数徽标 + 差异页着色。

    作为外层竖向 Panedwindow的第二个 slave（第一个是三栏 ``panes``），
    由 ``place_sashes`` 按 ``state.layout['output']`` 定位 sash——
    该字段语义为「上部区域占比」（旧值 .72 等直接兼容）。

    徽标数据源经构造参数注入（provider 契约，与 M3 ExplorerPanel 的
    ``expanded_getter`` / ``boundary_provider`` 同范式）：面板不读
    app 私有结构，只调用无参 callable 取整数。刷新时机：标签切换
    （``<<NotebookTabChanged>>``）与 ``refresh_badges()``（app 在
    ``refresh_project`` 扫描周期调用）。
    """

    def __init__(self, master, *, changes_provider=None, tests_provider=None,
                 problems_provider=None, **kw):
        super().__init__(master, **kw)
        # v0.2 六页制：provider 键与 BADGED 页名一致（'差异'）。
        self._providers = {'差异': changes_provider, '测试': tests_provider,
                           '问题': problems_provider}
        self.pages = {}
        self._pages = self.pages  # M5 兼容别名（同一字典）
        self.notebook = ttk.Notebook(self, style='Coding.TNotebook')
        self.notebook.pack(fill='both', expand=True)
        self.notebook.bind('<<NotebookTabChanged>>',
                           lambda _event: self.refresh_badges())
        self.views = {}
        # page frame 直接保存：避免 nametowidget 在 init 期间路径不稳（pages
        # 已在别名前建立）。
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
        # M5：差异页增删行着色 tag（任务书 Git Diff：新增删除行高亮）。
        # 语义与 Git 状态装饰一致：新增 ok / 删除 error / hunk 头 accent。
        text.tag_configure('diff_add', foreground=C['ok'])
        text.tag_configure('diff_del', foreground=C['error'])
        text.tag_configure('diff_hunk', foreground=C['accent2'])
        sy = ttk.Scrollbar(page, command=text.yview)
        sy.pack(side='right', fill='y')
        sx = ttk.Scrollbar(page, orient='horizontal', command=text.xview)
        sx.pack(side='bottom', fill='x')
        text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set,
                       state='disabled')
        text.pack(fill='both', expand=True)
        self.views[name] = text
        # 空态引导：一次性提示，tag 'empty_hint' 标记，app.append() 首
        # 次写入真实内容时抹除。
        # v0.5：空态改为居中空态卡（_empty_state_card）——旧实现是
        # 左上角一行 9px 灰字，无层级无引导。empty_hint tag 语义保留：
        # app.append() 写入真实内容时仍按 tag 抹除（空态卡同步销毁）。
        hint = EMPTY_HINTS.get(name)
        if hint:
            try:
                text.tag_configure('empty_hint', foreground=C['ter'])
                card = _empty_state_card(page, name, hint)
                text._empty_card = card  # noqa: SLF001 — app.append 抹除钩子
            except tk.TclError:
                pass

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

    def set_badge(self, name, count):
        """更新页 Tab 文案为 `name (count)`（BADGED 页专用；count 为 int 或 str）。

        M5 交付嫁接：计数一律来自 app 注入的真实数据源 provider，
        异常时归零显示（不伪造）。
        """
        try:
            base = getattr(self, '_tab_base', {}).get(name, name)
            if name in BADGED:
                text = f"{base} ({int(count)})"
            else:
                text = base
            if name in self.pages:
                self.notebook.tab(self.pages[name], text=text)
        except Exception:
            pass

    def has_content(self):
        """任何一页有真实内容（非空态）→ True。

        判定走两层：BADGED 三页用注入 provider 的计数（变更/测试/问题
        任一非零即有内容）；文本页逐页检查 Text 是否有超出空态卡以外
        的内容（append 会销毁空态卡，故 card 不存在且有字符即真内容）。
        """
        for name in BADGED:
            provider = self._providers.get(name)
            try:
                if provider is not None and int(provider()) > 0:
                    return True
            except Exception:
                pass
        for name, view in self.views.items():
            if getattr(view, 'tag_ranges', None) is None:
                continue  # 注入类视图（能力/审批）不算文本内容
            if getattr(view, '_empty_card', None) is not None:
                continue  # 空态卡还在 = 无真实内容
            if str(view.get('1.0', 'end-1c')).strip():
                return True
        return False

    def select(self, name):
        """按页名（兼容旧别名）选中输出页。"""
        target = PAGE_ALIASES.get(name, name)
        for index, page_name in enumerate(PAGES):
            if page_name == target:
                self.notebook.select(index)
                return True
        return False

    def refresh_badges(self):
        """按真实数据源刷新标签计数徽标，并重算差异页增删行着色。

        provider 取数异常时该项显示 0（卫仪；真实数据源不应抛错，
        项目未打开 / evidence 未初始化等正常空态由 provider 自身
        返回 0）。徽标恒显示计数（示例图问题(0)即 0 也显示）。
        """
        for name in BADGED:
            provider = self._providers.get(name)
            try:
                count = int(provider()) if provider is not None else 0
            except Exception:
                count = 0
            self.notebook.tab(self.pages[name],
                              text=f'{TAB_LABELS[name]} ({count})')
        self._highlight_diff()

    def _highlight_diff(self):
        """差异页增删行着色（实现经模块级 :func:`highlight_diff_lines`
        单一真源——M6 中央 Git Diff 视图共用同一套语义）。"""
        highlight_diff_lines(self.views.get('差异'))
