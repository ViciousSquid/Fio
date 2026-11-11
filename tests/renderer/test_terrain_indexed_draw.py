"""The heightfield is drawn indexed, with flat face outputs (M5).

``terrain.vert`` shaded six unshared vertices a quad; the drawn program
shades two layers of the grid's points and takes a triangle's flat normal and
colour from its provoking (last) vertex. These check the index list against
the order and corners ``terrain.vert`` documents; the images are compared in
``tests/visual/test_terrain_heightfield_parity.py``."""

import numpy as np
import pytest

pytest.importorskip("OpenGL")

from engine import shaders                                # noqa: E402
from engine.terrain import heightfield_indices            # noqa: E402

pytestmark = [pytest.mark.qt]


def _corner_points(res):
    """terrain.vert's six corners of every quad, as grid points (i, k)."""
    out = []
    for q in range(res * res):
        ix, iz = divmod(q, res)
        out.append([(ix, iz), (ix + 1, iz), (ix, iz + 1),
                    (ix + 1, iz), (ix + 1, iz + 1), (ix, iz + 1)])
    return out


@pytest.mark.parametrize("res", [1, 2, 8, 48])
def test_every_triangle_keeps_its_corners_and_winding(res):
    side, points = res + 1, (res + 1) ** 2
    indices = heightfield_indices(res).reshape(-1, 2, 3)
    assert indices.shape[0] == res * res
    for quad, corners in zip(indices, _corner_points(res)):
        for triangle, (a, b, c) in zip(quad, (corners[:3], corners[3:])):
            drawn = [divmod(int(v) % points, side) for v in triangle]
            # The same three points, in a rotation of (a, b, c): same facing.
            assert drawn in ([a, b, c], [b, c, a], [c, a, b])


@pytest.mark.parametrize("res", [1, 2, 8, 48])
def test_each_provoking_vertex_carries_one_triangle_only(res):
    side, points = res + 1, (res + 1) ** 2
    indices = heightfield_indices(res).reshape(-1, 2, 3)
    last = indices[:, :, 2]
    assert len(np.unique(last)) == last.size
    ix, iz = np.divmod(np.arange(res * res), res)
    # First triangle: layer 0, the quad's own point. Second: layer 1, the
    # point diagonally across -- what terrain_indexed.vert assumes.
    assert np.array_equal(last[:, 0], ix * side + iz)
    assert np.array_equal(last[:, 1], points + (ix + 1) * side + iz + 1)
    assert int(indices.max()) < 2 * points


def test_far_fewer_vertices_are_shaded():
    res = 48
    assert len(np.unique(heightfield_indices(res))) <= 2 * (res + 1) ** 2
    assert 6 * res * res > 2.8 * 2 * (res + 1) ** 2


def test_the_drawn_program_is_the_reference_with_flat_face_outputs():
    vert = shaders.DEFAULT_SHADERS['terrain_indexed.vert']
    frag = shaders.DEFAULT_SHADERS['terrain_flat.frag']
    reference = shaders.DEFAULT_SHADERS['terrain.vert']
    head = vert.split('void main() {')[0].replace('flat out', 'out')
    assert head == reference.split('void main() {')[0]
    for name in ('Normal', 'VertexColor'):
        assert 'flat out mediump vec3 %s;' % name in vert
        assert 'flat in vec3 %s;' % name in frag
    assert 'flat in' not in shaders.DEFAULT_SHADERS['terrain.frag']
    assert shaders.SHADER_MAP['terrain'] == ('terrain_indexed.vert', 'terrain_flat.frag')
