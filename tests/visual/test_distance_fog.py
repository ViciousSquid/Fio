"""Visual tier: far-plane fog and global ambient, on a real OpenGL context.

Marked ``gl``, so these skip with a readable reason where no context can be
created (see ``tests/README.md``). Under CI they run against Mesa's software
rasteriser via ``xvfb-run``.

What is under test is the claim the feature rests on: the view distance can be
pulled in *without* geometry popping out of existence at the clip, because the
fog has already reached the background colour by the time the far plane
discards anything. That is a statement about pixels, so it is checked in
pixels — the arithmetic behind it is covered headlessly in
``tests/renderer/test_view_distance.py``.

Nothing here depends on a particular GPU's shading. Every assertion compares
one render of the scene against another render of the same scene, or against
the fog colour the test itself chose.
"""

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]

SIZE = 192

#: A saturated fog colour nothing in the scene could produce by accident, so a
#: pixel matching it is unambiguously fog (or the background behind it). The
#: scene is lit white and textured white, so "redness" separates the two
#: cleanly whatever the driver's shading precision.
FOG_RGB = (0.85, 0.05, 0.05)

#: A camera raked down over a ground plane. The angle matters: a near-level
#: camera compresses thousands of world units into a couple of pixel rows, and
#: no fog band is measurable in two rows. Looking down ~19 degrees spreads the
#: distance range across half the frame.
EYE = (0.0, 1200.0, 2000.0)
TARGET = (0.0, 0.0, -1500.0)


def ground_scene():
    """One ground slab far larger than any view distance these tests set.

    A single slab, not a field of separate brushes: it is clipped *through* by
    the far plane rather than dropped whole by the broad-phase cull, which is
    the case the fog exists to cover.
    """
    from editor.things import Light
    from tests.helpers.worlds import box_brush, make_thing

    brushes = [box_brush("ground", (0, -32, -3000), (16000, 64, 16000))]
    light = make_thing(
        Light, "sun", (0.0, 2500.0, 1500.0),
        color=[255, 255, 255], intensity=0.9, radius=20000.0, state="on",
        casts_shadows=False,
    )
    return brushes, [light]


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


def settings(distance, **kwargs):
    """A :class:`ViewDistance` at *distance* with the tests' fog colour."""
    from engine.view_distance import ViewDistance

    vd = ViewDistance(distance)
    vd.fog_color = FOG_RGB
    for key, value in kwargs.items():
        setattr(vd, key, value)
    return vd


def render(renderer, context, brushes, things, view_distance,
           clear=FOG_RGB, **config_overrides):
    """Draw one frame, with the projection's far plane taken from *view_distance*.

    Taking the far plane from the same object the shaders fog against is the
    pairing under test, so the test must not let the two drift apart — this
    mirrors ``QtGameView.paintGL``, which also clears to the fog colour (the
    renderer does not own the clear colour, so that is reproduced here).
    """
    import glm
    import OpenGL.GL as gl

    eye = glm.vec3(*EYE)
    projection = glm.perspective(glm.radians(70.0), 1.0, 1.0,
                                 view_distance.far_plane)
    view = glm.lookAt(eye, glm.vec3(*TARGET), glm.vec3(0, 1, 0))

    renderer.view_distance = view_distance
    config = glh.render_config(all_brushes=brushes, all_things=things,
                               **config_overrides)
    context.bind()
    gl.glClearColor(clear[0], clear[1], clear[2], 1.0)
    renderer.render_scene(projection, view, eye, brushes, things, None, config,
                          brush_slots=config["all_brush_slots"])
    gl.glFinish()
    return context.read_pixels()


# -- measurement -----------------------------------------------------------

def band(image, y0, y1, x0=0.25, x1=0.75):
    """A horizontal slice of the frame, addressed in fractions of its size."""
    return glh.region(image, x0, y0, x1, y1).astype(float)


def redness(pixels):
    """How far a region leans toward the fog colour, in 0..255.

    Zero for the neutral grey the scene is lit in, ~210 for the pure fog
    colour. Luminance cannot serve here: red at this value happens to sit in
    the same luminance range as the lit ground, so the two would be
    indistinguishable.
    """
    return float(pixels[..., 0].mean() - pixels[..., 1:].mean())


def redness_profile(image):
    """Redness down the centre of the frame, one value per pixel row."""
    strip = image[:, SIZE // 2 - 16:SIZE // 2 + 16, :].astype(float).mean(axis=1)
    return strip[:, 0] - (strip[:, 1] + strip[:, 2]) / 2.0


def sharpest_edge(image):
    """``(row, size)`` of the largest single-row step in redness.

    On a frame whose geometry runs past the far plane this finds the clip
    boundary, because nothing else in the scene changes that fast.
    """
    steps = np.abs(np.diff(redness_profile(image)))
    row = int(steps.argmax())
    return row, float(steps[row])


# ---------------------------------------------------------------------------
# The pipeline still runs
# ---------------------------------------------------------------------------

def test_the_fogged_pipeline_compiles_and_draws(renderer, context):
    """Every shader carrying the fog block must compile and submit cleanly."""
    brushes, things = ground_scene()
    with glh.no_gl_errors("fogged render_scene"):
        image = render(renderer, context, brushes, things, settings(4096.0))
    assert image.shape == (SIZE, SIZE, 3)
    assert image.std() > 0.0, "the frame is a flat colour — nothing was drawn"


def test_play_mode_with_dynamic_light_draws_cleanly(renderer, context):
    """A live Light must not break the play-mode lighting pipeline."""
    brushes, things = ground_scene()
    import glm
    import OpenGL.GL as gl

    eye = glm.vec3(*EYE)
    from engine.view_distance import ViewDistance
    vd = ViewDistance(4096.0)
    renderer.view_distance = vd
    projection = glm.perspective(glm.radians(70.0), 1.0, 1.0, vd.far_plane)
    view = glm.lookAt(eye, glm.vec3(*TARGET), glm.vec3(0, 1, 0))
    config = glh.render_config(
        play_mode=True,
        all_brushes=brushes,
        all_things=things,
        all_lights=things,
    )
    context.bind()
    gl.glClearColor(*FOG_RGB, 1.0)
    with glh.no_gl_errors("play-mode dynamic-light render"):
        renderer.render_scene(projection, view, eye, brushes, things, None, config,
                          brush_slots=config["all_brush_slots"])
        gl.glFinish()

# ---------------------------------------------------------------------------
# Fog and clipping work together
# ---------------------------------------------------------------------------

def test_fog_removes_the_hard_edge_at_the_far_plane(renderer, context):
    """The point of the feature: no pop-out where the clip lands.

    Both frames use the same view distance, so the clip is in the same place
    and only the fog differs. Without fog the ground meets the background at
    full contrast; with it, the ground has already *become* the background.
    """
    brushes, things = ground_scene()
    unfogged = render(renderer, context, brushes, things,
                      settings(4096.0, fog_enabled=False))
    fogged = render(renderer, context, brushes, things, settings(4096.0))

    _, hard = sharpest_edge(unfogged)
    _, soft = sharpest_edge(fogged)

    assert hard > 100.0, (
        "the unfogged control shows no clip edge — the scene does not reach "
        "the far plane, so this test would prove nothing")
    assert soft < hard * 0.2, (
        f"fog left a hard edge at the clip: largest step {soft:.1f} vs "
        f"{hard:.1f} unfogged")


def test_where_the_clip_lands_the_frame_is_already_the_fog_colour(renderer, context):
    """Locate the clip in the unfogged control, then look there with fog on."""
    brushes, things = ground_scene()
    unfogged = render(renderer, context, brushes, things,
                      settings(4096.0, fog_enabled=False))
    fogged = render(renderer, context, brushes, things, settings(4096.0))

    clip_row, _ = sharpest_edge(unfogged)
    target = np.array(FOG_RGB) * 255.0
    at_and_beyond = fogged[:clip_row + 1].astype(float)

    deviation = float(np.abs(at_and_beyond - target).max())
    assert deviation < 8.0, (
        f"at the clip (row {clip_row}) the fogged frame is {deviation:.1f} "
        "levels off the fog colour — geometry is showing through the fog wall")


def test_distant_geometry_is_tinted_toward_the_fog_colour(renderer, context):
    """Ground inside the fog band reads as fog; ground in front of it does not."""
    brushes, things = ground_scene()
    image = render(renderer, context, brushes, things, settings(4096.0))

    far = redness(band(image, 0.58, 0.64))
    near = redness(band(image, 0.85, 1.00))
    assert far > 20.0, f"the far band was not fogged (redness {far:.1f})"
    assert near < 5.0, f"the near band was fogged (redness {near:.1f})"


def test_reducing_the_far_plane_leaves_the_rest_of_the_scene_alone(renderer, context):
    """Pulling the view distance in changes what is far away and nothing else."""
    brushes, things = ground_scene()
    wide = render(renderer, context, brushes, things, settings(8192.0))
    near = render(renderer, context, brushes, things, settings(4096.0))

    foreground = np.abs(band(wide, 0.85, 1.0) - band(near, 0.85, 1.0)).mean()
    assert foreground < 1.0, (
        f"the foreground shifted by {foreground:.2f} levels when only the far "
        "plane moved")

    distance = np.abs(band(wide, 0.50, 0.60) - band(near, 0.50, 0.60)).mean()
    assert distance > 5.0, (
        "the far plane moved but the distance it governs did not change")


def test_the_broad_phase_cull_follows_the_view_distance(renderer, context):
    """The entity broad-phase cull reads the live distance through dense slots."""
    import numpy as np
    from editor.things import Thing
    from engine.entity_table import EntityTable
    from engine.renderer_core import BaseRenderer

    inside = Thing(pos=[0.0, 0.0, 1500.0])
    outside = Thing(pos=[0.0, 0.0, 6000.0])
    table = EntityTable()
    table.begin_frame([inside, outside], epoch=1)
    slots = np.asarray([0, 1], dtype=np.int32)

    renderer.view_distance = settings(8192.0)
    kept = BaseRenderer._distance_cull_thing_slots(
        table, slots, 0.0, 0.0, renderer.view_distance.distance ** 2)
    assert kept.tolist() == [0, 1]

    renderer.view_distance = settings(2048.0)
    kept = BaseRenderer._distance_cull_thing_slots(
        table, slots, 0.0, 0.0, renderer.view_distance.distance ** 2)
    assert kept.tolist() == [0]


def test_fog_colour_is_what_distance_fades_to(renderer, context):
    """Same geometry, two fog colours: the far band takes each one."""
    brushes, things = ground_scene()
    red_frame = render(renderer, context, brushes, things, settings(4096.0))

    blue = settings(4096.0)
    blue.fog_color = (0.05, 0.05, 0.85)
    blue_frame = render(renderer, context, brushes, things, blue,
                        clear=(0.05, 0.05, 0.85))

    far_red = band(red_frame, 0.58, 0.64)
    far_blue = band(blue_frame, 0.58, 0.64)
    assert far_red[..., 0].mean() > far_blue[..., 0].mean() + 20.0
    assert far_blue[..., 2].mean() > far_red[..., 2].mean() + 20.0


def test_density_thickens_the_band(renderer, context):
    """A positive density deepens the fog without moving where it goes opaque."""
    brushes, things = ground_scene()
    linear = render(renderer, context, brushes, things, settings(4096.0))
    dense = render(renderer, context, brushes, things,
                   settings(4096.0, fog_density=0.003))

    assert redness(band(dense, 0.58, 0.64)) > redness(band(linear, 0.58, 0.64)) + 15.0
    # The foreground stays out of it: density thickens the band, it does not
    # drag fog forward over the player's feet.
    assert redness(band(dense, 0.85, 1.00)) < 5.0


def test_disabling_fog_is_a_true_no_op(renderer, context):
    """`r_distancefog off` must remove the fog, not leave a pale version."""
    brushes, things = ground_scene()
    off = render(renderer, context, brushes, things,
                 settings(4096.0, fog_enabled=False))
    assert redness(band(off, 0.58, 0.64)) < 5.0


# ---------------------------------------------------------------------------
# Global ambient
# ---------------------------------------------------------------------------

def test_ambient_lights_surfaces_no_light_entity_reaches(renderer, context):
    """`ambient` must brighten a level with no lights in it at all."""
    brushes, _ = ground_scene()
    dark = render(renderer, context, brushes, [],
                  settings(4096.0, fog_enabled=False))
    lit = render(renderer, context, brushes, [],
                 settings(4096.0, fog_enabled=False, ambient=(0.6, 0.6, 0.6)))

    dark_l = glh.luminance(band(dark, 0.85, 1.0)).mean()
    lit_l = glh.luminance(band(lit, 0.85, 1.0)).mean()
    assert lit_l > dark_l + 50.0, (
        f"ambient did not brighten the scene: {dark_l:.1f} -> {lit_l:.1f}")


def test_zero_ambient_renders_identically_to_no_ambient(renderer, context):
    """The default must leave every existing map looking exactly as before."""
    brushes, things = ground_scene()
    explicit = render(renderer, context, brushes, things,
                      settings(4096.0, ambient=(0.0, 0.0, 0.0)))
    default = render(renderer, context, brushes, things, settings(4096.0))
    assert np.array_equal(explicit, default)


def test_ambient_adds_no_entity_to_the_world(renderer, context):
    """It is a global light in effect, but nothing the map can see or save."""
    brushes, things = ground_scene()
    before = list(things)
    render(renderer, context, brushes, things,
           settings(4096.0, ambient=(0.4, 0.4, 0.4)))
    assert things == before
