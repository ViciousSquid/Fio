"""An Effect's optional particle emitter, and the PARTICLES Effect type.

Every Effect may carry an emitter (``emitter``, a preset name); PARTICLES is
the emitter alone. Pinned here:

* property defaults and validation, on construction;
* PARTICLES defaults: an emitter, an extent, and no light unless asked for;
* the EntityTable columns the renderer reads (``effect_emitter``,
  ``effect_emitter_params``, ``effect_emitter_tint``) are resolved with the
  static Effect columns: on an authored change, not with the clock, and
  carried by ``adopt`` to the other render buffer;
* maps saved before emitters existed load with emitter NONE everywhere;
* the ``SetEmitter`` input;
* the shader's preset table lines up with the preset names.
"""

import json

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="Effect is an editor Thing")
pytestmark = pytest.mark.qt

from editor.things import Effect                                # noqa: E402
from engine import shaders                                      # noqa: E402
from engine.effect_entity import (                              # noqa: E402
    EFFECT_FIRE, EFFECT_PARTICLES, EFFECT_TYPES, EMITTER_IDS,
    EMITTER_MAX_PARTICLES, EMITTER_PRESETS, PARTICLES_DEFAULT_SIZE)
from engine.effect_table import (                               # noqa: E402
    FAMILY_PARTICLES, EffectStore, effect_family)
from engine.change_journal import touch                         # noqa: E402
from engine.entity_table import EntityTable                     # noqa: E402

from tests.engine.test_effect_primitive import _io_for          # noqa: E402


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

def test_an_effect_has_no_emitter_by_default():
    props = Effect().properties
    assert props["emitter"] == "NONE"
    assert props["emitter_rate"] == 1.0
    assert props["emitter_scale"] == 1.0
    assert props["emitter_tint"] == [255, 255, 255]


def test_particles_is_an_effect_type_with_its_own_defaults():
    assert EFFECT_PARTICLES in EFFECT_TYPES
    props = Effect(properties={"effect_type": "particles"}).properties
    assert props["effect_type"] == EFFECT_PARTICLES
    assert props["light_enabled"] is False
    assert props["emitter"] == "SMOKE"            # it emits something
    assert (props["width"], props["height"]) == PARTICLES_DEFAULT_SIZE


def test_particles_keeps_what_the_map_says():
    props = Effect(properties={
        "effect_type": "PARTICLES", "light_enabled": True, "emitter": "NONE",
        "width": 200.0, "height": 12.0}).properties
    assert props["light_enabled"] is True
    assert props["emitter"] == "NONE"
    assert (props["width"], props["height"]) == (200.0, 12.0)


def test_other_types_keep_their_light_and_take_an_emitter():
    props = Effect(properties={"effect_type": "FIRE", "emitter": "smoke"}).properties
    assert props["light_enabled"] is True
    assert props["emitter"] == "SMOKE"


@pytest.mark.parametrize("value, expected", [
    ("SMOKE", "SMOKE"), (" steam ", "STEAM"), ("Sparks", "SPARKS"),
    ("SPLASH", "SPLASH"), ("mist", "MIST"), ("none", "NONE"),
    ("FOG", "NONE"), ("", "NONE"), (None, "NONE"), (3, "NONE"),
])
def test_the_emitter_must_be_a_preset_name(value, expected):
    assert Effect(properties={"emitter": value}).properties["emitter"] == expected


@pytest.mark.parametrize("key, value, expected", [
    ("emitter_rate", 0.25, 0.25),
    ("emitter_rate", 2.0, 1.0),
    ("emitter_rate", -1.0, 0.0),
    ("emitter_rate", "0.5", 0.5),
    ("emitter_rate", "lots", 1.0),
    ("emitter_rate", float("nan"), 1.0),
    ("emitter_scale", 2.5, 2.5),
    ("emitter_scale", 0.0, 0.1),
    ("emitter_scale", 100.0, 8.0),
    ("emitter_scale", None, 1.0),
])
def test_rate_and_scale_are_clamped(key, value, expected):
    assert Effect(properties={key: value}).properties[key] == pytest.approx(expected)


@pytest.mark.parametrize("value, expected", [
    ([10, 20, 30], [10, 20, 30]),
    ([300, -5, 128.4], [255, 0, 128]),
    ((1, 2, 3, 4), [1, 2, 3]),
    ([1, 2], [255, 255, 255]),
    ("red", [255, 255, 255]),
    (None, [255, 255, 255]),
])
def test_the_tint_is_an_rgb_triple(value, expected):
    assert Effect(properties={"emitter_tint": value}).properties["emitter_tint"] == expected


def test_particles_is_a_looping_family_active_from_the_start():
    effect = Effect(properties={"effect_type": "PARTICLES"})
    assert effect_family("PARTICLES") == FAMILY_PARTICLES
    store = EffectStore()
    store.rebuild([effect])
    row = store.index_of(effect)
    assert store.family_id[row] == FAMILY_PARTICLES
    assert bool(store.active[row])


# ---------------------------------------------------------------------------
# EntityTable columns
# ---------------------------------------------------------------------------

def _table(things, runtime=False, store=None):
    table = EntityTable()
    table.begin_frame(list(things), epoch=1, effect_runtime=runtime,
                      effect_store=store)
    return table


def test_emitter_columns_have_the_documented_layout():
    table = _table([Effect()])
    assert table.effect_emitter.dtype == np.uint8
    assert table.effect_emitter_params.dtype == np.float32
    assert table.effect_emitter_params.shape[1:] == (4,)
    assert table.effect_emitter_tint.shape[1:] == (3,)


def test_emitter_columns_are_resolved_from_the_authored_effect():
    fire = Effect(properties={
        "effect_type": "FIRE", "emitter": "SMOKE", "emitter_rate": 0.5,
        "emitter_scale": 2.0, "emitter_tint": [255, 0, 51],
        "width": 40.0, "height": 60.0})
    plain = Effect(properties={"effect_type": "FIRE"})
    mist = Effect(properties={"effect_type": "PARTICLES", "emitter": "MIST",
                              "width": 300.0, "height": 20.0})
    table = _table([fire, plain, mist])

    assert table.effect_emitter[0] == EMITTER_IDS["SMOKE"]
    np.testing.assert_allclose(table.effect_emitter_params[0], (0.5, 2.0, 40.0, 60.0))
    np.testing.assert_allclose(table.effect_emitter_tint[0], (1.0, 0.0, 0.2), atol=1e-6)
    assert table.effect_type[0] == 0

    assert table.effect_emitter[1] == 0

    assert table.effect_emitter[2] == EMITTER_IDS["MIST"]
    assert table.effect_type[2] == FAMILY_PARTICLES
    np.testing.assert_allclose(table.effect_emitter_params[2, 2:], (300.0, 20.0))
    assert not bool(table.light_enabled[2])       # PARTICLES: no light


def test_particles_is_family_four_with_and_without_a_store():
    effect = Effect(properties={"effect_type": "PARTICLES"})
    store = EffectStore()
    store.rebuild([effect])
    assert _table([effect]).effect_type[0] == FAMILY_PARTICLES
    assert _table([effect], runtime=True, store=store).effect_type[0] == FAMILY_PARTICLES


def test_a_particles_effect_is_alive_in_the_editor_and_in_play():
    effect = Effect(properties={"effect_type": "PARTICLES"})
    store = EffectStore()
    store.begin_session([effect])
    assert bool(_table([effect]).effect_alive[0])
    assert bool(_table([effect], runtime=True, store=store).effect_alive[0])


def test_an_authored_emitter_change_republishes_the_columns():
    effect = Effect(properties={"effect_type": "FIRE"})
    store = EffectStore()
    store.begin_session([effect])
    table = _table([effect], runtime=True, store=store)
    assert table.effect_emitter[0] == 0

    effect.properties["emitter"] = "SPARKS"
    effect.properties["emitter_rate"] = 0.25
    touch(effect)              # whoever writes a resolved value says so
    table.begin_frame([effect], epoch=1, effect_runtime=True, effect_store=store)
    assert table.effect_emitter[0] == EMITTER_IDS["SPARKS"]
    assert table.effect_emitter_params[0, 0] == pytest.approx(0.25)


def test_the_clock_does_not_touch_the_emitter_columns():
    effect = Effect(properties={"effect_type": "FIRE", "emitter": "SMOKE"})
    store = EffectStore()
    store.begin_session([effect])
    table = _table([effect], runtime=True, store=store)
    before = (table.effect_emitter.copy(), table.effect_emitter_params.copy(),
              table.effect_emitter_tint.copy())
    # A store change publishes the execution columns (generation moves)...
    store.set_type(effect, "ORB")
    table.begin_frame([effect], epoch=1, effect_runtime=True, effect_store=store)
    assert table.effect_type[0] == 2
    # ...and a plain tick: neither changes what the author set.
    table.begin_frame([effect], epoch=1, effect_runtime=True, effect_store=store)
    np.testing.assert_array_equal(table.effect_emitter, before[0])
    np.testing.assert_array_equal(table.effect_emitter_params, before[1])
    np.testing.assert_array_equal(table.effect_emitter_tint, before[2])


def test_the_other_render_buffer_adopts_the_emitter_columns():
    effect = Effect(properties={"effect_type": "PARTICLES", "emitter": "STEAM",
                                "emitter_rate": 0.75})
    first = _table([effect])
    second = EntityTable()
    second.adopt(first)
    assert second.effect_emitter[0] == EMITTER_IDS["STEAM"]
    np.testing.assert_array_equal(second.effect_emitter_params[:1],
                                  first.effect_emitter_params[:1])
    # A copy, not a view: the buffers stay independent.
    second.effect_emitter[0] = 0
    assert first.effect_emitter[0] == EMITTER_IDS["STEAM"]


def test_a_row_that_stops_being_an_effect_clears_its_emitter():
    from editor.things import Light
    effect = Effect(properties={"effect_type": "FIRE", "emitter": "SMOKE"})
    table = _table([effect])
    assert table.effect_emitter[0] != 0
    light = Light()
    table.begin_frame([light], epoch=2)
    assert table.effect_emitter[0] == 0
    assert not table.effect_emitter_params[0].any()


# ---------------------------------------------------------------------------
# Old maps
# ---------------------------------------------------------------------------

#: An Effect record as maps saved before emitters existed hold it.
_LEGACY_EFFECT = {
    "type": "effect", "pos": [10, 20, 30],
    "properties": {
        "type": "effect", "io_enabled": True, "name": "Effect_1",
        "id": "e540bb6c-509a-46f5-a8fe-0000a4706f1d", "effect_type": "FIRE",
        "preview": False, "silent": False,
        "fire_texture": "assets/textures/effects/fire01.gif",
        "orb_texture": "assets/textures/effects/orb01.gif",
        "custom_gif": "", "custom_loop": True, "width": 152.89,
        "height": 227.78, "intensity": 1.91, "colour": [255, 75, 186],
        "lifetime": 0.5, "light_enabled": True, "light_radius": 480.95,
        "light_intensity": 2.5, "light_colour": [255, 53, 124],
        "effect_seed": 2620696981,
    },
}


def _load(data):
    from editor.editor_state import EditorState
    state = EditorState()
    state.load_from_data(json.loads(json.dumps(data)))
    return [t for t in state.things if isinstance(t, Effect)]


def test_an_effect_from_an_old_map_loads_without_an_emitter():
    (effect,) = _load({"brushes": [], "things": [_LEGACY_EFFECT]})
    assert effect.properties["emitter"] == "NONE"
    assert effect.properties["effect_type"] == EFFECT_FIRE
    assert effect.properties["light_enabled"] is True
    table = _table([effect])
    assert table.effect_emitter[0] == 0
    assert table.effect_type[0] == 0


@pytest.mark.parametrize("path", ["maps/_SHOWCASE.json", "maps/DevTest.json"])
def test_the_shipped_maps_load_with_every_emitter_off(path):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    effects = _load(data)
    assert effects, "the map was expected to hold Effects"
    assert all(e.properties["emitter"] == "NONE" for e in effects)
    table = _table(effects)
    assert not table.effect_emitter[table.effect_slots].any()


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def test_set_emitter_input_sets_the_preset_and_fires_onchanged():
    effect = Effect(properties={"id": "emitter-io", "effect_type": "FIRE"})
    events = []
    send = _io_for(effect, events)
    table = EntityTable()

    def deliver(value):
        send("SetEmitter", value)
        table.begin_frame([effect], epoch=1, effect_runtime=True,
                          effect_store=send.effect_store)

    deliver("smoke")
    assert effect.properties["emitter"] == "SMOKE"
    assert table.effect_emitter[0] == EMITTER_IDS["SMOKE"]
    assert events == [("OnChanged", "SMOKE")]

    deliver("SMOKE")                      # unchanged: no event
    assert events == [("OnChanged", "SMOKE")]

    deliver("lava")                       # not a preset: ignored
    assert effect.properties["emitter"] == "SMOKE"
    assert len(events) == 1

    deliver("none")
    assert effect.properties["emitter"] == "NONE"
    assert table.effect_emitter[0] == 0
    assert events[-1] == ("OnChanged", "NONE")


def test_set_type_particles_gives_an_emitterless_effect_an_emitter():
    effect = Effect(properties={"id": "to-particles", "effect_type": "FIRE"})
    send = _io_for(effect)
    send("SetType", "particles")
    assert effect.properties["effect_type"] == EFFECT_PARTICLES
    assert effect.properties["emitter"] == "SMOKE"
    store = send.effect_store
    assert store.family_id[store.index_of(effect)] == FAMILY_PARTICLES


# ---------------------------------------------------------------------------
# Shader tables
# ---------------------------------------------------------------------------

def test_the_preset_table_follows_the_preset_names():
    table = shaders.particle_preset_table()
    vec4s = shaders.PARTICLE_PRESET_VEC4S
    assert table.shape == (len(EMITTER_PRESETS) * vec4s, 4)
    assert table.dtype == np.float32
    assert not table[:vec4s].any()                          # NONE
    lives = shaders.particle_preset_lives()
    for index, name in enumerate(EMITTER_PRESETS[1:], start=1):
        rows = table[index * vec4s:(index + 1) * vec4s]
        assert rows[0, 0] > 0.0 and lives[index] == pytest.approx(rows[0, 0]), name
        assert 0.0 <= rows[2, 3] <= 1.0, name               # opacity


def test_the_presets_behave_as_described():
    p = shaders.PARTICLE_PRESETS
    assert p["SPARKS"][2][3] == 0.0                         # additive
    assert p["SMOKE"][2][3] == 1.0                          # alpha
    assert p["SPARKS"][1][0] < 0.0 < p["SMOKE"][1][0]       # falls / rises
    assert p["SPLASH"][4][0] == 1.0                         # line emitter
    assert p["SMOKE"][1][3] >= 4.0 * p["SMOKE"][1][2]       # grows 1 -> 4x
    assert p["MIST"][3][0] < p["SMOKE"][3][0]               # very low alpha
    assert p["MIST"][0][0] > p["SMOKE"][0][0] > p["STEAM"][0][0]


def test_the_shader_sources_agree_with_the_python_side():
    vert = shaders.DEFAULT_SHADERS["particle.vert"]
    assert f"PARTICLES_PER_EMITTER = {EMITTER_MAX_PARTICLES};" in vert
    assert f"PARTICLE_PRESET_COUNT = {len(EMITTER_PRESETS)};" in vert
    assert "gl_InstanceID % PARTICLES_PER_EMITTER" in vert
    assert "iParticleIndex" not in shaders.DEFAULT_SHADERS["effect.vert"]
