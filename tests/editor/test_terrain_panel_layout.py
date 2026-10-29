"""The terrain tools fit a dock with the real application theme and font."""

import pytest

pytest.importorskip("PyQt5")
from PyQt5.QtWidgets import QPushButton, QScrollArea
from PyQt5.QtTest import QTest

from editor.terrain_editor import TerrainEditorPanel
from engine.terrain import Terrain
from main import dark_stylesheet

pytestmark = pytest.mark.qt


@pytest.fixture(params=[11, 16])
def terrain_panel(request, qt_app):
    old_style, old_font = qt_app.styleSheet(), qt_app.font()
    qt_app.setStyleSheet(dark_stylesheet)
    font = qt_app.font()
    font.setPointSize(request.param)
    qt_app.setFont(font)
    panel = TerrainEditorPanel(Terrain())
    panel.resize(720, 450)
    panel.show()
    panel._terrain_tabs.setCurrentIndex(panel._sculpt_stamp_tab_index)
    for _ in range(3):
        qt_app.processEvents()
    QTest.qWait(50)
    qt_app.processEvents()
    try:
        yield panel
    finally:
        panel.close()
        panel.deleteLater()
        qt_app.processEvents()
        qt_app.setStyleSheet(old_style)
        qt_app.setFont(old_font)


def test_actions_fit_the_font_without_large_empty_buttons(terrain_panel):
    panel = terrain_panel
    actions = {"Regenerate", "Reset Defaults", "Close", "Sculpt", "Stamp"}
    buttons = [b for b in panel.findChildren(QPushButton) if b.text() in actions]
    assert len(buttons) == len(actions)
    for button in buttons:
        text_height = button.fontMetrics().height()
        assert text_height <= button.height() <= text_height + 14, button.text()
        assert button.width() < panel.width() / 2, button.text()
        assert button.font().pointSize() == panel.font().pointSize()


def test_hidden_palette_page_does_not_force_horizontal_scrolling(terrain_panel):
    panel = terrain_panel
    scroll = panel.findChild(QScrollArea)
    assert scroll.horizontalScrollBar().maximum() == 0, {
        'scroll': [scroll.width(), scroll.height()], 'frame': scroll.frameWidth(),
        'viewport': [scroll.viewport().width(), scroll.viewport().height()],
        'content': [scroll.widget().width(), scroll.widget().height()],
        'minimum': [scroll.widget().minimumSizeHint().width(), scroll.widget().minimumSizeHint().height()],
        'tabs': [panel._terrain_tabs.width(), panel._terrain_tabs.minimumSizeHint().width()],
    }
    assert panel._terrain_tabs.width() <= scroll.viewport().width()
    assert scroll.verticalScrollBar().maximum() > 0
    for button in [*panel.palette_swatches, panel.palette_add_btn, panel.palette_remove_btn]:
        assert button.width() <= 30
        assert button.height() <= 30
