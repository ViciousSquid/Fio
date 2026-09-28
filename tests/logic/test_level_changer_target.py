"""A LevelChanger only ever changes to a map inside ``maps/``.

``target_map`` is authored map data — a played package included — and the map
it loads becomes the editor's save target.  The old guard only checked that
the string *started* with ``maps/``, so ``maps/../../elsewhere`` passed.
"""

import pytest

pytest.importorskip("PyQt5", reason="LevelChanger is an editor.things entity")

from editor.things import LevelChanger                   # noqa: E402

pytestmark = pytest.mark.qt


class _Signal:
    def __init__(self):
        self.emitted = []

    def emit(self, path):
        self.emitted.append(path)


class _Window:
    def __init__(self):
        self.load_level_signal = _Signal()


def _changer():
    changer = LevelChanger(pos=[0, 0, 0])
    changer._main_window = _Window()
    return changer


@pytest.mark.parametrize("target, expected", [
    ("next", "maps/next.json"),
    ("maps/next.json", "maps/next.json"),
    ("maps\\chapter2\\boss", "maps/chapter2/boss.json"),
    ("chapter2/../next", "maps/next.json"),
])
def test_targets_inside_maps_are_loaded(target, expected):
    changer = _changer()
    assert changer.change_level(target) is True
    assert changer._main_window.load_level_signal.emitted == [expected]


@pytest.mark.parametrize("target", ["maps/../../victim", "../victim.json",
                                    "maps\\..\\..\\victim"])
def test_targets_climbing_out_of_maps_are_refused(target):
    changer = _changer()
    assert changer.change_level(target) is False
    assert changer._main_window.load_level_signal.emitted == []
