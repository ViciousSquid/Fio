"""The glasses a player wears in the world.

Players are drawn as a glasses billboard (split-screen and portal views). The
styles live together in ``assets/sprites/glasses/``, all on the same 300x128
transparent canvas so any of them fills the billboard the same way. Player 1
chooses theirs in Settings > Appearance (settings.ini ``[Appearance]
glasses``); player 2 always wears the default.
"""

#: Folder under ``assets/`` holding every style, for ``load_texture``.
GLASSES_SUBFOLDER = 'sprites/glasses'

#: ``(style id, label, file name)`` in the order the Appearance tab lists them.
GLASSES_STYLES = (
    ('classic', "Classic", 'classic.png'),
    ('cateye_pink', "Pink Cat-Eye", 'cateye_pink.png'),
    ('pixel_shades', "Pixel Shades", 'pixel_shades.png'),
    ('sunnies_yellow', "Yellow Sunnies", 'sunnies_yellow.png'),
    ('sunnies_pink', "Pink Sunnies", 'sunnies_pink.png'),
    ('sunnies_green', "Green Sunnies", 'sunnies_green.png'),
)

DEFAULT_GLASSES = 'classic'

#: Sprite-texture key of the default style (what player 2 wears).
DEFAULT_SPRITE_KEY = 'Glasses'

_FILES = {style: fname for style, _label, fname in GLASSES_STYLES}


def normalize_glasses(style):
    """*style* if it is a known glasses style, else the default."""
    style = str(style or '').strip().lower()
    return style if style in _FILES else DEFAULT_GLASSES


def glasses_file(style):
    """File name (inside :data:`GLASSES_SUBFOLDER`) of *style*."""
    return _FILES[normalize_glasses(style)]


def glasses_sprite_key(style):
    """The ``sprite_textures`` key a style's texture is loaded under."""
    style = normalize_glasses(style)
    return DEFAULT_SPRITE_KEY if style == DEFAULT_GLASSES else f'Glasses:{style}'


def glasses_path(style):
    """Path of *style*'s image relative to the project root."""
    return f'assets/{GLASSES_SUBFOLDER}/{glasses_file(style)}'
