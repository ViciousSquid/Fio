"""Core ``Prop`` entity contract."""
import pytest

pytest.importorskip('PyQt5')
from editor.things import Thing, Prop


def test_prop_has_serializable_carry_and_physics_defaults():
    prop = Prop(pos=[1, 2, 3])
    assert prop.properties['type'] == 'prop'
    assert prop.properties['pickup_enabled'] is True
    # A bare Prop has no model_path, so its representation follows the one
    # asset it does have. See test_a_bare_prop_is_drawable.
    assert prop.properties['render_mode'] == 'billboard'
    assert prop.properties['physics_enabled'] is False
    assert prop.properties['no_collision'] is True
    assert prop.properties['carry_offset'] == [0.0, -6.0, 0.0]
    assert prop.properties['drop_angular_velocity'] == [0.0, 0.0, 0.0]

    restored = Thing.from_dict(prop.to_dict())
    assert isinstance(restored, Prop)
    assert restored.pos == [1, 2, 3]
    assert restored.properties['mass'] == 1.0


def test_prop_can_be_a_billboard_without_a_model():
    prop = Prop(properties={
        'render_mode': 'billboard',
        'sprite_path': 'assets/sprites/health.png',
        'sprite_size': [48, 64],
    })
    assert prop.properties['render_mode'] == 'billboard'
    assert prop.properties['model_path'] == ''
    assert prop.get_sprite_path() == 'assets/sprites/health.png'


# ---------------------------------------------------------------------------
# render_mode is authoritative, and its default has to be one that can draw.
#
# PROP_DEFAULTS ships a sprite_path but no model_path. A fixed default of
# 'model' therefore produced a prop classified as a model with no model to
# draw: it reached neither the model list nor the sprite list and was rendered
# by nothing at all -- which is exactly what the editor's "add Prop" action
# created. The default follows the authored assets instead; a record that
# states a render_mode is never second-guessed.
# ---------------------------------------------------------------------------

def _drawn_as(prop):
    from engine.renderer_core import BaseRenderer
    renderer = BaseRenderer.__new__(BaseRenderer)
    models = []
    _, _, sprites, *_ = BaseRenderer._sort_objects(
        renderer, [], [prop], {'play_mode': True})
    BaseRenderer._sort_objects(
        renderer, [], [prop], {'play_mode': True}, model_out=models)
    return {'model' if models else None, 'sprite' if sprites else None} - {None}


def test_a_bare_prop_is_drawable():
    """What the editor's right-click "Prop" action produces."""
    prop = Prop(pos=[0, 0, 0])
    assert prop.properties['render_mode'] == 'billboard'
    assert _drawn_as(prop) == {'sprite'}, (
        "a freshly placed Prop is rendered by nothing")


def test_a_prop_with_a_model_defaults_to_model_mode():
    prop = Prop(pos=[0, 0, 0],
                properties={'model_path': 'plugins/tidy/assets/book.obj'})
    assert prop.properties['render_mode'] == 'model'
    assert _drawn_as(prop) == {'model'}


def test_an_authored_render_mode_is_never_second_guessed():
    explicit_model = Prop(pos=[0, 0, 0],
                          properties={'sprite_path': 'a.png',
                                      'render_mode': 'model'})
    assert explicit_model.properties['render_mode'] == 'model'

    explicit_billboard = Prop(pos=[0, 0, 0],
                              properties={'model_path': 'm.obj',
                                          'render_mode': 'billboard'})
    assert explicit_billboard.properties['render_mode'] == 'billboard'
    assert _drawn_as(explicit_billboard) == {'sprite'}


def test_the_implied_mode_survives_a_round_trip():
    prop = Prop(pos=[0, 0, 0])
    restored = Thing.from_dict(prop.to_dict())
    assert restored.properties['render_mode'] == 'billboard'
    assert _drawn_as(restored) == {'sprite'}
