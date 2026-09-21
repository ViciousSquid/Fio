# Fixes applied

Every code fix has a test, and every test was verified to fail with the fix
reverted (negative control) unless noted.

## Engine

| # | File | Change | Guards |
|---|---|---|---|
| 1 | `engine/logic_thread.py` | `_handle_triggers` publishes the use-trigger prompt only when there is one, instead of unconditionally overwriting `current_hud_message` | REG-01 — the reported door/key HUD bug |
| 2 | `engine/logic_thread.py` | `TRIGGER_POLL_EPSILON` applied to the scheduler-tick comparison as well as the per-trigger interval | REG-06 |
| 3 | `engine/physics.py` | `PhysicsBody` regains `size`, `half_extents`, `offset`, `solid`, `gravity`, `friction`, `linear_damping`, `angular_velocity` as index-backed properties | REG-07 |
| 4 | `engine/physics.py` | `PhysicsBody._index` packs on demand, so a handle read straight after `register_body` reports real state | REG-07 |
| 5 | `engine/physics.py` | `step()` excludes bodies their entity marks `disabled`, bounded to awake bodies as the pre-vectorisation loop was | REG-04 |
| 6 | `engine/prop_runtime.py` | `_carry` publishes `"[E] Drop"` only while the prop is still carried | the drop-frame prompt suppressing Tidy's progress line |

## Editor

| # | File | Change | Guards |
|---|---|---|---|
| 7 | `editor/property_editor.py` | `_create_trigger_tab` reads defaults instead of `setdefault`-ing them | REG-05 — selection mutating map data and defeating the page cache |
| 8 | `editor/io_system.py` | `prop` declares `Hide` / `Show` / `ToggleVisibility` | pre-existing: implemented but undeclared |

## Plugins — map migration restored (REG-03)

Reverts of commits 534–540, exactly as they were removed:

* `plugins/api.py` — `FioPlugin.migrate_map_data`
* `plugins/manager.py` — `PluginManager.migrate_map_data` + the
  `required_plugins_for_map` call site
* `editor/editor_state.py` — the pre-deserialisation hook in `load_from_data`
* `player/plugin_host.py` — the call in `build_and_start`
* `plugins/tidy/plugin.py` — `migrate_map_data` and `map_uses_plugin`'s
  `tidyobject` branch

## Test infrastructure

| # | File | Change |
|---|---|---|
| 9 | `conftest.py` | plugins load at **session scope**, before the first per-test `IO_REGISTRY` snapshot, so plugin registrations are not rolled back and lost for the rest of the process |

## Tests repaired (they had drifted behind the engine)

* `tests/logic/test_trigger_filters.py` — helper now builds the scheduler state
  the per-trigger-interval work added (`_trigger_poll_elapsed_by_bid`,
  `_trigger_use_seen`, `_trigger_use_prompts`); `Vec` stub given the sequence
  protocol the vectorised broad-phase needs (`glm.vec3` has it);
  `_process_hurt_trigger` stub given its `poll_interval` argument.
  **This module had never passed since it was written.**
* `tests/physics/test_dynamic_bodies.py` — `Grid` stub carries `cells` and
  `cell_size`, which the batched rewrite reads directly off the spatial grid;
  `Vec` stub given the sequence protocol. **Also had never passed.**
* `tests/renderer/test_light_budget.py` — repaired the literal `\n\n` escapes
  from commit 396 that made the module a `SyntaxError`, and normalised the tail
  to CRLF. While uncollectable it aborted the **entire** pytest run.
* `tests/io/test_io_contract.py` — re-indented the dedented tail of
  `test_io_disabled_source_does_not_fire_output` (a module-level `NameError`,
  **pre-existing at 2.4.2**, which also aborted the whole run); wrapped raw
  connection dicts in `OutputConnection.from_dict`, which is what the runtime
  actually stores; gave the activator test the `logic` reference its handler
  reads; made `_make_entity` resolve types the way `Thing.from_dict` does
  rather than through `ENTITY_TYPES` (the editor palette, which omits `Prop`).

## Test expectations corrected (they asserted things no implementation produced)

* **Friction** — asserted the barrel stops within `10.0` units. Coulomb
  friction with `mu=0.55`, `g=900` gives an analytic stopping distance of
  `v²/2a = 10.10`, so the bound was below the exact answer. Replaced with a
  bound derived from the physics plus discretisation headroom, and the
  reasoning written down.
* **Prop rest height** — asserted the prop rests at its spawn height (40) after
  being dropped onto a floor at y=0. With a 32-unit box centred on the entity
  origin the centre must rest at 16. Corrected, and the invariant that actually
  matters (`pos.y - half_extents.y == floor`) asserted alongside.
* **Mesh-bounds offset** — asserted `(0, -15, -3)`; bounds centre `(0,5,0)`
  minus origin `(10,20,30)` is `(-10,-15,-30)`. Corrected, with the arithmetic
  shown.
* **Cull limit** — passed `1000.0` as `limit_sq`, i.e. a 31-unit radius, while
  expecting everything kept. Squared it, and noted the footgun.
* **`io_enabled` on a target** — wrapped `_execute_input` and asserted it was
  never *entered*, but the disabled-target check lives inside it (the target is
  resolved there). Rewritten to observe the input **handler**, plus a control
  proving delivery resumes when the target is re-enabled. **The engine was
  correct; the test was measuring the wrong thing.**
* **Cull source-text assertions** — two tests matched exact source strings
  (`"self._sort_objects(cull_brushes, cull_things, config)"`,
  `"out=tbuf, keep=self._cull_keep_thing"`) and broke when the renderer was
  reformatted, while the invariants they guard still held. Rewritten as AST
  checks: the culled lists may reach only `_sort_objects`; the `keep` predicate
  guards the Thing cull and never the brush cull. Negative control confirms
  they still fail if the exemption is dropped.

## Tests added

* `tests/persistence/test_legacy_map_integrity.py` — 5 tests pinned to the real
  2.4.2 `tidyobject` record shape: entities survive, `tidy_category` carries
  over, authored identity and appearance are untouched, migration is
  idempotent, a 2.5-format map is left alone, and a legacy map still
  auto-enables Tidy.
* `tests/logic/test_trigger_filters.py` — 3 HUD-ownership tests.
* `tests/physics/test_dynamic_bodies.py` — 3 `PhysicsBody` accessor tests and
  3 disabled/parked-body tests (including an enabled control).
* `tests/renderer/test_render_cull.py` — 12 parameterised
  scalar-vs-batched depth-sort equivalence tests across the
  `min_numpy_count=16` boundary, and 2 tie-stability tests.
* `tests/editor/test_property_editor_rebuilds.py` — selection must author
  nothing into a brush or a Thing, and the page cache must actually hit.
* Restored `plugins/tidy/tests/test_smoke.py::test_legacy_map_migration` and
  `test_auto_enable.py::test_legacy_map_migration_auto_enables`, deleted at
  539/540.

## Result

| Tier (as CI runs it) | Before | After |
|---|---|---|
| Core (headless, no Qt, no GL) | collection aborted | **1372 passed** |
| Editor + engine (Qt, offscreen) | collection aborted; 24 failed once collectable | **2372 passed** |
| Renderer (OpenGL, llvmpipe) | not reached | **33 passed** |

## Reported, deliberately not fixed

* **REG-02** — use-trigger prompt latency. Fixing it means re-introducing
  per-tick work for use triggers; that is a decision about where to spend the
  frame. Recommended shape given in regressions.md.
* **REG-08** — dead `yield_hook` in `editor/procedural_generator.py`. Benchmark
  responsiveness only; the yield granularity is a judgement call.
* **`angular_damping`** on Prop is authored and read by nothing (true at 2.4.2
  as well). Either apply it or drop it.
* **`Light`/`Speaker` `show_radius` has no control** in the property panel
  though `view_2d` draws it. Absent at 2.4.2 too, so a missing feature rather
  than a regression in this range.
* **`SysMon` culled-brush count is derived across the thread boundary** rather
  than taken from `render_state.culled_brushes`.
* **COMPAT-01** — the general "unknown entity disappears" hazard. Two designs
  offered; the choice is the maintainer's.
