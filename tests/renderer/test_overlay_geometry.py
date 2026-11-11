"""The overlays' geometry is built by the host, from the authored objects,
and handed to the renderer as arrays (engine/overlay_geometry.py)."""

import numpy as np
import pytest

from engine import brush_geometry
from engine.overlay_geometry import (
    AABB_COLOUR, HIGHLIGHT_BIAS, MESH_COLOUR, collision_wireframes,
    connection_segments, face_highlight_triangles)

BOX = {'pos': [10.0, 20.0, 30.0], 'size': [8.0, 4.0, 2.0]}
FACES = {'north': (2, +1), 'south': (2, -1), 'east': (0, +1),
         'west': (0, -1), 'top': (1, +1), 'down': (1, -1)}


@pytest.mark.parametrize("face", sorted(FACES))
def test_a_box_face_is_two_triangles_just_off_its_side(face):
    axis, sign = FACES[face]
    triangles = face_highlight_triangles(BOX, face)
    assert triangles.shape == (6, 3) and triangles.dtype == np.float32
    plane = BOX['pos'][axis] + sign * (BOX['size'][axis] / 2 + HIGHLIGHT_BIAS)
    assert np.allclose(triangles[:, axis], plane)
    for other in {0, 1, 2} - {axis}:
        lo = BOX['pos'][other] - BOX['size'][other] / 2
        hi = BOX['pos'][other] + BOX['size'][other] / 2
        assert triangles[:, other].min() == lo and triangles[:, other].max() == hi


def test_an_unknown_face_gives_nothing():
    assert face_highlight_triangles(BOX, 'sideways') is None


def test_building_a_highlight_leaves_a_box_brush_untouched():
    brush = {'pos': [0.0, 0.0, 0.0], 'size': [64.0, 64.0, 64.0]}
    before = dict(brush)
    face_highlight_triangles(brush, 'top')
    assert brush == before


def test_a_cut_face_is_a_fan_over_its_polygon():
    from tests.helpers.worlds import angled_brush
    brush = angled_brush()
    convex = brush_geometry.get_convex(brush)
    face = convex.faces[0]
    triangles = face_highlight_triangles(brush, brush_geometry.face_key(face))
    assert triangles.shape == (3 * (len(face['indices']) - 2), 3)
    normal = np.asarray(face['normal'], dtype=np.float64)
    ring = convex.verts[face['indices']]
    # Every vertex lies in the face's plane, HIGHLIGHT_BIAS off the surface.
    offsets = (triangles.astype(np.float64) - ring[0]) @ normal
    assert np.allclose(offsets, HIGHLIGHT_BIAS, atol=1e-3)


def test_connections_are_read_into_arrays():
    src, dst = [1.0, 2.0, 3.0], [4.0, 5.0, 6.0]
    connections = [{'src': src, 'dst': dst, 'color': (1.0, 0.0, 0.5)},
                   {'src': dst, 'dst': src}]
    segments, colours = connection_segments(connections)
    assert segments.shape == (2, 2, 3) and segments.dtype == np.float32
    assert segments[0].tolist() == [src, dst] and segments[1].tolist() == [dst, src]
    assert colours.tolist() == [[1.0, 0.0, 0.5], [0.0, 1.0, 1.0]]
    src[0] = 99.0                               # a copy, not a view
    assert segments[0, 0, 0] == 1.0
    empty, none = connection_segments([])
    assert empty.shape == (0, 2, 3) and none.shape == (0, 3)


def test_collision_records_become_line_runs_in_order():
    tri = (((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)), (0.0, 0.0, 1.0))
    records = [
        {'_model_collision': True, '_collision_mode': 'aabb',
         'pos': [10.0, 0.0, 0.0], 'size': [2.0, 4.0, 6.0]},
        {'pos': [0.0, 0.0, 0.0], 'size': [1.0, 1.0, 1.0]},        # a map brush
        {'_model_collision': True, '_collision_mode': 'mesh', '_mesh_triangles': [tri, tri]},
    ]
    vertices, runs = collision_wireframes(records)
    assert runs == ((0, 24, AABB_COLOUR), (24, 12, MESH_COLOUR))
    assert vertices.shape == (36, 3) and vertices.dtype == np.float32
    box = vertices[:24]
    assert box.min(axis=0).tolist() == [9.0, -2.0, -3.0]
    assert box.max(axis=0).tolist() == [11.0, 2.0, 3.0]
    # Each triangle as its three edges: 0-1, 1-2, 2-0.
    assert vertices[24:30].tolist() == [[0, 0, 0], [1, 0, 0], [1, 0, 0],
                                        [0, 1, 0], [0, 1, 0], [0, 0, 0]]
    nothing, no_runs = collision_wireframes([records[1]])
    assert len(nothing) == 0 and no_runs == ()
