"""Renderer benchmark scenarios derived directly from the visual test suite.

The benchmark imports the visual suite's _render helper and uses the same
lit_cube_scene, renderer configuration and GL path as
tests/visual/test_lit_scene.py. This makes the numbers comparable to the
renderer tests rather than measuring a synthetic scene.

Run:
    python -m pytest tests/performance/test_fullscreen_resolution_benchmark.py --run-benchmarks -s
"""

import json
import os
import statistics
import sys
import time

import pytest

from tests.helpers import gl as glh
from tests.visual.test_lit_scene import _render

pytestmark = [pytest.mark.gl, pytest.mark.benchmark, pytest.mark.slow]

WARMUP_FRAMES = 10
MEASURED_FRAMES = 30

DEFAULT_RESOLUTIONS = (
    (192, 192),       # exact framebuffer size used by test_lit_scene.py
    (1280, 720),
    (1600, 900),
    (1920, 1080),
    (2560, 1440),
    (2880, 1920),
)


def _execution_environment():
    """Return the Python process and host architecture/translation mode."""
    import platform
    process_arch = platform.machine() or "unknown"
    host_arch = process_arch
    translation = "none detected"

    if sys.platform == "win32":
        try:
            import ctypes
            process_machine = ctypes.c_ushort()
            native_machine = ctypes.c_ushort()
            fn = ctypes.windll.kernel32.IsWow64Process2
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ushort)]
            fn.restype = ctypes.c_bool
            if fn(ctypes.windll.kernel32.GetCurrentProcess(),
                  ctypes.byref(process_machine), ctypes.byref(native_machine)):
                names = {0x014C: "x86", 0x8664: "x64", 0xAA64: "ARM64"}
                process_arch = names.get(process_machine.value, "0x%04X" % process_machine.value)
                host_arch = names.get(native_machine.value, "0x%04X" % native_machine.value)
                if process_machine.value and process_machine.value != native_machine.value:
                    translation = "Microsoft Prism / Windows on ARM emulation"
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            import ctypes
            libc = ctypes.CDLL(None)
            translated = ctypes.c_int(0)
            size = ctypes.c_size_t(ctypes.sizeof(translated))
            if libc.sysctlbyname(b"sysctl.proc_translated", ctypes.byref(translated),
                                 ctypes.byref(size), None, 0) == 0 and translated.value == 1:
                translation = "Apple Rosetta 2"
                host_arch = "ARM64"
        except Exception:
            pass

    return "Python: %s | Host CPU: %s | Translation: %s" % (
        process_arch, host_arch, translation
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


SCENARIOS = (
    ("test_a_lit_cube_scene_renders_without_gl_errors", False, False),
    ("test_a_scene_with_shadows_renders_without_gl_errors", True, False),
    ("test_rendering_an_empty_world_is_harmless", False, True),
)


def _measure_scenario(width, height, name, shadows, empty):
    glh.reset_texture_cache()

    if empty:
        brushes, things = [], []
    else:
        brushes, things = glh.lit_cube_scene(shadows=shadows)

    with glh.GLTestContext(width, height) as context:
        renderer = glh.make_renderer()
        try:
            def frame():
                # _render is the exact helper used by test_lit_scene.py.
                _render(renderer, context, brushes, things)

            frame()
            for _ in range(WARMUP_FRAMES):
                frame()

            samples = []
            for _ in range(MEASURED_FRAMES):
                start = time.perf_counter()
                frame()
                samples.append(time.perf_counter() - start)

            mean = statistics.fmean(samples)
            p95 = sorted(samples)[
                min(len(samples) - 1, int(round(0.95 * (len(samples) - 1)))
            ]
            pixels = width * height
            return {
                "scenario": name,
                "width": width,
                "height": height,
                "pixels": pixels,
                "megapixels": pixels / 1_000_000.0,
                "mean_ms": mean * 1000.0,
                "p95_ms": p95 * 1000.0,
                "worst_ms": max(samples) * 1000.0,
                "average_fps": 1.0 / mean if mean else float("inf"),
                "ms_per_megapixel": mean * 1000.0 / (pixels / 1_000_000.0),
            }
        finally:
            try:
                renderer.cleanup()
            except Exception:
                pass

    glh.reset_texture_cache()



def _make_renderer_stress_scene():
    """Build the baseline dense renderer workload."""
    return _make_brush_stress_scene(576)


def _make_brush_stress_scene(brush_count):
    """Build a renderer stress scene containing exactly *brush_count* boxes."""
    from editor.things import Light
    from tests.helpers.worlds import box_brush, make_thing
    import math

    brushes = []
    columns = int(math.ceil(math.sqrt(brush_count)))
    spacing = 120
    half = columns // 2
    for index in range(brush_count):
        x = index % columns
        z = index // columns
        brushes.append(box_brush(
            "stress_brush_%d" % index,
            ((x - half) * spacing, 0, (z - half) * spacing),
            (96, 96, 96),
        ))

    things = []
    light_count = 32
    radius = max(1350.0, columns * spacing * 0.45)
    for i in range(light_count):
        angle = (i / float(light_count)) * 6.28318530718
        things.append(make_thing(
            Light,
            "stress_light_%d" % i,
            (math.cos(angle) * radius * 0.7, 180, math.sin(angle) * radius * 0.7),
            color=[255, 220, 180],
            intensity=3.0,
            radius=1800.0,
            state="on",
            casts_shadows=(i < 8),
        ))
    return brushes, things


def _run_renderer_stress():
    """Time the baseline dense renderer workload in windowed/fullscreen modes."""
    glh.reset_texture_cache()
    brushes, things = _make_renderer_stress_scene()
    results = []

    # The benchmark harness uses offscreen GL contexts, so "fullscreen" means
    # the fullscreen-sized 1920x1080 render target used by the editor.
    for mode, width, height in (("windowed", 1280, 720), ("fullscreen", 1920, 1080)):
        with glh.GLTestContext(width, height) as context:
            renderer = glh.make_renderer()
            try:
                for _ in range(5):
                    _render(renderer, context, brushes, things)

                samples = []
                for _ in range(20):
                    start = time.perf_counter()
                    _render(renderer, context, brushes, things)
                    samples.append(time.perf_counter() - start)

                mean = statistics.fmean(samples)
                results.append({
                    "test": "renderer_stress_%s" % mode,
                    "mode": mode,
                    "description": "%d brushes + %d lights (%d shadowed)" % (
                        len(brushes), len(things),
                        sum(1 for t in things if t.properties.get("casts_shadows"))),
                    "brush_count": len(brushes),
                    "resolution": "%dx%d" % (width, height),
                    "mean_ms": mean * 1000.0,
                    "p95_ms": sorted(samples)[min(
                        len(samples) - 1,
                        int(round(0.95 * (len(samples) - 1))))] * 1000.0,
                    "worst_ms": max(samples) * 1000.0,
                    "average_fps": 1.0 / mean if mean else float("inf"),
                })
            finally:
                try:
                    renderer.cleanup()
                except Exception:
                    pass

    glh.reset_texture_cache()
    return results


def _run_brush_count_stress(counts):
    """Run selected large brush scenes in both render-target modes."""
    results = []
    for brush_count in counts:
        glh.reset_texture_cache()
        brushes, things = _make_brush_stress_scene(brush_count)

        # Large scenes are deliberately shorter than the normal benchmark:
        # their purpose is to find scaling cliffs, not produce a long soak.
        for mode, width, height in (("windowed", 1280, 720), ("fullscreen", 1920, 1080)):
            with glh.GLTestContext(width, height) as context:
                renderer = glh.make_renderer()
                try:
                    for _ in range(3):
                        _render(renderer, context, brushes, things)

                    samples = []
                    for _ in range(10):
                        start = time.perf_counter()
                        _render(renderer, context, brushes, things)
                        samples.append(time.perf_counter() - start)

                    mean = statistics.fmean(samples)
                    results.append({
                        "test": "brush_scene_%d_%s" % (brush_count, mode),
                        "mode": mode,
                        "brush_count": brush_count,
                        "description": "%d brushes + %d lights (%d shadowed)" % (
                            brush_count, len(things),
                            sum(1 for t in things if t.properties.get("casts_shadows"))),
                        "resolution": "%dx%d" % (width, height),
                        "mean_ms": mean * 1000.0,
                        "p95_ms": sorted(samples)[min(
                            len(samples) - 1,
                            int(round(0.95 * (len(samples) - 1))))] * 1000.0,
                        "worst_ms": max(samples) * 1000.0,
                        "average_fps": 1.0 / mean if mean else float("inf"),
                    })
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
            raise ValueError("unsupported brush stress size: %s" % count)
        if count not in counts:
            counts.append(count)
    return tuple(counts)


def _run_io_stress():
    """Time a long real I/O chain through the production dispatcher."""
    from editor import io_system as io
    from editor.io_system import IOManager, OutputConnection
    from editor.io_handlers import register_all_input_handlers
    from editor.things import LogicRelay

    manager = IOManager()
    logic = type("StressLogic", (), {})()
    logic.io_manager = manager
    manager.set_logic_thread(logic)
    register_all_input_handlers(manager)
    entities = [
        LogicRelay(pos=[0, 0, 0],
                   properties={"name": "stress_relay_%d" % i, "fire_once": False})
        for i in range(700)
    ]
    by_name = {e.properties["name"]: e for e in entities}
    manager.set_entity_finder(lambda name: by_name.get(name))
    manager.set_entity_finder_by_id(
        lambda entity_id: next(
            (e for e in entities if e.properties.get("id") == entity_id), None))

    for i in range(len(entities) - 1):
        io.add_connection(entities[i], OutputConnection(
            output_name="OnTrigger",
            target_name=entities[i + 1].properties["name"],
            input_name="Trigger",
            parameter="",
            target_id=entities[i + 1].properties.get("id"),
        ))

    samples = []
    for _ in range(20):
        manager.reset()
        start = time.perf_counter()
        manager.fire_output(entities[0], "OnTrigger")
        samples.append(time.perf_counter() - start)

    mean = statistics.fmean(samples)
    return {
        "test": "io_stress",
        "description": "699 LogicRelay I/O hops x 20 runs",
        "hops": 699,
        "runs": len(samples),
        "mean_ms": mean * 1000.0,
        "p95_ms": sorted(samples)[min(
            len(samples) - 1, int(round(0.95 * (len(samples) - 1))))] * 1000.0,
        "worst_ms": max(samples) * 1000.0,
        "hops_per_second": 699.0 / mean if mean else float("inf"),
    }


def _run_io_chain_stress(entity_count=1000):
    """Time a production I/O chain containing exactly *entity_count* entities."""
    from editor import io_system as io
    from editor.io_system import IOManager, OutputConnection
    from editor.io_handlers import register_all_input_handlers
    from editor.things import LogicRelay
    import sys

    manager = IOManager()
    logic = type("StressLogic", (), {})()
    logic.io_manager = manager
    manager.set_logic_thread(logic)
    register_all_input_handlers(manager)

    entities = [
        LogicRelay(
            pos=[0, 0, 0],
            properties={"name": "stress_chain_%d" % i, "fire_once": False},
        )
        for i in range(entity_count)
    ]
    by_name = {e.properties["name"]: e for e in entities}
    by_id = {e.properties.get("id"): e for e in entities}
    manager.set_entity_finder(lambda name: by_name.get(name))
    manager.set_entity_finder_by_id(lambda entity_id: by_id.get(entity_id))

    for i in range(entity_count - 1):
        io.add_connection(entities[i], OutputConnection(
            output_name="OnTrigger",
            target_name=entities[i + 1].properties["name"],
            input_name="Trigger",
            parameter="",
            target_id=entities[i + 1].properties.get("id"),
        ))

    samples = []
    old_limit = sys.getrecursionlimit()
    # fire_output -> relay handler -> fire_output is intentionally a direct
    # synchronous chain. Raise the Python recursion limit only for this
    # benchmark so a 1000-entity chain measures the production path rather
    # than failing at CPython's default recursion guard.
    sys.setrecursionlimit(max(old_limit, entity_count * 4))
    try:
        for _ in range(10):
            manager.reset()
            start = time.perf_counter()
            manager.fire_output(entities[0], "OnTrigger")
            samples.append(time.perf_counter() - start)
    finally:
        sys.setrecursionlimit(old_limit)

    mean = statistics.fmean(samples)
    return {
        "test": "io_chain_%d" % entity_count,
        "description": "%d LogicRelay I/O hops x %d runs" % (entity_count - 1, len(samples)),
        "entities": entity_count,
        "hops": entity_count - 1,
        "runs": len(samples),
        "mean_ms": mean * 1000.0,
        "p95_ms": sorted(samples)[min(
            len(samples) - 1, int(round(0.95 * (len(samples) - 1))))] * 1000.0,
        "worst_ms": max(samples) * 1000.0,
        "hops_per_second": (entity_count - 1) / mean if mean else float("inf"),
    }


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
            raise ValueError("unsupported I/O chain size: %s" % count)
        if count not in counts:
            counts.append(count)
    return tuple(counts)


def _run_csg_stress():
    """Time repeated real convex-geometry/CSG reconstruction work."""
    from engine.brush_geometry import ConvexGeometry, box_planes, make_plane

    clip_planes = [
        make_plane((1, 1, 0), (32, 0, 0)),
        make_plane((-1, 1, 0), (-32, 0, 0)),
        make_plane((0, 1, 1), (0, 0, 32)),
        make_plane((0, 1, -1), (0, 0, -32)),
        make_plane((1, 0, 1), (32, 0, 32)),
        make_plane((-1, 0, 1), (-32, 0, 32)),
    ]
    planes = box_planes([0, 0, 0], [256, 256, 256])
    planes.extend(clip_planes)

    samples = []
    for _ in range(30):
        start = time.perf_counter()
        for _ in range(100):
            geometry = ConvexGeometry(planes)
            if not geometry.is_valid:
                raise AssertionError("CSG stress geometry became invalid")
        samples.append(time.perf_counter() - start)

    mean = statistics.fmean(samples)
    return {
        "test": "csg_stress",
        "description": "100 ConvexGeometry rebuilds x 30 runs; 12 planes",
        "rebuilds_per_run": 100,
        "planes": len(planes),
        "mean_ms": mean * 1000.0,
        "p95_ms": sorted(samples)[min(
            len(samples) - 1, int(round(0.95 * (len(samples) - 1))))] * 1000.0,
        "worst_ms": max(samples) * 1000.0,
        "rebuilds_per_second": 100.0 / mean if mean else float("inf"),
    }


def run_additional_stress_tests():
    """Run opt-in workloads for I/O, renderer and CSG."""
    results = _run_renderer_stress() + [_run_io_stress(), _run_csg_stress()]
    brush_counts = _selected_brush_counts()
    if brush_counts:
        results.extend(_run_brush_count_stress(brush_counts))
    io_counts = _selected_io_chain_counts()
    for count in io_counts:
        results.append(_run_io_chain_stress(count))
    return results


def run_benchmark(additional_tests=False):
    """Run the visual-suite scenarios and optional stress workloads."""
    results = []
    for width, height in _resolutions():
        for name, shadows, empty in SCENARIOS:
            results.append(
                _measure_scenario(width, height, name, shadows, empty)
            )
    if additional_tests:
        results.extend(run_additional_stress_tests())
    return results


def format_results(results, info=None):
    lines = [
        "Fio renderer benchmark — visual test suite scenarios",
        _execution_environment(),
        "Rendering path: tests/visual/test_lit_scene.py::_render",
        "",
        "scenario                                      resolution       FPS     mean ms   p95 ms   ms/MP",
    ]

    for result in results:
        if "scenario" not in result:
            continue
        lines.append(
            "%-46s %4dx%-4d %8.1f %10.2f %8.2f %8.3f"
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
        for r in stress_results:
            lines.append(
                "  %-16s %-52s mean %8.2f ms  p95 %8.2f ms  worst %8.2f ms" %
                (r["test"], r["description"], r["mean_ms"], r["p95_ms"], r["worst_ms"])
            )
            if "average_fps" in r:
                lines.append("    renderer FPS: %.1f" % r["average_fps"])
            if "hops_per_second" in r:
                lines.append("    I/O throughput: %.0f hops/s" % r["hops_per_second"])
            if "rebuilds_per_second" in r:
                lines.append("    CSG throughput: %.1f rebuilds/s" % r["rebuilds_per_second"])

    baselines = {
        result["scenario"]: result
        for result in results
        if "scenario" in result and result["width"] == 192 and result["height"] == 192
    }
    lines.extend(["", "Resolution scaling relative to the suite's 192x192 framebuffer:"])
    for result in results:
        if result["scenario"] not in baselines:
            continue
        if result["width"] == 192 and result["height"] == 192:
            continue
        base = baselines[result["scenario"]]
        lines.append(
            "  %-46s %4dx%-4d  pixels x%7.2f  frame-time x%7.2f"
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


def test_fullscreen_resolution_scaling_benchmark(capsys):
    """Benchmark real visual-suite rendering paths across resolutions."""
    with glh.GLTestContext(64, 64) as probe:
        info = probe.info()

    additional_tests = os.environ.get("FIO_FULLSCREEN_BENCH_ADDITIONAL") == "1"
    results = run_benchmark(additional_tests=additional_tests)
    output = format_results(results, info)

    output_path = os.environ.get("FIO_FULLSCREEN_BENCH_OUT")
    if output_path:
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "gpu": info["renderer"],
                    "gl_version": info["version"],
                    "warmup_frames": WARMUP_FRAMES,
                    "measured_frames": MEASURED_FRAMES,
                    "additional_tests": additional_tests,
                    "results": results,
                },
                handle,
                indent=2,
            )
        output += "\n\nWritten to %s" % output_path

    with capsys.disabled():
        print("\n" + output)

    assert results, "no benchmark resolutions configured"
