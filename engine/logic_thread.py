"""
Game Logic Processing

This thread runs game logic at a fixed timestep (60 Hz), handling:
- Player movement and physics
- Entity interactions and triggers
- I/O event dispatching
- Mover and door animations
- Prop collection
- Player death detection
- Portal transit (Prey 2006-style world portals)
"""

import threading
import time
import json
import numpy as np
from typing import List, Dict, Any, Optional
import glm
import math
import os
import random

from .threaded_game_state import ThreadedGameState
from .camera import Camera
from .change_journal import moved, touch
from .cutscene_runtime import CutsceneRuntime
from .logic_camera import LogicCamera
from .logic_player import LogicPlayer
from .logic_movers import LogicMovers, DOOR_DIRECTION_MAP
from .logic_parenting import LogicParenting
from .logic_portals import LogicPortals, _PORTAL_TRANSIT_COOLDOWN, _PORTAL_PLAYER_EXIT_EPSILON
from .logic_triggers import LogicTriggers, _trigger_activation, _trigger_damage, _trigger_is_once, _trigger_save
from .logic_combat import LogicCombat
from .logic_timing import LogicTiming
from .logic_collision import LogicCollision
from .logic_world import LogicWorld
from .logic_render import LogicRender
from .logic_session import LogicSession
from .logic_interaction import LogicInteraction
from .logic_editor import LogicEditor
from .prop_runtime import PropSession



# Import Thing subclasses for type checking
try:
    from editor.things import (Speaker, Prop as PropThing, Light,
                               Monster as MonsterThing, PathNode, LogicTimer,
                               PlayerStart, Portal, LevelChanger, ENTITY_TYPES)
except ImportError:
    Speaker = None
    PropThing = None
    Light = None
    MonsterThing = None
    PathNode = None
    LogicTimer = None
    PlayerStart = None
    Portal = None
    LevelChanger = None
    ENTITY_TYPES = {}

# Import I/O system
try:
    from editor.io_system import IOManager
    from editor.io_handlers import register_all_input_handlers
    IO_AVAILABLE = True
except ImportError as e:
    print(f"################################################")
    print(f"CRITICAL ERROR: I/O SYSTEM FAILED TO LOAD")
    print(f"Error details: {e}")
    print(f"################################################")
    import traceback
    traceback.print_exc()
    IO_AVAILABLE = False
    IOManager = None

# Plugin system (optional). The logic thread drives the plugin lifecycle
# natively: attach runtime I/O at construction, dispatch play-start/stop with
# play mode, and tick active plugins once per play frame. Guarded so a build
# without the plugins package runs unchanged.
try:
    from plugins.manager import get_manager as _get_plugin_manager, load_plugins as _load_plugins
    PLUGINS_AVAILABLE = True
except Exception:
    _get_plugin_manager = None
    _load_plugins = None
    PLUGINS_AVAILABLE = False

# Import debug logger
try:
    from editor.debug_console import debug_log
except ImportError:
    def debug_log(category, message):
        print(f"[{category}] {message}")

# Import the extracted MonsterAI class and new thread
from .monster_ai import MonsterAI

# Noise "loudness" multipliers scale a monster's hearing range per event.
# 1.0 = heard out to the full sensory radius (gunshots); water splashes are
# quieter, so a monster has to be closer to notice the player entering/leaving.
_GUNFIRE_LOUDNESS = 1.0
_WATER_LOUDNESS = 0.7


class LogicThread(threading.Thread):
    """
    Unified logic thread for both editor and play mode.
    Runs continuously at a fixed timestep (60 Hz).
    """
    
    TICK_RATE = 60
    TICK_DURATION = 1.0 / TICK_RATE

    # Construction-time host contracts for the extracted Logic runtimes.
    #
    # These are deliberately the shared attributes a runtime may assume exist
    # after LogicThread.__init__ has completed. Session-specific contents are
    # reset by LogicSession, but the host containers themselves are created here.
    # Every extracted runtime listed below is constructed during LogicThread
    # initialization and participates in the host validation seam.
    _RUNTIME_HOSTS = (
        "camera",
        "player_runtime",
        "mover_runtime",
        "render_runtime",
        "session_runtime",
        "interaction_runtime",
        "editor_runtime",
        "trigger_runtime",
        "portal_runtime",
        "parenting_runtime",
        "combat_runtime",
        "timing_runtime",
        "collision_runtime",
        "world_runtime",
        "prop_runtime",
    )

    _RUNTIME_HOST_CONTRACTS = {
        "camera": (
            "player_runtime",
        ),
        "movers": (
            "editor_state",
            "player_runtime",
            "io_manager",
            "world_runtime",
        ),
        "parenting": (
            "editor_state",
        ),
        "render": (
            "game_state",
            "editor_state",
            "player_runtime",
        ),
        "session": (
            "editor_state",
            "io_manager",
            "player_runtime",
        ),
        "interaction": (
            "player_runtime",
            "io_manager",
            "mover_runtime",
            "world_runtime",
        ),
        "editor": (
            "game_state",
        ),
        "triggers": (
            "player_runtime",
            "game_state",
            "interaction_runtime",
            "io_manager",
            "portal_runtime",
            "prop_runtime",
            "session_runtime",
            "world_runtime",
        ),
        "portals": (),
        "combat": (
            "player_runtime",
            "io_manager",
            "camera",
            "collision_runtime",
            "editor_state",
            "game_state",
            "monster_ai",
            "portal_runtime",
            "session_runtime",
            "trigger_runtime",
            "world_runtime",
        ),
        "timing": (
            "io_manager",
        ),
        "collision": (
        ),
        "world": (
            "editor_state",
            "monster_ai",
            "_monster_lock",
        ),
    }

    def __init__(self, game_state: ThreadedGameState, editor_state):
        super().__init__(daemon=True)
        # Serialises the simulation with everything that rebuilds or reads the
        # world from another thread.  The run loop holds it for each frame's
        # ticks and render-state projection; entering or leaving play mode and
        # saving or restoring a session (all called from the UI thread) hold it
        # for their whole duration, so a tick never sees a half-built or
        # half-torn-down session.  Reentrant: the same thread may nest.
        self._tick_lock = threading.RLock()
        self.game_state = game_state
        self.editor_state = editor_state
        self.running = False
        
        # Camera state and camera math live in LogicCamera; LogicThread keeps
        # the camera runtime as an owned subsystem and schedules its updates.
        self.camera = LogicCamera(self)
        self.player_runtime = LogicPlayer(self)
        # LogicRender owns frustum math, HUD render fades, and dense render-state publication.
        self.render_runtime = LogicRender(self)
        self.session_runtime = LogicSession(self)
        self.interaction_runtime = LogicInteraction(self)
        self.editor_runtime = LogicEditor(self)
        self.mover_runtime = LogicMovers(self)
        # LogicWorld owns entity lookup, indexing and LevelChanger projections.
        self.world_runtime = LogicWorld(
            self,
            levelchanger_type=LevelChanger,
            monster_type=MonsterThing,
            timer_type=LogicTimer,
            path_node_type=PathNode,
        )
        self.prop_runtime = PropSession(self)
        
        # Player stats

        # I/O System
        self.io_manager = None
        if IO_AVAILABLE and IOManager:
            self.io_manager = IOManager()
            self.io_manager.set_logic_thread(self)
            self.io_manager.set_entity_finder(self.world_runtime.find_entity_by_name)
            self.io_manager.set_entity_finder_by_id(self.world_runtime.find_entity_by_id)
            self.io_manager.set_game_state(self.game_state)
            register_all_input_handlers(self.io_manager)

        # Plugin runtime: load once and attach this thread's I/O handlers. All
        # loaded plugins attach (handlers self-gate on the plugin's enabled
        # state), so a plugin enabled later — e.g. auto-enabled when its level
        # loads — works without a re-attach. Fully guarded and optional.
        self.plugins = None
        # Hard kill-switch: FIO_NO_PLUGINS=1 turns the plugin system off at the
        # engine level — nothing loads, attaches or binds, and every per-frame
        # guard below short-circuits on ``self.plugins is None`` for literally
        # zero plugin overhead. (Distinct from FIO_DISABLED_PLUGINS, which only
        # skips named plugins.)
        _plugins_off = os.environ.get("FIO_NO_PLUGINS", "").strip().lower() in ("1", "true", "yes", "on")
        if _plugins_off:
            print("[LogicThread] plugins disabled via FIO_NO_PLUGINS")
        elif PLUGINS_AVAILABLE and _get_plugin_manager is not None:
            try:
                _load_plugins()
                self.plugins = _get_plugin_manager()
                if self.io_manager is not None:
                    self.plugins.attach_runtime(self)
                # Bind the host so plugins can reach the whole engine and hook
                # its event stream (the emit points below). One-time, like attach.
                self.plugins.bind_host(self, kind="engine")
            except Exception as exc:
                print(f"[LogicThread] plugin attach skipped: {exc}")

        self.trigger_runtime = LogicTriggers(self)
        self.trigger_runtime._reset_trigger_state()

        # Countdown state for logic_timer entities, keyed by the timer's UUID
        # (see LogicThread._timer_key) so it survives a save and can never be
        # confused with another entity's.

        # Active light FadeIn/FadeOut transitions, keyed by id(light entity)
        
        
        # Collection state
        
        
        # Mover and door animation state are owned by mover_runtime.
        # Model collision pseudo-brushes for things with model_path


        # Interaction state is owned by interaction_runtime.

        # Visual FX

        # Monster AI (delegated to separate class + thread)
        self._player_damage_lock = threading.Lock()
        self.monster_ai = MonsterAI(self)


        # CutsceneRuntime owns cinematic playback state. LogicThread remains
        # the simulation orchestrator and delegates the state machine here.
        self.cutscene_runtime = CutsceneRuntime(self)
        # LogicPortals owns portal topology, transit and fade runtime.
        self.portal_runtime = LogicPortals(self, portal_type=Portal)
        # LogicParenting owns mover-parented light and portal transforms.
        self.parenting_runtime = LogicParenting(
            self, light_type=Light, portal_type=Portal
        )
        # LogicCombat owns weapons, projectiles, bullet marks and noise.
        self.combat_runtime = LogicCombat(self)
        # LogicTiming owns timer and light-fade advancement.
        self.timing_runtime = LogicTiming(self)
        # LogicCollision owns world/model collision geometry and cache rebuilding.
        self.collision_runtime = LogicCollision(self)

        # Entity lookup caches — built on play-mode enter
        # Dense LevelChanger activation columns. Spatial data is rebuilt with
        # the entity caches; the per-tick interaction path only consumes these
        # float32 columns and scalar-dispatches the selected row.
        # Authored health per monster UUID, captured on play-mode enter so the
        # Respawn input has a value to restore (see LogicSession.reset_all_monsters).

        # Portal slots use the same enumerate(editor_state.things) address space
        # as EntityTable. Links are resolved once when topology changes.

        # Level-complete UI state is owned by interaction_runtime


        # Performance Monitoring
        self.actual_tps = 0.0
        self._tick_count = 0
        self._last_tps_time = time.perf_counter()

        # A failed simulation tick enters a controlled fault state instead of
        # continuing from a partially mutated world. The current play session
        # is torn down cleanly and the editor remains usable.
        self._tick_faulted = False
        #: GUI-thread callback used only to marshal fatal tick teardown.
        #: The engine stays Qt-free; QtGameView supplies a bound signal emitter.
        self._gui_fault_teardown = None
        self._gui_fault_teardown_requested = False
        self._tick_fault_message = ""

        # Fail immediately if the extraction changed construction order or
        # forgot a host-owned attribute needed by one of the runtimes.
        self._validate_runtime_contracts()

    def _validate_runtime_contracts(self):
        """Validate the construction-level seam between LogicThread and runtimes."""
        host_state = vars(self)
        for runtime_name in self._RUNTIME_HOSTS:
            runtime = host_state.get(runtime_name)
            assert runtime is not None, (
                f"{runtime_name} was not constructed before runtime validation"
            )
            if runtime_name != "camera":
                assert runtime.logic is self, (
                    f"{runtime_name}.logic must point at this LogicThread"
                )

        for runtime_name, attributes in self._RUNTIME_HOST_CONTRACTS.items():
            missing = [name for name in attributes if name not in host_state]
            assert not missing, (
                f"{runtime_name} runtime host contract missing: "
                + ", ".join(missing)
            )

    # =========================================================================
    # PLUGIN EVENTS
    # =========================================================================

    def _plugin_emit(self, event: str, **data):
        """Emit an engine event to subscribed plugins. Always safe.

        The single choke point for the engine's plugin event stream: fully
        guarded, and a no-op when the plugin system is absent or nobody is
        listening. New extension points are added by calling this — no other
        engine change, and plugins can subscribe to events that don't exist yet.
        """
        mgr = self.plugins
        if mgr is None:
            return
        try:
            mgr.emit(event, logic=self, **data)
        except Exception:
            pass

    # =========================================================================
    # CUTSCENE RUNTIME
    # =========================================================================
    # CutsceneRuntime owns cinematic playback state and behaviour; LogicThread
    # invokes it directly rather than mirroring its API.

    # =========================================================================
    # ENTITY LOOKUP (for I/O system)
    # =========================================================================
    
    # -- visibility invalidation ------------------------------------------
    #
    # Two notifications, because "what is drawn" and "what is collided with"
    # go stale at different costs.  Both are the *host* side of the streaming
    # contract in ``plugins.bigworld.runtime.StreamingHost``; neither knows
    # anything about a particular streaming layer.

        # =========================================================================
    # PLAYER & MODE MANAGEMENT
    # =========================================================================

    def start(self):
        # Set before the thread exists, not in run(): a stop() that arrives
        # before run() gets going must not be overwritten.
        self.running = True
        super().start()

    def run(self):
        last_time = time.perf_counter()
        accumulator = 0.0
        
        while self.running:
            current_time = time.perf_counter()
            frame_time = current_time - last_time
            last_time = current_time
            
            if frame_time > 0.25:
                frame_time = 0.25
                
            accumulator += frame_time
            accumulator = self._step_frame(accumulator)
            self._publish_frame()
            
            sleep_time = self.TICK_DURATION - (time.perf_counter() - current_time)
            if sleep_time > 0:
                time.sleep(sleep_time * 0.9)
                
    def _publish_frame(self) -> bool:
        """Hand the frame just prepared to the renderer, if it is not reading.

        One-shot events stay latched until a frame carrying them is actually
        published: a declined swap (the renderer is mid-paint) or a catch-up
        frame running several ticks would otherwise drop them.
        """
        if not getattr(self, '_frame_prepared', True):
            return False
        if not self.game_state.request_swap():
            return False
        self.combat_runtime.muzzle_flash_active = False
        return True

    def _step_frame(self, accumulator: float) -> float:
        """Run every whole tick *accumulator* holds, then project the frame.

        One acquisition of the tick lock per frame, so a play-mode change or a
        save/restore from the UI thread lands between frames, never inside one.
        Returns the time left over for the next frame.
        """
        with self._tick_lock:
            while accumulator >= self.TICK_DURATION:
                started = time.perf_counter()
                try:
                    self._tick(self.TICK_DURATION)
                except Exception:
                    # Never continue a failed simulation tick. By this point the
                    # authoritative world may be only partially advanced, so
                    # silently executing another tick would compound corruption.
                    import traceback
                    trace = traceback.format_exc()
                    self._tick_faulted = True
                    self._tick_fault_message = trace
                    debug_log(
                        "LogicThread",
                        "Fatal simulation tick failure; ending play session:\n" + trace,
                    )
                    # Qt/editor teardown must not run on this worker thread.
                    # Stop gameplay immediately, then let the GUI thread run
                    # the normal QtGameView play-mode teardown path.
                    callback = self._gui_fault_teardown
                    if callback is not None and not self._gui_fault_teardown_requested:
                        self._gui_fault_teardown_requested = True
                        try:
                            callback(trace)
                        except Exception:
                            debug_log(
                                "LogicThread",
                                "Could not queue GUI teardown after tick failure:\n"
                                + traceback.format_exc(),
                            )
                    else:
                        # Standalone/headless hosts have no Qt lifecycle to marshal
                        # through, so they still need the native engine teardown.
                        try:
                            self.session_runtime.apply_play_mode(False)
                        except Exception:
                            debug_log(
                                "LogicThread",
                                "Play-session teardown after tick failure also failed:\n"
                                + traceback.format_exc(),
                            )
                    # Discard accumulated play-mode time. The next frame starts
                    # from a clean editor-mode state rather than replaying stale
                    # simulation debt.
                    accumulator = 0.0
                    break
                accumulator -= self.TICK_DURATION
                #: Milliseconds the last simulation tick took (Debug Tables).
                self.tick_ms = (time.perf_counter() - started) * 1000.0
                self._update_tps_counter()

            try:
                self.render_runtime.prepare_render_state()
                self._frame_prepared = True
            except Exception:
                # Same policy as a bad tick: a frame that cannot be projected
                # (a malformed authored value, a projection bug) must not kill
                # the thread and freeze the game for good. The half-built
                # buffer is not published; the error is logged once per kind.
                self._frame_prepared = False
                import traceback
                trace = traceback.format_exc()
                key = trace.strip().splitlines()[-1]
                if key != getattr(self, '_last_prepare_error', None):
                    self._last_prepare_error = key
                    debug_log("LogicThread",
                              "Unhandled exception preparing a frame:\n" + trace)
        return accumulator

    def set_gui_fault_teardown(self, callback):
        """Register the GUI callback used to marshal fatal-tick teardown.

        The callback is generic so the logic thread does not import or touch Qt.
        """
        self._gui_fault_teardown = callback

    def stop(self):
        self.running = False
        self.session_runtime.stop_monster_ai()

    def _update_tps_counter(self):
        self._tick_count += 1
        t = time.perf_counter()
        if t - self._last_tps_time >= 1.0:
            self.actual_tps = self._tick_count / (t - self._last_tps_time)
            self._tick_count = 0
            self._last_tps_time = t

    def _tick(self, delta: float):
        if self.session_runtime.play_mode:
            self._tick_play_mode(delta)
            self.collision_runtime.rebuild_if_dirty()
        else:
            self.editor_runtime.tick(delta)


    #: Ticks to keep comparing the world's row sets after an editor edit.

    def _tick_paused_world(self, delta):
        """One play tick with the world frozen: input drained, plugins run.

        Look and fire input is discarded rather than queued, so nothing the
        player did over a menu lands in the world when it resumes. The use key
        goes to the plugins, which is how a game's screen closes on it.

        Plugins tick exactly when an unpaused tick would reach them: not during
        a cinematic, a death or the level-complete screen, which freeze input
        and return before the plugin step either way.
        """
        self.game_state.consume_mouse_delta()
        use_key = self.game_state.consume_use_key()
        self.game_state.consume_shot()
        if self.cutscene_runtime.state or self.player_runtime.player_dead or self.interaction_runtime.level_complete_ui:
            return
        if self.plugins is not None and self.plugins.wants_tick():
            self.plugins.tick(
                self,
                use_pressed=use_key,
                interaction_consumed=bool(self.interaction_runtime.current_hud_message),
                delta=delta,
                keys=self.game_state.get_keys,
            )

    def _tick_play_mode(self, delta):
        if not self.player_runtime.player:
            return
        self.world_runtime.watch_world_rows()

        if self.session_runtime.world_paused:
            self._tick_paused_world(delta)
            return
        
        # Update movers & doors first (for platform carrying)
        self.mover_runtime._update_movers(delta)
        self.mover_runtime._update_doors(delta)
        self.parenting_runtime._update_parented_lights()
        self.parenting_runtime._update_parented_portals()
        
        # Update I/O system (delayed events)
        if self.io_manager:
            self.io_manager.update(delta)
        
        # Update logic timers
        self.timing_runtime.update_logic_timers(delta)

        # Update light FadeIn/FadeOut transitions
        self.timing_runtime.update_light_fades(delta)

        # ---- Camera transition (First Person <-> Overhead tween) ----
        # Advances even while a cinematic runs so a queued toggle resolves; it
        # only affects the view matrix when no cinematic is overriding it.
        self.camera.update_camera_transition(delta)

        # ---- Cinematic camera: suppress player input while active ----
        self.cutscene_runtime._update_cinematic_camera(delta)
        if self.cutscene_runtime.state:
            self.game_state.consume_mouse_delta()
            self.game_state.consume_use_key()
            self.game_state.consume_shot()
            return

        # ---- Player dead: freeze all gameplay input ----
        if self.player_runtime.player_dead:
            self.game_state.consume_mouse_delta()
            self.game_state.consume_use_key()
            self.game_state.consume_shot()
            return

        # ---- Level Complete UI: freeze player input ----
        if self.interaction_runtime.level_complete_ui:
            self.game_state.consume_mouse_delta()
            self.game_state.consume_use_key()
            self.game_state.consume_shot()
            return
        
        # Player input
        keys = self.game_state.get_keys()
        mouse_dx, mouse_dy = self.game_state.consume_mouse_delta()
        use_key = self.game_state.consume_use_key()

        # Player movement/physics live in LogicPlayer; this method retains
        # authoritative tick ordering and the existing consumed-input surface.
        player_runtime = self.player_runtime
        player_runtime.update_primary(delta, keys, mouse_dx, mouse_dy)
        player_runtime.update_water_sounds(delta)

        # Gameplay
        self.interaction_runtime.handle(use_key)
        self.prop_runtime.tick(delta, use_key)
        physics_world = self.session_runtime.physics_world
        if physics_world is not None:
            physics_world.step(delta, self.player_runtime.player)
            # Physics owned those positions for the duration of the step; the
            # Prop domain takes its index back into line now that it is over.
            self.prop_runtime.sync_physics_positions()

        self.trigger_runtime._handle_triggers(use_key, delta)

        # Plugin tick: runs last in the gameplay sequence so the use-key edge is
        # intact and any plugin HUD prompt is the final word for the frame. The
        # manager early-outs before building a context when no plugin ticks, so
        # a plugin-free session pays almost nothing here.
        # Gate the whole call on a cached O(1) check: with no ticking plugin and
        # no 'tick' listener, we skip the call and its argument packing entirely.
        if self.plugins is not None and self.plugins.wants_tick():
            self.plugins.tick(
                self,
                use_pressed=use_key,
                interaction_consumed=bool(self.interaction_runtime.current_hud_message),
                delta=delta,
                # Pass the getter, not the keys: the manager calls it only if a
                # plugin actually ticks/listens, so an idle session never pays
                # the lock+copy that reading held keys costs.
                keys=self.game_state.get_keys,
            )

        # Portal transit detection — must run AFTER player physics so the
        # post-physics position is the one tested against portal planes.
        self.portal_runtime.update(delta)
        
        # Player shooting
        if self.game_state.consume_shot():
            self.combat_runtime._handle_shooting()
            
        combat_runtime = self.combat_runtime
        combat_runtime._update_bullet_marks()

        # Clean up expired player-noise events through combat ownership.
        combat_runtime.prune_noise_events(3.0)

        # Update monster projectiles (flying monster ranged attacks)
        # NOTE: Monster AI itself now runs in MonsterAIThread
        combat_runtime._update_monster_projectiles(delta)

        # ── Player 2 physics (split-screen) ──────────────────────────────────
        self.player_runtime.update_player2(delta)

    # =========================================================================
    # LOGIC TIMER UPDATE
    # =========================================================================
    
    # =========================================================================
    # TRIGGER HANDLING
    # =========================================================================

    # INTERACTIONS
    # =========================================================================

    # PARENTED ENTITY RUNTIME
    # =========================================================================

