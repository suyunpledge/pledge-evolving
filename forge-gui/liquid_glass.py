"""液态玻璃材质（Liquid Glass，参考 iOS 26 / macOS 26 的玻璃层级）。

Tk 没有 alpha 混合与背景模糊，这里用标准库现场编码 PNG 来近似玻璃的
四个可见特征——它们才是「玻璃感」的来源，而不是透明度本身：

1. 镜面边缘（specular rim）：1px 亮边，亮度随法线方向变化，左上迎光最亮，
   右下有一道弱的背光反射；
2. 边缘透镜（lensing）：贴着边缘往里几像素逐渐提亮，模拟厚玻璃折射；
3. 顶部光泽（sheen）：顶边内侧一道很快衰减的高光；
4. 柔和投影：形状下方偏移的渐隐阴影，让玻璃「浮」在内容上。

内部区域保持纯色（= ``fill``），所以放在玻璃上的普通 Label/Frame 只要用
同一个底色就能无缝融进去——光效全部落在内边距里。

性能：玻璃的每一行在水平方向只有两端需要逐像素计算，中间是同一颜色；
竖直方向上下两个边缘区之外的行也完全相同。所以渲染量只与圆角/边缘带
宽度有关，与面板尺寸无关；同宽不同高（展开动画的每一帧）直接复用行缓存。

零依赖：只用 tkinter 与标准库。
"""
from __future__ import annotations

import base64
import math
import os
import struct
import time
import tkinter as tk
import zlib
from collections import OrderedDict
from dataclasses import dataclass, replace
from tkinter import font as tkfont

import i18n
from gui_theme import C, FONT_UI, IS_WINDOWS, _bg_of, mix, ui_px

# 展开（文件夹）动画时长；FORGE_REDUCE_MOTION=1 时一律瞬时完成。
GLASS_MOTION_MS = 300
GLASS_FRAME_MS = 16
# Windows 上用颜色键把窗口矩形的四角挖空，露出真正的圆角玻璃。
# 选一个界面里绝不会出现的颜色：任何恰好等于它的像素都会变成「洞」。
WINDOW_KEY = "#FF00FE"

_LIGHT = (-0.38, -0.925)          # 指向光源（左上偏上），已近似归一化


def reduce_motion() -> bool:
    return os.environ.get("FORGE_REDUCE_MOTION", "") not in ("", "0")


def _rgb(colour: str) -> tuple[int, int, int]:
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


@dataclass(frozen=True)
class GlassSpec:
    """一块玻璃的全部外观参数（可哈希，用作缓存键）。"""
    radius: float = 18
    fill: str = "#1A1E26"
    shadow: int = 0               # 投影外扩像素；0 = 无投影、形状贴满图像
    shadow_dy: int = 0            # 投影向下偏移
    shadow_alpha: float = 0.45
    rim: float = 1.0              # 镜面边缘强度
    rim_colour: str = "#FFFFFF"
    glow: float = 1.0             # 边缘透镜提亮强度
    band: float = 7.0             # 透镜带宽（px）
    sheen: float = 1.0            # 顶部光泽强度
    sheen_h: float = 12.0         # 顶部光泽高度（px）
    inner_line: float = 0.10      # 亮边内侧一道暗线，给玻璃「厚度」
    matte: str | None = None      # 给定时输出不透明 RGB：半透明像素先合成到 matte
    key: str | None = None        # 与 matte 同用：完全透明的像素写成该颜色（窗口挖空）


# ─── 渲染 ─────────────────────────────────────────────────────


class _Shape:
    """形状几何 + 单像素着色。坐标用像素中心（x + .5）。"""

    def __init__(self, w: int, h: int, spec: GlassSpec):
        self.spec = spec
        s = max(0, int(spec.shadow))
        dy = int(spec.shadow_dy) if s else 0
        self.mx = s
        self.my0 = max(0, s - dy)
        self.my1 = s + dy if s else 0
        self.x0, self.x1 = self.mx, w - self.mx
        self.y0, self.y1 = self.my0, h - self.my1
        self.hw = max(0.5, (self.x1 - self.x0) / 2)
        self.hh = max(0.5, (self.y1 - self.y0) / 2)
        self.cx = (self.x0 + self.x1) / 2
        self.cy = (self.y0 + self.y1) / 2
        self.r = max(0.0, min(spec.radius, self.hw, self.hh))
        self.dy = dy
        self.fill = _rgb(spec.fill)
        self.rimc = _rgb(spec.rim_colour)
        self.matte = _rgb(spec.matte) if spec.matte else None
        self.key = bytes(_rgb(spec.key)) if spec.key else None
        self.opaque = self.matte is not None
        self.bpp = 3 if self.opaque else 4

    def _sdf(self, px, py):
        qx = abs(px - self.cx) - (self.hw - self.r)
        qy = abs(py - self.cy) - (self.hh - self.r)
        ox, oy = (qx if qx > 0 else 0.0), (qy if qy > 0 else 0.0)
        outside = math.hypot(ox, oy) if (ox or oy) else 0.0
        return outside + min(max(qx, qy), 0.0) - self.r, qx, qy

    def pixel(self, x: int, y: int) -> bytes:
        spec = self.spec
        px, py = x + .5, y + .5
        d, qx, qy = self._sdf(px, py)
        cov = 0.5 - d
        cov = 0.0 if cov < 0 else 1.0 if cov > 1 else cov
        # 投影：同一形状下移 dy 后的距离场，向外按平方衰减。
        sa = 0.0
        if spec.shadow and cov < 1:
            ds = self._sdf(px, py - self.dy)[0]
            if ds <= 0:
                sa = spec.shadow_alpha
            elif ds < spec.shadow:
                t = 1 - ds / spec.shadow
                sa = spec.shadow_alpha * t * t
        if cov <= 0 and sa <= 0:
            return self._out(None, 0.0, 0.0)
        colour = None
        if cov > 0:
            depth = -d if d < 0 else 0.0
            # 外法线：圆角区取径向，直边取轴向。
            sx = -1.0 if px < self.cx else 1.0
            sy = -1.0 if py < self.cy else 1.0
            if qx > 0 and qy > 0:
                length = math.hypot(qx, qy) or 1.0
                nx, ny = sx * qx / length, sy * qy / length
            elif qx > qy:
                nx, ny = sx, 0.0
            else:
                nx, ny = 0.0, sy
            facing = nx * _LIGHT[0] + ny * _LIGHT[1]
            spec_hi = facing if facing > 0 else 0.0
            back = -facing if facing < 0 else 0.0
            lift = 0.0
            if spec.sheen and spec.sheen_h > 0:
                top = py - self.y0
                if top < spec.sheen_h:
                    k = 1 - top / spec.sheen_h
                    lift += spec.sheen * 0.05 * k * k
            if spec.glow and depth < spec.band:
                k = 1 - depth / spec.band
                lift += spec.glow * (0.03 + 0.15 * spec_hi * spec_hi + 0.075 * back * back) * k * k
            r, g, b = self.fill
            if lift:
                r += (255 - r) * lift
                g += (255 - g) * lift
                b += (255 - b) * lift
            if spec.inner_line and 1.2 <= depth < 2.4:
                dark = spec.inner_line * (1 - abs(depth - 1.8) / 0.6)
                r, g, b = r * (1 - dark), g * (1 - dark), b * (1 - dark)
            if spec.rim and depth < 1.6:
                k = 1.6 - depth
                k = 1.0 if k > 1 else k
                strength = spec.rim * (0.13 + 0.62 * spec_hi ** 1.6 + 0.30 * back * back) * k
                strength = 1.0 if strength > 1 else strength
                rr, rg, rb = self.rimc
                r += (rr - r) * strength
                g += (rg - g) * strength
                b += (rb - b) * strength
            colour = (r, g, b)
        return self._out(colour, cov, sa)

    def _out(self, colour, cov, sa) -> bytes:
        alpha = cov + sa * (1 - cov)
        if self.opaque:
            if alpha < 0.035 and self.key is not None:
                return self.key
            mr, mg, mb = self.matte
            under = (1 - sa)
            r = mr * under * (1 - cov)
            g = mg * under * (1 - cov)
            b = mb * under * (1 - cov)
            if colour is not None:
                r += colour[0] * cov
                g += colour[1] * cov
                b += colour[2] * cov
            return bytes((_c(r), _c(g), _c(b)))
        if alpha <= 0:
            return b"\x00\x00\x00\x00"
        if colour is None:
            return bytes((0, 0, 0, _c(255 * alpha)))
        scale = cov / alpha
        return bytes((_c(colour[0] * scale), _c(colour[1] * scale), _c(colour[2] * scale),
                      _c(255 * alpha)))

    # 行：两端逐像素，中段同色重复。
    def row(self, y: int, w: int) -> bytes:
        edge = int(self.mx + self.r + self.spec.band + 3)
        if edge * 2 + 1 >= w:
            return b"".join(self.pixel(x, y) for x in range(w))
        left = b"".join(self.pixel(x, y) for x in range(edge))
        mid = self.pixel(edge, y) * (w - 2 * edge)
        right = b"".join(self.pixel(x, y) for x in range(w - edge, w))
        return left + mid + right


def _c(value) -> int:
    value = round(value)
    return 0 if value < 0 else 255 if value > 255 else value


def _zones(spec: GlassSpec) -> tuple[int, int]:
    s = max(0, int(spec.shadow))
    dy = int(spec.shadow_dy) if s else 0
    top = math.ceil(max(0, s - dy) + spec.radius + max(spec.band, spec.sheen_h) + 3)
    bottom = math.ceil(s + dy + spec.radius + spec.band + 3)
    return top, bottom


_ROW_CACHE: OrderedDict = OrderedDict()
_PNG_CACHE: OrderedDict = OrderedDict()


def _lru_put(cache: OrderedDict, key, value, limit):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def _glass_rows(w: int, h: int, spec: GlassSpec) -> list[bytes]:
    top, bottom = _zones(spec)
    if h < top + bottom + 1:
        shape = _Shape(w, h, spec)
        return [shape.row(y, w) for y in range(h)]
    key = (w, spec)
    cached = _ROW_CACHE.get(key)
    if cached is None:
        # 规范高度：上下边缘区之间留一行纯「中段」，且两区不越过中线。
        canon = 2 * max(top, bottom) + 1
        shape = _Shape(w, canon, spec)
        cached = ([shape.row(y, w) for y in range(top)], shape.row(top, w),
                  [shape.row(y, w) for y in range(canon - bottom, canon)])
        _lru_put(_ROW_CACHE, key, cached, 48)
    else:
        _ROW_CACHE.move_to_end(key)
    head, middle, tail = cached
    return [*head, *([middle] * (h - top - bottom)), *tail]


def glass_png(w: int, h: int, spec: GlassSpec) -> str:
    """渲染一块 w×h 的玻璃，返回 base64 PNG（Tk PhotoImage 可直接吃）。"""
    w, h = max(1, int(w)), max(1, int(h))
    key = (w, h, spec)
    hit = _PNG_CACHE.get(key)
    if hit is not None:
        _PNG_CACHE.move_to_end(key)
        return hit
    rows = _glass_rows(w, h, spec)
    raw = b"".join(b"\x00" + row for row in rows)
    colour_type = 2 if spec.matte else 6

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", w, h, 8, colour_type, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
           + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))
    data = base64.b64encode(png).decode("ascii")
    _lru_put(_PNG_CACHE, key, data, 96)
    return data


def glass_photo(master, w: int, h: int, spec: GlassSpec) -> tk.PhotoImage:
    colours = {}
    for field in ("fill", "rim_colour", "matte", "key"):
        value = getattr(spec, field)
        if value is not None and (not value.startswith("#") or len(value) != 7):
            rgb = master.winfo_rgb(value)
            colours[field] = "#%02x%02x%02x" % tuple(channel // 257 for channel in rgb)
    if colours:
        spec = replace(spec, **colours)
    return tk.PhotoImage(master=master, data=glass_png(w, h, spec))


def spring(t: float, *, zeta: float = 0.68, omega: float = 11.0) -> float:
    """欠阻尼弹簧位移（0→1，约 5% 过冲后回落）；t 为 0..1 的归一化时间。"""
    if t >= 1:
        return 1.0
    if t <= 0:
        return 0.0
    wd = omega * math.sqrt(1 - zeta * zeta)
    decay = math.exp(-zeta * omega * t)
    return 1 - decay * (math.cos(wd * t) + zeta * omega / wd * math.sin(wd * t))


# ─── 材质预设 ─────────────────────────────────────────────────
# 深色界面下的玻璃：比所在底色亮一档的冷灰，亮边与透镜带负责「玻璃感」。


def folder_spec(backdrop: str, *, radius=16, shadow=8) -> GlassSpec:
    """对话流里的展开面板（文件夹）：浮在 chat 底上，带下方柔和投影。"""
    return GlassSpec(radius=radius, fill=C["glass_folder"], shadow=shadow, shadow_dy=3,
                     shadow_alpha=0.50, rim=0.9, glow=1.0, band=8, sheen=1.0, sheen_h=14)


def chip_spec(fill: str, *, radius, rim=0.75) -> GlassSpec:
    """胶囊（文件夹标签 / 菜单悬停 / 按钮）：无投影，窄透镜带。"""
    return GlassSpec(radius=radius, fill=fill, rim=rim, glow=0.9, band=4, sheen=0.8,
                     sheen_h=8, inner_line=0.06)


def window_spec(fill: str, *, radius, matte: str, shadow=0, shadow_dy=0) -> GlassSpec:
    """无边框浮窗（弹窗 / 菜单）：四角用颜色键挖空，半透明像素合成到 matte。"""
    return GlassSpec(radius=radius, fill=fill, shadow=shadow, shadow_dy=shadow_dy,
                     shadow_alpha=0.55, rim=1.0, glow=1.1, band=9, sheen=1.0, sheen_h=18,
                     matte=matte, key=WINDOW_KEY if IS_WINDOWS else None)


# ─── 画布托管的玻璃面 ─────────────────────────────────────────


class GlassPanel(tk.Frame):
    """玻璃卡片：Canvas 画玻璃位图，``content`` 是真正的容器（底色 = 玻璃 fill）。

    用法与 RoundedCard 一致；另外提供 ``unfold()``：像 iOS 文件夹那样从
    小到大弹开（欠阻尼弹簧，约 5% 过冲），内容在玻璃基本展开后才出现。
    """

    def __init__(self, parent, *, spec: GlassSpec, padx=12, pady=10, bg=None):
        outer = bg or _bg_of(parent)
        super().__init__(parent, bg=outer, highlightthickness=0, bd=0)
        self.spec = spec
        self._padx, self._pady = padx, pady
        self._cv = tk.Canvas(self, bg=outer, highlightthickness=0, bd=0, height=1, width=1)
        self._cv.pack(fill=tk.BOTH, expand=True)
        self._image_item = self._cv.create_image(0, 0, anchor="nw")
        self.content = tk.Frame(self._cv, bg=spec.fill)
        self._win = self._cv.create_window(0, 0, window=self.content, anchor="nw")
        self._photo = None
        self._size = None
        self._render_job = None
        self._anim_job = None
        self._anim_height = None
        self._cap_item = None
        self._anim_images = None
        self._cv.bind("<Configure>", self._on_canvas)
        self.content.bind("<Configure>", self._on_content)
        self.bind("<Destroy>", self._cleanup, add="+")

    # 形状在图像里的内边距（投影外扩）
    def _margins(self):
        s = max(0, int(self.spec.shadow))
        dy = int(self.spec.shadow_dy) if s else 0
        return s, max(0, s - dy), s + dy if s else 0

    def natural_height(self) -> int:
        _mx, top, bottom = self._margins()
        return self.content.winfo_reqheight() + self._pady * 2 + top + bottom

    def _on_content(self, _event=None):
        if self._anim_job is not None:
            return
        try:
            need = self.natural_height()
            if abs(self._cv.winfo_reqheight() - need) > 1:
                self._cv.configure(height=need)
        except tk.TclError:
            pass

    def _on_canvas(self, _event=None):
        try:
            w = self._cv.winfo_width()
        except tk.TclError:
            return
        if w <= 2:
            return
        mx, top, _bottom = self._margins()
        self._cv.coords(self._win, mx + self._padx, top + self._pady)
        self._cv.itemconfigure(self._win, width=max(1, w - 2 * (mx + self._padx)))
        if self._render_job is None:
            self._render_job = self.after_idle(self._render)

    def _render(self):
        self._render_job = None
        if getattr(self, "_cap_item", None) is not None:
            return                    # 展开动画期间由主体 + 底边两张图负责
        try:
            if not self.winfo_exists():
                return
            w = self._cv.winfo_width()
            h = self._anim_height or self._cv.winfo_height()
        except tk.TclError:
            return
        if w <= 2 or h <= 2 or (w, h) == self._size:
            return
        self._paint(w, h)

    def _paint(self, w, h):
        try:
            photo = glass_photo(self, w, h, self.spec)
            self._cv.itemconfigure(self._image_item, image=photo)
        except tk.TclError:
            return
        self._photo = photo           # 防 GC
        self._size = (w, h)

    def set_spec(self, spec: GlassSpec):
        self.spec = spec
        self.content.configure(bg=spec.fill)
        self._size = None
        self._on_canvas()

    def unfold(self, on_done=None):
        """弹簧展开。已映射后调用；FORGE_REDUCE_MOTION 时直接到位。

        每帧不重新渲染玻璃：玻璃「主体」按最大高度只渲染一次（下方超出
        画布的部分被裁掉），另渲染一条不透明的「底边」（底部圆角 + 亮边 +
        投影，已合成到外部底色上）贴在当前高度处。每帧只改画布高度和
        底边坐标，开销与面板大小无关。
        """
        self._stop_anim()
        try:
            self.update_idletasks()
            target = self.natural_height()
            width = self._cv.winfo_width()
            outer = self._cv.cget("bg")
        except tk.TclError:
            return
        top_zone, cap_h = _zones(self.spec)
        if reduce_motion() or width <= 8 or target < top_zone + cap_h + 4:
            self._cv.configure(height=target)
            if on_done:
                on_done()
            return
        start = max(top_zone + cap_h + 2, round(target * 0.34))
        tallest = round(start + (target - start) * 1.12) + 2
        try:
            body = glass_photo(self, width, tallest, self.spec)
            opaque = GlassSpec(**{**self.spec.__dict__, "matte": outer, "key": None})
            full = glass_photo(self, width, top_zone + cap_h + 1, opaque)
            cap = tk.PhotoImage(master=self)
            cap.tk.call(cap, "copy", full, "-from", 0, top_zone + 1, width, top_zone + cap_h + 1)
        except tk.TclError:
            self._cv.configure(height=target)
            return
        self._anim_images = (body, full, cap)
        self._cv.itemconfigure(self._image_item, image=body)
        self._size = None
        cap_item = self._cv.create_image(0, start - cap_h, anchor="nw", image=cap)
        self._cv.tag_raise(cap_item, self._image_item)
        self._cap_item = cap_item
        self._cv.itemconfigure(self._win, state="hidden")
        began = time.perf_counter()

        def finish():
            self._anim_height = None
            self._drop_cap()
            self._cv.itemconfigure(self._win, state="normal")
            self._cv.configure(height=self.natural_height())
            self._size = None
            self._render()
            if on_done:
                on_done()

        def tick():
            self._anim_job = None
            try:
                if not self.winfo_exists():
                    return
            except tk.TclError:
                return
            # 按墙钟推进：Tk 繁忙时掉帧也不会把动画拖长。
            t = (time.perf_counter() - began) * 1000 / GLASS_MOTION_MS
            if t >= 1:
                finish()
                return
            h = max(4, round(start + (target - start) * spring(t)))
            self._anim_height = h
            try:
                self._cv.configure(height=h)
                self._cv.coords(cap_item, 0, h - cap_h)
                # 玻璃基本到位（过冲峰值之后）再放出内容：映射内容是整段动画
                # 里最贵的一步，放在末段只会吃掉一帧。
                if t >= 0.5 and self._cv.itemcget(self._win, "state") == "hidden":
                    self._cv.itemconfigure(self._win, state="normal")
            except tk.TclError:
                return
            self._anim_job = self.after(GLASS_FRAME_MS, tick)

        self._anim_height = start
        self._cv.configure(height=start)
        self._anim_job = self.after(GLASS_FRAME_MS, tick)

    def _drop_cap(self):
        item = getattr(self, "_cap_item", None)
        if item is not None:
            try:
                self._cv.delete(item)
            except tk.TclError:
                pass
            self._cap_item = None
        self._anim_images = None

    def _stop_anim(self):
        if self._anim_job is not None:
            try:
                self.after_cancel(self._anim_job)
            except tk.TclError:
                pass
            self._anim_job = None
        self._anim_height = None
        self._drop_cap()
        try:
            self._cv.itemconfigure(self._win, state="normal")
        except tk.TclError:
            pass
        self._size = None

    def fold(self):
        """收起前调用：结束进行中的展开动画，保证下次展开从头开始。"""
        self._stop_anim()

    def _cleanup(self, event):
        if event.widget is not self:
            return
        for name in ("_render_job", "_anim_job"):
            job = getattr(self, name)
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, name, None)

    @property
    def canvas(self) -> tk.Canvas:
        return self._cv


class GlassBackdrop:
    """给一个 Frame 垫一张随尺寸重绘的玻璃胶囊（悬停行 / 文件夹标签）。

    背景是一个被 ``lower()`` 到最底层的 Label，子控件盖在它上面；子控件
    与玻璃内部同色，所以只有内边距里的亮边与透镜光效露出来。
    """

    def __init__(self, frame: tk.Frame, spec: GlassSpec | None = None):
        self.frame = frame
        self.spec = spec
        self._label = tk.Label(frame, bd=0, highlightthickness=0, bg=_bg_of(frame))
        # Painted padding must forward the owning row's bindings; a child
        # Label otherwise consumes clicks that used to reach the Frame.
        self._label.bindtags((str(frame), *self._label.bindtags()))
        self._photo = None
        self._size = None
        self._job = None
        frame.bind("<Configure>", self._schedule, add="+")
        frame.bind("<Destroy>", self._cancel, add="+")
        if spec is not None:
            self.show(spec)

    def show(self, spec: GlassSpec):
        self.spec = spec
        self._size = None
        # outside：相对整个 Frame（含 padx/pady 内边距），胶囊才盖满整行。
        self._label.place(x=0, y=0, relwidth=1, relheight=1, bordermode="outside")
        self._label.lower()
        self._paint()

    def hide(self):
        self.spec = None
        self._label.place_forget()

    def _schedule(self, _event=None):
        if self.spec is not None and self._job is None:
            try:
                self._job = self.frame.after_idle(self._run)
            except tk.TclError:
                pass

    def _run(self):
        self._job = None
        self._paint()

    def _paint(self):
        if self.spec is None:
            return
        try:
            w, h = self.frame.winfo_width(), self.frame.winfo_height()
            if w <= 2 or h <= 2:
                w, h = self.frame.winfo_reqwidth(), self.frame.winfo_reqheight()
            if w <= 2 or h <= 2 or (w, h, self.spec) == self._size:
                return
            photo = glass_photo(self.frame, w, h, self.spec)
            self._label.configure(image=photo, bg=_bg_of(self.frame))
        except tk.TclError:
            return
        self._photo = photo
        self._size = (w, h, self.spec)

    def _cancel(self, event):
        if event.widget is self.frame and self._job is not None:
            try:
                self.frame.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None


# ─── 无边框玻璃窗口（弹窗 / 菜单浮层）────────────────────────


def mark_glass_window(win):
    """告诉 apply_window_chrome：这个窗口自己画圆角，DWM 不要再圆角/描边。"""
    win._forge_glass = True


def glass_window(win, *, fill: str, radius: float, matte: str, inset: int | None = None,
                 shadow: int = 0, shadow_dy: int = 0) -> tk.Frame:
    """把一个 overrideredirect Toplevel 变成圆角玻璃，返回放内容的 Frame。

    窗口矩形四角在 Windows 上用颜色键挖空；其他平台退化为 matte 色角。
    宿主 Frame 的请求尺寸会回传给 Canvas，调用方照常 ``update_idletasks``
    后读 ``winfo_reqwidth/height`` 定位即可。
    """
    mark_glass_window(win)
    key = WINDOW_KEY if IS_WINDOWS else matte
    win.configure(bg=key)
    if IS_WINDOWS:
        try:
            win.attributes("-transparentcolor", WINDOW_KEY)
        except tk.TclError:
            key = matte
            win.configure(bg=key)
    spec = window_spec(fill, radius=radius, matte=matte, shadow=shadow, shadow_dy=shadow_dy)
    if key != WINDOW_KEY:
        spec = GlassSpec(**{**spec.__dict__, "key": None})
    pad = int(inset if inset is not None else max(spec.band, 6))
    s = max(0, int(shadow))
    dy = int(shadow_dy) if s else 0
    left, top, bottom = s + pad, max(0, s - dy) + pad, (s + dy if s else 0) + pad
    canvas = tk.Canvas(win, bg=key, highlightthickness=0, bd=0, width=1, height=1)
    canvas.pack(fill=tk.BOTH, expand=True)
    image_item = canvas.create_image(0, 0, anchor="nw")
    host = tk.Frame(canvas, bg=fill)
    window_item = canvas.create_window(left, top, window=host, anchor="nw")
    state = {"photo": None, "size": None, "job": None}

    def fit_request(_event=None):
        try:
            canvas.configure(width=host.winfo_reqwidth() + 2 * left,
                             height=host.winfo_reqheight() + top + bottom)
        except tk.TclError:
            pass

    def render():
        state["job"] = None
        try:
            w, h = canvas.winfo_width(), canvas.winfo_height()
            if w <= 2 or h <= 2 or (w, h) == state["size"]:
                return
            photo = glass_photo(canvas, w, h, spec)
            canvas.itemconfigure(image_item, image=photo)
        except tk.TclError:
            return
        state["photo"], state["size"] = photo, (w, h)

    def on_canvas(_event=None):
        try:
            w, h = canvas.winfo_width(), canvas.winfo_height()
            canvas.itemconfigure(window_item, width=max(1, w - 2 * left),
                                 height=max(1, h - top - bottom))
            if state["job"] is None:
                state["job"] = canvas.after_idle(render)
        except tk.TclError:
            pass

    def cleanup(event):
        if event.widget is canvas and state["job"] is not None:
            canvas.after_cancel(state["job"])
            state["job"] = None

    canvas.bind("<Destroy>", cleanup, add="+")
    host.bind("<Configure>", fit_request, add="+")
    canvas.bind("<Configure>", on_canvas, add="+")
    host._glass_canvas = canvas
    host._glass_fit = fit_request
    host._glass_state = state
    host._glass_spec = spec
    host._glass_render = render
    return host


def fit_glass_window(host):
    """内容建好后调用：把宿主的请求尺寸（加玻璃边距）回传给窗口。"""
    try:
        host.update_idletasks()
    except tk.TclError:
        return
    host._glass_fit()
    try:
        host.update_idletasks()
    except tk.TclError:
        pass


def modal_scrim(root, *, alpha=0.46):
    """模态弹窗背后的压暗层（iOS alert 的 dimming view）。不抢焦点、不接 grab。"""
    scrim = tk.Toplevel(root)
    scrim.withdraw()
    scrim.overrideredirect(True)
    mark_glass_window(scrim)
    scrim.configure(bg="#000000", cursor="arrow")
    try:
        scrim.attributes("-alpha", alpha)
        scrim.transient(root)
    except tk.TclError:
        pass
    root.update_idletasks()
    scrim.geometry(f"{root.winfo_width()}x{root.winfo_height()}"
                   f"+{root.winfo_rootx()}+{root.winfo_rooty()}")
    scrim.deiconify()
    scrim._forge_scrim = True
    return scrim


# ─── 玻璃胶囊按钮 ─────────────────────────────────────────────


def _button_palette(kind: str, base: str):
    """(fill, fg, rim_colour)；fill 是胶囊内部色，hover/pressed 由它派生。"""
    if kind == "primary":
        return C["accent"], C.get("on_accent", "#FFFFFF"), C["accent2"]
    if kind == "danger":
        return mix(base, C["error"], .20), C["error"], C["error"]
    if kind == "ok":
        return C["ok"], "#0B0B10", "#B9F2DC"
    if kind == "accent_soft":
        return mix(base, C["accent"], .22), C["accent_text"], C["accent2"]
    if kind == "quiet":
        return mix(base, "#FFFFFF", .035), C["body"], "#FFFFFF"
    return mix(base, "#FFFFFF", .075), C["text"], "#FFFFFF"


class GlassButton(i18n.Label):
    """胶囊形玻璃按钮。

    用 Label 而不是原生 Button：Windows 版 Tk 会在「已聚焦的 Button 每次
    重绘」时画系统虚线焦点框（悬停、换图都会触发），盖在胶囊上很扎眼，
    且这个框被钳在控件边缘、无法用位图溢出躲开。Label 不画焦点框；
    这里补齐按钮语义：``invoke()``、``state``、Tab 聚焦、空格 / 回车 /
    单击（在控件内松开才触发）。焦点由胶囊亮边（focus 图）表达。
    """

    def __init__(self, parent, text, command, *, images, base, fg, font, width, height):
        super().__init__(parent, text=text, image=images["normal"], compound="center",
                         bg=base, fg=fg, font=font, bd=0, padx=0, pady=0,
                         highlightthickness=0, takefocus=1, cursor="hand2",
                         width=width, height=height)
        self._command = command
        self._glass_images = images
        self._flags = {"hover": False, "pressed": False, "focus": False}
        self._glass_state = "normal"
        for sequence, changes in (("<Enter>", {"hover": True}),
                                  ("<Leave>", {"hover": False, "pressed": False}),
                                  ("<FocusIn>", {"focus": True}),
                                  ("<FocusOut>", {"focus": False, "pressed": False})):
            self.bind(sequence, lambda _e, c=changes: self._set(**c), add="+")
        self.bind("<ButtonPress-1>", self._press, add="+")
        self.bind("<ButtonRelease-1>", self._release, add="+")
        for key in ("<space>", "<Return>", "<KP_Enter>"):
            self.bind(key, self._keyboard, add="+")
        self._paint()

    def _set(self, **changes):
        self._flags.update(changes)
        self._paint()

    def _paint(self):
        f = self._flags
        name = ("pressed" if f["pressed"] else "focus" if f["focus"] else
                "hover" if f["hover"] else "normal")
        if name != self._glass_state:
            try:
                tk.Label.configure(self, image=self._glass_images[name])
            except tk.TclError:
                return
            self._glass_state = name

    def _press(self, _event=None):
        if str(self.cget("state")) == tk.DISABLED:
            return
        try:
            self.focus_set()
        except tk.TclError:
            pass
        self._set(pressed=True)

    def _release(self, event=None):
        was_pressed = self._flags["pressed"]
        self._set(pressed=False)
        if not was_pressed or event is None:
            return
        if 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height():
            self.invoke()

    def _keyboard(self, _event=None):
        self.invoke()
        return "break"

    def invoke(self):
        if str(self.cget("state")) != tk.DISABLED and callable(self._command):
            return self._command()
        return None


def glass_button(parent, text, command, *, kind="ghost", bg=None, width=None,
                 height=None, font=None, padx=18):
    """胶囊玻璃按钮（GlassButton）：常态、悬停、按下、聚焦四张位图。"""
    base = bg or _bg_of(parent)
    font = font or FONT_UI
    fill, fg, rim_colour = _button_palette(kind, base)
    shown = str(i18n.resolve(text, parent))
    measure = tkfont.Font(root=parent, font=font).measure(shown)
    h = int(height or ui_px(parent, 36))
    w = int(width or measure + 2 * ui_px(parent, padx))
    w = max(w, measure + ui_px(parent, 16), h)
    radius = h / 2
    lighter = mix(fill, "#FFFFFF", .10)
    darker = mix(fill, "#000000", .16)

    def spec(colour, rim=0.85, rim_c=rim_colour):
        return GlassSpec(radius=radius, fill=colour, rim=rim, rim_colour=rim_c, glow=1.0,
                         band=5, sheen=0.9, sheen_h=h * .45, inner_line=0.05)

    specs = {"normal": spec(fill), "hover": spec(lighter, 1.0), "pressed": spec(darker, .6),
             "focus": spec(lighter, 1.0, C["focus_ring"])}
    images = {name: glass_photo(parent, w, h, value) for name, value in specs.items()}
    button = GlassButton(parent, text, command, images=images, base=base, fg=fg, font=font,
                         width=w, height=h)
    button._glass_kind = kind
    return button


__all__ = [
    "GLASS_MOTION_MS", "GlassBackdrop", "GlassButton", "GlassPanel", "GlassSpec", "WINDOW_KEY",
    "chip_spec", "folder_spec", "glass_button", "glass_png", "glass_photo", "glass_window",
    "fit_glass_window", "mark_glass_window", "modal_scrim", "reduce_motion", "spring", "window_spec",
]
