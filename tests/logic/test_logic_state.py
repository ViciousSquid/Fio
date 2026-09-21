"""The ``LogicState`` entity: storage, events, queries, and what survives.

:mod:`tests.logic.test_state_values` covers what a value *means*; this covers
the entity that holds values — what it accepts, what it refuses, which outputs
a write produces, and what is still there after a save, a level change or a
duplication.

Everything goes through the real I/O path (``fire_output`` -> connection ->
registered handler), because a state primitive that works when called directly
and not when wired up is not working.
"""

import json

import pytest

pytest.importorskip("PyQt5", reason="editor.things needs PyQt5")

from editor import io_system as io                       # noqa: E402
from editor import state_values as sv                    # noqa: E402
from editor.io_system import IOManager, OutputConnection  # noqa: E402
from editor.io_handlers import register_all_input_handlers  # noqa: E402
from editor.things import LogicState, LogicKeyValueStore, Thing  # noqa: E402

pytestmark = pytest.mark.qt

UUID_TEXT = "550e8400-e29b-41d4-a716-446655440000"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class Bench:
    """A store wired into a real I/O manager, recording what it fires.

    ``logic`` stands in for the logic thread: the handlers need
    ``io_manager``, ``brushes``, ``things``, ``gate_inputs`` and
    ``timer_states`` and nothing else, which is a useful measure of how little
    of the engine the logic system reaches into.
    """

    def __init__(self, store_name="world", **props):
        self.manager = IOManager()
        self.manager.set_logic_thread(self)
        self.io_manager = self.manager
        self.brushes = []
        self.things = []
        self.gate_inputs = {}
        self.timer_states = {}
        self.fired = []                # [(output name, payload), ...]

        props.setdefault("store_name", store_name)
        self.store = LogicState(pos=[0, 0, 0], properties=dict(props))
        self.things.append(self.store)
        self.sink = None

        self.manager.set_entity_finder(self._by_name)
        self.manager.set_entity_finder_by_id(self._by_id)
        register_all_input_handlers(self.manager)

        # Record every output the store fires by wrapping the manager's own
        # dispatch, so the recording sees exactly what a connection would.
        real_fire = self.manager.fire_output

        def _recording_fire(entity, output_name, value=None):
            if entity is self.store:
                self.fired.append((output_name, value))
            return real_fire(entity, output_name, value)

        self.manager.fire_output = _recording_fire

    # -- world ------------------------------------------------------------
    def _by_name(self, name):
        for t in self.things:
            if t.properties.get("name") == name:
                return t
        return None

    def _by_id(self, entity_id):
        for t in self.things:
            if t.properties.get("id") == entity_id:
                return t
        return None

    # -- driving ----------------------------------------------------------
    def send(self, input_name, parameter="", source=None):
        """Call one input on the store, the way a connection would."""
        self.fired.clear()
        source = source if source is not None else self.store
        self.manager._execute_input(
            self.store.properties["name"], input_name, parameter,
            self.store.properties["name"],
            target_id=self.store.properties["id"],
            source_entity=source,
            source_id=source.properties.get("id", ""))

    def outputs(self):
        return [name for name, _payload in self.fired]

    def payload(self, output_name):
        for name, value in self.fired:
            if name == output_name:
                return value
        return None


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test gets an empty persistent registry.

    The registry is process-wide by design — that is how a store survives a
    level change — so a test that did not clear it could be made to pass by
    whatever ran before it.
    """
    LogicState._persistent_registry.clear()
    yield
    LogicState._persistent_registry.clear()


@pytest.fixture
def bench():
    return Bench()


# ---------------------------------------------------------------------------
# Identity and naming
# ---------------------------------------------------------------------------

def test_the_old_name_is_the_same_class():
    assert LogicKeyValueStore is LogicState


def test_a_store_carries_a_stable_uuid():
    store = LogicState(pos=[0, 0, 0])
    assert store.properties["id"]
    assert store.properties["id"] != LogicState(pos=[0, 0, 0]).properties["id"]


# ---------------------------------------------------------------------------
# Create / set / get
# ---------------------------------------------------------------------------

def test_setvalue_stores_a_typed_value(bench):
    bench.send("SetValue", "killed=3")
    assert bench.store.get_value("killed") == 3
    assert bench.store.value_type("killed") == "int"


def test_setvalue_with_no_value_sets_the_flag(bench):
    bench.send("SetValue", "door_unlocked")
    assert bench.store.get_value("door_unlocked") == 1


def test_setvalue_can_be_told_the_type(bench):
    bench.send("SetValue", "door_code:string=007")
    assert bench.store.get_value("door_code") == "007"
    assert bench.store.value_type("door_code") == "string"


def test_a_value_that_does_not_fit_its_declared_type_is_refused(bench):
    bench.send("SetValue", "hp:int=banana")
    assert not bench.store.has_key("hp")
    assert "OnStoreFull" in bench.outputs()


def test_a_uuid_value_keeps_its_type(bench):
    bench.send("SetValue", "boss=%s" % UUID_TEXT)
    assert bench.store.get_value("boss") == UUID_TEXT
    assert bench.store.value_type("boss") == "uuid"


def test_a_boolean_value_keeps_its_type(bench):
    bench.send("SetValue", "boss_dead=true")
    assert bench.store.get_value("boss_dead") is True
    assert bench.store.value_type("boss_dead") == "bool"


def test_getvalue_fires_onvalueread_with_the_value(bench):
    bench.store.set_value("stage", 3)
    bench.send("GetValue", "stage")
    assert bench.payload("OnValueRead") == "3"


def test_getvalue_on_a_missing_key_fires_onkeynotfound(bench):
    bench.send("GetValue", "nothing")
    assert bench.outputs() == ["OnKeyNotFound"]


def test_a_blank_key_is_refused(bench):
    assert bench.store.apply_value("  ", 1) == (False, False, None)


# ---------------------------------------------------------------------------
# Changed vs unchanged
# ---------------------------------------------------------------------------

def test_a_first_write_is_a_change(bench):
    bench.send("SetValue", "alarm=1")
    assert bench.outputs() == ["OnValueSet", "OnValueChanged"]


def test_writing_the_same_value_again_is_not_a_change(bench):
    bench.send("SetValue", "alarm=1")
    bench.send("SetValue", "alarm=1")
    assert bench.outputs() == ["OnValueSet"]


def test_writing_a_different_value_is_a_change(bench):
    bench.send("SetValue", "alarm=1")
    bench.send("SetValue", "alarm=2")
    assert bench.outputs() == ["OnValueSet", "OnValueChanged"]


def test_the_same_number_in_a_different_type_is_a_change(bench):
    # True == 1 in Python, and they are not the same state: a chain driven by
    # OnValueChanged has to see a flag becoming a counter.
    bench.store.set_value("x", True)
    bench.send("SetValue", "x:int=1")
    assert "OnValueChanged" in bench.outputs()


def test_onvaluechanged_reports_the_new_value(bench):
    bench.send("SetValue", "stage=2")
    assert bench.payload("OnValueChanged") == "stage=2"


# ---------------------------------------------------------------------------
# Clearing
# ---------------------------------------------------------------------------

def test_clearkey_removes_and_reports(bench):
    bench.store.set_value("flag", True)
    bench.send("ClearKey", "flag")
    assert not bench.store.has_key("flag")
    assert bench.outputs() == ["OnValueCleared", "OnKeyCleared"]


def test_clearing_a_key_that_is_not_there_fires_nothing(bench):
    bench.send("ClearKey", "absent")
    assert bench.outputs() == []


def test_clearall_empties_the_store(bench):
    bench.store.set_value("a", 1)
    bench.store.set_value("b", 2)
    bench.send("ClearAll")
    assert bench.store.get_all_pairs() == {}


def test_a_key_holding_null_still_exists(bench):
    bench.send("SetValue", "flag=null")
    assert bench.store.has_key("flag")
    assert bench.store.get_value("flag") is None


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------

def test_increment_from_nothing_gives_one(bench):
    bench.send("Increment", "killed")
    assert bench.store.get_value("killed") == 1


def test_increment_by_an_amount(bench):
    bench.send("Increment", "killed,5")
    assert bench.store.get_value("killed") == 5


def test_decrement(bench):
    bench.store.set_value("ammo", 10)
    bench.send("Decrement", "ammo,3")
    assert bench.store.get_value("ammo") == 7


def test_increment_reports_the_new_value(bench):
    bench.send("Increment", "killed")
    assert bench.payload("OnValueChanged") == "killed=1"


@pytest.mark.parametrize("input_name,param,start,expected", [
    ("Add", "n,4", 3, 7),
    ("Subtract", "n,4", 3, -1),
    ("Multiply", "n,4", 3, 12),
    ("Divide", "n,2", 7, 3.5),
    ("Min", "n,2", 7, 2),
    ("Max", "n,2", 7, 7),
])
def test_arithmetic_inputs(bench, input_name, param, start, expected):
    bench.store.set_value("n", start)
    bench.send(input_name, param)
    assert bench.store.get_value("n") == expected


def test_clamp(bench):
    bench.store.set_value("n", 42)
    bench.send("Clamp", "n,0,10")
    assert bench.store.get_value("n") == 10


def test_dividing_by_zero_leaves_the_value_alone(bench):
    bench.store.set_value("n", 7)
    bench.send("Divide", "n,0")
    assert bench.store.get_value("n") == 7


def test_toggle_on_a_key_that_was_never_set(bench):
    bench.send("Toggle", "flag")
    assert bench.store.get_value("flag") is True


def test_toggle_inverts(bench):
    bench.store.set_value("flag", True)
    bench.send("Toggle", "flag")
    assert bench.store.get_value("flag") is False


def test_arithmetic_heals_a_legacy_string_counter(bench):
    bench.store._runtime_data["killed"] = "4"      # as a pre-2.4 store held it
    bench.send("Increment", "killed")
    assert bench.store.get_value("killed") == 5


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def test_compare_fires_ontrue(bench):
    bench.store.set_value("killed", 5)
    bench.send("Compare", "killed>=5")
    assert "OnTrue" in bench.outputs()
    assert "OnFalse" not in bench.outputs()


def test_compare_fires_onfalse(bench):
    bench.store.set_value("killed", 4)
    bench.send("Compare", "killed>=5")
    assert "OnFalse" in bench.outputs()


def test_compare_also_fires_the_legacy_output_names(bench):
    bench.store.set_value("killed", 5)
    bench.send("Compare", "killed>=5")
    assert "OnCompareTrue" in bench.outputs()


def test_the_legacy_testvalue_input_still_works(bench):
    bench.store.set_value("killed", 5)
    bench.send("TestValue", "killed>=5")
    assert "OnTrue" in bench.outputs()


def test_comparing_a_missing_key_fires_onkeynotfound(bench):
    bench.send("Compare", "absent>=5")
    assert bench.outputs() == ["OnKeyNotFound"]


def test_compare_carries_the_actual_value(bench):
    bench.store.set_value("killed", 7)
    bench.send("Compare", "killed>=5")
    assert bench.payload("OnTrue") == "7"


def test_compare_against_a_boolean(bench):
    bench.store.set_value("door_unlocked", True)
    bench.send("Compare", "door_unlocked==true")
    assert "OnTrue" in bench.outputs()


def test_exists_and_missing_are_inverses(bench):
    bench.store.set_value("here", 1)
    bench.send("Exists", "here")
    assert "OnTrue" in bench.outputs()
    bench.send("Missing", "here")
    assert "OnFalse" in bench.outputs()
    bench.send("Exists", "gone")
    assert "OnFalse" in bench.outputs()
    bench.send("Missing", "gone")
    assert "OnTrue" in bench.outputs()


def test_a_comparison_with_no_operator_does_nothing(bench):
    bench.send("Compare", "killed")
    assert bench.outputs() == []


# ---------------------------------------------------------------------------
# Copying
# ---------------------------------------------------------------------------

def test_copyvalue_within_a_store(bench):
    bench.store.set_value("a", 5)
    bench.send("CopyValue", "a,b")
    assert bench.store.get_value("b") == 5


def test_copyvalue_from_another_store(bench):
    LogicState._persistent_registry["other"] = {"score": 9}
    bench.send("CopyValue", "other.score,score")
    assert bench.store.get_value("score") == 9


def test_copying_a_missing_key_reports_rather_than_writing(bench):
    bench.send("CopyValue", "nope,here")
    assert not bench.store.has_key("here")
    assert "OnKeyNotFound" in bench.outputs()


def test_copyfrom_copies_a_whole_store(bench):
    LogicState._persistent_registry["other"] = {"a": 1, "b": 2}
    bench.send("CopyFrom", "other")
    assert bench.store.get_value("a") == 1 and bench.store.get_value("b") == 2


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------

def test_the_default_capacity_is_unchanged():
    store = LogicState(pos=[0, 0, 0])
    assert store.capacity == LogicState.MAX_PAIRS == 25


def test_a_full_store_refuses_new_keys_and_says_so(bench):
    bench.store.properties["capacity"] = 2
    bench.send("SetValue", "a=1")
    bench.send("SetValue", "b=2")
    bench.send("SetValue", "c=3")
    assert bench.outputs() == ["OnStoreFull"]
    assert not bench.store.has_key("c")


def test_a_full_store_still_updates_a_key_it_already_has(bench):
    bench.store.properties["capacity"] = 1
    bench.send("SetValue", "a=1")
    bench.send("SetValue", "a=2")
    assert bench.store.get_value("a") == 2


def test_capacity_can_be_raised(bench):
    bench.store.properties["capacity"] = 100
    for i in range(40):
        bench.store.set_value("k%d" % i, i)
    assert bench.store.get_pair_count() == 40


# ---------------------------------------------------------------------------
# Object-local state
# ---------------------------------------------------------------------------

def test_object_local_state_is_keyed_by_uuid(bench):
    monster = Thing(pos=[0, 0, 0], properties={"name": "mon", "type": "monster"})
    bench.things.append(monster)
    bench.send("SetObjectValue", "looted=true", source=monster)
    assert bench.store.get_object_value(monster.properties["id"], "looted") is True


def test_two_objects_keep_separate_state(bench):
    a = Thing(pos=[0, 0, 0], properties={"name": "a", "type": "monster"})
    b = Thing(pos=[0, 0, 0], properties={"name": "b", "type": "monster"})
    bench.things += [a, b]
    bench.send("SetObjectValue", "looted=true", source=a)
    bench.send("SetObjectValue", "looted=false", source=b)
    assert bench.store.get_object_value(a.properties["id"], "looted") is True
    assert bench.store.get_object_value(b.properties["id"], "looted") is False


def test_object_local_state_lives_in_the_same_store(bench):
    """No second database: it is ordinary keys, namespaced by UUID."""
    monster = Thing(pos=[0, 0, 0], properties={"name": "mon", "type": "monster"})
    bench.things.append(monster)
    bench.send("SetObjectValue", "looted=true", source=monster)
    key = LogicState.object_key(monster.properties["id"], "looted")
    assert key in bench.store.get_all_pairs()
    assert LogicState._persistent_registry["world"][key] is True


def test_clearing_one_objects_state_leaves_the_others(bench):
    a = Thing(pos=[0, 0, 0], properties={"name": "a", "type": "monster"})
    b = Thing(pos=[0, 0, 0], properties={"name": "b", "type": "monster"})
    bench.things += [a, b]
    bench.store.properties["capacity"] = 50
    bench.send("SetObjectValue", "looted=true", source=a)
    bench.send("SetObjectValue", "looted=true", source=b)
    bench.store.set_value("world_flag", 1)

    assert bench.store.clear_object_state(a.properties["id"]) == 1
    assert bench.store.get_object_value(b.properties["id"], "looted") is True
    assert bench.store.get_value("world_flag") == 1


def test_object_local_state_follows_the_activator_through_a_relay(bench):
    """A relay in the middle is plumbing, not the thing the state is about."""
    from editor.things import LogicRelay
    monster = Thing(pos=[0, 0, 0], properties={"name": "mon", "type": "monster"})
    relay = LogicRelay(pos=[0, 0, 0], properties={"name": "relay"})
    bench.things += [monster, relay]

    io.add_connection(monster, OutputConnection(
        output_name="OnDeath", target_name="relay", input_name="Trigger",
        target_id=relay.properties["id"]))
    io.add_connection(relay, OutputConnection(
        output_name="OnTrigger", target_name=bench.store.properties["name"],
        input_name="SetObjectValue", parameter="looted=true",
        target_id=bench.store.properties["id"]))

    bench.manager.fire_output(monster, "OnDeath")

    assert bench.store.get_object_value(monster.properties["id"], "looted") is True


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def test_values_reach_the_persistent_registry(bench):
    bench.send("SetValue", "stage=2")
    assert LogicState._persistent_registry["world"]["stage"] == 2


def test_two_stores_sharing_a_name_share_their_values():
    first = LogicState(pos=[0, 0, 0], properties={"store_name": "world"})
    first.set_value("bridge_destroyed", True)
    # A fresh level with a store of the same name: the values come with it.
    second = LogicState(pos=[0, 0, 0], properties={"store_name": "world"})
    assert second.get_value("bridge_destroyed") is True


def test_named_stores_do_not_leak_into_each_other():
    LogicState(pos=[0, 0, 0], properties={"store_name": "a"}).set_value("x", 1)
    other = LogicState(pos=[0, 0, 0], properties={"store_name": "b"})
    assert not other.has_key("x")


def test_initial_data_seeds_a_store_that_does_not_exist_yet():
    store = LogicState(pos=[0, 0, 0], properties={
        "store_name": "fresh", "initial_data": {"stage": 1}})
    assert store.get_value("stage") == 1


def test_a_persisted_store_wins_over_initial_data():
    LogicState._persistent_registry["seeded"] = {"stage": 7}
    store = LogicState(pos=[0, 0, 0], properties={
        "store_name": "seeded", "initial_data": {"stage": 1}})
    assert store.get_value("stage") == 7


def test_a_store_round_trips_through_a_map_file():
    store = LogicState(pos=[0, 0, 0], properties={"store_name": "world"})
    store.set_value("stage", 3)
    store.set_value("flag", True)

    data = json.loads(json.dumps(store.to_dict()))
    LogicState._persistent_registry.clear()
    restored = LogicState.from_dict(data)

    assert restored is not None
    assert restored.get_value("stage") == 3
    assert restored.get_value("flag") is True
    assert restored.properties["id"] == store.properties["id"]


def test_string_values_load_untouched_and_still_work():
    legacy = {
        "type": "logic_state",
        "pos": [0, 0, 0],
        "properties": {"name": "Store_1", "store_name": "world"},
        "runtime_data": {"killed": "4", "flag": "true"},
    }
    store = LogicState.from_dict(legacy)
    assert store.get_value("killed") == "4"      # not rewritten on load
    assert store.compare("killed", ">=", 4) == (True, True)
    assert store.compare("flag", "==", "true") == (True, True)


def test_saving_a_store_is_deterministic():
    store = LogicState(pos=[0, 0, 0], properties={"store_name": "world"})
    for key in ("zeta", "alpha", "mid"):
        store.set_value(key, 1)
    first = json.dumps(store.to_dict()["runtime_data"])
    second = json.dumps(store.to_dict()["runtime_data"])
    assert first == second
    assert list(store.to_dict()["runtime_data"]) == ["alpha", "mid", "zeta"]


def test_a_duplicated_store_gets_a_fresh_uuid():
    store = LogicState(pos=[0, 0, 0], properties={"store_name": "world"})
    clone = store.duplicate(existing_names={store.name})
    assert clone.properties["id"] != store.properties["id"]


# ---------------------------------------------------------------------------
# Plugin registry compatibility
# ---------------------------------------------------------------------------

def test_a_plugin_and_a_map_share_one_store():
    from plugins.api import GlobalStore
    store = LogicState(pos=[0, 0, 0], properties={"store_name": "shared"})
    store.set_value("stage", 3)
    assert GlobalStore().get("stage", store="shared") == "3"


def test_the_plugin_api_still_hands_back_strings():
    """Its documented contract, over a registry that now holds typed values."""
    from plugins.api import GlobalStore
    store = LogicState(pos=[0, 0, 0], properties={"store_name": "shared"})
    store.set_value("flag", True)
    store.set_value("count", 5)
    api = GlobalStore()
    assert api.get("flag", store="shared") == "true"
    assert api.get("count", store="shared") == "5"
    assert api.all(store="shared") == {"flag": "true", "count": "5"}


def test_a_plugin_write_is_visible_to_the_map():
    from plugins.api import GlobalStore
    GlobalStore().set("stage", 4, store="shared")
    store = LogicState(pos=[0, 0, 0], properties={"store_name": "shared"})
    assert store.compare("stage", "==", 4) == (True, True)


def test_a_missing_plugin_key_still_returns_the_default():
    from plugins.api import GlobalStore
    assert GlobalStore().get("absent", "fallback", store="shared") == "fallback"


# ---------------------------------------------------------------------------
# What LogicState is not
# ---------------------------------------------------------------------------

def test_the_state_entity_has_no_per_frame_entry_point():
    """A state primitive that ticks is a gameplay runtime wearing a hat."""
    for forbidden in ("update", "tick", "think", "evaluate_all", "poll"):
        assert not hasattr(LogicState, forbidden), (
            "LogicState.%s exists — state must be evaluated because something "
            "asked, never on a clock" % forbidden)


def test_the_state_entity_knows_about_no_other_system():
    source = open("editor/things.py", encoding="utf-8").read()
    start = source.index("class LogicState(Thing):")
    end = source.index("LogicKeyValueStore = LogicState")
    body = source[start:end].lower()
    # Prose may cite an example chain; code may not reach into these at all.
    for forbidden in ("import monster", "import door", "logicspawner(",
                      "logictimer(", "self.spawn", "def tick"):
        assert forbidden not in body, (
            "LogicState reaches into another system via %r" % forbidden)
