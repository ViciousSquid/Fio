"""Tag, hidden and lock indicators sit left of the name, visible at any width."""

import os

import pytest

pytest.importorskip('PyQt5', reason='the hierarchy is a Qt widget')

from PyQt5.QtCore import QSize, Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from editor.things import Light

pytestmark = pytest.mark.qt

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
LONG = 'A brush with a very long name that cannot fit in the panel'


@pytest.fixture
def hierarchy(main_window, qt_app):
    logic = main_window.view_3d.logic_thread
    logic.stop()
    if logic.is_alive():
        logic.join(timeout=1)
    state = main_window.state
    state.brushes = [dict(brush, pos=[64 * n, 0, 0], size=[32, 32, 32]) for n, brush in enumerate([
        {'id': 'tag', 'name': LONG, 'color': 'red'},
        {'id': 'hidden', 'name': LONG + ' (hidden)', 'hidden': True},
        {'id': 'both', 'name': LONG + ' (locked)', 'lock': True, 'color': 'blue'},
        {'id': 'plain', 'name': 'Plain brush'}])]
    state.things = [Light(properties={'name': LONG + ' thing', 'lock': True})]
    state.selected_objects = []
    panel = main_window.scene_hierarchy
    # The editor runs from the Fio folder; tests run from a temporary one, so
    # load the same icon files by absolute path.
    def icon(path):
        loaded = QIcon(os.path.join(ROOT, path.replace('/', os.sep)))
        assert not loaded.actualSize(QSize(20, 20)).isEmpty(), path
        return loaded
    panel.lock_icon = icon('assets/lock.png')
    panel.hidden_icon = icon('assets/hidden.png')
    panel.colour_icons = {name: icon(f'assets/circ_{name}.png') for name in panel.colour_icons}
    panel.refresh_list()
    main_window.resize(1600, 900)
    qt_app.processEvents()
    return panel


def rows(panel):
    found = {}
    model = panel.tree.model()
    for index in range(panel.tree.topLevelItemCount()):
        header = panel.tree.topLevelItem(index)
        for child in range(header.childCount()):
            item = header.child(child)
            found[item.text(0)] = (item, model.index(child, 0, model.index(index, 0)))
    return found


def test_the_name_is_the_only_column_and_carries_the_indicators(hierarchy):
    tree = hierarchy.tree
    assert tree.columnCount() == 1
    items = rows(hierarchy)
    for name in (LONG, LONG + ' (hidden)', LONG + ' (locked)', LONG + ' thing'):
        assert not items[name][0].icon(0).isNull(), name
    assert items['Plain brush'][0].icon(0).isNull()


@pytest.mark.parametrize('width', [140, 200, 400])
def test_indicators_are_left_of_the_name_and_inside_the_panel(hierarchy, qt_app, width):
    dock = hierarchy.main_window.scene_hierarchy_dock
    hierarchy.main_window.resizeDocks([dock], [width], Qt.Horizontal)
    qt_app.processEvents()
    tree = hierarchy.tree
    viewport = tree.viewport().rect()
    style = tree.style()
    # Lays the row out exactly as the tree's default delegate does.
    delegate = QStyledItemDelegate()
    for name, (item, index) in rows(hierarchy).items():
        if item.icon(0).isNull():
            continue
        option = QStyleOptionViewItem()
        option.initFrom(tree.viewport())
        option.rect = tree.visualRect(index)
        option.decorationSize = tree.iconSize()
        delegate.initStyleOption(option, index)
        icon = style.subElementRect(QStyle.SE_ItemViewItemDecoration, option, tree)
        text = style.subElementRect(QStyle.SE_ItemViewItemText, option, tree)
        assert icon.width() > 0 and viewport.contains(icon), (name, width)
        assert icon.right() < text.left(), (name, width)
    if os.environ.get('FIO_HIERARCHY_SHOT'):
        hierarchy.grab().save(os.environ['FIO_HIERARCHY_SHOT'].replace('.png', f'-{width}.png'))
