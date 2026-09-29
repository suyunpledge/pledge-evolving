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
- ``hero_banner``     欢迎屏主视觉：orb + 星座 + 点阵的组合画
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


def hero_banner(parent, width: int, height: int, *, bg: str,
                accent: str = "#7C3AED", accent2: str = "#5865F2",
                seed: int = 11) -> tk.Canvas:
    """欢迎屏主视觉：柔光球 + 星座 + 点阵 + 电路纹的组合画布。"""
    canvas = tk.Canvas(parent, width=width, height=height, bg=bg,
                       highlightthickness=0, bd=0)
    pad = 8  # 光晕最大半径约 6px；留 8px 边距防止被画布裁切
    # 背景点阵（整幅，步距大一点保持克制）
    dot_grid(canvas, pad + 8, pad + 4, width - pad - 8, height - pad - 4,
             bg=bg, fg=accent2, step=24, r=1.0)
    # 左右两团柔光球，主体居中偏上
    soft_orb(canvas, width * 0.30, height * 0.46, height * 0.40,
             bg=bg, fg=accent2, layers=10)
    soft_orb(canvas, width * 0.72, height * 0.58, height * 0.28,
             bg=bg, fg=accent, layers=8)
    # 中央星座（留出光晕边距）
    constellation(canvas, width * 0.16, pad + 8,
                  width * 0.84, height - pad - 8, bg=bg, count=10, seed=seed)
    # 角落电路纹
    circuit_lines(canvas, pad + 4, pad + 4, width * 0.4, height - pad - 4,
                  bg=bg, seed=seed)
    circuit_lines(canvas, width * 0.6, pad + 4, width - pad - 4, height - pad - 4,
                  bg=bg, fg=accent2, seed=seed + 1)
    # 中心主 orb（品牌核）
    soft_orb(canvas, width / 2, height * 0.46, height * 0.24,
             bg=bg, fg=accent, layers=9, core="#EDE9FE")
    return canvas
