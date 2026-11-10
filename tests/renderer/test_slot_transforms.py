"""Every row's matrices kept by change (renderer M3, core/slot_transforms.py).

The cache must hold exactly what ``render_table.model_matrices`` builds --
to the bit, boxes and turned rows alike -- after any mix of moves, turns,
resizes and reconciles, while rebuilding only the rows that changed.
"""

import numpy as np

from engine import render_table
from engine.render_table import RenderTable
from engine.renderer.core.slot_transforms import SlotTransforms
from tests.helpers.worlds import box_brush


def _table(n=40):
    brushes = []
    for i in range(n):
        brushes.append(box_brush("b%d" % i, (i * 70.0, 10.0 * (i % 3), -i * 20.0),
                                 (32 + i, 16 + 2 * i, 48)))
    table = RenderTable()
    table.sync(brushes, 1)
    return table, brushes


def _fresh(table):
    return render_table.model_matrices(table, np.arange(table.count, dtype=np.intp))


def _same(cache, table):
    models, normals = _fresh(table)
    assert np.array_equal(cache.models.view(np.uint32), models.view(np.uint32))
    assert np.array_equal(cache.normals.view(np.uint32), normals.view(np.uint32))


def test_built_once_then_only_what_changes():
    table, _brushes = _table()
    cache = SlotTransforms(table)
    assert cache.sync(table) == table.count
    _same(cache, table)
    assert cache.sync(table) == 0
    version = cache.version
    rng = np.random.default_rng(3)
    for step in range(30):
        rows = rng.choice(table.count, size=int(rng.integers(1, 6)), replace=False)
        table.bounds[rows, :3] += rng.normal(size=(len(rows), 3))
        if step % 3 == 0:
            table.bounds[rows, 3:] *= 1.01
        if step % 4 == 0:
            table.rot[rows] = np.array([0.3, 1.0, 0.2, 10.0 * step], dtype=np.float32)
        assert cache.sync(table) == len(rows)
        _same(cache, table)
        changed = cache.changed_since(version, np.arange(table.count))
        assert set(changed.tolist()) >= set(rows.tolist())
        version = cache.version
        assert cache.changed_since(version, np.arange(table.count)).tolist() == []


def test_nan_rows_do_not_rebuild_every_time():
    table, _brushes = _table()
    cache = SlotTransforms(table)
    cache.sync(table)
    table.bounds[4, 0] = np.nan
    assert cache.sync(table) == 1
    assert cache.sync(table) in (0, 1)       # NaN never equals itself


def test_a_reconcile_rebuilds_everything():
    table, brushes = _table()
    cache = SlotTransforms(table)
    cache.sync(table)
    brushes.append(box_brush("extra", (5.0, 5.0, 5.0), (8, 8, 8)))
    table.sync(brushes, 2)
    assert cache.sync(table) == table.count
    _same(cache, table)


def test_renderer_frame_transforms_match_a_fresh_build():
    from engine.renderer.core.tables import TablesMixin

    class Probe(TablesMixin):
        def __init__(self):
            self._brush_mat_buf = np.empty((0, 16), dtype=np.float32)
            self._brush_nmat_buf = np.empty((0, 9), dtype=np.float32)
            self._slot_transform_caches = {}

    table, _brushes = _table()
    probe = Probe()
    slots = np.array([5, 1, 7, 30, 2], dtype=np.int32)
    for step in range(5):
        table.bounds[slots[step], 0] += 3.0
        table.rot[slots[(step + 2) % 5]] = np.array([0, 1, 0, 15.0 * step], dtype=np.float32)
        models, normals = probe._frame_transforms(table, slots)
        fresh_m, fresh_n = render_table.model_matrices(table, slots)
        assert np.array_equal(models.view(np.uint32), fresh_m.view(np.uint32))
        assert np.array_equal(normals.view(np.uint32), fresh_n.view(np.uint32))
