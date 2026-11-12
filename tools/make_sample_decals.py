"""Generate Fio's sample decals (assets/textures/decals/).

    python tools/make_sample_decals.py [--force]

Existing files are kept (they may have been replaced with real artwork)
unless ``--force`` is given.

Two images and two wet masks, placed with the Decal tool:

* ``grime_01.png``     -- a dark, grimy stain (an Image decal; mid grey is
  "no change", so the dark parts darken the surface).
* ``scorch_01.png``    -- a burn mark, black at the middle.
* ``wet_puddle_01.png`` -- blotchy puddle clusters: a Wet decal mask. The
  strongest (most opaque) parts become standing water, the edges damp.
* ``wet_damp_01.png``  -- a streaky, patchy damp area: a Wet decal mask.
* ``wet_full.png``     -- solid, softened only at the very edge: scale a Wet
  decal with it over a whole floor or wall to make all of it wet and glossy.

Deterministic: the same files every run.
"""

from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'assets' / 'textures' / 'decals'
SIZE = 256


def value_noise(rng, size, cells):
    """Smooth noise in 0..1: a random lattice of *cells*, bicubically smoothed."""
    lattice = rng.random((cells + 3, cells + 3))
    t = np.linspace(0.0, cells, size, endpoint=False)
    i = t.astype(int)
    f = t - i
    f = f * f * (3.0 - 2.0 * f)
    rows = lattice[i][:, None, :] * (1 - f)[:, None, None] + lattice[i + 1][:, None, :] * f[:, None, None]
    rows = rows[:, 0, :]
    out = rows[:, i] * (1 - f)[None, :] + rows[:, i + 1] * f[None, :]
    return out


def fbm(rng, size, octaves=5, base=4):
    total = np.zeros((size, size))
    amplitude, weight = 1.0, 0.0
    for octave in range(octaves):
        total += value_noise(rng, size, base * 2 ** octave) * amplitude
        weight += amplitude
        amplitude *= 0.5
    return total / weight


def radial(size, cx=0.5, cy=0.5, rx=0.5, ry=0.5):
    y, x = np.mgrid[0:size, 0:size] / (size - 1)
    return np.sqrt(((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2)


def save(name, rgb, alpha):
    rgba = np.dstack([np.clip(rgb, 0, 1), np.clip(alpha, 0, 1)])
    Image.fromarray((rgba * 255).round().astype(np.uint8)).save(OUT / name, optimize=True)


def grime(rng):
    shape = fbm(rng, SIZE, 5, 3)
    edge = 1.0 - radial(SIZE, rx=0.48, ry=0.44)
    alpha = np.clip((shape * 0.9 + edge * 1.1 - 0.62) * 3.0, 0, 1)
    tone = 0.18 + 0.22 * fbm(rng, SIZE, 4, 8)          # well under mid grey: darkens
    rgb = np.dstack([tone * 1.05, tone * 0.95, tone * 0.8])
    return rgb, alpha * 0.9


def scorch(rng):
    d = radial(SIZE, rx=0.46, ry=0.46)
    ragged = d + (fbm(rng, SIZE, 5, 6) - 0.5) * 0.45
    alpha = np.clip((1.0 - ragged) * 2.2, 0, 1)
    core = np.clip(1.0 - d * 1.6, 0, 1)
    tone = 0.05 + 0.3 * (1.0 - core) * fbm(rng, SIZE, 3, 10)
    rgb = np.dstack([tone * 1.1, tone * 0.85, tone * 0.7])
    return rgb, alpha


#: The wet masks' colour: a dark, wet-looking grey-blue. Placed as Wet
#: decals only their alpha is read; placed as Images they still darken the
#: surface visibly rather than leaving it as it was (mid grey would).
WET_TINT = np.array((0.34, 0.38, 0.44))


def puddles(rng):
    """Clusters of rounded blobs, solid in the middle: standing water."""
    field = np.zeros((SIZE, SIZE))
    y, x = np.mgrid[0:SIZE, 0:SIZE] / (SIZE - 1)
    for _ in range(26):
        cx, cy = rng.uniform(0.12, 0.88, 2)
        r = rng.uniform(0.03, 0.11)
        field = np.maximum(field, 1.0 - np.sqrt((x - cx) ** 2 + (y - cy) ** 2) / r)
    # Ragged, irregular edges rather than discs.
    warp = fbm(rng, SIZE, 5, 5) - 0.5
    field += warp * 1.3 + (fbm(rng, SIZE, 4, 14) - 0.5) * 0.5
    edge = np.clip((1.0 - radial(SIZE, rx=0.5, ry=0.5)) * 3.0, 0, 1)
    alpha = np.clip(field * 2.2, 0, 1) * edge
    return np.ones((SIZE, SIZE, 3)) * WET_TINT, alpha


def damp(rng):
    """A broad, streaky damp patch: mostly damp, a few wetter streaks."""
    y, x = np.mgrid[0:SIZE, 0:SIZE] / (SIZE - 1)
    streaks = fbm(rng, SIZE, 5, 4)
    stretched = value_noise(rng, SIZE, 24)[:, ::1] * 0.5 + 0.5 * np.roll(streaks, 7, axis=1)
    body = 1.0 - radial(SIZE, rx=0.47, ry=0.42)
    alpha = np.clip((streaks * 0.8 + stretched * 0.4 + body * 1.2 - 1.05) * 2.4, 0, 1)
    return np.ones((SIZE, SIZE, 3)) * WET_TINT, alpha * 0.85


def full(_rng):
    """Solid wetness to the edges (a 4-pixel soft rim): a whole surface."""
    y, x = np.mgrid[0:SIZE, 0:SIZE]
    rim = np.minimum.reduce([x, y, SIZE - 1 - x, SIZE - 1 - y]).astype(float)
    return np.ones((SIZE, SIZE, 3)) * WET_TINT, np.clip(rim / 4.0, 0, 1)


def build(force=False):
    OUT.mkdir(parents=True, exist_ok=True)
    for name, maker, seed in (('grime_01.png', grime, 11), ('scorch_01.png', scorch, 23),
                              ('wet_puddle_01.png', puddles, 37), ('wet_damp_01.png', damp, 41),
                              ('wet_full.png', full, 0)):
        if (OUT / name).exists() and not force:
            # Never over an image someone has replaced with their own.
            print('kept', OUT / name, '(exists; --force to regenerate)')
            continue
        save(name, *maker(np.random.default_rng(seed)))
        print('wrote', OUT / name)


if __name__ == '__main__':
    import sys
    build(force='--force' in sys.argv[1:])
