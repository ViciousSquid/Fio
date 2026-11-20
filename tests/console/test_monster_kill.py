"""Regression tests for console monster kills.

A killed monster must remain renderable so its dead sprite can be shown.
"""

import pytest

pytest.importorskip("PyQt5", reason="console commands are editor-tier")

from editor.console_commands import ConsoleCommandHandler
from editor.things import Monster

pytestmark = pytest.mark.qt


def test_console_kill_marks_monster_dead_without_hiding_it(main_window):
    monster = Monster(
        pos=[10.0, 20.0, 30.0],
        properties={"name": "grunt", "health": 100, "hidden": False},
    )
    window = main_window
    window.state.things[:] = [monster]

    ConsoleCommandHandler(window).cmd_monster_kill("grunt")

    assert monster.properties["health"] == 0
    assert monster.properties["dead"] is True
    assert monster.properties["hidden"] is False
    assert window.state.things[0] is monster


def test_console_kill_lands_between_ticks(main_window, monkeypatch):
    """The kill and its OnDeath are made under the I/O dispatch lock (the
    running logic thread's tick lock in Play), as every other console I/O
    command is: the tick and the AI thread work on this monster."""
    import contextlib

    monster = Monster(pos=[0.0, 0.0, 0.0], properties={"name": "grunt", "health": 100})
    main_window.state.things[:] = [monster]
    handler = ConsoleCommandHandler(main_window)
    inside = []

    @contextlib.contextmanager
    def dispatch_lock():
        inside.append(True)
        yield
        inside.append(False)

    monkeypatch.setattr(handler, "_io_dispatch_lock", dispatch_lock)
    seen = []

    class _IO:
        def fire_output(self, entity, output):
            seen.append((output, inside[-1], entity.properties["dead"]))

    logic = main_window.view_3d.logic_thread
    if logic is None:
        pytest.skip("no logic thread in this window")
    monkeypatch.setattr(logic, "io_manager", _IO())

    handler.cmd_monster_kill("grunt")

    assert seen == [("OnDeath", True, True)]
    assert inside == [True, False]
    assert monster.properties["health"] == 0
