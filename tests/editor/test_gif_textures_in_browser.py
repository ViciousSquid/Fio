"""GIF textures in the Asset Browser.

A GIF is listed with the textures and its thumbnail is still (the first
frame). Only an animated GIF has the small orange arrow; clicking it plays
the animation on the thumbnail, clicking again stops it on the first frame.
"""

import time

import pytest

pytest.importorskip("PyQt5")
pytest.importorskip("PIL")

from PIL import Image                                         # noqa: E402

from editor.asset_browser import AssetBrowser                # noqa: E402
from tests.engine.test_animated_texture import write_gif      # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def browser(qt_app, tmp_path):
    textures = tmp_path / "assets" / "textures"
    textures.mkdir(parents=True)
    write_gif(textures / "anim.gif", durations=(30, 30, 30))
    write_gif(textures / "still.gif", durations=(100,))
    Image.new("RGB", (8, 8), (9, 9, 9)).save(textures / "brick.png")
    b = AssetBrowser(str(textures))
    yield b
    b.deleteLater()


def _items(browser):
    return {item.name_text: item for item in browser.tab_textures.items}


def _colour(item):
    image = item.thumb_label.pixmap().toImage()
    c = image.pixelColor(image.width() // 2, image.height() // 2)
    return (c.red(), c.green(), c.blue())


def test_only_an_animated_gif_has_the_arrow(browser):
    items = _items(browser)
    assert set(items) == {"anim.gif", "still.gif", "brick.png"}
    assert items["anim.gif"].preview_btn is not None
    assert items["still.gif"].preview_btn is None
    assert items["brick.png"].preview_btn is None
    assert _colour(items["anim.gif"]) == (255, 0, 0)     # still: the first frame


def test_the_arrow_plays_the_animation_on_the_thumbnail_and_stops_it(browser, qt_app):
    item = _items(browser)["anim.gif"]
    item.preview_btn.click()
    assert item.previewing and item.preview_btn.text() == "■"
    seen = set()
    deadline = time.perf_counter() + 2.0
    while len(seen) < 3 and time.perf_counter() < deadline:
        qt_app.processEvents()
        seen.add(_colour(item))
        time.sleep(0.005)
    assert seen == {(255, 0, 0), (0, 255, 0), (0, 0, 255)}

    item.preview_btn.click()
    assert not item.previewing and item.preview_btn.text() == "▶"
    assert _colour(item) == (255, 0, 0)


def test_previewing_does_not_select_the_texture(browser):
    item = _items(browser)["anim.gif"]
    item.preview_btn.click()
    assert browser.tab_textures.selected_item is None
    item.stop_preview()


def test_the_surface_inspector_sizes_a_gif_like_a_png(main_window, tmp_path, monkeypatch):
    from editor.surface_inspector import SurfaceInspector
    textures = tmp_path / "assets" / "textures"
    textures.mkdir(parents=True)
    write_gif(textures / "anim.gif", size=(64, 32))
    monkeypatch.setattr(main_window, "root_dir", str(tmp_path))
    inspector = SurfaceInspector(main_window, main_window)
    try:
        assert inspector._texture_size("anim.gif") == (64, 32)
    finally:
        inspector.deleteLater()


def test_a_texture_tooltip_gives_its_type_and_size(qt_app, tmp_path):
    from editor.asset_browser import texture_info
    textures = tmp_path / "assets" / "textures"
    textures.mkdir(parents=True)
    Image.new("RGB", (512, 256), (1, 2, 3)).save(textures / "wall.png")
    Image.new("RGB", (64, 32), (1, 2, 3)).save(textures / "floor.jpeg")
    write_gif(textures / "anim.gif", size=(16, 8))
    (textures / "broken.png").write_bytes(b"not an image")
    b = AssetBrowser(str(textures))
    try:
        tips = {item.name_text: item.toolTip() for item in b.tab_textures.items}
        assert tips["wall.png"] == "PNG 512x256"
        assert tips["floor.jpeg"] == "JPG 64x32"
        assert tips["anim.gif"] == "GIF 16x8, animated"
        assert tips["broken.png"] == "PNG"
        assert texture_info(str(textures / "wall.png")) == "PNG 512x256"
    finally:
        b.deleteLater()


def test_models_and_sounds_have_no_texture_tooltip(qt_app, tmp_path):
    textures = tmp_path / "assets" / "textures"
    textures.mkdir(parents=True)
    (tmp_path / "assets" / "sounds").mkdir()
    (tmp_path / "assets" / "sounds" / "beep.wav").write_bytes(b"")
    b = AssetBrowser(str(textures))
    try:
        assert [item.toolTip() for item in b.tab_audio.items] == [""]
    finally:
        b.deleteLater()
