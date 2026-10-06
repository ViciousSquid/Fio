"""Pass timing for renderer implementations.

:func:`timed_pass` accumulates a pass's CPU time into the renderer's
:class:`~engine.renderer.api.RenderStats`. Any implementation may use it.
"""

import functools
import time


def timed_pass(name):
    """Accumulate a renderer pass's CPU time into ``render_stats.pass_ms``.

    Two clock reads per call, a handful of calls per frame: what the Debug
    Tables instrument shows as the per-pass submission cost.
    """
    def decorate(method):
        @functools.wraps(method)
        def timed(self, *args, **kwargs):
            started = time.perf_counter()
            try:
                return method(self, *args, **kwargs)
            finally:
                ms = self.render_stats.pass_ms
                ms[name] = ms.get(name, 0.0) + (time.perf_counter() - started) * 1000.0
        return timed
    return decorate

