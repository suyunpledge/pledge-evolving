"""Work Mode 命令面板（Ctrl+K）：命令、会话统一入口。Runtime: tkinter only.

Ember 设计稿 03：顶部输入行、按组列出结果（图标块 + 名称 + 说明 + 快捷键），
↑↓ 选择、Enter 执行、Esc / 点外部关闭。前缀限定范围：``>`` 只搜命令，
``#`` 只搜会话。条目由宿主每次打开时现取，面板本身不认识任何业务对象。
"""
from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from typing import Callable

import i18n
from i18n import tr
import gui_theme as theme
from gui_theme import C, FONT_CAPTION, FONT_MONO_XS, FONT_SMALL, FONT_UI
from ui_icons import IconCanvas

GROUP_ORDER = ("命令", "会话")
SCOPES = {">": "命令", "#": "会话"}
MAX_ROWS = 9


@dataclass(frozen=True)
class PaletteItem:
    label: str
    group: str
    run: Callable[[], object]
    icon: str = "chevron_right"
    hint: str = ""
    shortcut: str = ""
    keywords: str = ""


def score(query: str, text: str) -> int | None:
    """子序列匹配打分：连续命中与词首命中加分；不匹配返回 None。"""
    if not query:
        return 0
    text_l, query_l = text.lower(), query.lower()
    at = text_l.find(query_l)
    if at >= 0:
        return 1000 - at * 3 - len(text_l)
    position, total, streak = 0, 0, 0
    for char in query_l:
        found = text_l.find(char, position)
        if found < 0:
            return None
        streak = streak + 1 if found == position else 0
        total += 5 + streak * 4 - (found - position)
        position = found + 1
    return total - len(text_l)


def rank(items, query):
    scope = SCOPES.get(query[:1]) if query else None
    if scope:
        query = query[1:].strip()
    scored = []
    for order, item in enumerate(items):
        if scope and item.group != scope:
            continue
        value = score(query, f"{item.label} {item.keywords}")
        if value is not None:
            scored.append((-value if query else order, order, item))
    scored.sort(key=lambda entry: (GROUP_ORDER.index(entry[2].group)
                                   if entry[2].group in GROUP_ORDER else 9, entry[0], entry[1]))
    return [item for _value, _order, item in scored], scope, query


class CommandPalette(tk.Toplevel):
    def __init__(self, master, items, *, on_close=None):
        super().__init__(master, bg=C["border_hi"])
        self.withdraw()
        self.overrideredirect(True)
        try:
            self.attributes("-topmost", True)
        except tk.TclError:
            pass
        self._items = list(items)
        self._on_close = on_close
        self._results: list[PaletteItem] = []
        self._rows: list[tuple] = []
        self._selected = 0
        self._closed = False
        px = lambda value: theme.ui_px(master, value)  # noqa: E731
        self._px = px
        shell = tk.Frame(self, bg=C["surface"], padx=0, pady=0)
        shell.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
        head = tk.Frame(shell, bg=C["surface"], padx=px(16), pady=px(12))
        head.pack(fill=tk.X)
        IconCanvas(head, "command", size=px(18), bg=C["surface"], fg=C["ember"]).pack(side=tk.LEFT)
        self._scope = i18n.Label(head, text="", bg=C["accent_soft"], fg=C["accent_text"],
                                 font=FONT_CAPTION, padx=px(7))
        self.query = tk.StringVar(self)
        self.entry = tk.Entry(head, textvariable=self.query, bg=C["surface"], fg=C["text"],
                              insertbackground=C["ember"], relief=tk.FLAT, bd=0,
                              highlightthickness=0, font=(theme.UI_FAMILY, 12))
        self.entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(px(10), px(8)))
        i18n.Label(head, text="Esc", bg=C["surface2"], fg=C["subtext"], font=FONT_MONO_XS,
                   padx=px(5), highlightthickness=1,
                   highlightbackground=C["border_hi"]).pack(side=tk.RIGHT)
        tk.Frame(shell, bg=C["line"], height=1).pack(fill=tk.X)
        self._list = tk.Frame(shell, bg=C["surface"], padx=px(8), pady=px(6))
        self._list.pack(fill=tk.BOTH, expand=True)
        self._empty = i18n.Label(self._list, text=tr("没有匹配的命令或会话"), bg=C["surface"],
                                 fg=C["faint"], font=FONT_SMALL, pady=px(16))
        foot = tk.Frame(shell, bg=C["sidebar"], padx=px(16), pady=px(8))
        tk.Frame(shell, bg=C["line"], height=1).pack(fill=tk.X)
        foot.pack(fill=tk.X)
        for keys, text in (("↑ ↓", tr("选择")), ("Enter", tr("执行")), ("> #", tr("限定命令 / 会话"))):
            i18n.Label(foot, text=keys, bg=C["surface2"], fg=C["subtext"], font=FONT_MONO_XS,
                       padx=px(4), highlightthickness=1,
                       highlightbackground=C["border_hi"]).pack(side=tk.LEFT)
            i18n.Label(foot, text=text, bg=C["sidebar"], fg=C["faint"],
                       font=FONT_CAPTION).pack(side=tk.LEFT, padx=(px(5), px(14)))
        self.query.trace_add("write", lambda *_: self.refresh())
        for sequence, handler in (("<Escape>", self.close), ("<Return>", self._run_selected),
                                  ("<KP_Enter>", self._run_selected),
                                  ("<Down>", lambda _e: self._move(1)),
                                  ("<Up>", lambda _e: self._move(-1)),
                                  ("<Tab>", self._cycle_scope)):
            self.entry.bind(sequence, handler)
        self.bind("<FocusOut>", self._focus_out, add="+")
        self.refresh()
        self._place()
        self.deiconify()
        self.lift()
        self.entry.focus_force()

    # ── 布局 ──
    def _place(self):
        root = self.master.winfo_toplevel()
        root.update_idletasks()
        width = min(self._px(640), max(self._px(360), root.winfo_width() - self._px(80)))
        self.update_idletasks()
        height = self.winfo_reqheight()
        x = root.winfo_rootx() + (root.winfo_width() - width) // 2
        y = root.winfo_rooty() + self._px(72)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _focus_out(self, _event=None):
        # 焦点离开整个面板（点到窗口别处）才关闭；面板内部切换焦点不算。
        self.after(60, self._close_if_unfocused)

    def _close_if_unfocused(self):
        if self._closed:
            return
        try:
            focus = self.focus_get()
        except (tk.TclError, KeyError):
            focus = None
        if focus is None or not str(focus).startswith(str(self)):
            self.close()

    # ── 数据 ──
    def refresh(self):
        self._results, scope, _query = rank(self._items, self.query.get().strip())
        self._results = self._results[:MAX_ROWS]
        if scope:
            self._scope.configure(text=tr(scope))
            if not self._scope.winfo_manager():
                self._scope.pack(side=tk.LEFT, padx=(self._px(10), 0), before=self.entry)
        else:
            self._scope.pack_forget()
        self._selected = 0
        self._build_rows()
        if not self._closed and self.winfo_viewable():
            self._place()

    def _build_rows(self):
        for child in self._list.winfo_children():
            if child is not self._empty:
                child.destroy()
        self._rows = []
        self._empty.pack_forget()
        if not self._results:
            self._empty.pack(fill=tk.X)
            return
        px = self._px
        group = None
        for index, item in enumerate(self._results):
            if item.group != group:
                group = item.group
                i18n.Label(self._list, text=tr(group), bg=C["surface"], fg=C["faint"],
                           font=FONT_CAPTION, anchor="w",
                           padx=px(10)).pack(fill=tk.X, pady=(px(6) if index else 0, px(2)))
            row = tk.Frame(self._list, bg=C["surface"], height=px(38), cursor="hand2")
            row.pack(fill=tk.X)
            row.pack_propagate(False)
            tile = tk.Canvas(row, width=px(26), height=px(26), bg=C["surface"],
                             highlightthickness=0, bd=0)
            tile.pack(side=tk.LEFT, padx=(px(8), px(10)))
            label = i18n.Label(row, text=item.label, bg=C["surface"], fg=C["body"], font=FONT_UI,
                               anchor="w")
            label.pack(side=tk.LEFT)
            hint = i18n.Label(row, text=item.hint, bg=C["surface"], fg=C["faint"],
                              font=FONT_CAPTION, anchor="w")
            hint.pack(side=tk.LEFT, padx=(px(8), 0), fill=tk.X, expand=True)
            key = None
            if item.shortcut:
                key = i18n.Label(row, text=item.shortcut, bg=C["surface2"], fg=C["subtext"],
                                 font=FONT_MONO_XS, padx=px(5), highlightthickness=1,
                                 highlightbackground=C["border_hi"])
                key.pack(side=tk.RIGHT, padx=(0, px(10)))
            parts = [row, tile, label, hint] + ([key] if key else [])
            for widget in parts:
                widget.bind("<Button-1>", lambda _e, i=index: self._run(i))
                widget.bind("<Enter>", lambda _e, i=index: self._select(i))
            self._rows.append((row, tile, label, hint, item))
        self._paint()

    def _paint(self):
        px = self._px
        for index, (row, tile, label, hint, item) in enumerate(self._rows):
            active = index == self._selected
            bg = C["raised"] if active else C["surface"]
            for widget in (row, tile, label, hint):
                widget.configure(bg=bg)
            label.configure(fg=C["text"] if active else C["body"])
            tile.delete("all")
            size = px(26)
            theme.round_rect(tile, 0, 0, size - 1, size - 1, px(7),
                             fill=C["accent_soft"] if active else C["surface2"], outline="")
            glyph = px(15)
            theme.draw_icon(tile, item.icon, x=(size - glyph) / 2, y=(size - glyph) / 2,
                            size=glyph, fg=C["ember"] if active else C["subtext"])

    # ── 交互 ──
    def _select(self, index):
        if 0 <= index < len(self._rows) and index != self._selected:
            self._selected = index
            self._paint()

    def _move(self, delta):
        if self._rows:
            self._selected = (self._selected + delta) % len(self._rows)
            self._paint()
        return "break"

    def _cycle_scope(self, _event=None):
        text = self.query.get()
        prefix = text[:1] if text[:1] in SCOPES else ""
        order = ["", ">", "#"]
        following = order[(order.index(prefix) + 1) % len(order)]
        self.query.set(following + text[len(prefix):].lstrip())
        self.entry.icursor(tk.END)
        return "break"

    def _run_selected(self, _event=None):
        self._run(self._selected)
        return "break"

    def _run(self, index):
        if not 0 <= index < len(self._results):
            return
        item = self._results[index]
        self.close()
        item.run()

    def close(self, _event=None):
        if self._closed:
            return "break"
        self._closed = True
        try:
            self.destroy()
        except tk.TclError:
            pass
        if self._on_close is not None:
            self._on_close()
        return "break"
