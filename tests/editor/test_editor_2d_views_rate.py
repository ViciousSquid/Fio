"""Outside play the editor's 2D views refresh on their own timers at 30 Hz
(the camera cone, the animated connection arrows); during play the 3D view
repaints them at 10 Hz and those timers stand down."""

import pytest

pytest.importorskip("PyQt5")

pytestmark = pytest.mark.qt


def test_2d_views_refresh_at_30hz_in_the_editor_and_10hz_in_play(main_window):
    from editor.view_2d import View2D
    from engine.qt_game_view import QtGameView
    assert View2D.EDITOR_2D_VIEWS_HZ == 30.0
    assert QtGameView.PLAY_2D_VIEWS_HZ == 10.0
    for view in (main_window.view_top, main_window.view_front, main_window.view_side):
        assert view.animation_timer.interval() == 33
        assert view._camera_tracking_timer.interval() == 33


def test_connection_arrows_keep_their_speed_at_the_lower_rate(main_window, monkeypatch):
    view = main_window.view_top
    monkeypatch.setattr(view, 'isVisible', lambda: True)
    monkeypatch.setattr(view, '_paced_by_play', lambda: False)
    monkeypatch.setattr(main_window, 'show_logic_links', True, raising=False)
    monkeypatch.setattr(view, 'connection_animations',
                        {'k': {'growing': True, 'progress': 0.0}})
    monkeypatch.setattr(view, 'arrow_travel_progress', {})
    monkeypatch.setattr(view, 'update', lambda *a: None)
    # A sixth of a second grows a link half way, as 10 ticks at 60 Hz did.
    for _ in range(round(view.EDITOR_2D_VIEWS_HZ / 6)):
        view._update_connection_animations()
    assert view.connection_animations['k']['progress'] == pytest.approx(0.5)
