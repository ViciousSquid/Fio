"""Placed terrain stamps and .stamp files.

A stamp is a record (texture, X/Z position, radius...) that is also baked
into the terrain paint. The editor can select, move and nudge stamps; a
.stamp file carries only the records, never terrain height.
"""

import json

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="stamp textures are loaded with QImage")

from engine import terrain_stamp                              # noqa: E402
from engine.terrain import Terrain                            # noqa: E402


@pytest.fixture
def texture(tmp_path):
    from PyQt5.QtGui import QColor, QImage
    image = QImage(16, 16, QImage.Format_RGBA8888)
    image.fill(QColor(200, 100, 50, 255))
    path = str(tmp_path / "rock.png")
    assert image.save(path)
    return path


def _texel(terrain, x, z, maps=None):
    """RGBA of the paint texel under world (x, z)."""
    maps = terrain.texture_paint_maps if maps is None else maps
    cx = int(np.floor((x - terrain.offset_x) / terrain.chunk_size))
    cz = int(np.floor((z - terrain.offset_z) / terrain.chunk_size))
    cell = terrain.chunk_size / terrain.TERRAIN_PAINT_RESOLUTION
    ix = int((x - terrain.offset_x - cx * terrain.chunk_size) // cell)
    iz = int((z - terrain.offset_z - cz * terrain.chunk_size) // cell)
    paint = maps.get((cx, cz))
    return (0, 0, 0, 0) if paint is None else tuple(int(v) for v in paint[ix, iz])


def test_a_placed_stamp_is_baked_and_recorded(texture):
    terrain = Terrain(seed=1)
    stamp = terrain.add_paint_stamp(10.0, 20.0, 16.0, texture, feather=0.0)
    assert stamp is not None and terrain.paint_stamps == [stamp]
    assert _texel(terrain, 10.0, 20.0) == (200, 100, 50, 255)
    assert terrain.paint_stamp_at(12.0, 22.0) == stamp["id"]
    assert terrain.paint_stamp_at(100.0, 100.0) is None


def test_moving_a_stamp_rebakes_both_places(texture):
    terrain = Terrain(seed=1)
    stamp = terrain.add_paint_stamp(10.0, 10.0, 8.0, texture, feather=0.0)
    terrain.move_paint_stamps({stamp["id"]}, 300.0, 0.0)   # into another chunk
    assert _texel(terrain, 10.0, 10.0)[3] == 0
    assert _texel(terrain, 310.0, 10.0) == (200, 100, 50, 255)


def test_a_drag_updates_the_display_then_the_stored_paint(texture):
    terrain = Terrain(seed=1)
    stamp = terrain.add_paint_stamp(10.0, 10.0, 8.0, texture, feather=0.0)
    terrain.set_selected_stamps({stamp["id"]})
    terrain.move_paint_stamps({stamp["id"]}, 40.0, 0.0, defer=True)
    # Shown at the new place straight away; stored paint follows on finish.
    assert _texel(terrain, 50.0, 10.0, terrain._paint_display)[3] == 255
    terrain.finish_stamp_edit()
    assert _texel(terrain, 50.0, 10.0)[3] == 255
    assert _texel(terrain, 10.0, 10.0)[3] == 0


def test_selection_tints_the_display_but_never_the_saved_paint(texture):
    terrain = Terrain(seed=1)
    stamp = terrain.add_paint_stamp(10.0, 10.0, 8.0, texture, feather=0.0)
    terrain.set_selected_stamps({stamp["id"]})
    shown = _texel(terrain, 10.0, 10.0, terrain._paint_display)
    assert shown != (200, 100, 50, 255) and shown[3] == 255
    assert _texel(terrain, 10.0, 10.0) == (200, 100, 50, 255)

    reloaded = Terrain(seed=1)
    reloaded.from_dict(terrain.to_dict())
    assert _texel(reloaded, 10.0, 10.0) == (200, 100, 50, 255)

    terrain.set_selected_stamps(())
    assert not terrain._paint_display


def test_stamps_survive_a_map_round_trip_and_stay_editable(texture):
    terrain = Terrain(seed=1)
    stamp = terrain.add_paint_stamp(10.0, 10.0, 8.0, texture, feather=0.0)
    reloaded = Terrain(seed=1)
    reloaded.from_dict(terrain.to_dict())
    assert reloaded.paint_stamps == [stamp]
    reloaded.move_paint_stamps({stamp["id"]}, 0.0, 40.0)
    assert _texel(reloaded, 10.0, 10.0)[3] == 0
    assert _texel(reloaded, 10.0, 50.0)[3] == 255


def test_old_paint_stays_under_moved_stamps(texture):
    terrain = Terrain(seed=1)
    terrain.paint_texture_at(10.0, 10.0, 8.0, texture, feather=0.0)   # free paint
    reloaded = Terrain(seed=1)
    reloaded.from_dict(terrain.to_dict())
    stamp = reloaded.add_paint_stamp(60.0, 10.0, 8.0, texture, feather=0.0)
    reloaded.move_paint_stamps({stamp["id"]}, 0.0, 40.0)
    assert _texel(reloaded, 10.0, 10.0)[3] == 255
    assert _texel(reloaded, 60.0, 10.0)[3] == 0


def test_stamp_file_holds_positions_only(texture, tmp_path):
    terrain = Terrain(seed=1)
    terrain.apply_sculpt_at(10.0, 10.0, 32.0, 5.0)
    terrain.add_paint_stamp(10.0, 10.0, 8.0, texture, feather=0.0)
    path = tmp_path / "level.stamp"
    terrain_stamp.save_stamp_file(terrain, str(path))
    data = json.loads(path.read_text())
    assert data["format"] == terrain_stamp.STAMP_FORMAT
    assert set(data) == {"format", "version", "stamps"}
    assert set(data["stamps"][0]) == {
        "id", "texture", "x", "z", "radius", "angle", "feather", "opacity"}


def test_importing_onto_changed_terrain_bakes_the_stamps(texture, tmp_path):
    terrain = Terrain(seed=1)
    terrain.add_paint_stamp(10.0, 10.0, 8.0, texture, feather=0.0)
    terrain.add_paint_stamp(300.0, -40.0, 12.0, texture, feather=0.0)
    path = tmp_path / "level.stamp"
    terrain_stamp.save_stamp_file(terrain, str(path))

    changed = Terrain(seed=99)             # a different, re-generated map
    changed.apply_sculpt_at(10.0, 10.0, 64.0, 40.0)
    sculpt = dict(changed.sculpt_offsets)
    assert terrain_stamp.apply_stamp_file(changed, str(path)) == 2
    assert _texel(changed, 10.0, 10.0) == (200, 100, 50, 255)
    assert _texel(changed, 300.0, -40.0) == (200, 100, 50, 255)
    assert changed.sculpt_offsets == sculpt          # height untouched


def test_applying_the_same_stamp_file_twice_does_not_duplicate(texture, tmp_path):
    terrain = Terrain(seed=1)
    terrain.add_paint_stamp(10.0, 10.0, 8.0, texture, feather=0.5, opacity=0.5)
    path = tmp_path / "level.stamp"
    terrain_stamp.save_stamp_file(terrain, str(path))
    paint = {k: v.copy() for k, v in terrain.texture_paint_maps.items()}

    assert terrain_stamp.apply_stamp_file(terrain, str(path)) == 0
    assert terrain_stamp.apply_stamp_file(terrain, str(path)) == 0
    assert len(terrain.paint_stamps) == 1
    for key, value in paint.items():
        assert np.array_equal(terrain.texture_paint_maps[key], value)

    # A stamp moved since the export goes back to the file's position.
    stamp_id = terrain.paint_stamps[0]["id"]
    terrain.move_paint_stamps({stamp_id}, 100.0, 0.0)
    assert terrain_stamp.apply_stamp_file(terrain, str(path)) == 1
    assert len(terrain.paint_stamps) == 1
    assert terrain.paint_stamps[0]["x"] == 10.0


def test_rejects_non_stamp_files(tmp_path):
    path = tmp_path / "bad.stamp"
    path.write_text('{"format": "something_else"}')
    with pytest.raises(terrain_stamp.StampError):
        terrain_stamp.load_stamp_file(str(path))
    path.write_text("not json")
    with pytest.raises(terrain_stamp.StampError):
        terrain_stamp.load_stamp_file(str(path))


def test_default_stamp_path_follows_map_name(tmp_path):
    map_path = tmp_path / "maps" / "forest.json"
    assert terrain_stamp.default_stamp_path(str(map_path)) == str(
        tmp_path / "maps" / "forest.stamp")
    assert terrain_stamp.default_stamp_path(None, str(tmp_path)).endswith(
        "untitled.stamp")


def test_vector_and_scalar_stamp_lookups_agree(texture):
    import random
    rng = random.Random(4)
    records = [Terrain.make_paint_stamp(rng.uniform(-900, 900), rng.uniform(-900, 900),
                                        rng.uniform(4, 300), texture)
               for _ in range(Terrain.STAMP_VECTOR_MIN + 40)]
    big = Terrain(seed=1)
    big.paint_stamps = records
    big._invalidate_stamp_index()
    big._stamp_index()
    assert big._stamp_range_arr is not None              # NumPy path
    for key in [(cx, cz) for cx in range(-5, 5) for cz in range(-5, 5)]:
        scalar = [i for i, (x0, x1, z0, z1) in enumerate(big._stamp_ranges)
                  if x0 <= key[0] <= x1 and z0 <= key[1] <= z1]
        assert big._stamps_over(key) == scalar

    # Moves keep the vector index in step with the records.
    big.move_paint_stamps({records[3]["id"]}, 512.0, -256.0, defer=True)
    assert tuple(big._stamp_range_arr[3]) == big._stamp_range(records[3])
