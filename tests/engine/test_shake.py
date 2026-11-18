"""engine.shake: the Shake entity's runtime, without a session.

Pinned: no shake, no change (the very matrix back); a shake wobbles the view
and dies away over its duration; shakes add up to a cap; the wobble is the
same every run; intensity and duration are read and bounded.
"""

import glm
import pytest

from engine.shake import (SHAKE_DEFAULTS, SHAKE_DURATION_RANGE, ShakeRuntime,
                          shake_settings)


def _view():
    return glm.lookAt(glm.vec3(10.0, 20.0, 30.0), glm.vec3(0.0, 0.0, 0.0),
                      glm.vec3(0.0, 1.0, 0.0))


def _differs(a, b):
    return max(abs(a[c][r] - b[c][r]) for c in range(4) for r in range(4))


def test_without_a_shake_the_view_is_returned_untouched():
    runtime = ShakeRuntime()
    view = _view()
    assert runtime.apply(view, now=5.0) is view
    assert runtime.amplitude(5.0) == 0.0


def test_a_shake_wobbles_the_view_and_dies_away():
    runtime = ShakeRuntime()
    runtime.start(1.0, 2.0, now=100.0)
    view = _view()
    moved = [_differs(runtime.apply(view, now=100.0 + t), view) for t in (0.05, 0.1, 0.17)]
    assert max(moved) > 0.01
    amplitudes = [runtime.amplitude(100.0 + t) for t in (0.0, 0.5, 1.0, 1.5, 1.99)]
    assert amplitudes == sorted(amplitudes, reverse=True) and amplitudes[0] == 1.0
    assert runtime.apply(view, now=102.1) is view            # over
    assert runtime.shakes == []


def test_shakes_add_up_to_a_cap():
    runtime = ShakeRuntime()
    runtime.start(0.3, 1.0, now=0.0)
    assert runtime.amplitude(0.0) == pytest.approx(0.3)
    runtime.start(0.3, 1.0, now=0.0)
    assert runtime.amplitude(0.0) == pytest.approx(0.6)
    runtime.start(0.9, 1.0, now=0.0)
    assert runtime.amplitude(0.0) == 1.0
    runtime.stop()
    assert runtime.amplitude(0.0) == 0.0


def test_the_wobble_is_the_same_every_run():
    a, b = ShakeRuntime(), ShakeRuntime()
    a.start(0.7, 1.0, now=3.0)
    b.start(0.7, 1.0, now=3.0)
    assert _differs(a.apply(_view(), now=3.3), b.apply(_view(), now=3.3)) == 0.0


def test_a_shake_turns_the_view_only_slightly():
    runtime = ShakeRuntime()
    runtime.start(1.0, 1.0, now=0.0)
    view = _view()
    for k in range(20):
        shaken = runtime.apply(view, now=k * 0.013)
        forward = glm.vec3(-view[0][2], -view[1][2], -view[2][2])
        moved = glm.vec3(-shaken[0][2], -shaken[1][2], -shaken[2][2])
        angle = glm.degrees(glm.acos(min(1.0, glm.dot(forward, moved))))
        assert angle < 5.0


@pytest.mark.parametrize("props, expected", [
    ({}, (SHAKE_DEFAULTS["intensity"], SHAKE_DEFAULTS["duration"])),
    ({"intensity": 2.0, "duration": 0.0}, (1.0, SHAKE_DURATION_RANGE[0])),
    ({"intensity": "x", "duration": "y"}, (SHAKE_DEFAULTS["intensity"], SHAKE_DEFAULTS["duration"])),
    ({"intensity": 0.25, "duration": 3.0}, (0.25, 3.0)),
])
def test_shake_settings(props, expected):
    assert shake_settings(props) == pytest.approx(expected)


def test_a_zero_intensity_shake_is_nothing():
    runtime = ShakeRuntime()
    runtime.start(0.0, 1.0, now=0.0)
    assert runtime.shakes == []
