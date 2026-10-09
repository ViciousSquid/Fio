"""A GIF on a brush face is one texture whose pixels follow the play clock."""

import pytest

pytest.importorskip("PyQt5")
pytest.importorskip("PIL")

from tests.engine.test_animated_texture import COLOURS, write_gif  # noqa: E402
from tests.helpers.gl import GLTestContext                  # noqa: E402

pytestmark = pytest.mark.gl


def _texel(tex):
    import numpy as np
    import OpenGL.GL as gl
    gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
    gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 1)
    raw = gl.glGetTexImage(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
    return tuple(int(v) for v in np.frombuffer(raw, np.uint8)[:3])


def test_the_texture_shows_the_frame_due_and_keeps_its_name(tmp_path, monkeypatch):
    from PIL import Image
    from engine.renderer.forward import ForwardRenderer
    textures = tmp_path / "assets" / "textures"
    textures.mkdir(parents=True)
    write_gif(textures / "anim.gif")
    Image.new("RGB", (4, 4), (9, 9, 9)).save(textures / "still.png")
    monkeypatch.chdir(tmp_path)

    with GLTestContext():
        renderer = ForwardRenderer(None)
        try:
            tex = renderer.load_texture("anim.gif", "textures")
            still = renderer.load_texture("still.png", "textures")
            assert renderer._texture_pixel_size("anim.gif") == (16, 8)   # Fit reads this
            assert set(renderer._animated_textures()) == {int(tex)}
            assert _texel(tex) == COLOURS[0]                 # outside Play: first frame

            for clock, colour in ((0.06, COLOURS[1]), (0.2, COLOURS[2]),
                                  (0.23, COLOURS[0]), (0.0, COLOURS[0])):
                renderer.animate_textures(clock)
                assert _texel(tex) == colour, clock
            assert renderer.load_texture("anim.gif", "textures") == tex
            assert _texel(still) == (9, 9, 9)
        finally:
            renderer.cleanup()
        assert renderer._animated_textures() == {}
