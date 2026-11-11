"""Directional damage indicators: where a hit on the player came from.

An attacker names its position before it applies damage; the logic thread
publishes the recent ones with what is left of each; the HUD turns each into
a bearing under the camera it draws with -- first person or overhead."""

import math

import glm
import pytest

pytest.importorskip("PyQt5")

pytestmark = pytest.mark.qt

from engine.view_hud import ViewHud

MAP = "maps/Simple_Map_Test_Extended.json"
PLAYER = (100.0, 40.0, -50.0)


def _bearing(view, offset):
    source = (PLAYER[0] + offset[0], PLAYER[1], PLAYER[2] + offset[1])
    return math.degrees(ViewHud.damage_bearing(view, PLAYER, source))


def _first_person(forward):
    eye = glm.vec3(PLAYER) + glm.vec3(0, 60, 0)
    return glm.lookAt(eye, eye + glm.vec3(forward), glm.vec3(0, 1, 0))


@pytest.mark.parametrize("pitch", [0.0, -0.9, 0.7])
def test_first_person_bearing_is_relative_to_where_the_player_looks(pitch):
    # Looking along +x (and up or down): +z is to the right.
    forward = (math.cos(pitch), math.sin(pitch), 0.0)
    view = _first_person(forward)
    assert _bearing(view, (300, 0)) == pytest.approx(0.0, abs=1e-3)       # ahead
    assert _bearing(view, (0, 300)) == pytest.approx(90.0, abs=1e-3)      # right
    assert _bearing(view, (0, -300)) == pytest.approx(-90.0, abs=1e-3)    # left
    assert abs(_bearing(view, (-300, 0))) == pytest.approx(180.0, abs=1e-3)


@pytest.mark.parametrize("tilt", [0.0, 35.0, 70.0])
def test_overhead_bearing_is_the_direction_on_screen(tilt):
    # North (-z) up the screen, the camera above and, tilted, to the south.
    t = math.radians(tilt)
    target = glm.vec3(PLAYER)
    eye = target + glm.vec3(0.0, 800.0 * math.cos(t), 800.0 * math.sin(t))
    view = glm.lookAt(eye, target, glm.vec3(0, 0, -1))
    assert _bearing(view, (0, -300)) == pytest.approx(0.0, abs=1e-3)      # north: up
    assert _bearing(view, (300, 0)) == pytest.approx(90.0, abs=1e-3)      # east: right
    assert _bearing(view, (-300, 0)) == pytest.approx(-90.0, abs=1e-3)
    assert abs(_bearing(view, (0, 300))) == pytest.approx(180.0, abs=1e-3)


def test_a_source_on_the_player_has_no_bearing():
    assert ViewHud.damage_bearing(_first_person((1, 0, 0)), PLAYER, PLAYER) is None


@pytest.mark.qt
@pytest.mark.gl
@pytest.mark.integration
def test_a_hit_with_a_source_is_published_and_one_without_is_not(fio_session):
    session = fio_session(MAP, size=(200, 160))
    session.start_play()
    session.step(2)
    logic = session.logic
    runtime = logic.player_runtime
    x, y, z = (float(c) for c in runtime.player.pos)

    logic.trigger_runtime._apply_player_damage(5)          # a hurt trigger: no side
    assert runtime.damage_marks == []

    runtime.damage_source = (x + 400.0, y, z)
    logic.trigger_runtime._apply_player_damage(5)
    assert runtime.damage_source is None                   # taken by that hit
    assert [m[:3] for m in runtime.damage_marks] == [[x + 400.0, y, z]]

    session.step(2)
    marks = session.view.game_state.published('damage_marks')
    assert len(marks) == 1 and marks[0][:3] == (x + 400.0, y, z)
    assert 0.0 < marks[0][3] <= 1.0

    # Old marks are dropped, and the published tuple empties.
    runtime.damage_marks[0][3] -= logic.render_runtime.DAMAGE_MARK_TIME + 1.0
    session.step(2)
    assert session.view.game_state.published('damage_marks') == ()
    assert runtime.damage_marks == []


@pytest.mark.qt
@pytest.mark.gl
@pytest.mark.integration
def test_god_mode_takes_the_source_without_marking(fio_session):
    session = fio_session(MAP, size=(200, 160))
    session.start_play()
    session.step(2)
    runtime = session.logic.player_runtime
    runtime.god_mode = True
    runtime.damage_source = (1.0, 2.0, 3.0)
    session.logic.trigger_runtime._apply_player_damage(5)
    assert runtime.damage_source is None and runtime.damage_marks == []


@pytest.mark.parametrize("degrees, expected", [
    (0, (0.0, -290.0)), (90, (390.0, 0.0)), (180, (0.0, 290.0)), (-90, (-390.0, 0.0)),
])
def test_an_indicator_sits_on_the_edge_of_the_view(degrees, expected):
    x, y = ViewHud.damage_edge_point(math.radians(degrees), 800, 600, 10)
    assert (x, y) == pytest.approx(expected, abs=1e-6)


def test_a_diagonal_indicator_stays_inside_the_view():
    for degrees in range(0, 360, 7):
        x, y = ViewHud.damage_edge_point(math.radians(degrees), 800, 600, 10)
        assert abs(x) <= 390.0 + 1e-6 and abs(y) <= 290.0 + 1e-6
        assert abs(x) == pytest.approx(390.0) or abs(y) == pytest.approx(290.0)
