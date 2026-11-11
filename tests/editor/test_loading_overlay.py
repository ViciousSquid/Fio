"""A long load shows one small floating panel with a throbber -- not a veil
over the window -- and it survives nested loads and exceptions."""

import json

import pytest

pytest.importorskip('PyQt5')
from PyQt5.QtGui import QColor                      # noqa: E402
from PyQt5.QtWidgets import QWidget                 # noqa: E402
from editor.loading_overlay import (                # noqa: E402
    FIO_ORANGE, PANEL_HEIGHT, PANEL_WIDTH, THROB_LINE, THROB_SECONDS,
    LoadingOverlay, throbber_span)

pytestmark = pytest.mark.qt


def test_nested_stages_keep_one_panel_and_its_stage(qt_app):
    host = QWidget()
    host.resize(900, 600)
    overlay = LoadingOverlay(host)
    with overlay.busy('Loading village', 'Reading map', 5):
        overlay.step('Building terrain', 50)
        assert overlay.stage == 'Building terrain'
        assert overlay.value == 50
        with overlay.busy('Nested', 'Uploading geometry', 30):
            assert overlay.value == 50
            assert overlay.title == 'Loading village'
        assert overlay.active
    assert not overlay.active
    assert overlay.isHidden()


def test_it_is_a_small_panel_in_the_middle_not_a_veil(qt_app):
    host = QWidget()
    host.resize(1280, 800)
    overlay = LoadingOverlay(host)
    with overlay.busy('Loading village', 'Reading map'):
        rect = overlay.panel_rect()
        assert (rect.width(), rect.height()) == (PANEL_WIDTH, PANEL_HEIGHT)
        assert rect.center().x() == 640 and rect.center().y() == 400
        # The panel is a small part of the window, and is no progress bar.
        assert rect.width() * rect.height() < 0.04 * 1280 * 800
        assert not hasattr(overlay, 'bar')
        host.resize(640, 480)
        qt_app.processEvents()
        overlay._layout()
        assert overlay.panel_rect().center().x() == 320


def test_the_editor_behind_it_is_shown_blurred_not_hidden(qt_app):
    """A picture of the window, softened: detail goes, the layout stays."""
    from PyQt5.QtWidgets import QLabel
    host = QWidget()
    host.resize(600, 400)
    host.setStyleSheet("background: black;")
    stripes = QLabel(host)
    stripes.setGeometry(0, 0, 600, 400)
    stripes.setStyleSheet(
        "background: qlineargradient(x1:0, y1:0, x2:0.02, y2:0, spread:repeat,"
        " stop:0 white, stop:0.5 white, stop:0.51 black, stop:1 black);")
    host.show()
    qt_app.processEvents()

    def edges(image, y=40):
        """(the sharpest step between neighbouring pixels, the mean level)."""
        values = [QColor(image.pixel(x, y)).red() for x in range(40, 560)]
        steps = [abs(a - b) for a, b in zip(values, values[1:])]
        return max(steps), sum(values) / len(values)

    sharp, sharp_mean = edges(host.grab().toImage())
    overlay = LoadingOverlay(host)
    with overlay.busy('Loading village', 'Reading map'):
        assert overlay.backdrop is not None
        assert overlay.geometry() == host.rect()
        soft, soft_mean = edges(overlay.grab().toImage())
    assert overlay.backdrop is None                  # dropped when it lowers
    assert sharp > 200, "the fixture has no hard edges to blur"
    assert soft < sharp * 0.5, "the backdrop is not noticeably blurred"
    assert abs(soft_mean - sharp_mean) < 40, "the backdrop was darkened, not blurred"
    host.close()


def test_a_window_that_cannot_be_pictured_still_gets_its_panel(qt_app):
    host = QWidget()                                 # never shown
    overlay = LoadingOverlay(host)
    with overlay.busy('Loading'):
        assert overlay.backdrop is None
        assert overlay.active


def test_the_throbber_line_crosses_the_track_and_comes_back():
    width = 200.0
    line = width * THROB_LINE
    assert throbber_span(0.0, width) == (0.0, line)
    start, end = throbber_span(THROB_SECONDS / 2, width)
    assert end == pytest.approx(width) and end - start == pytest.approx(line)
    assert throbber_span(THROB_SECONDS, width)[0] == pytest.approx(0.0)
    quarter = throbber_span(THROB_SECONDS / 4, width)[0]
    assert 0.0 < quarter < width - line
    # Going back mirrors going out.
    assert throbber_span(3 * THROB_SECONDS / 4, width)[0] == pytest.approx(quarter)


def test_each_step_paints_the_line_where_the_clock_has_carried_it(qt_app):
    host = QWidget()
    host.resize(900, 600)
    host.show()
    overlay = LoadingOverlay(host)
    now = [100.0]
    overlay.clock = lambda: now[0]

    def line_centre():
        image = overlay.grab().toImage()
        track = overlay.throbber_track()
        y = int(track.center().y())
        xs = [x for x in range(image.width())
              if QColor(image.pixel(x, y)).red() > 200
              and QColor(image.pixel(x, y)).blue() < 80]
        assert xs, "no throbber line was painted"
        return sum(xs) / len(xs)

    with overlay.busy('Loading village', 'Reading map'):
        left = line_centre()
        now[0] += THROB_SECONDS / 2
        overlay.step('Building terrain')
        right = line_centre()
    assert right - left > 100
    assert FIO_ORANGE.red() > 200
    host.close()


def test_progress_closes_after_a_failed_stage(qt_app):
    host = QWidget()
    overlay = LoadingOverlay(host)
    with pytest.raises(ValueError), overlay.busy('Loading'):
        raise ValueError('bad map')
    assert not overlay.active
    assert overlay.isHidden()


def test_file_load_uses_progress_without_starting_another_process(main_window, tmp_path, monkeypatch):
    import subprocess

    def unexpected_process(*args, **kwargs):
        pytest.fail('map loading must not spawn a throbber process')

    monkeypatch.setattr(subprocess, 'Popen', unexpected_process)
    level = tmp_path / 'map.json'
    level.write_text(json.dumps({'version': 3, 'brushes': [], 'things': []}))
    stages = []
    overlay = main_window.loading_overlay
    original_step = overlay.step

    def track(stage, percent=None):
        stages.append(stage)
        assert overlay.active
        original_step(stage, percent)

    monkeypatch.setattr(overlay, 'step', track)
    assert main_window.load_level_file(str(level))
    assert 'Reading the map' in stages
    assert 'Drawing the world' in stages
    assert not overlay.active
