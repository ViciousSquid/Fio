"""A damaged terrain record costs that record, not the whole map load.

``Terrain.from_dict`` reads a map file, and maps arrive in shared packages. A
heightmap blob that is not a plain 2-D ``.npy`` (corrupt base64, a header
promising data it does not hold, an ``.npz``) or a malformed sculpt entry used
to raise out of ``from_dict`` and abort loading the level.
"""

import base64
import io

import numpy as np
import pytest

pytest.importorskip("OpenGL", reason="engine.terrain imports PyOpenGL")

from engine.terrain import Terrain                 # noqa: E402

pytestmark = pytest.mark.qt


def _npy(array):
    buf = io.BytesIO()
    np.save(buf, array)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _lying_header():
    buf = io.BytesIO()
    np.lib.format.write_array_header_1_0(
        buf, {"descr": "<f4", "fortran_order": False, "shape": (200_000, 200_000)})
    buf.write(b"\0" * 16)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _npz():
    buf = io.BytesIO()
    np.savez(buf, a=np.zeros((2, 2)))
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _loaded(**fields):
    terrain = Terrain(seed=3)
    data = terrain.to_dict()
    data.update(fields)
    terrain.from_dict(data)
    return terrain


def test_a_saved_heightmap_round_trips():
    heights = np.linspace(0, 1, 12, dtype=np.float32).reshape(3, 4)
    terrain = _loaded(heightmap_blob=_npy(heights))
    np.testing.assert_array_equal(terrain.heightmap_data, heights)


@pytest.mark.parametrize("blob", [
    "@@not base64@@",
    _lying_header(),
    _npz(),
    _npy(np.zeros(5, dtype=np.float32)),
    _npy(np.array([["a", "b"]])),
    _npy(np.array([[{"x": 1}]], dtype=object)),
])
def test_an_unusable_heightmap_is_dropped(blob):
    assert _loaded(heightmap_blob=blob).heightmap_data is None


def test_malformed_sculpt_entries_are_skipped():
    terrain = _loaded(sculpt_offsets=[[1, 2, 0.5], [3, 4], "xyz", [5, "z", 1.0], None])
    assert terrain.sculpt_offsets == {(1, 2): 0.5}
