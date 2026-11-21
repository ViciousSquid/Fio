"""A map's ``terrain_data`` and its angled brushes' plane counts are checked
before the open level is replaced.

Core tier: ``engine.level_validation`` has no Qt dependency.

Regressions covered:

* ``validate_level`` never looked at ``terrain_data``. A seed of ``"abc"``,
  ``1.5`` or ``1e30``, a string ``grass_density``, a list ``biome`` or a list
  instead of the whole object raised from ``Terrain.from_dict`` -- *after*
  ``load_from_data`` had replaced the scene, leaving a half-built untitled
  one (or a dead Play session, when a LevelChanger asked for the map).
  Sculpt offsets of ``NaN``/``Infinity`` gave NaN heights that reached
  collision.
* an angled brush could carry any number of planes, and building its faces
  is quadratic in that number: 1000 planes took 8.5 s for one brush.
"""

import glob
import json
import math
import os

import pytest

from engine import level_validation
from engine.level_validation import (MAX_BRUSH_PLANES, TERRAIN_SEED_MAX,
                                     validate_level)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TERRAIN = {"enabled": True, "seed": 42, "biome": "Desert Canyon", "chunk_size": 256,
           "mesh_scale": 1.0, "base_resolution": 48, "offset_x": 0, "offset_y": 0.0,
           "offset_z": 0, "min_chunk_x": -2, "max_chunk_x": 2, "min_chunk_z": -2,
           "max_chunk_z": 2, "grass_density": 0.02, "grass_height": 6.0,
           "grass_color": [0.2, 0.5, 0.1], "grass_tip_color": None,
           "grass_height_range": None, "appearance": {}, "custom_biome": {
               "name": "Desert Canyon", "height_scale": 200.0, "hills_scale": 0.002,
               "mountains_enabled": False, "color_gradient": [[0.0, [0.6, 0.4, 0.25]]],
               "blend_weights": [1.0, 0.3, 0.0, 0.0]},
           "sculpt_offsets": [[0, 0, 1.5], [-3, 7, -2]], "sculpt_grid_resolution": 4.0,
           "texture_paint_resolution": 512, "texture_paint": []}


def _map(**terrain_changes):
    terrain = dict(TERRAIN)
    for key, value in terrain_changes.items():
        if value is _DROP:
            terrain.pop(key, None)
        else:
            terrain[key] = value
    return {"version": 5, "brushes": [], "things": [], "terrain_data": terrain}


_DROP = object()


def test_a_well_formed_terrain_passes():
    validate_level(_map())
    validate_level({"brushes": [], "things": [], "terrain_data": {}})
    validate_level({"brushes": [], "things": [], "terrain_data": None})


@pytest.mark.parametrize("path", sorted(glob.glob(os.path.join(ROOT, "maps", "*.json"))),
                         ids=os.path.basename)
def test_every_shipped_map_passes(path):
    with open(path, encoding="utf-8") as fh:
        validate_level(json.load(fh))


@pytest.mark.parametrize("changes, field", [
    ({"seed": "abc"}, "seed"),
    ({"seed": 1.5}, "seed"),
    ({"seed": 10 ** 30}, "seed"),
    ({"seed": -1}, "seed"),
    ({"seed": TERRAIN_SEED_MAX + 1}, "seed"),
    ({"seed": True}, "seed"),
    ({"seed": math.nan}, "seed"),
    ({"grass_density": "x"}, "grass_density"),
    ({"grass_density": math.nan}, "grass_density"),
    ({"grass_height": "tall"}, "grass_height"),
    ({"heightmap_strength": "x"}, "heightmap_strength"),
    ({"chunk_size": math.inf}, "chunk_size"),
    ({"offset_y": None}, "offset_y"),
    ({"biome": [1]}, "biome"),
    ({"heightmap_blob": 5}, "heightmap_blob"),
    ({"appearance": "x"}, "appearance"),
    ({"custom_biome": [1, 2]}, "custom_biome"),
    ({"custom_biome": {"height_scale": "x"}}, "height_scale"),
    ({"custom_biome": {"hills_scale": math.inf}}, "hills_scale"),
    ({"sculpt_offsets": {"0,0": 1}}, "sculpt_offsets"),
    ({"sculpt_offsets": [[0, 0, math.nan]]}, "sculpt_offsets"),
    ({"sculpt_offsets": [[0, 0, 1.0], [1, 1, math.inf]]}, "sculpt_offsets"),
    ({"sculpt_offsets": [[0, 0]]}, "sculpt_offsets"),
    ({"sculpt_offsets": [[0, 0, "1"]]}, "sculpt_offsets"),
    ({"sculpt_offsets": [[True, False, True]]}, "sculpt_offsets"),
    ({"csg_subtractions": [[0, 0, 0, math.inf, 1, 1]]}, "csg_subtractions"),
    ({"paint_stamps": "x"}, "paint_stamps"),
])
def test_a_malformed_terrain_is_refused_naming_the_field(changes, field):
    with pytest.raises(ValueError, match=field):
        validate_level(_map(**changes))


@pytest.mark.parametrize("terrain", [[1], "terrain", 7])
def test_terrain_data_must_be_an_object(terrain):
    with pytest.raises(ValueError, match="terrain_data"):
        validate_level({"brushes": [], "things": [], "terrain_data": terrain})


def test_a_save_carrying_a_bad_terrain_is_refused():
    from engine.level_validation import validate_snapshot
    with pytest.raises(ValueError, match="seed"):
        validate_snapshot({"level": _map(seed="abc")})


def test_a_large_valid_sculpt_is_checked_in_one_pass():
    """The usual case -- every entry three numbers -- takes one NumPy pass;
    the entry-by-entry walk (which names the bad entry) runs only on failure."""
    sculpt = [[x, z, 0.5] for x in range(300) for z in range(300)]
    calls = []
    real = level_validation.finite_number

    def counting(value):
        calls.append(value)
        return real(value)

    level_validation.finite_number = counting
    try:
        validate_level(_map(sculpt_offsets=sculpt))
    finally:
        level_validation.finite_number = real
    assert len(calls) < 100


def test_a_refused_terrain_leaves_the_open_level_untouched():
    """The editor validates before it touches the scene, so a refused
    terrain changes nothing (EditorState.load_from_data)."""
    pytest.importorskip("PyQt5")
    from editor.editor_state import EditorState
    state = EditorState()
    state.load_from_data(_map())
    before = json.dumps(state.get_level_data(), sort_keys=True, default=str)
    with pytest.raises(ValueError, match="seed"):
        state.load_from_data(_map(seed="abc"))
    assert json.dumps(state.get_level_data(), sort_keys=True, default=str) == before


# -- plane count -----------------------------------------------------------------

def _sphere_planes(count, radius=64.0):
    golden = math.pi * (3 - math.sqrt(5))
    planes = []
    for i in range(count):
        y = 1 - 2 * (i + 0.5) / count
        r = math.sqrt(1 - y * y)
        planes.append({"n": [r * math.cos(golden * i), y, r * math.sin(golden * i)],
                       "d": radius})
    return planes


def _brush(planes):
    return {"brushes": [{"pos": [0, 0, 0], "geometry": {"planes": planes}}],
            "things": []}


def test_a_brush_at_the_plane_cap_passes():
    validate_level(_brush(_sphere_planes(MAX_BRUSH_PLANES)))


def test_a_brush_past_the_plane_cap_is_refused():
    with pytest.raises(ValueError, match="planes"):
        validate_level(_brush(_sphere_planes(MAX_BRUSH_PLANES + 1)))


def test_the_cap_is_far_above_any_real_brush():
    """The bundled maps' angled brushes and the largest hull a component
    edit can rebuild (MAX_HULL_POINTS corners: at most 2n - 4 faces) all fit
    several times over."""
    from engine.brush_geometry import MAX_HULL_POINTS
    largest = 0
    for path in glob.glob(os.path.join(ROOT, "maps", "*.json")):
        with open(path, encoding="utf-8") as fh:
            level = json.load(fh)
        for brush in level.get("brushes") or []:
            geometry = brush.get("geometry") if isinstance(brush, dict) else None
            if isinstance(geometry, dict):
                largest = max(largest, len(geometry.get("planes") or []))
    assert largest * 4 <= MAX_BRUSH_PLANES
    assert (2 * MAX_HULL_POINTS - 4) * 3 <= MAX_BRUSH_PLANES


def test_a_clip_never_takes_a_brush_past_the_cap(monkeypatch):
    """Clipping adds a plane; at the cap it is refused, so the editor can
    never save a brush its own loader would refuse."""
    from engine import brush_geometry
    monkeypatch.setattr(brush_geometry, "MAX_BRUSH_PLANES", 6)
    brush = {"pos": [0, 0, 0], "size": [64, 64, 64]}
    assert not brush_geometry.clip_brush(brush, [1, 1, 0], 20.0)
    monkeypatch.setattr(brush_geometry, "MAX_BRUSH_PLANES", 7)
    brush = {"pos": [0, 0, 0], "size": [64, 64, 64]}
    assert brush_geometry.clip_brush(brush, [1, 1, 0], 20.0)
    assert len(brush["geometry"]["planes"]) == 7
