"""Mesh booleans by BSP trees (the csg.js algorithm), plus seam repair.

The BSP method is simple and robust on closed inputs but leaves T-junctions:
one side of a seam is split where the other side is not. ``polygons_to_mesh``
welds the result, inserts the missing seam vertices and re-triangulates each
convex piece without slivers, so the output passes an edge-manifold check
instead of only *looking* closed.

All tree walks are iterative: a deep tree must not hit Python's recursion limit.
"""
from __future__ import annotations

from .mesh import Mesh, Vec, cross, dot, sub

EPS = 1e-5
COPLANAR, FRONT, BACK, SPANNING = 0, 1, 2, 3


class Plane:
    __slots__ = ("n", "w")

    def __init__(self, n: Vec, w: float) -> None:
        self.n, self.w = n, w

    @staticmethod
    def from_points(a: Vec, b: Vec, c: Vec) -> "Plane | None":
        n = cross(sub(b, a), sub(c, a))
        size = (n[0] * n[0] + n[1] * n[1] + n[2] * n[2]) ** 0.5
        if size < 1e-12:
            return None
        n = (n[0] / size, n[1] / size, n[2] / size)
        return Plane(n, dot(n, a))

    def flipped(self) -> "Plane":
        return Plane((-self.n[0], -self.n[1], -self.n[2]), -self.w)


class Polygon:
    __slots__ = ("verts", "plane", "src")

    def __init__(self, verts: list[Vec], plane: Plane, src: int = -1) -> None:
        self.verts, self.plane, self.src = verts, plane, src

    def flipped(self) -> "Polygon":
        return Polygon(self.verts[::-1], self.plane.flipped(), self.src)


def _split(plane: Plane, poly: Polygon, cof, cob, front, back) -> None:
    n, w = plane.n, plane.w
    types = []
    ptype = 0
    for v in poly.verts:
        t = n[0] * v[0] + n[1] * v[1] + n[2] * v[2] - w
        k = BACK if t < -EPS else FRONT if t > EPS else COPLANAR
        ptype |= k
        types.append((k, t))
    if ptype == COPLANAR:
        (cof if dot(n, poly.plane.n) > 0 else cob).append(poly)
    elif ptype == FRONT:
        front.append(poly)
    elif ptype == BACK:
        back.append(poly)
    else:
        f, b = [], []
        verts = poly.verts
        count = len(verts)
        for i in range(count):
            j = (i + 1) % count
            ti, di = types[i]
            tj, dj = types[j]
            vi, vj = verts[i], verts[j]
            if ti != BACK:
                f.append(vi)
            if ti != FRONT:
                b.append(vi)
            if (ti | tj) == SPANNING:
                t = di / (di - dj)
                v = (vi[0] + (vj[0] - vi[0]) * t, vi[1] + (vj[1] - vi[1]) * t,
                     vi[2] + (vj[2] - vi[2]) * t)
                f.append(v)
                b.append(v)
        if len(f) >= 3:
            front.append(Polygon(f, poly.plane, poly.src))
        if len(b) >= 3:
            back.append(Polygon(b, poly.plane, poly.src))


def _partition(plane: Plane, polys, cof, cob, front, back) -> None:
    """``_split`` for a batch, with an inline fast path for polygons fully on one side."""
    nx, ny, nz = plane.n
    w = plane.w
    lo_lim, hi_lim = w - EPS, w + EPS
    f_append, b_append = front.append, back.append
    for poly in polys:
        verts = poly.verts
        x, y, z = verts[0]
        t = nx * x + ny * y + nz * z
        lo = hi = t
        for x, y, z in verts[1:]:
            t = nx * x + ny * y + nz * z
            if t < lo:
                lo = t
            elif t > hi:
                hi = t
        if hi < lo_lim:
            b_append(poly)
        elif lo > hi_lim:
            f_append(poly)
        else:
            _split(plane, poly, cof, cob, front, back)


class Node:
    __slots__ = ("plane", "front", "back", "polygons")

    def __init__(self, polygons: list[Polygon] | None = None) -> None:
        self.plane: Plane | None = None
        self.front: Node | None = None
        self.back: Node | None = None
        self.polygons: list[Polygon] = []
        if polygons:
            self.build(polygons)

    def nodes(self):
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            if node.front is not None:
                stack.append(node.front)
            if node.back is not None:
                stack.append(node.back)

    def invert(self) -> None:
        for node in list(self.nodes()):
            node.polygons = [p.flipped() for p in node.polygons]
            if node.plane is not None:
                node.plane = node.plane.flipped()
            node.front, node.back = node.back, node.front

    def clip_polygons(self, polygons: list[Polygon]) -> list[Polygon]:
        out: list[Polygon] = []
        stack = [(self, polygons)]
        while stack:
            node, polys = stack.pop()
            if node.plane is None:
                out.extend(polys)
                continue
            front: list[Polygon] = []
            back: list[Polygon] = []
            _partition(node.plane, polys, front, back, front, back)
            if node.front is not None:
                stack.append((node.front, front))
            else:
                out.extend(front)
            if node.back is not None:
                stack.append((node.back, back))
            # polygons behind a leaf are inside the solid: dropped
        return out

    def clip_to(self, bsp: "Node") -> None:
        for node in list(self.nodes()):
            node.polygons = bsp.clip_polygons(node.polygons)

    def all_polygons(self) -> list[Polygon]:
        out: list[Polygon] = []
        for node in self.nodes():
            out.extend(node.polygons)
        return out

    def build(self, polygons: list[Polygon]) -> None:
        stack = [(self, polygons)]
        while stack:
            node, polys = stack.pop()
            if not polys:
                continue
            if node.plane is None:
                node.plane = polys[0].plane
            front: list[Polygon] = []
            back: list[Polygon] = []
            _partition(node.plane, polys, node.polygons, node.polygons, front, back)
            if front:
                if node.front is None:
                    node.front = Node()
                stack.append((node.front, front))
            if back:
                if node.back is None:
                    node.back = Node()
                stack.append((node.back, back))


def mesh_to_polygons(mesh: Mesh, sources: list[Polygon] | None = None) -> list[Polygon]:
    out = []
    v = mesh.vertices
    for a, b, c in mesh.triangles:
        plane = Plane.from_points(v[a], v[b], v[c])
        if plane is not None:
            poly = Polygon([v[a], v[b], v[c]], plane)
            if sources is not None:
                poly.src = len(sources)
                sources.append(poly)
            out.append(poly)
    return out


def _area(verts: list[Vec]) -> float:
    total = (0.0, 0.0, 0.0)
    for k in range(1, len(verts) - 1):
        c = cross(sub(verts[k], verts[0]), sub(verts[k + 1], verts[0]))
        total = (total[0] + c[0], total[1] + c[1], total[2] + c[2])
    return dot(total, total) ** 0.5 / 2


def _restore(polygons: list[Polygon], sources: list[Polygon]) -> list[Polygon]:
    """Put back source triangles whose fragments all survived the boolean.

    BSP clipping splits faces far from the actual seam; when every piece of a
    triangle is kept (same side, total area unchanged) the original triangle is
    the same surface with far fewer, fatter faces.
    """
    groups: dict[int, list[Polygon]] = {}
    out: list[Polygon] = []
    for poly in polygons:
        if poly.src < 0 or len(poly.verts) == 3 and poly.verts == sources[poly.src].verts:
            out.append(poly)
        else:
            groups.setdefault(poly.src, []).append(poly)
    for src, parts in groups.items():
        orig = sources[src]
        same = [dot(p.plane.n, orig.plane.n) > 0 for p in parts]
        whole = _area(orig.verts)
        if (len(set(same)) == 1 and whole > 0
                and abs(sum(_area(p.verts) for p in parts) - whole) <= 1e-9 * max(whole, 1.0)):
            out.append(orig if same[0] else orig.flipped())
        else:
            out.extend(parts)
    return out


def _union(a: list[Polygon], b: list[Polygon]) -> list[Polygon]:
    na, nb = Node(a), Node(b)
    na.clip_to(nb)
    nb.clip_to(na)
    nb.invert()
    nb.clip_to(na)
    nb.invert()
    na.build(nb.all_polygons())
    return na.all_polygons()


def _subtract(a: list[Polygon], b: list[Polygon]) -> list[Polygon]:
    na, nb = Node(a), Node(b)
    na.invert()
    na.clip_to(nb)
    nb.clip_to(na)
    nb.invert()
    nb.clip_to(na)
    nb.invert()
    na.build(nb.all_polygons())
    na.invert()
    return na.all_polygons()


def _intersect(a: list[Polygon], b: list[Polygon]) -> list[Polygon]:
    na, nb = Node(a), Node(b)
    na.invert()
    nb.clip_to(na)
    nb.invert()
    na.clip_to(nb)
    nb.clip_to(na)
    na.build(nb.all_polygons())
    na.invert()
    return na.all_polygons()


def _overlap(a: Mesh, b: Mesh, pad: float = EPS) -> bool:
    (a0, a1), (b0, b1) = a.bbox(), b.bbox()
    return all(a0[i] <= b1[i] + pad and b0[i] <= a1[i] + pad for i in range(3))


def boolean(op: str, a: Mesh, b: Mesh) -> Mesh:
    """``union`` / ``difference`` / ``intersection`` of two closed meshes."""
    if not a.triangles:
        return b.copy() if op == "union" else Mesh()
    if not b.triangles:
        return a.copy() if op != "intersection" else Mesh()
    if not _overlap(a, b):
        # disjoint boxes: exact and free
        if op == "union":
            return a.copy().extend(b)
        return a.copy() if op == "difference" else Mesh()
    fn = {"union": _union, "difference": _subtract, "intersection": _intersect}[op]
    sources: list[Polygon] = []
    result = fn(mesh_to_polygons(a, sources), mesh_to_polygons(b, sources))
    return polygons_to_mesh(_restore(result, sources))


# ---------------------------------------------------------------------------
# seam repair + triangulation
# ---------------------------------------------------------------------------

WELD = 1e-6


def _point_on_segment(p: Vec, a: Vec, b: Vec, tol: float) -> float | None:
    """Parameter t in (0,1) when p lies on segment ab (strictly inside), else None."""
    ab = sub(b, a)
    denom = dot(ab, ab)
    if denom <= 0:
        return None
    t = dot(sub(p, a), ab) / denom
    if t <= 1e-9 or t >= 1 - 1e-9:
        return None
    q = (a[0] + ab[0] * t, a[1] + ab[1] * t, a[2] + ab[2] * t)
    d = sub(p, q)
    if dot(d, d) > tol * tol:
        return None
    return t


def _triangulate_convex(ids: list[int], verts: list[Vec], normal: Vec) -> list[tuple[int, int, int]]:
    """Ear-clip a convex polygon that may carry collinear (seam) vertices."""
    ring = list(ids)
    out = []
    guard = 0
    while len(ring) > 3 and guard < 10000:
        guard += 1
        best = None
        best_score = 0.0
        count = len(ring)
        for i in range(count):
            a, b, c = ring[i - 1], ring[i], ring[(i + 1) % count]
            pa, pb, pc = verts[a], verts[b], verts[c]
            area = dot(cross(sub(pb, pa), sub(pc, pb)), normal)
            if area <= 1e-12:
                continue  # reflex or collinear corner
            # no other ring vertex may sit on or inside the ear (seam points on the diagonal)
            blocked = False
            for k in ring:
                if k in (a, b, c):
                    continue
                p = verts[k]
                w0 = dot(cross(sub(pb, pa), sub(p, pa)), normal)
                w1 = dot(cross(sub(pc, pb), sub(p, pb)), normal)
                w2 = dot(cross(sub(pa, pc), sub(p, pc)), normal)
                if w0 >= -1e-12 and w1 >= -1e-12 and w2 >= -1e-12:
                    blocked = True
                    break
            if blocked:
                continue
            # prefer fat ears: area over squared longest edge
            e = max(dot(sub(pb, pa), sub(pb, pa)), dot(sub(pc, pb), sub(pc, pb)),
                    dot(sub(pa, pc), sub(pa, pc)))
            score = area / e if e > 0 else 0.0
            if score > best_score:
                best, best_score = i, score
        if best is None:
            break
        count = len(ring)
        out.append((ring[best - 1], ring[best], ring[(best + 1) % count]))
        ring.pop(best)
    if len(ring) == 3:
        pa, pb, pc = (verts[i] for i in ring)
        if dot(cross(sub(pb, pa), sub(pc, pa)), normal) > 1e-12:
            out.append(tuple(ring))
    elif len(ring) > 3:
        # fallback (should not happen for convex input): fan
        for k in range(1, len(ring) - 1):
            out.append((ring[0], ring[k], ring[k + 1]))
    return out


def polygons_to_mesh(polygons: list[Polygon]) -> Mesh:
    index: dict[tuple[int, int, int], int] = {}
    verts: list[Vec] = []
    inv = 1.0 / WELD

    def vid(p: Vec) -> int:
        key = (round(p[0] * inv), round(p[1] * inv), round(p[2] * inv))
        slot = index.get(key)
        if slot is None:
            slot = index[key] = len(verts)
            verts.append(p)
        return slot

    rings: list[tuple[list[int], Vec]] = []
    for poly in polygons:
        ids = []
        for p in poly.verts:
            i = vid(p)
            if not ids or ids[-1] != i:
                ids.append(i)
        while len(ids) > 1 and ids[0] == ids[-1]:
            ids.pop()
        if len(set(ids)) >= 3:
            rings.append((ids, poly.plane.n))

    # T-junction repair: only edges without an opposite partner can need it.
    for _ in range(3):
        directed: dict[tuple[int, int], int] = {}
        for ids, _n in rings:
            for k in range(len(ids)):
                e = (ids[k], ids[(k + 1) % len(ids)])
                directed[e] = directed.get(e, 0) + 1
        open_edges = {e for e in directed if (e[1], e[0]) not in directed}
        if not open_edges:
            break
        import bisect
        candidates = sorted({i for e in open_edges for i in e}, key=lambda i: verts[i][0])
        cand_x = [verts[i][0] for i in candidates]
        changed = False
        for r, (ids, n) in enumerate(rings):
            new_ids: list[int] = []
            touched = False
            for k in range(len(ids)):
                a, b = ids[k], ids[(k + 1) % len(ids)]
                new_ids.append(a)
                if (a, b) not in open_edges:
                    continue
                pa, pb = verts[a], verts[b]
                lo = [min(pa[i], pb[i]) - 1e-5 for i in range(3)]
                hi = [max(pa[i], pb[i]) + 1e-5 for i in range(3)]
                hits = []
                for c in candidates[bisect.bisect_left(cand_x, lo[0]):bisect.bisect_right(cand_x, hi[0])]:
                    if c == a or c == b:
                        continue
                    pc = verts[c]
                    if not (lo[0] <= pc[0] <= hi[0] and lo[1] <= pc[1] <= hi[1] and lo[2] <= pc[2] <= hi[2]):
                        continue
                    t = _point_on_segment(pc, pa, pb, 1e-5)
                    if t is not None:
                        hits.append((t, c))
                if hits:
                    touched = True
                    new_ids.extend(c for _t, c in sorted(hits))
            if touched:
                rings[r] = (new_ids, n)
                changed = True
        if not changed:
            break

    tris: list[tuple[int, int, int]] = []
    for ids, n in rings:
        tris.extend(_triangulate_convex(ids, verts, n))
    return Mesh(verts, tris).welded(WELD)
