"""The benchmark window must never be a dead end.

Run Benchmark needs something to benchmark: the map Fio currently has loaded,
or a ticked stress test. Its enabled state used to be decided once, when the
manager connected, and nothing re-decided it — so a session opened with no map
loaded could tick every test in the list and still never start a run.
"""

import argparse

import pytest

pytest.importorskip("PyQt5", reason="the benchmark manager is a Qt window")

from plugins.benchmark.manager import BenchmarkManager      # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def manager(qt_app):
    args = argparse.Namespace(host="127.0.0.1", port=1, token="t", pid=123,
                              root=".", repetitions=1, duration=None,
                              auto_start=False)
    win = BenchmarkManager(args)
    win._connected = True          # pretend the socket came up
    yield win
    win.close()
    win.deleteLater()


def connect(manager, has_map):
    manager._handle_message({"event": "hello", "has_current_map": has_map,
                             "pid": 123})


def test_a_loaded_map_is_enough_to_run(manager):
    connect(manager, has_map=True)
    assert manager.run_button.isEnabled()


def test_with_no_map_a_ticked_test_opens_the_run_button(manager):
    """The regression: this is the only way out when Fio has no map."""
    connect(manager, has_map=False)
    assert not manager.run_button.isEnabled()

    manager.checkboxes["live_io_1000"].setChecked(True)
    assert manager.run_button.isEnabled(), (
        "ticking a stress test with no map loaded left Run Benchmark disabled; "
        "the window cannot be used at all in that state")


def test_unticking_the_last_test_closes_it_again(manager):
    connect(manager, has_map=False)
    box = manager.checkboxes["live_io_1000"]
    box.setChecked(True)
    box.setChecked(False)
    assert not manager.run_button.isEnabled()


def test_the_additional_tests_box_counts_as_a_selection(manager):
    connect(manager, has_map=False)
    manager.checkboxes["additional_tests"].setChecked(True)
    assert manager.run_button.isEnabled()


def test_a_loaded_map_keeps_the_button_open_regardless_of_ticks(manager):
    connect(manager, has_map=True)
    manager.checkboxes["live_io_1000"].setChecked(True)
    manager.checkboxes["live_io_1000"].setChecked(False)
    assert manager.run_button.isEnabled()


def test_the_button_stays_shut_while_a_run_is_in_flight(manager):
    connect(manager, has_map=True)
    manager.running = True
    manager.run_button.setEnabled(False)
    manager.checkboxes["live_io_1000"].setChecked(True)
    assert not manager.run_button.isEnabled(), (
        "a checkbox toggle re-enabled Run Benchmark mid-run")


# ---------------------------------------------------------------------------
# The opening screen
# ---------------------------------------------------------------------------

def test_the_tests_list_starts_collapsed(manager):
    """One output pane on the opening screen, not an empty second box."""
    from PyQt5.QtWidgets import QScrollArea
    scroll = manager.findChild(QScrollArea)
    assert scroll is not None
    assert not scroll.isVisible()


def test_export_is_absent_until_there_is_something_to_export(manager):
    assert not manager.export_button.isVisible()
