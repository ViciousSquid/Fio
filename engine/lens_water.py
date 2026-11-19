"""Water on the lens: the camera comes up out of the water wet.

When the player's eye leaves the water -- surfacing from deep water, or
standing up after crouching under -- the camera's glass is left covered in
drops, which run off it as water does off a window or a camera lens:

* every drop runs downward, and the bigger it is the faster: a tiny bead
  clings to the glass and barely creeps (surface tension holds it), a
  middling drop wanders slowly down, and a big one races down and off the
  bottom;
* a drop that runs does so in fits and starts, snagging and slipping, and
  wanders a little from side to side as it goes;
* it leaves a trail of small beads behind it, losing water to them, and
  sweeps up whatever beads lie in its path, each one making it heavier and
  faster;
* a running drop is drawn long, rounded at the bottom and narrowing above;
  a bead is egg-shaped, a little wider at the bottom;
* beads dry off the glass, the smallest first;
* turning the camera quickly pushes the drops back across the glass,
  against its movement -- big ones most.

**How deep.** Shallow water (below the waist) leaves the glass dry. Waist
deep or more with the eye above the surface (:func:`wets_bottom_half`), the
bottom half of the glass has drops on it -- fresh ones keep landing there,
each gone within WADE_SECONDS. The eye under and then out again wets all of
it (below).

**Never more than LENS_WATER_SECONDS.** Every drop has a deadline: from the
moment the eye surfaces, everything it left is gone within
LENS_WATER_SECONDS (a standing-in-water drop within WADE_SECONDS of landing): the drops
run and dry as they would, and over the last FADE_SECONDS whatever is left
fades away, so the view is always back to normal by then. Going under again
clears the glass (a lens under water is not dripping); surfacing again wets
it afresh.

Each drop is a little lens with no colour of its own: the picture behind it
is seen through it upside-down, mirrored and much smaller, with a darker top
and a lit underside -- :mod:`engine.renderer.core.lens_water_gl`, one pass
over the finished frame. This module is only the drops, as plain data in
screen fractions (x 0..1 across, y 0..1 down; a radius as a share of the
screen's height), advanced by real time. No Qt, no GL: tested headless.

The drop simulation and its pass are MiniWind's camera-lens water
(game/lens_water.py, game/ui/magic_gl.py), with its rain replaced by the
splash of coming up out of the water and the time limit above.
"""

from __future__ import annotations

import math
import random

#: Drops on the glass at once, at most (the shader's uniform array).
MAX_DROPS = 64
#: The splash as the eye surfaces: this many drops, of these radii (share of
#: the screen's height). Most are small beads, a few are big runners
#: (SPLASH_BIAS > 1 leans the sizes small).
SPLASH_DROPS = 46
SPLASH_RADIUS = (0.006, 0.04)
SPLASH_BIAS = 1.7
#: Below this radius a bead clings to the glass and only creeps.
PIN_RADIUS = 0.011
#: A drop this big runs at RUN_SPEED (screen heights a second); between the
#: two the speed rises steeply with size (``t ** 1.5``). Bigger still runs
#: faster yet, up to MAX_SPEED.
RUN_RADIUS = 0.045
RUN_SPEED = 0.55
MAX_SPEED = 0.9
#: How fast a clinging bead creeps down.
CREEP_SPEED = 0.006
#: Stick-slip: the speed swings between SNAG (stuck) and a surge, averaging
#: the size's speed; snags come and go this often (a second) for a small
#: runner, twice as often for a big one.
SNAG = 0.3
SLIP_RATE = 0.6
#: Side-to-side wander of a running drop (share of the screen's width).
WANDER = 0.012
#: A runner leaves a bead behind every this-many of its own radii of travel,
#: each bead this share of its radius.
TRAIL_GAP = (1.3, 2.4)
TRAIL_BEAD = (0.16, 0.3)
#: Seconds a trail bead lasts before it fades off the glass.
TRAIL_LIFE = (0.8, 2.2)
#: Seconds a drop takes to dry (the smallest first; big ones mostly run off
#: before they dry).
DRY_SECONDS = (1.5, 4.5)
#: How far a running drop stretches with speed (1 = round), and the most.
STRETCH_PER_SPEED = 1.6
MAX_STRETCH = 1.7
#: Camera movement: *motion* 1 (fast) pushes a RUN_RADIUS drop back across
#: the glass at this speed (screen heights a second), smaller ones less.
AIR_PUSH = 0.18
#: From the moment the eye surfaces, the glass is dry again within this many
#: seconds -- always; the last FADE_SECONDS of it fade whatever is left.
LENS_WATER_SECONDS = 6.0
FADE_SECONDS = 1.2
#: Waist deep: the share of the player's feet-to-eye height under water
#: from which the bottom half of the glass gets drops (below it, none).
WAIST_DEPTH = 0.5
#: Waist deep: this many drops land a second, on the bottom half of
#: the glass only (WADE_BAND, top to bottom), of these radii -- fewer,
#: smaller and fainter (WADE_STRENGTH of a surfacing drop's) than the
#: soaking the eye coming up out of the water gets; each is gone within
#: WADE_SECONDS of landing (its last WADE_FADE fading).
WADE_PER_SECOND = 3.0
WADE_BAND = (0.5, 0.97)
WADE_RADIUS = (0.004, 0.016)
WADE_STRENGTH = 0.5
WADE_SECONDS = 3.0
WADE_FADE = 0.8


def wets_bottom_half(depth_frac, eye_under) -> bool:
    """Whether the player stands deep enough in water -- waist deep or more,
    the eye above the surface -- for the bottom half of the glass to get
    drops. *depth_frac* is the share of the feet-to-eye height under water."""
    return (not eye_under) and float(depth_frac) >= WAIST_DEPTH


class Drop:
    """One drop on the glass."""

    __slots__ = ("x", "y", "r", "wet", "phase", "gap", "seed", "life", "trail",
                 "speed", "x0", "dry", "deadline", "fade_for", "strength")

    def __init__(self, x, y, r, wet=1.0, *, rng=None, trail=False):
        rng = rng or random
        self.x = self.x0 = float(x)       # x0: where it would be without wandering
        self.y = float(y)
        self.r = float(r)
        self.wet = float(wet)
        self.phase = rng.random() * 6.2831853
        self.gap = self.r * rng.uniform(*TRAIL_GAP)
        self.seed = rng.random() * 100.0
        self.life = rng.uniform(*TRAIL_LIFE) if trail else float('inf')
        self.trail = trail
        self.speed = 0.0                   # how fast it moved last update
        # Seconds this one takes to dry: small first.
        self.dry = DRY_SECONDS[0] + (DRY_SECONDS[1] - DRY_SECONDS[0]) * min(
            1.0, self.r / RUN_RADIUS)
        # The lens's clock reading by which it is gone, whatever else
        # happens, and how long before that it fades (set where it lands).
        self.deadline = float('inf')
        self.fade_for = FADE_SECONDS
        #: How visible it is at its wettest (1: a surfacing drop).
        self.strength = 1.0

    def stretch(self) -> float:
        return min(MAX_STRETCH, 1.0 + self.speed * STRETCH_PER_SPEED)


def run_speed(r: float) -> float:
    """How fast a drop of radius *r* runs down the glass, on average (screen
    heights a second): bigger is faster."""
    if r <= PIN_RADIUS:
        return CREEP_SPEED
    t = (r - PIN_RADIUS) / (RUN_RADIUS - PIN_RADIUS)
    if t <= 1.0:
        return CREEP_SPEED + (RUN_SPEED - CREEP_SPEED) * t ** 1.5
    return min(MAX_SPEED, RUN_SPEED * (r / RUN_RADIUS) ** 1.5)


def _slip(phase: float) -> float:
    """The stick-slip swing: averages 1 over a cycle, SNAG at its least."""
    pulse = max(0.0, math.sin(phase)) ** 2          # averages 1/4
    return SNAG + (1.0 - SNAG) * 4.0 * pulse


def _wander(y: float, seed: float) -> float:
    """Side-to-side offset at height *y*: mostly straight, now and then a
    sideways jink (``sin(3w) * sin(w)^6``)."""
    w = y * 9.0 + seed
    return math.sin(3.0 * w) * math.sin(w) ** 6


class LensWater:
    """The drops on the camera's glass, and how long they have left."""

    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.drops: list[Drop] = []
        #: Seconds since the eye last surfaced (None: not since the glass was
        #: last dry). The surfacing drops are gone by LENS_WATER_SECONDS.
        self.age = None
        #: The lens's own clock: seconds while it has been wet.
        self.clock = 0.0
        self._wade_carry = 0.0

    # -- what starts and stops it ---------------------------------------

    def surfaced(self) -> None:
        """The eye has just left the water: the glass is splashed all over
        (whatever was on it is replaced), and the clock starts."""
        rng = self.rng
        lo, hi = SPLASH_RADIUS
        self.drops = []
        deadline = self.clock + LENS_WATER_SECONDS
        for _ in range(SPLASH_DROPS):
            r = lo + (hi - lo) * rng.random() ** SPLASH_BIAS
            drop = Drop(rng.random(), rng.random() * 0.92, r, 1.0, rng=rng)
            drop.deadline = deadline
            self.drops.append(drop)
        self._merge()
        self.age = 0.0

    def standing_in_water(self, seconds) -> None:
        """Waist deep with the eye above the water (:func:`wets_bottom_half`),
        for *seconds*: drops land on the bottom half of the glass."""
        self._wade_carry += WADE_PER_SECOND * max(0.0, min(0.25, float(seconds)))
        rng = self.rng
        while self._wade_carry >= 1.0:
            self._wade_carry -= 1.0
            r = rng.uniform(*WADE_RADIUS)
            drop = Drop(rng.random(), rng.uniform(*WADE_BAND), r, 1.0, rng=rng)
            drop.deadline = self.clock + WADE_SECONDS
            drop.fade_for = WADE_FADE
            drop.strength = WADE_STRENGTH
            drop.dry = min(drop.dry, WADE_SECONDS)
            if len(self.drops) >= MAX_DROPS:
                # The glass is full: the smallest gives way.
                del self.drops[min(range(len(self.drops)), key=lambda k: self.drops[k].r)]
            self.drops.append(drop)

    def submerged(self) -> None:
        """The eye is under water: no drops on a lens in the water."""
        self.clear()

    def clear(self) -> None:
        self.drops = []
        self.age = None
        self._wade_carry = 0.0

    @property
    def active(self) -> bool:
        """Whether there is water on the glass."""
        return bool(self.drops)

    # -- advancing -------------------------------------------------------

    def update(self, seconds: float, motion=(0.0, 0.0)) -> None:
        """Advance by *seconds* of real time; *motion* is the camera's
        movement across the screen, ``(x, y)`` (y down), length 1 for fast."""
        if not self.drops:
            self.age = None
            return
        dt = max(0.0, min(0.25, float(seconds)))
        self.clock += dt
        if self.age is not None:
            self.age += dt
            if self.age >= LENS_WATER_SECONDS:
                self.age = None
        clock = self.clock
        rng = self.rng
        mx, my = (float(motion[0]), float(motion[1])) if motion else (0.0, 0.0)
        m = math.hypot(mx, my)
        if m > 1.0:
            mx, my = mx / m, my / m
        kept = []
        beads = []
        for d in self.drops:
            if clock >= d.deadline:
                continue                        # never longer than its time
            if d.trail:
                d.life -= dt
            if d.trail and d.life <= 0.0:
                d.wet -= dt * 1.5               # a trail bead fades off
            else:
                d.wet -= dt / d.dry             # drying, the smallest first
            if d.wet <= 0.0:
                continue
            running = d.r > PIN_RADIUS
            speed = run_speed(d.r)
            if running:
                d.phase += dt * 6.2831853 * SLIP_RATE * (1.0 + min(1.0, d.r / RUN_RADIUS))
                speed *= _slip(d.phase)
            dy = speed * dt
            d.y += dy
            # The air pushes it back against the camera's movement.
            push = AIR_PUSH * min(1.5, d.r / RUN_RADIUS) * dt
            d.x0 -= mx * push
            d.y -= my * push
            d.x = d.x0 + (WANDER * _wander(d.y, d.seed) if running else 0.0)
            d.speed = speed
            if running:
                d.gap -= dy
                if d.gap <= 0.0:
                    d.gap = d.r * rng.uniform(*TRAIL_GAP)
                    b = d.r * rng.uniform(*TRAIL_BEAD)
                    # Behind its tail, just clear of it (so it is not
                    # gathered straight back).
                    back = (d.r + b) * 0.85 + d.r * (d.stretch() - 1.0)
                    bead = Drop(d.x, d.y - back, b, d.wet, rng=rng, trail=True)
                    bead.deadline, bead.fade_for = d.deadline, d.fade_for
                    bead.strength = d.strength
                    beads.append(bead)
                    d.r = math.sqrt(max(PIN_RADIUS ** 2 * 0.5, d.r * d.r - b * b))
            if d.y - d.r > 1.0 or d.y + d.r < 0.0 or d.x + d.r < -0.1 or d.x - d.r > 1.1:
                continue                           # off the glass
            kept.append(d)
        room = MAX_DROPS - len(kept)
        if beads and room > 0:
            kept.extend(beads[:room])
        self.drops = kept[-MAX_DROPS:]
        self._merge()
        if not self.drops:
            self.age = None                         # dry: back to normal
            self.clock = 0.0

    def _merge(self) -> None:
        """Drops that touch run together into one bigger drop -- a runner
        sweeping up the beads in its path grows, and so runs faster."""
        drops = self.drops
        n = len(drops)
        gone = set()
        for i in range(n):
            if i in gone:
                continue
            a = drops[i]
            for j in range(i + 1, n):
                if j in gone:
                    continue
                b = drops[j]
                if math.hypot(a.x - b.x, a.y - b.y) < (a.r + b.r) * 0.8:
                    # Area adds; the bigger keeps its place and its way.
                    big, small = (a, b) if a.r >= b.r else (b, a)
                    big.r = math.sqrt(big.r ** 2 + small.r ** 2)
                    big.wet = max(big.wet, small.wet)
                    big.life = max(big.life, small.life)
                    big.trail = big.trail and small.trail
                    big.dry = max(big.dry, small.dry)
                    big.strength = max(big.strength, small.strength)
                    # Never outlives the sooner of the two.
                    if small.deadline < big.deadline:
                        big.deadline, big.fade_for = small.deadline, small.fade_for
                    gone.add(j if big is a else i)
                    if big is b:
                        break
        if gone:
            self.drops = [d for k, d in enumerate(drops) if k not in gone]

    # -- for the pass ------------------------------------------------------

    def fade(self, drop=None) -> float:
        """How visible *drop* still is by its deadline (1 until its last
        fade, then down to 0); without one, the surfacing drops' fade."""
        if drop is None:
            if self.age is None:
                return 0.0
            return max(0.0, min(1.0, (LENS_WATER_SECONDS - self.age) / FADE_SECONDS))
        return max(0.0, min(1.0, (drop.deadline - self.clock) / drop.fade_for))

    def frame(self):
        """``[(x, y, radius, wetness, stretch)]`` for the lens pass."""
        return [(d.x, d.y, d.r,
                 max(0.0, min(1.0, d.wet)) * d.strength * self.fade(d), d.stretch())
                for d in self.drops]
