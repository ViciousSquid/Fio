# Renderer M3: change-driven box instance uploads

Status: **implemented** for box brush instances (the lit, textured and shadow
passes): see *M3 as built* below, then *M3.1* (what the passes derive, kept
by change) and the outline of *M4* (proposed, not started). The profiling
that designed M3 follows, unchanged, as it was recorded before any renderer
change.

Measured on the Stage 2 head (immutable convex geometry, transform-only
movement), CPython 3.13, llvmpipe (software GL), 640×360, one pinned core,
with every GL buffer upload intercepted and attributed to the renderer
function that made it. Byte and call counts are exact; times are relative
(the host was shared with a test run).

## What a frame uploads

| Map, scenario | Upload per frame | From |
|---|---|---|
| BigWorld (3,604 boxes, 1 draw): still | 450.5 KB, 1 call | `instancing._pack_brush_instances` |
| BigWorld: 64 box movers moving | 450.5 KB, 1 call | same |
| BigWorld: one brush edited per frame | 450.5 KB, 1 call | same |
| `_SHOWCASE` (85 brushes, 17 angled, 47 draws): still | 28.6 KB, 3 calls | same |
| `_SHOWCASE`: 32 box movers moving | 28.6 KB, 3 calls | same |
| `_SHOWCASE`: one brush edited per frame | 28.6 KB, 3 calls | same |

The box (AABB) instance path repacks and re-uploads **every visible
instance, in every pass that draws boxes, on every frame** -- 128 bytes per
instance (model matrix, padded normal matrix, payload) -- whether anything
moved or not. It is the only per-frame upload on these maps: the angled
brushes' GPU-resident transforms (M2's texture buffer, written per dirty
slot) upload nothing on a still frame and one slot per moved brush.

## Where the CPU goes (BigWorld, per frame)

* Packing and uploading the instance buffer: ~0.7 ms (vectorised takes
  into a reused staging array, then one `glBufferSubData` of 450 KB).
* `RenderTable.model_matrices` for every visible slot: ~0.4 ms, recomputed
  every frame although the transforms are unchanged.
* The remainder of `render_scene` (~8 ms here) is llvmpipe rasterising
  inside the draw call -- GPU work done on the CPU by the software driver,
  which a real GPU does not charge to the CPU.

On the Snapdragon 8cx Gen 3 target the Python share scales with
single-thread speed (several times slower than this x86 core), and 450 KB a
frame is ~27 MB/s of uploads at 60 fps on shared memory -- for a still
world.

## What M3 would change (outline, not started)

The same pattern M2 applied to angled brushes, for the boxes:

* Keep each brush's instance data (matrix, normal matrix, payload) in a
  persistent, slot-indexed GPU buffer (a texture buffer: OpenGL 3.3 core,
  which Adreno offers, has no base-instance draws), written only for the
  slots whose transform or payload changed -- dirty ranges, as
  `geo_store.write_transforms` already does.
* Per pass, upload only the visible slots' indices (4 bytes per instance
  instead of 128) and fetch the instance data in the vertex shader.
* Derive model matrices only for changed rows (the table's transform
  generation already says which), not every visible slot every frame.

Expected on BigWorld: a still frame uploads ~14 KB (the index list) instead
of 450 KB, and the pack/matrix CPU (~1.1 ms here) mostly goes; a moving
frame adds 128 bytes per moved brush.

## Still to measure before designing

* Shadow and other passes that pack brush instances (with shadows on, the
  depth passes pack again per light).
* Camera motion: the visible set and its order change every frame, which
  is the case the index list must keep cheap.
* Sprite, model and effect instance buffers (also repacked per frame;
  absent from these maps).
* The same scenarios on the target device (or an ARM CPU) to weigh upload
  bandwidth against Python packing time.

Reproduce: `xvfb-run -a python tools/profile_frame_uploads.py BigWorld_streaming_test.json`
(and `_SHOWCASE.json --movers 32`).


## M3 as built

Commits on `3.1.0.0611_CANARY`: `d91d8e0` the benchmark tool
(`tools/bench_instance_uploads.py`, extended in `071d3d0`), `6733997` the
slot-transform cache, `d3e7e43` the per-pass instance streams, `8e00359`
their coalescing rule, and this record (`3a59395` and after).

### The choice: change-driven attributes, not slot-resident transforms

The outline above proposed moving each box's matrices into a slot-indexed
buffer texture and fetching them in the vertex shader (M2's pattern for
angled brushes). For boxes that multiplies the GPU's reads: an instance
attribute is fetched once per instance, a buffer-texture fetch once per
*vertex* -- seven texels for each of a cube's 36 vertices in the lit pass,
252 fetches where the attributes cost one instance record. On a tiled mobile
GPU that is the wrong trade for a CPU saving that can be had without it. So
the instance layout, the shaders and the draw calls are unchanged; what
changed is when the rows are built and uploaded.

### What was built

* **Every row's matrices kept by change** (`engine/renderer/core/
  slot_transforms.py`). Per render table, the model and normal matrices of
  every row and the two columns they are built from (`bounds`, `rot`). A
  reader first tests both columns with whole-column equality (~8 µs for
  BigWorld's 3,604 rows; the row-wise `!= ... any(axis=1)` it replaced cost
  118) and only on a difference finds and rebuilds the changed rows; a
  reconcile that renumbers the slots rebuilds all. Rebuilt rows are stamped
  with the cache's version. `_frame_transforms` gathers from it.
* **One instance stream per pass** (`engine/renderer/forward/instancing.py`,
  `BrushStream`): its own buffer, vertex array and CPU mirror, keyed by the
  pass, the view (the main view or a portal's) and the pass's use within the
  frame (the textured and lit passes draw more than one brush group). Each
  frame:
  * nothing changed -- the same items, payload and spare, and the table's
    matrices at the version the buffer last matched -- returns at once:
    **no row is built and no byte uploaded**;
  * the same items with some rows moved: only the rows the stamps name, the
    rows another table's frame wrote meanwhile, and rows whose payload
    changed (a selection, a colour) are rebuilt and uploaded, in coalesced
    ranges;
  * the items changed (the visible set moved, a brush was shown or hidden):
    the rows before the first changed item are kept unless they changed;
    everything from it on is rewritten;
  * a stream under 1,024 rows, or one most of which changes, is rewritten
    whole and uploaded in one call, as before M3 (comparing costs more than
    it saves there, measured).
* **The two double-buffered render tables** each keep an entry per stream
  (the version of that table's matrices the buffer last matched, and the
  rows the other table's frame wrote since), so alternating tables upload
  only what differs between them.
* **The shadow pass** keeps a stream per shadow slot; with its existing
  signature cache a still light neither renders nor uploads.
* **Lifecycle.** `cleanup()` deletes every stream and drops the matrix
  caches; a new renderer (renderer switching, a recreated context) starts
  empty. A play/editor transition, undo/redo, creation or deletion reaches
  the renderer as a table reconcile (new generation: everything rebuilt
  once) or a row-local refresh (the rows that changed); a Big World park or
  unpark as a visibility change (the items change).

### Validation

`tests/visual/test_box_instance_streams.py` (GL): through 40 frames of
random moves, turns, resizes, selection changes, parks and unparks, added
and deleted brushes, camera turns and two render tables taking turns, in the
textured and the solid display modes, the long-lived renderer draws exactly
what a renderer whose streams are rebuilt from scratch every frame draws,
pixel for pixel, and every stream's buffer *on the GPU* (read back) holds
exactly the rows a fresh pack would, to the bit. A still world uploads no
instance byte, with and without shadows; a mover in a 1,500-box world
uploads its one row; a changed selection its rows; cleanup deletes every
buffer and a new renderer draws the same picture; two renderers in turn keep
their own streams. `tests/renderer/test_slot_transforms.py`: the kept
matrices are `model_matrices`' to the bit through moves, turns, resizes and
reconciles. GL renderer and visual tiers: 905 passed; the three
`test_portal_fade` cases fail the same way on the accepted baseline. Full
suite (headless): 6,976 passed, 1 failed -- the pre-existing play-cycle
memory guard (Stage 2 §11.10), at 615 KiB against the accepted baseline's
604. The 11 KiB is the kept matrices of the live render tables: measured
over 10, 30 and 60 play sessions the renderer's share stays at 62.4, 62.7
and 62.8 KiB, so it is bounded, not a leak; a table's matrices are dropped
when the table is collected.

### Measured (x86 VM, llvmpipe; three interleaved rounds against the accepted Stage 2 baseline)

`tools/bench_instance_uploads.py MAP --no-raster` (draw calls stubbed, so
`render_scene` times the CPU side alone -- Python, GL calls, uploads -- which
is what the target pays on its CPU; llvmpipe would otherwise add the GPU's
rasterising to it). CPU is the median of per-round medians, ratio to the
baseline. Instance bytes and upload calls a frame are exact.

BigWorld (3,604 boxes):

| Scenario | CPU ms | ×base | Instance bytes / frame | Uploads |
|---|---|---|---|---|
| still | 1.03 → 0.69 | 0.67 | 450.5 KB → **0** | 1 → 0 |
| frustum-culled turning camera | 0.72 → 0.56 | 0.77 | 129.8 → 128.2 KB | 1 → 1 |
| one mover | 1.33 → 0.95 | 0.71 | 450.5 → 0.1 KB | 1 → 1 |
| 64 movers | 1.31 → 0.91 | 0.69 | 450.5 → 8 KB | 1 → 1 |
| 512 movers | 1.06 → 0.90 | 0.85 | 450.5 → 64 KB | 1 → 1 |
| a selection change | 1.33 → 0.87 | 0.66 | 450.5 → 0.2 KB | 1 → 1 |
| 32 edits a frame | 1.24 → 0.91 | 0.73 | 450.5 → 4 KB | 1 → 1 |
| two tables in turn, 64 movers | 1.29 → 0.93 | 0.72 | 450.5 → 8 KB | 1 → 1 |

`_SHOWCASE` (85 brushes, 68 boxes; streams of a few hundred rows):

| Scenario | CPU ms | ×base (p50 / min) | Instance bytes / frame | Uploads |
|---|---|---|---|---|
| still | 3.30 → 2.70 | 0.82 / 0.73 | 28.6 KB → **0** | 3 → 0 |
| turning camera | 2.33 → 1.76 | 0.76 / 0.77 | 7.0 → 1.3 KB | 2.2 → 0.3 |
| one mover | 3.37 → 3.18 | 0.94 / 1.01 | 28.6 → 22.1 KB | 3 → 1 |
| 64 movers (64 of 68 boxes) | 2.67 → 3.39 | 1.27 / 1.04 | 28.6 → 28.6 KB | 3 → 3 |
| every box moving | 3.21 → 3.36 | 1.05 / 0.86 | 28.6 → 28.6 KB | 3 → 3 |
| a selection change | 3.54 → 3.09 | 0.87 / 0.91 | 28.6 → 0 KB | 3 → 0 |
| 32 edits a frame | 3.40 → 3.21 | 0.94 / 0.88 | 28.6 → 26.0 KB | 3 → 2.2 |
| two tables in turn | 2.52 → 2.66 | 1.06 / 1.05 | 28.6 → 28.4 KB | 3 → 3 |

Timed in the frame (the instance work alone: matrices and streams, two
alternations), `_SHOWCASE` costs 135–148 µs against 258–290 on a still frame,
~310 against 241–310 with one mover, and 30–70 µs more than the baseline
when nearly every box moves every frame (change detection, and the frame's
one rebuild of the moved rows' matrices) -- ~2 % of its 3.2 ms CPU frame,
at the same bytes. BigWorld's instance work is lower in every case.

GL calls a frame: unchanged (±3). Python calls a frame: fewer on still
frames (BigWorld 805 → 732), up to ~100 (BigWorld) or ~300 (`_SHOWCASE`)
more when rows change. Memory the renderer keeps for box instances (stream
mirrors and kept matrices, against the old staging and matrix buffers):
BigWorld 0.80 → 1.14 MB (1.75 MB with both double-buffered tables' matrices),
`_SHOWCASE` 36 → 133 KB; the frames' allocation peak falls (BigWorld 640 →
490 KB). Each stream's GPU buffer is the size of its mirror.

The production path (`tools/perf_gate.py --probes frames,gameplay`, base the
accepted baseline): `render_scene` frames 0.95–0.99 (BigWorld moving 0.96,
faster); gameplay frame and paint on BigWorld, MonsterTest, Office and
`_SHOWCASE` 0.95–1.05, none significant over seven rounds (a first
five-round run flagged `gameplay_big_logic_ms` at 1.024, p .028 -- the logic
thread, which M3 does not touch; 0.99 on the re-run).

### Remaining limitations

* **A changed visible set re-uploads from its first change.** Streams hold
  the visible items compacted, so a camera turn, a cull or a brush shown or
  hidden shifts every row after the first changed item; those rows go up
  again (BigWorld's turning camera: ~128 KB a frame, as before M3, at less
  CPU). Item-stable layouts need either GPU-side culling (vertex work for
  every culled instance) or an index indirection (the per-vertex fetches
  rejected above); per-cell streams, or base-instance draws on GL 4.2
  hardware, are the options left.
* **Coalescing favours fewer calls over fewer bytes.** One `_SHOWCASE`
  mover's faces, spread through a few hundred rows, go up as one 22 KB call
  rather than six 128-byte ones.
* **Small maps pay a little for change detection** when nearly everything
  moves every frame (+30–70 µs on `_SHOWCASE`, above).
* **Sprites, models and effects** still repack their instance buffers every
  frame; M3 covers box brushes. (Absent from the maps above.)
* **`MoverTable.publish`** still writes every mover's and door's row into
  the render table every frame; the kept matrices' equality test sees the
  unchanged values, so nothing is rebuilt or uploaded, but the writes remain.
* **Nothing is measured on the Snapdragon 8cx Gen 3 / Adreno 690 target**,
  where the balance of call cost, upload bandwidth and Python speed differs;
  the thresholds (`STREAM_GAP_ROWS`, `STREAM_MAX_RANGES`) were set from this
  VM's measurements.

## M3.1: what the passes derive, kept by change

M3 stopped re-uploading unchanged instances, but every frame still derived
again, from columns that had not changed, what the passes draw: the brush
groups (classification), the lit pass's classes, colours and box payload,
the textured pass's face batches (each box face's texture, run order, UV
scale, shift and rotation), the shadow pass's split of casters into boxes
and meshes; and the glass and fog passes still built their matrices from
the columns. M3.1 keeps all of it until what it was derived from changes.

Commits on `3.1.0.0611_CANARY`: `373fc24` (versions), `4b2368b` (kept pass
arrays), `824dc87` (glass and fog), `14049bc` (the selection patches the
lit pass's kept payload), and this record.

### What was built

* **The table says when material and extents change**
  (`RenderTable.material_version`, `extent_version`). Both move with a
  reconcile or an adopt, as `generation` does; otherwise
  `material_version` moves only when a refresh actually changes a row's
  classification or material columns (everything but `bounds`, `rot` and
  `hidden`), `extent_version` only when a row's half-extents change. A
  mover's centre written every tick, a still frame, or a journalled edit
  that changes nothing moves neither. Telling a real change from a rewrite
  costs a snapshot of the edited rows, at edit time.
* **Each pass keeps what it derived** (`TablesMixin._pass_memo`): per pass,
  view and use in the frame -- the same key as its instance stream -- and
  per render table, since the two render buffers' tables take turns frame
  by frame (one entry per key would be replaced every frame and never
  reused; three tables are kept, the dead dropped first). An entry is
  reused while its table is at the same generation, `material_version` and
  `extent_version`, the visible rows are the same, and the pass's own inputs
  are unchanged: play mode (nodraw faces, triggers) and the texture id and
  size arrays (told by identity: `_gl_texture_ids` hands out a new array
  when a table interns a texture). A still frame costs one equality test of
  the visible rows per pass. Kept: ~180 KB per render table on BigWorld
  (359 KB for the two buffers' tables), 13-26 KB on `_SHOWCASE`; at most
  three tables per pass.
  * classification (`renderer.render_scene`; a copy is taken, since the
    depth sort replaces some of its groups);
  * the lit pass (`passes._lit_derived`): classes, colours, convex rows,
    and the box rows' slots, payload and run descriptors. The payload is
    kept with no selection; the selection is applied as the pass draws
    (`lit_payload_selecting`: the same array when the selection is not a
    box row or is a trigger, else a copy with its colour), so selecting a
    brush derives nothing again;
  * the textured pass (`passes._face_derived`): the face batches with each
    face's slot, UV scale, shift and rotation, the instance payload and the
    run descriptors -- **a box's face state is resolved once per edit**, as
    an angled brush's is once per mesh build -- and whether any convex row
    is visible, so the kept matrices are gathered only when something reads
    them;
  * the shadow pass, per light: the casters split into boxes and convex
    meshes.
* **Glass and fog read the kept matrices** (`_frame_transforms`): no pass
  builds a matrix from the columns itself any more.

Not built: skipping the instance stream's own equality tests when a pass
hands it the very arrays it kept (an identity test). It would save
~10-20 µs a frame here, and would be wrong the first time a caller handed
in a buffer it reuses and refills; the tests stay.

### Validation

`tests/visual/test_box_instance_streams.py` (GL), extended:

* the parity run (now 60 frames) adds retextures, UV and colour edits and
  natural-scale (tiling) boxes to the moves, turns, resizes, parks,
  additions, deletions, selection and camera changes and alternating
  tables; after every frame, every array a pass keeps equals, element for
  element and dtype for dtype, what a renderer deriving everything afresh
  derived, and some were reused;
* each kind of edit on its own (`test_kept_pass_arrays_follow_each_kind_of_edit`,
  textured and solid): a still frame, a move and a selection rebuild
  nothing; a resize of a natural-scale box, a retexture, a UV edit, a tint
  and a hide rebuild; and the kept arrays and the picture match a fresh
  renderer.
* Mutation checks: a memo that ignores `material_version`, or one that
  ignores `extent_version`, fails these tests.

`tests/renderer/test_render_table_row_local.py`: which edits move which
version. `tests/renderer/test_lit_instance_payload.py`: the selection
applied to the kept payload reads exactly as a payload made with it (plain,
subtract and trigger brushes, nothing selected, a selection outside the
rows). GL renderer and visual tiers: 910 passed, 35 skipped; the three
`test_portal_fade` cases fail as they do on M3's head. Full suite under
xvfb, GL oracles included: 7,300 passed, 175 skipped, 5 failed -- the five
known baseline failures of Stage 2 §11.5 (the play-cycle memory guard, the
position window, three `test_portal_fade` cases). The memory guard on its
own reads 616 KiB on M3's head and 630 here; over 10, 30 and 60 Play
sessions the renderer's share is 10.9, 13.7 and 13.9 KiB on M3's head and
24.9, 28.7 and 29.7 here. The difference, ~15 KiB and flat, is the arrays
kept for the live tables; the growth with the session count is the same
in both trees.

### Measured

Five interleaved rounds against `1d7a6b0` (M3's head), pinned to one core,
`tools/bench_instance_uploads.py MAP --no-raster`: the CPU side of
`render_scene` (draw calls stubbed), median of the per-round medians, and
the ratio of the per-round medians and minima. The tool now runs with
PyOpenGL's error checking off, as `main.py` runs the game, so the
milliseconds are below the M3 tables' (which had it on); both trees here ran
the same way. Instance bytes, upload calls and GL calls a frame are the
same in both trees in every scenario (M3.1 changes what the CPU derives,
not what reaches GL).

BigWorld (3,604 boxes):

| Scenario | CPU ms | ×M3 (p50 / min) | Python calls | Allocation peak |
|---|---|---|---|---|
| still | 0.68 → 0.30 | 0.44 / 0.37 | 635 → 579 | 462 → 158 KB |
| frustum-culled turning camera | 0.69 → 0.55 | 0.80 / 0.81 | 745 → 771 | 380 → 435 KB |
| one mover | 0.89 → 0.39 | 0.43 / 0.45 | 784 → 728 | 473 → 193 KB |
| 64 movers | 0.89 → 0.52 | 0.58 / 0.59 | 784 → 728 | 473 → 193 KB |
| 512 movers | 1.08 → 0.71 | 0.65 / 0.65 | 784 → 728 | 529 → 249 KB |
| a selection change | 0.86 → 0.51 | 0.59 / 0.58 | 737 → 693 | 459 → 211 KB |
| 32 edits a frame | 0.89 → 0.55 | 0.62 / 0.61 | 784 → 728 | 526 → 246 KB |
| two tables in turn, 64 movers | 0.95 → 0.57 | 0.60 / 0.62 | 807 → 751 | 474 → 193 KB |

`_SHOWCASE` (85 brushes, 68 boxes):

| Scenario | CPU ms | ×M3 (p50 / min) | Python calls |
|---|---|---|---|
| still | 3.06 → 2.50 | 0.82 / 0.71 | 4,248 → 4,072 |
| turning camera | 2.53 → 2.03 | 0.81 / 0.74 | 2,700 → 2,637 |
| one mover | 3.16 → 2.76 | 0.87 / 0.87 | 4,494 → 4,318 |
| 64 movers (64 of 68 boxes) | 2.68 → 2.81 | 1.05 / 1.10 | 4,552 → 4,376 |
| every box moving | 3.21 → 2.84 | 0.88 / 0.89 | 4,552 → 4,376 |
| a selection change | 3.02 → 2.14 | 0.71 / 0.70 | 4,256 → 4,102 |
| 32 edits a frame | 3.28 → 3.01 | 0.92 / 0.85 | 4,521 → 4,345 |
| two tables in turn | 3.15 → 2.72 | 0.86 / 0.88 | 4,581 → 4,405 |

The 64-mover row's 1.05 is this scenario's noise (M3 recorded it at 1.27 /
1.04): the baseline's five rounds ranged 2.41-3.35 ms. Seven more rounds
of 120 frames, those two scenarios only: 64 movers 3.64 → 2.93 ms (0.80 p50
/ 0.84 min), every box moving 3.19 → 2.78 (0.87 / 0.87).

In every scenario but a turning camera the passes reuse every derived array
(2 a frame on BigWorld, 4 on `_SHOWCASE`); with the camera turning they
rebuild them, as before M3.1, for ~25 more Python calls and ~55 KB more
allocation peak a frame (the kept copy of the visible rows), and the frame
is still faster.

The production path (`tools/perf_gate.py`, base M3's head, both trees clean
worktrees): `render_scene` frames 0.92-0.94 over five rounds (BigWorld
still and moving and `_SHOWCASE` moving judged faster; `_SHOWCASE` still,
0.94, the same); gameplay frame and paint over seven rounds: MonsterTest 0.94
(faster), `_SHOWCASE` 0.97, Office 0.96, BigWorld 1.00, none slower. A first
gameplay run had flagged `_SHOWCASE` paint at 1.13: its two trees had
different `settings.ini` window layouts (the test suite writes one), so
they drew different frusta -- every frame a different triangle count. With
the same `settings.ini` the two trees drew identically, frame for frame
(draw calls, triangles and every pass's counts over 40 frames), and the
gate above shows no regression.

### Remaining limitations

* **A changed visible set still re-derives the passes' arrays** as well as
  re-uploading from its first change: a turning camera rebuilds them every
  frame (at about the cost it had before M3.1). Keeping them per cell, with the
  per-cell streams M3's limitations describe, would cover both.
* **The tests that tell a still frame are still O(rows)**: one equality
  test of the visible rows per pass, two whole-column tests of `bounds` and
  `rot` per table (M3), and the streams' own. Contiguous memory and C
  loops, but under cProfile they are about a fifth of a still BigWorld
  frame's `render_scene` (the pass memos' tests ~7 %, the instance stream's
  ~13 %, the matrices' sync -- ~5 % -- included), and they grow with the
  world; M4 below replaces them with versions the tables publish.

## M4 (proposed, not started): change sets from the tables to GL

Direction agreed (2026-11-10), with two qualifications that this outline
now carries: the renderer must not miss a change, and "flat cost" means
flat *change-detection* cost.

M3 and M3.1 reach zero instance uploads and no re-derivation on a still
frame, but they *find out* that nothing changed by comparing: two
whole-column tests of `bounds` and `rot` per table, one test of the visible
rows per pass, the streams' tests of items and payload. The logic thread
meanwhile writes every mover's and door's pose into the render table every
frame, moved or not. M4: **the tables publish what changed, and the
renderer reads that.**

### The primary invariant

**Establishing that nothing changed costs a constant amount of work per
table and per pass, whatever the size of the world and however many rows
are visible; a change costs work in proportion to the rows it changed.**

Total frame cost is *not* held constant. An unchanged frame still submits
its draws, sets its state, and culls when the camera moves; that work
grows with what is visible, it is rendering, and it is measured as it is
today. What M4 removes is the work that grows with the world only to
discover that nothing happened.

The invariant is measured by counters, not only by time: every
change-detection site reports how many rows (or elements) it examined. On
a still frame the total is a constant independent of world size (the
target is zero rows); for k changed rows it is O(k). Tests assert the
counters on BigWorld's rows at 1×, 4× and 16×; the benchmarks report
change-detection time separately from render time.

### The renderer must not miss a change

A version counter is only as reliable as its least careful writer, and a
missed mark does not fail loudly: the renderer reuses stale data, and keeps
reusing it, indefinitely. So participation in the contract is enforced and
checked, not assumed.

1. **One door per table.** Today the render table's columns are written by
   `render_table.py`'s own resolve, refresh, reconcile and adopt paths and,
   outside it, by `MoverTable.publish` alone (the centre of `bounds`, and
   `rot`, assigned directly). M4 puts every write behind table methods that
   write and stamp in one step (`MoverTable.publish` calls a
   `write_transforms(slots, centre, rot)` instead of assigning columns), and
   makes the columns read-only outside them (NumPy `writeable = False`,
   opened by the writing method; columns are handed out as read-only
   views). A write that bypasses the stamps raises at once -- in a test, or
   in play -- instead of going stale.
2. **An oracle mode.** With `FIO_RENDER_ORACLE` set, every frame also runs
   M3/M3.1's full comparisons beside the stamps and fails, naming the rows
   and columns, if anything changed that no stamp reported (and if any kept
   pass array differs from deriving it afresh). The **whole test suite**
   -- editor, e2e play, plugins (Big World, deathmatch), savegame, GL
   oracles -- runs once with it on, so a write path nobody listed is caught
   by any test that renders through it, not only by tests written for M4.
3. **A test for every known write path.** Each performs the write through
   the real API (no hand-poked columns), then checks the version-driven
   renderer against a renderer with no caches: kept arrays, stream mirrors,
   GPU buffers read back, and the picture (M3.1's per-edit test, extended).
   The audited paths (2026-11-10) are below; the list is part of the
   contract, and a new writer adds its row and its test.
4. **A bounded-staleness net in the field** (to be decided on measurement):
   a rolling audit compares a fixed number of rows a frame -- constant cost,
   so the invariant holds -- repairs any it finds stale and logs the path
   once. A missed writer then shows stale data for at most rows/K frames,
   not forever. At least for editor and debug builds.

Write paths into the render table, as audited:

| Path | Reaches the table through |
|---|---|
| Editor edits (move, resize, rotate, retexture, UV, tint, shader, property editor) | editor epoch + dirty objects (`EditorState.render_dirty_since`) → `refresh_rows` / `refresh_transforms`, in place or by reconcile |
| A drag in progress (dicts written for hundreds of frames after one checkpoint) | `edited` objects → `refresh_edited` (transform; geometry epoch → `refresh_rows`); `logic_render` journals the objects a drag leaves |
| Scene hierarchy (hide, show, rename), console commands, `main_window` | change journal (`scene_hierarchy`, `console_commands`, `main_window`) |
| Undo / redo | epoch moves with its dirty set, or a full reconcile |
| Load, new map, add / delete / duplicate | reconcile (`generation`); an unannounced row-set change caught by the row tuple |
| The other render buffer | `adopt`, plus `peer_dirty` re-resolution |
| Movers and doors in play | `MoverTable.publish` (direct column writes today) |
| Parked movers (Big World): asleep, I/O while asleep, catch-up on wake | `brush_placement.settle` + publication; visibility journal on unpark |
| I/O (`io_system`), entity links, portal links | change journal |
| Savegame restore | change journal (`savegame.py`) |
| Big World streaming: park, unpark, cell activation | change journal, `VISIBILITY` (`plugins/bigworld`) |
| Plugins (deathmatch teams, spectator, network, authoring) | change journal (entities; brushes wherever a plugin touches one) |
| Play start / stop, session reset | `logic_session` journals the reset rows; reconcile |
| Journal overflow | `refresh_rows` over every row |
| Physics, monsters, props, combat, timing | change journal (entities: the entity-table half, item 4) |
| Renderer side: renderer switch, context recreation, cleanup, two renderers in turn, texture loads, play/editor state (nodraw, triggers) | the renderer's caches dropped, or keyed on the state |
| What the visible set depends on (item 2): camera, cull distance, view filters, occlusion results, the hidden mask, bounds, portal views | each must move the visible-set version |

### The steps

0. **The contract first**, while the full comparisons still run: the write
   methods, read-only columns, the oracle mode, the per-path tests and the
   change-detection counters. Nothing reads a stamp yet, so the oracle
   compares the stamps against the comparisons it will replace.
1. **Transform change sets.** `MoverTable.publish` writes only the movers
   and doors whose pose changed since it last published (at rest, at a path
   end, parked: nothing). Every transform writer stamps the rows it wrote
   with the table's transform version. `SlotTransforms` reads the rows
   stamped since its own version instead of comparing the columns; the
   streams already follow its stamps. This closes M3's `MoverTable.publish`
   limitation and saves on the logic thread as well.
2. **Visible-set versions.** The cull publishes its visible rows with a
   version that stands while every input in the table's last row stands
   (and the same array with it). The passes' kept arrays and the streams
   key on that version, so a still frame's tests compare integers; the
   depth sort of the transparent, water and glass groups is kept while the
   camera stands. Re-culling when the camera moves is rendering work, not
   change detection; incremental, cell-by-cell culling for Big World is a
   separate saving, measured on its own.
3. **Uniform state by change.** Every frame each program used is sent the
   distance-fog and ambient block (seven calls, deliberately unconditional)
   and its shadow-map bindings, and the light set is keyed again
   (`_upload_lights_once`). Program uniforms persist in GL, so these can be
   sent when they change: a version from `ViewDistance`, the light table and
   the shadow pool, per program. Under cProfile they are ~17 % of a still
   BigWorld frame's `render_scene`.
4. **Entity instance streams.** Sprites, models and effects still repack
   their instance buffers every frame. Measure them first (MonsterTest and
   an entity-heavy map), then give them `BrushStream`'s treatment from the
   entity table's versions, under the same contract (its writers: physics,
   monsters, props, combat, timing, plugins).
5. **Camera motion.** Per-cell streams for Big World, drawn cell by cell,
   so a turn changes which cells are drawn instead of rewriting every row
   after the first changed item (~128 KB a frame now). On GL 3.3 that costs
   a draw and a vertex-array bind per cell; kept only if it beats the
   compacted rewrite on CPU.
6. **The target.** The instance-upload, frame and gameplay benchmarks on
   the Snapdragon 8cx Gen 3 / Adreno 690; `STREAM_GAP_ROWS`,
   `STREAM_MAX_RANGES`, `Player.MOVER_NEAR_FROM` and `LEG_COUNT_LIMIT`
   retuned there, keeping the fastest measured.

Order: 0, then 1 and 2, then 3; 4 and 5 after measuring; 6 as soon as a
device is available.

### Acceptance

* **No missed change:** every path in the table has its test and passes;
  the full suite passes with the oracle on; the columns are read-only
  outside the write methods.
* **The invariant:** on a still frame the change-detection counters are
  the same constant at 1×, 4× and 16× BigWorld's rows (target: zero rows
  examined), and O(k) for k changed rows; change-detection time reported
  separately and flat within noise.
* **As M3:** the GL oracles and pixel parity with a fresh renderer, zero
  instance bytes on a still world, no CPU regression in any benchmark
  scenario against M3.1, memory bounded, frames and gameplay gate no worse.
* **Total frame time** is reported at each world size and is expected to
  grow with what is visible; it is not held flat.
