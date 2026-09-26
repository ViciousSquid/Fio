from editor.things import Effect, Thing
from engine.effect_entity import EFFECT_EXPLOSION, EFFECT_FIRE, EFFECT_FIRE_TEXTURES
from engine.entity_table import ENT_EFFECT, EntityTable
from editor.io_system import IOManager, get_input_names, get_output_names
from editor.io_handlers import register_all_input_handlers
from types import SimpleNamespace

import numpy as np


def test_effect_defaults_to_fire_with_intrinsic_light():
    effect = Effect()
    assert effect.properties["type"] == "effect"
    assert effect.properties["effect_type"] == EFFECT_FIRE
    assert effect.properties["preview"] is False
    assert effect.properties["fire_texture"] == EFFECT_FIRE_TEXTURES[0]
    assert len(EFFECT_FIRE_TEXTURES) == 5
    assert effect.properties["width"] == 32.0
    assert effect.properties["height"] == 24.0
    assert "size" not in effect.properties
    assert "scale" not in effect.properties
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


def test_fire_texture_choice_is_projected_to_dense_variant():
    effect = Effect(
        properties={
            "effect_type": EFFECT_FIRE,
            "fire_texture": EFFECT_FIRE_TEXTURES[2],
        }
    )
    table = EntityTable()
    hidden = table.begin_frame(
        [effect],
        epoch=1,
        effect_runtime=False,
    )

    assert not hidden[0]
    assert table.effect_type[0] == 0
    assert table.effect_fire_variant.tolist() == [2]


def test_fire_texture_variants_set_dominant_emitted_light_colour():
    expected = np.asarray((
        (0xE4, 0x92, 0x34),
        (0xFF, 0x9A, 0x00),
        (0xFC, 0x24, 0x00),
        (0xFE, 0xAC, 0x1D),
    ), dtype=np.float32) / 255.0

    for variant, colour in enumerate(expected):
        effect = Effect(properties={
            "effect_type": EFFECT_FIRE,
            "fire_texture": EFFECT_FIRE_TEXTURES[variant],
        })
        table = EntityTable()
        table.begin_frame([effect], epoch=1, effect_runtime=False)

        np.testing.assert_allclose(table.effect_light_color[0], colour)
        np.testing.assert_allclose(table.light_color[0], colour)


def test_explosion_preview_is_editor_only_and_starts_at_frame_zero():
    explosion = Effect(properties={
        "effect_type": EFFECT_EXPLOSION,
        "preview": True,
        "lifetime": 0.5,
    })
    table = EntityTable()

    table.begin_frame([explosion], epoch=1, effect_runtime=False)
    assert table.effect_type[0] == 1
    assert bool(table.effect_preview[0])
    assert not bool(table.effect_active[0])
    assert bool(table.effect_alive[0])
    # Human-facing frame 10 is atlas index 9; the midpoint of that frame keeps
    # the shader's floor(t * 16) selection unambiguous.
    expected_elapsed = 0.5 * (9.5 / 16.0)
    assert abs(float(table.effect_elapsed[0]) - expected_elapsed) < 1e-6

    table.begin_frame([explosion], epoch=1, effect_runtime=True)
    assert not bool(table.effect_active[0])
    assert not bool(table.effect_alive[0])


def test_explosion_preview_off_remains_dormant_in_editor():
    explosion = Effect(properties={
        "effect_type": EFFECT_EXPLOSION,
        "preview": False,
    })
    table = EntityTable()
    table.begin_frame([explosion], epoch=1, effect_runtime=False)

    assert not bool(table.effect_preview[0])
    assert not bool(table.effect_alive[0])


def test_effect_billboard_width_and_height_are_projected_directly():
    effect = Effect(properties={
        "effect_type": EFFECT_EXPLOSION,
        "width": 48.0,
        "height": 18.0,
        "preview": True,
    })
    table = EntityTable()
    table.begin_frame([effect], epoch=1, effect_runtime=False)

    assert float(table.effect_params[0, 0]) == 48.0
    np.testing.assert_allclose(table.sprite_size[0], (48.0, 18.0))


def test_fire_preview_flag_is_available_for_the_same_effect_primitive():
    effect = Effect(properties={
        "effect_type": EFFECT_FIRE,
        "preview": True,
    })
    table = EntityTable()
    table.begin_frame([effect], epoch=1, effect_runtime=False)

    assert bool(table.effect_preview[0])
    assert bool(table.effect_alive[0])
    np.testing.assert_allclose(table.sprite_size[0], (32.0, 24.0))


def test_explosion_light_is_a_short_runtime_flash_not_a_constant_source():
    explosion = Effect(properties={
        "effect_type": EFFECT_EXPLOSION,
        "lifetime": 0.5,
    })
    table = EntityTable()
    table.begin_frame([explosion], epoch=1, effect_runtime=True)
    assert not bool(table.effect_active[0])
    assert not bool(table.light_enabled[0])

    import time
    table.effect_spawn_time[0] = time.perf_counter() - 0.02
    table.effect_active[0] = True
    table.begin_frame([explosion], epoch=1, effect_runtime=True)
    assert bool(table.effect_alive[0])
    assert bool(table.light_enabled[0])
    assert float(table.light_params[0, 0]) > float(table.effect_params[0, 2])

    table.effect_spawn_time[0] = time.perf_counter() - 0.25
    table.effect_active[0] = True
    table.begin_frame([explosion], epoch=1, effect_runtime=True)
    assert not bool(table.light_enabled[0])


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


def test_effect_set_type_input_changes_type_and_fires_onchanged():
    effect = Effect(properties={
        "id": "type-test",
        "effect_type": EFFECT_FIRE,
    })
    table = EntityTable()
    table.begin_frame([effect], epoch=1, effect_runtime=True)

    events = []
    io_manager = SimpleNamespace(
        fire_output=lambda entity, name, value=None: events.append((name, value))
    )
    logic = SimpleNamespace(_entity_table=table, io_manager=io_manager)
    real_io = IOManager()
    register_all_input_handlers(real_io)

    assert get_input_names("effect") == ["SetType", "Explode"]
    assert get_output_names("effect") == ["OnChanged"]

    set_type = real_io._input_handlers[("effect", "settype")]
    set_type(effect, "explosion", logic)

    assert effect.properties["effect_type"] == EFFECT_EXPLOSION
    assert table.effect_type[0] == 1
    assert not bool(table.effect_active[0])
    assert not bool(table.effect_alive[0])
    assert not bool(table.light_enabled[0])
    assert events == [("OnChanged", "EXPLOSION")]

    set_type(effect, "explosion", logic)
    assert events == [("OnChanged", "EXPLOSION")]

    set_type(effect, "fire", logic)
    assert effect.properties["effect_type"] == EFFECT_FIRE
    assert table.effect_type[0] == 0
    assert bool(table.effect_active[0])
    assert bool(table.effect_alive[0])
    assert events[-1] == ("OnChanged", "FIRE")

    set_type(effect, "future_type", logic)
    assert effect.properties["effect_type"] == EFFECT_FIRE
    assert events[-1] == ("OnChanged", "FIRE")


def test_explode_input_forces_fire_to_explosion_and_never_reverts():
    effect = Effect(properties={
        "id": "fire-to-explosion",
        "effect_type": EFFECT_FIRE,
    })
    table = EntityTable()
    table.begin_frame([effect], epoch=1, effect_runtime=True)
    assert table.effect_type[0] == 0
    assert bool(table.effect_alive[0])

    io_manager = IOManager()
    register_all_input_handlers(io_manager)
    logic = SimpleNamespace(_entity_table=table, io_manager=io_manager)
    explode = io_manager._input_handlers[("effect", "explode")]

    explode(effect, "", logic)

    assert effect.properties["effect_type"] == EFFECT_EXPLOSION
    assert effect.properties["preview"] is False
    assert table.effect_type[0] == 1
    assert bool(table.effect_active[0])
    assert bool(table.effect_alive[0])
    assert float(table.effect_elapsed[0]) == 0.0

    table.effect_spawn_time[0] -= 1.0
    table.begin_frame([effect], epoch=1, effect_runtime=True)
    assert not bool(table.effect_active[0])
    assert not bool(table.effect_alive[0])
    assert effect.properties["effect_type"] == EFFECT_EXPLOSION

    explode(effect, "", logic)
    assert effect.properties["effect_type"] == EFFECT_EXPLOSION
    assert table.effect_type[0] == 1
    assert bool(table.effect_active[0])
    assert bool(table.effect_alive[0])
    assert float(table.effect_elapsed[0]) == 0.0


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
