> Applies to Fio 3.5.0.1111.

# Fio Runtime Architecture

Fio edits and plays one world. The editor's `EditorState` (`editor/editor_state.py`) holds the authoritative brushes (dicts) and entities (`Thing` objects); Play does not export or copy it — the simulation runs on those same objects. Everything the runtime executes over in bulk (rendering, AI, movers, physics bodies, collision queries) is a dense NumPy projection derived from that world and rebuilt from it at any time. This page is the map of the runtime: the threads, what each owns, how data crosses between them, and the lifecycle of a play session. The per-thread detail is on [Logic Thread](https://github.com/ViciousSquid/Fio/wiki/Logic-Thread) and [Monster AI Thread](https://github.com/ViciousSquid/Fio/wiki/Monster-AI-Thread).

## Process requirements

`python_runtime.py: require_supported_python()` is called first by `main.py` and aborts unless the interpreter is CPython 3.13 or newer (`MINIMUM_PYTHON = (3, 14)`) with the GIL enabled. A free-threaded build (`Py_GIL_DISABLED`, or `sys._is_gil_enabled()` false) is refused: several lock-free fast paths in the runtime rely on single dict/deque operations being atomic under the GIL.

`main.py` then sets, before anything else is imported:

| Setting | Value | Why |
| --- | --- | --- |
| `sys.setswitchinterval` | `0.001` | A thread that releases the GIL (every GL call, every large NumPy operation) otherwise waits up to 5 ms to get it back. |
| `PYOPENGL_ERROR_CHECKING` | `"0"` unless `FIO_GL_DEBUG=1` | PyOpenGL's per-call `glGetError` is skipped. |
| `Terrain.BACKGROUND_BUILDS` | `True` | Terrain height grids are built on the `TerrainBuilder` thread instead of in the frame. |

With `ANDROID_ARGUMENT` or `FIO_PLAYER` set, `main.py` starts `player.main.main` instead of the editor and none of the Qt runtime described here is created (see [Fio Player (Android)](https://github.com/ViciousSquid/Fio/wiki/Fio-Player-(Android))).

## Threads

| Thread | Created as | Lifetime | Rate | Owns |
| --- | --- | --- | --- | --- |
| GUI / render (main) | Qt main thread | process | `QTimer` 16 ms → `QtGameView.update_loop` | All Qt widgets, the GL context and every renderer object, audio playback, the terrain object (`Terrain.advance`/`publish`), console command execution, editor edits, map load/save. |
| Logic | `engine/logic_thread.py: LogicThread(threading.Thread)`, `daemon=True`, default thread name | From `QtGameView.initializeGL` (`_start_logic_thread`) to `closeEvent` (`_stop_logic_thread`, `join(timeout=1.0)`) | 60 Hz fixed step (`TICK_RATE = 60`) | The simulation: every `Logic*` runtime, the play session, the editor camera, and the write side of the render-state double buffer. |
| Monster AI | `engine/monster_ai.py: MonsterAIThread`, `daemon=True`, `name="MonsterAIThread"` | One per play session: `LogicSession.start_monster_ai` at Play, `stop_monster_ai` at Stop (`join(timeout=2.0)`) | 30 Hz fixed step (`tick_rate=30`) | `MonsterAI.update`: the dense monster table, `monster_states`, monster positions and flags. |
| Terrain builder | `threading.Thread(target=Terrain._builder_loop, name='TerrainBuilder', daemon=True)` in `engine/terrain.py` | Started lazily by `Terrain.advance` when chunks are queued; stopped by `Terrain.stop_builder` | Job queue (`BUILD_QUEUE_LIMIT = 8` in flight) | Height-grid generation for queued chunks. It writes no terrain table: results are collected on the GUI thread by `Terrain._collect_built`. |
| Terrain paint packers | `ThreadPoolExecutor(max_workers=min(4, cpu_count), thread_name_prefix='TerrainPaintPack')` in `engine/terrain.py: _compress_paint` | Created on the first save that has two or more paint arrays | On demand | zlib + base64 of terrain paint arrays during a map save. The arrays are never written after they are stored. |
| LAN (Deathmatch plugin only) | Unnamed daemon threads in `plugins/deathmatch/lan.py` (`_accept`, `_discover`, `_connect`, per-connection readers) | While a LAN game is hosted or joined | Socket-driven | Network I/O only. |

The logic thread runs for the whole life of the 3D view, in the editor as well as in Play. Outside Play its tick only moves the editor camera (`LogicEditor.tick`); it still projects and publishes a render frame every iteration, which is how the editor viewport is drawn.

## Ownership of the world

* **Authoring state** — `EditorState.brushes` (list of dicts) and `EditorState.things` (list of `Thing`) — is the single source of truth. Brushes and entities carry a stable UUID ([Stable UUIDs](https://github.com/ViciousSquid/Fio/wiki/Stable-UUIDs)).
* **Play-session state** lives in the runtimes `LogicThread` constructs: `LogicSession` (play flag, world pause, `PhysicsWorld`, `SpatialGrid`, effect store), `LogicPlayer`, `LogicCombat`, `LogicMovers` (which owns the `MoverTable`), `LogicTriggers`, `LogicTiming`, `LogicPortals`, `LogicParenting`, `LogicCollision`, `LogicWorld`, `LogicInteraction`, `PropSession`, `CutsceneRuntime`, `LogicCamera`, `LogicRender`, `LogicEditor`, plus `MonsterAI` and the `IOManager`.
* **Play writes into the authored objects.** Monster health, `dead`, `hidden`, entity positions, collected flags and so on are written to the same `properties` dicts the editor shows. After Stop the editor shows the played state unless *Settings > Play Modes > "Restore the world when leaving Play"* (`[Settings] restore_world_on_stop`, default `False`) is on, in which case `MainWindow._capture_pre_play_world` snapshots the world, undo and redo stacks and the unsaved flag at Play and `_restore_pre_play_world` puts them back at Stop.
* **A mover's or door's live position is not written to its brush.** `brush['pos']` is always the authored placement; the live transform is owned by the `MoverTable` and read through `engine.brush_placement` ([Physics](https://github.com/ViciousSquid/Fio/wiki/Physics)).

## Dense projections

| Projection | Module | Built by | Read by |
| --- | --- | --- | --- |
| `RenderTable` (brush rows) | `engine/render_table.py` | Logic thread, `LogicRender.prepare_render_state` → `table.begin_frame(...)` | Renderer (GUI thread), via the published `RenderState` |
| `EntityTable` (entity rows) | `engine/entity_table.py` | Logic thread, same call | Renderer |
| `MoverTable` (movers and doors) | `engine/mover_table.py` | Logic thread, `LogicMovers._update_movers/_update_doors` | Logic thread; published into the `RenderTable` by `MoverTable.publish`; every other reader through `engine.brush_placement` |
| `MonsterTable` | `engine/monster_table.py` | AI thread, `MonsterAI._update_dense` | AI thread; Debug Tables |
| `PhysicsWorld` SoA buffers | `engine/physics.py` | Logic thread, `PhysicsWorld.step` | Logic thread |
| `SpatialGrid`, `CollisionRows` | `engine/physics.py`, `engine/logic_collision.py` | Logic thread at Play start and on collision invalidation | Logic thread (player, hitscan, projectiles) and AI thread (ground, wall and line-of-sight queries) |
| `DecalTable` (blood) | `engine/decal_table.py` | Logic thread, `LogicCombat._spawn_blood` | Copied into `RenderState.decals` each frame |
| `TerrainFrame` | `engine/terrain_frame.py` | GUI thread, `Terrain.publish()` in `QtGameView._publish_terrain` | Renderer, same thread |

Each `RenderState` owns its own persistent `RenderTable` and `EntityTable`; the logic thread refreshes the write-side pair incrementally from the change journal (`engine/change_journal.py: JOURNAL`) and from `EditorState`'s render-dirty set.

## GUI → logic: input

All input crosses through `engine/threaded_game_state.py: ThreadedGameState`, created by `QtGameView.__init__` as `self.game_state`. Each channel has its own lock; the logic thread drains them inside its tick.

| Channel | Written by (GUI thread) | Read by (logic thread) | Semantics |
| --- | --- | --- | --- |
| Held keys | `update_loop`: `set_keys(editor.keys_pressed)` every 16 ms; an empty set while the console overlay or pause menu is open | `get_keys()` (a copy) | Level-triggered set of Qt key codes. |
| Mouse look | `_poll_mouse_look`: `set_mouse_delta(dx, dy)` once per UI frame from the live cursor, then the cursor is re-centred | `consume_mouse_delta()` | Accumulates until consumed. |
| Use key | `MainWindow.keyPressEvent`: `set_use_key_pressed()` on `E` | `consume_use_key()` | Latched edge, cleared on read. |
| Fire | `QtGameView.mousePressEvent`: `queue_shot()` — only when the published frame says `active_weapon` is set and `shot_ready` is true, and the player is not dead | `consume_shot()` | FIFO; one shot consumed per tick. |
| Weapon slots | `keyPressEvent`: `queue_weapon_slot(n)` for keys 1–4 | `consume_weapon_slots()` | FIFO, in press order. |
| Player 2 | `_poll_gamepad` (16 ms timer) or `_update_p2_keyboard_input`: `set_p2_input(...)` | `get_p2_input()` | Latest-value dict `move_x, move_z, look_dx, look_dy, jump, crouch`. |

UI event handlers that need one fact about the game (is the player dead, is a shot ready) call `game_state.published(name, default)`, which reads one field of the last published frame under the swap lock and borrows nothing.

## Logic → GUI: frames, sounds, console commands

**Frames.** `ThreadedGameState` holds two `RenderState` objects. The logic thread fills the write buffer (`get_write_state()`), then calls `request_swap()`. The renderer borrows the read buffer for the length of one paint (`get_render_state()` … `release_render_state()` in `QtGameView.paintGL`); the borrow is a lease counted in `_read_leases`.

* `request_swap()` swaps only when no lease is out. Otherwise it increments `declined_swaps`, sets `_write_ready`, and returns `False`; nothing is published.
* A declined frame is published by whichever comes first: the renderer releasing its last lease (`_release_render_state_lease` swaps if `_write_ready`), or the logic thread's next iteration, which rebuilds the write buffer with newer state and asks again.
* So a slow paint delays publication by at most that paint, the logic thread never writes a buffer that is being drawn, and there is no third copy of the tables.
* `_swap_locked()` resets the old read buffer (its tables are kept, its slot vectors emptied), increments `published_frames` and sets `_has_new_frame`.
* The UI timer calls `try_swap()`, which consumes `_has_new_frame`; only then does it schedule a paint (`self.update()`). Repainting an unchanged frame would hold the lease back to back and starve publication.
* In Play, the editor's 2D views are repainted at `QtGameView.PLAY_2D_VIEWS_HZ = 10.0`, not at frame rate.
* One-shot frame events stay latched until a frame carrying them is actually published: `LogicThread._publish_frame` clears `combat_runtime.muzzle_flash_active` only after a successful swap.
* The lease has a `weakref.finalize` safety net. Because a garbage collection can run the finalizer on the thread already holding the swap lock, `_OwnedLock` records its holder and the finalizer defers to `_deferred_releases`, folded in by the next locked call.

**Sounds.** Any thread calls `game_state.queue_sound(request)`; the queue is a `deque(maxlen=SOUND_QUEUE_LIMIT)` with `SOUND_QUEUE_LIMIT = 256`, drained each UI frame by `ViewAudio._process_sound_queue`.

**Console commands.** `game_state.queue_console_command(str)` (a `logic_command` entity's `RunCommand`, a trigger's `trigger_save`) is drained each UI frame by `QtGameView._process_console_command_queue`, which runs `console_handler.handle_command(cmd, from_map=True)` on the GUI thread. A command therefore takes effect one UI frame after the tick that queued it.

## Locks

| Lock | Type | Guards | Taken by |
| --- | --- | --- | --- |
| `LogicThread._tick_lock` | `RLock` | The simulation against anything that rebuilds or reads the world from another thread. Held for each frame's ticks and render projection. | Logic thread (`_step_frame`); GUI thread in `LogicSession.apply_play_mode`, `save_session`, `load_session`, `carried_loadout`, `restore_loadout`, `LogicRender.set_hud_fade_enabled`; console commands that add/remove entities or dispatch I/O in Play (`_rebuild_logic_entity_caches`, `_io_dispatch_lock`, delete); `plugins.api` `_entities_changed`; Deathmatch plugin. |
| `LogicSession.monster_lock` | `RLock` | Monster state shared between the AI and logic threads: `MonsterAI.monster_states`, the monster list, monster health and `dead`, the projectile list. | AI thread for the whole of each `MonsterAI.update`; logic thread in `LogicCombat._fire_rays`, `_add_monster_projectile`, `_publish_projectile_render_snapshot`, `_update_monster_projectiles`, `LogicWorld.build_entity_caches`, `LogicCollision.rebuild_if_dirty`, `LogicSession.reset_all_monsters`; console `monster_revive*`. |
| `LogicPlayer.damage_lock` | `Lock` | Player health, armor and `damage_marks` in `LogicTriggers._apply_player_damage` (called from both the logic and AI threads). | Both. |
| `LogicSession._world_pause_lock` | `Lock` | The frozenset of world-pause owners. | Any thread. |
| `ThreadedGameState._render_state_lock` | `_OwnedLock` | The read/write buffer pair, lease count, publication flags. | Logic and GUI threads. |
| `ThreadedGameState._keys_lock`, `_mouse_lock`, `_shot_lock`, `_use_key_lock`, `_slot_lock`, `_p2_lock`, `_sound_lock`, `_console_cmd_lock` | `Lock` | One input or output channel each. | Producer and consumer of that channel. |
| `EditorState._render_dirty_lock` | `RLock` | The render-dirty object set, its epochs and `world_epoch` bookkeeping. | GUI thread (edits) and logic thread (`render_dirty_snapshot`, `render_dirty_since`, `clear_render_dirty`). |
| `ChangeJournal._lock` | `Lock` | Per-subscriber pending change sets. | Any writer (`touch`, `moved`, `set_positions`) and each draining table. |
| `engine/terrain_frame.py: TerrainProducts.lock` | `Lock` | Published per-slot terrain grass/block arrays when a tool reads them from another thread. | GUI thread; tools. |
| `engine/asset_archives._lock` | `Lock` | The asset archive index. | Any thread loading an asset. |
| `engine/physics.py: _CellRows._lock` | `Lock` | Re-deriving the spatial grid's mover rows (`refresh_movers`, `_resolve_asleep`) when a placement stamp moved. | Any thread querying the grid (logic and AI). |

Where two locks are held together the order used throughout the code is `_tick_lock` then `monster_lock` (for example `plugins/deathmatch/plugin.py`). The AI thread holds `monster_lock` and never takes `_tick_lock`.

## Rules for touching state across threads

1. **From the GUI thread, mutate a running play session only under `logic._tick_lock`.** Entering or leaving Play, saving and restoring already do. A console command or plugin that appends or removes entities in Play must then call `logic.world_runtime.build_entity_caches()` under the same lock, because the tick walks indexes built at Play start, not the level lists.
2. **Monster state is written under `session_runtime.monster_lock`.** That includes clearing `monster_ai.monster_states` and changing a monster's `dead`/health from outside the AI.
3. **Never touch Qt or GL from the logic or AI thread.** Ask for a sound with `queue_sound`, a console command with `queue_console_command`. Tick-fault teardown is marshalled through a Qt signal for the same reason.
4. **I/O outputs fired off the tick thread are deferred.** `IOManager.fire_output` compares the caller with the thread that last ran `IOManager.update` (`_tick_thread`); outputs fired by the AI thread (`OnDeath`, `OnSeePlayer`, `OnAttack`, PathNode patrol outputs) go to `_foreign_outputs` and are delivered, in order, at the start of the next `update()` on the logic thread.
5. **Borrow a render frame for one paint only.** Hold `get_render_state()` no longer than a paint; for a scalar use `published()`.
6. **Assign positions, do not mutate them.** `Thing.pos` is a `TrackedPosition` descriptor: assigning a new list journals a move for the render projection. For property changes that affect rendering call `engine.change_journal.touch(obj)`.
7. **Runtime sources replace, never mutate.** A source registered with `brush_placement.register_runtime` changes `runtime_entries` only by single dict operations or by replacing the dict, then calls `note_runtime_change()`, because line of sight reads it from the AI thread.
8. **`hidden` and `disabled` are written through `engine.spatial.set_authored_flag`**, so a Big World-parked object keeps the change when its cell returns.

## World pause

`LogicSession.set_world_paused(owner, paused)` holds or releases a pause for a named owner; the world is paused while any owner holds one (`world_paused`). The pause menu uses the owner `LogicSession.PAUSE_MENU = "pause_menu"`. While paused:

* the logic tick runs `_tick_paused_world`: mouse, use, shot and slot input is drained and discarded, nothing in the world advances, and plugins are still ticked (unless a cutscene, death or the level-complete screen is up);
* the AI thread zeroes its accumulator each iteration, so monsters do not fast-forward on resume;
* animated textures stop (`QtGameView._animate_textures`).

Every play-mode change clears all pause owners. Tested in `tests/engine/test_world_pause.py`.

## Play enter and leave

**Enter** (`MainWindow.enter_play_mode` → `_start_play(player_start)`, GUI thread). A level with no Player Start is refused with a message box before anything changes. Then, under the loading overlay:

1. `_capture_pre_play_world()` (only if `restore_world_on_stop`).
2. `state.materialize_editor_world()` — a Big World map plays on the complete authored world, not the editor's resident cells.
3. `view_3d.preload_level_textures()`.
4. `logic.package = self._package` (a played `.fiopak`'s cutscenes are read from it).
5. `QtGameView.toggle_play_mode(player_start, physics_enabled)`: creates the `Player` at the start (`player_angle = radians(90 - start_angle) + pi`), forces split-screen off, hides the cursor, sets `logic.player_runtime.player`, then calls `logic.session_runtime.apply_play_mode(True, spawn_start=player_start)`. `physics_enabled` is `[Settings] physics` (default `True`).
6. `apply_gore_setting()` and `gc_policy.settle()`.

`LogicSession.apply_play_mode` holds `_tick_lock` for its whole duration, so no tick sees a half-built session. Its order is given on [Logic Thread](https://github.com/ViciousSquid/Fio/wiki/Logic-Thread#play-session-start-and-stop).

**Leave** (`MainWindow._exit_play_mode`, GUI thread): `QtGameView.toggle_play_mode()` stops all audio, drops the pause menu, restores the cursor, calls `apply_play_mode(False)` and clears the players; then `_restore_pre_play_world()`, the Big World editor stream is resumed, and `gc_policy.settle()` runs. `apply_play_mode(False)` stops and joins the AI thread before tearing down session state.

`Esc` in Play opens the pause menu (it does not leave Play); *Exit to Editor* there, or `Esc` while dead, leaves through `MainWindow.leave_play_mode_from_escape` / `_exit_play_mode`.

## Map load

`MainWindow.open_level_file` → `load_level_file` / `_load_package_map` → `_load_level`, all on the GUI thread:

1. The document is validated (`EditorState.validate_level_data`) and, for a level change, the destination Player Start is resolved **before** the open level is touched. A failure leaves the open level and a running game exactly as they were.
2. If a game is running, its weapons are captured (`LogicSession.carried_loadout`: `active_weapon`, `weapons`, `player_ammo`) and Play is ended first — the session's teardown restores state by index into the world it started on, so it must run against that world.
3. The scene is replaced (`_apply_level_data`); the editor camera is placed at the start.
4. For a level change made during Play, Play is started again at the destination start and the loadout is handed back (`restore_loadout`).

A map loaded from a version older than 5 is normalised at load (angled-brush planes localised, play-state keys dropped by `brush_placement.drop_play_state`); see [Map format (.json)](https://github.com/ViciousSquid/Fio/wiki/Map-format-(.json)).

## Tick fault

A simulation tick that raises does not continue and does not kill the thread (`LogicThread._step_frame`):

1. `_tick_faulted = True`, `_tick_fault_message` holds the traceback, and it is logged under `LogicThread`.
2. If a GUI callback is registered (`set_gui_fault_teardown`; `QtGameView` registers `_logic_tick_fault_signal.emit`), it is called once per session with the traceback. The queued signal runs `QtGameView._handle_logic_tick_fault` on the GUI thread, which calls `editor._exit_play_mode()` — the editor's own Stop path, so a world captured at Play is restored and the mode label, tabs and Play button return — and shows the hint "Logic tick failed; Play Mode stopped".
3. With no callback (a headless host), the logic thread calls `session_runtime.apply_play_mode(False)` itself.
4. The accumulator is discarded so no stale simulation debt is replayed.

The next Play clears the fault markers. A failure while *projecting* a frame (`prepare_render_state`) is handled separately: the half-built buffer is not published and the error is logged once per distinct final traceback line. An exception inside `MonsterAI.update` is logged under `MonsterAI` and the AI carries on with the next tick.

Tests: `tests/e2e/test_tick_fault.py`, `tests/threading/test_logic_thread_lifecycle.py` (`test_a_fatal_tick_marshals_play_teardown_to_gui`, `test_a_tick_that_raises_does_not_kill_the_thread`).

## Plugins in the runtime

`LogicThread.__init__` loads plugins once (`plugins.manager.load_plugins`), attaches their I/O handlers (`attach_runtime`) and binds the host (`bind_host(self, kind="engine")`). `FIO_NO_PLUGINS=1` (also `true`, `yes`, `on`) leaves `logic.plugins` as `None` and every per-frame plugin guard short-circuits. Engine events reach plugins through `LogicThread._plugin_emit(event, **data)`, always on the thread that raised them; the events the engine emits are `play_start`, `play_stop`, `player_spawn`, `player_damage`, `player_death`, `player_shoot`, `noise`, `door_open`, `portal_transit`, `trigger_enter`, `trigger_exit`, `world_rebuilt`, `pause_menu_opened` and `pause_menu_closed` (see [Plugin API](https://github.com/ViciousSquid/Fio/wiki/Plugin-API)).

## Tests

| Area | Tests |
| --- | --- |
| Thread lifecycle, tick lock, session reset | `tests/threading/test_logic_thread_lifecycle.py`, `tests/engine/test_logic_thread_lifecycle.py` |
| Tick pacing | `tests/engine/test_logic_tick_pacing.py` |
| Publication under load | `tests/threading/test_pipeline_soak.py`, `tests/threading/test_publication_order.py` |
| World pause | `tests/engine/test_world_pause.py` |
| Input to published frame | `tests/e2e/test_input_to_render_state.py` |
| Play start/stop cycles | `tests/e2e/test_play_cycles.py` |
| Tick fault | `tests/e2e/test_tick_fault.py` |
| AI thread | `tests/monster_ai/test_ai_thread.py` |
