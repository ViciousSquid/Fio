"""
Game Logic Processing

This thread runs game logic at a fixed timestep (60 Hz), handling:
- Player movement and physics
- Entity interactions and triggers
- I/O event dispatching
- Mover and door animations
- Pickup collection
- Player death detection
- Portal transit (Prey 2006-style world portals)
"""

import threading
import time
import numpy as np
from typing import List, Dict, Any, Optional
import glm
import math
import os

from .threaded_game_state import ThreadedGameState, PublishedBrushes
from .player import Player
from .camera import Camera
from .constants import is_solid_world_brush, is_water_brush, brush_aabb_bounds
from .brush_geometry import build_collision_mesh, brush_has_geometry, GEO_RUNTIME_KEYS
from .prop_runtime import PropSession
from .render_table import RenderTable
from .entity_table import EntityTable

# Import Thing subclasses for type checking
try:
    from editor.things import (Speaker, Pickup, Prop as PropThing, Light,
                               Monster as MonsterThing, PathNode, LogicTimer,
                               PlayerStart, Portal, LevelChanger)
except ImportError:
    Speaker = None
    Pickup = None
    PropThing = None
    Light = None
    MonsterThing = None
    PathNode = None
    LogicTimer = None
    PlayerStart = None
    Portal = None

# Import I/O system
try:
    from editor.io_system import IOManager, get_connections
    from editor.io_handlers import register_all_input_handlers
    IO_AVAILABLE = True
    print("[LogicThread] I/O System loaded successfully.")
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
    MONSTER_PROJECTILE_MAX_DIST,
    MONSTER_PROJECTILE_SPRITE_SIZE,
)

# Import the extracted MonsterAI class and new thread
from .monster_ai import MonsterAI, MonsterAIThread

# FIX#1: Map door_direction editor strings to movement vectors
DOOR_DIRECTION_MAP = {
    'up':    [0,  1,  0],
    'down':  [0, -1,  0],
    'north': [0,  0,  1],
    'south': [0,  0, -1],
    'east':  [1,  0,  0],
    'west':  [-1, 0,  0],
}

# Qt key constants
Key_W = 0x57
Key_S = 0x53
Key_A = 0x41
Key_D = 0x44
Key_Space = 0x20
Key_C = 0x43
Key_Shift = 0x01000020
Key_Control = 0x01000021

# Portal transit cooldown — prevents the player from oscillating back and
# forth between two portals if they are very close together (seconds).
_PORTAL_TRANSIT_COOLDOWN = 0.5

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
        self.frustum_aspect = 16.0 / 9.0
        # Shared with the viewport and the renderer (set_view_distance). Held
        # as None until the viewport hands one over, so a LogicThread built in
        # a test without one still culls against the historical far plane.
        self.view_distance = None

        # Play-mode camera mode: "First Person" (default) or "Overhead" (a
        # native top-down camera, GTA 1 / Alien Swarm style). Controlled by the
        # editor's "Camera" dropdown. In overhead mode the view matrix AND the
        # frustum-culling planes are both derived from the overhead camera, so
        # culling stays correct; ``overhead_height`` is how far the camera floats
        # above the player and ``overhead_orientation`` is "north" (fixed map) or
        # "player" (rotate with facing).
        self.camera_mode = "First Person"
        self.overhead_height = 800.0
        self.overhead_tilt = 0.0
        self.overhead_orientation = "north"
        # PERF: is_overhead() runs every render-state build (~60 Hz). Cache the
        # normalised boolean and only recompute when camera_mode actually
        # changes, so the hot path never re-does str().strip().lower().
        self._camera_mode_raw = None
        self._camera_mode_overhead = False
        # Active camera transition (First Person <-> Overhead tween), or None.
        # Set by start_camera_transition, advanced by _update_camera_transition,
        # and consumed in _prepare_render_state to blend the view matrix.
        self.camera_transition = None

        # The dense render projection (T3) and the per-slot render references
        # published alongside it.  The table owns the numbers; `_render_refs`
        # is the object array the renderer is still handed, with movers and
        # doors replaced by their per-frame snapshot.
        self._render_table = RenderTable()
        # The entity half of the same projection.  Entities move every
        # tick and their classification does not, so the table splits
        # those two costs the way the brush one does.
        self._entity_table = EntityTable()
        self._entity_refs = np.empty(0, dtype=object)
        self._entity_all_slots = np.empty(0, dtype=np.int32)
        self._render_refs = np.empty(0, dtype=object)

        # Editor camera
        self.editor_camera = Camera()
        self.editor_camera.pos = glm.vec3(0, 150, 400)
        
        self._editor_mouselook_active = False
        
        # Player stats
        self.player_health = 100
        self.player_max_health = 100
        self.player_dead = False
        self.god_mode = False
        self.buddha_mode = False
        self.notarget = False
        
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

        # Trigger state
        self.fired_once_triggers: set = set()
        # Trigger occupancy/scheduler state has a single owner.
        self._reset_trigger_state()

        # Logic Gate State
        self.gate_inputs = {}
        
        # Countdown state for logic_timer entities, keyed by the timer's UUID
        # (see LogicThread._timer_key) so it survives a save and can never be
        # confused with another entity's.
        self.timer_states: Dict[str, Dict[str, float]] = {}

        # Active light FadeIn/FadeOut transitions, keyed by id(light entity)
        self.light_fade_states: Dict[int, Dict[str, Any]] = {}
        
        # Hurt trigger timers
        self.hurt_trigger_timers: Dict[int, float] = {}
        self.HURT_INTERVAL = 0.5
        
        # Pickup state
        self.collected_pickups: set = set()
        self.collected_keys: set = set()
        
        # Respawn timers
        self.respawn_timers: Dict[int, float] = {}
        
        # Speaker state
        self.active_speakers: set = set()
        
        # Mover/Door Lists
        self.movers = []
        self.doors = []
        # PERF: cached brush-only views of self.movers/self.doors (see _init_movers/_init_doors)
        self._mover_brush_list = []
        self._door_brush_list = []
        
        # Mover Animation State
        self.mover_states = {}
        
        # Door Animation State
        self.door_states: Dict[int, Dict[str, Any]] = {}

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

        # Water sound state (enter/exit transition + wade footstep cadence)
        self._player_was_in_water = False
        self._waterwalk_timer = 0.0

        # Visual FX
        self.bullet_marks = []
        self.BULLET_FADE_TIME = 20.0
        
        # Active weapon
        self.active_weapon = None

        # Muzzle flash
        self.muzzle_flash_active = False

        # Monster AI (delegated to separate class + thread)
        self._monster_lock = threading.RLock()
        self._player_damage_lock = threading.Lock()
        self.monster_ai = MonsterAI(self)
        self.monster_ai_thread = None

        # Mover PathNode waypoint state (used by io_handlers FollowPath)
        self.mover_path_states = {}

        # Cinematic camera state (used by io_handlers LogicCamera)
        self.cinematic_state = None

        # Entity lookup caches — built on play-mode enter
        self._name_cache = {}
        self._id_cache = {}
        self._trigger_brushes = []
        self._trigger_brush_by_bid = {}
        self._use_trigger_entries = []
        self._pickup_things = []
        # The Prop registry (engine.prop_runtime.PropSession).  Created on
        # play-mode enter and None in the editor, where nothing simulates.
        self._props = None
        self._levelchanger_things = []
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
        self._portals_by_name: Dict[str, object] = {}

        self.level_complete_ui = None

        # Monster projectiles (flying monster ranged attacks)
        self._monster_projectiles: list = []

        # Gunfire sound events for AI hearing (list of dicts with pos, time, source)
        self._gunfire_events: list = []

        # Performance Monitoring
        self.actual_tps = 0.0
        self._tick_count = 0
        self._last_tps_time = time.perf_counter()

        # Player 2 turn sensitivity (degrees per second)
        self.p2_turn_sensitivity = 10.0

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
    # ENTITY LOOKUP (for I/O system)
    # =========================================================================
    
    def _build_entity_caches(self):
        """Build O(1) lookup dicts for I/O entity resolution.

        Also precomputes per-tick filtered entity lists (trigger brushes,
        pickups, level changers) so hot-path tick handlers don't have to
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

        # PERF: precomputed thing lists for _handle_interactions / _handle_pickups
        self._pickup_things = [t for t in self.things if Pickup and isinstance(t, Pickup)]
        # Props are not cached here.  PropSession is the registry for the Prop
        # domain and a second list would be a competing copy of it; this is the
        # point at which it re-derives itself from the thing list, alongside
        # every other entity cache, and the engine reads Props back off it.
        if self._props is not None:
            self._props.rebuild(self.things)
        self._levelchanger_things = [t for t in self.things if LevelChanger and isinstance(t, LevelChanger)]

        # PERF: precomputed monster list + id lookup, used by MonsterAI so it
        # doesn't have to isinstance-scan the full (brushes+things) list of
        # every entity in the level on every AI tick.
        self._monster_things = [t for t in self.things if MonsterThing and isinstance(t, MonsterThing)]
        self._monster_by_id = {id(t): t for t in self._monster_things}

        # PERF: the timer list, for the same reason — _update_logic_timers is
        # the one per-frame path the logic system has, and it should walk the
        # timers, not the level.
        self._timer_things = [t for t in self.things if LogicTimer and isinstance(t, LogicTimer)]

        # PERF: portals, for the same reason again.  _update_portals ticks every
        # portal's fade every frame, which used to mean an isinstance scan of
        # the entire thing list per frame on a map with no portals at all.  The
        # name index is derived here too, in the same pass, so the two can never
        # disagree about which portals exist.
        self._portal_things = [t for t in self.things if Portal and isinstance(t, Portal)]
        self._portals_by_name = {}
        for t in self._portal_things:
            n = t.properties.get('name', '')
            if n:
                self._portals_by_name[n] = t

    def _find_entity_by_name(self, name: str):
        if not name:
            return None
        return self._name_cache.get(name)

    def _find_entity_by_id(self, entity_id: str):
        if not entity_id:
            return None
        return self._id_cache.get(entity_id)

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

    @staticmethod
    def _clear_brush_collision(brush):
        """Strip runtime mesh-collision keys so the brush reverts to AABB."""
        for k in GEO_RUNTIME_KEYS:
            if k in ('_geo_cache', '_geo_cache_sig'):
                continue  # keep the geometry render/query cache
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
                and any(float(v) != 0.0 for v in collision_size)
            )

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

    def set_player2(self, player2: Optional[Player]) -> None:
        """Set or clear Player 2 for split-screen mode."""
        self.player2 = player2
        if player2 is None:
            self.player2_health = 100
            self.player2_max_health = 100
            self.player2_dead = False
        
    def set_play_mode(self, enabled: bool):
        self.play_mode = enabled
        
        if enabled:
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

            # Reset player stats
            self.player_health = 100
            self.player_max_health = 100
            self.player_dead = False
            self.god_mode = False
            self.buddha_mode = False
            self.notarget = False
            
            # Reset pickup state
            self._reset_trigger_state()
            self.collected_pickups.clear()
            self.collected_keys.clear()
            self.respawn_timers.clear()
            for thing in self.things:
                if Pickup and isinstance(thing, Pickup):
                    thing.properties['collected'] = False
            
            # Reset speaker state
            self.active_speakers.clear()
            self.hurt_trigger_timers.clear()
            self.current_hud_message = ""

            # Reset water sound state (no spurious enter/exit on spawn)
            self._player_was_in_water = False
            self._waterwalk_timer = 0.0

            # Reset gate inputs
            self.gate_inputs = {}
            
            # Reset timer states
            self.timer_states = {}
            
            # Reset active weapon
            self.active_weapon = None
            
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

            # Reset portal transit state
            self._portal_cooldowns.clear()
            self._portal_prev_player_pos = None

            # Reset portal fade state so portals start at the correct opacity
            if Portal is not None:
                for t in self.things:
                    if isinstance(t, Portal):
                        _a = t.is_active()
                        t._fade_alpha = 1.0 if _a else 0.0
                        t._fade_target = t._fade_alpha

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

            # Clear gunfire events
            self._gunfire_events.clear()

            # Fire OnPlayerSpawn
            self._fire_player_spawn_outputs()
            
            # Initialize timers that start on
            self._init_logic_timers()

            # Start monster AI thread
            self._start_monster_ai()
            
        else:
            self._stop_monster_ai()
            self._reset_trigger_state()
            self.fired_once_triggers.clear()
            self.collected_pickups.clear()
            self.collected_keys.clear()
            self.respawn_timers.clear()
            self.active_speakers.clear()
            self.hurt_trigger_timers.clear()
            self._reset_movers()
            self._reset_doors()
            self._reset_parented_lights()
            self._reset_parented_portals()
            self._clear_angled_brush_collision()
            self._model_collision_brushes = []
            self._physics_body_brushes = []
            self._refresh_collision_brushes_cache()
            self.current_hud_message = ""
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

            # Reset portal transit state
            self._portal_cooldowns.clear()
            self._portal_prev_player_pos = None

            # Reset portal fade state to match 'active' property (editor view stays correct)
            if Portal is not None:
                for t in self.things:
                    if isinstance(t, Portal):
                        _a = t.is_active()
                        t._fade_alpha = 1.0 if _a else 0.0
                        t._fade_target = t._fade_alpha

            self.level_complete_ui = None

            # Clear monster projectiles
            self._monster_projectiles.clear()

            # Clear gunfire events
            self._gunfire_events.clear()

            # Reset monster AI state
            self._reset_all_monsters(clear_dead=False)

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
                session.commit_all()   # flush every cell, loaded or unloaded
                snapshot = savegame.build_snapshot(
                    self, map_name=map_name,
                    world_mode=savegame.WORLD_MODE_BIGWORLD,
                    cell_deltas=session.serialize_registry(),
                    base_world=session.base_identity(map_name))
            else:
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

    def load_session(self, path: str, *, map_name: str = ""):
        """Restore a saved play session from *path* as an overlay on the live
        session. Returns ``(ok, message)``.

        Native counterpart to the editor's ``load`` / ``quickload`` console
        commands *when already in play mode*. The scene is not rebuilt — entity
        state is matched back by stable id — so this must run against the same
        map the save was taken on (the caller loads the map and enters play mode
        first when starting from the editor).

        The save mode (full / delta / both / legacy) is auto-detected from the
        file's metadata; *map_name* is the currently-loaded map, used to validate
        a delta's base map. Loading never prompts unless recovery is impossible.
        """
        if not self.play_mode:
            return False, "Enter play mode before loading a session."
        try:
            from engine import savegame
            data = savegame.read(path)
            report = savegame.restore_auto(self, data, current_map_name=map_name)
            msg = f"Loaded play session from '{os.path.basename(path)}'"
            warning = report.get("warning")
            if warning:
                msg += f" — {warning}"
            return True, msg
        except FileNotFoundError:
            return False, f"Save file not found: {path}"
        except Exception as exc:
            return False, f"Load failed: {exc}"

    def _start_monster_ai(self):
        """Start the monster AI processing thread."""
        self._stop_monster_ai()
        self.monster_ai_thread = MonsterAIThread(
            self, self.monster_ai, self._monster_lock, tick_rate=30
        )
        self.monster_ai_thread.start()

    def _stop_monster_ai(self):
        """Signal the monster AI thread to stop."""
        if self.monster_ai_thread is not None:
            self.monster_ai_thread.stop()
            self.monster_ai_thread = None

    def _reset_all_monsters(self, clear_dead=True):
        """Reset all monster AI state. Called when entering or exiting play mode.

        Also records each monster's authored health, keyed by UUID, so the
        Respawn input has something to restore to: the live ``health`` property
        is what damage mutates, so by the time a monster is dead the number the
        map authored is gone.  One dict filled during a pass that already walks
        every monster — no extra scan, and nothing new on the entity itself.
        """
        self.monster_ai.monster_states = {}
        if not MonsterThing:
            return
        if clear_dead:
            self._monster_spawn_health = {}
        for thing in self.things:
            if not isinstance(thing, MonsterThing):
                continue
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

    def _fire_player_spawn_outputs(self):
        if not self.io_manager:
            return
        if not PlayerStart:
            return
        for thing in self.things:
            if isinstance(thing, PlayerStart):
                self.io_manager.fire_output(thing, 'OnPlayerSpawn')
                self._plugin_emit("player_spawn", start=thing)
                break
    
    @staticmethod
    def _timer_key(thing):
        """A timer's countdown is filed under its UUID, not its memory address.

        ``id(thing)`` is not an identity: it changes on every load, so a
        countdown could never be saved, and CPython reuses addresses, so a
        freed entity's slot could be inherited by an unrelated one.
        """
        return thing.properties.get('id') or thing.properties.get('name', '')

    def _init_logic_timers(self):
        if not LogicTimer:
            return
        for thing in self._timer_things:
            if thing.properties.get('start_on', False):
                try:
                    interval = max(0.01, float(thing.properties.get('interval', 1.0)))
                except (TypeError, ValueError):
                    interval = 1.0
                thing.properties['timer_enabled'] = True
                self.timer_states[self._timer_key(thing)] = {
                    'remaining': interval,
                    'interval': interval
                }
    
    def set_terrain(self, terrain):
        self.terrain = terrain
    
    def set_editor_camera(self, pos: glm.vec3, yaw: float, pitch: float, fov: float):
        self.editor_camera.pos = glm.vec3(pos)
        self.editor_camera.yaw = yaw
        self.editor_camera.pitch = pitch
        self.editor_camera.fov = fov
    
    def get_editor_camera(self) -> Camera:
        return self.editor_camera

    def set_frustum_aspect(self, aspect: float):
        self.frustum_aspect = aspect

    def set_view_distance(self, view_distance):
        """Adopt the viewport's shared view-distance settings.

        The frustum this thread culls against must use the same far plane the
        renderer draws with. If it kept a larger one it would keep feeding the
        renderer brushes the far plane then clips -- harmless but wasted work
        every frame; a smaller one would cull something still on screen. The
        object is shared, not copied, so a spinbox or console change is picked
        up on the next tick.
        """
        self.view_distance = view_distance

    def set_camera_mode(self, mode: str):
        """Select the play-mode camera: 'First Person' or 'Overhead'."""
        self.camera_mode = str(mode)

    def is_overhead(self) -> bool:
        # PERF: cached — recompute only when camera_mode is reassigned (works
        # whether set via set_camera_mode or by direct attribute assignment).
        cm = self.camera_mode
        if cm != self._camera_mode_raw:
            self._camera_mode_raw = cm
            self._camera_mode_overhead = str(cm).strip().lower() in (
                "overhead", "top-down", "topdown")
        return self._camera_mode_overhead

    def _overhead_camera(self, player_pos, angle):
        """Compute ``(cam_pos, direction, up)`` for the overhead camera.

        The camera floats ``overhead_height`` above the player looking down (raked
        by ``overhead_tilt``); the up hint is the ground heading (fixed north or
        the player's facing) so it is always perpendicular to a straight-down view
        — never the degenerate world-up that would corrupt the view/frustum.
        """
        px, py, pz = float(player_pos.x), float(player_pos.y), float(player_pos.z)
        if str(self.overhead_orientation).strip().lower() == "player":
            head_x, head_z = math.sin(angle), math.cos(angle)
        else:  # fixed north — world -Z at the top of the screen (GTA 1 style)
            head_x, head_z = 0.0, -1.0

        tilt = math.radians(max(0.0, min(89.0, float(self.overhead_tilt))))
        sin_t, cos_t = math.sin(tilt), math.cos(tilt)
        dir_x, dir_y, dir_z = head_x * sin_t, -cos_t, head_z * sin_t
        dlen = math.sqrt(dir_x * dir_x + dir_y * dir_y + dir_z * dir_z) or 1.0
        direction = glm.vec3(dir_x / dlen, dir_y / dlen, dir_z / dlen)

        dist = float(self.overhead_height) / max(1e-3, cos_t)
        cam_pos = glm.vec3(px - direction.x * dist,
                           py - direction.y * dist,
                           pz - direction.z * dist)
        up = self._safe_up(direction, glm.vec3(head_x, 0.0, head_z))
        return cam_pos, direction, up

    @staticmethod
    def _safe_up(direction, up):
        """A non-degenerate up vector for ``glm.lookAt`` (see _overhead_camera)."""
        d = glm.vec3(direction)
        if glm.length(d) < 1e-8:
            return glm.vec3(0, 1, 0)
        d = glm.normalize(d)
        u = glm.vec3(up)
        u = glm.normalize(u) if glm.length(u) > 1e-8 else glm.vec3(0, 1, 0)
        if abs(glm.dot(d, u)) > 0.999:
            u = glm.vec3(0, 0, 1) if abs(d.y) > 0.9 else glm.vec3(0, 1, 0)
        return u

    def _camera_for_mode(self, overhead, player_pos, player_angle,
                         player_pitch, camera_height):
        """Return ``(cam_pos, direction, up, fov)`` for one camera mode.

        Both endpoints of a camera tween are computed from the *current* player
        position/facing each frame, so the blend tracks the player as they move.
        """
        if overhead:
            cam_pos, direction, up = self._overhead_camera(player_pos, player_angle)
            return cam_pos, direction, up, 90.0
        cam_pos = player_pos + glm.vec3(0, camera_height, 0)
        direction = glm.vec3(
            math.sin(player_angle) * math.cos(player_pitch),
            math.sin(player_pitch),
            math.cos(player_angle) * math.cos(player_pitch),
        )
        return cam_pos, direction, glm.vec3(0, 1, 0), 90.0

    def start_camera_transition(self, target_mode=None, duration=1.0):
        """Begin a smooth tween between First Person and Overhead cameras.

        ``target_mode`` may be ``None`` (toggle to the opposite of the current
        mode) or a string ("overhead"/"top-down"/"topdown" → overhead, anything
        else → first person). ``duration`` is the tween length in seconds; <= 0
        switches instantly. ``camera_mode`` is updated to the target immediately
        so gameplay (aiming, the overhead sprite) uses the new mode, while the
        view matrix blends over ``duration``. Returns the new mode string.
        """
        current_overhead = self.is_overhead()
        if target_mode is None:
            to_overhead = not current_overhead
        else:
            to_overhead = str(target_mode).strip().lower() in (
                "overhead", "top-down", "topdown", "top", "td")
        new_mode = "Overhead" if to_overhead else "First Person"

        try:
            duration = float(duration)
        except (TypeError, ValueError):
            duration = 1.0

        ct = self.camera_transition

        # No-op when already in the requested mode and not mid-tween.
        if to_overhead == current_overhead and not ct:
            self.camera_mode = new_mode
            return new_mode

        if duration <= 0.0:
            self.camera_transition = None
            self.camera_mode = new_mode
            return new_mode

        # Reversing an in-flight tween back toward its origin: mirror the current
        # progress so the camera continues smoothly from where it is rather than
        # snapping to an endpoint.
        if ct and to_overhead == ct['from_overhead']:
            progressed = min(ct['elapsed'], ct['duration'])
            remaining_frac = 1.0 - (progressed / ct['duration'] if ct['duration'] > 0 else 1.0)
            self.camera_transition = {
                'from_overhead': ct['to_overhead'],
                'to_overhead':   to_overhead,
                'elapsed':       remaining_frac * duration,
                'duration':      duration,
            }
            self.camera_mode = new_mode
            return new_mode

        self.camera_transition = {
            'from_overhead': current_overhead,
            'to_overhead':   to_overhead,
            'elapsed':       0.0,
            'duration':      duration,
        }
        self.camera_mode = new_mode
        return new_mode

    def _update_camera_transition(self, delta):
        """Advance the active camera tween; clear it when complete."""
        ct = self.camera_transition
        if not ct:
            return
        ct['elapsed'] += delta
        if ct['elapsed'] >= ct['duration']:
            self.camera_transition = None

    # =========================================================================
    # MOVER/DOOR INITIALIZATION
    # =========================================================================

    def _init_movers(self):
        self.mover_states = {}
        self.mover_path_states = {}
        self.movers = []
        for i, brush in enumerate(self.brushes):
            if brush.get('is_mover'):
                self.movers.append((i, brush))
                if 'original_pos' not in brush:
                    brush['original_pos'] = list(brush['pos'])

                # FIX: initialise rotation_yaw if mover rotates
                if brush.get('rotate', False) and 'rotation_yaw' not in brush:
                    brush['rotation_yaw'] = 0.0

                path_target = brush.get('path_target', '')
                if path_target and brush.get('start_on', False):
                    self.mover_path_states[i] = {
                        'current_node': path_target,
                        'lerp_t':       0.0,
                        'origin':       list(brush['pos']),
                        'waiting':      False,
                        'wait_remaining': 0.0,
                    }
                elif not brush.get('move_once', False):
                    self.mover_states[i] = {'progress': 0.0, 'forward': True}
        # PERF: cache the brush-only view of self.movers — was rebuilt via a
        # list comprehension every tick in _tick_play_mode.
        self._mover_brush_list = [b for _, b in self.movers]

    def _reset_movers(self):
        self.movers = []
        for i, brush in enumerate(self.brushes):
            if brush.get('is_mover') and 'original_pos' in brush:
                brush['pos'] = list(brush['original_pos'])
        self.mover_states = {}
        self._mover_brush_list = []

    def _init_doors(self):
        self.door_states = {}
        self.doors = []
        for i, brush in enumerate(self.brushes):
            if brush.get('is_door'):
                # Resolve runtime parameters from editor properties without mutating the source brush
                speed = float(brush.get('door_speed', brush.get('speed', 128.0)))
                distance = float(brush.get('door_distance', brush.get('distance', 128.0)))
                dir_str = brush.get('door_direction', '')
                direction = DOOR_DIRECTION_MAP.get(dir_str, [0, 1, 0])

                if 'door_lip' in brush:
                    lip = float(brush.get('door_lip', 0.0))
                    distance = max(1.0, distance - lip)

                self.doors.append((i, brush))
                if 'original_pos' not in brush:
                    brush['original_pos'] = list(brush['pos'])
                # PERF: DOOR_DIRECTION_MAP entries are already unit vectors,
                # and door direction never changes at runtime, so normalize
                # once here instead of every tick in _update_doors.
                self.door_states[i] = {
                    'progress': 0.0,
                    'state': 'closed',
                    'open_timer': 0.0,
                    'speed': speed,
                    'distance': distance,
                    'direction': direction,
                    # Scalar unit-direction tuple (DOOR_DIRECTION_MAP entries are
                    # already unit vectors). Kept as plain Python floats -- not a
                    # NumPy array -- so the per-tick offset maths below produces
                    # ordinary floats and brush['pos'] stays JSON-serialisable.
                    '_direction_np': (float(direction[0]), float(direction[1]),
                                      float(direction[2])),
                }
        # PERF: cache the brush-only view of self.doors — was rebuilt via a
        # list comprehension every tick in _tick_play_mode.
        self._door_brush_list = [b for _, b in self.doors]

    def _reset_doors(self):
        self.doors = []
        for i, brush in enumerate(self.brushes):
            if brush.get('is_door') and 'original_pos' in brush:
                brush['pos'] = list(brush['original_pos'])
        self.door_states = {}
        self._door_brush_list = []

    def _trigger_door_open(self, door_idx: int, brush: dict):
        """Start opening a door if it is currently closed or closing."""
        if door_idx not in self.door_states:
            return
        state = self.door_states[door_idx]
        if state['state'] in ('closed', 'closing'):
            state['state'] = 'opening'
            if self.io_manager:
                self.io_manager.fire_output(brush, 'OnOpen')
            self._plugin_emit("door_open", door=brush, door_idx=door_idx)

    # =========================================================================
    # MAIN LOOP
    # =========================================================================
            
    def run(self):
        self.running = True
        last_time = time.perf_counter()
        accumulator = 0.0
        
        while self.running:
            current_time = time.perf_counter()
            frame_time = current_time - last_time
            last_time = current_time
            
            if frame_time > 0.25:
                frame_time = 0.25
                
            accumulator += frame_time
            
            while accumulator >= self.TICK_DURATION:
                try:
                    self._tick(self.TICK_DURATION)
                except Exception:
                    # A single bad tick (e.g. a broken entity handler) must not
                    # silently kill the whole logic thread -- that freezes the
                    # game and stops every other system with no visible error.
                    # The full traceback is logged, so this isolates the failure
                    # without hiding it; it is never a bare pass.
                    import traceback
                    debug_log("LogicThread",
                              "Unhandled exception in _tick:\n" + traceback.format_exc())
                accumulator -= self.TICK_DURATION
                self._update_tps_counter()
                
            self._prepare_render_state()
            self.game_state.request_swap()
            
            sleep_time = self.TICK_DURATION - (time.perf_counter() - current_time)
            if sleep_time > 0:
                time.sleep(sleep_time * 0.9)
                
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
        else:
            self._tick_editor_mode(delta)

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

    def _tick_play_mode(self, delta):
        if not self.player:
            return
        
        # Update movers & doors first (for platform carrying)
        self._update_respawns(delta)
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
        # only affects the view matrix when no cinematic is overriding it.
        self._update_camera_transition(delta)

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
        
        # Clear muzzle flash from previous frame
        self.muzzle_flash_active = False

        # Player input
        keys = self.game_state.get_keys()
        mouse_dx, mouse_dy = self.game_state.consume_mouse_delta()
        use_key = self.game_state.consume_use_key()
        
        # Mouse look
        SENSITIVITY = 0.002
        self.player.angle -= mouse_dx * SENSITIVITY
        self.player.pitch -= mouse_dy * SENSITIVITY
        self.player.pitch = max(-1.5, min(1.5, self.player.pitch))
        
        # Movement
        move_dir = glm.vec3(0)
        if Key_W in keys: move_dir.z += 1
        if Key_S in keys: move_dir.z -= 1
        if Key_A in keys: move_dir.x += 1  
        if Key_D in keys: move_dir.x -= 1 
        
        jump = Key_Space in keys
        crouch = Key_C in keys
        
        # Physics update
        collision_brushes = self._collision_brushes_cache
        self.player.update(delta, move_dir, jump, crouch, collision_brushes,
                          self._mover_brush_list, self._door_brush_list, self.terrain,
                          spatial_grid=getattr(self, '_spatial_grid', None))

        # Water enter/exit/wade sounds (uses the post-physics immersion state)
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

        self._check_pickups()
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
        if self.player2 and not self.player2_dead:
            p2 = self.game_state.get_p2_input()
            p2_dir = glm.vec3(float(p2['move_x']), 0.0, float(p2['move_z']))
            # Apply turning with sensitivity and delta
            turn_input = float(p2['look_dx'])
            self.player2.angle -= turn_input * self.p2_turn_sensitivity * delta
            self.player2.pitch -= float(p2['look_dy']) * 0.002
            self.player2.pitch = max(-1.5, min(1.5, self.player2.pitch))
            self.player2.update(
                delta, p2_dir,
                bool(p2['jump']), False,   # crouch removed
                collision_brushes, self._mover_brush_list, self._door_brush_list, self.terrain,
                spatial_grid=getattr(self, '_spatial_grid', None),
            )

    # =========================================================================
    # WATER SOUNDS
    # =========================================================================

    def _update_water_sounds(self, delta: float):
        """Queue splash sounds off the player's immersion state.

        - enterwater.wav on the transition dry → in water
        - exitwater.wav  on the transition in water → dry
        - waterwalk.wav  on a repeating footstep cadence while wading
          (in water, not deep enough to swim, on the ground, and moving)

        Missing sound files are handled gracefully by the render thread's
        _process_sound_queue, so this is safe even before the assets exist.
        """
        if not self.player:
            return

        in_water = bool(self.player.in_water)

        # Enter / exit transitions. Each splash is also an audible event so
        # nearby hearing monsters can wake and investigate (same system as
        # gunfire) — quieter than a gunshot, hence _WATER_LOUDNESS.
        if in_water and not self._player_was_in_water:
            self.game_state.queue_sound({'file': 'enterwater.wav', 'volume': 1.0})
            self._emit_noise_event(self.player.pos, source='water_enter',
                                   loudness=_WATER_LOUDNESS)
            self._waterwalk_timer = 0.0  # allow a wade step promptly after entry
        elif not in_water and self._player_was_in_water:
            self.game_state.queue_sound({'file': 'exitwater.wav', 'volume': 1.0})
            self._emit_noise_event(self.player.pos, source='water_exit',
                                   loudness=_WATER_LOUDNESS)
        self._player_was_in_water = in_water

        # Wading footsteps: only while shallow (not swimming), grounded, moving
        wading = (in_water and not self.player.swimming and self.player.on_ground)
        horiz_speed = math.hypot(self.player.velocity.x, self.player.velocity.z)
        if wading and horiz_speed > 20.0:
            self._waterwalk_timer -= delta
            if self._waterwalk_timer <= 0.0:
                self.game_state.queue_sound({'file': 'waterwalk.wav', 'volume': 0.8})
                self._waterwalk_timer = self.WATERWALK_INTERVAL
        else:
            # Reset so the next stride into water plays a step immediately
            self._waterwalk_timer = 0.0

    # =========================================================================
    # PORTAL TRANSIT
    # =========================================================================

    def _update_portals(self, delta: float):
        """
        Detect and execute player transit through active portal pairs.
        """
        if Portal is None or not self.player:
            return
        if not self._portal_things:
            # No portals in this map: nothing to fade, nothing to cross, and no
            # cooldowns to decay (they are only ever written below).
            return

        # Tick fade transitions for every portal each frame, off the list built
        # by _build_entity_caches — walking the portals, not the level.
        for t in self._portal_things:
            t.tick_fade(delta)

        # Decay all active cooldowns
        for pid in list(self._portal_cooldowns):
            self._portal_cooldowns[pid] -= delta
            if self._portal_cooldowns[pid] <= 0.0:
                del self._portal_cooldowns[pid]

        portals_by_name = self._portals_by_name

        cur = (float(self.player.pos.x), float(self.player.pos.y), float(self.player.pos.z))
        prev = self._portal_prev_player_pos
        if prev is None:
            prev = cur
        # Player half-extents — used for radius-aware exit clearance so the body
        # never emerges embedded in the wall behind the destination.
        half = getattr(self.player, '_half', None)
        try:
            hx, hy, hz = float(half.x), float(half.y), float(half.z)
        except AttributeError:
            hx, hy, hz = 25.0, 50.0, 25.0

        for portal_a in list(portals_by_name.values()):
            if not portal_a.is_active():
                continue
            target_name = portal_a.properties.get('portal_target', '')
            if not target_name:
                continue
            portal_b = portals_by_name.get(target_name)
            if portal_b is None or not portal_b.is_active():
                continue
            if id(portal_a) in self._portal_cooldowns:
                continue

            hit = self._segment_crosses_aperture(portal_a, prev, cur)
            if hit is not None:
                self._execute_portal_transit(portal_a, portal_b, hx, hy, hz)
                cd = getattr(Portal, 'TRANSIT_COOLDOWN', _PORTAL_TRANSIT_COOLDOWN)
                self._portal_cooldowns[id(portal_a)] = cd
                self._portal_cooldowns[id(portal_b)] = cd
                if self.io_manager:
                    # Fire both output names so connections made against either
                    # the canonical 'OnTeleport' pin (logic graph editor / IO
                    # registry) or the legacy 'OnPlayerEnter' pin both trigger.
                    self.io_manager.fire_output(portal_a, 'OnTeleport')
                    self.io_manager.fire_output(portal_a, 'OnPlayerEnter')
                debug_log(
                    "Portal",
                    f"Player transited '{portal_a.properties.get('name')}' "
                    f"→ '{portal_b.properties.get('name')}'"
                )
                break  # one transit per frame; player pos has now jumped

        # Store the post-update position so next frame's segment starts here.
        # After a transit that is the emerged position, so the paired portal
        # won't see a bogus crossing.
        self._portal_prev_player_pos = (
            float(self.player.pos.x), float(self.player.pos.y), float(self.player.pos.z)
        )

    @staticmethod
    def _segment_crosses_aperture(portal, prev, cur):
        """Return the world crossing point if the segment prev→cur passes
        through ``portal`` front-to-back within its aperture, else None.

        Testing the actual segment/plane intersection (rather than the endpoint)
        stops fast movers from tunnelling through a small aperture between
        frames.
        """
        nx, ny, nz = portal.get_normal()
        ox, oy, oz = portal.pos
        s_prev = (prev[0] - ox) * nx + (prev[1] - oy) * ny + (prev[2] - oz) * nz
        s_cur  = (cur[0]  - ox) * nx + (cur[1]  - oy) * ny + (cur[2]  - oz) * nz
        # Only a front(>=0) → back(<0) crossing counts.
        if not (s_prev >= 0.0 and s_cur < 0.0):
            return None
        denom = s_prev - s_cur
        t = s_prev / denom if denom > 1e-9 else 0.0
        t = min(1.0, max(0.0, t))
        hit = (prev[0] + (cur[0] - prev[0]) * t,
               prev[1] + (cur[1] - prev[1]) * t,
               prev[2] + (cur[2] - prev[2]) * t)
        if portal.contains_point(hit[0], hit[1], hit[2], margin=0.0):
            return hit
        return None

    def _execute_portal_transit(self, portal_a, portal_b, hx=25.0, hy=50.0, hz=25.0):
        """Teleport the player through portal_a to portal_b using the portal's
        shared link transform, so this exactly matches the view the renderer
        draws through the aperture.  Position, velocity and look direction are
        all carried through, including pitch for tilted/floor portals."""
        p = self.player.pos
        # Position and velocity through the shared transform.
        tx, ty, tz = portal_a.map_point(portal_b, float(p.x), float(p.y), float(p.z))
        vx, vy, vz = portal_a.map_direction(
            portal_b, float(self.player.velocity.x),
            float(self.player.velocity.y), float(self.player.velocity.z))

        # Push out along the destination normal by the body's extent along that
        # normal plus a small clearance, so we never spawn inside the far wall.
        bnx, bny, bnz = portal_b.get_normal()
        clearance = abs(bnx) * hx + abs(bny) * hy + abs(bnz) * hz + Portal.EXIT_CLEARANCE
        self.player.pos = glm.vec3(tx + bnx * clearance,
                                   ty + bny * clearance,
                                   tz + bnz * clearance)
        self.player.velocity = glm.vec3(vx, vy, vz)

        # Re-derive yaw (and pitch) from the transformed look direction so the
        # camera comes out pointing the right way even for pitched portals.
        angle = float(self.player.angle)
        pitch = float(getattr(self.player, 'pitch', 0.0))
        fx = math.sin(angle) * math.cos(pitch)
        fy = math.sin(pitch)
        fz = math.cos(angle) * math.cos(pitch)
        mfx, mfy, mfz = portal_a.map_direction(portal_b, fx, fy, fz)
        self.player.angle = math.atan2(mfx, mfz)
        if hasattr(self.player, 'pitch'):
            self.player.pitch = math.asin(max(-1.0, min(1.0, mfy)))

        self._plugin_emit("portal_transit", portal_from=portal_a, portal_to=portal_b)

    def _transit_projectile_through_portals(self, proj, prev_pos):
        """Teleport a monster projectile through any active portal pair whose
        aperture its movement segment crossed this frame.  Position and
        velocity are carried through the shared link transform, so a fireball
        that flies into portal A comes out of portal B on course.

        No cooldown is needed: the projectile emerges in front of B travelling
        *away* from it, so the front-to-back crossing test cannot re-fire on the
        following frame (its stored prev position becomes the emerged point)."""
        if Portal is None or not self._portals_by_name:
            return
        cur = (proj['pos'][0], proj['pos'][1], proj['pos'][2])
        for portal_a in self._portals_by_name.values():
            if not portal_a.is_active():
                continue
            target_name = portal_a.properties.get('portal_target', '')
            if not target_name:
                continue
            portal_b = self._portals_by_name.get(target_name)
            if portal_b is None or not portal_b.is_active():
                continue
            if self._segment_crosses_aperture(portal_a, prev_pos, cur) is None:
                continue
            npx, npy, npz = portal_a.map_point(portal_b, cur[0], cur[1], cur[2])
            nvx, nvy, nvz = portal_a.map_direction(
                portal_b, proj['vel'][0], proj['vel'][1], proj['vel'][2])
            bnx, bny, bnz = portal_b.get_normal()
            proj['pos'][0] = npx + bnx * Portal.EXIT_CLEARANCE
            proj['pos'][1] = npy + bny * Portal.EXIT_CLEARANCE
            proj['pos'][2] = npz + bnz * Portal.EXIT_CLEARANCE
            proj['vel'][0], proj['vel'][1], proj['vel'][2] = nvx, nvy, nvz
            break

    # =========================================================================
    # LOGIC TIMER UPDATE
    # =========================================================================
    
    def _update_logic_timers(self, delta: float):
        """Advance the running timers.  The only clock Fio's logic has.

        Walks a precomputed list of timer entities rather than isinstance-testing
        every thing in the level each frame, and an *enabled* timer is the only
        thing it touches — a level full of timers that are switched off costs a
        flag read each, and a level with none costs nothing at all.

        This is not a logic tick: no state is scanned, no condition is
        evaluated, and nothing else in the logic system has a per-frame path.
        Time is simply the one event source that has to come from somewhere.
        """
        if not self._timer_things:
            return

        for thing in self._timer_things:
            if not thing.properties.get('timer_enabled', False):
                continue
            key = self._timer_key(thing)
            state = self.timer_states.get(key)
            if state is None:
                try:
                    interval = max(0.01, float(thing.properties.get('interval', 1.0)))
                except (TypeError, ValueError):
                    interval = 1.0
                state = {'remaining': interval, 'interval': interval}
                self.timer_states[key] = state
            state['remaining'] -= delta
            if state['remaining'] > 0:
                continue

            if self.io_manager:
                self.io_manager.fire_output(thing, 'OnTimer')
            # A one-shot timer stops itself rather than being stopped by the
            # chain it drives, so a map does not have to remember to wire the
            # Disable back — and OnFinished says it happened, for a chain that
            # wants to know.
            if thing.properties.get('one_shot', False):
                thing.properties['timer_enabled'] = False
                self.timer_states.pop(key, None)
                if self.io_manager:
                    self.io_manager.fire_output(thing, 'OnFinished')
            else:
                state['remaining'] = state['interval']

    # =========================================================================
    # LIGHT FADE UPDATE
    # =========================================================================

    def _update_light_fades(self, delta: float):
        """Advance any active light FadeIn/FadeOut transitions.

        Fade state is created by the light 'fadein'/'fadeout' I/O handlers.
        Each frame we lerp the light's intensity toward its target; when the
        transition completes we snap to the target and, for a fade-out, turn
        the light off and fire OnTurnedOff.
        """
        if not self.light_fade_states:
            return
        finished = []
        for key, st in self.light_fade_states.items():
            entity = st['entity']
            st['elapsed'] += delta
            duration = st['duration']
            t = 1.0 if duration <= 0.0 else min(1.0, st['elapsed'] / duration)
            entity.properties['intensity'] = st['from'] + (st['to'] - st['from']) * t
            if t >= 1.0:
                entity.properties['intensity'] = st['to']
                if st['end_off']:
                    entity.properties['state'] = 'off'
                    if self.io_manager:
                        self.io_manager.fire_output(entity, 'OnTurnedOff')
                finished.append(key)
        for key in finished:
            self.light_fade_states.pop(key, None)

    # =========================================================================
    # TRIGGER HANDLING
    # =========================================================================

    @staticmethod
    def _trigger_filters(brush):
        """Return configured trigger detection categories.

        Missing filters are the compatibility default: player only.
        """
        filters = brush.get('trigger_filters', ['player'])
        if isinstance(filters, str):
            filters = [filters]
        if not isinstance(filters, (list, tuple, set)):
            filters = ['player']
        return {
            str(name).strip().lower()
            for name in filters
            if str(name).strip().lower() in ('player', 'props', 'monsters')
        }

    def _reset_trigger_state(self):
        """Create or clear all trigger occupancy and scheduler state.

        Containers are cleared in place when they already exist, so any
        holder of a reference (e.g. ``player_in_triggers``) sees the reset.
        """
        def fresh(name, factory):
            current = getattr(self, name, None)
            if current is None:
                setattr(self, name, factory())
            else:
                current.clear()

        # Active occupants keyed by trigger id, then (entity type, entity id).
        # Each trigger is sampled at its own configured interval; unchanged
        # contacts are retained between that trigger's polls.
        fresh('_trigger_contacts', dict)
        # Mirrors for code that inspects player-only or non-player state.
        fresh('player_in_triggers', set)
        fresh('_nonplayer_trigger_contacts', dict)
        # Scheduler: wakes every TRIGGER_POLL_TICK and polls only triggers
        # whose own interval has elapsed; never scans at the 60 Hz tick rate.
        self._trigger_poll_elapsed = 0.0
        fresh('_trigger_poll_elapsed_by_bid', dict)
        # Each use-key press gets a generation number consumed independently
        # per trigger, so a fast trigger cannot steal a slower one's press.
        self._trigger_use_generation = 0
        fresh('_trigger_use_seen', dict)
        # Evaluated per tick by _sample_use_prompt; kept as an attribute only
        # so the render state and tests can read the frame's current prompt.
        self._trigger_use_prompt = ""
        self._refresh_use_triggers()

    @staticmethod
    def use_trigger_contains(distance_sq, use_radius):
        """Whether something at *distance_sq* is inside a use trigger's volume.

        **Fio's authored use volume is a sphere.** ``use_radius`` is the exact
        activation radius in every direction, which is what a mapper writing
        ``use_radius = 128`` means and what 2.4.2 implemented
        (``glm.distance(player, trigger) < use_radius``).

        The spatial broad phase bounds a use trigger with an axis-aligned box
        of the same radius because that is what a batched pass can do cheaply.
        That box is an **acceleration structure, not a second trigger shape**:
        it fully contains the sphere, so it can only ever admit candidates, and
        this predicate is the only thing that decides. Letting the box decide
        made a button usable from up to sqrt(3) times its authored radius on
        the diagonal.

        Squared throughout -- no square roots, and it vectorises, so the
        prompt pass and the firing pass share one definition rather than
        keeping two that can drift apart.
        """
        radius = np.asarray(use_radius, dtype=np.float64)
        return distance_sq < radius * radius

    def _refresh_use_triggers(self):
        """The use-activated subset of the trigger list, in trigger order.

        Kept apart because the prompt for a use trigger is evaluated every
        tick while occupancy for everything else stays on the poll scheduler.
        Refreshed wherever the trigger list is rebuilt and again on each poll,
        so an activation mode changed at runtime is picked up.
        """
        # Tolerates being called before the trigger list exists: state reset
        # runs during construction, ahead of the first cache build.
        self._use_trigger_entries = [
            (bid, brush) for bid, brush in getattr(self, '_trigger_brushes', ())
            if str(brush.get('trigger_activation', 'touch')).lower() == 'use'
        ]

    def _use_prompt_candidates(self):
        """(bid, brush, centre, radius) for every use trigger a prompt may name."""
        for bid, brush in self._use_trigger_entries:
            if brush.get('disabled', False):
                continue
            if 'player' not in self._trigger_filters(brush):
                continue
            # A spent 'once' trigger does nothing, so it must not keep
            # advertising itself -- 2.4.2 suppressed the prompt for exactly
            # this case and the rewrite dropped the check.
            if (str(brush.get('trigger_type', 'multiple')).lower() == 'once'
                    and bid in self.fired_once_triggers):
                continue
            centre = brush.get('pos', (0.0, 0.0, 0.0))
            yield (bid, brush,
                   (float(centre[0]), float(centre[1]), float(centre[2])),
                   float(brush.get('use_radius', 96.0)))

    def _sample_use_prompt(self):
        """The '[E] ...' line for the use trigger the player is facing, now.

        Occupancy for touch triggers is the expensive pass -- every entity
        against every trigger -- and stays on the poll scheduler. This is only
        the use-activated subset, which is buttons, and it is evaluated against
        the live player position and angle so the prompt appears and clears the
        moment the player moves or turns instead of up to a poll interval
        later. The arithmetic is one batched pass over that subset.
        """
        player = self.player
        if player is None or not self._use_trigger_entries:
            return ""

        candidates = list(self._use_prompt_candidates())
        if not candidates:
            return ""

        centres = np.asarray([c[2] for c in candidates], dtype=np.float64)
        radii = np.asarray([c[3] for c in candidates], dtype=np.float64)
        pos = player.pos
        origin = np.asarray(
            (float(pos[0]), float(pos[1]), float(pos[2])), dtype=np.float64)

        offset = centres - origin
        distance_sq = np.einsum('ij,ij->i', offset, offset)
        in_range = self.use_trigger_contains(distance_sq, radii)
        if not in_range.any():
            return ""

        forward = np.asarray(
            (math.sin(player.angle), 0.0, math.cos(player.angle)),
            dtype=np.float64)
        # Facing is undefined when the player stands on the trigger centre;
        # 2.4.2 and the poll path both treat that as facing it.
        coincident = distance_sq <= 1.0e-8
        with np.errstate(invalid='ignore', divide='ignore'):
            facing = (offset @ forward) / np.sqrt(distance_sq)
        usable = in_range & (coincident | (facing > 0.5))
        if not usable.any():
            return ""

        index = int(np.flatnonzero(usable)[0])
        label = candidates[index][1].get('use_label', '') or 'Activate'
        return f"[E] {label}"

    def _trigger_poll_interval(self, brush):
        """Return a valid per-trigger polling interval in seconds."""
        try:
            value = float(brush.get('trigger_poll_interval', 1.0))
        except (TypeError, ValueError):
            return 1.0

        allowed = (1.0, 0.5, 0.25)
        return min(allowed, key=lambda interval: abs(interval - value))

    def _poll_triggers(self, use_key_pressed=False, trigger_ids=None):
        """Run one batched trigger poll for the triggers that are due.

        The scheduler wakes every 0.25 s, but only triggers whose configured
        polling interval has elapsed are included in the NumPy broad-phase.
        With the default 1.0 s setting this preserves the old 1 Hz workload.
        """
        if not self.player:
            return

        if use_key_pressed:
            self._trigger_use_generation += 1

        if trigger_ids is None:
            trigger_ids = {
                bid for bid, _ in self._trigger_brushes
            }
        else:
            trigger_ids = set(trigger_ids)

        if not trigger_ids:
            return

        # The use-activated subset can change if a brush's activation mode is
        # edited mid-session; refreshing it here keeps the per-tick prompt pass
        # correct without walking the whole trigger list every frame.
        self._refresh_use_triggers()

        # Snapshot the trigger AABBs due for this poll.
        trigger_entries = []
        polled_ids = set()
        for bid, brush in self._trigger_brushes:
            if bid not in trigger_ids:
                continue

            polled_ids.add(bid)
            if brush.get('disabled', False):
                continue

            activation = brush.get('trigger_activation', 'touch').lower()
            if activation == 'use':
                center = brush.get('pos', (0.0, 0.0, 0.0))
                radius = float(brush.get('use_radius', 96.0))
                bounds = (
                    float(center[0]) - radius,
                    float(center[1]) - radius,
                    float(center[2]) - radius,
                    float(center[0]) + radius,
                    float(center[1]) + radius,
                    float(center[2]) + radius,
                )
            else:
                bounds = brush_aabb_bounds(brush)

            filters = self._trigger_filters(brush)
            filter_mask = (
                (1 if 'player' in filters else 0) |
                (2 if 'props' in filters else 0) |
                (4 if 'monsters' in filters else 0)
            )
            if filter_mask:
                trigger_entries.append(
                    (bid, brush, bounds, filter_mask, activation)
                )

        # Preserve contacts for triggers that were not due. Replace only the
        # state belonging to triggers sampled on this pass.
        new_contacts = dict(self._trigger_contacts)
        for bid in polled_ids:
            new_contacts.pop(bid, None)

        if not trigger_entries:
            self._trigger_contacts = new_contacts
            self.player_in_triggers = {
                bid for bid, contacts in new_contacts.items()
                if any(entity_type == 'player' for entity_type, _ in contacts)
            }
            self._nonplayer_trigger_contacts = {
                bid: {
                    contact for contact in contacts
                    if contact[0] != 'player'
                }
                for bid, contacts in new_contacts.items()
                if any(contact[0] != 'player' for contact in contacts)
            }
            return

        # ------------------------------------------------------------------
        # Snapshot ALL eligible entities into one compact array.
        # The first row is always the player; props and monsters follow.
        # ------------------------------------------------------------------
        entities = [self.player]
        entity_types = [1]  # player
        entity_ids = [id(self.player)]

        for entity in (self._props.props if self._props is not None else ()):
            if not getattr(entity, 'properties', {}).get('disabled', False):
                entities.append(entity)
                entity_types.append(2)
                entity_ids.append(id(entity))

        for entity in self._monster_things:
            if not getattr(entity, 'properties', {}).get('disabled', False):
                entities.append(entity)
                entity_types.append(4)
                entity_ids.append(id(entity))

        positions = np.asarray(
            [entity.pos for entity in entities],
            dtype=np.float32,
        )
        entity_type_mask = np.asarray(entity_types, dtype=np.uint8)

        # ------------------------------------------------------------------
        # ONE vectorised broad-phase over only the trigger subset that is due.
        # No Python entity × trigger nested loop.
        # ------------------------------------------------------------------
        trigger_bounds = np.asarray(
            [entry[2] for entry in trigger_entries],
            dtype=np.float32,
        )
        inside = (
            (positions[:, None, 0] >= trigger_bounds[None, :, 0]) &
            (positions[:, None, 0] <= trigger_bounds[None, :, 3]) &
            (positions[:, None, 1] >= trigger_bounds[None, :, 1]) &
            (positions[:, None, 1] <= trigger_bounds[None, :, 4]) &
            (positions[:, None, 2] >= trigger_bounds[None, :, 2]) &
            (positions[:, None, 2] <= trigger_bounds[None, :, 5])
        )

        trigger_filter_masks = np.asarray(
            [entry[3] for entry in trigger_entries],
            dtype=np.uint8,
        )
        inside &= (
            (entity_type_mask[:, None] & trigger_filter_masks[None, :]) != 0
        )

        # Sparse result: only actual overlaps are materialised from NumPy.
        entity_indices, trigger_indices = np.nonzero(inside)

        for entity_index, trigger_index in zip(entity_indices, trigger_indices):
            entry = trigger_entries[int(trigger_index)]
            bid = entry[0]
            entity_type = entity_type_mask[int(entity_index)]
            category = (
                'player' if entity_type == 1
                else 'props' if entity_type == 2
                else 'monsters'
            )
            new_contacts.setdefault(bid, set()).add(
                (category, entity_ids[int(entity_index)])
            )

        old_contacts = self._trigger_contacts

        # ------------------------------------------------------------------
        # Only changed contacts generate trigger enter/exit I/O.
        # Unchanged occupancy produces no I/O work.
        # ------------------------------------------------------------------
        for bid in polled_ids:
            old = old_contacts.get(bid, set())
            new = new_contacts.get(bid, set())
            entered = new - old
            exited = old - new

            if entered:
                brush = self._trigger_brush_by_bid.get(bid)
                if brush:
                    for activator_type, entity_id in entered:
                        if activator_type == 'player':
                            activator = self.player
                        elif activator_type == 'props':
                            activator = (self._props.by_id(entity_id)
                                         if self._props is not None else None)
                        else:
                            activator = self._monster_by_id.get(entity_id)

                        if activator is not None:
                            # Use triggers are activation-driven rather than
                            # touch-state-driven; their broad-phase contact is
                            # handled below, but it must not fire OnStartTouch
                            # merely because the player entered its AABB.
                            if brush.get('trigger_activation', 'touch').lower() != 'use':
                                self._on_trigger_enter(
                                    brush,
                                    bid,
                                    activator_type=activator_type,
                                    activator_entity=activator,
                                )

            if exited:
                brush = self._trigger_brush_by_bid.get(bid)
                if brush:
                    for activator_type, entity_id in exited:
                        if activator_type == 'player':
                            activator = self.player
                        elif activator_type == 'props':
                            activator = (self._props.by_id(entity_id)
                                         if self._props is not None else None)
                        else:
                            activator = self._monster_by_id.get(entity_id)

                        if activator is not None:
                            if brush.get('trigger_activation', 'touch').lower() != 'use':
                                self._on_trigger_exit(
                                    brush,
                                    bid,
                                    activator_type=activator_type,
                                    activator_entity=activator,
                                )

                    # A player leaving a hurt trigger clears its cadence.
                    if any(entity_type == 'player' for entity_type, _ in exited):
                        self.hurt_trigger_timers.pop(bid, None)

        self._trigger_contacts = new_contacts

        # Maintain the legacy mirrors from the same sampled contact state.
        self.player_in_triggers = {
            bid for bid, contacts in new_contacts.items()
            if any(entity_type == 'player' for entity_type, _ in contacts)
        }
        self._nonplayer_trigger_contacts = {
            bid: {
                contact for contact in contacts
                if contact[0] != 'player'
            }
            for bid, contacts in new_contacts.items()
            if any(contact[0] != 'player' for contact in contacts)
        }

        # ------------------------------------------------------------------
        # Use triggers: broad-phase already identified candidate player
        # contacts. Facing and the queued use-key generation are the
        # narrow-phase.
        # ------------------------------------------------------------------
        for trigger_index, entry in enumerate(trigger_entries):
            bid, brush, bounds, _, activation = entry
            if activation != 'use':
                continue

            generation = self._trigger_use_generation
            last_seen = self._trigger_use_seen.get(bid, generation)
            use_edge = last_seen < generation
            self._trigger_use_seen[bid] = generation

            if not inside[0, trigger_index]:
                continue
            if not use_edge:
                continue

            center = np.asarray(
                brush.get('pos', (0.0, 0.0, 0.0)),
                dtype=np.float32,
            )
            offset = center - positions[0]
            distance_sq = float(np.dot(offset, offset))
            # The sphere is what decides; the broad-phase box only nominated
            # this trigger as a candidate. Same predicate the prompt uses, so
            # what the player is shown and what pressing E does cannot drift.
            if not self.use_trigger_contains(
                    distance_sq, float(brush.get('use_radius', 96.0))):
                continue
            if distance_sq > 1.0e-8:
                to_trigger = offset / math.sqrt(distance_sq)
                p_forward = np.asarray(
                    [math.sin(self.player.angle), 0.0, math.cos(self.player.angle)],
                    dtype=np.float32,
                )
                if float(np.dot(p_forward, to_trigger)) <= 0.5:
                    continue

            trigger_type = brush.get('trigger_type', 'multiple').lower()
            if trigger_type == 'once' and bid in self.fired_once_triggers:
                continue

            self._on_trigger_enter(
                brush,
                bid,
                activator_type='player',
                activator_entity=self.player,
            )

        # ------------------------------------------------------------------
        # Persistent hurt triggers are evaluated only for triggers sampled on
        # this pass, never on the 60 Hz logic path.
        # ------------------------------------------------------------------
        for bid in polled_ids:
            if bid not in self.player_in_triggers:
                continue
            brush = self._trigger_brush_by_bid.get(bid)
            if (
                brush
                and brush.get('trigger_action') == 'hurt'
                and brush.get('trigger_activation', 'touch').lower() != 'use'
            ):
                self._process_hurt_trigger(
                    brush,
                    bid,
                    self._trigger_poll_interval(brush),
                )

        # Use prompts are no longer sampled here: _sample_use_prompt evaluates
        # them every tick against the live player position and angle, which is
        # both more responsive and cheaper than carrying per-trigger prompt
        # state between polls.

    def _handle_triggers(self, use_key_pressed: bool, delta=None):
        """Schedule trigger polls without scanning occupancy at 60 Hz."""
        if use_key_pressed:
            self._trigger_use_generation += 1

        # The use prompt is evaluated here, every tick, against the live player
        # position and angle -- not republished from the last poll. Sampling it
        # at the poll cadence made it appear up to a poll interval late and
        # linger that long after the player turned away.
        #
        # Set only when there is one. _handle_triggers runs after
        # _handle_interactions and PropSession.tick in _tick_play_mode, so this
        # is the last word on the HUD line before the render state is
        # published; assigning unconditionally wiped the line those earlier
        # stages had just set, which is what silently removed "NEED: <key>",
        # "[E] Open", "[E] Unlock (...)", "[E] Pick up ...",
        # "[E] Complete Level" and "[E] Drop" from the HUD.
        self._trigger_use_prompt = self._sample_use_prompt()
        if self._trigger_use_prompt:
            self.current_hud_message = self._trigger_use_prompt

        step = float(delta) if delta is not None else float(self.TICK_DURATION)
        self._trigger_poll_elapsed += max(0.0, step)

        scheduler_tick = self.TRIGGER_POLL_TICK
        # Tolerance: 15 x (1/60) sums to 0.2499999..., which would otherwise
        # push every poll one logic tick late (same epsilon as per-trigger).
        while self._trigger_poll_elapsed + self.TRIGGER_POLL_EPSILON >= scheduler_tick:
            self._trigger_poll_elapsed = max(0.0, self._trigger_poll_elapsed - scheduler_tick)

            due_ids = set()
            for bid, brush in self._trigger_brushes:
                elapsed = (
                    self._trigger_poll_elapsed_by_bid.get(bid, 0.0)
                    + scheduler_tick
                )
                interval = self._trigger_poll_interval(brush)
                if elapsed + self.TRIGGER_POLL_EPSILON >= interval:
                    due_ids.add(bid)
                    elapsed %= interval
                self._trigger_poll_elapsed_by_bid[bid] = elapsed

            if due_ids:
                self._poll_triggers(trigger_ids=due_ids)

    def _apply_player_damage(self, damage):
        with self._player_damage_lock:
            if self.god_mode:
                return
            was_alive = self.player_health > 0
            self.player_health = max(0, self.player_health - damage)
            if self.buddha_mode and self.player_health < 2:
                self.player_health = 2
            became_dead = was_alive and self.player_health <= 0
        # Emit outside the lock so a handler can't deadlock on the damage path.
        self._plugin_emit("player_damage", damage=damage, health=self.player_health)
        if became_dead:
            self._plugin_emit("player_death")

    def _on_trigger_enter(
        self,
        brush: dict,
        trigger_id: int,
        activator_type='player',
        activator_entity=None,
    ):
        trigger_type = brush.get('trigger_type', 'multiple')
        if trigger_type == 'once' and trigger_id in self.fired_once_triggers:
            return

        action = brush.get('trigger_action', 'target')

        if action == 'teleport':
            target_node_name = brush.get('target_node', '')
            if target_node_name:
                node = self._find_path_node_by_name(target_node_name)
                if node and (activator_entity or self.player):
                    activator = activator_entity or self.player
                    dest = glm.vec3(node.pos[0], node.pos[1], node.pos[2])
                    if activator is self.player:
                        self.player.pos = dest
                        self.player.velocity = glm.vec3(0, 0, 0)
                    else:
                        activator.pos = [dest.x, dest.y, dest.z]
                        physics_world = getattr(self, '_physics_world', None)
                        if physics_world is not None:
                            try:
                                physics_world.sync_entity_position(activator, wake=True)
                            except (AttributeError, TypeError, ValueError):
                                pass
                    if self.io_manager:
                        self.io_manager.fire_output(
                            brush, 'OnTeleport', activator_entity=activator
                        )
                    debug_log("IO", f"Trigger teleported {activator_type} → '{target_node_name}' "
                                     f"({node.pos[0]:.0f}, {node.pos[1]:.0f}, {node.pos[2]:.0f})")
            else:
                debug_log("Warning", "Trigger action 'teleport' used but no target_node set.")

        elif action == 'hurt':
            # Only the player has damage/health semantics at present.
            if activator_type == 'player':
                damage = brush.get('damage', 10)
                self._apply_player_damage(damage)
                self.hurt_trigger_timers[trigger_id] = self.HURT_INTERVAL

        elif action == 'target':
            if self.io_manager:
                self.io_manager.fire_output(
                    brush, 'OnStartTouch', activator_entity=activator_entity
                )
                self.io_manager.fire_output(
                    brush, 'OnTrigger', activator_entity=activator_entity
                )

        self._plugin_emit(
            "trigger_enter",
            trigger=brush,
            action=action,
            trigger_id=trigger_id,
            activator_type=activator_type,
        )
        if trigger_type == 'once':
            self.fired_once_triggers.add(trigger_id)

    def _on_trigger_exit(
        self,
        brush: dict,
        trigger_id: int,
        activator_type='player',
        activator_entity=None,
    ):
        if self.io_manager:
            self.io_manager.fire_output(
                brush, 'OnEndTouch', activator_entity=activator_entity
            )
        self._plugin_emit(
            "trigger_exit",
            trigger=brush,
            trigger_id=trigger_id,
            activator_type=activator_type,
        )

    def _process_hurt_trigger(self, brush: dict, trigger_id: int, poll_interval=1.0):
        if trigger_id in self.hurt_trigger_timers:
            self.hurt_trigger_timers[trigger_id] -= float(poll_interval)
            if self.hurt_trigger_timers[trigger_id] <= 0:
                damage = brush.get('damage', 10)
                self._apply_player_damage(damage)
                self.hurt_trigger_timers[trigger_id] = self.HURT_INTERVAL

    # =========================================================================
    # INTERACTIONS
    # =========================================================================

    def _handle_interactions(self, use_key_pressed: bool):
        self.current_hud_message = ""
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
                        pretty_key_name = key_name.replace('_', ' ').title() if key_name else "Key"
                        if has_key:
                            self.current_hud_message = f"[E] Unlock ({pretty_key_name})"
                            if use_key_pressed:
                                self._trigger_door_open(found_door_idx, found_door_brush)
                                door_consumed_use = True
                        else:
                            self.current_hud_message = f"NEED: {pretty_key_name}"
                    else:
                        self.current_hud_message = "[E] Open"
                        if use_key_pressed:
                            self._trigger_door_open(found_door_idx, found_door_brush)
                            door_consumed_use = True


        if Pickup and not door_consumed_use:
            p_pos = glm.vec3(px, py, pz)
            p_forward = glm.vec3(math.sin(self.player.angle), 0, math.cos(self.player.angle))
            for thing in self._pickup_things:
                if thing.properties.get('collected', False):
                    continue
                if thing.properties.get('activation') != 'use':
                    continue
                if thing.properties.get('disabled', False):
                    continue
                if id(thing) in self.collected_pickups:
                    continue
                t_pos = glm.vec3(thing.pos)
                dist = glm.distance(p_pos, t_pos)
                if dist < 80.0:
                    to_thing = glm.normalize(t_pos - p_pos)
                    if glm.dot(p_forward, to_thing) > 0.8:
                        item_name = thing.properties.get('item_type', 'Item').replace('_', ' ').title()
                        self.current_hud_message = f"[E] Pick up {item_name}"
                        if use_key_pressed:
                            self._collect_pickup(thing)
                        return


        if not door_consumed_use:
            p_pos = glm.vec3(px, py, pz)
            p_forward = glm.vec3(math.sin(self.player.angle), 0, math.cos(self.player.angle))
            for thing in self._levelchanger_things:
                if thing.properties.get('disabled', False):
                    continue
                # skip if not usable
                if not thing.properties.get('usable', True):
                    continue
                t_pos = glm.vec3(thing.pos)
                dist = glm.distance(p_pos, t_pos)
                radius = float(thing.properties.get('radius', 128.0))
                if dist < radius:
                    to_thing = glm.normalize(t_pos - p_pos)
                    if glm.dot(p_forward, to_thing) > 0.5:
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
    # PICKUPS
    # =========================================================================

    def _check_pickups(self):
        self._handle_pickups(False)

    def _handle_pickups(self, use_key_pressed: bool):
        if not self.player or not Pickup:
            return
        player_pos = self.player.pos
        pickup_radius = 32.0

        for thing in self._pickup_things:
            if thing.properties.get('collected', False):
                continue
            if id(thing) in self.collected_pickups:
                continue
            if thing.properties.get('disabled', False):
                continue
            thing_pos = glm.vec3(thing.pos)
            distance = glm.distance(player_pos, thing_pos)
            if thing.properties.get('activation') == 'walk_over' and distance <= pickup_radius:
                self._collect_pickup(thing)
    
    def _collect_pickup(self, pickup):
        item_type = pickup.properties.get('item_type', 'health')
        value = pickup.properties.get('value', 25)
        if item_type == 'health':
            self.player_health = min(self.player_max_health, self.player_health + value)
        elif item_type == 'key':
            key_name = pickup.properties.get('key_name', '')
            if key_name:
                self.collected_keys.add(key_name)
        elif item_type in ['gun1', 'gun2', 'cig']:
            self.active_weapon = item_type
            self.current_hud_message = f"Picked up {item_type.upper()}"
        pickup.properties['collected'] = True
        pid = id(pickup)
        self.collected_pickups.add(pid)
        if self.io_manager:
            self.io_manager.fire_output(pickup, 'OnPickedUp')
        self._plugin_emit("pickup_collected", pickup=pickup,
                          item_type=item_type, value=value)
        if pickup.properties.get('respawns', False):
            respawn_time = pickup.properties.get('respawn_time', 20.0)
            self.respawn_timers[pid] = {
                'remaining': respawn_time,
                'entity': pickup,
            }
    
    def _update_respawns(self, delta: float):
        if not Pickup:
            return
        to_respawn = []
        for pid, timer_data in list(self.respawn_timers.items()):
            timer_data['remaining'] -= delta
            if timer_data['remaining'] <= 0:
                to_respawn.append(pid)
        for pid in to_respawn:
            timer_data = self.respawn_timers.pop(pid)
            entity = timer_data.get('entity')
            if entity is not None and isinstance(entity, Pickup):
                entity.properties['collected'] = False
                self.collected_pickups.discard(pid)
                if self.io_manager:
                    self.io_manager.fire_output(entity, 'OnRespawn')
    
    # =========================================================================
    # MOVER/DOOR UPDATES
    # =========================================================================

    def _update_movers(self, delta: float):
        for i, brush in self.movers:
            if brush.get('move_once', False):
                continue
            if not brush.get('start_on', False):
                continue

            if i in self.mover_path_states:
                self._update_mover_path(i, brush, delta)
                continue

            if brush.get('rotate', False):
                speed = brush.get('speed', 45.0)
                current = brush.get('_rot_angle', 0.0)
                new_angle = (current + speed * delta) % 360.0
                brush['_rot_angle'] = new_angle
                brush['rotation_yaw'] = new_angle

            if i not in self.mover_states:
                if 'original_pos' not in brush:
                    brush['original_pos'] = list(brush['pos'])
                self.mover_states[i] = {'progress': 0.0, 'forward': True}
            state = self.mover_states[i]
            speed = brush.get('speed', 64.0)
            distance = brush.get('distance', 128.0)
            # PERF: mover direction is static during play — normalize once
            # (via NumPy, for identical rounding) and cache as a plain scalar
            # tuple so the per-tick offset maths below is pure Python and never
            # rebuilds a small NumPy array each frame.
            direction = state.get('_direction_np')
            if direction is None:
                d = np.array(brush.get('direction', [0, 1, 0]), dtype=float)
                dir_length = np.linalg.norm(d)
                if dir_length > 0:
                    d = d / dir_length
                direction = (float(d[0]), float(d[1]), float(d[2]))
                state['_direction_np'] = direction
            progress_delta = (speed * delta) / distance if distance > 0 else 0
            was_at_end = state['progress'] >= 1.0
            was_at_start = state['progress'] <= 0.0
            if state['forward']:
                state['progress'] += progress_delta
                if state['progress'] >= 1.0:
                    state['progress'] = 1.0
                    state['forward'] = False
                    if not was_at_end and self.io_manager:
                        self.io_manager.fire_output(brush, 'OnFullyOpen')
            else:
                state['progress'] -= progress_delta
                if state['progress'] <= 0.0:
                    state['progress'] = 0.0
                    state['forward'] = True
                    if not was_at_start and self.io_manager:
                        self.io_manager.fire_output(brush, 'OnFullyClosed')
            t = state['progress']
            eased = 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2
            # PERF: scalar offset — bit-identical to the old NumPy expression
            # (original + direction*distance*eased, which associates as
            # (direction*distance)*eased), with no per-tick array allocation.
            original = brush['original_pos']
            cur = brush['pos']
            nx = original[0] + (direction[0] * distance) * eased
            ny = original[1] + (direction[1] * distance) * eased
            nz = original[2] + (direction[2] * distance) * eased
            brush['pos'] = [nx, ny, nz]
            if self.player and self.player.ground_object == brush:
                self.player.pos += glm.vec3(nx - cur[0], ny - cur[1], nz - cur[2])

    def _update_cinematic_camera(self, delta: float):
        cs = self.cinematic_state
        if not cs or not cs.get('active') or cs.get('paused'):
            return

        node_name = cs['current_node']
        node = self._find_path_node_by_name(node_name)
        if node is None:
            debug_log("IO", f"CinematicCamera: node '{node_name}' not found — aborting")
            entity = cs.get('entity')
            self.cinematic_state = None
            if entity and self.io_manager:
                self.io_manager.fire_output(entity, 'OnFinished')
            return

        origin = np.array(cs['origin'], dtype=float)
        target = np.array(node.pos, dtype=float)
        segment_vec = target - origin
        segment_len = np.linalg.norm(segment_vec)

        if segment_len < 1.0:
            cs['lerp_t'] = 1.0
        else:
            cs['lerp_t'] += (cs['speed'] * delta) / segment_len

        t = min(cs['lerp_t'], 1.0)
        current_pos = origin + segment_vec * t

        cs['cam_pos'] = current_pos.tolist()

        if cs.get('look_ahead'):
            next_name = node.get_next_node_name()
            look_node = self._find_path_node_by_name(next_name) if next_name else node
            look_target = np.array(look_node.pos if look_node else node.pos, dtype=float)
        else:
            look_target = target

        diff = look_target - current_pos
        dist = np.linalg.norm(diff)
        if dist > 0.01:
            cs['cam_angle'] = math.atan2(diff[0], diff[2])
            cs['cam_pitch'] = math.asin(np.clip(diff[1] / dist, -1.0, 1.0))

        if cs['lerp_t'] >= 1.0:
            if self.io_manager:
                self.io_manager.fire_output(cs['entity'], 'OnReachNode')

            next_name = node.get_next_node_name()
            if next_name:
                cs['origin'] = list(node.pos)
                cs['current_node'] = next_name
                cs['lerp_t'] = 0.0
            else:
                entity = cs['entity']
                self.cinematic_state = None
                if self.io_manager:
                    self.io_manager.fire_output(entity, 'OnFinished')

    def _update_mover_path(self, idx: int, brush: dict, delta: float):
        state = self.mover_path_states[idx]
        node_name = state['current_node']
        if not node_name:
            return

        node = self._find_path_node_by_name(node_name)
        if node is None:
            debug_log("IO", f"Mover path: node '{node_name}' not found — stopping")
            self.mover_path_states.pop(idx, None)
            return

        if state['waiting']:
            state['wait_remaining'] -= delta
            if state['wait_remaining'] <= 0.0:
                state['waiting'] = False
                next_name = node.get_next_node_name()
                if next_name:
                    state['origin'] = list(brush['pos'])
                    state['current_node'] = next_name
                    state['lerp_t'] = 0.0
                else:
                    brush['start_on'] = False
                    if self.io_manager:
                        self.io_manager.fire_output(brush, 'OnFullyClosed')
                    self.mover_path_states.pop(idx, None)
            return

        origin = np.array(state['origin'], dtype=float)
        target = np.array(node.pos, dtype=float)
        segment_vec = target - origin
        segment_len = np.linalg.norm(segment_vec)

        if segment_len < 1.0:
            state['lerp_t'] = 1.0
        else:
            speed = brush.get('speed', 64.0) * node.get_speed()
            state['lerp_t'] += (speed * delta) / segment_len

        if state['lerp_t'] >= 1.0:
            state['lerp_t'] = 1.0
            new_pos = target
            move_delta = new_pos - np.array(brush['pos'])
            brush['pos'] = new_pos.tolist()

            if self.player and self.player.ground_object == brush:
                self.player.pos += glm.vec3(float(move_delta[0]), float(move_delta[1]), float(move_delta[2]))

            if self.io_manager:
                # Per-node arrival event (fires at every PathNode in the chain),
                # plus OnFullyOpen for backward compatibility with existing maps.
                self.io_manager.fire_output(brush, 'OnPathNodeReached', value=node_name)
                self.io_manager.fire_output(brush, 'OnFullyOpen')

            wait_time = node.get_wait_time()
            if wait_time > 0.0:
                state['waiting'] = True
                state['wait_remaining'] = wait_time
            else:
                next_name = node.get_next_node_name()
                if next_name:
                    state['origin'] = list(brush['pos'])
                    state['current_node'] = next_name
                    state['lerp_t'] = 0.0
                else:
                    if self.io_manager:
                        self.io_manager.fire_output(brush, 'OnFullyClosed')
                    self.mover_path_states.pop(idx, None)
        else:
            t = state['lerp_t']
            eased = 4 * t * t * t if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2
            new_pos = origin + segment_vec * eased
            move_delta = new_pos - np.array(brush['pos'])
            brush['pos'] = new_pos.tolist()

            if self.player and self.player.ground_object == brush:
                self.player.pos += glm.vec3(float(move_delta[0]), float(move_delta[1]), float(move_delta[2]))

    def _update_doors(self, delta: float):
        for i, brush in self.doors:
            if i not in self.door_states:
                continue
            state = self.door_states[i]
            # PERF: fully-closed, idle doors cost nothing until triggered.
            if state['state'] == 'closed' and state['progress'] == 0.0:
                continue
            speed = state.get('speed', 128.0)
            distance = state.get('distance', 128.0)
            open_time = brush.get('open_time', 3.0)
            # PERF: direction is precomputed (already unit-length) in
            # _init_doors — no need to renormalize every tick. Cached as a
            # scalar tuple so the offset maths below allocates no NumPy arrays.
            direction = state.get('_direction_np')
            if direction is None:
                d = np.array(state.get('direction', [0, 1, 0]), dtype=float)
                dir_length = np.linalg.norm(d)
                if dir_length > 0:
                    d = d / dir_length
                direction = (float(d[0]), float(d[1]), float(d[2]))
                state['_direction_np'] = direction
            progress_delta = (speed * delta) / distance if distance > 0 else 0
            if state['state'] == 'opening':
                state['progress'] += progress_delta
                if state['progress'] >= 1.0:
                    state['progress'] = 1.0
                    state['state'] = 'open'
                    state['open_timer'] = open_time
                    if self.io_manager:
                        self.io_manager.fire_output(brush, 'OnFullyOpen')
            elif state['state'] == 'open':
                state['open_timer'] -= delta
                if state['open_timer'] <= 0:
                    state['state'] = 'closing'
                    if self.io_manager:
                        self.io_manager.fire_output(brush, 'OnClose')
            elif state['state'] == 'closing':
                state['progress'] -= progress_delta
                if state['progress'] <= 0.0:
                    state['progress'] = 0.0
                    state['state'] = 'closed'
                    if self.io_manager:
                        self.io_manager.fire_output(brush, 'OnFullyClosed')
            # PERF: scalar offset — bit-identical to the old NumPy expression
            # (original + direction*distance*progress), no per-tick array alloc.
            original = brush['original_pos']
            cur = brush['pos']
            progress = state['progress']
            nx = original[0] + (direction[0] * distance) * progress
            ny = original[1] + (direction[1] * distance) * progress
            nz = original[2] + (direction[2] * distance) * progress
            brush['pos'] = [nx, ny, nz]
            if self.player and self.player.ground_object == brush:
                self.player.pos += glm.vec3(nx - cur[0], ny - cur[1], nz - cur[2])

    # =========================================================================
    # PARENTED LIGHTS
    # =========================================================================

    def _init_parented_lights(self):
        self._parented_lights = []
        if not Light:
            return
        for thing in self.things:
            if not isinstance(thing, Light):
                continue
            parent_name = thing.properties.get('parent_mover', '')
            if not parent_name:
                continue
            brush = None
            for b in self.brushes:
                if b.get('is_mover') and b.get('name') == parent_name:
                    brush = b
                    break
            if brush is None:
                print(f"[Light] Warning: parent_mover '{parent_name}' not found for light '{thing.name}'")
                continue
            thing.properties['_original_pos'] = list(thing.pos)
            offset = thing.properties.get('parent_offset')
            if not offset or offset == [0.0, 0.0, 0.0]:
                offset = [
                    thing.pos[0] - brush['pos'][0],
                    thing.pos[1] - brush['pos'][1],
                    thing.pos[2] - brush['pos'][2],
                ]
                thing.properties['parent_offset'] = offset
            self._parented_lights.append((thing, brush, offset))

    def _reset_parented_lights(self):
        for light, _brush, _offset in self._parented_lights:
            original = light.properties.pop('_original_pos', None)
            if original is not None:
                light.pos = list(original)
        self._parented_lights = []

    def _update_parented_lights(self):
        for light, brush, offset in self._parented_lights:
            bpos = brush['pos']
            light.pos[0] = bpos[0] + offset[0]
            light.pos[1] = bpos[1] + offset[1]
            light.pos[2] = bpos[2] + offset[2]


    # =========================================================================
    # PARENTED PORTALS (FIX: full transformation including rotation)
    # =========================================================================

    def _init_parented_portals(self):
        self._parented_portals = []
        if Portal is None:
            return
        for thing in self.things:
            if not isinstance(thing, Portal):
                continue
            parent_name = thing.properties.get('parent_mover', '')
            if not parent_name:
                continue
            brush = None
            for b in self.brushes:
                if b.get('is_mover') and b.get('name') == parent_name:
                    brush = b
                    break
            if brush is None:
                print(f"[Portal] Warning: parent_mover '{parent_name}' not found for portal '{thing.properties.get('name', '')}'")
                continue

            thing.properties['_original_pos'] = list(thing.pos)
            thing.properties['_original_yaw'] = thing.get_yaw_degrees()

            if thing.properties.get('parent_local_pos') is None:
                mover_yaw = brush.get('rotation_yaw', 0.0)
                thing.set_parent_local_transform(brush['pos'], mover_yaw)

            local_pos = thing.get_parent_local_pos()
            local_yaw = thing.get_parent_local_yaw()

            self._parented_portals.append((thing, brush, local_pos, local_yaw))

    def _reset_parented_portals(self):
        for portal, _brush, _local_pos, _local_yaw in self._parented_portals:
            original = portal.properties.pop('_original_pos', None)
            if original is not None:
                portal.pos = list(original)
            original_yaw = portal.properties.pop('_original_yaw', None)
            if original_yaw is not None:
                portal.set_yaw_degrees(original_yaw)
        self._parented_portals = []

    def _update_parented_portals(self):
        for portal, brush, local_pos, local_yaw in self._parented_portals:
            mover_pos = brush['pos']
            mover_yaw = brush.get('rotation_yaw', 0.0)

            yaw_rad = math.radians(mover_yaw)
            cos_y = math.cos(yaw_rad)
            sin_y = math.sin(yaw_rad)
            world_x = mover_pos[0] + local_pos[0] * cos_y - local_pos[2] * sin_y
            world_z = mover_pos[2] + local_pos[0] * sin_y + local_pos[2] * cos_y
            portal.pos[0] = world_x
            portal.pos[1] = mover_pos[1] + local_pos[1]
            portal.pos[2] = world_z

            portal.set_yaw_degrees(mover_yaw + local_yaw)

    # =========================================================================
    # PLAYER SHOOTING
    # =========================================================================

    def _handle_shooting(self):
        if not self.player or not self.active_weapon:
            return
        # Non-firing weapons (e.g. cig) never fire: no muzzle flash, no
        # hitscan/projectile, no damage, and no gunfire noise event.
        if self.active_weapon in NON_FIRING_WEAPONS:
            return
        self.muzzle_flash_active = True
        self._plugin_emit("player_shoot", weapon=self.active_weapon)
        yaw_rad = self.player.angle
        if self.is_overhead():
            # Top-down aiming is planar: the player rotates to face a target and
            # fires along that ground heading. The overhead camera and sprite
            # both ignore pitch, so there is no way to aim vertically — folding
            # pitch into the ray would just tilt shots into the sky or floor and
            # make monsters (which stand on the ground plane) nearly unhittable.
            # Keep the ray horizontal at eye height so it can actually connect.
            dir_x = math.sin(yaw_rad)
            dir_y = 0.0
            dir_z = math.cos(yaw_rad)
        else:
            pitch_rad = self.player.pitch
            dir_x = math.sin(yaw_rad) * math.cos(pitch_rad)
            dir_y = math.sin(pitch_rad)
            dir_z = math.cos(yaw_rad) * math.cos(pitch_rad)
        ray_origin = glm.vec3(self.player.pos.x,
                              self.player.pos.y + self.player.camera_height,
                              self.player.pos.z)
        ray_dir = glm.normalize(glm.vec3(dir_x, dir_y, dir_z))
        closest_brush_hit = None
        closest_brush_dist = float('inf')
        collision_brushes = self._collision_brushes_cache
        for brush in collision_brushes:
            if (brush.get('is_trigger') or brush.get('hidden') or
                is_water_brush(brush) or brush.get('is_fog')):
                continue
            pos = glm.vec3(brush['pos'])
            size = glm.vec3(brush['size'])
            min_b = pos - size * 0.5
            max_b = pos + size * 0.5
            hit, dist = self.intersect_ray_aabb(ray_origin, ray_dir, min_b, max_b)
            if hit and dist < closest_brush_dist:
                closest_brush_dist = dist
                closest_brush_hit = ray_origin + ray_dir * dist
        
        # Raycast against monsters — acquire lock for consistent positions
        closest_monster = None
        closest_monster_dist = float('inf')
        
        with self._monster_lock:
            for thing in self.things:
                if not isinstance(thing, MonsterThing):
                    continue
                if thing.properties.get('dead', False) or thing.properties.get('hidden', False):
                    continue
                sprite_width = float(thing.properties.get('sprite_width', 64.0))
                sprite_height = float(thing.properties.get('sprite_height', 128.0))
                # Use a wider, more forgiving hit box for better gameplay feel
                # Width matters more than height for shooting comfort
                radius = max(sprite_width * 0.75, sprite_height * 0.4, 48.0)
                center = glm.vec3(thing.pos[0], thing.pos[1] + sprite_height * 0.45, thing.pos[2])
                oc = ray_origin - center
                a = glm.dot(ray_dir, ray_dir)
                b = 2.0 * glm.dot(oc, ray_dir)
                c = glm.dot(oc, oc) - radius * radius
                disc = b * b - 4 * a * c
                if disc >= 0:
                    t = (-b - math.sqrt(disc)) / (2.0 * a)
                    if t >= 0 and t < closest_monster_dist:
                        if t < closest_brush_dist:
                            closest_monster_dist = t
                            closest_monster = thing

            if closest_monster is not None:
                damage = WEAPON_DAMAGE.get(self.active_weapon, 25)
                health_raw = closest_monster.properties.get('health', 100)
                try:
                    health = int(health_raw)
                except (ValueError, TypeError):
                    health = 100
                new_health = health - damage
                closest_monster.properties['health'] = new_health
                debug_log("MonsterAI", f"Monster {closest_monster.properties.get('name')} health: {health} -> {new_health} (weapon={self.active_weapon}, dmg={damage})")
                self.game_state.queue_sound({
                    'file': 'hit.wav',
                    'volume': 1.0,
                    'entity_id': id(closest_monster)
                })
                if self.io_manager:
                    self.io_manager.fire_output(closest_monster, 'OnDamaged')
                if new_health <= 0:
                    closest_monster.properties['dead'] = True
                    closest_monster.properties.pop('is_shooting', None)
                    if self.io_manager:
                        self.io_manager.fire_output(closest_monster, 'OnDeath')
                    if self.monster_ai.monster_debug_active:
                        name = closest_monster.properties.get('name', '?')
                        debug_log("MonsterAI",
                            f'<a href="filter:{name}" style="color: #EF5350; font-weight: bold; text-decoration: none;">{name}</a> '
                            f'<span style="color: #B71C1C; font-weight: bold;">DIED</span> (shot by player)')
                return
        
        # Record gunfire sound event for AI hearing
        self._emit_noise_event(
            [ray_origin.x, ray_origin.y, ray_origin.z],
            source='gunfire', loudness=_GUNFIRE_LOUDNESS)

        if closest_brush_hit is not None:
            self.bullet_marks.append({
                'pos': closest_brush_hit,
                'time': time.perf_counter()
            })

    def intersect_ray_aabb(self, origin, direction, box_min, box_max):
        t_min = 0.0
        t_max = 10000.0
        for i in range(3):
            if abs(direction[i]) < 1e-6:
                if origin[i] < box_min[i] or origin[i] > box_max[i]:
                    return False, 0
            else:
                inv_d = 1.0 / direction[i]
                t1 = (box_min[i] - origin[i]) * inv_d
                t2 = (box_max[i] - origin[i]) * inv_d
                t_near = min(t1, t2)
                t_far = max(t1, t2)
                t_min = max(t_min, t_near)
                t_max = min(t_max, t_far)
                if t_min > t_max:
                    return False, 0
        return True, t_min

    def _update_bullet_marks(self):
        current_time = time.perf_counter()
        self.bullet_marks = [
            m for m in self.bullet_marks 
            if (current_time - m['time']) < self.BULLET_FADE_TIME
        ]

    # =========================================================================
    # MONSTER PROJECTILES (flying monster ranged attacks)
    # =========================================================================

    def _update_monster_projectiles(self, delta: float):
        """Update all active monster projectiles: move, check collisions, apply damage."""
        if not hasattr(self, '_monster_projectiles'):
            return

        remaining = []
        if self._monster_projectiles:
            # PERF: reuse the cached combined collision-brush list instead of
            # rebuilding it (was previously rebuilt once per projectile).
            all_collision_brushes = self._collision_brushes_cache
            owner_team_by_id = {}
            with self._monster_lock:
                for t in self.things:
                    if isinstance(t, MonsterThing):
                        owner_team_by_id[id(t)] = t.properties.get('team', '')
        for proj in self._monster_projectiles:
            # Update position
            vel = proj['vel']
            prev_pos = (proj['pos'][0], proj['pos'][1], proj['pos'][2])
            proj['pos'][0] += vel[0] * delta
            proj['pos'][1] += vel[1] * delta
            proj['pos'][2] += vel[2] * delta

            # Route the projectile through any portal it crossed this step, so
            # ranged attacks can travel between linked portals like the player.
            self._transit_projectile_through_portals(proj, prev_pos)

            # Track distance travelled
            speed = math.sqrt(vel[0]**2 + vel[1]**2 + vel[2]**2)
            proj['distance_travelled'] += speed * delta

            # Decrease lifetime
            proj['lifetime'] -= delta

            # Check max distance
            if proj['distance_travelled'] >= MONSTER_PROJECTILE_MAX_DIST:
                continue  # Expired

            if proj['lifetime'] <= 0.0:
                continue  # Expired

            p_pos = glm.vec3(proj['pos'][0], proj['pos'][1], proj['pos'][2])

            # ---- Collision with player ----
            if self.player and not self.god_mode and not self.player_dead:
                player_pos = self.player.pos
                # Simple sphere collision with player (radius ~32 units)
                dist_to_player = glm.distance(p_pos, player_pos)
                if dist_to_player < 32.0:
                    damage = proj['damage']
                    self._apply_player_damage(damage)
                    if self.monster_ai.monster_debug_active:
                        debug_log("MonsterAI", f"Projectile hit player for {damage} dmg")
                    continue  # Projectile consumed

            # ---- Collision with monsters (team-aware) ----
            owner_id = proj['owner_id']
            hit_monster = None
            
            owner_team = owner_team_by_id.get(owner_id)
            with self._monster_lock:
                for thing in self.things:
                    if not isinstance(thing, MonsterThing):
                        continue
                    if id(thing) == owner_id:
                        continue  # Don\'t hit self
                    if thing.properties.get('dead', False) or thing.properties.get('hidden', False):
                        continue

                    # Team-aware: don\'t hit same-team allies
                    target_team = thing.properties.get('team', '')
                    if owner_team and target_team and owner_team == target_team:
                        continue

                    t_pos = glm.vec3(thing.pos[0], thing.pos[1] + 64.0, thing.pos[2])
                    dist = glm.distance(p_pos, t_pos)
                    if dist < 64.0:  # Monster hit radius (increased for better feel)
                        hit_monster = thing
                        break

                if hit_monster is not None:
                    damage = proj['damage']
                    self.monster_ai._apply_monster_damage(hit_monster, damage, attacker=None)
                    if self.monster_ai.monster_debug_active:
                        name = hit_monster.properties.get('name', '?')
                        debug_log("MonsterAI", f"Projectile hit {name} for {damage} dmg")
                    continue  # Projectile consumed

            # ---- Collision with solid brushes (walls) ----
            hit_wall = False
            # PERF: narrow candidates via the spatial grid (same brush set
            # and filtering as populate()) instead of scanning every brush.
            grid = getattr(self, '_spatial_grid', None)
            if grid is not None:
                wall_candidates = grid.get_nearby_brushes(p_pos.x, p_pos.z)
            else:
                wall_candidates = all_collision_brushes
            for brush in wall_candidates:
                if not is_solid_world_brush(brush):
                    continue
                pos = brush['pos']
                size = brush['size']
                bx_min = pos[0] - size[0] * 0.5
                bx_max = pos[0] + size[0] * 0.5
                by_min = pos[1] - size[1] * 0.5
                by_max = pos[1] + size[1] * 0.5
                bz_min = pos[2] - size[2] * 0.5
                bz_max = pos[2] + size[2] * 0.5

                if (bx_min <= p_pos.x <= bx_max and
                    by_min <= p_pos.y <= by_max and
                    bz_min <= p_pos.z <= bz_max):
                    hit_wall = True
                    break

            if hit_wall:
                continue  # Projectile consumed

            # Projectile survived this tick
            remaining.append(proj)

        self._monster_projectiles = remaining

        # Sync projectiles to render state for visualisation
        write_state = self.game_state.get_write_state()
        write_state.projectiles = [
            {
                'pos': list(proj['pos']),
                'sprite': proj.get('sprite', 'projectile.png'),
                'size': proj.get('size', MONSTER_PROJECTILE_SPRITE_SIZE),
            }
            for proj in remaining
        ]


    # =========================================================================
    # GUNFIRE SOUND EVENTS (for AI hearing)
    # =========================================================================

    def _emit_noise_event(self, pos, source: str, loudness: float = 1.0):
        """Record an audible player action so hearing monsters can react.

        Stored in the shared player-noise list (self._gunfire_events); every
        event carries a position, timestamp, a source tag and a loudness
        multiplier that scales how far it can be heard. Used by the monster
        AI both to wake sleeping monsters and to steer awake ones toward the
        source (see MonsterAI._hears_noise / _investigate_sounds).
        """
        self._gunfire_events.append({
            'pos': [float(pos[0]), float(pos[1]), float(pos[2])],
            'time': time.perf_counter(),
            'source': source,
            'loudness': float(loudness),
        })
        self._plugin_emit("noise", pos=[float(pos[0]), float(pos[1]), float(pos[2])],
                          source=source, loudness=float(loudness))

    def get_recent_noise_events(self, max_age: float = 3.0) -> list:
        current_time = time.perf_counter()
        return [
            e for e in self._gunfire_events
            if (current_time - e['time']) < max_age
        ]

    # Backwards-compatible alias: the noise list started as gunfire-only.
    def get_recent_gunfire_events(self, max_age: float = 3.0) -> list:
        return self.get_recent_noise_events(max_age)

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
        c = np.asarray(centers, dtype=np.float64)
        h = np.asarray(halves, dtype=np.float64)
        if c.size == 0:
            return np.ones(len(centers), dtype=bool)
        p = np.asarray(planes, dtype=np.float64)        # (6, 4)
        normals = p[:, :3]                               # (6, 3)
        d = p[:, 3]                                       # (6,)
        # Positive-vertex distance for every (box, plane) pair, branch-free:
        #   dot(n, c + sign(n)*h) + d  ==  dot(n, c) + dot(|n|, h) + d
        dist = c @ normals.T + h @ np.abs(normals).T + d  # (N, 6)
        return np.all(dist >= 0.0, axis=1)

    # =========================================================================
    # RENDER STATE PREPARATION
    # =========================================================================

    def _prepare_render_state(self):
        write_state = self.game_state.get_write_state()
        write_state.is_play_mode = self.play_mode

        if self.play_mode and self.player:
            cs = self.cinematic_state
            if cs and 'cam_pos' in cs:
                cam_pos = glm.vec3(*cs['cam_pos'])
                cam_angle = cs.get('cam_angle', 0.0)
                cam_pitch = cs.get('cam_pitch', 0.0)
                direction = glm.vec3(
                    math.sin(cam_angle) * math.cos(cam_pitch),
                    math.sin(cam_pitch),
                    math.cos(cam_angle) * math.cos(cam_pitch),
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
                    a = self._camera_for_mode(ct['from_overhead'], player_pos,
                                              player_angle, player_pitch, camera_height)
                    b = self._camera_for_mode(ct['to_overhead'], player_pos,
                                              player_angle, player_pitch, camera_height)
                    cam_pos = a[0] + (b[0] - a[0]) * t
                    direction = a[1] + (b[1] - a[1]) * t
                    if glm.length(direction) < 1e-8:
                        direction = b[1]
                    direction = glm.normalize(direction)
                    up_vec = self._safe_up(direction, a[2] + (b[2] - a[2]) * t)
                    view_matrix = glm.lookAt(cam_pos, cam_pos + direction, up_vec)
                    fov = a[3] + (b[3] - a[3]) * t
                elif self.is_overhead():
                    # Native top-down camera. The frustum planes below are built
                    # from this view_matrix, so overhead culling is correct; the
                    # up hint is horizontal, avoiding the straight-down lookAt
                    # degeneracy that would corrupt the view and every plane.
                    cam_pos, direction, up_vec = self._overhead_camera(player_pos, player_angle)
                    view_matrix = glm.lookAt(cam_pos, cam_pos + direction, up_vec)
                    fov = 90.0
                else:
                    cam_pos = player_pos + glm.vec3(0, camera_height, 0)
                    direction = glm.vec3(
                        math.sin(player_angle) * math.cos(player_pitch),
                        math.sin(player_pitch),
                        math.cos(player_angle) * math.cos(player_pitch),
                    )
                    view_matrix = glm.lookAt(cam_pos, cam_pos + direction, glm.vec3(0, 1, 0))
                    fov = 90.0
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
        write_state.player_health = self.player_health
        write_state.player_max_health = self.player_max_health
        write_state.player_dead = self.player_dead
        if self.play_mode and self.player and not self.cinematic_state:
            write_state.player_underwater = bool(getattr(self.player, 'eye_underwater', False))
            write_state.underwater_tint = list(getattr(self.player, 'water_tint', [0.0, 0.4, 0.6]))
        else:
            write_state.player_underwater = False
        write_state.collected_keys = set(self.collected_keys)
        write_state.hud_message = self.current_hud_message
        write_state.active_weapon = self.active_weapon
        write_state.muzzle_flash_active = self.muzzle_flash_active
        write_state.camera_transition_active = bool(self.camera_transition)

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
        # Fio already paid to describe the world numerically for culling; this
        # keeps the other half -- what each brush *is* -- numerical too, so the
        # renderer never has to go back to the dicts to rediscover it.  The
        # table is a projection, not a second world: it is rebuilt from
        # `brushes` whenever the editor's coarse world epoch moves, and holds
        # nothing that is not already in them.
        table = self._render_table
        render_dirty_snapshot = self.editor_state.render_dirty_snapshot()
        world_epoch, render_dirty = render_dirty_snapshot
        # Rows are named by the brush's UUID, so ids have to exist before the
        # table reconciles -- but only then, not on every frame.
        # Stable ids are needed when rows are first created/replaced, not
        # for ordinary epoch bumps. Avoid walking the whole scene on every edit.
        if (table.needs_reconcile(brushes, world_epoch)
                and (len(brushes) != table.count
                     or any(b.get('id') is None for b in brushes))):
            self.editor_state.ensure_entity_ids()
        generation = table.generation
        # One Python pass over the brush list, for the only two things that
        # cannot be cached: the live `hidden` flag (Big World parks through it)
        # and an unannounced change to the row set.
        live_hidden = table.begin_frame(
            brushes, world_epoch, dirty_objects=render_dirty)
        if table.generation != generation:
            refs = np.empty(table.count, dtype=object)
            for i, b in enumerate(brushes):
                refs[i] = b
            self._render_refs = refs
        refs = self._render_refs
        total_count = table.count

        # ---- warm columns ------------------------------------------------
        # Movers and doors move every tick and have no per-tick notification,
        # so their transform columns are re-read unconditionally.  They are
        # float copies -- no classification, no texture resolution.
        dynamic_slots = table.dynamic_slots
        if len(dynamic_slots):
            table.refresh_transforms(brushes, dynamic_slots)
            for i in dynamic_slots:
                i = int(i)
                b = brushes[i]
                # The render thread still reads pos/size off the dict for the
                # model matrix, so a moving brush is handed over as a snapshot.
                # Once the draw paths take their transform from table.center /
                # table.half this copy has no remaining purpose.
                b_ref = b.copy()
                b_ref['pos'] = list(b['pos'])
                b_ref['size'] = list(b['size'])
                if 'direction' in b:
                    b_ref['direction'] = list(b['direction'])
                if 'original_pos' in b:
                    b_ref['original_pos'] = list(b['original_pos'])
                refs[i] = b_ref

        if not self.play_mode:
            # An editor drag mutates pos for hundreds of frames after its one
            # save_state, so in the editor every row's warm columns are re-read.
            # This is the cheap half of the table by design; the expensive cold
            # columns stay behind the epoch.
            table.refresh_transforms(brushes, range(total_count))

        # ---- T4: visibility, as masks over the table ---------------------
        keep = ~live_hidden
        if self.culling_enabled and total_count:
            visible_mask = keep & self._aabb_in_frustum_batch(
                frustum_planes, table.center[:total_count],
                table.half[:total_count])
        else:
            visible_mask = keep

        all_slots = np.flatnonzero(keep)
        visible_slots = np.flatnonzero(visible_mask)
        # Published as views over the slots, not as lists: the conversion back
        # to Python objects happens only if something actually reads one, and
        # on the main camera path nothing does.
        all_brushes = PublishedBrushes(refs, all_slots)
        visible_brushes = PublishedBrushes(refs, visible_slots)
        culled_count = total_count - len(visible_slots)

        brush_positions = write_state.ensure_visible_brush_positions(
            len(visible_slots))
        if len(visible_slots):
            np.take(table.center[:, 0], visible_slots,
                    out=brush_positions[:len(visible_slots), 0])
            np.take(table.center[:, 2], visible_slots,
                    out=brush_positions[:len(visible_slots), 1])

        # The numerical result itself, published rather than thrown away: the
        # slots index every column of the table, so the renderer can classify,
        # sort and batch without reconstructing anything.
        write_state.render_table = table
        write_state.render_refs = refs
        write_state.visible_brush_slots = visible_slots
        write_state.all_brush_slots = all_slots

        write_state.visible_brushes = visible_brushes
        write_state.visible_brush_position_count = len(visible_brushes)
        write_state.all_brushes = all_brushes
        write_state.total_brushes = total_count
        write_state.culled_brushes = culled_count

        # ---- the entity half of the projection ---------------------------
        # What used to be one Python pass per entity per frame -- two NumPy
        # scalar stores, three isinstance tests and a list append each -- is a
        # bulk position store, a live `hidden` read, and masks over columns.
        things = self.things
        etable = self._entity_table
        entity_generation = etable.generation
        thing_hidden = etable.begin_frame(
            things, world_epoch, dirty_objects=render_dirty)
        if etable.generation != entity_generation:
            erefs = np.empty(etable.count, dtype=object)
            for i, t in enumerate(things):
                erefs[i] = t
            self._entity_refs = erefs
            self._entity_all_slots = np.arange(etable.count, dtype=np.int32)
        erefs = self._entity_refs
        thing_count = etable.count

        self.editor_state.clear_render_dirty(render_dirty_snapshot)

        # A Monster is handed to the renderer as a render snapshot, because the
        # AI thread is free to move it while the frame is being drawn.  Those
        # rows are the entity table's dynamic rows, and refreshing them is the
        # only per-entity work left that is not a column operation.
        for i in etable.monster_slots:
            erefs[i] = things[i].get_render_snapshot()

        # A collected pickup is not published.  Only pickup rows can be
        # collected, so the filter costs pickups rather than entities -- on a
        # map with no pickups it costs nothing at all.
        visible_thing_slots = self._entity_all_slots
        if (self.play_mode and self.collected_pickups
                and len(etable.pickup_slots)):
            collected = self.collected_pickups
            dropped = [int(i) for i in etable.pickup_slots
                       if id(things[int(i)]) in collected]
            if dropped:
                keep_things = np.ones(thing_count, dtype=bool)
                keep_things[dropped] = False
                visible_thing_slots = np.flatnonzero(keep_things)

        visible_count = len(visible_thing_slots)
        visible_thing_positions = write_state.ensure_visible_thing_positions(
            visible_count)
        if visible_count:
            np.take(etable.pos[:, 0], visible_thing_slots,
                    out=visible_thing_positions[:visible_count, 0])
            np.take(etable.pos[:, 2], visible_thing_slots,
                    out=visible_thing_positions[:visible_count, 1])

        all_lights = (erefs[etable.light_slots].tolist()
                      if len(etable.light_slots) else [])
        visible_things = (erefs[visible_thing_slots].tolist()
                          if visible_count else [])

        write_state.visible_things = visible_things
        write_state.visible_thing_position_count = visible_count
        write_state.all_things = list(things)
        write_state.all_lights = all_lights
        # The numerical result itself, for the renderer's entity classification.
        # Whether any portal exists at all. The portal virtual views draw
        # their sprites through the object path, so a view deciding whether to
        # skip the per-entity texture overrides has to know. Read off the
        # entity cache rather than by scanning, like every other per-tick
        # portal question.
        write_state.has_portals = bool(self._portal_things)
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