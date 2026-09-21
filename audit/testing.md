# Test architecture audit — 2.4.2 → 2.5.0.0_canary

## Headline

The maintainer's own summary is accurate and is confirmed by measurement:
**the engine and editor were significantly updated; the tests were not updated
with them.**

Measured with the CI "editor" tier command
(`QT_QPA_PLATFORM=offscreen pytest -m "not gl and not benchmark" -q`),
Python 3.11, full desktop dependency set installed:

| Branch | Passed | Failed | Collection errors |
|---|---|---|---|
| `2.4.2.1709_Latest` (baseline) | 2303 | 3 | 1 (`tests/io/test_io_contract.py`) |
| 2.5 development branch (as found) | — | — | 10 (suite could not even collect) |
| 2.5 development branch (after the two file repairs below) | 2314 | 24 | 0 |
| **after this audit's fixes** | **2372** | **0** | **0** |

**18 tests newly failed on 2.5 that pass on 2.4.2.**

Note on lineage: the numbers above were measured on the 559-commit development
branch (preserved as tag `2.5.0.0_canary-full-history`). The published
`origin/2.5.0.0_canary` is a separate, squashed snapshot that had independently
repaired several of the same defects and collected cleanly at 2349 passed; the
fixes it was still missing — the HUD clobber, the plugin-registration leakage,
the `PhysicsBody` accessors and the disabled-body guard — were ported onto it,
taking it to 2372. Both lines were measured; the findings below hold for both
unless stated.

CI (`.github/workflows/tests.yml`) runs on `push` to `branches: ["**"]`, so the
`Core (headless…)` and `Editor + engine (Qt, offscreen)` jobs have been red for
much of this branch's life. Nothing gated on them.

## Two modules could not be collected at all

Before any of the above could be measured, two test modules had to be repaired.
While either is uncollectable, **pytest aborts the entire run** (`Interrupted:
N errors during collection`), so *no* test in the suite executes. This is why
the breakage was invisible.

### 1. `tests/renderer/test_light_budget.py` — SyntaxError (2.5 REGRESSION)

Introduced by commit **`fc1bd88` (#396) "Update light UBO layout test"**.
The appended test block was written with **literal `\n\n` escape characters**
instead of real newlines, into a CRLF file, with an LF-only tail:

    assert "'lowpower_mode'" in renderer_src<CR><LF>
    \n\ndef test_light_ubo_cpu_layout_matches_std140_light_struct():<LF>

`SyntaxError: unexpected character after line continuation character`.

Same defect class as commits 374/375 ("Fix GLSL newline escapes in instanced
shader generation", "Correct newline escaping in instanced GLSL source") — the
tooling that generated the light-UBO work emitted escaped newlines more than
once, and this one was never caught because it landed in a test file.

**Consequence: the entire light-budget module — 26 tests, the only coverage of
`MAX_LIGHTS`, the ARM light cap and the new std140 light-UBO layout — has never
run since the UBO rewrite.** The UBO work is commits 365–398, which contains
eight separate "Fix light UBO …" repair commits; its test was dead throughout.

Repaired: real newlines restored, whole file normalised to CRLF.
Result: 26 tests collect; 26 pass.

### 2. `tests/io/test_io_contract.py` — NameError (PRE-EXISTING, not 2.5)

The tail of `test_io_disabled_source_does_not_fire_output` is dedented to module
level, so `mgr._execute_input = tracking_execute` executes at import:
`NameError: name 'mgr' is not defined`.

**This is present in the 2.4.2 baseline too** — verified by running the baseline
module directly. It is a pre-existing defect, not a 2.5 regression. Its sibling
`test_io_disabled_target_does_not_receive_input` is the correctly-indented
template.

Repaired by re-indenting into the function body.

**Consequence, and it matters for 2.5:** because this module has never been
collectable, commit **`aa56788` (#476) "Wire trigger activators through I/O"**
added `test_fire_output_propagates_explicit_trigger_activator_through_delay`
into a dead module. 2.5's trigger-activator propagation feature has **never had
its test executed**, and it does not pass (see below).

## Baseline failures (pre-existing — NOT 2.5 regressions)

These fail on `2.4.2.1709_Latest` as well and are out of scope for a
2.4.2→2.5 regression report, though they are real:

* `tests/editor/test_property_editor_rebuilds.py::test_light_show_radius_row_labels_itself`
* `tests/editor/test_property_editor_rebuilds.py::test_light_show_radius_accepts_a_string_value`
* `tests/editor/test_property_editor_rebuilds.py::test_light_show_radius_writes_back`
  — all `KeyError: 'light_show_radius_cb'`
* `tests/io/test_io_contract.py::test_nothing_is_implemented_that_is_not_declared`
* `tests/io/test_io_contract.py::test_every_registered_type_has_an_instance_the_probe_can_build`
* `tests/io/test_io_contract.py::test_io_disabled_target_does_not_receive_input`
  — the last passes raw dicts as `_io_connections` where `IOManager` requires
  `IOConnection` objects (`AttributeError: 'dict' object has no attribute
  'output_name'`); latent behind the collection error until now.

## The 18 new failures (2.5 regressions or stale tests)

| Module | Count | Cluster |
|---|---|---|
| `tests/physics/test_dynamic_bodies.py` | 6 | **2.5's own new physics suite — every test in it fails** |
| `tests/editor/test_property_editor_rebuilds.py` | 6 | property-panel rebuild/caching |
| `tests/integration/test_backport_integration.py` | 2 | distance cull ↔ `_sort_objects` |
| `tests/io/test_io_contract.py` | 1 | trigger-activator propagation (2.5's own new test) |
| `tests/renderer/test_render_cull.py` | 1 | batched positions `out` tracking |
| `tests/test_prop_runtime.py` | 1 | core Prop pickup/drop without plugins |
| `plugins/tidy/tests/test_player.py` | 1 | standalone player HUD |

Triage per cluster is in `regressions.md`.

## Structural observation

Three of these clusters are **tests 2.5 wrote for its own new features which
have never passed**:

* `tests/physics/test_dynamic_bodies.py` (added by commit 430 "Add engine
  dynamic body physics tests") — 6/6 fail.
* `tests/logic/test_trigger_filters.py` (added by commit 477–482) — **4/4
  failed until repaired in this audit**; it errored on import at the commit that
  added it and has never once run green.
* `test_fire_output_propagates_explicit_trigger_activator_through_delay`
  (commit 476) — added into an uncollectable module.

A test written alongside a feature but never executed provides no assurance at
all; worse, it reads as coverage. The three subsystems 2.5 changed most deeply —
dynamic-body physics, trigger scheduling/filtering, and I/O activators — are
precisely the three whose new tests do not run.
