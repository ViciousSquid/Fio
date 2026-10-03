"""The lock-free caches Fio's background threads read, and why they are safe.

Fio does not lock its per-brush AABB cache.  ``engine.constants.brush_aabb_bounds``
is called from the logic thread and, concurrently, from the MonsterAI thread
(via ``SpatialGrid.has_line_of_sight``), and its safety rests on one property
that is easy to break by accident:

    the bounds are stored *before* the signature that validates them.

A reader that sees the new signature is therefore guaranteed to see the matching
bounds; a reader that sees the old one recomputes, which is always correct.  The
reverse order would let a reader latch a fresh signature against stale bounds
and - because the pair is cached - keep returning that stale value until the
brush moved again.

These tests state that invariant directly (by recording the store order), then
exercise it under real concurrency.  They deliberately do *not* add a lock: the
design relies on the GIL making each dict assignment atomic, and the point is
to protect that assumption, not to replace it.
"""

import threading
import time

import pytest

from engine.constants import brush_aabb_bounds
from tests.helpers.fakes import OrderRecordingDict
from tests.helpers.worlds import box_brush

pytestmark = pytest.mark.slow

DEADLINE = 5.0


def _recording_brush(pos=(0, 0, 0), size=(64, 64, 64)):
    brush = OrderRecordingDict(box_brush("watched", pos, size))
    brush.write_log.clear()
    return brush


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------

def test_the_bounds_are_stored_before_the_signature():
    brush = _recording_brush()

    brush_aabb_bounds(brush)

    writes = [key for key in brush.write_log if key in ("_aabb_bounds", "_aabb_sig")]
    assert writes == ["_aabb_bounds", "_aabb_sig"], (
        "publication order is load-bearing: the bounds must be stored before "
        "the signature that validates them, so a concurrent reader can never "
        "see a fresh signature against stale bounds. Stores were %s"
        % (writes,))


def test_the_same_order_holds_when_a_moved_brush_refreshes_its_cache():
    brush = _recording_brush()
    brush_aabb_bounds(brush)
    brush.write_log.clear()

    brush["pos"] = [500.0, 0.0, 0.0]
    brush_aabb_bounds(brush)

    writes = [key for key in brush.write_log if key in ("_aabb_bounds", "_aabb_sig")]
    assert writes == ["_aabb_bounds", "_aabb_sig"], (
        "a refresh after a move published in the order %s" % (writes,))


def test_an_unchanged_brush_writes_nothing_at_all():
    """A second read must be a pure lookup - no store, so nothing to observe."""
    brush = _recording_brush()
    brush_aabb_bounds(brush)
    brush.write_log.clear()

    brush_aabb_bounds(brush)

    assert brush.write_log == [], (
        "a cache hit wrote %s; a reader on another thread would see the cache "
        "churn for no reason" % (brush.write_log,))


def test_the_signature_is_exactly_the_pos_and_size_it_validates():
    brush = box_brush("b", (1, 2, 3), (4, 5, 6))
    brush_aabb_bounds(brush)
    assert brush["_aabb_sig"] == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0), (
        "the signature %r does not name the pos/size it was computed from"
        % (brush["_aabb_sig"],))


def test_the_cached_bounds_are_the_box_the_brush_describes():
    brush = box_brush("b", (10, 20, 30), (4, 6, 8))
    lo_x, lo_y, lo_z, hi_x, hi_y, hi_z = brush_aabb_bounds(brush)
    assert (lo_x, lo_y, lo_z) == pytest.approx((8.0, 17.0, 26.0))
    assert (hi_x, hi_y, hi_z) == pytest.approx((12.0, 23.0, 34.0))


def test_moving_a_brush_invalidates_the_cache_through_the_signature():
    brush = box_brush("mover", (0, 0, 0), (64, 64, 64))
    assert brush_aabb_bounds(brush)[0] == pytest.approx(-32.0)

    brush["pos"] = [100.0, 0.0, 0.0]

    assert brush_aabb_bounds(brush)[0] == pytest.approx(68.0), (
        "the cache returned bounds for the brush's old position - the "
        "signature did not catch the move")


def test_resizing_a_brush_invalidates_the_cache_too():
    brush = box_brush("resized", (0, 0, 0), (64, 64, 64))
    brush_aabb_bounds(brush)
    brush["size"] = [128.0, 64.0, 64.0]
    assert brush_aabb_bounds(brush)[0] == pytest.approx(-64.0)


# ---------------------------------------------------------------------------
# Under real concurrency
# ---------------------------------------------------------------------------

def test_a_reader_never_sees_bounds_that_do_not_match_the_position():
    """A writer moves a brush while a reader reads its cached bounds.

    Every observation the reader makes must be *some* consistent state: the
    bounds it gets back must be the box of one of the positions the brush has
    actually held, never a mixture.  This is the property the unlocked cache
    exists to provide.
    """
    brush = box_brush("contended", (0.0, 0.0, 0.0), (64.0, 64.0, 64.0))
    positions = [(float(i) * 1000.0, 0.0, 0.0) for i in range(64)]
    legal_bounds = set()
    for pos in positions:
        legal_bounds.add((pos[0] - 32.0, -32.0, -32.0, pos[0] + 32.0, 32.0, 32.0))

    stop = threading.Event()
    observations = []
    failures = []

    def _writer():
        index = 0
        while not stop.is_set():
            brush["pos"] = list(positions[index % len(positions)])
            index += 1

    def _reader():
        while not stop.is_set():
            bounds = brush_aabb_bounds(brush)
            rounded = tuple(round(v, 4) for v in bounds)
            observations.append(rounded)
            if rounded not in legal_bounds:
                failures.append(rounded)
                return

    writer = threading.Thread(target=_writer, daemon=True)
    reader = threading.Thread(target=_reader, daemon=True)
    writer.start()
    reader.start()
    time.sleep(0.25)
    stop.set()
    writer.join(timeout=DEADLINE)
    reader.join(timeout=DEADLINE)

    assert not failures, (
        "the reader observed bounds %s, which correspond to no position the "
        "brush ever held - the cache published a signature before its bounds"
        % (failures[:3],))
    assert len(observations) > 100, (
        "only %d reads happened; the test did not exercise the contention it "
        "is meant to" % len(observations))


def test_many_readers_agree_on_a_static_brush():
    """The common case: nothing moves, and every thread gets the same answer."""
    brush = box_brush("static", (12.0, 34.0, 56.0), (10.0, 20.0, 30.0))
    results = []
    barrier = threading.Barrier(8)

    def _read():
        barrier.wait(DEADLINE)
        for _ in range(500):
            results.append(brush_aabb_bounds(brush))

    threads = [threading.Thread(target=_read, daemon=True) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=DEADLINE)
        assert not thread.is_alive(), "a reader thread did not finish"

    assert len(set(results)) == 1, (
        "%d distinct results for an unchanging brush: %s"
        % (len(set(results)), sorted(set(results))[:3]))
