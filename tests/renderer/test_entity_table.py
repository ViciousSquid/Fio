"""The dense entity projection: what it classifies, and when it refreshes.

The equivalence test is the important one.  ``_sort_objects``' Thing half is
what the renderer used to classify with, per visible entity per frame; the
table has to reach the same verdict -- the same entities, in the same order,
in the same pass -- from its cold column, or the refactor changes what gets
drawn.
"""

import itertools

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="the entity classes live in editor.things")

from editor.things import (Effect, Light, LevelChanger, LogicGate,  # noqa: E402
                           LogicRelay, LogicTimer, Monster, PathNode,
                           Portal, Thing)
from plugins.bigworld.entities import BigWorldSettings  # noqa: E402
from engine import entity_table as et                        # noqa: E402
from engine.change_journal import touch                      # noqa: E402
from engine.entity_table import EntityTable                  # noqa: E402
from engine.prop_entity import Prop                          # noqa: E402
from tests.helpers.worlds import make_thing                  # noqa: E402

pytestmark = pytest.mark.qt


def _synced(things, epoch=1):
    table = EntityTable()
    table.begin_frame(things, epoch)
    return table


@pytest.fixture
def renderer():
    pytest.importorskip("OpenGL", reason="renderer entity-slot tests require OpenGL")
    from tests.helpers import gl as glh

    glh.reset_texture_cache()
    with glh.GLTestContext(64, 64):
        value = glh.make_renderer()
        try:
            yield value
        finally:
            value.cleanup()
            glh.reset_texture_cache()


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('cls,bit', [
    (PathNode, et.ENT_SKIP),
    (Portal, et.ENT_PORTAL),
    (Monster, et.ENT_MONSTER),
    (LogicGate, et.ENT_ENTITY_SPRITE),
    (LogicRelay, et.ENT_ENTITY_SPRITE),
    (LogicTimer, et.ENT_ENTITY_SPRITE),
    (LevelChanger, et.ENT_ENTITY_SPRITE),
    (Light, et.ENT_LIGHT),
    (Prop, et.ENT_PROP),
])
def test_class_bit_is_set_for_its_entity_kind(cls, bit):
    table = _synced([make_thing(cls, 'e')])
    assert table.class_bits[0] & bit, (
        "a %s classified as %s" % (cls.__name__, et.describe(int(table.class_bits[0]))))


def test_a_monster_classifies_as_what_is_actually_published():
    """The logic thread hands the renderer a snapshot dict, not the Monster."""
    table = _synced([make_thing(Monster, 'grunt')])
    bits = int(table.class_bits[0])
    assert bits & et.ENT_ALWAYS_SPRITE, (
        "the renderer draws a Monster as a sprite before it looks at "
        "anything else; got %s" % et.describe(bits))


def test_a_plain_dict_is_skipped_rather_than_drawn():
    assert et._entity_class_bits({'pos': [0, 0, 0]}) == et.ENT_SKIP
    assert et.sprite_candidates({'pos': [0, 0, 0]}) is None


def test_a_raw_dict_row_is_polled_since_it_cannot_journal():
    """A dict standing in for a Thing has no setter to journal a move."""
    row = {'pos': [5.0, 6.0, 7.0]}
    table = EntityTable()
    table.begin_frame([row, make_thing(Light, 'lamp')], epoch=1)

    row['pos'] = [8.0, 9.0, 10.0]
    row['hidden'] = True
    hidden = table.begin_frame([row, table.things[1]], epoch=1)

    assert table.pos[0].tolist() == [8.0, 9.0, 10.0]
    assert list(hidden) == [True, False]
    assert table.rows_read == 1, "only the dict row should be read"


def test_portal_and_light_are_the_cull_exempt_pair():
    """The distance cull's exemption, as bits: lighting and portals are
    deliberately unaffected by it."""
    portal = _synced([make_thing(Portal, 'p')])
    light = _synced([make_thing(Light, 'l')])
    monster = _synced([make_thing(Monster, 'm')])
    assert portal.class_bits[0] & et.ENT_CULL_EXEMPT
    assert light.class_bits[0] & et.ENT_CULL_EXEMPT
    assert not monster.class_bits[0] & et.ENT_CULL_EXEMPT, (
        "a monster exempt from the distance cull would be simulated and drawn "
        "at any range")


def test_bigworld_settings_is_a_3d_billboard_entity():
    settings = BigWorldSettings(pos=[0.0, 0.0, 0.0])
    table = EntityTable()
    hidden = table.begin_frame([settings], epoch=1)
    bits = int(table.class_bits[0])
    model_slots, sprite_slots = et.classify_slots(
        table, np.arange(1, dtype=np.int32), hidden, False, False
    )

    assert settings.properties["render_mode"] == "billboard"
    assert settings.properties["sprite_path"] == settings.pixmap_path
    assert settings.properties["sprite_size"] == [64.0, 64.0]
    assert bits & et.ENT_HAS_SPRITE
    assert bits & et.ENT_MODE_BILLBOARD
    assert model_slots.tolist() == []
    assert sprite_slots.tolist() == [0]
    assert et.sprite_candidates(settings)[0][1] == "bigworldsettings.png"


def test_render_mode_resolves_the_same_way_the_loop_read_it():
    billboard = make_thing(Thing, 'b', render_mode='Billboard', sprite_path='s.png')
    table = _synced([billboard])
    assert table.class_bits[0] & et.ENT_MODE_BILLBOARD, (
        "render_mode is lower-cased before it is compared, as _sort_objects "
        "did with str(...).lower()")


def test_an_unset_render_mode_defaults_to_model():
    thing = make_thing(Thing, 'm', model_path='crate.glb')
    thing.properties.pop('render_mode', None)
    table = _synced([thing])
    assert table.class_bits[0] & et.ENT_MODE_MODEL


# ---------------------------------------------------------------------------
# Refresh discipline
# ---------------------------------------------------------------------------

def test_same_epoch_and_same_rows_is_a_no_op():
    things = [make_thing(Light, 'a'), make_thing(Light, 'b')]
    table = _synced(things)
    generation = table.generation
    assert table.sync(things, 1) is False
    assert table.generation == generation


def test_begin_frame_freezes_a_live_entity_list_before_projection():
    """A concurrent list mutation must not split one frame across two row sets."""
    things = [make_thing(Light, 'lamp')]
    late = make_thing(Monster, 'late')

    class MutatingProperties(dict):
        def __init__(self, values, owner):
            super().__init__(values)
            self.owner = owner
            self.did_mutate = False

        def get(self, key, default=None):
            if key == 'hidden' and not self.did_mutate:
                self.did_mutate = True
                self.owner.append(late)
            return super().get(key, default)

    things[0].properties = MutatingProperties(things[0].properties, things)
    table = EntityTable()

    hidden = table.begin_frame(things, epoch=1)

    assert len(things) == 2, "the mutation happened during the frame"
    assert table.count == 1, "the published table must stay on the frame snapshot"
    assert table.ids == [things[0].properties['id']]
    assert hidden.tolist() == [False]


def test_an_added_entity_reconciles_without_an_epoch_bump():
    things = [make_thing(Light, 'a')]
    table = _synced(things)
    things.append(make_thing(Monster, 'b'))
    assert table.sync(things, 1) is True
    assert table.count == 2
    assert len(table.monster_slots) == 1


def test_light_render_state_stays_dense_and_tracks_motion_and_io():
    lamp = make_thing(
        Light, 'lamp', (10.0, 20.0, 30.0),
        colour=[64, 128, 255], intensity=2.5, radius=900.0, state='on')
    table = _synced([lamp])

    assert np.allclose(table.light_color[0], [64 / 255.0, 128 / 255.0, 1.0])
    assert np.allclose(table.light_params[0], [2.5, 900.0])
    assert bool(table.light_enabled[0]) is True

    lamp.pos = [110.0, 220.0, 330.0]
    lamp.properties['colour'] = [255, 32, 16]
    lamp.properties['intensity'] = 0.75
    lamp.properties['radius'] = 1200.0
    lamp.properties['state'] = 'off'
    touch(lamp)          # what the I/O dispatcher does after any input
    table.begin_frame([lamp], epoch=1)

    assert np.allclose(table.pos[0], [110.0, 220.0, 330.0])
    assert np.allclose(table.light_color[0], [1.0, 32 / 255.0, 16 / 255.0])
    assert np.allclose(table.light_params[0], [0.75, 1200.0])
    assert bool(table.light_enabled[0]) is False


def test_rendered_portal_aperture_is_inset_without_changing_physical_size():
    """The render aperture is smaller, while authored portal dimensions stay intact."""
    pytest.importorskip("OpenGL")
    from engine.renderer_core import BaseRenderer

    portal = make_thing(
        Portal, 'portal', (10.0, 20.0, 30.0),
        width=128.0, height=256.0,
    )
    table = _synced([portal])

    authored = BaseRenderer._portal_slot_corners(table, 0)
    rendered = BaseRenderer._portal_slot_corners(
        table, 0, BaseRenderer.PORTAL_APERTURE_INSET)

    authored_width = np.linalg.norm(
        np.asarray(authored[1]) - np.asarray(authored[0]))
    authored_height = np.linalg.norm(
        np.asarray(authored[3]) - np.asarray(authored[0]))
    rendered_width = np.linalg.norm(
        np.asarray(rendered[1]) - np.asarray(rendered[0]))
    rendered_height = np.linalg.norm(
        np.asarray(rendered[3]) - np.asarray(rendered[0]))

    assert np.isclose(authored_width, 128.0)
    assert np.isclose(authored_height, 256.0)
    assert np.isclose(rendered_width, 120.0)
    assert np.isclose(rendered_height, 248.0)
    assert np.isclose(table.portal_width_height[0, 0], 128.0)
    assert np.isclose(table.portal_width_height[0, 1], 256.0)

@pytest.mark.gl
def test_renderer_consumes_active_lights_as_entity_slots(renderer):
    pytest.importorskip("OpenGL")
    on = make_thing(Light, 'on', state='on')
    off = make_thing(Light, 'off', state='off')
    table = _synced([on, off])
    packet = renderer._get_active_lights(
        {'entity_table': table, 'all_lights': []},
    )

    assert isinstance(packet, tuple)
    assert packet[0] is table
    assert packet[1].tolist() == [0]
    assert not any(isinstance(x, Light) for x in packet[1])


def test_light_shadow_flag_is_normalised_in_the_projection():
    lamp = make_thing(Light, 'lamp', casts_shadows='true')
    table = _synced([lamp])
    assert bool(table.light_casts_shadows[0]) is True

    lamp.properties['casts_shadows'] = 'off'
    touch(lamp)
    table.begin_frame([lamp], epoch=1)
    assert bool(table.light_casts_shadows[0]) is False


def test_released_prop_with_no_carry_yaw_uses_free_billboard_sentinel():
    prop = make_thing(Prop, 'released', render_mode='billboard',
                     sprite_path='assets/sprites/pickup.png')
    prop._carry_sprite_yaw = None

    table = _synced([prop])

    assert table.sprite_fixed_yaw[0] == -10000.0


def test_carrying_a_prop_reaches_its_row_without_a_touch():
    """The carry yaw journals itself on assignment, like ``pos``."""
    prop = make_thing(Prop, 'carried', render_mode='billboard',
                      sprite_path='assets/sprites/pickup.png')
    table = _synced([prop])

    prop._carry_sprite_yaw = 1.25
    prop._respawn_fade_alpha = 0.5
    table.begin_frame([prop], epoch=1)

    assert table.sprite_fixed_yaw[0] == pytest.approx(1.25)
    assert table.render_alpha[0] == pytest.approx(0.5)


def test_prop_respawn_alpha_is_a_dense_render_column():
    prop = make_thing(Prop, 'fading', render_mode='billboard',
                     sprite_path='assets/sprites/pickup.png')
    prop._respawn_fade_alpha = 0.25

    table = _synced([prop])

    assert table.render_alpha[0] == 0.25


def test_moved_position_uses_journal_payload_without_reading_the_thing(monkeypatch):
    light = make_thing(Light, 'moved', (1.0, 2.0, 3.0))
    table = _synced([light])

    light.pos = [101.0, 202.0, 303.0]
    monkeypatch.setattr(
        et, '_pos_of',
        lambda _thing: (_ for _ in ()).throw(
            AssertionError('MOVED path re-read the Thing position')
        ),
    )

    table.begin_frame([light], epoch=1)

    assert np.allclose(table.pos[0], [101.0, 202.0, 303.0])
    assert table.rows_read == 1


def test_positions_refresh_every_frame_without_reconciling():
    monster = make_thing(Monster, 'grunt', (0.0, 0.0, 0.0))
    table = _synced([monster])
    generation = table.generation

    monster.pos = [128.0, 64.0, -256.0]
    table.begin_frame([monster], epoch=1)

    assert table.generation == generation, "a move must not reconcile the table"
    assert np.allclose(table.pos[0], [128.0, 64.0, -256.0])


def test_hiding_through_the_parking_aware_writer_reaches_the_table():
    """I/O Hide/Show, Big World and save restores all write through here."""
    from engine.spatial import set_authored_flag

    light = make_thing(Light, 'l')
    table = _synced([light])
    assert table.begin_frame([light], epoch=1)[0] == False  # noqa: E712

    set_authored_flag(light, 'hidden', True)
    assert table.begin_frame([light], epoch=1)[0] == True   # noqa: E712


def test_a_frame_with_nothing_journalled_reads_no_entity(monkeypatch):
    """The whole point: an unchanged world costs no per-entity Python."""
    things = [make_thing(Light, 'l%d' % i) for i in range(50)]
    things += [make_thing(Monster, 'm%d' % i) for i in range(50)]
    table = _synced(things)

    reads = []
    monkeypatch.setattr(et, '_props_of',
                        lambda thing: reads.append(thing) or thing.properties)
    monkeypatch.setattr(et, '_pos_of', lambda thing: reads.append(thing) or thing.pos)
    for _ in range(3):
        table.begin_frame(things, epoch=1)

    assert reads == [], "%d entity reads on frames where nothing changed" % len(reads)


def test_rows_are_named_by_the_entitys_existing_uuid():
    light = make_thing(Light, 'lamp')
    table = _synced([light])
    assert table.ids[0] == light.properties['id']
    assert table.slot_of_id[light.properties['id']] == 0


def test_a_glm_position_is_stored_as_a_list():
    """Normalised where it is assigned, so the projection never writes back."""
    glm = pytest.importorskip("glm")
    thing = make_thing(Light, 'l')
    table = _synced([thing])

    thing.pos = glm.vec3(1.0, 2.0, 3.0)
    table.begin_frame([thing], epoch=1)

    assert thing.pos == [1.0, 2.0, 3.0] and isinstance(thing.pos, list)
    assert np.allclose(table.pos[0], [1.0, 2.0, 3.0])


def test_the_columns_are_a_pure_projection():
    """Rebuilding from the entity list must give identical bits."""
    things = [make_thing(Monster, 'm'), make_thing(Light, 'l'),
              make_thing(Prop, 'p', model_path='crate.glb'),
              make_thing(PathNode, 'n')]
    first = _synced(things)
    second = _synced(things)
    assert np.array_equal(first.class_bits[:first.count],
                          second.class_bits[:second.count])
    assert np.array_equal(first.pos[:first.count], second.pos[:second.count])


def test_a_monster_dying_updates_its_sprite_key_without_reconciling():
    monster = make_thing(Monster, 'grunt')
    table = _synced([monster])
    generation = table.generation
    before = int(table.sprite_key_id[0])

    monster.properties['dead'] = True
    touch(monster)
    table.begin_frame([monster], epoch=1)

    assert table.generation == generation
    assert int(table.sprite_key_id[0]) != before


def test_prop_model_representation_has_a_model_recipe_even_when_collection_is_weapon():
    prop = make_thing(
        Prop, 'weapon-prop',
        collect_enabled=True,
        collect_type='weapon',
        collect_weapon='gun1',
        render_mode='model',
        model_path=Prop.DEFAULT_MODEL_PATH,
    )
    table = _synced([prop])
    hidden = table.begin_frame([prop], epoch=1)
    slots = np.arange(table.count, dtype=np.int32)

    model_slots, sprite_slots = et.classify_slots(
        table, slots, hidden, is_play=True, show_sprites=False
    )

    assert model_slots.tolist() == [0]
    assert sprite_slots.tolist() == []
    recipe_id = int(table.model_recipe_id[0])
    assert recipe_id >= 0
    assert table.model_recipes()[recipe_id][0] == Prop.DEFAULT_MODEL_PATH


def test_model_prop_enters_the_dense_model_pass_with_its_recipe():
    prop = make_thing(
        Prop, 'oil-drum',
        model_path='assets/models/Oil_Drum.obj',
        render_mode='model',
        rotation=[0.0, 45.0, 0.0],
        scale=1.5,
    )
    table = _synced([prop])
    hidden = table.begin_frame([prop], epoch=1)
    slots = np.arange(table.count, dtype=np.int32)

    model_slots, sprite_slots = et.classify_slots(
        table, slots, hidden, is_play=True, show_sprites=False)

    assert model_slots.tolist() == [0]
    assert sprite_slots.tolist() == []
    recipe_id = int(table.model_recipe_id[0])
    assert recipe_id >= 0
    assert table.model_recipes()[recipe_id][0] == 'assets/models/Oil_Drum.obj'
    assert not np.allclose(table.model_base_matrix[0], 0.0)


def test_prop_switching_model_to_billboard_refreshes_dense_sprite_columns():
    prop = make_thing(
        Prop, 'oil-drum',
        model_path='assets/models/Oil_Drum.obj',
        render_mode='model',
        sprite_path='assets/sprites/pickup.png',
    )
    table = _synced([prop])
    model_recipe = int(table.model_recipe_id[0])
    assert model_recipe >= 0
    assert int(table.sprite_key_id[0]) >= 0

    prop.properties['render_mode'] = 'billboard'
    table.refresh_rows([prop], [0])

    assert int(table.class_bits[0]) & et.ENT_MODE_BILLBOARD
    assert not int(table.class_bits[0]) & et.ENT_MODE_MODEL
    assert int(table.sprite_key_id[0]) >= 0
    sid = int(table.sprite_key_id[0])
    assert table.sprite_recipes()[sid][0][1] == 'pickup.png'
    assert np.allclose(table.sprite_size[0], [32.0, 32.0])


def test_model_state_is_cold_and_position_is_separate():
    thing = make_thing(
        Prop, 'model',
        model_path='crate.glb',
        rotation=[15.0, 30.0, 45.0],
        scale=2.0,
        pos=[10.0, 20.0, 30.0],
    )
    table = _synced([thing])
    assert table.model_recipe_id[0] >= 0
    assert np.isclose(table.model_base_matrix[0, 3], 0.0)
    assert np.isclose(table.model_base_matrix[0, 7], 0.0)
    assert np.isclose(table.model_base_matrix[0, 11], 0.0)
    assert np.isclose(table.model_base_matrix[0, 15], 1.0)

    base = table.model_base_matrix[0].copy()
    thing.pos = [100.0, 200.0, 300.0]
    table.begin_frame([thing], epoch=1)

    np.testing.assert_array_equal(table.model_base_matrix[0], base)
    np.testing.assert_allclose(table.pos[0], [100.0, 200.0, 300.0])


# ---------------------------------------------------------------------------
# Equivalence with the loop it replaces
# ---------------------------------------------------------------------------

def _every_representation():
    """One entity per (class x model_path x render_mode x sprite_path x hidden).

    Exhaustive rather than sampled, because the chain being replaced has five
    fall-through branches and an ordering between them, and a mask that gets
    one of them wrong draws an entity in the wrong pass -- or not at all.
    """
    classes = [Thing, PathNode, Portal, Monster, LogicGate, LogicRelay,
               LogicTimer, LevelChanger, Light, Prop]
    things = []
    for cls, model_path, render_mode, sprite_path, hidden in itertools.product(
            classes, (None, 'crate.glb'),
            (None, 'model', 'billboard', 'Billboard', 'weird'),
            (None, 's.png'), (False, True)):
        thing = cls([0.0, 0.0, 0.0])
        props = thing.properties
        for key, value in (('model_path', model_path),
                           ('render_mode', render_mode),
                           ('sprite_path', sprite_path)):
            if value is None:
                props.pop(key, None)
            else:
                props[key] = value
        props['hidden'] = hidden
        things.append(thing)
    return things


@pytest.mark.parametrize('is_play,show_sprites', list(
    itertools.product((False, True), (False, True))))
def test_the_projection_splits_entities_without_legacy_object_sorting(
        is_play, show_sprites):
    """The dense classifier is the sole entity-pass boundary."""
    things = _every_representation()
    table = EntityTable()
    hidden = table.begin_frame(things, epoch=1)
    slots = np.arange(table.count, dtype=np.int32)

    model_slots, sprite_slots = et.classify_slots(
        table, slots, hidden, is_play, show_sprites)
    model_set = {int(i) for i in model_slots}
    sprite_set = {int(i) for i in sprite_slots}

    assert model_set.isdisjoint(sprite_set)
    assert all(not (table.class_bits[i] & et.ENT_EFFECT) for i in model_set | sprite_set)
    assert all(not (table.class_bits[i] & et.ENT_SKIP) for i in model_set | sprite_set)
    assert model_set | sprite_set <= set(range(table.count))


def test_entity_projection_preserves_authoritative_model_and_billboard_cases():
    model = Prop(properties={
        'model_path': 'crate.glb', 'render_mode': 'model',
        'sprite_path': 'crate.png',
    })
    billboard = Prop(properties={
        'model_path': 'crate.glb', 'render_mode': 'billboard',
        'sprite_path': 'crate.png',
    })
    effect = Effect(properties={'effect_type': 'FIRE'})
    table = EntityTable()
    hidden = table.begin_frame([model, billboard, effect], epoch=1)
    model_slots, sprite_slots = et.classify_slots(
        table, np.arange(3, dtype=np.int32), hidden, True, False)

    assert model_slots.tolist() == [0]
    assert sprite_slots.tolist() == [1]
    assert 2 not in model_slots and 2 not in sprite_slots


def test_portal_target_is_resolved_to_an_integer_entity_slot():
    a = make_thing(Portal, 'A')
    b = make_thing(Portal, 'B')
    a.properties['portal_target'] = 'B'
    table = _synced([a, b])

    assert table.portal_slots.tolist() == [0, 1]
    assert table.portal_target_slot[:table.count].tolist() == [1, -1]


def test_portal_authored_state_is_dense_and_geometry_is_shared():
    portal = make_thing(
        Portal, 'P', (10.0, 20.0, 30.0),
        width=192.0, height=320.0, rotation=[45.0, 20.0, 10.0],
        portal_direction='reverse', color=[64, 128, 255], show_rim=False,
    )
    table = _synced([portal])

    assert np.allclose(table.portal_width_height[0], [192.0, 320.0])
    assert int(table.portal_direction[0]) == et.PORTAL_DIRECTION_REVERSE
    assert np.allclose(table.portal_color[0], [64/255.0, 128/255.0, 1.0])
    assert bool(table.portal_show_rim[0]) is False
    assert bool(table.portal_glasses[0]) is True       # "Glasses" defaults on
    assert np.allclose(table.portal_basis[0], np.asarray(portal.get_basis()))


def test_portal_glasses_property_is_per_portal():
    on = make_thing(Portal, 'On')
    off = make_thing(Portal, 'Off', glasses=False)
    table = _synced([on, off])
    assert table.portal_glasses[:2].tolist() == [True, False]




@pytest.mark.gl
def test_portal_candidate_keeps_a_disabled_portal_during_fade_out(renderer):
    """Disabling gameplay must not remove the portal before its visual fade finishes."""
    a = make_thing(Portal, 'A', portal_target='B')
    b = make_thing(Portal, 'B', portal_target='A')
    a.properties['active'] = False
    a._fade_alpha = 0.5

    table = _synced([a, b])
    pytest.importorskip("OpenGL")
    slots = np.arange(2, dtype=np.int32)

    candidates = renderer._portal_candidate_slots(table, slots, None)
    assert 0 in candidates.tolist()
    assert 1 in candidates.tolist()

    a._fade_alpha = 0.0
    table.begin_frame([a, b], epoch=1)
    candidates = renderer._portal_candidate_slots(table, slots, None)
    assert 0 not in candidates.tolist()
    assert 1 in candidates.tolist()


def test_portal_live_state_refreshes_without_reconciling():
    portal = make_thing(Portal, 'P')
    table = _synced([portal])
    generation = table.generation

    portal.properties['active'] = False
    portal._fade_alpha = 0.25
    portal.pos = [100.0, 200.0, 300.0]
    portal.set_yaw_degrees(90.0)
    table.begin_frame([portal], epoch=1)

    assert table.generation == generation
    assert bool(table.portal_active[0]) is False
    assert np.isclose(table.portal_fade[0], 0.25)
    assert np.allclose(table.pos[0], [100.0, 200.0, 300.0])
    assert np.allclose(table.portal_basis[0], np.asarray(portal.get_basis()))


def test_shared_portal_transform_matches_the_authoring_wrapper():
    from engine.portal_transform import map_direction, map_point

    a = make_thing(Portal, 'A', (10.0, 20.0, 30.0), rotation=[30.0, 15.0, 5.0])
    b = make_thing(Portal, 'B', (-80.0, 12.0, 140.0), rotation=[-70.0, -10.0, 20.0])
    point = (25.0, 60.0, -12.0)
    direction = (0.3, -0.4, 0.5)

    assert np.allclose(
        map_point(a.pos, a.get_basis(), b.pos, b.get_basis(), point),
        a.map_point(b, *point),
    )
    assert np.allclose(
        map_direction(a.get_basis(), b.get_basis(), direction),
        a.map_direction(b, *direction),
    )

def _keys(table, slot):
    sid = int(table.sprite_key_id[slot])
    if sid < 0:
        return None
    return list(dict.fromkeys(candidate[0] for candidate in table.sprite_recipes()[sid]))

def test_a_portal_draws_no_sprite():
    """The sprite pass has always skipped Portals; the column says so."""
    table = _synced([make_thing(Portal, 'p')])
    assert int(table.sprite_key_id[0]) == et.SPRITE_NONE
    assert _keys(table, 0) is None


def test_monster_type_and_variant_are_constrained():
    invalid = make_thing(Monster, 'invalid', monster_type='orc', variant='unknown')
    assert invalid.properties['monster_type'] == 'human'
    assert invalid.properties['variant'] == '<None>'

    flying = make_thing(Monster, 'flying', monster_type='flying', variant='variant1')
    assert flying.properties['monster_type'] == 'flying'
    assert flying.properties['variant'] == 'variant1'


def test_a_monsters_sprite_key_names_its_current_frame():
    grunt = make_thing(Monster, 'grunt', monster_type='human')
    table = _synced([grunt])
    assert _keys(table, 0) == ['msprite_human_<None>_idle_']

    grunt.properties['is_shooting'] = True
    touch(grunt)
    table.begin_frame([grunt], epoch=1)
    assert _keys(table, 0) == ['msprite_human_<None>_shoot_']

    grunt.properties['dead'] = True
    touch(grunt)
    table.begin_frame([grunt], epoch=1)
    assert _keys(table, 0) == ['msprite_human_<None>_dead_'], (
        "dead wins over shooting, as the object path's chain decides it")



def test_a_dead_monster_keeps_an_idle_fallback_if_dead_frame_is_missing():
    """The numeric recipe must not drop the monster when dead.png is absent."""
    grunt = make_thing(Monster, 'grunt', monster_type='human')
    grunt.properties['dead'] = True
    table = _synced([grunt])
    recipe = table.sprite_recipes()[int(table.sprite_key_id[0])]

    assert [c[1:] for c in recipe] == [
        ('dead.png', 'sprites/monsters/human', True),
        ('idle.png', 'sprites/monsters/human', True),
    ]


def test_a_variant_monster_falls_back_to_the_base_folder():
    """Two load attempts, in the order draw_sprites makes them."""
    grunt = make_thing(Monster, 'grunt', monster_type='human', variant='variant1')
    table = _synced([grunt])
    sid = int(table.sprite_key_id[0])
    recipe = table.sprite_recipes()[sid]
    assert [c[2] for c in recipe] == ['sprites/monsters/human/variant1',
                                      'sprites/monsters/human']
    assert {c[0] for c in recipe} == {'msprite_human_variant1_idle_'}


def test_a_custom_monster_sprite_is_loaded_from_its_own_path():
    grunt = make_thing(Monster, 'grunt', monster_type='human',
                       custom_idle='assets/sprites/mine/idle.png')
    table = _synced([grunt])
    recipe = table.sprite_recipes()[int(table.sprite_key_id[0])]
    assert recipe[0] == ('msprite_human_<None>_idle_assets/sprites/mine/idle.png',
                         'idle.png', 'sprites/mine', True)
    assert recipe[1] == ('msprite_human_<None>_idle_assets/sprites/mine/idle.png',
                         'idle.png', 'sprites/monsters/human', True)


@pytest.mark.parametrize('cls,expected', [
    (LogicRelay, ('LogicRelay', 'logic_relay.png', 'sprites', True)),
    (LogicTimer, ('LogicTimer', 'logic_timer.png', 'sprites', True)),
    (LevelChanger, ('LevelChanger', 'levelchanger.png', 'sprites', True)),
])
def test_logic_entity_sprite_recipe_has_a_real_texture_source(cls, expected):
    thing = make_thing(cls, 'e')
    table = _synced([thing])
    recipe = table.sprite_recipes()[int(table.sprite_key_id[0])]
    assert recipe == (expected,)


def test_a_logic_gates_sprite_follows_its_type():
    gate = make_thing(LogicGate, 'g', logic_type='and')
    table = _synced([gate])
    assert _keys(table, 0)[0] == 'logic_and'

    gate.properties['logic_type'] = 'or'
    touch(gate)
    table.begin_frame([gate], epoch=1)
    assert _keys(table, 0)[0] == 'logic_or'


def test_the_override_is_tried_before_the_classs_shared_sprite():
    """The two chains, in the order the object path runs them."""
    gate = make_thing(LogicGate, 'g', logic_type='and')
    table = _synced([gate])
    assert _keys(table, 0) == ['logic_and', 'LogicGate'], (
        "the per-entity override must be tried first and the class texture "
        "second, or a gate would draw whatever the first gate loaded")


def test_an_entity_with_no_sprite_source_is_lookup_only():
    """No filename means the renderer must never load for this row."""
    table = _synced([make_thing(Light, 'l')])
    recipe = table.sprite_recipes()[int(table.sprite_key_id[0])]
    assert recipe == (('Light', '', '', False),)


def test_sprite_sizes_match_what_draw_sprites_chose():
    lamp = make_thing(Light, 'l')
    grunt = make_thing(Monster, 'm', sprite_width=96, sprite_height=160)
    billboard = make_thing(Thing, 'b', render_mode='billboard',
                           sprite_path='s.png', sprite_size=[48.0, 72.0])
    plain = make_thing(LogicRelay, 'r')
    table = _synced([lamp, grunt, billboard, plain])
    assert list(table.sprite_size[0]) == [16.0, 16.0]
    assert list(table.sprite_size[1]) == [96.0, 160.0]
    assert list(table.sprite_size[2]) == [48.0, 72.0]
    assert list(table.sprite_size[3]) == [32.0, 32.0]


def test_a_light_keeps_its_marker_size_even_with_a_sprite_path():
    """The size ladder tests Light first; that order is the contract."""
    lamp = make_thing(Light, 'l', sprite_path='s.png', sprite_size=[99.0, 99.0])
    table = _synced([lamp])
    assert list(table.sprite_size[0]) == [16.0, 16.0]


def test_a_malformed_sprite_size_falls_back_rather_than_raising():
    thing = make_thing(Thing, 'b', render_mode='billboard',
                       sprite_path='s.png', sprite_size='nonsense')
    table = _synced([thing])
    assert list(table.sprite_size[0]) == [32.0, 32.0]


def _count_resolves(monkeypatch):
    """Count how often the expensive recipe build actually runs."""
    calls = []
    real = et.sprite_candidates

    def counted(thing):
        calls.append(type(thing).__name__)
        return real(thing)

    monkeypatch.setattr(et, 'sprite_candidates', counted)
    return calls


def test_a_steady_frame_re_resolves_no_sprite_at_all(monkeypatch):
    """The recipe build formats keys and splits asset paths; it must not run
    on a frame where nothing about any sprite has changed.

    Ten times the cost of checking whether the inputs moved, measured at
    2.39 ms against 0.195 ms over 961 warm rows -- so "warm" has to mean
    "re-checked", not "rebuilt".
    """
    calls = _count_resolves(monkeypatch)
    things = [make_thing(Light, 'l'), make_thing(Monster, 'm'),
              make_thing(LogicRelay, 'r'), make_thing(Prop, 'p'),
              make_thing(Prop, 'prop', render_mode='billboard',
                         sprite_path='s.png')]
    table = EntityTable()
    table.begin_frame(things, epoch=1)
    calls.clear()

    for _ in range(5):
        table.begin_frame(things, epoch=1)

    assert calls == [], (
        "%d sprite recipes were rebuilt over five unchanged frames" % len(calls))


def test_only_the_journalled_row_is_re_resolved(monkeypatch):
    calls = _count_resolves(monkeypatch)
    grunt = make_thing(Monster, 'grunt', monster_type='human')
    lamp = make_thing(Light, 'lamp')
    table = EntityTable()
    table.begin_frame([grunt, lamp], epoch=1)
    calls.clear()

    grunt.properties['is_shooting'] = True
    touch(grunt)
    table.begin_frame([grunt, lamp], epoch=1)

    assert calls == ['Monster'], (
        "expected exactly the monster to be re-resolved, got %s" % (calls,))
    assert _keys(table, 0) == ['msprite_human_<None>_shoot_']


def test_a_cold_row_is_never_re_resolved_by_a_frame(monkeypatch):
    """A Light's sprite cannot change without an edit, so a frame must not ask."""
    calls = _count_resolves(monkeypatch)
    lamp = make_thing(Light, 'lamp')
    table = EntityTable()
    table.begin_frame([lamp], epoch=1)
    calls.clear()

    lamp.properties['sprite_path'] = 'changed.png'   # nobody was told
    for _ in range(5):
        table.begin_frame([lamp], epoch=1)

    assert calls == [], "a cold row was re-resolved during a frame"


@pytest.mark.parametrize('cls,field,value', [
    (Monster, 'dead', True),
    (Monster, 'is_shooting', True),
    (Monster, 'monster_type', 'alien'),
    (Monster, 'variant', 'red'),
    (Monster, 'custom_idle', 'assets/sprites/x.png'),
    (LogicGate, 'logic_type', 'or'),
    (Prop, 'render_mode', 'billboard'),
    (Prop, 'sprite_path', 'assets/sprites/x.png'),
])
def test_a_journalled_row_matches_a_rebuild(cls, field, value):
    """Whatever field changed, re-resolving the row gives what a fresh table has."""
    thing = make_thing(cls, 'e', render_mode='billboard',
                       sprite_path='assets/sprites/pickup.png')
    table = _synced([thing])

    thing.properties[field] = value
    touch(thing)
    table.begin_frame([thing], epoch=1)

    fresh = _synced([thing])
    assert table.sprite_recipes()[int(table.sprite_key_id[0])] == \
        fresh.sprite_recipes()[int(fresh.sprite_key_id[0])]
    assert table.class_bits[0] == fresh.class_bits[0]


def test_identical_recipes_intern_to_one_id():
    """Two identical collectible Props share a run, so they share an id."""
    table = _synced([make_thing(Prop, 'a', sprite_path='assets/sprites/health.png'),
                     make_thing(Prop, 'b', sprite_path='assets/sprites/health.png')])
    assert table.sprite_key_id[0] == table.sprite_key_id[1]
    assert len(table.sprite_recipes()) == 1


def test_sprite_columns_are_a_pure_projection():
    things = [make_thing(Monster, 'm'), make_thing(Light, 'l'),
              make_thing(Prop, 'p'), make_thing(Portal, 'pt')]
    first, second = _synced(things), _synced(things)
    assert np.array_equal(first.sprite_size[:first.count],
                          second.sprite_size[:second.count])
    assert ([_keys(first, i) for i in range(first.count)]
            == [_keys(second, i) for i in range(second.count)])


# ---------------------------------------------------------------------------
# Editor transactions
# ---------------------------------------------------------------------------

def test_an_editor_transaction_resolves_only_its_rows(monkeypatch):
    """A retarget of one entity must not re-walk a few thousand others."""
    things = [make_thing(Light, 'l%d' % i, (i * 10.0, 0.0, 0.0)) for i in range(200)]
    table = EntityTable()
    table.begin_frame(things, epoch=1)
    generation = table.generation
    resolved = []
    real = EntityTable._resolve_row
    monkeypatch.setattr(EntityTable, '_resolve_row',
                        lambda self, slot, thing: (resolved.append(slot),
                                                   real(self, slot, thing)))

    things[57].properties['colour'] = [255, 0, 0]
    table.begin_frame(things, epoch=2, dirty_objects={id(things[57])})

    assert resolved == [57]
    assert table.generation == generation
    assert table.light_color[57].tolist() == [1.0, 0.0, 0.0]


def test_retargeting_a_portal_in_place_relinks_it():
    a = make_thing(Portal, 'a', portal_target='b')
    b = make_thing(Portal, 'b', portal_target='a')
    c = make_thing(Portal, 'c', portal_target='')
    table = EntityTable()
    table.begin_frame([a, b, c], epoch=1)
    assert table.portal_target_slot[0] == 1

    a.properties['portal_target'] = 'c'
    table.begin_frame([a, b, c], epoch=2, dirty_objects={id(a)})

    assert table.portal_target_slot[0] == 2


def test_an_entity_table_adopts_its_peer_instead_of_rebuilding():
    things = [make_thing(Light, 'l'), make_thing(Monster, 'm'),
              make_thing(Portal, 'p'), make_thing(Prop, 'prop')]
    first, second = EntityTable(), EntityTable()
    first.begin_frame(things, 3)
    second.begin_frame(things, 3, peer=first)

    fresh = _synced(things, epoch=3)
    n = len(things)
    for name in ('pos', 'class_bits', 'hidden', 'light_color', 'sprite_size',
                 'portal_target_slot', 'model_recipe_id'):
        np.testing.assert_array_equal(getattr(second, name)[:n],
                                      getattr(fresh, name)[:n])
    assert second.ids == fresh.ids
    assert second.sprite_recipes() == fresh.sprite_recipes()
    assert second.pos is not first.pos
