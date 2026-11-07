"""Visual tier: every AABB brush is drawn from the shared cube geometry.

The unified brush model changes what a brush *means* -- who owns its
geometry, transform and face material -- not how an ordinary box reaches
the GPU. ~99 % of Fio's brushes are boxes, and every one is drawn from one
36-vertex cube VBO, scaled and placed by its RenderTable row, with no
geometry of its own. This file is the guard on that, for every stage:

* no convex machinery runs for a box: no plane set, no convex solve, no
  geometry record, no tessellation, no shared-store page;
* every draw in a box-only frame -- textured, flat, wireframe, shadow,
  editor and play -- reads its positions from a buffer shared by all boxes
  (the cube VBO; the unit-cube edge VBO for wireframe), or from none
  (a full-screen pass); the set of buffers does not grow with the world;
* growing the world tenfold creates no buffer and no vertex array, and the
  instanced passes (textured, flat, shadow) issue the same number of draws;
* a box's dict is the same after a frame as before it.

Wireframe mode is the exception to the submission count, and is pinned as
it is: one ``glDrawArrays`` per box, from the shared edge buffer.
"""

import collections
import copy

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.qt]

MODES = ('Textured', 'Flat', 'Wireframe')
DRAWS = ('glDrawArrays', 'glDrawArraysInstanced', 'glDrawElements',
         'glDrawElementsInstanced', 'glMultiDrawArrays')
CREATES = ('glGenBuffers', 'glGenVertexArrays')
CONVEX_MACHINERY = ('get_convex', 'get_shape', 'box_planes', 'box_to_geometry',
                    'build_collision_mesh')


def box_world(count):
    """*count* ordinary boxes covering the box material and transform rules:
    two textures (so the instanced passes have more than one run), authored
    UV scale/angle/shift, natural scale, brush-wide tiling, caulk and
    nodraw faces, rotation (a spinning mover's runtime spin), movers and
    doors."""
    from tests.helpers import runtime
    from tests.helpers.worlds import box_brush
    out = []
    for i in range(count):
        box = box_brush("box%d" % i, ((i % 30) * 80.0, 0.0, (i // 30) * 80.0),
                        (64.0, 64.0 + (i % 3) * 16.0, 64.0),
                        textures={'top': ('Dev/128.jpg', 'Dev/256.jpg')[i % 2],
                                  'down': 'caulk.jpg', 'east': 'nodraw.jpg'})
        if i % 5 == 0:
            box.update(uv_scale={'north': [2.0, 0.5]}, uv_angle={'north': 30.0},
                       uv_shift={'west': [0.5, 0.25]})
        if i % 7 == 0:
            box.update(rot_axis=[0.0, 1.0, 0.0], is_mover=True)
            runtime.drive(box, spin=((0.0, 1.0, 0.0), 20.0))
        if i % 11 == 0:
            box['texture_tiling'] = True
        if i % 13 == 0:
            box['uv_natural'] = {'south': True}
        if i % 17 == 0:
            box['is_door'] = True
        out.append(box)
    return out


class _Recorder:
    """Counts draws by (call, program, attribute-0 buffer), GL object
    creation, and calls into the convex machinery."""

    def __init__(self, renderer):
        self.renderer = renderer
        self.draws = collections.Counter()
        self.creates = collections.Counter()
        self.convex = collections.Counter()
        self._restore = []

    def __enter__(self):
        import OpenGL.GL as gl
        from engine import brush_geometry as bg
        from engine.renderer.core import geometry as core_geometry
        names = {int(p): n for n, p in self.renderer.shaders.items() if p}

        def scalar(value):
            return int(np.asarray(value).ravel()[0])

        def patch(owner, name, wrapper):
            original = getattr(owner, name)
            self._restore.append((owner, name, original))
            setattr(owner, name, wrapper(original))

        for name in DRAWS:
            def draw(original, name=name):
                def wrapped(*args, **kwargs):
                    program = scalar(gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM))
                    buffer = scalar(gl.glGetVertexAttribiv(
                        0, gl.GL_VERTEX_ATTRIB_ARRAY_BUFFER_BINDING))
                    self.draws[(name, names.get(program, program), buffer)] += 1
                    return original(*args, **kwargs)
                return wrapped
            patch(gl, name, draw)
        for name in CREATES:
            def create(original, name=name):
                def wrapped(*args, **kwargs):
                    self.creates[name] += 1
                    return original(*args, **kwargs)
                return wrapped
            patch(gl, name, create)
        for name in CONVEX_MACHINERY:
            def counted(original, name=name):
                def wrapped(*args, **kwargs):
                    self.convex[name] += 1
                    return original(*args, **kwargs)
                return wrapped
            patch(bg, name, counted)
        for owner, name in ((bg.ConvexGeometry, '__init__'),
                            (core_geometry, 'tessellate_geo')):
            def counted(original, name=name):
                def wrapped(*args, **kwargs):
                    self.convex[name] += 1
                    return original(*args, **kwargs)
                return wrapped
            patch(owner, name, counted)
        return self

    def __exit__(self, *exc):
        for owner, name, original in reversed(self._restore):
            setattr(owner, name, original)

    def buffers(self):
        return {buffer for (_call, _program, buffer) in self.draws}

    def instanced(self):
        return sum(n for (call, _p, _b), n in self.draws.items()
                   if call == 'glDrawArraysInstanced')


@pytest.fixture
def scene():
    glh.reset_texture_cache()
    with glh.GLTestContext(256, 256) as ctx:
        from engine.renderer.forward import ForwardRenderer
        from editor.things import Light
        from tests.helpers.worlds import make_thing
        renderer = ForwardRenderer(None)
        renderer.set_grid(4096, 64)
        renderer.set_sprite_textures({})
        light = make_thing(Light, "sun", (1200.0, 400.0, 600.0),
                           color=[255, 240, 220], intensity=2.0, radius=6000.0,
                           state="on", casts_shadows=True)
        try:
            yield ctx, renderer, light
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()


def _frame(ctx, renderer, light, brushes, mode, play):
    config = glh.render_config(all_brushes=brushes, all_things=[light],
                               play_mode=play, shadows_enabled=True,
                               brush_display_mode=mode)
    projection, view, eye = glh.camera_matrices(
        aspect=1.0, eye=(1200.0, 900.0, 1500.0), target=(1200.0, 0.0, 300.0))
    ctx.bind()
    renderer.render_scene(projection, view, eye, None, config,
                          brush_slots=config['all_brush_slots'])
    return config['render_table']


def _shared_buffers(renderer):
    """The vertex buffers every box shares: the cube, the unit-cube edges
    (wireframe, selection outline) and the occlusion probe's unit cube
    (play, one draw per cell group); 0 is a full-screen pass."""
    return {int(renderer._cube_vbo or 0), int(renderer._edge_vbo or 0),
            int(getattr(renderer, '_occlusion_vbo', 0) or 0), 0}


def _record_two_frames(scene, brushes, mode, play):
    """The first frame after the world changes, and a second one."""
    ctx, renderer, light = scene
    _frame(ctx, renderer, light, box_world(7), mode, play)
    frames = []
    for _ in range(2):
        with _Recorder(renderer) as recorder:
            table = _frame(ctx, renderer, light, brushes, mode, play)
        frames.append(recorder)
    return frames, table


@pytest.mark.parametrize("play", (False, True), ids=("editor", "play"))
@pytest.mark.parametrize("mode", MODES)
def test_boxes_are_drawn_from_shared_geometry_alone(scene, mode, play):
    _ctx, renderer, _light = scene
    # The small world has every variant the large one has, so both make the
    # same instanced runs; the large one has ten times the boxes.
    small, large = box_world(60), box_world(600)
    _frame(*scene, box_world(60), mode, play)          # warm: shaders, textures
    before = copy.deepcopy(large)
    few, _ = _record_two_frames(scene, small, mode, play)
    many, table = _record_two_frames(scene, large, mode, play)

    for frame in few + many:
        assert not frame.convex, dict(frame.convex)
        assert frame.draws, "the frame drew nothing"
        # Positions come only from buffers every box shares.
        assert frame.buffers() <= _shared_buffers(renderer), frame.buffers()
    assert table.geometry_records == [] and (table.geometry_id == -1).all()
    assert renderer._geo_mesh_cache == {}
    store = renderer.__dict__.get('_geo_shared_store')
    assert store is None or not [p for p in store.pages if p is not None]
    for frame in many:
        assert not frame.creates, dict(frame.creates)
    assert renderer._cube_vbo in many[0].buffers()
    # Instanced (the textured, flat and shadow passes): ten times the boxes,
    # the same draws, frame for frame.
    assert [f.instanced() for f in many] == [f.instanced() for f in few]
    assert many[0].instanced() > 0
    if mode == 'Wireframe':
        # Known: wireframe draws each box's edges with its own glDrawArrays
        # (from the shared unit-cube edge buffer). A submission per box, not
        # an allocation; pinned so a change to it is seen.
        edges = sum(n for (call, program, _b), n in many[0].draws.items()
                    if call == 'glDrawArrays' and program == 'simple')
        assert edges == len(large)
    assert large == before, "a frame changed a box's dict"
