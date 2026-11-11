"""Generate Fio's blood decal textures (assets/textures/decals/).

    python tools/make_blood_decals.py [--size 256] [--out assets/textures/decals]

Drawn procedurally (no source artwork), in two kinds, because blood looks
different on what it lands on:

* ``blood_wall_NN.png`` -- what a wall takes: impact splats with runs
  dripping down, fine spray, cast-off streaks. Row 0 is the top: on a wall
  the runs go downwards.
* ``blood_floor_NN.png`` -- what a floor takes: pools, scattered drops,
  splashes drawn out along their flight. No runs: nothing drips across a
  floor.

Alpha is coverage; the projected-decal pass stains the surface with the
colour, so the light on the surface makes the blood bright or dark.
Deterministic: the same files every run. Replace or add artwork of your
own: every ``blood_wall_*.png`` and ``blood_floor_*.png`` in the folder is
used, and the game picks among them at random.
"""

import argparse
import os

import numpy as np
from PIL import Image


def _disc(yy, xx, cy, cx, radius, soft=1.5):
    """Coverage of a soft-edged disc."""
    return np.clip((radius - np.hypot(yy - cy, xx - cx)) / soft + 0.5, 0.0, 1.0)


def _capsule(yy, xx, ay, ax, by, bx, radius, soft=1.5):
    """Coverage of a soft-edged line from a to b."""
    dy, dx = by - ay, bx - ax
    length_sq = max(dy * dy + dx * dx, 1e-6)
    t = np.clip(((yy - ay) * dy + (xx - ax) * dx) / length_sq, 0.0, 1.0)
    return np.clip((radius - np.hypot(yy - (ay + t * dy), xx - (ax + t * dx))) / soft + 0.5,
                   0.0, 1.0)


def _core(rng, yy, xx, size, cy, cx, radius, lobes):
    cover = _disc(yy, xx, cy, cx, radius)
    for _ in range(lobes):
        angle = rng.uniform(0, 2 * np.pi)
        reach = radius * rng.uniform(0.45, 0.95)
        cover = np.maximum(cover, _disc(yy, xx, cy + np.sin(angle) * reach,
                                        cx + np.cos(angle) * reach,
                                        radius * rng.uniform(0.3, 0.6)))
    return cover


def _droplets(rng, yy, xx, cy, cx, count, near, far, big, streak=0.0):
    """Droplets thrown outwards from (cy, cx): smaller the further they fly,
    drawn out along their flight when *streak* is set."""
    cover = np.zeros_like(yy)
    for _ in range(count):
        angle = rng.uniform(0, 2 * np.pi)
        t = rng.uniform(0.0, 1.0) ** 1.6
        dist = near + (far - near) * t
        radius = max(0.7, big * (1.0 - 0.8 * t) * rng.uniform(0.35, 1.0))
        y, x = cy + np.sin(angle) * dist, cx + np.cos(angle) * dist
        if streak > 0.0 and rng.uniform() < 0.6:
            tail = radius * streak * rng.uniform(1.0, 3.0)
            cover = np.maximum(cover, _capsule(yy, xx, y, x, y + np.sin(angle) * tail,
                                               x + np.cos(angle) * tail, radius * 0.75))
        cover = np.maximum(cover, _disc(yy, xx, y, x, radius))
    return cover


def _runs(rng, yy, xx, size, cy, cx, radius, count):
    """Runs dripping from the lower edge of a core, each ending in a bead."""
    cover = np.zeros_like(yy)
    for _ in range(count):
        x = cx + rng.uniform(-0.75, 0.75) * radius
        top = cy + radius * rng.uniform(0.2, 0.8)
        length = rng.uniform(0.12, 0.38) * size
        bottom = min(top + length, size - 6.0)
        width = rng.uniform(0.012, 0.022) * size
        cover = np.maximum(cover, _capsule(yy, xx, top, x, bottom, x, width))
        cover = np.maximum(cover, _disc(yy, xx, bottom, x, width * 1.5))
    return cover


def _streak(rng, yy, xx, size, count):
    """Cast-off: drops flung in a line, each drawn out along it."""
    cover = np.zeros_like(yy)
    angle = rng.uniform(-0.5, 0.5) + (np.pi if rng.uniform() < 0.5 else 0.0)
    dy, dx = np.sin(angle), np.cos(angle)
    for i in range(count):
        t = (i + rng.uniform(0.0, 1.0)) / count
        along = (t - 0.5) * size * 0.8
        across = rng.normal(0.0, size * 0.035)
        y, x = size / 2 + dy * along - dx * across, size / 2 + dx * along + dy * across
        radius = size * rng.uniform(0.006, 0.022) * (1.2 - t * 0.6)
        tail = radius * rng.uniform(1.5, 5.0)
        cover = np.maximum(cover, _capsule(yy, xx, y, x, y + dy * tail, x + dx * tail,
                                           radius * 0.7))
        cover = np.maximum(cover, _disc(yy, xx, y, x, radius))
    return cover


def _pool(rng, yy, xx, size):
    """A pool: a smooth irregular outline, with a few drops beside it."""
    mid = size / 2.0
    angle = np.arctan2(yy - mid, xx - mid)
    radius = size * rng.uniform(0.22, 0.30)
    edge = np.full_like(yy, radius)
    for k in range(2, 7):
        edge += radius * rng.uniform(0.03, 0.12) * np.sin(k * angle + rng.uniform(0, 2 * np.pi))
    cover = np.clip((edge - np.hypot(yy - mid, xx - mid)) / 2.0 + 0.5, 0.0, 1.0)
    return np.maximum(cover, _droplets(rng, yy, xx, mid, mid, 22, radius * 1.05,
                                       size * 0.46, size * 0.02))


def make(kind, variant, size):
    """One image: *kind* ``'wall'`` or ``'floor'``, *variant* from 0."""
    rng = np.random.default_rng((9000 if kind == 'wall' else 12000) + variant)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    mid = size / 2.0
    if kind == 'wall':
        shape = ('runs', 'spray', 'streak', 'runs', 'splat', 'spray', 'runs', 'streak')[variant % 8]
    else:
        shape = ('pool', 'drops', 'splash', 'pool', 'drops', 'splash')[variant % 6]
    if shape == 'runs':            # an impact, running down the wall
        cy = size * rng.uniform(0.30, 0.40)
        radius = size * rng.uniform(0.10, 0.17)
        cover = _core(rng, yy, xx, size, cy, mid, radius, int(rng.integers(5, 10)))
        cover = np.maximum(cover, _runs(rng, yy, xx, size, cy, mid, radius,
                                        int(rng.integers(2, 7))))
        cover = np.maximum(cover, _droplets(rng, yy, xx, cy, mid, 70, radius,
                                            size * 0.40, size * 0.024, streak=1.0))
    elif shape == 'splat':         # an impact thrown outwards
        radius = size * rng.uniform(0.11, 0.17)
        cover = _core(rng, yy, xx, size, mid, mid, radius, 9)
        cover = np.maximum(cover, _droplets(rng, yy, xx, mid, mid, 110, radius * 0.9,
                                            size * 0.47, size * 0.030, streak=1.2))
    elif shape == 'spray':         # fine spray, no core to speak of
        cy, cx = mid + size * rng.uniform(-0.1, 0.1), mid + size * rng.uniform(-0.1, 0.1)
        cover = _core(rng, yy, xx, size, cy, cx, size * rng.uniform(0.02, 0.05), 4)
        cover = np.maximum(cover, _droplets(rng, yy, xx, cy, cx, int(rng.integers(180, 340)),
                                            size * 0.02, size * 0.48, size * 0.018, streak=0.8))
    elif shape == 'streak':        # cast-off
        cover = _streak(rng, yy, xx, size, int(rng.integers(26, 48)))
    elif shape == 'pool':
        cover = _pool(rng, yy, xx, size)
    elif shape == 'drops':         # scattered round drops
        cover = _droplets(rng, yy, xx, mid, mid, int(rng.integers(25, 70)), size * 0.01,
                          size * 0.46, size * 0.034)
    else:                          # splash: a small core, drops drawn out from it
        radius = size * rng.uniform(0.07, 0.11)
        cover = _core(rng, yy, xx, size, mid, mid, radius, 6)
        cover = np.maximum(cover, _droplets(rng, yy, xx, mid, mid, 90, radius * 0.9,
                                            size * 0.47, size * 0.024, streak=1.6))
    # Nothing reaches the border: the decal's box clips there.
    border = np.minimum(np.minimum(yy, xx), np.minimum(size - 1 - yy, size - 1 - xx))
    cover *= np.clip(border / 4.0, 0.0, 1.0)
    # Thick blood is darker: a blurred copy of the coverage stands for depth.
    thick = cover.copy()
    for _ in range(6):
        thick = (thick + np.roll(thick, 1, 0) + np.roll(thick, -1, 0)
                 + np.roll(thick, 1, 1) + np.roll(thick, -1, 1)) / 5.0
    thin, deep = np.array([0.82, 0.05, 0.04]), np.array([0.55, 0.01, 0.01])
    rgb = thin + (deep - thin) * np.clip(thick * 1.3, 0.0, 1.0)[..., None]
    image = np.empty((size, size, 4), dtype=np.uint8)
    image[..., :3] = np.clip(rgb * 255.0 + 0.5, 0, 255).astype(np.uint8)
    image[..., 3] = np.clip(cover * 235.0 + 0.5, 0, 255).astype(np.uint8)
    return image


#: Images made of each kind.
COUNTS = {'wall': 8, 'floor': 6}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--size', type=int, default=256)
    ap.add_argument('--out', default=os.path.join('assets', 'textures', 'decals'))
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    for kind, count in COUNTS.items():
        for variant in range(count):
            path = os.path.join(args.out, 'blood_%s_%02d.png' % (kind, variant + 1))
            Image.fromarray(make(kind, variant, args.size)).save(path, optimize=True)
            print(path)


if __name__ == '__main__':
    main()
