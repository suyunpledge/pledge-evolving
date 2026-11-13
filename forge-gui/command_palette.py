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
RESERVED_ROWS = 3      # 混合视图里留给会话的行数


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


def rank(items, query, *, text_of=None):
    """按组、按匹配度排序。``text_of(item)`` 给出参与匹配的文字——面板传入
    当前语言下屏幕上显示的标签，否则只会匹配到中文源串。"""
    scope = SCOPES.get(query[:1]) if query else None
    if scope:
        query = query[1:].strip()
    scored = []
    for order, item in enumerate(items):
        if scope and item.group != scope:
            continue
        value = score(query, text_of(item) if text_of else f"{item.label} {item.keywords}")
        if value is not None:
            scored.append((-value if query else order, order, item))
    scored.sort(key=lambda entry: (GROUP_ORDER.index(entry[2].group)
                                   if entry[2].group in GROUP_ORDER else 9, entry[0], entry[1]))
    return [item for _value, _order, item in scored], scope, query


def balance(results, scope, rows=MAX_ROWS):
    """未限定范围时的首屏：给会话留出位置，不让命令把 9 行全占掉。

    返回 ``(要显示的条目, {组: 该组总数})``。限定范围（``>`` / ``#``）或只有
    一组有结果时原样给出全部——那时列表可以滚动；混合视图只放每组最靠前的
    几条，组标题上的「显示数 / 总数」提示还有更多。
    """
    totals = {}
    for item in results:
        totals[item.group] = totals.get(item.group, 0) + 1
    if scope or len(totals) < 2 or len(results) <= rows:
        return list(results), totals
    reserved = GROUP_ORDER[-1]
    tail = [item for item in results if item.group == reserved]
    front = [item for item in results if item.group != reserved]
    keep_tail = min(len(tail), RESERVED_ROWS)
    keep_front = min(len(front), rows - keep_tail)
    keep_tail = min(len(tail), rows - keep_front)
    return front[:keep_front] + tail[:keep_tail], totals


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
        self._totals: dict[str, int] = {}
        self._selected = 0      # 在 _results 里的下标
        self._offset = 0        # 可见窗口的第一行
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
        # 页脚先于列表占位：列表内容偶尔比窗口高时，被裁的是最后一行而不是页脚。
        foot = tk.Frame(shell, bg=C["sidebar"], padx=px(16), pady=px(8))
        foot.pack(side=tk.BOTTOM, fill=tk.X)
        tk.Frame(shell, bg=C["line"], height=1).pack(side=tk.BOTTOM, fill=tk.X)
        self._list = tk.Frame(shell, bg=C["surface"], padx=px(8), pady=px(6))
        self._list.pack(fill=tk.BOTH, expand=True)
        self._empty = i18n.Label(self._list, text=tr("没有匹配的命令或会话"), bg=C["surface"],
                                 fg=C["faint"], font=FONT_SMALL, pady=px(16))
        for keys, text in (("↑ ↓", tr("选择")), ("Enter", tr("执行")), ("> #", tr("限定命令 / 会话"))):
            i18n.Label(foot, text=keys, bg=C["surface2"], fg=C["subtext"], font=FONT_MONO_XS,
                       padx=px(4), highlightthickness=1,
                       highlightbackground=C["border_hi"]).pack(side=tk.LEFT)
            i18n.Label(foot, text=text, bg=C["sidebar"], fg=C["faint"],
                       font=FONT_CAPTION).pack(side=tk.LEFT, padx=(px(5), px(14)))
        # 列表可滚动时在页脚右侧给出「第几条 / 共几条」。
        self._position = tk.Label(foot, text="", bg=C["sidebar"], fg=C["faint"], font=FONT_MONO_XS)
        self._position.pack(side=tk.RIGHT)
        for widget in (self, self.entry, self._list):
            widget.bind("<MouseWheel>", self._wheel, add="+")
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
    def _match_text(self, item):
        # 匹配屏幕上显示的译文；中文源串也留着，任何语言下输入中文都能搜到。
        shown = str(i18n.resolve(item.label, self))
        source = str(item.label)
        return " ".join(part for part in (shown, source if source != shown else "", item.keywords) if part)

    def refresh(self):
        ranked, scope, _query = rank(self._items, self.query.get().strip(), text_of=self._match_text)
        self._results, self._totals = balance(ranked, scope)
        self._offset = 0
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
        shown = {}
        for item in self._results:
            shown[item.group] = shown.get(item.group, 0) + 1
        visible = self._results[self._offset:self._offset + MAX_ROWS]
        for position, item in enumerate(visible):
            index = self._offset + position
            if item.group != group:
                group = item.group
                head = tk.Frame(self._list, bg=C["surface"])
                head.pack(fill=tk.X, pady=(px(6) if position else 0, px(2)))
                i18n.Label(head, text=tr(group), bg=C["surface"], fg=C["faint"],
                           font=FONT_CAPTION, anchor="w", padx=px(10)).pack(side=tk.LEFT)
                total = self._totals.get(group, 0)
                if shown.get(group, 0) < total:
                    # 混合视图只放每组最靠前的几条：标出还有多少没显示。
                    tk.Label(head, text=f"{shown[group]} / {total}", bg=C["surface"], fg=C["faint"],
                             font=FONT_MONO_XS, padx=px(10)).pack(side=tk.RIGHT)
                for widget in (head, *head.winfo_children()):
                    widget.bind("<MouseWheel>", self._wheel, add="+")
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
                # 用 Motion 而不是 Enter：列表在静止的鼠标指针下重建时会收到 Enter，
                # 那样边打字边被指针下的那一行抢走选中，回车就执行错了条目。
                widget.bind("<Motion>", lambda _e, i=index: self._select(i))
                widget.bind("<MouseWheel>", self._wheel, add="+")
            self._rows.append((row, tile, label, hint, item, index))
        self._paint()

    def _paint(self):
        px = self._px
        total = len(self._results)
        self._position.configure(text=f"{self._selected + 1} / {total}" if total > MAX_ROWS else "")
        for row, tile, label, hint, item, index in self._rows:
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
        if 0 <= index < len(self._results) and index != self._selected:
            self._selected = index
            self._paint()

    def _move(self, delta):
        if not self._results:
            return "break"
        self._selected = (self._selected + delta) % len(self._results)
        self._reveal()
        return "break"

    def _reveal(self):
        """让选中行落在可见窗口里；窗口动了才重建行，否则只重绘。"""
        offset = min(max(self._offset, self._selected - MAX_ROWS + 1), self._selected)
        if offset != self._offset:
            self._offset = offset
            self._build_rows()
        else:
            self._paint()

    def _wheel(self, event):
        limit = max(0, len(self._results) - MAX_ROWS)
        offset = min(limit, max(0, self._offset + (-1 if event.delta > 0 else 1)))
        if offset != self._offset:
            self._offset = offset
            # 选中行跟着窗口走，保证回车执行的一定是看得见的那一行。
            self._selected = min(max(self._selected, offset), offset + MAX_ROWS - 1)
            self._build_rows()
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
