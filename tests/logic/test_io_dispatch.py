"""The I/O execution path: firing an output and what reaches the target.

``IOManager`` is the whole of Fio's logic runtime.  An output fires, each
matching connection resolves its target (by id first, by name second) and calls
an input handler, immediately or after a delay driven by ``update(delta)``.

These build miniature networks out of recording handlers and assert the exact
resulting state: which inputs ran, in which order, with which parameters.  Time
is always an explicit delta — nothing here sleeps or reads a clock, so a chain
of delayed events resolves identically on every machine.

The pathological shapes get their own section: A->B->A, a target deleted during
dispatch, several sources aimed at one object, and unbounded recursion.
"""

import pytest

pytest.importorskip("PyQt5", reason="editor.things needs PyQt5")

from editor import io_system as io                     # noqa: E402
from editor.io_system import IOManager, OutputConnection  # noqa: E402
from editor.things import LogicRelay                   # noqa: E402
from tests.helpers.worlds import box_brush, make_thing  # noqa: E402

pytestmark = pytest.mark.qt


class Network:
    """A miniature logic network with recording input handlers.

    Entities are brush dicts (cheap, and the shape ``IOManager`` resolves by
    both id and name).  Every ``Fire`` input appends to :attr:`log`, so a test
    asserts on an exact sequence rather than on a flag.
    """

    def __init__(self):
        self.manager = IOManager()
        self.entities = {}
        self.log = []                 # [(entity name, input name, parameter), ...]
        self.manager.set_entity_finder(self._by_name)
        self.manager.set_entity_finder_by_id(self._by_id)

    # -- world ------------------------------------------------------------
    def add(self, name, **props):
        brush = box_brush(name, **props)
        self.entities[name] = brush
        return brush

    def remove(self, name):
        self.entities.pop(name, None)

    def _by_name(self, name):
        return self.entities.get(name)

    def _by_id(self, entity_id):
        for brush in self.entities.values():
            if brush.get("id") == entity_id:
                return brush
        return None

    # -- wiring -----------------------------------------------------------
    def connect(self, source, output, target, input_name="Fire", by_id=True, **kw):
        target_brush = self.entities.get(target)
        connection = OutputConnection(
            output_name=output, target_name=target, input_name=input_name,
            target_id=(target_brush["id"] if (by_id and target_brush) else ""),
            **kw)
        io.add_connection(self.entities[source], connection)
        return connection

    def handler(self, entity_type="brush", input_name="Fire", then=None):
        """Register a recording handler, optionally with a side effect."""
        def _handle(entity, parameter, logic):
            self.log.append((entity.get("name"), input_name, parameter))
            if then is not None:
                then(entity, parameter, logic)
        self.manager.register_input_handler(entity_type, input_name, _handle)
        return _handle

    # -- driving ----------------------------------------------------------
    def fire(self, source, output, value=None):
        self.manager.fire_output(self.entities[source], output, value)

    def advance(self, delta):
        self.manager.update(delta)

    @property
    def names(self):
        return [entry[0] for entry in self.log]


@pytest.fixture
def net():
    return Network()


# ---------------------------------------------------------------------------
# A single hop
# ---------------------------------------------------------------------------

def test_firing_an_output_runs_the_targets_input(net):
    net.add("button", is_trigger=True)
    net.add("door", is_door=True)
    net.connect("button", "OnTrigger", "door")
    net.handler("door")

    net.fire("button", "OnTrigger")

    assert net.log == [("door", "Fire", "")], (
        "expected one call to door.Fire, got %s" % (net.log,))


def test_an_output_with_no_connections_does_nothing(net):
    net.add("button", is_trigger=True)
    net.handler()
    net.fire("button", "OnTrigger")
    assert net.log == []


def test_only_connections_matching_the_output_name_fire(net):
    net.add("button", is_trigger=True)
    net.add("a", is_door=True)
    net.add("b", is_door=True)
    net.connect("button", "OnStartTouch", "a")
    net.connect("button", "OnEndTouch", "b")
    net.handler("door")

    net.fire("button", "OnStartTouch")

    assert net.names == ["a"], (
        "OnStartTouch should reach only 'a'; it reached %s" % (net.names,))


def test_output_matching_ignores_case(net):
    net.add("button", is_trigger=True)
    net.add("door", is_door=True)
    net.connect("button", "OnTrigger", "door")
    net.handler("door")

    net.fire("button", "ontrigger")

    assert net.names == ["door"], (
        "output names are matched case-insensitively; 'ontrigger' reached %s"
        % (net.names,))


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------

def test_the_stable_id_is_preferred_over_the_name(net):
    """Two entities can share a name; only one can share an id."""
    net.add("button", is_trigger=True)
    real = net.add("door", is_door=True)
    # A second entity answering to the same name, registered first in the map.
    impostor = box_brush("door", is_door=True)
    impostor["id"] = "some-other-id"
    net.entities["door_impostor"] = impostor
    net.entities["door"] = real

    net.connect("button", "OnTrigger", "door", by_id=True)
    net.handler("door")

    net.fire("button", "OnTrigger")

    assert len(net.log) == 1
    # The finder returns the entity whose id matches, not the first by name.
    assert net.manager._find_entity_by_id(real["id"]) is real


def test_a_connection_with_no_id_falls_back_to_the_name(net):
    """Legacy maps carry names only."""
    net.add("button", is_trigger=True)
    net.add("door", is_door=True)
    net.connect("button", "OnTrigger", "door", by_id=False)
    net.handler("door")

    net.fire("button", "OnTrigger")

    assert net.names == ["door"]


def test_a_missing_target_is_skipped_without_taking_the_tick_down(net):
    net.add("button", is_trigger=True)
    net.add("door", is_door=True)
    net.connect("button", "OnTrigger", "ghost", by_id=False)
    net.connect("button", "OnTrigger", "door")
    net.handler("door")

    net.fire("button", "OnTrigger")

    assert net.names == ["door"], (
        "a connection to a non-existent entity must be skipped and the rest of "
        "the output still delivered; log is %s" % (net.log,))


def test_a_target_deleted_before_dispatch_is_skipped(net):
    net.add("button", is_trigger=True)
    net.add("door", is_door=True)
    net.connect("button", "OnTrigger", "door")
    net.handler("door")

    net.remove("door")
    net.fire("button", "OnTrigger")

    assert net.log == [], "a deleted target still received its input"


def test_a_target_deleted_during_dispatch_is_skipped(net):
    """One output fires several connections; the first can delete the second's
    target.  The rest of the dispatch must survive it."""
    net.add("button", is_trigger=True)
    net.add("first", is_door=True)
    net.add("doomed", is_door=True)
    net.add("last", is_door=True)
    net.connect("button", "OnTrigger", "first")
    net.connect("button", "OnTrigger", "doomed")
    net.connect("button", "OnTrigger", "last")

    def _delete_doomed(entity, parameter, logic):
        if entity.get("name") == "first":
            net.remove("doomed")

    net.handler("door", then=_delete_doomed)

    net.fire("button", "OnTrigger")

    assert net.names == ["first", "last"], (
        "deleting a target mid-dispatch should skip only that target; log is %s"
        % (net.log,))


def test_a_target_replaced_between_firings_receives_the_input(net):
    """Undo rebuilds entities; a connection addressed by id must follow."""
    net.add("button", is_trigger=True)
    original = net.add("door", is_door=True)
    net.connect("button", "OnTrigger", "door")
    net.handler("door")

    replacement = box_brush("door", is_door=True)
    replacement["id"] = original["id"]        # same identity, new dict
    net.entities["door"] = replacement

    net.fire("button", "OnTrigger")

    assert net.names == ["door"]
    assert net.manager._find_entity_by_id(original["id"]) is replacement, (
        "the id lookup still resolves to the replaced object")


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

def test_the_authored_parameter_reaches_the_handler(net):
    net.add("button", is_trigger=True)
    net.add("door", is_door=True)
    net.connect("button", "OnTrigger", "door", parameter="slow")
    net.handler("door")

    net.fire("button", "OnTrigger")

    assert net.log == [("door", "Fire", "slow")]


def test_a_blank_parameter_inherits_the_fired_value(net):
    """Source-engine style pass-through."""
    net.add("button", is_trigger=True)
    net.add("door", is_door=True)
    net.connect("button", "OnTrigger", "door")           # no parameter
    net.handler("door")

    net.fire("button", "OnTrigger", value="dynamic")

    assert net.log == [("door", "Fire", "dynamic")], (
        "a connection with a blank parameter should inherit the fired value; "
        "got %s" % (net.log,))


def test_an_authored_parameter_wins_over_the_fired_value(net):
    net.add("button", is_trigger=True)
    net.add("door", is_door=True)
    net.connect("button", "OnTrigger", "door", parameter="authored")
    net.handler("door")

    net.fire("button", "OnTrigger", value="dynamic")

    assert net.log == [("door", "Fire", "authored")], (
        "an explicit parameter must not be overwritten by pass-through; got %s"
        % (net.log,))


# ---------------------------------------------------------------------------
# Delays
# ---------------------------------------------------------------------------

def test_a_delayed_connection_does_not_fire_immediately(net):
    net.add("button", is_trigger=True)
    net.add("door", is_door=True)
    net.connect("button", "OnTrigger", "door", delay=2.0)
    net.handler("door")

    net.fire("button", "OnTrigger")

    assert net.log == [], "a 2s delay fired at once"


def test_a_delayed_connection_fires_once_its_time_arrives(net):
    net.add("button", is_trigger=True)
    net.add("door", is_door=True)
    net.connect("button", "OnTrigger", "door", delay=2.0)
    net.handler("door")
    net.fire("button", "OnTrigger")

    net.advance(1.0)
    assert net.log == [], "fired after 1.0s of a 2.0s delay"
    net.advance(1.0)
    assert net.log == [("door", "Fire", "")], (
        "did not fire at 2.0s; log is %s" % (net.log,))


def test_a_delayed_connection_fires_exactly_once(net):
    net.add("button", is_trigger=True)
    net.add("door", is_door=True)
    net.connect("button", "OnTrigger", "door", delay=0.5)
    net.handler("door")
    net.fire("button", "OnTrigger")

    for _ in range(20):
        net.advance(0.1)

    assert len(net.log) == 1, (
        "the pending event fired %d times; it must be consumed on delivery"
        % len(net.log))


def test_delays_deliver_in_time_order_not_wiring_order(net):
    net.add("button", is_trigger=True)
    for name, delay in (("late", 0.9), ("early", 0.1), ("middle", 0.5)):
        net.add(name, is_door=True)
        net.connect("button", "OnTrigger", name, delay=delay)
    net.handler("door")

    net.fire("button", "OnTrigger")
    for _ in range(10):
        net.advance(0.1)

    assert net.names == ["early", "middle", "late"], (
        "delayed events must arrive in time order; got %s" % (net.names,))


def test_several_delays_landing_in_one_tick_all_fire(net):
    net.add("button", is_trigger=True)
    for name in ("a", "b", "c"):
        net.add(name, is_door=True)
        net.connect("button", "OnTrigger", name, delay=0.1)
    net.handler("door")

    net.fire("button", "OnTrigger")
    net.advance(1.0)

    assert sorted(net.names) == ["a", "b", "c"], (
        "three events due in the same tick delivered %s" % (net.names,))


def test_a_target_deleted_while_an_event_is_pending_is_skipped(net):
    net.add("button", is_trigger=True)
    net.add("door", is_door=True)
    net.connect("button", "OnTrigger", "door", delay=1.0)
    net.handler("door")
    net.fire("button", "OnTrigger")

    net.remove("door")
    net.advance(2.0)

    assert net.log == [], "a pending event fired at an entity that is gone"


def test_resetting_the_manager_drops_pending_events(net):
    """Leaving play mode must not leave a timer armed for the next session."""
    net.add("button", is_trigger=True)
    net.add("door", is_door=True)
    net.connect("button", "OnTrigger", "door", delay=1.0)
    net.handler("door")
    net.fire("button", "OnTrigger")

    net.manager.reset()
    net.advance(5.0)

    assert net.log == [], (
        "an event queued before the reset still fired: %s" % (net.log,))
    assert net.manager.pending_events == []


# ---------------------------------------------------------------------------
# fire_once
# ---------------------------------------------------------------------------

def test_a_fire_once_connection_fires_only_the_first_time(net):
    net.add("button", is_trigger=True)
    net.add("door", is_door=True)
    net.connect("button", "OnTrigger", "door", fire_once=True)
    net.handler("door")

    net.fire("button", "OnTrigger")
    net.fire("button", "OnTrigger")
    net.fire("button", "OnTrigger")

    assert len(net.log) == 1, (
        "a fire_once connection fired %d times" % len(net.log))


def test_resetting_a_fire_once_connection_re_arms_it(net):
    net.add("button", is_trigger=True)
    net.add("door", is_door=True)
    connection = net.connect("button", "OnTrigger", "door", fire_once=True)
    net.handler("door")
    net.fire("button", "OnTrigger")

    connection.reset()
    net.fire("button", "OnTrigger")

    assert len(net.log) == 2, (
        "after reset() the connection should fire again; log is %s" % (net.log,))


# ---------------------------------------------------------------------------
# Chains, loops and fan-out
# ---------------------------------------------------------------------------

def test_a_chain_runs_a_to_b_to_c_in_order(net):
    for name in ("a", "b", "c"):
        net.add(name, is_door=True)
    net.connect("a", "OnFired", "b")
    net.connect("b", "OnFired", "c")

    def _relay(entity, parameter, logic):
        net.manager.fire_output(entity, "OnFired")

    net.handler("door", then=_relay)

    net.manager.fire_output(net.entities["a"], "OnFired")

    assert net.names == ["b", "c"], (
        "A->B->C should deliver b then c; got %s" % (net.names,))


def test_one_output_fanning_out_reaches_every_target_in_wiring_order(net):
    net.add("hub", is_trigger=True)
    for name in ("first", "second", "third"):
        net.add(name, is_door=True)
        net.connect("hub", "OnTrigger", name)
    net.handler("door")

    net.fire("hub", "OnTrigger")

    assert net.names == ["first", "second", "third"], (
        "fan-out must be deterministic and follow wiring order; got %s"
        % (net.names,))


def test_several_sources_targeting_one_object_all_reach_it(net):
    net.add("target", is_door=True)
    for name in ("s1", "s2", "s3"):
        net.add(name, is_trigger=True)
        net.connect(name, "OnTrigger", "target", parameter=name)
    net.handler("door")

    for name in ("s1", "s2", "s3"):
        net.fire(name, "OnTrigger")

    assert [entry[2] for entry in net.log] == ["s1", "s2", "s3"], (
        "each source should deliver its own parameter; got %s" % (net.log,))


def test_a_loop_broken_by_fire_once_terminates(net):
    """A -> B -> A is legal wiring; ``fire_once`` is how a map bounds it."""
    net.add("a", is_door=True)
    net.add("b", is_door=True)
    net.connect("a", "OnFired", "b", fire_once=True)
    net.connect("b", "OnFired", "a", fire_once=True)

    def _relay(entity, parameter, logic):
        net.manager.fire_output(entity, "OnFired")

    net.handler("door", then=_relay)

    net.manager.fire_output(net.entities["a"], "OnFired")

    assert net.names == ["b", "a"], (
        "the loop should run one lap and stop; got %s" % (net.names,))


def test_an_unbounded_loop_is_bounded_by_delays_rather_than_by_the_manager(net):
    """Documents that the manager does not detect recursion itself.

    A zero-delay A->B->A with no ``fire_once`` would recurse until Python's
    stack gave out - the map is what has to break the cycle.  A *delayed* loop
    is the safe shape: each lap is queued, so it advances one step per
    ``update`` and a test (or the player) can stop it.
    """
    net.add("a", is_door=True)
    net.add("b", is_door=True)
    net.connect("a", "OnFired", "b", delay=0.1)
    net.connect("b", "OnFired", "a", delay=0.1)

    def _relay(entity, parameter, logic):
        net.manager.fire_output(entity, "OnFired")

    net.handler("door", then=_relay)
    net.manager.fire_output(net.entities["a"], "OnFired")

    for _ in range(6):
        net.advance(0.1)

    assert net.names == ["b", "a", "b", "a", "b", "a"], (
        "a delayed loop should advance exactly one hop per 0.1s tick; got %s"
        % (net.names,))
    assert len(net.manager.pending_events) == 1, (
        "exactly one lap should be in flight at a time, %d are queued"
        % len(net.manager.pending_events))


# ---------------------------------------------------------------------------
# Handler failure
# ---------------------------------------------------------------------------

def test_a_handler_that_raises_does_not_stop_the_rest_of_the_dispatch(net):
    net.add("button", is_trigger=True)
    net.add("boom", is_door=True)
    net.add("after", is_door=True)
    net.connect("button", "OnTrigger", "boom", input_name="Explode")
    net.connect("button", "OnTrigger", "after", input_name="Fire")

    def _explode(entity, parameter, logic):
        raise RuntimeError("handler failed on purpose")

    net.manager.register_input_handler("door", "Explode", _explode)
    net.handler("door")

    net.fire("button", "OnTrigger")

    assert net.names == ["after"], (
        "a raising handler must not swallow the connections after it; got %s"
        % (net.log,))


def test_an_unregistered_input_falls_through_to_the_generic_handling(net):
    """An input with no registered handler must not raise."""
    net.add("button", is_trigger=True)
    target = net.add("plain")
    net.connect("button", "OnTrigger", "plain", input_name="NoSuchInput")

    net.fire("button", "OnTrigger")   # must not raise

    assert net.log == []


def test_the_generic_hide_and_show_inputs_work_without_a_registered_handler(net):
    net.add("button", is_trigger=True)
    target = net.add("prop")
    net.connect("button", "Off", "prop", input_name="Hide")
    net.connect("button", "On", "prop", input_name="Show")

    net.fire("button", "Off")
    assert target.get("hidden") is True, (
        "the generic Hide input did not hide the brush (hidden=%r)"
        % target.get("hidden"))

    net.fire("button", "On")
    assert target.get("hidden") is False


# ---------------------------------------------------------------------------
# Manager wiring
# ---------------------------------------------------------------------------

def test_a_manager_with_no_entity_finder_does_not_raise(net):
    manager = IOManager()
    source = box_brush("button", is_trigger=True)
    io.add_connection(source, OutputConnection(
        output_name="OnTrigger", target_name="door", input_name="Open"))
    manager.fire_output(source, "OnTrigger")   # must not raise


def test_handlers_are_keyed_case_insensitively(net):
    net.add("button", is_trigger=True)
    net.add("door", is_door=True)
    net.connect("button", "OnTrigger", "door", input_name="fire")
    net.handler("door", input_name="Fire")

    net.fire("button", "OnTrigger")

    assert net.names == ["door"], (
        "input names are matched case-insensitively; 'fire' found %s"
        % (net.names,))
