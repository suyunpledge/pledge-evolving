"""Agent tools for 3D modelling: ``model3d_build`` and ``model3d_inspect``.

Host-owned, standard-library tools (``keystone.model3d``). ``model3d_build``
is a write tool: its ``path`` goes through the same Policy gate, workspace
write lock, checkpoint snapshot and post-write feedback as ``write_file``;
sibling outputs (extra formats, the preview PNG) are confined to the same
directory and stem. Nothing here touches the network or runs a subprocess.

Why a tool and not a prompt: hand-written vertex lists are where models lose
turns (wrong winding, open seams, a hole that is not through, numbers off by
the units). Here the model states *what* it wants, and one result carries the
file, its re-read proof, the measured dimensions, topology checks and its own
stated expectations as pass/fail lines — so a fix is one more call, not a
read/compute/guess detour.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

BUILD_DESCRIPTION = (
    "Build a 3D model from a JSON spec (parametric solids + booleans), check it and export it in ONE call. "
    "Writes `path` (.stl/.obj/.glb/.gltf) and <name>.preview.png; the result reports size, watertightness, "
    "through-holes, volume, printability, ASCII top/front views and pass/fail for `expect`. "
    "Use this instead of writing vertices or STL text by hand.\n"
    'Spec: {"units":"mm","model":NODE}. Ops: box{size[x,y,z],center} | cylinder{h, r|d, or r1+r2 (d1+d2) '
    "for a cone, segments, center} | sphere{r|d} | torus{major,minor} | extrude{profile,height,scale_top} | "
    "revolve{profile in the (r,z) half-plane, angle} | union/difference/intersection{children:[...]} "
    "(difference = first child minus the rest) | linear_array{child,count,step[x,y,z]} | "
    "polar_array{child,count,angle} | mirror_copy{child,normal} | polyhedron{points,faces}. "
    "Every node may add translate[x,y,z], rotate[x,y,z] (degrees), scale, mirror[nx,ny,nz], name. "
    "Profiles: {shape: polygon{points} | rect{size[w,h],center,radius} | circle{r|d} | "
    "regular_polygon{sides,r|d} | gear{teeth, module|pitch_diameter|outer_diameter}, holes:[profile], "
    "translate[x,y], rotate}. Placement follows OpenSCAD: box starts at the origin corner, cylinder stands "
    "on z=0 centred on Z. Make cutters overshoot the faces they cut (e.g. h+2, translate z -1)."
)

EXPECT_SCHEMA = {
    "type": "object",
    "description": "What the user asked for, checked against the result: size [x,y,z] (null = any), "
                   "through_holes, shells, volume, max_size [x,y,z], tolerance (default 0.01).",
}


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".keystone-model3d-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _size_label(n: int) -> str:
    return f"{n / 1024:.1f} KB" if n < 1024 * 1024 else f"{n / 1048576:.2f} MB"


def _views(mesh, units: str, size: list[float], cols: int = 40) -> list[str]:
    from .model3d import ascii_view
    lines = []
    for view, axes, dims in (("top", "X right, Y up", (0, 1)), ("front", "X right, Z up", (0, 2))):
        rows = ascii_view(mesh, view, cols)
        if not rows:
            continue
        lines.append(f"{view} view ({axes}; {size[dims[0]]} x {size[dims[1]]} {units}; "
                     "blank = empty, '@' = nearest):")
        lines += ["  |" + row.ljust(cols) + "|" for row in rows]
    return lines


def _workspace_path(ctx, raw: str) -> Path:
    from .secrets import assert_public_path
    path = Path(ctx.policy.abs_path(raw)).resolve()
    root = Path(ctx.workspace).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"path escapes the workspace: {raw}")
    assert_public_path(path)
    return path


def _expect(args: dict[str, Any]):
    from .tools import ToolResult
    expect = args.get("expect")
    if expect is None:
        return {}, None
    if isinstance(expect, str):
        import json
        try:
            expect = json.loads(expect)
        except ValueError:
            return None, ToolResult(False, error="expect must be an object, e.g. {\"size\":[40,40,10]}")
    if not isinstance(expect, dict):
        return None, ToolResult(False, error="expect must be an object, e.g. {\"size\":[40,40,10]}")
    return expect, None


def register_model3d_tools(reg) -> None:
    from .tools import ToolResult

    @reg.tool(
        "model3d_build", BUILD_DESCRIPTION,
        read_only=False, trusted=True,
        tags=("3d", "model", "mesh", "cad", "stl", "obj", "gltf", "print", "三维", "建模", "3D打印"),
        schema={
            "spec": {"type": "object", "description": 'Model spec: {"units":"mm","model":{"op":...}}. '
                                                      "A JSON string is accepted too."},
            "path": {"type": "string", "description": "Output file in the workspace, e.g. models/plate.stl. "
                                                      "Extension picks the format: .stl .obj .glb .gltf"},
            "formats": {"type": "array", "items": {"type": "string", "enum": ["stl", "obj", "glb", "gltf"]},
                        "description": "Extra formats written next to path with the same name."},
            "expect": EXPECT_SCHEMA,
            "purpose": {"type": "string", "enum": ["print", "visual"],
                        "description": "print (default) adds bed/overhang/size checks; visual skips them."},
            "ascii_stl": {"type": "boolean", "description": "Write ASCII instead of binary STL."},
            "preview": {"type": "boolean", "description": "Write <name>.preview.png and show ASCII views "
                                                         "(default true)."},
        },
    )
    def model3d_build(args: dict[str, Any], ctx) -> ToolResult:
        from .model3d import SpecError, analyze, compile_spec, contact_sheet, decode, encode, format_for
        from .model3d.validate import UNIT_TO_MM, summary_lines

        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return ToolResult(False, error="model3d_build needs 'path', e.g. models/part.stl")
        try:
            fmt = format_for(raw_path)
        except ValueError as exc:
            return ToolResult(False, error=str(exc))
        extra = args.get("formats") or []
        if isinstance(extra, str):
            extra = [x.strip() for x in extra.split(",") if x.strip()]
        if not isinstance(extra, list) or any(x not in ("stl", "obj", "glb", "gltf") for x in extra):
            return ToolResult(False, error="formats must list any of stl, obj, glb, gltf")
        expect, bad = _expect(args)
        if bad is not None:
            return bad
        purpose = args.get("purpose", "print")
        if purpose not in ("print", "visual"):
            return ToolResult(False, error="purpose must be 'print' or 'visual'")
        if "spec" not in args:
            return ToolResult(False, error='model3d_build needs \'spec\': {"units":"mm","model":{"op":"box",'
                                           '"size":[10,10,10]}}')
        try:
            mesh, meta = compile_spec(args["spec"])
        except SpecError as exc:
            return ToolResult(False, error=f"spec error — {exc}\nNothing was written; fix the spec and call again.",
                              meta={"error_code": "spec_error"})
        units = meta["units"]
        mm = UNIT_TO_MM[units]
        report = analyze(mesh, units=units, expect=expect, purpose=purpose)

        try:
            out = _workspace_path(ctx, raw_path)
        except (ValueError, PermissionError) as exc:
            return ToolResult(False, error=str(exc))
        targets = [(out, fmt)]
        for other in dict.fromkeys(extra):
            if other != fmt:
                targets.append((out.with_suffix("." + other), other))
        written = []
        stem = out.stem
        for target, kind in targets:
            data = encode(mesh, kind, mm_per_unit=mm, ascii_stl=bool(args.get("ascii_stl")), name=stem)
            _atomic_bytes(target, data)
            # prove the file round-trips: re-read from disk, compare the face count
            back = decode(target.read_bytes(), kind)
            same = len(back.triangles) == len(mesh.triangles)
            written.append({"path": target.relative_to(Path(ctx.workspace).resolve()).as_posix(),
                            "format": kind, "bytes": len(data), "reread_ok": same,
                            "reread_triangles": len(back.triangles)})
        preview_path = None
        if args.get("preview", True) is not False and mesh.triangles:
            png = out.with_name(stem + ".preview.png")
            _atomic_bytes(png, contact_sheet(mesh, 256))
            preview_path = png.relative_to(Path(ctx.workspace).resolve()).as_posix()

        unit_note = {"stl": "mm", "obj": "mm", "glb": "metres, Y up", "gltf": "metres, Y up"}
        lines = []
        if not report.get("passed"):
            lines.append("CHECKS FAILED — the file was written, but fix the problems below and call "
                         "model3d_build again with the same path.")
        for row in written:
            label = "binary STL" if row["format"] == "stl" and not args.get("ascii_stl") else row["format"].upper()
            proof = (f"re-read ok, {row['reread_triangles']} triangles" if row["reread_ok"]
                     else f"RE-READ MISMATCH ({row['reread_triangles']} triangles)")
            lines.append(f"wrote {row['path']} ({label}, {unit_note[row['format']]}, "
                         f"{_size_label(row['bytes'])}; {proof})")
        if preview_path:
            lines.append(f"preview: {preview_path} (iso | front / top | right; magenta = inside-out faces)")
        lines += summary_lines(report)
        for gear in meta.get("gears", []):
            lines.append(f"gear: {gear['teeth']} teeth, module {gear['module']}, pitch diameter "
                         f"{gear['pitch_diameter']}, outer diameter {gear['outer_diameter']}, "
                         f"root diameter {gear['root_diameter']}")
        for part in meta.get("parts", [])[:12]:
            lines.append(f"part {part['name']}: min {part['min']} max {part['max']}")
        if args.get("preview", True) is not False and report.get("bbox"):
            lines += _views(mesh, units, report["bbox"]["size"])
        slim = {k: v for k, v in report.items() if k not in ("hints",)}
        return ToolResult(True, "\n".join(lines), meta={
            "model3d": slim, "files": written, "preview": preview_path,
            "checks_passed": bool(report.get("passed")), "spec_nodes": meta["nodes"]})

    @reg.tool(
        "model3d_inspect",
        "Check an existing 3D model file (.stl/.obj/.glb/.gltf) in the workspace: size in mm, triangle count, "
        "watertight/manifold/orientation, through-holes, volume, printability and ASCII top/front views. "
        "Pass expect (size, through_holes, ...) to compare with what the user asked for.",
        # deferred: only needed for files the user brings; model3d_build already checks its own output.
        # tool_search "3d" / "stl" / "inspect" surfaces it.
        read_only=True, trusted=True, deferred=True,
        tags=("3d", "model", "mesh", "stl", "obj", "gltf", "validate", "inspect", "三维", "模型检查"),
        schema={
            "path": {"type": "string", "description": "Model file in the workspace."},
            "expect": EXPECT_SCHEMA,
            "purpose": {"type": "string", "enum": ["print", "visual"]},
            "views": {"type": "boolean", "description": "Include ASCII top/front views (default true)."},
        },
    )
    def model3d_inspect(args: dict[str, Any], ctx) -> ToolResult:
        from .model3d import analyze, decode, format_for
        from .model3d.validate import summary_lines
        from .secrets import read_public_bytes

        raw_path = args.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return ToolResult(False, error="model3d_inspect needs 'path'")
        try:
            fmt = format_for(raw_path)
            path = _workspace_path(ctx, raw_path)
        except (ValueError, PermissionError) as exc:
            return ToolResult(False, error=str(exc))
        if not path.is_file():
            return ToolResult(False, error=f"not a file: {raw_path}")
        expect, bad = _expect(args)
        if bad is not None:
            return bad
        purpose = args.get("purpose", "print")
        if purpose not in ("print", "visual"):
            return ToolResult(False, error="purpose must be 'print' or 'visual'")
        try:
            mesh = decode(read_public_bytes(path, limit=64 * 1024 * 1024), fmt)
        except (ValueError, KeyError, IndexError, OSError) as exc:
            return ToolResult(False, error=f"cannot parse {raw_path} as {fmt}: {exc}")
        report = analyze(mesh, units="mm", expect=expect, purpose=purpose)
        lines = [f"{raw_path}: {fmt} (read as mm, Z up)"] + summary_lines(report)
        if args.get("views", True) is not False and report.get("bbox"):
            lines += _views(mesh, "mm", report["bbox"]["size"])
        return ToolResult(True, "\n".join(lines), meta={
            "model3d": {k: v for k, v in report.items() if k != "hints"},
            "checks_passed": bool(report.get("passed"))})
