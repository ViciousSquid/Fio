"""A rotating model Prop draws on the player as it does on the desktop.

The player builds each model's matrix itself (glmath.item_matrix) rather than
reading the entity table's; the rotation must land in the same place in it:
``T @ R(rotation) @ R_rotate(axis, angle) @ S``. No GPU: the shadow pass runs
against a recording stand-in for GL.
"""
from pathlib import Path

import numpy as np

from engine.model_rotate import rotate_matrix
from engine.prop_entity import Prop
from player.render import glmath, uniforms
from player.render.shadows import ShadowAtlas

PLACED = {'model_path': 'models/reel.glb', 'rotation': (20.0, 35.0, -15.0),
          'scale': (2.0, 0.5, 3.0)}


def _prop(angle=0.0, axis=2, pos=(10.0, 4.0, -6.0), **properties):
    prop = Prop(list(pos), dict(PLACED, **properties))
    prop._rotate_angle = angle
    prop._rotate_axis = axis
    return prop


def _placed_rotation(rotation):
    pitch, yaw, roll = (np.radians(v) for v in rotation)
    rx = np.array(((1, 0, 0), (0, np.cos(pitch), -np.sin(pitch)),
                   (0, np.sin(pitch), np.cos(pitch))))
    ry = np.array(((np.cos(yaw), 0, np.sin(yaw)), (0, 1, 0),
                   (-np.sin(yaw), 0, np.cos(yaw))))
    rz = np.array(((np.cos(roll), -np.sin(roll), 0),
                   (np.sin(roll), np.cos(roll), 0), (0, 0, 1)))
    return ry @ rx @ rz


def test_rotate_turns_the_model_in_its_own_frame_after_scale():
    prop = _prop(angle=90.0, axis=2)
    model = glmath.item_matrix(prop)
    expected = (_placed_rotation(PLACED['rotation']) @ rotate_matrix(2, 90.0)
                @ np.diag(PLACED['scale']))
    np.testing.assert_allclose(model[:3, :3], expected, atol=1e-5)
    np.testing.assert_allclose(model[:3, 3], prop.pos, atol=1e-5)
    np.testing.assert_array_equal(model[3], (0, 0, 0, 1))
    assert model.dtype == np.float32


def test_each_axis_matches_the_shared_rotate_matrix():
    rotation = _placed_rotation(PLACED['rotation'])
    for axis in (0, 1, 2):
        model = glmath.item_matrix(_prop(angle=-37.5, axis=axis))
        np.testing.assert_allclose(
            model[:3, :3],
            rotation @ rotate_matrix(axis, -37.5) @ np.diag(PLACED['scale']),
            atol=1e-5)


def test_player_matrix_equals_the_desktop_entity_tables():
    from engine.entity_table import _model_transform_columns
    for angle, axis in ((90.0, 2), (33.0, 0), (-120.0, 1), (0.0, 2)):
        prop = _prop(angle=angle, axis=axis)
        desktop, _ = _model_transform_columns(prop)
        desktop = desktop.reshape(4, 4).T  # column-major, no translation
        player = glmath.item_matrix(prop)
        np.testing.assert_allclose(player[:3, :3], desktop[:3, :3], atol=1e-5)


def test_no_rotation_is_exactly_the_placed_matrix():
    for prop in (_prop(angle=0.0), _prop(angle=0.0, axis=0), Prop([1, 2, 3], dict(PLACED))):
        old = glmath.entity_matrix(prop.pos, prop.properties)
        np.testing.assert_array_equal(glmath.item_matrix(prop), old)
    # Something without the runtime attributes at all (not a Prop).
    class Item:
        pos = (1.0, 2.0, 3.0)
        properties = dict(PLACED)
    np.testing.assert_array_equal(
        glmath.item_matrix(Item()), glmath.entity_matrix(Item.pos, Item.properties))
    assert glmath.rotation_of(Item()) == (2, 0.0)


class _RecordingGL:
    """Just enough GL for ShadowAtlas.prepare: every call returns 0 and the
    'model' uniform uploads are kept."""

    def __init__(self):
        self.models = []

    def glGetUniformLocation(self, program, name):
        return name

    def glUniformMatrix4fv(self, location, count, transpose, data):
        if location == 'model':
            self.models.append(np.asarray(data).T.copy())

    def __getattr__(self, name):
        if name.startswith('GL_'):
            return 0
        return lambda *args, **kwargs: 0


def test_shadows_cast_from_the_spun_matrix():
    prop = _prop(angle=90.0, axis=2)
    atlas = ShadowAtlas.__new__(ShadowAtlas)
    atlas.gl = _RecordingGL()
    atlas.program = 7_193_001  # a program id no other test caches locations for
    atlas.texture = atlas.fbo = atlas.static_texture = atlas.static_fbo = 1
    atlas.TILE = 16
    atlas.eye = (0, 0, 0)
    atlas.invalidate()
    lights = [dict(pos=(0.0, 0.0, 0.0), radius=500.0, casts_shadows=True)]
    vao = 42
    uniforms.clear()
    try:
        atlas.prepare(lights, [(vao, 0, 0, 36)], {vao: ('model', prop, (0, 0, 0))},
                      64, 64)
    finally:
        uniforms.clear()
    assert atlas.gl.models, 'the shadow pass drew no model'
    expected = glmath.item_matrix(prop)
    unspun = glmath.entity_matrix(prop.pos, prop.properties)
    assert not np.allclose(expected, unspun)
    for model in atlas.gl.models:
        np.testing.assert_allclose(model, expected, atol=1e-6)


def test_every_model_matrix_in_the_renderer_carries_the_rotation():
    """The colour pass, its culling and the shadow casters build a model's
    matrix through item_matrix; a bare entity_matrix(item.pos,
    item.properties) would draw the model unspun."""
    render = Path(glmath.__file__).resolve().parent
    for path in sorted(render.glob('*.py')):
        if path.name == 'glmath.py':
            continue
        assert 'entity_matrix(' not in path.read_text(), path.name
    for name in ('renderer.py', 'shadows.py'):
        assert 'glmath.item_matrix(item)' in (render / name).read_text(), name
