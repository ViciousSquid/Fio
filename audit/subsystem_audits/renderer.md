# Subsystem audit — renderer

Scope: `engine/renderer_core.py`, `renderer_F.py`, `render_cull.py`,
`shaders.py`, `terrain.py`, plus the `qt_game_view` handoff.

## Initialization, resources, lifetime

* `BaseRenderer.__init__` owns GL resources; `ShaderLoader` reads
  `assets/shaders` with in-source defaults; `UniformCache` memoises uniform
  locations with an explicit `preload`.
* Variant selection is unchanged from 2.4.2: `_detect_lowpower_platform()` /
  `engine.shaders.detect_low_power_arm()` choose `_compile_arm_shaders` or
  `_compile_standard_shaders`, and `'arm_mode'` is still honoured alongside
  `'lowpower_mode'` (asserted by `test_the_old_settings_key_is_still_honoured`).
* **GL context discipline:** commit 315 ("Keep OBJ GPU loading inside the 3D GL
  context") is a real correctness fix — uploading buffers outside the current
  context is undefined. Worth keeping in mind for any future loader.
* **Teardown / resize / reload:** unchanged from 2.4.2 and not exercised by the
  test suite in either version. The window-mode benchmarks (073–076) resize the
  real window mid-run, which exercises resize incidentally but asserts nothing.

## Light UBO (the big change)

One std140 uniform block, packed with NumPy into a structured array, uploaded
once per frame, bound by every lit shader including terrain and water.
Replaces 2.4.2's per-shader `_upload_lights_once`.

**The fragile part: shader sources are rewritten by regular expression at
runtime** to inject the block. Three commits were needed to get that regex
right (366, 397, 398) and four more to fix the generated GLSL — twice because
it was emitted with **literal `\n` escapes instead of newlines** (374, 375).

Nothing tests the rewrite at the string level. `test_light_budget.py` asserts
the CPU-side std140 layout (`('position','<f4',(4,))` etc.) and the light caps,
which is valuable but is not the rewrite. **Recommended:** run the rewrite over
each shader source in a test and assert the block appears exactly once with no
leftover `uniform Light` declarations.

That module was itself **unparseable from commit 396 to this audit** (the same
literal-escape defect, in the test file), and because a collection error aborts
the entire pytest run, the whole 35-commit UBO transformation shipped with no
test coverage at all. Repaired; 26 tests pass.

## Culling

* `visible_xz_bounds` derives the relevant XZ box from the live camera and the
  world height slab; the working radius is a live `ViewDistance` setting, not
  the `CAMERA_RENDER_CULL_DISTANCE` default.
* `cull_by_distance` — squared compares, persistent scratch buffer, strict
  one-row-per-object contract on the NumPy path, fail-open on the scalar path.
* **Scope preserved from 2.4.2, and verified:** shadow and portal passes do
  **not** distance-cull, and the `Light`/`Portal` exemption predicate is passed
  to the **Thing** cull only, never the brush cull. The test that guarded this
  had drifted into asserting exact source text
  (`"out=tbuf, keep=self._cull_keep_thing"`) and broke on reformatting; it now
  asserts the invariant structurally over the AST. Negative control confirms it
  still fails if the predicate is dropped.
* `_camera_distance_cull` grows its scratch buffers geometrically and slices
  them — no per-frame allocation.

## CPU vs GPU, and Python in the frame path

Checked for the things the brief lists:

* **Unnecessary Python loops:** the per-frame `isinstance` classification is
  gone (342–344). What remains in Python per frame is buffer bookkeeping and
  the final `[objects[int(i)] for i in order]` gather after `argsort`, which is
  unavoidable while the object lists are Python lists.
* **Repeated allocations:** addressed — reusable cull buffers, position
  buffers, and a preallocated `visible_thing_positions` on the render state.
* **Redundant conversions:** `load_model` no longer normalises paths per frame.
* **Redundant GL calls:** the light UBO is the main win; `_upload_env_uniforms`
  and the shadow-sampler bindings were repaired after the refactor broke them
  (378, 388).
* **Accidental synchronization:** none found. No `glGetError` or readback on
  the frame path; the textured-brush GL diagnostics added at 376 were moved off
  the hot path at 377.
* **Stale caches:** the model/normal matrix caches are keyed and invalidated;
  `update_instance_textures` hashes a per-Thing tuple that 2.5 correctly
  extended with `Prop`'s `render_mode` and `sprite_path`, so a Prop switching
  representation re-uploads.
* **Resource leaks:** not evidenced. Shadow resources are allocated once in
  `_init_shadow_resources`.

## Vectorisation judgement

The work is targeted at measured hot paths rather than applied blanket-fashion,
which is the right instinct. The one place it bought speed with coupling is in
physics, not the renderer (`PhysicsWorld` reading `SpatialGrid.cells`).

The scalar/vectorised divergence risk in `sort_by_distance` is real but
**checked**: the paths agree across the `min_numpy_count=16` boundary in both
directions including ties, and that is now pinned by tests. See performance.md.

## Editor / player differences

`qt_game_view` supplies `all_lights` from the render state with a documented
editor fallback (`Renderer_F` keeps a cached light collection keyed to the
Thing-list identity and size) when no render state exists. That fallback is the
only editor/runtime divergence in the light path, and it is deliberate.

**One defect:** the HUD brush count now mixes a main-thread
`len(self.editor.state.brushes)` with the snapshot's `visible_brushes` and
derives `culled` from the difference, leaving `render_state.culled_brushes`
unused. Two sources of truth for one number, and the derived one is wrong
whenever the editor list has changed since the snapshot. It feeds
`SysMon.get_metrics()` and therefore the benchmark's culling report.
