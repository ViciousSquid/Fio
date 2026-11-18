"""Visual tier: a placed image decal paints from its image at the image's own
resolution -- each axis kept, a 512x64 banner its 512 across -- not from the
shared array's 256x256 layers (which stay for blood and wet masks)."""

import io

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]

SIZE = 192
BANNER = 'test_stripes_512x64.png'


def _stripes_png():
    from PIL import Image
    # One-pixel black and white columns: squeezed to 256 across, each texel
    # is their average -- mid grey, which an image decal leaves unchanged.
    row = np.tile(np.array([0, 255], dtype=np.uint8), 256)
    grey = np.tile(row, (64, 1))
    rgba = np.dstack([grey, grey, grey, np.full_like(grey, 255)])
    data = io.BytesIO()
    Image.fromarray(rgba, 'RGBA').save(data, format='PNG')
    return data.getvalue()


@pytest.fixture
def renderer(monkeypatch):
    from engine import decal_textures
    from engine.renderer.forward import decals as decal_pass
    png = _stripes_png()
    real_source, real_read = decal_pass.asset_source, decal_textures.read_image
    monkeypatch.setattr(decal_textures, '_read', lambda: (BANNER,))
    monkeypatch.setattr(decal_pass, 'asset_source',
                        lambda path: io.BytesIO(png) if path.endswith(BANNER)
                        else real_source(path))
    monkeypatch.setattr(decal_textures, 'read_image',
                        lambda name: png if name == BANNER else real_read(name))
    decal_textures.use_texts(())
    decal_textures.refresh()
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as context:
        made = glh.make_renderer()
        try:
            yield made, context
        finally:
            made.cleanup()
    glh.reset_texture_cache()
    monkeypatch.undo()
    decal_textures.refresh()


def _frame(renderer, context, decals):
    import glm
    from editor.things import Light
    from engine.decal_table import DecalTable
    from tests.helpers.worlds import box_brush, make_thing
    wall = box_brush('wall', (0, 100, -216), (3000, 900, 32))
    lamp = make_thing(Light, 'lamp', (0.0, 100.0, 100.0), radius=4000.0,
                      intensity=0.6, state='on', casts_shadows=False)
    table = DecalTable()
    table.place(list(decals), show_selection=False, epoch=1)
    published = table.frame(0.0, 1.0, 1.0)
    # The banner wide enough that a texel of it is about a pixel on screen.
    eye = glm.vec3(0.0, 100.0, 100.0)
    projection = glm.perspective(glm.radians(60.0), 1.0, 1.0, 4000.0)
    view = glm.lookAt(eye, glm.vec3(0.0, 100.0, -200.0), glm.vec3(0, 1, 0))
    config = glh.render_config(all_brushes=[wall], all_things=[lamp], all_lights=[lamp],
                               play_mode=True, decals=published)
    context.bind()
    with glh.no_gl_errors("image decal"):
        renderer.render_scene(projection, view, eye, None, config,
                              brush_slots=config["all_brush_slots"])
    return context.read_pixels().astype(np.int32)


def _banner(width):
    from editor.things import Decal
    return Decal(pos=[0.0, 100.0, -200.0], properties=dict(
        texture=BANNER, width=width, height=width / 8.0, depth=64.0,
        yaw=90.0, pitch=0.0, name='banner'))


def test_a_banner_keeps_its_full_width_of_detail(renderer):
    made, context = renderer
    plain = _frame(made, context, [])
    painted = _frame(made, context, [_banner(1024.0)])
    assert made._decal_image_side == 512                  # its 512 across, kept
    row = painted[SIZE // 2, SIZE // 4: 3 * SIZE // 4, 0]
    wall = plain[SIZE // 2, SIZE // 4: 3 * SIZE // 4, 0]
    # The stripes show: dark and light columns, not the mid grey (the wall
    # unchanged) that a 256-wide layer would have averaged them to.
    assert row.max() - row.min() > 80
    assert np.abs(row - wall).mean() > 20


def test_the_array_is_made_once_whatever_the_camera_sees(renderer, monkeypatch):
    from engine.renderer.forward import decals as decal_pass
    made, context = renderer
    builds = []
    real = decal_pass.gl.glTexImage3D
    monkeypatch.setattr(decal_pass.gl, 'glTexImage3D', lambda *a: builds.append(a[3]) or real(*a))
    for width in (1024.0, 1024.0, 1024.0):
        _frame(made, context, [_banner(width)])
    assert builds.count(512) == 1
