"""The bottom bar's boxes are as wide as their values need, no wider: they
were several times wider than "16" (Grid Size), wasting the bar."""

import pytest

pytest.importorskip("PyQt5")

pytestmark = pytest.mark.qt


def _boxes(window):
    return (window.grid_size_spinbox, window.world_size_spinbox,
            window.cull_dist_spinbox, window.display_mode_combobox,
            window.camera_mode_combobox)


def test_the_bottom_bar_boxes_are_snug(main_window):
    from PyQt5.QtWidgets import QComboBox
    for box in _boxes(main_window):
        metrics = box.fontMetrics()
        if isinstance(box, QComboBox):
            texts = [box.itemText(i) for i in range(box.count())]
        else:
            texts = [box.textFromValue(box.maximum())]
        widest = max(metrics.horizontalAdvance(t) for t in texts)
        # Room for the widest value and the arrows -- and not much more.
        assert widest + 30 <= box.width() <= widest + 70, (box, box.width(), widest)
        assert box.minimumWidth() == box.maximumWidth()        # fixed, not stretched


def test_every_option_fits_its_dropdown(main_window):
    combo = main_window.display_mode_combobox
    metrics = combo.fontMetrics()
    for i in range(combo.count()):
        assert metrics.horizontalAdvance(combo.itemText(i)) + 30 <= combo.width()
