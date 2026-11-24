"""Bounded Office interchange. Imported files stay intact; exports are new files."""

import ast
import csv
import io
import math
import operator
from pathlib import Path
import re
import uuid
from zipfile import ZipFile

from .local_tool_runtime import workspace_path

FORMATS = {"document": ".docx", "spreadsheet": ".xlsx", "presentation": ".pptx"}
OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
CELL = re.compile(r"([A-Z]{1,2})([1-9][0-9]{0,3})\Z")


def col_num(name):
    value = 0
    for c in name:
        value = value * 26 + ord(c) - 64
    return value


def col_name(number):
    out = ""
    while number:
        number, rem = divmod(number - 1, 26)
        out = chr(65 + rem) + out
    return out


def calculated(fields):
    cache = {}
    budget = [80000]

    def value(key, visiting):
        if key in cache:
            return cache[key]
        raw = fields.get(key, "")
        if not raw.startswith("="):
            return raw
        if key in visiting or len(visiting) >= 40 or len(raw) > 512:
            raise ValueError
        prefix = key.rpartition(".")[0]
        prefix = prefix + "." if prefix else ""

        def referenced(ref):
            budget[0] -= 1
            if budget[0] < 0:
                raise ValueError
            return value(prefix + ref, visiting | {key})

        def aggregate(match):
            start, end = CELL.fullmatch(match[2]), CELL.fullmatch(match[3])
            left, right = col_num(start[1]), col_num(end[1])
            top, bottom = int(start[2]), int(end[2])
            if (
                left > right
                or top > bottom
                or (right - left + 1) * (bottom - top + 1) > 5000
            ):
                raise ValueError
            values = []
            for row in range(top, bottom + 1):
                for col in range(left, right + 1):
                    cell_value = referenced(col_name(col) + str(row))
                    if cell_value == "#ERROR!":
                        raise ValueError
                    try:
                        values.append(float(cell_value))
                    except ValueError:
                        continue
            fn = {
                "SUM": sum,
                "MIN": min,
                "MAX": max,
                "AVERAGE": lambda a: sum(a) / len(a),
                "COUNT": len,
            }[match[1]]
            if not values and match[1] == "AVERAGE":
                raise ValueError
            return str(fn(values) if values else 0)

        expression = re.sub(
            r"(SUM|AVERAGE|MIN|MAX|COUNT)\(([A-Z]{1,2}[1-9]\d{0,3}):([A-Z]{1,2}[1-9]\d{0,3})\)",
            aggregate,
            raw[1:].strip().upper(),
        )
        if not re.fullmatch(r"[A-Z0-9.+*/() \t-]+", expression):
            raise ValueError
        tree = ast.parse(expression, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 120:
            raise ValueError

        def evaluate(node):
            if isinstance(node, ast.Constant) and type(node.value) in (int, float):
                return float(node.value)
            if isinstance(node, ast.Name) and CELL.fullmatch(node.id):
                return float(referenced(node.id) or 0)
            if isinstance(node, ast.BinOp) and type(node.op) in OPS:
                return OPS[type(node.op)](evaluate(node.left), evaluate(node.right))
            if isinstance(node, ast.UnaryOp) and isinstance(
                node.op, (ast.UAdd, ast.USub)
            ):
                return evaluate(node.operand) * (
                    -1 if isinstance(node.op, ast.USub) else 1
                )
            raise ValueError

        result = evaluate(tree.body)
        if not math.isfinite(result):
            raise ValueError
        cache[key] = format(result, ".15g")
        return cache[key]

    for key, raw in fields.items():
        if CELL.fullmatch(key.rsplit(".", 1)[-1]) and raw.startswith("="):
            try:
                value(key, set())
            except (
                ValueError,
                SyntaxError,
                ZeroDivisionError,
                OverflowError,
                RecursionError,
            ):
                cache[key] = "#ERROR!"
    return cache


def indices(fields, prefix):
    return sorted(
        {
            int(m[1])
            for key in fields
            if (m := re.fullmatch(prefix + r"([1-9]\d*)(?:\..+)?", key))
        }
    )


def import_file(path):
    path = Path(path)
    ext = path.suffix.lower()
    if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("请选择 20 MB 以内的办公文件。")
    if ext in FORMATS.values():
        with ZipFile(path) as pack:
            entries = pack.infolist()
            if (
                len(entries) > 3500
                or sum(e.file_size for e in entries) > 70 * 1024 * 1024
            ):
                raise ValueError("文件展开后超出导入容量。")
    fields = {}
    if ext in (".docx", ".md", ".txt"):
        if ext == ".docx":
            from docx import Document

            for i, p in enumerate(Document(path).paragraphs, 1):
                fields["P" + str(i)] = p.text
                style = p.style.name if p.style else ""
                if style.startswith(("Heading", "Title", "List Bullet")):
                    fields[f"P{i}.style"] = "bullet" if "Bullet" in style else "heading"
        else:
            fields = {
                f"P{i}": s
                for i, s in enumerate(
                    path.read_text(encoding="utf-8-sig").splitlines(), 1
                )
            }
        return "document", fields
    if ext in (".xlsx", ".csv"):
        if ext == ".xlsx":
            import openpyxl

            book = openpyxl.load_workbook(
                path, read_only=True, data_only=False, keep_links=False
            )
            try:
                for i, sheet in enumerate(book, 1):
                    if i > 32 or (sheet.max_row or 0) * (sheet.max_column or 0) > 80000:
                        raise ValueError("工作表超出导入容量。")
                    fields[f"T{i}.name"] = sheet.title
                    for n, row in enumerate(sheet.values, 1):
                        for col, v in enumerate(row, 1):
                            if v is not None:
                                fields[
                                    ("" if i == 1 else f"T{i}.")
                                    + col_name(col)
                                    + str(n)
                                ] = str(v)
                        if len(fields) > 20000:
                            raise ValueError("表格超出导入容量。")
            finally:
                book.close()
        else:
            with path.open(encoding="utf-8-sig", newline="") as stream:
                for r, row in enumerate(csv.reader(stream), 1):
                    for c, v in enumerate(row, 1):
                        if v:
                            fields[col_name(c) + str(r)] = v
                    if len(fields) > 20000:
                        raise ValueError("表格超出导入容量。")
        return "spreadsheet", fields
    if ext == ".pptx":
        from pptx import Presentation

        for i, slide in enumerate(Presentation(path).slides, 1):
            title = slide.shapes.title
            fields[f"S{i}"] = title.text if title is not None else ""
            fields[f"S{i}.body"] = "\n".join(
                s.text
                for s in slide.shapes
                if s.has_text_frame and (title is None or s.shape_id != title.shape_id)
            )
        return "presentation", fields
    raise ValueError("支持 docx、xlsx、pptx、md、txt 和 csv。")


def export_file(workspace, d):
    f, kind = d["fields"], d["kind"]
    out = io.BytesIO()
    if kind == "document":
        from docx import Document

        book = Document()
        for i in indices(f, "P"):
            key = f"P{i}"
            style = {"heading": "Heading 1", "bullet": "List Bullet"}.get(
                f.get(key + ".style"), "Normal"
            )
            book.add_paragraph(f.get(key, ""), style)
    elif kind == "presentation":
        from pptx import Presentation
        from pptx.util import Inches

        book = Presentation()
        book.slide_width, book.slide_height = Inches(13.3333), Inches(7.5)
        for i in indices(f, "S") or [1]:
            key = f"S{i}"
            slide = book.slides.add_slide(book.slide_layouts[1])
            slide.shapes.title.text = f.get(key, "")
            slide.placeholders[1].text = f.get(key + ".body", "")
    else:
        import openpyxl

        book = openpyxl.Workbook()
        sheets = {}
        for i in sorted(set(indices(f, "T")) | {1}):
            sheet = book.active if i == 1 else book.create_sheet()
            sheet.title = f.get(f"T{i}.name") or f"Sheet{i}"
            sheets[i] = sheet
        formulas = calculated(f)
        for key, raw in f.items():
            sheet_id, _, ref = key.rpartition(".")
            if not CELL.fullmatch(ref):
                continue
            cell = sheets[int(sheet_id[1:]) if sheet_id else 1][ref]
            if raw.startswith("=") and formulas.get(key) != "#ERROR!":
                cell.value = raw
            elif re.fullmatch(r"-?(?:0|[1-9]\d{0,13})(?:\.\d{1,12})?", raw):
                cell.value = float(raw) if "." in raw else int(raw)
            else:
                cell.value = raw
                cell.data_type = "s"
    book.save(out)
    stem = re.sub(r"[^\w -]+", "_", f["title"]).strip()[:64] or "Office"
    filename = stem + "-" + uuid.uuid4().hex[:10] + FORMATS[kind]
    dest = workspace_path(workspace, str(Path("Office") / "Exports" / filename))
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("xb") as stream:
        try:
            stream.write(out.getbuffer())
            stream.flush()
        except OSError:
            stream.close()
            dest.unlink(missing_ok=True)
            raise
    return dest
