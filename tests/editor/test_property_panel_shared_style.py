"""The property panel's recurring control looks come from one shared style
sheet on the panel, not a style sheet on each control of every page.

A control with a sheet of its own is matched against it separately as it is
polished: about a quarter of building a page. The looks are rules in the
panel's sheet scoped to a dynamic property; the pages render pixel-for-pixel
as they did (checked across every page and tab of _SHOWCASE when this was
introduced).
"""

import pytest

pytestmark = pytest.mark.qt


def test_registered_looks_are_scoped_rules_in_the_panels_sheet(main_window):
    from editor.property_editor import _Style
    sheet = main_window.property_editor.styleSheet()
    assert 'QCheckBox[fio="check"]::indicator:checked' in sheet
    # A rule for a child of the styled control matches its descendants, as
    # the control's own sheet did (the tab widget's tab bar).
    assert 'QTabWidget[fio="tabs"] QTabBar::tab:selected' in sheet
    assert 'QGroupBox[fio="group_F08000_2b3d3b"]::title' in sheet
    assert _Style.group_box("#F08000") in _Style._shared


def test_a_page_carries_no_sheet_of_its_own_for_a_shared_look(main_window):
    from PyQt5.QtWidgets import QCheckBox, QTabWidget, QWidget
    from editor.things import Light
    light = Light(pos=[0, 0, 0])
    main_window.state.things.append(light)
    main_window.set_selected_objects([light])
    page = main_window.property_editor._page
    shared = [w for w in page.findChildren(QWidget) if w.property('fio')]
    assert shared, "no control took a shared look"
    assert all(not w.styleSheet() for w in shared)
    assert any(isinstance(w, QCheckBox) and w.property('fio') == 'check' for w in shared)
    assert any(isinstance(w, QTabWidget) and w.property('fio') == 'tabs' for w in shared)
    # The page's scroll bar existed (and was polished) before it took its
    # look: it is styled by the shared rule all the same.
    assert page.verticalScrollBar().property('fio') == 'scroll'
    main_window.unsaved_changes = False


def test_an_unregistered_sheet_stays_on_the_control(qt_app):
    from PyQt5.QtWidgets import QLabel
    from editor.property_editor import _Style
    label = QLabel()
    _Style.apply(label, "QLabel { color: #123456; }")
    assert label.styleSheet() == "QLabel { color: #123456; }"
    assert label.property('fio') is None
    # A registered look on the wrong kind of widget also keeps its own sheet.
    _Style.apply(label, _Style.CHECKBOX)
    assert label.styleSheet() == _Style.CHECKBOX
