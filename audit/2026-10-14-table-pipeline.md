# Dense-table and GL audit

Baseline: `66e6b5f1b918700fad4af166ee4715cf039277d7` (`3.0.0.1010`).
Measured on GIL-enabled CPython 3.14.8, Linux x86-64 and Mesa llvmpipe.

## Retained optimizations

- Entity classification uses a read-only 64 KiB truth table over the existing
  classification word, hidden flag and sprite policy. All 65,536 words match
  the independent scalar predicate under every policy. Up to eight rows use
  scalar lookup; larger batches use one numerical lookup instead of rebuilding
  the boolean chain every frame.
- Dense AI movement converts an entire position array to Python float lists
  in one native pass. Python performs the required object assignments. The
  journal freezes those already-converted triples once, without converting
  each coordinate again. Scalar/non-numerical callers keep their original path.
- Model bounding radii retain a list of unresolved recipes. Loaded recipes
  are no longer scanned with `isinf`/`flatnonzero` every frame; unresolved
  resources still retry, and both alternating render buffers retain their cache.
- Model-axis culling uses a strided view of the padded matrix columns rather
  than stacking and copying them. Shadow-model collection reuses its gathered
  positions. The reduction order and output dtype remain unchanged.
- Frustum extraction reads GLM columns once, retaining the original scalar
  arithmetic and normalization order.
- Small model runs gather XYZ into their staging buffer in one operation.
  Above 512 rows the original column gathers win and are retained.
- Light slot keys convert the numerical array in one native operation.
  Duplicate UBO setup/binding is removed: the real upload function still
  ensures and binds the buffer, including cache hits.
- Collected change-journal subscribers release pending notification and
  position dictionaries immediately. Live subscribers retain their original
  notification behavior; repeated Play/Stop no longer retains the dead data.

The dense projections already avoid re-reading authored objects on still
frames. Geometry matrices, culling products, effect clocks and instance
packing already operate numerically. Broad algebraic rewrites were avoided
because changed reduction/rounding order would violate exact output equality.
Small material groups, entity identity writes and editing operations still
require scalar orchestration.

## Measurements

These are same-input microbenchmarks with real entity objects/tables. They are
not estimates of total game FPS.

| Operation | Rows | Before | After |
| --- | ---: | ---: | ---: |
| Entity classification | 1 | 24.36 us | 1.58 us |
| Entity classification | 8 | 25.39 us | 4.01 us |
| Entity classification | 32 | 24.51 us | 8.41 us |
| Entity classification | 256 | 26.25 us | 10.12 us |
| Entity classification | 4096 | 52.82 us | 39.43 us |
| Position assignment + journal recording/draining | 1 | 2.65 us | 2.20 us |
| Position assignment + journal recording/draining | 256 | 245.01 us | 79.73 us |
| Position assignment + journal recording/draining | 1024 | 1350.17 us | 368.14 us |

The production `tools/bench_compare.py` run (120 frames, unchanged water
budget for this comparison) reduced Showcase paint calls from 5,398 to 5,066
per frame. MonsterTest dropped from 2,989 to 2,908. Showcase light-buffer
ensure calls fell from 1,920 to 960 over the run. Whole-frame wall times were
collected during concurrent validation and are not used to claim an FPS gain.

An attempted instance-offset cache changed six Showcase pixels because an
offset alone does not describe GL's current buffer binding. It was rejected.
No such cache remains in the implementation.

## Exact-output evidence

`tools/oracle_capture.py` was run on plain source snapshots of the baseline
and candidate, with seeded input and an unbudgeted deterministic terrain
streaming path. The tool records both Debug Tables and independent reads of
published columns, draw calls and pixels.

All **2,416 records were byte-identical** across Showcase, MonsterTest,
Portal Test and BigWorld streaming at four checkpoints per map. Both runs
used the original 20-step water shader, isolating the numerical optimizations
from the requested visual change. The earlier two-map capture also matched
all 1,208 records. The oracle's inactive zero effect-clock sentinels are now
left at zero, instead of subtracting different process origins from them.

Old grass instance data was compared with the original implementation at
densities 0, 0.002, 0.02, 0.04 and 0.06, over four chunks: every byte matched.
New movement tests check real objects and journal snapshots for multiple
numerical dtypes and non-contiguous input arrays. No owner methods or thresholds
are replaced to obtain these results.

## Requested features

Expensive water has 10 SSR march steps instead of 20; confidence fade follows
the new budget. The existing five binary-refinement steps and cheap-water path
are preserved. This requested visual change is intentionally excluded from
the old/new pixel equality comparison.

The Terrain editor's Grass group now exposes blade height in world units.
Height changes the existing shader uniform without rebuilding instance data,
and persists as `grass_height`. Old maps keep the 5-unit default. Density's
maximum is now 0.24, four times its former 0.06 limit. The per-chunk tuft cap
scales above the old ceiling, reaching 16,384 tufts at the new maximum; old
density settings retain their old scatter and cap.

Separate editor, sprites, textures, models, fonts and music archives are
created alongside the existing sounds archive. Startup restores missing
non-audio files before Qt constructs the splash or toolbar. Existing loose
non-audio files remain overrides. Audio retains its native, archive-first
readers. See `assets/README.md` for archive layout and reproducible packing.

## Regression workflow

Install the pinned runtime requirements plus `pytest`, `pyflakes` and
`glslangValidator`. From the repository root, with the development interpreter:

```bash
QT_QPA_PLATFORM=offscreen python -m pytest -m 'not gl' --run-benchmarks -q -ra
LIBGL_ALWAYS_SOFTWARE=1 QT_QPA_PLATFORM=xcb xvfb-run -a \
    python -m pytest -m gl --run-benchmarks -q -ra
```

Both commands together select every test, including benchmarks. Graphics runs
use a separate identical filesystem snapshot when run concurrently, so editor
settings writes cannot interfere between processes.

Final results: **5,471 editor/core tests passed and 214 GL tests passed**,
including benchmarks: **5,685 passed, zero failures, zero skips**. After the
archive completeness check added PPM model textures and the 3D noise data,
all 13 archive/startup tests passed again. Every one of the 187 packaged
payloads matches its loose original byte for byte.

The existing suite issues were corrected using real behavior: shipped audio
references are read through the actual archive reader; terrain stamp tests
have their Qt tier declared and build enough real stamps to exercise the real
vector threshold; keyboard sessions activate the actual editor window before
sending shortcuts. Assertions and memory-growth limits remain intact.
