> Applies to Fio 3.1.0.0611 (CANARY).

# Cell Occlusion Culling

Cell occlusion culling is the GPU stage of `ForwardRenderer`'s brush visibility pipeline. The frustum cull answers "is it in the view pyramid", the distance cull answers "is it in range"; neither knows that a wall stands in front. This stage asks the GPU one question per 512-unit **cell group** — *did any pixel of this cell's bounding box pass the depth test?* — with asynchronous OpenGL occlusion queries, and drops every brush of a cell whose box has been verified hidden, before the brush passes classify, sort, pack and submit them.

It runs in **Play mode, main view only**, changes no pixel, never blocks on the GPU, and fails open: whenever an answer is missing, late, stale or doubtful, the brushes are drawn.

| | |
|---|---|
| GPU half | `engine/renderer/forward/occlusion.py` — `OcclusionMixin` (mixed into `ForwardRenderer`) |
| GL-free half | `engine/cell_cull.py` — `group_rows_by_cell`, `CellGroupIndex`, `select_probe_groups`, `OcclusionProbeBook`, `ProbeGovernor` |
| Change record | `engine/render_table.py` — `RenderTable.row_changes_since(serial)` / `row_change_serial`: the rows the table refreshed since the renderer last looked |
| Thread | The GUI thread that owns the GL context (inside `ForwardRenderer.render_scene`). The logic thread only logs the rows it refreshes; the renderer writes nothing on the published table. |
| Setting | `[Renderer] occlusion_culling` in `settings.ini` (default `True`) |
| Scope | Built-in `"Forward"` renderer only. It is not part of the `engine.renderer.Renderer` contract; another renderer sees only the `splitscreen_active` frame-input key. |

Related pages: [Renderer Technical overview](https://github.com/ViciousSquid/Fio/wiki/Renderer-Technical-overview), [RenderTable](https://github.com/ViciousSquid/Fio/wiki/RenderTable), [Debug Tables](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables), [Spatial Grid](https://github.com/ViciousSquid/Fio/wiki/Spatial-Grid).

---

## Where it sits in the frame

`ForwardRenderer.render_scene` calls the mixin twice:

```text
brush_slots (logic thread's frustum result)
   -> _distance_cull_slots            XZ distance, play mode only
   -> _occlusion_filter_slots         HOOK 1: drop cells verified hidden
   -> _classify_brush_slots           opaque / textured / solid / water / glass / fog / glow / trigger
   -> shadow maps, terrain, portals, opaque brushes, models, sprites, water, glass, fog
   -> _occlusion_end_of_frame         HOOK 2: poll last frames' queries, issue this frame's probes
   -> selection overlays
```

Hook 1 runs before pass classification, so every brush pass (opaque, glow, water, glass, fog, trigger) receives the same narrowed slot list. Hook 2 runs after the fog volumes, when the frame's depth buffer is complete, and before `render_scene` disables the depth test for the selection overlays.

With the feature off, unavailable or inactive, both hooks return without touching the slots or issuing GL calls (hook 2 still polls and recycles any outstanding queries while the probe pass is ready).

The narrowed slot array hook 1 returns is read-only and is handed back as the **same object** while its inputs stand — the same visible-set array from the distance cull, the same `(table.generation, table.transform_version)` and the same set of culled groups (kept per view in `_occlusion_kept`). The passes and instance streams downstream recognise an unchanged visible set by identity, so a still frame with culling active re-derives nothing.

---

## When it runs

`OcclusionMixin._occlusion_active(config)` is true only when all of these hold:

| Condition | Source |
|---|---|
| `renderer.occlusion_culling` is true | `[Renderer] occlusion_culling`, or set on the instance |
| the probe pass initialised (`_occlusion_ready`) | program compiled, VBO/VAO made, a query target accepted |
| `config['play_mode']` is true | frame input |
| `config['splitscreen_active']` is false | frame input; published by `LogicRender` when player 2 exists and by `QtGameView` when split-screen is on |

Hook 1 additionally returns the slots untouched when:

- `camera_pos` is `None` or the table has no `bounds`;
- fewer than `MIN_PROBED_ROWS` slots survived the distance cull (512; 1024 on low-power ARM), or fewer than that many of them are *eligible* (see below);
- the governor's budget is 0 (parked);
- the camera jumped or turned sharply since the previous frame (see [View changes](#view-changes)).

What is never reached:

| Path | Why |
|---|---|
| Editor camera | `play_mode` is false. The editor always draws every brush the frustum keeps. |
| Portal virtual views | `PortalsMixin._draw_portal_scene` builds its own slot list from `all_brush_slots` and calls the passes directly; neither hook is called. |
| Shadow cube-maps | `render_shadow_maps` selects casters from `all_brush_slots`; a brush hidden from the camera still casts. |
| Split-screen | Two views would share one probe history; `splitscreen_active` disables it for both. |
| Other renderers | The hooks are `ForwardRenderer`'s own. |

---

## Cell groups

A cell group is the set of *eligible* brush rows whose **centre** falls in one 512-unit XZ cell, plus the axis-aligned union box of those rows.

- The cell of a row is `engine.spatial.cells_of_points(centre_x, centre_z)` with `engine.spatial.CELL_SIZE = 512.0` — the same `floor(coord / 512)` convention as the collision/streaming grid. The group key is `(cell_x, cell_z)`; Y is not part of the key.
- A row belongs to exactly one group (its centre's cell). This is deliberately narrower than the streaming cell index, which files a brush under every cell its footprint touches.
- The union box is the min/max over each member's `centre ± half`, stored as `(cx, cy, cz, hx, hy, hz)` float64. Because a brush only has to be *centred* in the cell, the box can extend beyond the cell.

`engine.cell_cull.group_rows_by_cell(bounds, cell_size=512.0)` is the reference implementation: one stable argsort over an int64 key (`cell_x * 2**32 + cell_z`) and two `reduceat` calls. It returns `(group_bounds (M,6), group_of_row (N,), group_cells (M,2))`.

### Eligibility

Only rows that satisfy `CellGroupIndex._eligible` are grouped:

| Requirement | Column | Reason |
|---|---|---|
| not hidden | `RenderTable.hidden` | hidden rows are not drawn |
| unrotated | `RenderTable.rot[:, 3] == 0` | `bounds` is the unrotated box; a rotated brush can reach outside it |
| a plain box | `RenderTable.geometry_id < 0` | convex (angled) brushes are not culled by their box |

Ineligible rows get group `-1`. They are never culled and never counted towards a group; they pass through hook 1 unchanged.

### Padding

Every union box is padded outwards before use:

```python
pad = 8.0 * np.finfo(np.float32).eps * np.maximum(1.0, np.maximum(abs(group_min), abs(group_max)))
half = (group_max - group_min) * 0.5 + pad
```

The table is float64 but the probe's model matrix, the cube vertices and the GPU transform are float32, so centre + half can round inwards. The pad (about `9.5e-7 × max(1, |coordinate|)` per axis) guarantees the drawn probe box contains the union box.

---

## The cached index: `CellGroupIndex`

Grouping is not redone per frame. The **renderer** keeps one `CellGroupIndex` per published `RenderTable`, in `OcclusionMixin._occlusion_by_table`: `id(table) -> [weakref(table), table.generation, index, row_change_serial last applied]`. Nothing is stored on the table (`tests/renderer/test_renderer_boundary.py::test_the_renderer_writes_nothing_on_a_published_table`). An index is built the first time its table reaches hook 1 with the feature active, so the editor (where the feature never runs) pays nothing; entries whose table has been collected are dropped when a new index is made.

| Field | Content |
|---|---|
| `slot_group` | `int32[count]`: group id per slot, `-1` if ineligible |
| `coord_ids` | `{(cell_x, cell_z): group id}` |
| `cells` | group id -> `(cell_x, cell_z)` |
| `member_sets` | group id -> `set` of slots |
| `boxes` | `float64[groups, 6]` padded union boxes |
| `signatures` | group id -> `(box tuple, 16-byte BLAKE2b digest)` or `None` for an emptied group |
| `rebuilds`, `changed_groups` | counters |

There are two render buffers ([double-buffered publication](https://github.com/ViciousSquid/Fio/wiki/Renderer-Technical-overview)), each with its own `RenderTable`, so there are two indexes; each is created the first time its table is drawn in Play.

### Lifecycle

| Event | Code | Effect |
|---|---|---|
| First occlusion frame on a table, or its `generation` moved (a reconcile or an `adopt`) | `occlusion.py: _occlusion_filter_slots_impl` | a new `CellGroupIndex(table)`; the entry records `table.row_change_serial`; `renderer._occlusion_index_rebuilds += 1` |
| Journalled move, hide/show, park/unpark | `RenderTable.refresh_transforms` | the slots are logged as one batch (`_note_row_changes`); `row_change_serial` moves |
| Journalled semantic change (material, class, shape) | `RenderTable.refresh_rows` | as above |
| Editor drag of the selection | `RenderTable.refresh_edited` -> `refresh_transforms` / `refresh_rows` | as above |
| Next frame that draws this table | hook 1 | `changed = table.row_changes_since(serial)`: an empty array does nothing, an array of slots goes to `index.update(table, changed)`, `None` (the log of `ROW_CHANGE_LOG` = 128 batches no longer reaches back) goes to `index.rebuild(table)`; the entry then records the table's current serial |
| Mover or door moved by the tick | `MoverTable.publish` -> `RenderTable.write_transforms_many` | logged in the **transform** log only (`transforms_since`), not in the row-change log: the index is not updated (see [Conservativeness](#conservativeness)) |

`CellGroupIndex.update(table, dirty)`:

1. Ignores slots outside `0 <= slot < table.count`.
2. Falls back to a full `rebuild` when `len(slot_group) != table.count` or `len(dirty) > max(128, table.count // 8)`.
3. Otherwise, for each dirty slot, re-evaluates eligibility and cell, moves it between `member_sets`, creates a new group if the cell was unoccupied, and marks the old and new groups *touched*.
4. For each touched group recomputes the padded union box from its members and a fresh signature. A group left empty gets a zero box and signature `None`.

`CellGroupIndex.visible(slots)` is the per-frame call: gather `slot_group[slots]`, `np.bincount` the group ids, and return, for the groups present among the frame's slots, `(eligible mask, boxes, group_of_row, cells (M,2) int64, counts, signatures)` — or `None` when no slot is eligible. No sort and no AABB arithmetic run per frame. The union box is always the box of **all** eligible members of the cell, including members this frame's frustum rejected, so it is at least as large as the box of the visible members.

### Signatures and invalidating old answers

A signature is `(box tuple, blake2b(sorted member slots, bounds[members], class_bits[members], geo_epoch[members]))`. It changes when any member moves, resizes, joins, leaves, changes class or changes shape, even if the union box itself stays the same. It does not depend on which buffer's table computed it, so identical content in the two publication buffers produces the same signature.

Each frame hook 1 compares every active cell's signature with the one stored in `renderer._occlusion_box_by_cell` from the previous frame. A cell whose signature differs is passed to `OcclusionProbeBook.forget(coord)`: its hidden streak and last result are dropped, so it is drawn until fresh probes prove it hidden again. Each issued probe also records the signature it was issued for; a result whose signature no longer matches the cell is discarded when polled.

---

## Probes and queries

### GL resources

Created in `OcclusionMixin._init_occlusion(config)`, called at the end of `ForwardRenderer.__init__`:

| Resource | Where it is kept | Freed by |
|---|---|---|
| probe program (`uProjView`, `uModel`; fragment writes `vec4(0.0)`) | `shaders['occlusion_probe']` | `RendererCore.cleanup` |
| VAO, attribute 0 = `vec3` position | `vaos['occlusion_probe']` | `RendererCore.cleanup` |
| VBO, 36 vertices of a unit cube centred on the origin | `_occlusion_vbo` | `_cleanup_occlusion` |
| query objects | `_occlusion_probes` (outstanding, by cell) and `_occlusion_query_pool` (free) | `_cleanup_occlusion` |

Query objects are created lazily with `glGenQueries` when the pool is empty and are returned to the pool when their result is read. `_cleanup_occlusion` is the first step of `ForwardRenderer.cleanup` and copes with a partly initialised instance.

If any step of initialisation raises, or the program does not link, or no query target is accepted, `_occlusion_ready` stays `False`, `[Renderer] occlusion culling unavailable: ...` is printed, and the renderer runs without the feature.

### Query target

`OcclusionMixin._pick_query_target` asks the live context rather than assuming. Candidates, strongest first:

1. `GL_ANY_SAMPLES_PASSED_CONSERVATIVE` (desktop OpenGL 4.3 / `ARB_ES3_compatibility`; a 3.3 core context rejects it with `GL_INVALID_ENUM`)
2. `GL_ANY_SAMPLES_PASSED` (OpenGL 3.3 core)
3. `GL_SAMPLES_PASSED` (OpenGL 1.5)

For each, pending GL errors are drained, an empty query is begun and ended, and `glGetError` is read; the first target that leaves `GL_NO_ERROR` is used for the life of the renderer. A constant missing from the PyOpenGL binding is skipped. `None` disables the feature. The feature keys only on *zero samples*, which all three targets answer exactly; the conservative target may report a box visible when it barely failed, which is the fail-open direction.

### Issuing probes (hook 2)

After polling (below), `_occlusion_end_of_frame_impl` issues this frame's probes if the feature is active and hook 1 produced groups this frame:

1. `cell_cull.select_probe_groups(group_of_row, near_sq, budget, near_exempt_sq=1024**2, min_group_rows=..., counts=...)` picks candidates:
   - a group is a candidate only if its box is at least 1024 units from the eye (`near_sq >= NEAR_EXEMPT_SQ`; `near_sq` is the squared 3D distance from the eye to the box, 0 inside it) **and** it has at least `MIN_PROBE_GROUP_ROWS` of this frame's slots (16; 32 on low-power ARM);
   - candidates are ordered by row count descending, then by distance ascending;
   - at most `budget` are returned (the governor's current budget).
2. Nothing is issued when the selection is empty or the number of outstanding queries already equals `occlusion_governor.max_probes`.
3. Cells that already have an outstanding query are removed from the selection (one outstanding query per cell). They are not replaced by lower-ranked groups: selection is by rank, so the same top-`budget` groups are chosen each frame while the view holds.
4. If anything remains, the probe pass binds its program and state once and, per cell: takes a query from the pool, `glBeginQuery(target)`, uploads `uModel = translate(centre) * scale(2 * half)` (`_probe_model_matrix`), `glDrawArrays(GL_TRIANGLES, 0, 36)`, `glEndQuery(target)`. The loop also stops when outstanding queries reach `max_probes`.
5. For each probe it stores `(signature, view epoch, issued frame, eye position)` in `_occlusion_probe_meta`.

Probe state: blend off, stencil test off, face culling off (both faces of the cube are rasterised, so the eye inside or outside the box and whatever cull state a brush pass left do not matter), depth test on with `GL_LESS`, depth writes off, colour writes off. Afterwards the pass unbinds the VAO, restores the colour and depth masks and unbinds the program. Probe draws are **not** counted in `RenderStats.draw_calls`.

The probes are tested against whatever wrote depth this frame: opaque and glow brushes, terrain, opaque models, and each portal aperture (whose depth `_portal_flatten_depth` resets to the aperture plane). What `render_scene` draws after it turns depth writes off — fading models, projected decals, sprites, trigger volumes, water, glass and fog — never hides a cell. Effects are the one exception: in a frame that has high-quality water, `_write_effect_depth` writes the texels of each effect that are at least 50 % opaque into the depth buffer, and those can occlude a probe.

### Polling results (hook 2, start)

Every frame the probe pass is ready, hook 2 first increments the renderer's frame counter and polls each outstanding query with `GL_QUERY_RESULT_AVAILABLE`. A query that is not ready is left alone — `GL_QUERY_RESULT` is only read once the driver reports it available, so no frame waits on the GPU. A ready query is read, its object returned to the pool, and its answer **discarded** unless all of these hold:

| Check | Meaning |
|---|---|
| the cell is still among this frame's groups | the camera can still see that cell |
| stored signature == the cell's current signature | the cell's content has not changed |
| stored view epoch == current view epoch | no jump/turn invalidation since it was issued |
| `frame - issued_frame <= MAX_RESULT_AGE` (2) | the driver did not answer too late |
| squared distance between the eye now and the eye at issue `<= 32**2` | the camera has not drifted more than 32 units |

An accepted answer is recorded with `occlusion_book.record(coord, samples, issued_frame)`. Then `occlusion_book.retire(candidates)` drops every cell that is not in this frame's groups; when the feature did not run this frame (not in Play, split-screen, too few rows, parked, a jump/turn) the candidate set is empty and the whole book is cleared.

### `OcclusionProbeBook`

Plain per-cell integers, no GL (`engine/cell_cull.py`):

| Method | Rule |
|---|---|
| `record(coord, samples, frame)` | `samples > 0` clears the cell's streak; `samples == 0` increments it. Stores `frame` as the cell's last result. |
| `culled(coord, frame)` | `True` only if streak `>= OCCLUDE_AFTER` (2) **and** `frame - last_result <= MAX_RESULT_AGE` (2). Unknown cells read `False`. |
| `forget(coord)` | drops streak and last result for one cell |
| `retire(coords)` | drops every cell not in `coords` |

Consequences:

- A cell is skipped only after **two consecutive completed** zero-sample probes. A pending probe decides nothing.
- One visible answer clears the streak outright.
- A culled cell keeps being probed (its group is still in the frame's candidate set); it stays culled only while fresh zero-sample answers keep arriving. If its answers stop — the budget shrank, the governor parked, the query came back late — it is drawn again within `MAX_RESULT_AGE` frames.

### Latency

With a driver that answers a query by the end of the next frame:

| Frame | Hook 1 | Hook 2 |
|---|---|---|
| N | cell drawn | probe 1 issued |
| N+1 | cell drawn | probe 1 read (streak 1); probe 2 issued |
| N+2 | cell drawn | probe 2 read (streak 2); probe 3 issued |
| N+3 | **cell skipped** | probe 3 read; probe 4 issued |

So a cell first seen (or first eligible) is drawn for at least three frames before it can be skipped. A slower driver lengthens this; an answer more than two frames late is discarded and culling does not start.

### View changes

Hook 1 compares the eye position and the view's forward vector (`-view[i][2]`) with the previous frame's:

| Trigger | Threshold |
|---|---|
| jump | eye moved more than 32 world units in one frame |
| turn | dot of the two forward vectors `< MAX_TURN_COS` (0.8, about 37 degrees) |

On either, the view epoch is incremented, the whole book is retired (`book.retire(())`), and this frame's slots are returned unculled. Probes outstanding from the old view are discarded when they return (epoch mismatch). Probes are still issued at the end of that frame for the new view, so culling can resume three frames later. A teleport, a level start, a portal transit and a camera cut are all covered by the jump rule; the first Play frame has no previous eye and an empty book, so nothing is culled.

---

## What is and is not culled

| Content | Culled? |
|---|---|
| Box brushes, unrotated, not hidden, in a cell at least 1024 units away with a hidden streak | Yes — every class: opaque, textured, glow, water, glass, fog |
| Brushes in a cell whose box is within 1024 units of the eye (`NEAR_EXEMPT_SQ`) | Never |
| Rotated brushes (`rot` angle != 0), convex/angled brushes (`geometry_id >= 0`) | Never (ineligible) |
| Hidden / parked brushes | Not drawn anyway; not grouped |
| Cells with fewer than 16 (32 low-power) of the frame's slots | Never probed, so never culled |
| Cells beyond the governor's budget rank | Never probed, so never culled |
| Entities: sprites, models, effects, lights, path nodes | No. Entity passes use their own distance and frustum culls. Lights keep lighting. |
| Terrain | No. Terrain chunks are frustum-culled by `TerrainGL` (`engine/renderer/core/terrain_gl.py`). |
| Portal virtual scenes, shadow casters | No (see [When it runs](#when-it-runs)) |
| Anything in the editor | No |

Transparent brushes (water, glass, fog) are *subject to* culling like any eligible brush — hook 1 runs before classification — but they never *cause* it, because they do not write depth.

### Conservativeness

The argument for correctness: a group's padded union box contains every member box; if no fragment of the box passes `GL_LESS` against the frame's depth, no fragment of any member would. The mechanisms that keep the answer valid across frames are the two-probe streak, the two-frame result age, signature invalidation, the view epoch, the 32-unit eye drift check and the 1024-unit near exemption. The failure direction of each is a wasted draw.

**Known limitation — movers and doors.** In Play, movers and doors are positioned by `MoverTable.publish` (`engine/mover_table.py`) through `RenderTable.write_transforms_many`, which logs the rows in the transform log (`transform_version`) but not in the row-change log that the occlusion index reads, so the `CellGroupIndex` is not updated. An unrotated box mover or door therefore stays in the cell group, union box and signature it had when the index was built (or when it was last journalled), wherever it travels: it is skipped or drawn with its original cell. Doors and short-travel movers stay inside or next to their original box; a mover that travels far from a cell that is itself hidden can be skipped while visible. Spinning movers (`rot` angle != 0 when the index is built) and angled movers are ineligible and unaffected. Setting `occlusion_culling = False` removes the effect.

---

## The probe-budget governor

A probe is a draw call plus query bookkeeping through PyOpenGL; on the low-power ARM target that is a measurable share of a frame. `engine.cell_cull.ProbeGovernor` decides how many probes a frame may issue from what the probes actually removed and what they cost.

### Construction

`OcclusionMixin._init_occlusion` creates it with (low-power means `engine.shaders.detect_low_power_arm()[0]`):

| Parameter | Default platforms | Low-power ARM | Meaning |
|---|---:|---:|---|
| `max_probes` | 8 (`OcclusionMixin.MAX_PROBES`) | 4 | ceiling on probes per frame **and** on outstanding queries |
| `min_budget` | 2 | 2 | smallest budget before parking |
| `keep_fraction` | 0.10 (`PROBE_KEEP_FRACTION`) | 0.15 | fraction of sampled rows that must be skipped for a window to count as paying |
| `max_cpu_ms` | 0.25 | 0.167 | mean measured overhead per frame above which probing is "expensive" |
| `window` | 32 | 32 | samples per decision |
| `cooldown` | 180 (`PROBE_COOLDOWN`) | 180 | parked frames |
| `strong_savings_fraction` | 0.35 | 0.35 | skip fraction that excuses expensive probing |

`cell_cull.MIN_PROBE_BUDGET = 8` is the class default for `min_budget`; the renderer always passes 2.

The related early-outs in the mixin are also platform-dependent: `_occlusion_min_rows` is 512 (`MIN_PROBED_ROWS`) or 1024 on low-power ARM; `_occlusion_min_group_rows` is 16 (`MIN_PROBE_GROUP_ROWS`) or 32.

### Inputs

`governor.record(rows_probed, rows_skipped, overhead_ms)` is called once per frame from the `finally` of `_occlusion_filter_slots`, whenever hook 1 got as far as counting eligible rows:

| Argument | Value |
|---|---|
| `rows_probed` | eligible rows among this frame's slots (`_occlusion_rows_sampled`); a sample with 0 is ignored |
| `rows_skipped` | rows hook 1 removed this frame |
| `overhead_ms` | this frame's hook-1 CPU time + the previous frame's query-poll time + the previous frame's probe-submission time |

A frame suspended by a jump or turn is recorded too, with `rows_skipped = 0`: the eligible-row count is stored before the view-change test. Frames where hook 1 returned earlier (not active, too few rows, parked) record nothing. Only the newest `window` samples are kept.

### Decision

`governor.probe_count()` is called once per frame by hook 1 and returns the budget (0 = parked):

```text
if parked:
    idle_left -= 1
    if idle_left == 0:  budget = min(max_probes, min_budget * 2); samples = []
    return 0
if len(samples) >= window:
    fraction  = sum(skipped) / sum(probed)
    expensive = mean(overhead_ms) > max_cpu_ms and fraction < strong_savings_fraction
    samples = []
    if fraction < keep_fraction or expensive:
        if budget > min_budget:  budget = max(min_budget, budget // 2)
        else:                    idle_left = cooldown; return 0      # park
    elif budget < max_probes:    budget = min(max_probes, budget * 2)
return budget
```

On default platforms an unpaying scene steps 8 -> 4 -> 2 -> parked, one step per 32 sampled frames; on low-power ARM 4 -> 2 -> parked. After 180 parked frames the budget re-arms at 4 (`min_budget * 2`, capped at `max_probes`) with an empty window. A paying window doubles the budget back towards `max_probes`. A skip fraction exactly equal to `keep_fraction` counts as paying. Probing that costs more than `max_cpu_ms` is still kept at full budget when it hides at least 35 % of the sampled rows.

While parked, hook 1 returns before touching the index, so neither grouping nor probes are paid for; hook 2 finds no groups, clears the book, and everything is drawn.

`governor.state()` returns `"budget N"` or `"parked N"` (frames left).

---

## Controls

| Control | Where | Default | Effect |
|---|---|---|---|
| `occlusion_culling` | `settings.ini`, section `[Renderer]` | `True` | Read once by `OcclusionMixin._init_occlusion` when a `ForwardRenderer` is created (start-up, or `r_renderer Forward` after another renderer). A value that does not parse as a boolean leaves it on. |
| `renderer.occlusion_culling` | attribute of the live `ForwardRenderer` | from the setting | Read every frame; assigning it takes effect on the next frame. `tools/bench_compare.py --occlusion on\|off` sets it this way. |
| `FIO_ARM_MODE` | environment variable (`1`/`0`) | unset | Forces `detect_low_power_arm()`, and with it the low-power probe ceiling, thresholds and governor parameters. |

There is **no console command** and **no Settings-window checkbox** for occlusion culling in this release; `settings.ini` is the only persisted switch.

### View > Show whole world

The checkable **View > Show whole world** action (`editor/ui.py`, `MainWindow.show_whole_world_action`) was moved to the View menu in the same change set, but it is not an occlusion control. It is the [Big World](https://github.com/ViciousSquid/Fio/wiki/BigWorld-Plugin) editor-streaming toggle:

| | |
|---|---|
| Setting | `[Display] show_whole_world` in `settings.ini`, default `True` |
| Handler | `MainWindow.set_show_whole_world(shown)` -> `EditorState.set_editor_whole_world(shown, camera_pos)` |
| Checked | every editor cell of a Big World map is loaded |
| Cleared | cells are streamed around the 3D editor camera |
| In Play | the action is disabled (`MainWindow.update_play_button_color`); `set_show_whole_world` stores the preference and returns without changing what is loaded |
| Applied | immediately when toggled outside Play, and by `MainWindow` each time a map is loaded |
| Maps without Big World editor streaming | no effect |

It changes which brushes exist in the editor's world (and therefore in the RenderTable); occlusion culling never runs in the editor regardless of this toggle. In Play, Big World parks out-of-range brushes through `hidden`, which journals the slots and takes them out of their cell groups.

---

## Diagnostics

### Debug Tables

`ForwardRenderer` publishes one line under `RenderStats.details['occlusion']` (`OcclusionMixin._describe_occlusion`), shown by [Debug Tables](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables) with the other renderer detail lines:

| State | Text |
|---|---|
| probe pass not initialised | `unavailable` |
| `occlusion_culling` false | `off` |
| otherwise | `S of T brushes skipped; I probes issued, P pending; CPU filter F ms, GPU query calls Q ms, probe submission B ms; index X ms; budget N` (or `parked N`) |

`S`/`T` are the last frame's skipped and skipped+drawn brush slots, `I` the probes issued at the end of the last frame, `P` the outstanding queries; the four timings are the CPU cost of hook 1, of polling, of probe submission and of the index update within hook 1.

Readable attributes on the renderer instance (used by tools and tests; private names are implementation detail):

| Attribute | Meaning |
|---|---|
| `occlusion_culling` | the switch |
| `occlusion_book` | the `OcclusionProbeBook`; `len()` is the number of cells with a recorded result |
| `occlusion_governor` | the `ProbeGovernor` (`budget`, `max_probes`, `state()`) |
| `_occlusion_ready`, `_occlusion_target` | probe pass state and the chosen query target |
| `_occlusion_skipped`, `_occlusion_drawn`, `_occlusion_issued` | last frame's counts |
| `_occlusion_cpu_filter_ms`, `_occlusion_cpu_index_ms`, `_occlusion_cpu_query_ms`, `_occlusion_cpu_probe_ms` | last frame's CPU timings |
| `_occlusion_index_rebuilds` | how many `CellGroupIndex` objects this renderer created |

### SysMon

[SysMon](https://github.com/ViciousSquid/Fio/wiki/System-Monitor-(SysMon)) has no occlusion counter. What it shows moves indirectly:

| SysMon field | Source | Effect of occlusion culling |
|---|---|---|
| `Draws` / `draw_calls` | `renderer.render_stats.draw_calls` | falls when cells are skipped (fewer texture runs and per-row draws); probe draws are not included |
| `Tris (Visible: n)` / `visible_tris` | `renderer.render_stats.visible_tris` (+ terrain) | falls when cells are skipped |
| `Brushes (Visible: n)`, `Culled: p%` | `QtGameView` from the logic thread's frustum result (`visible_brush_slots`) | **unchanged** — these count the frustum cull only |

---

## Tools

### `tools/bench_compare.py --occlusion off|on`

Drives the real editor host (load map, enter Play, step the logic thread by hand, paint) for a fixed frame count and prints one JSON object per map. `--occlusion` sets `renderer.occlusion_culling` after Play starts; without it `settings.ini` decides. Needs a display with OpenGL:

```bash
xvfb-run -a python tools/bench_compare.py --occlusion off --json off.json maps/Office_Corridor.json
xvfb-run -a python tools/bench_compare.py --occlusion on  --json on.json  maps/Office_Corridor.json
```

Occlusion fields in each result (present when the renderer has `occlusion_book`):

| Field | Meaning |
|---|---|
| `occlusion_enabled`, `occlusion_ready` | switch and probe-pass state |
| `occlusion_skipped_mean`, `occlusion_drawn_mean` | brush slots skipped / drawn per frame |
| `occlusion_probes_issued_mean`, `occlusion_probes_issued_max`, `occlusion_probes_pending` | probe counts |
| `occlusion_probe_budget_max` | `occlusion_governor.max_probes` |
| `occlusion_cpu_filter_ms_*`, `occlusion_cpu_index_ms_*`, `occlusion_cpu_query_ms_*`, `occlusion_cpu_probe_ms_*` (`median`, `p95`) | CPU cost per stage |
| `occlusion_cpu_overhead_ms_median` | median of filter + query + probe per frame — the extra work, not net savings |
| `occlusion_index_rebuilds` | index constructions during the run |

### `tools/occlusion_compare.py off.json on.json`

Compares two `bench_compare` runs and reports `on - off` deltas of `paint_cpu_ms_median`, `paint_ms_median`, `frame_ms_median` and `frame_ms_p95` per map (negative = culling helped), with probe counts and skipped brushes alongside. It raises `ValueError` rather than report when the runs do not match: a map missing from either run, different `frames`, `viewport` or `brushes`, a baseline not run with `--occlusion off`, an experiment not run with `--occlusion on`, or `occlusion_ready` false. It never infers a benefit from skip counts alone.

---

## Tests

All headless (no GL context); run with `pytest tests/renderer/<file>`.

| File | What it pins |
|---|---|
| `tests/renderer/test_cell_cull.py` | union box contains every member; one group per row; grouping by centre not footprint; negative/far coordinates; empty input; the book's streak, stale-result, visible-clears, retire and forget rules; `select_probe_groups` near/small exclusion, budget ceiling and 3D distance; `CellGroupIndex` equals direct grouping, updates only touched cells on move/hide, excludes rotated and convex rows, signature changes without a box change, new groups and the large-batch rebuild |
| `tests/renderer/test_probe_governor.py` | paying window keeps the budget; the boundary counts as paying; halving; park and re-arm; step back up; unsampled frames do not count; the low-power 4 -> 2 -> park sequence; the CPU-cost guardrail and its strong-savings exemption |
| `tests/renderer/test_probe_box.py` | `_probe_model_matrix` maps the unit cube's corners onto the full union box; the probe vertex buffer spans both extremes of every axis |
| `tests/renderer/test_query_target.py` | target selection against fake contexts: conservative when accepted, `GL_ANY_SAMPLES_PASSED` on a 3.3 core context, `GL_SAMPLES_PASSED` as last resort, a missing constant skipped, no target disables the feature, a leftover GL error is drained rather than misread |
| `tests/renderer/test_render_table.py` | the table carries no occlusion state; `row_changes_since` reports refreshed rows, nothing when current, and `None` after a reconcile |
| `tests/renderer/test_renderer_boundary.py` | no attribute of a published table is assigned anywhere in `engine/renderer/` |
| `tests/renderer/test_occlusion_compare.py` | `tools/occlusion_compare.py` compares measured frame times, does not infer benefit from skip counts, rejects mismatched runs |
| `tests/editor/test_default_layout.py` | **Show whole world** is a checkable View-menu action bound to `[Display] show_whole_world` and disabled during Play |

The two GL hooks themselves (`_occlusion_filter_slots_impl`, `_occlusion_end_of_frame_impl`) have no dedicated test file; their GL-free decisions are the classes and functions tested above.

---

## Constants

| Constant | Value | Defined in |
|---|---:|---|
| `CELL_SIZE` | 512.0 | `engine/spatial.py` |
| `OCCLUDE_AFTER` | 2 | `engine/cell_cull.py` |
| `MAX_RESULT_AGE` | 2 frames | `engine/cell_cull.py` |
| `PROBE_KEEP_FRACTION` | 0.10 | `engine/cell_cull.py` |
| `MIN_PROBE_BUDGET` | 8 (class default; renderer passes 2) | `engine/cell_cull.py` |
| `PROBE_COOLDOWN` | 180 frames | `engine/cell_cull.py` |
| `ProbeGovernor` `window` / `strong_savings_fraction` | 32 / 0.35 | `engine/cell_cull.py` |
| `OcclusionMixin.MAX_PROBES` | 8 (4 low-power) | `engine/renderer/forward/occlusion.py` |
| `OcclusionMixin.NEAR_EXEMPT_SQ` | `1024.0 ** 2` | `engine/renderer/forward/occlusion.py` |
| `OcclusionMixin.MAX_TURN_COS` | 0.8 | `engine/renderer/forward/occlusion.py` |
| `OcclusionMixin.MIN_PROBED_ROWS` | 512 (1024 low-power) | `engine/renderer/forward/occlusion.py` |
| `OcclusionMixin.MIN_PROBE_GROUP_ROWS` | 16 (32 low-power) | `engine/renderer/forward/occlusion.py` |
| eye jump / drift limit | 32 world units | `engine/renderer/forward/occlusion.py` (literal) |
| union-box pad | `8 * float32 eps * max(1, |coord|)` | `engine/cell_cull.py` |
