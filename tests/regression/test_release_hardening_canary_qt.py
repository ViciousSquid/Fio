"""Hardening from the pre-release audit of Canary: the parts that need Qt.

The map values these are about are in ``test_release_hardening_canary.py``,
which runs in the headless tier.
"""

import pytest

pytestmark = pytest.mark.qt


# -- a colour that is null ------------------------------------------------------------------

def test_a_light_with_a_null_colour_is_white():
    from editor.things import Light, LightVolume
    for cls in (Light, LightVolume):
        thing = cls([0.0, 0.0, 0.0])
        thing.properties["colour"] = None
        assert thing.get_color() == [1.0, 1.0, 1.0]


# -- a 2D paint that raises ----------------------------------------------------------------------

def test_a_2d_paint_that_raises_leaves_no_painter_open(main_window, monkeypatch):
    """The exception left ``_paint_scene_layer`` with its QPainter active on
    a local pixmap; Qt then destroyed the pixmap under the painter, and the
    editor died with a segmentation fault on opening the map."""
    from PyQt5.QtGui import QPainter

    import editor.view_2d as view_2d

    opened = []

    class Recorded(QPainter):
        def __init__(self, *args):
            super().__init__(*args)
            opened.append(self)

    def unpaintable(painter):
        raise OverflowError("int too large to convert to float")

    view = main_window.view_top
    with monkeypatch.context() as patch:
        patch.setattr(view_2d, "QPainter", Recorded)
        patch.setattr(view, "_paint_base_layer", unpaintable)

        with pytest.raises(OverflowError):
            view._paint_scene_layer(view._base_layer_key())
        assert len(opened) == 1 and not opened[0].isActive()

        opened.clear()
        view._base_layer_valid = False
        with pytest.raises(OverflowError):
            view.paintEvent(None)
        assert len(opened) == 2 and not any(painter.isActive() for painter in opened)

    view._base_layer_valid = False
    view.repaint()                       # and the view paints again
    assert view._base_layer_valid
