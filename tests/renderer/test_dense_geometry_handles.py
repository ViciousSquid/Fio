"""Focused regression tests for dense convex geometry handles."""

from pathlib import Path

import numpy as np
import pytest

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


@pytest.mark.gl
def test_dense_geometry_mesh_preparation_uses_handles_not_refs():
    """Meshes are resolved from the dense geometry records, by handle.

    The renderer reaches a record through ``geometry_id`` and the table's
    ``geometry_records`` -- never through ``refs``/``brushes`` -- and keeps
    what it resolved in arrays indexed by the same handle.
    """
    from tests.helpers import gl as glh

    convex = angled_brush("convex")
    table = RenderTable()
    table.sync([convex], 1)
    table.refs = None          # any route through the object column fails
    table.brushes = None

    glh.reset_texture_cache()
    try:
        with glh.GLTestContext(32, 32):
            renderer = glh.make_renderer()
            try:
                calls = []
                resolve = renderer._get_geo_mesh_record

                def probe(record, *args, **kwargs):
                    calls.append(record)
                    return resolve(record, *args, **kwargs)

                renderer._get_geo_mesh_record = probe
                meshes = renderer._prepare_geo_meshes(
                    table, np.array([0], dtype=np.int32))
                assert list(meshes) == [0]
                assert calls == [table.geometry_records[0]]
                assert calls[0].convex is not convex
                handles = renderer._geo_handles(table)
                assert handles.meshes[0] is meshes[0]
                assert handles.vao[0] == meshes[0].vao
            finally:
                renderer.cleanup()
    finally:
        glh.reset_texture_cache()


def test_dense_render_paths_have_no_convex_refs_slot_lookup():
    root = Path(__file__).resolve().parents[2]
    renderer = "".join(
        path.read_text(encoding="utf-8")
        for path in sorted((root / "engine" / "renderer").rglob("*.py")))

    assert "refs[slot]" not in renderer
    assert "refs[int(brushes[index])]" not in renderer
    assert "refs[int(s)]" not in renderer


@pytest.mark.gl
def test_shadow_preparation_returns_dense_convex_slots():
    from engine.renderer.forward import ForwardRenderer

    box = box_brush("box")
    convex = angled_brush("convex")
    table = RenderTable()
    table.sync([box, convex], 1)

    class Probe:
        def _brush_stream(self, key, table, slots, spare=0.0, payload=0.0,
                          normals=True):
            self.packed = (key, list(slots), normals)
            return 'stream'

        def _pass_memo(self, key, table, slots, state, objects, build):
            return build()

    probe = Probe()
    cube_slots, geo_slots, stream = ForwardRenderer._prepare_shadow_instances(
        probe, table, np.array([0, 1], dtype=np.int32), True, key=('shadow', 3))

    assert cube_slots.tolist() == [0]
    assert geo_slots.tolist() == [1]
    assert probe.packed == (('shadow', 3), [0], False)
    assert stream == 'stream'
