"""
Generate the tidy 'book' model and a set of random cover textures.

Produces, under ``plugins/tidy/assets/``:

    book.obj / book.mtl          a UV-mapped book (front/back = cover region,
                                 edges = page region of the texture)
    covers/cover_NN.png          N distinct book covers (art + cream page edge)

Each :class:`TidyObject` picks a random cover at creation and renders it via the
engine's per-instance ``texture`` override, so a shelf of books shows many
different covers instead of identical grey boxes.

The book's UVs map the front and back faces to the left ~72% of the texture (the
cover art) and every edge face to a cream strip on the right (the pages), so one
image per book gives a proper cover + visible page edges.

Run from the repo root:  python plugins/tidy/tools/make_books.py
"""

import math
import os
import random

from PIL import Image, ImageDraw

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_ASSETS = os.path.join(_ROOT, "plugins", "tidy", "assets")
_COVERS = os.path.join(_ASSETS, "covers")

N_COVERS = 12
SEED = 20240607  # stable output across runs

# Cover palettes: (background, accent/frame, ink) — classic book-cover tones.
PALETTES = [
    ((122, 32, 34), (222, 190, 120), (245, 232, 205)),   # deep red / gold
    ((28, 54, 92), (200, 210, 230), (235, 240, 250)),    # navy / silver
    ((34, 82, 54), (214, 196, 128), (240, 236, 214)),    # forest / gold
    ((92, 58, 28), (222, 200, 150), (244, 232, 208)),    # brown / tan
    ((60, 40, 96), (214, 196, 232), (240, 232, 248)),    # purple / lilac
    ((26, 82, 92), (210, 224, 224), (238, 246, 246)),    # teal / pale
    ((150, 92, 24), (60, 42, 20), (250, 240, 220)),      # mustard / dark
    ((96, 26, 60), (226, 190, 208), (246, 232, 240)),    # burgundy / rose
    ((44, 66, 40), (200, 176, 96), (236, 232, 210)),     # olive / brass
    ((32, 40, 52), (188, 150, 92), (236, 228, 212)),     # slate / bronze
    ((170, 78, 34), (54, 34, 22), (250, 238, 222)),      # burnt orange / dark
    ((40, 88, 120), (216, 226, 234), (240, 246, 250)),   # blue / ice
]

PAGE = (238, 231, 210)        # cream page colour
PAGE_LINE = (206, 196, 172)   # faint page lines

SS = 2                        # supersample
W = 256 * SS
H = 256 * SS
COVER_X = int(190 * SS)       # cover art occupies x < COVER_X; pages to the right


def _cover(idx: int, rng: random.Random) -> Image.Image:
    bg, accent, ink = PALETTES[idx % len(PALETTES)]
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Page strip (right side) — cream with faint horizontal lines.
    d.rectangle([COVER_X, 0, W, H], fill=PAGE + (255,))
    y = 6 * SS
    while y < H:
        d.line([COVER_X + 3 * SS, y, W - 2 * SS, y], fill=PAGE_LINE + (255,), width=SS)
        y += 9 * SS

    # Cover background.
    d.rectangle([0, 0, COVER_X, H], fill=bg + (255,))
    # Inset frame.
    inset = 10 * SS
    d.rectangle([inset, inset, COVER_X - inset, H - inset],
                outline=accent + (255,), width=2 * SS)

    # Title band with two "text" bars.
    tb_x0, tb_x1 = inset + 6 * SS, COVER_X - inset - 6 * SS
    tb_y0 = int(H * 0.16)
    tb_y1 = int(H * 0.40)
    d.rounded_rectangle([tb_x0, tb_y0, tb_x1, tb_y1], radius=6 * SS,
                        fill=accent + (255,))
    bar_w = tb_x1 - tb_x0
    d.rectangle([tb_x0 + int(bar_w * 0.12), tb_y0 + 7 * SS,
                 tb_x0 + int(bar_w * 0.88), tb_y0 + 13 * SS], fill=bg + (255,))
    d.rectangle([tb_x0 + int(bar_w * 0.22), tb_y0 + 20 * SS,
                 tb_x0 + int(bar_w * 0.78), tb_y0 + 26 * SS], fill=bg + (255,))

    # Central emblem (varies the look between covers).
    cx, cy = COVER_X // 2, int(H * 0.60)
    r = int(min(COVER_X, H) * 0.12)
    shape = idx % 3
    if shape == 0:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=accent + (255,), width=3 * SS)
        d.ellipse([cx - r // 2, cy - r // 2, cx + r // 2, cy + r // 2], fill=accent + (255,))
    elif shape == 1:
        d.polygon([(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)],
                  outline=accent + (255,), width=3 * SS)
    else:
        d.line([(cx - r, cy - r), (cx + r, cy + r)], fill=accent + (255,), width=3 * SS)
        d.line([(cx - r, cy + r), (cx + r, cy - r)], fill=accent + (255,), width=3 * SS)

    # Author bar near the bottom.
    ab_y = int(H * 0.82)
    d.rectangle([tb_x0 + int(bar_w * 0.20), ab_y,
                 tb_x0 + int(bar_w * 0.80), ab_y + 8 * SS], fill=ink + (255,))

    return img.resize((256, 256), Image.LANCZOS)


def make_covers():
    os.makedirs(_COVERS, exist_ok=True)
    rng = random.Random(SEED)
    for i in range(1, N_COVERS + 1):
        _cover(i - 1, rng).save(os.path.join(_COVERS, f"cover_{i:02d}.png"))
    print(f"[make_books] wrote {N_COVERS} covers -> "
          f"{os.path.relpath(_COVERS, _ROOT)}/cover_01..{N_COVERS:02d}.png")


# --- book geometry (UV-mapped) ---------------------------------------------
def make_model():
    hx, hy, hz = 7.0, 10.0, 2.5
    v = [
        (-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),  # back  0-3
        (-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz),      # front 4-7
    ]
    # UVs: 1-4 cover region (left), 5-8 page region (right strip).
    vt = [
        (0.02, 0.03), (0.70, 0.03), (0.70, 0.97), (0.02, 0.97),   # cover
        (0.80, 0.05), (0.96, 0.05), (0.96, 0.95), (0.80, 0.95),   # pages
    ]
    vn = [(0, 0, 1), (0, 0, -1), (-1, 0, 0), (1, 0, 0), (0, 1, 0), (0, -1, 0)]

    # (v_index0, uv_index1based) quads with a normal; split into two tris.
    faces = [
        # front (cover)
        ([(4, 1), (5, 2), (6, 3), (7, 4)], 1),
        # back (cover)
        ([(0, 1), (1, 2), (2, 3), (3, 4)], 2),
        # left / spine (pages)
        ([(0, 5), (3, 8), (7, 7), (4, 6)], 3),
        # right / fore-edge (pages)
        ([(1, 5), (5, 6), (6, 7), (2, 8)], 4),
        # top (pages)
        ([(3, 5), (2, 6), (6, 7), (7, 8)], 5),
        # bottom (pages)
        ([(0, 5), (4, 6), (5, 7), (1, 8)], 6),
    ]

    lines = [
        "# Tidy plugin book model (UV-mapped: cover on front/back, pages on edges).",
        "# Per-instance cover comes from the entity's 'texture' property.",
        "mtllib book.mtl",
        "o tidy_book",
    ]
    for x, y, z in v:
        lines.append(f"v {x:.3f} {y:.3f} {z:.3f}")
    for u, w in vt:
        lines.append(f"vt {u:.4f} {w:.4f}")
    for x, y, z in vn:
        lines.append(f"vn {x:.1f} {y:.1f} {z:.1f}")
    lines.append("usemtl cover")
    for quad, n in faces:
        (a, ua), (b, ub), (c, uc), (dd, ud) = quad
        def f(vi, ui):
            return f"{vi + 1}/{ui}/{n}"
        lines.append(f"f {f(a, ua)} {f(b, ub)} {f(c, uc)}")
        lines.append(f"f {f(a, ua)} {f(c, uc)} {f(dd, ud)}")

    with open(os.path.join(_ASSETS, "book.obj"), "w") as fh:
        fh.write("\n".join(lines) + "\n")

    # A default material (cream) — only used if an instance has no cover texture.
    with open(os.path.join(_ASSETS, "book.mtl"), "w") as fh:
        fh.write("newmtl cover\nKa 0.20 0.19 0.17\nKd 0.86 0.83 0.74\n"
                 "Ks 0.03 0.03 0.03\nNs 6.0\nd 1.0\n")

    print("[make_books] wrote book.obj + book.mtl")


def main():
    make_model()
    make_covers()
    print("[make_books] done")


if __name__ == "__main__":
    main()
