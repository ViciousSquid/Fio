"""Archive-only startup preserves payload bytes and loose development overrides."""

import zipfile

import pytest

from engine.asset_archives import prepare_asset_archives
from tools.pack_assets import pack_category


def write_archive(root, category, members):
    assets = root / 'assets'
    assets.mkdir(exist_ok=True)
    with zipfile.ZipFile(assets / f'{category}.fiopak', 'w') as archive:
        for name, data in members.items():
            archive.writestr(name, data)


def test_startup_restores_exact_bytes_without_overwriting_loose_assets(tmp_path):
    write_archive(tmp_path, 'editor', {'assets/b_save.png': b'original icon',
                                     'assets/splash.png': b'splash'})
    write_archive(tmp_path, 'sprites', {'assets/sprites/monster/a.png': b'sprite'})
    override = tmp_path / 'assets/b_save.png'
    override.write_bytes(b'my edited icon')
    assert prepare_asset_archives(tmp_path) == ('editor', 'sprites')
    assert override.read_bytes() == b'my edited icon'
    assert (tmp_path / 'assets/splash.png').read_bytes() == b'splash'
    sprite = tmp_path / 'assets/sprites/monster/a.png'
    assert sprite.read_bytes() == b'sprite'
    mtime = sprite.stat().st_mtime_ns
    prepare_asset_archives(tmp_path)
    assert sprite.stat().st_mtime_ns == mtime


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
    assert not (tmp_path / 'assets' / category).exists()
    assert read_archive_audio(str(tmp_path / 'assets' / f'{category}.fiopak'),
                              member, f'assets/{category}') == b'audio'


@pytest.mark.parametrize('name', ['assets/../outside.png', '/assets/sprites/a.png',
                                  'assets/sprites/../../outside.png',
                                  'assets/sprites/code.py', 'assets/models/a.png',
                                  'assets/sprites\\a.png'])
def test_invalid_archive_writes_nothing(tmp_path, name):
    write_archive(tmp_path, 'sprites', {'assets/sprites/ok.png': b'ok', name: b'bad'})
    prepare_asset_archives(tmp_path)
    assert not (tmp_path / 'assets/sprites/ok.png').exists()


def test_symlink_destination_cannot_escape_assets(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    write_archive(tmp_path, 'sprites', {'assets/sprites/ok.png': b'ok'})
    (tmp_path / 'assets/sprites').symlink_to(outside, target_is_directory=True)
    prepare_asset_archives(tmp_path)
    assert not (outside / 'ok.png').exists()


def test_corrupt_archive_keeps_the_loose_installation_usable(tmp_path):
    assets = tmp_path / 'assets'
    assets.mkdir()
    (assets / 'editor.fiopak').write_bytes(b'not a ZIP')
    (assets / 'b_save.png').write_bytes(b'loose')
    prepare_asset_archives(tmp_path)
    assert (assets / 'b_save.png').read_bytes() == b'loose'


def test_repacking_is_deterministic_and_round_trips(tmp_path):
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
    assert source.read_bytes() == payload


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
