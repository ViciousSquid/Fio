"""Visual tier: the Ball on the real renderer.

Its tint colours its textured model and nothing else; it casts a shadow from a
shadow-casting light, that shadow follows it when it moves, and it is shaded
by what stands between it and the light.

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


def _render(renderer, context, brushes, things, camera=None):
    import OpenGL.GL as gl

    projection, view, eye = camera or glh.camera_matrices(aspect=1.0)
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


# -- shadows -----------------------------------------------------------------

#: Looking along -Z at the ball from the side and a little above.
SIDE_EYE, SIDE_TARGET = (0.0, 140.0, 360.0), (0.0, 30.0, 0.0)


def _side_camera():
    return glh.camera_matrices(aspect=1.0, eye=SIDE_EYE, target=SIDE_TARGET)


def _pixel(point, camera):
    """Image (row, column) of world *point*, top row first."""
    import glm
    projection, view, _ = camera
    clip = projection * view * glm.vec4(*point, 1.0)
    x, y = clip.x / clip.w, clip.y / clip.w
    return int((1.0 - (y + 1.0) / 2.0) * SIZE), int((x + 1.0) / 2.0 * SIZE)


def _patch(image, point, camera, half=4):
    row, col = _pixel(point, camera)
    return glh.luminance(image[row - half:row + half + 1,
                               col - half:col + half + 1]).mean()


def _shadow_scene(light_pos, ball_x=0.0, occluder=False, model=None, shadows=True):
    from editor.things import Light
    from engine.prop_entity import Prop
    from tests.helpers.worlds import box_brush, make_thing

    brushes = [box_brush("floor", (0, -16, 0), (1024, 32, 1024))]
    if occluder:
        brushes.append(box_brush("roof", (0, 170, 0), (160, 10, 160)))
    light = make_thing(Light, "lamp", light_pos, color=[255, 255, 255],
                       intensity=2.0, radius=1400.0, state="on",
                       casts_shadows=shadows)
    properties = {"model_path": model} if model else {}
    ball = Prop.for_ball(pos=[ball_x, 25.0, 0.0], properties=properties)
    return brushes, [light, ball]


@pytest.mark.parametrize("model", [None, "assets/models/ball_numbers.obj"],
                         ids=["golf", "numbered"])
def test_the_ball_casts_a_shadow(renderer, context, model):
    camera = _side_camera()
    # Light up and to the left: the shadow falls on the floor to the right.
    light = (-150.0, 150.0, 0.0)
    lit = _render(renderer, context, *_shadow_scene(light, model=model, shadows=False),
                  camera=camera)
    shaded = _render(renderer, context, *_shadow_scene(light, model=model), camera=camera)
    spot = (70.0, 0.0, 0.0)
    assert _patch(shaded, spot, camera) < 0.6 * _patch(lit, spot, camera)


def test_the_balls_shadow_follows_it(renderer, context):
    camera = _side_camera()
    light = (0.0, 200.0, 0.0)
    here = _render(renderer, context, *_shadow_scene(light, ball_x=-120.0), camera=camera)
    moved = _render(renderer, context, *_shadow_scene(light, ball_x=120.0), camera=camera)
    # Under the light, the shadow lies beyond the ball on the floor.
    left, right = (-170.0, 0.0, 0.0), (170.0, 0.0, 0.0)
    assert _patch(here, left, camera) < _patch(moved, left, camera)
    assert _patch(moved, right, camera) < _patch(here, right, camera)


def test_the_ball_receives_shadows(renderer, context):
    camera = _side_camera()
    # A slab between the light overhead and the ball.
    light = (0.0, 300.0, 0.0)
    lit = _render(renderer, context, *_shadow_scene(light, occluder=True, shadows=False),
                  camera=camera)
    shaded = _render(renderer, context, *_shadow_scene(light, occluder=True), camera=camera)
    top_of_ball = (0.0, 45.0, 12.0)
    assert _patch(shaded, top_of_ball, camera) < 0.6 * _patch(lit, top_of_ball, camera)
