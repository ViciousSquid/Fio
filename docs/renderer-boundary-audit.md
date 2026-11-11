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
* Remaining: the face highlight (V2), two editor/debug overlays (V3), and
  one shared-state write (R1). Recommendations below.

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

### V2. `draw_face_highlight` takes a live brush dictionary -- OPEN

`core/overlays.py:360-420`, `_geo_face_highlight_verts` `:422-445`; called
from `qt_game_view.py:2101` each frame a face is hovered in face mode
(editor only). Reads `brush['pos']`, `brush['size']` (`:385`),
`brush_geometry.brush_has_geometry(brush)` (`:375`) and
`brush_geometry.get_convex(brush)` (`:425`), which **writes**
`brush['_world_cache']` (`engine/brush_geometry.py:1023`) -- the paint path
mutating the authored dictionary. The protocol sanctions the argument
(`api.py`, "a brush dict in the world format").

Minimal fix: the host passes `(brush_id, face name)`; the renderer takes the
box from `RenderTable` (`slot_of_id` -> `bounds`, `rot`) or the convex face's
range from the geometry handles it already holds (`_geo_handles(table)`).
Alternative: the editor builds a float32 triangle array on hover change.
Either changes one protocol member (`api.py`, `tests/helpers/renderers.py`,
`test_renderer_contract.py`; check
`tests/integration/test_backport_boundaries.py`, which names these
functions). No effect on play; pixel parity is checkable with the existing
face-highlight tests. Not done here: a protocol change deserves its own
review.

### V3. Editor/debug overlays fed dictionaries -- OPEN, low priority

* `draw_connection_lines` (`core/overlays.py:704-740`): host-built dicts
  whose `src`/`dst` **alias the authored `brush['pos']` / `thing.pos` lists**
  (`qt_game_view.py:1559-1606`). Editor only, with logic links shown. Fix:
  the host builds a float32 `(n, 2, 3)` array and colours. Note the host's
  cache key is only `(len(brushes), len(things), filter version)`: today the
  aliasing is what keeps a dragged object's line current, so the copy must
  come with a real invalidation (the state's `world_epoch`).
* `draw_collision_visualization` (`core/overlays.py:742-805`): reads
  `brush.get('_model_collision' | '_collision_mode' | 'pos' | 'size' |
  '_mesh_triangles')` from the collision runtime's brush-shaped dicts
  (`qt_game_view.py:2078`). Debug only. Fix: the collision runtime publishes
  one line-vertex array when it builds.

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

* **R1.** `forward/occlusion.py:382-390` writes private state on the
  published `RenderTable` (`_occlusion_index`, `_occlusion_dirty`) and
  clears a set the logic thread adds to (`engine/render_table.py`). The
  renderer mutating published data, and a set shared across threads without
  a lock: a row marked dirty between the renderer's `update` and `clear` is
  lost, leaving a stale occluder until its next move. Play only, with
  occlusion on. Fix: keep the index renderer-side, keyed by table identity
  like `_geo_by_table`, and take changed rows from the table's change log
  (`transforms_since`). Not reproduced as a visible fault; not changed here.
* V2: the paint path writes `brush['_world_cache']` on the authored dict.
  The GUI thread owns the dict in the editor, so no race today.
* V3: connection lines read authored position lists the editor may be
  mutating in the same thread; benign, but tied to the weak cache key.
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
the terrain it is handed; `engine/terrain.py` imports no OpenGL. It does not yet catch
`brush['pos']`-style reads in the overlays (V2, V3): add those keys when
they are fixed.
