# Fio 2.4.2.1709_Latest — Architectural Baseline

Baseline tip: `bcf394e3a517e5b633e4bd9750cca3037dc4399d`
("Merge pull request #28 from ViciousSquid/perf/batched-distance-cull", 2026-09-18)

This is the behavioural/architectural reference against which 2.5.0.0_canary
regressions are judged. **The 2.5 design is not assumed correct.**

## 0. Range facts (verified)

* `origin/2.4.2.1709_Latest` is a direct ancestor of local `2.5.0.0_canary`.
  `git merge-base` = `bcf394e` = the baseline tip. 0 commits on base not in canary.
* `git rev-list --count origin/2.4.2.1709_Latest..2.5.0.0_canary` = **559**. Matches the brief.
* **Caveat:** `origin/2.5.0.0_canary` on GitHub is a *single orphan commit*
  ("Initial commit with newer version", 8748305) with **no shared ancestry** with
  either the baseline or the local branch. The real 559-commit history exists only
  on the local checkout. Publishing the audit branch must not be done by
  fast-forwarding onto that orphan. (See compatibility.md / RISK-REPO-1.)
* Net diff: 68 files, +30,692 / -13,025.

## 1. Entity architecture

`editor/things.py` (2115 lines) is the single source of truth for placeable
entities. `Thing` carries `pos`, a free-form `properties` dict, `name`
(property-backed), a per-class auto-numbered counter, `id` (uuid4),
`_io_connections`, plus `to_dict` / `from_dict` (name-matched by
lowercased class name with underscores stripped).

Concrete baseline types: `PlayerStart`, `Light`, `Speaker`, `Monster`,
`Pickup`, `Trigger`, `Model`, `Prop`, `LogicRelay`, `LogicGate`, `LogicTimer`,
`LogicCommand`, `LevelChanger`, `PathNode`.

Entities are data-only: defaults are `setdefault` into `properties` so maps
round-trip through a generic serializer with no per-type format.
`EDITOR_PRIMARY_PROPERTIES` on a class drives the property panel ordering.

### Prop (baseline)

`editor.things.Prop(Model)` — "a generic carryable world object". Renders
`model_path` when set, otherwise `sprite_path` as a camera-facing billboard.
Defaults (data-only, all `setdefault`):

    sprite_path='assets/sprites/pickup.png', sprite_size=[32.0,32.0], mass=1.0,
    collision_size=[0,0,0], no_collision=True, physics_enabled=False,
    gravity=True, friction=0.55, linear_damping=0.08, angular_damping=0.12,
    pickup_enabled=True, pickup_reach=110.0, carry_distance=55.0,
    carry_offset=[0,-6,0], drop_velocity=0.0,
    drop_angular_velocity=[0,0,0], disabled=False

### The deliberate Prop duplication (baseline intent)

`plugins/entitybase.py` (118 lines) is a **PyQt-free fallback**, not a second
engine primitive. Its own docstring states the contract: plugin entity classes
*normally* subclass `editor.things.Model`/`Thing`; `entitybase` exists only for
the standalone `.fiopak` player and the Android build where PyQt5 is absent.

Critically, **at baseline the two Prop default sets are value-identical** — the
same 17 keys with the same values. That equality is the contract. Any 2.5
divergence in defaults/properties/physics/IO/serialization between
`editor.things.Prop` and `plugins.entitybase.Prop` is a regression, not a design
choice, unless 2.5 explicitly re-documents the split.

`entitybase.Thing.to_dict` already diverges by design (it drops
`_io_connections` and always emits `io_connections: []`) — a known fallback
limitation to re-check in 2.5.

## 2. Editor / runtime relationship

* `editor/` is Qt/PyQt5, owns authoring: `main_window.py` (4847),
  `view_2d.py` (4441), `property_editor.py` (3660), `console_commands.py` (2116),
  `io_system.py` (1753), `io_handlers.py` (1650), `logic_graph_widget.py`,
  `logic_wizard.py`, `editor_state.py`, `scene_hierarchy.py`,
  `procedural_generator.py`, `terrain_editor.py`, `surface_inspector.py`.
* `engine/` is the runtime: `logic_thread.py` (3397) is the authoritative
  simulation; `renderer_core.py` (3444) + `renderer_F.py` (699) are GL;
  `qt_game_view.py` (2956) is the Qt/GL widget bridging the two.
* `player/` is the standalone runtime host (`plugin_host.py`) for `.fiopak`,
  deliberately PyQt-free.
* Parity contract: editor play-mode, engine runtime, and the standalone player
  should implement the same entity semantics. Three hosts => three places a
  behaviour change can be half-applied.

## 3. Threading model

`engine/threaded_game_state.py` (369) — double-buffered `RenderState` with
`get_render_state()` / `get_write_state()` / `request_swap()` / `try_swap()`.
Input (keys, mouse delta, use-key edge, shot queue, p2 input, sound queue,
console commands) crosses the boundary through explicit consume-once accessors.
`RenderState.ensure_visible_thing_positions(count)` pre-allocates the
contiguous (N,2) XZ array used by the batched distance cull — i.e. the
snapshot is already NumPy-shaped at baseline.

`LogicThread(threading.Thread)` runs the sim. `_prepare_render_state()` is the
handoff. `_player_damage_lock` guards health; plugin emits deliberately happen
*outside* the lock to avoid handler deadlock. `TICK_DURATION` is the fixed sim
step.

## 4. Renderer (baseline)

`BaseRenderer` (`renderer_core.py`) owns GL resources, `ShaderLoader`
(`assets/shaders` with in-source defaults), `UniformCache` (per-program uniform
location memo with `preload`), `LODManager` (full_dist=500, cull_dist=2000),
`RenderStats`, `BrushGeoMesh`.

* Shader variants: `_compile_common_shaders` then either `_compile_arm_shaders`
  or `_compile_standard_shaders`, chosen by `_detect_lowpower_platform()` /
  `engine.shaders.detect_low_power_arm()` (CPU-name markers).
* Light caps live in `engine/shaders.py`: `MAX_LIGHTS=64`, `MAX_LIGHTS_ARM=16`,
  `MAX_LIGHTS_WATER=8`, `MAX_LIGHTS_TERRAIN=8`, `MAX_SHADOW_LIGHTS=4`.
  `_shader_light_cap(shader_name)` resolves per-shader.
* Passes: lit brushes, textured brushes, glow brushes, models (instanced),
  sprites (instanced), water, glass, fog volumes, terrain, shadow maps
  (`render_shadow_maps`, `_collect_shadow_casters`), portals, editor overlays
  (outline, AABB, face highlight, component overlay, path-node cubes).
* `_upload_lights_once(shader_name, lights)` and `_upload_env_uniforms` exist to
  avoid re-uploading per draw call — the baseline's stated anti-redundancy
  mechanism.
* `_sort_objects` / `_split_opaque` order the transparent pass.

### Distance cull (baseline, post-PR #28)

`engine/render_cull.py` (245) is GL-free and headlessly testable.

* `visible_xz_bounds(cam, corners, y_min, y_max, max_dist)` derives the
  *actual* relevant XZ box from the live camera + world height slab
  (`WORLD_SLAB_MARGIN=512`), which for overhead is far tighter than the far plane.
* `CAMERA_RENDER_CULL_DISTANCE` is only the **default**; the live radius is a
  camera setting on `engine.view_distance.ViewDistance`, read per frame.
* `cull_by_distance(objects, cx, cz, limit_sq, out=, keep=, positions=)`:
  squared compares only; `out` is a persistent scratch buffer (no per-frame
  allocation); `keep` and missing-position are **fail-open** (never wrongly hide);
  `positions` is the vectorised NumPy path with a strict 1-row-per-object
  contract that raises on mismatch.
* Deliberate scope limit: **shadow and portal passes do not distance-cull** —
  they operate on the full scene. Any 2.5 change here is behavioural.
* `renderer_F._camera_distance_cull(brushes, things, camera_pos, thing_positions)`
  is the call site; `_cull_keep_thing` is the fail-open predicate.

## 5. Physics (baseline)

`engine/physics.py` (310) is brush-collision only — no rigid bodies:
`SpatialGrid` with `CELL_SIZE`, `populate(brushes)`, `get_potential_colliders`,
`get_nearby_brushes`, `overlaps_wall(mx,my,mz,margin)`, `raycast_down(x,z,start_y)`,
`has_line_of_sight`.

Player movement/collision lives in `engine/player.py` (1026).
Model collision is *baked into brushes* by `LogicThread._build_model_collision_brushes`
/ `_compute_model_collision_mesh` / `toggle_model_collision`.

## 6. Prop runtime (baseline) — `engine/prop_runtime.py` (179)

`PropSession(logic)` owns **only** entities whose map `type == 'prop'`
(explicitly documented: plugin subclasses reuse Prop's persisted contract but
keep their own rules; a plain Prop map needs no plugin).

* `has_props(things)` — static predicate.
* `start()` — snapshots `_prop_home_pos`, clears `_physics_awake`,
  `_drop_requested`.
* `stop()` — **restores `pos` from `_prop_home_pos`** and pops the transient keys.
  This is the editor play-stop restore contract.
* `is_empty()` — prunes stale refs by `id()` against live things; clears `held`.
* `tick(delta, use_pressed)` — carry if held, else pick on use; then `_simulate`
  only when `self.moving` is non-empty (no per-frame work when idle).
* `_pick_in_view` — linear scan of props; skips `disabled` / `not pickup_enabled`;
  range `pickup_reach`; **aim dot > 0.86**; nearest wins; fires `OnPickedUp`.
* `_carry` — hard-snaps pos to eye + forward*carry_distance + carry_offset;
  sets HUD `'[E] Drop'`; drop on use *or* `_drop_requested`; fires `OnDropped`.
* `_simulate` — **vertical axis only**. `GRAVITY=-900`, `MAX_STEP=0.05`
  (dt clamp), linear damping applied as `v *= 1 - damping*dt`,
  `raycast_down` floor test gated on `no_collision`, rest => pos snapped to
  floor, `_physics_awake=False`, fires `OnRest`, removed from `moving` (sleep).
  Rotation integrates the *constant* `drop_angular_velocity` — there is no
  per-body angular velocity state.
* No horizontal velocity, no prop-vs-prop collision, no mass use, no friction
  use (`friction`/`mass`/`collision_size`/`angular_damping` are authored but
  **unused** by the baseline sim).
* IO: fires `OnPickedUp`, `OnDropped`, `OnRest` via `logic.io_manager`.

## 7. Triggers (baseline) — `LogicThread._handle_triggers`

* **Player-only.** No monster/prop filtering exists at baseline.
* Runs **every tick**; iterates `self._trigger_brushes` (prebuilt list of
  `(bid, brush)`), skips `disabled`.
* Containment via cached float32 `brush_aabb_bounds(brush)` and hoisted scalar
  player x/y/z — deliberately allocation-free (no throwaway `glm.vec3`).
* `trigger_activation`: `'touch'` (default) or `'use'`. Use-activation is a
  radius test (`use_radius`, default 96) + facing dot > 0.5, sets
  `[E] <use_label>` HUD, fires on the use edge, honours `trigger_type=='once'`
  via `fired_once_triggers`.
* Touch: enter/exit tracked in `player_in_triggers` set; `_on_trigger_enter`
  handles `teleport` / `hurt` / `target` actions; `_on_trigger_exit` fires
  `OnEndTouch`.
* `target` fires **both** `OnStartTouch` and `OnTrigger`.
* Hurt re-damage is a countdown: `hurt_trigger_timers[id] -= TICK_DURATION`,
  reset to `HURT_INTERVAL` — i.e. tick-quantised, not wall-clock.
* Plugin events: `trigger_enter` (with action + id), `trigger_exit`.

## 8. LogicState and I/O (baseline)

`editor/io_system.py` (1753) holds the registry and dispatch:
`IODef(name, description, param_type)`, `register_io(entity_type, inputs, outputs)`,
`register_io_alias`, `is_registered_type`, `get_inputs`, `get_outputs`,
plus `io_enabled(obj)` / authored-flag helpers that work on both objects and
plain dicts (`_property_dict`, `_plain_authored_flag`).
`editor/io_handlers.py` (1650) holds the input implementations.
`editor/state_values.py` (439) backs LogicState.

Path: output → connection (`_io_connections` on the source) → delay →
target resolution by name → input dispatch → state mutation.
`LogicThread` caches lookups: `_build_entity_caches`, `_find_entity_by_name`,
`_find_entity_by_id`, `_find_path_node_by_name`.
Baseline tests: `tests/logic/test_logic_state.py` (665),
`tests/logic/test_io_dispatch.py` (596), `tests/io/test_io_contract.py` (577),
`tests/logic/test_logic_composition.py`, `test_logic_primitives.py`,
`tests/persistence/test_logic_round_trip.py`.

## 9. Plugin system (baseline)

`plugins/api.py` (931): `FioPlugin` base (`register`, `describe_properties`,
`register_runtime`, `connect`, `on_play_start`, `on_play_stop`, `on_tick`,
`on_enabled_changed`, `menu_entries`), `EditorAPI` (register_entity, register_io,
register_properties, register_extra_fields, register_property_tab,
register_singleton_entity, register_entity_wizard, register_renderer, globals),
`RuntimeAPI` (register_input_handler, fire_output, entities_of_type, things_near,
player_eye, player_forward, raycast_from_crosshair, spawn, despawn, globals),
`TickContext` (key_down, set_prompt with priority, toast, `_finalize_hud`),
`PropertySpec`/`prop()`, `GlobalStore`, `version_tuple`, `io_def`, `key_code`.

`plugins/manager.py` (783): `PluginManager` with `discover_and_load`,
`_load_one`, `_verify_requirements`, enable/disable (`set_enabled(..., auto=)`),
type→plugin ownership (`_record_entity_owner`, `plugin_for_type`,
`entity_class_for_type`), **map-driven auto-enable**
(`required_plugins_for_types`, `auto_enable_for_types`, `auto_enable_for_map`,
`disable_auto_enabled`), host binding (`attach_runtime`, `bind_host`, `host_for`),
event emit (`emit`, `has_listeners`, `wants_tick`), dispatch
(`dispatch_play_start/stop/tick`, `tick`), and **module-level singletons**
`get_manager()` / `load_plugins()`.

That module-level singleton plus class-level registries is the known hazard:
`conftest.py` already carries `_isolate_process_singletons` (autouse) with
`_snapshot_plugin_state` / `_restore_plugin_state` precisely to stop
registration state leaking between tests.

Baseline plugins: `plugins/bigworld/` (streaming 675, runtime 668, manager 570
+ 3 test modules), `plugins/tidy/` (runtime 511, entities, plugin, tools, tests).
**Benchmark is not a plugin at baseline.** Tidy is already a plugin at baseline.

## 10. BigWorld (baseline)

`plugins/bigworld/{manager,runtime,streaming}.py` with tiered activation,
dormant/parked entities keyed by UUID, distance-based tier selection, and
save/restore. Tests: `test_bigworld.py` (662), `test_bigworld_tiers.py` (672),
`test_bigworld_saves.py` (502).
**Note: no file under `plugins/bigworld/` is touched in the 559-commit range.**
BigWorld risk in 2.5 is therefore *interaction* risk (Prop/physics/renderer/
plugin-lifecycle changes underneath it), not direct modification.

## 11. Serialization / map loading

Generic: `Thing.to_dict` / `Thing.from_dict` keyed on lowercased type name.
Maps are JSON under `maps/`. `engine/savegame.py` (1048) handles save states.
`plugins/packaging.py` builds `.fiopak`. Auto-enable resolves which plugins a
map needs from its entity types before load.
**Release requirement: an unknown entity type must never silently vanish on
load/save.** Baseline `from_dict` returning `None` for an unmatched type is the
exact place that requirement can be violated — to be re-verified in 2.5.

## 12. Player / standalone host

`player/plugin_host.py` (~290): `PlayerPluginHost` with `load(package)`,
`_extract_plugins`, `_activate_required_plugins(map_data)`, `build_and_start`,
`tick(dt, cam_pos, yaw, pitch, ...)`, `camera_override`, `stop`.
`_CamPlayer` is a minimal player stand-in (pos/angle/pitch) for plugin code;
`_NullIO.fire_output` swallows outputs; `_BridgeLogic` is a minimal `logic`
shim exposing `things`.
**Parity hazard:** `_NullIO` means outputs fired by runtime code (e.g. Prop's
`OnPickedUp`/`OnDropped`/`OnRest`) are *silently dropped* in the standalone
player at baseline. Any 2.5 work that makes IO load-bearing for gameplay must
address this.

## 13. Benchmark infrastructure (baseline)

There is **no** `plugins/benchmark/` at baseline. Performance work is covered by
targeted tests (`tests/renderer/test_render_cull.py`,
`tests/renderer/test_light_budget.py`, `tests/renderer/test_render_state.py`)
and `engine/sysmon.py` (438) — an on-screen stats overlay (FPS graph, frame-time
ring, visible/culled/total brushes, VRAM) with `_rebuild_stats_cache` to keep
the overlay off the hot path.

## 14. Performance-critical paths (baseline)

1. `LogicThread._tick_play_mode` — per-tick sim.
2. `_handle_triggers` — per-tick, all trigger brushes.
3. `_prepare_render_state` + `_build_cull_cache` / `_invalidate_cull_cache`.
4. `_aabb_in_frustum_batch` — already vectorised.
5. `render_cull.cull_by_distance` — squared compares, scratch buffer, NumPy path.
6. `renderer_core.draw_models` / `draw_sprites` — instanced.
7. `_upload_lights_once` / `_upload_env_uniforms` — redundant-upload guards.
8. `terrain.*_batch` — NumPy noise/colour generation, chunk LOD + streaming.
9. `PropSession._simulate` — only runs while `moving` is non-empty.

## 15. Test architecture (baseline)

`pytest.ini` + root `conftest.py` (10061 bytes). Autouse fixtures:
`_deterministic_rngs`, `_isolate_process_singletons` (plugin-state
snapshot/restore), `_skip_without_gl`; session-scoped `qt_app`;
`gl_availability` probe; `pytest_addoption` / `pytest_collection_modifyitems`
for opt-in marks.
Layout: `tests/{editor,logic,io,renderer,persistence,integration,monster_ai,
plugins,visual,...}` plus per-plugin suites under `plugins/<name>/tests/`.
