"""Soak: many Play/Stop sessions on the showcase map.

Every Play builds a session runtime -- a physics world subscribed to the
change journal, render and entity tables, decal rows for the map's Decal
entities, the sky resolved from the Primary PlayerStart -- and Stop must
let all of it go. ``_SHOWCASE`` is version 5 and carries a sky, a decal,
physics props and monsters, so it reaches each 3.5.0.1611 change at once.

Across many sessions: the traced heap, live journal subscribers and thread
count stay flat after warm-up, the sky survives every round trip, and a
session costs no more at the end than at the start.
"""

import threading

import pytest

from tests.helpers.soak import Soak, cycles

pytest.importorskip("PyQt5")

from engine.change_journal import JOURNAL     # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.soak, pytest.mark.slow]

LEVEL = "maps/_SHOWCASE.json"


def _live_sinks():
    return sum(1 for sink in JOURNAL._live if sink.alive)


def test_repeated_play_sessions_release_what_they_build(fio_session, qt_app):
    from PyQt5.QtCore import QEvent

    session = fio_session(LEVEL)
    sky = session.window.sky_image_path_from_map()
    assert sky, "the showcase map is expected to carry a sky panorama"

    total = cycles(40, minimum=12)
    with Soak("play/stop sessions on _SHOWCASE", total, warmup=4) as soak:
        for n in range(total):
            session.start_play()
            session.step(20)
            session.step(10, shoot=(n % 2 == 0), keys={"w"} if n % 3 else ())
            session.stop_play()
            qt_app.processEvents()
            qt_app.sendPostedEvents(None, QEvent.DeferredDelete)
            assert session.window.sky_image_path_from_map() == sky
            assert session.view.sky_image_path == sky
            soak.cycle_done(counters={
                'threads': threading.active_count(),
                'live journal sinks': _live_sinks(),
            })

    soak.assert_counter_flat('threads')
    soak.assert_counter_flat('live journal sinks', slack=2)
    soak.assert_heap_flat(per_cycle_bytes=16 * 1024, slack_bytes=2 * 1024 * 1024)
    soak.assert_rss_flat(per_cycle_bytes=256 * 1024)
    soak.assert_time_flat(factor=1.8, slack_seconds=0.050)
