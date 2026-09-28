"""对话区渲染组件（深色主题，重构版：交互与视觉）。

模块内 API 表面（forge_gui_v2.py 在用，不要改签名）：
    MessageArea      可滚动消息列表
        .add_user / .add_agent / .add_notice / .add_label / .clear /
        .show_empty / .scroll(.at_bottom/.near_bottom/.scroll_to_end) /
        ._count / .inner
    UserMessage      右侧气泡（自适应宽度，已修历史 1px 压扁 bug）
    AgentMessage     左侧富文本回复，支持正文+折叠 trace
        .set_status / .stream_text / .append_stream / .render_markdown /
        .add_steps / .add_tool_card / .add_note / .add_actions / .body
    InputCard        底部 Composer
        .entry / .send_var / .think_pill / .send_circle / .stop_circle /
        .set_busy / .set_model_text / .set_thinking_text / .set_status /
        .focus_entry / ._sync_send_state
    模块级
        .set_brand_avatar(image, keep_alive=None)
        .set_file_link_handler(handler)

正文渲染：轻量 Markdown（段落 / 标题 / 有序无序列表 / 待办勾选 / 引用 /
代码块 / 行内 **加粗** / `行内代码` / 文件路径 → 可点击链接）。代码块
内不识别文件路径。
"""
from __future__ import annotations

import re
import time
import tkinter as tk
from typing import Callable, Optional

from gui_theme import (
    C, FONT_CAPTION, FONT_MICRO, FONT_MONO, FONT_MONO_SM, FONT_MONO_XS, FONT_SECTION,
    FONT_SMALL, FONT_TITLE, FONT_UI, FONT_UI_BOLD, R_CARD, R_MD, R_PILL,
    RoundedCard, attach_tooltip, avatar, badge, circle_button, circle_button_state,
    dot, emoji_font, glyph_button, highlight_python, round_rect, rounded_label,
    setup_code_tags, style_scrollbar,
)

MAX_BUBBLE_WIDTH = 740          # 中央 Conversation 是主体，长文允许更宽的阅读行
USER_AUTOSIZE_PAD_X = 14
USER_AUTOSIZE_PAD_Y = 10

# ─── 品牌头像（由主程序启动时注入） ─────────────────────────
_BRAND_AVATAR = None
_BRAND_AVATAR_KEEP = None


def set_brand_avatar(image, keep_alive=None):
    """注入品牌头像位图（tk.PhotoImage）。主程序从 assets 加载后调用。"""
    global _BRAND_AVATAR, _BRAND_AVATAR_KEEP
    _BRAND_AVATAR = image
    _BRAND_AVATAR_KEEP = keep_alive if keep_alive is not None else image


# ─── 文件引用 handler（任务 D） ─────────────────────────────
# 调用方约定：handler(path_str: str) -> bool；返回 True 表示已处理。
_FILE_LINK_HANDLER: Optional[Callable[[str], bool]] = None


def set_file_link_handler(handler: Optional[Callable[[str], bool]]):
    """注册文件链接处理器。传 None 时点击静默无操作。"""
    global _FILE_LINK_HANDLER
    _FILE_LINK_HANDLER = handler


# 形如 `forge/loop.py`、`forge-gui/workspace.py`、`src/lib/rate-limit.ts`
# 可选带行号 / 行号范围；也允许被 markdown 链接 [text](path) 包裹。
_FILE_LINK_RE = re.compile(
    r"(?P<md>\[(?P<md_text>[^\]]+)\]\((?P<md_path>[^)\s]+)\)"  # [text](path)
    r"|(?P<path>[A-Za-z0-9_./-]+/[A-Za-z0-9_./\-]+\.[A-Za-z0-9]+"
    r"(?::[0-9]+(?:-[0-9]+)?)?))"
)


def _looks_like_markdown_or_path(token: str) -> bool:
    """识别 token 是否像文件路径（含行号）或被 markdown 链接包裹的路径。"""
    return bool(_FILE_LINK_RE.fullmatch(token)) or _FILE_LINK_RE.search(token)


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
    """自动高度的只读 Text：支持 **加粗**、`行内代码` 与可点击文件链接。"""

    def __init__(self, parent, *, bg=None, fg=None, font=None, width=None,
                 wrap=tk.WORD, on_link=None):
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
        self.tag_configure("file_link", foreground=C["accent2"],
                           underline=True, font=font or FONT_UI)
        self._base_bg = base
        self._on_link = on_link
        if width:
            self.configure(width=width)
        self.bind("<Configure>", self._fit_height)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda _e: self.configure(cursor="arrow"))
        self.bind("<Button-1>", self._click)
        self.configure(state=tk.DISABLED)
        self._last_h = 1
        self.tag_bind("file_link", "<Enter>",
                      lambda _e: self.configure(cursor="hand2"))
        self.tag_bind("file_link", "<Leave>",
                      lambda _e: self.configure(cursor="arrow"))

    # -- 高度自适应 --
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
        """segs 是 (text, tag|None|iterable) 列表，自动识别文件链接。"""
        self.configure(state=tk.NORMAL)
        self.delete("1.0", tk.END)
        for text, tag in segs:
            if not text:
                continue
            extra_tag = ("file_link",) if tag is None else (
                tag + ("file_link",) if isinstance(tag, tuple)
                else (tag, "file_link")
            )
            ordered = _split_text_into_plain_and_links(text)
            for piece, is_link in ordered:
                chosen_tag = extra_tag if is_link else (
                    tuple(t for t in (extra_tag if isinstance(extra_tag, tuple) else (extra_tag,))
                          if t != "file_link")
                )
                self.insert(tk.END, piece, chosen_tag)
        self.configure(state=tk.DISABLED)
        self._last_h = -1
        self.after_idle(self._recompute)

    def set_text(self, text, tag=None):
        self.set_segments([(text, tag)])

    def append_text(self, text, tag=None):
        self.configure(state=tk.NORMAL)
        if not text:
            pass
        else:
            extra_tag = ("file_link",) if tag is None else (
                tag + ("file_link",) if isinstance(tag, tuple)
                else (tag, "file_link")
            )
            ordered = _split_text_into_plain_and_links(text)
            for piece, is_link in ordered:
                chosen_tag = extra_tag if is_link else (
                    tuple(t for t in (extra_tag if isinstance(extra_tag, tuple) else (extra_tag,))
                          if t != "file_link")
                )
                self.insert(tk.END, piece, chosen_tag)
        self.configure(state=tk.DISABLED)
        self._last_h = -1
        self.after_idle(self._recompute)

    # -- 链接交互 --
    def _motion(self, event):
        try:
            idx = self.index(f"@{event.x},{event.y}")
            for tag in self.tag_names(idx):
                if tag == "file_link":
                    self.configure(cursor="hand2")
                    return
        except tk.TclError:
            pass
        self.configure(cursor="arrow")

    def _click(self, event):
        try:
            idx = self.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return
        for tag in self.tag_names(idx):
            if tag == "file_link":
                # 走到 file_link 区间起点以拿到完整 token
                start = self.index(f"{idx} wordstart")
                end = self.index(f"{idx} wordend")
                token = self.get(start, end).strip()
                if self._on_link:
                    self._on_link(token)
                else:
                    _dispatch_file_link(token)
                return "break"
        return None


def _split_text_into_plain_and_links(text: str):
    """把文本切成 (片段, 是否链接) 段。在 markdown 链接场合只用 md_path。"""
    segs = []
    i = 0
    for m in _FILE_LINK_RE.finditer(text):
        s, e = m.span()
        if s > i:
            segs.append((text[i:s], False))
        if m.group("md_path"):
            # [text](path)：用 path 作为可点击区域；显示原文可保留 md 文本
            segs.append((m.group("md"), True))
        else:
            segs.append((m.group("path"), True))
        i = e
    if i < len(text):
        segs.append((text[i:], False))
    return segs or [(text, False)]


def _dispatch_file_link(token: str):
    if _FILE_LINK_HANDLER is None:
        return
    path = token
    # 处理 markdown 包裹 [text](path) 时取括号内路径
    md = _FILE_LINK_RE.fullmatch(token)
    if md and md.group("md_path"):
        path = md.group("md_path")
    try:
        _FILE_LINK_HANDLER(path)
    except Exception:
        # handler 自己负责容错，这里吞掉避免破坏渲染链
        pass


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
    """轻量 Markdown → 块列表。"""
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
            mark = "✅" if block["done"] else "⬜"
            color = C["ok"] if block["done"] else C["muted"]
            tk.Label(row, text=mark, bg=base, fg=color, font=emoji_font(mark),
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
            # 代码块不识别文件链接：用不签名 InlineText 的 _on_link 的 Text 直接渲染。
            card = RoundedCard(host, radius=R_MD, fill=C["code_bg"],
                               outline=C["border_hi"], padx=10, pady=8, bg=base)
            card.pack(fill=tk.X, pady=6)
            tx = tk.Text(card.content, bg=C["code_bg"], fg=C["code_plain"],
                         font=FONT_MONO_SM, relief=tk.FLAT, bd=0,
                         highlightthickness=0, wrap=tk.NONE, height=1)
            tx.insert("1.0", block["text"])
            setup_code_tags(tx, font=FONT_MONO_SM)
            lang = block.get("lang") or ""
            if lang in ("", "py", "python") or lang.startswith("py"):
                try:
                    highlight_python(tx)
                except Exception:
                    pass
            line_count = max(1, len(block["text"].split("\n")))
            tx.configure(height=min(line_count, 24), state=tk.DISABLED)
            tx.pack(fill=tk.X)
        elif kind == "hr":
            tk.Frame(host, bg=C["border"], height=1).pack(fill=tk.X, pady=6)
    return host


# ─── 消息块（折叠版） ──────────────────────────────────────


def _auto_wrap(text: str, seg_list) -> str:
    """根据 seg_list[(label, unit_seconds)] 拼出 'N 个工具 · Xs'。"""
    n = len(seg_list)
    if n == 0:
        return "0 个工具"
    parts = [f"{n} 个工具"]
    seconds = 0.0
    for _, unit in seg_list:
        if isinstance(unit, (int, float)):
            seconds += float(unit)
    if seconds > 0:
        # 1.2s / 0.45s / 12s
        if seconds >= 10:
            parts.append(f"{seconds:.0f}s")
        else:
            parts.append(f"{seconds:.1f}s")
    return " · ".join(parts)


def _guess_seconds_from_row(row: dict) -> float:
    """从 ToolCard/StepList 行 dict 推断秒数（数字 elapsed）。"""
    e = row.get("elapsed_s")
    if isinstance(e, (int, float)):
        return float(e)
    e = row.get("elapsed")
    if isinstance(e, (int, float)):
        return float(e)
    # 字符串格式暂不解析（如 '2.4s' / '#1'）——保守不计入总秒数
    return 0.0


class ToolCard(tk.Frame):
    """「调用工具 (n)」折叠卡片：默认折叠，点摘要展开明细。"""

    def __init__(self, parent, *, title="调用工具", rows=None, bg=None,
                 expanded=False):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        self._expanded = bool(expanded)
        self._title_text = title

        # 摘要行（永远可见，最弱视觉）
        self._summary = tk.Frame(self, bg=base, cursor="hand2")
        self._summary.pack(fill=tk.X, pady=(8, 2))
        self._summary.bind("<Button-1>", lambda _e: self.toggle())
        self._arrow = tk.Label(self._summary, text="▸", bg=base, fg=C["ter"],
                               font=FONT_MONO_SM, cursor="hand2")
        self._arrow.pack(side=tk.LEFT, padx=(0, 6))
        self._arrow.bind("<Button-1>", lambda _e: self.toggle())
        self._summary_label = tk.Label(self._summary, text="", bg=base,
                                       fg=C["muted"], font=FONT_CAPTION,
                                       cursor="hand2", anchor="w")
        self._summary_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._summary_label.bind("<Button-1>", lambda _e: self.toggle())

        # 明细容器（按需显示）
        card = RoundedCard(self, radius=R_CARD, fill=C["surface2"],
                           outline=C["border_hi"], padx=10, pady=8, bg=base)
        self._card = card
        self._card_host = card  # alias
        self.rows_frame = tk.Frame(card.content, bg=C["surface2"])
        self.rows_frame.pack(fill=tk.X)
        self._row_specs: list[tuple[str, float]] = []
        rows = rows or []
        for row in rows:
            self.add_row(row)

        if self._expanded:
            card.pack(fill=tk.X, pady=(2, 4))
        # 计算摘要文字
        self._refresh_summary()

    # -- 摘要 --
    def _refresh_summary(self):
        n = len(self._row_specs)
        secs = sum(s for _, s in self._row_specs)
        text = f"工具调用 · {n} 项"
        if secs > 0:
            if secs >= 10:
                text += f" · {secs:.0f}s"
            else:
                text += f" · {secs:.1f}s"
        self._summary_label.configure(text=text)

    # -- 折叠 --
    def toggle(self):
        self._expanded = not self._expanded
        self._arrow.configure(text="▾" if self._expanded else "▸")
        if self._expanded:
            self._card.pack(fill=tk.X, pady=(2, 4))
        else:
            self._card.pack_forget()

    def add_row(self, row: dict):
        bgc = C["surface2"]
        wrap = tk.Frame(self.rows_frame, bg=bgc)
        wrap.pack(fill=tk.X, pady=1)
        ok = row.get("ok", True)
        mark = "✅" if ok else "⚠️"
        tk.Label(wrap, text=mark, bg=bgc,
                 fg=C["ok"] if ok else C["warn"],
                 font=emoji_font(mark, 10), width=2).pack(side=tk.LEFT)
        tk.Label(wrap, text=row.get("name", ""), bg=bgc, fg=C["accent2"],
                 font=FONT_MONO_SM).pack(side=tk.LEFT)
        desc = row.get("desc")
        if desc:
            tk.Label(wrap, text=desc, bg=bgc, fg=C["ter"], font=FONT_SMALL,
                     anchor="w", justify=tk.LEFT).pack(side=tk.LEFT, padx=(10, 6))
        if row.get("elapsed"):
            tk.Label(wrap, text=str(row["elapsed"]), bg=bgc, fg=C["muted"],
                     font=FONT_MONO_SM).pack(side=tk.RIGHT)
        if row.get("detail"):
            attach_tooltip(wrap, row["detail"])
            for child in wrap.winfo_children():
                attach_tooltip(child, row["detail"])
        # 记录 (label, seconds) 以便摘要显示耗时
        self._row_specs.append((row.get("name", ""), _guess_seconds_from_row(row)))


class StepList(tk.Frame):
    """执行步骤列表（默认折叠，摘要行显示总数）。"""

    def __init__(self, parent, items, *, bg=None, expanded=False):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        self._expanded = bool(expanded)
        self._items = items or []

        # 摘要行
        self._summary = tk.Frame(self, bg=base, cursor="hand2")
        self._summary.pack(fill=tk.X, pady=(6, 2))
        self._summary.bind("<Button-1>", lambda _e: self.toggle())
        self._arrow = tk.Label(self._summary, text="▸", bg=base, fg=C["ter"],
                               font=FONT_MONO_SM, cursor="hand2")
        self._arrow.pack(side=tk.LEFT, padx=(0, 6))
        self._arrow.bind("<Button-1>", lambda _e: self.toggle())
        n = len(self._items)
        self._summary_label = tk.Label(self._summary,
                                       text=f"执行步骤 {n} 步",
                                       bg=base, fg=C["muted"], font=FONT_CAPTION,
                                       cursor="hand2", anchor="w")
        self._summary_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._summary_label.bind("<Button-1>", lambda _e: self.toggle())

        # 明细容器
        self._detail = tk.Frame(self, bg=base)
        if self._expanded:
            self._detail.pack(fill=tk.X, pady=(2, 0))
        for item in self._items:
            self._add_detail_row(item)

    def _add_detail_row(self, item: dict):
        base = self["bg"]
        row = tk.Frame(self._detail, bg=base)
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
            tk.Label(right, text=str(item["elapsed"]), bg=base, fg=C["muted"],
                     font=FONT_MONO_SM).pack(side=tk.LEFT, padx=(0, 8))
        if item.get("done", True):
            chk = tk.Canvas(right, width=14, height=14, bg=base,
                            highlightthickness=0, bd=0)
            chk.create_oval(0, 0, 13, 13, fill=C["ok"], outline="")
            chk.create_text(7, 7, text="✓", fill="#0B0B10", font=FONT_MICRO)
            chk.pack(side=tk.LEFT)

    def toggle(self):
        self._expanded = not self._expanded
        self._arrow.configure(text="▾" if self._expanded else "▸")
        if self._expanded:
            self._detail.pack(fill=tk.X, pady=(2, 0))
        else:
            self._detail.pack_forget()


class ActionRow(tk.Frame):
    """消息底部动作按钮组。"""

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


# ─── 消息：用户与 Agent ────────────────────────────────────


class AgentStatusIndicator(tk.Frame):
    """Forge 状态：品牌化 glyph + 低权重文字，替代临时“表情”。"""

    ACTIVE_WORDS = ("生成", "思考", "规划", "读取", "执行", "运行", "编辑", "连接")

    def __init__(self, parent, *, bg):
        super().__init__(parent, bg=bg)
        self._base = bg
        self._after_id = None
        self._phase = 0
        self.canvas = tk.Canvas(self, width=18, height=18, bg=bg,
                                highlightthickness=0, bd=0)
        self.canvas.pack(side=tk.LEFT, padx=(0, 5))
        self.label = tk.Label(self, text="", bg=bg, fg=C["muted"],
                              font=FONT_CAPTION)
        self.label.pack(side=tk.LEFT)
        self.pack_forget()
        self.bind("<Destroy>", self._on_destroy, add="+")

    def _on_destroy(self, event=None):
        if event is not None and event.widget is not self:
            return
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def set(self, text: str):
        text = str(text or "")
        self.label.configure(text=text)
        if not text:
            self.pack_forget()
            self._on_destroy()
            return
        if not self.winfo_manager():
            self.pack(side=tk.RIGHT)
        lowered = text.lower()
        if any(word in lowered for word in self.ACTIVE_WORDS):
            self._tick()
        else:
            self._on_destroy()
            color = C["error"] if any(w in lowered for w in ("失败", "错误")) else C["ok"]
            self._draw_static(color)

    def _draw_logo(self, color: str):
        self.canvas.delete("all")
        self.canvas.create_polygon(9, 1, 17, 9, 9, 17, 1, 9,
                                   fill=color, outline="")
        self.canvas.create_polygon(9, 5, 13, 9, 9, 13, 5, 9,
                                   fill=self._base, outline="")

    def _draw_static(self, color: str):
        self._draw_logo(color)
        self.canvas.create_oval(13, 1, 17, 5, fill=color, outline="")

    def _tick(self):
        self._on_destroy()
        colors = (C["accent2"], C["accent"], C["accent_hover"])
        self._draw_logo(colors[self._phase % len(colors)])
        for idx in range(3):
            color = C["accent_text"] if idx == self._phase % 3 else C["muted"]
            self.canvas.create_oval(2 + idx * 5, 14, 5 + idx * 5, 17,
                                    fill=color, outline="")
        self._phase += 1
        try:
            self._after_id = self.after(320, self._tick)
        except tk.TclError:
            self._after_id = None


class UserMessage(tk.Frame):
    """右侧气泡用户消息（已修复 1px 压扁 bug：autosize_width=True）。

    整行容器 fill=tk.X；气泡用 anchor="e" 靠右；头像+名+时间是同一行。
    """

    def __init__(self, parent, text, *, bg=None, ts=None, name="你"):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)

        # head 行：名字 + 时间 靠右（以便头像与气泡视觉对齐）
        head = tk.Frame(self, bg=base)
        head.pack(fill=tk.X)
        # 名字靠右
        tk.Label(head, text=ts or time.strftime("%H:%M"), bg=base, fg=C["muted"],
                 font=FONT_CAPTION).pack(side=tk.RIGHT, padx=(8, 0))
        tk.Label(head, text=name, bg=base, fg=C["subtext"], font=FONT_CAPTION,
                 anchor="e").pack(side=tk.RIGHT)
        # 占位 spacer（左）与头像，用来把气泡挤到右侧
        spacer = tk.Frame(head, bg=base)
        spacer.pack(side=tk.LEFT, fill=tk.X, expand=True)
        spacer_l = tk.Frame(spacer, bg=base)
        spacer_l.pack(side=tk.RIGHT)
        av = avatar(spacer_l, size=26, glyph="你", fill="#2A2A38", shape="circle",
                    bg=base)
        av.pack(side=tk.RIGHT, padx=(8, 0))

        # 气泡（autosize_width=True 修历史 bug）
        bubble_host = tk.Frame(self, bg=base)
        bubble_host.pack(fill=tk.X, anchor="e", pady=(4, 0))
        card = RoundedCard(bubble_host, radius=R_CARD, fill=C["msg_user_bg"],
                           outline=C["msg_user_border"],
                           padx=USER_AUTOSIZE_PAD_X, pady=USER_AUTOSIZE_PAD_Y,
                           bg=base, autosize_width=True)
        card.pack(anchor="e")
        self._card = card
        # 文字 label：撑开气泡。wraplength 设上限，让长文本真的换行
        max_text = MAX_BUBBLE_WIDTH - USER_AUTOSIZE_PAD_X * 2 - 16
        self.label = tk.Label(card.content, text=text, bg=C["msg_user_bg"],
                              fg=C["body"], font=FONT_UI, justify=tk.LEFT,
                              anchor="w", wraplength=max_text)
        self.label.pack(anchor="w")
        self.bind("<Configure>", self._fit_bubble)

    def _fit_bubble(self, event):
        # Workspace 打开后行宽变小，气泡必须重新换行，而不是裁掉正文。
        available = max(80, event.width - USER_AUTOSIZE_PAD_X * 2 - 16)
        self.label.configure(wraplength=min(MAX_BUBBLE_WIDTH - 44, available))


class AgentMessage(tk.Frame):
    """Forge 的回复：头像 + 名字 + 角色徽章 + 正文 + trace（可折叠）。"""

    def __init__(self, parent, *, bg=None, name="Forge", role=None, ts=None,
                 glyph="F", subtitle=None):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        self._base = base
        # 正文气孔用独立底色：head 行在对话底色上，正文落在气孔里。
        # （以前 agent 侧根本没有气孔，整条时间线是平的。）
        self._bg = C["msg_agent_bg"]
        self._max_width = MAX_BUBBLE_WIDTH

        # head 行：avatar + 名字 + 时间(降权) + 角色徽章 + 状态（右对齐）
        head = tk.Frame(self, bg=base)
        head.pack(fill=tk.X)
        self._head = head
        avatar(head, size=28, glyph=glyph, fill=C["accent"], shape="rounded",
               bg=base, image=_BRAND_AVATAR).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(head, text=name, bg=base, fg=C["text"], font=FONT_UI_BOLD).pack(side=tk.LEFT)
        tk.Label(head, text=ts or time.strftime("%H:%M"), bg=base, fg=C["muted"],
                 font=FONT_CAPTION).pack(side=tk.LEFT, padx=(8, 0))
        self._role_badge = None
        if role:
            self._role_badge = badge(head, f"⚡ {role}", tone="accent_soft", bg=base)
            self._role_badge.pack(side=tk.LEFT, padx=(8, 0))
        self._status_indicator = AgentStatusIndicator(head, bg=base)
        self._status = self._status_indicator.label  # 保留旧测试/调用方可见属性

        self.subtitle = None
        if subtitle:
            self.subtitle = tk.Label(self, text=subtitle, bg=base, fg=C["ter"],
                                     font=FONT_SMALL, anchor="w", justify=tk.LEFT,
                                     wraplength=MAX_BUBBLE_WIDTH)
            self.subtitle.pack(fill=tk.X, pady=(4, 0))

        # 正文气孔：圆角卡 + hairline 描边，跟用户侧对称。整体宽度跟随对话列，
        # 这样代码块/工具卡/表格都能拿到完整宽度（不为了「抱得紧」把内容压窄）。
        self._bubble_host = tk.Frame(self, bg=base)
        self._bubble_host.pack(fill=tk.X, anchor="w", pady=(6, 0))
        self._bubble = RoundedCard(self._bubble_host, radius=R_CARD,
                                   fill=self._bg, outline=C["msg_agent_border"],
                                   padx=USER_AUTOSIZE_PAD_X,
                                   pady=USER_AUTOSIZE_PAD_Y, bg=base)
        # 跟随列宽（fill=X）而不是抱紧内容：里面的 markdown 标签带 wraplength 上限，
        # 列窄时靠 fill=X 让它们重新折行，不会横向溢出。
        self._bubble.pack(fill=tk.X)
        self.body = tk.Frame(self._bubble.content, bg=self._bg)
        self.body.pack(fill=tk.X)
        # RoundedCard 靠 content 的 <Configure> 反推高度；正文是后来才填进去的，
        # 在 fill=X 模式下这条链会断（画布高度停在 1，内容不 mapped）。
        # 这里由 body/host 直接驱动高度，不依赖那条隐式链。
        self._bubble_host.bind("<Configure>", self._sync_bubble_height)
        self.body.bind("<Configure>", self._sync_bubble_height)
        self._stream = None

    def _sync_bubble_height(self, _event=None):
        try:
            need = self.body.winfo_reqheight() + USER_AUTOSIZE_PAD_Y * 2
            cv = self._bubble._cv
            if abs(cv.winfo_reqheight() - need) > 1:
                cv.configure(height=max(1, need))
            self._bubble._on_canvas()
        except tk.TclError:
            pass

    def set_status(self, text: str):
        self._status_indicator.set(text)

    def set_role(self, role):
        if role and self._role_badge is None:
            self._role_badge = badge(self._head, f"⚡ {role}",
                                     tone="accent_soft", bg=self._base)
            self._role_badge.pack(side=tk.LEFT, padx=(8, 0))
        elif self._role_badge is not None and not role:
            self._role_badge.destroy()
            self._role_badge = None

    # -- 正文（流式 & markdown）--
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
        # 注释要求：「如果 AgentMessage 里既渲染正文又渲染工具/步骤，确保正文始终在最上、
        # trace 折叠块在正文下方，且有轻微分组」。
        # 我们每次 render_markdown：若已有 trace，加细分割线，重新顺序正文/trace。
        if self._stream is not None:
            self._stream.destroy()
            self._stream = None
        host = render_blocks(self.body, text, bg=self._bg)
        host.pack(fill=tk.X, in_=self.body, side=tk.TOP)
        # 已有 trace？保持顺序：Body 段在最上 → 分割线 → 现有 trace 块
        self._reorder_body_with_trace()

    def _reorder_body_with_trace(self):
        """如有 trace 容器，重新排布：正文 block(s) → 分割线 → trace。"""
        if not getattr(self, "_trace_container", None):
            return
        if not self._trace_container.winfo_exists():
            return
        # 把分割线（如果还没有）插在最后正文段和 trace 之间
        if not getattr(self, "_trace_divider", None) or not self._trace_divider.winfo_exists():
            self._trace_divider = tk.Frame(self.body, bg=C["border"], height=1)
        # 让所有 w 在 body 里按当前 pack 顺序重新插入
        # tk 没有公开 reorder API；这里简化为「再次 pack_forget + pack」按目标顺序
        # 但因为正文 host 由 render_blocks 创建，我们只在「正文在 trace 上方」已经成立时再保证。
        # —— 默认情况正文先 render、trace 后 add，pack 顺序天然正确；
        # 这里补一个分割线 ID + 在 trace 顶部 pack
        try:
            self._trace_divider.pack(fill=tk.X, pady=(8, 4), before=self._trace_container)
        except tk.TclError:
            self._trace_divider.pack(fill=tk.X, pady=(8, 4))

    def add_widget(self, factory):
        widget = factory(self.body)
        widget.pack(fill=tk.X, pady=(4, 0))
        return widget

    def add_steps(self, items, *, title="执行步骤"):
        host_frame = self._ensure_trace_host()
        steps = StepList(host_frame, items, bg=self._bg, expanded=False)
        steps.pack(fill=tk.X)
        self._trace_container = host_frame
        self._reorder_body_with_trace()
        return steps

    def add_tool_card(self, rows, *, title="调用工具", expanded=False):
        host_frame = self._ensure_trace_host()
        card = ToolCard(host_frame, title=title, rows=rows, bg=self._bg,
                        expanded=bool(expanded))
        card.pack(fill=tk.X)
        self._trace_container = host_frame
        self._reorder_body_with_trace()
        return card

    def _ensure_trace_host(self):
        """第一次 add_steps/add_tool_card 时创建 trace 容器。"""
        host = getattr(self, "_trace_host", None)
        if host is None or not host.winfo_exists():
            host = tk.Frame(self.body, bg=self._bg)
            host.pack(fill=tk.X)
            self._trace_host = host
        return host

    def add_note(self, text: str, *, tone="muted"):
        # add_note 不属于正文/trace，更接近「提示」：降权 + 小字 + 紧贴 trace 之后
        host_frame = getattr(self, "_trace_host", None)
        if host_frame is None or not host_frame.winfo_exists():
            host_frame = self._ensure_trace_host()
        colors = {"muted": C["muted"], "ok": C["ok"], "error": C["error"],
                  "warn": C["warn"], "info": C["info"]}
        tk.Label(host_frame, text=text, bg=self._bg,
                 fg=colors.get(tone, C["muted"]),
                 font=FONT_CAPTION, anchor="w", justify=tk.LEFT,
                 wraplength=self._max_width).pack(fill=tk.X, pady=(4, 0))

    def add_actions(self, actions):
        # 动作按钮单独一个 host，靠底部（不被 trace 折叠吸收）
        if not hasattr(self, "_action_row_packed"):
            self._action_row_packed = False
        host = tk.Frame(self, bg=self._bg)
        host.pack(fill=tk.X, pady=(8, 0))
        row = ActionRow(host, actions, bg=self._bg)
        row.pack(fill=tk.X)
        self._action_row_packed = True
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
        self.scroll = ScrollArea(self, bg=base, padx=18, pady=18)
        self.scroll.pack(fill=tk.BOTH, expand=True)
        self._empty = None
        self._count = 0
        self.show_empty()

    def show_empty(self, title="从一个目标开始",
                   lines=("描述你想解决的问题，或添加文件作为上下文。",
                          "执行工具请选择左侧「任务」；连接信息在右上「状态」。"),
                   actions=()):
        self.clear()
        box = tk.Frame(self.scroll.inner, bg=self._bg)
        box.pack(fill=tk.X, pady=(32, 0))
        tk.Label(box, text=title, bg=self._bg, fg=C["text"], font=FONT_TITLE).pack()
        for line in lines:
            label = tk.Label(box, text=line, bg=self._bg, fg=C["ter"],
                             font=FONT_SMALL, wraplength=500, justify=tk.CENTER)
            label.pack(fill=tk.X, pady=(8, 0))
            label.bind("<Configure>", lambda e, w=label: w.configure(
                wraplength=max(80, e.width - 16)))
        if actions:
            choices = tk.Frame(box, bg=self._bg)
            choices.pack(pady=(18, 0))
            for title, detail, callback in actions:
                row = tk.Frame(choices, bg=self._bg, padx=14, pady=6,
                               cursor="hand2")
                row.pack(fill=tk.X, pady=(0, 3))
                label = tk.Label(row, text=title, bg=self._bg, fg=C["body"],
                                 font=FONT_SMALL, anchor="w", cursor="hand2")
                label.pack(fill=tk.X)
                hint = tk.Label(row, text=detail, bg=self._bg, fg=C["muted"],
                                font=FONT_CAPTION, anchor="w", justify=tk.LEFT,
                                cursor="hand2")
                hint.pack(fill=tk.X, pady=(2, 0))
                for widget in (row, label, hint):
                    widget.bind("<Button-1>", lambda _e, fn=callback: fn())
                    widget.bind("<Enter>", lambda _e, r=row, l=label, h=hint: (
                        r.configure(bg=C["hover"]), l.configure(bg=C["hover"]),
                        h.configure(bg=C["hover"])))
                    widget.bind("<Leave>", lambda _e, r=row, l=label, h=hint: (
                        r.configure(bg=self._bg), l.configure(bg=self._bg),
                        h.configure(bg=self._bg)))
        self._empty = box

    def clear(self):
        for child in self.scroll.inner.winfo_children():
            child.destroy()
        self._empty = None
        self._count = 0

    @property
    def empty(self) -> bool:
        return self._count == 0

    def _prepare(self):
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None

    def _finish(self, following):
        self._count += 1
        if following:
            self.scroll.scroll_to_end()

    def add_user(self, text, *, ts=None):
        following = self.scroll.at_bottom()
        self._prepare()
        msg = UserMessage(self.scroll.inner, text, bg=self._bg, ts=ts)
        msg.pack(fill=tk.X, pady=(10, 0))
        self._finish(following)
        return msg

    def add_agent(self, *, role=None, ts=None, name="Forge", glyph="F",
                  subtitle=None):
        following = self.scroll.at_bottom()
        self._prepare()
        msg = AgentMessage(self.scroll.inner, bg=self._bg, name=name, role=role,
                           ts=ts, glyph=glyph, subtitle=subtitle)
        msg.pack(fill=tk.X, pady=(16, 0))
        self._finish(following)
        return msg

    def add_notice(self, text, *, tone="error", title=None):
        following = self.scroll.at_bottom()
        self._prepare()
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


# ─── 输入卡（Composer） ────────────────────────────────────


class InputCard(tk.Frame):
    """底部 Composer：输入是主体，低频控制统一收进水平工具栏。"""

    def __init__(self, parent, *, bg=None, placeholder="输入消息，或输入 / 使用命令...",
                 on_send=None, on_stop=None, on_paste=None, on_model=None,
                 models=None, model_var=None, thinking_text="◎ 沉思 · 关闭",
                 on_thinking=None, footer_left=None, footer_right=None,
                 attach_button=True, model_widget=None,
                 on_attach=None, on_context=None, on_commands=None,
                 on_settings=None):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        self._on_send = on_send
        self._on_stop = on_stop
        self._thinking_text = thinking_text
        self._on_settings = on_settings
        self.send_var = tk.StringVar()

        card = RoundedCard(self, radius=R_CARD, fill=C["input_bg"],
                           outline=C["border_hi"], padx=12, pady=10, bg=base)
        card.pack(fill=tk.X)
        self._card = card
        inner = card.content

        # ─── 顶行：＋ 加号 + 次级小按钮（左） + 主输入区（中） ─────────────
        top_row = tk.Frame(inner, bg=C["input_bg"])
        top_row.pack(fill=tk.X)

        # 附件入口在底栏；输入正文从同一条左边线开始。

        # ＋ 按钮（on_attach 优先；缺省回落到 on_paste，保持旧行为）
        plus_cb = on_attach if on_attach is not None else (on_paste or (lambda: None))
        # 次级小按钮：上下文、命令（弱化，小字）
        # 主输入区
        entry_host = tk.Frame(top_row, bg=C["input_bg"])
        entry_host.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.entry = tk.Text(entry_host, height=2, wrap="word", font=FONT_UI,
                              bg=C["input_bg"], fg=C["text"],
                              insertbackground=C["accent"], relief=tk.FLAT, bd=0,
                              highlightthickness=0)
        self.entry.pack(fill=tk.X, ipady=3)
        self._hint = tk.Label(entry_host, text=placeholder, bg=C["input_bg"],
                              fg=C["placeholder"], font=FONT_UI, anchor="w",
                              cursor="xterm")
        self._hint.place(x=1, y=2)
        self._hint.bind("<Button-1>", lambda _e: self.entry.focus_set())
        self.entry.bind("<Return>", self._enter)
        self.entry.bind("<Shift-Return>", lambda _e: None)
        self._syncing = False
        self.entry.bind("<<Modified>>", self._text_changed)
        self.entry.bind("<Configure>", lambda _e: self._resize_entry())
        self.send_var.trace_add("write", lambda *_: self._sync_hint())
        self.entry.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.entry.bind("<FocusOut>", lambda _e: self._set_focus(False))

        # ─── 底栏：模型 + 沉思 + ⚙ + 发送（右组） ─────────────────────────
        bar = tk.Frame(inner, bg=C["input_bg"])
        bar.pack(fill=tk.X, pady=(10, 0))
        bar.grid_columnconfigure(0, weight=1)
        self._toolbar = bar
        self._low_controls = None

        attachments = tk.Frame(bar, bg=C["input_bg"])
        attachments.grid(row=0, column=0, sticky="w")
        self._low_controls = attachments
        self.plus = circle_button(attachments, "＋", plus_cb, size=28,
                                  kind="muted", bg=C["input_bg"], glyph_size=11,
                                  tooltip="添加附件")
        self.plus.pack(side=tk.LEFT, padx=(0, 5))

        # 低频操作收进水平工具栏，让输入框成为清晰的视觉主体。
        if attach_button:
            low = attachments
            self._low_controls = low
            for text, tip, callback in (
                    ("上下文", "查看历史与附件，检查本轮实际发送内容", on_context),
                    ("⋯", "工具与命令", on_commands)):
                pill = rounded_label(low, text, fill=C["input_bg"], outline="",
                                     fg=C["muted"], font=FONT_MICRO,
                                     bg=C["input_bg"], tooltip=tip,
                                     command=callback, radius=R_PILL, padx=6,
                                     pady=2)
                pill.pack(side=tk.LEFT, padx=(0, 4))

        right = tk.Frame(bar, bg=C["input_bg"])
        right.grid(row=0, column=1, sticky="e")
        self._primary_controls = right
        self._toolbar_compact = None
        bar.bind("<Configure>", self._fit_toolbar)
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
                                        tooltip="Forge 任务的沉思配置；普通 gateway 对话不执行任务沉思")
        self.think_pill.pack(side=tk.LEFT, padx=(0, 8))

        # ⚙ 设置按钮（可选）
        if on_settings is not None:
            self.settings_btn = glyph_button(right, "⚙", on_settings, bg=C["input_bg"],
                                             fg=C["subtext"], size=11,
                                             hover=C["hover"], tooltip="设置")
            self.settings_btn.pack(side=tk.LEFT, padx=(0, 8))
        else:
            self.settings_btn = None

        # 发送 / 停止（同一物理位置，set_busy 切换）
        self.send_circle = circle_button(right, "↑", self._fire_send, size=30,
                                         kind="muted", bg=C["input_bg"],
                                         tooltip="发送（Enter）")
        self.send_circle.pack(side=tk.LEFT)
        self.stop_circle = circle_button(right, "■", self._fire_stop, size=30,
                                         kind="danger", bg=C["input_bg"],
                                         tooltip="停止生成")
        self._busy = False
        self.stop_circle.pack_forget()

        # 提示行（footer）—— 放到 InputCard 自带的 foot，不属于 inner card
        foot = tk.Frame(self, bg=base)
        foot.pack(fill=tk.X, pady=(6, 2))
        self.footer_left = tk.Label(foot, text=footer_left or "空闲", bg=base,
                                    fg=C["muted"], font=FONT_CAPTION)
        self.footer_left.pack(side=tk.LEFT)
        if footer_right is None:
            footer_right = "Enter ↵  ·  Shift+Enter 换行"
        self.footer_right = tk.Label(foot, text=footer_right, bg=base,
                                     fg=C["muted"], font=FONT_CAPTION)
        self.footer_right.pack(side=tk.RIGHT)

        self._sync_hint()
        self._sync_send_state()

    # -- 交互 --
    def _resize_entry(self):
        """按显示行增长，长输入保留内部滚动，不把时间线挤出屏幕。"""
        try:
            count = self.entry.count("1.0", "end", "displaylines")
            rows = max(2, min(7, int(count[0]) if count else 2))
            if int(self.entry.cget("height")) != rows:
                self.entry.configure(height=rows)
        except tk.TclError:
            pass

    def _set_focus(self, focused: bool):
        self._card.set_fill(C["input_bg"], C["accent"] if focused else C["border_hi"])
        self._sync_hint()

    def _fit_toolbar(self, event):
        low = self._low_controls
        if low is None:
            return
        need = low.winfo_reqwidth() + self._primary_controls.winfo_reqwidth() + 12
        compact = event.width < need
        if compact == self._toolbar_compact:
            return
        self._toolbar_compact = compact
        if compact:
            low.grid_configure(row=1, column=0, columnspan=2, pady=(6, 0))
            self._primary_controls.grid_configure(row=0, column=0, columnspan=2)
        else:
            low.grid_configure(row=0, column=0, columnspan=1, pady=0)
            self._primary_controls.grid_configure(row=0, column=1, columnspan=1)

    def _enter(self, event):
        if event.state & 1:                # Shift
            return None
        self._fire_send()
        return "break"

    def _text_changed(self, _event=None):
        if self.entry.edit_modified():
            if not self._syncing:
                self._syncing = True
                self.send_var.set(self.entry.get("1.0", "end-1c"))
                self.entry.edit_modified(False)
                self._syncing = False

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
        self._resize_entry()

    def _sync_send_state(self):
        if self._busy:
            return
        has_text = bool(self.send_var.get().strip())
        circle_button_state(self.send_circle, "primary" if has_text else "muted")

    def set_busy(self, busy: bool):
        self._busy = busy
        if busy:
            self.send_circle.pack_forget()
            self.stop_circle.pack(side=tk.LEFT)
        else:
            self.stop_circle.pack_forget()
            self.send_circle.pack(side=tk.LEFT)
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
