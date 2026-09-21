"""
Simulation tiers: semantics, hysteresis, and the cost model.

The cost tests are the point of this file. A tier system that works but scans
the world each frame has not solved the problem it exists to solve, so the
scaling assertions here count work done rather than time taken and are written
so that they fail if the event-driven model is ever quietly replaced by a scan.
"""

import pytest

from engine.spatial import (SIM_TIER_KEY, TIER_ACTIVE, TIER_DISTANT,
                            TIER_DORMANT, TIER_NAMES, TIER_NEAR,
                            tier_of)
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


# ----------------------------------------------------------------------
# The whole transition matrix, driven by the player walking
# ----------------------------------------------------------------------
#
# Each band is checked by moving the player and reading the stamp back, not by
# calling the classifier's arithmetic: the stamp is the contract a consumer
# reads, and the arithmetic being right while the stamp never lands is a way of
# failing the tests above would not see.
#
# Tier boundaries land on cell edges, so the positions here are chosen a cell
# clear of each radius rather than a metre past it -- the model is conservative
# by up to one cell by design, and a test that pretended otherwise would be
# asserting a precision Fio deliberately does not offer.

def _walk_to(session, x, z=0.0):
    """Move the player to ``(x, z)`` and let the session re-tier."""
    session.logic.player.pos = [float(x), 0.0, float(z)]
    session.tick()
    return session


def test_every_tier_transition_a_walking_player_can_cause():
    """NEAR -> ACTIVE -> DISTANT -> DORMANT and all the way back.

    One entity, one continuous walk away from it and back again, asserting the
    stamp at every step.

    The radii are deliberately not the defaults.  ``DISTANT`` is the band of
    cells kept resident only by the manager's activation/deactivation
    hysteresis, and the tier model's own hysteresis stretches ``ACTIVE`` out to
    ``activation * (1 + TIER_HYSTERESIS)`` -- which with the stock 2048/2304
    pair lands exactly on the deactivation radius, leaving ``DISTANT`` no width
    at all.  A map wanting a distant band configures a deactivation radius
    beyond it, as this one does; see the test below, which pins the stock
    behaviour so the two cannot be confused for a bug in each other.
    """
    subject = FakeThing(0.0, 0.0, uuid="subject")
    session = started_session([subject], activation=2048.0,
                              deactivation=4096.0, near=1024.0, at=(0.0, 0.0))
    try:
        assert tier_of(subject) == TIER_NEAR, "an entity underfoot is not NEAR"

        # NEAR -> ACTIVE: past the near radius plus its hysteresis band.
        _walk_to(session, 2000.0)
        assert tier_of(subject) == TIER_ACTIVE, (
            "walking out of the full-simulation radius did not demote the "
            "entity to ACTIVE (tier=%s)" % TIER_NAMES[tier_of(subject)])

        # ACTIVE -> DISTANT: past the activation radius, still resident
        # because the cell is inside the deactivation radius.
        _walk_to(session, 3000.0)
        assert tier_of(subject) == TIER_DISTANT, (
            "an entity beyond the activation radius but still resident should "
            "be DISTANT (tier=%s)" % TIER_NAMES[tier_of(subject)])
        assert session.manager.active_cells, (
            "fixture: the cell should still be resident at this distance")

        # DISTANT -> DORMANT: the cell leaves the resident set entirely.
        _walk_to(session, 9000.0)
        assert tier_of(subject) == TIER_DORMANT, (
            "an entity whose cell was unloaded is still being simulated")
        assert not session.manager.active_cells

        # DORMANT -> ACTIVE: the cell comes back.  It cannot come back DISTANT
        # -- see the test below.
        _walk_to(session, 2000.0)
        assert tier_of(subject) == TIER_ACTIVE, (
            "an entity whose cell streamed back in did not leave DORMANT")

        # ACTIVE -> NEAR.
        _walk_to(session, 0.0)
        assert tier_of(subject) == TIER_NEAR
    finally:
        session.stop()


def test_a_cell_can_never_come_back_from_dormant_straight_into_distant():
    """Residency and the ACTIVE boundary are one number, so the edge cannot exist.

    A cell only re-enters the resident set when it is within the *activation*
    radius, and that same radius is the tier model's ACTIVE boundary -- so
    anything that has just woken up is, by construction, NEAR or ACTIVE.  This
    is §13 holding: the streamer and the tier model cannot disagree about how
    far out the world is live, because they are the same measurement.
    """
    subject = FakeThing(0.0, 0.0, uuid="subject")
    session = started_session([subject], activation=2048.0,
                              deactivation=4096.0, near=1024.0, at=(0.0, 0.0))
    try:
        seen = []
        for x in (9000.0, 4000.0, 3000.0, 2500.0, 2000.0, 1500.0, 0.0):
            _walk_to(session, x)
            seen.append(tier_of(subject))
        pairs = list(zip(seen, seen[1:]))
        assert (TIER_DORMANT, TIER_DISTANT) not in pairs, (
            "a dormant cell woke straight into DISTANT: residency and the "
            "ACTIVE boundary have drifted apart (walk was %s)"
            % ([TIER_NAMES[t] for t in seen],))
        assert TIER_NEAR in seen and TIER_DORMANT in seen, (
            "fixture: the walk should cover both ends")
    finally:
        session.stop()


def test_the_stock_radii_leave_the_distant_band_no_width():
    """Pinned, because it is surprising and it is not a bug.

    With the shipped 2048/2304 pair the tier model's ACTIVE band -- stretched
    by ``TIER_HYSTERESIS`` to mirror the manager's own hysteresis -- reaches
    exactly as far as residency does, so a resident cell is never DISTANT.  The
    model errs towards *more* simulation, which is the safe direction it
    documents; a map that wants a distant band widens its deactivation radius.
    """
    from plugins.bigworld.manager import (DEFAULT_ACTIVATION_RADIUS,
                                          DEFAULT_DEACTIVATION_RADIUS)

    active_outer = DEFAULT_ACTIVATION_RADIUS * (1.0 + TIER_HYSTERESIS)
    assert active_outer >= DEFAULT_DEACTIVATION_RADIUS, (
        "the stock radii now leave a DISTANT band %.1f units wide; that is a "
        "behaviour change, not a failure -- update this test and the "
        "'Full-simulation radius' help text together"
        % (DEFAULT_DEACTIVATION_RADIUS - active_outer))

    subject = FakeThing(0.0, 0.0, uuid="subject")
    session = started_session([subject], activation=DEFAULT_ACTIVATION_RADIUS,
                              deactivation=DEFAULT_DEACTIVATION_RADIUS,
                              near=1024.0, at=(0.0, 0.0))
    try:
        seen = set()
        for x in range(0, 6000, 128):
            _walk_to(session, float(x))
            seen.add(tier_of(subject))
        assert TIER_DISTANT not in seen, (
            "a resident entity reached DISTANT on the stock radii; the two "
            "hysteresis models have drifted apart")
        assert seen == {TIER_NEAR, TIER_ACTIVE, TIER_DORMANT}
    finally:
        session.stop()


def test_a_tier_transition_never_touches_identity_or_persistent_state():
    """The one thing a tier may never be allowed to cost.

    A tier is how live an object is, and nothing else.  Walk an entity through
    every band, including out of memory's reach and back, and its uuid and its
    gameplay state must read exactly as they did -- otherwise "dormant" has
    quietly become "reset".
    """
    subject = FakeThing(0.0, 0.0, uuid="subject", health=37, quest_flag="given")
    session = started_session([subject], activation=2048.0,
                              deactivation=2304.0, near=1024.0, at=(0.0, 0.0))
    identity = id(subject)
    try:
        subject.properties["health"] = 12          # a gameplay change, mid-play
        for x in (2000.0, 2300.0, 8000.0, 40000.0, 2300.0, 0.0):
            _walk_to(session, x)
            assert id(subject) is not None and id(subject) == identity, (
                "the entity object itself was replaced at x=%s" % x)
            assert subject.properties["id"] == "subject", (
                "the uuid changed crossing a tier boundary at x=%s" % x)
            assert subject.properties["health"] == 12, (
                "a gameplay value was reset by a tier transition at x=%s" % x)
            assert subject.properties["quest_flag"] == "given", (
                "a persistent flag was lost by a tier transition at x=%s" % x)
    finally:
        session.stop()


def test_repeated_activation_and_deactivation_settles_in_the_same_place():
    """Twenty round trips must leave the world exactly where one does.

    Parking stashes flags and un-parking puts them back; a stash written twice,
    or restored from the wrong side, drifts.  Drift over a long session is the
    kind of bug that only shows up in a save taken an hour in.
    """
    subject = FakeThing(0.0, 0.0, uuid="subject", hidden=True, disabled=False)
    session = started_session([subject], activation=2048.0,
                              deactivation=2304.0, near=1024.0, at=(0.0, 0.0))
    try:
        for _ in range(20):
            _walk_to(session, 40000.0)
            assert tier_of(subject) == TIER_DORMANT
            _walk_to(session, 0.0)
            assert tier_of(subject) == TIER_NEAR
        session.stop()
        assert subject.properties["hidden"] is True, (
            "twenty streaming round trips lost the entity's authored hidden "
            "flag (now %r)" % subject.properties.get("hidden"))
        assert subject.properties["disabled"] is False
        assert SIM_TIER_KEY not in subject.properties, (
            "a tier stamp survived play-stop")
    finally:
        session._started = False


def test_a_cell_that_leaves_the_resident_set_takes_its_tier_record_with_it():
    """No tier bookkeeping may outlive the cell it describes.

    The classifier keeps one entry per active cell.  A cell that unloads and is
    never revisited must not leave an entry behind, or a long walk across a big
    world grows the tier table without bound -- the world-sized cost the model
    exists to avoid, reintroduced through the back door.
    """
    session = started_session(grid_world(cells_each_way=8),
                              activation=1024.0, deactivation=1152.0,
                              near=512.0, at=(0.0, 0.0))
    try:
        peak = 0
        for x in range(0, 4000, 256):
            _walk_to(session, float(x))
            peak = max(peak, len(session.tiers._cell_tier))
        assert len(session.tiers._cell_tier) <= peak, (
            "the tier table kept growing as the player walked; it holds %d "
            "cells after a walk whose active set never exceeded %d"
            % (len(session.tiers._cell_tier), peak))
        assert set(session.tiers._cell_tier) <= session.manager.active_cells, (
            "the tier table describes cells that are no longer resident: %s"
            % (set(session.tiers._cell_tier) - session.manager.active_cells,))
    finally:
        session.stop()


def test_a_dormant_entity_that_is_restored_wakes_up_with_the_restored_state():
    """save -> tier transition -> the transition honours what was restored.

    The tier model must not be the thing that decides an object's gameplay
    state.  Restoring hidden/disabled onto a dormant entity writes through the
    engine's parking markers; waking it must then read *those*, not whatever
    the object happened to be parked with.
    """
    from engine.spatial import set_authored_flag

    subject = FakeThing(0.0, 0.0, uuid="subject")
    session = started_session([subject], activation=2048.0,
                              deactivation=2304.0, near=1024.0, at=(0.0, 0.0))
    try:
        _walk_to(session, 40000.0)
        assert tier_of(subject) == TIER_DORMANT

        # What a restore does to a dormant entity.
        set_authored_flag(subject, "hidden", True)
        set_authored_flag(subject, "disabled", True)

        _walk_to(session, 0.0)
        assert tier_of(subject) == TIER_NEAR, "the entity did not wake up"
        assert subject.properties["hidden"] is True, (
            "waking the entity discarded the restored 'hidden'")
        assert subject.properties["disabled"] is True, (
            "waking the entity discarded the restored 'disabled'")
    finally:
        session.stop()


# ----------------------------------------------------------------------
# The tier vocabulary is the engine's, not an application's
# ----------------------------------------------------------------------

def test_the_tier_names_live_in_the_engine_and_cost_nothing_to_read():
    """A system must be able to ask "how live is this?" without Big World.

    The four names and the reader are in ``engine.spatial``; the classifier
    that assigns them is the plugin's.  If the names moved into the plugin,
    every consumer would have to import the streaming runtime to ask a question
    an ordinary map answers with "NEAR, always".
    """
    import engine.spatial as spatial

    for name in ("TIER_NEAR", "TIER_ACTIVE", "TIER_DISTANT", "TIER_DORMANT",
                 "TIER_NAMES", "SIM_TIER_KEY", "tier_of"):
        assert hasattr(spatial, name), (
            "engine.spatial no longer defines %s; a consumer would have to "
            "import the Big World plugin to name a tier" % name)
    assert spatial.tier_of(object()) == spatial.TIER_NEAR, (
        "an object nothing has classified must read NEAR -- that is what makes "
        "a map with no streaming session behave as ordinary Fio")


def test_the_tiers_carry_no_application_specific_meaning():
    """Fio defines what a tier *is*, never what a game *does* at one.

    The classifier's entire output is an integer under one property key.  If it
    ever started switching AI off, freezing animation or skipping pickups, the
    engine would be making gameplay decisions for every game built on it.

    Scanned over *executable* code only: prose may explain where a default came
    from ("Fio's monster perception range"), and saying so is not the same as
    acting on it.
    """
    import ast
    import inspect

    import plugins.bigworld.tiers as tiers

    tree = ast.parse(inspect.getsource(tiers))
    for node in ast.walk(tree):
        # Drop docstrings: they are prose, and prose is allowed to explain.
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
                and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            del body[0]
    code = ast.unparse(tree).lower()

    for term in ("monster", "pickup", "weapon", "health", "monster_ai",
                 "door", "trigger", "inventory", "quest"):
        assert term not in code, (
            "the tier classifier's code mentions %r: tiers are generic engine "
            "infrastructure and must not know what a game does at each one"
            % term)


def test_the_classifier_only_ever_writes_the_one_property_key():
    """The stamp is the whole contract.

    Anything else the classifier wrote onto an object would be state a consumer
    could not have asked for and the session could not clean up -- and play-stop
    sweeps exactly one key.
    """
    subject = FakeThing(0.0, 0.0, uuid="subject", health=5)
    before = dict(subject.properties)
    session = started_session([subject], activation=2048.0,
                              deactivation=4096.0, near=1024.0, at=(0.0, 0.0))
    try:
        for x in (0.0, 2000.0, 3000.0, 9000.0, 0.0):
            _walk_to(session, x)
        added = set(subject.properties) - set(before)
        assert added <= {SIM_TIER_KEY, "bw_active", "hidden", "disabled",
                         "_bw_parked_hidden", "_bw_parked_disabled"}, (
            "tiering or parking wrote unexpected keys onto an entity: %s"
            % (added - {SIM_TIER_KEY},))
    finally:
        session.stop()
    assert SIM_TIER_KEY not in subject.properties
    assert subject.properties["health"] == 5
