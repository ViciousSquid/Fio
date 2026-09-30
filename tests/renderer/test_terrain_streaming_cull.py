"""Headless tests for the terrain's streaming radius and chunk culling.

Big World fills a map with terrain by widening the terrain's bounds and
switching it to streaming, trusting the stream radius it sets to bound what is
resident. The terrain used to raise that radius to its 4096-unit protected
zone plus a chunk every frame, and the renderer drew every resident chunk with
no frustum -- so a map asking for 2048 units got ~1000 full-detail chunks, all
of them drawn, and pulling the view distance in changed nothing.

Only the pure maths is exercised: a Terrain is built without ``__init__`` (that
needs a GL context) and no chunk is ever uploaded.
"""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

glm = pytest.importorskip("glm")
pytest.importorskip("OpenGL")

from engine.terrain import Terrain  # noqa: E402
from engine.terrain_table import TerrainTable  # noqa: E402


def bare_terrain(stream_radius=2048.0, streaming=True):
    t = object.__new__(Terrain)
    t.chunk_size = 256.0
    t.offset_x = t.offset_z = t.offset_y = 0.0
    t.min_chunk_x = t.min_chunk_z = -100000
    t.max_chunk_x = t.max_chunk_z = 100000
    t.table = TerrainTable()
    t.streaming = streaming
    t.stream_radius = stream_radius
    t.stream_evict_padding = 512.0
    return t


def frustum(eye, target, far):
    """The renderer's six planes for a camera at *eye* looking at *target*."""
    from engine.renderer_core import BaseRenderer
    projection = glm.perspective(glm.radians(70.0), 16 / 9, 1.0, far)
    view = glm.lookAt(glm.vec3(*eye), glm.vec3(*target), glm.vec3(0, 1, 0))
    return BaseRenderer._frustum_planes(projection * view)


def test_the_protected_zone_never_outgrows_the_stream_radius():
    assert bare_terrain(2048.0)._near_detail_radius() == 2048.0
    assert bare_terrain(9000.0)._near_detail_radius() == Terrain.NEAR_DETAIL_RADIUS
    assert (bare_terrain(2048.0, streaming=False)._near_detail_radius()
            == Terrain.NEAR_DETAIL_RADIUS)


def visible(t, planes, *coords):
    """Visibility of chunks ``(cx, cz)``, allocating them unbuilt first."""
    slots = [t.table.ensure(cx, cz, t.chunk_size, t.offset_x, t.offset_z)
             for cx, cz in coords]
    return list(t.table.visible(slots, planes))


def test_the_streamed_ring_is_sized_by_the_stream_radius():
    t = bare_terrain(2048.0)
    t._stream_chunks(glm.vec3(0.0, 0.0, 0.0))
    reach = 2048.0 + t.chunk_size
    upper = math.pi * reach * reach / (t.chunk_size ** 2)
    assert 0 < t.table.count <= upper
    assert t.stream_radius == 2048.0, "streaming must not inflate the radius"


def test_a_chunk_behind_the_camera_is_culled():
    planes = frustum((0, 100, 0), (0, 100, -1000), 4000.0)
    t = bare_terrain()
    # z in [-1024, -768] ahead, z in [1024, 1280] behind
    assert visible(t, planes, (0, -4), (0, 4)) == [True, False]


def test_a_chunk_past_the_view_distance_is_culled():
    planes = frustum((0, 100, 0), (0, 100, -1000), 1000.0)
    t = bare_terrain()
    assert visible(t, planes, (0, -2), (0, -12)) == [True, False]


def test_an_unbuilt_chunk_is_tested_at_its_own_footprint():
    """Before a chunk is built it has no measured centre; culling it at the
    origin would hide chunks in view and keep ones behind the camera."""
    planes = frustum((5000, 100, 5000), (5000, 100, 4000), 4000.0)
    t = bare_terrain()
    slot = t.table.ensure(19, 17, t.chunk_size, 0.0, 0.0)   # just ahead
    assert not t.table.built[slot]
    assert visible(t, planes, (19, 17), (0, 0)) == [True, False]
