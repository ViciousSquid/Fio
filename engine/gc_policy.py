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

:func:`paused` only holds the collector off for an editor history step (an
undo, a redo, a checkpoint), which builds a world's worth of records in one
go, and decides nothing about collecting or freezing.
"""

import contextlib

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


@contextlib.contextmanager
def paused():
    """Pause the cyclic collector for one editor history step (nests safely).

    An undo, a redo or an undo checkpoint builds a whole world's worth of
    records at once -- JSON text, brush dicts, Things -- and the allocation
    counts it runs up start collections part way through, which walk every
    tracked object made since the last settle: after an undo, the whole world
    it rebuilt. Nothing is collected, frozen or unfrozen afterwards: what the
    step left behind waits for the next collection the counts start, as any
    garbage does.
    """
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()


def release() -> None:
    """Unfreeze everything (the editor window is closing): what it held,
    cycles included, is collectable again."""
    gc.unfreeze()
