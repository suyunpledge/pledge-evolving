"""Mesh checks that a model can act on in the same turn.

Every problem comes with a concrete fix hint phrased in spec terms, and an
optional ``expect`` block turns the user's stated intent (outer size, number of
through-holes, volume) into pass/fail lines — the model sees "size z: want 10,
got 12" instead of having to re-derive it.
"""
from __future__ import annotations

import math
from typing import Any

from .mesh import Mesh, cross, dot, length, sub, tri_normal

UNIT_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "inch": 25.4}


def _r(v: float, nd: int = 4) -> float:
    out = round(v, nd)
    return 0.0 if out == 0 else out


def _components(n_verts: int, tris) -> list[list[int]]:
    """Triangle indices grouped by connected shell (union-find over vertices)."""
    parent = list(range(n_verts))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, c in tris:
        ra = find(a)
        for other in (b, c):
            ro = find(other)
            if ro != ra:
                parent[ro] = ra
    groups: dict[int, list[int]] = {}
    for t, (a, _b, _c) in enumerate(tris):
        groups.setdefault(find(a), []).append(t)
    return list(groups.values())


def analyze(mesh: Mesh, *, units: str = "mm", expect: dict | None = None,
            purpose: str = "print") -> dict[str, Any]:
    mesh = mesh.welded()
    v, tris = mesh.vertices, mesh.triangles
    report: dict[str, Any] = {"units": units, "triangles": len(tris), "vertices": len(v)}
    problems: list[str] = []
    hints: list[str] = []
    if not tris:
        report.update(passed=False, problems=["mesh is empty (a difference/intersection removed everything?)"],
                      hints=["check that cutters overlap the base only where intended"])
        return report

    lo, hi = mesh.bbox()
    size = [hi[i] - lo[i] for i in range(3)]
    report["bbox"] = {"min": [_r(x) for x in lo], "max": [_r(x) for x in hi], "size": [_r(x) for x in size]}

    # faces
    area = 0.0
    degenerate = 0
    scale = max(max(size), 1e-9)
    for a, b, c in tris:
        n = tri_normal(v[a], v[b], v[c])
        double = length(n)
        area += double / 2
        if double <= 1e-10 * scale * scale:
            degenerate += 1
    report["surface_area"] = _r(area, 3)
    report["degenerate_faces"] = degenerate

    # edges
    directed: dict[tuple[int, int], int] = {}
    for a, b, c in tris:
        for e in ((a, b), (b, c), (c, a)):
            directed[e] = directed.get(e, 0) + 1
    undirected: dict[tuple[int, int], list[int]] = {}
    for (a, b), count in directed.items():
        key = (a, b) if a < b else (b, a)
        undirected.setdefault(key, [0, 0])[0 if a < b else 1] += count
    boundary = nonmanifold = flipped = 0
    for fwd, back in undirected.values():
        total = fwd + back
        if total == 1:
            boundary += 1
        elif total > 2:
            nonmanifold += 1
        elif fwd != 1 or back != 1:
            flipped += 1
    watertight = boundary == 0 and nonmanifold == 0
    report.update(boundary_edges=boundary, nonmanifold_edges=nonmanifold,
                  orientation_conflicts=flipped, watertight=watertight)

    shells = _components(len(v), tris)
    report["shells"] = len(shells)
    volume = mesh.signed_volume()
    report["volume"] = _r(abs(volume), 3)
    report["inverted"] = watertight and volume < 0
    if watertight and flipped == 0:
        euler = len(v) - len(undirected) + len(tris)
        genus = (2 * len(shells) - euler) // 2
        report["through_holes"] = max(0, genus)

    # shells whose boxes overlap usually mean parts were placed, not unioned
    if len(shells) > 1:
        boxes = []
        for group in shells:
            ids = {i for t in group for i in tris[t]}
            pts = [v[i] for i in ids]
            boxes.append(([min(p[k] for p in pts) for k in range(3)],
                          [max(p[k] for p in pts) for k in range(3)]))
        overlap = 0
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                (a0, a1), (b0, b1) = boxes[i], boxes[j]
                if all(a0[k] < b1[k] - 1e-6 and b0[k] < a1[k] - 1e-6 for k in range(3)):
                    overlap += 1
        report["overlapping_shell_pairs"] = overlap
        if overlap:
            hints.append(f"{overlap} pair(s) of separate shells overlap in space: wrap the parts in "
                         "{\"op\":\"union\"} so they become one solid (slicers may mis-fill overlaps)")
        else:
            hints.append(f"model has {len(shells)} separate pieces; fine for a multi-part print, "
                         "otherwise make the parts touch/overlap and union them")

    if boundary:
        problems.append(f"not watertight: {boundary} open edge(s)")
        hints.append("open edges come from hand-written polyhedron faces or a cutter that exactly "
                     "grazes a face; prefer primitives/extrude and make cutters stick out ~1 unit")
    if nonmanifold:
        problems.append(f"{nonmanifold} non-manifold edge(s) (shared by >2 faces)")
        hints.append("parts that touch only along an edge/corner make non-manifold edges; overlap them "
                     "by a small amount before union")
    if flipped:
        problems.append(f"{flipped} edge(s) with inconsistent face orientation")
        hints.append("in a polyhedron, list every face counter-clockwise as seen from outside")
    if report["inverted"]:
        problems.append("normals point inward (negative volume)")
        hints.append("reverse the face order of the polyhedron, or check a negative scale/mirror")
    if degenerate:
        problems.append(f"{degenerate} degenerate (zero-area) face(s)")

    # printability
    if purpose == "print":
        down = 0.0
        floor = lo[2] + 1e-4 * scale
        steep = math.cos(math.radians(45))
        for a, b, c in tris:
            n = tri_normal(v[a], v[b], v[c])
            double = length(n)
            if double <= 0:
                continue
            nz = n[2] / double
            if nz < -steep and max(v[a][2], v[b][2], v[c][2]) > floor:
                down += double / 2
        report["print"] = {"rests_on_z0": abs(lo[2]) < 1e-6,
                           "overhang_area": _r(down, 2),
                           "overhang_ratio": _r(down / area if area else 0.0, 3),
                           "size_mm": [_r(x * UNIT_TO_MM.get(units, 1.0), 3) for x in size]}
        if abs(lo[2]) >= 1e-6:
            hints.append(f"lowest point is z={_r(lo[2])}, not 0: translate the model so it rests on the bed")
        if area and down / area > 0.1:
            hints.append(f"{_r(100 * down / area, 1)}% of the surface overhangs more than 45 degrees: "
                         "rotate to put a large flat face down, chamfer, or plan for supports")
        mm = max(size) * UNIT_TO_MM.get(units, 1.0)
        if mm < 1.0:
            hints.append(f"largest dimension is only {_r(mm, 3)} mm: check the units (is this metres?)")
        elif mm > 400:
            hints.append(f"largest dimension is {_r(mm, 1)} mm: larger than most printer beds (~250 mm)")

    checks = _expectations(report, expect or {}, units)
    if checks:
        report["expect"] = checks
        for row in checks:
            if not row["ok"]:
                problems.append(f"expectation failed: {row['check']} wanted {row['want']}, got {row['got']}")
    report["problems"] = problems
    report["hints"] = hints
    report["passed"] = not problems
    return report


def _expectations(report: dict, expect: dict, units: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not isinstance(expect, dict):
        return [{"check": "expect", "want": "object", "got": type(expect).__name__, "ok": False}]
    tol = expect.get("tolerance", 0.01)
    tol = float(tol) if isinstance(tol, (int, float)) and not isinstance(tol, bool) else 0.01
    size = report.get("bbox", {}).get("size")
    if "size" in expect and size is not None:
        want = expect["size"]
        if isinstance(want, (list, tuple)) and len(want) == 3:
            for axis, w, g in zip("xyz", want, size):
                if w is None:
                    continue
                ok = isinstance(w, (int, float)) and abs(g - float(w)) <= max(tol, abs(float(w)) * 1e-6)
                rows.append({"check": f"size {axis}", "want": w, "got": g, "ok": bool(ok)})
        else:
            rows.append({"check": "size", "want": "[x, y, z]", "got": want, "ok": False})
    for key, label in (("through_holes", "through_holes"), ("shells", "shells")):
        if key in expect:
            want = expect[key]
            got = report.get(key)
            rows.append({"check": label, "want": want, "got": got, "ok": got == want})
    if "max_size" in expect and size is not None:
        limit = expect["max_size"]
        if isinstance(limit, (list, tuple)) and len(limit) == 3:
            for axis, w, g in zip("xyz", limit, size):
                if isinstance(w, (int, float)):
                    rows.append({"check": f"max size {axis}", "want": f"<= {w}", "got": g,
                                 "ok": g <= float(w) + tol})
    if "volume" in expect:
        want = expect["volume"]
        got = report.get("volume", 0.0)
        rel = expect.get("volume_tolerance", 0.02)
        ok = isinstance(want, (int, float)) and abs(got - want) <= abs(want) * float(rel)
        rows.append({"check": "volume", "want": want, "got": got, "ok": bool(ok)})
    if expect.get("watertight") is True:
        rows.append({"check": "watertight", "want": True, "got": report.get("watertight"),
                     "ok": report.get("watertight") is True})
    return rows


def summary_lines(report: dict[str, Any]) -> list[str]:
    """Compact human/model readable digest of ``analyze`` output."""
    u = report.get("units", "mm")
    lines = []
    status = "PASS" if report.get("passed") else "FAIL"
    lines.append(f"checks: {status}")
    bbox = report.get("bbox")
    if bbox:
        sx, sy, sz = bbox["size"]
        lines.append(f"size: {sx} x {sy} x {sz} {u} (min {bbox['min']}, max {bbox['max']})")
    lines.append(f"mesh: {report.get('triangles', 0)} triangles, {report.get('vertices', 0)} vertices, "
                 f"{report.get('shells', 0)} shell(s)")
    if bbox:
        topo = (f"watertight: {'yes' if report.get('watertight') else 'NO'}; "
                f"open edges {report.get('boundary_edges', 0)}, non-manifold {report.get('nonmanifold_edges', 0)}, "
                f"orientation conflicts {report.get('orientation_conflicts', 0)}, "
                f"degenerate faces {report.get('degenerate_faces', 0)}")
        if "through_holes" in report:
            topo += f"; through-holes (genus) {report['through_holes']}"
        lines.append(topo)
        lines.append(f"volume: {report.get('volume')} {u}^3; surface: {report.get('surface_area')} {u}^2"
                     + ("; NORMALS INVERTED" if report.get("inverted") else ""))
    if report.get("print"):
        p = report["print"]
        lines.append(f"print: rests on z=0 {'yes' if p['rests_on_z0'] else 'NO'}; "
                     f"overhang >45deg {round(100 * p['overhang_ratio'], 1)}% of surface")
    for row in report.get("expect", []):
        mark = "ok" if row["ok"] else "FAIL"
        lines.append(f"expect {row['check']}: want {row['want']}, got {row['got']} [{mark}]")
    for text in report.get("problems", []):
        lines.append(f"problem: {text}")
    for text in report.get("hints", []):
        lines.append(f"hint: {text}")
    return lines
