"""Small things the UI thread did that it should not.

* The spatial-grid overlay walked ``grid.cells`` live while the logic thread
  repopulates it (clear, then refill) -- "dictionary changed size during
  iteration" in a paint.
* The gamepad poll pumped SDL every 16 ms whether or not anything read the
  pad, and never took the events it queued off the queue.
* ``RenderState.reset()`` -- run on every swap -- built about twenty new
  objects, though its docstring says it exists to avoid exactly that.
"""

import glm
import pytest

pytest.importorskip("PyQt5", reason="the 3D view's helpers import Qt")

from engine import view_debug_draw                    # noqa: E402
from engine.spatial_grid import SpatialGrid            # noqa: E402
from engine.threaded_game_state import RenderState    # noqa: E402

pytestmark = pytest.mark.qt


class _GL:
    """The GL calls the grid overlay makes, recording the vertex upload."""

    GL_FALSE = 0
    GL_ARRAY_BUFFER = 1
    GL_DYNAMIC_DRAW = 2
    GL_LINES = 3

    def __init__(self):
        self.drawn = []

    def __getattr__(self, name):
        return lambda *args: None

    def glDrawArrays(self, mode, first, count):
        self.drawn.append(count)


class _RepopulatingCells(dict):
    """A grid's cells, refilled by "another thread" while they are walked."""

    def __iter__(self):
        for i, key in enumerate(dict.__iter__(self)):
            if i == 1:
                self[(99, i)] = []       # the logic thread's populate()
            yield key                    # ...and the walk raises, as a dict's does


def _debug_draw(main_window, monkeypatch, cells):
    """The real view's debug drawer over a real play-mode grid holding *cells*."""
    view = main_window.view_3d
    logic = view.logic_thread
    if logic is None:
        pytest.skip("no logic thread in this window")
    grid = SpatialGrid(cell_size=512.0)
    grid.cells = cells
    monkeypatch.setattr(logic.session_runtime, "spatial_grid", grid)
    monkeypatch.setattr(view, "debug_shader", 1, raising=False)
    for name in ("_debug_proj_loc", "_debug_view_loc", "_debug_color_loc",
                 "debug_vao", "debug_vbo"):
        monkeypatch.setattr(view, name, 0, raising=False)
    return view.debug_draw


def test_the_grid_overlay_survives_a_repopulate_mid_paint(main_window, monkeypatch):
    gl = _GL()
    monkeypatch.setattr(view_debug_draw, "gl", gl)
    cells = _RepopulatingCells({(0, 0): [], (1, 0): [], (2, 0): []})
    # Must not raise "dictionary changed size during iteration".
    _debug_draw(main_window, monkeypatch, cells)._render_spatial_grid(
        glm.mat4(1.0), glm.mat4(1.0))


def test_the_grid_overlay_draws_every_cell_of_a_still_grid(main_window, monkeypatch):
    gl = _GL()
    monkeypatch.setattr(view_debug_draw, "gl", gl)
    cells = {(0, 0): [], (1, 0): [], (0, 1): []}
    _debug_draw(main_window, monkeypatch, cells)._render_spatial_grid(
        glm.mat4(1.0), glm.mat4(1.0))
    assert gl.drawn == [3 * 8]


class _Pad:
    """A connected controller (the SDL device leaf), at rest."""

    def get_axis(self, index):
        return 0.0

    def get_button(self, index):
        return 0


def _poll_counts(main_window, monkeypatch, play_mode, splitscreen_mode):
    import pygame
    calls = []
    monkeypatch.setattr(pygame.event, "pump", lambda: calls.append("pump"))
    monkeypatch.setattr(pygame.event, "clear", lambda *a, **k: calls.append("clear"))
    view = main_window.view_3d
    inputs = []
    monkeypatch.setattr(view, "gamepad", _Pad())
    monkeypatch.setattr(view, "play_mode", play_mode)
    monkeypatch.setattr(view, "splitscreen_mode", splitscreen_mode)
    monkeypatch.setattr(view.game_state, "set_p2_input", lambda *a: inputs.append(a))
    for _ in range(60):
        view._poll_gamepad()
    return calls, inputs


@pytest.mark.perf
def test_the_gamepad_is_not_polled_outside_split_screen_play(main_window, monkeypatch):
    for state in ((False, False), (True, False), (False, True)):
        calls, inputs = _poll_counts(main_window, monkeypatch, *state)
        assert calls == [] and inputs == [], state


@pytest.mark.perf
def test_split_screen_polling_drains_what_it_pumps(main_window, monkeypatch):
    calls, inputs = _poll_counts(main_window, monkeypatch, True, True)
    assert calls == ["pump", "clear"] * 60
    assert len(inputs) == 60


@pytest.mark.perf
def test_render_state_reset_reuses_its_defaults():
    first, second = RenderState(), RenderState()
    first.reset()
    second.reset()
    shared = ("camera_view_matrix", "projection_matrix", "editor_camera_pos",
              "player_pos", "player2_pos", "player2_view_matrix", "underwater_tint",
              "entity_refs", "visible_thing_slots", "thing_hidden", "render_refs",
              "visible_brush_slots", "all_brush_slots", "collected_keys",
              "bullet_marks", "projectiles", "monster_debug_rays")
    rebuilt = [name for name in shared if getattr(first, name) is not getattr(second, name)]
    assert rebuilt == [], "reset() allocated new defaults for: %s" % rebuilt
    tables = (first.render_table, first.entity_table)
    first.reset()
    assert (first.render_table, first.entity_table) == tables


def test_reset_defaults_are_the_documented_values():
    state = RenderState()
    state.player_pos = glm.vec3(5, 6, 7)
    state.collected_keys = {"red"}
    state.reset()
    assert state.player_pos == glm.vec3(0, 0, 0)
    assert state.camera_view_matrix == glm.mat4(1.0)
    assert not state.collected_keys and "red" not in state.collected_keys
    assert len(state.visible_brush_slots) == 0 and state.projectiles.shape == (0, 3)
    assert list(state.underwater_tint) == [0.0, 0.4, 0.6]
