# Fio 3.5.1 release audit: performance, optimisation, hardening

Base: tag `3.5.1.0` (`884a6d0`). Branch: `claude/nice-hawking-l71x19`.

The audit looked at four areas, each profiled or read in full:

* **Untrusted input:** maps, `.fiopak` packages, savegames, models and images.
* **Runtime faults:** plugins, threads, OS interaction and exception handling.
* **Play-mode frame path.**
* **Editor:** start-up, map open and save, Play transitions and interaction.

Timings are x86 medians taken with `perf_counter`; on the low-power ARM target, expect each to be several times larger.

The baseline test run, all three CI tiers on `3.5.1.0`, was fully green before any change.

---

## 1. Hardening: untrusted data (maps, packages, models)

| # | Severity | Defect | Fix |
|---|---|---|---|
| H1 | High | `terrain_data` chunk bounds unchecked. A map with `min/max_chunk_x/z = ±2000` built the whole grid on the first frame and was OOM-killed; `±100000` raised `MemoryError` every frame. The same happens on Android. | Bounds must be whole numbers covering at most 128×128 chunks, otherwise the defaults are used. `TerrainTable.ensure_bounds` also refuses more than 256×256. |
| H2 | High | Zip bomb in the terrain paint decoder: a 1.3 MB payload reached 2.1 GB RSS. Reachable from map JSON, `.paint` files and packages. | Bounded `decompressobj` that never inflates past the expected chunk size. |
| H3 | High | `chunk_size` / `sculpt_grid_resolution` of 0 or NaN, string offsets, or list fields holding numbers raised on every frame or every height sample, leaving a half-loaded map. | Each number that sizes an allocation or divides must be finite and in range, otherwise the default is used. List fields of the wrong type are ignored. |
| H4 | High | Stamp images were decoded by QImage with no size limit: a 140 KB PNG decoded to 1.16 GB. | The header size is checked (`check_texture_size`) before Qt decodes. |
| H5 | Medium | UNC/device paths in stamp textures and the sky image were probed; on Windows that opens an SMB connection and leaks NTLM credentials. | Never read. |
| H6 | Medium | GLB: primitives that all name one accessor each decoded a fresh copy, so a 1 MiB file reached 1.45 GB. | All accessor reads share one decode budget (4× file size, capped at 1 GiB). |
| H7 | Medium | OBJ: one face line with millions of indices amplified about 250×. | At most 16M triangles after fan triangulation. |
| H8 | Medium | Angled-brush planes unvalidated: a NaN normal loaded and then broke Play start halfway; `d=1e308` raised `MemoryError` in the grid. | Planes need a finite, non-zero `n` and a finite `d`. Every `pos`/`size` must lie within 1e7 units. |
| H9 | Low | A decal image named by a map could be `../../x` or an absolute path. | Confined to the decal folder. |
| H10 | Low | `.fiopak` member reads raised on encrypted, unsupported or corrupt entries (`RuntimeError`, `NotImplementedError`, `zlib.error`), and a non-list `plugins` field in the manifest also raised. A paint file whose `stamps` was not a list failed the whole map. | Each such case is skipped as unreadable. |

Checked and found sound, with no change needed:

* `.fiopak` reading:
  * per-entry and total size caps, with alias-proof caching
  * no extraction
  * bundled `plugins/` refused
  * `PackageSession` limits
* `asset_paths` confinement, used by model companions, glTF URIs, light cookies and decal layers.
* Savegame magic, version and schema validation.
* Atomic writes for maps, autosave, paint files, settings, savegames and package export.
* No pickle, `eval` or data-driven imports.

## 2. Hardening: fault containment

| # | Severity | Defect | Fix |
|---|---|---|---|
| F1 | Med-High | A paint error that recurs every frame sent a full traceback 60 times a second to stderr, the Debug Console and `fio_crash.log`. The crash log then truncated itself at 512 KB, deleting the root cause. Play kept running behind a frozen picture. | The excepthook and the crash log write a repeated traceback once, with a repeat count. The crash log rotates to `fio_crash.log.1` instead of truncating. After 30 consecutive failed paints in Play, Play stops. |
| F2 | Medium | Plugin `host.on(...)` handlers (tick, render, physics events) had no throttle and were never auto-disabled. One failing `tick` handler emitted 600 times produced about 231 KB of log. | These handlers share `on_tick`'s policy: thinned logging, and the plugin is auto-disabled after 10 failures in a row. |
| F3 | Medium | One bad monster froze the whole monster AI and logged a traceback at 30 Hz. | The error is logged once per distinct message. After 30 consecutive failures, Play ends through the logic thread's fault path. |
| F4 | Medium | A plugin whose `register()` raised left its menu entries, tools and console commands live, and the Plugins menu could not switch them off. | The plugin is disabled and its partial registrations are removed. |
| F5 | Low-Med | Map logic (`logic_command`) could run `condump` without limit, creating a new file each time, and could grow the console queue without bound. | `condump` is user-only, and the queue is capped at 256. |
| F6 | Low | After a fatal tick, the logic thread kept ticking the half-advanced world until the GUI teardown ran. | A faulted world is not ticked again. |
| F7 | Low | Exceptions that ended background threads never reached the crash log, so windowed builds lost them. | `threading.excepthook` records them. |
| F8 | Low | `autosave_interval` was not range-checked: `0` autosaved on every idle loop, and a huge value overflowed QTimer inside `MainWindow.__init__`, so the editor would not start. | Clamped to 1–60 minutes. |
| F9 | Low | Apply & Restart used `os.execl`, which splits paths containing spaces on Windows, and skipped the normal shutdown. | Closes through the normal shutdown, then relaunches with an argument list. The unused `MainWindow._restart_application` was removed. |
| F10 | Low | Save slots ignored the read-only-install fallback (a translocated macOS app). | Use `writable_dir`. |
| F11 | Low | `PluginHost.wrap` could run the original twice when the wrapper raised after calling through. | Never twice. The original is called directly while the plugin is disabled. |
| F12 | Low | The terrain builder silently retried a failing grid every frame. | Logged once per distinct error. |
| F13 | Low | Project Overview put map-supplied text into rich text unescaped. | Escaped. |
| F14 | Hygiene | The shipped `settings.ini` contained a test run's recent-files paths. | Removed. |

## 3. Performance

Play-mode frame path: already well optimised. Logic `_step_frame` takes 0.45–0.57 ms, `MonsterAI.update` at most 0.3 ms, and the Python side of paintGL about 1–3 ms. Most of the remaining paint time is GPU or driver work.

| Change | Before → after |
|---|---|
| Property page: the page on screen is no longer rebuilt after every undoable edit (its signature folded in the global I/O revision, which every `save_state` bumps) | `update_all_ui` after an edit: **93 → 1.8 ms** (_SHOWCASE), **126 → 24 ms** (BigWorld) |
| `EditorStyle.pixelMetric`/`styleHint` tables built once, not rebuilt on each call (~7,000 calls per page) | 2.2 → 0.05 µs per call |
| UUID made only when missing (`setdefault` evaluated `uuid4()` on every serialise) | about 60% of `_serialize_brushes` saved |
| Map open: two redundant scene-hierarchy rebuilds removed | about 40–65 ms per BigWorld open |
| Big World editor streaming: JSON-shaped copier instead of `copy.deepcopy` | about 4× faster copies (each snapshot, save and Play transition) |
| Audio device probed once at start-up | about 100 ms saved on machines with no audio device |
| Decal library decoded on worker threads for the first frame that shows decals | **521 → ~190 ms** |
| Lit passes: fog uniforms re-send only the camera position; the light UBO key is built once per frame | about 120 fewer GL calls per frame on _SHOWCASE; about 0.2 ms per frame of Python |

## 4. Decisions for the maintainer (not changed)

* **Stamp textures outside the project.** A shared map can name any local image as a terrain stamp texture. Its pixels are baked into the terrain and written to the next save or package. The Terrain Editor stores absolute paths on purpose for textures picked from elsewhere, and 18 tests cover that workflow, so only UNC/device paths are refused. Confining stamps to the project (`asset_paths.project_asset_path`) would close this, at the cost of that workflow.
* **Map-issued `save`/`quicksave`.** Map logic can still overwrite the user's save slots. This might be wanted for checkpoint saves; a per-map prefix would avoid clobbering other games' slots.
* **Sky images** keep accepting local absolute paths (documented behaviour); only UNC/device paths are refused.

## 5. Follow-ups worth doing (measured, deferred as too risky for a release)

* **Selecting a brush** still costs 55–90 ms:
  * Build the trigger, mover and door tabs lazily, as the I/O tab already is (about 18 ms + polish).
  * Park pages without re-parenting (about 22 ms).
* **Scene hierarchy** rebuilds its whole tree on each selection: about 21 ms on 3.6k brushes. A selection-only fast path needs a full signature of what the rebuild reads.
* **Loading overlay** `parent.grab()` costs 80–130 ms per open or Play. A cheaper blur source would help, but `QScreen.grabWindow` triggers macOS's Screen Recording permission prompt.
* **HUD QPainter overlay** costs about 0.6 ms/frame. Cache paths, fonts and pens; use `QStaticText` for values that rarely change.
* **`LogicInteraction.handle`** costs about 50 µs/tick in NumPy over 1–3 level changers. A small-N loop needs a registered crossover per `tests/performance/test_small_n_invariant.py`.
* **Shadow cubemaps and the cookie placeholder** are re-bound on every lit pass (about 60 GL calls per frame). Track bindings per unit.
* **Soak coverage:** no test opens many maps one after another. Renderer textures and models are freed only in renderer cleanup.

## 6. Verification

* **Regression tests:**
  * `tests/regression/test_release_hardening_3_5_1.py` covers H1–H10, the crash-log part of F1, F2, F4, F5 and F11.
  * `tests/editor/test_property_editor_rebuilds.py` gains three property-page tests.
  * `tests/engine/test_audio_backoff.py` now expects the single start-up probe.
* **Suite runs:** all three CI tiers (core / Qt offscreen / GL on llvmpipe under Xvfb), run per directory and excluding benchmarks.

  | Tree | Passed | Failed | Skipped |
  |---|---|---|---|
  | `3.5.1.0` | 9,408 | 0 | 60 |
  | This branch | 9,441 | 0 | 60 |

  The branch figure is after the audio-backoff expectation update. The one failure in the final run was that test, which pinned the old double probe.
* **Flaky test:** `tests/e2e/test_face_pick_oracle.py::...[none]` failed once in a combined renderer+visual+e2e GL run. It passed alone, on a rerun and in the full run. It looks timing-sensitive under llvmpipe.
* **Test environment:** CI's Qt/XCB/EGL packages had to be installed in this container first. Without `libegl1`, every Qt-tier test errors out.

---

## 7. Stable-release pass (second audit)

Branch `claude/nice-hawking-l71x19` + `3.5.1.0` (the Retro furniture pack and the `_SHOWCASE` update merged in), audited again before it becomes the main stable release. Covered: the code changed since §1–6 (groups, multi-selection editing, model wireframes, link layers, browse buttons, renderer unit binding), the deferred items in §5, and a session-length stability check. Benchmarks and correctness checks were taken through the Debug Tables instrument (`tools/debug_tables.py`).

### Defects fixed

| # | Severity | Defect | Fix |
|---|---|---|---|
| S1 | High | Typing one axis of a vector (Rotation, Scale) with several objects selected copied the primary's **whole** vector onto every selected object: three chairs at yaw 0/90/180 all became `[10, 0, 0]`. No undo checkpoint was taken, so Ctrl+Z could not bring them back. | The other objects get only the edited component. An edit that reaches more than one object takes one undo checkpoint first. |
| S2 | High (CI) | The autouse settings.ini redirect (8c125a5) requested `monkeypatch`, which made each test's own `monkeypatch` outlive `fio_session`. A test that patched its window kept the closed window reachable, and the session's leak check failed. 30 errors (pause menu, groups in Play, save/restore in Play, the 3.5 regressions, mouse grab, freelook): the branch tip was red. | The redirect uses a private `MonkeyPatch`. |
| S3 | Medium | A NaN or infinite model scale, rotation or position (a typed `inf`, a malformed map value), or a NaN corner in an OBJ, raised inside the 2D views' `paintEvent` on every repaint. | Such a model is drawn as its box. |
| S4 | Medium | The 2D wireframe image cache budgeted pixels only. Thousands of small images (props at scattered yaws, zoom steps) fitted the 16M-pixel budget while holding gigabytes. | Each entry is charged a fixed cost too, with at most 4096 entries. |
| S5 | Medium (perf) | The wireframe edge cache cleared completely at 256 models, so larger levels rebuilt nearly every model's edges on every repaint (~12 ms per 6k-triangle model). | The least recently used entry is evicted. |
| S6 | Low | Animated I/O links that were no longer drawn (deleted, filtered, scrolled away) kept the 60 Hz arrow timer running for good. | Undrawn links stop animating, so the timer sleeps. |
| S7 | Low | Double-clicking a Scene Hierarchy section title closed it and immediately reopened it. | The double-click is swallowed. |
| S8 | Low (test) | `test_shipped_map_references` reported the Retro chairs as missing. They ship in `assets/bundled_furniture.fiopak`. | The test asks `engine.asset_archives`, the reader the engine uses. |
| S9 | Hardening | Native crashes (segfaults in a GL driver or in Qt) left no trace in windowed builds. | `crash_log.install` points `faulthandler` at `fio_fault.log`, unless faulthandler is already on. |

### Performance

| Change | Before → after |
|---|---|
| Scene Hierarchy, selection change: rows are reselected; no rebuild unless something it displays changed (compared by value) | **57 → 11 ms** a click on BigWorld (3.6k brushes). A refresh with nothing to change: 1.4 ms. |
| 2D brush culling with float comparisons, not a `QRectF` per brush | 0.58 → 0.27 µs a brush |

Debug Tables measurements (llvmpipe, medians). Baseline and branch are identical in frame cost, publication cost, tick and table contents on all eight maps checked (`_SHOWCASE`, `Office_Corridor`, `MonsterTest`, `Portal_Test`, `Terrain_Test_small2`, `BigWorld_streaming_test`, `Wet_Decal_Test`, `Glass_Test`). The changes in this pass are on the editor side.

### Correctness checks run through the instrument

On each of the eight maps, with the change-record oracle on (`FIO_RENDER_ORACLE=record`):

* RenderTable and EntityTable rows match the authored brushes and entities. Every brush id has a row, and every selected brush is followed to its row.
* Still editor frames examine 0 rows at every change-detection site (M4).
* The S1 edit reaches each model's published `model_base_matrix`, the matrices stay distinct, and one undo restores them. The baseline fails all three checks on `_SHOWCASE` and `Office_Corridor`.
* Row counts are unchanged across Play → Stop.
* No oracle misses.

The first two checks are now permanent: `tests/tools/test_debug_tables_release_checks.py`.

### Session-length stability

* Opening `_SHOWCASE` and `MonsterTest` alternately, each followed by Play, 5 frames and Stop, 10 cycles: resident memory **795 → 802 MB**, flat after cycle 3. Renderer textures and models are cached per asset, bounded by the asset library.

### Observations, not changed

* **Undo snapshots on Big World maps:** ~20–90 ms per checkpoint (whole-world copy). Structural sharing would fix this, but it is too large a change for a release.
* **Play-mode still frames:** examine `pass_memo` rows (132 per frame on `_SHOWCASE`). The baseline does the same, so this is not a regression. It is worth a look against the M4 invariant.
* **Entity selection:** builds the property page fresh (~45 ms for a Monster on BigWorld). Pages for brushes are already cached.
* **`test_p_opens_the_position_window_from_anywhere`:** fails under Xvfb without a window manager (no window activation). It passes on the offscreen platform CI uses.
* **One segfault:** seen once in a combined GL run under llvmpipe and not reproduced in three reruns. `fio_fault.log` (S9) would now record such a crash in the field.
