"""
Align the overhead (top-down) player sprite frames so they animate seamlessly.

The Overhead camera plays the player frames as a flip-book: idle (`player1`),
walk (`player2`/`player3`), the weapon-held variants (`player1_g`/`2_g`/`3_g`)
and firing (`player_shoot_g`). For the character not to jump around between
frames, every frame must be the **same canvas size** with the body **registered
to the same spot**. Exported art rarely satisfies that (each frame is a
different size with the character at a different offset), so this tool fixes it:

1. **Background → transparent** (default): art on a flat light background gets
   that background knocked out so trimming/centreing see the real silhouette.
   `--bg white` forces near-white removal; `--bg none` keeps existing alpha.
2. **Trim** each frame to the bounding box of its visible pixels.
3. **Common canvas** = the largest trimmed width/height across *all* the frames
   you pass (+ `--pad`), so process the whole set together in one run.
4. **Centre** each frame's content on that canvas (`--anchor center`, best for a
   top-down sprite that turns in place; `bottom`/`top`/`left`/`right` pin an edge).

Run it over the whole set at once (from the repo root), so the new weapon-held
frames end up registered to exactly the same canvas as the existing ones:

    python -m tools.align_topdown_sprites --dir assets/sprites/topdown --in-place

or explicitly, keeping originals (writes to assets/sprites/topdown/aligned/):

    python -m tools.align_topdown_sprites assets/sprites/topdown/player*.png

Requires Pillow (`pip install Pillow`).
"""

from __future__ import annotations

import argparse
import glob
import os
import sys
from typing import List, Optional, Tuple

try:
    from PIL import Image
except Exception:  # pragma: no cover - import guard
    Image = None


def _knock_out_background(img: "Image.Image", mode: str, tolerance: int) -> "Image.Image":
    """Return an RGBA copy with a flat background made transparent."""
    img = img.convert("RGBA")
    if mode == "none":
        return img
    px = img.load()
    w, h = img.size
    if mode == "white":
        bg = (255, 255, 255)
    else:  # auto: the most common of the four corner colours
        corners = [px[0, 0][:3], px[w - 1, 0][:3], px[0, h - 1][:3], px[w - 1, h - 1][:3]]
        bg = max(set(corners), key=corners.count)
    tol2 = tolerance * tolerance
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a == 0:
                continue
            dr, dg, db = r - bg[0], g - bg[1], b - bg[2]
            if dr * dr + dg * dg + db * db <= tol2:
                px[x, y] = (r, g, b, 0)
    return img


def _anchor_offset(canvas: Tuple[int, int], content: Tuple[int, int], anchor: str) -> Tuple[int, int]:
    cw, ch = canvas
    w, h = content
    x = (cw - w) // 2
    y = (ch - h) // 2
    if anchor == "bottom":
        y = ch - h
    elif anchor == "top":
        y = 0
    elif anchor == "left":
        x = 0
    elif anchor == "right":
        x = cw - w
    return x, y


def align_frames(paths: List[str], out_dir: Optional[str] = None, in_place: bool = False,
                 bg: str = "auto", tolerance: int = 24, anchor: str = "center",
                 pad: int = 2, size: Optional[Tuple[int, int]] = None, log=print) -> List[str]:
    """Align *paths* to a common size + registration. Returns output paths."""
    if Image is None:
        raise RuntimeError("Pillow is required: pip install Pillow")
    if not paths:
        raise RuntimeError("no input frames given")

    trimmed = []
    for p in paths:
        try:
            img = _knock_out_background(Image.open(p), bg, tolerance)
        except Exception as exc:
            log(f"  skip {p}: {exc}")
            continue
        bbox = img.getbbox()
        cropped = img.crop(bbox) if bbox else img
        trimmed.append((p, cropped))
        log(f"  {os.path.basename(p)}: content {cropped.size}")
    if not trimmed:
        raise RuntimeError("no readable image frames")

    if size is not None:
        cw, ch = size
    else:
        cw = max(im.size[0] for _, im in trimmed) + pad * 2
        ch = max(im.size[1] for _, im in trimmed) + pad * 2
    log(f"  canvas: {cw}x{ch} (anchor={anchor}, pad={pad})")

    outputs = []
    for p, im in trimmed:
        canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        ox, oy = _anchor_offset((cw, ch), im.size, anchor)
        canvas.alpha_composite(im, (max(0, ox), max(0, oy)))
        if in_place:
            out = p
        else:
            d = out_dir or os.path.join(os.path.dirname(p) or ".", "aligned")
            os.makedirs(d, exist_ok=True)
            out = os.path.join(d, os.path.splitext(os.path.basename(p))[0] + ".png")
        canvas.save(out)
        outputs.append(out)
        log(f"  -> {out}")
    return outputs


def _parse_size(s: Optional[str]) -> Optional[Tuple[int, int]]:
    if not s:
        return None
    a, b = s.lower().split("x")
    return int(a), int(b)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Align top-down player sprite frames to a common size + registration.")
    ap.add_argument("frames", nargs="*", help="frame image paths (or use --dir)")
    ap.add_argument("--dir", help="directory of frames (all *.png). Default: assets/sprites/topdown")
    ap.add_argument("--out", help="output directory (default: <frames>/aligned)")
    ap.add_argument("--in-place", action="store_true", help="overwrite the input files")
    ap.add_argument("--bg", choices=("auto", "white", "none"), default="auto",
                    help="flat-background removal (default: auto)")
    ap.add_argument("--tolerance", type=int, default=24, help="background colour tolerance (0-255)")
    ap.add_argument("--anchor", choices=("center", "bottom", "top", "left", "right"),
                    default="center", help="how to register each frame (default: center)")
    ap.add_argument("--pad", type=int, default=2, help="padding around content (px)")
    ap.add_argument("--size", help="force canvas size, e.g. 128x128")
    args = ap.parse_args(argv)

    paths = list(args.frames)
    scan_dir = args.dir
    if not paths and scan_dir is None:
        scan_dir = os.path.join("assets", "sprites", "topdown")
    if scan_dir:
        for ext in ("*.png", "*.webp", "*.jpg", "*.jpeg"):
            paths.extend(sorted(glob.glob(os.path.join(scan_dir, ext))))
    paths = sorted(dict.fromkeys(paths))
    if not paths:
        ap.error("no frames found (pass paths or --dir)")

    try:
        outs = align_frames(paths, out_dir=args.out, in_place=args.in_place, bg=args.bg,
                            tolerance=args.tolerance, anchor=args.anchor, pad=args.pad,
                            size=_parse_size(args.size))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"aligned {len(outs)} frame(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
