"""Visual tier: box instances upload only what changed (renderer M3).

Each pass that draws box brushes keeps its own instance buffer
(``instancing.BrushStream``) and every row's matrices are kept by change
(``core/slot_transforms.SlotTransforms``). These tests pin:

* **Parity.** Through random moves, turns, edits, retextures, UV and colour
  edits, selection changes, hide/show (a streaming park), added and deleted
  brushes, camera turns and two render tables taking turns, the long-lived
  renderer draws exactly what a renderer whose instance buffers are rebuilt
  from scratch every frame draws -- pixel for pixel -- every stream's buffer
  on the GPU holds exactly the rows a fresh pack would, and every array a
  pass keeps derived from the tables (M3.1) is what deriving it afresh
  gives.
* **Steady state.** A still world uploads no instance byte; a mover uploads
  its own rows; a changed selection its own rows; a large world's mover does
  not re-upload the rest.
* **Lifecycle.** Cleanup releases every stream; a new renderer in the same
  context (renderer switching, a recreated context) draws the same picture.
"""

import random

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl]

SIZE = 128


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as ctx:
        yield ctx
    glh.reset_texture_cache()


def _renderer():
    made = glh.make_renderer()
    made.shadows_enabled = False
    return made


def _world(n=24, textured=True):
    from tests.helpers.worlds import angled_brush, box_brush
    brushes = [box_brush("floor", (0, -16, 0), (1400, 32, 1400))]
    rng = random.Random(7)
    for i in range(n):
        props = {} if textured else {"textures": {}}
        brushes.append(box_brush(
            "b%d" % i, (rng.uniform(-500, 500), rng.uniform(10, 120), rng.uniform(-500, 500)),
            (rng.uniform(20, 90), rng.uniform(20, 90), rng.uniform(20, 90)),
            color=[rng.random(), rng.random(), rng.random()], **props))
    brushes.append(angled_brush("ramp", (100, 40, -100), (120, 80, 120)))
    return brushes


def _draw(renderer, context, config, eye=(0, 520, 760), slots=None):
    import OpenGL.GL as gl
    context.bind()
    gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
    renderer.render_scene(*glh.camera_matrices(eye=eye, target=(0, 0, 0)),
                          None, config, clear=False,
                          brush_slots=config['all_brush_slots'] if slots is None else slots)
    return context.read_pixels()


def _fresh_streams(renderer):
    """Make *renderer* rebuild every instance buffer from scratch (what the
    renderer did before M3): every frame starts with no stream."""
    renderer._release_brush_streams()
    renderer._slot_transform_caches.clear()
    renderer._pass_memos.clear()


def _same(kept, fresh):
    if isinstance(kept, dict):
        return kept.keys() == fresh.keys() and all(_same(kept[k], fresh[k]) for k in kept)
    if isinstance(kept, (list, tuple)):
        return len(kept) == len(fresh) and all(map(_same, kept, fresh))
    if isinstance(kept, np.ndarray) or isinstance(fresh, np.ndarray):
        return (np.asarray(kept).dtype == np.asarray(fresh).dtype
                and np.array_equal(kept, fresh))
    return kept == fresh


def _derived_as_fresh(kept, fresh, table):
    """Every array *kept* holds derived from *table* for a pass drawn this
    frame equals what *fresh* (which derives everything every frame)
    derived."""
    for key, tables in fresh._pass_memos.items():
        want = tables[id(table)][4]
        got = kept._pass_memos[key][id(table)][4]
        assert _same(got, want), key


def _gpu_rows(stream):
    import OpenGL.GL as gl
    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, stream.vbo)
    data = gl.glGetBufferSubData(gl.GL_ARRAY_BUFFER, 0, stream.count * 128)
    return np.frombuffer(bytes(data), dtype=np.float32).reshape(stream.count, 32)


def _streams_hold_a_fresh_pack(renderer, table):
    from engine.renderer.core.slot_transforms import SlotTransforms
    fresh = SlotTransforms(table)
    fresh.sync(table)
    for key, stream in renderer._brush_streams.items():
        if not stream.count or key[0] == 'shadow':
            continue
        slots = stream.slots[:stream.count]
        normals = next(iter(stream.seen.values()))[3] if stream.seen else True
        block = renderer._stream_block(fresh, slots, stream.spare[:stream.count],
                                       stream.payload[:stream.count], normals)
        assert np.array_equal(stream.mirror[:stream.count].view(np.uint32),
                              block.view(np.uint32)), key
        assert np.array_equal(_gpu_rows(stream).view(np.uint32),
                              block.view(np.uint32)), key


@pytest.mark.parametrize("display", ["Textured", "Solid"])
def test_change_driven_streams_draw_what_a_full_rebuild_draws(context, display):
    from engine.change_journal import VISIBILITY, touch
    from engine.render_table import RenderTable
    from engine.spatial import PARKED_HIDDEN_KEY
    from tests.helpers.worlds import box_brush

    brushes = _world(textured=(display == "Textured"))
    # Some faces keep a constant texel size, so a resize changes their UV
    # scale (what a pass derives from a box's extent).
    for brush in brushes[1:-1:3]:
        brush['texture_tiling'] = True
    brushes[2]['uv_natural'] = {'top': True, 'south': True}
    tables = [RenderTable(), RenderTable()]
    for table in tables:
        table.begin_frame(brushes, epoch=1)
    configs = [glh.render_config(all_brushes=brushes, render_table=table,
                                 all_brush_slots=table.shown()[1].astype(np.int32),
                                 shadows_enabled=False, brush_display_mode=display)
               for table in tables]
    kept, fresh = _renderer(), _renderer()
    rng = random.Random(11)
    epoch = 1
    eye = [0.0, 520.0, 760.0]
    try:
        for frame in range(60):
            op = frame % 10
            dirty = set()
            if op == 0:                                  # movers
                for i in rng.sample(range(1, len(brushes) - 1), 4):
                    brushes[i]['pos'][1] += rng.uniform(-20, 20)
                    dirty.add(id(brushes[i]))
            elif op == 1:                                # a turn
                i = rng.randrange(1, len(brushes) - 1)
                brushes[i]['rotation'] = [0.0, rng.uniform(0, 90), 0.0]
                dirty.add(id(brushes[i]))
            elif op == 2:                                # selection
                from editor.selection_overlay import describe_selection
                chosen = describe_selection(brushes[rng.randrange(1, len(brushes))])
                for config in configs:
                    config['primary_selection'] = chosen
            elif op == 3:                                # a park / unpark
                i = rng.randrange(1, len(brushes) - 1)
                brush = brushes[i]
                if PARKED_HIDDEN_KEY in brush:
                    brush['hidden'] = brush.pop(PARKED_HIDDEN_KEY)
                else:
                    brush[PARKED_HIDDEN_KEY] = brush.get('hidden', False)
                    brush['hidden'] = True
                touch(brush, VISIBILITY)
                dirty.add(id(brush))
            elif op == 4:                                # the camera turns
                eye[0] += 140.0
                eye[2] -= 60.0
            elif op == 5:                                # a brush added
                brushes.insert(rng.randrange(1, len(brushes)), box_brush(
                    "new%d" % frame, (rng.uniform(-300, 300), 30, rng.uniform(-300, 300)),
                    (40, 40, 40), **({} if display == "Textured" else {"textures": {}})))
                dirty = None
            elif op == 6:                                # a brush deleted
                del brushes[rng.randrange(1, len(brushes) - 1)]
                dirty = None
            elif op == 7:                                # a resize
                i = rng.randrange(1, len(brushes) - 1)
                brushes[i]['size'][0] *= 1.3
                dirty.add(id(brushes[i]))
            elif op == 8:                                # a retexture
                i = rng.randrange(1, len(brushes) - 1)
                brush = brushes[i]
                if display == "Textured":
                    face = rng.choice(["top", "north", "east"])
                    brush['textures'][face] = rng.choice(
                        ["Dev/256.jpg", "Dev/512.jpg", "caulk.jpg", "nodraw.jpg"])
                brush['tint'] = [rng.randrange(256) for _ in range(3)]
                dirty.add(id(brush))
            else:                                        # a UV edit
                i = rng.randrange(1, len(brushes) - 1)
                brush = brushes[i]
                brush['uv_shift'] = {'top': [rng.uniform(0, 1), rng.uniform(0, 1)]}
                brush['uv_angle'] = {'north': rng.choice([0.0, 45.0, 90.0])}
                brush['uv_scale'] = {'east': [rng.uniform(0.5, 2), 1.0]}
                dirty.add(id(brush))
            epoch += 1
            for table, config in zip(tables, configs):
                table.begin_frame(brushes, epoch=epoch, dirty_objects=dirty)
                config['all_brush_slots'] = table.shown()[1].astype(np.int32)
                config['all_brushes'] = brushes
            # The two double-buffered tables take turns.
            config = configs[frame % 2]
            got = _draw(kept, context, config, eye=tuple(eye))
            _streams_hold_a_fresh_pack(kept, config['render_table'])
            _fresh_streams(fresh)
            want = _draw(fresh, context, config, eye=tuple(eye))
            assert np.array_equal(got, want), "frame %d (op %d) differs" % (frame, op)
            _derived_as_fresh(kept, fresh, config['render_table'])
        # Reuse, not just rebuilds, was what the check above checked.
        assert kept.pass_memo_hits > 0
    finally:
        kept.cleanup()
        fresh.cleanup()


@pytest.mark.parametrize("display", ["Textured", "Solid"])
def test_kept_pass_arrays_follow_each_kind_of_edit(context, display):
    """M3.1: what a pass derives from the table -- its groups, face batches,
    UV scale, shift and rotation, payloads and runs -- is kept from frame to
    frame and rebuilt when, and only when, an edit changes what it was
    derived from. After each edit below, on its own, it equals what a
    renderer deriving everything afresh derives, and the picture matches; a
    still frame, a move or a selection reuses it (the lit pass applies the
    selection to its kept payload as it draws)."""
    from editor.selection_overlay import describe_selection
    from engine.render_table import RenderTable

    brushes = _world(textured=(display == "Textured"))
    for brush in brushes[1:-1]:
        brush['texture_tiling'] = True       # a resize changes the UV scale
    table = RenderTable()
    epoch = 1
    table.begin_frame(brushes, epoch=epoch)
    config = glh.render_config(all_brushes=brushes, render_table=table,
                               all_brush_slots=table.shown()[1].astype(np.int32),
                               shadows_enabled=False, brush_display_mode=display)
    target = brushes[5]

    def move():
        target['pos'][0] += 40.0

    def select():
        config['primary_selection'] = describe_selection(target)

    def resize():
        target['size'][0] *= 1.5

    def retexture():
        target['textures']['top'] = 'Dev/256.jpg'
        target['textures']['north'] = 'caulk.jpg'

    def uv():
        target['uv_shift'] = {'top': [0.25, 0.5]}
        target['uv_angle'] = {'south': 45.0}
        target['uv_scale'] = {'east': [2.0, 1.0]}

    def tint():
        target['tint'] = [200, 40, 40]

    def hide():
        target['hidden'] = True

    edits = [('still', None), ('move', move), ('select', select),
             ('resize', resize),
             ('retexture', retexture), ('uv', uv), ('tint', tint), ('hide', hide)]
    kept, fresh = _renderer(), _renderer()
    try:
        _draw(kept, context, config)
        _draw(kept, context, config)
        for label, edit in edits:
            if edit is select:
                edit()
            elif edit is not None:
                edit()
                epoch += 1
                table.begin_frame(brushes, epoch=epoch, dirty_objects={id(target)})
                config['all_brush_slots'] = table.shown()[1].astype(np.int32)
            misses = kept.pass_memo_misses
            got = _draw(kept, context, config)
            rebuilt = kept.pass_memo_misses - misses
            _fresh_streams(fresh)
            want = _draw(fresh, context, config)
            assert np.array_equal(got, want), label
            _derived_as_fresh(kept, fresh, table)
            if label in ('still', 'move', 'select'):
                assert rebuilt == 0, label
            else:
                assert rebuilt > 0, label
    finally:
        kept.cleanup()
        fresh.cleanup()


def _uploaded(renderer, context, config, frames=3, **kwargs):
    before = renderer.instance_bytes_uploaded
    images = [_draw(renderer, context, config, **kwargs) for _ in range(frames)]
    return renderer.instance_bytes_uploaded - before, images


@pytest.mark.parametrize("display", ["Textured", "Solid"])
def test_a_still_world_uploads_no_instance_bytes(context, display):
    brushes = _world(textured=(display == "Textured"))
    config = glh.render_config(all_brushes=brushes, shadows_enabled=False,
                               brush_display_mode=display)
    renderer = _renderer()
    try:
        first = _draw(renderer, context, config)
        assert renderer.instance_bytes_uploaded > 0
        sent, images = _uploaded(renderer, context, config, frames=4)
        assert sent == 0
        for image in images:
            assert np.array_equal(image, first)
    finally:
        renderer.cleanup()


def test_a_still_world_with_shadows_uploads_no_instance_bytes(context):
    brushes, things = glh.lit_cube_scene()
    brushes = brushes + _world(8)[1:]
    config = glh.render_config(all_brushes=brushes, all_things=things)
    renderer = glh.make_renderer()
    try:
        etable = config['entity_table']
        lights = np.flatnonzero(etable.light_color[:etable.count].any(axis=1)) \
            if hasattr(etable, 'light_color') else np.empty(0, np.int32)
        for _ in range(2):
            if len(lights):
                renderer.render_shadow_maps((etable, lights.astype(np.int32)), config)
            _draw(renderer, context, config)
        assert len(lights)
        assert any(key[0] == 'shadow' for key in renderer._brush_streams)
        before = renderer.instance_bytes_uploaded
        for _ in range(3):
            renderer.render_shadow_maps((etable, lights.astype(np.int32)), config)
            _draw(renderer, context, config)
        assert renderer.instance_bytes_uploaded == before
    finally:
        renderer.cleanup()


def _large_world(n=1500):
    from tests.helpers.worlds import box_brush
    brushes = [box_brush("floor", (0, -16, 0), (4000, 32, 4000), textures={})]
    side = int(np.ceil(np.sqrt(n)))
    for i in range(n):
        brushes.append(box_brush("c%d" % i, ((i % side) * 50.0 - 900, 20.0,
                                             (i // side) * 50.0 - 900),
                                 (30, 30, 30), textures={}))
    return brushes


def test_a_mover_uploads_its_own_rows(context):
    from engine import brush_placement as bp
    brushes = _large_world()
    config = glh.render_config(all_brushes=brushes, shadows_enabled=False,
                               brush_display_mode="Solid")
    table = config['render_table']
    renderer = _renderer()
    source = bp.RuntimeOverrides()
    bp.register_runtime(source)
    try:
        _draw(renderer, context, config, eye=(0, 2400, 2400))
        _draw(renderer, context, config, eye=(0, 2400, 2400))
        mover = 700
        brushes[mover]['is_mover'] = True
        for step in range(3):
            source.set(brushes[mover], centre=(brushes[mover]['pos'][0],
                                               40.0 + 10 * step, brushes[mover]['pos'][2]))
            table.refresh_transforms(brushes, np.array([mover], dtype=np.intp))
            sent, _images = _uploaded(renderer, context, config, frames=1,
                                      eye=(0, 2400, 2400))
            assert 0 < sent <= 128, sent            # its one row of the lit pass
        sent, _images = _uploaded(renderer, context, config, frames=2,
                                  eye=(0, 2400, 2400))
        assert sent == 0
    finally:
        source.clear()
        renderer.cleanup()


def test_a_changed_selection_uploads_its_own_rows(context):
    from editor.selection_overlay import describe_selection
    brushes = _large_world()
    config = glh.render_config(all_brushes=brushes, shadows_enabled=False,
                               brush_display_mode="Solid")
    renderer = _renderer()
    try:
        _draw(renderer, context, config, eye=(0, 2400, 2400))
        for chosen in (brushes[10], brushes[900], brushes[10]):
            config['primary_selection'] = describe_selection(chosen)
            sent, _images = _uploaded(renderer, context, config, frames=1,
                                      eye=(0, 2400, 2400))
            # The newly selected row and the one it replaces (coalesced only
            # when they are close).
            assert 0 < sent <= 2 * 128 * (renderer.STREAM_GAP_ROWS + 1), sent
    finally:
        renderer.cleanup()


def test_cleanup_releases_every_stream_and_a_new_renderer_draws_the_same(context):
    brushes = _world()
    config = glh.render_config(all_brushes=brushes, shadows_enabled=False)
    first = _renderer()
    try:
        before = _draw(first, context, config)
        streams = list(first._brush_streams.values())
        assert streams and all(s.vbo and s.vao for s in streams)
    finally:
        first.cleanup()
    assert not first._brush_streams and not first._slot_transform_caches
    import OpenGL.GL as gl
    for stream in streams:
        assert not gl.glIsBuffer(stream.vbo)
    second = _renderer()
    try:
        assert np.array_equal(_draw(second, context, config), before)
    finally:
        second.cleanup()


def test_two_renderers_in_turn_keep_their_own_streams(context):
    brushes = _world()
    config = glh.render_config(all_brushes=brushes, shadows_enabled=False)
    table = config['render_table']
    a, b = _renderer(), _renderer()
    try:
        reference = _draw(a, context, config)
        for frame in range(4):
            brushes[0]['pos'][1] -= 40.0            # the floor, always in view
            table.begin_frame(brushes, epoch=2 + frame, dirty_objects={id(brushes[0])})
            image_a = _draw(a, context, config)
            image_b = _draw(b, context, config)
            assert np.array_equal(image_a, image_b)
            assert not np.array_equal(image_a, reference)
    finally:
        a.cleanup()
        b.cleanup()
