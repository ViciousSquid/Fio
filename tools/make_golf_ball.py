"""Build the Ball's model: ``assets/models/ball.obj``, ``ball.mtl`` and
``ball_texture.png``.

A low-poly golf ball 50 units across: a flat-shaded geodesic sphere (an
icosahedron with each edge split in two: 80 faces) whose 20 icosahedron faces
each show one triangle of a hexagonal dimple lattice, so the dimples cover the
ball evenly, with five-way dimples at the 12 corners as on a real golf ball.
Every face edge is a lattice line and each dimple looks the same all round,
so the dimples cut by an edge match on the next face: no seams, no pinched
poles.

usage: python tools/make_golf_ball.py [OUT_DIR]   (default assets/models)
"""
import math
import sys

import numpy as np
from PIL import Image

OUT = sys.argv[1] if len(sys.argv) > 1 else 'assets/models'
R = 25.0          # 50 units across: half the player's 100-unit height
N = 6             # dimples along an icosahedron edge -> 10*N^2 + 2 = 362 dimples
F = 2             # mesh subdivisions per icosahedron edge -> 20*F^2 = 80 triangles
TEX = 512

# ---------------------------------------------------------------- texture --
# Triangular lattice: points ((i + j/2) p, j h). The UV triangle every face
# uses is A=(0,0), B=(N p, 0), C=(N p/2, N h): three lattice points, so each
# face edge is a lattice line and the dimples cut by it match the next face.
p = TEX / N
h = p * math.sqrt(3) / 2
SS = 2                                    # supersample, then downsample
size = TEX * SS
yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
x = (xx + 0.5) / SS
y = (size - yy - 0.5) / SS                # UV v grows up the image
# Nearest lattice point.
j = np.round(y / h)
i = np.round(x / p - j / 2)
best = np.full(x.shape, np.inf)
for dj in (-1, 0, 1):
    for di in (-1, 0, 1):
        cj, ci = j + dj, i + di
        d = np.hypot(x - (ci + cj / 2) * p, y - cj * h)
        best = np.minimum(best, d)
r = best / (0.40 * p)                     # 1.0 at the dimple's rim

value = np.full(x.shape, 0.96)
inside = r < 1.0
# A shallow cup: darker toward its wall, a crisp shadowed lip, a soft lit
# floor -- the same all round, so it reads under any light and matches across
# face edges.
value[inside] = 0.84 + 0.07 * (1.0 - r[inside] ** 2)
lip = (r >= 0.86) & (r < 1.0)
value[lip] -= 0.10 * np.sin((r[lip] - 0.86) / 0.14 * math.pi)
rim = (r >= 1.0) & (r < 1.12)
value[rim] += 0.025 * np.sin((r[rim] - 1.0) / 0.12 * math.pi)
rng = np.random.default_rng(11)
for _ in range(1400):                     # faint speckle, as in the reference
    sx, sy = rng.integers(0, size, 2)
    value[sy:sy + 2, sx:sx + 2] -= rng.uniform(0.02, 0.05)
img = Image.fromarray(np.clip(value * 255.0, 0, 255).round().astype(np.uint8))
img = img.resize((TEX, TEX), Image.LANCZOS).convert('RGB')
img.save(f'{OUT}/ball_texture.png')

# ------------------------------------------------------------------ mesh ---
phi = (1 + math.sqrt(5)) / 2
ico = [(-1, phi, 0), (1, phi, 0), (-1, -phi, 0), (1, -phi, 0),
       (0, -1, phi), (0, 1, phi), (0, -1, -phi), (0, 1, -phi),
       (phi, 0, -1), (phi, 0, 1), (-phi, 0, -1), (-phi, 0, 1)]
ico = [np.array(v) / np.linalg.norm(v) for v in ico]
faces = [(0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
         (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
         (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
         (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1)]
uv_a, uv_b, uv_c = (np.array([0.0, 0.0]), np.array([1.0, 0.0]),
                    np.array([0.5, N * h / TEX]))

vs, vts, fs = [], [], []
for a, b, c in faces:
    A, B, C = ico[a], ico[b], ico[c]
    if np.dot(np.cross(B - A, C - A), A + B + C) < 0:   # outward winding
        B, C = C, B
    index = {}
    for s in range(F + 1):
        for t in range(F + 1 - s):
            u_, w_ = s / F, t / F            # weights of B and C
            point = A * (1 - u_ - w_) + B * u_ + C * w_
            index[(s, t)] = len(vs)
            vs.append(point / np.linalg.norm(point) * R)
            vts.append(uv_a * (1 - u_ - w_) + uv_b * u_ + uv_c * w_)
    for s in range(F):
        for t in range(F - s):
            fs.append((index[(s, t)], index[(s + 1, t)], index[(s, t + 1)]))
            if s + t < F - 1:
                fs.append((index[(s + 1, t)], index[(s + 1, t + 1)], index[(s, t + 1)]))

lines = [
    "# Fio golf ball: 50 units across (half the player's height), centred on",
    "# its origin so it rolls about its middle. A flat-shaded 80-face geodesic",
    "# sphere whose 20 icosahedron faces each show one triangle of a hexagonal",
    "# dimple lattice: 362 even dimples, five-way ones at the 12 corners.",
    "# fio:up=y",
    "mtllib ball.mtl",
    "o ball",
]
lines += ["v %.6f %.6f %.6f" % tuple(0.0 if abs(c) < 5e-7 else c for c in v) for v in vs]
lines += ["vt %.6f %.6f" % tuple(t) for t in vts]
# Flat shading: one normal per face, so each facet catches the light whole.
normals = []
for f in fs:
    a, b, c = (vs[k] for k in f)
    n = np.cross(b - a, c - a)
    normals.append(n / np.linalg.norm(n))
lines += ["vn %.6f %.6f %.6f" % tuple(n) for n in normals]
lines += ["usemtl ball", "s off"]
lines += ["f " + " ".join(f"{k + 1}/{k + 1}/{n + 1}" for k in f) for n, f in enumerate(fs)]
open(f'{OUT}/ball.obj', 'w').write("\n".join(lines) + "\n")
open(f'{OUT}/ball.mtl', 'w').write(
"""# Fio golf ball: a white dimpled texture, so a Prop's tint sets its colour.
newmtl ball
Ka 0.2 0.2 0.2
Kd 1.0 1.0 1.0
Ks 0.0 0.0 0.0
map_Kd ball_texture.png
""")
print(len(vs), 'vertices', len(fs), 'triangles')
