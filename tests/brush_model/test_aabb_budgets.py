"""Stage 0: the AABB budgets the unified brush model must stay within.

Boxes are ~99 % of Fio's brushes; the unified model must not make them
dearer. Measured on the 3,604-box Big World streaming map (CPython 3.13,
x86-64) when the budgets were set:

* **1,359 B** of Python heap per box dict, as the map loads it;
* **353 B** per RenderTable row (the column bytes) and **541 B** per row
  retained by a synced table (columns plus id map and interned names);
* a box gains no keys from being projected (table sync, collision rows,
  model matrices): no plane dicts, no cached shape, no second copy;
* projecting boxes runs no convex machinery.

Heap sizes depend on the interpreter's object layouts, so the byte budgets
are exact on the CPython minor version they were measured on and carry a
10 % allowance on others; the structural guards hold everywhere. The 40 ms
full-sync budget is a timing, reported by ``tools/bench_brush_model.py``
rather than asserted here (``perf`` guards count work, they do not time it).
"""

import collections
import copy
import gc
import json
import sys
import tracemalloc

import numpy as np
import pytest

from engine import brush_geometry as bg
from engine import render_table as rt
from engine.render_table import RenderTable
from tests.helpers.paths import repo_path

pytestmark = pytest.mark.perf

MEASURED_ON = (3, 13)
BOX_HEAP_BYTES = 1359
ROW_COLUMN_BYTES = 353
ROW_RETAINED_BYTES = 541


def _budget(value):
    return value if sys.version_info[:2] == MEASURED_ON else value * 1.10


@pytest.fixture(scope='module')
def boxes():
    with open(repo_path('maps', 'BigWorld_streaming_test.json')) as handle:
        raw = json.load(handle)['brushes']
    out = [b for b in raw if not b.get('geometry')]
    assert len(out) == 3604
    return out


def _heap(make):
    gc.collect()
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        made = make()
        gc.collect()
        after = tracemalloc.get_traced_memory()[0]
    finally:
        tracemalloc.stop()
    return made, after - before


def test_a_box_dict_stays_within_its_heap_budget(boxes):
    copies, used = _heap(lambda: json.loads(json.dumps(boxes)))
    per_box = used / len(copies)
    assert per_box <= _budget(BOX_HEAP_BYTES), "%.0f B per box" % per_box


def test_a_render_table_row_stays_within_its_column_budget():
    row = sum(np.dtype(dtype).itemsize * int(np.prod(shape or (1,)))
              for _name, shape, dtype, _fill in rt._COLUMNS)
    assert row <= ROW_COLUMN_BYTES, row


def test_a_synced_table_retains_its_row_budget(boxes):
    copies = json.loads(json.dumps(boxes))

    def sync():
        table = RenderTable()
        table.sync(copies, 1)
        return table

    table, used = _heap(sync)
    assert table.count == len(copies)
    per_row = used / len(copies)
    assert per_row <= _budget(ROW_RETAINED_BYTES), "%.0f B per row" % per_row


@pytest.mark.qt                  # engine.logic_collision imports the editor
def test_projecting_boxes_adds_nothing_to_them_and_solves_nothing(boxes, monkeypatch):
    from engine.logic_collision import CollisionRows
    copies = json.loads(json.dumps(boxes))
    before = copy.deepcopy(copies)
    calls = collections.Counter()
    for name in ('get_convex', 'get_shape', 'box_planes', 'box_to_geometry',
                 'build_collision_mesh'):
        original = getattr(bg, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            calls[_name] += 1
            return _original(*args, **kwargs)
        monkeypatch.setattr(bg, name, counted)
    table = RenderTable()
    table.sync(copies, 1)
    CollisionRows(copies, 1)
    rt.model_matrices(table, np.arange(table.count))
    copies[5]['pos'] = [copies[5]['pos'][0] + 8.0] + list(copies[5]['pos'][1:])
    table.sync(copies, 2, dirty_objects={id(copies[5])})
    assert not calls, dict(calls)
    assert table.geometry_records == [] and (table.geometry_id == -1).all()
    before[5]['pos'] = copies[5]['pos']
    assert copies == before
