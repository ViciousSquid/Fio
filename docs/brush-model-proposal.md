# Unified brush model — investigation and migration proposal

Status: **proposal, not implemented.** Written after M2 (`5b26ede`), which
exposed the problem while keying convex GPU meshes.

Goal: one authoritative definition of every brush, two efficient geometry
encodings (compact AABB, arbitrary convex), specialised algorithms for each,
one transform model, one material model, one GPU-ready publication contract
— with no regression for AABBs, which are ~99 % of brushes.

## What exists today

Fio began AABB-only; angled brushes were added later on top. A brush dict is
read under two models that overlap without one being authoritative:

| | AABB brush | convex ("angled") brush |
|---|---|---|
| geometry | `pos`, `size` | `geometry.planes` (world space) |
| `pos`/`size` | the geometry | a derived bound (`sync_brush_bounds`) |
| face material | per-tag dicts: `textures`, `uv_scale`, `uv_angle`, `uv_shift`, `uv_natural`; brush `texture_tiling` | per plane: `texture`, `uv_scale`, `uv_angle`, `uv_shift`, `uv_u`/`uv_v`, `uv_natural` — **and** the per-tag dicts |
| rotation | `_rot_angle`/`rot_axis` (runtime; movers, savegame) | planes rotated (`rotate_brush`) and `_rot_angle` |
| runtime motion | `pos` written per tick; `original_pos` restores it | same — but the mesh is anchored to `pos` (below) |

Measured consequences:

1. **`pos`/`size` drift from the planes.** Bit-exact on generated worlds, but
   off by 4.9e-5 (`Angled_Brush_Test`), ~1e-11 (`_SHOWCASE`, `dLight2_Test`)
   and **128 units** (`Terrain_Test_small`, `Topdown_example`). Rendering
   draws the planes; collision AABBs, the spatial grid, culling and picking
   read `pos`/`size`. Those two maps disagree with themselves today.
2. **Material stored twice.** `face_texture.set_transform` and the retexture
   action write a convex face's texture to both the plane and
   `brush['textures'][tag]`. Natural scale has two homes, and the renderer
   reads the per-tag flag for convex faces, so a plane-level `uv_natural`
   (which the editor writes for cut faces) never renders — a latent bug
   pinned by the M1 frozen reference.
3. **Position means three things**: geometry (box), a cached bound (convex)
   and a runtime transform (movers write `pos` every tick). Convex meshes
   are stored in the unit cube of `pos`/`size` (the retrofit that let them
   reuse the box transform), so `pos` is baked into vertices *and* applied
   as the transform. M2 made that consistent with an anchored cache key
   (`GeometryRecord.surface`), but the ambiguity is in the data.
4. **Ten modules branch on the encoding** (`brush_geometry`,
   `render_table`, `logic_collision`, `qt_game_view`, `renderer/core/overlays`,
   `editor/main_window`, `editor/view_2d`, `editor/component_edit`,
   `editor/face_texture`, a tool). `pos` alone has ~320 read/write sites,
   `size` ~140, `textures` ~85, plugins included (`pos` 8, `size` 14).

AABB baselines to protect (BigWorld, 3,604 boxes; llvmpipe/x86-64):
Python heap per box dict **1,359 B** (`pos`, `size`, `operation`,
`textures`, `id`, `name`); RenderTable **353 B/row**; full table sync
**40 ms**; `_SHOWCASE` still-frame render 5.0–5.2 ms.

## Target model

One record per brush; derived data is never saved and never written by
editing code.

* **`shape`** — exactly one encoding:
  * `box`: `size` (half-extents implied). Fixed topology: six faces with
    fixed tags, no planes, no solver. This is today's AABB, unchanged.
  * `convex`: the plane set (normal, offset per plane). Solved once into
    windings when it changes (as now), cached by content.
* **`transform`** — the only placement: `pos` (box centre; convex
  reference point) plus optional rotation. Authored; never written by the
  runtime.
* **`faces`** — one material/UV record per face, the only one: `texture`,
  `uv_scale`, `uv_angle`, `uv_shift`, `uv_natural`, and for convex faces the
  texture basis. Indexed by tag for boxes (6 entries), by plane for convex.
* **Runtime state** (movers, doors): a displacement held by the
  `MoverTable` and published as a transform column; authored `pos` stays
  put, so `original_pos` disappears.
* **Derived** (cached, rebuilt on change, never saved): world bounds,
  convex windings and collision meshes, render records.

Specialised algorithms stay specialised: boxes keep the cube VAO and the
instanced face path, dense `(N, 6)` face columns, AABB collision arrays and
cell bucketing; convex brushes keep windings, mesh collision and the M2
shared store. The publication contract becomes: one RenderTable row per
brush with an encoding bit, transform columns, and either the six face
columns or a geometry handle into convex records — what exists today, minus
the duplicated inputs.

## Incremental migration

Each stage ships on its own, behind the oracles below, and must meet the
performance gates before the next starts.

0. **Guard rails, no behaviour change.** A canonical "resolved brush"
   oracle (world-space faces with materials, from today's dicts) captured
   for every shipped map; render parity through the M1/M2 draw oracle;
   collision, picking and spatial-grid parity snapshots; AABB benchmarks
   (memory/brush, table bytes/row, sync, steady render, collision step,
   editor drag latency, map load). A check that flags new writes to the
   duplicated fields.
1. **One face record.** One accessor (`brush_faces(brush)`) used by every
   reader. For convex brushes the plane is authoritative; stop writing the
   per-tag copies. At load, resolve the natural-scale conflict by the rule
   that renders today (per-tag wins), written onto the plane. Draw-oracle
   parity on every map.
2. **Unambiguous position.** Movers stop writing `pos`: their displacement
   is a runtime transform column (the MoverTable already owns dense
   positions). Convex `pos`/`size` become derived and read-only; a convex
   mesh depends on its shape and faces only (the anchor becomes the shape's
   own frame). The one deliberate behaviour change — the two maps whose
   bounds are 128 units off — gets corrected bounds, so collision, culling
   and picking agree with what is drawn; called out and tested.
3. **Compact encodings in the tables.** RenderTable/EntityTable keep the
   box columns as they are; convex rows reference cached shapes. No change
   for boxes beyond reading the same values from the canonical record.
4. **Save format v4.** Writes the canonical record; v3 loads through a
   migration; round-trip tests; compatibility keys for plugins for one
   release (read-only views of `pos`/`size`/`textures`), then removed.

## Gates

Performance (per stage, same machine, base vs branch): box dict memory
≤ 1,359 B; RenderTable bytes/row unchanged; table sync ≤ +5 %; steady render
unchanged within noise on `_SHOWCASE`, BigWorld and the angled benchmark;
collision step and editor drag latency p95/p99 unchanged; map load ≤ +5 %.

Correctness: every shipped map identical through the draw oracle (pixels and
per-triangle state) except where a stage names a deliberate change; collision
and picking snapshots; undo/redo and save/load round trips; Big World
streaming; savegames; plugin API contract tests.

## Open questions

* Rotation: a box with a rotation is not an AABB. Keep rotated boxes as the
  box encoding plus a transform (cheap), or convert them to convex on
  rotate (today's editor behaviour)? Proposed: keep today's editor
  behaviour, and use the transform rotation only for runtime motion.
* Whether convex planes should move to brush-local space. Rebasing changes
  float values; proposed only if Stage 2 shows a need, with an explicit
  tolerance-based parity rule.
