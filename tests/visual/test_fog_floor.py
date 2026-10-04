"""Standing inside a fog volume, the floor fogs like the walls and ceiling.

The fog pass ray-marches from the camera to where the ray leaves the volume,
drawn on the face it leaves through. It drew five faces of a box volume: the
bottom was left out (a pattern copied from the water pass, where the floor
hides it). From inside, the floor is seen through that bottom face, so it
showed through stark and unfogged. The bottom face also usually lies exactly
on the floor, so it is drawn pulled a hair towards the camera, or it would
z-fight it into stripes.
"""

import numpy as np
import pytest

from tests.helpers import gl as glh
from tests.helpers.worlds import box_brush

pytestmark = [pytest.mark.gl, pytest.mark.slow]

SIZE = 160


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


def _room(with_fog):
    brushes = [box_brush("floor", (0, -16, 0), (1024, 32, 1024)),
               box_brush("ceiling", (0, 272, 0), (1024, 32, 1024)),
               box_brush("north", (0, 128, -528), (1024, 256, 32)),
               box_brush("south", (0, 128, 528), (1024, 256, 32)),
               box_brush("west", (-528, 128, 0), (32, 256, 1024)),
               box_brush("east", (528, 128, 0), (32, 256, 1024))]
    if with_fog:
        # Fills the room exactly: its bottom lies on the floor's top.
        brushes.append(box_brush("fog", (0, 128, 0), (1024, 256, 1024), is_fog=True,
                                 fog_color=[120, 140, 170], fog_density=3.0,
                                 fog_noise_scale=0.01))
    return brushes


def _draw(renderer, context, brushes, eye, target):
    import OpenGL.GL as gl
    from engine.constants import RENDER_MODE_LIT

    projection, view, eye_vec = glh.camera_matrices(aspect=1.0, eye=eye, target=target)
    cfg = glh.render_config(all_brushes=brushes, all_things=[], render_mode=RENDER_MODE_LIT)
    context.bind()
    gl.glClearColor(0.1, 0.1, 0.1, 1.0)
    renderer.render_scene(projection, view, eye_vec, None, cfg,
                          brush_slots=cfg["all_brush_slots"])
    gl.glFinish()
    return context.read_pixels()[:, :, :3].astype(int)


@pytest.mark.parametrize("target", [(0, 40, -300), (0, 0, 0), (200, 60, -100)])
def test_every_surface_seen_from_inside_the_fog_is_fogged(renderer, context, target):
    eye = (0, 120, 300)
    clear = _draw(renderer, context, _room(False), eye, target)
    fogged = _draw(renderer, context, _room(True), eye, target)
    untouched = (np.abs(fogged - clear).max(axis=-1) == 0).mean()
    assert untouched < 0.01, (
        "%.0f%% of the view is exactly as it is without the fog; from inside a "
        "fog volume every surface should be fogged (the floor was left out)"
        % (100 * untouched))
