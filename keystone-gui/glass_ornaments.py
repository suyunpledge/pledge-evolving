"""液态玻璃界面的图案元素（2026-11-13）。

与 liquid_glass 同一套做法：标准库现场编码带 alpha 的 PNG，边缘抗锯齿
（Tk Canvas 在 Windows 上画圆不抗锯齿，小圆点、细圆环会出锯齿）。

- ``halo_photo``：弹窗图标背后的语义色光晕 + 两道同心细环；
- ``icon_chip``：玻璃小圆片 + 线性图标（菜单行首、标签内）；
- ``bead_strip_photo``：折叠标签里的状态珠串，每颗代表一步（成功 / 失败 /
  进行中），超出上限时末尾显示「+N」由调用方另配文字。

零依赖：只用 tkinter 与标准库。
"""
from __future__ import annotations

import base64
import math
import struct
import tkinter as tk
import zlib
from collections import OrderedDict

from gui_theme import C, FONT_MICRO, _bg_of, draw_icon, mix, ui_px
import liquid_glass as glass

_CACHE: OrderedDict = OrderedDict()


def _rgb(colour: str):
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


def _png(width: int, height: int, pixel) -> str:
    """pixel(x, y) -> (r, g, b, a) 浮点；编码为 RGBA PNG（base64）。"""
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            r, g, b, a = pixel(x + .5, y + .5)
            a = 0.0 if a < 0 else 1.0 if a > 1 else a
            rows += bytes((_c(r), _c(g), _c(b), _c(255 * a)))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
           + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode("ascii")


def _c(value) -> int:
    value = round(value)
    return 0 if value < 0 else 255 if value > 255 else value


def _cached(key, build):
    data = _CACHE.get(key)
    if data is None:
        data = build()
        _CACHE[key] = data
        while len(_CACHE) > 64:
            _CACHE.popitem(last=False)
    else:
        _CACHE.move_to_end(key)
    return data


def _ring(dist, radius, width):
    """细环覆盖率：|dist - radius| 落在线宽内为 1，向外 1px 线性抗锯齿。"""
    edge = abs(dist - radius) - width / 2
    return 1.0 if edge <= 0 else max(0.0, 1.0 - edge)


# ─── 弹窗光晕 ──────────────────────────────────────────────────


def halo_png(size: int, colour: str, *, core: float) -> str:
    """size×size 的光晕：中心柔光（半径 core 以外按平方衰减）+ 两道同心细环。

    细环亮度在左上方更高（与玻璃镜面边缘同一光源方向），像光晕被玻璃折射。
    """
    def build():
        r0, g0, b0 = _rgb(colour)
        c = size / 2
        rings = ((core * 1.42, 0.30), (core * 1.86, 0.16))

        def pixel(x, y):
            dx, dy = x - c, y - c
            dist = math.hypot(dx, dy)
            glow = 0.0
            if dist < c:
                t = max(0.0, (dist - core * .55) / (c - core * .55))
                glow = 0.34 * (1 - t) ** 2.4
            light = 0.55 + 0.45 * max(0.0, -(dx * .38 + dy * .925) / (dist or 1))
            ring = max(_ring(dist, radius, 1.1) * alpha * light for radius, alpha in rings)
            # HUD 刻度盘（科幻感）：外环上每 15° 一道短刻度、每 45° 一道长刻度；
            # 内环左上 70° 一段更亮的弧，像正在扫描的指示段。
            angle = math.degrees(math.atan2(dy, dx)) % 360
            outer = rings[1][0]
            step = angle % 15
            off = min(step, 15 - step) * math.pi / 180 * dist      # 到最近刻度线的弧长（px）
            major = min(angle % 45, 45 - angle % 45) < 7.5
            half = 3.2 if major else 1.8
            if abs(dist - outer) <= half + 1 and off < 1.6:
                tick = max(0.0, 1 - max(0.0, off - .55)) * max(0.0, min(1.0, half + .5 - abs(dist - outer)))
                ring = max(ring, tick * (.62 if major else .34) * light)
            if 200 <= angle <= 270:
                ring = max(ring, _ring(dist, rings[0][0], 1.6) * .85)
            a = glow + ring * (1 - glow)
            if a <= 0:
                return 0, 0, 0, 0
            # 环比柔光略亮一点（向白混 25%），读起来像一道细亮线。
            k = ring * (1 - glow) / a * .25
            return r0 + (255 - r0) * k, g0 + (255 - g0) * k, b0 + (255 - b0) * k, a
        return _png(size, size, pixel)
    return _cached(("halo2", size, colour, round(core, 1)), build)


def halo_badge(parent, icon: str, *, tone: str, fill: str, tile: int, size: int):
    """弹窗顶部的语义图标：光晕 + 同心环 + 玻璃圆片 + 线性图标。"""
    cv = tk.Canvas(parent, width=size, height=size, bg=fill, highlightthickness=0, bd=0)
    cv._halo = tk.PhotoImage(master=cv, data=halo_png(size, tone, core=tile / 2))
    cv.create_image(size / 2, size / 2, image=cv._halo)
    cv._glass = glass.glass_photo(cv, tile, tile, glass.GlassSpec(
        radius=tile / 2, fill=mix(fill, tone, .22), rim=.95, rim_colour=tone,
        glow=1.1, band=5, sheen=1.0, sheen_h=tile * .45, inner_line=.05))
    cv.create_image(size / 2, size / 2, image=cv._glass)
    glyph = round(tile * .48)
    draw_icon(cv, icon, x=(size - glyph) / 2, y=(size - glyph) / 2, size=glyph, fg=tone)
    return cv


# ─── 玻璃图标圆片 ──────────────────────────────────────────────


def icon_chip(parent, icon: str, *, size: int | None = None, tone: str | None = None,
              bg: str | None = None):
    """菜单行首 / 标签里的小玻璃圆片：语义色淡底 + 亮边 + 线性图标。"""
    base = bg or _bg_of(parent)
    size = size or ui_px(parent, 24)
    tone = tone or C["subtext"]
    cv = tk.Canvas(parent, width=size, height=size, bg=base, highlightthickness=0, bd=0)
    cv._chip_spec = glass.GlassSpec(radius=size / 2, fill=mix(base, tone, .14), rim=.75,
                                    rim_colour=mix(tone, "#FFFFFF", .35), glow=.9, band=3,
                                    sheen=.9, sheen_h=size * .45, inner_line=.04)
    cv._chip_icon, cv._chip_tone = icon, tone
    _paint_chip(cv, base)
    return cv


def _paint_chip(cv, base):
    size = int(cv.cget("width"))
    cv.delete("all")
    cv.configure(bg=base)
    spec = cv._chip_spec
    cv._glass = glass.glass_photo(cv, size, size, spec)
    cv.create_image(0, 0, image=cv._glass, anchor="nw")
    glyph = round(size * .56)
    draw_icon(cv, cv._chip_icon, x=(size - glyph) / 2, y=(size - glyph) / 2, size=glyph,
              fg=cv._chip_tone)


def set_chip_bg(cv, base: str):
    """行悬停换底色时同步圆片外缘（透明角露出的是 Canvas 底色）。"""
    if str(cv.cget("bg")).upper() != base.upper():
        _paint_chip(cv, base)


# ─── 状态珠串 ──────────────────────────────────────────────────

BEAD_LIMIT = 10


def bead_strip_png(states: tuple, *, bead: int, gap: int, height: int) -> str:
    """一排抗锯齿小圆珠。states 元素：'ok' / 'fail' / 'run' / 'todo'。

    成功 = 实心绿、失败 = 实心琥珀、进行中 = 熔铁色、未完成 = 空心细环；
    每颗珠子左上角一点高光，与玻璃同一光源。
    """
    def build():
        colours = {"ok": C["ok"], "fail": C["warn"], "run": C["ember"], "todo": C["faint"]}
        width = max(1, len(states) * bead + max(0, len(states) - 1) * gap)
        r = bead / 2
        cy = height / 2
        centres = [(i * (bead + gap) + r, states[i]) for i in range(len(states))]

        def pixel(x, y):
            index = min(len(centres) - 1, max(0, int(x // (bead + gap))))
            cx, state = centres[index]
            dist = math.hypot(x - cx, y - cy)
            r0, g0, b0 = _rgb(colours.get(state, C["faint"]))
            if state == "todo":
                a = _ring(dist, r - .7, 1.0) * .9
                return r0, g0, b0, a
            a = max(0.0, min(1.0, r - dist + .5))
            if a <= 0:
                return 0, 0, 0, 0
            hx, hy = cx - r * .35, cy - r * .38
            spec = max(0.0, 1 - math.hypot(x - hx, y - hy) / (r * .62)) * .55
            return r0 + (255 - r0) * spec, g0 + (255 - g0) * spec, b0 + (255 - b0) * spec, a
        return _png(width, height, pixel)
    return _cached(("beads", states, bead, gap, height), build)


class BeadStrip(tk.Label):
    """折叠标签里的步骤珠串；``set_states`` 只在内容变化时重绘。"""

    def __init__(self, parent, *, bg: str):
        super().__init__(parent, bg=bg, bd=0, highlightthickness=0, padx=0, pady=0,
                         compound="left", fg=C["faint"], font=FONT_MICRO)
        self._bead = max(5, ui_px(parent, 6))
        self._gap = max(2, ui_px(parent, 3))
        self._height = max(self._bead + 2, ui_px(parent, 14))
        self._states = None
        self._photo = None

    def set_states(self, states):
        states = tuple(states)
        if states == self._states:
            return
        self._states = states
        if not states:
            self.configure(image="", text="")
            self._photo = None
            return
        shown, extra = states[:BEAD_LIMIT], len(states) - BEAD_LIMIT
        self._photo = tk.PhotoImage(master=self, data=bead_strip_png(
            shown, bead=self._bead, gap=self._gap, height=self._height))
        self.configure(image=self._photo, text=f" +{extra}" if extra > 0 else "")


__all__ = ["BEAD_LIMIT", "BeadStrip", "bead_strip_png", "halo_badge", "halo_png", "icon_chip",
           "set_chip_bg"]


# ─── 内容呈现的科幻元素（代码卡角标 / 标题刻度 / 分隔线）──────────


from gui_theme import RoundedCard  # noqa: E402


class HudCard(RoundedCard):
    """带 HUD 角标的圆角卡（代码 / 表格）：左上、右下两角用熔铁色细线勾出
    取景框，其余与 RoundedCard 完全一致（仍是 RoundedCard 实例）。"""

    def __init__(self, *args, hud_colour=None, hud_len=14, **kwargs):
        self._hud_colour = hud_colour or C["ember"]
        self._hud_len = hud_len
        super().__init__(*args, **kwargs)

    def _draw(self, w, h):
        super()._draw(w, h)
        try:
            if w <= 2 or h <= 2 or not self._cv.winfo_exists():
                return
            r = min(self._radius, w / 2, (h - self._shadow) / 2)
            body_h = h - self._shadow
            length = ui_px(self, self._hud_len)
            colour = mix(self._fill, self._hud_colour, .75)
            kw = {"fill": colour, "width": 1, "tags": ("card-decor",)}
            arc = {"style": tk.ARC, "outline": colour, "width": 1, "tags": ("card-decor",)}
            # 左上
            self._cv.create_arc(0.5, 0.5, 2 * r + .5, 2 * r + .5, start=90, extent=90, **arc)
            self._cv.create_line(r, .5, r + length, .5, **kw)
            self._cv.create_line(.5, r, .5, r + length, **kw)
            # 右下
            x1, y1 = w - .5, body_h - .5
            self._cv.create_arc(x1 - 2 * r, y1 - 2 * r, x1, y1, start=270, extent=90, **arc)
            self._cv.create_line(x1 - r, y1, x1 - r - length, y1, **kw)
            self._cv.create_line(x1, y1 - r, x1, y1 - r - length, **kw)
        except tk.TclError:
            pass


def heading_marker(parent, *, height: int, bg: str, level: int):
    """标题前的刻度标记：一条熔铁色竖条 + 两颗渐暗小方点，像仪表读数的游标。"""
    width = ui_px(parent, 14)
    cv = tk.Canvas(parent, width=width, height=height, bg=bg, highlightthickness=0, bd=0)
    bar = max(2, ui_px(parent, 3))
    top, bottom = height * .22, height * .78
    if level > 2:
        top, bottom = height * .32, height * .68
    cv.create_rectangle(0, top, bar, bottom, fill=C["ember"], outline="")
    dot = max(2, ui_px(parent, 2))
    for i, t in enumerate((.55, .28)):
        x = bar + dot + i * (dot * 2)
        cv.create_rectangle(x, bottom - dot, x + dot, bottom, fill=mix(bg, C["ember"], t),
                            outline="")
    return cv


class HudDivider(tk.Canvas):
    """分隔线（替代 markdown 的 ---）：两侧淡入的细线，中间一枚菱形读数点，
    菱形两侧各一小段熔铁色亮线。随宽度重绘。"""

    def __init__(self, parent, *, bg: str):
        self._h = ui_px(parent, 14)
        super().__init__(parent, height=self._h, bg=bg, highlightthickness=0, bd=0)
        self._bg = bg
        self.bind("<Configure>", self._draw, add="+")

    def _draw(self, _event=None):
        w = self.winfo_width()
        if w <= 4:
            return
        self.delete("all")
        cy = self._h / 2
        mid = w / 2
        steps = 12
        for i in range(steps):
            # 由两端向中间逐段提亮（Tk 无渐变，用分段线模拟）
            t = (i + 1) / steps
            colour = mix(self._bg, C["border_hi"], t)
            x0, x1 = mid * i / steps, mid * (i + 1) / steps
            self.create_line(x0, cy, x1, cy, fill=colour)
            self.create_line(w - x0, cy, w - x1, cy, fill=colour)
        accent = ui_px(self, 22)
        gap = ui_px(self, 7)
        self.create_line(mid - gap - accent, cy, mid - gap, cy, fill=mix(self._bg, C["ember"], .7))
        self.create_line(mid + gap, cy, mid + gap + accent, cy, fill=mix(self._bg, C["ember"], .7))
        d = ui_px(self, 4)
        self.create_polygon(mid, cy - d, mid + d, cy, mid, cy + d, mid - d, cy,
                            fill=C["ember"], outline="")


__all__ += ["HudCard", "HudDivider", "heading_marker"]
