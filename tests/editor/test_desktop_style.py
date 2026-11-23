"""Platform themes must not change editor alignment, fonts or button order."""

import os
from pathlib import Path
import subprocess
import sys

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QFont, QFontInfo
from PyQt5.QtWidgets import (QDialogButtonBox, QFileDialog, QFormLayout, QLabel, QMessageBox, QSplitter, QStyle,
                             QTabWidget, QWidget)

from editor.desktop_style import MONO_FONT, UI_FONT, keep_message_box_titles, message_box_size
from main import dark_stylesheet

# Its message boxes are real ones: on Windows the Qt shard runner runs this
# module on the windows platform (Qt 5.15.2's offscreen one crashes in
# QMessageBox's show; see tools/run_qt_suite_shard.py).
pytestmark = [pytest.mark.qt, pytest.mark.native_message_box]


def test_tabs_stay_at_the_left_even_when_a_dialog_has_its_own_stylesheet(qt_app):
    old = qt_app.styleSheet()
    qt_app.setStyleSheet(dark_stylesheet)
    tabs = QTabWidget()
    tabs.setStyleSheet('QTabBar::tab { padding: 6px 10px; background: #425f5d; }')
    for name in ('General', 'Appearance', 'I/O'):
        tabs.addTab(QLabel(name), name)
    tabs.resize(700, 300)
    tabs.show()
    qt_app.processEvents()
    try:
        assert tabs.tabBar().geometry().left() <= 4
        assert tabs.tabBar().geometry().right() < tabs.width() - 100
    finally:
        tabs.close()
        tabs.deleteLater()
        qt_app.setStyleSheet(old)


def test_new_forms_and_button_boxes_use_windows_alignment(qt_app):
    form = QFormLayout()
    assert form.formAlignment() == Qt.AlignLeft | Qt.AlignTop
    assert form.labelAlignment() == Qt.AlignLeft | Qt.AlignVCenter
    assert qt_app.style().styleHint(QStyle.SH_DialogButtonLayout) == QDialogButtonBox.WinLayout
    for family in (UI_FONT, MONO_FONT):
        assert QFontInfo(QFont(family, 11)).family() == family


def test_deferred_pages_get_explicit_alignment_properties(qt_app):
    tabs = QTabWidget()
    page = QWidget()
    form = QFormLayout(page)
    form.addRow('Name', QLabel('Value'))
    form.setFormAlignment(Qt.AlignCenter)
    form.setLabelAlignment(Qt.AlignRight)
    tabs.addTab(page, 'Deferred page')
    tabs.show()
    qt_app.processEvents()
    try:
        assert form.formAlignment() == Qt.AlignLeft | Qt.AlignTop
        assert form.labelAlignment() == Qt.AlignLeft | Qt.AlignVCenter
        assert not tabs.tabBar().expanding()
    finally:
        tabs.close()
        tabs.deleteLater()


def test_spacing_and_indicator_metrics_are_explicit(qt_app):
    style = qt_app.style()
    assert style.pixelMetric(QStyle.PM_LayoutLeftMargin) == 9
    assert style.pixelMetric(QStyle.PM_LayoutHorizontalSpacing) == 6
    assert style.pixelMetric(QStyle.PM_IndicatorWidth) == 16
    assert style.pixelMetric(QStyle.PM_ScrollBarExtent) == 12



def test_dock_title_buttons_are_the_same_size_on_every_platform(qt_app):
    # Unpinned, Qt scales this margin from 72 dpi on macOS (21px buttons)
    # and 96 dpi elsewhere (20px).
    from PyQt5.QtCore import QRect
    from PyQt5.QtWidgets import QStyleOptionDockWidget
    style = qt_app.style()
    assert style.pixelMetric(QStyle.PM_DockWidgetTitleBarButtonMargin) == 4
    option = QStyleOptionDockWidget()
    option.rect = QRect(0, 0, 400, 28)
    option.closable = option.floatable = True
    for element in (QStyle.SE_DockWidgetCloseButton, QStyle.SE_DockWidgetFloatButton):
        rect = style.subElementRect(element, option, None)
        assert (rect.width(), rect.height()) == (20, 20)

@pytest.mark.parametrize('points,pixels,height', [(11, 15, 20), (16, 21, 28)])
def test_native_widget_fonts_use_windows_logical_metrics(qt_app, points, pixels, height):
    label = QLabel('Native widget metrics')
    label.setFont(QFont(UI_FONT, points))
    label.show()
    qt_app.processEvents()
    try:
        assert QFontInfo(label.font()).pixelSize() == pixels
        assert label.fontMetrics().height() == height
    finally:
        label.close()
        label.deleteLater()


def test_label_height_tracks_font_changes_without_staying_enlarged(qt_app):
    label = QLabel('Label text')
    label.show()
    qt_app.processEvents()
    for size in (16, 11):
        font = label.font()
        font.setPointSize(size)
        label.setFont(font)
        qt_app.processEvents()
        assert label.minimumHeight() == label.fontMetrics().height()
    label.close()
    label.deleteLater()


def test_message_box_keeps_content_and_buttons_in_the_shared_layout(qt_app):
    box = QMessageBox(QMessageBox.Information, 'Title', 'Message body', QMessageBox.Ok | QMessageBox.Cancel)
    box.setInformativeText('Additional explanation')
    box.show()
    qt_app.processEvents()
    try:
        assert box.contentsMargins().left() == 0
        assert box.layout().getContentsMargins() == (9, 9, 9, 9)
        assert box.findChild(QLabel, 'qt_msgbox_label').isVisible()
        assert box.findChild(QLabel, 'qt_msgbox_informativelabel').isVisible()
        buttons = box.findChild(QDialogButtonBox)
        row, column, _, span = box.layout().getItemPosition(box.layout().indexOf(buttons))
        assert column == 0 and span == box.layout().columnCount()
        box.button(QMessageBox.Ok).click()
        assert box.result() == QMessageBox.Ok
    finally:
        box.close()
        box.deleteLater()


def test_a_message_box_is_tall_enough_for_all_of_its_text(qt_app):
    """The one-line floor on labels replaced the label's own minimum, and a
    message box is sized from minimums: Debug > Project Overview showed the
    map's name and none of its counts."""
    from editor.project_overview import OVERVIEW_ROWS, format_overview

    stats = {key: 1 for row in OVERVIEW_ROWS if row for key in row[1:]}
    stats.update(created='today', modified='today')
    box = QMessageBox()
    box.setTextFormat(Qt.RichText)
    box.setText(format_overview(stats, 'map.json'))
    box.show()
    qt_app.processEvents()
    try:
        label = box.findChild(QLabel, 'qt_msgbox_label')
        rows = sum(1 for row in OVERVIEW_ROWS if row)
        assert label.height() >= label.sizeHint().height(), (
            'the report is cut off: %d of %d px' % (label.height(), label.sizeHint().height()))
        assert label.height() > rows * label.fontMetrics().height()
    finally:
        box.close()
        box.deleteLater()

    plain = QMessageBox(QMessageBox.Warning, 'Title', 'First line.\n\nSecond.\nThird.', QMessageBox.Ok)
    plain.show()
    qt_app.processEvents()
    try:
        label = plain.findChild(QLabel, 'qt_msgbox_label')
        assert label.height() >= 4 * label.fontMetrics().height()
    finally:
        plain.close()
        plain.deleteLater()


def test_message_box_uses_the_windows_wrap_width_after_any_platform_sizing(qt_app):
    # Qt wraps message text at 420px on macOS and 500px elsewhere.
    box = QMessageBox(QMessageBox.Warning, 'Title', 'A long explanation of a problem. ' * 30, QMessageBox.Ok)
    box.setInformativeText('Further detail that also wraps. ' * 12)
    box.show()
    qt_app.processEvents()
    try:
        expected = message_box_size(box)
        assert box.size() == expected
        box.setFixedSize(expected.width() - 80, expected.height() + 40)
        qt_app.processEvents()
        assert box.size() == expected
    finally:
        box.close()
        box.deleteLater()


def test_message_box_titles_survive_every_entry_point(qt_app, monkeypatch):
    # Qt 5 drops QMessageBox titles on macOS, inside its constructor and
    # static helpers alike; the editor restores them on that platform.
    seen = []

    def untitled_question(*args, **kwargs):
        box = QMessageBox()
        box.show()
        qt_app.processEvents()
        seen.append(box.windowTitle())
        box.close()
        box.deleteLater()
        return QMessageBox.No

    for name in ('__init__', 'setWindowTitle', 'information', 'warning', 'critical', 'about'):
        monkeypatch.setattr(QMessageBox, name, getattr(QMessageBox, name))
    monkeypatch.setattr(QMessageBox, 'question', staticmethod(untitled_question))
    monkeypatch.setattr(QMessageBox, '_fio_titles_kept', False, raising=False)
    keep_message_box_titles()
    assert QMessageBox.question(None, 'Delete Map', 'Delete it?') == QMessageBox.No
    assert seen == ['Delete Map']
    box = QMessageBox(QMessageBox.Information, 'Project Overview', 'Text')
    assert box.windowTitle() == 'Project Overview'
    box.setWindowTitle('Renamed')
    assert box.windowTitle() == 'Renamed'
    box.deleteLater()


def test_file_picker_size_does_not_depend_on_sidebar_entries(qt_app):
    # Qt names the first sidebar entry "My Computer" on Windows and
    # "Computer" elsewhere, and sizes the dialog from it unless overridden.
    dialog = QFileDialog(None, 'Open Map', 'maps', 'Maps (*.json)')
    dialog.show()
    qt_app.processEvents()
    try:
        line = dialog.fontMetrics().height()
        available = dialog.screen().availableGeometry().size() - (dialog.frameGeometry().size() - dialog.size())
        if available.width() >= 36 * line and available.height() >= 24 * line:
            assert (dialog.width(), dialog.height()) == (36 * line, 24 * line)
        else:
            # A work area smaller than the picker (the 512x339 macOS runner at
            # scale 2): the window manager may shrink it further still.
            assert dialog.width() <= available.width() and dialog.height() <= available.height()
        assert dialog.findChild(QSplitter, 'splitter').sizes()[0] == 7 * line
    finally:
        dialog.reject()
        dialog.deleteLater()

def test_fresh_startup_converts_points_to_windows_logical_pixels():
    result = subprocess.run([sys.executable, '-c', '''
from editor.desktop_style import prepare_qt, install_editor_style, set_editor_font_size
prepare_qt()
from PyQt5.QtWidgets import QApplication, QLabel
from PyQt5.QtGui import QFont, QFontInfo
app = QApplication([])
install_editor_style(app)
assert QFontInfo(QFont("Fio UI", 11)).pixelSize() == 15
assert QFontInfo(QFont("Fio UI", 16)).pixelSize() == 21
from main import dark_stylesheet
app.setStyleSheet(dark_stylesheet)
label = QLabel("Styled text")
label.show()
for points, pixels, height in ((16, 21, 28), (11, 15, 20)):
    set_editor_font_size(app, points)
    app.processEvents()
    assert QFontInfo(label.font()).pixelSize() == pixels
    assert label.fontMetrics().height() == height
label.close()
'''], cwd=Path(__file__).resolve().parents[2], env=dict(os.environ, QT_QPA_PLATFORM='offscreen'),
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_reapplying_the_font_size_clears_theme_fonts_restored_by_a_stylesheet(qt_app):
    """Setting the size again replaces per-class theme fonts, size unchanged.

    Qt gives QLabel, QPushButton, the item views and more their own font from
    the platform theme, and puts those back whenever a style is installed --
    which QApplication.setStyleSheet does. main.py and the UI audit set the
    stylesheet after the startup font, so the following call with the same
    size must still clear them: skipping it because the application font was
    unchanged left macOS on .AppleSystemUIFont 13pt across the editor (the
    native UI parity check). The planted 13pt fonts stand in for the theme's,
    which the offscreen platform does not have.
    """
    from editor.desktop_style import set_editor_font_size
    set_editor_font_size(qt_app, 11)
    editor_font = qt_app.font()
    assert editor_font.family() == UI_FONT and editor_font.pointSize() == 11
    system = QFont('DejaVu Sans', 13)
    for name in ('QLabel', 'QPushButton', 'QAbstractItemView'):
        qt_app.setFont(system, name)
    assert qt_app.font('QLabel') == system
    try:
        set_editor_font_size(qt_app, 11)
        for name in ('QLabel', 'QPushButton', 'QAbstractItemView'):
            assert qt_app.font(name) == editor_font, name
        label = QLabel('Label text')
        try:
            assert label.font().family() == UI_FONT
            assert label.font().pointSize() == 11
        finally:
            label.deleteLater()
    finally:
        qt_app.setFont(editor_font)
        set_editor_font_size(qt_app, 11)
