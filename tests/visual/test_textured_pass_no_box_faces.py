"""Visual tier: a textured pass with no box face to draw draws nothing.

Brushes reach the textured pass but every box face is filtered out (all
caulk, or all nodraw in play), and no brush is angled. The per-face fallback
used to run anyway with no transforms gathered for it and crashed every
frame (``uniform_rows('mat4', None)``: "'NoneType' object has no attribute
'dtype'" from ``draw_textured_brushes_optimized``).
"""

import pytest

from engine.render_table import TEX_SKIP
from tests.helpers import gl as glh
from tests.helpers.worlds import box_brush
from engine import brush_geometry as bg

pytestmark = [pytest.mark.gl]


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(64, 64) as ctx:
        yield ctx
    glh.reset_texture_cache()


@pytest.mark.parametrize('play_mode', [False, True])
def test_all_caulk_brushes_render_without_error(context, play_mode):
    import OpenGL.GL as gl
    brush = box_brush('sealed', (0, 0, 0), (128, 128, 128))
    brush['textures'] = {tag: TEX_SKIP for tag in bg.FACE_TAGS}
    renderer = glh.make_renderer()
    projection, view, eye = glh.camera_matrices()
    cfg = glh.render_config(all_brushes=[brush], all_things=[],
                            play_mode=play_mode)
    context.bind()
    for _frame in range(2):
        renderer.render_scene(projection, view, eye, None, cfg,
                              brush_slots=cfg['all_brush_slots'])
    gl.glFinish()
    glh.no_gl_errors('all-caulk textured pass')
