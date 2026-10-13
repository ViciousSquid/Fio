"""Terrain .stamp export/import: merging must be idempotent."""

import json

import numpy as np
import pytest

from engine import terrain_stamp
from engine.terrain import Terrain


def _paint(terrain, key, value, region=(slice(10, 20), slice(30, 40))):
    res = terrain.TERRAIN_PAINT_RESOLUTION
    paint = terrain.texture_paint_maps.get(key)
    if paint is None:
        paint = np.zeros((res, res, 4), dtype=np.uint8)
        terrain.texture_paint_maps[key] = paint
    paint[region] = value


def _authored_terrain():
    terrain = Terrain(seed=3)
    terrain.apply_sculpt_at(10.0, 10.0, 32.0, 5.0)
    _paint(terrain, (0, 0), (200, 100, 50, 255))
    _paint(terrain, (1, -1), (10, 20, 30, 128))
    terrain.csg_subtractions.append([-8.0, 0.0, -8.0, 8.0, 50.0, 8.0])
    return terrain


def _snapshot(terrain):
    return (dict(terrain.sculpt_offsets),
            {k: v.copy() for k, v in terrain.texture_paint_maps.items()},
            [list(c) for c in terrain.csg_subtractions])


def _assert_same(a, b):
    assert a[0] == b[0]
    assert a[1].keys() == b[1].keys()
    for key in a[1]:
        assert np.array_equal(a[1][key], b[1][key])
    assert a[2] == b[2]


def test_round_trip_restores_cleared_work(tmp_path):
    terrain = _authored_terrain()
    before = _snapshot(terrain)
    path = tmp_path / "level.stamp"
    terrain_stamp.save_stamp_file(terrain, str(path))
    assert json.loads(path.read_text())["format"] == terrain_stamp.STAMP_FORMAT

    terrain.clear_sculpt()
    terrain.clear_texture_paint()
    terrain.csg_subtractions.clear()

    result = terrain_stamp.apply_stamp_file(terrain, str(path))
    assert result["sculpt_cells"] > 0
    assert result["paint_chunks"] == 2
    assert result["csg_cuts"] == 1
    _assert_same(_snapshot(terrain), before)


def test_applying_same_stamp_twice_does_not_duplicate():
    terrain = _authored_terrain()
    stamp = terrain_stamp.export_stamp(terrain)
    before = _snapshot(terrain)

    # The terrain still carries the stamped work: nothing changes.
    assert terrain_stamp.apply_stamp(terrain, stamp) == {
        "sculpt_cells": 0, "paint_chunks": 0, "csg_cuts": 0}
    assert terrain_stamp.apply_stamp(terrain, stamp) == {
        "sculpt_cells": 0, "paint_chunks": 0, "csg_cuts": 0}
    _assert_same(_snapshot(terrain), before)


def test_merge_keeps_work_outside_the_stamp():
    terrain = _authored_terrain()
    stamp = terrain_stamp.export_stamp(terrain)
    stamped = _snapshot(terrain)

    terrain.clear_sculpt()
    terrain.clear_texture_paint()
    # New work authored after the export, away from the stamped areas.
    terrain.apply_sculpt_at(2000.0, 2000.0, 16.0, 3.0)
    _paint(terrain, (0, 0), (1, 2, 3, 255), region=(slice(100, 110), slice(100, 110)))
    new_sculpt = dict(terrain.sculpt_offsets)

    terrain_stamp.apply_stamp(terrain, stamp)
    terrain_stamp.apply_stamp(terrain, stamp)

    for key, val in stamped[0].items():
        assert terrain.sculpt_offsets[key] == val
    for key, val in new_sculpt.items():
        assert terrain.sculpt_offsets[key] == val
    paint = terrain.texture_paint_maps[(0, 0)]
    assert np.array_equal(paint[10:20, 30:40], stamped[1][(0, 0)][10:20, 30:40])
    assert (paint[100:110, 100:110] == (1, 2, 3, 255)).all()


def test_stamp_resamples_onto_shifted_terrain_grid():
    terrain = _authored_terrain()
    stamp = terrain_stamp.export_stamp(terrain)

    target = Terrain(seed=9)
    target.offset_x = terrain.chunk_size * 0.5
    target.sculpt_grid_resolution = 8.0
    terrain_stamp.apply_stamp(target, stamp)
    again = _snapshot(target)
    assert terrain_stamp.apply_stamp(target, stamp) == {
        "sculpt_cells": 0, "paint_chunks": 0, "csg_cuts": 0}
    _assert_same(_snapshot(target), again)

    # Paint lands at the same world position on the shifted grid.
    cell = terrain.chunk_size / terrain.TERRAIN_PAINT_RESOLUTION
    wx = terrain.offset_x + 15.5 * cell
    wz = terrain.offset_z + 35.5 * cell
    cx = int(np.floor((wx - target.offset_x) / target.chunk_size))
    cz = int(np.floor((wz - target.offset_z) / target.chunk_size))
    ix = int((wx - target.offset_x - cx * target.chunk_size) // cell)
    iz = int((wz - target.offset_z - cz * target.chunk_size) // cell)
    assert tuple(target.texture_paint_maps[(cx, cz)][ix, iz]) == (200, 100, 50, 255)
    assert target.sculpt_offsets


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
