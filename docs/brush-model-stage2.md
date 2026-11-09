# Unified brush model — Stage 2: shape, placement and derived bounds

Status: **implemented** (2a–2d; §11 records each substage, its validation
and measurements, §11.5 the integrated result and §11.6 the performance
gate against Stage 1). **The performance gate is not passed** (§11.6):
representative gameplay is level with Stage 1, the player next to moving
shapes is not. Stage 2e (true-shape hitscan, projectiles, AI line of sight
and physics bodies) is a follow-up. Builds on Stage 1 (`56ec0e3`, one material record per face); the
overall plan is `docs/brush-model-proposal.md`. Sections 1–10 are the
investigation and design as approved.

The invariant Stage 2 has to make true:

> **A brush can move without its shape changing, and every subsystem agrees
> on where it is.** The geometry used for rendering, collision, picking and
> spatial visibility describes the same object in the same effective place;
> a save/reload or a cache miss never changes where it is drawn.

Measurements below are CPython 3.13 on x86-64 (llvmpipe for GL), the same
machine as Stages 0 and 1. Scripts are in the session scratchpad and are
re-created as tools in the implementation (see Gates).

## 1. Baseline

`56ec0e3` is clean and pushed (`origin/3.1.0.0611_CANARY` = local `HEAD`). The
full suite on that exact commit, in a clean worktree (llvmpipe):
**6,907 passed, 150 skipped, 4 xfailed, 5 failed** — the five failures Stage 0
at `a820c40` also has (play-cycle leak, editor chrome, three portal-fade), and
nothing else. The 4 xfails are the Stage 2 targets. This settles the Stage 1
result, whose two intended guard updates had been re-run only in their
directories, and is the baseline Stage 2 is measured against.

## 2. Current architecture and confirmed defects

Fio began AABB-only. A box's `pos`/`size` are its geometry *and* its
placement. Angled brushes were added on top: their planes are stored in
**world space**, `pos`/`size` are kept as a copy of the planes' bounds
(`sync_brush_bounds`), and the rendering reuses the box transform by storing
each angled mesh in the unit cube of the `pos`/`size` it had when its geometry
record was made (the *anchor*). Movers move a brush by writing `pos` (and, for
spinners, `_rot_angle`/`rotation_yaw`) into the authored dict every tick.

Confirmed defects, each reproduced on this branch:

| # | Defect | Evidence |
|---|---|---|
| D1 | **An editor move of an angled brush is a shape edit.** Dragging rewrites every plane (`translate_brush`), which re-solves the windings (~0.8 ms for an 8-plane brush) and, through the mesh cache key, rebuilds and re-uploads the GPU mesh on every drag step. Editor rotation is the same. | Drag probe on the M2 path, per step: **1 mesh build, 2,688 B geometry + 112 B transform upload**; a `pos`-only (mover-style) move: **0 builds, 112 B**. |
| D2 | **Angled movers collide as their bounding box.** Mesh collision is prepared only for brushes `angled_brush_is_solid` accepts, which excludes movers and doors; the player's AABB path then uses `pos`/`size`. A ramp that moves collides like a box. (Corrects Stage 0's finding 9: in play the triangles are not stale, they are absent.) | `logic_collision.angled_brush_is_solid`, `player._resolve_collision` / `brush_aabb_bounds`. |
| D3 | **Rotation is ignored by most derived bounds.** A spinning brush (box or angled) is culled by the main view and collided as its *unrotated* box: main-view frustum culling (`aabb_in_frustum_bounds` on `bounds`), collision rows, line of sight, the physics grid and the player's AABB. A long bar at 45° can be culled while visible and collides where it is not drawn. Three consumers already handle rotation and show the pattern: `actor_pick` turns its ray by the row's rotation, portal views cull by bounding sphere, and occlusion grouping leaves rotated rows out. | `logic_render` cull on `table.bounds`; `brush_aabb_bounds` reads `pos`/`size` only; `cell_cull` (`rot == 0` rows only); `visibility` portal cull (`norm(half)`). |
| D4 | **Bounds are a second authority for angled brushes.** `pos`/`size` are stored and saved, and drift from the planes: three brushes beyond float noise in shipped maps, two by 128 units (`Terrain_Test_small`, `Topdown_example`: a clip removed a whole side and the bound was never re-synced). Consumers disagree: player collision, the physics grid and Big World *in play* use the plane-derived `_mesh_bounds`; culling, picking, AI line of sight, hitscan, projectiles and Big World *editor streaming* use `pos`/`size`. | Stage 0 inventory; `physics.SpatialGrid.populate`, `bigworld.manager.add_brush`, `logic_render`, `logic_collision.CollisionRows`. |
| D5 | **What is drawn depends on history.** The mesh anchor is `pos`/`size` at record creation and is kept while the planes are unchanged, so a `size`-only change (console `setprop`, a plugin) stretches an existing angled brush until reload. | Stage 0 `test_size_of_an_existing_convex_brush_is_a_transform` and its strict-xfail target. |
| D6 | **Runtime state leaks into the authored dict.** `MoverTable` (and path movers, door inputs, the editor's mover preview, savegame restore) write `pos`, `_rot_angle`, `rotation_yaw` and `original_pos` into the brush; the serialiser keeps them. Seven shipped maps carry `original_pos` (all saved at rest: `pos == original_pos`); one carries `rotation_yaw`. | `mover_table._write_positions`, `logic_movers`, `io_handlers.door_open`, `main_window._advance_mover_preview`, `savegame._restore_moving_brushes`. |
| D7 | **World-space planes cap the world.** The convex solver seeds each face with a polygon of half-size 262,144 units (`_BOGUS`); an angled brush much beyond that from the origin solves to nothing and disappears (measured: valid at 2×10⁵, invalid at 3×10⁵). Boxes have no such limit; Big World offers infinite terrain. | `brush_geometry._BOGUS`; translation probe. |
| D8 | **Shape fidelity outside the player.** Hitscan, projectiles, AI line of sight and physics bodies treat every brush, angled ones included, as an AABB. This is a *shape* approximation, not a placement disagreement; recorded separately (§9). | `logic_combat`, `physics._shape_from_brush`, `CollisionRows`. |

Measured precision of world-space planes: vertex error against a local-frame
solve is 4×10⁻¹³ units at 10³, 2.6×10⁻¹² at 10⁴, 2.2×10⁻¹¹ at 6×10⁴ — fine at
today's map sizes; the hard limit is D7, not rounding.

## 3. Who owns what today

"Writers" excludes the save/load copy and undo snapshots, which copy every
field.

| Field / structure | Current owner | Writers | Readers | Intended owner (Stage 2) |
|---|---|---|---|---|
| box `pos` | authored dict | editor move/resize, movers (every tick), door/path inputs, savegame restore, editor mover preview | everything (≈210 brush/thing `pos` sites): tables, collision, culling, picking, Big World, AI, triggers, parenting, plugins | **authored placement**; runtime never writes it |
| box `size` | authored dict | editor resize, `setprop` | tables, collision, culling, picking, Big World, plugins | **authored shape** (unchanged) |
| angled `geometry.planes` | authored dict, **world space** | every geometry edit, editor *move* and *rotate*, clip, CSG | `get_convex` → render records, collision mesh, picking, 2D views, Big World (via `_mesh_bounds`), plugins | **authored shape, local frame** (§5) |
| angled `pos` | dict, a *copy* of the planes' bound centre | `sync_brush_bounds`, editor move, movers | as box `pos`, plus the mesh anchor | **authored placement** = the shape's origin |
| angled `size` | dict, a *copy* of the planes' extent, saved | `sync_brush_bounds`, `setprop` | as box `size`, plus the mesh anchor | **derived** (shape extent), read-only, not saved |
| `_rot_angle`, `rot_axis` | dict | movers, savegame restore, editor preview | RenderTable `rot`, `actor_pick` | `rot_axis`: authored mover parameter; angle: **MoverTable** |
| `rotation_yaw` | dict (mirror of `_rot_angle`) | movers, savegame | parented portals | **MoverTable** (accessor) |
| `original_pos` | dict, runtime | movers, door input, logic wizard, editor preview | movers (restore), deathmatch plugin | **gone**: MoverTable's authored origin is `pos` |
| mover position / spin | `MoverTable.pos`, `rot_angle` (dense) **and** the dict | `MoverTable` step, path movers | RenderTable (`publish`), the dict mirror for everyone else | **MoverTable only**, as a runtime displacement + spin |
| RenderTable `bounds` (centre, half), `rot` | RenderTable | warm resolve from `pos`/`size`; `MoverTable.publish` | culling, instancing, model matrices, M2 transforms, occlusion, portals, shadows, `actor_pick` | unchanged layout; **centre = effective placement, half = shape half-extent** |
| GeometryRecord anchor (`origin`, `scale`) and mesh key (`surface`) | RenderTable | record creation from `pos`/`size` | GPU mesh build, M2 cache | **shape's own local bounds** — translation-invariant key |
| `_mesh_triangles` / `_mesh_planes` / `_mesh_bounds` | dict, runtime, world space, built at play start for static solid angled brushes | `build_collision_mesh` | player, physics grid, Big World (play), deathmatch navigation | **per shape, local frame, cached on the shape**; placement applied per query |
| CollisionRows `lo`/`hi`, physics `SpatialGrid` cells, LOS rows | logic thread, from `pos`/`size` (or `_mesh_bounds`), movers re-read from dicts | grid build; `refresh_movers` | hitscan, projectiles, AI, physics bodies | **derived bounds** of the effective placement; movers read from MoverTable columns |
| Big World cells | manager, from `pos`/`size` (editor) or `_mesh_bounds` (play) | index/stream | streaming, parking, persistence | **derived bounds** of the authored placement (home cell), one rule for editor and play |
| EntityTable (things) | EntityTable | thing positions; parented lights/portals written from the mover's `brush['pos']` each tick | entity passes | unchanged; parenting reads the mover's effective placement |

## 4. The contract

Four things, each with exactly one owner:

1. **Shape** — authored, changed only by a shape edit (clip, vertex/face drag,
   resize, editor rotation, CSG, retexture does not count):
   * box: `size` (fixed topology; the cube VBO and every box fast path);
   * convex: the plane set **in the brush's local frame**, normalised so the
     shape's bounds are centred on the local origin. Solved once into
     windings, local bounds, local collision data and a GPU mesh, all cached
     on the shape and keyed by it.
2. **Authored transform** — `pos`: where the shape's origin is in the world
   (for every brush, the centre of its bounds, as every reader of `pos`
   already assumes). An editor move changes `pos` and nothing else.
   No authored rotation field: an editor rotation stays a shape edit (it
   bakes into a convex brush's planes, as today), so a box stays an AABB and
   every box consumer stays on its fast path.
3. **Runtime transform** — owned by `MoverTable` (linear movers, doors, path
   movers; the editor's mover preview drives the same columns): a
   displacement from `pos` and a spin (axis, angle) about it. Never written
   into the brush. Effective placement = `pos` + displacement, rotated by the
   spin about that point.
4. **Derived bounds** — the shape's local bounds (box: ±`size`/2; convex:
   cached on the shape) carried by the effective placement; with a spin, the
   AABB of the turned box (|R|·half). Never saved for a convex brush, never
   written by anything but the resolver. A convex brush's `size` remains in
   memory as a read-only view of the shape's extent for existing readers.

There is one transform authority per kind (authored: the dict's `pos`;
runtime: `MoverTable`), and one resolver every consumer goes through for the
effective placement and bounds (`engine/brush_placement.py`, GL-free).

## 5. Local-frame planes or world-space planes

| | World-space planes (today) | **Local frame, origin = bound centre (proposed)** |
|---|---|---|
| Editor move of an angled brush | rewrites every plane; re-solve (~0.8 ms); GPU mesh rebuilt and re-uploaded per drag step (D1) | `pos` only: no re-solve, no rebuild, 112 B transform upload |
| Editor rotate / resize / clip | shape edit | shape edit (unchanged cost) |
| Mesh cache key | includes world plane offsets and the `pos`/`size` anchor | shape + materials only: a moved brush keeps its mesh, identical shapes (duplicates, pasted copies) share one |
| Precision | error grows with distance from the origin (2×10⁻¹¹ at 6×10⁴) | errors bounded by the brush's own size |
| World size | angled brushes fail beyond ~2.6×10⁵ units (D7) | no limit beyond the shape's own extent (`MAX_BRUSH_EXTENT` 32,768) |
| Consumers needing world geometry (picking, 2D outlines, component edit, clip, CSG, navigation) | read planes directly | read a world view: local result + `pos` (cached per shape and placement; a translation, not a re-solve) |
| Format | unchanged | map version 5, migrated at load |
| Blast radius | — | `brush_geometry` brush-level wrappers, editor tools, 2D/3D views, Big World, deathmatch navigation, plugins reading planes |

**Recommendation: local frame.** It is the only option that makes an editor
move a transform-only edit, which is the stated invariant, and it removes D7.
The blast radius is real but mechanical: nearly every world-geometry consumer
goes through a handful of `brush_geometry` functions (`get_convex`,
`brush_points`, `ray_convex_face`, `face_plane_index`, the component-edit
helpers), which gain a world view, and the hot paths (rendering, collision)
consume the local shape and the placement directly.

Rejected: *keep world planes and make the mesh key translation-invariant*
by re-expressing planes relative to the bound centre on the fly. Equality of
keys would then rest on floating-point rounding of `d − n·c`, so a moved
brush could miss the cache (a spurious rebuild) or, worse, two near-equal
shapes could share a key; and the plane rewrite and re-solve per drag step
would remain.

## 6. Effects by subsystem

**Rendering.** RenderTable layout unchanged (353 B per row): `bounds` holds the
effective centre and the shape's half-extent, `rot` the runtime spin. The
geometry record's anchor becomes the shape's own local bounds, so the mesh is
a function of the shape alone: an editor move or a mover is a 112 B transform
update with no geometry upload, as a mover already is; D5 disappears. Main-view
culling expands the half-extent of the (few) rotated rows at cull time
(|R|·half) instead of storing a second extent column; portal views (bounding
spheres) and occlusion grouping (rotated rows excluded) already cope. The box path,
the shared cube VBO, the six-face columns and M2 batching are untouched.

**Collision** (mandatory, D2/D3). Two ways to make collision follow a moving
brush were measured:

| | A: query in the shape's frame (**proposed**) | B: rewrite world-space collision data as it moves |
|---|---|---|
| per tick per moving angled brush | nothing | 47 µs (16 triangles, 8 planes, translation only; more with rotation) |
| per query | 0.2 µs to move a point into the frame (translation); ~3 µs with a rotation in NumPy (cheaper in GLM) | nothing |
| queries per tick | the player tests each nearby brush about a dozen times (capsule spheres × relaxation passes) | — |
| rotation | free (turn the query) | rotate every triangle and plane every tick |

A: collision data (triangles, half-space planes, local bounds) is built once
per *shape*, in its local frame, and cached on it like the windings — for
static angled brushes too, replacing the per-brush world-space bake at play
start. Each query moves the probe into the frame (p′ = Rᵀ(p − c)), resolves,
and turns the answer back. Broad phase uses the derived bounds. Angled movers
get true-shape collision; a spinning box collides as its turned box through the
same path with its six planes implied by `size` (no stored plane dicts);
static boxes stay on the AABB path. `actor_pick` already works this way.
Today's cost per player tick (p50): 8 static angled brushes 0.050 ms, 8 angled
movers on the AABB path 0.036 ms — moving them onto A should cost about what
static angled brushes already do. Riding a platform keeps the existing linear
carry (now from MoverTable's displacement); angular carry on a spinning
platform is out of scope.

**Spatial indexing.** The physics grid, collision rows, line of sight and Big
World filing use one rule — the derived bounds of the authored placement for
static brushes, and MoverTable's live columns for movers (as `refresh_movers`
re-reads dicts today, but dense). The editor and play file a brush in the
same Big World cells.

**Picking.** Rays are moved into the brush's frame (as `actor_pick` does);
angled faces are tested on the local shape.

**Editor.** Move = `pos`. Rotate, resize, clip, vertex/face drags = shape
edits on the world view, re-localised (the origin re-centred on the new
bounds). Undo/redo snapshots carry `pos` and the local planes; Stage 1's face
records are untouched. The mover preview drives the runtime columns.

**Runtime and plugins.** Parented lights and portals, savegames and the
deathmatch plugin read the effective placement through the resolver.
Plugin API 1.8.0 adds `placement(brush)` (authored `pos`, effective centre,
spin, derived bounds). A third-party plugin that read `brush['pos']` of a
*moving* brush mid-play would read the authored position (D6 is precisely
that it used to be overwritten) — a documented behaviour change.

## 7. Serialisation and compatibility

* **Map version 5** (Stage 1's version 4 keeps its meaning): an angled brush
  saves `pos` and local planes, no `size`; no brush saves `original_pos`,
  `_rot_angle` or `rotation_yaw`. Version ≤ 4 loads through the existing chain
  plus one migration: planes re-expressed about the planes' bound centre
  (`d′ = d − n·c`, `pos = c`), so the two 128-unit brushes get their true bound
  and draw exactly where they did; runtime keys dropped (all seven shipped
  maps were saved at rest; if `pos ≠ original_pos` the box's authored position
  is `original_pos`).
* **Big World**: resident and dormant records migrated at load, as Stage 1
  does; a dormant mover's runtime state lives in MoverTable/savegame state,
  not in its record, so parking and reactivation cannot move its home.
* **Savegames**: the moving-brush record (`pos`, `rot`) becomes a runtime
  displacement and spin against the authored placement; older saves restore
  through that mapping.
* **Legacy export** extends to version 5 → 4/3 (world planes and `size`
  re-materialised), alongside Stage 1's.
* **Android player**: reads world-space planes today; it needs the world view
  (or the exported layout). It is out of scope here, so until it is updated
  the player is handed exported (version 4) maps — called out as a release
  dependency.

Precision risk of the migration: re-expressing a plane about `c` and back
moves a vertex by ~|c|·10⁻¹⁶ (≈10⁻¹² at today's coordinates); GPU vertices
stay in the anchor's unit cube in float32. Expected pixel-identical; gated by
`compare_map_pixels`.

## 8. Implementation sequence (each step reversible on its own)

1. **2a — Placement resolver and derived bounds** (no format change). One
   GL-free resolver for effective placement and bounds; RenderTable, main-view
   culling (rotated rows expanded), collision rows, physics grid, Big World
   filing and picking read it. Anchor = the shape's bounds. Fixes D3, D4, D5. Rollback:
   revert the resolver and its callers.
2. **2b — Runtime transform ownership.** MoverTable, path movers, door inputs,
   the editor preview and savegame restore stop writing the dict; parenting,
   savegames and the deathmatch plugin read the resolver; the serialiser drops
   runtime keys. Fixes D6. Rollback: restore the dict mirror (one function).
3. **2c — Transform-based collision.** Per-shape local collision data; the
   player's mesh path queries in the shape's frame for static and moving
   angled brushes; spinning boxes through the same path; broad phase from the
   resolver. Fixes D2 (mandatory) and the collision half of D3. Depends on 2a
   and 2b, not on 2d.
4. **2d — Local-frame planes, map version 5.** Shape local, editor move =
   `pos`, translation-invariant mesh key, world view for tools, migration and
   legacy export. Fixes D1 and D7.
5. **(2e, optional)** True-shape narrow phase for hitscan, projectiles, line
   of sight and physics bodies against angled brushes (D8): ray/segment
   against half-spaces, vectorisable. Can follow Stage 2.

2a–2c deliver the mandatory correctness without a format change; 2d is the
largest step and the one that changes the file format.

## 9. Gates

**Correctness** (characterise first, then hold):

* static box and angled parity: the Stage 0/1 snapshot and the GPU draw
  oracle, unchanged pixels on every shipped map (`compare_map_pixels`), the two
  128-unit brushes included;
* transform-only movement: editor move and mover tick — zero geometry
  uploads, zero mesh builds, zero convex solves, zero collision rebuilds
  (counters, as `test_geo_steady_state` does);
* **a collision oracle**, independent of the collision code: the effective
  world shape evaluated as half-spaces (inside test, signed distance, ray
  entry) from the authored shape and the runtime transform, compared with
  the engine's answers (player depenetration, collision rows, physics grid,
  picking) at sampled points along a mover's whole path, translating and
  spinning, box and angled;
* bounds agreement: every consumer's bounds for a brush equal the resolver's
  (rendering, culling, collision rows, physics grid, Big World filing,
  picking), rotated rows included;
* editor translate, rotate, resize, clip, undo/redo; save/reload and forced
  mesh-cache misses give identical world-space geometry; material/UV stable
  through translation and rotation (Stage 1's GPU-state tests);
* Big World: resident and dormant records, parking, reactivation, a mover
  crossing cells; savegame round trips mid-motion;
* Stage 0/1 guards unchanged (AABB shared-cube guard, face precedence,
  write-site ratchet — which must go *down* for `pos`, `original_pos`,
  `_rot_angle`, `rotation_yaw`).

**Performance** (same machine, interleaved, base `56ec0e3`):

* AABB budgets: heap per box ≤ 1,359 B; RenderTable 353 B per row; BigWorld
  sync, one-row refresh, still frame and edit latency within run-to-run
  noise;
* static world and moving world benchmarked separately: per-tick
  transform-update cost (MoverTable step + publish), collision step (player
  with N box / angled / spinning movers), GPU upload bytes per tick (112 B
  per moved angled brush, 0 geometry), mesh builds and convex solves per tick
  (0), frame-time p50/p95/p99/max;
* editor drag of an angled brush: 0 mesh builds and 0 geometry bytes per step
  (today 1 and 2,688 B).

## 10. Decisions for approval

1. **Local-frame planes and map version 5** (2d), or keep world-space planes
   (no format change; editor moves of angled brushes keep rebuilding meshes;
   the ~2.6×10⁵-unit limit stays).
2. **`brush['pos']` of a moving brush means the authored placement**, with
   plugins reading motion through the new accessor (proposed), or keep a
   transitional runtime mirror for one release.
3. **2e** (true-shape hitscan/projectiles/line of sight/physics bodies) inside
   Stage 2, or as a follow-up.
4. Angular carry on spinning platforms stays out of scope.

## 11. Implementation

Each substage is committed on its own, with its validation.

### 11.1 Stage 2a — placement resolver and derived bounds

#### The resolver

`engine/brush_placement.py` (GL-free) is the one answer to "where is this
brush and what does it cover". It gives, for any brush dict:

* `authored_box` — the shape at its authored placement, unturned, as
  `(cx, cy, cz, hx, hy, hz)`: a box's `pos`/`size`; a convex brush's planes'
  bounds (`ConvexGeometry.shape_box`, cached on the shape); an entity
  model's collision mesh proxy's `_mesh_bounds`; an invalid plane set's
  `pos`/`size`, exactly as it is drawn;
* `placed_box` — that box displaced by the runtime transform;
* `runtime_spin` — the spin (axis, angle), the values the RenderTable's
  `rot` column takes;
* `world_box` / `world_bounds` — the derived bounds: the placed box, widened
  to the AABB of the turned box (`|R| · half`) while it spins;
* `authored_bounds` — no runtime transform, no spin: a brush's home;
* `turned_bounds` — the same widening for a block of RenderTable rows
  (returns the table's own view, uncopied, when no row turns);
* `ray_to_shape` / `ray_box` — a world ray carried into the frame the shape
  is authored in, so a pick hits a moved or spinning brush where it is drawn;
* `normalise_loaded` — re-derive a loaded convex brush's `pos`/`size` copy
  from its planes.

Runtime transform in 2a is transitional: movers still write `pos` and
`_rot_angle` into the dict, so a convex mover's displacement is read as its
live `pos` against its planes' rest centre (equal at rest: kept so by
`sync_brush_bounds` and by load normalisation). Stage 2b moves the runtime
transform into `MoverTable`; the rules do not change, only where the
displacement and spin are read from.

Cost: a box is two dict reads. A convex brush reads its cached shape
(`_geo_cache`, which every geometry invalidation drops); `get_convex`
re-hashes every plane (~10 µs for 7 planes) and is only reached on a miss.

#### Consumers

| Consumer | Before | Stage 2a |
|---|---|---|
| RenderTable `bounds` (centre, half) | `pos`/`size` | `placed_box` (convex: planes; layout unchanged, 353 B/row) |
| Convex mesh anchor (`GeometryRecord.origin/scale`, part of the M2 mesh key) | `pos`/`size` when the record was made | the planes' own bounds |
| Main-view frustum cull | `table.bounds`, rotation ignored | `turned_bounds(table.bounds, table.rot)` |
| Editor cordon (view filters) | `table.bounds`, rotation ignored | `turned_bounds` |
| Collision rows (hitscan, projectiles, AI walls) | `pos`/`size` | `world_box` |
| Physics grid filing | `pos`/`size`, or `_mesh_bounds` for mesh brushes | `world_bounds` |
| Physics grid rows, wall overlap, floor ray, floor batch | `pos`/`size` | `world_bounds` |
| Line-of-sight AABB cache (`brush_aabb_bounds`) | `pos`/`size` | `world_box` (key: pos, size, spin, shape, mesh bounds) |
| Physics static cells, body shapes | `_mesh_bounds` or `pos`/`size` | `world_bounds` / `world_box` |
| Player water, ledge, step tests | `pos`/`size` | `world_bounds` |
| Big World filing (editor and play) | editor `pos`/`size`, play `_mesh_bounds` | `authored_bounds` (one rule) |
| Editor 3D object pick, face pick | `pos`/`size` AABB; convex faces on world planes | ray in the shape's frame: authored box / convex faces |
| Editor 2D pick, view cull, footprints | `pos`/`size` | `world_bounds` |
| Combat and AI fallback walks | `pos`/`size` | `world_bounds` / `brush_aabb_bounds` |

Already rotation-aware and unchanged: `actor_pick` (rays turned by the row's
rotation), portal views (bounding spheres), occlusion grouping (rotated rows
excluded), shadow casters (per-light reach from the table).

#### Intended behaviour changes

1. **Convex bounds come from the planes.** On shipped maps this changes
   three bounds and nothing drawn: brush `1a817c94` in `Terrain_Test_small`
   and `Topdown_example` (the copy was 128 units too wide on −X; culling,
   collision rows, the grid, Big World and picking now stop at the solid),
   and `bcac8b00` in `Angled_Brush_Test` (a copy rounded to three decimals,
   4.85×10⁻⁵ smaller than the solid; now conservative). Pinned by
   `tests/brush_model/test_stage2a_changes.py`; the semantics snapshot
   records the two maps' new digests.
2. **History independence (D5).** A `size`-only write (console `setprop`, a
   plugin) or a stray `pos` write to a static convex brush changes nothing
   drawn, collided or picked, before or after a reload.
3. **Spinning rows (D3).** A spinning box or angled mover is culled, filed,
   collided by every AABB consumer and picked as its turned box (the AABB of
   it, for the AABB consumers) — before, as its unturned box, so a long bar
   at 45° could be culled while visible and collided where it was not
   drawn.
4. **Mesh proxies.** An entity model's collision mesh proxy is one shape
   for every reader (its mesh bounds); hitscan, projectiles and the AI's
   wall tests used to read the entity's placeholder 1-unit box. No shipped
   map has one: every shipped model is OBJ, which collides through box
   proxies.

#### Remaining approximations (recorded)

* The player collides with a spinning box, and with an angled mover, as the
  AABB of its turned box (previously the unturned box): Stage 2c makes it
  the true shape.
* Hitscan, projectiles, AI line of sight and walls, and physics bodies test
  every brush as its derived AABB (Stage 2e, follow-up).
* The editor's 2D resize/move handles still edit the stored `pos`/`size`
  copy of a convex brush (kept in sync); Stage 2d moves editing to the
  shape and its placement.

#### Validation

* **Full suite** (tests + plugins, llvmpipe, before the hot-path tuning
  below): 7,077 passed, 150 skipped, 2 xfailed, 7 failed. Five failures are
  the baseline's (play-cycle leak, editor chrome, three portal-fade); the
  other two were GL tests restated in this stage after that run had
  collected them (`test_geo_shared_storage`: a non-mover's `pos` write is no
  longer a move — see behaviour change 2), which pass. 150 skipped as at the
  baseline (the GL tier ran). The two Stage 2 targets 2a delivers — derived
  bounds and history independence — now pass as regression tests (4 → 2
  xfails). About 170 new tests (`test_placement`, `test_stage2a_changes`).
  After the tuning: the brush-model, physics, spatial, logic, threading,
  regression, AI, renderer, engine and Big World groups re-run, all passing.
* **Pixel parity** (`tools/compare_map_pixels.py`, base `56ec0e3`): all 20
  shipped maps identical in all 120 views, `Terrain_Test_small` and
  `Topdown_example` included. (The tool now frames its cameras from the map
  file's records, which both checkouts read alike.)
* **Extreme orientations**: zero, 10⁻⁹°, half-degree, quarter, half and full
  turns, 359.999°, −45°, 725°, the three principal axes, skew axes, an
  unnormalised axis and two axes too short to rotate — the turned bounds
  equal a quaternion turn of the box's corners to 10⁻⁹, every drawn vertex
  of a spinning box or angled row lies inside them, and a box's are exactly
  its drawn AABB.
* **Bounds agreement**: RenderTable (as culled), physics grid rows and
  filing, the AABB cache, collision rows, Big World filing, the cordon and
  both editor pickers read the resolver's bounds, turned rows included.

#### Measurements

Same machine, interleaved with `56ec0e3`; best of three rounds (the host is
noisy: medians move by up to 10 % between identical runs).

| | Stage 1 | 2a |
|---|---|---|
| BigWorld heap per box (budget 1,359 B) | 1,358.2 B | 1,358.2 B |
| RenderTable bytes per row | 353 B | 353 B |
| RenderTable retained heap per row, BigWorld / `_SHOWCASE` | 540.9 / 3,048.9 B | 540.8 / 3,163.9 B |
| warm sync, BigWorld (3,604 boxes) | 47.2 ms | 50.8 ms (rounds overlap: 47.3–55.5 vs 46.4–55.5) |
| one-row refresh, BigWorld / `_SHOWCASE` | 0.082 / 0.068 ms | 0.081 / 0.071 ms |
| still frame p50, BigWorld, two rounds | 11.0 / 11.8 ms | 11.0 / 10.6 ms |
| still frame p50, `_SHOWCASE`, two rounds | 8.4 / 9.8 ms | 8.7 / 8.0 ms |
| editor move p50, BigWorld | 16.2 / 16.6 ms | 15.8 / 16.3 ms |
| physics grid populate, BigWorld | 21.2 ms | 21.6 ms |
| collision rows build, BigWorld | 2.30 ms | 2.57 ms |
| Big World index, BigWorld | 4.31 ms | 4.29 ms |
| AABB cache hit: box / angled | 0.179 / 0.186 µs | 0.235 / 0.247 µs |
| AABB cache miss: box | 0.935 µs | 0.962 µs |
| AI wall test per query (`overlaps_wall`) | 2.20 µs | 2.29 µs |
| player tick p50: floor / 8 box movers / 8 static angled | 7.7 / 24.2 / 21.0 µs | 7.7 / 26.6 / 21.2 µs |
| player tick p50: 8 angled movers / 8 spinning boxes | 28.6 / 33.7 µs | 44.5 / 55.5 µs |

Box paths are at Stage 1 levels: memory identical, rendering within noise.
The costs that remain are the price of consistency, and are recorded: a
cached AABB costs 0.05 µs more per query (three key tests decide whether a
brush is a plain box — a spin starting in place changes nothing a cheaper
key could see); building the collision rows walks every brush once more per
world change (+0.27 ms on 3,604 brushes); the `_SHOWCASE` rows carry their
shapes' cached bounds (+115 B per row on average) and angled brushes are
solved at load rather than at their first draw. Angled movers and spinning
boxes cost more per player tick: a spinner's turned AABB is larger (more
contacts to resolve), and an angled mover resolves its shape through the
resolver — both paths are replaced by Stage 2c's shape collision.

To get there, the resolver has a plain-box fast path (two dict reads, the
old arithmetic), a still angled brush's bounds are cached on its shape
(`ConvexGeometry.shape_bounds`), and the hottest per-brush loops — the AI's
wall and floor queries, grid filing, collision rows and Big World filing —
test for a plain box inline and keep the old arithmetic for it, asking the
resolver only for anything else.

### 11.2 Stage 2b — the mover table owns runtime transforms

#### Ownership

A mover's or door's travel and spin are its *runtime transform*. They are
owned by a **runtime source** registered with the resolver
(`brush_placement.register_runtime`, held weakly) and are never written into
the brush: `pos` is the authored placement, always, and `original_pos`,
`_rot_angle` and `rotation_yaw` are neither written nor read. Two sources
exist:

* `MoverTable` during play. A row's `original` column is the brush's
  authored centre (from the resolver, so an angled mover's rest is its planes'
  centre), `pos` is where it is now, a mover's spin is `rot_angle`. After
  every step it refreshes a per-brush `(centre, axis, angle)` tuple for the
  rows that moved or turned, which is what the resolver hands every reader;
  a row back at rest (its position its authored centre, no spin) has no
  entry. A source answers with the **absolute** centre, not a displacement:
  `authored + (now − authored)` is not always `now` in floating point, and
  the table's position column must reach every reader exactly as it reaches
  the renderer (`MoverTable.publish`).
* `brush_placement.PREVIEW`, the editor's mover preview, a
  `RuntimeOverrides` (tests use their own: `tests/helpers/runtime.py`).

A source keeps a dict, `runtime_entries`: `id(brush) → (brush, transform)`
(the brush itself, so a reused id never answers for another brush). The
resolver reads it directly, since the question is asked in every broad
phase; a source changes it only by single dict operations or by replacing
the whole dict (the mover table builds a rebuilt group's entries aside and
swaps them in), so a reader on the AI thread never sees half of an update.
Only a brush flagged `is_mover` or `is_door` is looked up, so every other
brush pays two dict reads. `runtime_of(brush)` returns the first source's
answer or `None`.

#### Writers removed and readers moved

| Before | Stage 2b |
|---|---|
| `MoverTable` wrote `pos` per moved row, `_rot_angle`/`rotation_yaw` per spun row, `original_pos` lazily | columns only; `_publish` refreshes the runtime tuple |
| path movers wrote `pos` (`LogicMovers._update_mover_path`) and the `FollowPath` input read it | `MoverTable.move_to` / `position_of` |
| `_init_movers`/`_init_doors`, the `Open` input and the logic wizard wrote `original_pos`; stop restored `pos` from it | nothing written; stop drops the runtime transforms (`MoverTable.reset`) and journals the move |
| savegame capture read `pos`/`_rot_angle`; restore wrote them | capture reads the resolver; restore calls `MoverTable.restore_placement` (applied now, or when the row is built). Older saves carry the same `pos` (the centre in play) and `rot`: same mapping |
| parented lights and portals read `pos` and `rotation_yaw` | `placed_box` and the runtime spin |
| editor mover preview wrote `pos` and `_rot_angle` | `PREVIEW.set(brush, centre=…, spin=…)`; stop clears it; the 2D views draw a previewed mover at its runtime centre (an angled one too, which before did not move in 2D) |
| deathmatch plugin read `pos` (live) and `original_pos` | `plugins.api.placement` (live centre / authored); its navigation hands its box tests view copies at the live centre |
| `trigger` `TouchTest` input read `pos`/`size` | `world_bounds` |
| serialiser kept the play keys | dropped on save and undo; dropped at load (`drop_play_state`: a box saved mid-motion goes back to its `original_pos`; a streamed world's dormant records too, and are re-filed) |

The write-site ratchet (`tests/brush_model/write_sites.json`) goes down:
`_rot_angle`, `original_pos` and `rotation_yaw` writes 12 → 0; `pos` writes
in the mover and play paths 20 → 2 (the editor's own placement tools). One
write is new: `drop_play_state` putting a box saved mid-motion back at
`original_pos` when an older map is loaded.

#### Plugin API 1.8.0

`plugins.api.placement(brush)` → `Placement(authored, centre, spin, half,
bounds)`. A plugin that read a moving brush's live position from
`brush['pos']`, its rest from `original_pos` or its spin from
`_rot_angle`/`rotation_yaw` reads `placement` instead (documented in
`plugins/API.md`); every other plugin is unaffected. The deathmatch plugin
declares 1.8.0.

#### Big World

A mover parked mid-motion keeps its runtime transform in the mover table;
its record (and Big World's cell delta) carries only its authored `pos`, so
its home cell never follows it. Parking and reactivation change visibility
only; no second copy of where it is exists anywhere.

#### Keeping the hot paths at their old cost

Publishing a transform instead of writing a list into the brush, and asking
a source instead of reading `pos`, are both per-row costs on paths that run
every tick, so each was measured and trimmed:

* **Publication.** One row (the small groups' row-by-row pass, a path
  mover, a restore) is published from the scalars the step already holds
  (0.9 µs; through NumPy it was 6.7 µs). Many rows are packed with a few
  NumPy calls and stored by `dict.update` over `zip`/`map`, so the tuples
  are built and stored in C; a row that spins and moves is published once,
  not once for each. A mover's output fired mid-step still sees its new spin
  (it is published before the output, as the old loop wrote `_rot_angle`
  first).
* **The turned box.** `turned_half` keeps `|R|` per `(axis, angle)`
  (bounded at 4,096 entries), so a spinner's trigonometry runs once per
  angle however many broad phases ask for its box. Same values to the bit.
* **Collision rows.** `CollisionRows` (logic thread only, rebuilt whenever
  the world epoch moves) keeps each mover's authored box and re-derives a
  mover's row only when its runtime tuple changes (sources replace the tuple
  whenever a brush moves, so `is` decides). A second hitscan or projectile
  query in the same tick costs 15 µs for 64 movers instead of Stage 1's
  53 µs. The physics cell rows are refreshed from both the logic and the AI
  thread, so they keep no such state; their refresh is one gather of
  `world_bounds` instead of per-element NumPy writes.
* **The AABB cache.** A box mover's key is its runtime tuple (or `'still'`);
  a plain box's key and its four flag tests are unchanged from 2a, with the
  flags tested by membership first.

Tests hold the fast paths to the plain ones:
`test_publication_is_the_same_row_by_row_or_in_bulk` (movers, doors and a
brush that is both, turned, displaced and at rest; values and types),
`test_collision_rows_follow_movers_and_skip_when_nothing_moved` (bit-equal
to a fresh build after 1, 13 and 40 ticks; no work when nothing moved; only
moved rows re-derived), `test_stopping_and_rebuilding_rows_swaps_the_transforms_whole`
and `test_a_turned_box_is_the_same_from_the_turn_memo`.

#### Validation

* **Full suite** (tests + plugins, llvmpipe, `50a4f32`): 7,099 passed, 150
  skipped, 1 xfailed, 8 failed. Five failures are the baseline's (play-cycle
  leak, editor chrome, three portal-fade). The other three were 2b's: one
  test reading the mover table's old attribute name (fixed in `50a4f32`
  itself, after the run had collected it), and two found by the run and
  fixed in `c4551b0` -- the collision rows skipped a mover whose runtime
  transform had not changed, so a `pos` written during play without an
  editor transaction (a console `setprop`, a plugin) was missed by hitscan
  while every other reader followed it; and a deathmatch test put its lift
  at the far landing by writing `pos` (it now asks the mover table). On
  `50a4f32` + `c4551b0` those three pass; the five baseline failures remain.
  One more Stage 2 target is a regression test (2 → 1 xfail).
* `tests/brush_model/test_runtime_transform.py` (18 tests): play writes
  nothing into any brush (linear movers, spinners, doors, path movers,
  angled movers); rendering (published and re-resolved), the AABB cache,
  collision rows, the physics grid, saved games and parented lights see a
  mover where the table put it, to the bit; stop returns everything home;
  saved games round-trip mid-motion, older saves restore through the same
  mapping, including a restore before the rows exist; a map saved in play
  carries no play state and an older one that did loads as authored; Big
  World parks a mover mid-motion and wakes it where the table has carried
  it, its home cell unchanged; the editor preview; the plugin accessor and
  the deathmatch navigation; and the fast paths above.
* The mover-table equivalence oracle
  (`tests/logic/test_mover_table_equivalence.py`) still compares the table
  with the per-brush loop it replaced, now through the resolver, and asserts
  the table writes no brush.
* GPU: the frozen M1/M2 reference is unchanged; its bridge
  (`tests/helpers/legacy_view.py`) tells each layout copy its original, so a
  runtime source answers for the copy as for the live brush, and hands a
  geometry snapshot over at its authored placement. Angled-brush draw and
  pixel parity (36 + 18 cases), the shared-storage editing session (a
  transform-only move uploads 0 geometry and 0 indices, one transform slot)
  and the shared-cube and projection equivalence tests pass.
* The write-site ratchet and the semantics snapshot of the two 128-unit
  maps are lowered and re-recorded (above).

#### Measurements

Same machine; interleaved with `56ec0e3` (Stage 1) and `a9a5340` (2a), best
of three rounds, one pinned core.

| | Stage 1 | 2a | 2b |
|---|---|---|---|
| mover tick: 8 + 8 doors | 120.9 µs | 122.1 µs | 140.6 µs |
| mover tick: 32 + 32 doors | 167.1 µs | 169.8 µs | 212.5 µs |
| mover tick: 128 + 128 doors | 234.6 µs | 226.1 µs | 335.6 µs |
| publish to the render table, 8 / 32 / 128 | 19.1 / 22.6 / 33.5 µs | 19.1 / 22.5 / 32.9 µs | 19.8 / 23.4 / 37.0 µs |
| collision rows refresh after a tick, 64 movers | 53.0 µs | 90.4 µs | 99.5 µs |
| … a further query in the same tick | 53.0 µs | 90.4 µs | 15 µs |
| AABB cache, moving box mover: hit / miss | 0.16 / 0.69 µs | — | 0.46 / 1.47 µs |
| AABB cache hit, plain box / angled | 0.18 / 0.18 µs | 0.22 / 0.28 µs | 0.25 / 0.34 µs (micro: 0.168 vs 0.218) |
| AABB cache miss, plain box | 0.87 µs | 0.92 µs | 0.99 µs |
| AI wall test per query | 2.12 µs | 2.30 µs | 2.38 µs |
| RenderTable warm sync / one row, BigWorld | 47.6 / 0.08 ms | 46.2 / 0.08 ms | 47.2 / 0.08 ms |
| grid populate / collision rows build / Big World index, BigWorld | 19.6 / 2.31 / 4.37 ms | 20.1 / 2.53 / 4.24 ms | 20.9 / 2.71 / 4.31 ms |
| player tick p50: floor / 8 static angled | 7.6 / 21.2 µs | 7.7 / 21.1 µs | 7.7 / 21.0 µs |
| player tick p50 near 8 moving box movers / angled movers / spinning boxes | 24.5 / 28.2 / 33.2 µs | 26.9 / 42.5 / 57.2 µs | 38.3 / 44.7 / 48.1 µs |
| BigWorld heap per box; RenderTable bytes per row | 1,358.2 B; 353 B | 1,358.2 B; 353 B | 1,358.2 B; 353 B |
| still frame p50, BigWorld / `_SHOWCASE` (two rounds each) | 8.1, 10.7 / 10.1 ms | — | 10.3, 11.1 / 9.6 ms |

The mover tick is the one real cost: 0.35 µs more per moving row, 20 µs at
the size of a typical level's movers. It is what replacing a write of the
moved brush's `pos` list with a published transform tuple costs (building
the tuple and freeing the last tick's, through `dict.update`) plus a few
NumPy calls per group; the tick is otherwise unchanged. Every reader that
asks for a moving brush pays one dict lookup per source instead of reading
`pos` (a moving mover's cached AABB: +0.3 µs), and repeated queries within a
tick are cheaper than before. The player is the reader that feels it most:
near 8 moving box movers its tick is 14 µs slower than Stage 1's (19 runtime
lookups and 8 cache refreshes per tick, at 0.46 µs a hit and 1.47 µs a miss
against 0.16 / 0.69 µs). Stage 2c replaces the player's mover collision
(each moving brush collides by its shape in its own frame), so the lookup is
re-measured there rather than tuned here. (The benchmark drives 2b's movers
through a runtime source, as the mover table does; Stages 1 and 2a are driven
by writing `pos` and `_rot_angle`, as their movers were.) Box paths are at
Stage 1 levels: memory
identical, sync and rendering within noise (Stage 1's own still frame varies
from 8.1 to 10.7 ms between identical runs). The bench's "loader adds"
figure for `_SHOWCASE` doubled in 2a (1,373 → 2,870 B per brush) because
the loader now resolves each angled brush's shape, which the first frame
built anyway: after the load and the first table sync the heap is 4,904 B
per brush on Stage 1 and 5,065 B on 2b (+3 %, the shape's cached box and
bounds); box maps are unchanged (352.9 / 352.7 B).

### 11.3 Stage 2c — collision by the shape, in its frame

#### What collides how

`brush_placement.collision_shape(brush)` is the one answer to "what does
the player collide with": `(planes, triangles, bounds, frame)`, or `None`
for the AABB fast path.

| Brush | Collides as | Frame |
|---|---|---|
| angled, still | its planes (`ConvexGeometry.collision_shape`, built once per shape) | a translation by its placement (Stage 2d: local planes) |
| angled mover or door | its planes | its runtime transform: `s = A + Rᵀ(w − P)` |
| spinning box | its six planes at its authored placement | its runtime transform |
| entity model's collision mesh proxy | its world-space mesh | none |
| any other box -- static, or carried without turning | its derived AABB, which is its exact shape | -- |

A world query (the capsule's spheres) is carried into the shape's frame,
tested against the shape's planes there, and the push-out normal is carried
back (`Frame.to_shape`, `Frame.to_world_dir`: a few float operations; no
matrix is built per query). Stage 1 collided an angled mover as its box
(the triangles were baked only for still brushes) and a spinning box as its
unturned box; 2a made both the AABB of the turned box. The player now
stands on, walks into and is carried by each as the shape it is drawn as. The brush that pushed the player up is its ground
object, so a mover carries what it holds (linear carry, unchanged; angular
carry is out of scope).

#### No rebuild per tick, no allocation per query

* The shape's collision data (planes with unit normals, swept-sphere
  triangles, bounds) is built once per `ConvexGeometry` and shared;
  `build_collision_mesh` hands out the same lists.
* `collision_shape` caches its whole answer on the brush
  (`_collision_entry`, stripped on save) against what it is built from: the
  runtime transform tuple (which a source replaces whenever the brush
  moves, so `is` decides), the cached shape (dropped by every geometry
  edit) and the placement. The player's several questions per tick build
  it once per move; a brush that does not move never builds it again.
  Rotations are memoised per `(axis, angle)`.
* The capsule's broad reject is the tight axis-aligned test whenever the
  frame only translates, and the circumscribed-radius bound only for a
  turned frame.

`tools/bench_collision.py` counts the rebuilds while it times the player:
over 900 ticks next to eight shapes, **no convex solve and no collision-shape
build in any scenario**; a frame is built once per moving shape per tick
(once per move), never per query.

#### Remaining approximations (recorded; Stage 2e)

Hitscan, projectiles, AI line of sight and walls, and physics bodies still
test every brush as its derived AABB -- the turned box's AABB for a spinner,
the planes' AABB for an angled brush, both where the brush is now. The
player's step-up and un-duck headroom tests also use those AABBs. These are
shape approximations on a correct placement, not disagreements about where
a brush is.

#### Validation

* `tests/brush_model/test_collision_oracle.py`: an oracle independent of the
  collision code builds each effective world shape from the authored shape
  and the runtime transform with unit quaternions (not the engine's
  matrices). Along a mover's whole path, translating and spinning, for
  angled brushes and boxes: the player's depenetration in the shape's frame
  lands the capsule where the same rule on the oracle's world half-spaces
  does and never moves a capsule the solid does not touch; a point inside a
  moving ramp's AABB but outside the ramp is not pushed; every AABB consumer
  contains the true shape; the editor's face pick enters where the oracle's
  ray does; frames are built once per move and no rotation per query.
* The known-inconsistency target "a convex mover's collision follows its
  transform" is a regression test (1 → 0 xfail).

#### Measurements

Player tick next to eight shapes (`tools/bench_collision.py`, one pinned
core, best of three, interleaved with Stage 1):

| next to | Stage 1 p50 / p99 | Stage 2 p50 / p99 | solves | shape builds | frames per tick |
|---|---|---|---|---|---|
| 8 static boxes | 11.2 / 29.8 µs | 12.6 / 36.7 µs | 0 | 0 | 0 |
| 8 still angled brushes | 20.1 / 43.5 µs | 29.0 / 63.9 µs | 0 | 0 | 0 |
| 8 box movers | 24.9 / 48.4 µs | 43.6 / 74.4 µs | 0 | 0 | 0 |
| 8 angled movers | 29.1 / 53.7 µs | 71.0 / 109.0 µs | 0 | 0 | 8 |
| 8 spinning boxes | 33.0 / 57.2 µs | 80.8 / 128.0 µs | 0 | 0 | 8 |
| 8 spinning angled brushes | 28.6 / 51.9 µs | 72.8 / 113.7 µs | 0 | 0 | 8 |

(Stage 2 is the head of the branch, 2c and 2d together: a still angled
brush collides through its local frame. Retained allocations per tick:
0.04–0.24 blocks in every scenario on both.)

The moving and spinning rows are where the cost is: they now collide by
their true shape -- sphere against planes, in the shape's frame -- where
Stage 1 collided an angled mover as its box and a spinner as its unturned
box. Box movers pay 2b's runtime lookups (19 per tick) and per-tick cache
refreshes. Static boxes are within 1.4 µs of Stage 1; still angled brushes
cost 9 µs more (+44 %): since 2d each is collided through its translation
frame and its cached collision answer is looked up every tick. On a slow core (the Snapdragon 8cx Gen 3 target)
these scale with single-thread speed; the collision path is pure Python on
the logic thread and allocates nothing per query, so the counts above, not
the x86 timings, are the portable result.

### 11.4 Stage 2d — local-frame planes, map version 5

#### The layout

An angled brush's planes are stored in its **local frame**, about their own
centre, and `pos` places them: the shape is `ConvexGeometry(planes)` and the
world solid is that solid carried by `pos`. So a translation is a placement
and never a shape edit -- the same rule as a box. `brush_geometry.local_convex`
is the shape (cached, `_geo_cache`, rebuilt only when the planes change);
`get_convex` is the world view, the shape translated (`ConvexGeometry.translated`:
no solve), cached per placement.

* `translate_brush` writes `pos` and nothing else: no plane rewrite, no
  winding solve, no mesh rebuild; the renderer sees a transform.
* `clip_brush` takes a world plane and cuts in the local frame
  (`d′ = d − n̂·pos`); rotate, fit, component edits and CSG commit world or
  local planes explicitly. A commit re-centres the planes on their new
  bounds when the edit moved them (beyond 10⁻⁹ of the extent), moving `pos`
  by the same amount, and the cached shape is then the stored planes solved
  -- exactly what a reload solves -- so what is drawn never depends on edit
  history.
* The mesh is built in the local frame (its anchor is the local shape's
  bounds), so its key is placement-free. The key also carries the brush's
  id (`GeometryRecord.owner`): two brushes of one shape keep two GPU meshes
  and two transform slots, as M2 batches them, while a move, an undo of a
  move or a reload of the same brush finds its mesh.
* **D7 is fixed**: an angled brush is solved about the origin, so one at
  9×10⁵ units solves (world-space planes solved to nothing beyond ~2.6×10⁵).

#### Map version 5

`EditorState.get_level_data` writes `version: 5`. Loading a map below
version 5 localises each angled brush (`brush_placement.localise_legacy`):
its world planes are re-expressed about their solved centre `c`
(`d′ = d − n·c`) and `pos` becomes `c`, so it draws exactly where it did and
a stale `pos`/`size` copy (two shipped brushes, 128 units) cannot misplace
it. A streamed Big World map's records are localised resident or not, and
re-filed at home. Every shipped map (version 3) loads to the solids its file
describes (corners within 10⁻¹² of their magnitude), and its version-5 save
reloads bit for bit.

One deviation from §7: version 5 **keeps `size`** for an angled brush, as a
derived copy re-derived from the planes on load and never read for a bound.
Dropping it broke loading (the validator, now relaxed, required it), would
lose the fallback box an invalid plane set is drawn by, and gains nothing
since the loader ignores it.

#### Older builds and the Android player

* `brush_placement.export_world_level` writes the version-4 layout
  (world-space planes and `size`): lossless. `brush_faces.export_legacy_level`
  goes on to version 3 (Stage 1's per-tag layout) from it.
  `tools/export_legacy_map.py --version 4|3` writes either; a map already at
  or below the requested version is copied unchanged.
* **Android**: the player (not modified) reads world-space planes. The
  desktop package exporter writes a version-5 map into the `.fiopak` as its
  version-4 export; older maps are copied byte for byte. Tested with the
  player's own package reader: the packaged map is version 4 and its planes
  solve to the editor's world solids. Release dependency: the player needs
  this exporter (or an update) before it is handed version-5 files directly.

#### Behaviour changes

1. A `pos` write moves an angled brush (it moved nothing in 2a, where `pos`
   was a copy): the editor's own move, a console `setprop`, a plugin.
   `size` is the copy now.
2. An editor move, an undo or redo of a move and a save/reload rebuild no
   mesh and upload no geometry (2a rebuilt the moved brush's mesh).
3. Rotating a brush about its centre is one solve, as before Stage 2d
   (0.79 ms for 7 planes against Stage 1's 1.03 ms); an asymmetric brush whose
   box centre moves as it turns is re-centred, two solves (2.40 ms against
   1.32 ms for 10 planes).
4. The two clipped terrain brushes' `pos`/`size` are their planes' bounds
   once loaded (they were 128 units off in the file).

#### Validation

* `tests/brush_model/test_local_frame.py` (32 tests): the version-5 layout;
  save → load → save gives the same document and bit-identical solids; every
  shipped map migrates to its file's solids and its v5 save reloads bit for
  bit; the stale terrain copies; 1,000 editor moves with no solve and the
  same shape, collision data and mesh record; the mesh key; clip and
  rotation after a move against world-space references; undo of a move
  restores the placement alone; the v4 and v3 exports and the tool.
* **1,000 consecutive movements** (`tests/visual/test_geo_shared_storage.py`):
  editor translations and movers' runtime transforms, alternately, each
  published and drawn through the real pipeline, each moved brush asked for
  its collision shape as the player would. After the first frames: no
  convex solve, no geometry upload, no index-list upload, no mesh
  allocation, no collision-shape build -- only transform slots -- and both
  oracles hold at the end.
* GPU parity: the frozen reference's bridge hands it each brush's stored
  planes (it reads geometry through `get_convex`, which places them); its
  draw oracle treats a vertex value within 10⁻⁹ of zero as zero, since the
  engine projects local corners and the reference world ones and an exactly
  zero texture coordinate comes out as up to ~10⁻¹² in either. The pixel
  oracle is unchanged and exact: 0 differing pixels.
* Package export (`tests/persistence/test_package_export.py`), map
  round trip and compatibility tests updated to version 5.
* Tests that pinned 2a's "pos is a copy" are restated for 2d (named in the
  commit), and the semantics helpers learnt that a shipped record's planes
  are world space (`localised`, `loaded`, `local_rest`).

### 11.5 Stage 2 integrated

The branch from Stage 1 (`56ec0e3`): 2a `a9a5340`; 2b `50a4f32` (+ `1e88a24`,
two stray e2e maps removed, and `c4551b0`); 2c `040ac72` (+ `6edf6db`); 2d
`fe1385f` (+ `a445d9b`, `f0d83ef`); `e6c80ad` (mover refresh, one rotation
memo).

#### Validation

* **Full suite** on `f1556d0` (after the performance work of §11.6), under
  xvfb with llvmpipe: 7,221 passed, 150 skipped, 5 failed. The five are
  the known baseline -- `test_repeated_play_sessions_leak_no_threads_or_memory`,
  `test_p_opens_the_position_window_from_anywhere` and three
  `test_the_portal_view_shows_at_the_fades_opacity` cases -- and fail the
  same way on Stage 1 (`56ec0e3`) in this environment. Included: 318
  GL-marked tests (the draw and pixel oracles among them), 541 brush-model
  tests (map compatibility, the collision oracle, local frames), 277
  persistence tests (save/load, package and Android export), 146 Big World
  tests.

* **Pixel parity** (`tools/compare_map_pixels.py --base` Stage 1): all 20
  shipped maps identical in all 120 views -- every angled brush loaded
  through the local-frame migration, the two 128-unit terrain brushes
  included.
* **GPU draw and pixel oracles** against the frozen M1/M2 reference
  (unmodified): angled-brush draws and pixels in every cull mode, editor and
  play, elements / multidraw / off (54 cases); the shared-storage editing
  session; the shared-cube and projection equivalence tests; the Stage 1
  face-semantics GL tests.
* **Collision oracle** (§11.3) along translating and spinning paths.
* **1,000 consecutive movements** (§11.4): no convex solve, no geometry or
  index upload, no mesh allocation, no collision-shape build after the first
  frames; both oracles hold.
* **Save/reload, map compatibility, legacy export and Android**: version-5
  round trip bit for bit; every shipped map migrates to its file's solids;
  v4/v3 exports load back to the same solids; the package exporter hands
  the player version 4.
* **Big World**: records localised resident or not and filed at home; a
  mover parked mid-motion keeps its transform in the mover table and its
  home cell; saved games restore runtime transforms mid-motion.

#### Mandatory requirements

| Requirement | Result |
|---|---|
| Shared cube VBO, instanced AABB path | unchanged; `test_aabb_shared_cube` passes |
| M2 batching unchanged | one mesh and one transform slot per brush (the mesh key carries the brush's id); batched draw counts asserted in the run-parity tests |
| AABB memory at Stage 1 levels | BigWorld heap per box 1,358.2 B and RenderTable 353 B per row, as Stage 1 |
| AABB CPU at Stage 1 levels | plain box cache hit 0.17 → 0.23 µs (the four flag tests that make angled and moving bounds right); miss 0.86 → 0.95 µs |
| Mesh cache hits and misses identical | a reorder, undo or redo rebuilds nothing; an editor move or undo of one rebuilds nothing (2a rebuilt one) |
| GPU draw oracle and collision oracle pass | yes |
| Big World authored home cell vs live occupancy | home cell from authored bounds; a parked mover keeps its runtime transform |
| Runtime transforms survive saved games and streaming | yes (mid-motion save/restore, parking and reactivation) |

#### Measurements against Stage 1

The state at `e6c80ad`, before the performance work: §11.6 supersedes these
figures. Same machine, interleaved, best of three on one pinned core (`56ec0e3` vs
the head):

| | Stage 1 | Stage 2 |
|---|---|---|
| BigWorld heap per box; RenderTable bytes per row | 1,358.2 B; 353 B | 1,358.2 B; 353 B |
| `_SHOWCASE` heap per brush, parsed, loaded and first sync | 6,885 B | 7,100 B (+3.1 %) |
| RenderTable warm sync / one row, BigWorld | 46.7 / 0.080 ms | 46.4 / 0.080 ms |
| physics grid populate, BigWorld / `_SHOWCASE` | 19.9 / 0.92 ms | 20.8 / 1.21 ms |
| collision rows build, BigWorld | 2.33 ms | 2.71 ms |
| Big World index, BigWorld | 4.22 ms | 4.28 ms |
| still frame p50, BigWorld / `_SHOWCASE` (draw calls 1 / 47) | 12.7 / 11.1 ms | 12.5 / 12.5 ms (within noise) |
| editor move / undo p50, BigWorld | 16.7 / 87.8 ms | 17.7 / 92.5 ms |
| AABB cache hit: plain box / angled; miss | 0.17 / 0.17; 0.86 µs | 0.23 / 0.33; 0.95 µs |
| AI wall test per query | 2.10 µs | 2.58 µs |
| mover tick, 8 / 32 / 128 movers and as many doors | 121 / 171 / 225 µs | 135 / 211 / 317 µs |
| publish to the render table, 8 / 32 / 128 | 19.0 / 22.6 / 33.0 µs | 19.6 / 23.3 / 35.7 µs |
| moving mover's cached AABB, per query (20 per tick) | 0.32 µs | 0.88 µs |
| collision rows refresh, 64 movers: first after a tick / again | 57 / 51 µs | ~150 / 62 µs |
| editor rotation step, 7 / 10 planes | 1.03 / 1.32 ms | 0.79 / 2.40 ms |
| player tick next to 8 moving / spinning shapes | §11.3 | §11.3 |

#### Remaining regressions

At `e6c80ad`; for the state after the performance work see §11.6.

1. **Mover tick**: +14 / +40 / +91 µs at 8 / 32 / 128 movers (and as many
   doors) -- publishing a runtime transform per moving row instead of a
   `pos` list.
2. **Readers of a moving brush** pay a runtime lookup: a moving mover's
   cached AABB 0.32 → 0.88 µs per query; the collision rows' first refresh
   after a tick ~150 µs against 57 (runtime lookups, and every spinner's
   turned box, which Stage 1 ignored); a repeated query in the tick 62
   against 51.
3. **Player collision** next to moving and spinning shapes is 2–3× Stage 1
   (true-shape collision where Stage 1 used the wrong box); next to box
   movers +75 %, next to still angled brushes +44 %.
4. **Static paths**: a plain box's cached AABB +0.05 µs, the AI wall test
   +0.5 µs, grid populate +4 % (BigWorld) and +31 % (`_SHOWCASE`, angled
   brushes placed through their local box), collision rows build +16 %.
5. **Editor rotation** of an asymmetric angled brush is two solves (2.40 ms
   against 1.32 ms for 10 planes); about its centre a symmetric one is one
   solve and faster than Stage 1.
6. **Memory**: +3.1 % per brush on the angled-heavy `_SHOWCASE`; box maps
   unchanged.

#### Limitations

* Stage 2e (follow-up): hitscan, projectiles, AI line of sight and walls,
  physics bodies, and the player's step-up/un-duck headroom tests use each
  brush's derived AABB (correctly placed and turned) rather than its shape.
* Angular carry is out of scope: a spinning platform carries its rider
  linearly only.
* Version-5 maps are not read by builds before Stage 2d: such a build (and
  the Android player) is given the version-4 export
  (`tools/export_legacy_map.py --version 4`; the package exporter does it).

### 11.6 Performance gate against Stage 1

Correctness alone is not the acceptance criterion: Stage 2 must show no
statistically meaningful CPU regression against Stage 1 (`56ec0e3`) in
comparable workloads, with every Stage 2 correctness improvement kept --
true-shape collision, local-frame geometry, the shared cube VBO, OpenGL 3.3
core, and authored placement never written during play.

**Verdict at `f1556d0`: not passed.** Stationary scenes, representative
gameplay, static builds, AI queries and the collision structures are level
with or faster than Stage 1; the player next to moving shapes and a moving
mover's first AABB query after each move are not (§ What remains).

#### Method

`tools/perf_probes.py` measures seven families of workload on a checkout
and keeps every sample; `tools/perf_gate.py` runs them for both checkouts
in fresh processes pinned to one core, alternating base and head (the order
flips every round), and reports p50 / p95 / p99 over all samples, the p50
ratio, and a one-sided exact Mann–Whitney test of "head slower" on the
per-round medians (rounds are the independent unit). A metric **regresses**
when p < 0.05 and the head's p50 is more than 2 % (the noise floor) above
the base's. With five rounds the smallest attainable p is 0.004.

    xvfb-run -a env LIBGL_ALWAYS_SOFTWARE=1 \
        python tools/perf_gate.py --base STAGE1_TREE --head . --rounds 5 --core 3

| Probe | What it measures |
|---|---|
| movers | 8 / 32 / 128 movers and as many doors, 300 ticks: the tick, publication to the render table, the collision rows' and the grid's cell rows' refresh (first after a tick, and again), a moving mover's cached AABB (20 queries a tick), the frame total |
| aabb | `brush_aabb_bounds`: plain and angled hits, plain misses |
| static | BigWorld and `_SHOWCASE`: render-table sync, grid populate, collision-row build, Big World index |
| ai | wall and floor queries, batched point/box queries, line of sight |
| player | the six `tools/bench_collision.py` scenarios: still boxes, still angled brushes, and 8 box, angled, spinning-box and spinning-angled movers beside the player's path (the movers driven by a runtime source; the source's own update is not timed, in either tree) |
| frames | `render_scene`, still and with 32 box movers, BigWorld and `_SHOWCASE` (GL) |
| gameplay | `tools/bench_compare.py`'s production path (load, play, the logic thread stepped by hand with scripted input, the monster AI every other frame, `paintGL`) on `_SHOWCASE`, MonsterTest, BigWorld (34 movers, 40 monsters) and Office_Corridor: per-frame logic, AI, paint and frame time, and Python calls per logic frame |

Measured on the session's x86-64 VM (4 vCPU Xeon @ 2.1 GHz, CPython 3.13,
NumPy 2.5, llvmpipe software GL). **The Snapdragon 8cx Gen 3 / Adreno target
was not available; nothing here is a measurement on it.** The tools run
unchanged on the device.

#### Measurement noise

The VM's timing noise is large, and part of it is systematic, so the
verdicts were calibrated:

* **A/A run** -- Stage 1 against itself, the movers, player, AABB and
  frames probes (37 metrics), five rounds: p50 ratios range 0.84–1.09 with
  no code difference; the test called one metric a regression
  (`player_static_angled`, 1.07, p = 0.048) and sixteen "faster" (a
  consistent 6–16 % bias on the movers probe). A difference below ~10–15 %
  at p ≈ 0.02–0.05 is therefore not evidence on this machine.
* **Per-round spread**: the five round medians of one metric on one tree
  vary by 10–120 % (the Noise column below).
* **Run to run**: two full gates an hour apart, on commits that differ only
  in the carried convex brush's AABB path, flagged 5 and 14 regressions;
  every extra flag was at p 0.008–0.048 and ratio 1.07–1.25, in code the
  change does not touch.
* **Nine rounds** (Stage 1 against `f1556d0`, the same four probes)
  separate the robust results from the noisy ones (§ What remains).
* Python call counts per logic frame are exact (no noise): their round
  medians are identical, so their p-value is the test's floor whatever
  the difference; only the 2 % floor decides them.

#### Where Stage 2 stood (`0c6e6f2`)

The first gate found 21 regressions: the AABB hits (plain 1.29×, angled
1.54×), the AI wall and floor queries (1.26×, 1.34×), the mover tick (1.43 /
1.64 / 2.25× at 8 / 32 / 128), a moving mover's AABB (2.4–2.8×), publication
at 128 (1.11×), the collision rows' and cell rows' first refresh at 8 movers
(1.28×, 1.93×) and the frame total there (1.25×), the static collision-row
build (1.49×), and the player next to still boxes (1.10×), still angled
brushes (1.43×), box movers (1.80×), angled movers (2.30×) and spinners
(2.41–2.48×).

#### What each was, and what removed it

| Regression | Cause (profiled) | Fix | Commit |
|---|---|---|---|
| AABB hit 1.3–1.5× | every call classified the brush and built a key with a runtime lookup before comparing | every brush keyed by pos/size (Stage 1's key); whatever changes a box without changing them drops the key | `2741be4` |
| AI wall / floor queries 1.3× | each query re-classified its cells' brushes and re-derived angled boxes | per-cell static box tuples from the cell rows; movers resolved live | `2741be4` |
| Mover tick at 128: 2.25× | each spinner's turned box through two memo dicts keyed by angle -- always missing | `turned_half_now` (same bits, no memo; a vertical-axis short form); the turned box worked out only when a reader gathers boxes | `951f1c8` |
| Mover tick at 8: 1.43× | the per-row walk stored each row through two one-element arrays, a twin gather and a mask | scalar single-row store | `951f1c8`, `8aa31b6` |
| Mover tick at 32: 1.64× | `union1d`, picks when every row moved, a Python loop for rows back home, `np.unique` on empty sequence points | masks, `compress`, no sort without sequence points | `951f1c8`, `8f37273` |
| Collision-row build 1.49× | the resolver asked for every angled box; the level classified twice | one pass; angled boxes remembered with their AABB; one flat read per column | `8299bf1` |
| Rows / cells first refresh 1.3–1.9× | both structures gathered the same movers every tick through ~20 NumPy calls each | one shared gather per placement stamp; brush by brush below 128 brushes; one block store | `8299bf1` |
| Moving mover's AABB 2.4–2.8× | the key led with the runtime transform, so a hit asked every runtime source | pos/size key for every brush; each stamped runtime change drops the keys of the movers cached since the last | `c3534f7` |
| Grid populate (+10–30 %, from `2741be4`) | the per-cell box rows built eagerly; scalar NumPy stores per brush | built on first query; bulk bounds | `be1673f` |
| Player 1.1–2.5× | shapes asked for twice a tick; every sphere carried into every frame on every pass; a spinner's planes rebuilt every tick; box movers looked up twice | shapes handed on; the player carried in once per shape per pass; planes per placement; one runtime lookup a tick | `08863b5` |
| AABB plain miss (1.1×, from `c3534f7`) | movers sharing the plain key meant four membership tests per miss | tagged keys for angled brushes and movers | `8f37273` |
| Python calls per logic frame (+16 Office_Corridor, +29 `_SHOWCASE`) | the frustum cull's turned-bounds pass every frame with nothing turning; water volumes through the resolver | `RenderTable.turning`; plain water boxes inline | `bad8171` |
| A moving brush resolved by every subsystem separately (+11 calls MonsterTest) | AABB cache, broad phases, step-up and collision shapes each asked the runtime sources | one resolution per placement stamp, in a resolver dict the stamp replaces (nothing written to brushes); hot readers inline | `2cf2734` |
| Carried convex AABB derived its whole authored box | only the shape's half-extents are needed with a transform | read from the cached shape | `f1556d0` |

#### Gate runs

| Head | Regressions | Faster | Same |
|---|---|---|---|
| `0c6e6f2` (before) | 21 | -- | -- |
| `8651c02` | 12 | 19 | 42 |
| `2cf2734` | 5 | 23 | 45 |
| `f1556d0` (final) | 14 | 21 | 38 |

`2cf2734` and `f1556d0` differ only in the carried convex brush's AABB
path, yet the second run flags nine more metrics, all at p between 0.008
and 0.048 and in code the change does not touch (the mover tick and
publication, still boxes, BigWorld frames): see the noise calibration.

#### Final gate, `f1556d0` (all 73 metrics)

Noise is the spread of the five per-round medians (max / min − 1) for each
side.

| Metric | Stage 1 p50 / p95 / p99 | Stage 2 p50 / p95 / p99 | Ratio | p | Noise S1 / S2 | Verdict |
|---|---|---|---|---|---|---|
| `aabb_angled_hit` (µs) | 0.136 / 0.164 / 0.202 | 0.143 / 0.169 / 0.19 | 1.05 | 0.210 | 35 % / 22 % | same |
| `aabb_plain_hit` (µs) | 0.133 / 0.193 / 0.276 | 0.138 / 0.156 / 0.171 | 1.04 | 0.345 | 29 % / 16 % | same |
| `aabb_plain_miss` (µs) | 0.617 / 0.931 / 1.15 | 0.695 / 0.877 / 1.11 | 1.13 | 0.155 | 53 % / 12 % | same |
| `ai_big_batch32` (µs) | 196 / 275 / 624 | 145 / 210 / 628 | 0.74 | 1.000 | 36 % / 18 % | faster |
| `ai_big_line_of_sight` (µs) | 9.49 / 12.2 / 14.3 | 9.68 / 13.5 / 16.1 | 1.02 | 0.345 | 23 % / 17 % | same |
| `ai_big_overlaps_wall` (µs) | 2.25 / 2.88 / 3.38 | 1.05 / 1.41 / 16.4 | 0.47 | 1.000 | 33 % / 17 % | faster |
| `ai_big_raycast_down` (µs) | 1.74 / 2.18 / 2.6 | 0.636 / 0.933 / 1.11 | 0.37 | 1.000 | 30 % / 15 % | faster |
| `ai_show_batch32` (µs) | 182 / 254 / 481 | 177 / 259 / 422 | 0.97 | 0.655 | 36 % / 32 % | same |
| `ai_show_line_of_sight` (µs) | 3.2 / 4.31 / 5.32 | 3.24 / 4.43 / 5.84 | 1.01 | 0.500 | 24 % / 17 % | same |
| `ai_show_overlaps_wall` (µs) | 1.69 / 2.12 / 2.36 | 0.802 / 1.14 / 1.58 | 0.48 | 1.000 | 26 % / 21 % | faster |
| `ai_show_raycast_down` (µs) | 1.94 / 2.31 / 2.49 | 0.72 / 1 / 1.27 | 0.37 | 1.000 | 24 % / 23 % | faster |
| `frame_big_moving_ms` (ms) | 8.01 / 10.7 / 12.8 | 8.6 / 12.3 / 16.7 | 1.07 | 0.048 | 22 % / 15 % | **regression** |
| `frame_big_still_ms` (ms) | 8.29 / 9.94 / 12.3 | 8.43 / 12.6 / 16.2 | 1.02 | 0.421 | 27 % / 43 % | same |
| `frame_show_moving_ms` (ms) | 6.65 / 8.82 / 10.8 | 6.67 / 9.14 / 10.3 | 1.00 | 0.726 | 22 % / 31 % | same |
| `frame_show_still_ms` (ms) | 6.67 / 8.68 / 9.94 | 6.4 / 10.2 / 12.9 | 0.96 | 0.655 | 21 % / 39 % | same |
| `gameplay_big_ai_ms` (ms) | 0.217 / 0.325 / 0.466 | 0.219 / 0.305 / 0.38 | 1.01 | 0.274 | 17 % / 10 % | same |
| `gameplay_big_frame_ms` (ms) | 122 / 148 / 161 | 121 / 143 / 163 | 0.99 | 0.421 | 12 % / 8 % | same |
| `gameplay_big_logic_calls` (calls) | 1248 / 1248 / 1248 | 1241 / 1242 / 1242 | 1.00 | 1.000 | 0 % / 0 % | same |
| `gameplay_big_logic_ms` (ms) | 0.926 / 1.35 / 1.82 | 0.922 / 1.28 / 1.75 | 0.99 | 0.579 | 22 % / 21 % | same |
| `gameplay_big_paint_ms` (ms) | 121 / 147 / 160 | 120 / 142 / 162 | 0.99 | 0.421 | 11 % / 8 % | same |
| `gameplay_monster_ai_ms` (ms) | 0.669 / 1.02 / 1.29 | 0.712 / 1.11 / 1.46 | 1.06 | 0.274 | 33 % / 49 % | same |
| `gameplay_monster_frame_ms` (ms) | 16 / 22.1 / 25.3 | 16.7 / 23.9 / 30.3 | 1.04 | 0.500 | 26 % / 30 % | same |
| `gameplay_monster_logic_calls` (calls) | 942 / 942 / 942 | 944 / 944 / 944 | 1.00 | 0.004 | 0 % / 0 % | same |
| `gameplay_monster_logic_ms` (ms) | 0.609 / 0.96 / 1.21 | 0.628 / 0.976 / 1.15 | 1.03 | 0.345 | 56 % / 74 % | same |
| `gameplay_monster_paint_ms` (ms) | 15.4 / 21.2 / 24.2 | 16 / 23 / 29.2 | 1.04 | 0.500 | 25 % / 28 % | same |
| `gameplay_office_ai_ms` (ms) | 0.00675 / 0.0116 / 0.0218 | 0.00762 / 0.0136 / 0.0265 | 1.13 | 0.210 | 80 % / 65 % | same |
| `gameplay_office_frame_ms` (ms) | 9.55 / 12.1 / 15 | 10.3 / 14.7 / 18.1 | 1.07 | 0.155 | 38 % / 45 % | same |
| `gameplay_office_logic_calls` (calls) | 451 / 451 / 451 | 449 / 449 / 449 | 1.00 | 1.000 | 0 % / 0 % | same |
| `gameplay_office_logic_ms` (ms) | 0.315 / 0.48 / 0.636 | 0.336 / 0.535 / 0.631 | 1.06 | 0.155 | 52 % / 38 % | same |
| `gameplay_office_paint_ms` (ms) | 9.24 / 11.8 / 14.3 | 9.92 / 14.2 / 17.5 | 1.07 | 0.155 | 36 % / 46 % | same |
| `gameplay_show_ai_ms` (ms) | 0.128 / 0.21 / 0.307 | 0.129 / 0.205 / 0.281 | 1.01 | 0.421 | 26 % / 23 % | same |
| `gameplay_show_frame_ms` (ms) | 19.9 / 27 / 32.7 | 20.9 / 26 / 29.9 | 1.05 | 0.500 | 19 % / 20 % | same |
| `gameplay_show_logic_calls` (calls) | 485 / 485 / 485 | 488 / 488 / 489 | 1.01 | 0.004 | 0 % / 0 % | same |
| `gameplay_show_logic_ms` (ms) | 0.414 / 0.653 / 0.879 | 0.417 / 0.637 / 0.804 | 1.01 | 0.726 | 49 % / 51 % | same |
| `gameplay_show_paint_ms` (ms) | 19.5 / 26.2 / 32 | 20.4 / 25.3 / 29.4 | 1.05 | 0.500 | 19 % / 20 % | same |
| `movers128_aabb_moving` (µs) | 0.349 / 0.579 / 1.09 | 0.47 / 0.913 / 2.07 | 1.35 | 0.004 | 18 % / 36 % | **regression** |
| `movers128_cells_again` (µs) | 129 / 178 / 234 | 0.335 / 0.465 / 0.698 | 0.00 | 1.000 | 17 % / 24 % | faster |
| `movers128_cells_first` (µs) | 132 / 179 / 221 | 11.3 / 19.8 / 31.5 | 0.09 | 1.000 | 16 % / 29 % | faster |
| `movers128_frame_total` (µs) | 766 / 1.03e+03 / 1.42e+03 | 430 / 777 / 1.39e+03 | 0.56 | 1.000 | 14 % / 35 % | faster |
| `movers128_publish` (µs) | 27.1 / 46.7 / 74.3 | 30.6 / 61.6 / 95.4 | 1.13 | 0.155 | 12 % / 31 % | same |
| `movers128_rows_again` (µs) | 119 / 166 / 222 | 0.555 / 0.82 / 1.39 | 0.00 | 1.000 | 14 % / 25 % | faster |
| `movers128_rows_first` (µs) | 127 / 190 / 266 | 111 / 192 / 319 | 0.87 | 0.996 | 14 % / 30 % | faster |
| `movers128_tick` (µs) | 207 / 328 / 465 | 259 / 520 / 952 | 1.25 | 0.016 | 14 % / 36 % | **regression** |
| `movers32_aabb_moving` (µs) | 0.256 / 0.569 / 0.797 | 0.401 / 0.734 / 1.31 | 1.57 | 0.004 | 27 % / 24 % | **regression** |
| `movers32_cells_again` (µs) | 32.3 / 49.2 / 69.3 | 0.315 / 0.424 / 0.598 | 0.01 | 1.000 | 31 % / 13 % | faster |
| `movers32_cells_first` (µs) | 34.2 / 53.8 / 82.5 | 8.58 / 14.2 / 23.1 | 0.25 | 1.000 | 31 % / 23 % | faster |
| `movers32_frame_total` (µs) | 318 / 556 / 673 | 252 / 439 / 595 | 0.79 | 0.992 | 30 % / 30 % | faster |
| `movers32_publish` (µs) | 17.9 / 36.4 / 61.1 | 20 / 37.7 / 59.9 | 1.11 | 0.048 | 27 % / 25 % | **regression** |
| `movers32_rows_again` (µs) | 38.5 / 65.3 / 97.4 | 0.46 / 0.658 / 0.851 | 0.01 | 1.000 | 27 % / 14 % | faster |
| `movers32_rows_first` (µs) | 42.4 / 72.9 / 108 | 48.8 / 85.8 / 133 | 1.15 | 0.008 | 29 % / 23 % | **regression** |
| `movers32_tick` (µs) | 139 / 276 / 393 | 163 / 277 / 399 | 1.17 | 0.016 | 27 % / 33 % | **regression** |
| `movers8_aabb_moving` (µs) | 0.25 / 0.365 / 0.829 | 0.356 / 0.545 / 0.753 | 1.42 | 0.004 | 17 % / 32 % | **regression** |
| `movers8_cells_again` (µs) | 8.76 / 13 / 24.5 | 0.289 / 0.383 / 0.659 | 0.03 | 1.000 | 22 % / 19 % | faster |
| `movers8_cells_first` (µs) | 10.2 / 14.6 / 26.6 | 5.69 / 9.13 / 15.4 | 0.56 | 1.000 | 18 % / 46 % | faster |
| `movers8_frame_total` (µs) | 187 / 284 / 340 | 108 / 169 / 244 | 0.58 | 1.000 | 22 % / 41 % | faster |
| `movers8_publish` (µs) | 16 / 25.5 / 44.9 | 17.3 / 32.6 / 50.1 | 1.08 | 0.210 | 22 % / 48 % | same |
| `movers8_rows_again` (µs) | 17.9 / 27.5 / 47.8 | 0.41 / 0.582 / 0.861 | 0.02 | 1.000 | 21 % / 15 % | faster |
| `movers8_rows_first` (µs) | 21.4 / 34.6 / 54.6 | 22.3 / 37.2 / 63.8 | 1.05 | 0.274 | 22 % / 41 % | same |
| `movers8_tick` (µs) | 104 / 169 / 207 | 52.7 / 83.9 / 132 | 0.51 | 1.000 | 22 % / 34 % | faster |
| `player_angled_movers` (µs) | 23.8 / 35.9 / 50.4 | 51.2 / 72.5 / 94.3 | 2.15 | 0.004 | 13 % / 14 % | **regression** |
| `player_box_movers` (µs) | 20.3 / 40.1 / 56.2 | 35.8 / 56.6 / 77.9 | 1.77 | 0.004 | 23 % / 26 % | **regression** |
| `player_spinning_angled` (µs) | 24.2 / 39.6 / 59 | 50.7 / 74 / 95.6 | 2.09 | 0.004 | 16 % / 11 % | **regression** |
| `player_spinning_boxes` (µs) | 26.2 / 39.7 / 55.9 | 61.5 / 89.6 / 122 | 2.34 | 0.004 | 16 % / 11 % | **regression** |
| `player_static_angled` (µs) | 16.4 / 23.3 / 40.7 | 20.2 / 36.3 / 77.7 | 1.23 | 0.004 | 19 % / 45 % | **regression** |
| `player_static_boxes` (µs) | 9.26 / 13.6 / 30.3 | 9.86 / 15.5 / 33.9 | 1.07 | 0.028 | 14 % / 32 % | **regression** |
| `static_big_bigworld_index_ms` (ms) | 3.85 / 4.65 / 5.33 | 3.87 / 4.3 / 4.41 | 1.00 | 0.655 | 19 % / 32 % | same |
| `static_big_collision_rows_ms` (ms) | 2.28 / 2.72 / 3.36 | 1.41 / 1.65 / 1.83 | 0.62 | 1.000 | 17 % / 29 % | faster |
| `static_big_grid_populate_ms` (ms) | 17.5 / 24 / 28 | 16.6 / 20.1 / 21.6 | 0.95 | 0.984 | 39 % / 26 % | faster |
| `static_big_rt_sync_ms` (ms) | 42.4 / 58.4 / 59.6 | 41 / 56.9 / 58.7 | 0.97 | 0.655 | 16 % / 42 % | same |
| `static_show_bigworld_index_ms` (ms) | 0.129 / 0.178 / 0.194 | 0.143 / 0.2 / 0.239 | 1.10 | 0.111 | 24 % / 35 % | same |
| `static_show_collision_rows_ms` (ms) | 0.0502 / 0.0837 / 0.0965 | 0.0503 / 0.091 / 0.126 | 1.00 | 0.726 | 29 % / 34 % | same |
| `static_show_grid_populate_ms` (ms) | 0.695 / 0.857 / 0.903 | 0.713 / 0.902 / 0.92 | 1.03 | 0.274 | 30 % / 35 % | same |
| `static_show_rt_sync_ms` (ms) | 1.75 / 2.04 / 2.08 | 1.83 / 2.12 / 2.2 | 1.04 | 0.274 | 30 % / 35 % | same |


73 metrics: 14 regressions, 21 faster, 38 same
regressions: frame_big_moving_ms, movers128_aabb_moving, movers128_tick, movers32_aabb_moving, movers32_publish, movers32_rows_first, movers32_tick, movers8_aabb_moving, player_angled_movers, player_box_movers, player_spinning_angled, player_spinning_boxes, player_static_angled, player_static_boxes

#### Collision ablation

The player scenarios in three configurations, interleaved, best of five
rounds (p50 µs per player tick): Stage 1; Stage 2 with movers and doors
collided as their derived AABB (Stage 1's semantics for moving shapes --
true shape is kept for still angled brushes, as Stage 1 kept it); Stage 2
as shipped, true shape throughout. The ablation is an experiment only: no
production path collides a moving shape as its box.

| Scenario | Stage 1 | Stage 2, moving shapes as AABB | Stage 2, true shape | Engine overhead (AABB − Stage 1) | True-shape work (true shape − AABB) |
|---|---|---|---|---|---|
| static boxes | 9.8 | 10.2 | 10.4 | +0.4 | +0.2 |
| static angled | 15.3 | 18.2 | 17.9 | +2.9 | -0.3 |
| box movers | 18.3 | 31.5 | 33.0 | +13.2 | +1.5 |
| angled movers | 22.3 | 30.8 | 40.3 | +8.5 | +9.5 |
| spinning boxes | 25.6 | 42.6 | 59.8 | +17.0 | +17.2 |
| spinning angled | 22.2 | 37.2 | 42.0 | +15.0 | +4.8 |

(best of five interleaved rounds, `f1556d0`; medians in the session's
records.) Read across:

* **The larger share next to moving shapes is engine overhead, not the
  correctness work.** With every moving shape collided as its box, Stage 2
  is still 8.5–17 µs a tick slower than Stage 1 beside 8 movers: each moved
  brush's AABB is derived through the runtime layer once per tick (the
  transform found in its source, its box worked out and cached, the cache
  entry registered for invalidation) where Stage 1 read the `pos` the mover
  had written into the brush. Per moved brush, the first AABB query after a
  move costs 1.2–1.6 µs for a box and 1.65–1.94 µs for a convex mover,
  against 0.51–0.64 µs; later queries are hits at Stage 1's cost
  (0.12–0.15 µs).
* **True-shape work** is ~0 for still boxes, still angled brushes (Stage 1
  collided those by shape too) and non-turning box movers; +9.5 µs for 8
  angled movers and +4.8 µs for 8 spinning angled movers (a collision frame
  per moved convex brush per tick, and the player carried into it);
  +17 µs for 8 spinning boxes (a new rotated frame and six planes' tests
  per spinner per tick).
* For the spinners the middle column is not Stage 1's work either: Stage 2
  collides a spinner's *turned* AABB (its true bound, larger than Stage 1's
  unturned box), so the player touches it more often.

#### What remains, and why

Each metric flagged in the final gate, with the nine-round run where it
was repeated, classified: **engine overhead** (avoidable in principle),
**true-shape work** (the correctness Stage 2 adds), or **not demonstrated**
(inside the measured noise).

| Metric | Final gate | 9 rounds | Classification and cause |
|---|---|---|---|
| `player_spinning_boxes` | 2.34×, p .004 | 2.47×, p < .001 | Overhead +17 µs (runtime AABB derivation per spinner per tick, and the turned AABB colliding more) and true-shape work +17 µs (a rotated frame and plane tests per spinner per tick) |
| `player_angled_movers` | 2.15×, p .004 | 2.26×, p < .001 | Overhead +8.5 µs, true-shape work +9.5 µs |
| `player_spinning_angled` | 2.09×, p .004 | 2.26×, p < .001 | Overhead +15 µs, true-shape work +4.8 µs |
| `player_box_movers` | 1.77×, p .004 | 1.76×, p < .001 | Overhead +13 µs: no true-shape work for boxes that do not turn |
| `movers8_aabb_moving`, `movers32_aabb_moving` | 1.42×, 1.57× | 1.40×, 1.38×, p < .001 | Overhead: the first query of a moved mover after each move resolves it through the runtime layer (+0.6–0.9 µs once per move; hits at parity); averaged over the probe's 20 queries a tick |
| `movers128_aabb_moving` | 1.35×, p .004 | 1.22×, p .020 | The same mechanism; not significant in the `2cf2734` run (1.11×) |
| `player_static_angled` | 1.23×, p .004 | 1.16×, p .057 | Not demonstrated (the A/A run flagged this metric at 1.07×); the ablation puts +2.9 µs on the local-frame path, which Stage 1 did not have |
| `player_static_boxes` | 1.07×, p .028 | 1.01×, p .218 | Not demonstrated |
| `movers128_tick`, `movers32_tick` | 1.25×, 1.17× | 1.17× (p .016), 1.05× (p .081) | Not demonstrated: 1.02× and 0.93× in the `2cf2734` run with the same tick code. Publishing a runtime transform per moved row costs more than Stage 1's `pos` write (measured 28.5 µs a tick at 32 movers + 32 doors and 77 µs at 128 + 128, against ~12 µs per 128 rows for `pos` writes in a microbenchmark); the step code is faster than Stage 1's (the 8-mover tick is 0.50×) |
| `movers32_publish`, `movers128_publish` | 1.11×, 1.13× | 1.03×, 1.07× (p .039) | Not demonstrated; the publish code is Stage 1's apart from one flag check |
| `movers32_rows_first` | 1.15×, p .008 | 1.08×, p .047 | Not demonstrated: 0.96× in the `2cf2734` run. Whichever structure refreshes first pays the shared mover gather; rows + cells together are 0.75× Stage 1 (57 against 77 µs) |
| `frame_big_moving_ms` | 1.07×, p .048 | 1.00×, p .302 | Not demonstrated |

What has been tried and why the remaining overhead is still there:

* The runtime transform is resolved **once per placement stamp per brush,
  by whoever asks first** (`resolved_runtime`, a resolver dict each stamp
  replaces: nothing is written to the brush), and the AABB cache keeps
  Stage 1's hit cost. What is left is the miss: finding the transform in
  its source, deriving the box, and registering the cached key so the next
  stamp drops it.
* **Pushing derived AABBs into the brush caches at publication** (the
  writer pays instead of the reader) was estimated (not measured) at ~0.45 µs per moved
  row per tick for every moved row, queried or not -- about +115 µs a tick
  at 128 movers and doors, against a saving only for the rows a reader asks
  about. Not adopted.
* **Dense row indices** into the mover table's columns replace a ~0.25 µs
  dictionary probe with NumPy scalar reads of about the same cost in
  CPython (estimated from single-element read timings). Not adopted.
* **Building a moving shape's collision frame only when the player comes
  near it**, behind a world-space test proven no stricter than the existing
  one (trajectories bit-identical over 18 runs × 960 ticks), cut frame
  builds from 10 to 2 a tick but added more calls than it saved (522 → 575
  calls a tick, spinning boxes 51 → 58 µs). Reverted.
* None of the above writes runtime positions into authored `pos`, and none
  collides a moving shape as its box in production.

Representative gameplay -- the four maps, with their movers, doors,
monsters and the player -- shows no regression: frame, logic, paint and AI
time are level (ratios 0.99–1.13, the highest the AI on Office_Corridor at 7 µs a frame; none significant, p ≥ 0.155), and Python calls per logic
frame are within 0.6 % of Stage 1 (Office_Corridor 449 against 451,
BigWorld 1,241 against 1,248, MonsterTest 944 against 942, `_SHOWCASE` 488
against 485). The regressions above are in synthetic worst cases: eight
shapes all moving every tick beside the player's path.

#### Not yet measured on the target

Everything above is x86-64 with software GL. On the Snapdragon 8cx Gen 3
(Adreno 690), still to be measured:

* the whole gate (`tools/perf_gate.py`, all seven probes) under CPython on
  the ARM cores: the remaining overheads are per-call Python costs in
  microseconds, which scale with single-thread speed;
* `frames` and `gameplay` on the Adreno driver through OpenGL 3.3 core:
  shader compilation, the texture-buffer transforms of M2, the shared cube
  VBO, and whether GPU time (zero here: llvmpipe charges rasterising to the
  CPU) moves the bottleneck;
* memory and allocation behaviour under the device's memory;
* the visual oracles on the Adreno driver (validated here only on llvmpipe).

### 11.7 Stage 2 optimisation: a dense mover broad phase for the player

Status: **proposed** (this section is written before the implementation and
updated with its measurements). Stage 2 is not accepted.

#### The production path, measured

The gate's `player` probe drives Stage 2's movers through a
`RuntimeOverrides` (eight stamps a tick, the source's own update untimed)
and Stage 1's by writing `pos`. A level moves them differently: the logic
thread ticks the `MoverTable` (Stage 1 writes `pos` into each brush; Stage 2
publishes runtime transforms, once a tick) and then hands the player every
mover and door in the level. `tools/bench_player_table.py` does exactly
that on any checkout, with each tree's own table and player, and times the
tick (publication included), the player update, and both together.

Baseline, `8d42dfc` (HEAD's code is `f1556d0`) against Stage 1 `56ec0e3`:
five interleaved rounds, one pinned core, median of the per-round medians
(µs per tick); calls are exact.

| Scenario | Player calls S1 / HEAD | Player S1 / HEAD | Tick S1 / HEAD | Total S1 / HEAD |
|---|---|---|---|---|
| 8 box movers | 261 / 317 | 32.8 / 42.4 (1.29×) | 70.2 / 37.1 (0.53×) | 103.6 / 80.5 (0.78×) |
| 8 angled movers | 323 / 522 | 35.0 / 61.3 (1.75×) | 69.9 / 38.0 (0.54×) | 106.3 / 99.9 (0.94×) |
| 8 spinning boxes | 327 / 606 | 35.1 / 88.7 (2.53×) | 76.1 / 43.8 (0.58×) | 113.0 / 133.6 (1.18×) |
| 8 spinning angled | 323 / 631 | 35.8 / 73.9 (2.06×) | 74.2 / 42.5 (0.57×) | 111.2 / 117.2 (1.05×) |
| 34 doors away from the player | 811 / 1,015 | 70.0 / 118.1 (1.69×) | 110.7 / 126.2 (1.14×) | 182.5 / 244.9 (1.34×) |
| 1 mover (most shipped maps) | 109 / 121 | 19.7 / 23.5 (1.19×) | 21.5 / 17.2 (0.80×) | 41.5 / 40.7 (0.98×) |

Read across: Stage 2's tick and publication are cheaper than Stage 1's; the
player is not. A box mover nowhere near the player costs the player's
update about 1.6 µs a tick on Stage 1 and 2.9–4.6 µs on HEAD (classified,
its AABB derived through the runtime layer, tested by every pass), so the
34 far doors cost ~100 µs a tick. Work scales with movers, not contacts.

#### Proposal

The smallest change that removes per-mover Python from the player's tick:

1. **`MoverTable.near_rows`**: one vectorised test per group over the
   table's `pos` column -- the dense projection the step already writes and
   publishes every tick -- against a per-row reach: a row's authored
   half-extents, or for a row that can turn, the radius of its box. The
   reaches depend only on the authored shape and the row's flags, so they
   are worked out once per row rebuild or resync (change-driven), never per
   tick. Nothing is added to the tick or to publication; nothing is written
   to a brush.
2. **`brush_placement.movers_near`**: the resolver's entry point. It
   answers only when the mover table is the one runtime source with entries
   (the editor preview and test overrides keep the per-brush path) and the
   lists handed in are the table's own rows.
3. **`Player.update`** gathers only the movers and doors that answer near;
   every pass then runs as before, per brush, on that short list.
   Exactness is proved, not assumed: the region is the player's standing
   hull plus a displacement allowance and the reach of every test a pass
   makes (step-up, un-duck, the capsule's broad reject in a turned frame).
   A guard checks the player's displacement before each pass and after each
   push; if a pass would test beyond the region (a dash, a long push), that
   pass re-runs from its saved state on the full lists. The result is
   bit-identical to scanning every mover.
4. Below a few rows a group keeps the per-brush path, where one NumPy call
   costs more than the rows.

What it is expected to remove: the per-tick Python for every mover and
door not near the player (classification, the runtime AABB miss, a
`_has_headroom` per pass, and for a moving convex or spinning shape its
collision frame). What stays: the per-brush path for movers near the
player, which is where true-shape collision does its work.
