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
    decal = _decal(decal_type='wet', texture='wet_full.png', width=900.0, height=300.0)
    loaded = Thing.from_dict(decal.to_dict())
    assert type(loaded) is Decal
    assert loaded.properties['decal_type'] == 'wet'
    assert loaded.properties['width'] == 900.0


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
                                           'decal_type': 'wet', 'mirrored': True}, 2)
    m = np.asarray(model).reshape(4, 4).T
    np.testing.assert_allclose(m[:3, 2], (0.0, 0.0, -10.0), atol=1e-4)
    assert tuple(params) == (2.0, 1.0, float(WET), 1.0)


def test_place_makes_rows_and_frame_publishes_them_with_blood():
    table = DecalTable()
    things = [_decal('a', (0, 0, 0)), _decal('b', (100, 0, 0), decal_type='wet',
                                             texture='wet_puddle_01.png', opacity=0.5)]
    assert table.place(things)
    assert not table.place(things)                          # nothing changed: kept
    table.add((5, 5, 5), (0, -1, 0), 32.0, 16.0, 3, now=1.0, kind=FLOOR)
    models, params = table.frame(now=1.0, lifetime=10.0, fade=1.0)
    assert models.shape == (3, 16)
    names = decal_textures.images()[1]
    assert params[0, 0] == names.index('grime_01.png') and params[0, 2] == IMAGE
    assert params[1, 0] == names.index('wet_puddle_01.png') and params[1, 2] == WET
    assert params[1, 1] == pytest.approx(0.5)
    assert params[2, 2] == FLOOR                            # blood last: on top
    # Moving one rebuilds; blood clearing leaves the placed decals.
    things[0].pos = [50.0, 0.0, 0.0]
    assert table.place(things)
    table.clear()
    models, params = table.frame(now=1.0, lifetime=10.0, fade=1.0)
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
