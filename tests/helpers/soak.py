"""Measurement for the soak tier (``tests/soak``, opt-in via ``--run-soak``).

A soak test repeats one realistic cycle -- a Play/Stop, an edit and its undo,
a flag toggled on a sleeping body, a sky switched -- many times and checks
that nothing accumulates: Python heap (``tracemalloc``, which also sees NumPy
buffers), whatever resource count the test names (GL textures, journal sinks,
threads), and the time one cycle takes. A leak or an O(n) structure that only
grows shows up as a slope; a single slow cycle does not.

Cycle counts scale with ``FIO_SOAK_SCALE`` (default 1). A nightly or
pre-release run can use 5-10; 0.2 is a quick smoke of the tier itself.
"""

from __future__ import annotations

import gc
import os
import statistics
import sys
import time
import tracemalloc


def scale() -> float:
    try:
        value = float(os.environ.get("FIO_SOAK_SCALE", "1") or 1)
    except ValueError:
        value = 1.0
    return max(0.05, value)


def cycles(base: int, minimum: int = 8) -> int:
    """*base* cycles at scale 1, never fewer than *minimum*."""
    return max(minimum, int(round(base * scale())))


def _measured(snapshot):
    """Drop the harness's own bookkeeping (and tracemalloc's) from a snapshot.

    The per-cycle timing list and counter series grow by design; counted, they
    read as a leak of a few hundred bytes a cycle in the code under test.
    """
    return snapshot.filter_traces((
        tracemalloc.Filter(False, __file__),
        tracemalloc.Filter(False, tracemalloc.__file__),
    ))


def rss_bytes():
    """Resident set size on Linux, else None (only used as a report)."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return None


class Soak:
    """Times each cycle and samples the Python heap at fixed points.

    Use as a context manager around the loop and call :meth:`cycle_done`
    after each cycle; extra named counters go through ``counters=``.
    The first *warmup* cycles are excluded from every check: caches fill,
    shaders compile and lazily built tables are built there.
    """

    def __init__(self, name, total, warmup=None, heap_samples=8):
        self.name = name
        self.total = int(total)
        self.warmup = int(warmup if warmup is not None else max(2, self.total // 8))
        self.heap_every = max(1, (self.total - self.warmup) // heap_samples)
        self.times = []
        self.heap = []          # (cycle index, bytes)
        self.counters = {}      # name -> [(cycle index, value)]
        self.rss = []           # (cycle index, bytes)
        self._t = None
        self._started_tracing = False
        self._baseline_snapshot = None
        self._final_snapshot = None

    # -- lifecycle --------------------------------------------------------
    def __enter__(self):
        gc.collect()
        if not tracemalloc.is_tracing():
            tracemalloc.start(int(os.environ.get("FIO_SOAK_TRACE_FRAMES", "1")))
            self._started_tracing = True
        self._t = time.perf_counter()
        return self

    def __exit__(self, *exc):
        if self._baseline_snapshot is not None and tracemalloc.is_tracing():
            gc.collect()
            self._final_snapshot = _measured(tracemalloc.take_snapshot())
        if self._started_tracing:
            tracemalloc.stop()
        print("\n" + self.report())
        return False

    def cycle_done(self, counters=None):
        now = time.perf_counter()
        index = len(self.times)
        self.times.append(now - self._t)
        for key, value in (counters or {}).items():
            self.counters.setdefault(key, []).append((index, value))
        if index + 1 == self.warmup or (
                index + 1 > self.warmup
                and ((index + 1 - self.warmup) % self.heap_every == 0
                     or index + 1 == self.total)):
            gc.collect()
            snapshot = _measured(tracemalloc.take_snapshot())
            self.heap.append((index, sum(t.size for t in snapshot.traces)))
            rss = rss_bytes()
            if rss is not None:
                self.rss.append((index, rss))
            if index + 1 == self.warmup:
                self._baseline_snapshot = snapshot
        # Exclude the sampling itself from the next cycle's time.
        self._t = time.perf_counter()

    # -- measures -----------------------------------------------------------
    def steady_times(self):
        return self.times[self.warmup:]

    def heap_growth(self):
        """Bytes the traced heap grew from the end of warm-up to the end."""
        if len(self.heap) < 2:
            return 0
        return self.heap[-1][1] - self.heap[0][1]

    def time_ratio(self):
        """Median of the last quarter of steady cycles over the first quarter."""
        steady = self.steady_times()
        quarter = max(1, len(steady) // 4)
        first = statistics.median(steady[:quarter])
        last = statistics.median(steady[-quarter:])
        return last / first if first > 0 else 1.0, first, last

    def top_growth(self, limit=8):
        final = self._final_snapshot
        if final is None and tracemalloc.is_tracing():
            final = _measured(tracemalloc.take_snapshot())
        if self._baseline_snapshot is None or final is None:
            return ""
        stats = final.compare_to(self._baseline_snapshot, "lineno")
        return "\n".join("    " + str(stat) for stat in stats[:limit])

    def report(self):
        ratio, first, last = self.time_ratio() if self.steady_times() else (1.0, 0, 0)
        lines = [
            "[soak] %s: %d cycles (%d warm-up), cycle median %.2f ms -> %.2f ms "
            "(x%.2f), heap %+.1f KiB"
            % (self.name, len(self.times), self.warmup, first * 1e3, last * 1e3,
               ratio, self.heap_growth() / 1024.0)
        ]
        if len(self.rss) >= 2:
            lines.append("[soak]   RSS %+.1f MiB"
                         % ((self.rss[-1][1] - self.rss[0][1]) / 2**20))
        for key, series in self.counters.items():
            values = [v for i, v in series if i >= self.warmup] or [v for _, v in series]
            lines.append("[soak]   %s: %s .. %s (min %s, max %s)"
                         % (key, values[0], values[-1], min(values), max(values)))
        return "\n".join(lines)

    # -- checks ---------------------------------------------------------------
    def assert_heap_flat(self, per_cycle_bytes=512, slack_bytes=256 * 1024):
        """Heap growth after warm-up within ``slack + per_cycle * cycles``."""
        steady = len(self.times) - self.warmup
        allowed = slack_bytes + per_cycle_bytes * steady
        growth = self.heap_growth()
        assert growth <= allowed, (
            "%s: traced heap grew %.1f KiB over %d steady cycles (allowed %.1f KiB)."
            " Largest growth since warm-up:\n%s"
            % (self.name, growth / 1024.0, steady, allowed / 1024.0,
               self.top_growth()))

    def assert_time_flat(self, factor=2.0, slack_seconds=0.002):
        """The late cycles are not materially slower than the early ones."""
        ratio, first, last = self.time_ratio()
        assert last <= first * factor + slack_seconds, (
            "%s: cycle median went from %.3f ms to %.3f ms (x%.2f) -- per-cycle "
            "cost grows with the number of cycles run"
            % (self.name, first * 1e3, last * 1e3, ratio))

    def assert_rss_flat(self, per_cycle_bytes, slack_bytes=16 * 2**20):
        """Resident memory after warm-up within ``slack + per_cycle * cycles``.

        Catches native leaks the traced heap cannot see -- Qt widgets, GL
        driver buffers, C extension allocations. Linux only (elsewhere this is
        a no-op); keep the bound loose, the allocator does not return every
        page it frees.
        """
        if len(self.rss) < 2:
            return
        steady = len(self.times) - self.warmup
        allowed = slack_bytes + per_cycle_bytes * steady
        growth = self.rss[-1][1] - self.rss[0][1]
        assert growth <= allowed, (
            "%s: resident memory grew %.1f MiB over %d steady cycles (allowed "
            "%.1f MiB) while the traced Python heap grew %.1f KiB -- a native "
            "leak (Qt objects, GL resources) rather than Python objects"
            % (self.name, growth / 2**20, steady, allowed / 2**20,
               self.heap_growth() / 1024.0))

    def assert_counter_flat(self, key, slack=0):
        series = [v for i, v in self.counters.get(key, ()) if i >= self.warmup]
        assert series, "no samples for %s" % key
        assert max(series) - series[0] <= slack, (
            "%s: %s grew from %s to %s during steady cycles"
            % (self.name, key, series[0], max(series)))
