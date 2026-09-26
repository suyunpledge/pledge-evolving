"""对话区渲染组件（深色主题）。

给 `forge_gui_v2.py` 的「对话」和「任务」两个视图复用：

    MessageArea      可滚动消息列表（空态、跟随滚动、追加消息）
    UserMessage      用户消息（圆形头像 + 气泡）
    AgentMessage     Forge 消息（圆角方头像 + 角色徽章 + 丰富正文）
    ToolCard         「调用工具 (n)」折叠卡片（工具名 / 说明 / 耗时 / 勾）
    StepList         编号执行步骤（编号圆圈 + 标题 + 说明 + 耗时 + 绿勾）
    CompletionBlock  「已完成」收尾块（正文 + 特性清单 + 动作按钮）
    InputCard        底部输入卡（占位提示 + 工具条 + 模型胶囊 + 圆形发送）

正文渲染：`render_blocks()` 做轻量 Markdown（段落 / 标题 / 有序无序列表 /
待办勾选 / 引用 / 代码块 / 行内 **加粗** 与 `代码`），全部落到 tkinter
原生控件上，零第三方依赖。
"""
from __future__ import annotations

import re
import time
import tkinter as tk

from gui_theme import (
    C, FONT_CAPTION, FONT_MICRO, FONT_MONO, FONT_MONO_SM, FONT_SECTION,
    FONT_SMALL, FONT_TITLE, FONT_UI, FONT_UI_BOLD, R_CARD, R_MD, R_PILL,
    RoundedCard, attach_tooltip, avatar, badge, circle_button, circle_button_state,
    dot, glyph_button, highlight_python, round_rect, rounded_label,
    setup_code_tags, style_scrollbar,
)

MAX_BUBBLE_WIDTH = 620


# ─── 可滚动区域 ────────────────────────────────────────────


class ScrollArea(tk.Frame):
    """Canvas + 内嵌 Frame 的滚动容器（深色细滚动条）。"""

    def __init__(self, parent, *, bg=None, padx=0, pady=0):
        base = bg or C["chat"]
        super().__init__(parent, bg=base, highlightthickness=0, bd=0)
        self._bg = base
        self.canvas = tk.Canvas(self, bg=base, highlightthickness=0, bd=0,
                                highlightcolor=base)
        self.vbar = tk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview,
                                 bg=C["surface2"], troughcolor=base, bd=0,
                                 highlightthickness=0, width=8,
                                 activebackground=C["scroll"], relief=tk.FLAT)
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.vbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.inner = tk.Frame(self.canvas, bg=base, padx=padx, pady=pady)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._sync_scrollregion)
        self.canvas.bind("<Configure>", self._sync_width)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.inner.bind("<MouseWheel>", self._on_wheel)
        self._bar_visible = True
        self.bind("<Enter>", self._grab_wheel)
        self.bind("<Leave>", self._release_wheel)

    # -- 滚动 --
    def _on_scroll(self, first, last):
        if float(last) - float(first) >= 0.999:
            if self._bar_visible:
                self.vbar.pack_forget()
                self._bar_visible = False
        elif not self._bar_visible:
            self.vbar.pack(side=tk.RIGHT, fill=tk.Y)
            self._bar_visible = True
        self.vbar.set(first, last)

    def _sync_scrollregion(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _sync_width(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def _on_wheel(self, event):
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _grab_wheel(self, _e=None):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _release_wheel(self, _e=None):
        self.canvas.unbind_all("<MouseWheel>")

    # -- 状态 --
    def at_bottom(self) -> bool:
        try:
            return self.canvas.yview()[1] >= 0.98
        except tk.TclError:
            return True

    def scroll_to_end(self):
        self.update_idletasks()
        self.canvas.yview_moveto(1.0)

    def near_bottom(self) -> bool:
        try:
            return self.canvas.yview()[1] >= 0.90
        except tk.TclError:
            return True


# ─── 行内富文本 ────────────────────────────────────────────


class InlineText(tk.Text):
    """自动高度的只读 Text：支持 **加粗** 与 `行内代码`。"""

    def __init__(self, parent, *, bg=None, fg=None, font=None, width=None,
                 wrap=tk.WORD):
        base = bg or C["chat"]
        super().__init__(parent, wrap=wrap, bg=base, fg=fg or C["body"],
                         font=font or FONT_UI, relief=tk.FLAT, bd=0,
                         highlightthickness=0, padx=0, pady=0, height=1,
                         cursor="arrow", insertwidth=0, spacing1=1, spacing3=1,
                         selectbackground=C["accent_soft"])
        self.tag_configure("b", font=FONT_UI_BOLD, foreground=C["text"])
        self.tag_configure("code", font=FONT_MONO_SM, background=C["code_bg"],
                           foreground=C["code_str"])
        self.tag_configure("muted", foreground=C["subtext"])
        self._base_bg = base
        if width:
            self.configure(width=width)
        self.bind("<Configure>", self._fit_height)
        self.configure(state=tk.DISABLED)
        self._last_h = 1

    def _fit_height(self, _event=None):
        self.after_idle(self._recompute)

    def _recompute(self):
        try:
            if not self.winfo_exists():
                return
            self.configure(state=tk.NORMAL)
            res = self.count("1.0", "end", "displaylines")
            n = res[0] if isinstance(res, (tuple, list)) else res
            n = max(1, int(n or 1))
            if n != self._last_h:
                self._last_h = n
                self.configure(height=n)
            self.configure(state=tk.DISABLED)
        except tk.TclError:
            pass

    def set_segments(self, segs):
        """segs: [(text, tag|None), ...]"""
        self.configure(state=tk.NORMAL)
        self.delete("1.0", tk.END)
        for text, tag in segs:
            self.insert(tk.END, text, tag or ())
        self.configure(state=tk.DISABLED)
        self._last_h = -1
        self.after_idle(self._recompute)

    def set_text(self, text, tag=None):
        self.set_segments([(text, tag)])

    def append_text(self, text, tag=None):
        self.configure(state=tk.NORMAL)
        self.insert(tk.END, text, tag or ())
        self.configure(state=tk.DISABLED)
        self._last_h = -1
        self.after_idle(self._recompute)


_INLINE_RE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`)")


def inline_segments(text: str):
    """把一行文字切成 (文本, tag) 段：加粗 / 行内代码 / 普通。"""
    segs = []
    for piece in _INLINE_RE.split(text):
        if not piece:
            continue
        if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
            segs.append((piece[2:-2], "b"))
        elif piece.startswith("`") and piece.endswith("`") and len(piece) > 2:
            segs.append((piece[1:-1], "code"))
        else:
            segs.append((piece, None))
    return segs or [(text, None)]


_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
_TODO_RE = re.compile(r"^\s*[-*+]\s+\[([ xX])\]\s*(.*)$")
_OL_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")


def parse_blocks(text: str):
    """轻量 Markdown → 块列表（不追求完整语法，够读就行）。"""
    blocks = []
    lines = (text or "").split("\n")
    i = 0
    para: list[str] = []

    def flush():
        if para:
            blocks.append({"type": "p", "text": " ".join(para).strip()})
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            flush()
            lang = line.strip().strip("`").strip()
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            blocks.append({"type": "code", "lang": lang, "text": "\n".join(buf)})
            continue
        if not line.strip():
            flush()
            i += 1
            continue
        m = _HEAD_RE.match(line)
        if m:
            flush()
            blocks.append({"type": "h", "level": len(m.group(1)), "text": m.group(2).strip()})
            i += 1
            continue
        m = _TODO_RE.match(line)
        if m:
            flush()
            blocks.append({"type": "todo", "done": m.group(1).lower() == "x",
                           "text": m.group(2).strip()})
            i += 1
            continue
        m = _BULLET_RE.match(line)
        if m:
            flush()
            blocks.append({"type": "li", "text": m.group(1).strip()})
            i += 1
            continue
        m = _OL_RE.match(line)
        if m:
            flush()
            blocks.append({"type": "oli", "num": m.group(1), "text": m.group(2).strip()})
            i += 1
            continue
        m = _QUOTE_RE.match(line)
        if m:
            flush()
            blocks.append({"type": "quote", "text": m.group(1).strip()})
            i += 1
            continue
        if line.strip() in ("---", "***", "___"):
            flush()
            blocks.append({"type": "hr"})
            i += 1
            continue
        para.append(line.strip())
        i += 1
    flush()
    return blocks


def render_blocks(parent, text, *, bg=None, max_width=None,
                  mono_for_code=True) -> tk.Frame:
    """把 markdown 轻量渲染成一叠控件，返回承载它们的 Frame。"""
    base = bg or C["chat"]
    host = tk.Frame(parent, bg=base)
    wrap = max_width or MAX_BUBBLE_WIDTH
    for block in parse_blocks(text):
        kind = block["type"]
        if kind == "h":
            f = FONT_TITLE if block["level"] <= 2 else FONT_SECTION
            tk.Label(host, text=block["text"], bg=base, fg=C["text"], font=f,
                     anchor="w", justify=tk.LEFT, wraplength=wrap).pack(
                fill=tk.X, pady=(8, 3))
        elif kind == "p":
            t = InlineText(host, bg=base)
            t.set_segments(inline_segments(block["text"]))
            t.pack(fill=tk.X, pady=2)
        elif kind == "li":
            row = tk.Frame(host, bg=base)
            row.pack(fill=tk.X, pady=1)
            tk.Label(row, text="•", bg=base, fg=C["accent2"], font=FONT_UI_BOLD,
                     width=2, anchor="nw").pack(side=tk.LEFT)
            t = InlineText(row, bg=base)
            t.set_segments(inline_segments(block["text"]))
            t.pack(side=tk.LEFT, fill=tk.X, expand=True)
        elif kind == "oli":
            row = tk.Frame(host, bg=base)
            row.pack(fill=tk.X, pady=2)
            num = tk.Canvas(row, width=18, height=18, bg=base,
                            highlightthickness=0, bd=0)
            num.create_oval(0, 0, 17, 17, fill=C["sel"], outline=C["sel_border"])
            num.create_text(9, 9, text=block["num"], fill=C["subtext"],
                            font=FONT_MICRO)
            num.pack(side=tk.LEFT, anchor="n", padx=(0, 8))
            t = InlineText(row, bg=base)
            t.set_segments(inline_segments(block["text"]))
            t.pack(side=tk.LEFT, fill=tk.X, expand=True)
        elif kind == "todo":
            row = tk.Frame(host, bg=base)
            row.pack(fill=tk.X, pady=1)
            mark = "✓" if block["done"] else "○"
            color = C["ok"] if block["done"] else C["muted"]
            tk.Label(row, text=mark, bg=base, fg=color, font=FONT_UI_BOLD,
                     width=2).pack(side=tk.LEFT)
            t = InlineText(row, bg=base)
            t.set_segments(inline_segments(block["text"]))
            t.pack(side=tk.LEFT, fill=tk.X, expand=True)
        elif kind == "quote":
            row = tk.Frame(host, bg=base)
            row.pack(fill=tk.X, pady=2)
            tk.Frame(row, bg=C["accent_border"], width=3).pack(side=tk.LEFT, fill=tk.Y)
            t = InlineText(row, bg=base)
            t.set_segments(inline_segments(block["text"]))
            t.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))
        elif kind == "code":
            card = RoundedCard(host, radius=R_MD, fill=C["code_bg"],
                               outline=C["border_hi"], padx=10, pady=8, bg=base)
            card.pack(fill=tk.X, pady=6)
            tx = tk.Text(card.content, bg=C["code_bg"], fg=C["code_plain"],
                         font=FONT_MONO_SM, relief=tk.FLAT, bd=0,
                         highlightthickness=0, wrap=tk.NONE, height=1)
            tx.insert("1.0", block["text"])
            setup_code_tags(tx, font=FONT_MONO_SM)
            if (block.get("lang") or "") in ("", "py", "python") or (block.get("lang") or "").startswith("py"):
                try:
                    highlight_python(tx)
                except Exception:
                    pass
            lines = max(1, len(block["text"].split("\n")))
            tx.configure(height=min(lines, 24), state=tk.DISABLED)
            tx.pack(fill=tk.X)
        elif kind == "hr":
            tk.Frame(host, bg=C["border"], height=1).pack(fill=tk.X, pady=6)
    return host


# ─── 消息块 ────────────────────────────────────────────────


class StepList(tk.Frame):
    """编号执行步骤（参考稿「1 分析现有 GUI 结构 / 2m 14s ✓」）。"""

    def __init__(self, parent, items, *, bg=None):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        for item in items:
            self.add(item)

    def add(self, item: dict):
        base = self["bg"]
        row = tk.Frame(self, bg=base)
        row.pack(fill=tk.X, pady=3)
        idx = tk.Canvas(row, width=18, height=18, bg=base, highlightthickness=0, bd=0)
        idx.create_oval(0, 0, 17, 17, fill=C["sel"], outline=C["sel_border"])
        idx.create_text(9, 9, text=str(item.get("index", "")), fill=C["subtext"],
                        font=FONT_MICRO)
        idx.pack(side=tk.LEFT, anchor="n", padx=(0, 10))
        text_box = tk.Frame(row, bg=base)
        text_box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(text_box, text=item.get("title", ""), bg=base, fg=C["text"],
                 font=FONT_UI_BOLD, anchor="w").pack(fill=tk.X)
        desc = item.get("desc")
        if desc:
            tk.Label(text_box, text=desc, bg=base, fg=C["ter"], font=FONT_SMALL,
                     anchor="w", justify=tk.LEFT, wraplength=460).pack(fill=tk.X)
        right = tk.Frame(row, bg=base)
        right.pack(side=tk.RIGHT, anchor="n")
        if item.get("elapsed"):
            tk.Label(right, text=item["elapsed"], bg=base, fg=C["muted"],
                     font=FONT_MONO_SM).pack(side=tk.LEFT, padx=(0, 8))
        if item.get("done", True):
            chk = tk.Canvas(right, width=14, height=14, bg=base,
                            highlightthickness=0, bd=0)
            chk.create_oval(0, 0, 13, 13, fill=C["ok"], outline="")
            chk.create_text(7, 7, text="✓", fill="#0B0B10", font=FONT_MICRO)
            chk.pack(side=tk.LEFT)


class ToolCard(tk.Frame):
    """「调用工具 (n)」折叠卡片。"""

    def __init__(self, parent, *, title="调用工具", rows=None, bg=None, expanded=True):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        self._expanded = expanded
        head = tk.Frame(self, bg=base)
        head.pack(fill=tk.X, pady=(6, 2))
        self._arrow = tk.Label(head, text="⌃" if expanded else "⌄", bg=base,
                               fg=C["ter"], font=FONT_SMALL, cursor="hand2")
        self._arrow.pack(side=tk.LEFT, padx=(0, 6))
        self._title = tk.Label(head, text=f"{title} ({len(rows or [])})", bg=base,
                               fg=C["text"], font=FONT_UI_BOLD)
        self._title.pack(side=tk.LEFT)
        for w in (self._arrow, self._title, head):
            w.bind("<Button-1>", lambda _e: self.toggle())
        card = RoundedCard(self, radius=R_CARD, fill=C["surface2"],
                           outline=C["border_hi"], padx=10, pady=8, bg=base)
        card.pack(fill=tk.X, pady=(2, 4))
        self._card = card
        self.rows = tk.Frame(card.content, bg=C["surface2"])
        self.rows.pack(fill=tk.X)
        for row in rows or []:
            self.add_row(row)

    def toggle(self):
        self._expanded = not self._expanded
        self._arrow.configure(text="⌃" if self._expanded else "⌄")
        if self._expanded:
            self._card.pack(fill=tk.X, pady=(2, 4))
        else:
            self._card.pack_forget()

    def add_row(self, row: dict):
        bgc = C["surface2"]
        wrap = tk.Frame(self.rows, bg=bgc)
        wrap.pack(fill=tk.X, pady=1)
        tk.Label(wrap, text="✓" if row.get("ok", True) else "✕", bg=bgc,
                 fg=C["ok"] if row.get("ok", True) else C["error"],
                 font=FONT_SMALL, width=2).pack(side=tk.LEFT)
        tk.Label(wrap, text=row.get("name", ""), bg=bgc, fg=C["accent2"],
                 font=FONT_MONO_SM).pack(side=tk.LEFT)
        desc = row.get("desc")
        if desc:
            tk.Label(wrap, text=desc, bg=bgc, fg=C["ter"], font=FONT_SMALL,
                     anchor="w", justify=tk.LEFT).pack(side=tk.LEFT, padx=(10, 6))
        if row.get("elapsed"):
            tk.Label(wrap, text=row["elapsed"], bg=bgc, fg=C["muted"],
                     font=FONT_MONO_SM).pack(side=tk.RIGHT)
        self._title.configure(text=f"调用工具 ({len(self.rows.winfo_children())})")


class ActionRow(tk.Frame):
    """消息底部动作按钮组（查看修改的文件 / 打开工作区 / 预览效果 …）。"""

    def __init__(self, parent, actions, *, bg=None):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        for action in actions:
            kind = action.get("kind", "ghost")
            btn = tk.Button(self, text=action.get("label", ""),
                            command=action.get("command"),
                            bg=C["accent"] if kind == "primary" else C["chat"],
                            fg="#FFFFFF" if kind == "primary" else C["body"],
                            activebackground=C["accent_hover"] if kind == "primary" else C["hover"],
                            activeforeground="#FFFFFF" if kind == "primary" else C["text"],
                            font=FONT_SMALL, relief=tk.FLAT, bd=0, padx=12, pady=5,
                            cursor="hand2", highlightthickness=1,
                            highlightbackground=C["accent"] if kind == "primary" else C["border_hi"])
            btn.pack(side=tk.LEFT, padx=(0, 8))


# ─── 消息 ──────────────────────────────────────────────────


class UserMessage(tk.Frame):
    def __init__(self, parent, text, *, bg=None, ts=None, name="你"):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        head = tk.Frame(self, bg=base)
        head.pack(fill=tk.X)
        avatar(head, size=28, glyph="你", fill="#2A2A38", shape="circle",
               bg=base).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(head, text=name, bg=base, fg=C["text"], font=FONT_UI_BOLD).pack(side=tk.LEFT)
        tk.Label(head, text=ts or time.strftime("%H:%M"), bg=base, fg=C["muted"],
                 font=FONT_CAPTION).pack(side=tk.LEFT, padx=(8, 0))

        card = RoundedCard(self, radius=R_CARD, fill=C["surface"],
                           outline=C["border_hi"], padx=14, pady=10, bg=base)
        card.pack(anchor="w", pady=(6, 0))
        label = tk.Label(card.content, text=text, bg=C["surface"], fg=C["body"],
                         font=FONT_UI, justify=tk.LEFT, anchor="w",
                         wraplength=MAX_BUBBLE_WIDTH - 40)
        label.pack(fill=tk.X)


class AgentMessage(tk.Frame):
    """Forge 的回复：头像 + 名字 + 角色徽章 + 正文（可流式） + 富块。"""

    def __init__(self, parent, *, bg=None, name="Forge", role=None, ts=None,
                 glyph="F", subtitle=None):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        self._bg = base
        self._max_width = MAX_BUBBLE_WIDTH
        head = tk.Frame(self, bg=base)
        head.pack(fill=tk.X)
        avatar(head, size=30, glyph=glyph, fill=C["accent"], shape="rounded",
               bg=base).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(head, text=name, bg=base, fg=C["text"], font=FONT_UI_BOLD).pack(side=tk.LEFT)
        tk.Label(head, text=ts or time.strftime("%H:%M"), bg=base, fg=C["muted"],
                 font=FONT_CAPTION).pack(side=tk.LEFT, padx=(8, 0))
        self._role_badge = None
        if role:
            self._role_badge = badge(head, f"⚡ {role}", tone="accent_soft", bg=base)
            self._role_badge.pack(side=tk.LEFT, padx=(8, 0))
        self._status = tk.Label(head, text="", bg=base, fg=C["ter"], font=FONT_SMALL)
        self._status.pack(side=tk.RIGHT)
        self.subtitle = None
        if subtitle:
            self.subtitle = tk.Label(self, text=subtitle, bg=base, fg=C["ter"],
                                     font=FONT_SMALL, anchor="w", justify=tk.LEFT,
                                     wraplength=MAX_BUBBLE_WIDTH)
            self.subtitle.pack(fill=tk.X, pady=(4, 0))
        self.body = tk.Frame(self, bg=base)
        self.body.pack(fill=tk.X, pady=(6, 0))
        self._stream = None

    # -- 状态行 --
    def set_status(self, text: str):
        self._status.configure(text=text)

    def set_role(self, role: str | None):
        if role and self._role_badge is None:
            self._role_badge = badge(self.body.master, f"⚡ {role}", tone="accent_soft",
                                     bg=self._bg)
            self._role_badge.pack(side=tk.LEFT, padx=(8, 0))
        elif self._role_badge is not None and not role:
            self._role_badge.destroy()
            self._role_badge = None

    # -- 正文 --
    def stream_text(self, text: str):
        if self._stream is None:
            self._stream = InlineText(self.body, bg=self._bg)
            self._stream.pack(fill=tk.X)
        self._stream.set_text(text)

    def append_stream(self, piece: str):
        if self._stream is None:
            self._stream = InlineText(self.body, bg=self._bg)
            self._stream.pack(fill=tk.X)
        self._stream.append_text(piece)

    def render_markdown(self, text: str):
        if self._stream is not None:
            self._stream.destroy()
            self._stream = None
        host = render_blocks(self.body, text, bg=self._bg)
        host.pack(fill=tk.X)

    def add_widget(self, factory):
        widget = factory(self.body)
        widget.pack(fill=tk.X, pady=(4, 0))
        return widget

    def add_steps(self, items, *, title="执行步骤"):
        if title:
            tk.Label(self.body, text=title, bg=self._bg, fg=C["text"],
                     font=FONT_UI_BOLD, anchor="w").pack(fill=tk.X, pady=(8, 2))
        steps = StepList(self.body, items, bg=self._bg)
        steps.pack(fill=tk.X)
        return steps

    def add_tool_card(self, rows, *, title="调用工具", expanded=True):
        card = ToolCard(self.body, title=title, rows=rows, bg=self._bg,
                        expanded=expanded)
        card.pack(fill=tk.X)
        return card

    def add_note(self, text: str, *, tone="muted"):
        colors = {"muted": C["ter"], "ok": C["ok"], "error": C["error"],
                  "warn": C["warn"]}
        tk.Label(self.body, text=text, bg=self._bg, fg=colors.get(tone, C["ter"]),
                 font=FONT_SMALL, anchor="w", justify=tk.LEFT,
                 wraplength=self._max_width).pack(fill=tk.X, pady=(4, 0))

    def add_actions(self, actions):
        row = ActionRow(self.body, actions, bg=self._bg)
        row.pack(fill=tk.X, pady=(8, 0))
        return row


class NoticeMessage(tk.Frame):
    """系统提示 / 错误（左侧色条 + 小字）。"""

    def __init__(self, parent, text, *, bg=None, tone="error", title=None):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        tones = {"error": (C["error"], C["error_soft"]),
                 "warn": (C["warn"], C["warn_soft"]),
                 "ok": (C["ok"], C["ok_soft"]),
                 "info": (C["info"], C["info_soft"])}
        color, soft = tones.get(tone, tones["info"])
        card = RoundedCard(self, radius=R_MD, fill=soft, outline=color,
                           padx=12, pady=8, bg=base)
        card.pack(fill=tk.X)
        tk.Label(card.content, text=(title or "提示"), bg=soft, fg=color,
                 font=FONT_UI_BOLD, anchor="w").pack(fill=tk.X)
        tk.Label(card.content, text=text, bg=soft, fg=C["body"], font=FONT_SMALL,
                 anchor="w", justify=tk.LEFT,
                 wraplength=MAX_BUBBLE_WIDTH).pack(fill=tk.X, pady=(2, 0))


class MessageArea(tk.Frame):
    """可滚动消息列表 + 空态。"""

    def __init__(self, parent, *, bg=None):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        self._bg = base
        self.scroll = ScrollArea(self, bg=base, padx=18, pady=16)
        self.scroll.pack(fill=tk.BOTH, expand=True)
        self._empty = None
        self._count = 0
        self.show_empty()

    # -- 空态 --
    def show_empty(self, title="开始新对话",
                   lines=("右上启动 gateway，选好模型后在下方输入消息",
                          "输入卡左下可切换「沉思」：关闭 / 智能 / 开启")):
        self.clear()
        box = tk.Frame(self.scroll.inner, bg=self._bg)
        box.pack(fill=tk.X, pady=(60, 0))
        tk.Label(box, text=title, bg=self._bg, fg=C["text"], font=FONT_TITLE).pack()
        for line in lines:
            tk.Label(box, text=line, bg=self._bg, fg=C["muted"],
                     font=FONT_SMALL).pack(pady=(4, 0))
        self._empty = box

    def clear(self):
        for child in self.scroll.inner.winfo_children():
            child.destroy()
        self._empty = None
        self._count = 0

    @property
    def empty(self) -> bool:
        return self._count == 0

    # -- 追加 --
    def _prepare(self):
        """（保留钩子）子类/调用方可扩展。"""
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None

    def _finish(self, following):
        self._count += 1
        if following:
            self.scroll.scroll_to_end()

    def add_user(self, text, *, ts=None):
        following = self.scroll.at_bottom()
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None
        msg = UserMessage(self.scroll.inner, text, bg=self._bg, ts=ts)
        msg.pack(fill=tk.X, pady=(10, 0), anchor="e")
        self._finish(following)
        return msg

    def add_agent(self, *, role=None, ts=None, name="Forge", glyph="F", subtitle=None):
        following = self.scroll.at_bottom()
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None
        msg = AgentMessage(self.scroll.inner, bg=self._bg, name=name, role=role,
                           ts=ts, glyph=glyph, subtitle=subtitle)
        msg.pack(fill=tk.X, pady=(16, 0), anchor="w")
        self._finish(following)
        return msg

    def add_notice(self, text, *, tone="error", title=None):
        following = self.scroll.at_bottom()
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None
        msg = NoticeMessage(self.scroll.inner, text, bg=self._bg, tone=tone,
                            title=title)
        msg.pack(fill=tk.X, pady=(12, 0))
        self._finish(following)
        return msg

    def add_label(self, text, *, fg=None, font=None, pady=(8, 0)):
        following = self.scroll.at_bottom()
        lbl = tk.Label(self.scroll.inner, text=text, bg=self._bg,
                       fg=fg or C["ter"], font=font or FONT_SMALL,
                       anchor="w", justify=tk.LEFT, wraplength=MAX_BUBBLE_WIDTH)
        lbl.pack(fill=tk.X, pady=pady)
        self._finish(following)
        return lbl


# ─── 输入卡 ────────────────────────────────────────────────


class InputCard(tk.Frame):
    """底部输入卡（参考稿 5.4：占位提示 + 工具条 + 模型胶囊 + 圆形发送）。"""

    def __init__(self, parent, *, bg=None, placeholder="输入消息，或输入 / 使用命令...",
                 on_send=None, on_stop=None, on_paste=None, on_model=None,
                 models=None, model_var=None, thinking_text="◎ 沉思 · 关闭",
                 on_thinking=None, footer_left=None, footer_right=None,
                 attach_button=True, model_widget=None):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        self._on_send = on_send
        self._on_stop = on_stop
        self._thinking_text = thinking_text
        self.send_var = tk.StringVar()

        card = RoundedCard(self, radius=R_CARD, fill=C["input_bg"],
                           outline=C["border_hi"], padx=12, pady=10, bg=base)
        card.pack(fill=tk.X)
        inner = card.content

        entry_host = tk.Frame(inner, bg=C["input_bg"])
        entry_host.pack(fill=tk.X)
        self.entry = tk.Entry(entry_host, textvariable=self.send_var, font=FONT_UI,
                              bg=C["input_bg"], fg=C["text"],
                              insertbackground=C["accent"], relief=tk.FLAT, bd=0,
                              highlightthickness=0)
        self.entry.pack(fill=tk.X, ipady=3)
        self._hint = tk.Label(entry_host, text=placeholder, bg=C["input_bg"],
                              fg=C["placeholder"], font=FONT_UI, anchor="w",
                              cursor="xterm")
        self._hint.place(x=1, y=2)
        self._hint.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self.entry.bind("<Return>", lambda _e: self._fire_send())
        self.send_var.trace_add("write", lambda *_: self._sync_hint())
        self.entry.bind("<FocusIn>", lambda _e: self._sync_hint())
        self.entry.bind("<FocusOut>", lambda _e: self._sync_hint())

        bar = tk.Frame(inner, bg=C["input_bg"])
        bar.pack(fill=tk.X, pady=(10, 0))

        right = tk.Frame(bar, bg=C["input_bg"])
        right.pack(side=tk.RIGHT)
        self.model_var = model_var or tk.StringVar(value="default")
        if model_widget is not None:
            self.model_pill = None
            self.model_widget = model_widget(right)
        else:
            self.model_widget = None
            self.model_pill = rounded_label(right, f"▣ {self.model_var.get()}",
                                            fill=C["sel"], outline=C["sel_border"],
                                            fg=C["body"], font=FONT_SMALL,
                                            command=on_model, bg=C["input_bg"],
                                            tooltip="切换模型")
            self.model_pill.pack(side=tk.LEFT, padx=(0, 8))
        self.think_pill = rounded_label(right, thinking_text,
                                        fill=C["surface2"], outline=C["border_hi"],
                                        fg=C["subtext"], font=FONT_SMALL,
                                        command=on_thinking, bg=C["input_bg"],
                                        tooltip="沉思模式：关闭 / 智能 / 开启")
        self.think_pill.pack(side=tk.LEFT, padx=(0, 8))
        self._mic = tk.Label(right, text="◉", bg=C["input_bg"], fg=C["subtext"],
                             font=FONT_SECTION, cursor="hand2")
        self._mic.pack(side=tk.LEFT, padx=(0, 8))
        attach_tooltip(self._mic, "语音输入（预留）")
        self.send_circle = circle_button(right, "↑", self._fire_send, size=30,
                                         kind="muted", bg=C["input_bg"],
                                         tooltip="发送（Enter）")
        self.send_circle.pack(side=tk.LEFT)
        self.stop_circle = circle_button(right, "■", self._fire_stop, size=30,
                                         kind="danger", bg=C["input_bg"],
                                         tooltip="停止生成")
        self._busy = False
        self.stop_circle.pack_forget()

        left = tk.Frame(bar, bg=C["input_bg"])
        left.pack(side=tk.LEFT)
        self.plus = circle_button(left, "＋", on_paste or (lambda: None), size=26,
                                  kind="muted", bg=C["input_bg"], glyph_size=11,
                                  tooltip="粘贴剪贴板")
        self.plus.pack(side=tk.LEFT, padx=(0, 8))
        if attach_button:
            for text, tip in (("附件", "附件（预留）"), ("Context", "上下文（预留）"),
                              ("/ 命令", "斜杠命令（预留）")):
                pill = rounded_label(left, text, fill=C["surface2"],
                                     outline=C["border_hi"], fg=C["subtext"],
                                     font=FONT_MICRO, bg=C["input_bg"],
                                     tooltip=tip)
                pill.pack(side=tk.LEFT, padx=(0, 6))

        foot = tk.Frame(self, bg=base)
        foot.pack(fill=tk.X, pady=(6, 2))
        self.footer_left = tk.Label(foot, text=footer_left or "空闲", bg=base,
                                    fg=C["muted"], font=FONT_CAPTION)
        self.footer_left.pack(side=tk.LEFT)
        self.footer_right = tk.Label(foot, text=footer_right or "forge 在本地运行，内容由 AI 生成",
                                     bg=base, fg=C["muted"], font=FONT_CAPTION)
        self.footer_right.pack(side=tk.RIGHT)

        self._sync_hint()
        self._sync_send_state()

    # -- 交互 --
    def _fire_send(self):
        if self._busy:
            return
        if self._on_send:
            self._on_send()

    def _fire_stop(self):
        if self._on_stop:
            self._on_stop()

    def _sync_hint(self):
        try:
            focused = self.focus_get()
        except (tk.TclError, KeyError):
            focused = None
        show = not self.send_var.get().strip() and focused is not self.entry
        try:
            self._hint.configure(bg=C["input_bg"])
            if show:
                self._hint.place(x=1, y=2)
            else:
                self._hint.place_forget()
        except tk.TclError:
            pass
        self._sync_send_state()

    def _sync_send_state(self):
        if self._busy:
            return
        has_text = bool(self.send_var.get().strip())
        circle_button_state(self.send_circle, "primary" if has_text else "muted")

    def set_busy(self, busy: bool):
        self._busy = busy
        if busy:
            self.stop_circle.pack(side=tk.LEFT)
            circle_button_state(self.send_circle, "muted")
        else:
            self.stop_circle.pack_forget()
            self._sync_send_state()

    def set_model_text(self, text: str):
        if self.model_pill is None:
            return
        try:
            self.model_pill.set_text(f"▣ {text}")
        except Exception:
            pass

    def set_thinking_text(self, text: str):
        self._thinking_text = text
        try:
            self.think_pill.set_text(text)
        except Exception:
            pass

    def set_status(self, text: str):
        self.footer_left.configure(text=text)

    def focus_entry(self):
        self.entry.focus_set()
