"""The numeric path must draw exactly what the object path drew.

``Renderer_F.render_scene`` has two ways through the brush half of a frame:
the main camera pass consumes integer slots into the dense render projection,
and everything else (the portal virtual views, the split-screen second view,
the non-threaded editor) still walks brush dicts.

They are the same picture or the refactor changed what Fio renders.  So these
draw one scene both ways on a real GL context and compare the pixels -- which
is the only check that covers the whole chain at once: classification into
passes, the batched model and normal matrices, the face batching and its
texture ordering, the NATURAL/authored/FIT scale choice, and the colour and
overbright columns.
"""

import numpy as np
import pytest

from tests.helpers import gl as glh
from tests.helpers.worlds import box_brush, make_thing

pytestmark = [pytest.mark.gl, pytest.mark.slow]

SIZE = 192
#: Mesa's rasteriser is deterministic for identical geometry, but the two paths
#: compute their matrices differently -- NumPy float32 against PyGLM float32 --
#: so an edge pixel may land one level either side.  A handful of such pixels is
#: the refactor being numerically honest; a shape drawn differently is not, and
#: would move the mean far beyond this.
MAX_MEAN_DIFF = 0.75
MAX_OUTLIER_FRACTION = 0.01


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


def _projection_for(brushes):
    """The projection, refs and slots the logic thread would publish."""
    from engine.render_table import RenderTable

    table = RenderTable()
    table.sync(brushes, 1)
    refs = np.empty(len(brushes), dtype=object)
    refs[:] = brushes
    slots = np.arange(table.count, dtype=np.int32)
    return table, refs, slots


def _render(renderer, context, brushes, things, numeric, **overrides):
    import OpenGL.GL as gl

    projection, view, eye = glh.camera_matrices(aspect=1.0)
    config = glh.render_config(all_brushes=brushes, all_things=things,
                               **overrides)
    brush_slots = None
    if numeric:
        table, refs, slots = _projection_for(brushes)
        config["render_table"] = table
        config["render_refs"] = refs
        config["all_brush_slots"] = slots
        brush_slots = slots
    context.bind()
    gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    renderer.render_scene(projection, view, eye, brushes, things, None, config,
                          brush_slots=brush_slots)
    gl.glFinish()
    return context.read_pixels().astype(np.int16)


def _assert_same_picture(objects_img, slots_img, what):
    assert not glh.is_blank(objects_img), "%s: the object path drew nothing" % what
    assert not glh.is_blank(slots_img), "%s: the numeric path drew nothing" % what

    diff = np.abs(objects_img - slots_img)
    mean = float(diff.mean())
    outliers = float((diff > 8).mean())
    assert mean <= MAX_MEAN_DIFF, (
        "%s: mean pixel difference %.3f -- the two paths are not drawing the "
        "same scene" % (what, mean))
    assert outliers <= MAX_OUTLIER_FRACTION, (
        "%s: %.2f%% of samples differ by more than 8 levels" % (what, outliers * 100))


def _scene():
    """A scene touching every brush pass the numeric path took over."""
    from editor.things import Light

    brushes = [
        box_brush("floor", (0, -16, 0), (1024, 32, 1024)),
        box_brush("cube", (0, 64, 0), (128, 128, 128)),
        # A second textured box with per-face UV state, so the face batching
        # has to order more than one texture and honour scale/angle/shift.
        box_brush("wall", (220, 64, 0), (64, 256, 256),
                  uv_scale={"top": [2.0, 3.0]},
                  uv_angle={"north": 30.0},
                  uv_shift={"south": [0.25, 0.5]},
                  uv_natural={"east": True}),
        # No real texture: goes to the lit (solid) pass, tinted.
        box_brush("solid", (-220, 64, 0), (96, 96, 96),
                  textures={}, tint=[200, 40, 40]),
        # Overbright.
        box_brush("lamp", (0, 64, -260), (48, 48, 48),
                  shader="Glow", tint=[40, 120, 255], glow_intensity=3.0),
    ]
    light = make_thing(Light, "test_light", (0, 300, 300),
                       color=[255, 255, 255], intensity=2.0, radius=1400.0,
                       state="on", casts_shadows=False)
    return brushes, [light]


def test_the_two_paths_draw_the_same_scene(renderer, context):
    brushes, things = _scene()
    objects_img = _render(renderer, context, brushes, things, numeric=False)
    slots_img = _render(renderer, context, brushes, things, numeric=True)
    _assert_same_picture(objects_img, slots_img, "textured scene")


def test_the_two_paths_agree_with_a_rotated_brush(renderer, context):
    """The batched Rodrigues build against the glm one, through real pixels."""
    brushes, things = _scene()
    brushes[1]["_rot_angle"] = 35.0
    brushes[1]["rot_axis"] = [0.3, 1.0, 0.2]
    objects_img = _render(renderer, context, brushes, things, numeric=False)
    slots_img = _render(renderer, context, brushes, things, numeric=True)
    _assert_same_picture(objects_img, slots_img, "rotated brush")


def test_the_two_paths_agree_in_solid_lit_mode(renderer, context):
    brushes, things = _scene()
    objects_img = _render(renderer, context, brushes, things, numeric=False,
                          brush_display_mode="Solid")
    slots_img = _render(renderer, context, brushes, things, numeric=True,
                        brush_display_mode="Solid")
    _assert_same_picture(objects_img, slots_img, "solid display mode")


def test_the_two_paths_agree_with_a_shadow_casting_light(renderer, context):
    """Covers the caster selection and the per-light reach test as well."""
    from editor.things import Light

    brushes, _ = _scene()
    light = make_thing(Light, "shadow_light", (0, 320, 240),
                       color=[255, 255, 255], intensity=2.0, radius=1400.0,
                       state="on", casts_shadows=True)
    things = [light]
    objects_img = _render(renderer, context, brushes, things, numeric=False)
    slots_img = _render(renderer, context, brushes, things, numeric=True)
    _assert_same_picture(objects_img, slots_img, "shadowed scene")


def test_a_hidden_brush_is_absent_from_both(renderer, context):
    brushes, things = _scene()
    lit = _render(renderer, context, brushes, things, numeric=True)
    brushes[1]["hidden"] = True
    # The projection is rebuilt per render here, but `hidden` is read live in
    # the engine and the slots come from the logic thread; what this checks is
    # that the numeric draw path honours the visible set it is handed.
    table, refs, slots = _projection_for(brushes)
    kept = slots[np.array([not b.get("hidden") for b in brushes])]
    import OpenGL.GL as gl
    projection, view, eye = glh.camera_matrices(aspect=1.0)
    config = glh.render_config(all_brushes=brushes, all_things=things,
                               render_table=table, render_refs=refs,
                               all_brush_slots=kept)
    context.bind()
    gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    renderer.render_scene(projection, view, eye, brushes, things, None, config,
                          brush_slots=kept)
    gl.glFinish()
    hidden_img = context.read_pixels().astype(np.int16)
    assert float(np.abs(lit - hidden_img).mean()) > MAX_MEAN_DIFF, (
        "hiding a brush changed nothing, so the slots are not what is drawn")
