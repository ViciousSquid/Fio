"""A level full of logic, saved and loaded back, unchanged.

The 2.4 logic system puts a lot into a map file: typed state values, connections
carrying parameters and delays, UUID-addressed targets alongside legacy
name-addressed ones, per-object state keyed by UUID, and graph node positions.
Any of it going through JSON wrong is a level that opens subtly different from
the one that was saved — a delay that became a string, a counter that became a
float, a wire pointing at nothing.

So the level built here is deliberately awkward: every logic entity type, wires
of every shape, values of every state type, a connection whose target no longer
exists and one whose input name is a typo. It is saved, loaded, and compared
field by field — and then put through the Logic Graph's Apply, which rewrites
connections across the whole scene and is the most destructive thing a designer
can do to one by accident.
"""

import json

import pytest

pytest.importorskip("PyQt5", reason="editor.things needs PyQt5")

from editor.editor_state import EditorState                # noqa: E402
from editor.io_system import OutputConnection              # noqa: E402
from editor.things import (LogicGate, LogicRelay, LogicState,  # noqa: E402
                           LogicTimer, Monster, Thing)
from tests.helpers.worlds import box_brush                 # noqa: E402

pytestmark = pytest.mark.qt

UUID_TEXT = "550e8400-e29b-41d4-a716-446655440000"


@pytest.fixture(autouse=True)
def _clean_registry():
    LogicState._persistent_registry.clear()
    yield
    LogicState._persistent_registry.clear()


# ---------------------------------------------------------------------------
# The level
# ---------------------------------------------------------------------------

def _wire(entity, output, target_name, input_name, **kw):
    conn = OutputConnection(output_name=output, target_name=target_name,
                            input_name=input_name, **kw)
    if hasattr(entity, "properties"):
        entity.properties.setdefault("_io_connections", []).append(conn)
    else:
        entity.setdefault("_io_connections", []).append(conn)
    return conn


def build_complex_level():
    """A state with a lot of logic in it, in every shape 2.4 supports."""
    state = EditorState()
    state.brushes = []
    state.things = []

    store = LogicState(pos=[0, 0, 0], properties={
        "name": "world_state", "id": "state-1", "store_name": "world",
        "capacity": 64,
        "initial_data": {"stage": 1, "door_unlocked": False, "label": "start"}})
    # Every state type, including one held against an object's UUID.
    store.set_value("killed", 7)
    store.set_value("ratio", 0.25)
    store.set_value("boss_dead", True)
    store.set_value("nothing", None)
    store.set_value("boss_uuid", UUID_TEXT)
    store.set_value("legacy_text", "start")
    store.set_object_value("monster-3", "looted", True)

    relay = LogicRelay(pos=[64, 0, 0], properties={
        "name": "ambush_relay", "id": "relay-1", "fire_once": True})
    gate = LogicGate(pos=[128, 0, 0], properties={
        "name": "both_switches", "id": "gate-1", "logic_type": "AND"})
    timer = LogicTimer(pos=[192, 0, 0], properties={
        "name": "wave_timer", "id": "timer-1", "interval": 2.5,
        "one_shot": True, "start_on": True})
    state.things += [store, relay, gate, timer]

    monsters = []
    for i in range(3):
        monster = Monster(pos=[i * 32, 0, 0], properties={
            "name": "grunt_%d" % i, "id": "monster-%d" % i, "health": 60})
        monsters.append(monster)
    state.things += monsters

    door = box_brush("vault", is_door=True)
    door["id"] = "door-1"
    switch_a = box_brush("switch_a", is_trigger=True)
    switch_a["id"] = "switch-a"
    switch_b = box_brush("switch_b", is_trigger=True)
    switch_b["id"] = "switch-b"
    anonymous = box_brush("", is_trigger=True)
    anonymous["id"] = "anon-1"
    state.brushes += [door, switch_a, switch_b, anonymous]

    # -- wires of every shape ------------------------------------------------
    for monster in monsters:
        _wire(monster, "OnDeath", "world_state", "Increment",
              parameter="killed,1", target_id="state-1")
    _wire(store, "OnValueChanged", "world_state", "Compare",
          parameter="killed>=5", target_id="state-1")
    _wire(store, "OnTrue", "vault", "Open", delay=1.5, target_id="door-1")
    _wire(switch_a, "OnTrigger", "both_switches", "Trigger",
          parameter="a", target_id="gate-1")
    _wire(switch_b, "OnTrigger", "both_switches", "Trigger",
          parameter="b", target_id="gate-1")
    _wire(gate, "OnTrigger", "ambush_relay", "Trigger",
          delay=0.25, fire_once=True, target_id="relay-1")
    _wire(relay, "OnTrigger", "wave_timer", "Enable", target_id="timer-1")
    _wire(timer, "OnTimer", "world_state", "SetValue",
          parameter="stage:int=2", target_id="state-1")
    # Legacy shape: addressed by name only, no target_id.
    _wire(switch_a, "OnStartTouch", "vault", "Close")
    # A connection on an entity with no name at all.
    _wire(anonymous, "OnTrigger", "vault", "Open", target_id="door-1")
    # Awkward on purpose: a target that is not in the map, and an input the
    # door's type does not declare.
    _wire(switch_b, "OnTrigger", "deleted_entity", "Open")
    _wire(switch_b, "OnEndTouch", "vault", "Opne", target_id="door-1")

    state._logic_graph_positions = {
        "state-1": {"x": 100.0, "y": 200.0},
        "relay-1": {"x": 400.0, "y": 200.0},
    }
    return state


def _conn_tuples(entity):
    conns = (entity.properties.get("_io_connections", [])
             if hasattr(entity, "properties") else entity.get("_io_connections", []))
    return [(c.output_name, c.target_name, c.input_name, c.parameter,
             c.delay, c.fire_once, c.target_id) for c in conns]


def _all_conn_tuples(state):
    out = {}
    for entity in list(state.things) + list(state.brushes):
        name = (entity.properties.get("id") if hasattr(entity, "properties")
                else entity.get("id"))
        out[name] = _conn_tuples(entity)
    return out


def round_trip(state):
    """Save to JSON and load back, exactly as the editor does."""
    data = json.loads(json.dumps(state.get_level_data()))
    loaded = EditorState()
    loaded.load_from_data(data)
    return loaded


@pytest.fixture
def level():
    return build_complex_level()


# ---------------------------------------------------------------------------
# Structure survives
# ---------------------------------------------------------------------------

def test_the_level_serialises_to_json_at_all():
    state = build_complex_level()
    text = json.dumps(state.get_level_data())
    assert json.loads(text)


def test_every_entity_comes_back(level):
    loaded = round_trip(level)
    assert len(loaded.things) == len(level.things)
    assert len(loaded.brushes) == len(level.brushes)


def test_every_uuid_survives(level):
    loaded = round_trip(level)
    before = {t.properties["id"] for t in level.things}
    after = {t.properties["id"] for t in loaded.things}
    assert before == after
    assert {b["id"] for b in level.brushes} == {b["id"] for b in loaded.brushes}


def test_no_connection_is_lost(level):
    before = _all_conn_tuples(level)
    after = _all_conn_tuples(round_trip(level))
    assert sum(len(v) for v in after.values()) == sum(len(v) for v in before.values())


def test_every_connection_comes_back_field_for_field(level):
    before = _all_conn_tuples(level)
    after = _all_conn_tuples(round_trip(level))
    assert after == before


def test_delays_stay_floats(level):
    """A delay that came back as a string would fire immediately, or never."""
    loaded = round_trip(level)
    for entity in list(loaded.things) + list(loaded.brushes):
        conns = (entity.properties.get("_io_connections", [])
                 if hasattr(entity, "properties")
                 else entity.get("_io_connections", []))
        for conn in conns:
            assert isinstance(conn.delay, float), (
                "%s.%s delay is %r" % (conn.target_name, conn.input_name, conn.delay))
            assert isinstance(conn.fire_once, bool)


def test_a_name_addressed_connection_stays_name_addressed(level):
    """It is the legacy shape, and rewriting it would change its meaning."""
    loaded = round_trip(level)
    switch = next(b for b in loaded.brushes if b["name"] == "switch_a")
    by_name = [c for c in switch["_io_connections"] if c.output_name == "OnStartTouch"]
    assert len(by_name) == 1
    assert by_name[0].target_id == ""
    assert by_name[0].target_name == "vault"


def test_a_connection_on_an_unnamed_entity_survives(level):
    loaded = round_trip(level)
    anon = next(b for b in loaded.brushes if b["id"] == "anon-1")
    assert len(anon["_io_connections"]) == 1


def test_a_connection_to_a_deleted_target_survives(level):
    """A broken reference is reported, not silently repaired or dropped."""
    loaded = round_trip(level)
    switch = next(b for b in loaded.brushes if b["name"] == "switch_b")
    targets = [c.target_name for c in switch["_io_connections"]]
    assert "deleted_entity" in targets


# ---------------------------------------------------------------------------
# State survives
# ---------------------------------------------------------------------------

def test_state_values_come_back_with_their_types(level):
    loaded = round_trip(level)
    store = next(t for t in loaded.things if t.properties["id"] == "state-1")
    assert store.get_value("killed") == 7 and store.value_type("killed") == "int"
    assert store.get_value("ratio") == 0.25 and store.value_type("ratio") == "float"
    assert store.get_value("boss_dead") is True
    assert store.get_value("nothing") is None
    assert store.get_value("boss_uuid") == UUID_TEXT
    assert store.value_type("boss_uuid") == "uuid"
    assert store.get_value("legacy_text") == "start"


def test_object_local_state_comes_back(level):
    loaded = round_trip(level)
    store = next(t for t in loaded.things if t.properties["id"] == "state-1")
    assert store.get_object_value("monster-3", "looted") is True


def test_the_store_capacity_survives(level):
    loaded = round_trip(level)
    store = next(t for t in loaded.things if t.properties["id"] == "state-1")
    assert store.capacity == 64


def test_logic_entity_properties_survive(level):
    loaded = round_trip(level)
    by_id = {t.properties["id"]: t for t in loaded.things}
    assert by_id["relay-1"].properties["fire_once"] is True
    assert by_id["gate-1"].properties["logic_type"] == "AND"
    assert float(by_id["timer-1"].properties["interval"]) == 2.5
    assert by_id["timer-1"].properties["one_shot"] is True


def test_saving_twice_produces_the_same_bytes(level):
    """A save that differs run to run makes every map file a noisy diff."""
    first = json.dumps(level.get_level_data(), sort_keys=True)
    second = json.dumps(level.get_level_data(), sort_keys=True)
    assert first == second


def test_a_reloaded_level_saves_to_the_same_thing(level):
    """One round trip, then another, must not keep changing the file."""
    once = round_trip(level)
    twice = round_trip(once)
    assert (json.dumps(_all_conn_tuples(twice), sort_keys=True)
            == json.dumps(_all_conn_tuples(once), sort_keys=True))


# ---------------------------------------------------------------------------
# The logic graph must not damage any of it
# ---------------------------------------------------------------------------

def _graph_apply(state):
    from editor.logic_graph_widget import LogicGraphScene
    scene = LogicGraphScene(state)
    scene.apply_to_entities()
    return scene


def test_opening_and_applying_the_graph_changes_nothing(qapp, level):
    """Apply rewrites connections across the whole scene; a no-op edit must be
    a no-op save."""
    before = _all_conn_tuples(level)
    _graph_apply(level)
    after = _all_conn_tuples(level)
    for key in before:
        assert sorted(after[key]) == sorted(before[key]), (
            "entity %s changed on Apply" % key)


def test_the_graph_preserves_what_it_cannot_draw(qapp, level):
    scene = _graph_apply(level)
    assert scene.undrawable_count() >= 3        # deleted target, typo, unnamed
    switch = next(b for b in level.brushes if b["name"] == "switch_b")
    assert "deleted_entity" in [c.target_name for c in switch["_io_connections"]]
    assert "Opne" in [c.input_name for c in switch["_io_connections"]]


def test_a_level_survives_save_graph_apply_save(qapp, level):
    """The whole path a designer actually takes."""
    loaded = round_trip(level)
    before = _all_conn_tuples(loaded)
    _graph_apply(loaded)
    reloaded = round_trip(loaded)
    after = _all_conn_tuples(reloaded)
    for key in before:
        assert sorted(after[key]) == sorted(before[key]), (
            "entity %s changed across save → graph apply → save" % key)


def test_graph_node_positions_survive_the_round_trip(qapp, level):
    from editor.logic_graph_widget import LogicGraphScene
    scene = LogicGraphScene(level)
    scene.auto_arrange()
    positions = dict(level._logic_graph_positions)
    assert positions, "auto_arrange published no positions"

    loaded = round_trip(level)
    assert loaded._logic_graph_positions == positions


def test_state_values_survive_the_graph(qapp, level):
    """Apply touches connections; it must not touch anything else."""
    _graph_apply(level)
    store = next(t for t in level.things if t.properties["id"] == "state-1")
    assert store.get_value("killed") == 7
    assert store.get_object_value("monster-3", "looted") is True


# ---------------------------------------------------------------------------
# At scale
# ---------------------------------------------------------------------------

def build_large_level(entities=120, fan_out=4):
    """A level with hundreds of connections, wired in a long chain plus fan-out.

    Not a stress test for speed — a test that nothing about the round trip is
    quietly O(n^2) in a way that only shows on a real level, and that nothing
    goes missing once there is more than a handful of everything.
    """
    state = EditorState()
    state.brushes = []
    state.things = []

    store = LogicState(pos=[0, 0, 0], properties={
        "name": "big_state", "id": "big-state", "store_name": "big",
        "capacity": 512})
    state.things.append(store)

    relays = []
    for i in range(entities):
        relay = LogicRelay(pos=[i * 8, 0, 0], properties={
            "name": "relay_%03d" % i, "id": "relay-%03d" % i})
        relays.append(relay)
        state.things.append(relay)

    total = 0
    for i, relay in enumerate(relays):
        # A chain: each relay drives the next.
        if i + 1 < len(relays):
            _wire(relay, "OnTrigger", relays[i + 1].name, "Trigger",
                  delay=round(i * 0.01, 3), target_id=relays[i + 1].properties["id"])
            total += 1
        # And fan-out into the store, with a distinct parameter each time.
        for j in range(fan_out):
            _wire(relay, "OnTrigger", "big_state", "Increment",
                  parameter="counter_%d,%d" % (j, i + 1), target_id="big-state")
            total += 1
    return state, total


def test_a_large_level_round_trips_without_losing_a_connection():
    level, expected = build_large_level()
    before = _all_conn_tuples(level)
    assert sum(len(v) for v in before.values()) == expected

    after = _all_conn_tuples(round_trip(level))
    assert after == before


def test_a_large_level_is_stable_across_two_round_trips():
    level, _expected = build_large_level()
    once = round_trip(level)
    twice = round_trip(once)
    assert json.dumps(once.get_level_data(), sort_keys=True) == \
        json.dumps(twice.get_level_data(), sort_keys=True)


def test_a_large_level_survives_the_graph(qapp):
    level, expected = build_large_level()
    before = _all_conn_tuples(level)
    scene = _graph_apply(level)
    assert len(scene._connections) == expected, (
        "the graph drew %d of %d connections" % (len(scene._connections), expected))
    after = _all_conn_tuples(level)
    for key in before:
        assert sorted(after[key]) == sorted(before[key])


@pytest.mark.slow
@pytest.mark.perf
def test_hundreds_of_connections_do_not_make_the_graph_quadratic(qapp):
    """A gate asks how many connections target it; that answer comes from a
    cached index, and a per-signal rescan of the level would show up here."""
    import time
    from editor.logic_graph_widget import LogicGraphScene

    small, _ = build_large_level(entities=40)
    large, _ = build_large_level(entities=160)

    def _build(state):
        start = time.perf_counter()
        LogicGraphScene(state)
        return time.perf_counter() - start

    _build(small)                       # warm any import-time cost
    small_time = min(_build(small) for _ in range(3))
    large_time = min(_build(large) for _ in range(3))

    # Four times the entities. Linear would be ~4x; quadratic ~16x. The bound is
    # loose on purpose — this is a shape check, not a benchmark.
    assert large_time < small_time * 10 + 0.5, (
        "building the graph scaled badly: %.3fs for 40 entities, %.3fs for 160"
        % (small_time, large_time))


@pytest.fixture(scope="module")
def qapp():
    """One QApplication for the graph tests; the widgets need one to exist."""
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    return app
