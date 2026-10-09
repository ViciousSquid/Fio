"""The loading throbber shown while a very large level loads.

The editor's GUI thread is busy for the whole load, so the throbber is a
separate process (``main.py --fio-load-throbber``): a five-pixel strip of
orange and green stripes scrolling across the editor. These tests check what it
draws, that the real child starts and goes away when told, and that the editor
shows it for big levels only and always takes it down again.
"""

import json
import os
import sys
import time

import pytest

pytest.importorskip("PyQt5", reason="the throbber is a Qt window")

import load_throbber as lt                                  # noqa: E402

pytestmark = pytest.mark.qt


def test_stripes_alternate_orange_and_green_and_scroll():
    w = lt.STRIPE_WIDTH
    assert lt.stripe_colour(0, 0) == lt.ORANGE
    assert lt.stripe_colour(w, 0) == lt.GREEN
    assert lt.stripe_colour(2 * w, 0) == lt.ORANGE
    assert lt.stripe_colour(0, w) == lt.GREEN                 # scrolled by a stripe


def test_the_strip_is_five_pixels_tall_and_moves(qt_app):
    widget = lt._make_widget("Loading")
    widget.resize(300, lt.HEIGHT)
    first = widget.grab().toImage()
    assert first.height() == lt.HEIGHT == 5

    colours = {first.pixelColor(x, 2).getRgb()[:3] for x in range(first.width())}
    assert lt.ORANGE in colours and lt.GREEN in colours

    widget.offset = lt.STRIPE_WIDTH * 0.5
    moved = widget.grab().toImage()
    assert any(first.pixelColor(x, 2) != moved.pixelColor(x, 2) for x in range(300))
    widget.deleteLater()


def test_the_strip_sits_centred_over_the_editor():
    x, y, w, h = lt.throbber_geometry((100, 50, 1400, 900))
    assert h == 5
    assert x + w / 2 == pytest.approx(100 + 1400 / 2, abs=1)
    assert 50 < y < 50 + 900


def test_the_child_runs_main_py_from_source_and_the_exe_when_packaged(monkeypatch):
    cmd = lt.child_command("Loading big.json", (1, 2, 3, 5), "/fio")
    assert cmd[:2] == [sys.executable, os.path.join("/fio", "main.py")]
    assert cmd[2:] == [lt.CHILD_FLAG, "1", "2", "3", "5", "Loading big.json"]

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    packaged = lt.child_command("Loading big.json", (1, 2, 3, 5), "/fio")
    assert packaged[0] == sys.executable and packaged[1] == lt.CHILD_FLAG


def test_the_real_child_starts_and_leaves_when_told():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    throbber = lt.LoadThrobber("Loading", (0, 0, 300, 5), root).start()
    proc = throbber._proc
    try:
        time.sleep(0.5)
        assert throbber.running, "the throbber process exited on its own"
    finally:
        throbber.stop()
    assert proc.returncode == 0
    assert not throbber.running


# ---------------------------------------------------------------------------
# The editor
# ---------------------------------------------------------------------------

class _FakeThrobber:
    made = []

    def __init__(self, caption, geometry, root_dir=None):
        self.caption, self.geometry = caption, geometry
        self.started = self.stopped = False
        _FakeThrobber.made.append(self)

    def start(self):
        self.started = True
        return self

    def stop(self):
        self.stopped = True


@pytest.fixture
def fake_throbber(monkeypatch):
    _FakeThrobber.made = []
    monkeypatch.setattr(lt, "LoadThrobber", _FakeThrobber)
    return _FakeThrobber.made


def _level(path):
    path.write_text(json.dumps({"version": 3, "brushes": [], "things": []}))
    return str(path)


def test_an_ordinary_level_loads_without_a_throbber(main_window, tmp_path, fake_throbber):
    assert main_window.load_level_file(_level(tmp_path / "small.json"))
    assert fake_throbber == []


def test_a_big_level_shows_the_throbber_until_the_load_is_painted(
        main_window, tmp_path, fake_throbber, monkeypatch, qt_app):
    monkeypatch.setattr(lt, "MIN_LEVEL_BYTES", 1)
    assert main_window.load_level_file(_level(tmp_path / "big.json"))

    (throbber,) = fake_throbber
    assert throbber.started and not throbber.stopped
    assert throbber.caption == "Loading big.json…"
    assert throbber.geometry[3] == lt.HEIGHT

    qt_app.processEvents()                     # the next turn of the event loop
    assert throbber.stopped


def test_a_failed_big_load_still_takes_the_throbber_down(
        main_window, tmp_path, fake_throbber, monkeypatch, qt_app):
    monkeypatch.setattr(lt, "MIN_LEVEL_BYTES", 1)
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json")
    monkeypatch.setattr(main_window, "show_toast", lambda *a, **k: None)
    assert main_window.load_level_file(str(broken)) is False
    qt_app.processEvents()
    assert fake_throbber[0].stopped


def test_closing_the_window_takes_the_throbber_down(
        main_window, tmp_path, fake_throbber, monkeypatch):
    monkeypatch.setattr(lt, "MIN_LEVEL_BYTES", 1)
    assert main_window.load_level_file(_level(tmp_path / "big.json"))
    main_window.unsaved_changes = False
    main_window.close()
    assert fake_throbber[0].stopped
