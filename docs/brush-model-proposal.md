# Unified brush model — investigation and migration proposal

Status: **Stage 0 implemented** (guard rails, characterisation tests and
baselines; no behaviour change). Stages 1–4 are proposals. Written after M2
(`5b26ede`), which exposed the problem while keying convex GPU meshes.

Goal: one authoritative definition of every brush, two efficient geometry
encodings (compact AABB, arbitrary convex), specialised algorithms for each,
one transform model, one material model, one GPU-ready publication contract
— with no regression for AABBs, which are ~99 % of brushes.

Migration invariant: **every AABB keeps using the existing shared cube
geometry.** Changing the brush model must not introduce per-AABB vertex
allocations, plane tessellation, plane dictionaries or duplicated cube
meshes. One brush abstraction, two specialised geometry paths, one shared
cube VBO for all ordinary AABBs.

## What exists today

Fio began AABB-only; angled brushes were added later on top. A brush dict is
read under two models that overlap without one being authoritative:

| | AABB brush | convex ("angled") brush |
|---|---|---|
| geometry | `pos`, `size` | `geometry.planes` (world space) |
| `pos`/`size` | the geometry | a derived bound (`sync_brush_bounds`) — and the mesh's anchor |
| face material | per-tag dicts: `textures`, `uv_scale`, `uv_angle`, `uv_shift`, `uv_natural`; brush `texture_tiling` | per plane: `texture`, `uv_scale`, `uv_angle`, `uv_shift`, `uv_u`/`uv_v`, `uv_natural` — **and** the per-tag dicts |
| textured or solid pass | any real texture in `textures` | the same rule, from the per-tag copy, not the planes |
| rotation | `_rot_angle`/`rot_axis` (runtime; movers, savegame) | planes rotated (`rotate_brush`) and `_rot_angle` |
| runtime motion | `pos` written per tick; `original_pos` restores it | the same — the drawn mesh follows, the collision mesh does not |

`pos` alone has ~320 read/write sites, `size` ~140, `textures` ~85, plugins
included; ten modules branch on the encoding (`brush_geometry`,
`render_table`, `logic_collision`, `qt_game_view`, `renderer/core/overlays`,
`editor/main_window`, `editor/view_2d`, `editor/component_edit`,
`editor/face_texture`, a tool).

## Stage 0 — what is pinned (implemented)

No engine or editor code changed. Everything below is tests, fixtures and
tools.

* **An executable statement of today's semantics**
  (`tests/helpers/brush_semantics.py`): for a brush dict, its encoding, the
  pass that draws it, the faces drawn (world-space polygon and resolved
  material), the bound the bound-readers use, the transform, and every
  inconsistency it carries. GL-free.
* **Anchored to the engine, not to an opinion of it**
  (`tests/brush_model/test_semantics_anchor.py`): on every brush of every
  shipped map and on synthetic brushes covering each rule — box faces
  against the RenderTable columns the instanced box pass draws from
  (including the natural/tiling rule); convex faces against the frozen
  convex path (vertices through the published model matrix, and each run's
  face state), which `test_geo_run_parity` proves against the GPU; bounds
  against the RenderTable, `brush_aabb_bounds` (float32, as physics and AI
  read it) and the collision rows; the pass against the class bit; the
  transform against the rotation column. And at the GPU itself
  (`tests/visual/test_brush_semantics_gl.py`): the faces whose two material
  homes disagree are read back triangle by triangle through the draw oracle,
  so "this edit does not render" is a statement about GPU state.
* **Per-map snapshot** (`tests/brush_model/semantics_snapshot.json`,
  `test_semantics_snapshot.py`): every shipped map's encodings, a digest of
  the drawn faces, materials, passes and bounds (to 1e-4 of a unit), and the
  exact per-brush inventory of inconsistencies. A stage that changes a
  shipped map's meaning regenerates it in the same commit
  (`python tools/brush_semantics_snapshot.py --write`) and names the change.
* **Known inconsistencies pinned both ways**
  (`tests/brush_model/test_known_inconsistencies.py`): each is asserted as it
  behaves today *and* as the target contract, the latter
  `xfail(strict=True)` with the stage that delivers it. A stage cannot fix
  or break one silently: the characterisation test fails and the target
  passes unexpectedly.
* **Write-site ratchet** (`tests/brush_model/write_sites.json`,
  `test_write_sites.py`): per module, the code writing `pos`, `size`,
  `geometry`, the per-tag material dicts, `texture_tiling`, `original_pos`,
  `_rot_angle`, `rotation_yaw` (92 writes in 16 modules today). It may only
  go down.
* **AABB invariant** (`tests/visual/test_aabb_shared_cube.py`): in a box-only
  world, in textured, flat and wireframe modes, editor and play, with
  shadows: no convex machinery runs (no plane set, solve, record,
  tessellation or store page); every draw takes its positions from a buffer
  all boxes share (cube VBO, unit-cube edge VBO, occlusion probe cube) or
  none; growing the world tenfold creates no buffer or vertex array and the
  instanced passes issue the same draws; a frame leaves box dicts unchanged.
* **AABB budgets** (`tests/brush_model/test_aabb_budgets.py`, `perf`):
  heap per box dict, RenderTable column and retained bytes per row, and that
  projecting boxes adds no keys and solves nothing.
* **Baselines** (`tools/bench_brush_model.py`): memory, table sync, edit
  latency through `EditorState`, undo/redo, retained history, steady
  rendering.

## Stage 0 findings

Measured on this branch; "shipped" means one of the 20 maps in `maps/`.

**Face material (two homes).**

1. *Surface Inspector edits of a tagged angled face do not render.*
   `face_texture.set_transform` writes a tagged face's shift, scale and
   angle to the per-tag dicts and reads them back from there; the convex
   path draws the plane's values. Confirmed from GPU state. Texture is the
   one field written to both homes, so retexturing works.
2. *In shipped data:* four convex brushes in `_SHOWCASE` carry per-tag
   `uv_scale` 0.25 that their planes lack — the inspector shows 0.25, the
   viewport draws 1.0 (fit).
3. *Converting a box to convex drops its shift and angle.*
   `box_to_geometry` (and the first-edit seeding in `sync_brush_bounds`)
   carry textures and scales onto the planes, not shifts, angles or natural
   flags; the first clip, vertex drag or rotation of a mapped box resets
   the mapping on screen.
4. *Natural scale on a cut face never renders.* The editor writes
   `uv_natural` on the plane for a cut face; the renderer resolves convex
   natural mode from the per-tag flags only.
5. *`texture_tiling` does not reach convex faces* (boxes draw unscaled faces
   natural; the convex path draws them fitted).
6. *Pass routing reads the per-tag copy.* An angled brush whose only real
   textures are on planes — a cut face textured by the clip tool or the
   inspector on an otherwise untextured box — goes to the solid pass and is
   drawn entirely in its colour. Confirmed from GPU state; no shipped map.

**Position and bounds.**

7. *Bounds drift.* Seven shipped convex brushes have `pos`/`size` that are
   not their planes' AABB bit for bit: four by float noise (≤ 6e-11), one by
   4.85e-5 (`Angled_Brush_Test`; that bound is 2.4e-5 *smaller* than its
   geometry) and two by 128 units (`Terrain_Test_small`, `Topdown_example`;
   larger bounds). Culling, collision broad-phase, picking and the spatial
   grid read the bound, so they are conservative to within 1e-4; deriving
   bounds will tighten those two maps' culling and broad-phase, not change
   their pixels.
8. *What is drawn depends on history.* A convex mesh is anchored to
   `pos`/`size` when its record is made and kept while the planes are
   unchanged, so a `size`-only change to an existing convex brush (console
   `setprop`, a plugin) stretches it until the map is reloaded, when the
   planes are drawn where they are.
9. *Convex movers' collision stays behind.* Movers write `pos`; the drawn
   brush follows, but its collision triangles (world space, baked at play
   start) do not. No shipped map has a convex mover.
10. *Play state is saved.* Movers write `original_pos`, `_rot_angle` and
    `rotation_yaw` into the authored dict and the serialiser keeps them:
    seven shipped maps carry `original_pos`, one `rotation_yaw`.
11. Physics and AI read the bound rounded to float32 (`brush_aabb_bounds`,
    through GLM); the RenderTable and collision rows read it in float64.
    Consistent with `pos`/`size`, but a third numeric reading to keep.

**AABB rendering and editing.**

12. Every box is drawn from the shared cube VBO by instanced passes; the
    draw count does not depend on the number of boxes. Exception: wireframe
    mode issues one `glDrawArrays` per brush (from the shared edge buffer:
    a submission per box, not an allocation) — an M5 candidate.
13. Editing cost on large AABB worlds is the undo checkpoint, not the
    tables: each edit serialises the whole world (1.10 MB of JSON on
    BigWorld, ~11 ms) while the table refresh is ~0.2 ms; undo/redo take
    68–70 ms (restore ~25 ms, table reconcile ~43 ms). The 50-entry history
    retains up to ~55 MB on BigWorld. Not a brush-model problem, but every
    stage must keep it from growing (a canonical record must not make the
    snapshot larger).

## AABB baselines (CPython 3.13, llvmpipe, x86-64)

| | BigWorld (3,604 boxes) | `_SHOWCASE` (68 boxes, 17 convex) |
|---|---|---|
| heap per box dict | **1,358 B** (budget 1,359) | 1,389 B |
| heap per convex dict | — | 5,670 B |
| RenderTable column bytes per row | **353 B** | 353 B |
| RenderTable retained per row | 541 B | 2,916 B |
| full table sync (warm / cold) | **40–42 ms** / 41–54 ms | 1.9 / 10.6–16.5 ms |
| one-row refresh (move) p50 / max | 0.07–0.09 / 0.20 ms | 0.05 / 0.14 ms |
| edit: move, resize, retexture, UV p50 | 11.1–12.3 ms (checkpoint ~11) | 1.3–1.4 ms |
| edit: create / delete p50 | 16.1 / 17.2 ms | 1.5 / 1.6 ms |
| undo / redo p50 (p99) | 67.7 (88.3) / 69.6 (110) ms | 14.7 (21.4) / 15.1 (23.8) ms |
| undo checkpoint | 1.10 MB | 64 KB |
| still frame render CPU p50 / p95 / p99 / max | 9.3 / 13.9 / 14.9 / 27.9 ms | 8.1 / 10.7 / 11.2 / 11.5 ms |

Ranges are two runs on a shared machine. Reproduce:

```bash
xvfb-run -a env LIBGL_ALWAYS_SOFTWARE=1 python tools/bench_brush_model.py --render --json base.json
```

## Target model

One record per brush; derived data is never saved and never written by
editing code.

* **`shape`** — exactly one encoding:
  * `box`: `size` (half-extents implied). Fixed topology: six faces with
    fixed tags, no planes, no solver. This is today's AABB, unchanged, drawn
    from the shared cube VBO.
  * `convex`: the plane set (normal, offset per plane). Solved once into
    windings when it changes (as now), cached by content.
* **`transform`** — the only placement: `pos` (box centre; convex
  reference point) plus optional rotation. Authored; never written by the
  runtime.
* **`faces`** — one material/UV record per face, the only one: `texture`,
  `uv_scale`, `uv_angle`, `uv_shift`, `uv_natural`, and for convex faces the
  texture basis. For a box, the per-tag dicts are that record (six entries,
  no plane dicts); for a convex brush, the planes.
* **Runtime state** (movers, doors): a displacement held by the
  `MoverTable` and published as a transform column; authored `pos` stays
  put, so `original_pos` disappears.
* **Derived** (cached, rebuilt on change, never saved): world bounds,
  convex windings and collision meshes, render records, the pass a brush is
  drawn in.

Specialised algorithms stay specialised: boxes keep the cube VAO and the
instanced face path, dense `(N, 6)` face columns, AABB collision arrays and
cell bucketing; convex brushes keep windings, mesh collision and the M2
shared store. The publication contract: one RenderTable row per brush with
an encoding bit, transform columns, and either the six face columns or a
geometry handle into convex records — what exists today, minus the
duplicated inputs.

## Incremental migration

Each stage ships on its own and must meet the gates before the next starts.

0. **Guard rails, no behaviour change.** Done (above).
1. **One face record.** Proposed in detail below.
2. **Unambiguous position.** Movers stop writing `pos`: their displacement
   is a runtime transform column (the MoverTable already owns dense
   positions), applied to drawing and convex collision alike. Convex
   `pos`/`size` become derived and read-only; a convex mesh depends on its
   shape and faces only (the anchor becomes the shape's own frame), so what
   is drawn no longer depends on history. Play state leaves the authored
   dict. The two maps whose bounds are 128 units off get exact bounds:
   tighter culling and broad-phase, identical pixels; called out and tested.
3. **Compact encodings in the tables.** RenderTable/EntityTable keep the box
   columns as they are; convex rows reference cached shapes. No change for
   boxes beyond reading the same values from the canonical record.
4. **Save format v4.** Writes the canonical record; v3 loads through a
   migration; round-trip tests; compatibility keys for plugins for one
   release (read-only views of `pos`/`size`/`textures`), then removed.

## Proposed Stage 1 — one face record

Scope: face material only. No change to geometry, transforms, bounds, the
table layout or the save format version.

1. **One owner per face.** A GL-free module (`engine/brush_faces.py`) with
   one reader and one writer:
   * `face_material(brush, key)` / `face_materials(brush)`: the resolved
     material of a face, by the rule that renders;
   * `set_face_material(brush, key, **fields)`: writes the one home — a
     box's per-tag dicts (unchanged: no plane dicts for AABBs), a convex
     face's plane (tagged and cut faces alike) — and invalidates what
     depends on it.
   Readers switched to it: the RenderTable box columns (same values), the
   convex record's natural flags, the pass classification, the Surface
   Inspector (`face_texture.get_transform`/`set_transform`), the retexture
   action, and the box-to-convex seeding (`box_to_geometry`,
   `sync_brush_bounds`), which then carries shift, angle and natural too.
2. **Load-time normalisation for convex brushes.** A tagged face's per-tag
   entries are folded into its plane by the rule that renders today — the
   plane wins for texture, scale, shift and angle; the per-tag flag wins for
   natural — and the per-tag copies are dropped. Boxes are not touched.
3. **Classification from faces.** A convex brush is textured if any of its
   faces renders a real texture (finding 6); boxes unchanged.
4. **Tiling and plane natural render** for convex faces (findings 4, 5).

Expected effect on shipped maps: **no digest changes** — no shipped map has
a plane-level natural flag, a tiled convex brush or plane-only textures,
and "plane wins" keeps `_SHOWCASE`'s four brushes drawing as they do. The
inventory loses its `material_mismatch` entries; the inspector then shows
those faces' rendered scale (1.0) instead of 0.25. The fixes reach brushes
edited from then on.

Gates: snapshot digests unchanged; inventory without `material_mismatch`,
`ignored_plane_natural`, `untextured_planes`, `ignored_texture_tiling`; the
six Stage 1 targets pass (markers removed) and their characterisation twins
are updated; draw-oracle parity in every batching mode; the AABB guard and
budgets unchanged; `bench_brush_model` within noise of the baseline; the
write-site ratchet lowered (`face_texture`'s per-tag writes for convex
faces gone). The mesh key test moves `plane uv_natural` out of its
conservative set (it becomes a mesh input).

Decision needed before Stage 1: for `_SHOWCASE`'s four brushes, keep what
renders (proposed: pixels unchanged, the inspector's 0.25 disappears) or
honour the authored per-tag scale (the inspector stays, four brushes'
textures change scale on screen).

## Gates (every stage)

Performance (same machine, base vs branch): box dict heap ≤ 1,359 B;
RenderTable column bytes ≤ 353 per row; table sync ≤ +5 %; steady render
unchanged within noise on `_SHOWCASE`, BigWorld and the angled benchmark;
edit, undo and redo p95/p99 unchanged; undo checkpoint not larger; collision
step and map load ≤ +5 %.

Correctness: the semantics snapshot unchanged except where a stage names a
deliberate change; draw-oracle parity (pixels and per-triangle state); the
AABB shared-cube guard; collision and picking reading the same bounds;
undo/redo and save/load round trips; Big World streaming; savegames;
plugin API contract tests.

## Open questions

* Rotation: a box with a rotation is not an AABB. Keep rotated boxes as the
  box encoding plus a transform (cheap), or convert them to convex on
  rotate (today's editor behaviour)? Proposed: keep today's editor
  behaviour, and use the transform rotation only for runtime motion.
* Whether convex planes should move to brush-local space. Rebasing changes
  float values; proposed only if Stage 2 shows a need, with an explicit
  tolerance-based parity rule.
* Compatibility of Stage 1's normalisation with older builds: a plane-level
  natural flag written by Stage 1 is ignored by a build from before it (the
  face renders fitted there).
