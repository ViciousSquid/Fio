"""Stage 2d: an angled brush's planes live in its local frame; pos places it.

Map version 5 stores each angled brush's planes about their own centre and
``pos`` says where that centre is (``docs/brush-model-stage2.md`` §11.4), so a
translation is a placement and never a shape edit. These tests hold:

* the version-5 round trip: save, load, save again -- the same document,
  the same solids;
* migration: every shipped map (version 3, world-space planes) loads to
  exactly the solids its file describes, ``pos`` at each one's centre, and
  a version-5 reload of its save is bit-for-bit what the load produced;
* an editor move is transform-only: 1,000 consecutive translations solve
  nothing, keep the shape, its collision data and its mesh record, and
  leave the mesh key alone (the GPU half -- no geometry upload -- is in
  ``tests/visual/test_geo_shared_storage.py``);
* the mesh key is the shape's, wherever it is placed;
* shape edits after a move (clip, rotate) land where the world-space rule
  says, and undo of a move restores the placement alone;
* the explicit exports for older builds: version 4 (world planes, size) and
  version 3 (Stage 1's per-tag layout on top), each loading back to the
  same solids.
"""

import copy
import json
import os

import numpy as np
import pytest

from engine import brush_faces
from engine import brush_geometry as bg
from engine import brush_placement as bp
from engine.render_table import RenderTable
from tests.helpers import brush_semantics as bs
from tests.helpers.paths import REPO_ROOT
from tests.helpers.worlds import angled_brush, box_brush

pytest.importorskip("PyQt5", reason="EditorState pulls in editor.things")

from editor.editor_state import EditorState       # noqa: E402

pytestmark = pytest.mark.qt

MAPS = bs.shipped_maps(REPO_ROOT)


def _scale(points):
    return max(1.0, float(np.abs(np.asarray(points)).max()))


def _same_solid(a, b, rel=1e-12):
    """Two ConvexGeometry the same solid: the same corners, face for face,
    within ``rel`` of their coordinates' magnitude (re-expressing a plane
    about a centre ``c`` and back moves a corner by ~|c| x 1e-16)."""
    assert a.is_valid and b.is_valid
    assert len(a.faces) == len(b.faces)
    tol = rel * _scale(a.verts) * 100.0
    gaps = np.abs(a.verts[:, None, :] - b.verts[None, :, :]).max(axis=2)
    return gaps.min(axis=1).max() <= tol and gaps.min(axis=0).max() <= tol


def _editor_world():
    cut = angled_brush("cut", (120, 40, -60), (128, 80, 96), clip_normal=(0.3, 1.0, 0.2))
    turned = angled_brush("turned", (-300, 64, 200), (96, 128, 64))
    bg.rotate_brush(turned, 30.0, (0.0, 1.0, 0.0))
    far = angled_brush("far", (250000.0, 16, -250000.0), (64, 32, 64),
                       clip_normal=(0.0, 1.0, 0.4))
    return [box_brush("floor", (0, -16, 0), (2048, 32, 2048)), cut, turned, far]


def _loaded(level):
    state = EditorState()
    state.load_from_data(json.loads(json.dumps(level)), stream_editor=False)
    return state


# ---------------------------------------------------------------------------
# The version-5 layout and its round trip
# ---------------------------------------------------------------------------

def test_a_saved_angled_brush_is_its_local_planes_and_its_pos():
    state = EditorState()
    state.brushes = _editor_world()
    level = state.get_level_data()
    assert level['version'] == bp.MAP_VERSION == 5
    for live, record in zip(state.brushes, level['brushes']):
        assert record['size'] == live['size']    # a copy, re-derived on load
        if not bg.brush_has_geometry(live):
            continue
        assert record['pos'] == live['pos']
        local = bg.ConvexGeometry(record['geometry']['planes'])
        lo, hi = local.bounds
        assert np.abs((lo + hi) / 2).max() < 1e-9   # centred on its own bounds
        # Placed by pos, it is the live solid.
        assert _same_solid(local.translated(record['pos']), bg.get_convex(live))


def test_save_load_save_is_the_same_document_and_the_same_solids():
    state = EditorState()
    state.brushes = _editor_world()
    first = state.get_level_data()
    reloaded = _loaded(first)
    second = reloaded.get_level_data()
    for key in ('version', 'brushes'):
        assert second[key] == first[key], key
    for before, after in zip(state.brushes, reloaded.brushes):
        if bg.brush_has_geometry(before):
            assert after['pos'] == before['pos']
            assert after['size'] == before['size']
            assert np.array_equal(bg.get_convex(after).verts, bg.get_convex(before).verts)
        assert bs.render_faces(after) == bs.render_faces(before)


def test_far_from_the_origin_an_angled_brush_still_solves():
    """D7: world-space planes solved to nothing beyond ~2.6e5 units (the
    solver's seed polygon); local planes are solved about the origin."""
    brush = angled_brush("far", (900000.0, 0.0, -900000.0), (64, 64, 64),
                         clip_normal=(0.2, 1.0, 0.0))
    assert bg.local_convex(brush).is_valid
    lo, hi = bg.get_convex(brush).bounds
    assert lo[0] > 899000.0 and hi[2] < -899000.0


# ---------------------------------------------------------------------------
# Migration of every shipped map
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(MAPS))
def test_every_shipped_map_loads_to_the_solids_its_file_describes(name):
    with open(os.path.join(REPO_ROOT, 'maps', name)) as handle:
        level = json.load(handle)
    assert EditorState.is_world_plane_layout(level)
    state = _loaded(level)
    for raw, live in zip(MAPS[name], state.brushes):
        if bs.encoding(raw) != 'convex':
            continue
        world = bs.world_convex(raw)
        assert _same_solid(bg.get_convex(live), world), (name, raw.get('id'))
        lo, hi = world.bounds
        assert np.allclose(live['pos'], (lo + hi) / 2,
                           atol=1e-12 * _scale(world.verts) * 100), (name, raw.get('id'))
        assert np.allclose(live['size'], hi - lo, atol=1e-9), (name, raw.get('id'))
    # Saved as version 5 and loaded again: exactly what the first load holds.
    again = _loaded(state.get_level_data())
    for first, second in zip(state.brushes, again.brushes):
        assert second.get('pos') == first.get('pos'), name
        assert second.get('geometry') == first.get('geometry'), name
        if bg.brush_has_geometry(first):
            assert np.array_equal(bg.get_convex(second).verts, bg.get_convex(first).verts)


def test_loading_localises_a_shipped_brush_however_stale_its_copy():
    """The two clipped terrain brushes store a pos/size 128 units off their
    planes; the load places them by their planes' centre, so nothing the
    editor holds drifts."""
    for name in ("Terrain_Test_small.json", "Topdown_example.json"):
        raw = next(b for b in MAPS[name] if str(b.get('id')).startswith('1a817c94'))
        assert bs.inconsistencies(raw)['bound_drift'] == pytest.approx(128.0)
        live = bs.loaded(raw)
        assert 'bound_drift' not in bs.inconsistencies(live)
        assert _same_solid(bg.get_convex(live), bs.world_convex(raw))


# ---------------------------------------------------------------------------
# A move is a placement: 1,000 of them solve and rebuild nothing
# ---------------------------------------------------------------------------

def test_a_thousand_editor_moves_solve_nothing_and_keep_the_shape(monkeypatch):
    brushes = _editor_world()
    table = RenderTable()
    table.sync(brushes, 1)
    convex = [b for b in brushes if bg.brush_has_geometry(b)]
    shapes = [bg.local_convex(b) for b in convex]
    collision = [s.collision_shape() for s in shapes]
    records = [table.geometry_records[int(table.geometry_id[brushes.index(b)])]
               for b in convex]
    keys = [r.surface for r in records]
    solves = {'n': 0}
    real = bg.compute_windings

    def counted(*a, **k):
        solves['n'] += 1
        return real(*a, **k)

    monkeypatch.setattr(bg, 'compute_windings', counted)
    rng = np.random.default_rng(5)
    start = [list(b['pos']) for b in convex]
    for step in range(1000):
        brush = convex[step % len(convex)]
        delta = [float(v) for v in rng.uniform(-48.0, 48.0, 3)]
        assert bg.translate_brush(brush, delta)
        table.refresh_transforms(brushes, np.array([brushes.index(brush)]))
        # Every reader asks once per step, as a frame would.
        bp.world_bounds(brush)
        bp.collision_shape(brush)
    assert solves['n'] == 0
    for brush, shape, coll, record, key, home in zip(convex, shapes, collision,
                                                     records, keys, start):
        assert bg.local_convex(brush) is shape            # the shape is kept
        assert shape.collision_shape() is coll            # collision data too
        assert bp.collision_shape(brush)[0] is coll[0]
        slot = brushes.index(brush)
        assert table.geometry_records[int(table.geometry_id[slot])] is record
        assert record.surface == key                          # the same mesh
        assert brush['pos'] != home
        # And it is where the moves put it, drawn there.
        assert np.allclose(table.center[slot],
                           np.asarray(brush['pos']) + np.asarray(shape.shape_box[:3]))
    # A full re-sync after all that (what a load does) builds no new mesh.
    table.sync(brushes, 2)
    assert solves['n'] == 0
    for brush, record in zip(convex, records):
        assert table.geometry_records[int(table.geometry_id[brushes.index(brush)])].surface == record.surface


def test_the_mesh_key_is_the_shapes_wherever_it_is_placed():
    """A brush's mesh key is its shape (and its owner), never its placement:
    moved anywhere, it keys the same mesh. Two brushes of one shape key two
    meshes -- one GPU mesh and one transform slot per brush, as M2 batches
    them -- and a copy of a brush under its id keys the same one (an undo)."""
    a = angled_brush("a", (0, 0, 0), (96, 64, 96), clip_normal=(0.3, 1.0, 0.0))
    table = RenderTable()
    table.sync([a], 1)
    home = table.geometry_records[int(table.geometry_id[0])].surface
    bg.translate_brush(a, (5000.0, -300.0, 12000.0))
    moved = RenderTable()
    moved.sync([a], 1)
    assert moved.geometry_records[int(moved.geometry_id[0])].surface == home
    restored = copy.deepcopy({k: v for k, v in a.items() if not k.startswith('_')})
    again = RenderTable()
    again.sync([restored], 1)
    assert again.geometry_records[int(again.geometry_id[0])].surface == home
    twin = dict(restored, id='twin')
    both = RenderTable()
    both.sync([restored, twin], 1)
    ra = both.geometry_records[int(both.geometry_id[0])]
    rb = both.geometry_records[int(both.geometry_id[1])]
    assert ra.surface[:4] == rb.surface[:4]             # the same shape
    assert ra.surface != rb.surface                     # two meshes


# ---------------------------------------------------------------------------
# Shape edits after a move, and undo
# ---------------------------------------------------------------------------

def test_a_clip_after_a_move_cuts_where_the_world_plane_is():
    brush = angled_brush("block", (0, 0, 0), (128, 128, 128))
    bg.translate_brush(brush, (1000.0, 200.0, -500.0))
    world_before = bg.get_convex(brush)
    normal = np.array([0.4, 1.0, -0.2])
    unit = normal / np.linalg.norm(normal)
    d = float(unit @ np.array([1000.0, 210.0, -500.0]))
    assert bg.clip_brush(brush, normal.tolist(), d)
    # The reference: the same world solid cut by the same world plane.
    planes = [dict(p) for p in world_before.planes] + [{'n': unit.tolist(), 'd': d}]
    expected = bg.ConvexGeometry(planes)
    assert _same_solid(bg.get_convex(brush), expected, rel=1e-11)
    lo, hi = bg.local_convex(brush).bounds
    assert np.abs((lo + hi) / 2).max() < 1e-9           # re-centred


def test_a_rotation_turns_the_world_solid_about_its_pivot():
    brush = angled_brush("bar", (400, 50, 0), (256, 32, 32))
    before = bg.get_convex(brush)
    pivot = np.array(brush['pos'], dtype=np.float64)
    bg.rotate_brush(brush, 90.0, (0.0, 1.0, 0.0))
    after = bg.get_convex(brush)
    # Turned 90 degrees about +Y through the pivot: x -> -z, z -> x.
    turned = before.verts - pivot
    turned = np.stack([turned[:, 2], turned[:, 1], -turned[:, 0]], axis=1) + pivot
    gaps = np.abs(after.verts[:, None, :] - turned[None, :, :]).max(axis=2)
    assert gaps.min(axis=1).max() < 1e-9


def test_undo_of_a_move_restores_the_placement_alone():
    state = EditorState()
    state.brushes = _editor_world()
    brush = state.brushes[1]
    geometry = copy.deepcopy(brush['geometry'])
    home = list(brush['pos'])
    state.selected_objects = [brush]
    state.save_state()
    bg.translate_brush(brush, (64.0, 0.0, 32.0))
    state.mark_world_changed([brush])
    assert brush['pos'] != home
    assert brush['geometry'] == geometry                 # a move is no edit
    state.undo()
    restored = state.brushes[1]
    assert restored['pos'] == home
    assert restored['geometry'] == geometry


# ---------------------------------------------------------------------------
# Exports for older builds
# ---------------------------------------------------------------------------

def test_the_version_4_export_is_world_planes_and_loads_back_the_same():
    state = EditorState()
    state.brushes = _editor_world()
    level = state.get_level_data()
    exported = bp.export_world_level(level)
    assert exported['version'] == 4 and level['version'] == 5
    for live, record in zip(state.brushes, exported['brushes']):
        if not bg.brush_has_geometry(live):
            continue
        world = bg.ConvexGeometry(record['geometry']['planes'])
        assert _same_solid(world, bg.get_convex(live), rel=1e-11)
        lo, hi = world.bounds
        assert np.allclose(record['size'], hi - lo, atol=1e-9)
    back = _loaded(exported)
    for live, again in zip(state.brushes, back.brushes):
        if bg.brush_has_geometry(live):
            assert _same_solid(bg.get_convex(again), bg.get_convex(live), rel=1e-11)
        assert len(bs.render_faces(again)) == len(bs.render_faces(live))


def test_the_version_3_export_starts_from_world_planes():
    state = EditorState()
    state.brushes = _editor_world()
    level = state.get_level_data()
    exported, lossy = brush_faces.export_legacy_level(level)
    assert exported['version'] == 3 and not lossy
    for live, record in zip(state.brushes, exported['brushes']):
        if bg.brush_has_geometry(live):
            world = bg.ConvexGeometry(record['geometry']['planes'])
            assert _same_solid(world, bg.get_convex(live), rel=1e-11)


def test_the_export_tool_writes_version_4_or_3(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_export_legacy_map", os.path.join(REPO_ROOT, "tools", "export_legacy_map.py"))
    export_legacy_map = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(export_legacy_map)
    state = EditorState()
    state.brushes = _editor_world()
    source = tmp_path / 'map.json'
    source.write_text(json.dumps(state.get_level_data()))
    for version in (4, 3):
        target = tmp_path / ('v%d.json' % version)
        assert export_legacy_map.main([str(source), str(target), '--version', str(version)]) == 0
        written = json.loads(target.read_text())
        assert written['version'] == version
        cut = next(b for b in written['brushes'] if b.get('name') == 'cut')
        assert _same_solid(bg.ConvexGeometry(cut['geometry']['planes']),
                           bg.get_convex(state.brushes[1]), rel=1e-11)
