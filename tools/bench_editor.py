"""Opt-in editor startup, 2D brush drawing and undo snapshot measurements.

Run from the repository root with the normal desktop dependencies:
    QT_QPA_PLATFORM=offscreen python tools/bench_editor.py --json /tmp/editor.json

Uses the real MainWindow and View2D with an in-memory QImage, without OpenGL.
Reports median milliseconds over deterministic visible brush grids with empty
and half-world selections. No timing is a pass/fail threshold. --dump writes
cProfile files for startup, drawing and snapshots. --compare interleaves the
original selection/projection algorithms in the same window. Settings are restored.
"""
from __future__ import annotations

import argparse
import cProfile
import contextlib
import gc
import json
import math
import os
from pathlib import Path
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--counts', nargs='+', type=int, default=[64, 1024, 4096])
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--json')
    parser.add_argument('--dump')
    parser.add_argument('--compare', action='store_true',
                        help='interleave the original selection/projection algorithms '
                             'with current drawing/snapshots in the same window')
    args = parser.parse_args()
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    settings = (ROOT / 'settings.ini').read_bytes()
    dump = Path(args.dump) if args.dump else None
    if dump:
        dump.mkdir(parents=True, exist_ok=True)
    profile = cProfile.Profile()
    started = time.perf_counter()
    profile.enable()
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QImage, QPainter
    from PyQt5.QtWidgets import QApplication
    from editor.main_window import MainWindow
    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = None
    try:
        window = MainWindow(str(ROOT))
        profile.disable()
        startup_ms = (time.perf_counter() - started) * 1000
        if dump:
            profile.dump_stats(str(dump / 'startup.pstats'))
        view = window.view_top
        view.resize(640, 480)
        image = QImage(640, 480, QImage.Format_ARGB32)
        from PyQt5.QtCore import QPointF, QRectF
        from editor import view_2d
        current_lookup = view_2d.SelectionLookup
        current_rect = view._brush_screen_rect
        current_selection = window.state._selection_list
        def legacy_rect(pos, size, axis1, axis2, transform):
            minimum = QPointF(pos[axis1] - size[axis1]/2, pos[axis2] - size[axis2]/2)
            extent = QPointF(size[axis1], size[axis2])
            return QRectF(view.world_to_screen(minimum),
                          view.world_to_screen(minimum + extent)).normalized()
        def legacy_selection():
            selected = []
            for obj in window.state.selected_objects:
                if not any(obj is existing for existing in selected):
                    selected.append(obj)
            return selected
        @contextlib.contextmanager
        def reference(enabled):
            try:
                view_2d.SelectionLookup = list if enabled else current_lookup
                view._brush_screen_rect = legacy_rect if enabled else current_rect
                window.state._selection_list = legacy_selection if enabled else current_selection
                yield
            finally:
                view_2d.SelectionLookup = current_lookup
                view._brush_screen_rect = current_rect
                window.state._selection_list = current_selection
        rows = []
        for count in args.counts:
            side = math.ceil(math.sqrt(count))
            window.state.brushes = [
                {'id': str(i), 'pos': [(i % side - side / 2) * 16, 0,
                                      (i // side - side / 2) * 16],
                 'size': [12, 12, 12], 'textures': {}}
                for i in range(count)]
            window.state.things = []
            view.zoom_factor = min(1., 400 / (side * 16))
            bounds = view.get_visible_world_bounds()

            def draw():
                image.fill(Qt.transparent)
                painter = QPainter(image)
                try:
                    view.draw_brushes(painter, bounds)
                finally:
                    painter.end()

            for selected in (0, count // 2):
                window.state.selected_objects = window.state.brushes[:selected]
                for operation, fn in [('draw', draw), ('snapshot', window.state.snapshot)]:
                    fn()
                    samples = {False: [], True: []}
                    for repeat in range(args.repeats):
                        modes = ([True, False] if repeat % 2 else [False, True]) if args.compare else [False]
                        for mode in modes:
                            gc.collect()
                            with reference(mode):
                                started = time.perf_counter()
                                fn()
                                samples[mode].append((time.perf_counter() - started) * 1000)
                    row = dict(brushes=count, selected=selected, operation=operation,
                               median_ms=round(statistics.median(samples[False]), 3))
                    if args.compare:
                        row['reference_ms'] = round(statistics.median(samples[True]), 3)
                        row['speedup'] = round(row['reference_ms'] / row['median_ms'], 3)
                    print(json.dumps(row), flush=True)
                    rows.append(row)
                    if dump:
                        profile = cProfile.Profile()
                        profile.runcall(fn)
                        profile.dump_stats(str(dump / f'{operation}_{count}_{selected}.pstats'))
                if dump:
                    image.save(str(dump / f'draw_{count}_{selected}.png'))
        result = dict(startup_ms=round(startup_ms, 3), results=rows)
        print(json.dumps({'startup_ms': result['startup_ms']}), flush=True)
        if args.json:
            Path(args.json).write_text(json.dumps(result, indent=2))
    finally:
        if window is not None:
            window.close()
        app.processEvents()
        (ROOT / 'settings.ini').write_bytes(settings)


if __name__ == '__main__':
    main()
