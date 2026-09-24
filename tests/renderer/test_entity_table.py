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

from editor.things import (Light, LevelChanger, LogicGate,  # noqa: E402
                           LogicRelay, LogicTimer, Monster, PathNode, Pickup,
                           Portal, Thing)
from engine import entity_table as et                        # noqa: E402
from engine.entity_table import EntityTable                  # noqa: E402
from engine.prop_entity import Prop                          # noqa: E402
from tests.helpers.worlds import make_thing                  # noqa: E402

pytestmark = pytest.mark.qt


def _synced(things, epoch=1):
    table = EntityTable()
    table.sync(things, epoch)
    return table


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('cls,bit', [
    (PathNode, et.ENT_SKIP),
    (Portal, et.ENT_PORTAL),
    (Pickup, et.ENT_PICKUP),
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
        "a Monster is published as a render-snapshot dict, which the renderer "
        "draws as a sprite before it looks at anything else; got %s"
        % et.describe(bits))


def test_a_monster_snapshot_dict_classifies_the_same_way():
    snapshot = make_thing(Monster, 'grunt').get_render_snapshot()
    assert et._entity_class_bits(snapshot) & et.ENT_ALWAYS_SPRITE


def test_a_plain_dict_is_skipped_rather_than_drawn():
    assert et._entity_class_bits({'pos': [0, 0, 0]}) == et.ENT_SKIP


def test_a_published_list_of_snapshots_can_be_projected_too():
    """The table is built over the live Things; a caller projecting what was
    published instead must not hit an AttributeError on a snapshot dict."""
    published = [make_thing(Monster, 'grunt', (5.0, 6.0, 7.0)).get_render_snapshot(),
                 make_thing(Light, 'lamp', (1.0, 2.0, 3.0))]
    table = EntityTable()
    hidden = table.begin_frame(published, epoch=1)

    assert np.allclose(table.pos[0], [5.0, 6.0, 7.0])
    assert np.allclose(table.pos[1], [1.0, 2.0, 3.0])
    assert list(hidden) == [False, False]
    assert table.class_bits[0] & et.ENT_ALWAYS_SPRITE


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


def test_an_added_entity_reconciles_without_an_epoch_bump():
    things = [make_thing(Light, 'a')]
    table = _synced(things)
    things.append(make_thing(Monster, 'b'))
    assert table.sync(things, 1) is True
    assert table.count == 2
    assert len(table.monster_slots) == 1


def test_positions_refresh_every_frame_without_reconciling():
    monster = make_thing(Monster, 'grunt', (0.0, 0.0, 0.0))
    table = _synced([monster])
    generation = table.generation

    monster.pos = [128.0, 64.0, -256.0]
    table.begin_frame([monster], epoch=1)

    assert table.generation == generation, "a move must not reconcile the table"
    assert np.allclose(table.pos[0], [128.0, 64.0, -256.0])


def test_hidden_is_read_live_and_never_cached():
    """Big World parks entities by writing `hidden` with no notification."""
    light = make_thing(Light, 'l')
    table = _synced([light])
    assert table.begin_frame([light], epoch=1)[0] == False  # noqa: E712

    light.properties['hidden'] = True
    assert table.begin_frame([light], epoch=1)[0] == True   # noqa: E712


def test_rows_are_named_by_the_entitys_existing_uuid():
    light = make_thing(Light, 'lamp')
    table = _synced([light])
    assert table.ids[0] == light.properties['id']
    assert table.slot_of_id[light.properties['id']] == 0


def test_a_glm_position_is_normalised_back_onto_the_entity():
    """The publish loop this replaces did it, so the projection must too."""
    glm = pytest.importorskip("glm")
    thing = make_thing(Light, 'l')
    thing.pos = glm.vec3(1.0, 2.0, 3.0)
    table = _synced([thing])

    table.begin_frame([thing], epoch=1)

    assert isinstance(thing.pos, list), (
        "a Thing carrying a glm vector was left carrying one")
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


# ---------------------------------------------------------------------------
# Equivalence with the loop it replaces
# ---------------------------------------------------------------------------

def _every_representation():
    """One entity per (class x model_path x render_mode x sprite_path x hidden).

    Exhaustive rather than sampled, because the chain being replaced has five
    fall-through branches and an ordering between them, and a mask that gets
    one of them wrong draws an entity in the wrong pass -- or not at all.
    """
    classes = [Thing, PathNode, Portal, Pickup, Monster, LogicGate, LogicRelay,
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
def test_the_projection_splits_entities_exactly_as_sort_objects_did(
        is_play, show_sprites):
    pytest.importorskip("OpenGL", reason="_sort_objects lives on BaseRenderer")
    from engine.renderer_core import BaseRenderer

    things = _every_representation()
    # The published stream: a Monster is handed over as its render snapshot.
    published = [t.get_render_snapshot() if isinstance(t, Monster) else t
                 for t in things]

    renderer = BaseRenderer.__new__(BaseRenderer)
    config = {'play_mode': is_play, 'show_sprites_in_play_mode': show_sprites}
    model_out = []
    _, _, want_sprites, _, _, _, _ = renderer._sort_objects(
        [], published, config, model_out=model_out)

    table = EntityTable()
    hidden = table.begin_frame(things, epoch=1)
    slots = np.arange(table.count, dtype=np.int32)
    model_slots, sprite_slots = et.classify_slots(
        table, slots, hidden, is_play, show_sprites)

    got_models = [published[int(s)] for s in model_slots]
    got_sprites = [published[int(s)] for s in sprite_slots]

    _assert_same_entities(model_out, got_models, table, things, 'model pass')
    _assert_same_entities(want_sprites, got_sprites, table, things, 'sprite pass')


def _assert_same_entities(want, got, table, things, what):
    if [id(o) for o in want] == [id(o) for o in got]:
        return
    want_ids, got_ids = {id(o) for o in want}, {id(o) for o in got}
    index = {id(o): i for i, o in enumerate(things)}

    def _describe(ids):
        out = []
        for oid in list(ids)[:5]:
            i = index.get(oid)
            if i is None:
                out.append('<snapshot>')
                continue
            props = things[i].properties
            out.append('%s(%s) model=%r mode=%r sprite=%r hidden=%r' % (
                type(things[i]).__name__,
                et.describe(int(table.class_bits[i])),
                props.get('model_path'), props.get('render_mode'),
                props.get('sprite_path'), props.get('hidden')))
        return '; '.join(out)

    missing, extra = want_ids - got_ids, got_ids - want_ids
    assert not missing and not extra, (
        "%s: the projection and _sort_objects disagree.\n"
        "  only _sort_objects drew: %s\n"
        "  only the projection drew: %s"
        % (what, _describe(missing) or 'none', _describe(extra) or 'none'))
    assert [id(o) for o in want] == [id(o) for o in got], (
        "%s: same entities, different order -- the sprite pass is depth "
        "ordered afterwards, but the pre-sort order still has to match" % what)
