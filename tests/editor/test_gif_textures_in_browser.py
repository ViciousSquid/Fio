"""GIF textures in the Asset Browser.

A GIF is listed with the textures and its thumbnail is still (the first
frame). Only an animated GIF wears a play arrow in a circle, centred over
it; clicking the texture selects it and plays the animation on its tile,
clicking again stops it on the first frame.
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


def _colour(browser, item):
    image = browser.tab_textures.thumbnail_pixmap(item).toImage()
    c = image.pixelColor(image.width() // 2, image.height() // 2)
    return (c.red(), c.green(), c.blue())


def _cell(browser, item):
    tab = browser.tab_textures
    return tab.view.visualRect(tab.model.index(tab.model.row_of(item)))


def _click(browser, qt_app, item, where=None):
    """Click the texture's tile (by default, near a corner: anywhere on it
    plays), as a user would."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtTest import QTest
    cell = _cell(browser, item)
    QTest.mouseClick(browser.tab_textures.view.viewport(), Qt.LeftButton,
                     pos=where or cell.topLeft() + cell.center() * 0 + type(cell.topLeft())(6, 6))
    qt_app.processEvents()


@pytest.fixture
def shown(browser, qt_app):
    browser.resize(500, 300)
    browser.show()
    qt_app.processEvents()
    browser.tab_textures.finish_thumbnails()
    yield browser
    browser.tab_textures.stop_previews()
    browser.hide()


def test_only_an_animated_gif_has_the_arrow(shown):
    items = _items(shown)
    assert set(items) == {"anim.gif", "still.gif", "brick.png"}
    assert items["anim.gif"].animated is True
    assert items["still.gif"].animated is False
    assert items["brick.png"].animated is False
    assert _colour(shown, items["anim.gif"]) == (255, 0, 0)     # still: the first frame


def test_the_play_badge_is_centred_over_the_texture(shown, qt_app):
    from PyQt5.QtGui import QColor
    from editor.asset_browser import preview_button_rect
    tab = shown.tab_textures
    item = _items(shown)["anim.gif"]
    cell = _cell(shown, item)
    badge = preview_button_rect(cell)
    assert abs(badge.center().x() - cell.center().x()) <= 1
    assert abs(badge.center().y() - cell.center().y()) <= 1
    assert badge.width() >= 20
    grab = tab.view.viewport().grab().toImage()
    # The orange arrow at the centre, over the red first frame.
    centre = QColor(grab.pixel(badge.center()))
    assert centre.red() > 200 and 90 < centre.green() < 170 and centre.blue() < 60
    # A still GIF has none.
    still = _cell(shown, _items(shown)["still.gif"])
    assert QColor(grab.pixel(still.center())).green() < 60


def test_clicking_plays_the_animation_on_the_thumbnail_and_again_stops_it(shown, qt_app):
    tab = shown.tab_textures
    item = _items(shown)["anim.gif"]
    _click(shown, qt_app, item)
    assert tab.is_previewing(item)
    assert tab.selected_item is item            # clicking a texture selects it
    seen = set()
    deadline = time.perf_counter() + 2.0
    while len(seen) < 3 and time.perf_counter() < deadline:
        qt_app.processEvents()
        seen.add(_colour(shown, item))
        time.sleep(0.005)
    assert seen == {(255, 0, 0), (0, 255, 0), (0, 0, 255)}

    _click(shown, qt_app, item)
    assert not tab.is_previewing(item)
    assert tab.selected_item is item
    assert _colour(shown, item) == (255, 0, 0)


def test_selecting_another_texture_stops_the_preview(shown, qt_app):
    tab = shown.tab_textures
    anim, brick = _items(shown)["anim.gif"], _items(shown)["brick.png"]
    _click(shown, qt_app, anim)
    assert tab.is_previewing(anim)
    _click(shown, qt_app, brick)
    assert tab.selected_item is brick and not tab.is_previewing(anim)
    # A still texture only selects.
    assert not tab.is_previewing(brick)


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
        # The full name first (a small tile's label is cut short), then the
        # type and size.
        tips = {item.name_text: item.tooltip() for item in b.tab_textures.items}
        assert tips["wall.png"] == "wall.png\nPNG 512x256"
        assert tips["floor.jpeg"] == "floor.jpeg\nJPG 64x32"
        assert tips["anim.gif"] == "anim.gif\nGIF 16x8, animated"
        assert tips["broken.png"] == "broken.png\nPNG"
        assert texture_info(str(textures / "wall.png")) == "PNG 512x256"
    finally:
        b.deleteLater()


def test_models_and_sounds_have_only_their_name_as_a_tooltip(qt_app, tmp_path):
    textures = tmp_path / "assets" / "textures"
    textures.mkdir(parents=True)
    (tmp_path / "assets" / "sounds").mkdir()
    (tmp_path / "assets" / "sounds" / "beep.wav").write_bytes(b"")
    b = AssetBrowser(str(textures))
    try:
        assert [item.tooltip() for item in b.tab_audio.items] == ["beep.wav"]
    finally:
        b.deleteLater()
