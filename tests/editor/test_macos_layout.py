"""Property forms and editor markers must fit regardless of native Qt style."""

import pytest

pytest.importorskip('PyQt5')
from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import QFormLayout, QPushButton

from editor.things import Light
from editor.view_2d import View2D
from main import dark_stylesheet

pytestmark = pytest.mark.qt


@pytest.mark.parametrize('dpr', [1, 2])
def test_large_source_markers_keep_the_same_logical_size_and_pick_bounds(main_window, dpr):
    pixmap = QPixmap(120 * dpr, 60 * dpr)
    pixmap.fill(Qt.white)
    pixmap.setDevicePixelRatio(dpr)
    rect = View2D._sprite_rect(pixmap, QPointF(0, 0))
    assert (rect.width(), rect.height()) == (32, 16)

    light = Light(pos=[0, 0, 0])
    light.get_icon_pixmap = lambda: pixmap
    main_window.state.things = [light]
    main_window.state.brushes = []
    view = main_window.view_top
    center = view.world_to_screen(QPointF(0, 0))
    assert view.get_object_at(center + QPointF(15, 7)) is light
    assert view.get_object_at(center + QPointF(18, 0)) is None
    assert view.get_object_at(center + QPointF(0, 10)) is None


def test_property_forms_override_platform_alignment_and_grow_fields(main_window):
    main_window.set_selected_objects([Light()])
    editor = main_window.property_editor
    forms = editor.findChildren(QFormLayout)
    assert forms
    for form in forms:
        assert form.formAlignment() == Qt.AlignLeft | Qt.AlignTop
        assert form.labelAlignment() == Qt.AlignLeft | Qt.AlignVCenter
        assert form.fieldGrowthPolicy() == QFormLayout.AllNonFixedFieldsGrow
        assert form.rowWrapPolicy() == QFormLayout.WrapLongRows


def test_small_toolbar_obeys_its_size_with_the_real_application_theme(main_window, qt_app):
    old_style = qt_app.styleSheet()
    try:
        qt_app.setStyleSheet(dark_stylesheet)
        if not main_window.config.has_section('Display'):
            main_window.config.add_section('Display')
        main_window.config.set('Display', 'big_toolbar_buttons', 'False')
        main_window.apply_toolbar_button_size()
        qt_app.processEvents()
        for button in main_window.tool_toolbar.findChildren(QPushButton):
            if button.property('toolbar_icon_button'):
                assert button.iconSize().width() == 24
                assert button.width() == 28  # global QPushButton min-width must not win
    finally:
        qt_app.setStyleSheet(old_style)


def test_property_tabs_do_not_inherit_the_console_tab_dimensions(main_window, qt_app):
    main_window.set_selected_objects([Light()])
    qt_app.processEvents()
    bar = main_window.property_editor.tab_widget.tabBar()
    assert bar.tabRect(0).height() < 40
    assert bar.tabRect(0).width() < 120


def test_hierarchy_has_room_for_names_and_can_be_resized(main_window):
    dock = main_window.scene_hierarchy_dock
    assert dock.minimumWidth() == dock.MIN_WIDTH
    assert dock.maximumWidth() >= 400
    main_window.state.things = [Light(properties={'name': 'Village courtyard light'})]
    main_window.scene_hierarchy.refresh_list()
    tree = main_window.scene_hierarchy.tree
    for row in range(tree.topLevelItemCount()):
        group = tree.topLevelItem(row)
        assert group.textAlignment(0) & Qt.AlignLeft
        for child in range(group.childCount()):
            assert group.child(child).textAlignment(0) & Qt.AlignLeft
