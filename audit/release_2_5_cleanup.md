# 2.5 release-readiness pass

Not an audit and not an optimisation project: a consolidation pass over the
tree that 2.5 actually ships, plus the one architectural migration the audit
had deferred — the Prop domain.

## 1. Tests

| | before | after |
|---|---|---|
| passing | 2666 | 2691 |
| skipped | 36 | 36 |
| failures introduced | — | **0** |

The suite was green at every step; no test was weakened or deleted to keep it
that way. Two tests changed shape because the thing they assert on changed:
`test_trigger_filters` now builds a `PropSession` instead of hand-setting the
Prop caches the engine no longer has, and `test_logic_state` asserts the
pre-2.4 alias is *gone* rather than that it resolves.

One of those edits is worth recording as a near miss. Replacing the end marker
in `test_the_state_entity_knows_about_no_other_system` with a class name that
appears *earlier* in the file made its source slice empty — the test kept
passing while checking nothing. Caught by checking the slice length rather than
the exit code. A green suite is not the same as a suite that still asks the
question.

## 2. The Prop domain migration

The audit had flagged `PropSession.props` as a third copy of the Prop set
(alongside `LogicThread._prop_things` and `_prop_by_id`) and left it, on the
grounds that the standalone player has no `LogicThread`. That reasoning was
wrong: it justified a duplicate by pointing at a tier that should have been
using the engine in the first place.

**Ownership now, stated once and asserted in tests:**

| | |
|---|---|
| World data | the thing list (`editor_state.things`, the package's things) |
| Prop data and session state | `PropSession` |
| Position while in motion | `PhysicsWorld` |
| Placement / reset / restore | whichever subsystem performs the operation |
| Spatial membership | `PropSession`'s `CellIndex` |
| Synchronisation | an explicit `moved(prop)` / `refile(props)` call |

`prop.pos` is the source of truth; the cell index is a derived acceleration
structure. That asymmetry is load-bearing: the index chooses which Props are
examined and live positions decide the answer, so a stale cell can cost a Prop
its place in a result but can never put a wrong one in. Pinned by
`test_a_stale_cell_can_only_lose_a_prop_never_invent_one`.

What went:

* `LogicThread._prop_things` and `_prop_by_id` — deleted. The trigger paths ask
  the session. One registry, derived in `_build_entity_caches` alongside every
  other entity cache.
* The per-frame liveness poll (`is_empty`) and the fingerprint machinery added
  for it — **deleted outright**. Once the registry re-derives at the same
  defined moments as the rest of the engine's caches, there is nothing to poll.
  The previous pass optimised that scan; this one removed the reason for it.
* `PropSession.has_props` — gone with its only caller.

### Player-to-Prop interaction is a spatial query now

`_pick_in_view` scanned every Prop in the map to find the one in front of the
player, at ~0.78 µs per Prop:

```
                  before      after
   10 props      8.3 us      5.4 us     1.5x
   50 props     36.2 us      7.2 us     5.0x
  200 props    142.1 us     10.7 us    13.3x
 1000 props    748.1 us     24.8 us    30.2x
 5000 props   3911.0 us     69.0 us    56.7x
```

The shape was the defect, not the constant: cost scaled with the whole map
while `pickup_reach` is 110 units against a 512-unit cell. 0.75 ms on a
keypress at 1000 Props is a dropped frame on the CPUs Fio targets.

It is faster at *every* size measured, including 10 Props, so there is no
crossover and no scalar fallback — the "keep a fallback only where it has a
demonstrated purpose" rule cuts against having one here. The residual growth is
real local density (more Props genuinely within reach), not a scan.

The index is `engine.spatial.CellIndex` — the shared bucketing primitive both
of Fio's spatial users already sit on — not a grid of its own. `CellIndex`
gained `remove_point` (by identity, because Things define `__eq__`), and
`engine.spatial` gained `cells_of_points`, the batched form of `cell_of_point`,
kept beside the scalar one so there is still one definition of which cell a
coordinate is in. It is duck-typed rather than NumPy-typed, so `spatial.py`
keeps its stdlib-only promise, and
`test_the_batched_cell_maths_matches_the_scalar_convention` checks the two
agree over 500 random points rather than assuming it.

### Why Props are not in `SpatialGrid`

`SpatialGrid` is a *static* index: populated once, rebuilt only on authored
visibility change, indexed by `authored_hidden` so a streamed-out brush
survives parking, and it explicitly excludes `_physics_body`. Filing moving
Props into it would break the invariant it exists to hold. The Prop domain owns
its own membership, on the shared cell convention, which is what
"spatial integration" means here.

### The synchronisation contract

Anything that moves a Prop outside `PropSession` either calls `moved(prop)` or
exposes the moved set through a batch interface. Every writer in the tree:

| writer | route |
|---|---|
| `PropSession._carry` | `moved()` — it is the mover |
| `PhysicsWorld` | batch: `entities_that_changed_cell()` → `refile()` |
| Tidy placement / reset | `moved()` |
| savegame restore | batch `refile()` — a save restores a level at once |
| Big World cell delta | batch `refile()` — one per cell |

The physics route deliberately does **not** call `moved()` per body per step.
`entities_that_changed_cell` answers the narrowest useful question with one
vectorised comparison over the body arrays — not "which are awake", not even
"which moved", but "which are no longer in the cell they were in". A body has
to cross a whole 512-unit column to appear in it, so the answer is normally
empty and the Python work is proportional to cell transitions rather than to
bodies.

Each of the five routes has a test, and **all five were mutation-verified** by
deleting the notification and watching the test fail.

### The physics boundary was already right

`PropSession` mirrors no `PhysicsBody` state. It asks `PhysicsWorld` to make a
body kinematic, to wake it, and to call back on rest, and reads nothing back;
`PhysicsWorld._set_kinematic_index` already documented the handoff. Nothing to
fix, so nothing was changed — the boundary is now written down in the
`PropSession` docstring instead of being implicit.

## 3. Obsolete code removed

* **`player/player/`** — a stale duplicate snapshot of `player/`, 12 files,
  1239 lines, predating the plugin host. Nothing imported it, and its 8 tests
  were running against the dead copy.
* **`Player._check_overlap`** (58 lines) — no callers anywhere; a third copy of
  the collision filter and AABB test.
* **`LogicKeyValueStore`** — the pre-2.4 alias, plus the `logic_keyvalue`
  sprite filename, the `add_logic_keyvalue_action` menu variable, and the
  comments and docs naming it. `editor/LOGIC.md` also still advertised
  `legacy_map_types`, which the compatibility purge had already removed.
* **103 unused imports** across 37 production modules, removed mechanically but
  conservatively — whole names only, never a `noqa`-marked probe or a
  multi-line block. Verified by pyflakes reporting no undefined name anywhere
  in the tree (it catches function-body uses, not just module scope) and by
  importing all 136 modules.
* **Two dead `zipfile.ZipFile` opens** in `main_window` that opened a package,
  ignored the handle, closed it, and then let `_safe_extract_zip` re-open it.

## 4. Defects found and fixed

**`plugins/integration._patch_property_editor` installed nothing.** The whole
body after `_orig_iterate = ...` had fallen out of the function to column 0, so
the wrappers were defined and dropped. `PropertyEditor._iterate_thing_properties`
was never replaced and `populate_for_thing` was never wrapped, in every build,
silently — meaning **plugin-declared property schemas fell back to type-guessed
widgets and `register_property_tab` did nothing at all**. Before: no
`_fio_plugins_patched` attribute. After: both methods carry
`_patch_property_editor.<locals>.` qualnames. `tests/plugins/test_editor_patches.py`
pins the installation of each wrap and guards the defect class with an AST check
for wrapper functions stranded at module level.

**`plugins/benchmark/benchmark.py` had five missing imports.** `configparser`,
`platform`, `datetime`/`timezone`, `QFileDialog` and the `_execution_environment`
helper were all referenced but never imported, so `_vsync_metadata` and the
whole HTML results/export path raised `NameError` on use — including from
inside the `except configparser.Error` clause meant to handle its own failure.
`_execution_environment` is imported lazily from `fio_benchmark` to keep this
module free until the benchmark is opened.

## 5. One solidity definition

Five byte-identical copies of "is this brush part of the solid world?" —
three in the monster AI's no-grid fallbacks, one in the logic thread's monster
wall query, one inside the player's predicate — are now
`engine.constants.is_solid_world_brush`.

Two nearby predicates deliberately do **not** use it, and the docstring says
why, so nobody folds them in later:

* `SpatialGrid.populate` asks `authored_hidden`, not `hidden`, because it
  builds a durable index that has to outlive a streaming layer parking a cell,
  and it files water separately rather than discarding it.
* `Player._blocks_player` adds `disabled` and `_physics_body` on top, because
  it classifies the movers and doors that never went through the grid.

Two others are different questions and were left alone: the angled-brush
eligibility test (which also excludes `operation == 'subtract'`) and the
hitscan ray (which excludes *all* triggers, including dynamic ones).

Trigger semantics are untouched: a use trigger is an authored sphere of
`use_radius`, the AABB is broad-phase only, and touch triggers keep their
authored brush semantics.

## 6. Benchmark boundary

The engine retains **no benchmark-specific runtime machinery**. The only traces
were two docstrings naming the benchmark plugin as their motivation; they now
describe what the code does. `SysMon.get_metrics` / `reset_metrics` stay — they
are an on-demand query over state the overlay already keeps for its own HUD,
they add nothing to the frame path, and removing them would push the plugin
into reading `_ft_buffer` privates, which is worse coupling, not better.

## 7. Left for post-2.5, deliberately

* **`plugins/benchmark/fio_benchmark.py` imports `tests.helpers`** at module
  scope (`worlds.box_brush`, `make_thing`, `gl`). A shipped plugin depending on
  the test suite is a development artefact in a release path, and it means the
  package has to carry `tests/`. Fixing it means giving the plugin its own
  world builders; that is a benchmark-plugin change, not a cleanup, and the
  brief was explicit about not expanding scope.
* **PyGLM vs the Android build.** `player/README.md` states PyGLM has no
  python-for-android recipe and `buildozer.spec` requires only
  `python3,pygame,numpy,pyopengl,pillow` — but `engine.physics` and
  `engine.player` both `import glm` and are both on the headless boundary list.
  The list's real contract is "no PyQt5, no PyOpenGL, no editor"; PyGLM is a
  separate and currently unsatisfied question for Android. Either the engine
  loses PyGLM or the Android build gains a recipe; both are larger than this
  pass.
* **The `Player` app's own simulation.** `player/app.py` still carries
  `TODO(port)` for stepping the world and pausing audio. `engine.player` being
  Qt-free now is a step toward it, and `PropSession` already runs identically
  in both tiers.
* **Profiling not yet done** (recorded, not started): MonsterAI thread,
  renderer submission, entity I/O dispatch, serialization, Big World state
  transitions.
* **Renderer teardown / resize / context reload** and the `.fiopak` device
  boundary — still unassessed, carried over from the audit.
