"""A model Prop's Rotate in Play: the runtime (PropSession), the drawn matrix
and the I/O inputs.  The maths and the three modes are test_model_rotate.py's."""

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="Prop is an editor-tier Thing")

from editor.things import Prop                                   # noqa: E402
from engine import model_rotate as mr                            # noqa: E402
from engine.prop_runtime import PropSession                      # noqa: E402

pytestmark = pytest.mark.qt

REEL_BOUNDS = ((-20.0, -20.0, -3.35), (20.0, 20.0, 6.0))


# -- the Play runtime -------------------------------------------------------------


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
