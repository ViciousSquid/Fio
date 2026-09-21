# Physics hot-path audit — vectorisation state of the dynamic body path

Measured on this container (Python 3.11, NumPy 1.26), scene = randomised
statics over a 4000-unit square, all bodies awake. `ms/s` is the cost at 60 Hz.

## What was still scalar

The batched rewrite (commits 439–443) vectorised integration, the static
collision broad phase and the sleep test, but two sections stayed per-object.

### 1. `_batch_floor` — the dominant one

Five support points per body (centre plus four in-footprint samples, added by
commit 453 so a body straddling a floor seam keeps contact) meant **5N
downward queries per step**, each a separate `raycast_down` call that
re-derives the grid cell and then walks that cell's brush dicts in Python.
Cost is 5N x (brushes per cell) Python iterations.

Measured before:

| active bodies | `_batch_floor` | ms/s @60Hz |
|---|---|---|
| 30 | 0.62 ms | 37 |
| 120 | 2.29 ms | 137 |
| 400 | 7.44 ms | 446 |
| 1000 | 19.2 ms | 1150 |

At 400 bodies the floor query alone was ~45% of one core.

### 2. Rotation integration

`angular_velocity` was already a NumPy array, but the update looped over
spinning bodies doing a per-body `np.asarray`, a per-body multiply and a
per-body `.tolist()`.

## What changed

### `_batch_floor` — group the samples by cell

All 5N sample points are now built in one NumPy pass, bucketed by grid cell
with a single `lexsort`, and each distinct cell tests **every point in it
against every brush in it with one broadcast**. The Python work becomes
proportional to the number of *distinct cells touched* rather than to the
number of samples.

Two constraints shaped the implementation:

* **Live positions.** Movers and doors are in the grid and move without it
  being repopulated (`populate` runs on play-start and on a model-collision
  toggle, not per frame); `raycast_down` reads `brush['pos']` live, so a prop
  riding a platform depends on seeing its current height. Caching the cell
  AABBs would have quietly broken that, so they are rebuilt from the dicts
  each call. That is the one piece of Python left, and it is per *cell*, not
  per sample. Pinned by `test_grouped_floor_query_reads_mover_heights_live`.
* **Custom grids.** Anything overriding `raycast_down` — a test double, or a
  future grid with its own tracing — cannot be answered from `cells` alone, so
  it keeps the scalar path. Pinned by `test_a_custom_grid_keeps_the_scalar_path`.

**The grouped path is not unconditionally faster.** Below roughly 56–64 active
bodies the NumPy assembly costs more than the scalar raycasts it saves:

| bodies | scalar | grouped | ratio |
|---|---|---|---|
| 8 | 0.148 ms | 0.280 ms | 0.53 |
| 32 | 0.593 ms | 0.802 ms | 0.74 |
| 64 | 1.118 ms | 1.019 ms | 1.10 |
| 128 | 2.294 ms | 1.578 ms | 1.45 |

So `FLOOR_BATCH_MIN_BODIES = 64` gates it, the same idiom
`render_cull.sort_by_distance` already uses with `min_numpy_count`. Blanket
vectorisation would have made small scenes — the common case — slower.

Result, with the gate in place:

| active bodies | before | after | speedup |
|---|---|---|---|
| 30 | 0.62 ms | 0.55 ms | 1.1x (scalar path) |
| 120 | 2.29 ms | 1.41 ms | 1.6x |
| 400 | 7.44 ms | 2.29 ms | **3.3x** |
| 1000 | 19.2 ms | 3.01 ms | **6.4x** |

### Rotation — vectorise the arithmetic, keep the boundary thin

The gather, the multiply-add and the conversion back to lists are each now one
operation over all spinning bodies; only the dict reads and writes stay
per-entity, because rotation lives on the entity's property dict rather than in
the packed arrays. 2.5–3x across 50–1500 bodies, at every size, so no gate.

Which bodies spin is unchanged (any body with non-zero angular velocity, awake
or not), and a malformed authored rotation now restarts that one body at zero
instead of raising out of the step for every other body.

## Equivalence, not approximation

The grouped path promotes to float64 before the support-point arithmetic
specifically because the scalar path promotes each float32 component to a
Python float before subtracting. That makes the two **bit-identical**, not
merely close:

    test_grouped_and_scalar_floor_queries_agree_exactly
        parametrised over 1 / 7 / 40 / 130 bodies x 4 seeds -> np.array_equal

Verified by mutation: dropping the four extra support points fails 11 of those
cases; caching the cell bounds fails the live-mover test; widening the rotation
accumulator to float64 fails the rotation test.

## Still scalar, deliberately

* **`_cell_bounds`** — reads brush dicts per cell. Required for live mover
  positions; the alternative is a cache invalidated by every mover tick, which
  costs more than it saves at realistic mover counts.
* **`_sync_entities`** — the NumPy→`Thing.pos` write-back. A genuine object
  boundary. See the note below.

## The bigger question: one authoritative transform buffer

State currently crosses four representations per frame:

    physics arrays -> Thing.pos -> logic render-state extraction -> renderer arrays

Each hop is a copy, and two of them (`_sync_entities`, and the render-state
position buffers added by commits 346–352) exist only to hand the same numbers
to the next stage in the shape it wants.

A shared authoritative transform buffer — physics writing into the same array
the renderer reads, with `Thing.pos` becoming a view onto a row — would remove
both copies and make the per-frame cost independent of entity count. It is
also the single most invasive change available: `Thing.pos` is a plain list
touched by the editor, serialisation, plugins, the 2D views and every gameplay
system, and a view onto a NumPy row does not behave like a list under all of
them (assignment, `copy.deepcopy`, JSON).

**Recommendation: not now, and not as part of a regression audit.** It is worth
more than further micro-optimisation of individual collision functions, but it
needs its own design pass, a decision about `Thing.pos`'s type contract, and a
migration for plugins that mutate `pos` directly. Recorded here so it is a
deliberate deferral rather than an oversight.
