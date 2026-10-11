# -*- coding: utf-8 -*-
"""Canvas 装饰图案库（Tk 原生，无第三方依赖）。

Tk Canvas 没有透明 fill，也没有渐变；这里的柔和观感全部靠
**颜色向背景渐混**模拟：把前景色按比例插值到背景色上，得到一串
越来越接近背景的色阶，画同心圆 / 点阵时就有半透明叠加的错觉。

提供的图形（都是纯 Canvas item，无动画、无定时器，不耗电）：

- ``dot_grid``        点阵：大面积背景的细腻纹理
- ``soft_orb``        柔光球：同心色阶圆，当「光晕」或品牌点缀
- ``constellation``   星座图：节点 + 连线，呼应 agent 网络
- ``circuit_lines``   电路纹：折线 + 端点，科技感角饰
- ``forge_motif``     Forge 火种切面 + Agent 轨道节点
- ``hero_banner``     欢迎屏主视觉：静态 Forge 切面与节点轨道
"""
from __future__ import annotations

import random
import tkinter as tk


def mix(first: str, second: str, amount: float) -> str:
    """两个 #RRGGBB 之间按比例取色；0=first, 1=second。"""
    amount = max(0.0, min(1.0, amount))
    a = tuple(int(first[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(second[i:i + 2], 16) for i in (1, 3, 5))
    rgb = tuple(round(x + (y - x) * amount) for x, y in zip(a, b))
    return "#" + "".join(f"{c:02x}" for c in rgb)


def _hex_ok(value: str) -> bool:
    return (isinstance(value, str) and len(value) == 7
            and value[0] == "#" and all(c in "0123456789abcdefABCDEF"
                                        for c in value[1:]))


def dot_grid(canvas: tk.Canvas, x1, y1, x2, y2, *, bg: str,
             fg: str = "#8A8AF0", step: int = 22, r: float = 1.0,
             amounts=(0.55, 0.72), tag: str = "decor"):
    """点阵纹理：两种深度交替，比单一色点更有层次。"""
    if not (_hex_ok(bg) and _hex_ok(fg)):
        return 0
    colors = [mix(fg, bg, a) for a in amounts]
    n = 0
    row = 0
    y = y1
    while y <= y2:
        x = x1
        col = 0
        while x <= x2:
            color = colors[(row + col) % len(colors)]
            canvas.create_oval(x - r, y - r, x + r, y + r,
                               fill=color, outline="", tags=(tag,))
            n += 1
            x += step
            col += 1
        y += step
        row += 1
    return n


def soft_orb(canvas: tk.Canvas, cx, cy, radius, *, bg: str,
             fg: str = "#7C3AED", layers: int = 9,
             core: str | None = None, tag: str = "decor") -> int:
    """柔光球：从外到内逐层收拢的同心圆，色阶向背景渐混出光晕感。"""
    if not (_hex_ok(bg) and _hex_ok(fg)):
        return 0
    n = 0
    for i in range(layers, 0, -1):
        ratio = i / layers                     # 1.0(外) -> 内
        r = radius * ratio
        # 外层更接近背景（淡），内层更接近前景（浓）；
        # 指数 2.6：让光晕外圈迅速沉入背景，避免一册灰蒙蒙的“大饼”
        color = mix(fg, bg, 0.06 + 0.86 * (ratio ** 2.6))
        canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                           fill=color, outline="", tags=(tag,))
        n += 1
    if core:
        cr = max(2.0, radius * 0.22)
        canvas.create_oval(cx - cr, cy - cr, cx + cr, cy + cr,
                           fill=core, outline="", tags=(tag,))
        n += 1
    return n


def constellation(canvas: tk.Canvas, x1, y1, x2, y2, *, bg: str,
                  node: str = "#A5B4FC", link: str | None = None,
                  count: int = 9, seed: int = 7,
                  node_r: float = 2.2, tag: str = "decor",
                  glow: bool = True) -> int:
    """星座图：随机但可复现（固定 seed）的节点 + 邻近连线。

    ``glow=False`` 时只画实心节点不画光晕（边界处用，避免被画布裁切）。
    """
    rng = random.Random(seed)
    pts = []
    for _ in range(count):
        pts.append((rng.uniform(x1, x2), rng.uniform(y1, y2)))
    if link is None:
        link = node
    n = 0
    # 连线：每个点连到最近的 1~2 个邻居
    for i, (px, py) in enumerate(pts):
        dists = sorted(((px - qx) ** 2 + (py - qy) ** 2, j)
                       for j, (qx, qy) in enumerate(pts) if j != i)
        for _d, j in dists[:2]:
            if j > i:
                qx, qy = pts[j]
                canvas.create_line(px, py, qx, qy, fill=mix(link, bg, 0.62),
                                   width=1, capstyle=tk.ROUND, tags=(tag,))
                n += 1
    # 节点：主点 + 光晕
    for k, (px, py) in enumerate(pts):
        if glow:
            g = node_r * 2.6
            canvas.create_oval(px - g, py - g, px + g, py + g,
                               fill=mix(node, bg, 0.72), outline="", tags=(tag,))
            n += 1
        canvas.create_oval(px - node_r, py - node_r, px + node_r, py + node_r,
                           fill=node, outline="", tags=(tag,))
        n += 1
    return n


def circuit_lines(canvas: tk.Canvas, x1, y1, x2, y2, *, bg: str,
                  fg: str = "#8B5CF6", seed: int = 3,
                  tag: str = "decor") -> int:
    """电路角饰：横竖折线 + 圆端点，放卡片四角很提气。"""
    rng = random.Random(seed)
    n = 0
    for _ in range(3):
        sx = rng.uniform(x1, x2 - 40)
        sy = rng.uniform(y1, y2 - 10)
        length = rng.uniform(24, min(70, x2 - sx - 8))
        mid = sx + length * rng.uniform(0.35, 0.65)
        ey = sy + rng.choice((-1, 1)) * rng.uniform(6, 14)
        color = mix(fg, bg, 0.55)
        canvas.create_line(sx, sy, mid, sy, mid, ey, sx + length, ey,
                           fill=color, width=1.2, joinstyle=tk.ROUND,
                           tags=(tag,))
        for (ex, ey2) in ((sx, sy), (sx + length, ey)):
            canvas.create_oval(ex - 1.6, ey2 - 1.6, ex + 1.6, ey2 + 1.6,
                               fill=mix(fg, bg, 0.3), outline="", tags=(tag,))
            n += 1
        n += 1
    return n


def forge_motif(canvas: tk.Canvas, cx: float, cy: float, size: float, *,
                bg: str, accent: str = "#7C3AED",
                accent2: str = "#5865F2", spark: str = "#F5BC74",
                tag: str = "decor") -> int:
    """绘制静态 Forge 火种切面与 Agent 轨道；坐标和笔画按 size 缩放。

    图形以棱面核心代替发光球，以对称分叉轨道表达 Agent 协作；不触碰
    原有品牌 logo，也不使用定时器或动画。返回 Canvas item 数量。
    """
    if not all(_hex_ok(c) for c in (bg, accent, accent2, spark)):
        return 0
    s = max(18.0, float(size))
    half = s * 0.5
    line = max(1.0, s / 64.0)
    faint = mix(accent2, bg, 0.68)
    rail = mix(accent2, bg, 0.48)
    n = 0

    # 中心六边切面：外框、两侧金属面与中间火种。保留足够的空隙，
    # 在 112px 高的欢迎横幅上仍能读成清晰标记，而不是一团光斑。
    outer = [(cx, cy - half * .72), (cx + half * .58, cy - half * .38),
             (cx + half * .58, cy + half * .38), (cx, cy + half * .72),
             (cx - half * .58, cy + half * .38), (cx - half * .58, cy - half * .38)]
    canvas.create_polygon(*[v for p in outer for v in p], fill=mix(accent2, bg, .84),
                          outline=accent2, width=line, joinstyle=tk.ROUND, tags=(tag,))
    n += 1
    canvas.create_polygon(cx, cy - half * .48, cx - half * .38, cy - half * .25,
                          cx - half * .38, cy + half * .24, cx, cy + half * .47,
                          fill=mix(accent2, bg, .62), outline=faint, width=line,
                          tags=(tag,))
    canvas.create_polygon(cx, cy - half * .48, cx + half * .38, cy - half * .25,
                          cx + half * .38, cy + half * .24, cx, cy + half * .47,
                          fill=mix(accent, bg, .73), outline=faint, width=line,
                          tags=(tag,))
    canvas.create_line(cx - half * .38, cy - half * .25, cx, cy,
                       cx + half * .38, cy - half * .25,
                       fill=accent2, width=line, tags=(tag,))
    canvas.create_line(cx, cy, cx, cy + half * .47,
                       fill=rail, width=line, tags=(tag,))
    n += 4

    # 上方短促的锻火切片，采用棱形而非星点/光晕。
    tip_y = cy - half * .9
    canvas.create_polygon(cx, tip_y - half * .16, cx + half * .095, tip_y,
                          cx, tip_y + half * .2, cx - half * .095, tip_y,
                          fill=spark, outline="", tags=(tag,))
    canvas.create_line(cx, tip_y + half * .18, cx, cy - half * .64,
                       fill=accent, width=line, tags=(tag,))
    n += 2

    # 两段未闭合轨道和左右分叉节点；轨道刻意断开，形成有方向的切口。
    orbit = half * .91
    box = (cx - orbit, cy - orbit, cx + orbit, cy + orbit)
    for start, extent in ((36, 104), (216, 104)):
        canvas.create_arc(*box, start=start, extent=extent, style=tk.ARC,
                          outline=rail, width=line, tags=(tag,))
        n += 1

    branch = half * .58
    reach = half * 1.24
    nodes = []
    for side in (-1, 1):
        core_x = cx + side * half * .58
        mid_x = cx + side * branch
        for dy in (-half * .48, half * .48):
            node_x = cx + side * reach
            node_y = cy + dy * .68
            canvas.create_line(core_x, cy + dy * .42, mid_x, cy + dy * .42,
                               node_x, node_y, fill=rail, width=line,
                               joinstyle=tk.ROUND, tags=(tag,))
            nodes.append((node_x, node_y, side == 1 and dy < 0))
            n += 1
    # 每侧主节点及两枚次节点，尺寸和对比度分级，不画随机星座。
    for nx, ny, primary in nodes:
        r = max(1.6, s * (.045 if primary else .032))
        color = accent if primary else accent2
        canvas.create_oval(nx - r * 1.7, ny - r * 1.7,
                           nx + r * 1.7, ny + r * 1.7,
                           fill=mix(color, bg, .7), outline="", tags=(tag,))
        canvas.create_oval(nx - r, ny - r, nx + r, ny + r,
                           fill=color, outline="", tags=(tag,))
        n += 2
    return n


def hero_banner(parent, width: int, height: int, *, bg: str,
                accent: str = "#7C3AED", accent2: str = "#5865F2",
                seed: int = 11, spark: str = "#F5BC74") -> tk.Canvas:
    """欢迎屏 Forge 主视觉：棱面火种、分叉 Agent 轨道与静态边框刻线。

    ``width``/``height`` 和历史 API 保持不变；seed 保留以兼容现有调用。
    所有绘制均为静态 Canvas item，无动画、无定时器。
    """
    del seed  # API 兼容；几何为固定品牌构图，不再使用随机星座。
    canvas = tk.Canvas(parent, width=width, height=height, bg=bg,
                       highlightthickness=0, bd=0)
    if width <= 0 or height <= 0 or not _hex_ok(bg):
        return canvas

    # 横向切面轨道利用宽画幅留白，中心保持紧凑，边缘不堆叠点阵。
    scale = min(float(height) * .68, float(width) * .27)
    cx, cy = width * .5, height * .5
    fine = mix(accent2, bg, .72) if _hex_ok(accent2) else bg
    line = max(1.0, scale / 64.0)
    left, right = width * .075, width * .925
    rail_y = cy + scale * .48
    canvas.create_line(left, rail_y, cx - scale * 1.02, rail_y,
                       fill=fine, width=line, tags=("decor",))
    canvas.create_line(cx + scale * 1.02, rail_y, right, rail_y,
                       fill=fine, width=line, tags=("decor",))
    # 边缘刻线用同一相对尺度，适配不同横幅尺寸与 Tk DPI 缩放。
    for x, direction in ((left, 1), (right, -1)):
        canvas.create_line(x, rail_y - scale * .11,
                           x + direction * scale * .16, rail_y,
                           x, rail_y + scale * .11,
                           fill=fine, width=line, tags=("decor",))
    forge_motif(canvas, cx, cy, scale, bg=bg, accent=accent,
                accent2=accent2, spark=spark, tag="decor")
    return canvas
