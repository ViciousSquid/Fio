# Subsystem audit — plugin architecture, Tidy, Big World, benchmark

## Plugin architecture

Discovery, loading, enable/disable, registration, type ownership, map-driven
auto-enable, host binding and dispatch are structurally as at 2.4.2. 2.5 adds:

* `migrate_map_data` (514/515) — removed at 534–537, **restored** (REG-03).
* Plugin **menu actions** (543–547) — added three times, deduplicated at 556.
* `main_window=` on console dispatch (502).
* `register_extra_fields` used to extend a **core** entity type (Tidy's
  `tidy_category` on `prop`) rather than registering a new one.
* Plugin versions surfaced in the About dialog rather than the menu (511/512).

### Mutable global/class registration state — the brief's specific concern

**Confirmed and fixed.** `plugins/tidy/tests/test_smoke.py::test_plugin_loads_and_registers`
passed alone and failed whenever another test loaded the plugins first — the
exact "a plugin should not mysteriously lose its registrations depending on
which other test ran first" symptom.

Root cause is not in the plugin system but in `conftest.py`:
`_isolate_process_singletons` restores `IO_REGISTRY` to its state at the
**start of each test**, while plugin registration is a **once-per-process**
event (`discover_and_load` early-outs on `self._loaded`). So the first
`load_plugins()` inside a test had its registrations rolled back at that test's
teardown — and nothing ever registered them again. Every later test saw a
plugin that was loaded and enabled but whose I/O and entity declarations had
silently vanished.

Fixed by loading plugins at **session scope**, before the first snapshot is
taken, so registrations live inside every snapshot while anything an individual
test adds is still dropped. Guarded for the headless tier, which has no PyQt5.

This is a genuine lifecycle-leakage finding, and it was live on both the
559-commit branch and the published snapshot.

### Lifecycle leakage — other checks

* `set_enabled(..., auto=True)` / `disable_auto_enabled` / `auto_enable_for_map`
  behave; `tests/regression/test_plugin_lifecycle_ownership.py` passes.
* `on_play_stop` restores the previous `_prop_drop_interceptor` rather than
  clearing it, so stacked interceptors unwind correctly.
* `tests/plugins/test_plugin_contract.py` fixtures cover a plugin that raises
  on register / connect / tick, a non-plugin module, and duplicate handlers.

## Tidy

Now a plugin composed around core primitives: its objects *are* core Props
carrying `tidy_category`; it owns only `TidyReceptacle` and `TidyGoal`, and
intercepts drops through `logic._prop_drop_interceptor`.

* Objects / receptacles / goals, slot filling, category filtering, goal
  progress and completion: covered and passing (20 tests).
* Pickup/drop integration: the core `PropSession` picks up and carries; Tidy
  consumes the drop when the crosshair is over a valid receptacle.
* HUD: `[E] Put away (<name>)` while carrying over a receptacle, else the
  progress line — **which only appeared once the carry prompt stopped
  lingering on the drop frame** (fixed; `plugins/tidy/tests/test_player.py`
  asserts it end-to-end in the standalone host).
* Map migration: **was deleted, restored** — see REG-03. Existing 2.4.2 Tidy
  maps load intact again.
* Packaging: `.fiopak` bundles a plugin attached to a core entity (521).
* Demo map: bundled in the plugin, loaded through the editor's existing
  unsaved-changes dialog (554/555) — correct handling of a genuinely
  destructive menu action.

## Big World

**Not one of the 559 commits touches `plugins/bigworld/`.** All 102 of its
tests pass. Its risk in 2.5 is therefore entirely *interaction* risk, and one
such interaction was broken:

* **Parking did not stop physics (REG-04).** Parking stashes the authored
  flags and forces `disabled`/`hidden` on. `PhysicsWorld` never read
  `disabled`, so a parked Prop went on falling — costing simulation time while
  dormant and, worse, **drifting away from the position it was parked at**, so
  restoring the cell restored its props somewhere else. Fixed; tested.
* Activation tiers, dormant entities, UUID handling, distance calculations,
  hidden/disabled state and serialisation are otherwise untouched and green.
* The `authored_flag` / `set_authored_flag` split in `engine/spatial.py` — the
  mechanism that lets a save restore what an object *is* rather than what
  streaming temporarily made it — is intact and is what makes the REG-04 fix
  safe to read directly.

## Benchmark

Now `plugins/benchmark/`, an optional developer plugin. The engine carries no
benchmark-specific code (verified: `io_system.py` byte-identical to 2.4.2 after
the revert; `sysmon.py` pull-only; no hooks in `qt_game_view`'s frame path).

Residue worth cleaning:
* `fio_benchmark.py` defines **`load_live_benchmark_world` twice** (lines 370
  and 810); the second shadows the first.
* `benchmark.py` and `benchmark_tests.py` both exist because the 231
  consolidation was partly undone at 306/307.
* It reaches into core internals: `EditorState.load_from_data(save_undo=False)`
  and `logic_thread` camera state from the Qt thread.
* It passes `yield_hook` into `create_map_data` believing brush generation
  yields cooperatively. It does not (REG-08).
