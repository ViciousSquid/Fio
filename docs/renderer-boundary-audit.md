# "Tables all the way to GL": renderer compliance audit

Branch `3.1.0.0611_CANARY`, line numbers at `2cf304f`. The rule: during a
frame, code under `engine/renderer/` reads the published dense tables
(`RenderTable`, `EntityTable`, the frame input in `api.FRAME_INPUT`) and its
own GL caches -- not brush dictionaries, Things, model material
dictionaries or editor-state objects.

Method: a source inventory of the whole package (attribute and key reads,
followed into helpers outside it), each hit read in context; then the fixes
below, checked by the renderer, visual and end-to-end tiers with
`FIO_RENDER_ORACLE=record`. Debug Tables (`tools/debug_tables.py`) reads the
same published snapshot the renderer does and is the instrument for checking
any future column added for V2-V4.

## Summary

* `render_scene` and every brush, entity, sprite, light, shadow and portal
  pass are columnar. No pass reads `table.brushes`, `table.refs`,
  `etable.things`, `.properties` or tests a Thing class; there is no
  `self.editor` or `editor.state` anywhere in the package. Selection arrives
  as the frozen `SelectionOverlay`.
* Fixed this session (`a6c85a9`): model material dictionaries read every
  frame (V1); the selection outline's geometry-record read (V5); authored
  object arrays in the frame input (V6).
* Fixed in M5: the terrain (V4).
* Fixed after it: the face highlight (V2), the connection-line and
  collision overlays (V3), the occlusion index's state on the published
  table (R1). **No violation found by this audit remains open.** What the
  renderer still reads of authored shape is the resource builders listed
  under "Necessary GPU resource management".

Files with no authored-world access: `core/visibility.py`,
`core/light_stream.py`, `core/slot_transforms.py`, `core/uniform_rows.py`,
`core/geo_store.py`, `core/base.py`, `core/diagnostics.py`,
`forward/instancing.py`, `forward/portals.py`, `forward/distance.py`,
`forward/lighting.py` (EntityTable columns and model GL handles only),
`api.py`, `registry.py`.

## A. Architectural violations

### V1. Model passes read MTL/GLB material dictionaries per frame -- FIXED

Was: `forward/passes.py` `_model_groups` (a sort over `obj.groups` with a
`obj.materials[...]` lookup per group), `draw_models_instanced` and
`_draw_dense_model_single` (`material.get('texture' | 'color' | 'alpha')`,
`group['start' | 'count' | 'indexed']`) -- per group, per model run, per
view, per frame; and `core/resources.py` `_model_texture_id`, which resolved
the texture path (`material.get('mtl_dir')`, two `asset_exists` probes)
*before* asking its cache.

Now: `core/resources.py:657` `_model_draw_groups(obj)` resolves a model's
groups once, on its first draw, into `ModelDrawGroup` records (range,
indexed, alpha, colour, GL texture name), opaque first;
`_manual_model_texture_id` (`:694`) does the same for a row's manual
texture. The passes read only those. Per-entity data was already published
(`EntityTable.model_recipe_id`, `model_recipes()`).

### V2. `draw_face_highlight` took a live brush dictionary -- FIXED

Was: `core/overlays.py` read `brush['pos']`, `brush['size']` and called
`brush_geometry.get_convex(brush)`, which writes `brush['_world_cache']` --
the paint path mutating the authored dictionary. Now the host builds the
face's world-space triangles (`engine/overlay_geometry.py`
`face_highlight_triangles`, the same arithmetic, moved) and the contract is
`draw_face_highlight(projection, view, triangles)`. `overlays.py` no longer
imports `brush_geometry`.

### V3. Editor/debug overlays fed dictionaries -- FIXED

* `draw_connection_lines(projection, view, segments, colours)`: float32
  `(n, 2, 3)` and `(n, 3)`. The host's records still alias the live
  position lists of brushes and Things (that is what keeps a dragged
  object's line current, given the host's coarse cache key); they are read
  into arrays by the host each frame (`connection_segments`), not by the
  renderer.
* `draw_collision_visualization(projection, view, vertices, runs)`: one
  GL_LINES vertex array and `(first, count, colour)` runs in the records'
  order (`collision_wireframes`). It also no longer creates and deletes a
  vertex array and buffer per collision shape per frame: every loose-line
  overlay shares one dynamic buffer.

### V4. The terrain -- FIXED (M5)

Was: the frame input's `terrain` was the editor's live
`engine.terrain.Terrain`. Every frame the renderer read `terrain.enabled`,
**wrote** `terrain.shader_program`, `terrain.uniforms[...]` and the four
ground-texture names on it, and called `terrain.update_and_render`, which
streamed, scheduled and generated chunks and owned every GL object, on the
paint thread.

Now:

* `Terrain.publish()` returns a `TerrainFrame` (`engine/terrain_frame.py`,
  GL-free, immutable): the dense `TerrainTable`, the per-slot products
  (`TerrainProducts`: grass instances and block-terrace meshes, each with a
  version and the slot's allocation order), the paint maps and their
  versions behind read-only views, and that frame's appearance as numbers
  (uniform values, gradient, grass settings, flags). Nothing in it leads
  back to the Terrain.
* `Terrain.advance(camera, planes)` does the CPU upkeep before the frame:
  streaming, pruning, LOD hysteresis, and building what is due. No GL:
  `engine/terrain.py` no longer imports OpenGL.
* `engine/renderer/core/terrain_gl.py` `TerrainGL` owns the GL side:
  programs, height and paint texture pages, index arrays, grass and block
  buffers, the per-slot versions it has uploaded. The forward renderer
  keeps one under the frame's key (`passes.py` `_terrain_gl`) and frees it
  when the key changes or the renderer is retired. A slot freed or reused
  is recognised by its allocation `order` in the table.
* The host (`QtGameView._publish_terrain`) advances and publishes per view,
  and copies the renderer's terrain counters (`RenderStats.terrain_*`) back
  onto the terrain for SysMon, the terrain editor and Debug Tables.
* Height grids can be built on a `TerrainBuilder` thread
  (`Terrain.BACKGROUND_BUILDS`, on in `main.py`): it computes a grid from
  numbers captured when the build was asked for; the table is still written
  on the GUI thread, so table writes stay on one thread.

Left on the GUI thread, inside `advance` and budgeted as before: grass
blade generation and block-mesh building. The draw code moved unchanged.

### V5. Selection outline read the geometry record -- FIXED

`core/overlays.py:202-213` now takes a convex brush's mesh from
`_geo_handles(table).meshes[gid]`, the handle array the brush passes
resolve; before it fetched `table.geometry_records[gid]` and hashed the
record's whole surface each frame a brush was selected.

### V6. Authored object arrays in the frame input -- FIXED

`render_refs` (brush dictionaries) and `entity_refs` (Things) were put in
the renderer's `config` every frame; nothing read them. Removed from the
frame input; they remain on `RenderState` for the host.

### Necessary GPU resource management (not violations)

Cache-miss only, keyed by a published name or handle: `load_model`
(`core/resources.py`), `_model_draw_groups` (V1's builder),
`_gl_texture_ids` / `_sprite_gl_ids` / `_model_recipe_radii`
(`core/tables.py`), `ensure_effect_frames`, and the convex mesh build --
`tessellate_geo` (`core/geometry.py:91`, reads `record.convex`),
`_resolve_face_run` (`:478`, `record.materials`), `_sync_geo_handles`
(`:316`), which run only when `table.geometry_version` has moved.
`record.convex` is the same solid the authored brush caches, so this is the
one place render code walks authored-shaped faces; moving tessellation to
the logic thread would remove it at the price of logic-thread time, and is
not recommended without a measured reason.

## B. Performance issues

* V1's path resolution: two file probes per textured model group per frame.
  The whole model pass measured 0.22-0.24 ms a frame on `_SHOWCASE` before
  and after the fix -- no measurable gain, no regression.
* V5: a tuple hash of every plane of the selected brush per frame (editor
  only); now a list index. Not measured separately.
* `draw_collision_visualization` creates and deletes a VAO and VBO per
  brush per frame (`core/overlays.py:789-803`). Debug only.
* V4 (fixed): the terrain was 70 of 84 ms of BigWorld's frame under
  llvmpipe and generated chunks on the paint thread. See the performance
  audit, F3, and the handoff's M5 section for the numbers after.

## C. Correctness risks

* **R1 -- FIXED.** `forward/occlusion.py` wrote its index and a dirty set
  onto the published `RenderTable` (`_occlusion_index`, `_occlusion_dirty`),
  and cleared a set the publisher added to. (With double-buffered
  publication the logic thread writes only the buffer the renderer is not
  reading, so no lost update was reproduced; the fault was the renderer
  owning state on published data.) Now the index is the renderer's, held
  under the table's identity and generation, and the table reports which
  rows it refreshed since a serial (`RenderTable.row_changes_since`, a
  bounded log of batches -- one array copy per refresh call, no per-row
  Python, which also replaces a per-slot generator on the logic thread).
  No attribute of a table is assigned anywhere in the package (ratchet).
* V3 (remaining, host side): the I/O link records are cached under
  `(len(brushes), len(things), filter version)`; a link added or removed
  without changing those counts shows a frame late at most until one does.
  Not a renderer matter any more.
* V1 after the fix: a model's texture is resolved once. A texture file
  added for an already loaded model appears when the model is reloaded, not
  on the next frame as before.

## Tests

`tests/renderer/test_renderer_boundary.py` gained an attribute and key
ratchet over the package: `materials`, `groups`, `convex`,
`geometry_records`, `brushes`, `things`, `properties`, `editor`, `mtl_dir`
may be read only in the listed resource builders (the list must shrink when
one stops); the model passes contain no dictionary read; the host puts no
`render_refs` / `entity_refs` in the frame input; the package imports no
`engine.terrain`, calls nothing the Terrain had and writes no attribute on
the terrain it is handed; `engine/terrain.py` imports no OpenGL; the three
overlays take arrays and read no brush or record key; no attribute of a
published table is assigned in the package.
