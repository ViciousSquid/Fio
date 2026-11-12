"""The Decal tool works as terrain stamps do: click to place the image chosen
in the Asset Browser on a surface, Shift-click to select, drag across
surfaces, nudge with the arrow keys, Delete, Escape -- each change one undo
step. Decals are entities but never drawn in the 2D views."""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QEvent, QPoint, Qt                       # noqa: E402
from PyQt5.QtGui import QMouseEvent                               # noqa: E402

from editor.things import Decal                                   # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def tool(main_window, monkeypatch):
    main_window.state.clear_scene()
    tool = main_window.view_3d.decal_tool
    # The surface under the cursor, as the depth readback would find it.
    surfaces = {}
    monkeypatch.setattr(tool, 'surface_at', lambda mx, my: surfaces.get((mx, my)))
    monkeypatch.setattr(tool, 'chosen_image', lambda: 'wet_puddle_01.png')
    tool.surfaces = surfaces
    main_window.toggle_decal_tool(True)
    yield tool
    tool.set_active(False)


def press(tool, x, y, shift=False):
    event = QMouseEvent(QEvent.MouseButtonPress, QPoint(x, y), Qt.LeftButton,
                        Qt.LeftButton, Qt.ShiftModifier if shift else Qt.NoModifier)
    return tool.press(event)


def decals(main_window):
    return [t for t in main_window.state.things if isinstance(t, Decal)]


def test_click_places_shift_click_selects_and_escape_clears_then_leaves(main_window, tool):
    assert tool.active and main_window.asset_browser.tab_textures.decal_btn.isChecked()
    assert main_window.decal_tool_action.isChecked()
    tool.surfaces[(10, 10)] = ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    tool.settings.update(decal_type='wet', width=300.0, height=200.0)
    press(tool, 10, 10)
    placed, = decals(main_window)
    assert placed.properties['texture'] == 'wet_puddle_01.png'
    assert placed.properties['decal_type'] == 'wet'
    assert (placed.properties['width'], placed.properties['height']) == (300.0, 200.0)
    assert placed.properties['pitch'] == pytest.approx(-90.0)        # onto the floor
    assert tool.selected() == [placed] and placed.editor_selected

    press(tool, 10, 10, shift=True)              # shift on a selected one: deselect
    tool.release()
    assert tool.selected() == []
    press(tool, 10, 10, shift=True)
    assert tool.selected() == [placed]
    tool.release()
    tool.escape()
    assert tool.selected() == [] and tool.active
    tool.escape()
    assert not tool.active
    assert not main_window.asset_browser.tab_textures.decal_btn.isChecked()


def test_dragging_moves_the_selection_onto_a_wall_as_one_undo_step(main_window, tool):
    tool.surfaces[(10, 10)] = ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    press(tool, 10, 10)
    placed, = decals(main_window)
    undo_depth = len(main_window.state.undo_stack)
    press(tool, 10, 10)                          # on the selected decal: a drag
    tool.surfaces[(40, 10)] = ((0.0, 64.0, -500.0), (0.0, 0.0, 1.0))   # a wall facing +Z
    tool.drag(40, 10)
    tool.surfaces[(50, 10)] = ((32.0, 64.0, -500.0), (0.0, 0.0, 1.0))
    tool.drag(50, 10)
    tool.release()
    assert placed.pos == [32.0, 64.0, -500.0]
    assert placed.properties['pitch'] == pytest.approx(0.0)
    assert placed.properties['yaw'] == pytest.approx(90.0)          # thrown along -Z
    assert len(main_window.state.undo_stack) == undo_depth + 1


def test_arrow_keys_nudge_on_the_floor_and_along_a_wall(main_window, tool):
    tool.surfaces[(10, 10)] = ((3.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    press(tool, 10, 10)
    floor, = decals(main_window)
    assert tool.nudge(Qt.Key_Right, grid=16.0)
    assert floor.pos == [16.0, 0.0, 0.0]                  # snapped to the grid
    assert tool.nudge(Qt.Key_Up, shift=True, grid=16.0)
    assert floor.pos == [16.0, 0.0, -80.0]                # Up is -Z, Shift x5
    tool.surfaces[(20, 20)] = ((0.0, 64.0, -500.0), (0.0, 0.0, 1.0))
    press(tool, 20, 20)
    wall = tool.selected()[0]
    assert tool.nudge(Qt.Key_Up, grid=16.0)
    assert wall.pos[1] == pytest.approx(80.0)             # up the wall
    tool.nudge(Qt.Key_Right, grid=16.0)
    assert wall.pos[2] == pytest.approx(-500.0)           # along it, not into it


def test_delete_removes_the_selection_and_undo_brings_it_back(main_window, tool):
    tool.surfaces[(10, 10)] = ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    press(tool, 10, 10)
    assert tool.delete_selected()
    assert decals(main_window) == []
    main_window.undo()
    assert len(decals(main_window)) == 1


def test_panel_changes_apply_to_the_selected_decal(main_window, tool):
    tool.surfaces[(10, 10)] = ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    press(tool, 10, 10)
    placed, = decals(main_window)
    panel = tool.panel()
    panel.width_spin.setValue(2048.0)                     # scale it over a whole floor
    panel.type_combo.setCurrentIndex(0)
    assert placed.properties['width'] == 2048.0
    assert placed.properties['decal_type'] == 'image'


def test_decals_are_not_in_the_2d_views(main_window, tool):
    tool.surfaces[(10, 10)] = ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    press(tool, 10, 10)
    placed, = decals(main_window)
    assert main_window.view_top._hidden(placed)


def test_the_menu_item_and_button_turn_it_off_and_clear_the_selection(main_window, tool):
    tool.surfaces[(10, 10)] = ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    press(tool, 10, 10)
    main_window.toggle_decal_tool(False)
    assert not tool.active and tool.selected() == []
    assert not main_window.asset_browser.tab_textures.decal_btn.isChecked()


def test_the_decal_button_shows_textures_decals_and_goes_back_after(main_window, monkeypatch):
    import os
    tab = main_window.asset_browser.tab_textures
    tool = main_window.view_3d.decal_tool
    tool.set_active(False)
    assert not hasattr(main_window, 'decal_btn')            # not on the toolbar any more
    start = os.path.join(tab.root_path, 'Dev')
    tab.load_directory(start)
    tab.decal_btn.click()
    assert tool.active and tab.decal_btn.isChecked()
    assert os.path.abspath(tab.current_asset_folder) == os.path.abspath(tab.decals_path())
    assert 'wet_full.png' in [item.name_text for item in tab.items]
    tab.decal_btn.click()                                   # again: back where we were
    assert not tool.active and not tab.decal_btn.isChecked()
    assert os.path.abspath(tab.current_asset_folder) == os.path.abspath(start)
    # Escape out of the tool goes back too.
    tab.decal_btn.click()
    tool.escape()
    assert os.path.abspath(tab.current_asset_folder) == os.path.abspath(start)
    assert not tab.decal_btn.isChecked()
