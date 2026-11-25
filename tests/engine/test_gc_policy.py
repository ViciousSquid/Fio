"""The collector is settled where a pause is expected (engine/gc_policy.py):
a settle collects and freezes the survivors, so later collections skip them;
release unfreezes, so a closing window's world is collectable again."""

import gc
import weakref

import pytest

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


def test_paused_holds_the_collector_off_and_nests():
    # An editor history step: the collector is off for it, put back as it
    # was afterwards (even on an error), and nothing is collected or frozen.
    assert gc.isenabled()
    garbage = weakref.ref(_cycle())
    with gc_policy.paused():
        assert not gc.isenabled()
        with gc_policy.paused():
            assert not gc.isenabled()
        assert not gc.isenabled()              # the inner one leaves it off
    assert gc.isenabled()
    assert garbage() is not None               # no collection of its own
    assert gc.get_freeze_count() == 0
    try:
        with gc_policy.paused():
            raise RuntimeError("the step failed")
    except RuntimeError:
        pass
    assert gc.isenabled()
    gc.collect()
    assert garbage() is None


@pytest.mark.qt
def test_an_undo_checkpoint_is_taken_with_the_collector_paused(monkeypatch):
    pytest.importorskip("PyQt5", reason="EditorState is editor-tier")
    from editor.editor_state import EditorState
    state = EditorState()
    seen = []
    real = state._history_entry
    monkeypatch.setattr(state, '_history_entry',
                        lambda: (seen.append(gc.isenabled()), real())[1])
    state.save_state()
    assert seen == [False]
    assert gc.isenabled()
