"""A model Prop's Rotate animation (engine.model_rotate): the maths, the three
modes.  Play, the drawn matrix and the I/O inputs: test_model_rotate_play.py."""

import math

import glm
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



def test_speed_zero_then_stop_still_coasts_down_over_its_ease():
    m = _motion(mr.CONTINUOUS, rotate_speed=180.0, rotate_ease=0.5)
    _steps(m, 1.0)
    assert m.velocity == pytest.approx(180.0)
    m.set_speed(0.0)
    m.stop()
    events = _steps(m, 0.55)
    assert m.velocity == 0.0 and events == ['stopped']


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
