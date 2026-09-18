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
    """Build a dense renderer workload: hundreds of brushes and many lights."""
    from editor.things import Light
    from tests.helpers.worlds import box_brush, make_thing
    import math

    brushes = []
    grid = 24
    spacing = 120
    for z in range(grid):
        for x in range(grid):
            brushes.append(box_brush(
                "stress_brush_%d_%d" % (x, z),
                ((x - grid // 2) * spacing, 0, (z - grid // 2) * spacing),
                (96, 96, 96),
            ))

    things = []
    light_count = 32
    radius = 1350.0
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
    """Time the renderer against a deliberately dense scene at 1920x1080."""
    glh.reset_texture_cache()
    brushes, things = _make_renderer_stress_scene()

    with glh.GLTestContext(1920, 1080) as context:
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
            return {
                "test": "renderer_stress",
                "description": "%d brushes + %d lights (%d shadowed)" % (
                    len(brushes), len(things),
                    sum(1 for t in things if t.properties.get("casts_shadows"))),
                "resolution": "1920x1080",
                "mean_ms": mean * 1000.0,
                "p95_ms": sorted(samples)[min(
                    len(samples) - 1, int(round(0.95 * (len(samples) - 1))))] * 1000.0,
                "worst_ms": max(samples) * 1000.0,
                "average_fps": 1.0 / mean if mean else float("inf"),
            }
        finally:
            try:
                renderer.cleanup()
            except Exception:
                pass
    glh.reset_texture_cache()


def _run_io_stress():
    """Time a long real I/O chain through the production dispatcher."""
    from editor import io_system as io
    from editor.io_system import IOManager, OutputConnection
    from editor.io_handlers import register_all_input_handlers
    from editor.things import LogicRelay

    manager = IOManager()
    register_all_input_handlers(manager)
    entities = [
        LogicRelay(pos=[0, 0, 0],
                   properties={"name": "stress_relay_%d" % i, "fire_once": False})
        for i in range(1000)
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
        "description": "1000 LogicRelay I/O hops x 20 runs",
        "hops": 999,
        "runs": len(samples),
        "mean_ms": mean * 1000.0,
        "p95_ms": sorted(samples)[min(
            len(samples) - 1, int(round(0.95 * (len(samples) - 1))))] * 1000.0,
        "worst_ms": max(samples) * 1000.0,
        "hops_per_second": 999.0 / mean if mean else float("inf"),
    }


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
    return [_run_io_stress(), _run_renderer_stress(), _run_csg_stress()]


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
        "Rendering path: tests/visual/test_lit_scene.py::_render",
        "",
        "scenario                                      resolution       FPS     mean ms   p95 ms   ms/MP",
    ]

    for result in results:
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

    baselines = {
        result["scenario"]: result
        for result in results
        if result["width"] == 192 and result["height"] == 192
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

    results = run_benchmark()
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
                    "results": results,
                },
                handle,
                indent=2,
            )
        output += "\n\nWritten to %s" % output_path

    with capsys.disabled():
        print("\n" + output)

    assert results, "no benchmark resolutions configured"
