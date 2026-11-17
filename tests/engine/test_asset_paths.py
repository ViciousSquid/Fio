"""Map- and model-supplied asset names never resolve outside the project.

A map is data from outside: a texture name, a model path, a model's
``mtllib`` line or a glTF buffer URI may be absolute, climb with ``..`` or be
a Windows drive or UNC path (``\\\\host\\share\\x.png`` -- which Windows
answers with an outbound SMB connection just for being probed). The resolver
in ``engine.asset_paths`` refuses those the same way on every OS, and keeps
every shape the shipped maps and plugins use.
"""

import os

import pytest

from engine import asset_paths
from engine.asset_paths import (
    asset_root, companion_path, confined_path, project_asset_path, safe_relative,
)

HOSTILE = [
    "/etc/passwd",
    "../secret.png",
    "../../../outside/secret.png",
    "a/../../secret.png",
    "..\\..\\secret.png",
    "\\\\evil-host\\share\\x.png",
    "//evil-host/share/x.png",
    "\\\\?\\C:\\Windows\\x.png",
    "C:\\Windows\\x.png",
    "C:/Windows/x.png",
    "c:x.png",
    "\\Windows\\x.png",
    "file:///etc/passwd",
    "x.png:stream",
    "..",
    "",
    "   ",
    "a\0b.png",
    None,
    42,
]

LEGITIMATE = {
    "default.png": "default.png",
    "Dev/256.jpg": "Dev/256.jpg",
    "Dev\\256.jpg": "Dev/256.jpg",
    "monsters/human/idle.png": "monsters/human/idle.png",
    "./a/./b.png": "a/b.png",
    "a/../b.png": "b.png",
    ' "quoted name.png" ': "quoted name.png",
    "Metal_08-512x512.png": "Metal_08-512x512.png",
}


@pytest.mark.parametrize("name", HOSTILE, ids=repr)
def test_a_hostile_name_is_refused_on_every_os(name):
    assert safe_relative(name) is None, f"safe_relative accepted {name!r}"
    if name not in ("", "   ", None):
        assert confined_path("assets", name) is None, (
            f"confined_path('assets', {name!r}) left the assets folder")


@pytest.mark.parametrize("name, expected", LEGITIMATE.items(), ids=repr)
def test_a_legitimate_name_keeps_working(name, expected):
    assert safe_relative(name) == expected
    assert confined_path("assets", "textures", name) == os.path.join(
        "assets", "textures", *expected.split("/"))


def test_dotdot_may_cancel_a_folder_but_never_leave_the_root():
    assert confined_path("assets", "textures", "../sprites/a.png") == os.path.join(
        "assets", "sprites", "a.png")
    assert confined_path("assets", "textures", "../../a.png") is None, (
        "'..' climbed out of the assets root")
    # An empty subfolder (a sprite directly in assets/) is not a refusal.
    assert confined_path("assets", "", "a.png") == os.path.join("assets", "a.png")


def test_a_symlink_leading_out_of_the_root_is_refused(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.png").write_bytes(b"x")
    textures = tmp_path / "proj" / "assets" / "textures"
    textures.mkdir(parents=True)
    (textures / "ok.png").write_bytes(b"x")
    try:
        os.symlink(outside, textures / "link")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")
    monkeypatch.chdir(tmp_path / "proj")
    assert confined_path("assets", "textures", "ok.png") is not None
    assert confined_path("assets", "textures", "link/secret.png") is None, (
        "a symlink inside assets/ reached a file outside it")


@pytest.mark.parametrize("path", [
    "assets/models/Oil_Drum.obj",
    "assets\\models/Oil_Drum_Grey.obj",          # as a shipped map spells it
    "plugins/tidy/assets/book.obj",
    "plugins/bigworld/assets/bigworldsettings.png",
    "assets/textures/effects/fire01.gif",
])
def test_project_paths_used_by_shipped_maps_resolve(path):
    expected = os.path.join(*path.replace("\\", "/").split("/"))
    assert project_asset_path(path) == expected


@pytest.mark.parametrize("path", [
    "/etc/passwd", "../outside/a.obj", "assets/../../a.obj", "assets/../settings.ini",
    "settings.ini", "maps/DevTest.json", "\\\\host\\share\\a.obj", "//host/share/a.obj",
    "C:\\a.obj", "C:/a.obj", "", None,
], ids=repr)
def test_project_paths_outside_assets_and_plugins_are_refused(path):
    assert project_asset_path(path) is None, f"project_asset_path accepted {path!r}"


def test_an_absolute_path_is_taken_only_inside_the_project(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    (project / "assets" / "editor").mkdir(parents=True)
    inside = project / "assets" / "editor" / "playerstart.obj"
    inside.write_text("v 0 0 0\n")
    outside = tmp_path / "elsewhere.obj"
    outside.write_text("v 0 0 0\n")
    monkeypatch.chdir(project)
    monkeypatch.setattr(asset_paths, "_project_roots", lambda: [str(project)])
    # Fio's own class attributes (PlayerStart.editor_model_path) are absolute.
    assert project_asset_path(str(inside)) == str(inside)
    assert project_asset_path(str(outside)) is None, "an absolute path outside the project was accepted"
    assert project_asset_path(str(project / "settings.ini")) is None


def test_a_models_companions_stay_in_its_assets_folder():
    model = os.path.join("assets", "models", "tree", "tree.obj")
    assert asset_root(model) == "assets"
    assert asset_root(os.path.join("plugins", "tidy", "assets", "book.obj")) == os.path.join(
        "plugins", "tidy", "assets")
    assert companion_path(model, "tree.mtl") == os.path.join("assets", "models", "tree", "tree.mtl")
    assert companion_path(model, "../../textures/bark.png") == os.path.join(
        "assets", "textures", "bark.png")
    for name in ("../../../outside/secret.mtl", "/etc/passwd", "\\\\host\\share\\a.mtl",
                 "C:\\a.mtl", "..\\..\\..\\a.mtl"):
        assert companion_path(model, name) is None, f"companion {name!r} left the assets folder"
    plugin_model = os.path.join("plugins", "tidy", "assets", "book.obj")
    assert companion_path(plugin_model, "book.mtl") == os.path.join(
        "plugins", "tidy", "assets", "book.mtl")
    assert companion_path(plugin_model, "../../../assets/x.mtl") is None, (
        "a plugin model reached outside its plugin's assets")


def test_a_model_outside_any_assets_folder_is_confined_to_its_own_folder(tmp_path):
    model = tmp_path / "models" / "a.obj"
    model.parent.mkdir()
    assert companion_path(str(model), "sub/a.mtl") == str(tmp_path / "models" / "sub" / "a.mtl")
    assert companion_path(str(model), "../a.mtl") is None
