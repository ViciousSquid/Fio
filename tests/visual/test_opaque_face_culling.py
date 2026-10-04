"""Back-face culling in the main view's opaque brush passes.

An opaque brush is a closed solid, so from outside it the faces turned away
from the camera are always behind the ones turned towards it. Culling them
changes no pixel, apart from a few depth ties along silhouette edges, which
it settles the right way (see below). What it does change is how many fragments reach the
depth test: on the 24 000-brush stress map, 50-60% of the faces submitted
faced away. Depth-passing fragments per covered pixel fell from 1.34-2.06 to
1.19-1.44, and the opaque passes got 20-45% cheaper on Mesa in the
overhead and editor views.

These check the "no pixel" half on real geometry, boxes and clipped convex
brushes alike (the two wind oppositely), and that the saving is real. They
also check that nothing else is culled: transparent passes, whose far faces
show through the near ones, and the wireframe and vertex modes, whose far
edges are part of the view.
"""

import numpy as np
import pytest

from tests.helpers import gl as glh
from tests.helpers.worlds import box_brush

pytestmark = [pytest.mark.gl, pytest.mark.slow]

SIZE = 160
#: Pixels a view may change through depth ties on shared silhouette edges.
EDGE_TIE_PIXELS = 8


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


def _scene():
    """Overlapping boxes and clipped (convex) brushes, stacked in depth."""
    from engine import brush_geometry as bg

    brushes = [box_brush("floor", (0, -16, 0), (1600, 32, 1600))]
    for i in range(5):
        brushes.append(box_brush("wall%d" % i, (-300 + 150 * i, 96, -200 - 120 * i),
                                 (120, 192, 40 + 30 * i)))
        brushes.append(box_brush("crate%d" % i, (200 - 90 * i, 32, 60 * i - 100),
                                 (64, 64, 64)))
    for i in range(3):
        ramp = box_brush("ramp%d" % i, (-150 + 220 * i, 48, 150), (160, 96, 160))
        bg.clip_brush(ramp, (0.0, 1.0, 1.0 - i * 0.5), 10.0 * i)
        brushes.append(ramp)
    # Lit, so that a face turned towards the light and one turned away shade
    # differently: without a light every face is the same flat colour, and a
    # wrong winding (drawing the far faces instead) would be invisible.
    from editor.things import Light
    from tests.helpers.worlds import make_thing
    things = [make_thing(Light, "key", (250, 420, 380), color=[255, 240, 220],
                         intensity=2.5, radius=2200.0, state="on",
                         casts_shadows=False)]
    return brushes, things


CAMERAS = [((0, 900, 900), (0, 0, 0)), ((700, 120, 500), (0, 50, 0)),
           ((-600, 400, -900), (0, 0, 0)), ((20, 1500, 30), (0, 0, 0))]


def _draw(renderer, context, brushes, things, eye, target, cull, **config):
    import OpenGL.GL as gl

    projection, view, eye_vec = glh.camera_matrices(aspect=1.0, eye=eye, target=target)
    cfg = glh.render_config(all_brushes=brushes, all_things=things, **config)
    renderer.cull_opaque_back_faces = cull
    context.bind()
    gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    query = gl.glGenQueries(1)[0]
    gl.glBeginQuery(gl.GL_SAMPLES_PASSED, query)
    renderer.render_scene(projection, view, eye_vec, None, cfg,
                          brush_slots=cfg["all_brush_slots"])
    gl.glEndQuery(gl.GL_SAMPLES_PASSED)
    gl.glFinish()
    samples = gl.glGetQueryObjectuiv(query, gl.GL_QUERY_RESULT)
    gl.glDeleteQueries(1, [query])
    return context.read_pixels().copy(), int(samples)


@pytest.mark.parametrize("mode", ["lit", "unlit"])
def test_culling_changes_no_pixel(renderer, context, mode):
    from engine.constants import RENDER_MODE_LIT, RENDER_MODE_UNLIT

    brushes, things = _scene()
    render_mode = RENDER_MODE_LIT if mode == "lit" else RENDER_MODE_UNLIT
    for eye, target in CAMERAS:
        off, _ = _draw(renderer, context, brushes, things, eye, target, False,
                       render_mode=render_mode)
        on, _ = _draw(renderer, context, brushes, things, eye, target, True,
                      render_mode=render_mode)
        assert not glh.is_blank(off)
        differ = np.abs(off.astype(int) - on.astype(int)).max(axis=-1) > 0
        # A few silhouette pixels may change, for the better: where a face
        # turned away shares an edge with one turned towards the camera, the
        # two have the same depth there, and without culling whichever was
        # drawn first won the tie (the top face is drawn last, so a dark far
        # side could show along a floor's rim). A wrong winding changes
        # hundreds to thousands of pixels (832 for the convex meshes, 16 238
        # for the boxes, at this size).
        assert differ.sum() <= EDGE_TIE_PIXELS, (
            "culling away-facing faces changed %d pixels from %r; a face that "
            "should be visible was culled (a winding is the wrong way round)"
            % (int(differ.sum()), eye))


def test_culling_reduces_the_fragments_that_pass_the_depth_test(renderer, context):
    brushes, things = _scene()
    eye, target = CAMERAS[0]
    _, off = _draw(renderer, context, brushes, things, eye, target, False)
    _, on = _draw(renderer, context, brushes, things, eye, target, True)
    assert on < off, (
        "culling did not reduce the depth-passing fragments (%d with, %d "
        "without)" % (on, off))


def test_only_the_opaque_passes_cull(renderer, context, monkeypatch):
    """Transparent brushes show their far faces; they must never be culled,
    and culling must be off again once the frame is drawn."""
    import OpenGL.GL as gl

    brushes, things = _scene()
    brushes.append(box_brush("glassy", (0, 60, 300), (200, 120, 200),
                             transparent=True, opacity=0.5))
    seen = []
    draw = renderer.draw_lit_brushes_optimized

    def spy(*args, **kwargs):
        if kwargs.get("is_transparent_pass"):
            seen.append(renderer._culling_interiors())
        return draw(*args, **kwargs)

    monkeypatch.setattr(renderer, "draw_lit_brushes_optimized", spy)
    eye, target = CAMERAS[1]
    _draw(renderer, context, brushes, things, eye, target, True)
    assert seen and not any(seen), "a transparent pass was drawn with culling on"
    assert not gl.glIsEnabled(gl.GL_CULL_FACE), "culling was left on after the frame"
    assert renderer._opaque_cull_pass is False


@pytest.mark.parametrize("mode", ["wireframe", "vertex"])
def test_wireframe_and_vertex_modes_keep_every_face(renderer, context, mode):
    from engine.constants import RENDER_MODE_VERTEX, RENDER_MODE_WIREFRAME

    render_mode = RENDER_MODE_WIREFRAME if mode == "wireframe" else RENDER_MODE_VERTEX
    brushes, things = _scene()
    eye, target = CAMERAS[0]
    off, _ = _draw(renderer, context, brushes, things, eye, target, False,
                   render_mode=render_mode)
    on, _ = _draw(renderer, context, brushes, things, eye, target, True,
                  render_mode=render_mode)
    assert np.array_equal(off, on), "the far edges of a %s view were culled" % mode
