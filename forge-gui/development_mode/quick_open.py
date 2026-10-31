"""Ctrl+P 快速打开：项目内文件的模糊搜索浮层。

为什么单独成文件：Coding 工作台（``app.py``）已 3000+ 行，且多处
布局路径被 Ownership 保护组覆盖。快速打开是**纯增量**能力——没有
既有调用点、不改任何布局/挂载路径，独立成模块既能单测（``rank``
是纯函数，不依赖 Tk 窗口），也便于宿主复用。

三条约束与工作台其余部分保持一致：

* **不建第二个爬虫**——文件清单只来自 ``workspace._scan_tree``
  （与文件树/搜索同一条扫描路径），由宿主缓存，切项目时失效；
* **只读**——选中项交回宿主既有的 ``open_file``（边界校验 / 编码
  探测 / 只读保护都在那条路径上），本模块不碰文件系统；
* **不伪造**——没有匹配项就显示「没有匹配项」，不编造建议。

列表用自绘行而不是 Listbox：成熟产品的快速打开是「文件名亮、目录暗」
的两级文字 + 整行高亮；Listbox 只能给整行一种颜色，二十条
``forge-gui/development_mode/...`` 全糊成一串，认不出是哪个文件。
"""
import tkinter as tk

from gui_theme import C, FONT_CAPTION, FONT_UI

# 单次浮层最多返回的结果数（更多匹配靠继续输入收窄）。
LIMIT = 60
# 实际画出来的行数上限（再多的匹配靠打字收窄，不做滚动）。
VISIBLE_ROWS = 11
ROW_HEIGHT = 26


def _match_score(path, needle):
    """单条路径的匹配分；不匹配返回 None。

    评分优先级：文件名前缀 > 文件名子串 > 全路径子串 > 子序列。
    同档内越靠前命中、路径越短，分数越高——这样输入 ``app`` 时
    ``development_mode/app.py`` 会排在 ``forge/app_store/schema.py`` 前面。
    """
    name = path.rsplit('/', 1)[-1]
    low_name = name.casefold()
    low_path = path.casefold()
    needle = str(needle or '').casefold()
    penalty = len(path) * 0.05
    if needle in low_name:
        score = 1000.0 - low_name.index(needle) * 2
        if low_name.startswith(needle):
            score += 150
        # 文件名（去扩展名）与查询完全相等时再抬一档：输入 ``app`` 想找
        # 的是 ``app.py``，不是 ``appendix.md``——两者都以 app 开头，
        # 只有「名字几乎就是查询」这一条能把它们分开。
        if low_name.rsplit('.', 1)[0] == needle:
            score += 120
        # 名字越短，与查询越接近（``app.py`` 优于 ``application_state.py``）
        score += max(0, 40 - len(low_name))
        return score - penalty
    if needle in low_path:
        return 700.0 - low_path.index(needle) - penalty
    position = -1
    gaps = 0
    for char in needle:
        found = low_path.find(char, position + 1)
        if found < 0:
            return None
        gaps += found - position - 1
        position = found
    return 400.0 - gaps - penalty


def rank(paths, query, limit=LIMIT):
    """按匹配度排序的路径列表（空查询保持原序，截断到 limit）。"""
    needle = str(query or '').strip().casefold()
    if not needle:
        return list(paths)[:limit]
    scored = []
    for order, path in enumerate(paths):
        score = _match_score(path, needle)
        if score is not None:
            scored.append((score, order, path))
    # 分数降序；同分时保持扫描顺序（稳定，不因重排抖动）
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [path for _score, _order, path in scored[:limit]]


def split_path(path):
    """``forge/app.py`` → ``('app.py', 'forge/')``；根目录文件目录部分为空串。"""
    head, _sep, name = str(path).rpartition('/')
    return name, (head + '/' if head else '')


def clip(text, limit, *, from_left=False):
    """超长文本截断成 ``…尾`` 或 ``头…``（按字符数）。

    自绘行里文件名与目录各占一侧，长名字会顶到目录文字上（实拍截图
    里 ``app.py.bak-codingmode-20261013`` 直接压住了目录）。文件名保留
    头部（后缀名有意义），目录保留尾部（离文件最近的层级最有用）。
    """
    text = str(text)
    if limit <= 1 or len(text) <= limit:
        return text
    return _clip_px(text, limit, len, from_left=from_left)


def _clip_px(text, room, measure, *, from_left=False, ellipsis='…'):
    """把 ``text`` 裁到 ``room``（由 ``measure`` 量出的单位）以内。

    ``measure`` 注入而非内建——传 ``len`` 就是按字符裁（:func:`clip`），
    传字体 ``measure`` 就是按像素裁。二分找最长能放下的长度，不会像
    逐字退那样在长文本上退成 O(n²)。
    """
    text = str(text)
    if not text or room <= 0:
        return ''
    if measure(text) <= room:
        return text
    ell = measure(ellipsis)
    if room <= ell:
        return ''
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = (ellipsis + text[-mid:]) if from_left else (text[:mid] + ellipsis)
        if measure(candidate) <= room:
            lo = mid
        else:
            hi = mid - 1
    if lo <= 0:
        return ellipsis
    return (ellipsis + text[-lo:]) if from_left else (text[:lo] + ellipsis)


def fit_pair(name, directory, budget, measure_name, measure_dir, *,
             gap=10, dir_share=0.45):
    """把「文件名 + 目录」压进一行 ``budget``，两侧都保留省略号。

    为什么不能交给 pack 去挤：pack 里后放的控件吃亏，被挤掉的是**尾部
    像素**——目录从左边被啃掉几个字，连 ``…`` 一起消失（实拍里
    ``forge-gui/development_mode/`` 变成 ``rge-gui/development_mode/``，
    看不出这行是路径截断还是数据有问题）。

    这里先在像素层面分配：目录最多拿 ``dir_share`` 的预算（超了从左边
    裁，保留离文件最近的层级），剩下的留给文件名（从右边裁，保留后缀）。
    """
    name = str(name)
    directory = str(directory)
    budget = max(0, int(budget))
    dir_room = min(measure_dir(directory), int(budget * dir_share)) if directory else 0
    name_room = max(0, budget - (gap if directory else 0) - dir_room)
    return (
        _clip_px(name, name_room, measure_name),
        _clip_px(directory, dir_room, measure_dir, from_left=True) if directory else '',
    )


class QuickOpen:
    """无边框浮层：输入即筛，↑↓ 选择，Enter 打开，Esc 关闭。

    浮层挂在宿主 toplevel 上、以 ``overrideredirect`` 去掉标题栏，
    位置按宿主窗口水平居中、垂直靠近顶部（IDE 惯例：视线不离开
    编辑区）。不抢 grab——hosted 形态下与宿主共用同一个 root，
    强 grab 会把宿主的其它交互一起锁住。
    """

    WIDTH = 620

    HINTS = (('↑↓', '选择'), ('Tab', '补全'), ('Enter', '打开'), ('Esc', '关闭'))
    STATUS = '只读 · 不修改项目'

    def __init__(self, master, paths, on_open, *, on_close=None):
        self._paths = list(paths)
        self._results = []
        self._selected = 0
        self._rows = []
        self._origin = None
        self._on_open = on_open
        self._on_close = on_close
        self._closed = False

        top = tk.Toplevel(master)
        self.window = top
        top.withdraw()
        top.overrideredirect(True)
        top.configure(bg=C['border_hi'])
        # 1px 描边 + 实底：overrideredirect 窗口没有系统边框和阴影，
        # 不自己画一道边界，浮层就会和下面的编辑器糊成一片（实拍截图
        # 里就是"文字飘在代码上"）。highlightthickness 比外层 padding
        # 更稳——padding 会被子控件的 fill 吃掉。
        shell = tk.Frame(top, bg=C['surface'], bd=0,
                         highlightthickness=1,
                         highlightbackground=C['border_hi'],
                         highlightcolor=C['border_hi'])
        shell.pack(fill='both', expand=True)
        self._shell = shell

        field = tk.Frame(shell, bg=C['input_bg'])
        field.pack(fill='x')
        self._field = field
        self.query = tk.StringVar(value='')
        self.entry = tk.Entry(
            field, textvariable=self.query, font=FONT_UI, bg=C['input_bg'],
            fg=C['text'], insertbackground=C['text'], relief='flat', bd=0,
            highlightthickness=0)
        self.entry.pack(fill='x', padx=10, pady=9)
        # 计数常驻输入行右侧：脚注在窄窗里会被压缩，这里不会。
        self.count = tk.Label(field, text='', font=FONT_CAPTION,
                              bg=C['input_bg'], fg=C['ter'])
        self.count.place(relx=1.0, rely=0.5, anchor='e', x=-12)
        tk.Frame(shell, bg=C['border'], height=1).pack(fill='x')

        # 结果区：自绘行（文件名亮 / 目录暗），高度按行数固定。
        self._list = tk.Frame(shell, bg=C['surface'])
        self._list.pack(fill='both', expand=True)
        self._empty = tk.Label(self._list, text='', font=FONT_CAPTION,
                               bg=C['surface'], fg=C['ter'])

        footer = tk.Frame(shell, bg=C['surface'])
        footer.pack(fill='x')
        self._footer = footer
        tk.Frame(footer, bg=C['border'], height=1).pack(fill='x')
        # 底部键位提示：键帽 + 说明（与输入区、欢迎页同一套语法）。
        # tone='quiet'——辅助信息不跟正文抢注意力。
        from .buttons import keycap
        hints = tk.Frame(footer, bg=C['surface'])
        hints.pack(side='right', padx=10, pady=4)
        for keys, text in self.HINTS:
            cell = tk.Frame(hints, bg=C['surface'])
            cell.pack(side='left', padx=(8, 0))
            keycap(cell, keys, bg=C['surface'], tone='quiet').pack(side='left')
            tk.Label(cell, text=text, font=FONT_CAPTION, bg=C['surface'],
                     fg=C['ter']).pack(side='left', padx=(4, 0))
        self.status = tk.Label(footer, text=self.STATUS, font=FONT_CAPTION,
                               bg=C['surface'], fg=C['ter'], anchor='w')
        self.status.pack(side='left', padx=10, pady=4)

        self.query.trace_add('write', lambda *_a: self.refresh())
        self.entry.bind('<Down>', lambda _e: self._move(1))
        self.entry.bind('<Up>', lambda _e: self._move(-1))
        # VS Code 同款：列表内 Ctrl+N/Ctrl+P（或 Ctrl+J/Ctrl+K）上下移动，
        # 手不用离开主键区。entry 级绑定先于 toplevel 的全局 Ctrl+P 执行，
        # 返回 'break' 后不会再去"重新打开浮层"。
        for sequence, delta in (('<Control-n>', 1), ('<Control-j>', 1),
                                ('<Control-p>', -1), ('<Control-k>', -1)):
            self.entry.bind(sequence, lambda _e, d=delta: self._move(d))
        self.entry.bind('<Next>', lambda _e: self._move(VISIBLE_ROWS))
        self.entry.bind('<Prior>', lambda _e: self._move(-VISIBLE_ROWS))
        self.entry.bind('<Tab>', lambda _e: self._complete())
        self.entry.bind('<Return>', lambda _e: self._accept())
        self.entry.bind('<Escape>', lambda _e: self.close())
        top.bind('<Escape>', lambda _e: self.close())
        # 点浮层外部即关闭：overrideredirect 窗口不参与系统焦点链，
        # FocusOut 是最稳的信号（无 grab，宿主其余控件仍可点）。
        top.bind('<FocusOut>', self._focus_lost)

        self.refresh()
        self._place(master)
        top.deiconify()
        top.lift()
        try:
            top.focus_force()
        except tk.TclError:
            pass
        self.entry.focus_set()

    # ── 状态 ──────────────────────────────────────────────────
    @property
    def alive(self):
        if self._closed:
            return False
        try:
            return bool(self.window.winfo_exists())
        except tk.TclError:
            return False

    # ── 布局 ──────────────────────────────────────────────────
    def _place(self, master):
        """水平居中于宿主窗口，垂直贴近顶部（不遮住输入焦点区域）。"""
        try:
            host = master.winfo_toplevel()
            width = host.winfo_width()
            self._origin = (host.winfo_rootx() + max(0, (width - self.WIDTH) // 2),
                            host.winfo_rooty() + 96)
        except tk.TclError:
            self._origin = (160, 120)
        self._resize()

    def _resize(self):
        """按当前结果条数收放高度。

        高度问控件本身（``winfo_reqheight``）而不是按行数手算：行高由
        字体度量决定，手算偏小会把底部脚注整块裁掉——pack 在空间不足
        时牺牲的正是最后打包的 slave。
        """
        if self._origin is None:
            return
        rows = max(1, min(VISIBLE_ROWS, len(self._results)))
        try:
            self._list.configure(height=rows * ROW_HEIGHT)
            self._list.pack_propagate(False)
        except tk.TclError:
            pass
        # 高度 = 输入行 + 分隔线 + 行数×行高 + 脚注 + 描边。输入行与脚注
        # 的高度问控件自己（键帽比普通文字高，写死 34 会把脚注裁掉一半）。
        try:
            self._field.update_idletasks()
            self._footer.update_idletasks()
            head = self._field.winfo_reqheight()
            foot = self._footer.winfo_reqheight()
        except tk.TclError:
            head, foot = 40, 44
        # +8 是安全余量：脚注里的键帽是 Canvas，reqheight 偶尔比实际
        # 绘制高度少几像素，不留余量时最后一排键帽会被窗口下沿切掉。
        height = head + 1 + rows * ROW_HEIGHT + foot + 2 + 8
        x, y = self._origin
        try:
            self.window.geometry(f'{self.WIDTH}x{height}+{x}+{y}')
        except tk.TclError:
            pass

    # ── 渲染 ──────────────────────────────────────────────────
    def _measure_pair(self):
        """行内两段文字的量宽函数（按需建一次 Font，随缩放变化重取）。"""
        from tkinter import font as tkfont
        name_font = tkfont.Font(font=FONT_UI)
        dir_font = tkfont.Font(font=FONT_CAPTION)
        return name_font.measure, dir_font.measure

    def _build_rows(self):
        for row in self._rows:
            row[0].destroy()
        self._rows = []
        measure_name, measure_dir = self._measure_pair()
        # 行内固定占位：名字左内边距 10 + 目录左内边距 10 + 目录右内边距 12。
        chrome = 10 + 10 + 12
        # WIDTH 是浮层外宽，减 2 是两侧 1px 描边。
        budget = self.WIDTH - 2
        for index, path in enumerate(self._results[:VISIBLE_ROWS]):
            name, directory = split_path(path)
            # 宽度分配在像素层面算好再交给 Label：靠 pack 去挤的话，
            # 吃亏的是后放的目录，被啃掉的是尾部像素（连省略号一起
            # 消失），看起来像数据坏了而不是被截断。
            name_text, dir_text = fit_pair(
                name, directory, budget, measure_name, measure_dir,
                gap=chrome, dir_share=0.45)
            holder = tk.Frame(self._list, bg=C['surface'], height=ROW_HEIGHT)
            holder.pack(fill='x')
            holder.pack_propagate(False)
            # padx/bd 显式归零：tk.Label 默认各留 1px 内边距，会让实际
            # 宽度比量宽结果多出几像素——那样算好的预算又会被挤掉一截。
            label = tk.Label(holder, text=name_text, font=FONT_UI,
                             bg=C['surface'], fg=C['text'], anchor='w',
                             padx=0, bd=0)
            label.pack(side='left', fill='x', expand=True, padx=(10, 0))
            # 目录暗一档并靠右：文件名先被看到，路径是补充信息。
            tail = tk.Label(holder, text=dir_text,
                            font=FONT_CAPTION, bg=C['surface'], fg=C['ter'],
                            anchor='e', padx=0, bd=0)
            tail.pack(side='right', padx=(10, 12))
            for widget in (holder, label, tail):
                widget.bind('<Button-1>', lambda _e, i=index: self._pick(i))
                widget.bind('<Enter>', lambda _e, i=index: self._hover(i, True))
                widget.bind('<Leave>', lambda _e, i=index: self._hover(i, False))
                widget.configure(cursor='hand2')
            self._rows.append((holder, label, tail))

    def _paint(self):
        """重刷选中态（整行高亮 + 文字提亮，与 VS Code 的选中行一致）。"""
        for index, (holder, label, tail) in enumerate(self._rows):
            active = index == self._selected
            bg = C['sel'] if active else C['surface']
            try:
                holder.configure(bg=bg)
                label.configure(bg=bg, fg='#FFFFFF' if active else C['text'])
                tail.configure(bg=bg, fg=C['accent_text'] if active else C['ter'])
            except tk.TclError:
                pass

    def _hover(self, index, entering):
        if index == self._selected:
            return
        try:
            holder, label, tail = self._rows[index]
            bg = C['hover'] if entering else C['surface']
            holder.configure(bg=bg)
            label.configure(bg=bg)
            tail.configure(bg=bg)
        except (IndexError, tk.TclError):
            pass

    def refresh(self):
        """按当前查询重算结果并重绘。"""
        self._results = rank(self._paths, self.query.get())
        self._selected = 0
        try:
            self._empty.pack_forget()
            self._build_rows()
        except tk.TclError:
            return
        total = len(self._paths)
        if not self._results:
            self.count.configure(text='无匹配')
            self._empty.configure(text=f'没有匹配项 · 项目内共 {total} 个文件')
            self._empty.pack(pady=14)
        else:
            shown = min(VISIBLE_ROWS, len(self._results))
            self.count.configure(text=f'{len(self._results)} / {total}')
            self._paint()
            if len(self._results) > shown:
                # 超出可视行时给一句收窄提示，避免用户以为"就这些"。
                self.status.configure(
                    text=f'还有 {len(self._results) - shown} 条')
            else:
                self.status.configure(text=self.STATUS)
        self._resize()

    # ── 交互 ──────────────────────────────────────────────────
    def _move(self, delta):
        if not self._results:
            return 'break'
        limit = min(VISIBLE_ROWS, len(self._results))
        self._selected = max(0, min(limit - 1, self._selected + delta))
        self._paint()
        return 'break'

    def _pick(self, index):
        if 0 <= index < len(self._results):
            self._selected = index
            self._accept()

    def _complete(self):
        """Tab：把选中项的文件名补进输入框（VS Code 的补全语义）。"""
        if not self._results:
            return 'break'
        name, _directory = split_path(self._results[self._selected])
        self.query.set(name)
        try:
            self.entry.icursor('end')
        except tk.TclError:
            pass
        return 'break'

    def _accept(self):
        if not self._results:
            return 'break'
        path = self._results[self._selected]
        self.close()
        if self._on_open is not None:
            self._on_open(path)
        return 'break'

    def _focus_lost(self, _event=None):
        # 延迟一拍：点列表项会先触发 FocusOut，立刻关会吞掉这次点击。
        try:
            self.window.after(120, self._close_if_unfocused)
        except tk.TclError:
            pass

    def _close_if_unfocused(self):
        if not self.alive:
            return
        try:
            focused = self.window.focus_get()
        except (tk.TclError, KeyError):
            focused = None
        if focused is None or not str(focused).startswith(str(self.window)):
            self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.window.destroy()
        except tk.TclError:
            pass
        if self._on_close is not None:
            self._on_close()
