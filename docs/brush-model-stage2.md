# Unified brush model — Stage 2: shape, placement and derived bounds

Status: **approved; being implemented in substages** (§11 records each as it
lands). Builds on Stage 1 (`56ec0e3`, one material record per face); the
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
