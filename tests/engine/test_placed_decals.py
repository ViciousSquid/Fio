"""Placed decals are rows of the one decal table blood uses.

A ``Decal`` entity becomes a row -- the same box, image layer, kind and
flags -- published in the same two arrays and painted by the same pass. It
differs from blood only in not fading and in naming its own image.
"""

import math

import numpy as np
import pytest

from editor.things import Decal, Thing
from engine import decal_textures
from engine.decal_table import FLOOR, IMAGE, WALL, WET, DecalTable, placed_row
from tests.helpers.worlds import make_thing

# The Decal entity is editor.things', which imports PyQt5 at module level:
# the module belongs to the Qt tier (the no-Qt jobs cannot import it).
pytestmark = pytest.mark.qt


def test_the_shared_image_list_orders_layers_and_blood_ranges():
    names = ('blood_floor_01.png', 'blood_floor_02.png', 'blood_wall_01.png',
             'grime_01.png', 'wet_puddle_01.png')
    assert decal_textures.layer_of('wet_puddle_01.png', names) == 4
    assert decal_textures.layer_of('assets/textures/decals/GRIME_01.PNG', names) == 3
    assert decal_textures.layer_of('missing.png', names) == -1
    # walls first-layer, count; floors first-layer, count
    assert decal_textures.blood_ranges(names) == (2.0, 1.0, 0.0, 2.0)
    assert decal_textures.blood_ranges(('grime_01.png',)) == (0.0, 1.0, 0.0, 1.0)


def test_the_repository_ships_the_sample_decals():
    names = decal_textures.refresh()[1]
    for name in ('grime_01.png', 'scorch_01.png', 'wet_puddle_01.png',
                 'wet_damp_01.png', 'wet_full.png'):
        assert name in names


def _decal(name='d', pos=(0.0, 0.0, 0.0), **props):
    props.setdefault('texture', 'grime_01.png')
    return make_thing(Decal, name, pos, **props)


def test_a_decal_is_saved_and_loaded_as_a_decal():
    decal = _decal(texture='wet_full.png', width=900.0, height=300.0)
    loaded = Thing.from_dict(decal.to_dict())
    assert type(loaded) is Decal
    assert loaded.properties['texture'] == 'wet_full.png'
    assert loaded.properties['width'] == 900.0
    # An older map's decal_type is dropped: the image's name decides.
    old = decal.to_dict()
    old['properties']['decal_type'] = 'image'
    assert 'decal_type' not in Thing.from_dict(old).properties


@pytest.mark.parametrize('name, wet', [
    ('wet_full.png', True), ('Puddle_02.PNG', True), ('my_WET_floor.png', True),
    ('assets/textures/decals/oil_puddle.png', True), ('scorch_01.png', False),
    ('grime_01.png', False), ('blood_floor_01.png', False), ('', False)])
def test_a_decal_is_wet_exactly_when_its_image_says_so(name, wet):
    assert decal_textures.is_wet_image(name) is wet
    _model, params = placed_row((0, 0, 0), {'texture': name}, 0)
    assert params[2] == (WET if wet else IMAGE)


def test_a_placed_row_throws_its_box_along_yaw_and_pitch():
    model, params = placed_row((10.0, 0.0, 5.0), {'width': 200.0, 'height': 100.0,
                                                  'depth': 40.0, 'pitch': -90.0}, 7)
    m = np.asarray(model).reshape(4, 4).T                   # column-major
    np.testing.assert_allclose(m[:3, 3], (10.0, 0.0, 5.0))
    np.testing.assert_allclose(m[:3, 2], (0.0, -40.0, 0.0), atol=1e-4)  # down, 40 deep
    assert np.linalg.norm(m[:3, 0]) == pytest.approx(200.0)
    assert np.linalg.norm(m[:3, 1]) == pytest.approx(100.0)
    assert tuple(params) == (7.0, 1.0, float(IMAGE), 0.0)
    # Yaw 90, level: thrown along -Z, into a wall.
    model, params = placed_row((0, 0, 0), {'yaw': 90.0, 'pitch': 0.0, 'depth': 10.0,
                                           'texture': 'wet_full.png', 'mirrored': True}, 2)
    m = np.asarray(model).reshape(4, 4).T
    np.testing.assert_allclose(m[:3, 2], (0.0, 0.0, -10.0), atol=1e-4)
    assert tuple(params) == (2.0, 1.0, float(WET), 1.0)


def test_place_makes_rows_and_frame_publishes_them_with_blood():
    table = DecalTable()
    things = [_decal('a', (0, 0, 0)), _decal('b', (100, 0, 0),
                                             texture='wet_puddle_01.png', opacity=0.5)]
    assert table.place(things)
    assert not table.place(things)                          # nothing changed: kept
    table.add((5, 5, 5), (0, -1, 0), 32.0, 16.0, 3, now=1.0, kind=FLOOR)
    models, params, inverses = table.frame(now=1.0, lifetime=10.0, fade=1.0)
    assert models.shape == (3, 16) and inverses.shape == (3, 16)
    names = decal_textures.images()[1]
    assert params[0, 0] == names.index('grime_01.png') and params[0, 2] == IMAGE
    assert params[1, 0] == names.index('wet_puddle_01.png') and params[1, 2] == WET
    assert params[1, 1] == pytest.approx(0.5)
    assert params[2, 2] == FLOOR                            # blood last: on top
    # Moving one rebuilds; blood clearing leaves the placed decals.
    things[0].pos = [50.0, 0.0, 0.0]
    assert table.place(things)
    table.clear()
    models, params, _inverses = table.frame(now=1.0, lifetime=10.0, fade=1.0)
    assert len(models) == 2 and models[0][12] == pytest.approx(50.0)


def test_hidden_or_missing_images_are_left_out_and_said_once(capsys):
    table = DecalTable()
    things = [_decal('shown'), _decal('hidden', hidden=True),
              _decal('lost', texture='no_such_image.png')]
    table.place(things)
    assert len(table.placed_model) == 1
    out = capsys.readouterr().out
    assert out.count("No image 'no_such_image.png'") == 1
    things[0].pos = [1.0, 0.0, 0.0]
    table.place(things)
    assert "No image" not in capsys.readouterr().out


def test_selection_is_drawn_only_in_the_editor():
    table = DecalTable()
    decal = _decal(mirrored=True)
    decal.editor_selected = True
    table.place([decal], show_selection=True)
    assert table.placed_params[0, 3] == 3.0                 # mirrored 1 + selected 2
    table.place([decal], show_selection=False)              # play: never tinted
    assert table.placed_params[0, 3] == 1.0
    assert table.frame(0.0, 10.0, 1.0) is not None          # placed rows alone publish


@pytest.mark.parametrize('normal', [(0, 1, 0), (0, -1, 0), (1, 0, 0), (0, 0, -1),
                                    (0.6, 0.8, 0.0), (0.3, 0.2, -0.93)])
def test_placing_on_a_surface_throws_the_decal_into_it(normal):
    from editor.decal_tool import orientation_from_normal
    yaw, pitch = orientation_from_normal(normal)
    model, _params = placed_row((0, 0, 0), {'yaw': yaw, 'pitch': pitch, 'depth': 1.0}, 0)
    throw = np.asarray(model).reshape(4, 4).T[:3, 2]
    n = np.asarray(normal, dtype=float) / np.linalg.norm(normal)
    np.testing.assert_allclose(throw, -n, atol=1e-5)
    assert math.isfinite(yaw) and math.isfinite(pitch)


class _CountedList(list):
    """Things that count how often they are walked."""
    walks = 0

    def __iter__(self):
        type(self).walks += 1
        return super().__iter__()


def test_only_what_changed_is_remade():
    """Fio's rule: a still tick touches no decal; one moved remakes one row."""
    things = _CountedList(_decal(str(i), (i * 10.0, 0.0, 0.0)) for i in range(50))
    table = DecalTable()
    assert table.place(things, epoch=7)
    assert table.rows_made == 50
    walks = _CountedList.walks
    for _ in range(10):                                     # still: no walk, nothing made
        assert not table.place(things, epoch=7)
    assert _CountedList.walks == walks and table.rows_made == 50
    before = table.placed_model.copy()
    things[20].pos = [999.0, 0.0, 0.0]                      # journalled by the assignment
    assert table.place(things, epoch=7)
    assert table.rows_made == 51 and _CountedList.walks == walks
    assert table.placed_model[20][12] == pytest.approx(999.0)
    changed = np.flatnonzero((table.placed_model != before).any(axis=1))
    assert changed.tolist() == [20]                         # the others untouched
    # A selection the Decal tool makes is touched: that row alone, tinted.
    from engine.change_journal import touch
    things[3].editor_selected = True
    touch(things[3])
    assert table.place(things, epoch=7)
    assert table.rows_made == 52 and table.placed_params[3, 3] == 2.0


def test_a_new_world_revision_reconciles_but_reuses_unchanged_rows():
    things = [_decal(str(i), (i * 10.0, 0.0, 0.0)) for i in range(20)]
    table = DecalTable()
    table.place(things, epoch=1)
    assert not table.place(things, epoch=2)                 # an edit elsewhere
    assert table.rows_made == 20
    things[5].properties['width'] = 300.0                   # a property edit...
    del things[0]                                           # ...and a removal
    things.append(_decal('new', (5.0, 0.0, 5.0)))           # ...and an addition
    assert table.place(things, epoch=3)
    assert table.rows_made == 22                            # the edited one and the new
    assert len(table.placed_model) == 20
    np.testing.assert_allclose(table.placed_model[:, 12],
                               [t.pos[0] for t in things])  # in the level's order
    assert np.linalg.norm(table.placed_model[4, 0:3]) == pytest.approx(300.0)


def test_unrelated_movement_costs_nothing():
    """A battle journals every monster each tick; decals stay as they were."""
    from editor.things import Monster
    decals = [_decal(str(i)) for i in range(5)]
    monsters = [make_thing(Monster, f'm{i}', (0.0, 0.0, 0.0)) for i in range(200)]
    things = decals + monsters
    table = DecalTable()
    table.place(things, epoch=1)
    made = table.rows_made
    for m in monsters:
        m.pos = [1.0, 2.0, 3.0]
    assert not table.place(things, epoch=1)
    assert table.rows_made == made


def test_frame_publishes_each_boxs_inverse():
    table = DecalTable()
    table.place([_decal(yaw=30.0, pitch=-45.0, roll=10.0, width=90.0, depth=7.0)])
    table.add((5, 5, 5), (0.3, -1, 0.2), 32.0, 16.0, 3, now=1.0, kind=FLOOR)
    models, _params, inverses = table.frame(now=1.0, lifetime=10.0, fade=1.0)
    for model, inverse in zip(models, inverses):
        m = model.reshape(4, 4).T                           # column-major
        inv = inverse.reshape(4, 4).T
        np.testing.assert_allclose(inv @ m, np.eye(4), atol=1e-4)


def test_decals_off_screen_or_too_small_are_not_drawn():
    import glm
    from engine.decal_table import box_matrix
    from engine.renderer.forward.decals import visible_decals
    projection = glm.perspective(glm.radians(90.0), 1.0, 1.0, 10000.0)
    view = glm.lookAt(glm.vec3(0, 0, 0), glm.vec3(0, 0, -1), glm.vec3(0, 1, 0))
    eye = glm.vec3(0, 0, 0)
    boxes = np.stack([
        box_matrix((0, 0, -500), (0, 0, -1), 64, 16),       # ahead: drawn
        box_matrix((0, 0, 500), (0, 0, -1), 64, 16),        # behind: not
        box_matrix((3000, 0, -500), (0, 0, -1), 64, 16),    # far off to the side: not
        box_matrix((0, 0, -9000), (0, 0, -1), 2, 2),        # a speck: not
        box_matrix((0, 0, 20), (0, 0, -1), 64, 64),         # just behind, but around the
                                                            # near plane: drawn
        box_matrix((0, 0, 0), (0, -1, 0), 4000, 32),        # the camera inside: drawn
    ])
    assert visible_decals(boxes, projection, view, eye, 540).tolist() == [0, 4, 5]
