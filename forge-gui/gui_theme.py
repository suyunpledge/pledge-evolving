"""forge 桌面端设计系统（v2：深色 / Indigo 主题）。

单一来源：所有配色、字体、圆角、间距、通用绘制原语都放这里，
`forge_gui_v2.py`、`workspace.py`、`chat_widgets.py` 全部从这里取，
避免三处各写一套 token。

设计口径（对照参考稿实测）：
    背景层级    #000000 底 → #0B0B10 侧栏 → #0E0E13 主背景 → #111117 对话面板
                → #15151C 二级卡片 → #1A1A22 卡片/气泡 → #1E1E2A 选中态
    线条        #23232C 分隔线 / #2A2A34 卡片描边 / #34344A 选中描边
    主色        #4F46E5（Indigo），亮态 #6366F1，次级 #7C7CF0
    语义色      成功 #22C55E / 警告 #F59E0B / 危险 #EF4444
    文字        #E9E9F0 标题 / #D2D2DC 正文 / #9A9AA8 次要 / #6B6B78 弱化

零依赖：只用 tkinter（Python 自带）。
"""
from __future__ import annotations

import platform
import tkinter as tk
from tkinter import ttk

IS_WINDOWS = platform.system() == "Windows"

# ─── 调色板 ────────────────────────────────────────────────

C: dict[str, str] = {
    # 背景层级
    "bg": "#0E0E13",
    "sidebar": "#0B0B10",
    "chat": "#111117",
    "surface": "#1A1A22",
    "surface2": "#15151C",
    "surface_subtle": "#15151C",
    "hover": "#1E1E2A",
    "sel": "#1E1E2A",
    "code_bg": "#0D0D12",
    "input_bg": "#16161C",
    "black": "#000000",

    # 线条
    "border": "#23232C",
    "border_hi": "#2A2A34",
    "sel_border": "#34344A",
    "scroll": "#3A3A46",

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
    "muted": "#6B6B78",
    "ter": "#8A8A96",
    "placeholder": "#6B6B78",
    "comment": "#5C6370",

    # 消息
    "msg_user_bg": "#1A1A22",
    "msg_agent_bg": "#111117",

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
PAD_XS, PAD_S, PAD_M, PAD_L, PAD_XL = 4, 8, 12, 16, 20

# ─── 字体 ──────────────────────────────────────────────────
# 参考稿用 Inter / JetBrains Mono；本机没有，退回 Windows 自带等价字体
# （Segoe UI / Microsoft YaHei UI 承担拉丁+中文，Cascadia Code 承担等宽）。

UI_FAMILY = "Segoe UI" if IS_WINDOWS else "Helvetica"
CJK_FAMILY = "Microsoft YaHei UI" if IS_WINDOWS else UI_FAMILY
MONO_FAMILY = "Cascadia Code" if IS_WINDOWS else "Menlo"

FONT_UI = (UI_FAMILY, 10)
FONT_UI_BOLD = (UI_FAMILY, 10, "bold")
FONT_UI_MED = (UI_FAMILY, 10, "normal")
FONT_TITLE = (UI_FAMILY, 13, "bold")
FONT_SECTION = (UI_FAMILY, 11, "bold")
FONT_SMALL = (UI_FAMILY, 9)
FONT_CAPTION = (UI_FAMILY, 8)
FONT_MICRO = (UI_FAMILY, 7)
FONT_BRAND = (UI_FAMILY, 15, "bold")
FONT_MONO = (MONO_FAMILY, 10)
FONT_MONO_SM = (MONO_FAMILY, 9)
FONT_MONO_XS = (MONO_FAMILY, 8)
FONT_MONO_BOLD = (MONO_FAMILY, 10, "bold")
FONT_GLYPH = (UI_FAMILY, 11)


# ─── 绘制原语 ──────────────────────────────────────────────


def rounded_points(x1, y1, x2, y2, r):
    """圆角矩形的平滑多边形顶点（配合 create_polygon(smooth=True)）。"""
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
            x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
            x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kw):
    """在 Canvas 上画一个圆角矩形，返回 item id。"""
    return canvas.create_polygon(rounded_points(x1, y1, x2, y2, r),
                                 smooth=True, splinesteps=18, **kw)


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
        self._cv = tk.Canvas(self, bg=outer_bg, highlightthickness=0, bd=0,
                             height=1, width=1)
        self._cv.pack(fill=tk.BOTH, expand=True)
        self.content = tk.Frame(self._cv, bg=self._fill)
        self._win = self._cv.create_window(0, 0, window=self.content, anchor="nw")
        self._shape = None
        self._cv.bind("<Configure>", self._on_canvas)
        self.content.bind("<Configure>", self._on_content)
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
        if not self._autosize_width:
            self._cv.itemconfigure(self._win, width=max(1, w - self._padx * 2))
        self._draw(w, max(1, self._cv.winfo_height()))

    def _on_content(self, _event=None):
        try:
            need_h = self.content.winfo_reqheight() + self._pady * 2
            if abs(self._cv.winfo_reqheight() - need_h) > 1:
                self._cv.configure(height=need_h)
            if self._autosize_width:
                need_w = self.content.winfo_reqwidth() + self._padx * 2
                if abs(self._cv.winfo_reqwidth() - need_w) > 1:
                    self._cv.configure(width=need_w)
            self._cv.after_idle(lambda: self._draw(self._cv.winfo_width(),
                                                   self._cv.winfo_height()))
        except tk.TclError:
            pass

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
    btn = tk.Button(parent, text=label, command=command, bg=bgb, fg=fg,
                    activebackground=hovbg, activeforeground=hovfg,
                    font=font or FONT_SMALL, relief=tk.FLAT, bd=0,
                    padx=padx, pady=3, cursor="hand2", highlightthickness=0,
                    state=state)
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
    btn = tk.Button(parent, text=glyph, command=command, bg=base,
                    fg=fg or C["ter"], activebackground=hover or C["hover"],
                    activeforeground=C["text"], font=(UI_FAMILY, size),
                    relief=tk.FLAT, bd=0, padx=6, pady=1, cursor="hand2",
                    highlightthickness=0)
    if tooltip:
        attach_tooltip(btn, tooltip)
    return btn


def attach_tooltip(widget, text: str):
    tip = {"win": None}

    def show(_e=None):
        if tip["win"] is not None:
            return
        win = tk.Toplevel(widget)
        win.wm_overrideredirect(True)
        win.configure(bg=C["border_hi"])
        tk.Label(win, text=text, bg=C["surface"], fg=C["body"], font=FONT_MICRO,
                 padx=8, pady=4).pack(padx=1, pady=1)
        x = widget.winfo_rootx() + 12
        y = widget.winfo_rooty() + widget.winfo_height() + 6
        win.wm_geometry(f"+{x}+{y}")
        tip["win"] = win

    def hide(_e=None):
        if tip["win"] is not None:
            tip["win"].destroy()
            tip["win"] = None

    widget.bind("<Enter>", show, add="+")
    widget.bind("<Leave>", hide, add="+")
    widget.bind("<Button-1>", hide, add="+")


def avatar(parent, *, size=32, glyph="F", fg="#FFFFFF", fill=None,
           shape="rounded", bg=None, image=None):
    """头像：圆形（用户）或圆角方（Forge）。给 image 时直接画该位图（品牌标志）。"""
    base = bg or _bg_of(parent)
    cv = tk.Canvas(parent, width=size, height=size, bg=base,
                   highlightthickness=0, bd=0)
    if image is not None:
        cv.create_image(size / 2, size / 2, image=image)
        cv.image = image          # 防 GC
        return cv
    fill = fill or C["accent"]
    if shape == "circle":
        cv.create_oval(0, 0, size - 1, size - 1, fill=fill, outline="")
    else:
        round_rect(cv, 0, 0, size - 1, size - 1, int(size * 0.28), fill=fill,
                   outline="")
        # 左上高光：一块更亮的圆角三角，模拟参考稿的紫色渐变
        cv.create_polygon(2, 2, size - 6, 2, 2, size - 6,
                          smooth=True, splinesteps=12, fill="#6D63F0", outline="")
    cv.create_text(size / 2, size / 2, text=glyph, fill=fg,
                   font=(UI_FAMILY, max(8, int(size * 0.42)), "bold"))
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
    txt = cv.create_text(size / 2, size / 2, text=glyph, fill=fg,
                         font=(UI_FAMILY, glyph_size or max(9, int(size * 0.38)), "bold"))
    cv.bind("<Button-1>", lambda _e: command())
    cv.bind("<Enter>", lambda _e: cv.itemconfigure(oval, fill=hov))
    cv.bind("<Leave>", lambda _e: cv.itemconfigure(oval, fill=fill))
    if tooltip:
        attach_tooltip(cv, tooltip)
    cv._palette = (fill, hov)  # type: ignore[attr-defined]
    cv._oval = oval  # type: ignore[attr-defined]
    cv._glyph = txt  # type: ignore[attr-defined]
    return cv


def circle_button_state(cv, kind: str):
    """切换圆形按钮状态（primary/muted/danger）。"""
    palettes = {
        "primary": (C["accent"], "#FFFFFF", C["accent_hover"]),
        "muted": (C["surface2"], C["muted"], C["hover"]),
        "danger": (C["surface2"], C["error"], C["error_soft"]),
    }
    fill, fg, hov = palettes.get(kind, palettes["primary"])
    cv.itemconfigure(cv._oval, fill=fill)          # type: ignore[attr-defined]
    cv.itemconfigure(cv._glyph, fill=fg)           # type: ignore[attr-defined]
    cv._palette = (fill, hov)                      # type: ignore[attr-defined]


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
    w = fnt.measure(text) + padx * 2
    h = fnt.metrics("linespace") + pady * 2
    cv = tk.Canvas(parent, width=w, height=h, bg=base, highlightthickness=0, bd=0)
    fill_c = fill or C["surface2"]
    shape = round_rect(cv, 0.5, 0.5, w - 0.5, h - 0.5, radius, fill=fill_c,
                       outline=outline or "", width=1)
    txt = cv.create_text(w / 2, h / 2, text=text, fill=fg or C["body"], font=font or FONT_SMALL)

    def set_text(new):
        cv.itemconfigure(txt, text=new)
        nw = tkfont.Font(font=font or FONT_SMALL).measure(new) + padx * 2
        cv.configure(width=nw)
        cv.coords(txt, nw / 2, h / 2)
        cv.coords(shape, *rounded_points(0.5, 0.5, nw - 0.5, h - 0.5, radius))

    cv.set_text = set_text  # type: ignore[attr-defined]
    if command is not None:
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
