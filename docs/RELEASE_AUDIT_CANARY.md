# Fio Canary release audit: hardening, performance, CI

Base: `0109cf6` (Canary, after the HD_High performance pass). Audited before
Canary becomes the next stable release.

Six areas were read and profiled, each with repro scripts or measurements
before anything was changed:

* the logic thread, its runtimes and threading;
* the dense tables, the spatial grid, physics and player movement;
* the frame path: `QtGameView` and the Classic renderer;
* map load and save, packages, models and textures;
* the editor: state ownership, undo, the features added since 3.5.1;
* the test suite and the CI workflows.

`engine/renderer/hd_high` was left to its own audit (`docs/hd_high_audit.md`).
Classic's digest-locked files (`engine/renderer/forward`, `core`,
`engine/shaders.py`) were not changed.

Timings are x86 medians on a 4-core machine under Mesa llvmpipe; on the
low-power ARM target expect each several times larger.

---

## 1. Defects fixed

### Critical and high

| # | Area | Defect | Fix |
|---|---|---|---|
| C1 | Player | Touching a Mesh-collision model while standing on a box floor threw the player to the edge of the floor brush: `(107.8, 50, 169.6) -> (1025, 50, 166)` in one tick, then a fall into the void. 43 of 48 approaches to the bundled catwalk staircase did it. The box re-check after a mesh slide ran the axis passes with no movement, and the X pass takes any overlap for an X overlap. | The re-check leaves each box along the axis it is entered least on. 0 of 48 approaches teleport. |
| H1 | 3D view | A frame that raised left its `QPainter` active in native-painting mode. It lived on in the interpreter's kept traceback and was destroyed when that was next replaced, under whatever GL context was current: a segfault long after the failed frame. | The frame is drawn inside a `try`; a failure ends native painting and the painter. |
| H2 | Map load | A light volume with `"colour": null`, or a light whose radius is a 400-digit integer, raised inside a 2D paint with its painter open on a local pixmap: the editor segfaulted on opening the map. | Both 2D paint paths end their painter whatever happens; a null colour is white. |
| H3 | Map load | A Prop `collision_size` of 1e308 filled the spatial grid until memory ran out. A `rotation`, `scale` or `model_path` of the wrong type raised half-way into Play start, leaving `play_mode` on. `1e999` (strict JSON for infinity) in any property loaded, and Save and autosave then refused the map for the rest of the session. | `validate_level` refuses a property that is NaN, infinite or an integer no float holds, and holds a Prop's fields to their shapes. |
| H4 | Map load | One brush could cover 3.8e8 grid cells: a floor 1e6 units wide took 50 s and 3.3 GB to file at Play start. Movers had a cap; static brushes had none. | A box brush covering more than 2^18 cells (260,000 units square) is refused; the grid applies the limit to what validation cannot size. |
| H5 | Player (Android) | `PlayerWorld` never called `validate_level`: a package was built into a world unchecked. | It validates first and refuses with a `PackageError`. |
| H6 | Editor | A property edit of a single selected object took no undo checkpoint: Ctrl+Z did nothing, or took the edit back together with the step before. | Every panel edit checkpoints, once per burst of edits to a property. |
| H7 | Editor | Console commands that change the scene never marked the map modified: no prompt on close, and autosave skipped the map. | They checkpoint through the window. |
| H8 | Renderer contract | The registry checked only that members existed and were callable. | See section 3. |

### Medium and low

| # | Area | Defect | Fix |
|---|---|---|---|
| M1 | Rotate | Models stood still through a cutscene, the death screen and the level-complete screen while movers and light paths went on. | Rotations are stepped at the head of the tick with the movers. |
| M2 | Save games | Rotate state was not saved: a Turn-mode valve opened after the save stayed open, with its solid collision, once the save was loaded. | `RotateMotion.snapshot` / `restore`, beside the light paths'. |
| M3 | Props | A model carried and turned with the player kept that rotation after Stop when the world is not restored on leaving Play. | The authored rotation is restored with the place. |
| M4 | Light paths | A path looping through nodes where the light already stands arrived 64 times a tick: 3,840 `OnPathNodeReached` a second. | One such arrival a tick. |
| M5 | Editor | Undo, redo or Play during a gizmo drag or a pending clone left the gesture on objects no longer in the scene; its cancel dropped a checkpoint that was not its own. | Ctrl+Z / Ctrl+Y mid-gesture cancels the gesture; Play and a level load end it. |
| M6 | Editor | The mover/door Preview outlived its selection, an undo, a map load and Play start (where the mover ran at the preview's transform). | It ends in each case. |
| M7 | Editor | Rotate turned locked objects, which Move and Scale skip. | Locked objects are left out. |
| M8 | Editor | Shift+click selecting in the 3D view took an undo checkpoint: redo history dropped, map marked modified, the world copied (29 ms a click on BigWorld). | Selecting takes none. |
| L1 | Editor | A copy or clone taken during a Rotate preview carried the preview's angle. | The preview ends first. |
| L2 | Editor | Clicking a gizmo handle, or cancelling a drag, marked the map modified. | Only a drag that changed something does. |
| L3 | 3D view | `switch_renderer("Classic")` answered "Unknown renderer". | The alias is resolved. |
| L4 | 3D view | After Qt replaced the widget's GL context, the new renderer was never given the grid. | `initializeGL` marks it dirty. |
| L5 | Frame input | The host published two keys outside `FRAME_INPUT` (`culling_enabled`, `show_caulk`) that no renderer read. | Removed; a test holds the host to `FRAME_INPUT`. |

## 2. Performance

| Change | Before → after |
|---|---|
| Player against a Mesh-collision model: one NumPy bounds test selects the triangles near the capsule for each pass (`triangles_near`), which then runs its own tests unchanged | Tick while touching the 1,924-triangle mainframe: **6.3 → 0.43 ms** median (max 13.6 → 3.4). The player's position is identical to the bit on all 360 ticks. |
| Model collision build: a model file is read once, world triangles and normals are built with NumPy | 40 chairs (5,960 triangles each), `build_model_collision_brushes`: **1795 → ~700 ms** |
| `sync_bodies` (a barrel exploding, a plugin's spawn): a model that has not moved keeps its triangles | 40 chairs in Play: **1737 → 43 ms** |
| `EntityTable._apply_changes` rebuilt a set for every moved row | 2,000 entities moved in a frame, `prepare_render_state`: **54.6 → 1.6 ms** |
| A monster's sprite candidates are kept, not re-derived on every heading change | 200 chasing monsters, `prepare_render_state`: **11.4 → 2.9 ms** |

### Redundant sends in a frame

Both renderers drew a model one material group at a time, and every group
sent its emissive factor, opacity and colour whether or not the group before
had the same; each clear group also asked GL for the blend state it would
put back. A pass now keeps what it has sent each program (`_ModelSends`) and
asks for the blend state once. Classic also uploaded its light block and
sent the shadow filter to every lit program on every frame, changed or not.

| `Reel_To_Reel_Test`, Play, Classic | Before | After |
|---|---|---|
| GL calls a frame | 719 | 389 |
| of which set a value already set | 365 | 43 |
| GL state queries a frame | 16 | 9 |
| Light block uploaded on a still frame | 8,192 B | 0 |
| `paintGL`, median (llvmpipe) | 14.1 ms | 6.8 ms |

`_SHOWCASE`: 624 → 578 calls a frame.

Verified three ways: Classic's frames are identical to the stored golden
set and to the code before the change; for both renderers, the uniform
values and blend state every model draw sees (1,080 draws on
`Reel_To_Reel_Test`, also `_SHOWCASE` and `Office_Corridor`) are identical
before and after; and Classic's sources were re-locked. HD_High's own frames
differ from run to run on unchanged code, so its pictures could not be
compared.

What remains are queries a pass makes once to put back state it does not own
(the decal, water and shadow passes; the frame's viewport and framebuffer).
Removing those needs the renderer to track GL state across passes.

The mesh broad phase is a registered small-N crossover
(`MESH_CULL_VECTOR_FROM = 32`, measured table where it is defined): below it
each pass keeps its loop.

Measured and sound, so left alone: the per-tick path on the shipped maps
(`_tick_play_mode` 57–189 µs, `prepare_render_state` 105–277 µs); Soma (8 µs
asleep, 1.1 ms for 200 falling bodies, no tunnelling, no NaN contamination);
map open (138–266 ms) and save (8–34 ms); the Classic frame's Python outside
GL calls (1.5–5.4 ms). No GL object, heap or thread growth over repeated map
opens, Play cycles or renderer swaps.

## 3. The renderer contract

Both built-in renderers and any plugin renderer are held to
`engine/renderer/api.py` by one check, `contract_violations`, which the
registry runs on what a factory returns:

* every member is there, and reading it does not raise;
* each method takes the declared arguments: names, order and defaults;
* each attribute holds its declared type (`shadows_enabled` and `ready` a
  `bool`, `render_stats` a `RenderStats`, `view_distance` a `ViewDistance` or
  `None`) and `water_quality` one of `WATER_QUALITIES`;
* `contract_version` is required and must be a version; an undeclared one is
  no longer taken as compatible;
* the optional members the host looks up by name (`animate_textures`,
  `draw_face_hover`, `draw_lens_water`) are declared as `RendererOptions` and
  held to their declaration when present;
* so are the four settings the editor reads from a renderer (`hd`,
  `lowpower_mode`, `reflections_enabled`, `decal_reflection_fade`). A fifth
  the editor wrote, `clear_color`, no renderer had: `r_clearcolor` reported
  success and changed nothing. It now sets the fog colour, which is what a
  renderer clears to.

A test holds the viewport, the editor and the plugins to `Renderer` and
`RendererOptions`: nothing else is read from a renderer.

A refused renderer is cleaned up, and a cleanup that raises is reported with
the refusal. The same conformance tests run against Forward and HD_High,
including a count of live GL objects after `cleanup()` and after each
refusal of a malformed variant. Neither renderer needed changing.

## 4. Tests and CI

| Finding | Fix |
|---|---|
| 72 tests in six modules ran in no CI job: they import PyQt5/OpenGL inside a test, so the headless tier leaves the module out, and carried no `qt` mark, so the Qt tier deselected them. | Marked `qt`; the suite-integrity check applies the headless tier's own rule. |
| 35 tests marked `slow` but not `qt` ran in no job. | The core tier includes `slow`. |
| The GL job was green when no context could be created (every test skipped). | `FIO_REQUIRE_GL=1` makes that a failure in that job. |
| 30 name-resolution tests skipped everywhere: no job installed pyflakes. | Installed in the Qt jobs. |
| `opengl`, `benchmark` and `launch` had no timeout (a hang held a runner for six hours). | Timeouts; Qt 30 → 45 minutes. |
| A test defined twice; an assertion ending `or True`. | Removed; made real. |
| The Windows workflow could not fail: every job was `continue-on-error`, so it concluded "success" with failed jobs inside (5 of 16 on one commit of this branch), and releases were gated on the Linux run alone. | Every Windows job can fail the workflow; `build.yml` and `full-build-all-platforms.yml` require it. |

New regression tests accompany every defect above. The teleport, the paint
crash, the Rotate and the editor tests were each run against the code
without its fix and fail there (the paint test segfaults).

## 5. Decisions for the maintainer (not changed)

* **macOS has no test tier.** Only two path-filtered UI jobs run there.
* **Push and pull_request both trigger the suites**, so a same-repo PR runs
  everything twice, including 13 Windows Qt shards.
* **Material companions are not exported:** a package carries a map's
  textures but not their `_n` / `_m` / `.mat.json` files, so HD_High
  regenerates defaults on another install.
* **A zero-thickness box** still collides (a brush of size 0 on an axis is an
  invisible obstacle). Skipping it would also drop any flat brush an author
  stands on.

## 6. Follow-ups worth doing (measured, deferred)

* **Mesh collision is still a Python list of triangles.** After this pass a
  solid model costs ~11 ms to build per 6,000 triangles, nearly all of it
  list construction, and a turning mesh is converted to lists every tick
  (2 ms a tick standing near one). The passes now select by index from
  arrays, so the lists could go.
* **A rotating model re-resolves its whole entity row every frame** (22 µs a
  model): the angle is journalled as a state change. A narrow journal flag
  would rewrite only the transform columns.
* **`material_maps.generate` peaks at 31× the image** (2 GB for 4096²).
* **Path-following movers** keep their state in a dict beside the mover
  table (22 µs a mover a tick).
* **Walk-over collection** loops every Prop in the player's cells, scenery
  included (0.7 µs a Prop a tick).
* **Gizmo Rotate** solves each brush's planes twice per mouse move (1 ms a
  brush).

## 7. Verification

All CI tiers were run on the final tree with CI's own commands and pinned
dependencies (CPython 3.13, llvmpipe under Xvfb, `FIO_REQUIRE_GL=1`,
`FIO_REQUIRE_GLSLANG=1`).

| Tier | Base `0109cf6` | Final tree |
|---|---|---|
| Core (`not qt and not gl and not benchmark`; the base run also excluded `slow`) | 5,546 passed, 17 skipped | 6,309 passed, 19 skipped |
| Qt (`qt and not gl and not benchmark`) | 4,513 passed, 6 skipped | 4,635 passed, 6 skipped |
| GL (`gl`) | 555 passed, 11 skipped | 558 passed, 11 skipped |
| Shader portability (glslang) | 134 passed | 134 passed |
| Android player (`player/tests`, staged runtime) | 211 passed | 217 passed |

* The final full run had two failures, both from this pass and both fixed
  and re-run before this was written: the new hardening module mixed
  headless and Qt tests (split in two), and
  `test_out_of_range_blast_settings_are_clamped` loaded an infinity from a
  map, which is now refused on load (the test sets it while the session
  runs, as a console `setprop` does).
* All 29 shipped and plugin maps pass the stricter `validate_level`, and
  load, save and reload identically.
* Not run here: the Windows and macOS workflows, the Android APK build, and
  anything on a real GPU. Python 3.14, which the releases are built with, is
  covered by CI only.
