"""Draw the Fio installer window's background: an arrow and one instruction.

Writes <out>.png (1x) and <out>@2x.png; the macOS job combines them into one
Retina-aware TIFF with `tiffutil -cathidpicheck`.

    python .github/macos-dmg/make_background.py build/dmg-background
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

WINDOW = (660, 400)        # dmg_settings.window_rect size
APP_AT = (165, 175)        # icon centres, as in dmg_settings.icon_locations
APPS_AT = (495, 175)
ICON = 128

BACKGROUND = (242, 242, 244)
INK = (70, 72, 78)
ACCENT = (240, 128, 0)     # Fio's orange

_FONTS = (
    '/System/Library/Fonts/SFNS.ttf',
    '/System/Library/Fonts/Helvetica.ttc',
    '/Library/Fonts/Arial.ttf',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
)


def _font(size):
    for path in _FONTS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def draw(scale):
    w, h = WINDOW[0] * scale, WINDOW[1] * scale
    img = Image.new('RGB', (w, h), BACKGROUND)
    d = ImageDraw.Draw(img)

    # Arrow from the app's right edge to the Applications icon's left edge.
    y = APP_AT[1] * scale
    x0 = (APP_AT[0] + ICON // 2 + 22) * scale
    x1 = (APPS_AT[0] - ICON // 2 - 22) * scale
    head = 18 * scale
    d.line([(x0, y), (x1 - head, y)], fill=ACCENT, width=6 * scale)
    d.polygon([(x1, y), (x1 - head, y - head * 0.7), (x1 - head, y + head * 0.7)],
              fill=ACCENT)

    text = 'Drag Fio onto Applications to install'
    font = _font(17 * scale)
    tw = d.textlength(text, font=font)
    d.text(((w - tw) / 2, 315 * scale), text, fill=INK, font=font)
    return img


def main(out):
    draw(1).save(out + '.png')
    draw(2).save(out + '@2x.png')


if __name__ == '__main__':
    main(sys.argv[1])
