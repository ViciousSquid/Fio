# Renderer M4: change records from the tables to GL

Status: **implemented** on `3.1.0.0611_CANARY` (commits below). M3 stopped
re-uploading unchanged box instances and M3.1 stopped re-deriving what the
passes draw; both still found out that nothing had changed by *comparing*
-- whole columns, visible rows, items and payloads, the row set -- and the
logic thread wrote every mover's pose into the render table every frame,
moved or not. M4 replaces the comparisons with **change records published
by the systems that make the changes**, consumed all the way to the
instance buffers: the tables say what changed, and nothing asks every row.

The direction, its two qualifications (the renderer must not miss a
change; the invariant is flat *change-detection* cost, not flat frame
cost) and the order of the steps were agreed on 2026-11-10; the outline is
in `docs/renderer-m3-profiling.md`, *M4*.

## The invariant, measured

**Establishing that nothing changed costs a constant amount of work,
whatever the size of the world; a change costs work in proportion to the
rows it changed.** Every place that works out what changed reports the
rows it examined and the time it took (`engine/change_detection.py`,
`DETECT`), apart from the frame's rendering work, so the invariant is
measured by counters and asserted by tests, not inferred from frame times.

Change detection on a **still** BigWorld frame -- the logic thread's
publication (`--logic`: `begin_frame` and the cull) and the renderer's
frame -- against M4.0 (the same code before M4, with the counters), at the
map's 3,604 rows and tiled 4 and 16 times (`tools/bench_instance_uploads.py
BigWorld_streaming_test.json --no-raster --logic --scale N`, three
interleaved rounds, medians):

| World | Rows examined | Change detection | `render_scene` CPU | Logic side |
|---|---|---|---|---|
| 1× (3,604 rows) | 25,228 → **0** | 73.6 → 1.0 µs | 0.26 → 0.15 ms | 0.130 → 0.026 ms |
| 4× (14,416) | 67,808 → **0** | 236.8 → 1.6 µs | 0.49 → 0.20 ms | 0.354 → 0.044 ms |
| 16× (57,664) | 197,664 → **0** | 765.1 → 1.7 µs | 0.81 → 0.22 ms | 1.306 → 0.048 ms |

Before M4 every term grew with the world; now the still frame's change
detection is flat at about a microsecond and examines no row
(`tests/visual/test_change_detection_invariant.py` asserts zero rows at 1×
and 4×). `render_scene` still grows a little with the world -- what is
visible grows, and drawing it is rendering work, not change detection.

## What was built

### Change records

* **Transform log** (`RenderTable.transform_version`, `transforms_since`).
  Every write to the transform columns (`bounds`, `rot`) logs the rows it
  wrote: `refresh_transforms`, `refresh_rows`, and `write_transforms` /
  `write_transforms_many` -- the only way a writer outside the table writes
  them. A reconcile or adopt starts the log afresh (readers rebuild). Under
  test (`conftest.py` sets `FIO_SEAL_COLUMNS`) and in oracle mode the two
  columns are **read-only outside the table's write methods**: a write that
  bypasses the log raises at once. Play does not pay for toggling the
  seal (`render_table.SEAL_COLUMNS`).
* **Mover publication** (`MoverTable.publish`). Each row's runtime
  publication (`_publish_into` / `_publish_row`, the one every gameplay
  reader already depends on) marks it; `publish` logs the marked rows and
  writes a render table only the rows marked since *that* table was last
  written, so the two render buffers' tables each catch up on what moved
  during the other's frame. **Dense publication** (M4.5): a group with at
  most `DENSE_SLACK_ROWS` rows unmarked (so every small group) or at least
  `DENSE_SHARE` marked is written whole in one step -- and still logged,
  every row of it, a superset of what moved, so the kept matrices and the
  streams see every change, and the other table catches up on the whole
  group. A group with no marks writes nothing. The two thresholds are
  measured, not assumed (`tools/bench_mover_crossover.py`, below).
* **Row-set versions** (`engine/row_list.py`, `RowList`). `EditorState`'s
  brush and entity lists take a fresh version from one process-wide counter
  on every change to their rows; a table holding the version it reconciled
  recognises the same rows with one comparison instead of an identity test
  per row (`begin_frame`, both tables). A plain list is still compared.
* **The kept matrices** (`SlotTransforms`) rebuild exactly the rows the
  table logged since they last caught up, and log their own rebuilds; an
  instance stream with unchanged items finds the rows to rewrite there, by
  search in an index of its items (`BrushStream.positions_of`).
* **Visible sets as immutable versions.** The logic thread's cull
  (`LogicRender.cull_brushes`) is kept per table: the same read-only array
  while the frustum, the shown mask and the rows stand; rows written since
  are re-tested alone. The play-mode distance cull and the occlusion filter
  keep their results the same way. Downstream, a read-only array that is
  the same object as last frame is unchanged, with no look at a row: the
  passes' kept arrays (themselves read-only, so versions in turn), the
  instance streams' items, spare and payload, the lit pass's selected
  payload.
* **Both alternating tables.** The game draws the two render buffers'
  tables in turn, every frame. A visible set or a pass's kept value made for
  one table adopts the other table's array when the contents are equal
  (compared only when one is made), so consumers see the same objects
  whichever table they drew last.
* **Uniforms** (M4.3). The fog/ambient block and `active_lights` are sent
  to a program when their values change, kept on the program's
  `UniformCache` (program state; a recompiled program, a new context or
  another renderer comes with a new cache).

### Correctness: a missed record must be detected

* **Oracle mode** (`FIO_RENDER_ORACLE=1`, or `=record`): every site that
  trusts a change record also performs the full comparison it replaced --
  the kept matrices against the columns, the kept cull against a fresh
  one, each pass's kept value against a fresh derivation, each stream's
  inputs, the mover rows against the mover table, the row lists against
  their tuples -- and reports a `MissedChange`. Raised where found in unit
  tests; recorded in `record` mode (raising inside a GL frame can take the
  process down) and turned into a test failure by `conftest.py`.
* **Debug Tables as a second oracle** (`tests/e2e/test_change_records.py`):
  its copy of the published RenderTable must equal a table rebuilt from
  scratch from the same world -- which catches a writer that never told the
  table at all, the one case the first oracle cannot see. Debug Tables'
  dashboard now shows the tables' versions and change detection per site
  (rows and microseconds a published frame).
* **The picture**: the long-lived renderer draws exactly what it draws once
  every cache it keeps is dropped.
* **Write paths exercised**, in the real editor with all three oracles:
  editor edits (move, resize, retexture, add, delete), undo and redo, the
  editor camera, mover and door activation in play, I/O inputs (Hide, Show,
  SetTint, ToggleVisibility), a saved game's load, stopping play, renderer
  switching, Big World (a plugin) parking and waking; and, at table level,
  every mover path (tick, doors, restored placements, stop and play again,
  parking, alternating tables) and every table writer. Tests force a missed
  record (a write past the seal, a pose written without being published, a
  brush edited with no journal entry) to show each oracle catches one.

Validation, all with the oracle on:

* `tests/e2e/test_change_records.py` (9 tests, the real editor, three
  oracles per step), `tests/visual/test_change_detection_invariant.py`,
  `tests/brush_model/test_mover_publication.py` (8),
  `tests/renderer/test_render_table_row_local.py` (change log),
  `tests/renderer/test_slot_transforms.py`, `tests/engine/test_row_list.py`,
  and the M3/M3.1 parity tests (`tests/visual/test_box_instance_streams.py`),
  now under the oracle too.
* Mutation checks: the invariant test fails when the row list is a plain
  list (400 rows a frame); the harness test catches a write past the seal;
  the Debug Tables comparison catches a brush edited with no journal entry;
  the mover oracle catches a pose written without being published.
* Full suite under xvfb with GL and `FIO_RENDER_ORACLE=record` (every change
  record checked, a miss fails the test), on `6a66004`: **7,354 passed, 4
  failed -- exactly the documented environment failures** (the position
  window, three `test_portal_fade` cases; the play-cycle memory guard is
  run on its own, below). No test left a missed change record. Dense
  publication (`c43d6b8`) was then validated across the brush-model,
  renderer, visual, e2e, engine, logic and Big World tiers with the oracle
  recording (3,005 passed), with tests that both publication modes leave a
  complete record.

## Measured

Commits on `3.1.0.0611_CANARY`: `84d312e` M4.0 (counters, the cull as one
method, the bench's `--logic` and `--scale`), `9b92066` M4.1 (mover
publication and the transform log), `42f98d7` M4.2a (the kept matrices and
the streams read the logs), `0ca6aec` M4.2b (visible sets as immutable
versions), `ed60466` M4.2c (row-set versions, both tables, the oracle's
record mode, the write-path tests), `0497431` M4.3 (uniforms), `6a66004`
M4.4 (the logic thread's share trimmed), and this record.

**CPU side of a frame** (`tools/bench_instance_uploads.py --no-raster
--logic`, M4.0 -> M4, three interleaved rounds, medians; `render_scene` /
the logic side, ms):

| Scenario | 1× (3,604 rows) | 16× (57,664 rows) |
|---|---|---|
| still | 0.26 / 0.130 -> 0.15 / 0.026 | 0.81 / 1.306 -> 0.22 / 0.048 |
| one mover | 0.50 / 0.148 -> 0.42 / 0.083 | 1.24 / 1.283 -> 0.34 / 0.069 |
| 64 movers | 0.56 / 0.153 -> 0.48 / 0.093 | 1.14 / 1.137 -> 0.40 / 0.086 |
| a selection change | 0.53 / 0.176 -> 0.35 / 0.040 | 1.02 / 1.254 -> 0.51 / 0.048 |
| 32 edits a frame | 0.57 / 0.133 -> 0.44 / 0.092 | 1.36 / 1.278 -> 0.47 / 0.095 |
| two tables in turn, 64 movers | 0.43 / 0.116 -> 0.52 / 0.107 | 1.38 / 1.338 -> 0.48 / 0.103 |
| frustum-culled turning camera | 0.66 / 0.161 -> 0.50 / 0.132 | 5.28 / 1.352 -> 3.94 / 0.635 |

The 1× two-tables row is within this host's noise (rounds 0.40-0.55
against 0.42-0.49; at 4× and 16× it is 0.53× and 0.35×). A turning camera
genuinely changes its visible set every frame, so it still compares and
re-derives what it draws (rendering work; at 16× it uploads 2.5 MB of
newly visible rows a frame). GL calls on every frame: 89-91 -> 73-75
(the uniforms); Python calls on a still frame 663 -> 546.

**The mover publication** (publish alone, five interleaved rounds against
M4.0): at rest 13 -> 3 us (8 movers), 24 -> 5 (128); every mover moving
every tick, the worst case: 17 -> 25 (8), 21 -> 29 (32), 30 -> 41 (128)
before dense publication; with it, +7-10 us remained. That was closed
(M4.5) by not toggling the column seal outside tests (~1.5 us), reading a
whole group's rows as a slice rather than a gathered copy, and writing
whole groups through flat indices kept with the slot map (a 1-D store,
~3x cheaper than `bounds[slots, :3] = ...`). In-process A/B against
M3.1's publication on the same movers, alternating every tick (byte-
identical columns): 1.01-1.03x at 8-32 movers, 0.81x at 128.

**Where dense publication pays** (`tools/bench_mover_crossover.py`: both
modes forced, the publication plus the renderer's work on the rows logged
-- matrices rebuilt, instance rows packed -- two alternating tables, 300
publications, medians). Logging a row needlessly costs the renderer ~0.18
us; picking rows out costs the publication ~6 us a group. Dense is cheaper
at nearly any moving share up to ~64 rows; for 128-512 rows the crossover
lies between 40 % and 75 % moving, the two modes within a few microseconds
of each other near it (and within this host's noise). Hence an absolute
slack for small groups and a share for large ones: `DENSE_SLACK_ROWS = 48`,
`DENSE_SHARE = 0.75`. An ARM CPU and a mobile driver weigh the two sides
differently (uploads cost more there, Python more too): rerun the tool on
the target and set both from what it prints.

**Entity instance buffers and per-cell buffers, measured before building
anything** (gameplay, draws stubbed: CPU only). Sprites, models and effects
upload 0.06, 0.34 and 0.16 KB a frame on `_SHOWCASE` (0.04 KB on
MonsterTest); their passes cost 0.10-0.23 ms CPU each, which is per-pass
work, not uploads. Change-driven entity streams would save next to nothing:
not built. A turning camera's upload (~128 KB at BigWorld's size) costs on
the order of 10 us on this driver; per-cell buffers would cost more in
draws and bookkeeping: not built.

**Production path** (`tools/perf_gate.py`, all probes, five rounds, base
M3.1's head, both trees clean worktrees; first run on `6a66004`):

* `render_scene` frames the same or faster (`_SHOWCASE` still 0.86×);
  gameplay paint on Office 0.93× (faster); BigWorld and MonsterTest
  gameplay the same; the angled-mover table probe 0.89× (faster).
* Flagged: `MoverTable.publish` 1.47-1.61× and the mover probe's frame
  total 1.07-1.13× with every mover moving every tick (the trade-off
  above); `_SHOWCASE` gameplay logic 1.12×, and frame and paint 1.07×
  (p .028); the Python call counts of the logic frame (+25-50 a frame).
* Also flagged in this run and not in the previous full gate (where they
  read 0.96-0.99): `player_static_angled` / `_boxes` 1.20× and
  `table_one_mover_tick` 1.15×. M4.4 added an `EditorState.__setattr__`
  in between; these are being re-run with more rounds to tell a cost from
  this host's noise.

## Trade-offs and what remains

* **The record's cost where everything changes** is now offset: with
  every mover moving every tick the publication is level with M3.1's (the
  log, marks and counters paid for by cheaper writes); at rest it costs
  10-20 us less.
* **The column seal is a test-time check.** In play an unlogged write to
  `bounds`/`rot` is not caught (the suite and the oracle catch it); a stale
  picture rather than a crash in a player's build.
* **Dense publication logs rows that did not move** (by design: a
  superset), which the renderer rebuilds and re-uploads; the thresholds
  are where that costs less than picking, measured on this host only.
* **Frames that change still do work in proportion to what changed** -- by
  design. A turning camera re-culls, compares its new visible set once with
  the last one made (to keep identity when it has not changed) and
  re-derives the passes' arrays; an edit refreshes its rows.
* **The alternating tables share arrays by comparing once when one is
  made.** Cheap, and only on frames that make a new array.
* **Hidden rows are not kept up to date.** A parked mover is neither walked
  nor published until it wakes, when it is refreshed; its row is hidden,
  so nothing draws it meanwhile (before M4 the publication wrote its frozen
  position, which was no more current).
* **`EditorState.brushes` and `things` copy a plain list on assignment.**
  Code that assigns a list and then keeps mutating its own reference would
  diverge; none in the engine, editor or plugins does, and other list types
  are left as they are (and compared).
* **The logic thread pays a few calls a frame for the records** (counting,
  the cull cache, the publication's log): ~25-55 more Python calls a logic
  frame on the gameplay maps, which the gate flags as a count; the time is
  the same or lower except where noted above.
* **Not cached**: the projection and view matrices (38 writers, portal
  views among them) and the shadow cube-map bindings (context state shared
  with the HUD's painter and the terrain) -- about 20 GL calls a frame,
  left as they are. The light UBO is already change-driven; on `_SHOWCASE`
  in play it uploads 4 KB a frame because the streamed lights fade.
* **Oracle mode is slow** (every reuse is re-derived to check it): for
  tests and debugging only.
* **Nothing measured on the Snapdragon 8cx Gen 3 / Adreno 690 target.**
