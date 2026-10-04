"""Clicking an actor in Play Mode: the dense pick and the view's pick mode.

``engine.actor_pick.pick_actor`` answers "which actor is under this ray" from
the published tables the frame was drawn from; ``QtGameView.begin_actor_pick``
arms a one-shot pick that pauses the world, frees the cursor and, on a click
on an actor, hands it over -- by default to the Entity Inspector.

The first half builds real ``EntityTable`` / ``RenderTable`` projections from
real Monsters and brushes, so the column contract the picker reads is the one
the renderer publishes. The second half drives the view's pick mode on a light
host that borrows ``QtGameView``'s methods (a real view needs a GL context).
"""

import types

import glm
import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="the entity classes live in editor.things")

from PyQt5.QtCore import QEvent, QPoint, Qt          # noqa: E402
from PyQt5.QtGui import QKeyEvent, QMouseEvent       # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget    # noqa: E402

from editor.things import Light, Monster             # noqa: E402
from engine.actor_pick import NO_SLOT, nearest_wall, pick_actor  # noqa: E402
from engine.entity_table import EntityTable          # noqa: E402
from engine.logic_session import LogicSession          # noqa: E402
from engine.qt_game_view import QtGameView           # noqa: E402
from engine.render_table import RenderTable          # noqa: E402
from tests.helpers.worlds import box_brush, make_thing  # noqa: E402

pytestmark = pytest.mark.qt

DOWN = (0.0, -1.0, 0.0)
EAST = (1.0, 0.0, 0.0)


def _actor(name, pos, w=128, h=128, **props):
    return make_thing(Monster, name, pos, sprite_width=w, sprite_height=h, **props)


def _tables(things=(), brushes=()):
    entities = EntityTable()
    entities.begin_frame(list(things), 1)
    walls = RenderTable()
    walls.begin_frame(list(brushes), 1)
    return entities, walls


def _picked(things, brushes, origin, direction):
    entities, walls = _tables(things, brushes)
    slot = pick_actor(origin, direction, entities, walls)
    return None if slot == NO_SLOT else entities.things[slot]


# ---------------------------------------------------------------------------
# pick_actor
# ---------------------------------------------------------------------------

def test_a_ray_through_an_actor_picks_it():
    npc = _actor("npc", (500, 64, 0))
    assert _picked([npc], [], (0, 64, 0), EAST) is npc


def test_a_ray_that_passes_by_picks_nothing():
    npc = _actor("npc", (500, 64, 0))
    assert _picked([npc], [], (0, 64, 300), EAST) is None


def test_the_pick_sphere_matches_the_billboard_that_was_drawn():
    """A 128x192 sprite reaches 96 units from its centre: aiming at the head
    of a tall sprite hits it, just past the top does not."""
    tall = _actor("tall", (500, 100, 0), w=128, h=192)
    assert _picked([tall], [], (0, 100 + 90, 0), EAST) is tall
    assert _picked([tall], [], (0, 100 + 100, 0), EAST) is None


def test_a_tiny_sprite_is_still_clickable():
    tiny = _actor("tiny", (500, 64, 0), w=8, h=8)
    assert _picked([tiny], [], (0, 64 + 20, 0), EAST) is tiny


def test_the_floor_under_an_actor_never_wins_the_click():
    """Brushes only occlude: looking down at an actor standing on a floor."""
    npc = _actor("npc", (0, 64, 0))
    floor = box_brush("floor", (0, -16, 0), (2048, 32, 2048))
    assert _picked([npc], [floor], (0, 1000, 0), DOWN) is npc


def test_an_actor_behind_a_wall_is_not_clickable():
    npc = _actor("npc", (500, 64, 0))
    wall = box_brush("wall", (250, 64, 0), (32, 512, 512))
    assert _picked([npc], [wall], (0, 64, 0), EAST) is None


@pytest.mark.parametrize("props", [
    {"is_trigger": True}, {"hidden": True}, {"shader": "Glass"},
    {"operation": "subtract"},
])
def test_only_visible_solid_brushes_occlude(props):
    npc = _actor("npc", (500, 64, 0))
    volume = box_brush("volume", (250, 64, 0), (32, 512, 512), **props)
    assert _picked([npc], [volume], (0, 64, 0), EAST) is npc


def test_a_turned_brush_occludes_where_it_is_drawn():
    """A swung door or a spinning mover: rotation about the brush's centre,
    in degrees, as the renderer draws it."""
    actor = _actor("a", [600.0, 64.0, 200.0])
    # 16 wide on X, 512 long on Z: square across the ray at z=200 ...
    door = box_brush("door", (300.0, 64.0, 0.0), (16.0, 128.0, 512.0))
    assert _picked([actor], [door], (0.0, 64.0, 200.0), EAST) is None
    # ... swung 90 degrees it lies along X at z ~ 0, out of the ray's way ...
    door['rot_axis'] = [0.0, 1.0, 0.0]
    door['_rot_angle'] = 90.0
    assert _picked([actor], [door], (0.0, 64.0, 200.0), EAST) is actor
    # ... and now blocks a ray it used to miss.
    behind = _actor("b", [300.0, 64.0, 600.0])
    assert _picked([behind], [door], (300.0, 64.0, -600.0), (0.0, 0.0, 1.0)) is None


def test_a_brush_around_the_eye_does_not_occlude():
    npc = _actor("npc", (500, 64, 0))
    room = box_brush("room", (0, 64, 0), (256, 256, 256))
    assert _picked([npc], [room], (0, 64, 0), EAST) is npc


def test_the_nearest_of_two_actors_wins():
    near = _actor("near", (300, 64, 0))
    far = _actor("far", (700, 64, 0))
    assert _picked([far, near], [], (0, 64, 0), EAST) is near


def test_a_hidden_actor_is_not_pickable():
    """Big World parks through ``hidden``: a parked actor is not there."""
    npc = _actor("npc", (500, 64, 0), hidden=True)
    assert _picked([npc], [], (0, 64, 0), EAST) is None


def test_scenery_is_not_an_actor():
    lamp = make_thing(Light, "lamp", (500, 64, 0))
    assert _picked([lamp], [], (0, 64, 0), EAST) is None


def test_an_actor_behind_the_eye_is_not_picked():
    npc = _actor("npc", (-500, 64, 0))
    assert _picked([npc], [], (0, 64, 0), EAST) is None


def test_the_eye_inside_an_actors_sphere_still_picks_it():
    npc = _actor("npc", (10, 64, 0))
    assert _picked([npc], [], (0, 64, 0), EAST) is npc


def test_nearest_wall_handles_a_ray_parallel_to_an_axis():
    entities, walls = _tables(brushes=[box_brush("w", (250, 64, 0), (32, 64, 64))])
    assert nearest_wall((0, 64, 0), EAST, walls) == pytest.approx(234.0)
    assert nearest_wall((0, 500, 0), EAST, walls) == np.inf


def test_no_tables_pick_nothing():
    assert pick_actor((0, 0, 0), EAST, None) == NO_SLOT
    entities, _walls = _tables()
    assert pick_actor((0, 0, 0), EAST, entities, None) == NO_SLOT


# ---------------------------------------------------------------------------
# The view's pick mode
# ---------------------------------------------------------------------------

class _Logic:
    def __init__(self):
        self.session_runtime = LogicSession(self)


class _GameState:
    """Hands out a render state and checks every borrow is returned."""

    def __init__(self, render_state):
        self.render_state = render_state
        self.borrowed = 0

    def get_render_state(self):
        self.borrowed += 1
        return self.render_state

    def release_render_state(self, snapshot):
        assert snapshot is self.render_state
        self.borrowed -= 1


class _Editor:
    def __init__(self):
        self.inspected = []

    def show_entity_inspector(self, entity):
        self.inspected.append(entity)


class PickView(QWidget):
    """The slice of QtGameView pick mode touches, with its real methods."""

    ACTOR_PICK_PAUSE = QtGameView.ACTOR_PICK_PAUSE
    actor_pick_active = QtGameView.actor_pick_active
    begin_actor_pick = QtGameView.begin_actor_pick
    cancel_actor_pick = QtGameView.cancel_actor_pick
    _end_actor_pick = QtGameView._end_actor_pick
    _pick_ray = QtGameView._pick_ray
    actor_at = QtGameView.actor_at
    _update_actor_pick_hover = QtGameView._update_actor_pick_hover
    _click_actor_pick = QtGameView._click_actor_pick
    _open_inspector_for = QtGameView._open_inspector_for
    _show_pick_cursor = QtGameView._show_pick_cursor
    _capture_play_cursor = QtGameView._capture_play_cursor
    _close_console_overlay = QtGameView._close_console_overlay
    keyPressEvent = QtGameView.keyPressEvent
    mousePressEvent = QtGameView.mousePressEvent

    def __init__(self, things=(), brushes=(), eye=(0.0, 64.0, 0.0), target=(1.0, 64.0, 0.0)):
        super().__init__()
        self.resize(200, 200)
        self.play_mode = True
        self.console_overlay_active = False
        self._console_input = QWidget()
        self._actor_pick = None
        self.actor_pick_hover = None
        self.logic_thread = _Logic()
        entities, walls = _tables(things, brushes)
        self.game_state = _GameState(types.SimpleNamespace(
            entity_table=entities, render_table=walls))
        self.editor = _Editor()
        self.projection_matrix = glm.perspective(glm.radians(60.0), 1.0, 1.0, 10000.0)
        self.view_matrix = glm.lookAt(glm.vec3(*eye), glm.vec3(*target), glm.vec3(0, 1, 0))
        self.last_mouse_pos = QPoint(0, 0)


@pytest.fixture
def view(qt_app):
    created = []

    def build(*args, **kwargs):
        v = PickView(*args, **kwargs)
        created.append(v)
        return v

    yield build
    while QApplication.overrideCursor() is not None:
        QApplication.restoreOverrideCursor()
    for v in created:
        v.deleteLater()


def _centre(v):
    return v.width() // 2, v.height() // 2


def _press(v, button, x=None, y=None):
    cx, cy = _centre(v)
    pos = QPoint(cx if x is None else x, cy if y is None else y)
    v.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, pos, button, button,
                                  Qt.NoModifier))


def _key(v, key):
    v.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))


def test_arming_a_pick_pauses_the_world_and_frees_the_cursor(view):
    v = view()
    assert v.begin_actor_pick() is True
    assert v.actor_pick_active
    assert v.logic_thread.session_runtime.world_pause_owners() == {QtGameView.ACTOR_PICK_PAUSE}
    assert QApplication.overrideCursor().shape() == Qt.CrossCursor


def test_there_is_no_pick_outside_play_mode(view):
    v = view()
    v.play_mode = False
    assert v.begin_actor_pick() is False
    assert not v.actor_pick_active and not v.logic_thread.session_runtime.world_pause_owners()


def test_clicking_an_actor_opens_the_inspector_and_resumes(view):
    npc = _actor("npc", (500, 64, 0))
    v = view([npc])
    v.begin_actor_pick()
    _press(v, Qt.LeftButton)
    assert v.editor.inspected == [npc]
    assert not v.actor_pick_active
    assert not v.logic_thread.session_runtime.world_pause_owners(), "the world stayed paused after the pick"
    assert QApplication.overrideCursor().shape() == Qt.BlankCursor
    assert v.game_state.borrowed == 0, "the render state was not released"


def test_a_pick_can_hand_the_actor_to_its_own_handler(view):
    npc = _actor("npc", (500, 64, 0))
    v = view([npc])
    chosen = []
    v.begin_actor_pick(chosen.append)
    _press(v, Qt.LeftButton)
    assert chosen == [npc] and v.editor.inspected == []


def test_a_click_on_nothing_keeps_the_pick_armed(view):
    v = view([_actor("npc", (500, 64, 0))])
    v.begin_actor_pick()
    _press(v, Qt.LeftButton, 5, 5)
    assert v.actor_pick_active
    assert v.logic_thread.session_runtime.world_pause_owners() == {QtGameView.ACTOR_PICK_PAUSE}
    assert v.editor.inspected == []


@pytest.mark.parametrize("cancel", ["escape", "right_click"])
def test_a_pick_can_be_cancelled(view, cancel):
    v = view([_actor("npc", (500, 64, 0))])
    v.begin_actor_pick()
    if cancel == "escape":
        _key(v, Qt.Key_Escape)
    else:
        _press(v, Qt.RightButton)
    assert not v.actor_pick_active
    assert not v.logic_thread.session_runtime.world_pause_owners()
    assert v.editor.inspected == []


def test_hovering_names_the_actor_and_repaints_only_on_change(view, monkeypatch):
    npc = _actor("npc", (500, 64, 0))
    v = view([npc])
    repaints = []
    monkeypatch.setattr(v, "update", lambda: repaints.append(1))
    v.begin_actor_pick()
    repaints.clear()
    v._update_actor_pick_hover(*_centre(v))
    assert v.actor_pick_hover is npc and len(repaints) == 1
    v._update_actor_pick_hover(*_centre(v))
    assert len(repaints) == 1
    v._update_actor_pick_hover(5, 5)
    assert v.actor_pick_hover is None and len(repaints) == 2


def test_the_ray_leaves_the_camera_the_frame_was_drawn_from(view):
    """Not the editor camera: in Play Mode the view matrix is the player's."""
    v = view(eye=(100.0, 300.0, -40.0), target=(100.0, 0.0, -39.0))
    origin, direction = v._pick_ray(*_centre(v))
    assert glm.distance(origin, glm.vec3(100.0, 300.0, -40.0)) < 1e-3
    assert direction.y < -0.99


def test_an_overhead_camera_picks_the_actor_below_the_cursor(view):
    npc = _actor("npc", (0, 64, 0))
    floor = box_brush("floor", (0, -16, 0), (2048, 32, 2048))
    v = view([npc], [floor], eye=(0.0, 1200.0, 1.0), target=(0.0, 0.0, 0.0))
    v.begin_actor_pick()
    _press(v, Qt.LeftButton)
    assert v.editor.inspected == [npc]


def test_closing_the_console_overlay_keeps_an_armed_picks_cursor(view):
    """'inspect' is typed into the overlay, which closes right after it runs."""
    v = view()
    v.console_overlay_active = True
    v.begin_actor_pick()
    v._close_console_overlay()
    assert QApplication.overrideCursor().shape() == Qt.CrossCursor
    assert v.actor_pick_active


def test_the_main_window_arms_the_views_pick(qt_app):
    from editor.main_window import MainWindow
    armed = []
    host = types.SimpleNamespace(view_3d=types.SimpleNamespace(
        begin_actor_pick=lambda on_pick=None: armed.append(on_pick) or True))
    assert MainWindow.begin_actor_pick(host) is True
    assert armed == [None]
    assert MainWindow.begin_actor_pick(types.SimpleNamespace(view_3d=None)) is False
