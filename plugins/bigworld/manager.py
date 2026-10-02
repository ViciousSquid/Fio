"""
:class:`BigWorldManager` — the cell manager at the heart of Big World.

It builds a UUID-addressed index of the world's brushes, entities and lights,
groups them into 512-unit cells (the same grid :class:`engine.physics.SpatialGrid`
uses), and answers the only question the runtime asks each time the player
moves: *which cells are active, and what just entered or left the active set?*

Design constraints honoured here
--------------------------------
* **Reuse, don't replace.** Cell coordinates and the multi-cell spanning rule
  are the spatial grid's, imported from :mod:`.cell`. No BVH / octree / BSP.
* **Identity is the UUID, cells are metadata.** Every object is keyed by its
  stable UUID (``brush['id']`` / ``thing.properties['id']``). Streaming a cell
  in or out never changes an object's UUID, and a brush that spans several cells
  is stored *once* and referenced from each — never duplicated.
* **Cost proportional to change, not to world size.** :meth:`update` early-outs
  until the player crosses a cell boundary, then touches only the handful of
  cells (and the objects in them) that entered or left the active region — never
  the whole world. Objects spanning several cells are reference-counted so a
  brush stays active while *any* active cell still references it.
* **Dependency-light.** Plain Python only, so the manager runs in the editor,
  the standalone player and headless tests alike. Lights and persistent globals
  are recognised by their ``type`` string / a marker property, not by importing
  editor classes.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .cell import (CELL_SIZE, BigWorldCell, CellCoord, CellState,
                   cell_distance_sq, cell_of_point, cells_for_aabb)

#: Default radius (world units) of the player's active region.
DEFAULT_ACTIVATION_RADIUS = 2048.0
#: Default outer radius at which an already-active cell is finally dropped.
#: The gap to :data:`DEFAULT_ACTIVATION_RADIUS` is the hysteresis band that
#: stops cells thrashing on/off when the player lingers on a boundary.
DEFAULT_DEACTIVATION_RADIUS = 2304.0

#: Entity ``type`` strings treated as global/persistent by default: they keep
#: ticking regardless of which cell they fall in (or whether they have a
#: position at all). A map may also mark any single entity persistent with a
#: truthy ``bw_persistent`` property.
DEFAULT_PERSISTENT_TYPES = frozenset({
    "worldmanager", "gamestate", "globalscript",
    "questcontroller", "logickeyvaluestore", "logicrelay",
})


def _normalise_type(type_name) -> str:
    """Lowercase + strip underscores — matches ``editor.things.from_dict``."""
    return str(type_name or "").replace("_", "").lower()


class ActivationDelta:
    """What one :meth:`BigWorldManager.update` changed.

    ``changed`` is False when the player has not crossed a cell boundary (the
    overwhelmingly common per-frame case), in which case every list is empty and
    the runtime does nothing. When it *is* True, the ``*_entering`` / ``*_leaving``
    lists hold the **net** transitions — objects whose active reference count
    went 0→1 (entering) or 1→0 (leaving) — already accounting for objects shared
    between several cells, so the runtime applies each effect exactly once.
    """

    __slots__ = ("changed", "entering_cells", "leaving_cells",
                 "brushes_entering", "brushes_leaving",
                 "things_entering", "things_leaving",
                 "lights_entering", "lights_leaving")

    def __init__(self):
        self.changed = False
        self.entering_cells: List[CellCoord] = []
        self.leaving_cells: List[CellCoord] = []
        self.brushes_entering: List[dict] = []
        self.brushes_leaving: List[dict] = []
        self.things_entering: List = []
        self.things_leaving: List = []
        self.lights_entering: List = []
        self.lights_leaving: List = []

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        if not self.changed:
            return "<ActivationDelta unchanged>"
        return (f"<ActivationDelta +{len(self.entering_cells)}/-{len(self.leaving_cells)} cells "
                f"brush +{len(self.brushes_entering)}/-{len(self.brushes_leaving)} "
                f"thing +{len(self.things_entering)}/-{len(self.things_leaving)} "
                f"light +{len(self.lights_entering)}/-{len(self.lights_leaving)}>")


class BigWorldManager:
    """Indexes a world into cells and streams the active set as the player moves."""

    def __init__(self, cell_size: float = CELL_SIZE,
                 activation_radius: float = DEFAULT_ACTIVATION_RADIUS,
                 deactivation_radius: float = DEFAULT_DEACTIVATION_RADIUS,
                 persistent_types: Optional[Iterable[str]] = None):
        self.cell_size = float(cell_size)
        self.activation_radius = float(activation_radius)
        # Never let the deactivation radius fall below the activation radius, or
        # cells would drop the same frame they were added (thrash, not hysteresis).
        self.deactivation_radius = max(float(deactivation_radius), self.activation_radius)
        self.persistent_types: Set[str] = {
            _normalise_type(t) for t in (persistent_types or DEFAULT_PERSISTENT_TYPES)
        }

        # (cell_x, cell_z) -> BigWorldCell
        self.cells: Dict[CellCoord, BigWorldCell] = {}
        # UUID -> object, the single source of identity for each kind.
        self._brush_by_id: Dict[str, dict] = {}
        self._thing_by_id: Dict[str, object] = {}
        self._light_by_id: Dict[str, object] = {}
        # Entities that ignore streaming entirely (world managers, global scripts).
        self.persistent_things: List = []

        # Live active state.
        self.active_cells: Set[CellCoord] = set()
        # UUID -> number of active cells that reference it. An object is active
        # iff its count > 0; spanning objects stay active while any active cell
        # still holds them.
        self._brush_active_ref: Counter = Counter()
        self._thing_active_ref: Counter = Counter()
        self._light_active_ref: Counter = Counter()
        self._active_brush_ids: Set[str] = set()
        self._active_thing_ids: Set[str] = set()
        self._active_light_ids: Set[str] = set()

        self._last_player_cell: Optional[CellCoord] = None

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @staticmethod
    def brush_uuid(brush: dict) -> str:
        """The persistent UUID of a brush (``brush['id']``)."""
        return str(brush.get("id", ""))

    @staticmethod
    def thing_uuid(thing) -> str:
        """The persistent UUID of an entity (``thing.properties['id']``)."""
        props = getattr(thing, "properties", None)
        if isinstance(props, dict):
            return str(props.get("id", ""))
        return ""

    def _is_light(self, thing) -> bool:
        return _normalise_type(getattr(thing, "properties", {}).get("type")) == "light"

    def _is_persistent(self, thing) -> bool:
        props = getattr(thing, "properties", {}) or {}
        if props.get("bw_persistent"):
            return True
        return _normalise_type(props.get("type")) in self.persistent_types

    # ------------------------------------------------------------------
    # Build / index
    # ------------------------------------------------------------------

    def _cell(self, coord: CellCoord) -> BigWorldCell:
        cell = self.cells.get(coord)
        if cell is None:
            cell = BigWorldCell(coord[0], coord[1])
            self.cells[coord] = cell
        return cell

    def index_world(self, brushes: Iterable[dict], things: Iterable) -> None:
        """(Re)build the whole cell index from a world's brushes and entities.

        Called once when a streaming session starts (and re-callable if the
        static world is rebuilt — a rare event, exactly like re-populating the
        spatial grid). Cheap relative to a frame budget even for hundreds of
        thousands of objects: a single linear pass that reuses the grid's cell
        maths. Existing UUIDs are read, never regenerated.
        """
        self.cells.clear()
        self._brush_by_id.clear()
        self._thing_by_id.clear()
        self._light_by_id.clear()
        self.persistent_things = []
        self.active_cells.clear()
        self._brush_active_ref.clear()
        self._thing_active_ref.clear()
        self._light_active_ref.clear()
        self._active_brush_ids.clear()
        self._active_thing_ids.clear()
        self._active_light_ids.clear()
        self._last_player_cell = None

        for brush in brushes or ():
            self.add_brush(brush)
        for thing in things or ():
            self.add_thing(thing)

    def add_brush(self, brush: dict) -> None:
        """Index one brush into every cell its XZ footprint overlaps."""
        uuid = self.brush_uuid(brush)
        if not uuid:
            return  # untracked; the editor backfills IDs, so this is rare
        self._brush_by_id[uuid] = brush
        pos = brush.get("pos", (0.0, 0.0, 0.0))
        size = brush.get("size", (0.0, 0.0, 0.0))
        # Prefer real mesh bounds when present, matching SpatialGrid.populate.
        if brush.get("_collision_mode") == "mesh" and brush.get("_mesh_bounds"):
            min_b, max_b = brush["_mesh_bounds"]
            min_x, min_z = min_b[0], min_b[2]
            max_x, max_z = max_b[0], max_b[2]
        else:
            hx, hz = size[0] * 0.5, size[2] * 0.5
            min_x, max_x = pos[0] - hx, pos[0] + hx
            min_z, max_z = pos[2] - hz, pos[2] + hz
        for coord in cells_for_aabb(min_x, min_z, max_x, max_z, self.cell_size):
            self._cell(coord).brushes.append(brush)

    def add_thing(self, thing) -> None:
        """Index one entity: a persistent global, a light, or a point entity."""
        uuid = self.thing_uuid(thing)
        if self._is_persistent(thing):
            self.persistent_things.append(thing)
            return
        pos = getattr(thing, "pos", None)
        if pos is None:
            # No position and not marked persistent: treat as global so it is
            # never silently frozen (fail safe — see §10 "do not assume every
            # entity can safely be unloaded").
            self.persistent_things.append(thing)
            return

        if self._is_light(thing):
            if uuid:
                self._light_by_id[uuid] = thing
            radius = float(getattr(thing, "properties", {}).get("radius", self.cell_size))
            min_x, max_x = pos[0] - radius, pos[0] + radius
            min_z, max_z = pos[2] - radius, pos[2] + radius
            for coord in cells_for_aabb(min_x, min_z, max_x, max_z, self.cell_size):
                self._cell(coord).lights.append(thing)
        else:
            if uuid:
                self._thing_by_id[uuid] = thing
            self._cell(cell_of_point(pos[0], pos[2], self.cell_size)).things.append(thing)

    # ------------------------------------------------------------------
    # Streaming API (state transitions; future disk streaming slots in here)
    # ------------------------------------------------------------------

    def load_cell(self, cell_x: int, cell_z: int) -> BigWorldCell:
        """Ensure a cell's contents are in memory.

        For the in-RAM milestone the whole map is already resident, so this only
        guarantees the cell exists and marks it loaded/inactive. An async
        disk-streaming milestone overrides this to read the cell off disk while
        keeping this exact signature — the runtime above it never changes.
        """
        cell = self._cell((int(cell_x), int(cell_z)))
        if cell.state == CellState.UNLOADED:
            cell.state = CellState.INACTIVE
        return cell

    def unload_cell(self, cell_x: int, cell_z: int) -> None:
        """Mark a cell unloaded (a no-op for the in-RAM milestone beyond state).

        Deactivates first if needed so an unloaded cell can never stay active.
        A disk-streaming milestone overrides this to actually free the bytes.
        """
        coord = (int(cell_x), int(cell_z))
        if coord in self.active_cells:
            self.deactivate_cell(*coord)
        cell = self.cells.get(coord)
        if cell is not None:
            cell.state = CellState.UNLOADED

    def activate_cell(self, cell_x: int, cell_z: int) -> Tuple[list, list, list]:
        """Mark a cell active and return objects that became newly active.

        Returns ``(brushes, things, lights)`` whose active reference count rose
        from 0 to 1 as a result of this cell — i.e. the objects the runtime must
        now switch *on*. Objects shared with an already-active cell are omitted
        (they were already on).
        """
        coord = (int(cell_x), int(cell_z))
        cell = self.cells.get(coord)
        if cell is None or coord in self.active_cells:
            return ([], [], [])
        self.active_cells.add(coord)
        cell.state = CellState.ACTIVE
        return (
            self._ref_up(cell.brushes, self._brush_active_ref, self._active_brush_ids,
                         self.brush_uuid),
            self._ref_up(cell.things, self._thing_active_ref, self._active_thing_ids,
                         self.thing_uuid),
            self._ref_up(cell.lights, self._light_active_ref, self._active_light_ids,
                         self.thing_uuid),
        )

    def deactivate_cell(self, cell_x: int, cell_z: int) -> Tuple[list, list, list]:
        """Mark a cell inactive and return objects that became fully inactive.

        Returns ``(brushes, things, lights)`` whose active reference count fell
        from 1 to 0 — the objects the runtime must switch *off*. Objects still
        referenced by another active cell are omitted (they stay on).
        """
        coord = (int(cell_x), int(cell_z))
        cell = self.cells.get(coord)
        if cell is None or coord not in self.active_cells:
            return ([], [], [])
        self.active_cells.discard(coord)
        cell.state = CellState.INACTIVE
        return (
            self._ref_down(cell.brushes, self._brush_active_ref, self._active_brush_ids,
                           self.brush_uuid),
            self._ref_down(cell.things, self._thing_active_ref, self._active_thing_ids,
                           self.thing_uuid),
            self._ref_down(cell.lights, self._light_active_ref, self._active_light_ids,
                           self.thing_uuid),
        )

    @staticmethod
    def _ref_up(objects, refs: Counter, active_ids: Set[str], key_fn) -> list:
        newly = []
        for obj in objects:
            uuid = key_fn(obj)
            if refs[uuid] == 0:
                active_ids.add(uuid)
                newly.append(obj)
            refs[uuid] += 1
        return newly

    @staticmethod
    def _ref_down(objects, refs: Counter, active_ids: Set[str], key_fn) -> list:
        gone = []
        for obj in objects:
            uuid = key_fn(obj)
            c = refs.get(uuid, 0)
            if c <= 1:
                refs.pop(uuid, None)
                active_ids.discard(uuid)
                gone.append(obj)
            else:
                refs[uuid] = c - 1
        return gone

    # ------------------------------------------------------------------
    # Active-cell calculation
    # ------------------------------------------------------------------

    def get_active_cells(self, player_pos, radius: Optional[float] = None) -> Set[CellCoord]:
        """The set of cells whose 512×512 area intersects the player's circle.

        A pure query with no side effects (used by :meth:`update`, tests and
        debug views). It obtains candidate cells from the *square* that bounds
        the radius — cheap integer ranges over the grid — then keeps only those
        whose nearest edge is within the circular *radius* of the player. It
        never measures distance to individual brushes; work is O(candidate
        cells), a small constant for a 2048-unit radius (≈9×9 cells).
        """
        r = self.activation_radius if radius is None else float(radius)
        px, pz = float(player_pos[0]), float(player_pos[2])
        return self._cells_within(px, pz, r)

    def _cells_within(self, px: float, pz: float, radius: float) -> Set[CellCoord]:
        r2 = radius * radius
        # Candidate square: every cell the bounding box of the circle touches.
        reach = int(math.ceil(radius / self.cell_size)) + 1
        base = cell_of_point(px, pz, self.cell_size)
        out: Set[CellCoord] = set()
        for dx in range(-reach, reach + 1):
            for dz in range(-reach, reach + 1):
                coord = (base[0] + dx, base[1] + dz)
                if coord not in self.cells:
                    continue  # empty cells never need activating
                if cell_distance_sq(coord[0], coord[1], px, pz, self.cell_size) <= r2:
                    out.add(coord)
        return out

    def update(self, player_pos, force: bool = False) -> ActivationDelta:
        """Recompute the active set for the player's position; stream the diff.

        Cheap by construction: it early-outs with an empty (``changed=False``)
        delta unless the player has crossed into a new cell (or ``force``). When
        it does run it activates only the cells that entered the region and
        deactivates only those that left, applying **hysteresis** — a cell is
        added within :attr:`activation_radius` but not dropped until beyond
        :attr:`deactivation_radius` — so a player loitering on a boundary does
        not thrash cells on and off.
        """
        delta = ActivationDelta()
        px, pz = float(player_pos[0]), float(player_pos[2])
        player_cell = cell_of_point(px, pz, self.cell_size)
        if not force and player_cell == self._last_player_cell:
            return delta
        self._last_player_cell = player_cell

        new_active = self._compute_active_with_hysteresis(px, pz)
        entering = new_active - self.active_cells
        leaving = self.active_cells - new_active
        if not entering and not leaving:
            return delta

        delta.changed = True
        delta.entering_cells = sorted(entering)
        delta.leaving_cells = sorted(leaving)
        for coord in delta.entering_cells:
            self.load_cell(*coord)
            b, t, l = self.activate_cell(*coord)
            delta.brushes_entering.extend(b)
            delta.things_entering.extend(t)
            delta.lights_entering.extend(l)
        for coord in delta.leaving_cells:
            b, t, l = self.deactivate_cell(*coord)
            delta.brushes_leaving.extend(b)
            delta.things_leaving.extend(t)
            delta.lights_leaving.extend(l)
        return delta

    def _compute_active_with_hysteresis(self, px: float, pz: float) -> Set[CellCoord]:
        """Cells that should be active given the current active set (hysteresis).

        Within one pass over the deactivation-radius candidate square: a cell
        already active is retained while it stays inside the *deactivation*
        radius; a currently-inactive cell is added only once inside the tighter
        *activation* radius.
        """
        act_r2 = self.activation_radius * self.activation_radius
        deact_r2 = self.deactivation_radius * self.deactivation_radius
        reach = int(math.ceil(self.deactivation_radius / self.cell_size)) + 1
        base = cell_of_point(px, pz, self.cell_size)
        out: Set[CellCoord] = set()
        for dx in range(-reach, reach + 1):
            for dz in range(-reach, reach + 1):
                coord = (base[0] + dx, base[1] + dz)
                if coord not in self.cells:
                    continue
                d2 = cell_distance_sq(coord[0], coord[1], px, pz, self.cell_size)
                if coord in self.active_cells:
                    if d2 <= deact_r2:
                        out.add(coord)
                elif d2 <= act_r2:
                    out.add(coord)
        return out

    # ------------------------------------------------------------------
    # Queries the runtime / renderer consume
    # ------------------------------------------------------------------

    def active_brushes(self) -> List[dict]:
        """The live active brush set (deduped across spanning cells)."""
        return [self._brush_by_id[u] for u in self._active_brush_ids
                if u in self._brush_by_id]

    def active_things(self) -> List:
        return [self._thing_by_id[u] for u in self._active_thing_ids
                if u in self._thing_by_id]

    def active_lights(self) -> List:
        return [self._light_by_id[u] for u in self._active_light_ids
                if u in self._light_by_id]

    def is_brush_active(self, brush: dict) -> bool:
        return self.brush_uuid(brush) in self._active_brush_ids

    def is_thing_active(self, thing) -> bool:
        """True if an entity is active — persistent globals are always active."""
        if self._is_persistent(thing):
            return True
        return self.thing_uuid(thing) in self._active_thing_ids \
            or self.thing_uuid(thing) in self._active_light_ids

    # ------------------------------------------------------------------
    # Stats (debug / profiling)
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """A snapshot of counts for the debug overlay and benchmarks."""
        return {
            "player_cell": self._last_player_cell,
            "activation_radius": self.activation_radius,
            "deactivation_radius": self.deactivation_radius,
            "cell_size": self.cell_size,
            "active_cells": len(self.active_cells),
            "loaded_cells": sum(1 for c in self.cells.values() if c.is_loaded),
            "total_cells": len(self.cells),
            "active_brushes": len(self._active_brush_ids),
            "total_brushes": len(self._brush_by_id),
            "active_entities": len(self._active_thing_ids),
            "total_entities": len(self._thing_by_id),
            "active_lights": len(self._active_light_ids),
            "total_lights": len(self._light_by_id),
            "persistent_entities": len(self.persistent_things),
        }
