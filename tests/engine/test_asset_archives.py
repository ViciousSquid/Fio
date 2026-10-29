"""Archive-backed category reads never materialise missing project files."""

import zipfile

import pytest

from engine.asset_archives import (
    asset_bytes, asset_exists, asset_names, prepare_asset_archives,
)
from tools.pack_assets import pack_category


def write_archive(root, category, members):
    assets = root / 'assets'
    assets.mkdir(exist_ok=True)
    with zipfile.ZipFile(assets / f'{category}.fiopak', 'w') as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def test_all_categories_mount_without_materialisation(tmp_path):
    categories = {
        'editor': ('assets/b_save.png', b'icon'),
        'sprites': ('assets/sprites/monster/a.png', b'sprite'),
        'textures': ('assets/textures/stone.jpg', b'texture'),
        'models': ('assets/models/tree.obj', b'obj'),
        'fonts': ('assets/fonts/editor/FioUI-Regular.ttf', b'font'),
        'music': ('assets/music/theme.ogg', b'music'),
        'sounds': ('assets/sounds/hit.wav', b'sound'),
    }
    for category, (member, data) in categories.items():
        write_archive(tmp_path, category, {member: data})

    assert prepare_asset_archives(tmp_path) == tuple(categories)
    for category in ('sprites', 'textures', 'models', 'fonts', 'sounds', 'music'):
        directory = tmp_path / 'assets' / category
        assert directory.is_dir()
        assert not any(directory.iterdir())
    for member, data in categories.values():
        path = tmp_path / member
        assert not path.exists()
        assert asset_exists(path)
        assert asset_bytes(path) == data
        assert not path.exists()
    assert asset_names('assets/models/', tmp_path) == ['assets/models/tree.obj']
    # Reading a ZIP member must not populate its matching loose directory.
    assert not any((tmp_path / 'assets/models').iterdir())


def test_loose_development_override_takes_precedence(tmp_path):
    write_archive(tmp_path, 'sprites', {'assets/sprites/a.png': b'archive'})
    path = tmp_path / 'assets/sprites/a.png'
    path.parent.mkdir()
    path.write_bytes(b'loose')
    prepare_asset_archives(tmp_path)
    assert asset_bytes(path) == b'loose'
    assert path.read_bytes() == b'loose'


@pytest.mark.parametrize('category, file_name', [
    ('sounds', 'effect.wav'),
    ('music', 'theme.ogg'),
])
def test_audio_is_discovered_and_read_natively_without_extraction(
        tmp_path, category, file_name):
    from engine.audio_archives import read_archive_audio
    member = f'assets/{category}/{file_name}'
    write_archive(tmp_path, category, {member: b'audio'})
    assert prepare_asset_archives(tmp_path) == (category,)
    assert (tmp_path / 'assets' / category).is_dir()
    assert not any((tmp_path / 'assets' / category).iterdir())
    assert read_archive_audio(str(tmp_path / 'assets' / f'{category}.fiopak'),
                              member, f'assets/{category}') == b'audio'


@pytest.mark.parametrize('name', [
    'assets/../outside.png', '/assets/sprites/a.png',
    'assets/sprites/../../outside.png', 'assets/sprites/code.py',
    'assets/models/a.png', 'assets/sprites\\a.png',
])
def test_invalid_archive_writes_nothing(tmp_path, name):
    write_archive(tmp_path, 'sprites', {
        'assets/sprites/ok.png': b'ok', name: b'bad',
    })
    prepare_asset_archives(tmp_path)
    assert (tmp_path / 'assets/sprites').is_dir()
    assert not (tmp_path / 'assets/sprites/ok.png').exists()
    assert asset_bytes(tmp_path / 'assets/sprites/ok.png') is None


def test_corrupt_archive_keeps_loose_installation_usable(tmp_path):
    assets = tmp_path / 'assets'
    assets.mkdir()
    (assets / 'editor.fiopak').write_bytes(b'not a ZIP')
    (assets / 'b_save.png').write_bytes(b'loose')
    prepare_asset_archives(tmp_path)
    assert asset_bytes(assets / 'b_save.png') == b'loose'


def test_repacking_is_deterministic_and_archive_reads_round_trip(tmp_path):
    source = tmp_path / 'assets/sprites/nested/a.png'
    source.parent.mkdir(parents=True)
    payload = bytes(range(256)) * 10
    source.write_bytes(payload)
    path, count = pack_category(tmp_path, 'sprites')
    assert count == 1
    first = path.read_bytes()
    pack_category(tmp_path, 'sprites')
    assert path.read_bytes() == first
    source.unlink()
    prepare_asset_archives(tmp_path)
    assert asset_bytes(source) == payload
    assert not source.exists()


def test_repacking_partial_overrides_preserves_archive_only_members(tmp_path):
    write_archive(tmp_path, 'sprites', {
        'assets/sprites/a.png': b'old',
        'assets/sprites/b.png': b'archive only',
    })
    source = tmp_path / 'assets/sprites/a.png'
    source.parent.mkdir()
    source.write_bytes(b'edited')
    path, _ = pack_category(tmp_path, 'sprites')
    with zipfile.ZipFile(path) as archive:
        assert archive.read('assets/sprites/a.png') == b'edited'
        assert archive.read('assets/sprites/b.png') == b'archive only'
