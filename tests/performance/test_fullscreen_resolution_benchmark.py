"""Fullscreen-resolution renderer benchmark.

This benchmark answers one specific question: does Fio get slower in fullscreen
because the renderer processes more pixels, or because something else changes
in the fullscreen path?

It renders the same deterministic scene at several framebuffer resolutions.
The GPU is synchronized with glFinish() so the measured interval includes
completed GPU work rather than only CPU command submission.

Run with:
    python -m pytest tests/performance/test_fullscreen_resolution_benchmark.py --run-benchmarks -s

Interpretation:
* frame time rising roughly with pixel count -> likely fill-rate / fragment /
  framebuffer cost;
* frame time staying roughly flat -> likely CPU submission, synchronization,
  or another resolution-independent bottleneck;
* a sharp discontinuity at one resolution -> investigate framebuffer/MSAA,
  driver presentation, or a fullscreen-specific path.
"""

import json
import os
import statistics
import time

import pytest

from tests.helpers import gl as glh


pytestmark = [pytest.mark.gl, pytest.mark.benchmark, pytest.mark.slow]

WARMUP_FRAMES = 10
MEASURED_FRAMES = 30

# Surface Pro 9-class displays plus common desktop resolutions. Override with
# FIO_FULLSCREEN_BENCH_RESOLUTIONS="1280x720,1920x1080" if desired.
DEFAULT_RESOLUTIONS = (
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


def _benchmark_resolution(width, height):
    """Render the same scene repeatedly into a framebuffer at one resolution."""
    import OpenGL.GL as gl

    glh.reset_texture_cache()
    brushes, things = glh.lit_cube_scene(shadows=False)

    with glh.GLTestContext(width, height) as context:
        renderer = glh.make_renderer()
        try:
            aspect = float(width) / float(height)
            projection, view, eye = glh.camera_matrices(aspect=aspect)
            config = glh.render_config(
                all_brushes=brushes,
                all_things=things,
            )

            def frame():
                context.bind()
                gl.glClearColor(0.05, 0.05, 0.08, 1.0)
                renderer.render_scene(
                    projection, view, eye, brushes, things, None, config
                )
                # Include completed GPU work in the sample.
                gl.glFinish()

            # First frame is intentionally not part of steady-state timing:
            # shader compilation and initial buffer creation can dominate it.
            frame()
            for _ in range(WARMUP_FRAMES):
                frame()

            samples = []
            for _ in range(MEASURED_FRAMES):
                start = time.perf_counter()
                frame()
                samples.append(time.perf_counter() - start)

            mean_seconds = statistics.fmean(samples)
            p95_seconds = sorted(samples)[
                min(len(samples) - 1, int(round(0.95 * (len(samples) - 1))))
            ]
            pixels = width * height
            return {
                "width": width,
                "height": height,
                "pixels": pixels,
                "megapixels": pixels / 1_000_000.0,
                "mean_ms": mean_seconds * 1000.0,
                "p95_ms": p95_seconds * 1000.0,
                "worst_ms": max(samples) * 1000.0,
                "average_fps": 1.0 / mean_seconds if mean_seconds else float("inf"),
                "ms_per_megapixel": (
                    mean_seconds * 1000.0 / (pixels / 1_000_000.0)
                    if pixels else 0.0
                ),
            }
        finally:
            try:
                renderer.cleanup()
            except Exception:
                pass

    glh.reset_texture_cache()


def test_fullscreen_resolution_scaling_benchmark(capsys):
    """Report how renderer cost changes as framebuffer resolution increases."""
    results = [_benchmark_resolution(w, h) for w, h in _resolutions()]

    with glh.GLTestContext(64, 64) as probe:
        info = probe.info()

    lines = [
        "",
        "Fio fullscreen-resolution renderer benchmark",
        "GPU: %s" % info["renderer"],
        "GL:  %s" % info["version"],
        "warm-up=%d  measured=%d" % (WARMUP_FRAMES, MEASURED_FRAMES),
        "",
        "resolution       megapixels   avg FPS   mean ms   p95 ms   ms/MP",
    ]

    for result in results:
        lines.append(
            "%4dx%-4d          %7.2f     %7.1f   %7.2f   %7.2f   %7.3f"
            % (
                result["width"],
                result["height"],
                result["megapixels"],
                result["average_fps"],
                result["mean_ms"],
                result["p95_ms"],
                result["ms_per_megapixel"],
            )
        )

    base = results[0]
    lines.extend(["", "relative to %dx%d:" % (base["width"], base["height"])])
    for result in results:
        pixel_ratio = result["pixels"] / float(base["pixels"])
        time_ratio = result["mean_ms"] / max(base["mean_ms"], 1e-9)
        lines.append(
            "  %4dx%-4d  pixels x%.2f  frame-time x%.2f"
            % (result["width"], result["height"], pixel_ratio, time_ratio)
        )

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
        lines.append("")
        lines.append("written to %s" % output_path)

    with capsys.disabled():
        print("\n".join(lines))

    assert results, "no benchmark resolutions configured"
