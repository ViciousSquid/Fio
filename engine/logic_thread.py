"""Logic-thread orchestration for Fio's extracted runtime domains.

The thread owns cross-domain ordering, the fixed-timestep loop, fault handling,
and render publication. Authoritative authoring state remains in EditorState;
play-session state belongs to the extracted Logic* runtimes constructed here.
"""

import threading
import time
import os

from .threaded_game_state import ThreadedGameState
from .explosions import ExplosionRuntime, finish_explosions
from .shake import ShakeRuntime
from .cutscene_runtime import CutsceneRuntime
from .logic_camera import LogicCamera
from .logic_player import LogicPlayer
from .logic_movers import LogicMovers
from .logic_parenting import LogicParenting
from .logic_portals import LogicPortals
from .logic_triggers import LogicTriggers
from .logic_combat import LogicCombat
from .logic_timing import LogicTiming
from .logic_collision import LogicCollision
from .logic_world import LogicWorld
from .logic_render import LogicRender
from .logic_session import LogicSession
from .physics import CRUSH_DAMAGE, PHYSICS_EVENTS
from .logic_interaction import LogicInteraction
from .logic_editor import LogicEditor
from .prop_runtime import PropSession



# Import Thing subclasses for type checking
try:
    from editor.things import (Light, Monster as MonsterThing, PathNode,
                               LogicTimer, Portal, LevelChanger)
except ImportError:
    Light = None
    MonsterThing = None
    PathNode = None
    LogicTimer = None
    Portal = None
    LevelChanger = None

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
_WATER_LOUDNESS = 0.7


class TickLock:
    """The logic thread's re-entrant tick lock, which knows who is waiting.

    CPython's locks are not fair: a thread that releases a lock and takes it
    again microseconds later wins nearly every time against one already
    blocked on it. The run loop does exactly that whenever a frame overruns,
    so with a busy map the UI thread could wait seconds -- in a synthetic
    test, for ever -- to stop, save, load or edit. Every other thread that
    has to block here is counted, and the loop steps aside until they have
    had their turn (LogicThread._yield_to_waiters).

    Otherwise an ``RLock``: ``with``, ``acquire(blocking, timeout)`` and
    ``release()`` behave the same, and the same thread may nest.
    """

    __slots__ = ("_lock", "_count_lock", "_waiters")

    def __init__(self):
        self._lock = threading.RLock()
        self._count_lock = threading.Lock()
        self._waiters = 0

    def acquire(self, blocking=True, timeout=-1):
        lock = self._lock
        if lock.acquire(False):
            return True
        if not blocking:
            return False
        with self._count_lock:
            self._waiters += 1
        try:
            return lock.acquire(True, timeout)
        finally:
            with self._count_lock:
                self._waiters -= 1

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()

    def _is_owned(self):
        """Whether the calling thread holds the lock (as ``RLock._is_owned``)."""
        return self._lock._is_owned()

    @property
    def waiters(self) -> int:
        """Threads blocked waiting for the lock right now."""
        return self._waiters


class LogicThread(threading.Thread):
    """
    Unified logic thread for both editor and play mode.
    Runs continuously at a fixed timestep (60 Hz).
    """
    
    TICK_RATE = 60
    TICK_DURATION = 1.0 / TICK_RATE
    #: How far past a tick's due time the loop wakes (seconds): an early
    #: wake would run no tick. The accumulator carries it, so it never drifts.
    WAKE_MARGIN = 0.0002
    #: Most ticks one frame runs to catch up. A frame further behind than
    #: this drops the rest: under sustained overload the game slows down
    #: instead of running ever longer frames (15 ticks in one lock hold at
    #: the old 0.25 s clamp), which also starved the UI thread of the lock.
    MAX_CATCHUP_TICKS = 4
    #: The loop always sleeps at least this long between frames, overrun or
    #: not, so a thread waiting for the tick lock is scheduled.
    MIN_FRAME_SLEEP = 0.0005
    #: Longest the loop waits for blocked threads to take the tick lock.
    MAX_YIELD_SECONDS = 0.1
    #: Waiting longer than this for the tick lock means another thread held
    #: the world (play start, a load): that time is not simulated.
    LOCK_WAIT_RESYNC = 2.0 / TICK_RATE

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
        ),
    }

    def __init__(self, game_state: ThreadedGameState, editor_state, *, threaded_ai=True):
        super().__init__(daemon=True)
        self.threaded_ai = threaded_ai
        # Serialises the simulation with everything that rebuilds or reads the
        # world from another thread.  The run loop holds it for each frame's
        # ticks and render-state projection; entering or leaving play mode and
        # saving or restoring a session (all called from the UI thread) hold it
        # for their whole duration, so a tick never sees a half-built or
        # half-torn-down session.  Reentrant: the same thread may nest.
        self._tick_lock = TickLock()
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
            self.io_manager.set_group_finder(self.world_runtime.find_group_members)
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

        # Extracted runtime domains below own their state; LogicThread only
        # constructs them and establishes their dependency order.


        # Monster AI (delegated to separate class + thread)
        self.monster_ai = MonsterAI(self)


        # CutsceneRuntime owns cinematic playback state. LogicThread remains
        # the simulation orchestrator and delegates the state machine here.
        #: Extraction folder of a played .fiopak, set by the editor at Play;
        #: its cutscenes/ is searched before the project's.
        self.package = None
        self.cutscene_runtime = CutsceneRuntime(self)
        # LogicPortals owns portal topology, transit and fade runtime.
        self.portal_runtime = LogicPortals(self, portal_type=Portal)
        # LogicParenting owns mover-parented light and portal transforms.
        self.parenting_runtime = LogicParenting(
            self, light_type=Light, portal_type=Portal
        )
        # LogicCombat owns weapons, projectiles, bullet marks and noise.
        self.combat_runtime = LogicCombat(self)
        #: Explosive Props: damage taken, explosions due, what Stop puts back.
        self.explosion_runtime = ExplosionRuntime(self)
        #: Screen shakes (the Shake entity): the camera view only.
        self.shake_runtime = ShakeRuntime()
        # LogicTiming owns timer and light-fade advancement.
        self.timing_runtime = LogicTiming(self)
        # LogicCollision owns world/model collision geometry and cache rebuilding.
        self.collision_runtime = LogicCollision(self)

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
        #: Pause-menu state the plugins were last told about.
        self._pause_menu_announced = False
        self._gui_fault_teardown_requested = False
        self._tick_fault_message = ""
        #: Last line of the last tick traceback logged: a tick failing the
        #: same way every frame is logged once, not 60 times a second.
        self._last_tick_error = None
        #: Set (under the tick lock) by whatever replaced the world while the
        #: loop waited -- entering play, a load: the next frame starts its
        #: clock afresh instead of simulating the wait.
        self._clock_resync = False
        self._clock_resynced = False
        #: Why the run loop ended, when an exception escaped it; None while
        #: it runs or after an ordinary stop(). Read by the GUI's health check.
        self.death_message = None
        self._last_loop_error = None

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
    # THREAD LIFECYCLE
    # =========================================================================

    def start(self):
        # Set before the thread exists, not in run(): a stop() that arrives
        # before run() gets going must not be overwritten.
        self.running = True
        super().start()

    def run(self):
        try:
            self._run_loop()
        except BaseException:
            # _run_loop catches everything a frame raises; reaching here means
            # the loop's own bookkeeping failed. Say so: a thread that ends
            # silently leaves the editor showing its last frame for ever.
            import traceback
            self.death_message = traceback.format_exc()
            debug_log("LogicThread", "The logic thread stopped:\n" + self.death_message)
            raise

    def _run_loop(self):
        last_time = time.perf_counter()
        accumulator = 0.0

        while self.running:
            current_time = time.perf_counter()
            try:
                frame_time = current_time - last_time
                last_time = current_time

                if frame_time > 0.25:
                    frame_time = 0.25

                accumulator += frame_time
                accumulator = self._step_frame(accumulator)
                if self._clock_resynced:
                    # The world was held by another thread (or replaced) while
                    # this frame waited for it: that wait is not game time.
                    self._clock_resynced = False
                    last_time = current_time = time.perf_counter()
                self._publish_frame()
            except Exception:
                # _step_frame guards the tick and the projection; anything
                # else that raises (publication, the clock) must not end the
                # thread either. Logged once per kind; the frame's debt goes.
                import traceback
                trace = traceback.format_exc()
                key = trace.strip().splitlines()[-1]
                if key != self._last_loop_error:
                    self._last_loop_error = key
                    debug_log("LogicThread", "Unhandled exception in the logic loop:\n" + trace)
                accumulator = 0.0
                last_time = time.perf_counter()

            # Sleep until the accumulator next holds a whole tick -- the next
            # tick's due time, less what stepping and publishing took -- and
            # wake just past it, never just before (an early wake runs no
            # tick and sleeps again). Sleeping a fixed share of a tick from
            # this iteration's start instead overshot whenever an iteration
            # began just short of a tick: ticks then landed up to ~14 ms off
            # their 60 Hz slots (p95), and with no interpolation the picture
            # moved unevenly. Never less than MIN_FRAME_SLEEP, and never
            # straight back to the lock past a thread waiting for it.
            sleep_time = (self.TICK_DURATION - accumulator
                          - (time.perf_counter() - current_time) + self.WAKE_MARGIN)
            time.sleep(max(sleep_time, self.MIN_FRAME_SLEEP))
            self._yield_to_waiters()

    def _yield_to_waiters(self) -> int:
        """Let threads blocked on the tick lock take it before the next frame.

        Returns how many polls it took (0 when nobody was waiting). Bounded
        by MAX_YIELD_SECONDS, so a waiter that never wakes cannot stop the
        simulation.
        """
        lock = self._tick_lock
        if not getattr(lock, 'waiters', 0):
            return 0
        polls = 0
        deadline = time.perf_counter() + self.MAX_YIELD_SECONDS
        while lock.waiters and self.running and time.perf_counter() < deadline:
            time.sleep(self.MIN_FRAME_SLEEP)
            polls += 1
        return polls

    def request_clock_resync(self):
        """The world was just rebuilt or restored: don't catch up on the time
        that took. Call with the tick lock held."""
        self._clock_resync = True

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
        entered = time.perf_counter()
        with self._tick_lock:
            if time.perf_counter() - entered > self.LOCK_WAIT_RESYNC:
                # Another thread held the world for a while (play start, a
                # load, a long edit): the time it held it is not simulated.
                self._clock_resync = True
            if self._clock_resync:
                self._clock_resync = False
                self._clock_resynced = True
                accumulator = min(accumulator, self.TICK_DURATION)
            if self._tick_faulted and self.session_runtime.play_mode:
                # A tick failed and the GUI teardown is queued but has not run
                # yet: the world is half-advanced, so it must not be ticked
                # again (each attempt would also log another fatal traceback).
                accumulator = 0.0
            ticks = 0
            while accumulator >= self.TICK_DURATION:
                if ticks >= self.MAX_CATCHUP_TICKS:
                    # Too far behind to catch up: drop the rest.
                    accumulator %= self.TICK_DURATION
                    break
                ticks += 1
                started = time.perf_counter()
                try:
                    self._tick(self.TICK_DURATION)
                except Exception:
                    self._tick_failed()
                    # Discard accumulated time. The next frame starts from a
                    # clean state rather than replaying stale simulation debt.
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

    def _tick_failed(self):
        """Handle a tick that raised (call inside ``except``).

        Never continue a failed play tick: by this point the authoritative
        world may be only partially advanced, so the session is ended -- on
        the GUI thread when there is one. A failed editor tick has no session
        to end: it is logged and the next tick runs. Either way the traceback
        is logged once per distinct error, not once a frame.
        """
        import traceback
        trace = traceback.format_exc()
        key = trace.strip().splitlines()[-1] if trace.strip() else ""
        playing = self.session_runtime.play_mode
        # A play fault is logged every time: it ends its session, so it
        # happens once per Play.
        if playing or key != self._last_tick_error:
            self._last_tick_error = key
            debug_log(
                "LogicThread",
                ("Fatal simulation tick failure; ending play session:\n"
                 if playing else "Editor tick failed:\n") + trace,
            )
        if not playing:
            return
        self._tick_faulted = True
        self._tick_fault_message = trace
        callback = self._gui_fault_teardown
        if callback is not None:
            # Qt/editor teardown must not run on this worker thread: the GUI
            # thread runs the normal QtGameView play-mode teardown path, once.
            if not self._gui_fault_teardown_requested:
                self._gui_fault_teardown_requested = True
                try:
                    callback(trace)
                except Exception:
                    debug_log(
                        "LogicThread",
                        "Could not queue GUI teardown after tick failure:\n"
                        + traceback.format_exc(),
                    )
            return
        # Standalone/headless hosts have no Qt lifecycle to marshal through,
        # so they still need the native engine teardown.
        try:
            self.session_runtime.apply_play_mode(False)
        except Exception:
            debug_log(
                "LogicThread",
                "Play-session teardown after tick failure also failed:\n"
                + traceback.format_exc(),
            )

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

    def _announce_pause_menu(self):
        """Emit ``pause_menu_opened`` / ``pause_menu_closed`` on a change.

        The menu is opened and closed on the UI thread; its events reach
        plugins here, on the logic thread, like every other engine event.
        """
        is_open = self.session_runtime.pause_menu_open
        if is_open != self._pause_menu_announced:
            self._pause_menu_announced = is_open
            self._plugin_emit("pause_menu_opened" if is_open else "pause_menu_closed")

    def _tick(self, delta: float):
        self._announce_pause_menu()
        # Explosions that have played go back to their type (a FIRE burns
        # again), in a game and after a preview in the editor alike.
        finish_explosions(self)
        if self.session_runtime.play_mode:
            # Player events the MonsterAI thread raised since the last tick,
            # delivered here: under the tick lock, not its monster lock.
            self.trigger_runtime.drain_deferred_events()
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
        self.game_state.consume_weapon_slots()
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
            self.game_state.consume_weapon_slots()
            return

        # ---- Player dead: freeze all gameplay input ----
        if self.player_runtime.player_dead:
            self.game_state.consume_mouse_delta()
            self.game_state.consume_use_key()
            self.game_state.consume_shot()
            self.game_state.consume_weapon_slots()
            return

        # ---- Level Complete UI: freeze player input ----
        if self.interaction_runtime.level_complete_ui:
            self.game_state.consume_mouse_delta()
            self.game_state.consume_use_key()
            self.game_state.consume_shot()
            self.game_state.consume_weapon_slots()
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
            # A player whose own physics is off -- noclip, a spectator's free
            # camera -- touches no body: it pushes nothing and is pushed by
            # nothing.
            player = self.player_runtime.player
            if not getattr(player, 'physics_enabled', True):
                player = None
            # Events are recorded only while a plugin is subscribed to one.
            plugins = self.plugins
            wanted = plugins is not None and any(
                plugins.has_listeners(event) for event in PHYSICS_EVENTS)
            physics_world.events_wanted = wanted
            physics_world.step(delta, player)
            if getattr(physics_world, 'player_crushed', False):
                # A heavy body pinned the player against a wall.
                physics_world.player_crushed = False
                self.trigger_runtime._apply_player_damage(CRUSH_DAMAGE)
            if wanted:
                for kind, entity, data in physics_world.drain_events():
                    self._plugin_emit('physics.' + kind, entity=entity, **data)
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
        
        # Weapon slot keys, then shooting with whatever is now in hand.
        for slot in self.game_state.consume_weapon_slots():
            self.combat_runtime.select_slot(slot)
        if self.game_state.consume_shot():
            self.combat_runtime._handle_shooting()
            
        combat_runtime = self.combat_runtime
        combat_runtime._update_bullet_marks()

        # Clean up expired player-noise events through combat ownership.
        combat_runtime.prune_noise_events(3.0)

        # Update monster projectiles (flying monster ranged attacks)
        # NOTE: Monster AI itself now runs in MonsterAIThread
        combat_runtime._update_monster_projectiles(delta)

        # Explosive Props that shots or blasts have finished off go up now
        # (outside the monster lock, which their blast takes).
        self.explosion_runtime.update()

        # ── Player 2 physics (split-screen) ──────────────────────────────────
        self.player_runtime.update_player2(delta)


