"""An entity's authored ``sprite_path`` reads only project assets.

The 2D view asks ``Thing._pixmap_for_path`` for the icon a map names. It used
to take an absolute path as it came, so a map could make the editor open any
image file (or, on Windows, contact ``\\\\host\\share``).
"""

import os

import pytest

pytest.importorskip("PyQt5", reason="editor.things needs Qt")

from PyQt5.QtWidgets import QApplication                      # noqa: E402

from editor import things                                     # noqa: E402
from engine import asset_paths                                # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture(scope="module")
def qt_app():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def thing(qt_app, monkeypatch):
    loaded = []
    real = things.load_pixmap

    def spy(path):
        loaded.append(path)
        return real(path)

    monkeypatch.setattr(things, "load_pixmap", spy)
    monkeypatch.setattr(asset_paths, "_warned", set())
    entity = things.Thing.__new__(things.Thing)
    monkeypatch.setattr(things.Thing, "_pixmap_cache", {}, raising=False)
    return entity, loaded


def test_a_sprite_path_outside_the_project_is_not_opened(thing, tmp_path, capsys):
    from PyQt5.QtGui import QPixmap
    entity, loaded = thing
    secret = tmp_path / "secret.png"
    pixmap = QPixmap(4, 4)
    pixmap.fill()
    assert pixmap.save(str(secret)), "could not write the probe image"
    for path in (str(secret), "../" * 12 + str(secret).lstrip("/"),
                 "\\\\evil-host\\share\\x.png", "//evil-host/share/x.png", "C:\\x.png",
                 "assets/../settings.ini", "maps/DevTest.json"):
        assert entity._pixmap_for_path(path) is None, f"sprite_path {path!r} produced a pixmap"
    assert loaded == [], f"files handed to the image loader: {loaded}"
    assert "Refused sprite path" in capsys.readouterr().out


@pytest.mark.parametrize("path", [
    "assets/sprites/pickup.png",
    "assets/sprites/monsters/monster_flesh_eye.png",
    "plugins/bigworld/assets/bigworldsettings.png",
])
def test_sprite_paths_used_by_shipped_maps_still_load(thing, path):
    entity, loaded = thing
    pixmap = entity._pixmap_for_path(path)
    assert pixmap is not None and not pixmap.isNull(), f"{path} no longer loads"
    assert len(loaded) == 1 and loaded[0].replace("\\", "/").endswith(path)
