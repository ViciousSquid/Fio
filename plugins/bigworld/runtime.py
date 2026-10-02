"""
Runtime glue for Big World: :class:`BigWorldSession`.

The :class:`~plugins.bigworld.manager.BigWorldManager` is pure bookkeeping — it
knows which cells and objects are active but touches no engine state. The
session is the thin layer that *applies* that knowledge to a live Fio play
session, and it does so by cooperating with machinery Fio already has rather
than by rewriting any subsystem:

* **Rendering.** An inactive brush is flagged ``hidden``; Fio's per-frame cull
  already drops hidden brushes before they reach the draw path
  (``not b.get('hidden')`` in ``LogicThread._prepare_render_state``), so only
  active-cell geometry is ever submitted to the renderer — no renderer change.
* **Physics.** The player already collides through
  ``SpatialGrid.get_potential_colliders``, which only ever returns brushes in
  the player's *local* cells — cells far outside the activation radius are never
  queried, so inactive static geometry costs nothing. The session additionally
  parks inactive dynamic brushes (movers/doors) so they don't animate off-screen.
* **Entities.** An inactive entity is flagged ``disabled`` (and ``hidden``),
  which Fio's monster AI and pickup handlers already treat as "skip me" — so an
  NPC or pickup in a distant cell drops out of per-frame processing. Persistent
  globals are never touched.
* **Lights.** An inactive light is hidden and switched off, keeping the count of
  lights the renderer considers local to the active cells.

Every change the session makes is *reversible and tracked*: it records what it
altered on each object and restores the exact prior value when the object
re-activates or when play stops. Nothing is duplicated, no UUID is touched, and
the editor world is returned untouched — so editing, saving and reloading are
unaffected (see the README's Persistence / Editing sections).

Plain Python throughout: ``player.pos`` may be a ``glm.vec3`` (editor) or a
list/tuple (player build); both are read by index.
"""

from __future__ import annotations

from typing import Optional

from .cell import cell_of_point
from .manager import (BigWorldManager, DEFAULT_ACTIVATION_RADIUS,
                      DEFAULT_DEACTIVATION_RADIUS)
from .persistence import (build_cell_delta_registry, flatten_cell_delta_registry,
                          normalize_streaming_state)

# Marker keys the session writes onto objects it parks, so it can restore the
# exact prior value and never clobber a user's own hidden/disabled state.
_HID_MARK = "_bw_parked_hidden"      # present ⇒ BW set `hidden`; value = prior
_DIS_MARK = "_bw_parked_disabled"    # present ⇒ BW set `disabled`; value = prior


def _xz(pos):
    """(x, z) from a glm.vec3 / list / tuple position."""
    return float(pos[0]), float(pos[2])


class BigWorldSession:
    """Per-play-session controller that streams cells as the player moves."""

    #: World half-extent (units) used when terrain is streamed "forever". Large
    #: enough that a player never reaches its edge in normal play, small enough
    #: to stay well inside float precision. Only the ring of chunks around the
    #: camera is ever resident, so the size costs nothing.
    INFINITE_HALF_EXTENT = 1.0e7

    def __init__(self, logic, activation_radius: float = DEFAULT_ACTIVATION_RADIUS,
                 deactivation_radius: float = DEFAULT_DEACTIVATION_RADIUS,
                 terrain_fill: bool = False,
                 terrain_infinite: bool = False,
                 terrain_stream_radius: float = 0.0):
        self.logic = logic
        self.manager = BigWorldManager(
            activation_radius=activation_radius,
            deactivation_radius=deactivation_radius,
        )
        #: When False the session is inert — the full world stays active exactly
        #: as vanilla Fio (used for ordinary small maps / editor preview off).
        self.streaming = True
        #: When True and the map has a procedural terrain, expand the terrain to
        #: cover every cell of the world and stream its chunks around the player.
        self.terrain_fill = bool(terrain_fill)
        #: When True (with terrain_fill), stream terrain forever around the player
        #: rather than stopping at the world's content bounds — no map edge.
        self.terrain_infinite = bool(terrain_infinite)
        #: World units of terrain kept resident around the player. 0 ⇒ derive it
        #: from the activation radius when the session starts.
        self.terrain_stream_radius = float(terrain_stream_radius)
        self._started = False
        # Prior terrain config captured on start(), restored verbatim on stop()
        # so the editor/authored terrain is returned exactly as it was.
        self._terrain = None
        self._terrain_saved = None
        # Objects the session has parked (inactive), keyed by id() so play-stop
        # can restore every one even if the player quit far from the start (brush
        # dicts are unhashable, so a set won't do — an id→object map dedups and
        # keeps the live reference).
        self._parked_brushes = {}
        self._parked_things = {}
        self._parked_lights = {}

        # ---- persistent cell delta registry -----------------------------
        # The world-level record of gameplay changes, bucketed by cell id and
        # independent of which cells are currently streamed in. Survives cell
        # unload: a cell's changes are committed here before it is parked, and
        # stay here for the save even once the cell is inactive.
        self.registry: dict = {}
        #: The pristine world serialized at start() (pre-gameplay, pre-parking),
        #: the base each cell's delta is measured against.
        self._base_level: Optional[dict] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, player_pos=None) -> None:
        """Index the world and stream in the region around the player.

        The one-off O(total objects) cost lives here (index + park-everything),
        not in the per-frame path. After this, everything is inactive except the
        cells inside the activation radius of ``player_pos``.
        """
        brushes = list(getattr(self.logic, "brushes", None) or [])
        things = list(getattr(self.logic, "things", None) or [])
        self.manager.index_world(brushes, things)

        # Snapshot the pristine world *before* parking/gameplay mutates anything —
        # this is the base every cell delta is measured against.
        self._capture_base()

        # Fit the procedural terrain to the world and switch it to streaming so
        # it covers every cell without tessellating the whole grid up-front.
        # Independent of brush streaming: a map may fill terrain while leaving
        # `streaming` behaviour for brushes/entities as usual.
        self._setup_terrain()

        if not self.streaming:
            self._started = True
            return

        pos = player_pos if player_pos is not None else self._player_pos()
        # Baseline: park the whole world, then activate only the local region.
        # Parking is a single startup pass; the per-frame path never does this.
        for brush in brushes:
            self._set_brush_active(brush, False)
        for thing in things:
            if self.manager._is_persistent(thing):
                thing.properties["bw_active"] = True
                continue
            if self.manager._is_light(thing):
                self._set_light_active(thing, False)
            else:
                self._set_thing_active(thing, False)

        if pos is not None:
            self.manager.update(pos, force=True)
            self._apply_active_snapshot()
        self._started = True

    def stop(self) -> None:
        """Restore every object the session parked; leave the world untouched.

        Guarantees the editor/runtime world is exactly as it was before play —
        no lingering hidden/disabled flags, no lost data, no changed UUIDs.
        """
        for brush in list(self._parked_brushes.values()):
            self._restore_brush(brush)
        for thing in list(self._parked_things.values()):
            self._restore_thing(thing)
        for light in list(self._parked_lights.values()):
            self._restore_thing(light)
        self._parked_brushes.clear()
        self._parked_things.clear()
        self._parked_lights.clear()
        self._restore_terrain()
        self._started = False

    def tick(self, player_pos=None) -> bool:
        """Per-frame entry point. Returns True if the active set changed.

        The hot path: a single cell-of-point compare inside ``manager.update``
        early-outs until the player crosses a cell boundary, so a stationary or
        slow-moving player pays almost nothing. On a crossing, only the objects
        that entered/left the region are toggled.
        """
        if not self.streaming or not self._started:
            return False
        pos = player_pos if player_pos is not None else self._player_pos()
        if pos is None:
            return False
        delta = self.manager.update(pos)
        if not delta.changed:
            return False
        # Commit the persistent state of every cell that is about to leave the
        # active set into the registry *before* it is parked, so its changes
        # outlive the unload (the core invariant of a Big World save).
        for coord in delta.leaving_cells:
            self.commit_cell(coord)
        for brush in delta.brushes_leaving:
            self._set_brush_active(brush, False)
        for brush in delta.brushes_entering:
            self._set_brush_active(brush, True)
        for thing in delta.things_leaving:
            self._set_thing_active(thing, False)
        for thing in delta.things_entering:
            self._set_thing_active(thing, True)
        for light in delta.lights_leaving:
            self._set_light_active(light, False)
        for light in delta.lights_entering:
            self._set_light_active(light, True)
        return True

    def _apply_active_snapshot(self) -> None:
        """Turn on everything the manager currently marks active (post-start)."""
        for brush in self.manager.active_brushes():
            self._set_brush_active(brush, True)
        for thing in self.manager.active_things():
            self._set_thing_active(thing, True)
        for light in self.manager.active_lights():
            self._set_light_active(light, True)

    # ------------------------------------------------------------------
    # Terrain fill / streaming
    # ------------------------------------------------------------------

    def _setup_terrain(self) -> None:
        """Expand the map's terrain to cover the world and turn on streaming.

        Guarded and fully reversible: if the map has no terrain, or terrain
        fill was not requested, this does nothing. When it does act it snapshots
        the terrain's prior bounds/streaming config so :meth:`_restore_terrain`
        can put it back exactly on play-stop. It never enables or disables the
        terrain itself (a map that authored terrain off stays off), and it makes
        no OpenGL call — the bounds change defers its chunk prune to the render
        thread via ``prune=False``.
        """
        if not self.terrain_fill:
            return
        terrain = getattr(self.logic, "terrain", None)
        if terrain is None:
            return
        # Only cooperate with a terrain that exposes the streaming surface added
        # to engine.terrain.Terrain; fail safe on anything else.
        if not (hasattr(terrain, "set_streaming")
                and hasattr(terrain, "set_world_extent")):
            return

        self._terrain = terrain
        self._terrain_saved = {
            "streaming": getattr(terrain, "streaming", False),
            "stream_radius": getattr(terrain, "stream_radius", 1536.0),
            "stream_evict_padding": getattr(terrain, "stream_evict_padding", 512.0),
            "min_chunk_x": terrain.min_chunk_x,
            "max_chunk_x": terrain.max_chunk_x,
            "min_chunk_z": terrain.min_chunk_z,
            "max_chunk_z": terrain.max_chunk_z,
        }

        extent = self._world_extent()
        if extent is not None:
            min_wx, min_wz, max_wx, max_wz = extent
            terrain.set_world_extent(min_wx, min_wz, max_wx, max_wz, prune=False)

        radius = self.terrain_stream_radius
        if radius <= 0.0:
            # Keep roughly a cell-radius of terrain resident around the player,
            # matched to how far brush/entity cells activate.
            radius = self.manager.activation_radius
        terrain.set_streaming(True, radius)

    def _restore_terrain(self) -> None:
        """Undo :meth:`_setup_terrain`, returning the terrain to its authored state."""
        terrain = self._terrain
        saved = self._terrain_saved
        self._terrain = None
        self._terrain_saved = None
        if terrain is None or saved is None:
            return
        try:
            # Restore bounds first (deferred prune — GL delete happens on the
            # render thread), then the streaming flags.
            terrain.set_bounds(saved["min_chunk_x"], saved["max_chunk_x"],
                               saved["min_chunk_z"], saved["max_chunk_z"],
                               prune=False)
            terrain.stream_evict_padding = saved["stream_evict_padding"]
            terrain.set_streaming(saved["streaming"], saved["stream_radius"])
        except Exception:
            pass  # never let terrain teardown break play-stop

    def _world_extent(self):
        """World-space XZ rectangle to size the streamed terrain to, or None.

        With ``terrain_infinite`` this is a huge rectangle centred on the origin
        so the terrain streams forever (there is no edge to walk off). Otherwise
        it is ``(min_x, min_z, max_x, max_z)`` — the bounding box of all cells
        the manager grouped objects into, i.e. "every available cell" of the
        streamed world. Returns None when the world has no positioned objects and
        streaming is bounded (nothing to size against — leave terrain authored).
        """
        if self.terrain_infinite:
            h = self.INFINITE_HALF_EXTENT
            return (-h, -h, h, h)
        cells = getattr(self.manager, "cells", None)
        if not cells:
            return None
        cs = self.manager.cell_size
        xs = [c[0] for c in cells]
        zs = [c[1] for c in cells]
        min_cx, max_cx = min(xs), max(xs)
        min_cz, max_cz = min(zs), max(zs)
        return (min_cx * cs, min_cz * cs,
                (max_cx + 1) * cs, (max_cz + 1) * cs)

    # ------------------------------------------------------------------
    # Effect application (reversible, tracked)
    # ------------------------------------------------------------------

    def _set_brush_active(self, brush: dict, active: bool) -> None:
        if active:
            self._restore_brush(brush)
            self._parked_brushes.pop(id(brush), None)
        else:
            if _HID_MARK not in brush:
                brush[_HID_MARK] = brush.get("hidden", False)
            brush["hidden"] = True
            brush["bw_active"] = False
            self._parked_brushes[id(brush)] = brush

    def _restore_brush(self, brush: dict) -> None:
        if _HID_MARK in brush:
            brush["hidden"] = brush.pop(_HID_MARK)
        brush["bw_active"] = True

    def _set_thing_active(self, thing, active: bool) -> None:
        props = getattr(thing, "properties", None)
        if not isinstance(props, dict):
            return
        if active:
            self._restore_thing(thing)
            self._parked_things.pop(id(thing), None)
        else:
            if _HID_MARK not in props:
                props[_HID_MARK] = props.get("hidden", False)
            if _DIS_MARK not in props:
                props[_DIS_MARK] = props.get("disabled", False)
            props["hidden"] = True
            props["disabled"] = True
            props["bw_active"] = False
            self._parked_things[id(thing)] = thing

    def _set_light_active(self, light, active: bool) -> None:
        props = getattr(light, "properties", None)
        if not isinstance(props, dict):
            return
        if active:
            self._restore_thing(light)
            self._parked_lights.pop(id(light), None)
        else:
            if _HID_MARK not in props:
                props[_HID_MARK] = props.get("hidden", False)
            props["hidden"] = True
            props["bw_active"] = False
            self._parked_lights[id(light)] = light

    def _restore_thing(self, thing) -> None:
        props = getattr(thing, "properties", None)
        if not isinstance(props, dict):
            return
        if _HID_MARK in props:
            props["hidden"] = props.pop(_HID_MARK)
        if _DIS_MARK in props:
            props["disabled"] = props.pop(_DIS_MARK)
        props["bw_active"] = True

    # ------------------------------------------------------------------
    # Persistent cell delta registry
    # ------------------------------------------------------------------

    def _capture_base(self) -> None:
        """Snapshot the pristine world as the base for all cell deltas."""
        self._base_level = normalize_streaming_state(self._live_level())
        self.registry = {}

    def _live_level(self) -> dict:
        """Serialize the whole resident world (all cells, loaded or not).

        Uses the editor state's serializer so the level is in the exact shape the
        core delta code expects. Because streaming never frees objects, this
        includes cells that are currently parked/inactive.
        """
        try:
            return self.logic.editor_state.get_level_data()
        except Exception:
            return {"things": [], "brushes": []}

    def _cell_live_level(self, cell) -> dict:
        """Serialize just one cell's resident objects into a partial level.

        Covers both the cell's entities *and* its lights — the manager files
        lights in a separate ``cell.lights`` list, but a light is just a
        gameplay entity (a switched-off light is a real change), so both are
        serialized as ``things`` and diffed the same way. Without the lights a
        light change would only be captured by the save-time ``commit_all``, not
        by the commit that runs as the cell unloads.
        """
        things = []
        for t in list(getattr(cell, "things", []) or []) + list(getattr(cell, "lights", []) or []):
            to_dict = getattr(t, "to_dict", None)
            if callable(to_dict):
                try:
                    things.append(to_dict())
                except Exception:
                    pass
        brushes = [dict(b) for b in (getattr(cell, "brushes", []) or [])
                   if isinstance(b, dict)]
        return {"things": things, "brushes": brushes}

    def commit_cell(self, coord) -> None:
        """Merge one cell's current persistent delta into the registry.

        Recomputes the cell's changes relative to the base and *replaces* its
        stored entry, so a value that has returned to its base state drops out
        (the registry converges on ``current − base``, it does not accumulate an
        event history). Called before a cell is unloaded and by :meth:`commit_all`.
        """
        if self._base_level is None:
            return
        coord = (int(coord[0]), int(coord[1]))
        cell = self.manager.cells.get(coord)
        key = f"{coord[0]},{coord[1]}"
        self.registry.pop(key, None)
        if cell is None:
            return
        live = normalize_streaming_state(self._cell_live_level(cell))
        sub = build_cell_delta_registry(self._base_level, live, self.manager.cell_size)
        for k, entry in sub.items():
            if entry.get("things") or entry.get("brushes"):
                self.registry[k] = entry

    def commit_all(self) -> dict:
        """Flush every cell's current state into the registry and return it.

        Called at save time. Rebuilds the whole registry from the resident world
        in one authoritative pass, so no pending change is omitted — including
        cells that were modified earlier and have since unloaded (their objects
        are still resident in the in-RAM streaming model). Unchanged cells are
        not emitted.
        """
        if self._base_level is None:
            self._capture_base()
        live = normalize_streaming_state(self._live_level())
        self.registry = build_cell_delta_registry(
            self._base_level, live, self.manager.cell_size)
        return self.registry

    def serialize_registry(self) -> dict:
        """The persistent cell delta registry as a JSON-serialisable dict."""
        return self.registry

    def base_identity(self, map_name: str = "") -> dict:
        """Base-world identity to guard the delta against the wrong world."""
        from engine import savegame
        ident = savegame._base_map_identity(self._base_level or {}, map_name)
        ident["world_mode"] = "bigworld"
        return ident

    def apply_registry(self, registry: dict) -> None:
        """Adopt a saved registry and overlay its changes onto the resident world.

        Stores the registry (so future streaming can re-apply per cell) and, since
        the whole world is resident, overlays every cell's UUID-keyed changes now
        via the core overlay — the same path a normal delta restore uses.
        """
        from engine import savegame
        self.registry = registry or {}
        level = flatten_cell_delta_registry(self.registry)
        savegame._overlay_entities(self.logic, level)

    def registry_stats(self) -> dict:
        """Counts for the debug overlay / tests: changed cells and records."""
        cells = len(self.registry)
        things = sum(len(c.get("things", []) or []) for c in self.registry.values())
        brushes = sum(len(c.get("brushes", []) or []) for c in self.registry.values())
        return {"changed_cells": cells, "changed_things": things,
                "changed_brushes": brushes}

    # ------------------------------------------------------------------
    # Helpers / queries
    # ------------------------------------------------------------------

    def _player_pos(self):
        player = getattr(self.logic, "player", None)
        if player is None:
            return None
        return getattr(player, "pos", None)

    def player_cell(self):
        pos = self._player_pos()
        if pos is None:
            return self.manager._last_player_cell
        x, z = _xz(pos)
        return cell_of_point(x, z, self.manager.cell_size)

    def stats(self) -> dict:
        s = self.manager.stats()
        terrain = self._terrain
        if terrain is not None:
            s["terrain_fill"] = True
            s["terrain_infinite"] = bool(self.terrain_infinite)
            s["terrain_streaming"] = bool(getattr(terrain, "streaming", False))
            s["terrain_chunks"] = int(getattr(terrain, "streamed_chunks", 0))
            s["terrain_stream_radius"] = float(getattr(terrain, "stream_radius", 0.0))
        else:
            s["terrain_fill"] = False
        return s
