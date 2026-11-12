"""The Asset Browser shows what end users' own .fiopak packs hold.

A pack in assets/ (any name but the built-in archives') adds its textures,
models and sounds to the Textures, Models and Audio tabs beside the loose
files, read in place. A pack dropped in while the editor runs appears when
assets/ changes.
"""

import os
import zipfile

import pytest

pytest.importorskip("PyQt5", reason="the Asset Browser is a Qt widget")

from editor.asset_browser import AssetBrowser               # noqa: E402
from engine import asset_archives                           # noqa: E402
from engine.asset_archives import prepare_asset_archives     # noqa: E402

pytestmark = pytest.mark.qt

PNG = bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489'
    '0000000d49444154789c6360f8cfc0f01f0005000201a5f1a8a40000000049454e44ae426082')


@pytest.fixture
def project(tmp_path):
    for rel in ("textures", "models", "sounds", "music"):
        (tmp_path / "assets" / rel).mkdir(parents=True)
    (tmp_path / "assets/textures/loose.png").write_bytes(PNG)
    (tmp_path / "assets/sounds/beep.wav").write_bytes(b"")
    with zipfile.ZipFile(tmp_path / "assets/MyPack.fiopak", "w") as archive:
        archive.writestr("MyPack/textures/Brick/wall.png", PNG)
        archive.writestr("MyPack/textures/top.png", PNG)
        archive.writestr("MyPack/models/crate.obj", "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
        archive.writestr("MyPack/sounds/door.ogg", b"")
    return tmp_path


@pytest.fixture
def browser(project, main_window, monkeypatch):
    saved = dict(asset_archives._state)
    monkeypatch.setattr(main_window, "root_dir", str(project))
    prepare_asset_archives(project)
    b = AssetBrowser(str(project / "assets" / "textures"), editor=main_window)
    yield b
    b.deleteLater()
    asset_archives._state.update(saved)


def _names(tab):
    return sorted(item.name_text for item in tab.items)


def test_pack_textures_models_and_sounds_are_listed(browser, project):
    textures = browser.tab_textures
    assert _names(textures) == ["loose.png", "top.png"]
    textures.load_directory(str(project / "assets/textures/Brick"))   # a pack folder
    assert _names(textures) == ["wall.png"]
    folders = [textures.dir_model.filePath(textures.dir_model.index(r, 0))
               for r in range(textures.dir_model.rowCount())]
    assert [os.path.basename(f) for f in folders] == ["Brick"]

    assert _names(browser.tab_models) == ["crate.obj"]
    assert _names(browser.tab_audio) == ["beep.wav", "door.ogg"]
    door = next(i for i in browser.tab_audio.items if i.name_text == "door.ogg")
    browser.tab_audio.select_item(door)
    assert browser.tab_audio.selected_speaker_sound() == "assets/sounds/door.ogg"


def test_a_pack_dropped_in_while_the_editor_runs_appears(browser, project):
    tab = browser.tab_textures
    with zipfile.ZipFile(project / "assets/Later.fiopak", "w") as archive:
        archive.writestr("textures/late.png", PNG)
    # What QFileSystemWatcher reports when a file lands in assets/.
    tab._on_asset_directory_changed(str(project / "assets"))
    assert _names(tab) == ["late.png", "loose.png", "top.png"]
    (project / "assets/Later.fiopak").unlink()
    tab._on_asset_directory_changed(str(project / "assets"))
    assert _names(tab) == ["loose.png", "top.png"]
