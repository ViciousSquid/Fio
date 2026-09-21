"""``EditorState``: the world every other Fio subsystem reads.

The brush list and the thing list *are* the world — the editor, the logic
thread, the renderer and plugins all hold the same two lists.  These cover what
that shared object promises: entity creation and lookup, stable ids, the
selection, clearing the scene, and the legacy-map migrations that let an old
file open with its wiring intact.

Undo and redo have their own suite (``tests/undo_redo``); persistence has
``tests/persistence``.
"""

import pytest

pytest.importorskip("PyQt5", reason="EditorState builds editor.things entities")

from editor import io_system as io                 # noqa: E402
from editor.editor_state import EditorState        # noqa: E402
from editor.io_system import OutputConnection      # noqa: E402
from editor.things import Light, LogicRelay, Monster  # noqa: E402
from engine import brush_geometry as bg            # noqa: E402
from tests.helpers.worlds import box_brush, make_thing  # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def state():
    return EditorState()


@pytest.fixture
def populated(state):
    """A scene with two brushes and two entities, all named and identified."""
    state.brushes = [box_brush("floor", (0, -16, 0), (512, 32, 512)),
                     box_brush("door", (0, 64, 0), (64, 128, 16), is_door=True)]
    state.things = [make_thing(Light, "lamp", (0, 100, 0)),
                    make_thing(LogicRelay, "relay")]
    return state


# ---------------------------------------------------------------------------
# Creation and identity
# ---------------------------------------------------------------------------

def test_a_new_state_is_empty_but_has_an_undo_baseline(state):
    assert state.brushes == []
    assert state.things == []
    assert state.selected_object is None
    assert len(state.undo_stack) == 1, (
        "a fresh scene should hold exactly one checkpoint (the empty state), "
        "so the first undo has somewhere to go back to; it holds %d"
        % len(state.undo_stack))


def test_every_entity_gets_a_stable_id(state):
    thing = make_thing(Light, "lamp")
    assert thing.properties["id"], "a Thing was created with no id"
    brush = box_brush("wall")
    assert brush["id"], "a brush fixture was created with no id"


def test_two_entities_never_share_an_id():
    first = Light(pos=[0, 0, 0])
    second = Light(pos=[0, 0, 0])
    assert first.properties["id"] != second.properties["id"], (
        "two Lights were handed the same id %r" % first.properties["id"])


def test_two_entities_of_a_type_get_distinct_default_names():
    first, second = Light(), Light()
    assert first.name != second.name, (
        "both Lights are named %r; name-addressed I/O would be ambiguous"
        % first.name)


def test_a_duplicate_gets_its_own_identity(populated):
    original = populated.things[0]
    clone = original.duplicate(existing_names=["lamp"])

    assert clone.properties["id"] != original.properties["id"], \
        "the duplicate reused the original's id"
    assert clone.name != original.name, \
        "the duplicate reused the name %r" % clone.name
    assert clone.properties is not original.properties, (
        "the duplicate shares the original's properties dict; editing one "
        "would edit both")
    clone.properties["intensity"] = 99
    assert original.properties.get("intensity") != 99


def test_duplicating_repeatedly_keeps_names_unique(populated):
    original = populated.things[0]
    taken = {"lamp"}
    names = []
    for _ in range(4):
        clone = original.duplicate(existing_names=taken)
        taken.add(clone.name)
        names.append(clone.name)
    assert len(set(names)) == 4, "duplicate names among %s" % (names,)


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------

def test_an_entity_is_findable_by_name(populated):
    assert populated.find_entity_by_name("door") is populated.brushes[1]
    assert populated.find_entity_by_name("lamp") is populated.things[0]


def test_an_entity_is_findable_by_id(populated):
    door = populated.brushes[1]
    assert populated.find_entity_by_id(door["id"]) is door
    lamp = populated.things[0]
    assert populated.find_entity_by_id(lamp.properties["id"]) is lamp


def test_looking_up_a_name_that_is_not_there_returns_none(populated):
    assert populated.find_entity_by_name("nothing_called_this") is None
    assert populated.find_entity_by_name("") is None


def test_looking_up_an_id_that_is_not_there_returns_none(populated):
    assert populated.find_entity_by_id("not-a-real-uuid") is None


def test_get_entity_id_works_for_both_entity_shapes(populated):
    assert populated.get_entity_id(populated.brushes[0]) == populated.brushes[0]["id"]
    assert populated.get_entity_id(populated.things[0]) == \
        populated.things[0].properties["id"]


def test_every_name_in_the_scene_is_reported(populated):
    names = set(populated.get_all_entity_names())
    assert {"floor", "door", "lamp", "relay"} <= names, (
        "get_all_entity_names missed some: %s" % (sorted(names),))


def test_a_deleted_entity_is_no_longer_findable(populated):
    door = populated.brushes[1]
    populated.brushes.remove(door)
    assert populated.find_entity_by_name("door") is None
    assert populated.find_entity_by_id(door["id"]) is None


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def test_selecting_one_object_sets_both_selection_fields(populated):
    brush = populated.brushes[0]
    populated.set_selected_object(brush)
    assert populated.selected_object is brush
    assert populated.selected_objects == [brush], (
        "the single and multi selection must agree; multi is %s"
        % (populated.selected_objects,))


def test_deselecting_clears_both_selection_fields(populated):
    populated.set_selected_object(populated.brushes[0])
    populated.set_selected_object(None)
    assert populated.selected_object is None
    assert populated.selected_objects == []


def test_a_selection_survives_a_checkpoint_and_restore(populated):
    brush = populated.brushes[0]
    populated.set_selected_object(brush)
    snapshot = populated.snapshot()

    populated.set_selected_object(None)
    populated.restore_state(snapshot)

    assert len(populated.selected_objects) == 1
    assert populated.selected_objects[0]["id"] == brush["id"], (
        "the restored selection points at %r, expected the brush with id %r"
        % (populated.selected_objects[0].get("name"), brush["id"]))


def test_a_restored_selection_points_at_the_live_objects(populated):
    """Restore rebuilds the brush dicts; the selection must follow the new ones."""
    brush = populated.brushes[0]
    populated.set_selected_object(brush)
    snapshot = populated.snapshot()
    populated.restore_state(snapshot)

    assert populated.selected_objects[0] is populated.brushes[0], (
        "the selection holds an object that is not in the scene - every later "
        "edit would be applied to a brush nobody can see")
    assert populated.selected_objects[0] is not brush, (
        "restore_state is expected to rebuild the brush dicts")


def test_selecting_an_object_that_was_deleted_leaves_an_empty_selection(populated):
    brush = populated.brushes[0]
    populated.set_selected_object(brush)
    snapshot = populated.snapshot()
    populated.brushes.remove(brush)
    populated.save_state()

    populated.restore_state(populated.snapshot())

    assert all(b in populated.brushes for b in populated.selected_objects), (
        "the selection references an object outside the scene: %s"
        % ([b.get("name") for b in populated.selected_objects],))


# ---------------------------------------------------------------------------
# Clearing
# ---------------------------------------------------------------------------

def test_clearing_the_scene_empties_everything(populated):
    populated.set_selected_object(populated.brushes[0])
    populated.clear_scene()

    assert populated.brushes == []
    assert populated.things == []
    assert populated.selected_object is None
    assert populated.selected_objects == []
    assert populated.terrain_data is None


def test_clearing_the_scene_drops_the_history(populated):
    populated.save_state()
    populated.save_state()
    populated.clear_scene()
    assert len(populated.undo_stack) == 1, (
        "a cleared scene should start a fresh history, it has %d entries"
        % len(populated.undo_stack))
    assert list(populated.redo_stack) == []


def test_clearing_the_scene_invalidates_the_io_index(populated):
    """Anything caching per-object data must be told the objects are gone."""
    before = io.io_revision()
    populated.clear_scene()
    assert io.io_revision() != before, (
        "clear_scene left the I/O revision at %d; a cached 'targeted by' panel "
        "would go on listing entities that no longer exist" % before)


# ---------------------------------------------------------------------------
# Geometry operations on the state
# ---------------------------------------------------------------------------

def test_clipping_through_the_state_makes_the_brush_angled(state):
    brush = box_brush("b")
    state.brushes = [brush]
    assert state.brush_is_angled(brush) is False

    assert state.clip_brush(brush, (0, 1, 0), 0.0) is True
    assert state.brush_is_angled(brush) is True
    assert bg.get_convex(brush).bounds[1][1] == pytest.approx(0.0)


def test_a_clip_that_changes_nothing_leaves_no_undo_step(state):
    brush = box_brush("b")
    state.brushes = [brush]
    depth = len(state.undo_stack)

    assert state.clip_brush(brush, (1, 0, 0), 5000.0) is False

    assert len(state.undo_stack) == depth, (
        "a clip that cut nothing left an empty step in the history "
        "(%d -> %d)" % (depth, len(state.undo_stack)))


def test_resetting_a_brush_to_a_box_drops_its_geometry(state):
    brush = box_brush("b")
    state.brushes = [brush]
    state.clip_brush(brush, (0, 1, 0), 0.0)

    state.reset_brush_to_box(brush)

    assert "geometry" not in brush
    assert "_geo_cache" not in brush, (
        "the derived geometry cache outlived the plane set it was built from")


def test_simplify_demotes_a_box_shaped_geometry_brush(state):
    """A rotated-back brush is a box again; keeping the plane set costs the
    renderer its axis-aligned fast path for nothing."""
    brush = box_brush("b")
    state.brushes = [brush]
    bg.box_to_geometry(brush)
    assert bg.brush_has_geometry(brush)

    state.simplify_brush_geometry(brush)

    assert "geometry" not in brush, (
        "a brush whose plane set describes a plain axis-aligned box should be "
        "demoted back to pos/size")


def test_simplify_leaves_a_genuinely_angled_brush_alone(state):
    brush = box_brush("b")
    state.brushes = [brush]
    state.clip_brush(brush, (1, 1, 0), 10.0)
    state.simplify_brush_geometry(brush)
    assert bg.brush_has_geometry(brush), "an angled brush lost its plane set"


def test_marking_a_brush_static_marks_the_lighting_dirty(state):
    brush = box_brush("b")
    state.brushes = [brush]
    state.mark_brush_static(brush, True)
    assert brush["lightmap_static"] is True
    assert state.count_static_brushes() == 1
    assert state.get_static_brushes() == [brush]


# ---------------------------------------------------------------------------
# Legacy maps
# ---------------------------------------------------------------------------

def test_a_map_with_no_ids_gets_them_backfilled(state):
    state.load_from_data({
        "version": 1,
        "brushes": [{"pos": [0, 0, 0], "size": [64, 64, 64], "name": "old"}],
        "things": [],
    })
    brush = state.brushes[0]
    assert brush.get("id"), "a legacy brush was loaded with no stable id"
    assert state.find_entity_by_id(brush["id"]) is brush


def test_loading_a_map_replaces_rather_than_appends(populated):
    populated.load_from_data({"version": 3, "brushes": [], "things": []})
    assert populated.brushes == [] and populated.things == [], (
        "loading a map appended to the old scene instead of replacing it")


def test_loading_a_map_invalidates_the_io_index(populated):
    before = io.io_revision()
    populated.load_from_data({"version": 3, "brushes": [], "things": []})
    assert io.io_revision() != before


# ---------------------------------------------------------------------------
# Sources targeting an entity
# ---------------------------------------------------------------------------

def test_find_entities_targeting_reports_the_source(populated):
    button = box_brush("button", is_trigger=True)
    populated.brushes.append(button)
    door = populated.find_entity_by_name("door")
    io.add_connection(button, OutputConnection(
        output_name="OnTrigger", target_name="door", input_name="Open",
        target_id=door["id"]))

    sources = populated.find_entities_targeting("door", door["id"])

    assert len(sources) == 1, (
        "expected exactly one source targeting the door; got %s"
        % ([s["entity"].get("name") for s in sources],))
    assert sources[0]["entity"] is button
    assert sources[0]["type"] == "brush"
    assert sources[0]["connection"].output_name == "OnTrigger"


def test_find_entities_targeting_matches_by_name_for_a_legacy_connection(
        populated):
    """A connection with no id must still be found by the target's name."""
    button = box_brush("legacy_button", is_trigger=True)
    populated.brushes.append(button)
    io.add_connection(button, OutputConnection(
        output_name="OnTrigger", target_name="door", input_name="Open"))

    sources = populated.find_entities_targeting("door", "")

    assert [s["entity"].get("name") for s in sources] == ["legacy_button"]


def test_renaming_the_target_breaks_only_the_name_addressed_connection(populated):
    """Which is exactly why connections carry an id as well."""
    door = populated.find_entity_by_name("door")
    by_id = box_brush("by_id", is_trigger=True)
    by_name = box_brush("by_name", is_trigger=True)
    populated.brushes += [by_id, by_name]
    io.add_connection(by_id, OutputConnection(
        output_name="OnTrigger", target_name="door", input_name="Open",
        target_id=door["id"]))
    io.add_connection(by_name, OutputConnection(
        output_name="OnTrigger", target_name="door", input_name="Open"))

    door["name"] = "renamed_door"

    still_wired = [s["entity"].get("name")
                   for s in populated.find_entities_targeting("renamed_door",
                                                              door["id"])]
    assert still_wired == ["by_id"], (
        "after the rename the id-addressed connection should still resolve and "
        "the name-addressed one should not; resolved %s" % (still_wired,))


def test_find_entities_targeting_nothing_is_empty(populated):
    assert populated.find_entities_targeting("door", populated.brushes[1]["id"]) == []
