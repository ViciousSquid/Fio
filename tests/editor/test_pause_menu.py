"""The pause menu: Esc in Play pauses the world and offers Resume, Save Game,
Load Game, Options (Volume, Video), the plugins' items, and Exit.

Its Volume and Video are the settings of Settings > Play Modes, and the
console's ``quit`` leaves Fio without asking.
"""

import pytest

pytest.importorskip("PyQt5", reason="the pause menu is part of the Qt game view")

from PyQt5.QtCore import QEvent, Qt                          # noqa: E402
from PyQt5.QtGui import QKeyEvent                            # noqa: E402
from PyQt5.QtWidgets import QApplication                     # noqa: E402

from editor.things import PlayerStart                        # noqa: E402
from tests.helpers.worlds import level_data, make_thing, room  # noqa: E402

pytestmark = pytest.mark.qt


def _press(widget, key):
    QApplication.sendEvent(widget, QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))


def _labels(menu):
    return [item.label for item in menu.page.items]


def _choose(menu, prefix):
    for index, item in enumerate(menu.page.items):
        if item.label.startswith(prefix):
            menu.activate(index)
            return
    raise AssertionError(f"no item {prefix!r} in {_labels(menu)}")


@pytest.fixture
def playing(fio_session, tmp_path, monkeypatch):
    start = make_thing(PlayerStart, "start", (0.0, 8.0, 0.0))
    session = fio_session(level_data(brushes=room(), things=[start]))
    monkeypatch.setattr(session.window.console_handler, "_saves_dir", lambda: str(tmp_path))
    session.start_play()
    session.step(30)
    return session


def test_esc_pauses_the_world_and_esc_again_plays_on(playing):
    view, logic = playing.view, playing.logic
    _press(view, Qt.Key_Escape)
    assert view.pause_menu_active and logic.session_runtime.pause_menu_open
    assert _labels(view.pause_menu) == [
        "Resume", "Save Game", "Load Game", "Options", "Exit to Editor"]

    before = tuple(logic.player_runtime.player.pos)
    playing.step(30, keys={Qt.Key_W})
    assert tuple(logic.player_runtime.player.pos) == before, "the world moved behind the pause menu"

    _press(view, Qt.Key_Escape)
    assert not view.pause_menu_active and not logic.session_runtime.pause_menu_open
    playing.step(30, keys={Qt.Key_W})
    assert tuple(logic.player_runtime.player.pos) != before


def test_keys_choose_and_activate(playing):
    view = playing.view
    _press(view, Qt.Key_Escape)
    _press(view, Qt.Key_Down)
    _press(view, Qt.Key_Down)
    _press(view, Qt.Key_Down)                          # Options
    _press(view, Qt.Key_Return)
    assert view.pause_menu.page.title == "Options"
    _press(view, Qt.Key_Escape)                        # back, not closed
    assert view.pause_menu.page.title == "Paused"
    _press(view, Qt.Key_Up)
    _press(view, Qt.Key_Up)
    _press(view, Qt.Key_Up)                            # Resume
    _press(view, Qt.Key_Return)
    assert not view.pause_menu_active


def test_three_save_slots_round_trip_a_session(playing, tmp_path):
    view, logic = playing.view, playing.logic
    view.open_pause_menu()
    menu = view.pause_menu
    _choose(menu, "Load Game")
    assert [item.enabled for item in menu.page.items] == [False, False, False, True]
    menu.back()

    _choose(menu, "Save Game")
    assert _labels(menu)[:3] == ["Slot 1    Empty", "Slot 2    Empty", "Slot 3    Empty"]
    menu.activate(0)
    assert menu.notice == "Saved to Slot 1"
    assert (tmp_path / "slot1.fiosave").is_file()
    assert not _labels(menu)[0].endswith("Empty")
    saved = tuple(logic.player_runtime.player.pos)

    view.close_pause_menu()
    playing.step(60, keys={Qt.Key_W})
    assert tuple(logic.player_runtime.player.pos) != saved

    view.open_pause_menu()
    menu = view.pause_menu
    _choose(menu, "Load Game")
    assert [item.enabled for item in menu.page.items] == [True, False, False, True]
    menu.activate(0)
    assert not view.pause_menu_active, "a load plays on"
    assert tuple(logic.player_runtime.player.pos) == pytest.approx(saved)


def test_exit_to_editor_leaves_play(playing):
    view = playing.view
    view.open_pause_menu()
    _choose(view.pause_menu, "Exit to Editor")
    assert not view.play_mode and view.pause_menu is None
    assert not playing.logic.session_runtime.pause_menu_open


def test_plugin_items_sit_before_exit_and_run_with_the_window_and_logic(playing, monkeypatch):
    from plugins.manager import get_manager
    mgr = get_manager()
    calls = []

    class Plugin:
        enabled = True

    plugin = Plugin()
    monkeypatch.setattr(mgr, "_pause_menu_items", [])
    mgr._record_pause_menu_item(plugin, "Map", lambda w, lt: calls.append((w, lt)))
    mgr._record_pause_menu_item(plugin, "Journal", lambda w, lt: calls.append("j"),
                                close_menu=False)

    view = playing.view
    view.open_pause_menu()
    assert _labels(view.pause_menu)[-3:] == ["Map", "Journal", "Exit to Editor"]
    _choose(view.pause_menu, "Journal")
    assert calls == ["j"] and view.pause_menu_active
    _choose(view.pause_menu, "Map")
    assert calls[-1] == (playing.window, playing.logic) and not view.pause_menu_active

    plugin.enabled = False
    view.open_pause_menu()
    assert "Map" not in _labels(view.pause_menu)


def test_a_failing_plugin_item_does_not_take_play_down(playing, monkeypatch):
    from plugins.manager import get_manager
    mgr = get_manager()
    monkeypatch.setattr(mgr, "_pause_menu_items", [])

    def broken(window, logic):
        raise RuntimeError("boom")

    mgr._record_pause_menu_item(object(), "Broken", broken)
    view = playing.view
    view.open_pause_menu()
    _choose(view.pause_menu, "Broken")
    assert view.play_mode and not view.pause_menu_active


def test_volume_is_the_settings_volume(playing):
    from editor.SettingsWindow import SettingsWindow
    window, view = playing.window, playing.view
    window.set_master_volume(100)
    view.open_pause_menu()
    menu = view.pause_menu
    _choose(menu, "Options")
    assert menu.item.label == "Volume"
    _press(view, Qt.Key_Left)
    _press(view, Qt.Key_Left)
    assert window.config.get("Audio", "master_volume") == "90"
    assert view.master_volume == pytest.approx(0.9)

    dialog = SettingsWindow(window.config, window)
    assert dialog.master_volume_slider.value() == 90
    dialog.master_volume_slider.setValue(40)
    dialog._save_settings()
    dialog.deleteLater()
    assert menu.item.slider_value() == 40


def test_video_is_the_settings_display_mode(playing):
    from editor.SettingsWindow import SettingsWindow
    window, view = playing.window, playing.view
    view.open_pause_menu()
    menu = view.pause_menu
    _choose(menu, "Options")
    _choose(menu, "Video")
    assert _labels(menu) == ["Fullscreen", "Borderless", "Windowed", "Back"]

    _choose(menu, "Borderless")                         # shows the game window in it
    assert window.is_kiosk_mode
    assert window.windowFlags() & Qt.FramelessWindowHint
    assert [item.checked for item in menu.page.items[:3]] == [False, True, False]
    dialog = SettingsWindow(window.config, window)
    assert dialog.kiosk_mode_combo.currentText() == "Borderless"
    dialog.deleteLater()

    _choose(menu, "Windowed")
    assert not window.windowFlags() & Qt.FramelessWindowHint
    assert window.config.get("Kiosk", "window_mode") == "Windowed"

    window.exit_kiosk_mode(keep_play_mode=True)
    assert not window.is_kiosk_mode


def test_master_volume_scales_every_game_sound(main_window, monkeypatch):
    view = main_window.view_3d
    volumes = []

    class Channel:
        def set_volume(self, *v):
            volumes.append(v)

    class Sound:
        def play(self, loops=0):
            return Channel()

    monkeypatch.setattr(view, "_get_sound_instance", lambda name: Sound())
    main_window.set_master_volume(50)
    view.game_state.queue_sound({"file": "x.wav", "volume": 0.8})
    view._process_sound_queue()
    assert volumes == [(pytest.approx(0.4),)]


def test_quit_closes_fio_without_asking(main_window, qt_app, monkeypatch):
    import editor.main_window as mw
    quits = []

    class App:
        @staticmethod
        def instance():
            class Instance:
                quit = staticmethod(lambda: quits.append(True))
            return Instance()

        def __getattr__(self, name):
            return getattr(QApplication, name)

    monkeypatch.setattr(mw, "QApplication", App())
    monkeypatch.setattr(main_window, "check_unsaved_changes",
                        lambda: pytest.fail("quit asked about unsaved changes"))
    main_window.unsaved_changes = True
    main_window.console_handler.handle_command("quit")
    qt_app.processEvents()
    assert quits == [True]
    assert not main_window.isVisible()


def test_map_logic_cannot_quit(main_window, qt_app, monkeypatch):
    monkeypatch.setattr(main_window, "quit_immediately",
                        lambda: pytest.fail("a map quit Fio"))
    main_window.console_handler.handle_command("quit", from_map=True)
    qt_app.processEvents()


@pytest.mark.gl
def test_the_game_is_blurred_behind_the_menu(fio_session):
    from editor.things import Light
    start = make_thing(PlayerStart, "start", (0.0, 8.0, 0.0))
    lamp = make_thing(Light, "lamp", (0.0, 200.0, 0.0))
    session = fio_session(level_data(brushes=room(), things=[start, lamp]))
    session.start_play()
    session.step(5)
    view = session.view
    before = session.paint()

    assert view.open_pause_menu()
    backdrop = view._pause_backdrop
    assert backdrop is not None and not backdrop.isNull()
    scale = view.devicePixelRatioF()
    assert backdrop.width() == int(view.width() * scale) // 4
    assert backdrop.height() == int(view.height() * scale) // 4
    behind = session.paint()
    assert (behind != before).any(), "the menu drew nothing"

    view.close_pause_menu()
    assert view._pause_backdrop is None


def _mouse(view, kind, pos, buttons=Qt.LeftButton):
    from PyQt5.QtCore import QPoint
    from PyQt5.QtGui import QMouseEvent
    types = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove,
             "release": QEvent.MouseButtonRelease}
    button = Qt.NoButton if kind == "move" else Qt.LeftButton
    event = QMouseEvent(types[kind], QPoint(*pos), button, buttons, Qt.NoModifier)
    {"press": view.mousePressEvent, "move": view.mouseMoveEvent,
     "release": view.mouseReleaseEvent}[kind](event)


def test_the_menu_is_a_floating_window_like_sysmon(playing):
    from engine.floating_windows import FloatingWindow
    view = playing.view
    view.resize(800, 600)
    view.open_pause_menu()
    window = view._pause_window
    assert isinstance(window, FloatingWindow) and window.title == "Paused"
    rect = window.window_rect
    assert abs(rect.center().x() - 400) <= 2              # opens centred

    _choose(view.pause_menu, "Options")
    from PyQt5.QtGui import QImage, QPainter
    image = QImage(800, 600, QImage.Format_ARGB32)
    painter = QPainter(image)
    view._draw_pause_menu(painter)
    painter.end()
    assert window.title == "Options"

    # Drag it by its title bar; it opens there next time.
    origin = (rect.x(), rect.y())
    start = (origin[0] + 40, origin[1] + 10)
    _mouse(view, "press", start)
    _mouse(view, "move", (start[0] - 100, start[1] - 50))
    _mouse(view, "release", (start[0] - 100, start[1] - 50), buttons=Qt.NoButton)
    moved = (window.window_rect.x(), window.window_rect.y())
    assert moved == (origin[0] - 100, origin[1] - 50)
    assert view.pause_menu_active

    # [X] closes it and play goes on.
    _mouse(view, "press", (window.window_rect.right() - 10, window.window_rect.y() + 10))
    assert not view.pause_menu_active
    assert not playing.logic.session_runtime.pause_menu_open

    view.open_pause_menu()
    assert (view._pause_window.window_rect.x(), view._pause_window.window_rect.y()) == moved
    view.close_pause_menu()


def test_clicking_a_row_in_the_window_activates_it(playing):
    from PyQt5.QtGui import QImage, QPainter
    view = playing.view
    view.resize(800, 600)
    view.open_pause_menu()
    image = QImage(800, 600, QImage.Format_ARGB32)
    painter = QPainter(image)
    view._draw_pause_menu(painter)                       # lays the rows out
    painter.end()
    row, _bar = view.pause_menu.rects[0]                 # Resume
    _mouse(view, "press", (row.center().x(), row.center().y()))
    assert not view.pause_menu_active


def test_the_cursor_is_free_while_the_menu_is_open(playing, monkeypatch):
    import engine.qt_game_view as gv
    view = playing.view
    view._capture_play_cursor()
    assert QApplication.overrideCursor().shape() == Qt.BlankCursor

    view.open_pause_menu()
    assert QApplication.overrideCursor().shape() == Qt.ArrowCursor
    recentred = []
    monkeypatch.setattr(gv.QCursor, "setPos", lambda *a: recentred.append(a))
    playing.game_state.consume_mouse_delta()
    for pos in ((10, 10), (300, 200), (40, 400)):
        _mouse(view, "move", pos, buttons=Qt.NoButton)
    assert recentred == [], "mouse look kept pulling the cursor back"
    assert playing.game_state.consume_mouse_delta() == (0.0, 0.0)
    assert QApplication.overrideCursor().shape() == Qt.ArrowCursor

    monkeypatch.undo()
    view.close_pause_menu()
    assert QApplication.overrideCursor().shape() == Qt.BlankCursor
    while QApplication.overrideCursor() is not None:
        QApplication.restoreOverrideCursor()
