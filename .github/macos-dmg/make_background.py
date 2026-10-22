"""Draw the Fio installer window's background: an arrow and one instruction.

Finder draws the icon labels ("Fio", "Applications") itself, in black in
Light mode and white in Dark mode, over this one static image -- a disk
image's window has no per-appearance background.  So the background is the
grey on which black and white text are equally legible (luminance ~0.18,
about 4.6:1 for both, past WCAG AA's 4.5:1), and :func:`check_contrast`
fails the build if a change ever loses that under the labels.

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

# Luminance 0.18: black text 4.6:1, white text 4.6:1.
BACKGROUND = (116, 118, 121)
INK = (255, 255, 255)
INK_SHADOW = (60, 62, 66)
ACCENT = (240, 128, 0)     # Fio's orange

#: Contrast both label colours must keep against the background (WCAG AA).
MIN_CONTRAST = 4.5

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
    head = 20 * scale
    shaft = 4 * scale
    outline = [(x0, y - shaft), (x1 - head, y - shaft), (x1 - head, y - head * 0.75),
               (x1, y), (x1 - head, y + head * 0.75), (x1 - head, y + shaft),
               (x0, y + shaft)]
    # The white edge carries the shape (4.6:1 on this grey); orange alone is
    # too close to it in lightness.
    d.polygon(outline, fill=ACCENT, outline=INK, width=2 * scale)

    text = 'Drag Fio onto Applications to install'
    font = _font(18 * scale)
    tw = d.textlength(text, font=font)
    x, y = (w - tw) / 2, 318 * scale
    d.text((x, y + scale), text, fill=INK_SHADOW, font=font)
    d.text((x, y), text, fill=INK, font=font)
    return img


def _luminance(rgb):
    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def check_contrast(img, scale):
    """Fail unless black and white labels both read where Finder puts them."""
    for cx, cy in (APP_AT, APPS_AT):
        # Labels sit just under the icon, roughly icon-wide.
        box = ((cx - ICON // 2) * scale, (cy + ICON // 2 + 4) * scale,
               (cx + ICON // 2) * scale, (cy + ICON // 2 + 26) * scale)
        for colour in set(img.crop(box).getdata()):
            for label in ((0, 0, 0), (255, 255, 255)):
                ratio = contrast(colour, label)
                if ratio < MIN_CONTRAST:
                    raise SystemExit(
                        f'label {label} on {colour}: contrast {ratio:.2f} < '
                        f'{MIN_CONTRAST} -- unreadable in one appearance')


def main(out):
    for scale, suffix in ((1, ''), (2, '@2x')):
        img = draw(scale)
        check_contrast(img, scale)
        img.save(out + suffix + '.png')


if __name__ == '__main__':
    main(sys.argv[1])
