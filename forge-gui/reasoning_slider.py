# -*- coding: utf-8 -*-
"""Codex 风格的「思考强度」控制卡。

它不是系统设置式的细刻度条，而是 Composer 模型配置中的一等控件：当前档位和
模型在上方，渐变能量轨道居中，大号旋钮吸附到五个离散档位。视觉参考 Codex，
但颜色、层级和字体仍服从 Forge 的深色设计系统。

交互（都吸附到最近的档位，不做连续取值）：

- 点击轨道任意位置 → 跳到最近档
- 按住圆点拖动 → 实时跟手，松手落定
- 鼠标滚轮 / ↑↓←→ → 走一档；Home / End → 首尾档
- 点档位标签 → 直接选该档

不关闭外层弹层：滑杆必须能连续拖，选完就关弹层会毁掉手感（这是与「点选按钮」最主要的差别）。
"""
from __future__ import annotations

import tkinter as tk

from gui_theme import C, FONT_CAPTION, FONT_MICRO, FONT_UI_BOLD, round_rect

TRACK_HEIGHT = 14
CARD_HEIGHT = 126
CARD_RADIUS = 14
TRACK_Y = 78
KNOB_RADIUS = 13
PAD_X = KNOB_RADIUS + 12
MINI_WIDTH = 34
MINI_HEIGHT = 10

GRADIENT_START = "#5865F2"
GRADIENT_MID = "#7C3AED"
GRADIENT_END = "#C026D3"


def _mix(first: str, second: str, amount: float) -> str:
    """Return a Tk color between two #RRGGBB colors."""
    amount = max(0.0, min(1.0, amount))
    a = tuple(int(first[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(second[i:i + 2], 16) for i in (1, 3, 5))
    rgb = tuple(round(x + (y - x) * amount) for x, y in zip(a, b))
    return "#" + "".join(f"{channel:02x}" for channel in rgb)


def _gradient_color(amount: float) -> str:
    if amount <= .52:
        return _mix(GRADIENT_START, GRADIENT_MID, amount / .52)
    return _mix(GRADIENT_MID, GRADIENT_END, (amount - .52) / .48)


def _set_bg(widget, color: str) -> None:
    """Avoid turning a paint into another <Configure> event on Windows Tk."""
    try:
        if str(widget.cget("bg")).lower() != color.lower():
            widget.configure(bg=color)
    except tk.TclError:
        pass


class ReasoningSlider(tk.Frame):
    """离散思考强度滑杆。

    ``textvariable`` 与 ``choices`` 是唯一数据来源；``choices`` 为
    ``(value, label, hint)`` 序列，顺序即档位顺序。
    """

    def __init__(self, parent, textvariable, choices, *, on_select=None, bg=None,
                 model_getter=None, default_value="medium"):
        base = bg or C["surface"]
        super().__init__(parent, bg=base)
        self.var = textvariable
        self.stops = tuple(value for value, _label, _hint in choices)
        self._labels_text = {value: label for value, label, _hint in choices}
        self._hints = {value: hint for value, _label, hint in choices}
        self._on_select = on_select
        self._model_getter = model_getter
        self.default_value = (default_value if default_value in self.stops else
                              (self.stops[0] if self.stops else ""))
        self._bg = base
        self._hover_part = ""
        self._dragging = False
        self._width = 0
        self._pad = float(PAD_X)

        self.canvas = tk.Canvas(self, bg=base, highlightthickness=0, bd=0,
                                height=CARD_HEIGHT, takefocus=1, cursor="hand2")
        self.canvas.pack(fill=tk.X)

        # 保留既有自动化/兼容接口。可见标签已经收进卡片标题，隐藏的 Label 仍让
        # 调用方能按 value 查到名称，不需要依赖 Canvas 内部 item id。
        self.label_widgets: dict[str, tuple[tk.Frame, tk.Label]] = {}
        for value in self.stops:
            host = tk.Frame(self, bg=base, cursor="hand2")
            text = tk.Label(host, text=self._labels_text[value], bg=base,
                            fg=C["muted"], font=FONT_MICRO, cursor="hand2")
            for widget in (host, text):
                widget.bind("<Button-1>", lambda _e, v=value: self.pick(v))
            self.label_widgets[value] = (host, text)

        self.hint_label = tk.Label(self, text="", bg=base, fg=C["muted"],
                                   font=FONT_MICRO, anchor="w", justify=tk.LEFT)

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda _e: self._set_hover_part(""))
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Left>", lambda _e: self.step(-1))
        self.canvas.bind("<Right>", lambda _e: self.step(1))
        self.canvas.bind("<Up>", lambda _e: self.step(1))
        self.canvas.bind("<Down>", lambda _e: self.step(-1))
        self.canvas.bind("<Home>", lambda _e: self.set_index(0))
        self.canvas.bind("<End>", lambda _e: self.set_index(len(self.stops) - 1))
        self.canvas.bind("<FocusIn>", lambda _e: self._paint())
        self.canvas.bind("<FocusOut>", lambda _e: self._paint())

        self._trace = self.var.trace_add("write", lambda *_: self.refresh())
        self.refresh()

    # ── 取值 ────────────────────────────────────────────
    def current_index(self) -> int:
        value = self.var.get()
        try:
            return self.stops.index(value)
        except ValueError:
            return 0

    def current_value(self) -> str:
        return self.stops[self.current_index()] if self.stops else ""

    def set_index(self, index: int, *, notify: bool = True) -> None:
        if not self.stops:
            return
        index = max(0, min(len(self.stops) - 1, int(index)))
        value = self.stops[index]
        if value != self.var.get():
            if notify and self._on_select is not None:
                self._on_select(value)
            else:
                self.var.set(value)
        self.refresh()

    def step(self, delta: int) -> None:
        self.set_index(self.current_index() + delta)

    def pick(self, value: str) -> None:
        """按档位取值（点标签时用）。

        顺带把键盘焦点交给滑杆：用户点完档位后，方向键能接着微调。
        """
        if value in self.stops:
            try:
                self.canvas.focus_set()
            except tk.TclError:
                pass
            self.set_index(self.stops.index(value))

    # ── 存活 ────────────────────────────────────────────
    def _alive(self) -> bool:
        """弹层销毁后 Tk 仍可能把排队的 <Configure>/trace 回调送过来。

        不判存活就会出现 ``invalid command name ...reasoningslider.!canvas``。
        """
        try:
            return bool(self.winfo_exists()) and bool(self.canvas.winfo_exists())
        except tk.TclError:
            return False

    def refresh(self) -> None:
        if not self._alive():
            return
        self._paint()

    # ── 几何 ────────────────────────────────────────────
    def _x_for(self, index: int) -> float:
        width = self._width or self.canvas.winfo_width() or 1
        span = max(1.0, width - self._pad * 2)
        return self._pad + (span * index / (len(self.stops) - 1) if len(self.stops) > 1 else 0)

    def _index_for(self, x: float) -> int:
        width = self._width or self.canvas.winfo_width() or 1
        span = max(1.0, width - self._pad * 2)
        if len(self.stops) < 2:
            return 0
        ratio = (x - self._pad) / span
        return max(0, min(len(self.stops) - 1, int(round(ratio * (len(self.stops) - 1)))))

    def _on_configure(self, event) -> None:
        if not self._alive():
            return
        self._width = event.width
        self._paint()

    # ── 交互 ────────────────────────────────────────────
    def _hit_part(self, x: float, y: float) -> str:
        width = self._width or self.canvas.winfo_width() or 1
        if x >= width - 54 and y <= 58:
            return "reset"
        if TRACK_Y - 25 <= y <= TRACK_Y + 25:
            return "track"
        return ""

    def _set_hover_part(self, part: str) -> None:
        if part == self._hover_part:
            return
        self._hover_part = part
        self.canvas.configure(cursor="hand2" if part else "arrow")
        self._paint()

    def _on_motion(self, event) -> None:
        self._set_hover_part("track" if self._dragging else
                             self._hit_part(event.x, event.y))

    def _on_press(self, event) -> str:
        part = self._hit_part(event.x, event.y)
        if part == "reset":
            self.reset()
            return "break"
        if part != "track":
            return "break"
        self._dragging = True
        self.canvas.focus_set()
        self.set_index(self._index_for(event.x))
        return "break"

    def _on_drag(self, event) -> str:
        if self._dragging:
            self.set_index(self._index_for(event.x))
        return "break"

    def _on_release(self, _event) -> str:
        self._dragging = False
        self._paint()
        return "break"

    def _on_wheel(self, event) -> str:
        self.step(-1 if event.delta > 0 else 1)
        return "break"

    def reset(self) -> None:
        """回到产品默认的「中」档；弹层保持打开。"""
        if self.default_value in self.stops:
            self.set_index(self.stops.index(self.default_value))

    # ── 绘制 ────────────────────────────────────────────
    def _paint(self) -> None:
        if not self._alive():
            return
        try:
            self._paint_inner()
        except tk.TclError:
            # 正在销毁的最后一帧：忽略，不再抛给 report_callback_exception
            pass

    def _paint_inner(self) -> None:
        c = self.canvas
        c.delete("all")
        if not self.stops:
            return
        width = max(160, self._width or c.winfo_width() or 1)
        index = self.current_index()
        knob_x = self._x_for(index)
        base = self._bg
        _set_bg(c, base)
        self._retint(base)

        # 卡片：圆角、克制描边，悬停只在控件内部反馈。
        card_fill = C["hover"] if self._hover_part else C["surface2"]
        round_rect(c, 1, 1, width - 1, CARD_HEIGHT - 1, CARD_RADIUS,
                   fill=card_fill,
                   outline=C["accent_border"] if c.focus_get() is c else C["border_hi"],
                   width=1, tags=("slider-card",))

        # 顶部信息层：闪电 / 当前档位 / 当前模型 / 复位。
        c.create_polygon(24, 19, 17, 31, 23, 31, 19, 43, 33, 27, 26, 27,
                         fill=C["subtext"], outline="", tags=("slider-bolt",))
        active_label = self._labels_text.get(self.current_value(), self.current_value())
        c.create_text(width / 2, 23, text=f"{active_label}  ›",
                      fill=C["accent_text"], font=FONT_UI_BOLD,
                      tags=("slider-title",))
        model = ""
        if callable(self._model_getter):
            try:
                model = str(self._model_getter() or "")
            except (tk.TclError, TypeError, ValueError):
                model = ""
        c.create_text(width / 2, 43, text=model or "当前模型",
                      fill=C["subtext"], font=FONT_CAPTION,
                      tags=("slider-model",))
        reset_color = (C["body"] if self._hover_part == "reset" else
                       (C["muted"] if self.current_value() == self.default_value
                        else C["subtext"]))
        c.create_arc(width - 37, 20, width - 17, 40, start=36, extent=285,
                     style=tk.ARC, outline=reset_color, width=2,
                     tags=("slider-reset",))
        c.create_polygon(width - 18, 19, width - 13, 24, width - 20, 25,
                         fill=reset_color, outline="", tags=("slider-reset",))

        pad = self._pad
        track_end = width - pad
        c.create_line(pad, TRACK_Y, track_end, TRACK_Y, fill=C["border_hi"],
                      width=TRACK_HEIGHT, capstyle=tk.ROUND)

        # Tk Canvas 没有原生渐变，用短线段构成蓝 → 紫 → 品红的能量轨道。
        active_end = max(pad, knob_x)
        span = max(1.0, track_end - pad)
        x = int(pad)
        while x < int(active_end):
            next_x = min(x + 2, active_end)
            color = _gradient_color((x - pad) / span)
            c.create_line(x, TRACK_Y, next_x, TRACK_Y, fill=color,
                          width=TRACK_HEIGHT, capstyle=tk.BUTT,
                          tags=("slider-track", "slider-track-active"))
            x += 2
        start_color = _gradient_color(0)
        end_color = _gradient_color(max(0.0, (active_end - pad) / span))
        track_radius = TRACK_HEIGHT / 2
        c.create_oval(pad - track_radius, TRACK_Y - track_radius,
                      pad + track_radius, TRACK_Y + track_radius,
                      fill=start_color, outline="", tags=("slider-track-active",))
        if active_end > pad + 1:
            c.create_oval(active_end - track_radius, TRACK_Y - track_radius,
                          active_end + track_radius, TRACK_Y + track_radius,
                          fill=end_color, outline="", tags=("slider-track-active",))

        # 静态微光颗粒提供 Codex 参考图的“能量”质感，但不做动画，避免耗电与闪烁。
        sparkles = ((.10, -2, 1.1), (.18, 3, .8), (.31, -3, .9),
                    (.43, 2, 1.2), (.56, -1, .7), (.64, 3, 1.0),
                    (.78, -3, 1.0), (.88, 1, .7))
        for ratio, dy, size in sparkles:
            sx = pad + span * ratio
            if sx < knob_x - KNOB_RADIUS:
                c.create_oval(sx - size, TRACK_Y + dy - size,
                              sx + size, TRACK_Y + dy + size,
                              fill="#D8D4FF", outline="",
                              tags=("slider-sparkle",))

        # 五档仍然是离散值：用极弱的点标识吸附位置，主旋钮明显高于刻度。
        for i in range(len(self.stops)):
            sx = self._x_for(i)
            if abs(sx - knob_x) > KNOB_RADIUS:
                c.create_oval(sx - 1.3, TRACK_Y - 1.3, sx + 1.3, TRACK_Y + 1.3,
                              fill="#A5A0D8" if i <= index else C["muted"],
                              outline="", tags=("slider-stop",))

        radius = KNOB_RADIUS + (1 if (self._hover_part == "track" or self._dragging)
                                else 0)
        c.create_oval(knob_x - radius + 2, TRACK_Y - radius + 3,
                      knob_x + radius + 2, TRACK_Y + radius + 3,
                      fill="#09090D", outline="", tags=("slider-thumb-shadow",))
        if c.focus_get() is c:
            c.create_oval(knob_x - radius - 3, TRACK_Y - radius - 3,
                          knob_x + radius + 3, TRACK_Y + radius + 3,
                          outline=C["accent_border"], width=1)
        c.create_oval(knob_x - radius, TRACK_Y - radius,
                      knob_x + radius, TRACK_Y + radius,
                      fill="#F2F2F6", outline="#C9C8D2", width=1,
                      tags=("slider-thumb",))

        hint = self._hints.get(self.current_value(), "")
        c.create_text(width / 2, 108, text=hint or "拖动选择思考强度",
                      fill=C["subtext"] if hint else C["muted"],
                      font=FONT_MICRO, tags=("slider-hint",))
        self.hint_label.configure(text=hint, bg=base,
                                  fg=C["subtext"] if hint else C["muted"])

    def _retint(self, base: str) -> None:
        for widget in (self, self.hint_label):
            _set_bg(widget, base)


class MiniReasoningTrack(tk.Canvas):
    """收起态用的小滑杆示意：一条细轨 + 档位点 + 圆点。

    只做「现在在第几档」的可视化，不接交互（点它仍走外层胶囊的打开逻辑）。
    """

    def __init__(self, parent, choices, *, bg=None):
        base = bg or C["input_bg"]
        super().__init__(parent, width=MINI_WIDTH, height=MINI_HEIGHT, bg=base,
                         highlightthickness=0, bd=0)
        self._values = tuple(value for value, _label, _hint in choices)
        self._getter = None
        self._bg = base
        # 这是固定尺寸的收起态指示器。Windows Tk 上在 <Configure> 中删除并
        # 重建 Canvas item 会反复产生几何事件，导致 root.update() 不返回；
        # 值变化与父级 hover 已经会显式 refresh，因此这里不再监听 Configure。

    def follow(self, variable) -> None:
        """跟随一个 StringVar：值变了就重画。"""
        self._getter = lambda: variable.get()
        variable.trace_add("write", lambda *_: self.refresh())
        self.refresh()

    def refresh(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        base = self._bg
        if self._getter is not None:
            # 悬停态由外层统一换底色后再调一次
            try:
                base = self.master.cget("bg")
            except tk.TclError:
                base = self._bg
        _set_bg(self, base)
        self.delete("all")
        if not self._values:
            return
        current = 0
        if self._getter is not None:
            try:
                current = self._values.index(self._getter())
            except ValueError:
                current = 0
        width = self.winfo_width() or MINI_WIDTH
        mid = MINI_HEIGHT / 2
        pad = 2.0
        span = max(1.0, width - pad * 2)
        knob_x = pad + (span * current / (len(self._values) - 1)
                        if len(self._values) > 1 else 0)
        self.create_line(pad, mid, width - pad, mid, fill=C["border_hi"],
                         width=2, capstyle=tk.ROUND)
        if knob_x > pad + 0.5:
            x = int(pad)
            span = max(1.0, width - pad * 2)
            while x < int(knob_x):
                nx = min(x + 2, knob_x)
                self.create_line(x, mid, nx, mid,
                                 fill=_gradient_color((x - pad) / span), width=2)
                x += 2
        self.create_oval(knob_x - 2.2, mid - 2.2, knob_x + 2.2, mid + 2.2,
                         fill="#F2F2F6", outline=C["accent_border"], width=1)
