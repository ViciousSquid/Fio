"""One BigWorldSettings per map through the real editor host."""

import pytest

pytest.importorskip("PyQt5", reason="the plugin registries are editor-tier")

from editor.view_2d import _singleton_blocked, singleton_instance
from plugins.manager import get_manager, load_plugins

pytestmark = pytest.mark.qt


@pytest.fixture
def settings_cls():
    load_plugins()
    from plugins.bigworld.entities import BigWorldSettings

    return BigWorldSettings


def test_bigworld_settings_is_a_per_map_singleton(settings_cls):
    assert get_manager().is_singleton_entity("bigworldsettings")
    assert get_manager().is_singleton_entity("BigWorldSettings")


def test_placing_a_second_one_selects_the_first(
    settings_cls, main_window, monkeypatch
):
    existing = settings_cls(pos=[0, 40, 0])
    main_window.state.things[:] = [existing]

    toasts = []
    monkeypatch.setattr(
        main_window,
        "show_toast",
        lambda text, is_error=False: toasts.append(text),
    )

    assert _singleton_blocked(
        main_window, main_window.state, "bigworldsettings"
    ) is True
    assert main_window.state.selected_objects == [existing]
    assert toasts

    main_window.state.things[:] = []
    main_window.state.selected_objects = []
    toasts.clear()

    assert _singleton_blocked(
        main_window, main_window.state, "bigworldsettings"
    ) is False
    assert main_window.state.selected_objects == []
    assert toasts == []


def test_clone_and_paste_drop_copies_of_a_placed_singleton(
    settings_cls, main_window, monkeypatch
):
    from editor.things import Light

    settings = settings_cls(pos=[0, 40, 0])
    light = Light(pos=[0, 0, 0])
    brush = {"name": "b", "pos": [0, 0, 0], "size": [64, 64, 64]}
    main_window.state.things[:] = [settings, light]

    toasts = []
    monkeypatch.setattr(
        main_window,
        "show_toast",
        lambda text, is_error=False: toasts.append(text),
    )

    kept, skipped = main_window._drop_singleton_copies(
        [settings, light, brush]
    )
    assert kept == [light, brush]
    assert skipped == 1
    assert toasts

    main_window.state.things[:] = [light]
    toasts.clear()

    kept, skipped = main_window._drop_singleton_copies([settings])
    assert kept == [settings]
    assert skipped == 0
    assert toasts == []
    assert singleton_instance(
        main_window.state.things, "bigworldsettings"
    ) is None
