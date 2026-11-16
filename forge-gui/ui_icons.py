"""Font-independent UI icons and pre-rendered emoji. Runtime: tkinter only.

The application logo remains in brand_marks/assets/forge-logo-*.png.
"""
from __future__ import annotations

import json
import math
import re
import base64
import struct
import sys
import tkinter as tk
import zlib
from pathlib import Path
import i18n

# Coordinates use a 24 × 24 grid; all icons share a 1.75-unit rounded stroke.
# Shapes are also consumed by desktop/make_ui_assets.py.
# 2026-11-08 图标刷新：曲线（齿轮、DNA、刷新箭头、人像肩线、对话气泡圆角）由
# 下列小工具在网格上生成折线，canvas 与位图图集共享同一份坐标；
# 「rect」统一画成 2 网格单位圆角，与图集（rounded_rectangle）一致。


def _arc(cx, cy, r, start, end, steps=14):
    """Polyline along a circle; angles in degrees, 0° = east, clockwise on screen."""
    points = []
    for i in range(steps + 1):
        a = math.radians(start + (end - start) * i / steps)
        points += [round(cx + r * math.cos(a), 2), round(cy + r * math.sin(a), 2)]
    return tuple(points)


def _rounded(x1, y1, x2, y2, r, *, tail=()):
    """Closed rounded-rectangle outline; ``tail`` points are spliced into the bottom edge."""
    points = []
    for cx, cy, start in ((x2 - r, y1 + r, 270), (x2 - r, y2 - r, 0),
                          (x1 + r, y2 - r, 90), (x1 + r, y1 + r, 180)):
        points += _arc(cx, cy, r, start, start + 90, 4)
        if start == 0:
            points += tail
    return tuple(points)


# ── v2 图标（2026-11-11 Ember 设计稿）──────────────────────────
# 设计源：design/icons.js。这里用一个极小的 SVG path 解析器把同一份
# path 数据折成 line / poly / circle / dot / fill 原语，canvas 与位图
# 图集（desktop/make_ui_assets.py）共用，不再手抄两套坐标。
# 规格：24 网格、活动区 3–21、1.75 圆头描边、外框圆角 2.5。

_TOKEN = re.compile(r"[MmLlHhVvAaCcSsZz]|-?(?:\d+\.?\d*|\.\d+)(?:[eE]-?\d+)?")


def _arc_points(x1, y1, rx, ry, large, sweep, x2, y2):
    """SVG 端点式椭圆弧 → 折线点（不含起点）。仅支持 x-axis-rotation = 0。"""
    if rx == 0 or ry == 0 or (x1 == x2 and y1 == y2):
        return [x2, y2]
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    lam = dx * dx / (rx * rx) + dy * dy / (ry * ry)
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * dy * dy - ry * ry * dx * dx
    den = rx * rx * dy * dy + ry * ry * dx * dx
    coef = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        coef = -coef
    cxp, cyp = coef * rx * dy / ry, -coef * ry * dx / rx
    cx, cy = cxp + (x1 + x2) / 2, cyp + (y1 + y2) / 2
    start = math.atan2((dy - cyp) / ry, (dx - cxp) / rx)
    end = math.atan2((-dy - cyp) / ry, (-dx - cxp) / rx)
    delta = end - start
    if sweep and delta < 0:
        delta += 2 * math.pi
    elif not sweep and delta > 0:
        delta -= 2 * math.pi
    steps = max(2, math.ceil(abs(delta) / (math.pi / 10)))
    points = []
    for i in range(1, steps + 1):
        a = start + delta * i / steps
        points += [cx + rx * math.cos(a), cy + ry * math.sin(a)]
    return points


def _bezier(x0, y0, c1x, c1y, c2x, c2y, x, y, steps=8):
    points = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        points += [u ** 3 * x0 + 3 * u * u * t * c1x + 3 * u * t * t * c2x + t ** 3 * x,
                   u ** 3 * y0 + 3 * u * u * t * c1y + 3 * u * t * t * c2y + t ** 3 * y]
    return points


def _path(d, *, filled=False):
    """最小 SVG path 解析：M L H V A C S Z（含相对写法）。"""
    tokens = _TOKEN.findall(d)
    shapes, points = [], []
    index, command = 0, None
    x = y = start_x = start_y = 0.0
    control = None

    def number():
        nonlocal index
        value = float(tokens[index])
        index += 1
        return value

    def flush(closed):
        if len(points) >= 4:
            kind = "fill" if filled else ("poly" if closed else "line")
            shapes.append((kind, tuple(round(v, 2) for v in points)))
        points.clear()

    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
            if command in "Zz":
                flush(True)
                x, y = start_x, start_y
                control = None
                continue
        relative = command.islower()
        op = command.upper()
        base_x, base_y = (x, y) if relative else (0.0, 0.0)
        if op == "M":
            flush(False)
            x, y = base_x + number(), base_y + number()
            start_x, start_y = x, y
            points += [x, y]
            command = "l" if relative else "L"
            control = None
        elif op == "L":
            x, y = base_x + number(), base_y + number()
            points += [x, y]
            control = None
        elif op == "H":
            x = base_x + number()
            points += [x, y]
            control = None
        elif op == "V":
            y = base_y + number()
            points += [x, y]
            control = None
        elif op == "A":
            rx, ry, _rotation, large, sweep = number(), number(), number(), number(), number()
            nx, ny = base_x + number(), base_y + number()
            points += _arc_points(x, y, rx, ry, int(large), int(sweep), nx, ny)
            x, y = nx, ny
            control = None
        elif op in "CS":
            if op == "C":
                c1x, c1y = base_x + number(), base_y + number()
            else:
                c1x, c1y = (2 * x - control[0], 2 * y - control[1]) if control else (x, y)
            c2x, c2y = base_x + number(), base_y + number()
            nx, ny = base_x + number(), base_y + number()
            points += _bezier(x, y, c1x, c1y, c2x, c2y, nx, ny)
            control = (c2x, c2y)
            x, y = nx, ny
        else:
            raise ValueError(f"unsupported path command {command!r}")
    flush(False)
    return shapes


def _shapes(*specs, flip=False):
    """紧凑描述：'p …' 描边 path，'P …' 实心 path，'c cx cy r' 圆，'C …' 实心点，
    'r x y w h rx' 圆角矩形，'R …' 实心圆角矩形。"""
    shapes = []
    for spec in specs:
        tag, _, body = spec.partition(" ")
        if tag in "pP":
            shapes += _path(body, filled=tag == "P")
            continue
        values = [float(v) for v in body.split()]
        if tag in "cC":
            cx, cy, r = values
            shapes.append(("circle" if tag == "c" else "dot", (cx - r, cy - r, cx + r, cy + r)))
        elif tag in "rR":
            x0, y0, w, h, r = values
            outline = _rounded(x0, y0, x0 + w, y0 + h, r)
            shapes.append(("poly" if tag == "r" else "fill", outline))
    if flip:
        flipped = []
        for kind, coords in shapes:
            coords = [v if i % 2 == 0 else 24 - v for i, v in enumerate(coords)]
            if kind in ("circle", "dot"):
                coords = [coords[0], coords[3], coords[2], coords[1]]
            flipped.append((kind, tuple(round(v, 2) for v in coords)))
        shapes = flipped
    return shapes


def _gear(teeth=6, outer=9.4, inner=7.1):
    """6 枚宽齿：16px 下不再像 8 窄齿那样起锯齿。"""
    points = []
    for i in range(teeth):
        a = 2 * math.pi * i / teeth - math.pi / 2
        for radius, offset in ((inner, -.4), (outer, -.22), (outer, .22), (inner, .4)):
            points += [round(12 + radius * math.cos(a + offset), 2),
                       round(12 + radius * math.sin(a + offset), 2)]
    return [("poly", tuple(points))] + _shapes("c 12 12 2.9")


def _spark(cx, cy, r):
    k = r * .24
    return (f"p M{cx} {cy - r}L{cx + k} {cy - k}L{cx + r} {cy}L{cx + k} {cy + k}"
            f"L{cx} {cy + r}L{cx - k} {cy + k}L{cx - r} {cy}L{cx - k} {cy - k}Z")


_FILE = "p M7 3h7l5 5v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"
_THUMB = ("p M8 10.5 11.3 3.6a2.1 2.1 0 0 1 2.5 2.4l-.6 3.5h5a2 2 0 0 1 2 2.4l-1.3 6.4a2 2 0 0 1-2 1.7H8z",
          "r 3.5 10.5 4.5 9.5 1.5")

ICONS = {
    "chat": _shapes("p M6 4.5h12A2.5 2.5 0 0 1 20.5 7v8a2.5 2.5 0 0 1-2.5 2.5h-6.5L7 21v-3.5H6A2.5 2.5 0 0 1 3.5 15V7A2.5 2.5 0 0 1 6 4.5z"),
    "task": _shapes("r 5 4.5 14 16.5 2.5", "r 9 2.75 6 3.5 1.25", "p M9 13l2.2 2.2L15.5 11"),
    "agents": _shapes("r 4.5 8 15 11.5 3.5", "p M12 8V5.4", "C 12 4.2 1.2", "C 9.4 13.6 1.15",
                      "C 14.6 13.6 1.15", "p M2.5 12.5v3", "p M21.5 12.5v3"),
    "tools": _shapes("p M4 7h4", "p M12 7h8", "p M4 17h8", "p M16 17h4", "c 10 7 2", "c 14 17 2"),
    "knowledge": _shapes("p M3 5.5c3-.8 6-.5 9 1.5v13c-3-2-6-2.3-9-1.5z",
                         "p M21 5.5c-3-.8-6-.5-9 1.5v13c3-2 6-2.3 9-1.5z"),
    # 演化 = 新芽（生长），不再用 DNA 双螺旋。
    "evolution": _shapes("p M12 21v-8.5", "p M12 14.5c-4 0-6.8-2.5-6.8-6.8 4 0 6.8 2.7 6.8 6.8z",
                         "p M12 12c0-4.3 2.7-7.3 6.8-7.3 0 4.3-2.7 7.3-6.8 7.3z"),
    "files": _shapes("p M3.5 7a2 2 0 0 1 2-2h3.8l2 2.2h7.2a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"),
    "file": _shapes(_FILE, "p M14 3v5h5", "p M9 13h6", "p M9 17h4"),
    "code": _shapes("p M8.5 7.5 4 12l4.5 4.5", "p M15.5 7.5 20 12l-4.5 4.5", "p M13.5 5l-3 14"),
    "config": _gear(),
    "search": _shapes("c 10.5 10.5 6.5", "p M15.4 15.4 20.5 20.5"),
    "plus": _shapes("p M12 5v14", "p M5 12h14"),
    "minus": _shapes("p M5 12h14"),
    "close": _shapes("p M6.5 6.5l11 11", "p M17.5 6.5l-11 11"),
    # Filled dots keep the overflow affordance legible at 16–20px.
    "more": _shapes("C 5.5 12 1.5", "C 12 12 1.5", "C 18.5 12 1.5"),
    "chevron_right": _shapes("p M9.5 6l6 6-6 6"),
    "chevron_left": _shapes("p M14.5 6l-6 6 6 6"),
    "chevron_down": _shapes("p M6 9.5l6 6 6-6"),
    "chevron_up": _shapes("p M6 14.5l6-6 6 6"),
    "hexagram": _shapes("p M5 6h14", "p M5 12h5.5", "p M13.5 12H19", "p M5 18h14"),
    "refresh": _shapes("p M20 12a8 8 0 1 1-2.34-5.66L20 8.5", "p M20 4v4.5h-4.5"),
    "external": _shapes("p M14 4h6v6", "p M20 4l-8.5 8.5",
                        "p M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"),
    "sidebar": _shapes("r 3.5 4.5 17 15 2.5", "p M9.5 4.5v15"),
    "workspace": _shapes("r 3.5 4.5 17 15 2.5", "p M3.5 9.5h17", "p M10 9.5v10"),
    "diff": _shapes(_FILE, "p M12 8.5v5", "p M9.5 11h5", "p M9.5 17h5"),
    "send": _shapes("p M12 19V5.5", "p M6 11.5l6-6 6 6"),
    "play": _shapes("P M8 5.6v12.8a1 1 0 0 0 1.53.85l10.1-6.4a1 1 0 0 0 0-1.7L9.53 4.75A1 1 0 0 0 8 5.6z"),
    "stop": _shapes("R 6.5 6.5 11 11 2.2"),
    "check": _shapes("p M5 12.5l4.5 4.5L19 7.5"),
    "check_circle": _shapes("c 12 12 9", "p M8 12.3l2.7 2.7L16 9.7"),
    # Warning keeps its exclamation dot as the third shape (size-specific mark).
    "warning": _shapes("p M10.27 4.3a2 2 0 0 1 3.46 0l7.2 12.7a2 2 0 0 1-1.73 3H4.8a2 2 0 0 1-1.73-3z",
                       "p M12 9.5v4", "C 12 16.8 1.1"),
    "error": _shapes("c 12 12 9", "p M9 9l6 6", "p M15 9l-6 6"),
    "info": _shapes("c 12 12 9", "p M12 11v5.5", "C 12 7.8 1.1"),
    "copy": _shapes("r 8.5 8.5 12 12 2.5", "p M15.5 8.5V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7.5a2 2 0 0 0 2 2h2.5"),
    "thumb_up": _shapes(*_THUMB),
    "thumb_down": _shapes(*_THUMB, flip=True),
    "paperclip": _shapes("p M20 11.5l-7.8 7.8a5 5 0 0 1-7.1-7.1l8.2-8.2a3.3 3.3 0 0 1 4.7 4.7l-8.2 8.2a1.7 1.7 0 0 1-2.4-2.4L15 6.9"),
    "key": _shapes("c 8 15.5 4.5", "p M11.2 12.3 20 3.5", "p M16.5 7l2.5 2.5", "p M14 9.5l2 2"),
    "model": _shapes("p M12 3 20 7.5v9L12 21l-8-4.5v-9z", "p M4 7.5l8 4.5 8-4.5", "p M12 12v9"),
    "terminal": _shapes("r 3 4.5 18 15 2.5", "p M7 9.5l3 2.5-3 2.5", "p M12.5 15H17"),
    "preview": _shapes("p M2.5 12C4 9 7.5 5.5 12 5.5S20 9 21.5 12C20 15 16.5 18.5 12 18.5S4 15 2.5 12z", "c 12 12 3"),
    "image": _shapes("r 3.5 4 17 16 2.5", "c 9 9.5 1.6", "p M20.5 15.5 16 11l-9 9"),
    "user": _shapes("c 12 8 4", "p M4.5 20.5a7.5 7.5 0 0 1 15 0"),
    "edit": _shapes("p M15.5 4.5l4 4L9 19H5v-4z", "p M13.2 6.8l4 4"),
    "folder_open": _shapes("p M3.5 18V7a2 2 0 0 1 2-2h3.8l2 2.2h6.2a2 2 0 0 1 2 2V11",
                           "p M3.6 18.4 6.1 12a1.5 1.5 0 0 1 1.4-1H20a1 1 0 0 1 .94 1.35L18.6 18.5a1.5 1.5 0 0 1-1.4 1H4.5a1 1 0 0 1-.9-1.1z"),
    "save": _shapes("p M6 3.5h10l4 4v11a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2z",
                    "p M8 3.5v4h7v-4", "p M8 20.5v-6h8v6"),
    # 品牌火焰：颜色由调用方取 gui_theme 的 brand_spark / ember。
    "flame": _shapes("p M12 21c-3.9 0-6.5-2.6-6.5-6.2 0-3.6 2.7-5.6 4-8.8.6 1.8 1.6 2.8 2.8 3.4C12.6 6.6 13.6 4.4 15.5 3c-.3 3 3 5.9 3 11.3 0 4-2.7 6.7-6.5 6.7z",
                     "p M12 21c-1.7 0-2.8-1.2-2.8-2.8 0-1.8 1.4-2.7 2.3-4.2.9 1.5 3.3 2.3 3.3 4.4 0 1.5-1.1 2.6-2.8 2.6z"),
    "clock": _shapes("c 12 12 9", "p M12 7.5V12l3 2"),
    "connector": _shapes("p M9 3v4", "p M15 3v4", "p M6.5 7h11v3.5a5.5 5.5 0 0 1-11 0z", "p M12 16v5"),
    # ── v2 新增 ──
    "inspector": _shapes("r 3.5 4.5 17 15 2.5", "p M14.5 4.5v15", "p M17 8.5h1", "p M17 11.5h1"),
    "branch": _shapes("c 6.5 5.5 2", "c 6.5 18.5 2", "c 17.5 8 2", "p M6.5 7.5v9", "p M17.5 10c0 3-2.5 4-6 4H6.5"),
    "history": _shapes("p M3.6 12A8.5 8.5 0 1 0 6 6", "p M3.5 3.8V8h4.2", "p M12 8v4l2.8 1.8"),
    "pin": _shapes("p M9 3.5h6", "p M10 3.5V9l-3.5 4h11L14 9V3.5", "p M12 13v7.5"),
    "spark": _shapes(_spark(12, 12, 8.5)),
    "sparkles": _shapes(_spark(10, 13, 7), _spark(18.5, 5.5, 3)),
    "bolt": _shapes("p M13 3 5 13.5h6L10.5 21 19 10.5h-6z"),
    "cpu": _shapes("r 6.5 6.5 11 11 2", "r 9.5 9.5 5 5 1", "p M9.5 3.5v3", "p M14.5 3.5v3", "p M9.5 17.5v3",
                   "p M14.5 17.5v3", "p M3.5 9.5h3", "p M3.5 14.5h3", "p M17.5 9.5h3", "p M17.5 14.5h3"),
    "globe": _shapes("c 12 12 9", "p M3 12h18",
                     "p M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"),
    "cursor": _shapes("p M5 4l5.5 15 2.3-6.2L19 10.5z", "p M13.2 13.2 18.5 18.5"),
    "timeline": _shapes("c 6 5.5 1.9", "c 6 12 1.9", "c 6 18.5 1.9", "p M6 7.4v2.7", "p M6 13.9v2.7",
                        "p M10.5 5.5h9", "p M10.5 12h6.5", "p M10.5 18.5h8"),
    "mic": _shapes("r 9 3 6 11 3", "p M5.5 11a6.5 6.5 0 0 0 13 0", "p M12 17.5V21"),
    "at": _shapes("c 12 12 3.5", "p M15.5 12v1.5a2.5 2.5 0 0 0 5 0V12a8.5 8.5 0 1 0-3.5 6.9"),
    "command": _shapes("r 3.5 3.5 17 17 4", "p M14 8l-4 8"),
    "layers": _shapes("p M12 3.5 21 8l-9 4.5L3 8z", "p M3 12.5l9 4.5 9-4.5", "p M3 16.5l9 4.5 9-4.5"),
    # 品牌标：铁砧 + 火花（Forge = 锻造）。
    "anvil": _shapes("p M3.5 7.5h11.5c.3 2.6 2.2 4.1 5.5 4.3v1.7h-5.3L12.8 16v2h2.7v2.5H6.5V18h2.7v-2L6.8 13.6C4.7 12.4 3.5 10.3 3.5 7.5z",
                     _spark(19, 4.5, 2.6)),
    "pause": _shapes("R 6.5 5.5 3.8 13 1.3", "R 13.7 5.5 3.8 13 1.3"),
    "retry": _shapes("p M4 12a8 8 0 0 1 13.66-5.66L20 8.5", "p M20 4v4.5h-4.5",
                     "p M20 12a8 8 0 0 1-13.66 5.66L4 15.5", "p M4 20v-4.5h4.5"),
    "keyboard": _shapes("r 2.5 6 19 12 2.5", "C 6.5 10 .9", "C 10 10 .9", "C 13.5 10 .9", "C 17 10 .9",
                        "p M7.5 14.5h9"),
}

# Compatibility fallback only; the shipped masks use distinct Lucide geometry.
ICONS['plugin'] = ICONS.get('plugin', ICONS['tools'])
ICONS['context'] = ICONS.get('context', ICONS['copy'])
ICONS['computer'] = ICONS.get('computer', ICONS['workspace'])

ALIASES = {
    '◷':'clock', '⏰':'clock', '🔌':'connector', '🛠':'code',
    "💬": "chat", "✅": "check_circle", "☑": "task", "🤖": "agents", "🧰": "tools",
    "📚": "knowledge", "🧬": "evolution", "📁": "files", "📂": "folder_open", "📄": "file",
    "🐍": "code", "⚙": "config", "⚙️": "config", "🔍": "search", "⌗": "search",
    "＋": "plus", "+": "plus", "−": "minus", "✕": "close", "×": "close", "✖": "close",
    "⋯": "more", "…": "more", "▸": "chevron_right", "▾": "chevron_down", "▴": "chevron_up", "⌃": "chevron_up",
    "⌄": "chevron_down", "◀": "chevron_left", "▶": "play", "↑": "send", "■": "stop",
    "⟳": "refresh", "↗": "external", "☰": "sidebar", "≪": "chevron_left", "≫": "chevron_right", "▤": "workspace",
    "⑂": "diff", "✓": "check", "✔": "check", "⚠": "warning", "⚠️": "warning",
    "❌": "error", "✗": "error", "ℹ": "info", "⧉": "copy", "👍": "thumb_up", "👎": "thumb_down",
    "📎": "paperclip", "🔑": "key", "▣": "model", "⬡": "model", "☷": "hexagram", "✎": "edit", "🖼": "image",
    "📋": "task", "💾": "save",
}


def icon_key(value):
    return value if value in ICONS else ALIASES.get(value)


def split_icon_text(text):
    prefix, separator, rest = text.partition(" ")
    key = icon_key(prefix)
    return (key, rest.lstrip()) if key and separator else (None, text)


# 默认色与 gui_theme.C 同源（bg / muted）。ui_icons 被 gui_theme 导入，
# 不能反向 import gui_theme（循环依赖），故此处保持字面值同步。
def draw_icon(canvas, name, *, x=0, y=0, size=24, fg="#A2ADC5", tag="icon"):
    name = icon_key(name) or "file"
    bitmap = _lucide_image(canvas, name, size=size, fg=fg)
    if bitmap is not None:
        canvas.create_image(x, y, image=bitmap, anchor=tk.NW, tags=tag)
        # Owning-interpreter cache retains the image across widget repaints.
        return
    factor = size / 24
    width = max(1.2, factor * 1.75)
    for kind, points in ICONS[name]:
        coordinates = [v * factor + (x if i % 2 == 0 else y) for i, v in enumerate(points)]
        if kind == "line":
            canvas.create_line(*coordinates, fill=fg, width=width, capstyle=tk.ROUND,
                               joinstyle=tk.ROUND, tags=tag)
        elif kind == "circle":
            canvas.create_oval(*coordinates, outline=fg, width=width, tags=tag)
        elif kind == "rect":
            # Same 2-unit corner radius as the bitmap atlas (rounded_rectangle).
            x1, y1, x2, y2 = coordinates
            radius = min(2 * factor, (x2 - x1) / 2, (y2 - y1) / 2)
            outline = []
            for cx, cy, start in ((x2 - radius, y1 + radius, 270), (x2 - radius, y2 - radius, 0),
                                  (x1 + radius, y2 - radius, 90), (x1 + radius, y1 + radius, 180)):
                for step in range(4):
                    angle = math.radians(start + 90 * step / 3)
                    outline += [cx + radius * math.cos(angle), cy + radius * math.sin(angle)]
            canvas.create_polygon(*outline, outline=fg, fill="", width=width,
                                  joinstyle=tk.ROUND, tags=tag)
        elif kind == "dot":
            canvas.create_oval(*coordinates, outline="", fill=fg, tags=tag)
        elif kind == "fill":
            # 实心块（播放 / 停止 / 暂停）：描边同色，圆角连接，小尺寸不缩水。
            canvas.create_polygon(*coordinates, outline=fg, fill=fg, width=max(1, width * .5),
                                  joinstyle=tk.ROUND, tags=tag)
        else:
            canvas.create_polygon(*coordinates, outline=fg, fill="", width=width,
                                  joinstyle=tk.ROUND, tags=tag)


class IconCanvas(tk.Canvas):
    def __init__(self, parent, name, *, size=20, hit_size=None, bg="#0D141F", fg="#A2ADC5", command=None):
        hit_size = max(size, hit_size or size)
        super().__init__(parent, width=hit_size, height=hit_size, bg=bg, bd=0, highlightthickness=0,
                         takefocus=bool(command), cursor="hand2" if command else "arrow")
        self.name, self._fg, self._size = (icon_key(name) or "file") if name else None, fg, size
        self._text = name
        self._hit_size = hit_size
        self._command = command
        self._base_bg = bg
        self._state = tk.NORMAL
        self._hovered = False
        self._pressed = False
        self._focused = False
        if command:
            self.bind("<Return>", self._activate)
            self.bind("<space>", self._activate)
            self.bind("<Enter>", self._enter)
            self.bind("<Leave>", self._leave)
            self.bind("<ButtonPress-1>", self._press)
            self.bind("<ButtonRelease-1>", self._release, add="+")
            self.bind("<FocusIn>", self._focus_in)
            self.bind("<FocusOut>", self._focus_out)
        self._draw()

    @staticmethod
    def _blend(color, target, amount):
        """Blend #RRGGBB colors without timers or per-frame animation."""
        if not (isinstance(color, str) and len(color) == 7 and color.startswith("#")):
            return color
        try:
            source = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
        except ValueError:
            return color
        mixed = tuple(round(value + (goal - value) * amount) for value, goal in zip(source, target))
        return "#%02X%02X%02X" % mixed

    def _visual_bg(self):
        if self._state == tk.DISABLED:
            return self._base_bg
        if self._pressed:
            return self._blend(self._base_bg, (0, 0, 0), .14)
        if self._hovered:
            return self._blend(self._base_bg, (255, 255, 255), .10)
        return self._base_bg

    def _visual_fg(self):
        if self._state == tk.DISABLED:
            return self._blend(self._fg, (128, 128, 128), .52)
        return self._blend(self._fg, (255, 255, 255), .25) if self._hovered else self._fg

    def _redraw_visual(self):
        if not hasattr(self, "name"):
            return
        enabled = self._state == tk.NORMAL and self._command
        super().configure(bg=self._visual_bg(), cursor="hand2" if enabled else "arrow",
                          takefocus=bool(enabled))
        self._draw()

    def _activate(self, _event=None):
        if self._state == tk.NORMAL and self._command:
            self._command()
        return "break"

    def _enter(self, _event=None):
        self._hovered = self._state == tk.NORMAL
        self._redraw_visual()

    def _leave(self, _event=None):
        self._hovered = self._pressed = False
        self._redraw_visual()

    def _press(self, _event=None):
        if self._state == tk.NORMAL:
            self._pressed = True
            self._redraw_visual()
            self._command()

    def _release(self, _event=None):
        if self._pressed:
            self._pressed = False
            self._redraw_visual()

    def _focus_in(self, _event=None):
        self._focused = True
        self._redraw_visual()

    def _focus_out(self, _event=None):
        self._focused = False
        self._redraw_visual()

    def _draw(self):
        self.delete("icon")
        self.delete("focus")
        if self.name:
            offset = (self._hit_size - self._size) / 2
            draw_icon(self, self.name, x=offset, y=offset, size=self._size, fg=self._visual_fg())
        if self._focused and self._state == tk.NORMAL:
            inset = max(1, round(self._size / 16))
            self.create_rectangle(inset, inset, self._hit_size - inset, self._hit_size - inset,
                                  outline="#FFC7A6", width=1, tags="focus")  # 与 gui_theme.focus_ring 同源

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, dict):
            kwargs = {**cnf, **kwargs}
            cnf = None
        if "fg" in kwargs:
            self._fg = kwargs.pop("fg")
        if "bg" in kwargs:
            self._base_bg = kwargs["bg"]
        if "state" in kwargs:
            self._state = kwargs.pop("state")
            if self._state != tk.NORMAL:
                self._hovered = self._pressed = False
        if "text" in kwargs:
            self._text = kwargs.pop("text")
            self.name = icon_key(self._text.strip()) if self._text else None
        result = super().configure(cnf, **kwargs) if cnf is not None else super().configure(**kwargs)
        if hasattr(self, "name"):
            self._redraw_visual()
        return result

    config = configure

    def cget(self, key):
        if key == "fg":
            return self._fg
        if key == "text":
            return self._text
        if key == "state":
            return self._state
        return super().cget(key)


_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / "ui"
_manifest = None
_lucide_masks = None


def _lucide_image(master, name, *, size, fg):
    """Antialiased library geometry, exact size/color, tkinter-only runtime."""
    global _lucide_masks
    if _lucide_masks is None:
        try:
            _lucide_masks = json.loads((_ROOT / 'lucide-masks.json').read_text(encoding='utf-8'))['masks']
        except (OSError, ValueError, KeyError):
            _lucide_masks = {}
    size = max(1, round(size))
    encoded = _lucide_masks.get(name, {}).get(str(size))
    if encoded is None:
        return None
    owner = master._root()
    cache = getattr(owner, '_forge_ui_images', None)
    if cache is None:
        cache = owner._forge_ui_images = {}
    rgb = tuple(value // 257 for value in master.winfo_rgb(fg))
    key = ('lucide', name, size, rgb)
    if key not in cache:
        alpha = zlib.decompress(base64.b64decode(encoded))
        pixel = bytes(rgb)
        rows = b''.join(b'\0' + b''.join(pixel + bytes((a,)) for a in alpha[y * size:(y + 1) * size])
                        for y in range(size))

        def chunk(kind, data):
            return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))

        png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', size, size, 8, 6, 0, 0, 0))
               + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))
        cache[key] = tk.PhotoImage(master=master, data=base64.b64encode(png), format='png')
    return cache[key]


def _metadata():
    global _manifest
    if _manifest is None:
        try:
            _manifest = json.loads((_ROOT / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _manifest = {"icons": {}, "emoji": {}}
    return _manifest


def _tile(master, atlas_name, index, tile_size):
    # Cache on the owning Tk interpreter, never on the process/default root.
    owner = master._root()
    cache = getattr(owner, "_forge_ui_images", None)
    if cache is None:
        cache = owner._forge_ui_images = {}
    key = (atlas_name, index, tile_size)
    if key in cache:
        return cache[key]
    try:
        # Tk's PNG decoder stalls the UI on the full 1,382-emoji sheet.
        # Build-time pages cap each decode to 256 sprites, retaining exact
        # pixels and interpreter-local caching without a runtime dependency.
        source_name, source_index = atlas_name, index
        if atlas_name.startswith('emoji-'):
            page_name = atlas_name[:-4] + f'-page-{index // 256}.png'
            if (_ROOT / page_name).is_file():
                source_name, source_index = page_name, index % 256
        atlas_key = ("atlas", source_name)
        if atlas_key not in cache:
            cache[atlas_key] = tk.PhotoImage(master=master, file=str(_ROOT / source_name))
        atlas = cache[atlas_key]
        x, y = (source_index % 32) * tile_size, (source_index // 32) * tile_size
        tile = tk.PhotoImage(master=master, width=tile_size, height=tile_size)
        tile.tk.call(str(tile), "copy", str(atlas), "-from", x, y, x + tile_size, y + tile_size)
        cache[key] = tile
        return tile
    except (tk.TclError, OSError):
        return None


def asset_image(master, stem, size):
    """assets/ui/{stem}-{n}.png 中挑最接近 size 的一张（品牌块 / hero 光晕）。

    与图集共用按 Tk 解释器隔离的缓存；找不到文件时返回 None，调用方回退绘制。
    """
    sizes = sorted(int(path.stem.rsplit("-", 1)[1]) for path in _ROOT.glob(f"{stem}-*.png")
                   if path.stem.rsplit("-", 1)[1].isdigit())
    if not sizes:
        return None
    best = min(sizes, key=lambda n: (abs(n - size), -n))
    owner = master._root()
    cache = getattr(owner, "_forge_ui_images", None)
    if cache is None:
        cache = owner._forge_ui_images = {}
    key = ("asset", stem, best)
    if key not in cache:
        try:
            cache[key] = tk.PhotoImage(master=master, file=str(_ROOT / f"{stem}-{best}.png"))
        except tk.TclError:
            return None
    return cache[key]


# 位图图集的固定色板：icon_image 按最近色挑图集。与 gui_theme.C 同步
# （Ember 设计稿）：accent = 熔铁橙，ink = 橙底按钮上的深色图标。
PALETTE = {"muted": "#A0A8B6", "text": "#EEF0F4", "accent": "#FF7A3D",
           "ok": "#47C59B", "warn": "#E9B65A", "error": "#F27B7B",
           "disabled": "#7F8898", "info": "#82A8FF", "ink": "#1E0F06"}


def icon_image(master, name, *, size=20, fg="#9A9AA8"):
    name = icon_key(name)
    bitmap = _lucide_image(master, name, size=size, fg=fg)
    if bitmap is not None:
        return bitmap
    index = _metadata()["icons"].get(name)
    if index is None:
        return None
    def distance(color):
        try:
            return sum((int(fg[i:i+2], 16) - int(color[i:i+2], 16)) ** 2 for i in (1, 3, 5))
        except ValueError:
            return 0
    color = min(PALETTE, key=lambda key: distance(PALETTE[key]))
    size = 20 if size <= 20 else 24 if size <= 24 else 28
    return _tile(master, f"icons-{color}-{size}.png", index, size)


class IconButton(tk.Button):
    """Native activation, bitmap icons and optional event-driven visual feedback."""
    _COLOR_ALIASES = {"background": "bg", "foreground": "fg"}

    def __init__(self, parent, *, icon=None, icon_size=20, **kwargs):
        self._feedback = None
        kwargs = {self._COLOR_ALIASES.get(key, key): value
                  for key, value in kwargs.items()}
        self._explicit_icon = icon_key(icon) if icon else None
        self._icon_size = icon_size
        self._icon_name = None
        text = i18n.as_message(kwargs.get("text", ""))
        localized_text = text
        text = i18n.resolve(text, parent)
        kwargs["text"] = text
        name, label = split_icon_text(text)
        if icon_key(text):
            name, label = icon_key(text), ""
        self._icon_name = self._explicit_icon or name
        fg = (kwargs.get("disabledforeground", "#9A9AA8") if kwargs.get("state") == tk.DISABLED
              else kwargs.get("fg", "#9A9AA8"))
        image = icon_image(parent, self._icon_name, size=icon_size, fg=fg)
        if image is not None:
            kwargs.update(text=label, image=image, compound=tk.LEFT)
        super().__init__(parent, **kwargs)
        self._icon_reference = image
        i18n.remember(self, localized_text, method="_set_localized_text")

    def _set_localized_text(self, text):
        self.configure(text=text)

    def set_feedback(self, palette):
        """Theme supplies colours; native Button keeps mouse/Space activation.

        Keep intended colours separately from transient hover/disabled paint so
        status updates made while disabled are restored correctly on enable.
        No animation, timer, global binding or replacement widget is involved.
        """
        first = self._feedback is None
        self._feedback = dict(palette)
        custom_border = getattr(self, "_feedback_outline_override", None)
        if custom_border is not None:
            self._feedback["hover_border"] = custom_border
        if first:
            keys = ("bg", "fg", "activebackground", "activeforeground", "highlightbackground",
                    "highlightcolor", "disabledforeground", "cursor", "takefocus")
            self._feedback_base = {key: self.cget(key) for key in keys}
            self._feedback_hovered = self._feedback_pressed = self._feedback_focused = False
            for sequence, changes in (
                ("<Enter>", {"hovered": True}), ("<Leave>", {"hovered": False, "pressed": False}),
                ("<ButtonPress-1>", {"pressed": True}), ("<ButtonRelease-1>", {"pressed": False}),
                ("<FocusIn>", {"focused": True}), ("<FocusOut>", {"focused": False, "pressed": False}),
            ):
                self.bind(sequence, lambda _e, values=changes: self._change_feedback(**values), add="+")
            self.bind("<Return>", self._keyboard_invoke, add="+")
        self._feedback_visual = None
        self._paint_feedback()
        return self

    def _keyboard_invoke(self, _event=None):
        self.invoke()  # Native invoke already rejects disabled state.
        return "break"

    def _change_feedback(self, **changes):
        for key, value in changes.items():
            setattr(self, "_feedback_" + key, value)
        self._paint_feedback()

    def _paint_feedback(self):
        if self._feedback is None or not self.winfo_exists():
            return
        base, palette = self._feedback_base, self._feedback
        disabled = str(self.cget("state")) == tk.DISABLED
        if disabled:
            self._feedback_hovered = self._feedback_pressed = False
            bg, fg, border = palette["disabled_bg"], base["disabledforeground"], palette["disabled_border"]
        else:
            active = self._feedback_hovered or self._feedback_pressed
            bg = (palette["pressed_bg"] if self._feedback_pressed else
                  base["activebackground"] if self._feedback_hovered else base["bg"])
            fg = base["activeforeground"] if active else base["fg"]
            border = (base["highlightcolor"] if self._feedback_focused else
                      palette["hover_border"] if active else base["highlightbackground"])
        visual = {"bg": bg, "fg": fg, "activebackground": bg, "activeforeground": fg,
                  "highlightbackground": border,
                  "cursor": "arrow" if disabled else base["cursor"],
                  "takefocus": 0 if disabled else base["takefocus"]}
        if visual == self._feedback_visual:
            return
        tk.Button.configure(self, **visual)
        if self._icon_name:
            image = icon_image(self, self._icon_name, size=self._icon_size, fg=fg)
            if image is not None:
                tk.Button.configure(self, image=image, compound=tk.LEFT)
                self._icon_reference = image
        self._feedback_visual = visual

    def configure(self, cnf=None, **kwargs):
        if isinstance(cnf, dict):
            kwargs = {**cnf, **kwargs}
            cnf = None
        kwargs = {self._COLOR_ALIASES.get(key, key): value
                  for key, value in kwargs.items()}
        if self._feedback is not None and kwargs:
            for key, value in kwargs.items():
                if key in self._feedback_base:
                    self._feedback_base[key] = value
                if key == "highlightbackground":
                    # A caller's semantic outline (e.g. orange planning) must
                    # survive hovering, not revert to the default purple/grey.
                    self._feedback["hover_border"] = value
                    self._feedback_outline_override = value
            self._feedback_visual = None
        if "text" in kwargs:
            kwargs["text"] = i18n.as_message(kwargs["text"])
            i18n.remember(self, kwargs["text"], method="_set_localized_text")
            text = i18n.resolve(kwargs["text"], self)
            kwargs["text"] = text
            name, label = split_icon_text(text)
            if icon_key(text):
                name, label = icon_key(text), ""
            self._icon_name = self._explicit_icon or name
            kwargs["text"] = label
        if self._icon_name and any(key in kwargs for key in ("text", "fg", "state", "disabledforeground")):
            disabled = str(kwargs.get("state", self.cget("state"))) == tk.DISABLED
            fg = (kwargs.get("disabledforeground", self.cget("disabledforeground")) if disabled
                  else kwargs.get("fg", self.cget("fg")))
            image = icon_image(self, self._icon_name, size=self._icon_size, fg=fg)
            if image is not None:
                kwargs.update(image=image, compound=tk.LEFT)
                self._icon_reference = image
        elif "text" in kwargs and not self._icon_name:
            kwargs["image"] = ""
            self._icon_reference = None
        result = super().configure(cnf, **kwargs) if cnf is not None else super().configure(**kwargs)
        if kwargs:
            self._paint_feedback()
        return result

    config = configure


def emoji_parts(text):
    """Yield complete emoji clusters; unknown clusters remain exact Unicode text."""
    def starts_at(position):
        cp = ord(text[position])
        return (0x1F000 <= cp <= 0x1FAFF or 0x2600 <= cp <= 0x27BF
                or cp in (0x231A, 0x231B, 0x23F0, 0x23F3)
                or (text[position] in "0123456789#*"
                    and (text[position+1:position+2] == "\u20e3"
                         or text[position+1:position+3] == "\ufe0f\u20e3")))
    index = 0
    while index < len(text):
        char = text[index]
        cp = ord(char)
        if not starts_at(index):
            end = index + 1
            while end < len(text) and not starts_at(end):
                end += 1
            yield text[index:end], False
            index = end
            continue
        end = index + 1
        if 0x1F1E6 <= cp <= 0x1F1FF and end < len(text) and 0x1F1E6 <= ord(text[end]) <= 0x1F1FF:
            end += 1
        while end < len(text):
            other = ord(text[end])
            if other in (0xFE0E, 0xFE0F, 0x20E3) or 0x1F3FB <= other <= 0x1F3FF or 0xE0020 <= other <= 0xE007F:
                end += 1
            elif other == 0x200D and end + 1 < len(text):
                end += 2
            else:
                break
        yield text[index:end], True
        index = end


def emoji_image(master, cluster, *, size=24):
    # FE0E explicitly requests text presentation; honor it.
    if "\ufe0e" in cluster:
        return None
    normalized = cluster.replace("\ufe0f", "")
    index = _metadata()["emoji"].get(normalized)
    if index is None:
        return None
    size = 20 if size <= 20 else 24 if size <= 24 else 32
    return _tile(master, f"emoji-{size}.png", index, size)
