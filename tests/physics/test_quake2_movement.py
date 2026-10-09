"""The player moves by Quake 2's rules, on Fio's one physics.

Collision is Fio's (the player controller's own resolution against the
spatial grid's colliders) and gravity has one authority, the live
PhysicsWorld -- the same source Big World and every falling thing use. On
top of that the movement rules are Quake 2's: speeds, acceleration and
friction per frame, the jump and its landing lockout, air stepping, ducking
and the steepest walkable slope.
"""

import math

import pytest

pytest.importorskip("glm", reason="the player is built on PyGLM vectors")

import glm                                                  # noqa: E402

from engine import brush_geometry as bg                     # noqa: E402
from engine.constants import (                              # noqa: E402
    CL_FORWARDSPEED, JUMP_STRENGTH, PM_ACCELERATE, PM_DUCKSPEED, PM_FRICTION,
    PM_LAND_TIME, PM_MAXSPEED, PM_STOPSPEED,
)
from engine.physics import SV_GRAVITY, PhysicsWorld, world_gravity  # noqa: E402
from engine.player import Player                            # noqa: E402
from tests.helpers.worlds import angled_brush, box_brush    # noqa: E402

DT = 1.0 / 60.0


def floor(top=0.0):
    return box_brush(name="floor", pos=(0.0, top - 20.0, 0.0), size=(4000.0, 40.0, 4000.0))


def frames(player, world, n, move=(0.0, 0.0), jump=False, crouch=False,
           sprint=False, gravity=None):
    move_dir = glm.vec3(move[0], 0.0, move[1])
    for _ in range(n):
        player.update(DT, move_dir, jump, crouch, world, sprint=sprint, gravity=gravity)
    return player


def standing(world, x=0.0, angle=math.pi / 2.0):
    """A player resting on *world*, facing +x."""
    p = Player(x, 0.0, angle=angle)
    p.pos.y = p._half.y + 1.0
    frames(p, world, 30)
    assert p.on_ground, "fixture is wrong: the player never landed"
    return p


def hspeed(p):
    return math.hypot(float(p.velocity.x), float(p.velocity.z))


def feet(p):
    return float(p.pos.y - p._half.y)


# ---------------------------------------------------------------------------
# One gravity
# ---------------------------------------------------------------------------

def test_the_physics_world_holds_quake_2s_gravity():
    assert SV_GRAVITY == 800.0
    assert float(PhysicsWorld.GRAVITY) == -800.0
    assert world_gravity() == -800.0


def test_the_player_falls_by_the_live_worlds_gravity():
    for g in (-800.0, -400.0):
        p = Player(0.0, 0.0)
        p.pos.y = 5000.0
        frames(p, [], 30, gravity=g)
        assert float(p.velocity.y) == pytest.approx(g * 30 * DT, rel=1e-3)


def test_monsters_and_dropped_props_fall_by_the_same_world():
    import engine.monster_constants as mc
    from engine import monster_ai, prop_runtime
    assert not hasattr(mc, "MONSTER_GRAVITY")
    assert not any(hasattr(cls, "DROP_GRAVITY") for cls in vars(prop_runtime).values()
                   if isinstance(cls, type))

    class _World:
        gravity = -123.0

    class _Session:
        physics_world = _World()

    class _Logic:
        session_runtime = _Session()

    ai = monster_ai.MonsterAI.__new__(monster_ai.MonsterAI)
    ai.lt = _Logic()
    assert ai._gravity() == -123.0


# ---------------------------------------------------------------------------
# Speeds, acceleration, friction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("move, sprint, expected", [
    ((0.0, 1.0), False, CL_FORWARDSPEED),                 # walk 200
    ((0.0, 1.0), True, PM_MAXSPEED),                      # run: 400 clamped to 300
    ((1.0, 1.0), False, math.hypot(200.0, 200.0)),        # walking diagonally: 283
    ((1.0, 1.0), True, PM_MAXSPEED),
], ids=["walk", "run", "walk-diagonal", "run-diagonal"])
def test_ground_speeds(move, sprint, expected):
    world = [floor()]
    p = standing(world)
    frames(p, world, 120, move=move, sprint=sprint)
    assert hspeed(p) == pytest.approx(expected, rel=1e-3)


def test_the_first_frame_of_a_walk_adds_accelerate_times_wish_speed():
    world = [floor()]
    p = standing(world)
    frames(p, world, 1, move=(0.0, 1.0))
    assert hspeed(p) == pytest.approx(PM_ACCELERATE * DT * CL_FORWARDSPEED, rel=1e-3)


def test_friction_takes_six_times_speed_per_second():
    world = [floor()]
    p = standing(world)
    frames(p, world, 120, move=(0.0, 1.0), sprint=True)
    before = hspeed(p)
    frames(p, world, 1)
    assert hspeed(p) == pytest.approx(before * (1.0 - PM_FRICTION * DT), rel=1e-3)


def test_below_stopspeed_friction_works_as_if_at_stopspeed():
    world = [floor()]
    p = standing(world)
    p.velocity.z = 50.0
    frames(p, world, 1)
    assert hspeed(p) == pytest.approx(50.0 - PM_STOPSPEED * PM_FRICTION * DT, rel=1e-3)


def test_looking_down_shortens_a_walk_by_a_third_of_the_pitch():
    world = [floor()]
    p = standing(world)
    p.pitch = -1.2
    frames(p, world, 120, move=(0.0, 1.0))
    assert hspeed(p) == pytest.approx(CL_FORWARDSPEED * math.cos(0.4), rel=1e-3)


# ---------------------------------------------------------------------------
# Jumping
# ---------------------------------------------------------------------------

def test_a_jump_peaks_about_46_units_up():
    world = [floor()]
    p = standing(world)
    start = feet(p)
    peak = start
    frames(p, world, 1, jump=True)
    for _ in range(60):
        frames(p, world, 1, jump=True)
        peak = max(peak, feet(p))
    ideal = JUMP_STRENGTH ** 2 / (2.0 * SV_GRAVITY)       # 45.6
    assert peak - start == pytest.approx(ideal, abs=3.0)


def test_a_hard_landing_locks_out_jumping_briefly():
    world = [floor()]
    p = Player(0.0, 0.0)
    p.pos.y = p._half.y + 120.0                           # lands at ~480 u/s
    for _ in range(60):
        frames(p, world, 1)
        if p.on_ground:
            break
    assert p.landing
    frames(p, world, 1, jump=True)
    assert float(p.velocity.y) <= 0.0, "jumped inside the landing lockout"
    frames(p, world, 1)                                   # release
    frames(p, world, int(PM_LAND_TIME * 2 / DT) + 1)
    frames(p, world, 1, jump=True)
    assert float(p.velocity.y) > 0.0


def test_a_soft_landing_does_not_lock_out_jumping():
    world = [floor()]
    p = Player(0.0, 0.0)
    p.pos.y = p._half.y + 10.0
    frames(p, world, 30)
    assert p.on_ground and not p.landing


def test_jump_pressed_in_the_air_fires_on_landing():
    """Only a jump that happened sets the latch, as in Quake 2."""
    world = [floor()]
    p = Player(0.0, 0.0)
    p.pos.y = p._half.y + 20.0
    frames(p, world, 1)
    jumped = False
    for _ in range(60):
        frames(p, world, 1, jump=True)
        jumped = jumped or float(p.velocity.y) > 100.0
    assert jumped


# ---------------------------------------------------------------------------
# Steps, slides, slopes
# ---------------------------------------------------------------------------

def _walk_at_ledge(height, jump=False, n=90):
    ledge = box_brush(name="ledge", pos=(1100.0, height * 0.5, 0.0),
                      size=(2000.0, height, 400.0))
    world = [floor(), ledge]
    p = standing(world)
    frames(p, world, n, move=(0.0, 1.0))
    if jump:
        frames(p, world, n, move=(0.0, 1.0), jump=True)
    return p, height


def test_an_18_unit_step_is_walked_up_and_a_20_unit_one_is_not():
    p, top = _walk_at_ledge(18.0)
    assert feet(p) == pytest.approx(top, abs=0.5)
    p, top = _walk_at_ledge(20.0)
    assert feet(p) < top


def test_a_jump_lands_on_a_ledge_a_step_above_its_peak():
    p, top = _walk_at_ledge(60.0, jump=True)
    assert feet(p) == pytest.approx(top, abs=0.5)
    p, top = _walk_at_ledge(70.0, jump=True)
    assert feet(p) < top


def _ramp(degrees):
    rad = math.radians(degrees)
    brush = angled_brush("ramp", pos=(200.0, 100.0, 0.0), size=(400.0, 200.0, 400.0),
                         clip_normal=(-math.sin(rad), math.cos(rad), 0.0), clip_offset=0.0)
    assert bg.build_collision_mesh(brush)
    return brush


def test_a_gentle_slope_is_ground_and_is_walked_up():
    world = [floor(), _ramp(30.0)]
    p = standing(world, x=-60.0)
    frames(p, world, 90, move=(0.0, 1.0))
    assert feet(p) > 40.0


def test_a_slope_steeper_than_45_degrees_is_not_ground():
    world = [floor(), _ramp(60.0)]
    p = Player(60.0, 0.0, angle=math.pi / 2.0)
    p.pos.y = 300.0
    for _ in range(120):
        frames(p, world, 1)
        if p.ground_object is not None and p.ground_object.get("name") == "ramp":
            pytest.fail("a 60 degree slope counted as ground")


# ---------------------------------------------------------------------------
# Ducking
# ---------------------------------------------------------------------------

def test_ducking_halves_the_hull_and_the_speed():
    world = [floor()]
    p = standing(world)
    before = feet(p)
    frames(p, world, 120, move=(0.0, 1.0), crouch=True, sprint=True)
    assert p.ducked
    assert p.height == pytest.approx(p.stand_height * 0.5)
    assert feet(p) == pytest.approx(before, abs=1.0)       # shrinks from the top
    assert hspeed(p) == pytest.approx(PM_DUCKSPEED, rel=1e-3)
    assert p.camera_height < p.stand_camera_height
    frames(p, world, 2)
    assert not p.ducked                                   # room to stand


def test_a_ducked_player_fits_under_a_low_ceiling_and_stays_ducked_there():
    ceiling = box_brush(name="ceiling", pos=(300.0, 70.0 + 100.0, 0.0),
                        size=(200.0, 200.0, 400.0))         # 70 units of headroom
    world = [floor(), ceiling]
    p = standing(world)
    frames(p, world, 240, move=(0.0, 1.0))
    assert float(p.pos.x) < 200.0 - 25.0 + 0.5, "walked under a 70-unit gap standing"

    p = standing(world)
    frames(p, world, 180, move=(0.0, 1.0), crouch=True)
    assert 200.0 < float(p.pos.x) < 400.0
    frames(p, world, 2)                                    # let go of crouch
    assert p.ducked, "stood up into the ceiling"
