"""Visual tier: water reflects the map's sky, and the hidden-wall skip.

With a sky panorama on the map, every water brush picks it up, high quality
and cheap alike -- built-in or custom. High-quality box water does not shade
wall pixels its opaque top surface covers; that must not change a single
pixel.
"""

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]

SIZE = 160
SKY = 'assets/backgrounds/void-panorama-q80.webp'


def pool(high_quality=True):
    from editor.things import Light
    from tests.helpers.worlds import box_brush, make_thing

    water = box_brush('water', (0, -130, 0), (5000, 260, 5000))
    water.update(shader='Water', is_water=True, water_tint=[0.05, 0.35, 0.45],
                 water_opacity=0.85, water_fresnel=1.0, water_reflectivity=1.0,
                 water_wave_height=0.25, water_wave_enabled=True,
                 water_high_quality=high_quality)
    brushes = [box_brush('floor', (0, -300, 0), (6000, 64, 6000)), water]
    light = make_thing(Light, 'sun', (0.0, 3000.0, 1500.0), color=[255, 255, 255],
                       intensity=1.0, radius=20000.0, state='on', casts_shadows=False)
    return brushes, [light]


@pytest.fixture
def context():
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as ctx:
        yield ctx
    glh.reset_texture_cache()


def render(context, brushes, things, sky='', eye=(-700.0, 160.0, 900.0),
           target=(100.0, -60.0, -300.0), reflect=True):
    """The frame; *reflect* False keeps the sky drawn but stops the water
    reflecting it, so a comparison isolates the reflection from the sky the
    water refracts."""
    import glm
    from engine.view_distance import ViewDistance

    renderer = glh.make_renderer()
    try:
        renderer.view_distance = ViewDistance(8000.0)
        renderer.set_background_image(sky)
        if not reflect:
            renderer._background_panorama_texture = lambda: 0
        projection = glm.perspective(glm.radians(65.0), 1.0, 1.0,
                                     renderer.view_distance.far_plane)
        eye_v = glm.vec3(*eye)
        view = glm.lookAt(eye_v, glm.vec3(*target), glm.vec3(0, 1, 0))
        config = glh.render_config(all_brushes=brushes, all_things=things,
                                   all_lights=things, play_mode=True, time=3.0)
        context.bind()
        with glh.no_gl_errors("reflect sky"):
            renderer.render_scene(projection, view, eye_v, None, config,
                                  brush_slots=config["all_brush_slots"])
        return context.read_pixels().astype(np.int32)
    finally:
        renderer.cleanup()


WATER = (slice(SIZE // 2, SIZE), slice(0, SIZE))     # the lower half is water


@pytest.mark.parametrize("high_quality", [True, False])
def test_a_sky_on_the_map_is_reflected(context, high_quality):
    unreflected = render(context, *pool(high_quality), sky=SKY, reflect=False)
    reflected = render(context, *pool(high_quality), sky=SKY)
    assert np.abs(reflected[WATER] - unreflected[WATER]).mean() > 2.0


def test_skipping_covered_walls_changes_no_pixel(context, monkeypatch):
    """A tall box seen from above (DevTest's pool): its walls lie under the
    top surface; drawn or skipped, the frame is the same."""
    from editor.things import Light
    from engine.renderer.forward import passes
    from tests.helpers.worlds import box_brush, make_thing

    water = box_brush('water', (0, 96, -360), (768, 320, 208))
    water.update(shader='Water', is_water=True)
    brushes = [box_brush('floor', (-80, -152, -16), (1184, 272, 896)), water]
    things = [make_thing(Light, 'l', (0.0, 176.0, -144.0), state='on', radius=600.0)]
    views = [((0.0, 350.0, -120.0), (0.0, 270.0, -420.0)),
             ((0.0, 350.0, -360.0), (0.0, 250.0, -362.0))]
    for eye, target in views:
        skipped = render(context, brushes, things, eye=eye, target=target)
        # Shade every wall: the camera, taken into the box's space, is put far
        # below it, where no wall can ever be covered by the top.
        monkeypatch.setattr(passes.np.linalg, 'solve',
                            lambda m, v: np.array((0.0, -1.0e6, 0.0, 1.0)))
        drawn = render(context, brushes, things, eye=eye, target=target)
        monkeypatch.undo()
        assert np.abs(skipped - drawn).max() <= 1, eye


@pytest.mark.parametrize("high_quality", [True, False])
def test_a_custom_sky_is_reflected_too(context, tmp_path, high_quality):
    """A sky the author picked from anywhere (an absolute path, as the editor
    accepts before importing it) is the texture the water reflects."""
    from PIL import Image

    custom = tmp_path / "red-panorama.png"
    Image.new("RGB", (64, 32), (255, 0, 0)).save(custom)
    plain = render(context, *pool(high_quality), sky=str(custom), reflect=False)
    red = render(context, *pool(high_quality), sky=str(custom))
    redder = ((red[WATER][..., 0] - red[WATER][..., 2])
              - (plain[WATER][..., 0] - plain[WATER][..., 2]))
    assert redder.mean() > 2.0
