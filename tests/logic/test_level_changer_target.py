"""A LevelChanger only ever changes to a map inside ``maps/``.

``target_map`` is authored map data — a played package included — and the map
it loads becomes the editor's save target.  The old guard only checked that
the string *started* with ``maps/``, so ``maps/../../elsewhere`` passed.
"""

import pytest

pytest.importorskip("PyQt5", reason="LevelChanger is an editor.things entity")

from editor.things import LevelChanger                   # noqa: E402

pytestmark = pytest.mark.qt


def _changer(main_window):
    changer = LevelChanger(pos=[0, 0, 0])
    emitted = []
    main_window.load_level_signal.connect(emitted.append)
    changer._main_window = main_window
    return changer, emitted


@pytest.mark.parametrize("target, expected", [
    ("next", "maps/next.json"),
    ("maps/next.json", "maps/next.json"),
    ("maps\\chapter2\\boss", "maps/chapter2/boss.json"),
    ("chapter2/../next", "maps/next.json"),
])
def test_targets_inside_maps_are_loaded(main_window, target, expected):
    changer, emitted = _changer(main_window)
    assert changer.change_level(target) is True
    assert emitted == [expected]


@pytest.mark.parametrize("target", ["maps/../../victim", "../victim.json",
                                    "maps\\..\\..\\victim"])
def test_targets_climbing_out_of_maps_are_refused(main_window, target):
    changer, emitted = _changer(main_window)
    assert changer.change_level(target) is False
    assert emitted == []
