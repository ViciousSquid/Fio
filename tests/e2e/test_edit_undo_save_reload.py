"""Editor action -> undo/redo -> save -> reload, verified at every step.

Real keystrokes into the real editor window, the editor's own undo stack,
``save_level`` to disk and ``load_level_file`` back. At each step the
published RenderTable (what the renderer will draw) is checked against the
authored world, and the authored world against the file on disk.
"""

import json

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt                     # noqa: E402
from PyQt5.QtTest import QTest                  # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

MAP = "maps/_SHOWCASE.json"


def _published_brushes(session):
    """``{id: (centre, half extents)}`` from the newest published frame."""
    session.step(2)
    with session.render_state() as frame:
        table = frame.render_table
        rows = {bid: (tuple(round(float(v), 3) for v in table.bounds[slot, 0:3]),
                      tuple(round(float(v), 3) for v in table.bounds[slot, 3:6]))
                for bid, slot in table.slot_of_id.items()}
        assert table.count == len(rows)
    return rows


def _authored_brushes(brushes):
    return {b["id"]: (tuple(round(float(v), 3) for v in b["pos"]),
                      tuple(round(float(v) * 0.5, 3) for v in b["size"]))
            for b in brushes}


def _assert_published_matches_authored(session):
    assert _published_brushes(session) == _authored_brushes(session.state.brushes)


def test_delete_undo_redo_save_and_reload_round_trip(fio_session, tmp_path):
    session = fio_session(MAP)
    window = session.window
    with open(session.window.file_path, encoding="utf-8") as handle:
        on_disk = json.load(handle)
    assert _authored_brushes(session.state.brushes) == _authored_brushes(on_disk["brushes"])
    _assert_published_matches_authored(session)

    victim = session.state.brushes[len(session.state.brushes) // 2]
    victim_id = victim["id"]
    count = len(session.state.brushes)

    # Delete it with the Delete key.
    window.set_selected_objects([victim])
    QTest.keyClick(window, Qt.Key_Delete)
    assert victim_id not in {b["id"] for b in session.state.brushes}
    assert len(session.state.brushes) == count - 1
    published = _published_brushes(session)
    assert victim_id not in published
    _assert_published_matches_authored(session)

    # Undo brings it back, as a rebuilt object with the same id and bounds.
    QTest.keyClick(window, Qt.Key_Z, Qt.ControlModifier)
    assert len(session.state.brushes) == count
    _assert_published_matches_authored(session)
    assert _authored_brushes(session.state.brushes) == _authored_brushes(on_disk["brushes"])

    # Redo deletes it again.
    QTest.keyClick(window, Qt.Key_Y, Qt.ControlModifier)
    assert victim_id not in {b["id"] for b in session.state.brushes}
    _assert_published_matches_authored(session)

    # Save under a new name, reload, and the world is what was saved.
    saved = tmp_path / "edited.json"
    window.file_path = str(saved)
    window.save_level()
    assert not window.unsaved_changes
    with open(saved, encoding="utf-8") as handle:
        written = json.load(handle)
    assert _authored_brushes(written["brushes"]) == _authored_brushes(session.state.brushes)
    assert [t["properties"]["id"] for t in written["things"]] == [
        t.properties["id"] for t in session.state.things]

    assert window.load_level_file(str(saved))
    assert len(session.state.brushes) == count - 1
    assert _authored_brushes(session.state.brushes) == _authored_brushes(written["brushes"])
    _assert_published_matches_authored(session)
