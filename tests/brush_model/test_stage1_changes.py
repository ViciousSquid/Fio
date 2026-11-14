"""Stage 1's one-face material layout is now persisted in the version-5
_showcase map. The old per-tag material migration is still covered by the
synthetic legacy-face tests below; no shipped version-5 face needs migration.

For any remaining legacy world-plane maps, this module compares the frozen
Stage 0 reading with the Stage 1 reading as drawn: missing scale draws as
1.0, and Natural replaces any stored scale. The pixel-level check remains
tools/compare_map_pixels.py.
"""

import pytest

from engine import brush_geometry as bg
from tests.helpers import brush_semantics as bs
from tests.helpers.paths import REPO_ROOT

MAPS = bs.shipped_maps(REPO_ROOT)

QUARTER = '5b10b49b-9b6c-45d9-91ee-0b6f06970590'
TEN_BY_EIGHT = '8c04e483-4d44-4217-a03d-32844a727043'

#: ``(map, brush id, face tag) -> (Stage 0 scale, Stage 1 scale)`` as drawn.
INTENDED = {}


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
            if not brush.get(bs.WORLD_PLANES):
                continue        # version-5 maps are already in the canonical layout
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
