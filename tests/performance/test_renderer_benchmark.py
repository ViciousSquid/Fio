"""Renderer benchmark: measurements, reported rather than asserted.

Opt in with ``--run-benchmarks``; the normal suite skips this module, because a
test whose verdict depends on how fast the machine is teaches nobody anything.

What it produces is a table: total frames, elapsed time, average FPS and
frame-time percentiles for each of several renderer paths, with cold/startup
frames measured separately from steady state (the first frames compile shaders,
upload buffers and fill shadow cube-maps, and mixing them into the average
hides exactly the regression the benchmark is for).

Comparison against a stored baseline is opt-in too: set ``FIO_BENCH_BASELINE``
to a JSON file written by a previous run on the *same* machine and a path that
got materially slower is flagged.  Without it, the numbers are reported and
nothing fails.  A regression on one developer's laptop is not a defect in Fio.
"""

import json
import os
import statistics
import time

import pytest

from tests.helpers import gl as glh
from tests.helpers.worlds import box_brush, make_thing, pillar_grid

pytestmark = [pytest.mark.gl, pytest.mark.benchmark, pytest.mark.slow]

SIZE = 256

#: Frames discarded before measuring: shader compilation, buffer uploads and
#: the first shadow cube-map fill all land here.
WARMUP_FRAMES = 12

#: Frames in the measured steady-state window.
MEASURED_FRAMES = 40

#: How much slower than the baseline a path may get before it is flagged.
#: Generous, because even the same machine varies with thermal state and load.
REGRESSION_FACTOR = 1.6


def _percentile(values, fraction):
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


class BenchResult:
    """One measured path."""

    def __init__(self, name, cold_seconds, frame_times):
        self.name = name
        self.cold_seconds = cold_seconds
        self.frame_times = list(frame_times)

    @property
    def frames(self):
        return len(self.frame_times)

    @property
    def elapsed(self):
        return sum(self.frame_times)

    @property
    def average_fps(self):
        return self.frames / self.elapsed if self.elapsed else float("inf")

    @property
    def mean_ms(self):
        return 1000.0 * statistics.fmean(self.frame_times)

    @property
    def p95_ms(self):
        return 1000.0 * _percentile(self.frame_times, 0.95)

    @property
    def worst_ms(self):
        return 1000.0 * max(self.frame_times)

    def as_dict(self):
        return {"frames": self.frames, "elapsed": round(self.elapsed, 4),
                "average_fps": round(self.average_fps, 2),
                "cold_ms": round(1000.0 * self.cold_seconds, 3),
                "mean_ms": round(self.mean_ms, 3),
                "p95_ms": round(self.p95_ms, 3),
                "worst_ms": round(self.worst_ms, 3)}

    def report(self):
        return ("%-28s frames=%3d  elapsed=%6.3fs  avg=%7.1f fps  "
                "cold=%7.2f ms  mean=%6.2f ms  p95=%6.2f ms  worst=%6.2f ms"
                % (self.name, self.frames, self.elapsed, self.average_fps,
                   1000.0 * self.cold_seconds, self.mean_ms, self.p95_ms,
                   self.worst_ms))


def _benchmark_scene(brushes, things, name, **config_overrides):
    """Render ``brushes``/``things`` repeatedly and measure the steady state."""
    import OpenGL.GL as gl

    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as context:
        renderer = glh.make_renderer()
        try:
            projection, view, eye = glh.camera_matrices(aspect=1.0)
            config = glh.render_config(all_brushes=brushes, all_things=things,
                                       **config_overrides)

            def _frame():
                context.bind()
                gl.glClearColor(0.05, 0.05, 0.08, 1.0)
                renderer.render_scene(projection, view, eye, brushes, things,
                                      None, config)
                gl.glFinish()

            # Cold: the very first frame, on its own.
            cold_start = time.perf_counter()
            _frame()
            cold_seconds = time.perf_counter() - cold_start

            for _ in range(WARMUP_FRAMES - 1):
                _frame()

            frame_times = []
            for _ in range(MEASURED_FRAMES):
                start = time.perf_counter()
                _frame()
                frame_times.append(time.perf_counter() - start)
            return BenchResult(name, cold_seconds, frame_times)
        finally:
            try:
                renderer.cleanup()
            except Exception:
                pass
    glh.reset_texture_cache()


# ---------------------------------------------------------------------------
# The scenes
# ---------------------------------------------------------------------------

def _simple_scene():
    return glh.lit_cube_scene(shadows=False)


def _shadowed_scene():
    return glh.lit_cube_scene(shadows=True)


def _many_visible():
    """A representative but controlled crowd, all of it on screen."""
    from editor.things import Light
    brushes = [box_brush("floor", (0, -16, 0), (4096, 32, 4096))]
    brushes += pillar_grid(8, 8, spacing=160.0, size=(48, 160, 48))
    light = make_thing(Light, "bench_light", (0, 400, 400),
                       color=[255, 255, 255], intensity=2.0, radius=4000.0,
                       state="on", casts_shadows=False)
    return brushes, [light]


def _many_culled():
    """The same crowd, but spread far beyond the frustum."""
    from editor.things import Light
    brushes = [box_brush("floor", (0, -16, 0), (4096, 32, 4096))]
    brushes += pillar_grid(8, 8, spacing=6000.0, size=(48, 160, 48))
    light = make_thing(Light, "bench_light", (0, 400, 400),
                       color=[255, 255, 255], intensity=2.0, radius=4000.0,
                       state="on", casts_shadows=False)
    return brushes, [light]


PATHS = [
    ("simple", _simple_scene, {}),
    ("dynamic_light", _simple_scene, {}),
    ("shadows", _shadowed_scene, {}),
    ("frustum_cull_on", _many_culled, {"camera_distance_cull": True}),
    ("many_visible", _many_visible, {}),
    ("many_culled", _many_culled, {}),
]


def test_renderer_benchmark(record_property, capsys):
    """Measure each renderer path and report; compare to a baseline if given."""
    results = []
    for name, scene_factory, overrides in PATHS:
        brushes, things = scene_factory()
        results.append(_benchmark_scene(brushes, things, name, **overrides))

    lines = ["", "Fio renderer benchmark  (%d warm-up frames discarded, %d measured)"
             % (WARMUP_FRAMES, MEASURED_FRAMES)]
    with glh.GLTestContext(64, 64) as probe:
        info = probe.info()
    lines.append("GL: %s | %s" % (info["renderer"], info["version"]))
    lines += [result.report() for result in results]

    measurements = {result.name: result.as_dict() for result in results}
    record_property("fio_renderer_benchmark", json.dumps(measurements))

    baseline_path = os.environ.get("FIO_BENCH_BASELINE")
    regressions = []
    if baseline_path and os.path.exists(baseline_path):
        with open(baseline_path, "r", encoding="utf-8") as handle:
            baseline = json.load(handle)
        lines.append("baseline: %s" % baseline_path)
        for result in results:
            previous = baseline.get(result.name)
            if not previous:
                continue
            ratio = result.mean_ms / max(previous["mean_ms"], 1e-9)
            lines.append("  %-28s %6.2f ms vs %6.2f ms  (x%.2f)"
                         % (result.name, result.mean_ms, previous["mean_ms"],
                            ratio))
            if ratio > REGRESSION_FACTOR:
                regressions.append(
                    "%s is %.2fx slower than the baseline (%.2f ms vs %.2f ms)"
                    % (result.name, ratio, result.mean_ms, previous["mean_ms"]))
    else:
        lines.append("no baseline (set FIO_BENCH_BASELINE to a JSON file from a "
                     "previous run on this machine to compare)")

    out_path = os.environ.get("FIO_BENCH_OUT")
    if out_path:
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(measurements, handle, indent=2, sort_keys=True)
        lines.append("written to %s" % out_path)

    with capsys.disabled():
        print("\n".join(lines))

    assert all(result.frames == MEASURED_FRAMES for result in results), (
        "a benchmark path did not complete its measured window: %s"
        % [(r.name, r.frames) for r in results])
    assert not regressions, (
        "renderer performance regressed against the stored baseline:\n  %s"
        % "\n  ".join(regressions))


def test_culling_a_crowd_is_not_slower_than_drawing_it(capsys):
    """The one comparison that is machine-independent.

    Both scenes hold the same number of brushes; one has them on screen, the
    other far outside the frustum.  Culling that cost *more* than drawing would
    mean the cull itself had become the expensive part - a real regression, and
    one that shows up the same way on every machine.
    """
    visible = _benchmark_scene(*_many_visible(), name="many_visible")
    culled = _benchmark_scene(*_many_culled(), name="many_culled")

    with capsys.disabled():
        print("\n" + visible.report() + "\n" + culled.report())

    assert culled.mean_ms <= visible.mean_ms * 1.25, (
        "drawing %d off-screen brushes took %.2f ms a frame while drawing the "
        "same number on screen took %.2f ms; culling is costing more than "
        "rendering" % (len(_many_culled()[0]), culled.mean_ms, visible.mean_ms))
