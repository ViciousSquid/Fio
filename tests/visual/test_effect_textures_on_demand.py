"""FIRE and ORB animations are loaded when a map holds an effect that uses
them, one variant at a time -- never at start-up (the ten authored GIFs are
~150 MB of textures and half a second of decoding)."""

import glm
import pytest

from tests.helpers import gl as glh

pytest.importorskip("PyQt5", reason="Effect is an editor Thing")

pytestmark = [pytest.mark.gl]

SIZE = 64


@pytest.fixture
def renderer():
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as ctx:
        made = glh.make_renderer()
        made.shadows_enabled = False
        yield ctx, made
        made.cleanup()
    glh.reset_texture_cache()


def _table(*effects):
    from engine.entity_table import EntityTable
    table = EntityTable()
    table.begin_frame(list(effects), epoch=1, effect_runtime=False)
    return table


def _effect(kind, texture_index, pos=(0.0, 40.0, -200.0)):
    from editor.things import Effect
    from engine.effect_entity import EFFECT_FIRE_TEXTURES, EFFECT_ORB_TEXTURES
    textures = EFFECT_FIRE_TEXTURES if kind == "FIRE" else EFFECT_ORB_TEXTURES
    key = "fire_texture" if kind == "FIRE" else "orb_texture"
    effect = Effect(properties={"effect_type": kind, key: textures[texture_index]})
    effect.pos = list(pos)
    return effect


def test_nothing_is_decoded_at_start_up(renderer):
    _, made = renderer
    assert made.effect_fire_frames == {}
    assert made.effect_orb_frames == {}


def test_a_map_loads_only_the_variants_its_effects_use(renderer):
    _, made = renderer
    table = _table(_effect("FIRE", 2), _effect("ORB", 0))
    made.ensure_effect_frames(table)
    assert list(made.effect_fire_frames) == [2]
    assert list(made.effect_orb_frames) == [0]
    assert len(made.effect_fire_frames[2]) > 1          # an animation, uploaded
    assert len(made.effect_orb_cumulative[0]) == len(made.effect_orb_frames[0])
    made.ensure_effect_frames(table)                    # once each
    assert list(made.effect_fire_frames) == [2]


def test_a_map_without_effects_loads_none(renderer):
    _, made = renderer
    made.ensure_effect_frames(_table())
    assert made.effect_fire_frames == {} and made.effect_orb_frames == {}


def test_drawing_a_scene_loads_its_effects_and_draws_them(renderer):
    ctx, made = renderer
    config = glh.render_config(all_things=[_effect("FIRE", 4)], shadows_enabled=False)
    projection = glm.perspective(glm.radians(70.0), 1.0, 1.0, 5000.0)
    view = glm.lookAt(glm.vec3(0.0, 40.0, 0.0), glm.vec3(0.0, 40.0, -200.0),
                      glm.vec3(0.0, 1.0, 0.0))
    ctx.bind()
    made.render_scene(projection, view, glm.vec3(0.0, 40.0, 0.0), None, config,
                      brush_slots=config["all_brush_slots"])
    assert list(made.effect_fire_frames) == [4]
    assert made.effect_orb_frames == {}


def test_the_draw_loads_a_variant_it_was_not_told_about(renderer):
    _, made = renderer
    frames, cumulative = made._effect_frames(0, 1)
    assert len(frames) > 1 and len(cumulative) == len(frames)
    assert list(made.effect_fire_frames) == [1]
