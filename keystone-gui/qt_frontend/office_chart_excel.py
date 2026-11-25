"""Native Excel XY charts, including formula-backed samples for function charts."""
import ast
import json
import math
from posixpath import dirname, join, normpath

from .office_charts import COLOR, cells, parse_chart
from .math_tools import expression


def excel_expression(node, x):
    """Translate the grammar already checked by math_tools.expression."""
    if isinstance(node, ast.Name):
        return {"x": x, "e": "EXP(1)", "pi": "PI()", "tau": "(2*PI())"}[node.id]
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.UnaryOp):
        sign = "-" if isinstance(node.op, ast.USub) else "+"
        return "(" + sign + excel_expression(node.operand, x) + ")"
    if isinstance(node, ast.BinOp):
        a, b = (excel_expression(n, x) for n in (node.left, node.right))
        if isinstance(node.op, ast.Mod):
            return f"MOD({a},{b})"
        op = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Pow: "^"}[type(node.op)]
        return f"({a}{op}{b})"
    fn, arg = node.func.id, excel_expression(node.args[0], x)
    if fn == "ceil":
        return f"(-INT(-({arg})))"
    if fn == "round":
        # Excel ROUND rounds ties away from zero. Use the nearest even on exact ties.
        # Limit expansion for deeply nested expressions to Excel's formula size.
        if len(arg) > 1900:
            raise ValueError("公式超出 Excel 长度。")
        return f"IF(MOD({arg},1)=0.5,2*ROUND(({arg})/2,0),ROUND({arg},0))"
    return {"floor": "INT", "log": "LN"}.get(fn, fn.upper()) + "(" + arg + ")"


def sample_formula(tree, ref):
    try:
        formula = excel_expression(tree, ref)
    except ValueError:
        return None
    if len(formula) <= 8100:
        return f"=IFERROR({formula},NA())"
    return None


def add_excel_chart(book, sheet, scene):
    from openpyxl.chart import Reference, ScatterChart, Series
    from openpyxl.chart.label import DataLabelList

    spec = scene["spec"]
    chart = ScatterChart()
    chart.width, chart.height = 17, 8
    if spec.get("title"):
        chart.title = spec["title"]
    if spec.get("xTitle"):
        chart.x_axis.title = spec["xTitle"]
    if spec.get("yTitle"):
        chart.y_axis.title = spec["yTitle"]
    chart.scatterStyle = "marker" if spec.get("style") == "scatter" else "lineMarker"
    if "expression" in spec:
        data = book.create_sheet("_KeystoneXY")
        data.sheet_state = "hidden"
        data.append(["x", "y", "Keystone XY/1"])
        data["C2"] = spec["expression"]
        data["C2"].data_type = "s"
        tree = ast.parse(spec["expression"].strip().replace("^", "**"), mode="eval").body
        for row, point in enumerate(scene["points"], 2):
            data.cell(row, 1, point["x"])
            formula = sample_formula(tree, "A" + str(row))
            data.cell(row, 2, formula if formula is not None else point["y"])
        x = Reference(data, min_col=1, min_row=2, max_row=data.max_row)
        y = Reference(data, min_col=2, min_row=2, max_row=data.max_row)
        series = Series(y, x, title="y = " + spec["expression"])
        chart.visible_cells_only = False
        chart.legend.position = "t"
    else:
        x = Reference(sheet, **_reference_args(spec["xRange"]))
        y = Reference(sheet, **_reference_args(spec["yRange"]))
        series = Series(y, x)
        chart.legend = None
    color = spec.get("color", COLOR)[1:]
    series.marker.symbol = "circle"
    series.marker.size = 5
    series.marker.graphicalProperties.solidFill = color
    series.marker.graphicalProperties.line.solidFill = color
    if chart.scatterStyle == "marker":
        series.graphicalProperties.line.noFill = True
    else:
        series.graphicalProperties.line.solidFill = color
    series.smooth = False
    chart.series.append(series)
    if spec.get("labels"):
        chart.dLbls = DataLabelList(showVal=True, showSerName=False, showCatName=False, showLegendKey=False)
    sheet.add_chart(chart, "D2")


def _reference_args(ref):
    from openpyxl.utils.cell import range_boundaries
    a, b, c, d = range_boundaries(ref)
    return dict(min_col=a, min_row=b, max_col=c, max_row=d)


NS = {"c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
      "a": "http://schemas.openxmlformats.org/drawingml/2006/main"}


def _related(pack, part, kind):
    from openpyxl.xml.functions import fromstring
    folder, name = dirname(part), part.rsplit("/", 1)[-1]
    try:
        rels = fromstring(pack.read(join(folder, "_rels", name + ".rels")))
    except KeyError:
        return
    for rel in rels:
        if rel.get("TargetMode") == "External" or not rel.get("Type", "").endswith("/" + kind):
            continue
        target = rel.get("Target", "")
        yield normpath(target.lstrip("/") if target.startswith("/") else join(folder, target))


def read_excel_chart(book, sheet, rows):
    """Inspect drawing parts only; retain streaming, bounded worksheet import."""
    from openpyxl.xml.functions import fromstring
    pack = book._archive
    for draw in _related(pack, sheet._worksheet_path, "drawing"):
        for part in _related(pack, draw, "chart"):
            try:
                root = fromstring(pack.read(part))
                result = _read_xy(root, sheet.title, rows, book)
                if result:
                    return result
            except (ValueError, KeyError, TypeError, SyntaxError, AttributeError):
                continue
    return None


def _read_xy(root, sheet_name, rows, book):
    chart = root.find("c:chart", NS)
    xy = chart.find("c:plotArea/c:scatterChart", NS)
    if xy is None or len(xy.findall("c:ser", NS)) != 1:
        return
    series = xy.find("c:ser", NS)

    def ref(axis):
        formula = series.findtext("c:" + axis + "Val/c:numRef/c:f", "", NS)
        name, _, region = formula.rpartition("!")
        if name.startswith("'") and name.endswith("'"):
            name = name[1:-1].replace("''", "'")
        region = region.replace("$", "")
        cells(region)
        return name, region

    xs, xr = ref("x")
    ys, yr = ref("y")
    if xs != ys:
        return
    helper = None
    if xs == sheet_name:
        spec = dict(xRange=xr, yRange=yr)
    elif (xs in rows and book[xs].sheet_state == "hidden" and rows[xs].get("C1") == "Keystone XY/1"
          and xr == "A2:A50" and yr == "B2:B50"):
        data = rows[xs]
        spec = dict(expression=data["C2"], min=float(data["A2"]), max=float(data["A50"]))
        # Hide only the generated sampling table. Keep any user's additions or edits
        # in the imported workbook as ordinary cells.
        expected = {"A1", "B1", "C1", "C2"} | set(cells(xr)) | set(cells(yr))
        parse_chart(json.dumps(spec))
        tree = ast.parse(spec["expression"].strip().replace("^", "**"), mode="eval").body
        fn = expression(spec["expression"])
        for i, xref in enumerate(cells(xr)):
            t = i / 48
            expected_x = (1 - t) * spec["min"] + t * spec["max"]
            if not math.isclose(float(data[xref]), expected_x, rel_tol=2e-14, abs_tol=1e-14):
                return
            formula, actual = sample_formula(tree, xref), data.get("B" + xref[1:])
            if formula is not None:
                if actual != formula:
                    return
            else:
                try:
                    expected_y = fn(expected_x)
                except (ValueError, ArithmeticError):
                    expected_y = None
                if expected_y is None:
                    if actual is not None:
                        return
                elif actual is None or not math.isclose(float(actual), expected_y, rel_tol=2e-14, abs_tol=1e-14):
                    return
        if data.keys() <= expected and data.get("A1") == "x" and data.get("B1") == "y":
            helper = xs
    else:
        return
    def title(element):
        return "".join(n.text or "" for n in element.findall("c:title//a:t", NS))

    spec["title"] = title(chart)
    for axis in chart.findall("c:plotArea/c:valAx", NS):
        ax_id = axis.find("c:axId", NS).get("val")
        axis_ids = [n.get("val") for n in xy.findall("c:axId", NS)]
        if ax_id in axis_ids:
            spec["xTitle" if axis_ids.index(ax_id) == 0 else "yTitle"] = title(axis)
    style = xy.find("c:scatterStyle", NS)
    spec["style"] = "scatter" if (style is not None and style.get("val") == "marker"
        or series.find("c:spPr/a:ln/a:noFill", NS) is not None) else "line"
    color = series.find("c:marker/c:spPr/a:solidFill/a:srgbClr", NS)
    if color is None:
        color = series.find("c:spPr/a:ln/a:solidFill/a:srgbClr", NS)
    spec["color"] = "#" + color.get("val") if color is not None else COLOR
    label = xy.find("c:dLbls/c:showVal", NS)
    spec["labels"] = label is not None and label.get("val") in ("1", "true")
    raw = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
    parse_chart(raw)
    return raw, helper
