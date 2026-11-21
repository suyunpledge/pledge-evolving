"""Previews: a 2x2 PNG contact sheet (iso/front/top/right) and ASCII height maps.

Software z-buffer, flat shading, orthographic cameras — slow next to a GPU,
fast enough for a few thousand triangles, and needs nothing but ``zlib``.
Back faces are painted magenta, so inside-out or open parts stand out.
The ASCII views exist for text-only models: a through-hole shows up as blank
cells in the top view, which a model can read without image input.
"""
from __future__ import annotations

import math
import struct
import zlib

from .mesh import Mesh, cross, dot, sub

VIEWS = {
    # name: (right, up, toward-viewer)
    "iso": None,
    "front": ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0)),
    "top": ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)),
    "right": ((0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)),
}


def _norm(v):
    n = math.sqrt(dot(v, v))
    return (v[0] / n, v[1] / n, v[2] / n)


def _iso_basis():
    view = _norm((1.0, -1.0, 0.8))          # from front-right, above
    right = _norm(cross((0.0, 0.0, 1.0), view))
    right = (-right[0], -right[1], -right[2]) if right[0] < 0 else right
    up = cross(view, right)
    return right, up, view


def _basis(name):
    return _iso_basis() if name == "iso" else VIEWS[name]


def _raster(mesh: Mesh, view: str, w: int, h: int, margin: int = 6):
    """Return (depth, shade, front) buffers; None where empty."""
    R, U, F = _basis(view)
    v = mesh.vertices
    proj = [(dot(p, R), dot(p, U), dot(p, F)) for p in v]
    if not proj:
        return [None] * (w * h), [0.0] * (w * h), [True] * (w * h)
    xs = [p[0] for p in proj]
    ys = [p[1] for p in proj]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    span = max(x1 - x0, y1 - y0, 1e-9)
    s = min((w - 2 * margin) / max(x1 - x0, 1e-9 * span), (h - 2 * margin) / max(y1 - y0, 1e-9 * span))
    ox = (w - (x1 - x0) * s) / 2 - x0 * s
    oy = (h - (y1 - y0) * s) / 2 - y0 * s
    scr = [(p[0] * s + ox, h - (p[1] * s + oy), p[2]) for p in proj]
    depth: list = [None] * (w * h)
    shade = [0.0] * (w * h)
    front = [True] * (w * h)
    light = _norm((0.35, 0.25, 1.0))
    for a, b, c in mesh.triangles:
        n = cross(sub(v[b], v[a]), sub(v[c], v[a]))
        nn = math.sqrt(dot(n, n))
        if nn == 0:
            continue
        n = (n[0] / nn, n[1] / nn, n[2] / nn)
        facing = dot(n, F)
        # light in camera space: mostly from the viewer, a little from upper left
        cam = (dot(n, R), dot(n, U), facing)
        lum = 0.25 + 0.75 * abs(dot(cam, light))
        is_front = facing >= -1e-9
        (ax, ay, az), (bx, by, bz), (cx, cy, cz) = scr[a], scr[b], scr[c]
        area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        if abs(area) < 1e-12:
            continue
        minx = max(0, int(math.floor(min(ax, bx, cx))))
        maxx = min(w - 1, int(math.ceil(max(ax, bx, cx))))
        miny = max(0, int(math.floor(min(ay, by, cy))))
        maxy = min(h - 1, int(math.ceil(max(ay, by, cy))))
        inv = 1.0 / area
        for py in range(miny, maxy + 1):
            fy = py + 0.5
            row = py * w
            for px in range(minx, maxx + 1):
                fx = px + 0.5
                w0 = ((bx - fx) * (cy - fy) - (by - fy) * (cx - fx)) * inv
                w1 = ((cx - fx) * (ay - fy) - (cy - fy) * (ax - fx)) * inv
                w2 = 1.0 - w0 - w1
                if w0 < -1e-9 or w1 < -1e-9 or w2 < -1e-9:
                    continue
                z = w0 * az + w1 * bz + w2 * cz
                k = row + px
                if depth[k] is None or z > depth[k]:
                    depth[k] = z
                    shade[k] = lum
                    front[k] = is_front
    return depth, shade, front


def png_bytes(width: int, height: int, rgb: bytearray) -> bytes:
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def contact_sheet(mesh: Mesh, size: int = 256) -> bytes:
    """2x2 PNG: iso | front / top | right."""
    size = max(64, min(1024, int(size)))
    W, H = size * 2 + 3, size * 2 + 3
    img = bytearray([236, 238, 242] * (W * H))
    for idx, view in enumerate(("iso", "front", "top", "right")):
        depth, shade, front = _raster(mesh, view, size, size)
        ox = 1 + (idx % 2) * (size + 1)
        oy = 1 + (idx // 2) * (size + 1)
        for y in range(size):
            for x in range(size):
                k = y * size + x
                p = ((oy + y) * W + ox + x) * 3
                if depth[k] is None:
                    img[p:p + 3] = bytes((250, 250, 252))
                    continue
                lum = shade[k]
                if front[k]:
                    col = (int(70 + 150 * lum), int(110 + 125 * lum), int(150 + 100 * lum))
                else:
                    col = (int(120 + 135 * lum), int(30 + 40 * lum), int(120 + 135 * lum))
                img[p:p + 3] = bytes(min(255, c) for c in col)
    # separators
    for y in range(H):
        for x in (0, size + 1, W - 1):
            img[(y * W + x) * 3:(y * W + x) * 3 + 3] = b"\x9a\xa0\xaa"
    for x in range(W):
        for y in (0, size + 1, H - 1):
            img[(y * W + x) * 3:(y * W + x) * 3 + 3] = b"\x9a\xa0\xaa"
    return png_bytes(W, H, img)


RAMP = " .:-=+*#%@"


def ascii_view(mesh: Mesh, view: str = "top", cols: int = 40) -> list[str]:
    """Height map in characters: blank = nothing there, '@' = nearest the viewer."""
    R, U, F = _basis(view)
    v = mesh.vertices
    if not v:
        return []
    xs = [dot(p, R) for p in v]
    ys = [dot(p, U) for p in v]
    span_x = max(max(xs) - min(xs), 1e-9)
    span_y = max(max(ys) - min(ys), 1e-9)
    # terminal cells are ~2x taller than wide: halve rows to keep proportions
    rows = max(4, min(cols, int(round(cols * span_y / span_x / 2))))
    sub_x, sub_y = 2, 4  # supersample per character cell
    depth, _shade, front = _raster(mesh, view, cols * sub_x, rows * sub_y, margin=0)
    if all(d is None for d in depth):
        return []
    # shade by the model's full depth extent, not the visible range: a flat top
    # must read as one level instead of amplifying float noise
    zs = [dot(p, F) for p in v]
    lo, hi = min(zs), max(zs)
    out = []
    for r in range(rows):
        line = []
        for c in range(cols):
            best = None
            hits = 0
            for dy in range(sub_y):
                for dx in range(sub_x):
                    d = depth[(r * sub_y + dy) * cols * sub_x + c * sub_x + dx]
                    if d is not None:
                        hits += 1
                        best = d if best is None else max(best, d)
            if best is None or hits * 2 < sub_x * sub_y:
                line.append(" ")
            else:
                t = (best - lo) / (hi - lo) if hi > lo else 1.0
                line.append(RAMP[1 + min(len(RAMP) - 2, int(t * (len(RAMP) - 1)))])
        out.append("".join(line).rstrip())
    return out
