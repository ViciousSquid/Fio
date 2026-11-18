"""Screen shake: the Shake entity's runtime (purely visual).

A shake wobbles the published camera view -- a small rotation and offset
that die away over its duration -- and nothing else: the player, physics
and the world are untouched, so aiming, collision and simulation are
exactly as they would be without it. Shakes overlap and add up (to a cap).

The wobble is a sum of sines at unrelated frequencies per axis: smooth,
cheap, and the same every run. With no shake running, :meth:`apply` returns
the view it was given after one list check.
"""

from __future__ import annotations

import math
import time

import glm

#: Defaults of a Shake entity.
SHAKE_DEFAULTS = {
    'intensity': 0.5,       # 0..1
    'duration': 1.0,        # seconds
}
#: Bounds of a Shake's duration, seconds.
SHAKE_DURATION_RANGE = (0.05, 10.0)

#: At full intensity: how far the view turns (degrees) and moves (world
#: units, across and up the screen).
SHAKE_MAX_DEGREES = 2.5
SHAKE_MAX_OFFSET = 4.0

#: (frequency Hz, phase) pairs per axis: yaw, pitch, roll, across, up.
_WOBBLE = (
    ((13.0, 0.0), (21.7, 1.3)),
    ((15.3, 2.1), (24.1, 0.4)),
    ((9.1, 4.0), (17.9, 2.7)),
    ((11.7, 0.9), (19.3, 3.6)),
    ((14.1, 5.2), (22.9, 1.8)),
)


def _number(value, default, lo, hi):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number:
        return float(default)
    return max(lo, min(hi, number))


def shake_settings(props):
    """``(intensity 0..1, duration seconds)`` of a Shake's properties."""
    return (_number(props.get('intensity'), SHAKE_DEFAULTS['intensity'], 0.0, 1.0),
            _number(props.get('duration'), SHAKE_DEFAULTS['duration'],
                    *SHAKE_DURATION_RANGE))


class ShakeRuntime:
    """The shakes running now, and the view they make."""

    def __init__(self):
        self.shakes = []            # [(start, duration, intensity)]

    def start(self, intensity, duration, now=None):
        """Start a shake (it adds to any already running)."""
        intensity = _number(intensity, 0.0, 0.0, 1.0)
        if intensity <= 0.0:
            return
        now = time.perf_counter() if now is None else now
        duration = _number(duration, SHAKE_DEFAULTS['duration'], *SHAKE_DURATION_RANGE)
        self.shakes.append((float(now), duration, intensity))

    def stop(self):
        self.shakes = []

    def amplitude(self, now=None):
        """How strong the shake is now, 0..1: each shake fades out over its
        duration (eased), and overlapping shakes add up to at most 1."""
        if not self.shakes:
            return 0.0
        now = time.perf_counter() if now is None else now
        total = 0.0
        alive = []
        for start, duration, intensity in self.shakes:
            t = (now - start) / duration
            if t >= 1.0:
                continue
            alive.append((start, duration, intensity))
            if t > 0.0:
                fade = 1.0 - t
                total += intensity * fade * fade
            else:
                total += intensity
        self.shakes = alive
        return min(total, 1.0)

    def apply(self, view, now=None):
        """*view* (a camera's view matrix), shaken."""
        if not self.shakes:
            return view
        now = time.perf_counter() if now is None else now
        a = self.amplitude(now)
        if a <= 0.0:
            return view
        w = [0.6 * math.sin(6.283185307 * f1 * now + p1)
             + 0.4 * math.sin(6.283185307 * f2 * now + p2)
             for (f1, p1), (f2, p2) in _WOBBLE]
        degrees = a * SHAKE_MAX_DEGREES
        offset = a * SHAKE_MAX_OFFSET
        # In camera space, so the shake is the same whichever way one looks.
        shake = glm.translate(glm.mat4(1.0), glm.vec3(w[3] * offset, w[4] * offset, 0.0))
        shake = glm.rotate(shake, math.radians(w[2] * degrees * 0.6), glm.vec3(0.0, 0.0, 1.0))
        shake = glm.rotate(shake, math.radians(w[1] * degrees), glm.vec3(1.0, 0.0, 0.0))
        shake = glm.rotate(shake, math.radians(w[0] * degrees), glm.vec3(0.0, 1.0, 0.0))
        return shake * view
