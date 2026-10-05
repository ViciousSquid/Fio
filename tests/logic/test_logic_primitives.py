"""Relay, gate and timer — the three primitives state composes with.

Each answers one question and no more: a relay routes an event, a gate combines
transient signals, a timer turns time into events.  The tests here are mostly
about the boundaries between them, because that separation is the thing most
easily lost.

Several cases are regressions for defects the pre-2.4 implementations carried:
a ``fire_once`` relay that fired every time, an AND gate that closed on its
first signal whenever it was wired the way the editor actually wires things, and
timer state filed under a memory address.
"""

import pytest

pytest.importorskip("PyQt5", reason="editor.things needs PyQt5")

from editor import io_system as io                       # noqa: E402
from editor.editor_state import EditorState               # noqa: E402
from editor.io_system import IOManager, OutputConnection  # noqa: E402
from editor.io_handlers import register_all_input_handlers  # noqa: E402
from editor.things import LogicGate, LogicRelay, LogicState, LogicTimer  # noqa: E402
from tests.helpers.worlds import box_brush                # noqa: E402
from engine.logic_thread import LogicThread                 # noqa: E402
from engine.threaded_game_state import ThreadedGameState   # noqa: E402
from engine.logic_timing import LogicTiming                    # noqa: E402
from engine.logic_world import LogicWorld                          # noqa: E402

pytestmark = pytest.mark.qt


class World:
    """Real LogicThread world with the production I/O dispatcher and runtimes."""

    def __init__(self):
        self.logic = LogicThread(ThreadedGameState(), EditorState())
        self.manager = self.logic.io_manager
        self.io_manager = self.manager
        self.editor_state = self.logic.editor_state
        self.timing_runtime = self.logic.timing_runtime
        self.world_runtime = self.logic.world_runtime
        self.door_states = self.logic.mover_runtime.door_states
        self.mover_states = self.logic.mover_runtime.mover_states
        self.log = []

        self.manager.set_entity_finder(self._by_name)
        self.manager.set_entity_finder_by_id(self._by_id)

        def _sink(entity, parameter, logic):
            self.log.append((entity.get("name"), parameter))

        self.manager.register_input_handler("brush", "fire", _sink)
        self.sink = self.add_brush("sink")

    def close(self):
        self.logic.stop()

    def add_brush(self, name, **props):
        brush = box_brush(name, **props)
        self.editor_state.brushes.append(brush)
        return brush

    def add(self, thing):
        self.editor_state.things.append(thing)
        self.logic.world_runtime.build_entity_caches()
        return thing

    def _by_name(self, name):
        for brush in self.editor_state.brushes:
            if brush.get("name") == name:
                return brush
        for thing in self.editor_state.things:
            if thing.properties.get("name") == name:
                return thing
        return None

    def _by_id(self, entity_id):
        for brush in self.editor_state.brushes:
            if brush.get("id") == entity_id:
                return brush
        for thing in self.editor_state.things:
            if thing.properties.get("id") == entity_id:
                return thing
        return None

    def connect(self, source, output, target, input_name, parameter="", **kw):
        target_id = (
            target.get("id") if isinstance(target, dict)
            else target.properties.get("id")
        )
        target_name = (
            target.get("name") if isinstance(target, dict)
            else target.properties.get("name")
        )
        conn = OutputConnection(
            output_name=output,
            target_name=target_name,
            input_name=input_name,
            parameter=parameter,
            target_id=target_id,
            **kw,
        )
        io.add_connection(source, conn)
        return conn

    def to_sink(self, source, output="OnTrigger"):
        return self.connect(source, output, self.sink, "Fire")

    def send(self, entity, input_name, parameter=""):
        name = (
            entity.get("name") if isinstance(entity, dict)
            else entity.properties.get("name")
        )
        entity_id = (
            entity.get("id") if isinstance(entity, dict)
            else entity.properties.get("id")
        )
        self.manager._execute_input(
            name, input_name, parameter, "test", target_id=entity_id
        )

    @property
    def hits(self):
        return len(self.log)


@pytest.fixture
def world():
    value = World()
    yield value
    value.close()


def world():
    return World()


# ===========================================================================
# LogicRelay
# ===========================================================================

def test_a_relay_routes_its_trigger_on(world):
    relay = world.add(LogicRelay(pos=[0, 0, 0], properties={"name": "relay"}))
    world.to_sink(relay)
    world.send(relay, "Trigger")
    assert world.hits == 1


def test_a_disabled_relay_routes_nothing(world):
    relay = world.add(LogicRelay(pos=[0, 0, 0], properties={"name": "relay"}))
    world.to_sink(relay)
    world.send(relay, "Disable")
    world.send(relay, "Trigger")
    assert world.hits == 0


def test_enable_puts_a_disabled_relay_back(world):
    relay = world.add(LogicRelay(pos=[0, 0, 0],
                                 properties={"name": "relay", "disabled": True}))
    world.to_sink(relay)
    world.send(relay, "Enable")
    world.send(relay, "Trigger")
    assert world.hits == 1


def test_toggle_flips_the_enabled_state(world):
    relay = world.add(LogicRelay(pos=[0, 0, 0], properties={"name": "relay"}))
    world.to_sink(relay)
    world.send(relay, "Toggle")
    world.send(relay, "Trigger")
    assert world.hits == 0
    world.send(relay, "Toggle")
    world.send(relay, "Trigger")
    assert world.hits == 1


def test_a_fire_once_relay_fires_once(world):
    """The property existed and nothing read it, so one-shot relays repeated."""
    relay = world.add(LogicRelay(pos=[0, 0, 0],
                                 properties={"name": "relay", "fire_once": True}))
    world.to_sink(relay)
    for _ in range(5):
        world.send(relay, "Trigger")
    assert world.hits == 1


def test_reset_re_arms_a_fire_once_relay(world):
    relay = world.add(LogicRelay(pos=[0, 0, 0],
                                 properties={"name": "relay", "fire_once": True}))
    world.to_sink(relay)
    world.send(relay, "Trigger")
    world.send(relay, "Reset")
    world.send(relay, "Trigger")
    assert world.hits == 2


def test_a_relay_passes_its_parameter_through(world):
    relay = world.add(LogicRelay(pos=[0, 0, 0], properties={"name": "relay"}))
    world.to_sink(relay)
    world.send(relay, "Trigger", "payload")
    assert world.log == [("sink", "payload")]


def test_ten_real_entities_forward_a_signal_to_the_end():
    """Ten real Things form a production I/O chain and the last entity detects it.

    There are no recording stubs in the chain: nine LogicRelay instances use
    the shipped relay input/output handlers, and the tenth entity is a real
    LogicState whose SetValue input records the payload. The initial Trigger is
    dispatched through the real IOManager, so every intermediate hop is actual
    entity logic.
    """
    world = World()
    LogicState._persistent_registry.pop("ten_entity_signal", None)
    try:
        chain = [
            world.add(LogicRelay(
                pos=[float(i * 64), 0, 0],
                properties={"name": "signal_relay_%d" % i},
            ))
            for i in range(9)
        ]
        detector = world.add(LogicState(
            pos=[576, 0, 0],
            properties={
                "name": "signal_detector",
                "store_name": "ten_entity_signal",
            },
        ))
        chain.append(detector)

        assert len(world.editor_state.things) == 10

        for source, target in zip(chain, chain[1:]):
            world.connect(
                source,
                "OnTrigger",
                target,
                "SetValue" if target is detector else "Trigger",
            )

        world.send(chain[0], "Trigger", "signal=arrived")

        assert detector.get_value("signal") == "arrived"
    finally:
        LogicState._persistent_registry.pop("ten_entity_signal", None)
        world.close()


def test_cancelpending_drops_a_relays_delayed_events(world):
    relay = world.add(LogicRelay(pos=[0, 0, 0], properties={"name": "relay"}))
    world.connect(relay, "OnTrigger", world.sink, "Fire", delay=1.0)
    world.send(relay, "Trigger")
    world.send(relay, "CancelPending")
    world.manager.update(2.0)
    assert world.hits == 0


# ===========================================================================
# LogicGate
# ===========================================================================

def _gate(world, logic_type, sources=2):
    gate = world.add(LogicGate(pos=[0, 0, 0],
                               properties={"name": "gate", "logic_type": logic_type}))
    for i in range(sources):
        switch = world.add_brush("switch%d" % i, is_trigger=True)
        # Wired by UUID — what the editor actually produces, and what the
        # pre-2.4 input count could not see.
        world.connect(switch, "OnTrigger", gate, "Trigger", parameter="s%d" % i)
    world.to_sink(gate)
    return gate


def test_an_and_gate_needs_every_input(world):
    gate = _gate(world, "AND")
    world.send(gate, "Trigger", "s0")
    assert world.hits == 0, "an AND gate fired on one of two signals"
    world.send(gate, "Trigger", "s1")
    assert world.hits == 1


def test_an_and_gate_wired_by_uuid_counts_its_inputs(world):
    """The count matched target *names* only, so a gate wired by id saw zero
    expected inputs, fell back to one, and closed on its first signal."""
    gate = _gate(world, "AND", sources=3)
    world.send(gate, "Trigger", "s0")
    world.send(gate, "Trigger", "s1")
    assert world.hits == 0
    world.send(gate, "Trigger", "s2")
    assert world.hits == 1


def test_an_or_gate_needs_one_input(world):
    gate = _gate(world, "OR")
    world.send(gate, "Trigger", "s0")
    assert world.hits == 1


def test_an_xor_gate_wants_exactly_one(world):
    gate = _gate(world, "XOR")
    world.send(gate, "Trigger", "s0")
    assert world.hits == 1
    world.send(gate, "Trigger", "s1")
    assert world.hits == 1, "XOR fired again with two signals asserted"


def test_a_nand_gate_is_true_until_every_input_arrives(world):
    gate = _gate(world, "NAND")
    world.send(gate, "Trigger", "s0")
    assert world.hits == 1
    world.send(gate, "Trigger", "s1")
    assert world.hits == 1


def test_a_nor_gate_is_true_while_nothing_is_asserted(world):
    gate = _gate(world, "NOR")
    world.send(gate, "Evaluate")
    assert world.hits == 1
    world.send(gate, "Trigger", "s0")
    assert world.hits == 1


def test_trigger_asserts_idempotently(world):
    """Before 2.4 Trigger *flipped* the signal, so a source reporting twice
    un-asserted itself and an AND gate could never close."""
    gate = _gate(world, "AND")
    world.send(gate, "Trigger", "s0")
    world.send(gate, "Trigger", "s0")
    world.send(gate, "Trigger", "s1")
    assert world.hits == 1


def test_clearinput_de_asserts(world):
    gate = _gate(world, "AND")
    world.send(gate, "Trigger", "s0")
    world.send(gate, "ClearInput", "s0")
    world.send(gate, "Trigger", "s1")
    assert world.hits == 0


def test_toggleinput_keeps_the_old_flip_behaviour(world):
    gate = _gate(world, "AND")
    world.send(gate, "ToggleInput", "s0")
    world.send(gate, "ToggleInput", "s0")
    world.send(gate, "Trigger", "s1")
    assert world.hits == 0


def test_reset_clears_every_signal(world):
    gate = _gate(world, "AND")
    world.send(gate, "Trigger", "s0")
    world.send(gate, "Reset")
    world.send(gate, "Trigger", "s1")
    assert world.hits == 0


def test_a_disabled_gate_ignores_signals(world):
    gate = _gate(world, "OR")
    world.send(gate, "Disable")
    world.send(gate, "Trigger", "s0")
    assert world.hits == 0


def test_a_failing_gate_fires_onfalse(world):
    gate = _gate(world, "AND")
    world.connect(gate, "OnFalse", world.sink, "Fire")
    world.send(gate, "Trigger", "s0")
    assert world.log == [("sink", "")]


def test_two_gates_keep_separate_signals(world):
    """Gate state is filed by UUID; two gates that share a name are still two."""
    first = world.add(LogicGate(pos=[0, 0, 0],
                                properties={"name": "gate", "logic_type": "OR"}))
    second = world.add(LogicGate(pos=[0, 0, 0],
                                 properties={"name": "gate", "logic_type": "OR"}))
    world.send(first, "Trigger", "a")
    assert world.manager.gate_inputs[first.properties["id"]] == {"a"}
    assert second.properties["id"] not in world.manager.gate_inputs


def test_unnamed_signals_from_different_sources_are_different_signals(world):
    """A connection with no parameter is identified by whoever fired it."""
    gate = world.add(LogicGate(pos=[0, 0, 0],
                               properties={"name": "gate", "logic_type": "AND"}))
    for i in range(2):
        switch = world.add_brush("switch%d" % i, is_trigger=True)
        world.connect(switch, "OnTrigger", gate, "Trigger")
    world.to_sink(gate)

    world.manager.fire_output(world.editor_state.brushes[1], "OnTrigger")
    assert world.hits == 0
    world.manager.fire_output(world.editor_state.brushes[2], "OnTrigger")
    assert world.hits == 1


# ===========================================================================
# LogicTimer
# ===========================================================================

def test_a_timer_fires_when_its_interval_elapses(world):
    from engine.logic_timing import LogicTiming
    timer = world.add(LogicTimer(pos=[0, 0, 0],
                                 properties={"name": "timer", "interval": 1.0}))
    world.to_sink(timer, "OnTimer")
    world.world_runtime.timer_things = [timer]
    world.send(timer, "Enable")

    world.timing_runtime.update_logic_timers(0.5)
    assert world.hits == 0
    world.timing_runtime.update_logic_timers( 0.6)
    assert world.hits == 1


def test_a_timer_repeats(world):
    from engine.logic_timing import LogicTiming
    timer = world.add(LogicTimer(pos=[0, 0, 0],
                                 properties={"name": "timer", "interval": 1.0}))
    world.to_sink(timer, "OnTimer")
    world.world_runtime.timer_things = [timer]
    world.send(timer, "Start")

    for _ in range(3):
        world.timing_runtime.update_logic_timers( 1.0)
    assert world.hits == 3


def test_a_one_shot_timer_stops_itself(world):
    from engine.logic_timing import LogicTiming
    timer = world.add(LogicTimer(pos=[0, 0, 0], properties={
        "name": "timer", "interval": 1.0, "one_shot": True}))
    world.to_sink(timer, "OnTimer")
    world.world_runtime.timer_things = [timer]
    world.send(timer, "Enable")

    for _ in range(4):
        world.timing_runtime.update_logic_timers( 1.0)
    assert world.hits == 1
    assert timer.properties["timer_enabled"] is False


def test_a_one_shot_timer_announces_that_it_finished(world):
    from engine.logic_timing import LogicTiming
    timer = world.add(LogicTimer(pos=[0, 0, 0], properties={
        "name": "timer", "interval": 1.0, "one_shot": True}))
    world.connect(timer, "OnFinished", world.sink, "Fire")
    world.world_runtime.timer_things = [timer]
    world.send(timer, "Enable")
    world.timing_runtime.update_logic_timers( 1.0)
    assert world.hits == 1


def test_a_stopped_timer_does_not_fire(world):
    from engine.logic_timing import LogicTiming
    timer = world.add(LogicTimer(pos=[0, 0, 0],
                                 properties={"name": "timer", "interval": 1.0}))
    world.to_sink(timer, "OnTimer")
    world.world_runtime.timer_things = [timer]
    world.send(timer, "Enable")
    world.send(timer, "Stop")
    world.timing_runtime.update_logic_timers( 5.0)
    assert world.hits == 0


def test_firetimer_fires_immediately(world):
    timer = world.add(LogicTimer(pos=[0, 0, 0],
                                 properties={"name": "timer", "interval": 100.0}))
    world.to_sink(timer, "OnTimer")
    world.send(timer, "FireTimer")
    assert world.hits == 1


def test_resettimer_puts_the_countdown_back(world):
    from engine.logic_timing import LogicTiming
    timer = world.add(LogicTimer(pos=[0, 0, 0],
                                 properties={"name": "timer", "interval": 1.0}))
    world.to_sink(timer, "OnTimer")
    world.world_runtime.timer_things = [timer]
    world.send(timer, "Enable")
    world.timing_runtime.update_logic_timers( 0.9)
    world.send(timer, "ResetTimer")
    world.timing_runtime.update_logic_timers( 0.9)
    assert world.hits == 0


def test_settime_changes_the_interval(world):
    from engine.logic_timing import LogicTiming
    timer = world.add(LogicTimer(pos=[0, 0, 0],
                                 properties={"name": "timer", "interval": 10.0}))
    world.to_sink(timer, "OnTimer")
    world.world_runtime.timer_things = [timer]
    world.send(timer, "SetTime", "0.5")
    world.send(timer, "Enable")
    world.timing_runtime.update_logic_timers( 0.6)
    assert world.hits == 1


def test_timer_state_is_filed_under_the_uuid_not_the_object_address(world):
    """``id(entity)`` cannot be saved and can be reused by an unrelated object."""
    timer = world.add(LogicTimer(pos=[0, 0, 0],
                                 properties={"name": "timer", "interval": 1.0}))
    world.send(timer, "Enable")
    assert set(world.timing_runtime.timer_states) == {timer.properties["id"]}


def test_a_level_of_disabled_timers_does_no_work(world):
    """The one per-frame path in the logic system, and it should stay cheap."""
    from engine.logic_timing import LogicTiming
    timers = [world.add(LogicTimer(pos=[0, 0, 0], properties={"name": "t%d" % i}))
              for i in range(50)]
    world.world_runtime.timer_things = timers
    world.timing_runtime.update_logic_timers( 1.0)
    assert world.timing_runtime.timer_states == {}, (
        "a switched-off timer created countdown state it never needed")
