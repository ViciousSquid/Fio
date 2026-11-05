import pytest

from editor.things import Monster

pytestmark = pytest.mark.qt


def test_command_opens_one_floating_window_with_live_tabs(main_window, qt_app):
    a, b = Monster(properties={'name': 'Alpha'}), Monster(properties={'name': 'Bravo'})
    main_window.state.things = [a, b]
    main_window.console_handler.handle_command('botdebug')
    panel = main_window._bot_debug_window
    assert panel.isVisible() and panel._timer.isActive()
    assert panel._timer.interval() == 500
    assert panel.tabs.count() == 3
    assert panel.tabs.tabText(0) == 'Overview'
    assert {panel.tabs.tabText(i) for i in (1, 2)} == {'Alpha', 'Bravo'}
    panel.tabs.setCurrentIndex(2)
    b.properties['name'] = 'Renamed'
    panel.refresh()
    assert panel.tabs.tabText(2) == 'Renamed'
    assert panel.tabs.currentIndex() == 2
    main_window.state.things = [b]
    panel.refresh()
    assert panel.tabs.count() == 2
    assert panel.overview.rowCount() == 1
    main_window.console_handler.handle_command('botdebug')
    assert main_window._bot_debug_window is panel
    main_window.console_handler.handle_command('botdebug off')
    assert not panel.isVisible() and not panel._timer.isActive()
    main_window.console_handler.handle_command('botdebug')
    assert panel.isVisible() and panel._timer.isActive()
    panel.close()
