import pytest

pytest.importorskip("PyQt5", reason="Effect is an editor Thing")
pytestmark = pytest.mark.qt

from editor.things import Effect
from engine.effect_table import (
    FAMILY_CUSTOM,
    FAMILY_EXPLOSION,
    FAMILY_FIRE,
    FAMILY_ORB,
    EffectStore,
)
from engine.entity_table import EntityTable


def test_effect_store_is_dense_and_does_not_store_authoring_objects():
    first = Effect(
        pos=(1.0, 2.0, 3.0),
        properties={"effect_type": "FIRE", "lifetime": 1.25},
    )
    second = Effect(
        pos=(4.0, 5.0, 6.0),
        properties={"effect_type": "CUSTOM", "lifetime": 2.5},
    )
    store = EffectStore(capacity=1)
    store.begin_session([first, second])

    assert len(store) == 2
    assert store.pos.shape[0] >= 2
    assert store.family_id[:2].tolist() == [FAMILY_FIRE, FAMILY_CUSTOM]
    assert store.lifetime[:2].tolist() == pytest.approx([1.25, 2.5])
    assert store.index_of(first) == 0
    assert store.index_of(second) == 1
    assert not any(value is first or value is second for value in store.__dict__.values()) if hasattr(store, "__dict__") else True


def test_effect_store_preserves_runtime_state_when_authoring_rows_reorder():
    first = Effect(properties={"effect_type": "FIRE"})
    second = Effect(properties={"effect_type": "ORB"})
    store = EffectStore()
    store.begin_session([first, second])

    first_phase = float(store.phase[0])
    second_phase = float(store.phase[1])
    store.trigger_explosion(second, 123.456)

    store.rebuild([second, first])

    assert store.index_of(second) == 0
    assert store.index_of(first) == 1
    assert float(store.phase[1]) == pytest.approx(first_phase)
    assert float(store.phase[0]) == 0.0
    assert float(store.spawn_time[0]) == pytest.approx(123.456)
    assert bool(store.active[0])
    assert float(store.phase[1]) == pytest.approx(first_phase)
    assert second_phase != float(store.phase[0])


def test_entity_table_reads_effect_runtime_from_effect_store():
    effect = Effect(
        properties={"effect_type": "FIRE", "lifetime": 0.75}
    )
    store = EffectStore()
    store.begin_session([effect])
    store.trigger_explosion(effect, 123.456)

    # Deliberately poison the compatibility mirrors.  The production path must
    # take the runtime from EffectStore instead.
    effect._effect_spawn_time = 999.0
    effect._effect_animation_phase = 0.99
    effect._effect_active = False

    table = EntityTable()
    table.begin_frame(
        [effect],
        epoch=1,
        effect_runtime=True,
        effect_store=store,
    )

    assert table.effect_type[0] == FAMILY_EXPLOSION
    assert float(table.effect_spawn_time[0]) == pytest.approx(123.456)
    assert float(table.effect_lifetime[0]) == pytest.approx(0.75)
    assert bool(table.effect_active[0])
    assert bool(table.effect_alive[0])


def test_effect_store_set_type_resets_dense_runtime_state():
    effect = Effect(properties={"effect_type": "FIRE"})
    store = EffectStore()
    store.begin_session([effect])

    store.set_type(effect, "ORB")
    assert store.family_id[0] == FAMILY_ORB
    assert bool(store.active[0])
    assert float(store.spawn_time[0]) == 0.0
    assert 0.0 <= float(store.phase[0]) <= 1.0

    store.set_type(effect, "EXPLOSION")
    assert store.family_id[0] == FAMILY_EXPLOSION
    assert not bool(store.active[0])
