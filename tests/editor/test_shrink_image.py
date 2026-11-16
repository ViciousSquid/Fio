"""engine.asset_qt.shrink_image: the even box blur the loading overlay and
the pause menu shrink a whole window's picture with.

It must never go through Qt's smooth scaler or format conversion: on a
picture that large Qt 5.15 splits the work across its global thread pool and
waits for it, and on Windows ARM64 that wait sometimes never ended (CI runs
hung raising the loading overlay for Play and for ``map``).
"""

import numpy as np
import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtGui import QImage, QPixmap                               # noqa: E402

from engine.asset_qt import shrink_image                             # noqa: E402

pytestmark = pytest.mark.qt


def _image(pixels, fmt=QImage.Format_ARGB32_Premultiplied):
    h, w = pixels.shape[:2]
    data = np.ascontiguousarray(pixels.astype(np.uint8))
    return QImage(data.data, w, h, w * 4, fmt).copy()


def _array(image):
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    rows = np.frombuffer(bits, np.uint8).reshape(image.height(), image.bytesPerLine())
    return rows[:, :image.width() * 4].reshape(image.height(), image.width(), 4).copy()


def test_each_pixel_is_the_mean_of_its_block():
    rng = np.random.default_rng(3)
    pixels = rng.integers(0, 256, (7, 11, 4))               # sizes not multiples of 3
    pixels[..., 3] = 255
    out = shrink_image(_image(pixels), 3)
    assert (out.width(), out.height()) == (3, 2)             # the ragged edge is dropped
    exact = pixels[:6, :9].reshape(2, 3, 3, 3, 4).mean(axis=(1, 3))
    # Pillow divides in fixed point: within rounding of the exact mean.
    assert np.abs(_array(out).astype(float) - exact).max() <= 0.6
    assert out.format() == QImage.Format_ARGB32_Premultiplied


@pytest.mark.parametrize("fmt", [QImage.Format_RGB32, QImage.Format_ARGB32,
                                 QImage.Format_RGBA8888, QImage.Format_ARGB32_Premultiplied])
def test_a_flat_picture_stays_flat_in_every_32_bit_format(fmt):
    pixels = np.empty((12, 12, 4), np.uint8)
    pixels[:] = (40, 120, 200, 255)
    out = shrink_image(_image(pixels, fmt), 4)
    assert out.format() == fmt and (out.width(), out.height()) == (3, 3)
    assert (_array(out) == (40, 120, 200, 255)).all()


def test_pixmaps_null_images_and_tiny_pictures(qt_app):
    pixels = np.full((10, 20, 4), 255, np.uint8)
    out = shrink_image(QPixmap.fromImage(_image(pixels)), 5)
    assert (out.width(), out.height()) == (4, 2)
    assert shrink_image(QImage(), 3).isNull()
    tiny = shrink_image(_image(np.full((2, 2, 4), 9, np.uint8)), 16)
    assert (tiny.width(), tiny.height()) == (1, 1) and (_array(tiny) == 9).all()


def test_a_whole_window_is_shrunk_without_qt_scaling_or_converting(monkeypatch):
    pixels = np.zeros((1080, 1920, 4), np.uint8)
    pixels[:, 960:] = 255                                    # a hard edge to soften
    image = _image(pixels)

    def forbidden(*args, **kwargs):
        raise AssertionError("Qt's threaded scaler or converter was used")
    for name in ("scaled", "smoothScaled", "convertToFormat", "scaledToWidth",
                 "scaledToHeight"):
        monkeypatch.setattr(QImage, name, forbidden, raising=False)
    out = shrink_image(image, 3)
    assert (out.width(), out.height()) == (640, 360)
    row = _array(out)[180, :, 0].astype(int)
    assert row[0] == 0 and row[-1] == 255 and row[320] == 255   # 960 / 3 = 320 exactly
