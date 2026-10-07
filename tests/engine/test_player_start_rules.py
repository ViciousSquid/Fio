"""The PlayerStart rules: one explicit primary, chosen by creation order.

``engine.player_starts`` works on property dicts, so the editor, the logic
thread and the standalone player all apply these same rules.
"""

import copy

from engine.player_starts import (
    pick_start, resolve_start_record, settle, start_names, start_records,
)


def _created(starts, **props):
    """A start just created after *starts*, as the editor then settles it."""
    start = {"name": props.pop("name", f"PlayerStart_{len(starts) + 1}"), "primary": False}
    start.update(props)
    starts.append(start)
    settle(starts)
    return start


def _primaries(starts):
    return [s["name"] for s in starts if s["primary"] is True]


def test_the_first_start_created_becomes_primary_and_later_ones_do_not():
    starts = []
    first = _created(starts)
    assert first["primary"] is True
    second = _created(starts)
    third = _created(starts)
    assert (second["primary"], third["primary"]) == (False, False)
    assert [s["creation_index"] for s in starts] == [0, 1, 2]
    assert _primaries(starts) == ["PlayerStart_1"]


def test_there_is_never_more_than_one_primary():
    starts = [{"name": n, "primary": True, "creation_index": i}
              for i, n in enumerate(("A", "B", "C"))]
    settle(starts)
    assert _primaries(starts) == ["A"]


def test_a_copy_of_the_primary_is_numbered_anew_and_is_not_primary():
    starts = []
    original = _created(starts, name="Hub")
    _created(starts, name="Side")
    starts.append(copy.deepcopy(original) | {"name": "Hub_copy"})
    settle(starts)
    assert _primaries(starts) == ["Hub"]
    assert starts[-1]["creation_index"] == 2


def test_deleting_the_primary_promotes_the_earliest_created_remaining():
    starts = []
    for name in ("A", "B", "C"):
        _created(starts, name=name)
    # Listed out of creation order: creation order decides, not the list.
    starts = [starts[2], starts[1]]
    settle(starts)
    assert _primaries(starts) == ["B"]


def test_deleting_every_start_leaves_no_primary():
    starts = []
    assert settle(starts) == []
    assert pick_start(starts) is None


def test_reordering_starts_does_not_change_the_primary():
    starts = []
    for name in ("A", "B", "C"):
        _created(starts, name=name)
    starts[1]["primary"], starts[0]["primary"] = True, False   # B made primary
    settle(starts)
    starts.reverse()
    assert settle(starts) == []
    assert _primaries(starts) == ["B"]


def test_a_map_from_before_primary_existed_keeps_its_first_start_as_the_spawn():
    starts = [{"name": "First"}, {"name": "Second"}]
    changed = settle(starts)
    assert changed == [0, 1]
    assert _primaries(starts) == ["First"]
    assert [s["creation_index"] for s in starts] == [0, 1]
    assert settle(starts) == []          # settled once, stable after


def test_pick_by_name_finds_that_start_and_never_another():
    starts = []
    for name in ("Hub", "DungeonEntrance"):
        _created(starts, name=name)
    assert pick_start(starts) == 0
    assert pick_start(starts, "DungeonEntrance") == 1
    assert pick_start(starts, "Nowhere") is None


def test_an_unsettled_level_spawns_where_settling_would_make_primary():
    starts = [{"name": "B", "creation_index": 5}, {"name": "A", "creation_index": 2}]
    assert pick_start(starts) == 1


def _map(*starts):
    return {"things": [{"type": "playerstart", "pos": [i, 0, 0], "properties": dict(p)}
                       for i, p in enumerate(starts)]
            + [{"type": "light", "pos": [0, 0, 0], "properties": {"name": "Hub"}}]}


def test_map_records_resolve_by_the_same_rules():
    level = _map({"name": "Side", "primary": False, "creation_index": 1},
                 {"name": "Hub", "primary": True, "creation_index": 0})
    assert len(start_records(level)) == 2
    assert resolve_start_record(level)["properties"]["name"] == "Hub"
    assert resolve_start_record(level, "Side")["pos"] == [0, 0, 0]
    assert resolve_start_record(level, "Missing") is None
    assert resolve_start_record({"things": []}) is None


def test_start_names_list_the_primary_first_then_creation_order():
    level = _map({"name": "C", "creation_index": 2},
                 {"name": "B", "primary": True, "creation_index": 1},
                 {"name": "A", "creation_index": 0},
                 {"name": "", "creation_index": 3})
    assert start_names(level) == ["B", "A", "C"]
