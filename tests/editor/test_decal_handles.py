"""A selected decal has handles in the 2D views: drag an edge to size it,
snapped to the grid, with "Keep aspect ratio" (on by default) scaling the
other side; the opposite edge stays put; one drag, one undo step."""

import pytest

from editor import decal_handles as dh
from editor.things import Decal

# The headless tier leaves out any module that imports PyQt5 or OpenGL
# (one test here does); unmarked, the Qt tier deselected it too.
pytestmark = pytest.mark.qt

# The 2D views' axes: top (x, z), front (x, y), side (z, y).
TOP, FRONT, SIDE = (0, 2), (0, 1), (2, 1)


def _wall_decal(width=128.0, height=64.0, pos=(0.0, 100.0, -200.0)):
    """On a wall facing +Z: width along x, height along y."""
    return Decal(pos=list(pos), properties=dict(
        texture='x.png', width=width, height=height, depth=32.0, yaw=90.0, pitch=0.0))


def _floor_decal(width=128.0, height=64.0):
    return Decal(pos=[0.0, 0.0, 0.0], properties=dict(
        texture='x.png', width=width, height=height, depth=32.0, yaw=0.0, pitch=-90.0))


def test_the_views_show_the_edges_they_can_drag():
    decal = _wall_decal()
    keys = lambda axes: sorted({k for k, _s, _p in dh.handles(decal, *axes)})
    assert keys(FRONT) == ['height', 'width']              # facing the view: both
    assert keys(TOP) == ['width']                          # from above: its width only
    assert keys(SIDE) == ['height']                        # from the side: its height
    floor = _floor_decal()
    assert sorted({k for k, _s, _p in dh.handles(floor, *TOP)}) == ['height', 'width']
    assert dh.handles(floor, *FRONT) and all(abs(p[1]) < 1e-6 for _k, _s, p in dh.handles(floor, *FRONT))


def test_the_outline_is_the_face():
    corners = dh.outline(_wall_decal(), *FRONT)
    us = sorted(round(u, 6) for u, _v in corners)
    vs = sorted(round(v, 6) for _u, v in corners)
    assert (us[0], us[-1]) == (-64.0, 64.0) and (vs[0], vs[-1]) == (68.0, 132.0)


def _outermost(decal, key, axes, along):
    """``(sign, (u, v))`` of the *key* handle furthest along view axis
    *along* (0: u, 1: v): which end that is depends on how the decal is
    turned, not on its sign."""
    found = [(s, p) for k, s, p in dh.handles(decal, *axes) if k == key]
    return max(found, key=lambda h: h[1][along])


def test_dragging_an_edge_snaps_it_to_the_grid_and_keeps_the_other_end():
    decal = _wall_decal()                                  # x from -64 to 64
    sign, (u, _v) = _outermost(decal, 'width', FRONT, 0)
    assert u == pytest.approx(64.0)
    # The +x edge dragged to 97 (grid 16): it lands on 96, the -x edge stays
    # at -64, so the width is 160 and the centre moves to x 16.
    pos, width, height = dh.resize(decal, 'width', sign, (97.0, 100.0), *FRONT,
                                   grid=16, keep_aspect=False)
    assert width == pytest.approx(160.0) and height == pytest.approx(64.0)
    assert pos == pytest.approx([16.0, 100.0, -200.0])


def test_keep_aspect_scales_the_other_side_about_the_centre_line():
    decal = _wall_decal(width=128.0, height=64.0, pos=(0.0, 96.0, -200.0))   # 2:1, y 64..128
    sign, (_u, v) = _outermost(decal, 'height', FRONT, 1)
    assert v == pytest.approx(128.0)
    # The top edge up to 161 (snaps to 160): height 96, width 192 -- 2:1.
    pos, width, height = dh.resize(decal, 'height', sign, (0.0, 161.0), *FRONT,
                                   grid=16, keep_aspect=True)
    assert (width, height) == (pytest.approx(192.0), pytest.approx(96.0))
    assert pos == pytest.approx([0.0, 112.0, -200.0])      # bottom edge stayed at 64


def test_without_the_grid_a_drag_is_free_and_never_inverts():
    decal = _wall_decal()
    sign, _p = _outermost(decal, 'width', FRONT, 0)
    _pos, width, _h = dh.resize(decal, 'width', sign, (70.3, 0.0), *FRONT,
                                grid=None, keep_aspect=False)
    assert width == pytest.approx(134.3)
    _pos, width, _h = dh.resize(decal, 'width', sign, (-500.0, 0.0), *FRONT,
                                grid=16, keep_aspect=False)
    assert width == pytest.approx(16.0)                    # one grid step, not negative


def test_a_turned_decal_snaps_its_length_to_grid_steps():
    decal = Decal(pos=[0.0, 100.0, 0.0], properties=dict(
        texture='x.png', width=100.0, height=50.0, depth=32.0, yaw=45.0, pitch=0.0))
    key, sign, point = next(h for h in dh.handles(decal, *TOP) if h[0] == 'width')
    far = (point[0] * 1.5, point[1] * 1.5)
    _pos, width, _h = dh.resize(decal, key, sign, far, *TOP, grid=16, keep_aspect=False)
    assert width % 16 == pytest.approx(0.0) or width % 16 == pytest.approx(16.0)


@pytest.mark.qt
def test_dragging_a_handle_in_the_front_view(main_window, monkeypatch):
    from PyQt5.QtCore import QEvent, QPointF, Qt
    from PyQt5.QtGui import QMouseEvent
    main_window.state.clear_scene()
    decal = _wall_decal(width=128.0, height=64.0)
    main_window.state.things.append(decal)
    main_window.set_selected_objects([decal])
    view = main_window.view_front
    view.resize(800, 600)
    view.snap_to_grid_enabled, view.grid_size = True, 16
    assert main_window.view_3d.decal_tool.keep_aspect      # on by default
    i1, i2, _ = view._axis_indices()
    _sign, (u, v) = _outermost(decal, 'width', (i1, i2), 0)     # its +x edge, at 64

    def mouse(kind, wu, wv, buttons=Qt.LeftButton):
        at = view.world_to_screen(QPointF(wu, wv))
        return QMouseEvent(kind, at, Qt.LeftButton, buttons, Qt.NoModifier)

    undo_before = len(main_window.state.undo_stack) if hasattr(main_window.state, 'undo_stack') else None
    view.mousePressEvent(mouse(QEvent.MouseButtonPress, u, v))
    assert view._decal_resize is not None
    view.mouseMoveEvent(mouse(QEvent.MouseMove, u + 30.0, v))
    view.mouseMoveEvent(mouse(QEvent.MouseMove, u + 63.0, v))     # to 127: snaps to 128
    view.mouseReleaseEvent(mouse(QEvent.MouseButtonRelease, u + 63.0, v, Qt.NoButton))
    assert view._decal_resize is None
    props = decal.properties
    assert props['width'] == pytest.approx(192.0)          # edge 64 -> 128 (snapped)
    assert props['height'] == pytest.approx(96.0)          # kept 2:1
    if undo_before is not None:
        assert len(main_window.state.undo_stack) == undo_before + 1   # one step
