"""演化审批 Tab 内容（编辑器与布局域）。

把 :class:`forge.evolution.EvolutionEngine` 的 pending 候选拉成可注
入到 ``ExplorerPanel._evolution_tab`` 的视图组件。

候选操作：approve / reject / quarantine —— 与 CLI ``forge evolution`` 的
三个动作一致；这里只是 UI 包装。

优雅降级：EvolutionEngine 不可用时退到 ``EvolutionPlaceholder``。
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .buttons import dev_button
from gui_theme import C, FONT_CAPTION, FONT_SECTION, FONT_UI


class EvolutionPlaceholder(ttk.Frame):
    def __init__(self, master, **kw):
        super().__init__(master, **kw)
        self.configure(padding=16)
        ttk.Label(self, text='演化视图', font=FONT_SECTION,
                  foreground=C['text']).pack(anchor='w')
        ttk.Label(self,
                  text='EvolutionEngine 不可用，'
                       '请检查 forge.evolution 导入路径。',
                  foreground=C['subtext'], wraplength=320, justify='left').pack(
            anchor='w', pady=(6, 0))


class EvolutionView(ttk.Frame):
    """演化候选列表 + 三按钮操作。"""

    RISK_TOKENS = {'low': 'ok', 'medium': 'warn', 'high': 'error'}

    def __init__(self, master, *, home, **kw):
        super().__init__(master, **kw)
        self.home = home
        self._engine = None
        self._build()

    def _build(self):
        self.configure(padding=(8, 6))
        bar = ttk.Frame(self)
        bar.pack(fill='x')
        dev_button(bar, '刷新', self.refresh, kind='ghost').pack(side='left')
        dev_button(bar, '批准', lambda: self._act('approve'), kind='primary').pack(
            side='left', padx=(6, 0))
        dev_button(bar, '拒绝', lambda: self._act('reject'), kind='danger').pack(
            side='left', padx=(6, 0))
        dev_button(bar, '隔离', lambda: self._act('quarantine'), kind='ghost').pack(
            side='left', padx=(6, 0))
        self._tree = ttk.Treeview(self, show='tree headings',
                                  columns=('kind', 'risk', 'target'),
                                  height=10)
        self._tree.heading('#0', text='候选')
        self._tree.heading('kind', text='类型')
        self._tree.heading('risk', text='风险')
        self._tree.heading('target', text='目标')
        self._tree.column('#0', width=200)
        self._tree.column('kind', width=80)
        self._tree.column('risk', width=80)
        self._tree.column('target', width=160)
        for risk, token in self.RISK_TOKENS.items():
            self._tree.tag_configure(f'risk-{risk}', foreground=C[token])
        scroll = ttk.Scrollbar(self, command=self._tree.yview)
        scroll.pack(side='right', fill='y')
        self._tree.configure(yscrollcommand=scroll.set)
        self._tree.pack(fill='both', expand=True, pady=(6, 0))

    def _ensure_engine(self):
        if self._engine is not None:
            return self._engine
        try:
            from forge.evolution import EvolutionEngine
            from pathlib import Path
            home = Path(self.home)
            self._engine = EvolutionEngine(home,
                                           memory=None,
                                           workspace=home / 'workspaces' / 'default')
        except Exception:
            self._engine = False
        return self._engine

    def refresh(self):
        engine = self._ensure_engine()
        if not engine:
            return
        try:
            candidates = list(getattr(engine, 'pending', lambda: [])() or [])
        except Exception:
            return
        self._tree.delete(*self._tree.get_children())
        for c in candidates:
            self._tree.insert('', 'end', iid=getattr(c, 'id', None) or getattr(c, 'target', ''),
                              text=(getattr(c, 'content', '') or '')[:80],
                              values=(getattr(c, 'kind', ''),
                                      getattr(c, 'risk', ''),
                                      getattr(c, 'target', '')),
                              tags=(f"risk-{getattr(c, 'risk', 'medium')}",))

    def _act(self, action):
        engine = self._ensure_engine()
        if not engine:
            return
        selection = self._tree.selection()
        if not selection:
            return
        cid = selection[0]
        try:
            if action == 'approve':
                engine.approve(cid)
            elif action == 'reject':
                engine.reject(cid, reason='UI 拒绝')
            elif action == 'quarantine':
                engine.quarantine(cid, reason='UI 隔离')
        except Exception:
            pass
        self.refresh()
