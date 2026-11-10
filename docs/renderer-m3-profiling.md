# Renderer M3: change-driven box instance uploads

Status: **implemented** for box brush instances (the lit, textured and shadow
passes): see *M3 as built* below. The profiling that designed it follows,
unchanged, as it was recorded before any renderer change.

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
