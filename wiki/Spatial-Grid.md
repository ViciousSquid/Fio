> Applies to Fio 3.5.0.1111.

# Spatial Grid

The spatial grid is Fio's broad phase for collision and line-of-sight queries: solid brushes are bucketed into uniform 512-unit columns on the XZ plane so a query looks only at the brushes filed under the cells it touches. The cell convention itself is defined once, in `engine/spatial.py`, and is shared by the collision grid (`engine.physics.SpatialGrid`), the renderer's occlusion cell groups ([Cell Occlusion Culling](https://github.com/ViciousSquid/Fio/wiki/Cell-Occlusion-Culling)), the light stream and the Big World streaming plugin ([BigWorld Plugin](https://github.com/ViciousSquid/Fio/wiki/BigWorld-Plugin)).

## Viewing it

In Play, run the [console](https://github.com/ViciousSquid/Fio/wiki/Debug-console) command `sg` (`ConsoleHandler.cmd_spatial_grid`). It toggles `QtGameView.show_spatial_grid`, shows a toast `Spatial Grid: ON|OFF`, and is refused outside Play with `Command 'sg' can only be used in Play Mode.` The flag is cleared when Play is left.

While it is on, `ViewDebugDraw._render_spatial_grid` (`engine/view_debug_draw.py`) draws the outline of every **occupied** cell of the session's grid as cyan lines at Y = 1, in one `GL_LINES` draw with the host's debug program, after each view's scene (both halves in split-screen). Empty cells are not drawn. The lines are the cells' XZ footprints; cells have no vertical extent.

## The cell convention (`engine/spatial.py`)

Stdlib-only (no NumPy, glm or Qt), so the standalone player, plugins and headless tests use the same code.

| Name | Definition |
|---|---|
| `CELL_SIZE` | `512.0` world units |
| `cell_of_point(x, z, cell_size=CELL_SIZE)` | `(floor(x / size), floor(z / size))` — integer `(cell_x, cell_z)`. Floors toward negative infinity; a point exactly on a boundary belongs to the higher cell |
| `cells_of_points(xs, zs, cell_size)` | the same for arrays (`xs // size, zs // size`); returns the type it was given |
| `cell_range(min_x, min_z, max_x, max_z, cell_size)` | `(cx0, cz0, cx1, cz1)`, inclusive |
| `cells_for_aabb(min_x, min_z, max_x, max_z, cell_size)` | every cell an XZ box overlaps, both edges inclusive |
| `cell_bounds(cell_x, cell_z, cell_size)` | `(min_x, min_z, max_x, max_z)` |
| `cell_distance_sq(cell_x, cell_z, px, pz, cell_size)` | squared distance from a point to the cell's nearest edge; 0 inside |
| `cells_within(cells, px, pz, radius, cell_size)` | the keys of `cells` whose area a circle reaches (a true circle, not a square) |

Y is never part of a cell key: a cell is a full-height column.

`CellIndex(cell_size=CELL_SIZE)` is the bucketing primitive. `cells` is a plain `{(cx, cz): [obj, ...]}` dict that hot paths read directly.

| Method | Behaviour |
|---|---|
| `insert(obj, min_x, min_z, max_x, max_z)` | files a **reference** to `obj` under every cell its footprint overlaps; an object spanning a boundary is referenced from each cell, never split or copied. Inserting twice files it twice |
| `insert_point(obj, x, z)` | files under the single cell containing the point |
| `remove_point(obj, coord)` | unfiles by identity from the cell it was filed at (pass the old coordinate, not the object's current position); returns whether it was found |
| `cell(coord)` | the bucket, or `()` |
| `cells_within(px, pz, radius)` | occupied cells a circle reaches |
| `clear()` | empties every bucket |

The same module holds the streaming-related accessors the grid depends on: `authored_hidden(obj)` / `authored_flag(obj, flag)` return the value the map gave a flag, ignoring a streaming layer's temporary park (which stashes the authored value under `PARKED_HIDDEN_KEY = '_bw_parked_hidden'` / `PARKED_DISABLED_KEY = '_bw_parked_disabled'`), and `set_authored_flag(obj, flag, value)` writes it and journals the object for the render tables. The simulation tier constants (`TIER_NEAR` 0, `TIER_ACTIVE` 1, `TIER_DISTANT` 2, `TIER_DORMANT` 3, `SIM_TIER_KEY = '_sim_tier'`, `tier_of(obj)`) also live here; an unstamped object reads as `TIER_NEAR`.

## The collision grid (`engine/physics.py: SpatialGrid`)

### Lifetime

| Event | Code |
|---|---|
| Play starts | `LogicSession` creates `SpatialGrid(cell_size=512.0)`, calls `populate(editor_state.brushes + collision_runtime._model_collision_brushes)`, builds the `PhysicsWorld` on it and hands it to the monster AI (`monster_ai.set_spatial_grid`) |
| model collision toggled during Play | `LogicCollision.toggle_model_collision` repopulates it |
| Play stops | `spatial_grid.clear()`, then `session_runtime.spatial_grid = None` |

There is no grid outside Play. The cell size is passed as the literal `512.0` by the session; nothing reads it from `engine/constants.py` or from settings.

### What `populate(brushes)` files

| Brush | Result |
|---|---|
| `authored_hidden(brush)` or `is_fog` | skipped. The *authored* flag is used, so a brush parked by streaming stays in the grid and a brush the mapper hid stays out |
| `_physics_body` | skipped (simulated by `PhysicsWorld`) |
| water (`engine.constants.is_water_brush`) | appended to `water_brushes`, not filed as solid |
| `is_trigger` that is neither a mover nor a door | skipped |
| mover or door | filed for its **whole travel**: every cell its box can reach along `engine.mover_table.travel_range(brush)` plus where it is now; the filed range is kept on the brush as `_travel_filed`, and a row that later leaves it is marked off range by the mover table |
| convex brush, mesh-collision brush (`geometry` / `_mesh_bounds`) | filed by `engine.brush_placement.world_bounds(brush)` |
| plain box | filed by `pos ± size / 2` |

Every filed brush is also appended to `_all_solid`. After filing, `populate` builds the derived row sets and publishes them by single assignments, so a query in flight on another thread keeps the rows it started with.

### Queries

| Method | Used by | Returns |
|---|---|---|
| `get_potential_colliders(player_min, player_max)` | player physics (`engine/player.py`) | unique brushes filed under the cells the AABB overlaps; a box inside one cell returns that cell's bucket with no de-duplication |
| `get_nearby_brushes(x, z, radius=0.0)` | monster AI | unique solid brushes in cells overlapping the point/radius |
| `overlaps_wall(mx, my, mz, margin)` | monster AI | whether the box `mx ± margin`, `my .. my + 128`, `mz ± margin` overlaps a solid brush |
| `raycast_down(x, z, start_y=10000.0)` | monster AI, props | Y of the highest brush top at or below `start_y` under the point, among brushes in the point's cell; `None` if there is none |
| `has_line_of_sight(start, end)` | monster AI | `True` if no solid brush blocks the segment |
| `raycast_down_batch(x, z, start_y)`, `overlaps_wall_batch(mx, my, mz, margin)` | the dense monster pass | the same answers for arrays of points/boxes (`NaN` where the scalar form returns `None`); a box spanning more than two cells on an axis is routed to the scalar query |

Static brushes are read from boxes resolved once per authored change (`_BoxRows`, rebuilt when `brush_placement.authored_generation` moves); movers and doors are resolved at query time from their live placement.

### Line of sight

`has_line_of_sight` visits exactly the cells the ray's XZ projection crosses (`_cells_along_ray`, Amanatides–Woo traversal, start cell first). Candidates are the brushes in those cells. With at least `SpatialGrid.LOS_DENSE_MIN_CANDIDATES` (256) static candidates, static brushes are tested as dense NumPy rows (`_los_dense`) and only movers and doors are walked; below that, every candidate is walked in Python with early exit (`_los_walk`). Both paths give the same answer; the constant is a speed knob (0 means always dense) and can be set on the class or an instance.

Line of sight runs on the monster AI thread. It reads only the rows `populate` published (static AABBs copied once; movers and doors as live brush references), never a render table.

## Other users of the convention

| User | How it uses cells |
|---|---|
| `engine/cell_cull.py` | occlusion cell groups: each eligible brush row is grouped by the cell of its **centre** (one group per row, unlike `CellIndex.insert`, which files a footprint under every cell it touches) |
| `engine/renderer/core/light_stream.py` | re-gathers candidate lights when the light focus crosses a 512-unit cell |
| `plugins/bigworld` (`cell.py`, `manager.py`) | streaming cells are the same columns; `brush_cells` is `cells_for_aabb` |

## Tuning

`SpatialGrid(cell_size=...)` accepts another size, and `CellIndex` honours a custom size everywhere, but the engine constructs the session grid at 512 and Big World streams the same columns. Larger cells mean fewer buckets and more candidates per query; smaller cells mean fewer candidates per query, more buckets, and more cells crossed per line-of-sight ray.

## Tests

| File | Covers |
|---|---|
| `tests/spatial/test_cell_index.py` | flooring toward negative infinity, boundary ownership, inclusive AABB ranges, references not copies, circle queries, negative coordinates, custom cell sizes, the authored-flag accessors and their parked-state behaviour |
| `tests/spatial/test_spatial_grid.py` | what `populate` files (hidden, fog, triggers, water, parked brushes, mesh bounds), repopulation and `clear`, every query against an exhaustive scan, the single-cell fast paths, line of sight including a diagonal ray past a brush straddling a cell boundary |
| `tests/spatial/test_large_cell_queries.py` | batched queries at extreme and non-finite coordinates |
| `tests/physics/test_line_of_sight.py` | dense and walked line of sight agree; cell traversal equivalence |
| `tests/performance/test_los_threshold_benchmark.py` | opt-in measurement behind `LOS_DENSE_MIN_CANDIDATES` |
| `tests/renderer/test_cell_cull.py` | the occlusion groups use the same cell convention |
