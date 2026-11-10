# Fio 3.1 brush model, Stage 2: handoff

State at handoff (2026-11-10, after the third session; read *Third
session* first). **Stage 2 is accepted for progression to M3** (the user,
2026-11-10), on the gate recorded in `docs/brush-model-stage2.md` §11.10.
**M3 (change-driven GPU instance uploads for box brushes) is implemented**,
and **M3.1** (what the passes derive kept by change); see the M3 and M3.1
sections below and `docs/renderer-m3-profiling.md`, which also outlines
**M4** (proposed, not started). The earlier sections are kept as history.

## Third session: state published once, the grid's movers, parked movers (read this first)

Full record: `docs/brush-model-stage2.md` §11.8 (published once; the grid's
movers, gathered once), §11.9 (parked movers) and §11.10 (the gate, the
known localised regressions, the pre-existing test failure, acceptance).

### Commits on `3.1.0.0611_CANARY` after `6333b17` (all pushed; tree clean)

* `afffbd5` perf: a mover's runtime state is published once per change into
  its brush's runtime-only slot (`_aabb_runtime`); its world box, float64
  bounds and rotation are derived once by the first reader
  (`constants.mover_box`, `brush_placement.turn_of`) and shared; only the
  changed rows' keys are dropped.
* `c5314a0` perf: movers and doors are filed in the spatial grid for their
  whole travel (`mover_table.travel_range`, kept as `_travel_filed`) -- they
  were filed where play began and lost by every grid query once they left
  that 512-unit cell; rows driven off their travel are marked
  (`brush_placement.off_range`) and added by every grid reader; the
  player's movers come from the same grid, each gathered and narrow-phased
  once a pass; the table's own broad phase (`near_rows`, `movers_near`) is
  removed; the player forgets its mover lists at Stop.
* `fd5118b` feat: parked movers sleep. Rows Big World parks
  (`PARKED_HIDDEN_KEY`) are not walked or published; I/O reaches them at any
  distance (`IOManager._execute_input` settles the target first); state is
  caught up on demand in closed form, ends landing on the walk's ticks;
  missed outputs are never replayed; readers on any thread are answered for
  now (`brush_placement.asleep`, `runtime_now`). Also fixes a walk bug that
  predates Stage 1: a path follower that waited at its last node kept
  running (its `start_on` clear was not journalled).
* `f0f99d6` perf: the grid broad phase keeps each span's merged movers.
* `1c6f58d5` docs: §11.8, §11.9.
* `9b91c54` perf: the `RuntimeOverrides` path (editor preview, tests) --
  the broad phase steps aside when another source moves one of the
  player's movers, the gather asks only for an unpublished mover's
  transform, the resolver shares one rotation with the collision frame.
* This handoff.

Trajectories are bit-identical through every commit (both trace sets, 18
lanes each, production path and `RuntimeOverrides` path, against
`6333b17`).

### Tests

Full suite on `fd5118b`: 6,963 passed, 489 skipped, 1 failed. The failure is
`tests/e2e/test_play_cycles.py::test_repeated_play_sessions_leak_no_threads_or_memory`
(~603 KiB retained over 30 play sessions against a 512 KiB limit). It is
**pre-existing, not a regression**: it fails the same way in isolation on
every tree in this container, Stage 1 included (601–605 KiB); in the full
suite it passed once and failed once on the same code; and none of its top
retained allocations is in a module this session changed (§11.10). Engine, e2e, integration and Big World tiers on
`9b91c54`: 2,066 passed, the same one failure. New tests:
`tests/brush_model/test_published_state.py`,
`tests/brush_model/test_parked_movers.py` (54), and
`tests/brush_model/test_mover_broad_phase.py` rewritten for the grid.

### The gate against Stage 1 (`f0f99d6`, all probes, 5 rounds, core 3)

97 metrics, 17 regressions, all localised (§11.10). Representative
gameplay and every complete mover frame are at or below Stage 1:

| Metric | ×Stage 1 | |
|---|---|---|
| `gameplay_*_frame_ms` (BigWorld, MonsterTest, Office, `_SHOWCASE`) | 0.97–1.00 | same |
| `gameplay_big_logic_ms` / `_calls` | 0.89 / 0.84 | faster |
| `table_*_total` (box, angled, spinning boxes, spinning angled, far doors, one mover) | 0.72, 0.80, 0.77, 0.78, 0.83, 0.98 | faster / same |
| `movers{8,32,128}_frame_total` | 0.57, 0.80, 0.54 | faster / same |
| `ai_*` | 0.46–1.00 | faster / same |
| `frame_*` (GL) | 0.98–1.01 | same |

The regressions, and what they are:

| Metric | ×Stage 1 | What it is |
|---|---|---|
| `table_box_movers_player` | 1.045 | Engine overhead, ~2–3 µs a tick: the grid lookup, the once-each dedupe and the published-slot reads, against Stage 1's direct loop (§11.8's segment table). The box-mover frame total is 0.72×. |
| `table_one_mover_player` | 1.16 | The same structure with one mover; total 0.98×. |
| `table_angled_movers_player`, `table_spinning_boxes_player` (and `_spinning_angled`, 1.31, not significant) | 1.22, 1.23 | Mostly true-shape collision: a convex or turned mover is collided by its planes in its own frame, where Stage 1 collided the AABB (the wrong shape). Totals 0.77–0.80×. |
| `player_*` (`RuntimeOverrides`, not the production path) | 2.30–3.49 at `f0f99d6` | Made worse by `afffbd5`/`c5314a0`, fixed in `9b91c54`: player µs now box 46, angled 54, spinning boxes 59, spinning angled 66 (Stage 1's gate p50s 23.7, 28.6, 31.0, 28.8; `6333b17` 42, 60, 72, 61). Not re-gated. Every override goes through the resolver; plus true-shape collision. |
| `player_static_angled` | 1.22 | Still angled brushes, no movers; was 1.18 (not significant) before this session. Not investigated. |
| `movers32_tick`, `movers128_tick`, `table_far_doors_tick` | 1.22, 1.27, 1.14 | Publication: each changed row's runtime entry and published slot (and its derived key dropped), the cost that makes readers' derivation once per change. Avoidable in principle (a validity key needing no per-row pop). |
| `movers32/128_publish`, `movers32_rows_first` | 1.07–1.12 | The render-table publication and first collision-rows refresh at 32/128 rows. |
| `movers32/128_aabb_moving` | 1.42, 1.25 | A moved mover's first AABB query derives its box (`mover_box`) once per change. |

Measured on the x86 VM only; nothing is measured on the Snapdragon 8cx Gen 3
target.

### Parked movers (§11.9), measured

`tools/bench_parked_movers.py` (64 cycling movers, 16 spinners, 34 doors in
parked cells): tick ~185 → ~7 µs, the AI's batched floor query near the
player ~128 → ~80 µs, placement stamps 1.08 → 0 a tick. Nothing parked:
unchanged (identical calls; ticks within noise).

### Open items (not started)

* `player_static_angled` (1.22×): profile still angled brushes against
  Stage 1.
* The publication cost at 32+ rows (tick 1.22–1.27×): see the table.
* Everything on the target hardware, including `Player.MOVER_NEAR_FROM`
  (3) and `LEG_COUNT_LIMIT`.

### M3: change-driven box instance uploads (implemented; record in `docs/renderer-m3-profiling.md`)

* Commits: `d91d8e0`/`071d3d0` tools (`tools/bench_instance_uploads.py`:
  eight scenarios, uploads, GL and Python calls, memory, `--no-raster` for
  the CPU side alone); `6733997` every render-table row's matrices kept by
  change (`engine/renderer/core/slot_transforms.py`); `d3e7e43` one instance
  stream per pass, view and use (`instancing.BrushStream`), uploading only
  rows that changed; `8e00359` small streams patched too, uploads coalesced
  by call cost; the docs.
* The instance layout, shaders and draw calls are unchanged: slot-resident
  transforms in a buffer texture were rejected (seven texel fetches per
  vertex, 252 per cube, against one attribute read per instance).
* BigWorld: a still frame uploads **0** instance bytes (was 450.5 KB); one
  mover 0.1 KB, 64 movers 8 KB, 512 movers 64 KB, a selection 0.2 KB, 32
  edits 4 KB, two tables in turn 8 KB; the CPU side of `render_scene`
  0.66–0.85× the accepted baseline in every scenario. `_SHOWCASE`: still and
  selection 0 bytes (was 28.6 KB); CPU at or below the baseline except when
  nearly every box moves every frame (+30–70 µs, ~2 %). frames/gameplay gate:
  the same or faster.
* Validation: `tests/visual/test_box_instance_streams.py` (pixel parity with
  a full rebuild through random edits, GPU buffers read back equal to a
  fresh pack, zero-byte still frames with and without shadows, a mover's and
  a selection's own rows, cleanup and renderer switching);
  `tests/renderer/test_slot_transforms.py` (bit-exact matrices). GL tiers:
  905 passed (the three `test_portal_fade` failures are pre-existing). Full
  suite: 6,976 passed, 1 failed -- the pre-existing memory guard, at 615 KiB
  against the baseline's 604: the live tables' kept matrices, flat at
  ~62.7 KiB over 10, 30 and 60 sessions (bounded, not a leak).
* Limitations (in the record): a changed visible set re-uploads from its
  first change (a turning camera, as before, at less CPU); coalescing
  favours fewer calls; sprites, models and effects still repack per frame;
  `MoverTable.publish` still writes every mover's row each frame; nothing
  measured on the target.
* Next: entity instance buffers (sprites, models, effects) the same way;
  `MoverTable.publish` change-driven; an item-stable layout for camera
  motion (per-cell streams); everything on the Snapdragon 8cx Gen 3.

The brief M3 started from (kept as history): change-driven GPU instance
uploads. Start from
`docs/renderer-m3-profiling.md` (profiling only; nothing built): a still
BigWorld frame uploads 450.5 KB in one call, every visible box instance
repacked at 128 bytes in every pass, and `render_table.model_matrices`
rebuilds every visible slot's matrices every frame. Where to look:

* `engine/renderer/forward/instancing.py` -- `_pack_brush_instances` (the
  128-byte layout: model matrix, padded normal matrix, payload) and the
  instance VBO/VAO; `_point_brush_instances_at` (GL 3.3 has no base
  instance);
* `engine/renderer/forward/passes.py` -- `_draw_lit_brushes_instanced`,
  `_draw_face_runs_instanced`, `_submit_brush_runs`;
  `engine/renderer/forward/lighting.py` (the shadow pass packs too);
* `engine/renderer/core/tables.py` -- `_frame_transforms` (the per-frame
  matrix build);
* `engine/renderer/core/geo_store.py` -- M2's pattern to follow: a
  slot-indexed transform buffer texture (seven RGBA32F texels a slot, read
  through `uTransforms` in `forward/shaders.py`), a CPU mirror, and
  dirty-range `glBufferSubData`;
* `MoverTable.publish` -- still writes every mover's and door's position
  and spin into the render table every frame, moved or not.

Constraints to carry over: the two render tables alternate frames (slot
maps keyed by table generation), renderer hot-swapping, OpenGL 3.3 core,
Adreno. Measure upload bytes, GL calls, CPU render time, frame time and
memory; reproduce with `tools/profile_frame_uploads.py
BigWorld_streaming_test.json` (and `_SHOWCASE.json --movers 32`).

### M3.1: what the passes derive, kept by change (implemented; record in `docs/renderer-m3-profiling.md`)

* Commits: `373fc24` `RenderTable.material_version` / `extent_version`
  (move only on a real change to a row's material or extents, and with a
  reconcile or adopt); `4b2368b` the passes keep what they derive
  (`TablesMixin._pass_memo`: classification, the lit pass's classes,
  colours and payload, the textured pass's face batches with UV scale,
  shift and rotation and run descriptors, the shadow pass's caster split),
  per pass, view, use and render table; `824dc87` glass and fog read the
  kept matrices; `14049bc` a selection patches the lit pass's kept payload
  instead of deriving the pass again; the docs (and
  `bench_instance_uploads` now runs with PyOpenGL error checking off, as
  `main.py` runs the game, and reports the kept arrays).
* A box's face state is resolved once per edit; a still frame derives
  nothing and costs one equality test of the visible rows per pass.
* CPU side of `render_scene` against M3's head (`1d7a6b0`), five
  interleaved rounds: BigWorld still 0.44×, one mover 0.43×, 64 / 512
  movers 0.58 / 0.65×, selection 0.59×, 32 edits 0.62×, two tables in turn
  0.60×, turning camera 0.80×; `_SHOWCASE` 0.71-0.92× (64 movers read
  1.05× in the first run, its known noise; 0.80× over seven more rounds). Bytes, uploads and GL calls unchanged; Python calls ~55
  (BigWorld) / ~175 (`_SHOWCASE`) fewer a frame; kept arrays ~180 KB per
  render table on BigWorld. Production gate (clean worktrees, same
  `settings.ini`): frames 0.92-0.94, gameplay 0.94-1.00, no regressions.
* Validation: the GL parity test adds retextures, UV and colour edits and
  natural-scale boxes and checks every kept array against a fresh
  derivation each frame; a per-edit test pins what rebuilds (a still
  frame, a move and a selection: nothing). Mutation checks: dropping `material_version` or
  `extent_version` from the key fails them. GL tiers: 910 passed (the
  three `test_portal_fade` failures are pre-existing). Full suite (xvfb, GL
  included): 7,300 passed, 175 skipped, the known five failed; the memory
  guard reads 630 KiB against M3's 616 alone, the kept arrays (~15 KiB,
  flat over 10, 30 and 60 sessions).
* Not done (measured too small to be worth an aliasing risk): skipping the
  streams' own equality tests by array identity.
* **Next: M4** (direction agreed, not started; outline in the record):
  change sets from the tables to GL. Two requirements from the user:
  * **the renderer must not miss a change** -- every writer is part of the
    contract: writes go through table methods that stamp, columns are
    read-only outside them, an oracle mode runs the old full comparisons
    beside the stamps across the whole test suite, and every audited write
    path (editor edits, drags, undo/redo, load, movers, parked movers, I/O,
    savegame restore, Big World streaming, plugins, play start/stop,
    renderer transitions, visibility inputs) has a test against a
    cache-free renderer; this lands first (step 0), before anything reads a
    stamp;
  * **the invariant is flat change-detection cost**, not flat frame cost:
    establishing that nothing changed is constant per table and pass,
    whatever the world's size (counters of rows examined, asserted at 1×,
    4×, 16× BigWorld); total frame time may grow with what is visible.
  Order (the user's): 1. mover publication and transform dirty sets
  (unchanged mover writes gone first, then matrix updates from explicit
  change records; the contract lands at the start of this step); 2.
  visible-set versioning (generation checks, invalidated on camera, bounds
  and visibility changes); 3. uniform caching (per program and per context,
  with program and GL-context changes tracked); 4. entity instances and
  camera-motion buffers, each benchmarked separately and kept only if it
  reduces complete-frame time. Per-cell instance buffers are **not**
  pursued by default: the ~128 KB camera-turn upload may cost less than
  the bookkeeping to avoid it, particularly on Adreno.

### Environment notes (this session)

* **`settings.ini` is tracked, and the editor tests and an interrupted
  probe leave a `[Layout]` section (the window's geometry) and recent files
  in it.** A gameplay gate then runs the two trees at different window
  sizes -- different frusta, different visible sets, different raster cost
  -- and reports it as a regression (M3.1's first gameplay gate did:
  `_SHOWCASE` paint 1.13×, with every frame drawing a different triangle
  count; with the same `settings.ini` both trees drew identically, frame
  for frame). Run `git checkout -- settings.ini` in both trees before a
  gameplay gate, and after the test suite.
* Full suite ~25 minutes; the full gate (all probes, five rounds) ~13.
* Benchmarks pinned to core 3 with nothing heavy running beside them;
  check `uptime` and use minima and alternation when the VM is noisy.
* Worktrees used: `../fio-stage1` (`56ec0e3`), and one per commit compared.

## Second session: a dense mover broad phase

Full record: `docs/brush-model-stage2.md` §11.7.

* Commits on `3.1.0.0611_CANARY` after `8d42dfc`:
  * `371e1a8` tools: `tools/bench_player_table.py` (the production path:
    each tree's own `MoverTable` ticked, then the player) and the §11.7
    proposal, recorded before the implementation;
  * `9f5d0c6` fix: stopping play with a brush that is both a mover and a
    door raised `IndexError` (a Stage 2b regression; Stage 1 stopped it);
  * `b30a77c` tools: the gate's `table` probe; the bench stops each world
    as the game does, and its `--trace` forces the rare paths;
  * `051b165` perf: `MoverTable.near_rows` + `brush_placement.movers_near`
    + `Player.update` gathering only the movers and doors near the player,
    exact by a displacement guard with a full-list fallback;
  * the docs commit after it.
* Result against `8d42dfc` (production path, player µs a tick): 8 box
  movers 44.9 → 40.7, angled 63.1 → 52.9, spinning boxes 95.1 → 73.8,
  spinning angled 77.1 → 65.0, 34 far doors 116.5 → 32.9 (Stage 1 71.1),
  1 mover 24.6 → 25.1 (+0.6 µs: the structure). Trajectories bit-identical.
  `gameplay` gate: 0 regressions in 20.
* Against Stage 1 the player next to moving shapes is still 1.27–2.07×
  (production path) and 1.71–2.34× (the `RuntimeOverrides` probe), and a
  moved mover's single AABB query 1.34–1.49×. The remaining cost is per
  contact: the movers near the player take the per-brush path (runtime
  miss, collision frame, true-shape planes).
* The experiment patch (`docs/stage2-handoff/turning-ids-gather.patch`) was
  **not applied**: with the broad phase the gather resolves only the
  movers near the player (0.6–1.9 a tick in the scenarios), so its saving
  per gathered mover is now a saving per near mover, and it adds upkeep to
  every publication.
* Lesson measured this session: a NumPy call costs 1–4 µs in the frame
  (caches the mover tick has just used) against well under 1 µs in a
  `timeit` loop. An all-NumPy broad phase cost 13 µs a tick and made the
  8-mover player slower; time candidates in the frame
  (`bench_player_table.py`), not in a loop.

### Next steps (not started)

1. The per-contact cost: hand the near rows' boxes (and, for a turned or
   convex row, its frame) to the player from the columns the broad phase
   already reads, instead of the per-brush runtime miss.
2. `movers*_aabb_moving`: the single-brush miss, untouched.
3. The calibrated gate on the new head: an A/A run, nine rounds of
   `movers,player,aabb,table`, the full gate (GL). Run nothing else on the
   machine meanwhile, or pin the rest away from the gate's core.
4. Everything on the Snapdragon 8cx Gen 3 target, including the
   thresholds `MoverTable.NEAR_VECTOR_FROM` and `Player.MOVER_NEAR_FROM`.

### Environment notes (this container)

* Install the requirements first (`pip install -r requirements.txt
  pytest`); PyGLM was missing.
* Without a display PyOpenGL fails to import (EGL), so even the headless
  tier needs `xvfb-run -a env QT_QPA_PLATFORM=offscreen ...`.
* Qt's `xcb` plugin (the GL probes) needed `apt-get install -y
  libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0
  libxcb-xinerama0 libxcb-xkb1 libxkbcommon-x11-0 libxcb-shape0
  libxcb-randr0 libxcb-xfixes0`.
* The Qt tier took ~25 minutes here.
* Tests elsewhere in the suite leave `RuntimeOverrides` sources registered
  with entries; the broad phase rightly steps aside for them, so
  `test_mover_broad_phase.py` plays with the editor preview alone
  registered (its `one_world` fixture).

## First session (state at `8d42dfc`)

## The architectural requirement

Fio 3.1 must **minimise Python execution in hot paths through dense
numerical tables and change-driven publication**. Optimising Python
dictionary lookups is not the goal; the last round showed it has run out
(see *Rejected approaches*).

The mover table already keeps dense columns (`pos`, `rot_angle`,
`rot_axis`, `live`) and publishes only the rows that change. The remaining
overhead is Python on the *reader* side, one brush at a time:

* Each moved box's AABB is re-derived per brush on its first query after
  every move: find the transform in a runtime source, build the box through
  GLM, write the dict keys, register for invalidation.
* The player's collision passes walk every mover and door in the level in
  Python, every tick (`_has_headroom` once per brush per pass).
* Every placement stamp drops the cached AABB of every mover queried since
  the last one, **including movers at rest**, so a level with N movers
  re-derives up to N boxes a tick when any one of them moves.

Work should scale with contacts and with changed rows, not with movers.

## Branch and commits

* Branch `3.1.0.0611_CANARY`. HEAD **`4db693d`** is pushed; the working
  tree was clean before this handoff.
* Stage 1 baseline: **`56ec0e3`** ("Brush model Stage 1: one material
  record per face").
* Stage 2 commits run `a3f2253`..`4db693d` (design, 2a–2d, fixes, and the
  performance commits `2741be4` … `f1556d0`); `git log 56ec0e3..HEAD` lists
  them. The full record is in `docs/brush-model-stage2.md`, with the gate
  in §11.6.
* This handoff commit adds:
  * this file;
  * `tools/bench_player_movers.py`: a per-tick call count, first and
    second update timings, and a bit-exact trajectory trace, on any tree;
  * `docs/stage2-handoff/turning-ids-gather.patch`: the unvalidated
    experiment below, **not applied**.

## Measurements: player beside eight moving boxes

Measured on the session VM (4 vCPU Xeon @ 2.1 GHz, CPython 3.13, NumPy 2.5,
llvmpipe). Nothing has been measured on the Snapdragon 8cx Gen 3 / Adreno
target.

Python calls in one `Player.update` per tick (cProfile, 900 ticks;
deterministic):

| Tree | Calls per tick | vs Stage 1 |
|---|---|---|
| Stage 1 `56ec0e3` | 283.4 | -- |
| HEAD `4db693d` | 341.5 | **+58.1** (the original "+58") |
| HEAD + experiment patch | 310.4 | **+27.0** |

The first count of +58 (285.4 against 343.5) came from a harness that also
profiled its own two timer calls; `bench_player_movers.py` gives the same
differences.

What the remaining +27 calls a tick are:
* 8 `_brush_aabb_runtime` (one per moved box);
* 9 `id()`;
* 6 dict `get` (HEAD has +29);
* 2 `getattr`;
* one `resolved_runtime` (step-up);
* one `turning_ids`.

Time per update (µs, min / median of 9 interleaved runs, pinned to core 3):

| Tree | First update after the move | Second (all cached) | Miss share per moved box |
|---|---|---|---|
| Stage 1 | 16.8 / 21.4 | 13.0 / 16.6 | 0.48 / 0.60 |
| HEAD | 29.3 / 37.1 | 18.5 / 23.7 | 1.30 / 1.67 |
| HEAD + patch | 26.6 / 29.9 | 16.8 / 18.8 | 1.23 / 1.38 |

**Remaining first-update overhead with the patch: +9.8 µs at the minimum
and +8.5 µs at the median (about 1.4–1.6× Stage 1).** It splits into:
* about 6 µs from the eight moved-box misses (+0.75 µs each);
* about 3–4 µs in the cached update (the step-up's resolution, the
  turning check, the extra dict tests).

The source update itself (`RuntimeOverrides.set`, ~7 µs for 8 boxes,
against Stage 1's ~1.5 µs `pos` writes) is not timed by the gate.

One moved box's AABB miss, priced ingredient by ingredient (ns, timer
corrected, min of 15 rounds):

| Ingredient | ns |
|---|---|
| Whole miss | 890 |
| Stage 1's whole miss, for comparison | 500 |
| GLM box arithmetic, the same in both | ~365 |
| Source scan | ~150 |
| `_MoverKey` tag | 68 |
| Registry | 34 |
| Stamp recheck | 28 |
| Per-stamp cache | 28 |

No single ingredient is worth chasing; the per-brush Python path is.

## Remaining regressions

Gate on HEAD's code (`f1556d0`), 73 metrics, five rounds: 14 flagged. A
nine-round run separates the robust results from noise; an A/A run of
Stage 1 against itself flagged 1 metric of 37.

| Metric | Ratio vs Stage 1 | Cause |
|---|---|---|
| `player_spinning_boxes` | 2.34–2.47× | ~+17 µs runtime AABB path, ~+17 µs true-shape work (a rotated frame and plane tests per spinner) |
| `player_angled_movers` | 2.15–2.26× | +8.5 µs overhead, +9.5 µs true-shape work |
| `player_spinning_angled` | 2.09–2.26× | +15 µs overhead, +4.8 µs true-shape work |
| `player_box_movers` | **1.76–1.77×** | +13 µs overhead with no true-shape work: the per-brush miss, the gather and the step-up (the patch takes about a third of it off) |
| `movers{8,32,128}_aabb_moving` | 1.2–1.6× | The first query of a moved mover resolves it through the runtime layer, averaged over 20 queries a tick |
| `player_static_angled`, `player_static_boxes`, `movers*_tick`, `movers*_publish`, `movers32_rows_first`, `frame_big_moving_ms` | 1.07–1.25× | Not demonstrated: not significant over nine rounds, or contradicted by other runs |

Representative gameplay (`_SHOWCASE`, MonsterTest, BigWorld,
Office_Corridor) is level: frame, logic, paint and AI ratios are 0.99–1.13
and none is significant, and logic-frame Python calls are within 0.6 %.
The regressions are synthetic worst cases: eight shapes, all moving, beside
the player's path. The collision ablation and the per-metric reasoning are
in `docs/brush-model-stage2.md` §11.6.

## The experiment patch: worth preserving, not yet validated

`docs/stage2-handoff/turning-ids-gather.patch` (applies cleanly to
`4db693d`).

* `brush_placement.turning_ids()` gives a per-stamp union of each source's
  `turning_ids` set: the ids whose entry has a non-zero angle. It is a
  superset, and `None` if any source cannot say.
  * `RuntimeOverrides` and `MoverTable` keep the set: the id is added
    before a turned entry is stored and discarded after an unturned one.
  * `MoverTable._adopt` builds the set aside, and `reset` replaces it.
* The `Player.update` gather tells a box by two dict tests. A box mover or
  door is resolved only if its id is in `turning_ids()`, so non-turning
  box movers go straight to the AABB passes.
* The step-up reads a box mover's top from `resolved_runtime` inline, with
  no `world_bounds` call.
* The `_aabb_runtime` key and `brush_aabb_runtime()` are removed (their
  only reader was the old gather).

Validated so far:
* headless tier: 3,791 passed, 140 skipped (the same as HEAD);
* trajectories byte-identical to HEAD over 18 lanes × 960 ticks (`--trace`);
* gate against HEAD, `movers,player,aabb`, five rounds: 33 metrics, 0
  regressions (`player_box_movers` 0.88×, `player_spinning_boxes` 0.81×,
  `movers128_publish` 0.93×).

**Not yet run:**
* the Qt tier;
* the GL tier and the full suite;
* the `static`, `ai`, `frames` and `gameplay` probes;
* a gate against Stage 1.

One thing to watch: the set upkeep per publication when spinners move (the
movers probe spins every third mover; no regression was seen). A
zero-per-tick alternative derives the superset from the `rotate` column
plus restored angles at `sync`/restore time.

## Next steps (designed, not implemented)

(First session's list. Step 2 became the second session's broad phase, in
the form measured best: the reader scans the table's `pos` column against
per-row reaches rather than the table writing float32 bounds at
publication. Step 1's aim -- invalidation proportional to what was asked --
followed from it: the player now queries only near movers, so a stamp
drops only theirs. See *Second session*.)

1. **Change-driven invalidation (dirty rows).** Writers pass the brushes
   they changed: `note_runtime_change(changed)`.
   * The stamp then drops only the cached keys that are both changed and
     registered, iterating whichever set is smaller. That is never more
     work than now.
   * Registered movers at rest keep their hits.
   * Source registration and unregistration, `reset`, `_adopt`,
     `clear()` and authored changes still drop everything.
   * The writers: `MoverTable._publish` (return `chosen` from
     `_publish_into`), `_publish_row_now`, `_store_one`, and
     `RuntimeOverrides.set` / `clear(brush)`.
   * Measure it on a scene where most queried movers are at rest
     (BigWorld has 34 movers).
2. **A dense effective-bounds projection.**
   * In the same NumPy step as publication, the mover table writes float32
     `lo/hi` for the changed rows. This is bit-identical with GLM if done
     in float32: `f32(c) - f32(size)*0.5`.
   * The player tests all mover and door boxes against its swept box in
     one vectorised call, and runs Python only for the boxes it overlaps.
   * The per-brush dict cache stays as the path for single-brush queries.
   * A turned row's half-extents come from `settle_turns`.
   * This removes per-mover Python from the player's passes and from the
     miss.
3. Validate the patch (or its successor), then run the calibrated gate:
   an A/A run, nine rounds on `movers,player,aabb,frames`, and the full
   gate. Update `docs/brush-model-stage2.md` §11.6.

## Rejected approaches

| Approach | Result |
|---|---|
| Lazy moving-shape collision frames | Bit-identical, but slower: 522 → 575 calls a tick; spinning boxes 51 → 58 µs. Reverted. |
| A resolved transform cached as a `_placed` key on brushes | Broke `test_projecting_boxes_adds_nothing_to_them_and_solves_nothing`. Replaced by the resolver dict per stamp (`2cf2734`). |
| A cache keyed on the identity of `pos` | Broke `test_grouped_floor_query_reads_mover_heights_live`, which mutates `pos` in place. |
| Pushing derived AABBs into brush dicts at publication | Estimated +115 µs a tick at 128 + 128 rows, paid for rows nobody queries. Not adopted. |
| Dense row indices read as NumPy scalars | About the cost of a dict probe. Dense tables pay only when consumed vectorised. |
| Gather reading the per-stamp runtime cache inline | Tests pass; no measurable gain. |
| Miss-path micro-variants (a per-stamp tuple of active source dicts, the mover branch inlined into `brush_aabb_bounds`, no per-stamp cache store, invalidation by overwrite instead of `pop`) | Within noise: each ingredient is 28–68 ns. |
| Dropping the `_MoverKey` tag | Incorrect: a mover whose `pos` is edited without a stamp would take the plain-box miss. |
| An all-NumPy mover broad phase (second session) | 13 µs a tick in the frame for 8 rows (5.2 µs in `timeit`); the 8-mover player slower than before, the 1-mover player 7 µs slower. Replaced by a scan of the column below 48 rows and a leaner NumPy form above (§11.7). |

## Relevant files

* `engine/brush_placement.py`: the resolver.
  * Stamps (`placement_stamps`, `note_runtime_change`,
    `note_authored_change`) and sources (`register_runtime`,
    `runtime_of`).
  * `resolved_runtime` / `_runtime_cache`, and the `_runtime_cached`
    registry with `_drop_runtime_cached`.
  * `world_bounds`, `world_box(_at)`, `collision_shape(_at)`, `Frame`,
    `BulkBoxes`, `RuntimeOverrides`, `PREVIEW`.
* `engine/constants.py`: `brush_aabb_bounds` (the hit path, Stage 1's
  key), `_brush_aabb_runtime` (the mover miss), the `_MoverKey` and
  `_ShapeKey` tags, `brush_still_box`.
* `engine/mover_table.py`: the dense columns.
  * The tick: `_step`, `_commit`, `_one`.
  * Publication: `_publish_into`, `_publish_row`, `_store_one`, `_adopt`,
    `reset`.
  * `fill_boxes`, `settle_turns`, and the render-table publish.
* `engine/player.py`: the gather in `update`, `_move_with_collision`,
  `_try_step_up`, `_has_headroom`, `_resolve_collision`,
  `_resolve_mesh_capsule`.
* `engine/logic_player.py`: it hands the player **every** mover and door
  in the level each tick.
* The shared mover gather: `engine/logic_collision.py` (collision rows) and
  `engine/physics.py` (`SpatialGrid` cell rows).
* `engine/render_table.py` (`turning`) and `engine/logic_render.py` (the
  cull).
* Tools: `perf_gate.py`, `perf_probes.py`, `bench_collision.py`,
  `bench_compare.py`, `bench_player_movers.py`.
* Tests: `tests/physics/test_aabb_and_physics.py`,
  `tests/brush_model/test_runtime_transform.py` and
  `tests/helpers/runtime.py`. `tests/helpers/geo_reference.py` is frozen:
  do not modify it.

## Commands

Set up a Stage 1 tree beside the checkout:

```bash
git worktree add ../fio-stage1 56ec0e3
```

To try the experiment:

```bash
git apply docs/stage2-handoff/turning-ids-gather.patch
```

The production path (each tree's own mover table, then the player), one
tree, every scenario; or interleaved over several trees:

```bash
taskset -c 3 python tools/bench_player_table.py ../fio-stage1 --kind all
python tools/bench_player_table.py ../fio-head --trace /tmp/a.json && python tools/bench_player_table.py . --trace /tmp/b.json && cmp /tmp/a.json /tmp/b.json
python tools/perf_gate.py --base ../fio-stage1 --head . --rounds 5 --core 3 --probes movers,player,aabb,table --json gate_mpat.json
```

M3's instance uploads, one tree (draws stubbed: the CPU side alone), and
upload bytes, GL calls, Python calls and memory; compare against a worktree
of `bee6c67` (the accepted baseline), interleaved and pinned:

```bash
xvfb-run -a env LIBGL_ALWAYS_SOFTWARE=1 taskset -c 3 python tools/bench_instance_uploads.py BigWorld_streaming_test.json --no-raster --tree .
```

Its "derived arrays" line (M3.1) says how many of the passes' kept arrays a
frame reused and rebuilt, and how much they hold. It runs with PyOpenGL's
error checking off, as `main.py` does; M3's recorded milliseconds had it
on.

M3.1 was compared against a worktree of `1d7a6b0` (M3's head):

```bash
git worktree add --detach ../fio-m31base 1d7a6b00
```

M3's GL tests need a real context (the offscreen Qt platform cannot make
one, and the tests then skip):

```bash
xvfb-run -a env LIBGL_ALWAYS_SOFTWARE=1 python -m pytest -q -p no:cacheprovider tests/visual/test_box_instance_streams.py tests/renderer
```

Parked movers, one tree (HEAD and a tree before `fd5118b` to compare):

```bash
taskset -c 3 python tools/bench_parked_movers.py .
```

Calls and update timing for one tree (`RuntimeOverrides`-driven):

```bash
taskset -c 3 python tools/bench_player_movers.py ../fio-stage1 --calls 40
```

Interleaved runs, 9 per tree; compare the minima and medians of `first`,
`second` and `miss share`:

```bash
for i in 1 2 3 4 5 6 7 8 9; do for t in ../fio-stage1 .; do taskset -c 3 python tools/bench_player_movers.py "$t"; done; done
```

Trajectory equivalence between two Stage 2 trees (for example, HEAD in a
second worktree against the patched tree):

```bash
python tools/bench_player_movers.py ../fio-head --trace /tmp/a.json && python tools/bench_player_movers.py . --trace /tmp/b.json && cmp /tmp/a.json /tmp/b.json
```

Gate subset (under a minute):

```bash
python tools/perf_gate.py --base ../fio-stage1 --head . --rounds 5 --core 3 --probes movers,player,aabb --json gate_mpa.json
```

Full gate, all seven probes (GL; long):

```bash
xvfb-run -a env LIBGL_ALWAYS_SOFTWARE=1 python tools/perf_gate.py --base ../fio-stage1 --head . --rounds 5 --core 3 --json gate.json
```

Test tiers. Headless (~1 min):

```bash
python -m pytest -q -p no:cacheprovider -m "not qt and not gl"
```

Qt (~22 min):

```bash
QT_QPA_PLATFORM=offscreen python -m pytest -q -p no:cacheprovider -m "qt and not gl"
```

Full suite, GL oracles included (~41 min). On `f1556d0`, which has HEAD's code: 7,221 passed, 150 skipped, 5 failed. The five are the known environment failures that fail the same way on Stage 1 (listed in §11.5).

```bash
xvfb-run -a env LIBGL_ALWAYS_SOFTWARE=1 python -m pytest -q -p no:cacheprovider -rf
```

## Ground rules

* Work on `3.1.0.0611_CANARY` only. No pull request unless asked.
* British spelling. No model identifiers in commits, code or docs; end
  commits with the attribution lines the session specifies.
* Never amend or rewrite pushed history; follow-ups go in new commits.
* Do not touch the Android `player/` folder.
* OpenGL 3.3 core only, and ARM-first: Snapdragon 8cx Gen 3 / Adreno 690
  is the target.
* Keep what Stage 2 preserves:
  * true-shape collision;
  * local-frame geometry;
  * the shared cube VBO;
  * the authored/runtime separation: never write runtime movement into
    authored `pos`.
* Keep only changes that show a measured improvement and no regression.
  Do not declare the gate passed unless the evidence supports it. Report
  measured results apart from estimates, and say what is unverified on the
  hardware.
