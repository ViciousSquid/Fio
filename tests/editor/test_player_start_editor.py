"""PlayerStarts in the editor: the primary flag, its labels, Play, persistence.

Every change goes through the editor's own operations -- a checkpoint, the
change, then the selection/UI refresh every operation ends in -- so the
primary invariant, undo and labels are tested as an author meets them.
"""

import json

import pytest

pytest.importorskip("PyQt5", reason="drives the real editor")

from PyQt5.QtCore import QEvent, Qt                       # noqa: E402
from PyQt5.QtGui import QKeyEvent                         # noqa: E402

from editor.editor_state import EditorState               # noqa: E402
from editor.property_editor import PropertyEditor         # noqa: E402
from editor.things import LevelChanger, PlayerStart       # noqa: E402

pytestmark = pytest.mark.qt


def _create(window, name=None, pos=(0, 40, 0)):
    """Create a PlayerStart as the 2D view's context menu does."""
    props = {"name": name} if name is not None else None
    start = PlayerStart(pos=list(pos), properties=props)
    window.save_state()
    window.state.things.append(start)
    window.set_selected_objects([start])
    return start


def _delete(window, *things):
    """Delete with the Delete key, as an author does."""
    window.set_selected_objects(list(things))
    window.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))


def _primaries(window):
    return [s.properties["name"] for s in window.state.player_starts() if s.primary]


@pytest.fixture
def window(main_window):
    main_window.state.clear_scene()
    return main_window


# -- creation ---------------------------------------------------------------------

def test_the_first_start_created_is_primary_and_later_ones_are_not(window):
    first = _create(window, "A")
    assert first.primary
    second = _create(window, "B", (100, 40, 0))
    third = _create(window, "C", (200, 40, 0))
    assert not second.primary and not third.primary
    assert _primaries(window) == ["A"]


def test_a_pasted_copy_of_the_primary_is_not_primary(window):
    first = _create(window, "A")
    window.set_selected_objects([first])
    window.copy_selection()
    window.paste_selection()
    assert len(window.state.player_starts()) == 2
    assert _primaries(window) == ["A"]


# -- changing the primary ---------------------------------------------------------

def test_making_another_start_primary_clears_the_old_one(window):
    a = _create(window, "A")
    b = _create(window, "B", (100, 40, 0))
    window.save_state()
    window.state.set_primary_player_start(b)
    window.update_all_ui()
    assert (a.primary, b.primary) == (False, True)


def test_changing_the_primary_is_undoable(window):
    _create(window, "A")
    b = _create(window, "B", (100, 40, 0))
    window.save_state()
    window.state.set_primary_player_start(b)
    window.update_all_ui()

    window.undo()
    assert _primaries(window) == ["A"]
    window.redo()
    assert _primaries(window) == ["B"]


def test_the_primary_checkbox_moves_the_primary_and_is_undoable(window):
    a = _create(window, "A")
    b = _create(window, "B", (100, 40, 0))
    window.set_selected_objects([b])
    panel = window.property_editor
    box = panel._widgets["player_start_primary"]
    assert not box.isChecked() and box.isEnabled()

    box.setChecked(True)

    assert (a.primary, b.primary) == (False, True)
    rebuilt = panel._widgets["player_start_primary"]
    assert rebuilt.isChecked() and not rebuilt.isEnabled()
    window.undo()
    assert _primaries(window) == ["A"]


# -- deleting ------------------------------------------------------------------

def test_deleting_the_primary_promotes_the_earliest_created_remaining(window):
    a = _create(window, "A")
    _create(window, "B", (100, 40, 0))
    _create(window, "C", (200, 40, 0))
    # Listing order must not matter: put C before B.
    things = window.state.things
    things[1], things[2] = things[2], things[1]

    _delete(window, a)

    assert _primaries(window) == ["B"]
    window.undo()
    assert _primaries(window) == ["A"]


def test_deleting_every_start_leaves_no_primary_and_creates_none(window):
    a = _create(window, "A")
    b = _create(window, "B", (100, 40, 0))
    _delete(window, a, b)
    assert window.state.player_starts() == []
    assert window.state.resolve_player_start() is None


def test_reordering_the_entities_does_not_change_the_primary(window):
    _create(window, "A")
    b = _create(window, "B", (100, 40, 0))
    window.save_state()
    window.state.set_primary_player_start(b)
    window.update_all_ui()
    window.state.things.reverse()
    window.update_all_ui()
    assert _primaries(window) == ["B"]
    assert window.state.resolve_player_start() is b


# -- labels --------------------------------------------------------------------

def test_labels_show_the_name_and_primary_status(window):
    named_primary = _create(window, "DungeonEntrance")
    named = _create(window, "Side", (100, 40, 0))
    assert named_primary.editor_label_lines() == ("DungeonEntrance", "Primary")
    assert named.editor_label_lines() == ("Side",)
    named_primary.properties["name"] = ""
    named.properties["name"] = ""
    assert named_primary.editor_label_lines() == ("Primary",)
    assert named.editor_label_lines() == ()


def _top_view_image(window, qt_app):
    view = window.view_top
    view.resize(400, 400)
    window.center_2d_views_on([0.0, 40.0, 0.0])
    qt_app.processEvents()
    return view.grab().toImage()


def test_labels_are_drawn_and_follow_changes_immediately(window, qt_app):
    start = _create(window, "")
    start.properties["name"] = ""
    window.save_state()
    window.state.set_primary_player_start(start)
    window.update_all_ui()
    other = _create(window, "", (0, 40, 0))
    other.properties["name"] = ""
    window.set_selected_objects([])
    # Two starts at one spot; only the primary label differs between frames.
    with_primary = _top_view_image(window, qt_app)

    start.properties["primary"] = False
    window.state.mark_world_changed([start])
    window.update_views()
    no_label = _top_view_image(window, qt_app)
    assert with_primary != no_label, "the Primary label was not drawn"

    start.properties["name"] = "Renamed"
    window.update_views()
    renamed = _top_view_image(window, qt_app)
    assert renamed != no_label, "the name label did not follow the rename"


def test_monsters_keep_their_name_label():
    from editor.things import Monster
    assert Monster(properties={"name": "grunt"}).editor_label_lines() == ("grunt",)


# -- Play ------------------------------------------------------------------------

def test_play_starts_at_the_primary_start(window):
    _create(window, "A", (0, 40, 0))
    b = _create(window, "B", (320, 40, -160))
    window.save_state()
    window.state.set_primary_player_start(b)
    window.update_all_ui()
    window.state.things.reverse()

    window.enter_play_mode()
    try:
        player = window.view_3d.player
        assert (float(player.pos.x), float(player.pos.z)) == (320.0, -160.0)
        assert window.view_3d.logic_thread.session_runtime.spawn_start is b
    finally:
        window.enter_play_mode()


def test_play_without_a_start_still_refuses(window, monkeypatch):
    from PyQt5.QtWidgets import QMessageBox
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a: warned.append(a[1])))
    window.enter_play_mode()
    assert warned == ["No Player Start"]
    assert not window.view_3d.play_mode


# -- persistence ----------------------------------------------------------------

def test_names_primary_and_destination_spawn_survive_save_and_reload(window):
    _create(window, "HubStart")
    entrance = _create(window, "DungeonEntrance", (100, 40, 0))
    window.save_state()
    window.state.set_primary_player_start(entrance)
    changer = LevelChanger(pos=[0, 40, 200], properties={
        "name": "ToDungeon", "target_map": "maps/dungeon.json",
        "destination_spawn": "DungeonStart"})
    window.state.things.append(changer)
    window.update_all_ui()

    data = json.loads(json.dumps(window.state.get_level_data()))
    reloaded = EditorState()
    reloaded.load_from_data(data)

    starts = {s.properties["name"]: s for s in reloaded.player_starts()}
    assert set(starts) == {"HubStart", "DungeonEntrance"}
    assert (starts["HubStart"].primary, starts["DungeonEntrance"].primary) == (False, True)
    changer = next(t for t in reloaded.things if isinstance(t, LevelChanger))
    assert changer.properties["destination_spawn"] == "DungeonStart"
    assert reloaded.get_level_data() == data


def test_an_old_map_gets_exactly_one_primary_on_load(window):
    window.state.load_from_data({"version": 3, "brushes": [], "things": [
        {"type": "playerstart", "pos": [i * 10, 0, 0],
         "properties": {"type": "playerstart", "name": f"S{i}"}} for i in range(3)]})
    assert _primaries(window) == ["S0"]


def test_a_malformed_primary_is_refused():
    with pytest.raises(ValueError, match="primary"):
        EditorState.validate_level_data({"things": [
            {"type": "playerstart", "pos": [0, 0, 0], "properties": {"primary": "yes"}}]})
    with pytest.raises(ValueError, match="destination_spawn"):
        EditorState.validate_level_data({"things": [
            {"type": "levelchanger", "pos": [0, 0, 0], "properties": {"destination_spawn": 3}}]})


# -- the LevelChanger panel ------------------------------------------------------

@pytest.fixture
def dungeon_map():
    """A map in maps/ with three named starts; removed afterwards."""
    import os
    import uuid
    from editor.property_editor import _project_root
    name = "_test_dungeon_%s.json" % uuid.uuid4().hex[:8]
    path = os.path.join(_project_root(), "maps", name)
    starts = [("Side", False, 2), ("DungeonStart", True, 0), ("Back", False, 1)]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"version": 3, "brushes": [], "things": [
            {"type": "playerstart", "pos": [i * 64, 40, 0], "properties": {
                "type": "playerstart", "name": n, "primary": p, "creation_index": c}}
            for i, (n, p, c) in enumerate(starts)]}, handle)
    try:
        yield "maps/" + name
    finally:
        os.remove(path)


def test_destination_spawn_choices_are_the_target_maps_starts(dungeon_map):
    assert PropertyEditor.destination_spawn_choices(dungeon_map) == [
        ("<Primary>", ""), ("DungeonStart", "DungeonStart"),
        ("Back", "Back"), ("Side", "Side")]
    # A bare map name resolves as the LevelChanger resolves it.
    bare = dungeon_map[len("maps/"):-len(".json")]
    assert PropertyEditor.destination_spawn_choices(bare)[1] == ("DungeonStart", "DungeonStart")


def test_a_missing_destination_spawn_is_kept_and_marked(dungeon_map):
    assert PropertyEditor.destination_spawn_choices("maps/no_such_map.json", "Gone") == [
        ("<Primary>", ""), ("Gone (missing)", "Gone")]
    assert PropertyEditor.destination_spawn_choices(dungeon_map, "Gone")[-1] == (
        "Gone (missing)", "Gone")


def test_the_destination_spawn_dropdown_stores_the_start_name(window, dungeon_map):
    changer = LevelChanger(pos=[0, 40, 0], properties={"target_map": dungeon_map})
    window.save_state()
    window.state.things.append(changer)
    window.set_selected_objects([changer])
    combo = window.property_editor._widgets["destination_spawn"]
    assert [combo.itemText(i) for i in range(combo.count())] == [
        "<Primary>", "DungeonStart", "Back", "Side"]
    assert combo.currentIndex() == 0

    combo.setCurrentIndex(combo.findData("Back"))
    assert changer.properties["destination_spawn"] == "Back"
    combo.setCurrentIndex(0)
    assert changer.properties["destination_spawn"] == ""


def test_retargeting_the_map_refreshes_the_destination_choices(window, dungeon_map):
    changer = LevelChanger(pos=[0, 40, 0], properties={"target_map": "maps/no_such_map.json"})
    window.save_state()
    window.state.things.append(changer)
    window.set_selected_objects([changer])
    panel = window.property_editor
    assert panel._widgets["destination_spawn"].count() == 1
    panel.update_object_prop("target_map", dungeon_map)
    assert panel._widgets["destination_spawn"].count() == 4
