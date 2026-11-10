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

Commits on `3.1.0.0611_CANARY`: the benchmark tool
(`tools/bench_instance_uploads.py`), the slot-transform cache, the
per-pass instance streams, and this record.

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
reconciles. GL renderer and visual tiers: all pass but the three
`test_portal_fade` cases, which fail the same way on the accepted baseline.
