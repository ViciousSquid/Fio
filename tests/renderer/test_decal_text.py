"""Text on decals (engine.decal_text), without GL.

A ``Decal`` with ``text`` paints a layer of its own: its image with the
words drawn on, in its ``font`` (Fio UI unless it names another). These
check the layer's name (what makes two signs share one), the fonts the
property editor lists, how the words are drawn, where text layers go in
the decal list, and that an exported map carries the fonts its signs use.
(The Decal entity's own defaults need Qt: tests/editor/test_decal_text_panel.py.)
"""

import io

import numpy as np
import pytest

from engine import decal_text, decal_textures


def _sign(**props):
    base = dict(texture='', text='EXIT', font='Fio UI', width=320.0, height=80.0)
    base.update(props)
    return base


@pytest.fixture
def clean_texts():
    decal_textures.use_texts(())
    yield
    decal_textures.use_texts(())


def test_a_decal_without_words_paints_no_text_layer():
    assert decal_text.entry(_sign(text='')) is None
    assert decal_text.entry(_sign(text='   ')) is None
    assert decal_text.entry({'texture': 'grime_01.png'}) is None


def test_two_signs_alike_share_a_layer_and_any_difference_makes_another():
    sign = decal_text.entry(_sign())
    assert decal_text.is_entry(sign)
    assert decal_text.entry(_sign()) == sign
    assert decal_text.entry(_sign(texture='assets/textures/decals/x.png')) == \
        decal_text.entry(_sign(texture='x.png'))                 # by the image's name
    for change in (dict(text='ENTRY'), dict(font='Hornet Display'), dict(texture='x.png'),
                   dict(text_colour='White'), dict(width=160.0)):     # 2:1, not 4:1
        assert decal_text.entry(_sign(**change)) != sign, change
    # The box's size alone is not a difference: only its proportions are.
    assert decal_text.entry(_sign(width=640.0, height=160.0)) == sign


def test_fio_ui_is_the_default_font_and_the_dropdown_lists_the_hud_fonts():
    names = decal_text.font_names()
    assert names[0] == 'Fio UI' == decal_text.DEFAULT_FONT
    for name in ('Fio UI Bold', 'Rushford Clean', 'O.K. Retro', 'Hornet Display',
                 'LCD AT&T Phone Time/Date'):
        assert name in names
    assert decal_text.font_name('hornet display') == 'Hornet Display'   # any case
    assert decal_text.font_name('No Such Font') == 'Fio UI'
    assert decal_text.font_name(None) == 'Fio UI'
    assert decal_text.entry(_sign(font='No Such Font')) == decal_text.entry(_sign())
    assert decal_text.font_file('Fio UI') == 'editor/FioUI-Regular.ttf'


def test_words_are_black_unless_white_is_chosen():
    assert decal_text.text_colour(None) == 'Black' == decal_text.DEFAULT_COLOUR
    assert decal_text.text_colour(' white ') == 'White'
    assert decal_text.text_colour('purple') == 'Black'
    assert list(decal_text.TEXT_COLOURS) == ['Black', 'White']
    assert decal_text.entry(_sign(text_colour='WHITE')) == decal_text.entry(_sign(text_colour='White'))
    assert decal_text.entry(_sign(text_colour='purple')) == decal_text.entry(_sign())
    white = decal_text.layer_pixels(decal_text.entry(_sign(text_colour='White')), 256)
    inked = white[..., 3] > 128
    assert inked.sum() > 200 and white[inked][:, :3].min() >= 247


def test_text_size_shrinks_the_words_to_fit_inside_the_decal():
    assert decal_text.text_scale(None) == 1.0
    assert decal_text.text_scale(0.333) == 0.33
    assert decal_text.text_scale(0.0) == 0.1 and decal_text.text_scale(5) == 1.0
    assert decal_text.text_scale('big') == 1.0
    assert decal_text.entry(_sign(text_scale=0.5)) != decal_text.entry(_sign())

    def extent(scale):
        layer = decal_text.layer_pixels(decal_text.entry(_sign(text_scale=scale)), 256)
        rows, cols = np.nonzero(layer[..., 3] > 128)
        return rows.max() - rows.min(), cols.max() - cols.min(), \
            (rows.min() + rows.max()) / 2, (cols.min() + cols.max()) / 2
    full, half = extent(1.0), extent(0.5)
    assert half[0] == pytest.approx(full[0] / 2, rel=0.12)          # half as tall
    assert half[1] == pytest.approx(full[1] / 2, rel=0.12)          # half as wide
    assert half[2:] == pytest.approx(full[2:], abs=3)               # still centred


def test_text_offset_moves_the_words_right_and_up_inside_the_decal():
    assert decal_text.text_offset(None) == 0.0
    assert decal_text.text_offset(-0.126) == -0.13
    assert decal_text.text_offset(3) == 0.5 and decal_text.text_offset('x') == 0.0
    assert decal_text.entry(_sign(text_offset_y=0.1)) != decal_text.entry(_sign())

    def centre(**offset):
        layer = decal_text.layer_pixels(
            decal_text.entry(_sign(text='LEVEL B', text_scale=0.5, **offset)), 256)
        rows, cols = np.nonzero(layer[..., 3] > 128)
        return (rows.min() + rows.max()) / 2, (cols.min() + cols.max()) / 2
    row, col = centre()
    assert (row, col) == pytest.approx((127.5, 127.5), abs=3)        # centred
    assert centre(text_offset_y=0.2) == pytest.approx((row - 0.2 * 256, col), abs=2)
    assert centre(text_offset_x=-0.15) == pytest.approx((row, col - 0.15 * 256), abs=2)


def test_jpeg_images_are_decal_images_too():
    names = decal_textures.refresh()[1]
    assert 'DecalLong01.jpg' in names and 'Decal02.jpg' in names
    assert decal_textures.layer_of('assets/textures/decals/DecalLong01.jpg', names) >= 0
    source = decal_textures.DecalSource({'a.png': bytes, 'b.JPG': bytes, 'c.jpeg': bytes,
                                         'notes.txt': bytes})
    assert source.names() == ('a.png', 'b.JPG', 'c.jpeg')


def test_every_listed_font_draws_the_words():
    for name in decal_text.font_names():
        layer = decal_text.layer_pixels(decal_text.entry(_sign(font=name)), 128)
        assert layer.shape == (128, 128, 4) and layer.dtype == np.uint8
        assert (layer[..., 3] > 128).sum() > 200, name            # ink


def _ink(layer):
    return int((layer[..., 3] > 128).sum())


def test_words_alone_are_black_ink_on_nothing():
    layer = decal_text.layer_pixels(decal_text.entry(_sign()), 256)
    inked = layer[..., 3] > 128
    assert inked.any()
    assert layer[inked][:, :3].max() <= 8                       # black
    assert layer[0, 0, 3] == 0 and layer[-1, -1, 3] == 0         # the rest: nothing
    # Laid out at the box's 4:1, then scaled to the square layer: spread back
    # across the 320 x 80 box, the words have their own shape, not stretched.
    from PIL import ImageFont
    from tests.helpers.paths import repo_path
    rows, cols = np.nonzero(inked)
    shown = ((cols.max() - cols.min()) / 256 * 320) / ((rows.max() - rows.min()) / 256 * 80)
    left, top, right, bottom = ImageFont.truetype(
        repo_path('assets', 'fonts', 'editor', 'FioUI-Regular.ttf'), 400).getbbox('EXIT')
    assert shown == pytest.approx((right - left) / (bottom - top), rel=0.08)
    assert rows.max() - rows.min() > 256 * 0.6      # a short word: as tall as it may be


def test_a_new_line_is_typed_as_backslash_n():
    one = decal_text.layer_pixels(decal_text.entry(_sign(text='AB', height=160.0)), 256)
    two = decal_text.layer_pixels(decal_text.entry(_sign(text=r'A\nB', height=160.0)), 256)
    rows_one = np.nonzero((one[..., 3] > 128).any(axis=1))[0]
    rows_two = np.nonzero((two[..., 3] > 128).any(axis=1))[0]
    # Two lines: a gap between them (rows with no ink inside the block).
    gap = set(range(rows_two.min(), rows_two.max())) - set(rows_two)
    assert gap and not set(range(rows_one.min(), rows_one.max())) - set(rows_one)


def test_hornet_display_is_drawn_heavier_than_its_hairline(monkeypatch):
    sign = decal_text.entry(_sign(text='MAINTENANCE', font='Hornet Display'))
    heavy = _ink(decal_text.layer_pixels(sign, 256))
    monkeypatch.setattr(decal_text, 'THICKEN', {})
    hairline = _ink(decal_text.layer_pixels(sign, 256))
    assert heavy > 1.6 * hairline, (heavy, hairline)


def test_words_go_over_the_decals_image(monkeypatch):
    from PIL import Image
    grey = Image.new('RGBA', (64, 64), (128, 128, 128, 255))
    data = io.BytesIO()
    grey.save(data, format='PNG')
    monkeypatch.setattr(decal_textures, 'read_image',
                        lambda name: data.getvalue() if name == 'plate.png' else None)
    layer = decal_text.layer_pixels(decal_text.entry(_sign(texture='plate.png')), 128)
    assert (layer[..., 3] == 255).all()                          # the image, opaque
    assert (layer[..., 0] < 20).sum() > 100                      # the words on it
    assert abs(int(layer[0, 0, 0]) - 128) <= 2                   # the plate elsewhere


def test_text_layers_come_after_the_images_which_keep_their_layers(clean_texts):
    version, before = decal_textures.images()
    files = [n for n in before if not decal_text.is_entry(n)]
    assert files == list(before)
    signs = [decal_text.entry(_sign(text=t)) for t in ('B', 'A', 'B')]
    version2, names = decal_textures.use_texts(signs)
    assert version2 != version
    assert list(names[:len(files)]) == files
    assert sorted(names[len(files):]) == sorted(set(signs))
    assert decal_textures.images() == (version2, names)
    assert decal_textures.use_texts(reversed(signs))[0] == version2   # same set: no change
    assert decal_textures.use_texts(())[1] == tuple(files)


def test_new_words_take_the_layer_their_old_words_left_and_nothing_else_moves(clean_texts):
    files = len(decal_textures.images()[1])
    a, b, c, d = (decal_text.entry(_sign(text=t)) for t in 'ABCD')
    layout = decal_textures.layout()
    names = decal_textures.use_texts([a, b, c])[1]
    assert names[files:] == (a, b, c)
    names = decal_textures.use_texts([a, d, c])[1]           # B's sign now says D
    assert names[files:] == (a, d, c)                        # in B's layer; A, C stay
    names = decal_textures.use_texts([a, c])[1]              # D's words cleared
    assert names[files:] == (a, decal_text.FREE, c)          # its layer: blank, kept
    assert decal_text.layer_pixels(decal_text.FREE, 32).max() == 0
    names = decal_textures.use_texts([a, c, b])[1]
    assert names[files:] == (a, b, c)
    assert decal_textures.layout() == layout                 # no sign's layer moved
    # Many freed layers are packed away -- and then the layout changes.
    many = [decal_text.entry(_sign(text=str(i))) for i in range(20)]
    decal_textures.use_texts(many)
    names = decal_textures.use_texts(many[:3])[1]
    assert names[files:] == tuple(sorted(many[:3])) and decal_textures.layout() != layout


def test_blood_never_picks_a_signs_layer():
    names = ('grime_01.png', 'scorch_01.png', decal_text.entry(_sign()))
    assert decal_textures.blood_ranges(names) == (0.0, 2.0, 0.0, 2.0)


def test_an_exported_map_carries_the_fonts_its_signs_use():
    from engine.package_assets import map_assets

    def thing(**props):
        props.setdefault('type', 'decal')
        return {'type': props['type'], 'pos': [0, 0, 0], 'properties': props}

    data = {'things': [thing(texture='Signage.png', text='EXIT'),
                       thing(texture='grime_01.png', text='', font='O.K. Retro'),
                       thing(text='DANGER', font='Hornet Display'),
                       thing(type='light', text='not a decal', font='O.K. Retro')]}
    assets = map_assets(data)
    assert 'fonts/editor/FioUI-Regular.ttf' in assets
    assert 'fonts/HornetDisplay-Regular.ttf' in assets
    assert 'fonts/O.K.Retro.otf' not in assets                   # no words, or not a decal
    assert 'Signage.png' in assets
    from engine.package_assets import ProjectAssets
    from tests.helpers.paths import repo_path
    with ProjectAssets(repo_path()) as project:
        assert project.resolve('fonts/editor/FioUI-Regular.ttf') == \
            'assets/fonts/editor/FioUI-Regular.ttf'


def test_a_played_package_draws_signs_in_the_fonts_it_brought(clean_texts):
    from tests.helpers.paths import repo_path
    regular = open(repo_path('assets', 'fonts', 'editor', 'FioUI-Regular.ttf'), 'rb').read()
    source = decal_textures.DecalSource({}, {'editor/FioUI-Regular.ttf': lambda: regular})
    try:
        decal_textures.use_source(source)
        assert decal_text._read_font('editor/FioUI-Regular.ttf') == regular
        layer = decal_text.layer_pixels(decal_text.entry(_sign()), 128)
        assert _ink(layer) > 200
        # A font the package did not bring: Fio UI's, so the words still show.
        layer = decal_text.layer_pixels(decal_text.entry(_sign(font='Hornet Display')), 128)
        assert _ink(layer) > 200
    finally:
        decal_textures.use_source(None)
