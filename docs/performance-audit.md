# Whole-engine performance audit (fifth session)

Branch `3.1.0.0611_CANARY`, from `11e8cb8`. Scope: millisecond-scale costs in
start-up, load, play entry, frame time, hitches and memory. Target: OpenGL
3.3 core on ARM (Snapdragon 8cx Gen 3 / Adreno 690).

**Everything measured here was measured in a container with software GL
(Mesa llvmpipe, x86, 4 cores).** CPU-side Python costs carry over in kind;
anything inside a GL call (shader compilation, vertex and fragment work, Qt
composing the window) is llvmpipe's and is marked *host-specific*. Nothing in
this document was measured on the Snapdragon.

## 1. How it was measured

* `tools/audit_play.py MAP` -- as in the fourth session (paints made from
  the script).
* `tools/audit_play.py MAP --loop` (new) -- the frame loop as shipped: Qt's
  event loop, the view's 16 ms timer, Qt's paints of the 3D view *and*
  whatever else a frame repaints, `main.py`'s 1 ms switch interval. Reports a
  frame's wall time and the main thread's CPU by part. `--hide-2d` prices
  the 2D views; `--span MODULE:CLASS.METHOD` times one more method.
* `tools/audit_startup.py` (new) -- start to the first painted frame, stage
  by stage, with the parts of `initializeGL`.
* `py-spy record --threads [--gil | --native]` attached through `--ready`.

Three things about the fourth session's harness that change how its numbers
read:

1. **It painted every published frame twice** -- once from the script, once
   from Qt (the view's own timer runs inside `processEvents`). Its `paint_ms`
   is one paint; its "paints per second" understates the frame rate.
2. **It ran with the default 5 ms switch interval.** `main.py` sets 1 ms; the
   handoff's "a 1 ms switch interval ... not adopted" was already shipped.
3. **Its first runs were cold.** llvmpipe keeps compiled shaders in
   `~/.cache/mesa_shader_cache`; a first run pays LLVM compilation on first
   use of each shader variant (see Portal_Test below).

A map without a PlayerStart (`Light_Test`) cannot be played: the editor
shows a dialogue, which the old harness waited on for ever. It is now
measured in the editor view.

## 2. Measurements

### 2.1 The frame in play, as shipped (`--loop`, 10 s, llvmpipe)

Before this session's fixes. "CPU" is the main thread's, per frame.

| map | fps | paintGL wall / CPU ms | 2D view CPU ms | Qt CPU ms | Qt + idle wall ms | main thread cores |
|---|---|---|---|---|---|---|
| Office_Corridor | 58.8 | 1.9 / 1.8 | 3.7 | 1.8 | 10.9 | 0.43 |
| MonsterTest | 41.3 | 5.5 / 3.1 | 5.0 | 1.9 | 13.0 | 0.42 |
| _SHOWCASE | 30.0 | 14.1 / 5.3 | 4.8 | 1.8 | 13.9 | 0.36 |
| Portal_Test | 44.5 | 5.7 / 4.1 | 4.0 | 1.8 | 12.5 | 0.45 |
| Terrain_Test_medium | 50.1 | 4.7 / 4.4 | 3.8 | 1.8 | 11.2 | 0.51 |
| BigWorld_streaming_test | 9.8 | 84.1 / 75.7 | 8.4 | 1.3 | 7.6 | 0.84 |
| Light_Test (editor view) | 52.5 | 2.5 / 1.6 | 1.0 | 1.9 | 15.4 | 0.24 |

Logic thread 2-4 % of a core, tick 0.14-0.3 ms (p50), 2.3-5.5 ms (p99);
monster AI at most 3 %. No generation-2 collection in play on any map;
longest GC pause 0.3-4.2 ms.

After the fixes (same command):

| map | fps | paintGL CPU ms | 2D view CPU ms | Qt CPU ms | main thread cores |
|---|---|---|---|---|---|
| Office_Corridor | 57.2 | 1.9 | 0.6 | 0.9 | 0.20 |
| MonsterTest | 50.8 | 3.3 | 1.0 | 1.0 | 0.27 |
| _SHOWCASE | 33.9 | 5.6 | 1.4 | 1.0 | 0.28 |
| Portal_Test | 49.2 | 4.6 | 0.8 | 1.0 | 0.32 |
| Terrain_Test_medium | 51.8 | 4.9 | 0.8 | 1.0 | 0.35 |
| BigWorld_streaming_test | 8.1 | 83.9 | 8.8 | 1.2 | 0.76 |

(BigWorld runs below 10 fps here, so the 10 Hz 2D repaint is still every
frame; its fps moves 8-10 between runs.)

### 2.2 Start-up (`audit_startup.py`, warm cache, three runs)

| stage | before, ms | after, ms |
|---|---|---|
| imports (PyQt5, the engine, NumPy, OpenGL, pygame) | 235-266 | same |
| QApplication, archives, style | 15-18 | same |
| `MainWindow()` | 352-490 | same |
| `initializeGL` | 455-527 | 184-197 |
| -- `create_renderer` (shaders; *host-specific*) | 157-188 | same |
| -- of which the 8 shadow cube maps | 11-13 (27 MB) | same |
| -- every texture in `assets/textures` | 279-310 (53 MB) | 0 |
| -- sprites | 30-57 | same |
| first painted frames (*host-specific*) | 108-131 | same |
| **total to the first frame** | 1113-1455 | 1060-1172 |
| resident memory at the first frame | 378 MB | 328 MB |

With a cold shader cache the same start is 1955 ms (shaders 443 ms, first
frames 247 ms): llvmpipe compiling. A GPU driver's link times are not known
from here.

### 2.3 Load, play entry, memory (loop mode, warm)

| map | load s | play entry s | first play paint ms | resident MB in play, before -> after |
|---|---|---|---|---|
| Office_Corridor | 0.19 | 0.08-0.10 | 8-14 | 415 -> 361 |
| MonsterTest | 0.32 | 0.10 | 14-24 | 441 -> 390 |
| _SHOWCASE | 0.63 -> 0.75 | 0.11-0.13 | 30-34 | 521 -> 489 |
| BigWorld_streaming_test | 0.52-0.68 | 0.36-0.37 | 33-53 | 564 -> 528 |
| Portal_Test | 0.21 | 0.14-0.29 | 28-208 | 421 -> 365 |
| Terrain_Test_medium | 0.19-0.30 | 0.09 | 16-19 | 449 -> 419 |

## 3. Findings, ranked by user-visible milliseconds

### F1. The 2D editor view was repainted in full on every frame of play -- FIXED

4-9 ms of main-thread CPU a frame on every map (Python and QPainter, GIL
held): more than the 3D frame's own Python on Office, MonsterTest and
Portal_Test. Three triggers, all per frame in play: `update_loop` calling
`editor.update_views()`, each `View2D`'s 60 Hz camera timer (the camera moves
every frame of play, and the "camera indicator" repaint is the whole view),
and its 60 Hz arrow animation. The old harness never showed it: it timed
`paintGL` only. Not host-specific (CPU-bound).

Fix (`bf29bd0`): in play the 2D views repaint at
`QtGameView.PLAY_2D_VIEWS_HZ` (10) and their own timers stand down. Measured:
2D view 3.7-5.0 -> 0.6-1.4 ms a frame; Qt's own CPU 1.8 -> 0.9 ms; main
thread 0.43 -> 0.20 cores (Office), 0.42 -> 0.27 (MonsterTest); MonsterTest
41.3 -> 50.8-52.6 fps, Portal_Test 44.5 -> 49-51, _SHOWCASE 30.0 -> 34.
**Behaviour change:** the top/side/front views follow play ten times a
second instead of every frame. Editing is unchanged.

Not fixed: one 2D repaint is still 4-9 ms (a per-brush and per-model-triangle
Python loop: `_draw_model_wireframe` draws three `drawLine` calls per
triangle of the PlayerStart model). It is paid when editing (pan, drag) and,
in play, on one frame in six -- a periodic 4-9 ms on that frame.

### F2. Every texture decoded at start-up -- FIXED

`_preload_assets` decoded all of `assets/textures` in `initializeGL`: 279-310
ms and 53 MB, whatever the map. Fix (`3c1f53b`): the open map's textures are
loaded when the map has loaded and as Play starts, under the loading bar.
Measured: `initializeGL` 455-527 -> 184-197 ms, 50 MB less resident memory
from start-up through play; `_SHOWCASE` loads 0.1 s slower (its own
textures), other maps within noise. (On Windows the old preload also keyed
the four terrain textures differently from the terrain's own loader --
`textures\terrain\x` against `textures/terrain\x` -- so they were decoded
twice there. Read from the code, not run.)

### F3. BigWorld's frame is the terrain draw -- PART FIXED (M5.1)

`Terrain.draw_heightfield_slots` is 70 of the 76-84 ms of main-thread CPU in
BigWorld's paint; the rest of the renderer is ~7 ms. `py-spy --native` puts
that time inside the draw calls: Mesa and its JIT-compiled vertex shader
running on the calling thread. *Host-specific in magnitude.* What is not
host-specific:

* 65-68 chunks drawn a frame, **all at the top resolution (48)**: the first
  LOD threshold is 4608 units, beyond what is in view.
* 300-313k triangles drawn unindexed: **~930,000 vertex-shader runs a frame
  for ~160,000 distinct grid points** (each shared by six triangles).
* Each run does 7 texel fetches (3 corner heights, 4 for the smooth normal)
  and walks the colour gradient (a loop of up to 32 stops).
* Chunk heights are generated on the paint thread while moving:
  `Terrain._upload_chunk` ~2 calls a frame, 0.8 ms p50, 6.6 ms max.

Whether 930k such vertices matter on the Adreno is **unknown**; nothing here
can say.

M5.1 (`9a9049b`, after this audit): the heightfield is drawn indexed with
flat face outputs, 2 (res+1)^2 vertices a chunk instead of 6 res^2. Measured
with `tools/bench_terrain.py`: frame 81.7-84.5 -> 52.5-54.9 ms, the
terrain's vertex stage 44-50 -> 14.6-15.2; as shipped BigWorld 8.1-9.8 ->
13.7 fps. LOD selecting full detail is by design (every resident chunk is
inside the protected zone); chunk generation is still on the paint thread.
See the handoff's M5 section.

### F4. Qt composing the window -- OPEN, host-specific

11-15 ms of wall time a frame is outside `update_loop`, `paintGL` and the 2D
paint: Qt composing the window with the 3D view's framebuffer, and waiting
for the 16 ms timer. Under llvmpipe the composition is a software blit of the
whole window; on a GPU it should be small. Unverified. If it is not small on
the target, full-screen play (F12) is the first thing to compare.

### F5. First use of shaders (Portal_Test's play entry) -- host-specific

The handoff's 0.9 s. Reproduced as a cache effect: with Mesa's shader cache
removed the first play paint is 654 ms and entry 0.74 s; warm, the paint is
19-250 ms and entry 0.11-0.35 s. It is llvmpipe compiling the portal pass's
shader variants on first draw. On a GPU the equivalent is the driver's
first-draw compilation; measure there before doing anything (the remedy
would be a warm-up draw of each program under the loading bar).

### F6. Hitches in play -- small, OPEN

* Textures decoded in a frame: BigWorld `load_texture` p95 4 ms, max 17 ms
  (sprite recipes resolved when first seen). F2's preload covers brush
  textures only; sprites and model textures still load on first sight.
* The sound mixer: with no audio device, `pygame.mixer.init` fails in ~100
  ms on the GUI thread and is retried every 10 s while sounds play
  (`MIXER_RETRY_SECONDS`). Seen here because the container has no device;
  a machine with audio pays once.
* The BigWorld debug panel and minimap (on by default,
  `show_cell_debug`): 1.7 ms CPU a frame, 8 ms p95, 18 ms max.
* GC: no generation-2 collection in play; longest pause 0.3-4.2 ms.

### F7. The GIL behind the main thread -- measured, no fix

As shipped (1 ms switch interval) ticks are 0.14-0.3 ms (p50) and 2.3-5.5 ms
(p99); they land within 3.3-5.3 ms of their slots at p95. `py-spy --gil`:
the main thread holds the GIL ~17 % of the time on BigWorld, most of it in
the 2D paint (37 % of its GIL-held samples), the BigWorld overlay (10 %) and
terrain chunk generation (6 %); GL calls release it. F1 removes most of the
2D share. With the 2D view hidden, tick p99 fell on the quiet maps (Light
0.61 -> 0.04 ms, Terrain 2.39 -> 1.54) and was mixed elsewhere: at this
level the tail is the switch interval itself.

### F8. Shadow cube maps -- OPEN, small

Eight 384 px depth cube maps allocated at start: 11-13 ms and 27 MB here
(on a GPU, 28 MB of VRAM). Allocating on the first shadow-casting light
would save that for maps without one; not done (most shipped maps have
lights, and the first use would then cost a frame).

### F9. Play-cycle memory guard -- passes here, cause not isolated

`tests/e2e/test_play_cycles.py` passes at this head: 478 KiB retained over
30 sessions (limit 512; 631-647 at the last handoff). One run; which change
moved it was not isolated. The largest retainer is still `json/decoder.py`
(116 KiB, 2,267 objects: about 75 objects a session), then the Debug
Console's log lines. It is close enough to the limit to fail again.

### F10. SysMon's VRAM line -- FIXED as far as it can be checked

`2cf304f`: the vendor queries are made only where the driver advertises the
extension. Not a millisecond cost. llvmpipe advertises both and reports
numbers of its own (a different "total" each run), so its line is unchanged
here; the fix is for drivers without the extensions and is **unverified on
Adreno**.

### Not built

* Monster AI on alternate ticks: measured last session, no benefit.
* Model draw groups (the boundary fix, `a6c85a9`): the model pass costs
  0.22-0.24 ms a frame before and after. Kept for the boundary, not for
  speed.

## 4. Recommendation: M5

**M5 = the terrain through tables to GL, starting with a measurement on the
Snapdragon.**

Evidence:

* It is the only place where one subsystem is the frame: 70 of 84 ms in
  BigWorld here, against ~7 ms for every brush, model, sprite and light pass
  together. After F1 and F2 no other measured item exceeds ~5 ms a frame.
* The work per frame is large by count, not only by llvmpipe's clock: 930k
  vertex runs for 160k distinct points, no LOD in view, 7 fetches each.
* It is also the largest breach of the renderer boundary
  (`docs/renderer-boundary-audit.md`, V4): the renderer drives and mutates
  the editor's `Terrain` object every frame and generates chunks on the
  paint thread. The performance work and the boundary work are the same
  work.

Proposed order:

1. **Measure on the target first** (a GL timer query around the terrain
   pass, and the frame with terrain off). If the Adreno shades it in a
   millisecond or two, steps 3-4 lose their case and M5 is steps 2 and 5.
2. Chunk heights generated off the paint thread (the logic thread, or a
   worker), published as `TerrainTable` rows; the renderer uploads only.
3. Fewer vertex runs: two vertices a quad instead of six (flat outputs from
   the provoking vertex), or indexed strips with the flat normal derived in
   the fragment shader -- each checked for pixel parity.
4. LOD thresholds that engage inside the view distance.
5. The renderer reads published terrain rows and keeps its GL names itself;
   `terrain` leaves the frame input.

What would change this recommendation: a Snapdragon run showing the frame
bound by something this container cannot see (F4's composition, F5's
first-draw compiles). The self-hosted runner the last handoff asked for is
the precondition for judging any GL-bound item, this one included.
