"""The HUD draws its strings from text laid out once (ViewHud._text).

Counts, prompts and the FPS counter seldom change, yet were shaped again on
every frame. _text keeps a QStaticText per (string, font) and draws it where
drawText would have put the baseline: these hold that to drawText's pixels
and keep the cache bounded.
"""

import pytest

pytestmark = pytest.mark.qt

pytest.importorskip("PyQt5")


def _hud():
    from engine.view_hud import ViewHud
    return ViewHud(view=None)


def _image():
    from PyQt5.QtGui import QColor, QImage
    image = QImage(240, 80, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(20, 30, 40))
    return image


@pytest.mark.parametrize("size,text", [(10, "FPS: 59"), (36, "100"), (14, "[E] Open")])
def test_static_text_draws_what_drawtext_draws(qt_app, size, text):
    from PyQt5.QtGui import QColor, QFont, QPainter
    font = QFont("Fio UI", size)
    a, b = _image(), _image()
    for image, cached in ((a, False), (b, True)):
        painter = QPainter(image)
        painter.setFont(font)
        painter.setPen(QColor(255, 200, 0))
        if cached:
            hud = _hud()
            hud._text(painter, 12, 50, text)
            hud._text(painter, 12, 50, text)       # from the cache this time
        else:
            painter.drawText(12, 50, text)
            painter.drawText(12, 50, text)
        painter.end()
    assert a == b


def test_the_cache_is_bounded(qt_app, monkeypatch):
    from PyQt5.QtGui import QFont, QPainter
    from engine.view_hud import ViewHud
    monkeypatch.setattr(ViewHud, "STATIC_TEXT_LIMIT", 8)
    hud = _hud()
    image = _image()
    painter = QPainter(image)
    painter.setFont(QFont("Fio UI", 10))
    for fps in range(40):
        hud._text(painter, 0, 20, "FPS: %d" % fps)
    painter.end()
    assert len(hud._static_texts) == 8
