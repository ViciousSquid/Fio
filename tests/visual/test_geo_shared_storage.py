"""Visual tier: shared convex storage stays correct and local while editing.

Angled brushes share vertex pages, a transform buffer and per-(texture, page)
index lists (``engine/renderer/core/geo_store.py``). Fio's editor *is* its
runtime, so that storage must hold up while the world changes, not only on a
still one. Each step below edits the 576-brush benchmark world through the
real pipeline -- EditorState -> LogicThread -> double-buffered publication ->
renderer -- and checks both oracles (the table rebuild-diff, and the GPU-state
draw oracle against the frozen old path) and exactly what the edit cost:

1. a warmed-up world uploads nothing;
2. reshaping one brush moves only that brush's allocation; every other
   mesh keeps its page range, slot and bytes;
3. moving one brush without changing its geometry (as a mover does) changes
   its transform slot and nothing else;
4. hide, unhide, reorder and undo keep parity, with index lists rebuilt only
   for the groups an edit touched (and reused when a view returns);
5. a deleted brush's range and slot are reused by the next brush, with no
   stale transform or index surviving;
6. batching off and on draw the same pixels.

A churn test then creates, resizes and deletes brushes of varied sizes for
hundreds of frames and checks that GPU memory plateaus.
"""

import copy
import random

import numpy as np
import pytest

from tests.helpers import gl as glh
from tests.helpers.draw_oracle import DrawCapture, compare_geo_draws, convex_programs
from tests.helpers.geo_reference import expected_geo_draws
from tests.helpers.legacy_view import for_frozen_reference


def _frozen(brushes, geometry_of, gl_texture, texture_pixel_size):
    """Arguments for the frozen reference, in the layout it reads."""
    copies, snapshots = for_frozen_reference(brushes, geometry_of)
    return copies, False, gl_texture, texture_pixel_size, snapshots
from tests.helpers.table_oracle import diff_rebuild

pytestmark = [pytest.mark.gl, pytest.mark.integration, pytest.mark.slow]

SIZE = 96


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as ctx:
        yield ctx
    glh.reset_texture_cache()


@pytest.fixture
def renderer(context):
    from tests.visual.test_geo_run_parity import distinct_texture_loader
    from engine.renderer.forward import ForwardRenderer
    made = ForwardRenderer(None)
    loader, cache = distinct_texture_loader(made)
    made.load_texture_callback = loader
    made.shadows_enabled = False
    made.geo_batching = 'elements'
    made.set_grid(4096, 64)
    made.set_sprite_textures({})
    try:
        yield made
    finally:
        import OpenGL.GL as gl
        if cache:
            gl.glDeleteTextures(sorted(set(cache.values())))
        made.cleanup()


class Session:
    """The editor pipeline, one published frame at a time."""

    def __init__(self, renderer, context, brushes):
        from editor.editor_state import EditorState
        from engine.logic_thread import LogicThread
        from engine.threaded_game_state import ThreadedGameState
        self.renderer, self.context = renderer, context
        self.state = EditorState()
        self.state.brushes = brushes
        self.state.mark_world_changed()
        self.game_state = ThreadedGameState()
        self.logic = LogicThread(self.game_state, self.state)
        self.logic.render_runtime.culling_enabled = False
        #: id(brush) -> the brush as it was when its geometry was resolved,
        #: for brushes moved without a geometry change (see geo_reference).
        self.geometry_of = {}

    def stop(self):
        self.logic.stop()

    def _published(self):
        self.logic._step_frame(self.logic.TICK_DURATION)
        self.logic._publish_frame()
        self.game_state.try_swap()
        return self.game_state.get_render_state()

    def _draw(self, frame, capture=None):
        import OpenGL.GL as gl
        config = glh.render_config(
            render_table=frame.render_table, render_refs=frame.render_refs,
            all_brush_slots=frame.all_brush_slots,
            visible_brush_slots=frame.visible_brush_slots,
            entity_table=frame.entity_table, entity_refs=frame.entity_refs,
            visible_thing_slots=frame.visible_thing_slots,
            thing_hidden=frame.thing_hidden, shadows_enabled=False)
        self.context.bind()
        gl.glClearColor(0.05, 0.05, 0.08, 1.0)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        self.renderer.render_scene(
            *glh.camera_matrices(eye=(0.0, 900.0, 1900.0), target=(0, 0, 0)),
            None, config, clear=False, brush_slots=frame.all_brush_slots)

    def frames(self, count=2):
        """Publish and draw *count* frames (2 = both render buffers)."""
        for _ in range(count):
            frame = self._published()
            try:
                self._draw(frame)
            finally:
                self.game_state.release_render_state(frame)
        return self.context.read_pixels()

    def check(self, step):
        """Both oracles on both buffers; returns the last frame's pixels."""
        renderer = self.renderer

        def gl_texture(name):
            return (renderer.texture_manager.get(renderer._tex_cache_path(name))
                    or renderer.load_texture_callback(name, 'textures'))

        for _ in range(2):
            frame = self._published()
            try:
                assert diff_rebuild(frame) == [], step
                with DrawCapture(program=convex_programs(renderer),
                                 skip_buffers=[renderer._cube_vbo]) as cap:
                    self._draw(frame)
            finally:
                self.game_state.release_render_state(frame)
            shown = [b for b in self.state.brushes if not b.get('hidden')]
            expected = expected_geo_draws(*_frozen(shown, self.geometry_of, gl_texture,
                                                   renderer._texture_pixel_size))
            assert expected, step
            problems = compare_geo_draws(expected, cap.draws)
            assert problems == [], "%s:\n%s" % (step, "\n".join(problems))
            assert cap.primitives == sum(d['count'] // 3 for d in expected) + 12, step
        return self.context.read_pixels()


def _uploads(renderer):
    return dict(renderer._geo_store().uploads)


def _delta(before, after):
    return {k: after[k] - before[k] for k in after}


def _allocations(renderer):
    """mesh key -> (page, start, count, slot), for every cached mesh."""
    return {m.key: (m.page, m.start, m.count, m.slot)
            for m in renderer._geo_mesh_cache.values()}


def _page_bytes(renderer):
    import ctypes
    import OpenGL.GL as gl
    from engine.renderer.core import geo_store
    out = []
    for page in renderer._geo_store().pages:
        if page is None:
            out.append(None)
            continue
        size = page.capacity * geo_store.STRIDE
        data = (ctypes.c_ubyte * size)()
        gl.glBindBuffer(gl.GL_COPY_READ_BUFFER, page.vbo)
        gl.glGetBufferSubData(gl.GL_COPY_READ_BUFFER, 0, size, data)
        gl.glBindBuffer(gl.GL_COPY_READ_BUFFER, 0)
        out.append(np.frombuffer(bytes(data), dtype=np.uint8).copy())
    return out


def _index_lists(renderer):
    """(texture, page) -> tuple of (first, count) bytes per cached variant."""
    return {key: tuple((e[1].tobytes(), e[2].tobytes()) for e in variants)
            for key, variants in renderer._geo_store()._index_lists.items()}


def _world():
    from tools.bench_geo_passes import angled_world
    return angled_world(24)


def _cut(brush, normal):
    """Clip *brush* by a plane through a point a little above its centre."""
    from engine import brush_geometry as bg
    n = np.asarray(normal, dtype=float)
    n /= np.linalg.norm(n)
    point = np.asarray(brush['pos'], dtype=float) + n * (0.15 * min(brush['size']))
    return bg.clip_brush(brush, n, float(n @ point))


def _mesh_of(renderer, brush):
    """The cached mesh a table would resolve *brush* to (by its surface key)."""
    from engine.render_table import RenderTable
    probe = RenderTable()
    probe.sync([brush], 1)
    record = probe.geometry_records[0]
    return renderer._geo_mesh_cache.get(record.surface)


def test_shared_storage_through_an_editing_session(renderer, context):
    import OpenGL.GL as gl
    from engine import brush_geometry as bg
    from engine.change_journal import moved
    from engine.spatial import set_authored_flag
    from engine.renderer.core import geo_store
    from tests.helpers import runtime

    world = _world()
    # Step 3 moves this brush as a mover does: since Stage 2a only a mover's
    # (or door's) displacement moves an angled brush -- its planes are its
    # placement otherwise.
    world[200]['is_mover'] = True
    session = Session(renderer, context, world)
    state = session.state
    try:
        # 1. Warm-up, then a still world uploads nothing at all.
        session.frames(4)
        session.check("warm-up")
        before = _uploads(renderer)
        session.frames(4)
        assert _delta(before, _uploads(renderer)) == {
            'geometry': 0, 'transforms': 0, 'indices': 0}

        # 2. Reshape one brush: only its allocation moves.
        target = state.brushes[100]
        old_mesh = _mesh_of(renderer, target)
        old_place = (old_mesh.page, old_mesh.start, old_mesh.count, old_mesh.slot)
        allocations = _allocations(renderer)
        pages = _page_bytes(renderer)
        lists = _index_lists(renderer)
        before = _uploads(renderer)
        assert _cut(target, (0.6, 1.0, -0.3))
        state.mark_world_changed([target])
        session.frames(2)
        new_mesh = _mesh_of(renderer, target)
        assert new_mesh is not None and new_mesh is not old_mesh
        after = _allocations(renderer)
        assert old_mesh.key not in after, "the replaced mesh was not released"
        unrelated = {k: v for k, v in allocations.items() if k != old_mesh.key}
        assert {k: after[k] for k in unrelated} == unrelated, (
            "a reshape moved another brush's allocation")
        cost = _delta(before, _uploads(renderer))
        assert cost['geometry'] == new_mesh.count * geo_store.STRIDE
        assert cost['transforms'] <= geo_store.TEXELS_PER_SLOT * 16
        # Only the reshaped brush's own (texture, page) groups were rebuilt.
        mesh_textures = set(renderer._geo_runs().texture[
            new_mesh.run_base:new_mesh.run_base + new_mesh.run_len].tolist())
        old_textures = set(renderer._geo_runs().texture[
            old_mesh.run_base:old_mesh.run_base + old_mesh.run_len].tolist())
        touched = {(t, p) for t in mesh_textures | old_textures
                   for p in (old_place[0], new_mesh.page)}
        changed = {key for key, v in _index_lists(renderer).items()
                   if lists.get(key) != v}
        assert changed <= touched, "an edit rebuilt unrelated index lists"
        assert cost['indices'] < sum(
            2 * int(np.frombuffer(c, np.int32).sum())
            for variants in _index_lists(renderer).values() for _, c in variants)
        # Every other mesh's bytes are untouched.
        stride = geo_store.STRIDE
        for index, (old_bytes, new_bytes) in enumerate(zip(pages, _page_bytes(renderer))):
            if old_bytes is None or new_bytes is None:
                continue
            mask = np.ones(len(old_bytes), dtype=bool)
            for page, start, count in ((old_place[0], old_place[1], old_place[2]),
                                       (new_mesh.page, new_mesh.start, new_mesh.count)):
                if page == index:
                    mask[start * stride:(start + count) * stride] = False
            assert np.array_equal(old_bytes[mask], new_bytes[:len(old_bytes)][mask]), (
                "page %d changed outside the reshaped brush's ranges" % index)
        session.check("reshaped one brush")

        # 3. Move one brush without changing its geometry, as a mover does.
        mover = state.brushes[200]
        session.geometry_of[id(mover)] = copy.deepcopy(mover)
        mesh = _mesh_of(renderer, mover)
        allocations = _allocations(renderer)
        mirror = renderer._geo_store().mirror.copy()
        lists = _index_lists(renderer)
        before = _uploads(renderer)
        # Its runtime transform (Stage 2b): the brush itself is not written.
        runtime.drive(mover, offset=(40.0, 0.0, -25.0))
        moved(mover)
        session.frames(2)
        cost = _delta(before, _uploads(renderer))
        assert _allocations(renderer) == allocations
        assert cost == {'geometry': 0, 'indices': 0,
                        'transforms': geo_store.TEXELS_PER_SLOT * 16}, cost
        now = renderer._geo_store().mirror
        differs = np.flatnonzero(np.any((now[:len(mirror)] != mirror).reshape(
            len(mirror), -1), axis=1))
        assert differs.tolist() == [mesh.slot]
        assert _index_lists(renderer) == lists
        session.check("moved one brush (transform only)")
        # Back home, as Stop puts every mover: its runtime transform is
        # dropped. Meshes are keyed by the shape, so a mover keeps its mesh
        # across record refreshes, as it always did.
        session.geometry_of.pop(id(mover))
        runtime.release(mover)
        moved(mover)
        session.frames(2)

        # 4a. Hide one brush: parity; its groups' index lists rebuilt.
        hidden = state.brushes[300]
        lists = _index_lists(renderer)
        set_authored_flag(hidden, 'hidden', True)
        session.check("hidden")
        # 4b. Unhide: the earlier lists are still cached -- nothing uploaded.
        set_authored_flag(hidden, 'hidden', False)
        session.frames(2)
        before = _uploads(renderer)
        session.frames(2)
        assert _delta(before, _uploads(renderer))['indices'] == 0
        # Every index list the frame had before the hide is cached again.
        now = _index_lists(renderer)
        for key, variants in lists.items():
            assert set(variants) <= set(now.get(key, ())), key
        session.check("unhidden")
        # 4c. Reorder the world: every handle renumbered, nothing rebuilt.
        builds = {'n': 0}
        build = renderer._build_geo_mesh

        def counted(*args, **kwargs):
            builds['n'] += 1
            return build(*args, **kwargs)

        renderer._build_geo_mesh = counted
        before = _uploads(renderer)
        allocations = _allocations(renderer)
        random.Random(5).shuffle(state.brushes)
        state.mark_world_changed()
        session.frames(2)
        assert builds['n'] == 0, "a reorder rebuilt meshes"
        assert _allocations(renderer) == allocations
        assert _delta(before, _uploads(renderer)) == {
            'geometry': 0, 'transforms': 0, 'indices': 0}
        session.check("reordered")
        # 4d. Undo and redo a move: parity holds, and although every brush is
        # restored as a new dict, nothing is rebuilt -- since Stage 2d a move
        # is a placement, so the restored brush has the same surface.
        state.save_state()
        edited = state.brushes[10]
        bg.translate_brush(edited, (16.0, 0.0, 0.0))
        state.mark_world_changed([edited])
        session.check("moved before undo")
        store = renderer._geo_store()
        capacity = [p.capacity if p is not None else 0 for p in store.pages]
        builds['n'] = 0
        before = _uploads(renderer)
        state.undo()
        session.frames(2)
        # Every brush comes back as a new dict; meshes are found by surface,
        # and the undo changed only a placement: no mesh is built again.
        assert builds['n'] == 0, "undo rebuilt %d meshes" % builds['n']
        assert _delta(before, _uploads(renderer))['geometry'] == 0
        assert [p.capacity if p is not None else 0 for p in store.pages] == capacity
        session.check("undo")
        builds['n'] = 0
        state.redo()
        session.frames(2)
        assert builds['n'] == 0, "redo rebuilt %d meshes" % builds['n']
        session.check("redo")
        renderer._build_geo_mesh = build

        # 5. Delete a brush; the next brush reuses its range and slot.
        victim = state.brushes[50]
        mesh = _mesh_of(renderer, victim)
        place = (mesh.page, mesh.start, mesh.count, mesh.slot)
        state.brushes.remove(victim)
        state.mark_world_changed()
        session.frames(2)
        assert mesh.key not in _allocations(renderer)
        reborn = copy.deepcopy(victim)
        reborn['id'] = 'reborn'
        for key in bg.GEO_RUNTIME_KEYS:
            reborn.pop(key, None)
        bg.translate_brush(reborn, (0.0, 0.0, 400.0))
        store = renderer._geo_store()
        free = [(i, start, length) for i, page in enumerate(store.pages)
                if page is not None for start, length in page.free]
        capacity = [p.capacity if p is not None else 0 for p in store.pages]
        state.brushes.append(reborn)
        state.mark_world_changed([reborn])
        session.frames(2)
        new = _mesh_of(renderer, reborn)
        assert new.slot == place[3], "the deleted brush's transform slot was not reused"
        assert any(p == new.page and s <= new.start and new.start + new.count <= s + n
                   for p, s, n in free), (
            "the new brush was not placed in freed space")
        assert [p.capacity if p is not None else 0 for p in store.pages] == capacity, (
            "placing one brush grew the vertex pages")
        session.check("deleted, then reused the allocation")

        # 6. Batching off and on: the same pixels.
        renderer.geo_batching = 'off'
        off = session.check("batching off")
        renderer.geo_batching = 'elements'
        on = session.check("batching on")
        assert np.array_equal(off, on)
        renderer.geo_batching = 'multidraw'
        multi = session.check("multi-draw")
        assert np.array_equal(off, multi)
        assert gl.glGetError() == gl.GL_NO_ERROR
    finally:
        session.stop()


def test_churn_reuses_storage_instead_of_growing(renderer, context):
    """Create, resize and delete brushes of varied sizes for many frames.

    Every frame changes the world; GPU storage must plateau: freed ranges and
    slots are reused, emptied pages released, and the cache holds exactly the
    meshes the tables still use.
    """
    from engine import brush_geometry as bg
    from engine.render_table import RenderTable
    from tools.bench_geo_passes import angled_world

    rng = random.Random(9)
    brushes = angled_world(6, seed=3)
    table = RenderTable()
    config = None
    epoch = 0
    store = renderer._geo_store()
    history = []
    serial = 0

    def make():
        nonlocal serial
        serial += 1
        size = [rng.uniform(40, 160), rng.uniform(30, 200), rng.uniform(40, 160)]
        brush = {"id": "churn-%d" % serial, "name": "churn",
                 "pos": [rng.uniform(-400, 400), size[1] / 2, rng.uniform(-400, 400)],
                 "size": size,
                 "textures": {t: rng.choice(("Dev/128.jpg", "Dev/256.jpg",
                                             "concrete02.png"))
                              for t in bg.FACE_TAGS}}
        for _ in range(rng.randint(1, 4)):        # 1-4 cuts: varied vertex counts
            n = [rng.uniform(-1, 1), rng.uniform(0.4, 1.4), rng.uniform(-1, 1)]
            p = [brush["pos"][0], brush["pos"][1] + size[1] * rng.uniform(0.05, 0.4),
                 brush["pos"][2]]
            length = sum(c * c for c in n) ** 0.5
            n = [c / length for c in n]
            bg.clip_brush(brush, n, sum(a * b for a, b in zip(n, p)))
        bg.box_to_geometry(brush)
        return brush

    for frame in range(300):
        epoch += 1
        dirty = []
        for _ in range(2):
            brushes.append(make())
            dirty.append(brushes[-1])
        victim = brushes.pop(rng.randrange(1, len(brushes)))
        target = brushes[rng.randrange(1, len(brushes))]
        lo = np.asarray(target["pos"]) - np.asarray(target["size"]) / 2
        hi = np.asarray(target["pos"]) + np.asarray(target["size"]) / 2
        scale = rng.uniform(0.7, 1.3)
        bg.fit_brush_to_bounds(target, lo, lo + (hi - lo) * scale)
        dirty.append(target)
        if len(brushes) > 60:
            for _ in range(2):
                brushes.pop(rng.randrange(1, len(brushes)))
        table.sync(brushes, epoch, dirty_objects={id(b) for b in dirty})
        config = glh.render_config(all_brushes=brushes, render_table=table,
                                   all_brush_slots=np.arange(table.count, dtype=np.int32),
                                   shadows_enabled=False)
        renderer.render_scene(*glh.camera_matrices(eye=(0, 700, 1100), target=(0, 0, 0)),
                              None, config, brush_slots=config["all_brush_slots"])
        live = [p for p in store.pages if p is not None]
        history.append((sum(p.capacity for p in live), sum(p.used for p in live),
                        store._next_slot, len(renderer._geo_mesh_cache),
                        len(renderer._geo_runs().first), victim is not None))

    print("\nchurn (frame: page capacity / live vertices / slots / cached meshes):")
    for index in range(0, len(history), 30):
        print("  %3d: %6d / %6d / %4d / %4d" % ((index,) + history[index][:4]))
    capacity = [h[0] for h in history]
    used = [h[1] for h in history]
    slots = [h[2] for h in history]
    cached = [h[3] for h in history]
    held = {id(m) for e in renderer._geo_by_table.values() for m in e.meshes if m is not None}
    # The cache holds exactly what tables hold (plus nothing leaked).
    assert cached[-1] == len(held)
    # Storage plateaus: the second half never needs more than the first half's
    # peak, and capacity stays within a small factor of what is live.
    half = len(history) // 2
    assert max(capacity[half:]) <= max(capacity[:half]), (
        "vertex pages kept growing under churn: %s" % capacity[::30])
    assert max(slots[half:]) <= max(slots[:half]) + 4, (
        "transform slots kept growing under churn: %s" % slots[::30])
    assert capacity[-1] <= 4 * max(used[half:]) + 2 * 4096
    # The face-run arena compacts: dead rows do not accumulate.
    arena = renderer._geo_runs()
    assert arena.used <= 3 * sum(m.run_len for m in renderer._geo_mesh_cache.values()) + 2048


@pytest.mark.parametrize("missing", (("geo_batched",), ("geo_batched", "geo_textured")),
                         ids=("no-batched-program", "no-convex-programs"))
def test_a_missing_convex_program_falls_back_with_parity(renderer, context, missing):
    """A driver that rejects the batched (or both convex) programs still draws
    every face, through the per-mesh path, exactly."""
    from tests.visual.test_geo_run_parity import (_gl_texture, _setup, _production,
                                                  angled_scene)
    brushes, things = angled_scene()
    for name in missing:
        program = renderer.shaders.pop(name)
        import OpenGL.GL as gl
        gl.glDeleteProgram(program)
    config, table, slots, camera = _setup(renderer, context, brushes, things, False)
    with DrawCapture(program=convex_programs(renderer),
                     skip_buffers=[renderer._cube_vbo]) as capture:
        _production(renderer, context, config, table, slots, camera, 'opaque')
    expected = expected_geo_draws(
        for_frozen_reference([brushes[int(s)] for s in slots])[0], False,
        _gl_texture(renderer), renderer._texture_pixel_size)
    import OpenGL.GL as gl
    assert compare_geo_draws(expected, capture.draws, cull=gl.GL_BACK) == []
    assert capture.calls > len({d['texture'] for d in expected}), (
        "the fallback should draw per mesh")


def test_page_placement_never_touches_neighbouring_ranges(context):
    """A full page, a new page, an oversize mesh and a released range: every
    other range's bytes are untouched, and oversize pages are not batched."""
    import ctypes
    import OpenGL.GL as gl
    from engine.renderer.core import geo_store
    store = geo_store.GeoStore()
    try:
        def mesh(count, value):
            vertices = np.full((count, 8), value, dtype=np.float32)
            faces = np.full((count, 5), -value, dtype=np.float32)
            return vertices, faces

        def read(page, start, count):
            size = count * geo_store.STRIDE
            data = (ctypes.c_ubyte * size)()
            gl.glBindBuffer(gl.GL_COPY_READ_BUFFER, store.pages[page].vbo)
            gl.glGetBufferSubData(gl.GL_COPY_READ_BUFFER, start * geo_store.STRIDE,
                                  size, data)
            gl.glBindBuffer(gl.GL_COPY_READ_BUFFER, 0)
            return bytes(data)

        first = geo_store.FIRST_PAGE_VERTICES
        placed = []
        for value, count in ((1.0, first - 300), (2.0, 300), (3.0, 500),
                             (4.0, geo_store.PAGE_VERTICES + 7), (5.0, 120)):
            vertices, faces = mesh(count, value)
            slot = store.take_slot()
            page, start = store.place(vertices, faces, slot)
            placed.append((page, start, count, read(page, start, count)))
        # The first page filled exactly; the next mesh opened a new page.
        assert placed[0][:2] == (0, 0) and placed[1][:2] == (0, first - 300)
        assert placed[2][0] != 0
        oversize = placed[3]
        assert store.pages[oversize[0]].capacity == oversize[2]
        assert not store.batchable([oversize[0]])[0]
        assert store.batchable([placed[0][0]])[0]
        # Release one range and fill it again: neighbours are untouched.
        store.release(placed[1][0], placed[1][1], placed[1][2])
        vertices, faces = mesh(200, 6.0)
        page, start = store.place(vertices, faces, store.take_slot())
        assert (page, start) == (0, first - 300)
        for page, start, count, data in placed[:1] + placed[2:]:
            assert read(page, start, count) == data
    finally:
        store.delete()


def test_teardown_releases_every_shared_gl_object(context):
    import OpenGL.GL as gl
    from engine.render_table import RenderTable
    from tools.bench_geo_passes import angled_world
    made = glh.make_renderer()
    brushes = angled_world(3)
    table = RenderTable()
    table.sync(brushes, 1)
    config = glh.render_config(all_brushes=brushes, render_table=table,
                               all_brush_slots=np.arange(table.count, dtype=np.int32),
                               shadows_enabled=False)
    made.render_scene(*glh.camera_matrices(eye=(0, 500, 700), target=(0, 0, 0)),
                      None, config, brush_slots=config['all_brush_slots'])
    store = made._geo_store()
    buffers = [p.vbo for p in store.pages if p is not None]
    buffers += [e[0] for variants in store._index_lists.values() for e in variants]
    buffers.append(store.transform_buffer)
    arrays = [p.vao for p in store.pages if p is not None]
    arrays += [m.vao for m in made._geo_mesh_cache.values()]
    assert buffers and arrays
    made.cleanup()
    assert not any(gl.glIsBuffer(b) for b in buffers), "a shared buffer outlived cleanup"
    assert not any(gl.glIsVertexArray(a) for a in arrays), "a VAO outlived cleanup"


def test_a_pos_write_moves_an_angled_brush_as_a_transform(renderer, context):
    """No mover involved: since Stage 2d ``pos`` is the authored placement of
    every brush and an angled brush's planes are in its local frame, so a
    ``pos``-only write -- a console ``setprop``, a plugin, the editor's own
    move -- carries the brush, drawn as a transform. (Before Stage 2a it was
    drawn as a transform until an undo re-anchored it, one rebuild: what was
    drawn depended on history; in Stage 2a it was a stale copy nothing drew
    from.) Through refreshes, undo and redo it is drawn where its placement
    puts its planes, and the mesh -- keyed by the shape -- is never
    rebuilt."""
    from editor.editor_state import EditorState  # noqa: F401  (Session uses it)
    from engine.change_journal import moved
    from tools.bench_geo_passes import angled_world
    session = Session(renderer, context, angled_world(4, seed=21))
    state = session.state
    builds = {'n': 0}
    build = renderer._build_geo_mesh

    def counted(*args, **kwargs):
        builds['n'] += 1
        return build(*args, **kwargs)

    try:
        session.frames(2)
        session.check("warm-up")
        renderer._build_geo_mesh = counted
        brush = state.brushes[5]
        session.geometry_of[id(brush)] = copy.deepcopy(brush)
        state.save_state()
        brush['pos'] = [brush['pos'][0] + 48.0, brush['pos'][1] + 8.0, brush['pos'][2]]
        moved(brush)
        session.check("pos write: drawn where it now places its planes")
        brush['colour'] = [0.1, 0.8, 0.3]
        state.mark_world_changed([brush])
        session.check("refreshed after the pos write: still placed there")
        assert builds['n'] == 0
        state.undo()
        session.geometry_of.clear()
        session.check("undo")
        state.redo()
        session.check("redo of the pos write")
        assert builds['n'] == 0, "the shape did not change: nothing may be rebuilt"
    finally:
        renderer._build_geo_mesh = build
        session.stop()


def test_a_thousand_moves_upload_no_geometry_and_rebuild_nothing(renderer, context,
                                                                 monkeypatch):
    """1,000 consecutive brush movements after the first frames -- editor
    translations (Stage 2d: a placement, the planes untouched) and movers'
    runtime transforms (Stage 2b), alternately, each one published and drawn
    through the real pipeline, each moved brush asked for its collision
    shape as the player would -- solve no convex brush, upload no geometry
    and no index list, allocate no mesh and build no collision shape: the
    GPU sees only transform slots. Both oracles hold at the end."""
    from engine import brush_geometry as bg
    from engine import brush_placement as bp
    from engine.change_journal import moved
    from engine.renderer.core import geo_store
    from tests.helpers import runtime

    world = _world()
    edited = [world[50], world[400]]
    movers = [world[120], world[333]]
    for brush in movers:
        brush['is_mover'] = True
    session = Session(renderer, context, world)
    try:
        session.frames(4)
        for brush in edited + movers:
            bp.collision_shape(brush)                 # prepared once
        allocations = _allocations(renderer)
        lists = _index_lists(renderer)
        before = _uploads(renderer)
        counts = {'solves': 0, 'shapes': 0}
        real_windings = bg.compute_windings
        real_shape = bg.ConvexGeometry.collision_shape

        def windings(*a, **k):
            counts['solves'] += 1
            return real_windings(*a, **k)

        def shape(geo):
            if geo._collision_shape is None:
                counts['shapes'] += 1
            return real_shape(geo)

        monkeypatch.setattr(bg, 'compute_windings', windings)
        monkeypatch.setattr(bg.ConvexGeometry, 'collision_shape', shape)
        for step in range(1000):
            sign = 1.0 if (step // 4) % 2 == 0 else -1.0
            if step % 2 == 0:
                brush = edited[(step // 2) % 2]
                bg.translate_brush(brush, (6.0 * sign, 0.0, -3.0 * sign))
                session.state.mark_world_changed([brush])
            else:
                brush = movers[(step // 2) % 2]
                runtime.drive(brush, offset=(0.0, 2.0 * (step % 40), 0.0),
                              spin=((0.0, 1.0, 0.0), float(step % 360)))
                moved(brush)
            session.frames(1)
            assert bp.collision_shape(brush) is not None
        cost = _delta(before, _uploads(renderer))
        assert counts == {'solves': 0, 'shapes': 0}, counts
        assert cost['geometry'] == 0 and cost['indices'] == 0, cost
        # One transform slot per moved brush per frame, at most.
        assert 0 < cost['transforms'] <= 1000 * geo_store.TEXELS_PER_SLOT * 16, cost
        assert _allocations(renderer) == allocations
        assert _index_lists(renderer) == lists
        session.check("after a thousand moves")
    finally:
        runtime.release()
        session.stop()
