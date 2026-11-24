"""Visual tier: HD_High, the second renderer.

HD_High (``engine/renderer/hd_high``) is created through the registry like any
renderer and drawn with on a real GL context: every sky mode draws without a
GL error; an indoor map keeps the brightness Classic gives it; daylight
spreads through a window and stops at a wall; the host swaps Classic and HD_High live
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


#: What OpenGL 3.3 guarantees a fragment shader, and all that some drivers
#: (macOS) give: a program that samples more does not link there.
FRAGMENT_SAMPLER_LIMIT = 16
#: Samplers only a program's vertex shader reads.
VERTEX_SAMPLERS = ("uTransforms", "uHeights")


def _active_samplers(program):
    import OpenGL.GL as gl
    kinds = {int(getattr(gl, name)) for name in dir(gl)
             if name.startswith(("GL_SAMPLER_", "GL_INT_SAMPLER_", "GL_UNSIGNED_INT_SAMPLER_"))}
    found = {}
    for index in range(gl.glGetProgramiv(program, gl.GL_ACTIVE_UNIFORMS)):
        name, size, kind = gl.glGetActiveUniform(program, index)
        if int(kind) in kinds:
            name = name.decode() if isinstance(name, bytes) else str(name)
            found[name.split("[")[0]] = int(size)
    return found


def test_no_program_samples_more_than_a_fragment_shader_is_guaranteed():
    """Every HD_High program links where a fragment shader has 16 samplers
    and no more: the point lights' shadow maps are one depth array, not a
    cube sampler per light."""
    glh.reset_texture_cache()
    with glh.GLTestContext(64, 64):
        renderer = glh.make_renderer(_config(), name="HD_High")
        try:
            assert renderer.ready
            for name in ("lit", "textured", "textured_instanced", "brush_instanced",
                         "geo_batched", "terrain"):
                assert renderer.shaders.get(name), name
            for name, program in renderer.shaders.items():
                if not program:
                    continue
                samplers = _active_samplers(int(getattr(program, "id", program)))
                in_fragment = sum(size for sampler, size in samplers.items()
                                  if sampler not in VERTEX_SAMPLERS)
                assert in_fragment <= FRAGMENT_SAMPLER_LIMIT, (name, samplers)
                if "shadowMaps" in samplers:
                    assert samplers["shadowMaps"] == 1, (name, samplers)
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()


def test_point_shadows_share_one_array_and_keep_each_lights_own_size():
    """Two shadow-casting lamps, one with its own (smaller) map size: both
    cast shadows from the one depth array, each at its size."""
    from editor.things import Light
    from tests.helpers.worlds import box_brush, make_thing
    brushes = [box_brush("floor", (0, -16, 0), (2048, 32, 2048)),
               box_brush("cube", (0, 150, 0), (300, 300, 300))]

    def lamps(shadows):
        return [make_thing(Light, "a", (-700.0, 500.0, 0.0), color=[255, 255, 255],
                           intensity=2.0, radius=1600.0, state="on", casts_shadows=shadows),
                make_thing(Light, "b", (0.0, 500.0, -700.0), color=[255, 255, 255],
                           intensity=1.6, radius=1400.0, state="on", casts_shadows=shadows,
                           shadow_map_size=256)]
    frames = {}
    glh.reset_texture_cache()
    with glh.GLTestContext(240, 150) as context:
        renderer = glh.make_renderer(_config(), name="HD_High")
        try:
            from engine import hd_graphics
            renderer.set_hd_sky(hd_graphics.sky_settings({"hd_grain": 0.0}, ""))
            for shadows in (True, False):
                frames[shadows] = _frame(context, renderer, brushes, lamps(shadows),
                                         aspect=1.6, eye=(500.0, 420.0, 900.0),
                                         target=(0.0, 60.0, 0.0)).astype(np.int32)
            assert len(renderer._shadow_cubemaps) == 1
            assert renderer._shadow_array_size == renderer.shadow_map_size
            assert renderer._shadow_array_slots == 2
            assert sorted(renderer._shadow_cube_sizes[:2]) == [256, renderer.shadow_map_size]
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()
    darker = (frames[False] - frames[True]).max(axis=2)
    # The cube's shadows are there, and nothing is brighter for casting them.
    assert (darker > 10).mean() > 0.01, [(darker > t).mean() for t in (5, 10, 25)]
    assert (frames[True] - frames[False]).max() <= 2


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
            assert bool(renderer._env_skylight_ready) == (sky != "none")
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


def _daylight_room(window):
    """A closed room on a wide floor, with a window in its +x wall or none."""
    from tests.helpers.worlds import box_brush
    brushes = [box_brush("floor", (0, -16, 0), (2048, 32, 2048)),
               box_brush("roof", (0, 264, 0), (544, 16, 544)),
               box_brush("west", (-264, 128, 0), (16, 256, 544)),
               box_brush("north", (0, 128, -264), (544, 256, 16)),
               box_brush("south", (0, 128, 264), (544, 256, 16)),
               box_brush("east a", (264, 128, -168), (16, 256, 208)),
               box_brush("east b", (264, 128, 168), (16, 256, 208)),
               box_brush("sill", (264, 32, 0), (16, 64, 128)),
               box_brush("head", (264, 224, 0), (16, 64, 128))]
    if not window:
        brushes.append(box_brush("plug", (264, 128, 0), (16, 128, 128)))
    return brushes


def test_daylight_spreads_through_a_window_and_stops_at_a_wall():
    """Under a sky with no lamps, seen from inside a room: with a window the
    floor is lit, brightest by the window; with the window walled up the
    room is dark. Outside, the ground is as bright either way."""
    from engine import hd_graphics
    glh.reset_texture_cache()
    seen = {}
    with glh.GLTestContext(240, 150) as context:
        renderer = glh.make_renderer(_config(), name="HD_High")
        try:
            renderer.set_background_image(PANORAMA)
            # A fixed exposure: the room is compared with itself.
            renderer.set_hd_sky(hd_graphics.sky_settings(
                {"hd_grain": 0.0, "hd_sun": False}, PANORAMA))
            renderer.environment.exposure = 1.0
            renderer.SKY_LIGHT_INTERVAL = 0.0       # each change at once
            for window in (True, False):
                # From the far wall, looking down at the floor toward the window.
                pixels = _frame(context, renderer, _daylight_room(window), [], aspect=1.6,
                                fov_deg=80.0, eye=(-220.0, 180.0, 0.0),
                                target=(200.0, 0.0, 0.0))
                if os.environ.get("FIO_TEST_IMAGES"):
                    from PIL import Image
                    Image.fromarray(pixels).save(os.path.join(
                        os.environ["FIO_TEST_IMAGES"], "daylight_%s.png" % window))
                near = glh.luminance(glh.region(pixels, 0.35, 0.45, 0.65, 0.6)).mean()
                far = glh.luminance(glh.region(pixels, 0.35, 0.85, 0.65, 0.98)).mean()
                outside = _frame(context, renderer, _daylight_room(window), [], aspect=1.6,
                                 fov_deg=80.0, eye=(900.0, 300.0, 0.0),
                                 target=(500.0, 0.0, 0.0))
                ground = glh.luminance(glh.region(outside, 0.3, 0.6, 0.7, 0.95)).mean()
                seen[window] = (near, far, ground)
            assert renderer._env_skylight_ready
            assert renderer.sun_shadow_stats.get("sky light", 0) >= 2
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()
    (near, far, ground), (dark_near, dark_far, dark_ground) = seen[True], seen[False]
    assert near > 40.0 and near > far * 1.3, seen          # daylight, fading into the room
    assert far > dark_far + 8.0, seen                      # ... and reaching its far side
    assert max(dark_near, dark_far) < near * 0.25, seen    # walled up: dark
    assert ground > 60.0 and abs(ground - dark_ground) < 3.0, seen


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


def test_the_host_swaps_classic_and_hd_high_live(fio_session, monkeypatch):
    """Settings > Display > Renderer and ``r_renderer`` both come down to
    QtGameView.switch_renderer: there and back, in the editor and in Play,
    and Classic draws what it drew before the trip.

    Hold the host's render clock still: the frame comparison is about renderer
    resource handoff, not animation accumulated while HD_High is constructed.
    """
    import time
    import types
    import engine.qt_game_view as qt_game_view

    frozen = types.SimpleNamespace(**{
        name: getattr(time, name) for name in dir(time)
        if not name.startswith("__")
    })
    now = time.perf_counter()
    frozen.perf_counter = lambda: now
    monkeypatch.setattr(qt_game_view, "time", frozen)

    from editor.things import Light, PlayerStart
    from tests.helpers.worlds import box_brush, level_data, make_thing, room
    spawn = make_thing(PlayerStart, "spawn", (0.0, 40.0, 0.0), angle=0.0)
    lamp = make_thing(Light, "lamp", (0.0, 300.0, -200.0), radius=2000.0,
                      intensity=2.0, casts_shadows=True)
    crate = box_brush("crate", (0.0, 64.0, -300.0), (128.0, 128.0, 128.0))
    session = fio_session(level_data(brushes=room(size=2048.0, height=512.0) + [crate],
                                     things=[spawn, lamp]))
    session.view.sysmon.set_active(False)
    # Begin on the original renderer, then switch both ways explicitly.
    assert session.view.switch_renderer("Forward")
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


# -- Forward+ ---------------------------------------------------------------

def _lamp_field(count):
    """A wide floor under a grid of *count* small lamps."""
    from editor.things import Light
    from tests.helpers.worlds import box_brush, make_thing
    side = int(np.ceil(np.sqrt(count)))
    lamps = []
    for index in range(count):
        x = (index % side - (side - 1) / 2.0) * 420.0
        z = (index // side - (side - 1) / 2.0) * 420.0
        lamps.append(make_thing(
            Light, "lamp%d" % index, (x, 140.0, z),
            color=[255, 220 - (index * 37) % 120, 160 + (index * 53) % 90],
            intensity=1.5, radius=520.0, state="on", casts_shadows=False))
    return [box_brush("floor", (0, -16, 0), (4096, 32, 4096)),
            box_brush("post", (40, 64, 60), (96, 128, 96))], lamps


def test_the_light_grid_lists_exactly_the_lamps_that_reach_each_cluster():
    from engine.renderer.hd_high import surface
    from engine.renderer.hd_high.environment import EnvironmentMixin
    rng = np.random.default_rng(11)
    positions = rng.uniform(-3000.0, 3000.0, (40, 3))
    positions[:, 1] = rng.uniform(0.0, 1600.0, 40)          # several storeys
    radii = rng.uniform(150.0, 900.0, 40)
    table, corner, size, height = EnvironmentMixin.light_grid(positions, radii)
    cells, layers = surface.LIGHT_GRID_CELLS, surface.LIGHT_GRID_LAYERS
    depth = surface.LIGHT_GRID_DEPTH
    assert table.shape == (layers * cells * cells, depth) and table.dtype == np.uint8
    steps = (size, height, size)
    for row in range(layers * cells * cells):
        listed = [int(i) for i in table[row] if i != 255]
        assert listed == sorted(listed)
        index = (row % cells, row // (cells * cells), (row // cells) % cells)   # x, y, z
        box = [(corner[a] + index[a] * steps[a], corner[a] + (index[a] + 1) * steps[a])
               for a in range(3)]
        expected = [i for i in range(40) if all(
            positions[i, a] + radii[i] > box[a][0] and positions[i, a] - radii[i] < box[a][1]
            for a in range(3))]
        assert listed == expected, row
    # Every point a lamp can light lies in a cluster that lists the lamp.
    for i in range(40):
        for offset in ((0, 0, 0), (0.99, 0, 0), (-0.99, 0, 0), (0, 0.99, 0), (0, -0.99, 0),
                       (0, 0, 0.99), (0, 0, -0.99)):
            point = positions[i] + np.array(offset) * radii[i]
            x, y, z = (int((point[a] - corner[a]) / steps[a]) for a in range(3))
            assert i in table[(y * cells + z) * cells + x]
    # Lamps on different storeys do not share a cluster.
    stacked = np.array([[0.0, 0.0, 0.0], [0.0, 2000.0, 0.0]])
    table, corner, size, height = EnvironmentMixin.light_grid(stacked, np.array([200.0, 200.0]))
    assert not ((table == 0).any(axis=1) & (table == 1).any(axis=1)).any()
    # More lamps on one spot than a list holds: no grid (the plain loop).
    crowd = np.zeros((depth + 1, 3))
    assert EnvironmentMixin.light_grid(crowd, np.full(depth + 1, 100.0)) is None


def test_forward_plus_draws_the_picture_the_plain_loop_draws():
    """Many lamps: the grid is in use, most lamps are skipped per fragment,
    and not a pixel is lit differently from testing every lamp."""
    brushes, lamps = _lamp_field(36)
    pictures = {}
    for label, minimum in (("grid", None), ("loop", 10 ** 6)):
        glh.reset_texture_cache()
        with glh.GLTestContext(200, 120) as context:
            renderer = glh.make_renderer(_config(), name="HD_High")
            try:
                if minimum is not None:
                    renderer.FORWARD_PLUS_MIN_LIGHTS = minimum
                # No film grain: it moves with the clock.
                from engine import hd_graphics
                renderer.set_hd_sky(hd_graphics.sky_settings({"hd_grain": 0.0}, ""))
                pictures[label] = _frame(context, renderer, brushes, lamps, aspect=200 / 120,
                                         eye=(300.0, 520.0, 1500.0), target=(0.0, 0.0, 0.0))
                stats = dict(renderer.light_grid_stats)
            finally:
                renderer.cleanup()
        if label == "grid":
            assert stats["on"] and stats["builds"] == 1 and stats["lamps"] == 36
            assert stats["max_per_cell"] < 36 and stats["mean_per_cell"] < 12
        else:
            assert not stats["on"] and stats["builds"] == 0
    glh.reset_texture_cache()
    difference = np.abs(pictures["grid"].astype(np.int16)
                        - pictures["loop"].astype(np.int16)).max()
    assert not glh.is_blank(pictures["grid"])
    assert difference <= 1, difference


def test_a_few_lamps_do_not_build_a_grid():
    brushes, lamps = _lamp_field(4)
    glh.reset_texture_cache()
    with glh.GLTestContext(96, 64) as context:
        renderer = glh.make_renderer(_config(), name="HD_High")
        try:
            _frame(context, renderer, brushes, lamps, aspect=1.5)
            assert not renderer.light_grid_stats["on"]
            assert renderer.light_grid_stats["builds"] == 0 and renderer._env_grid_tex == 0
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()


@pytest.mark.slow
def test_every_stock_map_draws_with_hd_high(tmp_path):
    """The stock maps of the golden set, loaded by the real editor started
    with HD_High, from the editor camera and in Play: every frame is drawn
    (none blank), in HD (the environment never switched itself off) and no
    Python error is raised on the way."""
    import subprocess
    import sys
    from PIL import Image
    from tests.helpers import golden
    from tests.helpers.paths import REPO_ROOT
    environment = dict(os.environ, PYTHONHASHSEED="0")
    environment.pop("QT_QPA_PLATFORM", None)
    result = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "tools", "hd_golden.py"),
         "--dump", str(tmp_path), "--renderer", "HD_High"],
        cwd=REPO_ROOT, env=environment, capture_output=True, text=True, timeout=2400)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-3000:]
    assert "Traceback" not in output, output[output.index("Traceback"):][:3000]
    for sign in ("[Environment] disabled", "sun shadows off", "sky light off",
                 "Shader Error", "failed to start"):
        assert sign not in output, sign
    with open(tmp_path / "manifest.json") as handle:
        import json
        frames = json.load(handle)["frames"]
    assert len(frames) >= 2 * len(golden.MAPS)
    for key in frames:
        pixels = np.asarray(Image.open(tmp_path / (key.replace("/", "__") + ".png")).convert("RGB"))
        assert not glh.is_blank(pixels), key
