"""Emissive Glow brushes: neon.

A Glow brush with ``emissive`` set is a light: in the colour it glows (its
tint if it has one, else its colour), with the intensity and radius it names,
standing at the brush's centre. It is not an entity, so it rides in the
frame's light list as a row past the entity table's own. A brush that is not
emissive -- every brush, in most maps -- is none of this: no slot, no row.
"""
import numpy as np

from engine import render_table as rt
from engine.entity_table import EntityTable
from engine.renderer.core.visibility import VisibilityMixin
from tests.helpers.worlds import box_brush


def _glow(name, pos=(0, 0, 0), **props):
    return box_brush(name, pos, (16, 128, 16), shader='Glow', **props)


def _brushes(*brushes):
    table = rt.RenderTable()
    table.sync(list(brushes), epoch=1)
    return table


# ---------------------------------------------------------------------------
# The brush side
# ---------------------------------------------------------------------------

def test_no_brush_is_a_light_unless_it_is_an_emissive_glow_brush():
    table = _brushes(
        box_brush('wall', (0, 0, 0), (64, 64, 64)),
        _glow('glow'),                                          # glows, lights nothing
        box_brush('odd', (0, 0, 0), (64, 64, 64), emissive=True))  # not a Glow brush

    assert len(table.emissive_slots) == 0
    assert table.brush_lights() is None
    assert not table._emissive_of, "a brush that is not a light was recorded as one"


def test_an_emissive_glow_brush_lights_in_the_colour_it_glows():
    table = _brushes(
        box_brush('wall', (0, 0, 0), (64, 64, 64)),
        _glow('plain', (10, 20, 30), emissive=True),
        _glow('red', (100, 0, 0), emissive=True, tint=[255, 0, 0],
              emissive_intensity=2.5, emissive_radius=300.0),
        _glow('green', (200, 0, 0), emissive=True, tint=[0.0, 1.0, 0.0]))

    positions, colours, params, on, sizes = table.brush_lights()

    assert table.emissive_slots.tolist() == [1, 2, 3]
    np.testing.assert_allclose(positions, [(10, 20, 30), (100, 0, 0), (200, 0, 0)])
    np.testing.assert_allclose(colours[0], (1.0, 1.0, 1.0), err_msg="a plain glow brush is white")
    np.testing.assert_allclose(colours[1], (1.0, 0.0, 0.0), err_msg="the tint is the light")
    np.testing.assert_allclose(colours[2], (0.0, 1.0, 0.0))
    np.testing.assert_allclose(params, [(1.0, 256.0), (2.5, 300.0), (1.0, 256.0)])
    assert on.all()
    # Half the longest side: the tube is the source, not a point in it.
    np.testing.assert_allclose(sizes, [64.0, 64.0, 64.0])


def test_a_brush_with_no_tint_emits_its_own_colour():
    table = _brushes(_glow('amber', emissive=True, colour=[255, 128, 0]))

    np.testing.assert_allclose(table.brush_lights()[1][0], (1.0, 128 / 255.0, 0.0), atol=1e-6)


def test_a_hidden_emissive_brush_is_switched_off():
    table = _brushes(_glow('neon', emissive=True, hidden=True))

    assert not table.brush_lights()[3][0]


def test_amounts_that_make_no_light_make_no_light():
    table = _brushes(
        _glow('zero', emissive=True, emissive_intensity=0.0),
        _glow('negative', emissive=True, emissive_radius=-5.0),
        _glow('nan', emissive=True, emissive_radius=float('nan')),
        _glow('words', emissive=True, emissive_intensity='lots'))

    # Not-a-number and not-a-number-at-all read as the default amount; zero
    # and a negative reach are no light.
    assert table.emissive_slots.tolist() == [2, 3]
    assert np.isfinite(table.brush_lights()[2]).all()


def test_ticking_emissive_on_an_open_map_makes_the_brush_a_light():
    neon = _glow('neon')
    brushes = [box_brush('wall'), neon]
    table = rt.RenderTable()
    table.sync(brushes, epoch=1)
    assert table.brush_lights() is None

    neon['emissive'] = True
    table.sync(brushes, epoch=2, dirty_objects={id(neon)})
    assert table.emissive_slots.tolist() == [1]

    neon['emissive'] = False
    table.sync(brushes, epoch=3, dirty_objects={id(neon)})
    assert table.brush_lights() is None


# ---------------------------------------------------------------------------
# Riding in the light list
# ---------------------------------------------------------------------------

def _lights(k, start=0.0):
    return (np.arange(k * 3, dtype=np.float64).reshape(k, 3) + start,
            np.tile((1.0, 0.5, 0.0), (k, 1)).astype(np.float32),
            np.tile((2.0, 300.0), (k, 1)).astype(np.float32),
            np.ones(k, dtype=bool),
            np.full(k, 48.0, dtype=np.float32))


def test_with_no_emissive_brush_the_light_list_is_left_alone():
    table = EntityTable()
    table.sync([], epoch=1)
    slots, generation = table.light_slots, table.generation

    table.set_aux_lights(None)

    assert table.light_slots is slots, "the light list was rebuilt for nothing"
    assert table.generation == generation


def test_brush_lights_are_rows_past_the_entities():
    table = EntityTable()
    table.sync([], epoch=1)

    table.set_aux_lights(_lights(3))

    assert table.count == 0, "a brush's light is not an entity"
    assert table.light_slots.tolist() == [0, 1, 2]
    np.testing.assert_allclose(table.pos[1], (3.0, 4.0, 5.0))
    np.testing.assert_allclose(table.light_color[2], (1.0, 0.5, 0.0))
    np.testing.assert_allclose(table.light_params[0], (2.0, 300.0))
    assert table.light_enabled[:3].all() and not table.light_casts_shadows[:3].any()
    np.testing.assert_allclose(table.light_size[:3], 48.0)


def test_brush_lights_follow_every_frame_and_go_when_the_brushes_do():
    table = EntityTable()
    table.sync([], epoch=1)

    table.set_aux_lights(_lights(2))
    table.set_aux_lights(_lights(2, start=100.0))            # a mover's neon moved
    assert table.light_slots.tolist() == [0, 1]
    np.testing.assert_allclose(table.pos[0], (100.0, 101.0, 102.0))

    table.set_aux_lights(_lights(40))                        # past the first capacity
    assert table.light_slots.tolist() == list(range(40))

    table.set_aux_lights(None)
    assert table.light_slots.tolist() == []
    table.set_aux_lights(None)                               # and stays quiet after


def test_the_other_buffer_takes_the_brush_lights_with_it():
    table = EntityTable()
    table.sync([], epoch=1)
    table.set_aux_lights(_lights(2))
    peer = EntityTable()

    peer.adopt(table)
    peer.set_aux_lights(None)

    assert peer.light_slots.tolist() == [], "a stale brush light survived in the peer"


def test_a_brush_light_is_not_filtered_by_the_entities_hidden_mask():
    """In Play the frame carries a ``hidden`` flag per entity row. A brush's
    light has no entity row: it must neither be looked up there (an index
    past the end) nor be switched off by it."""
    table = EntityTable()
    table.sync([], epoch=1)
    table.set_aux_lights(_lights(2))
    table.light_enabled[1] = False                           # its brush is hidden

    _, slots = VisibilityMixin._get_active_lights(None, {
        'entity_table': table, 'play_mode': True,
        'thing_hidden': np.zeros(table.count, dtype=bool)})

    assert slots.tolist() == [0]


def test_the_light_list_is_uploaded_again_only_when_a_brush_light_changes():
    """The renderer keys its light upload on the table's generation."""
    table = EntityTable()
    table.sync([], epoch=1)
    table.set_aux_lights(_lights(2))
    generation = table.generation

    table.set_aux_lights(_lights(2))
    assert table.generation == generation, "an unchanged neon moved the generation"

    table.set_aux_lights(_lights(2, start=5.0))
    assert table.generation > generation, "a neon that moved was not uploaded again"


def test_only_a_light_with_a_source_size_fades_in():
    """Every lit shader gets the same rewrite; a point light (size 0) is
    multiplied by exactly 1."""
    from engine import shaders

    source = shaders.light_ubo_source(shaders.DEFAULT_SHADERS['lit_arm.frag'])

    assert 'fioSourceFade(' in source and 'params.z' in source
    assert 'if (size <= 0.0) return 1.0;' in source
    assert '.intensity' not in source


# ---------------------------------------------------------------------------
# Emissive water
# ---------------------------------------------------------------------------

def _water(name, pos=(0, 0, 0), size=(200, 40, 100), **props):
    return box_brush(name, pos, size, shader='Water', **props)


def test_water_is_not_a_light_unless_it_is_emissive():
    table = _brushes(_water('pond'), _water('lava', water_tint=[1.0, 0.3, 0.0]))

    assert table.brush_lights() is None


def test_emissive_water_lights_in_its_tint_from_its_surface():
    table = _brushes(
        _water('plain', (0, 50, 0), emissive=True),
        _water('lava', (500, 10, 0), emissive=True, water_tint=[1.0, 0.25, 0.0],
               emissive_intensity=2.0, emissive_radius=320.0))

    positions, colours, params, on, sizes = table.brush_lights()

    np.testing.assert_allclose(colours[0], (0.0, 0.4, 0.6), atol=1e-6,
                               err_msg="untinted water glows the default water colour")
    np.testing.assert_allclose(colours[1], (1.0, 0.25, 0.0), atol=1e-6,
                               err_msg="the Water Tint is the light")
    # The surface: the top of the brush, not the middle of its depth.
    np.testing.assert_allclose(positions, [(0, 70, 0), (500, 30, 0)])
    np.testing.assert_allclose(params[1], (2.0, 320.0))
    np.testing.assert_allclose(sizes, [100.0, 100.0])
    assert on.all()


def test_the_water_shader_is_unchanged_for_water_that_does_not_glow():
    from engine import shaders

    source = shaders.DEFAULT_SHADERS['water.frag']

    assert 'uniform float waterEmissive;' in source
    assert 'if (waterEmissive > 0.0) {' in source, (
        "the emissive term is not guarded: it would touch all water")


def test_a_brush_that_is_not_a_light_pays_no_bytes_for_one():
    """The emissive data is kept apart from the per-row columns."""
    names = {name for name, *_ in rt._COLUMNS}

    assert not {name for name in names if 'emit' in name}, names
