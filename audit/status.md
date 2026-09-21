# Fio 2.5 audit — status

Branch: `2.5.0.0_canary`. This file is the current state; the detail lives in
`findings.md`, `regressions.md`, `compatibility.md`, `architecture.md`,
`performance.md`, `performance_physics.md`, `testing.md`, `commit_chunks/`.

## 1. The 559-commit history is preserved, on the real repository

| | |
|---|---|
| **Reference** | `refs/heads/history/2.5.0.0_canary-full` |
| **Commit** | `3a78157` ("tidy: move demo map into plugin") |
| **Range** | `bcf394e` (v2.4.2.1709 tip) .. `3a78157` = **559 commits** |
| **Pushed** | **Yes** — verified present on `github.com/ViciousSquid/Fio` |
| **Ancestry** | Continuous from `v2.4.2.1709`; `git log/blame/bisect` cross the 2.4→2.5 boundary |
| **Integrity** | `git fsck` clean; 0 missing commits; 9268 objects reachable |

A **branch** ref, not a tag: pushing `refs/tags/*` is refused by this
environment's egress policy (HTTP 403 — verified it is tag refs specifically,
not upload size, by pushing a tag pointing at a commit the remote already had).
The objects are now on the remote, so a tag can be added from any clone with:

    git fetch origin history/2.5.0.0_canary-full
    git tag -a 2.5.0.0_canary-full-history origin/history/2.5.0.0_canary-full \
        -m "Full 559-commit development history of 2.5.0.0_canary"
    git push origin refs/tags/2.5.0.0_canary-full-history

Nothing was reset, rebased, squashed or orphaned. `2.5.0.0_canary-backup`
(the original squashed orphan, `8748305`) is untouched.

## 2. Regressions

**10 confirmed. 9 fixed. 1 left unchanged by design. 0 awaiting a decision.**

| ID | Severity | What | Status |
|---|---|---|---|
| REG-01 | S1 | Door/pickup/prop HUD prompts erased by the trigger stage | **Fixed** |
| REG-02 | S2 | Use prompt up to 1 s stale in both directions | **Fixed** |
| REG-03 | S1 | 2.4.2 Tidy map lost 41 of 46 entities on load | **Fixed** |
| REG-04 | S1 | Disabled / BigWorld-parked props kept falling and drifting | **Fixed** |
| REG-05 | S2 | Selecting any brush authored trigger keys into it; page cache dead | **Fixed** (code was already right; had no test — now guarded) |
| REG-06 | S3 | Trigger scheduler drifted behind its configured interval | **Fixed** |
| REG-07 | S3 | `PhysicsBody` lost its read side in the NumPy rewrite | **Fixed** |
| REG-08 | S3 | `yield_hook` accepted, threaded through, never called | **Fixed** |
| REG-09 | S2 | Use trigger fired from ~1.73x its authored radius | **Fixed** |
| REG-10 | S3 | Spent `once` button kept advertising itself | **Fixed** |

**Left unchanged by design:** a thing record with **no `type` key at all** is
still skipped rather than preserved. Preservation exists so an entity whose
plugin is missing survives a round trip — such a record is identifiable, holds
authored content, and a plugin may supply its class later. A typeless record is
none of those. This is a deliberate contract pinned by
`test_a_thing_with_no_type_is_skipped_rather_than_crashing_the_load`; an
earlier over-generalisation of the preservation policy was reverted rather than
changing that test.

Every fix has a guard test, and **every guard was verified by mutation** —
reintroducing the defect makes the named test fail. No test was modified to go
green where doing so would hide a behavioural regression; where a test staged
behaviour through a mechanism that no longer exists (the poll-sampled prompt),
it was rewritten to exercise the real path, which is stronger coverage.

## 3. Architectural decisions taken

* **COMPAT-01 — unknown entities.** Preserve-and-round-trip, not refuse-the-
  load. Fio deliberately lets plugins be toggled while maps are open and
  auto-enables them from map content; refusing would fight that. Implemented on
  this branch as `UnresolvedThing` (verified byte-exact across two round trips,
  through undo/redo, and with duplication). Two further holes found and closed:
  the legacy `Model` fast path raised out of `load_from_data`, costing the
  author every entity after a malformed record.
* **Trigger volume.** `use_radius` is a sphere — the exact activation radius in
  every direction. The batched AABB is an acceleration structure that nominates
  candidates and never decides. One predicate,
  `LogicThread.use_trigger_contains`, shared by the prompt and firing paths so
  they cannot drift. Touch triggers remain boxes: they are drawn brushes with a
  `size`, not a radius, and converting them would change every existing map.
* **Plugin registration.** Made replayable rather than fire-once.
  `EditorAPI.extend_io` closes the API gap that forced Tidy to reach past the
  plugin API into `editor.io_system`. The recorded list is isolated per test.

## 4. Architectural intent preserved

No 2.5 improvement was reverted to fix a regression. Specifically kept:
engine-level primitives (one `Prop` via `engine/prop_entity.py`),
plugin-authoritative behaviour, the batched trigger architecture, the new
physics architecture, BigWorld semantics, and the optional-plugin/core
separation. Fixes were made to the contracts *around* the new architecture.

**Vectorisation was extended, not unwound.** `_batch_floor` went from 5N scalar
raycasts to cell-grouped NumPy (3.3x at 400 bodies, 6.4x at 1000), gated at 64
active bodies because below that the assembly costs more than it saves. Bodies
are bit-identical between the two paths. Rotation integration is batched
(2.5–3x). The one scalar read that remains — cell AABBs from brush dicts — is
required so movers are seen live, and is per *cell*, not per sample.

## 5. Test suites executed

| Tier (as `.github/workflows/tests.yml` runs it) | Result |
|---|---|
| Core (headless, no Qt, no GL) | **1641 passed**, 1 skipped |
| Editor + engine (Qt, offscreen) | **2620 passed**, 13 skipped |
| Renderer (OpenGL, llvmpipe under xvfb) | **33 passed**, 3 skipped |

Order-independence checked explicitly: the guard suite passes alone, as a
directory, in both orders across the plugin/IO boundary, and inside the full
run. Two order-dependencies were found and fixed during this work — the
original plugin-registration one, and one introduced by its fix.

`tests/test_suite_integrity.py` now compiles every test module and checks each
declares tests, so the defect class that hid all of this — a test file that
exists but never executes — is reported by name instead of aborting the run.
Verified by reintroducing the exact commit-396 `\n\n` defect.

## 6. Remaining known risks

1. **COMPAT-05** — `render_mode` is authoritative with `'model'` as the
   default. A 2.4.2 prop authored with a `sprite_path` and no `model_path` was
   a billboard by the old implicit rule. Worth checking against real authored
   maps; not reproducible from the maps in this repository.
2. **Shader-source regex rewriting** — the light UBO is injected by rewriting
   GLSL at runtime, and nothing tests the rewritten string. Three commits were
   needed to get that regex right and two more to fix literal `\n` escapes in
   the generated source.
3. **`SysMon` culled-brush count** is derived across the thread boundary
   (`editor_total - snapshot_visible`) rather than taken from
   `render_state.culled_brushes`, which is now unused. Feeds the benchmark's
   culling report.
4. **`angular_damping`** is authored on every Prop and read by nothing — true
   at 2.4.2 as well.
5. **Physics console commands** mutate `PhysicsWorld` from the Qt thread while
   the logic thread steps. Developer tools; not observed to fail.
6. **`plugins/benchmark/fio_benchmark.py`** defines `load_live_benchmark_world`
   twice; the second shadows the first.
7. **Shared transform buffer** — state crosses four representations per frame.
   Deferred deliberately, with reasoning, in `performance_physics.md`.

## 7. Tests pass ≠ the architecture is audited

These are different claims and only the first is fully established.

**Established:** all three CI tiers are green; the ten regressions above are
reproduced, fixed and mutation-guarded; the history is preserved on the
repository; the map-compatibility policy is implemented and tested; the
vectorisation direction is measured rather than assumed.

**Not established:** the chronological pass covered all 559 commits at the
level of message, files touched and diffstat, with deep inspection reserved for
commits touching production behaviour — it is not a line-by-line reading of
every diff. Renderer teardown, resize and context-reload paths are untested in
2.5 *and* in 2.4.2, so this audit can say they are unchanged, not that they are
correct. The `.fiopak` player was exercised through its tests and the Tidy
integration, not on a real device or an ARM build. Nothing here is a
performance audit of the renderer under a real GPU: the GL tier runs llvmpipe.
