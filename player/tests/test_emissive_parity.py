"""Parity with the desktop for what glows, and for what a player refuses.

* The shared light block's new member (a light's source size) reaches the
  player's packed block, and the fade that reads it uses the player's own
  name for the fragment position.
* Emissive Glow brushes and emissive water are lights here too, in the colour
  they glow; an emissive decal is one as on the desktop.
* A world from a newer Fio, or with an entity type nothing knows, is refused
  rather than played wrong.
"""
import numpy as np
import pytest

from player.render import world_shaders
from player.render.lighting import LightBuffer
from player.render.projection import EntityProjection, lights
from player.runtime.fiopak import PackageError
from player.world import PlayerWorld


def level(things=(), brushes=()):
    return {'brushes': [{'pos': [0, -8, 0], 'size': [512, 16, 512], 'id': 'floor'},
                        *brushes],
            'things': [{'type': 'PlayerStart', 'pos': [0, 64, 0]}, *things]}


def _world(things=(), brushes=()):
    return PlayerWorld(level(things, brushes))


# -- the light block ---------------------------------------------------------

def test_the_player_s_light_block_has_no_params_member_and_reads_the_size():
    source = world_shaders.FRAGMENT

    assert 'vec4 params' not in source and '.params.' not in source, (
        "the packed block and the shader disagree about the light's layout")
    assert 'fioSourceFade(' in source
    assert 'float(lights[i].indices.y)' in source
    assert 'FragPos' not in source, (
        "the fade reads a name this shader does not have: it will not compile")
    assert '.position_radius.xyz - worldPos' in source


def test_a_light_s_source_size_is_packed_beside_its_shadow_index():
    class _GL:
        GL_UNIFORM_BUFFER = GL_DYNAMIC_DRAW = 0

        def __getattr__(self, name):
            if name.startswith('GL_'):
                return 0                # an enum, as GL's are: an integer
            return lambda *args, **kwargs: 1

    buffer = LightBuffer(_GL(), 1)
    buffer.upload([dict(pos=(1, 2, 3), color=(1, 0, 0), intensity=2.0, radius=300.0, size=64.0),
                   dict(pos=(0, 0, 0), color=(1, 1, 1), intensity=1.0, radius=100.0)], {0: 3})

    assert buffer.data['indices'][0].tolist()[:2] == [3, 64]
    assert buffer.data['indices'][1].tolist()[:2] == [-1, 0], "a point light has no size"


# -- what glows --------------------------------------------------------------

def _glow(ident, pos, **props):
    return dict({'id': ident, 'pos': list(pos), 'size': [16, 128, 16],
                 'shader': 'Glow', 'textures': {}}, **props)


def test_emissive_brushes_and_water_are_lights_in_the_player():
    world = _world(brushes=[
        _glow('plain', (100, 64, 0)),                                   # glows, lights nothing
        _glow('neon', (200, 64, 0), emissive=True, tint=[255, 0, 0],
              emissive_intensity=2.0, emissive_radius=300.0),
        {'id': 'lava', 'pos': [-200, 10, 0], 'size': [200, 20, 100], 'shader': 'Water',
         'water_tint': [1.0, 0.25, 0.0], 'emissive': True, 'textures': {}}])
    try:
        table = EntityProjection().update(world)
        found = lights(table, (0.0, 64.0, 0.0))
    finally:
        world.close()

    by_colour = {tuple(round(float(c), 2) for c in light['color']): light for light in found}
    neon, lava = by_colour[(1.0, 0.0, 0.0)], by_colour[(1.0, 0.25, 0.0)]
    assert len(found) == 2, "a Glow brush that is not emissive became a light"
    np.testing.assert_allclose(neon['pos'], (200, 64, 0))
    assert (neon['intensity'], neon['radius'], neon['size']) == (2.0, 300.0, 64.0)
    np.testing.assert_allclose(lava['pos'], (-200, 20, 0), err_msg="water glows from its surface")
    assert lava['size'] == 100.0


def test_a_world_with_nothing_emissive_adds_no_lights():
    world = _world(things=[{'type': 'Light', 'pos': [0, 100, 0]}],
                   brushes=[_glow('plain', (100, 64, 0))])
    try:
        projection = EntityProjection()
        projection.update(world)
        table = projection.update(world)
        found = lights(table, (0.0, 64.0, 0.0))
    finally:
        world.close()

    assert len(found) == 1 and found[0]['size'] == 0.0
    assert table._aux_lights == 0 and int(table.light_slots.max()) < table.count, (
        "a world with nothing emissive got rows past its entities")


def test_an_emissive_decal_is_a_light_in_the_player():
    world = _world(things=[{'type': 'decal', 'pos': [0, 64, -100],
                            'properties': {'texture': '', 'emissive': True,
                                           'tint': [0.0, 1.0, 0.0], 'yaw': 90.0,
                                           'pitch': 0.0, 'emissive_intensity': 1.5}}])
    try:
        found = lights(EntityProjection().update(world), (0.0, 64.0, 0.0))
    finally:
        world.close()

    assert len(found) == 1
    np.testing.assert_allclose(found[0]['color'], (0.0, 1.0, 0.0))
    np.testing.assert_allclose(found[0]['pos'], (0, 64, -99), atol=1e-6,
                               err_msg="a unit off its face")
    assert found[0]['intensity'] == 1.5


# -- what is refused ---------------------------------------------------------

def test_a_world_from_a_newer_fio_is_refused():
    from engine import brush_placement

    data = dict(level(), version=brush_placement.MAP_VERSION + 1)
    with pytest.raises(PackageError, match="newer version of Fio"):
        PlayerWorld(data)


def test_a_world_with_an_entity_nothing_knows_is_refused():
    with pytest.raises(PackageError, match="Unknown entity type"):
        PlayerWorld(level([{'type': 'gravity_well_from_a_plugin', 'pos': [0, 0, 0],
                            'properties': {}}]))
