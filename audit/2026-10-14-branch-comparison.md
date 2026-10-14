# Direct branch comparison and merge

The comparison used these exact branch tips:

- `claude/bigworld-registry-light-ubo-grass`:
  `9806178c2bf811f9ad1f038a25a420981af1cf2b`.
- `codex/dense-table-audit-assets-grass`, before the merge:
  `a0b337e42236b5c296b6db593935d1f2d7da5107`.

Both started from `66e6b5f1b918700fad4af166ee4715cf039277d7`
(`3.0.0.1010`), with one distinct commit each. The comparison was a direct
tip-to-tip diff, rather than a diff against their common ancestor. Before
merging, neither branch contained the other's changes.

## Direct measurements before the merge

The unchanged production `tools/bench_compare.py` drove the actual editor,
simulation, table publication and GL renderer. Each run used seeded RNGs,
120 measured frames per map and cProfile. Runs were serial, in
Claude/Codex/Codex/Claude order, using the same CPython 3.14.8 interpreter and
Mesa llvmpipe under Xvfb. These runs used the actual branch shaders: 20 water
SSR steps on Claude and the requested 10 on Codex.

The table averages the two runs' results for each branch:

| Map | Claude paint calls/frame | Codex paint calls/frame | Change | Claude paint CPU median | Codex paint CPU median |
| --- | ---: | ---: | ---: | ---: | ---: |
| Showcase | 5,390 | 5,066 | -6.01% | 8.384 ms | 8.153 ms |
| MonsterTest | 2,983.5 | 2,907 | -2.56% | 4.816 ms | 4.861 ms |
| BigWorld streaming | 5,129.5 | 5,131.5 | +0.04% | 72.081 ms | 72.882 ms |

Codex reduces Python submission work in the first two maps. These timings
do not establish a universal FPS improvement: the MonsterTest and BigWorld
CPU medians are slightly higher, repeat counts are small, and software
rendering and profiling affect timings. BigWorld is dominated by terrain
work that the table changes do not address. The movement microbenchmark in
`2026-10-14-table-pipeline.md` measures larger numerical batches separately.

The full results, including per-pass times, logic times and both repeats,
are in `2026-10-14-branch-benchmarks.json`.

A further 120-frame profile of the merged implementation records 5,074
Showcase paint calls/frame, 2,913 for MonsterTest and 5,134 for BigWorld.
These are approximately 5.9% and 2.4% below Claude in the first two maps,
and essentially unchanged in BigWorld. The merged profile ran during core
validation, so its timings are retained for transparency but are not used
to claim a speedup. Python call counts also do not count every native GL call;
the light binding cache reduces native work even though checking that cache
adds some Python calls.

## What each branch contributed

Claude adds runtime entity reconciliation at the world-cache rebuild boundary:
new entities receive UUIDs and cell membership, departed entities are removed
from the registry, and new entities receive their existing cell's simulation
tier. Moving lights also follow their new cells. Codex had retained the
baseline behavior in these areas. The merge includes Claude's runtime files
without further production changes.

Claude's light UBO binding cache reduces indexed bindings to once per frame.
Codex had removed duplicate setup within each pass but still bound per pass.
The merge includes Claude's frame cache and reset, while retaining Codex's
native conversion of numerical slot keys.

Both branches add persisted blade height and raise the grass density ceiling
from 0.06 to 0.24. Claude's slider supports 0.0001 density steps across the
whole range and labels density relative to the 0.02 default. Codex's first
implementation used a coarser percentage-of-maximum slider. The merge uses
Claude's finer slider and label.

Claude raises the per-chunk tuft cap to 16,384 unconditionally. That changes
pre-existing scatter whenever an old density and larger chunk hit the old
4,096 cap. The merge preserves Codex's compatibility rule: keep the old cap
through density 0.06, then scale it up to 16,384 at density 0.24. This retains
old grass records while enabling the requested higher density.

Height retains Codex's supported 0.1–256 world-unit range and finite-value
handling. Claude's public `grass_height` attribute and Codex's existing
`GRASS_BLADE_HEIGHT` setting share one value, so either interface reaches the
shader and serializes correctly. Both editor height-widget names refer to
the same control.

Codex additionally supplies the numerical movement and classification
optimizations, model-radius cache, matrix-view and gather improvements,
collected-subscriber memory cleanup, asset archives, 10-step water SSR,
regression coverage and Debug Tables oracle checks described in
`2026-10-14-table-pipeline.md`. Those changes remain in the merge.

## Merge validation

The focused checks passed all 193 tests. New integration cases construct
real engine owners and entities: they verify that the world-cache rebuild
event files runtime spawns, assigns tiers and forgets removed entities, and
that an actual moving light remains active while its cell filing changes.
Grass tests verify the fine slider, actual terrain values, persistence and
the shared height setting. No runtime owner methods are replaced.

The unchanged Claude checkout also passed its 182 existing BigWorld,
terrain-appearance and light-budget tests selected for this comparison.

All **214 GL tests passed** on the merged tree. The full core/editor suite
is recorded after completion.

Debug Tables and independent table, draw-call and pixel captures match
before and after merging: **2,416 byte-identical records, zero differences**
across Showcase, MonsterTest, Portal Test and BigWorld streaming at ticks
0, 10, 20 and 30. These two captures both use the actual 10-step water shader.

Separate grass captures exercise densities 0, 0.002, 0.02, 0.04 and 0.06
on 256-, 512- and 1,024-unit chunks with real heightfields and grass
generation. All **15 merged records match the original baseline bytes**.
Claude's unconditional cap matches nine; six differ at the larger chunk
sizes. For example, at density 0.02 on the seeded 512-unit chunk, baseline
and merge generate 11,755 blades while Claude generates 15,180. This is why
the cap compatibility rule is retained.
