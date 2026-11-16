"""梦幻极光背景（2026-11-14）：与液态玻璃相辅相成的底色。

几团大而柔的色光（紫、青、品红，再点一抹熔铁橙）叠在星云底色上，加一层
稀疏的细小星点。Tk 没有渐变与模糊：

* 先按 1/SCALE 分辨率逐像素算（加色叠加 + 高斯衰减），编码成 PNG，再用
  Tk 的 ``zoom`` 放大。色光半径是几百像素，相邻 SCALE 像素的色差不到一级，
  放大后看不出块；
* 消息列（正文是实色控件）用 ``column`` 遮罩：列内只留极弱的光，向两侧
  在 ``fade`` 像素内过渡到全亮，实色控件的边缘因此融进背景、看不出方框。

零依赖：只用 tkinter 与标准库。
"""
from __future__ import annotations

import base64
import math
import random
import struct
import tkinter as tk
import zlib
from collections import OrderedDict

from gui_theme import C

SCALE = 8
# (相对 x, 相对 y, 相对半径（以短边计）, 颜色, 强度)
BLOBS = (
    (0.10, 0.12, 0.70, "#7B5CFF", 0.42),   # 紫
    (0.92, 0.22, 0.62, "#2EC5D6", 0.30),   # 青
    (0.78, 0.92, 0.70, "#FF5CA8", 0.27),   # 品红
    (0.22, 0.88, 0.55, "#FF7A3D", 0.15),   # 熔铁橙（品牌色，最弱）
    (0.52, 0.48, 0.85, "#4B3BB5", 0.10),   # 中部靛蓝薄雾
)
_CACHE: OrderedDict = OrderedDict()


def _rgb(colour: str):
    return tuple(int(colour[i:i + 2], 16) for i in (1, 3, 5))


def _png_rgb(width: int, height: int, rows: list[bytes]) -> str:
    raw = b"".join(b"\x00" + row for row in rows)

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
           + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode("ascii")


def aurora_png(width: int, height: int, *, base: str | None = None, column=None,
               fade: int = 260, inside: float = 0.10, strength: float = 1.0,
               fade_top: int = 0, fade_bottom: int = 0) -> tuple[str, int]:
    """返回 (base64 PNG, scale)。图像尺寸为 ceil(width/scale) × ceil(height/scale)。

    ``column`` = (x0, x1)：消息列的像素范围，列内光强降到 ``inside``。
    """
    base = base or C["chat"]
    width, height = max(1, int(width)), max(1, int(height))
    # 以低分辨率尺寸为键：同键的图放大后都覆盖整个画布（不会在右 / 下缘露出一条底色）。
    key = (math.ceil(width / SCALE), math.ceil(height / SCALE), base, None if column is None else
           (int(column[0]) // SCALE, int(column[1]) // SCALE), fade, inside, strength,
           fade_top, fade_bottom)
    hit = _CACHE.get(key)
    if hit is not None:
        _CACHE.move_to_end(key)
        return hit
    lw, lh = math.ceil(width / SCALE), math.ceil(height / SCALE)
    short = min(width, height)
    br, bg_, bb = _rgb(base)
    blobs = []
    for rx, ry, rr, colour, power in BLOBS:
        cr, cg, cb = _rgb(colour)
        radius = rr * short
        blobs.append((rx * width, ry * height, 1 / (2 * (radius / 2.2) ** 2),
                      cr * power * strength, cg * power * strength, cb * power * strength))
    # 列遮罩只与 x 有关：每列算一次。
    masks = []
    for lx in range(lw):
        x = (lx + .5) * SCALE
        if column is None:
            masks.append(1.0)
            continue
        x0, x1 = column
        if x0 <= x <= x1:
            masks.append(inside)
        else:
            d = (x0 - x) if x < x0 else (x - x1)
            t = min(1.0, d / max(1, fade))
            t = t * t * (3 - 2 * t)                       # smoothstep
            masks.append(inside + (1 - inside) * t)
    rows = []
    for ly in range(lh):
        y = (ly + .5) * SCALE
        # 上下缘淡出：贴着实色的标题栏 / 输入区时不出现硬边。
        vmask = 1.0
        if fade_top and y < fade_top:
            t = y / fade_top
            vmask *= t * t * (3 - 2 * t)
        if fade_bottom and height - y < fade_bottom:
            t = max(0.0, (height - y) / fade_bottom)
            vmask *= t * t * (3 - 2 * t)
        row = bytearray()
        for lx in range(lw):
            x = (lx + .5) * SCALE
            r, g, b = 0.0, 0.0, 0.0
            for cx, cy, k, cr, cg, cb in blobs:
                w = math.exp(-((x - cx) ** 2 + (y - cy) ** 2) * k)
                r += cr * w
                g += cg * w
                b += cb * w
            m = masks[lx] * vmask
            # 加色叠加后用「滤色」压住高光，不会冲成白斑。
            r = br + (255 - br) * (1 - math.exp(-r * m / 255))
            g = bg_ + (255 - bg_) * (1 - math.exp(-g * m / 255))
            b = bb + (255 - bb) * (1 - math.exp(-b * m / 255))
            row += bytes((min(255, int(r)), min(255, int(g)), min(255, int(b))))
        rows.append(bytes(row))
    result = (_png_rgb(lw, lh, rows), SCALE)
    _CACHE[key] = result
    while len(_CACHE) > 12:
        _CACHE.popitem(last=False)
    return result


def aurora_photo(master, width: int, height: int, **options) -> tk.PhotoImage:
    data, scale = aurora_png(width, height, **options)
    small = tk.PhotoImage(master=master, data=data)
    big = tk.PhotoImage(master=master)
    big.tk.call(big, "copy", small, "-zoom", scale, scale)
    return big


def colour_at(width: int, height: int, x: float, y: float, *, base: str | None = None,
              strength: float = 1.0) -> str:
    """不带遮罩的极光在 (x, y) 处的颜色：玻璃卡片取它做「透出来的底色」。"""
    base = base or C["chat"]
    short = min(width, height)
    channels = [0.0, 0.0, 0.0]
    for rx, ry, rr, colour, power in BLOBS:
        radius = rr * short
        w = math.exp(-((x - rx * width) ** 2 + (y - ry * height) ** 2) / (2 * (radius / 2.2) ** 2))
        for i, c in enumerate(_rgb(colour)):
            channels[i] += c * power * strength * w
    out = []
    for c0, add in zip(_rgb(base), channels):
        out.append(min(255, int(c0 + (255 - c0) * (1 - math.exp(-add / 255)))))
    return "#%02X%02X%02X" % tuple(out)


def scatter_stars(canvas: tk.Canvas, width: int, height: int, *, column=None, count=46,
                  seed=7, tag="aurora-star"):
    """稀疏星点（1–2px，不同亮度），只落在消息列以外。固定种子，重绘不跳动。"""
    canvas.delete(tag)
    rng = random.Random(seed)
    tints = ("#FFFFFF", "#CFC6FF", "#A9F0FF", "#FFD2E8", "#FFD2A8")
    placed = 0
    for _ in range(count * 4):
        if placed >= count:
            break
        x, y = rng.uniform(0, width), rng.uniform(0, height)
        if column is not None and column[0] - 24 <= x <= column[1] + 24:
            continue
        size = rng.choice((1, 1, 1, 1.5, 2))
        alpha = rng.uniform(.25, .7)
        colour = _mix(C["chat"], rng.choice(tints), alpha)
        canvas.create_oval(x, y, x + size, y + size, fill=colour, outline="", tags=(tag,))
        placed += 1
    canvas.tag_lower(tag)


def _mix(a: str, b: str, t: float) -> str:
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    return "#%02X%02X%02X" % (round(ra + (rb - ra) * t), round(ga + (gb - ga) * t),
                              round(ba + (bb - ba) * t))


class AuroraBackdrop:
    """把极光挂到一个 Canvas 的最底层，并让它固定在可视区（不随内容滚动）。

    ``column_of()`` 返回当前消息列 (x0, x1)（画布坐标）或 None。尺寸变化后
    防抖重绘；滚动时只移动图像坐标。
    """

    def __init__(self, canvas: tk.Canvas, *, column_of=None, stars=True, **options):
        self.canvas = canvas
        self._column_of = column_of
        self._stars = stars
        self._options = options
        self._item = canvas.create_image(0, 0, anchor="nw", tags=("aurora",))
        canvas.tag_lower(self._item)
        self._photo = None
        self._job = None
        self._key = None
        canvas.bind("<Configure>", self._schedule, add="+")
        canvas.bind("<Destroy>", self._cancel, add="+")

    def _schedule(self, _event=None):
        if self._job is None:
            try:
                # 定时器挂在根窗口上：任何地方按 id 取消都只删一次回调命令，
                # 画布随后销毁时不会再去删一个已不存在的命令。
                self._job = self.canvas._root().after(120, self.render)
            except tk.TclError:
                pass

    def _cancel(self, event):
        if event.widget is self.canvas and self._job is not None:
            try:
                self.canvas._root().after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None

    def render(self):
        self._job = None
        canvas = self.canvas
        try:
            if not canvas.winfo_exists():
                return
            w, h = canvas.winfo_width(), canvas.winfo_height()
        except tk.TclError:
            return
        if w <= 8 or h <= 8:
            return
        column = self._column_of() if self._column_of else None
        key = (w, h, column)
        if key != self._key:
            try:
                self._photo = aurora_photo(canvas, w, h, column=column, **self._options)
                canvas.itemconfigure(self._item, image=self._photo)
            except tk.TclError:
                return
            if self._stars:
                scatter_stars(canvas, w, h, column=column)
            self._key = key
        self.follow_view()

    def follow_view(self):
        """滚动后把图像与星点钉回可视区左上角。"""
        try:
            top = self.canvas.canvasy(0)
            x, y = self.canvas.coords(self._item)
            if abs(y - top) > .5:
                self.canvas.move("aurora", 0, top - y)
                self.canvas.move("aurora-star", 0, top - y)
            self.canvas.tag_lower("aurora-star")
            self.canvas.tag_lower(self._item)
        except (tk.TclError, ValueError):
            pass


__all__ = ["AuroraBackdrop", "aurora_png", "aurora_photo", "colour_at", "scatter_stars"]
