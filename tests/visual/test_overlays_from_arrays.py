"""The face highlight, the I/O links and the collision wireframes draw from
the arrays the host builds (engine/overlay_geometry.py), with a real context."""

import glm
import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl]

SIZE = 96


@pytest.fixture
def scene():
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as ctx:
        renderer = glh.make_renderer()
        projection = glm.perspective(glm.radians(60.0), 1.0, 1.0, 2000.0)
        view = glm.lookAt(glm.vec3(0, 0, 300), glm.vec3(0, 0, 0), glm.vec3(0, 1, 0))
        yield ctx, renderer, projection, view
        renderer.cleanup()
    glh.reset_texture_cache()


def _drawn(ctx, draw):
    import OpenGL.GL as gl
    ctx.bind()
    gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
    gl.glDisable(gl.GL_DEPTH_TEST)
    draw()
    gl.glFinish()
    assert glh.drain_gl_errors() == []
    return ctx.read_pixels()[:, :, :3].astype(int)


def test_a_face_highlight_draws_its_triangles(scene):
    from engine.overlay_geometry import face_highlight_triangles
    ctx, renderer, projection, view = scene
    brush = {'pos': [0.0, 0.0, 0.0], 'size': [120.0, 120.0, 120.0]}
    triangles = face_highlight_triangles(brush, 'north')      # faces the camera
    image = _drawn(ctx, lambda: renderer.draw_face_highlight(projection, view, triangles))
    centre = image[SIZE // 2 - 8:SIZE // 2 + 8, SIZE // 2 - 8:SIZE // 2 + 8]
    assert centre.sum() > 0, "the highlighted face drew nothing"
    assert image[2, 2].sum() == 0                             # and only the face
    blank = _drawn(ctx, lambda: renderer.draw_face_highlight(projection, view, None))
    assert blank.sum() == 0


def test_connection_lines_draw_in_their_colours(scene):
    from engine.overlay_geometry import connection_segments
    ctx, renderer, projection, view = scene
    segments, colours = connection_segments([
        {'src': [-100.0, 40.0, 0.0], 'dst': [100.0, 40.0, 0.0], 'color': (1.0, 0.0, 0.0)},
        {'src': [-100.0, -40.0, 0.0], 'dst': [100.0, -40.0, 0.0], 'color': (0.0, 1.0, 0.0)},
    ])
    image = _drawn(ctx, lambda: renderer.draw_connection_lines(
        projection, view, segments, colours))
    red = (image[:, :, 0] > 200) & (image[:, :, 1] < 50)
    green = (image[:, :, 1] > 200) & (image[:, :, 0] < 50)
    assert red.any() and green.any()
    # Rows on screen: the red link is the upper one in the world.
    assert np.nonzero(red)[0].mean() != np.nonzero(green)[0].mean()


def test_collision_wireframes_draw_their_runs(scene):
    from engine.overlay_geometry import collision_wireframes
    ctx, renderer, projection, view = scene
    tri = (((-90.0, -90.0, 0.0), (-30.0, -90.0, 0.0), (-60.0, -30.0, 0.0)), (0, 0, 1))
    vertices, runs = collision_wireframes([
        {'_model_collision': True, '_collision_mode': 'aabb',
         'pos': [50.0, 50.0, 0.0], 'size': [60.0, 60.0, 60.0]},
        {'_model_collision': True, '_collision_mode': 'mesh', '_mesh_triangles': [tri]},
    ])
    image = _drawn(ctx, lambda: renderer.draw_collision_visualization(
        projection, view, vertices, runs))
    yellow = (image[:, :, 0] > 200) & (image[:, :, 1] > 200) & (image[:, :, 2] < 50)
    cyan = (image[:, :, 1] > 200) & (image[:, :, 2] > 200) & (image[:, :, 0] < 50)
    assert yellow.any() and cyan.any()
    # Many frames reuse one buffer: no GL object is made per draw.
    import OpenGL.GL as gl
    before = renderer._conn_line_vbo
    for _ in range(5):
        _drawn(ctx, lambda: renderer.draw_collision_visualization(
            projection, view, vertices, runs))
    assert renderer._conn_line_vbo == before and gl.glIsBuffer(before)
