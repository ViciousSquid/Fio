# Changes

## Adaptive monster crossfire ray queries

Based on CANARY commit `69f6fc85b38fcda092e09e8296f2ccca20d8fa88`.

### Scope and implementation

Inspection found three distinct ray paths: player-shot sphere snapshots in
`LogicCombat._trace_monsters`, brush slabs in `CollisionRows.ray_hits`, and
AI crossfire in `MonsterTable.crossfire`. The crossfire path owns the tick-local
live-target cache and previously copied its candidate positions twice, allocated
separate offsets and distances, and lexsorted every intersection. This change
optimizes that path. Player hitscan's separate shot-snapshot/pellet/I/O contract,
brush tests, and unrelated application behavior are unchanged.

- Use Python scalar sphere tests below 32 table rows and NumPy at/above 32.
  Small queries prepare rays with Python floats and `math.sqrt`, and initialize
  a Python list for the tick-local live-target cache. Ordinary first-shot, warm,
  and stale-target queries make no NumPy calls. Large queries keep NumPy arrays.
- Select the nearest hit with a running minimum (scalar) or first `argmin`
  (NumPy). Both preserve table/list order for equal distances. Sort only in
  the fallback when the nearest cached target is now dead or hidden.
- Keep `_live` initialization on the first valid query and reset on `gather`,
  unchanged. Recheck flags only for hits encountered in distance/row order,
  evicting stale ones for the rest of the tick. A target initially excluded or
  evicted does not rejoin until the next gather. A farther hidden target that
  was never reached is not prematurely evicted. Positions and teams still come
  from the table's gathered snapshot.
- Python and NumPy/BLAS can differ in the last bit of a dot product. Near a
  tangent, an open segment endpoint, the one-unit length cutoff, or nearly equal
  entries at different centers, the scalar path recomputes the original NumPy
  normalization and delegates to the NumPy kernel. Nonfinite lengths also use
  the original path. The conservative
  `1e-12` guard chooses arithmetic only; it does **not** widen the hit region.
  Keep the original `disc >= 0`, `0 < entry < segment length`, radius/lift,
  shooter/team exclusions, and rejection of segments shorter than one unit.
- Fancy indexing already creates an owned position array: remove its redundant
  `.copy()` and reuse that buffer for offsets. Reuse the discriminant for
  roots/distances, with masked in-place square root; remove the filled distance
  array and the real-root fancy-indexed temporaries. Table geometry is never
  modified, and `_live` is never reused as scratch storage.

### Crossover and before/after timings

Measured on this cloud machine: Debian 13, Linux x86_64, GIL-enabled CPython
3.14.8, NumPy 2.5.3. The final run pinned the process to CPU 1 with `taskset`.
The benchmark uses the frozen original CANARY query in
`tests/helpers/crossfire.py` as both an independent behavioral oracle and the
before implementation, so old and new run interleaved on the same fixtures.
It asserts identical returned object **and cache evictions** for each mode.

Started with threshold 16. Initial measurements justified moving to **32**;
removing the remaining small-query NumPy preparation strengthens the scalar
advantage at 16 and 24 rows. In the updated warm run, NumPy wins at 32 for
aligned and stale hits; sparse, tied, and filtered populations cross later.
Cold aligned/tied queries cross around 32. Retain **32** as a conservative
compromise for dense hit populations, including excluded rows in the population. This is a measured
Fio threshold, not a borrowed constant. The class constant remains independently
tunable; neither timings nor a crossover location are test assertions.

Each table cell is best microseconds/query over seven interleaved repeats of
300 shots. GC is disabled only during timing and restored afterward. Positions
are deterministic; rays run along a 2200-unit segment. `sparse` spreads targets
in a 4000-unit cube; `aligned` puts many intersections on the ray in shuffled
row order; `ties` places targets at identical distances; `filtered` adds teams
and initially hidden targets; `stale` makes the four nearest cached targets dead
and resets eligibility before **each** query to exercise fallback repeatedly.
A one-row case contains only the shooter. Gather is outside timing and the
warm live cache matches repeated shots within one AI tick. Cold runs reset the
cache before every query, including its initialization in the timing; they
cover 1-32 rows and omit stale targets (a fresh cache already excludes dead
targets). Each mode is primed with its native cache representation, a Python
list for small scalar queries or an ndarray for dense queries. These are query
latencies, not whole-tick or FPS measurements.

| Cache | Distribution | Rows | Before sorted | Forced scalar | Forced NumPy | Adaptive | Before/adaptive |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| warm | sparse | 1 | 4.89 | 1.41 | 5.28 | 1.33 | 3.68x |
| warm | sparse | 4 | 33.68 | 2.85 | 31.64 | 3.04 | 11.07x |
| warm | sparse | 8 | 46.01 | 5.22 | 41.90 | 5.26 | 8.75x |
| warm | sparse | 16 | 59.96 | 14.57 | 49.91 | 8.81 | 6.80x |
| warm | sparse | 24 | 48.52 | 15.05 | 58.99 | 23.37 | 2.08x |
| warm | sparse | 32 | 50.48 | 25.70 | 35.82 | 59.76 | 0.84x |
| warm | sparse | 48 | 66.36 | 54.44 | 47.63 | 52.57 | 1.26x |
| warm | sparse | 64 | 41.99 | 36.99 | 29.61 | 28.59 | 1.47x |
| warm | sparse | 128 | 30.09 | 59.54 | 32.63 | 42.72 | 0.70x |
| warm | sparse | 256 | 50.35 | 155.30 | 46.09 | 37.81 | 1.33x |
| warm | sparse | 1024 | 62.14 | 518.33 | 67.22 | 63.59 | 0.98x |
| warm | aligned | 1 | 4.48 | 1.37 | 4.59 | 1.30 | 3.44x |
| warm | aligned | 4 | 25.29 | 3.78 | 22.85 | 3.91 | 6.47x |
| warm | aligned | 8 | 26.63 | 6.92 | 22.66 | 6.72 | 3.96x |
| warm | aligned | 16 | 24.60 | 13.23 | 23.09 | 12.81 | 1.92x |
| warm | aligned | 24 | 26.50 | 18.69 | 24.29 | 19.58 | 1.35x |
| warm | aligned | 32 | 27.33 | 24.81 | 23.52 | 23.13 | 1.18x |
| warm | aligned | 48 | 28.65 | 38.90 | 24.34 | 24.47 | 1.17x |
| warm | aligned | 64 | 26.91 | 52.81 | 22.49 | 24.25 | 1.11x |
| warm | aligned | 128 | 32.59 | 102.38 | 27.82 | 25.68 | 1.27x |
| warm | aligned | 256 | 39.94 | 206.32 | 35.04 | 33.39 | 1.20x |
| warm | aligned | 1024 | 94.56 | 873.73 | 59.17 | 59.81 | 1.58x |
| warm | ties | 1 | 4.58 | 1.41 | 4.79 | 1.31 | 3.49x |
| warm | ties | 4 | 26.07 | 4.16 | 23.03 | 3.96 | 6.59x |
| warm | ties | 8 | 26.40 | 7.48 | 23.50 | 7.41 | 3.56x |
| warm | ties | 16 | 27.15 | 17.43 | 24.54 | 14.90 | 1.82x |
| warm | ties | 24 | 27.82 | 21.56 | 25.11 | 22.32 | 1.25x |
| warm | ties | 32 | 26.97 | 27.29 | 28.02 | 24.00 | 1.12x |
| warm | ties | 48 | 28.98 | 44.20 | 25.91 | 26.05 | 1.11x |
| warm | ties | 64 | 32.53 | 61.75 | 23.80 | 25.93 | 1.25x |
| warm | ties | 128 | 33.91 | 118.41 | 27.92 | 28.03 | 1.21x |
| warm | ties | 256 | 36.05 | 232.92 | 34.24 | 34.32 | 1.05x |
| warm | ties | 1024 | 69.56 | 1003.22 | 57.29 | 58.78 | 1.18x |
| warm | filtered | 1 | 6.74 | 1.48 | 6.68 | 1.81 | 3.72x |
| warm | filtered | 4 | 22.57 | 2.79 | 23.92 | 2.93 | 7.72x |
| warm | filtered | 8 | 23.65 | 4.43 | 22.30 | 4.13 | 5.73x |
| warm | filtered | 16 | 25.79 | 7.79 | 26.41 | 7.49 | 3.44x |
| warm | filtered | 24 | 23.69 | 9.87 | 24.71 | 10.28 | 2.30x |
| warm | filtered | 32 | 24.86 | 12.43 | 23.80 | 24.02 | 1.04x |
| warm | filtered | 48 | 27.68 | 17.46 | 24.43 | 24.19 | 1.14x |
| warm | filtered | 64 | 24.93 | 22.62 | 24.00 | 26.14 | 0.95x |
| warm | filtered | 128 | 27.27 | 49.99 | 26.13 | 29.07 | 0.94x |
| warm | filtered | 256 | 31.40 | 96.63 | 30.19 | 29.91 | 1.05x |
| warm | filtered | 1024 | 51.96 | 389.43 | 46.11 | 49.51 | 1.05x |
| warm | stale | 1 | 8.16 | 1.69 | 10.59 | 1.72 | 4.76x |
| warm | stale | 4 | 27.28 | 5.59 | 28.07 | 5.61 | 4.86x |
| warm | stale | 8 | 25.85 | 10.89 | 27.24 | 9.76 | 2.65x |
| warm | stale | 16 | 26.86 | 16.61 | 27.72 | 17.91 | 1.50x |
| warm | stale | 24 | 26.85 | 26.97 | 29.79 | 23.75 | 1.13x |
| warm | stale | 32 | 27.83 | 32.04 | 29.15 | 29.47 | 0.94x |
| warm | stale | 48 | 27.83 | 50.96 | 31.74 | 33.62 | 0.83x |
| warm | stale | 64 | 27.27 | 64.32 | 32.21 | 31.74 | 0.86x |
| warm | stale | 128 | 36.07 | 136.40 | 34.43 | 31.66 | 1.14x |
| warm | stale | 256 | 41.09 | 261.07 | 41.36 | 44.23 | 0.93x |
| warm | stale | 1024 | 90.24 | 1158.93 | 109.05 | 104.87 | 0.86x |
| cold | sparse | 1 | 5.47 | 1.70 | 6.33 | 1.75 | 3.12x |
| cold | sparse | 4 | 24.21 | 3.46 | 23.73 | 3.46 | 7.00x |
| cold | sparse | 8 | 24.78 | 6.28 | 24.67 | 5.78 | 4.29x |
| cold | sparse | 16 | 27.70 | 9.54 | 25.57 | 9.81 | 2.82x |
| cold | sparse | 24 | 28.32 | 13.69 | 27.52 | 15.88 | 1.78x |
| cold | sparse | 32 | 28.67 | 18.23 | 27.92 | 29.65 | 0.97x |
| cold | aligned | 1 | 5.87 | 1.70 | 6.10 | 1.74 | 3.38x |
| cold | aligned | 4 | 27.89 | 5.46 | 24.23 | 4.76 | 5.86x |
| cold | aligned | 8 | 26.67 | 8.38 | 24.22 | 8.50 | 3.14x |
| cold | aligned | 16 | 27.43 | 15.79 | 24.76 | 15.08 | 1.82x |
| cold | aligned | 24 | 31.09 | 23.35 | 27.00 | 21.33 | 1.46x |
| cold | aligned | 32 | 31.66 | 28.16 | 27.75 | 27.32 | 1.16x |
| cold | ties | 1 | 5.69 | 1.75 | 6.45 | 1.74 | 3.27x |
| cold | ties | 4 | 24.82 | 4.70 | 23.94 | 4.63 | 5.36x |
| cold | ties | 8 | 26.18 | 8.38 | 23.37 | 8.05 | 3.25x |
| cold | ties | 16 | 28.27 | 16.33 | 25.65 | 15.77 | 1.79x |
| cold | ties | 24 | 27.81 | 23.51 | 25.31 | 22.15 | 1.26x |
| cold | ties | 32 | 30.19 | 30.53 | 26.26 | 27.05 | 1.12x |
| cold | filtered | 1 | 7.36 | 1.79 | 8.67 | 1.71 | 4.30x |
| cold | filtered | 4 | 26.43 | 3.53 | 23.23 | 3.17 | 8.33x |
| cold | filtered | 8 | 26.40 | 5.20 | 25.59 | 4.77 | 5.53x |
| cold | filtered | 16 | 26.21 | 8.29 | 27.15 | 8.09 | 3.24x |
| cold | filtered | 24 | 28.13 | 11.46 | 28.23 | 11.10 | 2.53x |
| cold | filtered | 32 | 28.71 | 15.31 | 28.04 | 27.31 | 1.05x |

The shared cloud host remains noisy, especially the initial warm sparse block:
forced NumPy and adaptive execute the same path at large populations yet can
differ significantly in timings. Do not interpret every ratio as algorithmic
speedup. The repeatable direction is that scalar dominates small populations;
large aligned hits benefit from removing the common sort (1024 rows: 94.56 ->
59.81 us, 1.58x). Some cases are slower: warm stale 48 rows 27.83 -> 33.62 us,
and warm stale 1024 rows 90.24 -> 104.87 us; those queries pay nearest selection
before fallback. Timings are reported without a universal speedup claim.

### Follow-up: remove the remaining small-N NumPy overhead

The first scalar implementation still used `np.asarray`, a dot product,
`np.sqrt`, array division and `np.fromiter` before entering the scalar kernel.
That work is now entirely avoided for ordinary small queries, including on the
first query of a tick. Numerical fallback reconstructs the original NumPy ray
from the original endpoints; it does not reuse the slightly different Python
normalization. Cache initialization/invalidation and lazy stale-target eviction
retain the same behavior, including when a Python cache reaches the dense
fallback. Geometry stays in the existing gathered table; no extra position
cache or invalidation mechanism was introduced.

An additional direct comparison loaded the saved pre-refinement implementation
and the current implementation in one process, with shared monsters and
identical query endpoints. Best of seven interleaved batches of 1000 queries,
CPU 1, same runtime; both used their own cache representations. The previous
column is the already optimized scalar implementation with NumPy preparation,
not the original sorted CANARY query. It isolates the additional improvement
from this follow-up, measured separately from the full crossover run above.

| Cache | Distribution | Rows | Previous scalar us | Native setup us | Previous/native |
| --- | --- | ---: | ---: | ---: | ---: |
| warm | sparse | 4 | 5.58 | 2.70 | 2.06x |
| warm | sparse | 8 | 8.20 | 4.76 | 1.72x |
| warm | sparse | 16 | 12.21 | 7.99 | 1.53x |
| warm | sparse | 24 | 16.63 | 11.94 | 1.39x |
| warm | aligned | 4 | 7.43 | 4.16 | 1.78x |
| warm | aligned | 8 | 10.99 | 6.71 | 1.64x |
| warm | aligned | 16 | 17.83 | 13.83 | 1.29x |
| warm | aligned | 24 | 27.31 | 20.91 | 1.31x |
| cold | sparse | 4 | 6.35 | 3.12 | 2.03x |
| cold | sparse | 8 | 9.95 | 5.28 | 1.89x |
| cold | sparse | 16 | 14.75 | 9.50 | 1.55x |
| cold | sparse | 24 | 17.65 | 13.67 | 1.29x |
| cold | aligned | 4 | 7.52 | 4.20 | 1.79x |
| cold | aligned | 8 | 12.00 | 8.29 | 1.45x |
| cold | aligned | 16 | 20.70 | 15.72 | 1.32x |
| cold | aligned | 24 | 26.95 | 22.39 | 1.20x |

The follow-up measured 1.20-2.06x faster across these small-N cases. Four sparse
targets improved 5.58 -> 2.70 us warm, and 6.35 -> 3.12 us cold. Sixteen aligned
targets improved 17.83 -> 13.83 us warm. The opt-in benchmark now includes cold
queries so first-shot setup overhead remains visible when tuning the threshold.

Reproduce from the repository root, with the project's Python dependencies
and pytest installed. Run these commands sequentially; `taskset` is optional
and its CPU must be in the machine's allowed affinity set:

```bash
python -m pytest tests/monster_ai/test_crossfire_queries.py -q
python -m pytest tests/monster_ai tests/engine/test_hitscan_dense.py \
  tests/engine/test_hitscan_snapshot.py tests/engine/test_item_runtime.py -q -ra
taskset -c 1 python -m pytest \
  tests/performance/test_crossfire_crossover_benchmark.py \
  --run-benchmarks -s -q
```

### Validation and failures

- Before implementation: the existing dense AI, dense hitscan, and shot-snapshot
  suites passed **39 tests**. No existing failure was observed in this scope.
- New query tests cover forced scalar/NumPy equivalence to the original across
  random populations, both sides of the dispatch threshold, tangents, parallel
  axes, behind/inside spheres, entry at the origin/end, the one-unit minimum,
  oblique near-boundary rounding, nearly equal oblique entries, equal-distance
  row ordering, multiple stale hits, hide/revive/next-tick cache rules, lazy
  distant-target eviction, row-set replacement, immutable table geometry, and
  the absence of a common-path sort in either implementation. Added explicit
  guards against NumPy calls in ordinary cold, warm, and stale small queries,
  plus oblique tests at adjacent representable values around the one-unit
  cutoff, checking both the answer and whether the live cache was initialized.
- Final focused AI/combat regression run: **242 passed**, no skipped, failed,
  or expected-failure tests. This includes all monster AI tests and the existing
  dense hitscan, shot-snapshot, and item runtime suites.
- Crossover benchmark: **2 passed**, all 79 warm/cold population/distribution
  scenarios executed with result/cache assertions. Without `--run-benchmarks`:
  **2 skipped**,
  confirming it is opt-in. Timings never decide pass/fail.
- An intermediate scalar regression involving a stale front hit and nearly
  tied farther hits was reproduced by a new test and fixed before final
  validation. The fallback now checks those ties before any cache eviction.
  There are no unresolved failures in the selected tests.
- `git diff --check` passed. No existing tests were weakened; no dependency or
  lockfile changes. The whole repository suite and GUI/renderer suites were
  not run for this CPU-only query change.


## Editor and engine performance pass

This pass keeps the ray-query changes above and profiles the wider development
workflow: MainWindow construction/asset startup, real map load/Play, 2D brush
painting, undo snapshots, simulation/publication, terrain generation, and OpenGL
rendering. It targets measured costs rather than rewriting every subsystem.
Raw timings and per-run details are saved in
[`audit/2026-10-16-editor-engine-performance.json`](audit/2026-10-16-editor-engine-performance.json).

### Editor changes

- `EditorState._selection_list` now deduplicates by an identity set while
  preserving first-occurrence order. Equal but distinct dictionaries remain
  distinct. This removes the quadratic identity scan from undo checkpoints.
- The 2D brush/entity draw passes build one short-lived `SelectionLookup` per
  draw. Identity matches are constant-time; brush ids narrow dictionary equality
  checks. Equal copies, duplicate/missing/unhashable ids, identity-only entities,
  and custom equality keep their prior membership behavior. Exceptional legacy
  data can still use the equality fallback. No persistent cache needs an epoch
  bump or new invalidation rule. The primary selection is read once per draw.
- Brush rectangles use a per-draw viewport/pan/zoom transform instead of
  repeated widget queries and intermediate Qt points per brush. Conversion and
  addition order match the old QPointF path, including float32 input rounding.
  There is no transform cache across redraws.
- No selection contents, undo format, drawing order, appearance, coordinates,
  filters, or mutation/restore behavior was deliberately changed.

Real MainWindow/View2D, top view, deterministic fully visible brush grids on a
640x480 QImage; median of five alternating current/reference calls in the same
window. GC is collected before each sample. The reference swaps in list
membership, the original QPointF rectangle projection, and the quadratic
identity-deduplication loop; it retains the common modern draw wrapper. This
interleaving reduces the substantial host drift seen between separate runs.
The original standalone-version results are also retained in the raw audit.

The six original/final QImage pairs are byte-identical (pixel hashes in the
audit). Automated tests additionally compare list/legacy-projection and current
pixels across all three view axes and multiple selection kinds, and exact
rectangle coordinates for 1200 float32/float64 position/size/pan/zoom cases.

| Brushes | Selected | Operation | Reference ms | Current ms | Reference/current |
| ---: | ---: | --- | ---: | ---: | ---: |
| 64 | 0 | draw | 1.240 | 1.158 | 1.07x |
| 64 | 0 | snapshot | 0.106 | 0.104 | 1.02x |
| 64 | 32 | draw | 1.628 | 1.291 | 1.26x |
| 64 | 32 | snapshot | 0.148 | 0.134 | 1.10x |
| 1024 | 0 | draw | 15.451 | 12.771 | 1.21x |
| 1024 | 0 | snapshot | 1.111 | 1.130 | 0.98x |
| 1024 | 512 | draw | 32.041 | 14.262 | 2.25x |
| 1024 | 512 | snapshot | 4.128 | 1.517 | 2.72x |
| 4096 | 0 | draw | 60.822 | 51.536 | 1.18x |
| 4096 | 0 | snapshot | 4.855 | 5.352 | 0.91x |
| 4096 | 2048 | draw | 281.282 | 58.475 | 4.81x |
| 4096 | 2048 | snapshot | 58.117 | 5.763 | 10.09x |

At 4096 brushes with 2048 selected, the interleaved drawing comparison improves
281.282 -> 58.475 ms (4.81x), and a full undo snapshot improves
58.117 -> 5.763 ms (10.09x). Empty-selection drawing also improves
60.822 -> 51.536 ms (1.18x). The profile had over two million identity-generator
calls in one large-selection snapshot; those scans are removed. The initial
separate-version pass measured drawing 255.768 -> 79.183 ms and snapshot
56.697 -> 7.172 ms before the final projection refinement.

Small/no-selection snapshot timings vary and some are slower; no universal
redraw or startup speedup is claimed. Profiled constructor/import startup
varied from 1262.899 to 3138.217 ms across separate runs (1583.518 ms in the
interleaved run), with native asset loading, imports, and sound decode
dominating. No startup/loader optimization was accepted or credited for a gain.

### Engine and renderer changes

- Perlin gradients select the signs of x/y from the hash's low bits, avoiding
  four full coordinate sums and repeated masked gathers/scatters. Coordinate
  floors are computed once per axis and reused. Scalar terrain APIs and the
  procedural recipe, seed, output dtype, and arithmetic order are unchanged.
- Terrain palette interpolation builds float32 weights only for the active
  interval. It no longer clears a whole-population weight array per stop or
  gathers those weights again for each color channel. Inclusive endpoints,
  duplicate/unsorted stops, out-of-range values, empty/single-stop gradients,
  NaNs, and float32 interpolation rounding retain the original behavior.
- Scene-capture texture sampler parameters are initialized once per new
  texture, instead of four `glTexParameteri` calls on every capture. Storage
  still resizes when needed, and pixels are still copied every pass. This
  avoids redundant state calls without changing refraction, water quality,
  shaders, render targets, or capture ordering. No GPU-time gain is attributed
  to removing these calls: the copy's profile time also includes waiting for
  preceding GPU work.

Terrain CPU benchmark: original frozen/current kernels, seeded equal inputs,
float32/float64, best of seven interleaved batches of 50 calls. Every timed case
first asserts exact output equality. Additional parity tests compare float bits,
including signed zero, empty/single batches, strided arrays, palette knots and
neighboring representable values, every built-in biome, and legacy gradients.

| Kernel | dtype | Values | Before us | After us | Before/after |
| --- | --- | ---: | ---: | ---: | ---: |
| noise | float32 | 64 | 89.63 | 52.24 | 1.72x |
| palette | float32 | 64 | 114.09 | 90.34 | 1.26x |
| noise | float32 | 1024 | 306.64 | 116.40 | 2.63x |
| palette | float32 | 1024 | 261.23 | 192.85 | 1.35x |
| noise | float32 | 16384 | 4095.68 | 1776.48 | 2.31x |
| palette | float32 | 16384 | 3300.80 | 1997.73 | 1.65x |
| noise | float64 | 64 | 88.12 | 50.58 | 1.74x |
| palette | float64 | 64 | 108.59 | 94.49 | 1.15x |
| noise | float64 | 1024 | 320.75 | 143.19 | 2.24x |
| palette | float64 | 1024 | 234.38 | 181.27 | 1.29x |
| noise | float64 | 16384 | 4473.43 | 2269.51 | 1.97x |
| palette | float64 | 16384 | 3418.12 | 2155.38 | 1.59x |

### Production-frame checks and limits

Six actual maps were measured without profiling, 120 frames per map after 30
warm-up frames, in serial before/after/after/before order. Before runs installed
the saved pre-pass methods at runtime rather than resetting the checkout; ray
queries were unchanged in both variants. Mesa llvmpipe, Xvfb, two rasterizer
workers, CPython 3.14.8 and NumPy 2.5.3. The following are means of the two run
medians, in ms. They are **not** a consistent overall FPS improvement:

| Map | Before logic | After logic | Before paint wall | After paint wall | Before paint CPU | After paint CPU |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| _SHOWCASE | 0.538 | 0.661 | 31.188 | 33.136 | 10.191 | 12.741 |
| MonsterTest | 0.622 | 1.409 | 21.445 | 32.194 | 4.832 | 12.047 |
| Terrain_Test_medium | 0.238 | 0.288 | 21.265 | 25.235 | 12.835 | 10.384 |
| Portal_Test | 0.385 | 0.422 | 19.646 | 24.589 | 8.011 | 8.587 |
| Office_Corridor | 0.333 | 0.376 | 14.345 | 18.121 | 2.827 | 2.984 |
| BigWorld_streaming_test | 1.156 | 1.168 | 133.056 | 136.343 | 122.407 | 124.194 |

The host and software renderer show substantial variation, including in logic
paths untouched by this pass. Several whole-frame measurements regress. Terrain
also uses a wall-clock build budget, so equal scripted ticks can build different
amounts of terrain when kernels get faster. Keep the local editor/kernel gains
separate from end-to-end frame-rate claims; large block terrain and scene-capture
GPU waits remain major costs.

`tools/bench_compare.py` now records the actual viewport and terrain built/visible
chunk and triangle counts. `--terrain-updates N` is a **benchmark-only** fixed
chunk-build limit that lifts the time budget, making the queued work comparable;
normal application defaults are untouched. A one-build/frame check on terrain
and streaming maps produced identical final viewport (456x472), built/visible
counts and triangle counts before/after. Its wall times were 22.941 -> 21.637 ms
for Terrain_Test_medium and 70.481 -> 86.211 ms for BigWorld_streaming_test: this
check also does not establish a general frame-rate gain. Both variants logged
an existing `terrain shader compilation: Link failure (0)` during map transition;
the built heightfield/block draw paths still executed. That warning was not
introduced or fixed by this pass.

Reproduce the local editor/kernel measurements and production workload from the
repository root with desktop dependencies installed. Run them sequentially:

```bash
QT_QPA_PLATFORM=offscreen python tools/bench_editor.py --compare \
  --json /tmp/editor.json --dump /tmp/editor-profiles
python -m pytest tests/performance/test_terrain_kernel_benchmark.py \
  --run-benchmarks -s -q
LIBGL_ALWAYS_SOFTWARE=1 LP_NUM_THREADS=2 xvfb-run -a \
  python tools/bench_compare.py --frames 120 --json /tmp/frames.json \
  maps/_SHOWCASE.json maps/MonsterTest.json maps/Terrain_Test_medium.json \
  maps/Portal_Test.json maps/Office_Corridor.json maps/BigWorld_streaming_test.json
LIBGL_ALWAYS_SOFTWARE=1 LP_NUM_THREADS=2 xvfb-run -a \
  python tools/bench_compare.py --frames 60 --terrain-updates 1 \
  maps/Terrain_Test_medium.json maps/BigWorld_streaming_test.json
```

### Validation

Before the editor/engine changes, the selected undo, terrain, and selection
suites passed **137 tests**, with no pre-existing test failure observed.

- Broad sequential CPU/editor run (editor, engine, undo/redo, persistence,
  integration, AI, work-count guards): **2128 passed, 25 skipped, 20 deselected**.
  All skips are optional pyflakes name checks; gl/benchmark cases were excluded.
- Renderer/visual suite under Xvfb/Mesa: **746 passed**, including the new
  capture texture test, terrain heightfield/pixel parity, water and portal paths.
- After the final 2D projection refinement, the selection, all view axes,
  component editing/interaction/identity, view filters, visgroups/cordon,
  selection overlays and undo/redo checks were rerun sequentially:
  **269 passed, 1 gl test deselected**. The changed rectangle path matches
  original coordinates exactly and all six original/final image artifacts match.
- Terrain opt-in CPU benchmark: **2 passed**, 12 before/after numeric cases with
  equality assertions. Timings do not determine test outcomes.
- No existing tests were weakened, dependencies changed, or quality settings
  reduced. `git diff --check` passed; settings.ini was restored after benchmarks.
  Earlier ray-query changes remain present and were included in the broad run.
- The shader link warning observed in both fixed-work benchmark variants above
  is an existing transition warning, not a new test regression. There are no
  unresolved failures in the selected suites. This pass did not run every
  repository test or measure hardware-GPU performance, and it does not claim
  every editor/engine path or whole-application FPS is now faster.

## 2026-10-16: Follow-up optimization and hardening

### Scoped audio archive loading

Sound preloading reopened `sounds.fiopak` for every member, rebuilding its ZIP
directory each time. `AudioArchiveReader` now owns one archive for the duration
of that preload. It reads and decodes one sound at a time, closes on normal or
exceptional exit, and keeps no persistent archive cache. Subsequent loads see
archive replacement. Project-relative keys, top-level bare-name aliases,
casefold sorting, duplicate-name last-entry behavior, and authoritative archives
with loose-file fallback only when absent are preserved.

Eager audio reads now reject a member whose declared size exceeds **256 MiB**
before opening/decompressing it, and also bound the read itself. This matches
the existing non-audio asset entry limit. The limit covers encoded member bytes,
not decoded PCM or the total sound pool. `open_archive_audio` retains its stream
ownership and size behavior. Encrypted/unsupported ZIP members and malformed
DEFLATE/LZMA data now surface as `BadZipFile`, which existing editor/runtime
error handling can report and skip. CRC checking remains active. Failed stream
opens close their owning ZIP, and a bad preload member does not block later
valid sounds.

### Heightmap import

The Qt image fallback now respects `bytesPerLine()` and excludes grayscale row
padding. Previously widths not divisible by four imported padding as pixels
and shifted later rows. Pillow sources now close explicitly. Both paths
normalize their owned float32 array in place, avoiding another full-size float
array. Pixel values remain bit-identical to the original Pillow normalization;
invalid images leave the previous heightmap and dirty state intact.

### Measurements and limits

CPython 3.14.8, NumPy 2.5.3, same shared Linux host as the earlier pass. Opt-in
benchmarks alternate frozen original/current implementations for seven rounds
and report the best duration, with equality assertions before timing and no
timing pass/fail budgets. All benchmarks and test suites ran sequentially.
Audio cases compare listing plus reading every member, excluding mixer startup
and decoding. Synthetic cases use nested names and alternating compressible /
seeded incompressible 4 KiB payloads.

| Archive | Members | Before ms | After ms | Before/after |
| --- | ---: | ---: | ---: | ---: |
| Synthetic stored | 1 | 0.054 | 0.040 | 1.37x |
| Synthetic stored | 16 | 1.302 | 0.270 | 4.82x |
| Synthetic stored | 128 | 53.149 | 2.343 | 22.68x |
| Synthetic stored | 1024 | 3311.747 | 23.077 | 143.51x |
| Synthetic DEFLATE | 1 | 0.059 | 0.042 | 1.40x |
| Synthetic DEFLATE | 16 | 1.368 | 0.448 | 3.05x |
| Synthetic DEFLATE | 128 | 63.056 | 2.912 | 21.65x |
| Synthetic DEFLATE | 1024 | 3959.679 | 28.084 | 140.99x |
| Bundled sounds.fiopak | 20 | 78.347 | 76.566 | 1.02x |

The bundled archive contains 11,129,450 uncompressed bytes; decompression
dominates and the measured improvement is small. The large synthetic gains
show removal of repeated directory parsing as member counts grow; they are
**not whole-editor startup or frame-rate improvements**.
A separate cProfile check confirms `_RealGetContents` calls fall from **21 to
1** for the bundled archive; decompression remains 20 calls. Profiled durations
are retained in the audit record and are not mixed into the timing table.

The image benchmark loads real seeded grayscale PNGs through the complete
Pillow loader, using a terrain stub only for the downstream dirty notification.
Peak allocation is measured separately from timing with `tracemalloc`; this
includes tracked NumPy allocations, not all native memory or process RSS.

| Image | Before ms | After ms | Before/after | Before peak MiB | After peak MiB |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1024 x 1024 | 11.679 | 11.853 | 0.99x | 8.003 | 5.004 |
| 2048 x 2048 | 55.324 | 50.570 | 1.09x | 32.003 | 20.004 |

Both sizes reduce tracked peak allocation by about 37.5%. Timing at 1024² is
roughly unchanged/slightly slower; this does not establish a universal image
loading speed gain. Raw results and validation are recorded in
`audit/2026-10-16-followup-hardening.json`.

Reproduce from the repository root, sequentially:

```bash
python -m pytest tests/performance/test_audio_preload_benchmark.py \
  --run-benchmarks -s -q
QT_QPA_PLATFORM=offscreen python -m pytest \
  tests/performance/test_heightmap_loading_benchmark.py --run-benchmarks -s -q
```

### Validation and existing defects

- Before this pass, the selected audio/archive/editor suites passed **59 tests**.
  Independent checks against the saved original code reproduced uncaught
  `RuntimeError` (encrypted), `NotImplementedError` (unsupported compression),
  and `zlib.error` (damaged DEFLATE), plus incorrect Qt heightmap rows at width 3.
  These defects predate this pass; they were not baseline suite failures.
- Audio/archive, new malformed-member/handle tests, Qt/Pillow image imports at
  eight widths, invalid-image state preservation, terrain load hardening,
  terrain serialization and stamp oracle: **117 passed**.
- Affected editor terrain tools, gameplay movers/speakers, prior crossfire
  equivalence and terrain kernel equivalence: **135 passed**.
- Real OpenGL terrain heightfield parity and texture rendering under
  Xvfb/Mesa: **22 passed**.
- Opt-in audio and image benchmarks: **11 passed**; output parity is asserted.
- No selected test failed, no existing tests were weakened, and no dependencies
  or runtime quality settings changed. Earlier optimizations remain intact.
  `git diff --check` passed. This is a targeted follow-up, not a fresh run of
  every repository test or a claim that every engine/editor path is faster.

## 2026-10-16: Continued editor/runtime optimization and hardening

### Runtime and editor changes

- Cutscene sampling selects the two bracketing frames in one stable O(N) pass,
  replacing sorting and repeated timestamp conversion on every camera/actor
  update. No persistent ordering cache is introduced. Frozen-reference tests
  cover equal timestamps, endpoint object identity, malformed/nonfinite times,
  NaN elapsed time, teleports, randomized tracks and edits between samples.
- Hierarchy refresh uses the same short-lived selection lookup as 2D drawing.
  It preserves copied-dictionary equality, missing/duplicate/unhashable ids and
  custom entity equality. Signal blocking now returns to the caller's prior
  state even when a refresh raises. Temporary highlighting restores both
  columns' backgrounds and respects search changes while highlighted.
- Block-terrain mesh construction writes position/UV components directly into
  the final interleaved float32 face buffer, broadcasts normals/colors, reuses
  repeated UV divisions, and avoids concatenating a single-part flat chunk.
  Vertex ordering, float bits, callback inputs/order, color broadcasting,
  callback buffer reuse, skirts, seams and coordinate rounding match the frozen
  original builder.

### Document, model and collision hardening

- Shared atomic output creates exclusive, unpredictable sibling files. Maps,
  settings, saves, terrain stamps and package exports never overwrite an
  unrelated fixed `.tmp` sibling or follow its symlink. Each save cleans only
  its own temporary file, including write/fsync/replace failures and failed
  descriptor wrapping. Normal output permissions/umask and encoding are
  preserved. Stamp/package output now also flushes and fsyncs before replacement.
- GLB accessors are bounded by their buffer view as well as the physical buffer.
  Invalid lengths and overlapping strides are rejected, preventing adjacent
  data from being interpreted as vertices and avoiding stride-based allocation
  amplification. Packed and valid interleaved layouts remain supported.
- Mixed indexed/non-indexed GLB primitives use their own EBO-element or
  VBO-vertex offset space. Previously a non-indexed primitive following an
  indexed one could receive a negative or unrelated draw offset.
- Batched spatial queries retain packed cell keys in the normal coordinate
  range and use exact coordinate lookup outside it. Distinct distant cells
  previously collided in the packed representation and could make batch floor
  or wall checks miss brushes that scalar queries found. Regression cases cover
  positive/negative aliases, range boundaries, huge integer keys and bucket order.

### Measurements

Benchmarks alternate frozen original/current code for seven rounds and report
the best duration, with output equality checks before timing. Benchmarks and
test processes run sequentially on the same CPython 3.14.8/NumPy 2.5.3 host.
These are local workload measurements, not whole-editor FPS claims.

Cutscene sampling: **72 scenarios** over 1/2/8/16/64/256 frames, ordered,
reversed, shuffled and tied timestamps, before/middle/after the track. All
measured cases improved, **1.21–4.65x**. Even one/two-frame tracks improved, so
the measurements did not justify a separate small-population dispatch.

Whole Qt hierarchy rebuilds, including creating rows and restoring selection,
showed the following representative cases. Empty/small selections can be
slightly slower; selection-heavy large scenes improved. Qt's own per-row
selection work still dominates the largest cases, so this is not a constant-time
refresh or a claim that the hierarchy has no remaining optimization potential.

| Rows / distribution | Selected | Before ms | After ms | Before/after |
| --- | ---: | ---: | ---: | ---: |
| 64 brushes | 32 | 0.792 | 0.830 | 0.95x |
| 1024 brushes | 0 | 4.836 | 5.184 | 0.93x |
| 1024 brushes | 512 | 72.896 | 60.956 | 1.20x |
| 4096 brushes | 2048 | 1267.590 | 937.969 | 1.35x |
| 4096 brushes | 4096 | 4215.386 | 3506.191 | 1.20x |
| 1024 mixed | 0 | 5.041 | 5.811 | 0.87x |
| 4096 mixed | 2048 | 1035.049 | 917.120 | 1.13x |
| 4096 mixed | 4096 | 3802.488 | 3509.860 | 1.08x |

All 18 hierarchy scenarios and the full cutscene/mesh timing rows are retained
in `audit/2026-10-16-continuing-optimization.json` with raw logs and limits.

Block mesh timing and separately measured tracked peak allocation:

| Pattern | Cells per side | Before us | After us | Before/after | Before peak bytes | After peak bytes |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Flat | 4 | 170.33 | 130.74 | 1.30x | 21150 | 17986 |
| Flat | 16 | 352.06 | 230.70 | 1.53x | 215021 | 154017 |
| Flat | 64 | 2710.32 | 1671.74 | 1.62x | 3291934 | 2336129 |
| Flat | 128 | 14035.10 | 7063.97 | 1.99x | 13121566 | 9302706 |
| Steps | 4 | 723.41 | 385.19 | 1.88x | 51742 | 52758 |
| Steps | 16 | 986.43 | 475.73 | 2.07x | 593934 | 605205 |
| Steps | 64 | 4854.06 | 4706.57 | 1.03x | 9045917 | 9221301 |
| Steps | 128 | 31656.76 | 30597.98 | 1.03x | 35956637 | 36657382 |
| Checker | 4 | 477.77 | 313.03 | 1.53x | 51590 | 52574 |
| Checker | 16 | 702.66 | 512.68 | 1.37x | 590894 | 601525 |
| Checker | 64 | 4765.74 | 5032.18 | 0.95x | 8994894 | 9159477 |
| Checker | 128 | 33820.19 | 32166.70 | 1.05x | 35747534 | 36404149 |

Flat chunks reduce tracked peak allocation by roughly 15–29%. Multi-part
chunks retain a small peak-allocation increase (about 2%) and the 64-cell
checker case is slightly slower. These limits are retained in the results;
there is no claim that every mesh distribution became faster or smaller.

Reproduce opt-in measurements sequentially:

```bash
python -m pytest tests/performance/test_cutscene_sample_benchmark.py --run-benchmarks -s -q
python -m pytest tests/performance/test_block_mesh_benchmark.py --run-benchmarks -s -q
QT_QPA_PLATFORM=offscreen python -m pytest tests/performance/test_hierarchy_refresh_benchmark.py --run-benchmarks -s -q
```

### Validation

- Hierarchy selection/signal/highlight and existing related editor tests:
  **36 passed**.
- Cutscene parity, cinematic I/O and editor preview restoration: **48 passed**.
- Block mesh byte/callback equivalence and existing terrain style/appearance:
  **108 passed**.
- Atomic saves/packages, GLB bounds/mixed primitives and spatial query checks:
  **157 passed**.
- Opt-in cutscene benchmark: **72 passed**; block mesh benchmark: **3 passed**.
- Opt-in whole-hierarchy benchmark: **2 passed**, covering 18 scenarios.
- Existing tests were not weakened. The previously uncaught atomic-temp,
  accessor-boundary, mixed-primitive and packed-cell defects are covered by new
  regression cases. No failure was observed in the completed targeted suites.
- Final combined persistence, physics, spatial, logic, affected editor/model
  and terrain suites after retaining the two newer remote showcase-map commits:
  **1234 passed**. The explicit nonfinite-coordinate case emits one expected
  NumPy invalid-subtraction warning while verifying those queries remain misses.
- Final real-GL terrain appearance, heightfield parity, texture rendering and
  capture-resource checks under Xvfb/Mesa: **29 passed**. `git diff --check`
  passed, and the working tree's settings and map files were not modified by
  the validation runs.
