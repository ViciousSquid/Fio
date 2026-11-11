> Applies to Fio 3.5.0.1111.

# Testing and CI

Fio's desktop test suite is a single pytest run rooted at the repository: `pytest.ini` collects `tests/` and `plugins/`, and the repository-root `conftest.py` applies the same rules to every test. Tests are sorted into tiers by marker, from a headless core that needs only Python, NumPy, PyGLM, pygame-ce and Pillow, up to a visual tier that drives the real renderer on a real OpenGL context. The Android player's tests (`player/tests`) are not collected by this run; `android-build.yml` runs them against a staged runtime.

```bash
pip install -r requirements.txt pytest
python -m pytest            # everything except benchmarks
```

Run from the repository root. Python must be CPython 3.14 or newer with the GIL enabled (see [Building and Running from Source](https://github.com/ViciousSquid/Fio/wiki/Building-and-Running-from-Source)).

## Tiers and markers

Declared in `pytest.ini`:

| Marker | Needs | Meaning |
|---|---|---|
| *(unmarked)* | Python, NumPy, PyGLM, pygame-ce, Pillow | The headless core. No PyQt5, no PyOpenGL, no display |
| `qt` | PyQt5 and/or PyOpenGL importable | The desktop dependency set. No display (Qt runs on the `offscreen` platform plugin) and no GPU (PyOpenGL is imported, never called). The editor, the logic thread and anything reaching `editor.things` |
| `gl` | A real OpenGL 3.3 core context | The visual tier: shaders compile, frames are rendered and read back. Skipped automatically with a reason when no context can be created |
| `integration` | — | Several subsystems driven together |
| `perf` | — | Performance-regression guards that count work done, not wall-clock time |
| `benchmark` | — | Timing measurements; opt-in with `--run-benchmarks` |
| `slow` | — | Takes more than a second |

`integration`, `perf` and `slow` are orthogonal to the three dependency tiers. A module that calls `pytest.importorskip("PyQt5")` must carry the `qt` mark or no tier runs it; `tests/test_suite_integrity.py` enforces this.

### Commands

These are the commands CI runs (`.github/workflows/tests.yml`):

| Tier | Command |
|---|---|
| Fast headless core | `FIO_HEADLESS_TIER=1 python -m pytest -m "not qt and not gl and not benchmark and not slow" -q` |
| Qt, offscreen, no GPU | `QT_QPA_PLATFORM=offscreen python -m pytest -m "qt and not gl and not benchmark" -q` |
| Visual (software GL) | `LIBGL_ALWAYS_SOFTWARE=1 xvfb-run -a python -m pytest -m gl -q` |
| Shader portability with glslang | `FIO_REQUIRE_GLSLANG=1 python -m pytest tests/renderer/test_shader_portability.py -q` |
| Renderer benchmark | `LIBGL_ALWAYS_SOFTWARE=1 FIO_BENCH_OUT=benchmark.json xvfb-run -a python -m pytest tests/performance/test_renderer_benchmark.py -m benchmark --run-benchmarks -q -s` |

Other useful selections:

| What | Command |
|---|---|
| Everything except benchmarks | `python -m pytest` (with no display the `gl` tests skip; under `xvfb-run -a` they run) |
| Integration workflows | `python -m pytest -m integration` |
| Performance guards | `python -m pytest -m perf` |
| One area | `python -m pytest tests/geometry` |

`LIBGL_ALWAYS_SOFTWARE=1` selects Mesa's llvmpipe rasteriser, which is what CI uses.

### Linux system packages

The headless core needs none. For the Qt and visual tiers CI installs (Ubuntu):

```bash
sudo apt-get install -y --no-install-recommends \
  xvfb libgl1 libglx0 libopengl0 libgl1-mesa-dri libglx-mesa0 libegl1 \
  libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 \
  libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 \
  libxcb-shape0 libxcb-xinerama0 libxcb-xkb1
```

plus `glslang-tools` for the shader-portability step.

## What `conftest.py` does

The repository-root `conftest.py` applies to `tests/` and to the plugin test packages alike.

**Environment, at import time**

- With neither `DISPLAY` nor `WAYLAND_DISPLAY` set it defaults `QT_QPA_PLATFORM=offscreen` and `PYOPENGL_PLATFORM=egl`. When a display is present it is honoured, because the offscreen plugin cannot create a GL context.
- `XDG_RUNTIME_DIR` defaults to `/tmp/fio-test-runtime` (created with mode 0700).
- The repository root is put on `sys.path`.

**Benchmarks.** `--run-benchmarks` is a custom option. Without it, `pytest_collection_modifyitems` adds a skip marker (reason "benchmark tier: pass --run-benchmarks to run it") to every `benchmark`-marked test: they are collected and reported as skipped, not deselected.

**Headless tier (`FIO_HEADLESS_TIER`).** When this variable is set, `pytest_ignore_collect` parses each test module with `ast` *without importing it* and ignores the module if it has a module-level `pytestmark` naming `qt` or `gl`, or if it imports `PyQt5` or `OpenGL` anywhere. That keeps the dependency-light CI job from importing Qt transitively (`editor.things` imports PyQt5).

**OpenGL availability.** The autouse fixture `_skip_without_gl` skips every `gl`-marked test with "OpenGL tier unavailable: <reason>" unless `gl_availability()` succeeds. The probe runs once per process: it fails if PyQt5's GL classes are missing, if the Qt platform is `offscreen`, or if a 3.3 core `QOpenGLContext` cannot be created and made current on a `QOffscreenSurface` (24-bit depth, 8-bit stencil).

**Process-singleton isolation.** Fio keeps deliberate process-wide state; the autouse fixture `_isolate_process_singletons` snapshots it before each test and restores it after:

| State | Restored how |
|---|---|
| `editor.io_system._io_revision` | Set back to its pre-test value |
| `editor.io_system._target_index_cache` / `_target_index_key` | Always cleared |
| `editor.io_system.IO_REGISTRY` | Replaced with the pre-test contents; `_declared_outputs_cache` cleared; then the plugin manager's `reapply_registrations()` replays plugin declarations |
| `editor.things.Thing._counters` | Replaced with the pre-test contents |
| Plugin manager | Each plugin's `enabled` flag, `_auto_enabled`, `_io_registrations` and `services` restored; `_enabled_generation` bumped |
| `QApplication` font | Restored (the editor sets it from `settings.ini` when a window is built) |

The session fixture `_plugins_loaded_before_isolation` calls `plugins.manager.load_plugins()` once before any snapshot, so plugin registrations are inside every snapshot. It is guarded: without PyQt5 the suite runs without plugins.

**Deterministic RNG.** The autouse fixture `_deterministic_rngs` seeds `random` and NumPy's legacy global RNG with `DEFAULT_SEED = 20240914` before every test.

**Modal-dialog guard.** `pytest_configure` replaces the blocking Qt dialogs so a test cannot hang in a nested event loop. Each call is recorded as `(kind, detail)` and answered immediately:

| Dialog | Answer |
|---|---|
| `QMessageBox.warning` / `information` / `critical` | `QMessageBox.Ok` |
| `QMessageBox.question` | `QMessageBox.No` |
| `QFileDialog.getOpenFileName` / `getSaveFileName` | `("", "")` |
| `QFileDialog.getOpenFileNames` | `([], "")` |
| `QFileDialog.getExistingDirectory` | `""` |
| `QInputDialog.getText` / `getItem` | `("", False)` |
| `QInputDialog.getInt` / `getDouble` | `(0, False)` / `(0.0, False)` |
| `QDialog.exec_` / `exec` | `QDialog.Rejected` |

The `modal_dialogs` fixture returns the list recorded during the current test. A test that needs a particular answer monkeypatches the dialog, which takes precedence.

**Qt-callback exceptions fail the test.** `pytest_configure` installs `sys.excepthook = _qt_callback_excepthook`, which records the traceback of any exception escaping a Qt slot or virtual instead of letting PyQt5 abort the process. The autouse fixture `_qt_callback_errors_fail_the_test` re-installs the hook after each test and fails the test during which an exception was recorded. The same fixture fails a test that leaves a keyboard modifier held in Qt's process-wide keyboard state (and releases it for the next test).

**`settings.ini` restore.** The autouse fixture `_restore_repo_settings_ini` reads the repository's `settings.ini` before each test and writes the original bytes back if the test changed it (closing a `MainWindow` saves its layout there).

**Editor style.** Before the first `qt`-marked (non-`gl`) test, `pytest_runtest_setup` creates the `QApplication` if needed and installs the editor's style (`editor.desktop_style.prepare_qt`, `install_editor_style`).

### Fixtures

| Fixture | Scope | Provides |
|---|---|---|
| `qt_app` | session | The one `QApplication`, with the editor style installed. Skips if PyQt5 is not installed. Every `qt`-marked test gets it automatically (`_qt_tests_get_an_application`) |
| `main_window` | function | A real `editor.main_window.MainWindow(repo_root)`, shown, with the working directory changed to `tmp_path` so its settings are per-test. On the offscreen platform the logic thread is started explicitly (there is no `initializeGL`). Closed and destroyed afterwards |
| `fio_session` | function | A factory, `fio_session(level, **kwargs)`, returning a `FioTestSession`; every session opened is closed after the test |
| `modal_dialogs` | function | The dialogs auto-answered during the test |

## Directory map

| Path | Covers |
|---|---|
| `tests/geometry/` | `engine/brush_geometry.py`: convex invariants, clipping, transforms, the derived-geometry cache, property-based cases |
| `tests/engine/` | `EditorState`, dense tables, loaders (OBJ, GLB, images), audio and asset archives, items, terrain data logic, pause and HUD models |
| `tests/physics/` | AABB maths, player collision and movement, dynamic bodies, line of sight, the prop spatial index |
| `tests/spatial/` | The cell index and `SpatialGrid` |
| `tests/monster_ai/` | Monster behaviour, patrol, combat queries, projectiles, the AI thread |
| `tests/renderer/` | Render-state construction, render and entity tables, culling, light budget, portals, the terrain table, shader portability, the renderer contract |
| `tests/visual/` | The `gl` tier: lit scenes, fog, sprites, terrain, water on a real context |
| `tests/io/` | Connections, the reverse target index, validation and dormancy |
| `tests/logic/` | The I/O execution path, triggers, movers, logic entities, cutscenes |
| `tests/threading/` | Publication order, `LogicThread` lifetime, a pipeline soak |
| `tests/persistence/` | Map save/load round trips, malformed and legacy input, atomic writes, package export and import |
| `tests/undo_redo/` | Editor mutations as perform → undo → verify → redo → verify, and object lifetime in history |
| `tests/editor/` | Editor windows and panels through the real `MainWindow`: selection, property editor, hierarchy, terrain panel, view filters, layout, Qt object lifetime |
| `tests/console/` | Console commands |
| `tests/plugins/` | The plugin API as a contract, driven by the real fixture plugins in `tests/plugins/fixtures/` |
| `tests/integration/` | Workflows across the editor, runtime, plugins and renderer |
| `tests/e2e/` | Whole sessions through `FioTestSession`: play cycles, save/restore in Play, renderer hot swap, input to render state, world to pixels |
| `tests/regression/` | One test per fixed defect, stating the invariant that was broken |
| `tests/performance/` | Work-per-frame guards and the opt-in benchmarks |
| `tests/tools/` | The scripts in `tools/` (`compare_editor_layout`, `compare_editor_ui`, `debug_tables`, `fio_to_map`) |
| `tests/helpers/` | Shared factories, fakes and frozen references; not collected |
| `tests/test_suite_integrity.py` | The suite checking itself: every module compiles and defines a test, PyQt5-dependent modules are in the `qt` tier, machinery tests do not replace production owners with fakes |
| `tests/test_prop.py`, `tests/test_prop_runtime.py` | The Prop entity and its runtime |
| `plugins/bigworld/tests/`, `plugins/deathmatch/tests/`, `plugins/tidy/tests/` | Each plugin's own tests, kept next to the code and collected by the same run |
| `player/tests/` | The Android player; run separately (see `android-build.yml`) |

`pytest.ini` sets `norecursedirs = .git .buildozer build dist *.egg-info __pycache__ assets maps`.

## Helpers (`tests/helpers/`)

| Module | Contents |
|---|---|
| `paths.py` | `REPO_ROOT`, `repo_path(*parts)`, `read_source(*parts)` |
| `worlds.py` | Deterministic factories: `box_brush`, `angled_brush`, `make_thing`, `world`, `level_data`, `room`, `pillar_grid`, `stable_id`, … |
| `fakes.py` | Narrow test doubles: `ManualClock`, `RecordingIOManager`, `RecordingHandler`, `OrderRecordingDict` |
| `session.py` | `FioTestSession` (below) |
| `gl.py` | `GLTestContext` (a current core-profile context rendering into an offscreen FBO, used as a context manager), `make_renderer`, `lit_cube_scene`, `camera_matrices`, `render_config`, `no_gl_errors`, pixel helpers (`luminance`, `region`, `is_blank`) |
| `renderers.py` | `StubRenderer`, `FlatRenderer` for the renderer-contract tests |
| `reachability.py` | `paths_to` — object-graph reachability for "nothing may still hold X" checks |
| `crossfire.py` | Crossfire fixtures and the sorted-query baseline |
| `terrain_reference.py`, `block_mesh_reference.py`, `cutscene_reference.py`, `hierarchy_reference.py`, `audio_archive_reference.py` | Frozen copies of earlier implementations. Equivalence tests compare the live code against them; they must not be changed to match the engine |

### `FioTestSession`

`tests/helpers/session.py: FioTestSession` opens the real editor on a map and lets a test advance the simulation one fixed tick at a time. Everything underneath is production code — `MainWindow` loads the map, `enter_play_mode` starts the session, `LogicThread._step_frame` runs the tick, `ThreadedGameState` publishes the frame — and only the environment is controlled:

- **Time**: after start-up the logic thread's own loop is halted (`logic.running = False`, joined); ticks happen only when the test calls `step()`, each `LogicThread.TICK_DURATION` (1/60 s) long.
- **Monster AI**: `start_play()` stops the AI thread; `step()` calls `MonsterAI.update(2 * TICK_DURATION)` under the monster lock on every other tick, reproducing its 30 Hz against the logic's 60 Hz.
- **Input**: written to `ThreadedGameState`, where Qt's key handlers put it.
- **Display**: offscreen Qt, or a real display for `paint()`.

| Member | Purpose |
|---|---|
| `FioTestSession(app, level, *, size=(320, 180))` | `level` is a repo-relative map path or a level-data dict. Changes the working directory to the repository root and remembers `settings.ini` |
| `window`, `view`, `logic` | The `MainWindow`, its `QtGameView` and its `LogicThread` |
| `state`, `game_state`, `playing` | `EditorState`, `ThreadedGameState`, whether Play is on |
| `start_play()` / `stop_play()` | Enter / leave Play through `MainWindow.enter_play_mode` |
| `step(ticks=1, *, keys=(), mouse=(0.0, 0.0), shoot=False, use=False)` | Run fixed ticks with this input held, publishing a frame after each |
| `publish()` | Project and publish the world as it is, without advancing time |
| `render_state()` | Context manager lending the newest published frame, as the renderer borrows it |
| `paint()` | Draw the newest frame through `QtGameView.paintGL` and return RGB pixels as a NumPy array; needs a real GL context |
| `close()` | Stop Play, close the window, restore `settings.ini` and the working directory, and assert that the window was actually garbage-collected |

Use the `fio_session` fixture rather than constructing it directly, so sessions are closed even when the test fails:

```python
import pytest

pytest.importorskip("PyQt5")
from PyQt5.QtCore import Qt

pytestmark = [pytest.mark.qt, pytest.mark.integration, pytest.mark.slow]


def test_walking_forward_moves_the_player(fio_session):
    session = fio_session("maps/_SHOWCASE.json")
    session.start_play()
    session.step(2)
    with session.render_state() as frame:
        start = tuple(float(v) for v in frame.player_pos)
        assert frame.entity_table.count > 0

    session.step(40, keys={Qt.Key_W})

    with session.render_state() as frame:
        assert tuple(float(v) for v in frame.player_pos) != start
    session.stop_play()
```

A level can also be given as data built with `tests/helpers/worlds.py` (`fio_session(level_data(...))`). `tests/e2e/test_play_cycles.py` and `tests/editor/test_view_filters.py` are complete examples of both forms.

## Conventions

- **Deterministic.** No test reads the clock to decide an outcome. Generated cases enumerate their seeds so a failure reproduces from its id. Tests that use real threads poll for a condition under a deadline.
- **Isolated.** Process-wide state is restored around every test (above), so order cannot change a result.
- **Real objects where it matters.** Integration tiers drive the real `EditorState`, `LogicThread`, `SpatialGrid` and renderer. If a test needs a plugin, add one under `tests/plugins/fixtures/`; they are real plugins loaded by the real host.
- **Placement.** Put a test in the area it belongs to, mark it if it needs more than the headless core, and prefer the `worlds.py` factories over hand-built dicts.

## Renderer benchmark

`tests/performance/test_renderer_benchmark.py` (markers `benchmark`, `gl`) renders seven paths — `simple`, `dynamic_light`, `shadows`, `frustum_cull_on`, `many_visible`, `many_culled`, `many_entities` — discarding 12 warm-up frames and measuring 40, and also times the scalar against the batched distance cull and the sprite sort. It prints a table and never fails on an absolute number.

| Variable | Effect |
|---|---|
| `FIO_BENCH_OUT=path.json` | Write the measurements as JSON |
| `FIO_BENCH_BASELINE=path.json` | Compare against a JSON file written by an earlier run on the *same* machine; the test fails only if a path's mean frame time is more than 1.6× its baseline (`REGRESSION_FACTOR`) |

The JSON maps each path name to its measurements:

```json
{
  "simple": {
    "frames": 40,
    "elapsed": 0.4123,
    "average_fps": 97.02,
    "cold_ms": 85.317,
    "mean_ms": 10.307,
    "p95_ms": 11.92,
    "worst_ms": 13.004
  }
}
```

`cold_ms` is the first frame (shader compilation, buffer uploads), measured separately from the steady state. The numbers above only illustrate the shape. The other modules in `tests/performance/` whose names end in `_benchmark.py` are opt-in in the same way. `tools/bench_compare.py` and `tools/bench_editor.py` are standalone end-to-end benchmarks through the real editor host and are not part of the pytest run.

## Shader portability

`tests/renderer/test_shader_portability.py` checks every `.vert` and `.frag` source in `engine.shaders.DEFAULT_SHADERS` in two layers:

1. Pure-Python checks that always run: no shader defines a function named like a GLSL built-in (`noise2`, `mix`, …) or uses an identifier reserved by later GLSL versions or GLSL ES (`patch`, `sample`, `centroid`, …).
2. When `glslangValidator` (the Khronos reference compiler, Ubuntu package `glslang-tools`) is on `PATH`, every shader is compiled with it as desktop GLSL 3.30 and, through the player's translator, as GLSL ES 3.00. Without it those tests skip; with `FIO_REQUIRE_GLSLANG=1` a missing compiler is an error instead.

## GitHub workflows

All in `.github/workflows/`. Every job that runs Python uses 3.14 except the Android APK jobs (3.12 for the build tooling).

| Workflow | Triggers | What it does | Artifacts |
|---|---|---|---|
| `tests.yml` — *Tests* | Push to any branch, pull request, manual, and `workflow_call` | Five independent jobs (below). Runs for one ref are cancelled when a newer commit arrives | `renderer-benchmark` (`benchmark.json`) |
| `build.yml` — *Nuitka Build and Validate* | Manual | Runs `tests.yml` on the build commit, builds Windows x64, Windows ARM64, macOS and Linux with Nuitka, then creates a GitHub release. The macOS release asset is the zipped app | `fio-windows`, `fio-windows-arm64`, `fio-macos`, `fio-linux` |
| `full-build-all-platforms.yml` — *FULL BUILD ALL PLATFORMS* | Manual | Identical to `build.yml` except that the macOS release asset is the `.dmg` installer image | same |
| `build-windows-arm64.yml` — *Native Windows ARM64 Build* | Manual; push to `2.6_CANARY` or `ARM64` that changes the workflow file | Builds Qt 5.15.2 (cached) and PyQt5 5.15.11 from source on a native ARM64 runner, then builds Fio with Nuitka and checks the executable's machine type and the packaged fog texture | `fio-windows-arm64` |
| `nuitka-build-console.yml` — *Nuitka Build Windows (Console)* | Manual | The Windows x64 Nuitka build with `--windows-console-mode=force`, for a build that keeps its console | `fio-windows-console` |
| `android-build.yml` — *Android Player Build* | Manual; push or pull request touching `player/**`, `engine/**`, `plugins/**`, `editor/*.py`, `assets/**`, `maps/**`, `cutscenes/**`, `main.py`, `python_runtime.py` or the workflow | `validate`: package-compatibility tests, exports `.fiopak` worlds with the desktop exporter, stages the Android runtime and runs `player/tests` there, GLES smoke checks under Mesa. `apk`: builds debug and release APKs with Buildozer and signs the release when signing secrets exist. `emulator`: installs an x86_64 debug APK on an emulator, opens the showcase and moves with touch input | `desktop-showcase-world`, `universal-fiopak-contract`, `fio-player-debug-apk`, `fio-player-release-<mode>-apk`, `android-showcase-device-evidence`, `fio-player-emulator-debug-apk` |
| `editor-ui-parity.yml` — *Native Windows and macOS editor UI parity* | Manual; push touching `editor/**`, `tools/editor_ui_audit.py`, `tools/compare_editor_ui.py`, `assets/fonts/editor/**`, `assets/ui/controls/**`, `main.py`, `editor_styles.py` or the workflow | Matrix of Windows and macOS × `QT_SCALE_FACTOR` 1 and 2: runs three editor style tests, then `tools/editor_ui_audit.py` at font size 11 (screenshots and metrics) and 16 (metrics only). A `compare` job joins the captures with `tools/compare_editor_ui.py` | `editor-ui-<platform>-scale-<n>` (manifests and PNGs), `editor-ui-parity-report` (open `index.html`) |
| `editor-screenshots.yml` — *Native editor screenshots* | Manual (input `map`, default `maps/_SHOWCASE.json`); push touching the workflow, `tools/compare_editor_layout.py`, `editor/hierarchy_dock.py` or `editor/desktop_style.py` | Starts the unmodified `main.py` on Windows, macOS, Linux X11 and Linux Wayland at 1920×1080, 2560×1440 and 2048×1080, loads the map, captures the screen, and records every visible control and dock at a fixed 1600×900 client size. `layout-parity` then runs `tools/compare_editor_layout.py captures --tolerance 1 --expect windows macos linux-x11 linux-wayland` on the 1080p captures | `editor-screenshot-<platform>-<size>` |

### `tests.yml` jobs

| Job | Installs | Runs |
|---|---|---|
| `no_gpu` — No-GPU fast core (2-minute timeout) | `pytest`, `numpy==2.5.3`, `PyGLM==2.8.3`, `pygame-ce==2.5.8`, `Pillow==11.3.0` only | Verifies CPython ≥ 3.14 with the GIL, then the fast headless command with `FIO_HEADLESS_TIER=1` |
| `qt_no_gpu` — Qt / no-GPU suite (30-minute timeout) | System Qt/XCB libraries, `requirements.txt`, `pytest` | The Qt command with `QT_QPA_PLATFORM=offscreen` |
| `opengl` — Renderer | Same plus `glslang-tools` | The visual tier under `xvfb-run` with llvmpipe, then the shader-portability test with `FIO_REQUIRE_GLSLANG=1` |
| `benchmark` — Renderer benchmark | Same as `qt_no_gpu` | The benchmark command; `continue-on-error`, so it never fails the run |
| `launch` — Fio launch | System libraries, `requirements.txt` | Verifies the interpreter and pinned versions, starts `python main.py` under `xvfb-run` with `QT_QPA_PLATFORM=xcb` and llvmpipe, and fails if the process exits within 12 seconds |

### Release builds

`build.yml` and `full-build-all-platforms.yml` build with Nuitka 4.2.2. Each build job appends its platform (`WinX64`, `WinARM64`, `macOS`, `Linux`) to `editor/version.txt` in its own checkout and validates that the packaged file carries it. The `release` job needs `tests` and all four builds, must run from a branch named `X.X.X.XXXX_Latest` whose version equals `editor/version.txt`, and creates a release tagged with that version. The Nuitka options are listed in [Building and Running from Source](https://github.com/ViciousSquid/Fio/wiki/Building-and-Running-from-Source).

## Layout and UI parity tools

| Tool | Usage | Purpose |
|---|---|---|
| `tools/editor_ui_audit.py` | `python tools/editor_ui_audit.py --output DIR [--font-size 11] [--metrics-only] [--sections a,b] [--source REPO]` | Opens the editor on the native Qt backend, walks every editor surface, and writes `manifest.json` (resolved widget metrics per frame, plus surfaces and errors) and, unless `--metrics-only`, a PNG per frame. Needs no rendering context or playable world |
| `tools/compare_editor_ui.py` | `python tools/compare_editor_ui.py --captures DIR --output DIR` | Joins the Windows and macOS manifests into `index.html` (side by side) and `summary.json`. Exits non-zero on a critical difference (font, tab geometry, oversized control, newly clipped text), when no pairs were found, on capture errors or on a missing platform |
| `tools/compare_editor_layout.py` | `python tools/compare_editor_layout.py CAPTURES [--tolerance N] [--expect platform …]` | Compares the `*-layout.json` records from the screenshot jobs: the same controls and docks with the same text, enabled state and font. Dock rectangles must be identical; control and tab rectangles and measured text metrics may differ by at most `--tolerance` pixels (default 0) |

`tests/tools/test_compare_editor_layout.py` and `tests/tools/test_compare_editor_ui.py` cover the two comparison tools.
