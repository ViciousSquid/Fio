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

from .threaded_game_state import ThreadedGameState, PublishedBrushes, PublishedEntities
from .player import Player
from .camera import Camera
from .constants import is_solid_world_brush, is_water_brush, brush_aabb_bounds
from .brush_geometry import build_collision_mesh, brush_has_geometry
from .prop_runtime import PropSession
from .change_journal import JOURNAL, STATE, moved, touch
from .entity_table import ENT_PROP
from .cutscene_runtime import CutsceneRuntime
from .logic_camera import LogicCamera
from .logic_movers import LogicMovers, DOOR_DIRECTION_MAP
from .logic_parenting import LogicParenting
from .logic_portals import LogicPortals, _PORTAL_TRANSIT_COOLDOWN, _PORTAL_PLAYER_EXIT_EPSILON
from .logic_triggers import LogicTriggers, _trigger_activation, _trigger_damage, _trigger_is_once, _trigger_save
from .logic_combat import LogicCombat, NO_PROJECTILES as _NO_PROJECTILES
from .logic_timing import LogicTiming
from .projectile_table import ProjectileStore
from .effect_table import EffectStore


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
    ENTITY_TYPES = {}

# Import I/O system
try:
    from editor.io_system import IOManager, get_connections
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

# Monster AI constants (still needed for initialisation)
from .monster_constants import (
    WEAPON_DAMAGE,
    NON_FIRING_WEAPONS,
    WEAPON_SHOOT_SOUND,
    MONSTER_PROJECTILE_MAX_DIST,
    MONSTER_PROJECTILE_SPRITE_SIZE,
)

# Import the extracted MonsterAI class and new thread
from .monster_ai import MonsterAI, MonsterAIThread

# Qt key constants
Key_W = 0x57
Key_S = 0x53
Key_A = 0x41
Key_D = 0x44
Key_Space = 0x20
Key_C = 0x43
Key_Shift = 0x01000020
Key_Control = 0x01000021

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
        self.camera = LogicCamera(player=self.player)
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
            self.io_manager.set_entity_finder(self._find_entity_by_name)
            self.io_manager.set_entity_finder_by_id(self._find_entity_by_id)
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
        self.fired_once_triggers: set = set()
        self._reset_trigger_state()

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
        # LogicTriggers owns trigger detection and activation logic.
        self.trigger_runtime = LogicTriggers(self)
        # LogicPortals owns portal topology, transit and fade runtime.
        self.portal_runtime = LogicPortals(self, portal_type=Portal)
        # LogicCombat owns weapons, projectiles, bullet marks and noise.
        self.combat_runtime = LogicCombat(self)
        # LogicTiming owns timer and light-fade advancement.
        self.timing_runtime = LogicTiming(self)

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

    def _cutscene_runtime(self):
        """Return the cutscene runtime, creating it for lightweight test doubles."""
        runtime = getattr(self, "cutscene_runtime", None)
        if runtime is None:
            runtime = CutsceneRuntime(self)
            self.cutscene_runtime = runtime
        return runtime

    @property
    def cinematic_state(self):
        """Compatibility view of the active cutscene state."""
        return self._cutscene_runtime().state

    @cinematic_state.setter
    def cinematic_state(self, value):
        self._cutscene_runtime().state = value

    def _load_cutscene_file(self, filename):
        return self._cutscene_runtime()._load_cutscene_file(filename)

    def _start_json_cutscene(self, entity, filename, data):
        return self._cutscene_runtime()._start_json_cutscene(entity, filename, data)

    def _finish_json_cutscene(self, cs, fire_finished=True):
        return self._cutscene_runtime()._finish_json_cutscene(cs, fire_finished)

    def _fire_cinematic_io_events(self):
        return self._cutscene_runtime()._fire_cinematic_io_events()

    def _update_cinematic_camera(self, delta):
        return self._cutscene_runtime()._update_cinematic_camera(delta)

    def consume_cinematic_messages(self):
        return self._cutscene_runtime().consume_cinematic_messages()


    # Preserve the private helper surface used by older tests/tools.
    _cutscene_number = staticmethod(CutsceneRuntime._cutscene_number)
    _cutscene_vec3 = staticmethod(CutsceneRuntime._cutscene_vec3)
    _cutscene_yaw = staticmethod(CutsceneRuntime._cutscene_yaw)
    _set_cutscene_yaw = staticmethod(CutsceneRuntime._set_cutscene_yaw)
    _cutscene_lerp_angle = staticmethod(CutsceneRuntime._cutscene_lerp_angle)
    _cutscene_sample = staticmethod(CutsceneRuntime._cutscene_sample)

    # =========================================================================
    # ENTITY LOOKUP (for I/O system)
    # =========================================================================
    
    def _build_entity_caches(self):
        """Build O(1) lookup dicts for I/O entity resolution.

        Also precomputes per-tick filtered entity lists (trigger brushes,
        Props, level changers) so hot-path tick handlers don't have to
        linearly rescan the full brush/thing lists every frame — these are
        rebuilt here (play-mode enter, and whenever a thing is spawned) since
        that's the only time the underlying brush/thing collections change.
        """
        self._name_cache = {}
        self._id_cache   = {}
        for b in self.brushes:
            n = b.get('name')
            if n:
                self._name_cache[n] = b
            i = b.get('id')
            if i:
                self._id_cache[i] = b
        for t in self.things:
            n = t.properties.get('name')
            if n:
                self._name_cache[n] = t
            i = t.properties.get('id')
            if i:
                self._id_cache[i] = t

        # PERF: precomputed trigger-brush list + bid lookup for _handle_triggers
        self._trigger_brushes = [
            (b.get('id') or i, b) for i, b in enumerate(self.brushes) if b.get('is_trigger')
        ]
        self._trigger_brush_by_bid = dict(self._trigger_brushes)
        self._refresh_use_triggers()

        # Props are not cached here. PropSession is the registry for the Prop
        # domain and a second list would be a competing copy of it; this is the
        # point at which it re-derives itself from the thing list, alongside
        # every other entity cache, and the engine reads Props back off it.
        if self._props is not None:
            self._props.rebuild(self.things)
        self._levelchanger_things = [t for t in self.things if LevelChanger and isinstance(t, LevelChanger)]
        self._refresh_levelchanger_table()

        # PERF: precomputed monster list + id lookup, used by MonsterAI so it
        # doesn't have to isinstance-scan the full (brushes+things) list of
        # every entity in the level on every AI tick.
        self._monster_things = [t for t in self.things if MonsterThing and isinstance(t, MonsterThing)]
        self._monster_by_id = {id(t): t for t in self._monster_things}
        # AI state of monsters that have left the world: keyed by id(), so a
        # monster spawned into a freed address would inherit it.
        live = self._monster_by_id
        with self._monster_lock:
            states = self.monster_ai.monster_states
            for key in [key for key in states if key not in live]:
                del states[key]

        # PERF: the timer list, for the same reason — _update_logic_timers is
        # the one per-frame path the logic system has, and it should walk the
        # timers, not the level.
        self._timer_things = [t for t in self.things if LogicTimer and isinstance(t, LogicTimer)]

        # The row sets this index describes; see _watch_world_rows.
        self._indexed_things = tuple(self.things)
        self._indexed_brushes = tuple(self.brushes)
        # Door/mover state is keyed by brush index. Whoever changed the brush
        # list -- the editor, or a console delete that rebuilds this index
        # itself -- the states are re-keyed here: the row watcher compares
        # against _indexed_brushes, which the line above has just moved on.
        if (self.play_mode and self._moving_rows is not None
                and self._indexed_brushes != self._moving_rows):
            self._reindex_moving_brushes()
            self.mark_collision_dirty()
        self._rebuild_portal_links()

    def _portal_runtime(self):
        """Return the portal runtime subsystem."""
        return self.portal_runtime

    def _rebuild_portal_links(self):
        """Rebuild cached portal target/slot relations."""
        return self._portal_runtime().rebuild_links()

    def _find_entity_by_name(self, name: str):
        if not name:
            return None
        if not self.play_mode:
            return self._scan_entity('name', name)
        return self._name_cache.get(name)

    def _find_entity_by_id(self, entity_id: str):
        if not entity_id:
            return None
        if not self.play_mode:
            return self._scan_entity('id', entity_id)
        return self._id_cache.get(entity_id)

    def _scan_entity(self, key, value):
        """Look an entity up in the live world, outside a play session.

        The caches are built when Play starts. The console's ``ent_fire``,
        ``send`` and ``trigger`` dispatch through the same I/O manager in the
        editor, where the caches are empty (never played) or hold the objects
        of the last session -- replaced by a restore or a map load -- so an
        input either failed with "not found" or landed on an object no longer
        in the world. Same precedence as the cache build: last one wins,
        entities over brushes.
        """
        for thing in reversed(self.things):
            if thing.properties.get(key) == value:
                return thing
        for brush in reversed(self.brushes):
            if brush.get(key) == value:
                return brush
        return None

    def _find_path_node_by_name(self, name: str):
        """Return PathNode thing with given name, or None."""
        if not name or PathNode is None:
            return None
        entity = self._name_cache.get(name)
        if entity is not None and isinstance(entity, PathNode):
            return entity
        for t in self.things:
            if isinstance(t, PathNode) and t.properties.get('name', '') == name:
                return t
        return None

    def _angled_brush_is_solid(self, brush):
        """Which angled brushes get solid mesh collision.

        Mirrors the player's own collision-skip logic (hidden / water / fog /
        trigger volumes are non-solid) and excludes movers/doors, which keep
        their existing dynamic AABB path.  Non-solid angled brushes simply fall
        through to the default AABB handling — a water/trigger volume never
        blocks the player, angled or not.
        """
        if brush.get('hidden') or brush.get('is_fog'):
            return False
        if is_water_brush(brush):
            return False
        if brush.get('operation') == 'subtract':
            return False
        if brush.get('is_mover') or brush.get('is_door'):
            return False
        if brush.get('is_trigger'):
            return False
        return True

    def _prepare_angled_brush_collision(self):
        """Attach swept-mesh collision to angled (clipped/convex) brushes.

        Angled brushes carry a ``geometry`` plane set instead of a plain box, so
        they can't collide as an AABB.  Here we bake each solid angled brush into
        world-space collision triangles and flag it ``_collision_mode='mesh'`` —
        the exact format the player's collide-and-slide path already uses for
        models — so ramps and wedges collide correctly and you can walk up
        slopes.  Box brushes are left untouched and keep the fast AABB path.

        Runs at play start; results are private keys stripped on save.
        """
        count = 0
        for brush in self.brushes:
            if not brush_has_geometry(brush):
                continue
            if not self._angled_brush_is_solid(brush):
                # Ensure a previously-solid brush that became non-solid loses
                # its stale mesh flag.
                self._clear_brush_collision(brush)
                continue
            if build_collision_mesh(brush):
                count += 1
            else:
                # Degenerate geometry — fall back to AABB rather than break.
                self._clear_brush_collision(brush)
        if count:
            debug_log("Collision", f"Prepared mesh collision for {count} angled brush(es)")
        return count

    #: What build_collision_mesh attaches, and all a revert may remove. The
    #: rest of GEO_RUNTIME_KEYS is the brush's geometry identity and cache:
    #: popping ``_geo_epoch`` gave every angled brush a new epoch behind the
    #: render tables' back at each play start/stop, so their rows held stale
    #: records and every convex shape was re-derived.
    _COLLISION_KEYS = ('_collision_mode', '_mesh_triangles', '_mesh_bounds',
                       '_mesh_planes')

    @classmethod
    def _clear_brush_collision(cls, brush):
        """Strip runtime mesh-collision keys so the brush reverts to AABB."""
        for k in cls._COLLISION_KEYS:
            brush.pop(k, None)

    def _clear_angled_brush_collision(self):
        """Remove play-time mesh-collision data from all angled brushes."""
        for brush in self.brushes:
            if brush_has_geometry(brush):
                self._clear_brush_collision(brush)

    def _build_model_collision_brushes(self):
        """Create collision data for model entities.

        Props can explicitly choose Automatic, AABB, or Mesh collision. A
        non-zero collision_size always overrides the shape choice with a
        custom AABB.
        """
        model_collision_enabled = bool(getattr(self, 'model_collision_enabled', True))
        brushes = []

        for thing in self.things:
            props = getattr(thing, 'properties', {})
            if not props.get('model_path'):
                continue
            physics_enabled = bool(props.get('physics_enabled', False))
            if not model_collision_enabled and not physics_enabled:
                continue
            if props.get('no_collision', False) and not physics_enabled:
                continue

            pos = getattr(thing, 'pos', [0, 0, 0])
            if hasattr(pos, 'x'):
                pos = [pos.x, pos.y, pos.z]
            else:
                pos = list(pos)

            scale = props.get('scale', 1.0)
            if isinstance(scale, (int, float)):
                scale = [scale, scale, scale]
            else:
                scale = list(scale)

            rot = props.get('rotation', [0, 0, 0])
            is_physics_body = physics_enabled
            collision_shape = str(
                props.get('collision_shape', 'auto')
            ).lower()

            # A non-zero explicit collision_size always forces a custom AABB.
            collision_size = props.get('collision_size')
            has_collision_size = (
                isinstance(collision_size, (list, tuple))
                and len(collision_size) == 3
                and any(float(v) != 0.0 for v in collision_size)            )

            if has_collision_size:
                size = list(collision_size)
                brushes.append({
                    'pos': pos,
                    'size': size,
                    'hidden': False,
                    'is_trigger': False,
                    'is_mover': False,
                    'is_door': False,
                    'is_water': False,
                    'is_fog': False,
                    '_model_collision': True,
                    '_physics_entity': thing,
                    '_physics_body': is_physics_body,
                    '_collision_mode': 'aabb',
                })
                continue

            # A Prop drawn as a sprite has no model-shaped collision.
            # ``model_path`` alone decides whether this loop looks at a Thing,
            # which is right for a Model entity but wrong for a Prop: a Prop
            # keeps its mesh path when its representation is switched back to
            # Billboard, and would otherwise collide as a mesh nobody can see.
            # An explicit collision_size still applies -- that is authored for
            # the entity, not derived from the model -- and is handled above.
            if str(props.get('render_mode', 'model')).lower() == 'billboard':
                continue

            model_path = props.get('model_path', '')

            # Explicit AABB mode skips mesh loading and always uses the model's
            # scaled bounds. This is useful for barrels, bricks and other props
            # where a stable box is preferable to triangle-level collision.
            if collision_shape == 'aabb':
                bounds = self._compute_model_bounds(model_path)
                if bounds:
                    min_v, max_v = bounds
                    size = [
                        (max_v[i] - min_v[i]) * scale[i]
                        for i in range(3)
                    ]
                    local_centre = [
                        (min_v[i] + max_v[i]) * 0.5
                        for i in range(3)
                    ]
                    aabb_pos = [
                        pos[i] + local_centre[i] * scale[i]
                        for i in range(3)
                    ]
                else:
                    base = 64.0
                    size = [base * scale[i] for i in range(3)]
                    aabb_pos = pos

                brushes.append({
                    'pos': aabb_pos,
                    'size': size,
                    'hidden': False,
                    'is_trigger': False,
                    'is_mover': False,
                    'is_door': False,
                    'is_water': False,
                    'is_fog': False,
                    '_model_collision': True,
                    '_physics_entity': thing,
                    '_physics_body': is_physics_body,
                    '_collision_mode': 'aabb',
                })
                continue

            # Automatic uses mesh collision where supported. Explicit Mesh
            # behaves the same today and falls back to AABB if the model cannot
            # provide mesh collision.
            mesh_tris = self._compute_model_collision_mesh(
                model_path, pos, scale, rot
            )
            if mesh_tris and collision_shape in ('auto', 'mesh'):
                brushes.append({
                    'pos': pos,
                    'size': [1, 1, 1],
                    'hidden': False,
                    'is_trigger': False,
                    'is_mover': False,
                    'is_door': False,
                    'is_water': False,
                    'is_fog': False,
                    '_model_collision': True,
                    '_physics_entity': thing,
                    '_physics_body': is_physics_body,
                    '_collision_mode': 'mesh',
                    '_mesh_triangles': mesh_tris,
                    '_mesh_bounds': self._compute_mesh_bounds(mesh_tris),
                })
                continue

            # Fallback for Automatic/Mesh when the model has no CPU collision
            # mesh (for example OBJ today).
            bounds = self._compute_model_bounds(model_path)
            if bounds:
                min_v, max_v = bounds
                size = [
                    (max_v[i] - min_v[i]) * scale[i]
                    for i in range(3)
                ]
                local_centre = [
                    (min_v[i] + max_v[i]) * 0.5
                    for i in range(3)
                ]
                aabb_pos = [
                    pos[i] + local_centre[i] * scale[i]
                    for i in range(3)
                ]
            else:
                base = 64.0
                size = [base * scale[i] for i in range(3)]
                aabb_pos = pos

            brushes.append({
                'pos': aabb_pos,
                'size': size,
                'hidden': False,
                'is_trigger': False,
                'is_mover': False,
                'is_door': False,
                'is_water': False,
                'is_fog': False,
                '_model_collision': True,
                '_physics_entity': thing,
                '_physics_body': is_physics_body,
                '_collision_mode': 'aabb',
            })

        return brushes

    def _compute_model_collision_mesh(self, model_path, world_pos, scale, rotation):
        """Load model and return world-space triangles for collision.

        Uses GLBLoader (CPU-only, no OpenGL calls) so this is safe to call from
        any thread regardless of whether a GL context is current.  The old path
        used GLB which called glGenVertexArrays/glGenBuffers and would silently
        fail when the GL context was not active on this thread.
        """
        if not model_path:
            return None

        full_path = os.path.join('assets', 'models', model_path)
        if not os.path.exists(full_path):
            full_path = model_path
        if not os.path.exists(full_path):
            return None

        ext = os.path.splitext(model_path)[1].lower()
        if ext != '.glb':
            return None  # Only GLB supports mesh collision for now

        try:
            # GLBLoader is pure file I/O + JSON parsing — zero OpenGL calls.
            from .glb_loader import GLBLoader

            loader = GLBLoader()
            loader._filepath_hint = full_path
            if not loader.load(full_path):
                debug_log("Collision", f"GLBLoader failed to load {model_path}")
                return None

            all_verts = loader.get_flattened_vertices()   # list of (x, y, z)
            all_tris  = loader.get_flattened_triangles()  # list of (i0, i1, i2)

            if not all_verts or not all_tris:
                debug_log("Collision", f"No geometry in {model_path}")
                return None

            # Build rotation matrix from euler angles (YXZ order, matching renderer)
            yaw, pitch, roll = (math.radians(rotation[1]),
                                math.radians(rotation[0]),
                                math.radians(rotation[2]))
            cy, sy = math.cos(yaw),   math.sin(yaw)
            cp, sp = math.cos(pitch), math.sin(pitch)
            cr, sr = math.cos(roll),  math.sin(roll)

            def transform_point(x, y, z):
                # Scale
                x, y, z = x * scale[0], y * scale[1], z * scale[2]
                # Rotate Y (yaw)
                x, z = x * cy - z * sy, x * sy + z * cy
                # Rotate X (pitch)
                y, z = y * cp - z * sp, y * sp + z * cp
                # Rotate Z (roll)
                x, y = x * cr - y * sr, x * sr + y * cr
                # Translate to world
                return (x + world_pos[0], y + world_pos[1], z + world_pos[2])

            world_tris = []
            for i0, i1, i2 in all_tris:
                if i0 >= len(all_verts) or i1 >= len(all_verts) or i2 >= len(all_verts):
                    continue
                w0 = transform_point(*all_verts[i0])
                w1 = transform_point(*all_verts[i1])
                w2 = transform_point(*all_verts[i2])

                # Compute face normal from world-space edge vectors
                e1 = (w1[0]-w0[0], w1[1]-w0[1], w1[2]-w0[2])
                e2 = (w2[0]-w0[0], w2[1]-w0[1], w2[2]-w0[2])
                nx = e1[1]*e2[2] - e1[2]*e2[1]
                ny = e1[2]*e2[0] - e1[0]*e2[2]
                nz = e1[0]*e2[1] - e1[1]*e2[0]
                length = math.sqrt(nx*nx + ny*ny + nz*nz)
                w_normal = (nx/length, ny/length, nz/length) if length > 0.001 else (0.0, 1.0, 0.0)

                world_tris.append(((w0, w1, w2), w_normal))

            debug_log("Collision", f"Built {len(world_tris)} mesh-collision tris for {model_path}")
            return world_tris if world_tris else None

        except Exception as e:
            debug_log("Collision", f"Failed to build mesh collision for {model_path}: {e}")
            return None

    def _compute_mesh_bounds(self, mesh_tris):
        """Compute AABB from mesh triangles for broad-phase culling."""
        if not mesh_tris:
            return None
        all_verts = []
        for (v0, v1, v2), _ in mesh_tris:
            all_verts.extend([v0, v1, v2])
        min_v = [min(v[i] for v in all_verts) for i in range(3)]
        max_v = [max(v[i] for v in all_verts) for i in range(3)]
        return (min_v, max_v)

    def _compute_model_bounds(self, model_path):
        """Compute axis-aligned bounds from a model file. Returns (min, max) or None."""
        if not model_path:
            return None

        full_path = os.path.join('assets', 'models', model_path)
        if not os.path.exists(full_path):
            full_path = model_path
        if not os.path.exists(full_path):
            return None

        ext = os.path.splitext(model_path)[1].lower()
        try:
            if ext == '.glb':
                from .glb_loader import GLBLoader
                loader = GLBLoader()
                loader._filepath_hint = full_path
                if loader.load(full_path):
                    verts = loader.get_flattened_vertices()
                else:
                    verts = None
            elif ext == '.obj':
                # OBJ collision only needs CPU geometry.  Do not instantiate the
                # OpenGL-backed OBJ model on the logic thread.
                from .obj_loader import OBJLoader
                loader = OBJLoader()
                if loader.load(full_path):
                    source_vertices = np.asarray(loader.vertices, dtype=np.float32)
                    if source_vertices.size == 0:
                        verts = None
                    else:
                        source_min = source_vertices.min(axis=0)
                        source_max = source_vertices.max(axis=0)
                        source_centre = (source_min + source_max) * 0.5
                        half_extent = (source_max - source_min) * 0.5
                        threshold = np.maximum(half_extent * 4.0, 2.0)
                        offset = np.where(
                            np.abs(source_centre) > threshold,
                            source_centre,
                            0.0,
                        ).astype(np.float32)
                        corrected = source_vertices - offset
                        verts = corrected.tolist()
                else:
                    verts = None
            else:
                return None

            if verts:
                min_v = [min(v[i] for v in verts) for i in range(3)]
                max_v = [max(v[i] for v in verts) for i in range(3)]
                return min_v, max_v
        except Exception as e:
            debug_log("Collision", f"Failed to compute {ext.upper()} bounds for {model_path}: {e}")
        return None

    def toggle_model_collision(self, enabled: bool = None) -> bool:
        """Toggle model collision on/off. If enabled is None, flip current state.
        Returns the new state. Works in both play mode and editor mode."""
        if enabled is None:
            self.model_collision_enabled = not self.model_collision_enabled
        else:
            self.model_collision_enabled = bool(enabled)

        # Rebuild collision brushes in both play mode and editor mode
        # (editor mode uses them for visualization via showcollision command)
        if self.model_collision_enabled:
            self._model_collision_brushes = self._build_model_collision_brushes()
            self._physics_body_brushes = [
                b for b in self._model_collision_brushes
                if b.get('_physics_body')
            ]
            if self.play_mode and hasattr(self, '_spatial_grid') and self._spatial_grid:
                self._spatial_grid.populate(self.brushes + self._model_collision_brushes)
                if getattr(self, '_physics_world', None) is not None:
                    self._physics_world.rebuild(self._physics_body_brushes)
        else:
            self._model_collision_brushes = []
            if self.play_mode and hasattr(self, '_spatial_grid') and self._spatial_grid:
                self._spatial_grid.populate(self.brushes)
                if getattr(self, '_physics_world', None) is not None:
                    self._physics_world.rebuild(self._physics_body_brushes)
        self._refresh_collision_brushes_cache()

        return self.model_collision_enabled

    def _refresh_collision_brushes_cache(self):
        """Recompute the combined static+model collision brush list.

        PERF: `self.brushes + self._model_collision_brushes` was previously
        rebuilt (a full list concatenation) every single tick — and, worse,
        once per active projectile per tick. Both collections only change
        here (model-collision toggle, play-mode enter/exit), so cache the
        concatenation and reuse it from the hot paths instead.
        """
        self._collision_brushes_cache = self.brushes + self._model_collision_brushes

    # -- visibility invalidation ------------------------------------------
    #
    # Two notifications, because "what is drawn" and "what is collided with"
    # go stale at different costs.  Both are the *host* side of the streaming
    # contract in ``plugins.bigworld.runtime.StreamingHost``; neither knows
    # anything about a particular streaming layer.

    def notify_visibility_changed(self):
        """The set of drawable objects changed.

        Cheap by contract — a counter bump and the per-frame cull buffers —
        because a streaming layer calls it every time the player crosses a cell
        boundary.  It really is just the counter: parking writes `hidden` and
        leaves the brush in the list, so the render projection's row set has not
        changed, and `hidden` is read live every frame anyway
        (`engine.render_table.RenderTable.begin_frame`).  Nothing to rebuild.
        """
        self.visibility_changes += 1

    def notify_authored_visibility_changed(self):
        """An object's *authored* hidden/disabled state changed.

        The expensive one, and the one streaming must never need: parking
        stashes an object's authored ``hidden`` rather than overwriting it
        (``engine.spatial.authored_hidden``), precisely so the collision grid
        can outlive a cell going in and out.  An *authored* change is different
        — an editor edit, an I/O Show/Hide, a save being restored over the live
        world — and the grid is built from exactly that, once, so it has to be
        rebuilt or the world collides like the map it used to be.
        """
        self.notify_visibility_changed()
        self._refresh_collision_brushes_cache()
        grid = getattr(self, '_spatial_grid', None)
        if grid is not None:
            grid.populate(self._collision_brushes_cache)

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
            self._apply_play_mode(enabled)

    def _apply_play_mode(self, enabled: bool):
        self.play_mode = enabled
        # A pause belongs to the session that took it: a new session, or the
        # editor after one, never starts frozen by a request nobody released.
        with self._world_pause_lock:
            self._world_pause_owners = frozenset()
        # Likewise a camera ceiling: a session that sets one (Big World) sets
        # it again from its play-start hook, which runs after this.
        self.overhead_height_limit = None

        if enabled:
            # A new Play session clears any previous fault marker.
            self._tick_faulted = False
            self._gui_fault_teardown_requested = False
            self._tick_fault_message = ""
            # Read P2 turn sensitivity from editor config
            if hasattr(self.editor_state, 'config'):
                self.p2_turn_sensitivity = float(
                    self.editor_state.config.get('Controls', 'p2_turn_sensitivity', fallback=10.0)
                )
            self._init_movers()
            self._init_doors()
            self._init_parented_lights()
            self._init_parented_portals()

            # Bake swept-mesh collision for angled (clipped/convex) brushes so
            # they collide as real slopes/wedges.  Must run before the spatial
            # grid is populated below so the grid indexes them by their true
            # geometry bounds.
            self._prepare_angled_brush_collision()

            # Build collision brushes for model entities
            self._model_collision_brushes = self._build_model_collision_brushes()
            self._physics_body_brushes = [
                b for b in self._model_collision_brushes
                if b.get('_physics_body')
            ]
            self._refresh_collision_brushes_cache()


            # Reset dense Effect execution state for this Play Mode session.
            # Runtime phase, origin and active state stay out of authoring objects.
            self.effect_store.begin_session(self.things)

            # Reset player stats
            self.player_health = 100
            self.player_max_health = 100
            self.player_dead = False
            self.god_mode = False
            self.buddha_mode = False
            self.notarget = False
            
            # Reset collection state
            self._reset_trigger_state()
            self.collected_keys.clear()
            for thing in self.things:
                if PropThing and isinstance(thing, PropThing):
                    # Restores what the author set; forcing carry on here made
                    # every Prop -- scenery models included -- carryable.
                    thing.reset_collection()
            if self._props is not None:
                self._props.start()
            
            # Reset speaker state
            self.active_speakers.clear()
            self.hurt_trigger_timers.clear()
            self.current_hud_message = ""
            self.current_hud_key_name = None

            # Reset water sound state (no spurious enter/exit on spawn)
            self._player_was_in_water = False
            self._waterwalk_timer = 0.0

            # Reset gate inputs
            self.gate_inputs = {}
            
            # Reset timer states
            self.timer_states = {}
            
            # Reset active weapon / ammunition
            self.active_weapon = None
            self.player_ammo = 0
            self.gun2_obtained = False
            self._last_player_shot_time = float("-inf")
            
            # Reset visual fx
            self.bullet_marks = []
            self.muzzle_flash_active = False

            # Reset P2 stats
            self.player2_health = 100
            self.player2_max_health = 100
            self.player2_dead = False

            # Reset monster AI state (delegated)
            self._reset_all_monsters(clear_dead=True)
            
            # Reset I/O system
            if self.io_manager:
                self.io_manager.reset()
                for brush in self.brushes:
                    for conn in get_connections(brush):
                        conn.reset()
                for thing in self.things:
                    for conn in get_connections(thing):
                        conn.reset()
            
            # Build entity caches
            self._build_entity_caches()

            # Build spatial grid for fast collision queries (monsters + player)
            from .physics import SpatialGrid, PhysicsWorld
            self._spatial_grid = SpatialGrid(cell_size=512.0)
            self._spatial_grid.populate(self.brushes + self._model_collision_brushes)
            self._physics_world = PhysicsWorld(self._spatial_grid)
            self._physics_world.rebuild(self._physics_body_brushes)
            self.monster_ai.set_spatial_grid(self._spatial_grid)

            # The Prop session is the registry for the Prop domain, so it
            # exists for the whole play session and is filled by
            # _build_entity_caches below.  A map with no Props leaves it empty,
            # which costs an empty list and an empty dict.
            self._props = PropSession(self)
            self._props.start()

            # Reset cinematic state (mover_path_states already reset by _init_movers)
            self.cinematic_state = None
            self.camera_transition = None
            self._hud_cinematic_last_active = False
            self._hud_cinematic_fade_started = None

            # Start the health HUD hidden; player spawn uses the same fast
            # 1.5-second fade-in followed immediately by the 4-second fade-out.
            _hud_now = time.perf_counter()
            self._hud_health_alpha = 0.0
            self._hud_health_last_value = self.player_health
            self._hud_health_fade_started = _hud_now
            self._hud_health_fade_from = 0.0
            self._hud_health_fade_phase = "in"

                # Reset portal runtime state (transit + fade state).
            self._portal_runtime().reset_session()

            self.level_complete_ui = None

            # Reset light fade transitions for a clean play session, and drop
            # any cached fade "nominal" so intensity edits made in the editor
            # between sessions are picked up on the next FadeIn.
            self.light_fade_states.clear()
            if Light is not None:
                for _t in self.things:
                    if isinstance(_t, Light) and hasattr(_t, '_fade_nominal'):
                        del _t._fade_nominal

            # Clear monster projectiles
            self._monster_projectiles.clear()
            self._projectile_positions = _NO_PROJECTILES

            # Clear gunfire events
            self._gunfire_events.clear()

            # Fire OnPlayerSpawn
            self._fire_player_spawn_outputs()
            
            # Initialize timers that start on
            self._init_logic_timers()

            # Start monster AI thread
            self._start_monster_ai()
            
        else:
            if self.cinematic_state and self.cinematic_state.get('json_cutscene'):
                self._finish_json_cutscene(self.cinematic_state, fire_finished=False)
            self._stop_monster_ai()
            self._reset_trigger_state()
            self.fired_once_triggers.clear()
            self.collected_keys.clear()
            self.active_speakers.clear()
            self.hurt_trigger_timers.clear()
            self._reset_movers()
            self._reset_doors()
            self._reset_parented_lights()
            self._reset_parented_portals()
            self._clear_angled_brush_collision()
            self.current_hud_message = ""
            self.current_hud_key_name = None
            self.gate_inputs = {}
            self.timer_states = {}
            self.light_fade_states.clear()
            self.active_weapon = None
            self.bullet_marks = []
            self.player_dead = False
            self.muzzle_flash_active = False

            # Clear spatial grid. Guarded on the *value*, not on the attribute
            # existing: after one exit the attribute is present and None, so a
            # second stop (a teardown path, or Stop pressed twice) used to raise
            # AttributeError here and abandon the rest of the cleanup below.
            props = getattr(self, '_props', None)
            if props is not None:
                props.stop()
            self._props = None
            physics_world = getattr(self, '_physics_world', None)
            if physics_world is not None:
                physics_world.clear()
            self._physics_world = None
            self.monster_ai.set_spatial_grid(None)
            grid = getattr(self, '_spatial_grid', None)
            if grid is not None:
                grid.clear()
            self._spatial_grid = None

            # Reset mover path / cinematic state
            self.mover_path_states = {}
            self.cinematic_state = None
            self.camera_transition = None
            self._hud_cinematic_last_active = False
            self._hud_cinematic_fade_started = None
            self._hud_health_alpha = 0.5
            self._hud_health_last_value = None
            self._hud_health_fade_started = None
            self._hud_health_fade_from = 0.5
            self._hud_health_fade_phase = "idle"

                # Reset portal runtime state (transit + fade state).
            self._portal_runtime().reset_session()

            self.level_complete_ui = None

            # Clear monster projectiles
            self._monster_projectiles.clear()
            self._projectile_positions = _NO_PROJECTILES

            # Clear gunfire events
            self._gunfire_events.clear()

            # Reset monster AI state
            self._reset_all_monsters(clear_dead=False)
            self._release_session_caches()

        # Plugin play lifecycle: initialise per-session state on entering play,
        # tear it down on leaving. Runs after the core reset above so plugins
        # see a fully-prepared session.
        if self.plugins is not None:
            try:
                if enabled:
                    self.plugins.dispatch_play_start(self)
                else:
                    self.plugins.dispatch_play_stop(self)
            except Exception as exc:
                print(f"[LogicThread] plugin lifecycle dispatch failed: {exc}")
            self._plugin_emit("play_start" if enabled else "play_stop")

    # =========================================================================
    # SAVE / LOAD  (native play-session serialization)
    # =========================================================================

    def save_session(self, path: str, *, map_name: str = "",
                     save_mode: str = "full", base_level: dict = None):
        """Serialize the live play session to *path*. Returns ``(ok, message)``.

        Native counterpart to the editor's ``save`` / ``quicksave`` console
        commands. Requires an active play session — there is no live state to
        capture in editor mode. Builds a snapshot with :mod:`engine.savegame`
        (the whole level plus player transform, stats, cheat flags, collected
        keys and door/mover/monster state) and writes it as JSON.

        *save_mode* selects ``full`` / ``delta`` / ``both`` (see
        :mod:`engine.savegame`); ``delta``/``both`` also want *base_level*, the
        normalized original map to diff against. Both degrade to ``full`` when no
        base level is available, so a save is never lost.
        """
        if not self.play_mode:
            return False, "Nothing to save — not in play mode."
        try:
            from engine import savegame
            # Big World maps force a delta save: never a full world snapshot.
            # The live streaming session owns the persistent per-cell registry.
            session = getattr(self, "_bigworld", None)
            if session is not None and getattr(session, "streaming", False):
                with self._tick_lock:
                    session.commit_all()   # flush every cell, loaded or unloaded
                    snapshot = savegame.build_snapshot(
                        self, map_name=map_name,
                        world_mode=savegame.WORLD_MODE_BIGWORLD,
                        cell_deltas=session.serialize_registry(),
                        base_world=session.base_identity(map_name))
            else:
                with self._tick_lock:   # a consistent frame, not a torn one
                    snapshot = savegame.build_snapshot(
                        self, map_name=map_name, save_mode=save_mode,
                        base_level=base_level)
            savegame.write(path, snapshot)
            mode_used = snapshot.get("save_mode", "full")
            world = snapshot.get("world_mode")
            label = f"{mode_used}/{world}" if world else mode_used
            return True, (f"Saved play session to '{os.path.basename(path)}' "
                          f"({label})")
        except Exception as exc:
            return False, f"Save failed: {exc}"

    def load_session(self, path: str, *, map_name: str = "",
                     base_level: dict = None):
        """Restore a saved play session from *path* as an overlay on the live
        session. Returns ``(ok, message)``.

        Native counterpart to the editor's ``load`` / ``quickload`` console
        commands *when already in play mode*. The scene is not rebuilt — entity
        state is matched back by stable id — so this must run against the same
        map the save was taken on (the caller loads the map and enters play mode
        first when starting from the editor).

        The save mode (full / delta / both / legacy) is auto-detected from the
        file's metadata; *map_name* is the currently-loaded map, used to validate
        a delta's base map, and *base_level* that map as loaded (see
        :func:`engine.savegame.restore_delta`). Loading never prompts unless
        recovery is impossible.
        """
        if not self.play_mode:
            return False, "Enter play mode before loading a session."
        try:
            from engine import savegame
            data = savegame.read(path)
            with self._tick_lock:
                report = savegame.restore_auto(self, data, current_map_name=map_name,
                                               base_level=base_level)
            msg = f"Loaded play session from '{os.path.basename(path)}'"
            warning = report.get("warning")
            if warning:
                msg += f" — {warning}"
            return True, msg
        except FileNotFoundError:
            return False, f"Save file not found: {path}"
        except Exception as exc:
            return False, f"Load failed: {exc}"

    def _release_session_caches(self):
        """Drop every reference the finished session's caches hold.

        Everything here is rebuilt when Play starts (_build_entity_caches,
        _init_movers/_init_doors, the collision set). Kept past Stop, these
        lists pinned the session's objects -- after a restore-on-stop or a map
        load, objects no longer in the world -- and anything resolving through
        them reached those instead of the live ones. Outside play the entity
        finders read the live world (see :meth:`_scan_entity`).
        """
        self._name_cache = {}
        self._id_cache = {}
        self._indexed_things = ()
        self._indexed_brushes = ()
        self._moving_rows = None
        self._monster_by_id = {}
        self._monster_things = []
        self._timer_things = []
        self._levelchanger_things = []
        self._levelchanger_centres = np.empty((0, 3), dtype=np.float32)
        self._levelchanger_radii = np.empty(0, dtype=np.float32)
        self._levelchanger_eligible = np.empty(0, dtype=bool)
        self._trigger_brushes = []
        self._trigger_brush_by_bid = {}
        self._use_trigger_entries = []
        self._portal_things = []
        self._portal_target_things = []
        self._portal_slots = np.empty(0, dtype=np.int32)
        self._portal_target_slots = np.empty(0, dtype=np.int32)
        self._collision_brushes_cache = []
        self._model_collision_brushes = []
        self._physics_body_brushes = []
        self._mover_brush_list = []
        self._door_brush_list = []
        self._monster_spawn_health = {}
        if self.io_manager is not None:
            # Delayed events hold their connection; outputs queued from other
            # threads hold their source entity.
            self.io_manager.reset()
        for player in (self.player, getattr(self, 'player2', None)):
            if player is not None:
                player.ground_object = None
    def _start_monster_ai(self):
        """Start the monster AI processing thread."""
        self._stop_monster_ai()
        self.monster_ai_thread = MonsterAIThread(
            self, self.monster_ai, self._monster_lock, tick_rate=30
        )
        self.monster_ai_thread.start()

    def _stop_monster_ai(self):
        """Stop the monster AI thread and wait for it to finish.

        Joined, not just signalled: the caller is about to tear down or
        rebuild what ``MonsterAI.update`` reads (the spatial grid, the monster
        list), and an update still in flight would run against it.
        """
        thread = self.monster_ai_thread
        self.monster_ai_thread = None
        if thread is not None:
            thread.stop()
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2.0)

    def _reset_all_monsters(self, clear_dead=True):
        """Reset all monster AI state. Called when entering or exiting play mode.

        Also records each monster's authored health, keyed by UUID, so the
        Respawn input has something to restore to: the live ``health`` property
        is what damage mutates, so by the time a monster is dead the number the
        map authored is gone.  One dict filled during a pass that already walks
        every monster — no extra scan, and nothing new on the entity itself.
        """
        with self._monster_lock:
            self.monster_ai.forget_monsters()
        if not MonsterThing:
            return
        if clear_dead:
            self._monster_spawn_health = {}
        reset = []
        for thing in self.things:
            if not isinstance(thing, MonsterThing):
                continue
            reset.append(thing)
            if clear_dead:
                try:
                    self._monster_spawn_health[thing.properties.get('id')] = \
                        int(thing.properties.get('health', 100))
                except (TypeError, ValueError):
                    pass
            thing.properties.pop('is_shooting', None)
            thing.properties.pop('_vel_y', None)
            if clear_dead:
                thing.properties.pop('dead', None)
            triggered  = thing.properties.get('triggered', False)
            wake_sight = thing.properties.get('wake_on_sight', True)
            if triggered or wake_sight:
                thing.properties['awake'] = False
            else:
                thing.properties['awake'] = True
        # dead and is_shooting choose the sprite: without this a monster left
        # mid-shot, or dead, when play stopped kept that sprite in the editor.
        JOURNAL.record_many(reset, STATE)

    def _start_speakers_on_spawn(self):
        """Turn on speakers authored with Start On when the player spawns.

        This goes through the normal PlaySound input so speaker state,
        active-speaker bookkeeping, and OnSoundStarted outputs stay consistent
        with ordinary I/O-triggered playback.
        """
        if not self.io_manager or not Speaker:
            return
        for thing in self.things:
            if not isinstance(thing, Speaker):
                continue
            if not bool(thing.properties.get('play_on_start', False)):
                continue
            target_name = thing.properties.get('name', '')
            target_id = thing.properties.get('id', '')
            self.io_manager._execute_input(
                target_name,
                'PlaySound',
                '',
                'PlayerSpawn',
                target_id=target_id,
            )

    def _fire_player_spawn_outputs(self):
        if not self.io_manager:
            return

        # Start-on speakers initialise before the PlayerStart output chain, so
        # an explicit OnPlayerSpawn connection can override the authored state.
        self._start_speakers_on_spawn()

        if not PlayerStart:
            return
        for thing in self.things:
            if isinstance(thing, PlayerStart):
                self.io_manager.fire_output(thing, 'OnPlayerSpawn')
                self._plugin_emit("player_spawn", start=thing)
                break

    @staticmethod
    def _timer_key(thing):
        """Compatibility wrapper for stable timer identity."""
        return LogicTiming.timer_key(thing)

    def _init_logic_timers(self):
        """Compatibility wrapper for timer initialisation."""
        return self.timing_runtime.init_logic_timers()

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

    def _update_camera_transition(self, delta):
        self.camera.update_camera_transition(delta)

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

    def _init_movers(self):
        return self._mover_runtime()._init_movers()

    def _reset_movers(self):
        return self._mover_runtime()._reset_movers()

    def _init_doors(self):
        return self._mover_runtime()._init_doors()

    def _reset_doors(self):
        return self._mover_runtime()._reset_doors()

    def _trigger_door_open(self, door_idx: int, brush: dict):
        return self._mover_runtime()._trigger_door_open(door_idx, brush)

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
                            self._apply_play_mode(False)
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
                self._prepare_render_state()
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
        self._stop_monster_ai()

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
            self.notify_authored_visibility_changed()

    def _tick_editor_mode(self, delta: float):
        dx, dy = self.game_state.consume_mouse_delta()
        if dx != 0 or dy != 0:
            self._editor_mouselook_active = True
            self.editor_camera.yaw += dx * self.EDITOR_MOUSE_SENSITIVITY
            self.editor_camera.pitch -= dy * self.EDITOR_MOUSE_SENSITIVITY
            self.editor_camera.pitch = max(-89.0, min(89.0, self.editor_camera.pitch))
        
        keys = self.game_state.get_keys()
        yaw_rad = math.radians(self.editor_camera.yaw)
        forward = glm.vec3(math.cos(yaw_rad), 0, math.sin(yaw_rad))
        forward = glm.normalize(forward)
        right = glm.normalize(glm.cross(forward, glm.vec3(0, 1, 0)))
        up = glm.vec3(0, 1, 0)
        
        move_dir = glm.vec3(0, 0, 0)
        if Key_W in keys: move_dir += forward
        if Key_S in keys: move_dir -= forward
        if Key_A in keys: move_dir -= right
        if Key_D in keys: move_dir += right
        if Key_Space in keys: move_dir += up
        if Key_C in keys: move_dir -= up
        
        if glm.length(move_dir) > 0.001:
            move_dir = glm.normalize(move_dir)
            speed = self.EDITOR_CAMERA_SPEED
            if Key_Shift in keys:
                speed *= self.EDITOR_CAMERA_FAST_MULT
            self.editor_camera.pos += move_dir * speed * delta

    #: Ticks to keep comparing the world's row sets after an editor edit.
    _ROW_WATCH_TICKS = 30
    _indexed_things = ()
    _indexed_brushes = ()
    _moving_rows = None
    _rows_epoch = None
    _rows_watch = 0

    def _watch_world_rows(self):
        """Re-index the session when the editor adds or removes objects.

        The session indexes the world when Play starts (_build_entity_caches)
        and the collision set with it. An object cloned, pasted, placed or
        deleted in the editor during play otherwise had no AI, no I/O name,
        or -- deleted -- kept being simulated and collided with. Every editor
        edit moves ``world_epoch``, so the row sets are compared only for a
        short while after one (tools checkpoint before they mutate): an
        integer compare per tick otherwise.
        """
        epoch = getattr(self.editor_state, 'world_epoch', None)
        if epoch != self._rows_epoch:
            self._rows_epoch = epoch
            self._rows_watch = self._ROW_WATCH_TICKS
        if not self._rows_watch:
            return
        self._rows_watch -= 1
        brushes_changed = tuple(self.brushes) != self._indexed_brushes
        if brushes_changed or tuple(self.things) != self._indexed_things:
            # Re-keys movers/doors and the collision set if brushes changed.
            self._build_entity_caches()

    def _reindex_moving_brushes(self):
        return self._mover_runtime()._reindex_moving_brushes()

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
        if self.cinematic_state or self.player_dead or self.level_complete_ui:
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
        self._watch_world_rows()

        if self._world_pause_owners:
            self._tick_paused_world(delta)
            return
        
        # Update movers & doors first (for platform carrying)
        self._update_movers(delta)
        self._update_doors(delta)
        self._update_parented_lights()
        self._update_parented_portals()
        
        # Update I/O system (delayed events)
        if self.io_manager:
            self.io_manager.update(delta)
        
        # Update logic timers
        self._update_logic_timers(delta)

        # Update light FadeIn/FadeOut transitions
        self._update_light_fades(delta)

        # ---- Camera transition (First Person <-> Overhead tween) ----
        # Advances even while a cinematic runs so a queued toggle resolves; it
        # only affects the view matrix when no cinematic is overriding it.        self._update_camera_transition(delta)

        # ---- Cinematic camera: suppress player input while active ----
        self._update_cinematic_camera(delta)
        if self.cinematic_state:
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
        self._player_runtime().update_primary(delta, keys, mouse_dx, mouse_dy)
        self._update_water_sounds(delta)

        # Gameplay
        self._handle_interactions(use_key)
        if self._props is not None:
            self._props.tick(delta, use_key)
        physics_world = getattr(self, '_physics_world', None)
        if physics_world is not None:
            physics_world.step(delta, self.player)
            # Physics owned those positions for the duration of the step; the
            # Prop domain takes its index back into line now that it is over.
            if self._props is not None:
                self._props.sync_physics_positions()

        self._handle_triggers(use_key, delta)

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
        self._update_portals(delta)
        
        # Player shooting
        if self.game_state.consume_shot():
            self._handle_shooting()
            
        self._update_bullet_marks()

        # Clean up expired gunfire sound events (keep for 3 seconds)
        current_time = time.perf_counter()
        self._gunfire_events = [
            e for e in self._gunfire_events
            if (current_time - e['time']) < 3.0
        ]

        # Update monster projectiles (flying monster ranged attacks)
        # NOTE: Monster AI itself now runs in MonsterAIThread
        self._update_monster_projectiles(delta)

        # ── Player 2 physics (split-screen) ──────────────────────────────────
        self._player_runtime().update_player2(delta)

    # =========================================================================
    # WATER SOUNDS
    # =========================================================================

    def _player_runtime(self):
        """Return the player runtime, creating it for lightweight test doubles."""
        runtime = getattr(self, "player_runtime", None)
        if runtime is None:
            from .logic_player import LogicPlayer
            runtime = LogicPlayer(self)
            self.player_runtime = runtime
        return runtime

    # Keep the historical helper as a forwarding surface for tests/tools.
    def _update_water_sounds(self, delta: float):
        return self._player_runtime().update_water_sounds(delta)

    # =========================================================================
    # PORTAL TRANSIT
    # =========================================================================

    def note_player_teleported(self):
        """Invalidate the previous portal segment after a non-portal teleport."""
        return self._portal_runtime().note_player_teleported()

    def _update_portals(self, delta: float):
        """Advance portal fade/transit runtime for one simulation tick."""
        return self._portal_runtime().update(delta)

    @staticmethod
    def _segment_crosses_aperture(portal, prev, cur):
        """Compatibility wrapper for the portal crossing predicate."""
        return LogicPortals._segment_crosses_aperture(portal, prev, cur)

    def _execute_portal_transit(self, portal_a, portal_b):
        """Compatibility wrapper for player portal transit."""
        return self._portal_runtime()._execute_player_transit(portal_a, portal_b)

    def _transit_projectile_through_portals(self, projectiles, index, prev_pos):
        """Compatibility wrapper for dense projectile portal transit."""
        return self._portal_runtime().transit_projectile_through_portals(
            projectiles, index, prev_pos
        )

    # =========================================================================
    # LOGIC TIMER UPDATE
    # =========================================================================
    
    def _update_logic_timers(self, delta: float):
        """Compatibility wrapper for logic_timer advancement."""
        return self.timing_runtime.update_logic_timers(delta)

    def _update_light_fades(self, delta: float):
        """Compatibility wrapper for light FadeIn/FadeOut advancement."""
        return self.timing_runtime.update_light_fades(delta)

    # =========================================================================
    # TRIGGER HANDLING
    # =========================================================================

    def _trigger_runtime(self):
        """Return the trigger subsystem, creating it for lightweight test doubles."""
        runtime = getattr(self, "trigger_runtime", None)
        if runtime is None:
            runtime = LogicTriggers(self)
            self.trigger_runtime = runtime
        return runtime

    def _reset_trigger_state(self):
        return self._trigger_runtime()._reset_trigger_state()

    @staticmethod
    def _trigger_filters(brush):
        return LogicTriggers._trigger_filters(brush)

    @staticmethod
    def use_trigger_contains(distance_sq, use_radius):
        return LogicTriggers.use_trigger_contains(distance_sq, use_radius)

    def _refresh_use_triggers(self):
        return self._trigger_runtime()._refresh_use_triggers()

    def _use_prompt_candidates(self):
        return self._trigger_runtime()._use_prompt_candidates()

    def _sample_use_prompt(self):
        return self._trigger_runtime()._sample_use_prompt()

    def _trigger_poll_interval(self, brush):
        return self._trigger_runtime()._trigger_poll_interval(brush)

    def _poll_triggers(self, use_key_pressed=False, trigger_ids=None):
        return self._trigger_runtime()._poll_triggers(
            use_key_pressed=use_key_pressed, trigger_ids=trigger_ids
        )

    def _handle_triggers(self, use_key_pressed: bool, delta=None):
        return self._trigger_runtime()._handle_triggers(use_key_pressed, delta)

    def _on_trigger_enter(self, brush, trigger_id, activator_type='player',
                          activator_entity=None):
        return self._trigger_runtime()._on_trigger_enter(
            brush, trigger_id, activator_type=activator_type,
            activator_entity=activator_entity
        )

    def _on_trigger_exit(self, brush, trigger_id, activator_type='player',
                         activator_entity=None):
        return self._trigger_runtime()._on_trigger_exit(
            brush, trigger_id, activator_type=activator_type,
            activator_entity=activator_entity
        )

    def _process_hurt_trigger(self, brush, trigger_id, poll_interval=1.0):
        return self._trigger_runtime()._process_hurt_trigger(
            brush, trigger_id, poll_interval
        )

    def _apply_player_damage(self, damage):
        return self._trigger_runtime()._apply_player_damage(damage)

    # =========================================================================
    # INTERACTIONS
    # =========================================================================

    def _refresh_levelchanger_table(self):
        """Pack LevelChanger activation geometry into dense numeric columns."""
        things = getattr(self, '_levelchanger_things', ())
        count = len(things)
        if not count:
            self._levelchanger_centres = np.empty((0, 3), dtype=np.float32)
            self._levelchanger_radii = np.empty(0, dtype=np.float32)
            self._levelchanger_eligible = np.empty(0, dtype=bool)
            return

        self._levelchanger_centres = np.asarray(
            [thing.pos for thing in things],
            dtype=np.float32,
        ).reshape(count, 3)
        self._levelchanger_radii = np.asarray(
            [float(thing.properties.get('radius', 128.0)) for thing in things],
            dtype=np.float32,
        )
        self._levelchanger_eligible = np.asarray(
            [
                not thing.properties.get('disabled', False)
                and thing.properties.get('usable', True)
                for thing in things
            ],
            dtype=bool,
        )

    def _handle_interactions(self, use_key_pressed: bool):
        self.current_hud_message = ""
        self.current_hud_key_name = None
        reach_distance = 80.0
        px, py, pz = self.player.pos
        
        found_door_idx = -1
        found_door_brush = None
        for i, brush in self.doors:
            pos = brush['pos']
            size = brush['size']
            dx = abs(pos[0] - px)
            dy = abs(pos[1] - py)
            dz = abs(pos[2] - pz)
            if (dx < size[0]/2 + reach_distance and 
                dz < size[2]/2 + reach_distance and 
                dy < size[1]/2 + 64):
                found_door_idx = i
                found_door_brush = brush
                break

        door_consumed_use = False
        if found_door_brush:
            door_state = self.door_states.get(found_door_idx, {}).get('state', 'closed')

            if door_state == 'closed':
                if found_door_brush.get('door_auto_open', False):
                    is_locked = found_door_brush.get('door_locked', False)
                    needs_key = found_door_brush.get('door_needs_key', False)
                    if not is_locked and not needs_key:
                        self._trigger_door_open(found_door_idx, found_door_brush)
                else:
                    is_locked = found_door_brush.get('door_locked', False)
                    needs_key = found_door_brush.get('door_needs_key', False)
                    key_name = found_door_brush.get('door_key_name', '')
                    
                    if is_locked:
                        self.current_hud_message = "Locked"
                        if use_key_pressed and self.io_manager:
                            self.io_manager.fire_output(found_door_brush, 'OnLockedUse')
                        door_consumed_use = use_key_pressed
                    elif needs_key:
                        has_key = key_name in self.collected_keys
                        if has_key:
                            self.current_hud_message = "[E] Use"
                            self.current_hud_key_name = key_name or None
                            if use_key_pressed:
                                self._trigger_door_open(found_door_idx, found_door_brush)
                                door_consumed_use = True
                        else:
                            self.current_hud_message = "Need"
                            self.current_hud_key_name = key_name or None
                    else:
                        self.current_hud_message = "[E] Open"
                        if use_key_pressed:
                            self._trigger_door_open(found_door_idx, found_door_brush)
                            door_consumed_use = True


        if not door_consumed_use and self._levelchanger_things:
            # Radius activation is squared, removing the old per-entry
            # glm.distance() sqrt. Facing is also tested without per-row
            # normalisation: forward_dot > 0 and forward_dot² > 0.25*d².
            centres = getattr(self, '_levelchanger_centres', None)
            radii = getattr(self, '_levelchanger_radii', None)
            eligible = getattr(self, '_levelchanger_eligible', None)
            if (
                centres is None
                or radii is None
                or eligible is None
                or len(centres) != len(self._levelchanger_things)
            ):
                self._refresh_levelchanger_table()
                centres = self._levelchanger_centres
                radii = self._levelchanger_radii
                eligible = self._levelchanger_eligible

            player_pos = np.asarray((px, py, pz), dtype=np.float32)
            offsets = centres - player_pos
            distance_sq = np.einsum('ij,ij->i', offsets, offsets)
            in_range = eligible & (distance_sq < radii * radii)

            if in_range.any():
                forward = np.asarray(
                    (math.sin(self.player.angle), 0.0, math.cos(self.player.angle)),
                    dtype=np.float32,
                )
                forward_dot = offsets @ forward
                facing = (
                    (forward_dot > 0.0)
                    & (forward_dot * forward_dot > (0.25 * distance_sq))
                )
                candidates = np.flatnonzero(in_range & facing)
                if candidates.size:
                    # Preserve authored list order: first matching row wins.
                    row = int(candidates[0])
                    thing = self._levelchanger_things[row]
                    self.current_hud_message = "[E] Complete Level"
                    if use_key_pressed:
                        target_map = thing.properties.get('target_map', '')
                        self.level_complete_ui = {
                            'active': True,
                            'target_map': target_map,
                            'title': 'Complete',
                            'button_text': 'Continue'
                        }
                        if self.io_manager:
                            self.io_manager.fire_output(thing, 'OnUse')
                    return

    # =========================================================================
    # MOVER/DOOR UPDATES
    # =========================================================================

    def _update_movers(self, delta: float):
        return self._mover_runtime()._update_movers(delta)

    def _update_mover_path(self, idx: int, brush: dict, delta: float):
        return self._mover_runtime()._update_mover_path(idx, brush, delta)

    def _update_doors(self, delta: float):
        return self._mover_runtime()._update_doors(delta)

    # =========================================================================
    # PARENTED ENTITY RUNTIME
    # =========================================================================

    def _parenting_runtime(self):
        """Return the parented light/portal runtime for this LogicThread."""
        runtime = getattr(self, "parenting_runtime", None)
        if runtime is None:
            runtime = LogicParenting(self, light_type=Light, portal_type=Portal)
            self.parenting_runtime = runtime
        return runtime

    def _init_parented_lights(self):
        return self._parenting_runtime()._init_parented_lights()

    def _reset_parented_lights(self):
        return self._parenting_runtime()._reset_parented_lights()

    def _update_parented_lights(self):
        return self._parenting_runtime()._update_parented_lights()

    def _init_parented_portals(self):
        return self._parenting_runtime()._init_parented_portals()

    def _reset_parented_portals(self):
        return self._parenting_runtime()._reset_parented_portals()

    def _update_parented_portals(self):
        return self._parenting_runtime()._update_parented_portals()

    # PLAYER SHOOTING
    # =========================================================================

    def _combat_runtime(self):
        """Return the combat runtime subsystem."""
        return self.combat_runtime

    def _handle_shooting(self):
        """Compatibility wrapper for player hitscan shooting."""
        return self._combat_runtime()._handle_shooting()

    def intersect_ray_aabb(self, origin, direction, box_min, box_max):
        """Compatibility wrapper for the ray/AABB intersection helper."""
        return self._combat_runtime().intersect_ray_aabb(
            origin, direction, box_min, box_max
        )

    def _update_bullet_marks(self):
        """Compatibility wrapper for bullet-mark ageing."""
        return self._combat_runtime()._update_bullet_marks()

    def _add_monster_projectile(self, pos, vel, owner_id, damage, lifetime):
        """Compatibility wrapper for dense monster projectile creation."""
        return self._combat_runtime()._add_monster_projectile(
            pos, vel, owner_id, damage, lifetime
        )

    def _projectile_monster_candidates(self, pos32, owners):
        """Compatibility wrapper for dense projectile/monster broad-phase."""
        return self._combat_runtime()._projectile_monster_candidates(
            pos32, owners
        )

    def _projectile_wall_candidates(self, pos32):
        """Compatibility wrapper for dense projectile/wall broad-phase."""
        return self._combat_runtime()._projectile_wall_candidates(pos32)

    def _publish_projectile_render_snapshot(self):
        """Compatibility wrapper for projectile render publication."""
        return self._combat_runtime()._publish_projectile_render_snapshot()

    def _update_monster_projectiles(self, delta: float):
        """Compatibility wrapper for projectile simulation dispatch."""
        return self._combat_runtime()._update_monster_projectiles(delta)

    def _update_monster_projectiles_scalar(self, projectiles, delta: float):
        """Compatibility wrapper for the scalar projectile path."""
        return self._combat_runtime()._update_monster_projectiles_scalar(
            projectiles, delta
        )

    def _update_monster_projectiles_dense(self, projectiles, delta: float):
        """Compatibility wrapper for the dense projectile path."""
        return self._combat_runtime()._update_monster_projectiles_dense(
            projectiles, delta
        )

    def _emit_noise_event(self, pos, source: str, loudness: float = 1.0):
        """Compatibility wrapper for player noise emission."""
        return self._combat_runtime()._emit_noise_event(
            pos, source, loudness
        )

    def get_recent_noise_events(self, max_age: float = 3.0) -> list:
        """Compatibility wrapper for monster-hearing noise queries."""
        return self._combat_runtime().get_recent_noise_events(max_age)

    def get_recent_gunfire_events(self, max_age: float = 3.0) -> list:
        """Compatibility wrapper for the legacy gunfire event query."""
        return self._combat_runtime().get_recent_gunfire_events(max_age)

    # =========================================================================
    # FRUSTUM CULLING
    # =========================================================================

    def _extract_frustum_planes(self, proj_view: glm.mat4):
        m = proj_view
        planes = []
        planes.append(self._normalize_plane(m[0][3] + m[0][0], m[1][3] + m[1][0], m[2][3] + m[2][0], m[3][3] + m[3][0]))
        planes.append(self._normalize_plane(m[0][3] - m[0][0], m[1][3] - m[1][0], m[2][3] - m[2][0], m[3][3] - m[3][0]))
        planes.append(self._normalize_plane(m[0][3] + m[0][1], m[1][3] + m[1][1], m[2][3] + m[2][1], m[3][3] + m[3][1]))
        planes.append(self._normalize_plane(m[0][3] - m[0][1], m[1][3] - m[1][1], m[2][3] - m[2][1], m[3][3] - m[3][1]))
        planes.append(self._normalize_plane(m[0][3] + m[0][2], m[1][3] + m[1][2], m[2][3] + m[2][2], m[3][3] + m[3][2]))
        planes.append(self._normalize_plane(m[0][3] - m[0][2], m[1][3] - m[1][2], m[2][3] - m[2][2], m[3][3] - m[3][2]))
        return planes

    def _normalize_plane(self, a, b, c, d):
        length = math.sqrt(a*a + b*b + c*c)
        if length < 1e-8:
            return (0, 0, 0, 0)
        return (a/length, b/length, c/length, d/length)

    def _aabb_in_frustum(self, planes, center, half_size):
        for plane in planes:
            a, b, c, d = plane
            px = center[0] + half_size[0] if a >= 0 else center[0] - half_size[0]
            py = center[1] + half_size[1] if b >= 0 else center[1] - half_size[1]
            pz = center[2] + half_size[2] if c >= 0 else center[2] - half_size[2]
            if a*px + b*py + c*pz + d < 0:
                return False
        return True

    def _aabb_in_frustum_batch(self, planes, centers, halves):
        """Vectorized equivalent of calling _aabb_in_frustum for every
        (center, half_size) pair. Returns a NumPy boolean array, True where
        the AABB is (at least partially) inside the frustum.

        PERF: replaces a per-brush, per-plane Python loop (thousands of
        scalar float ops per tick for a level with hundreds of brushes) with
        two NumPy matmuls over the whole brush batch and all six planes at
        once — no per-plane Python iteration or temporary-array allocation.
        """
        c = np.asarray(centers, dtype=np.float64).reshape(-1, 3)
        h = np.asarray(halves, dtype=np.float64).reshape(-1, 3)
        if c.size == 0:
            return np.ones(len(centers), dtype=bool)
        return self._aabb_in_frustum_bounds(planes, np.concatenate((c, h), axis=1))

    @staticmethod
    def _aabb_in_frustum_bounds(planes, bounds):
        """The frustum test over ``[centre | half]`` rows, as one product.

        Positive-vertex distance for every (box, plane) pair, branch-free:
        ``dot(n, c + sign(n)*h) + d == dot([n, |n|], [c, h]) + d``. One
        product, one compare and one reduction: each NumPy call on a big array
        releases and re-takes the GIL, and with the AI and UI threads running
        every re-take can wait, so the count of calls is what this is shaped
        by, as much as the arithmetic.

        Evaluated plane-major, ``(6, 6) x (6, N)``: the six per-plane results
        for a box are then six rows apart, and the reduction is five
        elementwise ANDs over contiguous rows. Box-major, ``.all(axis=1)``
        reduced six adjacent bytes at a time, which cost three times the
        product itself (24k rows: 0.97 ms, against 0.20 ms this way, for the
        same answers).
        """
        p = np.asarray(planes, dtype=np.float64)        # (6, 4)
        normals = p[:, :3]
        weights = np.concatenate((normals, np.abs(normals)), axis=1)   # (6, 6)
        return (weights @ bounds.T >= -p[:, 3:]).all(axis=0)

    # =========================================================================
    # RENDER STATE PREPARATION
    # =========================================================================

    def _update_hud_health_alpha(self, now: float) -> float:
        """Advance the health HUD fade state machine and return its alpha."""
        if not getattr(self, "hud_fade_enabled", True):
            self._hud_health_alpha = 1.0
            self._hud_health_fade_started = None
            self._hud_health_fade_from = 1.0
            self._hud_health_fade_phase = "idle"
            self._hud_health_last_value = self.player_health
            return self._hud_health_alpha

        def _sample(at):
            phase = self._hud_health_fade_phase
            if phase == "in":
                started = self._hud_health_fade_started
                if started is None:
                    self._hud_health_alpha = 1.0
                    self._hud_health_fade_from = 1.0
                    self._hud_health_fade_started = at
                    self._hud_health_fade_phase = "out"
                    return self._hud_health_alpha

                elapsed = max(0.0, at - started)
                # Treat the exact scheduled endpoint as the completed fade-in.
                # This keeps the state transition deterministic when callers sample
                # at ``started + duration`` and the perf-counter subtraction lands
                # one ULP below the nominal duration.
                if elapsed + 1e-12 < self._hud_health_fade_in_duration:
                    t = elapsed / self._hud_health_fade_in_duration
                    self._hud_health_alpha = (
                        self._hud_health_fade_from
                        + (1.0 - self._hud_health_fade_from) * t
                    )
                    return self._hud_health_alpha

                self._hud_health_alpha = 1.0
                self._hud_health_fade_from = 1.0
                self._hud_health_fade_phase = "out"
                out_elapsed = elapsed - self._hud_health_fade_in_duration
            elif phase == "out":
                started = self._hud_health_fade_started
                if started is None:
                    self._hud_health_alpha = 0.5
                    self._hud_health_fade_phase = "idle"
                    return self._hud_health_alpha
                out_elapsed = max(
                    0.0,
                    at - started - self._hud_health_fade_in_duration,
                )
            else:
                self._hud_health_alpha = 0.5
                return self._hud_health_alpha

            # Treat the exact end of the fade as a completed state before
            # normalising the duration.  This avoids a one-ULP floating-point
            # remainder leaving the state machine in "out" while alpha is
            # already at the idle value.
            if out_elapsed >= self._hud_health_fade_out_duration:
                self._hud_health_alpha = 0.5
                self._hud_health_fade_started = None
                self._hud_health_fade_phase = "idle"
                return self._hud_health_alpha

            t = max(
                0.0,
                min(
                    1.0,
                    out_elapsed / self._hud_health_fade_out_duration,
                ),
            )
            self._hud_health_alpha = 1.0 - (0.5 * t)
            return self._hud_health_alpha

        health = self.player_health
        health_changed = (
            self._hud_health_last_value is not None
            and health != self._hud_health_last_value
        )

        # A health change restarts the fast fade from the opacity that was
        # actually visible at the moment of the change. Sample the old phase
        # first; otherwise a second change during fade-out would incorrectly
        # restart from the stale alpha left by the previous call.
        if health_changed:
            _sample(now)
            self._hud_health_last_value = health
            self._hud_health_fade_started = now
            self._hud_health_fade_from = self._hud_health_alpha
            self._hud_health_fade_phase = "in"

        elif self._hud_health_last_value is None:
            self._hud_health_last_value = health

        return _sample(now)

    def _peer_render_dirty(self, own_dirty, peer_table, snapshot_epoch):
        """What changed since *peer_table*'s epoch, when a table must rebuild.

        Only asked when this buffer's own journal replay is a global rebuild
        (``own_dirty is None``); ``None`` when the peer is no help either.
        """
        if own_dirty is not None or peer_table is None:
            return None
        peer_epoch = getattr(peer_table, '_epoch', None)
        if peer_epoch is None:
            return None
        return self.editor_state.render_dirty_since(
            peer_epoch, through_epoch=snapshot_epoch)[1]

    def _prepare_render_state(self):
        started = time.perf_counter()
        write_state = self.game_state.get_write_state()
        write_state.is_play_mode = self.play_mode

        if self.play_mode and self.player:
            cs = self.cinematic_state
            if cs and 'cam_pos' in cs:
                cam_pos = glm.vec3(*cs['cam_pos'])
                cam_angle = cs.get('cam_angle', 0.0)
                cam_pitch = cs.get('cam_pitch', 0.0)
                # Deliberate cinematic convention: cam_angle/cam_pitch are
                # Camera yaw/pitch, not the Player's angle convention below.
                # JSON cutscenes store authored Camera yaw verbatim, and the
                # legacy PathNode path-facing producer retains its historical
                # player-angle convention. Do not "correct" this by adding a
                # pi/2 conversion here; that would silently change authored
                # cutscene camera orientation and legacy compatibility.
                direction = glm.vec3(
                    math.cos(cam_angle) * math.cos(cam_pitch),
                    math.sin(cam_pitch),
                    math.sin(cam_angle) * math.cos(cam_pitch),
                )
                view_matrix = glm.lookAt(cam_pos, cam_pos + direction, glm.vec3(0, 1, 0))
                write_state.player_pos = cam_pos
                write_state.player_angle = cam_angle
                write_state.player_pitch = cam_pitch
                fov = cs['fov'] if cs.get('fov') else 90.0
            else:
                player_pos = glm.vec3(self.player.pos.x, self.player.pos.y, self.player.pos.z)
                player_angle = self.player.angle
                player_pitch = self.player.pitch
                camera_height = self.player.camera_height
                ct = self.camera_transition
                if ct:
                    # Tween between First Person and Overhead. Both endpoints are
                    # rebuilt from the live player pose each frame, so the swoop
                    # tracks movement; smoothstep easing gives a soft in/out. The
                    # frustum planes below derive from this blended view_matrix,
                    # so culling stays correct throughout the transition.
                    dur = ct['duration']
                    t = 1.0 if dur <= 0.0 else max(0.0, min(1.0, ct['elapsed'] / dur))
                    t = t * t * (3.0 - 2.0 * t)  # smoothstep
                    a = self.camera._camera_for_mode(ct['from_overhead'], player_pos,
                                              player_angle, player_pitch, camera_height)
                    b = self.camera._camera_for_mode(ct['to_overhead'], player_pos,
                                              player_angle, player_pitch, camera_height)
                    cam_pos = a[0] + (b[0] - a[0]) * t
                    direction = a[1] + (b[1] - a[1]) * t
                    if glm.length(direction) < 1e-8:
                        direction = b[1]
                    direction = glm.normalize(direction)
                    up_vec = self.camera._safe_up(direction, a[2] + (b[2] - a[2]) * t)
                    view_matrix = glm.lookAt(cam_pos, cam_pos + direction, up_vec)
                    fov = a[3] + (b[3] - a[3]) * t
                elif self.is_overhead():
                    # Native top-down camera. The frustum planes below are built
                    # from this view_matrix, so overhead culling is correct; the
                    # up hint is horizontal, avoiding the straight-down lookAt
                    # degeneracy that would corrupt the view and every plane.
                    cam_pos, direction, up_vec = self.camera._overhead_camera(player_pos, player_angle)
                    view_matrix = glm.lookAt(cam_pos, cam_pos + direction, up_vec)
                    fov = self.frustum_fov
                else:
                    cam_pos = player_pos + glm.vec3(0, camera_height, 0)
                    direction = glm.vec3(
                        math.sin(player_angle) * math.cos(player_pitch),
                        math.sin(player_pitch),
                        math.cos(player_angle) * math.cos(player_pitch),
                    )
                    view_matrix = glm.lookAt(cam_pos, cam_pos + direction, glm.vec3(0, 1, 0))
                    fov = self.frustum_fov
                write_state.player_pos = player_pos
                write_state.player_angle = player_angle
                write_state.player_pitch = player_pitch
        else:
            write_state.editor_camera_pos = glm.vec3(self.editor_camera.pos)
            write_state.editor_camera_yaw = self.editor_camera.yaw
            write_state.editor_camera_pitch = self.editor_camera.pitch
            write_state.editor_camera_fov = self.editor_camera.fov
            view_matrix = self.editor_camera.get_view_matrix()
            fov = self.editor_camera.fov

        write_state.camera_view_matrix = view_matrix

        # LogicCamera owns the view while cinematic_state exists, including
        # paused cinematics. Publish HUD state so the render thread never needs
        # to inspect LogicThread directly.
        cinematic_active = bool(self.cinematic_state)
        now = time.perf_counter()
        if cinematic_active:
            self._hud_cinematic_last_active = True
            self._hud_cinematic_fade_started = None
            hud_alpha = 0.0
        elif self._hud_cinematic_last_active:
            self._hud_cinematic_last_active = False
            self._hud_cinematic_fade_started = now
            hud_alpha = 0.0
        elif self._hud_cinematic_fade_started is not None:
            hud_alpha = min(
                1.0, max(0.0, (now - self._hud_cinematic_fade_started) / 4.0)
            )
            if hud_alpha >= 1.0:
                self._hud_cinematic_fade_started = None
        else:
            hud_alpha = 1.0

        health_hud_alpha = self._update_hud_health_alpha(now)

        write_state.cinematic_camera_active = cinematic_active
        write_state.hud_alpha = hud_alpha
        write_state.hud_health_alpha = health_hud_alpha
        write_state.player_health = self.player_health
        write_state.player_max_health = self.player_max_health
        write_state.player_dead = self.player_dead
        write_state.player_ammo = max(0, int(getattr(self, "player_ammo", 0)))
        if self.play_mode and self.player and not self.cinematic_state:
            write_state.player_underwater = bool(getattr(self.player, 'eye_underwater', False))
            write_state.underwater_tint = list(getattr(self.player, 'water_tint', [0.0, 0.4, 0.6]))
        else:
            write_state.player_underwater = False
        write_state.collected_keys = set(self.collected_keys)
        write_state.hud_message = self.current_hud_message
        write_state.hud_prompt_key = self.current_hud_key_name
        write_state.active_weapon = self.active_weapon
        write_state.muzzle_flash_active = self.muzzle_flash_active
        if self.active_weapon == "gun1":
            write_state.shot_ready = True
        elif self.active_weapon == "gun2":
            now = time.perf_counter()
            try:
                ammo = max(0, int(getattr(self, "player_ammo", 0)))
            except (TypeError, ValueError):
                ammo = 0
            write_state.shot_ready = (
                ammo > 0
                and (now - float(getattr(
                    self, "_last_player_shot_time", float("-inf")
                ))) >= 1.0
            )
        else:
            write_state.shot_ready = False
        write_state.camera_transition_active = bool(self.camera_transition)

        if self.play_mode and getattr(self, '_monster_projectiles', None):
            self._publish_projectile_render_snapshot()
        else:
            self._projectile_positions = _NO_PROJECTILES
        write_state.projectiles = self._projectile_positions
        write_state.monster_debug_active = self.monster_ai.monster_debug_active
        write_state.monster_debug_rays = list(self.monster_ai._debug_rays)

        current_time = time.perf_counter()
        write_state.bullet_marks = [
            {'pos': [m['pos'].x, m['pos'].y, m['pos'].z],
             'alpha': max(0.0, 1.0 - (current_time - m['time']) / self.BULLET_FADE_TIME)}
            for m in self.bullet_marks
            if current_time - m['time'] < self.BULLET_FADE_TIME
        ]

        _far = self.view_distance.far_plane if self.view_distance is not None else 10000.0
        projection = glm.perspective(glm.radians(fov), self.frustum_aspect, 1.0, _far)
        proj_view = projection * view_matrix
        frustum_planes = self._extract_frustum_planes(proj_view)

        brushes = self.brushes

        # ---- T3: the dense render projection ----------------------------
        # Each RenderState owns its own dense projections.  The active write
        # buffer is the only table the logic thread may mutate; the renderer can
        # therefore continue consuming the previously published read buffer
        # without observing torn material/transform/classification columns.
        table = write_state.render_table
        etable = write_state.entity_table
        self._render_table = table
        self._entity_table = etable
        # Capture the live journal epoch for this frame boundary, then replay
        # every precise invalidation newer than this write buffer's own epoch.
        # The two RenderState buffers alternate ownership, so the first buffer
        # can consume the live journal before the second reaches the edit.
        render_dirty_snapshot = self.editor_state.render_dirty_snapshot()
        snapshot_epoch, _current_dirty = render_dirty_snapshot
        table_epoch = getattr(table, "_epoch", None)
        world_epoch, render_dirty = self.editor_state.render_dirty_since(
            table_epoch, through_epoch=snapshot_epoch
        )
        # Rows are named by the brush's UUID, so ids have to exist before the
        # table reconciles -- but only then, not on every frame.
        # Stable ids are needed when rows are first created/replaced, not
        # for ordinary epoch bumps. Avoid walking the whole scene on every edit.
        # A brush without one can only have arrived with a change to the row
        # set, so only then is the scene walked.
        if (len(brushes) != table.count
                and any(b.get('id') is None for b in brushes)):
            self.editor_state.ensure_entity_ids()
        # In the editor, a tool drags the selection by writing its dicts in
        # place for many frames after one undo checkpoint: those rows are the
        # only ones re-read every frame. Everything else changes through a
        # journal (see RenderTable.begin_frame).
        edited = () if self.play_mode else self.editor_state.edited_objects()
        # An edited row is re-read only by the buffer being written, so when an
        # object leaves the edited set (a deselect after a drag, entering
        # play) the other buffer still holds whatever it last saw and the two
        # published frames would alternate between old and new transforms.
        # Journal the leavers: every table drains its own copy of the journal.
        edited_ids = {id(obj): obj for obj in edited}
        left = [obj for oid, obj in getattr(self, '_last_edited', {}).items()
                if oid not in edited_ids]
        if left:
            JOURNAL.record_many(left, STATE)
        self._last_edited = edited_ids
        peer = self.game_state.peer_state()
        peer_table = peer.render_table if peer is not write_state else None
        table.begin_frame(
            brushes, world_epoch, dirty_objects=render_dirty, edited=edited,
            peer=peer_table,
            peer_dirty=self._peer_render_dirty(
                render_dirty, peer_table, snapshot_epoch))
        if self.play_mode:
            # Every mover and door position, as two array stores.
            self._movers().publish(self, table)
        # The table owns its row objects; each buffer owns its table.
        refs = table.refs
        total_count = table.count

        # ---- T4: visibility, as masks over the table ---------------------
        keep, all_slots = table.shown()
        if self.culling_enabled and total_count:
            visible_slots = np.flatnonzero(keep & self._aabb_in_frustum_bounds(
                frustum_planes, table.bounds[:total_count]))
        else:
            visible_slots = all_slots
        # Published as views over the slots, not as lists: the conversion back
        # to Python objects happens only if something actually reads one, and
        # on the main camera path nothing does.
        all_brushes = PublishedBrushes(refs, all_slots)
        visible_brushes = PublishedBrushes(refs, visible_slots)
        culled_count = total_count - len(visible_slots)

        # The numerical result itself, published rather than thrown away: the
        # slots index every column of the table, so the renderer can classify,
        # sort and batch without reconstructing anything.
        write_state.render_table = table
        write_state.render_refs = refs
        write_state.visible_brush_slots = visible_slots
        write_state.all_brush_slots = all_slots

        write_state.visible_brushes = visible_brushes
        write_state.all_brushes = all_brushes
        write_state.total_brushes = total_count
        write_state.culled_brushes = culled_count

        # ---- the entity half of the projection ---------------------------
        # What used to be one Python pass per entity per frame -- two NumPy
        # scalar stores, three isinstance tests and a list append each -- is a
        # bulk position store, a live `hidden` read, and masks over columns.
        # No lock: begin_frame freezes the entity list with one atomic copy
        # and builds everything from that, and the change journal is
        # thread-safe. Taking the monster lock here used to make every frame
        # wait out whatever AI update was running.
        things = self.things
        # etable is the table owned by the current write buffer.  It is the
        # only EntityTable touched until request_swap publishes this frame.
        etable = write_state.entity_table
        self._entity_table = etable
        peer_etable = peer.entity_table if peer is not write_state else None
        thing_hidden = etable.begin_frame(
            things,
            world_epoch,
            dirty_objects=render_dirty,
            effect_runtime=self.play_mode,
            effect_store=self.effect_store,
            peer=peer_etable,
            peer_dirty=self._peer_render_dirty(
                render_dirty, peer_etable, snapshot_epoch),
        )
        erefs = etable.refs
        entity_things = etable.things
        thing_count = etable.count

        self.editor_state.clear_render_dirty(render_dirty_snapshot)

        # A collected Prop is not published. The dense Prop registry owns
        # collection state, so the renderer filters only Prop rows rather than
        # walking the whole Thing list.
        visible_thing_slots = etable.all_slots
        collected = self._props.collected_ids if self._props is not None else set()
        if self.play_mode and collected:
            # class_bits is a capacity-sized array, while etable.things
            # contains only the live dense rows.  Never let stale bits in the
            # spare capacity turn into entity slots.
            prop_slots = np.flatnonzero(
                (etable.class_bits[:thing_count] & ENT_PROP) != 0
            )
            dropped = [int(i) for i in prop_slots
                       if id(entity_things[int(i)]) in collected]
            if dropped:
                keep_things = np.ones(thing_count, dtype=bool)
                keep_things[dropped] = False
                visible_thing_slots = np.flatnonzero(keep_things)

        # Lights still need their authored object state (colour, intensity,
        # state, etc.) during GL setup, but do not materialise them on the logic
        # thread. Keep the dense selection published and let the actual light
        # consumer materialise it when required.
        all_lights = PublishedEntities(erefs, etable.light_slots)
        # Keep the dense slot selection authoritative. Object materialisation is
        # deferred until a legacy/secondary consumer actually iterates it.
        visible_things = PublishedEntities(erefs, visible_thing_slots)

        write_state.visible_things = visible_things
        write_state.all_lights = all_lights
        # Portal existence is a numeric projection fact; the renderer reads
        # the published portal slot vector directly.
        write_state.has_portals = bool(len(etable.portal_slots))
        write_state.entity_table = etable
        write_state.entity_refs = erefs
        write_state.visible_thing_slots = visible_thing_slots
        write_state.thing_hidden = thing_hidden
        write_state.timestamp = time.perf_counter()

        # ── Player 2 render state ─────────────────────────────────────────────
        if self.play_mode and self.player2:
            p2_pos   = glm.vec3(self.player2.pos)
            p2_cam   = p2_pos + glm.vec3(0, self.player2.camera_height, 0)
            p2_angle = self.player2.angle
            p2_pitch = self.player2.pitch
            p2_dir   = glm.vec3(
                math.sin(p2_angle) * math.cos(p2_pitch),
                math.sin(p2_pitch),
                math.cos(p2_angle) * math.cos(p2_pitch),
            )
            write_state.player2_pos        = p2_pos
            write_state.player2_angle       = p2_angle
            write_state.player2_pitch       = p2_pitch
            write_state.player2_view_matrix = glm.lookAt(
                p2_cam, p2_cam + p2_dir, glm.vec3(0, 1, 0))
            write_state.player2_health      = self.player2_health
            write_state.player2_max_health  = self.player2_max_health
            write_state.player2_dead        = self.player2_dead
            write_state.player2_underwater  = bool(getattr(self.player2, 'eye_underwater', False))
            write_state.splitscreen_active  = True
        else:
            write_state.splitscreen_active  = False
        write_state.level_complete_ui = self.level_complete_ui
        write_state.prepare_ms = (time.perf_counter() - started) * 1000.0