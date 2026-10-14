"""M4 编辑器增强：行号槽 + 当前行高亮 + 搜索替换 + gutter 修改标记 +
另存为 + Python 语法高亮 + 分屏参考视图（编辑器与布局域）。

结构约束（与 M2/M3 同范式）：

* 每个标签页一个 :class:`EditorArea`（Notebook tab 的 frame 即父容器）。
  app 的 ``row['widget']`` 契约不变——``EditorArea.text`` 就是那个
  Text，既有 save / reload / close / attach / _replace_buffer 全部无感；
  行为回调（on_change / on_save）经构造参数注入，本模块不读 app
  私有结构。
* 语法高亮**按接口约定消费** ``gui_theme.setup_code_tags`` /
  ``highlight_python``（token 化与 tag 注册、配色的唯一真源在主仓，
  被 chat / workspace 跨域共享）——本模块不复制第二套配色。示例图
  的 VS Code Dark+ 精确色值与主仓 One Dark 系 token 的差异列在交付
  报告，由集成方的 gui_theme 单独提交对齐。
* 行号槽与主文本**同字体**（FONT_MONO_XS 行高与正文不同，yview 比例
  对齐会漂移），共享竖向滚动；修改标记是当前缓冲区相对
  ``document.original`` 的行级 diff（difflib.SequenceMatcher），
  只给行号上色、不改内容；超过 ``MARKER_LINE_LIMIT`` 行放弃标记
  （大文件保护），不算伪造。
* 分屏按钮切出**同文件的只读参考镜像**——真实内容、明示只读，
  不是 VS Code 的独立编辑组（那需要第二套文档状态，属编辑器域后续
  演化）；more 按钮给真实动作菜单（app 侧接线）。
"""
import difflib
import os
import tempfile
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from gui_theme import C, FONT_MONO_SM, gutter_lines, highlight_python, \
    setup_code_tags, style_editor
from forge.development.editor import MAX_DOCUMENT, fingerprint

# 修改标记的行数上限：超过则放弃行级 diff（大文件保护）。
MARKER_LINE_LIMIT = 4000


def save_document_as(boundary, document, target):
    """另存为：把当前缓冲区写到 ``target`` 并把文档改绑到新路径。

    原子写（临时文件 + ``os.replace``）、换行 / 编码规则与
    ``Document._save`` 一致；经 ``boundary.path()`` 校验（项目外与
    .git 内拒绝）。成功后 ``document`` 的 path / original / digest /
    status 改绑到新文件——VS Code 语义：另存为后当前标签编辑的就
    是新文件。与 ``Document.save`` 的差异：不检查外部冲突（目标是
    用户显式选择的新路径，允许覆盖），但仍持 FileRevisionGuard 的
    工作区锁，避免与其它标签的保存交错。
    """
    from forge.development.guard import FileRevisionGuard
    p = boundary.path(target)
    raw = document.text.replace('\n', document.newline).encode(
        document.encoding, 'strict')
    if len(raw) > MAX_DOCUMENT:
        raise ValueError('Editor file limit is 2 MiB')
    with FileRevisionGuard(boundary.root).lock:
        fd, name = tempfile.mkstemp(prefix='.' + p.name + '-',
                                    suffix='.tmp', dir=p.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
            os.replace(name, p)
        except BaseException:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise
    document.path = p
    document.original = document.text
    document.digest = fingerprint(raw)
    document.status = 'clean'
    return p


class EditorArea(ttk.Frame):
    """单标签页编辑器区：行号槽 + 代码文本 + 搜索替换 + 修改标记 +
    只读参考镜像（分屏）。

    构造参数：

    ``document``      forge.development.editor.Document（缓冲区真源）
    ``on_change``     ``<<Modified>>`` 回调（app.document_changed）
    ``on_save``       ``<Control-s>`` 回调（app.save_current）
    """

    def __init__(self, master, document, *, on_change=None, on_save=None, **kw):
        super().__init__(master, **kw)
        self.document = document
        self._on_change = on_change
        self._on_save = on_save
        self._search_open = False
        self._reference_visible = False
        self._build()
        self.set_text(document.text)

    # ── 构建 ──────────────────────────────────────────────────────
    def _build(self):
        self._build_search_bar()
        self._body_frame = body = ttk.Frame(self)
        body.pack(fill='both', expand=True)
        self.gutter = tk.Text(body, width=4, bg=C['code_bg'], fg=C['muted'],
                              font=FONT_MONO_SM, padx=4, pady=3, relief='flat',
                              highlightthickness=0, bd=0, takefocus=0,
                              wrap='none', state='disabled', cursor='arrow',
                              exportselection=False)
        self.text = tk.Text(body, wrap='none', font=FONT_MONO_SM, undo=True)
        style_editor(self.text)
        # 语法高亮 tag 注册（接口约定：真源在 gui_theme）。
        setup_code_tags(self.text)
        # pack 次序即超订让位优先级（与 tab_row 按钮位同规则）：固定尺寸
        # 的滚动条必须先于 expand 的正文 pack——正文 Text 默认 80 字符
        # 请求会超订 body 腔体，后 pack 的竖向滚动条恒得 1x1 且不映射
        # （1200x760 实测）。原 _document_loaded 的顺序本就是滚动条
        # 优先，这里保持同一约定。
        sy = self._vbar = ttk.Scrollbar(body, command=self._vscroll)
        sy.pack(side='right', fill='y')
        sx = ttk.Scrollbar(body, orient='horizontal', command=self.text.xview)
        sx.pack(side='bottom', fill='x')
        self.text.configure(yscrollcommand=self._yscroll, xscrollcommand=sx.set)
        self.gutter.pack(side='left', fill='y')
        self.text.pack(side='left', fill='both', expand=True)
        # 修改标记 / 当前行 / 搜索命中的 tag（gutter 与 text 各一套）。
        self.gutter.tag_configure('mod', foreground=C['warn'])
        self.gutter.tag_configure('add', foreground=C['ok'])
        self.text.tag_configure('current_line', background=C['hover'])
        self.text.tag_configure('search_hit', background=C['warn_soft'])
        self.text.tag_configure('search_current', background=C['warn'],
                                foreground=C['black'])
        # 分屏参考镜像（默认隐藏；与正文同字体同行高，只读）。请求尺寸
        # 取小值（height=10）——Text 默认 80x24 字符的请求会把所在帧的
        # 腔体超订，后加 slave 的分配会坍缩（见 _repack_body 注释）。
        self.reference = tk.Text(self, wrap='none', height=10,
                                 font=FONT_MONO_SM,
                                 state='disabled', cursor='arrow',
                                 relief='flat', highlightthickness=0, bd=0,
                                 takefocus=0, exportselection=False)
        setup_code_tags(self.reference)
        # 事件：修改 / 光标 / 保存 / 搜索开关。
        self.text.bind('<<Modified>>', self._on_modified)
        self.text.bind('<KeyRelease>', self._after_caret)
        self.text.bind('<ButtonRelease-1>', self._after_caret)
        self.text.bind('<Control-s>', self._save)
        self.text.bind('<Control-f>', lambda _e: (self.open_search(), 'break')[-1])
        self.text.bind('<Escape>', lambda _e: (self.close_search(), 'break')[-1])

    def _build_search_bar(self):
        bar = self.search_bar = ttk.Frame(self)
        # 默认不 pack（收起）；open_search 时 pack 到主体之上。
        ttk.Label(bar, text='查找').pack(side='left')
        self._find_var = tk.StringVar()
        find = ttk.Entry(bar, textvariable=self._find_var, width=24)
        find.pack(side='left', padx=(4, 8))
        find.bind('<Return>', lambda _e: self.find_next())
        ttk.Label(bar, text='替换为').pack(side='left')
        self._replace_var = tk.StringVar()
        replace = ttk.Entry(bar, textvariable=self._replace_var, width=24)
        replace.pack(side='left', padx=(4, 8))
        replace.bind('<Return>', lambda _e: self.replace_current())
        self._count_var = tk.StringVar(value='')
        ttk.Label(bar, textvariable=self._count_var,
                  foreground=C['subtext']).pack(side='left', padx=(0, 8))
        for text, command in (('上一个', self.find_previous),
                              ('下一个', self.find_next),
                              ('替换', self.replace_current),
                              ('全部替换', self.replace_all)):
            ttk.Button(bar, text=text, command=command).pack(side='left')
        ttk.Button(bar, text='关闭', command=self.close_search).pack(
            side='left', padx=(8, 0))
        self._find_entry = find

    # ── 内容装载 / 刷新 ───────────────────────────────────────────
    def set_text(self, text):
        """整块设置内容（初次装载 / 重载 / 外部替换后由 app 调用）。"""
        widget = self.text
        widget.configure(state='normal')
        widget.delete('1.0', 'end')
        widget.insert('1.0', text)
        widget.edit_reset()
        widget.edit_modified(False)
        self.refresh_all()

    def refresh_all(self):
        """内容变化后的统一刷新：高亮 + 行号槽 + 修改标记 + 镜像。"""
        try:
            highlight_python(self.text)
        except Exception:
            pass  # 高亮失败不影响编辑（纯 tag 打点，理论不可达）
        self.refresh_gutter()
        self.refresh_markers()
        self._highlight_current_line()
        self._sync_reference()

    def refresh_gutter(self):
        try:
            count = int(self.text.index('end-1c').split('.')[0])
            width = max(3, len(str(count)) + 1)
        except tk.TclError:
            width = 4
        self.gutter.configure(width=width, state='normal')
        self.gutter.delete('1.0', 'end')
        self.gutter.insert('1.0', gutter_lines(self.text, gutter_width=width - 1))
        self.gutter.configure(state='disabled')
        self._sync_gutter()

    def refresh_markers(self):
        """行级 diff（缓冲区 vs document.original）→ gutter 行号上色。"""
        self.gutter.tag_remove('mod', '1.0', 'end')
        self.gutter.tag_remove('add', '1.0', 'end')
        baseline = self.document.original.splitlines()
        current = self.text.get('1.0', 'end-1c').splitlines()
        if len(baseline) + len(current) > MARKER_LINE_LIMIT:
            return  # 大文件放弃标记（保护），非伪造
        for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(
                None, baseline, current, autojunk=False).get_opcodes():
            if tag == 'equal':
                continue
            # delete 没有当前行可标；insert → add，replace → mod。
            name = 'add' if tag == 'insert' else 'mod'
            for line in range(j1 + 1, j2 + 1):
                self.gutter.tag_add(name, f'{line}.0', f'{line}.end')

    # ── 滚动同步 ──────────────────────────────────────────────────
    def _vscroll(self, *args):
        self.text.yview(*args)
        self._sync_gutter()

    def _yscroll(self, *args):
        """Text 的 yscrollcommand：转发滚动条 + 行号槽跟随。"""
        try:
            self._vbar.set(*args)
        except tk.TclError:
            pass
        self._sync_gutter()

    def _sync_gutter(self):
        """行号槽跟随正文滚动（同字体 → yview 比例对齐才成立）。"""
        try:
            frac = self.text.yview()[0]
            self.gutter.configure(state='normal')
            self.gutter.yview_moveto(frac)
            self.gutter.configure(state='disabled')
        except (tk.TclError, AttributeError):
            pass

    # ── 当前行高亮 ────────────────────────────────────────────────
    def _after_caret(self, _event=None):
        self._highlight_current_line()
        self._sync_reference()

    def _highlight_current_line(self):
        self.text.tag_remove('current_line', '1.0', 'end')
        try:
            line = self.text.index('insert').split('.')[0]
            self.text.tag_add('current_line', f'{line}.0', f'{line}.end+1c')
        except tk.TclError:
            pass

    # ── 搜索替换 ──────────────────────────────────────────────────
    def open_search(self):
        if self._search_open:
            self._find_entry.focus_set()
            return
        self._search_open = True
        # search_bar 插到主体之前：收起时不占位，展开时位于主体上方。
        self.search_bar.pack(fill='x', before=self._body_frame)
        self._find_entry.focus_set()
        self._find_entry.selection_range(0, 'end')
        self._refresh_hits()

    def close_search(self):
        self._search_open = False
        self.search_bar.pack_forget()
        self.text.tag_remove('search_hit', '1.0', 'end')
        self.text.tag_remove('search_current', '1.0', 'end')
        self.text.focus_set()

    def _matches(self):
        pattern = self._find_var.get()
        if not pattern:
            return []
        found = []
        start = '1.0'
        while True:
            pos = self.text.search(pattern, start, stopindex='end',
                                   forwards=True)
            if not pos:
                break
            end = f'{pos}+{len(pattern)}c'
            found.append((pos, end))
            start = end
        return found

    def _refresh_hits(self):
        self.text.tag_remove('search_hit', '1.0', 'end')
        self.text.tag_remove('search_current', '1.0', 'end')
        matches = self._matches()
        for pos, end in matches:
            self.text.tag_add('search_hit', pos, end)
        self._count_var.set(f'{len(matches)} 处匹配' if matches else '无匹配')

    def _current_match(self, matches):
        caret = self.text.index('insert')
        for index, (pos, end) in enumerate(matches):
            if self.text.compare(pos, '<=', caret) and \
                    self.text.compare(caret, '<=', end):
                return index
        return None

    def find_next(self):
        matches = self._matches()
        if not matches:
            self._count_var.set('无匹配')
            return
        index = self._current_match(matches)
        target = matches[0] if index is None else matches[(index + 1) % len(matches)]
        self._select_match(matches, target)

    def find_previous(self):
        matches = self._matches()
        if not matches:
            self._count_var.set('无匹配')
            return
        index = self._current_match(matches)
        target = matches[-1] if index is None else matches[(index - 1) % len(matches)]
        self._select_match(matches, target)

    def _select_match(self, matches, target):
        self.text.tag_remove('search_current', '1.0', 'end')
        self.text.tag_add('search_current', *target)
        self.text.mark_set('insert', target[0])
        self.text.see(target[0])
        self._count_var.set(f'{matches.index(target) + 1}/{len(matches)}')

    def replace_current(self):
        pattern = self._find_var.get()
        if not pattern:
            return
        matches = self._matches()
        index = self._current_match(matches)
        target = matches[index] if index is not None else (
            matches[0] if matches else None)
        if target is None:
            self._count_var.set('无匹配')
            return
        self.text.delete(*target)
        self.text.insert(target[0], self._replace_var.get())
        self._refresh_hits()
        self.refresh_gutter()
        self.refresh_markers()

    def replace_all(self):
        pattern = self._find_var.get()
        if not pattern:
            return
        matches = self._matches()
        for pos, end in reversed(matches):
            self.text.delete(pos, end)
            self.text.insert(pos, self._replace_var.get())
        self._count_var.set(f'已替换 {len(matches)} 处')
        self.refresh_gutter()
        self.refresh_markers()

    # ── 分屏参考镜像 ──────────────────────────────────────────────
    def toggle_reference(self):
        if self._reference_visible:
            self._reference_visible = False
            self.reference.pack_forget()
            self._repack_body()
            return
        self._reference_visible = True
        self.reference.pack(side='bottom', fill='both', expand=False)
        self._repack_body()
        self._sync_reference()

    def _repack_body(self):
        """强制打包器重算主体布局。

        Tk 打包器在「帧已映射后再增/减 slave、且腔体被大请求（Text
        默认 80x24 字符）超订」时不会重算分配：新 slave 恒得 1x1 且
        不映射、旧 slave 不让位（最小复叠实测）。重 pack 主体帧是
        最小触发手段。与 M1 底部面板 height=6 同属打包器配额问题，
        但不改主体请求尺寸——主体初始布局本就正常，只在动态增删时
        需要重算。
        """
        self._body_frame.pack_forget()
        self._body_frame.pack(fill='both', expand=True)

    def _sync_reference(self):
        if not self._reference_visible:
            return
        content = self.text.get('1.0', 'end-1c')
        self.reference.configure(state='normal')
        self.reference.delete('1.0', 'end')
        self.reference.insert('1.0', content)
        try:
            highlight_python(self.reference)
        except Exception:
            pass
        self.reference.configure(state='disabled')

    # ── 事件入口 ──────────────────────────────────────────────────
    def _on_modified(self, _event=None):
        if self.text.edit_modified():
            self._on_change and self._on_change(self)
            self.text.edit_modified(False)
            self.refresh_markers()
            self._sync_reference()

    def _save(self, _event=None):
        if self._on_save:
            self._on_save()
        return 'break'
