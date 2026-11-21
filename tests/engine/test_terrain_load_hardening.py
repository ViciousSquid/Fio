"""A damaged terrain record costs that record, not the whole map load.

``Terrain.from_dict`` reads a map file, and maps arrive in shared packages. A
heightmap blob that is not a plain 2-D ``.npy`` (corrupt base64, a header
promising data it does not hold, an ``.npz``) or a malformed sculpt entry used
to raise out of ``from_dict`` and abort loading the level.
"""

import base64
import json
import io

import numpy as np
import pytest

pytest.importorskip("OpenGL", reason="engine.terrain imports PyOpenGL")

from engine.terrain import Terrain                 # noqa: E402

pytestmark = pytest.mark.qt


def _npy(array):
    buf = io.BytesIO()
    np.save(buf, array)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _lying_header():
    buf = io.BytesIO()
    np.lib.format.write_array_header_1_0(
        buf, {"descr": "<f4", "fortran_order": False, "shape": (200_000, 200_000)})
    buf.write(b"\0" * 16)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _npz():
    buf = io.BytesIO()
    np.savez(buf, a=np.zeros((2, 2)))
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _loaded(**fields):
    terrain = Terrain(seed=3)
    data = terrain.to_dict()
    data.update(fields)
    terrain.from_dict(data)
    return terrain


def test_a_saved_heightmap_round_trips():
    heights = np.linspace(0, 1, 12, dtype=np.float32).reshape(3, 4)
    terrain = _loaded(heightmap_blob=_npy(heights))
    np.testing.assert_array_equal(terrain.heightmap_data, heights)


@pytest.mark.parametrize("blob", [
    "@@not base64@@",
    _lying_header(),
    _npz(),
    _npy(np.zeros(5, dtype=np.float32)),
    _npy(np.array([["a", "b"]])),
    _npy(np.array([[{"x": 1}]], dtype=object)),
])
def test_an_unusable_heightmap_is_dropped(blob):
    assert _loaded(heightmap_blob=blob).heightmap_data is None


def test_malformed_sculpt_entries_are_skipped():
    terrain = _loaded(sculpt_offsets=[[1, 2, 0.5], [3, 4], "xyz", [5, "z", 1.0], None])
    assert terrain.sculpt_offsets == {(1, 2): 0.5}


def test_non_finite_sculpt_entries_are_skipped():
    # Python's json reads NaN/Infinity; a NaN offset became a NaN height,
    # and an infinite coordinate raised out of int().
    terrain = _loaded(sculpt_offsets=[[1, 2, 0.5], [3, 4, float("nan")],
                                      [5, 6, float("inf")], [7, 8, float("-inf")],
                                      [float("inf"), 0, 1.0], [0, float("nan"), 1.0]])
    assert terrain.sculpt_offsets == {(1, 2): 0.5}


# ---------------------------------------------------------------------------
# Any JSON in any field: defaults, never an exception
# ---------------------------------------------------------------------------

#: One of each JSON shape, and the numbers that break arithmetic.
_JSON_VALUES = [None, True, False, "x", "", 0, -1, 1.5, 1e30, 10 ** 30, -10 ** 30,
                float("nan"), float("inf"), float("-inf"), [], [1], [[1]], [None],
                ["x", "y", "z"], [1, 2, 3], [[1, 2, "x"]], {}, {"a": 1}, [{}],
                [{"tile": [0, 0], "data": "x"}], [{"coord": [0, 0], "data": "x"}]]

_TERRAIN_FIELDS = sorted(set(Terrain(seed=3).to_dict()) | {
    "lock", "sculpt_offsets", "sculpt_tiles", "sculpt_grid_resolution",
    "csg_subtractions", "texture_paint", "texture_paint_base",
    "texture_paint_resolution", "paint_stamps", "texture_stamps",
    "heightmap_blob", "heightmap_strength", "heightmap_blend"})


def _assert_loads_cleanly(terrain, data):
    terrain.from_dict(data)
    xs, zs = np.array([0.0, 100.0, -37.5]), np.array([0.0, 7.0, 512.0])
    heights = terrain._get_heights_batch(xs, zs)
    assert np.isfinite(heights).all() and np.isfinite(terrain.get_height_at(0.5, 0.5))
    terrain._get_colors_batch(heights, np.array([0.1, 0.5, 0.9]))
    # What it saves is strict JSON (no NaN) and loads back the same way.
    saved = json.dumps(terrain.to_dict(), allow_nan=False)
    terrain.from_dict(json.loads(saved))


@pytest.mark.parametrize("data", _JSON_VALUES, ids=repr)
def test_terrain_data_of_any_json_type_loads_as_defaults(data):
    terrain = Terrain(seed=3)
    _assert_loads_cleanly(terrain, data)
    assert terrain.seed == 42 and terrain.biome.name == "Grassy Hills"


@pytest.mark.parametrize("field", _TERRAIN_FIELDS)
def test_any_json_in_a_terrain_field_loads(field):
    terrain = Terrain(seed=3)
    for value in _JSON_VALUES:
        _assert_loads_cleanly(terrain, {field: value})


@pytest.mark.parametrize("record", ["custom_biome", "appearance"])
def test_any_json_in_a_nested_terrain_field_loads(record):
    terrain = Terrain(seed=3)
    fields = (terrain.biome.to_dict() if record == "custom_biome"
              else terrain.appearance.to_dict())
    for field in fields:
        for value in _JSON_VALUES:
            _assert_loads_cleanly(terrain, {record: {field: value}})


@pytest.mark.parametrize("field", ["x", "z", "radius", "texture", "feather",
                                   "opacity", "angle", "id", "tiled"])
def test_any_json_in_a_paint_stamp_field_loads(field):
    terrain = Terrain(seed=3)
    for value in _JSON_VALUES:
        stamp = {"x": 1.0, "z": 1.0, "radius": 4.0, "texture": "no/such.png", field: value}
        _assert_loads_cleanly(terrain, {"paint_stamps": [stamp]})


def test_the_reported_terrain_data_values_load_as_defaults():
    # The ones that raised out of from_dict and aborted the map load.
    for data, check in (
            ({"seed": "abc"}, lambda t: t.seed == 42),
            ({"seed": 1.5}, lambda t: t.seed == 42),
            ({"seed": 1e30}, lambda t: t.seed == 42),
            ({"seed": 7.0}, lambda t: t.seed == 7),
            ({"seed": Terrain.MAX_SEED + 1}, lambda t: t.seed == 42),
            ({"grass_density": "x"}, lambda t: t.grass_density == 0.02),
            ({"grass_density": 5}, lambda t: t.grass_density == 0.24),
            ({"grass_height": "x"}, lambda t: t.grass_height == 5.0),
            ({"biome": [1]}, lambda t: t.biome.name == "Grassy Hills"),
            ({"biome": "desert"}, lambda t: t.biome.name == "Desert"),
            ({"custom_biome": {"height_scale": "x", "color_gradient": "x"}},
             lambda t: t.biome.height_scale == 200.0 and len(t.biome.color_gradient) == 4),
            ({"enabled": "no"}, lambda t: t.enabled is True),
            ({"enabled": 0}, lambda t: t.enabled is False),
            ({"heightmap_blob": _npy(np.ones((4, 4), np.float32)),
              "heightmap_strength": "x", "heightmap_blend": [1]},
             lambda t: t.heightmap_strength == 100.0 and t.heightmap_blend == "additive"),
            ({"csg_subtractions": [[0, -1e9, 0, float("inf"), 1e9, 10]]},
             lambda t: t.csg_subtractions == []),
            ({"paint_stamps": [{"x": 0, "z": 0, "radius": 1e30, "texture": "a.png"}]},
             lambda t: t.paint_stamps == []),
            ({"texture_paint_resolution": float("inf")}, lambda t: True),
    ):
        terrain = Terrain(seed=3)
        _assert_loads_cleanly(terrain, data)
        terrain.from_dict(data)
        assert check(terrain), data
