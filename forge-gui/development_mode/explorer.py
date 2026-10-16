"""M3 Explorer：左栏活动栏 + 文件树 + Git 变更 + 文件操作（编辑器与布局域）。

示例图左区结构：48px 活动栏（文件 / 搜索 / Git / 运行 / 扩展 / 设置，
ui_icons 统一图标列）+ 文件面板（标题「文件」+ 新建 / 折叠 / 刷新 +
文件树；Git 变更清单带状态语义）。搜索框保留在面板顶部——既有的
文件名过滤行为不变（Enter 触发 app.refresh_project），活动栏
「搜索」项只是聚焦它，不引入第二套搜索状态。

Git 状态语义化：``git status --porcelain=v1`` 的双字符码经
:func:`normalize_status` 归一化为 M / A / D / U / R / C / X（冲突）
七类，每类有字母徽标（图标位）、主题色（warn / ok / error / ter /
accent）与中文标签。字母徽标本身就是**非颜色冗余**——不依赖颜色
也能区分状态；Git 标签页的图例行再把同一映射用文字复述一遍。

文件操作（新建文件 / 新建文件夹 / 重命名 / 删除 / 复制路径 / 刷新）
全部经 ``ProjectBoundary.path()`` 校验（项目外与 .git 内一律拒绝），
删除前 messagebox 确认；成功后经 on_refresh 回调让 app 重扫，复制
路径写系统剪贴板并经 status_var 给瞬态反馈。本模块不直接读 app
私有结构——边界与行为经构造参数注入（与 welcome.py 的 WelcomeView
同范式）。

``self.tree`` / ``self.changes`` 属性契约：ExplorerPanel 构建这两个
Treeview 后由 ``app._build_project`` 转交（``self.tree =
panel.tree``），既有调用点（open_tree / expand_tree / show_diff /
_project_scan）无感。数据填充的唯一入口是 :meth:`populate`，由
``app._project_scan`` 在扫描完成回调里调用。

已知边界（本期不做，避免过度设计）：重命名/删除不联动已打开的标签
页——旧标签仍指向旧路径，保存前请先关闭；VS Code 式的模型跟随属
编辑器域（M4）范畴。
"""
import shutil
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

from gui_theme import C, FONT_CAPTION, FONT_SECTION, attach_tooltip
from ui_icons import IconButton, IconCanvas, icon_image

# 活动栏宽度（示例图左区第一列 48px）。
ACTIVITY_WIDTH = 48

# Git porcelain 双字符码 → (字母徽标, 颜色 token, 中文标签)。
# 'X' 为冲突类（UU / AA / DD / DU ...）的归一化汇总键。
GIT_STATUS_STYLE = {
    'M': ('M', 'warn', '已修改'),
    'A': ('A', 'ok', '新增'),
    'D': ('D', 'error', '删除'),
    'U': ('U', 'ter', '未跟踪'),
    'R': ('R', 'accent', '重命名'),
    'C': ('C', 'accent', '复制'),
    'X': ('!', 'error', '冲突'),
}


def normalize_status(code):
    """把 porcelain 双字符码归一化为 GIT_STATUS_STYLE 的键。

    规则（与 VS Code 的显示优先级一致）：
      * 含 ``?`` → ``U``（未跟踪，porcelain 原样为 ``??``）；
      * 含 ``U`` 或 ``DD`` / ``AA`` → ``X``（合并冲突）；
      * 暂存区操作（R / C / A）优先于工作区操作（M / D）；
      * 工作区操作取**倒序**首字符——后字符是文件的当前态
        （如 ``MD``：暂存区改过、工作区又删了 → 显示 D）；
      * 其余原样返回首字符（理论不可达，防御性兜底）。
    """
    code = (code or '').strip()
    if not code:
        return ''
    if '?' in code:
        return 'U'
    if 'U' in code or code in ('DD', 'AA'):
        return 'X'
    for char in code:
        if char in 'RCA':
            return char
    for char in reversed(code):
        if char in 'MD':
            return char
    return code[0]


def status_letter(code):
    """状态码 → 字母徽标（空串表示无 Git 状态）。"""
    style = GIT_STATUS_STYLE.get(code)
    return style[0] if style else ''


def status_label(code):
    """状态码 → 中文标签（图例与测试用）。"""
    style = GIT_STATUS_STYLE.get(code)
    return style[2] if style else ''


def status_tag(code):
    """状态码 → Treeview tag 名（空串表示无状态，不加 tag）。"""
    return f'git-{code}' if code else ''


# 文件类型图标（扩展名 → ui_icons 图标名；目录用 folder_open）。
FILE_TYPE_ICONS = {
    '.py': 'code', '.pyi': 'code', '.pyx': 'code',
    '.md': 'file', '.markdown': 'file', '.rst': 'file',
    '.txt': 'file', '.log': 'file',
    '.json': 'config', '.toml': 'config', '.yaml': 'config', '.yml': 'config',
    '.ini': 'config', '.cfg': 'config', '.conf': 'config',
    '.png': 'image', '.jpg': 'image', '.jpeg': 'image',
    '.gif': 'image', '.svg': 'image', '.ico': 'image',
}


def file_icon_name(name):
    """文件名 → 文件类型图标名（目录由调用方传 folder_open）。"""
    return FILE_TYPE_ICONS.get(Path(name).suffix.casefold(), 'file')


def create_file(boundary, directory, name):
    """在 directory 下新建文件（父目录不存在则一并创建）。

    经 boundary.path() 校验：项目外 / .git 内抛 PermissionError。
    """
    path = boundary.path(Path(directory) / name.strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f'已存在：{path.name}')
    path.write_text('', encoding='utf-8')
    return path


def create_dir(boundary, directory, name):
    """在 directory 下新建文件夹（经 boundary 校验）。"""
    path = boundary.path(Path(directory) / name.strip())
    if path.exists():
        raise FileExistsError(f'已存在：{path.name}')
    path.mkdir(parents=True)
    return path


def rename_path(boundary, path, new_name):
    """重命名文件/文件夹（仅改当前级名，不跨目录；经 boundary 校验）。"""
    path = boundary.path(path)
    new_name = (new_name or '').strip()
    if not new_name:
        raise ValueError('名称不能为空')
    if new_name in ('.', '..') or '/' in new_name or '\\' in new_name:
        raise ValueError('名称不能包含路径分隔符')
    target = boundary.path(path.with_name(new_name))
    if target == path:
        return path
    if target.exists():
        raise FileExistsError(f'已存在：{new_name}')
    path.rename(target)
    return target


def delete_path(boundary, path):
    """删除文件/文件夹（递归；经 boundary 校验）。调用方负责确认。"""
    path = boundary.path(path)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return path


class ActivityBar(ttk.Frame):
    """48px 活动栏：图标列 + 激活态左侧 2px 指示条 + 悬停换色。

    图标用 :class:`IconCanvas`（矢量、逐态重绘、可键盘聚焦），
    与主仓活动栏（forge_gui_v2._make_activity_item）同一范式；
    激活/悬停配色经 :meth:`_paint` 统一重刷，不依赖 ttk 主题映射。

    9 个图标（自上而下）：文件 / 搜索 / Git / 任务 / 运行 / 知识 /
    演化 / 审批 / 设置——按「与代码模块一一对应」原则排序：前三是
    工作区事实、中四（任务/运行/知识/演化）是架构侧、后一是配置。
    """

    ITEMS = (
        ('files', 'files', '文件'),
        ('search', 'search', '搜索'),
        ('git', 'diff', 'Git 变更'),
        ('tasks', 'task', '任务'),
        ('run', 'play', '运行'),
        ('knowledge', 'book', '知识库'),
        ('evolution', 'spark', '演化'),
        ('approvals', 'check', '审批'),
        ('settings', 'config', '设置'),
    )

    def __init__(self, master, on_select, **kw):
        super().__init__(master, width=ACTIVITY_WIDTH, **kw)
        self.pack_propagate(False)
        self._on_select = on_select
        self._entries = {}          # key -> (holder, icon, marker)
        self._active = None
        for key, glyph, label in self.ITEMS:
            holder = tk.Frame(self, bg=C['activity'], height=44)
            holder.pack(fill='x')
            holder.pack_propagate(False)
            marker = tk.Frame(holder, bg=C['activity'], width=2)
            marker.pack(side='left', fill='y')
            icon = IconCanvas(holder, glyph, size=20,
                              bg=C['activity'], fg=C['ter'])
            icon.pack(expand=True)
            attach_tooltip(icon, label)
            for widget in (holder, icon):
                widget.bind('<Button-1>', lambda _e, k=key: self.select(k))
                widget.bind('<Enter>', lambda _e, k=key: self._paint(k, hover=True))
                widget.bind('<Leave>', lambda _e, k=key: self._paint(k))
                widget.configure(cursor='hand2')
            self._entries[key] = (holder, icon, marker)
        # 底部弹性占位：图标列整体靠上（与示例图一致）。
        tk.Frame(self, bg=C['activity']).pack(expand=True, fill='both')

    # ── 公开访问器（测试 / 验收探针用）────────────────────────────
    @property
    def active(self):
        """当前激活项键名（初始 None，挂载后由 app 选 'files'）。"""
        return self._active

    def entry(self, key):
        """取某项的 (holder, icon, marker) 三元组（键不存在返回 None）。"""
        return self._entries.get(key)

    # ── 交互 ──────────────────────────────────────────────────────
    def select(self, key):
        """激活某项并回调 on_select（重复选同一项也回调，保持幂等）。"""
        if key not in self._entries:
            return
        previous, self._active = self._active, key
        if previous in self._entries:
            self._paint(previous)
        self._paint(key)
        if self._on_select is not None:
            self._on_select(key)

    def _paint(self, key, *, hover=False):
        active = key == self._active
        bg = C['sel'] if active else C['hover'] if hover else C['activity']
        fg = C['accent_text'] if active else C['text'] if hover else C['ter']
        holder, icon, marker = self._entries[key]
        holder.configure(bg=bg)
        icon.configure(bg=bg, fg=fg)
        marker.configure(bg=C['accent'] if active else C['activity'])


class ExplorerPanel(ttk.Frame):
    """左栏面板：活动栏 + 搜索框 + 文件树 / Git 变更 / 占位视图。

    构造参数（全部经注入，不读 app 私有结构）：

    ``on_open_file(path)``      程序性打开文件（新建文件后）
    ``on_open_tree(event)``     树内双击/回车/菜单打开（app.open_tree，
                                由它做「仅文件可打开」的判定）
    ``on_open_changed()``       双击 Git 变更行（app.open_changed）
    ``on_show_diff(event)``     Git 变更选择 / diff 视图切换（app.show_diff）
    ``on_expand(opened)``       目录展开/收起（app.expand_tree）
    ``on_refresh()``            重扫项目（app.refresh_project）
    ``on_run()``                活动栏「运行」（app.run_tests）
    ``boundary_provider()``     当前 ProjectBoundary（无项目返回 None）
    ``expanded_getter()``       app 的展开目录集合（**getter 注入**：
                                open_project 会重绑定 self.expanded 为
                                新集合，直接传引用会持到旧对象）
    ``search_var`` / ``diff_var``  app 的 self.search / self.diff_view
    ``status_var``              app 的 self.status（瞬态反馈）
    """

    # 占位视图（无真实数据源的入口：明说未接入，不伪造内容）。
    PLACEHOLDERS = {
        'settings': ('设置', '设置视图将在后续模块接入；'
                            '模型设置可从顶部导航栏的 Settings 进入。'),
    }

    # 真实 Tab：6 个视图（文件 / Git / 搜索 / 任务 / 知识 / 演化）
    # 搜索不占独立 Tab——它是文件 Tab 顶部的过滤框，活动栏「搜索」只是
    # 聚焦它（与既有行为一致）。
    REAL_TABS = ('files', 'git', 'tasks', 'knowledge', 'evolution')

    def __init__(self, master, *, on_open_file, on_open_tree, on_open_changed,
                 on_show_diff, on_expand, on_refresh, on_run,
                 boundary_provider, expanded_getter, search_var, diff_var,
                 status_var,
                 on_show_tasks=None, on_show_knowledge=None, on_show_evolution=None,
                 on_show_approvals=None,
                 **kw):
        super().__init__(master, **kw)
        self._on_open_file = on_open_file
        self._on_open_tree = on_open_tree
        self._on_open_changed = on_open_changed
        self._on_show_diff = on_show_diff
        self._on_expand = on_expand
        self._on_refresh = on_refresh
        self._on_run = on_run
        self._boundary_provider = boundary_provider
        self._expanded_getter = expanded_getter
        self._search_var = search_var
        self._diff_var = diff_var
        self._status_var = status_var
        # 新 Tab 回调：缺省回落为「把任务/知识/演化/审批请回 app」的占位
        # 提示；activity_bar 触发时 app 注入真实处理（详见 app.py）。
        self._on_show_tasks = on_show_tasks
        self._on_show_knowledge = on_show_knowledge
        self._on_show_evolution = on_show_evolution
        self._on_show_approvals = on_show_approvals
        self._activity = ActivityBar(self, self._select_view)
        self._build_content()
        # 布局顺序：活动栏在左、内容区在右（先 pack 活动栏）。
        self._activity.pack(side='left', fill='y')
        self._content.pack(side='left', fill='both', expand=True)
        self._activity.select('files')

    # ── 构建 ──────────────────────────────────────────────────────
    def _build_content(self):
        content = self._content = ttk.Frame(self)
        # 搜索行（既有位置与行为：Enter → on_refresh）。
        self._search_row = ttk.Frame(content)
        self._search_row.pack(fill='x', pady=(0, 6))
        self.search_entry = ttk.Entry(self._search_row,
                                      textvariable=self._search_var)
        self.search_entry.pack(fill='x')
        self.search_entry.bind('<Return>', lambda _e: self._on_refresh())
        ttk.Label(self._search_row, text='文件名搜索 · Enter',
                  foreground=C['subtext']).pack(anchor='w')
        # 6 个真实 Tab：文件 / Git / 搜索(过滤) / 任务 / 知识 / 演化
        self.notebook = ttk.Notebook(content, style='Explorer.TNotebook')
        self._build_files_tab()
        self._build_git_tab()
        self._build_tasks_tab()
        self._build_knowledge_tab()
        self._build_evolution_tab()
        self.notebook.pack(fill='both', expand=True)
        # 占位视图（设置：与 notebook 二态切换）。
        self._placeholder = ttk.Frame(content)
        box = ttk.Frame(self._placeholder)
        box.pack(anchor='nw', padx=16, pady=24)
        self._placeholder_title = tk.Label(box, text='', font=FONT_SECTION,
                                           bg=C['bg'], fg=C['text'])
        self._placeholder_title.pack(anchor='w')
        self._placeholder_hint = tk.Label(box, text='', font=FONT_CAPTION,
                                          bg=C['bg'], fg=C['subtext'],
                                          wraplength=200, justify='left')
        self._placeholder_hint.pack(anchor='w', pady=(6, 0))

    def _build_files_tab(self):
        files = ttk.Frame(self.notebook)
        self._files_tab = files
        header = ttk.Frame(files)
        header.pack(fill='x')
        ttk.Label(header, text='文件', font=FONT_SECTION).pack(side='left')
        for icon, tip, command in (('plus', '新建文件', self._new_file),
                                   ('chevron_up', '折叠全部', self._collapse_all),
                                   ('refresh', '刷新', self._on_refresh)):
            button = IconButton(header, icon=icon, bg=C['bg'], fg=C['ter'],
                                relief='flat', bd=0, cursor='hand2',
                                activebackground=C['hover'],
                                activeforeground=C['text'],
                                padx=2, pady=2, command=command)
            button.pack(side='left', padx=(6, 0))
            attach_tooltip(button, tip)
        self.tree = ttk.Treeview(files, show='tree', selectmode='browse', style='Coding.Treeview')
        scroll = ttk.Scrollbar(files, command=self.tree.yview)
        scroll.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<Double-1>', self._on_open_tree)
        self.tree.bind('<Return>', self._on_open_tree)
        self.tree.bind('<Button-3>', self._context_menu)
        self.tree.bind('<Button-2>', self._context_menu)
        self.tree.bind('<<TreeviewOpen>>', lambda _e: self._on_expand(True))
        self.tree.bind('<<TreeviewClose>>', lambda _e: self._on_expand(False))
        self.notebook.add(files, text='文件')
        self._configure_status_tags(self.tree)

    def _build_git_tab(self):
        git = ttk.Frame(self.notebook)
        self._git_tab = git
        choice = ttk.Combobox(git, textvariable=self._diff_var,
                              state='readonly',
                              values=['combined', 'working', 'staged'],
                              width=18)
        choice.pack(fill='x', pady=4)
        choice.bind('<<ComboboxSelected>>', self._on_show_diff)
        # 图例行：字母 + 中文标签的文字冗余（不依赖颜色也能读状态）。
        legend = self.status_legend = ttk.Frame(git)
        legend.pack(fill='x')
        for code in ('M', 'A', 'D', 'U', 'R', 'X'):
            letter, token, label = GIT_STATUS_STYLE[code]
            tk.Label(legend, text=f'{letter} {label}', font=FONT_CAPTION,
                     bg=C['bg'], fg=C[token]).pack(side='left', padx=(0, 8))
        self.changes = ttk.Treeview(git, show='tree', selectmode='browse', style='Coding.Treeview')
        self.changes.pack(fill='both', expand=True)
        self.changes.bind('<<TreeviewSelect>>', self._on_show_diff)
        self.changes.bind('<Double-1>', lambda _e: self._on_open_changed())
        self.notebook.add(git, text='Git 变更')
        self._configure_status_tags(self.changes)

    def _configure_status_tags(self, tree):
        for code, (_letter, token, _label) in GIT_STATUS_STYLE.items():
            tree.tag_configure(f'git-{code}', foreground=C[token])

    def _build_tasks_tab(self):
        """任务 Tab：当前任务的子代理 / 调度队列占位。真实内容由 app 注入。"""
        tasks = ttk.Frame(self.notebook)
        self._tasks_tab = tasks
        self._tasks_placeholder = ttk.Label(
            tasks,
            text='任务视图由 app 注入（子代理 / 调度 / 工作流状态）。',
            foreground=C['subtext'], wraplength=200, justify='left',
            padding=16)
        self._tasks_placeholder.pack(anchor='nw')
        self.notebook.add(tasks, text='任务')
        return tasks

    def _build_knowledge_tab(self):
        """知识库 Tab：MemoryStore + KnowledgeBase 占位。真实内容由 app 注入。"""
        knowledge = ttk.Frame(self.notebook)
        self._knowledge_tab = knowledge
        self._knowledge_placeholder = ttk.Label(
            knowledge,
            text='知识库视图由 app 注入（pitfall / lesson / note 检索与注入预览）。',
            foreground=C['subtext'], wraplength=200, justify='left',
            padding=16)
        self._knowledge_placeholder.pack(anchor='nw')
        self.notebook.add(knowledge, text='知识')
        return knowledge

    def _build_evolution_tab(self):
        """演化 Tab：EvolutionEngine 候选审批占位。真实内容由 app 注入。"""
        evolution = ttk.Frame(self.notebook)
        self._evolution_tab = evolution
        self._evolution_placeholder = ttk.Label(
            evolution,
            text='演化视图由 app 注入（pending 候选 / approve · reject · quarantine）。',
            foreground=C['subtext'], wraplength=200, justify='left',
            padding=16)
        self._evolution_placeholder.pack(anchor='nw')
        self.notebook.add(evolution, text='演化')
        return evolution

    def install_tasks_view(self, view):
        """app 注入真实任务视图（替换占位）。"""
        self._tasks_placeholder.pack_forget()
        view.pack(in_=self._tasks_tab, fill='both', expand=True)

    def install_knowledge_view(self, view):
        self._knowledge_placeholder.pack_forget()
        view.pack(in_=self._knowledge_tab, fill='both', expand=True)

    def install_evolution_view(self, view):
        self._evolution_placeholder.pack_forget()
        view.pack(in_=self._evolution_tab, fill='both', expand=True)

    # ── 视图切换 ──────────────────────────────────────────────────
    def select_view(self, key):
        """公开视图切换入口（M6 Review Focus 等外部驱动用）。

        内部经 ActivityBar.select → _select_view 既有回调链执行，
        不引入第二条切换路径；键不存在时静默忽略（与活动栏一致）。
        """
        self._activity.select(key)

    def _select_view(self, key):
        if key in ('files', 'search', 'git'):
            self._placeholder.pack_forget()
            self._search_row.pack(fill='x', pady=(0, 6))
            self.notebook.pack(fill='both', expand=True)
            self.notebook.select(self._git_tab if key == 'git' else self._files_tab)
            if key == 'search':
                self.search_entry.focus_set()
        elif key == 'tasks':
            self._placeholder.pack_forget()
            self._search_row.pack_forget()
            self.notebook.pack(fill='both', expand=True)
            self.notebook.select(self._tasks_tab)
            if callable(self._on_show_tasks):
                self._on_show_tasks()
        elif key == 'knowledge':
            self._placeholder.pack_forget()
            self._search_row.pack_forget()
            self.notebook.pack(fill='both', expand=True)
            self.notebook.select(self._knowledge_tab)
            if callable(self._on_show_knowledge):
                self._on_show_knowledge()
        elif key == 'evolution':
            self._placeholder.pack_forget()
            self._search_row.pack_forget()
            self.notebook.pack(fill='both', expand=True)
            self.notebook.select(self._evolution_tab)
            if callable(self._on_show_evolution):
                self._on_show_evolution()
        elif key == 'approvals':
            # 审批 Tab 在底部面板，本活动栏选中时：滚动到底部并切到该 Tab。
            self._placeholder.pack_forget()
            self._search_row.pack_forget()
            self.notebook.pack(fill='both', expand=True)
            if callable(self._on_show_approvals):
                self._on_show_approvals()
        elif key in self.PLACEHOLDERS:
            self.notebook.pack_forget()
            self._search_row.pack_forget()
            title, hint = self.PLACEHOLDERS[key]
            self._placeholder_title.configure(text=title)
            self._placeholder_hint.configure(text=hint)
            self._placeholder.pack(fill='both', expand=True)
        elif key == 'run':
            self._on_run()

    # ── 数据填充（app._project_scan 的唯一入口）────────────────────
    def populate(self, value, root, *, changes_selection=()):
        """用一次扫描结果重填文件树与 Git 变更清单。

        ``value`` 为 ``WorkspaceSource.scan()`` 的返回（nodes /
        status / branch），``root`` 为项目根 Path。展开态、过滤词、
        选择恢复语义与既有实现逐项一致；新增的是状态字母徽标、
        主题色 tag 与文件类型图标。
        """
        tree = self.tree
        selected = tree.selection()
        tree.delete(*tree.get_children())
        status = value.get('status') or {}
        search = self._search_var.get()
        expanded = self._expanded_getter()
        for node in value['nodes']:
            path = node['path']
            if path == str(root):
                continue
            parent = str(Path(path).parent)
            parent = parent if tree.exists(parent) and not search else ''
            relative = Path(path).relative_to(root).as_posix()
            code = normalize_status(status.get(relative, ''))
            letter = status_letter(code)
            text = f'{letter} {node["name"]}' if letter else node['name']
            kind = 'dir' if node['is_dir'] else 'file'
            tree.insert(parent, 'end', iid=path, text=text,
                        open=path in expanded, values=(kind,),
                        image=self._node_image(kind, node['name']),
                        tags=(status_tag(code),) if code else ())
            if node['is_dir'] and path not in expanded:
                tree.insert(path, 'end', text='…')
        for item in selected:
            if tree.exists(item):
                tree.selection_set(item)
        changes = self.changes
        changes.delete(*changes.get_children())
        for path, raw in status.items():
            code = normalize_status(raw)
            letter = status_letter(code)
            changes.insert('', 'end', iid=path,
                           text=f'{letter} {path}' if letter else path,
                           tags=(status_tag(code),) if code else ())
        for path in changes_selection:
            if changes.exists(path):
                changes.selection_set(path)

    def _node_image(self, kind, name):
        if kind == 'dir':
            icon = 'folder_open'
        else:
            icon = file_icon_name(name)
        image = icon_image(self.tree, icon, size=16, fg=C['subtext'])
        if image is None:
            return ''
        return image

    # ── 选择与文件操作 ─────────────────────────────────────────────
    def _selected(self):
        selection = self.tree.selection()
        return Path(selection[0]) if selection else None

    def _target_dir(self):
        """新建操作的目标目录：选中目录用目录，选中文件用其父级，
        无选择用项目根（None 由调用方回落 boundary.root）。"""
        path = self._selected()
        if path is None:
            return None
        return path if path.is_dir() else path.parent

    def _new_file(self):
        boundary = self._boundary_provider()
        if boundary is None:
            return
        name = simpledialog.askstring('新建文件', '文件名：', parent=self)
        if not name or not name.strip():
            return
        try:
            created = create_file(boundary, self._target_dir() or boundary.root,
                                  name)
        except (PermissionError, OSError, ValueError) as error:
            messagebox.showerror('新建文件失败', str(error), parent=self)
            return
        self._on_open_file(created)
        self._on_refresh()

    def _new_folder(self):
        boundary = self._boundary_provider()
        if boundary is None:
            return
        name = simpledialog.askstring('新建文件夹', '文件夹名：', parent=self)
        if not name or not name.strip():
            return
        try:
            create_dir(boundary, self._target_dir() or boundary.root, name)
        except (PermissionError, OSError, ValueError) as error:
            messagebox.showerror('新建文件夹失败', str(error), parent=self)
            return
        self._on_refresh()

    def _rename(self):
        boundary = self._boundary_provider()
        path = self._selected()
        if boundary is None or path is None:
            return
        name = simpledialog.askstring('重命名', '新名称：',
                                      initialvalue=path.name, parent=self)
        if not name or not name.strip():
            return
        try:
            rename_path(boundary, path, name)
        except (PermissionError, OSError, ValueError) as error:
            messagebox.showerror('重命名失败', str(error), parent=self)
            return
        self._status_var.set(f'已重命名为 {name.strip()}')
        self._on_refresh()

    def _delete(self):
        boundary = self._boundary_provider()
        path = self._selected()
        if boundary is None or path is None:
            return
        if not messagebox.askyesno('删除', f'确定删除 {path.name}？'
                                   '此操作不可撤销。', parent=self):
            return
        try:
            delete_path(boundary, path)
        except (PermissionError, OSError, ValueError) as error:
            messagebox.showerror('删除失败', str(error), parent=self)
            return
        self._status_var.set(f'已删除 {path.name}')
        self._on_refresh()

    def _copy_path(self):
        boundary = self._boundary_provider()
        if boundary is None:
            return
        path = self._selected() or boundary.root
        self.clipboard_clear()
        self.clipboard_append(str(path))
        self._status_var.set(f'已复制路径 {path}')

    def _collapse_all(self):
        def walk(item):
            for child in self.tree.get_children(item):
                if self.tree.item(child, 'values') == ('dir',):
                    self.tree.item(child, open=False)
                    walk(child)
        walk('')
        # 展开态是 app 持久状态：清空后 refresh 才会保持折叠。
        self._expanded_getter().clear()
        self._on_refresh()

    def _context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
        menu = tk.Menu(self, tearoff=False, bg=C['bg'], fg=C['text'])
        menu.add_command(label='打开', command=lambda: self._on_open_tree(None))
        menu.add_command(label='新建文件', command=self._new_file)
        menu.add_command(label='新建文件夹', command=self._new_folder)
        menu.add_command(label='重命名', command=self._rename)
        menu.add_command(label='删除', command=self._delete)
        menu.add_command(label='复制路径', command=self._copy_path)
        menu.add_separator()
        menu.add_command(label='刷新', command=self._on_refresh)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
