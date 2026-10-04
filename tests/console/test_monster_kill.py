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
