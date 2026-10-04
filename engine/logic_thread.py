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
from .player import Player
from .camera import Camera
from .change_journal import moved, touch
from .cutscene_runtime import CutsceneRuntime
from .logic_camera import LogicCamera
from .logic_player import LogicPlayer
from .logic_movers import LogicMovers, DOOR_DIRECTION_MAP
from .logic_parenting import LogicParenting
from .logic_portals import LogicPortals, _PORTAL_TRANSIT_COOLDOWN, _PORTAL_PLAYER_EXIT_EPSILON
from .logic_triggers import LogicTriggers, _trigger_activation, _trigger_damage, _trigger_is_once, _trigger_save
from .logic_combat import LogicCombat, NO_PROJECTILES as _NO_PROJECTILES
from .logic_timing import LogicTiming
from .logic_collision import LogicCollision, COLLISION_KEYS as _COLLISION_KEYS
from .logic_world import LogicWorld
from .logic_render import LogicRender
from .logic_session import LogicSession
from .logic_interaction import LogicInteraction
from .logic_editor import LogicEditor
from .projectile_table import ProjectileStore
from .effect_table import EffectStore


# Qt key constants kept as part of LogicThread's historical input surface.
# Play input is now implemented by LogicPlayer, but tests/tools and older
# integrations still import these raw key codes from engine.logic_thread.
Key_W = 0x57
Key_S = 0x53
Key_A = 0x41
Key_D = 0x44
Key_Space = 0x20
Key_C = 0x43
Key_Shift = 0x01000020
Key_Control = 0x01000021


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
    _COLLISION_KEYS = _COLLISION_KEYS

    # Historical projectile constants remain on LogicThread while the
    # implementation lives in LogicCombat.
    PROJECTILE_MONSTER_LIFT = LogicCombat.PROJECTILE_MONSTER_LIFT
    PROJECTILE_MONSTER_RADIUS = LogicCombat.PROJECTILE_MONSTER_RADIUS
    PROJECTILE_PLAYER_RADIUS = LogicCombat.PROJECTILE_PLAYER_RADIUS
    """
    Unified logic thread for both editor and play mode.
    Runs continuously at a fixed timestep (60 Hz).
    """
    
    TICK_RATE = 60
    TICK_DURATION = 1.0 / TICK_RATE

    # Trigger polling is scheduled at the fastest supported interval, while
    # each trigger independently decides when its next sample is due.
    TRIGGER_POLL_TICK = 0.25
    #: Slack on both trigger-scheduler comparisons. The scheduler accumulates
    #: arbitrary frame deltas and 1/60 is not exactly representable, so 60
    #: ticks sum to 0.99999999999999989 rather than 1.0; comparing bare against
    #: an exact decimal lost one scheduler step per second and let the poll
    #: cadence drift behind the configured interval. A nanosecond is far below
    #: any cadence a map can author and comfortably above the accumulated
    #: representation error of a whole session.
    TRIGGER_POLL_EPSILON = 1.0e-9

    # Seconds between repeating wade footstep sounds while walking in water
    WATERWALK_INTERVAL = 0.45

    # Editor camera settings
    EDITOR_CAMERA_SPEED = 300.0
    EDITOR_CAMERA_FAST_MULT = 2.5
    EDITOR_MOUSE_SENSITIVITY = 0.15

    # Construction-time host contracts for the extracted Logic runtimes.
    #
    # These are deliberately the shared attributes a runtime may assume exist
    # after LogicThread.__init__ has completed. Session-specific contents are
    # reset by LogicSession, but the host containers themselves are created here.
    # player_runtime stays lazy for lightweight test doubles; the other extracted
    # runtimes are constructed during LogicThread initialization.
    _RUNTIME_HOSTS = (
        "camera",
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
    )

    _RUNTIME_HOST_CONTRACTS = {
        "camera": (),
        "movers": (
            "editor_state",
            "movers",
            "doors",
            "mover_states",
            "door_states",
            "mover_path_states",
            "_mover_brush_list",
            "_door_brush_list",
        ),
        "parenting": (
            "editor_state",
            "_parented_lights",
            "_parented_portals",
        ),
        "player": (
            "player",
            "player2",
            "game_state",
            "_collision_brushes_cache",
            "_mover_brush_list",
            "_player_was_in_water",
            "_waterwalk_timer",
        ),
        "render": (
            "game_state",
            "editor_state",
            "player",
            "player_health",
            "_render_table",
            "_entity_table",
            "_last_edited",
            "_projectile_positions",
        ),
        "session": (
            "editor_state",
            "play_mode",
            "_world_pause_lock",
            "_world_pause_owners",
            "_collision_brushes_cache",
            "_model_collision_brushes",
            "_physics_body_brushes",
            "_mover_brush_list",
            "_door_brush_list",
            "_monster_spawn_health",
            "io_manager",
            "player",
            "player2",
        ),
        "interaction": (
            "player",
            "doors",
            "door_states",
            "collected_keys",
            "current_hud_message",
            "current_hud_key_name",
            "_levelchanger_things",
            "_levelchanger_centres",
            "_levelchanger_radii",
            "_levelchanger_eligible",
            "level_complete_ui",
            "io_manager",
        ),
        "editor": (
            "game_state",
            "editor_camera",
            "_editor_mouselook_active",
        ),
        "triggers": (
            "player",
            "fired_once_triggers",
            "_trigger_brushes",
            "_trigger_contacts",
            "player_in_triggers",
            "_nonplayer_trigger_contacts",
            "_trigger_poll_elapsed_by_bid",
            "_trigger_use_generation",
            "_trigger_use_seen",
            "_use_trigger_entries",
            "_trigger_use_prompt",
            "hurt_trigger_timers",
        ),
        "portals": (
            "player",
            "_portal_cooldowns",
            "_portal_prev_player_pos",
            "_portal_things",
            "_portal_target_things",
            "_portal_slots",
            "_portal_target_slots",
        ),
        "combat": (
            "player",
            "active_weapon",
            "bullet_marks",
            "_monster_projectiles",
            "_projectile_positions",
            "_gunfire_events",
            "_collision_brushes_cache",
            "io_manager",
        ),
        "timing": (
            "_timer_things",
            "timer_states",
            "light_fade_states",
            "io_manager",
        ),
        "collision": (
            "model_collision_enabled",
            "_model_collision_brushes",
            "_physics_body_brushes",
            "_collision_brushes_cache",
        ),
        "world": (
            "editor_state",
            "_props",
            "monster_ai",
            "_monster_lock",
            "_levelchanger_things",
            "_timer_things",
            "_name_cache",
            "_id_cache",
            "_monster_by_id",
            "_indexed_things",
            "_indexed_brushes",
            "_moving_rows",
        ),
    }

    def __init__(self, game_state: ThreadedGameState, 
                 editor_state, 
                 visibility_system: Optional[Any] = None):
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
        self.visibility_system = visibility_system
        
        self.running = False
        self.player: Optional[Player] = None
        self.player2: Optional[Player] = None
        self.player2_health = 100
        self.player2_max_health = 100
        self.player2_dead = False
        self.play_mode = False
        self.terrain = None
        self._first_tick = False
        
        # Frustum culling settings
        self.culling_enabled = True
        self.view_distance = None

        # Camera state and camera math live in LogicCamera. LogicThread keeps
        # only the small forwarding surface needed by the rest of the engine.
        self.camera = LogicCamera(self)
        self.player_runtime = LogicPlayer(self)
        self.editor_camera = self.camera.editor_camera
        # HUD visibility follows LogicCamera control. When a cinematic ends,
        # the entire HUD fades back in over four seconds.
        self._hud_cinematic_last_active = False
        self._hud_cinematic_fade_started = None

        # Health HUD fade timing is deliberately asymmetric: a fast 1.5-second
        # fade-in to full opacity followed immediately by a slower 4-second
        # fade-out to the normal 50% idle state.
        self._hud_health_fade_in_duration = 1.5
        self._hud_health_fade_out_duration = 4.0
        self._hud_health_alpha = 0.5
        self._hud_health_last_value = None
        self._hud_health_fade_started = None
        self._hud_health_fade_from = 0.5
        self._hud_health_fade_phase = "idle"
        self.hud_fade_enabled = True

        # RenderState already owns one persistent RenderTable/EntityTable pair.
        # Keep these aliases only for diagnostics and older tests/code that inspect
        # the logic thread; the authoritative tables now belong to the write buffer
        # and therefore cannot be mutated while the renderer is reading the other
        # buffer.
        write_state = self.game_state.get_write_state()
        self._render_table = write_state.render_table
        self._entity_table = write_state.entity_table
        #: ``id -> object`` of the editor selection the last frame re-read as
        #: edited; see _prepare_render_state.
        self._last_edited = {}

        self._editor_mouselook_active = False
        # LogicRender owns frustum math, HUD render fades, and dense render-state publication.
        self.render_runtime = LogicRender(self)
        self.session_runtime = LogicSession(self)
        self.interaction_runtime = LogicInteraction(self)
        self.editor_runtime = LogicEditor(self)
        # LogicWorld owns entity lookup, indexing and LevelChanger projections.
        self.world_runtime = LogicWorld(
            self,
            levelchanger_type=LevelChanger,
            monster_type=MonsterThing,
            timer_type=LogicTimer,
            path_node_type=PathNode,
        )
        
        # Player stats
        self.player_health = 100
        self.player_max_health = 100
        self.player_dead = False
        self.god_mode = False
        self.buddha_mode = False
        self.notarget = False

        # World pause: each owner (a modal game screen, the entity picker, a
        # pause menu) holds its own request, and the world stays frozen while
        # any is held, so one owner releasing never unpauses another's. See
        # set_world_paused(). Replaced whole, never mutated, so a reader on
        # another thread always sees a consistent set.
        self._world_pause_owners = frozenset()
        self._world_pause_lock = threading.Lock()

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

        # Trigger state remains on LogicThread for compatibility; LogicTriggers
        # owns the algorithms that operate on it.
        # LogicTriggers owns trigger detection and activation state.
        self.trigger_runtime = LogicTriggers(self)
        self.fired_once_triggers: set = set()
        self.trigger_runtime._reset_trigger_state()

        # Logic Gate State
        self.gate_inputs = {}
        
        # Countdown state for logic_timer entities, keyed by the timer's UUID
        # (see LogicThread._timer_key) so it survives a save and can never be
        # confused with another entity's.
        self.timer_states: Dict[str, Dict[str, float]] = {}

        # Active light FadeIn/FadeOut transitions, keyed by id(light entity)
        self.light_fade_states: Dict[int, Dict[str, Any]] = {}
        
        # Hurt trigger timers remain on LogicThread for compatibility;
        # LogicTriggers owns their processing.
        self.hurt_trigger_timers: Dict[int, float] = {}
        self.HURT_INTERVAL = 0.5
        
        # Collection state
        self.collected_keys: set = set()
        
        
        # Speaker state
        self.active_speakers: set = set()
        
        # Mover/Door Lists
        self.movers = []
        self.doors = []
        # PERF: cached brush-only views of self.movers/self.doors (see _init_movers/_init_doors)
        self._mover_brush_list = []
        self._door_brush_list = []
        
        # Mover and door animation state live in a dense table (self._movers());
        # mover_states and door_states are mapping views over it.
        # Mover Animation State
        self.mover_states = {}
        
        # Door Animation State
        self.door_states = {}

        # Parented lights
        self._parented_lights: list = []

        # Parented portals (same system as lights — attach to movers)
        self._parented_portals: list = []

        # Model collision pseudo-brushes for things with model_path
        self._model_collision_brushes: list = []
        self._physics_body_brushes: list = []
        self._physics_world = None
        # PERF: cached self.brushes + self._model_collision_brushes (see
        # _refresh_collision_brushes_cache)
        self._collision_brushes_cache: list = []
        # Bumped every time the set of drawable objects changes, so a consumer
        # that caches across frames can tell whether its cache still describes
        # this world.  See notify_visibility_changed().
        self.visibility_changes = 0

        # Global toggle for model collision (F6 in play mode)
        self.model_collision_enabled = True
        
        # Interaction State
        self.current_hud_message = ""
        self.current_hud_key_name = None

        # Water sound state (enter/exit transition + wade footstep cadence)
        self._player_was_in_water = False
        self._waterwalk_timer = 0.0

        # Visual FX
        self.bullet_marks = []
        self.BULLET_FADE_TIME = 20.0
        
        # Active weapon / ammunition
        self.active_weapon = None
        self.player_ammo = 0
        self.gun2_obtained = False
        self._last_player_shot_time = float("-inf")

        # Muzzle flash
        self.muzzle_flash_active = False

        # Monster AI (delegated to separate class + thread)
        self._monster_lock = threading.RLock()
        self._player_damage_lock = threading.Lock()
        self.monster_ai = MonsterAI(self)
        self.monster_ai_thread = None

        # Mover PathNode waypoint state (used by io_handlers FollowPath)
        self.mover_path_states = {}

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
        self._name_cache = {}
        self._id_cache = {}
        self._trigger_brushes = []
        self._trigger_brush_by_bid = {}
        # The Prop registry (engine.prop_runtime.PropSession).  Created on
        # play-mode enter and None in the editor, where nothing simulates.
        self._props = None
        self._levelchanger_things = []
        # Dense LevelChanger activation columns. Spatial data is rebuilt with
        # the entity caches; the per-tick interaction path only consumes these
        # float32 columns and scalar-dispatches the selected row.
        self._levelchanger_centres = np.empty((0, 3), dtype=np.float32)
        self._levelchanger_radii = np.empty(0, dtype=np.float32)
        self._levelchanger_eligible = np.empty(0, dtype=bool)
        self._monster_things = []
        self._monster_by_id = {}
        self._timer_things = []
        # Authored health per monster UUID, captured on play-mode enter so the
        # Respawn input has a value to restore (see _reset_all_monsters).
        self._monster_spawn_health: Dict[str, int] = {}

        # ── Portal transit state ───────────────────────────────────────────
        self._portal_cooldowns: Dict[int, float] = {}
        # Player position at the end of the previous portal update.  Kept so a
        # crossing can be tested against the point where the movement *segment*
        # pierces the aperture (anti-tunnelling), not just the post-move point.
        self._portal_prev_player_pos = None
        # Portal name → Portal lookup cache; rebuilt on play start and when
        # the things list changes.  Avoids an O(n) rebuild every physics tick.
        self._portal_things: List = []
        self._portal_target_things: List = []
        # Portal slots use the same enumerate(self.things) address space as
        # EntityTable.  Links are resolved once when the topology cache changes.
        self._portal_slots = np.empty(0, dtype=np.int32)
        self._portal_target_slots = np.empty(0, dtype=np.int32)

        self.level_complete_ui = None

        # Monster projectiles (flying monster ranged attacks). The public list
        # surface is retained for compatibility, while numeric simulation state
        # lives in ProjectileStore's persistent NumPy columns.
        self._monster_projectiles: ProjectileStore = ProjectileStore()
        #: Dense execution state for Effect primitives. Authoring Effects remain
        #: in editor_state.things; this store owns their runtime phase and origin.
        self.effect_store: EffectStore = EffectStore()
        #: Their positions as the (N, 3) float32 array each frame publishes.
        self._projectile_positions = _NO_PROJECTILES

        # Gunfire sound events for AI hearing (list of dicts with pos, time, source)
        self._gunfire_events: list = []

        # Performance Monitoring
        self.actual_tps = 0.0
        self._tick_count = 0
        self._last_tps_time = time.perf_counter()

        # Player 2 turn sensitivity (degrees per second)
        self.p2_turn_sensitivity = 10.0
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
        for runtime_name in self._RUNTIME_HOSTS:
            runtime = getattr(self, runtime_name, None)
            assert runtime is not None, (
                f"{runtime_name} was not constructed before runtime validation"
            )
            if runtime_name != "camera":
                assert getattr(runtime, "logic", self) is self, (
                    f"{runtime_name}.logic must point at this LogicThread"
                )

        for runtime_name, attributes in self._RUNTIME_HOST_CONTRACTS.items():
            missing = [name for name in attributes if not hasattr(self, name)]
            assert not missing, (
                f"{runtime_name} runtime host contract missing: "
                + ", ".join(missing)
            )

    @property
    def brushes(self):
        """Dynamically get current brushes from editor state."""
        return self.editor_state.brushes
    
    @property
    def things(self):
        """Dynamically get current things from editor state."""
        return self.editor_state.things

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

    def set_player(self, player: Optional[Player]):
        self.player = player
        self.camera.player = player

    def set_hud_fade_enabled(self, enabled: bool):
        """Enable or disable the damage-driven health HUD fade."""
        with self._tick_lock:
            enabled = bool(enabled)
            if enabled == self.hud_fade_enabled:
                return
            self.hud_fade_enabled = enabled
            self._hud_health_alpha = 0.5 if enabled else 1.0
            self._hud_health_fade_started = None
            self._hud_health_fade_from = self._hud_health_alpha
            self._hud_health_fade_phase = "idle"
            self._hud_health_last_value = self.player_health

    def set_player2(self, player2: Optional[Player]) -> None:
        """Set or clear Player 2 for split-screen mode."""
        self.player2 = player2
        if player2 is None:
            self.player2_health = 100
            self.player2_max_health = 100
            self.player2_dead = False
        
    #: What the player takes with them through a level change: the weapon in
    #: hand, whether the second gun has been picked up, and the ammunition for
    #: it. Everything else (health, keys, the level's own state) starts afresh.
    LOADOUT_FIELDS = ('active_weapon', 'gun2_obtained', 'player_ammo')

    def carried_loadout(self) -> dict:
        """The player's weapons, as :meth:`restore_loadout` takes them."""
        with self._tick_lock:
            return {name: getattr(self, name) for name in self.LOADOUT_FIELDS}

    def restore_loadout(self, loadout: dict) -> None:
        """Hand the player back the weapons they came through a level change
        with. Called after play has restarted on the new level, whose start
        clears them."""
        with self._tick_lock:
            for name in self.LOADOUT_FIELDS:
                if name in loadout:
                    setattr(self, name, loadout[name])

    def set_play_mode(self, enabled: bool):
        """Enter or leave play mode.  Called from the UI thread.

        Held under the tick lock: the flag and the session state it implies
        (movers, doors, collision caches, spatial grid, Prop session, monster
        thread) change together, never with a tick running in between.
        """
        with self._tick_lock:
            self.session_runtime.apply_play_mode(enabled)

    # Session lifecycle is owned by LogicSession. LogicThread keeps only the
    # public play-mode entry point because it owns the tick-lock boundary.

    def set_terrain(self, terrain):
        self.terrain = terrain
    
    @property
    def frustum_aspect(self):
        return self.camera.frustum_aspect

    @frustum_aspect.setter
    def frustum_aspect(self, value):
        self.camera.frustum_aspect = value

    @property
    def frustum_fov(self):
        return self.camera.frustum_fov

    @frustum_fov.setter
    def frustum_fov(self, value):
        self.camera.frustum_fov = value

    @property
    def camera_mode(self):
        return self.camera.camera_mode

    @camera_mode.setter
    def camera_mode(self, value):
        self.camera.camera_mode = value

    @property
    def overhead_height(self):
        return self.camera.overhead_height

    @overhead_height.setter
    def overhead_height(self, value):
        self.camera.overhead_height = value

    @property
    def overhead_height_limit(self):
        return self.camera.overhead_height_limit

    @overhead_height_limit.setter
    def overhead_height_limit(self, value):
        self.camera.overhead_height_limit = value

    @property
    def overhead_tilt(self):
        return self.camera.overhead_tilt

    @overhead_tilt.setter
    def overhead_tilt(self, value):
        self.camera.overhead_tilt = value

    @property
    def overhead_orientation(self):
        return self.camera.overhead_orientation

    @overhead_orientation.setter
    def overhead_orientation(self, value):
        self.camera.overhead_orientation = value

    @property
    def camera_transition(self):
        return self.camera.camera_transition

    @camera_transition.setter
    def camera_transition(self, value):
        self.camera.camera_transition = value

    def set_editor_camera(self, pos: glm.vec3, yaw: float, pitch: float, fov: float):
        self.camera.set_editor_camera(pos, yaw, pitch, fov)

    def get_editor_camera(self) -> Camera:
        return self.camera.get_editor_camera()

    def set_frustum_aspect(self, aspect: float):
        self.camera.set_frustum_aspect(aspect)

    def set_frustum_fov(self, fov: float):
        self.camera.set_frustum_fov(fov)

    def set_view_distance(self, view_distance):
        """Adopt the viewport's shared view-distance settings."""
        self.view_distance = view_distance

    def set_camera_mode(self, mode: str):
        self.camera.set_camera_mode(mode)

    def is_overhead(self) -> bool:
        return self.camera.is_overhead()

    def effective_overhead_height(self) -> float:
        return self.camera.effective_overhead_height()

    def overhead_ground_footprint(self):
        # Keep the extracted camera in sync even when callers assign
        # LogicThread.player directly (older tests/game hosts do this).
        self.camera.player = self.player
        return self.camera.overhead_ground_footprint()

    def start_camera_transition(self, target_mode=None, duration=1.0):
        return self.camera.start_camera_transition(target_mode, duration)

    # =========================================================================
    # MOVER/DOOR RUNTIME
    # =========================================================================

    def _mover_runtime(self):
        """Return the mover/door runtime, creating it for lightweight test doubles."""
        runtime = getattr(self, "mover_runtime", None)
        if runtime is None:
            runtime = LogicMovers(self)
            self.mover_runtime = runtime
        return runtime

    def _movers(self):
        return self._mover_runtime()._movers()

    @property
    def mover_states(self):
        return self._mover_runtime().mover_states

    @mover_states.setter
    def mover_states(self, states):
        self._mover_runtime().mover_states = states

    @property
    def door_states(self):
        return self._mover_runtime().door_states

    @door_states.setter
    def door_states(self, states):
        self._mover_runtime().door_states = states


    # MAIN LOOP
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
        self.muzzle_flash_active = False
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
                    self.play_mode = False
                    callback = getattr(self, "_gui_fault_teardown", None)
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
        if self.play_mode:
            self._tick_play_mode(delta)
            if self._collision_dirty:
                self._rebuild_collision_for_authored_change()
        else:
            self._tick_editor_mode(delta)

    #: Set when an I/O input changed a brush's authored ``hidden`` during play;
    #: see :meth:`mark_collision_dirty`.
    _collision_dirty = False

    def mark_collision_dirty(self):
        """A brush's authored visibility changed at runtime (I/O Show/Hide/Kill).

        The collision grid files brushes by their authored ``hidden`` once, so
        a wall revealed by ``Show`` was drawn but walked through, and a hidden
        one still blocked monsters' sight. Callable from any thread: the rebuild
        itself runs once, at the end of the tick, on the logic thread.
        """
        self._collision_dirty = True

    def _rebuild_collision_for_authored_change(self):
        self._collision_dirty = False
        # The monster AI thread queries the grid while populate() refills it.
        with self._monster_lock:
            self.world_runtime.notify_authored_visibility_changed()

    def _tick_editor_mode(self, delta: float):
        return self.editor_runtime.tick(delta)

    #: Ticks to keep comparing the world's row sets after an editor edit.
    _indexed_things = ()
    _indexed_brushes = ()
    _moving_rows = None
    _rows_epoch = None
    _rows_watch = 0

    # =========================================================================
    # WORLD PAUSE
    # =========================================================================

    def set_world_paused(self, owner, paused: bool = True) -> None:
        """Hold (or release) a pause of the play-mode world for *owner*.

        While any owner holds one, a play tick advances nothing in the world:
        no player movement, look or shooting, no movers, doors, I/O timers,
        triggers, props, physics, projectiles or portals, and the monster AI
        thread idles. Plugins still tick, with the input they would normally
        see, so a game's menus keep working over the frozen world. The frame is
        still published, so the view keeps drawing it.

        *owner* is any hashable key naming who paused (a modal screen, the
        entity picker, a pause menu); each releases only its own request.
        Callable from any thread. Leaving or entering Play Mode drops every
        request.
        """
        with self._world_pause_lock:
            owners = set(self._world_pause_owners)
            if paused:
                owners.add(owner)
            else:
                owners.discard(owner)
            self._world_pause_owners = frozenset(owners)

    @property
    def world_paused(self) -> bool:
        """True while any owner holds a world pause (see set_world_paused)."""
        return bool(self._world_pause_owners)

    def world_pause_owners(self) -> frozenset:
        """The owners currently holding a world pause."""
        return self._world_pause_owners

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
        if self.cutscene_runtime.state or self.player_dead or self.level_complete_ui:
            return
        if self.plugins is not None and self.plugins.wants_tick():
            self.plugins.tick(
                self,
                use_pressed=use_key,
                interaction_consumed=bool(self.current_hud_message),
                delta=delta,
                keys=self.game_state.get_keys,
            )

    def _tick_play_mode(self, delta):
        if not self.player:
            return
        self.world_runtime.watch_world_rows()

        if self._world_pause_owners:
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
        if self.player_dead:
            self.game_state.consume_mouse_delta()
            self.game_state.consume_use_key()
            self.game_state.consume_shot()
            return

        # ---- Level Complete UI: freeze player input ----
        if self.level_complete_ui:
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
        if self._props is not None:
            self._props.tick(delta, use_key)
        physics_world = getattr(self, '_physics_world', None)
        if physics_world is not None:
            physics_world.step(delta, self.player)
            # Physics owned those positions for the duration of the step; the
            # Prop domain takes its index back into line now that it is over.
            if self._props is not None:
                self._props.sync_physics_positions()

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
                interaction_consumed=bool(self.current_hud_message),
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

        # Clean up expired gunfire sound events (keep for 3 seconds)
        current_time = time.perf_counter()
        self._gunfire_events = [
            e for e in self._gunfire_events
            if (current_time - e['time']) < 3.0
        ]

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

