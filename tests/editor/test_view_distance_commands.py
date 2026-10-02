"""Tests for the view-distance and fog console commands.

These are the commands the I/O system drives: a ``logic_command`` entity fires
``RunCommand`` with a line like ``r_fogcolor 40 30 60``, the logic thread
queues it and ``QtGameView._process_console_command_queue`` hands it to the
same :class:`ConsoleCommandHandler` tested here. So what a trigger brush can
do to the weather is exactly what these tests exercise.

The handler is driven against a stand-in for the main window rather than a
real editor: what is under test is the parsing and the effect on the shared
:class:`~engine.view_distance.ViewDistance`, not Qt.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from editor.console_commands import ConsoleCommandHandler  # noqa: E402
from engine.view_distance import (  # noqa: E402
    AUTO_FOG_END_FRAC, AUTO_FOG_START_FRAC, MAX_VIEW_DISTANCE,
    MIN_VIEW_DISTANCE, ViewDistance,
)

# Qt tier: PyQt5 must be importable. No display, no GPU — nothing here builds
# a widget, but console_commands imports Qt at module scope.
pytestmark = pytest.mark.qt


class _Spinbox:
    """Just enough QSpinBox for the handler: a clamped value."""

    def __init__(self, value, low=int(MIN_VIEW_DISTANCE), high=int(MAX_VIEW_DISTANCE)):
        self._value = value
        self._low, self._high = low, high

    def setValue(self, value):
        self._value = max(self._low, min(self._high, int(value)))

    def value(self):
        return self._value


class _View3D:
    """A stand-in viewport that mirrors QtGameView's view-distance contract."""

    def __init__(self):
        self.view_distance = ViewDistance()
        self.cull_distance = self.view_distance.distance
        self.play_mode = False
        self.renderer = None
        self.logic_thread = None
        self.updates = 0

    def set_cull_distance(self, distance):
        # Mirrors QtGameView.set_cull_distance, repaint included — the
        # repaint is part of that contract, and the console relies on it.
        self.view_distance.distance = float(distance)
        self.cull_distance = self.view_distance.distance
        self.update()

    def update(self):
        self.updates += 1


class _MainWindow:
    def __init__(self):
        from editor.editor_state import EditorState

        self.state = EditorState()
        self.view_3d = _View3D()
        self.cull_dist_spinbox = _Spinbox(int(self.view_3d.view_distance.distance))


@pytest.fixture
def handler():
    return ConsoleCommandHandler(_MainWindow())


@pytest.fixture
def vd(handler):
    return handler.main_window.view_3d.view_distance


def run(handler, line):
    """Dispatch a whole command line the way the console (and I/O) does."""
    handler.handle_command(line)


# ---------------------------------------------------------------------------
# Registration: every documented name and alias has to dispatch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "r_viewdistance", "r_culldistance", "viewdistance", "culldistance", "farplane",
    "r_distancefog", "distancefog",
    "r_fogdistance", "fogdistance", "fogdist",
    "r_fogstart", "fogstart", "r_fogend", "fogend",
    "r_fogdensity", "fogdensity",
    "r_fogcolor", "r_fogcolour", "fogcolor", "fogcolour",
    "ambient", "r_ambient",
])
def test_every_name_is_registered(handler, name):
    assert name in handler.commands


def test_the_volumetric_fog_toggle_is_left_alone(handler):
    """`fog` still means fog *brushes*; far-plane fog is `distancefog`."""
    assert handler.commands["fog"].__func__ is ConsoleCommandHandler.cmd_render_fog
    assert (handler.commands["distancefog"].__func__
            is ConsoleCommandHandler.cmd_distance_fog)


# ---------------------------------------------------------------------------
# View distance
# ---------------------------------------------------------------------------

def test_setting_the_view_distance(handler, vd):
    run(handler, "r_viewdistance 2500")
    assert vd.distance == 2500.0
    assert vd.far_plane == 2500.0


def test_it_goes_through_the_viewport_so_lod_and_the_logic_thread_follow(handler, vd):
    # Writing ViewDistance directly would skip the viewport's LOD refresh, so
    # the command must call set_cull_distance rather than poke the object.
    run(handler, "r_viewdistance 3000")
    assert handler.main_window.view_3d.cull_distance == 3000.0


def test_the_editor_spinbox_is_kept_in_step(handler):
    run(handler, "r_viewdistance 6000")
    assert handler.main_window.cull_dist_spinbox.value() == 6000


def test_an_out_of_range_request_is_clamped_not_rejected(handler, vd):
    run(handler, "r_viewdistance 999999")
    assert vd.distance == MAX_VIEW_DISTANCE
    run(handler, "r_viewdistance 1")
    assert vd.distance == MIN_VIEW_DISTANCE


def test_garbage_leaves_the_setting_untouched(handler, vd):
    before = vd.distance
    run(handler, "r_viewdistance banana")
    assert vd.distance == before


def test_no_argument_reports_without_changing_anything(handler, vd):
    before = vd.distance
    run(handler, "r_viewdistance")
    assert vd.distance == before


def test_aliases_reach_the_same_setting(handler, vd):
    run(handler, "farplane 1800")
    assert vd.distance == 1800.0
    run(handler, "culldistance 2600")
    assert vd.distance == 2600.0


# ---------------------------------------------------------------------------
# Fog band
# ---------------------------------------------------------------------------

def test_fog_distance_takes_a_start_and_an_end(handler, vd):
    run(handler, "r_viewdistance 8192")
    run(handler, "r_fogdistance 5000 7500")
    assert vd.resolve() == (5000.0, 7500.0)


def test_fog_distance_with_one_number_sets_the_end(handler, vd):
    run(handler, "r_viewdistance 8192")
    run(handler, "r_fogdistance 6000")
    start, end = vd.resolve()
    assert end == 6000.0
    assert start == pytest.approx(8192.0 * AUTO_FOG_START_FRAC)


def test_auto_returns_the_band_to_tracking_the_view_distance(handler, vd):
    run(handler, "r_viewdistance 8192")
    run(handler, "r_fogdistance 1000 2000")
    run(handler, "r_fogdistance auto")
    assert vd.resolve() == (8192.0 * AUTO_FOG_START_FRAC, 8192.0 * AUTO_FOG_END_FRAC)


def test_start_and_end_can_be_set_separately(handler, vd):
    run(handler, "r_viewdistance 8192")
    run(handler, "r_fogstart 4000")
    run(handler, "r_fogend 7000")
    assert vd.resolve() == (4000.0, 7000.0)
    run(handler, "r_fogstart auto")
    assert vd.resolve()[0] == pytest.approx(8192.0 * AUTO_FOG_START_FRAC)


def test_a_fog_end_past_the_clip_is_pulled_back_in_front_of_it(handler, vd):
    run(handler, "r_viewdistance 4096")
    run(handler, "r_fogend 99999")
    _, end = vd.resolve()
    assert end < vd.far_plane
    assert vd.fog_factor(vd.far_plane) == 1.0


def test_shrinking_the_view_distance_keeps_the_fog_ahead_of_the_clip(handler, vd):
    run(handler, "r_fogdistance 5000 7500")
    run(handler, "r_viewdistance 2000")
    assert vd.resolve()[1] < vd.far_plane
    assert vd.fog_factor(vd.far_plane) == 1.0


def test_bad_fog_distance_arguments_change_nothing(handler, vd):
    before = vd.resolve()
    run(handler, "r_fogdistance near far")
    assert vd.resolve() == before


# ---------------------------------------------------------------------------
# Toggle, density, colour
# ---------------------------------------------------------------------------

def test_distance_fog_toggles_and_takes_an_explicit_state(handler, vd):
    assert vd.fog_enabled is True
    run(handler, "r_distancefog")
    assert vd.fog_enabled is False
    run(handler, "r_distancefog on")
    assert vd.fog_enabled is True
    run(handler, "r_distancefog off")
    assert vd.fog_enabled is False


def test_fog_density(handler, vd):
    run(handler, "r_fogdensity 0.004")
    assert vd.fog_density == pytest.approx(0.004)
    run(handler, "r_fogdensity -1")
    assert vd.fog_density == 0.0


def test_fog_colour_accepts_bytes(handler, vd):
    run(handler, "r_fogcolor 255 128 0")
    r, g, b = vd.fog_color
    assert (r, b) == (1.0, 0.0)
    assert g == pytest.approx(128 / 255.0)


def test_fog_colour_accepts_floats(handler, vd):
    run(handler, "r_fogcolour 0.2 0.4 0.6")
    assert vd.fog_color == (0.2, 0.4, 0.6)


def test_a_malformed_colour_leaves_the_old_one(handler, vd):
    before = vd.fog_color
    run(handler, "r_fogcolor 255 128")
    assert vd.fog_color == before
    run(handler, "r_fogcolor red green blue")
    assert vd.fog_color == before


# ---------------------------------------------------------------------------
# Ambient
# ---------------------------------------------------------------------------

def test_ambient_takes_a_single_level(handler, vd):
    run(handler, "ambient 0.35")
    assert vd.ambient == (0.35, 0.35, 0.35)


def test_ambient_takes_a_colour(handler, vd):
    run(handler, "ambient 0.2 0.1 0.4")
    assert vd.ambient == (0.2, 0.1, 0.4)


def test_ambient_takes_a_byte_level(handler, vd):
    run(handler, "ambient 64")
    assert vd.ambient[0] == pytest.approx(64 / 255.0)


def test_ambient_off_restores_the_stock_look(handler, vd):
    run(handler, "ambient 0.5")
    run(handler, "ambient off")
    assert vd.ambient == (0.0, 0.0, 0.0)
    run(handler, "ambient 0.5")
    run(handler, "ambient 0")
    assert vd.ambient == (0.0, 0.0, 0.0)


def test_ambient_adds_nothing_to_the_world(handler, vd):
    """The "no entity" half of the promise: the scene is left alone."""
    state = handler.main_window.state
    before_things = list(state.things)
    before_brushes = list(state.brushes)
    run(handler, "ambient 0.5")
    assert state.things == before_things
    assert state.brushes == before_brushes


def test_bad_ambient_arguments_change_nothing(handler, vd):
    run(handler, "ambient 0.3")
    run(handler, "ambient bright")
    assert vd.ambient == (0.3, 0.3, 0.3)


# ---------------------------------------------------------------------------
# Repainting, so an edit from the console (or I/O) shows up at once
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "r_viewdistance 3000", "r_distancefog off", "r_fogdistance 1000 2000",
    "r_fogstart 900", "r_fogend 1900", "r_fogdensity 0.01",
    "r_fogcolor 10 20 30", "ambient 0.4",
])
def test_every_setter_asks_the_view_to_repaint(handler, line):
    before = handler.main_window.view_3d.updates
    run(handler, line)
    assert handler.main_window.view_3d.updates > before, (
        f"'{line}' changed a setting without repainting the 3D view")


# ---------------------------------------------------------------------------
# The readout
# ---------------------------------------------------------------------------

def test_the_commands_survive_having_no_3d_view():
    """Console input must not raise before the viewport exists."""
    class _Bare:
        state = None
        view_3d = None

    bare = ConsoleCommandHandler.__new__(ConsoleCommandHandler)
    bare.main_window = _Bare()
    assert bare._get_view_distance() is None
