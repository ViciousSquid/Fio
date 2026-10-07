"""Debug Tables as an oracle -- checked against a measurement that never uses it.

Debug Tables (Debug -> Debug Tables) reports what the renderer was given and
what it says it did: table rows, the visible slots, the active renderer, and
the renderer's own draw counters. It is a useful oracle, but it is reporting,
not measuring, so it is not trusted on its own:

* the oracle test reads only Debug Tables;
* the independent test never opens it: it reads the published frame directly
  and counts the GL draws actually issued, by wrapping ``OpenGL.GL``;
* the comparison test runs both on the same frames -- Forward, then a renderer
  that shares nothing with it, then Forward again -- and requires them to agree.

``draw_calls`` counts scene draws and ``shadow_draw_calls`` the shadow-map
draws; editor overlays (the grid, the selection outline and gizmo) are not
counted, so the independent count leaves them out too.
"""

import re
import sys

import numpy as np
import pytest

pytest.importorskip("PyQt5")

import OpenGL.GL as gl                                     # noqa: E402

from engine.renderer import registry                      # noqa: E402
from tests.e2e.test_renderer_hot_swap import _level       # noqa: E402
from tests.helpers.renderers import MAGENTA, FlatRenderer  # noqa: E402

pytestmark = [pytest.mark.gl, pytest.mark.integration]

_DRAWS = ("glDrawArrays", "glDrawElements", "glDrawArraysInstanced",
          "glDrawElementsInstanced")
#: Renderer functions that draw editor overlays, which the counters leave out.
_OVERLAYS = {"draw_grid", "_draw_selection_overlays"}


@pytest.fixture
def flat():
    saved = dict(registry._factories)
    FlatRenderer.created.clear()
    FlatRenderer.cleaned.clear()
    registry.register_renderer("Flat", FlatRenderer)
    yield
    registry._factories.clear()
    registry._factories.update(saved)


def _open(fio_session):
    session = fio_session(_level(), size=(240, 160))
    session.view.sysmon.set_active(False)
    return session


# -- independent measurement --------------------------------------------------

def _paint_counting_draws(session):
    """Paint one frame; return (pixels, GL scene draws issued by the renderer)."""
    counted = []
    real = {name: getattr(gl, name) for name in _DRAWS}

    def wrap(name):
        def draw(*args, **kwargs):
            names, frame = set(), sys._getframe(1)
            while frame is not None:
                names.add(frame.f_code.co_name)
                frame = frame.f_back
            if "render_scene" in names and not names & _OVERLAYS:
                counted.append(name)
            return real[name](*args, **kwargs)
        return draw

    for name in _DRAWS:
        setattr(gl, name, wrap(name))
    try:
        pixels = session.paint()
    finally:
        for name, fn in real.items():
            setattr(gl, name, fn)
    return pixels, len(counted)


def _measure_independently(session):
    """What the frame holds and what GL was asked to draw -- no Debug Tables."""
    with session.render_state() as frame:
        rows = int(frame.render_table.count)
        entities = int(frame.entity_table.count)
        visible = sorted(int(s) for s in frame.visible_brush_slots)
    pixels, draws = _paint_counting_draws(session)
    return {"renderer": session.view.renderer_mode, "rows": rows,
            "entities": entities, "visible": visible, "gl_draws": draws,
            "pixels": pixels}


# -- the oracle -----------------------------------------------------------------

def _read_debug_tables(instrument):
    """What Debug Tables reports, read from the window as a user would."""
    instrument.refresh()
    text = instrument.dashboard.toPlainText()
    renderer = instrument.renderer_label.text()
    draws = re.search(r"draw calls ([\d,]+)\s+shadow-map draws ([\d,]+)", text)
    rows = re.search(r"RenderTable\s+rows=([\d,]+)", text).group(1)
    entities = re.search(r"EntityTable\s+rows=([\d,]+)", text).group(1)
    as_int = lambda value: int(value.replace(",", ""))  # noqa: E731
    return {"renderer": renderer, "rows": as_int(rows), "entities": as_int(entities),
            "visible": sorted(int(s) for s in instrument.snapshot.visible_brush_slots),
            "draw_calls": as_int(draws.group(1)), "shadow_draws": as_int(draws.group(2))}


def _instrument(session):
    from tools.debug_tables import DebugTablesWindow
    instrument = DebugTablesWindow(session.window)
    instrument.timer.stop()
    return instrument


def _close(instrument):
    instrument.close()
    instrument.deleteLater()


# -- the tests --------------------------------------------------------------------

def test_the_oracle_follows_a_renderer_swap(fio_session, flat):
    session = _open(fio_session)
    instrument = _instrument(session)
    try:
        session.publish()
        session.paint()
        forward = _read_debug_tables(instrument)
        assert forward["renderer"] == "FORWARD"
        assert forward["rows"] == len(session.window.state.brushes)
        assert forward["draw_calls"] > 0 and forward["shadow_draws"] > 0

        assert session.view.switch_renderer("Flat")
        session.publish()
        session.paint()
        flat_reading = _read_debug_tables(instrument)
        assert flat_reading["renderer"] == "FLAT"
        assert (flat_reading["draw_calls"], flat_reading["shadow_draws"]) == (0, 0)
        assert flat_reading["rows"] == forward["rows"]       # the world did not change

        assert session.view.switch_renderer("Forward")
        session.publish()
        session.paint()
        again = _read_debug_tables(instrument)
        assert again["renderer"] == "FORWARD"
        # A new renderer renders its shadow maps afresh.
        assert (again["draw_calls"], again["shadow_draws"]) == (
            forward["draw_calls"], forward["shadow_draws"])
    finally:
        _close(instrument)


def test_the_independent_measurement_follows_a_renderer_swap(fio_session, flat):
    session = _open(fio_session)
    session.publish()
    forward = _measure_independently(session)
    assert forward["gl_draws"] > 0
    assert forward["rows"] == len(session.window.state.brushes)
    assert forward["visible"], "nothing in view"
    assert float(np.all(forward["pixels"] == MAGENTA, axis=2).mean()) < 0.001

    assert session.view.switch_renderer("Flat")
    session.publish()
    flat_frame = _measure_independently(session)
    assert flat_frame["gl_draws"] == 0
    assert float(np.all(flat_frame["pixels"] == MAGENTA, axis=2).mean()) > 0.5
    assert flat_frame["visible"] == forward["visible"]


def test_the_oracle_agrees_with_the_independent_measurement(fio_session, flat):
    session = _open(fio_session)
    instrument = _instrument(session)
    try:
        for name in ("Forward", "Flat", "Forward"):
            assert session.view.switch_renderer(name)
            for _frame in range(2):            # shadow maps drawn, then cached
                session.publish()
                measured = _measure_independently(session)
                reported = _read_debug_tables(instrument)
                where = f"{name}, frame {_frame}"
                assert reported["renderer"] == measured["renderer"].upper() == name.upper(), where
                assert reported["rows"] == measured["rows"], where
                assert reported["entities"] == measured["entities"], where
                assert reported["visible"] == measured["visible"], where
                assert (reported["draw_calls"] + reported["shadow_draws"]
                        == measured["gl_draws"]), (where, reported, measured["gl_draws"])
    finally:
        _close(instrument)


def test_debug_tables_works_unchanged_with_any_renderer(fio_session, flat):
    """A renderer it has never heard of -- here a stand-in registered as
    "Deferred" -- is shown by name, in Fio orange, and read like any other."""
    from tools.debug_tables import FIO_ORANGE

    class Deferred(FlatRenderer):
        pass

    registry.register_renderer("Deferred", Deferred)
    session = _open(fio_session)
    instrument = _instrument(session)
    try:
        assert FIO_ORANGE == "#f08000"
        assert FIO_ORANGE in instrument.renderer_label.styleSheet()
        for name in ("Forward", "Deferred", "Flat", "Forward"):
            assert session.view.switch_renderer(name)
            session.publish()
            measured = _measure_independently(session)
            reported = _read_debug_tables(instrument)
            assert reported["renderer"] == name.upper()
            assert reported["rows"] == measured["rows"]
            assert reported["visible"] == measured["visible"]
            assert reported["draw_calls"] + reported["shadow_draws"] == measured["gl_draws"]
            assert "EXPORT FAILED" not in instrument.status.text()
    finally:
        _close(instrument)
