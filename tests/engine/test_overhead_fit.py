"""The overhead camera's ground footprint, and an AI that thinks less often
about monsters off screen.

The AI's camera-derived behaviour -- leaving parked monsters out, throttling
off-screen ones -- runs only inside a Big World session that publishes the
overhead screen's box (``sim_view_rect``). With Big World off, or with a
first-person camera, every monster runs every tick exactly as before.
"""

import math
import types

import glm
import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="engine.logic_thread imports the editor tier")

from engine.logic_thread import LogicThread               # noqa: E402
from engine.monster_ai import MonsterAI                   # noqa: E402
from engine.spatial import SIM_TIER_KEY, TIER_DORMANT     # noqa: E402

TICK = 1.0 / 30.0


class _Camera:
    """What overhead_ground_footprint reads off the logic thread."""
    overhead_ground_footprint = LogicThread.overhead_ground_footprint
    _overhead_camera = LogicThread._overhead_camera
    _safe_up = staticmethod(LogicThread._safe_up)
    set_frustum_fov = LogicThread.set_frustum_fov
    effective_overhead_height = LogicThread.effective_overhead_height

    def __init__(self, aspect=16 / 9, height=800.0, tilt=0.0, overhead=True,
                 orientation="north", fov=90.0):
        self.frustum_aspect = aspect
        self.frustum_fov = fov
        self.overhead_height = height
        self.overhead_height_limit = None
        self.overhead_tilt = tilt
        self.overhead_orientation = orientation
        self.player = types.SimpleNamespace(pos=glm.vec3(100.0, 50.0, -40.0), angle=0.0)
        self._overhead = overhead

    def is_overhead(self):
        return self._overhead


def test_a_straight_down_camera_shows_height_by_aspect():
    hx, hz, reach = _Camera(aspect=16 / 9).overhead_ground_footprint()
    # A 90 degree vertical field of view: half-height equals the camera height.
    assert hz == pytest.approx(800.0, rel=1e-4)
    assert hx == pytest.approx(800.0 * 16 / 9, rel=1e-4)
    assert reach == pytest.approx(math.hypot(hx, hz), rel=1e-4)


def test_the_footprint_scales_with_the_camera_height():
    low = _Camera(height=400.0).overhead_ground_footprint()
    high = _Camera(height=1200.0).overhead_ground_footprint()
    assert high[0] == pytest.approx(3 * low[0], rel=1e-4)


def test_no_footprint_when_not_overhead_or_looking_at_the_horizon():
    assert _Camera(overhead=False).overhead_ground_footprint() is None
    assert _Camera(tilt=60.0).overhead_ground_footprint() is None


def test_the_footprint_follows_the_field_of_view_the_view_draws_with():
    cam = _Camera()
    narrow = cam.overhead_ground_footprint()
    cam.set_frustum_fov(110.0)
    wide = cam.overhead_ground_footprint()
    assert wide[1] == pytest.approx(800.0 * math.tan(math.radians(55.0)), rel=1e-4)
    assert wide[2] > narrow[2]
    for junk in (None, "wide", 0.0, 180.0):
        cam.set_frustum_fov(junk)
        assert cam.frustum_fov == 110.0


def test_a_turning_camera_moves_the_box_but_not_the_reach():
    cam = _Camera(orientation="player", tilt=20.0)
    seen = []
    for degrees in range(0, 360, 15):
        cam.player.angle = math.radians(degrees)
        seen.append(cam.overhead_ground_footprint())
    reaches = [r for _hx, _hz, r in seen]
    assert max(reaches) - min(reaches) < 1e-3 * reaches[0]
    assert len({round(hx) for hx, _hz, _r in seen}) > 1


# ---------------------------------------------------------------------------
# The AI's off-screen throttle
# ---------------------------------------------------------------------------

RECT = (500.0, 300.0)
PLAYER = (0.0, 0.0, 0.0)


def _thing(x=0.0, z=0.0, tier=None):
    props = {} if tier is None else {SIM_TIER_KEY: tier}
    return types.SimpleNamespace(properties=props, pos=[x, 0.0, z])


def _ai(bigworld, overhead):
    """An AI whose host has (or has not) a Big World session, fitted (or not)
    to an overhead camera."""
    ai = MonsterAI.__new__(MonsterAI)
    ai.lt = types.SimpleNamespace(_bigworld=object() if bigworld else None,
                                  sim_view_rect=RECT if (bigworld and overhead) else None)
    ai._tick_monsters = None
    ai._offscreen_accum = 0.0
    ai._owed = {}
    return ai


def _run(ai, monsters, ticks, dt=TICK, move=None):
    """Drive the throttle; return each monster's [delta, ...] runs."""
    runs = [[] for _ in monsters]
    for tick in range(ticks):
        if move:
            move(tick)
        row_dt, sit = ai._offscreen_rows(monsters, dt, PLAYER, ai._view_rect())
        for i in range(len(monsters)):
            if row_dt is None:
                runs[i].append(dt)
            elif not sit[i]:
                runs[i].append(float(row_dt[i]))
    return runs


def test_off_screen_monsters_run_every_interval_with_the_time_they_sat_out():
    ai = _ai(bigworld=True, overhead=True)
    on, off = _thing(10.0, 10.0), _thing(900.0, 0.0)
    runs = _run(ai, [on, off], 60)                # two seconds at 30 Hz
    assert runs[0] == [TICK] * 60                 # on screen: every tick
    # Off screen: every sixth tick (not seventh), each carrying 0.2 s.
    assert len(runs[1]) == 10
    assert runs[1] == pytest.approx([0.2] * 10)


def test_every_monster_covers_exactly_the_time_that_passed():
    ai = _ai(bigworld=True, overhead=True)
    walker, leaver = _thing(900.0, 0.0), _thing(0.0, 0.0)

    def move(tick):
        walker.pos[0] = 900.0 - 25.0 * tick       # walks on screen at tick 17
        leaver.pos[2] = 12.0 * tick               # walks off at tick 26

    ticks = 50
    runs = _run(ai, [walker, leaver], ticks, move=move)
    owed = {id(m): t for m, t in ai._owed.values()}
    for monster, ran in zip((walker, leaver), runs):
        assert sum(ran) + owed.get(id(monster), 0.0) == pytest.approx(ticks * TICK)


def test_a_monster_that_walks_on_screen_runs_from_the_next_tick():
    ai = _ai(bigworld=True, overhead=True)
    walker = _thing(900.0, 0.0)
    _run(ai, [walker], 2)                         # sits out, owed 2 ticks
    walker.pos[0] = 100.0
    row_dt, sit = ai._offscreen_rows([walker], TICK, PLAYER, RECT)
    assert not sit[0] and row_dt[0] == pytest.approx(3 * TICK)
    # ... and from then on it is an ordinary tick.
    assert ai._offscreen_rows([walker], TICK, PLAYER, RECT) == (None, None)


def test_a_screen_with_nothing_off_it_is_the_plain_tick():
    ai = _ai(bigworld=True, overhead=True)
    assert ai._offscreen_rows([_thing(), _thing(50.0)], 0.0, PLAYER, RECT) == (None, None)


@pytest.mark.parametrize("bigworld,overhead", [(False, False), (False, True), (True, False)])
def test_without_big_world_and_an_overhead_camera_nothing_changes(bigworld, overhead):
    ai = _ai(bigworld, overhead)
    # Even a box left on the host is ignored without a live session.
    ai.lt.sim_view_rect = RECT if overhead else None
    assert ai._view_rect() is None
    monsters = [_thing(900.0), _thing(tier=TIER_DORMANT)]
    assert ai._offscreen_rows(monsters, TICK, PLAYER, ai._view_rect()) == (None, None)


def test_the_fitted_gate_is_the_published_box():
    assert _ai(bigworld=True, overhead=True)._view_rect() == RECT


def test_the_throttle_holds_nothing_once_unfitted():
    ai = _ai(bigworld=True, overhead=True)
    _run(ai, [_thing(900.0)], 3)
    assert ai._owed
    ai._offscreen_rows([_thing(900.0)], TICK, PLAYER, None)
    assert ai._owed == {} and ai._offscreen_accum == 0.0


def test_row_deltas_are_float64():
    ai = _ai(bigworld=True, overhead=True)
    row_dt, sit = ai._offscreen_rows([_thing(900.0), _thing()], TICK, PLAYER, RECT)
    assert row_dt.dtype == np.float64 and sit.tolist() == [True, False]


# ---------------------------------------------------------------------------
# The overhead camera's height ceiling
# ---------------------------------------------------------------------------

class _CeilingCamera(_Camera):
    def __init__(self, limit=None, **kw):
        super().__init__(**kw)
        self.overhead_height_limit = limit


def test_the_camera_floats_no_higher_than_its_ceiling():
    cam = _CeilingCamera(height=5000.0, limit=2048.0)
    assert cam.effective_overhead_height() == 2048.0
    pos, _d, _u = cam._overhead_camera(cam.player.pos, 0.0)
    assert pos.y - cam.player.pos.y == pytest.approx(2048.0)
    # The footprint is the one the held camera shows, not the asked-for one.
    assert cam.overhead_ground_footprint()[1] == pytest.approx(2048.0, rel=1e-4)
    # The authored height is left alone, and a lower one is not raised.
    assert cam.overhead_height == 5000.0
    assert _CeilingCamera(height=600.0, limit=2048.0).effective_overhead_height() == 600.0


def test_no_ceiling_is_the_authored_height():
    assert _CeilingCamera(height=5000.0).effective_overhead_height() == 5000.0


@pytest.mark.parametrize("entering", [True, False])
def test_a_play_mode_change_drops_the_ceiling(entering):
    from editor.editor_state import EditorState
    from engine.threaded_game_state import ThreadedGameState
    logic = LogicThread(ThreadedGameState(), EditorState())
    try:
        logic.overhead_height_limit = 1024.0
        logic._apply_play_mode(entering)
        assert logic.overhead_height_limit is None
    finally:
        logic._apply_play_mode(False)
        logic.stop()
