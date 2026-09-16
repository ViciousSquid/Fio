"""The Property Manager's view of a ``LogicState`` store.

The panel used to show two disjoint lists — designer defaults above, live values
below — so a designer asking "what is this key right now?" had to read both and
work out which one won, and a key the map created at run time had no row at all.
It is one table now, one row per key, with a state saying where the value came
from.

The reconciliation is a static method precisely so it can be tested without
building a widget: these assert the view model, not the Qt.
"""

import pytest

pytest.importorskip("PyQt5", reason="the property editor is Qt")

from editor.property_editor import PropertyEditor      # noqa: E402
from editor.things import LogicState                   # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture(autouse=True)
def _clean_registry():
    LogicState._persistent_registry.clear()
    yield
    LogicState._persistent_registry.clear()


def _store(initial=None, live=None, store_name="panel"):
    store = LogicState(pos=[0, 0, 0], properties={
        "store_name": store_name, "initial_data": dict(initial or {})})
    if live is not None:
        store._runtime_data = dict(live)
    return store


def _rows(store):
    return {row[0]: row for row in PropertyEditor._state_rows(store)}


# ---------------------------------------------------------------------------
# Value state
# ---------------------------------------------------------------------------

def test_a_designer_default_that_play_never_touched_reads_as_default():
    store = _store(initial={"stage": 1}, live={})
    assert _rows(store)["stage"] == ("stage", "int", "1", "default")


def test_a_live_value_equal_to_the_default_reads_as_set():
    store = _store(initial={"stage": 1}, live={"stage": 1})
    assert _rows(store)["stage"][3] == "set"


def test_a_live_value_that_differs_reads_as_changed():
    store = _store(initial={"stage": 1}, live={"stage": 4})
    row = _rows(store)["stage"]
    assert row[2] == "4" and row[3] == "changed"


def test_a_key_created_during_play_still_gets_a_row():
    """The values that actually drive a level were the ones you could not see."""
    store = _store(initial={}, live={"killed": 3})
    assert _rows(store)["killed"] == ("killed", "int", "3", "runtime")


def test_defaults_and_runtime_keys_appear_together():
    store = _store(initial={"stage": 1}, live={"killed": 3})
    assert set(_rows(store)) == {"stage", "killed"}


def test_a_legacy_string_default_matching_a_typed_live_value_is_not_a_change():
    """The old store wrote "1"; the live one holds 1.  Same value, and a row
    that called that "changed" would cry wolf on every legacy map."""
    store = _store(initial={"stage": "1"}, live={"stage": 1})
    assert _rows(store)["stage"][3] == "set"


# ---------------------------------------------------------------------------
# Type column
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,type_name,shown", [
    (5, "int", "5"),
    (1.5, "float", "1.5"),
    (True, "bool", "true"),
    (None, "null", "null"),
    ("text", "string", "text"),
    ("550e8400-e29b-41d4-a716-446655440000", "uuid",
     "550e8400-e29b-41d4-a716-446655440000"),
])
def test_the_type_column_is_derived_from_the_value(value, type_name, shown):
    store = _store(live={"k": value})
    assert _rows(store)["k"][1:3] == (type_name, shown)


# ---------------------------------------------------------------------------
# Ordering and key naming
# ---------------------------------------------------------------------------

def test_rows_are_sorted_so_the_table_is_stable():
    store = _store(live={"zeta": 1, "alpha": 2, "mid": 3})
    assert [row[0] for row in PropertyEditor._state_rows(store)] == \
        ["alpha", "mid", "zeta"]


def test_an_empty_store_has_no_rows():
    assert PropertyEditor._state_rows(_store()) == []


def test_adding_a_key_twice_does_not_collide():
    """The panel inserted a literal ``new_key`` every time, and the second one
    silently replaced the first on write-back."""
    assert PropertyEditor._unused_state_key([]) == "new_key"
    assert PropertyEditor._unused_state_key(["new_key"]) == "new_key_2"
    assert PropertyEditor._unused_state_key(["new_key", "new_key_2"]) == "new_key_3"


def test_an_unused_key_ignores_unrelated_names():
    assert PropertyEditor._unused_state_key(["stage", "killed"]) == "new_key"


# ---------------------------------------------------------------------------
# Object-local keys are visible like anything else
# ---------------------------------------------------------------------------

def test_object_local_state_shows_up_in_the_table():
    key = LogicState.object_key("550e8400-e29b-41d4-a716-446655440000", "looted")
    store = _store(live={key: True})
    assert _rows(store)[key] == (key, "bool", "true", "runtime")


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------

def test_the_panel_reads_the_stores_capacity_not_a_constant():
    store = _store()
    store.properties["capacity"] = 64
    assert store.capacity == 64


def test_a_store_with_no_capacity_property_uses_the_historic_default():
    assert _store().capacity == 25
