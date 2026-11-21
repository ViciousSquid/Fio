"""2D views: animated links repaint only the links; the camera drives repaints.

With "Animate connections" on, the arrows travelling along I/O links asked
for a full repaint of the 2D view 60 times a second. The scene below the
links is now kept as an image, and a frame that only moves the arrows (or
the camera cone) reuses it. Any other repaint paints everything afresh.
The camera cone follows the 3D view's frames instead of a 60 Hz poll.
tests/performance/test_view2d_repaint_work.py counts what each frame does.
"""

import pytest

from editor.io_system import OutputConnection, add_connection
from editor.things import Light
from tests.helpers.worlds import box_brush

pytestmark = pytest.mark.qt


@pytest.fixture
def linked(main_window):
    w = main_window
    if not w.config.has_section('Display'):
        w.config.add_section('Display')
    w.config.set('Display', 'animate_connections', 'True')
    w.show_logic_links = True
    button = box_brush('button', (0, 0, 0), is_trigger=True)
    lamp = Light(pos=[256, 0, 128])
    lamp.properties['name'] = 'lamp'
    w.state.brushes.append(button)
    w.state.things.append(lamp)
    add_connection(button, OutputConnection(output_name='OnTrigger',
                                            target_name='lamp', input_name='TurnOn'))
    view = w.view_top
    view.resize(400, 300)
    view.pan_offset.setX(128.0)
    view.pan_offset.setY(64.0)
    view.update()
    return w, view, button


def _frame(view, arrows_only):
    if arrows_only:
        view._request_arrow_frame()
    else:
        view.update()
    return view.grab().toImage()


def test_an_arrow_frame_draws_what_a_full_repaint_draws(linked):
    _w, view, _button = linked
    full = _frame(view, arrows_only=False)
    assert view._base_layer is not None, "animated links keep the layer below them"
    cached = _frame(view, arrows_only=True)
    assert cached == full


def test_any_other_repaint_paints_the_scene_afresh(linked):
    w, view, button = linked
    _frame(view, arrows_only=False)
    layer = view._base_layer
    # An edit made without an update() call still reaches the next frame.
    button['pos'] = [64, 0, 64]
    w.state.mark_world_changed([button])
    _frame(view, arrows_only=True)
    assert view._base_layer is not layer
    layer = view._base_layer
    _frame(view, arrows_only=False)
    assert view._base_layer is not layer


def test_the_arrow_timer_runs_only_while_links_animate(linked):
    w, view, _button = linked
    _frame(view, arrows_only=False)
    assert view.animation_timer.isActive()
    w.config.set('Display', 'animate_connections', 'False')
    view._update_connection_animations()
    assert not view.animation_timer.isActive()
    # The scene is kept all the same: the camera cone is drawn over it.
    _frame(view, arrows_only=False)
    assert view._base_layer is not None


def test_a_camera_move_reaches_the_2d_view_without_a_timer(main_window, monkeypatch):
    view = main_window.view_top
    assert not hasattr(view, '_camera_tracking_timer')
    calls = []
    monkeypatch.setattr(type(view), '_request_overlay_frame',
                        lambda self: calls.append(self.view_type))
    view._last_camera_pos = None
    monkeypatch.setattr(type(view), 'isVisible', lambda self: True)
    main_window.notify_camera_moved()
    assert 'top' in calls
    calls.clear()
    main_window.notify_camera_moved()             # the camera has not moved
    assert calls == []
