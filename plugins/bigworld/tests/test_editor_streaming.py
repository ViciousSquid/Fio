"""Camera residency must never turn unloading into an authored deletion."""

import pytest

pytest.importorskip('PyQt5')
from editor.editor_state import EditorState
from editor.things import Light

pytestmark = pytest.mark.qt


def level():
    return {
        'version': 3,
        'brushes': [
            {'id': 'near-brush', 'name': 'Near wall', 'pos': [0, 0, 0], 'size': [64, 64, 64]},
            {'id': 'far-brush', 'name': 'Far wall', 'pos': [8192, 0, 0], 'size': [64, 64, 64]},
            {'id': 'spanning', 'name': 'Long road', 'pos': [4096, -64, 0], 'size': [8448, 32, 64]},
        ],
        'things': [
            {'type': 'bigworldsettings', 'pos': [0, 0, 0], 'properties': {
                'id': 'settings', 'type': 'bigworldsettings', 'activation_radius': 512,
                'deactivation_radius': 768}},
            {'type': 'playerstart', 'pos': [0, 40, 0], 'properties': {'id': 'start'}},
            {'type': 'light', 'pos': [0, 100, 0], 'properties': {'id': 'near-light', 'radius': 128}},
            {'type': 'light', 'pos': [8192, 100, 0], 'properties': {'id': 'far-light', 'radius': 128}},
        ],
    }


def load():
    state = EditorState()
    state.load_from_data(level(), stream_editor=True)
    return state


def ids(state):
    return {b['id'] for b in state.brushes} | {t.properties['id'] for t in state.things}


def test_only_camera_local_objects_are_instantiated_and_stationary_ticks_do_no_work(monkeypatch):
    state = load()
    assert 'near-brush' in ids(state)
    assert 'far-brush' not in ids(state)
    assert 'far-light' not in ids(state)
    monkeypatch.setattr(state.editor_stream, 'merge', lambda *args: pytest.fail('stationary scan'))
    assert not state.stream_editor_camera([1, 40, 1])


def test_crossing_cells_releases_old_objects_and_loads_new_ones_without_duplicate_spanning_brushes():
    state = load()
    assert state.stream_editor_camera([8192, 40, 0])
    assert 'far-brush' in ids(state)
    assert 'near-brush' not in ids(state)
    assert sum(b['id'] == 'spanning' for b in state.brushes) == 1
    assert 'start' in ids(state)


def test_edits_additions_and_deletions_survive_unload_save_and_reload():
    state = load()
    state.save_state()
    state.find_entity_by_id('near-brush')['name'] = 'Edited wall'
    state.things = [t for t in state.things if t.properties['id'] != 'near-light']
    added = Light(pos=[0, 100, 0])
    state.things.append(added)
    state.stream_editor_camera([8192, 40, 0])
    saved = state.get_level_data()
    assert len(saved['brushes']) == 3
    assert next(b for b in saved['brushes'] if b['id'] == 'near-brush')['name'] == 'Edited wall'
    assert 'near-light' not in {r['properties']['id'] for r in saved['things']}
    assert added.properties['id'] in {r['properties']['id'] for r in saved['things']}
    assert next(b for b in saved['brushes'] if b['id'] == 'far-brush') == level()['brushes'][1]
    state.load_from_data(saved, stream_editor=True)
    assert state.find_entity_by_id('near-brush')['name'] == 'Edited wall'
    assert added.properties['id'] in ids(state)


def test_selection_stays_live_across_camera_moves_and_history_restores_the_whole_map():
    state = load()
    wall = state.find_entity_by_id('near-brush')
    state.selected_objects = [wall]
    state.save_state()
    wall['name'] = 'Edited'
    state.stream_editor_camera([8192, 40, 0])
    assert state.find_entity_by_id('near-brush') is wall
    assert state.undo()
    assert state.selected_objects[0]['name'] == 'Near wall'
    assert len(state.get_level_data()['brushes']) == 3
    assert state.redo()
    assert state.selected_objects[0]['name'] == 'Edited'
    assert len(state.get_level_data()['brushes']) == 3


def test_play_handoff_retains_the_full_world_and_editor_can_resume_locally():
    state = load()
    checkpoint = state.snapshot()
    state.materialize_editor_world()
    assert {'near-brush', 'far-brush', 'near-light', 'far-light'} <= ids(state)
    assert state.editor_stream.suspended
    state.restore_state(checkpoint)
    assert not state.editor_stream.suspended
    assert 'far-brush' not in ids(state)
    assert len(state.get_level_data()['brushes']) == 3


def test_plain_maps_and_disabled_editor_streaming_keep_the_normal_load_path():
    data = level()
    data['things'][0]['properties']['editor_streaming'] = False
    state = EditorState()
    state.load_from_data(data, stream_editor=True)
    assert state.editor_stream is None
    assert 'far-brush' in ids(state)


def test_distant_objects_can_be_found_and_edited_without_loading_all_cells():
    state = load()
    assert 'Far wall' in state.get_all_entity_names()
    wall = state.find_entity_by_name('Far wall')
    assert wall['id'] == 'far-brush'
    assert 'far-light' not in ids(state)
    state.save_state()
    wall['name'] = 'Renamed distant wall'
    state.stream_editor_camera([1, 40, 1])
    saved = state.get_level_data()
    assert next(b for b in saved['brushes'] if b['id'] == 'far-brush')['name'] == 'Renamed distant wall'


def test_lookup_does_not_resurrect_a_deleted_resident_object():
    state = load()
    state.brushes = [b for b in state.brushes if b['id'] != 'near-brush']
    assert state.find_entity_by_id('near-brush') is None
    assert state.find_entity_by_name('Near wall') is None


def test_disabling_editor_streaming_restores_the_complete_world():
    state = load()
    state.save_state()
    state.find_entity_by_id('settings').properties['editor_streaming'] = False
    assert state.stream_editor_camera([1, 40, 1])
    assert state.editor_stream is None
    assert 'far-light' in ids(state)


def test_hierarchy_search_includes_unloaded_entity_types(main_window):
    main_window.state.load_from_data(level(), stream_editor=True)
    found = main_window.scene_hierarchy.find_matches('light')
    assert {t.properties['id'] for t in found} == {'near-light', 'far-light'}


def test_temporary_wizard_actors_remain_live_and_do_not_leak_into_saved_maps():
    state = load()
    actor = Light(pos=[0, 0, 0], properties={'_cutscene_temporary': True})
    state.save_state()
    state.things.append(actor)
    state.stream_editor_camera([8192, 40, 0])
    assert any(t is actor for t in state.things)
    saved_ids = {r['properties']['id'] for r in state.get_level_data()['things']}
    assert actor.properties['id'] not in saved_ids


def test_whole_world_toggle_keeps_edits_and_history_and_returns_to_local_cells(main_window):
    main_window.state.load_from_data(level(), stream_editor=True)
    state = main_window.state
    checkbox = main_window.show_whole_world_checkbox
    assert checkbox.text() == 'Show whole world'
    assert checkbox.x() > main_window.display_mode_combobox.x()
    checkbox.setChecked(False)
    history = list(state.undo_stack)
    checkbox.setChecked(True)
    assert state.editor_stream.whole_world
    assert 'far-brush' in ids(state)
    state.find_entity_by_id('far-brush')['name'] = 'Edited while showing all'
    assert not state.stream_editor_camera([0, 40, 0])
    checkpoint = state.snapshot()
    state.restore_state(checkpoint)
    assert state.editor_stream.whole_world
    checkbox.setChecked(False)
    assert not state.editor_stream.whole_world
    assert 'far-brush' not in ids(state)
    saved = state.get_level_data()
    assert next(b for b in saved['brushes'] if b['id'] == 'far-brush')['name'] == 'Edited while showing all'
    assert list(state.undo_stack) == history
    assert main_window.config.getboolean('Display', 'show_whole_world') is False


@pytest.mark.parametrize('show_whole_world', [True, False])
def test_map_load_shows_the_whole_world_by_default_and_honours_opt_out(main_window, show_whole_world):
    checkbox = main_window.show_whole_world_checkbox
    assert checkbox.isChecked()
    if not show_whole_world:
        checkbox.setChecked(False)
    assert main_window._load_level(level())
    assert main_window.state.editor_stream.whole_world is show_whole_world
    assert ('far-brush' in ids(main_window.state)) is show_whole_world
