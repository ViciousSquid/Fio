"""Build the Ball's models in ``assets/models``:

* ``ball.obj`` / ``ball.mtl`` / ``ball_texture.png`` -- the golf ball;
* ``ball_numbers.obj`` / ``ball_numbers.mtl`` / ``ball_numbers_texture.png``
  -- the same ball with a number on every face, like a large die.

A low-poly golf ball 50 units across: a flat-shaded geodesic sphere (an
icosahedron with each edge split in two: 80 faces) whose faces each show a
triangle of one hexagonal dimple lattice, so the dimples cover the ball evenly,
with five-way dimples at the 12 icosahedron corners as on a real golf ball.
Every face edge is a lattice line and each dimple looks the same all round,
so the dimples cut by an edge match on the next face: no seams, no pinched
poles.

The plain ball maps each icosahedron face onto one lattice triangle. The
numbered ball gives each of the 80 faces its own lattice triangle in an atlas,
with its number printed in the middle. As on a real die, opposite faces add up
to 81, and a number that reads as another one upside down is underlined.

usage: python tools/make_golf_ball.py [OUT_DIR]   (default assets/models)
"""
import math
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

OUT = sys.argv[1] if len(sys.argv) > 1 else 'assets/models'
R = 25.0          # 50 units across: half the player's 100-unit height
N = 6             # dimples along an icosahedron edge -> 10*N^2 + 2 = 362 dimples
F = 2             # mesh subdivisions per icosahedron edge -> 20*F^2 = 80 faces
SS = 2            # textures are drawn at twice the size, then downsampled
SQRT3_2 = math.sqrt(3) / 2


# ---------------------------------------------------------------- texture --

def dimple_lattice(width, height, p, seed):
    """Greyscale (0..1) dimples on the triangular lattice ((i + j/2) p, j h).

    Drawn *SS* times over. Coordinates run from the bottom-left corner, as
    UVs do (v grows up the image).
    """
    h = p * SQRT3_2
    w, ht = width * SS, height * SS
    yy, xx = np.mgrid[0:ht, 0:w].astype(np.float64)
    x = (xx + 0.5) / SS
    y = (ht - yy - 0.5) / SS
    j = np.round(y / h)
    i = np.round(x / p - j / 2)
    best = np.full(x.shape, np.inf)
    for dj in (-1, 0, 1):
        for di in (-1, 0, 1):
            cj, ci = j + dj, i + di
            best = np.minimum(best, np.hypot(x - (ci + cj / 2) * p, y - cj * h))
    r = best / (0.40 * p)                     # 1.0 at the dimple's rim

    value = np.full(x.shape, 0.96)
    inside = r < 1.0
    # A shallow cup: darker toward its wall, a crisp shadowed lip, a soft lit
    # floor -- the same all round, so it reads under any light and matches
    # across face edges.
    value[inside] = 0.84 + 0.07 * (1.0 - r[inside] ** 2)
    lip = (r >= 0.86) & (r < 1.0)
    value[lip] -= 0.10 * np.sin((r[lip] - 0.86) / 0.14 * math.pi)
    rim = (r >= 1.0) & (r < 1.12)
    value[rim] += 0.025 * np.sin((r[rim] - 1.0) / 0.12 * math.pi)
    rng = np.random.default_rng(seed)
    for _ in range(int(1400 * (width * height) / (512 * 512))):   # faint speckle
        sx, sy = rng.integers(0, w), rng.integers(0, ht)
        value[sy:sy + 2, sx:sx + 2] -= rng.uniform(0.02, 0.05)
    return value


def to_image(value):
    return Image.fromarray(np.clip(value * 255.0, 0, 255).round().astype(np.uint8))


def save_texture(image, width, height, name):
    image.resize((width, height), Image.LANCZOS).convert('RGB').save(f'{OUT}/{name}')


# ------------------------------------------------------------------ mesh ---

def geodesic_faces():
    """The 80 faces as (corners, plain-ball UVs), wound outward."""
    phi = (1 + math.sqrt(5)) / 2
    ico = [(-1, phi, 0), (1, phi, 0), (-1, -phi, 0), (1, -phi, 0),
           (0, -1, phi), (0, 1, phi), (0, -1, -phi), (0, 1, -phi),
           (phi, 0, -1), (phi, 0, 1), (-phi, 0, -1), (-phi, 0, 1)]
    ico = [np.array(v) / np.linalg.norm(v) for v in ico]
    faces = [(0, 11, 5), (0, 5, 1), (0, 1, 7), (0, 7, 10), (0, 10, 11),
             (1, 5, 9), (5, 11, 4), (11, 10, 2), (10, 7, 6), (7, 1, 8),
             (3, 9, 4), (3, 4, 2), (3, 2, 6), (3, 6, 8), (3, 8, 9),
             (4, 9, 5), (2, 4, 11), (6, 2, 10), (8, 6, 7), (9, 8, 1)]
    # The plain ball's lattice triangle, in UVs of its 512 x 512 texture.
    uv_a, uv_b, uv_c = (np.array([0.0, 0.0]), np.array([1.0, 0.0]),
                        np.array([0.5, SQRT3_2]))
    out = []
    for a, b, c in faces:
        A, B, C = ico[a], ico[b], ico[c]
        if np.dot(np.cross(B - A, C - A), A + B + C) < 0:   # outward winding
            B, C = C, B

        def corner(s, t):
            u_, w_ = s / F, t / F                # weights of B and C
            point = A * (1 - u_ - w_) + B * u_ + C * w_
            return (point / np.linalg.norm(point) * R,
                    uv_a * (1 - u_ - w_) + uv_b * u_ + uv_c * w_)

        for s in range(F):
            for t in range(F - s):
                out.append(tuple(zip(*(corner(s, t), corner(s + 1, t), corner(s, t + 1)))))
                if s + t < F - 1:
                    out.append(tuple(zip(*(corner(s + 1, t), corner(s + 1, t + 1),
                                           corner(s, t + 1)))))
    return [(np.array(points), np.array(uvs)) for points, uvs in out]


def write_obj(name, header, faces):
    """*faces*: (corners, uvs) per face. Flat-shaded: one normal per face."""
    lines = header + ["# fio:up=y", f"mtllib {name}.mtl", "o ball"]
    for points, _ in faces:
        lines += ["v %.6f %.6f %.6f" % tuple(0.0 if abs(c) < 5e-7 else c for c in v)
                  for v in points]
    for _, uvs in faces:
        lines += ["vt %.6f %.6f" % tuple(t) for t in uvs]
    for points, _ in faces:
        n = np.cross(points[1] - points[0], points[2] - points[0])
        lines.append("vn %.6f %.6f %.6f" % tuple(n / np.linalg.norm(n)))
    lines += ["usemtl ball", "s off"]
    for k in range(len(faces)):
        lines.append("f " + " ".join(f"{3 * k + c + 1}/{3 * k + c + 1}/{k + 1}"
                                     for c in range(3)))
    open(f'{OUT}/{name}.obj', 'w').write("\n".join(lines) + "\n")
    open(f'{OUT}/{name}.mtl', 'w').write(
        "# Fio golf ball: a white texture, so a Prop's tint sets its colour.\n"
        "newmtl ball\nKa 0.2 0.2 0.2\nKd 1.0 1.0 1.0\nKs 0.0 0.0 0.0\n"
        f"map_Kd {name}_texture.png\n")


FACES = geodesic_faces()
HEADER = [
    "# Fio golf ball: 50 units across (half the player's height), centred on",
    "# its origin so it rolls about its middle. A flat-shaded 80-face geodesic",
    "# sphere whose faces show a hexagonal dimple lattice: 362 even dimples,",
    "# five-way ones at the 12 icosahedron corners.",
]

# -- the plain ball: every icosahedron face is the same lattice triangle -----
PLAIN = 512
save_texture(to_image(dimple_lattice(PLAIN, PLAIN, PLAIN / N, seed=11)),
             PLAIN, PLAIN, 'ball_texture.png')
write_obj('ball', HEADER, FACES)

# -- the numbered ball: each face its own lattice triangle in an atlas -------
# A face is a lattice triangle of side N / F dimples. Rows of upright and
# inverted triangles, odd rows shifted half a side so every corner stays on a
# lattice point.
P = 50.0                                  # atlas pixels between dimples
SIDE = P * N / F                          # a face's side, in atlas pixels
ROW = SIDE * SQRT3_2
PER_ROW = 8                               # upright faces per row
cells = []
for row in range(6):
    x0, y0 = (row % 2) * SIDE / 2, row * ROW
    for i in range(PER_ROW):
        left = x0 + i * SIDE
        cells.append(((left, y0), (left + SIDE, y0), (left + SIDE / 2, y0 + ROW)))
        if i < PER_ROW - 1:
            cells.append(((left + SIDE, y0), (left + 1.5 * SIDE, y0 + ROW),
                          (left + SIDE / 2, y0 + ROW)))
cells = cells[:len(FACES)]
ATLAS_W, ATLAS_H = 1280, 784
assert PER_ROW * SIDE + SIDE / 2 <= ATLAS_W and 6 * ROW <= ATLAS_H

# Number the faces as a die is numbered: opposite faces add up to 81.
centres = np.array([points.mean(axis=0) for points, _ in FACES])
order = sorted(range(len(FACES)), key=lambda k: (-round(centres[k][1], 3),
                                                 math.atan2(centres[k][2], centres[k][0])))
numbers = {}
for k in order:
    if k in numbers:
        continue
    opposite = int(np.argmin(np.linalg.norm(centres + centres[k], axis=1)))
    number = len(numbers) // 2 + 1
    numbers[k] = number
    numbers[opposite] = len(FACES) + 1 - number

FLIP = {'0': '0', '1': '1', '6': '9', '8': '8', '9': '6'}


def needs_underline(number):
    """Whether *number* upside down reads as another face's number."""
    text = str(number)
    if any(c not in FLIP for c in text):
        return False
    turned = ''.join(FLIP[c] for c in reversed(text))
    # "01" upside down is no face's number; only a real one confuses.
    return (not turned.startswith('0') and int(turned) != number
            and 1 <= int(turned) <= len(FACES))


atlas = to_image(dimple_lattice(ATLAS_W, ATLAS_H, P, seed=12))
font = ImageFont.load_default(size=int(SIDE * 0.34 * SS))
ink = 38
for k, cell in enumerate(cells):
    text = str(numbers[k])
    stamp = Image.new('L', (int(SIDE * SS), int(SIDE * SS)), 0)
    draw = ImageDraw.Draw(stamp)
    cx, cy = stamp.width / 2, stamp.height / 2
    draw.text((cx, cy), text, fill=255, font=font, anchor='mm',
              stroke_width=max(1, SS), stroke_fill=255)
    if needs_underline(numbers[k]):
        box = draw.textbbox((cx, cy), text, font=font, anchor='mm')
        bar = (box[3] - box[1]) * 0.12
        draw.rectangle((box[0], box[3] + bar, box[2], box[3] + 2.2 * bar), fill=255)
    ys = [c[1] for c in cell]
    # An upright face has its apex on top; an inverted one, at the bottom.
    upright = sum(ys) / 3 < (min(ys) + max(ys)) / 2
    if not upright:
        stamp = stamp.rotate(180)
    # Centre on the face's centre, in image rows (which run down the page).
    centre_x = sum(c[0] for c in cell) / 3 * SS
    centre_y = (ATLAS_H - sum(c[1] for c in cell) / 3) * SS
    atlas.paste(ink, (int(centre_x - stamp.width / 2), int(centre_y - stamp.height / 2)),
                stamp)
save_texture(atlas, ATLAS_W, ATLAS_H, 'ball_numbers_texture.png')

numbered = [(points, np.array([[c[0] / ATLAS_W, c[1] / ATLAS_H] for c in cell]))
            for (points, _), cell in zip(FACES, cells)]
write_obj('ball_numbers', HEADER[:-1] + [
    "# five-way ones at the 12 icosahedron corners. Each face is numbered,",
    "# 1 to 80, opposite faces adding up to 81, like a large die.",
], numbered)
print(len(FACES), 'faces; numbered', sorted(numbers.values()) == list(range(1, 81)))
