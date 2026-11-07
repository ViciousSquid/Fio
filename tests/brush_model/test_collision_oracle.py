"""Stage 2c: collision follows the shape where it is -- against an oracle.

The oracle is independent of the collision code. From a brush's authored
shape (its planes, or a box's six faces) and its runtime transform (the
centre and spin its runtime source answers), it builds the effective world
shape as half-spaces -- turning normals and points with a unit quaternion,
not the engine's rotation matrices -- and answers with them: is a point
inside, how far is a sphere from the solid, where does a ray enter.

The engine is held to it at points sampled along a mover's whole path,
translating and spinning, for angled brushes and boxes:

* the player's depenetration, resolved in the shape's frame, lands the
  capsule where the same rule run on the oracle's world half-spaces does --
  and never moves a capsule the solid does not touch;
* a point inside the AABB of a moving ramp but outside the ramp itself is
  not pushed (before Stage 2c an angled mover collided as its box);
* every AABB consumer's bounds -- collision rows, the physics grid, the AI's
  cache -- contain the effective shape;
* the editor's face pick enters the shape where the oracle's ray does.
"""

import math

import numpy as np
import pytest

pytest.importorskip("glm", reason="the player is built on PyGLM vectors")

import glm                                            # noqa: E402

from engine import brush_geometry as bg               # noqa: E402
from engine import brush_placement as bp              # noqa: E402
from engine.constants import brush_aabb_bounds        # noqa: E402
from engine.physics import SpatialGrid                # noqa: E402
from engine.player import Player                      # noqa: E402
from tests.helpers import runtime                     # noqa: E402
from tests.helpers.worlds import angled_brush, box_brush  # noqa: E402


# ---------------------------------------------------------------------------
# The oracle
# ---------------------------------------------------------------------------

def _quat(axis, angle):
    axis = np.asarray(axis, dtype=np.float64)
    length = float(np.linalg.norm(axis))
    if not angle or length <= 0.001:
        return None
    half = math.radians(angle) / 2.0
    return axis / length * math.sin(half), math.cos(half)


def _turn(q, v):
    if q is None:
        return np.asarray(v, dtype=np.float64)
    u, w = q
    v = np.asarray(v, dtype=np.float64)
    t = 2.0 * np.cross(u, v)
    return v + w * t + np.cross(u, t)


class Oracle:
    """The effective world shape of *brush*: unit half-spaces ``n . w <= d``
    and the corners, from its authored shape and its runtime transform."""

    def __init__(self, brush):
        convex = bg.get_convex(brush) if bg.brush_has_geometry(brush) else None
        if convex is not None:
            authored = [(np.asarray(p['n'], float), float(p['d'])) for p in convex.planes]
            corners = np.asarray(convex.verts, dtype=np.float64)
            lo, hi = corners.min(axis=0), corners.max(axis=0)
        else:
            pos = np.asarray(brush['pos'], float)
            half = np.asarray(brush['size'], float) / 2.0
            authored = []
            for axis in range(3):
                for sign in (1.0, -1.0):
                    n = np.zeros(3)
                    n[axis] = sign
                    authored.append((n, float(n @ pos + half[axis])))
            corners = np.array([pos + half * np.array(s) for s in
                                [(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)]])
            lo, hi = pos - half, pos + half
        rest = (lo + hi) / 2.0
        rt = bp.runtime_of(brush)
        centre = np.asarray(rt[:3], float) if rt is not None else rest
        q = _quat(rt[3:6], rt[6]) if rt is not None else None
        self.planes = []
        for n, d in authored:
            length = float(np.linalg.norm(n))
            n, d = n / length, d / length
            point = n * d                                     # on the plane
            n_w = _turn(q, n)
            p_w = centre + _turn(q, point - rest)
            self.planes.append((n_w, float(n_w @ p_w)))
        self.corners = np.array([centre + _turn(q, c - rest) for c in corners])

    def distance(self, w):
        """Signed distance bound of a point: the largest plane distance
        (exact outside a face, a lower bound near edges; <= 0 inside)."""
        w = np.asarray(w, dtype=np.float64)
        return max(float(n @ w) - d for n, d in self.planes)

    def ray_entry(self, o, direction):
        """Where a ray enters the solid (distance), or None."""
        o = np.asarray(o, float)
        direction = np.asarray(direction, float)
        t_in, t_out = 0.0, math.inf
        for n, d in self.planes:
            denom = float(n @ direction)
            num = d - float(n @ o)
            if abs(denom) < 1e-12:
                if num < 0:
                    return None
                continue
            t = num / denom
            if denom < 0:
                t_in = max(t_in, t)
            else:
                t_out = min(t_out, t)
            if t_in > t_out:
                return None
        return t_in


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _ramp():
    return angled_brush("ramp", (0, 64, 0), (256, 128, 256),
                        clip_normal=(0.6, 1.0, 0.0), is_mover=True)


def _bar():
    return box_brush("bar", (0, 64, 0), (384, 32, 48), is_mover=True, rotate=True)


#: (offset, spin) samples along a mover's path: sliding, rising, spinning
#: about the vertical, tumbling about a skew axis, both at once.
PATH = ([((x, 0.0, 0.0), None) for x in (0.0, 37.5, 128.0, -300.0)]
        + [((0.0, y, 0.0), None) for y in (16.0, 96.0)]
        + [((0.0, 0.0, 0.0), ((0.0, 1.0, 0.0), a)) for a in (15.0, 45.0, 90.0, 133.0, 271.0)]
        + [((0.0, 0.0, 0.0), ((1.0, 2.0, 0.5), a)) for a in (30.0, 160.0)]
        + [((64.0, 32.0, -48.0), ((0.3, 1.0, -0.2), 77.0))])
PATH_IDS = ["off%s-spin%s" % (o, s[1] if s else 0) for o, s in PATH]


def _player_at(point):
    p = Player(float(point[0]), float(point[2]), angle=0.0)
    p.pos = glm.vec3(float(point[0]), float(point[1]), float(point[2]))
    p.velocity = glm.vec3(0.0)
    return p


def _spheres(player):
    offsets, radius = player._capsule_offsets()
    return [np.array([player.pos.x + o.x, player.pos.y + o.y, player.pos.z + o.z])
            for o in offsets], radius


def _oracle_depenetrate(oracle, centre, offsets, radius):
    """The player's depenetration rule -- relaxation passes, each pushing
    out of the deepest sphere/plane overlap -- run on the oracle's world
    half-spaces: what resolving in the shape's frame must reproduce."""
    centre = np.array(centre, dtype=np.float64)
    for _ in range(4):
        deepest, push = 1e-4, None
        for off in offsets:
            c = centre + off
            best_sd, best_n = -1e30, None
            separated = False
            for n, d in oracle.planes:
                sd = float(n @ c) - d
                if sd > radius:
                    separated = True
                    break
                if sd > best_sd:
                    best_sd, best_n = sd, n
            if separated or best_n is None:
                continue
            pen = radius - best_sd
            if pen > deepest:
                deepest, push = pen, best_n * pen
        if push is None:
            break
        centre = centre + push
    return centre


@pytest.mark.parametrize(("offset", "spin"), PATH, ids=PATH_IDS)
@pytest.mark.parametrize("make", [_ramp, _bar], ids=["ramp", "bar"])
def test_depenetration_is_the_world_shapes(make, offset, spin):
    brush = runtime.drive(make(), offset=offset, spin=spin)
    oracle = Oracle(brush)
    if bp.collision_shape(brush) is None:
        # A box carried without turning stays on the AABB fast path: its
        # derived bounds are its exact shape.
        lo, hi = oracle.corners.min(axis=0), oracle.corners.max(axis=0)
        assert np.allclose(bp.world_bounds(brush), np.concatenate((lo, hi)), atol=1e-9)
        return
    rng = np.random.default_rng(5)
    lo, hi = oracle.corners.min(axis=0), oracle.corners.max(axis=0)
    pushed = clear = 0
    for _ in range(80):
        player = _player_at(rng.uniform(lo - 40.0, hi + 40.0))
        offsets, radius = player._capsule_offsets()
        offsets = [np.array([o.x, o.y, o.z], dtype=np.float64) for o in offsets]
        start = np.array([player.pos.x, player.pos.y, player.pos.z], dtype=np.float64)
        want = _oracle_depenetrate(oracle, start, offsets, radius)
        player._resolve_mesh_capsule([brush])
        got = np.array([player.pos.x, player.pos.y, player.pos.z], dtype=np.float64)
        # The engine's position is float32 (glm), the oracle's float64.
        assert np.allclose(got, want, atol=2e-3), (start, got, want)
        if np.array_equal(want, start):
            assert np.array_equal(got, start)
            clear += 1
        else:
            pushed += 1
    assert pushed and clear


def test_a_moving_ramp_is_not_its_box():
    """A point inside the moving ramp's AABB but above its slope: before
    Stage 2c an angled mover collided as that box and pushed it out."""
    brush = runtime.drive(_ramp(), offset=(96.0, 48.0, 0.0))
    oracle = Oracle(brush)
    lo, hi = np.asarray(bp.world_bounds(brush)[:3]), np.asarray(bp.world_bounds(brush)[3:])
    rng = np.random.default_rng(9)
    found = 0
    for _ in range(400):
        point = rng.uniform(lo, hi)
        player = _player_at(point)
        spheres, radius = _spheres(player)
        if min(oracle.distance(c) for c in spheres) <= radius + 1.0:
            continue
        found += 1
        start = (player.pos.x, player.pos.y, player.pos.z)
        player._resolve_mesh_capsule([brush])
        assert (player.pos.x, player.pos.y, player.pos.z) == start
    assert found >= 5


@pytest.mark.parametrize(("offset", "spin"), PATH, ids=PATH_IDS)
@pytest.mark.parametrize("make", [_ramp, _bar], ids=["ramp", "bar"])
def test_every_aabb_consumer_contains_the_true_shape(make, offset, spin):
    brush = runtime.drive(make(), offset=offset, spin=spin)
    corners = Oracle(brush).corners
    lo, hi = corners.min(axis=0), corners.max(axis=0)
    tolerance = 1e-6 * max(1.0, float(np.abs(corners).max()))
    for name, b in (("resolver", bp.world_bounds(brush)),
                    ("ai cache (float32)", brush_aabb_bounds(brush))):
        slack = tolerance if name == "resolver" else 1e-3
        assert (np.asarray(b[:3]) <= lo + slack).all(), name
        assert (np.asarray(b[3:]) >= hi - slack).all(), name
    grid = SpatialGrid()
    grid.populate([brush])
    rows = grid._cell_rows
    assert (rows.lo[0] <= lo + tolerance).all() and (rows.hi[0] >= hi - tolerance).all()


@pytest.mark.parametrize(("offset", "spin"), PATH, ids=PATH_IDS)
def test_the_face_pick_enters_where_the_oracle_does(offset, spin):
    brush = runtime.drive(_ramp(), offset=offset, spin=spin)
    oracle = Oracle(brush)
    centre = oracle.corners.mean(axis=0)
    rng = np.random.default_rng(3)
    for _ in range(40):
        eye = centre + rng.normal(size=3) * 600.0
        target = centre + rng.uniform(-60.0, 60.0, 3)
        d = (target - eye) / np.linalg.norm(target - eye)
        want = oracle.ray_entry(eye, d)
        o, dd = bp.ray_to_shape(brush, eye, d)
        hit = bg.ray_convex_face(bp.shape_convex(brush), o, dd)
        if want is None:
            assert hit is None
        else:
            assert hit is not None
            assert hit[0] == pytest.approx(want, abs=1e-6 * max(1.0, want))


def test_a_static_box_keeps_the_aabb_fast_path():
    """Only a solid shape asks for its frame: a static box -- most of a
    level -- and a box a mover carries without turning stay on the AABB path
    (their derived bounds are their exact shape)."""
    still = box_brush("still", (0, 0, 0), (64, 64, 64))
    carried = runtime.drive(box_brush("lift", (0, 0, 0), (64, 64, 64)), offset=(0, 40, 0))
    assert bp.collision_shape(still) is None
    assert bp.collision_shape(carried) is None
    spun = runtime.drive(box_brush("spun", (0, 0, 0), (64, 64, 64)), spin=((0, 1, 0), 30.0))
    assert bp.collision_shape(spun) is not None


def test_a_moving_shapes_frame_is_built_once_per_move():
    """No per-query matrices: the frame is cached against the runtime
    transform object, which a source replaces only when the brush moves."""
    brush = runtime.drive(_ramp(), offset=(10, 0, 0), spin=((0, 1, 0), 30.0))
    first = bp.collision_shape(brush)[3]
    assert bp.collision_shape(brush)[3] is first
    runtime.drive(brush, offset=(20, 0, 0), spin=((0, 1, 0), 31.0))
    assert bp.collision_shape(brush)[3] is not first


def test_repeated_queries_build_no_rotation(monkeypatch):
    """Collision queries against a spinning mover reuse its frame: no
    rotation is built per query or per tick while it does not move, and one
    per move when it does."""
    brush = runtime.drive(_ramp(), offset=(32.0, 8.0, 0.0), spin=((0.2, 1.0, 0.1), 40.0))
    bp.collision_shape(brush)                          # the frame, built once
    built = {'n': 0}
    real = bp.rotation_matrix

    def counted(*args):
        built['n'] += 1
        return real(*args)

    monkeypatch.setattr(bp, 'rotation_matrix', counted)
    player = _player_at((20.0, 140.0, 0.0))
    for _ in range(25):
        player._resolve_mesh_capsule([brush])
    assert built['n'] == 0
    runtime.drive(brush, offset=(33.0, 8.0, 0.0), spin=((0.2, 1.0, 0.1), 41.0))
    for _ in range(25):
        player._resolve_mesh_capsule([brush])
    # One for the new frame (collision_shape also asks whether a spinning
    # box turns, which a convex brush never reaches).
    assert built['n'] == 1
