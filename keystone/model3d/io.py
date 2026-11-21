"""Mesh file formats: STL (binary/ASCII), OBJ, glTF 2.0 (.glb / .gltf).

STL and OBJ are written in millimetres (what slicers assume); glTF in metres
with Y up, as its specification requires. Every writer is paired with a reader
so the tool can re-open what it wrote and prove the file round-trips.
"""
from __future__ import annotations

import base64
import json
import math
import struct
from pathlib import Path

from .mesh import Mesh, tri_normal

FORMATS = {".stl": "stl", ".obj": "obj", ".glb": "glb", ".gltf": "gltf"}


def _unit(v):
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return (v[0] / n, v[1] / n, v[2] / n) if n > 0 else (0.0, 0.0, 0.0)


def stl_binary(mesh: Mesh, name: str = "keystone-model3d") -> bytes:
    v = mesh.vertices
    header = (name.encode("ascii", "replace") + b" " * 80)[:80]
    out = [header, struct.pack("<I", len(mesh.triangles))]
    pack = struct.Struct("<12fH").pack
    for a, b, c in mesh.triangles:
        n = _unit(tri_normal(v[a], v[b], v[c]))
        out.append(pack(*n, *v[a], *v[b], *v[c], 0))
    return b"".join(out)


def stl_ascii(mesh: Mesh, name: str = "keystone_model3d") -> bytes:
    v = mesh.vertices
    name = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in name) or "model"
    rows = [f"solid {name}"]
    for a, b, c in mesh.triangles:
        n = _unit(tri_normal(v[a], v[b], v[c]))
        rows.append(f"  facet normal {n[0]:.6e} {n[1]:.6e} {n[2]:.6e}")
        rows.append("    outer loop")
        for p in (v[a], v[b], v[c]):
            rows.append(f"      vertex {p[0]:.6e} {p[1]:.6e} {p[2]:.6e}")
        rows.append("    endloop")
        rows.append("  endfacet")
    rows.append(f"endsolid {name}")
    return ("\n".join(rows) + "\n").encode("ascii")


def obj_text(mesh: Mesh, name: str = "model") -> bytes:
    rows = ["# Pledge Keystone model3d export (units: mm, Z up)", f"o {name}"]
    rows += [f"v {p[0]:.6f} {p[1]:.6f} {p[2]:.6f}" for p in mesh.vertices]
    rows += [f"f {a + 1} {b + 1} {c + 1}" for a, b, c in mesh.triangles]
    return ("\n".join(rows) + "\n").encode("ascii")


def _gltf_parts(mesh: Mesh, mm_per_unit: float = 1.0):
    s = mm_per_unit / 1000.0  # glTF is metres, Y up: (x, y, z) -> (x, z, -y)
    pos = [(p[0] * s, p[2] * s, -p[1] * s) for p in mesh.vertices]
    vbytes = b"".join(struct.pack("<3f", *p) for p in pos)
    ibytes = b"".join(struct.pack("<3I", *t) for t in mesh.triangles)
    pad = (-len(vbytes)) % 4
    buffer = vbytes + b"\0" * pad + ibytes
    lo = [min(p[k] for p in pos) for k in range(3)] if pos else [0, 0, 0]
    hi = [max(p[k] for p in pos) for k in range(3)] if pos else [0, 0, 0]
    doc = {
        "asset": {"version": "2.0", "generator": "Pledge Keystone model3d"},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "name": "model"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorFactor": [0.72, 0.74, 0.78, 1.0],
                                                "metallicFactor": 0.0, "roughnessFactor": 0.8}}],
        "buffers": [{"byteLength": len(buffer)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(vbytes), "target": 34962},
            {"buffer": 0, "byteOffset": len(vbytes) + pad, "byteLength": len(ibytes), "target": 34963},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(pos), "type": "VEC3", "min": lo, "max": hi},
            {"bufferView": 1, "componentType": 5125, "count": 3 * len(mesh.triangles), "type": "SCALAR"},
        ],
    }
    return doc, buffer


def glb(mesh: Mesh, mm_per_unit: float = 1.0) -> bytes:
    doc, buffer = _gltf_parts(mesh, mm_per_unit)
    js = json.dumps(doc, separators=(",", ":")).encode("utf-8")
    js += b" " * ((-len(js)) % 4)
    buffer += b"\0" * ((-len(buffer)) % 4)
    total = 12 + 8 + len(js) + 8 + len(buffer)
    return (struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js
            + struct.pack("<II", len(buffer), 0x004E4942) + buffer)


def gltf(mesh: Mesh, mm_per_unit: float = 1.0) -> bytes:
    doc, buffer = _gltf_parts(mesh, mm_per_unit)
    doc["buffers"][0]["uri"] = "data:application/octet-stream;base64," + base64.b64encode(buffer).decode("ascii")
    return json.dumps(doc, indent=1).encode("utf-8")


def encode(mesh: Mesh, fmt: str, *, mm_per_unit: float = 1.0, ascii_stl: bool = False,
           name: str = "model") -> bytes:
    if fmt in ("stl", "obj") and mm_per_unit != 1.0:
        from .mesh import Matrix
        mesh = mesh.transformed(Matrix.scale(mm_per_unit, mm_per_unit, mm_per_unit))
    if fmt == "stl":
        return stl_ascii(mesh, name) if ascii_stl else stl_binary(mesh, name)
    if fmt == "obj":
        return obj_text(mesh, name)
    if fmt == "glb":
        return glb(mesh, mm_per_unit)
    if fmt == "gltf":
        return gltf(mesh, mm_per_unit)
    raise ValueError(f"unsupported format {fmt!r}; use one of {sorted(set(FORMATS.values()))}")


# -- readers ------------------------------------------------------------------

def _gltf_mesh(doc: dict, buffer: bytes) -> Mesh:
    prim = doc["meshes"][0]["primitives"][0]
    acc_p = doc["accessors"][prim["attributes"]["POSITION"]]
    acc_i = doc["accessors"][prim["indices"]]
    views = doc["bufferViews"]

    def view(acc):
        bv = views[acc["bufferView"]]
        start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        return start, bv["byteLength"]

    start, _ = view(acc_p)
    pos = [struct.unpack_from("<3f", buffer, start + 12 * i) for i in range(acc_p["count"])]
    start, _ = view(acc_i)
    if acc_i["componentType"] != 5125:
        raise ValueError("only uint32 glTF indices are supported")
    idx = struct.unpack_from(f"<{acc_i['count']}I", buffer, start)
    verts = [(x * 1000.0, -z * 1000.0, y * 1000.0) for x, y, z in pos]
    tris = [tuple(idx[k:k + 3]) for k in range(0, len(idx), 3)]
    return Mesh(verts, tris)


def decode(data: bytes, fmt: str) -> Mesh:
    """Parse file bytes into a mesh in millimetres, Z up."""
    if fmt == "stl":
        if len(data) >= 84:
            count = struct.unpack_from("<I", data, 80)[0]
            if 84 + 50 * count == len(data):
                verts, tris = [], []
                for k in range(count):
                    vals = struct.unpack_from("<12f", data, 84 + 50 * k)
                    base = len(verts)
                    verts += [vals[3:6], vals[6:9], vals[9:12]]
                    tris.append((base, base + 1, base + 2))
                return Mesh([tuple(map(float, p)) for p in verts], tris).welded()
        text = data.decode("ascii", "replace")
        if not text.lstrip().lower().startswith("solid"):
            raise ValueError("not an STL file (no binary size match and no 'solid' header)")
        verts = []
        for line in text.splitlines():
            parts = line.split()
            if parts and parts[0].lower() == "vertex" and len(parts) == 4:
                verts.append(tuple(float(x) for x in parts[1:]))
        if len(verts) % 3:
            raise ValueError("ASCII STL vertex count is not a multiple of 3")
        tris = [(k, k + 1, k + 2) for k in range(0, len(verts), 3)]
        return Mesh(verts, tris).welded()
    if fmt == "obj":
        verts, tris = [], []
        for line in data.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "v" and len(parts) >= 4:
                verts.append(tuple(float(x) for x in parts[1:4]))
            elif parts[0] == "f" and len(parts) >= 4:
                ids = []
                for token in parts[1:]:
                    k = int(token.split("/")[0])
                    ids.append(k - 1 if k > 0 else len(verts) + k)
                for k in range(1, len(ids) - 1):
                    tris.append((ids[0], ids[k], ids[k + 1]))
        if any(not 0 <= i < len(verts) for t in tris for i in t):
            raise ValueError("OBJ face references a missing vertex")
        return Mesh(verts, tris).welded()
    if fmt == "glb":
        magic, version, total = struct.unpack_from("<III", data, 0)
        if magic != 0x46546C67 or version != 2 or total != len(data):
            raise ValueError("not a glTF 2.0 binary (bad header or length)")
        jlen, jtype = struct.unpack_from("<II", data, 12)
        if jtype != 0x4E4F534A:
            raise ValueError("glb first chunk is not JSON")
        doc = json.loads(data[20:20 + jlen])
        off = 20 + jlen
        blen, btype = struct.unpack_from("<II", data, off)
        if btype != 0x004E4942:
            raise ValueError("glb second chunk is not BIN")
        return _gltf_mesh(doc, data[off + 8:off + 8 + blen]).welded()
    if fmt == "gltf":
        doc = json.loads(data.decode("utf-8"))
        uri = doc["buffers"][0].get("uri", "")
        if not uri.startswith("data:"):
            raise ValueError("only embedded (data URI) glTF buffers are supported")
        return _gltf_mesh(doc, base64.b64decode(uri.split(",", 1)[1])).welded()
    raise ValueError(f"unsupported format {fmt!r}")


def format_for(path: str | Path) -> str:
    fmt = FORMATS.get(Path(path).suffix.lower())
    if fmt is None:
        raise ValueError(f"unsupported file extension {Path(path).suffix!r}; use .stl, .obj, .glb or .gltf")
    return fmt
