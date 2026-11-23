"""A model Prop's spin animation: the maths shared by everything that uses it.

A model can spin about one of its own axes -- a tape reel on its hub, a fan,
a wheel. It is authored on the Prop (its Animation tab) and driven during
Play by :class:`engine.prop_runtime.PropSession` (Spin inputs, spin-up and
spin-down); the desktop renderer (engine.entity_table) and the Android
player (player/render/glmath) both draw the spun model through
:func:`spin_matrix`, so they turn it the same way.

The spin is applied in the model's own frame, after its scale and before
its placed rotation: ``R(rotation) @ R_spin(axis, angle) @ S``. A scaled
model keeps its shape as it turns, and the axis is the model's own -- a
reel lying on its back still spins about its hub.

The angle is runtime state (``Prop._spin_angle``), never written into the
map: leaving Play puts every model back as it was placed.

Pure Python + NumPy: no Qt, no GL.
"""

from __future__ import annotations

import math

import numpy as np

#: The Axis choices, as the property stores them.
SPIN_AXES = ('auto', 'x', 'y', 'z')
_AXIS_INDEX = {'x': 0, 'y': 1, 'z': 2}

#: What a Prop's spin properties default to (engine.prop_entity.PROP_DEFAULTS).
SPIN_DEFAULTS = {
    #: The model has a spin animation at all (the Animation tab's Spin box).
    'spin_enabled': False,
    #: Its own axis to spin about; 'auto' is its thinnest dimension -- a
    #: reel's, a fan's or a wheel's hub.
    'spin_axis': 'auto',
    #: Degrees a second at full speed; negative spins the other way.
    'spin_speed': 180.0,
    #: Seconds to spin up to full speed, and to coast down to a stop.
    'spin_ramp': 0.6,
    #: Spinning at full speed from the moment Play starts.
    'spin_autostart': False,
}

#: The most a spin may be set to, degrees a second (an entered 1e9 must not
#: wrap the angle into noise every tick).
MAX_SPIN_SPEED = 36000.0
#: The longest spin-up or spin-down, seconds.
MAX_SPIN_RAMP = 60.0


def _finite(value, default):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def spin_speed(props):
    """The authored full speed, degrees a second (clamped, signed)."""
    speed = _finite(props.get('spin_speed', SPIN_DEFAULTS['spin_speed']),
                    SPIN_DEFAULTS['spin_speed'])
    return max(-MAX_SPIN_SPEED, min(MAX_SPIN_SPEED, speed))


def spin_ramp(props):
    """Seconds to spin up or down; 0 starts and stops at once."""
    ramp = _finite(props.get('spin_ramp', SPIN_DEFAULTS['spin_ramp']),
                   SPIN_DEFAULTS['spin_ramp'])
    return max(0.0, min(MAX_SPIN_RAMP, ramp))


def axis_index(axis, bounds=None):
    """0, 1 or 2: the model axis *axis* names. ``'auto'`` (or anything not
    recognised) is the thinnest extent of *bounds* -- ``(lo, hi)`` of the
    model's own vertices -- and Z when the bounds are unknown, which is how
    a model of a disc lying in its X-Y plane (a reel, a wheel) is made."""
    key = str(axis or 'auto').strip().lower()
    if key in _AXIS_INDEX:
        return _AXIS_INDEX[key]
    if bounds is not None:
        try:
            lo, hi = bounds
            extent = [abs(float(hi[i]) - float(lo[i])) for i in range(3)]
        except (TypeError, ValueError, IndexError):
            return 2
        if all(math.isfinite(e) for e in extent):
            smallest = min(extent)
            # Ties go to Z, then Y: a cube spins like a turntable would.
            for index in (2, 1, 0):
                if extent[index] == smallest:
                    return index
    return 2


def spin_matrix(axis, angle_deg):
    """3x3 right-handed rotation of *angle_deg* about model axis *axis*
    (0, 1 or 2) -- the same sense as GLM's ``rotate``."""
    theta = math.radians(float(angle_deg))
    c, s = math.cos(theta), math.sin(theta)
    if axis == 0:
        return np.array(((1.0, 0.0, 0.0), (0.0, c, -s), (0.0, s, c)))
    if axis == 1:
        return np.array(((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c)))
    return np.array(((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0)))


def axis_vector(axis):
    """The unit vector of model axis *axis* (0, 1 or 2), as a tuple."""
    return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))[int(axis) % 3]


def approach(current, target, rate, delta):
    """*current* moved toward *target* by at most ``rate * delta`` (rate in
    units a second; a rate of None reaches it at once)."""
    if rate is None:
        return target
    step = max(0.0, float(rate)) * max(0.0, float(delta))
    if abs(target - current) <= step:
        return target
    return current + step if target > current else current - step


def ramp_rate(full_speed, ramp):
    """How fast (degrees a second, per second) the spin speed changes: full
    speed in *ramp* seconds; None for an instant change."""
    if ramp <= 0.0:
        return None
    return max(abs(full_speed), 1.0) / ramp
