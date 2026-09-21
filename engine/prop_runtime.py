"""Core interaction runtime for generic carryable Prop entities.

Physics simulation itself lives in :mod:`engine.physics`; this module only
handles pickup, carrying, dropping and Prop-specific I/O.
"""
from __future__ import annotations

import math

from .spatial import CellIndex, cell_of_point


def _vec(p):
    return float(p[0]), float(p[1]), float(p[2])


def _forward(player):
    return (math.sin(player.angle) * math.cos(player.pitch),
            math.sin(player.pitch),
            math.cos(player.angle) * math.cos(player.pitch))


def _length(v):
    return math.sqrt(sum(x * x for x in v))


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


class PropSession:
    """The Prop domain: which Things are Props, and their session state.

    PropSession is the authoritative registry for Props at runtime. The
    authoritative *world* data is still the thing list -- ``editor_state.things``
    in the editor, the package's things in the standalone player -- and the
    registry here is derived from it by :meth:`rebuild`, called from the one
    place each tier re-derives its entity caches. Nothing else keeps a second
    list of Props: ``LogicThread`` asks this session.

    Ownership, stated once so it can be checked:

    ===========================  =========================================
    Prop data and session state  ``PropSession``
    Position while in motion     ``PhysicsWorld``
    Placement / reset / restore  whichever subsystem performs the operation
    Spatial membership           ``PropSession``'s :class:`CellIndex`
    Synchronisation              an explicit :meth:`moved` call
    ===========================  =========================================

    **``prop.pos`` is the source of truth. The cell index is a derived
    acceleration structure.** It narrows a query to a few cells; the answer is
    then decided by reading live positions. A cell that has gone stale can
    therefore cost a Prop its place in a result, never put a wrong one in it —
    and :meth:`moved` is how a subsystem that moves a Prop keeps even that from
    happening.

    No ``PhysicsBody`` state is mirrored here: the session asks
    ``PhysicsWorld`` to make a body kinematic, to wake it, or to call back on
    rest, and reads nothing back. ``SpatialGrid`` remains the index of the
    static world; this index holds Props, which are dynamic and deliberately
    absent from it.
    """

    #: Query radius used when a Prop declares no ``pickup_reach`` of its own.
    DEFAULT_PICKUP_REACH = 110.0

    def __init__(self, logic):
        self.logic = logic
        self.props = []
        self.held = None
        self._by_id = {}
        # Spatial membership for the Prop domain, on the one cell convention
        # the rest of Fio partitions space with (engine.spatial).
        self._cells = CellIndex()
        self._filed = {}          # id(prop) -> the cell it is filed under
        self._max_reach = self.DEFAULT_PICKUP_REACH

    @staticmethod
    def is_prop(thing):
        """The serialised type contract, so every tier agrees what a Prop is."""
        return getattr(thing, "properties", {}).get("type") == "prop"

    @property
    def physics(self):
        return getattr(self.logic, "_physics_world", None)

    # -- registry ---------------------------------------------------------

    def rebuild(self, things=None):
        """Re-derive the registry from the authoritative thing list.

        Called wherever a tier rebuilds its entity caches -- play-mode enter,
        ``LogicSpawner``, savegame load, a console spawn, package load in the
        player -- so the Prop registry goes stale at exactly the same moments as
        every other derived entity list, and at no others. There is no polling:
        a Prop that enters or leaves the world does so through a code path that
        already has to say so.

        Adopting and releasing are per-Prop, so a rebuild never disturbs a Prop
        that was already registered -- its authored home position and its
        physics wiring survive a spawn elsewhere in the map.
        """
        if things is None:
            things = self.logic.things
        current = [t for t in things if self.is_prop(t)]
        current_ids = {id(t) for t in current}

        for prop in self.props:
            if id(prop) not in current_ids:
                self._release(prop, restore_home=False)

        known = self._by_id
        for prop in current:
            if id(prop) not in known:
                self._adopt(prop)

        self.props = current
        self._by_id = {id(t): t for t in current}
        if self.held is not None and id(self.held) not in current_ids:
            self.held = None

    def by_id(self, entity_id):
        """The Prop with this ``id()``, or None. The engine's Prop lookup."""
        return self._by_id.get(entity_id)

    def _adopt(self, prop):
        """Take responsibility for a Prop that has entered the world."""
        prop.properties["_prop_home_pos"] = list(prop.pos)
        prop.properties.pop("_drop_requested", None)
        self._file(prop)
        if self.physics is not None:
            self.physics.set_rest_callback(prop, self._on_rest)
            self.physics.set_kinematic(prop, False)

    def _release(self, prop, restore_home=True):
        """Hand a Prop back: drop our callbacks and our authored state."""
        home = prop.properties.pop("_prop_home_pos", None)
        prop.properties.pop("_drop_requested", None)
        self._unfile(prop)
        if self.physics is not None:
            self.physics.set_rest_callback(prop, None)
            self.physics.set_kinematic(prop, False)
        if restore_home and home is not None:
            prop.pos = home

    # -- spatial membership -----------------------------------------------

    def _reach_of(self, prop):
        try:
            return float(prop.properties.get("pickup_reach", self.DEFAULT_PICKUP_REACH))
        except (TypeError, ValueError):
            return self.DEFAULT_PICKUP_REACH

    def _file(self, prop):
        coord = cell_of_point(float(prop.pos[0]), float(prop.pos[2]))
        self._cells.insert_point(prop, float(prop.pos[0]), float(prop.pos[2]))
        self._filed[id(prop)] = coord
        # The query radius has to cover the furthest-reaching Prop, or a Prop
        # with a large authored reach would be filtered out by a radius derived
        # from the default one.
        reach = self._reach_of(prop)
        if reach > self._max_reach:
            self._max_reach = reach

    def _unfile(self, prop):
        coord = self._filed.pop(id(prop), None)
        if coord is not None:
            self._cells.remove_point(prop, coord)

    def moved(self, prop):
        """Tell the Prop domain that *prop* has been moved to a new position.

        The synchronisation half of the ownership contract. Whoever moved the
        Prop — a placement, a reset, a savegame restore, a streaming layer
        bringing a cell back — calls this once afterwards, and its cell
        membership is brought back in line with ``prop.pos``.

        Cheap and idempotent: a Prop that has not left its cell costs a
        comparison. Unknown Props are ignored, so a caller never has to check
        whether the thing it moved was a Prop.
        """
        previous = self._filed.get(id(prop))
        if previous is None:
            return
        x, z = float(prop.pos[0]), float(prop.pos[2])
        coord = cell_of_point(x, z)
        if coord == previous:
            return
        self._cells.remove_point(prop, previous)
        self._cells.insert_point(prop, x, z)
        self._filed[id(prop)] = coord

    def refile(self, props):
        """Batch half of the synchronisation contract.

        A subsystem that moves many Props at once does not call :meth:`moved`
        in a loop — it hands the set over here. Everything that can be decided
        in bulk already has been by then: the caller's own batch interface
        (see ``PhysicsWorld.entities_that_changed_cell``) is what narrows a
        world of bodies down to the few that actually left their cell, so what
        arrives is the set whose membership is genuinely wrong, and re-filing
        it is a handful of dict and list operations.
        """
        for prop in props:
            self.moved(prop)

    def sync_physics_positions(self):
        """Take the Props physics has moved out of their cells, and re-file them.

        The Prop domain owns its index, so it is the session that asks — after
        the physics update, once per frame. The question costs one vectorised
        comparison over the body arrays no matter how many bodies there are,
        and normally answers "none", because a body has to cross a whole
        512-unit column to need re-filing.
        """
        physics = self.physics
        if physics is None:
            return
        changed = getattr(physics, "entities_that_changed_cell", None)
        if changed is None:
            return
        moved = changed()
        if moved:
            self.refile(moved)

    def props_within(self, x, z, radius):
        """Broad phase: Props filed in cells the circle ``(x, z, radius)`` reaches.

        A superset — a cell is bigger than the circle — so every caller filters
        the result against live positions. That is the point: the index picks
        which Props are worth looking at, and ``prop.pos`` decides.
        """
        cells = self._cells
        found = []
        for coord in cells.cells_within(x, z, radius):
            found.extend(cells.cell(coord))
        return found

    # -- session lifecycle ------------------------------------------------

    def start(self):
        self.held = None
        self.rebuild()

    def stop(self):
        for prop in self.props:
            self._release(prop)
        self.held = None
        self.props = []
        self._by_id = {}
        self._cells.clear()
        self._filed.clear()
        self._max_reach = self.DEFAULT_PICKUP_REACH

    def _fire(self, prop, output):
        io = getattr(self.logic, "io_manager", None)
        if io is not None:
            io.fire_output(prop, output)

    def _on_rest(self, prop):
        self._fire(prop, "OnRest")

    def tick(self, delta, use_pressed):
        del delta
        player = getattr(self.logic, "player", None)
        if player is None:
            return
        eye_pos = _vec(player.pos)
        eye = (eye_pos[0], eye_pos[1] + float(getattr(player, "camera_height", 40.0)), eye_pos[2])
        forward = _forward(player)
        if self.held is not None:
            self._carry(eye, forward, use_pressed)
        elif use_pressed:
            self._pick_in_view(eye, forward)

    def _pick_in_view(self, eye, forward):
        """The Prop the player is looking at, within its pickup reach.

        Spatial membership narrows this to the Props filed near the player;
        the decision is then made on live positions, so the index can only ever
        affect *which* Props are examined, never the answer for one that is.
        """
        candidates = self.props_within(eye[0], eye[2], self._max_reach)
        best = None
        best_distance = None
        for prop in candidates:
            p = prop.properties
            if p.get("disabled") or not p.get("pickup_enabled", True):
                continue
            dx = float(prop.pos[0]) - eye[0]
            dy = float(prop.pos[1]) - eye[1]
            dz = float(prop.pos[2]) - eye[2]
            distance = _length((dx, dy, dz))
            reach = float(p.get("pickup_reach", 110.0))
            if distance < 0.001 or distance > reach:
                continue
            if _dot(forward, (dx / distance, dy / distance, dz / distance)) < 0.86:
                continue
            if best_distance is None or distance < best_distance:
                best, best_distance = prop, distance
        if best is not None:
            self.held = best
            if self.physics is not None:
                self.physics.set_kinematic(best, True)
            self._fire(best, "OnPickedUp")

    def _carry(self, eye, forward, use_pressed):
        prop = self.held
        p = prop.properties
        offset = p.get("carry_offset", [0.0, -6.0, 0.0])
        distance = float(p.get("carry_distance", 55.0))
        prop.pos = [
            eye[0] + forward[0] * distance + float(offset[0]),
            eye[1] + forward[1] * distance + float(offset[1]),
            eye[2] + forward[2] * distance + float(offset[2]),
        ]
        self.moved(prop)
        if not (use_pressed or p.pop("_drop_requested", False)):
            self.logic.current_hud_message = "[E] Drop"
            return

        # Drop requested this tick: no "[E] Drop" prompt, so a gameplay
        # plugin's HUD line (e.g. Tidy progress) is not suppressed.
        # A plugin may consume the drop (for example, a Tidy receptacle
        # placement) while the core Prop still owns pickup, carrying and the
        # eventual ordinary drop.
        interceptor = getattr(self.logic, "_prop_drop_interceptor", None)
        if interceptor is not None:
            try:
                if interceptor(prop):
                    return
            except Exception as exc:
                # A broken plugin must not prevent the core prop from
                # dropping normally, but the failure must be visible.
                print(f"[PropSession] drop interceptor failed: {exc!r}")

        self.held = None
        if self.physics is not None:
            self.physics.set_kinematic(prop, False)
            self.physics.wake(prop, [0.0, float(p.get("drop_velocity", 0.0)), 0.0])
        self._fire(prop, "OnDropped")
