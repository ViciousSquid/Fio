"""TerrainTable parity: the dense terrain state makes the old decisions exactly.

:class:`engine.terrain_table.TerrainTable` replaced a ``dict`` of
``TerrainChunk`` records that the terrain walked in Python every frame. The
port is meant to be behaviour-preserving, so these tests drive the table and a
frozen copy of the old per-chunk logic (``tests/helpers/terrain_reference.py``)
through the same frames -- camera moving and turning, LOD bands being crossed,
sculpt edits, bounds shrinking -- and require identical decisions every frame:
which chunks are resident and in what order, which are drawn, and which are
rebuilt at what resolution.

It also pins the one intended change: collision and rendering now read the
*same* heightfield, and collision interpolates it over the triangles the
renderer draws, so the ground the player stands on is the ground on screen.
"""

import json
import math
import os
import sys

import numpy as np
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

glm = pytest.importorskip("glm")
pytest.importorskip("OpenGL")

from engine.terrain_table import TerrainTable  # noqa: E402
from tests.helpers.terrain_reference import (  # noqa: E402
    ReferenceChunkModel, reference_chunk_mesh)

RES = ReferenceChunkModel.LOD_RESOLUTIONS
DIST = ReferenceChunkModel.LOD_DISTANCES_SQ
HYST = ReferenceChunkModel.LOD_HYSTERESIS_FRAMES
MAX_UPDATES = 2


def planes_for(eye, yaw_deg, far):
    from engine.renderer import Renderer
    yaw = math.radians(yaw_deg)
    target = (eye[0] + math.sin(yaw) * 100.0, eye[1] - 20.0,
              eye[2] - math.cos(yaw) * 100.0)
    projection = glm.perspective(glm.radians(70.0), 16 / 9, 1.0, far)
    view = glm.lookAt(glm.vec3(*eye), glm.vec3(*target), glm.vec3(0, 1, 0))
    return Renderer._frustum_planes(projection * view)


def fake_heights(world_x, world_z, res, size):
    """A cheap deterministic grid, identical for both pipelines.

    Bordered, as the table stores it: one extra sample beyond each edge.
    """
    g = np.arange(-1, res + 2, dtype=np.float64) * (size / res)
    x = world_x + g[:, None]
    z = world_z + g[None, :]
    return (40.0 * np.sin(x * 0.003) * np.cos(z * 0.002) + 0.01 * x).astype(np.float32)


class TablePipeline:
    """The table driven the way Terrain.update_and_render drives it."""

    def __init__(self, ref):
        self.ref = ref
        self.t = TerrainTable()

    def bounds(self):
        r = self.ref
        return (r.min_chunk_x, r.max_chunk_x, r.min_chunk_z, r.max_chunk_z)

    def residency(self, cam_x, cam_z):
        r = self.ref
        if r.streaming:
            self.t.stream(cam_x, cam_z, r.chunk_size, r.stream_radius,
                          r.stream_evict_padding, self.bounds(),
                          r.offset_x, r.offset_z)
        else:
            self.t.ensure_bounds(self.bounds(), r.chunk_size, r.offset_x, r.offset_z)

    def frame(self, cam_x, cam_z, planes):
        t = self.t
        slots = t.live_slots()
        vis = t.visible(slots, planes)
        dist = t.nearest_dist_sq(slots, cam_x, cam_z)
        q_slots, q_res = t.schedule(slots, dist, vis, self.ref.near_detail_radius(),
                                    RES, DIST, HYST)
        drawn = slots[vis & t.built[slots]]
        key = lambda s: (int(t.coord[s, 0]), int(t.coord[s, 1]))
        return ([key(s) for s in drawn],
                [(key(s), int(r)) for s, r in zip(q_slots, q_res)])

    def build(self, key, res):
        t = self.t
        slot = t.slot_of_coord[key]
        h = fake_heights(float(t.world[slot, 0]), float(t.world[slot, 1]),
                         res, float(t.size[slot]))
        t.store(slot, res, RES.index(res), h)
        return h[1:-1, 1:-1]            # the drawn grid, all the old model saw


def run_both(ref, cameras, events=None):
    """Drive both pipelines through *cameras*; assert every frame matches."""
    new = TablePipeline(ref)
    events = events or {}
    frames_with_builds = 0
    for n, (x, z, yaw, far) in enumerate(cameras):
        if n in events:
            events[n](ref, new)
        if ref.streaming:
            ref.stream_chunks(x, z)
        else:
            ref.ensure_bounds()
        new.residency(x, z)
        assert new.t.resident_coords() == list(ref.chunks.keys()), f"frame {n}: residency"

        planes = planes_for((x, 150.0, z), yaw, far)
        ref_drawn, ref_queue = ref.frame(x, z, planes)
        new_drawn, new_queue = new.frame(x, z, planes)
        assert new_drawn == ref_drawn, f"frame {n}: drawn set"
        assert new_queue == ref_queue, f"frame {n}: build queue"

        for key, res in ref_queue[:MAX_UPDATES]:
            h = new.build(key, res)
            ref.build(key, res, h)
            frames_with_builds += 1
    return new, frames_with_builds


def test_streaming_residency_visibility_and_queue_match_the_old_loops():
    ref = ReferenceChunkModel(bounds=(-100000, 100000, -100000, 100000))
    ref.streaming = True
    ref.stream_radius = 1300.0
    ref.stream_evict_padding = 512.0
    cams = [(i * 37.0, -i * 23.0, (i * 11) % 360, 1500.0) for i in range(120)]
    cams += [(4400.0 + i * 180.0, -2760.0, 90.0, 1500.0) for i in range(40)]
    _, built = run_both(ref, cams)
    assert built > 50, "the scenario should exercise the build queue"


def test_lod_bands_and_hysteresis_match_the_old_loops():
    """Non-streaming, wide bounds: chunks cross the 4608/6144/8192 bands."""
    ref = ReferenceChunkModel(bounds=(-28, 28, -3, 3))
    cams = [(0.0, 0.0, 90.0, 20000.0)] * 1400          # build everything
    cams += [(i * 90.0, 0.0, 90.0, 20000.0) for i in range(80)]   # walk: LOD churn
    cams += [(7200.0, 0.0, 270.0, 20000.0)] * 40         # hold still: hysteresis ripens
    new, _ = run_both(ref, cams)
    lods = {int(v) for v in new.t.lod[new.t.live_slots()]}
    assert len(lods) >= 3, f"the walk should leave several LOD levels resident, got {lods}"


def test_sculpt_marking_and_a_bounds_prune_match_the_old_loops():
    def sculpt(ref, new):
        wx, wz, radius = 300.0, -200.0, 90.0
        for c in ref.chunks.values():            # the old _mark_sculpt_region_dirty
            cx = c.world_x + c.size / 2
            cz = c.world_z + c.size / 2
            half = c.size / 2 + radius
            if abs(cx - wx) < half and abs(cz - wz) < half:
                c.is_dirty = True
        new.t.mark_dirty_region(wx, wz, radius)

    def shrink(ref, new):
        ref.min_chunk_x, ref.max_chunk_x = -2, 1
        ref.remove_out_of_bounds()
        new.t.prune_out_of_bounds((-2, 1, ref.min_chunk_z, ref.max_chunk_z))

    ref = ReferenceChunkModel(bounds=(-4, 4, -4, 4))
    cams = [(10.0 * i, 5.0 * i, 30.0 * i, 3000.0) for i in range(60)]
    run_both(ref, cams, events={45: sculpt, 50: shrink})


# ---------------------------------------------------------------------------
# One heightfield: mesh and collision
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def terrain():
    import engine.terrain as T
    original = T.Terrain._init_shader
    T.Terrain._init_shader = lambda self: None      # no GL context needed
    try:
        t = T.Terrain()
    finally:
        T.Terrain._init_shader = original
    with open(os.path.join(ROOT, "maps", "BigWorld_streaming_test.json")) as f:
        t.from_dict(json.load(f)["terrain_data"])
    return t


@pytest.mark.parametrize("cx,cz,res", [
    (0, -2, 48),     # sculpted
    (5, 7, 32),
    (-3, 1, 16),
    (20, 20, 8),
])
def test_the_table_heights_are_the_ones_the_old_mesh_was_built_from(terrain, cx, cz, res):
    """Every vertex height of the frozen CPU mesh is a table height, bit for bit.

    The rest of each vertex (normal, colour, UV, smooth normal) is rebuilt on
    the GPU from these heights; that half of the parity is proven in
    tests/visual/test_terrain_heightfield_parity.py.
    """
    slot = terrain.table.ensure(cx, cz, terrain.chunk_size,
                                terrain.offset_x, terrain.offset_z)
    heights = terrain._chunk_heights(slot, res)
    terrain.table.store(slot, res, RES.index(res), heights)
    ref, lo, hi = reference_chunk_mesh(
        terrain, float(terrain.table.world[slot, 0]),
        float(terrain.table.world[slot, 1]), float(terrain.table.size[slot]), res)
    q = np.arange(res * res)
    i, k = q // res, q % res
    corners = ((0, 0), (1, 0), (0, 1), (1, 0), (1, 1), (0, 1))
    stored = terrain.table.heights[slot, 1:, 1:]      # past the border ring
    for c, (di, dk) in enumerate(corners):
        assert np.array_equal(ref[c::6, 1], stored[i + di, k + dk])
    assert terrain.table.min_y[slot] == lo and terrain.table.max_y[slot] == hi


def surface_height(mesh, res, x, z, world_x, world_z, size):
    """Height of the drawn triangle under (x, z), from the reference mesh."""
    step = size / res
    i = min(int((x - world_x) / step), res - 1)
    k = min(int((z - world_z) / step), res - 1)
    quad = mesh[(i * res + k) * 6:(i * res + k) * 6 + 6, 0:3].astype(np.float64)
    fx = (x - world_x) / step - i
    fz = (z - world_z) / step - k
    tri = quad[0:3] if fx + fz <= 1.0 else quad[3:6]
    # Barycentric solve in XZ, then interpolate Y.
    a, b, c = tri
    m = np.array([[b[0] - a[0], c[0] - a[0]], [b[2] - a[2], c[2] - a[2]]])
    u, v = np.linalg.solve(m, [x - a[0], z - a[2]])
    return a[1] + u * (b[1] - a[1]) + v * (c[1] - a[1])


@pytest.mark.parametrize("res", [48, 32])
def test_collision_stands_on_the_drawn_triangles(terrain, res):
    cx, cz = 0, -2                                    # the sculpted chunk
    t = terrain.table
    slot = t.ensure(cx, cz, terrain.chunk_size, terrain.offset_x, terrain.offset_z)
    heights = terrain._chunk_heights(slot, res)
    t.store(slot, res, RES.index(res), heights)
    wx, wz, size = float(t.world[slot, 0]), float(t.world[slot, 1]), float(t.size[slot])
    mesh, _, _ = reference_chunk_mesh(terrain, wx, wz, size, res)

    rng = np.random.default_rng(0)
    for x, z in zip(wx + rng.uniform(0, size - 1e-3, 400), wz + rng.uniform(0, size - 1e-3, 400)):
        assert terrain.get_height_at(x, z) == pytest.approx(
            surface_height(mesh, res, x, z, wx, wz, size), abs=1e-3)
    # At a grid vertex it is the stored height exactly.
    step = size / res
    assert terrain.get_height_at(wx + 3 * step, wz + 5 * step) == pytest.approx(
        float(heights[3 + 1, 5 + 1]), abs=1e-4)          # +1: the border ring


def test_collision_falls_back_to_the_height_function_until_a_chunk_is_built(terrain):
    t = terrain.table
    slot = t.ensure(40, 40, terrain.chunk_size, terrain.offset_x, terrain.offset_z)
    x = float(t.world[slot, 0]) + 17.0
    z = float(t.world[slot, 1]) + 91.0
    assert not t.built[slot]
    assert terrain.get_height_at(x, z) == terrain._get_height_scalar(x, z)


def test_a_row_being_written_is_never_read(terrain):
    t = terrain.table
    slot = t.ensure(41, 40, terrain.chunk_size, terrain.offset_x, terrain.offset_z)
    t.store(slot, 48, 0, terrain._chunk_heights(slot, 48))
    x = float(t.world[slot, 0]) + 10.0
    z = float(t.world[slot, 1]) + 10.0
    assert t.height_at(41, 40, x, z) is not None
    t.seq[slot] += 1                                  # a writer is mid-row
    assert t.height_at(41, 40, x, z) is None
    t.seq[slot] += 1
    assert t.height_at(41, 40, x, z) is not None
    t.release([slot])
    assert t.height_at(41, 40, x, z) is None


def test_a_resize_is_visible_to_readers():
    t = TerrainTable()
    g = t.generation
    for i in range(200):
        t.ensure(i, 0, 256.0, 0.0, 0.0)
    assert t.generation > g


def test_an_empty_table_allocates_nothing():
    t = TerrainTable()
    assert t.capacity == 0 and t.count == 0 and t.triangle_count() == 0
    assert t.heights.nbytes == 0
