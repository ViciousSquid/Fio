"""A read-only install (a translocated Fio.app) writes to the user folder."""
import pytest

pytestmark = pytest.mark.qt  # importing the editor package loads PyQt5

from editor import user_paths


@pytest.fixture
def user_dir(tmp_path, monkeypatch):
    target = tmp_path / 'user'
    monkeypatch.setattr(user_paths, 'user_data_dir', lambda: str(target))
    return target


@pytest.fixture
def read_only_install(tmp_path, monkeypatch):
    """An install folder that cannot be written, as a translocated Fio.app."""
    install = tmp_path / 'Fio.app'
    install.mkdir()
    (install / 'settings.ini').write_text('[Display]\nfont_size = 11\n')
    real = user_paths._can_write
    monkeypatch.setattr(
        user_paths, '_can_write',
        lambda path: False if str(path).startswith(str(install)) else real(path))
    return install


def test_a_writable_install_keeps_its_own_files(tmp_path, user_dir):
    assert user_paths.settings_path(str(tmp_path)) == str(tmp_path / 'settings.ini')
    assert user_paths.writable_dir(str(tmp_path), 'maps') == str(tmp_path / 'maps')
    assert not user_dir.exists()


def test_read_only_settings_move_to_the_user_folder_seeded(read_only_install, user_dir):
    path = user_paths.settings_path(str(read_only_install))
    assert path == str(user_dir / 'settings.ini')
    assert 'font_size = 11' in open(path).read()


def test_read_only_settings_seed_only_once(read_only_install, user_dir):
    first = user_paths.settings_path(str(read_only_install))
    with open(first, 'a') as handle:
        handle.write('[Editor]\nautosave_enabled = False\n')
    again = user_paths.settings_path(str(read_only_install))
    assert 'autosave_enabled = False' in open(again).read()


def test_read_only_autosaves_move_to_the_user_folder(read_only_install, user_dir):
    assert user_paths.writable_dir(str(read_only_install), 'maps') == str(user_dir / 'maps')
