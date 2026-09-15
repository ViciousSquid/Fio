"""
Simulation tiers for Big World: :class:`TierClassifier`.

Cell residency answers *is this object in memory and in the scene?* It does not
answer *is it worth simulating at full rate?* — and in a world whose resident
region is itself thousands of units across, those are not the same question. An
entity two cells away is resident, collidable and drawable, and still has no
business running per-frame perception.

This module answers the second question, and only the second, using the four
tiers the engine names in :mod:`engine.spatial`:

``NEAR``      full fidelity
``ACTIVE``    resident and simulated, possibly at a reduced rate
``DISTANT``   persistent state is authoritative, live simulation suppressed
``DORMANT``   not simulated at all; parked or unloaded, identity unaffected

**Fio defines what the tiers mean. It does not define what a system does at
each one.** Nothing here touches AI, movement, animation or gameplay state; the
entire output is an integer stamped under :data:`engine.spatial.SIM_TIER_KEY`.
A consumer reads it with :func:`engine.spatial.tier_of` and decides for itself —
and reads ``NEAR`` on a map with no Big World session, because nothing stamped
it.

The tier is a property of a cell, not of a distance
---------------------------------------------------
This is the design decision the rest of the module falls out of, so it is worth
stating plainly.

The obvious implementation — walk the world each frame, measure every object's
distance, assign a tier — is exactly the per-frame global scan Big World exists
to abolish. It would make simulation cost scale with total world population,
which is the one thing §22 says must not happen, and it would do so in the
module whose entire purpose is to stop it.

So a tier is assigned to a **cell**, and its entities inherit it:

.. code-block:: text

    player crosses a cell boundary
            ↓
    residency delta (the manager's existing work)
            ↓
    cell tiers re-evaluated        O(active cells)
            ↓
    entities in cells whose tier changed are stamped
            ↓
    consumers read the stamp and react

Both numbers are bounded by the activation radius, not by the world. At the
default radii the active set is roughly 80 cells, and a one-cell step changes
the tier of a thin ring of them; a world of ten million objects costs the same
per crossing as a world of ten thousand. Between crossings the classifier does
nothing at all — there is no per-frame entry point to call.

That the same event drives both residency and tiering is not an optimisation.
It is what makes §13 true by construction: the streamer and the tier model
cannot disagree about how far out the world is live, because they are answering
from the same evaluation.

What it costs
-------------
Tier boundaries land on 512-unit cell edges, so a tier is conservative by up to
one cell — an entity 1300 units away can read ``NEAR`` when the boundary is
1024, because the cell it sits in reaches inside the radius. That errs towards
*more* simulation, never less, which is the safe direction: a system is never
told to stop simulating something that still matters.

A game that needs a sharper boundary than a cell already has what it needs to
draw one. Per-actor distances are gameplay's own business and gameplay is
already measuring them; Fio's job is to stop a million dormant objects from
being considered in the first place, not to decide the exact metre at which an
NPC stops looking around.

Scope
-----
Tiers are assigned to **entities** — ``cell.things`` — and to persistent globals.
Not to brushes and not to lights: neither simulates, so "how live is it" is
already fully answered for them by residency, and both are filed into several
cells at once (a brush by its footprint, a light by its radius), which would
make a single inherited tier ambiguous. An entity is a point and is filed into
exactly one cell, so its tier is unambiguous by construction.

Plain Python and stdlib only, like the rest of the plugin's bookkeeping, so it
runs in the editor, in the standalone ``.fiopak`` player and under a head-less
test without dragging in NumPy, Qt or GL.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from engine.spatial import (PARKED_DISABLED_KEY, PARKED_HIDDEN_KEY,
                            SIM_TIER_KEY, TIER_ACTIVE, TIER_DISTANT,
                            TIER_DORMANT, TIER_NAMES, TIER_NEAR,
                            cell_distance_sq)

#: Outer edge of :data:`~engine.spatial.TIER_NEAR`. Fio's monster perception
#: range: inside this, an entity can see the player or be seen by it, so
#: anything that could interact this tick is fully simulated.
DEFAULT_NEAR_RADIUS = 1024.0

#: Outer edge of :data:`~engine.spatial.TIER_ACTIVE`. Defaults to Big World's
#: activation radius — the region already streamed in and live — and is
#: overwritten with the map's actual activation radius by the session.
DEFAULT_ACTIVE_RADIUS = 2048.0

#: Fraction of a tier radius a cell must be beyond a boundary before it is
#: demoted across it — §12. Mirrors the activation/deactivation gap the manager
#: already uses (2048 → 2304), so the two hysteresis models have one shape.
#: Promotion uses the plain radius; only demotion is sticky.
TIER_HYSTERESIS = 0.125


def _props(obj):
    """The mutable property dict of a Thing-like object or a raw dict."""
    props = getattr(obj, 'properties', None)
    if isinstance(props, dict):
        return props
    if isinstance(obj, dict):
        return obj
    return None


def is_parked(obj) -> bool:
    """Whether a streaming layer has parked ``obj`` (§9).

    Deliberately *not* ``hidden or disabled``. Those flags mean two different
    things — "the mapper hid this" and "Big World parked it a moment ago" — and
    the engine's parking markers are what tell them apart. A trigger the mapper
    authored hidden, or an entity disabled by map logic, is still a resident
    part of the live world; only an object the streamer actually parked is
    dormant.
    """
    props = _props(obj)
    if props is None:
        return False
    return PARKED_HIDDEN_KEY in props or PARKED_DISABLED_KEY in props


class TierDelta:
    """What one :meth:`TierClassifier.update` changed.

    ``cell_changes`` is ``[(coord, previous, new), ...]`` for the cells that
    moved tier; ``changes`` is ``[(entity, previous, new), ...]`` for the
    entities restamped as a result. ``evaluated_cells`` is how many cells were
    measured — the pass's actual cost, and the number a performance test should
    assert stays proportional to the active set rather than to the world.
    """

    __slots__ = ('cell_changes', 'changes', 'evaluated_cells', 'counts')

    def __init__(self):
        self.cell_changes: List[Tuple[Tuple[int, int], Optional[int], int]] = []
        self.changes: List[Tuple[object, Optional[int], int]] = []
        self.evaluated_cells = 0
        #: Per-tier cell population of the active set after the pass.
        self.counts: Dict[int, int] = {TIER_NEAR: 0, TIER_ACTIVE: 0,
                                       TIER_DISTANT: 0, TIER_DORMANT: 0}

    @property
    def changed(self) -> bool:
        return bool(self.cell_changes)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        c = self.counts
        return ('<TierDelta %d cells changed of %d, %d entities | %s>'
                % (len(self.cell_changes), self.evaluated_cells,
                   len(self.changes),
                   ' '.join('%s=%d' % (TIER_NAMES[t], c[t])
                            for t in (TIER_NEAR, TIER_ACTIVE, TIER_DISTANT))))


class TierClassifier:
    """Tiers the active cells of a :class:`~plugins.bigworld.manager.BigWorldManager`.

    *near_radius* and *active_radius* are the two boundaries, clamped so
    ``near <= active`` for the same reason the manager clamps its deactivation
    radius: an inverted pair would make every cell flap on every crossing.

    Owns no world state beyond the tier it last gave each active cell, so a
    session can be torn down by dropping it.
    """

    def __init__(self, near_radius: float = DEFAULT_NEAR_RADIUS,
                 active_radius: float = DEFAULT_ACTIVE_RADIUS,
                 hysteresis: float = TIER_HYSTERESIS):
        self.hysteresis = max(0.0, float(hysteresis))
        self.near_radius = 0.0
        self.active_radius = 0.0
        #: coord -> the tier this cell currently holds. Only active cells are
        #: present; a cell that leaves the active set leaves this map, and its
        #: entities are stamped DORMANT by :meth:`park` as part of the
        #: residency delta.
        self._cell_tier: Dict[Tuple[int, int], int] = {}
        #: False until a pass has run against a real player position. Means
        #: "no relevance information" — an editor preview, a head-less test, a
        #: session that has not started — in which case every entity reads NEAR.
        self.authoritative = False
        self.set_radii(near_radius, active_radius)

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_radii(self, near_radius: float, active_radius: float) -> None:
        """Set both boundaries, keeping ``near <= active``.

        Called once by the session from the map's Big World settings. The active
        boundary is the manager's activation radius, so the tier model and the
        streamer are configured from one number rather than two that can drift
        apart (§13).
        """
        active = max(0.0, float(active_radius))
        self.active_radius = active
        self.near_radius = min(max(0.0, float(near_radius)), active)
        h = 1.0 + self.hysteresis
        self._near2 = self.near_radius * self.near_radius
        self._act2 = self.active_radius * self.active_radius
        self._near_out2 = self._near2 * h * h
        self._act_out2 = self._act2 * h * h
        # The boundaries moved even though the player did not: every cached cell
        # tier is stale, so forget them and let the next pass restamp.
        self._cell_tier.clear()

    # ------------------------------------------------------------------
    # Tier arithmetic
    # ------------------------------------------------------------------

    def tier_for_distance_sq(self, d2: float, previous: Optional[int] = None) -> int:
        """The tier for a squared distance, given the cell's previous tier.

        Each boundary is sticky *outward* only: a cell already inside a tier
        must pass the boundary plus the hysteresis band to leave it, while one
        coming inward promotes at the plain radius. Expressed as two thresholds
        chosen from *previous* rather than as a post-hoc fix-up, so a cell that
        crosses both bands in one step lands where its distance says it should
        instead of being pinned at the tier it used to hold.
        """
        near_limit = self._near2 if (previous is not None
                                     and previous > TIER_NEAR) else self._near_out2
        if d2 <= near_limit:
            return TIER_NEAR
        act_limit = self._act2 if (previous is not None
                                   and previous > TIER_ACTIVE) else self._act_out2
        if d2 <= act_limit:
            return TIER_ACTIVE
        return TIER_DISTANT

    # ------------------------------------------------------------------
    # The one entry point
    # ------------------------------------------------------------------

    def update(self, manager, px: float, pz: float) -> TierDelta:
        """Re-tier the active cells and restamp the entities of those that moved.

        Call this from the residency path, on the crossings that made the
        manager recompute its active set — not per frame. There is deliberately
        no per-frame entry point: between crossings the answer cannot have
        changed by more than the cell granularity the model already accepts.

        Cost is ``O(active cells)`` to measure plus ``O(entities in cells whose
        tier changed)`` to stamp. Neither term mentions the world.
        """
        delta = TierDelta()
        self.authoritative = True
        cell_size = manager.cell_size
        active = manager.active_cells
        previous = self._cell_tier

        # Cells that have left the active set are no longer tiered here; their
        # entities are stamped DORMANT by the session through park(), which is
        # part of the residency delta and therefore already paid for.
        for coord in [c for c in previous if c not in active]:
            del previous[coord]

        for coord in active:
            delta.evaluated_cells += 1
            prev = previous.get(coord)
            tier = self.tier_for_distance_sq(
                cell_distance_sq(coord[0], coord[1], px, pz, cell_size), prev)
            delta.counts[tier] += 1
            if prev == tier:
                continue
            previous[coord] = tier
            delta.cell_changes.append((coord, prev, tier))
            cell = manager.cells.get(coord)
            if cell is None:
                continue
            for thing in cell.things:
                self._stamp(thing, tier, delta)
        return delta

    def _stamp(self, obj, tier: int, delta: TierDelta) -> None:
        """Write *tier* onto *obj*, recording it only if it actually changed."""
        props = _props(obj)
        if props is None:
            return
        prev = props.get(SIM_TIER_KEY)
        if prev == tier:
            return
        props[SIM_TIER_KEY] = tier
        delta.changes.append((obj, prev, tier))

    # ------------------------------------------------------------------
    # Residency edges
    # ------------------------------------------------------------------

    def park(self, obj) -> None:
        """Stamp ``DORMANT`` on an entity the streamer has just parked.

        Driven off the manager's entering/leaving lists, so keeping the
        non-resident part of the world correctly tiered costs what the player's
        movement costs — not what the world's population costs (§20).
        """
        props = _props(obj)
        if props is not None:
            props[SIM_TIER_KEY] = TIER_DORMANT

    def unpark(self, obj) -> None:
        """Drop the stamp from an entity the streamer has just brought back.

        Removed rather than set: an unstamped entity reads ``NEAR``, so a newly
        resident one is fully simulated until :meth:`update` — running later in
        the same crossing — places it. Erring towards more simulation for an
        instant is the safe direction.
        """
        props = _props(obj)
        if props is not None:
            props.pop(SIM_TIER_KEY, None)

    def pin(self, objects: Iterable) -> None:
        """Stamp ``NEAR`` on entities that must never be demoted by distance.

        Big World's persistent globals (§10). A world manager or global script
        is resident by definition and has no meaningful position to be far
        from, so it is stamped once when the session starts and never
        reconsidered — it is not in any cell, so no crossing can reach it.
        """
        for obj in objects:
            props = _props(obj)
            if props is not None:
                props[SIM_TIER_KEY] = TIER_NEAR

    def clear(self, objects: Iterable) -> None:
        """Remove every tier stamp this classifier wrote (play-stop)."""
        for obj in objects:
            props = _props(obj)
            if props is not None:
                props.pop(SIM_TIER_KEY, None)
        self._cell_tier.clear()
        self.authoritative = False

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def cell_tier(self, coord) -> int:
        """The tier of one active cell, or ``DORMANT`` if it is not active."""
        return self._cell_tier.get((int(coord[0]), int(coord[1])), TIER_DORMANT)

    def cells_of_tier(self, tier: int) -> List[Tuple[int, int]]:
        return [c for c, t in self._cell_tier.items() if t == tier]

    def stats(self) -> dict:
        counts = {TIER_NEAR: 0, TIER_ACTIVE: 0, TIER_DISTANT: 0}
        for tier in self._cell_tier.values():
            counts[tier] = counts.get(tier, 0) + 1
        return {
            'authoritative': self.authoritative,
            'near_radius': self.near_radius,
            'active_radius': self.active_radius,
            'hysteresis': self.hysteresis,
            'tier_cells': {TIER_NAMES[t]: n for t, n in counts.items()},
        }
