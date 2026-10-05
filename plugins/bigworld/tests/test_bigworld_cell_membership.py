"""Cell membership through a whole streaming lifecycle.

A cell is not a container an object is put into; it is an answer to *where is
this object?*, recomputed from position and the shared 512-unit grid.  That
holds trivially for a brush, which never moves, and is the interesting case for
an entity, which does.

What is checked here is the invariant every other part of Big World rests on:
**every object is filed under exactly one answer, and that answer is where it
actually is.**  Filed twice and the reference counting that decides residency
drifts; filed nowhere and the object is lost from play with nothing to report
it; filed under a cell it has left and the streamer parks a monster standing in
front of the player, or simulates one nobody can reach.

Plain Python throughout, like the rest of the plugin's bookkeeping.
"""

import os
import sys

import pytest
pytest.importorskip("PyQt5", reason="Big World membership tests exercise the real LogicThread owner")

from editor.editor_state import EditorState
from editor.things import Thing as FioThing
from engine.logic_thread import LogicThread
from engine.threaded_game_state import ThreadedGameState


def make_thing(x, z, uuid, type_name="monster", **props):
    properties = {"type": type_name, "id": uuid}
    properties.update(props)
    return FioThing(pos=[float(x), 0.0, float(z)], properties=properties)


def make_logic(brushes=(), things=(), at=(0.0, 0.0)):
    state = EditorState()
    state.brushes = list(brushes)
    state.things = list(things)
    logic = LogicThread(ThreadedGameState(), state)
    logic.player_runtime.player.pos = [float(at[0]), 0.0, float(at[1])]
    logic.world_runtime.build_entity_caches()
    return logic


def brush(x, z, uuid, size=64.0):
    return {"id": uuid, "pos": [float(x), 0.0, float(z)],
            "size": [size, size, size]}


def session(things=(), brushes=(), activation=1024.0, deactivation=1152.0,
            near=512.0, at=(0.0, 0.0)):
    logic = make_logic(brushes=brushes, things=things, at=at)
    s = BigWorldSession(logic, activation_radius=activation,
                        deactivation_radius=deactivation, sim_near_radius=near)
    s.start()
    return s

def cells_holding(manager, thing):
    return sorted(c for c, cell in manager.cells.items() if thing in cell.things)


def walk(s, x, z=0.0, entities=()):
    """Move the player (and any entities travelling with it) and tick."""
    s.logic.player_runtime.player.pos = [float(x), 0.0, float(z)]
    for e in entities:
        e.pos = [float(x), 0.0, float(z)]
    s.tick()


def test_streaming_reads_player_from_logic_player_runtime():
    logic = make_logic(at=(123.0, 456.0))
    session = BigWorldSession(logic)
    assert session._player_pos() is logic.player_runtime.player.pos
    session.stop()


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------

def test_an_entity_is_filed_under_exactly_one_cell():
    """An entity is a point, so its cell is unambiguous — and singular.

    A brush is filed from every cell its footprint touches and reference
    counted; an entity must not be, or the counting has nothing to count.
    """
    mon = make_thing(100.0, 100.0, "mon")
    mgr = BigWorldManager()
    mgr.index_world([], [mon])
    assert cells_holding(mgr, mon) == [(0, 0)]
    assert sum(cell.things.count(mon) for cell in mgr.cells.values()) == 1


def test_a_brush_is_filed_from_every_cell_it_spans_and_stored_once():
    # Straddles x = 512, but sits well inside cell 0 in z (z spans 128..384).
    spanning = brush(512.0, 256.0, "span", size=256.0)
    mgr = BigWorldManager()
    mgr.index_world([spanning], [])
    holding = sorted(c for c, cell in mgr.cells.items() if spanning in cell.brushes)
    assert holding == [(0, 0), (1, 0)], (
        "a brush crossing a boundary must be referenced from both sides")
    assert all(cell.brushes.count(spanning) <= 1 for cell in mgr.cells.values()), (
        "the same brush was filed twice into one cell; reference counting "
        "would never reach zero and the brush would never park")
    assert len(mgr._brush_by_id) == 1, "the brush was duplicated, not referenced"


def test_re_indexing_a_world_leaves_nothing_of_the_previous_one():
    """``index_world`` is the rebuild, and a rebuild must not remember."""
    a, b = make_thing(100.0, 100.0, "a"), make_thing(5000.0, 0.0, "b")
    mgr = BigWorldManager()
    mgr.index_world([], [a, b])
    mgr.index_world([], [a])
    assert cells_holding(mgr, b) == [], "an entity survived a world rebuild"
    assert "b" not in mgr._thing_by_id
    assert "b" not in mgr._thing_cell, (
        "the per-entity cell record outlived the world it described")


# ---------------------------------------------------------------------------
# Movement: an entity that leaves the cell it was authored in
# ---------------------------------------------------------------------------

def test_an_entity_that_walks_is_filed_where_it_now_stands():
    """The defect this file exists for.

    A monster that chases the player out of its home cell used to keep the
    cell's membership.  When the home cell left the resident set the monster
    was parked — hidden and disabled — in the middle of a fight, standing right
    next to the player.
    """
    chaser = make_thing(0.0, 0.0, "chaser")
    s = session([chaser])
    try:
        assert cells_holding(s.manager, chaser) == [(0, 0)]

        for x in (600.0, 1200.0, 5000.0, 20000.0):
            walk(s, x, entities=[chaser])
            expected = cell_of_point(x, 0.0, CELL)
            assert cells_holding(s.manager, chaser) == [expected], (
                "an entity standing at x=%.0f is filed under %s, not %s"
                % (x, cells_holding(s.manager, chaser), [expected]))
            assert chaser.properties.get("hidden") is not True, (
                "the entity travelling with the player was parked at x=%.0f" % x)
            assert tier_of(chaser) == TIER_NEAR, (
                "an entity at the player's feet is not fully simulated (tier "
                "%s at x=%.0f)" % (tier_of(chaser), x))
    finally:
        s.stop()


def test_an_entity_that_stays_behind_still_parks():
    """The other half: re-filing must not make everything resident forever."""
    chaser, homebody = make_thing(0.0, 0.0, "chaser"), make_thing(0.0, 0.0, "homebody")
    s = session([chaser, homebody])
    try:
        walk(s, 20000.0, entities=[chaser])
        assert homebody.properties.get("hidden") is True, (
            "an entity left 20000 units behind is still resident")
        assert tier_of(homebody) == TIER_DORMANT
        assert chaser.properties.get("hidden") is not True
    finally:
        s.stop()


def test_a_mover_is_never_filed_under_two_cells_at_once():
    """Re-filing is a move, not a copy — over a long, winding walk."""
    chaser = make_thing(0.0, 0.0, "chaser")
    s = session([chaser])
    try:
        for step in range(40):
            x = (step % 13) * 700.0
            z = (step % 7) * 900.0
            walk(s, x, z, entities=[chaser])
            holding = cells_holding(s.manager, chaser)
            assert len(holding) == 1, (
                "after %d steps the entity is filed under %s"
                % (step + 1, holding))
            assert holding == [cell_of_point(x, z, CELL)]
        total = sum(cell.things.count(chaser) for cell in s.manager.cells.values())
        assert total == 1, "the entity was copied into %d cells" % total
    finally:
        s.stop()


def test_re_filing_keeps_the_reference_count_in_step():
    """Crossing between two *active* cells must not blink the entity out.

    Residency is reference counted, and the destination's reference is taken
    before the origin's is released precisely so a count never touches zero
    mid-move — a zero would fire a spurious park/unpark pair, and with it a
    spurious commit of the cell's persistent state.
    """
    chaser = make_thing(0.0, 0.0, "chaser")
    s = session([chaser])
    try:
        parks = []
        real = s._set_thing_active

        def _watch(thing, active):
            if thing is chaser:
                parks.append(active)
            return real(thing, active)

        s._set_thing_active = _watch
        for x in (600.0, 1100.0, 1700.0):     # three neighbouring active cells
            walk(s, x, entities=[chaser])
        assert parks == [], (
            "moving between adjacent active cells parked/unparked the entity "
            "%s times" % len(parks))
        assert s.manager._thing_active_ref[s.manager.thing_uuid(chaser)] == 1, (
            "the entity holds %d active references but lives in one cell"
            % s.manager._thing_active_ref[s.manager.thing_uuid(chaser)])
    finally:
        s._set_thing_active = real
        s.stop()


def test_an_entity_that_walks_out_of_range_parks_exactly_once():
    chaser = make_thing(0.0, 0.0, "chaser")
    s = session([chaser])
    try:
        walk(s, 3000.0)                       # the player moves; the entity does not
        assert chaser.properties.get("hidden") is True
        assert chaser.properties.get("disabled") is True
        assert s.manager.thing_uuid(chaser) not in s.manager._active_thing_ids
        # The marker holds the authored value, written once.
        assert chaser.properties.get("_bw_parked_hidden") is False
    finally:
        s.stop()


def test_a_parked_entity_is_not_re_filed():
    """Only resident entities are considered, and that is what bounds the cost.

    A parked entity carries ``disabled``, so nothing simulates it and it cannot
    move.  Walking the parked half of the world every crossing would make the
    cost of moving scale with the world's population — the one thing the design
    forbids.
    """
    far = make_thing(20000.0, 0.0, "far")
    near = make_thing(0.0, 0.0, "near")
    s = session([near, far])
    try:
        assert far.properties.get("hidden") is True
        before = cells_holding(s.manager, far)
        far.pos = [0.0, 0.0, 0.0]             # something moved it anyway
        walk(s, 600.0)
        assert cells_holding(s.manager, far) == before, (
            "the re-filing pass walked a dormant entity")
    finally:
        s.stop()


def test_an_entity_walking_into_empty_space_stays_live():
    """A cell that held nothing was never a candidate for the active set.

    Filing an entity into it has to bring it in, or walking into an empty part
    of the world would park the entity standing next to the player.
    """
    chaser = make_thing(0.0, 0.0, "chaser")
    s = session([chaser], brushes=[brush(0.0, 0.0, "floor")])
    try:
        empty = cell_of_point(30000.0, 0.0, CELL)
        assert empty not in s.manager.cells, "fixture: that cell should be empty"
        walk(s, 30000.0, entities=[chaser])
        assert cells_holding(s.manager, chaser) == [empty]
        assert empty in s.manager.active_cells, (
            "the cell the entity walked into was never activated")
        assert chaser.properties.get("hidden") is not True
    finally:
        s.stop()


# ---------------------------------------------------------------------------
# Repeated streaming, and handing the world back
# ---------------------------------------------------------------------------

def test_repeated_streaming_cycles_do_not_accumulate_membership():
    """Twenty round trips must leave the index the size one leaves it."""
    chaser = make_thing(0.0, 0.0, "chaser")
    others = [make_thing(i * 700.0, 0.0, "o%d" % i) for i in range(12)]
    s = session([chaser] + others)
    try:
        walk(s, 0.0, entities=[chaser])
        baseline = sum(len(c.things) for c in s.manager.cells.values())
        for _ in range(20):
            walk(s, 20000.0, entities=[chaser])
            walk(s, 0.0, entities=[chaser])
        total = sum(len(c.things) for c in s.manager.cells.values())
        assert total == baseline == len(others) + 1, (
            "cell membership grew from %d to %d over twenty streaming cycles"
            % (baseline, total))
        assert len(s.manager._thing_cell) == len(others) + 1
    finally:
        s.stop()


def test_no_entity_is_lost_by_any_amount_of_streaming():
    """The invariant with teeth: every entity is somewhere, always."""
    movers = [make_thing(0.0, 0.0, "m%d" % i) for i in range(4)]
    statics = [make_thing(i * 900.0, i * 900.0, "s%d" % i) for i in range(10)]
    s = session(movers + statics)
    try:
        for step in range(30):
            x = (step * 1300.0) % 9000.0
            walk(s, x, entities=movers)
            for thing in movers + statics:
                holding = cells_holding(s.manager, thing)
                assert len(holding) == 1, (
                    "step %d: %s is filed under %s"
                    % (step, thing.properties["id"], holding))
    finally:
        s.stop()


def test_play_stop_hands_back_a_world_with_every_entity_where_it_stands():
    """Streaming is a runtime state change, never an edit.

    The entity's position is the game's to change; its flags, its uuid and the
    absence of any streaming marker are the session's to hand back.
    """
    chaser = make_thing(0.0, 0.0, "chaser", hidden=True)
    homebody = make_thing(0.0, 0.0, "homebody")
    s = session([chaser, homebody])
    walk(s, 9000.0, entities=[chaser])
    s.stop()

    assert chaser.properties["id"] == "chaser", "a uuid changed"
    assert chaser.pos == [9000.0, 0.0, 0.0], "play-stop moved an entity"
    assert chaser.properties["hidden"] is True, (
        "the entity the mapper authored hidden came back visible")
    assert homebody.properties.get("hidden") in (None, False)
    for thing in (chaser, homebody):
        leaked = [k for k in thing.properties
                  if k.startswith("_bw") or k in ("bw_active", "_sim_tier")]
        assert leaked == [], "streaming markers left on %s: %s" % (
            thing.properties["id"], leaked)
