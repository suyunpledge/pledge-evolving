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

import i18n
from i18n import tr

import os
import re
import sys

try:
    from ime_inline import InlineIME
except Exception:  # pragma: no cover
    InlineIME = None
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
import math
from typing import Callable, Optional

import decor
import gui_theme as theme
from ui_icons import IconButton, IconCanvas, emoji_image, emoji_parts, draw_icon, icon_key
from gui_theme import (
    position_popover,
    C, FONT_CAPTION, FONT_MICRO, FONT_MONO, FONT_MONO_SM, FONT_MONO_XS, FONT_SECTION,
    FONT_SMALL, FONT_TITLE, FONT_UI, FONT_UI_BOLD, R_BUBBLE, R_CARD, R_MD, R_PILL,
    RoundedCard, attach_tooltip, avatar, badge, bind_keyboard_action, circle_button, circle_button_state,
    dot, emoji_font, glyph_button, highlight_python, round_rect, rounded_label,
    setup_code_tags, style_scrollbar, ui_px, text_width, bind_wrap, bind_scoped_wheel, flow_controls,
    _bg_of,
)

MAX_BUBBLE_WIDTH = 740          # 中央 Conversation 是主体，长文允许更宽的阅读行
USER_AUTOSIZE_PAD_X = 15
USER_AUTOSIZE_PAD_Y = 12

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

    def __init__(self, parent, *, bg=None, padx=0, pady=0, max_width=None):
        base = bg or C["chat"]
        super().__init__(parent, bg=base, highlightthickness=0, bd=0)
        self._bg = base
        self._max_width = max_width
        self.canvas = tk.Canvas(self, bg=base, highlightthickness=0, bd=0,
                                highlightcolor=base)
        self.vbar = tk.Scrollbar(self, orient=tk.VERTICAL, command=self._manual_scroll,
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
        bind_scoped_wheel(self.canvas, self.inner, self._on_wheel)
        self._bar_visible = True
        self._scroll_job = None
        self._scroll_follow_end = False
        self._follow_paused = False
        self.on_position_changed = None
        self._layout_job = None
        # An offscreen Canvas window can change its requested height without
        # resizing its Frame. Descendant Configure events still report the
        # content changes; coalesce them before reading the current Canvas bbox.
        self._layout_owner = self.winfo_toplevel()
        self._content_prefix = str(self.inner) + "."
        self._layout_binding = self._layout_owner.bind(
            "<Configure>", self._content_layout_changed, add="+")
        self.bind("<Destroy>", self._destroy_scroll, add="+")

    def _content_layout_changed(self, event):
        if str(event.widget).startswith(self._content_prefix) and self._layout_job is None:
            self._layout_job = self.after(16, self._settle_content_layout)

    def _settle_content_layout(self):
        self._layout_job = None
        self._sync_scrollregion()

    def _destroy_scroll(self, event):
        if event.widget is not self:
            return
        self._cancel_scroll()
        if self._layout_job is not None:
            self.after_cancel(self._layout_job)
            self._layout_job = None
        self._layout_owner.unbind("<Configure>", self._layout_binding)

    def _on_scroll(self, first, last):
        if float(last) - float(first) >= 0.999:
            if self._bar_visible:
                self.vbar.pack_forget()
                self._bar_visible = False
        elif not self._bar_visible:
            self.vbar.pack(side=tk.RIGHT, fill=tk.Y)
            self._bar_visible = True
        self.vbar.set(first, last)
        callback = self.on_position_changed
        if callback is not None:
            callback()

    def _sync_scrollregion(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        # Text/card geometry may finish after the first scroll callback. Keep
        # following those height changes until the user explicitly scrolls.
        if self._scroll_follow_end:
            self.canvas.yview_moveto(1.0)

    def _manual_scroll(self, *args):
        self._cancel_scroll()
        self.canvas.yview(*args)
        self._follow_paused = not self.at_bottom()

    def _sync_width(self, event):
        if self._max_width is None:
            width, left = event.width, 0
        else:
            width, left = theme.column_geometry(self, event.width, max_width=self._max_width)
        self.canvas.coords(self._win, left, 0)
        self.canvas.itemconfigure(self._win, width=width)

    def _on_wheel(self, event):
        self._cancel_scroll()
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        self._follow_paused = not self.at_bottom()
        return "break"

    def _bottom_distance(self):
        # Fractions grow with transcript length; measure the actual remaining
        # Canvas pixels so a long history cannot turn several screens into "near".
        # 2026-10-11 性能修复：bbox("all") 是 O(全部 item)，流式期间每个
        # token 都会经 _on_scroll → at_bottom 走到这里，长对话直接卡死。
        # 用 canvas.cget("scrollregion")（布局时 _sync_scrollregion 已写好，
        # 读取是 O(1)）代替即时重算。
        try:
            region = self.canvas.cget("scrollregion")
        except tk.TclError:
            return 0
        parts = str(region).split()
        if len(parts) != 4:
            return 0
        try:
            total_height = float(parts[3]) - float(parts[1])
        except ValueError:
            return 0
        return max(0, (1 - self.canvas.yview()[1]) * total_height)

    def at_bottom(self) -> bool:
        try:
            return self._bottom_distance() <= ui_px(self, 4)
        except tk.TclError:
            return True

    def scroll_to_end(self):
        # Streaming deltas can arrive faster than layout; merge their scrolls
        # instead of running the whole application's idle queue for every token.
        self._scroll_follow_end = True
        self._follow_paused = False
        if self._scroll_job is None:
            # Geometry cascades through Text -> card -> Canvas across several
            # idle passes. A frame delay lets those settle without nesting Tk.
            self._scroll_job = self.after(16, self._scroll_after_layout)

    def _scroll_after_layout(self):
        self._scroll_job = None
        if self.winfo_exists():
            self._sync_scrollregion()
            self.canvas.yview_moveto(1.0)

    def _cancel_scroll(self, event=None):
        if event is not None and event.widget is not self:
            return
        self._scroll_follow_end = False
        if self._scroll_job is not None:
            self.after_cancel(self._scroll_job)
            self._scroll_job = None

    def near_bottom(self) -> bool:
        try:
            return not self._follow_paused and (
                self._scroll_follow_end or self._bottom_distance() <= ui_px(self, 64))
        except tk.TclError:
            return True


# ─── 行内富文本 ────────────────────────────────────────────


class InlineText(tk.Text):
    """自动高度的只读 Text：支持 **加粗**、`行内代码` 与可点击文件链接。"""

    def __init__(self, parent, *, bg=None, fg=None, font=None, width=None,
                 wrap=tk.WORD, on_link=None):
        base = bg or C["chat"]
        super().__init__(parent, wrap=wrap, bg=base, fg=fg or C["body"],
                         font=font or theme.FONT_CHAT, relief=tk.FLAT, bd=0,
                         highlightthickness=0, padx=0, pady=0, height=1,
                         cursor="arrow", insertwidth=0, spacing1=ui_px(parent, 2),
                         spacing2=ui_px(parent, 2), spacing3=ui_px(parent, 3),
                         selectbackground=C["accent_soft"])
        self._measure_font = tkfont.Font(root=self, font=self.cget("font"))
        # Font descent supplies the optical inset at every DPI. Width and
        # wrapping still come from the allocated parent, never a fixed gutter.
        self.configure(padx=max(1, self._measure_font.metrics("descent")), pady=0)
        self.tag_configure("b", font=theme.FONT_CHAT_BOLD, foreground=C["text"])
        self.tag_configure("code", font=FONT_MONO_SM, background=C["code_bg"],
                           foreground=C["code_str"])
        self.tag_configure("muted", foreground=C["subtext"])
        self.tag_configure("file_link", foreground=C["accent2"],
                           underline=True, font=font or theme.FONT_CHAT)
        self._base_bg = base
        self._on_link = on_link
        self._emoji_names = {}
        self._emoji_references = []
        self._recompute_job = None
        self.bind("<Control-c>", self._copy_selection)
        self.bind("<<Copy>>", self._copy_selection)
        if width:
            self.configure(width=width)
        self.bind("<Configure>", self._fit_height)
        self.bind("<Destroy>", self._cancel_recompute, add="+")
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
        self._schedule_recompute()

    def _schedule_recompute(self):
        if self._recompute_job is None:
            self._recompute_job = self.after_idle(self._recompute)

    def _cancel_recompute(self, event=None):
        if event is not None and event.widget is not self:
            return
        if self._recompute_job is not None:
            try:
                self.after_cancel(self._recompute_job)
            except tk.TclError:
                pass
            self._recompute_job = None

    def _recompute(self):
        self._recompute_job = None
        try:
            if not self.winfo_exists():
                return
            pixels = int(self.tk.call(self._w, "count", "-update", "-ypixels", "1.0", "end"))
            line_height = self._measure_font.metrics("linespace")
            n = max(1, math.ceil(pixels / max(1, line_height)))
            if n != self._last_h:
                self._last_h = n
                self.configure(height=n)
        except tk.TclError:
            pass

    def set_segments(self, segs):
        """segs 是 (text, tag|None|iterable) 列表，自动识别文件链接。"""
        self.configure(state=tk.NORMAL)
        self.delete("1.0", tk.END)
        self._emoji_names.clear()
        self._emoji_references.clear()
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
                self._insert_display(piece, chosen_tag)
        self.configure(state=tk.DISABLED)
        self._last_h = -1
        self._schedule_recompute()

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
                self._insert_display(piece, chosen_tag)
        self.configure(state=tk.DISABLED)
        self._last_h = -1
        self._schedule_recompute()

    def _insert_display(self, text, tags):
        # Keep file links and inline code as literal text; their indices/meaning
        # must not be changed by an image renderer.
        if "code" in tags or "file_link" in tags:
            self.insert(tk.END, text, tags)
            return
        self.tag_configure("emoji_text", font=emoji_font("😀", 11))
        for part, is_emoji in emoji_parts(text):
            image = emoji_image(self, part, size=self._measure_font.metrics("linespace")) if is_emoji else None
            if image is None:
                self.insert(tk.END, part, tags + (("emoji_text",) if is_emoji else ()))
            else:
                name = self.image_create("end-1c", image=image, align="center",
                                         name=f"emoji-{len(self._emoji_names)}",
                                         padx=0)
                self._emoji_names[name] = part
                self._emoji_references.append(image)

    def display_text(self, start="1.0", end="end-1c"):
        """Recover original Unicode, including emoji represented by images."""
        return "".join(value if kind == "text" else self._emoji_names.get(value, "")
                       for kind, value, _index in self.dump(start, end, text=True, image=True))

    def _copy_selection(self, _event=None):
        try:
            text = self.display_text("sel.first", "sel.last")
        except tk.TclError:
            return "break"
        self.clipboard_clear()
        self.clipboard_append(text)
        return "break"

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
        if ('|' in line and i + 1 < len(lines) and
                re.fullmatch(r'\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*', lines[i+1])):
            flush()
            table = [line, lines[i+1]]
            i += 2
            while i < len(lines) and lines[i].strip() and '|' in lines[i]:
                table.append(lines[i])
                i += 1
            blocks.append({'type':'table', 'text':'\n'.join(table)})
            continue
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
    """把 markdown 轻量渲染成一叠控件，返回承载它们的 Frame。

    同步一次建全；长文请用 render_blocks_chunked（分批、不堵主线程）。
    块级渲染逻辑只在 _render_single_block 一处，两个入口共享。
    """
    base = bg or C["chat"]
    host = tk.Frame(parent, bg=base)
    wrap = max_width or MAX_BUBBLE_WIDTH
    for block in parse_blocks(text):
        _render_single_block(host, block, base=base, wrap=wrap)
    return host


def _render_single_block(host, block, *, base, wrap):
    """渲染单个 markdown 块（由 render_blocks 循环体抽取，行为一致）。"""
    kind = block["type"]
    if kind == "h":
        f = theme.FONT_MARKDOWN_HEADING if block["level"] <= 2 else theme.FONT_MARKDOWN_SUBHEAD
        if any(is_emoji for _part, is_emoji in emoji_parts(block["text"])):
            heading = InlineText(host, bg=base, fg=C["text"], font=f)
            heading.set_text(block["text"])
        else:
            heading = i18n.Label(host, text=block["text"], bg=base, fg=C["text"], font=f,
                               anchor="w", justify=tk.LEFT, wraplength=wrap)
        heading.pack(fill=tk.X, pady=(ui_px(host, 10), ui_px(host, 4)))
        if isinstance(heading, i18n.Label):
            bind_wrap(heading)
    elif kind == "p":
        t = InlineText(host, bg=base)
        t.set_segments(inline_segments(block["text"]))
        t.pack(fill=tk.X, pady=2)
    elif kind == "li":
        row = tk.Frame(host, bg=base)
        row.pack(fill=tk.X, pady=2)
        # 圆点与首行文字垂直居中（Label 居中对齐会在多行条目里漂到中间）。
        line = tkfont.Font(root=row, font=theme.FONT_CHAT).metrics("linespace")
        size = max(5, ui_px(row, 5))
        gutter = ui_px(row, 18)
        bullet = tk.Canvas(row, width=gutter, height=line, bg=base, highlightthickness=0, bd=0)
        top = (line - size) / 2
        left = ui_px(row, 5)
        bullet.create_oval(left, top, left + size, top + size, fill=C["accent2"], outline="")
        bullet.pack(side=tk.LEFT, anchor="n")
        t = InlineText(row, bg=base)
        t.set_segments(inline_segments(block["text"]))
        t.pack(side=tk.LEFT, anchor="n", fill=tk.X, expand=True)
    elif kind == "oli":
        row = tk.Frame(host, bg=base)
        row.pack(fill=tk.X, pady=2)
        line = tkfont.Font(root=row, font=theme.FONT_CHAT).metrics("linespace")
        size = min(line, ui_px(row, 18))
        num = tk.Canvas(row, width=size, height=line, bg=base,
                        highlightthickness=0, bd=0)
        top = (line - size) / 2
        num.create_oval(0.5, top + .5, size - 1, top + size - 1, fill=C["accent_soft"],
                        outline=theme.mix(C["accent_soft"], C["accent2"], .45))
        num.create_text(size / 2, line / 2, text=block["num"], fill=C["accent_text"],
                        font=FONT_MICRO)
        num.pack(side=tk.LEFT, anchor="n", padx=(0, 8))
        t = InlineText(row, bg=base)
        t.set_segments(inline_segments(block["text"]))
        t.pack(side=tk.LEFT, anchor="n", fill=tk.X, expand=True)
    elif kind == "todo":
        row = tk.Frame(host, bg=base)
        row.pack(fill=tk.X, pady=1)
        mark = "✅" if block["done"] else "⬜"
        color = C["ok"] if block["done"] else C["muted"]
        IconCanvas(row, "check_circle" if block["done"] else "stop", size=18,
                   bg=base, fg=color).pack(side=tk.LEFT, padx=(0, 8))
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
    elif kind in ("code", "table"):
        # 代码块不识别文件链接：用不签名 InlineText 的 _on_link 的 Text 直接渲染。
        card = RoundedCard(host, radius=R_MD, fill=C["code_bg"],
                           outline=C["border"], padx=10, pady=8, bg=base)
        card.pack(fill=tk.X, pady=6)
        toolbar = tk.Frame(card.content, bg=C['code_bg'])
        toolbar.pack(fill=tk.X, pady=(0,6))
        lang = block.get('lang') or ('Markdown' if kind == 'table' else 'Code')
        i18n.Label(toolbar, text=lang, bg=C['code_bg'], fg=C['muted'],
                   font=FONT_CAPTION).pack(side=tk.LEFT)
        def copy_source():
            card.clipboard_clear()
            card.clipboard_append(block['text'])
        card._copy_button = theme.pill_button(toolbar, tr('复制'), copy_source,
                                               kind='quiet', bg=C['code_bg'], icon='⧉')
        card._copy_button.pack(side=tk.RIGHT)
        tx = tk.Text(card.content, bg=C["code_bg"], fg=C["code_plain"],
                     font=FONT_MONO_SM, relief=tk.FLAT, bd=0,
                     highlightthickness=0, wrap=tk.NONE, height=1)
        setup_code_tags(tx, font=FONT_MONO_SM)
        if kind == 'table':
            rows = [[cell.strip().replace(r'\|','|') for cell in re.split(r'(?<!\\)\|', line.strip().strip('|'))]
                    for index,line in enumerate(block['text'].splitlines()) if index != 1]
            columns = max(len(row) for row in rows)
            heading_font = (theme.MONO_FAMILY, FONT_MONO_SM[1], 'bold')
            measure = tkfont.Font(root=tx, font=heading_font)
            widths = [max(measure.measure(row[col]) if col < len(row) else 0 for row in rows)
                      + ui_px(tx,24) for col in range(columns)]
            positions, position = [], 0
            for width in widths:
                position += width
                positions.append(position)
            tx.configure(tabs=tuple(positions))
            tx.insert('1.0', '\n'.join('\t'.join(row) for row in rows))
            tx.tag_configure('table_heading', font=heading_font,
                             foreground=C['text'], background=C['surface'])
            tx.tag_add('table_heading','1.0','1.end')
        else:
            tx.insert("1.0", block["text"])
        if kind == 'code' and (lang in ("Code", "py", "python") or lang.startswith("py")):
            try:
                highlight_python(tx)
            except Exception:
                pass
        line_count = max(1, len(tx.get('1.0','end-1c').split('\n')))
        tx.configure(height=min(line_count, 24), state=tk.DISABLED)
        # 设计稿：原生 tk.Scrollbar 在深色界面里是一条白轨道；改用主题 ttk 滚动条。
        tall = line_count > 24
        sx = ttk.Scrollbar(card.content, orient=tk.HORIZONTAL, command=tx.xview)
        def sync_x(first, last):
            sx.set(first, last)
            if float(first) > 0 or float(last) < 1:
                if not sx.winfo_manager():
                    sx.pack(side=tk.BOTTOM, fill=tk.X, before=tx)
            elif sx.winfo_manager():
                sx.pack_forget()
        tx.configure(xscrollcommand=sync_x)
        if tall:
            sy = ttk.Scrollbar(card.content, orient=tk.VERTICAL, command=tx.yview)
            tx.configure(yscrollcommand=sy.set)
            sy.pack(side=tk.RIGHT, fill=tk.Y)
        tx.pack(fill=tk.BOTH, expand=True)
    elif kind == "hr":
        tk.Frame(host, bg=C["border"], height=1).pack(fill=tk.X, pady=6)


def render_blocks_chunked(parent, text, *, bg=None, max_width=None,
                          on_done=None) -> tk.Frame:
    """分批渲染 markdown：每块之间 after(1) 让出事件循环。

    长回复不再把 Tk 主线程堵过 Windows「未响应」阈值（约 5s），
    每块渲染后窗口保持可响应。on_done(host) 全部完成后回调（可无）。
    """
    base = bg or C["chat"]
    host = tk.Frame(parent, bg=base)
    blocks = parse_blocks(text)
    wrap = max_width or MAX_BUBBLE_WIDTH
    total = len(blocks)
    pending = None

    def cancel(event):
        nonlocal pending
        if event.widget is host and pending is not None:
            host.after_cancel(pending)
            pending = None

    host.bind("<Destroy>", cancel, add="+")

    def build_one(i: int):
        nonlocal pending
        pending = None
        if not host.winfo_exists():
            return
        if i >= total:
            if on_done is not None:
                try:
                    on_done(host)
                except Exception:
                    pass
            return
        _render_single_block(host, blocks[i], base=base, wrap=wrap)
        pending = host.after(1, lambda: build_one(i + 1))

    build_one(0)
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


class _GlassFolder:
    """折叠块的液态玻璃外观（ToolCard / StepList 共用）。

    收起时：一枚玻璃胶囊「文件夹标签」（箭头 + 图标 + 摘要），悬停 / 聚焦
    变亮。展开时：标签下方弹出一块浮起的玻璃面板，像 iOS 文件夹那样带
    弹簧过冲地长开，内容在玻璃基本到位后出现；收起立即完成。
    """

    def _build_tab(self, base, *, icon=None):
        import liquid_glass as glass
        px = lambda value: ui_px(self, value)
        self._tab_fill = C["glass_chip"]
        self._tab_hover = C["glass_chip_hover"]
        self._summary = tk.Frame(self, bg=base, cursor="hand2", padx=px(11), pady=px(5))
        self._summary.pack(anchor="w", pady=(8, 2))
        self._tab_glass = glass.GlassBackdrop(self._summary)
        self._tab_specs = {
            False: glass.chip_spec(self._tab_fill, radius=px(15), rim=.7),
            True: glass.chip_spec(self._tab_hover, radius=px(15), rim=1.0),
        }
        self._tab_glass.show(self._tab_specs[False])
        bind_keyboard_action(self._summary, self.toggle)
        self._summary.configure(highlightthickness=0)
        fill = self._tab_fill
        self._arrow = IconCanvas(self._summary, "chevron_down" if self._expanded else "chevron_right",
                                 size=px(14), bg=fill, fg=C["subtext"])
        self._arrow.pack(side=tk.LEFT, padx=(0, px(4)))
        self._tab_widgets = [self._arrow]
        if icon:
            mark = IconCanvas(self._summary, icon, size=px(14), bg=fill, fg=C["accent"])
            mark.pack(side=tk.LEFT, padx=(0, px(6)))
            self._tab_widgets.append(mark)
        self._summary_label = i18n.Label(self._summary, text="", bg=fill,
                                       fg=C["body"], font=FONT_CAPTION,
                                       cursor="hand2", anchor="w", justify=tk.LEFT,
                                       wraplength=ui_px(self, MAX_BUBBLE_WIDTH))
        self._summary_label.pack(side=tk.LEFT)
        self._tab_widgets.append(self._summary_label)
        for widget in (self._summary, *self._tab_widgets):
            widget.bind("<Button-1>", lambda _e: self.toggle())
            widget.bind("<Enter>", lambda _e: self._paint_tab(True), add="+")
            widget.bind("<Leave>", lambda _e: self._paint_tab(self._summary.focus_get() is self._summary),
                        add="+")
        self._summary.bind("<FocusIn>", lambda _e: self._paint_tab(True), add="+")
        self._summary.bind("<FocusOut>", lambda _e: self._paint_tab(False), add="+")
        self.bind("<Configure>", self._fit_tab, add="+")

    def _fit_tab(self, _event=None):
        chrome = sum(w.winfo_reqwidth() for w in self._tab_widgets
                     if w is not self._summary_label) + ui_px(self, 38)
        width = max(ui_px(self, 40), self.winfo_width() - chrome)
        if int(self._summary_label.cget("wraplength")) != width:
            self._summary_label.configure(wraplength=width)


    def _paint_tab(self, active):
        spec = self._tab_specs[bool(active)]
        try:
            self._tab_glass.show(spec)
            for widget in self._tab_widgets:
                widget.configure(bg=spec.fill)
        except tk.TclError:
            pass

    def _build_folder(self, base, *, padx=12, pady=10):
        import liquid_glass as glass
        spec = glass.folder_spec(base, radius=ui_px(self, 16), shadow=ui_px(self, 8))
        panel = glass.GlassPanel(self, spec=spec, padx=ui_px(self, padx),
                                 pady=ui_px(self, pady), bg=base)
        return panel, spec.fill

    def _set_folder(self, panel, expanded, *, animate=True, pady=(0, 6)):
        self._arrow.configure(text="▾" if expanded else "▸")
        if expanded:
            panel.pack(fill=tk.X, pady=pady)
            if animate:
                panel.unfold()
        else:
            panel.fold()
            panel.pack_forget()


class ToolCard(_GlassFolder, tk.Frame):
    """「调用工具 (n)」玻璃文件夹：默认收起成一枚胶囊标签，点开弹出明细面板。"""

    def __init__(self, parent, *, title="调用工具", rows=None, bg=None,
                 expanded=False):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        self._expanded = bool(expanded)
        self._title_text = title

        # 摘要：玻璃胶囊标签（永远可见）
        self._build_tab(base, icon="timeline")

        # 明细：浮起的玻璃面板（按需显示）
        self._rail_lines = []
        card, self._card_fill = self._build_folder(base, padx=12, pady=10)
        self._card = card
        self._card_host = card  # alias
        self.rows_frame = tk.Frame(card.content, bg=self._card_fill)
        self.rows_frame.pack(fill=tk.X)
        self._row_specs: list[tuple[str, float]] = []
        rows = rows or []
        # 分批建行：几百步的任务返回不再一帧全建（白屏/未响应根因之一）
        self._pending_rows = list(rows)
        self._row_batch_job = None
        self.bind("<Destroy>", self._cancel_row_batch, add="+")
        self._kick_row_batch()

        if self._expanded:
            self._set_folder(card, True, animate=False)
        # 计算摘要文字（用全量行数，不等分批完成）
        self._all_row_count = len(rows)
        self._refresh_summary()

    def _kick_row_batch(self):
        if self._row_batch_job is not None or not self._pending_rows:
            return
        def batch():
            self._row_batch_job = None
            if not self.winfo_exists():
                return
            for _ in range(12):            # 每帧 12 行
                if not self._pending_rows:
                    break
                self.add_row(self._pending_rows.pop(0))
            self._refresh_summary()
            if self._pending_rows:
                self._row_batch_job = self.after(1, batch)
        self._row_batch_job = self.after(1, batch)

    def _cancel_row_batch(self, event=None):
        if event is not None and event.widget is not self:
            return
        if self._row_batch_job is not None:
            try:
                self.after_cancel(self._row_batch_job)
            except tk.TclError:
                pass
            self._row_batch_job = None

    # -- 摘要 --
    def _refresh_summary(self):
        n = getattr(self, "_all_row_count", None)
        if n is None:
            n = len(self._row_specs)
        secs = sum(s for _, s in self._row_specs)
        # 尊重 title（如「子 Agent 分工」「Agent 集群」），默认仍是「调用工具」
        text = f"{getattr(self, '_title_text', None) or '调用工具'} · {n} 项"
        if secs > 0:
            if secs >= 10:
                text += f" · {secs:.0f}s"
            else:
                text += f" · {secs:.1f}s"
        self._summary_label.configure(text=text)

    # -- 折叠 --
    def toggle(self):
        self._expanded = not self._expanded
        self._set_folder(self._card, self._expanded)

    def add_row(self, row: dict):
        bgc = self._card_fill
        wrap = tk.Frame(self.rows_frame, bg=bgc)
        wrap.pack(fill=tk.X, pady=1)
        ok = row.get("ok", True)
        mark = "✅" if ok else "⚠️"
        IconCanvas(wrap, "check_circle" if ok else "warning", size=18, bg=bgc,
                   fg=C["ok"] if ok else C["warn"]).pack(side=tk.LEFT, padx=(0, 6))
        # Reserve timing before giving the remaining width to the text column.
        if row.get("elapsed"):
            i18n.Label(wrap, text=str(row["elapsed"]), bg=bgc, fg=C["muted"],
                       font=FONT_MONO_SM).pack(side=tk.RIGHT, anchor="n", padx=(8, 0))
        text_box = tk.Frame(wrap, bg=bgc)
        text_box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        name = i18n.Label(text_box, text=row.get("name", ""), bg=bgc, fg=C["accent2"],
                         font=FONT_MONO_SM, anchor="w", justify=tk.LEFT,
                         wraplength=ui_px(self, MAX_BUBBLE_WIDTH))
        name.pack(fill=tk.X)
        bind_wrap(name)
        desc = row.get("desc")
        if desc:
            description = i18n.Label(text_box, text=desc, bg=bgc, fg=C["ter"], font=FONT_SMALL,
                                     anchor="w", justify=tk.LEFT, wraplength=ui_px(self, MAX_BUBBLE_WIDTH))
            description.pack(fill=tk.X)
            bind_wrap(description)
        if row.get("detail"):
            attach_tooltip(wrap, row["detail"])
            for child in [*wrap.winfo_children(), *text_box.winfo_children()]:
                attach_tooltip(child, row["detail"])
        # 记录 (label, seconds) 以便摘要显示耗时
        self._row_specs.append((row.get("name", ""), _guess_seconds_from_row(row)))


class StepList(_GlassFolder, tk.Frame):
    """执行步骤玻璃文件夹（默认收起，标签显示总数）。"""

    def __init__(self, parent, items, *, bg=None, expanded=False):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        self._expanded = bool(expanded)
        self._items = items or []

        self._build_tab(base, icon="task")
        n = len(self._items)
        self._summary_label.configure(text=tr("执行步骤 {count} 步", count=n))

        # 明细：玻璃面板
        self._detail, self._detail_fill = self._build_folder(base, padx=12, pady=8)
        if self._expanded:
            self._set_folder(self._detail, True, animate=False)
        for item in self._items:
            self._add_detail_row(item)

    def _add_detail_row(self, item: dict):
        base = self._detail_fill
        row = tk.Frame(self._detail.content, bg=base)
        row.pack(fill=tk.X, pady=3)
        idx = tk.Canvas(row, width=18, height=18, bg=base, highlightthickness=0, bd=0)
        idx.create_oval(0, 0, 17, 17, fill=C["sel"], outline=C["sel_border"])
        idx.create_text(9, 9, text=str(item.get("index", "")), fill=C["subtext"],
                        font=FONT_MICRO)
        idx.pack(side=tk.LEFT, anchor="n", padx=(0, 10))
        right = tk.Frame(row, bg=base)
        right.pack(side=tk.RIGHT, anchor="n", padx=(8, 0))
        text_box = tk.Frame(row, bg=base)
        text_box.pack(side=tk.LEFT, fill=tk.X, expand=True)
        title = i18n.Label(text_box, text=item.get("title", ""), bg=base, fg=C["text"],
                          font=FONT_UI_BOLD, anchor="w", justify=tk.LEFT,
                          wraplength=ui_px(self, MAX_BUBBLE_WIDTH))
        title.pack(fill=tk.X)
        bind_wrap(title)
        desc = item.get("desc")
        if desc:
            description = i18n.Label(text_box, text=desc, bg=base, fg=C["ter"], font=FONT_SMALL,
                                     anchor="w", justify=tk.LEFT, wraplength=ui_px(self, MAX_BUBBLE_WIDTH))
            description.pack(fill=tk.X)
            bind_wrap(description)
        if item.get("elapsed"):
            i18n.Label(right, text=str(item["elapsed"]), bg=base, fg=C["muted"],
                     font=FONT_MONO_SM).pack(side=tk.LEFT, padx=(0, 8))
        if item.get("done", True):
            chk = tk.Canvas(right, width=14, height=14, bg=base,
                            highlightthickness=0, bd=0)
            chk.create_oval(0, 0, 13, 13, fill=C["ok"], outline="")
            draw_icon(chk, "check", size=14, fg="#0B0B10")
            chk.pack(side=tk.LEFT)

    def toggle(self):
        self._expanded = not self._expanded
        self._set_folder(self._detail, self._expanded)


class ActionRow(tk.Frame):
    """消息底部动作按钮组（紧凑，不横贯整行）。

    旧版是整条横贯的边框条，视觉上像通知栏/坏掉的输入框。改为紧凑
    按钮组：primary 有底色，其余 ghost，整组靠左、宽度贴合内容。
    """

    def __init__(self, parent, actions, *, bg=None):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        self._buttons = []
        for action in actions:
            kind = action.get("kind", "ghost")
            label = action.get("label", "")
            if label == tr("打开工作区"):
                label = "📁 打开工作区"
            btn = IconButton(self, text=label,
                            command=action.get("command"),
                            bg=C["accent"] if kind == "primary" else C["surface2"],
                            fg="#FFFFFF" if kind == "primary" else C["body"],
                            activebackground=(C["accent_hover"] if kind == "primary"
                                              else C["hover"]),
                            activeforeground=("#FFFFFF" if kind == "primary"
                                              else C["text"]),
                            font=FONT_SMALL, relief=tk.FLAT, bd=0, padx=12, pady=4,
                            cursor="hand2", highlightthickness=0,
                            overrelief=tk.FLAT)
            self._buttons.append(btn)
            btn.grid(row=0, column=len(self._buttons)-1, sticky="w", padx=(0, 8), pady=2)
        self.bind("<Configure>", self._flow)

    def _flow(self, event):
        row, column, used = 0, 0, 0
        for button in self._buttons:
            size = button.winfo_reqwidth() + 8
            if used and used + size > event.width:
                row, column, used = row + 1, 0, 0
            button.grid(row=row, column=column, sticky="w", padx=(0, 8), pady=2)
            used += size
            column += 1


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
        self.label = i18n.Label(self, text="", bg=bg, fg=C["muted"],
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
        self.label.configure(text=text or "")
        if not text:
            self.pack_forget()
            self._on_destroy()
            return
        if not self.winfo_manager():
            self.pack(side=tk.RIGHT)
        lowered = str(text).lower()
        if any(word in lowered for word in self.ACTIVE_WORDS):
            self._tick()
        else:
            self._on_destroy()
            color = C["error"] if any(w in lowered for w in (tr("失败"), tr("错误"))) else C["ok"]
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


class EmojiLabel(InlineText):
    """Read-only text bubble with inline emoji and Label-compatible text/wrapping."""
    def __init__(self, parent, *, text, wraplength, **kwargs):
        self._source_text, self._wraplength = text, wraplength
        self._measure_font = tkfont.Font(root=parent, font=FONT_UI)
        super().__init__(parent, width=self._text_width(parent), **kwargs)
        self.set_text(text)

    def _text_width(self, master):
        pixels = text_width(master, self._source_text)
        pixels = min(self._wraplength, max(24, pixels))
        return max(2, math.ceil(pixels / max(1, self._measure_font.measure("0"))))

    def cget(self, key):
        if key == "text":
            return self._source_text
        if key == "wraplength":
            return self._wraplength
        return super().cget(key)

    def configure(self, cnf=None, **kwargs):
        if "wraplength" in kwargs:
            self._wraplength = kwargs.pop("wraplength")
            kwargs["width"] = self._text_width(self)
        return super().configure(cnf, **kwargs) if cnf is not None else super().configure(**kwargs)

    config = configure


class UserMessage(tk.Frame):
    """右侧气泡用户消息（已修复 1px 压扁 bug：autosize_width=True）。

    整行容器 fill=tk.X；气泡用 anchor="e" 靠右；头像+名+时间是同一行。
    """

    def __init__(self, parent, text, *, bg=None, ts=None, name="你",
                 attachment_names=(), view_attachments=None):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)

        # head 行：名字 + 时间 靠右（以便头像与气泡视觉对齐）
        head = tk.Frame(self, bg=base)
        head.pack(anchor="e")
        avatar(head, size=ui_px(self, 26), glyph="你", fill="#2A2A38", shape="circle",
               bg=base).pack(side=tk.LEFT, padx=(0, ui_px(self, 8)))
        i18n.Label(head, text=name, bg=base, fg=C["subtext"], font=FONT_CAPTION,
                 anchor="e").pack(side=tk.LEFT)
        i18n.Label(head, text=ts or time.strftime("%H:%M"), bg=base, fg=C["muted"],
                 font=FONT_CAPTION).pack(side=tk.LEFT, padx=(ui_px(self, 8), 0))

        # 气泡（autosize_width=True 修历史 bug）
        bubble_host = tk.Frame(self, bg=base)
        bubble_host.pack(fill=tk.X, anchor="e", pady=(4, 0))
        card = RoundedCard(bubble_host, radius=R_BUBBLE, fill=C["msg_user_bg"],
                           outline=C["msg_user_border"],
                           padx=ui_px(self, USER_AUTOSIZE_PAD_X), pady=ui_px(self, USER_AUTOSIZE_PAD_Y),
                           bg=base, autosize_width=True)
        card.pack(anchor="e")
        self._card = card
        # 文字 label：撑开气泡。wraplength 设上限，让长文本真的换行
        max_text = ui_px(self, MAX_BUBBLE_WIDTH) - card._padx * 2
        if any(is_emoji for _part, is_emoji in emoji_parts(text)):
            self.label = EmojiLabel(card.content, text=text, bg=C["msg_user_bg"],
                                    fg=C["msg_user_fg"], wraplength=max_text)
        else:
            self.label = i18n.Label(card.content, text=text, bg=C["msg_user_bg"],
                                  fg=C["msg_user_fg"], font=theme.FONT_CHAT, justify=tk.LEFT,
                                  anchor="w", wraplength=max_text, bd=0,
                                  highlightthickness=0, padx=0, pady=0)
        self.label.pack(anchor="w")
        self.attachment_button = None
        if attachment_names and view_attachments is not None:
            self.attachment_button = theme.pill_button(
                self, tr("查看附件（{count}）", count=len(attachment_names)), view_attachments,
                kind="quiet", icon="📄")
            self.attachment_button.pack(anchor="e", pady=(ui_px(self, 4), 0))
            attach_tooltip(self.attachment_button, "\n".join(attachment_names[:8]))
        self.bind("<Configure>", self._fit_bubble)

    def _fit_bubble(self, event):
        # Workspace 打开后行宽变小，气泡必须重新换行，而不是裁掉正文。
        available = max(1, event.width - self._card._padx * 2)
        wrap = min(ui_px(self, MAX_BUBBLE_WIDTH) - self._card._padx * 2, available)
        self._card.set_content_width(limit=wrap)
        self.label.configure(wraplength=wrap)


def copy_to_clipboard(widget, source_getter):
    """通用复制：从 source_getter() 取文本，写入系统剪贴板，按钮短暂变 ✓。"""
    try:
        text_val = str(source_getter() or "")
        if not text_val.strip():
            return
        widget.clipboard_clear()
        widget.clipboard_append(text_val)
        orig = widget.cget("text")
        widget.configure(text="✓")
        widget.after(1200, lambda: widget.configure(text=orig))
    except Exception:
        pass


class AgentMessage(tk.Frame):
    """Forge 的回复：头像 + 名字 + 角色徽章 + 正文 + trace（可折叠）。"""

    def __init__(self, parent, *, app=None, bg=None, name="Forge", role=None,
                 ts=None, glyph="F", subtitle=None):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        self._base = base
        # Document-style replies share the transcript background. Retain the
        # existing card's measured geometry and public attributes for callers.
        self._bg = base
        self._max_width = MAX_BUBBLE_WIDTH
        self._app = app          # 给 ghost 操作条的重生成/赞踩用
        self._action_row = None
        self._text_source = ""
        self._has_details = False

        # head 行：avatar + 名字 + 时间(降权) + 角色徽章 + 状态（右对齐）
        head = tk.Frame(self, bg=base)
        head.pack(fill=tk.X)
        self._head = head
        self._avatar = avatar(head, size=ui_px(self, 28), glyph=glyph, fill=C["accent"], shape="rounded",
                              bg=base, image=_BRAND_AVATAR)
        self._avatar.pack(side=tk.LEFT, padx=(0, ui_px(self, 8)))
        i18n.Label(head, text=name, bg=base, fg=C["text"], font=FONT_UI_BOLD).pack(side=tk.LEFT)
        i18n.Label(head, text=ts or time.strftime("%H:%M"), bg=base, fg=C["muted"],
                 font=FONT_CAPTION).pack(side=tk.LEFT, padx=(8, 0))
        self._role_badge = None
        if role:
            self._role_badge = badge(head, f"⚡ {role}", tone="accent_soft", bg=base)
            self._role_badge.pack(side=tk.LEFT, padx=(8, 0))
        self._status_indicator = AgentStatusIndicator(head, bg=base)
        self._status = self._status_indicator.label  # 保留旧测试/调用方可见属性
        self.subtitle = None
        if subtitle:
            self.subtitle = i18n.Label(self, text=subtitle, bg=base, fg=C["ter"],
                                     font=FONT_SMALL, anchor="w", justify=tk.LEFT,
                                     wraplength=MAX_BUBBLE_WIDTH)
            # 与名字、正文同一条左对齐线（头像 28 + 间距 8）。
            self.subtitle.pack(fill=tk.X, padx=(ui_px(self, 36), 0), pady=(2, 0))

        # 正文气孔：圆角卡 + hairline 描边，跟用户侧对称。整体宽度跟随对话列，
        # 这样代码块/工具卡/表格都能拿到完整宽度（不为了「抱得紧」把内容压窄）。
        self._bubble_host = tk.Frame(self, bg=base)
        self._bubble_host.pack(fill=tk.X, anchor="w", padx=(ui_px(self, 36), 0), pady=(6, 0))
        self._bubble = RoundedCard(self._bubble_host, radius=0,
                                   fill=self._bg, outline="",
                                   padx=0, pady=ui_px(self, 4), bg=base, autosize_width=True)
        # 跟随列宽（fill=X）而不是抱紧内容：里面的 markdown 标签带 wraplength 上限，
        # 列窄时靠 fill=X 让它们重新折行，不会横向溢出。
        self._bubble.pack(anchor="w")
        self.body = tk.Frame(self._bubble.content, bg=self._bg)
        self.body.pack(fill=tk.X)
        # RoundedCard 靠 content 的 <Configure> 反推高度；正文是后来才填进去的，
        # 在 fill=X 模式下这条链会断（画布高度停在 1，内容不 mapped）。
        # 这里由 body/host 直接驱动高度，不依赖那条隐式链。
        self._bubble_host.bind("<Configure>", self._fit_content)
        self.body.bind("<Configure>", self._sync_bubble_height)
        self._stream = None
        # 流式占位：正文为空时把状态（生成中…/连接中等）显示在气孔内，
        # 首段文本到达即让位（_teardown_placeholder）。head 行的指示器保留
        # 给终态（完成/失败），避免同一信息显示两遍。
        self._placeholder_label = None
        self._placeholder_shown = False

    def _fit_content(self, _event=None):
        available = max(1, self._bubble_host.winfo_width() - self._bubble._padx * 2)
        # The bounded transcript column owns reading width; nested text uses
        # its actual allocation (including avatar gutter), even during resize.
        limit = available
        self._max_width = limit
        self._bubble.set_content_width(limit, limit=limit)
        self._sync_bubble_height()

    def _sync_bubble_height(self, _event=None):
        try:
            need = self.body.winfo_reqheight() + self._bubble._pady * 2
            cv = self._bubble._cv
            if abs(cv.winfo_reqheight() - need) > 1:
                cv.configure(height=max(1, need))
            self._bubble._on_canvas()
        except tk.TclError:
            pass

    def set_status(self, text: str):
        self._status_indicator.set(text)
        # 气孔内占位：仅在还没有正文时跟随状态文案（生成中/连接中/停止中…），
        # 有正文后 head 指示器单独负责，避免重复。
        if self._stream is None and not self._has_details and not self._text_source.strip():
            if str(text or "").strip():
                self.set_stream_placeholder(text)
            else:
                self._teardown_placeholder()

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
        from forge.secrets import redact
        text = redact(text)
        self._teardown_placeholder()
        self._text_source = text
        if self._stream is None:
            self._stream = InlineText(self.body, bg=self._bg)
            self._stream.pack(fill=tk.X)
        self._stream.set_text(text)
        self._fit_content()

    def append_stream(self, piece: str):
        self._teardown_placeholder()
        self._text_source += piece
        if self._stream is None:
            self._stream = InlineText(self.body, bg=self._bg)
            self._stream.pack(fill=tk.X)
        self._stream.append_text(piece)
        self._fit_content()

    # -- 流式占位（气孔内的「生成中…」）--

    def _ensure_placeholder(self):
        if self._placeholder_label is None:
            self._placeholder_label = i18n.Label(
                self.body, text="", bg=self._bg, fg=C["muted"],
                font=FONT_UI, anchor="w")
        if not self._placeholder_shown and self._stream is None:
            # 首选左上挂一条占位文案；气孔已存在（add_agent 即建），只是空
            self._placeholder_label.pack(fill=tk.X, anchor="w")
            self._placeholder_shown = True
            self._sync_bubble_height()

    def _teardown_placeholder(self):
        if self._placeholder_shown:
            self._placeholder_label.pack_forget()
            self._placeholder_shown = False
            self._sync_bubble_height()

    def set_stream_placeholder(self, text: str):
        """正文为空期间的状态文案（如「生成中…」）；有正文后调用方应改用
        stream_text/append_stream，占位自动让位。"""
        if self._stream is not None or (self._text_source or "").strip():
            return
        self._ensure_placeholder()
        self._placeholder_label.configure(text=text or "")

    def render_markdown(self, text: str, *, retry=True):
        from forge.secrets import redact
        text = redact(text)
        self._teardown_placeholder()
        self._text_source = text
        self._has_details = self._has_details or any(block["type"] in ("code", "li", "oli", "quote", "task") for block in parse_blocks(text))
        # 注释要求：「如果 AgentMessage 里既渲染正文又渲染工具/步骤，确保正文始终在最上、
        # trace 折叠块在正文下方，且有轻微分组」。
        # 我们每次 render_markdown：若已有 trace，加细分割线，重新顺序正文/trace。
        if self._stream is not None:
            self._stream.destroy()
            self._stream = None
        previous = getattr(self, "_body_host", None)
        if previous is not None:
            previous.destroy()
        # 分批渲染：长 markdown 不再把主线程堵过 Windows 未响应阈值
        host = self._body_host = render_blocks_chunked(
            self.body, text, bg=self._bg, on_done=self._after_markdown_done)
        trace = getattr(self, "_trace_host", None)
        host.pack(fill=tk.X, in_=self.body, side=tk.TOP, **({"before": trace} if trace is not None else {}))
        self._fit_content()
        # 已有 trace？保持顺序：Body 段在最上 → 分割线 → 现有 trace 块
        self._reorder_body_with_trace()
        self._ensure_action_row(retry=retry)

    def _after_markdown_done(self, _host):
        # 分批渲染完成：恢复 trace 排序（正文块 → 分割线 → trace）并终校高度
        try:
            self._reorder_body_with_trace()
            self._fit_content()
        except tk.TclError:
            pass

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
        self._teardown_placeholder()   # 有真实内容（工具卡/步骤）了，占位让位
        self._has_details = True
        self._fit_content()
        host = getattr(self, "_trace_host", None)
        if host is None or not host.winfo_exists():
            host = tk.Frame(self.body, bg=self._bg)
            host.pack(fill=tk.X)
            self._trace_host = host
        return host

    def _ensure_action_row(self, *, retry=True):
        edit = getattr(self._app, "_retry_last_agent", None)
        actions = []
        if self._text_source.strip():
            actions.append(("⧉", tr("复制"), self._copy_reply))
        if retry and getattr(self, "_retry_prompt", None) and callable(edit):
            actions.append(("⟳", tr("编辑后重试"), lambda: edit(self)))
        if self._action_row is not None:
            self._action_row.destroy()
            self._action_row = None
        if not actions:
            return None
        host = self._action_row = tk.Frame(self, bg=self._base)
        host.pack(fill=tk.X, anchor="w", padx=(ui_px(self, 36), 0), pady=(ui_px(self, 4), 0))
        for icon, label, command in actions:
            theme.pill_button(host, label, command, kind="quiet", icon=icon).pack(side=tk.LEFT)
        flow_controls(host)
        return host

    def _copy_reply(self):
        from forge.secrets import redact
        self.clipboard_clear()
        self.clipboard_append(redact(self._text_source))
        if callable(getattr(self._app, "_set_status", None)):
            self._app._set_status(tr("已复制"), "ok")

    def add_note(self, text: str, *, tone="muted"):
        # add_note 不属于正文/trace，更接近「提示」：降权 + 小字 + 紧贴 trace 之后
        host_frame = getattr(self, "_trace_host", None)
        if host_frame is None or not host_frame.winfo_exists():
            host_frame = self._ensure_trace_host()
        colors = {"muted": C["muted"], "ok": C["ok"], "error": C["error"],
                  "warn": C["warn"], "info": C["info"]}
        label = i18n.Label(host_frame, text=text, bg=self._bg,
                 fg=colors.get(tone, C["muted"]),
                 font=FONT_CAPTION, anchor="w", justify=tk.LEFT,
                 wraplength=self._max_width)
        label.pack(fill=tk.X, pady=(4, 0))
        bind_wrap(label)

        return label

    def add_actions(self, actions):
        # 动作按钮单独一个 host，靠底部（不被 trace 折叠吸收）
        if not hasattr(self, "_action_row_packed"):
            self._action_row_packed = False
        host = tk.Frame(self, bg=self._base)
        host.pack(fill=tk.X, padx=(ui_px(self, 36), 0), pady=(8, 0))
        row = ActionRow(host, actions, bg=self._base)
        row.pack(fill=tk.X)
        self._action_row_packed = True
        return row


class NoticeMessage(tk.Frame):
    """系统提示 / 错误（左侧色条 + 小字）。"""

    def __init__(self, parent, text, *, bg=None, tone="error", title=None, actions=()):
        from forge.secrets import redact
        self._notice_source = text = str(redact(str(text)))
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        tones = {"error": (C["error"], C["error_soft"]),
                 "warn": (C["warn"], C["warn_soft"]),
                 "ok": (C["ok"], C["ok_soft"]),
                 "info": (C["info"], C["info_soft"])}
        color, soft = tones.get(tone, tones["info"])
        card = RoundedCard(self, radius=R_MD, fill=soft, outline=C["border"],
                           padx=12, pady=8, bg=base)
        card.pack(fill=tk.X)
        i18n.Label(card.content, text=(title or tr("提示")), bg=soft, fg=color,
                 font=FONT_UI_BOLD, anchor="w").pack(fill=tk.X)
        first_line = text.splitlines()[0] if text else ''
        summary = first_line.split('{', 1)[0].strip()
        # Details preserve the complete sanitized failure without duplicating
        # a full response in the transcript's main reading path.
        self.summary = i18n.Label(card.content, text=summary[:240] if summary else tr('查看详情'), bg=soft,
                                  fg=C["body"], font=FONT_SMALL, anchor="w", justify=tk.LEFT)
        self.summary.pack(fill=tk.X, pady=(2, 0))
        bind_wrap(self.summary)
        self.details = InlineText(card.content, bg=soft, font=FONT_MONO_SM)
        self.details.set_text(text)
        btn_row = tk.Frame(card.content, bg=soft)
        btn_row.pack(anchor='w', pady=(6, 0))
        self.details_button = theme.pill_button(btn_row, tr('查看详情'), self.toggle_details,
                                                kind='quiet', bg=soft)
        self.details_button.pack(side=tk.LEFT)
        # 一键复制报错原文（含未截断的完整错误信息）
        self._copy_error_btn = theme.pill_button(
            btn_row, tr('复制原文'),
            lambda: copy_to_clipboard(self._copy_error_btn,
                                      lambda: self._notice_source),
            kind='quiet', bg=soft, icon='⧉')
        self._copy_error_btn.pack(side=tk.LEFT, padx=(8, 0))
        if actions:
            self.actions = ActionRow(card.content, actions, bg=soft)
            self.actions.pack(fill=tk.X, pady=(6, 0))

    def toggle_details(self):
        if self.details.winfo_manager():
            self.details.pack_forget()
            self.details_button.configure(text=tr('查看详情'))
        else:
            self.details.pack(fill=tk.X, pady=(6, 0), before=self.details_button)
            self.details_button.configure(text=tr('收起详情'))


class MessageArea(tk.Frame):
    """可滚动消息列表 + 空态。"""

    def __init__(self, parent, *, bg=None):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        self._bg = base
        self.scroll = ScrollArea(self, bg=base, padx=0, pady=ui_px(self, 20),
                                 max_width=theme.CONTENT_WIDTH)
        self.scroll.pack(fill=tk.BOTH, expand=True)
        self.jump_latest = theme.control_button(
            self, text=tr("回到最新"), command=self.scroll.scroll_to_end,
            bg=C["surface2"], fg=C["text"], activebackground=C["hover"],
            activeforeground=C["text"], relief=tk.FLAT, borderwidth=1,
            highlightthickness=1, highlightbackground=C["border"],
            padx=12, pady=5, cursor="hand2", font=FONT_SMALL)
        self.scroll.on_position_changed = self._update_jump_latest
        self._empty = None
        self._count = 0
        self._work_context = None
        self._context_hint = None
        self._context_actions = ()
        self._context_var = i18n.StringVar(self, value="")
        self.scroll.canvas.bind("<Configure>", self._fit_context_hint, add="+")
        self.scroll.inner.bind("<Configure>", self._fit_context_hint, add="+")
        self.show_empty()

    def _update_jump_latest(self):
        # 流式输出时 _on_scroll 每个 token 都会进这里。place/forget/lift
        # 每次都触发再布局→再 _on_scroll 的级联，长对话下 UI 直接卡死
        # （点击排队延迟，第二次点击被解析成双击/三击→「输入一行变蓝」）。
        # 只在显隐状态真正变化时碰布局。
        at_bottom = self._count == 0 or self.scroll.at_bottom()
        shown = bool(self.jump_latest.winfo_manager())
        if at_bottom and shown:
            self.jump_latest.place_forget()
        elif not at_bottom and not shown:
            self.jump_latest.place(relx=.5, rely=1, anchor="s", y=-ui_px(self, 8))
            self.jump_latest.lift()

    def show_empty(self, title="从一个目标开始",
                   lines=("描述你想解决的问题，或添加文件作为上下文。",
                          "执行工具请选择左侧「任务」；连接信息在右上「状态」。"),
                   actions=(), compact_actions=False):
        self.clear()
        box = tk.Frame(self.scroll.inner, bg=self._bg)
        box.pack(fill=tk.X, padx=ui_px(self, 16), pady=(ui_px(self, 48), 12))
        size = ui_px(self, 42)
        banner = tk.Canvas(box, width=size, height=size, bg=self._bg,
                           highlightthickness=0, bd=0)
        round_rect(banner, .5, .5, size - .5, size - .5, ui_px(self, 12),
                   fill=C["welcome_action"], outline=C["welcome_edge"])
        glyph = ui_px(self, 23)
        draw_icon(banner, "flame", x=(size - glyph) / 2, y=(size - glyph) / 2,
                  size=glyph, fg=C["brand_spark"], tag="decor")
        banner.pack(anchor="w", pady=(0, ui_px(self, 14)))
        title_label = i18n.Label(box, text=title, bg=self._bg, fg=C["text"],
                                 font=theme.FONT_HERO, anchor="w", justify=tk.LEFT)
        title_label.pack(fill=tk.X)
        title_label.bind("<Configure>", lambda e: title_label.configure(wraplength=max(80, e.width)))
        for line in lines:
            label = i18n.Label(box, text=line, bg=self._bg, fg=C["subtext"],
                             font=theme.FONT_LEAD, wraplength=500, justify=tk.LEFT, anchor="w")
            label.pack(fill=tk.X, pady=(ui_px(self, 5), 0))
            label.bind("<Configure>", lambda e, w=label: w.configure(
                wraplength=max(80, e.width - 16)))
        context = i18n.Label(box, textvariable=self._context_var, fg=C["muted"],
                             font=FONT_CAPTION, bg=self._bg, anchor="w", justify=tk.LEFT)
        context.pack(fill=tk.X, pady=(ui_px(self, 12), 0))
        context.bind("<Configure>", lambda e: context.configure(wraplength=max(80, e.width)))
        if actions:
            choices = tk.Frame(box, bg=self._bg)
            choices.pack(fill=tk.X, pady=(ui_px(self, 20), 0))
            action_rows = []
            # Quiet monochrome actions; only hover adds a raised surface and blue edge.
            # 可选第 4 项指定图标名。
            card_bg, card_hover = C["welcome_action"], C["hover"]
            default_icons = ("chat", "task", "workspace")
            tile = ui_px(self, 24)
            for index, action in enumerate(actions):
                title, detail, callback = action[:3]
                icon_name = action[3] if len(action) > 3 else default_icons[index % 3]
                idle_edge = C["welcome_edge"]
                row = tk.Frame(choices, bg=card_bg, padx=ui_px(self, 14), pady=ui_px(self, 16),
                               cursor="hand2", highlightthickness=1,
                               highlightbackground=idle_edge)
                icon = tk.Canvas(row, width=tile, height=tile, bg=card_bg,
                                 highlightthickness=0, bd=0, cursor="hand2")
                glyph = ui_px(self, 18)
                draw_icon(icon, icon_name, x=(tile - glyph) / 2, y=(tile - glyph) / 2,
                          size=glyph, fg=C["accent2"])
                icon.pack(anchor="w", pady=(0, ui_px(self, 10)))
                label = i18n.Label(row, text=title, bg=card_bg, fg=C["text"],
                                 font=theme.FONT_CHAT_BOLD, anchor="w", justify=tk.LEFT, cursor="hand2")
                label.pack(fill=tk.X)
                hint = i18n.Label(row, text=detail, bg=card_bg, fg=C["subtext"],
                                font=FONT_SMALL, anchor="w", justify=tk.LEFT,
                                cursor="hand2")
                hint.pack(fill=tk.X, pady=(ui_px(self, 3), 0))
                bind_keyboard_action(row, callback)
                row.configure(highlightbackground=idle_edge)
                action_rows.append((row, icon, label, hint))
                parts = (row, icon, label, hint)

                def paint(fill, edge, parts=parts):
                    parts[0].configure(highlightbackground=edge)
                    for w in parts:
                        w.configure(bg=fill)
                for widget in parts:
                    widget.bind("<Button-1>", lambda _e, fn=callback: fn())
                    widget.bind("<Enter>", lambda _e, fn=paint: fn(card_hover, C["accent_border"]))
                    widget.bind("<Leave>", lambda _e, fn=paint: fn(card_bg, idle_edge))

            def fit_choices(event):
                columns = len(action_rows) if event.width >= ui_px(self, 620) else 1
                for col in range(len(action_rows)):
                    choices.grid_columnconfigure(col, weight=1 if col < columns else 0,
                                                 uniform="actions" if col < columns else "")
                width = max(120, event.width // columns - ui_px(self, 48))
                for index, (row, icon, label, hint) in enumerate(action_rows):
                    row.grid(row=index // columns, column=index % columns,
                             sticky="nsew", padx=(0, ui_px(self, 8) if index % columns < columns - 1 else 0),
                             pady=ui_px(self, 4))
                    # Side panels turn the welcome choices into compact rows;
                    # keep all three actions visible instead of tall stacked tiles.
                    inline = columns == 1 or compact_actions
                    icon.pack_configure(side=tk.LEFT if inline else tk.TOP,
                                        padx=(0, ui_px(self, 12) if inline else 0),
                                        pady=(0, 0 if inline else ui_px(self, 10)))
                    row.configure(pady=ui_px(self, 10 if inline else 16))
                    text_width = max(80, width - (ui_px(self, 36) if inline else 0))
                    label.configure(wraplength=text_width)
                    hint.configure(wraplength=text_width)

            choices.bind("<Configure>", fit_choices)
            # Pack/grid needs an initial size before the first Configure event.
            for index, (row, icon, label, hint) in enumerate(action_rows):
                row.grid(row=index, column=0, sticky="ew", pady=4)

        self._suggest_row = tk.Frame(box, bg=self._bg)
        self._suggest_row.pack(fill=tk.X, pady=(ui_px(self, 14), 0))
        self._fill_suggestions()

        def fit_banner(_event=None):
            if banner is None:
                return
            viewport = self.scroll.canvas
            compact = viewport.winfo_height() < ui_px(self, 450) or viewport.winfo_width() < ui_px(self, 680)
            box.pack_configure(pady=(ui_px(self, 14 if compact else 48), 12))
            title_label.configure(font=FONT_TITLE if compact else theme.FONT_HERO)
            if compact:
                banner.pack_forget()
            elif not banner.winfo_manager():
                banner.pack(anchor="w", pady=(0, ui_px(self, 14)), before=title_label)

        binding = self.scroll.canvas.bind("<Configure>", fit_banner, add="+")
        # A new conversation can replace an existing view without resizing its
        # canvas, so apply the current dimensions as well as future changes.
        fit_banner()
        # The callback belongs to this empty state, not to later conversation views.
        def forget_banner(event):
            if event.widget is box:
                self.scroll.canvas.unbind("<Configure>", binding)
        box.bind("<Destroy>", forget_banner, add="+")
        self._empty = box

    def clear(self):
        self.scroll._cancel_scroll()
        self.scroll._follow_paused = False
        self.jump_latest.place_forget()
        for child in self.scroll.inner.winfo_children():
            child.destroy()
        self._empty = None
        self._count = 0
        self._context_hint = None

    def _fill_suggestions(self):
        row = getattr(self, "_suggest_row", None)
        if row is None or not row.winfo_exists():
            return
        for child in row.winfo_children():
            child.destroy()
        actions = getattr(self, "_context_actions", ()) or ()
        if not actions:
            return
        i18n.Label(row, text=tr("试试"), bg=self._bg, fg=C["faint"],
                   font=FONT_CAPTION).pack(side=tk.LEFT, padx=(0, ui_px(self, 6)))
        for title, callback in actions:
            rounded_label(row, title, fill=self._bg, outline=C["line"], fg=C["body"],
                          font=FONT_SMALL, bg=self._bg, command=callback,
                          padx=ui_px(self, 12), pady=ui_px(self, 4)).pack(
                side=tk.LEFT, padx=ui_px(self, 4))

    def set_work_context(self, repo, changed=None, *, actions=()):
        self._work_context = repo
        fresh = [title for title, _cb in actions] != [
            title for title, _cb in (getattr(self, "_context_actions", ()) or ())]
        self._context_actions = actions
        if fresh and self._empty is not None:
            self._fill_suggestions()
        text = tr("当前工作区：{name}", name=repo) if repo else tr("尚未选择工作区")
        if repo and changed is not None:
            detail = tr(" · {count} 个文件有修改", count=changed) if changed else tr(" · 工作区没有未提交修改")
            text = tr("{base}{detail}", base=text, detail=detail)
        self._context_var.set(text)
        self._fit_context_hint()

    def _fit_context_hint(self, _event=None):
        if self._empty is not None or not self._work_context or not 0 < self._count <= 2:
            if self._context_hint is not None:
                self._context_hint.pack_forget()
            return
        if self._context_hint is None:
            box = self._context_hint = tk.Frame(self.scroll.inner, bg=C["surface_subtle"], padx=14, pady=12)
            label = i18n.Label(box, textvariable=self._context_var, bg=C["surface_subtle"], fg=C["subtext"],
                             font=FONT_SMALL, justify=tk.LEFT, anchor="w")
            label.pack(fill=tk.X)
            bind_wrap(label)
            actions = ActionRow(box, [{"label": title, "command": callback} for title, callback in self._context_actions],
                                bg=C["surface_subtle"])
            actions.pack(fill=tk.X, pady=(8, 0))
            hint = i18n.Label(box, text=tr("选择建议会填入草稿，确认后再发送。"), bg=C["surface_subtle"],
                            fg=C["muted"], font=FONT_CAPTION, anchor="w", justify=tk.LEFT)
            hint.pack(fill=tk.X, pady=(4, 0))
            bind_wrap(hint)
        required = sum(child.winfo_reqheight() for child in self.scroll.inner.winfo_children()
                       if child is not self._context_hint)
        room = self.scroll.canvas.winfo_height() - required - int(self.scroll.inner.cget("pady")) * 2
        if room >= self._context_hint.winfo_reqheight() + 24:
            if not self._context_hint.winfo_manager():
                self._context_hint.pack(fill=tk.X, pady=(24, 0))
        else:
            self._context_hint.pack_forget()

    @property
    def empty(self) -> bool:
        return self._count == 0

    def _prepare(self):
        if self._context_hint is not None:
            self._context_hint.pack_forget()
        if self._empty is not None:
            self._empty.destroy()
            self._empty = None

    def _finish(self, following):
        self._count += 1
        self._fit_context_hint()
        if following:
            self.scroll.scroll_to_end()

    def add_user(self, text, *, ts=None, attachment_names=(), view_attachments=None):
        from forge.secrets import redact
        text = redact(text)
        following = self.scroll.at_bottom()
        self._prepare()
        msg = UserMessage(self.scroll.inner, text, bg=self._bg, ts=ts,
                          attachment_names=attachment_names, view_attachments=view_attachments)
        msg.pack(fill=tk.X, pady=(10, 0))
        self._finish(following)
        return msg

    def add_agent(self, *, role=None, ts=None, name="Forge", glyph="F",
                  subtitle=None, app=None):
        following = self.scroll.at_bottom()
        self._prepare()
        msg = AgentMessage(self.scroll.inner, app=app, bg=self._bg, name=name,
                           role=role, ts=ts, glyph=glyph, subtitle=subtitle)
        msg.pack(fill=tk.X, pady=(16, 0))
        self._finish(following)
        return msg

    def add_notice(self, text, *, tone="error", title=None, actions=()):
        following = self.scroll.at_bottom()
        self._prepare()
        msg = NoticeMessage(self.scroll.inner, text, bg=self._bg, tone=tone,
                            title=title, actions=actions)
        msg.pack(fill=tk.X, pady=(12, 0))
        self._finish(following)
        return msg

    def add_label(self, text, *, fg=None, font=None, pady=(8, 0)):
        following = self.scroll.at_bottom()
        lbl = i18n.Label(self.scroll.inner, text=text, bg=self._bg,
                       fg=fg or C["ter"], font=font or FONT_SMALL,
                       anchor="w", justify=tk.LEFT, wraplength=MAX_BUBBLE_WIDTH)
        lbl.pack(fill=tk.X, pady=pady)
        self._finish(following)
        return lbl


# ─── 输入卡（Composer） ────────────────────────────────────




class TeamTogglePill(tk.Frame):
    """Agent 集群/分工三态开关（底栏胶囊）。

    三态循环：off → auto → on → off
      off  「智能体：关」      —— 单模型直答（默认，不烧额外额度）
      auto 「智能体：AI 决断」 —— 本地启发式判断该不该并行（零成本）
      on   「智能体：开」      —— 按集群/分工配置强制并行
    点击循环切换；右键直接弹出三选一菜单。
    """

    MODES = ("off", "auto", "on")
    LABELS = {"off": tr("智能体：关"), "auto": tr("智能体：AI 决断"), "on": tr("智能体：开")}
    COLORS = {   # (bg, fg)
        "off":  ("input_bg", "subtext"),
        "auto": ("accent_soft", "accent"),
        "on":   ("ok", "#0B0B10"),
    }

    def __init__(self, parent, *, mode: str = "off", on_change=None, bg=None):
        base = bg or _bg_of(parent)
        super().__init__(parent, bg=base)
        self._mode = mode if mode in self.MODES else "off"
        self._on_change = on_change
        self._btn = rounded_label(
            self, text=self.LABELS[self._mode], fill=C["input_bg"],
            outline=C["line"], fg=C["subtext"], font=FONT_SMALL,
            bg=base, command=self._cycle, radius=R_PILL, padx=8, pady=2,
            tooltip=tr("Agent 集群/分工：关=单模型；开=按配置并行；AI 决断=本地规则按问题判断"))
        self._btn.pack()
        self._apply_mode_color()
        # 右键直达指定档位
        menu = i18n.Menu(self, tearoff=0, bg=C["surface"], fg=C["text"],
                       activebackground=C["hover"], activeforeground=C["accent"])
        for m in self.MODES:
            menu.add_command(label=self.LABELS[m],
                             command=lambda mm=m: self.set_mode(mm))
        self._menu = menu
        for w in (self, self._btn):
            w.bind("<Button-3>", lambda _e: self._menu.tk_popup(_e.x_root, _e.y_root))

    # ── 状态 ────────────────────────────────────────────────────────

    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode not in self.MODES or mode == self._mode:
            return
        self._mode = mode
        self._btn.set_text(self.LABELS[mode])   # rounded_label 是 Canvas，用 set_text
        self._apply_mode_color()
        # Width changes with the label; ask the enclosing action row to reflow.
        container = self.master
        while container is not None:
            refresh = getattr(container, "_l10n_refresh", None)
            if refresh is not None:
                refresh()
                break
            container = getattr(container, "master", None)
        if self._on_change is not None:
            try:
                self._on_change(mode)
            except Exception:
                pass

    def _cycle(self) -> None:
        i = self.MODES.index(self._mode)
        self.set_mode(self.MODES[(i + 1) % len(self.MODES)])

    def _apply_mode_color(self) -> None:
        bg_key, fg_raw = self.COLORS[self._mode]
        bg_c = C[bg_key]
        fg_c = C.get(fg_raw, fg_raw)   # "#0B0B10" 这类字面色直接用
        for item in self._btn.find_all():
            itype = self._btn.type(item)
            if itype == "polygon":
                self._btn.itemconfigure(item, fill=bg_c,
                                        outline=C["border_hi"] if self._mode == "off" else "")
            elif itype == "text":
                self._btn.itemconfigure(item, fill=fg_c)



class _IMECaretAnchor:
    """Refresh the native Tk caret and IME after ancestor layout changes.

    Tk 8.6 caches Text-local caret coordinates. Moving a Canvas window or
    maximizing the root can leave those unchanged, so Tk skips the native
    update. IMM and the system caret use the focused *toplevel's client*
    coordinates, not Text-local or screen coordinates.
    """

    def __init__(self, text_widget):
        self._w = text_widget
        self._anchor_job = None
        self._bindings = []
        self._supported = sys.platform == "win32"
        self._anchor_func = None
        if self._supported:
            try:
                self._init_imm()
            except (OSError, AttributeError):
                self._supported = False
        if not self._supported:
            return
        self._owner = text_widget.winfo_toplevel()
        self._ancestors = set()
        parent = text_widget
        while parent is not None:
            self._ancestors.add(parent)
            parent = parent.master
        for sequence in ("<FocusIn>", "<ButtonRelease-1>", "<KeyRelease>",
                         "<Map>", "<<Modified>>", "<MouseWheel>"):
            self._bind(text_widget, sequence, self._reanchor)
        # Toplevel bindtags also receive descendant Configure events. This
        # catches Canvas placement even when Text width/height stay identical.
        self._bind(self._owner, "<Configure>", self._on_canvas_or_text_configure)
        self._bind(text_widget, "<Destroy>", self._cancel_anchor)

    def _bind(self, widget, sequence, callback):
        token = widget.bind(sequence, callback, add="+")
        self._bindings.append((widget, sequence, token))

    def _init_imm(self):
        import ctypes
        from ctypes import wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._imm32 = ctypes.WinDLL("imm32", use_last_error=True)

        class COMPOSITIONFORM(ctypes.Structure):
            _fields_ = [("dwStyle", wintypes.DWORD), ("ptCurrentPos", wintypes.POINT),
                        ("rcArea", wintypes.RECT)]

        class CANDIDATEFORM(ctypes.Structure):
            _fields_ = [("dwIndex", wintypes.DWORD), ("dwStyle", wintypes.DWORD),
                        ("ptCurrentPos", wintypes.POINT), ("rcArea", wintypes.RECT)]

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                        ("rcCaret", wintypes.RECT)]

        self._COMPOSITIONFORM = COMPOSITIONFORM
        self._CANDIDATEFORM = CANDIDATEFORM
        self._GUITHREADINFO = GUITHREADINFO
        self._user32.GetFocus.argtypes = []
        self._user32.GetFocus.restype = wintypes.HWND
        self._user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        self._user32.ScreenToClient.restype = wintypes.BOOL
        self._user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
        self._user32.GetGUIThreadInfo.restype = wintypes.BOOL
        self._user32.SetCaretPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self._user32.SetCaretPos.restype = wintypes.BOOL
        self._imm32.ImmGetContext.argtypes = [wintypes.HWND]
        self._imm32.ImmGetContext.restype = wintypes.HANDLE
        self._imm32.ImmReleaseContext.argtypes = [wintypes.HWND, wintypes.HANDLE]
        self._imm32.ImmReleaseContext.restype = wintypes.BOOL
        self._imm32.ImmSetCompositionWindow.argtypes = [wintypes.HANDLE,
                                                        ctypes.POINTER(COMPOSITIONFORM)]
        self._imm32.ImmSetCompositionWindow.restype = wintypes.BOOL
        self._imm32.ImmSetCandidateWindow.argtypes = [wintypes.HANDLE,
                                                     ctypes.POINTER(CANDIDATEFORM)]
        self._imm32.ImmSetCandidateWindow.restype = wintypes.BOOL
        self._anchor_func = self._imm_set

    def _reanchor(self, _event=None):
        # No caret recreation or WndProc hook: ordinary letters may safely
        # refresh geometry too. Focus/click events have keysym="??" in Tk.
        self._schedule_anchor()

    def _on_canvas_or_text_configure(self, event=None):
        if event is None or event.widget in self._ancestors:
            self._schedule_anchor()

    def _schedule_anchor(self):
        if not self._supported or self._anchor_job is not None:
            return
        self._anchor_job = self._w.after_idle(self._flush_anchor)

    def _flush_anchor(self):
        self._anchor_job = None
        try:
            if self._update():
                self._anchor_caret()
        except (tk.TclError, OSError, KeyError):
            # A window can disappear or lose focus while layout is pending.
            pass

    def _cancel_anchor(self, event):
        if event.widget is not self._w:
            return
        if self._anchor_job is not None:
            self._w.after_cancel(self._anchor_job)
            self._anchor_job = None
        for widget, sequence, token in self._bindings:
            widget.unbind(sequence, token)
        self._bindings.clear()

    def _update(self):
        import ctypes
        from ctypes import wintypes
        self._caret = None
        if not self._w.winfo_ismapped() or self._w.focus_get() is not self._w:
            return False
        hwnd = int(self._w.winfo_toplevel().winfo_id())
        if self._user32.GetFocus() != hwnd:
            return False
        box = self._w.bbox("insert")
        if box is None:
            return False
        x, y, _width, height = box
        point = wintypes.POINT(self._w.winfo_rootx() + x, self._w.winfo_rooty() + y)
        if not self._user32.ScreenToClient(hwnd, ctypes.byref(point)):
            return False
        self._caret = (hwnd, point.x, point.y, height)
        return True

    def _imm_set(self):
        import ctypes
        hwnd, x, y, height = self._caret
        # Only move the existing caret owned by Tk; never destroy/create it
        # during composition. This also updates the TSF compatibility path.
        info = self._GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if self._user32.GetGUIThreadInfo(0, ctypes.byref(info)) and info.hwndCaret == hwnd:
            self._user32.SetCaretPos(x, y)
        context = self._imm32.ImmGetContext(hwnd)
        if not context:
            return
        try:
            composition = self._COMPOSITIONFORM()
            composition.dwStyle = 0x2  # CFS_POINT; IME may adjust to fit the screen.
            composition.ptCurrentPos.x = x
            composition.ptCurrentPos.y = y
            self._imm32.ImmSetCompositionWindow(context, ctypes.byref(composition))
            candidate = self._CANDIDATEFORM()
            candidate.dwStyle = 0x80  # CFS_EXCLUDE: keep the current text line clear.
            candidate.ptCurrentPos.x = x
            candidate.ptCurrentPos.y = y + height
            candidate.rcArea.left, candidate.rcArea.top = x, y
            candidate.rcArea.right, candidate.rcArea.bottom = x + 2, y + height
            self._imm32.ImmSetCandidateWindow(context, ctypes.byref(candidate))
        finally:
            self._imm32.ImmReleaseContext(hwnd, context)

    def _anchor_caret(self):
        if self._anchor_func is not None:
            self._anchor_func()


def _composer_tool(parent, text, icon, command, tooltip):
    """输入卡工具栏按钮：16px 线性图标 + 可选文字，安静色，悬停浮起。"""
    btn = glyph_button(parent, text, command, bg=C["input_bg"], fg=C["subtext"], size=9,
                       tooltip=tooltip)
    # 显式图标：IconButton.configure(text=…) 会保留它（i18n / 紧凑切换不丢图标）。
    btn._explicit_icon = btn._icon_name = icon_key(icon)
    btn._icon_size = ui_px(parent, 16)
    # Tk 的图片按钮忽略 pady；纯图标按钮带一个细空格，占出与带字按钮相同的行高。
    btn.configure(text=text or "\u2009", padx=ui_px(parent, 6 if text else 3))
    return btn


class InputCard(tk.Frame):
    """One control surface with a responsive command rail below the editor."""

    def __init__(self, parent, *, bg=None, placeholder="输入消息，或输入 / 使用命令...",
                 on_send=None, on_stop=None, on_paste=None, on_model=None,
                 models=None, model_var=None, thinking_text="◎ 沉思 · 关闭",
                 on_thinking=None, footer_left=None, footer_right=None,
                 attach_button=True, model_widget=None,
                 on_attach=None, on_context=None, on_commands=None,
                 on_settings=None, on_team_change=None, team_mode="off",
                 on_planning=None, planning_text="", on_project=None,
                 on_project_files=None, on_team_settings=None,
                 thinking_var=None, thinking_choices=(),
                 on_reasoning_select=None, on_reasoning_label=None):
        base = bg or C["chat"]
        super().__init__(parent, bg=base)
        self._on_send = on_send
        self._on_stop = on_stop
        self._thinking_text = thinking_text
        self._on_settings = on_settings
        self.send_var = tk.StringVar()

        card = theme.GlassControlCard(self, bg=base)
        card.pack(fill=tk.X)
        self._card = card
        inner = card.content

        top_row = tk.Frame(inner, bg=C["input_bg"])
        top_row.pack(fill=tk.X)

        # 附件入口在底栏；输入正文从同一条左边线开始。

        # ＋ 按钮（on_attach 优先；缺省回落到 on_paste，保持旧行为）
        plus_cb = on_attach if on_attach is not None else (on_paste or (lambda: None))
        # 次级小按钮：上下文、命令（弱化，小字）
        # 主输入区
        entry_host = tk.Frame(top_row, bg=C["input_bg"])
        entry_host.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.entry = tk.Text(entry_host, height=2, wrap="word", font=theme.FONT_CHAT,
                              bg=C["input_bg"], fg=C["text"],
                              insertbackground=C["accent"], relief=tk.FLAT, bd=0,
                              highlightthickness=0)
        self.entry.pack(fill=tk.X, ipady=3)

        # Keep the native caret and IME aligned after moves/maximize/relayout.
        self._ime_anchor = _IMECaretAnchor(self.entry)
        # IME 行内组合（inline composition）：preedit 直接画进输入框光标处，
        # 抑制系统默认的组合浮层；候选词窗口仍由系统 IME 显示。
        # Windows 专属，失败自动 no-op（InlineIME 内部全兜底）。
        # 2026-10-11 止血：行内 IME（子类化窗口过程）默认关闭。
        # 它把 Tk wndproc 换成 Python 回调，ctypes 回调的硬崩（访问违例）
        # 无法被 try/except 捕获，消息泵死锁表现为「打不出字/点不动」，
        # 且重启必现（与 10-10 晚起用户实测完全吻合）。候选框锚定
        # （_IMECaretAnchor，非子类化）不受影响。
        # 需要行内 preedit 时设 FORGE_INLINE_IME=1 开回。
        self._inline_ime = (InlineIME(self.entry)
                            if InlineIME and os.environ.get("FORGE_INLINE_IME") == "1"
                            else None)
        if self._inline_ime is not None:
            self.entry.bind("<Button-1>",
                            lambda _e: self._inline_ime.cancel_if_composing(),
                            add="+")
        self._hint = i18n.Label(entry_host, text=placeholder, bg=C["input_bg"],
                              fg=C["placeholder"], font=theme.FONT_CHAT, anchor="w",
                              cursor="xterm")
        self._hint.place(x=1, y=2)
        # The entire painted input surface is clickable, including the rim
        # and padding. Child toolbar buttons retain their own bindings.
        for surface in (card, card.canvas, inner, top_row, entry_host, self._hint):
            surface.bind("<Button-1>", lambda _e: self.focus_entry(), add="+")
        self.entry.bind("<Return>", self._enter)
        self.entry.bind("<Shift-Return>", lambda _e: None)
        self._syncing = False
        self.entry.bind("<<Modified>>", self._text_changed, add="+")
        self.entry.bind("<Configure>", lambda _e: self._resize_entry(), add="+")
        self.send_var.trace_add("write", self._sync_from_var)
        self.entry.bind("<FocusIn>", lambda _e: self._set_focus(True), add="+")
        self.entry.bind("<FocusOut>", lambda _e: self._set_focus(False), add="+")

        # Editing options expand before the rail, so send/stop stay anchored.
        self._options_controls = tk.Frame(inner, bg=C["input_bg"])
        state_row = tk.Frame(inner, bg=C["input_bg"])
        state_row.pack(fill=tk.X, pady=(ui_px(self, 8), 0))
        self._toolbar = state_row
        state_row.grid_columnconfigure(1, weight=1)
        bar = tk.Frame(state_row, bg=C["input_bg"])
        self._tools_row = bar
        bar.grid(row=0, column=0, sticky="w", padx=(0, ui_px(self, 10)))

        self.plus = circle_button(bar, "paperclip", plus_cb, size=ui_px(self, 30),
                                  kind="muted", bg=C["input_bg"], glyph_size=11,
                                  tooltip=tr("添加附件"))
        self._rail_tools = [self.plus]

        if attach_button:
            for text, icon, tip, callback in (
                    (tr("上下文"), "context", "查看历史与附件，检查本轮实际发送内容", on_context),
                    (tr("命令"), "terminal", "工具与命令（也可在输入框里输入 /）", on_commands)):
                if callback is not None:
                    self._rail_tools.append(_composer_tool(bar, text, icon, callback, tr(tip)))

        # Secondary settings live in one disclosure, leaving the everyday row
        # for context, commands and the current project. Widgets stay mounted.
        self._options_open = False
        self._planning_active = False
        self._team_callback = on_team_change
        self.team_pill = TeamTogglePill(self._options_controls,
                                        mode=team_mode, on_change=self._team_changed,
                                        bg=C["input_bg"])
        self.team_pill.pack(side=tk.LEFT, padx=(4, 0))
        self.project_btn = self.project_files_btn = self.team_settings_btn = None
        if on_project is not None:
            self.project_btn = _composer_tool(bar, tr("项目"), "folder_open", on_project,
                                              tr("选择项目工作区"))
            self._rail_tools.append(self.project_btn)
        if on_project_files is not None:
            self.project_files_btn = _composer_tool(self._options_controls, tr("项目文件"), "file", on_project_files,
                                                    tr("项目文件"))
            self.project_files_btn.pack(side=tk.LEFT)
        if on_team_settings is not None:
            # 设计稿：Agent 集群设置只留图标；开关状态在左侧「智能体」胶囊上。
            self.team_settings_btn = _composer_tool(self._options_controls, tr("Agent 集群"), "agents", on_team_settings,
                                                    tr("Agent 集群"))
            self.team_settings_btn.pack(side=tk.LEFT)

        # Sending always has a dedicated right-hand column.
        metadata = self._metadata = tk.Frame(state_row, bg=C["input_bg"])
        metadata.grid(row=0, column=1, sticky="ew", padx=(0, ui_px(self, 8)))
        self.provider_var = i18n.StringVar(self, value=tr("Provider：未配置"))
        self.provider_label = i18n.Label(metadata, textvariable=self.provider_var,
                                       font=FONT_CAPTION, bg=C["input_bg"], fg=C["faint"], anchor="w")
        self.provider_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
        model_host = self._model_host = tk.Frame(metadata, bg=C["input_bg"])
        # 模型胶囊按内容宽度，不拉满整行：这一行读起来是「模型 · 模式」，不是表单。
        model_host.grid(row=0, column=1, sticky="w", padx=(0, 8))
        metadata.grid_columnconfigure(3, weight=1)
        self.mode_var = i18n.StringVar(self, value=tr("模式：标准"))
        # 思考强度入口：点模式按钮弹出 Codex 式分级滑杆卡。
        self._reasoning_var = thinking_var
        self._reasoning_choices = tuple(thinking_choices or ())
        self._on_reasoning_select = on_reasoning_select
        self._reasoning_label_of = on_reasoning_label
        self._reasoning_popup = None
        self.mode_pill = glyph_button(metadata, tr("模式：标准"), self._toggle_reasoning_popup,
                                      bg=C["input_bg"], fg=C["subtext"], size=10,
                                      tooltip=tr("当前思考强度；点击调整，实际支持能力取决于模型"))
        self.mode_pill.grid(row=0, column=2, sticky="w")
        self.planning_pill = None
        if on_planning is not None:
            self.planning_pill = glyph_button(self._options_controls, planning_text, on_planning,
                bg=C["input_bg"], fg=C["subtext"], size=9,
                tooltip=tr("事前规划"))
            self.planning_pill.pack(side=tk.RIGHT,padx=(6,0))
            # 边框色保留 warn（契约），文字只在启用规划时才提亮（见 set_planning_active）。
            self.planning_pill.configure(highlightbackground=C["warn"],
                                         highlightcolor=C["warn"], fg=C["subtext"])
        right = tk.Frame(state_row, bg=C["input_bg"])
        right.grid(row=0, column=2, sticky="e")
        self._primary_controls = right
        self._toolbar_compact = None
        state_row.bind("<Configure>", self._fit_toolbar)
        self.model_var = model_var or tk.StringVar(value="default")
        self._show_provider = model_widget is None
        if model_widget is not None:
            self.model_pill = None
            self.model_widget = model_widget(model_host)
            self.model_widget.pack_configure(padx=0)
            # 选择器自带厂商标志，再写一遍「Provider：…」只是重复；值仍随 set_metadata 更新。
            self.provider_label.grid_remove()
        else:
            self.model_widget = None
            self.model_pill = rounded_label(model_host, f"▣ {self.model_var.get()}",
                                            fill=C["sel"], outline=C["sel_border"],
                                            fg=C["body"], font=FONT_SMALL,
                                            command=on_model, bg=C["input_bg"],
                                            tooltip=tr("切换模型"))
            self.model_pill.pack(fill=tk.X)
        # Compatibility accessor: mode is visibly represented by mode_pill.
        self.think_pill = rounded_label(model_host, thinking_text,
                                        fill=C["surface2"], outline=C["border_hi"],
                                        fg=C["subtext"], font=FONT_SMALL,
                                        command=on_thinking, bg=C["input_bg"],
                                        tooltip=tr("Forge 任务的沉思配置；普通 gateway 对话不执行任务沉思"))

        # ⚙ 设置按钮（可选）
        if on_settings is not None:
            self.settings_btn = glyph_button(bar, "⚙", on_settings, bg=C["input_bg"],
                                             fg=C["subtext"], size=11,
                                             hover=C["hover"], tooltip=tr("设置"))
            self._rail_tools.append(self.settings_btn)
        else:
            self.settings_btn = None
        self.options_btn = _composer_tool(bar, tr("选项"), "tools", self._toggle_options,
                                          tr("智能体、规划与项目文件"))
        self._rail_tools.append(self.options_btn)
        for i, tool in enumerate(self._rail_tools):
            tool.grid(row=0, column=i, padx=(0, ui_px(self, 2)))
        bar.bind("<Configure>", self._fit_toolbar, add="+")
        flow_controls(self._options_controls, gap=8)

        # 发送 / 停止（同一物理位置，set_busy 切换）
        self.send_circle = circle_button(right, "↑", self._fire_send, size=ui_px(self, 36),
                                         kind="muted", bg=C["input_bg"],
                                         tooltip=tr("发送（Enter）"))
        self.send_circle.pack(side=tk.RIGHT)
        self.stop_circle = circle_button(right, "■", self._fire_stop, size=ui_px(self, 36),
                                         kind="danger", bg=C["input_bg"],
                                         tooltip=tr("停止生成"))
        self._busy = False
        self._stopping = False
        self._toolbar_shrunk = []
        self.stop_circle.pack_forget()

        # 提示行（footer）—— 放到 InputCard 自带的 foot，不属于 inner card
        foot = tk.Frame(self, bg=base)
        foot.pack(fill=tk.X, pady=(6, 2))
        self.footer_left = i18n.Label(foot, text=footer_left or tr("空闲"), bg=base,
                                    fg=C["faint"], font=FONT_CAPTION)
        self.footer_left.pack(side=tk.LEFT)
        if footer_right is None:
            footer_right = "Enter ↵  ·  Shift+Enter 换行"
        self.footer_right = i18n.Label(foot, text=footer_right, bg=base,
                                     fg=C["faint"], font=FONT_CAPTION)
        self.footer_right.pack(side=tk.RIGHT)

        self._sync_hint()
        self._sync_send_state()
        self._sync_options_hint()
        locale = i18n.owner(self)
        if locale:
            locale.register(self)
        parent.bind("<Configure>", lambda _event: self._resize_entry(), add="+")
        self.bind("<Configure>", lambda _event: self._resize_entry(), add="+")

    # -- 交互 --
    def _team_changed(self, mode):
        self._sync_options_hint()
        if self._team_callback is not None:
            self._team_callback(mode)

    def _sync_options_hint(self):
        button = getattr(self, "options_btn", None)
        if button is None:
            return
        active = self._planning_active or self.team_pill.mode() != "off"
        caption = tr("收起选项") if self._options_open else tr("选项 · 已启用") if active else tr("选项")
        button.configure(text=caption, fg=C["accent_text"] if active or self._options_open else C["subtext"])
        self._fit_toolbar()

    def _l10n_refresh(self):
        self._fit_toolbar()

    def _toggle_options(self):
        self.set_options_expanded(not self._options_open)

    def set_options_expanded(self, expanded):
        expanded = bool(expanded)
        if expanded == self._options_open:
            return
        self._options_open = expanded
        if expanded:
            self._options_controls.pack(fill=tk.X, pady=(8, 0), before=self._toolbar)
        else:
            focused = self.focus_get()
            if focused is not None and str(focused).startswith(str(self._options_controls)):
                self.options_btn.focus_set()
            self._options_controls.pack_forget()
        self._sync_options_hint()
        self._resize_entry()

    def _resize_entry(self):
        """按显示行增长，长输入保留内部滚动，不把时间线挤出屏幕。"""
        try:
            count = self.entry.count("1.0", "end", "displaylines")
            parent_height = self.master.winfo_height()
            max_rows = 7
            minimum = 2
            timeline = next((child for child in self.master.winfo_children() if isinstance(child, MessageArea)), None)
            if parent_height > 1 and timeline is not None:
                line_height = tkfont.Font(root=self, font=self.entry.cget("font")).metrics("linespace")
                def padding(widget, key):
                    info = widget.pack_info()
                    raw = info.get(key, 0)
                    values = raw if isinstance(raw, tuple) else widget.tk.splitlist(str(raw))
                    nums = [int(value) for value in values]
                    return sum(nums) if len(nums) > 1 else nums[0] * 2
                reserve = max(ui_px(self, 80), int(parent_height * .3))
                siblings = sum(child.winfo_reqheight() + padding(child, "pady") + padding(child, "ipady")
                               for child in self.master.winfo_children()
                               if child not in (self, timeline) and child.winfo_manager() == "pack")
                chrome = sum(widget.winfo_reqheight() + padding(widget, "pady")
                             for widget in (getattr(self, "_options_controls", None), self._tools_row,
                                            self._toolbar, self.footer_left.master)
                             if widget is not None and widget.winfo_manager() == "pack")
                chrome += self._card._pady * 2 + padding(self, "pady") + padding(self.entry, "ipady")
                chrome += int(self.entry.cget("pady")) * 2 + int(self.entry.cget("borderwidth")) * 2
                # Reclaim decorative spacing before a single input row can
                # crowd out the conversation at high DPI / minimum height.
                normal_gap = ui_px(self, 8)
                current_gap = padding(self._toolbar, "pady")
                chrome += normal_gap - current_gap
                budget = parent_height - reserve - siblings - chrome
                gap = max(ui_px(self, 2), normal_gap - max(0, line_height - budget))
                if current_gap != gap:
                    self._toolbar.pack_configure(pady=(gap, 0))
                budget += normal_gap - gap
                max_rows = max(1, min(7, budget // max(1, line_height)))
                minimum = min(2, max_rows)
            rows = max(minimum, min(max_rows, int(count[0]) if count else 2))
            if int(self.entry.cget("height")) != rows:
                self.entry.configure(height=rows)
        except tk.TclError:
            pass

    def _set_focus(self, focused: bool):
        self._card.set_focused(focused)
        self._sync_hint()

    def _toggle_reasoning_popup(self):
        """模式按钮：弹出/收起 Codex 式思考强度滑杆卡。

        浮层定位与生命周期照 ModelPicker 的成熟模式：position_popover
        锚定按钮（右对齐、下方优先、放不下翻上方、夹在主窗内），
        transient(owner) + 非 topmost（Forge 内部浮层，不压其它应用），
        跟随主窗 <Configure> 重定位，主窗最小化/移动出屏即关闭。
        """
        if getattr(self, "_reasoning_popup", None) is not None:
            self._close_reasoning_popup()
            return
        from reasoning_slider import ReasoningSlider
        import liquid_glass as glass
        pop = tk.Toplevel(self)
        pop.withdraw()
        pop.overrideredirect(True)
        owner = self.winfo_toplevel()
        try:
            pop.transient(owner)
            pop.attributes("-topmost", False)
        except tk.TclError:
            pass
        inset = ui_px(self, 8)
        glass_host = glass.glass_window(pop, fill=C["surface"], radius=ui_px(self, 18),
                                        matte=C["bg"], inset=inset)
        slider = ReasoningSlider(
            glass_host, self._reasoning_var, self._reasoning_choices,
            on_select=self._on_reasoning_select, bg=C["surface"],
            default_value=self._reasoning_var.get() or "medium")
        slider.pack(fill=tk.BOTH, expand=True, padx=4, pady=4)
        # overrideredirect 的 Toplevel 必须在映射前定 geometry——一旦 wm
        # 以 +0+0 映射过，再 set 坐标在部分 Windows/Tk 组合上不生效。
        glass.fit_glass_window(glass_host)
        w = max(360, slider.winfo_reqwidth() + 2 * (inset + 4))
        h = slider.winfo_reqheight() + 2 * (inset + 4) + 4
        position_popover(pop, self.mode_pill, w, h, align_right=True)
        pop.deiconify()
        slider.canvas.focus_set()
        self._reasoning_popup = pop
        self._reasoning_popup_slider = slider

        # 外点关闭：绝不能 bind_all——全局 bindtable 上的 handler 在弹层
        # 销毁后解不掉（实例 unbind 只解自己的表），僵尸 handler 每次点击
        # 对已销毁的 pop 抛 TclError，Tk 会中断整个事件链，输入框从此点
        # 不进焦点（2026-10-10 用户实测「打不进字」的根因）。照 ModelPicker
        # 的生命周期模式：bind 在 owner 与 pop 各自的表上并登记 funcid。
        self._reasoning_popup_bindings = []

        def _close_on_owner_click(_event):
            self._close_reasoning_popup()

        for widget in (owner, pop):
            try:
                funcid = widget.bind("<Button-1>", _close_on_owner_click, add="+")
            except tk.TclError:
                continue
            if funcid:
                self._reasoning_popup_bindings.append((widget, "<Button-1>", funcid))

        def _close_on_esc(_event):
            self._close_reasoning_popup()
            return "break"

        try:
            esc_id = pop.bind("<Escape>", _close_on_esc, add="+")
        except tk.TclError:
            esc_id = ""
        if esc_id:
            self._reasoning_popup_bindings.append((pop, "<Escape>", esc_id))

        def _reposition(_event):
            if getattr(self, "_reasoning_popup", None) is pop:
                try:
                    position_popover(pop, self.mode_pill, w, h, align_right=True)
                except tk.TclError:
                    self._close_reasoning_popup()

        self._reasoning_owner_cfg_id = owner.bind("<Configure>", _reposition, add="+")

        def _owner_gone(_event):
            # 主窗最小化/隐藏时 overrideredirect 浮层不会自动消失（只认
            # 主窗自己的 Unmap，子控件冒泡忽略——与 ModelPicker 同判法）。
            if (_event is not None and
                    str(getattr(_event, "widget", "")) != str(owner)):
                return
            self._close_reasoning_popup()

        self._reasoning_owner_unmap_id = owner.bind("<Unmap>", _owner_gone, add="+")
        pop.focus_set()

    def _close_reasoning_popup(self):
        pop = getattr(self, "_reasoning_popup", None)
        if pop is None:
            return
        focused = self.focus_get()
        restore_focus = (focused is not None and
                         (focused is pop or str(focused).startswith(str(pop) + ".")))
        for widget, sequence, funcid in getattr(self, "_reasoning_popup_bindings", ()):
            try:
                widget.unbind(sequence, funcid)
            except (tk.TclError, TypeError):
                pass
        self._reasoning_popup_bindings = []
        owner = self.winfo_toplevel()
        for attr, sequence in (("_reasoning_owner_cfg_id", "<Configure>"),
                               ("_reasoning_owner_unmap_id", "<Unmap>")):
            try:
                token = getattr(self, attr, None)
                if token:
                    owner.unbind(sequence, token)
            except (tk.TclError, TypeError):
                pass
        self._reasoning_popup = None
        try:
            # Return focus while the popup still owns the native activation;
            # after destroying it, focus_set may only record a future target.
            if restore_focus and owner.winfo_ismapped():
                self.focus_entry()
            pop.destroy()
        except tk.TclError:
            pass

    def _sync_mode_label(self):
        """档位变化后刷新模式按钮文字。"""
        label = getattr(self, "_reasoning_label_of", None)
        if label is not None:
            try:
                self.mode_pill.configure(text=label(self._reasoning_var.get()))
            except tk.TclError:
                pass

    def _fit_toolbar(self, _event=None):
        """Reflow the same controls, leaving send/stop in the lower-right corner."""
        canvas_width = self._card._cv.winfo_width()
        if canvas_width <= 1:
            return
        content_width = max(1, canvas_width - self._card._padx * 2)
        gap = ui_px(self, 2)
        tool_widths = [tool.winfo_reqwidth() + gap for tool in self._rail_tools]
        provider_width = self.provider_label.winfo_reqwidth() + ui_px(self, 8) if self._show_provider else 0
        fixed = provider_width + self.mode_pill.winfo_reqwidth() + ui_px(self, 16)
        send_width = max(self.send_circle.winfo_reqwidth(), self.stop_circle.winfo_reqwidth())
        signature = (content_width, tuple(tool_widths), fixed, send_width)
        if signature == getattr(self, "_toolbar_signature", None):
            return
        self._toolbar_signature = signature
        tools_width = sum(tool_widths)
        rail_margin = ui_px(self, 10)
        metadata_margin = ui_px(self, 8)
        available = max(1, content_width - send_width - metadata_margin)
        wrapped = available < tools_width + rail_margin + fixed + ui_px(self, 156)
        bar = self._tools_row
        if wrapped:
            bar.grid_configure(row=0, column=0, columnspan=3, sticky="w", padx=0, pady=(0, ui_px(self, 4)))
            self._metadata.grid_configure(row=1, column=0, columnspan=2)
            self._primary_controls.grid_configure(row=1)
        else:
            bar.grid_configure(row=0, column=0, columnspan=1, sticky="w", padx=(0, rail_margin), pady=0)
            self._metadata.grid_configure(row=0, column=1, columnspan=1)
            self._primary_controls.grid_configure(row=0)
            available -= tools_width + rail_margin
        row, column, used = 0, 0, 0
        for tool, width in zip(self._rail_tools, tool_widths):
            if used and used + width > content_width:
                row, column, used = row + 1, 0, 0
            tool.grid_configure(row=row, column=column, sticky="w")
            column += 1
            used += width

        compact = available < fixed + ui_px(self, 140)
        if compact != self._toolbar_compact:
            self._toolbar_compact = compact
            # 设计稿：模型选择器按内容宽度，余量给第 3 列空白；紧凑时才整行。
            self._metadata.grid_columnconfigure(1, weight=1 if compact else 0)
            self._metadata.grid_columnconfigure(3, weight=0 if compact else 1)
            self._metadata.grid_columnconfigure(0, weight=1 if compact else 0)
            if compact:
                self._model_host.grid(row=0, column=0, columnspan=3, sticky="ew", padx=0, pady=(0, 4))
                if self._show_provider:
                    self.provider_label.grid(row=1, column=0, columnspan=2, sticky="w")
                self.mode_pill.grid(row=1, column=2, columnspan=2, sticky="w")
            else:
                if self._show_provider:
                    self.provider_label.grid(row=0, column=0, columnspan=1, sticky="w")
                self._model_host.grid(row=0, column=1, columnspan=1, sticky="w", padx=(0, 8), pady=0)
                self.mode_pill.grid(row=0, column=2, sticky="w")
        # 宽屏下模型胶囊封顶 280：长模型名省略显示，不再把这一行撑成表单。
        budget = available if compact else min(max(1, available - fixed), ui_px(self, 280))
        if self.model_widget is not None and hasattr(self.model_widget, "set_width_budget"):
            self.model_widget.set_width_budget(budget)
        elif self.model_pill is not None:
            self.model_pill.configure(width=budget)

    def set_planning_active(self, active: bool):
        self._planning_active = bool(active)
        if self.planning_pill is not None:
            self.planning_pill.configure(fg=C["warn"] if active else C["subtext"])
        self._sync_options_hint()

    def set_metadata(self, *, provider, mode):
        self.provider_var.set(tr("Provider：{provider}", provider=provider))
        self.mode_var.set(tr("模式：{mode}", mode=tr(mode)))
        self.mode_pill.configure(text=tr("模式：{mode}", mode=tr(mode)))
        self._fit_toolbar()

    def _enter(self, event):
        if event.state & 1:                # Shift
            return None
        ime = getattr(self, "_inline_ime", None)
        if ime is not None and ime.composing:
            # 组合中：这一下回车属于 IME（确认候选），不是发送
            ime.cancel_if_composing()
            return "break"
        self._fire_send()
        return "break"

    def _text_changed(self, _event=None):
        ime = getattr(self, "_inline_ime", None)
        if ime is not None and ime.composing:
            # 组合中的拼音是显示层内容，不进 send_var
            self.entry.edit_modified(False)
            return
        if self.entry.edit_modified():
            if not self._syncing:
                self._syncing = True
                try:
                    self.send_var.set(self.entry.get("1.0", "end-1c"))
                    self.entry.edit_modified(False)
                finally:
                    self._syncing = False

    def _sync_from_var(self, *_args):
        # Restored messages and local commands must also update the visible draft.
        ime = getattr(self, "_inline_ime", None)
        if ime is not None and ime.composing:
            return   # 组合中改写 entry 会把 preedit 打碎；等确认后再同步
        if not self._syncing:
            value = self.send_var.get()
            if self.entry.get("1.0", "end-1c") != value:
                self._syncing = True
                try:
                    self.entry.delete("1.0", "end")
                    self.entry.insert("1.0", value)
                    self.entry.edit_modified(False)
                finally:
                    self._syncing = False
        self._sync_hint()

    def _fire_send(self):
        ime = getattr(self, "_inline_ime", None)
        if ime is not None and ime.composing:
            ime.cancel_if_composing()
            return
        if self._busy or not self.send_var.get().strip():
            return
        if self._on_send:
            self._on_send()

    def _fire_stop(self):
        if self._stopping or not self._busy:
            return
        self.set_stopping()
        if self._on_stop:
            self._on_stop()

    def set_stopping(self):
        self._stopping = True
        circle_button_state(self.stop_circle, "muted", enabled=False)
        self.set_status("正在停止…")

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
        circle_button_state(self.send_circle, "primary" if has_text else "muted", enabled=has_text)

    def set_busy(self, busy: bool):
        self._busy = busy
        self._stopping = False
        if busy:
            circle_button_state(self.stop_circle, "danger", enabled=True)
            self.send_circle.pack_forget()
            self.stop_circle.pack(side=tk.RIGHT)
        else:
            self.stop_circle.pack_forget()
            self.send_circle.pack(side=tk.RIGHT)
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
        self.footer_left.configure(text=text, fg=C["warn"] if self._stopping else C["subtext"])

    def focus_entry(self):
        self.entry.focus_set()
