"""
Simulation tiers: semantics, hysteresis, and the cost model.

The cost tests are the point of this file. A tier system that works but scans
the world each frame has not solved the problem it exists to solve, so the
scaling assertions here count work done rather than time taken and are written
so that they fail if the event-driven model is ever quietly replaced by a scan.
"""

import pytest

from engine.spatial import (SIM_TIER_KEY, TIER_ACTIVE, TIER_DISTANT,
                            TIER_DORMANT, TIER_NEAR, tier_of)
from plugins.bigworld.manager import BigWorldManager
from plugins.bigworld.runtime import BigWorldSession
from plugins.bigworld.tiers import TIER_HYSTERESIS, TierClassifier


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------

class FakeThing:
    """Minimal stand-in for an engine entity: a position and a property dict."""

    def __init__(self, x, z, type_name="monster", uuid=None, **props):
        self.pos = [float(x), 0.0, float(z)]
        self.properties = {"type": type_name, "id": uuid or f"t{x}_{z}"}
        self.properties.update(props)


class FakeLogic:
    """Stand-in for the streaming host (see ``runtime.StreamingHost``)."""

    def __init__(self, brushes=None, things=None, player=None):
        self.brushes = brushes or []
        self.things = things or []
        self.player = player


class FakePlayer:
    def __init__(self, x=0.0, z=0.0):
        self.pos = [float(x), 0.0, float(z)]


def brush(x, z, uuid, size=64.0):
    return {"id": uuid, "pos": [float(x), 0.0, float(z)],
            "size": [size, size, size]}


def grid_world(cells_each_way=6, per_cell=4, cell_size=512.0):
    """A world of entities spread evenly over a square block of cells."""
    things = []
    for cx in range(-cells_each_way, cells_each_way + 1):
        for cz in range(-cells_each_way, cells_each_way + 1):
            for i in range(per_cell):
                x = cx * cell_size + 64.0 + i * 32.0
                z = cz * cell_size + 64.0
                things.append(FakeThing(x, z, uuid=f"e{cx}_{cz}_{i}"))
    return things


def started_session(things, brushes=None, activation=2048.0,
                    deactivation=2304.0, near=1024.0, at=(0.0, 0.0)):
    logic = FakeLogic(brushes=brushes or [], things=things,
                      player=FakePlayer(*at))
    session = BigWorldSession(logic, activation_radius=activation,
                              deactivation_radius=deactivation,
                              sim_near_radius=near)
    session.start()
    return session


# ----------------------------------------------------------------------
# Tier semantics
# ----------------------------------------------------------------------

def test_absent_stamp_reads_near():
    """An unclassified world is a fully simulated world -- ordinary Fio."""
    assert tier_of(FakeThing(0, 0)) == TIER_NEAR
    assert tier_of(brush(0, 0, "b")) == TIER_NEAR
    assert tier_of(object()) == TIER_NEAR


def test_bands_are_near_then_active_then_distant():
    c = TierClassifier(near_radius=1000.0, active_radius=2000.0)
    assert c.tier_for_distance_sq(500.0 ** 2) == TIER_NEAR
    assert c.tier_for_distance_sq(1500.0 ** 2) == TIER_ACTIVE
    assert c.tier_for_distance_sq(5000.0 ** 2) == TIER_DISTANT


def test_near_radius_is_clamped_to_the_active_radius():
    """A map cannot ask for full simulation beyond what it streams (§13)."""
    c = TierClassifier(near_radius=9000.0, active_radius=2000.0)
    assert c.near_radius == 2000.0
    assert c.active_radius == 2000.0


# ----------------------------------------------------------------------
# Hysteresis (§12)
# ----------------------------------------------------------------------

def test_demotion_is_sticky_but_promotion_is_not():
    c = TierClassifier(near_radius=1000.0, active_radius=2000.0)
    just_outside = (1000.0 * (1.0 + TIER_HYSTERESIS * 0.5)) ** 2
    # Already NEAR: stays NEAR inside the band.
    assert c.tier_for_distance_sq(just_outside, previous=TIER_NEAR) == TIER_NEAR
    # Coming in from ACTIVE: does not promote until the plain radius.
    assert c.tier_for_distance_sq(just_outside, previous=TIER_ACTIVE) == TIER_ACTIVE
    assert c.tier_for_distance_sq(900.0 ** 2, previous=TIER_ACTIVE) == TIER_NEAR


def test_a_jump_across_both_bands_lands_where_distance_says():
    """Regression: hysteresis must not pin an object at a tier it long left."""
    c = TierClassifier(near_radius=1000.0, active_radius=2000.0)
    assert c.tier_for_distance_sq(9000.0 ** 2, previous=TIER_NEAR) == TIER_DISTANT


def test_loitering_on_a_boundary_does_not_flap():
    """Walk back and forth across a tier edge; count the transitions."""
    things = grid_world(cells_each_way=6)
    session = started_session(things, near=1024.0)
    watched = [t for t in things if abs(t.pos[0] - 1088.0) < 1e-6
               and abs(t.pos[2] - 64.0) < 1e-6]
    assert watched, "expected an entity just outside the NEAR radius"
    watched = watched[0]

    transitions = 0
    last = tier_of(watched)
    for step in range(24):
        x = 0.0 if step % 2 else 520.0     # oscillate across a cell boundary
        session.logic.player.pos = [x, 0.0, 0.0]
        session.tick()
        now = tier_of(watched)
        if now != last:
            transitions += 1
            last = now
    assert transitions <= 2, f"tier flapped {transitions} times on a boundary"


# ----------------------------------------------------------------------
# Agreement with residency (§13)
# ----------------------------------------------------------------------

def test_parked_entities_are_dormant():
    things = grid_world(cells_each_way=10)
    session = started_session(things, at=(0.0, 0.0))
    far = [t for t in things if abs(t.pos[0]) > 4000.0][0]
    assert not session.manager.is_thing_active(far)
    assert tier_of(far) == TIER_DORMANT


def test_no_resident_entity_is_dormant_and_no_dormant_one_is_resident():
    """The two systems may not disagree about what is live."""
    things = grid_world(cells_each_way=6)
    session = started_session(things)
    for pos in [(0.0, 0.0), (1500.0, 0.0), (3000.0, 1200.0), (-2600.0, -900.0)]:
        session.logic.player.pos = [pos[0], 0.0, pos[1]]
        session.tick()
        for t in things:
            resident = session.manager.is_thing_active(t)
            dormant = tier_of(t) == TIER_DORMANT
            assert resident != dormant, (
                f"{t.properties['id']} resident={resident} dormant={dormant} "
                f"at player {pos}")


def test_the_session_publishes_one_pair_of_radii():
    things = grid_world(cells_each_way=4)
    session = started_session(things, activation=4096.0, near=1024.0)
    assert session.logic.sim_active_radius == 4096.0
    assert session.logic.sim_near_radius == 1024.0
    assert session.tiers.active_radius == session.manager.activation_radius


def test_persistent_globals_are_never_demoted_by_distance():
    """§10: a world manager has no cell and must not fall dormant."""
    things = grid_world(cells_each_way=6)
    boss = FakeThing(0, 0, type_name="worldmanager", uuid="wm")
    things.append(boss)
    session = started_session(things)
    session.logic.player.pos = [12000.0, 0.0, 12000.0]
    session.tick()
    assert tier_of(boss) == TIER_NEAR


# ----------------------------------------------------------------------
# Identity and restoration
# ----------------------------------------------------------------------

def test_tiering_never_touches_uuids():
    things = grid_world(cells_each_way=5)
    before = [t.properties["id"] for t in things]
    session = started_session(things)
    for x in (0.0, 2000.0, -3000.0, 6000.0):
        session.logic.player.pos = [x, 0.0, 0.0]
        session.tick()
    assert [t.properties["id"] for t in things] == before


def test_play_stop_removes_every_tier_stamp():
    things = grid_world(cells_each_way=5)
    session = started_session(things)
    session.logic.player.pos = [2000.0, 0.0, 0.0]
    session.tick()
    assert any(SIM_TIER_KEY in t.properties for t in things)
    session.stop()
    assert not any(SIM_TIER_KEY in t.properties for t in things)


def test_authored_hidden_is_not_dormant():
    """Mapper intent and parked state are different things (§9)."""
    c = TierClassifier()
    hidden_trigger = FakeThing(0, 0, hidden=True)
    from plugins.bigworld.tiers import is_parked
    assert not is_parked(hidden_trigger)


# ----------------------------------------------------------------------
# Cost model (§20) -- the tests that keep the architecture honest
# ----------------------------------------------------------------------

def test_tiering_does_no_work_without_a_cell_crossing():
    """There is no per-frame tier path. Standing still must cost nothing."""
    things = grid_world(cells_each_way=6)
    session = started_session(things)

    calls = []
    real_update = session.tiers.update
    session.tiers.update = lambda *a, **k: (calls.append(1), real_update(*a, **k))[1]

    for _ in range(200):
        session.logic.player.pos = [10.0, 0.0, 10.0]   # same cell every frame
        session.tick()
    assert calls == [], "tier evaluation ran without a cell crossing"


@pytest.mark.perf
def test_evaluation_scales_with_the_active_set_not_the_world():
    """A 25x larger world must cost the same per crossing.

    This is the §22 invariant reduced to a number: the work a crossing does is
    bounded by the activation radius, so growing the world must not move it.
    """
    def cells_evaluated(cells_each_way):
        things = grid_world(cells_each_way=cells_each_way, per_cell=2)
        session = started_session(things)
        session.logic.player.pos = [520.0, 0.0, 0.0]
        before = session.manager._last_player_cell
        session.manager.update(session.logic.player.pos)
        assert session.manager._last_player_cell != before
        delta = session.tiers.update(session.manager, 520.0, 0.0)
        return delta.evaluated_cells, len(things)

    # Both worlds must be wide enough that the activation radius stays inside
    # them, or the small one's active set is clipped by the world's edge and the
    # comparison measures the fixture instead of the model.
    small_cells, small_objs = cells_evaluated(8)
    large_cells, large_objs = cells_evaluated(30)

    assert large_objs > small_objs * 10, "the large world must actually be larger"
    assert large_cells == small_cells, (
        f"evaluation grew with the world: {small_cells} -> {large_cells} cells "
        f"for {small_objs} -> {large_objs} objects")


@pytest.mark.perf
def test_a_crossing_restamps_a_ring_not_the_resident_set():
    """Only cells whose band actually changed pay for stamping."""
    things = grid_world(cells_each_way=10, per_cell=4)
    session = started_session(things)
    session.logic.player.pos = [520.0, 0.0, 0.0]
    session.tick()

    session.logic.player.pos = [1032.0, 0.0, 0.0]      # one cell further out
    before = session.manager._last_player_cell
    session.manager.update(session.logic.player.pos)
    assert session.manager._last_player_cell != before
    delta = session.tiers.update(session.manager, 1032.0, 0.0)

    resident = len(session.manager.active_things())
    assert len(delta.changes) < resident * 0.5, (
        f"restamped {len(delta.changes)} of {resident} resident entities -- "
        "that is a scan, not a ring")


# ----------------------------------------------------------------------
# Camera independence (§15)
# ----------------------------------------------------------------------

def test_camera_mode_cannot_move_the_resident_set_or_the_tiers():
    """Residency follows the player; visibility follows the camera.

    Fio switches between overhead and first person mid-play, and the two see
    very different amounts of world -- in first person the visible region
    reaches past the default activation radius. If the camera fed residency,
    toggling the mode would stream cells in and out and re-tier the world
    without the player having moved a unit. It must not.
    """
    things = grid_world(cells_each_way=10)
    session = started_session(things, at=(600.0, 0.0))
    session.tick()

    resident = {t.properties["id"] for t in things
                if session.manager.is_thing_active(t)}
    tiers = {t.properties["id"]: tier_of(t) for t in things}
    assert resident, "expected a non-empty resident set to compare against"

    # Everything a camera mode can change, changed -- except the player.
    session.logic.camera_mode = "Overhead"
    session.logic.overhead_height = 800.0
    session.logic.editor_camera = FakePlayer(9000.0, 9000.0)
    session.tick()
    session.logic.camera_mode = "First Person"
    session.tick()

    assert {t.properties["id"] for t in things
            if session.manager.is_thing_active(t)} == resident
    assert {t.properties["id"]: tier_of(t) for t in things} == tiers


def test_the_session_reads_the_player_not_the_camera():
    """The focus point is structural, not incidental."""
    things = grid_world(cells_each_way=8)
    session = started_session(things, at=(0.0, 0.0))
    session.tick()
    near_origin = session.tiers.cell_tier((0, 0))

    session.logic.editor_camera = FakePlayer(20000.0, 20000.0)
    session.tick()
    assert session.tiers.cell_tier((0, 0)) == near_origin

    session.logic.player.pos = [20000.0, 0.0, 20000.0]
    session.tick()
    assert session.tiers.cell_tier((0, 0)) == TIER_DORMANT
