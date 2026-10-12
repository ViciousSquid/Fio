"""Record what Debug Tables reports, and an independent read, at fixed ticks.

Two revisions are compared by capturing each and diffing the captures
(``tools/oracle_diff.py``). Each level is played on a *stepped* clock
(``time.perf_counter`` is the effect clock's origin plus whole 60 Hz ticks)
with seeded RNGs and scripted input -- walk, strafe, turn, fire -- so two runs
of one revision do the same work. Every ``--every`` ticks it records:

* ``oracle/...`` -- what Debug Tables shows, read from the window as a user
  would: every RenderTable / EntityTable column it freezes, rows read last
  frame, the visible slots, and the draw / shadow-map / batched / triangle
  counters parsed from its dashboard;
* ``indep/...`` -- a read that never goes through Debug Tables: the published
  frame's tables read directly, the GL draws actually issued (``OpenGL.GL``
  wrapped, editor overlays left out as the counters leave them out) and the
  frame's pixels.

Debug Tables is a reporter, so it is never trusted alone: a change passes when
both sets agree with the other revision. Two things that are not the scene are
neutralised: the FPS counter and sysmon (wall-clock overlays) are switched
off, and ``effect_spawn_time`` is stored relative to the effect clock's origin
(``perf_counter()`` at import, different in every process). Terrain streaming
is capped by a wall-clock budget per paint and Qt paints on the real clock
while a level loads, so ``--unbudgeted-terrain`` lifts the cap: every paint
then streams what it needs and the result no longer depends on timing.

Needs a display with OpenGL (``xvfb-run -a``)::

    xvfb-run -a python tools/oracle_capture.py --out before.npz \\
        --unbudgeted-terrain maps/_SHOWCASE.json maps/BigWorld_streaming_test.json

``--tree`` captures another checkout (a ``git worktree`` at another commit).
The checkout's ``settings.ini`` is restored afterwards.
"""
from __future__ import annotations

import argparse
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

_DRAWS = ("glDrawArrays", "glDrawElements", "glDrawArraysInstanced",
          "glDrawElementsInstanced")
_OVERLAYS = {"draw_grid", "_draw_selection_overlays"}
_COUNTERS = re.compile(r"draw calls ([\d,]+)\s+shadow-map draws ([\d,]+)\s+"
                       r"batched draws ([\d,]+)\s+visible triangles ([\d,]+)")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("maps", nargs="+")
    parser.add_argument("--out", required=True, help="capture file (.npz)")
    parser.add_argument("--tree", default=os.path.dirname(HERE),
                        help="checkout to capture (default: this one)")
    parser.add_argument("--ticks", type=int, default=120)
    parser.add_argument("--every", type=int, default=20)
    parser.add_argument("--unbudgeted-terrain", action="store_true")
    args = parser.parse_args(argv)

    tree = os.path.abspath(args.tree)
    out = os.path.abspath(args.out)
    maps = [os.path.abspath(m) if os.path.exists(m) else m for m in args.maps]
    settings_path = os.path.join(tree, "settings.ini")
    with open(settings_path, "rb") as handle:
        settings = handle.read()
    os.chdir(tree)
    sys.path.insert(0, tree)
    try:
        return _capture(tree, out, maps, args)
    finally:
        with open(settings_path, "wb") as handle:
            handle.write(settings)


def _capture(tree, out, maps, args):
    import numpy as np
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication, QMessageBox

    app = QApplication.instance() or QApplication(sys.argv[:1])
    # A level with no PlayerStart cannot enter Play; it is captured in the
    # editor view instead, without a modal box waiting for a click.
    for name in ("warning", "information", "critical", "question"):
        setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.Ok))

    import OpenGL.GL as gl
    import engine.entity_table as entity_table
    from tests.helpers.session import FioTestSession
    from tools.debug_tables import DebugTablesWindow

    if args.unbudgeted_terrain:
        import engine.terrain
        engine.terrain.Terrain.UPDATE_BUDGET_MS = 1e9
        engine.terrain.Terrain.MAX_UPDATES_PER_FRAME = 1 << 30

    origin = entity_table._CLOCK_ORIGIN

    def relative(name, value):
        return value - origin if name == "effect_spawn_time" else value

    def columns(table):
        found = {}
        for klass in type(table).__mro__:
            for name in getattr(klass, "__slots__", ()):
                value = getattr(table, name, None)
                if isinstance(value, np.ndarray) and value.dtype != object:
                    found[name] = value.copy()
        return found

    def paint_counting(session):
        counted = []
        real = {name: getattr(gl, name) for name in _DRAWS}

        def wrap(name):
            def draw(*a, **k):
                names, frame = set(), sys._getframe(1)
                while frame is not None:
                    names.add(frame.f_code.co_name)
                    frame = frame.f_back
                if "render_scene" in names and not names & _OVERLAYS:
                    counted.append(name)
                return real[name](*a, **k)
            return draw
        for name in _DRAWS:
            setattr(gl, name, wrap(name))
        try:
            pixels = session.paint()
        finally:
            for name, fn in real.items():
                setattr(gl, name, fn)
        return pixels, len(counted)

    real_clock = time.perf_counter
    data = {}
    for level in maps:
        session = FioTestSession(app, level, size=(240, 136))
        tag = os.path.splitext(os.path.basename(level))[0]
        instrument = None
        try:
            session.view.sysmon.set_active(False)
            config = session.view.editor.config
            if not config.has_section("Display"):
                config.add_section("Display")
            config.set("Display", "show_fps", "False")
            instrument = DebugTablesWindow(session.window)
            instrument.timer.stop()
            clock = [origin + 1000.0]
            time.perf_counter = lambda: clock[0]
            session.view.start_time = origin
            random.seed(1234)
            np.random.seed(1234)
            try:
                session.start_play()
                mode = "play"
            except AssertionError:
                mode = "editor"
            for tick in range(args.ticks + 1):
                if tick:
                    clock[0] += 1.0 / 60.0
                    if mode == "play":
                        keys = {Qt.Key_W} | ({Qt.Key_A} if (tick // 30) % 2 else set())
                        session.step(1, keys=keys,
                                     mouse=(5.0 if (tick // 25) % 2 else -5.0, 0.0),
                                     shoot=tick % 15 == 0)
                    else:
                        session.publish()
                if tick % args.every:
                    continue
                key = "%s/%03d" % (tag, tick)
                with session.render_state() as frame:
                    for table in ("render_table", "entity_table"):
                        for name, value in columns(getattr(frame, table)).items():
                            data["indep/%s/%s/%s" % (key, table, name)] = relative(name, value)
                    data["indep/%s/visible" % key] = np.array(
                        frame.visible_brush_slots, dtype=np.int64)
                pixels, draws = paint_counting(session)
                data["indep/%s/pixels" % key] = pixels
                data["indep/%s/gl_draws" % key] = np.array(draws)
                instrument.refresh()
                for table, frozen in (("render_table", instrument.render),
                                      ("entity_table", instrument.entities)):
                    for name, value in frozen.fields.items():
                        data["oracle/%s/%s/%s" % (key, table, name)] = relative(name, value)
                    data["oracle/%s/%s/rows_read" % (key, table)] = np.array(frozen.rows_read)
                data["oracle/%s/visible" % key] = np.array(
                    instrument.snapshot.visible_brush_slots, dtype=np.int64)
                counters = _COUNTERS.search(instrument.dashboard.toPlainText())
                data["oracle/%s/draws" % key] = np.array(
                    [int(v.replace(",", "")) for v in counters.groups()])
            print("captured %s (%s)" % (level, mode), flush=True)
        finally:
            time.perf_counter = real_clock
            if instrument is not None:
                instrument.close()
                instrument.deleteLater()
            try:
                session.close()
            except AssertionError:
                pass    # a session window still reachable: reported by the suite
    np.savez_compressed(out, **data)
    print("saved %s (%d records)" % (out, len(data)))
    return 0


if __name__ == "__main__":
    status = main()
    sys.stdout.flush()
    os._exit(status)
