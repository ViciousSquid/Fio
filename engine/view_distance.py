"""
Camera view distance and far-plane fog -- the pure, GL-free state behind it.

This is a **renderer/camera** setting, not a world-streaming one. Big World
decides which cells are loaded and simulated, from the *player's* position;
this decides how far the *camera* draws, and it never unloads anything, never
suppresses simulation and never narrows the resident set. The two cooperate --
the renderer only ever draws what is already resident -- but they stay separate
knobs, so pulling the far plane in to 2,000 on a slow machine changes nothing
about the world except how much of it is on screen.

The editor's "Cull Dist" spinbox and the ``r_viewdistance`` console command
both write :attr:`ViewDistance.distance` here, and everything downstream reads
it: the broad-phase distance cull, the projection's far plane, the logic
thread's frustum, and the distance fog the shaders apply.

**Fog and clipping work together.** A far plane on its own pops geometry out of
existence at a hard edge. So the fog is derived from the same distance and is
guaranteed to reach full opacity *before* the clip: by default fog starts at
60% of the view distance and is fully opaque at 92% of it, leaving the last 8%
as a margin in which nothing can be seen anyway. :meth:`ViewDistance.resolve`
enforces that ordering even when a map or a console command asks for something
else, so a hard pop-out is not reachable through the public API.

Fog is measured as the true radial distance from the camera to the fragment
(not view-space depth), which is what the shaders compute, so the fog wall is a
sphere around the eye and does not swim as the camera turns.

No GL, no glm, no Qt -- unit-testable headlessly.
"""

from __future__ import annotations

#: Default view distance in world units. Matches the editor spinbox default and
#: the historical :data:`engine.render_cull.CAMERA_RENDER_CULL_DISTANCE`.
DEFAULT_VIEW_DISTANCE = 4096.0

#: The span the spinbox and console accept -- the range the editor's "Cull
#: Dist" spinbox has always offered, now shared so the two cannot disagree.
#:
#: The ceiling is worth knowing about: the far plane used to be pinned at
#: 10,000 whatever the spinbox said, so a depth buffer spanning 0.1 to 20,000
#: is new, and it halves depth precision at a given distance compared with the
#: old fixed plane. The default of 4,096 is better than the old behaviour on
#: that count, not worse; 20,000 is the setting to reach for only when the map
#: genuinely needs it and z-fighting on distant coplanar faces is acceptable.
MIN_VIEW_DISTANCE = 500.0
MAX_VIEW_DISTANCE = 20000.0

#: Where fog begins, as a fraction of the view distance, when it is left to
#: track automatically. 0.6 of 8192 is ~4,900 units.
AUTO_FOG_START_FRAC = 0.60
#: Where fog becomes fully opaque, as a fraction of the view distance. 0.92 of
#: 8192 is ~7,500 units -- dense well before the 8,192 clip.
AUTO_FOG_END_FRAC = 0.92

#: The hard ceiling on where fog may become opaque, as a fraction of the view
#: distance. An explicit ``r_fogdistance`` past this is clamped back to it: fog
#: that ends *at* the far plane leaves no margin, and rounding in the depth test
#: would show a sliver of un-fogged geometry at the clip.
MAX_FOG_END_FRAC = 0.98

#: The smallest gap (world units) allowed between fog start and fog end, so the
#: transition is never a visible hard line.
MIN_FOG_BAND = 1.0

#: Default fog colour -- the cool blue-grey the editor's background already
#: uses, so switching fog on does not recolour a map that never configured it.
DEFAULT_FOG_COLOR = (0.10, 0.10, 0.15)

#: Default exponential-squared density coefficient. Zero means "linear only":
#: fog ramps evenly from start to end. A positive value adds an exp2 curve on
#: top, which thickens the near half without moving the opaque point.
DEFAULT_FOG_DENSITY = 0.0


def _clamp(value, low, high):
    return low if value < low else (high if value > high else value)


def clamp_color(color, default=DEFAULT_FOG_COLOR):
    """Coerce *color* to a 3-tuple of floats in 0..1, or *default* if unusable."""
    try:
        r, g, b = (float(c) for c in tuple(color)[:3])
    except (TypeError, ValueError):
        return tuple(float(c) for c in default)
    return (_clamp(r, 0.0, 1.0), _clamp(g, 0.0, 1.0), _clamp(b, 0.0, 1.0))


class ViewDistance:
    """The camera's draw distance, and the fog that hides its far plane.

    One instance is shared by the viewport, the renderer and the logic thread,
    so a change from the editor spinbox or the console is picked up by the next
    frame everywhere with no rebuild and no reload -- that is what makes the
    spinbox update the fog in real time.

    ``fog_start`` and ``fog_end`` are ``None`` by default, meaning "track the
    view distance" (:data:`AUTO_FOG_START_FRAC` / :data:`AUTO_FOG_END_FRAC`).
    Setting either pins it; setting it back to ``None`` returns it to tracking.
    That is the whole reason the far plane can be pulled in on its own: with
    both left automatic, one number moves the clip and the fog together and
    nothing else about the scene changes.
    """

    __slots__ = ('_distance', 'fog_enabled', '_fog_start', '_fog_end',
                 '_fog_density', '_fog_color', '_ambient')

    def __init__(self, distance=DEFAULT_VIEW_DISTANCE):
        self._distance = _clamp(float(distance), MIN_VIEW_DISTANCE, MAX_VIEW_DISTANCE)
        self.fog_enabled = True
        self._fog_start = None
        self._fog_end = None
        self._fog_density = DEFAULT_FOG_DENSITY
        self._fog_color = tuple(DEFAULT_FOG_COLOR)
        self._ambient = (0.0, 0.0, 0.0)

    # -- view distance ----------------------------------------------------
    @property
    def distance(self):
        """Maximum render distance in world units. Nothing is drawn past it."""
        return self._distance

    @distance.setter
    def distance(self, value):
        self._distance = _clamp(float(value), MIN_VIEW_DISTANCE, MAX_VIEW_DISTANCE)

    @property
    def distance_sq(self):
        """Squared view distance -- what the per-object XZ cull compares."""
        return self._distance * self._distance

    @property
    def far_plane(self):
        """The projection far plane. Identical to :attr:`distance`.

        They are deliberately the same number rather than the far plane being
        given slack: the broad-phase cull drops an object by the distance to
        its *centre*, so a large brush straddling the boundary survives the cull
        and is then clipped by the far plane. Both limits landing on the same
        surface is what makes "nothing renders past the view distance" true of
        whole objects and of individual fragments alike.
        """
        return self._distance

    # -- fog --------------------------------------------------------------
    @property
    def fog_start(self):
        """Where fog begins, or ``None`` when it tracks the view distance."""
        return self._fog_start

    @fog_start.setter
    def fog_start(self, value):
        self._fog_start = None if value is None else max(0.0, float(value))

    @property
    def fog_end(self):
        """Where fog is opaque, or ``None`` when it tracks the view distance."""
        return self._fog_end

    @fog_end.setter
    def fog_end(self, value):
        self._fog_end = None if value is None else max(0.0, float(value))

    @property
    def fog_density(self):
        """Exponential-squared coefficient; 0 for a plain linear ramp."""
        return self._fog_density

    @fog_density.setter
    def fog_density(self, value):
        self._fog_density = max(0.0, float(value))

    @property
    def fog_color(self):
        """Fog RGB in 0..1. Also what the frame is cleared to while fog is on,
        so geometry dissolving into fog meets a background of the same colour
        instead of a visible seam."""
        return self._fog_color

    @fog_color.setter
    def fog_color(self, value):
        self._fog_color = clamp_color(value, self._fog_color)

    @property
    def ambient(self):
        """Global omnidirectional light added to every lit surface.

        Equivalent to a Light entity that reaches the whole level from no
        particular direction, but without one existing in the world -- it is
        never saved with the map, never selectable and casts no shadows. Added
        on top of each shader's own baked ambient term, so the default of black
        leaves every existing map looking exactly as it did.
        """
        return self._ambient

    @ambient.setter
    def ambient(self, value):
        self._ambient = clamp_color(value, (0.0, 0.0, 0.0))

    def set_ambient_level(self, level):
        """Set a neutral grey ambient of *level* (a single 0..1 scalar)."""
        lvl = _clamp(float(level), 0.0, 1.0)
        self._ambient = (lvl, lvl, lvl)

    # -- derivation -------------------------------------------------------
    def resolve(self):
        """The effective ``(start, end)`` the shaders should use, in world units.

        Applies the automatic fractions for whichever of the two is unset, then
        enforces the invariant the whole feature rests on:

        * ``end`` never exceeds :data:`MAX_FOG_END_FRAC` of the view distance,
          so fog is opaque strictly before the clip, and
        * ``start`` is at least :data:`MIN_FOG_BAND` below ``end``, so the ramp
          is never a hard line.

        Clamping ``end`` first and ``start`` second means an over-large
        ``fog_start`` is pulled back with the band rather than inverting it.
        """
        far = self._distance
        end = self._fog_end if self._fog_end is not None else far * AUTO_FOG_END_FRAC
        end = _clamp(end, MIN_FOG_BAND, far * MAX_FOG_END_FRAC)

        start = self._fog_start if self._fog_start is not None else far * AUTO_FOG_START_FRAC
        start = _clamp(start, 0.0, end - MIN_FOG_BAND)
        return (start, end)

    def fog_factor(self, dist):
        """The 0..1 fog blend at *dist* world units from the camera.

        The reference implementation of what the shaders compute, kept here so
        the guarantee ("opaque before the clip") is testable without a GL
        context. Distance is radial from the camera, not view-space depth.
        """
        if not self.fog_enabled:
            return 0.0
        start, end = self.resolve()
        if dist <= start:
            return 0.0
        if dist >= end:
            return 1.0
        linear = (dist - start) / (end - start)
        if self._fog_density > 0.0:
            import math
            d = self._fog_density * (dist - start)
            exponential = 1.0 - math.exp(-d * d)
            return _clamp(max(linear, exponential), 0.0, 1.0)
        return linear

    def describe(self):
        """``(label, value)`` rows for the console's ``r_list`` readout."""
        start, end = self.resolve()
        auto_s = "" if self._fog_start is not None else " (auto)"
        auto_e = "" if self._fog_end is not None else " (auto)"
        r, g, b = self._fog_color
        ar, ag, ab = self._ambient
        return [
            ("View Distance", f"{self._distance:.0f}"),
            ("Distance Fog", "ON" if self.fog_enabled else "OFF"),
            ("Fog Start", f"{start:.0f}{auto_s}"),
            ("Fog End", f"{end:.0f}{auto_e}"),
            ("Fog Density", f"{self._fog_density:.4f}"),
            ("Fog Color", f"[{r:.2f}, {g:.2f}, {b:.2f}]"),
            ("Ambient Light", f"[{ar:.2f}, {ag:.2f}, {ab:.2f}]"),
        ]
