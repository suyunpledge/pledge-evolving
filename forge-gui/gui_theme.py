"""Forge 双工作空间共享设计语言（当前提供深色主题）。

单一来源：所有配色、字体、圆角、间距、通用绘制原语都放这里，
`forge_gui_v2.py`、`workspace.py`、`chat_widgets.py` 全部从这里取，
避免三处各写一套 token。Work Mode 与 Coding Mode 共用这些基础
原语，但导航、面板布局和密度由各工作空间独立决定。

深墨色层级搭配低饱和蓝色操作反馈；轻量用户气泡与文档式助手正文。
正文与必要说明优先保证对比度。自绘控件提供键盘激活和可见焦点，
常用操作保持足够热区，工具提示延迟出现并随控件销毁而清理。

零依赖：只用 tkinter（Python 自带）。
"""
from __future__ import annotations

import i18n
from i18n import tr

import math
import platform
import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont
from ui_icons import IconButton, IconCanvas, draw_icon, icon_key, split_icon_text, emoji_image

IS_WINDOWS = platform.system() == "Windows"

# ─── 调色板 ────────────────────────────────────────────────

C: dict[str, str] = {
    # 背景层级
    "bg": "#0D141F",            # Shared near-black blue chrome
    "activity": "#0A111C",
    "sidebar": "#111C2B",
    "sidebar_history": "#131923",
    "chat": "#0F1927",
    "surface": "#172439",
    "surface2": "#142034",
    "surface_subtle": "#111D2D",
    "hover": "#20344E",
    "sel": "#163B62",
    "code_bg": "#0B131F",
    "input_bg": "#111D2D",
    "editor_line": "#142238",
    "agent_accent": "#B79AF9",
    "black": "#000000",

    # 线条
    "border": "#26364D",
    "border_hi": "#3B5270",
    "sel_border": "#3974B5",
    "scroll": "#334B69",

    # 控件状态
    "control_pressed": "#1F293D",
    "disabled_bg": "#191F2D",
    "disabled_fg": "#8894AD",
    "disabled_border": "#343D50",
    "focus_ring": "#78B5FF",

    # 主色
    "accent": "#2454AD",
    "accent_hover": "#2A61BD",
    "accent_pressed": "#1D448D",
    "accent2": "#6AAEFF",
    "accent_soft": "#173453",
    "accent_border": "#3974B5",
    "accent_text": "#BCD9FF",
    "brand_ice": "#BDCAFF",       # 仅用于品牌图形，避免混同状态颜色
    "brand_spark": "#F5BC74",

    # 语义
    "ok": "#48B89A",
    "ok_pressed": "#399B80",
    "ok_soft": "#152C27",
    "warn": "#E5B45C",
    "warn_soft": "#2E2410",
    "error": "#E57373",
    "error_soft": "#321C2A",
    "info": "#7B9CFF",
    "info_soft": "#23314A",
    "link": "#AFC4FF",
    "link_soft": "#23314A",

    # 文字
    "text": "#E6EEF8",
    "body": "#CDD8E8",
    "subtext": "#A4B4C9",
    "muted": "#A2ADC5",
    "ter": "#A2ADC5",
    "placeholder": "#A2ADC5",
    "comment": "#8A9DB8",

    # 消息气泡：两侧要能看出「谁在说」，又不能让深色界面变花。
    # user 带一点主色倾向（“我说的话”），agent 用中性面；两者都配一道 hairline 描边。
    "msg_user_bg": "#23314A",         # 轻量、低饱和的用户气泡
    "msg_user_fg": "#FFFFFF",          # 用户气泡文字（白）
    "msg_agent_bg": "#0F1927",         # 助手正文融入文档背景
    "msg_agent_fg": "#E8ECFA",         # AI 气泡文字（浅灰）
    "msg_user_bg_old": "#232134",
    "msg_agent_bg_old": "#1A1A22",
    "msg_user_border": "#334666",
    "msg_agent_border": "#0F1927",

    # 代码高亮（One Dark 近似）
    "code_kw": "#74B4EA",
    "code_cls": "#67D4C0",
    "code_fn": "#DCDCAA",
    "code_str": "#CEAB8F",
    "code_const": "#E06C75",
    "code_num": "#B5CEA8",
    "code_comment": "#7FA17A",
    "code_plain": "#CDD8E8",

    # diff
    "diff_add": "#4ADE80",
    "diff_del": "#F87171",
    "diff_add_bg": "#122E1B",
    "diff_del_bg": "#2C1416",
    "diff_hunk_bg": "#1B1B33",

    # git 徽章
    "git_m": "#F59E0B",
    "git_a": "#22C55E",
    "git_d": "#EF4444",
    "git_u": "#7C7CF0",
}

# ─── 圆角 / 间距 ───────────────────────────────────────────

R_WINDOW, R_PANEL, R_CARD, R_PILL, R_MD, R_SM = 12, 12, 10, 8, 8, 6
# 钝角圆角（对话气泡 / 底部输入卡专用）：真圆弧绘制 + 大半径，圆钝柔和。
R_BUBBLE = 16
PAD_XS, PAD_S, PAD_M, PAD_L, PAD_XL = 4, 8, 12, 16, 20

# 交互尺寸：所有主按钮、图标热区与浮层以此为基线，避免页面各写一套。
CONTROL_HEIGHT = 32
ICON_SIZE = 16
ICON_HIT_SIZE = 32
OVERLAY_WIDTH = 448
FOCUS_WIDTH = 1
CONTENT_WIDTH = 1040
FORM_WIDTH = 800
CONTENT_GUTTER = 20
MOTION_MS = 0
SHADOW = None

# ─── 字体 ──────────────────────────────────────────────────
# 参考稿用 Inter / JetBrains Mono；本机没有，退回 Windows 自带等价字体
# （Segoe UI / Microsoft YaHei UI 承担拉丁+中文，Cascadia Code 承担等宽）。

UI_FAMILY = "Segoe UI" if IS_WINDOWS else "Helvetica"
CJK_FAMILY = "Microsoft YaHei UI" if IS_WINDOWS else UI_FAMILY
MONO_FAMILY = "Cascadia Code" if IS_WINDOWS else "Menlo"

FONT_UI = (UI_FAMILY, 11)
FONT_UI_BOLD = (UI_FAMILY, 11, "bold")
FONT_UI_MED = (UI_FAMILY, 11, "normal")
FONT_TITLE = (UI_FAMILY, 15, "bold")
FONT_SECTION = (UI_FAMILY, 12, "bold")
FONT_SMALL = (UI_FAMILY, 10)
FONT_CAPTION = (UI_FAMILY, 9)
FONT_MICRO = (UI_FAMILY, 8)
FONT_BRAND = (UI_FAMILY, 15, "bold")
FONT_MONO = (MONO_FAMILY, 11)
FONT_MONO_SM = (MONO_FAMILY, 10)
FONT_MONO_XS = (MONO_FAMILY, 9)
FONT_MONO_BOLD = (MONO_FAMILY, 11, "bold")
FONT_GLYPH = (UI_FAMILY, 11)


def ui_px(widget, value):
    """Convert 96-DPI layout units to the current Tk pixel scale."""
    return max(1, round(value * float(widget.tk.call("tk", "scaling")) * 72 / 96))


def column_geometry(widget, viewport, *, max_width=CONTENT_WIDTH, gutter=CONTENT_GUTTER):
    """One DPI-aware width contract for transcript, composer and forms."""
    width = max(1, min(ui_px(widget, max_width), viewport - ui_px(widget, gutter) * 2))
    return width, max(0, (viewport - width) // 2)


def bind_canvas_column(canvas, item, *, max_width=CONTENT_WIDTH, gutter=CONTENT_GUTTER):
    """Constrain an existing Canvas window; keep its parent and scroll ownership."""
    def fit(event):
        width, left = column_geometry(canvas, event.width, max_width=max_width, gutter=gutter)
        canvas.coords(item, left, 0)
        canvas.itemconfigure(item, width=width)
    canvas.bind('<Configure>', fit, add='+')
    return fit


def bind_packed_column(widget, *, max_width=CONTENT_WIDTH, gutter=CONTENT_GUTTER):
    """Bound a packed child without adding a competing layout container."""
    parent = widget.master
    def fit(event):
        if event.widget is not parent or widget.winfo_manager() != 'pack':
            return
        width, _inset = column_geometry(widget, event.width, max_width=max_width, gutter=gutter)
        inset = math.ceil((event.width - width) / 2)
        if widget.pack_info().get('padx') != inset:
            widget.pack_configure(padx=inset)
    token = parent.bind('<Configure>', fit, add='+')
    widget.bind('<Destroy>', lambda e: parent.unbind('<Configure>', token)
                if e.widget is widget else None, add='+')


def style_editor(widget):
    """Apply shared editable-text states to existing Entry/Text/Spinbox widgets."""
    widget.configure(bg=C['input_bg'], fg=C['text'], insertbackground=C['text'],
                     relief=tk.FLAT, bd=0, highlightthickness=FOCUS_WIDTH,
                     highlightbackground=C['border'], highlightcolor=C['focus_ring'],
                     selectbackground=C['accent_soft'], selectforeground=C['text'])
    return widget


def text_width(widget, text, font=FONT_UI, *, limit=None):
    """Measure rendered text and emoji in pixels, without character rounding.

    ``limit`` enables an early exit: once any line reaches the cap the exact
    width no longer matters (the caller clamps to that cap anyway), so we stop
    measuring. Without it, streaming a long reply re-measured the whole
    growing buffer on every delta — O(n²) in the reply length.
    """
    from ui_icons import emoji_parts
    measure = tkfont.Font(root=widget, font=font)
    best = 0
    linespace = None
    for line in text.split("\n"):
        # Bound the per-call cost. Every glyph is at least 1px wide, so a line
        # with more characters than the pixel cap necessarily exceeds it —
        # return immediately instead of handing emoji_parts a megabyte-long
        # line (which was the real O(n) cost behind the O(n²) streaming).
        if limit is not None and len(line) > limit:
            return limit
        width = 0
        for part, is_emoji in emoji_parts(line):
            if is_emoji:
                if linespace is None:
                    linespace = measure.metrics("linespace")
                bitmap = emoji_image(widget, part, size=linespace)
                width += bitmap.width() if bitmap is not None else measure.measure(part)
            else:
                width += measure.measure(part)
            if limit is not None and width >= limit:
                return limit
        if width > best:
            best = width
    if limit is not None and best >= limit:
        return limit
    return best


def bind_wrap(label):
    """Wrap inside a label's actual allocated width, including its own insets."""
    def fit(event):
        inset = sum(int(label.cget(key)) for key in ("padx", "borderwidth", "highlightthickness")) * 2
        width = max(1, event.width - inset)
        if int(label.cget("wraplength")) != width:
            label.configure(wraplength=width)
    label.bind("<Configure>", fit, add="+")
    return label


def bind_scoped_wheel(canvas, content, command=None):
    """Route descendant wheel events without replacing global/other views' bindings.

    Native editors and scrollable lists retain their own wheel behavior. Read-only
    inline text without a scrollbar follows its surrounding conversation instead.
    """
    owner = canvas.winfo_toplevel()

    def wheel(event):
        widget = event.widget
        if not isinstance(widget, tk.Misc) or not canvas.winfo_ismapped():
            return
        current = widget
        while current is not None:
            if isinstance(current, tk.Canvas) and current is not canvas and current.cget("yscrollcommand"):
                return  # Let a nested viewport's own binding handle this event.
            if isinstance(current, (tk.Text, tk.Listbox, tk.Scrollbar, ttk.Scrollbar)):
                if not (isinstance(current, tk.Text) and str(current.cget("state")) == "disabled"
                        and not current.cget("yscrollcommand")):
                    return
            if current is content or current is canvas:
                if not event.delta:
                    return
                if command is not None:
                    return command(event)
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return "break"
            current = current.master

    token = owner.bind("<MouseWheel>", wheel, add="+")

    def cleanup(event):
        if event.widget is canvas:
            owner.unbind("<MouseWheel>", token)

    canvas.bind("<Destroy>", cleanup, add="+")


def elide(widget, text, width, font=FONT_SMALL):
    measure = tkfont.Font(root=widget, font=font)
    if measure.measure(text) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if measure.measure(text[:mid] + "…") <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…" if width >= measure.measure("…") else ""

# This is a text fallback; Tk does not reliably render Windows color emoji fonts.
# UI icons use vectors, and supported message emoji use bundled PNG atlases.
EMOJI_FAMILY = "Segoe UI Emoji" if IS_WINDOWS else "Apple Color Emoji"
FONT_EMOJI = (EMOJI_FAMILY, 13)
FONT_EMOJI_SM = (EMOJI_FAMILY, 11)
FONT_EMOJI_XS = (EMOJI_FAMILY, 9)

_EMOJI_LO = 0x1F000
_EMOJI_HI = 0x1FAFF
_MISC_SYMBOL_LO = 0x2600          # ☀ ☑ ⚙ 等：Segoe UI Emoji 里也是彩色的
_MISC_SYMBOL_HI = 0x27BF
_VS16 = 0xFE0F                    # 变体选择符-16（强制彩色呈现）

def is_emoji(text: str) -> bool:
    """判断一段短文本是否应当用 emoji 字体渲染。

    只对短字形（图标位）判定；长文本走正常字体，避免正文里混进 emoji 字体导致
    中英文基线跳变。码位落在 emoji 区或常见符号区，或带变体选择符，都算。
    """
    if not text or len(text) > 3:
        return False
    for ch in text:
        cp = ord(ch)
        if cp == _VS16 or _EMOJI_LO <= cp <= _EMOJI_HI:
            return True
        if _MISC_SYMBOL_LO <= cp <= _MISC_SYMBOL_HI:
            return True
    return False


def emoji_font(text: str, size: int | None = None):
    """给 emoji 字形挑字体；不是 emoji 就返回正常 UI 字体。"""
    if not is_emoji(text):
        return (UI_FAMILY, size) if size else FONT_GLYPH
    base = size if size is not None else FONT_EMOJI[1]
    return (EMOJI_FAMILY, base)


# 颜色常量（独立于 token 的 rgba，Canvas / 头像等需要时用）
WECHAT_GREEN = (149, 236, 105, 255)     # #95EC69（亮端，UI 强调）
WECHAT_GREEN_DARK = (46, 174, 86, 255)   # #2EAE56（暗端，气泡底）


# ─── 绘制原语 ───────────────────────────────────────────────────────────────────────────────────────────


def rounded_points(x1, y1, x2, y2, r, *, seg=10):
    """圆角矩形顶点：四分之一圆用多段折线逼近，拐角是真正的钝圆弧。

    旧版每角只给 3 个控制点、靠 create_polygon(smooth=True) 样条插值——
    Tk 样条按切线走会把角往里削，视觉上「带棱角、发锐」。改成显式圆弧
    顶点（每角 seg 段）后，拐角是等半径的真圆弧，圆钝柔和。"""
    r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    if r <= 0:
        return [x1, y1, x2, y1, x2, y2, x1, y2]
    pts: list = []
    # 四角圆心 + 起止角（屏幕坐标 y 向下，故顺时针 180→270→0→90）
    for cx, cy, a0 in ((x1 + r, y1 + r, 180), (x2 - r, y1 + r, 270),
                       (x2 - r, y2 - r, 0), (x1 + r, y2 - r, 90)):
        for i in range(seg + 1):
            a = math.radians(a0 + 90 * i / seg)
            pts.append(cx + r * math.cos(a))
            pts.append(cy + r * math.sin(a))
    return pts


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画一个圆角矩形，返回 item id。

    顶点已是真圆弧折线，故 smooth=False（样条平滑反而会削角）。
    描边用圆角接头，密集顶点的轮廓不出现毛刺。"""
    kw.setdefault("joinstyle", tk.ROUND)
    return canvas.create_polygon(rounded_points(x1, y1, x2, y2, r), **kw)


class RoundedCard(tk.Frame):
    """圆角卡片：Canvas 负责画底与描边，`content` 是真正的容器。

    用法：
        card = RoundedCard(parent, fill=C["surface"], outline=C["border_hi"])
        card.pack(fill=tk.X)
        tk.Label(card.content, text="hi", bg=C["surface"]).pack()

    - `fill=tk.X` 时宽度跟随父容器，高度跟随内容；
    - 不 fill 时（anchor + 固定 wrap）宽度贴合内容，适合气泡。
    """

    def __init__(self, parent, *, radius=R_CARD, fill=None, outline=None,
                 padx=PAD_M, pady=10, bg=None, autosize_width=False,
                 on_click=None):
        outer_bg = bg or _bg_of(parent)
        super().__init__(parent, bg=outer_bg, highlightthickness=0, bd=0)
        self._bg = outer_bg
        self._radius = radius
        self._fill = fill or C["surface"]
        self._outline = outline
        self._padx, self._pady = padx, pady
        self._autosize_width = autosize_width
        self._content_width_limit = None
        self._preferred_content_width = None
        self._draw_job = None
        self._cv = tk.Canvas(self, bg=outer_bg, highlightthickness=0, bd=0,
                             height=1, width=1)
        self._cv.pack(fill=tk.BOTH, expand=True)
        self.content = tk.Frame(self._cv, bg=self._fill)
        self._win = self._cv.create_window(0, 0, window=self.content, anchor="nw")
        self._shape = None
        self._cv.bind("<Configure>", self._on_canvas)
        self.content.bind("<Configure>", self._on_content)
        self.bind("<Destroy>", self._cancel_draw, add="+")
        if on_click is not None:
            for w in (self, self._cv, self.content):
                w.bind("<Button-1>", lambda _e: on_click())
                w.configure(cursor="hand2")

    # -- 内部 --
    def _on_canvas(self, event=None):
        w = self._cv.winfo_width()
        if w <= 1:
            return
        self._cv.coords(self._win, self._padx, self._pady)
        self._cv.itemconfigure(self._win, width=max(1, w - self._padx * 2))
        self._draw(w, max(1, self._cv.winfo_height()))

    def _on_content(self, _event=None):
        try:
            need_h = self.content.winfo_reqheight() + self._pady * 2
            if abs(self._cv.winfo_reqheight() - need_h) > 1:
                self._cv.configure(height=need_h)
            if self._autosize_width:
                desired = self._preferred_content_width or self.content.winfo_reqwidth()
                if self._content_width_limit is not None:
                    desired = min(desired, self._content_width_limit)
                need_w = max(1, desired) + self._padx * 2
                if abs(self._cv.winfo_reqwidth() - need_w) > 1:
                    self._cv.configure(width=need_w)
            if self._draw_job is None:
                self._draw_job = self.after_idle(self._draw_later)
        except tk.TclError:
            pass

    def set_content_width(self, preferred=None, *, limit=None):
        """Pixel allocation for bubbles; the inner Canvas window follows it."""
        if (preferred, limit) == (self._preferred_content_width, self._content_width_limit):
            return
        self._preferred_content_width = preferred
        self._content_width_limit = limit
        self._on_content()

    def _draw_later(self):
        self._draw_job = None
        if self.winfo_exists():
            self._draw(self._cv.winfo_width(), self._cv.winfo_height())

    def _cancel_draw(self, event):
        if event.widget is self and self._draw_job is not None:
            self.after_cancel(self._draw_job)
            self._draw_job = None

    def _draw(self, w, h):
        if w <= 2 or h <= 2:
            return
        try:
            if not self.winfo_exists() or not self._cv.winfo_exists():
                return
            if self._shape is not None:
                self._cv.delete(self._shape)
            r = min(self._radius, w / 2, h / 2)
            kw = {"fill": self._fill}
            if self._outline:
                kw.update(outline=self._outline, width=1)
            self._shape = round_rect(self._cv, 0.5, 0.5, w - 0.5, h - 0.5, r, **kw)
            self._cv.tag_lower(self._shape)
        except tk.TclError:
            pass

    def set_fill(self, fill: str, outline: str | None = None):
        self._fill = fill
        if outline is not None:
            self._outline = outline
        self.content.configure(bg=fill)
        self._draw(self._cv.winfo_width(), self._cv.winfo_height())

    @property
    def canvas(self) -> tk.Canvas:
        return self._cv


def _bg_of(widget) -> str:
    try:
        return widget.cget("bg")
    except Exception:
        return C["bg"]


# ─── 小组件 ────────────────────────────────────────────────


def button_feedback(button: IconButton, *, kind="ghost") -> IconButton:
    """Shared feedback for native buttons, with stable geometry and semantics."""
    button.configure(highlightthickness=max(1, int(button.cget("highlightthickness"))),
                     highlightcolor=C["focus_ring"], disabledforeground=C["disabled_fg"])
    return button.set_feedback({
        "pressed_bg": C["accent_pressed"] if kind in {"primary", "accent_soft"} else
                      C["error_soft"] if kind == "danger" else
                      C["ok_pressed"] if kind == "ok" else C["control_pressed"],
        "hover_border": C["accent2"] if kind in {"primary", "accent_soft"} else
                        C["error"] if kind == "danger" else C["border_hi"],
        "disabled_bg": C["disabled_bg"], "disabled_border": C["disabled_border"],
    })


def pill_button(parent, text, command, *, kind="ghost", bg=None, height=None,
                font=None, padx=12, width=None, state=tk.NORMAL, icon=None):
    """扁平按钮（kind: primary / ghost / quiet / danger / accent_soft）。"""
    base = bg or _bg_of(parent)
    palettes = {
        "primary": (C["accent"], "#FFFFFF", C["accent_hover"], "#FFFFFF"),
        "ghost": (C["surface2"], C["body"], C["hover"], C["text"]),
        "quiet": (base, C["ter"], C["hover"], C["text"]),
        "danger": (C["surface2"], C["error"], C["error_soft"], C["error"]),
        "accent_soft": (C["accent_soft"], C["accent_text"], C["accent"], "#FFFFFF"),
        "ok": (C["ok"], "#0B0B10", "#34D399", "#0B0B10"),
    }
    bgb, fg, hovbg, hovfg = palettes.get(kind, palettes["ghost"])
    label = tr("{icon} {label}", icon=icon, label=text) if icon else text
    btn = IconButton(parent, text=label, command=command, bg=bgb, fg=fg,
                    activebackground=hovbg, activeforeground=hovfg,
                    font=font or FONT_SMALL, relief=tk.FLAT, bd=0,
                    padx=padx, pady=5, cursor="hand2", highlightthickness=1,
                    highlightbackground=C['accent_border'] if kind in {'primary','accent_soft'} else
                        (C['border_hi'] if kind in {'ghost','danger'} else base), highlightcolor=C["focus_ring"],
                    disabledforeground=C["disabled_fg"],
                    state=state)
    if width:
        btn.configure(width=width)
    if height:
        btn.configure(height=max(1, int(height / 16)))
    return button_feedback(btn, kind=kind)


def control_button(parent, **options):
    """Adapt legacy native button call sites to the existing shared control.

    Commands, variables, dimensions and explicit state colors remain intact.
    IconButton already owns localization and native keyboard activation.
    """
    options.setdefault('font', FONT_SMALL)
    options.setdefault('relief', tk.FLAT)
    options.setdefault('cursor', 'hand2')
    options.setdefault('bg', _bg_of(parent))
    options.setdefault('fg', C['body'])
    options.setdefault('activebackground', C['hover'])
    options.setdefault('activeforeground', C['text'])
    options.setdefault('highlightbackground', C['border'])
    bg = options.get('bg', _bg_of(parent))
    kind = ('primary' if bg == C['accent'] else 'ok' if bg == C['ok'] else
            'danger' if options.get('fg') == C['error'] else 'quiet' if bg == _bg_of(parent) else 'ghost')
    return button_feedback(IconButton(parent, **options), kind=kind)


def chip(parent, text, *, selected=False, command=None, bg=None):
    """筛选 chip（参考稿「全部文件 / 已修改 2 / 新增 1」）。"""
    bgb = C["accent_soft"] if selected else C["surface2"]
    fg = C["accent_text"] if selected else C["ter"]
    lbl = i18n.Label(parent, text=text, bg=bgb, fg=fg, font=FONT_MICRO,
                   padx=8, pady=3,
                   highlightthickness=1,
                   highlightbackground=C["accent"] if selected else C["border_hi"])
    if command is not None:
        bind_keyboard_action(lbl, command)
        lbl.bind("<Button-1>", lambda _e: command())
        lbl.configure(cursor="hand2")
        lbl.bind("<Enter>", lambda _e: lbl.configure(bg=C["sel"] if selected else C["hover"]))
        lbl.bind("<Leave>", lambda _e: lbl.configure(bg=bgb))
    return lbl


def badge(parent, text, *, tone="accent", bg=None):
    """小徽章（Beta / Planner / M / A）。"""
    tones = {
        "accent": (C["accent"], "#FFFFFF"),
        "accent_soft": (C["accent_soft"], C["accent_text"]),
        "ok": (C["ok_soft"], C["ok"]),
        "warn": (C["warn_soft"], C["warn"]),
        "error": (C["error_soft"], C["error"]),
        "muted": (C["surface2"], C["subtext"]),
    }
    bgb, fg = tones.get(tone, tones["accent"])
    return i18n.Label(parent, text=text, bg=bgb, fg=fg, font=FONT_MICRO,
                    padx=6, pady=2, highlightthickness=1, highlightbackground=fg)


def divider(parent, *, color=None, horizontal=True, bg=None):
    return tk.Frame(parent, bg=color or C["border"],
                    height=1 if horizontal else 0,
                    width=0 if horizontal else 1)


def flow_controls(parent, *, gap=6):
    """Wrap existing sibling controls by rendered width, without reparenting."""
    controls = list(parent.winfo_children())
    pending = None

    def fit():
        nonlocal pending
        pending = None
        if not parent.winfo_exists() or parent.winfo_width() <= 1:
            return
        padding = int(parent.cget("padx"))
        available = parent.winfo_width() - padding * 2
        used, top, row_height = 0, 2, 0
        for control in controls:
            width = control.winfo_reqwidth() + gap
            if used and used + width > available:
                top, used, row_height = top + row_height + 4, 0, 0
            height = control.winfo_reqheight()
            control.place(x=padding + used, y=top, width=width-gap, height=height)
            used += width
            row_height = max(row_height, height)
        parent.configure(height=top + row_height + 2)

    def schedule(_event=None):
        nonlocal pending
        if pending is None:
            pending = parent.after_idle(fit)

    def cleanup(event):
        nonlocal pending
        if event.widget is parent and pending is not None:
            parent.after_cancel(pending)
            pending = None

    for control in controls:
        control.pack_forget()
    parent._l10n_refresh = schedule
    locale = i18n.owner(parent)
    if locale:
        locale.register(parent)
    parent.bind("<Configure>", schedule, add="+")
    parent.bind("<Destroy>", cleanup, add="+")
    schedule()


def glyph_button(parent, glyph, command, *, bg=None, fg=None, size=13,
                 hover=None, tooltip=None):
    """无边框的图标按钮（用于 ✕ / ⋯ / ⟳ 之类）。"""
    base = bg or _bg_of(parent)
    btn = IconButton(parent, text=glyph, command=command, bg=base,
                    fg=fg or C["ter"], activebackground=hover or C["hover"],
                    activeforeground=C["text"], font=(UI_FAMILY, size),
                    relief=tk.FLAT, bd=0, padx=8, pady=4, cursor="hand2",
                    highlightthickness=1, highlightbackground=base,
                    highlightcolor=C["focus_ring"])
    if tooltip:
        attach_tooltip(btn, tooltip)
    return button_feedback(btn, kind="quiet")


def bind_keyboard_action(widget, command):
    """Give custom clickable controls the same keyboard contract as buttons."""
    border = (widget.cget("highlightbackground") if int(widget.cget("highlightthickness"))
              else widget.cget("bg"))
    widget.configure(takefocus=1, highlightthickness=1,
                     highlightbackground=border, highlightcolor=C["focus_ring"])

    def invoke(_event=None):
        if getattr(widget, "_enabled", True):
            command()
        return "break"

    widget.bind("<Return>", invoke)
    widget.bind("<space>", invoke)


def attach_tooltip(widget, text: str):
    # A delay avoids covering nearby controls while the pointer is travelling.
    tip = {"win": None, "after": None}
    owner = widget._root()

    def show(_e=None):
        tip["after"] = None
        if tip["win"] is not None or not widget.winfo_exists():
            return
        win = i18n.Toplevel(widget)
        win.wm_overrideredirect(True)
        win.configure(bg=C["border_hi"])
        i18n.Label(win, text=text, bg=C["surface"], fg=C["body"], font=FONT_CAPTION,
                 wraplength=360, justify=tk.LEFT,
                 padx=10, pady=6).pack(padx=1, pady=1)
        x = widget.winfo_rootx() + 12
        y = widget.winfo_rooty() + widget.winfo_height() + 6
        win.update_idletasks()
        # Virtual-root bounds also account for monitors to the left of the main one.
        left, top = widget.winfo_vrootx(), widget.winfo_vrooty()
        x = max(left, min(x, left + widget.winfo_vrootwidth() - win.winfo_reqwidth()))
        bottom = top + widget.winfo_vrootheight()
        if y + win.winfo_reqheight() > bottom:
            y = widget.winfo_rooty() - win.winfo_reqheight() - 6
        win.wm_geometry(f"{x:+d}{max(top, y):+d}")
        tip["win"] = win

    def hide(_e=None):
        if tip["after"] is not None:
            owner.after_cancel(tip["after"])
            tip["after"] = None
        if tip["win"] is not None:
            if tip["win"].winfo_exists():
                tip["win"].destroy()
            tip["win"] = None

    def schedule(_e=None):
        hide()
        tip["after"] = owner.after(450, show)

    widget.bind("<Enter>", schedule, add="+")
    widget.bind("<FocusIn>", schedule, add="+")
    widget.bind("<FocusOut>", hide, add="+")
    widget.bind("<Leave>", hide, add="+")
    widget.bind("<Button-1>", hide, add="+")
    widget.bind("<Destroy>", lambda event: hide() if event.widget is widget else None, add="+")


def position_popover(pop, anchor, width, height, *, prefer_above=False, align_right=False):
    """Tk geometry 与 winfo 已使用相同的屏幕单位，不能再次按 DPI 除算。"""
    owner = anchor.winfo_toplevel()
    left, top = owner.winfo_rootx(), owner.winfo_rooty()
    right, bottom = left + owner.winfo_width(), top + owner.winfo_height()
    width = max(1, min(width, owner.winfo_width() - 16))
    height = max(1, min(height, owner.winfo_height() - 16))
    x = anchor.winfo_rootx()
    if align_right:
        x += anchor.winfo_width() - width
    below = anchor.winfo_rooty() + anchor.winfo_height() + 6
    above = anchor.winfo_rooty() - height - 6
    y = above if prefer_above or below + height > bottom - 8 else below
    x = max(left + 8, min(x, right - width - 8))
    y = max(top + 8, min(y, bottom - height - 8))
    pop.geometry(f"{width}x{height}{int(x):+d}{int(y):+d}")


def show_popover_menu(anchor, items, *, title=None, width=300, prefer_above=False):
    """统一的产品化菜单浮层。

    ``items`` 是 ``{label, detail?, command?, selected?, danger?, separator?}``
    列表。它替代 Composer 主路径上的原生 ``tk.Menu``，并统一 hover、选中态、
    留白与弱化说明文字。返回 Toplevel，方便测试和调用方主动关闭。
    """
    previous = getattr(anchor, "_forge_popover", None)
    if previous is not None:
        try:
            previous.destroy()
        except tk.TclError:
            pass
    pop = i18n.Toplevel(anchor)
    pop.withdraw()
    pop.overrideredirect(True)
    pop.configure(bg=C["border_hi"])
    anchor._forge_popover = pop
    shell = tk.Frame(pop, bg=C["surface"], padx=8, pady=8)
    shell.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
    if title:
        i18n.Label(shell, text=title, bg=C["surface"], fg=C["subtext"],
                 font=FONT_CAPTION, anchor="w").pack(fill=tk.X, padx=8, pady=(4, 7))

    def close():
        if getattr(anchor, "_forge_popover", None) is pop:
            anchor._forge_popover = None
        try:
            pop.grab_release()
        except tk.TclError:
            pass
        try:
            pop.destroy()
        except tk.TclError:
            pass

    rows = []
    for item in items:
        if item.get("separator"):
            tk.Frame(shell, bg=C["border"], height=1).pack(fill=tk.X, pady=5)
            continue
        selected = bool(item.get("selected"))
        base = C["sel"] if selected else C["surface"]
        row = tk.Frame(shell, bg=base, cursor="hand2", padx=9, pady=6)
        row.pack(fill=tk.X, pady=1)
        rows.append(row)
        marker = tk.Frame(row, bg=C["accent"] if selected else base, width=2)
        marker.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 8))
        text = tk.Frame(row, bg=base, cursor="hand2")
        text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        label = i18n.Label(text, text=item.get("label", ""), bg=base,
                         fg=C["error"] if item.get("danger") else C["text"],
                         font=FONT_SMALL, anchor="w", cursor="hand2")
        label.pack(fill=tk.X)
        detail = None
        if item.get("detail"):
            detail = i18n.Label(text, text=item["detail"], bg=base,
                              fg=C["muted"], font=FONT_MICRO, anchor="w",
                              cursor="hand2")
            detail.pack(fill=tk.X, pady=(2, 0))

        def paint(bg, widgets=(row, marker, text, label, detail), is_selected=selected):
            for widget in widgets:
                if widget is None:
                    continue
                widget.configure(bg=C["accent"] if widget is marker and is_selected else bg)

        def invoke(_event=None, command=item.get("command")):
            close()
            if callable(command):
                command()
            return "break"

        bind_keyboard_action(row, invoke)

        for widget in (row, marker, text, label, detail):
            if widget is None:
                continue
            widget.bind("<Enter>", lambda _e, fn=paint: fn(C["hover"]))
            widget.bind("<Leave>", lambda _e, fn=paint, b=base: fn(b))
            widget.bind("<Button-1>", invoke)

    def dismiss(_event=None):
        close()
        try:
            anchor.focus_set()
        except tk.TclError:
            pass
        return "break"

    def move_focus(delta):
        focused = pop.focus_get()
        index = rows.index(focused) if focused in rows else -1
        if rows:
            rows[(index + delta) % len(rows)].focus_set()
        return "break"

    pop.bind("<Escape>", dismiss)
    pop.bind("<Down>", lambda _e: move_focus(1))
    pop.bind("<Up>", lambda _e: move_focus(-1))
    def maybe_close(_event=None):
        try:
            px, py = anchor.winfo_pointerxy()
            x, y = pop.winfo_rootx(), pop.winfo_rooty()
            inside = x <= px < x + pop.winfo_width() and y <= py < y + pop.winfo_height()
        except tk.TclError:
            inside = False
        if not inside:
            close()
    pop.bind("<Button-1>", maybe_close, add="+")
    pop.update_idletasks()
    position_popover(pop, anchor, width, pop.winfo_reqheight(), prefer_above=prefer_above)
    pop.deiconify()
    try:
        pop.grab_set()
        (rows[0] if rows else pop).focus_set()
    except tk.TclError:
        pass
    return pop


def avatar(parent, *, size=32, glyph="F", fg="#FFFFFF", fill=None,
           shape="rounded", bg=None, image=None):
    """头像：圆形（用户）或圆角方（Forge）。给 image 时直接画该位图（品牌标志）。"""
    base = bg or _bg_of(parent)
    cv = tk.Canvas(parent, width=size, height=size, bg=base,
                   highlightthickness=0, bd=0)
    if image is not None:
        try:
            # PhotoImage 与创建它的 Tcl interpreter 绑定。GUI 单测会
            # 连续创建/销毁多个 Tk 根窗口，模块级品牌图可能因此成为
            # “尚有 Python 引用、但 Tcl image 已不存在”的旧对象。
            # 真实应用只有一个根窗口；这里降级成绘制头像，
            # 让组件在多 root 环境中也保持可用。
            cv.create_image(size / 2, size / 2, image=image)
            cv.image = image          # 防 GC
            return cv
        except tk.TclError:
            pass
    fill = fill or C["accent"]
    if shape == "circle":
        cv.create_oval(0, 0, size - 1, size - 1, fill=fill, outline="")
    else:
        round_rect(cv, 0, 0, size - 1, size - 1, int(size * 0.28), fill=fill,
                   outline="")
        # 左上高光：一块更亮的圆角三角，模拟参考稿的紫色渐变
        cv.create_polygon(2, 2, size - 6, 2, 2, size - 6,
                          smooth=True, splinesteps=12, fill="#6D63F0", outline="")
    bitmap = emoji_image(cv, glyph, size=24) if is_emoji(glyph) else None
    if glyph == "你":
        draw_icon(cv, "user", x=4, y=4, size=size-8, fg=fg)
    elif bitmap is not None:
        cv.create_image(size/2, size/2, image=bitmap)
        cv.image = bitmap
    else:
        cv.create_text(size / 2, size / 2, text=glyph, fill=fg,
                   font=(emoji_font(glyph) if is_emoji(glyph)
                         else (UI_FAMILY, max(8, int(size * 0.42)), "bold")))
    return cv


def dot(parent, color, *, size=8, bg=None):
    base = bg or _bg_of(parent)
    cv = tk.Canvas(parent, width=size, height=size, bg=base,
                   highlightthickness=0, bd=0)
    cv.create_oval(0, 0, size - 1, size - 1, fill=color, outline="")
    return cv


def _circle_palette(kind):
    palettes = {
        "primary": (C["accent"], "#FFFFFF", C["accent_hover"], C["accent_border"], C["accent_soft"]),
        "muted": (C["surface2"], C["muted"], C["hover"], C["border_hi"], C["surface_subtle"]),
        "danger": (C["surface2"], C["error"], C["error_soft"], C["error"], C["surface_subtle"]),
    }
    return palettes.get(kind, palettes["primary"])


def _paint_circle(cv):
    from decor import mix
    fill, _fg, hover, border, pressed = cv._circle_palette
    active = cv._enabled
    color = pressed if active and cv._pressed else hover if active and cv._hovered else fill
    cv.itemconfigure(cv._oval, fill=color, outline=border)
    cv.itemconfigure(cv._sheen, outline=mix(C["brand_ice"], color, .65),
                     state=tk.NORMAL if active and cv._kind == "primary" else tk.HIDDEN)


def circle_button(parent, glyph, command, *, size=30, kind="primary", bg=None,
                  glyph_size=None, tooltip=None):
    """圆形按钮（发送 ↑ / 停止 ■）。"""
    base = bg or _bg_of(parent)
    fill, fg, hov, border, _pressed = _circle_palette(kind)
    cv = tk.Canvas(parent, width=size, height=size, bg=base,
                   highlightthickness=0, bd=0, cursor="hand2")
    oval = cv.create_oval(1, 1, size - 2, size - 2, fill=fill, outline=border)
    from decor import mix
    sheen = cv.create_arc(3, 3, size - 4, size - 4, start=45, extent=90,
                          style=tk.ARC, outline=mix(C["brand_ice"], fill, .65), width=1,
                          state=tk.NORMAL if kind == "primary" else tk.HIDDEN)
    icon = icon_key(glyph)
    if icon:
        icon_size = max(14, size-12)
        draw_icon(cv, icon, x=(size-icon_size)/2, y=(size-icon_size)/2, size=icon_size, fg=fg, tag="glyph")
        txt = "glyph"
    else:
        txt = cv.create_text(size / 2, size / 2, text=glyph, fill=fg,
                             font=(UI_FAMILY, glyph_size or max(9, int(size * 0.38)), "bold"))
    cv._icon_name = icon
    cv._icon_size = max(14, size-12)
    cv._button_size = size
    cv._enabled = True
    cv._kind, cv._circle_palette = kind, _circle_palette(kind)
    cv._hovered = cv._pressed = False
    cv._sheen = sheen
    bind_keyboard_action(cv, command)
    def paint(hovered=None, pressed=None):
        if hovered is not None: cv._hovered = hovered
        if pressed is not None: cv._pressed = pressed
        _paint_circle(cv)
    def invoke(_event):
        if cv._enabled:
            paint(pressed=True)
            command()
    cv.bind("<Button-1>", invoke)
    cv.bind("<ButtonRelease-1>", lambda _e: paint(pressed=False))
    cv.bind("<Enter>", lambda _e: paint(hovered=True))
    cv.bind("<Leave>", lambda _e: paint(hovered=False, pressed=False))
    if tooltip:
        attach_tooltip(cv, tooltip)
    cv._palette = (fill, hov)  # type: ignore[attr-defined]
    cv._oval = oval  # type: ignore[attr-defined]
    cv._glyph = txt  # type: ignore[attr-defined]
    return cv


def circle_button_state(cv, kind: str, *, enabled=None):
    """切换圆形按钮状态（primary/muted/danger）。"""
    fill, fg, hov, border, pressed = _circle_palette(kind)
    cv._kind, cv._circle_palette = kind, (fill, fg, hov, border, pressed)
    cv._pressed = False
    if getattr(cv, "_icon_name", None):
        cv.delete("glyph")
        offset = (cv._button_size-cv._icon_size)/2
        draw_icon(cv, cv._icon_name, x=offset, y=offset, size=cv._icon_size, fg=fg, tag="glyph")
    else:
        cv.itemconfigure(cv._glyph, fill=fg)
    cv._palette = (fill, hov)                      # type: ignore[attr-defined]
    if enabled is not None:
        cv._enabled = bool(enabled)
        cv.configure(takefocus=int(cv._enabled), cursor="hand2" if cv._enabled else "arrow")
    _paint_circle(cv)


# ─── 滚动 / ttk 主题 ───────────────────────────────────────


def style_scrollbar(widget):
    """给 scrolledtext / Text 内置滚动条上深色。"""
    try:
        widget.vbar.configure(bg=C["surface2"], activebackground=C["scroll"],
                              troughcolor=C["chat"], relief=tk.FLAT, bd=0,
                              highlightthickness=0, width=8,
                              elementborderwidth=0)
    except Exception:
        pass


def apply_ttk_theme(root):
    """ttk 控件（Combobox / Scrollbar / Notebook）深色主题。"""
    root.configure(background=C['bg'])
    for widget_class in ('Toplevel', 'Frame', 'Label', 'LabelFrame', 'PanedWindow', 'Menu'):
        root.option_add(f'*{widget_class}.background', C['bg'])
    for widget_class in ('Label', 'LabelFrame', 'Menu'):
        root.option_add(f'*{widget_class}.foreground', C['body'])
    for widget_class in ('Entry', 'Spinbox', 'Listbox', 'Text'):
        for option, colour in (('background', 'input_bg'), ('foreground', 'body'),
                               ('insertBackground', 'text'), ('selectBackground', 'accent_soft'),
                               ('selectForeground', 'accent_text')):
            root.option_add(f'*{widget_class}.{option}', C[colour])
    for widget_class in ('Button', 'Checkbutton', 'Radiobutton'):
        root.option_add(f'*{widget_class}.background', C['surface2'])
        root.option_add(f'*{widget_class}.foreground', C['body'])
        root.option_add(f'*{widget_class}.activeBackground', C['hover'])
        root.option_add(f'*{widget_class}.activeForeground', C['text'])
    root.option_add('*Button.relief', 'flat')
    root.option_add('*Button.borderWidth', 0)
    for widget_class in ('Checkbutton', 'Radiobutton'):
        root.option_add(f'*{widget_class}.selectColor', C['surface2'])
    root.option_add('*Menu.activeBackground', C['accent_soft'])
    root.option_add('*Menu.activeForeground', C['accent_text'])
    root.option_add('*Menu.disabledForeground', C['disabled_fg'])
    for widget_class in ('Checkbutton', 'Radiobutton', 'Entry', 'Text', 'Spinbox', 'Listbox'):
        root.option_add(f'*{widget_class}.highlightColor', C['focus_ring'])
        root.option_add(f'*{widget_class}.highlightBackground', C['border'])
    for widget_class in ('Checkbutton', 'Radiobutton', 'Entry', 'Spinbox', 'Listbox'):
        root.option_add(f'*{widget_class}.disabledForeground', C['disabled_fg'])
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    # Cover the base elements too: clam's defaults otherwise leak beige labels,
    # bright pane sashes and white tab/tree borders through the dark surfaces.
    style.configure('.', background=C['bg'], foreground=C['body'], font=FONT_SMALL,
                    bordercolor=C['border'], lightcolor=C['border'], darkcolor=C['border'],
                    focuscolor=C['focus_ring'], troughcolor=C['bg'])
    style.configure('TLabel', background=C['bg'], foreground=C['body'], font=FONT_SMALL)
    style.map('TLabel', foreground=[('disabled', C['disabled_fg'])])
    style.configure('TLabelframe', background=C['bg'], bordercolor=C['border'],
                    lightcolor=C['border'], darkcolor=C['border'], relief='solid', borderwidth=1)
    style.configure('TLabelframe.Label', background=C['bg'], foreground=C['subtext'], font=FONT_SMALL)
    style.configure('TPanedwindow', background=C['border'], sashwidth=ui_px(root, 3))
    style.configure("TNotebook", background=C["bg"], borderwidth=0,
                    tabmargins=(0, 0, 0, 0), bordercolor=C['border'],
                    lightcolor=C['border'], darkcolor=C['border'])
    style.configure("TNotebook.Tab", background=C["bg"], foreground=C["ter"],
                    padding=(12, 6), font=FONT_SMALL, borderwidth=0,
                    bordercolor=C['border'], lightcolor=C['bg'], darkcolor=C['bg'])
    style.map("TNotebook.Tab",
              background=[("selected", C["accent_soft"]), ("active", C["hover"])],
              foreground=[("selected", C["accent_text"]), ("active", C["text"])],
              lightcolor=[('selected', C['accent_border']), ('!selected', C['border'])],
              darkcolor=[('selected', C['border']), ('!selected', C['bg'])],
              expand=[("selected", (0, 0, 0, 0))])
    style.configure("TFrame", background=C["bg"])
    style.configure("Vertical.TScrollbar", background=C["scroll"],
                    troughcolor=C["chat"], arrowcolor=C["ter"],
                    bordercolor=C["chat"], lightcolor=C["scroll"],
                    darkcolor=C["scroll"])
    style.map("Vertical.TScrollbar", background=[("active", C["ter"])])
    style.configure("TCombobox", fieldbackground=C["surface2"],
                    background=C["surface2"], foreground=C["body"],
                    arrowcolor=C["ter"], padding=4, bordercolor=C["border_hi"],
                    lightcolor=C["surface2"], darkcolor=C["surface2"],
                    relief=tk.FLAT, insertcolor=C["text"])
    style.map("TCombobox",
              fieldbackground=[("disabled", C['disabled_bg']), ("readonly", C["input_bg"])],
              foreground=[("disabled", C['disabled_fg']), ("readonly", C["body"])],
              bordercolor=[("focus", C["focus_ring"])])
    style.configure('TEntry', fieldbackground=C['input_bg'], foreground=C['text'],
                    insertcolor=C['text'], bordercolor=C['border'], padding=5,
                    lightcolor=C['input_bg'], darkcolor=C['input_bg'])
    style.configure('TSpinbox', fieldbackground=C['input_bg'], foreground=C['text'],
                    arrowcolor=C['subtext'], bordercolor=C['border'], padding=5)
    for name in ('TEntry', 'TSpinbox'):
        style.map(name, bordercolor=[('focus', C['focus_ring'])],
                  foreground=[('disabled', C['disabled_fg'])])
    style.configure('Treeview', background=C['input_bg'], fieldbackground=C['input_bg'],
                    foreground=C['body'], borderwidth=0, font=FONT_SMALL,
                    bordercolor=C['border'], lightcolor=C['input_bg'], darkcolor=C['input_bg'],
                    rowheight=ui_px(root, CONTROL_HEIGHT))
    style.configure('Treeview.Heading', background=C['surface'], foreground=C['subtext'],
                    relief=tk.FLAT, font=FONT_SMALL, padding=6)
    style.map('Treeview', background=[('selected', C['accent_soft'])],
              foreground=[('selected', C['accent_text'])])
    style.map('Treeview.Heading', background=[('active', C['hover'])])
    root.option_add("*TCombobox*Listbox.background", C["surface"])
    root.option_add("*TCombobox*Listbox.foreground", C["body"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["accent_soft"])
    root.option_add("*TCombobox*Listbox.selectForeground", C["accent_text"])

    # ─── Coding Mode 工作台 ttk 深色化（对齐自绘控件视觉）───
    # DevelopmentWorkbench 大量使用 ttk.Button / TCheckbutton /
    # TMenubutton，此前无样式定义，渲染成 clam 默认浅灰浮雕——
    # 深色界面上一片白块（「黑白无常」问题的根源）。
    style.configure('TButton', background=C['surface2'], foreground=C['body'],
                    relief=tk.FLAT, borderwidth=0, focuscolor=C['border'],
                    padding=(10, 4), font=FONT_SMALL, anchor='center',
                    bordercolor=C['border'], lightcolor=C['surface2'], darkcolor=C['surface2'])
    style.map('TButton',
              background=[('disabled', C['disabled_bg']),
                          ('pressed', C['control_pressed']),
                          ('active', C['hover'])],
              foreground=[('disabled', C['disabled_fg']),
                          ('active', C['text'])],
              lightcolor=[('pressed', C['control_pressed'])],
              darkcolor=[('pressed', C['control_pressed'])])
    style.configure('TCheckbutton', background=C['bg'], foreground=C['body'],
                    focuscolor=C['border'], font=FONT_SMALL, padding=2,
                    indicatorcolor=C['surface2'], indicatorforeground=C['text'])
    style.map('TCheckbutton',
              background=[('active', C['bg'])],
              foreground=[('disabled', C['disabled_fg']), ('active', C['text'])],
              indicatorcolor=[('selected', C['accent'])])
    style.configure('TMenubutton', background=C['surface2'], foreground=C['body'],
                    relief=tk.FLAT, borderwidth=0, padding=(10, 4),
                    font=FONT_SMALL)
    style.map('TMenubutton',
              background=[('disabled', C['disabled_bg']),
                          ('active', C['hover'])],
              foreground=[('disabled', C['disabled_fg']), ('active', C['text'])])
    style.configure('Horizontal.TScrollbar', background=C['scroll'],
                    troughcolor=C['chat'], arrowcolor=C['ter'],
                    bordercolor=C['chat'], lightcolor=C['scroll'],
                    darkcolor=C['scroll'])
    style.map('Horizontal.TScrollbar', background=[('active', C['ter'])])
    style.configure('TSeparator', background=C['border'])
    # Shared language, local Coding density; Work keeps its larger tree rows.
    style.configure('Coding.TNotebook', background=C['bg'], borderwidth=0)
    style.configure('Coding.TNotebook.Tab', padding=(10, 5), font=FONT_SMALL)
    style.configure('Explorer.TNotebook.Tab', padding=(4, 5), font=FONT_CAPTION)
    style.configure('Coding.Treeview', background=C['sidebar'], fieldbackground=C['sidebar'],
                    rowheight=ui_px(root, 25), font=FONT_SMALL)
    style.configure('Card.TFrame', background=C['surface'], borderwidth=1,
                    relief='solid', bordercolor=C['border'])
    style.configure('Card.TLabel', background=C['surface'], foreground=C['body'])
    style.configure('Muted.Card.TLabel', foreground=C['subtext'])
    style.configure('Primary.TButton', background=C['accent'], foreground=C['text'],
                    lightcolor=C['accent'], darkcolor=C['accent'], bordercolor=C['accent'])
    style.map('Primary.TButton', background=[('disabled', C['disabled_bg']),
        ('pressed', C['accent_pressed']), ('active', C['accent_hover'])],
        foreground=[('disabled', C['disabled_fg'])])
    # Slim, arrow-free tracks retain native trough/drag/keyboard behavior.
    for orient, sticky in (('Vertical', 'ns'), ('Horizontal', 'we')):
        name = orient + '.TScrollbar'
        style.configure(name, arrowsize=ui_px(root, 10), width=ui_px(root, 10))
        style.layout(name, [(orient + '.Scrollbar.trough', {'sticky': sticky,
            'children': [(orient + '.Scrollbar.thumb', {'expand': 1, 'sticky': 'nswe'})]})])
    return style


# ─── 代码高亮 ──────────────────────────────────────────────

_PY_KEYWORDS = {
    "and", "as", "assert", "async", "await", "break", "class", "continue", "def",
    "del", "elif", "else", "except", "finally", "for", "from", "global", "if",
    "import", "in", "is", "lambda", "nonlocal", "not", "or", "pass", "raise",
    "return", "try", "while", "with", "yield", "self", "None", "True", "False",
}


def setup_code_tags(text: tk.Text, *, font=None, bg=None):
    """注册代码高亮用的 tag（幂等）。"""
    base_bg = bg or C["code_bg"]
    text.configure(bg=base_bg, fg=C["code_plain"], font=font or FONT_MONO_SM,
                   insertbackground=C["text"], relief=tk.FLAT,
                   highlightthickness=0, bd=0, selectbackground=C["accent_soft"])
    text.tag_configure("ln", foreground=C["muted"], font=FONT_MONO_XS,
                       justify="right")
    text.tag_configure("kw", foreground=C["code_kw"])
    text.tag_configure("cls", foreground=C["code_cls"])
    text.tag_configure("fn", foreground=C["code_fn"])
    text.tag_configure("st", foreground=C["code_str"])
    text.tag_configure("cm", foreground=C["code_comment"])
    text.tag_configure("num", foreground=C["code_num"])
    text.tag_configure("op", foreground=C["code_op"] if "code_op" in C else C["body"])
    text.tag_configure("add", foreground=C["diff_add"], background=C["diff_add_bg"])
    text.tag_configure("del", foreground=C["diff_del"], background=C["diff_del_bg"])
    text.tag_configure("hunk", foreground=C["accent2"], background=C["diff_hunk_bg"])
    text.tag_configure("meta", foreground=C["muted"], font=FONT_MONO_XS)


_CODE_TOKEN_RE = None


def highlight_python(text: tk.Text, *, start="1.0", end="end-1c"):
    """极简 Python 高亮：字符串 → 注释 → 关键字 → 类名 → 函数名 → 数字。

    纯 tag 打点，不做语法分析；足够让代码面板像那么回事，且零依赖。
    """
    import re
    content = text.get(start, end)
    text.tag_remove("kw", start, end)
    text.tag_remove("cls", start, end)
    text.tag_remove("fn", start, end)
    text.tag_remove("st", start, end)
    text.tag_remove("cm", start, end)
    text.tag_remove("num", start, end)
    if not content:
        return
    patterns = [
        ("cm", r"#[^\n]*"),
        ("st", r"(\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?'''|f?\"(?:[^\"\\\n]|\\.)*\"|f?'(?:[^'\\\n]|\\.)*')"),
        ("kw", r"\b(?:" + "|".join(sorted(_PY_KEYWORDS)) + r")\b"),
        ("cls", r"(?<=\bclass\s)\w+"),
        ("fn", r"\bdef\s+(\w+)"),
        ("fn", r"\b([a-zA-Z_]\w*)\s*(?=\()"),
        ("num", r"\b\d+(?:\.\d+)?\b"),
    ]
    for tag, pattern in patterns:
        for m in re.finditer(pattern, content):
            if m.lastindex:
                s, e = m.span(1)
            else:
                s, e = m.span()
            text.tag_add(tag, f"1.0+{s}c", f"1.0+{e}c")


def gutter_lines(text: tk.Text, gutter_width: int = 4) -> str:
    """Generate gutter text (line numbers) synced to the main Text widget."""
    line_count = int(text.index("end-1c").split(".")[0])
    return "\n".join(f"{i:>{gutter_width}}" for i in range(1, line_count + 1))


def human_size(n: float) -> str:
    """1536 -> '1.5K'."""
    for unit, div in (("G", 1024 ** 3), ("M", 1024 ** 2), ("K", 1024)):
        if n >= div:
            return f"{n / div:.1f}{unit}"
    return f"{int(n)}B"


def rounded_label(parent, text, *, fill=None, outline=None, fg=None, font=None,
                  radius=R_PILL, padx=10, pady=4, command=None, bg=None,
                  tooltip=None):
    """圆角小胶囊（模型选择器 / 附件 / 智能路由 这类 pill）。

    返回一个 Canvas，可用 `.set_text()` 改文字。
    """
    from tkinter import font as tkfont
    base = bg or _bg_of(parent)
    fnt = tkfont.Font(font=font or FONT_SMALL)
    icon, label = split_icon_text(i18n.resolve(text, parent))
    w = fnt.measure(label) + padx * 2 + (24 if icon else 0)
    h = max(CONTROL_HEIGHT if command else 0,
            max(fnt.metrics("linespace"), 20 if icon else 0) + pady * 2)
    cv = tk.Canvas(parent, width=w, height=h, bg=base, highlightthickness=0, bd=0)
    fill_c = fill or C["surface2"]
    shape = round_rect(cv, 0.5, 0.5, w - 0.5, h - 0.5, radius, fill=fill_c,
                       outline=outline or "", width=1)
    txt = cv.create_text(padx + (24 if icon else 0), h / 2, text=label, anchor="w",
                         fill=fg or C["body"], font=font or FONT_SMALL)
    if icon:
        draw_icon(cv, icon, x=padx, y=(h-20)/2, size=20, fg=fg or C["body"])

    def set_text(new):
        i18n.remember(cv, new, method="set_text")
        icon, label = split_icon_text(i18n.resolve(new, cv))
        cv.itemconfigure(txt, text=label)
        nw = fnt.measure(label) + padx * 2 + (24 if icon else 0)
        cv.configure(width=nw)
        cv.coords(txt, padx + (24 if icon else 0), h / 2)
        cv.coords(shape, *rounded_points(0.5, 0.5, nw - 0.5, h - 0.5, radius))
        cv.delete("icon")
        if icon:
            draw_icon(cv, icon, x=padx, y=(h-20)/2, size=20, fg=fg or C["body"])

    cv.set_text = set_text  # type: ignore[attr-defined]
    i18n.remember(cv, text, method="set_text")
    if command is not None:
        bind_keyboard_action(cv, command)
        cv.bind("<Button-1>", lambda _e: command())
        cv.configure(cursor="hand2")
    if tooltip:
        attach_tooltip(cv, tooltip)
    return cv


def progress_bar(parent, pct, *, width=28, height=3, bg=None, color=None):
    """顶部指标条：28x3 圆角槽 + 填充。"""
    base = bg or _bg_of(parent)
    cv = tk.Canvas(parent, width=width, height=height, bg=base,
                   highlightthickness=0, bd=0)
    round_rect(cv, 0, 0, width, height, height / 2, fill=C["border_hi"], outline="")
    filled = max(1, int(width * max(0.0, min(100.0, pct)) / 100.0))
    if pct and pct > 0:
        round_rect(cv, 0, 0, filled, height, height / 2, fill=color or C["accent_hover"],
                   outline="")
    return cv
