"""The renderer opens only project assets for names a map supplies.

Texture names, sprite folders, model paths, a model's texture references and
a custom effect GIF all arrive from the map. These drive the real
``ResourcesMixin`` loaders with OpenGL replaced by a recorder (no context is
needed to decide which file is opened) and record every file Pillow opens.
"""

import ctypes
import os

import pytest

pytest.importorskip("PyQt5")
pytest.importorskip("OpenGL")
pytest.importorskip("PIL")

from PIL import Image                                         # noqa: E402

from engine import asset_paths                                # noqa: E402
from engine.asset_archives import prepare_asset_archives      # noqa: E402
from engine.renderer.core import resources                    # noqa: E402
from engine.renderer.core import gl_state                     # noqa: E402

pytestmark = pytest.mark.qt


class _FakeGL:
    """Every GL call succeeds; texture names count up from 1."""
    GLubyte = ctypes.c_ubyte

    def __init__(self):
        self.next_name = 0
        self.uploads = []

    def glGenTextures(self, _count):
        self.next_name += 1
        return self.next_name

    def glTexImage2D(self, _target, _level, _fmt, width, height, *_rest):
        if (width, height) != (1, 1):          # the built-in default texture
            self.uploads.append((width, height))

    def __getattr__(self, name):
        if name.startswith("gl"):
            return lambda *args, **kwargs: None
        return name                        # an enum: only ever passed back


class _Renderer(resources.ResourcesMixin):
    def __init__(self):
        self.texture_manager = {}
        self.loaded_models = {}
        self._model_draw_group_cache = {}
        self._manual_model_textures = {}


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project with one texture and a secret image outside it."""
    root = tmp_path / "proj"
    for folder in ("textures/Dev", "sprites/monsters/human", "models", "textures/effects"):
        (root / "assets" / folder).mkdir(parents=True)
    (root / "plugins" / "demo" / "assets").mkdir(parents=True)
    Image.new("RGB", (4, 2), (1, 2, 3)).save(root / "assets" / "textures" / "Dev" / "256.png")
    Image.new("RGB", (6, 2), (1, 2, 3)).save(
        root / "assets" / "sprites" / "monsters" / "human" / "idle.png")
    Image.new("RGB", (8, 2), (1, 2, 3)).save(root / "plugins" / "demo" / "assets" / "skin.png")
    outside = tmp_path / "outside"
    outside.mkdir()
    Image.new("RGB", (3, 5), (9, 9, 9)).save(outside / "secret.png")
    monkeypatch.chdir(root)
    prepare_asset_archives(str(root))
    fake = _FakeGL()
    monkeypatch.setattr(resources, "gl", fake)
    monkeypatch.setattr(gl_state, "gl", fake)     # bindings go through core.gl_state
    monkeypatch.setattr(asset_paths, "_warned", set())
    opened = []
    real_open = Image.open

    def spy(source, *args, **kwargs):
        opened.append(os.fspath(source) if isinstance(source, (str, os.PathLike)) else "<bytes>")
        return real_open(source, *args, **kwargs)

    monkeypatch.setattr(Image, "open", spy)
    yield root, outside, fake, opened
    prepare_asset_archives(os.getcwd())


def _hostile(outside):
    return [
        str(outside / "secret.png"),                 # absolute
        "../../../outside/secret.png",
        "..\\..\\..\\outside\\secret.png",
        "\\\\evil-host\\share\\x.png",               # UNC
        "//evil-host/share/x.png",
        "C:\\Windows\\x.png",
    ]


def test_a_hostile_texture_name_gets_the_default_texture_and_opens_nothing(project, capsys):
    _root, outside, fake, opened = project
    renderer = _Renderer()
    default = renderer.load_texture("default.png", "textures")
    for name in _hostile(outside):
        tex = renderer.load_texture(name, "textures")
        assert tex == default, f"texture name {name!r} did not fall back to the default texture"
    assert opened == [], f"files opened for hostile texture names: {opened}"
    assert fake.uploads == [], "nothing but the 1x1 default may have been uploaded"
    out = capsys.readouterr().out
    assert out.count("Refused texture path") == len(_hostile(outside)), out
    renderer.load_texture(_hostile(outside)[0], "textures")
    assert "Refused" not in capsys.readouterr().out, "one warning per name, not one per request"


def test_a_hostile_sprite_folder_is_refused_too(project):
    _root, outside, _fake, opened = project
    renderer = _Renderer()
    default = renderer.load_texture("default.png", "textures")
    # entity_table splits an authored sprite_path into (file, folder).
    assert renderer.load_texture("secret.png", str(outside)) == default
    assert renderer.load_texture("secret.png", "../../outside") == default
    assert opened == [], f"opened {opened}"


def test_names_with_subfolders_still_load(project):
    _root, _outside, fake, opened = project
    renderer = _Renderer()
    default = renderer.load_texture("default.png", "textures")
    assert renderer.load_texture("Dev/256.png", "textures") != default
    assert renderer.load_texture("idle.png", "sprites/monsters/human") != default
    assert renderer.load_texture("monsters/human/idle.png", "sprites") != default
    assert fake.uploads == [(4, 2), (6, 2)], "the two spellings name one cached texture"
    assert renderer.load_texture("missing.png", "textures") == default
    assert len(opened) == 2, opened


def test_a_model_texture_reference_cannot_leave_the_assets(project):
    root, outside, _fake, opened = project
    renderer = _Renderer()
    material = {"mtl_dir": os.path.join("assets", "models")}
    for name in _hostile(outside):
        resolved = renderer._resolve_model_texture_path(material, name)
        assert resolved is None, f"map_Kd {name!r} resolved to {resolved!r}"
        renderer._model_texture_id(name, dict(material, texture=name))
    assert opened == [], f"opened {opened}"

    Image.new("RGB", (2, 2)).save(root / "assets" / "models" / "skin.png")
    assert renderer._resolve_model_texture_path(material, "skin.png") == os.path.join(
        "assets", "models", "skin.png")
    assert renderer._resolve_model_texture_path(material, "../textures/Dev/256.png") == os.path.join(
        "assets", "textures", "Dev", "256.png")
    assert renderer._resolve_model_texture_path({}, "Dev/256.png") == os.path.join(
        "assets", "textures", "Dev", "256.png")
    # A plugin model's material, and a manual texture given as a project path.
    plugin = {"mtl_dir": os.path.join("plugins", "demo", "assets")}
    assert renderer._resolve_model_texture_path(plugin, "skin.png") == os.path.join(
        "plugins", "demo", "assets", "skin.png")
    assert renderer._resolve_model_texture_path({}, "plugins/demo/assets/skin.png") == os.path.join(
        "plugins", "demo", "assets", "skin.png")
    assert renderer._resolve_model_texture_path(plugin, "../../../assets/textures/Dev/256.png") in (
        None, os.path.join("assets", "textures", "Dev", "256.png")), (
        "a plugin material may fall back to assets/textures, never climb to another path")


def test_a_hostile_model_path_is_not_opened(project, monkeypatch, capsys):
    root, outside, _fake, _opened = project
    (outside / "secret.obj").write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    (root / "assets" / "models" / "ok.obj").write_text("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    asked = []

    class _Model:
        def __init__(self, path):
            asked.append(path)
            self.is_loaded = True

    monkeypatch.setattr(resources, "OBJ", _Model)
    monkeypatch.setattr(resources, "GLB", _Model)
    renderer = _Renderer()
    for path in (str(outside / "secret.obj"), "../../outside/secret.obj",
                 "\\\\evil-host\\share\\a.obj", "//evil-host/share/a.glb", "C:\\a.obj"):
        assert renderer.load_model(path) is None, f"model path {path!r} was loaded"
    assert asked == [], f"loaders were given {asked}"
    assert "Refused model path" in capsys.readouterr().out

    assert renderer.load_model("ok.obj") is not None                       # bare name
    assert renderer.load_model("assets/models/ok.obj") is not None         # project path
    assert renderer.load_model("assets\\models/ok.obj") is not None        # as a shipped map spells it
    assert renderer.load_model(str(root / "assets" / "models" / "ok.obj")) is not None, (
        "an absolute path inside the project (PlayerStart.editor_model_path) must load")
    assert [os.path.normpath(p) for p in asked][0] == os.path.join("assets", "models", "ok.obj")


def test_a_custom_effect_gif_outside_the_project_is_not_read(project, monkeypatch, capsys):
    root, outside, _fake, _opened = project
    read = []
    monkeypatch.setattr(resources, "asset_bytes", lambda path: read.append(path))
    renderer = _Renderer()
    for path in (str(outside / "secret.png"), "../outside/secret.png", "\\\\host\\share\\a.gif",
                 "settings.ini"):
        frames, _timing = renderer._load_fire_gif(path)
        assert frames == [], f"effect GIF {path!r} produced frames"
    assert read == [], f"read {read}"
    renderer._load_fire_gif("assets/textures/effects/fire01.gif")
    assert read == [os.path.join(os.getcwd(), "assets", "textures", "effects", "fire01.gif")]


def test_an_oversized_animated_texture_keeps_one_frame(project):
    from tests.engine.test_texture_size_budget import bomb_gif
    root, _outside, fake, _opened = project
    bomb_gif(root / "assets" / "textures" / "bomb.gif", side=4096, frames=40)
    renderer = _Renderer()
    tex = renderer.load_texture("bomb.gif", "textures")
    assert tex != renderer.load_texture("default.png", "textures"), "its first frame still shows"
    assert fake.uploads == [(4096, 4096)]
    assert renderer._animated_textures() == {}, "an over-budget GIF must not keep animation frames"
