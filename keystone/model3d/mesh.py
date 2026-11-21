"""Triangle meshes, affine transforms and 3D primitives (standard library only).

Convention: right-handed, Z up, triangles wound counter-clockwise when seen
from outside (normals point out). Primitives follow OpenSCAD placement so the
habits a model already has carry over: ``box`` sits on the origin corner and
``cylinder`` stands on z=0 around the Z axis unless ``center`` is set.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

Vec = tuple[float, float, float]


def sub(a: Vec, b: Vec) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def length(a: Vec) -> float:
    return math.sqrt(dot(a, a))


def tri_normal(a: Vec, b: Vec, c: Vec) -> Vec:
    """Unnormalised normal; its length is twice the triangle area."""
    return cross(sub(b, a), sub(c, a))


@dataclass
class Mesh:
    vertices: list[Vec] = field(default_factory=list)
    triangles: list[tuple[int, int, int]] = field(default_factory=list)

    def copy(self) -> "Mesh":
        return Mesh(list(self.vertices), list(self.triangles))

    def extend(self, other: "Mesh") -> "Mesh":
        base = len(self.vertices)
        self.vertices.extend(other.vertices)
        self.triangles.extend((a + base, b + base, c + base) for a, b, c in other.triangles)
        return self

    def bbox(self) -> tuple[Vec, Vec]:
        if not self.vertices:
            return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
        used = {i for t in self.triangles for i in t} or range(len(self.vertices))
        xs = [self.vertices[i][0] for i in used]
        ys = [self.vertices[i][1] for i in used]
        zs = [self.vertices[i][2] for i in used]
        return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))

    def flipped(self) -> "Mesh":
        return Mesh(list(self.vertices), [(a, c, b) for a, b, c in self.triangles])

    def signed_volume(self) -> float:
        v = self.vertices
        total = 0.0
        for a, b, c in self.triangles:
            total += dot(v[a], cross(v[b], v[c]))
        return total / 6.0

    def transformed(self, m: "Matrix") -> "Mesh":
        out = Mesh([m.apply(p) for p in self.vertices], list(self.triangles))
        return out.flipped() if m.determinant() < 0 else out

    def welded(self, tol: float = 1e-6) -> "Mesh":
        """Merge coincident vertices, drop unused ones and index-degenerate triangles."""
        index: dict[tuple[int, int, int], int] = {}
        verts: list[Vec] = []
        remap: list[int] = []
        inv = 1.0 / tol
        for p in self.vertices:
            key = (round(p[0] * inv), round(p[1] * inv), round(p[2] * inv))
            slot = index.get(key)
            if slot is None:
                slot = index[key] = len(verts)
                verts.append(p)
            remap.append(slot)
        tris = []
        for a, b, c in self.triangles:
            a, b, c = remap[a], remap[b], remap[c]
            if a != b and b != c and a != c:
                tris.append((a, b, c))
        used = sorted({i for t in tris for i in t})
        if len(used) != len(verts):
            pack = {old: new for new, old in enumerate(used)}
            verts = [verts[i] for i in used]
            tris = [(pack[a], pack[b], pack[c]) for a, b, c in tris]
        return Mesh(verts, tris)


class Matrix:
    """Row-major 3x4 affine matrix."""

    __slots__ = ("m",)

    def __init__(self, m=None) -> None:
        self.m = m or [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]]

    def __matmul__(self, other: "Matrix") -> "Matrix":
        a, b = self.m, other.m
        out = []
        for i in range(3):
            row = []
            for j in range(4):
                value = sum(a[i][k] * b[k][j] for k in range(3))
                if j == 3:
                    value += a[i][3]
                row.append(value)
            out.append(row)
        return Matrix(out)

    def apply(self, p: Vec) -> Vec:
        m = self.m
        return (m[0][0] * p[0] + m[0][1] * p[1] + m[0][2] * p[2] + m[0][3],
                m[1][0] * p[0] + m[1][1] * p[1] + m[1][2] * p[2] + m[1][3],
                m[2][0] * p[0] + m[2][1] * p[1] + m[2][2] * p[2] + m[2][3])

    def determinant(self) -> float:
        (a, b, c, _), (d, e, f, _), (g, h, i, _) = self.m
        return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)

    @staticmethod
    def translate(x: float, y: float, z: float) -> "Matrix":
        return Matrix([[1.0, 0.0, 0.0, x], [0.0, 1.0, 0.0, y], [0.0, 0.0, 1.0, z]])

    @staticmethod
    def scale(x: float, y: float, z: float) -> "Matrix":
        return Matrix([[x, 0.0, 0.0, 0.0], [0.0, y, 0.0, 0.0], [0.0, 0.0, z, 0.0]])

    @staticmethod
    def rotate(rx: float, ry: float, rz: float) -> "Matrix":
        """Degrees, applied X then Y then Z (OpenSCAD ``rotate([x,y,z])``)."""
        def axis(deg, which):
            r = math.radians(deg)
            c, s = math.cos(r), math.sin(r)
            # snap exact quarter turns so axis-aligned results stay exact
            if abs(c) < 1e-15:
                c = 0.0
            if abs(s) < 1e-15:
                s = 0.0
            if which == 0:
                return Matrix([[1.0, 0.0, 0.0, 0.0], [0.0, c, -s, 0.0], [0.0, s, c, 0.0]])
            if which == 1:
                return Matrix([[c, 0.0, s, 0.0], [0.0, 1.0, 0.0, 0.0], [-s, 0.0, c, 0.0]])
            return Matrix([[c, -s, 0.0, 0.0], [s, c, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0]])
        return axis(rz, 2) @ axis(ry, 1) @ axis(rx, 0)

    @staticmethod
    def mirror(nx: float, ny: float, nz: float) -> "Matrix":
        """Reflection through the plane with normal n that passes through the origin."""
        n = length((nx, ny, nz))
        if n == 0:
            raise ValueError("mirror normal must not be zero")
        x, y, z = nx / n, ny / n, nz / n
        return Matrix([[1 - 2 * x * x, -2 * x * y, -2 * x * z, 0.0],
                       [-2 * x * y, 1 - 2 * y * y, -2 * y * z, 0.0],
                       [-2 * x * z, -2 * y * z, 1 - 2 * z * z, 0.0]])


def auto_segments(radius: float, requested: int | None = None) -> int:
    """Facet count: about one facet per 1.5 units of circumference, 24..96."""
    if requested:
        return max(3, min(512, int(requested)))
    return max(24, min(96, int(math.ceil(2 * math.pi * abs(radius) / 1.5))))


def box(sx: float, sy: float, sz: float, center: bool = False) -> Mesh:
    ox, oy, oz = (-sx / 2, -sy / 2, -sz / 2) if center else (0.0, 0.0, 0.0)
    v = [(ox + x * sx, oy + y * sy, oz + z * sz) for z in (0, 1) for y in (0, 1) for x in (0, 1)]
    # index = x + 2y + 4z
    quads = [(0, 2, 3, 1), (4, 5, 7, 6),   # bottom (-z), top (+z)
             (0, 1, 5, 4), (2, 6, 7, 3),   # front (-y), back (+y)
             (0, 4, 6, 2), (1, 3, 7, 5)]   # left (-x), right (+x)
    tris = []
    for a, b, c, d in quads:
        tris += [(a, b, c), (a, c, d)]
    return Mesh(v, tris)


def cylinder(r1: float, r2: float, h: float, segments: int | None = None,
             center: bool = False) -> Mesh:
    n = auto_segments(max(r1, r2), segments)
    z0 = -h / 2 if center else 0.0
    z1 = z0 + h
    verts: list[Vec] = [(0.0, 0.0, z0), (0.0, 0.0, z1)]
    tris: list[tuple[int, int, int]] = []
    ring = []
    for radius, z in ((r1, z0), (r2, z1)):
        if radius <= 0:
            ring.append(None)
            continue
        start = len(verts)
        for i in range(n):
            a = 2 * math.pi * i / n
            verts.append((radius * math.cos(a), radius * math.sin(a), z))
        ring.append(start)
    lo, hi = ring
    for i in range(n):
        j = (i + 1) % n
        if lo is not None:
            tris.append((0, lo + j, lo + i))
        if hi is not None:
            tris.append((1, hi + i, hi + j))
        if lo is not None and hi is not None:
            tris += [(lo + i, lo + j, hi + j), (lo + i, hi + j, hi + i)]
        elif lo is not None:
            tris.append((lo + i, lo + j, 1))
        elif hi is not None:
            tris.append((0, hi + j, hi + i))
    return Mesh(verts, tris).welded()


def sphere(r: float, segments: int | None = None) -> Mesh:
    n = auto_segments(r, segments)
    rings = max(2, n // 2)
    verts: list[Vec] = [(0.0, 0.0, -r), (0.0, 0.0, r)]
    tris: list[tuple[int, int, int]] = []
    for k in range(1, rings):
        phi = -math.pi / 2 + math.pi * k / rings
        z, rr = r * math.sin(phi), r * math.cos(phi)
        for i in range(n):
            a = 2 * math.pi * i / n
            verts.append((rr * math.cos(a), rr * math.sin(a), z))

    def at(k, i):
        return 2 + (k - 1) * n + (i % n)

    for i in range(n):
        tris.append((0, at(1, i + 1), at(1, i)))
        tris.append((1, at(rings - 1, i), at(rings - 1, i + 1)))
    for k in range(1, rings - 1):
        for i in range(n):
            a, b, c, d = at(k, i), at(k, i + 1), at(k + 1, i + 1), at(k + 1, i)
            tris += [(a, b, c), (a, c, d)]
    return Mesh(verts, tris)


def torus(major: float, minor: float, segments: int | None = None,
          tube_segments: int | None = None) -> Mesh:
    n = auto_segments(major + minor, segments)
    m = auto_segments(minor, tube_segments)
    verts: list[Vec] = []
    for i in range(n):
        a = 2 * math.pi * i / n
        for j in range(m):
            b = 2 * math.pi * j / m
            rr = major + minor * math.cos(b)
            verts.append((rr * math.cos(a), rr * math.sin(a), minor * math.sin(b)))
    tris = []
    for i in range(n):
        for j in range(m):
            a = i * m + j
            b = ((i + 1) % n) * m + j
            c = ((i + 1) % n) * m + (j + 1) % m
            d = i * m + (j + 1) % m
            tris += [(a, b, c), (a, c, d)]
    return Mesh(verts, tris)


def polyhedron(points, faces) -> Mesh:
    verts = [tuple(float(c) for c in p) for p in points]
    tris = []
    for face in faces:
        face = [int(i) for i in face]
        for k in range(1, len(face) - 1):
            tris.append((face[0], face[k], face[k + 1]))
    return Mesh(verts, tris)
