"""One BigWorldSettings per map.

A second settings entity would leave Big World's config ambiguous (only the
first is ever read), so the type is a per-map singleton: placement selects the
existing one instead, and clone/paste refuse to copy it while one exists.
"""
from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt5", reason="the plugin registries are editor-tier")

from plugins.integration import _singleton_blocked, singleton_instance  # noqa: E402
from plugins.manager import get_manager, load_plugins  # noqa: E402


@pytest.fixture
def settings_cls():
    load_plugins()
    from plugins.bigworld.entities import BigWorldSettings
    return BigWorldSettings


def test_bigworld_settings_is_a_per_map_singleton(settings_cls):
    assert get_manager().is_singleton_entity('bigworldsettings')
    assert get_manager().is_singleton_entity('BigWorldSettings')


def test_placing_a_second_one_selects_the_first(settings_cls):
    existing = settings_cls(pos=[0, 40, 0])
    toasts, selected = [], []
    window = SimpleNamespace(
        set_selected_object=selected.append,
        show_toast=lambda text, is_error=False: toasts.append(text))
    state = SimpleNamespace(things=[existing])

    assert _singleton_blocked(window, state, 'bigworldsettings') is True
    assert selected == [existing] and toasts
    assert _singleton_blocked(window, SimpleNamespace(things=[]),
                              'bigworldsettings') is False


def test_clone_and_paste_drop_copies_of_a_placed_singleton(settings_cls):
    from editor.main_window import MainWindow
    from editor.things import Light

    settings = settings_cls(pos=[0, 40, 0])
    light = Light(pos=[0, 0, 0])
    brush = {'name': 'b', 'pos': [0, 0, 0], 'size': [64, 64, 64]}
    toasts = []
    window = SimpleNamespace(
        state=SimpleNamespace(things=[settings, light]),
        show_toast=lambda text, is_error=False: toasts.append(text))

    kept, skipped = MainWindow._drop_singleton_copies(
        window, [settings, light, brush])
    assert kept == [light, brush] and skipped == 1 and toasts

    # Copied, then deleted: pasting it back is fine.
    window.state.things = [light]
    toasts.clear()
    kept, skipped = MainWindow._drop_singleton_copies(window, [settings])
    assert kept == [settings] and skipped == 0 and not toasts
    assert singleton_instance(window.state.things, 'bigworldsettings') is None
