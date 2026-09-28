"""Row-local refresh: a journalled edit costs its rows, not the level.

When the world epoch moves, the table used to reconcile: walk every row, match
ids, re-read every transform and rebuild the slot maps -- measured at ~10 ms
for one retextured brush on a 5 000-brush map and ~80 ms at 20 000.  With an
unchanged row set and a precise journal, only the journalled rows can be wrong,
so only they are re-resolved.

The property that makes that safe is *equivalence*: after any sequence of
edits, a table kept up to date row-locally must hold exactly what a table
rebuilt from scratch holds.  That is what these tests check, over random edits,
with convex geometry rows included -- and they check the fast path was really
the one taken, since a silently-reconciling "fast path" would pass the
equivalence check and fix nothing.
"""

import random

import numpy as np
import pytest

from engine import brush_geometry as bg
from engine import render_table as rt
from engine.render_table import RenderTable

COLUMNS = ('center', 'half', 'rot', 'class_bits', 'uv_scale', 'uv_angle',
           'uv_shift', 'uv_natural', 'uv_has_scale', 'colour', 'glow_colour',
           'geo_epoch', 'geometry_id', 'water_tint', 'water_params',
           'water_plane', 'glass_color', 'glass_params', 'fog_color',
           'fog_params')

TEXTURES = ('default.png', 'brick.png', 'stone.png', 'caulk.jpg', 'wood.png')
SHADERS = (None, 'Glass', 'Glow', 'Fog', 'Water')


def _brush(i, rng):
    b = {'id': 'b%d' % i, 'name': 'b%d' % i,
         'pos': [float(i * 80), 0.0, 0.0], 'size': [64.0, 64.0, 64.0],
         'textures': {f: rng.choice(TEXTURES) for f in rt.CUBE_FACE_KEYS}}
    if rng.random() < 0.3:
        bg.clip_brush(b, (0.0, 1.0, 1.0), rng.uniform(10.0, 40.0))
    if rng.random() < 0.2:
        b['shader'] = rng.choice(SHADERS[1:])
    return b


def _edit(brush, rng):
    """One authored edit of the kind the editor journals."""
    kind = rng.randrange(5)
    if kind == 0:
        brush['textures'][rng.choice(rt.CUBE_FACE_KEYS)] = rng.choice(TEXTURES)
    elif kind == 1:
        shader = rng.choice(SHADERS)
        if shader is None:
            brush.pop('shader', None)
        else:
            brush['shader'] = shader
    elif kind == 2:
        brush['tint'] = [rng.randrange(256) for _ in range(3)]
    elif kind == 3:
        brush['is_mover'] = not brush.get('is_mover', False)
    else:
        if bg.brush_has_geometry(brush):
            bg.translate_brush(brush, (rng.uniform(-50, 50), 0.0, 0.0))
        else:
            brush['uv_scale'] = {'top': [rng.uniform(0.5, 2), 1.0]}


def _texture_names(table, slots):
    return [[table.texture_names()[i] if i >= 0 else None
             for i in table.tex_name_id[s]] for s in slots]


def _assert_same(live, fresh, brushes):
    n = len(brushes)
    assert live.count == fresh.count == n
    for name in COLUMNS:
        np.testing.assert_array_equal(
            getattr(live, name)[:n], getattr(fresh, name)[:n],
            err_msg="column %r diverged from a from-scratch rebuild" % name)
    # Texture ids are table-local intern ids; compare the names they mean.
    assert _texture_names(live, range(n)) == _texture_names(fresh, range(n))
    np.testing.assert_array_equal(live.dynamic_slots, fresh.dynamic_slots)
    assert ([r.signature for r in live.geometry_records]
            == [r.signature for r in fresh.geometry_records])


@pytest.mark.parametrize("seed", range(12))
def test_row_local_refresh_matches_a_rebuild(seed, monkeypatch):
    rng = random.Random(seed)
    brushes = [_brush(i, rng) for i in range(30)]
    table = RenderTable()
    epoch = 1
    table.sync(brushes, epoch)
    generation = table.generation

    reconciles = []
    real = RenderTable._reconcile
    monkeypatch.setattr(RenderTable, '_reconcile',
                        lambda self, *a, **k: (reconciles.append(self is table),
                                               real(self, *a, **k)))

    for _ in range(25):
        changed = rng.sample(brushes, rng.randint(1, 4))
        for brush in changed:
            _edit(brush, rng)
        epoch += 1
        table.sync(brushes, epoch, dirty_objects={id(b) for b in changed})

        fresh = RenderTable()
        fresh.sync(brushes, 1)
        _assert_same(table, fresh, brushes)

    assert not any(reconciles), (
        "a journalled edit with an unchanged row set reconciled")
    assert table.generation == generation, "slots were re-addressed for no reason"


def test_a_journalled_edit_resolves_only_its_rows(monkeypatch):
    rng = random.Random(1)
    brushes = [_brush(i, rng) for i in range(200)]
    table = RenderTable()
    table.sync(brushes, 1)
    resolved = []
    real = RenderTable._resolve_cold_rows
    monkeypatch.setattr(RenderTable, '_resolve_cold_rows',
                        lambda self, slots, b: (resolved.extend(slots),
                                                real(self, slots, b)))

    brushes[57]['textures']['top'] = 'brick.png'
    table.begin_frame(brushes, 2, dirty_objects={id(brushes[57])})

    assert resolved == [57]
    assert table.texture_names()[table.tex_name_id[57, 5]] == 'brick.png'


def test_convex_to_box_changes_fall_back_to_a_reconcile():
    """Adding or removing geometry changes the dense geometry layout."""
    rng = random.Random(2)
    brushes = [_brush(i, rng) for i in range(10)]
    convex = next(b for b in brushes if bg.brush_has_geometry(b))
    table = RenderTable()
    table.sync(brushes, 1)
    generation = table.generation

    convex.pop('geometry')
    bg._invalidate(convex)
    table.sync(brushes, 2, dirty_objects={id(convex)})

    assert table.generation != generation
    fresh = RenderTable()
    fresh.sync(brushes, 1)
    _assert_same(table, fresh, brushes)


def test_refresh_rows_keeps_the_dense_geometry_layout():
    """``refresh_rows`` used to write a slot-indexed record into the dense list.

    ``geometry_records`` is indexed by ``geometry_id``, not by slot; with a box
    brush ahead of a convex one those differ, and the old code overwrote
    another row's record (or indexed past the end).
    """
    box = {'id': 'box', 'pos': [0, 0, 0], 'size': [64, 64, 64]}
    convex = {'id': 'cvx', 'pos': [200, 0, 0], 'size': [64, 64, 64]}
    bg.clip_brush(convex, (0.0, 1.0, 1.0), 30.0)
    brushes = [box, box.copy() | {'id': 'box2'}, convex]
    table = RenderTable()
    table.sync(brushes, 1)
    assert table.geometry_id.tolist()[:3] == [-1, -1, 0]

    bg.translate_brush(convex, (10.0, 0.0, 0.0))
    table.refresh_rows(brushes, [2])

    assert len(table.geometry_records) == 1
    assert table.geometry_records[0].signature == bg.geometry_signature(convex)
    assert table.geometry_id.tolist()[:3] == [-1, -1, 0]


def test_a_reshaped_convex_brush_is_noticed_while_it_is_being_edited():
    """Component edits reshape the selection over a drag, after one checkpoint.

    Its rows are passed as *edited*, and the geometry epoch says which of them
    actually changed shape.
    """
    convex = {'id': 'cvx', 'pos': [0, 0, 0], 'size': [64, 64, 64]}
    bg.clip_brush(convex, (0.0, 1.0, 1.0), 30.0)
    brushes = [convex]
    table = RenderTable()
    table.sync(brushes, 1)

    bg.clip_brush(convex, (1.0, 0.0, 0.0), 20.0)     # no epoch, no journal
    table.begin_frame(brushes, 1)
    assert table.geometry_records[0].signature != bg.geometry_signature(convex)

    table.begin_frame(brushes, 1, edited=[convex])
    assert table.geometry_records[0].signature == bg.geometry_signature(convex)


# ---------------------------------------------------------------------------
# The other buffer
# ---------------------------------------------------------------------------

def test_a_table_adopts_its_peer_instead_of_rebuilding(monkeypatch):
    """After a load or an undo both buffers need every row; one pays."""
    rng = random.Random(9)
    brushes = [_brush(i, rng) for i in range(40)]
    first, second = RenderTable(), RenderTable()
    first.begin_frame(brushes, 5)

    reconciles = []
    real = RenderTable._reconcile
    monkeypatch.setattr(RenderTable, '_reconcile',
                        lambda self, *a, **k: (reconciles.append(self),
                                               real(self, *a, **k)))
    second.begin_frame(brushes, 5, peer=first)

    assert reconciles == []
    fresh = RenderTable()
    real(fresh, tuple(brushes), True)
    _assert_same(second, fresh, brushes)
    # A copy, not a share: the next edit to one must not reach the other.
    assert second.bounds is not first.bounds
    assert second.texture_names() is not first.texture_names()


def test_a_peer_at_another_epoch_or_row_set_is_not_adopted(monkeypatch):
    rng = random.Random(10)
    brushes = [_brush(i, rng) for i in range(10)]
    peer = RenderTable()
    peer.begin_frame(brushes, 5)
    adopted = []
    monkeypatch.setattr(RenderTable, 'adopt', lambda self, p: adopted.append(p))

    RenderTable().begin_frame(brushes, 6, peer=peer)            # newer epoch
    RenderTable().begin_frame(brushes[:-1], 5, peer=peer)       # other rows
    assert adopted == []
