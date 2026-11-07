"""Stage 1 changes exactly the shipped faces it names, and no other.

Stage 1 (one face record, ``docs/brush-model-proposal.md``) draws each face
from its one material record. On shipped maps that changes what is drawn in
one place only: two convex brushes in ``_SHOWCASE`` whose authored per-tag
``uv_scale`` -- the value the Surface Inspector showed -- was never on their
planes, so the renderer drew 1.0 instead. Their faces now draw the authored
value. Every other face of every shipped map draws as it did.

The Stage 0 reading is frozen below (it is what
``tests/helpers/brush_semantics`` said before Stage 1); the Stage 1 reading
is the helper. Both are compared *as drawn*: a missing scale is the 1.0 it
draws as, and a Natural face's stored scale is not drawn. The pixel-level
check of the same claim is ``tools/compare_map_pixels.py``.
"""

import pytest

from engine import brush_geometry as bg
from tests.helpers import brush_semantics as bs
from tests.helpers.paths import REPO_ROOT

MAPS = bs.shipped_maps(REPO_ROOT)

QUARTER = '5b10b49b-9b6c-45d9-91ee-0b6f06970590'
TEN_BY_EIGHT = '8c04e483-4d44-4217-a03d-32844a727043'

#: ``(map, brush id, face tag) -> (Stage 0 scale, Stage 1 scale)`` as drawn.
INTENDED = {
    **{('_SHOWCASE.json', QUARTER, tag): ((1.0, 1.0), (0.25, 0.25))
       for tag in ('west', 'down', 'north', 'south')},
    **{('_SHOWCASE.json', TEN_BY_EIGHT, tag): ((1.0, 1.0), (10.0, 8.0))
       for tag in ('east', 'west', 'top', 'down', 'north', 'south')},
}


def _stage0_pass(brush):
    for name in (brush.get('textures') or {}).values():
        if name and name not in bs.UNTEXTURED:
            return 'textured'
    return 'solid'


def _stage0_faces(brush):
    """Frozen: the convex material rule before Stage 1 (plane only; Natural
    from the per-tag flag only; no tiling)."""
    convex = bg.get_convex(brush)
    out = []
    for face in convex.faces:
        plane = convex.planes[face['plane']]
        scale = face.get('uv_scale') or plane.get('uv_scale')
        shift = plane.get('uv_shift', (0.0, 0.0)) or (0.0, 0.0)
        out.append((face.get('face'), {
            'texture': face.get('texture') or bs.DEFAULT_TEXTURE,
            'uv_scale': None if scale is None else (float(scale[0]), float(scale[1])),
            'uv_angle': float(plane.get('uv_angle', 0.0)),
            'uv_shift': (float(shift[0]), float(shift[1])),
            'natural': bool(bg.face_uses_natural_scale(brush, face.get('face'), face)),
        }))
    return out


def _drawn(material):
    """The state a face is drawn with: Natural replaces the scale, and a
    missing scale draws as 1.0."""
    scale = None if material['natural'] else (material['uv_scale'] or (1.0, 1.0))
    return (material['texture'], scale, material['uv_angle'], material['uv_shift'],
            material['natural'])


def _changes():
    changes = {}
    for name, brushes in MAPS.items():
        for brush in brushes:
            if bs.encoding(brush) == 'box':
                continue        # the box path is untouched by Stage 1
            assert bs.draw_pass(bs.migrated(brush)) == _stage0_pass(brush), (
                name, brush.get('id'))
            if bs.encoding(brush) != 'convex':
                continue
            before = _stage0_faces(brush)
            # As the editor loads it: a legacy map is migrated at load.
            after = [(tag, material) for tag, _ring, material
                     in bs.render_faces(bs.migrated(brush))]
            for (tag, old), (_tag, new) in zip(before, after):
                if _drawn(old) != _drawn(new):
                    changes[(name, brush.get('id'), tag)] = (_drawn(old), _drawn(new))
    return changes


def test_only_the_named_showcase_faces_draw_differently():
    changes = _changes()
    assert set(changes) == set(INTENDED)
    for key, (old, new) in changes.items():
        texture, scale, angle, shift, natural = old
        assert (scale, new[1]) == INTENDED[key], key
        # Nothing but the scale moves: texture, rotation, shift and mode stay.
        assert (texture, angle, shift, natural) == (new[0], new[2], new[3], new[4]), key


@pytest.mark.parametrize("box_map", sorted(name for name, brushes in MAPS.items()
                                           if all(bs.encoding(b) == 'box' for b in brushes)))
def test_a_box_only_map_is_read_exactly_as_before(box_map):
    """Box faces read the same per-tag dicts in the same way."""
    import json
    with open(REPO_ROOT + '/tests/brush_model/semantics_snapshot.json') as handle:
        recorded = json.load(handle)[box_map]
    assert bs.digest(MAPS[box_map]) == recorded['digest']
