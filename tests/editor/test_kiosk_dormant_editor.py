"""Kiosk mode: the game has the window and the UI thread; the editor sleeps.

* The editor is dormant (``MainWindow.editor_dormant``): an update_all_ui
  (a level change inside the game, a console command) does only what the
  game draws -- the sky, a Big World's terrain fill -- and leaves the panels
  nobody can see, which are brought up to date once when kiosk mode ends.
* The view no longer refreshes the editor's hidden 2D views at 10 Hz (each
  refresh also asked for a 3D repaint of a frame already on screen).
* Each frame is painted when it is published rather than on the next tick of
  the 16 ms update loop. Outside kiosk mode nothing changes.
"""

import pytest

pytest.importorskip("PyQt5", reason="kiosk mode is the Qt editor window")

from editor.things import PlayerStart                        # noqa: E402
from tests.helpers.worlds import level_data, make_thing, room  # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def kiosk(fio_session):
    start = make_thing(PlayerStart, "start", (0.0, 8.0, 0.0))
    session = fio_session(level_data(brushes=room(), things=[start]))
    window = session.window
    if not window.config.has_section('Kiosk'):
        window.config.add_section('Kiosk')
    window.config.set('Kiosk', 'window_mode', 'Windowed')
    session.start_play()
    session.step(10)
    window.enter_kiosk_mode()
    assert window.is_kiosk_mode and window.editor_dormant
    yield session
    if window.is_kiosk_mode:
        window.exit_kiosk_mode(keep_play_mode=True, confirm=False)


def _count(monkeypatch, obj, name):
    calls = []
    original = getattr(obj, name)

    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(obj, name, counted)
    return calls


def test_the_editor_panels_wait_for_kiosk_mode_to_end(kiosk, monkeypatch):
    window = kiosk.window
    hierarchy = _count(monkeypatch, window.scene_hierarchy, 'refresh_list')
    panel = _count(monkeypatch, window.property_editor, 'set_object')
    views = _count(monkeypatch, window, 'update_views')
    sky = _count(monkeypatch, window, 'sync_sky_image_from_map')
    for _ in range(3):
        window.update_all_ui()
    assert (hierarchy, panel, views) == ([], [], [])
    assert len(sky) == 3, "the sky the game draws is still kept up to date"

    window.exit_kiosk_mode(keep_play_mode=True, confirm=False)
    assert not window.editor_dormant
    assert len(hierarchy) == 1 and len(panel) == 1, "brought up to date once"
    window.update_all_ui()
    assert len(hierarchy) == 2


def test_the_hidden_2d_views_are_not_refreshed_in_kiosk_mode(kiosk, monkeypatch):
    window, view = kiosk.window, kiosk.view
    views = _count(monkeypatch, window, 'update_views')
    view._play_views_due = 0.0
    kiosk.publish()
    view.set_paint_on_publish(False)          # let the update loop swap the frame
    view.update_loop()
    assert views == []

    window.exit_kiosk_mode(keep_play_mode=True, confirm=False)
    view._play_views_due = 0.0
    kiosk.publish()
    view.update_loop()
    assert views == [True], "in Play in the editor the 2D views still follow"


def test_kiosk_frames_are_painted_when_published(kiosk, monkeypatch, qt_app):
    window, view = kiosk.window, kiosk.view
    assert view.game_state.publish_listener is not None
    paints = _count(monkeypatch, view, 'update')
    kiosk.publish()                            # the logic thread publishes
    qt_app.processEvents()                     # the queued notification
    assert paints, "a published frame did not ask for a paint"
    assert not view.game_state.try_swap(), "the frame was taken for that paint"

    window.exit_kiosk_mode(keep_play_mode=True, confirm=False)
    assert view.game_state.publish_listener is None
