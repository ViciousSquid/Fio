"""
Disk-streaming milestone for Big World: cells that are actually *freed*.

The in-RAM milestone (:mod:`plugins.bigworld.runtime`) keeps every object
resident and only toggles ``hidden``/``disabled`` flags as cells stream in and
out — nothing is ever freed, so an unloaded cell's changes are trivially still
in memory. This module is the next step: a streaming session that **removes an
unloaded cell's objects from the live scene and from memory**, and re-instantiates
them from a *cell source* (its "disk") when the cell streams back in.

Because the whole world is no longer resident, two things that were free before
now have to be earned, and both are the point of this module:

* **Each cell's base is captured the first time it streams in** (from the
  pristine source), not once up-front from a resident world — so a delta can
  still be measured for a cell that was never all in memory at the same time.
* **A cell's changes are committed to the persistent registry *before* it is
  freed**, so they outlive the free. On reload the base is re-instantiated and
  the saved delta re-applied by UUID.

The persistent registry is the single source of truth for a save here (the world
can't be serialized wholesale — most of it isn't loaded), and it reuses the exact
same per-cell / per-UUID delta shape and the core delta maths
(:func:`engine.savegame.compute_delta_level`) as the in-RAM path, so a Big World
save file is identical whichever session produced it.

Everything is dependency-light (plain Python; ``glm``/NumPy positions are read by
index) so it runs and is tested head-less. Live-engine integration (mutating the
scene's ``things``/``brushes`` lists mid-frame and invalidating the renderer /
physics caches for the freed objects) is the remaining engine-side work; the
streaming, freeing, base-capture, registry and save/load logic here is complete
and tested on its own.
"""

from __future__ import annotations

import copy
import math
import os
from collections import Counter
from typing import Dict, List, Optional, Set, Tuple

from .cell import (CELL_SIZE, CellCoord, cell_distance_sq, cell_of_point,
                   cells_for_aabb)
from .manager import (DEFAULT_ACTIVATION_RADIUS, DEFAULT_DEACTIVATION_RADIUS,
                      DEFAULT_PERSISTENT_TYPES, _normalise_type)
from .persistence import cell_key_for_pos, normalize_streaming_state

_BRUSH = "brush"
_THING = "thing"


# ---------------------------------------------------------------------------
# Cell sources — where a cell's *pristine base* comes from
# ---------------------------------------------------------------------------

def _obj_pos(obj):
    if isinstance(obj, dict):
        return obj.get("pos", (0.0, 0.0, 0.0))
    return getattr(obj, "pos", (0.0, 0.0, 0.0))


def _brush_cells(brush: dict, cell_size: float) -> List[CellCoord]:
    pos = brush.get("pos", (0.0, 0.0, 0.0))
    size = brush.get("size", (0.0, 0.0, 0.0))
    if brush.get("_collision_mode") == "mesh" and brush.get("_mesh_bounds"):
        min_b, max_b = brush["_mesh_bounds"]
        min_x, min_z = min_b[0], min_b[2]
        max_x, max_z = max_b[0], max_b[2]
    else:
        hx, hz = size[0] * 0.5, size[2] * 0.5
        min_x, max_x = pos[0] - hx, pos[0] + hx
        min_z, max_z = pos[2] - hz, pos[2] + hz
    return cells_for_aabb(min_x, min_z, max_x, max_z, cell_size)


def _thing_uuid(thing) -> str:
    props = getattr(thing, "properties", None)
    if isinstance(props, dict):
        return str(props.get("id", ""))
    return ""


class CellSource:
    """A source of pristine per-cell base data (the streaming "disk").

    A source owns the *base* world, partitioned into cells, plus the entities
    that never stream (persistent globals). It never holds the live, mutated
    objects — every :meth:`load_cell` hands back a *fresh copy* of a cell's base,
    exactly as a disk read would, so freeing and reloading a cell yields the
    original state (onto which the saved delta is then overlaid).
    """

    cell_size: float = CELL_SIZE
    persistent_things: List = []

    def cell_keys(self) -> Set[CellCoord]:
        raise NotImplementedError

    def load_cell(self, coord: CellCoord) -> Tuple[List[dict], List]:
        """Fresh copies of ``(brushes, things)`` whose footprint is in *coord*."""
        raise NotImplementedError

    def world_identity(self, map_name: str = "") -> dict:
        """Stable identity of the whole base world (independent of what's loaded).

        A fingerprint over every base UUID across all cells, plus counts and the
        map name — computable by the source because it owns the base data, and
        used to fail a load safely against the wrong world.
        """
        raise NotImplementedError


class MemoryCellSource(CellSource):
    """A cell source backed by an in-memory pristine partition of a whole world.

    This is a real, usable source — it simulates disk faithfully by deep-copying
    the world once as the "disk image" and returning fresh deep copies on every
    :meth:`load_cell`. It lets disk streaming (with genuine freeing of unloaded
    cells) run over any ordinary loaded map without a new on-disk format: the
    only retained copy of an unloaded cell is the source's pristine partition.
    """

    def __init__(self, brushes, things, cell_size: float = CELL_SIZE,
                 persistent_types=None):
        self.cell_size = float(cell_size)
        self._ptypes = {_normalise_type(t)
                        for t in (persistent_types or DEFAULT_PERSISTENT_TYPES)}
        # cell -> {"brushes": [uuid...], "things": [uuid...]}
        self._cell_members: Dict[CellCoord, Dict[str, List[str]]] = {}
        self._brush_base: Dict[str, dict] = {}
        self._thing_base: Dict[str, object] = {}
        self.persistent_things: List = []
        self._index(brushes or [], things or [])

    # -- build ----------------------------------------------------------
    def _is_persistent(self, thing) -> bool:
        props = getattr(thing, "properties", {}) or {}
        if props.get("bw_persistent"):
            return True
        return _normalise_type(props.get("type")) in self._ptypes

    def _index(self, brushes, things) -> None:
        for brush in brushes:
            if not isinstance(brush, dict):
                continue
            uuid = str(brush.get("id", ""))
            if not uuid:
                continue
            self._brush_base[uuid] = copy.deepcopy(brush)
            for coord in _brush_cells(brush, self.cell_size):
                self._cell_members.setdefault(coord, {"brushes": [], "things": []})["brushes"].append(uuid)
        for thing in things:
            uuid = _thing_uuid(thing)
            if self._is_persistent(thing) or getattr(thing, "pos", None) is None:
                # Persistent globals (and position-less entities) never stream —
                # the session keeps them resident for the whole play session.
                self.persistent_things.append(thing)
                continue
            if not uuid:
                continue
            self._thing_base[uuid] = thing
            pos = thing.pos
            coord = cell_of_point(pos[0], pos[2], self.cell_size)
            self._cell_members.setdefault(coord, {"brushes": [], "things": []})["things"].append(uuid)

    @classmethod
    def from_logic(cls, logic, cell_size: float = CELL_SIZE, persistent_types=None):
        """Build a source from a live logic's currently-loaded world.

        Deep-copies the world as the pristine "disk image", so the session can
        then clear the live scene and stream cells in/out, genuinely freeing the
        objects of unloaded cells (the only retained copy is here).
        """
        return cls(list(getattr(logic, "brushes", None) or []),
                   list(getattr(logic, "things", None) or []),
                   cell_size=cell_size, persistent_types=persistent_types)

    # -- query ----------------------------------------------------------
    def cell_keys(self) -> Set[CellCoord]:
        return set(self._cell_members.keys())

    def load_cell(self, coord: CellCoord) -> Tuple[List[dict], List]:
        members = self._cell_members.get(tuple(coord))
        if not members:
            return ([], [])
        brushes = [copy.deepcopy(self._brush_base[u]) for u in members["brushes"]
                   if u in self._brush_base]
        things = [copy.deepcopy(self._thing_base[u]) for u in members["things"]
                  if u in self._thing_base]
        return (brushes, things)

    def world_identity(self, map_name: str = "") -> dict:
        thing_ids = sorted(self._thing_base.keys())
        brush_ids = sorted(self._brush_base.keys())
        raw = ("\n".join(thing_ids) + "\x00" + "\n".join(brush_ids)).encode("utf-8")
        import hashlib
        return {
            "name": os.path.basename(map_name or ""),
            "fingerprint": hashlib.sha1(raw).hexdigest()[:16],
            "thing_count": len(thing_ids),
            "brush_count": len(brush_ids),
            "world_mode": "bigworld",
        }


class DirectoryCellSource(MemoryCellSource):
    """A cell source that reads a world's cells from a directory of JSON files.

    Layout: a ``world.json`` manifest is optional; each ``cell_<cx>_<cz>.json``
    holds ``{"brushes": [...], "things": [...]}`` for that cell (things as plain
    map dicts). Provided for real on-disk worlds; it loads the whole set once
    into the in-memory partition (so it inherits every :class:`MemoryCellSource`
    behaviour). A truly lazy, per-file streaming variant would override
    :meth:`load_cell` to read one file on demand — the interface is identical.
    """

    def __init__(self, directory: str, thing_from_dict=None,
                 cell_size: float = CELL_SIZE, persistent_types=None):
        import glob
        import json
        import re
        brushes: List[dict] = []
        things: List = []
        pat = re.compile(r"cell_(-?\d+)_(-?\d+)\.json$")
        for path in sorted(glob.glob(os.path.join(directory, "cell_*.json"))):
            if not pat.search(os.path.basename(path)):
                continue
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for b in data.get("brushes", []) or []:
                brushes.append(b)
            for t in data.get("things", []) or []:
                things.append(thing_from_dict(t) if thing_from_dict else t)
        super().__init__(brushes, things, cell_size=cell_size,
                         persistent_types=persistent_types)


# ---------------------------------------------------------------------------
# Loaded-cell bookkeeping
# ---------------------------------------------------------------------------

class _LoadedCell:
    __slots__ = ("coord", "objs")

    def __init__(self, coord: CellCoord):
        self.coord = coord
        #: (kind, uuid) pairs this cell instantiated, for ref-counting.
        self.objs: List[Tuple[str, str]] = []


# ---------------------------------------------------------------------------
# The disk-streaming session
# ---------------------------------------------------------------------------

class DiskStreamingSession:
    """Streams cells in/out of the live scene, freeing unloaded cells' objects.

    Method names line up with :class:`~plugins.bigworld.runtime.BigWorldSession`
    (``streaming``, ``commit_all``, ``serialize_registry``, ``base_identity``) so
    the engine's existing Big World save branch drives it unchanged; loading goes
    through :meth:`restore_saved` (the world can't be overlaid wholesale because
    most of it isn't resident).
    """

    #: Duck-typing flag the engine/save code checks to route to the disk path.
    is_disk_streaming = True

    def __init__(self, logic, source: CellSource,
                 load_radius: float = DEFAULT_ACTIVATION_RADIUS,
                 evict_radius: float = DEFAULT_DEACTIVATION_RADIUS):
        self.logic = logic
        self.source = source
        self.cell_size = float(getattr(source, "cell_size", CELL_SIZE))
        self.load_radius = float(load_radius)
        self.evict_radius = max(float(evict_radius), self.load_radius)
        self.streaming = True
        self._started = False

        self._loaded: Dict[CellCoord, _LoadedCell] = {}
        self._live_by_id: Dict[str, object] = {}
        self._load_ref: Counter = Counter()
        self._last_player_cell: Optional[CellCoord] = None

        # Persistent registry, kept per UUID for correctness and bucketed by cell
        # only when serialized. _base_by_uuid holds the pristine base of currently
        # *streamable* uuids (captured on stream-in, dropped on free and
        # re-captured on reload — so it stays proportional to loaded cells).
        self._base_by_uuid: Dict[str, Tuple[str, dict]] = {}
        self._delta_by_uuid: Dict[str, Tuple[str, dict]] = {}
        self._cell_of_uuid: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _serialize(kind: str, obj) -> dict:
        if kind == _BRUSH:
            return dict(obj) if isinstance(obj, dict) else {}
        to_dict = getattr(obj, "to_dict", None)
        if callable(to_dict):
            try:
                return to_dict()
            except Exception:
                return {}
        return {}

    def _norm(self, kind: str, rec: dict) -> dict:
        key = "brushes" if kind == _BRUSH else "things"
        lvl = normalize_streaming_state({key: [rec]})
        items = lvl.get(key) or [{}]
        return items[0]

    @staticmethod
    def _rec_uuid(kind: str, rec: dict) -> str:
        if kind == _BRUSH:
            return str(rec.get("id", ""))
        return str((rec.get("properties") or {}).get("id", ""))

    @staticmethod
    def _rec_pos(rec: dict):
        return rec.get("pos", (0.0, 0.0, 0.0))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, player_pos=None) -> None:
        """Begin streaming: keep persistent globals resident, empty the rest of
        the scene, and stream in only the cells around the player."""
        persistent = list(getattr(self.source, "persistent_things", []) or [])
        # The live scene now holds *only* globals; cells fill it as they stream.
        try:
            self.logic.things[:] = list(persistent)
            self.logic.brushes[:] = []
        except Exception:
            self.logic.things = list(persistent)
            self.logic.brushes = []
        pos = player_pos if player_pos is not None else self._player_pos()
        if pos is not None:
            self._restream(pos, force=True)
        self._started = True

    def stop(self) -> None:
        self._loaded.clear()
        self._live_by_id.clear()
        self._load_ref.clear()
        self._base_by_uuid.clear()
        self._started = False

    def _reset_streaming(self) -> None:
        """Drop all streamed cells from the scene *without* committing.

        Used when loading a save into an already-running session: the current
        streamed state is being discarded in favour of the save, so it must not
        be committed into the registry. Persistent globals are left resident.
        """
        for uuid, obj in list(self._live_by_id.items()):
            target = self.logic.brushes if isinstance(obj, dict) else self.logic.things
            try:
                target.remove(obj)
            except (ValueError, Exception):
                pass
        self._loaded.clear()
        self._live_by_id.clear()
        self._load_ref.clear()
        self._base_by_uuid.clear()
        self._last_player_cell = None

    def tick(self, player_pos=None) -> bool:
        if not self.streaming or not self._started:
            return False
        pos = player_pos if player_pos is not None else self._player_pos()
        if pos is None:
            return False
        return self._restream(pos, force=False)

    def _restream(self, pos, force: bool) -> bool:
        px, pz = float(pos[0]), float(pos[2])
        player_cell = cell_of_point(px, pz, self.cell_size)
        if not force and player_cell == self._last_player_cell:
            return False
        self._last_player_cell = player_cell

        desired = self._desired_loaded(px, pz)
        to_out = set(self._loaded.keys()) - desired
        to_in = desired - set(self._loaded.keys())
        if not to_in and not to_out:
            return False
        # Unload (commit-then-free) first, then load the new cells in.
        for coord in sorted(to_out):
            self.stream_out_cell(coord)
        for coord in sorted(to_in):
            self.stream_in_cell(coord)
        return True

    def _desired_loaded(self, px: float, pz: float) -> Set[CellCoord]:
        load2 = self.load_radius * self.load_radius
        evict2 = self.evict_radius * self.evict_radius
        reach = int(math.ceil(self.evict_radius / self.cell_size)) + 1
        base = cell_of_point(px, pz, self.cell_size)
        keys = self.source.cell_keys()
        out: Set[CellCoord] = set()
        for dx in range(-reach, reach + 1):
            for dz in range(-reach, reach + 1):
                coord = (base[0] + dx, base[1] + dz)
                if coord not in keys:
                    continue
                d2 = cell_distance_sq(coord[0], coord[1], px, pz, self.cell_size)
                if coord in self._loaded:
                    if d2 <= evict2:
                        out.add(coord)
                elif d2 <= load2:
                    out.add(coord)
        return out

    # ------------------------------------------------------------------
    # Stream in / out
    # ------------------------------------------------------------------
    def stream_in_cell(self, coord: CellCoord) -> None:
        """Instantiate a cell's base from the source and overlay its saved delta.

        The pristine base of any UUID new to memory is captured here — this is
        the "capture each cell's base as it first streams in" step that replaces
        the whole-world snapshot the in-RAM path could take up-front.
        """
        coord = (int(coord[0]), int(coord[1]))
        if coord in self._loaded:
            return
        brushes, things = self.source.load_cell(coord)
        lc = _LoadedCell(coord)

        for kind, obj in ([(_BRUSH, b) for b in brushes] +
                          [(_THING, t) for t in things]):
            uuid = self._rec_uuid(kind, obj) if kind == _BRUSH else _thing_uuid(obj)
            if not uuid:
                continue
            lc.objs.append((kind, uuid))
            # Capture pristine base the first time this UUID is resident.
            if uuid not in self._base_by_uuid:
                base_rec = self._norm(kind, self._serialize(kind, obj))
                self._base_by_uuid[uuid] = (kind, base_rec)
                self._cell_of_uuid.setdefault(
                    uuid, cell_key_for_pos(_obj_pos(obj), self.cell_size))
            if uuid in self._live_by_id:
                self._load_ref[uuid] += 1           # shared across spanning cells
            else:
                self._live_by_id[uuid] = obj
                self._load_ref[uuid] = 1
                target = self.logic.brushes if kind == _BRUSH else self.logic.things
                try:
                    target.append(obj)
                except Exception:
                    pass
        self._loaded[coord] = lc
        self._apply_saved_delta(lc)

    def stream_out_cell(self, coord: CellCoord) -> None:
        """Commit a cell's changes to the registry, then FREE its objects.

        Commit strictly precedes freeing, so a cell's persistent gameplay changes
        are in the world-level registry before the cell leaves memory. A UUID
        shared by another still-loaded cell is kept (ref-counted); only when its
        last loaded cell leaves is it removed from the scene and dropped.
        """
        coord = (int(coord[0]), int(coord[1]))
        lc = self._loaded.pop(coord, None)
        if lc is None:
            return
        self._commit_loaded_cell(lc)               # persist BEFORE freeing
        for kind, uuid in lc.objs:
            c = self._load_ref.get(uuid, 0)
            if c <= 1:
                obj = self._live_by_id.pop(uuid, None)
                self._load_ref.pop(uuid, None)
                if obj is not None:
                    target = self.logic.brushes if kind == _BRUSH else self.logic.things
                    try:
                        target.remove(obj)
                    except (ValueError, Exception):
                        pass
                # Base is re-captured from the pristine source on reload, so it
                # need not be retained for a freed UUID — unless it still carries
                # a delta we must bucket at save time.
                self._base_by_uuid.pop(uuid, None)
                if uuid not in self._delta_by_uuid:
                    self._cell_of_uuid.pop(uuid, None)
            else:
                self._load_ref[uuid] = c - 1

    def _apply_saved_delta(self, lc: _LoadedCell) -> None:
        """Overlay any saved delta for this cell's UUIDs onto the fresh objects."""
        for kind, uuid in lc.objs:
            entry = self._delta_by_uuid.get(uuid)
            if entry is None:
                continue
            _, rec = entry
            obj = self._live_by_id.get(uuid)
            if obj is None:
                continue
            if kind == _BRUSH:
                self._apply_brush_rec(obj, rec)
            else:
                self._apply_thing_rec(obj, rec)

    @staticmethod
    def _apply_thing_rec(thing, rec: dict) -> None:
        try:
            if rec.get("pos") is not None:
                thing.pos = list(rec["pos"])
            for k, v in (rec.get("properties") or {}).items():
                if k == "_io_connections":
                    continue
                thing.properties[k] = v
        except Exception:
            pass

    def _apply_brush_rec(self, brush: dict, rec: dict) -> None:
        from engine.savegame import _BRUSH_OVERLAY_KEYS
        for k in _BRUSH_OVERLAY_KEYS:
            if k in rec:
                brush[k] = rec[k]
            else:
                brush.pop(k, None)

    # ------------------------------------------------------------------
    # Commit / registry
    # ------------------------------------------------------------------
    def _commit_loaded_cell(self, lc: _LoadedCell) -> None:
        """Recompute a loaded cell's delta (live − base) and update the registry.

        Per-UUID (not whole-key) so a spanning object bucketed to another cell
        never clobbers that cell's other records, and so the registry converges:
        a UUID that has returned to its base drops out.
        """
        from engine import savegame
        base_level = {"things": [], "brushes": []}
        live_level = {"things": [], "brushes": []}
        cell_uuids = {u for _, u in lc.objs}
        pos_by_uuid: Dict[str, object] = {}
        for kind, uuid in lc.objs:
            base = self._base_by_uuid.get(uuid)
            live_obj = self._live_by_id.get(uuid)
            if base is None or live_obj is None:
                continue
            _, base_rec = base
            live_rec = self._norm(kind, self._serialize(kind, live_obj))
            key = "brushes" if kind == _BRUSH else "things"
            base_level[key].append(base_rec)
            live_level[key].append(live_rec)
            pos_by_uuid[uuid] = self._rec_pos(live_rec)

        delta = savegame.compute_delta_level(base_level, live_level)
        changed: Set[str] = set()
        for t in delta.get("things", []):
            uuid = self._rec_uuid(_THING, t)
            self._delta_by_uuid[uuid] = (_THING, t)
            self._cell_of_uuid[uuid] = cell_key_for_pos(
                pos_by_uuid.get(uuid, self._rec_pos(t)), self.cell_size)
            changed.add(uuid)
        for b in delta.get("brushes", []):
            uuid = self._rec_uuid(_BRUSH, b)
            self._delta_by_uuid[uuid] = (_BRUSH, b)
            self._cell_of_uuid[uuid] = cell_key_for_pos(
                pos_by_uuid.get(uuid, (0.0, 0.0, 0.0)), self.cell_size)
            changed.add(uuid)
        # Convergence: a UUID in this cell that is no longer changed drops out.
        for uuid in cell_uuids:
            if uuid not in changed and uuid in self._delta_by_uuid:
                self._delta_by_uuid.pop(uuid, None)

    def commit_all(self) -> dict:
        """Commit every currently loaded cell; unloaded cells are already in the
        registry (they were committed before being freed). Returns the registry."""
        for lc in list(self._loaded.values()):
            self._commit_loaded_cell(lc)
        return self.serialize_registry()

    def serialize_registry(self) -> dict:
        """Bucket the per-UUID registry into the on-disk per-cell shape."""
        registry: Dict[str, dict] = {}
        for uuid, (kind, rec) in self._delta_by_uuid.items():
            key = self._cell_of_uuid.get(uuid, "0,0")
            bucket = registry.setdefault(key, {"things": [], "brushes": []})
            bucket["brushes" if kind == _BRUSH else "things"].append(rec)
        return registry

    def load_registry(self, cell_deltas: dict) -> None:
        """Adopt a saved per-cell registry into the per-UUID store (inverse of
        :meth:`serialize_registry`)."""
        self._delta_by_uuid.clear()
        self._cell_of_uuid.clear()
        for key, entry in (cell_deltas or {}).items():
            for t in entry.get("things", []) or []:
                uuid = self._rec_uuid(_THING, t)
                if uuid:
                    self._delta_by_uuid[uuid] = (_THING, t)
                    self._cell_of_uuid[uuid] = key
            for b in entry.get("brushes", []) or []:
                uuid = self._rec_uuid(_BRUSH, b)
                if uuid:
                    self._delta_by_uuid[uuid] = (_BRUSH, b)
                    self._cell_of_uuid[uuid] = key

    def registry_stats(self) -> dict:
        reg = self.serialize_registry()
        return {
            "changed_cells": len(reg),
            "changed_things": sum(len(c["things"]) for c in reg.values()),
            "changed_brushes": sum(len(c["brushes"]) for c in reg.values()),
            "loaded_cells": len(self._loaded),
            "live_objects": len(self._live_by_id),
        }

    # ------------------------------------------------------------------
    # Save / load integration (aligned with the engine's Big World branch)
    # ------------------------------------------------------------------
    def base_identity(self, map_name: str = "") -> dict:
        return self.source.world_identity(map_name)

    def restore_saved(self, data: dict, current_map_name: str = "") -> dict:
        """Load a Big World delta save into a disk-streaming session.

        Validates the base world against the source, adopts the saved registry,
        restores player/runtime, then streams the cells around the player — each
        streamed cell instantiates its base and re-applies its saved delta.
        """
        from engine import savegame
        saved = (data.get("base_map") or {})
        current = self.source.world_identity(current_map_name)
        if saved.get("fingerprint") and current.get("fingerprint") \
                and saved["fingerprint"] != current["fingerprint"] \
                and saved.get("name") and saved.get("name") != current.get("name"):
            raise ValueError(
                "this Big World save was made on a different world "
                f"('{saved.get('name', '?')}'); load that world first")

        self.load_registry(data.get("cell_deltas", {}) or {})

        # Seed the player position from the save so we stream the right cells,
        # then restore full player/runtime state once the cells are resident.
        player = data.get("player") or {}
        seed = player.get("pos")
        if not self._started:
            # A fresh session: start() streams the local cells, and because the
            # registry is already loaded each cell applies its saved delta.
            self.start(player_pos=seed)
        else:
            # An already-running session (e.g. the plugin started it at play): the
            # cells it streamed did so before the save's registry was known, so
            # discard that streamed state (without committing) and re-stream at the
            # saved position, now applying the saved deltas.
            self._reset_streaming()
            self._restream(seed if seed is not None else self._player_pos(),
                           force=True)

        savegame._restore_runtime_and_players(self.logic, data)
        warning = ""
        if saved.get("fingerprint") and current.get("fingerprint") \
                and saved["fingerprint"] != current["fingerprint"]:
            warning = ("base world changed since this save; applied by UUID, "
                       "missing entities skipped")
        return {"mode": "delta", "world_mode": "bigworld", "warning": warning}

    # ------------------------------------------------------------------
    def _player_pos(self):
        player = getattr(self.logic, "player", None)
        if player is None:
            return None
        return getattr(player, "pos", None)

    def player_cell(self):
        pos = self._player_pos()
        if pos is None:
            return self._last_player_cell
        return cell_of_point(float(pos[0]), float(pos[2]), self.cell_size)
