"""End-to-end frame benchmark through the real editor host.

Drives the production path rather than a synthetic scene:

    MainWindow.load_level_file -> MainWindow.enter_play_mode
    -> LogicThread._step_frame / _publish_frame   (simulation + publication)
    -> ThreadedGameState.try_swap -> QtGameView.paintGL -> OpenGL

The logic thread is stopped after start-up and stepped by hand, one fixed tick
per frame, so every run does exactly the same simulation work and the numbers
compare across commits (and across branches: only the public surface that has
been stable since 2.5.10 is used).  A scripted input walks, turns and fires.

Needs a display with OpenGL (``xvfb-run -a``; ``LIBGL_ALWAYS_SOFTWARE=1``
selects llvmpipe).  Usage::

    xvfb-run -a python tools/bench_compare.py [--frames N] [--json out.json]
        [--profile] [map.json ...]

Reported per map: load ms, enter-play ms, per-frame logic and paint ms
(median / p95), the paint's main-thread CPU ms (submission without the
software rasteriser's worker threads), Python function calls per frame for each phase (machine
independent: a Python hot loop shows up here before it shows up as time), and
traced Python/NumPy memory growth over the run.
"""
from __future__ import annotations

import argparse
import cProfile
import gc
import json
import os
import pstats
import statistics
import sys
import time
import tracemalloc

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

DEFAULT_MAPS = [
    "maps/_SHOWCASE.json",
    "maps/MonsterTest.json",
    "maps/Terrain_Test_medium.json",
    "maps/Portal_Test.json",
    "maps/Office_Corridor.json",
]


def _percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _script(frame, key_w, key_a):
    """Keys and mouse delta for one frame: walk, strafe, turn, fire."""
    keys = {key_w}
    if (frame // 90) % 2:
        keys.add(key_a)
    return keys, (6.0 if (frame // 45) % 2 else -6.0), 0.0, frame % 20 == 0


def _call_count(profile):
    stats = pstats.Stats(profile)
    return sum(entry[1] for entry in stats.stats.values())


class Bench:
    def __init__(self, app, window_size=(640, 360)):
        from editor.main_window import MainWindow
        self.app = app
        self.window = MainWindow(ROOT)
        self.window.resize(1280, 800)
        self.window.show()
        self.view = self.window.view_3d
        self.view.resize(*window_size)
        self._pump_until(lambda: self.view.logic_thread is not None
                         and self.view.renderer is not None)

    def _pump_until(self, predicate, timeout=20.0):
        deadline = time.perf_counter() + timeout
        while not predicate():
            if time.perf_counter() > deadline:
                raise RuntimeError("editor did not finish starting")
            self.app.processEvents()
            time.sleep(0.01)

    def _halt_logic_thread(self):
        logic = self.view.logic_thread
        logic.running = False
        logic.join(5.0)
        assert not logic.is_alive(), "logic thread did not stop"
        return logic

    def run_map(self, path, frames, profile=False):
        from PyQt5.QtCore import Qt
        from OpenGL import GL
        window, view = self.window, self.view
        gc.collect()

        # The logic thread's own loop would race the load and the stepped
        # frames below, and make both timings depend on the scheduler.
        logic = self._halt_logic_thread()
        started = time.perf_counter()
        window.load_level_file(os.path.join(ROOT, path))
        load_ms = (time.perf_counter() - started) * 1000.0
        self.app.processEvents()
        started = time.perf_counter()
        window.enter_play_mode()
        enter_ms = (time.perf_counter() - started) * 1000.0
        assert view.play_mode, "play mode did not start for %s" % path

        # The monster AI normally runs on its own 30 Hz thread. Run the same
        # update on this thread every other frame instead, so the work done is
        # identical from run to run and timings do not depend on scheduling.
        import threading
        for thread in threading.enumerate():
            if thread.name == "MonsterAIThread":
                thread.stop()
                thread.join(5.0)
        monster_ai = logic.monster_ai

        game_state = view.game_state
        tick = logic.TICK_DURATION
        logic_ms, paint_ms, ai_ms, paint_cpu_ms = [], [], [], []
        pass_totals = {}
        logic_prof = cProfile.Profile() if profile else None
        paint_prof = cProfile.Profile() if profile else None

        def frame(index, measure):
            keys, dx, dy, fire = _script(index, Qt.Key_W, Qt.Key_A)
            game_state.set_keys(keys)
            game_state.set_mouse_delta(dx, dy)
            if fire:
                game_state.queue_shot()
            t0 = time.perf_counter()
            if logic_prof and measure:
                logic_prof.enable()
            logic._step_frame(tick)
            ta = time.perf_counter()
            if index % 2 == 0:
                monster_ai.update(2.0 * tick)
            tb = time.perf_counter()
            logic._publish_frame()
            if logic_prof and measure:
                logic_prof.disable()
            t1 = time.perf_counter()
            c1 = time.thread_time()
            if paint_prof and measure:
                paint_prof.enable()
            if game_state.try_swap():
                view.makeCurrent()
                view.paintGL()
                GL.glFinish()
                view.doneCurrent()
            if paint_prof and measure:
                paint_prof.disable()
            t2 = time.perf_counter()
            c2 = time.thread_time()
            if measure:
                paint_cpu_ms.append((c2 - c1) * 1000.0)
                stats = getattr(view.renderer, "render_stats", None)
                for name, ms in (getattr(stats, "pass_ms", None) or {}).items():
                    pass_totals[name] = pass_totals.get(name, 0.0) + ms
                logic_ms.append((t1 - t0 - (tb - ta)) * 1000.0)
                if index % 2 == 0:
                    ai_ms.append((tb - ta) * 1000.0)
                paint_ms.append((t2 - t1) * 1000.0)

        warmup = min(30, frames // 4)
        for index in range(warmup):
            frame(index, False)
        gc.collect()
        tracemalloc.start()
        mem_before = tracemalloc.get_traced_memory()[0]
        for index in range(warmup, warmup + frames):
            frame(index, True)
        mem_after, mem_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        window.enter_play_mode()   # Stop
        self.app.processEvents()
        result = {
            "map": path,
            "frames": frames,
            "load_ms": round(load_ms, 1),
            "enter_play_ms": round(enter_ms, 1),
            "logic_ms_median": round(statistics.median(logic_ms), 3),
            "logic_ms_p95": round(_percentile(logic_ms, 0.95), 3),
            "ai_ms_median": round(statistics.median(ai_ms), 3),
            "ai_ms_p95": round(_percentile(ai_ms, 0.95), 3),
            "paint_ms_median": round(statistics.median(paint_ms), 3),
            "paint_ms_p95": round(_percentile(paint_ms, 0.95), 3),
            # This thread's CPU only: with llvmpipe the rasterisation runs on
            # the driver's worker threads, so this is what Fio's submission
            # costs, the part a hardware GPU would still leave on the CPU.
            "paint_cpu_ms_median": round(statistics.median(paint_cpu_ms), 3),
            "traced_growth_kb": round((mem_after - mem_before) / 1024.0, 1),
            "traced_peak_kb": round((mem_peak - mem_before) / 1024.0, 1),
            "pass_ms_mean": {name: round(total / frames, 3)
                             for name, total in sorted(pass_totals.items())},
            "threads": sorted(t.name for t in __import__("threading").enumerate()),
            "things": len(window.state.things),
            "brushes": len(window.state.brushes),
        }
        if profile:
            result["logic_calls_per_frame"] = round(_call_count(logic_prof) / frames)
            result["paint_calls_per_frame"] = round(_call_count(paint_prof) / frames)
            result["_logic_profile"] = logic_prof
            result["_paint_profile"] = paint_prof
        # Restart the free-running thread so the editor is back to normal.
        view._thread_started = False
        view._start_logic_thread()
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("maps", nargs="*", default=DEFAULT_MAPS)
    parser.add_argument("--frames", type=int, default=300)
    parser.add_argument("--json")
    parser.add_argument("--profile", action="store_true",
                        help="count Python calls per frame (slower)")
    parser.add_argument("--dump", help="directory to write .pstats files to")
    parser.add_argument("--top", type=int, default=0,
                        help="print the N hottest functions per phase")
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    sys.path.insert(0, ROOT)
    # The editor writes its layout and recent files back to settings.ini.
    settings_path = os.path.join(ROOT, "settings.ini")
    with open(settings_path, "rb") as handle:
        settings = handle.read()
    try:
        return _run(args)
    finally:
        with open(settings_path, "wb") as handle:
            handle.write(settings)


def _run(args):
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv[:1])
    bench = Bench(app)
    results = []
    for path in args.maps:
        result = bench.run_map(path, args.frames, profile=args.profile)
        for phase in ("logic", "paint"):
            prof = result.pop("_%s_profile" % phase, None)
            if prof is not None and args.dump:
                os.makedirs(args.dump, exist_ok=True)
                prof.dump_stats(os.path.join(args.dump, "%s_%s.pstats" % (
                    os.path.splitext(os.path.basename(path))[0], phase)))
            if prof is not None and args.top:
                print("---- %s: %s hottest (tottime)" % (path, phase))
                pstats.Stats(prof).sort_stats("tottime").print_stats(args.top)
        print(json.dumps(result))
        results.append(result)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=2)
    bench.window.close()
    app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
