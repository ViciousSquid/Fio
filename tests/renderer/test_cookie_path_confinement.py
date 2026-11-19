"""A Light's cookie image is named by the map: only project assets are read.

``cookie_texture`` is data from outside, like a texture name. Whatever it
says -- an absolute path, ``..`` in POSIX or Windows spelling, a drive, a
UNC share, a URL, an NTFS stream, a symlink out of ``assets/`` -- the
desktop reader (:func:`engine.light_cookies.project_bytes`) refuses it, on
every host, before anything is opened; the layer then samples nothing.
"""

import os

import pytest

pytest.importorskip("PIL")

from PIL import Image                                         # noqa: E402

from engine import asset_archives, asset_paths, light_cookies  # noqa: E402
from engine.asset_archives import prepare_asset_archives      # noqa: E402
from engine.package_assets import map_assets                  # noqa: E402

GOOD = "assets/textures/cookies/good.png"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A project with one cookie image, and a secret image outside it."""
    root = tmp_path / "proj"
    (root / "assets" / "textures" / "cookies").mkdir(parents=True)
    Image.new("RGB", (8, 8), (10, 20, 30)).save(root / GOOD)
    outside = tmp_path / "outside"
    outside.mkdir()
    Image.new("RGB", (8, 8), (9, 9, 9)).save(outside / "secret.png")
    monkeypatch.chdir(root)
    prepare_asset_archives(str(root))
    monkeypatch.setattr(asset_paths, "_warned", set())
    opened = []
    real = asset_archives.asset_bytes

    def spy(path, *args, **kwargs):
        opened.append(os.fspath(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(asset_archives, "asset_bytes", spy)
    yield root, outside, opened
    prepare_asset_archives(os.getcwd())


def _hostile(outside):
    names = [
        str(outside / "secret.png"),                         # absolute, this host
        "/etc/passwd.png",                                   # absolute, POSIX
        "C:\\Windows\\secret.png",                           # drive, Windows
        "C:/Windows/secret.png",
        "c:secret.png",                                      # drive-relative
        "\\\\evil-host\\share\\secret.png",                  # UNC
        "//evil-host/share/secret.png",
        "\\\\?\\C:\\secret.png",                             # device path
        "../outside/secret.png",
        "../../outside/secret.png",
        "..\\..\\outside\\secret.png",                       # Windows separators
        "assets/../../outside/secret.png",                   # climbs out after a good start
        "assets\\..\\..\\outside\\secret.png",
        "assets/textures/../../../outside/secret.png",
        "assets/textures/cookies/good.png:stream.png",       # NTFS stream
        "file:///etc/passwd.png",                            # URL scheme
        "maps/secret.png",                                   # not an asset folder
        "assets/textures/cookies/good.png\0.png",            # NUL
    ]
    return names


def test_a_hostile_cookie_name_is_refused_before_anything_is_opened(project, capsys):
    _root, outside, opened = project
    for name in _hostile(outside):
        assert light_cookies.project_bytes(name) is None, f"{name!r} was read"
    assert opened == [], f"files opened for hostile cookie names: {opened}"
    out = capsys.readouterr().out
    assert out.count("Refused cookie image path") == len(_hostile(outside)), out
    light_cookies.project_bytes(_hostile(outside)[0])
    assert "Refused" not in capsys.readouterr().out, "one warning per name"


def test_a_symlink_out_of_the_assets_is_refused(project):
    root, outside, opened = project
    link = root / "assets" / "textures" / "cookies" / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot make a directory symlink")
    assert light_cookies.project_bytes("assets/textures/cookies/escape/secret.png") is None
    assert opened == []


def test_a_project_cookie_image_is_read(project):
    _root, _outside, opened = project
    data = light_cookies.project_bytes(GOOD)
    assert data and data[:8] == b"\x89PNG\r\n\x1a\n"
    assert light_cookies.project_bytes(GOOD.replace("/", "\\")), (
        "a Windows-spelled project path is the same image")
    assert opened, "the project image should have been read"


def test_a_light_with_a_hostile_cookie_samples_nothing(project):
    """Through the real cookie array: a refused image gets no layer (and no
    GL is touched, so no context is needed to show it)."""
    _root, outside, opened = project
    array = light_cookies.CookieArray(None, size=8)
    ids = [light_cookies.intern_cookie(name) for name in _hostile(outside)]
    assert array.layers(ids).tolist() == [0] * len(ids)
    assert array.texture is None and opened == []


def test_the_packager_lists_the_cookie_image_and_the_exporter_confines_it(project):
    """``cookie_texture`` is packaged into a .fiopak; the exporter resolves it
    only inside the project's assets folder."""
    root, outside, _opened = project
    from editor.package_exporter import PackageExporter
    level = {"things": [{"type": "light", "properties": {
        "cookie_enabled": True, "cookie_texture": name}}
        for name in [GOOD, "../outside/secret.png", "..\\..\\outside\\secret.png",
                     str(outside / "secret.png")]]}
    names = map_assets(level)
    assert GOOD in names
    exporter = PackageExporter.__new__(PackageExporter)
    exporter.root_dir = str(root)
    exporter.errors = []
    resolved = {name: exporter._resolve_asset(name) for name in names}
    assert resolved[GOOD] == os.path.normpath(str(root / GOOD))
    for name, path in resolved.items():
        if name != GOOD:
            assert path is None, f"{name!r} resolved outside the assets: {path}"
