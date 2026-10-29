"""A real editor host reads archived icons and assets without unpacking them."""

from pathlib import Path
import shutil

import pytest

pytestmark = pytest.mark.qt


def test_archive_only_assets_start_editor_without_extraction(
        qt_app, tmp_path, monkeypatch):
    from PyQt5.QtGui import QPixmap
    from editor.main_window import MainWindow
    from engine.asset_archives import asset_bytes, asset_exists, prepare_asset_archives
    from engine.asset_qt import asset_pixmap

    root = Path(__file__).resolve().parents[2]
    assets = tmp_path / 'assets'
    assets.mkdir()
    for archive in (root / 'assets').glob('*.fiopak'):
        shutil.copy2(archive, assets / archive.name)
    monkeypatch.chdir(tmp_path)

    assert not (assets / 'b_save.png').exists()
    assert not (assets / 'sprites').exists()
    assert not (assets / 'textures').exists()
    prepare_asset_archives(tmp_path)
    window = MainWindow(str(tmp_path))
    try:
        assert not window.undo_action.icon().isNull()
        for name in ('b_save.png', 'b_undo.png', 'splash.png',
                     'sprites/logic_camera.png'):
            source = root / 'assets' / name
            target = assets / name
            assert asset_exists(target)
            assert asset_bytes(target) == source.read_bytes()
            expected = QPixmap(str(source)).toImage()
            actual = asset_pixmap(str(target)).toImage()
            assert not actual.isNull()
            assert actual == expected
            assert not target.exists()

        for category in ('sprites', 'textures', 'models', 'fonts',
                         'sounds', 'music'):
            # No category is materialised by editor startup or reading.
            assert not (assets / category).exists()
        assert asset_bytes(assets / 'noise_3d.bin') == (
            root / 'assets/noise_3d.bin').read_bytes()
        assert not (assets / 'noise_3d.bin').exists()
    finally:
        window.unsaved_changes = False
        window.close()
        window.deleteLater()
