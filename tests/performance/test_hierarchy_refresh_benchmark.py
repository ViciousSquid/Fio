"""Opt-in whole-hierarchy refresh benchmark with frozen original behavior.

QT_QPA_PLATFORM=offscreen python -m pytest \
    tests/performance/test_hierarchy_refresh_benchmark.py \
    --run-benchmarks -s -q

Original and current refresh methods run in alternating order on the same Qt
widget and world. Every case checks the selected row addresses before timing.
The report gives the best of seven samples, without imposing a timing budget.
"""

import gc
import time

import pytest

pytest.importorskip('PyQt5', reason='the hierarchy is a Qt widget')

from PyQt5.QtCore import Qt

from editor.scene_hierarchy import SceneHierarchy
from tests.helpers.hierarchy_reference import reference_refresh_list

pytestmark = [pytest.mark.qt, pytest.mark.benchmark, pytest.mark.slow]


class _Thing:
    def __init__(self, index):
        self.name = 'entity-%d' % index
        self.properties = {'id': 'entity-%d' % index}


def _selected_rows(panel):
    return sorted(item.data(0, Qt.UserRole) for item in panel.tree.selectedItems())


@pytest.mark.parametrize('distribution', ['brushes', 'mixed'])
def test_report_hierarchy_refresh(qt_app, main_window, distribution):
    logic = main_window.view_3d.logic_thread
    logic.stop()
    if logic.is_alive():
        logic.join(timeout=1)
    state = main_window.state
    panel = main_window.scene_hierarchy
    try:
        print('\ndistribution count selected original_ms current_ms old/new')
        for count in (64, 1024, 4096):
            brush_count = count if distribution == 'brushes' else count // 2
            state.brushes = [
                {'id': 'brush-%d' % i, 'name': 'brush-%d' % i,
                 'pos': [float(i * 64), 0., 0.], 'size': [64., 64., 64.]}
                for i in range(brush_count)]
            state.things = [_Thing(i) for i in range(count - brush_count)]
            objects = state.brushes + state.things
            for selected_count in (0, count // 2, count):
                # Interleave selection across both entity categories.
                state.selected_objects = objects[::2] if selected_count == count // 2 else objects[:selected_count]
                reference_refresh_list(panel)
                expected = _selected_rows(panel)
                panel.refresh_list()
                assert _selected_rows(panel) == expected
                samples = [[], []]
                gc.collect()
                was_enabled = gc.isenabled()
                gc.disable()
                try:
                    for repeat in range(7):
                        for index in ((0, 1) if repeat % 2 else (1, 0)):
                            method = (lambda: reference_refresh_list(panel), panel.refresh_list)[index]
                            started = time.perf_counter_ns()
                            method()
                            samples[index].append((time.perf_counter_ns() - started) / 1e6)
                finally:
                    if was_enabled:
                        gc.enable()
                before, after = [min(values) for values in samples]
                print('%s %d %d %.3f %.3f %.3fx' %
                      (distribution, count, selected_count, before, after,
                       before / after), flush=True)
    finally:
        panel.close()
        qt_app.processEvents()
