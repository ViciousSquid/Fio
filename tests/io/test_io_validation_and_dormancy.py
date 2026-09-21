"""Two things the I/O system has to get right that nothing used to check.

**Validation.** When the map is the program, a connection pointing at nothing
is a broken reference, and one calling an input the target does not have is a
typo that fails in complete silence.  Both are answerable from data the editor
already holds, and the answers are asserted here — including the distinction
that is easy to lose, between an entity type that *has* no inputs and one Fio
has never heard of.

**Dormancy.** Big World parks an entity by forcing ``hidden`` and ``disabled``
on and stashing what the map authored.  An I/O event that writes those flags
straight onto a parked entity is thrown away at the next unpark, which is the
one way an event-driven world can lose a change with nothing reporting an
error.  The generic inputs write through the authored-flag helpers instead, and
these tests are what keeps them doing so.
"""

import re

import pytest

from editor import io_system as io
from editor.io_system import IOManager, OutputConnection
from engine.spatial import (PARKED_DISABLED_KEY, PARKED_HIDDEN_KEY,
                            authored_disabled)
from tests.helpers.worlds import box_brush

#: The entity classes live in ``editor.things``, which needs PyQt5. Most of
#: this file does not, so the handful of cases that do carry the ``qt`` marker
#: rather than the whole module: the headless tier still runs everything else.
needs_entities = pytest.mark.qt


# ===========================================================================
# Validation
# ===========================================================================

class Scene:
    def __init__(self):
        self.brushes = []
        self.things = []

    def brush(self, name, **props):
        b = box_brush(name, **props)
        self.brushes.append(b)
        return b

    def wire(self, source, output, target, input_name, by_id=True):
        tid = target.get("id") if isinstance(target, dict) else \
            target.properties.get("id")
        conn = OutputConnection(
            output_name=output, target_name=(
                target.get("name") if isinstance(target, dict)
                else target.properties.get("name")),
            input_name=input_name, target_id=(tid if by_id else ""))
        io.add_connection(source, conn)
        return conn

    def problems(self):
        return io.validate_scene_connections(self.brushes, self.things)

    def codes(self):
        return sorted(code for _e, _c, code, _m in self.problems())


@pytest.fixture
def scene():
    return Scene()


def test_a_sound_connection_reports_nothing(scene):
    button = scene.brush("button", is_trigger=True)
    door = scene.brush("door", is_door=True)
    scene.wire(button, "OnTrigger", door, "Open")
    assert scene.problems() == []


def test_a_connection_to_a_missing_entity_is_reported(scene):
    button = scene.brush("button", is_trigger=True)
    ghost = box_brush("ghost", is_door=True)          # never added to the scene
    scene.wire(button, "OnTrigger", ghost, "Open")
    assert scene.codes() == [io.PROBLEM_MISSING_TARGET]


def test_a_name_addressed_connection_is_not_reported_as_broken(scene):
    """``target_id`` defaults to ``""``, not ``None``.

    The menu action used to treat "not None" as "addressed by id", so every
    legacy name-only connection in every map was reported broken.
    """
    button = scene.brush("button", is_trigger=True)
    door = scene.brush("door", is_door=True)
    scene.wire(button, "OnTrigger", door, "Open", by_id=False)
    assert scene.problems() == []


def test_an_input_the_target_does_not_accept_is_reported(scene):
    button = scene.brush("button", is_trigger=True)
    door = scene.brush("door", is_door=True)
    scene.wire(button, "OnTrigger", door, "Opne")
    assert scene.codes() == [io.PROBLEM_UNKNOWN_INPUT]


def test_an_output_the_source_does_not_have_is_reported(scene):
    button = scene.brush("button", is_trigger=True)
    door = scene.brush("door", is_door=True)
    scene.wire(button, "OnPushed", door, "Open")
    assert scene.codes() == [io.PROBLEM_UNKNOWN_OUTPUT]


def test_input_names_are_matched_without_regard_to_case(scene):
    button = scene.brush("button", is_trigger=True)
    door = scene.brush("door", is_door=True)
    scene.wire(button, "ontrigger", door, "OPEN")
    assert scene.problems() == []


def test_generic_inputs_are_accepted_on_any_entity(scene):
    """Enable, Hide, SetTint and the rest are implemented for everything, so
    they are not wrong just because a type does not list them."""
    button = scene.brush("button", is_trigger=True)
    target = scene.brush("thing", is_door=True)
    for name in sorted(IOManager.GENERIC_INPUTS):
        scene.wire(button, "OnTrigger", target, name)
    assert scene.problems() == []


def test_the_generic_input_set_matches_what_the_dispatcher_implements():
    """The set validation trusts and the set the dispatcher handles are one."""
    source = open("editor/io_system.py", encoding="utf-8").read()
    body = source[source.index("def _try_generic_input"):
                  source.index("def _apply_tint")]
    handled = set(re.findall(r"input_lower == '([a-z]+)'", body))
    assert handled == set(IOManager.GENERIC_INPUTS), (
        "the generic-input set and the dispatcher have drifted: %s"
        % (handled ^ set(IOManager.GENERIC_INPUTS),))


# -- the distinction the length of a list cannot make -----------------------

@needs_entities
def test_a_registered_type_with_no_inputs_still_rejects_one(scene):
    """``playerstart`` declares no inputs, so *any* input on it is wrong."""
    from editor.things import PlayerStart
    start = PlayerStart(pos=[0, 0, 0], properties={"name": "start"})
    scene.things.append(start)
    button = scene.brush("button", is_trigger=True)
    scene.wire(button, "OnTrigger", start, "Fire")
    assert scene.codes() == [io.PROBLEM_UNKNOWN_INPUT]


def test_a_registered_type_with_no_outputs_still_rejects_one(scene):
    """``brush`` declares no outputs, so *any* output from one is wrong."""
    wall = scene.brush("wall")
    door = scene.brush("door", is_door=True)
    scene.wire(wall, "OnPushed", door, "Open")
    assert scene.codes() == [io.PROBLEM_UNKNOWN_OUTPUT]


@needs_entities
def test_an_unregistered_type_is_not_judged_at_all(scene):
    """A plugin entity that declared no I/O gets no opinion, not a complaint."""
    from editor.things import Thing
    plugin_thing = Thing(pos=[0, 0, 0],
                         properties={"name": "widget", "type": "vendor_widget"})
    scene.things.append(plugin_thing)
    button = scene.brush("button", is_trigger=True)
    scene.wire(button, "OnTrigger", plugin_thing, "DoSomethingVendorSpecific")
    assert scene.problems() == []


@needs_entities
def test_a_plugin_type_that_did_register_its_io_is_judged(scene):
    """Registering I/O opts a plugin entity into the same checking."""
    from editor.things import Thing
    io.register_io('vendor_gizmo',
                   inputs=[io.IODef('Spin', 'Spin it')],
                   outputs=[io.IODef('OnSpun', 'Spun')])
    try:
        gizmo = Thing(pos=[0, 0, 0],
                      properties={"name": "gizmo", "type": "vendor_gizmo"})
        scene.things.append(gizmo)
        button = scene.brush("button", is_trigger=True)
        scene.wire(button, "OnTrigger", gizmo, "Wobble")
        assert scene.codes() == [io.PROBLEM_UNKNOWN_INPUT]
    finally:
        io.IO_REGISTRY.pop('vendor_gizmo', None)


def test_is_registered_type_separates_the_two_cases():
    assert io.is_registered_type('playerstart') is True
    assert io.get_input_names('playerstart') == []
    assert io.is_registered_type('a_type_fio_has_never_heard_of') is False


class Streamed:
    """A world with an I/O manager and a Big-World-style parking switch."""

    def __init__(self):
        self.manager = IOManager()
        self.entities = {}
        self.manager.set_entity_finder(lambda n: self.entities.get(n))
        self.manager.set_entity_finder_by_id(
            lambda i: next((e for e in self.entities.values()
                            if e.get("id") == i), None))

    def add(self, name, **props):
        brush = box_brush(name, **props)
        self.entities[name] = brush
        return brush

    @staticmethod
    def park(brush):
        """Exactly what BigWorldSession._set_brush_active(False) does."""
        brush.setdefault(PARKED_HIDDEN_KEY, brush.get("hidden", False))
        brush.setdefault(PARKED_DISABLED_KEY, brush.get("disabled", False))
        brush["hidden"] = True
        brush["disabled"] = True
        brush["bw_active"] = False

    @staticmethod
    def unpark(brush):
        if PARKED_HIDDEN_KEY in brush:
            brush["hidden"] = brush.pop(PARKED_HIDDEN_KEY)
        if PARKED_DISABLED_KEY in brush:
            brush["disabled"] = brush.pop(PARKED_DISABLED_KEY)
        brush["bw_active"] = True

    def wire(self, source, output, target, input_name, **kw):
        io.add_connection(source, OutputConnection(
            output_name=output, target_name=target["name"],
            input_name=input_name, target_id=target["id"], **kw))


@pytest.fixture
def streamed():
    return Streamed()


def test_a_delayed_event_still_reaches_an_entity_parked_in_the_meantime(streamed):
    """Targets resolve when the event fires, not when it was queued."""
    button = streamed.add("button", is_trigger=True)
    light = streamed.add("light", disabled=True)
    streamed.wire(button, "OnTrigger", light, "Enable", delay=2.0)

    streamed.manager.fire_output(button, "OnTrigger")
    streamed.park(light)                       # the cell goes dormant
    streamed.manager.update(3.0)

    assert authored_disabled(light) is False, (
        "the event was delivered but the write landed on the parked flag")


def test_the_change_survives_the_entity_waking_up(streamed):
    """The whole point: unparking restores the stash, so the write has to have
    gone into the stash."""
    button = streamed.add("button", is_trigger=True)
    light = streamed.add("light", disabled=True)
    streamed.wire(button, "OnTrigger", light, "Enable", delay=2.0)

    streamed.manager.fire_output(button, "OnTrigger")
    streamed.park(light)
    streamed.manager.update(3.0)
    streamed.unpark(light)

    assert light["disabled"] is False


def test_hiding_a_dormant_entity_survives_its_return(streamed):
    button = streamed.add("button", is_trigger=True)
    prop = streamed.add("statue")
    streamed.wire(button, "OnTrigger", prop, "Hide")

    streamed.park(prop)
    streamed.manager.fire_output(button, "OnTrigger")
    streamed.unpark(prop)

    assert prop["hidden"] is True


def test_showing_a_dormant_entity_survives_its_return(streamed):
    button = streamed.add("button", is_trigger=True)
    prop = streamed.add("statue", hidden=True)
    streamed.wire(button, "OnTrigger", prop, "Show")

    streamed.park(prop)
    streamed.manager.fire_output(button, "OnTrigger")
    streamed.unpark(prop)

    assert prop["hidden"] is False


def test_toggling_visibility_reads_the_authored_value_not_the_parked_one(streamed):
    """A parked entity is *always* hidden, so a toggle that read the live flag
    would always turn it on and never off."""
    button = streamed.add("button", is_trigger=True)
    prop = streamed.add("statue", hidden=True)
    streamed.wire(button, "OnTrigger", prop, "ToggleVisibility")

    streamed.park(prop)
    streamed.manager.fire_output(button, "OnTrigger")
    streamed.unpark(prop)

    assert prop["hidden"] is False


def test_an_entity_that_was_never_parked_behaves_exactly_as_before(streamed):
    button = streamed.add("button", is_trigger=True)
    light = streamed.add("light", disabled=True)
    streamed.wire(button, "OnTrigger", light, "Enable")

    streamed.manager.fire_output(button, "OnTrigger")

    assert light["disabled"] is False
    assert PARKED_DISABLED_KEY not in light, (
        "the write invented a parking marker on an object nothing had parked")


def test_a_dormant_entity_does_no_work_of_its_own(streamed):
    """Dormancy costs nothing: there is no per-entity logic path to run."""
    prop = streamed.add("statue")
    streamed.park(prop)
    before = dict(prop)
    for _ in range(100):
        streamed.manager.update(0.1)
    assert prop == before, "something ticked a dormant entity"


def test_an_empty_queue_costs_nothing_per_frame(streamed):
    """The only per-frame work the I/O system does is drain its own queue."""
    streamed.add("button", is_trigger=True)
    for _ in range(1000):
        streamed.manager.update(0.016)
    assert streamed.manager.pending_events == []


# ===========================================================================
# Activator
# ===========================================================================

def test_the_activator_is_carried_through_a_chain(streamed):
    """A relay in the middle is not the thing the chain is about."""
    seen = {}

    def _record(entity, parameter, logic):
        seen["source"] = streamed.manager.current_source_id()
        seen["activator"] = streamed.manager.current_activator_id()

    streamed.manager.register_input_handler("brush", "record", _record)

    player_plate = streamed.add("plate", is_trigger=True)
    relay = streamed.add("relay")
    sink = streamed.add("sink")
    streamed.wire(player_plate, "OnTrigger", relay, "Pass")
    streamed.wire(relay, "OnPass", sink, "Record")

    def _pass(entity, parameter, logic):
        streamed.manager.fire_output(entity, "OnPass")
    streamed.manager.register_input_handler("brush", "pass", _pass)

    streamed.manager.fire_output(player_plate, "OnTrigger")

    assert seen["source"] == relay["id"], "the immediate source should be the relay"
    assert seen["activator"] == player_plate["id"], (
        "the activator should still be whatever began the chain")


def test_outside_a_chain_there_is_no_source(streamed):
    assert streamed.manager.current_source_id() == ""
    assert streamed.manager.current_activator_id() == ""


def test_the_source_is_unwound_after_dispatch(streamed):
    def _noop(entity, parameter, logic):
        pass
    streamed.manager.register_input_handler("brush", "noop", _noop)
    button = streamed.add("button", is_trigger=True)
    sink = streamed.add("sink")
    streamed.wire(button, "OnTrigger", sink, "Noop")

    streamed.manager.fire_output(button, "OnTrigger")

    assert streamed.manager.current_source() is None
    assert streamed.manager.current_activator() is None
