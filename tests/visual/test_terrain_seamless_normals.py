"""Visual tier: the smooth normal is continuous across chunk borders.

The CPU mesh took np.gradient's one-sided differences at a chunk's edge, so
the smooth normal kinked at every chunk border -- a few degrees on gentle
ground, and up to ~70 degrees where a ridge crosses a border on Low Poly
Valley (the default biome) and over 100 on Jagged Peaks. The smooth normal
drives the grass/rock texture blend, so a big kink flipped the blend along
the border line.

Each chunk now stores a one-sample border around its height grid and the
vertex shader takes central differences everywhere, so two chunks sharing an
edge compute the same smooth normal on it. Checked on the GPU's own output.
"""

import numpy as np
import pytest

from tests.helpers import gl as glh
from tests.visual.test_terrain_heightfield_parity import Capture, build

pytestmark = [pytest.mark.gl, pytest.mark.slow]


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(64, 64) as ctx:
        yield ctx
    glh.reset_texture_cache()


def edge_normals(t, slot, got, edge_x):
    """``{z: smooth normal}`` for the chunk's vertices on the line x = edge_x."""
    on_edge = np.abs(got[:, 0] - edge_x) < 1e-2
    out = {}
    for z, sn in zip(got[on_edge, 2], got[on_edge, 11:14]):
        out[round(float(z), 2)] = sn
    return out


@pytest.mark.parametrize("biome,a,b", [
    ("low_poly_valley", (2, -2), (3, -2)),    # the ~70 degree ridge crossing
    ("jagged_peaks", (0, 0), (1, 0)),
    ("dark_cliffs", (-2, 1), (-1, 1)),
])
def test_neighbouring_chunks_agree_on_their_shared_edge(context, biome, a, b):
    from engine.terrain import Terrain
    t = Terrain()
    t.set_biome(biome)
    t.sculpt_offsets = {}
    t._touch_sculpt()
    capture = Capture()
    slot_a = build(t, a[0], a[1], 48)
    slot_b = build(t, b[0], b[1], 48)
    edge_x = float(t.table.world[slot_b, 0])            # A's +x edge is B's -x edge
    na = edge_normals(t, slot_a, capture.run(t, slot_a), edge_x)
    nb = edge_normals(t, slot_b, capture.run(t, slot_b), edge_x)
    shared = sorted(set(na) & set(nb))
    assert len(shared) == 49, f"expected the full shared edge, matched {len(shared)}"
    angles = [np.degrees(np.arccos(np.clip(float(na[z] @ nb[z]), -1.0, 1.0)))
              for z in shared]
    assert max(angles) < 0.1, (
        f"{biome}: the smooth normal kinks by {max(angles):.2f} degrees "
        f"across the chunk border")
