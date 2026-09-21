# Regressions — 2.4.2.1709_Latest → 2.5.0.0_canary

Severity: **S1** breaks map data or core gameplay; **S2** wrong behaviour a
player or author will notice; **S3** internal/perf/latent.

Every entry below was reproduced before being fixed, and each fix has a
negative control (the test fails with the fix reverted) where the fix is code.

---

## REG-01 — S1 — Door, pickup and prop HUD prompts silently removed

**Reported by the maintainer.** Walking up to a locked door no longer said
"NEED: Red Key", and an openable door no longer offered "[E] Open".

*Introduced by:* commit **481 "Run all trigger detection at 1 Hz"**.

*Mechanism.* `LogicThread._tick_play_mode` runs, in order,
`_handle_interactions(use_key)` → `PropSession.tick` → `_check_pickups` →
`_handle_triggers(use_key, delta)`. The trigger rewrite made
`_handle_triggers` do:

    self.current_hud_message = self._trigger_use_prompt

unconditionally. `_trigger_use_prompt` is `""` whenever no use-activated
trigger is in range — which is almost always — so the last stage of the tick
erased whatever the earlier stages had published. Lost prompts:
`"Locked"`, `"NEED: <key>"`, `"[E] Unlock (<key>)"`, `"[E] Open"`,
`"[E] Pick up <item>"`, `"[E] Complete Level"`, and `PropSession`'s
`"[E] Drop"`.

At 2.4.2 the assignment lived *inside* the in-range/facing branch
(`self.current_hud_message = f"[E] {use_label}"`), so it could only ever add a
prompt; it never cleared one.

*Fix.* Assign only when there is a prompt, preserving the baseline precedence
(a use-trigger prompt still wins over an interaction prompt).
*Tests.* `tests/logic/test_trigger_filters.py` —
`test_empty_trigger_prompt_does_not_clear_an_interaction_prompt`,
`test_use_trigger_prompt_still_wins_the_hud_line`,
`test_interaction_prompt_survives_a_full_poll_window`.

**"[E]" for use triggers does still reach the HUD** — confirmed by
`test_use_trigger_prompt_still_wins_the_hud_line`. See REG-02 for its timing.

---

## REG-02 — S2 — Use-trigger "[E]" prompt is up to one second stale

*Introduced by:* commits **481 / 483 / 499–501** (the same rewrite).

`_trigger_use_prompt` is no longer computed per frame. It is a value *sampled*
when a trigger's poll interval elapses (default 1.0 s, selectable 0.5 / 0.25).
Both the in-range test and the facing dot are evaluated against the player's
position and angle **as they were at that poll**.

Consequences at the 1 Hz default: the prompt can appear up to a second after
the player is in position, and linger up to a second after they walk away or
turn around. At 2.4.2 both were exact, every tick.

**FIXED.** The polling architecture is kept for what it was built for and the
prompt is taken off it.

`_refresh_use_triggers` maintains the use-activated subset of the trigger list
(rebuilt wherever the trigger list is, and again on each poll so an activation
mode edited mid-session is picked up). `_sample_use_prompt` evaluates *only*
that subset every tick, batched, against the live player position and angle.
The expensive pass -- every entity against every trigger -- stays exactly where
the rewrite put it.

Cost scales with the number of **use** triggers, not the trigger count:

| use triggers | touch triggers | per tick | ms/s @60Hz |
|---|---|---|---|
| 2 | 50 | 0.025 ms | 1.5 |
| 10 | 200 | 0.039 ms | 2.3 |
| 50 | 500 | 0.088 ms | 5.3 |
| 200 | 1000 | 0.284 ms | 17.0 |

A realistic level pays about 1.5 ms per second of play for a prompt that is
now exact rather than up to a second stale in both directions.

The per-trigger prompt dictionary the poll used to carry between samples is
gone entirely; the prompt is derived, not stored.

*Tests.* `test_use_prompt_appears_and_clears_within_one_tick`,
`test_use_prompt_clears_when_the_player_turns_away`,
`test_use_trigger_prompt_still_wins_the_hud_line`. Verified by mutation:
removing the per-tick call fails 5 of the module's tests.

---

## REG-03 — S1 — A 2.4.2 Tidy map loses every object on load

*Introduced by:* commit **517** (removes the `TidyObject` entity) in
combination with commits **534–540** (remove the plugin map-migration API,
Tidy's migration, and the tests for both).

*Measured, before the fix,* loading the genuine 2.4.2 `maps/Tidy_Test.json`:

    BEFORE: {'tidyobject': 42, 'playerstart': 1, 'light': 1,
             'tidyreceptacle': 1, 'tidygoal': 1}
    AFTER : {'playerstart': 1, 'light': 1, 'tidyreceptacle': 1, 'tidygoal': 1}
    ENTITIES LOST: 41

`Thing.from_dict` returns `None` for an unrecognised type token and prints a
warning to stdout; `EditorState.load_from_data` drops it. Saving afterwards
makes the loss permanent. This breaches the stated release requirement that an
unknown entity must never simply disappear during load/save.

Commit 538 additionally removed `map_uses_plugin`'s `tidyobject` branch, so a
legacy map containing only Tidy objects would not even auto-enable Tidy.

The migration API was removed as "unused" because nothing *in the repository*
called it any more — commit 533 had already converted the one shipped legacy
map. Its real users are maps outside the repository.

*Fix.* Reverted 534–540. Restores `FioPlugin.migrate_map_data`,
`PluginManager.migrate_map_data`, the `EditorState.load_from_data` and
`PlayerPluginHost` call sites, Tidy's `migrate_map_data`, its `tidyobject`
activation branch, and the two deleted test suites.
*After the fix:* 0 entities lost; all 42 recognised by `TidySession` with
`tidy_category == "book"`; migration idempotent; a 2.5-format map untouched.
*Tests.* `tests/persistence/test_legacy_map_integrity.py` (5 tests, pinned to
the real 2.4.2 record shape) plus the restored
`plugins/tidy/tests/test_smoke.py::test_legacy_map_migration` and
`test_auto_enable.py::test_legacy_map_migration_auto_enables`.
*Negative control:* disabling `TidyPlugin.migrate_map_data` fails 2 of the 5.

---

## REG-04 — S1 — A disabled or Big-World-parked Prop keeps falling

*Introduced by:* commits **422–425** (dynamic-body simulation moves from
`PropSession` into `engine/physics.PhysicsWorld`).

The baseline `PropSession._simulate` began each body with:

    if prop is self.held or p.get('disabled'):
        sleeping.append(key); continue

When simulation moved into `PhysicsWorld`, the `held`/kinematic half came with
it but **the `disabled` check did not**. `PhysicsWorld._pack` never reads
`disabled`, and `_dirty` is only set by `register_body`, so toggling the flag
at runtime had no effect at all.

*Measured, before the fix:* a prop dropped from y=400 with
`properties['disabled'] = True` fell to y=16 over one simulated second.

This matters twice over:
* **I/O:** an entity a designer disables via the `Disable` input goes on
  falling and sliding.
* **Big World:** parking a cell stashes the authored flags and forces
  `disabled`/`hidden` on. Dormant props therefore kept costing physics time and
  **drifted away from the position they were parked at**, so restoring a cell
  restored its props somewhere else. (`plugins/bigworld/` is untouched by all
  559 commits — this is purely interaction damage from the physics move.)

*Fix.* `PhysicsWorld.step` now excludes bodies their entity marks `disabled`.
The flag is written in a dozen places across `io_handlers` and the streaming
layer, so there is no single setter to hook and it has to be read; the read is
bounded exactly as the baseline bounded it, to bodies that are **awake**, so a
world with nothing in motion pays nothing.
*Tests.* `tests/physics/test_dynamic_bodies.py` —
`test_a_disabled_body_is_not_integrated`,
`test_a_parked_body_resumes_when_it_is_re_enabled`,
`test_an_enabled_body_still_falls` (control).

---

## REG-05 — S2 — Selecting any brush authored trigger properties into it

*Introduced by:* commits **475 / 499–501** (trigger filters and per-trigger
poll interval), via `PropertyEditor._create_trigger_tab`.

The trigger tab is built for **every** brush and merely hidden when the brush
is not a trigger. It opened with:

    brush.setdefault('trigger_filters', ['player'])
    brush.setdefault('trigger_poll_interval', 1.0)

so simply *selecting* a plain wall wrote two trigger keys into it — mutating
map data on a read-only action, and dirtying the document.

It also silently destroyed the property panel's page cache. The cache
signature is computed from the object's own keys, so a build that adds keys
guarantees the next lookup misses: the panel rebuilt on **every** selection,
drag and rotate — exactly what
`test_moving_a_brush_does_not_rebuild_the_panel` exists to prevent.

*Fix.* Defaults stay in `on_trigger_changed`, where the user has actually made
the brush a trigger; every reader already uses `.get(..., default)`, in both
the panel and `LogicThread`.
*Tests.* `test_selecting_a_brush_does_not_author_properties_into_it`,
`test_selecting_a_thing_does_not_author_properties_into_it`,
`test_the_page_cache_actually_hits_on_reselect`.

*Status on the published snapshot:* already fixed there, independently.

---

## REG-06 — S3 — Trigger scheduler drifts behind its configured interval

*Introduced by:* commits **481 / 499–501**.

The per-trigger interval test carried a `1.0e-9` tolerance; the scheduler-tick
comparison did not:

    while self._trigger_poll_elapsed >= scheduler_tick:

Sixty frames of `1/60` sum to `0.99999999999999989`, not `1.0`, so a 1 Hz
trigger lost one scheduler step every second and polled slightly slower than
authored, cumulatively.

*Fix.* Both comparisons share `LogicThread.TRIGGER_POLL_EPSILON`. The loop
still subtracts a fixed `scheduler_tick` each pass, so the tolerance cannot
spin it.
*Test.* `test_all_trigger_polling_is_not_frame_rate` (which asserts the poll
lands within one simulated second) now passes.

---

## REG-07 — S3 — PhysicsBody lost its read side in the NumPy rewrite

*Introduced by:* commit **439 "Vectorize dynamic physics with batched NumPy
state"**.

Before 439 a `PhysicsBody` carried `size`, `offset`, `gravity`, `friction`,
`linear_damping`, `angular_velocity`, `solid` and `brush` as attributes. The
structure-of-arrays rewrite kept all of that data in the world's arrays but
gave the handle back only `velocity`, `mass`, `awake` and `kinematic`. Nothing
could ask a body what shape it was — including
`tests/physics/test_dynamic_bodies.py`, which asserts `body.size` and
`body.offset` and therefore could not pass.

A second defect from the same commit: `PhysicsBody._index` read
`world._indices` without packing, so **every** accessor returned not-found
defaults (zeros) between `register_body` and the first `step`, silently.

*Fix.* The accessors are restored as index-backed properties reading the packed
row — no duplicated state. `size` keeps its pre-rewrite full-extent meaning
(as `size` does everywhere else in Fio) and `half_extents` exposes the
half-size the batched maths works in. `_index` packs on demand; `_pack`
early-outs unless dirty, so the steady-state path is unchanged.
*Tests.* `test_body_reports_its_shape_from_the_packed_row`,
`test_body_reports_its_authored_material`,
`test_shape_accessors_are_safe_once_the_body_is_gone`.

---

## REG-08 — S3 — Cooperative yielding never happens during brush generation

*Introduced by:* commits **164 / 171 / 172**.

`editor/procedural_generator.py` accepts `yield_hook` on
`generate_brushes_from_grid` and `create_map_data`, and `create_map_data`
threads it through — but **nothing ever calls it**. Verified at HEAD: three
occurrences of the name in the file, none of them an invocation.

The parameter was added by commit 164 *with syntax errors* (both `def` lines
missing their colon, so the module did not parse for 8 commits); 171 "fixed"
it by truncating the file from 1048 lines to 20; 172 restored the file with the
colons corrected but without any yield calls.

The benchmark plugin passes `yield_hook=cooperative_yield` into
`create_map_data` (`plugins/benchmark/fio_benchmark.py:300, 679, 861`)
believing it works, so the Qt event loop is starved for the whole
grid-geometry pass of a large generated map.

*Not fixed here.* It is a benchmark-responsiveness bug in developer tooling,
not a shipped-gameplay regression, and choosing the yield granularity is a
judgement call. **Recommended fix:** call `yield_hook()` every N cells in
`generate_brushes_from_grid`'s main loop, matching the
`index % 64` cadence `EditorState._deserialize_brushes` already uses.

---

## Pre-existing at 2.4.2 — NOT 2.5 regressions

Verified by running the same checks against `origin/2.4.2.1709_Latest`:

* `tests/io/test_io_contract.py` was uncollectable (`NameError: name 'mgr' is
  not defined` from a dedented function tail) at 2.4.2 as well. Repaired here,
  because while it is uncollectable pytest aborts the entire run.
* `prop` implemented `Hide` / `Show` / `ToggleVisibility` through the generic
  dispatcher but declared none of them, so no designer could wire them.
  Declared here, matching `model`.
* The I/O probe resolved entity classes through `ENTITY_TYPES`, the editor's
  *placeable palette*, which omits `Prop` (it is placed from the 2D view's
  context menu). The probe now resolves the way `Thing.from_dict` does.
* `Light`/`Speaker` `show_radius` has no control in the property panel even
  though `view_2d` draws the circle and the key is in
  `EDITOR_PRIMARY_PROPERTIES` — authored-only, console-only. Reported, not
  fixed: it is absent at 2.4.2 too, so it is a missing feature rather than a
  regression in this range.
* `plugins/tidy/tests/test_smoke.py::test_plugin_loads_and_registers` failed
  whenever another test loaded the plugins first. Root cause in `conftest.py`;
  see testing.md. Fixed here, since it makes the whole suite order-dependent.


---

## REG-09 — S2 — A use trigger fires from ~1.7x its authored radius

*Introduced by:* commit **483 "Vectorize complete trigger broad-phase"**.

At 2.4.2 a use trigger's range was a sphere:

    dist = glm.distance(player_pos, t_pos)
    if dist < use_radius:

The vectorised broad phase bounds each use trigger by an **axis-aligned box**
of `±use_radius` — which is the right shape for a batched AABB pass — and the
narrow phase then checks only the facing dot. The box was left as the answer,
so a button became usable from up to `sqrt(3) ≈ 1.73` times its authored radius
along a diagonal, and `use_radius` stopped meaning what a designer authoring it
would expect.

*Fix.* The box stays as the broad phase; the narrow phase now re-tests the
sphere, on both the firing path and the prompt path so the two cannot
disagree. Standard broad/narrow separation, and it restores the 2.4.2 contract
exactly without giving up the batched pass.
*Test.* `test_use_radius_is_a_sphere_not_a_box` — a player inside the box but
outside the sphere (|d| = 113 with radius 100) gets no prompt; moving to
|d| = 85 does. Verified by mutation.

---

## REG-10 — S3 — A spent 'once' use trigger keeps advertising itself

*Introduced by:* commit **481 / 483** (the prompt loop was rewritten without
the check).

2.4.2 suppressed the HUD prompt for a `once` trigger that had already fired:

    already_fired = (trigger_type == 'once' and bid in self.fired_once_triggers)
    if not already_fired:
        self.current_hud_message = f"[E] {use_label}"

The rewritten prompt loop dropped that, so a spent button went on offering
`[E] Activate` and did nothing when pressed.

*Fix.* `_use_prompt_candidates` skips a spent `once` trigger, along with
disabled triggers and any whose filters exclude the player.
*Test.* `test_a_spent_once_use_trigger_stops_advertising_itself`,
`test_a_disabled_use_trigger_shows_no_prompt`. Verified by mutation.
