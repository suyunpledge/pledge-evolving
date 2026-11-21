"""model3d: geometry building, booleans, export validity, validation messages, tools, skill."""
from __future__ import annotations

import json
import math
import struct
import tempfile
import unittest
from pathlib import Path

from keystone.model3d import (Mesh, SpecError, analyze, ascii_view, compile_spec, contact_sheet, decode,
                              encode, summary_lines)
from keystone.model3d.mesh import box
from keystone.policy import Mode, Policy, Sandbox
from keystone.tools import BUILTIN_TOOL_NAMES, ToolContext, build_builtin_registry

PLATE = {"units": "mm", "model": {"op": "difference", "children": [
    {"op": "box", "size": [40, 40, 10]},
    {"op": "cylinder", "d": 20, "h": 12, "translate": [20, 20, -1]}]}}


def build(spec, **kw):
    mesh, meta = compile_spec(spec)
    return mesh, analyze(mesh, units=meta["units"], **kw)


class GeometryTests(unittest.TestCase):
    def assertSolid(self, report, holes=None):
        self.assertTrue(report["watertight"], summary_lines(report))
        self.assertEqual(report["orientation_conflicts"], 0)
        self.assertEqual(report["degenerate_faces"], 0)
        self.assertFalse(report["inverted"])
        self.assertEqual(report["shells"], 1)
        if holes is not None:
            self.assertEqual(report["through_holes"], holes)

    def test_box_is_exact(self):
        _, r = build({"op": "box", "size": [40, 30, 10]})
        self.assertSolid(r, 0)
        self.assertEqual(r["bbox"]["size"], [40.0, 30.0, 10.0])
        self.assertAlmostEqual(r["volume"], 12000.0, places=6)
        _, r = build({"op": "box", "size": 10, "center": True})
        self.assertEqual(r["bbox"]["min"], [-5.0, -5.0, -5.0])

    def test_cylinder_volume_matches_polygon_prism(self):
        _, r = build({"op": "cylinder", "r": 10, "h": 5, "segments": 32})
        self.assertSolid(r, 0)
        expected = 0.5 * 32 * 100 * math.sin(2 * math.pi / 32) * 5
        self.assertAlmostEqual(r["volume"], expected, places=3)

    def test_cone_sphere_torus_are_closed(self):
        for spec, holes in (({"op": "cylinder", "r1": 10, "r2": 0, "h": 10}, 0),
                            ({"op": "cone", "d1": 10, "d2": 20, "h": 10}, 0),
                            ({"op": "sphere", "r": 8}, 0),
                            ({"op": "torus", "major": 20, "minor": 5}, 1)):
            _, r = build(spec)
            self.assertSolid(r, holes)

    def test_plate_with_through_hole(self):
        mesh, r = build(PLATE, expect={"size": [40, 40, 10], "through_holes": 1})
        self.assertSolid(r, 1)
        self.assertTrue(r["passed"])
        self.assertTrue(all(row["ok"] for row in r["expect"]))
        n = 42  # auto facets for r=10
        hole = 0.5 * n * 100 * math.sin(2 * math.pi / n) * 10
        self.assertAlmostEqual(r["volume"], 16000 - hole, places=2)

    def test_coplanar_cutter_still_makes_a_clean_hole(self):
        spec = {"op": "difference", "children": [{"op": "box", "size": [40, 40, 10]},
                                                 {"op": "cylinder", "d": 20, "h": 10, "translate": [20, 20, 0]}]}
        _, r = build(spec)
        self.assertSolid(r, 1)

    def test_union_and_intersection_volumes(self):
        a = {"op": "box", "size": 10}
        b = {"op": "box", "size": 10, "translate": [5, 5, 0]}
        _, r = build({"op": "union", "children": [a, b]})
        self.assertSolid(r, 0)
        self.assertAlmostEqual(r["volume"], 1750.0, places=6)
        _, r = build({"op": "intersection", "children": [a, b]})
        self.assertSolid(r, 0)
        self.assertAlmostEqual(r["volume"], 250.0, places=6)
        _, r = build({"op": "difference", "children": [a, b]})
        self.assertSolid(r, 0)
        self.assertAlmostEqual(r["volume"], 750.0, places=6)

    def test_curved_booleans_have_no_seams(self):
        _, r = build({"op": "union", "children": [{"op": "sphere", "r": 10, "segments": 24},
                                                  {"op": "sphere", "r": 10, "segments": 24,
                                                   "translate": [12, 0, 0]}]})
        self.assertSolid(r, 0)
        _, r = build({"op": "difference", "children": [{"op": "box", "size": 20, "center": True},
                                                       {"op": "cylinder", "d": 8, "h": 30, "center": True,
                                                        "translate": [-5, 0, 0]},
                                                       {"op": "cylinder", "d": 8, "h": 30, "center": True,
                                                        "rotate": [90, 0, 0], "translate": [5, 0, 0]}]})
        self.assertSolid(r, 2)
        # crossing bores meet inside: four openings around one void = genus 3
        _, r = build({"op": "difference", "children": [{"op": "box", "size": 20, "center": True},
                                                       {"op": "cylinder", "d": 8, "h": 30, "center": True},
                                                       {"op": "cylinder", "d": 8, "h": 30, "center": True,
                                                        "rotate": [90, 0, 0]}]})
        self.assertSolid(r, 3)

    def test_gear_profile_extrude_with_bore(self):
        mesh, meta = compile_spec({"op": "extrude", "height": 8, "profile": {
            "shape": "gear", "teeth": 12, "module": 2, "holes": [{"shape": "circle", "d": 6}]}})
        r = analyze(mesh, expect={"through_holes": 1})
        self.assertSolid(r, 1)
        gear = meta["gears"][0]
        self.assertEqual(gear["pitch_diameter"], 24.0)
        self.assertEqual(gear["outer_diameter"], 28.0)
        self.assertAlmostEqual(r["bbox"]["size"][0], 28.0, delta=0.1)
        # 12 teeth: the outline crosses the addendum circle 12 times
        top = ascii_view(mesh, "top", 48)
        self.assertTrue(any(" " in row.strip() for row in top))

    def test_revolve_full_and_partial(self):
        cup = {"op": "revolve", "profile": {"shape": "polygon",
                                            "points": [[0, 0], [30, 0], [30, 40], [27, 40], [27, 3], [0, 3]]}}
        _, r = build(cup)
        self.assertSolid(r, 0)
        self.assertAlmostEqual(r["bbox"]["size"][2], 40.0)
        _, r = build({"op": "revolve", "angle": 90,
                      "profile": {"shape": "rect", "size": [10, 5], "translate": [10, 0]}})
        self.assertSolid(r, 0)

    def test_arrays_and_mirror_copy(self):
        leg = {"op": "box", "size": [4, 4, 41], "translate": [3, 3, 0]}
        chair = {"op": "union", "children": [
            {"op": "box", "size": [40, 40, 4], "translate": [0, 0, 40], "name": "seat"},
            {"op": "mirror_copy", "normal": [0, 1, 0],
             "child": {"op": "mirror_copy", "normal": [1, 0, 0],
                       "child": {**leg, "translate": [12, 12, 0]}}, "translate": [20, 20, 0]}]}
        mesh, meta = compile_spec(chair)
        r = analyze(mesh)
        self.assertSolid(r, 0)
        self.assertEqual(r["bbox"]["size"], [40.0, 40.0, 44.0])
        self.assertEqual(meta["parts"][0]["name"], "seat")
        _, r = build({"op": "polar_array", "count": 6,
                      "child": {"op": "cylinder", "d": 4, "h": 2, "translate": [10, 0, 0]}})
        self.assertEqual(r["shells"], 6)
        _, r = build({"op": "linear_array", "count": 3, "step": [5, 0, 0],
                      "child": {"op": "box", "size": [6, 2, 2]}})
        self.assertSolid(r, 0)
        self.assertEqual(r["bbox"]["size"][0], 16.0)


class SpecErrorTests(unittest.TestCase):
    def error(self, spec) -> str:
        with self.assertRaises(SpecError) as caught:
            compile_spec(spec)
        return str(caught.exception)

    def test_messages_name_the_node_and_the_fix(self):
        msg = self.error({"model": {"op": "union", "children": [{"op": "box", "size": 1},
                                                                {"op": "cylnder", "d": 3}]}})
        self.assertIn("model.children[1].op", msg)
        self.assertIn("'cylinder'", msg)
        msg = self.error({"op": "cylinder", "d": 3})
        self.assertIn("needs h", msg)
        self.assertIn("example:", msg)
        msg = self.error({"op": "box", "size": [1, 2, 3], "radius": 2})
        self.assertIn("unknown key(s) ['radius']", msg)
        self.assertIn("allowed:", msg)
        self.assertIn("give either r or d", self.error({"op": "sphere", "r": 1, "d": 2}))
        self.assertIn("not valid JSON", self.error('{"op": "box",'))
        self.assertIn("units must be", self.error({"units": "furlong", "model": {"op": "box", "size": 1}}))
        self.assertIn("edges cross", self.error({"op": "extrude", "height": 1, "profile": {
            "shape": "polygon", "points": [[0, 0], [10, 10], [10, 0], [0, 10]]}}))
        self.assertIn("r >= 0", self.error({"op": "revolve", "profile": {
            "shape": "polygon", "points": [[-1, 0], [5, 0], [5, 5]]}}))

    def test_json_string_spec_is_accepted(self):
        mesh, meta = compile_spec(json.dumps(PLATE))
        self.assertEqual(meta["units"], "mm")
        self.assertTrue(mesh.triangles)


class ValidationTests(unittest.TestCase):
    def test_open_mesh_reports_edges_and_hint(self):
        m = box(10, 10, 10)
        m.triangles.pop()
        r = analyze(m)
        self.assertFalse(r["watertight"])
        self.assertEqual(r["boundary_edges"], 3)
        self.assertFalse(r["passed"])
        self.assertTrue(any("open edge" in p for p in r["problems"]))
        self.assertTrue(any("cutters stick out" in h for h in r["hints"]))

    def test_flipped_face_and_inverted_mesh(self):
        m = box(10, 10, 10)
        a, b, c = m.triangles[0]
        m.triangles[0] = (a, c, b)
        r = analyze(m)
        self.assertGreater(r["orientation_conflicts"], 0)
        self.assertTrue(any("counter-clockwise" in h for h in r["hints"]))
        r = analyze(box(10, 10, 10).flipped())
        self.assertTrue(r["inverted"])
        self.assertIn("normals point inward (negative volume)", r["problems"])

    def test_overlapping_shells_and_expect_failures(self):
        two = box(10, 10, 10).extend(box(10, 10, 10).transformed(
            __import__("keystone.model3d.mesh", fromlist=["Matrix"]).Matrix.translate(5, 0, 0)))
        r = analyze(two, expect={"size": [10, 10, 10], "shells": 1})
        self.assertEqual(r["overlapping_shell_pairs"], 1)
        self.assertTrue(any("union" in h for h in r["hints"]))
        failed = [row for row in r["expect"] if not row["ok"]]
        self.assertEqual({row["check"] for row in failed}, {"size x", "shells"})
        text = "\n".join(summary_lines(r))
        self.assertIn("expect size x: want 10, got 15.0 [FAIL]", text)
        self.assertIn("checks: FAIL", text)

    def test_print_hints(self):
        _, r = build({"op": "sphere", "r": 5})
        self.assertFalse(r["print"]["rests_on_z0"])
        self.assertTrue(any("rests on the bed" in h for h in r["hints"]))
        _, r = build({"op": "box", "size": [0.04, 0.02, 0.01]})
        self.assertTrue(any("check the units" in h for h in r["hints"]))
        _, r = build({"units": "m", "model": {"op": "box", "size": [2, 1, 0.75]}})
        self.assertEqual(r["print"]["size_mm"], [2000.0, 1000.0, 750.0])
        _, r = build({"op": "sphere", "r": 5}, purpose="visual")
        self.assertNotIn("print", r)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.mesh, _ = compile_spec(PLATE)
        self.n = len(self.mesh.triangles)

    def test_binary_stl_layout_and_roundtrip(self):
        data = encode(self.mesh, "stl")
        self.assertEqual(len(data), 84 + 50 * self.n)
        self.assertEqual(struct.unpack_from("<I", data, 80)[0], self.n)
        back = decode(data, "stl")
        self.assertEqual(len(back.triangles), self.n)
        self.assertTrue(analyze(back)["watertight"])
        # first facet normal is unit length
        nx, ny, nz = struct.unpack_from("<3f", data, 84)
        self.assertAlmostEqual(nx * nx + ny * ny + nz * nz, 1.0, places=5)

    def test_ascii_stl_obj_roundtrip_and_unit_conversion(self):
        text = encode(self.mesh, "stl", ascii_stl=True, name="plate 1").decode("ascii")
        self.assertTrue(text.startswith("solid plate_1"))
        self.assertEqual(text.count("facet normal"), self.n)
        self.assertEqual(len(decode(text.encode(), "stl").triangles), self.n)
        obj = encode(self.mesh, "obj", mm_per_unit=10.0)
        back = decode(obj, "obj")
        self.assertEqual(analyze(back)["bbox"]["size"], [400.0, 400.0, 100.0])

    def test_glb_and_gltf_are_valid_gltf2(self):
        data = encode(self.mesh, "glb")
        magic, version, total = struct.unpack_from("<III", data, 0)
        self.assertEqual((magic, version, total), (0x46546C67, 2, len(data)))
        jlen, jtype = struct.unpack_from("<II", data, 12)
        self.assertEqual(jtype, 0x4E4F534A)
        self.assertEqual(jlen % 4, 0)
        doc = json.loads(data[20:20 + jlen])
        self.assertEqual(doc["asset"]["version"], "2.0")
        pos = doc["accessors"][0]
        self.assertIn("min", pos)
        self.assertAlmostEqual(pos["max"][0] - pos["min"][0], 0.040, places=6)  # metres
        self.assertAlmostEqual(pos["max"][1] - pos["min"][1], 0.010, places=6)  # Z up -> Y up
        back = decode(data, "glb")
        self.assertEqual(analyze(back)["bbox"]["size"], [40.0, 40.0, 10.0])
        doc = json.loads(encode(self.mesh, "gltf"))
        self.assertTrue(doc["buffers"][0]["uri"].startswith("data:application/octet-stream;base64,"))
        self.assertEqual(len(decode(json.dumps(doc).encode(), "gltf").triangles), self.n)

    def test_preview_png_and_ascii_hole(self):
        png = contact_sheet(self.mesh, 96)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        w, h = struct.unpack(">II", png[16:24])
        self.assertEqual((w, h), (195, 195))
        rows = ascii_view(self.mesh, "top", 40)
        mid = rows[len(rows) // 2]
        self.assertEqual(mid[18:22], "    ")       # the hole is see-through
        self.assertEqual(rows[0][:4], "@@@@")      # the plate is solid at the edge


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name).resolve()
        self.reg = build_builtin_registry()
        self.reg.activate("model3d_inspect")

    def ctx(self, mode=Mode.ACCEPT_EDITS):
        policy = Policy(mode=mode, sandbox=Sandbox.WORKSPACE_WRITE, workspace=self.ws, non_interactive=True)
        return ToolContext(policy, self.ws)

    def test_registered_as_builtin_write_and_read_tools(self):
        self.assertTrue({"model3d_build", "model3d_inspect"} <= BUILTIN_TOOL_NAMES)
        self.assertTrue(self.reg.writes("model3d_build", {"path": "a.stl"}))
        self.assertFalse(self.reg.writes("model3d_inspect", {"path": "a.stl"}))
        self.assertIn("model3d_build", self.reg.names())
        # inspect is deferred (keeps the default tool surface small); tool_search surfaces it
        fresh = build_builtin_registry()
        self.assertNotIn("model3d_inspect", fresh.names())
        self.assertIn("model3d_inspect", [s.name for s in fresh.search("inspect stl")])
        self.assertIn("model3d_inspect", fresh.names())

    def test_build_writes_files_with_checks_and_views(self):
        result = self.reg.invoke("model3d_build", {
            "spec": PLATE, "path": "models/plate.stl", "formats": ["obj", "glb"],
            "expect": {"size": [40, 40, 10], "through_holes": 1}}, self.ctx())
        self.assertTrue(result.ok, result.error)
        for name in ("plate.stl", "plate.obj", "plate.glb", "plate.preview.png"):
            self.assertTrue((self.ws / "models" / name).is_file(), name)
        self.assertTrue(result.meta["checks_passed"])
        self.assertTrue(all(f["reread_ok"] for f in result.meta["files"]))
        self.assertIn("checks: PASS", result.content)
        self.assertIn("expect through_holes: want 1, got 1 [ok]", result.content)
        self.assertIn("top view", result.content)
        inspect = self.reg.invoke("model3d_inspect", {"path": "models/plate.obj",
                                                      "expect": {"through_holes": 1}}, self.ctx())
        self.assertTrue(inspect.ok, inspect.error)
        self.assertIn("checks: PASS", inspect.content)

    def test_failed_expectation_is_flagged_at_the_top(self):
        result = self.reg.invoke("model3d_build", {"spec": PLATE, "path": "p.stl", "preview": False,
                                                   "expect": {"size": [40, 40, 12]}}, self.ctx())
        self.assertTrue(result.ok)
        self.assertFalse(result.meta["checks_passed"])
        self.assertTrue(result.content.startswith("CHECKS FAILED"))
        self.assertIn("expect size z: want 12, got 10.0 [FAIL]", result.content)
        self.assertNotIn("top view", result.content)

    def test_spec_error_writes_nothing(self):
        result = self.reg.invoke("model3d_build", {"spec": {"op": "box"}, "path": "bad.stl"}, self.ctx())
        self.assertFalse(result.ok)
        self.assertIn("box needs size", result.error)
        self.assertFalse((self.ws / "bad.stl").exists())

    def test_permissions_are_enforced(self):
        outside = self.reg.invoke("model3d_build", {"spec": PLATE, "path": "../escape.stl"}, self.ctx())
        self.assertFalse(outside.ok)
        self.assertFalse((self.ws.parent / "escape.stl").exists())
        plan = self.reg.invoke("model3d_build", {"spec": PLATE, "path": "p.stl"}, self.ctx(Mode.PLAN))
        self.assertFalse(plan.ok)
        self.assertIn(plan.meta.get("authorization"), {"deny", "ask"})
        self.assertFalse((self.ws / "p.stl").exists())
        bad = self.reg.invoke("model3d_build", {"spec": PLATE, "path": "p.step"}, self.ctx())
        self.assertFalse(bad.ok)
        self.assertIn(".stl", bad.error)

    def test_inspect_reports_broken_file(self):
        m = box(10, 10, 10)
        m.triangles.pop()
        (self.ws / "broken.stl").write_bytes(encode(m, "stl"))
        result = self.reg.invoke("model3d_inspect", {"path": "broken.stl", "views": False}, self.ctx())
        self.assertTrue(result.ok)
        self.assertIn("checks: FAIL", result.content)
        self.assertIn("not watertight: 3 open edge(s)", result.content)
        missing = self.reg.invoke("model3d_inspect", {"path": "nope.stl"}, self.ctx())
        self.assertFalse(missing.ok)


class SkillTests(unittest.TestCase):
    def test_bundled_capability_is_trusted_and_loadable(self):
        from keystone.capability import BUNDLED_ROOT, CapabilityLibrary
        with tempfile.TemporaryDirectory() as tmp:
            lib = CapabilityLibrary([BUNDLED_ROOT, Path(tmp) / "capabilities"])
            lib.scan()
            cap = lib.get("model3d")
            self.assertIsNotNone(cap)
            self.assertTrue(cap.trusted and cap.enabled)
            self.assertIn("model3d_build", cap.allowed_tools)
            self.assertIn("model3d", lib.context_injection())
            reg = build_builtin_registry()
            ws = Path(tmp).resolve()
            ctx = ToolContext(Policy(workspace=ws, non_interactive=True), ws, extras={"capabilities": lib})
            result = reg.invoke("skill_list", {"name": "model3d"}, ctx)
            self.assertTrue(result.ok, result.error)
            self.assertIn("plan → build → check → export", result.content)
            self.assertFalse(reg.invoke("skill_list", {"name": "nope"}, ctx).ok)

    def test_desktop_catalog_body_matches_skill_md(self):
        from keystone.capability import BUNDLED_ROOT, FRONTMATTER
        body = FRONTMATTER.sub("", (BUNDLED_ROOT / "model3d" / "SKILL.md").read_text(encoding="utf-8"),
                               count=1).strip()
        catalog = json.loads((Path(__file__).resolve().parents[1] / "keystone-gui" / "catalog.builtin.json")
                             .read_text(encoding="utf-8"))
        entry = next(e for e in catalog["entries"] if e["id"] == "skill-model3d")
        self.assertEqual(entry["provides"]["skill_body"], body,
                         "catalog skill_body drifted from keystone/bundled/model3d/SKILL.md")
        self.assertLessEqual(len(body), 8192)
        self.assertFalse(entry["executes_code"])


if __name__ == "__main__":
    unittest.main()
