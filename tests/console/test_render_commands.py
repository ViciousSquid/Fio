"""The console's render commands talk to the editor and the Renderer contract.

* ``r_wireframe`` is the editor's Display box: it selects "Wireframe" and,
  turned off, returns to whatever the box showed before -- the two never
  disagree.
* ``r_renderer`` lists the registry and swaps the live renderer; map logic may
  not run it.
* ``r_waterquality`` works on any renderer that satisfies the contract.
* The old toggles for attributes no renderer had (``r_fog``, ``r_water``,
  ``r_glass``, ``r_lighting``, ``r_deferred``) are gone.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

import editor.console_commands as console_commands  # noqa: E402
from editor.console_commands import ConsoleCommandHandler  # noqa: E402
from tests.helpers.renderers import StubRenderer  # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def console(main_window, monkeypatch):
    logged = []
    monkeypatch.setattr(console_commands, "debug_log",
                        lambda level, message: logged.append((level, message)))
    handler = ConsoleCommandHandler(main_window)
    return handler, main_window, logged


def test_the_dead_render_toggles_are_gone(console):
    handler, _window, logged = console
    for name in ("r_fog", "r_water", "r_glass", "r_lighting", "r_deferred",
                 "fog", "water", "glass", "lighting", "deferred"):
        assert name not in handler.commands, name
    handler.handle_command("r_fog")
    assert logged[-1][0] == "Error" and "Unknown command" in logged[-1][1]


def test_r_wireframe_toggles_the_display_box(console):
    handler, window, _logged = console
    combo, view = window.display_mode_combobox, window.view_3d
    assert combo.currentText() == "Solid Lit"

    handler.handle_command("r_wireframe")
    assert combo.currentText() == "Wireframe"
    assert view.brush_display_mode == "Wireframe"

    handler.handle_command("wireframe")
    assert combo.currentText() == "Solid Lit"
    assert view.brush_display_mode == "Solid Lit"


def test_r_wireframe_off_returns_to_the_mode_it_replaced(console):
    handler, window, _logged = console
    combo, view = window.display_mode_combobox, window.view_3d
    combo.setCurrentText("Textured")

    handler.handle_command("r_wireframe on")
    assert view.brush_display_mode == "Wireframe"
    handler.handle_command("r_wireframe on")            # already on: no change
    assert combo.currentText() == "Wireframe"
    handler.handle_command("r_wireframe off")
    assert combo.currentText() == "Textured"
    assert view.brush_display_mode == "Textured"
    handler.handle_command("r_wireframe off")           # already off: no change
    assert combo.currentText() == "Textured"


def test_the_display_box_and_r_wireframe_agree(console):
    """Choosing Wireframe in the box is the same state r_wireframe turns off."""
    handler, window, _logged = console
    combo = window.display_mode_combobox
    combo.setCurrentText("Wireframe")
    handler.handle_command("r_wireframe")
    assert combo.currentText() == "Solid Lit"
    assert window.view_3d.brush_display_mode == "Solid Lit"


def test_r_wireframe_rejects_a_bad_argument(console):
    handler, window, logged = console
    handler.handle_command("r_wireframe sideways")
    assert window.display_mode_combobox.currentText() == "Solid Lit"
    assert logged[-1][0] == "Warning"


def test_r_waterquality_parses_its_argument(console):
    handler, window, _logged = console
    window.view_3d.renderer = StubRenderer(None)
    try:
        handler.handle_command("r_waterquality cheap")
        assert window.view_3d.renderer.water_quality == "cheap"
        handler.handle_command("waterquality expensive")
        assert window.view_3d.renderer.water_quality == "expensive"
        handler.handle_command("r_waterquality ultra")
        assert window.view_3d.renderer.water_quality == "expensive"
        handler.handle_command("r_waterquality")      # no argument toggles
        assert window.view_3d.renderer.water_quality == "cheap"
    finally:
        window.view_3d.renderer = None


def test_r_renderer_lists_the_registry(console):
    handler, _window, logged = console
    handler.handle_command("r_renderer")
    assert logged[-1][0] == "Info"
    assert "Forward" in logged[-1][1] and "(active)" in logged[-1][1]


def test_r_renderer_switches_through_the_view(console, monkeypatch):
    handler, window, logged = console
    asked = []
    monkeypatch.setattr(window.view_3d, "switch_renderer",
                        lambda name: asked.append(name) or True)
    handler.handle_command("r_renderer forward")        # case-insensitive
    assert asked == ["Forward"]
    handler.handle_command("r_renderer NoSuchRenderer")
    assert asked == ["Forward"]
    assert logged[-1][0] == "Warning"


def test_map_logic_cannot_swap_the_renderer(console, monkeypatch):
    handler, window, logged = console
    asked = []
    monkeypatch.setattr(window.view_3d, "switch_renderer",
                        lambda name: asked.append(name) or True)
    handler.handle_command("r_renderer Forward", from_map=True)
    assert asked == []
    assert logged[-1][0] == "Error"
