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
| F-08 | Low | Effects | EntityTable pairs Effect rows with EffectStore rows by position | Fixed: paired by identity, store order defined, copy only on change | `605a242`, `571de5e` |
| F-09 | Info | Session start | `PropSession.start()` runs twice per Play | Fixed | `c7b930d` |
| F-10 | Info | Threading | Would free-threaded (no-GIL) Python help? | Analysis | - |
| F-11 | Medium | Play lifecycle | A failed tick ended Play without the editor's Stop path (world not restored, UI left in Play) | Fixed | `ab3aa5a` |
| F-12 | Low | Editor | `mode_label` never exists; all uses are behind `hasattr` (dead, guarded code) | Open | - |
| F-13 | Low | Content | `door012.png` (Office_Corridor, Portal_Test) exists in no version | Pre-existing | - |
| F-14 | Info | Performance | NumPy 2.5.3 / CPython 3.14 usage | Measured; two fixes (second pass) | `cea2bd2`, `576b288` |
| F-15 | Low | HUD / combat | gun2 `shot_ready` read the cooldown from `LogicThread`, which no longer holds it | Fixed | `f60b382` |
| F-16 | Medium | Effects | A looping FIRE in Play animated on `perf_counter`'s epoch (the machine's uptime) as a float32 | Fixed | `65213d0` |

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

### F-15 gun2 published as ready during its cooldown (Low)

Found while profiling `prepare_render_state`. It computed `shot_ready` from
`getattr(logic, "_last_player_shot_time", -inf)`; the split moved that field
to the combat runtime, so the getattr always fell back and gun2 was published
as ready all through its one-second cooldown. The published flag gates Qt's
click queue (the logic thread still refused the shot, so no extra shot ever
fired). Reads `combat_runtime._last_player_shot_time` now.
`test_gun2_is_published_as_not_ready_while_it_cools_down` (e2e; fails
before).

### F-16 Looping Effects in Play animated on the machine's uptime (Medium)

Found while profiling `_advance_effects` (F-08's path). With an EffectStore
(every Play session), 3.0 computed `elapsed = now - spawn_time` from the
store's raw column, where a looping FIRE has no playback start (0.0), while
the table's published origin substitutes the shared clock origin; 2.5.10
measured from that origin. So a looping fire's `effect_elapsed` (the shader's
animation clock and the light flicker's input) was `perf_counter()` itself:
time since boot, held as float32. Its resolution degrades with uptime (~8 ms
after a day, ~60 ms after a week), so the flame animation and flicker
coarsen the longer the machine has been up. Now measured from the origin it
publishes. `test_a_looping_fire_in_play_runs_on_the_origin_it_publishes`
(fails before: elapsed 5903 s, this container's uptime). Not a measurable
speed change (`_effect_flicker` 0.042 -> 0.040 ms/frame).

## Benchmarks (llvmpipe, 640x360, 300-400 frames, medians)

These are the audit's numbers, taken with PyOpenGL error checking on and
`tracemalloc` running (see *Performance pass / Method*): use them to compare
the two versions with each other, not as absolute frame times.

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

Two faults in the harness inflated every timing above, in both versions:

* **PyOpenGL error checking** (`5eae5fe`). `main.py` sets
  `PYOPENGL_ERROR_CHECKING=0`; `bench_compare.py` does not come through
  `main.py`, so it ran a Python `glGetError` after each GL call (530-940 per
  frame) that the app never runs. Now applied as in `main.py`;
  `FIO_GL_DEBUG=1` keeps the checks (each then counts one GL call, and
  `glCheckError`'s callers give GL calls per function: how P-01 was found).
* **`tracemalloc` during the timed frames** (`c9a04bd`). It hooks every
  allocation, so NumPy-heavy code read several times slower than it runs: the
  monster AI on MonsterTest 5.1 ms per update traced, 1.1 ms not; the logic
  tick 2.5 vs 1.1 ms; Showcase paint 31 vs 17 ms. Memory is now traced only
  with `--memory`. Profiles taken before this fix (the P-01..P-03 entries'
  profiled figures) were inflated the same way; their before/after pairs were
  taken together and compare like with like, but the absolute values are high.
  A cost judged from them alone was re-measured untraced (`prepare_render_state`,
  the monster AI, the hitscan below).

Otherwise:

* Timings: 3 interleaved rounds per version, 300 frames, medians; ranges in
  brackets. Versions: 2.5.10, 3.0 at the start of this pass (`215269c`: the
  audited head plus the CI fix, no engine change), and the change under test.
  Profiles: one round each, `--profile --dump`, compared per function.
* Wall clock cannot resolve paint changes below ~1 ms on llvmpipe (the driver's
  shading runs on the main thread too), so paint-side changes are measured by
  the changed function's profiled time and its GL/Python call counts; logic
  (no GL) is measured by wall clock directly. Real time per line comes from a
  line tracer scoped to one function, over the same stepped frames.
* GL calls through ctypes are charged to their Python caller: a renderer
  function's self time is its Python plus its GL calls.

### Result (clean harness, ms, medians of 3 interleaved rounds)

| Map | Metric | 2.5.10 | 3.0 at start | 3.0 now |
| --- | --- | --- | --- | --- |
| _SHOWCASE | logic | 1.00 [0.99-1.06] | 1.09 [1.04-1.15] | **0.92** [0.87-1.03] |
| _SHOWCASE | AI per update | 0.176 | 0.188 | 0.186 |
| _SHOWCASE | paint CPU / wall | 8.72 / 16.8 | 8.94 / 17.5 | 8.97 / 17.4 |
| MonsterTest | logic | 1.06 [1.02-1.13] | 1.15 [1.07-1.20] | **0.96** [0.94-1.05] |
| MonsterTest | AI per update | 0.958 | 1.018 | 0.987 |
| MonsterTest | paint CPU / wall | 5.20 / 11.7 | 5.54 / 12.5 | 5.21 / 11.5 |
| Terrain_Test_medium | logic | 0.72 [0.71-0.77] | 0.78 [0.71-0.78] | **0.34** [0.33-0.37] |
| Terrain_Test_medium | paint CPU / wall | 8.69 / 14.1 | 9.19 / 14.9 | 8.31 / 13.5 |
| Portal_Test | logic | 0.87 [0.81-0.93] | 0.88 [0.79-0.88] | **0.56** [0.49-0.64] |
| Portal_Test | paint CPU / wall | 8.39 / 13.8 | 8.85 / 14.5 | 8.47 / 14.3 |
| BigWorld_streaming_test | logic (p95) | 1.32 (2.48) | 1.26 (2.38) | **1.19** (2.23) |
| BigWorld_streaming_test | paint CPU / wall | 106.2 / 114.8 | 105.4 / 112.7 | 104.5 / 112.3 |

Python calls per frame (logic / paint), 2.5.10 -> now: Showcase 718/5,173 ->
662/5,027; MonsterTest 1,391/2,823 -> 1,345/2,683; Terrain 447/1,434 ->
338/1,342; Portal 612/5,447 -> 516/5,419; BigWorld 2,144/5,476 ->
2,134/4,900. Fewer than 2.5.10 on every map, in both phases.

* **Logic: faster than 2.5.10 on every map** (8-52%), almost all from P-02.
* **Monster AI: at parity.** Same functions, same call counts; per-function
  differences are 1-13 us (noise). Nothing in it is 3.0-specific.
* **Paint: at parity** within the run-to-run ranges, faster on Terrain and
  BigWorld. Showcase's +0.25 ms CPU (ranges overlap) has no Python-call
  difference: it is `_capture_glass_scene`'s GL readback. What 3.0 adds per
  frame in GL calls is the 8 shadow samplers (+4 binds per lit pass) and the
  terrain paint/sampler set-up (+13 per frame); their shader cost needs a GPU
  to size.

### Ranked hot paths (3.0, production settings)

Ranked from the first, traced profiles (self time, ms/frame); the evidence
column gives the clean figure where re-measurement changed the picture.
"Shared" = same cost in 2.5.10.

| # | Path | Evidence | Kind | Status |
| --- | --- | --- | --- | --- |
| 1 | `MoverTable._walk` plans empty groups | `mover_table` 0.75-1.05 ms/frame on **every** map, Terrain_Test_medium has no brushes at all; ~30 NumPy calls per empty group per tick | Python, shared | P-02, fixed |
| 2 | `_bind_shadow_maps` re-sends sampler units | 136-145 us/call vs 80 in 2.5.10; 4-11 calls/frame; 3.0's 8-sampler shaders doubled its GL calls | GL calls + Python, **3.0 regression** | P-01, fixed |
| 3 | `prepare_render_state` | 0.25-0.75 ms self every frame (BigWorld highest) | Python, shared | measured: ~0.35 ms real, all dense NumPy; no change |
| 4 | Player hitscan (`_handle_shooting`) | per shot: a Python loop over every collision brush (dict gets, `is_water_brush`, two `glm.vec3`, ray/AABB). Untraced: **1.37 ms per shot** on BigWorld (3,604 brushes), not the ~10 ms the traced profile showed | Python, shared | fixed: see *Open after this pass* |
| 5 | Monster AI `_chase` | traced ~2.8 ms per update; **untraced 0.75 ms**, the whole update 1.1 ms; batched already | NumPy, shared | measured: tracemalloc artefact; at parity |
| 6 | Frustum (`extract_frustum_planes`, `aabb_in_frustum_bounds`) | 0.13-0.18 ms each, every frame | Python, shared | small clean; see *Open after this pass* |
| 7 | `_advance_effects` five-column copy | 0.41 ms traced, 0.12 ms clean on Showcase (F-08); found F-16 on the way | NumPy, 3.0 | F-16 fixed; copy kept |
| 8 | Terrain `chunk_uniforms` (+ its lambda), per-chunk paint uniforms | 68 + 342 calls/frame, ~2.6 ms on BigWorld; 3.0's paint adds a coord tuple, a dict lookup and `glUniform1i` per chunk on maps with no paint | Python + GL, shared + **3.0 regression** | P-03, fixed |
| 9 | Player collision helpers | `is_water_brush` 19-21, `brush_aabb_bounds` 20-56, `_has_headroom` 7-37 calls/frame | Python, shared | small clean; see *Open after this pass* |
| 10 | `RenderTable.model_matrices` per pass | 1-5x per frame over each pass's own slots, 0.1-0.22 ms/frame clean; already one vectorised build per pass | NumPy, shared | no change: one build per frame needs per-pass buffers (a frame cache) for ~0.1 ms |
| 11 | Brush-slot classification | `classify_slots` + `_classify_brush_slots` 1.7 ms on Portal (4.5 calls/frame, once per view) | NumPy + Python, shared | small clean; see *Open after this pass* |
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
* **Commit.** `d02276e`.
* **Result.** Logic tick median (ms; 3 interleaved rounds):

  | Map | 2.5.10 | before | after |
  | --- | --- | --- | --- |
  | Terrain_Test_medium | 1.77 | 1.75 | **0.95** (-46%) |
  | Portal_Test | 2.14 | 2.13 | **1.43** (-33%) |
  | MonsterTest | 2.59 | 3.07 | **2.52** (-18%) |
  | _SHOWCASE | 2.55 | 2.63 | **2.24** (-15%) |
  | BigWorld_streaming_test | 3.03 | 3.11 | **2.77** (-11%) |

  (Measured under `tracemalloc`; untraced, in *Result*: Terrain 0.78 ->
  0.34, Portal 0.88 -> 0.56, MonsterTest 1.15 -> 0.96, Showcase 1.09 -> 0.92
  ms.) Python calls per logic tick -50 to -100; `mover_table` self time on
  maps with neither group 0.83-0.85 -> 0.05 ms. The logic tick is now at or below
  2.5.10's on every map. (BigWorld's p95, ~10.8 ms in both versions, is the
  hitscan, ranked #4.)

### P-03 Terrain chunk uniforms one chunk at a time; paint state per chunk

* **Baseline.** BigWorld paint is 3.0's one remaining paint gap to 2.5.10
  (+2.5 to +5 ms wall clock across runs). Line-timed real time per frame:
  `glDrawArrays` 94.4 ms (llvmpipe), Fio's Python around it ~2.9 ms:
  `chunk_uniforms` 1.2 ms (68 calls, each ~8 NumPy scalar reads and 5 lambda
  calls; shared with 2.5.10), `glUniform1i(uPaintEnabled, 0)` 0.49 ms and the
  paint coord tuple 0.15 ms (both new in 3.0: sent for every chunk, on a map
  with no paint at all). GL calls: 3.0 +67/frame in `draw_heightfield_slots`.
* **Change.** `chunk_uniforms(slots)` computes every drawn chunk's uniforms in
  one set of column operations (same float64 arithmetic, same float32
  rounding) and returns plain Python values. The paint lookup runs only when
  the map has paint, and `uPaintEnabled` is sent when it changes.
* **Fixed on the way (pre-existing, 3.0).** `_upload_paint_chunk` binds the
  paint page on the active unit, and it ran inside the loop after the chunk's
  height page was bound: on the frame a chunk's paint was uploaded, that chunk
  and the rest of its height page drew with the paint texture as their height
  grid. Uploads now happen before the loop, as height uploads already did.
* **Tests.** `test_each_chunk_draws_with_its_own_height_grid_and_paint` (`gl`):
  painted and unpainted chunks in one draw, on the upload frame; at each
  chunk's `glDrawArrays` the paint uniforms and the bound height and paint
  pages are that chunk's. Fails before (a chunk drew with texture 308, the
  paint page, as its height grid 307). The vertex-data and pixel parity tests
  (`test_terrain_heightfield_parity.py`) pin `chunk_uniforms`' values. GL
  tier 163 passed.
* **Result.** `chunk_uniforms` 2.95 -> 1.21 ms/frame profiled (68.6 calls ->
  1); paint Python calls 5,308 -> 4,842 per frame. GL calls in
  `draw_heightfield_slots` now 2.8 *fewer* per frame than 2.5.10. Wall clock
  moved within BigWorld's +-3 ms noise.
* **Commit.** `223eac8`.
* **What is left of the BigWorld gap is not Python.** With `FIO_GL_DEBUG=1`,
  3.0 makes +13 GL calls in terrain set-up and +12 in `_bind_shadow_maps` (the
  8 shadow samplers), +5 in `_paint_frame`; the rest is per-fragment shader
  work for the 8-sampler shadows and the paint branch, which only a GPU
  measurement can size.

### Open after this pass

* **Hitscan (#4): done.** `CollisionRows` (`engine/logic_collision.py`)
  holds every box of `_collision_brushes_cache` in cache order, built on
  first use after `refresh_collision_brushes_cache` and again when the cache
  list is replaced or the world epoch moves. The objections above are met by
  reading live what can change live: mover and door rows are re-read once a
  shot, and blocking flags (`hidden`, water, fog, trigger) are asked only of
  the few rows a ray meets, nearest first. Box corners are float32 as
  `glm.vec3` rounds them; the slab test is float64 as the walk's Python
  floats were. Hit positions are bit-identical to the walk on every map
  in `maps/` with the dense path forced, and on randomised worlds
  (`tests/engine/test_hitscan_dense.py`). The monster pass is one batched
  ray/sphere test whose float32 dot products are summed `(x + y) + z` as
  `glm.dot` does (verified bitwise on 200k pairs).
  **Behaviour change, by decision:** a shot's pellets trace one snapshot and
  apply their effects afterwards in pellet order, so a pellet's I/O reaches
  the next shot, not the rest of this one (`tests/engine/test_hitscan_snapshot.py`,
  CHANGELOG). Per shot on BigWorld (1,224 shots, 3 interleaved rounds, ms
  median): 1 pellet 0.80-1.32 -> 0.16-0.41; 8 pellets 6.2-10.0 -> 0.64-1.00.
  What is left is NumPy dispatch, about equally the slab test over 3,604
  rows, the 34 movers' re-read, the monster snapshot and the batched sphere
  test; skipping unmoved movers by identity would save ~35 us but the 2D
  view resizes brushes in place, so it is not done. The small-swarm
  projectile wall test uses the same rows (exact, float64 boxes):
  511 -> 37 us a projectile on BigWorld. Threshold
  `DENSE_WALK_MIN_BRUSHES = 16`, measured by
  `tests/performance/test_hitscan_threshold_benchmark.py`.
* **Rows 6, 9, 11** (frustum, player collision helpers, slot classification
  per portal view): 0.05-0.23 ms each, clean; no Python loop to remove.
* **Intermittent GL-tier warning.** `test_interleaved_textures_are_one_draw`
  twice reported NumPy's "invalid value encountered in cast" in a full
  `-m gl` run (2 of ~6 runs on this branch, 0 of 1 at `215269c`; never alone,
  never with the warning as an error). Consistent with a cast over
  uninitialised (`np.empty`) rows somewhere on the sprite path; not traced.

## Second performance pass: remaining Python in hot paths

Goal: the measured-but-unadopted items left by the first pass, each kept only
if it shows a reliable benefit. Design yardstick: *tables all the way to GL* --
a direct path from authoritative table columns to GPU submission with as little
redundant work as possible, and no machinery that does not pay for itself.

### Environment and method

* **Interpreter: CPython 3.13.16** in this container (3.14 was not installable
  here); otherwise the pinned set (PyQt5 5.15.11, numpy 2.5.3, PyOpenGL
  3.1.10, PyGLM 2.8.3, pygame-ce 2.5.8). Mesa llvmpipe under Xvfb, 4 cores;
  every timing ran pinned to two cores (`taskset`, `LP_NUM_THREADS=2`) with
  the other two idle or running only stepped-clock captures.
* **Revisions.** `base` = `88f52e9` (start of this pass). Each change was
  measured as a worktree snapshot against the revision before it; the final
  candidate (`t5`) is task 1 + task 3 + task 5 + F-09, i.e. `576b288`,
  `571de5e`, `73428aa`, `c7b930d`.
* **In-place timing.** One `perf_counter` pair around the function in question
  during `tools/bench_compare.py`'s scripted run (300 frames, 3 interleaved
  rounds, medians, ranges in brackets). The pair costs the same on both sides,
  so differences are the function's own. Wall-clock paint on llvmpipe cannot
  resolve sub-millisecond paint changes (first pass, *Method*); logic-thread
  time can.
* **Debug Tables as oracle, never alone.** `tools/oracle_capture.py` (diffed
  with `tools/oracle_diff.py`) plays each level
  on a stepped clock (`perf_counter` = the effect clock's origin + whole
  ticks) with seeded RNGs and scripted input, and at every 20th tick records
  (a) everything Debug Tables shows -- every RenderTable/EntityTable column it
  freezes, rows read, visible slots, its draw/shadow/batched/triangle counters
  -- and (b) an independent read: the published frame's tables read directly,
  the GL draws counted by wrapping `OpenGL.GL`, and the frame's pixels.
  Terrain streaming is capped by a wall-clock budget per paint
  (`Terrain.UPDATE_BUDGET_MS`) and the paints Qt makes while a level loads run
  on the real clock, so how many chunks have streamed by the first checkpoint
  varied between runs of the *same* revision (one chunk, 4,608 triangles, on
  Terrain_Test_small); captures lift that budget so every paint streams what
  it needs.
* **Levels.** Every map in `maps/` (23) and a random level from
  `tools/procedural_map_gen.py` (seed 359815, large, 28 rooms: 866 brushes,
  28 lights, 10 monsters, 9 props), plus a copy augmented with what the
  generator does not make: 12 linear movers (4 spinning), 6 doors toggled by
  movers' I/O, fire/orb effects and an explosion triggered every ~0.5 s,
  water/glass/fog/glow volumes, 4 angled brushes (896 brushes, 55 things;
  `tools/augment_level.py`, same seed; both reproduce byte for byte).
* **Suite.** The whole suite, per top-level directory in its own process
  (`tests/tools/test_debug_tables.py` segfaulted the single-process run at
  ~93% on the untouched baseline in this container), compared test by test
  against the baseline.
* **Harness fault found and fixed.** A benchmark killed mid-run left the
  baseline worktree's `settings.ini` without its `[Display]`/`[Renderer]`
  sections, so for a while base and the candidate ran the editor with
  different settings (sysmon off on one side). Every comparison of whole-frame
  time against base from that period was discarded; every harness now resets
  `settings.ini` from git before each run. Captures were unaffected (they
  switch sysmon and the FPS counter off themselves and matched bit for bit).

### Judgement of the final candidate

**1. Correctness.** Whole suite, base vs candidate, test by test: no test
that passes on base fails on the candidate. Both share 9 failures and 61
errors that are this container's, not the code's: `r_wireframe` console tests
(3), `test_editor_chrome`, `test_load_throbber`, `test_toolbar_buttons`,
`test_portal_fade` (3), and a session teardown error ("a closed
FioTestSession's window is still reachable") on every GL session test. The
candidate adds one error of that last kind (the new GL e2e test's teardown;
its body passes). One real regression was found and fixed before judging: the
F-09 test patched `PropSession` itself, which `test_suite_integrity`
forbids; it now counts calls on the instance.

**2. Visual equivalence.** All 23 shipped maps and both random levels, 7
checkpoints each, stepped clock, unbudgeted terrain: every Debug Tables
reading and every independent reading (published tables, GL draws issued,
pixels) of the candidate is bit-identical to base, except
`effect_spawn_time` -- the load-time wall-clock stamp, which differs just as
much between two runs of the candidate. Checked element by element: no
spawn-time element differs base/candidate where it is stable between the
candidate's two runs. The same levels also ran the excluded changes
(transform cache, 2b, 7b) bit-identical; they were excluded for speed, not
correctness.

**3. Performance, end to end** (`bench_compare`, 5 interleaved rounds x 600
frames, medians [range], ms):

| Level | Logic tick, base | candidate | Paint CPU, base | candidate |
| --- | --- | --- | --- | --- |
| _SHOWCASE | 0.708 [0.665-0.732] | **0.520** [0.494-0.538] | 6.98 [6.75-7.18] | 7.08 [6.78-7.16] |
| MonsterTest | 0.602 [0.570-0.675] | **0.497** [0.491-0.566] | 3.08 [2.87-3.29] | 3.21 [3.20-3.53] |
| random_large_aug | 1.300 [1.220-1.397] | **1.198** [1.040-1.229] | 3.64 [3.31-3.80] | 3.66 [3.23-3.76] |
| random_large | 0.658 [0.536-0.733] | 0.591 [0.527-0.662] | 2.89 [2.53-3.24] | 2.66 [2.50-2.97] |
| BigWorld_streaming_test | 0.977 [0.970-0.990] | 0.963 [0.951-0.978] | 91.2 [90.1-92.9] | 90.4 [89.6-90.9] |
| Portal_Test | 0.370 [0.346-0.417] | 0.394 [0.347-0.560] | 5.89 [5.60-6.36] | 6.27 [5.71-9.68] |

The logic tick is faster wherever there are Effects or movers (-27% on the
showcase, -17% on MonsterTest, -8% on the augmented level, ranges apart) and
unchanged elsewhere. Paint CPU per frame at 640x360 cannot show task 1's
~0.24 ms: llvmpipe rasterises on the paint thread and rounds differ by up to
1 ms. Measured instead as total paint-thread CPU in `paintGL` over all
frames, base vs task 1 alone, 9 paired rounds at 64x36 (every GL call and
upload kept, rasterisation minimised): showcase **-0.26 ms** median, lower in
8 of 9 rounds; the control (MonsterTest, 2 uploads a frame) +0.14 ms, lower in
3 of 9. At 640x360 the showcase reads -0.19 ms, lower in 6 of 9 -- within the
noise. Task 1's frame-level gain is real but only where per-row uploads are
many, and small against llvmpipe's own cost; a GPU measurement would size it
better.

**4. Selection.** Kept only what showed a repeatable benefit, or (F-09)
removed code. Excluded with numbers: see the table.


### Decisions, one per task

| # | Task | Measured | Decision |
| --- | --- | --- | --- |
| 1 | Per-row uniforms by raw address | Uploads, showcase 28.3/frame: 330-415 -> 85-91 us/frame; MonsterTest 33-35 -> 7-9; frame: above | **Kept** `576b288` |
| 2 | Per-frame `model_matrices` cache | Builds/frame fall only where passes repeat a slot set: Portal_Test 5.38 -> 3.59 (302 -> 240 us/frame); elsewhere the key/pool bookkeeping costs more than it saves: showcase 255 -> 294, MonsterTest 151 -> 159, Office 104 -> 106, BigWorld 125 -> 122 | **Excluded** |
| 2b | Rotated rows' fill vectorised (found on the way) | Isolated: 99 -> 69 us for 14 rows with 2 rotated, bit-identical; in place no change (showcase 253 vs 251, augmented level 124 vs 123) | **Excluded** |
| 3 | Effect columns published on change | `_advance_effects` showcase 126 -> 71 us/frame, augmented level 157 -> 107 | **Kept** `571de5e` |
| 4 | No `tuple(things)` per frame | Saving per table per frame 0.07 / 2.3 / 9 us at 45 / 1,000 / 3,604 rows; the tuple is also the cross-thread snapshot | **Not adopted** |
| 5 | Small-N thresholds | Movers/doors: below 12 rows walk `_one`: showcase mover tick 316 -> 138 us, MonsterTest 274 -> 92, 6 doors 84-95 -> 55-58. `_advance_effects`: after task 3 it is clock-only and ~70 us on the showcase with no row-count crossover left worth code. Projectile wall test: already has `PROJECTILE_DENSE_THRESHOLD` | **Kept** (movers/doors) `73428aa` |
| 6 | Frustum planes once per view | Already once per view per frame (logic tick, `render_scene`, each portal view); 9-12 us each, Portal_Test 4.5 distinct views ~35-40 us; nothing repeated within a frame | **No change** |
| 7a | F-09 duplicate `PropSession.start()` | ~25 us once per Play start; removes code | **Kept** `c7b930d` |
| 7b | Skip unmoved movers in `CollisionRows` (+ the physics grid's twin) | Per shot on BigWorld 116 -> 12 us, but 0.05 shots/frame on the scripted route: ~5 us/frame; the grid twin saved nothing measurable. Needs an in-place-edit stamp in editor code | **Excluded** |
| 8 | mypyc spike | See below | **Not merged** |

### Notes on the kept changes

* **Task 1** (`engine/renderer/core/uniform_rows.py`). The native call uses
  PyOpenGL's own resolved pointer (refused if null), its own ctypes function
  type and scalar argument types; only the data argument is `void *`. Every
  row index is bounds-checked before an address is formed; the uploader holds
  the array. The first raw upload of each kind is read back through
  PyOpenGL's checked `glGetUniformfv` into an oversized buffer and compared
  byte for byte, falling back to the wrapper on any mismatch -- which catches
  wrong data, not a wrong ABI. `FIO_GL_DEBUG=1` keeps the wrappers.
  `tools/profile_uniform_uploads.py --tree <checkout>` reproduces the per-pass
  table. **Platform status:** executed on Linux x86-64 / Mesa only. Windows on
  ARM64, macOS and Android have not run it and are unverified until the GL
  tier runs there; the ARM64 CI job builds but runs no tests. The `player/`
  GLES front end does not run these passes yet.
* **Task 3.** `EffectStore.generation` moves on every change to a published
  column; each table of the double buffer publishes when it differs from what
  it last saw, pairing rows by identity. Store rows are in world order after
  a rebuild and in first-synced order for rows appended in between (the F-08
  wart).
* **Task 5.** The walked path is the per-row code the plan is already proven
  against (`tests/logic/test_mover_table_equivalence.py`), now run on every
  world both planned and walked.

### Spike: mypyc (not merged)

* **Effort.** mypy/mypyc 2.4 in a separate virtualenv (a build-time tool;
  nothing at runtime). `engine/` is a namespace package, so mypyc needs
  `--explicit-package-bases --ignore-missing-imports --follow-imports=silent`.
  `render_cull`, `mover_table` and `physics` compiled (~56 s) after small
  edits: return annotations on `PhysicsBody`'s properties (mypyc otherwise
  hits an internal error), `KEYS: tuple` on two `_Group` subclasses,
  `GRAVITY: ClassVar`. `entity_table` does not compile: the defensive
  `editor.things` import rebinds classes to `None`, and its columns are
  created with `setattr` from `_COLUMNS` -- native classes need declared
  attributes.
* **Tests do not pass unchanged.** On the physics, monster AI, render cull,
  engine and logic suites: interpreted 1,280 passed; compiled 366 failed and
  160 errors. Native classes refuse weak references (the change journal and
  caches hold tables and grids weakly), attributes added at runtime
  (`PhysicsBody._initial`) and attribute deletion. Each is fixable in the
  source (`@mypyc_attr(...)`, declared attributes), i.e. the modules would
  have to be written for mypyc.
* **Speed** (quiet core, 3 interleaved rounds, BigWorld's brushes):
  `render_cull.cull_by_distance` over 3,000 objects 617-990 -> 337-384 us
  (1.7-2.9x); `SpatialGrid.raycast_down`, `get_potential_colliders`,
  `overlaps_wall` (1.0-2.7 us a query) and `populate` (19-22 ms) unchanged:
  the hot code is NumPy and glm bound, which compilation does not touch.
* **Packaging.** One extension per module per platform and ABI, plus
  mypyc's shared runtime module at the package root: the Windows ARM64 job
  would need MSVC ARM64 builds of each, Android a python-for-android recipe,
  and the standalone tier must keep importing the `.py` sources. The Nuitka
  builds already compile all Python to C, so mypyc would mainly help source
  runs.
* **Verdict.** Not worth adopting now: one function gets ~2x, the
  dispatch-bound hot paths do not, and the modules would need rewriting for
  mypyc's object model.

### Open after this pass

* Per-pass `projection`/`view` uploads still go through PyOpenGL's wrapper
  with `glm.value_ptr` (6.3 us each, measured): ~26 a showcase frame, ~63 on
  Portal_Test. Same treatment as task 1 would apply.
* `_draw_dense_model_single` still uploads one matrix through the wrapper
  (~0.4 a frame on the showcase, ~4 us).
* Props adopted at Play start never get a rest callback on the new session's
  `PhysicsWorld` (neither `start()` call ran while it existed); `OnRest` may
  not fire for them. Unverified; not changed.
* Terrain streaming's wall-clock budget makes the first frames of a terrain
  level timing-dependent; harmless in play, but tests that compare frames
  must lift it (as `oracle_capture --unbudgeted-terrain` does).
* A GPU (and a Windows ARM64 machine) to measure paint-side changes and run
  the GL tier where the raw uploads have not yet run.

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

* Performance: see *Performance pass / Open after this pass* and *Second
  performance pass / Open after this pass*.

* F-06: resolve engine asset paths from the root directory rather than cwd.
* F-08 / F-09 as above.
* Suite wall time: the 3.0 suite takes ~5x 2.5.10's for ~7% more tests.
* Further end-to-end coverage: editor action -> journal/undo -> save -> reload;
  input -> player/AI -> render state; GL paint through the session.
