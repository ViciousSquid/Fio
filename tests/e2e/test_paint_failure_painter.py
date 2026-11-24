"""A frame that fails ends its QPainter before the failure leaves the paint.

``QtGameView._paint_frame`` opens a QPainter on the view and begins native
painting; the world is drawn, native painting ends, the HUD is drawn, the
painter ends. An exception anywhere in between used to leave the painter
active, in native-painting mode, alive in the traceback the interpreter
keeps (``sys.last_exc``). It was destroyed when that traceback was next
replaced -- by any later exception in any Qt callback -- under whatever GL
context was current by then: a segmentation fault well after, and nowhere
near, the frame that failed.
"""

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtGui import QPainter                      # noqa: E402

import engine.qt_game_view as qt_game_view           # noqa: E402
from editor.things import Light, PlayerStart         # noqa: E402
from tests.helpers.worlds import level_data, make_thing, room   # noqa: E402

pytestmark = [pytest.mark.gl, pytest.mark.integration]


def _level():
    spawn = make_thing(PlayerStart, "spawn", (0.0, 40.0, 0.0), angle=0.0)
    lamp = make_thing(Light, "lamp", (0.0, 300.0, -200.0), radius=2000.0)
    return level_data(brushes=room(size=1024.0, height=256.0), things=[spawn, lamp])


@pytest.fixture
def painters(monkeypatch):
    """Every QPainter the view opens, in order."""
    opened = []

    class Recorded(QPainter):
        def __init__(self, *args):
            super().__init__(*args)
            opened.append(self)
    monkeypatch.setattr(qt_game_view, "QPainter", Recorded)
    return opened


def _fail_once(monkeypatch, target, name, error):
    real = getattr(target, name)
    state = {"left": 1}

    def failing(*args, **kwargs):
        if state["left"]:
            state["left"] -= 1
            raise error
        return real(*args, **kwargs)
    monkeypatch.setattr(target, name, failing)


@pytest.mark.parametrize("where", ["world", "overlay"])
def test_a_failed_frame_leaves_no_active_painter(fio_session, monkeypatch, painters, where):
    session = fio_session(_level())
    view = session.view
    session.paint()
    assert painters and not painters[-1].isActive()
    painters.clear()

    if where == "world":            # inside native painting
        _fail_once(monkeypatch, view.renderer, "render_scene",
                   RuntimeError("the world failed"))
    else:                           # after it, while the HUD is drawn
        _fail_once(monkeypatch, view.floating_windows, "draw_all",
                   RuntimeError("the overlay failed"))
    with session.render_state() as frame, view.gl_context():
        with pytest.raises(RuntimeError, match="failed"):
            view._paint_frame(frame)

    assert len(painters) == 1
    assert not painters[0].isActive(), "the failed frame's painter was left open"

    painters.clear()
    image = session.paint()
    assert image is not None and painters and not painters[-1].isActive()
