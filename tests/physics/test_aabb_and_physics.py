"""Equivalence tests for the cached-AABB helper and the physics fast paths.

Each optimisation is checked against a reference implementation of the code it
replaced, so these assert *behaviour preservation*, not merely that the new code
runs.
"""

import math
import os
import random
import sys

import glm

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from engine.constants import AABB_RUNTIME_KEYS, brush_aabb_bounds  # noqa: E402
from engine.physics import SpatialGrid  # noqa: E402


def _brush(pos, size):
    return {"pos": list(pos), "size": list(size)}


def _reference_bounds(brush):
    """The exact expression brush_aabb_bounds replaced."""
    pos = glm.vec3(brush["pos"])
    size = glm.vec3(brush["size"])
    b_min = pos - size * 0.5
    b_max = pos + size * 0.5
    return (b_min.x, b_min.y, b_min.z, b_max.x, b_max.y, b_max.z)


def _reference_intersect_ray_aabb(origin, direction, box_min, box_max):
    """Copy of LogicThread.intersect_ray_aabb, for equivalence checking."""
    t_min = 0.0
    t_max = 10000.0
    for i in range(3):
        if abs(direction[i]) < 1e-6:
            if origin[i] < box_min[i] or origin[i] > box_max[i]:
                return False, 0
        else:
            inv_d = 1.0 / direction[i]
            t1 = (box_min[i] - origin[i]) * inv_d
            t2 = (box_max[i] - origin[i]) * inv_d
            t_near = min(t1, t2)
            t_far = max(t1, t2)
            t_min = max(t_min, t_near)
            t_max = min(t_max, t_far)
            if t_min > t_max:
                return False, 0
    return True, t_min


# ---------------------------------------------------------------------------
# AABB cache
# ---------------------------------------------------------------------------

def test_bounds_are_bit_identical_to_the_old_expression():
    random.seed(1234)
    for _ in range(500):
        b = _brush([random.uniform(-5000, 5000) for _ in range(3)],
                   [random.uniform(1, 900) for _ in range(3)])
        assert brush_aabb_bounds(b) == _reference_bounds(b)


def test_cache_is_populated_and_reused():
    b = _brush([10, 20, 30], [64, 64, 64])
    first = brush_aabb_bounds(b)
    assert "_aabb_sig" in b and "_aabb_bounds" in b
    second = brush_aabb_bounds(b)
    # Same tuple object back on the second call => the cache was hit.
    assert first is second


def test_cache_invalidates_when_pos_changes():
    """A mover/door writing a new pos must not see stale bounds."""
    b = _brush([0, 0, 0], [64, 64, 64])
    assert brush_aabb_bounds(b) == (-32, -32, -32, 32, 32, 32)
    b["pos"] = [100, 0, 0]
    assert brush_aabb_bounds(b) == (68, -32, -32, 132, 32, 32)
    assert brush_aabb_bounds(b) == _reference_bounds(b)


def test_cache_invalidates_when_size_changes():
    b = _brush([0, 0, 0], [64, 64, 64])
    brush_aabb_bounds(b)
    b["size"] = [128, 128, 128]
    assert brush_aabb_bounds(b) == (-64, -64, -64, 64, 64, 64)
    assert brush_aabb_bounds(b) == _reference_bounds(b)


def test_cache_invalidates_on_in_place_mutation_of_the_pos_list():
    """Doors assign a fresh list, but mutating in place must invalidate too."""
    b = _brush([0, 0, 0], [64, 64, 64])
    brush_aabb_bounds(b)
    b["pos"][0] = 500.0
    assert brush_aabb_bounds(b) == _reference_bounds(b)


def test_repeated_mover_sweep_tracks_position_every_step():
    b = _brush([0, 0, 0], [10, 10, 10])
    for step in range(200):
        b["pos"] = [float(step), 0.0, 0.0]
        assert brush_aabb_bounds(b) == _reference_bounds(b)


def test_runtime_keys_are_declared_for_stripping():
    b = _brush([1, 2, 3], [4, 5, 6])
    brush_aabb_bounds(b)
    private = [k for k in b if k.startswith("_")]
    assert set(private) == set(AABB_RUNTIME_KEYS)


def test_editor_state_strips_the_cache_keys():
    """editor_state must fold AABB_RUNTIME_KEYS into its strip set.

    Read as source rather than imported: editor.editor_state pulls in PyQt5,
    which is not available on a headless test runner.
    """
    here = os.path.dirname(__file__)
    src = open(os.path.join(here, "..", "..", "editor", "editor_state.py"),
               encoding="utf-8").read()
    assert "from engine.constants import AABB_RUNTIME_KEYS" in src
    assert "frozenset(AABB_RUNTIME_KEYS)" in src


# ---------------------------------------------------------------------------
# Spatial grid: single-cell fast path vs multi-cell path
# ---------------------------------------------------------------------------

def _populated_grid(cell_size=256.0, n=400, seed=7):
    rng = random.Random(seed)
    grid = SpatialGrid(cell_size=cell_size)
    brushes = [_brush([rng.uniform(-2000, 2000), rng.uniform(-500, 500),
                       rng.uniform(-2000, 2000)],
                      [rng.uniform(16, 200)] * 3)
               for _ in range(n)]
    grid.populate(brushes)
    return grid, brushes


def _reference_potential_colliders(grid, pmin, pmax):
    """Pre-optimisation gather: always the dedup path, no single-cell shortcut."""
    min_x = int(math.floor(pmin.x / grid.cell_size))
    max_x = int(math.floor(pmax.x / grid.cell_size))
    min_z = int(math.floor(pmin.z / grid.cell_size))
    max_z = int(math.floor(pmax.z / grid.cell_size))
    out, seen = [], set()
    for x in range(min_x, max_x + 1):
        for z in range(min_z, max_z + 1):
            cell = (x, z)
            if cell in grid.cells:
                for brush in grid.cells[cell]:
                    if id(brush) not in seen:
                        seen.add(id(brush))
                        out.append(brush)
    return out


def test_potential_colliders_match_reference_single_and_multi_cell():
    grid, _ = _populated_grid()
    rng = random.Random(11)
    saw_single = saw_multi = False
    for _ in range(300):
        cx, cz = rng.uniform(-2000, 2000), rng.uniform(-2000, 2000)
        # Alternate between a tiny box (one cell) and a wide one (many cells).
        half = rng.choice([4.0, 4.0, 600.0])
        pmin = glm.vec3(cx - half, -100, cz - half)
        pmax = glm.vec3(cx + half, 100, cz + half)

        single = (int(math.floor(pmin.x / grid.cell_size)) ==
                  int(math.floor(pmax.x / grid.cell_size)) and
                  int(math.floor(pmin.z / grid.cell_size)) ==
                  int(math.floor(pmax.z / grid.cell_size)))
        saw_single |= single
        saw_multi |= not single

        got = grid.get_potential_colliders(pmin, pmax)
        exp = _reference_potential_colliders(grid, pmin, pmax)
        assert [id(b) for b in got] == [id(b) for b in exp]
    assert saw_single and saw_multi, "both code paths must be exercised"


def test_potential_colliders_returns_a_fresh_list_not_the_cell_bucket():
    """The single-cell fast path must copy, or a caller could mutate the grid."""
    grid = SpatialGrid(cell_size=256.0)
    b = _brush([10, 0, 10], [8, 8, 8])
    grid.populate([b])
    pmin, pmax = glm.vec3(8, -4, 8), glm.vec3(12, 4, 12)
    got = grid.get_potential_colliders(pmin, pmax)
    cell = (int(math.floor(10 / 256.0)), int(math.floor(10 / 256.0)))
    assert got is not grid.cells.get(cell)
    got.append("scribble")
    assert "scribble" not in grid.cells.get(cell, [])


def test_nearby_brushes_match_reference():
    grid, _ = _populated_grid()
    rng = random.Random(13)
    for _ in range(200):
        x, z = rng.uniform(-2000, 2000), rng.uniform(-2000, 2000)
        radius = rng.choice([0.0, 0.0, 400.0])
        got = grid.get_nearby_brushes(x, z, radius)
        # Reference: dedup gather over the same cell span.
        min_cx = int(math.floor((x - radius) / grid.cell_size))
        max_cx = int(math.floor((x + radius) / grid.cell_size))
        min_cz = int(math.floor((z - radius) / grid.cell_size))
        max_cz = int(math.floor((z + radius) / grid.cell_size))
        exp, seen = [], set()
        for cx in range(min_cx, max_cx + 1):
            for cz in range(min_cz, max_cz + 1):
                for brush in grid.cells.get((cx, cz), ()):
                    if id(brush) not in seen:
                        seen.add(id(brush))
                        exp.append(brush)
        assert [id(b) for b in got] == [id(b) for b in exp]


def test_overlaps_wall_matches_reference():
    grid, _ = _populated_grid()
    rng = random.Random(17)
    results = []
    for _ in range(400):
        mx, my, mz = (rng.uniform(-2000, 2000), rng.uniform(-300, 300),
                      rng.uniform(-2000, 2000))
        margin = rng.choice([8.0, 8.0, 400.0])
        got = grid.overlaps_wall(mx, my, mz, margin)

        # Reference: brute force over every brush in the grid.
        m_xmin, m_xmax = mx - margin, mx + margin
        m_ymin, m_ymax = my, my + 128.0
        m_zmin, m_zmax = mz - margin, mz + margin
        min_cx = int(math.floor(m_xmin / grid.cell_size))
        max_cx = int(math.floor(m_xmax / grid.cell_size))
        min_cz = int(math.floor(m_zmin / grid.cell_size))
        max_cz = int(math.floor(m_zmax / grid.cell_size))
        exp = False
        for cx in range(min_cx, max_cx + 1):
            for cz in range(min_cz, max_cz + 1):
                for brush in grid.cells.get((cx, cz), ()):
                    pos, size = brush["pos"], brush["size"]
                    if (m_xmax > pos[0] - size[0] * 0.5 and
                            m_xmin < pos[0] + size[0] * 0.5 and
                            m_ymax > pos[1] - size[1] * 0.5 and
                            m_ymin < pos[1] + size[1] * 0.5 and
                            m_zmax > pos[2] - size[2] * 0.5 and
                            m_zmin < pos[2] + size[2] * 0.5):
                        exp = True
        assert got == exp
        results.append(got)
    assert any(results) and not all(results), "test data must cover both outcomes"


# ---------------------------------------------------------------------------
# Line of sight: inlined slab test vs the original callback
# ---------------------------------------------------------------------------

def _reference_has_line_of_sight(grid, start, end):
    """Pre-optimisation implementation, using the callback and glm bounds."""
    ray_dir = end - start
    ray_len = glm.length(ray_dir)
    if ray_len < 0.001:
        return True
    ray_dir = ray_dir / ray_len
    steps = max(1, int(ray_len / grid.cell_size) + 2)
    seen, candidates = set(), []
    for i in range(steps + 1):
        t = min(i / float(steps), 1.0) * ray_len
        pt = start + ray_dir * t
        cx = int(math.floor(pt.x / grid.cell_size))
        cz = int(math.floor(pt.z / grid.cell_size))
        for dx in (-1, 0, 1):
            for dz in (-1, 0, 1):
                cell = (cx + dx, cz + dz)
                if cell in grid.cells:
                    for brush in grid.cells[cell]:
                        if id(brush) not in seen:
                            seen.add(id(brush))
                            candidates.append(brush)
    for brush in candidates:
        pos = glm.vec3(brush["pos"])
        size = glm.vec3(brush["size"])
        hit, dist = _reference_intersect_ray_aabb(
            start, ray_dir, pos - size * 0.5, pos + size * 0.5)
        if hit and dist < ray_len - 0.1:
            return False
    return True


def test_line_of_sight_matches_reference():
    grid, _ = _populated_grid(n=250, seed=23)
    rng = random.Random(29)
    blocked = clear = 0
    for _ in range(400):
        start = glm.vec3(rng.uniform(-1500, 1500), rng.uniform(-200, 200),
                         rng.uniform(-1500, 1500))
        end = glm.vec3(rng.uniform(-1500, 1500), rng.uniform(-200, 200),
                       rng.uniform(-1500, 1500))
        got = grid.has_line_of_sight(start, end, _reference_intersect_ray_aabb)
        exp = _reference_has_line_of_sight(grid, start, end)
        assert got == exp, (tuple(start), tuple(end))
        blocked += not got
        clear += got
    assert blocked and clear, "test data must cover blocked and clear rays"


def test_line_of_sight_axis_aligned_rays():
    """Axis-aligned rays exercise the near-zero-component branches of the slab test."""
    grid = SpatialGrid(cell_size=256.0)
    wall = _brush([200, 0, 0], [50, 400, 400])
    grid.populate([wall])
    for axis in range(3):
        s = glm.vec3(0, 0, 0)
        e = glm.vec3(0, 0, 0)
        e[axis] = 600.0
        got = grid.has_line_of_sight(s, e, _reference_intersect_ray_aabb)
        exp = _reference_has_line_of_sight(grid, s, e)
        assert got == exp
    # Straight down +X must be blocked by the wall.
    assert not grid.has_line_of_sight(glm.vec3(0, 0, 0), glm.vec3(600, 0, 0),
                                      _reference_intersect_ray_aabb)


def test_line_of_sight_degenerate_zero_length_ray():
    grid, _ = _populated_grid(n=20, seed=31)
    p = glm.vec3(0, 0, 0)
    assert grid.has_line_of_sight(p, p, _reference_intersect_ray_aabb) is True


def test_line_of_sight_uses_cached_bounds_and_tracks_moved_brushes():
    grid = SpatialGrid(cell_size=256.0)
    wall = _brush([200, 0, 0], [50, 400, 400])
    grid.populate([wall])
    start, end = glm.vec3(0, 0, 0), glm.vec3(600, 0, 0)
    assert not grid.has_line_of_sight(start, end, _reference_intersect_ray_aabb)
    # Move the wall out of the way; the cached bounds must refresh.
    wall["pos"] = [200, 5000, 0]
    grid.populate([wall])
    assert grid.has_line_of_sight(start, end, _reference_intersect_ray_aabb)
