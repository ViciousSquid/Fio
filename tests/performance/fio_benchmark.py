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

from tests.helpers import gl as glh
from tests.helpers.worlds import box_brush, make_thing
from editor.procedural_generator import create_map_data


WARMUP_FRAMES = 10
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
    """Render one real Fio frame and synchronise GPU completion."""
    import OpenGL.GL as gl

    aspect = float(context.width) / float(max(1, context.height))
    projection, view, eye = glh.camera_matrices(aspect=aspect)

    config = glh.render_config(
        all_brushes=brushes,
        all_things=things,
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

            for _ in range(WARMUP_FRAMES):
                _render(renderer, context, brushes, things)

            samples = []
            for _ in range(MEASURED_FRAMES):
                start = time.perf_counter()
                _render(renderer, context, brushes, things)
                samples.append(time.perf_counter() - start)

            mean = statistics.fmean(samples)
            p95 = sorted(samples)[
                min(
                    len(samples) - 1,
                    int(round(0.95 * (len(samples) - 1))),
                )
            ]
            pixels = width * height
            megapixels = pixels / 1_000_000.0

            return {
                "scenario": name,
                "width": width,
                "height": height,
                "pixels": pixels,
                "megapixels": megapixels,
                "mean_ms": mean * 1000.0,
                "p95_ms": p95 * 1000.0,
                "worst_ms": max(samples) * 1000.0,
                "average_fps": 1.0 / mean if mean else float("inf"),
                "ms_per_megapixel": (
                    mean * 1000.0 / megapixels
                    if megapixels
                    else float("inf")
                ),
            }
        finally:
            try:
                renderer.cleanup()
            except Exception:
                pass

    glh.reset_texture_cache()


def _generate_procedural_map(monsters=0, relay_count=32, seed=1337):
    """Generate a real Fio map using the same procedural generator as the editor."""
    import random

    random.seed(seed)
    params = {
        "world_width": 4096,
        "world_height": 4096,
        "min_room": 256,
        "max_room": 640,
        "room_count": 18,
        "wall_tex": "wall.jpg",
        "floor_tex": "floor.jpg",
        "enable_floors": True,
        "floor_height": 128,
        "floor_room_count": 3,
        "spawn_monsters": monsters > 0,
        "monster_count": monsters,
        "spawn_health": False,
    }
    data = create_map_data(params)

    # Add actual Fio LogicRelay entities and serialized I/O links to the
    # generated map. These are consumed by the normal map loader.
    things = data["things"]
    relay_start = len(things)
    for i in range(relay_count):
        things.append({
            "type": "logicrelay",
            "pos": [128.0 + i * 48.0, 32.0, 128.0],
            "properties": {
                "type": "logicrelay",
                "name": "BenchmarkRelay_%d" % i,
                "id": "benchmark_relay_%d" % i,
                "fire_once": False,
                "_io_connections": [],
            },
            "io_connections": [],
        })

    for i in range(relay_count - 1):
        things[relay_start + i]["properties"]["_io_connections"] = [{
            "output": "OnTrigger",
            "target": "BenchmarkRelay_%d" % (i + 1),
            "target_id": "benchmark_relay_%d" % (i + 1),
            "input": "Trigger",
            "parameter": "",
            "delay": 0.0,
            "fire_once": False,
        }]

    return data


def _materialize_generated_map(data):
    """Turn generated JSON-shaped data into the real Fio EditorState objects."""
    from editor.editor_state import EditorState

    state = EditorState()
    state.load_from_data(data)
    return state


def _make_renderer_stress_scene():
    data = _generate_procedural_map(monsters=0, relay_count=32)
    state = _materialize_generated_map(data)
    return state.brushes, state.things


def _make_brush_stress_scene(brush_count):
    """Create a large real-Fio brush scene starting from procedural geometry."""
    data = _generate_procedural_map(monsters=0, relay_count=32)
    state = _materialize_generated_map(data)
    source = state.brushes
    if len(source) >= brush_count:
        return source[:brush_count], state.things

    brushes = list(source)
    index = 0
    while len(brushes) < brush_count:
        original = dict(source[index % len(source)])
        original["pos"] = list(original.get("pos", [0, 0, 0]))
        original["pos"][0] += (index // len(source) + 1) * 5000.0
        original["id"] = "benchmark_generated_%d" % len(brushes)
        brushes.append(original)
        index += 1
    return brushes, state.things


def _run_monster_stress(count):
    """Generate a real procedural Fio room populated with N monsters."""
    data = _generate_procedural_map(
        monsters=count, relay_count=32, seed=1337 + count
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
                samples = _render_sample_set(
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
                    average_fps=1.0 / mean if mean else float("inf"),
                ))
            finally:
                try:
                    renderer.cleanup()
                except Exception:
                    pass

    glh.reset_texture_cache()
    return results


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
    for _ in range(warmup):
        _render(renderer, context, brushes, things)

    timings = []
    for _ in range(samples):
        start = time.perf_counter()
        _render(renderer, context, brushes, things)
        timings.append(time.perf_counter() - start)
    return timings


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
                samples = _render_sample_set(
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
                        average_fps=1.0 / mean if mean else float("inf"),
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
                    samples = _render_sample_set(
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
                            "brush_scene_%d_%s" % (brush_count, mode),
                            "%d normal Fio brushes + %d lights (%d shadowed)"
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
                            average_fps=(
                                1.0 / mean if mean else float("inf")
                            ),
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
        if not bg.clip_brush(brush, normal, offset):
            raise RuntimeError(
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
        "",
        "scenario                     resolution       FPS     mean ms   p95 ms   ms/MP",
    ]

    for result in results:
        if "scenario" not in result:
            continue

        lines.append(
            "%-28s %4dx%-4d %8.1f %10.2f %8.2f %8.3f"
            % (
                result["scenario"],
                result["width"],
                result["height"],
                result["average_fps"],
                result["mean_ms"],
                result["p95_ms"],
                result["ms_per_megapixel"],
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
                    "    renderer FPS: %.1f" % result["average_fps"]
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


def main():
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
