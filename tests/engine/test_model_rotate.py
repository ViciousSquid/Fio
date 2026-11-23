"""A model Prop's Rotate animation (engine.model_rotate): the maths, the three
modes, the Play runtime (PropSession), the drawn matrix and the I/O inputs."""

import math

import glm
import numpy as np
import pytest

from engine import model_rotate as mr

REEL_BOUNDS = ((-20.0, -20.0, -3.35), (20.0, 20.0, 6.0))


# -- the maths ------------------------------------------------------------------

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
    assert mr.axis_index(axis, bounds) == expected


@pytest.mark.parametrize("axis", [0, 1, 2])
@pytest.mark.parametrize("angle", [0.0, 37.0, 90.0, -120.0])
def test_rotate_matrix_turns_as_glm_does(axis, angle):
    expected = glm.rotate(glm.mat4(1.0), glm.radians(angle), glm.vec3(*mr.axis_vector(axis)))
    got = mr.rotate_matrix(axis, angle)
    for r in range(3):
        for c in range(3):
            assert got[r][c] == pytest.approx(expected[c][r], abs=1e-6)


def test_settings_are_finite_and_clamped():
    assert mr.rotate_speed({'rotate_speed': 'fast'}) == mr.ROTATE_DEFAULTS['rotate_speed']
    assert mr.rotate_speed({'rotate_speed': float('inf')}) == mr.ROTATE_DEFAULTS['rotate_speed']
    assert mr.rotate_speed({'rotate_speed': -1e9}) == -mr.MAX_ROTATE_SPEED
    assert mr.rotate_ease({'rotate_ease': -3}) == 0.0
    assert mr.rotate_ease({'rotate_ease': 1e9}) == mr.MAX_ROTATE_EASE
    assert mr.rotate_angle({'rotate_angle': 1e9}) == mr.MAX_ROTATE_ANGLE
    assert mr.rotate_mode({'rotate_mode': 'Swing'}) == mr.SWING
    assert mr.rotate_mode({'rotate_mode': 'wobble'}) == mr.CONTINUOUS


def _motion(mode, running=True, **props):
    settings = {'rotate_mode': mode, 'rotate_speed': 90.0, 'rotate_angle': 90.0,
                'rotate_ease': 0.0}
    settings.update(props)
    return mr.RotateMotion(settings, running=running)


def _steps(motion, seconds, step=1.0 / 120.0):
    events = []
    for _ in range(int(round(seconds / step))):
        events += motion.step(step)
    return events


# -- Continuous ---------------------------------------------------------------------

def test_continuous_turns_at_speed_and_spins_up_over_its_ease():
    m = _motion(mr.CONTINUOUS, running=False, rotate_speed=180.0, rotate_ease=0.5)
    assert m.start()
    _steps(m, 0.25)
    assert m.velocity == pytest.approx(90.0, abs=1.0)
    _steps(m, 0.25)
    _steps(m, 0.25)
    assert m.velocity == 180.0
    # Spun up linearly over 0.5 s (45 degrees), then 0.25 s at full speed.
    assert m.angle == pytest.approx(45.0 + 45.0, abs=1.5)


def test_continuous_coasts_to_rest_and_reports_it_once():
    m = _motion(mr.CONTINUOUS, rotate_speed=180.0, rotate_ease=0.5)
    m.stop()
    events = _steps(m, 1.0)
    assert events == ['stopped'] and m.velocity == 0.0 and not m.moving
    rest = m.angle
    _steps(m, 0.5)
    assert m.angle == rest


def test_continuous_reverse_and_set_speed():
    m = _motion(mr.CONTINUOUS, rotate_speed=180.0)
    _steps(m, 0.1)
    m.reverse()
    _steps(m, 0.1)
    assert m.velocity == -180.0
    m.set_speed(720.0)
    _steps(m, 0.1)
    assert m.velocity == -720.0


# -- Swing -----------------------------------------------------------------------------

def test_swing_sweeps_its_arc_centred_on_the_placement():
    m = _motion(mr.SWING, rotate_angle=90.0, rotate_speed=90.0)
    seen = []
    for _ in range(int(10 / (1 / 120))):
        m.step(1 / 120)
        seen.append(m.angle)
    assert max(seen) == pytest.approx(45.0, abs=0.1)
    assert min(seen) == pytest.approx(-45.0, abs=0.1)


def test_swing_passes_the_middle_at_speed_and_slows_at_the_ends():
    m = _motion(mr.SWING, rotate_angle=90.0, rotate_speed=90.0)
    a0 = m.angle
    m.step(0.01)
    middle = abs(m.angle - a0) / 0.01
    assert middle == pytest.approx(90.0, rel=0.02)
    # A quarter of the way round the sine: at the end of the arc, nearly still.
    period = 2 * math.pi / (90.0 / 45.0)
    _steps(m, period / 4 - 0.01)
    before = m.angle
    m.step(0.01)
    assert abs(m.angle - before) / 0.01 < 10.0


def test_swing_eases_to_rest_where_it_is_when_stopped():
    m = _motion(mr.SWING, rotate_angle=90.0, rotate_speed=90.0, rotate_ease=0.5)
    _steps(m, 0.3)
    m.stop()
    events = _steps(m, 1.0)
    assert events == ['stopped']
    rest = m.angle
    assert abs(rest) > 1.0                       # it stopped where it was
    _steps(m, 1.0)
    assert m.angle == rest
    m.start()                                    # and carries on from there
    _steps(m, 0.2)
    assert m.angle != rest


def test_swing_reverse_swings_back_from_where_it_is():
    m = _motion(mr.SWING, rotate_angle=90.0, rotate_speed=90.0)
    _steps(m, 0.2)
    rising = m.angle
    m.reverse()
    m.step(0.05)
    assert m.angle < rising


# -- Turn ------------------------------------------------------------------------------

def test_turn_eases_to_its_angle_stops_there_and_turns_back():
    m = _motion(mr.TURN, running=False, rotate_angle=90.0, rotate_speed=90.0)
    assert m.angle == 0.0
    m.start()
    m.step(0.1)
    early = m.angle
    m.step(0.1)
    assert m.angle - early > early               # eases in: speeding up
    events = _steps(m, 1.5)
    assert events == ['stopped'] and m.angle == pytest.approx(90.0)
    _steps(m, 1.0)
    assert m.angle == pytest.approx(90.0)        # stays turned
    m.stop()                                     # and back again
    events = _steps(m, 1.5)
    assert events == ['stopped'] and m.angle == pytest.approx(0.0)


def test_turn_takes_angle_over_speed_seconds():
    m = _motion(mr.TURN, running=False, rotate_angle=-180.0, rotate_speed=90.0)
    m.start()
    _steps(m, 1.0)
    assert m.angle == pytest.approx(-90.0, abs=1.0)            # half way, the other way
    _steps(m, 1.05)
    assert m.angle == pytest.approx(-180.0)


def test_turn_reverse_turns_back_and_autostart_starts_turned():
    m = _motion(mr.TURN, rotate_angle=60.0)
    assert m.angle == pytest.approx(60.0)        # Rotating at start: already turned
    m.reverse()
    _steps(m, 2.0)
    assert m.angle == pytest.approx(0.0)


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
    logic = type("RotateLogic", (), {})()
    logic.editor_state = type("EditorState", (), {"things": list(props), "brushes": []})()
    logic.player_runtime = type("PlayerRuntime", (), {"player": None})()
    logic.io_manager = _IO()
    logic.collision_runtime = _Collision()
    logic.session_runtime = type("SessionRuntime", (), {"physics_world": None,
                                                        "spatial_grid": None})()
    session = PropSession(logic)
    session.start()
    return session, logic.io_manager


def _model(**props):
    settings = {'rotate_enabled': True, 'rotate_speed': 180.0, 'rotate_ease': 0.5}
    settings.update(props)
    return Prop.for_model(REEL, pos=[0, 0, 0], properties=settings)


def _run(session, seconds, step=1.0 / 60.0):
    for _ in range(int(round(seconds / step))):
        session.tick(step, False)


def test_a_model_without_rotate_never_turns():
    prop = _model(rotate_enabled=False, rotate_autostart=True)
    session, io = _session(prop)
    assert session.start_rotate(prop) is False
    _run(session, 1.0)
    assert prop._rotate_angle == 0.0 and not io.fired


def test_every_new_model_can_rotate_but_does_not_by_default():
    prop = Prop.for_model('assets/models/Oil_Drum.obj', pos=[0, 0, 0])
    for key, value in mr.ROTATE_DEFAULTS.items():
        assert prop.properties[key] == value
    assert prop.properties['rotate_enabled'] is False


def test_rotating_at_start_turns_from_the_first_tick():
    prop = _model(rotate_autostart=True, rotate_speed=180.0)
    session, io = _session(prop)
    assert prop._rotate_axis == 2                                # auto: the hub
    _run(session, 0.5)
    assert prop._rotate_angle == pytest.approx(90.0, abs=1e-6)


def test_start_and_stop_fire_their_outputs():
    prop = _model(rotate_speed=180.0, rotate_ease=0.25)
    session, io = _session(prop)
    assert session.start_rotate(prop) is True
    assert io.fired == [(prop, "OnRotateStarted")]
    assert session.start_rotate(prop) is False                   # already running
    _run(session, 0.5)
    session.stop_rotate(prop)
    _run(session, 0.5)
    assert io.fired.count((prop, "OnRotateStopped")) == 1


def test_a_turn_model_reports_each_end():
    prop = _model(rotate_mode='turn', rotate_angle=90.0, rotate_speed=180.0)
    session, io = _session(prop)
    session.toggle_rotate(prop)
    _run(session, 1.0)
    assert prop._rotate_angle == pytest.approx(90.0)
    session.reverse_rotate(prop)                                 # Turn: back
    _run(session, 1.0)
    assert prop._rotate_angle == pytest.approx(0.0, abs=1e-6)
    assert [o for _, o in io.fired] == ["OnRotateStarted", "OnRotateStopped",
                                         "OnRotateStopped"]


def test_set_rotate_speed_is_for_this_play_only():
    prop = _model(rotate_autostart=True, rotate_speed=180.0, rotate_ease=0.0)
    session, io = _session(prop)
    session.set_rotate_speed(prop, 720.0)
    _run(session, 0.1)
    assert session.rotate_velocity_of(prop) == 720.0
    assert prop.properties['rotate_speed'] == 180.0              # the map is untouched


def test_leaving_play_puts_every_model_back_as_placed():
    prop = _model(rotate_autostart=True, rotate_mode='swing')
    before = dict(prop.properties)
    session, io = _session(prop)
    _run(session, 0.3)
    assert prop._rotate_angle != 0.0
    session.stop()
    assert prop._rotate_angle == 0.0
    assert prop.properties == before
    assert '_rotate_angle' not in prop.to_dict()['properties']


def test_rotating_does_not_need_a_player():
    """Machinery turns before the player spawns (or with none at all)."""
    prop = _model(rotate_autostart=True)
    session, io = _session(prop)
    assert session.logic.player_runtime.player is None
    _run(session, 0.1)
    assert prop._rotate_angle > 0.0


# -- what is drawn ------------------------------------------------------------------


def _drawn_matrix(prop):
    from engine.entity_table import _model_transform_columns
    model, _normal = _model_transform_columns(prop)
    return np.asarray(model, dtype=float).reshape(4, 4).T[:3, :3]   # column-major


def test_the_drawn_model_turns_about_its_own_axis_after_its_placement():
    from engine.logic_collision import model_basis
    prop = _model(rotation=[20.0, 135.0, 0.0], scale=[2.0, 2.0, 1.0])
    placed = _drawn_matrix(prop)
    assert placed == pytest.approx(model_basis([20.0, 135.0, 0.0], [2.0, 2.0, 1.0]), abs=1e-5)
    prop._rotate_axis = 2
    prop._rotate_angle = 90.0
    expected = (model_basis([20.0, 135.0, 0.0], [1.0, 1.0, 1.0])
                @ mr.rotate_matrix(2, 90.0) @ np.diag([2.0, 2.0, 1.0]))
    assert _drawn_matrix(prop) == pytest.approx(expected, abs=1e-5)


def test_a_rotation_change_reaches_the_entity_table():
    from engine.entity_table import EntityTable
    prop = _model()
    table = EntityTable()
    table.begin_frame([prop], epoch=1)
    still = np.array(table.model_base_matrix[0])
    prop._rotate_angle = 45.0                                    # journals the row
    table.begin_frame([prop], epoch=1)
    assert not np.allclose(still, table.model_base_matrix[0])


# -- I/O ----------------------------------------------------------------------------


def test_the_rotate_inputs_reach_the_prop_runtime():
    from editor.io_handlers import register_all_input_handlers
    from editor.io_system import IOManager, get_input_names, get_output_names
    for name in ('StartRotate', 'StopRotate', 'ToggleRotate', 'ReverseRotate',
                 'SetRotateSpeed'):
        assert name in get_input_names('prop')
    for name in ('OnRotateStarted', 'OnRotateStopped'):
        assert name in get_output_names('prop')
    assert not any('Spin' in n for n in get_input_names('prop') + get_output_names('prop'))

    prop = _model(rotate_ease=0.0)
    session, io = _session(prop)
    mgr = IOManager()
    register_all_input_handlers(mgr)
    logic = session.logic
    logic.prop_runtime = session
    handlers = mgr._input_handlers
    handlers[('prop', 'startrotate')](prop, '', logic)
    _run(session, 0.1)
    assert session.rotate_velocity_of(prop) == 180.0
    handlers[('prop', 'setrotatespeed')](prop, '-360', logic)
    _run(session, 0.1)
    assert session.rotate_velocity_of(prop) == -360.0
    handlers[('prop', 'setrotatespeed')](prop, 'not a number', logic)   # ignored
    handlers[('prop', 'reverserotate')](prop, '', logic)
    _run(session, 0.1)
    assert session.rotate_velocity_of(prop) == 360.0
    handlers[('prop', 'stoprotate')](prop, '', logic)
    _run(session, 0.1)
    assert session.rotate_velocity_of(prop) == 0.0
    handlers[('prop', 'togglerotate')](prop, '', logic)
    _run(session, 0.1)
    assert session.rotate_velocity_of(prop) == 360.0
