# Subsystem audit — triggers, LogicState and I/O

## Triggers

Design at HEAD: a 0.25 s scheduler (`TRIGGER_POLL_TICK`) wakes and polls the
triggers whose own interval (`trigger_poll_interval`, snapped to 1.0 / 0.5 /
0.25) has elapsed. Occupancy is one NumPy broad-phase over
(entities × triggers) with a per-trigger filter bitmask
(`player`=1, `props`=2, `monsters`=4). Contacts are diffed against
`_trigger_contacts` to produce enter/exit events per activator.

**Verified good:**
* Polling work really is proportional to due triggers, not to frames
  (`test_all_trigger_polling_is_not_frame_rate`, `test_scheduler_polls_each_trigger_at_its_own_rate`).
* An unchanged contact set emits no I/O (`test_unchanged_trigger_state_emits_no_io`).
* Re-entry is per-entity, not per-trigger (`test_trigger_filter_reentry_is_per_entity`).
* Use triggers do **not** fire `OnStartTouch` merely because the player entered
  their AABB — activation-driven and touch-driven are kept distinct.
* Each use-key press gets a generation number consumed independently per
  trigger, so a fast trigger cannot steal the press from a slower one.
* Hurt cadence is charged per poll interval, not per tick, and clears on exit.

**Findings:** REG-01 (HUD clobber, fixed), REG-02 (prompt latency, reported),
REG-06 (scheduler float drift, fixed), COMPAT-06 (interval snapping).
Plus: `_trigger_use_prompt` is recomputed by scanning **all** trigger brushes
on each poll, inside the system whose point is not to scan all triggers.

**Floating point:** checked. The two comparisons now share
`TRIGGER_POLL_EPSILON = 1e-9`. The per-body inner accumulator adds exactly
`0.25` from `0.0`, which is exactly representable, so it does not drift; the
outer accumulator sums arbitrary frame deltas and does, which is what the
tolerance is for. The `elapsed %= interval` reset is safe because the inner
accumulator is exact.

## LogicState and I/O

Path traced end to end: `fire_output` → `_get_connections` →
per-connection `output_name` match → `fire_once` guard → delay queue →
`update(dt)` → `_execute_input` → target resolution (`target_id` first, then
`target_name`) → `io_enabled(target)` gate → registered handler, else
`_try_generic_input`.

**Verified good:**
* **Activator propagation** — a chain that is already running keeps its
  activator; one starting here takes its source. 2.5 adds an explicit
  `activator_entity` so the entity that touched a trigger survives every hop,
  **including delayed ones**. Its test existed but had never run (the module
  was uncollectable); it passes now.
* **`io_enabled=False` is honoured on both sides** — a disabled source never
  fires, and a disabled target never receives. The latter was reported as
  failing; that was the test watching `_execute_input` being *entered* rather
  than the input *running*. Proven directly: handler not invoked with
  `io_enabled=False`, invoked when re-enabled.
* **Delay** — `test_fire_output_propagates_explicit_trigger_activator_through_delay`
  now covers a 0.25 s delayed hop.
* **Undeclared-output diagnostic** — firing an output no type declares logs an
  error under the debug gate, and leaves unknown plugin types alone.
* 517 tests across `tests/logic/`, `tests/io/`, `tests/persistence/`,
  `tests/threading/`, `tests/regression/` pass.

**Findings:**
* `prop` implemented `Hide`/`Show`/`ToggleVisibility` via the generic
  dispatcher but **declared** none of them, so no designer could wire them.
  Pre-existing at 2.4.2; declared in this audit, matching `model`.
* The I/O conformance probe resolved classes through `ENTITY_TYPES` — the
  editor's *placeable palette*, which omits `Prop` (placed from the 2D view's
  context menu). So the probe silently skipped the types the palette forgot.
  It now resolves the way `Thing.from_dict` does.
* **Benchmark-era intrusion, fully reverted:** commits 156/159 gave
  `IOManager.fire_output` a second, queue-based dispatch mode with its own
  activator-propagation code path, live in the editor for 125 commits. Commit
  281 removed it; `editor/io_system.py` at that commit is **byte-identical** to
  2.4.2. No residue.
* **Competing sources of truth:** none found in I/O. The registry
  (`IO_REGISTRY`) and the handler table are deliberately two tables, and
  `audit_io_coverage` plus the conformance tests exist precisely to keep them
  agreeing.
