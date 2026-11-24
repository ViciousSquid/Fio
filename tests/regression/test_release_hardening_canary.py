"""Hardening from the pre-release audit of Canary: malformed map values.

Each of these loaded, then failed somewhere later: in a 2D paint (a
segmentation fault, because the paint's QPainter was left open), at Play
start (half-way into the session, or by filling the spatial grid until
memory ran out), or at the next Save, which refused the map for the rest of
the session.
"""

import copy

import pytest

from engine.level_validation import validate_level

TEXTURES = {face: "default.png" for face in ("north", "south", "east", "west", "top", "down")}


def _level(*things):
    return {
        "brushes": [{"pos": [0, -16, 0], "size": [1024, 32, 1024], "textures": TEXTURES}],
        "things": [{"type": "playerstart", "pos": [0, 40, 0],
                    "properties": {"type": "playerstart", "name": "start"}}, *things],
    }


def _thing(kind, **properties):
    return {"type": kind, "pos": [0, 64, 0], "properties": dict(properties, type=kind)}


# -- numbers nothing can use ---------------------------------------------------------

@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), 10 ** 400,
                                   -(10 ** 400)],
                         ids=["inf", "-inf", "nan", "10^400", "-10^400"])
@pytest.mark.parametrize("field", ["radius", "intensity", "shadow_softness"])
def test_a_property_that_is_not_a_finite_number_is_refused(field, value):
    """``1e999`` is strict JSON and reads as infinity; a 400-digit integer is
    JSON too, and ``float()`` of it raises OverflowError."""
    with pytest.raises(ValueError, match="things\\[1\\]\\['%s'\\].*not finite" % field):
        validate_level(_level(_thing("light", **{field: value})))


def test_a_list_property_holding_such_a_number_is_refused():
    with pytest.raises(ValueError, match="volume_size.*not finite"):
        validate_level(_level(_thing("light_volume", volume_size=[64.0, float("nan"), 64.0])))


def test_a_brush_number_that_is_not_finite_is_refused():
    level = _level()
    level["brushes"][0]["light_emission"] = float("inf")
    with pytest.raises(ValueError, match="brushes\\[0\\]\\['light_emission'\\].*not finite"):
        validate_level(level)


def test_large_and_ordinary_numbers_pass():
    validate_level(_level(_thing("light", radius=1e300, intensity=0, flags=2 ** 70,
                                 on=True, name="lamp", colour=[255, 128, 0])))


# -- what a model's collision is built from --------------------------------------------

@pytest.mark.parametrize("field, value", [
    ("collision_size", [1e308, 64, 64]),       # filled the grid's cells: MemoryError
    ("collision_size", [2e7, 64, 64]),
    ("collision_size", "big"),
    ("rotation", {"yaw": 90}),
    ("rotation", [0, 90]),
    ("scale", "2"),
    ("scale", [1, 1]),
    ("scale", {"x": 2}),
    ("model_path", 7),
    ("model_path", ["assets/models/a.obj"]),
])
@pytest.mark.parametrize("kind", ["prop", "model"])
def test_a_prop_field_of_the_wrong_shape_is_refused(kind, field, value):
    with pytest.raises(ValueError, match="things\\[1\\]\\['%s'\\]" % field):
        validate_level(_level(_thing(kind, **{field: value})))


def test_a_prop_as_the_editor_writes_it_passes():
    prop = _thing("prop", model_path="assets/models/crate.obj", rotation=[0.0, 90.0, 0.0],
                  scale=[1.0, 2.0, 1.0], collision_size=[64.0, 64.0, 64.0])
    validate_level(_level(prop))
    uniform = copy.deepcopy(prop)
    uniform["properties"].update(scale=1.5, collision_size=None, rotation=None)
    validate_level(_level(uniform))


def test_another_entity_keeps_its_own_meaning_of_those_names():
    """The rules are a Prop's; a plugin entity's ``scale`` or ``rotation``
    is its own business."""
    validate_level(_level(_thing("portal", rotation=[0, 90, 0]),
                          _thing("gravity_well", scale="wide", rotation=45)))


# -- a brush that covers too much ground ------------------------------------------------------------

def test_a_brush_too_wide_to_file_in_the_grid_is_refused():
    """The world limit (1e7 units) let one brush cover 3.8e8 grid cells: a
    floor a million units wide took 50 s and 3.3 GB at Play start."""
    from engine import level_validation
    from engine.spatial import CELL_SIZE
    assert level_validation._CELL == CELL_SIZE

    level = _level()
    level["brushes"][0]["size"] = [1.0e6, 32, 1.0e6]
    with pytest.raises(ValueError, match="brushes\\[0\\]\\['size'\\] covers too much ground"):
        validate_level(level)
    level["brushes"][0]["size"] = [1.0e7, 32, 64]          # long and thin: 19,533 cells
    validate_level(level)
    level["brushes"][0]["size"] = [250000.0, 32, 250000.0]
    validate_level(level)

    with pytest.raises(ValueError, match="collision_size.*covers too much ground"):
        validate_level(_level(_thing("prop", collision_size=[1.0e6, 64, 1.0e6])))


def test_the_grid_leaves_out_a_brush_no_map_could_ask_for(capsys):
    """What validation cannot size without building it (an angled brush, a
    model's mesh) meets the same limit where it is filed."""
    from engine.spatial_grid import MAX_STATIC_CELLS, SpatialGrid
    from engine import level_validation
    assert MAX_STATIC_CELLS >= level_validation.MAX_FOOTPRINT_CELLS

    floor = {"pos": [0.0, -16.0, 0.0], "size": [1024.0, 32.0, 1024.0]}
    vast = {"pos": [0.0, -100.0, 0.0], "size": [2.0e6, 32.0, 2.0e6]}
    grid = SpatialGrid(cell_size=512.0)
    grid.populate([floor, vast])
    assert 0 < len(grid.cells) < 64
    assert all(vast not in bucket for bucket in grid.cells.values())
    assert any(floor in bucket for bucket in grid.cells.values())
    assert "too large to collide with" in capsys.readouterr().out
