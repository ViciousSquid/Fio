"""Category .fiopak archives are read in place: nothing is extracted, a loose
file overrides its archive member, and every loader finds archived assets."""

import io
import zipfile

import pytest

from engine import asset_archives
from engine.asset_archives import (asset_bytes, asset_exists, asset_files, asset_isdir,
                                   asset_listdir, asset_source, prepare_asset_archives)
from tools.pack_assets import pack_category


def write_archive(root, category, members):
    assets = root / 'assets'
    assets.mkdir(exist_ok=True)
    with zipfile.ZipFile(assets / f'{category}.fiopak', 'w') as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def _files(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file())


@pytest.fixture(autouse=True)
def _forget_the_index():
    """Each test indexes its own installation; the editor's is restored."""
    saved = dict(asset_archives._state)
    yield
    asset_archives._state.update(saved)


def test_archives_are_read_in_place_with_loose_overrides(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_archive(tmp_path, 'editor', {'assets/b_save.png': b'original icon',
                                       'assets/splash.png': b'splash'})
    write_archive(tmp_path, 'sprites', {'assets/sprites/monster/a.png': b'sprite'})
    override = tmp_path / 'assets/b_save.png'
    override.write_bytes(b'my edited icon')
    before = _files(tmp_path)
    assert prepare_asset_archives(tmp_path) == ('editor', 'sprites')
    assert _files(tmp_path) == before                     # nothing written
    assert asset_bytes(override) == b'my edited icon'      # loose wins
    assert asset_bytes('assets/splash.png') == b'splash'
    assert asset_bytes(tmp_path / 'assets/sprites/monster/a.png') == b'sprite'
    assert asset_bytes('assets/sprites/monster/missing.png') is None
    assert asset_exists('assets/sprites/monster/a.png')
    assert not asset_exists('assets/sprites/monster/b.png')
    assert asset_isdir('assets/sprites/monster')
    assert asset_listdir('assets/sprites') == ['monster']
    assert asset_files('assets/sprites', ('.png',)) == [
        str(tmp_path / 'assets/sprites/monster/a.png').replace(str(tmp_path) + '/', '')]
    source = asset_source('assets/sprites/monster/a.png')
    assert isinstance(source, io.BytesIO) and source.read() == b'sprite'
    assert asset_source(override) == str(override)        # a loose file: its path


def test_a_member_is_found_whatever_the_case_of_its_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_archive(tmp_path, 'textures', {'assets/textures/Dev/256.jpg': b'jpeg'})
    prepare_asset_archives(tmp_path)
    assert asset_bytes('assets/textures/dev/256.JPG') == b'jpeg'


def test_audio_is_discovered_and_read_natively_without_extraction(tmp_path):
    from engine.audio_archives import read_archive_audio
    write_archive(tmp_path, 'music', {'assets/music/theme.ogg': b'music'})
    assert prepare_asset_archives(tmp_path) == ('music',)
    assert not (tmp_path / 'assets/music').exists()
    assert read_archive_audio(str(tmp_path / 'assets/music.fiopak'),
                              'assets/music/theme.ogg', 'assets/music') == b'music'


@pytest.mark.parametrize('name', ['assets/../outside.png', '/assets/sprites/a.png',
                                  'assets/sprites/../../outside.png',
                                  'assets/sprites/code.py', 'assets/models/a.png',
                                  'assets/sprites\\a.png'])
def test_an_invalid_archive_is_ignored_whole(tmp_path, monkeypatch, name):
    monkeypatch.chdir(tmp_path)
    write_archive(tmp_path, 'sprites', {'assets/sprites/ok.png': b'ok', name: b'bad'})
    prepare_asset_archives(tmp_path)
    assert not (tmp_path / 'assets/sprites/ok.png').exists()
    assert asset_bytes('assets/sprites/ok.png') is None


def test_nothing_is_written_through_a_symlinked_folder(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    write_archive(tmp_path, 'sprites', {'assets/sprites/ok.png': b'ok'})
    (tmp_path / 'assets/sprites').symlink_to(outside, target_is_directory=True)
    prepare_asset_archives(tmp_path)
    assert list(outside.iterdir()) == []


def test_corrupt_archive_keeps_the_loose_installation_usable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assets = tmp_path / 'assets'
    assets.mkdir()
    (assets / 'editor.fiopak').write_bytes(b'not a ZIP')
    (assets / 'b_save.png').write_bytes(b'loose')
    prepare_asset_archives(tmp_path)
    assert asset_bytes('assets/b_save.png') == b'loose'


def test_a_repacked_archive_is_read_again_without_a_restart(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / 'assets/sprites/nested/a.png'
    source.parent.mkdir(parents=True)
    payload = bytes(range(256)) * 10
    source.write_bytes(payload)
    path, count = pack_category(tmp_path, 'sprites')
    assert count == 1
    first = path.read_bytes()
    pack_category(tmp_path, 'sprites')
    assert path.read_bytes() == first                       # deterministic
    source.unlink()
    prepare_asset_archives(tmp_path)
    assert asset_bytes(source) == payload
    assert not source.exists()                              # still not extracted
    source.write_bytes(b'edited')
    pack_category(tmp_path, 'sprites')
    source.unlink()
    assert asset_bytes(source) == b'edited'                 # the new archive


def test_repacking_partial_overrides_preserves_archive_only_members(tmp_path):
    write_archive(tmp_path, 'sprites', {'assets/sprites/a.png': b'old',
                                        'assets/sprites/b.png': b'archive only'})
    source = tmp_path / 'assets/sprites/a.png'
    source.parent.mkdir()
    source.write_bytes(b'edited')
    path, _ = pack_category(tmp_path, 'sprites')
    with zipfile.ZipFile(path) as archive:
        assert archive.read('assets/sprites/a.png') == b'edited'
        assert archive.read('assets/sprites/b.png') == b'archive only'


def test_model_and_texture_loaders_read_archived_members(tmp_path, monkeypatch):
    from PIL import Image
    from engine import glb_loader, obj_loader
    from engine.animated_texture import decode_gif_frames, is_animated_gif
    monkeypatch.chdir(tmp_path)
    frames = [Image.new('RGBA', (4, 4), (255, 0, 0, 255)),
              Image.new('RGBA', (4, 4), (0, 255, 0, 255))]
    gif = io.BytesIO()
    frames[0].save(gif, format='GIF', save_all=True, append_images=frames[1:], duration=50)
    write_archive(tmp_path, 'models', {'assets/models/box.obj': b'v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n',
                                       'assets/models/box.glb': b'glTF....'})
    write_archive(tmp_path, 'textures', {'assets/textures/fire.gif': gif.getvalue()})
    prepare_asset_archives(tmp_path)
    assert obj_loader._read_text('assets/models/box.obj').startswith('v 0 0 0')
    assert glb_loader._read_bounded('assets/models/box.glb') == b'glTF....'
    assert is_animated_gif('assets/textures/fire.gif')
    images, _seconds = decode_gif_frames('assets/textures/fire.gif')
    assert len(images) == 2
    assert _files(tmp_path) == ['assets/models.fiopak', 'assets/textures.fiopak']


@pytest.mark.qt
def test_qt_images_icons_and_fonts_come_from_the_archive(qt_app, tmp_path, monkeypatch):
    from pathlib import Path
    from PyQt5.QtGui import QFontDatabase, QImage
    from engine.asset_qt import (add_application_font, image_size, load_icon, load_image,
                                 load_pixmap)
    monkeypatch.chdir(tmp_path)
    root = Path(__file__).resolve().parents[2]
    font = next((root / 'assets/fonts/editor').glob('*.ttf')).read_bytes()
    image = QImage(3, 2, QImage.Format_RGBA8888)
    image.fill(0xff336699)
    from PyQt5.QtCore import QBuffer, QByteArray, QIODevice
    data = QByteArray()
    qbuffer = QBuffer(data)
    qbuffer.open(QIODevice.WriteOnly)
    image.save(qbuffer, 'PNG')
    write_archive(tmp_path, 'editor', {'assets/icon.png': bytes(data)})
    write_archive(tmp_path, 'fonts', {'assets/fonts/editor/ui.ttf': font})
    prepare_asset_archives(tmp_path)
    assert load_image('assets/icon.png').size().width() == 3
    assert load_pixmap('assets/icon.png').height() == 2
    assert image_size('assets/icon.png').width() == 3
    assert not load_icon('assets/icon.png').isNull()
    font_id = add_application_font('assets/fonts/editor/ui.ttf')
    assert font_id >= 0 and QFontDatabase.applicationFontFamilies(font_id)
    assert load_pixmap('assets/missing.png').isNull()
    assert _files(tmp_path) == ['assets/editor.fiopak', 'assets/fonts.fiopak']


def test_editor_archive_holds_private_artwork_and_ignores_desktop_litter(
        tmp_path, monkeypatch):
    """An editor.fiopak rebuilt by hand (Finder's Compress adds __MACOSX and
    .DS_Store) that also carries assets/editor/playerstart.obj still serves
    every icon and the playerstart model: one such member used to make the
    whole archive unreadable -- blank toolbar icons and no player start."""
    monkeypatch.chdir(tmp_path)
    write_archive(tmp_path, 'editor', {
        'assets/select.png': b'icon',
        'assets/editor/playerstart.obj': b'mtllib playerstart.mtl\n',
        'assets/editor/playerstart.mtl': b'newmtl ink\n',
        '__MACOSX/assets/._select.png': b'resource fork',
        'assets/.DS_Store': b'finder',
        'assets/editor/.DS_Store': b'finder',
    })
    assert prepare_asset_archives(tmp_path) == ('editor',)
    assert asset_bytes('assets/select.png') == b'icon'
    model = tmp_path / 'assets' / 'editor' / 'playerstart.obj'
    assert asset_exists(model)
    assert asset_bytes(model.with_suffix('.mtl')) == b'newmtl ink\n'
    assert not asset_exists('assets/.DS_Store')
    assert _files(tmp_path) == ['assets/editor.fiopak']     # nothing extracted


@pytest.mark.parametrize('name', ['assets/editor/code.py', 'assets/editor/../x.png',
                                  'assets/sprites/a.png'])
def test_editor_archive_still_refuses_what_is_not_editor_data(
        tmp_path, monkeypatch, name):
    monkeypatch.chdir(tmp_path)
    write_archive(tmp_path, 'editor', {'assets/select.png': b'icon', name: b'bad'})
    prepare_asset_archives(tmp_path)
    assert asset_bytes('assets/select.png') is None


def test_repository_editor_archive_carries_the_playerstart_model():
    """The shipped archive alone draws the player start (no loose copy)."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    with zipfile.ZipFile(root / 'assets' / 'editor.fiopak') as archive:
        names = set(archive.namelist())
    assert {'assets/editor/playerstart.obj', 'assets/editor/playerstart.mtl',
            'assets/select.png'} <= names
