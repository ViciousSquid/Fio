"""Scene capture keeps texture state/storage while still copying each frame."""

import numpy as np
import pytest

pytest.importorskip('PyQt5')
pytest.importorskip('OpenGL')

from tests.helpers import gl as glh

pytestmark = pytest.mark.gl


def test_scene_capture_initializes_sampler_once_and_survives_resize(monkeypatch):
    import OpenGL.GL as gl
    glh.reset_texture_cache()
    with glh.GLTestContext(32, 24) as context:
        renderer = glh.make_renderer()
        parameters, allocations = [], []
        real_parameter = gl.glTexParameteri
        real_image = gl.glTexImage2D
        def parameter(*args):
            parameters.append(args)
            return real_parameter(*args)
        def image(*args):
            allocations.append(args)
            return real_image(*args)
        monkeypatch.setattr(gl, 'glTexParameteri', parameter)
        monkeypatch.setattr(gl, 'glTexImage2D', image)
        try:
            for viewport, color in (
                ((0, 0, 32, 24), (1., 0., 0., 1.)),
                ((0, 0, 32, 24), (0., 1., 0., 1.)),
                ((7, 3, 16, 12), (0., 0., 1., 1.)),
            ):
                context.bind()
                gl.glViewport(*viewport)
                gl.glClearColor(*color)
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)
                assert renderer._capture_glass_scene() == viewport[2:]
                captured = gl.glGetTexImage(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA,
                                            gl.GL_UNSIGNED_BYTE)
                pixels = np.frombuffer(captured, dtype=np.uint8).reshape(-1, 4)
                expected = np.array(color, dtype=np.uint8) * 255
                assert np.all(pixels == expected), 'scene capture reused stale pixels'
                assert len(parameters) == 4
                assert gl.glGetError() == gl.GL_NO_ERROR
            assert len(allocations) == 2
        finally:
            renderer.cleanup()
            glh.reset_texture_cache()
