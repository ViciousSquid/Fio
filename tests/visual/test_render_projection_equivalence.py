"""The numeric path must draw exactly what the object path drew.

``Renderer_F.render_scene`` feeds all brush passes from the dense render
projection. The portal virtual views also consume those slots, applying their
own virtual frustum as a numeric mask; entity sprites come from EntityTable
slots in the same pass.

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




def test_portal_virtual_scene_consumes_dense_tables(renderer):
    """Portal scene classification never reconstructs brush/entity objects."""
    from engine.entity_table import EntityTable
    from editor.things import Thing

    brushes = [
        box_brush('inside', (0.0, 0.0, 0.0), (64.0, 64.0, 64.0)),
        box_brush('outside', (100000.0, 0.0, 0.0), (64.0, 64.0, 64.0)),
    ]
    for brush in brushes:
        brush['textures'] = {'south': 'inside.png'}

    model = make_thing(
        Thing, 'portal_model', (0.0, 32.0, 0.0),
        model_path='assets/models/oil_drum.obj',
    )
    table, refs, slots = _projection_for(brushes)
    etable = EntityTable()
    hidden = etable.begin_frame([model], 1)

    projection, view, _eye = glh.camera_matrices(aspect=1.0)
    config = glh.render_config(
        all_brushes=brushes,
        all_things=[model],
        render_table=table,
        render_refs=refs,
        all_brush_slots=slots,
        entity_table=etable,
        entity_refs=np.asarray([model], dtype=object),
        visible_thing_slots=np.asarray([0], dtype=np.int32),
        thing_hidden=hidden,
        play_mode=True,
    )

    original_sort = renderer._sort_objects
    def fail_sort(*args, **kwargs):
        raise AssertionError("portal virtual view fell back to _sort_objects")
    renderer._sort_objects = fail_sort
    try:
        (table_out, groups, model_slots,
         sprite_slots, lights) = renderer._portal_numeric_scene_inputs(
            projection, view, config
        )
    finally:
        renderer._sort_objects = original_sort

    assert table_out is table
    assert set(groups['opaque'].tolist()) == {0}
    assert set(groups['textured'].tolist()) == {0}
    assert len(groups['solid']) == 0
    assert model_slots.tolist() == [0]
    assert len(sprite_slots) == 0
    assert isinstance(lights, tuple)
    assert lights[0] is etable

def test_portal_renderer_has_no_legacy_object_path():
    """Portal rendering must have no Thing/Portal object traversal fallback."""
    from pathlib import Path

    root = Path(__file__).parents[2]
    forward = (root / 'engine' / 'renderer_F.py').read_text(encoding='utf-8')
    core = (root / 'engine' / 'renderer_core.py').read_text(encoding='utf-8')

    portal_scene = forward[
        forward.index("def _portal_numeric_scene_inputs"):
        forward.index("def render_scene", forward.index("def _portal_numeric_scene_inputs"))
    ]
    portal_wire = core[
        core.index("def draw_portal_wireframes"):
        core.index("def draw_connection_lines", core.index("def draw_portal_wireframes"))
    ]

    assert "Portal)" not in portal_scene
    assert "isinstance" not in portal_scene
    assert "portal_things" not in portal_wire
    assert "isinstance" not in portal_wire
    assert "for t in things" not in portal_wire

def test_sprite_renderer_has_no_legacy_object_path(renderer):
    """There is exactly one sprite renderer: dense EntityTable instancing."""
    import engine.renderer_core as rc
    assert not hasattr(rc.BaseRenderer, "draw_sprites")
    assert not hasattr(renderer, "draw_sprites")


def test_numeric_sprite_render_submits_instanced_quads(renderer, context):
    """A populated EntityTable reaches GL through the instanced sprite pass."""
    import engine.renderer_core as rc

    brushes, things = _entity_scene()
    published = [
        t.get_render_snapshot() if type(t).__name__ == "Monster" else t
        for t in things
    ]
    calls = {"draws": 0, "instances": 0}
    real = rc.gl.glDrawArraysInstanced

    def draw_instanced(mode, first, count, instances, *args, **kwargs):
        calls["draws"] += 1
        calls["instances"] += int(instances)
        return real(mode, first, count, instances, *args, **kwargs)

    rc.gl.glDrawArraysInstanced = draw_instanced
    try:
        _render(renderer, context, brushes, published, numeric=True,
                live_things=things)
    finally:
        rc.gl.glDrawArraysInstanced = real

    assert calls["draws"] >= 1
    assert calls["instances"] >= 3, (
        "the entity scene contains a monster, pickup and billboard")


def test_numeric_sprite_submission_is_one_draw_per_texture(renderer, context):
    """Equal sprite texture ids collapse into one instanced draw run."""
    import engine.renderer_core as rc
    from editor.things import Light, Pickup

    brushes = [box_brush("floor", (0, -16, 0), (1024, 32, 1024))]
    things = [
        make_thing(Pickup, "p%d" % i, (i * 40 - 400, 48, 0),
                   item_type="health")
        for i in range(20)
    ]
    things.append(make_thing(Light, "l", (0, 300, 300),
                             color=[255, 255, 255], intensity=2.0,
                             radius=1400.0, state="on",
                             casts_shadows=False))
    calls = {"draws": 0, "instances": 0}
    real = rc.gl.glDrawArraysInstanced

    def draw_instanced(mode, first, count, instances, *args, **kwargs):
        calls["draws"] += 1
        calls["instances"] += int(instances)
        return real(mode, first, count, instances, *args, **kwargs)

    rc.gl.glDrawArraysInstanced = draw_instanced
    try:
        _render(renderer, context, brushes, things, numeric=True,
                live_things=things)
    finally:
        rc.gl.glDrawArraysInstanced = real

    assert calls["instances"] == 20
    assert calls["draws"] == 1


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
