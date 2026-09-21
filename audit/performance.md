# Performance audit

Method: trace the actual hot path first, then judge. Claims below were checked
against the code at HEAD, not inferred from commit messages.

## Where 2.5 genuinely wins

1. **Model classification removed from the frame path** (342–344, 349).
   At 2.4.2 `_sort_objects` walked every Thing each frame doing `isinstance`
   tests to split sprites from models. Now classification and model/normal
   matrices are cached and invalidated, not recomputed.
2. **`load_model` reduced to one dict lookup on the hot path** (342).
   Path normalisation and filesystem work moved to cache misses only, and the
   authored spelling is aliased to the resolved model so later frames stay on
   the single-lookup path. Textbook.
3. **Batched cull and depth sort** (345, 350–353) over **reusable** position
   buffers (346–348, 352, 363). No per-frame list allocation; squared distances
   only; `argsort(kind="stable")`.
4. **Batched shadow-caster distance tests** (351).
5. **GPU-instanced model rendering** (364) and a **single shared light UBO**
   (365–370, 379) instead of per-shader light uploads.
6. **Lights collected once by the logic thread** (380–384) instead of the
   renderer rescanning all Things per frame.
7. **Trigger occupancy batched** (483) and taken off the 60 Hz path (481).
   The intent is right: the baseline's per-tick scan over all trigger brushes
   is real work. The cost was paid in responsiveness (REG-02), not in CPU.
8. **Physics integration masked to awake bodies** (440) and static collision
   AABBs cached per grid cell (441/442).

## Things that are not as fast as they look

* **`yield_hook` in `editor/procedural_generator.py` is dead** (REG-08). The
  benchmark passes a cooperative-yield callback into `create_map_data` and it
  is never invoked during brush generation, so the Qt loop is starved for the
  whole grid-geometry pass of a large generated map.
* **`SysMon`'s culled-brush count is derived, not measured**
  (`qt_game_view`, commit 302). `culled = max(0, editor_total - snapshot_visible)`
  mixes a live main-thread count with a snapshot-derived one, and
  `render_state.culled_brushes` — the renderer's real figure — is now unused.
  The benchmark's HTML report advertises culling metrics that come from this.
* **OBJ import parses the file twice** (317): once to measure the mesh extent
  for auto-scaling, once to load it. One-off on an explicit user action, so
  acceptable, but it is a duplicate parse of a potentially large file on the
  UI thread.
* **`_trigger_use_prompt` is recomputed by scanning `self._trigger_brushes`**
  on every poll (`next((... for bid, _ in self._trigger_brushes ...), "")`),
  which is O(all triggers) rather than O(polled triggers), inside a function
  whose entire purpose is to avoid scanning all triggers. Small in absolute
  terms — it runs at 4 Hz, not 60 Hz — but it undercuts the design.

## Divergence risk between the scalar and vectorised paths

`render_cull.sort_by_distance` keeps two implementations either side of
`min_numpy_count=16`, and `cull_by_distance` keeps a scalar path for callers
that pass no `positions`. A scene crossing the sort threshold must not reorder,
or transparency pops as objects enter and leave view.

**Nothing tested this**, and commit 094 deliberately keeps the scalar fallback
out of the renderer benchmarks, so it is never measured either.

**Checked empirically:** the two paths agree for counts 2–40 in both
directions, including tie-stability. Commit 353's fix
(`argsort(-d, kind="stable")` rather than `argsort(d, kind="stable")[::-1]`)
is correct — reversing a stable sort reverses its ties too, which would have
drawn equal-distance transparent surfaces in reverse authored order.
**Now pinned** by `test_scalar_and_batched_depth_sort_agree` (parameterised
across the boundary) and `test_depth_sort_is_stable_across_equal_distances`.

The one deliberate asymmetry: an object with no readable position. The scalar
path fail-opens (keeps it, sorts it to one end); the batched path requires a
row per object by documented contract and raises on a count mismatch. The
engine only uses the batched path with authoritative render-state snapshots, so
this is sound — but it is a semantic difference across a threshold, and it is
worth keeping the contract comment that explains why.

## Cost added by this audit's fixes

* `PhysicsWorld.step` now reads `disabled` for bodies that are **awake**
  (REG-04). Bounded exactly as the pre-vectorisation simulation bounded it —
  that loop only consulted `disabled` for bodies it was moving. A world with
  nothing in motion pays nothing; a world with N moving props pays N dict
  lookups per step, against the N-body NumPy integration already running.
* `PhysicsBody._index` calls `world._pack()`, which early-outs on
  `if not self._dirty: return`. The handle accessors are not on the batched
  path; the steady-state cost is one boolean test.
* `_handle_triggers` gains one truthiness test per tick.
* Everything else is test-only.

## Recommended next, in order of value

1. Make `_trigger_use_prompt` per-tick over the use-activated subset only
   (fixes REG-02 and removes the O(all triggers) rescan in one change).
2. Call `yield_hook` in `generate_brushes_from_grid` (REG-08).
3. Restore `render_state.culled_brushes` as the HUD/benchmark source of truth.
4. Add a string-level test for the runtime shader-source regex rewrite.
