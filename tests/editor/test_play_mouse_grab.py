"""Play holds the mouse: a click fires, wherever the hidden cursor really is.

The look hides the cursor without confining it, so before the view grabbed
the mouse a click went to whatever widget the pointer was over -- on macOS,
where the cursor is detached in place, that was the Play button just
clicked, so clicking to fire pressed Stop.
"""

import json

import pytest

pytest.importorskip('PyQt5')
from PyQt5.QtCore import QEvent, QPoint, Qt
from PyQt5.QtGui import QFocusEvent

from editor.things import PlayerStart
from tests.helpers.worlds import level_data, make_thing, room

pytestmark = pytest.mark.qt


class FakeCursor:
    """The OS cursor, under the test's control (see _poll_mouse_look)."""

    def __init__(self):
        self.pos = QPoint()
        self.moves = 0
        self.lands = True       # whether a setPos moves it at once

    def set_pos(self, *args):
        self.moves += 1
        if self.lands:
            self.pos = QPoint(*args) if len(args) == 2 else QPoint(args[0])


class FakeMacMouse:
    def take(self):
        return 0.0, 0.0


@pytest.fixture
def playing(fio_session, monkeypatch):
    import engine.qt_game_view as gv
    session = fio_session(level_data(
        brushes=room(), things=[make_thing(PlayerStart, 'start', (0, 8, 0))]))
    cursor = FakeCursor()
    monkeypatch.setattr(gv.QCursor, 'setPos', cursor.set_pos)
    monkeypatch.setattr(gv.QCursor, 'pos', lambda: QPoint(cursor.pos))
    monkeypatch.setattr(type(session.window), 'isActiveWindow', lambda self: True)
    session.view._mac_mouse = None
    session.cursor = cursor
    session.start_play()
    return session


def test_play_grabs_the_mouse_on_entry_and_restores_it_after_focus_returns(playing):
    view = playing.view
    assert view.mouseGrabber() is view

    view.focusOutEvent(QFocusEvent(QEvent.FocusOut, Qt.OtherFocusReason))
    assert view.mouseGrabber() is not view

    view.focusInEvent(QFocusEvent(QEvent.FocusIn, Qt.OtherFocusReason))
    assert view.mouseGrabber() is view

    playing.stop_play()
    assert view.mouseGrabber() is not view


def test_loading_another_map_during_play_regrabs_after_outer_overlay(
        playing, qt_app, tmp_path, monkeypatch):
    session = playing
    view, window = session.view, session.window
    overlay = window.loading_overlay

    # Model the native grab being dropped when the outermost loading panel
    # disappears. A nested _start_play() cannot protect against that teardown.
    original_end = overlay.end

    def end_and_lose_grab():
        original_end()
        if not overlay.active and view.play_mode:
            view.releaseMouse()

    monkeypatch.setattr(overlay, 'end', end_and_lose_grab)

    data = level_data(
        brushes=room(),
        things=[make_thing(PlayerStart, 'destination', (16, 8, 16))],
    )
    path = tmp_path / 'destination.json'
    path.write_text(json.dumps(data), encoding='utf-8')

    assert window.load_level_file(str(path), destination_spawn='destination')
    assert view.play_mode
    assert not overlay.active
    assert view.mouseGrabber() is not view

    # The deferred pass runs only after load_level_file has unwound its
    # outermost busy() context.
    qt_app.processEvents()
    assert view.hasFocus()
    assert view.mouseGrabber() is view


def test_pause_menu_releases_the_grab_and_closing_it_takes_it_back(playing):
    view = playing.view
    view._poll_mouse_look()
    assert view.open_pause_menu()
    view._poll_mouse_look()
    assert view.mouseGrabber() is not view
    view.close_pause_menu()
    view._poll_mouse_look()
    assert view.mouseGrabber() is view


def test_inactive_window_releases_the_grab(playing, monkeypatch):
    view = playing.view
    view._poll_mouse_look()
    monkeypatch.setattr(type(playing.window), 'isActiveWindow', lambda self: False)
    view._poll_mouse_look()
    assert view.mouseGrabber() is not view


def test_macos_waits_for_the_recentre_before_detaching(playing, monkeypatch):
    import macos_app
    view, cursor = playing.view, playing.cursor
    detached = []
    monkeypatch.setattr(macos_app, 'set_cursor_follows_mouse',
                        lambda follows: detached.append(follows) or True,
                        raising=False)
    view._mac_mouse = FakeMacMouse()
    view._skip_next_look_poll = True
    cursor.pos = QPoint(5, 5)           # over the Play button, say
    cursor.lands = False                # the move has not landed yet
    view._poll_mouse_look()             # asks for the centre
    view._poll_mouse_look()
    view._poll_mouse_look()
    assert detached == [] and not view._mac_cursor_detached
    cursor.lands = True
    view._poll_mouse_look()             # asks again; this one lands
    view._poll_mouse_look()
    assert detached == [False] and view._mac_cursor_detached
    assert cursor.pos == view.mapToGlobal(view.rect().center())


def test_macos_detaches_anyway_if_the_cursor_never_arrives(playing, monkeypatch):
    import macos_app
    view, cursor = playing.view, playing.cursor
    monkeypatch.setattr(macos_app, 'set_cursor_follows_mouse',
                        lambda follows: True, raising=False)
    view._mac_mouse = FakeMacMouse()
    view._skip_next_look_poll = True
    cursor.pos = QPoint(5, 5)
    cursor.lands = False
    for _ in range(view._MAC_CENTRE_MAX_TRIES + 2):
        view._poll_mouse_look()
    assert view._mac_cursor_detached
    view._attach_mac_cursor()
