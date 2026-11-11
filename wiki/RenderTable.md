> Applies to Fio 3.5.0.1111.

# RenderTable

`RenderTable` (`engine/render_table.py`) is the dense, numeric projection of the brush list that every renderer draws from. It has one row per brush, addressed by an integer **slot**, and one NumPy column per fact a renderer needs: the box and rotation, the live `hidden` flag, a classification word, per-face texture and UV state, colours, a handle to the brush's convex mesh record, and the water/glass/fog shader parameters. It stores nothing authored: every column is a resolution of something the brush dictionary already says, and a table rebuilt from `EditorState.brushes` holds the same values. The module imports no OpenGL.

Related pages: [EntityTable](https://github.com/ViciousSquid/Fio/wiki/EntityTable) (the entity half), [Renderer Technical Overview](https://github.com/ViciousSquid/Fio/wiki/Renderer-Technical-overview), [Renderer Development Guide](https://github.com/ViciousSquid/Fio/wiki/Renderer-Development-Guide), [Debug Tables](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables), [Cell Occlusion Culling](https://github.com/ViciousSquid/Fio/wiki/Cell-Occlusion-Culling).

## Ownership and threads

| Who | Thread | Access |
|---|---|---|
| `LogicRender.prepare_render_state` (`engine/logic_render.py`) | logic | calls `begin_frame` on the **write** buffer's table once per published frame, then `MoverTable.publish` in Play |
| `MoverTable.publish` (`engine/mover_table.py`) | logic | writes mover and door centres and spins through `write_transforms_many` |
| the active renderer, `QtGameView` | GUI | reads the **read** buffer's table while it holds the frame lease; writes nothing (`tests/renderer/test_renderer_boundary.py::test_the_renderer_writes_nothing_on_a_published_table`) |
| Debug Tables (`tools/debug_tables.py`) | GUI | copies the leased table's columns |

There are exactly two tables, one in each `RenderState` of `ThreadedGameState` (`engine/threaded_game_state.py`). The logic thread writes only the buffer the renderer is not reading; a swap exchanges the two. Each table is therefore one frame behind the other and catches up from change records, not by re-reading the world.

`render_table.py`, `entity_table.py`, `logic_render.py` and `threaded_game_state.py` may not import `engine.renderer` (`test_world_logic_and_editor_state_do_not_import_the_renderer`).

## Identity: slots, ids, generation

| Attribute | Type | Meaning |
|---|---|---|
| `count` | `int` | live rows; columns may be longer (capacity) — always slice `[:count]` or index with published slots |
| `generation` | `int` | incremented by every reconcile and every `adopt`. A slot is an address valid within one generation |
| `ids` | `list` | slot → `brush['id']` (the brush's stable UUID) |
| `slot_of_id` | `dict` | UUID → slot. For cache boundaries only; frame code indexes columns with slot arrays |
| `brushes` | `list` | slot → the live brush dict (a reference; never walked per frame) |
| `refs` | object `ndarray (count,)` | the same references as an array; published as `RenderState.render_refs` for the host, never handed to a renderer |
| `dynamic_slots` | `int32 ndarray` | slots with `CLASS_DYNAMIC` (movers and doors), recomputed whenever rows are re-resolved |

A renderer that keeps anything per slot keys it by the table's identity and `generation` (the forward renderer's kept matrices, pass memos, occlusion index and geometry handles all do).

## Columns

Declared once in `render_table._COLUMNS` as `(name, trailing shape, dtype, fill)`; every array's first axis is the slot. Capacity grows geometrically (`_resize`: `max(16, capacity * 2, n)`), copying the live prefix; rows are never appended one at a time.

### Warm columns (transform and visibility)

| Column | dtype | Shape | Meaning |
|---|---|---|---|
| `bounds` | `float64` | `(N, 6)` | `[centre x, y, z, half-extent x, y, z]` of the brush's placed box (`engine.brush_placement.placed_box`). One contiguous block so the frustum test is a single matrix product |
| `center` | — | `(N, 3)` | property: a view of `bounds[:, :3]` |
| `half` | — | `(N, 3)` | property: a view of `bounds[:, 3:]` |
| `rot` | `float32` | `(N, 4)` | `[axis x, y, z, angle in degrees]`; all zero for a row that does not turn. Set from `brush_placement.runtime_spin` (a rotating mover) |
| `hidden` | `bool` | `(N,)` | the live `brush['hidden']` flag, Big World parking included |

`bounds` and `rot` are the **sealed** columns: with `FIO_SEAL_COLUMNS` set (the test suite's `conftest.py` does) or in oracle mode they are read-only outside the table's own write methods, so a write that bypasses the change log raises. The seal is off in normal play (`render_table.SEAL_COLUMNS`).

`RenderTable.turning` (bool) becomes `True` the first time any row is given a non-zero `rot` angle and is never cleared; until then the logic-thread cull skips the turned-bounds computation.

### Cold columns (classification and material)

| Column | dtype | Shape | Meaning |
|---|---|---|---|
| `class_bits` | `uint16` | `(N,)` | classification word, below |
| `tex_name_id` | `int32` | `(N, 6)` | interned texture-name id per cube face, in `CUBE_FACE_KEYS` order; a face with no entry resolves to `default.png` (id 0); `TEX_NONE` (-1) is the column's fill |
| `uv_scale` | `float32` | `(N, 6, 2)` | authored `uv_scale[face]`, or `(1, 1)` when none |
| `uv_angle` | `float32` | `(N, 6)` | authored `uv_angle[face]`, default 0 |
| `uv_shift` | `float32` | `(N, 6, 2)` | authored `uv_shift[face]`, default `(0, 0)` |
| `uv_natural` | `bool` | `(N, 6)` | the face keeps a constant texel size (`brush_geometry.face_uses_natural_scale`); recomputed from the live extent when drawn |
| `uv_has_scale` | `bool` | `(N, 6)` | an explicit `uv_scale` was authored for the face |
| `colour` | `float32` | `(N, 3)` | flat colour 0..1: `brush['tint']` if set, else `brush['colour']`, else the default |
| `glow_colour` | `float32` | `(N, 3)` | Glow pass colour: `min(base * glow_intensity, 10.0)` per channel, `glow_intensity` default 10, base white when the brush has no colour |
| `geo_epoch` | `int64` | `(N,)` | the brush's geometry epoch when the row was resolved (`_geo_epoch`) |
| `geometry_id` | `int32` | `(N,)` | index into `geometry_records` for a convex row; -1 draws the shared unit cube |
| `water_tint` | `float32` | `(N, 3)` | `water_tint`, default `[0.0, 0.4, 0.6]` |
| `water_params` | `float32` | `(N, 7)` | `water_opacity` (0.5), `water_fresnel` or `water_reflectivity` (0.65), `water_wave_height` (0.5), `water_wave_enabled` (1.0), `water_distortion` (0.5), `water_refraction` (1.333), `water_roughness` (0.0) |
| `water_plane` | `bool` | `(N,)` | `water_plane`: draw the surface only |
| `water_wave_spread` | `float32` | `(N,)` | "Wave Spacing", `water_wave_spread` clamped to 0..1, default 1.0 (0 tight and choppy, 1 broad) |
| `water_high_quality` | `bool` | `(N,)` | the brush's "High quality" flag (`engine.constants.water_high_quality`) |
| `glass_color` | `float32` | `(N, 3)` | `glass_color`, default `[0.7, 0.85, 0.95]` |
| `glass_params` | `float32` | `(N, 5)` | `glass_opacity` (0.3), `glass_distortion` (0.5), `glass_refraction` (1.5), `glass_roughness` (0.0), `glass_fresnel` (0.5) |
| `fog_color` | `float32` | `(N, 3)` | `fog_color`, default `[0.5, 0.6, 0.7]` |
| `fog_params` | `float32` | `(N, 2)` | `fog_density` (0.01), `fog_noise_scale` (0.01) |

The nine water/glass/fog columns (`_SPECIAL_COLUMNS`) are zero for rows of no special class. Non-finite or non-numeric authored numbers fall back to the default (`_num`).

### `class_bits`

Resolved by `_brush_class_bits(brush)` once per edit.

| Bit | Value | Set when |
|---|---:|---|
| `CLASS_HIDDEN_AUTHORED` | `1 << 0` | `engine.spatial.authored_hidden(brush)` — what the mapper or an I/O Hide set, **not** the live `hidden` column (which also carries streaming parks) |
| `CLASS_WATER` | `1 << 1` | `engine.constants.is_water_brush(brush)` |
| `CLASS_FOG` | `1 << 2` | `is_fog` or `shader == 'Fog'` |
| `CLASS_GLASS` | `1 << 3` | `shader == 'Glass'` |
| `CLASS_GLOW` | `1 << 4` | `shader == 'Glow'` |
| `CLASS_TRIGGER` | `1 << 5` | `is_trigger` |
| `CLASS_SUBTRACT` | `1 << 6` | `operation == 'subtract'` |
| `CLASS_HAS_GEOMETRY` | `1 << 7` | `brush_geometry.brush_has_geometry(brush)`: a convex plane set, drawn from its own mesh |
| `CLASS_TEXTURED` | `1 << 8` | box: any face texture other than `default.png` / `caulk.jpg`; convex: `brush_faces.is_textured(brush)` |
| `CLASS_SHADOW_CASTER` | `1 << 9` | none of hidden-authored, trigger, fog, water, glass, glow, subtract |
| `CLASS_DYNAMIC` | `1 << 10` | `is_mover` or `is_door` |
| `CLASS_TEXTURE_TILING` | `1 << 11` | `texture_tiling`: faces without an explicit scale keep a constant texel size |

Water, fog, glass, glow and trigger are tested in that order and are mutually exclusive. `CLASS_NON_OPAQUE` is their union; a row with none of them is opaque.

### Texture names

Names are interned per table (`intern_texture`, `texture_names()`); ids are local to one table and stable for its life. Three are pre-interned so their ids are constants:

| Constant | Id | Name | Renderer rule |
|---|---:|---|---|
| `TEX_ID_DEFAULT` | 0 | `default.png` | — |
| `TEX_ID_SKIP` | 1 | `caulk.jpg` | face never drawn |
| `TEX_ID_NODRAW` | 2 | `nodraw.jpg` | drawn while editing, dropped in Play |
| `TEX_NONE` | -1 | — | no texture |

Only the logic thread interns. A renderer maps name ids to its own GL names on its thread (`TablesMixin._gl_texture_ids`), walking `texture_names()` once per new name.

`CUBE_FACE_KEYS = ('south', 'north', 'west', 'east', 'down', 'top')`: face *i* is the six vertices starting at `i * 6` of the shared cube; the same order as `engine.renderer.core.geometry._CUBE_FACE_KEYS`.

### Geometry records

A row with `CLASS_HAS_GEOMETRY` and a valid convex solid has `geometry_id >= 0`, an index into the dense list `geometry_records` (box rows have no record). Parallel to it, `geometry_serial` (`int64`) holds each record's `serial`.

`GeometryRecord` is immutable and shared between the two tables:

| Field | Meaning |
|---|---|
| `signature` | `brush_geometry.geometry_signature(brush)` |
| `convex` | the brush's local convex solid (`brush_geometry.local_convex`) |
| `origin`, `scale` | the anchor: centre and full size of the shape's own bounds (`convex.shape_box`). The mesh is expressed in that unit box; the row's transform places it |
| `materials` | `{id(face): engine.brush_faces.Material}` — one resolved material per face |
| `owner` | `brush['id']`, so two identical brushes do not share one mesh and one GPU transform slot |
| `serial` | a process-unique number; a consumer recognises a record it has seen wherever a reconcile moved its handle |
| `surface` | the hashable, exact input of the mesh (planes, materials, anchor, owner): the GPU mesh cache key |
| `natural_scale` | property: `{id(face): bool}` |

`geometry_version` moves only when `geometry_records` changes (a reconcile, a row refresh that replaced a record, an adopt that copies the peer's version). A brush that was recoloured, toggled or moved keeps its record (`GeometryRecord.describes`). A consumer checks the one integer per frame and, when it moved, compares `geometry_serial`.

## Bringing the table up to date

`begin_frame(brushes, epoch=None, dirty_objects=None, edited=(), peer=None, peer_dirty=None)` returns `hidden[:n]`. It never visits a brush that has not changed:

1. **Row set.** If `brushes` is an `engine.row_list.RowList` (the editor's brush list) at the version last seen, the rows are the same with no comparison. Otherwise the list is compared with `_row_tuple`, an identity test per row in C.
2. **Runtime journal.** `JOURNAL.drain(self)` returns `{id(obj): flags}` recorded since this table's last frame.
3. **Epoch or row set changed.** In order of preference:
   - `_refresh_in_place`: same rows, a precise dirty set → `refresh_rows` on just those slots (generation unchanged);
   - `adopt(peer)`: the other buffer's table already holds exactly these rows at this epoch (or is behind by the journalled `peer_dirty` set, which is then refreshed) → its columns are copied;
   - `_reconcile`: rebuild the slot mapping. A row *survives* (keeps its cold columns, moved to its new slot) when the brush at a slot is the same dict under the same id and not in the dirty set; other rows are resolved. All warm columns are re-read.
4. **Journal entries.** `STATE` → `refresh_rows`; `MOVED` or `VISIBILITY` only → `refresh_transforms`; `OVERFLOW` → `refresh_rows` on every row.
5. **`edited`** (the editor's selection outside Play): `refresh_edited` re-reads those rows' transforms every frame, and re-resolves any whose `_geo_epoch` moved — a drag writes the dicts in place for many frames after one undo checkpoint.

Movers and doors are not polled. In Play `MoverTable.publish(logic, table)` stores their positions (and a mover's spin) for the rows the tick moved; a group with at most `DENSE_SLACK_ROWS` (48) rows unmarked or at least `DENSE_SHARE` (0.75) marked is written whole.

| Method | Effect |
|---|---|
| `refresh_transforms(brushes, slots)` | re-read `bounds`, `rot`, `hidden` for `slots` |
| `refresh_rows(brushes, slots)` | re-resolve warm and cold columns; replaces a changed geometry record in place; falls back to a reconcile if a row gained or lost convex geometry |
| `write_transforms(slots, centre=None, rot=None, half=None)` | a writer outside the table storing runtime transforms (logged) |
| `write_transforms_many(writes)` | the mover publication: `(slots, centre, rot, index)` per group, each logged |
| `transform_index(slots)` | flat indices into `bounds`/`rot` for `write_transforms_many` |
| `adopt(peer)` | become a copy of `peer` (generation moves) |
| `sync(brushes, epoch=None, dirty_objects=None)` | bring the table up to date outside the frame loop (tests, tools); returns whether anything was refreshed |
| `needs_reconcile(brushes, epoch=None)` | whether the next `begin_frame` will rebuild the mapping |
| `shown()` | `(mask, slots)` of rows not hidden: the same read-only arrays until a `hidden` value or the row set changes |

Where the notifications come from:

| Change | Channel |
|---|---|
| editor transaction (create, delete, edit, undo) | `EditorState.mark_world_changed(objects)` moves the world epoch and journals the objects; `LogicRender` passes `render_dirty_since(table._epoch)` as `dirty_objects` |
| runtime property change (I/O inputs, console, save restore) | `engine.change_journal.touch(obj)` → `STATE` |
| streaming park/unpark | `touch(obj, VISIBILITY)` (via `engine.spatial.set_authored_flag` / parking helpers) |
| mover or door moved in a tick | `MoverTable.publish` |
| editor drag | the `edited` argument |

## Change records for readers

A reader never compares columns to learn what changed; the table says.

| Record | Moves when | Reader API | Used by |
|---|---|---|---|
| `generation` | reconcile, adopt | compare | everything keyed per slot |
| `transform_version` | any write to `bounds` / `rot` | `transforms_since(version)` → slot array (possibly repeated), empty when current, `None` when the log (`TRANSFORM_LOG` = 64 writes) does not reach back | `SlotTransforms` (kept matrices), `LogicRender.cull_brushes`, the distance cull |
| `row_change_serial` | each `refresh_transforms` / `refresh_rows` batch | `row_changes_since(serial)` → slot array, empty, or `None` (log of `ROW_CHANGE_LOG` = 128 batches, halved when full; cleared by a reconcile or adopt) | the forward renderer's occlusion cell index |
| `material_version` | a refresh actually changed a classification/material column; reconcile; adopt | compare | `TablesMixin._pass_memo` (pass groups, payloads, face batches) |
| `extent_version` | a half-extent actually changed; reconcile; adopt | compare | `_pass_memo` |
| `shown_version` | `shown()` recomputed | compare; or identity of the arrays `shown()` returns | the logic-thread cull |
| `geometry_version`, `geometry_serial` | see above | compare | `GeometryMixin._sync_geo_handles` |
| `rows_read` | per `begin_frame`: brush dicts read | read | Debug Tables ("rows read last frame") |

`None` from either `*_since` call means "treat every row as changed". Readers also check `generation`, because a reconcile renumbers slots.

`FIO_RENDER_ORACLE=1` (or `=record`) makes every site that trusts one of these records also perform the full comparison it replaced and report a `MissedChange` (`engine/change_detection.py`).

## Derived helpers

- `model_matrices(table, slots, out_model=None, out_normal=None)` → `(models (V, 16), normals (V, 9))`, float32, column-major: `M = T(centre) · R(axis, angle) · S(size)`; the normal matrix is `R · S⁻¹` (zero where an extent is zero). Unrotated rows are filled vectorised; rotated rows on top.
- `rotation_matrices` — re-exported from `engine.brush_placement`.

## Reading the table in a renderer

```python
table = config['render_table']
slots = brush_slots                      # int array published for this view
bits = table.class_bits[slots]
water = slots[(bits & render_table.CLASS_WATER) != 0]
models, normals = render_table.model_matrices(table, water)
tint = table.water_tint[water]           # (k, 3)
```

Rules: index columns with slot arrays; never translate ids per frame; never write a column or assign an attribute on the table; treat `brushes`, `refs` and `geometry_records[...].convex` as authored objects, read only when a GPU resource is built on a cache miss.

## Adding a column

1. Add `(name, trailing shape, dtype, fill)` to `_COLUMNS`. Growth, reconcile moves and `adopt` then cover it; it is a material column unless it is `bounds`, `rot` or `hidden`.
2. Resolve it in `_resolve_cold_rows` (or `_resolve_special_rows` for a water/glass/fog parameter, adding it to `_SPECIAL_COLUMNS`).
3. Whoever writes the authored value at runtime must `change_journal.touch` the brush; editor edits go through `mark_world_changed`.
4. Add it to the equivalence tests and check it in Debug Tables (**RenderTable** tab shows every column).

## Tests

| File | Covers |
|---|---|
| `tests/renderer/test_render_table.py` | resolution of every column, classification, interning, reconcile and survivors, adopt, geometry records |
| `tests/renderer/test_render_table_row_local.py` | row-local refreshes, the transform and row-change logs |
| `tests/renderer/test_render_dirty_journal.py`, `test_checkpoint_journal.py` | the editor's render-dirty journal and its replay for the second buffer |
| `tests/renderer/test_table_boundary_equivalence.py`, `tests/visual/test_render_projection_equivalence.py` | the table equals a rebuild from the same world; the picture is the same |
| `tests/renderer/test_slot_transforms.py` | kept matrices follow `transforms_since` |
| `tests/brush_model/test_mover_publication.py` | mover publication, both publication modes, alternating tables |
| `tests/e2e/test_change_records.py`, `tests/visual/test_change_detection_invariant.py` | every write path leaves a complete record; a still frame examines zero rows |
