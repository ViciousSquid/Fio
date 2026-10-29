"""Streaming the lights near the player, the way Big World streams cells.

A map can hold far more lights than a shader has room for, as long as only
the ones near the player light at once. Finding those by measuring every
light in a huge world every frame would make each frame pay for the whole
map, so :class:`LightStream` keeps a small *candidate* set instead and does
the per-frame work on that alone:

* **The candidate set** is every light within :data:`CANDIDATE_RADIUS` of the
  player. It is gathered over the whole map only when it can have changed:
  the player crossed into another :data:`CELL_SIZE` cell (as Big World
  re-streams on a cell crossing), the table's light rows were renumbered
  (lights added or removed), or :data:`REFRESH_SECONDS` have passed -- many
  lights move (torches carried by NPCs, fires, spell effects), and the
  refresh is what lets one walk in from afar.
* **Each frame**, only the candidates are measured. A light lights within
  :data:`ACTIVE_LIGHT_RANGE`; one already lit stays lit until it is beyond
  :data:`DEACTIVATION_RANGE` -- the same **hysteresis** Big World gives its
  cells, so a light on the edge does not flicker on and off as the player
  loiters there. The lit lights come nearest first, so every shader's light
  cap keeps the closest.

Intensity eases to zero over the last 256 units inside the activation
boundary. This finishes before either the distance filter or Big World can
park the light, including when alternating render snapshots reset hysteresis.
Selection, light budgets and authored intensities are unchanged.

The candidate radius reaches past the deactivation range by more than a
light can travel between refreshes, so a lit light is never lost from the
candidates while it should still be lit.
"""

import time

import numpy as np

from engine.spatial import CELL_SIZE

#: A light within this distance of the player lights (world units).
ACTIVE_LIGHT_RANGE = 2048.0
#: A light already lit stays lit until it is this far away (hysteresis).
DEACTIVATION_RANGE = ACTIVE_LIGHT_RANGE + 256.0
#: Distance over which a departing light fades before reaching the cutoff.
FADE_DISTANCE = 256.0
#: Every light within this distance is a candidate, measured each frame.
CANDIDATE_RADIUS = DEACTIVATION_RANGE + CELL_SIZE
#: How often the candidates are gathered afresh even without a cell crossing
#: (seconds), for lights that move.
REFRESH_SECONDS = 0.5


def _cell(focus):
    return (int(np.floor(focus[0] / CELL_SIZE)), int(np.floor(focus[2] / CELL_SIZE)))


class LightStream:
    """The lit lights near a moving focus (see the module docstring)."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._key = None              # (table id, table generation)
        self._cell = None             # the focus cell the candidates were gathered in
        self._gathered_at = -1e18
        self._candidates = np.empty(0, dtype=np.int32)
        self._lit = np.empty(0, dtype=np.int32)
        #: Render-only intensity factors, in the order returned by select().
        self.weights = np.empty(0, dtype=np.float32)
        #: How many times the candidates were gathered over the whole map
        #: (diagnostics and tests).
        self.gathers = 0

    def reset(self):
        self._key = None
        self._candidates = np.empty(0, dtype=np.int32)
        self._lit = np.empty(0, dtype=np.int32)
        self.weights = np.empty(0, dtype=np.float32)

    @staticmethod
    def _dist_sq(table, slots, focus):
        d = table.pos[slots] - focus
        return np.einsum('ij,ij->i', d, d)

    def _gather(self, table, light_slots, focus):
        """Every light within :data:`CANDIDATE_RADIUS` of *focus*: the one
        pass over all the map's lights, run only when it can have changed."""
        self.gathers += 1
        if len(light_slots):
            d2 = self._dist_sq(table, light_slots, focus)
            self._candidates = light_slots[d2 <= CANDIDATE_RADIUS * CANDIDATE_RADIUS]
        else:
            self._candidates = np.empty(0, dtype=np.int32)

    def select(self, table, light_slots, focus, usable=None, reach=None, fade_reach=None):
        """The slots of the lights to light this frame, nearest first.

        *light_slots* are all the table's light rows; *usable(slots)* returns
        a mask of those that may light now (enabled, not hidden). *reach*
        overrides :data:`ACTIVE_LIGHT_RANGE` (deactivation keeps its margin).
        *fade_reach* can end the intensity fade earlier when a world streamer
        parks lights inside that range; it never changes slot selection.
        """
        focus = np.asarray(focus, dtype=np.float64)[:3]
        now = self._clock()
        key = (id(table), getattr(table, 'generation', None))
        cell = _cell(focus)
        if key != self._key:
            self._key = key
            self._lit = np.empty(0, dtype=np.int32)    # rows renumbered
            self._cell = None
        if cell != self._cell or now - self._gathered_at >= REFRESH_SECONDS:
            self._cell = cell
            self._gathered_at = now
            self._gather(table, light_slots, focus)
        cand = self._candidates
        if len(cand) and usable is not None:
            cand = cand[usable(cand)]
        if not len(cand):
            self._lit = cand
            self.weights = np.empty(0, dtype=np.float32)
            return cand
        on = ACTIVE_LIGHT_RANGE if reach is None else float(reach)
        off = on + (DEACTIVATION_RANGE - ACTIVE_LIGHT_RANGE)
        d2 = self._dist_sq(table, cand, focus)
        lit = d2 <= on * on
        if len(self._lit):
            # Hysteresis: a light lit last frame holds on until it is past
            # the deactivation range.
            lit |= np.isin(cand, self._lit, assume_unique=True) & (d2 <= off * off)
        cand, d2 = cand[lit], d2[lit]
        order = np.argsort(d2, kind='stable')
        self._lit = cand[order]
        # Smoothstep has zero slope at both ends, avoiding a brightness kink.
        # Use the inner boundary: render snapshots can change table identity,
        # and a previously lit row can then disappear at `on` rather than `off`.
        end = on if fade_reach is None else min(on, max(float(fade_reach), 0.0))
        width = min(FADE_DISTANCE, max(end, 0.0))
        if width > 0.0:
            t = np.clip((end - np.sqrt(d2[order])) / width, 0.0, 1.0)
            self.weights = (t * t * (3.0 - 2.0 * t)).astype(np.float32)
        else:
            self.weights = np.zeros(len(order), dtype=np.float32)
        return self._lit
