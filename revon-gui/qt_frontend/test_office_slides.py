import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from pptx import Presentation

from .office_controller import OfficeController
from .office_formats import export_file, import_file
from .office_workspace import OfficeWorkspace


class SlideTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = OfficeWorkspace(self.root)
        self.office = OfficeController(lambda: self.root)
        self.doc = self.store.create("presentation", "产品演示", {
            "S1": "第一部分", "S1.notes": "讲者备注", "S4": "取舍", "S4.body": "速度",
            "S4.right": "质量", "S4.layout": "columns", "S4.transition": "fade", "slideTheme": "midnight",
        })

    def change(self, action, **args):
        self.doc = self.store.change(self.doc["id"], action, args)
        return self.doc

    def arrange(self, command, slide, **args):
        return self.change("arrange", version=self.doc["version"], command=command, slide=slide, **args)

    def tool(self, key, **args):
        ans = self.office.call("office_" + key, self.root, dict(id=self.doc["id"], **args), Event())
        self.assertTrue(ans["ok"], ans)
        return json.loads(ans["content"])

    def test_stable_order_atomic_duplicate_and_reversible_delete(self):
        self.assertEqual(self.tool("read")["slideOrder"], ["S1", "S4"])
        self.arrange("move", "S4", index=0)
        self.arrange("duplicate", "S4")
        self.assertEqual(self.doc["fields"]["slideOrder"], "S4,S2,S1")
        self.assertEqual(self.doc["fields"]["S2.right"], "质量")
        self.assertEqual(self.doc["fields"]["S2.transition"], "fade")
        with self.assertRaises(ValueError):
            self.change("arrange", version=1, command="duplicate", slide="S4")
        self.arrange("delete", "S4")
        self.assertEqual(self.tool("read")["slideOrder"], ["S2", "S1"])
        self.change("undo", author="user")
        self.assertEqual(self.doc["fields"]["slideOrder"], "S4,S2,S1")
        self.assertEqual(self.doc["fields"]["S4.right"], "质量")

    def test_pending_review_after_move_and_undo_conflict(self):
        self.tool("apply", version=1, suggestions={"S1.notes": "新的讲稿"})
        self.doc = self.store.load(self.doc["id"])
        with self.assertRaises(ValueError):
            self.arrange("delete", "S1")
        self.arrange("move", "S1", index=1)
        self.change("accept", id=self.doc["pending"][0]["id"])
        self.assertEqual(self.doc["fields"]["S1.notes"], "新的讲稿")
        self.assertEqual(self.tool("read")["slideOrder"][1], "S1")
        self.arrange("insert", "S4")
        inserted = self.doc["version"]
        self.change("apply", version=inserted, changes=[dict(field="S2.notes", before=None, value="正在撰写")])
        with self.assertRaises(ValueError):
            self.change("undo", author="user", version=inserted)

    def test_pptx_roundtrip_in_display_order_with_private_notes(self):
        self.arrange("move", "S4", index=0)
        self.change("apply", version=self.doc["version"], changes=[dict(field="S1.hidden", before=None, value="true")])
        path = export_file(self.root, self.doc)
        deck = Presentation(path)
        self.assertEqual([s.shapes.title.text for s in deck.slides], ["取舍", "第一部分"])
        self.assertEqual(deck.slides[1].notes_slide.notes_text_frame.text, "讲者备注")
        self.assertNotIn("讲者备注", [s.text for s in deck.slides[1].shapes if s.has_text_frame])
        first = {s.name: s for s in deck.slides[0].shapes}
        self.assertLess(first["revon.body"].left, first["revon.right"].left)
        kind, new = import_file(path)
        self.assertEqual(kind, "presentation")
        self.assertEqual((new["S1.layout"], new["S1.right"], new["S1.transition"]), ("columns", "质量", "fade"))
        self.assertEqual((new["slideTheme"], new["S2.hidden"], new["S2.notes"]), ("midnight", "true", "讲者备注"))
        for slide in deck.slides:
            for shape in slide.shapes:
                if shape.name.startswith("revon."):
                    shape.name = "keystone." + shape.name.removeprefix("revon.")
        legacy = path.with_name("legacy.pptx")
        deck.save(legacy)
        self.assertEqual(import_file(legacy), (kind, new))

    def test_external_powerpoint_and_invalid_design_values(self):
        deck = Presentation()
        cover = deck.slides.add_slide(deck.slide_layouts[0])
        cover.shapes.title.text = "欢迎"
        cover.placeholders[1].text = "第一部分"
        cover.notes_slide.notes_text_frame.text = "内部备注"
        cols = deck.slides.add_slide(deck.slide_layouts[3])
        cols.shapes.title.text = "对比"
        cols.placeholders[1].text, cols.placeholders[2].text = "A", "B"
        path = self.root / "external.pptx"
        deck.save(path)
        _, fields = import_file(path)
        self.assertEqual([fields[k] for k in ("S1.layout", "S1.notes", "S2.layout", "S2.right")],
                         ["cover", "内部备注", "columns", "B"])
        for key, value in [("slideOrder", "S1,S1"), ("S4.hidden", "no"), ("slideTheme", "red"),
                           ("S4.layout", "unknown"), ("S4.transition", "random")]:
            with self.assertRaises(ValueError):
                self.change("apply", version=1, changes=[dict(field=key, before=None, value=value)])

    def test_table_review_and_native_cell_roundtrip(self):
        raw = json.dumps([["阶段", "责任人"], ["交付", "主讲人"]], ensure_ascii=False)
        self.tool("apply", version=1, suggestions={"S4.table": raw})
        self.doc = self.store.load(self.doc["id"])
        self.change("accept", id=self.doc["pending"][0]["id"])
        saved = export_file(self.root, self.doc)
        table = next(s.table for s in Presentation(saved).slides[1].shapes if s.has_table)
        self.assertEqual(table.cell(1, 1).text, "主讲人")
        self.assertEqual(json.loads(import_file(saved)[1]["S2.table"]), json.loads(raw))
        self.change("undo", author="ai")
        self.assertNotIn("S4.table", self.doc["fields"])
        with self.assertRaises(ValueError):
            self.change("apply", version=self.doc["version"],
                        changes=[dict(field="S4.table", before=None, value='[["a"],["b","c"]]')])
