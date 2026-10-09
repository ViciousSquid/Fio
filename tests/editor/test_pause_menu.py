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


def test_sound_and_music_volumes_are_the_settings_volumes(playing):
    from editor.SettingsWindow import SettingsWindow
    window, view = playing.window, playing.view
    window.set_sound_volume(100)
    window.set_music_volume(100)
    view.open_pause_menu()
    menu = view.pause_menu
    _choose(menu, "Options")
    assert _labels(menu) == ["Sound Volume", "Music Volume", "Video", "Back"]
    _press(view, Qt.Key_Left)                          # Sound Volume
    _press(view, Qt.Key_Left)
    _press(view, Qt.Key_Down)                          # Music Volume
    for _ in range(4):
        _press(view, Qt.Key_Left)
    assert window.config.get("Audio", "sound_volume") == "90"
    assert window.config.get("Audio", "music_volume") == "80"
    assert view.sound_volume == pytest.approx(0.9)
    assert view.music_volume == pytest.approx(0.8)

    dialog = SettingsWindow(window.config, window)
    assert dialog.sound_volume_slider.value() == 90
    assert dialog.music_volume_slider.value() == 80
    dialog.sound_volume_slider.setValue(40)
    dialog.music_volume_slider.setValue(15)
    dialog._save_settings()
    dialog.deleteLater()
    assert [item.slider_value() for item in menu.page.items[:2]] == [40, 15]


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


def test_music_plays_at_the_music_volume_and_everything_else_at_the_sound_volume(
        main_window, monkeypatch):
    view = main_window.view_3d
    volumes = {}

    class Channel:
        def __init__(self, name):
            self.name = name

        def set_volume(self, *v):
            volumes[self.name] = v

    class Sound:
        def __init__(self, name):
            self.name = name

        def play(self, loops=0):
            return Channel(self.name)

    monkeypatch.setattr(view, "_get_sound_instance", lambda name: Sound(name))
    main_window.set_sound_volume(50)
    main_window.set_music_volume(25)
    for name in ("x.wav", "assets/sounds/ui/click.wav", "assets/music/theme.ogg",
                 "music/act1/boss.mp3"):
        view.game_state.queue_sound({"file": name, "volume": 0.8})
    view._process_sound_queue()
    assert volumes == {
        "x.wav": (pytest.approx(0.4),),
        "assets/sounds/ui/click.wav": (pytest.approx(0.4),),
        "assets/music/theme.ogg": (pytest.approx(0.2),),
        "music/act1/boss.mp3": (pytest.approx(0.2),),
    }

    # A looping music speaker follows a change of the music volume live.
    volumes.clear()
    view.game_state.queue_sound({"file": "assets/music/theme.ogg", "volume": 1.0,
                                 "looping": True, "entity_id": 7})
    view._process_sound_queue()
    main_window.set_music_volume(60)
    view._process_sound_queue()
    assert volumes["assets/music/theme.ogg"] == (pytest.approx(0.6),)
    view._speaker_channels.clear()
    view._speaker_mix.clear()


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
    assert view._pause_backdrop is not None              # fading out over the game
    view._pause_clock = lambda: float("inf")
    view._pause_fade_level()
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


def test_animated_textures_run_on_unpaused_play_time(main_window, monkeypatch):
    import engine.qt_game_view as gv
    view = main_window.view_3d
    clocks = []

    class Renderer:
        ready = True

        def animate_textures(self, clock):
            clocks.append(round(clock, 3))

    now = [100.0]
    monkeypatch.setattr(gv.time, "perf_counter", lambda: now[0])
    monkeypatch.setattr(view, "renderer", Renderer())

    def frame(dt):
        now[0] += dt
        view._animate_textures()

    view._texture_clock_last = None
    frame(0.0)                                   # editor: first frame
    monkeypatch.setattr(view, "play_mode", True)
    frame(0.1)
    frame(0.1)
    session = view.logic_thread.session_runtime
    session.set_world_paused("test", True)
    frame(0.5)                                   # paused: the clock holds
    session.set_world_paused("test", False)
    frame(1.0)                                   # a long hitch counts 0.25 at most
    monkeypatch.setattr(view, "play_mode", False)
    frame(0.1)                                   # back in the editor: first frame
    assert clocks == [0.0, 0.1, 0.2, 0.2, 0.45, 0.0]


def test_the_backdrop_fades_in_and_out_over_a_second(playing, monkeypatch):
    from PyQt5.QtGui import QImage
    view, session = playing.view, playing.logic.session_runtime
    now = [100.0]
    monkeypatch.setattr(view, "_pause_clock", lambda: now[0])
    monkeypatch.setattr(view, "_blurred_backdrop", lambda: QImage(8, 8, QImage.Format_RGB32))
    assert view.PAUSE_FADE_SECONDS == 1.0

    view.open_pause_menu()
    assert session.pause_menu_open                        # paused at once
    backdrop = view._pause_backdrop
    levels = []
    for dt in (0.0, 0.5, 0.5, 0.5):
        now[0] += dt
        levels.append(round(view._pause_fade_level(), 3))
    assert levels == [0.0, 0.5, 1.0, 1.0]
    assert view._pause_fade is None and not view._pause_fade_timer.isActive()

    view.close_pause_menu()
    assert not session.pause_menu_open                    # play resumes at once
    assert view._pause_fade_timer.isActive()
    now[0] += 0.5
    assert view._pause_fade_level() == pytest.approx(0.5)
    assert view._pause_backdrop is backdrop               # still fading out

    view.open_pause_menu()                                # back in, mid-fade
    assert view._pause_backdrop is backdrop
    assert view._pause_fade_level() == pytest.approx(0.5)
    now[0] += 0.25
    assert view._pause_fade_level() == pytest.approx(0.625)
    view.close_pause_menu()
    now[0] += 1.0
    assert view._pause_fade_level() == 0.0
    assert view._pause_backdrop is None and not view._pause_fade_timer.isActive()

    # The overlay is drawn for as long as it fades out, and no longer.
    from PyQt5.QtGui import QPainter
    image = QImage(64, 64, QImage.Format_RGB32)
    image.fill(0xFFFFFFFF)
    view.open_pause_menu()
    now[0] += 1.0
    view.close_pause_menu()
    now[0] += 0.5
    painter = QPainter(image)
    view._draw_pause_menu(painter)
    painter.end()
    assert image.pixelColor(2, 2).red() < 255             # half-faded dim
