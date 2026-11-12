"""Edit > Colours: a Grid tab (the 2D views' grid) and a Windows tab (the
title bar of SysMon, the pause menu and the other windows drawn in the 3D
view), every colour saved to settings.ini and applied to every floating
window."""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtGui import QColor                                  # noqa: E402

from engine import floating_windows                             # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def restore_theme():
    yield
    floating_windows.set_theme()


def test_the_edit_menu_has_colours_with_a_grid_and_a_windows_tab(main_window, monkeypatch,
                                                                 restore_theme):
    from editor.main_window import ColoursDialog
    assert main_window.colours_action.text() == 'Colours…'
    monkeypatch.setattr(main_window, 'save_config', lambda: None)
    dialog = ColoursDialog(main_window.config, main_window)
    assert [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())] == [
        'Grid', 'Windows']
    # The Windows tab offers the title bar alone (and its Defaults).
    assert sorted(k for s, k in dialog.colours if s == 'WindowColours') == ['header']
    # The floating windows default to the teal they always had.
    assert dialog.colours[('WindowColours', 'header')].name() == '#425f5d'
    dialog.colours[('WindowColours', 'header')] = QColor('#802040')
    dialog.colours[('GridColours', 'major')] = QColor('#123456')
    dialog.accept()
    assert main_window.config.get('WindowColours', 'header') == '#802040'
    assert main_window.config.get('GridColours', 'major') == '#123456'
    # Every colour of both tabs is saved, not only the changed ones.
    for key in ('major', 'minor', 'background'):
        assert main_window.config.has_option('GridColours', key)
    main_window.apply_window_colours()
    assert floating_windows.theme('header').name() == '#802040'
    # The dragged title bar follows it, lighter.
    assert floating_windows.theme('header_active').lightness() > \
        floating_windows.theme('header').lightness()
    dialog.reset_to_defaults('WindowColours')
    assert dialog.colours[('WindowColours', 'header')].name() == '#425f5d'
    assert dialog.colours[('GridColours', 'major')].name() == '#123456'   # its own tab


def test_every_floating_window_draws_its_title_bar_in_the_theme(qt_app, restore_theme):
    from PyQt5.QtCore import QRect
    from PyQt5.QtGui import QImage, QPainter
    floating_windows.set_theme({'header': '#ff0000', 'background': 'not a colour'})
    assert floating_windows.theme('background').alpha() == 235       # bad: the default
    assert floating_windows.theme('background').name() == '#141419'
    window = floating_windows.FloatingWindow("Test", 10, 10, 200, 60)
    image = QImage(240, 120, QImage.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    window.draw(painter)
    painter.end()
    assert QColor(image.pixel(60, 18)).name() == '#ff0000'           # its title bar


def test_colours_persist_through_settings_ini(main_window, tmp_path, monkeypatch,
                                             restore_theme):
    """OK writes both tabs to settings.ini; a fresh read brings them back,
    and window colours the dialog no longer offers are dropped."""
    import configparser
    from editor.main_window import ColoursDialog
    path = tmp_path / 'settings.ini'
    monkeypatch.setattr(main_window, 'config_path', str(path))
    if not main_window.config.has_section('WindowColours'):
        main_window.config.add_section('WindowColours')
    main_window.config.set('WindowColours', 'border', '#ff00ff')   # an old option
    dialog = ColoursDialog(main_window.config, main_window)
    dialog.colours[('WindowColours', 'header')] = QColor('#204080')
    dialog.colours[('GridColours', 'background')] = QColor('#101010')
    dialog.accept()
    saved = configparser.ConfigParser()
    saved.read(path)
    assert saved.get('WindowColours', 'header') == '#204080'
    assert not saved.has_option('WindowColours', 'border')
    assert saved.get('GridColours', 'background') == '#101010'
    assert saved.has_option('GridColours', 'major') and saved.has_option('GridColours', 'minor')
