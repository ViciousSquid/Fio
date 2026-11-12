"""The floating Monster Debug window (F7 in play, ``monsterdebug``).

Tabs Perception (first) / Combat / Patrol, live from the
monster AI; switches for the sight lines and hitboxes overlays. Sampling
only *tries* the engine's locks, so it can never stall a tick.
"""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QEvent, Qt                                # noqa: E402
from PyQt5.QtGui import QKeyEvent                                  # noqa: E402

from editor.monster_debug import collect_monsters                  # noqa: E402

pytestmark = pytest.mark.qt


def _level():
    from editor.things import Monster, PathNode, PlayerStart
    from tests.helpers.worlds import box_brush, level_data, make_thing
    return level_data(
        brushes=[box_brush("floor", (0.0, -16.0, 0.0), (4096.0, 32.0, 4096.0))],
        things=[
            make_thing(PlayerStart, "spawn", (0.0, 50.0, 0.0), angle=90.0),
            make_thing(Monster, "Grunt", (0.0, 96.0, -400.0), monster_type="human",
                       sprite_width=128, sprite_height=192, awake=True, damage=7),
            make_thing(Monster, "Flyer", (1800.0, 300.0, 1800.0), monster_type="flying",
                       sprite_width=160, sprite_height=160, awake=False),
        ])


def _window(session):
    return session.window._monster_debug_window


def _press_f7(session):
    view = session.view
    view.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_F7, Qt.NoModifier))


def _column(window, tab, header):
    table = window.tables[tab]
    col = [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())].index(header)
    return {table.item(r, 0).text(): table.item(r, col).text() for r in range(table.rowCount())}


def test_in_the_editor_it_lists_the_monsters_with_the_overlays_unavailable(fio_session):
    session = fio_session(_level())
    session.window.console_handler.handle_command("monsterdebug")
    window = _window(session)
    assert window.isVisible()
    assert [window.tabs.tabText(i) for i in range(window.tabs.count())] == [
        "Perception", "Combat", "Patrol"]
    assert window.tabs.tabText(window.tabs.currentIndex()) == "Perception"
    assert window.status.text().startswith("Editor · 2 monsters")
    assert _column(window, "Perception", "Doing") == {"Grunt": "Not running",
                                                    "Flyer": "Not running"}
    assert not window.sight_lines.isEnabled() and not window.hitboxes.isEnabled()
    session.window.console_handler.handle_command("monsterdebug off")
    assert not window.isVisible()


def test_f7_in_play_opens_it_live_with_working_switches(fio_session):
    session = fio_session(_level())
    session.start_play()
    logic = session.logic
    _press_f7(session)
    window = _window(session)
    assert window.isVisible()
    assert logic.monster_ai.monster_debug_active          # F7 still shows sight lines
    session.step(20)
    window.refresh()
    assert window.status.text().startswith("Live · 2 monsters")
    doing = _column(window, "Perception", "Doing")
    assert doing["Flyer"] == "Asleep"
    assert doing["Grunt"] in ("Chasing", "Attacking")
    assert _column(window, "Perception", "Target")["Grunt"] == "Player"
    assert float(_column(window, "Perception", "Distance")["Grunt"]) == pytest.approx(400, abs=60)
    assert _column(window, "Combat", "Damage")["Grunt"] == "7"
    assert _column(window, "Combat", "Hit sphere radius")["Grunt"] == "96.0"
    perception = [window.tables["Perception"].horizontalHeaderItem(c).text()
                  for c in range(window.tables["Perception"].columnCount())]
    assert perception == ["Monster", "Doing", "Target", "Distance", "Eye height",
                          "Can hear", "Investigating", "Time left (s)"]

    # A flyer sees from its middle; a human from near the top of its figure.
    eyes = _column(window, "Perception", "Eye height")
    assert eyes["Flyer"] == "300.0" and eyes["Grunt"] == "163.2"

    window.hitboxes.setChecked(True)
    assert logic.combat_runtime.show_hitboxes
    window.sight_lines.setChecked(False)
    assert not logic.monster_ai.monster_debug_active
    window.hitboxes.setChecked(False)
    assert not logic.combat_runtime.show_hitboxes

    _press_f7(session)
    assert not window.isVisible() and not logic.monster_ai.monster_debug_active


def test_sampling_never_waits_for_a_busy_tick(fio_session):
    session = fio_session(_level())
    session.start_play()
    # The logic thread mid-tick: it holds the monster lock (re-entrant, so
    # it must be held by another thread to be busy for this one).
    import threading
    held, release = threading.Event(), threading.Event()

    def tick():
        with session.logic.session_runtime.monster_lock:
            held.set()
            release.wait(5.0)

    worker = threading.Thread(target=tick)
    worker.start()
    try:
        assert held.wait(5.0)
        assert collect_monsters(session.window) is None     # did not wait
    finally:
        release.set()
        worker.join(5.0)
    assert len(collect_monsters(session.window)["rows"]) == 2


def test_the_switches_follow_the_engine_when_play_ends(fio_session):
    session = fio_session(_level())
    session.start_play()
    session.window.console_handler.handle_command("monsterdebug on")
    window = _window(session)
    window.hitboxes.setChecked(True)
    session.stop_play()
    window.refresh()
    assert not window.sight_lines.isChecked() and not window.hitboxes.isChecked()
    assert not window.hitboxes.isEnabled()
    assert window.status.text().startswith("Editor")
