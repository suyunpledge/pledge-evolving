import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from openpyxl import load_workbook

from .office_charts import parse_chart
from .office_controller import OfficeController
from .office_formats import export_file, import_file
from .office_workspace import OfficeWorkspace


class OfficeChartTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = OfficeWorkspace(self.root)
        self.controller = OfficeController(lambda: self.root)

    def tool(self, name, **args):
        reply = self.controller.call("office_" + name, self.root, args, Event())
        self.assertTrue(reply["ok"], reply)
        return json.loads(reply["content"])

    def test_cell_formula_scene_review_and_undo(self):
        spec = dict(xRange="A2:A4", yRange="B2:B4", labels=True, title="销售额")
        doc = self.tool("create", kind="spreadsheet", title="销售预测", fields={
            "T2.A2": "1", "T2.A3": "2", "T2.A4": "3", "T2.B2": "0", "T2.B3": "=A3*2",
            "T2.B4": "8", "T2.chart": json.dumps(spec)})
        key = doc["id"]
        self.assertEqual(self.tool("read", id=key.split(".")[0])["id"], key)
        def read():
            return self.tool("read", id=key, chart="T2.chart")
        self.assertEqual(self.tool("read", id=key)["charts"], ["T2.chart"])
        initial = read()
        point = initial["points"][1]
        self.assertEqual((point["yCell"], point["yFormula"], point["y"]), ("T2.B3", "=A3*2", 4))
        self.assertIsNone(initial["viewport"])
        self.tool("apply", id=key, version=1, suggestions={"T2.A3": "3"})
        self.assertEqual(read()["points"], initial["points"])
        proposal = self.store.load(key)["pending"][0]
        self.store.change(key, "accept", {"id": proposal["id"]})
        new = read()
        self.assertLess(new["points"][1]["cy"], point["cy"])
        self.assertEqual(next(t["text"] for t in new["texts"] if t["id"] == "v1"), "6")
        self.store.change(key, "undo", {"author": "ai"})
        self.assertEqual(read()["points"], initial["points"])

    def test_function_paging_domain_gap_and_validation(self):
        bad = self.controller.call("office_read", self.root, {"id": "missing", "chart": "T1.chart"}, Event())
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["meta"]["execution_state"], "not_started")
        self.assertIn("office_list", bad["error"])
        ident = self.tool("create", kind="spreadsheet", title="倒数",
                          fields={"T1.chart": json.dumps(dict(expression="1/x", min=-1, max=1, labels=True))})["id"]
        page = self.tool("read", id=ident, chart="T1.chart", offset=24)
        self.assertEqual(page["point_count"], 49)
        self.assertEqual(page["points"][0], dict(id="p24", x=0, y=None))
        self.assertEqual(page["next_offset"], 36)
        self.assertEqual(next(t["text"] for t in page["texts"] if t["id"] == "expression"), "y = 1/x")
        self.assertFalse(any(t["id"] in ("v23", "v24", "v36") for t in page["texts"]))
        for spec in [dict(expression="x", min=1, max=1), dict(xRange="A2:A3", yRange="B2:B200"),
                     dict(expression="x", xRange="A1:A3"), dict(expression="x", color="blue")]:
            with self.assertRaises(ValueError):
                parse_chart(json.dumps(spec))

    def test_native_excel_roundtrip_preserves_sources_and_design(self):
        spec = dict(title="实验曲线", xTitle="时间", yTitle="位移", color="#138357", style="scatter", labels=True)
        fn = dict(spec, expression="sin(x)+x^2", min=-2, max=2)
        xy = dict(spec, xRange="A2:A3", yRange="B2:B3", style="line")
        values = {"T1.name": "O'Brien", "T2.name": "曲线", "A2": "1", "A3": "2", "B2": "=A2+1", "B3": "5",
                  "T1.chart": json.dumps(xy), "T2.chart": json.dumps(fn)}
        path = export_file(self.root, self.store.create("spreadsheet", "图表", values))
        wb = load_workbook(path)
        try:
            self.assertEqual([len(s._charts) for s in wb], [1, 1, 0])
            self.assertEqual(wb.worksheets[-1].sheet_state, "hidden")
            self.assertIn("SIN(A2)", wb.worksheets[-1]["B2"].value)
            self.assertIn("O''Brien", wb.worksheets[0]._charts[0].ser[0].xVal.numRef.f)
        finally:
            wb.close()
        kind, imported = import_file(path)
        self.assertEqual(kind, "spreadsheet")
        self.assertEqual(set(imported), set(values))
        for k, v in values.items():
            if k.endswith(".chart"):
                self.assertEqual(json.loads(imported[k]), json.loads(v))
            else:
                self.assertEqual(imported[k], v)

    def test_chart_numeric_strings_and_modified_excel_samples(self):
        values = {"A1": "1e-3", "A2": "2e-3", "B1": "1", "B2": "2", "C1": "001",
                  "T1.chart": json.dumps(dict(xRange="A1:A2", yRange="B1:B2")),
                  "T2.chart": json.dumps(dict(expression="x^2"))}
        path = export_file(self.root, self.store.create("spreadsheet", "样本", values))
        book = load_workbook(path)
        try:
            self.assertEqual(book.active["A1"].value, .001)
            self.assertEqual(book.active["C1"].value, "001")
            samples = book.worksheets[-1]
            samples["B3"] = 77
            samples["E5"] = "手动记录"
            book.save(path)
        finally:
            book.close()
        _, imported = import_file(path)
        self.assertEqual(imported["T3.B3"], "77")
        self.assertEqual(imported["T3.E5"], "手动记录")
        self.assertNotIn("T2.chart", imported)
