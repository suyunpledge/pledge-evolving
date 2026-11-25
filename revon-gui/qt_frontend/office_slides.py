"""Shared slide geometry (960 × 540), deck order, and atomic page operations."""
import json
import re

SLIDE = re.compile(r"(S[1-9][0-9]{0,2})(?:\.(?:body|right|notes|layout|hidden|transition|table))?\Z")
THEMES = {
    "paper": {"background": "#FAF8F3", "ink": "#252D32", "accent": "#A2442D"},
    "midnight": {"background": "#17283A", "ink": "#F1F6FA", "accent": "#93D7DF"},
    "sage": {"background": "#EEF4EE", "ink": "#193D37", "accent": "#397A60"},
}
LAYOUTS = ("content", "cover", "columns")
DEFAULT_THEME = "paper"


def validate_design(field, value):
    if field.endswith(".table"):
        parse_table(value)
    elif field == "slideOrder":
        keys = value.split(",")
        if len(keys) > 999 or len(set(keys)) != len(keys) or any(
                not re.fullmatch(r"S[1-9][0-9]{0,2}", k) for k in keys):
            raise ValueError("页序应由不重复的页面编号组成，如 S2,S1。")
    elif field == "slideTheme" and value not in THEMES:
        raise ValueError("此演示主题不可用。")
    elif field.endswith(".layout") and value not in LAYOUTS:
        raise ValueError("请选择标题正文、封面或双栏版式。")
    elif field.endswith(".hidden") and value not in ("true", "false"):
        raise ValueError("隐藏状态应为 true 或 false。")
    elif field.endswith(".transition") and value not in ("none", "fade"):
        raise ValueError("请选择无切换或淡入。")


def parse_table(value):
    rows = json.loads(value)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 8 or not isinstance(rows[0], list):
        raise ValueError("演示表格支持 1 至 8 行。")
    width = len(rows[0])
    if not 1 <= width <= 6 or any(not isinstance(row, list) or len(row) != width for row in rows):
        raise ValueError("演示表格支持 1 至 6 列，每行列数需相同。")
    if any(not isinstance(v, str) or len(v) > 200 or any(ord(c) < 32 and c not in "\n\t" for c in v)
           for row in rows for v in row):
        raise ValueError("每个表格单元格支持 200 字以内的文本。")
    return rows


def slide_order(fields):
    known = {m[1] for k in fields if (m := SLIDE.fullmatch(k))}
    order = [k for k in fields.get("slideOrder", "").split(",") if k in known]
    return order + sorted(known.difference(order), key=lambda k: int(k[1:]))


def geometry(layout, second, table=False):
    # Each box is [left, top, width, height, font size], in design pixels.
    if layout == "cover":
        accent = [62, 102, 96, 8]
        title = [60, 140, 844, 152, 60]
        body = [64, 314, 832, 162, 28]
    else:
        accent = [54, 116, 64, 6]
        title = [52, 40, 856, 70, 42]
        body = [54, 158, 848, 334, 28]
    boxes = {"title": title, "body": body}
    if layout == "columns":
        boxes.update(body=[54, 158, 399, 334, 26], right=[511, 158, 397, 334, 26])
    elif second:
        x, y, w, h, size = body
        boxes["body"] = [x, y, w, h // 2 - 6, size]
        boxes["right"] = [x, y + h // 2 + 6, w, h // 2 - 6, size]
    if table:
        if layout == "cover":
            accent, boxes["title"] = [56, 40, 80, 6], [54, 60, 848, 72, 46]
        if "right" in boxes:
            boxes.update(body=[54, 145, 399, 68, 24], right=[511, 145, 397, 68, 24])
        else:
            boxes["body"] = [54, 145, 850, 68, 24]
        return {"accent": accent, "boxes": boxes, "table": [54, 225, 852, 265]}
    return {"accent": accent, "boxes": boxes}


DESIGN = {"themes": THEMES, "layouts": {
    name: [geometry(name, second, table) for table in (False, True) for second in (False, True)]
    for name in LAYOUTS}}


def arrange(d, args):
    fields = d["fields"]
    if d["kind"] != "presentation" or args.get("version") != d["version"]:
        raise ValueError("页面已变化，请重新打开演示后操作。")
    order = slide_order(fields)
    key, command = args.get("slide"), args.get("command")
    if key not in order and not (not order and command == "insert"):
        raise ValueError("未找到选中的幻灯片。")
    index = order.index(key) if order else -1
    values = {}
    if command in ("insert", "duplicate"):
        reserved = set(order)
        for entry in [*d["pending"], *d["history"]]:
            for edit in entry["changes"]:
                if m := SLIDE.fullmatch(edit["field"]):
                    reserved.add(m[1])
        new = next((f"S{i}" for i in range(1, 1000) if f"S{i}" not in reserved), None)
        if new is None:
            raise ValueError("当前演示已达到页面编号上限。")
        values[new] = ""
        if command == "duplicate":
            values.update({new + k[len(key):]: v for k, v in fields.items()
                           if k == key or k.startswith(key + ".")})
        order.insert(index + 1, new)
    elif command == "delete":
        if len(order) == 1:
            raise ValueError("请至少保留一张幻灯片。")
        if any(any(e["field"].split(".")[0] == key for e in p["changes"]) for p in d["pending"]):
            raise ValueError("请先处理该页的待审建议，再删除幻灯片。")
        order.remove(key)
        values.update({k: None for k in fields if k == key or k.startswith(key + ".")})
    elif command == "move":
        target = args.get("index")
        if type(target) is not int or not 0 <= target < len(order):
            raise ValueError("目标位置无效。")
        if target == index:
            return []
        order.pop(index)
        order.insert(target, key)
    else:
        raise ValueError("未知的页面操作。")
    values["slideOrder"] = ",".join(order)
    return [dict(field=k, before=fields.get(k), value=v) for k, v in values.items() if fields.get(k) != v]
