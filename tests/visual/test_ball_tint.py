"""Visual tier: a Ball's tint colours its textured model, and nothing else.

Marked ``gl``; skips where no OpenGL context can be created.
"""

import os

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]

SIZE = 192


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as ctx:
        yield ctx
    glh.reset_texture_cache()


@pytest.fixture
def renderer(context):
    made = glh.make_renderer()
    yield made
    try:
        made.cleanup()
    except Exception:
        pass


def _render(renderer, context, brushes, things):
    import OpenGL.GL as gl

    projection, view, eye = glh.camera_matrices(aspect=1.0)
    config = glh.render_config(all_brushes=brushes, all_things=things)
    context.bind()
    gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    renderer.render_scene(projection, view, eye, None, config,
                          brush_slots=config["all_brush_slots"])
    gl.glFinish()
    image = context.read_pixels()
    out = os.environ.get("FIO_BALL_TINT_DUMP")
    if out:
        from PIL import Image
        Image.fromarray(np.ascontiguousarray(image[..., :3])).save(
            os.path.join(out, "ball_%d.png" % len(os.listdir(out))))
    return image


def _scene(*tints, xs=None):
    from engine.prop_entity import Prop
    brushes, things = glh.lit_cube_scene(shadows=False)
    floor = [b for b in brushes if b.get("name") != "cube"]
    xs = xs or [0.0] * len(tints)
    balls = [Prop.for_ball(pos=[x, 25.0, 0.0], properties={
        "tint": tint, "scale": [3.0, 3.0, 3.0]}) for x, tint in zip(xs, tints)]
    return floor, things + balls


def _centre(image):
    return glh.region(image, 0.4, 0.4, 0.6, 0.6)[..., :3].reshape(-1, 3).mean(axis=0)


@pytest.mark.parametrize("count", [1, 2], ids=["single", "instanced"])
def test_a_tinted_ball_draws_in_its_tint(renderer, context, count):
    xs = [0.0] if count == 1 else [0.0, 400.0]
    white = _centre(_render(renderer, context, *_scene(*([[1.0, 1.0, 1.0]] * count), xs=xs)))
    red = _centre(_render(renderer, context, *_scene(*([[1.0, 0.1, 0.1]] * count), xs=xs)))

    assert white.max() > 20.0, "the ball did not draw"
    # Untinted, the ball is near white; tinted, red dominates.
    assert abs(white[0] - white[1]) < 0.25 * white[0]
    assert red[0] > 2.5 * red[1] and red[0] > 2.5 * red[2]


def test_a_tint_does_not_stay_on_for_the_brushes_drawn_next(renderer, context):
    floor, things = _scene([1.0, 1.0, 1.0])
    lights = [t for t in things if t.properties.get("type") != "prop"]
    before = _render(renderer, context, floor, lights)
    _render(renderer, context, *_scene([0.0, 0.0, 1.0]))
    after = _render(renderer, context, floor, lights)
    assert np.array_equal(before, after)
