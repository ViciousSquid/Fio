"""Sculpt is saved as compressed tiles, and a save after a dab is cheap.

Sculpt used to be saved as a list of ``[gx, gz, offset]`` triples: at a
million sculpted cells that was 70 MB of indented JSON, rebuilt in full by
``to_dict`` after every dab (and the 3D view calls ``to_dict`` after every
dab). It is now cut into tiles, one compressed record each
(``Terrain._encode_sculpt_tile``); a dab marks only the tiles under it for
encoding again. Maps saved with the triples still load, to the same heights.

The perf tests count re-encoded tiles (a kept tile is the same record
object) and bytes rather than time them.
"""

import base64
import json
import os
import struct
import zlib

import numpy as np
import pytest

pytest.importorskip("OpenGL", reason="engine.terrain imports PyOpenGL")

from engine.terrain import Terrain                 # noqa: E402

pytestmark = pytest.mark.qt

SEED = 7
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _brushed(dabs=120, seed=0):
    """A terrain sculpted the way the editor does it: brush dabs."""
    terrain = Terrain(seed=SEED)
    rng = np.random.default_rng(seed)
    for _ in range(dabs):
        x, z = rng.uniform(-700.0, 700.0, 2)
        terrain.apply_sculpt_at(x, z, rng.uniform(6.0, 60.0), rng.uniform(-15.0, 15.0))
    return terrain


def _legacy(terrain):
    """*terrain*'s data as a map saved before sculpt tiles."""
    data = terrain.to_dict()
    del data["sculpt_tiles"]
    data["sculpt_offsets"] = [[gx, gz, v] for (gx, gz), v in terrain.sculpt_offsets.items()]
    return data


def _bits(offsets):
    return {key: struct.pack("<d", value) for key, value in offsets.items()}


def _reloaded(data):
    terrain = Terrain(seed=SEED)
    terrain.from_dict(json.loads(json.dumps(data, indent=4)))
    return terrain


def _assert_same_heights(a, b):
    rng = np.random.default_rng(3)
    xs, zs = rng.uniform(-900.0, 900.0, (2, 3000))
    np.testing.assert_array_equal(a._get_heights_batch(xs, zs), b._get_heights_batch(xs, zs))
    assert all(a.get_height_at(x, z) == b.get_height_at(x, z)
               for x, z in zip(xs[:50], zs[:50]))


def _tile(tx, tz, raw):
    return {"tile": [tx, tz], "data": base64.b64encode(zlib.compress(raw)).decode("ascii")}


def _cells(index, values):
    index = np.asarray(index, "<u2")
    values = np.asarray(values, "<f8")
    return index.tobytes() + values.view(np.uint8).reshape(len(values), 8).T.tobytes()


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------

def test_a_save_and_load_gives_back_every_cell_bit_for_bit():
    terrain = _brushed()
    # The awkward values too: zeros of both signs, a subnormal, huge.
    terrain.sculpt_offsets.update({(-1, -1): 0.0, (40, -33): -0.0,
                                   (-65, 31): 5e-324, (1000, 1000): -1.5e300})
    terrain._touch_sculpt()
    data = terrain.to_dict()
    assert "sculpt_offsets" not in data

    reloaded = _reloaded(data)
    assert _bits(reloaded.sculpt_offsets) == _bits(terrain.sculpt_offsets)
    assert reloaded.to_dict()["sculpt_tiles"] == data["sculpt_tiles"]


def test_a_legacy_map_loads_to_the_same_sculpt_and_heights():
    terrain = _brushed()
    legacy = _reloaded(_legacy(terrain))
    tiled = _reloaded(terrain.to_dict())
    assert _bits(legacy.sculpt_offsets) == _bits(tiled.sculpt_offsets)
    _assert_same_heights(legacy, tiled)
    # Saving it again writes tiles, which load to the same again.
    again = legacy.to_dict()
    assert "sculpt_offsets" not in again
    assert _bits(_reloaded(again).sculpt_offsets) == _bits(terrain.sculpt_offsets)


def test_the_shipped_legacy_map_survives_a_save_in_the_new_format():
    with open(os.path.join(ROOT, "maps", "BigWorld_streaming_test.json")) as f:
        saved = json.load(f)["terrain_data"]
    assert "sculpt_offsets" in saved
    legacy = _reloaded(saved)
    assert len(legacy.sculpt_offsets) == len(saved["sculpt_offsets"])
    resaved = _reloaded(legacy.to_dict())
    assert _bits(resaved.sculpt_offsets) == _bits(legacy.sculpt_offsets)
    _assert_same_heights(legacy, resaved)


def test_tiles_win_over_legacy_triples_for_the_same_cell():
    data = Terrain(seed=SEED).to_dict()
    data["sculpt_offsets"] = [[0, 0, 1.0], [100, 100, 2.0]]
    data["sculpt_tiles"] = [_tile(0, 0, _cells([0, 1], [5.0, 6.0]))]
    terrain = _reloaded(data)
    assert terrain.sculpt_offsets == {(0, 0): 5.0, (0, 1): 6.0, (100, 100): 2.0}


def test_damaged_tiles_cost_only_themselves():
    good = _tile(0, 0, _cells([0], [1.0]))
    bomb = {"tile": [1, 0], "data": base64.b64encode(
        zlib.compress(b"\0" * 10_000_000)).decode("ascii")}
    data = Terrain(seed=SEED).to_dict()
    data["sculpt_tiles"] = [
        good, bomb,
        {"tile": [2, 0], "data": "not base64 !!"},
        {"tile": [3, 0], "data": base64.b64encode(b"\x78\x9c junk").decode()},
        _tile(4, 0, _cells([0], [1.0])[:-1]),               # truncated
        _tile(5, 0, _cells([5000], [1.0])),                 # index past the tile
        _tile(True, 0, _cells([0], [1.0])),
        _tile(1.5, 0, _cells([0], [1.0])),
        _tile(2 ** 60, 0, _cells([0], [1.0])),
        {"tile": [6, 0]}, {"data": good["data"]}, "x", None, 7, [],
        _tile(0, 1, _cells([0, 1, 2], [float("nan"), 2.0, float("inf")])),
    ]
    terrain = _reloaded(data)
    side = 1 << terrain.SCULPT_TILE_SHIFT
    assert terrain.sculpt_offsets == {(0, 0): 1.0, (0, side + 1): 2.0}
    assert np.isfinite(terrain._get_heights_batch(np.zeros(3), np.arange(3.0) * 4)).all()
    # What was dropped is not written back.
    assert _bits(_reloaded(terrain.to_dict()).sculpt_offsets) == _bits(terrain.sculpt_offsets)


def test_out_of_range_legacy_cells_are_dropped():
    data = Terrain(seed=SEED).to_dict()
    data["sculpt_offsets"] = [[1, 2, 0.5], [10 ** 30, 0, 1.0], [0, -10 ** 30, 1.0],
                              [1e30, 0, 1.0]]
    terrain = _reloaded(data)
    assert terrain.sculpt_offsets == {(1, 2): 0.5}
    assert np.isfinite(terrain._get_heights_batch(np.array([4.0]), np.array([8.0]))).all()


# ---------------------------------------------------------------------------
# Work done
# ---------------------------------------------------------------------------

def _encoded(before, after):
    """Tiles whose record in *after* was encoded afresh: a tile kept from
    the previous save is the very same record object."""
    kept = {tuple(entry["tile"]): entry for entry in before}
    return [tuple(entry["tile"]) for entry in after
            if kept.get(tuple(entry["tile"])) is not entry]


@pytest.mark.perf
def test_a_save_after_a_dab_encodes_only_the_tiles_under_it():
    terrain = _brushed(dabs=200)
    before = terrain.to_dict()["sculpt_tiles"]
    assert _encoded(before, terrain.to_dict()["sculpt_tiles"]) == []  # nothing changed

    shift = terrain.SCULPT_TILE_SHIFT
    for x in range(0, 400, 20):
        terrain.apply_sculpt_at(float(x), 30.0, 24.0, 1.0)
        after = terrain.to_dict()["sculpt_tiles"]
        encoded = _encoded(before, after)
        # A 24-unit brush (8 cells across at 4 units a cell) spans at most
        # two tiles a side.
        assert 1 <= len(encoded) <= 4, encoded
        cx, cz = int(x // 4.0), int(30.0 // 4.0)
        reach = int(np.ceil(24.0 / 4.0)) + 1
        assert all((cx - reach) >> shift <= tx <= (cx + reach) >> shift
                   and (cz - reach) >> shift <= tz <= (cz + reach) >> shift
                   for tx, tz in encoded)
        before = after

    # And the tiles kept are what a full encode would write now.
    terrain._sculpt_tile_cache = None
    assert terrain.to_dict()["sculpt_tiles"] == before


@pytest.mark.perf
def test_loading_then_saving_reuses_the_loaded_tiles():
    loaded = json.loads(json.dumps(_brushed().to_dict()))
    terrain = Terrain(seed=SEED)
    terrain.from_dict(loaded)
    saved = terrain.to_dict()["sculpt_tiles"]
    assert saved == loaded["sculpt_tiles"]
    # Not encoded again: the very strings that were read.
    assert all(out["data"] is read["data"]
               for out, read in zip(saved, loaded["sculpt_tiles"]))


@pytest.mark.perf
def test_a_large_sculpt_saves_in_a_fraction_of_the_old_size():
    terrain = Terrain(seed=SEED)
    rng = np.random.default_rng(5)
    values = rng.normal(0.0, 10.0, 300 * 300).tolist()
    terrain.sculpt_offsets = {(gx - 150, gz - 150): values[gx * 300 + gz]
                              for gx in range(300) for gz in range(300)}
    terrain._touch_sculpt()
    data = terrain.to_dict()
    side = 1 << terrain.SCULPT_TILE_SHIFT
    assert len(data["sculpt_tiles"]) <= (300 // side + 2) ** 2
    saved = len(json.dumps(data["sculpt_tiles"], indent=4))
    legacy = len(json.dumps(_legacy(terrain)["sculpt_offsets"], indent=4))
    # 8 bytes a cell is the floor for an exact double, random ones barely
    # compress, and base64 adds a third: ~13 bytes, against ~70.
    assert saved < 14 * len(terrain.sculpt_offsets) < legacy / 4


# ---------------------------------------------------------------------------
# Every other change still invalidates
# ---------------------------------------------------------------------------

def test_every_kind_of_sculpt_change_reaches_the_saved_tiles():
    terrain = _brushed(dabs=40)
    steps = [
        lambda t: t.apply_sculpt_at(10.0, 10.0, 30.0, 4.0),
        lambda t: t.smooth_sculpt_at(10.0, 10.0, 50.0, 0.7),
        lambda t: [t.flatten_sculpt_at(10.0, 10.0, 60.0, 1.0) for _ in range(8)],
        lambda t: t.apply_sculpt_at(5000.0, -5000.0, 8.0, 1.0),
        lambda t: t.from_dict(_brushed(dabs=10, seed=9).to_dict()),
        lambda t: t.apply_sculpt_at(0.0, 0.0, 30.0, 4.0),
        lambda t: t.from_dict(_legacy(_brushed(dabs=10, seed=4))),
        lambda t: t.apply_sculpt_at(0.0, 0.0, 30.0, 4.0),
        lambda t: (t.sculpt_offsets.__setitem__((900, 900), 3.0), t._touch_sculpt()),
        lambda t: t.clear_sculpt(),
        lambda t: t.apply_sculpt_at(0.0, 0.0, 30.0, 4.0),
    ]
    for i, step in enumerate(steps):
        terrain.to_dict()
        step(terrain)
        data = terrain.to_dict()
        if terrain.sculpt_offsets:
            assert _bits(_reloaded(data).sculpt_offsets) == _bits(terrain.sculpt_offsets), i
        else:
            assert "sculpt_tiles" not in data, i
