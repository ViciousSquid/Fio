# Architecture — what 2.5 changed, and whether the migrations finished

For each transformation: **what existed → what replaced it → what was removed →
what still depends on the old mechanism → was the migration complete.**

---

## 1. Benchmark: test module → editor dialog → external process → plugin

* **Before (2.4.2):** no benchmark subsystem. Performance was covered by
  targeted tests and the `SysMon` overlay.
* **After:** `plugins/benchmark/` — an optional developer plugin
  (`benchmark.py`, `benchmark_tests.py`, `fio_benchmark.py`, `manager.py`,
  `plugin.py`).
* **Route:** pytest module (001) → editor dialog importing from `tests/` (003)
  → standalone `fio_benchmark.py` (018) → live in-editor instance (037) →
  four-module split (183–190) → consolidated single module (231) → external
  IPC manager process (221–224) → plugin (502). **308 of the 559 commits.**
* **Removed:** the pytest harness, `benchmark_dialog.py`, `benchmark_runner.py`,
  `benchmark_results.py`, `benchmark_host.py`, `benchmark_manager.py` at the
  repo root, the hard-coded Tools-menu entry, the hard-coded console command,
  SysMon push-capture hooks, and the iterative I/O dispatch mode.
* **Still depends on the old mechanism:** nothing. **Verified:**
  `editor/io_system.py` was restored byte-for-byte to its 2.4.2 content at
  commit 281; `engine/sysmon.py`'s only additions over baseline are
  `record_fps`, `reset_metrics` and `get_metrics`, all pull-only and none on
  the frame path; `editor/main_window.py` retains no benchmark code.
* **Complete?** **Yes for the engine** — 2.5 ships with no benchmark-specific
  code in any runtime module, which is the right end state. **Not quite for the
  plugin itself:** the 231 consolidation deleted `benchmark_tests.py`, which
  306/307 then re-created, so `plugins/benchmark/` carries both `benchmark.py`
  and `benchmark_tests.py`. And `fio_benchmark.py` defines
  **`load_live_benchmark_world` twice** (lines 370 and 810); the second
  shadows the first. Harmless today, a trap for the next editor.

**Cost assessment.** Three supervision mechanisms were built and discarded in
sequence (QThread monitor → same-process watchdog → external IPC), and the
module layout was split, consolidated and partly re-split. The subsystem
reached a good design; it did so by trying most of the alternatives in the main
branch rather than by deciding first.

---

## 2. Prop: an editor class → a core engine primitive

* **Before (2.4.2):** `editor.things.Prop(Model)` held the data contract;
  `plugins/entitybase.py` carried a **hand-duplicated** PyQt-free `Prop` for
  the `.fiopak` player and Android. The two default sets were value-identical
  by convention only. `PropSession` owned vertical-only motion.
* **After:** Prop is a physical primitive. It gained `render_mode`,
  `collision_shape` and live `mass`/`friction`; its simulation moved to
  `engine/physics.PhysicsWorld`; `PropSession` was reduced to
  pickup/carry/drop; the standalone player runs the same `PropSession`.
* **The duplication.** Commit 466 is titled "Keep plugin Prop render mode in
  sync" — the two classes being patched by hand. On the **published snapshot**
  this is properly resolved: `engine/prop_entity.py` defines one `Prop`,
  resolving its base at import (`editor.things.Model` when the editor tier is
  importable, else `plugins.entitybase.Model`), and both old names are aliases.
  **Verified at runtime:**

      editor.things.Prop is engine.prop_entity.Prop        -> True
      plugins.entitybase.Prop is engine.prop_entity.Prop   -> True
      key sets equal: True ; differing values: none

  So the answer to "are they genuinely intended to be separate?" is **no**, and
  2.5 has stopped pretending otherwise. This is the single best architectural
  decision in the range.
* **Complete?** Yes on the published snapshot. The 559-commit branch still had
  two classes kept in sync by hand.

---

## 3. Dynamic bodies: PropSession → engine/physics

* **Before:** `engine/physics.py` was brush collision only (`SpatialGrid`).
  Motion for props lived in `PropSession._simulate`: vertical axis only, a
  `raycast_down` floor test, sleep on contact. `mass`, `friction`,
  `collision_size` and `angular_damping` were authored but **unused**.
* **After:** `PhysicsWorld` + `PhysicsBody` with structure-of-arrays state,
  horizontal motion, player push scaled by mass, Coulomb ground friction, swept
  floor contact, mesh-derived bounds, and `collision_shape`.
* **Removed:** `PropSession._simulate` entirely; Prop-specific naming inside
  the engine (427/428 — correctly, a dynamic body is an engine concept).
* **Migration quality:** the *ownership* move (422–428) is the cleanest work in
  the range — it finished, including de-naming. The *vectorisation* (439–443)
  did not: it dropped `PhysicsBody`'s read side (REG-07) and left the
  `disabled` check behind in the now-deleted `PropSession._simulate` (REG-04).
* **New coupling.** `PhysicsWorld._rebuild_static_cells` reads
  `spatial_grid.cells` — the grid's internal dict — and
  `_batch_static_collision` reads `spatial_grid.cell_size`, rather than going
  through `get_potential_colliders`. Speed was bought with coupling to
  `SpatialGrid`'s data layout. This is precisely what broke the test stubs,
  which were written against the public API.

---

## 4. Renderer: per-frame Python → cached and batched

* **Before:** per-frame `isinstance` classification of every Thing in
  `_sort_objects`; per-shader light upload via `_upload_lights_once`; per-object
  model draws; Python depth sort; `cull_by_distance` already had a NumPy path
  (2.4.2's PR #28).
* **After:** cached classification and model/normal matrices; GPU-instanced
  model rendering; a shared std140 light UBO bound by every lit shader
  including terrain; batched cull and depth sort over reusable position
  buffers published by the logic thread; batched shadow-caster distance tests.
* **Removed:** per-frame classification scans; per-shader light upload;
  per-frame list allocation in the cull path.
* **Complete?** Functionally yes. Reached through **eleven repair commits**
  (366, 372–375, 378, 385–387, 390–398), including two separate instances of
  generated GLSL being emitted with literal `\n` escapes instead of newlines —
  the same defect that shipped in `test_light_budget.py` and made the module
  unparseable for the rest of the range.
* **The fragile part.** Shader sources are **rewritten by regular expression at
  runtime** to inject the UBO block (366, 397, 398 are three attempts at that
  regex). It is hard to avoid given the ARM/desktop variant split, but it is
  untested at the string level: nothing asserts the rewritten GLSL is what was
  intended. Recommended: a test that runs the rewrite over each shader source
  and asserts the resulting text compiles-shaped (block present exactly once,
  no leftover `uniform Light` declarations).
* **Deliberate scope, preserved:** shadow and portal passes still operate on
  the **full** scene and do not distance-cull, and the `Light`/`Portal`
  exemption predicate is still handed to the Thing cull only, never the brush
  cull. Both verified, and the second is now asserted structurally rather than
  by matching source text.

---

## 5. Triggers: per-tick scan → scheduled, filtered, vectorised polling

* **Before:** every tick, all trigger brushes, **player only**, cached float32
  AABBs, no allocation.
* **After:** a 0.25 s scheduler; each trigger polled at its own interval
  (1.0 / 0.5 / 0.25 s); one NumPy broad-phase over entities × triggers; filters
  for `player` / `props` / `monsters` with a bitmask; activators propagated
  through I/O including delayed hops.
* **Removed:** the per-tick scan and the player-only assumption.
* **Complete?** The mechanism is complete and the filter work is a genuine
  feature. But it is where the range's two worst regressions live: the HUD
  clobber (REG-01) and the scheduler drift (REG-06), plus the inherent prompt
  latency (REG-02). Its test module had **never passed** since being written.

---

## 6. Tidy: an owned entity type → metadata on a core primitive

* **Before:** `TidyObject(Prop)` with map type `tidyobject`, plus
  `TidyReceptacle` and `TidyGoal`.
* **After:** Tidy objects *are* core Props carrying `tidy_category`; Tidy
  registers that as a Prop extension and intercepts drops via
  `logic._prop_drop_interceptor`. `TidyReceptacle` and `TidyGoal` remain
  Tidy-owned.
* **Removed:** `TidyObject` — and, at 534–540, the migration that made old maps
  loadable (REG-03).
* **Complete?** The architecture is right; the migration was deleted and has
  been restored.
* **Fragile contract.** On interception `PropSession._carry` returns *without*
  clearing `self.held`, relying on the interceptor to do it (Tidy's `_place`
  does). An interceptor that consumes a drop without clearing `held` welds the
  prop to the player. The core should clear `held` itself and let the
  interceptor decide only where the prop goes.

---

## 7. Plugin API surface: registration-only → editor-capable

2.5 widens what a plugin can reach:

* `dispatch_console_command(..., main_window=...)` (502) — from the logic
  thread to the whole editor window.
* Plugin **menu actions** (543–547, deduplicated at 556) receive the
  `MainWindow`; Tidy's demo action loads a map.
* `EditorState.load_from_data(..., save_undo=False)` — a core undo knob whose
  only callers are in `plugins/benchmark/`.
* `migrate_map_data` — a plugin mutates map data before core deserialisation.

At 2.4.2 `EditorAPI` was essentially registration-only. This is a deliberate
direction (Benchmark genuinely needs it), but it makes "is this plugin
trusted?" a real question where it previously was not. Worth stating in
`plugins/API.md`; it currently is not.

Good practice worth keeping: the Tidy demo-map action goes through the editor's
existing `check_unsaved_changes()` dialog (554/555) instead of replacing the
user's document.

---

## 8. Threading

* The `ThreadedGameState` double-buffer and consume-once input accessors are
  unchanged from 2.4.2.
* **New cross-thread data contract:** the renderer now depends on the logic
  thread publishing position buffers (`visible_brush_positions`,
  `visible_thing_positions`) aligned one-for-one with the object lists.
  Commit 352 exists because the first version mis-aligned them. The alignment
  is asserted in `render_cull` (it raises on a shape mismatch), which is the
  right place.
* **New unsynchronised access — `qt_game_view`:** the HUD brush count now
  reads `self.editor.state.brushes` (main thread) and combines it with
  `render_state.visible_brushes` (snapshot). `culled` is derived as
  `total - visible` across that boundary, and `render_state.culled_brushes` —
  the renderer's actual figure — is no longer used. Two sources of truth for
  one number, and the derived one is wrong whenever the editor's brush list has
  changed since the snapshot. Cosmetic on the HUD, but it also feeds
  `SysMon.get_metrics()` and therefore the benchmark's reported culling
  statistics.
* **New unsynchronised access — physics console:** `phys_gravity`,
  `phys_timescale`, `phys_friction`, `phys_damping`, `phys_sleep`,
  `phys_reset` mutate `logic_thread._physics_world` from the **Qt main thread**
  while the logic thread is stepping, and call `wake_all()`. Scalar writes are
  GIL-atomic; `wake_all()` touching the awake array while `step()` masks on it
  is not obviously safe. Not observed to fail, and these are developer tools,
  but they are the only place in Fio that writes simulation state from the UI
  thread without a handoff.
