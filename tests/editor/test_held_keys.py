"""Held movement keys do not open macOS's press-and-hold accent picker."""

import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QInputMethodQueryEvent
from PyQt5.QtWidgets import QApplication, QLineEdit, QTreeView

pytestmark = pytest.mark.qt


def _ime_enabled(widget):
    # The query macOS's QNSView sends the focus object before a key reaches it.
    query = QInputMethodQueryEvent(Qt.ImEnabled | Qt.ImHints)
    QApplication.sendEvent(widget, query)
    return bool(query.value(Qt.ImEnabled))


@pytest.fixture
def widgets(main_window):
    """An item view (which reports an input method, for its keyboard search)
    and a text field, in the real editor window."""
    tree, text = QTreeView(main_window), QLineEdit(main_window)
    yield main_window, tree, text
    main_window.view_3d.play_mode = False
    main_window.view_3d.mouselook_active = False
    tree.deleteLater()
    text.deleteLater()


def test_editor_widgets_keep_their_input_method_outside_play(widgets):
    _window, tree, text = widgets
    assert _ime_enabled(tree)
    assert _ime_enabled(text)


@pytest.mark.parametrize('mode', ['play_mode', 'mouselook_active'])
def test_only_text_fields_take_text_while_keys_drive_the_view(widgets, mode):
    window, tree, text = widgets
    setattr(window.view_3d, mode, True)
    assert not _ime_enabled(tree)
    # The consoles still type, accents included.
    assert _ime_enabled(text)
