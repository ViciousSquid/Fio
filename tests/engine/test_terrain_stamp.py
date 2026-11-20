"""Placed terrain stamps and .paint files.

A stamp is a record (texture, X/Z position, radius...) that is also baked
into the terrain paint. The editor can select, move and nudge stamps; a
.paint file carries only the records, never terrain height.
"""

import json

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="stamp textures are loaded with QImage")
pytestmark = pytest.mark.qt

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


def test_a_paint_file_holds_the_paint_and_the_stamps_but_no_height(texture, tmp_path):
    terrain = Terrain(seed=1)
    terrain.apply_sculpt_at(10.0, 10.0, 32.0, 5.0)
    terrain.add_paint_stamp(10.0, 10.0, 8.0, texture, feather=0.0)
    path = tmp_path / "level.paint"
    terrain_stamp.save_stamp_file(terrain, str(path))
    data = json.loads(path.read_text())
    assert data["format"] == terrain_stamp.PAINT_FORMAT
    assert set(data) == {"format", "version", "stamps", "paint"}
    assert {"id", "texture", "x", "z", "radius", "angle", "feather",
            "opacity"} <= set(data["stamps"][0])
    assert set(data["paint"]) <= set(terrain_stamp.PAINT_KEYS)
    assert data["paint"]["texture_paint"]
    assert "sculpt" not in json.dumps(data), "a paint file holds no terrain height"


def test_importing_onto_changed_terrain_bakes_the_stamps(texture, tmp_path):
    terrain = Terrain(seed=1)
    terrain.add_paint_stamp(10.0, 10.0, 8.0, texture, feather=0.0)
    terrain.add_paint_stamp(300.0, -40.0, 12.0, texture, feather=0.0)
    path = tmp_path / "level.paint"
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
    path = tmp_path / "level.paint"
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
    path = tmp_path / "bad.paint"
    path.write_text('{"format": "something_else"}')
    with pytest.raises(terrain_stamp.StampError):
        terrain_stamp.load_stamp_file(str(path))
    path.write_text("not json")
    with pytest.raises(terrain_stamp.StampError):
        terrain_stamp.load_stamp_file(str(path))


def test_default_stamp_path_follows_map_name(tmp_path):
    map_path = tmp_path / "maps" / "forest.json"
    assert terrain_stamp.default_stamp_path(str(map_path)) == str(
        tmp_path / "maps" / "forest.paint")
    assert terrain_stamp.default_stamp_path(None, str(tmp_path)).endswith(
        "untitled.paint")


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


@pytest.mark.perf
def test_a_stamp_click_recompresses_only_the_chunks_it_touched(texture, monkeypatch):
    import zlib
    terrain = Terrain(seed=1)
    for i in range(12):
        terrain.add_paint_stamp(i * 300.0, 0.0, 8.0, texture, feather=0.0)
    terrain.to_dict()                                   # warm the cache

    calls = []
    real = zlib.compress
    monkeypatch.setattr(zlib, "compress", lambda *a: calls.append(1) or real(*a))
    stamp = terrain.add_paint_stamp(10.0, 10.0, 8.0, texture, feather=0.0)
    data = terrain.to_dict()
    assert len(calls) == len(terrain._stamp_chunks(stamp)) == 1
    terrain.to_dict()
    assert len(calls) == 1                              # nothing changed

    reloaded = Terrain(seed=1)
    reloaded.from_dict(data)
    assert _texel(reloaded, 10.0, 10.0) == (200, 100, 50, 255)
    assert len(reloaded.paint_stamps) == 13


def test_cached_terrain_data_follows_every_change(texture):
    terrain = Terrain(seed=1)
    stamp = terrain.add_paint_stamp(10.0, 10.0, 8.0, texture, feather=0.0)
    terrain.apply_sculpt_at(0.0, 0.0, 16.0, 2.0)
    terrain.heightmap_data = np.zeros((4, 4), np.float32)
    first = terrain.to_dict()

    terrain.move_paint_stamps({stamp["id"]}, 300.0, 0.0)
    terrain.apply_sculpt_at(500.0, 500.0, 16.0, 2.0)
    terrain.heightmap_data = np.ones((4, 4), np.float32)
    second = terrain.to_dict()
    assert second["texture_paint"] != first["texture_paint"]
    assert second["sculpt_tiles"] != first["sculpt_tiles"]
    assert second["heightmap_blob"] != first["heightmap_blob"]

    reloaded = Terrain(seed=1)
    reloaded.from_dict(second)
    assert _texel(reloaded, 10.0, 10.0)[3] == 0
    assert _texel(reloaded, 310.0, 10.0) == (200, 100, 50, 255)
    assert reloaded.sculpt_offsets == terrain.sculpt_offsets
    assert float(reloaded.heightmap_data.max()) == 1.0


# -- a saved map keeps its stamps beside it, not in its JSON -----------------

def _stamped_level(texture):
    terrain = Terrain(seed=5)
    terrain.add_paint_stamp(100.0, 40.0, 64.0, texture)
    terrain.add_paint_stamp(-50.0, 10.0, 32.0, texture, feather=0.5)
    return terrain, {"version": 5, "brushes": [], "things": [],
                     "terrain_data": terrain.to_dict()}


def test_a_saved_map_keeps_its_stamps_in_a_file_beside_it(tmp_path, texture):
    import json
    terrain, level = _stamped_level(texture)
    map_path = str(tmp_path / "forest.json")

    for_map, stamps = terrain_stamp.split_level(level)
    written = terrain_stamp.write_map_stamps(map_path, stamps)

    for key in ("paint_stamps", "texture_paint", "texture_paint_base",
                "texture_paint_resolution"):
        assert key not in for_map["terrain_data"], key
    assert "paint_stamps" in level["terrain_data"], "the live level was changed"
    assert level["terrain_data"]["texture_paint"], "the live level was changed"
    assert stamps["paint"]["texture_paint"] == level["terrain_data"]["texture_paint"]
    assert written == str(tmp_path / "forest.paint")
    text = open(written, encoding="utf-8").read()
    assert "\n" not in text and ", " not in text and ": " not in text, "not minified"
    assert [s["id"] for s in json.loads(text)["stamps"]] == [
        s["id"] for s in terrain.paint_stamps]


def test_opening_the_map_gives_the_terrain_its_stamps_back(tmp_path, texture):
    import json
    terrain, level = _stamped_level(texture)
    map_path = str(tmp_path / "forest.json")
    for_map, stamps = terrain_stamp.split_level(level)
    terrain_stamp.write_map_stamps(map_path, stamps)
    read = json.loads(json.dumps(for_map))

    joined = terrain_stamp.join_level(read, map_path)

    assert joined["terrain_data"] == json.loads(json.dumps(level["terrain_data"])), (
        "the map and its stamp file together are not what was saved")
    again = Terrain(seed=5)
    again.from_dict(joined["terrain_data"])
    assert [s["id"] for s in again.paint_stamps] == [s["id"] for s in terrain.paint_stamps]
    for key in ("paint_stamps", "texture_paint", "texture_paint_base"):
        assert again.to_dict().get(key) == terrain.to_dict().get(key), key


def test_a_map_without_its_paint_file_has_unpainted_ground(tmp_path, texture):
    import json
    terrain, level = _stamped_level(texture)
    for_map, _stamps = terrain_stamp.split_level(level)
    read = terrain_stamp.join_level(json.loads(json.dumps(for_map)),
                                    str(tmp_path / "forest.json"))

    alone = Terrain(seed=5)
    alone.from_dict(read["terrain_data"])
    assert not alone.paint_stamps and not alone.texture_paint_maps


def test_a_stamp_file_from_an_earlier_build_is_still_read(tmp_path, texture):
    """<map>.terrainstamps (stamps only) beside a map that kept its paint."""
    import json
    terrain, level = _stamped_level(texture)
    map_path = tmp_path / "forest.json"
    old_map = json.loads(json.dumps(level))
    records = old_map["terrain_data"].pop("paint_stamps")
    (tmp_path / "forest.terrainstamps").write_text(json.dumps(
        {"format": "terrain_stamp", "version": 1, "stamps": records}))

    # Such a map carries its paint, so it is left as it is ...
    assert terrain_stamp.join_level(old_map, str(map_path)) is old_map
    # ... and a map with no paint of its own takes the file's stamps.
    bare, _ = terrain_stamp.split_level(level)
    joined = terrain_stamp.join_level(json.loads(json.dumps(bare)), str(map_path))
    assert [s["id"] for s in joined["terrain_data"]["paint_stamps"]] == [
        s["id"] for s in terrain.paint_stamps]


def test_a_map_that_still_carries_its_stamps_keeps_them(tmp_path, texture):
    _terrain, level = _stamped_level(texture)
    other, other_level = _stamped_level(texture)
    map_path = str(tmp_path / "forest.json")
    terrain_stamp.write_map_stamps(map_path, terrain_stamp.split_level(other_level)[1])
    before = [s["id"] for s in level["terrain_data"]["paint_stamps"]]

    joined = terrain_stamp.join_level(level, map_path)

    assert [s["id"] for s in joined["terrain_data"]["paint_stamps"]] == before


def test_removing_every_stamp_empties_the_file(tmp_path, texture):
    import json
    terrain, level = _stamped_level(texture)
    map_path = str(tmp_path / "forest.json")
    terrain_stamp.write_map_stamps(map_path, terrain_stamp.split_level(level)[1])
    terrain.clear_texture_paint()
    level["terrain_data"] = terrain.to_dict()

    terrain_stamp.write_map_stamps(map_path, terrain_stamp.split_level(level)[1])

    path = tmp_path / "forest.paint"
    assert json.loads(path.read_text())["stamps"] == []


def test_clearing_the_paint_does_not_bring_back_an_earlier_build_s_stamps(
        tmp_path, texture):
    """A map with a legacy ``.terrainstamps`` beside it, saved with its paint
    cleared: no ``.paint`` was written (there was nothing to keep and none to
    empty), so the next load read the legacy file and the cleared stamps came
    back. An empty ``.paint`` is written over it instead; the user's legacy
    file itself is left alone."""
    import json
    terrain, level = _stamped_level(texture)
    map_path = tmp_path / "forest.json"
    legacy = tmp_path / "forest.terrainstamps"
    legacy.write_text(json.dumps({"format": "terrain_stamp", "version": 1,
                                  "stamps": level["terrain_data"]["paint_stamps"]}))
    terrain.clear_texture_paint()
    cleared = {"version": 5, "brushes": [], "things": [],
               "terrain_data": terrain.to_dict()}

    for_map, paint = terrain_stamp.split_level(cleared)
    assert terrain_stamp.write_map_stamps(str(map_path), paint) is not None
    reopened = terrain_stamp.join_level(json.loads(json.dumps(for_map)), str(map_path))

    assert not reopened["terrain_data"].get("paint_stamps")
    assert legacy.exists()
    assert json.loads((tmp_path / "forest.paint").read_text())["stamps"] == []


def test_a_map_with_no_stamps_gets_no_stamp_file(tmp_path):
    level = {"version": 5, "brushes": [], "things": [],
             "terrain_data": Terrain(seed=5).to_dict()}
    map_path = str(tmp_path / "plain.json")
    assert terrain_stamp.write_map_stamps(map_path, terrain_stamp.split_level(level)[1]) is None
    assert terrain_stamp.write_map_stamps(
        map_path, terrain_stamp.split_level({"brushes": []})[1]) is None
    assert list(tmp_path.iterdir()) == []


def test_the_editor_saves_and_opens_a_map_with_its_stamp_file(
        main_window, tmp_path, texture):
    window = main_window
    terrain = Terrain(seed=5)
    terrain.add_paint_stamp(100.0, 40.0, 64.0, texture)
    window.terrain = terrain
    window.state.terrain_data = terrain.to_dict()
    window.file_path = str(tmp_path / "forest.json")

    window.save_level()

    saved = json.loads((tmp_path / "forest.json").read_text())
    assert "paint_stamps" not in saved["terrain_data"]
    assert (tmp_path / "forest.paint").exists()

    assert window.load_level_file(str(tmp_path / "forest.json"))
    assert [s["id"] for s in window.terrain.paint_stamps] == [
        s["id"] for s in terrain.paint_stamps]
    assert window.state.terrain_data["paint_stamps"]
    window.unsaved_changes = False


# -- a map whose paint file has gone missing says so --------------------------

def test_a_saved_map_says_it_has_a_paint_file(tmp_path, texture):
    _terrain, level = _stamped_level(texture)
    for_map, _paint = terrain_stamp.split_level(level)
    assert for_map["terrain_data"]["paint_file"] is True
    plain, nothing = terrain_stamp.split_level(
        {"terrain_data": Terrain(seed=5).to_dict()})
    assert "paint_file" not in plain["terrain_data"]
    # Saved again once the paint is cleared, the map stops saying so.
    cleared = dict(for_map, terrain_data=dict(for_map["terrain_data"]))
    assert "paint_file" not in terrain_stamp.split_level(cleared)[0]["terrain_data"]


def test_the_missing_paint_file_is_named(tmp_path, texture):
    _terrain, level = _stamped_level(texture)
    for_map, paint = terrain_stamp.split_level(level)
    map_path = str(tmp_path / "forest.json")

    assert terrain_stamp.missing_paint_file(for_map, map_path) == "forest.paint"
    assert terrain_stamp.missing_paint_message("forest.paint") == (
        "forest.paint was not found so this map will not display paint/stamps")
    terrain_stamp.write_map_stamps(map_path, paint)
    assert terrain_stamp.missing_paint_file(for_map, map_path) is None
    assert terrain_stamp.missing_paint_file(
        {"terrain_data": Terrain(seed=5).to_dict()}, map_path) is None


def test_the_editor_warns_when_a_map_s_paint_file_is_missing(
        main_window, tmp_path, texture):
    window = main_window
    shown = []
    real = window.show_toast
    window.show_toast = lambda message, *a, **k: shown.append(str(message)) or real(
        message, *a, **k)
    terrain = Terrain(seed=5)
    terrain.add_paint_stamp(100.0, 40.0, 64.0, texture)
    window.terrain = terrain
    window.state.terrain_data = terrain.to_dict()
    window.file_path = str(tmp_path / "forest.json")
    window.save_level()
    (tmp_path / "forest.paint").unlink()
    shown.clear()

    assert window.load_level_file(str(tmp_path / "forest.json"))

    assert ("forest.paint was not found so this map will not display "
            "paint/stamps") in shown
    assert not window.terrain.paint_stamps and not window.terrain.texture_paint_maps

    # A map that never had paint loads without the warning.
    window.terrain.clear_texture_paint()
    window.state.terrain_data = window.terrain.to_dict()
    window.save_level()
    shown.clear()
    assert window.load_level_file(str(tmp_path / "forest.json"))
    assert not [m for m in shown if "was not found" in m]
    window.unsaved_changes = False
