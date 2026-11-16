"""Visual tier: reflection probes (editor.things.ReflectionProbe).

Water reflects the map's sky; under a roof that is the wrong thing to see.
Water whose centre is inside a probe's radius reflects a snapshot taken from
the probe instead -- here, a closed room with a red glowing ceiling and a
cloudy sky set on the map, so the two are easy to tell apart.

* inside a probe's radius the water shows the room, not the sky, high
  quality and cheap alike;
* water outside every probe is drawn exactly as before (which also shows the
  snapshot leaves the main view's framebuffer and state as it found them);
* a snapshot is taken once, and again only when its probe moves or the
  renderer is told the map changed; a map without probes takes none;
* the snapshot is the right way up, and mirrored below the horizon (water
  only ever looks up into it).
"""

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]

W, H = 192, 144
SKY = 'assets/backgrounds/cloudy-sky-panorama.webp'
WATER = (slice(H * 5 // 9, H), slice(0, W))       # the lower part of the frame


def _room(high_quality=False):
    from editor.things import Light
    from tests.helpers.worlds import box_brush, make_thing

    brushes = [
        box_brush('floor', (0, -16, 0), (1400, 32, 1400)),
        box_brush('ceiling', (0, 616, 0), (1400, 32, 1400), shader='Glow',
                  colour=[255, 40, 30], glow_intensity=1.2),
        box_brush('w1', (-716, 300, 0), (32, 600, 1400)),
        box_brush('w2', (716, 300, 0), (32, 600, 1400)),
        box_brush('w3', (0, 300, -716), (1400, 600, 32)),
        box_brush('w4', (0, 300, 716), (1400, 600, 32)),
    ]
    water = box_brush('water', (0, 40, 0), (800, 80, 800))
    water.update(shader='Water', is_water=True, water_tint=[0.05, 0.35, 0.45],
                 water_opacity=0.85, water_fresnel=1.0, water_reflectivity=1.0,
                 water_wave_height=0.25, water_wave_enabled=True,
                 water_high_quality=high_quality)
    lamp = make_thing(Light, 'lamp', (0.0, 450.0, 300.0), color=[255, 255, 255],
                      intensity=1.5, radius=2500.0, state='on', casts_shadows=False)
    return brushes + [water], [lamp]


def _probe(name, pos, radius):
    from editor.things import ReflectionProbe
    from tests.helpers.worlds import make_thing
    return make_thing(ReflectionProbe, name, pos, radius=radius)


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(W, H) as ctx:
        yield ctx
    glh.reset_texture_cache()


@pytest.fixture
def renderer(context):
    made = glh.make_renderer()
    made.set_background_image(SKY)
    yield made
    made.cleanup()


def _frame(renderer, context, brushes, things):
    import glm
    from editor.things import Light

    projection = glm.perspective(glm.radians(70.0), W / H, 1.0, 8000.0)
    eye = glm.vec3(0.0, 330.0, 620.0)
    view = glm.lookAt(eye, glm.vec3(0.0, 60.0, -60.0), glm.vec3(0, 1, 0))
    config = glh.render_config(all_brushes=brushes, all_things=things, play_mode=True,
                               all_lights=[t for t in things if isinstance(t, Light)],
                               time=2.0)
    context.bind()
    with glh.no_gl_errors("reflection probe frame"):
        renderer.render_scene(projection, view, eye, None, config,
                              brush_slots=config["all_brush_slots"])
    return context.read_pixels().astype(np.int32)


def _captures(renderer, monkeypatch):
    taken = []
    real = renderer._capture_reflection_probe

    def counted(position, config):
        taken.append(tuple(float(v) for v in position))
        return real(position, config)

    monkeypatch.setattr(renderer, '_capture_reflection_probe', counted)
    return taken


@pytest.mark.parametrize("high_quality", [False, True])
def test_water_in_a_probes_radius_reflects_the_room_not_the_sky(renderer, context,
                                                                high_quality):
    brushes, things = _room(high_quality)
    sky = _frame(renderer, context, brushes, things)
    room = _frame(renderer, context, brushes,
                  things + [_probe('probe', (0.0, 300.0, 0.0), 900.0)])
    # The sky is blue; the room is a red ceiling over grey walls.
    blue = lambda image: (image[WATER][..., 2] - image[WATER][..., 0]).mean()
    assert blue(sky) - blue(room) > 15.0, (blue(sky), blue(room))


def test_water_outside_every_probe_is_drawn_exactly_as_before(renderer, context):
    brushes, things = _room(True)
    before = _frame(renderer, context, brushes, things)
    after = _frame(renderer, context, brushes,
                   things + [_probe('far', (6000.0, 300.0, 6000.0), 200.0)])
    assert renderer._probe_textures, "the far probe's snapshot was not taken"
    assert np.array_equal(before, after)


def test_a_snapshot_is_taken_once_and_again_only_when_needed(renderer, context,
                                                             monkeypatch):
    taken = _captures(renderer, monkeypatch)
    brushes, things = _room()
    _frame(renderer, context, brushes, things)
    assert taken == [] and renderer._probe_frame is None    # no probes, no work

    probe = _probe('probe', (0.0, 300.0, 0.0), 900.0)
    for _ in range(3):
        _frame(renderer, context, brushes, things + [probe])
    assert taken == [(0.0, 300.0, 0.0)]

    probe.pos = [0.0, 280.0, 0.0]
    _frame(renderer, context, brushes, things + [probe])
    _frame(renderer, context, brushes, things + [probe])
    assert taken == [(0.0, 300.0, 0.0), (0.0, 280.0, 0.0)]
    assert len(renderer._probe_textures) == 1               # the old one is freed

    renderer.invalidate_reflection_probes()                  # map loaded, Play started
    _frame(renderer, context, brushes, things + [probe])
    assert len(taken) == 3

    _frame(renderer, context, brushes, things)               # probe deleted
    assert renderer._probe_textures == {} and renderer._probe_frame is None


def test_the_snapshot_is_the_room_the_right_way_up(renderer, context):
    import OpenGL.GL as gl

    brushes, things = _room()
    _frame(renderer, context, brushes, things + [_probe('probe', (0.0, 300.0, 0.0), 900.0)])
    texture = next(iter(renderer._probe_textures.values()))
    gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
    width = int(gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_WIDTH))
    height = int(gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_HEIGHT))
    gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
    raw = gl.glGetTexImage(gl.GL_TEXTURE_2D, 0, gl.GL_RGB, gl.GL_UNSIGNED_BYTE,
                           outputType=bytes)
    gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 4)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    snap = np.frombuffer(raw, np.uint8).reshape(height, width, 3).astype(np.int32)
    assert (width, height) == (4 * renderer.PROBE_FACE_SIZE, 2 * renderer.PROBE_FACE_SIZE)
    overhead = snap[: height // 8]                 # row 0 is straight up: the ceiling
    level = snap[height * 7 // 16: height // 2]    # just above the horizon: the walls
    assert (overhead[..., 0] - overhead[..., 2]).mean() > 60      # red
    assert abs(level[..., 0] - level[..., 2]).mean() < 20         # grey
    assert np.abs(snap - snap[::-1]).max() <= 2    # the lower half mirrors the upper


@pytest.mark.parametrize("play_mode", [False, True])
def test_the_probes_icon_is_drawn_while_editing_and_never_in_play(renderer, context,
                                                                  play_mode):
    """In the 3D view a probe is an icon, like a Light; in Play it is nothing
    (and with no water near it, it changes nothing else either)."""
    import glm

    brushes, things = glh.lit_cube_scene(shadows=False)
    probe = _probe('probe', (0.0, 220.0, 120.0), 300.0)    # above the cube, in view
    projection, view, eye = glh.camera_matrices(aspect=W / H, eye=(0.0, 220.0, 320.0),
                                                target=(0.0, 220.0, 0.0))

    def frame(scene_things):
        config = glh.render_config(all_brushes=brushes, all_things=scene_things,
                                   all_lights=things, play_mode=play_mode,
                                   grid_visible=False)
        context.bind()
        renderer.render_scene(projection, view, eye, None, config,
                              brush_slots=config["all_brush_slots"])
        return context.read_pixels().astype(np.int32)

    without, with_probe = frame(things), frame(things + [probe])
    changed = int((np.abs(with_probe - without).max(axis=2) > 0).sum())
    if play_mode:
        assert changed == 0, changed
    else:
        assert changed > 50, changed            # the icon, where the probe stands
