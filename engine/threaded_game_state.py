import threading
import weakref
import glm
import numpy as np
from collections import deque

from .render_table import RenderTable
from .entity_table import EntityTable

# Shared immutable "nothing to drain" result for the per-frame consumer methods
# (consume_sounds / consume_console_commands). Returning this singleton on the
# common empty path avoids allocating a throwaway list on every rendered frame.
_EMPTY_DRAIN: tuple = ()

class PublishedObjects:
    """Lazy object view over a dense slot selection."""

    __slots__ = ('_refs', '_slots', '_list', '_label')

    def __init__(self, refs, slots, label="PublishedObjects"):
        self._refs = refs
        self._slots = slots
        self._list = None
        self._label = label

    def materialise(self):
        if self._list is None:
            self._list = self._refs[self._slots].tolist() if len(self._slots) else []
        return self._list

    def __len__(self):
        return len(self._slots)

    def __bool__(self):
        return len(self._slots) > 0

    def __iter__(self):
        return iter(self.materialise())

    def __getitem__(self, index):
        return self.materialise()[index]

    def __repr__(self):
        return '<%s %d%s>' % (self._label, len(self._slots), '' if self._list is None else ' materialised')


class PublishedBrushes(PublishedObjects):
    __slots__ = ()
    def __init__(self, refs, slots):
        super().__init__(refs, slots, "PublishedBrushes")


class PublishedEntities(PublishedObjects):
    __slots__ = ()
    def __init__(self, refs, slots):
        super().__init__(refs, slots, "PublishedEntities")

class RenderState:
    """
    A snapshot of the game state specifically for the renderer.
    """
    def __init__(self):
        # Camera / View
        self.camera_view_matrix = glm.mat4(1.0)
        self.projection_matrix = glm.mat4(1.0)
        self.is_play_mode = False
        
        # Editor Camera
        self.editor_camera_pos = glm.vec3(0, 0, 0)
        self.editor_camera_yaw = 0.0
        self.editor_camera_pitch = 0.0
        self.editor_camera_fov = 90.0
        
        # Player
        self.player_pos = glm.vec3(0, 0, 0)
        self.player_angle = 0.0
        self.player_pitch = 0.0
        self.player_health = 100
        self.player_max_health = 100
        self.player_dead = False
        self.active_weapon = None
        self.player_ammo = 0
        self.shot_ready = False
        self.player_underwater = False
        self.underwater_tint = [0.0, 0.4, 0.6]

        # Player 2 (split-screen)
        self.player2_pos = glm.vec3(0, 0, 0)
        self.player2_angle = 0.0
        self.player2_pitch = 0.0
        self.player2_view_matrix = glm.mat4(1.0)
        self.player2_health = 100
        self.player2_max_health = 100
        self.player2_dead = False
        self.player2_underwater = False
        self.splitscreen_active = False
        
        # Scene Data
        self.visible_brushes = []
        self.all_brushes = []
        self.visible_things = []
        # Authoritative Light objects for renderer lighting; avoids scanning
        # the full Thing set every render frame.
        self.all_lights = []
        # The entity half of the dense projection (engine.entity_table), with
        # the slots the frame published and the live hidden mask it read.  The
        # renderer classifies entities into passes from these rather than
        # re-deriving each one's kind. Portal virtual views consume the same
        # columns; there is no portal-specific entity object walk.
        self.entity_table = EntityTable()
        self.entity_refs = np.empty(0, dtype=object)
        self.visible_thing_slots = np.empty(0, dtype=np.int32)
        self.thing_hidden = np.empty(0, dtype=bool)
        self.has_portals = False

        # The dense render projection (engine.render_table.RenderTable) and the
        # visibility result as integer slots into it. These are what let the
        # renderer classify, sort and batch numerically instead of walking the
        # published object lists to rediscover what it already knows. Every
        # RenderState owns its own persistent table, so the logic thread refreshes
        # only the write-side projection while the renderer consumes the read-side
        # projection unchanged.
        self.render_table = RenderTable()
        #: slot -> the render reference for that row: the live brush dict, or
        #: for a mover or a door the per-frame snapshot. Indexed by the slot
        #: arrays below, so a consumer converts an index to an object once, at
        #: the point it actually needs one, rather than up front for everything.
        self.render_refs = np.empty(0, dtype=object)
        self.visible_brush_slots = np.empty(0, dtype=np.int32)
        self.all_brush_slots = np.empty(0, dtype=np.int32)
        
        # HUD / Gameplay
        self.collected_keys = set()
        self.hud_message = ""
        self.hud_prompt_key = None
        
        # Visual FX
        self.bullet_marks = [] # List of {'pos': [x,y,z], 'alpha': float}
        #: Live monster projectiles, as an ``(N, 3)`` float32 array of positions.
        self.projectiles = np.empty((0, 3), dtype=np.float32)

        # Muzzle flash — True for one frame after the player fires
        self.muzzle_flash_active = False

        # Camera transition — True while the play-mode camera is tweening between
        # First Person and Overhead (see LogicThread.start_camera_transition).
        # The overhead ground sprite is suppressed during the blend so it does
        # not pop in/out mid-swoop.
        self.camera_transition_active = False
        # True while LogicCamera owns the player's view; HUD is suppressed.
        self.cinematic_camera_active = False
        # Opacity for the entire HUD after cinematic control returns.
        self.hud_alpha = 1.0
        # Independent opacity for the health count; the logic thread keeps this
        # at 50% while idle and raises it in response to health-value changes.
        self.hud_health_alpha = 0.5

        # Monster debug visualisation (F7 toggle)
        self.monster_debug_active = False
        # List of {'start': [x,y,z], 'end': [x,y,z], 'color': str}
        #   color is 'green' (has LOS) or 'red' (blocked)
        self.monster_debug_rays = []
        
        # Debug / Stats
        self.total_brushes = 0
        self.culled_brushes = 0
        self.timestamp = 0.0
        #: Logic-thread milliseconds spent preparing this frame.
        self.prepare_ms = 0.0

    def reset(self):
        """Reset all fields to defaults for reuse (avoids per-frame allocation)."""
        self.camera_view_matrix = glm.mat4(1.0)
        self.projection_matrix = glm.mat4(1.0)
        self.is_play_mode = False
        self.editor_camera_pos = glm.vec3(0, 0, 0)
        self.editor_camera_yaw = 0.0
        self.editor_camera_pitch = 0.0
        self.editor_camera_fov = 90.0
        self.player_pos = glm.vec3(0, 0, 0)
        self.player_angle = 0.0
        self.player_pitch = 0.0
        self.player_health = 100
        self.player_max_health = 100
        self.player_dead = False
        self.active_weapon = None
        self.player_ammo = 0
        self.shot_ready = False
        self.player_underwater = False
        self.underwater_tint = [0.0, 0.4, 0.6]
        self.player2_pos = glm.vec3(0, 0, 0)
        self.player2_angle = 0.0
        self.player2_pitch = 0.0
        self.player2_view_matrix = glm.mat4(1.0)
        self.player2_health = 100
        self.player2_max_health = 100
        self.player2_dead = False
        self.player2_underwater = False
        self.splitscreen_active = False
        self.visible_brushes = []
        self.all_brushes = []
        self.visible_things = []
        self.all_lights = []
        # Keep the dense projection objects across buffer recycling.  Their
        # published slot vectors below are emptied, so an interstitial frame
        # cannot draw stale rows, while the next LogicThread publish reuses the
        # same tables without allocating a RenderTable/EntityTable per frame.
        if self.entity_table is None:
            self.entity_table = EntityTable()
        self.entity_refs = np.empty(0, dtype=object)
        self.visible_thing_slots = np.empty(0, dtype=np.int32)
        self.thing_hidden = np.empty(0, dtype=bool)
        self.has_portals = False
        if self.render_table is None:
            self.render_table = RenderTable()
        #: slot -> the render reference for that row: the live brush dict, or
        #: for a mover or a door the per-frame snapshot. Indexed by the slot
        #: arrays below, so a consumer converts an index to an object once, at
        #: the point it actually needs one, rather than up front for everything.
        self.render_refs = np.empty(0, dtype=object)
        self.visible_brush_slots = np.empty(0, dtype=np.int32)
        self.all_brush_slots = np.empty(0, dtype=np.int32)
        self.collected_keys = set()
        self.hud_message = ""
        self.hud_prompt_key = None
        self.bullet_marks = []
        #: Live monster projectiles, as an ``(N, 3)`` float32 array of positions.
        self.projectiles = np.empty((0, 3), dtype=np.float32)
        self.muzzle_flash_active = False
        self.camera_transition_active = False
        self.cinematic_camera_active = False
        self.hud_alpha = 1.0
        self.hud_health_alpha = 0.5
        self.monster_debug_active = False
        self.monster_debug_rays = []
        self.total_brushes = 0
        self.culled_brushes = 0
        self.timestamp = 0.0
        self.prepare_ms = 0.0


class _OwnedLock:
    """A non-reentrant lock that knows which thread holds it.

    The render-state lease finalizer is a garbage-collector safety net, and a
    collection can run on whichever thread happens to be allocating -- which
    includes a thread inside this lock (a swap resets a RenderState in here).
    Taking the lock again from there would deadlock that thread on itself, so
    the finalizer asks :attr:`holder` first and defers instead.
    """

    __slots__ = ('_lock', 'holder')

    def __init__(self):
        self._lock = threading.Lock()
        self.holder = None

    def __enter__(self):
        self._lock.acquire()
        self.holder = threading.get_ident()
        return self

    def __exit__(self, *exc):
        self.holder = None
        self._lock.release()
        return False


class ThreadedGameState:
    """
    Thread-safe container for communication between UI/Input and Logic threads.
    """

    #: Pending sound requests kept while the UI is not draining them.
    SOUND_QUEUE_LIMIT = 256
    def __init__(self):
        self._render_state_lock = _OwnedLock()
        #: Lease releases a finalizer could not take the lock for (it ran on
        #: the thread already holding it); folded in by the next locked call.
        self._deferred_releases = []

        # Double buffering, as in Quake 3's SMP renderer: the renderer reads
        # one RenderState while the logic thread writes the other, and each
        # owns its own persistent RenderTable/EntityTable.
        #
        # The renderer borrows the read buffer for the length of a paint
        # (get_render_state / release_render_state). While it is borrowed the
        # logic thread does not swap: request_swap() declines, the write buffer
        # stays logic-owned, and the next tick rebuilds it with newer state.
        # So a slow paint delays publication by at most the paint, never lets
        # the logic thread write into a buffer that is being drawn, and never
        # needs a third copy of the dense tables to hide behind.
        self._read_state = RenderState()
        self._write_state = RenderState()
        # Live renderer snapshots borrowing the read buffer. Only the read
        # buffer can be borrowed, and it cannot change while this is non-zero.
        self._read_leases = 0
        self._has_new_frame = False
        # The write buffer holds a finished frame whose swap was declined, and
        # the logic thread has not started writing it again: the renderer may
        # publish it itself the moment it lets go of the read buffer.
        self._write_ready = False
        #: Publication counters for the Debug Tables instrument: frames
        #: handed to the renderer, and swaps declined because it was reading.
        self.published_frames = 0
        self.declined_swaps = 0

        # Input state
        self._keys_lock = threading.Lock()
        self._keys = set()
        self._mouse_lock = threading.Lock()
        self._mouse_delta = (0.0, 0.0)
        
        # Shot Queue — deque for O(1) popleft
        self._shot_lock = threading.Lock()
        self._shot_queue = deque()

        # Use key — protected by its own lock
        self._use_key_lock = threading.Lock()
        self._use_key_pressed = False

        # Player 2 input (gamepad / arrow keys)
        self._p2_lock = threading.Lock()
        self._p2_input = {
            'move_x': 0.0, 'move_z': 0.0,
            'look_dx': 0.0, 'look_dy': 0.0,
            'jump': False, 'crouch': False,
        }

        # Sound queue — thread-safe, accessed from logic and render threads.
        # Bounded: the UI drains it every frame, so it only fills while the UI
        # is stalled (a modal dialog, a long hitch), and then the oldest
        # requests are stale; unbounded, a 1000-monster fight queued ~25 a
        # second to play all at once when the UI came back.
        self._sound_lock = threading.Lock()
        self.sound_queue = deque(maxlen=self.SOUND_QUEUE_LIMIT)

        # Console command queue — thread-safe. The I/O system (logic thread)
        # enqueues command strings (e.g. from a logic_command entity fired by a
        # trigger brush); the render/UI thread drains and executes them on the
        # main thread, where the console handler and its Qt widgets are safe to
        # touch. Mirrors the sound queue pattern.
        self._console_cmd_lock = threading.Lock()
        self.console_command_queue = deque()

    @staticmethod
    def _release_render_state_lease(owner_ref) -> None:
        """Return one borrow of the read buffer.

        If that was the last borrow and a finished frame was held back for it,
        publish that frame now rather than on the logic thread's next tick.
        """
        owner = owner_ref()
        if owner is None:
            return
        if owner._render_state_lock.holder == threading.get_ident():
            # A collection inside a locked section of this very thread.
            owner._deferred_releases.append(1)
            return
        with owner._render_state_lock:
            owner._fold_deferred_releases()
            if owner._read_leases > 0:
                owner._read_leases -= 1
            if owner._read_leases == 0 and owner._write_ready:
                owner._swap_locked()

    def _fold_deferred_releases(self) -> None:
        """Apply lease releases deferred by a finalizer (caller holds the lock)."""
        deferred = self._deferred_releases
        while deferred:
            deferred.pop()
            if self._read_leases > 0:
                self._read_leases -= 1

    def get_render_state(self) -> RenderState:
        """Borrow the latest published frame for the renderer/UI.

        The returned object is a shallow snapshot for API compatibility, but
        its arrays/tables still belong to the published RenderState, so the
        borrow pins that buffer: no swap happens until
        ``release_render_state()`` is called or the snapshot is garbage-
        collected. Hold it for one paint, not longer -- publication waits for
        it. For a scalar or two outside a paint use :meth:`published`, which
        borrows nothing.
        """
        with self._render_state_lock:
            self._fold_deferred_releases()
            source = self._read_state
            self._read_leases += 1
            snap = object.__new__(RenderState)
            snap.__dict__ = source.__dict__.copy()
            snap._render_lease_finalizer = weakref.finalize(
                snap,
                ThreadedGameState._release_render_state_lease,
                weakref.ref(self),
            )
            return snap

    def release_render_state(self, snapshot: RenderState) -> None:
        """Release a borrowed render-state snapshot early.

        The finalizer is also attached as a safety net, so existing short-lived
        callers remain safe even if they do not explicitly release the snapshot.
        """
        finalizer = getattr(snapshot, "_render_lease_finalizer", None)
        if finalizer is not None:
            finalizer()

    def published(self, name, default=None):
        """One field of the latest published frame, without borrowing it.

        For UI event handlers that need a flag (is the player dead, is a shot
        ready): the value is read under the swap lock, so it is the field of
        one whole published frame, and nothing is pinned afterwards.
        """
        with self._render_state_lock:
            return getattr(self._read_state, name, default)

    def get_write_state(self) -> RenderState:
        """The buffer the logic thread writes; it stays the logic thread's
        until the next :meth:`request_swap` publishes it."""
        with self._render_state_lock:
            self._write_ready = False
            return self._write_state

    def peer_state(self) -> RenderState:
        """The buffer the logic thread is *not* writing, for reading only.

        Its tables are complete (the last published frame, or one prepared and
        held back) and nothing writes them until the next swap, which only
        the logic thread performs -- so the logic thread can copy from them
        while it prepares the other buffer.
        """
        with self._render_state_lock:
            return self._read_state

    def peek_has_new_frame(self) -> bool:
        """Non-consuming check used by update_loop."""
        with self._render_state_lock:
            return self._has_new_frame

    def request_swap(self) -> bool:
        """Publish the completed write buffer, unless the renderer is reading.

        Returns False, and publishes nothing, while the read buffer is
        borrowed. The finished frame is then published by whichever comes
        first: the renderer letting go of the read buffer, or the logic
        thread's next tick (which rebuilds it with newer state first).
        """
        with self._render_state_lock:
            self._fold_deferred_releases()
            if self._read_leases:
                self.declined_swaps += 1
                self._write_ready = True
                return False
            self._swap_locked()
            return True

    def _swap_locked(self) -> None:
        self.published_frames += 1
        self._write_ready = False
        old_read = self._read_state
        self._read_state = self._write_state
        old_read.reset()
        self._write_state = old_read
        self._has_new_frame = True

    def try_swap(self) -> bool:
        """Called by QtGameView to check if a new frame is available."""
        with self._render_state_lock:
            if self._has_new_frame:
                self._has_new_frame = False
                return True
            return False

    # --- Input Handling ---

    def set_keys(self, keys: set):
        with self._keys_lock:
            self._keys = keys.copy()
            
    def get_keys(self) -> set:
        with self._keys_lock:
            return self._keys.copy()
            
    def set_mouse_delta(self, dx, dy):
        with self._mouse_lock:
            self._mouse_delta = (self._mouse_delta[0] + dx, self._mouse_delta[1] + dy)
            
    def consume_mouse_delta(self):
        with self._mouse_lock:
            delta = self._mouse_delta
            self._mouse_delta = (0.0, 0.0)
            return delta

    def set_use_key(self, pressed: bool):
        """Sets the state of the use key explicitly (True/False)."""
        with self._use_key_lock:
            self._use_key_pressed = pressed
    
    def set_use_key_pressed(self):
        """Convenience method called by main_window.py to trigger the use key."""
        with self._use_key_lock:
            self._use_key_pressed = True
        
    def consume_use_key(self) -> bool:
        with self._use_key_lock:
            if self._use_key_pressed:
                self._use_key_pressed = False
                return True
            return False

    # --- Shooting Handling ---

    def queue_shot(self):
        with self._shot_lock:
            self._shot_queue.append(True)

    def consume_shot(self):
        # Called every logic tick; a shot is queued only on the rare tick the
        # player fires. Skip the lock on the empty fast path — the deque's
        # truthiness read is atomic under the GIL, and a shot queued
        # concurrently is consumed on the next tick.
        if not self._shot_queue:
            return False
        with self._shot_lock:
            if self._shot_queue:
                self._shot_queue.popleft()
                return True
            return False

    # --- Sound Queue ---

    def queue_sound(self, request: dict):
        """Thread-safe: enqueue a sound request from any thread."""
        with self._sound_lock:
            self.sound_queue.append(request)

    def clear_sounds(self) -> int:
        """Cancel all pending sound requests and return how many were removed."""
        with self._sound_lock:
            count = len(self.sound_queue)
            self.sound_queue.clear()
            return count

    # --- Player 2 Input ---

    def set_p2_input(self, move_x: float, move_z: float,
                     look_dx: float, look_dy: float,
                     jump: bool, crouch: bool = False) -> None:
        """Thread-safe: push P2 input from the render/UI thread."""
        with self._p2_lock:
            self._p2_input = {
                'move_x': float(move_x),
                'move_z': float(move_z),
                'look_dx': float(look_dx),
                'look_dy': float(look_dy),
                'jump': bool(jump),
                'crouch': bool(crouch),
            }

    def get_p2_input(self) -> dict:
        """Thread-safe: read P2 input from the logic thread."""
        with self._p2_lock:
            return self._p2_input.copy()

    def consume_sounds(self) -> list:
        """Thread-safe: drain all pending sound requests (called from render thread).

        Runs once per rendered frame. The empty case is by far the most common,
        so it is handled with a lock-free fast path: reading a deque's truthiness
        is atomic under the GIL, and a request appended concurrently is simply
        drained on the next frame (harmless for an async sound queue). This
        avoids a lock acquisition and an empty-list allocation on idle frames.
        """
        if not self.sound_queue:
            return _EMPTY_DRAIN
        with self._sound_lock:
            if not self.sound_queue:
                return _EMPTY_DRAIN
            result = list(self.sound_queue)
            self.sound_queue.clear()
            return result

    # --- Console Command Queue ---

    def queue_console_command(self, command: str) -> None:
        """Thread-safe: enqueue a console command string from any thread.

        The command is executed later on the UI/main thread (see
        QtGameView._process_console_command_queue), so I/O handlers running on
        the logic thread can safely trigger console commands.
        """
        if not command:
            return
        with self._console_cmd_lock:
            self.console_command_queue.append(str(command))

    def consume_console_commands(self) -> list:
        """Thread-safe: drain all pending console commands (called from UI thread).

        Called every rendered frame from QtGameView.update_loop, but the queue is
        empty on virtually all frames (commands only arrive when a trigger fires a
        logic_command entity). The empty case uses a lock-free fast path: reading
        a deque's truthiness is atomic under the GIL, and a command enqueued
        concurrently is drained on the next frame. This keeps the per-frame cost
        at a single pointer check instead of a lock acquisition plus a list
        allocation.
        """
        if not self.console_command_queue:
            return _EMPTY_DRAIN
        with self._console_cmd_lock:
            if not self.console_command_queue:
                return _EMPTY_DRAIN
            result = list(self.console_command_queue)
            self.console_command_queue.clear()
            return result
