"""forge 桌面端设计系统（v2：深色 / Indigo 主题）。

单一来源：所有配色、字体、圆角、间距、通用绘制原语都放这里，
`forge_gui_v2.py`、`workspace.py`、`chat_widgets.py` 全部从这里取，
避免三处各写一套 token。

暖灰深色层级搭配 Indigo 操作色；绿色气泡区分用户消息。
正文与必要说明优先保证对比度。自绘控件提供键盘激活和可见焦点，
常用操作保持足够热区，工具提示延迟出现并随控件销毁而清理。

零依赖：只用 tkinter（Python 自带）。
"""
from __future__ import annotations

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
    "bg": "#141414",            # 主背景（暖灰黑）
    "activity": "#0F0F0F",
    "sidebar": "#1C1C1C",
    "sidebar_history": "#171717",
    "chat": "#141414",
    "surface": "#232323",
    "surface2": "#1C1C1C",
    "surface_subtle": "#1F1F1F",
    "hover": "#262626",
    "sel": "#2B2940",
    "code_bg": "#1A1A1A",
    "input_bg": "#1F1F1F",
    "black": "#000000",

    # 线条
    "border": "#2A2A2A",
    "border_hi": "#333333",
    "sel_border": "#3A3A3A",
    "scroll": "#3A3A3A",

    # 主色
    "accent": "#4F46E5",
    "accent_hover": "#6366F1",
    "accent2": "#7C7CF0",
    "accent_soft": "#232145",
    "accent_border": "#3D3782",
    "accent_text": "#C7C7FF",

    # 语义
    "ok": "#22C55E",
    "ok_soft": "#132C1C",
    "warn": "#F59E0B",
    "warn_soft": "#2E2410",
    "error": "#EF4444",
    "error_soft": "#2E1315",
    "info": "#7C7CF0",
    "info_soft": "#1C1B33",
    "link": "#7C7CF0",
    "link_soft": "#1C1B33",

    # 文字
    "text": "#E9E9F0",
    "body": "#D2D2DC",
    "subtext": "#9A9AA8",
    "muted": "#9595A3",
    "ter": "#8A8A96",
    "placeholder": "#9595A3",
    "comment": "#5C6370",

    # 消息气泡：两侧要能看出「谁在说」，又不能让深色界面变花。
    # user 带一点主色倾向（“我说的话”），agent 用中性面；两者都配一道 hairline 描边。
    "msg_user_bg": "#206D3B",         # 保留绿色角色区分，白色正文有足够对比度
    "msg_user_fg": "#FFFFFF",          # 用户气泡文字（白）
    "msg_agent_bg": "#2A2A2A",         # AI 深灰气泡
    "msg_agent_fg": "#E8E8E8",         # AI 气泡文字（浅灰）
    "msg_user_bg_old": "#232134",
    "msg_agent_bg_old": "#1A1A22",
    "msg_user_border": "#34834E",
    "msg_agent_border": "#383838",

    # 代码高亮（One Dark 近似）
    "code_kw": "#C678DD",
    "code_cls": "#E5C07B",
    "code_fn": "#61AFEF",
    "code_str": "#98C379",
    "code_const": "#E06C75",
    "code_num": "#D19A66",
    "code_comment": "#5C6370",
    "code_plain": "#D2D2DC",

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
R_BUBBLE = 22
PAD_XS, PAD_S, PAD_M, PAD_L, PAD_XL = 4, 8, 12, 16, 20

# 交互尺寸：所有主按钮、图标热区与浮层以此为基线，避免页面各写一套。
CONTROL_HEIGHT = 32
ICON_SIZE = 16
ICON_HIT_SIZE = 32
OVERLAY_WIDTH = 448
FOCUS_WIDTH = 1

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


def text_width(widget, text, font=FONT_UI):
    """Measure rendered text and emoji in pixels, without character rounding."""
    from ui_icons import emoji_parts
    measure = tkfont.Font(root=widget, font=font)
    widths = []
    for line in text.split("\n"):
        width = 0
        for part, is_emoji in emoji_parts(line):
            bitmap = emoji_image(widget, part, size=measure.metrics("linespace")) if is_emoji else None
            width += bitmap.width() if bitmap is not None else measure.measure(part)
        widths.append(width)
    return max(widths, default=0)


def bind_wrap(label):
    """Wrap inside a label's actual allocated width, including its own insets."""
    def fit(event):
        inset = sum(int(label.cget(key)) for key in ("padx", "borderwidth", "highlightthickness")) * 2
        width = max(1, event.width - inset)
        if int(label.cget("wraplength")) != width:
            label.configure(wraplength=width)
    label.bind("<Configure>", fit, add="+")
    return label


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
    label = f"{icon} {text}".strip() if icon else text
    btn = IconButton(parent, text=label, command=command, bg=bgb, fg=fg,
                    activebackground=hovbg, activeforeground=hovfg,
                    font=font or FONT_SMALL, relief=tk.FLAT, bd=0,
                    padx=padx, pady=5, cursor="hand2", highlightthickness=1,
                    highlightbackground=base, highlightcolor=C["accent2"],
                    state=state)
    btn.configure(disabledforeground=C["muted"])
    if width:
        btn.configure(width=width)
    if height:
        btn.configure(height=max(1, int(height / 16)))
    return btn


def chip(parent, text, *, selected=False, command=None, bg=None):
    """筛选 chip（参考稿「全部文件 / 已修改 2 / 新增 1」）。"""
    bgb = C["accent_soft"] if selected else C["surface2"]
    fg = C["accent_text"] if selected else C["ter"]
    lbl = tk.Label(parent, text=text, bg=bgb, fg=fg, font=FONT_MICRO,
                   padx=8, pady=3,
                   highlightthickness=1,
                   highlightbackground=C["accent"] if selected else C["border_hi"])
    if command is not None:
        lbl.bind("<Button-1>", lambda _e: command())
        lbl.configure(cursor="hand2")
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
    return tk.Label(parent, text=text, bg=bgb, fg=fg, font=FONT_MICRO,
                    padx=6, pady=2, highlightthickness=1, highlightbackground=fg)


def divider(parent, *, color=None, horizontal=True, bg=None):
    return tk.Frame(parent, bg=color or C["border"],
                    height=1 if horizontal else 0,
                    width=0 if horizontal else 1)


def glyph_button(parent, glyph, command, *, bg=None, fg=None, size=13,
                 hover=None, tooltip=None):
    """无边框的图标按钮（用于 ✕ / ⋯ / ⟳ 之类）。"""
    base = bg or _bg_of(parent)
    btn = IconButton(parent, text=glyph, command=command, bg=base,
                    fg=fg or C["ter"], activebackground=hover or C["hover"],
                    activeforeground=C["text"], font=(UI_FAMILY, size),
                    relief=tk.FLAT, bd=0, padx=8, pady=4, cursor="hand2",
                    highlightthickness=1, highlightbackground=base,
                    highlightcolor=C["accent2"])
    if tooltip:
        attach_tooltip(btn, tooltip)
    return btn


def bind_keyboard_action(widget, command):
    """Give custom clickable controls the same keyboard contract as buttons."""
    widget.configure(takefocus=1, highlightthickness=1,
                     highlightbackground=widget.cget("bg"),
                     highlightcolor=C["accent2"])

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
        win = tk.Toplevel(widget)
        win.wm_overrideredirect(True)
        win.configure(bg=C["border_hi"])
        tk.Label(win, text=text, bg=C["surface"], fg=C["body"], font=FONT_CAPTION,
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
    pop = tk.Toplevel(anchor)
    pop.withdraw()
    pop.overrideredirect(True)
    pop.configure(bg=C["border_hi"])
    anchor._forge_popover = pop
    shell = tk.Frame(pop, bg=C["surface"], padx=8, pady=8)
    shell.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)
    if title:
        tk.Label(shell, text=title, bg=C["surface"], fg=C["subtext"],
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
        label = tk.Label(text, text=str(item.get("label", "")), bg=base,
                         fg=C["error"] if item.get("danger") else C["text"],
                         font=FONT_SMALL, anchor="w", cursor="hand2")
        label.pack(fill=tk.X)
        detail = None
        if item.get("detail"):
            detail = tk.Label(text, text=str(item["detail"]), bg=base,
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


def circle_button(parent, glyph, command, *, size=30, kind="primary", bg=None,
                  glyph_size=None, tooltip=None):
    """圆形按钮（发送 ↑ / 停止 ■）。"""
    base = bg or _bg_of(parent)
    palettes = {
        "primary": (C["accent"], "#FFFFFF", C["accent_hover"]),
        "muted": (C["surface2"], C["muted"], C["hover"]),
        "danger": (C["surface2"], C["error"], C["error_soft"]),
    }
    fill, fg, hov = palettes.get(kind, palettes["primary"])
    cv = tk.Canvas(parent, width=size, height=size, bg=base,
                   highlightthickness=0, bd=0, cursor="hand2")
    oval = cv.create_oval(0, 0, size - 1, size - 1, fill=fill, outline="")
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
    bind_keyboard_action(cv, command)
    cv.bind("<Button-1>", lambda _e: command() if cv._enabled else None)
    cv.bind("<Enter>", lambda _e: cv.itemconfigure(oval, fill=cv._palette[1]) if cv._enabled else None)
    cv.bind("<Leave>", lambda _e: cv.itemconfigure(oval, fill=cv._palette[0]))
    if tooltip:
        attach_tooltip(cv, tooltip)
    cv._palette = (fill, hov)  # type: ignore[attr-defined]
    cv._oval = oval  # type: ignore[attr-defined]
    cv._glyph = txt  # type: ignore[attr-defined]
    return cv


def circle_button_state(cv, kind: str, *, enabled=None):
    """切换圆形按钮状态（primary/muted/danger）。"""
    palettes = {
        "primary": (C["accent"], "#FFFFFF", C["accent_hover"]),
        "muted": (C["surface2"], C["muted"], C["hover"]),
        "danger": (C["surface2"], C["error"], C["error_soft"]),
    }
    fill, fg, hov = palettes.get(kind, palettes["primary"])
    cv.itemconfigure(cv._oval, fill=fill)          # type: ignore[attr-defined]
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
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("TNotebook", background=C["bg"], borderwidth=0,
                    tabmargins=(0, 0, 0, 0))
    style.configure("TNotebook.Tab", background=C["bg"], foreground=C["ter"],
                    padding=(14, 6), font=FONT_UI_BOLD, borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", C["accent_soft"]), ("active", C["hover"])],
              foreground=[("selected", C["accent_text"]), ("active", C["text"])],
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
              fieldbackground=[("readonly", C["surface2"])],
              foreground=[("readonly", C["body"])],
              bordercolor=[("focus", C["accent"])])
    root.option_add("*TCombobox*Listbox.background", C["surface"])
    root.option_add("*TCombobox*Listbox.foreground", C["body"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["accent_soft"])
    root.option_add("*TCombobox*Listbox.selectForeground", C["accent_text"])
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
    icon, label = split_icon_text(text)
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
        icon, label = split_icon_text(new)
        cv.itemconfigure(txt, text=label)
        nw = fnt.measure(label) + padx * 2 + (24 if icon else 0)
        cv.configure(width=nw)
        cv.coords(txt, padx + (24 if icon else 0), h / 2)
        cv.coords(shape, *rounded_points(0.5, 0.5, nw - 0.5, h - 0.5, radius))
        cv.delete("icon")
        if icon:
            draw_icon(cv, icon, x=padx, y=(h-20)/2, size=20, fg=fg or C["body"])

    cv.set_text = set_text  # type: ignore[attr-defined]
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
