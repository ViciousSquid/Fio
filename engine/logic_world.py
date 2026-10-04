"""World/entity indexing runtime delegated from LogicThread.

Owns play-session entity lookup caches, filtered entity indexes and the dense
LevelChanger activation projection. The host remains responsible for tick
ordering and for invoking this rebuild when world topology changes.
"""

from __future__ import annotations

import numpy as np


class LogicWorld:
    """Runtime indexes for entities and world rows."""

    def __init__(
        self,
        logic,
        *,
        levelchanger_type=None,
        monster_type=None,
        timer_type=None,
        path_node_type=None,
    ):
        self.logic = logic
        self.levelchanger_type = levelchanger_type
        self.monster_type = monster_type
        self.timer_type = timer_type
        self.path_node_type = path_node_type

    def build_entity_caches(self):
        """Build lookup and hot-path entity indexes for the current world."""
        logic = self.logic

        logic._name_cache = {}
        logic._id_cache = {}

        for brush in logic.editor_state.brushes:
            name = brush.get("name")
            if name:
                logic._name_cache[name] = brush
            entity_id = brush.get("id")
            if entity_id:
                logic._id_cache[entity_id] = brush

        for thing in logic.editor_state.things:
            name = thing.properties.get("name")
            if name:
                logic._name_cache[name] = thing
            entity_id = thing.properties.get("id")
            if entity_id:
                logic._id_cache[entity_id] = thing

        logic._trigger_brushes = [
            (brush.get("id") or index, brush)
            for index, brush in enumerate(logic.editor_state.brushes)
            if brush.get("is_trigger")
        ]
        logic._trigger_brush_by_bid = dict(logic._trigger_brushes)
        logic.trigger_runtime._refresh_use_triggers()

        if logic._props is not None:
            logic._props.rebuild(logic.editor_state.things)

        LevelChanger = self.levelchanger_type
        logic._levelchanger_things = [
            thing
            for thing in logic.editor_state.things
            if LevelChanger and isinstance(thing, LevelChanger)
        ]
        self.refresh_levelchanger_table()

        MonsterThing = self.monster_type
        logic._monster_things = [
            thing
            for thing in logic.editor_state.things
            if MonsterThing and isinstance(thing, MonsterThing)
        ]
        logic._monster_by_id = {
            id(thing): thing for thing in logic._monster_things
        }

        live = logic._monster_by_id
        with logic._monster_lock:
            states = logic.monster_ai.monster_states
            for key in [key for key in states if key not in live]:
                del states[key]

        LogicTimer = self.timer_type
        logic._timer_things = [
            thing
            for thing in logic.editor_state.things
            if LogicTimer and isinstance(thing, LogicTimer)
        ]

        logic._indexed_things = tuple(logic.editor_state.things)
        logic._indexed_brushes = tuple(logic.editor_state.brushes)

        if (
            logic.play_mode
            and logic._moving_rows is not None
            and logic._indexed_brushes != logic._moving_rows
        ):
            logic.mover_runtime._reindex_moving_brushes()
            logic.collision_runtime.mark_dirty()

        logic.portal_runtime.rebuild_links()

    def find_entity_by_name(self, name: str):
        """Resolve an entity by name against the live world/session cache."""
        logic = self.logic
        if not name:
            return None
        if not logic.play_mode:
            return self.scan_entity("name", name)
        return logic._name_cache.get(name)

    def find_entity_by_id(self, entity_id: str):
        """Resolve an entity by stable id against the live world/cache."""
        logic = self.logic
        if not entity_id:
            return None
        if not logic.play_mode:
            return self.scan_entity("id", entity_id)
        return logic._id_cache.get(entity_id)

    def scan_entity(self, key, value):
        """Search the live editor world outside a play session."""
        logic = self.logic
        for thing in reversed(logic.editor_state.things):
            if thing.properties.get(key) == value:
                return thing
        for brush in reversed(logic.editor_state.brushes):
            if brush.get(key) == value:
                return brush
        return None

    def find_path_node_by_name(self, name: str):
        """Return the named PathNode, or None."""
        logic = self.logic
        PathNode = self.path_node_type
        if not name or PathNode is None:
            return None

        entity = logic._name_cache.get(name)
        if entity is not None and isinstance(entity, PathNode):
            return entity

        for thing in logic.editor_state.things:
            if (
                isinstance(thing, PathNode)
                and thing.properties.get("name", "") == name
            ):
                return thing
        return None

    def refresh_levelchanger_table(self):
        """Pack LevelChanger activation geometry into dense numeric columns."""
        logic = self.logic
        things = getattr(logic, "_levelchanger_things", ())
        count = len(things)

        if not count:
            logic._levelchanger_centres = np.empty(
                (0, 3), dtype=np.float32
            )
            logic._levelchanger_radii = np.empty(
                0, dtype=np.float32
            )
            logic._levelchanger_eligible = np.empty(
                0, dtype=bool
            )
            return

        logic._levelchanger_centres = np.asarray(
            [thing.pos for thing in things],
            dtype=np.float32,
        ).reshape(count, 3)

        logic._levelchanger_radii = np.asarray(
            [
                float(thing.properties.get("radius", 128.0))
                for thing in things
            ],
            dtype=np.float32,
        )

        logic._levelchanger_eligible = np.asarray(
            [
                not thing.properties.get("disabled", False)
                and thing.properties.get("usable", True)
                for thing in things
            ],
            dtype=bool,
        )

    def notify_visibility_changed(self):
        """Record a drawable-world invalidation without rebuilding collision."""
        self.logic.visibility_changes += 1

    def notify_authored_visibility_changed(self):
        """Rebuild collision after an authored visibility change."""
        logic = self.logic
        self.notify_visibility_changed()
        logic.collision_runtime.refresh_collision_brushes_cache()
        grid = getattr(logic, "_spatial_grid", None)
        if grid is not None:
            grid.populate(logic._collision_brushes_cache)

    def watch_world_rows(self):
        """Re-index the play session briefly after an authored world edit."""
        logic = self.logic
        row_watch_ticks = 30

        epoch = getattr(logic.editor_state, "world_epoch", None)
        if epoch != getattr(logic, "_rows_epoch", None):
            logic._rows_epoch = epoch
            logic._rows_watch = row_watch_ticks

        if not getattr(logic, "_rows_watch", 0):
            return

        logic._rows_watch -= 1
        brushes_changed = tuple(logic.editor_state.brushes) != logic._indexed_brushes
        things_changed = tuple(logic.editor_state.things) != logic._indexed_things
        if brushes_changed or things_changed:
            self.build_entity_caches()

    def release_session_indexes(self):
        """Drop references held by play-session world indexes."""
        logic = self.logic

        logic._name_cache = {}
        logic._id_cache = {}
        logic._indexed_things = ()
        logic._indexed_brushes = ()
        logic._moving_rows = None

        logic._monster_by_id = {}
        logic._monster_things = []
        logic._timer_things = []

        logic._levelchanger_things = []
        logic._levelchanger_centres = np.empty((0, 3), dtype=np.float32)
        logic._levelchanger_radii = np.empty(0, dtype=np.float32)
        logic._levelchanger_eligible = np.empty(0, dtype=bool)

        logic._trigger_brushes = []
        logic._trigger_brush_by_bid = {}
        logic._use_trigger_entries = []

        logic._portal_things = []
        logic._portal_target_things = []
        logic._portal_slots = np.empty(0, dtype=np.int32)
        logic._portal_target_slots = np.empty(0, dtype=np.int32)
