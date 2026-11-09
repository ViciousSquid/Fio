# Fio 3.1 brush model, Stage 2: handoff

State at handoff (2026-11-09). Stage 2 is **not accepted**: the performance
gate against Stage 1 has not passed. **Do not begin M3**, and do not accept
Stage 2 until the user has reviewed the results.

## The architectural requirement

Fio 3.1 must **minimise Python execution in hot paths through dense
numerical tables and change-driven publication**. Optimising Python
dictionary lookups is not the goal; the last round showed it has run out
(see *Rejected approaches*).

The mover table already keeps dense columns (`pos`, `rot_angle`,
`rot_axis`, `live`) and publishes only the rows that change. The remaining
overhead is Python on the *reader* side, one brush at a time:

* Each moved box's AABB is re-derived per brush on its first query after
  every move: find the transform in a runtime source, build the box through
  GLM, write the dict keys, register for invalidation.
* The player's collision passes walk every mover and door in the level in
  Python, every tick (`_has_headroom` once per brush per pass).
* Every placement stamp drops the cached AABB of every mover queried since
  the last one, **including movers at rest**, so a level with N movers
  re-derives up to N boxes a tick when any one of them moves.

Work should scale with contacts and with changed rows, not with movers.

## Branch and commits

* Branch `3.1.0.0611_CANARY`. HEAD **`4db693d`** is pushed; the working
  tree was clean before this handoff.
* Stage 1 baseline: **`56ec0e3`** ("Brush model Stage 1: one material
  record per face").
* Stage 2 commits run `a3f2253`..`4db693d` (design, 2a–2d, fixes, and the
  performance commits `2741be4` … `f1556d0`); `git log 56ec0e3..HEAD` lists
  them. The full record is in `docs/brush-model-stage2.md`, with the gate
  in §11.6.
* This handoff commit adds:
  * this file;
  * `tools/bench_player_movers.py`: a per-tick call count, first and
    second update timings, and a bit-exact trajectory trace, on any tree;
  * `docs/stage2-handoff/turning-ids-gather.patch`: the unvalidated
    experiment below, **not applied**.

## Measurements: player beside eight moving boxes

Measured on the session VM (4 vCPU Xeon @ 2.1 GHz, CPython 3.13, NumPy 2.5,
llvmpipe). Nothing has been measured on the Snapdragon 8cx Gen 3 / Adreno
target.

Python calls in one `Player.update` per tick (cProfile, 900 ticks;
deterministic):

| Tree | Calls per tick | vs Stage 1 |
|---|---|---|
| Stage 1 `56ec0e3` | 283.4 | -- |
| HEAD `4db693d` | 341.5 | **+58.1** (the original "+58") |
| HEAD + experiment patch | 310.4 | **+27.0** |

The first count of +58 (285.4 against 343.5) came from a harness that also
profiled its own two timer calls; `bench_player_movers.py` gives the same
differences.

What the remaining +27 calls a tick are:
* 8 `_brush_aabb_runtime` (one per moved box);
* 9 `id()`;
* 6 dict `get` (HEAD has +29);
* 2 `getattr`;
* one `resolved_runtime` (step-up);
* one `turning_ids`.

Time per update (µs, min / median of 9 interleaved runs, pinned to core 3):

| Tree | First update after the move | Second (all cached) | Miss share per moved box |
|---|---|---|---|
| Stage 1 | 16.8 / 21.4 | 13.0 / 16.6 | 0.48 / 0.60 |
| HEAD | 29.3 / 37.1 | 18.5 / 23.7 | 1.30 / 1.67 |
| HEAD + patch | 26.6 / 29.9 | 16.8 / 18.8 | 1.23 / 1.38 |

**Remaining first-update overhead with the patch: +9.8 µs at the minimum
and +8.5 µs at the median (about 1.4–1.6× Stage 1).** It splits into:
* about 6 µs from the eight moved-box misses (+0.75 µs each);
* about 3–4 µs in the cached update (the step-up's resolution, the
  turning check, the extra dict tests).

The source update itself (`RuntimeOverrides.set`, ~7 µs for 8 boxes,
against Stage 1's ~1.5 µs `pos` writes) is not timed by the gate.

One moved box's AABB miss, priced ingredient by ingredient (ns, timer
corrected, min of 15 rounds):

| Ingredient | ns |
|---|---|
| Whole miss | 890 |
| Stage 1's whole miss, for comparison | 500 |
| GLM box arithmetic, the same in both | ~365 |
| Source scan | ~150 |
| `_MoverKey` tag | 68 |
| Registry | 34 |
| Stamp recheck | 28 |
| Per-stamp cache | 28 |

No single ingredient is worth chasing; the per-brush Python path is.

## Remaining regressions

Gate on HEAD's code (`f1556d0`), 73 metrics, five rounds: 14 flagged. A
nine-round run separates the robust results from noise; an A/A run of
Stage 1 against itself flagged 1 metric of 37.

| Metric | Ratio vs Stage 1 | Cause |
|---|---|---|
| `player_spinning_boxes` | 2.34–2.47× | ~+17 µs runtime AABB path, ~+17 µs true-shape work (a rotated frame and plane tests per spinner) |
| `player_angled_movers` | 2.15–2.26× | +8.5 µs overhead, +9.5 µs true-shape work |
| `player_spinning_angled` | 2.09–2.26× | +15 µs overhead, +4.8 µs true-shape work |
| `player_box_movers` | **1.76–1.77×** | +13 µs overhead with no true-shape work: the per-brush miss, the gather and the step-up (the patch takes about a third of it off) |
| `movers{8,32,128}_aabb_moving` | 1.2–1.6× | The first query of a moved mover resolves it through the runtime layer, averaged over 20 queries a tick |
| `player_static_angled`, `player_static_boxes`, `movers*_tick`, `movers*_publish`, `movers32_rows_first`, `frame_big_moving_ms` | 1.07–1.25× | Not demonstrated: not significant over nine rounds, or contradicted by other runs |

Representative gameplay (`_SHOWCASE`, MonsterTest, BigWorld,
Office_Corridor) is level: frame, logic, paint and AI ratios are 0.99–1.13
and none is significant, and logic-frame Python calls are within 0.6 %.
The regressions are synthetic worst cases: eight shapes, all moving, beside
the player's path. The collision ablation and the per-metric reasoning are
in `docs/brush-model-stage2.md` §11.6.

## The experiment patch: worth preserving, not yet validated

`docs/stage2-handoff/turning-ids-gather.patch` (applies cleanly to
`4db693d`).

* `brush_placement.turning_ids()` gives a per-stamp union of each source's
  `turning_ids` set: the ids whose entry has a non-zero angle. It is a
  superset, and `None` if any source cannot say.
  * `RuntimeOverrides` and `MoverTable` keep the set: the id is added
    before a turned entry is stored and discarded after an unturned one.
  * `MoverTable._adopt` builds the set aside, and `reset` replaces it.
* The `Player.update` gather tells a box by two dict tests. A box mover or
  door is resolved only if its id is in `turning_ids()`, so non-turning
  box movers go straight to the AABB passes.
* The step-up reads a box mover's top from `resolved_runtime` inline, with
  no `world_bounds` call.
* The `_aabb_runtime` key and `brush_aabb_runtime()` are removed (their
  only reader was the old gather).

Validated so far:
* headless tier: 3,791 passed, 140 skipped (the same as HEAD);
* trajectories byte-identical to HEAD over 18 lanes × 960 ticks (`--trace`);
* gate against HEAD, `movers,player,aabb`, five rounds: 33 metrics, 0
  regressions (`player_box_movers` 0.88×, `player_spinning_boxes` 0.81×,
  `movers128_publish` 0.93×).

**Not yet run:**
* the Qt tier;
* the GL tier and the full suite;
* the `static`, `ai`, `frames` and `gameplay` probes;
* a gate against Stage 1.

One thing to watch: the set upkeep per publication when spinners move (the
movers probe spins every third mover; no regression was seen). A
zero-per-tick alternative derives the superset from the `rotate` column
plus restored angles at `sync`/restore time.

## Next steps (designed, not implemented)

1. **Change-driven invalidation (dirty rows).** Writers pass the brushes
   they changed: `note_runtime_change(changed)`.
   * The stamp then drops only the cached keys that are both changed and
     registered, iterating whichever set is smaller. That is never more
     work than now.
   * Registered movers at rest keep their hits.
   * Source registration and unregistration, `reset`, `_adopt`,
     `clear()` and authored changes still drop everything.
   * The writers: `MoverTable._publish` (return `chosen` from
     `_publish_into`), `_publish_row_now`, `_store_one`, and
     `RuntimeOverrides.set` / `clear(brush)`.
   * Measure it on a scene where most queried movers are at rest
     (BigWorld has 34 movers).
2. **A dense effective-bounds projection.**
   * In the same NumPy step as publication, the mover table writes float32
     `lo/hi` for the changed rows. This is bit-identical with GLM if done
     in float32: `f32(c) - f32(size)*0.5`.
   * The player tests all mover and door boxes against its swept box in
     one vectorised call, and runs Python only for the boxes it overlaps.
   * The per-brush dict cache stays as the path for single-brush queries.
   * A turned row's half-extents come from `settle_turns`.
   * This removes per-mover Python from the player's passes and from the
     miss.
3. Validate the patch (or its successor), then run the calibrated gate:
   an A/A run, nine rounds on `movers,player,aabb,frames`, and the full
   gate. Update `docs/brush-model-stage2.md` §11.6.

## Rejected approaches

| Approach | Result |
|---|---|
| Lazy moving-shape collision frames | Bit-identical, but slower: 522 → 575 calls a tick; spinning boxes 51 → 58 µs. Reverted. |
| A resolved transform cached as a `_placed` key on brushes | Broke `test_projecting_boxes_adds_nothing_to_them_and_solves_nothing`. Replaced by the resolver dict per stamp (`2cf2734`). |
| A cache keyed on the identity of `pos` | Broke `test_grouped_floor_query_reads_mover_heights_live`, which mutates `pos` in place. |
| Pushing derived AABBs into brush dicts at publication | Estimated +115 µs a tick at 128 + 128 rows, paid for rows nobody queries. Not adopted. |
| Dense row indices read as NumPy scalars | About the cost of a dict probe. Dense tables pay only when consumed vectorised. |
| Gather reading the per-stamp runtime cache inline | Tests pass; no measurable gain. |
| Miss-path micro-variants (a per-stamp tuple of active source dicts, the mover branch inlined into `brush_aabb_bounds`, no per-stamp cache store, invalidation by overwrite instead of `pop`) | Within noise: each ingredient is 28–68 ns. |
| Dropping the `_MoverKey` tag | Incorrect: a mover whose `pos` is edited without a stamp would take the plain-box miss. |

## Relevant files

* `engine/brush_placement.py`: the resolver.
  * Stamps (`placement_stamps`, `note_runtime_change`,
    `note_authored_change`) and sources (`register_runtime`,
    `runtime_of`).
  * `resolved_runtime` / `_runtime_cache`, and the `_runtime_cached`
    registry with `_drop_runtime_cached`.
  * `world_bounds`, `world_box(_at)`, `collision_shape(_at)`, `Frame`,
    `BulkBoxes`, `RuntimeOverrides`, `PREVIEW`.
* `engine/constants.py`: `brush_aabb_bounds` (the hit path, Stage 1's
  key), `_brush_aabb_runtime` (the mover miss), the `_MoverKey` and
  `_ShapeKey` tags, `brush_still_box`.
* `engine/mover_table.py`: the dense columns.
  * The tick: `_step`, `_commit`, `_one`.
  * Publication: `_publish_into`, `_publish_row`, `_store_one`, `_adopt`,
    `reset`.
  * `fill_boxes`, `settle_turns`, and the render-table publish.
* `engine/player.py`: the gather in `update`, `_move_with_collision`,
  `_try_step_up`, `_has_headroom`, `_resolve_collision`,
  `_resolve_mesh_capsule`.
* `engine/logic_player.py`: it hands the player **every** mover and door
  in the level each tick.
* The shared mover gather: `engine/logic_collision.py` (collision rows) and
  `engine/physics.py` (`SpatialGrid` cell rows).
* `engine/render_table.py` (`turning`) and `engine/logic_render.py` (the
  cull).
* Tools: `perf_gate.py`, `perf_probes.py`, `bench_collision.py`,
  `bench_compare.py`, `bench_player_movers.py`.
* Tests: `tests/physics/test_aabb_and_physics.py`,
  `tests/brush_model/test_runtime_transform.py` and
  `tests/helpers/runtime.py`. `tests/helpers/geo_reference.py` is frozen:
  do not modify it.

## Commands

Set up a Stage 1 tree beside the checkout:

```bash
git worktree add ../fio-stage1 56ec0e3
```

To try the experiment:

```bash
git apply docs/stage2-handoff/turning-ids-gather.patch
```

Calls and update timing for one tree:

```bash
taskset -c 3 python tools/bench_player_movers.py ../fio-stage1 --calls 40
```

Interleaved runs, 9 per tree; compare the minima and medians of `first`,
`second` and `miss share`:

```bash
for i in 1 2 3 4 5 6 7 8 9; do for t in ../fio-stage1 .; do taskset -c 3 python tools/bench_player_movers.py "$t"; done; done
```

Trajectory equivalence between two Stage 2 trees (for example, HEAD in a
second worktree against the patched tree):

```bash
python tools/bench_player_movers.py ../fio-head --trace /tmp/a.json && python tools/bench_player_movers.py . --trace /tmp/b.json && cmp /tmp/a.json /tmp/b.json
```

Gate subset (under a minute):

```bash
python tools/perf_gate.py --base ../fio-stage1 --head . --rounds 5 --core 3 --probes movers,player,aabb --json gate_mpa.json
```

Full gate, all seven probes (GL; long):

```bash
xvfb-run -a env LIBGL_ALWAYS_SOFTWARE=1 python tools/perf_gate.py --base ../fio-stage1 --head . --rounds 5 --core 3 --json gate.json
```

Test tiers. Headless (~1 min):

```bash
python -m pytest -q -p no:cacheprovider -m "not qt and not gl"
```

Qt (~22 min):

```bash
QT_QPA_PLATFORM=offscreen python -m pytest -q -p no:cacheprovider -m "qt and not gl"
```

Full suite, GL oracles included (~41 min). On `f1556d0`, which has HEAD's code: 7,221 passed, 150 skipped, 5 failed. The five are the known environment failures that fail the same way on Stage 1 (listed in §11.5).

```bash
xvfb-run -a env LIBGL_ALWAYS_SOFTWARE=1 python -m pytest -q -p no:cacheprovider -rf
```

## Ground rules

* Work on `3.1.0.0611_CANARY` only. No pull request unless asked.
* British spelling. No model identifiers in commits, code or docs; end
  commits with the attribution lines the session specifies.
* Never amend or rewrite pushed history; follow-ups go in new commits.
* Do not touch the Android `player/` folder.
* OpenGL 3.3 core only, and ARM-first: Snapdragon 8cx Gen 3 / Adreno 690
  is the target.
* Keep what Stage 2 preserves:
  * true-shape collision;
  * local-frame geometry;
  * the shared cube VBO;
  * the authored/runtime separation: never write runtime movement into
    authored `pos`.
* Keep only changes that show a measured improvement and no regression.
  Do not declare the gate passed unless the evidence supports it. Report
  measured results apart from estimates, and say what is unverified on the
  hardware.
