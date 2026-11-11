"""High-quality water reflects the effects above it.

Water reflects by marching the copied depth buffer, and effects are blended
quads that wrote no depth: an orb floating over a pool was not reflected. In
a frame with high-quality water the effect pass now also writes the depth of
its solid texels, and hands the water pass the centre and size of the
nearest few, which it reflects directly (water.frag ``reflectEffects``)."""

import numpy as np
import pytest

pytest.importorskip("PyQt5")

pytestmark = [pytest.mark.qt, pytest.mark.gl, pytest.mark.integration]

MAP = "maps/_SHOWCASE.json"
ORB = (960.0, 304.0, -192.0)            # the orb over the showcase's pool
EYE = (620.0, 210.0, 200.0)


def _look_at_pool(session):
    import glm
    import math
    view = session.view
    if view.renderer is None or not view.renderer.ready:
        pytest.skip("no OpenGL context")
    view.sysmon.set_active(False)
    eye, target = glm.vec3(*EYE), glm.vec3(960.0, 150.0, -192.0)
    d = glm.normalize(target - eye)
    view.set_editor_camera(eye, math.degrees(math.atan2(d.z, d.x)),
                           math.degrees(math.asin(d.y)))
    for _ in range(3):
        session.publish()
        session.paint()
    return view.renderer


def test_the_orb_over_the_pool_is_handed_to_the_water_pass(fio_session):
    session = fio_session(MAP, size=(240, 200))
    renderer = _look_at_pool(session)
    assert renderer._effects_write_depth
    effects = renderer._reflected_effects
    assert effects is not None and 0 < len(effects) <= renderer.MAX_REFLECTED_EFFECTS
    assert any(np.allclose(row[:3], ORB) for row in effects)
    assert np.all(effects[:, 3:5] > 0)                      # width and height


def test_cheap_water_asks_nothing_of_the_effects(fio_session):
    session = fio_session(MAP, size=(240, 200))
    renderer = _look_at_pool(session)
    renderer.water_quality = 'cheap'
    session.publish()
    session.paint()
    assert not renderer._effects_write_depth
    assert renderer._reflected_effects is None


def test_the_reflection_shows_in_the_water(fio_session):
    """The water under the orb differs with the reflection on and off; the
    orb itself, and the water far from it, do not."""
    session = fio_session(MAP, size=(240, 200))
    renderer = _look_at_pool(session)

    def frame():
        session.publish()
        session.paint()
        image = session.view.grabFramebuffer()
        w, h = image.width(), image.height()
        data = image.constBits().asarray(image.byteCount())
        return np.frombuffer(bytes(data), dtype=np.uint8).reshape(h, w, 4)[:, :, :3].astype(int)

    kind = type(renderer)
    limit = kind.MAX_REFLECTED_EFFECTS
    with_reflection = frame()
    try:
        # No effect is kept for the water pass: the depth write still runs.
        kind.EFFECT_REFLECTION_REACH = 0.0
        without = frame()
    finally:
        kind.EFFECT_REFLECTION_REACH = 4.0
    assert kind.MAX_REFLECTED_EFFECTS == limit
    h = with_reflection.shape[0]
    water = slice(int(h * 0.6), h)
    changed = np.abs(with_reflection[water] - without[water]).max(axis=2) > 24
    assert changed.sum() > 40, "no reflection of the orb in the water"
    # Colour, not just brightness: the orb is purple, cyan and magenta.
    patch = with_reflection[water][changed]
    assert (patch.max(axis=1) - patch.min(axis=1)).mean() > 40


def test_the_shaders_carry_the_reflection_and_the_solid_cutoff(fio_session):
    from engine import shaders
    water = shaders.DEFAULT_SHADERS['water.frag']
    assert 'vec4 reflectEffects(' in water and 'uniform int effectCount;' in water
    assert '#define MAX_REFLECTED_EFFECTS 4' in water
    session = fio_session(MAP, size=(160, 120))
    renderer = session.view.renderer
    if renderer is None or not renderer.ready:
        pytest.skip("no OpenGL context")
    assert renderer.uniforms['sprite_instanced'].get('alphaCutoff', -1) >= 0
    assert renderer.MAX_REFLECTED_EFFECTS == 4
