"""The dense render projection: what it classifies, and when it refreshes.

The equivalence tests are the important ones.  ``_sort_objects`` and
``_split_opaque`` are what the renderer used to classify with, per visible brush
per frame; the table has to reach the same verdict from its cold columns or the
refactor changes what gets drawn.
"""

import numpy as np
import pytest

from engine import render_table as rt
from engine.render_table import RenderTable
from engine.spatial import PARKED_HIDDEN_KEY


def _brush(**kw):
    b = {
        'id': kw.pop('id', None) or f'brush-{len(kw)}-{id(kw)}',
        'pos': kw.pop('pos', [0.0, 0.0, 0.0]),
        'size': kw.pop('size', [64.0, 64.0, 64.0]),
        'textures': kw.pop('textures', {f: 'default.png' for f in rt.CUBE_FACE_KEYS}),
    }
    b.update(kw)
    return b


def _synced(brushes, epoch=1):
    t = RenderTable()
    t.sync(brushes, epoch)
    return t


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('kw,bit', [
    ({'shader': 'Water'}, rt.CLASS_WATER),
    ({'is_water': True}, rt.CLASS_WATER),
    ({'textures': dict.fromkeys(rt.CUBE_FACE_KEYS, 'blue_water.png')}, rt.CLASS_WATER),
    ({'shader': 'Fog'}, rt.CLASS_FOG),
    ({'is_fog': True}, rt.CLASS_FOG),
    ({'shader': 'Glass'}, rt.CLASS_GLASS),
    ({'shader': 'Glow'}, rt.CLASS_GLOW),
    ({'is_trigger': True}, rt.CLASS_TRIGGER),
    ({'operation': 'subtract'}, rt.CLASS_SUBTRACT),
])
def test_class_bit_is_set_for_its_brush_kind(kw, bit):
    t = _synced([_brush(id='b', **kw)])
    assert t.class_bits[0] & bit


def test_plain_brush_carries_no_non_opaque_bit():
    t = _synced([_brush(id='b')])
    assert not (t.class_bits[0] & rt.CLASS_NON_OPAQUE)


def test_textured_bit_matches_split_opaque_rule():
    """CLASS_TEXTURED must mean exactly what ``_split_opaque`` meant."""
    cases = [
        ({f: 'default.png' for f in rt.CUBE_FACE_KEYS}, False),
        ({f: 'caulk.jpg' for f in rt.CUBE_FACE_KEYS}, False),
        ({'top': 'wall.png'}, True),
        ({'top': 'default.png', 'down': 'floor.jpg'}, True),
        ({}, False),
    ]
    for textures, expected in cases:
        t = _synced([_brush(id='b', textures=textures)])
        got = bool(t.class_bits[0] & rt.CLASS_TEXTURED)
        legacy = any(x and x not in ('default.png', 'caulk.jpg')
                     for x in textures.values())
        assert got is expected is legacy, textures


def test_shadow_caster_bit_excludes_what_the_shadow_pass_excluded():
    casters = [_brush(id='solid')]
    non = [_brush(id='t', is_trigger=True),
           _brush(id='f', is_fog=True),
           _brush(id='w', shader='Water'),
           _brush(id='g', shader='Glass'),
           _brush(id='o', shader='Glow'),
           _brush(id='s', operation='subtract'),
           _brush(id='h', hidden=True)]
    t = _synced(casters + non)
    assert t.class_bits[0] & rt.CLASS_SHADOW_CASTER
    for i in range(1, len(non) + 1):
        assert not (t.class_bits[i] & rt.CLASS_SHADOW_CASTER), t.ids[i]


# ---------------------------------------------------------------------------
# hidden: authored in the column, live read separately
# ---------------------------------------------------------------------------

def test_class_bits_carry_authored_hidden_not_the_parked_flag():
    """A Big World parked brush is hidden *now* but not hidden *by the map*."""
    parked = _brush(id='p', hidden=True)
    parked[PARKED_HIDDEN_KEY] = False          # authored value stashed by parking
    t = _synced([parked])
    assert not (t.class_bits[0] & rt.CLASS_HIDDEN_AUTHORED)
    # ...and it still casts shadows, which is what keeps the collision/shadow
    # world stable while a cell is streamed out.
    assert t.class_bits[0] & rt.CLASS_SHADOW_CASTER


def test_live_hidden_sees_a_park_the_table_never_refreshed_for():
    b = _brush(id='p')
    t = _synced([b])
    assert not t.live_hidden([b])[0]
    b['hidden'] = True                          # parked, with no notification
    assert t.live_hidden([b])[0]


# ---------------------------------------------------------------------------
# Refresh discipline
# ---------------------------------------------------------------------------

def test_same_epoch_and_same_rows_is_a_no_op():
    brushes = [_brush(id='a'), _brush(id='b')]
    t = _synced(brushes, epoch=7)
    gen = t.generation
    assert t.sync(brushes, 7) is False
    assert t.generation == gen


def test_epoch_bump_re_resolves_cold_columns():
    b = _brush(id='a')
    t = _synced([b], epoch=1)
    assert not (t.class_bits[0] & rt.CLASS_GLASS)
    b['shader'] = 'Glass'                       # an editor edit
    assert t.sync([b], 2) is True
    assert t.class_bits[0] & rt.CLASS_GLASS


def test_structural_change_keeps_survivors_cold_columns():
    """A row that survives is not re-resolved -- that is what keeps a
    structural change from costing a full level re-classification."""
    a, b = _brush(id='a', shader='Glass'), _brush(id='b')
    t = _synced([a, b], epoch=1)
    assert t.class_bits[t.slot_of_id['a']] & rt.CLASS_GLASS

    # Mutate 'a' behind the table's back, then force a structural change at the
    # same epoch by inserting a new brush ahead of it.
    a['shader'] = 'Glow'
    c = _brush(id='c', is_trigger=True)
    assert t.sync([c, a, b], 1) is True

    slot_a = t.slot_of_id['a']
    assert t.class_bits[slot_a] & rt.CLASS_GLASS      # survivor: not re-resolved
    assert t.class_bits[t.slot_of_id['c']] & rt.CLASS_TRIGGER   # new row: resolved
    assert t.slot_of_id['b'] == 2


def test_transforms_refresh_without_touching_cold_columns():
    b = _brush(id='m', shader='Glass')
    t = _synced([b], epoch=1)
    cold = t.class_bits[0]
    b['pos'] = [128.0, 32.0, -64.0]
    t.refresh_transforms([b], [0])
    assert list(t.center[0]) == [128.0, 32.0, -64.0]
    assert t.class_bits[0] == cold


def test_rotation_is_zero_for_an_unrotated_brush():
    t = _synced([_brush(id='a'), _brush(id='r', _rot_angle=45.0,
                                        rot_axis=[0, 1, 0])])
    assert not t.rot[0].any()
    assert t.rot[1, 3] == 45.0


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def test_rows_are_named_by_the_brushs_existing_uuid():
    a, b = _brush(id='uuid-a'), _brush(id='uuid-b')
    t = _synced([a, b])
    assert t.ids == ['uuid-a', 'uuid-b']
    assert t.slot_of_id == {'uuid-a': 0, 'uuid-b': 1}


def test_reordering_moves_slots_but_not_names():
    a, b = _brush(id='a', shader='Glass'), _brush(id='b', shader='Glow')
    t = _synced([a, b], epoch=1)
    t.sync([b, a], 1)
    assert t.class_bits[t.slot_of_id['a']] & rt.CLASS_GLASS
    assert t.class_bits[t.slot_of_id['b']] & rt.CLASS_GLOW


# ---------------------------------------------------------------------------
# Texture interning
# ---------------------------------------------------------------------------

def test_texture_names_intern_to_dense_ids():
    t = _synced([_brush(id='a', textures={'top': 'wall.png', 'down': 'wall.png',
                                          'north': 'floor.png'})])
    names = t.texture_names()
    top = t.tex_name_id[0, rt.CUBE_FACE_KEYS.index('top')]
    down = t.tex_name_id[0, rt.CUBE_FACE_KEYS.index('down')]
    north = t.tex_name_id[0, rt.CUBE_FACE_KEYS.index('north')]
    assert top == down and top != north
    assert names[top] == 'wall.png'
    assert names[north] == 'floor.png'
    # Faces the brush does not name fall back to the default, as the renderer's
    # batch build did.
    assert names[t.tex_name_id[0, rt.CUBE_FACE_KEYS.index('east')]] == 'default.png'


def test_columns_are_a_pure_projection():
    """Deleting the table and rebuilding it yields identical bits."""
    brushes = [_brush(id='a', shader='Glass', pos=[1, 2, 3]),
               _brush(id='b', is_trigger=True),
               _brush(id='c', textures={'top': 'wall.png'})]
    first = _synced(brushes, epoch=3)
    second = _synced(brushes, epoch=3)
    for name in ('class_bits', 'center', 'half', 'rot', 'tex_name_id',
                 'uv_angle', 'uv_shift', 'uv_scale', 'geo_epoch'):
        np.testing.assert_array_equal(getattr(first, name)[:first.count],
                                      getattr(second, name)[:second.count])
