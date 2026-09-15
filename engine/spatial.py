"""World locality: the one 512-unit cell convention Fio partitions space with.

Fio has always bucketed the world into integer ``(cell_x, cell_z)`` columns of
:data:`CELL_SIZE` units using ``floor(coord / cell_size)`` — that is what
:class:`engine.physics.SpatialGrid` does for collision queries and what Big
World's streaming lifecycle sits on top of.  The maths used to be written out
twice, once in each place, which is how two systems that are *supposed* to agree
about which cell a brush is in end up disagreeing.  It lives here now, and both
read it from here.

Deliberately stdlib-only — no NumPy, no PyGLM, no Qt.  ``engine.physics`` needs
PyGLM and the standalone player does not, so putting the shared convention in
its own module is what lets the player, a plugin and a head-less test all use it
without dragging the renderer in.

There is exactly one implementation of "which cells does this box touch?" in
Fio, and it is :func:`cells_for_aabb`.  Anything that needs spatial locality
buckets through :class:`CellIndex` rather than growing a grid of its own.
"""

from __future__ import annotations

import math

#: Cell edge length in world units.  512 is the size the collision grid has
#: always used; Big World streams the same columns.
CELL_SIZE = 512.0

#: Key a streaming layer writes an object's *authored* ``hidden`` value under
#: while it is parking that object outside the active region.
#:
#: Parking reuses ``hidden`` because every per-frame consumer already skips
#: hidden objects, which is what makes streaming free at the draw and tick
#: sites.  The cost is that "hidden" then means two different things, and code
#: that builds a *durable* structure from the flag — the collision grid, most
#: importantly — must not confuse "the mapper hid this" with "it is out of
#: range this second".  The presence of this key is what tells them apart, and
#: :func:`authored_hidden` is the question to ask.
PARKED_HIDDEN_KEY = '_bw_parked_hidden'

#: The same, for the ``disabled`` flag that parks an entity's gameplay.
PARKED_DISABLED_KEY = '_bw_parked_disabled'


# --- Simulation tiers -------------------------------------------------------
#
# Residency and simulation are different questions.  A cell is resident or it
# is not; an object that *is* resident may still not be worth simulating at
# full rate.  These four names are how Fio says which, and they are here rather
# than in the streaming plugin for one reason: a system that wants to ask "how
# live is this thing?" must be able to name a tier without importing Big World.
#
# Nothing here runs.  It is four integers, a property key and a dict read.  The
# classifier that assigns tiers lives in ``plugins.bigworld.tiers`` and is only
# ever built by a live session, so a map that does not use Big World never
# executes a line of tier code — and its objects, never stamped, read back as
# :data:`TIER_NEAR`, which is ordinary Fio: simulate everything.

#: Full runtime fidelity.  Everything a system normally does, it does.
TIER_NEAR = 0
#: Resident and simulated, but a system may legitimately do less per frame —
#: stagger decisions, skip per-frame perception, coarsen movement.
TIER_ACTIVE = 1
#: Persistent state exists and is authoritative; expensive live simulation is
#: suppressed.  Coarse/event-driven advancement only.
TIER_DISTANT = 2
#: No simulation.  The object may be parked, or gone from memory entirely; its
#: identity and persistent state are unaffected either way.
TIER_DORMANT = 3

#: Indexable by tier, for debug text and test failure messages.
TIER_NAMES = ('NEAR', 'ACTIVE', 'DISTANT', 'DORMANT')

#: Property key a classifier stamps an object's current tier under.
#:
#: The stamp, not the classifier's arrays, is the contract.  It is what lets an
#: AI thread read a tier without touching the classifier across threads, and
#: what makes the absent case correct by construction: ``get(SIM_TIER_KEY,
#: TIER_NEAR)``.
SIM_TIER_KEY = '_sim_tier'


def tier_of(obj, default=TIER_NEAR):
    """The simulation tier stamped on ``obj``, or *default* if there is none.

    Accepts a brush dict or a Thing-like object with a ``properties`` dict, so
    one reader serves both.  Defaulting to :data:`TIER_NEAR` is the whole point:
    an unclassified world is a fully simulated world, which is what an ordinary
    map, an editor preview and a head-less test all want.
    """
    props = getattr(obj, 'properties', None)
    if not isinstance(props, dict):
        if not isinstance(obj, dict):
            return default
        props = obj
    tier = props.get(SIM_TIER_KEY)
    if tier is None:
        return default
    return int(tier)


def authored_hidden(obj):
    """Whether ``obj`` is hidden *by the map*, ignoring streaming.

    For an object no streaming layer has touched this is just its ``hidden``
    flag.  For a parked one it is the value the layer saved — so the object
    still takes part in anything built to last, and comes back correctly when
    the cell it lives in is activated again.
    """
    if PARKED_HIDDEN_KEY in obj:
        return bool(obj[PARKED_HIDDEN_KEY])
    return bool(obj.get('hidden'))


def cell_of_point(x, z, cell_size=CELL_SIZE):
    """The integer cell containing world point ``(x, z)``."""
    inv = 1.0 / cell_size
    return (int(math.floor(x * inv)), int(math.floor(z * inv)))


def cell_range(min_x, min_z, max_x, max_z, cell_size=CELL_SIZE):
    """``(cx0, cz0, cx1, cz1)`` inclusive cell bounds of an XZ box.

    The raw integer ranges, for callers that want to walk them without
    materialising a list (:meth:`CellIndex.insert` and the collision grid's
    populate loop both do).
    """
    inv = 1.0 / cell_size
    return (int(math.floor(min_x * inv)), int(math.floor(min_z * inv)),
            int(math.floor(max_x * inv)), int(math.floor(max_z * inv)))


def cells_for_aabb(min_x, min_z, max_x, max_z, cell_size=CELL_SIZE):
    """Every cell an XZ axis-aligned box overlaps, both edges inclusive.

    An object spanning a boundary is *referenced* from each cell it touches; it
    is never split, and never copied.
    """
    cx0, cz0, cx1, cz1 = cell_range(min_x, min_z, max_x, max_z, cell_size)
    return [(cx, cz)
            for cx in range(cx0, cx1 + 1)
            for cz in range(cz0, cz1 + 1)]


def cell_bounds(cell_x, cell_z, cell_size=CELL_SIZE):
    """World-space XZ bounds of a cell as ``(min_x, min_z, max_x, max_z)``."""
    return (cell_x * cell_size, cell_z * cell_size,
            (cell_x + 1) * cell_size, (cell_z + 1) * cell_size)


def cell_distance_sq(cell_x, cell_z, px, pz, cell_size=CELL_SIZE):
    """Squared distance from ``(px, pz)`` to the nearest edge of a cell.

    Zero when the point is inside.  Comparing this against ``radius ** 2`` gives
    a true circle of coverage — a cell counts the moment any part of it is
    within the radius — rather than a cell-centre approximation.
    """
    min_x, min_z, max_x, max_z = cell_bounds(cell_x, cell_z, cell_size)
    dx = 0.0
    if px < min_x:
        dx = min_x - px
    elif px > max_x:
        dx = px - max_x
    dz = 0.0
    if pz < min_z:
        dz = min_z - pz
    elif pz > max_z:
        dz = pz - max_z
    return dx * dx + dz * dz


class CellIndex:
    """Objects bucketed into :data:`CELL_SIZE` columns by their XZ footprint.

    The bucketing primitive underneath both of Fio's spatial users.  It holds
    *references*: the same brush dict or entity the editor and engine own, filed
    under every cell it touches, so an object's identity and data still live in
    exactly one place.

    :attr:`cells` is a plain ``{(cx, cz): [obj, ...]}`` dict and is read
    directly by the collision hot paths — the lookup a query does is one dict
    ``get``, with no wrapper object in between.
    """

    __slots__ = ('cell_size', 'cells')

    def __init__(self, cell_size=CELL_SIZE):
        self.cell_size = float(cell_size)
        self.cells = {}

    def clear(self):
        self.cells.clear()

    def insert(self, obj, min_x, min_z, max_x, max_z):
        """File ``obj`` under every cell its XZ footprint overlaps."""
        cx0, cz0, cx1, cz1 = cell_range(min_x, min_z, max_x, max_z,
                                        self.cell_size)
        cells = self.cells
        for cx in range(cx0, cx1 + 1):
            for cz in range(cz0, cz1 + 1):
                bucket = cells.get((cx, cz))
                if bucket is None:
                    cells[(cx, cz)] = [obj]
                else:
                    bucket.append(obj)

    def insert_point(self, obj, x, z):
        """File ``obj`` under the single cell containing ``(x, z)``."""
        coord = cell_of_point(x, z, self.cell_size)
        bucket = self.cells.get(coord)
        if bucket is None:
            self.cells[coord] = [obj]
        else:
            bucket.append(obj)

    def cell(self, coord):
        """The bucket for one cell (an empty tuple when nothing is filed)."""
        return self.cells.get(coord, ())

    def cells_within(self, px, pz, radius):
        """Occupied cells whose area intersects the circle ``(px, pz, radius)``."""
        return cells_within(self.cells, px, pz, radius, self.cell_size)


def cells_within(cells, px, pz, radius, cell_size=CELL_SIZE):
    """Which of ``cells`` the circle ``(px, pz, radius)`` reaches.

    ``cells`` is any mapping keyed by ``(cell_x, cell_z)`` — a
    :class:`CellIndex`'s buckets, or Big World's cell records; only the keys are
    read, so one radius query serves both.

    Candidates come from the integer square bounding the circle — cheap range
    arithmetic over the grid — and are kept only when their nearest edge is
    inside the radius, which gives a true circle of coverage.  Cells the mapping
    does not have are skipped outright, so the cost is proportional to occupied
    local cells rather than to the size of the world.
    """
    r2 = radius * radius
    reach = int(math.ceil(radius / cell_size)) + 1
    bx, bz = cell_of_point(px, pz, cell_size)
    out = set()
    for dx in range(-reach, reach + 1):
        cx = bx + dx
        for dz in range(-reach, reach + 1):
            coord = (cx, bz + dz)
            if coord not in cells:
                continue
            if cell_distance_sq(cx, coord[1], px, pz, cell_size) <= r2:
                out.add(coord)
    return out
