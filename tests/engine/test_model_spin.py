"""A model Prop's spin animation (engine.model_spin): the maths, the Play
runtime (PropSession), the drawn matrix and the I/O inputs."""

import math

import glm
import numpy as np
import pytest

from engine import model_spin as ms

# -- the maths ------------------------------------------------------------------


REEL_BOUNDS = ((-20.0, -20.0, -3.35), (20.0, 20.0, 6.0))


@pytest.mark.parametrize("axis, bounds, expected", [
    ('x', None, 0), ('Y', None, 1), ('z', None, 2),
    ('auto', REEL_BOUNDS, 2),                                  # a reel: its hub
    ('auto', ((-2, -50, -50), (2, 50, 50)), 0),                # a fan facing X
    ('auto', ((-50, -1, -50), (50, 1, 50)), 1),                # a turntable
    ('auto', None, 2), ('sideways', None, 2),
    ('auto', ((-1, -1, -1), (1, 1, 1)), 2),                    # a cube: Z
    ('auto', ((0, 0, 0), (float('nan'), 1, 1)), 2),
])
def test_axis_index(axis, bounds, expected):
    assert ms.axis_index(axis, bounds) == expected


@pytest.mark.parametrize("axis", [0, 1, 2])
@pytest.mark.parametrize("angle", [0.0, 37.0, 90.0, -120.0])
def test_spin_matrix_turns_as_glm_does(axis, angle):
    expected = glm.rotate(glm.mat4(1.0), glm.radians(angle), glm.vec3(*ms.axis_vector(axis)))
    got = ms.spin_matrix(axis, angle)
    for r in range(3):
        for c in range(3):
            assert got[r][c] == pytest.approx(expected[c][r], abs=1e-6)


def test_speed_and_ramp_are_finite_and_clamped():
    assert ms.spin_speed({'spin_speed': 'fast'}) == ms.SPIN_DEFAULTS['spin_speed']
    assert ms.spin_speed({'spin_speed': float('inf')}) == ms.SPIN_DEFAULTS['spin_speed']
    assert ms.spin_speed({'spin_speed': -1e9}) == -ms.MAX_SPIN_SPEED
    assert ms.spin_ramp({'spin_ramp': -3}) == 0.0
    assert ms.spin_ramp({'spin_ramp': 1e9}) == ms.MAX_SPIN_RAMP


def test_approach_moves_by_at_most_the_rate():
    assert ms.approach(0.0, 100.0, 50.0, 0.5) == 25.0
    assert ms.approach(90.0, 100.0, 50.0, 0.5) == 100.0
    assert ms.approach(100.0, -100.0, 400.0, 0.25) == 0.0
    assert ms.approach(0.0, 100.0, None, 0.01) == 100.0          # no ramp: at once
    assert ms.ramp_rate(180.0, 0.0) is None
    assert ms.ramp_rate(-180.0, 0.5) == 360.0


# -- the Play runtime -------------------------------------------------------------

pytest.importorskip("PyQt5", reason="Prop is an editor-tier Thing")
from editor.things import Prop                                   # noqa: E402
from engine.prop_runtime import PropSession                      # noqa: E402

REEL = 'assets/models/tape_reel_blue.glb'


class _IO:
    def __init__(self):
        self.fired = []

    def fire_output(self, entity, output, **kwargs):
        self.fired.append((entity, output))


class _Collision:
    def compute_model_bounds(self, path):
        return REEL_BOUNDS


def _session(*props):
    logic = type("SpinLogic", (), {})()
    logic.editor_state = type("EditorState", (), {"things": list(props), "brushes": []})()
    logic.player_runtime = type("PlayerRuntime", (), {"player": None})()
    logic.io_manager = _IO()
    logic.collision_runtime = _Collision()
    logic.session_runtime = type("SessionRuntime", (), {"physics_world": None,
                                                        "spatial_grid": None})()
    session = PropSession(logic)
    session.start()
    return session, logic.io_manager


def _reel(**props):
    settings = {'spin_enabled': True, 'spin_speed': 180.0, 'spin_ramp': 0.5}
    settings.update(props)
    return Prop.for_model(REEL, pos=[0, 0, 0], properties=settings)


def _run(session, seconds, step=1.0 / 60.0):
    for _ in range(int(round(seconds / step))):
        session.tick(step, False)


def test_a_model_without_spin_never_turns():
    prop = _reel(spin_enabled=False, spin_autostart=True)
    session, io = _session(prop)
    assert session.start_spin(prop) is False
    _run(session, 1.0)
    assert prop._spin_angle == 0.0 and not io.fired


def test_spinning_at_start_turns_at_full_speed_from_the_first_tick():
    prop = _reel(spin_autostart=True, spin_speed=180.0)
    session, io = _session(prop)
    assert prop._spin_axis == 2                                  # auto: the hub
    _run(session, 0.5)
    assert prop._spin_angle == pytest.approx(90.0, abs=1e-6)


def test_start_spins_up_over_the_ramp_and_fires_on_spin_started():
    prop = _reel(spin_speed=180.0, spin_ramp=0.5)
    session, io = _session(prop)
    assert session.start_spin(prop) is True
    assert io.fired == [(prop, "OnSpinStarted")]
    _run(session, 0.25)
    assert session.spin_speed_of(prop) == pytest.approx(90.0, abs=1.0)
    _run(session, 0.5)
    assert session.spin_speed_of(prop) == pytest.approx(180.0)
    # Spun up linearly over 0.5 s (45 degrees), then 0.25 s at full speed.
    assert prop._spin_angle == pytest.approx(45.0 + 45.0, abs=1.5)
    assert session.start_spin(prop) is False                     # already running


def test_stop_coasts_down_and_fires_on_spin_stopped_once_at_rest():
    prop = _reel(spin_autostart=True, spin_speed=180.0, spin_ramp=0.5)
    session, io = _session(prop)
    assert session.stop_spin(prop) is True
    _run(session, 0.25)
    assert 0.0 < session.spin_speed_of(prop) < 180.0
    assert (prop, "OnSpinStopped") not in io.fired
    _run(session, 0.5)
    assert session.spin_speed_of(prop) == 0.0
    assert io.fired.count((prop, "OnSpinStopped")) == 1
    resting = prop._spin_angle
    _run(session, 0.5)
    assert prop._spin_angle == resting                           # stays where it stopped


def test_toggle_and_reverse_like_a_tape_deck():
    prop = _reel(spin_speed=180.0, spin_ramp=0.0)
    session, io = _session(prop)
    session.toggle_spin(prop)
    _run(session, 0.1)
    assert session.spin_speed_of(prop) == 180.0
    session.reverse_spin(prop)
    _run(session, 0.1)
    assert session.spin_speed_of(prop) == -180.0
    session.toggle_spin(prop)
    _run(session, 0.1)
    assert session.spin_speed_of(prop) == 0.0


def test_set_spin_speed_is_for_this_play_only():
    prop = _reel(spin_autostart=True, spin_speed=180.0, spin_ramp=0.0)
    session, io = _session(prop)
    session.set_spin_speed(prop, 720.0)
    _run(session, 0.1)
    assert session.spin_speed_of(prop) == 720.0
    assert prop.properties['spin_speed'] == 180.0                # the map is untouched


def test_leaving_play_puts_every_model_back_as_placed():
    prop = _reel(spin_autostart=True)
    before = dict(prop.properties)
    session, io = _session(prop)
    _run(session, 0.3)
    assert prop._spin_angle != 0.0
    session.stop()
    assert prop._spin_angle == 0.0
    assert prop.properties == before
    assert '_spin_angle' not in prop.to_dict()['properties']


def test_spinning_does_not_need_a_player():
    """Machinery turns before the player spawns (or with none at all)."""
    prop = _reel(spin_autostart=True)
    session, io = _session(prop)
    assert session.logic.player_runtime.player is None
    _run(session, 0.1)
    assert prop._spin_angle > 0.0


# -- what is drawn ------------------------------------------------------------------


def _drawn_matrix(prop):
    from engine.entity_table import _model_transform_columns
    model, _normal = _model_transform_columns(prop)
    return np.asarray(model, dtype=float).reshape(4, 4).T[:3, :3]   # column-major


def test_the_drawn_model_turns_about_its_own_axis_after_its_placement():
    from engine.logic_collision import model_basis
    prop = _reel(rotation=[20.0, 135.0, 0.0], scale=[2.0, 2.0, 1.0])
    placed = _drawn_matrix(prop)
    assert placed == pytest.approx(model_basis([20.0, 135.0, 0.0], [2.0, 2.0, 1.0]), abs=1e-5)
    prop._spin_axis = 2
    prop._spin_angle = 90.0
    expected = (model_basis([20.0, 135.0, 0.0], [1.0, 1.0, 1.0])
                @ ms.spin_matrix(2, 90.0) @ np.diag([2.0, 2.0, 1.0]))
    assert _drawn_matrix(prop) == pytest.approx(expected, abs=1e-5)


def test_a_spin_change_reaches_the_entity_table():
    from engine.entity_table import EntityTable
    prop = _reel()
    table = EntityTable()
    table.begin_frame([prop], epoch=1)
    still = np.array(table.model_base_matrix[0])
    prop._spin_angle = 45.0                                      # journals the row
    table.begin_frame([prop], epoch=1)
    assert not np.allclose(still, table.model_base_matrix[0])


# -- I/O ----------------------------------------------------------------------------


def test_the_spin_inputs_reach_the_prop_runtime():
    from editor.io_handlers import register_all_input_handlers
    from editor.io_system import IOManager, get_input_names, get_output_names
    for name in ('StartSpin', 'StopSpin', 'ToggleSpin', 'ReverseSpin', 'SetSpinSpeed'):
        assert name in get_input_names('prop')
    for name in ('OnSpinStarted', 'OnSpinStopped'):
        assert name in get_output_names('prop')

    prop = _reel(spin_ramp=0.0)
    session, io = _session(prop)
    mgr = IOManager()
    register_all_input_handlers(mgr)
    logic = session.logic
    logic.prop_runtime = session
    handlers = mgr._input_handlers
    handlers[('prop', 'startspin')](prop, '', logic)
    _run(session, 0.1)
    assert session.spin_speed_of(prop) == 180.0
    handlers[('prop', 'setspinspeed')](prop, '-360', logic)
    _run(session, 0.1)
    assert session.spin_speed_of(prop) == -360.0
    handlers[('prop', 'setspinspeed')](prop, 'not a number', logic)   # ignored
    handlers[('prop', 'reversespin')](prop, '', logic)
    _run(session, 0.1)
    assert session.spin_speed_of(prop) == 360.0
    handlers[('prop', 'stopspin')](prop, '', logic)
    _run(session, 0.1)
    assert session.spin_speed_of(prop) == 0.0
    handlers[('prop', 'togglespin')](prop, '', logic)
    _run(session, 0.1)
    assert session.spin_speed_of(prop) == 360.0
