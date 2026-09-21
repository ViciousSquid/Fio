"""The Prop domain's spatial index, and the contract that keeps it honest.

``prop.pos`` is the source of truth; ``PropSession``'s ``CellIndex`` is a
derived acceleration structure. That asymmetry is what makes the index safe to
have: it decides which Props are worth looking at, and live positions decide the
answer, so a cell that has gone stale can cost a Prop its place in a result but
can never put a wrong one in.

Keeping even that from happening is the synchronisation contract — every
subsystem that moves a Prop outside PropSession either calls ``moved(prop)`` or
exposes the moved set through a batch interface. There is one test here per
subsystem that does so, because a missing notification is invisible until a
player walks up to a Prop and cannot pick it up.
"""

import math
import random
from types import SimpleNamespace

import numpy as np
import pytest

from engine.prop_entity import Prop                     # noqa: E402
from engine.prop_runtime import PropSession             # noqa: E402
from engine.spatial import CELL_SIZE, cell_of_point, cells_of_points  # noqa: E402


def make_session(props=(), physics=None):
    logic = SimpleNamespace(things=list(props), player=None,
                            current_hud_message="", io_manager=None,
                            _physics_world=physics)
    session = PropSession(logic)
    session.start()
    return session


def prop_at(x, y, z, **props):
    return Prop(pos=[float(x), float(y), float(z)], properties=dict(props))


def brute_force_pick(session, eye, forward):
    """The linear scan the index replaced, kept as the reference answer."""
    best, best_d = None, None
    for prop in session.props:
        p = prop.properties
        if p.get("disabled") or not p.get("pickup_enabled", True):
            continue
        d = [float(prop.pos[i]) - eye[i] for i in range(3)]
        distance = math.sqrt(sum(v * v for v in d))
        if distance < 0.001 or distance > float(p.get("pickup_reach", 110.0)):
            continue
        if sum(forward[i] * (d[i] / distance) for i in range(3)) < 0.86:
            continue
        if best_d is None or distance < best_d:
            best, best_d = prop, distance
    return best


# ---------------------------------------------------------------------------
# The index answers the same question the scan did
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_the_indexed_pick_agrees_with_a_full_scan(seed):
    """Randomised equivalence — the index may narrow, never decide."""
    rng = random.Random(seed)
    props = [prop_at(rng.uniform(-2000, 2000), rng.uniform(-100, 100),
                     rng.uniform(-2000, 2000))
             for _ in range(300)]
    # Guarantee some live candidates near the query points.
    props += [prop_at(rng.uniform(-80, 80), rng.uniform(-40, 40),
                      rng.uniform(-80, 80)) for _ in range(40)]
    session = make_session(props)

    for _ in range(60):
        eye = (rng.uniform(-2000, 2000), rng.uniform(-60, 60), rng.uniform(-2000, 2000))
        angle = rng.uniform(0, 2 * math.pi)
        forward = (math.sin(angle), 0.0, math.cos(angle))
        session.held = None
        session._pick_in_view(eye, forward)
        assert session.held is brute_force_pick(session, eye, forward)


def test_a_prop_with_a_long_authored_reach_is_still_found():
    """The query radius covers the furthest-reaching Prop, not the default one."""
    far = prop_at(0, 0, 900, pickup_reach=1200.0)
    session = make_session([far])
    session._pick_in_view((0.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    assert session.held is far, (
        "a Prop reaching further than the default was filtered out by a radius "
        "derived from the default")


def test_the_query_examines_local_props_not_every_prop():
    """The point of the index: cost follows the neighbourhood, not the map."""
    near = [prop_at(40 * i, 0, 60) for i in range(3)]
    far = [prop_at(CELL_SIZE * 8 + i, 0, CELL_SIZE * 8) for i in range(500)]
    session = make_session(near + far)

    candidates = session.props_within(0.0, 0.0, session._max_reach)
    assert len(candidates) < 50, (
        "the broad phase returned %d of %d Props for a 110-unit query"
        % (len(candidates), len(session.props)))
    assert all(p in candidates for p in near)


# ---------------------------------------------------------------------------
# Registry changes keep the index in step
# ---------------------------------------------------------------------------

def test_adopting_and_releasing_file_and_unfile():
    prop = prop_at(0, 0, 60)
    logic = SimpleNamespace(things=[prop], player=None, current_hud_message="",
                            io_manager=None, _physics_world=None)
    session = PropSession(logic)
    session.start()
    assert session.props_within(0.0, 0.0, 200.0) == [prop]

    logic.things.remove(prop)
    session.rebuild()
    assert session.props_within(0.0, 0.0, 200.0) == []
    assert session._filed == {}, "a released Prop left an entry in the index"


def test_stop_empties_the_index():
    session = make_session([prop_at(0, 0, 60)])
    session.stop()
    assert session._cells.cells == {}
    assert session._filed == {}


# ---------------------------------------------------------------------------
# The synchronisation contract, one writer at a time
# ---------------------------------------------------------------------------

def test_a_stale_cell_can_only_lose_a_prop_never_invent_one():
    """The safety property the whole design rests on."""
    prop = prop_at(0, 0, 60)
    session = make_session([prop])
    prop.pos = [CELL_SIZE * 5, 0.0, CELL_SIZE * 5]      # moved, nobody told us

    session._pick_in_view((0.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    assert session.held is None, (
        "the index answered from its own stale copy instead of prop.pos")


def test_moved_brings_a_stale_cell_back_into_line():
    prop = prop_at(0, 0, 60)
    session = make_session([prop])
    prop.pos = [CELL_SIZE * 5, 0.0, CELL_SIZE * 5 + 60.0]
    session.moved(prop)

    assert session._filed[id(prop)] == cell_of_point(prop.pos[0], prop.pos[2])
    session._pick_in_view((CELL_SIZE * 5, 0.0, CELL_SIZE * 5), (0.0, 0.0, 1.0))
    assert session.held is prop


def test_moved_ignores_a_thing_that_is_not_a_registered_prop():
    """Callers must not have to ask whether what they moved was a Prop."""
    session = make_session([prop_at(0, 0, 60)])
    session.moved(SimpleNamespace(pos=[1.0, 2.0, 3.0], properties={}))   # no raise


def test_carrying_refiles_the_held_prop():
    prop = prop_at(0, 0, 60)
    session = make_session([prop])
    session._pick_in_view((0.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    assert session.held is prop

    far_eye = (CELL_SIZE * 4, 0.0, CELL_SIZE * 4)
    session._carry(far_eye, (0.0, 0.0, 1.0), use_pressed=False)
    assert session._filed[id(prop)] == cell_of_point(prop.pos[0], prop.pos[2]), (
        "the session moved the Prop it carries without re-filing it")


# ---------------------------------------------------------------------------
# The batch interface
# ---------------------------------------------------------------------------

class FakePhysics:
    """Stands in for PhysicsWorld's batch interface."""

    def __init__(self):
        self.changed = []
        self.calls = 0

    def entities_that_changed_cell(self):
        self.calls += 1
        out, self.changed = self.changed, []
        return out

    def set_rest_callback(self, entity, cb):
        pass

    def set_kinematic(self, entity, value):
        pass


def test_physics_cell_transitions_are_taken_as_a_batch():
    prop = prop_at(0, 0, 60)
    physics = FakePhysics()
    session = make_session([prop], physics=physics)

    prop.pos = [CELL_SIZE * 3, 0.0, CELL_SIZE * 3]
    physics.changed = [prop]
    session.sync_physics_positions()

    assert session._filed[id(prop)] == cell_of_point(prop.pos[0], prop.pos[2])
    assert physics.calls == 1, "the session asked more than once per sync"


def test_a_quiet_frame_refiles_nothing():
    prop = prop_at(0, 0, 60)
    physics = FakePhysics()
    session = make_session([prop], physics=physics)
    before = dict(session._filed)
    for _ in range(10):
        session.sync_physics_positions()
    assert session._filed == before


# ---------------------------------------------------------------------------
# PhysicsWorld's side of the batch interface
# ---------------------------------------------------------------------------

def _physics_world_with(positions):
    from engine.physics import PhysicsWorld, SpatialGrid
    grid = SpatialGrid()
    grid.populate([])
    world = PhysicsWorld(grid)
    brushes = []
    for i, pos in enumerate(positions):
        entity = prop_at(*pos, physics_enabled=True, mass=1.0)
        brushes.append({'pos': list(pos), 'size': [32.0, 32.0, 32.0],
                        '_physics_body': True, '_physics_entity': entity,
                        '_collision_mode': 'aabb'})
    world.rebuild(brushes)
    return world, [b['_physics_entity'] for b in brushes]


def test_physics_reports_only_bodies_that_left_their_cell():
    world, entities = _physics_world_with([(0, 0, 0), (100, 0, 100), (200, 0, 0)])
    assert world.entities_that_changed_cell() == (), "the baseline call reported movement"

    # Move one body a long way, another a short way inside its own cell.
    world._position[0] = (CELL_SIZE * 4, 0.0, CELL_SIZE * 4)
    world._position[1] = (110.0, 0.0, 105.0)
    changed = world.entities_that_changed_cell()
    assert changed == [entities[0]], (
        "expected only the body that crossed a cell boundary, got %r" % (changed,))
    assert world.entities_that_changed_cell() == (), "the same move was reported twice"


def test_the_batched_cell_maths_matches_the_scalar_convention():
    """One convention, two forms — checked rather than assumed."""
    rng = random.Random(11)
    xs = np.array([rng.uniform(-9000, 9000) for _ in range(500)])
    zs = np.array([rng.uniform(-9000, 9000) for _ in range(500)])
    cx, cz = cells_of_points(xs, zs)
    for i in range(xs.size):
        assert (int(cx[i]), int(cz[i])) == cell_of_point(float(xs[i]), float(zs[i]))


# ---------------------------------------------------------------------------
# The out-of-band writers: placement, reset, restore, streaming
# ---------------------------------------------------------------------------

def test_a_savegame_restore_refiles_the_props_it_teleports():
    from engine.savegame import _overlay_entities

    prop = prop_at(0, 0, 60, id="prop-1")
    session = make_session([prop])
    logic = session.logic
    logic._props = session

    destination = [CELL_SIZE * 6, 0.0, CELL_SIZE * 6 + 60.0]
    _overlay_entities(logic, {"things": [
        {"pos": list(destination), "properties": {"id": "prop-1"}},
    ]})

    assert list(prop.pos) == destination
    assert session._filed[id(prop)] == cell_of_point(destination[0], destination[2]), (
        "a restore moved a Prop without telling the Prop domain")
    session._pick_in_view((destination[0], 0.0, destination[2] - 60.0), (0.0, 0.0, 1.0))
    assert session.held is prop


def test_a_streaming_delta_refiles_the_props_it_places():
    """Big World overlays a saved delta when a cell comes back."""
    from plugins.bigworld.streaming import DiskStreamingSession, _THING

    prop = prop_at(0, 0, 60, id="prop-1")
    session = make_session([prop])
    logic = session.logic
    logic._props = session

    host = DiskStreamingSession.__new__(DiskStreamingSession)
    host.logic = logic
    host._live_by_id = {"prop-1": prop}
    destination = [CELL_SIZE * 7, 0.0, CELL_SIZE * 7]
    host._delta_by_uuid = {"prop-1": (None, {"pos": list(destination)})}

    host._apply_saved_delta(SimpleNamespace(objs=[(_THING, "prop-1")]))

    assert list(prop.pos) == destination
    assert session._filed[id(prop)] == cell_of_point(destination[0], destination[2]), (
        "a streamed-in cell placed a Prop without telling the Prop domain")


def test_tidy_reset_refiles_the_prop_it_moves():
    """Tidy moves Props directly, so Tidy notifies."""
    pytest.importorskip("PyQt5", reason="the Tidy runtime is an editor-tier plugin")
    from plugins.tidy.runtime import TidySession

    prop = prop_at(0, 0, 60, tidy_category="books")
    session = make_session([prop])
    logic = session.logic
    logic._props = session

    tidy = TidySession.__new__(TidySession)
    tidy.logic = logic
    tidy.objects = [prop]
    tidy.tidied = 0
    tidy._tidied_by_cat = {}
    tidy._tidied_ids = set()
    tidy._fill = {}
    tidy._full_fired = set()
    tidy._check_goals = lambda: None
    tidy._category = lambda obj: "books"

    # Somebody put it on a shelf a long way from home; Reset brings it back.
    prop.pos = [CELL_SIZE * 9, 0.0, CELL_SIZE * 9]
    session.moved(prop)
    tidy.reset_object(prop)

    assert list(prop.pos) == [0.0, 0.0, 60.0], "Reset did not restore the home position"
    assert session._filed[id(prop)] == cell_of_point(prop.pos[0], prop.pos[2]), (
        "Tidy reset a Prop without telling the Prop domain")
    session._pick_in_view((0.0, 0.0, 0.0), (0.0, 0.0, 1.0))
    assert session.held is prop
