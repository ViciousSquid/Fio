"""Terrain paint: 256 texels a chunk, textures tiled, old maps converted.

Paint was 128 a chunk with one copy of the image stretched over the stamp
(muddy, and a different texture scale for every stamp size). Now the
texture repeats at its own scale in the world and a stamp is a window onto
it; a map saved before is converted when it is loaded: stamps are baked
again from their records, paint with no record behind it is resampled."""

import json

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="stamp textures are loaded with QImage")
pytestmark = pytest.mark.qt

from engine.terrain import Terrain                            # noqa: E402


@pytest.fixture
def checker(tmp_path):
    """64 x 64 pixels: 8-pixel squares of two colours."""
    from PyQt5.QtGui import QColor, QImage
    image = QImage(64, 64, QImage.Format_RGBA8888)
    for y in range(64):
        for x in range(64):
            on = ((x // 8) + (y // 8)) % 2
            image.setPixelColor(x, y, QColor(230, 40, 40) if on else QColor(20, 60, 220))
    path = str(tmp_path / "checker.png")
    assert image.save(path)
    return path


def _old_terrain_type():
    """A Terrain as it was: 128 a chunk."""
    return type('OldTerrain', (Terrain,), {'TERRAIN_PAINT_RESOLUTION': 128})


def _old_map(texture, stamps):
    """Terrain data as an old build saved it: 128, stamps with no 'tiled'."""
    old = _old_terrain_type()(seed=1)
    for x, z, radius in stamps:
        old._bake_texture_rect((x - radius, z - radius, x + radius, z + radius),
                               texture, feather=0.0)
    data = json.loads(json.dumps(old.to_dict()))
    assert data['texture_paint_resolution'] == 128
    data['paint_stamps'] = [
        {'id': 'old%d' % i, 'texture': texture, 'x': x, 'z': z, 'radius': radius,
         'angle': 0.0, 'feather': 0.0, 'opacity': 1.0}
        for i, (x, z, radius) in enumerate(stamps)]
    return data


def test_paint_is_256_a_chunk():
    assert Terrain.TERRAIN_PAINT_RESOLUTION == 256
    terrain = Terrain(seed=1)
    assert terrain.publish().paint_resolution == 256


def test_a_stamp_is_a_window_onto_the_tiled_texture(checker):
    """The texture's scale is the world's, not the stamp's: a small stamp
    and a large one put the same texels where they overlap."""
    small, large = Terrain(seed=1), Terrain(seed=1)
    small.add_paint_stamp(128.0, 128.0, 20.0, checker, feather=0.0)
    large.add_paint_stamp(128.0, 128.0, 110.0, checker, feather=0.0)
    a, b = small.texture_paint_maps[(0, 0)], large.texture_paint_maps[(0, 0)]
    both = (a[..., 3] == 255) & (b[..., 3] == 255)
    assert both.sum() > 800
    assert np.array_equal(a[both], b[both])
    # And it repeats: 64 pixels at 0.5 units a pixel is every 32 units.
    assert np.array_equal(b[100:140, 128], b[132:172, 128])
    assert len({tuple(v) for v in b[100:132, 128, :3]}) > 1


def test_stamps_of_one_texture_join(checker):
    terrain, whole = Terrain(seed=1), Terrain(seed=1)
    terrain.add_paint_stamp(100.0, 128.0, 40.0, checker, feather=0.0)
    terrain.add_paint_stamp(150.0, 128.0, 40.0, checker, feather=0.0)
    whole.add_paint_stamp(125.0, 128.0, 100.0, checker, feather=0.0)
    a, b = terrain.texture_paint_maps[(0, 0)], whole.texture_paint_maps[(0, 0)]
    painted = a[..., 3] == 255
    assert np.array_equal(a[painted], b[painted])


def test_a_stretched_stamp_is_still_possible(checker):
    terrain = Terrain(seed=1)
    stamp = terrain.make_paint_stamp(128.0, 128.0, 64.0, checker, feather=0.0, tiled=False)
    terrain.merge_paint_stamps([stamp])
    assert terrain.paint_stamps[0]['tiled'] is False
    paint = terrain.texture_paint_maps[(0, 0)]
    # One copy over 128 units: a square of the checker is 16 units wide.
    row = paint[64:192, 128, :3].astype(int)
    changes = int(np.count_nonzero(np.diff((row[:, 0] > 125).astype(int))))
    assert 5 <= changes <= 9


def test_an_old_map_is_converted_when_loaded(checker):
    stamps = [(100.0, 100.0, 60.0), (300.0, 80.0, 50.0)]
    loaded = Terrain(seed=1)
    loaded.from_dict(_old_map(checker, stamps))
    fresh = Terrain(seed=1)
    for x, z, radius in stamps:
        fresh.add_paint_stamp(x, z, radius, checker, feather=0.0)

    assert all(s['tiled'] for s in loaded.paint_stamps)
    assert set(loaded.texture_paint_maps) == set(fresh.texture_paint_maps)
    for key, paint in fresh.texture_paint_maps.items():
        assert loaded.texture_paint_maps[key].shape == (256, 256, 4)
        assert np.array_equal(loaded.texture_paint_maps[key], paint)
    # Saved again it is a new map: no second conversion, same pixels.
    again = Terrain(seed=1)
    data = json.loads(json.dumps(loaded.to_dict()))
    assert data['texture_paint_resolution'] == 256
    assert all(s['tiled'] is True for s in data['paint_stamps'])
    again.from_dict(data)
    for key, paint in fresh.texture_paint_maps.items():
        assert np.array_equal(again.texture_paint_maps[key], paint)


def test_old_paint_with_no_record_is_resampled(tmp_path):
    from PyQt5.QtGui import QColor, QImage
    image = QImage(16, 16, QImage.Format_RGBA8888)
    image.fill(QColor(200, 100, 50, 255))
    solid = str(tmp_path / "solid.png")
    assert image.save(solid)
    data = _old_map(solid, [(100.0, 100.0, 60.0)])
    del data['paint_stamps']
    terrain = Terrain(seed=1)
    terrain.from_dict(data)
    paint = terrain.texture_paint_maps[(0, 0)]
    assert paint.shape == (256, 256, 4)
    old = _old_terrain_type()(seed=1)
    old._bake_texture_rect((40.0, 40.0, 160.0, 160.0), solid, feather=0.0)
    before = old.texture_paint_maps[(0, 0)]
    # The same area is covered (to a texel of the old map)...
    assert abs(np.count_nonzero(paint[..., 3] > 127) / 4.0
               - np.count_nonzero(before[..., 3] > 127)) < 300
    # ...in the old colour, right to the edge: a half-covered texel keeps
    # its colour rather than fading towards the black of unpainted ones.
    covered = paint[paint[..., 3] > 0]
    assert len(covered) and np.all(np.abs(covered[:, :3].astype(int) - (200, 100, 50)) <= 2)
    assert np.any((paint[..., 3] > 0) & (paint[..., 3] < 255))
    assert terrain.texture_paint_base == terrain.texture_paint_maps


def test_resampling_to_the_same_side_is_the_same_array():
    paint = np.zeros((256, 256, 4), dtype=np.uint8)
    assert Terrain._resample_paint(paint, 256) is paint


def test_the_tiled_image_is_reduced_to_the_paint_density(checker):
    terrain = Terrain(seed=1)
    image, per_unit = terrain._tile_image(checker)
    # 2 pixels a unit, 1 texel a unit: halved.
    assert image.shape == (32, 32, 4) and per_unit == 1.0


def _scatter(texture, count=10):
    import random
    rnd = random.Random(5)
    terrain = Terrain(seed=1)
    for _ in range(count):
        terrain.add_paint_stamp(rnd.uniform(-200, 200), rnd.uniform(-200, 200),
                                rnd.uniform(40, 120), texture,
                                feather=rnd.choice((0.0, 0.3)))
    return terrain


@pytest.mark.parametrize("which", [0, 4, -1])
def test_a_drag_bakes_only_the_dragged_stamp_and_ends_exact(checker, which, monkeypatch):
    """What lies under and over a dragged stamp is kept, not baked again a
    step; the stored paint at the end is the exact composition."""
    terrain = _scatter(checker)
    stamp = terrain.paint_stamps[which]
    terrain.set_selected_stamps({stamp['id']})
    terrain.move_paint_stamps({stamp['id']}, 3.0, 2.0, defer=True)     # fills the cache

    baked = []
    real = terrain._bake_stamp
    monkeypatch.setattr(terrain, '_bake_stamp',
                        lambda s, *a, **k: (baked.append(s['id']), real(s, *a, **k))[1])
    terrain.move_paint_stamps({stamp['id']}, 3.0, 2.0, defer=True)
    assert baked and set(baked) == {stamp['id']}
    monkeypatch.undo()

    # The preview is the exact picture to a rounding step, tint aside.
    terrain.finish_stamp_edit()
    terrain.set_selected_stamps(())
    fresh = Terrain(seed=1)
    fresh.merge_paint_stamps([dict(s) for s in terrain.paint_stamps])
    assert set(fresh.texture_paint_maps) == set(terrain.texture_paint_maps)
    for key, paint in fresh.texture_paint_maps.items():
        assert np.array_equal(terrain.texture_paint_maps[key], paint), key


def test_the_drag_preview_is_within_a_rounding_step_of_the_exact_paint(checker):
    terrain = _scatter(checker)
    stamp = terrain.paint_stamps[0]                 # everything else is above it
    terrain.set_selected_stamps({stamp['id']})
    terrain.move_paint_stamps({stamp['id']}, 20.0, -10.0, defer=True)
    keys = terrain._stamp_chunks(stamp)
    for key in keys:
        preview = terrain._compose_paint_chunk(key, preview=True)
        exact = terrain._compose_paint_chunk(key)
        if exact is None:
            assert preview is None
            continue
        assert np.abs(preview.astype(int) - exact.astype(int)).max() <= 2


def test_the_cache_is_dropped_when_what_it_holds_changes(checker):
    terrain = _scatter(checker)
    a, b = terrain.paint_stamps[2], terrain.paint_stamps[7]
    terrain.set_selected_stamps({a['id']})
    terrain.move_paint_stamps({a['id']}, 1.0, 0.0, defer=True)
    assert terrain._drag_cache
    terrain.move_paint_stamps({b['id']}, 30.0, 0.0)      # an unselected stamp moves
    assert not terrain._drag_cache or all(
        np.array_equal(terrain._compose_paint_chunk(k), terrain.texture_paint_maps[k])
        for k in terrain._stamp_chunks(b) if k in terrain.texture_paint_maps)
    terrain.finish_stamp_edit()
    terrain.set_selected_stamps(())
    assert not terrain._drag_cache
    fresh = Terrain(seed=1)
    fresh.merge_paint_stamps([dict(s) for s in terrain.paint_stamps])
    for key, paint in fresh.texture_paint_maps.items():
        assert np.array_equal(terrain.texture_paint_maps[key], paint), key


def test_dragging_the_newest_stamp_into_a_fresh_chunk_bakes_only_it(checker, monkeypatch):
    """A chunk the drag enters is stored as it is without the selection:
    nothing under the dragged stamp is baked again."""
    terrain = Terrain(seed=1)
    for x in (300.0, 330.0, 360.0, 390.0):          # chunk (1, 0), well inside
        terrain.add_paint_stamp(x, 128.0, 50.0, checker)
    mover = terrain.add_paint_stamp(128.0, 128.0, 40.0, checker)    # chunk (0, 0)
    terrain.set_selected_stamps({mover['id']})
    baked = []
    real = terrain._bake_stamp
    monkeypatch.setattr(terrain, '_bake_stamp',
                        lambda s, *a, **k: (baked.append(s['id']), real(s, *a, **k))[1])
    terrain.move_paint_stamps({mover['id']}, 110.0, 0.0, defer=True)   # crosses into (1, 0)
    assert (1, 0) in terrain._stamp_chunks(mover)
    assert set(baked) == {mover['id']}
    monkeypatch.undo()

    terrain.finish_stamp_edit()
    terrain.set_selected_stamps(())
    fresh = Terrain(seed=1)
    fresh.merge_paint_stamps([dict(s) for s in terrain.paint_stamps])
    assert set(fresh.texture_paint_maps) == set(terrain.texture_paint_maps)
    for key, paint in fresh.texture_paint_maps.items():
        assert np.array_equal(terrain.texture_paint_maps[key], paint), key


def test_a_clipped_bake_is_the_whole_bake_inside_the_clip(checker):
    whole, part = Terrain(seed=1), Terrain(seed=1)
    stamp = whole.make_paint_stamp(120.0, 130.0, 90.0, checker, feather=0.4, opacity=0.7)
    key, clip = (0, 0), (40, 150, 60, 199)
    a = {key: np.full((256, 256, 4), 90, dtype=np.uint8)}
    b = {key: a[key].copy()}
    whole._bake_stamp(stamp, a, {key})
    part._bake_stamp(stamp, b, {key}, clip=clip)
    cut = (slice(clip[0], clip[1] + 1), slice(clip[2], clip[3] + 1))
    assert np.array_equal(a[key][cut], b[key][cut])
    outside = np.ones((256, 256), bool)
    outside[cut] = False
    assert np.all(b[key][outside] == 90)


def test_chunks_compressed_together_are_the_chunks_compressed_alone(checker):
    from engine import terrain as terrain_module
    terrain = _scatter(checker)
    paints = list(terrain.texture_paint_maps.values())
    assert len(paints) > 2
    assert terrain_module._compress_paint(paints) == [
        terrain_module._pack_paint(p) for p in paints]


def test_the_selection_tint_shows_and_is_never_stored(checker):
    terrain = Terrain(seed=1)
    stamp = terrain.add_paint_stamp(128.0, 128.0, 60.0, checker, feather=0.0)
    stored = terrain.texture_paint_maps[(0, 0)].copy()
    terrain.set_selected_stamps({stamp['id']})
    shown = terrain._paint_display[(0, 0)]
    assert not np.array_equal(shown, stored)
    rgb, mix = Terrain.STAMP_SELECT_TINT
    want = stored[128, 128, :3] * (1.0 - mix) + np.asarray(rgb) * 255.0 * mix
    assert np.all(np.abs(shown[128, 128, :3].astype(float) - want) <= 1.5)
    assert np.array_equal(terrain.texture_paint_maps[(0, 0)], stored)
    terrain.set_selected_stamps(())
    assert (0, 0) not in terrain._paint_display
