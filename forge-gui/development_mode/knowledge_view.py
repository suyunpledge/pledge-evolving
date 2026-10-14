"""知识库 Tab 内容（编辑器与布局域）。

把 :class:`forge.knowledge.KnowledgeBase` 的检索接口包装成可注入到
``ExplorerPanel._knowledge_tab`` 的视图组件。

组件契约（与 ``explorer.install_knowledge_view`` 配对）：

* 构造只需 ``(master, home)``；``home`` 是 ModeState 的根目录。
* 主区域是一个搜索框 + 检索结果列表（pitfall / lesson / note 三类
  kind 各自一行；颜色与 explorer 现有的 git status tag 一致）。
* ``refresh(query)`` 方法：app 主动拉取（手动同步 MemoryStore）。
* ``get_selection()``：返回当前选中条目的 (text, kind, source)。

优雅降级：MemoryStore 导入失败时退到 ``KnowledgePlaceholder``（纯提示），
绝不阻断 Coding Mode 启动。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from gui_theme import C, FONT_CAPTION, FONT_SECTION, FONT_UI


class KnowledgePlaceholder(ttk.Frame):
    """MemoryStore / KnowledgeBase 不可用时的占位视图。"""

    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.configure(padding=16)
        ttk.Label(self, text='知识库视图', font=FONT_SECTION,
                  foreground=C['text']).pack(anchor='w')
        ttk.Label(self,
                  text=('MemoryStore / KnowledgeBase 不可用，'
                        '请检查 forge.knowledge 导入路径。'),
                  foreground=C['subtext'], wraplength=320, justify='left').pack(
            anchor='w', pady=(6, 0))

    def refresh(self, query=''):
        pass

    def get_selection(self):
        return None


class KnowledgeView(ttk.Frame):
    """知识库视图主组件：检索框 + 列表。

    数据源懒加载：第一次 refresh 时才从 ``home / MEMORY.md`` 拉取。
    """

    KIND_TOKENS = {'pitfall': 'error', 'lesson': 'warn', 'note': 'subtext'}

    def __init__(self, master, *, home, **kw):
        super().__init__(master, **kw)
        self.home = home
        self._store = None
        self._build()

    def _build(self):
        self.configure(padding=(8, 6))
        # 搜索框
        bar = ttk.Frame(self)
        bar.pack(fill='x')
        self._query = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self._query)
        entry.pack(side='left', fill='x', expand=True)
        entry.bind('<Return>', lambda _e: self.refresh(self._query.get()))
        ttk.Button(bar, text='检索', command=lambda: self.refresh(self._query.get())).pack(
            side='left', padx=(6, 0))
        # 列表
        self._tree = ttk.Treeview(self, show='tree headings',
                                  columns=('kind', 'source'),
                                  height=10)
        self._tree.heading('#0', text='条目')
        self._tree.heading('kind', text='类型')
        self._tree.heading('source', text='来源')
        self._tree.column('#0', width=220)
        self._tree.column('kind', width=80)
        self._tree.column('source', width=80)
        for kind, token in self.KIND_TOKENS.items():
            self._tree.tag_configure(f'kind-{kind}', foreground=C[token])
        scroll = ttk.Scrollbar(self, command=self._tree.yview)
        scroll.pack(side='right', fill='y')
        self._tree.configure(yscrollcommand=scroll.set)
        self._tree.pack(fill='both', expand=True, pady=(6, 0))

    def _ensure_store(self):
        if self._store is not None:
            return self._store
        try:
            from forge.knowledge import KnowledgeBase
            from pathlib import Path
            self._store = KnowledgeBase(Path(self.home) / 'MEMORY.md')
        except Exception:
            self._store = False  # 标记为不可用
        return self._store

    def refresh(self, query=''):
        store = self._ensure_store()
        if not store:
            return
        try:
            hits = store.search(query or '') if query else store.entries(limit=200)
        except Exception:
            return
        self._tree.delete(*self._tree.get_children())
        for entry, score in hits:
            kind = getattr(entry, 'kind', 'note')
            source = getattr(entry, 'source', '')
            text = (getattr(entry, 'text', '') or '')[:120]
            self._tree.insert('', 'end', iid=getattr(entry, 'id', None) or text,
                              text=text, values=(kind, source),
                              tags=(f'kind-{kind}',))

    def get_selection(self):
        selection = self._tree.selection()
        if not selection:
            return None
        return self._tree.item(selection[0], 'text')
