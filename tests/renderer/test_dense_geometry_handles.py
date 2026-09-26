"""Focused regression tests for dense convex geometry handles."""

from pathlib import Path

import numpy as np

from engine.render_table import RenderTable, CLASS_HAS_GEOMETRY
from tests.helpers.worlds import angled_brush, box_brush


def test_geometry_id_is_dense_and_box_brushes_keep_the_sentinel():
    convex = angled_brush("convex")
    box = box_brush("box")
    second = angled_brush("second")
    table = RenderTable()
    table.sync([box, convex, second], 1)

    assert table.geometry_id.tolist()[:3] == [-1, 0, 1]
    assert len(table.geometry_records) == 2
    assert table.class_bits[1] & CLASS_HAS_GEOMETRY
    assert table.class_bits[2] & CLASS_HAS_GEOMETRY


def test_geometry_id_follows_row_compaction():
    convex = angled_brush("convex")
    box = box_brush("box")
    table = RenderTable()
    table.sync([box, convex], 1)

    table.sync([convex], 1)

    assert table.geometry_id.tolist()[:1] == [0]
    assert len(table.geometry_records) == 1
    assert table.geometry_id[table.slot_of_id[convex["id"]]] == 0


def test_geometry_handle_moves_with_a_surviving_row():
    a = angled_brush("a")
    b = box_brush("b")
    c = angled_brush("c")
    table = RenderTable()
    table.sync([a, b, c], 1)

    table.sync([c, b, a], 1)

    assert table.geometry_id.tolist()[:3] == [0, -1, 1]
    assert len(table.geometry_records) == 2
    assert table.geometry_id[table.slot_of_id[a["id"]]] == 1
    assert table.geometry_id[table.slot_of_id[c["id"]]] == 0


def test_dense_geometry_mesh_preparation_uses_handles_not_refs():
    from engine.renderer_core import BaseRenderer

    convex = angled_brush("convex")
    table = RenderTable()
    table.sync([convex], 1)

    class Probe:
        def __init__(self):
            self.calls = []

        def _get_geo_mesh_record(
                self, record, geometry_id=None, geometry_generation=None):
            self.calls.append((record, geometry_id, geometry_generation))
            return "mesh"

    probe = Probe()
    meshes = BaseRenderer._prepare_geo_meshes(
        probe, table, np.array([0], dtype=np.int32))

    assert meshes == {0: "mesh"}
    assert probe.calls == [(table.geometry_records[0], 0, table.generation)]
    assert probe.calls[0][0].convex is not convex
    assert probe.calls[0][0] is table.geometry_records[0]


def test_dense_render_paths_have_no_convex_refs_slot_lookup():
    root = Path(__file__).resolve().parents[2]
    renderer_f = (root / "engine" / "renderer_F.py").read_text(encoding="utf-8")
    renderer_core = (root / "engine" / "renderer_core.py").read_text(encoding="utf-8")

    assert "refs[slot]" not in renderer_f
    assert "refs[int(brushes[index])]" not in renderer_f
    assert "refs[int(s)]" not in renderer_core


def test_shadow_preparation_returns_dense_convex_slots():
    from engine.renderer_core import BaseRenderer

    box = box_brush("box")
    convex = angled_brush("convex")
    table = RenderTable()
    table.sync([box, convex], 1)

    class Probe:
        def _frame_transforms(self, table, slots):
            return np.zeros((len(slots), 16), dtype=np.float32), None

        def _pack_brush_instances(self, models, normals, rows, selected, alpha):
            self.packed = len(rows)

    probe = Probe()
    cube_slots, geo_slots = BaseRenderer._prepare_shadow_instances(
        probe, table, np.array([0, 1], dtype=np.int32), True)

    assert cube_slots.tolist() == [0]
    assert geo_slots.tolist() == [1]
    assert probe.packed == 1
