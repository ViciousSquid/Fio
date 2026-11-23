"""Visual tier: at ``graphics = low`` none of the HD renderer runs.

``tests/visual/test_golden_low.py`` proves low draws the pixels it always
drew. This is the other half of the lock: it does so *because* the HD code is
never entered, not because that code happens to cancel out. Every method of
the HD mixins is replaced by one that fails the test, the companion-map
lookups are watched, and a low renderer is built, drawn with and torn down:

* no HD method is called (only the two that set plain attributes at
  construction and the teardown that returns at once);
* the shader loader's ``compile_from_source`` is the loader's own -- no
  source is rewritten;
* no HDR, shadow or environment target, no ``FioHdr`` buffer;
* no ``_n`` / ``_m`` / ``.mat.json`` companion is looked for or opened;
* an OBJ's ``Ke`` / ``Pr`` / ``Pm`` change nothing a model is drawn with.

The default tier is low, and low-power ARM mode is low whatever is asked.
"""

import configparser

import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl]

#: The only HD-module methods a low renderer may call: plain attribute
#: set-up, the teardown (which returns immediately), and setters the host
#: calls at every tier, which only store what they are given.
ALLOWED_AT_LOW = {"_init_environment", "_init_postfx", "_cleanup_environment",
                  "set_hd_sky", "set_graphics", "hd_timing"}


def _config(**renderer):
    config = configparser.ConfigParser()
    config.add_section("Renderer")
    config.set("Renderer", "lowpower_mode", "False")
    for key, value in renderer.items():
        config.set("Renderer", key, value)
    return config


def _trap_hd(monkeypatch):
    """Make every HD method fail the test; return the list calls land in."""
    from engine.renderer.forward.environment import EnvironmentMixin
    from engine.renderer.forward.postfx import PostFXMixin
    called = []
    for mixin in (EnvironmentMixin, PostFXMixin):
        for name, value in list(vars(mixin).items()):
            if name in ALLOWED_AT_LOW or name.startswith("__"):
                continue
            target = value.__func__ if isinstance(value, (staticmethod, classmethod)) else value
            if not callable(target):
                continue

            def trap(*args, _name=name, **kwargs):
                called.append(_name)
                raise AssertionError("HD method %s ran at graphics = low" % _name)
            monkeypatch.setattr(mixin, name, trap)
    return called


def _watch_companions(monkeypatch):
    """Record every asset lookup that names a companion map or a sidecar."""
    import engine.asset_archives as archives
    import engine.material_maps as material_maps
    seen = []

    def watched(real):
        def lookup(path, *args, **kwargs):
            text = str(path).replace("\\", "/").lower()
            stem = text.rsplit(".", 1)[0]
            if text.endswith(".mat.json") or stem.endswith(("_n", "_m", "_h", "_e")):
                seen.append(text)
            return real(path, *args, **kwargs)
        return lookup
    for name in ("asset_exists", "asset_bytes", "asset_source"):
        if hasattr(archives, name):
            monkeypatch.setattr(archives, name, watched(getattr(archives, name)))
    for name in ("companion_paths", "ensure_cached", "generate", "load_params", "status"):
        def refuse(*args, _name=name, **kwargs):
            seen.append("material_maps." + _name)
            raise AssertionError("material_maps.%s ran at graphics = low" % _name)
        monkeypatch.setattr(material_maps, name, refuse)
    return seen


@pytest.mark.parametrize("setting", [None, "low", "LOW", "nonsense", ""])
def test_a_low_renderer_never_enters_the_hd_code(monkeypatch, setting):
    import OpenGL.GL as gl
    from engine.renderer.core.resources import ShaderLoader
    called = _trap_hd(monkeypatch)
    companions = _watch_companions(monkeypatch)
    config = _config() if setting is None else _config(graphics=setting)

    glh.reset_texture_cache()
    with glh.GLTestContext(96, 96) as context:
        renderer = glh.make_renderer(config)
        try:
            assert renderer.graphics == "low" and renderer.hd is False
            assert renderer.surface_shading is False
            # The loader's own method, not a wrapper that rewrites sources.
            assert (renderer.shader_loader.compile_from_source.__func__
                    is ShaderLoader.compile_from_source)
            for name, program in renderer.shaders.items():
                if program:
                    assert gl.glGetUniformLocation(program, "uSurfaceMode") == -1, name
                    assert gl.glGetUniformBlockIndex(program, "FioHdr") == gl.GL_INVALID_INDEX, name

            renderer.set_background_image("assets/backgrounds/cloudy-sky-panorama.webp")
            renderer.set_hd_sky({"sky": "procedural"})      # stored, never read
            brushes, things = glh.lit_cube_scene()
            for brush in brushes:
                brush["textures"] = {face: "cobblestone.jpg" for face in
                                     ("north", "south", "east", "west", "top", "down")}
            projection, view, eye = glh.camera_matrices()
            for play in (False, True):
                frame = glh.render_config(all_brushes=brushes, all_things=things,
                                          play_mode=play)
                for _ in range(2):
                    context.bind()
                    with glh.no_gl_errors("low frame"):
                        renderer.render_scene(projection, view, eye, None, frame,
                                              brush_slots=frame["all_brush_slots"])
            assert not glh.is_blank(context.read_pixels())

            # No HD target or buffer was ever made.
            for name in ("_env_shadow_tex", "_env_shadow_static_tex", "_env_map",
                         "_env_reflect_map", "_env_noise_tex", "_env_shadow_fbo",
                         "_env_map_fbo", "_hdr_ubo", "_env_vao"):
                assert getattr(renderer, name) == 0, name
            assert renderer._postfx_textures == {} and renderer._postfx_fbos == {}
            assert renderer._env_programs == {} and renderer._material_maps == {}
            assert renderer._env_active is False
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()
    assert called == [], called
    assert companions == [], companions


def test_low_power_mode_is_low_whatever_is_asked():
    from engine import hd_graphics
    config = _config(graphics="ultra")
    assert hd_graphics.tier_from(config) == "ultra"
    assert hd_graphics.tier_from(config, lowpower=True) == "low"
    config.set("Renderer", "lowpower_mode", "True")
    glh.reset_texture_cache()
    with glh.GLTestContext(16, 16):
        renderer = glh.make_renderer(config)
        try:
            assert renderer.lowpower_mode and renderer.graphics == "low" and not renderer.hd
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()


def test_the_default_tier_is_low_and_the_shipped_settings_say_low():
    from engine import hd_graphics
    from tests.helpers.paths import repo_path
    assert hd_graphics.DEFAULT_TIER == "low"
    assert hd_graphics.tier_from(None) == "low"
    assert hd_graphics.tier_from(configparser.ConfigParser()) == "low"
    shipped = configparser.ConfigParser()
    shipped.read(repo_path("settings.ini"))
    assert shipped.get("Renderer", "graphics", fallback="low").strip().lower() == "low"


def test_an_obj_materials_ke_pr_pm_reach_nothing_at_low(tmp_path):
    """The MTL parser reads ``Ke`` / ``Pr`` / ``Pm``, but a low renderer's
    draw groups carry exactly what they carried before it did: no emission
    from ``Ke`` (classic Fio never took any) and no surface values."""
    from engine import obj_loader
    from engine.renderer.core.resources import ResourcesMixin
    (tmp_path / "lamp.mtl").write_text(
        "newmtl glow\nKd 0.5 0.4 0.3\nKe 2.0 1.5 1.0\nPr 0.2\nPm 0.9\n")
    (tmp_path / "lamp.obj").write_text(
        "mtllib lamp.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl glow\nf 1 2 3\n")
    model = obj_loader.OBJLoader()
    model._load_mtl(str(tmp_path / "lamp.obj"), "lamp.mtl")
    material = model.materials["glow"]
    # The key classic Fio reads for emission is absent: Ke is kept apart.
    assert "emissive" not in material
    assert material["hd_emissive"] == (2.0, 1.5, 1.0)
    assert material["roughness"] == 0.2 and material["metallic"] == 0.9

    class Renderer(ResourcesMixin):
        def __init__(self, hd):
            self.hd = hd
            self._model_draw_group_cache = {}

    class Model:
        materials = {"glow": material}
        groups = [{"material": "glow", "start": 0, "count": 3}]
        ebo = None

    low = Renderer(False)._model_draw_groups(Model())[0]
    assert low.emissive == (0.0, 0.0, 0.0) and low.surface is None
    hd = Renderer(True)._model_draw_groups(Model())[0]
    assert hd.emissive == (2.0, 1.5, 1.0) and hd.surface == (0.2, 0.9)
