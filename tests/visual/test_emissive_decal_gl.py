"""Visual tier: an emissive decal is painted in its own colours, unlit.

A decal normally stains the surface it is on, so it is as bright as the light
falling there -- brighter in the middle of a nearby light, dark in a dark
room. An emissive one is a screen: every pixel of its face is the image's own
colour, the same in the light's hot spot, at its edge and in the dark.
"""

import io

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]

SIZE = 192
IMAGE = 'test_emissive_plate.png'
COLOUR = (220, 190, 30)


def _plate_png():
    from PIL import Image
    data = io.BytesIO()
    Image.new('RGBA', (64, 64), COLOUR + (255,)).save(data, format='PNG')
    return data.getvalue()


@pytest.fixture
def renderer(monkeypatch):
    from engine import decal_textures
    from engine.renderer.forward import decals as decal_pass
    png = _plate_png()
    real_source, real_read = decal_pass.asset_source, decal_textures.read_image
    monkeypatch.setattr(decal_textures, '_read', lambda: (IMAGE,))
    monkeypatch.setattr(
        decal_pass, 'asset_source',
        lambda path: io.BytesIO(png) if path.endswith(IMAGE) else real_source(path))
    monkeypatch.setattr(decal_textures, 'read_image',
                        lambda name: png if name == IMAGE else real_read(name))
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
    decal_textures.use_texts(())
    monkeypatch.undo()
    decal_textures.refresh()


def _plate(**props):
    from editor.things import Decal
    base = dict(texture=IMAGE, width=200.0, height=200.0, depth=64.0,
                yaw=90.0, pitch=0.0, name='plate')
    base.update(props)
    return Decal(pos=[0.0, 100.0, -200.0], properties=base)    # on the wall, facing +Z


def _frame(renderer, context, decal, intensity):
    """The wall, lit by a small light close to it (a hot spot in the middle,
    falling off towards the plate's edges), with *decal* on it."""
    import glm
    from editor.things import Light
    from engine.decal_table import DecalTable
    from tests.helpers.worlds import box_brush, make_thing
    wall = box_brush('wall', (0, 100, -216), (900, 900, 32))
    lamp = make_thing(Light, 'lamp', (0.0, 100.0, -150.0), radius=260.0,
                      intensity=intensity, state='on', casts_shadows=False)
    table = DecalTable()
    table.place([decal] if decal is not None else [], show_selection=False, epoch=1)
    projection = glm.perspective(glm.radians(60.0), 1.0, 1.0, 4000.0)
    eye = glm.vec3(0.0, 100.0, 100.0)
    view = glm.lookAt(eye, glm.vec3(0.0, 100.0, -200.0), glm.vec3(0, 1, 0))
    config = glh.render_config(all_brushes=[wall], all_things=[lamp], all_lights=[lamp],
                               play_mode=True, decals=table.frame(0.0, 1.0, 1.0))
    context.bind()
    with glh.no_gl_errors("emissive decal"):
        renderer.render_scene(projection, view, eye, None, config,
                              brush_slots=config["all_brush_slots"])
    return context.read_pixels().astype(np.int32)


def _face(image):
    """The middle of the plate and a patch near its corner, well inside it."""
    middle = image[SIZE // 2 - 4:SIZE // 2 + 4, SIZE // 2 - 4:SIZE // 2 + 4]
    corner = image[SIZE // 2 - 40:SIZE // 2 - 32, SIZE // 2 - 40:SIZE // 2 - 32]
    return middle.reshape(-1, 3).mean(axis=0), corner.reshape(-1, 3).mean(axis=0)


def test_the_whole_face_of_an_emissive_decal_is_equally_bright(renderer):
    made, context = renderer
    stained = _face(_frame(made, context, _plate(), intensity=3.0))
    glowing = _face(_frame(made, context, _plate(emissive=True), intensity=3.0))

    # The light's hot spot shows through an ordinary decal...
    assert stained[0].sum() > stained[1].sum() * 1.3, (
        "the test's light has no hot spot to rule out", stained)
    # ...and not through an emissive one: the same colour edge to middle.
    assert np.abs(glowing[0] - glowing[1]).max() <= 6, (
        "an emissive decal is brighter in the middle than at its edge", glowing)
    assert np.abs(glowing[0] - COLOUR).max() <= 12, (
        "an emissive decal is not painted in its image's own colour", glowing[0])


def test_an_emissive_decal_is_as_bright_in_the_dark(renderer):
    made, context = renderer
    dark_stained = _face(_frame(made, context, _plate(), intensity=0.0))[0]
    dark_glowing = _face(_frame(made, context, _plate(emissive=True), intensity=0.0))[0]

    assert dark_stained.max() < COLOUR[0] * 0.5, (
        "the room is not dark enough to tell", dark_stained)
    assert np.abs(dark_glowing - COLOUR).max() <= 12, (
        "an emissive decal dims with the room", dark_glowing)


def test_the_tint_colours_an_emissive_decal(renderer):
    made, context = renderer
    tinted = _face(_frame(made, context, _plate(emissive=True, tint=[1.0, 0.0, 0.0]),
                          intensity=0.0))[0]

    assert np.abs(tinted - (COLOUR[0], 0, 0)).max() <= 12, tinted


# ---------------------------------------------------------------------------
# Sharpness: an emissive decal shows its image at the image's own resolution
# ---------------------------------------------------------------------------

SHARP_IMAGE = 'test_emissive_sharp.png'
SHARP_SIDE = 1024


def _edge_png():
    """A 1024-pixel image, black on its left half and white on its right:
    one hard edge down the middle."""
    from PIL import Image
    pixels = np.zeros((SHARP_SIDE, SHARP_SIDE, 4), dtype=np.uint8)
    pixels[..., 3] = 255
    pixels[:, SHARP_SIDE // 2:, :3] = 255
    data = io.BytesIO()
    Image.fromarray(pixels).save(data, format='PNG')
    return data.getvalue()


@pytest.fixture
def sharp_renderer(monkeypatch):
    from engine import decal_textures
    from engine.renderer.forward import decals as decal_pass
    png = _edge_png()
    real_source, real_read = decal_pass.asset_source, decal_textures.read_image
    monkeypatch.setattr(decal_textures, '_read', lambda: (SHARP_IMAGE,))
    monkeypatch.setattr(
        decal_pass, 'asset_source',
        lambda path: io.BytesIO(png) if path.endswith(SHARP_IMAGE) else real_source(path))
    monkeypatch.setattr(decal_textures, 'read_image',
                        lambda name: png if name == SHARP_IMAGE else real_read(name))
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
    decal_textures.use_texts(())
    monkeypatch.undo()
    decal_textures.refresh()


def _blurred_columns(image):
    """How many pixels across the edge are neither black nor white, on
    average over the middle rows: the width of the blur."""
    rows = image[SIZE // 2 - 20:SIZE // 2 + 20].mean(axis=2)
    between = (rows > 255 * 0.12) & (rows < 255 * 0.88)
    return between.sum(axis=1).mean()


def test_an_emissive_decal_is_as_sharp_as_its_image(sharp_renderer):
    """The bug: 'decals become fuzzy when emissive is enabled'. An emissive
    decal is the image itself, unlit and unmodulated, so it shows the
    texture's resolution as it is -- and every decal layer is the image
    squeezed to 256 pixels. Magnified across a screen-sized decal, that is a
    blur several pixels wide. An emissive decal is drawn from its image at
    full resolution: the edge stays about a pixel wide."""
    made, context = sharp_renderer
    # Near the wall: 256 texels across the plate would be ~8 pixels a texel.
    decal = _plate(texture=SHARP_IMAGE, emissive=True, width=600.0, height=600.0)
    image = _frame_close(made, context, decal)
    middle = image[SIZE // 2 - 20:SIZE // 2 + 20].mean(axis=2)
    assert middle[:, :SIZE // 4].max() < 40 and middle[:, -SIZE // 4:].min() > 215, (
        "the plate does not fill the view black to white")
    assert _blurred_columns(image) <= 3.0, (
        "an emissive decal's edge is blurred across", _blurred_columns(image))


def _frame_close(renderer, context, decal):
    import glm
    from engine.decal_table import DecalTable
    from tests.helpers.worlds import box_brush
    wall = box_brush('wall', (0, 100, -216), (1400, 1400, 32))
    table = DecalTable()
    table.place([decal], show_selection=False, epoch=1)
    projection = glm.perspective(glm.radians(60.0), 1.0, 1.0, 4000.0)
    eye = glm.vec3(0.0, 100.0, -150.0)
    view = glm.lookAt(eye, glm.vec3(0.0, 100.0, -200.0), glm.vec3(0, 1, 0))
    config = glh.render_config(all_brushes=[wall], all_things=[], all_lights=[],
                               play_mode=True, decals=table.frame(0.0, 1.0, 1.0))
    context.bind()
    with glh.no_gl_errors("emissive decal"):
        renderer.render_scene(projection, view, eye, None, config,
                              brush_slots=config["all_brush_slots"])
    return context.read_pixels().astype(np.int32)
