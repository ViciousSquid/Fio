"""A real editor host reads archive assets in place: nothing is extracted."""

from pathlib import Path
import shutil

import pytest

pytestmark = pytest.mark.qt


def test_archive_only_assets_start_the_editor_with_the_same_icon_pixels(
        qt_app, tmp_path, monkeypatch):
    from PyQt5.QtGui import QIcon, QPixmap
    from editor.main_window import MainWindow
    from engine.asset_archives import ASSET_CATEGORIES
    from tools.pack_assets import pack_category
    root = Path(__file__).resolve().parents[2]
    # The repository keeps loose assets and need not ship every category
    # archive, so pack an archive-only installation from a copy of them.
    source = tmp_path / 'source'
    shutil.copytree(root / 'assets', source / 'assets',
                    ignore=shutil.ignore_patterns('__pycache__', 'shaders'))
    for category in ASSET_CATEGORIES:
        pack_category(source, category)
    install = tmp_path / 'install'
    assets = install / 'assets'
    assets.mkdir(parents=True)
    for archive in (source / 'assets').glob('*.fiopak'):
        shutil.copy2(archive, assets / archive.name)
    monkeypatch.chdir(install)
    assert not (assets / 'b_save.png').exists()
    before = sorted(p.relative_to(install).as_posix() for p in install.rglob('*') if p.is_file())
    window = MainWindow(str(install))
    try:
        from engine.asset_archives import asset_bytes
        from engine.asset_qt import load_icon, load_pixmap
        assert not window.undo_action.icon().isNull()
        for name in ('b_save.png', 'b_undo.png', 'splash.png', 'sprites/logic_camera.png'):
            expected = QPixmap(str(root / 'assets' / name)).toImage()
            actual = load_pixmap(str(assets / name)).toImage()
            assert not actual.isNull()
            assert actual == expected
        assert not load_icon(str(assets / 'b_save.png')).isNull()
        for category in ('models', 'textures', 'fonts', 'sprites'):
            for source in (root / 'assets' / category).rglob('*'):
                if source.is_file() and source.name != '.gitkeep':
                    member = assets / source.relative_to(root / 'assets')
                    assert asset_bytes(member) == source.read_bytes()
        assert asset_bytes(assets / 'noise_3d.bin') == (root / 'assets/noise_3d.bin').read_bytes()
        # Read in place: not one file was written under the installation
        # (settings aside: the editor keeps its own; the asset browser makes
        # its empty folders, as it always has).
        after = sorted(p.relative_to(install).as_posix() for p in install.rglob('*')
                       if p.is_file())
        written = [p for p in after if p not in before and not p.startswith('settings')]
        assert written == [], written[:10]
    finally:
        window.unsaved_changes = False
        window.close()
        window.deleteLater()
