"""Visual tier: terrain grass grows from the ground and leaks no GL state.

Two bugs showed up together when grass was switched on:

* the grass vertex shader built each blade position as ``vec3(xz, height)``,
  putting a tuft's world z in its height -- tufts floated anywhere from below
  the ground to high in the sky as green specks;
* the grass pass ended by force-enabling face culling. The terrain pass runs
  with culling off, and the forced state leaked into Qt's overlay painter,
  which then culled the SysMon panel's filled rectangles.
"""

import ctypes

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(320, 240) as ctx:
        yield ctx
    glh.reset_texture_cache()


def test_every_blade_vertex_stands_on_its_tuft(context):
    """Transform-feedback the grass shader for a few hand-made tufts."""
    import glm
    import OpenGL.GL as gl
    from OpenGL.GL.shaders import compileShader
    from engine import shaders

    vs = compileShader(shaders.DEFAULT_SHADERS['grass.vert'], gl.GL_VERTEX_SHADER)
    program = gl.glCreateProgram()
    gl.glAttachShader(program, vs)
    names = (ctypes.c_char_p * 1)(b"FragPos")
    gl.glTransformFeedbackVaryings(
        program, 1, ctypes.cast(names, ctypes.POINTER(ctypes.POINTER(ctypes.c_char))),
        gl.GL_INTERLEAVED_ATTRIBS)
    gl.glLinkProgram(program)
    assert gl.glGetProgramiv(program, gl.GL_LINK_STATUS), gl.glGetProgramInfoLog(program)
    gl.glUseProgram(program)
    identity = glm.mat4(1.0)
    for name in ("projection", "view"):
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(program, name), 1,
                              gl.GL_FALSE, glm.value_ptr(identity))
    gl.glUniform1f(gl.glGetUniformLocation(program, "time"), 3.7)
    gl.glUniform1f(gl.glGetUniformLocation(program, "windStrength"), 0.65)

    # x, y (ground height), z, size, phase, variation -- as Terrain uploads.
    tufts = np.array([
        [120.0, 55.0, -480.0, 1.0, 0.3, 0.9],
        [-900.0, 140.0, 700.0, 1.25, 5.1, 1.1],
        [3000.0, -20.0, 25.0, 0.65, 2.2, 0.82],
    ], dtype=np.float32)
    vao = gl.glGenVertexArrays(1)
    vbo = gl.glGenBuffers(1)
    gl.glBindVertexArray(vao)
    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
    gl.glBufferData(gl.GL_ARRAY_BUFFER, tufts.nbytes, tufts, gl.GL_STATIC_DRAW)
    stride = 6 * 4
    for loc, size, offset in ((2, 3, 0), (3, 1, 12), (4, 1, 16), (5, 1, 20)):
        gl.glVertexAttribPointer(loc, size, gl.GL_FLOAT, gl.GL_FALSE, stride,
                                 ctypes.c_void_p(offset))
        gl.glEnableVertexAttribArray(loc)
        gl.glVertexAttribDivisor(loc, 1)
    n = 12 * len(tufts)
    out = gl.glGenBuffers(1)
    gl.glBindBuffer(gl.GL_TRANSFORM_FEEDBACK_BUFFER, out)
    gl.glBufferData(gl.GL_TRANSFORM_FEEDBACK_BUFFER, n * 12, None, gl.GL_STATIC_READ)
    gl.glBindBufferBase(gl.GL_TRANSFORM_FEEDBACK_BUFFER, 0, out)
    gl.glEnable(gl.GL_RASTERIZER_DISCARD)
    gl.glBeginTransformFeedback(gl.GL_TRIANGLES)
    gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 12, len(tufts))
    gl.glEndTransformFeedback()
    gl.glDisable(gl.GL_RASTERIZER_DISCARD)
    pos = np.frombuffer(gl.glGetBufferSubData(gl.GL_TRANSFORM_FEEDBACK_BUFFER, 0, n * 12),
                        dtype=np.float32).reshape(len(tufts), 12, 3)
    gl.glBindVertexArray(0)

    for tuft, verts in zip(tufts, pos):
        x, y, z, size = tuft[:4]
        tallest = size * (2.0 + 0.55 * 1.12)          # iVariation <= 1.12
        assert (verts[:, 1] >= y).all() and (verts[:, 1] <= y + 0.02 + tallest).all(), \
            f"a blade of the tuft at ground {y} reaches {verts[:, 1].min()}..{verts[:, 1].max()}"
        reach = np.hypot(verts[:, 0] - x, verts[:, 2] - z).max()
        assert reach < 3.0 * size, f"a blade strays {reach:.1f} units from its tuft"


def terrain_with_grass():
    import json
    import os
    from engine.terrain import Terrain
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    t = Terrain()
    with open(os.path.join(root, "maps", "BigWorld_streaming_test.json")) as f:
        t.from_dict(json.load(f)["terrain_data"])
    t.set_grass(True, 0.02)
    return t


def test_a_grassy_frame_leaves_face_culling_as_it_found_it(context):
    import OpenGL.GL as gl
    from tests.visual.test_terrain_textures import player_view, render
    t = terrain_with_grass()
    eye, target = player_view(t)
    image = render(context, t, eye, target)
    assert not gl.glIsEnabled(gl.GL_CULL_FACE), \
        "the grass pass left face culling on; Qt's overlay painter culls with it"
    # And no grass in the sky: tufts grow from the ground.
    sky = image[:70].reshape(-1, 3)
    green = (sky[:, 1] > sky[:, 0] + 20) & (sky[:, 1] > sky[:, 2] + 10)
    assert green.mean() < 0.002, f"{green.mean():.2%} of the sky is grass"
