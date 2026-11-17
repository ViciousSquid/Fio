"""No grass on painted ground: a stamp or a painted path is a clearing."""

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="stamp textures are loaded with QImage")
pytestmark = pytest.mark.qt

from engine.terrain import Terrain                            # noqa: E402


@pytest.fixture
def texture(tmp_path):
    from PyQt5.QtGui import QColor, QImage
    image = QImage(16, 16, QImage.Format_RGBA8888)
    image.fill(QColor(120, 110, 90, 255))
    path = str(tmp_path / "path.png")
    assert image.save(path)
    return path


def _meadow():
    """A flat terrain with grass everywhere it can grow, chunk (0, 0) built."""
    terrain = Terrain(seed=3)
    terrain.enabled = True
    terrain.flat_mode = True
    terrain.set_grass(True, 0.05)
    terrain.grass_height_range = (0.0, 1.0)
    slot = terrain.table.allocate(0, 0) if hasattr(terrain.table, 'allocate') else None
    return terrain, slot


def _blades(terrain):
    table = terrain.table
    cap = table._capacity
    slots = np.flatnonzero(table.live[:cap] & (table.coord[:cap, 0] == 0)
                           & (table.coord[:cap, 1] == 0))
    assert len(slots) == 1
    return terrain._generate_grass_blades(int(slots[0]))


def _resident(terrain):
    """Bring chunk (0, 0) in the way the editor does."""
    import glm
    from engine.terrain_frame import frustum_planes
    projection = glm.perspective(glm.radians(70.0), 1.0, 1.0, 4000.0)
    eye = glm.vec3(128.0, 300.0, 128.0)
    view = glm.lookAt(eye, glm.vec3(128.0, 0.0, 128.5), glm.vec3(0, 1, 0))
    for _ in range(40):
        terrain.advance(eye, frustum_planes(projection * view))
    return terrain


def test_a_stamp_is_a_clearing(texture):
    terrain = _resident(_meadow()[0])
    before = _blades(terrain)
    inside = lambda b: np.hypot(b[:, 0] - 128.0, b[:, 2] - 128.0) < 60.0
    assert inside(before).sum() > 20, "the test needs grass where the stamp goes"

    terrain.add_paint_stamp(128.0, 128.0, 60.0, texture, feather=0.0)
    after = _blades(terrain)
    # To a paint texel (one unit) of the stamp's edge.
    within = lambda b, r: np.hypot(b[:, 0] - 128.0, b[:, 2] - 128.0) < r
    assert within(after, 58.0).sum() == 0
    # Every blade outside the stamp is the blade it was.
    assert np.array_equal(after[~within(after, 62.0)], before[~within(before, 62.0)])


def test_a_feathered_edge_thins_the_grass(texture):
    terrain = _resident(_meadow()[0])
    before = _blades(terrain)
    terrain.add_paint_stamp(128.0, 128.0, 100.0, texture, feather=0.6)
    after = _blades(terrain)
    ring = lambda b: (np.hypot(b[:, 0] - 128.0, b[:, 2] - 128.0) > 45.0) & (
        np.hypot(b[:, 0] - 128.0, b[:, 2] - 128.0) < 100.0)
    core = lambda b: np.hypot(b[:, 0] - 128.0, b[:, 2] - 128.0) < 38.0
    assert core(after).sum() == 0
    assert 0 < ring(after).sum() < ring(before).sum()


def test_painting_and_clearing_make_the_chunks_grass_due(texture):
    terrain = _resident(_meadow()[0])
    table = terrain.table
    cap = table._capacity
    here = table.live[:cap] & (table.coord[:cap, 0] == 0) & (table.coord[:cap, 1] == 0)
    table.grass_dirty[:cap] = False
    terrain.add_paint_stamp(128.0, 128.0, 40.0, texture)
    assert table.grass_dirty[:cap][here].all()
    assert not table.grass_dirty[:cap][table.live[:cap] & ~here
                                       & (np.abs(table.coord[:cap, 0]) > 1)].any()
    table.grass_dirty[:cap] = False
    terrain.clear_texture_paint()
    assert table.grass_dirty[:cap][here].all()


def test_moving_a_stamp_moves_the_clearing_when_the_drag_ends(texture):
    terrain = _resident(_meadow()[0])
    stamp = terrain.add_paint_stamp(70.0, 128.0, 40.0, texture, feather=0.0)
    terrain.set_selected_stamps({stamp['id']})
    terrain.move_paint_stamps({stamp['id']}, 110.0, 0.0, defer=True)
    at = lambda b, x: (np.hypot(b[:, 0] - x, b[:, 2] - 128.0) < 38.0).sum()
    assert at(_blades(terrain), 70.0) == 0          # still the stored paint
    terrain.finish_stamp_edit()
    after = _blades(terrain)
    assert at(after, 180.0) == 0 and at(after, 70.0) > 0


# -- adding a stamp thins the grass it has instead of growing it again ------

def _published(terrain):
    table = terrain.table
    slot = table.slot_of_coord[(0, 0)]
    return terrain.products.grass[int(slot)][2]


def _settle(terrain):
    return _resident(terrain)


def test_a_stamp_thins_the_published_grass_to_exactly_what_a_rebuild_grows(
        texture, monkeypatch):
    terrain = _settle(_meadow()[0])
    grown = []
    real = terrain._generate_grass_blades
    monkeypatch.setattr(terrain, "_generate_grass_blades",
                        lambda slot: grown.append(slot) or real(slot))

    terrain.add_paint_stamp(128.0, 128.0, 70.0, texture, feather=0.5)
    terrain.add_paint_stamp(60.0, 200.0, 40.0, texture, feather=0.0)
    _settle(terrain)

    assert not grown, "the chunk's grass was grown again for a stamp"
    thinned = _published(terrain)
    assert np.array_equal(thinned, real(terrain.table.slot_of_coord[(0, 0)])), (
        "thinning left different grass than growing it again does")
    assert np.hypot(thinned[:, 0] - 60.0, thinned[:, 2] - 200.0).min() > 38.0


def test_anything_but_an_added_stamp_still_grows_the_grass_again(
        texture, monkeypatch):
    terrain = _settle(_meadow()[0])
    stamp = terrain.add_paint_stamp(128.0, 128.0, 70.0, texture, feather=0.0)
    _settle(terrain)
    grown = []
    real = terrain._generate_grass_blades
    monkeypatch.setattr(terrain, "_generate_grass_blades",
                        lambda slot: grown.append(slot) or real(slot))

    # A stamp moved away uncovers ground: its grass must come back.
    terrain.move_paint_stamps([stamp['id']], 400.0, 0.0)
    _settle(terrain)
    assert grown, "moving a stamp did not regrow the grass it uncovered"
    blades = _published(terrain)
    assert (np.hypot(blades[:, 0] - 128.0, blades[:, 2] - 128.0) < 50.0).sum() > 20

    # A stamp added while the grass is already due for another reason
    # (density changed) is not thinned from the stale grass.
    grown.clear()
    terrain.set_grass(True, 0.03)
    terrain.add_paint_stamp(128.0, 128.0, 30.0, texture, feather=0.0)
    _settle(terrain)
    assert grown
    assert np.array_equal(_published(terrain),
                          real(terrain.table.slot_of_coord[(0, 0)]))
