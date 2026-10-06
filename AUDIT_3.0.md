# Fio 3.0 engineering audit

Living record of the 3.0 stability/performance audit. One entry per finding:
evidence, root cause, fix, tests, measurements, commit, what remains.

## Reference points

| | Ref | Notes |
| --- | --- | --- |
| Baseline | `origin/2.5.10.0310` @ `33f5231` | 2.5.10 + cutscenes; forked from the 3.0 line at `88d455c` |
| Audited | `3.0.0.0510_PreRelease` from `99c1db3` | 1,709 commits after the fork (1,840 after the 2.5.10 snapshot `3a86b45`) |

**Environment.** CPython 3.14.6 (GIL build) with exactly the pinned set:
PyQt5 5.15.11, numpy 2.5.3, Pillow 11.3.0, PyOpenGL 3.1.10, pygame-ce 2.5.8,
PyGLM 2.8.3. Both versions were run on **the same interpreter and the same
pins**, so a difference is a code difference. (2.5.10 runs on 3.14 + these pins
with 3 test failures of its own, below.) OpenGL is Mesa llvmpipe 4.5 core under
Xvfb (`LIBGL_ALWAYS_SOFTWARE=1`): real GL contexts and real shader compiles, but
**no GPU hardware was available to this audit**: see *GPU testing* below.

## How 3.0 differs from 2.5.10 (from the history)

* **What 2.5.x established**: the dense execution boundary (RenderTable,
  EntityTable, MonsterTable, TerrainTable, MoverTable), double-buffered
  lock-free publication through ThreadedGameState, the fixed 60 Hz LogicThread
  with a 30 Hz MonsterAIThread, the I/O system, Big World.
* **3.0's structural change**: the 5,431-line `LogicThread` was split into
  owned runtimes (`logic_camera`, `logic_player`, `logic_movers`,
  `logic_render`, `logic_session`, `logic_combat`, ... 15 in all;
  `logic_thread.py` is now 634 lines of orchestration). Tick order is
  preserved statement for statement (checked against 2.5.10's
  `_tick_play_mode`). The renderer (`renderer_core.py`, `renderer_F.py`)
  is essentially unchanged.
* **3.0 additions**: EffectStore / ProjectileStore SoA tables (`effect_table`,
  `projectile_table`, `soa`), `level_validation` (refuse malformed maps and
  saves), cutscene runtime, terrain CSG and baked texture stamps, sprint, HUD
  styles, removal of compatibility shims (plugin monkey-patch module, legacy
  settings/map keys).
* **Churn**: commit subjects are dominated by *remove* (278), *migrate* (154),
  *move* (135). Tests were migrated in lockstep with the code they test, which
  weakens them as an equivalence check: a test migrated wrongly and never run
  passes no judgement (F-02).

## Method

* `tools/bench_compare.py`: end-to-end frame benchmark through the real editor
  (`MainWindow.load_level_file` -> `enter_play_mode` -> `LogicThread._step_frame`
  / `_publish_frame` -> `QtGameView.paintGL` -> GL), using only public surface
  shared with 2.5.10 so the same script runs on both. The logic loop is halted
  and stepped; the monster AI's own `update` runs on the stepping thread every
  other tick. Reports logic/AI/paint medians and p95, the renderer's own
  per-pass timers, and Python calls per frame.
* Profiling note: on CPython >= 3.12 `cProfile` is built on `sys.monitoring`
  and records **other threads'** events too. Profiles taken with the monster AI
  thread running attribute its waits to the paint. All profiles here are taken
  with the AI stepped on the profiled thread.
* `tests/helpers/session.py` (`FioTestSession`, fixture `fio_session`): the same
  production path as a thin, steppable test session. See *Testing* below.
* Debug Tables (`tools/debug_tables.py`) is used as an instrument, not as the
  oracle of record: assertions check published tables against authored truth
  (`EditorState`), and against pixels or independent counts where it matters.

## Findings

| ID | Sev | Subsystem | Finding | Status | Commit |
| --- | --- | --- | --- | --- | --- |
| F-01 | High | Renderer / terrain | Block-terrain shader never compiled: wrong look, recompiled every frame (terrain pass 3x) | Fixed | `52fa3dd` |
| F-02 | High | Tests / CI | Qt tier never runs on push; 35 failures + 2 errors had accumulated unseen | Fixed | `c7643cc`, `4b60278`, `daa49d0` |
| F-03 | Medium | Tests / CI | Reference shader compiler check silently skipped in CI | Fixed | `52fa3dd` |
| F-04 | Medium | Logic / movers | Mover pass re-planned after every I/O sequence point (+0.3 ms/tick) | Fixed | `9e9dfff` |
| F-05 | Low | Persistence | Opening a map validated it three times (~25 ms each on Big World) | Fixed | `0ec788f` |
| F-06 | Medium | Tests | Editor test host had no LogicThread offscreen; runs with cwd != repo root | Partly fixed | `4b60278` |
| F-07 | Info | Maps | Showcase endboss I/O removed "to repair CI": dead connections | No action | - |
| F-08 | Low | Effects | EntityTable pairs Effect rows with EffectStore rows by position | Verified, test added | `605a242` |
| F-09 | Info | Session start | `PropSession.start()` runs twice per Play | No action | - |
| F-10 | Info | Threading | Would free-threaded (no-GIL) Python help? | Analysis | - |
| F-11 | Medium | Play lifecycle | A failed tick ended Play without the editor's Stop path (world not restored, UI left in Play) | Fixed | `ab3aa5a` |
| F-12 | Low | Editor | `mode_label` never exists; all uses are behind `hasattr` (dead, guarded code) | Open | - |
| F-13 | Low | Content | `door012.png` (Office_Corridor, Portal_Test) exists in no version | Pre-existing | - |
| F-14 | Info | Performance | NumPy 2.5.3 / CPython 3.14 usage | Measured; one fix | `cea2bd2` |
| F-15 | Low | HUD / combat | gun2 `shot_ready` read the cooldown from `LogicThread`, which no longer holds it | Fixed | see below |

### F-01 Block-terrain shader never compiled (High)

* **Evidence.** Terrain_Test_medium (blocks terrace mode), renderer pass timer
  `terrain`: 2.5.10 **15.9 ms**, 3.0 **46.9 ms** per frame; paint median 21 ->
  58 ms. Log, every frame: `error: 'PaintCoords' undeclared` compiling
  `terrain_mesh.vert`. `glslangValidator` rejects the same source.
* **Root cause.** `4344998` ("Bake texture stamps into terrain material data")
  added `PaintCoords = aTexCoord;` to `terrain_mesh.vert` without the
  `out vec2 PaintCoords;` declaration. The block program never linked, so
  `update_and_render` fell back to the heightfield (Voxel Blocks rendered
  smooth) and `_ensure_block_program()` retried the compile on every frame.
* **Fix.** Declare the output. A genuine terrain compile failure is now
  recorded and reported once; the no-context-yet case still retries.
* **Tests.** `tests/visual/test_terrain_appearance_render.py`: blocks mode
  draws through the block program (program linked, drawn triangles equal the
  block VBO vertex counts, pixels differ from the heightfield render); a
  failing program compiles once across frames. Both fail before the fix. The
  existing `test_every_look_renders_and_differs` passed throughout, because
  the fallback also renders *something* different.
* **Benchmark.** Terrain pass 46.9 -> **15.2 ms** (2.5.10: 14.2); paint median
  58 -> **18.6 ms** (2.5.10: 18.1).

### F-02 Qt tier never runs on push (High)

* **Evidence.** `.github/workflows/tests.yml`: push/PR runs only `no_gpu`
  (`-m "not qt and not gl"`, 2-minute timeout); the Qt suite is
  `workflow_dispatch` only. On the audited head: **35 failed, 2 errors**
  (offscreen). 2.5.10 on the same interpreter and pins: 3 failed.
* **Root cause.** Tests migrated onto real runtimes on 10-04..10-06 were never
  run. None of the 37 was an engine fault:
  * 25: the player is created by Play (`QtGameView.toggle_play_mode`, as in
    2.5.10); tests wrote to `logic.player_runtime.player` while it was None;
    a helper changed to return `(ai, logic)` with callers unchanged; a
    `ThreadedGameState.sounds` that never existed; a LevelChanger expectation
    contradicting its `usable=False` default.
  * 4: render-dirty journal tests moved to the real `EditorState` kept 2.5.10's
    hand-built epoch-0 expectations (a new state starts with a whole-world
    invalidation pending).
  * 3 + 2: tests that build a real renderer, not marked `gl`, so offscreen they
    could only fail or error.
  * 1: a hand-written `_Painter` fake that drifted from `QPainter`
    (production now draws arrowheads with `drawPolygon`). Replaced by a real
    QPainter on a QImage that records lines.
  * 2: see F-06.
* **Fix.** Tests corrected to the production contract (`c7643cc`, `4b60278`).
  CI runs the Qt tier offscreen on every push (`daa49d0`): 4,327 passed,
  0 failed in 9 minutes here.

### F-03 Reference shader compiler check skipped in CI (Medium)

* **Evidence.** `tests/renderer/test_shader_portability.py` compiles every
  shader with Khronos `glslangValidator` (desktop GLSL 3.30 and the GLES 3.00
  translation) and would have caught F-01, but skips when glslang is absent,
  and no CI job installs it.
* **Fix.** The OpenGL job installs `glslang-tools` and runs the portability
  suite with `FIO_REQUIRE_GLSLANG=1`, which turns a missing compiler into an
  error instead of a skip.

### F-04 Mover pass re-planned after every sequence point (Medium)

* **Evidence.** _SHOWCASE content made identical for both versions; AI stepped
  deterministically. Logic tick median 2.40-2.64 ms (2.5.10) vs 2.83-3.08 ms
  (3.0): +12-20% over three runs. Python calls per frame were equal (720 vs
  746); the self-time diff put `mover_table._step` at 3 calls/frame against 2
  (+210 us) with `_plan`/`_eased` likewise.
* **Root cause.** `9d7830a` ("Re-plan mover suffix after every sequence
  point") made `MoverTable._walk` re-plan the remaining rows unconditionally,
  on the theory that an input could change a later row without reaching the
  dirty set. No test or case accompanied it.
* **Investigation.** With 2.5.10's conditional re-plan restored, the
  equivalence oracle (the pre-table mover loop kept verbatim, compared bit for
  bit every tick, with completion outputs wired to FollowPath / StopPath /
  SetPosition / ... on earlier and later movers) passed 200 seeds x 700 ticks.
  Instrumented to compare against a fresh plan at every skipped re-plan:
  **115,531 skipped, 0 different**. Inputs reach the dirty set through
  `table.sync()` (change journal) or the state views.
* **Fix.** Conditional re-plan restored, with the evidence in the comment.
* **Tests.** `test_a_sequence_point_that_changes_no_later_row_does_not_replan_the_pass`
  (perf; also checks equivalence with the reference loop every tick). Fails
  before the fix.
* **Benchmark.** Showcase logic +12-20% -> **+5-11%** over 2.5.10. The
  remainder is mostly F-08's per-frame copy (~0.14 ms).

### F-05 Map validated three times on open (Low)

* **Evidence.** `validate_level` (new in 3.0) runs in `_load_level`,
  `_apply_level_data` and `EditorState.load_from_data`. One run on
  BigWorld_streaming_test (3,604 brushes): **24.6 ms**, as much as parsing
  its JSON (25.7 ms).
* **Fix.** Dropped the `_apply_level_data` call: its next statement is
  `load_from_data`, which validates before touching the scene. The other two
  each guard something (the pre-check runs before a level change ends Play).
* **Tests.** `test_opening_a_map_validates_it_once_before_play_and_once_on_parse`
  (perf, fails before), `test_applying_a_malformed_map_leaves_the_open_scene_alone`.
* **Note.** The earlier "map load 3x slower" reading was the benchmark's own
  fault (loading while the free-running logic thread competed); cold loads
  measured alone are equal (22 vs 22 ms on MonsterTest).

### F-06 Editor test host differs from the app (Medium)

* **Evidence.** Under offscreen Qt there is no GL context, so
  `QtGameView.initializeGL`, which is where the editor starts its LogicThread,
  never runs: every `main_window` test ran an editor with **no logic thread**.
  The cutscene preview's camera restore (`view_3d.logic_thread.camera...`)
  raised. (2.5.10 hid the same situation behind
  `getattr(view_3d, "logic_thread", None)`; 3.0 removed the fallback, which
  is right.)
* **Fix.** The fixture starts the logic thread exactly as `initializeGL` does.
  1,272 `main_window` tests pass with it, none newly failing.
* **Remaining.** The fixture `chdir`s to a temp directory so the editor's
  `settings.ini` writes stay out of the repo, but the engine resolves
  `assets/` against the working directory (`QtGameView` sounds, fonts,
  textures). So those tests run without assets. `FioTestSession` runs from the
  repo root and relies on the autouse `settings.ini` restore instead. The
  cleaner fix is in the engine: resolve assets from `MainWindow.root_dir`.

### F-07 Showcase endboss I/O removed to repair CI (Info, no action)

`3daab69` "Repair showcase CI regressions" deleted the PlayerStart's
`OnPlayerSpawn -> endboss Disable/Hide` connections. The same change was
reverted on 2.5.10.0310 (`3416612`). Checked: the targets name a fixed UUID
that matches no entity in either version; the endboss is created at runtime by
`endboss_spawner` with an auto-generated name. They were dead connections
(`test_every_shipped_connection_resolves` fails on 2.5.10 for this reason), so
removing them does not change gameplay.

### F-08 Effect rows paired by position (Low)

`EntityTable._advance_effects` reads `effect_store.<column>[:count]` and
writes it to `effect_ls` rows, pairing by position although an
`effect_store_index` column exists. The store is rebuilt in world order on
every reconcile, so the pairing holds. `tests/e2e/test_effect_rows.py` checks
every published Effect row against its own authored type through a mid-play
removal and insertion, and fails on a deliberately reversed pairing.
**Remaining:** `EffectStore.sync_authored` can append a row out of world order
(not reachable through the paths tested). The five-column copy costs ~0.14
ms/frame on the showcase, the bulk of the logic gap left after F-04.

### F-09 PropSession started twice per Play (Info, no action)

`LogicSession._apply_play_mode_unlocked` calls `prop_runtime.start()` twice.
In 2.5.10 the first call was skipped because `_props` was None after the
previous stop; 3.0 keeps one PropSession for the logic thread's life. The
second `rebuild()` finds every Prop already adopted: O(props) once per Play
start, no behavioural effect, and `enter_play_ms` matches 2.5.10. Not worth
touching working session-start code for.

### F-10 Would free-threaded Python help? (Analysis)

Not for 3.0.
1. PyQt5 5.15.11 / sip have no free-threaded (`cp314t`) builds: the pinned
   stack cannot run GIL-free (an extension without free-threading support
   either fails to install or re-enables the GIL).
2. The threading design assumes the GIL: frame preparation freezes the entity
   list with one atomic copy and takes no lock against the AI or the editor;
   publication is lock-free across Python objects and NumPy columns. Removing
   the GIL would need an audit of every cross-thread access.
3. Measured work does not contend much: the logic tick is 2-3 ms of a 16.7 ms
   frame; the monster AI already runs under a lock shared with the logic
   thread; paint time is mostly the GL driver, which releases the GIL. The
   GIL's real cost is hand-off latency (up to the 5 ms switch interval while
   another thread runs pure Python), which shows in p95, not throughput.
4. The free-threaded build is slower per thread.

`python_runtime.py` refusing free-threaded builds is correct.

### F-11 Failed tick bypassed the editor's Stop (Medium)

New in 3.0 (`45018ea`): a raising tick marshals teardown to the GUI thread,
which called `QtGameView.toggle_play_mode` directly and skipped
`MainWindow._exit_play_mode`. With "restore the world when leaving Play" on,
play-time changes stayed in the authored world (and Ctrl+S wrote them to the
map); the Play button kept saying Stop. The handler now takes the Stop path.
`tests/e2e/test_tick_fault.py` (fails before: the button still read Stop).

### F-12 `mode_label` is dead, guarded code (Low, open)

`MainWindow` never creates `mode_label`; `enter_play_mode`/`_exit_play_mode`
style it behind `hasattr(self, 'mode_label')`. Harmless, but it is the
pattern of guarded legacy interface this audit is meant to remove.

### F-15 gun2 published as ready during its cooldown (Low)

Found while profiling `prepare_render_state`. It computed `shot_ready` from
`getattr(logic, "_last_player_shot_time", -inf)`; the split moved that field
to the combat runtime, so the getattr always fell back and gun2 was published
as ready all through its one-second cooldown. The published flag gates Qt's
click queue (the logic thread still refused the shot, so no extra shot ever
fired). Reads `combat_runtime._last_player_shot_time` now.
`test_gun2_is_published_as_not_ready_while_it_cools_down` (e2e; fails
before).

### F-14 NumPy 2.5.3 / CPython 3.14 (Info, measured)

* **Semantics.** No `copy=False` constructors (NumPy 2 raises when a copy is
  needed), no removed or deprecated APIs. 2,100 engine/renderer/logic/AI
  tests pass with NumPy Deprecation/Future/Runtime warnings as errors.
* **Where the time is.** Per-phase self-time on four maps: NumPy is 2-14%,
  Fio's own Python 62-90% (that includes GL driver time; ctypes calls are
  charged to their caller). Wrapped per call site, all NumPy calls together
  are ~1.5-2.5 ms per frame across ~170 sites; no single site is above 0.3 ms.
* **Fixed.** `TerrainTable.ensure_many`: `np.isin(..., assume_unique=True)`;
  both inputs are distinct by construction. 54-80 us -> 25 us per frame.
* **Measured, not yet changed.**
  * PyOpenGL's NumPy handler reads `ndarray.__array_interface__` per array
    argument (a dict built per access, ~5 us). Per-brush uniform uploads in
    the textured/lit/glow passes pass row views: ~35 calls per showcase frame.
    Raw entry points with pre-built pointers: 6.6 -> 1.5 us per call, same
    uniforms read back. ~0.18 ms per showcase frame; not used on Big World
    (instanced).
  * `RenderTable.model_matrices` recomputes per pass (6x per frame, ~50 us
    each) for the same rows; a per-frame cache could save ~0.25 ms but must
    follow movers, which rewrite centres every frame.
  * Terrain chunk generation (`_grad`, `fbm_batch`, `build_block_mesh`'s
    `np.stack`) is the heaviest NumPy work, but it is amortised: only chunks
    being (re)built pay it.
* **Limit of this environment.** On llvmpipe the main thread also runs vertex
  processing, so neither wall-clock nor main-thread CPU separates Fio's
  submission cost from "GPU" work; Big World's 140 ms paint is mostly
  heightfield vertex shading. GPU-bound cost needs hardware to measure.

## Benchmarks (llvmpipe, 640x360, 300-400 frames, medians)

| Map | Metric | 2.5.10 | 3.0 before | 3.0 now |
| --- | --- | --- | --- | --- |
| Terrain_Test_medium | paint ms | 18.1-21.0 | 58.0 | 18.6-19.1 |
| Terrain_Test_medium | terrain pass ms | 14.2-15.9 | 46.9 | 15.2-15.6 |
| _SHOWCASE (same content) | logic ms | 2.40-2.64 | 2.83-3.08 | 2.56-2.66 |
| _SHOWCASE | AI ms (per update) | 0.81-0.93 | 0.81-0.88 | 0.82-0.86 |
| MonsterTest | logic ms | 2.61-2.85 | 2.66-2.86 | - |
| MonsterTest | AI ms | 4.50-4.74 | 4.59-4.70 | - |
| Portal_Test / Office_Corridor | paint, logic | parity | parity | - |
| BigWorld_streaming_test | logic ms | 3.16-3.18 | - | 3.09-3.21 |
| BigWorld_streaming_test | paint ms | 143-145 | - | 141-148 |
| BigWorld_streaming_test | Python calls / frame (logic, paint) | 2,168 / 6,009 | 2,187 / 6,138 | - |
| BigWorld_streaming_test | load ms | 297 | - | 242 |

Reproduce: `LIBGL_ALWAYS_SOFTWARE=1 xvfb-run -a python tools/bench_compare.py
--frames 400 maps/X.json` in each checkout.

## Performance pass

Goal: 3.0 at least as fast as 2.5.10 everywhere, by removing Fio-owned Python
work from hot paths, within the 3.0 architecture. One entry per change:
baseline, 2.5.10 comparison, change, tests, before/after, commit.

### Method

* **The benchmark now runs with the shipped app's PyOpenGL settings**
  (`5eae5fe`). `main.py` sets `PYOPENGL_ERROR_CHECKING=0`; `bench_compare.py`
  does not come through `main.py`, so every number in *Benchmarks* above
  includes a Python `glGetError` check after each GL call (530-940 per frame)
  that the app never runs. With the checks off, Showcase paint drops from ~40
  to ~31 ms here. `FIO_GL_DEBUG=1` keeps the checks (then
  `glCheckError`'s callers count GL calls per function, which is how the
  shadow-sampler item below was found).
* Timings: 3 interleaved rounds per version (3.0, 2.5.10, and a "before"
  worktree for each change), 300 frames, medians; ranges in brackets.
  Profiles: one round each, `--profile --dump`, compared per function.
* **Wall clock cannot resolve paint changes below ~1-2 ms** on llvmpipe (the
  driver's shading runs on the main thread too). Paint-side changes are
  therefore measured by the profile's cumulative time for the changed
  function, which is stable to a few percent; wall clock is reported
  alongside, and logic (no GL) is measured by wall clock directly.
* GL calls through ctypes are charged to their Python caller: a renderer
  function's self time is its Python plus its GL calls. Items below say which
  dominates.

### Baseline (production settings, ms, medians of 3 rounds)

| Map | Metric | 2.5.10 | 3.0 |
| --- | --- | --- | --- |
| _SHOWCASE | logic | 2.34 [2.29-2.61] | 2.45 [2.44-2.45] |
| _SHOWCASE | paint (main-thread CPU) | 20.5 [20.3-24.6] | 22.0 [20.3-22.2] |
| MonsterTest | logic / AI | 2.56 / 4.51 | 2.49 / 4.42 |
| MonsterTest | paint (CPU) | 11.2 | 10.8 |
| Terrain_Test_medium | logic / paint (CPU) | 1.66 / 12.6 | 1.55 / 12.1 |
| Portal_Test | logic | 1.95 [1.87-1.99] | 2.04 [1.94-2.08] |
| Portal_Test | paint (CPU) | 23.1 [22.3-23.8] | 24.7 [23.6-24.7] |
| BigWorld_streaming_test | logic / AI | 2.92 / 1.04 | 2.94 / 1.04 |
| BigWorld_streaming_test | paint (CPU) | 125.3 | 125.9 |

Per function, 3.0's hot code is 2.5.10's moved into the split runtimes: self
times match within noise except `_bind_shadow_maps` (P-01) and
`EntityTable._advance_effects` (+0.13 ms, F-08). So parity is close, and the
gains beyond it are in Python work both versions share.

### Ranked hot paths (3.0, production settings)

Profiled self time, ms/frame (the profiler inflates call-heavy Python ~2-3x;
the order is what matters). "Shared" = same cost in 2.5.10.

| # | Path | Evidence | Kind | Status |
| --- | --- | --- | --- | --- |
| 1 | `MoverTable._walk` plans empty groups | `mover_table` 0.75-1.05 ms/frame on **every** map, Terrain_Test_medium has no brushes at all; ~30 NumPy calls per empty group per tick | Python, shared | P-02, fixed |
| 2 | `_bind_shadow_maps` re-sends sampler units | 136-145 us/call vs 80 in 2.5.10; 4-11 calls/frame; 3.0's 8-sampler shaders doubled its GL calls | GL calls + Python, **3.0 regression** | P-01, fixed |
| 3 | `prepare_render_state` | 0.25-0.75 ms self every frame (BigWorld highest) | Python, shared | open |
| 4 | Player hitscan (`_handle_shooting`) | per shot: a Python loop over every collision brush (dict gets, `is_water_brush`, two `glm.vec3`, ray/AABB): ~10 ms per shot on BigWorld (3,604 brushes) | Python, shared; p95 spike | open |
| 5 | Monster AI `_chase` | ~2.8 ms per AI update on MonsterTest; batched already, per-row state write-back and flag loops remain | NumPy + Python, shared | open |
| 6 | Frustum (`extract_frustum_planes`, `aabb_in_frustum_bounds`) | 0.13-0.18 ms each, every frame | Python, shared | open |
| 7 | `_advance_effects` five-column copy | 0.41 ms on Showcase (F-08) | NumPy, 3.0 | open |
| 8 | Terrain `chunk_uniforms` (+ its lambda) | 68 + 342 calls/frame, ~2.6 ms on BigWorld | Python, shared | open |
| 9 | Player collision helpers | `is_water_brush` 19-21, `brush_aabb_bounds` 20-56, `_has_headroom` 7-37 calls/frame | Python, shared | open |
| 10 | `RenderTable.model_matrices` per pass | 1-5x per frame for the same rows (F-14) | NumPy, shared | open |
| 11 | Brush-slot classification | `classify_slots` + `_classify_brush_slots` 1.7 ms on Portal (4.5 calls/frame, once per view) | NumPy + Python, shared | open |
| - | BigWorld cell-debug overlay | `_paint_minimap` 9.4 ms + 293 `fillRect`/frame, only with the map's authored `show_cell_debug` on | Debug-only | not ranked |
| - | `draw_heightfield_slots`, `draw_block_slots`, glass capture, `_point_brush_instances_at` | self time is GL driver work (llvmpipe vertex shading, ~190 raw attrib-pointer calls) | GL | not Python |

### P-01 Shadow sampler units re-sent on every pass (3.0 regression)

* **Baseline.** With `FIO_GL_DEBUG=1`, 3.0 makes more GL calls than 2.5.10 on
  every map (Portal +170/frame), all from `_bind_shadow_maps` (266 vs 132 on
  Portal). The function is byte-identical; 3.0 raised the shaders'
  `MAX_SHADOW_LIGHTS` from 4 to 8 (the renderer already allocated 8 cube
  maps, 2.5.10 sampled 4), so each lit pass now does 8 x (f-string, uniform
  lookup, `glActiveTexture`, `glBindTexture`, `glUniform1i`).
* **Change.** A sampler's unit is program state and holds until the program
  is deleted; every compile makes a new `UniformCache`. The units are now
  assigned on a program's first lit pass, and the live slots kept on its
  `UniformCache`; later passes only bind the cube maps (texture units are
  shared context state: terrain binds its own placeholder there).
* **Tests.** `test_shadow_samplers_are_assigned_once_and_keep_their_units`
  (`gl`): a later frame re-sends no shadow sampler unit, every program's
  `shadowMaps[i]` reads unit base+i from GL itself, and the frame is
  pixel-identical. Fails before (16 re-sends per frame). GL tier 162 passed.
* **Result.** Per call 136-145 -> **80-82 us** (2.5.10: 79-83 us with half
  the samplers). Per frame: Portal 1.44 -> 0.87 ms, Showcase 0.78 -> 0.46,
  MonsterTest 0.58 -> 0.32 (profiled). Paint wall clock moved within noise.
* **Commit.** `96de537`.
* **Noted, not changed.** `render_terrain` uploads lights through
  `uniforms['terrain']` (a program the renderer compiles and never draws
  with) while `terrain.shader_program` is bound; terrain then assigns its own
  sampler units, so drawing is right. Same in 2.5.10.

### P-02 Empty mover and door groups planned every tick

* **Baseline.** `mover_table` self time 0.75-1.05 ms/frame (profiled) on every
  map, with or without movers; Terrain_Test_medium has no brushes at all.
  Same in 2.5.10. `MoverTable._walk` has no empty-group exit: each of the two
  groups (linear movers, doors) built a full plan over zero rows every tick
  (`arange`, masks, `_step`'s ~25 array ops, `concatenate`, `unique`,
  `searchsorted`, a no-op `_commit`). Most maps have no doors; many have no
  movers. (`publish` already returned early when both groups were empty.)
* **Change.** `_walk` returns after clearing the dirty set when its group has
  no rows; with none, the pass it skips commits nothing and fires nothing.
* **Tests.** `test_a_group_with_no_rows_plans_nothing` (movers only, and
  neither): the empty group never plans, the existing group plans once a tick
  and still matches the reference mover loop every tick. Fails before (the
  empty group planned 10 of 10 ticks). Mover equivalence suite: 22 passed.
* **Result.** Logic tick median (ms; 3 interleaved rounds):

  | Map | 2.5.10 | before | after |
  | --- | --- | --- | --- |
  | Terrain_Test_medium | 1.77 | 1.75 | **0.95** (-46%) |
  | Portal_Test | 2.14 | 2.13 | **1.43** (-33%) |
  | MonsterTest | 2.59 | 3.07 | **2.52** (-18%) |
  | _SHOWCASE | 2.55 | 2.63 | **2.24** (-15%) |
  | BigWorld_streaming_test | 3.03 | 3.11 | **2.77** (-11%) |

  Python calls per logic tick -50 to -100; `mover_table` self time on maps
  with neither group 0.83-0.85 -> 0.05 ms. The logic tick is now at or below
  2.5.10's on every map. (BigWorld's p95, ~10.8 ms in both versions, is the
  hitscan, ranked #4.)

## Stability

| Check | 2.5.10 | 3.0 |
| --- | --- | --- |
| Non-GL suite, offscreen, same interpreter + pins | 3 failed / 3,960 passed | before: 35 failed + 2 errors / 4,255 passed; now: 0 failed / 4,327 passed |
| GL tier under llvmpipe | - | before: 154 passed; now: 161 passed |
| 30 Play/Stop cycles, MonsterTest: retained traced memory | 446 KiB | 420 KiB |
| 30 Play/Stop cycles: per-cycle time | 149 ms | 144 ms |
| Threads alive after the cycles | MainThread only | MainThread only |
| Pipeline soak (logic + AI + churn + reader threads) | - | 3 x 20 s, every frame consistent, no thread raised |

The retained memory in both versions is bounded buffers filling to their caps
(the 50-deep undo deque, the 1,000-line console log), not a per-session leak.

## Testing

* `FioTestSession` (`tests/helpers/session.py`, fixture `fio_session`): real
  map, real `MainWindow`, real LogicThread and runtimes, real publication and
  tables, real `paintGL`. Controlled: tick stepping, AI scheduling, input,
  display. ~170 lines of orchestration, no fakes.
* `tests/e2e/` (session-driven, `qt` + `integration`):
  * `test_input_to_render_state.py`: keys/mouse -> LogicPlayer / MonsterAI ->
    published frame (walk direction against the published camera, look
    sensitivity, sprint scale, AI-moved monster rows).
  * `test_edit_undo_save_reload.py`: Delete key -> Ctrl+Z -> Ctrl+Y ->
    `save_level` -> `load_level_file`; the published RenderTable checked
    against the authored brushes, and those against the file, at every step.
  * `test_effect_rows.py`: F-08.
  * `test_play_cycles.py`: 30 Play/Stop sessions; threads, memory, identical
    first frame.
* Tiers as in `tests/README.md`. CI now runs the Qt tier offscreen on every
  push (F-02) and requires the reference shader compiler in the GL job (F-03).

## GPU testing

No GPU hardware was available in the audit environment. Everything marked `gl`
ran against Mesa llvmpipe: real contexts, real compiles and real draws, but not
a hardware driver. Driver-specific failures (the Intel `noise2` overload in
`test_shader_portability.py` is one) are only partly covered by the glslang
reference compiler now required in CI. A self-hosted GPU runner running
`pytest -m gl` and `tools/bench_compare.py` remains to be set up.

## Open work

* F-06: resolve engine asset paths from the root directory rather than cwd.
* F-08 / F-09 as above.
* Suite wall time: the 3.0 suite takes ~5x 2.5.10's for ~7% more tests.
* Further end-to-end coverage: editor action -> journal/undo -> save -> reload;
  input -> player/AI -> render state; GL paint through the session.
