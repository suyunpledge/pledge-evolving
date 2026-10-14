"""M1 全宽底部工具面板容器 + M5 四标签与真实计数徽标（编辑器与布局域）。

原输出 Notebook（Diff / 终端 / 测试 / 问题 / 输出五页）此前挂在
``editor_frame`` 内的竖向 Panedwindow 上，只占中央一栏的宽度；
M1 把它上移到 workbench 的**外层竖向 Panedwindow**（与三栏
``panes`` 并列），底部面板因此天然跨三栏全宽，与任务书 §M1 的
「全宽底部工具面板」结构一致。

迁移原则（M1，本期不新增、不删除任何输出页）：

* 五页的名称、Text 配置（``wrap='none'`` / ``FONT_MONO_SM`` /
  ``height=6`` / 双向滚动条 / 禁用编辑）与 ``_build_editor`` 原实现
  逐项一致——只是换了父容器；
* ``app.py`` 以 ``self.output = bottom.notebook`` 与
  ``self.output_views = bottom.views`` 两个别名保留既有调用点契约，
  ``write_output`` / ``self.output.select(0)`` 等**零改动**。

M5 四标签（任务书 §M5 + 用户示例图增量）：

* 可见标签四个——**差异 / 终端 / 测试 / 问题**（示例图基线标签与
  次序；任务书语义 Terminal / Output / Problems / Git Diff 由内容
  分工兑现，见下）。标签计数徽标（示例图：差异(3) / 测试(17) /
  问题(0)）一律取**真实数据源**，由 app 侧 provider 接线注入：
  差异 = Git 变更文件数（Explorer 变更清单行数）、测试 = 当前任务
  测试记录数、问题 = 当前任务失败记录数（失败工具执行 + 失败测试
  + 失败审查）；无该类数据时显示 0，**不伪造计数**。
* ``views`` 键名不变（``'Diff' / '终端' / '测试' / '问题' / '输出'``
  五个键全部保留）——``write_output`` 既有调用点零改动。原「输出」
  页并入「测试」页：任务书定义 Output = 构建输出 / 运行日志 /
  **测试结果** / 工具执行结果，与测试结果同页；
  ``views['输出']`` 是 ``views['测试']`` 的别名（同一 Text），
  Notebook 标签行只显示四个标签。
* 差异页增删行着色（任务书 Git Diff「新增删除行高亮」）：以 +/-
  开头的行按 git 语义上色（新增 ok / 删除 error），``@@`` hunk 头
  用 accent；非差异内容无 +/- 行则不染色。文件选择（Explorer 的
  Git 变更清单，双击/选择即写差异）与修改前后对比（inspect_diff
  统一 diff 文本）在 M3 已接线；「跳转对应文件」未实现，不伪装。
* Agent Events 标签的内容属 Codex 域——本模块只提供容器与排布
  约定（任务书 §M5）；本期按派单「四标签」执行，不预埋空标签。
  后续 Codex 需要该标签时经 ``ToolPanel`` 公开 API 增补，不动
  ``views`` 既有键。
"""
import tkinter as tk
from tkinter import ttk

from gui_theme import C, FONT_MONO_SM, style_editor

# 四个可见标签（顺序即 Tab 顺序）。views 键名与既有调用点保持一致。
PAGES = ('Diff', '终端', '测试', '问题')
# 标签行文案（示例图基线：差异 / 终端 / 测试 / 问题）。'Diff' 页的
# 标签文案为「差异」，views 键仍为 'Diff'（write_output('Diff') 等
# 既有调用点零改动）。
TAB_LABELS = {'Diff': '差异', '终端': '终端', '测试': '测试', '问题': '问题'}
# 带动态计数徽标的页（示例图：差异(3) / 测试(17) / 问题(0)）。
BADGED = ('Diff', '测试', '问题')
# 别名页：'输出' 与 '测试' 同一 Text（任务书 Output 语义 = 构建输出 /
# 运行日志 / 测试结果 / 工具执行结果），不出现在标签行。
ALIASES = {'输出': '测试'}


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


class ToolPanel(ttk.Frame):
    """全宽底部工具面板：四标签 Notebook + 真实计数徽标 + 差异页着色。

    作为外层竖向 Panedwindow 的第二个 slave（第一个是三栏 ``panes``），
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
        self._providers = {'Diff': changes_provider, '测试': tests_provider,
                           '问题': problems_provider}
        self._pages = {}
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill='both', expand=True)
        self.notebook.bind('<<NotebookTabChanged>>',
                           lambda _event: self.refresh_badges())
        self.views = {}
        for name in PAGES:
            self._add_page(name)
        for alias, target in ALIASES.items():
            self.views[alias] = self.views[target]
        self.refresh_badges()

    def _add_page(self, name):
        page = ttk.Frame(self.notebook)
        self._pages[name] = page
        self.notebook.add(page, text=TAB_LABELS[name])
        # height=6（原 editor_frame 内窄栏版本为 8）：底部面板现在是全宽
        # 容器，与三栏共享竖向空间；8 行的自然高度在 760px 级窗口上会把
        # 状态栏/竖向 pane 挤变形（Tk 从 Packing 序列末尾牺牲 Slave）。
        # sash 可拖，持久化比例照常，6 行只是自然尺寸下限而非显示上限。
        text = tk.Text(page, wrap='none', font=FONT_MONO_SM, height=6, undo=False)
        style_editor(text)
        # M5：差异页增删行着色 tag（任务书 Git Diff：新增删除行高亮）。
        # 语义与 Git 状态装饰一致：新增 ok / 删除 error / hunk 头 accent。
        text.tag_configure('diff_add', foreground=C['ok'])
        text.tag_configure('diff_del', foreground=C['error'])
        text.tag_configure('diff_hunk', foreground=C['accent'])
        sy = ttk.Scrollbar(page, command=text.yview)
        sy.pack(side='right', fill='y')
        sx = ttk.Scrollbar(page, orient='horizontal', command=text.xview)
        sx.pack(side='bottom', fill='x')
        text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set,
                       state='disabled')
        text.pack(fill='both', expand=True)
        self.views[name] = text

    def select(self, name):
        """按页名选中输出页（``self.output.select(0)`` 的语义化入口）。

        别名页（``'输出'``）映射到其目标页（``'测试'``）。
        """
        name = ALIASES.get(name, name)
        for index, page_name in enumerate(PAGES):
            if page_name == name:
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
            self.notebook.tab(self._pages[name],
                              text=f'{TAB_LABELS[name]}({count})')
        self._highlight_diff()

    def _highlight_diff(self):
        """差异页增删行着色：逐行扫描，+/-(非 +++/---) 与 @@ 行上色。

        只读既有文本内容打 tag，不改内容；Text 处于 disabled 态，
        着色前临时切 normal。非差异内容（无 +/- 行）不染色。
        """
        text = self.views.get('Diff')
        if text is None:
            return
        try:
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
