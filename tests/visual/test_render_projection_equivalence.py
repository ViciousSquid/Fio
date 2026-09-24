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


def _patterned_texture(index):
    """A distinct checkerboard, so UV state is actually visible.

    The shared visual harness loads a 1x1 white texture for every name, which
    is right for the lighting tests it was written for -- but it makes every UV
    mapping sample the same texel, so a scale, a shift or a rotation applied
    wrongly cannot be seen. Anything here that checks the UV path needs a
    texture with content, or it asserts nothing.
    """
    import OpenGL.GL as gl

    size = 16
    xs = np.arange(size)
    checker = ((xs[:, None] // 2 + xs[None, :] // 2) % 2).astype(np.uint8)
    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    rgba[..., 0] = np.where(checker, 40 + index * 60, 220)
    rgba[..., 1] = np.where(checker, 220, 30 + index * 40)
    rgba[..., 2] = np.where(checker, 90, 200 - index * 30)
    rgba[..., 3] = 255
    tex = gl.glGenTextures(1)
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, size, size, 0,
                    gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, rgba.tobytes())
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
    return tex


@pytest.fixture
def renderer(context):
    """A renderer whose textures carry a pattern, unlike the shared harness."""
    from engine.renderer_F import Renderer_F

    made_textures = {}

    def loader(name, subfolder):
        tex = made_textures.get(name)
        if tex is None:
            tex = _patterned_texture(len(made_textures))
            made_textures[name] = tex
        return tex

    made = Renderer_F(loader, 64, 4096, None)
    made.update_grid_buffers(4096, 64)
    made.set_sprite_textures({})
    _ENTITY_TABLES.clear()
    yield made
    _ENTITY_TABLES.clear()
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


def _entity_projection_for(live, published, table=None):
    """The entity projection the logic thread publishes beside the brush one.

    Mirrors production exactly, including the asymmetry that matters: the table
    is built over the *authoritative* Thing list, while the per-slot references
    hold what is actually handed to the renderer -- a render-snapshot dict for
    every Monster row.

    *table* is reused across frames the way a play session reuses one, so the
    warm sprite column is exercised on a live table rather than on a fresh one
    that has never seen the entity before.
    """
    from engine.entity_table import EntityTable

    table = table if table is not None else EntityTable()
    hidden = table.begin_frame(live, 1)
    refs = np.empty(len(published), dtype=object)
    for i, thing in enumerate(published):
        refs[i] = thing
    slots = np.arange(table.count, dtype=np.int32)
    return table, refs, slots, hidden


#: One projection per test, as a play session has one per session. Reset by
#: the renderer fixture so tests cannot leak interned ids into each other.
_ENTITY_TABLES = {}


class _InstanceTextureHost:
    """The handful of attributes ``QtGameView.update_instance_textures`` uses.

    The object path has two halves in production: this one, which runs on the
    Qt thread and resolves the per-entity texture override, and
    ``draw_sprites``, which falls back to the texture shared by a class.  A
    reference that ran only the second half would be a weaker renderer than the
    one Fio ships, and the comparison would flatter the dense path.  So the
    real function is driven here against a stub rather than reimplemented.
    """

    def __init__(self, renderer):
        self.renderer = renderer
        self.sprite_textures = renderer.sprite_textures
        self._instance_tex_hash = None

    def load_texture(self, filename, subfolder):
        return self.renderer.load_texture(filename, subfolder)


def _apply_instance_textures(renderer, published):
    """Run the production override resolution, as the Qt thread would."""
    from engine.qt_game_view import QtGameView

    host = _InstanceTextureHost(renderer)
    QtGameView.update_instance_textures(host, published)


def _render(renderer, context, brushes, things, numeric, live_things=None,
            **overrides):
    import OpenGL.GL as gl

    projection, view, eye = glh.camera_matrices(aspect=1.0)
    config = glh.render_config(all_brushes=brushes, all_things=things,
                               **overrides)
    brush_slots = None
    # The object path's first half. It runs every frame in production, before
    # the renderer sees anything, so it runs here for both paths -- what it
    # populates is a renderer-level cache, not a per-path one.
    _apply_instance_textures(renderer, things)
    if numeric:
        table, refs, slots = _projection_for(brushes)
        config["render_table"] = table
        config["render_refs"] = refs
        config["all_brush_slots"] = slots
        brush_slots = slots
        # Both halves of the projection, as the logic thread publishes them:
        # the numeric path in production never has one without the other.
        etable, erefs, eslots, ehidden = _entity_projection_for(
            live_things if live_things is not None else things, things,
            _ENTITY_TABLES.get('current'))
        _ENTITY_TABLES['current'] = etable
        config["entity_table"] = etable
        config["entity_refs"] = erefs
        config["visible_thing_slots"] = eslots
        config["thing_hidden"] = ehidden
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


def _entity_scene():
    """A scene whose *entities* exercise the passes the projection took over.

    One of each verdict the classification chain can reach: a sprite entity, a
    billboard, a pickup, a path node (drawn by nothing), and the light.
    """
    from editor.things import Light, Monster, PathNode, Pickup, Thing

    brushes = [box_brush("floor", (0, -16, 0), (1024, 32, 1024))]
    things = [
        make_thing(Monster, "grunt", (-160, 64, 0), monster_type="human"),
        make_thing(Pickup, "medkit", (0, 48, 0), item_type="health"),
        make_thing(Thing, "billboard", (160, 64, 0),
                   render_mode="billboard", sprite_path="assets/sprites/x.png"),
        make_thing(PathNode, "node", (0, 32, 200)),
        make_thing(Light, "entity_light", (0, 300, 300),
                   color=[255, 255, 255], intensity=2.0, radius=1400.0,
                   state="on", casts_shadows=False),
    ]
    return brushes, things


import contextlib


@contextlib.contextmanager
def _capture_sprite_submissions(renderer, out):
    """Record every billboard that reaches the GPU, from either sprite path.

    Pixels are the wrong instrument for the entity half -- the visual harness
    has no sprite atlas, so a misclassified billboard draws nothing either way
    and the image is identical while the classification is broken.  What both
    paths do have in common is the GL boundary: each ends up establishing a
    texture and submitting a quad at a world position and size.  Capturing
    that is sensitive to exactly what the dense path changed, and it compares
    the per-sprite path and the instanced one on equal terms.
    """
    import engine.renderer_core as rc

    state = {'tex': 0, 'pos': None, 'size': None}
    real = (rc.gl.glBindTexture, rc.gl.glUniform3fv, rc.gl.glUniform2f,
            rc.gl.glDrawArrays, rc.gl.glDrawArraysInstanced)

    def bind(target, tex_id, *a, **k):
        state['tex'] = int(tex_id)
        return real[0](target, tex_id, *a, **k)

    def u3(loc, count, value, *a, **k):
        state['pos'] = tuple(round(float(v), 3) for v in value)
        return real[1](loc, count, value, *a, **k)

    def u2(loc, x, y, *a, **k):
        state['size'] = (round(float(x), 3), round(float(y), 3))
        return real[2](loc, x, y, *a, **k)

    def draw(mode, first, count, *a, **k):
        # The per-sprite path: one quad per billboard, its state in uniforms.
        out.append(state['pos'] + state['size'] + (state['tex'],))
        return real[3](mode, first, count, *a, **k)

    def draw_instanced(mode, first, count, instances, *a, **k):
        # The instanced path: one draw per texture run, its state in the
        # packed buffer the renderer just uploaded.
        base = renderer._sprite_instance_base
        rows = renderer._sprite_instance_data[base:base + instances]
        for row in rows:
            out.append((round(float(row[0]), 3), round(float(row[1]), 3),
                        round(float(row[2]), 3), round(float(row[3]), 3),
                        round(float(row[4]), 3), state['tex']))
        return real[4](mode, first, count, instances, *a, **k)

    (rc.gl.glBindTexture, rc.gl.glUniform3fv, rc.gl.glUniform2f,
     rc.gl.glDrawArrays, rc.gl.glDrawArraysInstanced) = (
        bind, u3, u2, draw, draw_instanced)
    try:
        yield
    finally:
        (rc.gl.glBindTexture, rc.gl.glUniform3fv, rc.gl.glUniform2f,
         rc.gl.glDrawArrays, rc.gl.glDrawArraysInstanced) = real


def _submitted(renderer, context, brushes, published, live, numeric,
               **overrides):
    """Sprites, models and draw-call count for one path through render_scene."""
    sprites, models, draws = [], [], {'n': 0}

    real_sprite = renderer.draw_sprites
    real_inst = renderer.draw_sprites_instanced
    real_models = renderer.draw_models

    def with_capture(fn):
        def wrapped(*a, **k):
            before = len(sprites)
            with _capture_sprite_submissions(renderer, sprites):
                result = fn(*a, **k)
            draws['n'] += len(sprites) - before
            return result
        return wrapped

    def cap_models(projection, view, camera_pos, models_in, lights, config):
        models.extend(models_in)

    renderer.draw_sprites = with_capture(real_sprite)
    renderer.draw_sprites_instanced = with_capture(real_inst)
    renderer.draw_models = cap_models
    try:
        _render(renderer, context, brushes, published, numeric=numeric,
                live_things=live, **overrides)
    finally:
        renderer.draw_sprites = real_sprite
        renderer.draw_sprites_instanced = real_inst
        renderer.draw_models = real_models
    return sprites, models


def _assert_same_submissions(objects, slots, what):
    o_sprites, o_models = objects
    s_sprites, s_models = slots
    assert [id(m) for m in o_models] == [id(m) for m in s_models], (
        "%s: the two paths submitted different models" % what)
    # The instanced path regroups depth-ordered sprites into texture runs, so
    # the sequence differs by construction; the *set* of billboards drawn, with
    # their transforms and textures, may not.
    assert sorted(o_sprites) == sorted(s_sprites), (
        "%s: the object path submitted %d billboards and the instanced path "
        "%d, or with different positions, sizes or textures.\n  only object: "
        "%s\n  only instanced: %s"
        % (what, len(o_sprites), len(s_sprites),
           sorted(set(o_sprites) - set(s_sprites))[:4],
           sorted(set(s_sprites) - set(o_sprites))[:4]))


def test_the_two_paths_submit_the_same_entities(renderer, context):
    """The entity half of the projection, end to end through render_scene.

    A monster reaches the renderer as a snapshot dict, a pickup and a billboard
    through different branches of the chain, and a path node through none of
    them -- so a mask that mixes those up changes what is submitted.
    """
    brushes, things = _entity_scene()
    # A Monster is published as its render snapshot; that is what the object
    # path is handed in production, so it is what it is handed here.
    published = [t.get_render_snapshot() if type(t).__name__ == "Monster" else t
                 for t in things]
    objects = _submitted(renderer, context, brushes, published, things,
                         numeric=False)
    slots = _submitted(renderer, context, brushes, published, things,
                       numeric=True)
    assert objects[0], "the object path submitted no billboards at all"
    _assert_same_submissions(objects, slots, "entity scene")


def test_a_hidden_entity_is_absent_from_both(renderer, context):
    """`hidden` is the one entity field read live, so both paths must see it."""
    from editor.things import Thing

    brushes, things = _entity_scene()
    published = [t.get_render_snapshot() if type(t).__name__ == "Monster" else t
                 for t in things]
    for thing in published:
        if isinstance(thing, Thing):
            thing.properties["hidden"] = True
    objects = _submitted(renderer, context, brushes, published, things,
                         numeric=False)
    slots = _submitted(renderer, context, brushes, published, things,
                       numeric=True)
    _assert_same_submissions(objects, slots, "hidden entities")


def test_a_monsters_frame_follows_its_live_state(renderer, context):
    """The warm half: a monster that starts shooting must change texture.

    This is the field the cold projection deliberately does not hold, so if the
    warm refresh stopped running the sprite would freeze on its idle frame.
    """
    from editor.things import Light, Monster

    brushes = [box_brush("floor", (0, -16, 0), (1024, 32, 1024))]
    grunt = make_thing(Monster, "grunt", (0, 64, 0), monster_type="human")
    things = [grunt, make_thing(Light, "l", (0, 300, 300),
                                color=[255, 255, 255], intensity=2.0,
                                radius=1400.0, state="on", casts_shadows=False)]

    def run():
        published = [t.get_render_snapshot() if isinstance(t, Monster) else t
                     for t in things]
        objects = _submitted(renderer, context, brushes, published, things,
                             numeric=False)
        slots = _submitted(renderer, context, brushes, published, things,
                           numeric=True)
        _assert_same_submissions(objects, slots, "monster frame")
        return slots[0]

    idle = run()
    grunt.properties["is_shooting"] = True
    shooting = run()
    grunt.properties["is_shooting"] = False
    grunt.properties["dead"] = True
    dead = run()

    textures = {idle[0][5] if idle else None,
                shooting[0][5] if shooting else None,
                dead[0][5] if dead else None}
    assert len(textures) == 3, (
        "idle, shooting and dead resolved to %d distinct textures, not 3 -- "
        "the warm sprite column is not following the monster's state"
        % len(textures))


def test_a_retextured_billboard_changes_what_is_submitted(renderer, context):
    """A prop switching representation is warm state too."""
    from editor.things import Light
    from engine.prop_entity import Prop

    brushes = [box_brush("floor", (0, -16, 0), (1024, 32, 1024))]
    prop = make_thing(Prop, "crate", (0, 64, 0), render_mode="billboard",
                      sprite_path="assets/sprites/pickup.png")
    things = [prop, make_thing(Light, "l", (0, 300, 300),
                               color=[255, 255, 255], intensity=2.0,
                               radius=1400.0, state="on", casts_shadows=False)]

    def run():
        objects = _submitted(renderer, context, brushes, things, things,
                             numeric=False)
        slots = _submitted(renderer, context, brushes, things, things,
                           numeric=True)
        _assert_same_submissions(objects, slots, "prop representation")
        return slots[0]

    run()
    prop.properties["sprite_path"] = "assets/sprites/logic_relay.png"
    run()


def test_a_moved_sprite_follows_its_position_and_size(renderer, context):
    """Transforms are the warm column; the instances must track them."""
    from editor.things import Light, Pickup

    brushes = [box_brush("floor", (0, -16, 0), (1024, 32, 1024))]
    pickup = make_thing(Pickup, "medkit", (0, 48, 0), item_type="health")
    things = [pickup, make_thing(Light, "l", (0, 300, 300),
                                 color=[255, 255, 255], intensity=2.0,
                                 radius=1400.0, state="on",
                                 casts_shadows=False)]

    def run():
        objects = _submitted(renderer, context, brushes, things, things,
                             numeric=False)
        slots = _submitted(renderer, context, brushes, things, things,
                           numeric=True)
        _assert_same_submissions(objects, slots, "sprite transform")
        return slots[0]

    first = run()
    pickup.pos = [256.0, 96.0, -128.0]
    moved = run()
    assert first != moved, "the instance data did not follow the move"
    assert any(row[0:3] == (256.0, 96.0, -128.0) for row in moved), (
        "no instance was submitted at the moved position; got %s" % (moved,))


def test_depth_order_survives_being_grouped_into_a_run(renderer, context):
    """Grouping by texture must not reorder within a run.

    Billboards are drawn back to front, and `sort_into_runs` is a stable sort
    precisely so that the depth order the caller established survives being
    regrouped. Same texture here, so everything lands in one run and the
    instance order is the depth order.
    """
    from editor.things import Light, Pickup

    brushes = [box_brush("floor", (0, -16, 0), (1024, 32, 1024))]
    things = [make_thing(Pickup, "near", (0, 48, 300), item_type="health"),
              make_thing(Pickup, "far", (0, 48, -300), item_type="health"),
              make_thing(Pickup, "mid", (0, 48, 0), item_type="health"),
              make_thing(Light, "l", (0, 300, 300), color=[255, 255, 255],
                         intensity=2.0, radius=1400.0, state="on",
                         casts_shadows=False)]
    objects = _submitted(renderer, context, brushes, things, things,
                         numeric=False)
    slots = _submitted(renderer, context, brushes, things, things,
                       numeric=True)
    _assert_same_submissions(objects, slots, "sprite depth order")

    pickups = [row for row in slots[0] if row[1] == 48.0]
    assert len(pickups) == 3, "expected the three pickups, got %s" % (pickups,)
    zs = [row[2] for row in pickups]
    assert zs == sorted(zs), (
        "billboards were submitted in z order %s; the camera looks down -z, so "
        "far to near is ascending z and the run reordered them" % (zs,))


def test_instanced_billboards_are_fogged_like_the_per_sprite_ones(renderer,
                                                                  context):
    """The one thing a submission capture cannot see.

    Billboards are unlit, so fog is the only environment state the sprite pass
    uploads, and it is applied in the fragment shader from ``FragPos`` -- a
    varying the instanced vertex shader has to keep emitting.  Two paths that
    submit identical instances can still differ here.

    Deliberately a scene of nothing but billboards: with a floor in shot, the
    fog on the floor would dominate the image and the comparison would pass
    while saying nothing about sprites.
    """
    from editor.things import Light, Monster, Pickup

    things = [make_thing(Monster, "grunt", (-160, 64, -300), monster_type="human"),
              make_thing(Pickup, "medkit", (0, 48, -300), item_type="health"),
              make_thing(Light, "l", (0, 300, 0), color=[255, 255, 255],
                         intensity=2.0, radius=1400.0, state="on",
                         casts_shadows=False)]
    published = [t.get_render_snapshot() if type(t).__name__ == "Monster" else t
                 for t in things]

    # A band that brackets the billboards rather than saturating past them:
    # with everything fully fogged, a shader that lost FragPos would look
    # identical to one that kept it, and the test would prove nothing.
    renderer.view_distance.fog_enabled = True
    # The camera sits ~457 units from the origin and ~732 from the
    # billboards, so this band fogs them differently -- which is the whole
    # point: a shader that lost FragPos would fog them by the wrong distance.
    renderer.view_distance.fog_start = 400.0
    renderer.view_distance.fog_end = 900.0
    fogged = _render(renderer, context, [], published, numeric=True,
                     live_things=things)
    assert not glh.is_blank(fogged), "no billboard reached the framebuffer"
    renderer.view_distance.fog_enabled = False
    clear = _render(renderer, context, [], published, numeric=True,
                    live_things=things)
    assert float(np.abs(fogged - clear).mean()) > 0.2, (
        "turning fog off changed nothing, so this test cannot tell whether "
        "the instanced billboards are fogged at all")

    renderer.view_distance.fog_enabled = True
    objects_img = _render(renderer, context, [], published, numeric=False,
                          live_things=things)
    slots_img = _render(renderer, context, [], published, numeric=True,
                        live_things=things)
    # Both paths run the same fragment shader over the same quads, so this is
    # not "close enough" -- it is the same picture.
    diff = float(np.abs(objects_img - slots_img).mean())
    assert diff == 0.0, (
        "the instanced billboards differ from the per-sprite ones by %.4f mean "
        "levels; they should be pixel-identical" % diff)


def test_sprites_are_one_draw_per_texture_not_one_per_sprite(renderer, context):
    """The point of the change, asserted as work rather than as time."""
    import engine.renderer_core as rc
    from editor.things import Light, Pickup

    brushes = [box_brush("floor", (0, -16, 0), (1024, 32, 1024))]
    things = [make_thing(Pickup, "p%d" % i, (i * 40 - 400, 48, 0),
                         item_type="health") for i in range(20)]
    things.append(make_thing(Light, "l", (0, 300, 300), color=[255, 255, 255],
                             intensity=2.0, radius=1400.0, state="on",
                             casts_shadows=False))

    counts = {}

    def count_path(numeric):
        calls = {'draws': 0, 'instances': 0}
        real_draw = rc.gl.glDrawArrays
        real_inst = rc.gl.glDrawArraysInstanced

        def d(mode, first, count, *a, **k):
            calls['draws'] += 1
            return real_draw(mode, first, count, *a, **k)

        def di(mode, first, count, instances, *a, **k):
            calls['draws'] += 1
            calls['instances'] += instances
            return real_inst(mode, first, count, instances, *a, **k)

        real_sprites = renderer.draw_sprites
        real_isprites = renderer.draw_sprites_instanced

        def wrap(fn):
            def w(*a, **k):
                rc.gl.glDrawArrays, rc.gl.glDrawArraysInstanced = d, di
                try:
                    return fn(*a, **k)
                finally:
                    (rc.gl.glDrawArrays,
                     rc.gl.glDrawArraysInstanced) = real_draw, real_inst
            return w

        renderer.draw_sprites = wrap(real_sprites)
        renderer.draw_sprites_instanced = wrap(real_isprites)
        try:
            _render(renderer, context, brushes, things, numeric=numeric,
                    live_things=things)
        finally:
            renderer.draw_sprites = real_sprites
            renderer.draw_sprites_instanced = real_isprites
        return calls

    counts['object'] = count_path(False)
    counts['numeric'] = count_path(True)

    drawn = counts['object']['draws']
    assert drawn >= 20, ("the object path drew %d billboards; the scene has 21 "
                         "and this test asserts nothing if they are skipped"
                         % drawn)
    assert counts['numeric']['draws'] <= 3, (
        "the instanced path made %d draw calls for %d billboards; the whole "
        "point is one per texture run"
        % (counts['numeric']['draws'], counts['numeric']['instances']))
    assert counts['numeric']['instances'] == drawn, (
        "%d billboards were instanced but the object path drew %d"
        % (counts['numeric']['instances'], drawn))


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


# ---------------------------------------------------------------------------
# Submission: one draw per state run, not one per face
# ---------------------------------------------------------------------------

def _grid_scene(side=12, textures=4):
    """A scene big enough that per-face submission would be obvious."""
    import random
    from editor.things import Light

    random.seed(3)
    names = ['tex%d.png' % i for i in range(textures)]
    brushes = []
    for i in range(side * side):
        x = (i % side) * 200.0 - side * 100.0
        z = (i // side) * 200.0 - side * 100.0
        b = box_brush('g%d' % i, (x, 0.0, z), (128.0, 192.0, 128.0))
        faces = ('south', 'north', 'west', 'east', 'down', 'top')
        b['textures'] = {f: random.choice(names) for f in faces}
        # Per-face UV state, so the instance packing of scale, shift and
        # rotation is actually under test rather than uniformly default.
        b['uv_scale'] = {f: [random.choice([1.0, 2.0, 3.0]),
                             random.choice([1.0, 2.0])] for f in faces[:3]}
        b['uv_shift'] = {faces[1]: [0.25, 0.5], faces[4]: [0.125, 0.75]}
        b['uv_angle'] = {faces[2]: 30.0, faces[5]: 90.0}
        b['uv_natural'] = {faces[3]: True}
        brushes.append(b)
    light = make_thing(Light, 'gl', (0, 900, 0), color=[255, 255, 255],
                       intensity=2.0, radius=8000.0, state='on',
                       casts_shadows=False)
    return brushes, [light]


def test_faces_are_submitted_per_state_run_not_per_face(renderer, context):
    """The submission count must follow distinct state, not scene size.

    A run is one texture and one cube face; everything else that used to be a
    per-face uniform travels as instance data. So a level of any size drawn
    with T textures costs at most T * 6 submissions for its box brushes -- the
    Quake 3 backend's rule, that state changes only where the sorted key
    actually changes.
    """
    brushes, things = _grid_scene(side=12, textures=4)
    _render(renderer, context, brushes, things, numeric=True)
    stats = renderer.render_stats

    assert stats.visible_tris == len(brushes) * 12, "not all faces were drawn"
    assert stats.draw_calls <= 4 * 6, (
        "%d draw calls for %d brushes -- submission is still per face"
        % (stats.draw_calls, len(brushes)))


def test_submission_count_does_not_grow_with_the_scene(renderer, context):
    small, things = _grid_scene(side=6, textures=4)
    large, _ = _grid_scene(side=14, textures=4)

    _render(renderer, context, small, things, numeric=True)
    small_draws = renderer.render_stats.draw_calls
    _render(renderer, context, large, things, numeric=True)
    large_draws = renderer.render_stats.draw_calls

    assert len(large) > len(small) * 4
    assert large_draws == small_draws, (
        "submissions went from %d to %d as the scene grew %dx; they should "
        "follow distinct state, not object count"
        % (small_draws, large_draws, len(large) // len(small)))


def test_instanced_and_uniform_submission_draw_the_same_picture(renderer, context):
    """The instanced path against the per-face uniform path it replaced.

    The fallback still runs on any driver that rejects the instanced attribute
    interface, so the two have to agree -- and this is what says the instance
    packing (the normal matrix columns, the UV rotation tucked into a spare w,
    the scale/shift vec4) is laid out the way the shader reads it.
    """
    brushes, things = _grid_scene(side=8, textures=3)
    instanced = _render(renderer, context, brushes, things, numeric=True)

    saved = renderer.shaders.pop('brush_instanced')
    try:
        fallback = _render(renderer, context, brushes, things, numeric=True)
    finally:
        renderer.shaders['brush_instanced'] = saved

    assert renderer.render_stats.draw_calls > 100, (
        "the fallback did not take the per-face path, so this compares nothing")
    _assert_same_picture(instanced, fallback, "instanced vs per-face uniforms")


# ---------------------------------------------------------------------------
# The lit (flat-shaded) pass
# ---------------------------------------------------------------------------

def _solid_scene(side=10):
    """Untextured brushes, so they take the lit pass rather than the textured."""
    from editor.things import Light

    brushes = []
    for i in range(side * side):
        x = (i % side) * 200.0 - side * 100.0
        z = (i // side) * 200.0 - side * 100.0
        b = box_brush('s%d' % i, (x, 0.0, z), (128.0, 192.0, 128.0))
        b['textures'] = {}
        b['tint'] = [(i * 37) % 256, (i * 61) % 256, (i * 13) % 256]
        brushes.append(b)
    light = make_thing(Light, 'sl', (0, 900, 0), color=[255, 255, 255],
                       intensity=2.0, radius=8000.0, state='on',
                       casts_shadows=False)
    return brushes, [light]


def test_the_solid_world_is_one_submission(renderer, context):
    """Nothing varies per brush that is not instance data, so it is one run."""
    brushes, things = _solid_scene(side=10)
    _render(renderer, context, brushes, things, numeric=True)
    stats = renderer.render_stats
    assert stats.visible_tris == len(brushes) * 12
    assert stats.draw_calls == 1, (
        "%d draw calls for %d flat-shaded brushes" % (stats.draw_calls, len(brushes)))


def test_instanced_lit_matches_the_per_brush_path(renderer, context):
    brushes, things = _solid_scene(side=8)
    instanced = _render(renderer, context, brushes, things, numeric=True)

    saved = renderer.shaders.pop('lit_brush_instanced')
    try:
        fallback = _render(renderer, context, brushes, things, numeric=True)
    finally:
        renderer.shaders['lit_brush_instanced'] = saved

    assert renderer.render_stats.draw_calls >= len(brushes), (
        "the fallback did not take the per-brush path, so this compares nothing")
    _assert_same_picture(instanced, fallback, "instanced vs per-brush lit")


def _override_scene():
    """Four large brushes, one per colour-override case, filling the view.

    Large and few on purpose: a trigger is drawn by the *transparent* pass, as
    a wireframe unless the config asks for solid, so a scene of small brushes
    lets a wrong trigger colour hide in a few pixels of outline.
    """
    from editor.things import Light

    brushes = []
    for i, x in enumerate((-330.0, -110.0, 110.0, 330.0)):
        b = box_brush('o%d' % i, (x, 0.0, 0.0), (200.0, 320.0, 200.0))
        b['textures'] = {}
        b['tint'] = [30, 30, 30]      # dark, so any override is obvious
        brushes.append(b)
    brushes[0]['is_trigger'] = True
    brushes[1]['operation'] = 'subtract'
    brushes[2]['is_trigger'] = True        # selected below
    brushes[3]['operation'] = 'subtract'   # selected below
    light = make_thing(Light, 'ol', (0, 700, 700), color=[255, 255, 255],
                       intensity=2.0, radius=8000.0, state='on',
                       casts_shadows=False)
    return brushes, [light]


@pytest.mark.parametrize('selected_index', [None, 2, 3])
def test_the_colour_overrides_keep_their_priority(renderer, context,
                                                  selected_index):
    """trigger over selection over subtract over the brush's own colour.

    The per-brush chain was an if/elif; the instanced path writes masks over a
    payload, so the *order* of those writes is what encodes the priority. A
    selected trigger must still read as a trigger, and a selected subtract
    brush as selected. Triggers are drawn solid here so their colour and their
    0.3 alpha both reach the image.
    """
    import OpenGL.GL as gl

    brushes, things = _override_scene()
    selected = None if selected_index is None else brushes[selected_index]

    def draw():
        table, refs, slots = _projection_for(brushes)
        projection, view, eye = glh.camera_matrices(aspect=1.0)
        config = glh.render_config(all_brushes=brushes, all_things=things,
                                   render_table=table, render_refs=refs,
                                   all_brush_slots=slots,
                                   selected_object=selected,
                                   show_triggers_as_solid=True)
        context.bind()
        gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        renderer.render_scene(projection, view, eye, brushes, things, selected,
                              config, brush_slots=slots)
        gl.glFinish()
        return context.read_pixels().astype(np.int16)

    instanced = draw()
    saved = renderer.shaders.pop('lit_brush_instanced')
    try:
        fallback = draw()
    finally:
        renderer.shaders['lit_brush_instanced'] = saved

    _assert_same_picture(instanced, fallback,
                         'selection=%s' % (selected_index,))


# ---------------------------------------------------------------------------
# The render key is the boundary
# ---------------------------------------------------------------------------

def _distinct_texture_face_pairs(renderer, brushes, table, slots, config):
    """How many (texture, face) pairs the scene actually contains."""
    rows, faces, gl_tex, _scales, _starts = renderer._build_face_batches(
        table, slots, config)
    return len({(int(t), int(f)) for t, f in zip(gl_tex, faces)}), len(set(
        int(t) for t in gl_tex))


def test_one_submission_per_distinct_key_no_more_no_fewer(renderer, context):
    """The run count is the number of distinct keys, by construction.

    Fewer would mean two different GPU states got merged into one draw; more
    would mean the sort is not actually grouping. Either is invisible in the
    image, so neither is caught by comparing pixels.
    """
    brushes, things = _grid_scene(side=10, textures=5)
    _render(renderer, context, brushes, things, numeric=True)
    table, refs, slots = _projection_for(brushes)
    config = glh.render_config(all_brushes=brushes, all_things=things,
                               render_table=table, render_refs=refs,
                               all_brush_slots=slots)
    pairs, _textures = _distinct_texture_face_pairs(renderer, brushes, table,
                                                    slots, config)
    assert renderer.render_stats.draw_calls == pairs, (
        "%d draw calls for %d distinct (texture, face) keys"
        % (renderer.render_stats.draw_calls, pairs))


def test_texture_is_the_coarsest_field_so_each_binds_once(renderer, context):
    """Field order in the key decides how often the expensive state changes.

    Sorting by face before texture yields exactly the same picture and exactly
    the same number of runs -- but every texture is then bound once per face
    rather than once. Nothing about the image would show it, so the bind count
    is what pins the layout.
    """
    brushes, things = _grid_scene(side=10, textures=5)
    _render(renderer, context, brushes, things, numeric=True)
    table, refs, slots = _projection_for(brushes)
    config = glh.render_config(all_brushes=brushes, all_things=things,
                               render_table=table, render_refs=refs,
                               all_brush_slots=slots)
    _pairs, textures = _distinct_texture_face_pairs(renderer, brushes, table,
                                                    slots, config)
    assert renderer.render_stats.batched_draws == textures, (
        "%d texture binds for %d textures -- the key is not grouping by "
        "texture first" % (renderer.render_stats.batched_draws, textures))


# ---------------------------------------------------------------------------
# The shadow depth pass
# ---------------------------------------------------------------------------

def _caster_scene(count, casts=True):
    """Enough casters to exceed the instance buffer's initial capacity."""
    from editor.things import Light

    brushes = []
    side = int(count ** 0.5) + 1
    for i in range(count):
        x = (i % side) * 110.0 - side * 55.0
        z = (i // side) * 110.0 - side * 55.0
        b = box_brush('c%d' % i, (x, 0.0, z), (64.0, 96.0, 64.0))
        b['textures'] = {}
        brushes.append(b)
    light = make_thing(Light, 'caster_light', (0.0, 600.0, 0.0),
                       color=[255, 255, 255], intensity=2.0, radius=6000.0,
                       state='on', casts_shadows=casts)
    return brushes, [light]


def test_a_dirty_light_costs_six_submissions_not_six_per_caster(renderer, context):
    """The caster set does not vary between cube faces; only the matrix does.

    So the casters are packed once and drawn six times, rather than six times
    per caster -- which is what made the depth pass 96% of a frame's draw calls
    whenever a light or a mover moved.
    """
    import OpenGL.GL as gl

    brushes, things = _caster_scene(120)
    table, refs, slots = _projection_for(brushes)
    config = glh.render_config(all_brushes=brushes, all_things=things,
                               render_table=table, render_refs=refs,
                               all_brush_slots=slots, shadows_enabled=True)
    lights = [t for t in things]

    calls = []
    real = gl.glDrawArraysInstanced
    plain = []
    real_plain = gl.glDrawArrays
    gl.glDrawArraysInstanced = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    gl.glDrawArrays = lambda *a, **k: (plain.append(1), real_plain(*a, **k))[1]
    try:
        context.bind()
        renderer.render_shadow_maps(lights, brushes, things, config, None)
        gl.glFinish()
    finally:
        gl.glDrawArraysInstanced = real
        gl.glDrawArrays = real_plain

    assert len(calls) == 6, (
        "%d instanced submissions for one light's six faces" % len(calls))
    assert not plain, (
        "%d per-caster draws remain on the cube path" % len(plain))


def test_more_casters_than_the_buffers_initial_capacity(renderer, context):
    """Growing the instance buffer must not invalidate a held VAO.

    The shadow pass takes the VAO once and uses it across all six faces, so a
    growth that recreated it would leave the pass drawing with a deleted name.
    The brush passes never hit this because they fetch the VAO after packing;
    every scene in the suite was also small enough to fit the initial capacity,
    which is exactly why this is pinned at a size that is not.
    """
    import OpenGL.GL as gl

    brushes, things = _caster_scene(400)
    table, refs, slots = _projection_for(brushes)
    config = glh.render_config(all_brushes=brushes, all_things=things,
                               render_table=table, render_refs=refs,
                               all_brush_slots=slots, shadows_enabled=True)
    assert len(brushes) > 256, "the point is to exceed the initial capacity"

    with glh.no_gl_errors("rendering shadows for more casters than fit"):
        context.bind()
        renderer.render_shadow_maps(list(things), brushes, things, config, None)
        gl.glFinish()


def test_shadowed_output_survives_the_instanced_depth_pass(renderer, context):
    """Against the per-caster path, through the image a light actually casts."""
    brushes, things = _caster_scene(40)
    instanced = _render(renderer, context, brushes, things, numeric=True,
                        shadows_enabled=True)

    saved = renderer.shaders.pop('depth_cube_instanced')
    renderer._shadow_slot_sig = [None] * len(renderer._shadow_slot_sig)
    try:
        fallback = _render(renderer, context, brushes, things, numeric=True,
                           shadows_enabled=True)
    finally:
        renderer.shaders['depth_cube_instanced'] = saved
    _assert_same_picture(instanced, fallback, "instanced vs per-caster depth")
