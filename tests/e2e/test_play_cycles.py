"""Play and Stop, again and again, on a real map: nothing accumulates.

Each Play starts a monster AI thread, a physics world, a spatial grid, an
effect store session and the plugins' play hooks; each Stop must take all of
it down again. Leaks here are slow (one thread or one cache per Play) and
only show after many sessions, which is how an editor is actually used.
"""

import gc
import threading
import tracemalloc

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import Qt                     # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration, pytest.mark.slow]

MAP = "maps/MonsterTest.json"
CYCLES = 30


def _play_once(session):
    session.start_play()
    session.step(20, keys={Qt.Key_W}, shoot=True)
    session.stop_play()
    session.step(2)


def test_repeated_play_sessions_leak_no_threads_or_memory(fio_session):
    session = fio_session(MAP)
    for _ in range(3):                      # warm caches, textures, imports
        _play_once(session)
    gc.collect()
    threads = {t.name for t in threading.enumerate()}
    tracemalloc.start(1)
    try:
        before = tracemalloc.take_snapshot()
        for _ in range(CYCLES):
            _play_once(session)
        gc.collect()
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()

    assert {t.name for t in threading.enumerate()} == threads
    assert not any(t.name == "MonsterAIThread" for t in threading.enumerate())
    growth = sum(stat.size_diff for stat in after.compare_to(before, "filename"))
    # Allowance for interned strings and allocator slack, not for a per-Play
    # leak: a single leaked copy of this map's tables is larger than this.
    assert growth < 512 * 1024, "%d KiB retained over %d Play sessions:\n%s" % (
        growth // 1024, CYCLES,
        "\n".join(str(s) for s in after.compare_to(before, "lineno")[:10]))


def test_every_play_session_starts_from_the_same_state(fio_session):
    """A Play started after a Stop is the Play started from a fresh load."""
    session = fio_session(MAP)

    def first_frame():
        session.start_play()
        session.step(2)
        player = session.logic.player_runtime
        with session.render_state() as frame:
            snapshot = (tuple(round(float(v), 3) for v in frame.player_pos),
                        frame.player_health, frame.active_weapon,
                        frame.player_ammo, frame.entity_table.count,
                        frame.render_table.count, player.player_dead)
        session.step(40, keys={Qt.Key_W}, shoot=True)
        session.stop_play()
        return snapshot

    first = first_frame()
    for _ in range(3):
        assert first_frame() == first
