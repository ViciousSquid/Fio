"""Shared capture for the .fiopak end-to-end check (mirrors tools/oracle_capture.py).

Plays the open level on a stepped clock with seeded RNGs and scripted input and
records, every few ticks: what Debug Tables shows (its frozen RenderTable and
EntityTable columns, read from the DebugTablesWindow as a user would), an
independent read of the published frame's tables, and the frame's pixels.
"""
import os
import random
import sys
import time

import numpy as np


def qt_app():
    from PyQt5.QtWidgets import QApplication, QMessageBox, QDialog
    from editor.desktop_style import prepare_qt, install_editor_style
    prepare_qt()
    app = QApplication.instance() or QApplication(sys.argv[:1])
    install_editor_style(app)
    answered = []
    for name in ("warning", "information", "critical"):
        setattr(QMessageBox, name, staticmethod(
            lambda *a, _n=name, **k: answered.append((_n, str(a[2])[:300] if len(a) > 2 else "")) or QMessageBox.Ok))
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

    def refuse_modal(self, *a, **k):
        raise RuntimeError("unexpected modal dialog: %s %r" % (
            type(self).__name__, getattr(self, "text", lambda: "")()))
    QDialog.exec_ = QDialog.exec = refuse_modal
    return app, answered


def columns(table):
    found = {}
    for klass in type(table).__mro__:
        for name in getattr(klass, "__slots__", ()):
            value = getattr(table, name, None)
            if isinstance(value, np.ndarray) and value.dtype != object:
                found[name] = value.copy()
    return found


def direct_tables(brushes, things, effect_store):
    """The engine's dense tables filled straight from a loaded world: the same
    two calls on the desktop and in the Android runtime, so the columns can be
    compared without either host's frame pipeline in between."""
    from engine.entity_table import EntityTable
    from engine.render_table import RenderTable
    render_table = RenderTable()
    render_table.begin_frame(brushes, 0)
    entity_table = EntityTable()
    entity_table.begin_frame(things, effect_runtime=True, effect_store=effect_store)
    dense = {}
    for label, table in (("render_table", render_table), ("entity_table", entity_table)):
        for name, value in columns(table).items():
            dense["%s/%s" % (label, name)] = value
    return dense


def capture(session, tag, ticks=120, every=20, pixels=True, direct_out=None):
    from PyQt5.QtCore import Qt
    import engine.entity_table as entity_table
    from tools.debug_tables import DebugTablesWindow
    origin = entity_table._CLOCK_ORIGIN

    def relative(name, value):
        return (np.where(value != 0.0, value - origin, value)
                if name == "effect_spawn_time" else value)

    data = {}
    real_clock = time.perf_counter
    session.view.sysmon.set_active(False)
    config = session.view.editor.config
    if not config.has_section("Display"):
        config.add_section("Display")
    config.set("Display", "show_fps", "False")
    instrument = DebugTablesWindow(session.window)
    instrument.timer.stop()
    try:
        clock = [origin + 1000.0]
        time.perf_counter = lambda: clock[0]
        session.view.start_time = origin
        random.seed(1234)
        np.random.seed(1234)
        session.start_play()
        if direct_out:
            np.savez_compressed(direct_out, **direct_tables(
                session.state.brushes, session.state.things,
                session.logic.session_runtime.effect_store))
        for tick in range(ticks + 1):
            if tick:
                clock[0] += 1.0 / 60.0
                keys = {Qt.Key_W} | ({Qt.Key_A} if (tick // 30) % 2 else set())
                session.step(1, keys=keys, mouse=(5.0 if (tick // 25) % 2 else -5.0, 0.0),
                             shoot=tick % 15 == 0)
            if tick % every:
                continue
            key = "%s/%03d" % (tag, tick)
            with session.render_state() as frame:
                for table in ("render_table", "entity_table"):
                    for name, value in columns(getattr(frame, table)).items():
                        data["indep/%s/%s/%s" % (key, table, name)] = relative(name, value)
                data["indep/%s/visible" % key] = np.array(frame.visible_brush_slots, dtype=np.int64)
                data["indep/%s/player_pos" % key] = np.array(frame.player_pos, dtype=np.float64)
            if pixels:
                data["indep/%s/pixels" % key] = session.paint()
            instrument.refresh()
            for table, frozen in (("render_table", instrument.render),
                                  ("entity_table", instrument.entities)):
                for name, value in frozen.fields.items():
                    data["oracle/%s/%s/%s" % (key, table, name)] = relative(name, value)
                data["oracle/%s/%s/rows_read" % (key, table)] = np.array(frozen.rows_read)
            data["oracle/%s/visible" % key] = np.array(
                instrument.snapshot.visible_brush_slots, dtype=np.int64)
        dashboard = instrument.dashboard.toPlainText()
    finally:
        time.perf_counter = real_clock
        instrument.close()
        instrument.deleteLater()
    return data, dashboard


def tick_fault(session):
    return session.logic._tick_fault_message if session.logic._tick_faulted else ""
