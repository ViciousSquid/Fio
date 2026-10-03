"""Regression tests for editor chrome and menu placement."""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
pytest.importorskip("PyQt5", reason="Qt is not available in this environment")

from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402

from editor.main_window import Toast  # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture(scope="session")
def qt_app():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    yield app


def test_toast_uses_a_half_second_opacity_animation(qt_app):
    parent = QWidget()
    toast = Toast(parent)

    toast.show_message("Saved")

    assert toast.anim.duration() == 500
    assert "background-color: #2E6F40" in toast.styleSheet()

    toast.fade_out()
    assert toast.anim.duration() == 500


def test_error_toast_also_fades_its_coloured_background(qt_app):
    parent = QWidget()
    toast = Toast(parent)

    toast.show_message("Failed", is_error=True)

    assert toast.anim.duration() == 500
    assert "background-color: #8B0000" in toast.styleSheet()


def test_view_menu_contains_surface_inspector_and_debug_contains_sysmon():
    source = Path("editor/ui.py").read_text(encoding="utf-8")

    assert "Surface Inspector (T)" in source
    assert "MainWindow.debug_menu.addAction(MainWindow.system_monitor_action)" in source
    assert "'Sysmon (F3)'" in source
    assert "view_menu.addAction(system_monitor_action)" not in source
