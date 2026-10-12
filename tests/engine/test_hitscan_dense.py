"""The dense hitscan and wall passes answer exactly as the walks they replace.

Above ``LogicCollision.DENSE_WALK_MIN_BRUSHES`` the hitscan's brush pass and
the small-swarm projectile wall test run as NumPy queries over
:class:`engine.logic_collision.CollisionRows`, and the hitscan's monster pass
is one batched ray/sphere test. Each must be the same test, in the same
arithmetic, as the per-brush or per-monster walk it replaces:

* ties go to the first brush (or monster) in list order;
* an axis the ray runs parallel to (``abs(d) < 1e-6``) misses when the origin
  is outside that slab;
* box corners are rounded to float32 as ``glm.vec3`` rounds them, and the slab
  test runs in float64 as the walk's Python floats did; the projectile wall
  test's boxes are float64 throughout;
* whether a brush blocks, and where a mover is, are read live.

The reference walks below are the code the dense passes replaced, verbatim
but for their surroundings. Whether pellets see each other's effects is a
separate contract: ``test_hitscan_snapshot.py``.
"""

import math

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="the production LogicThread uses Qt-backed editor entities")

import glm                                                # noqa: E402

from editor.editor_state import EditorState               # noqa: E402
from editor.things import Monster                         # noqa: E402
from engine.constants import is_solid_world_brush, is_water_brush  # noqa: E402
from engine.logic_collision import LogicCollision         # noqa: E402
from engine.logic_thread import LogicThread               # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing    # noqa: E402

pytestmark = pytest.mark.qt

INF = float("inf")


@pytest.fixture
def logic(request):
    made = []

    def _build(brushes=(), things=(), dense=True):
        state = EditorState()
        state.brushes = list(brushes)
        state.things = list(things)
        thread = LogicThread(ThreadedGameState(), state)
        made.append(thread)
        collision = thread.collision_runtime
        collision.DENSE_WALK_MIN_BRUSHES = 0 if dense else 1 << 62
        collision.refresh_collision_brushes_cache()
        return thread

    yield _build
    for thread in made:
        thread.stop()


# -- the walks the dense passes replaced ------------------------------------------

def _walk_brushes(combat, brushes, ray_origin, ray_dir, reach):
    closest_brush_hit = None
    closest_brush_dist = INF
    for brush in brushes:
        if (brush.get('is_trigger') or brush.get('hidden') or
                is_water_brush(brush) or brush.get('is_fog')):
            continue
        pos = glm.vec3(brush['pos'])
        size = glm.vec3(brush['size'])
        min_b = pos - size * 0.5
        max_b = pos + size * 0.5
        hit, dist = combat.intersect_ray_aabb(ray_origin, ray_dir, min_b, max_b, reach)
        if hit and dist < closest_brush_dist:
            closest_brush_dist = dist
            closest_brush_hit = ray_origin + ray_dir * dist
    return closest_brush_dist, closest_brush_hit


def _walk_monsters(things, ray_origin, ray_dir, reach, closest_brush_dist):
    closest_monster = None
    closest_monster_dist = INF
    for thing in things:
        if not isinstance(thing, Monster):
            continue
        if thing.properties.get('dead', False) or thing.properties.get('hidden', False):
            continue
        sprite_width = float(thing.properties.get('sprite_width', 64.0))
        sprite_height = float(thing.properties.get('sprite_height', 128.0))
        radius = max(sprite_width * 0.75, sprite_height * 0.4, 48.0)
        center = glm.vec3(thing.pos[0], thing.pos[1] + sprite_height * 0.45, thing.pos[2])
        oc = ray_origin - center
        a = glm.dot(ray_dir, ray_dir)
        b = 2.0 * glm.dot(oc, ray_dir)
        c = glm.dot(oc, oc) - radius * radius
        disc = b * b - 4 * a * c
        if disc >= 0:
            t = (-b - math.sqrt(disc)) / (2.0 * a)
            if 0 <= t <= reach and t < closest_monster_dist:
                if t < closest_brush_dist:
                    closest_monster_dist = t
                    closest_monster = thing
    return closest_monster


def _walk_walls(brushes, px, py, pz):
    for brush in brushes:
        if not is_solid_world_brush(brush):
            continue
        bp = brush['pos']
        bs = brush['size']
        if (
            bp[0] - bs[0] * 0.5 <= px <= bp[0] + bs[0] * 0.5
            and bp[1] - bs[1] * 0.5 <= py <= bp[1] + bs[1] * 0.5
            and bp[2] - bs[2] * 0.5 <= pz <= bp[2] + bs[2] * 0.5
        ):
            return True
    return False


def _same(dense, walked):
    (d_dist, d_point), (w_dist, w_point) = dense, walked
    assert d_dist == w_dist and math.copysign(1.0, d_dist) == math.copysign(1.0, w_dist)
    if w_point is None:
        assert d_point is None
    else:
        assert d_point is not None
        assert tuple(d_point) == tuple(w_point)


def _trace(logic, origin, direction, reach):
    combat = logic.combat_runtime
    rows = logic.collision_runtime.collision_rows()
    return combat._trace_brushes(rows, origin, direction, reach)


# -- randomised worlds --------------------------------------------------------------

def _random_world(rng, count=240):
    brushes = []
    for i in range(count):
        pos = rng.uniform(-1500.0, 1500.0, 3) + 0.1       # rarely float32-exact
        size = rng.uniform(1.0, 300.0, 3)
        props = {}
        roll = rng.integers(12)
        if roll == 0:
            props['hidden'] = True
        elif roll == 1:
            props['is_trigger'] = True
        elif roll == 2:
            props['is_water'] = True
        elif roll == 3:
            props['is_fog'] = True
        elif roll == 4:
            props['is_mover'] = True
        brushes.append(box_brush("b%d" % i, pos, size, **props))
    # Coincident pairs: a tie at every distance, sometimes with the first of
    # the pair not blocking.
    for i in range(20):
        twin = dict(brushes[i * 7])
        twin['name'] = twin['id'] = "twin%d" % i
        twin.pop('hidden', None)
        brushes.append(twin)
    return brushes


def _random_rays(rng, brushes, count=300):
    rays = []
    for i in range(count):
        if i % 3 == 0:                 # from inside a box
            origin = np.asarray(brushes[rng.integers(len(brushes))]['pos'])
        else:
            origin = rng.uniform(-1800.0, 1800.0, 3)
        d = rng.normal(size=3)
        kind = i % 5
        if kind == 1:
            d[rng.integers(3)] = 0.0                      # exactly parallel
        elif kind == 2:
            d[rng.integers(3)] = rng.choice([5e-7, -5e-7, 9.99e-7, 2e-6])
        d = glm.normalize(glm.vec3(*d))
        rays.append((glm.vec3(*origin), d))
    return rays


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_the_dense_brush_pass_is_the_walk_bit_for_bit(logic, seed):
    rng = np.random.default_rng(seed)
    brushes = _random_world(rng)
    thread = logic(brushes)
    combat = thread.combat_runtime
    for origin, direction in _random_rays(rng, brushes):
        for reach in (300.0, 4000.0):
            _same(_trace(thread, origin, direction, reach),
                  _walk_brushes(combat, brushes, origin, direction, reach))


def test_below_the_threshold_the_walk_runs(logic):
    thread = logic([box_brush("b", (0, 0, 100), (64, 64, 16))], dense=False)
    assert thread.collision_runtime.collision_rows() is None
    dist, point = _trace(thread, glm.vec3(0, 0, 0), glm.vec3(0, 0, 1), 1000.0)
    assert dist == 92.0 and point is not None


# -- the semantics, one at a time --------------------------------------------------------

def test_a_tie_goes_to_the_first_blocking_brush_in_cache_order(logic):
    first = box_brush("first", (0, 0, 100), (64, 64, 16))
    second = box_brush("second", (0, 0, 100), (64, 64, 16))
    thread = logic([first, second])
    rows = thread.collision_runtime.collision_rows()
    hit_rows, dists = rows.ray_hits(glm.vec3(0, 0, 0), glm.vec3(0, 0, 1), 1000.0)
    assert hit_rows.tolist() == [0, 1] and dists.tolist() == [92.0, 92.0]


def test_a_brush_hidden_after_the_rows_were_built_does_not_block(logic):
    near = box_brush("near", (0, 0, 100), (64, 64, 16))
    far = box_brush("far", (0, 0, 300), (64, 64, 16))
    thread = logic([near, far])
    origin, direction = glm.vec3(0, 0, 0), glm.vec3(0, 0, 1)
    assert _trace(thread, origin, direction, 1000.0)[0] == 92.0
    near['hidden'] = True              # a streaming layer parking its cell
    assert _trace(thread, origin, direction, 1000.0)[0] == 292.0


def test_a_mover_is_hit_where_it_is_now(logic):
    floor = [box_brush("f%d" % i, (5000 + i * 100, 0, 0), (10, 10, 10)) for i in range(3)]
    door = box_brush("door", (0, 0, 100), (64, 64, 16), is_mover=True)
    thread = logic([*floor, door])
    origin, direction = glm.vec3(0, 0, 0), glm.vec3(0, 0, 1)
    assert _trace(thread, origin, direction, 1000.0)[0] == 92.0
    door['pos'] = [0.0, 0.0, 200.0]    # the mover pass assigns a new list
    assert _trace(thread, origin, direction, 1000.0)[0] == 192.0
    door['pos'] = [0.0, 500.0, 200.0]
    assert _trace(thread, origin, direction, 1000.0) == (INF, None)


def test_the_rows_follow_the_cache_and_the_world_epoch(logic):
    wall = box_brush("wall", (0, 0, 100), (64, 64, 16))
    thread = logic([wall])
    collision = thread.collision_runtime
    first = collision.collision_rows()
    assert collision.collision_rows() is first
    wall['pos'] = [0.0, 0.0, 400.0]    # an editor edit during Play...
    thread.editor_state.mark_world_changed([wall])
    assert collision.collision_rows() is not first     # ...moves the epoch
    assert _trace(thread, glm.vec3(0, 0, 0), glm.vec3(0, 0, 1), 1000.0)[0] == 392.0
    collision._collision_brushes_cache = []            # what Stop assigns
    assert collision.collision_rows().brushes == []


@pytest.mark.parametrize("offset, hits", [(31.0, True), (32.0, True), (32.5, False)])
def test_a_parallel_axis_misses_when_the_origin_is_outside_its_slab(logic, offset, hits):
    # y component below 1e-6: the y slab is a containment test, not a division.
    wall = box_brush("wall", (0, 0, 100), (64, 64, 16))
    thread = logic([wall])
    combat = thread.combat_runtime
    origin = glm.vec3(0, offset, 0)
    direction = glm.vec3(0.0, 5e-7, 1.0)
    dense = _trace(thread, origin, direction, 1000.0)
    _same(dense, _walk_brushes(combat, [wall], origin, direction, 1000.0))
    assert (dense[1] is not None) == hits


def test_box_corners_are_rounded_to_float32_as_glm_rounds_them(logic):
    wall = box_brush("wall", (100.1, 0, 0), (0.3, 10, 10))
    thread = logic([wall])
    lo32 = np.float32(100.1) - np.float32(0.3) * np.float32(0.5)
    lo64 = 100.1 - 0.3 * 0.5
    assert float(lo32) != lo64         # the case only means something if they differ
    dist, _ = _trace(thread, glm.vec3(0, 0, 0), glm.vec3(1, 0, 0), 1000.0)
    assert dist == float(lo32)


def test_a_ray_starting_inside_a_box_hits_it_at_zero(logic):
    wall = box_brush("wall", (0, 0, 0), (64, 64, 64))
    thread = logic([wall])
    dist, point = _trace(thread, glm.vec3(0, 0, 0), glm.vec3(0, 0, 1), 1000.0)
    assert dist == 0.0 and math.copysign(1.0, dist) == 1.0
    assert tuple(point) == (0.0, 0.0, 0.0)


# -- the batched monster pass --------------------------------------------------------

def _random_monsters(rng, count=40):
    monsters = []
    for i in range(count):
        props = {"health": 100}
        if i % 4 == 0:
            props["sprite_width"] = float(rng.uniform(16.0, 200.0))
        if i % 5 == 0:
            props["sprite_height"] = str(int(rng.uniform(32, 300)))   # authored as text
        if i % 11 == 0:
            props["dead"] = True
        if i % 13 == 0:
            props["hidden"] = True
        monsters.append(make_thing(Monster, "m%d" % i,
                                   tuple(rng.uniform(-800.0, 800.0, 3) + 0.3), **props))
    # Two monsters on one spot: the first in things order is the one hit.
    twin = make_thing(Monster, "twin", tuple(monsters[1].pos), health=100)
    return monsters + [twin]


@pytest.mark.parametrize("seed", [0, 1])
def test_the_batched_monster_pass_picks_what_the_walk_picked(logic, seed):
    rng = np.random.default_rng(seed)
    monsters = _random_monsters(rng)
    thread = logic(things=monsters)
    combat = thread.combat_runtime
    snapshot, centres, radii = combat._hitscan_monster_snapshot()
    for _ in range(60):
        origin = glm.vec3(*rng.uniform(-900.0, 900.0, 3))
        directions = [glm.normalize(glm.vec3(*rng.normal(size=3))) for _ in range(6)]
        # Aim some rays straight at a monster, so there is something to pick.
        target = monsters[rng.integers(len(monsters))]
        directions.append(glm.normalize(glm.vec3(*target.pos) + glm.vec3(0, 50, 0) - origin))
        walls = [float(rng.choice([INF, rng.uniform(50.0, 1500.0)])) for _ in directions]
        reach = float(rng.choice([400.0, 3000.0]))
        pending = combat._trace_monsters(origin, directions, reach, walls, centres, radii)
        for direction, wall, rows in zip(directions, walls, pending):
            expected = _walk_monsters(monsters, origin, direction, reach, wall)
            got = snapshot[rows[0]] if rows else None
            assert got is expected


def test_a_monster_against_a_wall_at_its_exact_distance_is_decided_as_the_walk_decided(logic):
    # The distance itself must be the walk's to the last bit: a monster is hit
    # only strictly before the wall. A direction whose float32 dot with itself
    # is not exactly 1 makes the quadratic's ``a`` matter.
    monster = make_thing(Monster, "m", (13.7, 0.0, 431.3), health=100)
    thread = logic(things=[monster])
    combat = thread.combat_runtime
    snapshot, centres, radii = combat._hitscan_monster_snapshot()
    origin = glm.vec3(0.0, 57.6, 0.0)
    direction = next(d for d in (glm.normalize(glm.vec3(0.03 * k, 0.001 * k, 1.0))
                                 for k in range(1, 200))
                     if glm.dot(d, d) != 1.0)
    sprite_width = float(monster.properties.get('sprite_width', 64.0))
    sprite_height = float(monster.properties.get('sprite_height', 128.0))
    radius = max(sprite_width * 0.75, sprite_height * 0.4, 48.0)
    center = glm.vec3(monster.pos[0], monster.pos[1] + sprite_height * 0.45, monster.pos[2])
    oc = origin - center
    a = glm.dot(direction, direction)
    b = 2.0 * glm.dot(oc, direction)
    c = glm.dot(oc, oc) - radius * radius
    disc = b * b - 4 * a * c
    assert disc >= 0
    t = (-b - math.sqrt(disc)) / (2.0 * a)
    at_wall = combat._trace_monsters(origin, [direction], 1000.0, [t], centres, radii)
    past_wall = combat._trace_monsters(origin, [direction], 1000.0,
                                       [math.nextafter(t, INF)], centres, radii)
    assert at_wall == [[]]
    assert past_wall == [[0]]


def test_two_monsters_on_one_spot_the_first_in_things_order_is_hit(logic):
    first = make_thing(Monster, "first", (0, 0, 300), health=100)
    second = make_thing(Monster, "second", (0, 0, 300), health=100)
    thread = logic(things=[first, second])
    combat = thread.combat_runtime
    snapshot, centres, radii = combat._hitscan_monster_snapshot()
    pending = combat._trace_monsters(glm.vec3(0, 57.6, 0), [glm.vec3(0, 0, 1)],
                                     1000.0, [INF], centres, radii)
    assert [snapshot[row] for row in pending[0]] == [first, second]


# -- the small-swarm projectile wall test --------------------------------------------

def test_the_dense_wall_test_is_the_walk_including_box_faces(logic):
    rng = np.random.default_rng(5)
    brushes = _random_world(rng, count=200)
    thread = logic(brushes)
    rows = thread.collision_runtime.collision_rows()
    points = []
    for brush in brushes:
        p = np.asarray(brush['pos'])
        half = np.asarray(brush['size']) * 0.5
        points.append(rng.uniform(p - half - 5.0, p + half + 5.0))
        on_face = rng.uniform(p - half, p + half)
        axis = rng.integers(3)
        on_face[axis] = (p - half if rng.integers(2) else p + half)[axis]
        points.append(on_face)
    # Projectile positions are float32 in the store.
    for x, y, z in np.asarray(points, dtype=np.float32).tolist():
        dense = any(is_solid_world_brush(rows.brushes[row])
                    for row in rows.point_hits(x, y, z).tolist())
        assert dense == _walk_walls(brushes, x, y, z)


def test_a_small_swarm_dies_on_a_wall_and_passes_a_hidden_one(logic):
    from engine.player import Player
    walls = [box_brush("far%d" % i, (5000 + 100 * i, 0, 0), (10, 10, 10)) for i in range(3)]
    wall = box_brush("wall", (0, 0, 100), (512, 512, 16))
    thread = logic([*walls, wall])
    thread.player_runtime.player = Player(0.0, -5000.0)
    combat = thread.combat_runtime
    combat._add_monster_projectile((0.0, 0.0, 0.0), (0.0, 0.0, 600.0), 12345, 10.0, 5.0)
    for _ in range(20):
        combat._update_monster_projectiles(1.0 / 60.0)
    assert len(combat._monster_projectiles) == 0
    wall['hidden'] = True
    combat._add_monster_projectile((0.0, 0.0, 0.0), (0.0, 0.0, 600.0), 12345, 10.0, 5.0)
    for _ in range(20):
        combat._update_monster_projectiles(1.0 / 60.0)
    assert len(combat._monster_projectiles) == 1


def test_the_threshold_is_a_class_knob(logic):
    assert LogicCollision.DENSE_WALK_MIN_BRUSHES > 0
