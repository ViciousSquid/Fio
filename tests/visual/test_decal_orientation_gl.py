"""Visual tier: a placed decal reads the right way round, whichever way it faces.

Seen from where it was thrown -- looking along the throw, the box's up at
the top of the screen -- an image's left must be on the viewer's left. A
decal of text used to read backwards: the box's x axis ran to the viewer's
left, and the image was drawn along it. Checked with an image whose left
half is red and right half blue, thrown at two walls, the floor and the
ceiling, and with ``mirrored`` (flip left to right) on.
"""

import io
import math

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]

SIZE = 128
IMAGE = 'test_left_red_right_blue.png'


def _image_png():
    from PIL import Image
    pixels = np.zeros((64, 64, 4), dtype=np.uint8)
    pixels[:, :32] = (255, 0, 0, 255)          # left half red
    pixels[:, 32:] = (0, 0, 255, 255)          # right half blue
    data = io.BytesIO()
    Image.fromarray(pixels).save(data, format='PNG')
    return data.getvalue()


@pytest.fixture
def renderer(monkeypatch):
    from engine import decal_textures
    from engine.renderer.forward import decals as decal_pass

    png = _image_png()
    version = decal_textures.images()[0] + 1000
    monkeypatch.setattr(decal_textures, 'images', lambda: (version, (IMAGE,)))
    real_source = decal_pass.asset_source
    monkeypatch.setattr(decal_pass, 'asset_source',
                        lambda path: io.BytesIO(png) if path.endswith(IMAGE)
                        else real_source(path))
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as context:
        made = glh.make_renderer()
        try:
            yield made, context
        finally:
            made.cleanup()
    glh.reset_texture_cache()


#: (surface brush pos, size), the decal's centre on it, (yaw, pitch) of the throw.
SURFACES = {
    'wall facing +Z': (((0, 100, -216), (800, 800, 32)), (0.0, 100.0, -200.0), (90.0, 0.0)),
    'wall facing -X': (((216, 100, 0), (32, 800, 800)), (200.0, 100.0, 0.0), (0.0, 0.0)),
    'floor': (((0, -16, 0), (800, 32, 800)), (0.0, 0.0, 0.0), (0.0, -90.0)),
    'ceiling': (((0, 216, 0), (800, 32, 800)), (0.0, 200.0, 0.0), (0.0, 90.0)),
}


def _frame(renderer, context, surface, mirrored):
    import glm
    from engine.decal_table import placed_row
    from tests.helpers.worlds import box_brush

    (brush_pos, brush_size), centre, (yaw, pitch) = SURFACES[surface]
    brushes = [box_brush('surface', brush_pos, brush_size)]
    model, params = placed_row(centre, dict(width=160.0, height=160.0, depth=64.0,
                                            yaw=yaw, pitch=pitch, mirrored=mirrored), 0)
    box = np.asarray(model, dtype=np.float64).reshape(4, 4)     # rows are columns
    up = box[1, :3] / np.linalg.norm(box[1, :3])
    forward = box[2, :3] / np.linalg.norm(box[2, :3])
    eye = np.asarray(centre, dtype=np.float64) - forward * 300.0
    projection = glm.perspective(glm.radians(60.0), 1.0, 1.0, 4000.0)
    view = glm.lookAt(glm.vec3(*eye), glm.vec3(*centre), glm.vec3(*up))
    lamp = _lamp(eye)
    config = glh.render_config(all_brushes=brushes, all_things=[lamp], all_lights=[lamp],
                               play_mode=True,
                               decals=(np.asarray([model], dtype=np.float32),
                                       np.asarray([params], dtype=np.float32)))
    context.bind()
    with glh.no_gl_errors("decal orientation"):
        renderer.render_scene(projection, view, glm.vec3(*eye), None, config,
                              brush_slots=config["all_brush_slots"])
    return context.read_pixels().astype(np.int32)


def _lamp(eye):
    from editor.things import Light
    from tests.helpers.worlds import make_thing
    return make_thing(Light, 'lamp', tuple(float(v) for v in eye), radius=4000.0,
                      intensity=2.0, state='on', casts_shadows=False)


def _redness(image, columns):
    region = image[SIZE // 2 - 10: SIZE // 2 + 10, columns]
    return float((region[..., 0] - region[..., 2]).mean())


@pytest.mark.parametrize("surface", sorted(SURFACES))
@pytest.mark.parametrize("mirrored", [False, True])
def test_a_decal_reads_left_to_right_from_where_it_was_thrown(renderer, surface, mirrored):
    made, context = renderer
    image = _frame(made, context, surface, mirrored)
    left = _redness(image, slice(SIZE // 2 - 30, SIZE // 2 - 10))
    right = _redness(image, slice(SIZE // 2 + 10, SIZE // 2 + 30))
    if mirrored:                      # flipped left to right, as asked
        left, right = right, left
    assert left > 40.0 and right < -40.0, (surface, mirrored, left, right)
