"""A player that leaves the world comes back to the spawn.

Below y = -2000 the player used to be put at (0, 100, 0) -- wherever the map's
spawn was, and even on a map whose floor is below -2000. A NaN position went
on into every collision query. Now the kill height is worked out from the
level (LogicPlayer.kill_height: below its lowest geometry and spawn), a player
below it, or with a position or velocity that is not a number, goes back to
the spawn, and a player standing on something is never taken.
"""

import math

import glm
import pytest

pytest.importorskip("PyQt5", reason="drives the real editor state and logic thread")

from editor.editor_state import EditorState               # noqa: E402
from editor.things import PlayerStart                     # noqa: E402
from engine.logic_player import LogicPlayer               # noqa: E402
from engine.logic_thread import LogicThread               # noqa: E402
from engine.player import Player, _intersect_swept_sphere_triangle  # noqa: E402
from engine.threaded_game_state import ThreadedGameState  # noqa: E402
from tests.helpers.worlds import box_brush, make_thing    # noqa: E402

pytestmark = [pytest.mark.qt, pytest.mark.integration]

TICK = 1.0 / 60.0


@pytest.fixture
def world():
    made = []

    def _build(brushes, spawn=(300.0, 64.0, -200.0)):
        state = EditorState()
        state.brushes = list(brushes)
        state.things = [make_thing(PlayerStart, "spawn", spawn)]
        logic = LogicThread(ThreadedGameState(), state)
        made.append(logic)
        logic.player_runtime.player = Player(0.0, 0.0)
        logic.session_runtime.apply_play_mode(True)
        logic.session_runtime.stop_monster_ai()
        return logic

    yield _build
    for logic in made:
        logic.session_runtime.apply_play_mode(False)
        logic.stop()


def _tick(logic, ticks=1):
    for _ in range(ticks):
        logic.player_runtime.update_primary(TICK, set(), 0.0, 0.0)


def test_falling_off_the_level_respawns_at_the_spawn(world):
    logic = world([box_brush("floor", (0, -16, 0), (512, 32, 512))])
    kill = logic.player_runtime.kill_height()
    assert kill == pytest.approx(-32.0 - LogicPlayer.KILL_MARGIN)
    player = logic.player_runtime.player
    player.pos = glm.vec3(5000.0, 0.0, 5000.0)           # over nothing
    player.velocity = glm.vec3(0.0, 0.0, 0.0)
    for _ in range(60 * 10):
        _tick(logic)
        if player.pos.x != 5000.0:
            break
    assert (player.pos.x, player.pos.y, player.pos.z) == pytest.approx((300.0, 64.0, -200.0))
    assert tuple(player.velocity) == (0.0, 0.0, 0.0)


def test_a_level_below_minus_2000_is_not_a_void(world):
    """The old fixed clamp teleported a player walking on a floor at -3000."""
    floor = box_brush("deep", (0, -3016, 0), (2048, 32, 2048))
    logic = world([floor], spawn=(0.0, -2950.0, 0.0))
    assert logic.player_runtime.kill_height() < -3032.0
    player = logic.player_runtime.player
    player.pos = glm.vec3(0.0, -2950.0, 0.0)
    _tick(logic, 120)
    assert player.pos.y < -2900.0                         # still down there


def test_a_position_that_is_not_a_number_respawns(world):
    logic = world([box_brush("floor", (0, -16, 0), (512, 32, 512))])
    player = logic.player_runtime.player
    player.pos = glm.vec3(math.nan, 40.0, 0.0)
    _tick(logic)                                          # used to reach the grid with NaN
    assert all(math.isfinite(v) for v in player.pos)
    assert player.pos.x == pytest.approx(300.0)
    player.velocity = glm.vec3(0.0, math.inf, 0.0)
    _tick(logic)
    assert all(math.isfinite(v) for v in player.velocity)


def test_leave_void_spares_a_player_on_the_ground():
    player = Player(0.0, 0.0)
    player.kill_height = 0.0
    player.respawn_point = (1.0, 2.0, 3.0)
    player.pos = glm.vec3(0.0, -50.0, 0.0)
    player.on_ground = True
    assert not player.leave_void()
    player.on_ground = False
    assert player.leave_void()
    assert tuple(player.pos) == (1.0, 2.0, 3.0)


def test_without_a_level_the_old_clamp_and_point_stand():
    player = Player(0.0, 0.0)
    player.pos = glm.vec3(0.0, -1999.0, 0.0)
    assert not player.leave_void()
    player.pos = glm.vec3(0.0, -2001.0, 0.0)
    assert player.leave_void()
    assert tuple(player.pos) == Player.RESPAWN_DEFAULT


def test_kill_height_is_worked_out_once_per_world(world, monkeypatch):
    logic = world([box_brush("floor", (0, -16, 0), (512, 32, 512))])
    runtime = logic.player_runtime
    runtime.kill_height()
    import engine.brush_placement as placement
    calls = []
    real = placement.world_bounds
    monkeypatch.setattr(placement, "world_bounds", lambda b: calls.append(b) or real(b))
    for _ in range(10):
        runtime.kill_height()
    assert calls == []


def test_a_sphere_on_a_degenerate_triangles_edge_gets_a_finite_normal():
    """The hit point lands exactly on the edge (here its end, a vertex): the
    edge-to-centre vector is zero, and normalising it was NaN."""
    collinear = ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (20.0, 0.0, 0.0))
    hit, _t, _point, normal = _intersect_swept_sphere_triangle(
        glm.vec3(0.0, 3.0, 0.0), glm.vec3(0.0, -4.0, 0.0), 1.0, collinear, (0.0, 1.0, 0.0))
    assert hit
    assert all(math.isfinite(v) for v in normal)
    assert tuple(normal) == (0.0, 1.0, 0.0)
