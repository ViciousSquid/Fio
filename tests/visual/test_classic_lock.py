"""Classic is locked: it is the renderer it was, and HD_High is not in it.

Fio has two renderers. Classic (``engine/renderer/forward``, registry name
``Forward``) is the original and the default; HD_High
(``engine/renderer/hd_high``) is a separate renderer beside it, reached
through the same registry and contract. Adding the second must not have
changed the first. ``tests/visual/test_classic_golden.py`` checks the pixels
and the shaders the driver holds; this is the rest of the lock:

* every source file Classic is made of -- its package, the shared
  ``engine/renderer/core`` and ``engine/shaders.py`` -- has the digest
  recorded in ``tests/visual/golden/classic_sources.json`` (recorded from the
  commit before HD_High existed). A deliberate change to Classic is recorded
  with ``python tools/hd_golden.py --lock-sources``, after
  ``tools/hd_golden.py --check`` or ``--base`` has shown what it does to the
  picture;
* none of those files mentions HD_High, and building and drawing with
  Classic never imports it, never looks for a material companion map and
  never calls the material generator;
* Classic is the registry's default renderer, what a missing setting means,
  and the only one a low-power machine is given;
* what the OBJ loader now reads for HD_High (``Ke`` / ``Pr`` / ``Pm``)
  reaches nothing Classic draws a model with.
"""

import configparser
import json
import os
import subprocess
import sys

import pytest

from tests.helpers import golden
from tests.helpers.paths import REPO_ROOT, repo_path


def test_classics_source_files_are_the_recorded_ones():
    with open(os.path.join(golden.GOLDEN_DIR, "classic_sources.json")) as handle:
        recorded = json.load(handle)
    current = golden.classic_source_digests()
    added = sorted(set(current) - set(recorded))
    removed = sorted(set(recorded) - set(current))
    changed = sorted(path for path in set(current) & set(recorded)
                     if current[path] != recorded[path])
    assert not (added or removed or changed), (
        "Classic's source changed (added %s, removed %s, changed %s). If that "
        "was meant: check the picture with tools/hd_golden.py --check, then "
        "record it with tools/hd_golden.py --lock-sources." % (added, removed, changed))


def test_classic_does_not_mention_hd_high():
    for path in golden.classic_source_digests():
        with open(os.path.join(REPO_ROOT, path), encoding="utf-8") as handle:
            text = handle.read()
        for word in ("hd_high", "hd_graphics", "material_maps", "HD_High"):
            assert word not in text, "%s mentions %s" % (path, word)


def test_importing_classic_imports_nothing_of_hd_high():
    code = ("import sys; import engine.renderer.forward; "
            "bad = [m for m in sys.modules if 'hd_high' in m or m == 'engine.material_maps']; "
            "print(','.join(bad))")
    result = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == "", result.stdout


def test_classic_is_the_default_renderer():
    from engine import hd_graphics
    from engine.renderer import DEFAULT_RENDERER, canonical_name, renderer_factory
    from engine.renderer.forward import ForwardRenderer
    assert DEFAULT_RENDERER == "Forward" and canonical_name("Classic") == "Forward"
    assert renderer_factory("Classic") is ForwardRenderer
    assert hd_graphics.DEFAULT_RENDERER == "Classic"
    assert hd_graphics.renderer_from(None) == "Classic"
    assert hd_graphics.renderer_from(configparser.ConfigParser()) == "Classic"
    # Which of the two the shipped settings.ini starts with is the project's
    # choice; it names one of them, and Classic is what any missing or
    # unreadable setting means.
    shipped = configparser.ConfigParser()
    shipped.read(repo_path("settings.ini"))
    assert hd_graphics.renderer_from(shipped) in hd_graphics.RENDERERS
    # A low-power machine is given Classic whatever is asked.
    asked = configparser.ConfigParser()
    asked.add_section("Renderer")
    asked.set("Renderer", "renderer", "HD_High")
    assert hd_graphics.renderer_from(asked) == "HD_High"
    assert hd_graphics.renderer_from(asked, lowpower=True) == "Classic"


def test_an_mtls_ke_pr_pm_reach_nothing_classic_draws_with(tmp_path):
    from engine import obj_loader
    from engine.renderer.core.resources import ModelDrawGroup, ResourcesMixin
    (tmp_path / "lamp.mtl").write_text(
        "newmtl glow\nKd 0.5 0.4 0.3\nKe 2.0 1.5 1.0\nPr 0.2\nPm 0.9\n")
    (tmp_path / "lamp.obj").write_text(
        "mtllib lamp.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nusemtl glow\nf 1 2 3\n")
    model = obj_loader.OBJLoader()
    model._load_mtl(str(tmp_path / "lamp.obj"), "lamp.mtl")
    material = model.materials["glow"]
    # The key Classic reads for emission is absent: Ke is kept apart.
    assert "emissive" not in material
    assert material["hd_emissive"] == (2.0, 1.5, 1.0)

    class Renderer(ResourcesMixin):
        def __init__(self):
            self._model_draw_group_cache = {}

    class Model:
        materials = {"glow": material}
        groups = [{"material": "glow", "start": 0, "count": 3}]
        ebo = None

    group = Renderer()._model_draw_groups(Model())[0]
    assert type(group) is ModelDrawGroup and not hasattr(group, "surface")
    assert group.emissive == (0.0, 0.0, 0.0)


@pytest.mark.gl
def test_a_classic_frame_never_looks_for_material_maps(monkeypatch):
    import engine.asset_archives as archives
    import engine.material_maps as material_maps
    from tests.helpers import gl as glh
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
            raise AssertionError("material_maps.%s ran in a Classic frame" % _name)
        monkeypatch.setattr(material_maps, name, refuse)

    config = configparser.ConfigParser()
    config.add_section("Renderer")
    config.set("Renderer", "lowpower_mode", "False")
    glh.reset_texture_cache()
    with glh.GLTestContext(96, 96) as context:
        renderer = glh.make_renderer(config, name="Classic")
        try:
            assert type(renderer).__name__ == "ForwardRenderer"
            assert not hasattr(renderer, "hd") and not hasattr(renderer, "environment")
            renderer.set_background_image("assets/backgrounds/cloudy-sky-panorama.webp")
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
                    with glh.no_gl_errors("classic frame"):
                        renderer.render_scene(projection, view, eye, None, frame,
                                              brush_slots=frame["all_brush_slots"])
            assert not glh.is_blank(context.read_pixels())
        finally:
            renderer.cleanup()
    glh.reset_texture_cache()
    assert seen == [], seen
