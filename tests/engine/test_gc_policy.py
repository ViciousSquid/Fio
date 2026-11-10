"""The collector is settled where a pause is expected (engine/gc_policy.py):
a settle collects and freezes the survivors, so later collections skip them;
release unfreezes, so a closing window's world is collectable again."""

import gc
import weakref

from engine import gc_policy


class _Node:
    pass


def _cycle():
    a, b = _Node(), _Node()
    a.other, b.other = b, a
    return a


def test_a_settle_collects_then_freezes_the_survivors():
    garbage = weakref.ref(_cycle())            # unreachable already
    kept = _cycle()
    try:
        gc_policy.settle()
        assert garbage() is None               # collected by the settle
        assert gc.get_freeze_count() > 0
        tracked = len(gc.get_objects())
        assert not any(o is kept for o in gc.get_objects())   # frozen, not walked
        assert tracked < gc.get_freeze_count()
    finally:
        gc_policy.release()
    assert gc.get_freeze_count() == 0


def test_a_frozen_cycle_dropped_later_waits_for_the_next_settle_or_release():
    held = [_cycle()]
    probe = weakref.ref(held[0])
    try:
        gc_policy.settle()
        del held[:]
        gc.collect()
        assert probe() is not None             # frozen: a plain collection skips it
        gc_policy.settle()                     # the next load / play change
        assert probe() is None
    finally:
        gc_policy.release()


def test_release_makes_a_frozen_world_collectable():
    held = [_cycle()]
    probe = weakref.ref(held[0])
    gc_policy.settle()
    del held[:]
    gc_policy.release()
    gc.collect()
    assert probe() is None
