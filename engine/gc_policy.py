"""Garbage collection settled where a pause is expected, not in play.

CPython's cyclic collector runs a full (generation 2) collection whenever
the long-lived objects have grown by a quarter. A loaded world is a few
hundred thousand long-lived objects -- brush and entity dicts, their
geometry records, the tables -- and a full collection walks every one of
them: ~45 ms on BigWorld, a dropped frame or three, at a moment nobody
chose.

:func:`settle` runs where the editor already shows a loading bar -- after a
level loads, as play starts, as play stops: it collects once, then freezes
what survives (``gc.freeze``), so later collections walk only the objects
made since. Frozen objects are still freed when nothing refers to them; a
reference cycle among them waits for the next settle, which unfreezes
first; :func:`release` (a window closing) unfreezes, so what it owned can
be collected.
"""

import gc
import time

#: Milliseconds the last settle took (diagnostics).
last_ms = 0.0


def settle() -> float:
    """Collect everything now, then freeze the survivors; the milliseconds
    it took."""
    global last_ms
    started = time.perf_counter()
    gc.unfreeze()
    gc.collect()
    gc.freeze()
    last_ms = (time.perf_counter() - started) * 1000.0
    return last_ms


def release() -> None:
    """Unfreeze everything (the editor window is closing): what it held,
    cycles included, is collectable again."""
    gc.unfreeze()
