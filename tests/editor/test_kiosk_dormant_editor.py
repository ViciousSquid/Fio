"""Kiosk mode: the game has the window and the UI thread; the editor sleeps.

* The editor is dormant (``MainWindow.editor_dormant``): an update_all_ui
  (a level change inside the game, a console command) does only what the
  game draws -- the sky, a Big World's terrain fill -- and leaves the panels
  nobody can see, which are brought up to date once when kiosk mode ends.
* The view no longer refreshes the editor's hidden 2D views at 10 Hz (each
  refresh also asked for a 3D repaint of a frame already on screen).
* Each frame is painted when it is published rather than on the next tick of
  the 16 ms update loop -- in Play inside the editor too, where the 2D views
  go on following play at 10 Hz.
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

    # F12 back to the editor: Play goes on, and so does painting on publish;
    # leaving Play stops it.
    window.exit_kiosk_mode(keep_play_mode=True, confirm=False)
    assert view.game_state.publish_listener is not None
    kiosk.stop_play()
    assert view.game_state.publish_listener is None


def test_play_in_the_editor_paints_on_publish_and_its_2d_views_follow(
        fio_session, monkeypatch, qt_app):
    start = make_thing(PlayerStart, "start", (0.0, 8.0, 0.0))
    session = fio_session(level_data(brushes=room(), things=[start]))
    window, view = session.window, session.view
    assert view.game_state.publish_listener is None
    session.start_play()
    assert not window.editor_dormant
    assert view.game_state.publish_listener is not None
    paints = _count(monkeypatch, view, 'update')
    views = _count(monkeypatch, window, 'update_views')
    view._play_views_due = 0.0
    session.publish()
    qt_app.processEvents()
    assert paints and views == [True], "the 2D views follow play at 10 Hz"
    session.publish()
    qt_app.processEvents()
    assert views == [True], "not at the frame rate"
    session.stop_play()
    assert view.game_state.publish_listener is None


def test_native_kiosk_without_an_opengl_context_stays_on_the_3d_view(fio_session):
    """[Kiosk] native_surface where the view has no GL context (offscreen,
    or a platform that cannot): kiosk mode as before, nothing half-made."""
    start = make_thing(PlayerStart, "start", (0.0, 8.0, 0.0))
    session = fio_session(level_data(brushes=room(), things=[start]))
    window, view = session.window, session.view
    if view.renderer is not None:
        pytest.skip("covered with a context by tests/visual/test_native_kiosk_presentation.py")
    if not window.config.has_section('Kiosk'):
        window.config.add_section('Kiosk')
    window.config.set('Kiosk', 'window_mode', 'Windowed')
    window.config.set('Kiosk', 'native_surface', 'True')
    session.start_play()
    window.enter_kiosk_mode()
    assert window.is_kiosk_mode and view._native_surface is None
    assert window.isVisible()
    window.exit_kiosk_mode(keep_play_mode=True, confirm=False)
    assert not window.is_kiosk_mode


def test_publish_notifications_are_coalesced(fio_session, monkeypatch, qt_app):
    """Five frames published while the UI thread is busy: one queued paint
    callback, which takes the newest frame -- not five, each drawing that
    same frame again."""
    start = make_thing(PlayerStart, "start", (0.0, 8.0, 0.0))
    session = fio_session(level_data(brushes=room(), things=[start]))
    view = session.view
    session.start_play()
    qt_app.processEvents()
    paints = _count(monkeypatch, view, 'update')
    view._play_views_due = float('inf')      # the 2D views' follow asks for its own
    view.publish_notifications_coalesced = 0
    for _ in range(5):
        session.publish()                      # the UI thread does not run meanwhile
    assert view.publish_notifications_coalesced == 4
    qt_app.processEvents()
    assert len(paints) == 1, "one callback, one paint"
    with session.render_state() as frame:
        assert frame.publication_serial == view.game_state.published_frames, \
            "the paint took the newest frame"
    assert view.game_state.superseded_frames >= 4
    session.publish()                          # the next publication queues again
    qt_app.processEvents()
    assert len(paints) == 2
    session.stop_play()
