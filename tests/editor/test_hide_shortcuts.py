"""H hides the selection -- a thing as well as a brush -- and Shift+H shows
everything hidden again, with or without a selection."""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt, QEvent                 # noqa: E402
from PyQt5.QtGui import QKeyEvent                   # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

MAP = "maps/Office_Corridor.json"


def _press(window, key, modifiers=Qt.NoModifier):
    window.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, modifiers))


def test_h_hides_a_selected_thing(fio_session):
    session = fio_session(MAP)
    window, thing = session.window, session.state.things[0]
    window.set_selected_objects([thing])
    _press(window, Qt.Key_H)
    assert thing.properties.get("hidden") is True
    assert not session.state.selected_objects


def test_h_hides_every_selected_object(fio_session):
    session = fio_session(MAP)
    window = session.window
    brush, thing = session.state.brushes[0], session.state.things[1]
    window.set_selected_objects([brush, thing])
    _press(window, Qt.Key_H)
    assert brush.get("hidden") is True and thing.properties.get("hidden") is True


def test_shift_h_shows_everything_without_a_selection(fio_session):
    session = fio_session(MAP)
    window = session.window
    brush, thing = session.state.brushes[0], session.state.things[0]
    window.set_selected_objects([brush, thing])
    _press(window, Qt.Key_H)
    assert not session.state.selected_objects
    _press(window, Qt.Key_H, Qt.ShiftModifier)
    assert not brush.get("hidden") and not thing.properties.get("hidden")


def test_hiding_is_one_undo_step(fio_session):
    session = fio_session(MAP)
    window, thing = session.window, session.state.things[0]
    depth = len(session.state.undo_stack)
    window.set_selected_objects([thing])
    _press(window, Qt.Key_H)
    assert len(session.state.undo_stack) == depth + 1
