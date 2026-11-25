"""Editable XY charts. The editor and Work render/read the same chart-local scene."""
import json
import math
import re

from .math_tools import expression

CHART_FIELD = re.compile(r"T([1-9]\d?)\.chart\Z")
RANGE = re.compile(r"([A-Z]{1,2})([1-9]\d{0,3}):\1([1-9]\d{0,3})\Z")
WIDTH, HEIGHT = 640, 300
LEFT, RIGHT, TOP, BOTTOM = 80, 580, 59, 245
COLOR = "#3679b8"


def cells(ref):
    m = RANGE.fullmatch(ref) if isinstance(ref, str) else None
    if not m or not 2 <= int(m[3]) - int(m[2]) + 1 <= 100:
        raise ValueError("图表数据请使用同一列的 2 至 100 个单元格，如 A2:A12。")
    return [m[1] + str(n) for n in range(int(m[2]), int(m[3]) + 1)]


def parse_chart(raw):
    if not isinstance(raw, str) or len(raw) > 2000:
        raise ValueError("图表配置过长或无效。")
    spec = json.loads(raw)
    if not isinstance(spec, dict):
        raise ValueError("图表配置应为 JSON 对象。")
    allowed = {"title", "xTitle", "yTitle", "color", "labels", "style"}
    if "expression" in spec:
        allowed.update(("expression", "min", "max"))
        expression(spec["expression"])
        lo, hi = spec.get("min", -5), spec.get("max", 5)
        if (any(type(v) not in (float, int) or abs(v) > 1e100 for v in (lo, hi))
                or not lo < hi):
            raise ValueError("请填写有效且递增的函数区间。")
    else:
        allowed.update(("xRange", "yRange"))
        if len(cells(spec.get("xRange"))) != len(cells(spec.get("yRange"))):
            raise ValueError("X 与 Y 数据区域的行数必须一致。")
    if spec.keys() - allowed:
        raise ValueError("图表含未知属性或混用了两种数据来源。")
    for k in ("title", "xTitle", "yTitle"):
        v = spec.get(k, "")
        if not isinstance(v, str) or len(v) > 80 or any(ord(c) < 32 for c in v):
            raise ValueError("图表标题应为 80 字以内的单行文字。")
    color = spec.get("color", COLOR)
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError("请选择六位十六进制图表颜色。")
    if spec.get("style", "line") not in ("line", "scatter") or type(spec.get("labels", False)) is not bool:
        raise ValueError("图表样式或数据标签开关无效。")
    return spec


def chart_scene(field, spec, fields, computed):
    sheet = int(CHART_FIELD.fullmatch(field)[1])
    prefix = "" if sheet == 1 else f"T{sheet}."
    points = []
    if "expression" in spec:
        fn = expression(spec["expression"])
        lo, hi = spec.get("min", -5), spec.get("max", 5)
        for i in range(49):
            fraction = i / 48
            x = (1 - fraction) * lo + fraction * hi
            try:
                y = fn(x)
            except (ValueError, ArithmeticError):
                y = None
            points.append({"x": x, "y": y})
    else:
        for a, b in zip(cells(spec["xRange"]), cells(spec["yRange"])):
            point = {}
            for axis, key in (("x", prefix + a), ("y", prefix + b)):
                raw = fields.get(key, "")
                point[axis + "Cell"] = key
                if raw.startswith("="):
                    point[axis + "Formula"] = raw
                try:
                    number = float(computed.get(key, raw))
                    point[axis] = number if math.isfinite(number) else None
                except ValueError:
                    point[axis] = None
            points.append(point)

    def bounds(axis):
        if axis == "x" and "expression" in spec:
            return spec.get("min", -5), spec.get("max", 5)
        values = [p[axis] for p in points if p["x"] is not None and p["y"] is not None]
        if not values:
            return 0, 1
        low, high = min(values), max(values)
        if low == high:
            # Scale the axis without overflowing for finite IEEE-754 endpoints.
            pad = max(1, abs(low) * .05)
            return (low - pad if math.isfinite(low - pad) else low,
                    high + pad if math.isfinite(high + pad) else high)
        return low, high

    xmin, xmax = bounds("x")
    ymin, ymax = bounds("y")

    def position(v, low, high):
        if math.isfinite(high - low):
            return (v - low) / (high - low)
        return (v / 2 - low / 2) / (high / 2 - low / 2)

    texts = []
    def label(key, value, x, y, align="middle", rotation=0):
        if value:
            item = dict(id=key, text=value, cx=x, cy=y, align=align)
            if rotation:
                item["rotation"] = rotation
            texts.append(item)

    label("title", spec.get("title"), WIDTH / 2, 23)
    label("expression", "y = " + spec["expression"] if "expression" in spec else "", WIDTH / 2, 43)
    label("xTitle", spec.get("xTitle"), (LEFT + RIGHT) / 2, HEIGHT - 8)
    label("yTitle", spec.get("yTitle"), 15, (TOP + BOTTOM) / 2, rotation=-90)
    for axis, low, high in (("x", xmin, xmax), ("y", ymin, ymax)):
        for i in range(3):
            t = i / 2
            value = format((1 - t) * low + t * high, ".5g")
            if axis == "x":
                label("xt" + str(i), value, LEFT + (RIGHT - LEFT) * t, BOTTOM + 18)
            else:
                label("yt" + str(i), value, LEFT - 7, BOTTOM - (BOTTOM - TOP) * t + 4, "end")
    for i, p in enumerate(points):
        p["id"] = "p" + str(i)
        if p["x"] is None or p["y"] is None:
            continue
        p["cx"] = round(LEFT + position(p["x"], xmin, xmax) * (RIGHT - LEFT), 3)
        p["cy"] = round(BOTTOM - position(p["y"], ymin, ymax) * (BOTTOM - TOP), 3)
        if spec.get("labels"):
            label("v" + str(i), format(p["y"], ".5g"), p["cx"], p["cy"] - 6)
    if not any("cx" in p for p in points):
        label("empty", "当前范围没有可绘制的数值对", (LEFT + RIGHT) / 2, (TOP + BOTTOM) / 2)
    return dict(field=field, spec=spec, viewBox=[0, 0, WIDTH, HEIGHT],
                plot=[LEFT, TOP, RIGHT, BOTTOM], texts=texts, points=points)
