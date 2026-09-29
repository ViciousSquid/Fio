"""The entity sprite pass: one draw, in depth order, from a texture array.

Billboards are blended with depth writes off, so the order they are drawn in is
part of the picture.  These tests use sprites with *different* textures --
the case the shared harness cannot see, because its loader hands every name
the same white texture -- and check that:

* a near sprite is drawn over a far one even when its texture was created
  first (a pass that groups by texture draws the lower id first, which puts
  the far sprite on top);
* interleaved textures still cost one draw;
* sampling a layer shows the same image the sprite's own 2D texture showed;
* the flame pass that shares the staging buffer never inherits a sprite's
  opacity.
"""

import numpy as np
import pytest

from tests.helpers import gl as glh
from tests.helpers.worlds import make_thing

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


def _texture(rgba_rows):
    """A mipmapped GL texture from an (h, w, 4) uint8 array."""
    import OpenGL.GL as gl

    rgba = np.ascontiguousarray(rgba_rows, dtype=np.uint8)
    h, w = rgba.shape[:2]
    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, w, h, 0,
                    gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, rgba.tobytes())
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER,
                       gl.GL_LINEAR_MIPMAP_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
    gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
    gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
    return int(tex)


def _solid(rgb, size=16):
    image = np.zeros((size, size, 4), dtype=np.uint8)
    image[..., :3] = rgb
    image[..., 3] = 255
    return _texture(image)


def _billboard(renderer, name, pos, tex_id, size=(200.0, 200.0)):
    """A billboard Prop whose sprite resolves to *tex_id*."""
    from editor.things import Prop
    from engine import entity_table

    prop = make_thing(Prop, name, pos, render_mode="billboard",
                      sprite_path="assets/sprites/%s.png" % name,
                      sprite_size=list(size))
    key = entity_table.sprite_candidates(prop)[0][0]
    renderer.sprite_textures[key] = tex_id
    return prop


def _render(renderer, context, things, eye=glh.CAMERA_EYE,
            target=glh.CAMERA_TARGET):
    import OpenGL.GL as gl

    projection, view, eye_v = glh.camera_matrices(aspect=1.0, eye=eye,
                                                  target=target)
    config = glh.render_config(all_brushes=[], all_things=things)
    context.bind()
    gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    renderer.render_scene(projection, view, eye_v, [], things, None, config,
                          brush_slots=np.empty(0, dtype=np.int32))
    gl.glFinish()
    return context.read_pixels()


def _count_sprite_draws(renderer):
    """Wrap the sprite pass and count the instanced draws issued inside it."""
    import engine.renderer_core as rc

    seen = {"draws": 0, "instances": 0}
    original_pass = renderer.draw_sprites_instanced
    real_draw = rc.gl.glDrawArraysInstanced

    def counting_draw(mode, first, count, instances, *a, **k):
        seen["draws"] += 1
        seen["instances"] += int(instances)
        return real_draw(mode, first, count, instances, *a, **k)

    def wrapped(*args, **kwargs):
        rc.gl.glDrawArraysInstanced = counting_draw
        try:
            return original_pass(*args, **kwargs)
        finally:
            rc.gl.glDrawArraysInstanced = real_draw

    renderer.draw_sprites_instanced = wrapped
    return seen


def test_near_sprite_covers_far_sprite_whatever_the_texture_ids(renderer, context):
    """Blended billboards must be drawn back to front across textures.

    The near sprite's texture is created first, so it has the lower GL id: a
    pass that groups by texture draws it first and then paints the far sprite
    over it, which is exactly what this would catch.
    """
    red = _solid((255, 0, 0))
    blue = _solid((0, 0, 255))
    assert red < blue
    # Both on the line of sight, the near one 270 units from the eye in XZ.
    near = _billboard(renderer, "near", (0.0, 90.0, 150.0), red)
    far = _billboard(renderer, "far", (0.0, 40.0, -150.0), blue,
                     size=(400.0, 400.0))
    image = _render(renderer, context, [far, near])
    centre = image[SIZE // 2, SIZE // 2].astype(int)
    assert centre[0] > 200 and centre[2] < 60, (
        "the far sprite was drawn over the near one: centre pixel %r"
        % (centre.tolist(),))


def test_interleaved_textures_are_one_draw(renderer, context):
    """Depth order alternates textures at every step and still costs one draw."""
    textures = [_solid(c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    things = [
        _billboard(renderer, "s%02d" % i, (0.0, 60.0, 200.0 - i * 30.0),
                   textures[i % 3], size=(40.0, 40.0))
        for i in range(12)
    ]
    seen = _count_sprite_draws(renderer)
    _render(renderer, context, things)
    assert seen["instances"] == 12
    assert seen["draws"] == 1, (
        "%d draws for 12 sprites over 3 textures" % seen["draws"])
    assert renderer._sprite_layers is not None
    assert renderer._sprite_layers.count == 3


def test_a_layer_shows_the_same_image_as_its_texture(renderer, context):
    """The array path samples what the per-texture path sampled."""
    image = np.zeros((32, 32, 4), dtype=np.uint8)
    image[:, :16, :3] = (230, 40, 40)          # left half red
    image[:, 16:, :3] = (40, 200, 60)          # right half green
    image[:16, :, 2] = 200                     # top half gains blue
    image[..., 3] = 255
    tex = _texture(image)
    sprite = _billboard(renderer, "split", (0.0, 60.0, 0.0), tex,
                        size=(260.0, 260.0))

    layered = _render(renderer, context, [sprite]).astype(np.int16)
    assert renderer._sprite_layers.count == 1

    # The fallback path draws from the sprite's own 2D texture.
    renderer._sprite_layers.disabled = True
    plain = _render(renderer, context, [sprite]).astype(np.int16)

    lit = plain.sum(axis=2) > 30
    assert lit.mean() > 0.1, "the sprite did not cover the view"
    assert float(np.abs(layered - plain).mean()) < 1.0


def test_array_grows_when_a_larger_sprite_arrives(renderer, context):
    small = _solid((255, 0, 0), size=16)
    big = _solid((0, 255, 0), size=128)
    a = _billboard(renderer, "a", (-60.0, 60.0, 0.0), small, size=(60.0, 60.0))
    _render(renderer, context, [a])
    layers = renderer._sprite_layers
    assert layers.size == 16
    b = _billboard(renderer, "b", (60.0, 60.0, 0.0), big, size=(60.0, 60.0))
    image = _render(renderer, context, [a, b])
    assert layers.size == 128 and layers.count == 2
    # Both still show their own colour after the re-blit.
    row = image[SIZE // 2]
    assert (row[:, 0] > 200).any() and (row[:, 1] > 200).any()


def test_flame_instances_are_fully_opaque_after_a_fading_sprite(renderer, context):
    """The flame pass shares the staging buffer; it must write its own alpha."""
    from engine.effect_entity import Effect

    fire = make_thing(Effect, "fire", (0.0, 60.0, 0.0), effect_type="FIRE",
                      preview=True)
    renderer._ensure_sprite_instance_buffer(64)
    renderer._sprite_instance_data[:] = 0.25
    _render(renderer, context, [fire])
    drawn = getattr(renderer, "_last_fire_count", None)
    data = renderer._sprite_instance_data
    # Whatever the pass wrote, the opacity column of what it wrote is 1.
    written = data[:, 0] != 0.25
    if not written.any():
        pytest.skip("no flame frames available to this renderer")
    assert np.all(data[written, 6] == 1.0), drawn


def test_frustum_cull_drops_only_what_cannot_reach_the_view(renderer, context):
    """Off-screen billboards are skipped; one straddling the edge is kept."""
    red = _solid((255, 0, 0))
    things = [
        # In front of the camera.
        _billboard(renderer, "front", (0.0, 60.0, 0.0), red, size=(40.0, 40.0)),
        # Behind the eye (it is at z=420, looking towards -z).
        _billboard(renderer, "behind", (0.0, 60.0, 900.0), red,
                   size=(40.0, 40.0)),
        # Far outside the left edge.
        _billboard(renderer, "left", (-2000.0, 60.0, 0.0), red,
                   size=(40.0, 40.0)),
        # Centre outside the right edge (~306 wide here) but its half-diagonal
        # reaches back in, so it can put pixels on screen and must be drawn.
        _billboard(renderer, "straddle", (330.0, 60.0, 0.0), red,
                   size=(80.0, 80.0)),
    ]
    seen = _count_sprite_draws(renderer)
    _render(renderer, context, things)
    stats = renderer.render_stats
    assert stats.entity_candidates == 4
    assert stats.culled_entities == 2
    assert seen["instances"] == 2


def test_model_rows_are_culled_by_their_measured_mesh_radius(renderer, context):
    """A model Prop off screen is skipped; its radius comes from its mesh."""
    from editor.things import Prop

    def drum(name, pos, scale):
        return make_thing(Prop, name, pos, render_mode="model",
                          model_path="assets/models/Oil_Drum.obj",
                          scale=[scale, scale, scale])

    things = [drum("seen", (0.0, 60.0, 0.0), 1.0),
              drum("gone", (-3000.0, 60.0, 0.0), 1.0),
              drum("huge", (-3000.0, 60.0, 0.0), 400.0)]
    _render(renderer, context, things)
    stats = renderer.render_stats
    assert stats.entity_candidates == 3
    # The small far drum cannot reach the view; scaled 400x the same mesh
    # does, so the radius must follow the row's scale.
    assert stats.culled_entities == 1
    radii = renderer._model_radius_by_recipe
    assert len(radii) and np.isfinite(radii).all() and (radii > 0).all()
