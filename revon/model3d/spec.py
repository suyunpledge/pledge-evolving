"""Compile a JSON model description into a mesh.

A spec is a tree of nodes. Every node has ``op`` and may carry ``translate``,
``rotate`` (degrees, X then Y then Z), ``scale``, ``mirror`` and ``name``; the
transforms apply after the node is built, in the order scale → mirror → rotate
→ translate. Errors name the exact node path and the keys that op accepts, so
a bad spec costs one corrected call, not a debugging session.
"""
from __future__ import annotations

import json
import math
from typing import Any

from . import csg, profiles
from .mesh import Matrix, Mesh, box, cylinder, polyhedron, sphere, torus

UNITS = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "inch": 25.4}
MAX_NODES = 400
MAX_TRIANGLES = 200_000
MAX_BOOLEAN_TRIANGLES = 60_000
COMMON = {"op", "name", "translate", "rotate", "scale", "mirror", "comment"}

OPS: dict[str, tuple[set[str], str]] = {
    "box": ({"size", "center"}, '{"op":"box","size":[40,40,10]}'),
    "cylinder": ({"h", "height", "r", "d", "r1", "r2", "d1", "d2", "segments", "center"},
                 '{"op":"cylinder","d":20,"h":12,"translate":[20,20,-1]}'),
    "sphere": ({"r", "d", "segments"}, '{"op":"sphere","r":10}'),
    "torus": ({"major", "minor", "R", "r", "segments", "tube_segments"},
              '{"op":"torus","major":20,"minor":4}'),
    "polyhedron": ({"points", "faces"},
                   '{"op":"polyhedron","points":[[0,0,0],[10,0,0],[0,10,0],[0,0,10]],'
                   '"faces":[[0,2,1],[0,1,3],[1,2,3],[0,3,2]]}'),
    "extrude": ({"profile", "height", "h", "center", "scale_top"},
                '{"op":"extrude","profile":{"shape":"gear","teeth":12,"module":2},"height":8}'),
    "revolve": ({"profile", "angle", "segments"},
                '{"op":"revolve","profile":{"shape":"polygon","points":[[0,0],[30,0],[30,5],[0,5]]}}'),
    "union": ({"children"}, '{"op":"union","children":[{...},{...}]}'),
    "difference": ({"children"}, '{"op":"difference","children":[{base},{cutter1},{cutter2}]}'),
    "intersection": ({"children"}, '{"op":"intersection","children":[{...},{...}]}'),
    "linear_array": ({"child", "count", "step"},
                     '{"op":"linear_array","count":4,"step":[10,0,0],"child":{...}}'),
    "polar_array": ({"child", "count", "angle"},
                    '{"op":"polar_array","count":6,"child":{...,"translate":[15,0,0]}}'),
    "mirror_copy": ({"child", "normal"},
                    '{"op":"mirror_copy","normal":[1,0,0],"child":{...}}'),
}
ALIASES = {"cube": "box", "cone": "cylinder", "group": "union", "subtract": "difference",
           "linear_extrude": "extrude", "rotate_extrude": "revolve"}

SHAPES: dict[str, tuple[set[str], str]] = {
    "polygon": ({"points"}, '{"shape":"polygon","points":[[0,0],[30,0],[30,4],[0,20]]}'),
    "rect": ({"size", "center", "radius", "segments"}, '{"shape":"rect","size":[40,20],"radius":3}'),
    "circle": ({"r", "d", "segments"}, '{"shape":"circle","d":20}'),
    "regular_polygon": ({"sides", "r", "d"}, '{"shape":"regular_polygon","sides":6,"d":10}'),
    "gear": ({"teeth", "module", "pitch_diameter", "outer_diameter", "pressure_angle"},
             '{"shape":"gear","teeth":12,"module":2}'),
}
SHAPE_COMMON = {"shape", "holes", "translate", "rotate"}


class SpecError(ValueError):
    pass


def _err(path: str, message: str, example: str = "") -> SpecError:
    return SpecError(f"{path}: {message}" + (f"\n  example: {example}" if example else ""))


def _num(node: dict, key: str, path: str, *, positive=False, minimum=None, default=None):
    if key not in node:
        return default
    value = node[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _err(f"{path}.{key}", f"must be a number, got {json.dumps(value)[:40]}")
    if positive and value <= 0:
        raise _err(f"{path}.{key}", f"must be > 0, got {value}")
    if minimum is not None and value < minimum:
        raise _err(f"{path}.{key}", f"must be >= {minimum}, got {value}")
    return float(value)


def _vec(value, path: str, n: int = 3, *, scalar_ok=False) -> tuple[float, ...]:
    if scalar_ok and isinstance(value, (int, float)) and not isinstance(value, bool):
        return (float(value),) * n
    if (not isinstance(value, (list, tuple)) or len(value) != n
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   for v in value)):
        raise _err(path, f"must be a list of {n} numbers, got {json.dumps(value)[:60]}")
    return tuple(float(v) for v in value)


def _radius(node: dict, path: str, r: str = "r", d: str = "d", required=True,
            allow_zero=False) -> float | None:
    if r in node and d in node:
        raise _err(path, f"give either {r} or {d}, not both")
    strict = not allow_zero
    if r in node:
        return _num(node, r, path, positive=strict, minimum=0 if allow_zero else None)
    if d in node:
        return _num(node, d, path, positive=strict, minimum=0 if allow_zero else None) / 2
    if required:
        raise _err(path, f"missing radius: give {r} (radius) or {d} (diameter)")
    return None


def _segments(node: dict, path: str) -> int | None:
    if "segments" not in node:
        return None
    value = node["segments"]
    if isinstance(value, bool) or not isinstance(value, int) or not 3 <= value <= 512:
        raise _err(f"{path}.segments", "must be an integer between 3 and 512")
    return value


def _check_keys(node: dict, allowed: set[str], path: str, example: str) -> None:
    extra = sorted(set(node) - allowed)
    if extra:
        raise _err(path, f"unknown key(s) {extra}; allowed: {sorted(allowed)}", example)


class Compiler:
    def __init__(self) -> None:
        self.nodes = 0
        self.parts: list[dict[str, Any]] = []
        self.info: dict[str, Any] = {}
        self.notes: list[str] = []

    # -- profiles -------------------------------------------------------
    def profile(self, node, path: str) -> tuple[list[tuple[float, float]], list[list[tuple[float, float]]]]:
        if not isinstance(node, dict):
            raise _err(path, "must be an object like {\"shape\":\"circle\",\"d\":20}")
        shape = node.get("shape")
        if shape not in SHAPES:
            raise _err(f"{path}.shape", f"unknown shape {shape!r}; use one of {sorted(SHAPES)}")
        allowed, example = SHAPES[shape]
        _check_keys(node, allowed | SHAPE_COMMON, path, example)
        if shape == "polygon":
            raw = node.get("points")
            if not isinstance(raw, list) or len(raw) < 3:
                raise _err(f"{path}.points", "needs at least 3 [x, y] points", example)
            pts = [_vec(p, f"{path}.points[{i}]", 2) for i, p in enumerate(raw)]
        elif shape == "rect":
            if "size" not in node:
                raise _err(path, "rect needs size [w, h]", example)
            w, h = _vec(node["size"], f"{path}.size", 2, scalar_ok=True)
            if w <= 0 or h <= 0:
                raise _err(f"{path}.size", "must be > 0")
            pts = profiles.rect(w, h, bool(node.get("center", False)),
                                _num(node, "radius", path, minimum=0, default=0.0), _segments(node, path))
        elif shape == "circle":
            pts = profiles.circle(_radius(node, path), _segments(node, path))
        elif shape == "regular_polygon":
            sides = node.get("sides")
            if isinstance(sides, bool) or not isinstance(sides, int) or not 3 <= sides <= 512:
                raise _err(f"{path}.sides", "must be an integer >= 3", example)
            pts = profiles.regular_polygon(sides, _radius(node, path))
        else:
            teeth = node.get("teeth")
            if isinstance(teeth, bool) or not isinstance(teeth, int) or not 4 <= teeth <= 400:
                raise _err(f"{path}.teeth", "must be an integer between 4 and 400", example)
            given = [k for k in ("module", "pitch_diameter", "outer_diameter") if k in node]
            if len(given) != 1:
                raise _err(path, "give exactly one of module, pitch_diameter, outer_diameter", example)
            size = _num(node, given[0], path, positive=True)
            module = {"module": size, "pitch_diameter": size / teeth,
                      "outer_diameter": size / (teeth + 2)}[given[0]]
            pa = _num(node, "pressure_angle", path, default=20.0)
            if not 10 <= pa <= 35:
                raise _err(f"{path}.pressure_angle", "must be between 10 and 35 degrees")
            pts, info = profiles.gear(teeth, module, pa)
            self.info.setdefault("gears", []).append({k: round(v, 4) if isinstance(v, float) else v
                                                      for k, v in info.items()})
        pts = self._place2d(pts, node, path)
        holes = []
        raw_holes = node.get("holes", [])
        if not isinstance(raw_holes, list):
            raise _err(f"{path}.holes", "must be a list of profiles")
        for i, hole in enumerate(raw_holes):
            outer, inner = self.profile(hole, f"{path}.holes[{i}]")
            holes.append(outer)
            holes.extend(inner)
        return pts, holes

    def _place2d(self, pts, node, path):
        if "rotate" in node:
            a = math.radians(_num(node, "rotate", path))
            c, s = math.cos(a), math.sin(a)
            pts = [(x * c - y * s, x * s + y * c) for x, y in pts]
        if "translate" in node:
            dx, dy = _vec(node["translate"], f"{path}.translate", 2)
            pts = [(x + dx, y + dy) for x, y in pts]
        return pts

    # -- nodes ------------------------------------------------------------
    def node(self, node, path: str) -> Mesh:
        if not isinstance(node, dict):
            raise _err(path, "must be an object with an \"op\" key")
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise _err(path, f"spec has more than {MAX_NODES} nodes; use linear_array/polar_array")
        op = node.get("op", node.get("type"))
        op = ALIASES.get(op, op)
        if op not in OPS:
            raise _err(f"{path}.op", f"unknown op {node.get('op')!r}; use one of {sorted(OPS)}")
        allowed, example = OPS[op]
        _check_keys({k: v for k, v in node.items() if k != "type"}, allowed | COMMON, path, example)
        mesh = getattr(self, "op_" + op)(node, path, example)
        mesh = self._transform(mesh, node, path)
        if len(mesh.triangles) > MAX_TRIANGLES:
            raise _err(path, f"model exceeds {MAX_TRIANGLES} triangles; lower 'segments'")
        if isinstance(node.get("name"), str) and mesh.triangles:
            lo, hi = mesh.bbox()
            self.parts.append({"name": node["name"][:60], "min": [round(v, 4) for v in lo],
                               "max": [round(v, 4) for v in hi]})
        return mesh

    def _transform(self, mesh: Mesh, node: dict, path: str) -> Mesh:
        m = Matrix()
        if "scale" in node:
            sx, sy, sz = _vec(node["scale"], f"{path}.scale", 3, scalar_ok=True)
            if 0 in (sx, sy, sz):
                raise _err(f"{path}.scale", "must not contain 0")
            m = Matrix.scale(sx, sy, sz) @ m
        if "mirror" in node:
            m = Matrix.mirror(*_vec(node["mirror"], f"{path}.mirror")) @ m
        if "rotate" in node:
            m = Matrix.rotate(*_vec(node["rotate"], f"{path}.rotate")) @ m
        if "translate" in node:
            m = Matrix.translate(*_vec(node["translate"], f"{path}.translate")) @ m
        if m.m == Matrix().m:
            return mesh
        return mesh.transformed(m)

    def op_box(self, node, path, example):
        if "size" not in node:
            raise _err(path, "box needs size [x, y, z] (or one number for a cube)", example)
        sx, sy, sz = _vec(node["size"], f"{path}.size", 3, scalar_ok=True)
        if min(sx, sy, sz) <= 0:
            raise _err(f"{path}.size", "all sizes must be > 0")
        return box(sx, sy, sz, bool(node.get("center", False)))

    def op_cylinder(self, node, path, example):
        h = node.get("h", node.get("height"))
        if "h" in node and "height" in node:
            raise _err(path, "give either h or height")
        if h is None:
            raise _err(path, "cylinder needs h (height)", example)
        h = _num({"h": h}, "h", path, positive=True)
        r = _radius(node, path, required=False)
        r1 = _radius(node, path, "r1", "d1", required=False, allow_zero=True)
        r2 = _radius(node, path, "r2", "d2", required=False, allow_zero=True)
        if r is not None and (r1 is not None or r2 is not None):
            raise _err(path, "use r/d for a straight cylinder or r1/r2 (d1/d2) for a cone, not both")
        if r is None and r1 is None and r2 is None:
            raise _err(path, "cylinder needs r or d (or r1/r2 for a cone)", example)
        if r is not None:
            r1 = r2 = r
        if r1 is None or r2 is None:
            raise _err(path, "a cone needs both r1 and r2 (or d1 and d2); use 0 for a point", example)
        if r1 <= 0 and r2 <= 0:
            raise _err(path, "at least one cone radius must be > 0")
        return cylinder(r1, r2, h, _segments(node, path), bool(node.get("center", False)))

    def op_sphere(self, node, path, example):
        return sphere(_radius(node, path), _segments(node, path))

    def op_torus(self, node, path, example):
        major = _num(node, "major", path, positive=True, default=_num(node, "R", path, positive=True))
        minor = _num(node, "minor", path, positive=True, default=_num(node, "r", path, positive=True))
        if major is None or minor is None:
            raise _err(path, "torus needs major (ring radius) and minor (tube radius)", example)
        if minor >= major:
            raise _err(path, "minor must be smaller than major")
        return torus(major, minor, _segments(node, path),
                     node.get("tube_segments") if isinstance(node.get("tube_segments"), int) else None)

    def op_polyhedron(self, node, path, example):
        pts, faces = node.get("points"), node.get("faces")
        if not isinstance(pts, list) or len(pts) < 4 or not isinstance(faces, list) or len(faces) < 4:
            raise _err(path, "polyhedron needs points (>=4 [x,y,z]) and faces (>=4 index lists)", example)
        pts = [_vec(p, f"{path}.points[{i}]") for i, p in enumerate(pts)]
        for i, face in enumerate(faces):
            if (not isinstance(face, list) or len(face) < 3
                    or any(isinstance(k, bool) or not isinstance(k, int) or not 0 <= k < len(pts) for k in face)):
                raise _err(f"{path}.faces[{i}]", f"must list >= 3 point indices in 0..{len(pts) - 1}")
        self.notes.append("polyhedron faces must be counter-clockwise seen from outside")
        return polyhedron(pts, faces)

    def op_extrude(self, node, path, example):
        if "profile" not in node:
            raise _err(path, "extrude needs a 2D profile", example)
        h = node.get("height", node.get("h"))
        if h is None:
            raise _err(path, "extrude needs height", example)
        h = _num({"height": h}, "height", path, positive=True)
        outer, holes = self.profile(node["profile"], f"{path}.profile")
        center = bool(node.get("center", False))
        scale_top = _num(node, "scale_top", path, positive=True, default=1.0)
        try:
            mesh = profiles.extrude(outer, h, center, scale_top)
        except ValueError as exc:
            raise _err(f"{path}.profile", str(exc)) from None
        if holes:
            if scale_top != 1.0:
                raise _err(path, "profile holes cannot be combined with scale_top; subtract a cutter instead")
            z0 = -h / 2 if center else 0.0
            for i, hole in enumerate(holes):
                try:
                    cutter = profiles.extrude(hole, h + 2.0).transformed(Matrix.translate(0, 0, z0 - 1.0))
                except ValueError as exc:
                    raise _err(f"{path}.profile.holes[{i}]", str(exc)) from None
                mesh = self._boolean("difference", mesh, cutter, path)
        return mesh

    def op_revolve(self, node, path, example):
        if "profile" not in node:
            raise _err(path, "revolve needs a 2D profile in the (r, z) half-plane", example)
        outer, holes = self.profile(node["profile"], f"{path}.profile")
        if holes:
            raise _err(f"{path}.profile.holes", "revolve does not take holes; leave a gap in the profile instead")
        angle = _num(node, "angle", path, default=360.0)
        try:
            return profiles.revolve(outer, angle, _segments(node, path))
        except ValueError as exc:
            raise _err(f"{path}.profile", str(exc)) from None

    def _children(self, node, path, example, minimum=1):
        kids = node.get("children")
        if not isinstance(kids, list) or len(kids) < minimum:
            raise _err(path, f"needs children: a list of at least {minimum} node(s)", example)
        return [self.node(child, f"{path}.children[{i}]") for i, child in enumerate(kids)]

    def _boolean(self, op, a: Mesh, b: Mesh, path) -> Mesh:
        if len(a.triangles) + len(b.triangles) > MAX_BOOLEAN_TRIANGLES:
            raise _err(path, f"boolean operands exceed {MAX_BOOLEAN_TRIANGLES} triangles; "
                             "lower 'segments' on curved parts")
        return csg.boolean(op, a, b)

    def op_union(self, node, path, example):
        meshes = self._children(node, path, example)
        out = meshes[0]
        for m in meshes[1:]:
            out = self._boolean("union", out, m, path)
        return out

    def op_difference(self, node, path, example):
        meshes = self._children(node, path, example, 2)
        out = meshes[0]
        for m in meshes[1:]:
            out = self._boolean("difference", out, m, path)
        return out

    def op_intersection(self, node, path, example):
        meshes = self._children(node, path, example, 2)
        out = meshes[0]
        for m in meshes[1:]:
            out = self._boolean("intersection", out, m, path)
        return out

    def _count(self, node, path, example):
        count = node.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 500:
            raise _err(f"{path}.count", "must be an integer between 1 and 500", example)
        return count

    def _child(self, node, path, example):
        if "child" not in node:
            raise _err(path, "needs child: one node", example)
        return self.node(node["child"], f"{path}.child")

    def _union_all(self, copies: list[Mesh], path) -> Mesh:
        out = copies[0]
        for m in copies[1:]:
            out = self._boolean("union", out, m, path)
        return out

    def op_linear_array(self, node, path, example):
        count = self._count(node, path, example)
        if "step" not in node:
            raise _err(path, "linear_array needs step [dx, dy, dz]", example)
        step = _vec(node["step"], f"{path}.step")
        child = self._child(node, path, example)
        copies = [child.transformed(Matrix.translate(step[0] * i, step[1] * i, step[2] * i))
                  for i in range(count)]
        return self._union_all(copies, path)

    def op_polar_array(self, node, path, example):
        count = self._count(node, path, example)
        angle = _num(node, "angle", path, default=360.0)
        child = self._child(node, path, example)
        full = abs(angle) >= 360.0 - 1e-9
        step = angle / count if full else (angle / (count - 1) if count > 1 else 0.0)
        copies = [child.transformed(Matrix.rotate(0, 0, step * i)) for i in range(count)]
        return self._union_all(copies, path)

    def op_mirror_copy(self, node, path, example):
        normal = _vec(node.get("normal", [1, 0, 0]), f"{path}.normal")
        if normal == (0.0, 0.0, 0.0):
            raise _err(f"{path}.normal", "must not be zero")
        child = self._child(node, path, example)
        return self._boolean("union", child, child.transformed(Matrix.mirror(*normal)), path)


def parse_spec(spec: Any) -> tuple[dict, str]:
    """Accept a dict or JSON text; return (root node, units)."""
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except json.JSONDecodeError as exc:
            raise SpecError(f"spec is not valid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}") from None
    if not isinstance(spec, dict):
        raise SpecError('spec must be an object: {"units":"mm","model":{"op":...}}')
    units = spec.get("units", "mm")
    if units not in UNITS:
        raise SpecError(f"units must be one of {sorted(UNITS)}")
    root = spec.get("model", spec.get("root"))
    if root is None:
        if "op" in spec or "type" in spec:
            root = {k: v for k, v in spec.items() if k != "units"}
        else:
            raise SpecError('spec needs "model": a node such as {"op":"box","size":[10,10,10]}')
    return root, units


def compile_spec(spec: Any) -> tuple[Mesh, dict[str, Any]]:
    root, units = parse_spec(spec)
    compiler = Compiler()
    mesh = compiler.node(root, "model").welded()
    meta = {"units": units, "nodes": compiler.nodes, "parts": compiler.parts, **compiler.info}
    if compiler.notes:
        meta["notes"] = sorted(set(compiler.notes))
    return mesh, meta
