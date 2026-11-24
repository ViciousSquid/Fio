"""A model Prop's Rotate animation: the maths shared by everything that uses it.

Any model can rotate about one of its own axes, in one of three modes:

* **Continuous** -- round and round: a fan, a wheel, a tape reel, a
  turntable. It spins up to Speed over its Ease time when started and
  coasts down to a stop when stopped.
* **Swing** -- back and forth across an arc centred on how it was placed: a
  CCTV camera sweeping, a hanging sign, a pendulum. It slows at each end
  like a pendulum, eases into its swing when started and out of it (where
  it is) when stopped.
* **Turn** -- to an angle and stop: a valve wheel, a lever, a hatch. Started
  (or toggled, or reversed) it turns smoothly to Angle; again, and it turns
  back to how it was placed.

It is authored on the Prop (the Animation tab), played in the editor by the
tab's Preview (editor.rotate_preview) and during Play by
:class:`engine.prop_runtime.PropSession` -- both drive one
:class:`RotateMotion` per model, so the Preview is exactly what the game
does. The desktop renderer (engine.entity_table) and the Android player
(player/render/glmath) draw the result through :func:`rotate_matrix`.

The rotation is applied in the model's own frame, after its scale and before
its placed rotation: ``R(rotation) @ R_rotate(axis, angle) @ S``. A scaled
model keeps its shape as it turns, and the axis is the model's own. The
angle is runtime state (``Prop._rotate_angle``), never written into the map:
leaving Play puts every model back as it was placed. In Play a solid model's
collision turns with it, about the same axis through its origin
(engine.logic_groups.CarriedModels).

Pure Python + NumPy: no Qt, no GL.
"""

from __future__ import annotations

import math

import numpy as np

CONTINUOUS, SWING, TURN = 'continuous', 'swing', 'turn'
ROTATE_MODES = (CONTINUOUS, SWING, TURN)

#: The Axis choices, as the property stores them.
ROTATE_AXES = ('auto', 'x', 'y', 'z')
_AXIS_INDEX = {'x': 0, 'y': 1, 'z': 2}

#: What a Prop's rotate properties default to (engine.prop_entity.PROP_DEFAULTS).
ROTATE_DEFAULTS = {
    #: The model rotates at all (the Animation tab's Rotate box).
    'rotate_enabled': False,
    #: 'continuous', 'swing' or 'turn'.
    'rotate_mode': CONTINUOUS,
    #: Its own axis to rotate about; 'auto' is its thinnest dimension -- a
    #: reel's, a fan's, a wheel's or a valve's hub.
    'rotate_axis': 'auto',
    #: Degrees a second. Continuous: its full speed, negative the other way.
    #: Swing and Turn: how fast it moves (the sign is ignored).
    'rotate_speed': 90.0,
    #: Degrees. Swing: the whole arc it sweeps; Turn: how far it turns
    #: (negative: the other way). Continuous ignores it.
    'rotate_angle': 90.0,
    #: Seconds. Continuous and Swing: to get up to speed when started and to
    #: come to rest when stopped. Turn always eases in and out.
    'rotate_ease': 0.6,
    #: Rotating from the moment Play starts (Turn: already turned).
    'rotate_autostart': False,
}

#: The most a speed may be set to, degrees a second.
MAX_ROTATE_SPEED = 36000.0
#: The largest Angle, degrees (ten full turns).
MAX_ROTATE_ANGLE = 3600.0
#: The longest Ease, seconds.
MAX_ROTATE_EASE = 60.0


def _finite(value, default):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def physics_owns_turn(props):
    """True when the Prop has Physics on: the simulation turns its body, so
    Rotate does not run (two owners of one orientation would leave the
    collision box where the drawn model is not)."""
    return bool(props.get('physics_enabled', False))


def rotates_in_play(props):
    """Rotate runs in Play for this Prop: on, a model, and no physics."""
    return (bool(props.get('rotate_enabled')) and bool(props.get('model_path'))
            and not physics_owns_turn(props))


def rotate_mode(props):
    mode = str(props.get('rotate_mode', CONTINUOUS) or CONTINUOUS).strip().lower()
    return mode if mode in ROTATE_MODES else CONTINUOUS


def rotate_speed(props):
    """The authored speed, degrees a second (clamped, signed)."""
    speed = _finite(props.get('rotate_speed', ROTATE_DEFAULTS['rotate_speed']),
                    ROTATE_DEFAULTS['rotate_speed'])
    return _clamp(speed, -MAX_ROTATE_SPEED, MAX_ROTATE_SPEED)


def rotate_angle(props):
    """The authored Angle, degrees (clamped, signed)."""
    angle = _finite(props.get('rotate_angle', ROTATE_DEFAULTS['rotate_angle']),
                    ROTATE_DEFAULTS['rotate_angle'])
    return _clamp(angle, -MAX_ROTATE_ANGLE, MAX_ROTATE_ANGLE)


def rotate_ease(props):
    """Seconds to get up to speed or come to rest; 0: at once."""
    ease = _finite(props.get('rotate_ease', ROTATE_DEFAULTS['rotate_ease']),
                   ROTATE_DEFAULTS['rotate_ease'])
    return _clamp(ease, 0.0, MAX_ROTATE_EASE)


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
            # Ties go to Z, then Y: a cube turns like a turntable would.
            for index in (2, 1, 0):
                if extent[index] == smallest:
                    return index
    return 2


def rotate_matrix(axis, angle_deg):
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


def _smoothstep(t):
    t = _clamp(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


class RotateMotion:
    """One model's Rotate animation, stepped in time.

    Reads the authored settings from *props* (a Prop's properties) every
    step, so editing them during a Preview takes effect at once. Speed is
    a runtime value: SetRotateSpeed changes it without touching *props*.

    :meth:`step` returns the events that happened (``'started'``,
    ``'stopped'``) for the caller to fire as I/O outputs; :attr:`angle` is
    the model's current rotation, degrees.
    """

    def __init__(self, props, running=None):
        self.props = props
        self.mode = rotate_mode(props)
        self.speed = rotate_speed(props)          # runtime (SetRotateSpeed)
        self.direction = 1.0                      # ReverseRotate flips it
        self._eased_to = None                     # Continuous: the velocity eased to
        self._span = 1.0                          # ... and the speed Ease spans
        self.running = bool(props.get('rotate_autostart')) if running is None else bool(running)
        self.angle = 0.0
        # Continuous: signed degrees a second now.
        self.velocity = self.speed if self.running else 0.0
        # Swing: how far into its swing it is (0 still .. 1 full), and where
        # in the swing (radians of the sine).
        self.amount = 1.0 if self.running else 0.0
        self.phase = 0.0
        # Turn: 0 as placed .. 1 turned.
        self.turned = 1.0 if self.running else 0.0
        self._sync()

    # -- control (I/O) ------------------------------------------------------

    def start(self):
        """Start rotating (Turn: turn to Angle). True when it changed."""
        if self.running:
            return False
        self.running = True
        return True

    def stop(self):
        """Stop (Continuous: coast down; Swing: ease to rest where it is;
        Turn: turn back as placed). True when it changed."""
        if not self.running:
            return False
        self.running = False
        return True

    def toggle(self):
        return self.stop() if self.running else self.start()

    def reverse(self):
        """The other way. Continuous: slows, stops and spins back up; Swing:
        swings the other way from where it is; Turn: turns back (or on)."""
        if self.mode == TURN:
            return self.toggle()
        self.direction = -self.direction
        return True

    def set_speed(self, speed):
        self.speed = rotate_speed({'rotate_speed': speed})
        return True

    # -- time ---------------------------------------------------------------

    def step(self, delta):
        """Advance *delta* seconds; returns the events ('started' is reported
        by the control calls' True; this reports 'stopped' when it comes to
        rest)."""
        delta = max(0.0, float(delta))
        self.mode = rotate_mode(self.props)
        ease = rotate_ease(self.props)
        events = []
        if self.mode == CONTINUOUS:
            target = self.speed * self.direction if self.running else 0.0
            if self.velocity != target:
                was = self.velocity
                # Ease is the time to span the faster of where it is going
                # and how fast it was going when that was set -- taken then,
                # so SetRotateSpeed(0) then StopRotate still coasts down over
                # Ease, not over minutes.
                if target != self._eased_to:
                    self._eased_to = target
                    self._span = max(abs(self.speed), abs(self.velocity), 1.0)
                rate = None if ease <= 0.0 else self._span / ease
                self.velocity = approach(self.velocity, target, rate, delta)
                if was != 0.0 and self.velocity == 0.0 and not self.running:
                    events.append('stopped')
            if self.velocity:
                self.angle = (self.angle + self.velocity * delta) % 360.0
        elif self.mode == SWING:
            target = 1.0 if self.running else 0.0
            if self.amount != target:
                was = self.amount
                self.amount = approach(self.amount, target,
                                       None if ease <= 0.0 else 1.0 / ease, delta)
                if was != 0.0 and self.amount == 0.0 and not self.running:
                    events.append('stopped')
            arc = abs(rotate_angle(self.props))
            if self.amount and arc > 1e-6:
                # A pendulum's sweep: through the middle at Speed, slowing to
                # a stop at each end. Period: the arc there and back at the
                # average speed of a sine (2/pi of the peak).
                peak = abs(self.speed)
                half = arc / 2.0
                omega = peak / half if half > 0.0 else 0.0     # rad/s of the sine
                self.phase += omega * self.direction * self.amount * delta
                self.phase %= 2.0 * math.pi
            # Where it is, held there while it comes to rest.
            self.angle = (arc / 2.0) * math.sin(self.phase)
        else:                                                  # TURN
            target = 1.0 if self.running else 0.0
            if self.turned != target:
                angle = abs(rotate_angle(self.props))
                peak = abs(self.speed)
                # Time for the whole turn at Speed (on average: the ease in
                # and out of smoothstep peaks at 1.5x).
                rate = None if angle <= 1e-6 or peak <= 0.0 else peak / angle
                if rate is None and peak <= 0.0:
                    rate = 0.0
                self.turned = approach(self.turned, target, rate, delta)
                if self.turned == target:
                    events.append('stopped')
        self._sync()
        return events

    def _sync(self):
        if self.mode == TURN:
            self.angle = rotate_angle(self.props) * _smoothstep(self.turned)
        elif self.mode == SWING:
            self.angle = (abs(rotate_angle(self.props)) / 2.0) * math.sin(self.phase)

    #: What a save game carries of a motion.
    SAVED = ('running', 'direction', 'speed', 'angle', 'velocity', 'amount',
             'phase', 'turned')

    def snapshot(self):
        return {key: getattr(self, key) for key in self.SAVED}

    def restore(self, saved):
        """Take up the progress :meth:`snapshot` recorded. An entry that is
        not one (a field missing, a value that is no finite number) is left
        alone; returns whether it was taken."""
        try:
            values = {key: float(saved[key]) for key in self.SAVED if key != 'running'}
            running = bool(saved['running'])
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        if not all(math.isfinite(value) for value in values.values()):
            return False
        self.running = running
        self.direction = -1.0 if values['direction'] < 0.0 else 1.0
        self.speed = rotate_speed({'rotate_speed': values['speed']})
        self.angle = values['angle'] % 360.0 if self.mode == CONTINUOUS else values['angle']
        self.velocity = values['velocity']
        self.amount = min(1.0, max(0.0, values['amount']))
        self.phase = values['phase'] % (2.0 * math.pi)
        self.turned = min(1.0, max(0.0, values['turned']))
        self._eased_to = None
        self._sync()
        return True

    @property
    def moving(self):
        """Whether it is still turning (a stopped one may still be coasting)."""
        if self.mode == CONTINUOUS:
            return self.velocity != 0.0
        if self.mode == SWING:
            return self.amount != 0.0
        return self.turned != (1.0 if self.running else 0.0)
