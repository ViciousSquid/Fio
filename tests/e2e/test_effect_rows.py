"""Each published Effect row describes its own Effect, through world edits in Play.

The entity table reads Effect runtime state from the session's EffectStore.
The two are separate dense tables, so a row in one must be paired with the
right row in the other however the world's Effect list changes while playing:
an Effect removed from the middle, one inserted ahead of the others.

Checked against authored truth (each Effect's own ``effect_type``), not
against another table.
"""

import pytest

pytest.importorskip("PyQt5")

from editor.things import PlayerStart                                # noqa: E402
from engine.effect_entity import EFFECT_EXPLOSION, EFFECT_FIRE, Effect  # noqa: E402
from engine.effect_table import FAMILY_EXPLOSION, effect_family      # noqa: E402
from tests.helpers.worlds import level_data, make_thing, room        # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]


def _effect(name, x, kind):
    return make_thing(Effect, name, (x, 64.0, 0.0), effect_type=kind)


def _level():
    things = [make_thing(PlayerStart, "spawn", (0.0, 40.0, 300.0)),
              _effect("fire_a", -200.0, EFFECT_FIRE),
              _effect("boom_b", 0.0, EFFECT_EXPLOSION),
              _effect("fire_c", 200.0, EFFECT_FIRE),
              _effect("boom_d", 400.0, EFFECT_EXPLOSION)]
    return level_data(brushes=room(), things=things)


def _assert_rows_match_their_effects(session):
    effects = [t for t in session.state.things if isinstance(t, Effect)]
    with session.render_state() as frame:
        table = frame.entity_table
        for effect in effects:
            slot = table.slot_of_id[effect.properties["id"]]
            authored = effect_family(effect.properties["effect_type"])
            name = effect.properties["name"]
            assert int(table.effect_type[slot]) == authored, (
                "%s's row carries family %d, it is %d"
                % (name, int(table.effect_type[slot]), authored))
            # FIRE burns from the start; an EXPLOSION nobody triggered is dormant.
            assert bool(table.effect_alive[slot]) == (authored != FAMILY_EXPLOSION), name
    return effects


def test_effect_rows_follow_their_effects_through_removal_and_insertion(fio_session):
    session = fio_session(_level()).start_play()
    session.step(4)
    _assert_rows_match_their_effects(session)

    # An Effect removed from the middle of the list.
    middle = next(t for t in session.state.things
                  if t.properties.get("name") == "boom_b")
    session.state.things.remove(middle)
    session.step(4)
    _assert_rows_match_their_effects(session)

    # A new Effect inserted ahead of every other one.
    session.state.things.insert(1, _effect("boom_new", -400.0, EFFECT_EXPLOSION))
    session.step(4)
    effects = _assert_rows_match_their_effects(session)
    assert [e.properties["name"] for e in effects] == [
        "boom_new", "fire_a", "fire_c", "boom_d"]
