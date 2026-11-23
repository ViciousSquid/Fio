"""Visual tier: HD_High, the second renderer.

HD_High (``engine/renderer/hd_high``) is created through the registry like any
renderer and drawn with on a real GL context: every sky mode draws without a
GL error; an indoor map keeps the brightness Classic gives it; sky light
lands only where the sky is overhead; the host swaps Classic and HD_High live
and Classic's picture is unchanged by the round trip; and a texture's
material maps are found beside it, or made and kept on disk when it has none.
"""

import configparser
import os

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl]

PANORAMA = "assets/backgrounds/cloudy-sky-panorama.webp"


def _config():
    config = configparser.ConfigParser()
    config.add_section("Renderer")
    config.set("Renderer", "lowpower_mode", "False")
    return config


def _frame(context, renderer, brushes, things, frames=3, **camera):
    projection, view, eye = glh.camera_matrices(**camera)
    config = glh.render_config(all_brushes=brushes, all_things=things, play_mode=True)
    for _ in range(frames):
        context.bind()
        with glh.no_gl_errors("frame"):
            renderer.render_scene(projection, view, eye, None, config,
                                  brush_slots=config["all_brush_slots"])
    return context.read_pixels()


def test_hd_high_is_a_separate_renderer_that_satisfies_the_contract():
    from engine.renderer import (RENDERER_CONTRACT_VERSION, available_renderers,
                                 contract_compatible, missing_members, renderer_factory)
    from engine.renderer.forward import ForwardRenderer
    assert "HD_High" in available_renderers() and "Forward" in available_renderers()
    factory = renderer_factory("HD_High")
    assert factory.__module__.startswith("engine.renderer.hd_high")
    assert not issubclass(factory, ForwardRenderer)
    # Nothing in the package imports the classic renderer.
    package = os.path.dirname(__import__("engine.renderer.hd_high", fromlist=["x"]).__file__)
    for folder, _dirs, files in os.walk(package):
        for name in files:
            if name.endswith(".py"):
                with open(os.path.join(folder, name), encoding="utf-8") as handle:
                    text = handle.read()
                imports = [line for line in text.splitlines()
                           if line.lstrip().startswith(("import ", "from "))]
                assert not any("forward" in line for line in imports), (name, imports)
    glh.reset_texture_cache()
    with glh.GLTestContext(32, 32):
        renderer = glh.make_renderer(_config(), name="HD_High")
        try:
            assert renderer.ready and renderer.hd is True
            assert missing_members(renderer) == [] and contract_compatible(renderer) is None
            assert renderer.contract_version == RENDERER_CONTRACT_VERSION
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()


@pytest.mark.parametrize("sky, sun", [("none", False), ("panorama", False),
                                      ("panorama", True), ("procedural", True),
                                      ("procedural", False)])
def test_every_sky_mode_draws(sky, sun):
    from engine import hd_graphics
    glh.reset_texture_cache()
    with glh.GLTestContext(160, 120) as context:
        renderer = glh.make_renderer(_config(), name="HD_High")
        try:
            image = PANORAMA if sky == "panorama" else ""
            renderer.set_background_image(image)
            renderer.set_hd_sky(hd_graphics.sky_settings({"hd_sky": sky, "hd_sun": sun}, image))
            brushes, things = glh.lit_cube_scene()
            pixels = _frame(context, renderer, brushes, things, aspect=160 / 120)
            assert not glh.is_blank(pixels)
            assert renderer.environment.enabled and renderer.environment.sky_mode == sky
            assert bool(renderer._env_shadow_ready) == sun
            assert bool(renderer._env_cover_ready) == (sky != "none")
            # A sky was drawn above the horizon; indoors the clear colour.
            top = glh.luminance(glh.region(pixels, 0.0, 0.0, 1.0, 0.15)).mean()
            assert (top > 60.0) == (sky != "none"), (sky, top)
            low, high = hd_graphics.EXPOSURE_RANGE
            assert low <= renderer._env_base_exposure <= high
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()


def test_an_indoor_map_is_as_bright_as_classic_shows_it():
    """No sky on the PlayerStart: the map's own lamps, at classic brightness
    (not lifted to mid grey, not crushed)."""
    brushes, things = glh.lit_cube_scene()
    means = {}
    for name in ("Classic", "HD_High"):
        glh.reset_texture_cache()
        with glh.GLTestContext(160, 120) as context:
            renderer = glh.make_renderer(_config(), name=name)
            try:
                pixels = _frame(context, renderer, brushes, things, aspect=160 / 120)
                # The lit floor in front of the cube.
                means[name] = glh.luminance(glh.region(pixels, 0.2, 0.6, 0.8, 0.95)).mean()
            finally:
                renderer.cleanup()
    glh.reset_texture_cache()
    ratio = means["HD_High"] / max(means["Classic"], 1e-6)
    assert 0.6 < ratio < 1.6, means


def test_sky_light_lands_only_where_the_sky_is_overhead():
    """A floor half under a roof, under a sky with no lamps: the open half is
    lit by the sky, the roofed half is not -- and no light leaks across."""
    from engine import hd_graphics
    from tests.helpers.worlds import box_brush
    brushes = [box_brush("floor", (0, -16, 0), (4096, 32, 4096)),
               box_brush("roof", (-1024, 400, 0), (2048, 32, 4096))]
    glh.reset_texture_cache()
    with glh.GLTestContext(240, 120) as context:
        renderer = glh.make_renderer(_config(), name="HD_High")
        try:
            renderer.set_background_image(PANORAMA)
            renderer.set_hd_sky(hd_graphics.sky_settings({}, PANORAMA))
            # From the open side's edge, low, looking along the roof's edge:
            # the roofed floor is on the left, the open floor on the right.
            pixels = _frame(context, renderer, brushes, [], aspect=2.0, fov_deg=80.0,
                            eye=(0.0, 200.0, 1800.0), target=(0.0, 0.0, 0.0))
            if os.environ.get("FIO_TEST_IMAGES"):
                from PIL import Image
                Image.fromarray(pixels).save(os.path.join(os.environ["FIO_TEST_IMAGES"], "cover.png"))
            covered = glh.luminance(glh.region(pixels, 0.05, 0.75, 0.35, 0.95)).mean()
            open_sky = glh.luminance(glh.region(pixels, 0.65, 0.75, 0.95, 0.95)).mean()
            assert open_sky > 60.0, open_sky
            assert covered < open_sky * 0.35, (covered, open_sky)
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()


def test_exposure_follows_the_view_from_under_a_roof_to_under_the_sky():
    from engine import hd_graphics
    from engine.renderer.hd_high.environment import EnvironmentMixin
    from engine.render_table import RenderTable
    from tests.helpers.worlds import box_brush
    brushes = [box_brush("floor", (0, -16, 0), (2048, 32, 2048)),
               box_brush("roof", (-512, 400, 0), (1024, 32, 2048))]
    table = RenderTable()
    table.begin_frame(brushes, epoch=1)

    class Probe(EnvironmentMixin):
        def _kept_shadow_casters(self, table, slots):
            return np.arange(table.count)

    config = {"render_table": table, "all_brush_slots": np.arange(table.count)}
    probe = Probe()
    assert probe._env_sky_share(config, (-512.0, 100.0, 0.0)) == 0.0     # under the roof
    assert probe._env_sky_share(config, (700.0, 100.0, 0.0)) == 1.0      # in the open
    edge = probe._env_sky_share(config, (60.0, 100.0, 0.0))
    assert 0.0 < edge < 1.0
    assert probe._env_sky_share({"render_table": table, "all_brush_slots": np.arange(0)},
                                (0.0, 0.0, 0.0)) is None
    assert hd_graphics.classic_exposure() > 0.0


def test_the_host_swaps_classic_and_hd_high_live(fio_session):
    """Settings > Display > Renderer and ``r_renderer`` both come down to
    QtGameView.switch_renderer: there and back, in the editor and in Play,
    and Classic draws what it drew before the trip."""
    from editor.things import Light, PlayerStart
    from tests.helpers.worlds import box_brush, level_data, make_thing, room
    spawn = make_thing(PlayerStart, "spawn", (0.0, 40.0, 0.0), angle=0.0)
    lamp = make_thing(Light, "lamp", (0.0, 300.0, -200.0), radius=2000.0,
                      intensity=2.0, casts_shadows=True)
    crate = box_brush("crate", (0.0, 64.0, -300.0), (128.0, 128.0, 128.0))
    session = fio_session(level_data(brushes=room(size=2048.0, height=512.0) + [crate],
                                     things=[spawn, lamp]))
    session.view.sysmon.set_active(False)
    session.publish()
    session.paint()
    before = session.paint()
    assert session.view.renderer_mode == "Forward"
    assert session.view.switch_renderer("HD_High")
    assert type(session.view.renderer).__name__ == "HDHighRenderer"
    session.publish()
    session.paint()
    hd = session.paint()
    assert not glh.is_blank(hd)
    session.start_play()
    session.step(3)
    assert not glh.is_blank(session.paint())
    session.stop_play()
    # The setting, applied through the editor: back to Classic.
    session.window.config.set("Renderer", "renderer", "Classic")
    assert session.window.apply_renderer_setting()
    assert session.view.renderer_mode == "Forward"
    session.publish()
    session.paint()
    after = session.paint()
    differing = (np.abs(after.astype(np.int16) - before.astype(np.int16)).max(axis=2) > 2).mean()
    assert differing < 0.002, differing
    session.window.config.set("Renderer", "renderer", "HD_High")
    assert session.window.apply_renderer_setting()
    assert session.view.renderer_mode == "HD_High"


class _Maps:
    """EnvironmentMixin._material_for without a GL context: textures are
    numbers and loading one is recorded."""

    def __init__(self, names, monkeypatch):
        from engine.renderer.hd_high import environment
        self.loaded = []
        self.texture_manager = {name: index + 1 for index, name in enumerate(names)}
        monkeypatch.setattr(environment, "load_data_texture",
                            lambda path, *a, **k: (self.loaded.append(path) or len(self.loaded) + 100))
        monkeypatch.setattr(environment, "_set_anisotropy", lambda amount: None)
        monkeypatch.setattr(environment.gl, "glBindTexture", lambda *a: None)
        monkeypatch.setattr(environment.gl, "glGetFloatv", lambda *a: 0.0)
        monkeypatch.setattr(environment.gl, "glGetError", lambda: 0)

        class Renderer(environment.EnvironmentMixin):
            lowpower_mode = False

        Renderer._init_postfx = lambda self: None
        self.renderer = Renderer()
        self.renderer.texture_manager = self.texture_manager
        self.renderer._init_environment()

    def maps(self, name, wait=False):
        texture = self.texture_manager[name]
        found = self.renderer._material_for(texture)
        if wait:
            job = self.renderer.__dict__.get("_material_pending", {}).get(texture)
            if job is not None:
                job.result(timeout=60)
                found = self.renderer._material_for(texture)
        return found


def test_material_maps_are_found_beside_a_texture(monkeypatch):
    world = _Maps(["textures/cobblestone.jpg", "textures/Dev/512.jpg",
                   "textures/water_normal.png", "textures/nodraw.jpg",
                   "textures/terrain/rock.jpg"], monkeypatch)
    normal, material, parallax, emission = world.maps("textures/cobblestone.jpg")
    assert [os.path.basename(p) for p in world.loaded] == ["cobblestone_n.webp",
                                                           "cobblestone_m.webp"]
    assert normal and material and parallax >= 0.0 and emission > 0.0
    # Tool textures, and what is not a surface, have none -- on purpose, and
    # without looking for any or generating any.
    for name in ("textures/Dev/512.jpg", "textures/water_normal.png", "textures/nodraw.jpg"):
        assert world.maps(name) is None
    assert len(world.loaded) == 2 and world.renderer.material_stats["generated"] == 0
    assert world.maps("textures/terrain/rock.jpg") is not None
    # Asked once per texture.
    world.maps("textures/cobblestone.jpg")
    assert len(world.loaded) == 4


def test_a_texture_without_maps_gets_them_made_and_kept(monkeypatch, tmp_path):
    from PIL import Image
    from engine.asset_archives import asset_bytes
    cache = tmp_path / "cache"
    monkeypatch.setenv("FIO_MATERIAL_CACHE", str(cache))
    picture = tmp_path / "bricks.png"
    rng = np.random.default_rng(7)
    Image.fromarray(rng.integers(0, 255, (64, 64, 3), dtype=np.uint8)).save(picture)
    world = _Maps(["model_tex:" + str(picture)], monkeypatch)
    name = "model_tex:" + str(picture)
    assert world.maps(name) is None                  # being made, off this thread
    found = world.maps(name, wait=True)
    assert found is not None and found[2] == 0.0     # a guessed height: no parallax
    assert world.renderer.material_stats["generated"] == 1
    made = sorted(os.listdir(cache))
    assert len(made) == 2 and made[0].endswith("_m.png") and made[1].endswith("_n.png")
    assert sorted(os.path.basename(p) for p in world.loaded) == made
    stamps = [os.path.getmtime(cache / n) for n in made]
    # A second renderer finds them on disk: nothing is generated again.
    again = _Maps([name], monkeypatch)
    assert again.maps(name, wait=True) is not None
    assert [os.path.getmtime(cache / n) for n in made] == stamps
    del asset_bytes
