"""The player moves as Quake 2's player does.

Each test drives the real ``Player.update`` and checks a rule or number of
Quake 2's ``qcommon/pmove.c`` (and the client and server values that feed it):
speeds, acceleration and friction per frame, the jump, stepping, sliding,
slopes, the hard-landing jump lockout, ducking and swimming. ``engine.pmove``
is the box trace all of it stands on, so it is tested directly too.
"""

import math

import pytest

pytest.importorskip("glm", reason="the player is built on PyGLM vectors")

import glm                                                  # noqa: E402

from engine import brush_geometry as bg                     # noqa: E402
from engine.constants import (                              # noqa: E402
    SV_GRAVITY, PM_JUMP_SPEED, PM_MAXSPEED, PM_DUCKSPEED, PM_STEPSIZE,
    PM_ACCELERATE, PM_FRICTION, PM_STOPSPEED, CL_FORWARDSPEED,
    PM_LAND_TIME,
)
from engine.player import Player                            # noqa: E402
from engine.pmove import BoxTracer                          # noqa: E402
from tests.helpers.worlds import angled_brush, box_brush    # noqa: E402

DT = 1.0 / 60.0
FLOOR_TOP = 0.0


def floor(name="floor", top=FLOOR_TOP, **props):
    return box_brush(name=name, pos=(0.0, top - 20.0, 0.0),
                     size=(4000.0, 40.0, 4000.0), **props)


def standing(world, x=0.0, z=0.0, angle=math.pi / 2.0, top=FLOOR_TOP):
    """A player resting on *world*, facing +x (angle pi/2) by default."""
    p = Player(x, z, angle=angle)
    p.pos.y = top + p._half.y + 0.5
    frames(p, world, 10)
    assert p.on_ground, "fixture is wrong: the player never landed"
    return p


def frames(player, world, n, move=(0.0, 0.0), jump=False, crouch=False,
           sprint=False, terrain=None, movers=None):
    move_dir = glm.vec3(move[0], 0.0, move[1])
    for _ in range(n):
        player.update(DT, move_dir, jump, crouch, world, movers=movers,
                      terrain=terrain, sprint=sprint)
    return player


def hspeed(player):
    return math.hypot(float(player.velocity.x), float(player.velocity.z))


def feet(player):
    return float(player.pos.y - player._half.y)


# ---------------------------------------------------------------------------
# Speeds, acceleration and friction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("move, sprint, expected", [
    ((0.0, 1.0), False, CL_FORWARDSPEED),                 # walk: 200
    ((0.0, 1.0), True, PM_MAXSPEED),                      # run: 400 clamped to 300
    ((1.0, 1.0), False, math.hypot(200.0, 200.0)),        # walking diagonally: 283
    ((1.0, 1.0), True, PM_MAXSPEED),                      # running diagonally: 300
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
    assert hspeed(p) == pytest.approx(PM_ACCELERATE * DT * CL_FORWARDSPEED, rel=1e-4)


def test_friction_removes_speed_times_six_per_second():
    world = [floor()]
    p = standing(world)
    frames(p, world, 120, move=(0.0, 1.0), sprint=True)
    before = hspeed(p)
    frames(p, world, 1)
    assert hspeed(p) == pytest.approx(before - before * PM_FRICTION * DT, rel=1e-3)


def test_below_stopspeed_friction_works_as_if_at_stopspeed():
    world = [floor()]
    p = standing(world)
    p.velocity.z = 50.0
    frames(p, world, 1)
    assert hspeed(p) == pytest.approx(50.0 - PM_STOPSPEED * PM_FRICTION * DT, rel=1e-3)


def test_air_control_is_one_tenth_of_ground_control():
    world = [floor()]
    p = standing(world)
    frames(p, world, 1, jump=True)
    assert not p.on_ground
    before = float(p.velocity.x)
    frames(p, world, 1, move=(0.0, 1.0))
    # pm_airaccelerate 0 -> PM_Accelerate(wish, wishspeed, 1)
    assert float(p.velocity.x) - before == pytest.approx(1.0 * DT * CL_FORWARDSPEED, rel=1e-3)


# ---------------------------------------------------------------------------
# Jumping and gravity
# ---------------------------------------------------------------------------

def test_a_jump_leaves_at_270_and_peaks_about_46_units_up():
    world = [floor()]
    p = standing(world)
    start = feet(p)
    frames(p, world, 1, jump=True)
    assert float(p.velocity.y) == pytest.approx(PM_JUMP_SPEED - SV_GRAVITY * DT, abs=1e-3)
    peak = start
    for _ in range(60):
        frames(p, world, 1, jump=True)
        peak = max(peak, feet(p))
    ideal = PM_JUMP_SPEED ** 2 / (2.0 * SV_GRAVITY)       # 45.6
    assert peak - start == pytest.approx(ideal, abs=3.0)


def test_falling_gains_800_units_per_second_every_second():
    p = Player(0.0, 0.0)
    p.pos.y = 5000.0
    frames(p, [], 60)
    assert float(p.velocity.y) == pytest.approx(-SV_GRAVITY, rel=1e-3)


def test_a_hard_landing_locks_out_jumping_briefly():
    world = [floor()]
    p = Player(0.0, 0.0, angle=math.pi / 2.0)
    # Within the ground probe and falling faster than 200: a landing.
    p.pos.y = FLOOR_TOP + p._half.y + 0.1
    p.velocity.y = -300.0
    frames(p, world, 1, jump=True)
    assert p.landing and p.on_ground
    assert float(p.velocity.y) <= 0.0, "jumped inside the landing lockout"

    frames(p, world, 1)                                   # release jump
    frames(p, world, int(PM_LAND_TIME / DT) + 1)          # lockout runs out
    frames(p, world, 1, jump=True)
    assert float(p.velocity.y) > 0.0


def test_jump_pressed_in_the_air_fires_on_landing():
    """Only a jump that happened sets the held latch, as in Quake 2."""
    world = [floor()]
    p = Player(0.0, 0.0)
    p.pos.y = FLOOR_TOP + p._half.y + 40.0
    frames(p, world, 1)
    for _ in range(60):
        frames(p, world, 1, jump=True)
        if float(p.velocity.y) > 100.0:
            break
    assert float(p.velocity.y) > 100.0


# ---------------------------------------------------------------------------
# Steps, slides and slopes
# ---------------------------------------------------------------------------

def _walk_at_step(height, jump=False, n=120):
    step = box_brush(name="step", pos=(1100.0, FLOOR_TOP + height * 0.5, 0.0),
                     size=(2000.0, height, 400.0))
    world = [floor(), step]
    p = standing(world)
    frames(p, world, n, move=(0.0, 1.0))
    if jump:        # from against the face, jump while still pushing forward
        frames(p, world, n, move=(0.0, 1.0), jump=True)
    return p, FLOOR_TOP + height


def test_a_step_up_to_18_units_is_walked_up():
    p, top = _walk_at_step(PM_STEPSIZE)
    assert feet(p) == pytest.approx(top, abs=0.1)


def test_a_step_over_18_units_stops_a_walk():
    p, top = _walk_at_step(PM_STEPSIZE + 2.0)
    assert feet(p) < top
    assert float(p.pos.x) < 100.0 - 25.0 + 0.1


def test_a_jump_clears_a_ledge_a_step_above_its_peak():
    """Stepping works in the air too, which is how a 46-unit jump lands on 60."""
    p, top = _walk_at_step(60.0, jump=True)
    assert feet(p) == pytest.approx(top, abs=0.1)
    p, top = _walk_at_step(70.0, jump=True)
    assert feet(p) < top


def test_walking_down_stairs_drops_a_step_at_a_time_without_a_hard_landing():
    """Quake 2 walks off each 16-unit stair and falls to the next.

    The stepped move only presses back down to where it started, so going
    down is a series of short falls -- never fast enough to be a landing that
    locks out jumping, and never a stop.
    """
    stairs = [floor(top=-16.0 * 6),
              box_brush(name="stair0", pos=(-384.0, -500.0, 0.0), size=(832.0, 1000.0, 400.0))]
    for i in range(1, 6):
        stairs.append(box_brush(name=f"stair{i}", pos=(i * 32.0 + 16.0, -16.0 * i - 500.0, 0.0),
                                size=(32.0, 1000.0, 400.0)))
    p = standing(stairs, x=-150.0)
    frames(p, stairs, 30, move=(0.0, 1.0))                # up to speed first
    for _ in range(90):
        frames(p, stairs, 1, move=(0.0, 1.0))
        assert not p.landing
        assert hspeed(p) > CL_FORWARDSPEED * 0.9
    frames(p, stairs, 30)                                 # settle on the bottom
    assert p.on_ground
    assert feet(p) == pytest.approx(-96.0, abs=0.5)


def test_running_into_a_wall_at_an_angle_slides_along_it():
    wall = box_brush(name="wall", pos=(100.0, 100.0, 0.0), size=(40.0, 400.0, 4000.0))
    world = [floor(), wall]
    p = standing(world, angle=math.pi / 4.0)               # facing +x+z
    start_z = float(p.pos.z)
    frames(p, world, 60, move=(0.0, 1.0), sprint=True)
    assert float(p.pos.x) < 100.0 - 20.0 - 25.0 + 0.1      # stopped at the wall face
    assert float(p.pos.z) - start_z > 100.0               # kept moving along it
    # Overclip (1.01) leaves the velocity pointing just off the wall.
    assert -5.0 < float(p.velocity.x) <= 0.0


def _ramp(degrees):
    """A wedge rising toward +x at *degrees*, its foot at x=0."""
    rad = math.radians(degrees)
    brush = angled_brush("ramp", pos=(200.0, 100.0, 0.0), size=(400.0, 200.0, 400.0),
                         clip_normal=(-math.sin(rad), math.cos(rad), 0.0), clip_offset=0.0)
    assert bg.build_collision_mesh(brush)
    return brush


def test_a_gentle_slope_is_ground_and_can_be_walked_up():
    world = [floor(), _ramp(30.0)]
    p = standing(world, x=-60.0)
    frames(p, world, 90, move=(0.0, 1.0))
    assert p.on_ground
    assert feet(p) > 40.0


def test_a_steep_slope_is_not_ground_and_is_slid_down():
    world = [floor(), _ramp(60.0)]
    p = Player(60.0, 0.0, angle=math.pi / 2.0)
    p.pos.y = 300.0
    heights = []
    for _ in range(120):
        frames(p, world, 1)
        if p.ground_object is not None and p.ground_object.get("name") == "ramp":
            pytest.fail("a 60 degree slope counted as ground")
        heights.append(feet(p))
    assert p.on_ground and p.ground_object["name"] == "floor"
    assert float(p.pos.x) < 25.0                          # slid off the foot


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
    assert feet(p) == pytest.approx(before, abs=0.1)       # shrinks from the top
    assert hspeed(p) == pytest.approx(PM_DUCKSPEED, rel=1e-3)
    assert p.camera_height < p.stand_camera_height


def test_a_ducked_player_fits_under_a_low_ceiling_and_stays_ducked_there():
    ceiling = box_brush(name="ceiling", pos=(300.0, FLOOR_TOP + 70.0 + 100.0, 0.0),
                        size=(200.0, 200.0, 400.0))         # 70 units of headroom
    world = [floor(), ceiling]
    p = standing(world)
    frames(p, world, 240, move=(0.0, 1.0))
    assert float(p.pos.x) < 200.0 - 25.0 + 0.1, "stood up under a 70-unit gap"

    p = standing(world)
    frames(p, world, 180, move=(0.0, 1.0), crouch=True)
    assert 200.0 < float(p.pos.x) < 400.0
    frames(p, world, 2)                                    # let go of crouch
    assert p.ducked, "stood up into the ceiling"


# ---------------------------------------------------------------------------
# Water
# ---------------------------------------------------------------------------

def _pool(surface):
    return box_brush(name="pool", pos=(0.0, surface - 500.0, 0.0),
                     size=(2000.0, 1000.0, 2000.0), is_water=True)


@pytest.mark.parametrize("surface, level", [
    (-200.0, 0), (FLOOR_TOP + 10.0, 1), (FLOOR_TOP + 60.0, 2), (FLOOR_TOP + 200.0, 3),
])
def test_water_level_is_sampled_at_feet_waist_and_eyes(surface, level):
    world = [floor(), _pool(surface)]
    p = Player(0.0, 0.0)
    p.pos.y = FLOOR_TOP + p._half.y + 0.5
    frames(p, world, 1)
    assert p.waterlevel == level


def test_swimming_is_half_speed_and_idle_swimmers_sink():
    world = [_pool(1000.0)]
    p = Player(0.0, 0.0, angle=math.pi / 2.0)
    p.pos.y = 500.0
    frames(p, world, 240, move=(0.0, 1.0), sprint=True)
    assert hspeed(p) < PM_MAXSPEED * 0.5 + 1.0
    assert hspeed(p) > PM_MAXSPEED * 0.5 * 0.8
    frames(p, world, 240)
    assert float(p.velocity.y) < 0.0


def test_held_jump_under_water_swims_up():
    world = [_pool(1000.0)]
    p = Player(0.0, 0.0)
    p.pos.y = 500.0
    frames(p, world, 30, jump=True)
    assert p.waterlevel == 3
    assert float(p.velocity.y) > 0.0


# ---------------------------------------------------------------------------
# Movers, terrain and gravity elsewhere
# ---------------------------------------------------------------------------

def test_a_mover_that_moves_into_the_player_does_not_trap_it():
    world = [floor()]
    p = standing(world)
    door = box_brush(name="door", pos=(float(p.pos.x) + 30.0, 60.0, 0.0),
                     size=(40.0, 120.0, 400.0), is_door=True)
    frames(p, world, 1, movers=[door])
    tracer = BoxTracer(world + [door])
    assert not tracer.position_is_solid(p.pos, p._half)
    frames(p, world, 30, move=(0.0, -1.0), movers=[door])  # and it can still move
    assert hspeed(p) > 0.0 or float(p.pos.x) < -10.0


class _Slope:
    """A terrain stand-in: y = x * tan(10 degrees)."""

    k = math.tan(math.radians(10.0))

    def is_solid(self):
        return True

    def get_height_at_safe(self, x, z):
        return x * self.k


def test_terrain_is_walked_up_and_down_on_the_ground():
    terrain = _Slope()
    p = Player(0.0, 0.0, angle=math.pi / 2.0)
    p.pos.y = p._half.y + 1.0
    frames(p, [], 30, terrain=terrain)
    assert p.on_ground and p.ground_object is terrain
    for _ in range(60):
        frames(p, [], 1, move=(0.0, 1.0), terrain=terrain)
    assert feet(p) == pytest.approx(float(p.pos.x) * _Slope.k, abs=1.0)
    # Downhill, as on stairs: short drops that never become a landing.
    p.angle = -math.pi / 2.0
    top_x = float(p.pos.x)
    for _ in range(60):
        frames(p, [], 1, move=(0.0, 1.0), terrain=terrain)
        assert not p.landing
        assert -0.5 < feet(p) - float(p.pos.x) * _Slope.k < 2.0
    assert top_x - float(p.pos.x) > 150.0


def test_one_gravity_for_the_player_monsters_and_props():
    from engine.monster_constants import MONSTER_GRAVITY
    from engine.physics import PhysicsWorld
    from engine import prop_runtime
    drop = next(getattr(cls, "DROP_GRAVITY") for cls in vars(prop_runtime).values()
                if isinstance(cls, type) and hasattr(cls, "DROP_GRAVITY"))
    assert MONSTER_GRAVITY == -SV_GRAVITY
    assert float(PhysicsWorld.GRAVITY) == -SV_GRAVITY
    assert drop == SV_GRAVITY


# ---------------------------------------------------------------------------
# The trace
# ---------------------------------------------------------------------------

def test_a_trace_stops_short_of_a_box_and_reports_its_face():
    wall = box_brush(name="wall", pos=(100.0, 0.0, 0.0), size=(20.0, 200.0, 200.0))
    tr = BoxTracer([wall]).trace((0.0, 0.0, 0.0), (200.0, 0.0, 0.0), (10.0, 10.0, 10.0))
    assert tr.ent is wall
    assert tr.normal == (-1.0, 0.0, 0.0)
    assert 90.0 - 0.1 < tr.endpos[0] + 10.0 < 90.0          # just short of x=90
    assert not tr.startsolid


def test_a_trace_out_of_a_solid_is_allowed_one_within_it_is_not():
    block = box_brush(name="block", pos=(0.0, 0.0, 0.0), size=(100.0, 100.0, 100.0))
    tracer = BoxTracer([block])
    out = tracer.trace((0.0, 0.0, 0.0), (200.0, 0.0, 0.0), (5.0, 5.0, 5.0))
    assert out.startsolid and not out.allsolid and out.fraction == 1.0
    stuck = tracer.trace((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (5.0, 5.0, 5.0))
    assert stuck.allsolid and stuck.fraction == 0.0


def test_a_trace_does_not_catch_on_the_corner_of_an_angled_brush():
    """Axial bevels: passing beside a wedge's sloped edge is not a hit."""
    ramp = _ramp(45.0)
    tracer = BoxTracer([ramp])
    # Above the wedge's top-right corner region but clear of the slope.
    tr = tracer.trace((-60.0, 150.0, 0.0), (-60.0, 150.0, 300.0), (25.0, 50.0, 25.0))
    assert tr.fraction == 1.0
