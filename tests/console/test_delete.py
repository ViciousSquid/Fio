from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt5", reason="console commands are editor-tier")

from PyQt5.QtWidgets import QMessageBox

from editor.console_commands import ConsoleCommandHandler
from editor.things import Monster, PathNode


pytestmark = pytest.mark.qt


class _State:
    def __init__(self, things):
        self.brushes = []
        self.things = list(things)
        self.saved = 0

    def find_entity_by_name(self, name):
        for thing in self.things:
            if thing.properties.get("name") == name:
                return thing
        return None

    def save_state(self):
        self.saved += 1


class _MainWindow:
    def __init__(self, things):
        self.state = _State(things)
        self.view_3d = SimpleNamespace(play_mode=False, logic_thread=None)
        self.ui_updates = 0

    def update_all_ui(self):
        self.ui_updates += 1


def _pathnode(name):
    return PathNode(pos=[0.0, 0.0, 0.0], properties={"name": name})


@pytest.mark.parametrize("type_name", ["pathnode", "path_node", "PathNode"])
def test_delete_all_removes_every_matching_pathnode(monkeypatch, type_name):
    nodes = [_pathnode("node_a"), _pathnode("node_b")]
    monster = Monster(pos=[0.0, 0.0, 0.0], properties={"name": "grunt"})
    window = _MainWindow(nodes + [monster])
    prompt = []

    def confirm(parent, title, text, buttons, default):
        prompt.append((title, text, buttons, default))
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", staticmethod(confirm))

    ConsoleCommandHandler(window).handle_command(f"delete all {type_name}")

    assert window.state.things == [monster]
    assert window.state.saved == 1
    assert window.ui_updates == 1
    assert prompt == [(
        "Delete all",
        "2 PathNode entities will be deleted.\\n\\nAre you sure?",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No,
    )]


def test_delete_all_can_be_cancelled(monkeypatch):
    nodes = [_pathnode("node_a"), _pathnode("node_b")]
    window = _MainWindow(nodes)

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args: QMessageBox.No),
    )

    ConsoleCommandHandler(window).handle_command("delete all pathnode")

    assert window.state.things == nodes
    assert window.state.saved == 0
    assert window.ui_updates == 0


def test_delete_still_removes_one_named_entity_without_bulk_prompt(monkeypatch):
    node = _pathnode("node_a")
    window = _MainWindow([node])

    def unexpected_prompt(*args):
        raise AssertionError("single-entity delete should not prompt")

    monkeypatch.setattr(QMessageBox, "question", staticmethod(unexpected_prompt))

    ConsoleCommandHandler(window).handle_command("delete node_a")

    assert window.state.things == []
    assert window.state.saved == 1
    assert window.ui_updates == 1
