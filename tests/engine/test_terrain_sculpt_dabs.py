"""A sculpt dab patches the dense sculpt grid instead of rebuilding it.

Sampling the sculpt goes through a dense array over its bounding box
(``Terrain._dense_sculpt``). Rebuilding that array re-reads the whole sculpt
dict -- ~150 ms at a million cells -- and used to happen on the first sample
after every dab of a brush drag. A dab now copies only its own square into
the grid, growing it with a margin when the dab passes an edge.

The perf tests count full rebuilds rather than time them. The rest hold the
patched grid to a from-scratch rebuild, bit for bit, and check that every
other way the sculpt changes still throws the grid away.
"""

import numpy as np
import pytest

pytest.importorskip("OpenGL", reason="engine.terrain imports PyOpenGL")

from engine.terrain import Terrain                 # noqa: E402

pytestmark = pytest.mark.qt

SEED = 5


def _sculpted(cells=200):
    """A terrain with a ``cells`` x ``cells`` block sculpted around the origin."""
    terrain = Terrain(seed=SEED)
    terrain.sculpt_grid_resolution = 4.0
    rng = np.random.default_rng(0)
    half = cells // 2
    values = rng.uniform(-30, 30, (cells, cells))
    terrain.sculpt_offsets = {
        (gx - half, gz - half): float(values[gx, gz])
        for gx in range(cells) for gz in range(cells)}
    terrain._touch_sculpt()
    return terrain


def _points(lo=-1200.0, hi=1200.0, n=4000, seed=1):
    rng = np.random.default_rng(seed)
    return rng.uniform(lo, hi, n), rng.uniform(lo, hi, n)


def _rebuilt(terrain):
    """A second terrain holding the same sculpt, its dense grid built fresh."""
    ref = Terrain(seed=SEED)
    ref.sculpt_grid_resolution = terrain.sculpt_grid_resolution
    ref.MAX_DENSE_SCULPT_CELLS = terrain.MAX_DENSE_SCULPT_CELLS
    ref.sculpt_offsets = dict(terrain.sculpt_offsets)
    ref._touch_sculpt()
    return ref


def _assert_matches_a_rebuild(terrain):
    ref = _rebuilt(terrain)
    xs, zs = _points()
    np.testing.assert_array_equal(terrain._sample_sculpt_batch(xs, zs),
                                  ref._sample_sculpt_batch(xs, zs))
    np.testing.assert_array_equal(terrain._get_heights_batch(xs, zs),
                                  ref._get_heights_batch(xs, zs))
    for base in ((-512.0, -512.0), (0.0, 0.0), (384.0, -128.0)):
        np.testing.assert_array_equal(terrain._grid_heights(*base, 256.0, 32),
                                      ref._grid_heights(*base, 256.0, 32))
    dense, fresh = terrain._dense_sculpt(), ref._dense_sculpt()
    assert (dense is None) == (fresh is None)
    if dense is not None:
        # The patched grid may reach past the bounding box; there it is zero.
        (gx, gz, grid), (fx, fz, fgrid) = dense, fresh
        ox, oz = fx - gx, fz - gz
        assert ox >= 0 and oz >= 0
        assert ox + fgrid.shape[0] <= grid.shape[0]
        assert oz + fgrid.shape[1] <= grid.shape[1]
        inner = grid[ox:ox + fgrid.shape[0], oz:oz + fgrid.shape[1]]
        np.testing.assert_array_equal(inner, fgrid)
        assert np.count_nonzero(grid) == np.count_nonzero(inner)


class _CountingOffsets(dict):
    """A sculpt dict that counts whole-dict reads (a full dense rebuild)."""

    full_reads = 0

    def __iter__(self):
        self.full_reads += 1
        return super().__iter__()

    def values(self):
        self.full_reads += 1
        return super().values()


def _counting(terrain):
    terrain.sculpt_offsets = _CountingOffsets(terrain.sculpt_offsets)
    terrain._touch_sculpt()
    return terrain.sculpt_offsets


# ---------------------------------------------------------------------------
# Work done
# ---------------------------------------------------------------------------

@pytest.mark.perf
@pytest.mark.parametrize("tool", ["apply_sculpt_at", "smooth_sculpt_at",
                                  "flatten_sculpt_at"])
def test_a_dab_inside_the_grid_does_not_rebuild_it(tool):
    terrain = _sculpted()
    offsets = _counting(terrain)
    xs, zs = _points(-300.0, 300.0)
    terrain._sample_sculpt_batch(xs, zs)
    grid = terrain._dense_sculpt()[2]
    assert offsets.full_reads == 2                 # the first build
    offsets.full_reads = 0

    for i in range(20):
        getattr(terrain, tool)(-200.0 + 20.0 * i, 40.0, 24.0, 0.5)
        terrain._sample_sculpt_batch(xs, zs)

    assert offsets.full_reads == 0, "a dab rebuilt the whole dense grid"
    assert terrain._dense_sculpt()[2] is grid, "a dab replaced the grid"
    _assert_matches_a_rebuild(terrain)


@pytest.mark.perf
def test_a_drag_off_the_edge_regrows_the_grid_a_few_times():
    terrain = _sculpted(cells=50)              # cells -25..24: x up to ~96
    offsets = _counting(terrain)
    xs, zs = _points(-300.0, 300.0)
    terrain._sample_sculpt_batch(xs, zs)
    offsets.full_reads = 0

    grids = set()
    for i in range(200):                       # 40 world units a dab, east
        terrain.apply_sculpt_at(80.0 + 40.0 * i, 0.0, 30.0, 1.0)
        terrain._sample_sculpt_batch(xs, zs)
        grids.add(id(terrain._dense_sculpt()[2]))

    assert offsets.full_reads == 0
    # 2000 cells east of a 50-cell grid: geometric growth, not 200 regrows.
    assert len(grids) <= 12
    _assert_matches_a_rebuild(terrain)


# ---------------------------------------------------------------------------
# Same result as a rebuild
# ---------------------------------------------------------------------------

def test_a_mix_of_dabs_matches_a_rebuild_after_every_dab():
    terrain = _sculpted(cells=60)
    xs, zs = _points(-200.0, 200.0, n=500)
    terrain._sample_sculpt_batch(xs, zs)
    dabs = [
        ("apply_sculpt_at", 0.0, 0.0, 30.0, 5.0),        # inside
        ("apply_sculpt_at", 10.0, -7.5, 18.0, -12.0),    # lower, inside
        ("smooth_sculpt_at", 3.0, 3.0, 40.0, 0.8),
        ("apply_sculpt_at", 118.0, 0.0, 20.0, 4.0),      # over the east edge
        ("apply_sculpt_at", -400.0, 0.0, 20.0, 4.0),     # far west
        ("apply_sculpt_at", 0.0, 900.0, 64.0, -3.0),     # far south
        ("apply_sculpt_at", 0.0, -650.0, 8.0, 2.0),      # far north
        ("smooth_sculpt_at", 0.0, 1200.0, 24.0, 1.0),    # smooth into nothing
        ("flatten_sculpt_at", 0.0, 0.0, 50.0, 1.0),      # deletes entries
        ("flatten_sculpt_at", -400.0, 0.0, 60.0, 1.0),
        ("apply_sculpt_at", 1500.0, 1500.0, 4.5, 7.0),
    ]
    for tool, x, z, radius, strength in dabs:
        getattr(terrain, tool)(x, z, radius, strength)
        _assert_matches_a_rebuild(terrain)


def test_flattening_everything_then_sculpting_again_matches_a_rebuild():
    terrain = Terrain(seed=SEED)
    terrain.apply_sculpt_at(0.0, 0.0, 20.0, 0.5)
    _assert_matches_a_rebuild(terrain)
    for _ in range(10):
        terrain.flatten_sculpt_at(0.0, 0.0, 40.0, 1.0)
    assert not terrain.sculpt_offsets
    terrain.apply_sculpt_at(300.0, 0.0, 20.0, 2.0)
    _assert_matches_a_rebuild(terrain)


def test_growing_past_the_dense_limit_falls_back_like_a_rebuild():
    terrain = _sculpted(cells=20)
    terrain.MAX_DENSE_SCULPT_CELLS = 30 * 20      # cells -10..9 square
    _assert_matches_a_rebuild(terrain)
    # Brush square x 13..19: the margin would not fit; the bare union
    # (x -10..19, z -10..9) just does.
    terrain.apply_sculpt_at(4.0 * 16, 0.0, 6.0, 1.0)
    assert terrain._dense_sculpt()[2].shape == (30, 20)
    _assert_matches_a_rebuild(terrain)
    # Too wide for a dense grid at all: the dictionary path, as a rebuild.
    terrain.apply_sculpt_at(4.0 * 200, 0.0, 6.0, 1.0)
    _assert_matches_a_rebuild(terrain)
    assert terrain._dense_sculpt() is None
    terrain.apply_sculpt_at(0.0, 0.0, 6.0, 1.0)
    _assert_matches_a_rebuild(terrain)
    # Flattened back inside the limit: dense again.
    for _ in range(10):
        terrain.flatten_sculpt_at(4.0 * 200, 0.0, 30.0, 1.0)
    _assert_matches_a_rebuild(terrain)
    assert terrain._dense_sculpt() is not None


# ---------------------------------------------------------------------------
# Every other change still invalidates
# ---------------------------------------------------------------------------

def _warm(terrain):
    xs, zs = _points(-300.0, 300.0, n=300)
    terrain._sample_sculpt_batch(xs, zs)
    assert terrain._dense_sculpt() is not None


def test_clear_drops_the_grid():
    terrain = _sculpted(cells=40)
    _warm(terrain)
    terrain.clear_sculpt()
    assert terrain._sculpt_cache is None
    xs, zs = _points()
    assert not terrain._sample_sculpt_batch(xs, zs).any()
    terrain.apply_sculpt_at(0.0, 0.0, 30.0, 1.0)
    _assert_matches_a_rebuild(terrain)


def test_undo_and_load_replace_the_grid():
    terrain = _sculpted(cells=40)
    _warm(terrain)
    saved = terrain.to_dict()
    for i in range(5):
        terrain.apply_sculpt_at(40.0 * i, 0.0, 30.0, 6.0)
    _warm(terrain)
    after = terrain.to_dict()

    terrain.from_dict(saved)                       # undo
    assert terrain._sculpt_cache is None
    _assert_matches_a_rebuild(terrain)
    loaded = Terrain(seed=SEED)
    loaded.from_dict(saved)
    xs, zs = _points()
    np.testing.assert_array_equal(terrain._sample_sculpt_batch(xs, zs),
                                  loaded._sample_sculpt_batch(xs, zs))

    terrain.from_dict(after)                       # redo
    _assert_matches_a_rebuild(terrain)
    terrain.apply_sculpt_at(0.0, 0.0, 30.0, 1.0)   # and dab on after it
    _assert_matches_a_rebuild(terrain)


def test_a_replaced_or_directly_edited_dict_is_not_patched():
    terrain = _sculpted(cells=40)
    _warm(terrain)
    # Replaced wholesale: a new dict is a new key, and the dab after it
    # must not patch the old dict's grid.
    terrain.sculpt_offsets = {(500, 500): 3.0}
    terrain.apply_sculpt_at(0.0, 0.0, 20.0, 1.0)
    _assert_matches_a_rebuild(terrain)
    # Edited in place without telling the terrain, outside the next dab:
    # the size change keeps the grid from being patched.
    _warm(terrain)
    terrain.sculpt_offsets[(-300, -300)] = 9.0
    terrain.apply_sculpt_at(0.0, 0.0, 20.0, 1.0)
    _assert_matches_a_rebuild(terrain)


def test_a_dab_invalidates_the_saved_form():
    terrain = _sculpted(cells=10)
    _warm(terrain)
    first = terrain.to_dict()["sculpt_tiles"]
    terrain.apply_sculpt_at(0.0, 0.0, 6.0, 1.0)
    second = terrain.to_dict()["sculpt_tiles"]
    assert first != second
    reloaded = Terrain(seed=SEED)
    reloaded.from_dict(terrain.to_dict())
    assert reloaded.sculpt_offsets == terrain.sculpt_offsets
