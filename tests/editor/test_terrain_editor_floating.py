"""The Terrain Editor's top-left button pops the panel out into a floating
window and docks it again."""
import pytest

pytestmark = pytest.mark.qt


@pytest.fixture
def editor(main_window):
    from engine.terrain import Terrain

    window = main_window
    window.terrain = Terrain(seed=42)
    window.state.terrain_data = window.terrain.to_dict()
    window._show_terrain_editor_panel()
    return window, window.terrain_editor_window


def test_the_float_button_is_first_in_the_top_row(editor):
    _window, panel = editor
    top_row = panel.layout().itemAt(0).layout()
    assert top_row.itemAt(0).widget() is panel.float_btn
    assert "floating window" in panel.float_btn.toolTip()


def test_the_button_floats_the_panel_and_gives_the_dock_back(editor):
    window, panel = editor
    properties = window._original_properties_widget
    assert window.properties_dock.widget() is panel and not panel.isWindow()
    panel._terrain_tabs.setCurrentIndex(panel._sculpt_stamp_tab_index)

    panel.float_btn.click()

    assert panel.isWindow() and panel.isVisible()
    assert panel.windowTitle() == "Terrain Editor"
    assert window.terrain_editor_floating()
    assert window.terrain_editor_window is panel, "it is the same panel"
    assert window.properties_dock.widget() is properties, (
        "the Properties pane did not come back")
    assert window._current_overlay is None
    assert panel._terrain_tabs.currentIndex() == panel._sculpt_stamp_tab_index
    assert "Dock" in panel.float_btn.toolTip()


def test_the_button_docks_it_again(editor):
    window, panel = editor
    panel.float_btn.click()

    panel.float_btn.click()

    assert not panel.isWindow()
    assert window.properties_dock.widget() is panel
    assert window._current_overlay is panel
    assert window.terrain_editor_window is panel


def test_the_window_s_close_box_docks_it(editor):
    window, panel = editor
    panel.float_btn.click()

    panel.close()

    assert window.terrain_editor_window is panel and not panel.isWindow()
    assert window.properties_dock.widget() is panel


def test_the_terrain_brush_stays_up_across_floating(editor):
    window, panel = editor
    window.view_3d.set_terrain_sculpt_active(True)

    panel.float_btn.click()
    assert window.view_3d.terrain_sculpt_active
    panel.float_btn.click()
    assert window.view_3d.terrain_sculpt_active
    window.view_3d.set_terrain_sculpt_active(False)


def test_close_closes_a_floating_editor(editor):
    window, panel = editor
    properties = window._original_properties_widget
    panel.float_btn.click()

    panel.request_close()

    assert window.terrain_editor_window is None
    assert not panel.isVisible()
    assert window.properties_dock.widget() is properties


def test_another_overlay_leaves_a_floating_editor_alone(editor):
    from PyQt5.QtWidgets import QWidget
    window, panel = editor
    panel.float_btn.click()

    window._show_overlay(QWidget())
    window._close_current_overlay()

    assert window.terrain_editor_window is panel and panel.isVisible()
    panel.request_close()


def test_opening_the_editor_again_raises_the_floating_window(editor):
    window, panel = editor
    panel.float_btn.click()

    window._show_terrain_editor_panel()

    assert window.terrain_editor_window is panel and panel.isWindow()
    assert window.properties_dock.widget() is not panel
    panel.request_close()
