"""Text on a decal: the words a ``Decal``'s ``text`` property names, drawn
into its layer of the decal texture array.

A decal with text paints its image (``texture``, if it has one) with the
text drawn over it, centred, in its ``font`` and ``text_colour``. Each
different text -- image, words, font, colour and the box's proportions -- is
a layer of its own, listed by
``engine.decal_textures`` after the image files and drawn once, when it
first appears (the map loads, the property is edited, an IO input changes
it); nothing is drawn per frame and the decal shader is unchanged. The text
is laid out at the box's proportions, so it is not stretched when the layer
is drawn across the box.

Fonts are Fio's own -- Fio UI (the default) and the HUD's fonts -- listed by
name for the property editor's dropdown; any other ``.ttf`` / ``.otf`` put
in ``assets/fonts`` is listed by its own name. The text is drawn with
Pillow, which the desktop renderer and the Android player both have, so a
sign reads the same on both.

The words are black or white. Image decals tint the surface rather than
cover it (``decal.frag``: mid grey leaves it alone), keeping its own light
and shadow: black lettering is black, and white doubles the surface's
brightness -- white on a mid-tone wall, a lighter tone of a dark one.
"""

from __future__ import annotations

import io
import json
import os
import threading

import numpy as np

#: The font a decal's text is drawn in unless it names another.
DEFAULT_FONT = 'Fio UI'

#: Folder the font files are read from (an asset path).
FONT_FOLDER = os.path.join('assets', 'fonts')

#: The fonts the property editor lists first, by name -> the font file under
#: ``assets/fonts``. Fio UI first: the default.
FONTS = {
    'Fio UI': 'editor/FioUI-Regular.ttf',
    'Fio UI Bold': 'editor/FioUI-Bold.ttf',
    'Rushford Clean': 'Rushfordclean-rgz89.otf',
    'O.K. Retro': 'O.K.Retro.otf',
    'Hornet Display': 'HornetDisplay-Regular.ttf',
    'LCD AT&T Phone Time/Date': 'LCDAT&TPhoneTimeDate.ttf',
}

#: Fonts drawn heavier than they are: each stroke is widened on both sides
#: by this fraction of the font's size. Hornet Display's hairline strokes
#: vanish on a wall at any distance; widened, they read like painted lines.
THICKEN = {'Hornet Display': 0.035}

#: The colours a decal's words can be, by name (the property editor's
#: dropdown, the SetTextColor input), and the first: the default.
TEXT_COLOURS = {'Black': (0, 0, 0, 255), 'White': (255, 255, 255, 255)}
DEFAULT_COLOUR = 'Black'

#: How big the words are drawn, as a fraction of the most the box allows
#: (the property editor's Text Size slider: 10 to 100 %).
MIN_TEXT_SCALE, DEFAULT_TEXT_SCALE = 0.1, 1.0
#: How far the words may be moved from the box's centre, as a fraction of
#: its width (x, to the right) and height (y, up): to sit them inside the
#: panel of a plate rather than across its trim.
MAX_TEXT_OFFSET = 0.5

#: A text layer's name in ``engine.decal_textures``: this, then the JSON of
#: ``[image, text, font, colour, scale, x, y, aspect]``. No image file can
#: be named so (they are image files, and these end in ``]``).
PREFIX = 'text:'
#: A text layer no sign's words use now (engine.decal_textures keeps it for
#: the next new words): blank.
FREE = PREFIX

#: The words are drawn on a canvas this many times the layer's side along
#: its longer side (at the box's proportions), then scaled to the layer:
#: smooth edges at any size.
SUPERSAMPLE = 4
#: How much of the box the text may fill, across and up, at full size.
FILL_ACROSS, FILL_UP = 0.90, 0.70

_lock = threading.Lock()
_found = {'where': None, 'fonts': {}}
_font_data = {}
#: Decoded images under signs, scaled to the layer, by (project, image,
#: size): new words on the same plate do not decode it again.
_plates = {}
_PLATES_KEPT = 16


def is_entry(name):
    """Whether *name* in the decal list is a text layer, not an image file."""
    return isinstance(name, str) and name.startswith(PREFIX)


def _number(value, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default


def entry(props):
    """The text layer a decal with *props* paints, or None when it has no
    text. Two decals with the same image, words, font, colour and
    proportions share one layer."""
    text = props.get('text')
    if not isinstance(text, str) or not text.strip():
        return None
    texture = os.path.basename(str(props.get('texture') or '').replace('\\', '/'))
    width = _number(props.get('width'), 128.0)
    height = _number(props.get('height'), width)
    aspect = width / height if width > 0.0 and height > 0.0 else 1.0
    aspect = round(min(max(aspect, 1.0 / 16.0), 16.0), 2)
    return PREFIX + json.dumps([texture, text, font_name(props.get('font')),
                                text_colour(props.get('text_colour')),
                                text_scale(props.get('text_scale')),
                                text_offset(props.get('text_offset_x')),
                                text_offset(props.get('text_offset_y')), aspect],
                               ensure_ascii=False, separators=(',', ':'))


def text_offset(value):
    """*value* as a Text Offset: -0.5 to 0.5 of the box, in hundredths;
    centred when it is not a number."""
    offset = _number(value, 0.0)
    return round(min(max(offset, -MAX_TEXT_OFFSET), MAX_TEXT_OFFSET), 2)


def text_scale(value):
    """*value* as a Text Size: 0.1 to 1.0 in hundredths; full size when it
    is not a number."""
    scale = _number(value, DEFAULT_TEXT_SCALE)
    return round(min(max(scale, MIN_TEXT_SCALE), 1.0), 2)


def text_colour(name):
    """*name* as one of :data:`TEXT_COLOURS` (any case); black when it
    names neither."""
    if isinstance(name, str):
        folded = name.strip().casefold()
        for candidate in TEXT_COLOURS:
            if candidate.casefold() == folded:
                return candidate
    return DEFAULT_COLOUR


# -- fonts ---------------------------------------------------------------------

def _where():
    from engine import decal_textures
    return decal_textures._where()


def _read_font(path):
    """The bytes of font file *path* (under ``assets/fonts``), or None."""
    from engine import decal_textures
    source = decal_textures.current_source()
    if source is not None:
        return source.read_font(path)
    from engine.asset_archives import asset_source
    try:
        found = asset_source(os.path.join(FONT_FOLDER, *path.split('/')))
        if hasattr(found, 'read'):
            return found.read()
        with open(found, 'rb') as handle:
            return handle.read()
    except (OSError, TypeError, ValueError):
        return None


def _font_files():
    """Every font file the project (or the played package) has, as paths
    under ``assets/fonts``."""
    from engine import decal_textures
    source = decal_textures.current_source()
    if source is not None:
        return source.font_files()
    from engine.asset_archives import asset_listdir
    return [n for n in asset_listdir(FONT_FOLDER) if n.lower().endswith(('.ttf', '.otf'))]


def _other_fonts():
    """``{name: file}`` for the font files that are not in :data:`FONTS`,
    each named as its file names itself -- read once per project."""
    where = _where()
    with _lock:
        if _found['where'] == where:
            return _found['fonts']
    from PIL import ImageFont
    known = set(FONTS.values())
    fonts = {}
    for path in _font_files():
        if path in known or path.startswith('editor/'):
            continue
        data = _font_data_for(path)
        if data is None:
            continue
        try:
            family, style = ImageFont.truetype(io.BytesIO(data), 12).getname()
        except Exception:
            continue
        name = family if style in (None, '', 'Regular') else f'{family} {style}'
        if name and name not in FONTS:
            fonts.setdefault(name, path)
    with _lock:
        _found['where'], _found['fonts'] = where, fonts
    return fonts


def font_names():
    """The fonts a decal's text can be drawn in, by name: Fio UI first, the
    rest of :data:`FONTS`, then any other font in ``assets/fonts``."""
    return list(FONTS) + sorted(_other_fonts(), key=str.casefold)


def font_name(name):
    """*name* as one of :func:`font_names` (any case); the default when it
    names none of them."""
    if isinstance(name, str):
        name = name.strip()
        if name in FONTS:
            return name
        others = _other_fonts()
        if name in others:
            return name
        folded = name.casefold()
        for candidate in list(FONTS) + list(others):
            if candidate.casefold() == folded:
                return candidate
    return DEFAULT_FONT


def font_file(name):
    """The file (under ``assets/fonts``) font *name* is drawn from."""
    name = font_name(name)
    return FONTS.get(name) or _other_fonts().get(name) or FONTS[DEFAULT_FONT]


def _font_data_for(path):
    key = (_where(), path)
    with _lock:
        if key in _font_data:
            return _font_data[key]
    data = _read_font(path)
    with _lock:
        _font_data[key] = data
    return data


def _load_font(name, size):
    """Font *name* at *size* pixels: its file, else Fio UI's, else Pillow's
    own (a package that did not bring the font still shows the words)."""
    from PIL import ImageFont
    for path in (font_file(name), FONTS[DEFAULT_FONT]):
        data = _font_data_for(path)
        if data is not None:
            try:
                return ImageFont.truetype(io.BytesIO(data), size)
            except Exception:
                continue
    return ImageFont.load_default(size=size)


# -- drawing -------------------------------------------------------------------

def _plate(texture, size):
    """Image *texture* scaled to the layer, as RGBA bytes -- as an image
    decal's own layer is -- or None when it cannot be read."""
    from PIL import Image
    from engine import decal_textures
    key = (_where(), texture, size)
    with _lock:
        if key in _plates:
            return _plates[key]
    plate = None
    raw = decal_textures.read_image(texture)
    try:
        with Image.open(io.BytesIO(raw)) as image:
            plate = np.asarray(image.convert('RGBA').resize((size, size), Image.LANCZOS),
                               dtype=np.uint8)
    except Exception as exc:
        print(f"[Decal] text decal image '{texture}' not loaded ({exc}): "
              f"drawing its text alone")
    with _lock:
        if len(_plates) >= _PLATES_KEPT:
            _plates.pop(next(iter(_plates)))
        _plates[key] = plate
    return plate


def layer_pixels(name, size):
    """Text layer *name* (see :func:`entry`) as ``(size, size, 4)`` RGBA
    bytes: the decal's image, the text drawn over it -- laid out at the
    box's proportions, then scaled to the layer."""
    from PIL import Image, ImageDraw
    if name == FREE:
        return np.zeros((size, size, 4), dtype=np.uint8)
    texture, text, font, colour, scale, across_by, up_by, aspect = json.loads(
        name[len(PREFIX):])
    ink_colour = TEXT_COLOURS.get(colour, TEXT_COLOURS[DEFAULT_COLOUR])
    aspect = float(aspect)
    side = size * SUPERSAMPLE
    if aspect >= 1.0:
        width, height = side, max(16, round(side / aspect))
    else:
        width, height = max(16, round(side * aspect)), side
    plate = _plate(texture, size) if texture else None
    layer = Image.fromarray(plate) if plate is not None else \
        Image.new('RGBA', (size, size), (0, 0, 0, 0))
    words = text.replace('\\n', '\n').strip('\n')
    if words.strip():
        ink = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(ink)
        weight = THICKEN.get(font, 0.0)

        def measure(px):
            face = _load_font(font, px)
            stroke = round(px * weight)
            box = draw.multiline_textbbox((0, 0), words, font=face, align='center',
                                          spacing=round(px * 0.2), stroke_width=stroke)
            return face, stroke, box

        # Text grows with its size: measure once, scale to fit (times the
        # Text Size), then make sure rounding left it inside.
        across, up = width * FILL_ACROSS * scale, height * FILL_UP * scale
        _face, _stroke, box = measure(100)
        fit = min(across / max(box[2] - box[0], 1), up / max(box[3] - box[1], 1))
        px = max(4, int(100 * fit))
        face, stroke, box = measure(px)
        while px > 4 and (box[2] - box[0] > across or box[3] - box[1] > up):
            px -= max(1, px // 20)
            face, stroke, box = measure(px)
        # Centred, then moved by the Text Offset (right, and up).
        x = (width - (box[2] - box[0])) / 2.0 - box[0] + across_by * width
        y = (height - (box[3] - box[1])) / 2.0 - box[1] - up_by * height
        draw.multiline_text((x, y), words, font=face, fill=ink_colour, align='center',
                            spacing=round(px * 0.2), stroke_width=stroke,
                            stroke_fill=ink_colour)
        # Only the words are scaled down (a box filter: smooth, and quick);
        # the image under them is the layer an image decal of it would be.
        layer = Image.alpha_composite(layer, ink.resize((size, size), Image.BOX))
    return np.asarray(layer, dtype=np.uint8)
