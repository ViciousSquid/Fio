"""
Cell abstraction and the streaming lifecycle for Big World.

The 512-unit grid coordinate maths is *not* here: it is the engine's, imported
from :mod:`engine.spatial`, the same module :class:`engine.physics.SpatialGrid`
buckets collision brushes with. A Big World cell and a spatial-grid cell with
the same ``(cell_x, cell_z)`` therefore cover the same 512x512 patch of world by
construction, not by two copies of ``floor(coord / 512)`` happening to agree.
Big World does **not** introduce a second spatial structure; it layers a
streaming lifecycle over the grid Fio already uses.

``engine.spatial`` is deliberately stdlib-only, so importing it here keeps this
module running unchanged in the editor's play mode, in the standalone
``.fiopak`` player, and under a headless test.

A cell is addressed by *integer* coordinates ``(cell_x, cell_z)`` — never by
floating-point world coordinates — and represents a fixed 512x512-unit column
in X/Z (unbounded in Y). Objects are *referenced* by a cell, never copied into
it: the stored brush dicts / entity objects are the same instances the editor
and engine own, so a brush's identity (its UUID) and its data live in exactly
one place regardless of how many cells it touches.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from engine.spatial import (CELL_SIZE, CellIndex, cell_bounds, cell_distance_sq,
                            cell_of_point, cell_range, cells_for_aabb,
                            cells_within)

#: A cell coordinate: ``(cell_x, cell_z)`` integers.
CellCoord = Tuple[int, int]

__all__ = [
    "CELL_SIZE", "CellCoord", "CellIndex", "CellState", "BigWorldCell",
    "cell_bounds", "cell_distance_sq", "cell_of_point", "cell_range",
    "cells_for_aabb", "cells_within",
]


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
