"""Standalone Fio performance benchmark.

This is deliberately independent of pytest.  The development test suite uses
pytest, but Tools > Benchmark must measure Fio without making pytest a runtime
dependency.

The renderer workload uses Fio's real Renderer_F and world representation.
I/O uses the production IOManager, OutputConnection, LogicRelay and registered
input handlers.  CSG uses the production engine.brush_geometry.clip_brush API.

Run directly:
    python tests/performance/fio_benchmark.py
"""
import json
import math
import os
import platform
import statistics
import sys
import time
import traceback

# This file is intentionally runnable directly (without pytest).  When Python
# executes a script by path, sys.path starts at tests/performance rather than
# the repository root, so add the Fio root before importing test helpers.
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from tests.helpers import gl as glh
from tests.helpers.worlds import box_brush, make_thing
from editor.procedural_generator import create_map_data


WARMUP_FRAMES = 10

# Every benchmark workload that uses the procedural map generator must use
# this seed.  Changing the workload size may change the number of spawned
# entities, but the generator receives the same deterministic seed every time.
BENCHMARK_MAP_SEED = 0xF10
MEASURED_FRAMES = 30

DEFAULT_RESOLUTIONS = (
    (192, 192),
    (1280, 720),
    (1600, 900),
    (1920, 1080),
    (2560, 1440),
    (2880, 1920),
)

SCENARIOS = (
    ("lit_scene", False, False),
    ("lit_scene_with_shadows", True, False),
    ("empty_world", False, True),
)


def _execution_environment():
    """Return accurate Python process/host architecture information."""
    process_arch = platform.machine() or "unknown"
    host_arch = process_arch
    translation = "none detected"

    if sys.platform == "win32":
        try:
            import ctypes

            names = {
                0x014C: "x86",
                0x8664: "x64",
                0xAA64: "ARM64",
            }
            kernel32 = ctypes.windll.kernel32
            process_machine = ctypes.c_ushort()
            native_machine = ctypes.c_ushort()
            fn = getattr(kernel32, "IsWow64Process2", None)

            if fn is not None:
                fn.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(ctypes.c_ushort),
                    ctypes.POINTER(ctypes.c_ushort),
                ]
                fn.restype = ctypes.c_bool
                ok = fn(
                    kernel32.GetCurrentProcess(),
                    ctypes.byref(process_machine),
                    ctypes.byref(native_machine),
                )
                if ok:
                    native_value = native_machine.value
                    process_value = process_machine.value

                    host_arch = names.get(
                        native_value,
                        platform.machine() or "unknown",
                    )

                    # IsWow64Process2 reports 0 for a native process on some
                    # Windows versions.  Fall back to the Python architecture
                    # in that case rather than inventing an emulation mode.
                    if process_value == 0:
                        process_arch = platform.machine() or host_arch
                    else:
                        process_arch = names.get(
                            process_value,
                            "0x%04X" % process_value,
                        )

                    # A differing x86/x64 process on an ARM64 host is the
                    # Windows-on-ARM emulation case.  Do not call ordinary
                    # 32-bit x86 on x64 Windows "Prism".
                    if native_value == 0xAA64 and process_value in (
                        0x014C,
                        0x8664,
                    ):
                        translation = (
                            "Microsoft Prism / Windows on ARM emulation"
                        )
                    elif (
                        native_value == 0xAA64
                        and process_value == 0
                        and process_arch != "ARM64"
                    ):
                        translation = (
                            "Microsoft Prism / Windows on ARM emulation"
                        )
        except Exception:
            pass

    elif sys.platform == "darwin":
        try:
            import ctypes

            libc = ctypes.CDLL(None)
            translated = ctypes.c_int(0)
            size = ctypes.c_size_t(ctypes.sizeof(translated))
            if (
                libc.sysctlbyname(
                    b"sysctl.proc_translated",
                    ctypes.byref(translated),
                    ctypes.byref(size),
                    None,
                    0,
                )
                == 0
                and translated.value == 1
            ):
                translation = "Apple Rosetta 2"
                host_arch = "ARM64"
        except Exception:
            pass

    return (
        "Python: %s | Host CPU: %s | Translation: %s"
        % (process_arch, host_arch, translation)
    )


def _resolutions():
    raw = os.environ.get("FIO_FULLSCREEN_BENCH_RESOLUTIONS")
    if not raw:
        return DEFAULT_RESOLUTIONS

    result = []
    for item in raw.split(","):
        width, height = item.strip().lower().split("x", 1)
        result.append((int(width), int(height)))
    return tuple(result)


def _render(renderer, context, brushes, things):
    """Render one real production Renderer_F frame and synchronise GPU completion.

    This standalone renderer microbenchmark intentionally does not enable
    camera-distance culling. The live Play Mode benchmarks exercise the actual
    engine culling path, including its contiguous production position snapshot.
    Keeping culling disabled here prevents the compatibility scalar fallback
    from becoming part of a renderer-only benchmark by accident.
    """
    import OpenGL.GL as gl

    aspect = float(context.width) / float(max(1, context.height))
    projection, view, eye = glh.camera_matrices(aspect=aspect)

    config = glh.render_config(
        all_brushes=brushes,
        all_things=things,
        camera_distance_cull=False,
    )

    context.bind()
    gl.glClearColor(0.0, 0.0, 0.0, 1.0)
    renderer.render_scene(
        projection,
        view,
        eye,
        brushes,
        things,
        None,
        config,
    )
    gl.glFinish()


def _measure_scenario(width, height, name, shadows, empty):
    """Measure a renderer scenario and report through Fio's SysMon metrics."""
    glh.reset_texture_cache()

    if empty:
        brushes, things = [], []
    else:
        data = _generate_procedural_map(monsters=0, relay_count=32)
        state = _materialize_generated_map(data)
        brushes, things = state.brushes, state.things

    with glh.GLTestContext(width, height) as context:
        renderer = glh.make_renderer()
        try:
            _render(renderer, context, brushes, things)

            samples, sysmon_metrics = _render_sample_set(
                renderer,
                context,
                brushes,
                things,
                warmup=WARMUP_FRAMES,
                samples=MEASURED_FRAMES,
            )

            mean = statistics.fmean(samples)
            pixels = width * height
            megapixels = pixels / 1_000_000.0

            return {
                "scenario": name,
                "width": width,
                "height": height,
                "pixels": pixels,
                "megapixels": megapixels,
                "mean_ms": mean * 1000.0,
                "p95_ms": sysmon_metrics["p95_frame_time_ms"],
                "worst_ms": max(samples) * 1000.0,
                "average_fps": sysmon_metrics["fps"],
                "ms_per_megapixel": (
                    mean * 1000.0 / megapixels
                    if megapixels
                    else float("inf")
                ),
                "sysmon": sysmon_metrics,
            }
        finally:
            try:
                renderer.cleanup()
            except Exception:
                pass

    glh.reset_texture_cache()
def _generate_procedural_map(monsters=0, relay_count=32, seed=BENCHMARK_MAP_SEED, live_monster=False, yield_hook=None):
    """Generate a real Fio map using the same procedural generator as the editor."""
    import random

    random.seed(seed)
    # Live monster benchmarks are deliberately compact. The workload is
    # supposed to stress Monster/LogicThread/renderer behaviour, not spend the
    # preparation phase constructing a huge 4096x4096 procedural level and
    # thousands of unrelated wall brushes. Standalone renderer benchmarks
    # keep the larger map when live_monster=False.
    if live_monster:
        world_width = 2048
        world_height = 2048
        room_count = 8
        max_room = 384
        enable_floors = False
        floor_room_count = 0
    else:
        world_width = 4096
        world_height = 4096
        room_count = 18
        max_room = 640
        enable_floors = True
        floor_room_count = 3

    params = {
        "world_width": world_width,
        "world_height": world_height,
        "min_room": 256,
        "max_room": max_room,
        "room_count": room_count,
        "wall_tex": "default.png",
        "floor_tex": "default.png",
        "enable_floors": enable_floors,
        "floor_height": 128,
        "floor_room_count": floor_room_count,
        "spawn_monsters": monsters > 0,
        "monster_count": monsters,
        "spawn_health": False,
    }
    data = create_map_data(params, yield_hook=yield_hook)

    # Add actual Fio LogicRelay entities and serialized I/O links to the
    # generated map. These are consumed by the normal map loader.
    things = data["things"]
    relay_start = len(things)
    for i in range(relay_count):
        if yield_hook is not None and i % 25 == 0:
            yield_hook()
        things.append({
            "type": "logicrelay",
            "pos": [128.0 + i * 48.0, 32.0, 128.0],
            "properties": {
                "type": "logicrelay",
                "name": "BenchmarkRelay_%d" % i,
                "id": "benchmark_relay_%d" % i,
                "fire_once": False,
            },
            "io_connections": [],
        })

    for i in range(relay_count - 1):
        if yield_hook is not None and i % 25 == 0:
            yield_hook()
        things[relay_start + i]["io_connections"] = [{            "output": "OnTrigger",
            "target": "BenchmarkRelay_%d" % (i + 1),
            "target_id": "benchmark_relay_%d" % (i + 1),
            "input": "Trigger",
            "parameter": "",
            "delay": 0.0,
            "fire_once": False,
        }]

    # Route monster combat outputs into real LogicRelay sinks.  These are
    # deliberately one-hop sinks: every attack/damage event exercises the
    # production I/O dispatcher without turning the benchmark into an
    # artificial recursive relay stress test.
    monsters_in_map = [
        t for t in things
        if str(t.get("type", "")).lower() == "monster"
    ]
    if monsters_in_map and relay_count:
        for index, monster in enumerate(monsters_in_map):
            sink = "BenchmarkRelay_%d" % (index % relay_count)
            sink_id = "benchmark_relay_%d" % (index % relay_count)
            monster.setdefault("io_connections", []).extend([
                {
                    "output": "OnAttack",
                    "target": sink,
                    "target_id": sink_id,
                    "input": "Trigger",
                    "parameter": "",
                    "delay": 0.0,
                    "fire_once": False,
                },
                {
                    "output": "OnDamaged",
                    "target": sink,
                    "target_id": sink_id,
                    "input": "Trigger",
                    "parameter": "",
                    "delay": 0.0,
                    "fire_once": False,
                },
            ])

    return data


# Live benchmark loader deliberately accepts yield_hook so large worlds can be built cooperatively.
def load_live_benchmark_world(window, data, yield_hook=None):
    """Load benchmark data through the normal live Fio editor machinery.

    Brush stress scenes deliberately exercise EditorState, the scene
    hierarchy, all orthographic views, and the 3D view just as normal editor
    work does. The benchmark must not substitute a reduced 3D-only path.
    """
    window.state.load_from_data(
        data,
        yield_hook=yield_hook,
        save_undo=False,
    )
    if yield_hook is not None:
        yield_hook()

    # Always rebuild the normal editor UI and every view. Large brush
    # workloads are specifically intended to exercise these production paths.
    window.update_all_ui()
    if yield_hook is not None:
        yield_hook()
    window.update_views()
    if yield_hook is not None:
        yield_hook()
    window.view_3d.update()
    window.view_top.update()
    window.view_side.update()
    window.view_front.update()

    if yield_hook is not None:
        yield_hook()


def _materialize_generated_map(data):
    """Turn generated JSON-shaped data into the real Fio EditorState objects."""
    from editor.editor_state import EditorState

    state = EditorState()
    state.load_from_data(data)

    # Rebuild generated relay graphs through the real Thing API so the
    # benchmark exercises Fio's OutputConnection objects, not just JSON.
    raw_things = data.get("things", [])
    generated = {}
    for raw in raw_things:
        properties = raw.get("properties", raw)
        name = str(properties.get("name", ""))
        if name.startswith(("BenchmarkRelay_", "ApocalypseRelay_")):
            generated[name] = raw

    loaded = {
        str(t.properties.get("name", "")): t
        for t in state.things
        if str(t.properties.get("name", "")).startswith(
            ("BenchmarkRelay_", "ApocalypseRelay_")
        )
    }

    for name, raw in generated.items():
        source = loaded.get(name)
        if source is None:
            raise RuntimeError("generated benchmark relay did not load: %s" % name)

        source.properties["_io_connections"] = []
        for connection in raw.get("io_connections", []):
            target_name = connection.get("target", "")
            target = loaded.get(target_name)
            if target is None:
                raise RuntimeError(
                    "generated benchmark relay target did not load: %s -> %s"
                    % (name, target_name)
                )
            source.add_output_connection(
                output_name=connection.get("output", "OnTrigger"),
                target_name=target.properties["name"],
                input_name=connection.get("input", "Trigger"),
                target_id=target.properties.get("id", ""),
            )

    return state


def _make_renderer_stress_scene():
    data = _generate_procedural_map(monsters=0, relay_count=32)
    state = _materialize_generated_map(data)
    return state.brushes, state.things


def _make_brush_stress_scene(brush_count, yield_hook=None):
    """Create a visible field of real Fio brushes with varied dimensions.

    The brushes stay spatially bounded so the generated workload remains
    visible in the editor instead of marching hundreds of thousands of units
    away from the camera.  The source map still supplies real Fio brush
    dictionaries/textures, while deterministic scaling makes the generated
    brushes visibly different sizes.
    """
    import copy
    import math
    import numpy as np

    data = _generate_procedural_map(
        monsters=0,
        relay_count=32,
        yield_hook=yield_hook,
    )
    source = list(data.get("brushes", []))
    if not source:
        raise RuntimeError("procedural benchmark map generated no brushes")

    brush_count = int(brush_count)
    if brush_count <= 0:
        data["brushes"] = []
        return data

    # Keep the authored scene and its PlayerStart together.  The generated
    # source map is shifted so its first brush field is centred on PlayerStart.
    player_start = next(
        (
            thing for thing in data.get("things", [])
            if str(thing.get("type", "")).lower() == "playerstart"
        ),
        None,
    )
    target_x = float((player_start or {}).get("pos", [0.0, 0.0, 0.0])[0])
    target_z = float((player_start or {}).get("pos", [0.0, 0.0, 0.0])[2])

    min_x = min_z = float("inf")
    max_x = max_z = float("-inf")
    for brush in source:
        pos = brush.get("pos") or [0.0, 0.0, 0.0]
        size = brush.get("size") or [64.0, 64.0, 64.0]
        hx = abs(float(size[0])) * 0.5
        hz = abs(float(size[2])) * 0.5
        bx = float(pos[0])
        bz = float(pos[2])
        min_x = min(min_x, bx - hx)
        max_x = max(max_x, bx + hx)
        min_z = min(min_z, bz - hz)
        max_z = max(max_z, bz + hz)

    source_centre_x = (min_x + max_x) * 0.5
    source_centre_z = (min_z + max_z) * 0.5
    scene_shift = (
        target_x - source_centre_x,
        target_z - source_centre_z,
    )

    source_count = len(source)
    tile_count = int(math.ceil(float(brush_count) / float(source_count)))
    grid_dim = int(math.ceil(math.sqrt(tile_count)))
    source_width = max(512.0, max_x - min_x)
    source_depth = max(512.0, max_z - min_z)
    tile_spacing_x = source_width + 256.0
    tile_spacing_z = source_depth + 256.0

    indices = np.arange(brush_count, dtype=np.int64)
    source_indices = indices % source_count
    tile_indices = indices // source_count
    tile_x = (tile_indices % grid_dim).astype(np.float64)
    tile_z = (tile_indices // grid_dim).astype(np.float64)
    tile_x -= (grid_dim - 1) * 0.5
    tile_z -= (grid_dim - 1) * 0.5

    # Deliberately vary all three dimensions.  The pattern is deterministic,
    # avoids degenerate boxes, and is obvious in both orthographic and 3D views.
    size_patterns = np.asarray(
        (
            (0.55, 0.75, 0.85),
            (0.80, 1.15, 0.65),
            (1.20, 0.70, 1.10),
            (1.45, 1.00, 0.80),
            (0.70, 1.45, 1.25),
            (1.30, 0.85, 1.40),
            (0.95, 1.30, 0.75),
            (1.55, 0.65, 1.20),
        ),
        dtype=np.float64,
    )

    brushes = []
    for output_index, (
        source_index, tile_offset_x, tile_offset_z
    ) in enumerate(
        zip(
            source_indices.tolist(),
            tile_x.tolist(),
            tile_z.tolist(),
        )
    ):
        if yield_hook is not None and output_index % 64 == 0:
            yield_hook()

        brush = copy.deepcopy(source[int(source_index)])
        position = list(brush.get("pos", [0.0, 0.0, 0.0]))
        size = np.asarray(
            brush.get("size", [64.0, 64.0, 64.0]),
            dtype=np.float64,
        )
        scale = size_patterns[output_index % len(size_patterns)]

        position[0] = (
            float(position[0])
            + scene_shift[0]
            + float(tile_offset_x * tile_spacing_x)
        )
        position[2] = (
            float(position[2])
            + scene_shift[1]
            + float(tile_offset_z * tile_spacing_z)
        )
        size = np.maximum(np.abs(size) * scale, 8.0)

        brush["pos"] = [
            float(position[0]),
            float(position[1]),
            float(position[2]),
        ]
        brush["size"] = [float(v) for v in size]
        brush["id"] = "benchmark_generated_%d" % output_index
        brushes.append(brush)

    # Keep all non-brush entities aligned with the shifted first scene tile.
    for thing in data.get("things", []):
        pos = thing.get("pos")
        if pos and len(pos) >= 3:
            pos[0] = float(pos[0]) + scene_shift[0]
            pos[2] = float(pos[2]) + scene_shift[1]

    data["brushes"] = brushes
    return data


def _run_monster_stress(count):
    """Generate a real procedural Fio room populated with N monsters."""
    data = _generate_procedural_map(
        monsters=count, relay_count=32, seed=BENCHMARK_MAP_SEED
    )
    state = _materialize_generated_map(data)
    brushes, things = state.brushes, state.things
    results = []

    for mode, width, height in (
        ("windowed-sized", 1280, 720),
        ("fullscreen-sized", 1920, 1080),
    ):
        glh.reset_texture_cache()
        with glh.GLTestContext(width, height) as context:
            renderer = glh.make_renderer()
            try:
                samples, sysmon_metrics = _render_sample_set(
                    renderer, context, brushes, things, warmup=3, samples=10
                )
                mean = statistics.fmean(samples)
                results.append(_timing_result(
                    "monster_room_%d_%s" % (count, mode),
                    "procedural Fio map with %d real Monster entities" % count,
                    samples,
                    mode=mode,
                    monster_count=count,
                    brush_count=len(brushes),
                    entity_count=len(things),
                    resolution="%dx%d" % (width, height),
                    average_fps=sysmon_metrics["fps"],
                    sysmon=sysmon_metrics,
                ))
            finally:
                try:
                    renderer.cleanup()
                except Exception:
                    pass

    glh.reset_texture_cache()
    return results


def _generate_monster_apocalypse(yield_hook=None):
    """Generate the optional worst-case Fio stress world.

    This intentionally goes beyond the editor's normal UI limits.  The point
    is not to represent a sensible game level; it is to find the point where
    the complete engine becomes overloaded.
    """
    data = _generate_procedural_map(monsters=1000, relay_count=1000, seed=BENCHMARK_MAP_SEED, yield_hook=yield_hook)

    # Push the procedural world to its maximum generator dimensions/room
    # complexity, while keeping geometry creation in the production generator.
    # create_map_data() itself accepts these values even though the editor UI
    # deliberately exposes smaller monster counts and room-size controls.
    import random
    random.seed(0xF10)
    params = {
        "world_width": 4096,
        "world_height": 4096,
        "min_room": 256,
        "max_room": 640,
        "room_count": 24,
        "wall_tex": "default.png",
        "floor_tex": "default.png",
        "enable_floors": True,
        "floor_height": 512,
        "floor_room_count": 16,
        "spawn_monsters": True,
        "monster_count": 1000,
        "spawn_health": True,
        "health_count": 64,
    }
    data = create_map_data(params, yield_hook=yield_hook)

    # Add a large real I/O graph to the generated world.  The normal benchmark
    # materializer converts these to actual Thing/OutputConnection instances.
    things = data["things"]
    relay_start = len(things)
    for i in range(1000):
        if yield_hook is not None and i % 25 == 0:
            yield_hook()
        things.append({
            "type": "logicrelay",
            "pos": [128.0 + (i % 50) * 96.0, 32.0, 128.0 + (i // 50) * 96.0],
            "properties": {
                "type": "logicrelay",
                "name": "ApocalypseRelay_%d" % i,
                "id": "apocalypse_relay_%d" % i,
                "fire_once": False,
            },
            "io_connections": [],
        })

    # Chain plus local fan-out: one trigger exercises a long traversal while
    # each relay also addresses several nearby relays.
    for i in range(1000):
        if yield_hook is not None and i % 25 == 0:
            yield_hook()
        targets = [(i + 1) % 1000, (i + 7) % 1000, (i + 31) % 1000]
        data["things"][relay_start + i]["io_connections"] = [
            {
                "output": "OnTrigger",
                "target": "ApocalypseRelay_%d" % target,
                "target_id": "apocalypse_relay_%d" % target,
                "input": "Trigger",
                "parameter": "",
                "delay": 0.0,
                "fire_once": False,
            }
            for target in targets
            if target != i
        ]

    return data


def _run_monster_apocalypse():
    """Optional final test: deliberately overload the complete Fio stack."""
    data = _generate_monster_apocalypse()
    state = _materialize_generated_map(data)
    brushes, things = state.brushes, state.things

    results = []
    for mode, width, height in (
        ("windowed-sized", 1280, 720),
        ("fullscreen-sized", 1920, 1080),
    ):
        glh.reset_texture_cache()
        with glh.GLTestContext(width, height) as context:
            renderer = glh.make_renderer()
            try:
                # Very short warmup: this is explicitly a stress-to-failure
                # test, not a polished benchmark workload.
                samples, sysmon_metrics = _render_sample_set(
                    renderer, context, brushes, things, warmup=1, samples=10
                )
                mean = statistics.fmean(samples)
                results.append(_timing_result(
                    "FINAL_MONSTER_APOCALYPSE_%s" % mode,
                    "MAX procedural world / 1000 monsters / 1000 relays / dense I/O",
                    samples,
                    mode=mode,
                    monster_count=1000,
                    brush_count=len(brushes),
                    entity_count=len(things),
                    resolution="%dx%d" % (width, height),
                    average_fps=sysmon_metrics["fps"],
                    sysmon=sysmon_metrics,
                ))
            finally:
                try:
                    renderer.cleanup()
                except Exception:
                    pass

    # Do not create a second MainWindow here.  This benchmark is launched
    # from Tools > Benchmark, so opening a fresh Fio instance for Play Mode
    # would benchmark a different application instance than the one the user
    # started the test from.  The renderer stress above is the apocalypse
    # measurement; live Play Mode benchmarks belong to the existing MainWindow.

    glh.reset_texture_cache()
    return results


def run_live_renderer_sample(window, duration=1.0, warmup=0.75):
    """Measure the already-running Fio viewport through its real Qt event loop.

    No second MainWindow, QOpenGLWidget, OpenGL context, or Renderer_F is
    created. The existing viewport paints normally while the benchmark dialog
    is open, and SysMon records the frames that actually reached paintGL().    """
    from PyQt5.QtWidgets import QApplication

    view = window.view_3d
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("Fio QApplication is not running")

    view.sysmon.reset_metrics()
    warmup_deadline = time.perf_counter() + float(warmup)
    while time.perf_counter() < warmup_deadline:
        view.update()
        app.processEvents()
        time.sleep(0.001)

    view.sysmon.reset_metrics()
    start = time.perf_counter()
    deadline = start + float(duration)
    while time.perf_counter() < deadline:
        view.update()
        app.processEvents()
        time.sleep(0.001)

    # Let the final queued paint reach the same SysMon instance before reading it.
    view.update()
    app.processEvents()
    metrics = view.sysmon.get_metrics()
    metrics["viewport_width"] = int(view.width())
    metrics["viewport_height"] = int(view.height())
    metrics["wall_time_s"] = time.perf_counter() - start
    return metrics


def load_live_benchmark_world(window, data, yield_hook=None):
    """Load benchmark content into the existing Fio editor/runtime state cooperatively."""
    window.state.load_from_data(
        data,
        yield_hook=yield_hook,
        save_undo=False,
    )
    if yield_hook is not None:
        yield_hook()

    window.update_all_ui()
    if yield_hook is not None:
        yield_hook()

    window.update_views()
    if yield_hook is not None:
        yield_hook()

    window.view_3d.update()
    window.view_top.update()
    window.view_side.update()
    window.view_front.update()
    if yield_hook is not None:
        yield_hook()



def make_monster_chaos_witness_world(seed="43", monster_count=50, yield_hook=None):
    """Build the deterministic single-room world used by the live chaos witness."""
    import random

    random.seed(seed)
    monster_count = int(monster_count)
    if monster_count != 50:
        raise ValueError("chaos witness requires exactly 50 monsters")

    params = {
        "world_width": 2048,
        "world_height": 2048,
        "min_room": 384,
        "max_room": 512,
        "room_count": 1,
        "wall_tex": "default.png",
        "floor_tex": "default.png",
        "enable_floors": False,
        "floor_height": 128,
        "floor_room_count": 0,
        "spawn_monsters": True,
        "monster_count": monster_count,
        "spawn_health": False,
    }
    data = create_map_data(params, yield_hook=yield_hook)

    player_start = next(
        (
            thing for thing in data.get("things", [])
            if str(thing.get("type", "")).lower() == "playerstart"
        ),
        None,
    )
    if player_start is None:
        raise RuntimeError("chaos witness procedural map has no PlayerStart")

    px, py, pz = [float(v) for v in player_start.get("pos", [0.0, 96.0, 0.0])]

    pathnode_name = "ChaosPathNode"
    data["things"].append({
        "type": "path_node",
        "pos": [px, py, pz],
        "properties": {
            "type": "path_node",
            "name": pathnode_name,
            "id": "chaos_pathnode",
            "radius": 64.0,
            "show_radius": True,
            "affects_type": "both",
            "next_node": "",
            "wait_time": 0.0,
            "speed": 1.0,
        },
        "io_connections": [],
    })

    monsters = [
        thing for thing in data.get("things", [])
        if str(thing.get("type", "")).lower() == "monster"
    ]
    if len(monsters) != monster_count:
        raise RuntimeError(
            "chaos witness generated %d monsters, expected %d"
            % (len(monsters), monster_count)
        )

    # Two hostile mixed teams: 15 human + 10 flying on each side.
    monster_specs = (
        [("benchmark_red", "human")] * 15
        + [("benchmark_blue", "human")] * 15
        + [("benchmark_red", "flying")] * 10
        + [("benchmark_blue", "flying")] * 10
    )
    rng = random.Random(seed)
    rng.shuffle(monster_specs)

    # Randomly use the base sprite set or the available alternate skin.
    variants = ("<None>", "variant1")

    # Put the teams on opposite sides of the single room. The PathNode sits
    # at the room centre, so both sides converge on the same destination.
    team_positions = {
        "benchmark_red": [],
        "benchmark_blue": [],
    }
    for row in range(25):
        z_offset = ((row % 5) - 2) * 32.0 + rng.uniform(-8.0, 8.0)
        x_offset = (row // 5) * 5.0 + rng.uniform(-6.0, 6.0)
        team_positions["benchmark_red"].append(
            [px - 105.0 + x_offset, z_offset]
        )
        team_positions["benchmark_blue"].append(
            [px + 105.0 - x_offset, z_offset]
        )

    team_indices = {"benchmark_red": 0, "benchmark_blue": 0}
    for index, monster in enumerate(monsters):
        team, monster_type = monster_specs[index]
        position_index = team_indices[team]
        team_indices[team] += 1
        x_offset, z_offset = team_positions[team][position_index]

        monster["pos"] = [
            x_offset,
            py + (32.0 if monster_type == "flying" else 0.0),
            pz + z_offset,
        ]
        props = monster.setdefault("properties", {})
        props.update({
            "monster_type": monster_type,
            "health": 120,
            "damage": 12,
            "awake": True,
            "wake_on_sight": True,
            "dead": False,
            "team": team,
            "variant": rng.choice(variants),
            "patrol": True,
            "patrol_target": pathnode_name,
            "patrol_mode": "once",
            "target_name": pathnode_name,
        })

        if yield_hook is not None and index % 8 == 0:
            yield_hook()

    if yield_hook is not None:
        yield_hook()

    return data, {
        "seed": str(seed),
        "monster_count": monster_count,
        "human_count": sum(1 for _, mtype in monster_specs if mtype == "human"),
        "flying_count": sum(1 for _, mtype in monster_specs if mtype == "flying"),
        "team_counts": {
            team: sum(1 for monster_team, _ in monster_specs if monster_team == team)
            for team in ("benchmark_red", "benchmark_blue")
        },
        "pathnode_name": pathnode_name,
        "pathnode_pos": [px, py, pz],
    }

def prepare_live_monster_test(window, aggro_fraction=0.25, yield_hook=None):
    """Enter real Play Mode and configure a representative monster combat load.

    Monster AI remains fully live: teams make monsters acquire opposing
    monsters, a subset receives explicit runtime aggro, and the player is put
    in god mode so the benchmark can run without the player dying.  The
    generated monsters also retain their normal OnAttack/OnDamaged/OnDeath I/O.
    """
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import Qt

    app = QApplication.instance()
    if app is None:
        raise RuntimeError("Fio QApplication is not running")

    view = window.view_3d
    if view.play_mode:
        window._exit_play_mode()
        app.processEvents()

    window.enter_play_mode()
    if not view.play_mode:
        raise RuntimeError("Fio failed to enter Play Mode for monster benchmark")

    logic = getattr(view, "logic_thread", None)
    if logic is None:
        window._exit_play_mode()
        raise RuntimeError("Fio Play Mode has no LogicThread")

    # God mode protects the benchmark player without disabling monster AI.
    logic.god_mode = True

    monsters = [
        t for t in window.state.things
        if str(t.properties.get("type", "")).lower() == "monster"
    ]
    if not monsters:
        window._exit_play_mode()
        raise RuntimeError("monster benchmark generated no Monster entities")

    # Split monsters into two opposing factions.  The existing production AI
    # already understands team-based enemy targeting and infighting.
    for index, monster in enumerate(monsters):
        monster.properties["team"] = "benchmark_red" if index % 2 == 0 else "benchmark_blue"
        monster.properties["awake"] = True
        monster.properties["wake_on_sight"] = True
        monster.properties["dead"] = False
        if yield_hook is not None and index % 25 == 0:
            yield_hook()

    # Seed explicit aggro on a subset so infighting starts immediately rather
    # than depending entirely on the player wandering into every sight cone.
    seed_count = max(2, int(len(monsters) * float(aggro_fraction)))
    seed_count = min(seed_count, len(monsters))
    for index in range(seed_count):
        source = monsters[index]
        source_team = source.properties["team"]
        sx, sy, sz = [float(v) for v in source.pos]
        candidates = []
        for target in monsters:
            if target is source or target.properties.get("dead", False):
                continue
            if target.properties.get("team") == source_team:
                continue
            tx, ty, tz = [float(v) for v in target.pos]
            dx, dy, dz = sx - tx, sy - ty, sz - tz
            candidates.append((dx * dx + dy * dy + dz * dz, target))
        if candidates:
            candidates.sort(key=lambda item: item[0])
            source.properties["_aggro_target"] = id(candidates[0][1])
        if yield_hook is not None and index % 10 == 0:
            yield_hook()

    if yield_hook is not None:
        yield_hook()

    # Keep the player moving through the encounter while god mode prevents
    # combat from ending the measurement.
    window.keys_pressed.add(Qt.Key_W)
    window.keys_pressed.add(Qt.Key_D)

    return {
        "monster_count": len(monsters),
        "aggro_seeded": sum(
            1 for m in monsters if m.properties.get("_aggro_target") is not None
        ),
        "god_mode": True,
        "teams": 2,
    }


def finish_live_monster_test(window):
    """Stop benchmark player input and leave real Play Mode cleanly."""
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import Qt

    window.keys_pressed.discard(Qt.Key_W)
    window.keys_pressed.discard(Qt.Key_D)
    if window.view_3d.play_mode:
        window._exit_play_mode()
        QApplication.processEvents()


def run_live_play_sample(window, data, seconds=2.0):
    """Run real Play Mode in the existing Fio window and measure SysMon."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication

    load_live_benchmark_world(window, data)
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("Fio QApplication is not running")

    if window.view_3d.play_mode:
        window._exit_play_mode()
        app.processEvents()

    window.enter_play_mode()
    if not window.view_3d.play_mode:
        raise RuntimeError("Fio failed to enter Play Mode")

    window.keys_pressed.add(Qt.Key_W)
    window.keys_pressed.add(Qt.Key_D)
    try:
        metrics = run_live_renderer_sample(
            window,
            duration=seconds,
            warmup=0.25,
        )
    finally:
        window.keys_pressed.discard(Qt.Key_W)
        window.keys_pressed.discard(Qt.Key_D)

    final_pos = getattr(window.view_3d.camera, "pos", None)
    if final_pos is not None:
        final_pos = (
            float(final_pos.x),
            float(final_pos.y),
            float(final_pos.z),
        )
    metrics["final_camera_pos"] = final_pos
    window._exit_play_mode()
    app.processEvents()
    return metrics


def run_live_benchmark_world(window, data, label, duration=1.0):
    """Load one world into the live Fio instance and benchmark its renderer."""
    load_live_benchmark_world(window, data)
    metrics = run_live_renderer_sample(window, duration=duration)
    metrics["label"] = label
    metrics["brush_count"] = len(window.state.brushes)
    metrics["entity_count"] = len(window.state.things)
    return metrics


def _selected_monster_counts():
    raw = os.environ.get("FIO_FULLSCREEN_BENCH_MONSTERS", "")
    if not raw:
        return ()
    allowed = {100, 500, 1000}
    counts = []
    for item in raw.split(","):
        if not item.strip():
            continue
        count = int(item.strip())
        if count not in allowed:
            raise ValueError("unsupported monster stress size: %s" % count)
        if count not in counts:
            counts.append(count)
    return tuple(counts)

def _render_sample_set(renderer, context, brushes, things, warmup, samples):
    """Render frames and collect the same metrics exposed by Fio SysMon."""
    from engine.sysmon import SysMon

    sysmon = SysMon(None)

    for _ in range(warmup):
        _render(renderer, context, brushes, things)

    timings = []
    for _ in range(samples):
        start = time.perf_counter()
        _render(renderer, context, brushes, things)
        elapsed = time.perf_counter() - start
        timings.append(elapsed)
        sysmon.record_frame_time(elapsed * 1000.0)

    mean = statistics.fmean(timings)
    sysmon.record_fps(1.0 / mean if mean else 0.0)

    render_stats = getattr(renderer, "render_stats", None)
    if render_stats is not None:
        sysmon.update_stats(
            visible_brushes=getattr(render_stats, "visible_brushes", 0),
            culled_brushes=getattr(render_stats, "culled_brushes", 0),
            total_brushes=getattr(render_stats, "total_brushes", len(brushes)),
        )
        sysmon.stats["visible_tris"] = int(
            getattr(render_stats, "visible_tris", 0)
        )
        sysmon.stats["culled_tris"] = int(
            getattr(render_stats, "culled_tris", 0)
        )

    # The GL context is still current here, so SysMon's native OpenGL VRAM
    # query measures the actual renderer used for this benchmark.
    return timings, sysmon.get_metrics()
def _timing_result(test, description, samples, **extra):
    mean = statistics.fmean(samples)
    p95 = sorted(samples)[
        min(
            len(samples) - 1,
            int(round(0.95 * (len(samples) - 1))),
        )
    ]
    result = {
        "test": test,
        "description": description,
        "runs": len(samples),
        "mean_ms": mean * 1000.0,
        "p95_ms": p95 * 1000.0,
        "worst_ms": max(samples) * 1000.0,
    }
    result.update(extra)

    # When SysMon data is supplied, it is the authoritative renderer result.
    # The raw stopwatch timings remain useful for diagnostics, but the reported
    # FPS/frame-time values come from the same metric object Fio displays.
    metrics = result.get("sysmon")
    if metrics:
        result["average_fps"] = metrics.get("fps", result.get("average_fps"))
        result["mean_ms"] = metrics.get(
            "average_frame_time_ms", result["mean_ms"]
        )
        result["p95_ms"] = metrics.get("p95_frame_time_ms", result["p95_ms"])

    return result


def _exercise_real_play_mode(data, seconds=0.75):
    """Load generated content into MainWindow, enter real play mode, and move."""
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QApplication
    from editor.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    window = MainWindow(root_dir)
    window.state.load_from_data(data)
    window.update_all_ui()
    window.show()
    app.processEvents()

    window.enter_play_mode()
    if not window.view_3d.play_mode:
        window.close()
        raise RuntimeError("Fio failed to enter play mode in benchmark")

    window.keys_pressed.add(Qt.Key_W)
    window.keys_pressed.add(Qt.Key_D)
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        window.view_3d.update_loop()
        app.processEvents()
        time.sleep(0.005)
    window.keys_pressed.discard(Qt.Key_W)
    window.keys_pressed.discard(Qt.Key_D)

    final_pos = getattr(window.view_3d.camera, "pos", None)
    if final_pos is not None:
        final_pos = (float(final_pos.x), float(final_pos.y), float(final_pos.z))

    window._exit_play_mode()
    window.close()
    app.processEvents()
    return final_pos


def _run_play_mode_stress():
    data = _generate_procedural_map(monsters=100, relay_count=32, seed=4242)
    start = time.perf_counter()
    final_pos = _exercise_real_play_mode(data)
    elapsed = time.perf_counter() - start
    return {
        "test": "play_mode_camera",
        "description": "procedural map -> real Fio state -> Play Mode -> WASD camera/player movement",
        "runs": 1,
        "mean_ms": elapsed * 1000.0,
        "p95_ms": elapsed * 1000.0,
        "worst_ms": elapsed * 1000.0,
        "entities": len(data["things"]),
        "brushes": len(data["brushes"]),
        "final_camera_pos": final_pos,
    }


def _run_renderer_stress():
    brushes, things = _make_renderer_stress_scene()
    results = []

    for mode, width, height in (
        ("windowed-sized", 1280, 720),
        ("fullscreen-sized", 1920, 1080),
    ):
        glh.reset_texture_cache()
        with glh.GLTestContext(width, height) as context:
            renderer = glh.make_renderer()
            try:
                samples, sysmon_metrics = _render_sample_set(
                    renderer,
                    context,
                    brushes,
                    things,
                    warmup=5,
                    samples=20,
                )
                mean = statistics.fmean(samples)
                results.append(
                    _timing_result(
                        "renderer_stress_%s" % mode,
                        "%d brushes + %d lights (%d shadowed)"
                        % (
                            len(brushes),
                            len(things),
                            sum(
                                1
                                for t in things
                                if t.properties.get("casts_shadows")
                            ),
                        ),
                        samples,
                        mode=mode,
                        brush_count=len(brushes),
                        resolution="%dx%d" % (width, height),
                        average_fps=sysmon_metrics["fps"],
                        sysmon=sysmon_metrics,
                    )
                )
            finally:
                try:
                    renderer.cleanup()
                except Exception:
                    pass

    glh.reset_texture_cache()
    return results


def _run_brush_count_stress(counts):
    """Measure the real renderer against 1K/10K/100K normal Fio brushes."""
    results = []

    for brush_count in counts:
        glh.reset_texture_cache()
        brushes, things = _make_brush_stress_scene(brush_count)

        for mode, width, height in (
            ("windowed-sized", 1280, 720),
            ("fullscreen-sized", 1920, 1080),
        ):
            with glh.GLTestContext(width, height) as context:
                renderer = glh.make_renderer()
                try:
                    samples, sysmon_metrics = _render_sample_set(
                        renderer,
                        context,
                        brushes,
                        things,
                        warmup=3,
                        samples=10,
                    )
                    mean = statistics.fmean(samples)
                    results.append(
                        _timing_result(
                            "brush_scene_%d_%s" % (brush_count, mode),                            "%d normal Fio brushes + %d lights (%d shadowed)"
                            % (
                                brush_count,
                                len(things),
                                sum(
                                    1
                                    for t in things
                                    if t.properties.get("casts_shadows")
                                ),
                            ),
                            samples,
                            mode=mode,
                            brush_count=brush_count,
                            resolution="%dx%d" % (width, height),
                            average_fps=sysmon_metrics["fps"],
                            sysmon=sysmon_metrics,
                        )
                    )
                finally:
                    try:
                        renderer.cleanup()
                    except Exception:
                        pass

        del brushes, things

    glh.reset_texture_cache()
    return results


def _selected_brush_counts():
    raw = os.environ.get("FIO_FULLSCREEN_BENCH_BRUSH_STRESS", "")
    if not raw:
        return ()

    allowed = {1000, 10000, 100000}
    counts = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        count = int(item)
        if count not in allowed:
            raise ValueError(
                "unsupported brush stress size: %s" % count
            )
        if count not in counts:
            counts.append(count)
    return tuple(counts)


def _make_io_chain(entity_count):
    """Build a production LogicRelay chain with UUID-addressed connections."""
    from editor import io_system as io
    from editor.io_system import IOManager, OutputConnection
    from editor.io_handlers import register_all_input_handlers
    from editor.things import LogicRelay

    manager = IOManager()
    logic = type("BenchmarkLogic", (), {})()
    logic.io_manager = manager
    manager.set_logic_thread(logic)
    register_all_input_handlers(manager)

    entities = [
        LogicRelay(
            pos=[0, 0, 0],
            properties={
                "name": "benchmark_relay_%d" % i,
                "fire_once": False,
            },
        )
        for i in range(entity_count)
    ]

    by_name = {e.properties["name"]: e for e in entities}
    by_id = {e.properties.get("id"): e for e in entities}
    manager.set_entity_finder(lambda name: by_name.get(name))
    manager.set_entity_finder_by_id(lambda entity_id: by_id.get(entity_id))

    for i in range(entity_count - 1):
        io.add_connection(
            entities[i],
            OutputConnection(
                output_name="OnTrigger",
                target_name=entities[i + 1].properties["name"],
                input_name="Trigger",
                target_id=entities[i + 1].properties.get("id"),
            ),
        )

    return manager, entities


def _run_io_stress():
    """Run the production I/O dispatcher through 699 real relay hops."""
    manager, entities = _make_io_chain(700)
    samples = []

    for _ in range(20):
        manager.reset()
        start = time.perf_counter()
        manager.fire_output(entities[0], "OnTrigger")
        samples.append(time.perf_counter() - start)

    mean = statistics.fmean(samples)
    return _timing_result(
        "io_stress",
        "700 LogicRelay entities / 699 production I/O hops",
        samples,
        entities=700,
        hops=699,
        hops_per_second=699.0 / mean if mean else float("inf"),
    )


def _run_io_chain_stress(entity_count=1000):
    """Run a production I/O chain containing exactly N entities."""
    manager, entities = _make_io_chain(entity_count)

    samples = []
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old_limit, entity_count * 4))
    try:
        for _ in range(10):
            manager.reset()
            start = time.perf_counter()
            manager.fire_output(entities[0], "OnTrigger")
            samples.append(time.perf_counter() - start)
    finally:
        sys.setrecursionlimit(old_limit)

    hops = entity_count - 1
    mean = statistics.fmean(samples)
    return _timing_result(
        "io_chain_%d" % entity_count,
        "%d LogicRelay entities / %d production I/O hops"
        % (entity_count, hops),
        samples,
        entities=entity_count,
        hops=hops,
        hops_per_second=hops / mean if mean else float("inf"),
    )


def _selected_io_chain_counts():
    raw = os.environ.get("FIO_FULLSCREEN_BENCH_IO_CHAIN", "")
    if not raw:
        return ()

    counts = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        count = int(item)
        if count != 1000:
            raise ValueError(
                "unsupported I/O chain size: %s" % count
            )
        if count not in counts:
            counts.append(count)
    return tuple(counts)


def _make_csg_brush(index):
    """Create a real Fio brush and apply six production clip operations."""
    from engine import brush_geometry as bg

    brush = box_brush(
        "csg_benchmark_%d" % index,
        (0, 0, 0),
        (256, 256, 256),
    )

    cuts = (
        ((1, 1, 0), 32.0),
        ((-1, 1, 0), 32.0),
        ((0, 1, 1), 32.0),
        ((0, 1, -1), 32.0),
        ((1, 0, 1), 32.0),
        ((-1, 0, 1), 32.0),
    )

    for normal, offset in cuts:
        if not bg.clip_brush(brush, normal, offset):            raise RuntimeError(
                "production clip_brush rejected CSG cut %r" % (normal,)
            )

    return brush


def _run_csg_stress():
    """Time production brush clipping, geometry rebuild and bounds sync."""
    samples = []

    for run in range(20):
        start = time.perf_counter()
        for index in range(20):
            _make_csg_brush(run * 20 + index)
        samples.append(time.perf_counter() - start)

    operations = 20 * 6
    mean = statistics.fmean(samples)
    return _timing_result(
        "csg_stress",
        "20 brushes x 6 production clip_brush operations x 20 runs",
        samples,
        brushes_per_run=20,
        clip_operations_per_brush=6,
        clip_operations_per_run=operations,
        clip_operations_per_second=operations / mean if mean else float("inf"),
    )


def run_additional_stress_tests():
    results = [_run_renderer_stress(), _run_io_stress(), _run_csg_stress(), _run_play_mode_stress()]

    brush_counts = _selected_brush_counts()
    if brush_counts:
        results.extend(_run_brush_count_stress(brush_counts))

    for count in _selected_io_chain_counts():
        results.append(_run_io_chain_stress(count))

    for count in _selected_monster_counts():
        results.extend(_run_monster_stress(count))

    if os.environ.get("FIO_FULLSCREEN_BENCH_APOCALYPSE") == "1":
        results.extend(_run_monster_apocalypse())

    return results


def run_benchmark(additional_tests=False):
    results = []

    for width, height in _resolutions():
        for name, shadows, empty in SCENARIOS:
            results.append(
                _measure_scenario(
                    width,
                    height,
                    name,
                    shadows,
                    empty,
                )
            )

    if additional_tests:
        results.extend(run_additional_stress_tests())

    return results


def _format_sysmon_vram(metrics):
    if not metrics:
        return "N/A"
    used = metrics.get("vram_used_mb")
    total = metrics.get("vram_total_mb")
    if used is None and total is None:
        return "N/A"
    if used is None:
        return "%sMB free" % total
    if total is None:
        return "%sMB" % used
    return "%s/%sMB" % (used, total)


def format_results(results, info=None):
    lines = [
        "Fio performance benchmark",
        _execution_environment(),
        "",
        "Renderer path: engine.renderer_F.Renderer_F.render_scene",
        "World data: procedural Fio map generator -> real brushes + Thing entities",
        "Gameplay data: 32 LogicRelay entities linked by serialized UUID I/O",
        "Monster stress: procedural generator creates real Monster entities",
        "GPU synchronization: glFinish() per measured frame",
        "Performance metrics: Fio SysMon (frame time, FPS, OpenGL VRAM)",
        "",
        "scenario                     resolution       FPS     frame ms  p95 ms   VRAM",
    ]

    for result in results:
        if "scenario" not in result:
            continue

        lines.append(
            "%-28s %4dx%-4d %8.1f %10.2f %8.2f %12s"
            % (
                result["scenario"],
                result["width"],
                result["height"],
                result["average_fps"],
                result["mean_ms"],
                result["p95_ms"],
                _format_sysmon_vram(result.get("sysmon")),
            )
        )

    stress_results = [r for r in results if "test" in r]
    if stress_results:
        lines.extend(["", "Additional stress tests:"])

        for result in stress_results:
            lines.append(
                "  %-30s %-55s mean %8.2f ms  p95 %8.2f ms  worst %8.2f ms"
                % (
                    result["test"],
                    result["description"],
                    result["mean_ms"],
                    result["p95_ms"],
                    result["worst_ms"],
                )
            )

            if "final_camera_pos" in result:
                lines.append(
                    "    play mode camera final position: %s"
                    % (result["final_camera_pos"],)
                )
            if "average_fps" in result:
                lines.append(
                    "    Fio SysMon FPS: %.1f" % result["average_fps"]
                )
            if "sysmon" in result:
                metrics = result["sysmon"]
                lines.append(
                    "    Fio SysMon frame time: %.2f ms (p95 %.2f ms)"
                    % (
                        metrics.get("average_frame_time_ms", 0.0),
                        metrics.get("p95_frame_time_ms", 0.0),
                    )
                )
                lines.append(
                    "    Fio SysMon VRAM: %s"
                    % _format_sysmon_vram(metrics)
                )
            if "hops_per_second" in result:
                lines.append(
                    "    I/O throughput: %.0f hops/s"
                    % result["hops_per_second"]
                )
            if "clip_operations_per_second" in result:
                lines.append(
                    "    CSG throughput: %.0f clip operations/s"
                    % result["clip_operations_per_second"]
                )

    baselines = {
        result["scenario"]: result
        for result in results
        if (
            "scenario" in result
            and result["width"] == 192
            and result["height"] == 192
        )
    }

    lines.extend([
        "",
        "Resolution scaling relative to 192x192:",
    ])

    for result in results:
        if result.get("scenario") not in baselines:
            continue
        if result["width"] == 192 and result["height"] == 192:
            continue

        base = baselines[result["scenario"]]
        lines.append(
            "  %-28s %4dx%-4d  pixels x%7.2f  frame-time x%7.2f"
            % (
                result["scenario"],
                result["width"],
                result["height"],
                result["pixels"] / float(base["pixels"]),
                result["mean_ms"] / max(base["mean_ms"], 1e-9),
            )
        )

    if info:
        lines.extend([
            "",
            "GL renderer: %s" % info.get("renderer", "?"),
            "GL version:  %s" % info.get("version", "?"),
        ])

    return "\n".join(lines)


def _run_worker_test(label):
    """Run exactly one risky benchmark in an isolated worker process."""
    if label == "procedural_100_monsters":
        return _run_monster_stress(100)
    if label == "procedural_500_monsters":
        return _run_monster_stress(500)
    if label == "procedural_1000_monsters":
        return _run_monster_stress(1000)
    if label == "live_io_1000":
        return [_run_io_chain_stress(1000)]
    if label == "live_1000_brushes":
        return _run_brush_count_stress((1000,))
    if label == "live_10000_brushes":
        return _run_brush_count_stress((10000,))
    if label == "live_100000_brushes":
        return _run_brush_count_stress((100000,))
    if label == "monster_apocalypse":
        return _run_monster_apocalypse()
    raise ValueError("unsupported isolated benchmark worker: %s" % label)


def _write_worker_result(path, payload):
    """Atomically publish worker results so the parent never reads a partial JSON file."""
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(temporary, path)


def _run_worker_main(label, output_path):
    payload = {
        "format": "fio-benchmark-worker-v1",
        "ok": False,
        "label": label,
    }
    try:
        payload["results"] = _run_worker_test(label)
        payload["ok"] = True
    except BaseException:
        # The parent process can surface this as a failed/aborted individual
        # test while continuing the rest of the benchmark queue.
        payload["error"] = traceback.format_exc()
    _write_worker_result(output_path, payload)
    return 0 if payload["ok"] else 1


def main():
    worker_label = os.environ.get("FIO_FULLSCREEN_BENCH_WORKER_TEST")
    worker_output = os.environ.get("FIO_FULLSCREEN_BENCH_WORKER_OUT")
    if worker_label:
        if not worker_output:
            raise RuntimeError("isolated benchmark worker has no output path")
        raise SystemExit(_run_worker_main(worker_label, worker_output))

    additional_tests = (
        os.environ.get("FIO_FULLSCREEN_BENCH_ADDITIONAL") == "1"
    )

    # Probe the real GL context before running the benchmark.  This also gives
    # the dialog useful GPU information without relying on pytest collection.
    with glh.GLTestContext(64, 64) as probe:
        info = probe.info()

    results = run_benchmark(additional_tests=additional_tests)
    output = format_results(results, info)
    print("\n" + output)

    output_path = os.environ.get("FIO_FULLSCREEN_BENCH_OUT")
    if output_path:
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "execution_environment": _execution_environment(),
                    "gpu": info.get("renderer", "?"),
                    "gl_version": info.get("version", "?"),
                    "warmup_frames": WARMUP_FRAMES,
                    "measured_frames": MEASURED_FRAMES,
                    "additional_tests": additional_tests,
                    "results": results,
                },
                handle,
                indent=2,
            )
        print("\nWritten to %s" % output_path)

    if not results:
        raise RuntimeError("no benchmark resolutions configured")


if __name__ == "__main__":
    main()