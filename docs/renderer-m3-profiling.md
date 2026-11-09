# Renderer M3: dirty-range instance uploads — profiling first

Status: **profiling only.** No renderer change has been made for M3. This
records what the renderer uploads per frame and where its CPU goes today, so
that M3 is designed against measurements rather than added as another
rendering layer.

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
