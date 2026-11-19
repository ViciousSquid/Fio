"""Soak: many maps opened one after another in one session, each played.

The 3.5.1 release audit's open item: nothing opened map after map. Every
open replaces the world; renderer textures and models are cached per asset
for the session (bounded by the asset library), and everything a map or a
Play session builds -- tables, journal subscribers, pass memos, kept cull
results, physics, threads -- must go with it. After a first rotation through
the maps (caches fill there) the counts, the traced heap, resident memory
and the time a cycle takes stay flat.

Under a display (``xvfb-run``) each Play paints frames, which reaches the
renderer's per-table caches too; offscreen it runs without painting.
"""

import threading

import pytest

from tests.helpers.soak import Soak, cycles

pytest.importorskip("PyQt5")

from engine.change_journal import JOURNAL     # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.soak, pytest.mark.slow]

MAPS = ("maps/_SHOWCASE.json", "maps/MonsterTest.json",
        "maps/Office_Corridor.json", "maps/Portal_Test.json")


def _live_sinks():
    return sum(1 for sink in JOURNAL._live if sink.alive)


def _renderer_counts(renderer):
    if renderer is None:
        return {}
    return {
        'textures': len(getattr(renderer, 'texture_manager', ()) or ()),
        'models': len(getattr(renderer, 'loaded_models', ()) or ()),
        'pass memo entries': sum(len(t) for t in renderer._pass_memos.values()),
        'kept distance culls': len(renderer._distance_culls),
        'kept portal culls': len(renderer.__dict__.get('_portal_culls', ())),
        'slot transform caches': len(renderer._slot_transform_caches),
    }


def test_opening_map_after_map_releases_what_each_builds(fio_session, qt_app):
    import os
    from PyQt5.QtCore import QEvent
    from tests.helpers.paths import REPO_ROOT

    session = fio_session(MAPS[0])
    window, view = session.window, session.view
    can_paint = view.renderer is not None
    total = cycles(6, minimum=3) * len(MAPS)
    with Soak("map after map, each played", total, warmup=len(MAPS)) as soak:
        for n in range(total):
            level = MAPS[n % len(MAPS)]
            assert window.load_level_file(os.path.join(REPO_ROOT, level)), level
            window.unsaved_changes = False
            session.start_play()
            session.step(20, keys={"w"} if n % 2 else ())
            if can_paint:
                session.paint()
                session.step(2)
                session.paint()
            session.stop_play()
            qt_app.processEvents()
            qt_app.sendPostedEvents(None, QEvent.DeferredDelete)
            soak.cycle_done(counters=dict({
                'threads': threading.active_count(),
                'live journal sinks': _live_sinks(),
            }, **_renderer_counts(view.renderer)))

    soak.assert_counter_flat('threads')
    soak.assert_counter_flat('live journal sinks', slack=2)
    if can_paint:
        for name in ('textures', 'models', 'pass memo entries', 'kept distance culls',
                     'kept portal culls', 'slot transform caches'):
            soak.assert_counter_flat(name, slack=2)
    soak.assert_heap_flat(per_cycle_bytes=64 * 1024, slack_bytes=8 * 1024 * 1024)
    soak.assert_rss_flat(per_cycle_bytes=1024 * 1024, slack_bytes=48 * 2**20)
    soak.assert_time_flat(factor=1.8, slack_seconds=0.150)
