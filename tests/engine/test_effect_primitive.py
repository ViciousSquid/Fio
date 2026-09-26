from editor.things import Effect, Thing
from engine.effect_entity import EFFECT_EXPLOSION, EFFECT_FIRE
from engine.entity_table import ENT_EFFECT, EntityTable
from editor.io_system import IOManager, get_input_names
from editor.io_handlers import register_all_input_handlers
from types import SimpleNamespace


def test_effect_defaults_to_fire_with_intrinsic_light():
    effect = Effect()
    assert effect.properties["type"] == "effect"
    assert effect.properties["effect_type"] == EFFECT_FIRE
    assert effect.properties["size"] == 32.0
    assert effect.properties["light_enabled"] is True
    assert effect.properties["light_radius"] == 128.0
    assert effect.properties["light_intensity"] == 2.5
    assert effect.properties["effect_seed"] != 0


def test_effect_seed_is_stable_and_copy_gets_a_new_seed():
    effect = Effect()
    first = effect.properties["effect_seed"]
    loaded = Thing.from_dict(effect.to_dict())
    assert loaded.properties["effect_seed"] == first

    clone = effect.duplicate()
    assert clone.properties["id"] != effect.properties["id"]
    assert clone.properties["effect_seed"] != first


def test_effect_is_projected_as_one_dense_visual_and_light_primitive():
    effect = Effect(
        properties={
            "effect_type": EFFECT_EXPLOSION,
            "lifetime": 0.5,
            "light_radius": 7.0,
        }
    )
    table = EntityTable()

    hidden = table.begin_frame(
        [effect],
        epoch=1,
        effect_runtime=True,
    )

    assert not hidden[0]
    assert table.class_bits[0] & ENT_EFFECT
    assert table.effect_slots.tolist() == [0]
    assert table.light_slots.tolist() == [0]
    assert not bool(table.effect_alive[0])
    assert not bool(table.effect_active[0])
    assert table.effect_type[0] == 1
    assert float(table.effect_params[0, 3]) == 7.0

    spawned = float(table.effect_spawn_time[0])
    table.begin_frame(
        [effect],
        epoch=1,
        effect_runtime=True,
    )
    assert float(table.effect_elapsed[0]) >= 0.0
    assert float(table.effect_spawn_time[0]) == spawned


def test_explosion_expires_but_fire_does_not():
    explosion = Effect(properties={"effect_type": EFFECT_EXPLOSION, "lifetime": 0.5})
    table = EntityTable()
    table.begin_frame([explosion], epoch=1, effect_runtime=True)
    assert not bool(table.effect_alive[0])
    assert not bool(table.effect_active[0])

    table.effect_spawn_time[0] -= 1.0
    table.begin_frame([explosion], epoch=1, effect_runtime=True)
    assert not bool(table.effect_alive[0])
    assert not bool(table.light_enabled[0])

    fire = Effect(properties={"effect_type": EFFECT_FIRE})
    table = EntityTable()
    table.begin_frame([fire], epoch=1, effect_runtime=True)
    table.effect_spawn_time[0] -= 1000.0
    table.begin_frame([fire], epoch=1, effect_runtime=True)
    assert bool(table.effect_alive[0])
    assert bool(table.light_enabled[0])


def test_effect_explode_io_plays_once_and_can_be_retriggered():
    explosion = Effect(
        properties={
            "id": "explosion-test",
            "effect_type": EFFECT_EXPLOSION,
            "lifetime": 0.5,
        }
    )
    table = EntityTable()
    table.begin_frame([explosion], epoch=1, effect_runtime=True)

    assert get_input_names("effect") == ["Explode"]

    io_manager = IOManager()
    register_all_input_handlers(io_manager)
    logic = SimpleNamespace(
        _entity_table=table,
        io_manager=io_manager,
    )
    explode = io_manager._input_handlers[("effect", "explode")]

    explode(explosion, "", logic)
    assert bool(table.effect_active[0])
    assert bool(table.effect_alive[0])
    assert float(table.effect_elapsed[0]) == 0.0

    table.effect_spawn_time[0] -= 1.0
    table.begin_frame([explosion], epoch=1, effect_runtime=True)
    assert not bool(table.effect_active[0])
    assert not bool(table.effect_alive[0])
    assert not bool(table.light_enabled[0])

    explode(explosion, "", logic)
    assert bool(table.effect_active[0])
    assert bool(table.effect_alive[0])
    assert float(table.effect_elapsed[0]) == 0.0
