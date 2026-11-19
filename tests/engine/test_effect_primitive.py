import pytest

pytest.importorskip("PyQt5", reason="Effect is an editor Thing")
pytestmark = pytest.mark.qt

from editor.things import Effect, Thing
from engine.effect_entity import (
    EFFECT_EXPLOSION,
    EFFECT_FIRE,
    EFFECT_ORB,
    EFFECT_CUSTOM,
    EFFECT_FIRE_TEXTURES,
    EFFECT_ORB_TEXTURES,
)
from engine.entity_table import ENT_EFFECT, EntityTable
from engine.effect_table import EffectStore
from engine.threaded_game_state import ThreadedGameState
from editor.io_system import IOManager, get_input_names, get_output_names
from editor.io_handlers import register_all_input_handlers

import numpy as np


def _io_for(effect, events=None):
    """Drive the Effect I/O path through the production LogicThread."""
    from editor.editor_state import EditorState
    from engine.logic_thread import LogicThread

    state = EditorState()
    state.things = [effect]
    state.brushes = []
    logic = LogicThread(ThreadedGameState(), state)
    logic.world_runtime.build_entity_caches()
    logic.session_runtime.effect_store.begin_session([effect])

    if events is not None:
        real_fire_output = logic.io_manager.fire_output

        def record_and_dispatch(entity, name, value=None):
            events.append((name, value))
            return real_fire_output(entity, name, value)

        logic.io_manager.fire_output = record_and_dispatch

    def send(input_name, param=""):
        logic.io_manager._execute_input(effect.properties.get("name", ""),
                                        input_name, param, "test")

    send.effect_store = logic.session_runtime.effect_store
    send.logic = logic
    return send

def test_effect_defaults_to_fire_with_intrinsic_light():
    effect = Effect()
    assert effect.properties["type"] == "effect"
    assert effect.properties["effect_type"] == EFFECT_FIRE
    assert effect.properties["preview"] is False
    assert effect.properties["silent"] is False
    assert effect.properties["fire_texture"] == EFFECT_FIRE_TEXTURES[0]
    assert len(EFFECT_FIRE_TEXTURES) == 5
    assert effect.properties["width"] == 32.0
    assert effect.properties["height"] == 46.0
    assert effect.properties["orb_texture"] == EFFECT_ORB_TEXTURES[0]
    assert effect.properties["custom_loop"] is True
    assert "size" not in effect.properties
    assert "scale" not in effect.properties
    assert effect.properties["light_enabled"] is True
    assert effect.properties["light_radius"] == 128.0
    assert effect.properties["light_intensity"] == 2.5
    assert effect.properties["effect_seed"] != 0


def test_orb_defaults_to_blue_square_animation():
    orb = Effect(properties={"effect_type": EFFECT_ORB})
    assert orb.properties["effect_type"] == EFFECT_ORB
    assert orb.properties["orb_texture"] == EFFECT_ORB_TEXTURES[0]
    assert orb.properties["width"] == 32.0
    assert orb.properties["height"] == 32.0
    assert orb.properties["light_colour"] == [74, 155, 255]

    table = EntityTable()
    table.begin_frame([orb], epoch=1, effect_runtime=False)

    assert table.effect_type[0] == 2
    assert bool(table.effect_alive[0])
    assert bool(table.effect_active[0])
    np.testing.assert_allclose(table.sprite_size[0], (32.0, 32.0))
    np.testing.assert_allclose(
        table.effect_light_color[0],
        np.asarray((0x4A, 0x9B, 0xFF), dtype=np.float32) / 255.0,
    )


def test_explosion_trigger_queues_centered_sound():
    from editor.io_handlers import register_all_input_handlers

    effect = Effect(
        pos=(10.0, 20.0, 30.0),
        properties={"effect_type": EFFECT_FIRE, "silent": False},
    )
    send = _io_for(effect)
    logic = send.logic
    register_all_input_handlers(logic.io_manager)
    logic.io_manager._input_handlers[("effect", "explode")](effect, "", logic)

    assert list(logic.game_state.sound_queue) == [{
        "action": "play",
        "file": "assets/sounds/explode.mp3",
        "volume": 1.0,
        "position": [10.0, 20.0, 30.0],
    }]

def test_silent_explosion_does_not_queue_sound():
    from editor.io_handlers import register_all_input_handlers

    effect = Effect(
        pos=(1.0, 2.0, 3.0),
        properties={"effect_type": EFFECT_FIRE, "silent": True},
    )
    send = _io_for(effect)
    logic = send.logic
    register_all_input_handlers(logic.io_manager)
    logic.io_manager._input_handlers[("effect", "explode")](effect, "", logic)

    assert list(logic.game_state.sound_queue) == []

def test_animation_origin_is_shared_across_render_buffers():
    """Alternating RenderState buffers must not reset an animated GIF's phase.

    Both resolve the origin from the same place -- the shared clock, for an
    Effect with no playback start -- and neither writes it back.
    """
    effect = Effect()
    first = EntityTable()
    second = EntityTable()

    first.begin_frame([effect], epoch=1, effect_runtime=True)
    second.begin_frame([effect], epoch=1, effect_runtime=True)

    assert float(first.effect_spawn_time[0]) > 0.0
    assert first.effect_spawn_time[0] == second.effect_spawn_time[0]
    assert first.effect_phase[0] == second.effect_phase[0]


def test_both_buffers_agree_after_an_explode():
    """The I/O handler used to write only the table the logic thread held.

    With two buffers alternating, the other one kept drawing FIRE: frames
    flickered between FIRE and EXPLOSION until something reconciled.
    """
    effect = Effect(properties={"name": "boom", "effect_type": EFFECT_FIRE})
    tables = [EntityTable(), EntityTable()]
    for table in tables:
        table.begin_frame([effect], epoch=1, effect_runtime=True)

    send = _io_for(effect)
    send("Explode")
    for table in tables:
        table.begin_frame(
            [effect], epoch=1, effect_runtime=True, effect_store=send.effect_store
        )

    for table in tables:
        assert table.effect_type[0] == 1
        assert bool(table.effect_alive[0])
    assert tables[0].effect_spawn_time[0] == tables[1].effect_spawn_time[0]


def test_explosion_origin_is_shared_across_render_buffers():
    """Triggered one-shot Effects keep one timestamp in both dense tables."""
    effect = Effect(properties={"effect_type": "EXPLOSION"})
    store = EffectStore()
    store.begin_session([effect])
    store.trigger_explosion(effect, 123.456)

    first = EntityTable()
    second = EntityTable()

    first.begin_frame([effect], epoch=1, effect_runtime=True, effect_store=store)
    second.begin_frame([effect], epoch=1, effect_runtime=True, effect_store=store)

    assert float(first.effect_spawn_time[0]) == 123.456
    assert float(second.effect_spawn_time[0]) == 123.456


def test_custom_effect_uses_selected_gif_path():
    custom = Effect(properties={
        "effect_type": EFFECT_CUSTOM,
        "custom_gif": r"custom\magic.gif",
    })
    assert custom.properties["effect_type"] == EFFECT_CUSTOM
    assert custom.properties["custom_gif"] == "custom/magic.gif"

    table = EntityTable()
    table.begin_frame([custom], epoch=1, effect_runtime=False)

    assert table.effect_type[0] == 3
    assert table.effect_custom_id[0] > 0
    assert table.effect_custom_path(table.effect_custom_id[0]) == "custom/magic.gif"
    assert bool(table.effect_custom_loop[0])
    assert bool(table.effect_active[0])
    assert bool(table.effect_alive[0])

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
    assert table.effect_fire_variant[:table.count].tolist() == [2]


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


def test_explosion_preview_is_editor_only_and_static_at_frame_ten():
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
    # Human-facing frame 10 is atlas index 9, shown whole (unblended) at
    # (10 - 0.5) / 17 of the lifetime: the sheet has 17 frames.
    expected_elapsed = 0.5 * (9.5 / 17.0)
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
    np.testing.assert_allclose(table.sprite_size[0], (32.0, 46.0))


def test_explosion_light_flashes_then_fades_out_with_the_fireball():
    """Off until triggered; a flash above the authored intensity; on for the
    whole explosion, easing to nothing by its end (no step down while still
    bright, as the old 0.12 s cut-off had); off once it is over."""
    import time
    explosion = Effect(properties={
        "effect_type": EFFECT_EXPLOSION,
        "lifetime": 0.5,
    })
    table = EntityTable()
    table.begin_frame([explosion], epoch=1, effect_runtime=True)
    assert not bool(table.effect_active[0])
    assert not bool(table.light_enabled[0])
    base = float(table.effect_params[0, 2])

    def light_at(elapsed):
        table.effect_spawn_time[0] = time.perf_counter() - elapsed
        table.effect_active[0] = True
        table.begin_frame([explosion], epoch=1, effect_runtime=True)
        return bool(table.light_enabled[0]), float(table.light_params[0, 0])

    on, flash = light_at(0.02)
    assert on and flash > base
    on, middle = light_at(0.25)            # past the old cut-off: still lit
    assert on and 0.0 < middle < flash
    on, late = light_at(0.49)
    assert on and late < 0.01 * base       # all but out just before the end
    on, _ = light_at(0.6)
    assert not on and not bool(table.effect_alive[0])


def test_explosion_light_scale_is_continuous_and_ends_at_zero():
    from engine.effect_entity import explosion_light_scale
    life = 0.8
    samples = [explosion_light_scale(life * i / 400.0, life) for i in range(401)]
    assert samples[0] == pytest.approx(3.0)            # 1 + GAIN at the trigger
    assert samples[-1] == 0.0
    assert all(s >= 0.0 for s in samples)
    # No jumps: neighbouring samples 2 ms apart differ by little.
    assert max(abs(a - b) for a, b in zip(samples, samples[1:])) < 0.15
    # Falls throughout: never brighter later.
    assert all(b <= a + 1e-12 for a, b in zip(samples, samples[1:]))


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
    send = _io_for(effect, events)

    def set_type(entity, value, _logic):
        send("SetType", value)
        table.begin_frame(
            [effect], epoch=1, effect_runtime=True, effect_store=send.effect_store
        )

    logic = None

    assert get_input_names("effect") == [
        "SetType", "SetFireTexture", "SetOrbTexture",
        "SetCustomGif", "SetLoop", "SetEmitter",
        "Hide", "Show", "ToggleVisibility", "Explode",
    ]
    assert get_output_names("effect") == [
        "OnChanged", "OnExplode", "OnExplodeFinished"]

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

    set_type(effect, "orb", logic)
    assert effect.properties["effect_type"] == EFFECT_ORB
    assert table.effect_type[0] == 2
    assert bool(table.effect_active[0])
    assert bool(table.effect_alive[0])
    np.testing.assert_allclose(table.sprite_size[0], (32.0, 32.0))
    assert events[-1] == ("OnChanged", "ORB")

    set_type(effect, "custom", logic)
    assert effect.properties["effect_type"] == EFFECT_CUSTOM
    assert table.effect_type[0] == 3
    assert bool(table.effect_active[0])
    assert bool(table.effect_alive[0])
    assert events[-1] == ("OnChanged", "CUSTOM")



def test_effect_texture_and_custom_inputs_update_dense_projection():
    effect = Effect(properties={
        "id": "effect-input-test",
        "effect_type": EFFECT_FIRE,
    })
    table = EntityTable()
    table.begin_frame([effect], epoch=1, effect_runtime=True)

    events = []
    send = _io_for(effect, events)

    def deliver(input_name, param):
        send(input_name, param)
        table.begin_frame(
            [effect], epoch=1, effect_runtime=True, effect_store=send.effect_store
        )

    deliver("SetFireTexture", "3")
    assert effect.properties["fire_texture"] == EFFECT_FIRE_TEXTURES[2]
    assert table.effect_fire_variant[0] == 2

    deliver("SetFireTexture", "1")
    assert effect.properties["fire_texture"] == EFFECT_FIRE_TEXTURES[0]
    assert table.effect_fire_variant[0] == 0

    deliver("SetOrbTexture", "5")
    assert effect.properties["orb_texture"] == EFFECT_ORB_TEXTURES[4]

    deliver("SetOrbTexture", "4")
    assert effect.properties["orb_texture"] == EFFECT_ORB_TEXTURES[3]

    deliver("SetCustomGif", r"custom\\pulse.gif")
    assert effect.properties["custom_gif"] == "custom/pulse.gif"

    deliver("SetLoop", "false")
    assert effect.properties["custom_loop"] is False
    assert not bool(table.effect_custom_loop[0])

    deliver("SetLoop", "true")
    assert effect.properties["custom_loop"] is True
    assert bool(table.effect_custom_loop[0])

    assert events[-1] == ("OnChanged", "true")


def test_effect_texture_inputs_ignore_invalid_variants():
    effect = Effect(properties={"id": "invalid-effect-input"})
    table = EntityTable()
    table.begin_frame([effect], epoch=1, effect_runtime=True)
    send = _io_for(effect)
    logic = send.logic
    register_all_input_handlers(logic.io_manager)
    io_manager = logic.io_manager

    io_manager._input_handlers[("effect", "setfiretexture")](effect, "6", logic)
    io_manager._input_handlers[("effect", "setfiretexture")](effect, "fire01", logic)
    io_manager._input_handlers[("effect", "setfiretexture")](effect, "FIRE 3", logic)
    io_manager._input_handlers[("effect", "setorbtexture")](effect, "0", logic)
    io_manager._input_handlers[("effect", "setorbtexture")](effect, "orb05", logic)
    io_manager._input_handlers[("effect", "setorbtexture")](effect, "ORB 4", logic)

    assert effect.properties["fire_texture"] == EFFECT_FIRE_TEXTURES[0]
    assert effect.properties["orb_texture"] == EFFECT_ORB_TEXTURES[0]


def test_explode_with_return_off_makes_fire_an_explosion_for_good():
    effect = Effect(properties={
        "id": "fire-to-explosion",
        "effect_type": EFFECT_FIRE,
        "return_after_explosion": False,
    })
    table = EntityTable()
    table.begin_frame([effect], epoch=1, effect_runtime=True)
    assert table.effect_type[0] == 0
    assert bool(table.effect_alive[0])

    send = _io_for(effect)

    def explode():
        send("Explode")
        table.begin_frame(
            [effect], epoch=1, effect_runtime=True, effect_store=send.effect_store
        )

    # "Just started": well inside the 0.5 s default lifetime -- a restart
    # that failed would read a second or more. Wall-clock time passes
    # between the send and the frame, so not a tighter bound (a busy CI
    # runner measured 0.15 s).
    just_started = 0.4

    explode()

    assert effect.properties["effect_type"] == EFFECT_EXPLOSION
    assert effect.properties["preview"] is False
    assert table.effect_type[0] == 1
    assert bool(table.effect_active[0])
    assert bool(table.effect_alive[0])
    assert float(table.effect_elapsed[0]) < just_started

    table.effect_spawn_time[0] -= 1.0          # a second later
    table.begin_frame([effect], epoch=1, effect_runtime=True)
    assert not bool(table.effect_alive[0])
    assert effect.properties["effect_type"] == EFFECT_EXPLOSION

    explode()
    assert effect.properties["effect_type"] == EFFECT_EXPLOSION
    assert table.effect_type[0] == 1
    assert bool(table.effect_active[0])
    assert bool(table.effect_alive[0])
    assert float(table.effect_elapsed[0]) < just_started


def test_a_fire_that_explodes_burns_again_afterwards():
    """The default: Explode plays over the FIRE, which is never rewritten,
    and the FIRE is back once the explosion has played -- OnExplode when it
    goes off, OnExplodeFinished when it is back."""
    import time
    from engine.explosions import finish_explosions
    effect = Effect(properties={"name": "torch", "effect_type": EFFECT_FIRE,
                                "lifetime": 0.5})
    events = []
    send = _io_for(effect, events)
    logic, store = send.logic, send.effect_store
    logic.session_runtime.play_mode = True
    table = EntityTable()

    send("Explode")
    table.begin_frame([effect], epoch=1, effect_runtime=True, effect_store=store)
    assert effect.properties["effect_type"] == EFFECT_FIRE      # authored, untouched
    assert table.effect_type[0] == 1 and bool(table.effect_alive[0])
    assert ("OnExplode", None) in events
    assert store.returning(effect) == EFFECT_FIRE

    assert finish_explosions(logic, now=time.perf_counter()) == []   # still playing
    finished = finish_explosions(logic, now=time.perf_counter() + 0.6)
    assert finished == [(effect, EFFECT_FIRE)]
    assert ("OnExplodeFinished", None) in events
    table.begin_frame([effect], epoch=1, effect_runtime=True, effect_store=store)
    assert table.effect_type[0] == 0 and bool(table.effect_alive[0])
    assert store.returning(effect) is None
    logic.session_runtime.play_mode = False


def test_stopping_play_mid_explosion_leaves_the_fire_a_fire():
    effect = Effect(properties={"name": "torch", "effect_type": EFFECT_FIRE})
    send = _io_for(effect)
    store = send.effect_store
    send("Explode")
    assert store.family_id[store.index_of(effect)] == 1
    assert store.cancel_returns() == [(effect, EFFECT_FIRE)]
    assert store.family_id[store.index_of(effect)] == 0
    assert effect.properties["effect_type"] == EFFECT_FIRE


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

    assert get_input_names("effect") == [
        "SetType", "SetFireTexture", "SetOrbTexture",
        "SetCustomGif", "SetLoop", "SetEmitter",
        "Hide", "Show", "ToggleVisibility", "Explode",
    ]

    send = _io_for(explosion)

    def explode():
        send("Explode")
        table.begin_frame(
            [explosion], epoch=1, effect_runtime=True, effect_store=send.effect_store
        )

    explode()
    assert bool(table.effect_active[0])
    assert bool(table.effect_alive[0])
    assert float(table.effect_elapsed[0]) < 0.1

    table.effect_spawn_time[0] -= 1.0          # a second later
    table.begin_frame([explosion], epoch=1, effect_runtime=True)
    assert not bool(table.effect_alive[0])
    assert not bool(table.light_enabled[0])

    explode()
    assert bool(table.effect_active[0])
    assert bool(table.effect_alive[0])
    assert float(table.effect_elapsed[0]) < 0.1


def test_a_looping_fire_in_play_runs_on_the_origin_it_publishes():
    """A FIRE with no playback start animates on the shared clock origin, the
    value its effect_spawn_time column carries -- not on perf_counter's own
    epoch, which as a float32 elapsed loses precision with the machine's
    uptime (and is far slower to take sin() of)."""
    import time
    from engine.entity_table import _CLOCK_ORIGIN

    effect = Effect(properties={"effect_type": EFFECT_FIRE})
    store = EffectStore()
    store.begin_session([effect])
    assert float(store.spawn_time[0]) == 0.0

    table = EntityTable()
    before = time.perf_counter()
    table.begin_frame([effect], epoch=1, effect_runtime=True, effect_store=store)
    after = time.perf_counter()

    assert float(table.effect_spawn_time[0]) == _CLOCK_ORIGIN
    elapsed = float(table.effect_elapsed[0])
    assert before - _CLOCK_ORIGIN - 1e-3 <= elapsed <= after - _CLOCK_ORIGIN + 1e-3
