# Fio test suite

One suite, four tiers, selected by marker. Everything is run with `pytest` from
the repository root; `pytest.ini` already points it at the three roots that hold
tests (`tests/`, `plugins/`, `player/`), so no path arguments are needed.

```bash
pip install -r requirements.txt pytest
python -m pytest                     # everything except the benchmarks
```

## The commands

| What | Command |
| --- | --- |
| Everything (except benchmarks) | `python -m pytest` |
| Headless only — no Qt, no OpenGL, no display | `python -m pytest -m "not qt and not gl and not benchmark"` |
| Editor + engine (Qt, offscreen, no GPU) | `QT_QPA_PLATFORM=offscreen python -m pytest -m "not gl and not benchmark"` |
| Renderer / visual (needs a real GL context) | `xvfb-run -a python -m pytest -m gl` |
| Integration workflows | `python -m pytest -m integration` |
| Performance guards | `python -m pytest -m perf` |
| Renderer benchmark (reports timings) | `xvfb-run -a python -m pytest -m benchmark --run-benchmarks -s` |
| One area | `python -m pytest tests/geometry` |

On a machine with no GPU, `LIBGL_ALWAYS_SOFTWARE=1` in front of the `xvfb-run`
commands selects Mesa's `llvmpipe` rasteriser, which is what CI uses.

A plain `python -m pytest` picks the tier up automatically: with no display it
runs Qt offscreen and the `gl` tests skip with a reason; under a display
(`xvfb-run -a python -m pytest`) the same command runs the visual tier too.

## The tiers

**Unmarked — the headless core.** Runs on Python, NumPy and PyGLM alone: no
PyQt5, no PyOpenGL, no display, no GPU. Geometry, the spatial index, the I/O
system, the logic dispatcher, the plugin API, Big World, persistence and the
threading invariants all live here. This is the tier CI runs first, and the one
a failure should be read from: if it is red, something in Fio is broken, not
something about the machine.

**`qt` — the desktop dependency set.** Needs PyQt5 and/or PyOpenGL to be
*importable*. It does not need a display (Qt runs against the `offscreen`
platform plugin) and it does not need a GPU (PyOpenGL is imported, never
called). The editor, the property editor, entity types and anything that
reaches through `editor.things` is here, and so is the logic thread, because it
pulls in the editor's entity classes.

**`gl` — the visual tier.** Needs a real OpenGL context. These drive the actual
`Renderer_F` against an offscreen framebuffer: shaders compile, geometry is
submitted, shadow maps are generated, the frame is read back and checked. They
skip with an explicit reason (not a mysterious error) when no context can be
created — the offscreen Qt platform plugin cannot make one, so they need a
display, real or virtual.

**`benchmark` — measurements, never a verdict.** Excluded from every normal run;
`--run-benchmarks` opts in. It reports frames, elapsed time, average FPS and
frame-time percentiles for several renderer paths, with the cold frame measured
separately from the steady state. It fails only against a stored baseline from
the *same* machine (`FIO_BENCH_BASELINE=path.json`), never on an absolute
number.

Two more markers are orthogonal to the tiers: `integration` (several subsystems
driven together) and `perf` (guards that count work rather than timing it, e.g.
"this array is built once per session, not once per frame"). `slow` marks the
handful of tests that use real threads and real time.

## Where things live

| Area | What it covers |
| --- | --- |
| `tests/geometry/` | `engine/brush_geometry.py`: convex invariants, clipping, transforms, the derived-geometry cache, generated/property-based cases |
| `tests/engine/` | `EditorState` — the shared world: entities, ids, lookup, selection, legacy maps |
| `tests/physics/` | AABB maths and the collide-and-slide primitives |
| `tests/spatial/` | The 512-unit cell convention and `SpatialGrid` |
| `tests/monster_ai/` | Monster behaviour, patrol, combat, and the AI thread's lifetime |
| `tests/renderer/` | Render-state construction, culling, double buffering, light budget |
| `tests/plugins/` | The plugin API as a contract, driven by the fixture plugins in `tests/plugins/fixtures/` |
| `tests/io/` | Connections, the reverse target index and its revision counter |
| `tests/logic/` | The I/O execution path: dispatch, delays, chains, loops, failure |
| `tests/threading/` | Lock-free publication order, `LogicThread` lifetime |
| `tests/persistence/` | Map save/load round trips, malformed and legacy input |
| `tests/undo_redo/` | Every editor mutation as perform → undo → verify → redo → verify, and what still points at a live object afterwards |
| `tests/editor/` | Component editing, selection modes, the property editor, Qt object lifetime |
| `tests/integration/` | End-to-end workflows across the editor, runtime, plugins and renderer |
| `tests/regression/` | One test per fixed defect, stating the invariant that was broken |
| `tests/visual/` | The `gl` tier: a lit cube, its shadow, and a moving light |
| `tests/performance/` | Work-per-frame guards and the renderer benchmark |
| `tests/helpers/` | Shared factories and fakes (not collected) |

The plugin and player packages keep their own tests next to the code
(`plugins/*/tests/`, `player/tests/`) because both are exported as
self-contained packages; `pytest.ini` includes them in every run.

## Conventions

* **Deterministic.** No test reads the clock to decide an outcome. Generated
  cases enumerate their seeds, so a failure reproduces from its test id
  (`...[seed17]`). The few tests that use real threads poll for a condition
  under an explicit deadline rather than sleeping a fixed amount.
* **Isolated.** The repository-root `conftest.py` resets the process-wide state
  Fio keeps — the plugin manager's enabled flags, the I/O revision counter and
  its cached index, `Thing`'s name counters, the RNGs — around every test, so
  order cannot change a result.
* **Useful failures.** Assertions carry the expected value, the actual value
  and the identity of whatever went wrong (the brush's name, the monster's
  state key, the plane's normal). `assert result` with no message is not a
  test anyone can debug.
* **Real objects where it matters.** Behavioural tests use lightweight fakes
  for the surface they are not testing, but the integration tiers drive the
  real `EditorState`, the real `LogicThread`, the real `SpatialGrid` and the
  real renderer. A fake that answers more kindly than the engine proves
  nothing.

## Adding a test

Put it in the area it belongs to, mark it if it needs more than Python and
NumPy, and prefer the factories in `tests/helpers/worlds.py` over hand-built
dicts so the fixture reads as a world rather than as JSON. If a test needs a
plugin, add one to `tests/plugins/fixtures/` rather than reaching for a mock —
they are real plugins, and the point is that the host treats them as such.
