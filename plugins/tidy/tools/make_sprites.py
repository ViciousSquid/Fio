"""
Generate the Tidy plugin's editor sprites (2D-view icons).

Produces three distinct 64x64 RGBA icons under ``plugins/tidy/assets/`` so the
tidy entities read at a glance in the editor instead of borrowing core sprites:

    tidyobject.png       a book (the thing you pick up)
    tidyreceptacle.png   a bookshelf with coloured spines (where it goes)
    tidygoal.png         a checklist with a green tick (the objective)

Drawn at 4x and downsampled for clean edges. Requires Pillow.

Run from the repo root:  python plugins/tidy/tools/make_sprites.py
"""

import os

from PIL import Image, ImageDraw

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_ASSETS = os.path.join(_ROOT, "plugins", "tidy", "assets")

S = 4            # supersample factor
N = 64 * S       # working canvas size


def _canvas():
    img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)


def _save(img, name):
    os.makedirs(_ASSETS, exist_ok=True)
    out = os.path.join(_ASSETS, name)
    img.resize((64, 64), Image.LANCZOS).save(out)
    print(f"[make_sprites] wrote {os.path.relpath(out, _ROOT)}")


def _r(*box):
    """Scale an integer box/point list by the supersample factor."""
    return [v * S for v in box]


def make_object():
    """A warm-orange book with a spine, page block and title lines."""
    img, d = _canvas()
    cover = (214, 109, 38, 255)
    cover_dark = (120, 60, 18, 255)
    spine = (180, 86, 26, 255)
    pages = (240, 236, 220, 255)
    title = (247, 236, 214, 255)

    # Page block peeking out on the right (drawn first, sits behind the cover).
    d.rounded_rectangle(_r(44, 15, 51, 52), radius=S, fill=pages,
                        outline=(150, 140, 120, 255), width=S)
    for i in range(4):
        y = 20 + i * 8
        d.line(_r(46, y, 50, y), fill=(190, 180, 158, 255), width=S)

    # Cover.
    d.rounded_rectangle(_r(15, 11, 46, 53), radius=3 * S, fill=cover,
                        outline=cover_dark, width=2 * S)
    # Spine strip.
    d.rectangle(_r(15, 11, 23, 53), fill=spine)
    d.line(_r(23, 11, 23, 53), fill=cover_dark, width=S)
    # Title lines.
    d.line(_r(28, 24, 42, 24), fill=title, width=2 * S)
    d.line(_r(28, 31, 39, 31), fill=(247, 236, 214, 220), width=2 * S)

    _save(img, "tidyobject.png")


def make_receptacle():
    """A wooden bookshelf with coloured book spines on two shelves."""
    img, d = _canvas()
    wood = (150, 104, 51, 255)
    wood_dark = (94, 63, 29, 255)
    back = (58, 43, 26, 255)

    # Frame + back panel.
    d.rounded_rectangle(_r(9, 9, 55, 55), radius=3 * S, fill=back,
                        outline=wood_dark, width=3 * S)
    # Shelf boards (top, middle, bottom).
    for y in (10, 32, 53):
        d.rectangle(_r(9, y - 1, 55, y + 1), fill=wood)

    top_books = [(70, 130, 200), (200, 74, 74), (92, 182, 112), (223, 193, 84)]
    low_books = [(182, 122, 202), (120, 200, 200), (232, 152, 92), (150, 160, 210)]

    def row(books, y0, y1):
        x = 14
        for c in books:
            d.rounded_rectangle(_r(x, y0, x + 5, y1), radius=S, fill=c + (255,),
                                outline=(0, 0, 0, 60), width=1)
            x += 7

    row(top_books, 14, 31)
    row(low_books, 35, 52)

    _save(img, "tidyreceptacle.png")


def make_goal():
    """A clipboard checklist with a bold green tick."""
    img, d = _canvas()
    paper = (233, 233, 239, 255)
    edge = (92, 98, 108, 255)
    clip = (122, 130, 142, 255)
    lines = (150, 156, 166, 255)
    box = (120, 126, 138, 255)
    green = (46, 190, 92, 255)

    # Paper.
    d.rounded_rectangle(_r(14, 12, 50, 54), radius=3 * S, fill=paper,
                        outline=edge, width=2 * S)
    # Clip.
    d.rounded_rectangle(_r(26, 7, 38, 15), radius=2 * S, fill=clip,
                        outline=(78, 84, 94, 255), width=S)
    # List rows: little checkbox + line.
    for i in range(3):
        y = 25 + i * 8
        d.rounded_rectangle(_r(19, y - 2, 23, y + 2), radius=S, outline=box, width=S)
        d.line(_r(27, y, 44, y), fill=lines, width=2 * S)

    # Bold green tick over the list (the "done" mark), with a joined corner.
    d.line([(21 * S, 35 * S), (29 * S, 45 * S), (47 * S, 19 * S)],
           fill=green, width=4 * S, joint="curve")

    _save(img, "tidygoal.png")


def main():
    make_object()
    make_receptacle()
    make_goal()
    print("[make_sprites] done")


if __name__ == "__main__":
    main()
