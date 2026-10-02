"""Core ``Prop`` entity contract."""
import pytest

pytest.importorskip('PyQt5')
from editor.things import Thing, Prop


def test_prop_has_serializable_carry_and_physics_defaults():
    prop = Prop(pos=[1, 2, 3])
    assert prop.properties['type'] == 'prop'
    assert prop.properties['pickup_enabled'] is True
    assert prop.properties['physics_enabled'] is False
    assert prop.properties['no_collision'] is True
    assert prop.properties['carry_offset'] == [0.0, -6.0, 0.0]
    assert prop.properties['drop_angular_velocity'] == [0.0, 0.0, 0.0]

    restored = Thing.from_dict(prop.to_dict())
    assert isinstance(restored, Prop)
    assert restored.pos == [1, 2, 3]
    assert restored.properties['mass'] == 1.0


def test_prop_can_be_a_billboard_without_a_model():
    prop = Prop(properties={'sprite_path': 'assets/sprites/health.png',
                            'sprite_size': [48, 64]})
    assert prop.properties['model_path'] == ''
    assert prop.get_sprite_path() == 'assets/sprites/health.png'
