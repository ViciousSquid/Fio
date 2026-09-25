"""Focused regression tests for dense convex geometry handles."""

from pathlib import Path

import numpy as np

from engine.render_table import RenderTable, CLASS_HAS_GEOMETRY
from tests.helpers.worlds import angled_brush, box_brush


def test_geometry_id_is_dense_and_box_brushes_keep_the_sentinel():
    convex = angled_brush("convex")
    box = box_brush("box")
    table = RenderTable()
    table.sync([box, convex], 1)

    assert table.geometry_id.tolist() == [-1, 1]
    assert table.class_bits[1] & CLASS_HAS_GEOMETRY


def test_geometry_id_follows_row_compaction():
    convex = angled_brush("convex")
    box = box_brush("box")
    table = RenderTable()
    table.sync([box, convex], 1)

    table.sync([convex], 1)

    assert table.geometry_id.tolist()[:1] == [0]
    assert table.geometry_id[table.slot_of_id[convex["id"]]] == 0


def test_geometry_handle_moves_with_a_surviving_row():
    a = angled_brush("a")
    b = box_brush("b")
    table = RenderTable()
    table.sync([a, b], 1)

    table.sync([b, a], 1)

    slot = table.slot_of_id[a["id"]]
    assert slot == 1
    assert table.geometry_id[slot] == slot


def test_dense_geometry_mesh_preparation_uses_handles_not_refs():
    from engine.renderer_core import BaseRenderer

    convex = angled_brush("convex")
    table = RenderTable()
    table.sync([convex], 1)

    class Probe:
        def __init__(self):
            self.calls = []

        def _get_geo_mesh(self, brush, geometry_id=None, geometry_generation=None):
            self.calls.append((brush, geometry_id, geometry_generation))
            return "mesh"

    probe = Probe()
    meshes = BaseRenderer._prepare_geo_meshes(
        probe, table, np.array([0], dtype=np.int32))

    assert meshes == {0: "mesh"}
    assert probe.calls == [(convex, 0, table.generation)]


def test_dense_render_paths_have_no_convex_refs_slot_lookup():
    root = Path(__file__).resolve().parents[2]
    renderer_f = (root / "engine" / "renderer_F.py").read_text(encoding="utf-8")
    renderer_core = (root / "engine" / "renderer_core.py").read_text(encoding="utf-8")

    assert "refs[slot]" not in renderer_f
    assert "refs[int(brushes[index])]" not in renderer_f
    assert "refs[int(s)]" not in renderer_core
