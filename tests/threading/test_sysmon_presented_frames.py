"""SysMon counts completed Qt presentations, not update-timer callbacks."""

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

    # 121 presentations create 120 intervals: exactly one second at 120 Hz.
    for index in range(121):
        sysmon.record_presented_frame(100.0 + index / 120.0)

    metrics = sysmon.get_metrics()
    assert metrics["fps"] == pytest.approx(120.0)
    assert metrics["tps"] == pytest.approx(60.0)
    assert metrics["frame_time_ms"] == pytest.approx(1000.0 / 120.0)
    assert metrics["average_frame_time_ms"] == pytest.approx(1000.0 / 120.0)


def test_fps_and_average_use_the_same_rolling_window(qt_app):
    sysmon = _make_sysmon()

    # Fill the 60-interval window with 10 ms presentations.
    for index in range(61):
        sysmon.record_presented_frame(100.0 + index / 100.0)
    assert sysmon._fps == pytest.approx(100.0)

    # Replace every interval with 20 ms; the old samples must be evicted.
    for index in range(1, 61):
        sysmon.record_presented_frame(100.6 + index / 50.0)

    metrics = sysmon.get_metrics()
    assert metrics["fps"] == pytest.approx(50.0)
    assert metrics["average_frame_time_ms"] == pytest.approx(20.0)


def test_frame_swapped_counts_only_a_fresh_successful_paint(qt_app):
    from engine.qt_game_view import QtGameView

    recorded = []

    class Recorder:
        def record_presented_frame(self, timestamp):
            recorded.append(timestamp)
            return 120.0

    view = SimpleNamespace(
        _paint_completed_since_swap=False,
        sysmon=Recorder(),
        fps=0.0,
    )

    QtGameView._frame_swapped(view)
    assert recorded == []

    view._paint_completed_since_swap = True
    QtGameView._frame_swapped(view)
    assert len(recorded) == 1
    assert view.fps == pytest.approx(120.0)
    assert not view._paint_completed_since_swap

    QtGameView._frame_swapped(view)
    assert len(recorded) == 1


def test_paint_gl_marks_only_successful_paints_for_presentation(qt_app):
    from engine.qt_game_view import QtGameView

    def make_view(paint):
        state = object()
        return SimpleNamespace(
            renderer=SimpleNamespace(ready=True),
            logic_thread=object(),
            game_state=SimpleNamespace(
                get_render_state=lambda: state,
                release_render_state=lambda _state: None,
            ),
            _animate_textures=lambda: None,
            _paint_frame=paint,
            _collect_terrain_stats=lambda: None,
            _gl_error_checks_left=0,
            _muzzle_flash_counter=0,
            _paint_completed_since_swap=False,
        )

    view = make_view(lambda _state: None)
    QtGameView.paintGL(view)
    assert view._paint_completed_since_swap

    def fail(_state):
        raise RuntimeError("synthetic render failure")

    failed_view = make_view(fail)
    with pytest.raises(RuntimeError, match="synthetic render failure"):
        QtGameView.paintGL(failed_view)
    assert not failed_view._paint_completed_since_swap
