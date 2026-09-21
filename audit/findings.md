# Fio 2.5.0.0_canary — incremental audit, headline findings

Range audited: `2.4.2.1709_Latest..2.5.0.0_canary`, **559 commits**, read in
28 chronological chunks (`commit_chunks/`), followed by subsystem passes
(`subsystem_audits/`) against the resulting code.

Baseline: `baseline.md`. Regressions: `regressions.md`. Fixes: `fixes.md`.
Architecture: `architecture.md`. Also `performance.md`, `compatibility.md`,
`testing.md`.

---

## The three that matter

**1. A 2.4.2 Tidy map lost every one of its objects on load.** Commit 517
deleted the `TidyObject` entity; commits 534–540 then deleted the plugin
map-migration API, Tidy's migration and the tests for both, on the grounds that
they were "unused". They were unused *inside the repository* — their users are
maps outside it. Measured: 41 of 46 entities dropped, silently, with a warning
only on stdout. A save afterwards made it permanent. This is a direct breach of
the stated release requirement. **Fixed and pinned.** (REG-03)

**2. Door, pickup and prop HUD prompts were silently removed** — the regression
you reported. Commit 481 made `_handle_triggers` assign `current_hud_message`
unconditionally; it runs last in the gameplay tick, so an empty sample erased
what `_handle_interactions` and `PropSession` had just published.
At 2.4.2 that assignment lived inside the in-range branch and could only ever
*add* a prompt. **Fixed and pinned.** `[E]` for use triggers does still reach
the HUD — but it is now sampled at the trigger's poll interval, so it can be up
to a second late and linger up to a second after you turn away (REG-02,
reported with a recommended fix rather than patched blind).

**3. The test suite could not be collected at all**, and had not been able to
for much of the branch. `tests/renderer/test_light_budget.py` carried literal
`\n\n` escapes from commit 396 (`SyntaxError`); `tests/io/test_io_contract.py`
had a dedented function tail (`NameError`, pre-existing at 2.4.2). Either one
aborts the **entire** pytest run. CI runs on every push to every branch, so it
was red and nothing gated on it. This is the mechanism by which everything
above shipped.

---

## The pattern behind them

Your own summary — *"the engine and editor were significantly updated but the
tests were not updated along with them"* — is exactly right, and measurable:

| | 2.4.2 baseline | 2.5 as found |
|---|---|---|
| Editor tier | 2303 passed, 3 failed, 1 collection error | collection aborted; **24 failed** once collectable |

18 tests newly failed. But the sharper finding is which ones: **the three
subsystems 2.5 changed most deeply are precisely the three whose new tests had
never run.**

* `tests/physics/test_dynamic_bodies.py` (added with the dynamic-body work) —
  6/6 failing. Its `Grid` stub predated the vectorisation that made
  `PhysicsWorld` read the spatial grid's internals.
* `tests/logic/test_trigger_filters.py` (added with the trigger rewrite) —
  errored on import at the very commit that added it, and had **never once
  passed**.
* The trigger-activator test (added with that feature) went into a module that
  could not be collected.

A test written alongside a feature but never executed provides no assurance
while reading as coverage. That, plus a suite that aborts on a collection
error, is how a 42-entity data-loss bug reached a canary branch.

---

## Also found and fixed

* **A disabled or Big-World-parked Prop kept falling.** Moving simulation from
  `PropSession` into `PhysicsWorld` (422–425) left the `disabled` check behind.
  Parked props cost physics time while dormant *and drifted away from where
  they were parked*, so restoring a cell restored them somewhere else. Nothing
  in `plugins/bigworld/` was touched by any of the 559 commits — this was pure
  interaction damage. (REG-04)
* **Selecting any brush wrote trigger properties into it** — mutating map data
  on a read-only action, and destroying the property panel's page cache so it
  rebuilt on every selection, drag and rotate. (REG-05)
* **The trigger scheduler drifted**: the per-trigger interval comparison had a
  float tolerance, the scheduler-tick comparison did not, and 60 frames of
  1/60 sum to 0.99999999999999989. (REG-06)
* **`PhysicsBody` lost its read side** in the NumPy rewrite — no way to ask a
  body its shape — and its accessors silently returned zeros before the first
  pack. (REG-07)
* **Plugin registrations vanished depending on test order** — the brief's
  specific concern, confirmed. Root cause was `conftest.py` restoring
  `IO_REGISTRY` per test while plugin registration is once-per-process.

## Answered: is the Prop duplication intended?

**No — and 2.5 has stopped pretending otherwise.** On the published snapshot
`engine/prop_entity.py` defines one `Prop`, resolving its base at import, and
both old names are aliases of it:

    editor.things.Prop is engine.prop_entity.Prop      -> True
    plugins.entitybase.Prop is engine.prop_entity.Prop -> True

At 2.4.2 they were two hand-synchronised classes with identical defaults; mid-
branch they drifted and were patched by hand (commit 466, "Keep plugin Prop
render mode in sync"). Collapsing them is the best architectural decision in
the range.

## What went well

Worth saying, because most of this history is careful work:

* The **dynamic-body ownership move** (422–428) finished properly, including
  removing Prop-specific naming from the engine — a migration that completed.
* **Floor contact was fixed five times, each with a regression test** (445–454)
  — tunnelling, sliding, seams, pushing, model origin. All five still pass.
* The **renderer vectorisation** targets measured hot paths rather than
  vectorising indiscriminately, and commit 353's stable-sort fix
  (`argsort(-d)` rather than `argsort(d)[::-1]`) is a subtle correctness catch.
* The **benchmark ends up in the right place** — an optional plugin, with no
  benchmark code left anywhere in the engine. `editor/io_system.py` was
  restored byte-for-byte to its 2.4.2 content when the experiment was reverted.
* **Tidy composed around a core primitive** instead of owning a near-duplicate
  entity, and its demo-map menu action respects the editor's unsaved-changes
  dialog.

## Cost of the benchmark detour

308 of the 559 commits — 55% — are the benchmark. It passed through a pytest
module, an editor dialog importing from `tests/`, a standalone script, a live
in-editor instance, a four-module split, a re-consolidation, an external IPC
process, and finally a plugin. Three supervision mechanisms were built and
discarded. It twice modified production code (`IOManager.fire_output`,
`SysMon`) and both intrusions were fully reverted — cleanly, and verified so.
The destination is right. Most of the journey is in the main branch.

## Current status

See `audit/status.md` for the live position: 10 confirmed regressions, 9
fixed, 1 left unchanged by design, every fix mutation-guarded, three CI tiers
green (2620 / 1641 / 33).

## Open items for you

1. **COMPAT-01** — `Thing.from_dict` still silently drops *any* unknown entity
   type. The Tidy case is fixed, but a missing or disabled plugin will do the
   same thing again. Two designs offered in `compatibility.md`; this is the one
   finding that needs a decision rather than a patch.
2. **COMPAT-05** — `render_mode` now authoritative with `'model'` as the
   default. Worth checking real authored maps for props that were billboards by
   the old implicit rule.
3. **REG-02** — use-trigger prompt latency.
4. **RISK-REPO-1 — RESOLVED.** The 559-commit history is on the repository as
   `refs/heads/history/2.5.0.0_canary-full` (tip `3a78157`), with continuous
   ancestry from `v2.4.2.1709`, so `git log`/`blame`/`bisect` cross the 2.4→2.5
   boundary again. It had to be a branch rather than a tag: this environment's
   egress policy refuses `refs/tags/*` (HTTP 403 — confirmed tag-specific, not
   size). `audit/status.md` has the one command to add a tag from any clone now
   that the objects are published.
