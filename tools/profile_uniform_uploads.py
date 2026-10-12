"""Per-pass CPU time spent uploading per-row brush uniforms, measured in place.

Runs ``tools/bench_compare.py``'s scripted frames (real editor host, real
paintGL, PyOpenGL error checking off as in ``main.py``) against a checkout
and times, with one ``perf_counter`` pair each,

* every ``glUniformMatrix4fv`` / ``glUniformMatrix3fv`` / ``glUniform3fv``
  that receives a NumPy array -- the wrapper path, and
* every call of an ``engine.renderer.core.uniform_rows`` uploader -- the raw
  path, where the checkout has it,

attributed to the render pass that made it.  The timer pair costs the same on
both paths, so before/after differences are the uploads' own.  Point
``--tree`` at another checkout (an older commit in a ``git worktree``) to
measure it with this same script::

    LIBGL_ALWAYS_SOFTWARE=1 xvfb-run -a python tools/profile_uniform_uploads.py \\
        --tree ../fio-before --frames 300 maps/_SHOWCASE.json

Reports, per map and pass: uploads per frame, us per frame, us per upload.
"""
from __future__ import annotations

import argparse
import collections
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("maps", nargs="*", default=["maps/_SHOWCASE.json"])
    parser.add_argument("--tree", default=os.path.dirname(HERE),
                        help="checkout to measure (default: this one)")
    parser.add_argument("--frames", type=int, default=300)
    args = parser.parse_args(argv)
    tree = os.path.abspath(args.tree)
    sys.path.insert(0, tree)
    sys.path.insert(0, os.path.join(tree, "tools"))
    os.environ.setdefault("PYOPENGL_ERROR_CHECKING", "0")

    import numpy as np
    import OpenGL.GL as gl
    import bench_compare

    per_pass = collections.defaultdict(lambda: [0, 0.0])
    clock = time.perf_counter

    for name in ("glUniformMatrix4fv", "glUniformMatrix3fv", "glUniform3fv"):
        real = getattr(gl, name)

        def timed(*call_args, _real=real):
            if not isinstance(call_args[-1], np.ndarray):
                return _real(*call_args)
            started = clock()
            result = _real(*call_args)
            entry = per_pass[sys._getframe(1).f_code.co_name]
            entry[0] += 1
            entry[1] += clock() - started
            return result
        setattr(gl, name, timed)

    try:
        from engine.renderer.core import uniform_rows as rows_module
    except ImportError:            # a checkout from before the raw path
        rows_module = None
    if rows_module is not None:
        import engine.renderer.forward.lighting as lighting
        import engine.renderer.forward.passes as passes
        real_rows = rows_module.uniform_rows

        def timed_rows(kind, rows):
            upload = real_rows(kind, rows)

            def timed_upload(location, index):
                started = clock()
                upload(location, index)
                entry = per_pass[sys._getframe(1).f_code.co_name]
                entry[0] += 1
                entry[1] += clock() - started
            return timed_upload
        passes.uniform_rows = lighting.uniform_rows = timed_rows

    run_map = bench_compare.Bench.run_map

    def measured(self, path, frames, **kwargs):
        per_pass.clear()
        result = run_map(self, path, frames, **kwargs)
        total = frames + min(30, frames // 4)       # bench_compare's warm-up
        count = sum(n for n, _ in per_pass.values())
        seconds = sum(s for _, s in per_pass.values())
        print("%s (%d brushes, %d things): %.1f uploads/frame, %.1f us/frame"
              % (path, result["brushes"], result["things"], count / total,
                 seconds / total * 1e6))
        for name, (n, s) in sorted(per_pass.items(), key=lambda kv: -kv[1][1]):
            print("    %-34s %6.2f/frame %8.1f us/frame %6.2f us/upload"
                  % (name, n / total, s / total * 1e6, s / n * 1e6))
        return result

    bench_compare.Bench.run_map = measured
    return bench_compare.main(["--frames", str(args.frames)] + [
        os.path.join(tree, m) if not os.path.isabs(m) else m for m in args.maps])


if __name__ == "__main__":
    raise SystemExit(main())
