"""Regression tests for console monster kills.

A killed monster must remain renderable so its dead sprite can be shown.
"""

import pytest

pytest.importorskip("PyQt5", reason="console commands are editor-tier")

from editor.console_commands import ConsoleCommandHandler
from editor.things import Monster

pytestmark = pytest.mark.qt


class _State:
    def __init__(self, entity):
        self.entity = entity

    def find_entity_by_name(self, name):
        return self.entity if name == self.entity.properties["name"] else None


class _View:
    logic_thread = None


class _MainWindow:
    def __init__(self, entity):
        self.state = _State(entity)
        self.view_3d = _View()
        self.ui_updates = 0

    def update_all_ui(self):
        self.ui_updates += 1


def test_console_kill_marks_monster_dead_without_hiding_it():
    monster = Monster(
        pos=[10.0, 20.0, 30.0],
        properties={"name": "grunt", "health": 100, "hidden": False},
    )
    window = _MainWindow(monster)

    ConsoleCommandHandler(window).cmd_monster_kill("grunt")

    assert monster.properties["health"] == 0
    assert monster.properties["dead"] is True
    assert monster.properties["hidden"] is False
    assert window.ui_updates == 1
