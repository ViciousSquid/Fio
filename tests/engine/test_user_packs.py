"""End users' own .fiopak packs in assets/ are recognised in place.

Any ``assets/*.fiopak`` that is not a built-in category archive is a user
pack of textures, models, sounds, music or sprites. Its members reach every
loader through the archive index, package export carries them, and Speakers
can pick its sounds -- with nothing extracted. Packs are read leniently (an
end user zips a folder however their archiver does it), but only user asset
data is ever served from one.
"""

import zipfile

import pytest

from engine import asset_archives
from engine.asset_archives import (asset_bytes, asset_exists, asset_files, asset_isdir,
                                   asset_listdir, prepare_asset_archives, user_member_name,
                                   user_packs)


def write_zip(path, members):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in members.items():
            archive.writestr(name, data)


@pytest.fixture(autouse=True)
def _forget_the_index():
    """Each test indexes its own installation; the editor's is restored."""
    saved = dict(asset_archives._state)
    yield
    asset_archives._state.update(saved)


@pytest.mark.parametrize('member, expected', [
    ('assets/textures/Brick/wall.png', 'assets/textures/Brick/wall.png'),
    ('textures/wall.png', 'assets/textures/wall.png'),            # no assets/
    ('MyPack/textures/wall.png', 'assets/textures/wall.png'),     # zipped folder
    ('MyPack/assets/models/crate.obj', 'assets/models/crate.obj'),
    ('sounds/door.ogg', 'assets/sounds/door.ogg'),
    ('music/theme.mp3', 'assets/music/theme.mp3'),
    ('sprites/coin.png', 'assets/sprites/coin.png'),
    ('textures\\win\\tile.jpg', 'assets/textures/win/tile.jpg'),  # Windows zip
    ('wall.png', None),                          # no category folder
    ('textures/evil.py', None),                  # not asset data
    ('fonts/x.ttf', None), ('editor/x.png', None),   # not user categories
    ('textures/../../escape.png', None), ('/textures/a.png', None),
    ('C:/textures/a.png', None),
    ('__MACOSX/textures/._wall.png', None), ('textures/.DS_Store', None),
])
def test_a_user_pack_member_names_one_user_asset_or_nothing(member, expected):
    assert user_member_name(member) == expected


def test_user_packs_reach_every_loader_without_extraction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_zip(tmp_path / 'assets/MyStuff.fiopak', {
        'MyStuff/textures/Brick/wall.png': b'wall',
        'MyStuff/models/crate.obj': b'mtllib crate.mtl\n',
        'MyStuff/models/crate.mtl': b'newmtl a\n',
        'MyStuff/sounds/door.ogg': b'door',
        'MyStuff/readme.txt': b'not an asset',
        'MyStuff/textures/hack.py': b'print()',
        '__MACOSX/MyStuff/._readme.txt': b'fork',
    })
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*') if p.is_file())
    prepare_asset_archives(tmp_path)
    assert user_packs() == ('MyStuff.fiopak',)
    assert asset_bytes('assets/textures/Brick/wall.png') == b'wall'
    assert asset_bytes(tmp_path / 'assets/textures/brick/WALL.PNG') == b'wall'   # any case
    assert asset_exists('assets/models/crate.obj')
    assert asset_isdir('assets/textures/Brick')
    assert asset_listdir('assets/textures') == ['Brick']
    assert asset_listdir('assets/models') == ['crate.mtl', 'crate.obj']
    assert [p.replace('\\', '/') for p in asset_files('assets/sounds', ('.ogg',))] == [
        'assets/sounds/door.ogg']
    assert not asset_exists('assets/textures/hack.py')
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob('*') if p.is_file())
    assert after == before                                           # nothing written


def test_loose_files_then_built_in_archives_win_over_user_packs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    write_zip(tmp_path / 'assets/a_pack.fiopak', {'textures/a.png': b'pack',
                                                 'textures/b.png': b'pack',
                                                 'textures/c.png': b'pack a'})
    write_zip(tmp_path / 'assets/b_pack.fiopak', {'textures/c.png': b'pack b'})
    write_zip(tmp_path / 'assets/textures.fiopak', {'assets/textures/b.png': b'built-in'})
    (tmp_path / 'assets/textures').mkdir()
    (tmp_path / 'assets/textures/a.png').write_bytes(b'loose')
    prepare_asset_archives(tmp_path)
    assert asset_bytes('assets/textures/a.png') == b'loose'
    assert asset_bytes('assets/textures/b.png') == b'built-in'
    assert asset_bytes('assets/textures/c.png') == b'pack a'        # packs by name


def test_a_pack_added_changed_or_removed_is_seen_on_the_next_index(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'assets').mkdir()
    prepare_asset_archives(tmp_path)
    assert user_packs() == ()
    pack = tmp_path / 'assets/new.fiopak'
    write_zip(pack, {'textures/n.png': b'one'})
    prepare_asset_archives(tmp_path)
    assert asset_bytes('assets/textures/n.png') == b'one'
    write_zip(pack, {'textures/n.png': b'two, and longer'})
    assert asset_bytes('assets/textures/n.png') == b'two, and longer'   # re-read
    pack.unlink()
    prepare_asset_archives(tmp_path)
    assert user_packs() == () and asset_bytes('assets/textures/n.png') is None


def test_a_broken_pack_is_reported_and_leaves_the_rest_working(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    write_zip(tmp_path / 'assets/good.fiopak', {'textures/g.png': b'good'})
    (tmp_path / 'assets/broken.fiopak').write_bytes(b'not a zip')
    prepare_asset_archives(tmp_path)
    assert 'Cannot read broken.fiopak' in capsys.readouterr().out
    assert asset_bytes('assets/textures/g.png') == b'good'
    assert user_packs() == ('good.fiopak',)


def test_package_export_sources_include_user_pack_assets(tmp_path):
    from engine.package_assets import ProjectAssets
    write_zip(tmp_path / 'assets/mine.fiopak', {'models/crate.obj': b'obj',
                                               'textures/t.png': b'pack'})
    (tmp_path / 'assets/textures').mkdir(parents=True)
    (tmp_path / 'assets/textures/t.png').write_bytes(b'loose')
    with ProjectAssets(tmp_path) as sources:
        assert sources.resolve('crate.obj') == 'assets/models/crate.obj'
        assert sources.read('assets/models/crate.obj') == b'obj'
        assert sources.read('assets/textures/t.png') == b'loose'      # loose wins


def test_speakers_can_choose_sounds_from_user_packs(tmp_path):
    from engine.speaker_audio import list_speaker_audio
    (tmp_path / 'assets/sounds').mkdir(parents=True)
    (tmp_path / 'assets/sounds/loose.wav').write_bytes(b'')
    write_zip(tmp_path / 'assets/sfx.fiopak', {'sounds/ui/click.ogg': b'c',
                                              'music/theme.mp3': b'm'})
    assert list_speaker_audio(str(tmp_path), 'sounds') == [
        'assets/sounds/loose.wav', 'assets/sounds/ui/click.ogg']
    assert list_speaker_audio(str(tmp_path), 'music') == ['assets/music/theme.mp3']
