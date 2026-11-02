"""The occlusion box must conservatively enclose the entire rendered cell.

No GL context is required: these tests exercise the exact model matrix passed
to the OpenGL 3.3 probe draw, including all eight corners of its source cube.
"""
import itertools
import os
import sys

import glm
import numpy as np
import pytest

# The no-GPU CI tier deliberately omits PyOpenGL. This matrix test needs
# renderer imports but no actual context; the OpenGL tier runs it.
pytest.importorskip("OpenGL.GL")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
from engine.renderer.forward.occlusion import OcclusionMixin, _PROBE_VERTS  # noqa: E402


@pytest.mark.parametrize("bounds", [
    (100.0, 20.0, -50.0, 12.0, 24.0, 36.0),
    (-1024.0, -32.0, 8192.0, 256.0, 2.0, 64.0),
    (0.0, 0.0, 0.0, 512.0, 100.0, 512.0),
])
def test_probe_model_maps_every_corner_to_union_box(bounds):
    matrix = OcclusionMixin._probe_model_matrix(bounds)
    centre, half = np.array(bounds[:3]), np.array(bounds[3:])
    corners = []
    for signs in itertools.product((-0.5, 0.5), repeat=3):
        world = matrix * glm.vec4(*signs, 1.0)
        corners.append([float(world.x), float(world.y), float(world.z)])
    corners = np.array(corners)
    np.testing.assert_allclose(corners.min(axis=0), centre - half,
                               rtol=0, atol=0.002)
    np.testing.assert_allclose(corners.max(axis=0), centre + half,
                               rtol=0, atol=0.002)


def test_probe_vertex_buffer_contains_both_extreme_faces():
    np.testing.assert_allclose(_PROBE_VERTS.min(axis=0), [-0.5] * 3)
    np.testing.assert_allclose(_PROBE_VERTS.max(axis=0), [0.5] * 3)
