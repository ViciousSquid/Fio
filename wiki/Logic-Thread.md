> Applies to Fio 3.5.0.1111.

# Logic Thread

`engine/logic_thread.py: LogicThread` is the one thread that advances the simulation. It is a daemon `threading.Thread` that runs a fixed 60 Hz step for the whole life of the 3D view — in the editor (where a tick only flies the editor camera) and in Play — and after each iteration projects the world into a `RenderState` and offers it to the renderer. `LogicThread` itself holds no gameplay rules: it constructs the `Logic*` runtimes, fixes the order they run in, owns the tick lock, and contains faults. This page is the reference for that loop. For the surrounding threads, locks and the frame hand-off see [Fio Runtime Architecture](https://github.com/ViciousSquid/Fio/wiki/Fio-Runtime-Architecture).

## Construction

`LogicThread(game_state: ThreadedGameState, editor_state)` is created by `QtGameView._start_logic_thread` (called from `initializeGL`). It is Qt-free. The constructor builds, in this order:

| Attribute | Class (module) | Owns |
| --- | --- | --- |
| `camera` | `LogicCamera` (`engine/logic_camera.py`) | Editor camera, frustum aspect/FOV, play camera mode (`"First Person"` / `"Overhead"`), overhead height/tilt/orientation, the mode tween. |
| `player_runtime` | `LogicPlayer` (`engine/logic_player.py`) | The `Player` objects (`player`, `player2`), health, armor, death, cheats (`god_mode`, `buddha_mode`, `notarget`), collected keys, damage marks, water sounds. |
| `render_runtime` | `LogicRender` (`engine/logic_render.py`) | Frustum culling, view distance, HUD fades, `prepare_render_state`. |
| `session_runtime` | `LogicSession` (`engine/logic_session.py`) | `play_mode`, world pause, `physics_world`, `spatial_grid`, `monster_lock`, the AI thread, `effect_store`, save/load of a session. |
| `interaction_runtime` | `LogicInteraction` (`engine/logic_interaction.py`) | Use-key door interaction, the HUD prompt line, the level-complete screen state. |
| `editor_runtime` | `LogicEditor` (`engine/logic_editor.py`) | Editor-mode camera navigation. |
| `mover_runtime` | `LogicMovers` (`engine/logic_movers.py`) | The `MoverTable`, mover and door lists and state views, path-following state. |
| `world_runtime` | `LogicWorld` (`engine/logic_world.py`) | Name and id lookup caches, monster/timer/LevelChanger indexes, terrain reference, spawn health. |
| `prop_runtime` | `PropSession` (`engine/prop_runtime.py`) | Prop carrying, collecting, respawns and the Prop spatial index. |
| `io_manager` | `IOManager` (`editor/io_system.py`) | Output dispatch, delayed events, LogicGate state. Input handlers come from `editor/io_handlers.py: register_all_input_handlers`. |
| `plugins` | plugin manager, or `None` | See [Plugin API](https://github.com/ViciousSquid/Fio/wiki/Plugin-API). `None` when `FIO_NO_PLUGINS` is set or the package is absent. |
| `trigger_runtime` | `LogicTriggers` (`engine/logic_triggers.py`) | Trigger volumes, hurt triggers, use prompts, player damage. |
| `monster_ai` | `MonsterAI` (`engine/monster_ai.py`) | Monster behaviour; driven by `MonsterAIThread`, not by this thread. |
| `cutscene_runtime` | `CutsceneRuntime` (`engine/cutscene_runtime.py`) | Cinematic camera and cutscene playback. |
| `portal_runtime` | `LogicPortals` (`engine/logic_portals.py`) | Portal links and player/projectile transit. |
| `parenting_runtime` | `LogicParenting` (`engine/logic_parenting.py`) | Lights and portals parented to movers. |
| `combat_runtime` | `LogicCombat` (`engine/logic_combat.py`) | Weapons, hitscan, monster projectiles, bullet marks, blood decals, player noise. |
| `timing_runtime` | `LogicTiming` (`engine/logic_timing.py`) | `LogicTimer` countdowns and light fades. |
| `collision_runtime` | `LogicCollision` (`engine/logic_collision.py`) | Angled-brush and model collision proxies, the collision brush cache, `CollisionRows`. |

The constructor ends with `_validate_runtime_contracts()`: every name in `_RUNTIME_HOSTS` must be constructed and (except `camera`) have `runtime.logic is self`, and every host attribute listed in `_RUNTIME_HOST_CONTRACTS` must exist. A runtime added out of order fails here with an `AssertionError`, at construction rather than mid-tick. Adding a runtime means adding it to `_RUNTIME_HOSTS` and listing the host attributes it reads in `_RUNTIME_HOST_CONTRACTS`.

Each runtime takes the `LogicThread` as `logic` and reaches its peers through it (`logic.player_runtime`, `logic.session_runtime.spatial_grid`, …). The engine package stays importable without the editor: `editor.things`, `editor.io_system` and `plugins` imports are guarded.

## The loop

```python
TICK_RATE = 60
TICK_DURATION = 1.0 / TICK_RATE
WAKE_MARGIN = 0.0002      # seconds past a tick's due time the loop wakes
```

`run()`:

1. `frame_time = now - last_time`, clamped to at most `0.25` s, added to an accumulator.
2. `_step_frame(accumulator)` — under `_tick_lock`, run `_tick(TICK_DURATION)` once for every whole tick the accumulator holds, then call `render_runtime.prepare_render_state()`. One lock acquisition per frame, so a play-mode change or save/restore from the GUI thread lands between frames, never inside one.
3. `_publish_frame()` — `game_state.request_swap()`, if the frame was prepared. A declined swap (the renderer is mid-paint) is not retried here; the next iteration rebuilds the buffer.
4. Sleep `TICK_DURATION - accumulator - (time spent this iteration) + WAKE_MARGIN`. The loop sleeps until the accumulator next holds a whole tick and wakes just past it, never before; the accumulator carries the remainder, so the rate does not drift. Rendering does no interpolation, so even tick spacing is what keeps motion even (`tests/engine/test_logic_tick_pacing.py`).

`start()` sets `running = True` before the thread exists so a `stop()` that arrives before `run()` is not overwritten. `stop()` clears `running` and stops the AI thread.

Instrumentation written by the loop: `tick_ms` (last tick's duration), `actual_tps` (ticks counted over one second), `RenderState.prepare_ms`, `ThreadedGameState.published_frames` / `declined_swaps`.

## A tick

`_tick(delta)`:

1. `_announce_pause_menu()` — emits `pause_menu_opened` / `pause_menu_closed` to plugins when `session_runtime.pause_menu_open` changed since the last tick.
2. If `session_runtime.play_mode`: `_tick_play_mode(delta)`, then `collision_runtime.rebuild_if_dirty()`.
3. Otherwise `editor_runtime.tick(delta)`.

### Editor tick

`LogicEditor.tick` consumes the mouse delta (`EDITOR_MOUSE_SENSITIVITY = 0.15` degrees per pixel, pitch clamped to ±89°) and moves the editor camera with W/A/S/D, Space (up) and C (down) at `EDITOR_CAMERA_SPEED = 300.0` units/s, times `EDITOR_CAMERA_FAST_MULT = 2.5` with Shift. Nothing else in the world is touched.

### Play tick: exact order

`_tick_play_mode(delta)` returns immediately when there is no player. Otherwise:

| # | Call | What it does |
| --- | --- | --- |
| 1 | `world_runtime.watch_world_rows()` | For 30 ticks after `editor_state.world_epoch` changes, compares the brush and thing lists with the indexed tuples and calls `build_entity_caches()` if membership changed (an edit made while playing). |
| 2 | world pause check | If `session_runtime.world_paused`: run `_tick_paused_world` and return (see below). |
| 3 | `mover_runtime._update_movers(delta)` | `MoverTable.tick_movers`: linear and path-following movers; carries a player standing on one. Movers go first so platform carrying precedes player movement. |
| 4 | `mover_runtime._update_doors(delta)` | `MoverTable.tick_doors`. |
| 5 | `parenting_runtime._update_parented_lights()` | Lights with `parent_mover` follow their mover. |
| 6 | `parenting_runtime._update_parented_portals()` | Portals with `parent_mover` follow their mover. |
| 7 | `io_manager.update(delta)` | Records this thread as the tick thread, delivers outputs other threads fired since the last update, advances the I/O clock and fires delayed events that are due. |
| 8 | `timing_runtime.update_logic_timers(delta)` | Counts down enabled `LogicTimer`s; fires `OnTimer` (and `OnFinished` for `one_shot`). |
| 9 | `timing_runtime.update_light_fades(delta)` | Advances light FadeIn/FadeOut. |
| 10 | `camera.update_camera_transition(delta)` | Advances a First Person ↔ Overhead tween; runs even during a cinematic. |
| 11 | `cutscene_runtime._update_cinematic_camera(delta)` | Advances an active cinematic. |
| 12 | freeze checks | If a cutscene is active, or `player_runtime.player_dead`, or `interaction_runtime.level_complete_ui` is set: consume and discard mouse, use, shot and slot input and **return**. Steps 13–24 do not run. |
| 13 | read input | `keys = game_state.get_keys()`, `consume_mouse_delta()`, `use_key = consume_use_key()`. |
| 14 | `player_runtime.update_primary(delta, keys, dx, dy)` | Look (0.002 rad per pixel, pitch clamped to ±1.5 rad), then `Player.update` with W/A/S/D, Space (jump), C (crouch), Shift (sprint), the collision cache, mover and door lists, terrain, the spatial grid and `world_gravity(physics_world)`. |
| 15 | `player_runtime.update_water_sounds(delta)` | `enterwater.wav` / `exitwater.wav` with a noise event at loudness 0.7; `waterwalk.wav` every `WATERWALK_INTERVAL = 0.45` s while wading faster than 20 units/s. |
| 16 | `interaction_runtime.handle(use_key)` | Clears the HUD prompt, then door proximity and use (reach 80 units horizontally, 64 vertically past the door's half size). |
| 17 | `prop_runtime.tick(delta, use_key)` | Prop carry, drop, collect, respawn. |
| 18 | `physics_world.step(delta, player)` | Dynamic bodies. If `physics_world.player_crushed`, `trigger_runtime._apply_player_damage(CRUSH_DAMAGE)`. Then `prop_runtime.sync_physics_positions()`. |
| 19 | `trigger_runtime._handle_triggers(use_key, delta)` | Samples the use prompt every tick; polls trigger occupancy on a `TRIGGER_POLL_TICK = 0.25` s scheduler, each trigger at its own `trigger_poll_interval` (snapped to 1.0, 0.5 or 0.25 s). |
| 20 | `plugins.tick(...)` | Only when `plugins.wants_tick()`. Receives `use_pressed`, `interaction_consumed` (a HUD prompt is showing), `delta`, and `keys` as a getter. Runs after gameplay so the use edge is intact. |
| 21 | `portal_runtime.update(delta)` | Portal transit, tested against the post-physics player position. |
| 22 | weapons | `combat_runtime.select_slot(slot)` for each queued slot key, then `_handle_shooting()` if `consume_shot()`. |
| 23 | `combat_runtime._update_bullet_marks()`, `prune_noise_events(3.0)`, `_update_monster_projectiles(delta)` | Bullet-mark expiry, noise events older than 3 s dropped, monster projectiles advanced. |
| 24 | `player_runtime.update_player2(delta)` | Split-screen player 2 look and physics. |

Monster behaviour is not in this list: it runs on `MonsterAIThread` at 30 Hz ([Monster AI Thread](https://github.com/ViciousSquid/Fio/wiki/Monster-AI-Thread)). What the logic thread does for monsters is hitscan against them (step 22) and fly their projectiles (step 23), both under `monster_lock`.

### Paused tick

`_tick_paused_world(delta)` discards mouse, shot and slot input, keeps the use-key edge, and — unless a cutscene, death or the level-complete screen is up — ticks plugins with it. Look and fire made over a menu never reach the world. Movers, I/O, timers, physics and triggers do not advance.

## Projecting a frame

`LogicRender.prepare_render_state()` runs after the ticks, inside the same lock hold, every iteration (including editor mode and paused frames). It writes the buffer returned by `game_state.get_write_state()`:

1. **Camera.** Play with a cutscene camera → the cutscene's position, angle, pitch and FOV. Play otherwise → first person at `player.pos + (0, view_height, 0)`, overhead (`LogicCamera._overhead_camera`), or a smoothstep blend of the two during a tween. Editor → the editor camera. Stored as `camera_view_matrix`; play also stores `player_pos`, `player_angle`, `player_pitch`.
2. **HUD state.** `cinematic_camera_active`; `hud_alpha` (0 during a cinematic, then a 4 s fade back to 1); `hud_health_alpha` (idle 0.5; 1.5 s fade in and 4 s fade out on a health change; fixed 1.0 when the fade is disabled); health, armor, ammo, death, underwater flag and tint, collected keys, `hud_message`, `hud_prompt_key`, `active_weapon`, `weapon_switch_serial`, `muzzle_flash_active`, `shot_ready`, `camera_transition_active`.
3. **Effects.** `projectiles` (an `(N, 3)` float32 array), `monster_debug_active` and `monster_debug_rays`, `bullet_marks` (fading over `LogicCombat.BULLET_FADE_TIME = 20.0` s), `decals` from `combat.blood.frame(now, BLOOD_LIFETIME, BLOOD_FADE_TIME)`, and `damage_marks` — `(x, y, z, strength)` per recent directional hit, strength falling from 1 to 0 over `LogicRender.DAMAGE_MARK_TIME = 1.6` s.
4. **Brushes.** `RenderTable.begin_frame(brushes, world_epoch, dirty_objects, edited, peer, peer_dirty)` refreshes only rows the editor's render-dirty set and the change journal name, copying unchanged rows from the peer buffer's table. In Play, `MoverTable.publish(logic, table)` then writes mover and door transforms into the table. Rows hidden by the editor's Filter menu are removed (editor only), then `cull_brushes` frustum-culls (`culling_enabled`). Published as `render_table`, `render_refs`, `visible_brush_slots`, `all_brush_slots`, `total_brushes`, `culled_brushes`.
5. **Entities.** `EntityTable.begin_frame(things, …)`; filtered entity rows are dropped (a filtered light still lights the editor); in Play, Props in `prop_runtime.collected_ids` are dropped. Published as `entity_table`, `entity_refs`, `visible_thing_slots`, `thing_hidden`, `has_portals`.
6. **Player 2.** `player2_*` fields and `splitscreen_active` when a second player exists.
7. `level_complete_ui`, `timestamp`, `prepare_ms`.

The far plane comes from `render_runtime.view_distance.far_plane`; the near plane is 1.0. An exception here leaves the frame unpublished and is logged once per distinct error.

## Play session start and stop

`LogicSession.apply_play_mode(enabled, spawn_start=None)` runs under `_tick_lock`. It first sets `play_mode`, resolves `spawn_start` (the given Player Start, else the level's primary start by `engine.player_starts.pick_start`), clears every world-pause owner and resets `camera.overhead_height_limit`.

**Entering Play**, in order:

1. Clear the tick-fault markers.
2. `mover_runtime._init_movers()`, `_init_doors()`; `parenting_runtime._init_parented_lights()`, `_init_parented_portals()`.
3. `collision_runtime.prepare_angled_brush_collision()` — bake collision triangles for solid angled brushes (before the grid is populated, so it indexes them by true bounds).
4. `collision_runtime.build_model_collision_brushes()` — collision proxies for Props; those flagged `_physics_body` are kept as `_physics_body_brushes`. Then `refresh_collision_brushes_cache()`.
5. `effect_store.begin_session(things)`.
6. Reset player stats: health 100, max health 100, armor 0, not dead, `god_mode`/`buddha_mode`/`notarget` off.
7. Reset triggers, collected keys, each Prop's collection state (`Prop.reset_collection`), `prop_runtime.start()`, hurt timers, HUD prompt, water-sound state, timer states.
8. `combat_runtime.items = editor_state.item_definitions.registry()`; `reset_weapons()` (the player starts unarmed); clear bullet marks, blood, damage marks, muzzle flash.
9. Reset player 2 stats.
10. `reset_all_monsters(clear_dead=True)` — see below.
11. `io_manager.reset()` and `conn.reset()` on every connection of every brush and thing.
12. `world_runtime.build_entity_caches()`.
13. `spatial_grid = SpatialGrid(cell_size=512.0)`, populated with `editor_state.brushes + _model_collision_brushes`; `physics_world = PhysicsWorld(spatial_grid)`, `rebuild(_physics_body_brushes)`; `monster_ai.set_spatial_grid(grid)`.
14. Reset cinematic and camera-transition state; start the health HUD hidden and fading in.
15. `portal_runtime.reset_session()`; clear the level-complete screen and light fades; clear projectiles and noise events.
16. `fire_player_spawn_outputs()` — every Speaker with `play_on_start` receives `PlaySound`, then the spawn start fires `OnPlayerSpawn` and plugins get `player_spawn`.
17. `timing_runtime.init_logic_timers()` — arms timers with `start_on`.
18. `start_monster_ai()` — a new `MonsterAIThread` at 30 Hz.
19. Plugins: `dispatch_play_start(logic)`, then the `play_start` event.

Spawn I/O (16) is deliberately before timers and the AI.

**Leaving Play**: finish a running JSON cutscene without firing its finished output; `stop_monster_ai()` (join, 2 s timeout); reset triggers and once-fired triggers, keys, hurt timers; `_reset_movers()`, `_reset_doors()`, parented lights and portals; `clear_angled_brush_collision()`; clear HUD prompt, timers, light fades, weapons, marks, blood; `prop_runtime.stop()`; clear and drop the `PhysicsWorld` and `SpatialGrid`; reset cutscene, camera tween and HUD fades (health alpha back to 0.5); `portal_runtime.reset_session()`; clear projectiles and noise; `reset_all_monsters(clear_dead=False)`; `release_session_caches()`; then plugins `dispatch_play_stop` and the `play_stop` event.

`release_session_caches()` drops every reference held only for the finished session: world indexes, the collision cache and rows, model collision brushes, mover and door brush lists, `monster_spawn_health`, the I/O manager's state, and each player's `ground_object` and mover broad-phase caches.

`reset_all_monsters(clear_dead)` clears the AI's per-monster state under `monster_lock`, and for every Monster removes `is_shooting` and `_vel_y`, sets `awake = not (triggered or wake_on_sight)`, and — when `clear_dead` (entering Play) — removes `dead` and records the authored health in `world_runtime.monster_spawn_health[id]` for the `Respawn` input.

## Entity caches

`LogicWorld.build_entity_caches()` is the one place play-session indexes are built. It:

* calls `editor_state.ensure_entity_ids()` so everything has a UUID;
* rebuilds `name_cache` and `id_cache` over brushes and things (`find_entity_by_name`, `find_entity_by_id` — what I/O resolves targets with);
* rebuilds the trigger index, the Prop registry, the LevelChanger table, `monster_things` / `monster_by_id`, `timer_things`;
* drops AI state for monsters no longer present (under `monster_lock`);
* re-indexes movers and marks collision dirty if the brush list changed during Play;
* rebuilds portal links;
* emits `world_rebuilt` to plugins.

Call it (under `_tick_lock` when off the logic thread) whenever entities are added or removed during Play. `LogicSpawner`, the console's entity commands and `plugins.api` already do.

## Collision invalidation

`collision_runtime.mark_dirty()` schedules `rebuild_if_dirty()` for the end of the current tick: under `monster_lock` it calls `world_runtime.notify_authored_visibility_changed()`, which refreshes the collision brush cache and repopulates the spatial grid. `world_runtime.notify_visibility_changed()` only counts a drawable-world invalidation and rebuilds nothing. `F6` in Play (`collision_runtime.toggle_model_collision()`) rebuilds Prop collision proxies and physics bodies.

## Fault containment

| Failure | Policy |
| --- | --- |
| `_tick` raises | Mark `_tick_faulted`, log the traceback, request GUI teardown once (or tear the session down natively when no GUI callback is registered), zero the accumulator, stop ticking this frame. See [Fio Runtime Architecture](https://github.com/ViciousSquid/Fio/wiki/Fio-Runtime-Architecture#tick-fault). |
| `prepare_render_state` raises | Frame not published; logged once per distinct error; loop continues. |
| A plugin lifecycle dispatch raises | Printed; the session start/stop completes. |
| `_plugin_emit` listener raises | Swallowed. |

## Extending

* **New per-tick behaviour belonging to a map entity**: prefer an I/O input handler (`io_manager.register_input_handler(type, input, fn)`) or a plugin `tick`; both run on this thread in a defined slot.
* **New engine runtime**: construct it in `LogicThread.__init__`, list it in `_RUNTIME_HOSTS` and `_RUNTIME_HOST_CONTRACTS`, reset its state in both branches of `LogicSession._apply_play_mode_unlocked`, release its references in `release_session_caches`, and insert its call at a deliberate position in `_tick_play_mode`.
* **State the renderer needs**: add the field to `RenderState.__init__` **and** `RenderState.reset()` (both are needed — the buffers are recycled), and write it in `prepare_render_state`.
* **State a save must keep**: see [Save Games](https://github.com/ViciousSquid/Fio/wiki/Save-Games).

## Tests

`tests/threading/test_logic_thread_lifecycle.py` (session reset, AI thread lifetime, fault marshalling, publication), `tests/engine/test_logic_thread_lifecycle.py` (no tick against a half-built session, teardown waits for the tick in progress), `tests/engine/test_logic_tick_pacing.py`, `tests/engine/test_world_pause.py`, `tests/threading/test_pipeline_soak.py`, `tests/e2e/test_play_cycles.py`, `tests/e2e/test_input_to_render_state.py`, `tests/e2e/test_tick_fault.py`.

### Source

`engine/logic_thread.py`, `engine/logic_session.py`, `engine/logic_world.py`, `engine/logic_render.py`, `engine/logic_player.py`, `engine/logic_editor.py`, `engine/threaded_game_state.py`.
