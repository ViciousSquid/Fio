"""While a game is played the editor's 2D views repaint at
``QtGameView.PLAY_2D_VIEWS_HZ``, not once per 3D frame: a repaint of the
visible one costs 4-9 ms of the GUI thread, every frame, on every map."""

import pytest

pytest.importorskip("PyQt5")

pytestmark = [pytest.mark.qt, pytest.mark.integration]

MAP = "maps/Office_Corridor.json"


def _frames(session, count):
    """*count* frames of the view's own loop, each with a new published frame."""
    calls = []
    real = session.window.update_views
    session.window.update_views = lambda: calls.append(1) or real()
    try:
        for _ in range(count):
            session.step(1)
            session.view.update_loop()
    finally:
        del session.window.update_views
    return len(calls)


def test_the_2d_views_are_not_repainted_every_frame_of_play(fio_session):
    session = fio_session(MAP)
    session.start_play()
    session.view._play_views_due = 0.0
    session.view.PLAY_2D_VIEWS_HZ = 0.5            # one repaint per 2 s
    try:
        assert _frames(session, 30) == 1           # the first frame, then none
    finally:
        del session.view.PLAY_2D_VIEWS_HZ
        session.stop_play()


def test_they_still_follow_play_at_their_own_rate(fio_session):
    session = fio_session(MAP)
    session.start_play()
    session.view.PLAY_2D_VIEWS_HZ = 1e9            # due again at once
    try:
        assert _frames(session, 5) == 5
    finally:
        del session.view.PLAY_2D_VIEWS_HZ
        session.stop_play()
    assert session.view.PLAY_2D_VIEWS_HZ == 10.0


def test_the_2d_views_own_timers_stand_down_in_play(fio_session):
    """Their 60 Hz camera and arrow timers repaint the whole view too."""
    session = fio_session(MAP)
    view = session.window.view_top
    asked = []
    view.update = lambda *a: asked.append(1)
    try:
        view._last_camera_pos = None               # "the camera has moved"
        view._check_camera_changed()
        assert asked                               # editing: repainted
        session.start_play()
        del asked[:]
        view._last_camera_pos = None
        view._check_camera_changed()
        view._update_connection_animations()
        assert not asked
        session.stop_play()
    finally:
        del view.update
