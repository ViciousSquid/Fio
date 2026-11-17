"""A zero-delay I/O cycle is stopped by the dispatcher, not by Python's stack.

A connection with no delay is delivered in place, so A -> B -> A nests one
delivery inside the next for ever.  Before the guard that ran until the
interpreter's recursion limit (leaving whatever state the stack happened to
unwind through) and, with two connections per hop, never returned at all - on
the logic thread, holding the tick.  ``IOManager`` now abandons a chain that
nests deeper than ``MAX_CHAIN_DEPTH`` and says so once.

Everything here is a bare ``IOManager`` over brush dicts with counting
handlers: no Qt, no clock, no threads.
"""

import sys

import pytest

from editor import io_system as io
from editor.io_system import IOManager, OutputConnection
from tests.helpers.worlds import box_brush


#: The dispatcher's limit.  Read defensively so the tests that describe
#: behaviour older than the guard (finite chains, delayed loops) still run
#: against a dispatcher without one.
DEPTH = getattr(IOManager, "MAX_CHAIN_DEPTH", 64)


class Net:
    def __init__(self, monkeypatch):
        self.manager = IOManager()
        self.entities = {}
        self.calls = []
        self.errors = []
        self.manager.set_entity_finder(self.entities.get)
        self.manager.set_entity_finder_by_id(
            lambda eid: next((e for e in self.entities.values()
                              if e.get("id") == eid), None))
        monkeypatch.setattr(
            io, "debug_log",
            lambda category, message: (self.errors.append(message)
                                       if category == "Error" else None))

        def relay(entity, parameter, logic):
            self.calls.append(entity["name"])
            self.manager.fire_output(entity, "OnFired")

        self.manager.register_input_handler("door", "Fire", relay)

    def add(self, name):
        self.entities[name] = box_brush(name, is_door=True)
        return self.entities[name]

    def connect(self, source, target, **kw):
        io.add_connection(self.entities[source], OutputConnection(
            output_name="OnFired", target_name=target, input_name="Fire",
            target_id=self.entities[target]["id"], **kw))

    def fire(self, name):
        self.manager._execute_input(name, "Fire", "", "test",
                                    target_id=self.entities[name]["id"])

    @property
    def loop_errors(self):
        return [m for m in self.errors if "zero-delay loop" in m]


@pytest.fixture
def net(monkeypatch):
    return Net(monkeypatch)


def test_a_zero_delay_cycle_is_stopped_at_the_depth_limit(net):
    net.add("a"); net.add("b")
    net.connect("a", "b"); net.connect("b", "a")

    net.fire("a")

    assert len(net.calls) == DEPTH, (
        "a zero-delay A->B->A should deliver exactly MAX_CHAIN_DEPTH inputs "
        "and stop; %d were delivered" % len(net.calls))
    assert len(net.loop_errors) == 1, (
        "the stopped loop should be reported exactly once, got %d reports: %s"
        % (len(net.loop_errors), net.loop_errors[:3]))
    assert "a.Fire" in net.loop_errors[0] and "b.Fire" in net.loop_errors[0], (
        "the report should name the entities in the loop: %r"
        % net.loop_errors[0])
    assert not any("handler failed" in m for m in net.errors), (
        "the loop must be stopped by the guard, not by a RecursionError "
        "caught in a handler: %s" % net.errors[:3])


def test_a_cycle_with_fan_out_returns_instead_of_branching_for_ever(net):
    net.add("a"); net.add("b")
    for _ in range(2):
        net.connect("a", "b"); net.connect("b", "a")

    net.fire("a")

    assert len(net.calls) == DEPTH, (
        "once the limit trips, the frames still on the stack must not deliver "
        "their remaining connections; %d inputs ran" % len(net.calls))
    assert len(net.loop_errors) == 1, (
        "one report per stopped chain, got %d" % len(net.loop_errors))


def test_an_entity_wired_to_itself_is_stopped_too(net):
    net.add("a")
    net.connect("a", "a")
    net.fire("a")
    assert len(net.calls) == DEPTH
    assert len(net.loop_errors) == 1


def test_the_guard_stays_far_inside_the_recursion_limit(net):
    net.add("a"); net.add("b")
    net.connect("a", "b"); net.connect("b", "a")
    deepest = []

    def probe(entity, parameter, logic):
        frame, depth = sys._getframe(), 0
        while frame is not None:
            frame, depth = frame.f_back, depth + 1
        deepest.append(depth)
        net.manager.fire_output(entity, "OnFired")

    net.manager.register_input_handler("door", "Fire", probe)
    base = len(__import__("inspect").stack())
    net.fire("a")

    assert max(deepest) - base < sys.getrecursionlimit() // 2, (
        "the depth limit should stop a loop long before Python's own limit "
        "(%d frames used of %d)" % (max(deepest) - base,
                                    sys.getrecursionlimit()))


def test_unrelated_io_still_works_after_a_loop_was_stopped(net):
    net.add("a"); net.add("b"); net.add("c"); net.add("d")
    net.connect("a", "b"); net.connect("b", "a")
    net.connect("c", "d")

    net.fire("a")
    del net.calls[:]
    net.fire("c")

    assert net.calls == ["c", "d"], (
        "a stopped loop must not leave the dispatcher refusing later chains; "
        "c -> d delivered %s" % (net.calls,))
    assert net.manager._chain_depth == 0 and not net.manager._chain_aborted, (
        "the guard's bookkeeping should be back at rest after the chain")
    assert len(net.loop_errors) == 1, "the healthy chain must not be reported"


def test_a_stopped_loop_queues_nothing_more_on_its_way_out(net):
    net.add("a"); net.add("b"); net.add("late")
    net.connect("a", "b")
    net.connect("b", "a")
    net.connect("b", "late", delay=1.0)   # after the looping connection

    net.fire("a")

    # Every lap that completed normally would queue one; the frames unwinding
    # after the abort must not.
    assert net.manager.pending_events == [], (
        "an abandoned chain should not go on queueing delayed events while it "
        "unwinds; %d were queued" % len(net.manager.pending_events))


def test_a_long_finite_chain_below_the_limit_is_delivered_whole(net):
    count = DEPTH
    names = ["n%d" % i for i in range(count)]
    for name in names:
        net.add(name)
    for left, right in zip(names, names[1:]):
        net.connect(left, right)

    net.fire(names[0])

    assert net.calls == names, (
        "a finite chain of %d hops is legal and must run to its end; "
        "%d hops ran" % (count, len(net.calls)))
    assert net.loop_errors == [], "a finite chain is not a loop"


def test_a_handler_failure_inside_a_chain_does_not_leak_depth(net):
    net.add("a"); net.add("b")
    net.connect("a", "b")

    def boom(entity, parameter, logic):
        if entity["name"] == "b":
            raise RuntimeError("boom")
        net.manager.fire_output(entity, "OnFired")

    net.manager.register_input_handler("door", "Fire", boom)
    for _ in range(DEPTH * 2):
        net.fire("a")

    assert getattr(net.manager, "_chain_depth", 0) == 0, (
        "a raising handler must still unwind the depth counter")
    assert net.loop_errors == [], (
        "repeated failing deliveries are not a loop and must not trip the guard")


def test_a_delayed_loop_is_a_timer_and_runs_indefinitely(net):
    net.add("a"); net.add("b")
    net.connect("a", "b", delay=0.1)
    net.connect("b", "a", delay=0.1)

    net.fire("a")
    laps = DEPTH * 3
    for _ in range(laps):
        net.manager.update(0.1)

    assert len(net.calls) == laps + 1, (
        "a delayed loop advances one hop per tick for as long as it is ticked; "
        "%d hops ran in %d ticks" % (len(net.calls) - 1, laps))
    assert net.loop_errors == [], "a delayed loop must never be reported"


def test_a_loop_with_one_zero_delay_lap_per_tick_keeps_running(net):
    """Zero-delay hops reached from a delayed event start a fresh chain."""
    net.add("a"); net.add("b"); net.add("c")
    net.connect("a", "b")
    net.connect("b", "c")
    net.connect("c", "a", delay=0.1)

    net.fire("a")
    for _ in range(DEPTH * 2):
        net.manager.update(0.1)

    assert len(net.calls) == 3 * (DEPTH * 2 + 1)
    assert net.loop_errors == []


def test_the_limit_is_deep_enough_for_authored_chains():
    assert 32 <= IOManager.MAX_CHAIN_DEPTH <= 128, (
        "the limit must clear any authored chain and stay well inside the "
        "interpreter's recursion limit; it is %d" % IOManager.MAX_CHAIN_DEPTH)


def test_reset_clears_the_guard(net):
    net.manager._chain_depth = 5
    net.manager._chain_aborted = True
    net.manager.reset()
    assert net.manager._chain_depth == 0 and not net.manager._chain_aborted
