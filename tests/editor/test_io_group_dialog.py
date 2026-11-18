"""The I/O connection dialog targets a group by its UUID.

Picking a group from the Target dropdown (or pasting its UUID) stores the
group's id as the connection's ``target_id`` and its name for display, so a
renamed group keeps its connections; the input list is every input some
member accepts.
"""

import os

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtWidgets import QApplication                    # noqa: E402

from editor.editor_state import EditorState                 # noqa: E402
from editor.io_editor_widget import IOConnectionDialog      # noqa: E402
from editor.things import Light, Speaker                    # noqa: E402
from tests.helpers.worlds import box_brush, make_thing      # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def dialog():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QApplication.instance() or QApplication([])
    state = EditorState()
    button = box_brush("button", is_trigger=True)
    lamps = [make_thing(Light, "lamp%d" % i) for i in range(2)]
    radio = make_thing(Speaker, "radio")
    state.brushes.append(button)
    state.things.extend(lamps + [radio])
    group = state.view_filters.add_visgroup("hall lights", lamps + [radio])
    made = IOConnectionDialog(entity=button, editor_state=state)
    yield made, state, group
    made.deleteLater()


def _inputs(made):
    return [made.input_combo.itemText(i) for i in range(made.input_combo.count())]


def test_picking_a_group_stores_its_uuid(dialog):
    made, state, group = dialog
    made._set_target_group(group.name, group.id)
    assert "TurnOff" in _inputs(made)            # a light's input
    made.input_combo.setCurrentText("TurnOff")
    conn = made.get_connection()
    assert (conn.target_name, conn.target_id) == ("hall lights", group.id)


def test_typing_a_group_name_or_pasting_its_uuid_finds_it(dialog):
    made, state, group = dialog
    for text in ("hall lights", group.id):
        made.target_edit.setText(text)
        made._update_input_options(text)
        assert "TurnOff" in _inputs(made), text
        conn = made.get_connection()
        assert (conn.target_name, conn.target_id) == ("hall lights", group.id), text


def test_the_dropdown_lists_groups(dialog, monkeypatch):
    from PyQt5.QtWidgets import QMenu
    made, state, group = dialog
    monkeypatch.setattr(QMenu, "exec_", lambda self, *a: None)
    made._show_target_dropdown()
    texts = [a.text() for a in made._target_menu.actions()]
    assert "hall lights  [group: 3]" in texts
