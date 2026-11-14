"""SysMon counts successful Qt renders, not update-timer callbacks."""

from types import SimpleNamespace

import pytest

pytest.importorskip("PyQt5")

pytestmark = pytest.mark.qt


def _make_sysmon():
    from engine.sysmon import SysMon

    parent = SimpleNamespace(
        logic_thread=SimpleNamespace(actual_tps=60.0),
        renderer=None,
        editor=SimpleNamespace(
            state=SimpleNamespace(brushes=[], things=[]),
            terrain=None,
        ),
    )
    sysmon = SysMon(parent)
    sysmon._get_vram_info = lambda: (None, None)
    sysmon.reset_metrics()
    return sysmon


def test_fps_uses_completed_frame_intervals_and_remains_separate_from_tps(qt_app):
    sysmon = _make_sysmon()
    assert sysmon.get_metrics()["fps"] == 0.0

    # 121 rendered frames create 120 intervals: exactly one second at 120 Hz.
    for index in range(121):
        sysmon.record_rendered_frame(100.0 + index / 120.0)

    metrics = sysmon.get_metrics()
    assert metrics["fps"] == pytest.approx(120.0)
    assert metrics["tps"] == pytest.approx(60.0)
    assert metrics["frame_time_ms"] == pytest.approx(1000.0 / 120.0)
    assert metrics["average_frame_time_ms"] == pytest.approx(1000.0 / 120.0)


def test_fps_and_average_use_the_same_rolling_window(qt_app):
    sysmon = _make_sysmon()

    # Fill the 60-interval window with 10 ms rendered-frame intervals.
    for index in range(61):
        sysmon.record_rendered_frame(100.0 + index / 100.0)
    assert sysmon._fps == pytest.approx(100.0)

    # Replace every interval with 20 ms; the old samples must be evicted.
    for index in range(1, 61):
        sysmon.record_rendered_frame(100.6 + index / 50.0)

    metrics = sysmon.get_metrics()
    assert metrics["fps"] == pytest.approx(50.0)
    assert metrics["average_frame_time_ms"] == pytest.approx(20.0)


def test_paint_gl_records_only_successful_renders(main_window, monkeypatch):
    from engine.qt_game_view import QtGameView

    target = main_window.view_3d
    recorded = []

    class Recorder:
        def record_rendered_frame(self, timestamp):
            recorded.append(timestamp)
            return 120.0

    class ReadyRenderer:
        ready = True
        render_stats = None

    target.sysmon = Recorder()
    target.renderer = ReadyRenderer()
    target.logic_thread = object()
    target.fps = 0.0
    state = object()
    monkeypatch.setattr(target.game_state, "get_render_state", lambda: state)
    monkeypatch.setattr(target.game_state, "release_render_state", lambda _state: None)
    monkeypatch.setattr(target, "_animate_textures", lambda: None)
    monkeypatch.setattr(target, "_paint_frame", lambda _state: None)
    monkeypatch.setattr(target, "_collect_terrain_stats", lambda: None)
    target._gl_error_checks_left = 0
    target._muzzle_flash_counter = 0

    QtGameView.paintGL(target)
    assert len(recorded) == 1
    assert target.fps == pytest.approx(120.0)

    def fail(_state):
        raise RuntimeError("synthetic render failure")

    monkeypatch.setattr(target, "_paint_frame", fail)
    with pytest.raises(RuntimeError, match="synthetic render failure"):
        QtGameView.paintGL(target)
    assert len(recorded) == 1


def test_numeric_display_smooths_spikes_without_changing_raw_samples(qt_app):
    sysmon = _make_sysmon()

    for _ in range(sysmon.GRAPH_POINTS):
        sysmon.record_frame_time(10.0)

    assert sysmon._smoothed_frame_time_ms == pytest.approx(10.0)
    assert sysmon._smoothed_fps == pytest.approx(100.0)

    # A single long frame remains visible in the raw graph, but the readout
    # moves gradually instead of jumping straight to the outlier.
    sysmon.record_frame_time(100.0)
    raw_last = float(
        sysmon._ft_buffer[(sysmon._ft_index - 1) % sysmon.GRAPH_POINTS]
    )
    assert raw_last == pytest.approx(100.0)
    assert 10.0 < sysmon._smoothed_frame_time_ms < 100.0
    assert sysmon._fps < sysmon._smoothed_fps < 100.0


def test_numeric_readouts_refresh_at_five_hz(qt_app):
    sysmon = _make_sysmon()
    sysmon._smoothed_fps = 120.0
    sysmon._smoothed_frame_time_ms = 8.33

    assert sysmon._refresh_display_values(now=10.0)
    assert sysmon._display_fps == pytest.approx(120.0)
    assert sysmon._display_frame_time_ms == pytest.approx(8.33)

    sysmon._smoothed_fps = 60.0
    sysmon._smoothed_frame_time_ms = 16.67

    # 100 ms later: keep the previous displayed value.
    assert not sysmon._refresh_display_values(now=10.1)
    assert sysmon._display_fps == pytest.approx(120.0)

    # At 200 ms: publish the latest smoothed values.
    assert sysmon._refresh_display_values(now=10.21)
    assert sysmon._display_fps == pytest.approx(60.0)
    assert sysmon._display_frame_time_ms == pytest.approx(16.67)
