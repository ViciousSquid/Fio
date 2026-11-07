"""The precedence rule that settles a legacy face's two material homes.

Before Stage 1 a tagged face of a convex brush could carry its material on
its plane *and* in the brush's per-tag dicts. ``engine.brush_faces``
settles every such pair by an explicit rule, field by field
(``PRECEDENCE``); this file is that rule's specification:

=============  ================  ===========  ==========  ===============
field          per-tag only      plane only   both equal  both differ
=============  ================  ===========  ==========  ===============
texture        plane (default)   plane        either      **plane**
uv_scale       per-tag           plane        either      **per-tag**
uv_shift       per-tag           plane        either      **per-tag**
uv_angle       per-tag           plane        either      **per-tag**
uv_natural     set               set          set         set if either
=============  ================  ===========  ==========  ===============

Not "the per-tag value always wins": a per-tag texture never decides a
convex face (its only writers judged faces by the AABB and were never
drawn), and a plane value stands wherever no per-tag value was authored.
The reasons are in ``engine/brush_faces.py``; the cases below are the
evidence for each.

The rule is applied once, by the load-time migration of a map saved before
the canonical layout (``migrate_legacy``). After it every field has one
value, on the plane, and nothing at run time reads a per-tag dict for a
convex face -- so a stale per-tag value can never reappear (Natural, once
disabled, stays disabled).
"""

import copy
import itertools

import pytest

from engine import brush_faces as bf
from engine import brush_geometry as bg
from engine.render_table import RenderTable
from tests.helpers.worlds import angled_brush, box_brush

VALUES = {
    'texture': ('Dev/128.jpg', 'Dev/256.jpg'),
    'uv_scale': ((0.25, 0.25), (2.0, 4.0)),
    'uv_shift': ((0.5, 0.0), (0.1, 0.9)),
    'uv_angle': (30.0, 90.0),
    'uv_natural': (True, True),
}

#: For each field, the value kept for (per-tag value, plane value), with
#: ``None`` for "no value in that home". Index 0 is the per-tag value, 1 the
#: plane's.
EXPECTED = {
    'texture':    {'tag only': None, 'plane only': 1, 'equal': 0, 'differ': 1},
    'uv_scale':   {'tag only': 0, 'plane only': 1, 'equal': 0, 'differ': 0},
    'uv_shift':   {'tag only': 0, 'plane only': 1, 'equal': 0, 'differ': 0},
    'uv_angle':   {'tag only': 0, 'plane only': 1, 'equal': 0, 'differ': 0},
    'uv_natural': {'tag only': 0, 'plane only': 1, 'equal': 0, 'differ': 0},
}


def _legacy(field, case):
    """An angled brush whose 'top' face holds *field* as *case* describes."""
    brush = angled_brush("legacy", (0, 0, 0), (96, 96, 96))
    plane = next(p for p in brush['geometry']['planes'] if p.get('face') == 'top')
    for key in bf.FIELDS:
        plane.pop(key, None)
    tag_value, plane_value = VALUES[field]
    if case == 'equal':
        plane_value = tag_value
    if case in ('tag only', 'equal', 'differ'):
        stored = list(tag_value) if isinstance(tag_value, tuple) else tag_value
        brush[bf.TAG_DICTS[field]] = {'top': stored}
    if case in ('plane only', 'equal', 'differ'):
        plane[field] = list(plane_value) if isinstance(plane_value, tuple) else plane_value
    bg.invalidate_geometry_cache(brush)
    return brush, (tag_value, plane_value)


@pytest.mark.parametrize("field,case", list(itertools.product(
    bf.FIELDS, ('tag only', 'plane only', 'equal', 'differ'))))
def test_the_rule_for_each_field_and_case(field, case):
    brush, values = _legacy(field, case)
    expected_index = EXPECTED[field][case]
    expected = None if expected_index is None else values[expected_index]
    plane = next(p for p in brush['geometry']['planes'] if p.get('face') == 'top')
    assert bf.legacy_record(brush, plane).get(field) == expected
    # Before migration nothing reads the per-tag copy: the plane is the record.
    assert bf.face_record(brush, 'top').get(field) == bf.plane_value(plane, field)
    # The load-time migration settles it: the rule's value, on the plane,
    # with no second copy.
    resolutions = bf.migrate_legacy(brush)
    assert bf.face_record(brush, 'top').get(field) == expected
    assert 'top' not in (brush.get(bf.TAG_DICTS[field]) or {})
    plane = next(p for p in brush['geometry']['planes'] if p.get('face') == 'top')
    assert bf.plane_value(plane, field) == expected
    kept = {r.field: r.kept for r in resolutions}
    if case == 'differ' and field != 'uv_natural':
        assert kept[field] == ('plane' if field == 'texture' else 'tag')
    # And what is drawn is that record.
    table = RenderTable()
    table.sync([brush], 1)
    convex = bg.get_convex(brush)
    face = next(f for f in convex.faces if f.get('face') == 'top')
    assert table.geometry_records[0].materials[id(face)] == bf.resolve(
        bf.face_record(brush, 'top'))


def test_a_stray_per_tag_value_is_never_read_at_run_time():
    """On a brush in the canonical layout a per-tag entry for a convex face
    is not a second opinion: it is ignored, and a canonical load drops it."""
    brush = angled_brush("canonical", (0, 0, 0), (96, 96, 96))
    bf.set_face_material(brush, 'top', uv_scale=(2.0, 2.0))
    brush['uv_scale'] = {'top': [9.0, 9.0]}            # a plugin's stray write
    assert bf.face_material(brush, 'top').uv_scale == (2.0, 2.0)
    assert bf.drop_tag_copies(brush) == 1
    assert 'uv_scale' not in brush
    assert bf.face_material(brush, 'top').uv_scale == (2.0, 2.0)


def test_a_neither_home_face_takes_the_defaults():
    brush, _ = _legacy('uv_shift', 'tag only')
    brush.pop('uv_shift')
    material = bf.face_material(brush, 'top')
    assert material == bf.Material('default.png', None, (0.0, 0.0), 0.0, False)


def test_the_showcase_case_honours_the_authored_scale():
    """Two shipped _SHOWCASE brushes: a per-tag scale the Surface Inspector
    showed, no scale on the plane. Migrated, the authored value is the
    record."""
    brush = angled_brush("showcase", (0, 0, 0), (96, 96, 96))
    for plane in brush['geometry']['planes']:
        plane.pop('uv_scale', None)
    brush['uv_scale'] = {'north': [0.25, 0.25], 'east': [10, 8]}
    bg.invalidate_geometry_cache(brush)
    bf.migrate_legacy(brush)
    assert bf.face_material(brush, 'north').uv_scale == (0.25, 0.25)
    assert bf.face_material(brush, 'east').uv_scale == (10.0, 8.0)


def test_an_autocaulk_texture_on_a_convex_face_is_not_drawn():
    """Autocaulk wrote ``nodraw`` to per-tag dicts by testing AABB faces; on
    a sloped brush that is not a judgement about the face. The plane keeps
    deciding, as it always did."""
    brush, _ = _legacy('texture', 'differ')
    brush['textures'] = {'top': 'nodraw.jpg'}
    bf.migrate_legacy(brush)
    assert bf.face_material(brush, 'top').texture == 'Dev/256.jpg'


def test_a_cut_face_has_only_its_plane():
    brush = angled_brush("cut", (0, 0, 0), (96, 96, 96), clip_normal=(0.3, 1.0, 0.2))
    index = next(i for i, p in enumerate(brush['geometry']['planes']) if not p.get('face'))
    brush['geometry']['planes'][index]['uv_angle'] = 20.0
    brush['uv_angle'] = {'#%d' % index: 75.0}      # a junk key Page Up/Down wrote
    bg.invalidate_geometry_cache(brush)
    bf.migrate_legacy(brush)
    assert bf.face_material(brush, '#%d' % index).uv_angle == 20.0


def test_normalising_leaves_the_source_document_untouched():
    """The loader shallow-copies each brush from the map document; the
    normalisation must not reach through that copy."""
    brush, _ = _legacy('uv_scale', 'differ')
    document = copy.deepcopy(brush)
    loaded = dict(document)
    bf.migrate_legacy(loaded)
    assert document == brush
    assert loaded != document


def test_normalising_is_idempotent_and_skips_boxes():
    brush, _ = _legacy('uv_shift', 'differ')
    bf.migrate_legacy(brush)
    again = copy.deepcopy(brush)
    assert bf.migrate_legacy(again) == []
    assert again['geometry'] == brush['geometry']
    box = box_brush("box", (0, 0, 0), (64, 64, 64), textures={'top': 'Dev/128.jpg'},
                    uv_shift={'top': [0.5, 0.5]})
    before = copy.deepcopy(box)
    assert bf.migrate_legacy(box) == []
    assert box == before


def test_orphan_per_tag_entries_are_kept():
    """Per-tag entries for a tag no plane carries describe no face; they are
    left as they are (they are used again if the brush returns to a box)."""
    brush = angled_brush("orphan", (0, 0, 0), (96, 96, 96))
    tags = {p.get('face') for p in brush['geometry']['planes'] if p.get('face')}
    missing = next(t for t in bf.FACE_TAGS if t not in tags) if len(tags) < 6 else None
    brush['uv_shift'] = {'top': [0.5, 0.5], 'nowhere': [0.1, 0.1]}
    if missing:
        brush['uv_shift'][missing] = [0.2, 0.2]
    bf.migrate_legacy(brush)
    assert 'top' not in brush['uv_shift'] and brush['uv_shift']['nowhere'] == [0.1, 0.1]


def test_a_vertex_drag_keeps_every_field_of_every_face():
    """Rebuilding the plane set from dragged corners carries the whole record
    (Stage 0: shift, angle and Natural were dropped, cut faces included)."""
    brush = angled_brush("drag", (0, 0, 0), (128, 128, 128), clip_normal=(0.3, 1.0, 0.2))
    cut = next(i for i, p in enumerate(brush['geometry']['planes']) if not p.get('face'))
    bf.set_face_material(brush, 'top', uv_shift=(0.25, 0.5), uv_angle=30.0)
    bf.set_face_material(brush, '#%d' % cut, uv_shift=(0.75, 0.0), uv_angle=60.0,
                         uv_natural=True)
    before = {key: bf.face_material(brush, key) for key in bf.face_keys(brush)}
    points = bg.brush_points(brush)
    points = points + (points[:, 1:2] > 0) * [0.0, 8.0, 0.0]   # raise the top corners
    assert bg.rebuild_brush_from_points(brush, points)
    after = {key: bf.face_material(brush, key) for key in bf.face_keys(brush)}
    assert after['top'] == before['top']
    moved_cut = [m for k, m in after.items() if k.startswith('#')]
    assert before['#%d' % cut] in moved_cut


# ---------------------------------------------------------------------------
# Natural: independently enabled and disabled; a legacy value never returns
# ---------------------------------------------------------------------------

def _legacy_natural():
    """A legacy brush whose 'top' face is Natural in both homes."""
    brush = angled_brush("natural", (0, 0, 0), (96, 96, 96))
    plane = next(p for p in brush['geometry']['planes'] if p.get('face') == 'top')
    plane['uv_natural'] = True
    brush['uv_natural'] = {'top': True}
    bg.invalidate_geometry_cache(brush)
    bf.migrate_legacy(brush)
    return brush


def _natural(brush, key='top'):
    return bf.face_material(brush, key).natural


def test_natural_is_disabled_and_enabled_on_its_own():
    brush = _legacy_natural()
    assert _natural(brush) and 'uv_natural' not in brush
    bf.set_face_material(brush, 'top', uv_natural=False)
    assert not _natural(brush)
    bf.set_face_material(brush, 'north', uv_natural=True)
    assert _natural(brush, 'north') and not _natural(brush)
    bf.set_face_material(brush, 'top', uv_natural=True)
    assert _natural(brush)
    bf.set_face_material(brush, 'top', uv_scale=(2.0, 2.0))   # scale alone leaves it
    assert _natural(brush)


def test_a_disabled_natural_flag_stays_disabled():
    """Through a stray legacy copy, a vertex drag, a round trip to a box and
    back, and the canonical save/load path."""
    import json as _json
    brush = _legacy_natural()
    bf.set_face_material(brush, 'top', uv_natural=False)
    brush['uv_natural'] = {'top': True}                  # an obsolete copy reappears
    assert not _natural(brush)
    points = bg.brush_points(brush)
    assert bg.rebuild_brush_from_points(brush, points * [1.0, 1.05, 1.0])
    assert not _natural(brush)
    bf.drop_tag_copies(brush)
    bg.drop_geometry(brush)
    assert not bf.box_material(brush, 'top').natural
    bg.box_to_geometry(brush)
    assert not _natural(brush)
    saved = _json.loads(_json.dumps({k: v for k, v in brush.items()
                                     if k not in bg.GEO_RUNTIME_KEYS}))
    bf.drop_tag_copies(saved)                            # a canonical load
    assert not _natural(saved)
