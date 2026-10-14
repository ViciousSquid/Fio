"""A real editor host finds archive assets before constructing its toolbar."""

from pathlib import Path
import shutil

import pytest

pytestmark = pytest.mark.qt


def test_archive_only_assets_start_the_editor_with_the_same_icon_pixels(
        qt_app, tmp_path, monkeypatch):
    from PyQt5.QtGui import QIcon, QPixmap
    from editor.main_window import MainWindow
    root = Path(__file__).resolve().parents[2]
    assets = tmp_path / 'assets'
    assets.mkdir()
    for archive in (root / 'assets').glob('*.fiopak'):
        shutil.copy2(archive, assets / archive.name)
    monkeypatch.chdir(tmp_path)
    assert not (assets / 'b_save.png').exists()
    window = MainWindow(str(tmp_path))
    try:
        assert not window.undo_action.icon().isNull()
        for name in ('b_save.png', 'b_undo.png', 'splash.png', 'sprites/logic_camera.png'):
            expected = QPixmap(str(root / 'assets' / name)).toImage()
            actual = QPixmap(str(assets / name)).toImage()
            assert not actual.isNull()
            assert actual == expected
        assert not QIcon(str(assets / 'b_save.png')).isNull()
        for category in ('models', 'textures', 'fonts', 'sprites'):
            for source in (root / 'assets' / category).rglob('*'):
                if source.is_file():
                    assert (assets / source.relative_to(root / 'assets')).read_bytes() == source.read_bytes()
        assert (assets / 'noise_3d.bin').read_bytes() == (root / 'assets/noise_3d.bin').read_bytes()
    finally:
        window.unsaved_changes = False
        window.close()
        window.deleteLater()
