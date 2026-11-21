"""2D profiles (polygon, rect, circle, regular polygon, involute gear) and the
solids built from them: ``extrude`` (along +Z) and ``revolve`` (around Z)."""
from __future__ import annotations

import math

from .mesh import Mesh, auto_segments

P2 = tuple[float, float]


def signed_area(pts: list[P2]) -> float:
    s = 0.0
    for i in range(len(pts)):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % len(pts)]
        s += x0 * y1 - x1 * y0
    return s / 2.0


def clean(pts: list[P2], tol: float = 1e-9) -> list[P2]:
    """Drop repeated and collinear points; orient counter-clockwise."""
    out: list[P2] = []
    for p in pts:
        p = (float(p[0]), float(p[1]))
        if not out or abs(out[-1][0] - p[0]) > tol or abs(out[-1][1] - p[1]) > tol:
            out.append(p)
    while len(out) > 1 and abs(out[0][0] - out[-1][0]) <= tol and abs(out[0][1] - out[-1][1]) <= tol:
        out.pop()
    changed = True
    while changed and len(out) >= 3:
        changed = False
        for i in range(len(out)):
            a, b, c = out[i - 1], out[i], out[(i + 1) % len(out)]
            cr = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
            scale = max(abs(b[0] - a[0]) + abs(b[1] - a[1]), abs(c[0] - b[0]) + abs(c[1] - b[1]), 1e-12)
            if abs(cr) <= 1e-12 * scale * scale:
                out.pop(i)
                changed = True
                break
    if len(out) >= 3 and signed_area(out) < 0:
        out.reverse()
    return out


def self_intersects(pts: list[P2]) -> bool:
    n = len(pts)

    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)

    for i in range(n):
        a, b = pts[i], pts[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            c, d = pts[j], pts[(j + 1) % n]
            o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
            if o1 * o2 < 0 and o3 * o4 < 0:
                return True
    return False


def triangulate(pts: list[P2]) -> list[tuple[int, int, int]]:
    """Ear clipping for a simple CCW polygon (indices into ``pts``)."""
    n = len(pts)
    if n < 3:
        return []
    ring = list(range(n))
    out = []

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    def inside(p, a, b, c):
        return cross(a, b, p) >= -1e-12 and cross(b, c, p) >= -1e-12 and cross(c, a, p) >= -1e-12

    guard = 0
    while len(ring) > 3 and guard < 4 * n * n + 10:
        guard += 1
        clipped = False
        m = len(ring)
        for i in range(m):
            ia, ib, ic = ring[i - 1], ring[i], ring[(i + 1) % m]
            a, b, c = pts[ia], pts[ib], pts[ic]
            if cross(a, b, c) <= 1e-12:
                continue
            if any(inside(pts[k], a, b, c) for k in ring if k not in (ia, ib, ic)):
                continue
            out.append((ia, ib, ic))
            ring.pop(i)
            clipped = True
            break
        if not clipped:
            raise ValueError("profile is not a simple polygon (self-intersecting or degenerate)")
    if len(ring) == 3:
        out.append(tuple(ring))
    return out


# -- shapes -----------------------------------------------------------------

def circle(r: float, segments: int | None = None) -> list[P2]:
    n = auto_segments(r, segments)
    return [(r * math.cos(2 * math.pi * i / n), r * math.sin(2 * math.pi * i / n)) for i in range(n)]


def regular_polygon(sides: int, r: float) -> list[P2]:
    return [(r * math.cos(2 * math.pi * i / sides), r * math.sin(2 * math.pi * i / sides))
            for i in range(sides)]


def rect(w: float, h: float, center: bool = False, radius: float = 0.0,
         segments: int | None = None) -> list[P2]:
    x0, y0 = (-w / 2, -h / 2) if center else (0.0, 0.0)
    if radius <= 0:
        return [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    radius = min(radius, w / 2, h / 2)
    steps = max(2, auto_segments(radius, segments) // 4)
    corners = [(x0 + w - radius, y0 + radius, -90), (x0 + w - radius, y0 + h - radius, 0),
               (x0 + radius, y0 + h - radius, 90), (x0 + radius, y0 + radius, 180)]
    pts = []
    for cx, cy, start in corners:
        for k in range(steps + 1):
            a = math.radians(start + 90 * k / steps)
            pts.append((cx + radius * math.cos(a), cy + radius * math.sin(a)))
    return pts


def _inv(alpha: float) -> float:
    return math.tan(alpha) - alpha


def gear(teeth: int, module: float, pressure_angle: float = 20.0,
         flank_points: int = 6, clearance: float = 0.25) -> tuple[list[P2], dict]:
    """Involute spur gear outline centred on the origin. Returns (points, info)."""
    if teeth < 4:
        raise ValueError("gear needs at least 4 teeth")
    alpha = math.radians(pressure_angle)
    rp = module * teeth / 2
    rb = rp * math.cos(alpha)
    ra = rp + module
    rf = max(rp - (1 + clearance) * module, 0.1 * rp)
    half_pitch = math.pi / (2 * teeth)

    def half_thickness(r):
        r = max(r, rb)
        return half_pitch + _inv(alpha) - _inv(math.acos(min(1.0, rb / r)))

    r_start = max(rb, rf)
    if half_thickness(ra) <= 0:
        # pointed teeth: stop the flank where both sides meet
        lo, hi = r_start, ra
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if half_thickness(mid) > 0 else (lo, mid)
        ra = lo
    radii = [r_start + (ra - r_start) * k / flank_points for k in range(flank_points + 1)]
    pts: list[P2] = []
    for t in range(teeth):
        c = 2 * math.pi * t / teeth
        psi0 = half_thickness(r_start)
        if rf < r_start - 1e-9:
            pts.append((rf * math.cos(c - psi0), rf * math.sin(c - psi0)))
        for r in radii:                      # rising flank
            a = c - half_thickness(r)
            pts.append((r * math.cos(a), r * math.sin(a)))
        psi_a = half_thickness(ra)
        for k in range(1, 3):                # tip land
            a = c - psi_a + 2 * psi_a * k / 3
            pts.append((ra * math.cos(a), ra * math.sin(a)))
        for r in reversed(radii):            # falling flank
            a = c + half_thickness(r)
            pts.append((r * math.cos(a), r * math.sin(a)))
        if rf < r_start - 1e-9:
            pts.append((rf * math.cos(c + psi0), rf * math.sin(c + psi0)))
        # root land toward the next tooth
        nxt = 2 * math.pi * (t + 1) / teeth - psi0
        cur = c + psi0
        for k in range(1, 3):
            a = cur + (nxt - cur) * k / 3
            pts.append((rf * math.cos(a), rf * math.sin(a)))
    info = {"teeth": teeth, "module": module, "pitch_diameter": 2 * rp,
            "outer_diameter": 2 * ra, "root_diameter": 2 * rf, "pressure_angle": pressure_angle}
    return pts, info


# -- solids -----------------------------------------------------------------

def extrude(pts: list[P2], height: float, center: bool = False, scale_top: float = 1.0) -> Mesh:
    pts = clean(pts)
    if len(pts) < 3:
        raise ValueError("profile needs at least 3 distinct, non-collinear points")
    if height <= 0:
        raise ValueError("extrude height must be > 0")
    if self_intersects(pts):
        raise ValueError("profile edges cross each other; give the outline in order without crossings")
    cap = triangulate(pts)
    n = len(pts)
    z0 = -height / 2 if center else 0.0
    z1 = z0 + height
    if scale_top <= 0:
        raise ValueError("scale_top must be > 0")
    verts = [(x, y, z0) for x, y in pts] + [(x * scale_top, y * scale_top, z1) for x, y in pts]
    tris = [(c, b, a) for a, b, c in cap] + [(a + n, b + n, c + n) for a, b, c in cap]
    for i in range(n):
        j = (i + 1) % n
        tris += [(i, j, j + n), (i, j + n, i + n)]
    return Mesh(verts, tris)


def revolve(pts: list[P2], angle: float = 360.0, segments: int | None = None) -> Mesh:
    """Revolve a profile given in the (r, z) half-plane (r >= 0) around the Z axis."""
    raw = [(float(r), float(z)) for r, z in pts]
    if any(r < -1e-9 for r, _ in raw):
        raise ValueError("revolve profile must stay at r >= 0 (x is the distance from the Z axis)")
    pts = clean([(max(0.0, r), z) for r, z in raw])
    if len(pts) < 3:
        raise ValueError("revolve profile needs at least 3 distinct, non-collinear points")
    if self_intersects(pts):
        raise ValueError("revolve profile edges cross each other")
    full = abs(angle) >= 360.0 - 1e-9
    if not 0 < abs(angle) <= 360:
        raise ValueError("revolve angle must be in (0, 360]")
    rmax = max(r for r, _ in pts)
    n = auto_segments(rmax, segments)
    if not full:
        n = max(3, int(math.ceil(n * abs(angle) / 360.0)))
    steps = n if full else n + 1
    verts = []
    ids: list[list[int]] = []
    for r, z in pts:
        if r <= 1e-9:
            verts.append((0.0, 0.0, z))
            ids.append([len(verts) - 1] * steps)
            continue
        row = []
        for k in range(steps):
            a = math.radians(angle) * k / n
            row.append(len(verts))
            verts.append((r * math.cos(a), r * math.sin(a), z))
        ids.append(row)
    tris = []
    m = len(pts)
    for i in range(m):
        a_row, b_row = ids[i], ids[(i + 1) % m]
        for k in range(n):
            k2 = (k + 1) % steps if full else k + 1
            a0, a1, b0, b1 = a_row[k], a_row[k2], b_row[k], b_row[k2]
            for tri in ((a0, b1, b0), (a0, a1, b1)):
                if len(set(tri)) == 3:
                    tris.append(tri)
    if not full:
        cap = triangulate(pts)
        start = [ids[i][0] for i in range(m)]
        end = [ids[i][steps - 1] for i in range(m)]
        tris += [(start[a], start[b], start[c]) for a, b, c in cap]
        tris += [(end[c], end[b], end[a]) for a, b, c in cap]
    mesh = Mesh(verts, tris)
    if mesh.signed_volume() < 0:
        mesh = mesh.flipped()
    return mesh
