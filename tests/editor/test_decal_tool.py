"""The Decal tool works as terrain stamps do: click to place the image chosen
in the Asset Browser on a surface, Shift-click to select, drag across
surfaces, nudge with the arrow keys, Delete, Escape -- each change one undo
step. Decals are entities, marked in the 2D views with the DECAL button's
icon."""

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
    tool.settings.update(width=300.0, height=200.0)
    press(tool, 10, 10)
    placed, = decals(main_window)
    assert placed.properties['texture'] == 'wet_puddle_01.png'
    assert 'decal_type' not in placed.properties             # the name decides
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
    assert placed.properties['width'] == 2048.0
    assert not hasattr(panel, 'type_combo')               # no Image / Wet setting


def test_decals_are_in_the_2d_views_with_the_decal_button_icon(main_window, tool):
    tool.surfaces[(10, 10)] = ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    press(tool, 10, 10)
    placed, = decals(main_window)
    assert not main_window.view_top._hidden(placed)
    icon = placed.get_icon_pixmap()
    assert icon is not None and not icon.isNull()
    import os
    from engine.asset_qt import load_pixmap
    from tests.helpers.paths import REPO_ROOT
    button = load_pixmap(os.path.join(REPO_ROOT, "assets", "decal_button.png"))
    assert not button.isNull()
    assert icon.toImage() == button.toImage()        # the DECAL button's own icon


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
    assert tab.decal_btn.size() == tab.tint_btn.size()      # side by side, one size
    gap = tab.decal_btn.geometry().left() - tab.tint_btn.geometry().right() - 1
    assert gap == tab.SIDE_BUTTON_GAP
    start = os.path.join(tab.root_path, 'Dev')
    tab.load_directory(start)
    tab.decal_btn.click()
    assert tool.active and tab.decal_btn.isChecked()
    assert os.path.abspath(tab.current_asset_folder) == os.path.abspath(tab.decals_path())
    names = [item.name_text for item in tab.items]
    assert 'wet_full.png' in names
    # Blood (gore's own images) always last, after the decals you place.
    blood = [n.startswith('blood_') for n in names]
    assert any(blood) and blood == sorted(blood)
    tab.decal_btn.click()                                   # again: back where we were
    assert not tool.active and not tab.decal_btn.isChecked()
    assert os.path.abspath(tab.current_asset_folder) == os.path.abspath(start)
    # Escape out of the tool goes back too.
    tab.decal_btn.click()
    tool.escape()
    assert os.path.abspath(tab.current_asset_folder) == os.path.abspath(start)
    assert not tab.decal_btn.isChecked()


def test_the_tint_and_decal_buttons_have_a_size_menu_kept_in_settings(main_window, monkeypatch):
    from PyQt5.QtCore import QPoint
    from PyQt5.QtWidgets import QMenu
    from editor.ui import TOOLBAR_ICON_SIZES
    tab = main_window.asset_browser.tab_textures
    monkeypatch.setattr(main_window, 'save_config', lambda: None)
    config = main_window.config
    big_toolbar = config.get('Display', 'big_toolbar_buttons', fallback=None)
    main_window.config.remove_option('AssetBrowser', 'asset_browser_toolbar_size') if (
        config.has_option('AssetBrowser', 'asset_browser_toolbar_size')) else None
    tab.apply_side_button_size(tab.side_button_size())
    small = TOOLBAR_ICON_SIZES[False]
    assert tab.side_button_size() == 'small'
    assert tab.tint_btn.iconSize().height() == small        # the toolbar's small icon
    assert tab.tint_btn.height() == small + 8
    assert tab.tint_btn.size() == tab.decal_btn.size()
    menu = tab.side_button_menu()
    labels = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert labels[-2:] == ['Small', 'Large']
    next(a for a in menu.actions() if a.text() == 'Large').trigger()
    large = TOOLBAR_ICON_SIZES[True]
    assert config.get('AssetBrowser', 'asset_browser_toolbar_size') == 'large'
    assert tab.tint_btn.height() == large + 8 and tab.decal_btn.iconSize().height() == large
    assert tab.tint_btn.size() == tab.decal_btn.size()
    assert tab.action_bar.height() >= tab.decal_btn.height() + 4   # not clipped
    # The toolbar's own setting is untouched.
    assert config.get('Display', 'big_toolbar_buttons', fallback=None) == big_toolbar
    # Only the empty space offers it, not the buttons.
    shown = []
    monkeypatch.setattr(QMenu, 'exec_', lambda self, *a: shown.append(self))
    tab._side_button_menu(tab.decal_btn.geometry().center())
    assert shown == []
    empty = QPoint(tab.inspector_btn.geometry().left() - 20, tab.inspector_btn.geometry().center().y())
    tab._side_button_menu(empty)
    assert len(shown) == 1
    tab.set_side_button_size('small')


@pytest.mark.parametrize('key', [Qt.Key_Backspace, Qt.Key_Delete])
def test_backspace_and_delete_remove_the_selection_wherever_the_keyboard_is(
        main_window, tool, key):
    from PyQt5.QtGui import QKeyEvent
    tool.surfaces[(10, 10)] = ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    press(tool, 10, 10)
    assert len(decals(main_window)) == 1
    # Not the 3D view: the main window (the Asset Browser, the panel) gets it.
    main_window.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
    assert decals(main_window) == []
    # And in the 3D view, as before.
    press(tool, 10, 10)
    main_window.view_3d.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))
    assert decals(main_window) == []


def test_an_unselected_decal_stays_in_the_level(main_window, tool):
    tool.surfaces[(10, 10)] = ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    press(tool, 10, 10)
    tool.escape()                              # clears the selection
    tool.escape()                              # leaves the tool
    assert not tool.active and len(decals(main_window)) == 1


# -- size: the picked image's, and its shape kept -----------------------------

def test_picking_an_image_sets_the_size_new_decals_get(main_window, tool):
    """Placing: the panel's Width and Height become the picked image's pixel
    size (a 512x64 banner: 512 by 64 units), and can be changed."""
    from editor.decal_tool import image_size
    assert image_size('Decal02.jpg') == (512, 64)
    tool.image_chosen('Decal02.jpg')
    assert (tool.settings['width'], tool.settings['height']) == (512.0, 64.0)
    panel = tool.panel()
    assert (panel.width_spin.value(), panel.height_spin.value()) == (512.0, 64.0)
    tool.keep_aspect = False
    panel.height_spin.setValue(100.0)                     # changed by hand
    assert (tool.settings['width'], tool.settings['height']) == (512.0, 100.0)


def test_picking_an_image_in_the_asset_browser_tells_the_tool(main_window, tool, monkeypatch):
    seen = []
    monkeypatch.setattr(tool, 'image_chosen', seen.append)
    tab = main_window.asset_browser.tab_textures
    item = type('Tile', (), {'file_path': 'assets/textures/decals/Banner01.png'})()
    monkeypatch.setattr(tab.model, 'refresh_path', lambda *_: None)
    monkeypatch.setattr(tab, 'stop_preview', lambda *_: None, raising=False)
    tab.select_item(item)
    assert seen == ['assets/textures/decals/Banner01.png']
    tool.set_active(False)
    tab.select_item(None)
    tab.select_item(item)                                 # tool off: not told
    assert seen == ['assets/textures/decals/Banner01.png']


def test_a_placed_decal_keeps_its_size_when_another_image_is_picked(main_window, tool):
    tool.surfaces[(10, 10)] = ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    tool.settings.update(width=300.0, height=200.0)
    press(tool, 10, 10)
    placed, = decals(main_window)
    tool.image_chosen('Decal02.jpg')
    assert (placed.properties['width'], placed.properties['height']) == (300.0, 200.0)


def test_keep_aspect_ratio_is_on_and_resizing_keeps_the_shape(main_window, tool):
    panel = tool.panel()
    assert tool.keep_aspect and panel.keep_aspect_cb.isChecked()
    tool.surfaces[(10, 10)] = ((0.0, 0.0, 0.0), (0.0, 1.0, 0.0))
    tool.settings.update(width=400.0, height=100.0)
    press(tool, 10, 10)
    placed, = decals(main_window)                         # selected: 4:1
    panel.width_spin.setValue(200.0)
    assert (placed.properties['width'], placed.properties['height']) == (200.0, 50.0)
    assert panel.height_spin.value() == 50.0
    panel.height_spin.setValue(80.0)
    assert (placed.properties['width'], placed.properties['height']) == (320.0, 80.0)
    panel.keep_aspect_cb.setChecked(False)                # off: one side alone
    panel.width_spin.setValue(100.0)
    assert (placed.properties['width'], placed.properties['height']) == (100.0, 80.0)
