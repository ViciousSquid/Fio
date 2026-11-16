"""Visual tier: a decal's words, drawn on a wall, and changed by IO.

A ``Decal`` with ``text`` paints its words (engine.decal_text) through the
same decal table and pass as any decal. Seen from where it was thrown:

* the words are black, the right way round, and nothing else of the wall
  changes (a sign with no image paints its words alone);
* words over an image go on top of it;
* the IO inputs change them in a running level -- SetText, ClearText,
  SetFont -- through the decal table's change journal, as any input does;
* new words draw their own layer only: no image is read again.
"""

import io

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]

SIZE = 192
GREY = 'test_mid_grey.png'


def _grey_png():
    from PIL import Image
    data = io.BytesIO()
    # Mid grey: an image decal tints the surface 2x, so this one leaves it
    # exactly as it was -- only the words show.
    Image.new('RGBA', (64, 64), (128, 128, 128, 255)).save(data, format='PNG')
    return data.getvalue()


@pytest.fixture
def images(monkeypatch):
    """The decal list: one mid-grey image. Returns the image files read."""
    from engine import decal_textures
    from engine.renderer.forward import decals as decal_pass
    png = _grey_png()
    reads = []
    real_source, real_read = decal_pass.asset_source, decal_textures.read_image

    def source(path):
        if path.endswith(GREY):
            reads.append(path)
            return io.BytesIO(png)
        return real_source(path)

    monkeypatch.setattr(decal_textures, '_read', lambda: (GREY,))
    monkeypatch.setattr(decal_pass, 'asset_source', source)
    monkeypatch.setattr(decal_textures, 'read_image',
                        lambda name: png if name == GREY else real_read(name))
    decal_textures.use_texts(())
    decal_textures.refresh()
    yield reads
    decal_textures.use_texts(())
    monkeypatch.undo()
    decal_textures.refresh()


@pytest.fixture
def renderer(images):
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as context:
        made = glh.make_renderer()
        try:
            yield made, context
        finally:
            made.cleanup()
    glh.reset_texture_cache()


def _sign(**props):
    from editor.things import Decal
    base = dict(texture='', text='L', width=240.0, height=120.0, depth=64.0,
                yaw=90.0, pitch=0.0, name='sign')
    base.update(props)
    return Decal(pos=[0.0, 100.0, -200.0], properties=base)    # on the wall, facing +Z


class Scene:
    """A wall facing +Z, lit from the camera, looking along the throw."""

    def __init__(self, renderer, context, intensity=2.0):
        from editor.things import Light
        from engine.decal_table import DecalTable
        from tests.helpers.worlds import box_brush, make_thing
        self.renderer, self.context = renderer, context
        self.wall = box_brush('wall', (0, 100, -216), (900, 900, 32))
        self.lamp = make_thing(Light, 'lamp', (0.0, 100.0, 100.0), radius=4000.0,
                               intensity=intensity, state='on', casts_shadows=False)
        self.table = DecalTable()        # one table, as the logic thread keeps
        # As the editor's: one list of things, and a revision that moves when
        # things are added or removed -- an IO input moves neither, so its
        # change reaches the table through the change journal.
        self.things, self.epoch = [], 0

    def frame(self, decals=()):
        import glm
        decals = list(decals)
        if [id(d) for d in decals] != [id(d) for d in self.things]:
            self.things[:] = decals
            self.epoch += 1
        self.table.place(self.things, show_selection=False, epoch=self.epoch)
        published = self.table.frame(0.0, 1.0, 1.0)
        projection = glm.perspective(glm.radians(60.0), 1.0, 1.0, 4000.0)
        eye = glm.vec3(0.0, 100.0, 100.0)
        view = glm.lookAt(eye, glm.vec3(0.0, 100.0, -200.0), glm.vec3(0, 1, 0))
        config = glh.render_config(all_brushes=[self.wall], all_things=[self.lamp],
                                   all_lights=[self.lamp], play_mode=True,
                                   decals=published)
        self.context.bind()
        with glh.no_gl_errors("decal text"):
            self.renderer.render_scene(projection, view, eye, None, config,
                                       brush_slots=config["all_brush_slots"])
        return self.context.read_pixels().astype(np.int32)


def _ink(plain, signed):
    """Where the decal darkened the wall."""
    return (plain - signed).max(axis=2) > 60


def test_the_words_are_black_and_the_right_way_round_and_change_nothing_else(renderer):
    scene = Scene(*renderer)
    plain = scene.frame()
    signed = scene.frame([_sign()])
    ink = _ink(plain, signed)
    assert ink.sum() > 150
    assert np.median(signed[ink].max(axis=1)) < 50            # black lettering
    changed = np.abs(plain - signed).max(axis=2) > 6
    assert changed.sum() < ink.sum() * 1.6                    # the words, nothing else
    # An L: its stem on the left, its foot along the bottom -- mirrored, the
    # stem would be on the right.
    rows, cols = np.nonzero(ink)
    middle_x = (cols.min() + cols.max()) / 2.0
    middle_y = (rows.min() + rows.max()) / 2.0
    assert cols.mean() < middle_x - 3 and rows.mean() > middle_y + 3, (
        cols.mean(), middle_x, rows.mean(), middle_y)


def test_words_over_an_image_go_on_top_of_it(renderer):
    scene = Scene(*renderer)
    plain = scene.frame()
    image_only = scene.frame([_sign(texture=GREY, text='')])
    assert np.abs(plain - image_only).max() <= 3               # mid grey: no change
    signed = scene.frame([_sign(texture=GREY)])
    assert _ink(plain, signed).sum() > 150


def test_white_words_brighten_the_wall(renderer):
    scene = Scene(*renderer, intensity=0.6)              # a mid-tone wall
    plain = scene.frame()
    wall = float(np.median(plain[SIZE // 2 - 40: SIZE // 2 + 40, :, 0]))
    assert 60 < wall < 170, wall
    signed = scene.frame([_sign(text_colour='White')])
    lit = (signed - plain).max(axis=2) > 40
    assert lit.sum() > 150
    # The surface doubled where the letters are -- its light kept.
    assert np.median(signed[lit][:, 0]) == pytest.approx(min(255.0, 2 * wall), abs=25)
    assert not ((plain - signed).max(axis=2) > 20).any()      # nothing darkened


def _io(decal):
    from editor.io_system import IOManager
    from editor.io_handlers import register_all_input_handlers
    manager = IOManager()
    register_all_input_handlers(manager)
    manager.set_entity_finder(lambda name: decal if name == decal.properties['name'] else None)

    def send(input_name, parameter=''):
        manager._execute_input(decal.properties['name'], input_name, parameter, 'trigger')
    return send


def test_io_changes_the_words_while_the_level_runs(renderer):
    scene = Scene(*renderer)
    plain = scene.frame()
    decal = _sign()
    send = _io(decal)
    first = scene.frame([decal])

    send('SetText', 'EXIT')
    assert decal.properties['text'] == 'EXIT'
    exit_sign = scene.frame([decal])
    assert _ink(plain, exit_sign).sum() > 1.5 * _ink(plain, first).sum()

    send('SetFont', 'hornet display')
    assert decal.properties['font'] == 'Hornet Display'
    hornet = scene.frame([decal])
    assert np.abs(hornet - exit_sign).max() > 60               # drawn again, in Hornet

    send('SetTextColor', 'white')
    assert decal.properties['text_colour'] == 'White'
    white = scene.frame([decal])
    assert not _ink(plain, white).any()                       # white on a lit-white wall
    send('SetTextColor', 'Black')

    send('ClearText')
    assert decal.properties['text'] == ''
    assert np.array_equal(scene.frame([decal]), plain)        # no image, no words: gone

    send('SetText', r'TWO\nLINES')
    assert _ink(plain, scene.frame([decal])).sum() > 150


def test_new_words_draw_their_own_layer_and_read_no_image_again(renderer, images):
    scene = Scene(*renderer)
    decal = _sign(texture=GREY)
    scene.frame([decal])
    assert len(images) == 1                                   # the image, once
    texture = scene.renderer._decal_textures
    send = _io(decal)
    for words in ('ONE', 'TWO', 'THREE'):
        send('SetText', words)
        updated = scene.frame([decal])
    assert len(images) == 1                                   # kept, not read again
    text_layers = [n for n in scene.renderer._decal_layer_pixels if n.startswith('text:')]
    assert len(text_layers) == 1                              # only the words in use
    # Updated in place -- the same texture, its one layer and mipmaps
    # rewritten -- and drawn as a texture built afresh draws it.
    assert scene.renderer._decal_textures == texture
    fresh_renderer = glh.make_renderer()
    try:
        fresh = Scene(fresh_renderer, scene.context).frame([decal])
    finally:
        fresh_renderer.cleanup()
    assert np.abs(fresh - updated).max() <= 2


def test_unchanged_words_are_never_drawn_again(renderer, monkeypatch):
    """Nearly every sign never changes: frame after frame its words are not
    drawn, its row is not remade and the texture is not touched -- only a
    change (an edit, an IO input) does any of that, once."""
    from engine import decal_text
    from engine.renderer.forward import decals as decal_pass
    drawn = []
    real = decal_text.layer_pixels
    monkeypatch.setattr(decal_text, 'layer_pixels',
                        lambda name, size: drawn.append(name) or real(name, size))
    uploads = []
    real_sub = decal_pass.gl.glTexSubImage3D
    monkeypatch.setattr(decal_pass.gl, 'glTexSubImage3D',
                        lambda *a: uploads.append(a[4]) or real_sub(*a))
    scene = Scene(*renderer)
    decals = [_sign(text='EXIT'), _sign(text='EXIT', name='twin'), _sign(text='B-2', name='b2')]
    for _ in range(30):
        scene.frame(decals)
    assert sorted(drawn) == sorted({decal_text.entry(d.properties) for d in decals})
    assert scene.table.rows_made == 3 and uploads == []        # once each, at load

    send = _io(decals[2])
    send('SetText', 'B-3')
    for _ in range(30):
        scene.frame(decals)
    assert len(drawn) == 3 and drawn[-1] == decal_text.entry(decals[2].properties)
    assert scene.table.rows_made == 4                         # that one row, once
    assert len(set(uploads)) == 1                             # that one layer
