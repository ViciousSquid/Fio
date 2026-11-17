"""Soak: the editor's sky ownership, undo history and map round trips.

3.5.0.1611 made the map's panorama a property of the Primary PlayerStart and
moved its canonicalisation into ``save_state`` and ``mark_as_modified`` -- so
it now runs on every editor operation. These soaks run that path for many
operations through the editor's real entry points (property edits, Make
Primary, Delete, placement, undo, redo) and hold, after every one:

* at most one PlayerStart owns ``sky_image`` and it is the Primary;
* the 3D view shows exactly what the map resolves;

and over the whole run: per-operation cost, the traced heap and (on Linux)
resident memory stay flat once the 50-deep undo history is full. The resident
check is what catches stranded Qt widgets, which the Python heap cannot see.

The map soak loads every shipped map, saves and reloads it repeatedly: the
first save must already be a fixed point (version-5 local planes drift by
not one bit per round trip), and nothing accumulates across loads.
"""

import copy
import glob
import json
import os

import pytest

from tests.helpers.paths import REPO_ROOT
from tests.helpers.soak import Soak, cycles
from tests.helpers.worlds import make_thing

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QEvent, Qt          # noqa: E402
from PyQt5.QtGui import QKeyEvent             # noqa: E402

from editor.things import PlayerStart         # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.soak, pytest.mark.slow]


def _settle(qt_app):
    """What the editor's event loop does between two user operations,
    including destroying the widgets handed to ``deleteLater``."""
    qt_app.processEvents()
    qt_app.sendPostedEvents(None, QEvent.DeferredDelete)

SKIES = ("assets/backgrounds/cloudy-sky-panorama.webp",
         "assets/backgrounds/void-panorama-q80.webp", "")


def _starts(window):
    return window.state.player_starts()


def _check_sky_invariant(window, where):
    starts = _starts(window)
    primaries = [s for s in starts if s.properties.get('primary') is True]
    assert len(primaries) == (1 if starts else 0), where
    owners = [s for s in starts if 'sky_image' in s.properties]
    assert len(owners) <= 1, "%s: %d PlayerStarts own sky_image" % (where, len(owners))
    if owners:
        assert owners[0] is primaries[0], "%s: a non-primary owns the sky" % where
    assert window.view_3d.sky_image_path == window.sky_image_path_from_map(), where


def _place_start(window, index):
    window.save_state()
    start = make_thing(PlayerStart, 'soak_start_%d' % index,
                       (float(64 * (index % 9)), 0.0, float(64 * (index // 9 % 9))))
    window.state.things.append(start)
    window.state.settle_player_starts()
    window.mark_as_modified()
    return start


def _edit_sky(window, start, value):
    editor = window.property_editor
    editor.set_object(start)
    window.save_state()
    editor.update_object_prop('sky_image', value)


def _delete(window, thing):
    window.set_selected_objects([thing])
    window.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Delete, Qt.NoModifier))


def test_sky_ownership_through_many_edits_undos_and_redos(main_window, qt_app):
    window = main_window
    for i in range(4):
        _place_start(window, i)
    _check_sky_invariant(window, "setup")

    total = cycles(150, minimum=40)
    serial = 4
    with Soak("editor sky ownership ops", total, warmup=20) as soak:
        for n in range(total):
            starts = _starts(window)
            # A property edit on a start that may not be the Primary.
            _edit_sky(window, starts[n % len(starts)], SKIES[n % len(SKIES)])
            _check_sky_invariant(window, "edit %d" % n)

            # Edit a displayed field of the start on screen and let the panel
            # refresh, as most editor operations do: the same object's page
            # is rebuilt and the superseded one must be destroyed.
            primary = window.state.resolve_player_start()
            window.set_selected_objects([primary])
            assert window.property_editor.current_object is primary
            window.property_editor.update_object_prop('name', 'soak_primary_%d' % n)
            window.update_all_ui()
            _check_sky_invariant(window, "rename %d" % n)

            # Make another start Primary: ownership follows.
            target = _starts(window)[(n + 1) % len(_starts(window))]
            window.property_editor._make_primary_start(target)
            _check_sky_invariant(window, "make primary %d" % n)

            # Delete the Primary: the promoted start inherits the sky.
            sky = window.sky_image_path_from_map()
            primary = window.state.resolve_player_start()
            _delete(window, primary)
            _check_sky_invariant(window, "delete %d" % n)
            assert window.sky_image_path_from_map() == sky

            _place_start(window, serial)
            serial += 1
            _check_sky_invariant(window, "place %d" % n)

            window.undo()
            _check_sky_invariant(window, "undo place %d" % n)
            window.undo()
            _check_sky_invariant(window, "undo delete %d" % n)
            window.redo()
            _check_sky_invariant(window, "redo delete %d" % n)
            if len(_starts(window)) < 3:
                _place_start(window, serial)
                serial += 1
            _settle(qt_app)
            soak.cycle_done(counters={
                'undo depth': len(window.state.undo_stack),
                'player starts': len(_starts(window)),
            })

    # Undo depth is capped at 50, so per-op memory must not accumulate. Each
    # cycle rebuilds the property page several times: a stranded page was
    # ~300 KiB of native widgets per rebuild (see test_property_editor_rebuilds).
    soak.assert_heap_flat(per_cycle_bytes=2048, slack_bytes=1024 * 1024)
    soak.assert_rss_flat(per_cycle_bytes=96 * 1024)
    soak.assert_time_flat(factor=2.0, slack_seconds=0.010)
    assert max(v for _, v in soak.counters['undo depth']) <= 50


def _shipped_maps():
    return sorted(glob.glob(os.path.join(REPO_ROOT, 'maps', '*.json')))


def test_every_shipped_map_round_trips_to_a_fixed_point(main_window, qt_app):
    window = main_window
    maps = []
    for path in _shipped_maps():
        with open(path, encoding='utf-8') as handle:
            maps.append((os.path.basename(path), json.load(handle)))
    assert maps

    # First save of each map: what every later round trip must reproduce.
    fixed = {}
    for name, data in maps:
        assert window._load_level(copy.deepcopy(data)), name
        fixed[name] = json.dumps(window.state.get_level_data(), sort_keys=True)

    total = cycles(12, minimum=4)
    with Soak("shipped map save/load round trips", total, warmup=2) as soak:
        for _ in range(total):
            for name, _data in maps:
                assert window._load_level(json.loads(fixed[name])), name
                again = json.dumps(window.state.get_level_data(), sort_keys=True)
                assert again == fixed[name], (
                    "%s: a save/load round trip changed the document" % name)
            window.unsaved_changes = False
            _settle(qt_app)
            soak.cycle_done()

    soak.assert_heap_flat(per_cycle_bytes=64 * 1024, slack_bytes=4 * 1024 * 1024)
    soak.assert_rss_flat(per_cycle_bytes=1024 * 1024)
    soak.assert_time_flat(factor=1.6, slack_seconds=0.050)
