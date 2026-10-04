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

import math
from typing import Optional

from engine.change_journal import VISIBILITY, touch

from .cell import cell_of_point
from .manager import (BigWorldManager, DEFAULT_ACTIVATION_RADIUS,
                      DEFAULT_DEACTIVATION_RADIUS)
from .persistence import (build_cell_delta_registry, flatten_cell_delta_registry,
                          normalize_streaming_state)
from .config import bound_view_horizon, effective_streaming_radii

# Marker keys the session writes onto objects it parks, so it can restore the
# exact prior value and never clobber a user's own hidden/disabled state.
#
# They are the engine's names, not this plugin's: anything that builds a durable
# structure out of `hidden` (the collision grid, above all) has to be able to
# tell "the mapper hid this" from "Big World parked it a moment ago", and the
# engine is where that question is answered — see `engine.spatial.authored_hidden`.
from engine.spatial import (PARKED_DISABLED_KEY as _DIS_MARK,
                            PARKED_HIDDEN_KEY as _HID_MARK,
                            SIM_TIER_KEY)
from .tiers import DEFAULT_NEAR_RADIUS, TierClassifier


def _xz(pos):
    """(x, z) from a glm.vec3 / list / tuple position."""
    return float(pos[0]), float(pos[2])


class StreamingHost:
    """What :class:`BigWorldSession` needs of the object it streams into.

    :class:`engine.logic_thread.LogicThread` is the real host and provides a
    superset of this. The class exists so the contract is written down next to
    the code that depends on it, and so a head-less test or a world-generation
    benchmark can stand in for the logic thread without a live play session.

    A host must additionally expose ``editor_state.brushes``, ``editor_state.things`` and ``player`` —
    those come from the scene, so they are the caller's to supply.

    The host contract is explicit: scene and runtime subsystems are owned by the live
    engine host, and the session reads them directly.
    not an enforced interface.
    """

    def __init__(self):
        #: Outer edge of full-fidelity simulation. The session overwrites this
        #: from the map's Big World settings so that whatever the host uses to
        #: decide how hard to simulate agrees with what the streamer keeps
        #: resident (§13).
        self.sim_near_radius = DEFAULT_NEAR_RADIUS
        #: Outer edge of live simulation; tracks the activation radius.
        self.sim_active_radius = 0.0
        #: Bumped whenever the set of drawable objects changes. A host that
        #: caches "every non-hidden brush" between frames — rebuilding it per
        #: frame is O(total brushes), exactly the cost streaming exists to
        #: avoid — invalidates that cache on this.
        self.visibility_changes = 0

    def notify_visibility_changed(self) -> None:
        """Streaming a cell in or out changed which objects are drawable."""
        self.visibility_changes += 1


class BigWorldSession:
    """Per-play-session controller that streams cells as the player moves."""

    #: World half-extent (units) used when terrain is streamed "forever". Large
    #: enough that a player never reaches its edge in normal play, small enough
    #: to stay well inside float precision. Only the ring of chunks around the
    #: camera is ever resident, so the size costs nothing.
    INFINITE_HALF_EXTENT = 1.0e7

    #: While the host's camera is overhead, residency and tiers are fitted to
    #: the ground it shows (see :meth:`_fitted_view`); with any other camera
    #: the authored radii and the view-distance horizon apply unchanged.
    #: Fitted, residency is refreshed whenever the player has moved this far
    #: since the last refresh, and the activation radius carries the same
    #: distance past the screen's farthest corner, so whatever is on screen is
    #: always resident. A fit never makes residency larger than authored: a
    #: screen that reaches further keeps the authored radius and camera reach.
    FIT_REFRESH = 256.0
    #: Tiers are refreshed every this far, and NEAR's box carries the same
    #: distance (plus :attr:`FIT_NEAR_MARGIN`) past the screen's edges.
    FIT_RETIER = 128.0
    FIT_NEAR_MARGIN = 96.0
    #: Fitted radii are rounded up to this, so a resize jitter is not a
    #: residency change.
    FIT_QUANTUM = 64.0

    def __init__(self, logic, activation_radius: float = DEFAULT_ACTIVATION_RADIUS,
                 deactivation_radius: float = DEFAULT_DEACTIVATION_RADIUS,
                 terrain_fill: bool = False,
                 terrain_infinite: bool = False,
                 terrain_stream_radius: float = 0.0,
                 sim_near_radius: float = DEFAULT_NEAR_RADIUS):
        self.logic = logic
        #: The residency currently fitted to the camera, or None when the
        #: authored radii are in force: ``(activation, deactivation, limit)``.
        #: The screen's rectangle is :attr:`TierClassifier.near_rect`.
        self._fit = None
        self._fit_pos = None
        self._tier_pos = None
        self._authored_activation_radius = max(0.0, float(activation_radius))
        self._authored_deactivation_radius = max(
            self._authored_activation_radius, float(deactivation_radius)
        )
        self.manager = BigWorldManager(
            activation_radius=self._authored_activation_radius,
            deactivation_radius=self._authored_deactivation_radius,
        )
        #: Assigns NEAR/ACTIVE/DISTANT/DORMANT to the resident set. Its active
        #: boundary is the manager's activation radius, so "how far out is the
        #: world resident" and "how far out is it simulated" are one number
        #: rather than two that can drift apart (§13).
        self.tiers = TierClassifier(
            near_radius=sim_near_radius,
            active_radius=self.manager.activation_radius,
        )
        #: The map's authored full-fidelity radius. Configuration in, nothing
        #: more: it sizes the tier model's inner band and is published to the
        #: host, and the session neither reads it back nor acts on it.
        self.sim_near_radius = float(sim_near_radius)
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
        #: from the effective activation radius. Keep the authored value
        #: separately so a derived radius can continue following visibility
        #: changes without treating a runtime-derived value as explicit config.
        self._terrain_stream_radius_authored = max(0.0, float(terrain_stream_radius))
        self.terrain_stream_radius = self._terrain_stream_radius_authored
        self._started = False
        #: Last camera horizon applied to residency.
        self._visual_horizon = None
        #: Undoes the camera-horizon limit start() places (see
        #: :func:`~plugins.bigworld.config.bound_view_horizon`).
        self._release_view_horizon = None
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
    # Residency / camera cooperation
    # ------------------------------------------------------------------

    def _current_visual_horizon(self):
        """Return the renderer's useful horizon when the host exposes one."""
        return self.logic.render_runtime.view_distance.visual_horizon

    def _fitted_view(self):
        """``((activation, deactivation, view_limit), near_rect)`` sized from
        the overhead camera's screen, or None to use the authored radii.

        None whenever the camera is not overhead: the host's footprint is
        None for a first-person camera, and a host without one (a head-less
        test, a tool) never fits at all.

        Residency comes from the footprint's ``reach`` -- the distance to the
        screen's farthest ground corner -- so a camera that turns with the
        player does not change it. It is capped at the authored activation
        radius: past that the map's own residency and camera reach stand, as
        they do with no fit, and fog hides the rest. ``near_rect`` is the
        screen's box, which does follow the camera's turn; changing it only
        re-tiers.
        """
        try:
            fp = self.logic.camera.overhead_ground_footprint()
        except Exception:
            fp = None
        if not fp:
            return None
        hx, hz = float(fp[0]), float(fp[1])
        reach = float(fp[2]) if len(fp) > 2 else math.hypot(hx, hz)
        if not all(map(math.isfinite, (hx, hz, reach))):
            return None
        q = self.FIT_QUANTUM

        def up(v):
            return math.ceil(v / q) * q
        margin = self.FIT_RETIER + self.FIT_NEAR_MARGIN
        near_rect = (up(hx + margin), up(hz + margin))
        authored = self._authored_activation_radius
        activation = up(reach + self.FIT_REFRESH)
        height = self._camera_height()
        if activation >= authored:
            # The screen reaches past the map's residency: keep it, and let the
            # camera see down to its edge at the player's ground -- which, with
            # the camera held under the activation radius, always includes the
            # player (see start()).
            residency = (authored, self._authored_deactivation_radius,
                         up(math.hypot(authored, height)))
        else:
            band = max(q, self._authored_deactivation_radius - authored)
            # The camera's reach: from its height down to the residency edge,
            # with room for ground lower than the player. Inside the authored
            # radius, so it is bounded too.
            limit = up(math.hypot(activation + band, height + 400.0))
            residency = (activation, activation + band, limit)
        return residency, near_rect

    def _camera_height(self) -> float:
        """How high the host's overhead camera floats, ceiling included."""
        try:
            return float(self.logic.camera.effective_overhead_height())
        except Exception:
            return float(self.logic.camera.overhead_height or 800.0)

    def _tier_near_radius(self) -> float:
        """The near circle the tiers are configured with: the authored one, or
        the whole resident region while a fitted rectangle decides NEAR."""
        if self._fit is not None:
            return self.manager.activation_radius
        return self.sim_near_radius

    def _sync_visual_horizon(self) -> bool:
        """Keep residency outside the camera's visible/fogged region."""
        fit = self._fitted_view()
        if fit is not None or self._fit is not None:
            return self._sync_fit(fit)
        horizon = self._current_visual_horizon()
        activation, deactivation = effective_streaming_radii(
            self._authored_activation_radius,
            self._authored_deactivation_radius,
            horizon,
        )
        changed = (
            activation != self.manager.activation_radius
            or deactivation != self.manager.deactivation_radius
        )
        self.manager.activation_radius = activation
        self.manager.deactivation_radius = max(deactivation, activation)
        # A zero terrain stream radius is the authored "derive from activation"
        # sentinel. Keep that mode live as the camera horizon changes; an
        # explicit terrain_stream_radius remains authoritative.
        if self._terrain_stream_radius_authored <= 0.0:
            self.terrain_stream_radius = activation
            # _sync_visual_horizon() runs from the tick hot path; only touch
            # the terrain object when the effective boundary actually changed.
            if changed and self._terrain is not None:
                try:
                    self._terrain.set_streaming(True, self.terrain_stream_radius)
                except Exception:
                    pass
        self._visual_horizon = horizon
        if changed:
            self.tiers.set_radii(self._tier_near_radius(), activation)
            self._publish_relevance_radii()
        return changed

    def _sync_fit(self, fit) -> bool:
        """Apply a camera fit (or, with ``fit`` None, go back to the authored
        radii). Returns True when residency changed.

        Residency, the camera's reach and the terrain stream are touched only
        when the residency part of the fit changes; a new screen rectangle
        alone just re-tiers on the next tick and is republished.
        """
        residency, rect = (None, None) if fit is None else fit
        changed = residency != self._fit
        if changed:
            self._fit = residency
            if residency is None:
                activation = self._authored_activation_radius
                deactivation = self._authored_deactivation_radius
                limit = self._authored_activation_radius
                self._fit_pos = self._tier_pos = None
            else:
                activation, deactivation, limit = residency
            self.manager.activation_radius = activation
            self.manager.deactivation_radius = max(deactivation, activation)
            # The camera's far plane follows residency, as bound_view_horizon
            # does for the authored radius.
            if self._release_view_horizon is not None:
                self._release_view_horizon()
            self._release_view_horizon = bound_view_horizon(self.logic, limit)
            # Filled terrain streamed at the derived radius keeps following
            # residency, exactly as it follows the visual horizon.
            if self._terrain_stream_radius_authored <= 0.0:
                self.terrain_stream_radius = activation
                if self._terrain is not None:
                    try:
                        self._terrain.set_streaming(True, activation)
                    except Exception:
                        pass
            self.tiers.set_radii(self._tier_near_radius(), activation)
            self._publish_relevance_radii()
        if changed or rect != self.tiers.near_rect:
            self.tiers.set_near_rect(rect)
            self._tier_pos = None        # re-tier on the next tick
        return changed

    def _set_camera_ceiling(self, height) -> None:
        """Hold the host's overhead camera at or under *height* (None: no
        ceiling). Guarded, like the published radii."""
        try:
            self.logic.camera.overhead_height_limit = height
        except Exception:
            pass

    def _fit_moved(self, pos, last, step) -> bool:
        if last is None:
            return True
        return math.hypot(pos[0] - last[0], pos[1] - last[1]) >= step

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, player_pos=None) -> None:
        """Index the world and stream in the region around the player.

        The one-off O(total objects) cost lives here (index + park-everything),
        not in the per-frame path. After this, everything is inactive except the
        cells inside the activation radius of ``player_pos``.
        """
        # The overhead camera may never float higher than the map's
        # activation radius: above it, the far plane that follows residency
        # would leave the camera nothing to see. A ceiling, not an edit --
        # the host's own overhead_height is kept and applies again on stop().
        self._set_camera_ceiling(self._authored_activation_radius)
        # Fade the camera out at the activation radius before reading its
        # horizon, so residency is the authored radius rather than whatever
        # the view distance happens to reach.
        if self._release_view_horizon is None:
            self._release_view_horizon = bound_view_horizon(
                self.logic, self._authored_activation_radius)
        self._sync_visual_horizon()
        brushes = list(self.logic.editor_state.brushes)
        things = list(self.logic.editor_state.things)
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

        # The tier model is configured from the manager's own activation radius,
        # so "how far out is the world resident" and "how far out is it
        # simulated" are one number (§13). Persistent globals are stamped once,
        # here: they live in no cell, so no crossing can ever reach them.
        self.tiers.set_radii(self._tier_near_radius(), self.manager.activation_radius)
        self.tiers.pin(self.manager.persistent_things)
        self._publish_relevance_radii()

        if pos is not None:
            self.manager.update(pos, force=True)
            self._apply_active_snapshot()
            self.tiers.update(self.manager, *_xz(pos))
            if self._fit is not None:
                # Residency and tiers were just fitted here; the first tick
                # need not redo them.
                self._fit_pos = self._tier_pos = _xz(pos)
        self._started = True

    def _publish_relevance_radii(self) -> None:
        """Tell the host how far out this map's world is live.

        There has to be exactly one answer to that. A map that streams 8192
        units would otherwise still have its consumers stop at the stock 2048,
        and a map that streams 1024 would keep paying full simulation for
        entities it has already parked.

        This publishes; it does not manage. The values are the two boundaries
        the tier model was configured with, written where a host can read them,
        and nothing here looks at what the host does with them. Guarded, so a
        host that has no such fields is simply given them.
        """
        logic = self.logic
        try:
            logic.sim_active_radius = float(self.tiers.active_radius)
            logic.sim_near_radius = float(self.tiers.near_radius)
        except Exception:
            pass  # a host that refuses the attributes is not an error

    #: Transient per-object markers the session writes while it is running.
    #: They mean nothing once it has stopped, and a map saved with them in it
    #: would carry this session's activation state into the file.
    TRANSIENT_KEYS = ("bw_active", SIM_TIER_KEY)

    def stop(self) -> None:
        """Restore every object the session parked; leave the world untouched.

        Guarantees the editor/runtime world is exactly as it was before play —
        no lingering hidden/disabled flags, no lost data, no changed UUIDs, and
        none of the session's own activation markers.

        The marker sweep covers the whole world, not just the parked set: an
        object inside the active region is marked ``bw_active`` too, and it was
        never parked, so restoring the parked objects alone would leave the
        markers on everything the player walked near.
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
        self._clear_transient_markers()   # includes every tier stamp
        self.tiers.clear(())
        # Hand the camera-fitted tiers back: the host keeps no trace of this
        # session, so the next play -- Big World or not -- starts unfitted.
        self._fit = None
        self._fit_pos = self._tier_pos = None
        self.tiers.set_near_rect(None)
        self._set_camera_ceiling(None)
        self._restore_terrain()
        if self._release_view_horizon is not None:
            self._release_view_horizon()
            self._release_view_horizon = None
        self._started = False

    def _clear_transient_markers(self) -> None:
        """Drop this session's own activation markers from every object."""
        for brush in self.logic.editor_state.brushes:
            if isinstance(brush, dict):
                for key in self.TRANSIENT_KEYS:
                    brush.pop(key, None)
        for thing in self.logic.editor_state.things:
            props = getattr(thing, "properties", None)
            if isinstance(props, dict):
                for key in self.TRANSIENT_KEYS:
                    props.pop(key, None)

    def tick(self, player_pos=None) -> bool:
        """Per-frame entry point. Returns True if the active set changed.

        The hot path: a single cell-of-point compare early-outs until the
        player crosses a cell boundary, so a stationary or slow-moving player
        pays almost nothing. On a crossing, only the objects that entered or
        left the region are toggled — plus the resident entities that walked
        into a different cell, which is the same order of work.
        """
        if not self.streaming or not self._started:
            return False
        pos = player_pos if player_pos is not None else self._player_pos()
        if pos is None:
            return False
        px, pz = _xz(pos)
        radius_changed = self._sync_visual_horizon()

        crossed = (cell_of_point(px, pz, self.manager.cell_size)
                   != self.manager._last_player_cell)
        # Fitted to the camera, residency and tiers follow the player's own
        # movement rather than waiting for a cell crossing.
        refresh = retier = False
        if self._fit is not None:
            refresh = self._fit_moved((px, pz), self._fit_pos, self.FIT_REFRESH)
            retier = self._fit_moved((px, pz), self._tier_pos, self.FIT_RETIER)
            if refresh:
                self._fit_pos = (px, pz)
            radius_changed = radius_changed or refresh
        if not crossed and not radius_changed and not retier:
            # The common path remains a cheap cell comparison plus a shared
            # camera-horizon read. A render-distance change is the deliberate
            # exception: residency must follow the new visual boundary.
            return False

        # When both the player crosses a cell and the effective residency
        # radius changes, re-file movers before the forced residency recompute.
        # That forced update can drop their old cell from _active_thing_ids; once
        # that happens the later refile pass cannot see them.
        moved = None
        if crossed or refresh:
            moved = self.manager.refile_moved_things(pos)
            if moved.changed:
                self._apply_delta(moved)

        radius_delta = None
        if radius_changed:
            radius_delta = self.manager.update(pos, force=True)
            if radius_delta.changed:
                for coord in radius_delta.leaving_cells:
                    self.commit_cell(coord)
                self._apply_delta(radius_delta)

        if not crossed:
            self.tiers.update(self.manager, px, pz)
            self._tier_pos = (px, pz)
            return bool(radius_delta and radius_delta.changed) or bool(moved and moved.changed)

        delta = self.manager.update(pos)
        if delta.changed:
            # Commit the persistent state of every cell that is about to leave
            # the active set into the registry *before* it is parked, so its
            # changes outlive the unload (the core invariant of a Big World
            # save).
            for coord in delta.leaving_cells:
                self.commit_cell(coord)
            self._apply_delta(delta)

        # The entering/leaving stamps were written by _set_thing_active above,
        # proportional to what actually moved. This re-tiers the active cells,
        # restamping the entities of the ring whose distance band changed — and
        # on a crossing that changed no cell's residency, it is all that runs.
        self.tiers.update(self.manager, *_xz(pos))
        self._tier_pos = (px, pz)
        return delta.changed or bool(moved and moved.changed)

    def _apply_delta(self, delta) -> None:
        """Switch the world on/off for one activation delta.

        Shared by the residency delta and the moved-entity delta because they
        are the same statement — *these objects entered the live region, those
        left it* — reached by two different routes.
        """
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
        terrain = self.logic.world_runtime.terrain
        if terrain is None:
            return
        self._terrain = terrain
        self._terrain_saved = {
            "streaming": terrain.streaming,
            "stream_radius": terrain.stream_radius,
            "stream_evict_padding": terrain.stream_evict_padding,
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
        # Parking changes the live flag only; the authored value is stashed.
        touch(brush, VISIBILITY)

    def _restore_brush(self, brush: dict) -> None:
        if _HID_MARK in brush:
            brush["hidden"] = brush.pop(_HID_MARK)
        brush["bw_active"] = True

    def _set_thing_active(self, thing, active: bool) -> None:
        """Park or unpark one entity, tier stamp included.

        The single place an entity crosses the residency boundary, and therefore
        the single place its DORMANT stamp is written or dropped. Doing it here
        rather than in ``tick`` means every caller -- the startup park-everything
        pass, the per-crossing delta, the post-start snapshot -- keeps residency
        and tier in step without any of them having to remember to.
        """
        props = getattr(thing, "properties", None)
        if not isinstance(props, dict):
            return
        if active:
            self._restore_thing(thing)
            self._parked_things.pop(id(thing), None)
            self.tiers.unpark(thing)
        else:
            self.tiers.park(thing)
            if _HID_MARK not in props:
                props[_HID_MARK] = props.get("hidden", False)
            if _DIS_MARK not in props:
                props[_DIS_MARK] = props.get("disabled", False)
            props["hidden"] = True
            props["disabled"] = True
            props["bw_active"] = False
            self._parked_things[id(thing)] = thing
        touch(thing, VISIBILITY)

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
        touch(light, VISIBILITY)

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
        sub = build_cell_delta_registry(
            self._base_subset(live), live, self.manager.cell_size)
        for k, entry in sub.items():
            if entry.get("things") or entry.get("brushes"):
                self.registry[k] = entry

    def _base_subset(self, live: dict) -> dict:
        """The base records that share an id with *live*'s.

        The delta only ever looks base records up by the ids *live* holds, so
        diffing one cell against this subset gives the same result as diffing
        it against the whole base world -- at the cost of the cell, not of the
        world, for every cell committed on unload. The id index is built once
        per captured base.
        """
        base = self._base_level or {}
        index = getattr(self, "_base_index", None)
        if index is None or index[0] is not base:
            things = {}
            for t in base.get("things", []) or []:
                tid = (t.get("properties") or {}).get("id")
                if tid:
                    things[tid] = t
            brushes = {b.get("id"): b for b in base.get("brushes", []) or []
                       if isinstance(b, dict) and b.get("id")}
            index = self._base_index = (base, things, brushes)
        _base, things, brushes = index
        sub_things = [things[tid] for tid in (
            (t.get("properties") or {}).get("id") for t in live.get("things", []))
            if tid in things]
        sub_brushes = [brushes[bid] for bid in (
            b.get("id") for b in live.get("brushes", []) if isinstance(b, dict))
            if bid in brushes]
        return {"things": sub_things, "brushes": sub_brushes}

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
        player = self.logic.player_runtime.player
        if player is None:
            return None
        return player.pos

    def player_cell(self):
        pos = self._player_pos()
        if pos is None:
            return self.manager._last_player_cell
        x, z = _xz(pos)
        return cell_of_point(x, z, self.manager.cell_size)

    def stats(self) -> dict:
        s = self.manager.stats()
        s["configured_activation_radius"] = self._authored_activation_radius
        s["configured_deactivation_radius"] = self._authored_deactivation_radius
        s["visual_horizon"] = self._visual_horizon
        terrain = self._terrain
        if terrain is not None:
            s["terrain_fill"] = True
            s["terrain_infinite"] = bool(self.terrain_infinite)
            s["terrain_streaming"] = bool(terrain.streaming)
            s["terrain_chunks"] = int(terrain.streamed_chunks)
            s["terrain_stream_radius"] = float(terrain.stream_radius)
        else:
            s["terrain_fill"] = False
        s.update(self.tiers.stats())
        return s
