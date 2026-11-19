"""Release checks read through the Debug Tables instrument (tools/debug_tables).

The instrument copies the dense tables the logic thread publishes -- what the
renderer actually draws from -- so these checks hold editor operations to
what reaches the frame, not only to the Python objects the editor edits:

* typing one axis of a vector with several models selected changes only that
  axis on each (it used to give them all the primary's whole rotation), and
  one undo puts every model's published matrix back;
* a still editor frame examines no rows at any change-detection site, with
  the change-record oracle checking every record.
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="the instrument is a Qt window")

from engine import change_detection                              # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

LEVEL = {
    "version": 5,
    "brushes": [{"id": "floor", "name": "floor", "pos": [0, -8, 0],
                 "size": [1024, 16, 1024]}],
    "things": [{"type": "playerstart", "pos": [0, 40, 300],
                "properties": {"type": "player_start", "name": "start",
                               "id": "ps-1", "primary": True}}],
}


@pytest.fixture
def instrumented(fio_session, monkeypatch):
    monkeypatch.setattr(change_detection, "ORACLE", True)
    monkeypatch.setattr(change_detection, "RAISE", False)
    monkeypatch.setattr(change_detection, "MISSES", [])
    from tools.debug_tables import DebugTablesWindow
    session = fio_session(LEVEL)
    instrument = DebugTablesWindow(session.window)
    instrument.timer.stop()
    # A list, not a tuple: pytest keeps the fixture's value until the test is
    # torn down, and the instrument holds the window, which must be gone
    # before fio_session checks that the closed window is unreachable.
    held = [session, instrument]
    try:
        yield held
    finally:
        held.clear()
        instrument.close()
        instrument.setParent(None)
        instrument.deleteLater()
        del instrument
        qt_app = session.app
        from PyQt5.QtCore import QEvent
        qt_app.processEvents()
        qt_app.sendPostedEvents(None, QEvent.DeferredDelete)
    assert change_detection.MISSES == [], "a change record missed a change"


def _sample(session, instrument):
    session.publish()
    instrument.refresh()
    return instrument


def _matrices(instrument, things):
    entities = instrument.entities
    return [entities.model_base_matrix[entities.slot_of_id[t.properties["id"]]].copy()
            for t in things]


def test_one_axis_typed_on_several_models_reaches_each_models_published_matrix(
        instrumented, qt_app):
    from PyQt5.QtWidgets import QFormLayout, QLineEdit, QWidget
    from editor.things import Prop
    session, instrument = instrumented
    window = session.window
    chairs = []
    for i in range(3):
        chair = Prop.for_model("assets/models/chair.obj", pos=[i * 96, 0, 0])
        chair.properties["name"] = "chair%d" % i
        chair.properties["rotation"] = [0.0, 60.0 * i, 0.0]
        chairs.append(chair)
    window.state.things.extend(chairs)
    window.state.mark_world_changed(chairs)
    window.set_selected_objects(chairs)
    qt_app.processEvents()
    before = _matrices(_sample(session, instrument), chairs)
    assert len({m.tobytes() for m in before}) == 3

    host = QWidget()
    window.property_editor.add_vector3_widget(
        QFormLayout(host), window.property_editor.current_object, "rotation")
    box = host.findChildren(QLineEdit)[0]
    box.setText("15")
    box.editingFinished.emit()
    host.deleteLater()

    after = _matrices(_sample(session, instrument), chairs)
    assert [c.properties["rotation"] for c in chairs] == [
        [15.0, 0.0, 0.0], [15.0, 60.0, 0.0], [15.0, 120.0, 0.0]]
    # Each model's own matrix changed, and they are still three matrices.
    assert len({m.tobytes() for m in after}) == 3
    assert all(not np.allclose(a, b) for a, b in zip(before, after))

    window.undo()
    qt_app.processEvents()
    restored = [t for t in window.state.things if t.properties.get("name", "").startswith("chair")]
    undone = _matrices(_sample(session, instrument), restored)
    assert all(np.allclose(a, b) for a, b in zip(before, undone))


def test_a_still_editor_frame_examines_no_rows(instrumented):
    session, instrument = instrumented
    # The first frames after a load still settle (deferred work catches up).
    for _ in range(8):
        _sample(session, instrument)
    before = {site: tuple(v) for site, v in change_detection.DETECT.sites.items()}
    for _ in range(5):
        _sample(session, instrument)
    after = {site: tuple(v) for site, v in change_detection.DETECT.sites.items()}
    examined = {site: after[site][0] - before.get(site, (0,))[0] for site in after}
    assert not any(examined.values()), examined
    assert int(instrument.render.count) == len(session.state.brushes)
