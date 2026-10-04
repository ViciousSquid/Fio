"""Play-session lifecycle delegated from LogicThread.

Owns play-mode enter/exit, native save/load, session cache teardown, monster
thread lifetime/reset, and player-spawn I/O initialisation.
"""

from __future__ import annotations

import os
import threading
import time

from .change_journal import JOURNAL, STATE
from .logic_combat import NO_PROJECTILES as _NO_PROJECTILES
from .monster_ai import MonsterAIThread

try:
    from editor.things import (
        Speaker,
        Prop as PropThing,
        Light,
        Monster as MonsterThing,
        PlayerStart,
    )
except ImportError:
    Speaker = None
    PropThing = None
    Light = None
    MonsterThing = None
    PlayerStart = None


class LogicSession:
    """Runtime lifecycle for a LogicThread play session."""

    def __init__(self, logic):
        self.logic = logic
        self._world_pause_owners = frozenset()
        self.physics_world = None
        self.spatial_grid = None
        self._world_pause_lock = threading.Lock()

    def set_world_paused(self, owner, paused: bool = True) -> None:
        """Hold or release a world pause owned by *owner*."""
        with self._world_pause_lock:
            owners = set(self._world_pause_owners)
            if paused:
                owners.add(owner)
            else:
                owners.discard(owner)
            self._world_pause_owners = frozenset(owners)

    @property
    def world_paused(self) -> bool:
        with self._world_pause_lock:
            return bool(self._world_pause_owners)

    def world_pause_owners(self) -> frozenset:
        with self._world_pause_lock:
            return self._world_pause_owners

    def apply_play_mode(self, enabled: bool):
        """Enter or leave Play Mode under the session's tick lock."""
        with self.logic._tick_lock:
            return self._apply_play_mode_unlocked(enabled)

    def _apply_play_mode_unlocked(self, enabled: bool):
        logic = self.logic
        logic.play_mode = enabled

        # A pause belongs to the session that took it: a new session, or the
        # editor after one, never starts frozen by a request nobody released.
        with self._world_pause_lock:
            self._world_pause_owners = frozenset()

        # Likewise a camera ceiling: a session that sets one (Big World) sets
        # it again from its play-start hook, which runs after this.
        logic.camera.overhead_height_limit = None

        if enabled:
            # A new Play session clears any previous fault marker.
            logic._tick_faulted = False
            logic._gui_fault_teardown_requested = False
            logic._tick_fault_message = ""

            if hasattr(logic.editor_state, "config"):
                logic.player_runtime.p2_turn_sensitivity = float(
                    logic.editor_state.config.get(
                        "Controls",
                        "p2_turn_sensitivity",
                        fallback=10.0,
                    )
                )

            logic.mover_runtime._init_movers()
            logic.mover_runtime._init_doors()
            logic.parenting_runtime._init_parented_lights()
            logic.parenting_runtime._init_parented_portals()

            # Bake swept-mesh collision for angled (clipped/convex) brushes so
            # they collide as real slopes/wedges. Must run before the spatial
            # grid is populated below so the grid indexes them by true bounds.
            logic.collision_runtime.prepare_angled_brush_collision()

            logic.collision_runtime._model_collision_brushes = (
                logic.collision_runtime.build_model_collision_brushes()
            )
            logic.collision_runtime._physics_body_brushes = [
                b
                for b in logic.collision_runtime._model_collision_brushes
                if b.get("_physics_body")
            ]
            logic.collision_runtime.refresh_collision_brushes_cache()

            # Runtime effect state belongs to this play session, not authoring.
            logic.effect_store.begin_session(logic.editor_state.things)

            # Reset player stats.
            logic.player_runtime.player_health = 100
            logic.player_runtime.player_max_health = 100
            logic.player_runtime.player_dead = False
            logic.player_runtime.god_mode = False
            logic.player_runtime.buddha_mode = False
            logic.player_runtime.notarget = False

            # Reset collection state.
            logic.trigger_runtime._reset_trigger_state()
            logic.player_runtime.collected_keys.clear()
            for thing in logic.editor_state.things:
                if PropThing and isinstance(thing, PropThing):
                    # Restores what the author set; forcing carry here made every
                    # Prop -- scenery models included -- carryable.
                    thing.reset_collection()
            logic.prop_runtime.start()

            # Reset speaker/interaction state.
            logic.trigger_runtime.hurt_trigger_timers.clear()
            logic.interaction_runtime.current_hud_message = ""
            logic.interaction_runtime.current_hud_key_name = None

            # Reset water sound state (no spurious enter/exit on spawn).
            logic.player_runtime._player_was_in_water = False
            logic.player_runtime._waterwalk_timer = 0.0

            logic.timing_runtime.timer_states.clear()

            # Reset active weapon / ammunition.
            logic.combat_runtime.active_weapon = None
            logic.combat_runtime.player_ammo = 0
            logic.combat_runtime.gun2_obtained = False
            logic.combat_runtime._last_player_shot_time = float('-inf')
            logic.combat_runtime._last_player_shot_time = float("-inf")

            # Reset visual FX.
            logic.combat_runtime.bullet_marks = []
            logic.combat_runtime.muzzle_flash_active = False

            # Reset P2 stats.
            logic.player_runtime.player2_health = 100
            logic.player_runtime.player2_max_health = 100
            logic.player_runtime.player2_dead = False

            self.reset_all_monsters(clear_dead=True)

            # Reset I/O system.
            if logic.io_manager:
                logic.io_manager.reset()
                from editor.io_system import get_connections

                for brush in logic.editor_state.brushes:
                    for conn in get_connections(brush):
                        conn.reset()
                for thing in logic.editor_state.things:
                    for conn in get_connections(thing):
                        conn.reset()

            # Build entity caches before constructing session-local physics.
            logic.world_runtime.build_entity_caches()

            from .physics import PhysicsWorld, SpatialGrid

            self.spatial_grid = SpatialGrid(cell_size=512.0)
            self.spatial_grid.populate(
                logic.editor_state.brushes + logic.collision_runtime._model_collision_brushes
            )
            self.physics_world = PhysicsWorld(self.spatial_grid)
            self.physics_world.rebuild(logic.collision_runtime._physics_body_brushes)
            logic.monster_ai.set_spatial_grid(self.spatial_grid)

            # PropSession is the registry for the Prop runtime domain.
            logic.prop_runtime.start()

            # Reset cinematic state.
            logic.cutscene_runtime.state = None
            logic.camera.camera_transition = None
            logic.render_runtime._hud_cinematic_last_active = False
            logic.render_runtime._hud_cinematic_fade_started = None

            # Start the health HUD hidden; player spawn uses the normal
            # fast 1.5-second fade-in followed by the 4-second fade-out.
            hud_now = time.perf_counter()
            logic.render_runtime._hud_health_alpha = 0.0
            logic.render_runtime._hud_health_last_value = logic.player_runtime.player_health
            logic.render_runtime._hud_health_fade_started = hud_now
            logic.render_runtime._hud_health_fade_from = 0.0
            logic.render_runtime._hud_health_fade_phase = "in"

            # Reset portal runtime state.
            logic.portal_runtime.reset_session()

            logic.interaction_runtime.level_complete_ui = None

            # Reset light fade transitions for a clean play session.
            logic.timing_runtime.light_fade_states.clear()
            if Light is not None:
                for thing in logic.editor_state.things:
                    if isinstance(thing, Light) and hasattr(
                        thing, "_fade_nominal"
                    ):
                        del thing._fade_nominal

            logic.combat_runtime._monster_projectiles.clear()
            logic.combat_runtime.projectile_positions = _NO_PROJECTILES
            logic.combat_runtime._gunfire_events.clear()

            # Spawn I/O is deliberately before timers and AI, matching the
            # original LogicThread ordering.
            self.fire_player_spawn_outputs()
            logic.timing_runtime.init_logic_timers()
            self.start_monster_ai()

        else:
            if (
                logic.cutscene_runtime.state
                and logic.cutscene_runtime.state.get("json_cutscene")
            ):
                logic.cutscene_runtime._finish_json_cutscene(
                    logic.cutscene_runtime.state,
                    fire_finished=False,
                )

            self.stop_monster_ai()
            logic.trigger_runtime._reset_trigger_state()
            logic.trigger_runtime.fired_once_triggers.clear()
            logic.player_runtime.collected_keys.clear()
            logic.trigger_runtime.hurt_trigger_timers.clear()
            logic.mover_runtime._reset_movers()
            logic.mover_runtime._reset_doors()
            logic.parenting_runtime._reset_parented_lights()
            logic.parenting_runtime._reset_parented_portals()
            logic.collision_runtime.clear_angled_brush_collision()
            logic.interaction_runtime.current_hud_message = ""
            logic.interaction_runtime.current_hud_key_name = None
            logic.timing_runtime.timer_states.clear()
            logic.timing_runtime.light_fade_states.clear()
            logic.combat_runtime.active_weapon = None
            logic.combat_runtime.bullet_marks = []
            logic.player_runtime.player_dead = False
            logic.combat_runtime.muzzle_flash_active = False

            logic.prop_runtime.stop()

            if self.physics_world is not None:
                self.physics_world.clear()
            self.physics_world = None

            logic.monster_ai.set_spatial_grid(None)
            if self.spatial_grid is not None:
                self.spatial_grid.clear()
            self.spatial_grid = None

            logic.mover_runtime.mover_path_states = {}
            logic.cutscene_runtime.state = None
            logic.camera.camera_transition = None
            logic.render_runtime._hud_cinematic_last_active = False
            logic.render_runtime._hud_cinematic_fade_started = None
            logic.render_runtime._hud_health_alpha = 0.5
            logic.render_runtime._hud_health_last_value = None
            logic.render_runtime._hud_health_fade_started = None
            logic.render_runtime._hud_health_fade_from = 0.5
            logic.render_runtime._hud_health_fade_phase = "idle"

            # Reset portal runtime state.
            logic.portal_runtime.reset_session()

            logic.interaction_runtime.level_complete_ui = None
            logic.combat_runtime._monster_projectiles.clear()
            logic.combat_runtime.projectile_positions = _NO_PROJECTILES
            logic.combat_runtime._gunfire_events.clear()

            self.reset_all_monsters(clear_dead=False)
            self.release_session_caches()

        # Plugin lifecycle runs only after the core session is coherent.
        if logic.plugins is not None:
            try:
                if enabled:
                    logic.plugins.dispatch_play_start(logic)
                else:
                    logic.plugins.dispatch_play_stop(logic)
            except Exception as exc:
                print(
                    "[LogicThread] plugin lifecycle dispatch failed: "
                    f"{exc}"
                )
            logic._plugin_emit("play_start" if enabled else "play_stop")

    #: What the player intentionally carries through a level change.
    LOADOUT_FIELDS = ("active_weapon", "gun2_obtained", "player_ammo")

    def carried_loadout(self) -> dict:
        """Capture the player's carried weapons for a level change."""
        logic = self.logic
        with logic._tick_lock:
            combat = logic.combat_runtime
            return {
                "active_weapon": combat.active_weapon,
                "gun2_obtained": combat.gun2_obtained,
                "player_ammo": combat.player_ammo,
            }

    def restore_loadout(self, loadout: dict) -> None:
        """Restore a carried weapon loadout after a new session starts."""
        logic = self.logic
        with logic._tick_lock:
            combat = logic.combat_runtime
            if "active_weapon" in loadout:
                combat.active_weapon = loadout["active_weapon"]
            if "gun2_obtained" in loadout:
                combat.gun2_obtained = bool(loadout["gun2_obtained"])
            if "player_ammo" in loadout:
                combat.player_ammo = int(loadout["player_ammo"])

    def save_session(
        self,
        path: str,
        *,
        map_name: str = "",
        save_mode: str = "full",
        base_level: dict = None,
    ):
        logic = self.logic
        if not logic.play_mode:
            return False, "Nothing to save — not in play mode."

        try:
            from engine import savegame

            session = logic.plugins.services.get("bigworld")
            if session is not None and session.streaming:
                with logic._tick_lock:
                    session.commit_all()
                    snapshot = savegame.build_snapshot(
                        logic,
                        map_name=map_name,
                        world_mode=savegame.WORLD_MODE_BIGWORLD,
                        cell_deltas=session.serialize_registry(),
                        base_world=session.base_identity(map_name),
                    )
            else:
                with logic._tick_lock:
                    snapshot = savegame.build_snapshot(
                        logic,
                        map_name=map_name,
                        save_mode=save_mode,
                        base_level=base_level,
                    )

            savegame.write(path, snapshot)
            mode_used = snapshot.get("save_mode", "full")
            world = snapshot.get("world_mode")
            label = f"{mode_used}/{world}" if world else mode_used
            return True, (
                f"Saved play session to '{os.path.basename(path)}' "
                f"({label})"
            )
        except Exception as exc:
            return False, f"Save failed: {exc}"

    def load_session(
        self,
        path: str,
        *,
        map_name: str = "",
        base_level: dict = None,
    ):
        logic = self.logic
        if not logic.play_mode:
            return False, "Enter play mode before loading a session."

        try:
            from engine import savegame

            data = savegame.read(path)
            with logic._tick_lock:
                report = savegame.restore_auto(
                    logic,
                    data,
                    current_map_name=map_name,
                    base_level=base_level,
                )

            msg = f"Loaded play session from '{os.path.basename(path)}'"
            warning = report.get("warning")
            if warning:
                msg += f" — {warning}"
            return True, msg
        except FileNotFoundError:
            return False, f"Save file not found: {path}"
        except Exception as exc:
            return False, f"Load failed: {exc}"

    def release_session_caches(self):
        """Drop every reference held only for the finished play session."""
        logic = self.logic
        logic.world_runtime.release_session_indexes()
        logic.collision_runtime._collision_brushes_cache = []
        logic.collision_runtime._model_collision_brushes = []
        logic.collision_runtime._physics_body_brushes = []
        logic.mover_runtime._mover_brush_list = []
        logic.mover_runtime._door_brush_list = []
        logic.world_runtime.monster_spawn_health = {}

        if logic.io_manager is not None:
            logic.io_manager.reset()

        for player in (logic.player, logic.player2):
            if player is not None:
                player.ground_object = None

    def start_monster_ai(self):
        logic = self.logic
        self.stop_monster_ai()
        logic.monster_ai_thread = MonsterAIThread(
            logic,
            logic.monster_ai,
            logic._monster_lock,
            tick_rate=30,
        )
        logic.monster_ai_thread.start()

    def stop_monster_ai(self):
        logic = self.logic
        thread = logic.monster_ai_thread
        logic.monster_ai_thread = None
        if thread is not None:
            thread.stop()
            if (
                thread.is_alive()
                and thread is not threading.current_thread()
            ):
                thread.join(timeout=2.0)

    def reset_all_monsters(self, clear_dead=True):
        """Reset all monster AI state and capture authored spawn health."""
        logic = self.logic
        with logic._monster_lock:
            logic.monster_ai.forget_monsters()

        if not MonsterThing:
            return

        if clear_dead:
            logic.world_runtime.monster_spawn_health = {}

        reset = []
        for thing in logic.editor_state.things:
            if not isinstance(thing, MonsterThing):
                continue

            reset.append(thing)
            if clear_dead:
                try:
                    logic.world_runtime.monster_spawn_health[
                        thing.properties.get("id")
                    ] = int(thing.properties.get("health", 100))
                except (TypeError, ValueError):
                    pass

            thing.properties.pop("is_shooting", None)
            thing.properties.pop("_vel_y", None)
            if clear_dead:
                thing.properties.pop("dead", None)

            triggered = thing.properties.get("triggered", False)
            wake_sight = thing.properties.get("wake_on_sight", True)
            thing.properties["awake"] = bool(
                not (triggered or wake_sight)
            )

        JOURNAL.record_many(reset, STATE)

    def start_speakers_on_spawn(self):
        logic = self.logic
        if not logic.io_manager or not Speaker:
            return

        for thing in logic.editor_state.things:
            if not isinstance(thing, Speaker):
                continue
            if not bool(thing.properties.get("play_on_start", False)):
                continue

            target_name = thing.properties.get("name", "")
            target_id = thing.properties.get("id", "")
            logic.io_manager._execute_input(
                target_name,
                "PlaySound",
                "",
                "PlayerSpawn",
                target_id=target_id,
            )

    def fire_player_spawn_outputs(self):
        logic = self.logic
        if not logic.io_manager:
            return

        # Start-on speakers initialise before the PlayerStart output chain, so
        # an explicit OnPlayerSpawn connection can override authored state.
        self.start_speakers_on_spawn()

        if not PlayerStart:
            return

        for thing in logic.editor_state.things:
            if isinstance(thing, PlayerStart):
                logic.io_manager.fire_output(
                    thing,
                    "OnPlayerSpawn",
                )
                logic._plugin_emit("player_spawn", start=thing)
                break
