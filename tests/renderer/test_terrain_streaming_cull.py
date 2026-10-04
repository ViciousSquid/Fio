"""Terrain streaming radius and chunk culling through real Terrain machinery.

The old tests avoided Terrain.__init__ because it allocates GL state. That made
the tests prove only that a hand-built object still satisfied the maths. The
tests now construct the real Terrain inside the same offscreen core-profile
context used by the renderer tier.
"""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

glm = pytest.importorskip("glm")
pytest.importorskip("OpenGL")

from engine.renderer_core import BaseRenderer  # noqa: E402
from engine.terrain import Terrain  # noqa: E402
from tests.helpers.gl import GLTestContext  # noqa: E402

pytestmark = pytest.mark.gl


@pytest.fixture
def terrain():
    with GLTestContext(64, 64):
        value = Terrain()
        try:
            yield value
        finally:
            value.cleanup()


def configure(terrain, stream_radius=2048.0, streaming=True):
    terrain.streaming = streaming
    terrain.stream_radius = stream_radius
    terrain.stream_evict_padding = 512.0
    terrain.set_bounds(
        -100000,
        100000,
        -100000,
        100000,
        prune=False,
    )
    return terrain


def frustum(eye, target, far):
    projection = glm.perspective(glm.radians(70.0), 16 / 9, 1.0, far)
    view = glm.lookAt(glm.vec3(*eye), glm.vec3(*target), glm.vec3(0, 1, 0))
    return BaseRenderer._frustum_planes(projection * view)


def test_the_protected_zone_never_outgrows_the_stream_radius(terrain):
    terrain = configure(terrain, 2048.0)
    assert terrain._near_detail_radius() == 2048.0

    terrain = configure(terrain, 9000.0)
    assert terrain._near_detail_radius() == Terrain.NEAR_DETAIL_RADIUS

    terrain = configure(terrain, 2048.0, streaming=False)
    assert terrain._near_detail_radius() == Terrain.NEAR_DETAIL_RADIUS


def visible(terrain, planes, *coords):
    slots = [
        terrain.table.ensure(
            cx, cz, terrain.chunk_size, terrain.offset_x, terrain.offset_z
        )
        for cx, cz in coords
    ]
    return list(terrain.table.visible(slots, planes))


def test_the_streamed_ring_is_sized_by_the_stream_radius(terrain):
    terrain = configure(terrain, 2048.0)
    terrain._stream_chunks(glm.vec3(0.0, 0.0, 0.0))
    reach = 2048.0 + terrain.chunk_size
    upper = math.pi * reach * reach / (terrain.chunk_size ** 2)
    assert 0 < terrain.table.count <= upper
    assert terrain.stream_radius == 2048.0


def test_a_chunk_behind_the_camera_is_culled(terrain):
    planes = frustum((0, 100, 0), (0, 100, -1000), 4000.0)
    configure(terrain)
    assert visible(terrain, planes, (0, -4), (0, 4)) == [True, False]


def test_a_chunk_past_the_view_distance_is_culled(terrain):
    planes = frustum((0, 100, 0), (0, 100, -1000), 1000.0)
    configure(terrain)
    assert visible(terrain, planes, (0, -2), (0, -12)) == [True, False]


def test_an_unbuilt_chunk_is_tested_at_its_own_footprint(terrain):
    """Unbuilt chunks use their own footprint for culling."""
    planes = frustum((5000, 100, 5000), (5000, 100, 4000), 4000.0)
    configure(terrain)
    slot = terrain.table.ensure(19, 17, terrain.chunk_size, 0.0, 0.0)
    assert not terrain.table.built[slot]
    assert visible(terrain, planes, (19, 17), (0, 0)) == [True, False]
