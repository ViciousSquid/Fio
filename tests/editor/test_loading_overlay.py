"""A single, thin progress bar survives nested loads and exceptions."""

import json

import pytest

pytest.importorskip('PyQt5')
from PyQt5.QtWidgets import QWidget
from editor.loading_overlay import BAR_HEIGHT, LoadingOverlay

pytestmark = pytest.mark.qt


def test_progress_stays_ten_pixels_high_and_keeps_its_stage(qt_app):
    host = QWidget()
    host.resize(900, 600)
    overlay = LoadingOverlay(host)
    with overlay.busy('Loading village', 'Reading map', 5):
        overlay.step('Building terrain', 50)
        assert overlay.bar.height() == BAR_HEIGHT == 10
        assert not overlay.bar.isTextVisible()
        assert overlay.stage == 'Building terrain'
        assert overlay.value == 50
        with overlay.busy('Nested', 'Uploading geometry', 30):
            assert overlay.value == 50
        assert overlay.active
    assert not overlay.active
    assert overlay.isHidden()


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
