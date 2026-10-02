"""
Cell abstraction and the 512-unit grid coordinate maths for Big World.

Everything here is deliberately dependency-light — plain Python, no PyQt, no
NumPy, no PyGLM — so it runs unchanged in the editor's play mode, in the
standalone ``.fiopak`` player, and under a headless test. It shares the *exact*
coordinate convention of :class:`engine.physics.SpatialGrid` (integer X/Z cells
of :data:`CELL_SIZE` units, ``floor(coord / cell_size)``), so a Big World cell
and a spatial-grid cell with the same ``(cell_x, cell_z)`` cover the same
512×512 patch of world. Big World does **not** introduce a second spatial
structure; it layers a streaming lifecycle over the same grid Fio already uses.

A cell is addressed by *integer* coordinates ``(cell_x, cell_z)`` — never by
floating-point world coordinates — and represents a fixed 512×512-unit column
in X/Z (unbounded in Y). Objects are *referenced* by a cell, never copied into
it: the stored brush dicts / entity objects are the same instances the editor
and engine own, so a brush's identity (its UUID) and its data live in exactly
one place regardless of how many cells it touches.
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

#: Cell edge length in world units. Matches ``engine.physics.SpatialGrid``'s
#: default ``cell_size`` so the two grids are coordinate-compatible. Big World
#: reuses this rather than defining a grid of its own.
CELL_SIZE = 512.0

#: A cell coordinate: ``(cell_x, cell_z)`` integers.
CellCoord = Tuple[int, int]


# ---------------------------------------------------------------------------
# Streaming lifecycle
# ---------------------------------------------------------------------------

class CellState:
    """The streaming lifecycle of a cell.

    ``UNLOADED``  — the cell's contents are not in memory (a future disk-streaming
                    milestone; for the in-RAM milestone a cell is loaded as soon
                    as the world is indexed).
    ``LOADING``   — contents are being brought in (async disk load; transient).
    ``INACTIVE``  — loaded and stored, but outside the activation radius: it takes
                    part in *no* runtime rendering, collision or entity ticking.
    ``ACTIVE``    — inside the activation radius: submitted to the normal Fio
                    render / physics / entity pipeline.
    ``UNLOADING`` — being evicted from memory (async disk unload; transient).

    The distinction that matters for the first milestone is **ACTIVE vs
    INACTIVE**; the LOADING/UNLOADING states exist so true asynchronous disk
    streaming can be layered on later without reshaping the runtime.
    """

    UNLOADED = "unloaded"
    LOADING = "loading"
    INACTIVE = "inactive"
    ACTIVE = "active"
    UNLOADING = "unloading"


# ---------------------------------------------------------------------------
# Coordinate helpers (shared 512-unit grid convention)
# ---------------------------------------------------------------------------

def cell_of_point(x: float, z: float, cell_size: float = CELL_SIZE) -> CellCoord:
    """Return the integer cell containing world point ``(x, z)``.

    Identical to the indexing ``SpatialGrid`` uses: ``floor(coord / cell_size)``.
    """
    inv = 1.0 / cell_size
    return (int(math.floor(x * inv)), int(math.floor(z * inv)))


def cells_for_aabb(min_x: float, min_z: float, max_x: float, max_z: float,
                   cell_size: float = CELL_SIZE) -> List[CellCoord]:
    """Every cell an XZ axis-aligned box overlaps (inclusive of both edges).

    This is exactly how ``SpatialGrid.populate`` spreads a brush that spans
    several cells across all of them — so a large brush is *referenced* from
    each cell it touches, never split or duplicated.
    """
    inv = 1.0 / cell_size
    cx0 = int(math.floor(min_x * inv))
    cx1 = int(math.floor(max_x * inv))
    cz0 = int(math.floor(min_z * inv))
    cz1 = int(math.floor(max_z * inv))
    out: List[CellCoord] = []
    for cx in range(cx0, cx1 + 1):
        for cz in range(cz0, cz1 + 1):
            out.append((cx, cz))
    return out


def cell_bounds(cell_x: int, cell_z: int,
                cell_size: float = CELL_SIZE) -> Tuple[float, float, float, float]:
    """World-space XZ bounds of a cell as ``(min_x, min_z, max_x, max_z)``."""
    return (cell_x * cell_size, cell_z * cell_size,
            (cell_x + 1) * cell_size, (cell_z + 1) * cell_size)


def cell_distance_sq(cell_x: int, cell_z: int, px: float, pz: float,
                     cell_size: float = CELL_SIZE) -> float:
    """Squared distance from point ``(px, pz)`` to the nearest edge of a cell.

    Zero when the point is inside the cell. This is the value the circular
    activation test compares against ``radius**2`` — the *nearest-point* metric,
    so a cell counts as within the radius the moment any part of it is, giving a
    true circle of coverage rather than a cell-centre approximation.
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


# ---------------------------------------------------------------------------
# Cell
# ---------------------------------------------------------------------------

class BigWorldCell:
    """One streamable 512×512 column of the world, addressed by ``(cell_x, cell_z)``.

    A cell holds *references* to the world objects whose footprint touches it,
    grouped by kind so the runtime can activate/deactivate rendering, collision,
    entity ticking and lighting independently:

    * ``brushes`` — static/geometry brush dicts (the same dicts the editor owns).
    * ``things``  — gameplay entities (pickups, monsters, triggers, movers…).
    * ``lights``  — light entities whose influence radius reaches this cell.

    An object appears in every cell its footprint overlaps (a brush spanning a
    cell boundary is referenced from each side) but is stored once in the
    manager's UUID index, so its identity is never split or duplicated.
    """

    __slots__ = ("cell_x", "cell_z", "state", "brushes", "things", "lights")

    def __init__(self, cell_x: int, cell_z: int):
        self.cell_x = int(cell_x)
        self.cell_z = int(cell_z)
        self.state = CellState.INACTIVE
        self.brushes: List[dict] = []
        self.things: List = []
        self.lights: List = []

    @property
    def key(self) -> CellCoord:
        """The cell's integer address, the key it is stored under."""
        return (self.cell_x, self.cell_z)

    @property
    def is_active(self) -> bool:
        return self.state == CellState.ACTIVE

    @property
    def is_loaded(self) -> bool:
        """True once the cell's contents are in memory (not UN/LOADING)."""
        return self.state in (CellState.INACTIVE, CellState.ACTIVE)

    def object_count(self) -> int:
        return len(self.brushes) + len(self.things) + len(self.lights)

    def bounds(self, cell_size: float = CELL_SIZE):
        return cell_bounds(self.cell_x, self.cell_z, cell_size)

    def clear(self) -> None:
        self.brushes.clear()
        self.things.clear()
        self.lights.clear()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (f"<BigWorldCell ({self.cell_x},{self.cell_z}) {self.state} "
                f"b={len(self.brushes)} t={len(self.things)} l={len(self.lights)}>")
