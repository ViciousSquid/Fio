# Play-mode tick — hot-path audit

Companion to `performance.md` (renderer/culling) and `performance_physics.md`
(`_batch_floor`, rotation batching). This one covers what the *running game*
does every frame: `LogicThread._tick_play_mode` and everything under it.

## Method

A driven play-mode tick, not a synthetic loop: a real `EditorState` populated
with brushes and Things, a real `Player`, `set_play_mode(True)`, and
`_tick_play_mode(dt)` called at 60 Hz with the player walking a circuit so the
spatial-grid query, collision resolution and trigger sampling all do real work.

Cost is reported **as a function of scene size**, because the thing being
hunted is work that scales with the whole world rather than with what is near
the player. A constant is a budget; a slope is a bug.

```
                       before            after
 61 things,  510 br    0.122 ms/tick     0.088 ms/tick    1.39x
241 things,  840 br    0.203 ms/tick     0.123 ms/tick    1.66x
961 things, 2160 br    0.607 ms/tick     0.289 ms/tick    2.10x

growth 61 -> 961         4.9x              3.3x
```

Three runs each, same machine, same session, interleaved with `git stash` so
both sides see the same conditions. The flattening of the slope is the result;
the absolute numbers are just what this machine does.

## Classifying a loop before calling it a hot path

A loop over 12 plugins at map load is not a loop over 4,000 bodies at 60 Hz.
Every finding below is classified by **N × frequency**, and that classification
is what decided whether to touch it:

| # | site | N | frequency | verdict |
|---|------|---|-----------|---------|
| TICK-01 | `PropSession.is_empty` | every Thing in the map | every frame, from the logic thread *and* the standalone player | large x frequent — fixed |
| TICK-02 | `LogicThread._update_portals` fade scan | every Thing in the map | every frame, **including maps with no portals at all** | large x frequent — fixed |
| TICK-03 | player solidity re-derivation | brushes near the player (10-100) | 5-6x per frame | medium x very frequent — fixed, but see below |

### TICK-01 — the Prop liveness poll walked the map every frame

```python
live = {id(t) for t in self.logic.things
        if getattr(t, "properties", {}).get("type") == "prop"}
```

Nothing notifies the session when a map edit removes a Thing mid-play, so this
is a poll, and it ran every tick to answer one boolean: are any Props left?
On the 961-Thing scene it was ~19% of the entire tick.

Fixed by gating the walk on a cheap fingerprint of the thing list — its
identity and length. Adding or removing a Thing changes the fingerprint and is
caught on the very next poll. The two cases a fingerprint cannot see on its own
(a same-poll remove-then-add, and a list replacement that happens to reuse the
freed list's id at the same length) are caught by an unconditional rescan every
`RESCAN_INTERVAL` polls, which bounds staleness to half a second at 60 Hz
regardless. No notification plumbing, no silent-failure mode: the worst case is
that a removed Prop stays interactable for 30 frames.

**Duplicated representation, flagged not created:** `PropSession.props` is a
third copy of the prop set, alongside `LogicThread._prop_things` and
`_prop_by_id`. It has a real reason — `PropSession` also runs in the standalone
`.fiopak` player, where no `LogicThread` cache exists — but that reason was
never written down and its synchronisation policy was "rescan the world every
frame". The reason is now in the docstring and the policy is the fingerprint
above. Collapsing the three copies into one is a larger change that would have
to cross the editor/player boundary; it is not attempted here.

### TICK-02 — the portal system charged every map for portals it doesn't have

```python
for t in self.things:
    if isinstance(t, Portal):
        t.tick_fade(delta)
```

An `isinstance` scan of the entire thing list, every frame, to tick fades — on
a map with no portals, this was pure loss. Worse, the function *already* had a
cached `_portals_by_name` behind a `_portals_cache_dirty` flag, so the file
contained two mechanisms for the same derived data: one cached and lazy, one
uncached and per-frame.

Fixed by deriving portals where every other per-tick entity list is already
derived — `_build_entity_caches`, alongside `_monster_things`, `_timer_things`,
`_pickup_things` and `_prop_things` — and deleting `_portals_cache_dirty`
entirely. **Net mechanism count went down by one.** A map with no portals now
returns immediately.

**Ownership rule for the new `_portal_things`** (and the family it joins):
`editor_state.things` is authoritative. The lists are derived, never written
back to, and rebuilt at exactly one place, `_build_entity_caches()`, called on
play-mode enter, by `LogicSpawner`, by savegame load, and — new — by the
console's portal create/delete. The name index is built in the same pass as the
list, so the two cannot disagree about which portals exist.

This also fixes a latent defect: a portal created from the console mid-play was
never entering `_portals_by_name`, so it silently did not work until play mode
was toggled. `ConsoleCommands._rebuild_logic_entity_caches` now says so, the
same way `LogicSpawner` already did.

### TICK-03 — five passes disagreeing about what is solid

This one was found while profiling but is **a correctness defect first and a
performance defect second**, and it would have been worth fixing at zero
speed-up.

The player runs five collision passes per frame (horizontal sweep, step-up
probe, vertical resolve, mesh-capsule depenetration, waterjump probe). Three of
them re-derived "is this brush solid?" per brush — including `is_water_brush`,
which lower-cases every face texture of every candidate. The other two derived
nothing and relied entirely on `SpatialGrid.populate` having already excluded
water, fog, non-dynamic triggers and physics bodies.

That reliance is only valid for brushes that came *from* the grid. It is not
valid for:

* **movers and doors**, which are appended to the collider list *after* the
  grid query, precisely because the grid cannot see them; or
* **the no-grid fallback path** in `Player.update`, which passes the raw brush
  list straight through.

So a hidden mover blocked horizontal movement but not vertical movement, and on
the fallback path a pool of water was a solid wall you could not walk into.

Fixed with one predicate, `engine.player._blocks_player`, applied **once per
frame** in the pass that already split colliders into mesh and AABB lists. The
passes now walk the list they are handed. The expensive water test is last in
the predicate deliberately: on the normal path the grid has already removed
water, so the only brushes that reach it are the handful of movers and doors.

Two further cleanups fell out of this:

* `_resolve_collision` was still building four throwaway `glm.vec3` per brush
  per axis pass, when `brush_aabb_bounds` exists for exactly that reason and
  `_has_headroom` — ten lines above, in the same class — had already been
  migrated to it. A half-migration; now finished. The cached bounds are
  computed through GLM and are bit-identical, so this is not a numerical
  change.
* `Player._check_overlap` (58 lines) had no callers anywhere in the tree —
  a third copy of the collision filter and AABB test, kept alive by nothing.
  Deleted.

## What was deliberately *not* done

* **No NumPy was added to any of this.** None of these findings is a numeric
  kernel; they are redundant scans and duplicated classification. Vectorising
  them would mean building arrays out of Python dicts every frame and reading
  the answers straight back out — the conversion-round-trip pattern, which is
  slower than the scan it replaces.
* **Player-vs-brush AABB resolution stays scalar.** N is the brushes sharing a
  grid cell with the player: ~10, occasionally ~100. That is below any
  plausible NumPy crossover (`_batch_floor`'s measured crossover is 56-64
  bodies, and that kernel does far more arithmetic per element than an AABB
  overlap test). A batched path here would be premature batching of a tiny
  workload, and would add exactly the Python-objects-to-array-and-back cost it
  was meant to remove.
* **`is_water_brush` was not given a memo cache.** Measured first: 0.665 us
  uncached vs 0.367 us with a signature cache on a solid brush, and *slower*
  on a water brush. Hoisting the call out of the per-frame path (TICK-03)
  removed ~96 calls per frame outright, which a 1.8x cache could not match.
  Cache rejected on the measurement.

## Coverage added

`engine/player.py` had **no test module at all** — which is how five passes
came to disagree about what is solid without anything noticing.

* `tests/physics/test_player_collision.py` (33 tests, new). The predicate
  itself; horizontal and vertical blocking, each parameterised over the grid
  and no-grid paths; a hidden mover on the mover path; step-up versus wall;
  waterjump against a reachable ledge, read as a *difference* from the same
  frame with no ledge, because swimming upward moves `velocity.y` on its own;
  and a work-counting test that solidity is decided once per frame.
* `tests/performance/test_engine_hot_paths.py`: portal fades tick off the
  cache, unnamed portals included; a map with no portals does no per-frame
  portal work.
* `tests/test_prop_runtime.py`: the liveness poll does not walk the map every
  frame; it still notices a removal immediately; a same-length swap is caught
  by the periodic rescan.

Every one of these was mutation-verified — the fix reverted, the test observed
to fail, the fix restored. 17 of the 33 player-collision tests fail when
`_blocks_player` is neutered; both portal tests fail when the fade scan is put
back; the two liveness tests fail under the two opposite mutations (never
rescan / always rescan).

Suite: 2617 -> 2657 passing, 49 skipped.
