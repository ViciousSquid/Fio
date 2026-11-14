"""Stage 2a changes exactly the shipped bounds it names, and nothing drawn.

Stage 2a (derived bounds, ``docs/brush-model-stage2.md``) makes every
bound-reader -- culling, collision rows, the physics grid, Big World,
picking -- take a convex brush's bound from its planes instead of the
``pos``/``size`` copy the map stores. On shipped maps that copy is right
everywhere except one brush, shared by two maps: ``Terrain_Test_small`` and
``Topdown_example`` both carry brush ``1a817c94``, clipped along -X without
its copy being re-synced, so the copy is 128 units wider than the solid.
Its bound now shrinks to the solid. One more bound moves by less than a
ten-thousandth of a unit: ``Angled_Brush_Test``'s ``bcac8b00`` stored a
copy rounded to three decimals (163.923 for 163.92304845...), 4.85e-5
*smaller* than its solid, so its bound grows by that much and is now
conservative. Nothing any shipped brush *draws* changes: the faces (rings
and materials) are identical, and so is every other bound beyond the
solver's last bits (``DRIFT_NOISE``).

The pre-Stage-2a reading of a bound is frozen below (``pos``/``size``);
the current reading is the helper. The pixel-level check of "nothing drawn
changes" is ``tools/compare_map_pixels.py``.
"""

import numpy as np
import pytest

from tests.helpers import brush_semantics as bs
from tests.helpers.paths import REPO_ROOT

MAPS = bs.shipped_maps(REPO_ROOT)

CLIPPED_TERRAIN = '1a817c94'
ROUNDED_COPY = 'bcac8b00'

#: ``(map, brush id prefix) -> (stored centre, size) -> (derived centre, size)``.
INTENDED = {
    ('Terrain_Test_small.json', CLIPPED_TERRAIN): (((280.0, 0.0), (464.0,)),
                                                   ((344.0, 0.0), (336.0,))),
    ('Topdown_example.json', CLIPPED_TERRAIN): (((536.0, 0.0), (464.0,)),
                                                ((600.0, 0.0), (336.0,))),
}


def _changed_bounds():
    out = {}
    for name, brushes in MAPS.items():
        for raw in brushes:
            if not raw.get(bs.WORLD_PLANES):
                continue        # Stage 2a characterises legacy world-plane maps
            brush = bs.migrated(raw)
            stored = tuple(map(np.asarray, bs.stored_bound(brush)))
            derived = tuple(map(np.asarray, bs.bound(brush)))
            drift = max(float(np.abs(stored[0] - derived[0]).max()),
                        float(np.abs(stored[1] - derived[1]).max()))
            if drift > bs.DRIFT_NOISE:
                out[(name, str(raw.get('id'))[:8])] = (stored, derived)
    return out


def test_only_the_named_bounds_change():
    changed = _changed_bounds()
    rounded = changed.pop(('Angled_Brush_Test.json', ROUNDED_COPY))
    assert set(changed) == set(INTENDED)
    stored, derived = rounded
    assert np.allclose(derived[0], stored[0], atol=1e-10)
    grown = derived[1] - stored[1]
    assert (grown >= 0.0).all() and grown.max() == pytest.approx(4.85e-5, abs=1e-7)
    for key, (stored, derived) in changed.items():
        (s_x, _), (s_w,) = INTENDED[key][0]
        (d_x, _), (d_w,) = INTENDED[key][1]
        # Only X moves: the -X side the clip removed.
        assert stored[0][0] == pytest.approx(s_x) and stored[1][0] == pytest.approx(s_w)
        assert derived[0][0] == pytest.approx(d_x) and derived[1][0] == pytest.approx(d_w)
        assert np.allclose(stored[0][1:], derived[0][1:])
        assert np.allclose(stored[1][1:], derived[1][1:])
        # The new bound is the solid's: smaller, and inside the old one.
        assert (derived[0] - derived[1] / 2 >= stored[0] - stored[1] / 2 - 1e-9).all()
        assert (derived[0] + derived[1] / 2 <= stored[0] + stored[1] / 2 + 1e-9).all()


@pytest.mark.parametrize("name", sorted(MAPS))
def test_no_shipped_brush_draws_differently(name):
    """The faces each brush draws -- world rings, posed, and materials --
    are the planes' (or the box's), whatever the stored copy says."""
    for raw in MAPS[name]:
        brush = bs.migrated(raw)
        for tag, ring, _material in bs.render_faces(brush):
            assert np.allclose(
                bs.posed(brush, ring), np.asarray(ring), atol=1e-9, rtol=0.0), (
                    name, raw.get('id'), tag)
