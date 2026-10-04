"""Console deletion commands through the real MainWindow/editor machinery."""

import pytest

pytest.importorskip("PyQt5", reason="console commands are editor-tier")

from PyQt5.QtWidgets import QMessageBox

from editor.console_commands import ConsoleCommandHandler
from editor.things import Monster, PathNode

pytestmark = pytest.mark.qt


def _pathnode(name):
    return PathNode(
        pos=[0.0, 0.0, 0.0],
        properties={"name": name},
    )


def _install_spies(monkeypatch, main_window):
    state_saves = []
    ui_updates = []
    real_save = main_window.state.save_state
    real_update = main_window.update_all_ui

    def save_state():
        state_saves.append(True)
        return real_save()

    def update_all_ui():
        ui_updates.append(True)
        return real_update()

    monkeypatch.setattr(main_window.state, "save_state", save_state)
    monkeypatch.setattr(main_window, "update_all_ui", update_all_ui)
    return state_saves, ui_updates


@pytest.mark.parametrize("type_name", ["pathnode", "path_node", "PathNode"])
def test_delete_all_removes_every_matching_pathnode(
    main_window, monkeypatch, type_name
):
    nodes = [_pathnode("node_a"), _pathnode("node_b")]
    monster = Monster(
        pos=[0.0, 0.0, 0.0],
        properties={"name": "grunt"},
    )
    main_window.state.things[:] = nodes + [monster]
    main_window.state.brushes[:] = []

    saves, ui_updates = _install_spies(monkeypatch, main_window)
    prompt = []

    def confirm(parent, title, text, buttons, default):
        prompt.append((parent, title, text, buttons, default))
        return QMessageBox.Yes

    monkeypatch.setattr(QMessageBox, "question", staticmethod(confirm))

    ConsoleCommandHandler(main_window).handle_command(
        f"delete all {type_name}"
    )

    assert main_window.state.things == [monster]
    assert len(saves) == 1
    assert len(ui_updates) == 1
    assert prompt == [(
        main_window,
        "Delete all",
        "2 PathNode entities will be deleted.\n\nAre you sure?",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No,
    )]


def test_delete_all_can_be_cancelled(main_window, monkeypatch):
    nodes = [_pathnode("node_a"), _pathnode("node_b")]
    main_window.state.things[:] = nodes
    main_window.state.brushes[:] = []

    saves, ui_updates = _install_spies(monkeypatch, main_window)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args: QMessageBox.No),
    )

    ConsoleCommandHandler(main_window).handle_command("delete all pathnode")

    assert main_window.state.things == nodes
    assert saves == []
    assert ui_updates == []


def test_delete_still_removes_one_named_entity_without_bulk_prompt(
    main_window, monkeypatch
):
    node = _pathnode("node_a")
    main_window.state.things[:] = [node]
    main_window.state.brushes[:] = []

    saves, ui_updates = _install_spies(monkeypatch, main_window)

    def unexpected_prompt(*args):
        raise AssertionError("single-entity delete should not prompt")

    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(unexpected_prompt)
    )

    ConsoleCommandHandler(main_window).handle_command("delete node_a")

    assert main_window.state.things == []
    assert len(saves) == 1
    assert len(ui_updates) == 1
