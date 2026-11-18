"""The scissor (clip) and rotate tools snap to the grid, and [ / ] change the
grid -- the tools' 'sensitivity' -- without leaving them.

[ and ] used to repaint through update_views(), which resets each 2D view: a
half-placed cut lost its first point and a rotate drag was dropped. The
scissor's preview line followed the raw cursor, and rotation always stepped
15 degrees whatever the grid.
"""

import math

import pytest

pytest.importorskip("PyQt5")

from PyQt5.QtCore import QPointF, Qt  # noqa: E402
from PyQt5.QtTest import QTest  # noqa: E402

from editor.view_2d import ROTATE_STEP_FOR_GRID, rotate_step_for_grid  # noqa: E402
from tests.editor.test_view2d_component_interaction import (  # noqa: E402,F401
    editor, move, press, release, select_box)

pytestmark = pytest.mark.qt


def _bracket(host, key):
    QTest.keyClick(host.view_top, key)


def test_the_scissor_preview_snaps_to_the_grid(editor):
    host, view = editor
    select_box(host)
    host.clip_mode = True
    move(view, (37, 21), buttons=Qt.NoButton)
    assert (view.clip_hover.x(), view.clip_hover.y()) == (32, 16)   # grid 16


def test_brackets_change_the_scissor_grid_mid_cut(editor):
    host, view = editor
    select_box(host)
    host.clip_mode = True
    press(view, (37, 21))
    release(view, (37, 21))
    assert [(p.x(), p.y()) for p in view.clip_points] == [(32, 16)]
    move(view, (99, 51), buttons=Qt.NoButton)
    assert (view.clip_hover.x(), view.clip_hover.y()) == (96, 48)

    _bracket(host, Qt.Key_BracketRight)                 # grid 16 -> 32
    assert view.grid_size == 32
    assert host.clip_mode and len(view.clip_points) == 1    # the cut goes on
    assert (view.clip_hover.x(), view.clip_hover.y()) == (96, 64)   # re-snapped

    _bracket(host, Qt.Key_BracketLeft)
    _bracket(host, Qt.Key_BracketLeft)                  # 32 -> 8
    assert view.grid_size == 8
    assert (view.clip_hover.x(), view.clip_hover.y()) == (96, 48)
    press(view, (99, 51))
    release(view, (99, 51))
    assert [(p.x(), p.y()) for p in view.clip_points] == [(32, 16), (96, 48)]


def test_the_rotate_step_follows_the_grid():
    assert rotate_step_for_grid(16) == 15.0             # the default, as before
    assert rotate_step_for_grid(2) < rotate_step_for_grid(8) < rotate_step_for_grid(128)
    for step in ROTATE_STEP_FOR_GRID.values():
        assert (360.0 / step).is_integer()               # whole turns land on 0


def _angle(view):
    return view.rotate_applied


def test_brackets_change_the_rotate_step_mid_drag(editor):
    host, view = editor
    select_box(host)
    host.rotate_mode = True
    view.rotate_snap_deg = None                          # the grid's step
    press(view, (64, 0))                                 # grab at 0 degrees
    r = 64.0
    at = (r * math.cos(math.radians(38)), r * math.sin(math.radians(38)))
    move(view, at)
    assert _angle(view) == pytest.approx(45.0) or _angle(view) == pytest.approx(30.0)
    assert abs(_angle(view) - 38) <= 7.5                 # 15-degree steps (grid 16)

    _bracket(host, Qt.Key_BracketLeft)                   # grid 8: 10-degree steps
    assert view.rotate_dragging and host.rotate_mode     # the drag goes on
    assert _angle(view) == pytest.approx(40.0)           # re-snapped at once

    _bracket(host, Qt.Key_BracketLeft)                   # grid 4: 5-degree steps
    assert _angle(view) == pytest.approx(40.0)
    _bracket(host, Qt.Key_BracketLeft)                   # grid 2: 1-degree steps
    assert _angle(view) == pytest.approx(38.0, abs=0.6)
    release(view, at)
    assert not view.rotate_dragging


def test_the_rotate_banner_shows_the_step(editor):
    host, view = editor
    select_box(host)
    host.rotate_mode = True
    view.rotate_snap_deg = None
    view.grid_size = 64
    assert view.rotate_snap_deg == 45.0
