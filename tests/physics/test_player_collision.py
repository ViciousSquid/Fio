"""What the player does and does not collide with.

``engine.player`` had no tests of its own, which is why the collision passes
were free to disagree with each other about what counts as solid: the vertical
resolve skipped water, hidden and trigger brushes, while the horizontal sweep
(``_has_headroom``) skipped nothing and relied entirely on ``SpatialGrid``
having filtered them out first.  Anything reaching the player through the
no-grid path — the fallback in ``Player.update``, and every mover and door,
which are appended *after* the grid query — was therefore classified
differently depending on which way the player happened to be moving.

The contract these tests pin is a single one: ``_blocks_player`` decides, once
per frame, and every pass walks the list it is handed.
"""

import math

import pytest

pytest.importorskip("glm", reason="the player is built on PyGLM vectors")

from engine.player import Player, _blocks_player      # noqa: E402
from engine.physics import SpatialGrid                # noqa: E402
from tests.helpers.worlds import box_brush            # noqa: E402


FLOOR = dict(pos=(0.0, -100.0, 0.0), size=(2000.0, 40.0, 2000.0))


def floor_brush(**props):
    return box_brush(name="floor", **FLOOR, **props)


def player_at(x=0.0, y=0.0, z=0.0):
    p = Player(x, z, angle=0.0)
    p.pos.y = y
    return p


def run(player, brushes, steps=1, move=(0.0, 0.0), dt=1.0 / 60.0,
        jump=False, grid=False):
    """Drive real ``Player.update`` frames, with or without a spatial grid."""
    import glm
    spatial = None
    if grid:
        spatial = SpatialGrid()
        spatial.populate(brushes)
    move_dir = glm.vec3(move[0], 0.0, move[1])
    for _ in range(steps):
        player.update(dt, move_dir, jump, False, brushes,
                      spatial_grid=spatial, sprint=sprint)
    return player


# ---------------------------------------------------------------------------
# The predicate itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("props, blocks", [
    ({}, True),
    ({"hidden": True}, False),
    ({"disabled": True}, False),
    ({"is_fog": True}, False),
    ({"is_water": True}, False),
    ({"shader": "Water"}, False),
    ({"is_trigger": True}, False),
    ({"is_trigger": True, "is_mover": True}, True),
    ({"is_trigger": True, "is_door": True}, True),
    ({"_physics_body": True}, False),
])
def test_what_counts_as_solid_for_the_player(props, blocks):
    assert _blocks_player(box_brush(name="b", **props)) is blocks


def test_a_water_textured_brush_is_not_solid():
    """Water is classified by texture as well as by flag — see is_water_brush."""
    brush = box_brush(name="pool")
    brush["textures"] = dict(brush["textures"], top="assets/textures/Water_Blue.png")
    assert _blocks_player(brush) is False


# ---------------------------------------------------------------------------
# Horizontal movement
# ---------------------------------------------------------------------------

def _walked_into(wall_props, grid):
    """How far east the player gets with a wall 100 units east of the start."""
    wall = box_brush(name="wall", pos=(100.0, 0.0, 0.0), size=(40.0, 200.0, 400.0),
                     **wall_props)
    world = [floor_brush(), wall]
    player = player_at(0.0, 0.0, 0.0)
    run(player, world, steps=30, grid=grid)            # settle onto the floor
    assert player.on_ground, "fixture is wrong: the player never landed"
    run(player, world, steps=30, move=(1.0, 0.0), grid=grid)
    return float(player.pos.x)


@pytest.mark.parametrize("grid", [False, True], ids=["no-grid", "grid"])
def test_a_solid_wall_stops_horizontal_movement(grid):
    assert _walked_into({}, grid) < 70.0


@pytest.mark.parametrize("grid", [False, True], ids=["no-grid", "grid"])
@pytest.mark.parametrize("props", [
    {"is_water": True},
    {"hidden": True},
    {"disabled": True},
    {"is_trigger": True},
], ids=["water", "hidden", "disabled", "trigger"])
def test_a_non_solid_brush_never_stops_horizontal_movement(props, grid):
    """The regression this file exists for.

    Without a grid these all used to be solid walls, because the horizontal
    sweep did no filtering of its own — so a map played through the fallback
    path (or any mover, which is appended after the grid query) could not be
    walked into a pool at all.
    """
    assert _walked_into(props, grid) > 80.0


def test_a_hidden_mover_blocks_neither_axis():
    """Movers bypass the grid entirely, so the predicate is all they get."""
    mover = box_brush(name="lift", pos=(100.0, 0.0, 0.0), size=(40.0, 200.0, 400.0),
                      is_mover=True, hidden=True)
    player = player_at(0.0, 0.0, 0.0)
    player.physics_enabled = False
    import glm
    for _ in range(30):
        player.update(1 / 60.0, glm.vec3(1.0, 0.0, 0.0), False, False, [],
                      movers=[mover], spatial_grid=SpatialGrid())
    assert float(player.pos.x) > 90.0


# ---------------------------------------------------------------------------
# Vertical movement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("grid", [False, True], ids=["no-grid", "grid"])
def test_the_player_lands_on_a_floor(grid):
    floor = floor_brush()
    player = player_at(0.0, 200.0, 0.0)
    run(player, [floor], steps=180, grid=grid)
    assert player.on_ground
    assert player.ground_object is floor
    # Feet rest on the floor's top face, not inside it.
    top = floor["pos"][1] + floor["size"][1] * 0.5
    assert abs((player.pos.y - player._half.y) - top) < 1.0


@pytest.mark.parametrize("props", [
    {"is_water": True},
    {"hidden": True},
    {"is_trigger": True},
], ids=["water", "hidden", "trigger"])
def test_the_player_falls_through_a_non_solid_floor(props):
    player = player_at(0.0, 200.0, 0.0)
    run(player, [floor_brush(**props)], steps=60)
    assert not player.on_ground
    assert player.pos.y < 100.0


def test_a_step_is_climbed_and_a_wall_is_not():
    floor = floor_brush()
    top = floor["pos"][1] + floor["size"][1] * 0.5

    def walk_into(height):
        obstacle = box_brush(name="step",
                             pos=(120.0, top + height * 0.5 - 20.0, 0.0),
                             size=(60.0, height, 400.0))
        player = player_at(0.0, 0.0, 0.0)
        run(player, [floor, obstacle], steps=120, move=(1.0, 0.0))
        return player

    stepped = walk_into(24.0)            # inside step_height (18) of the floor
    assert stepped.pos.x > 100.0, "a low lip should be climbed, not walked into"

    blocked = walk_into(200.0)
    assert blocked.pos.x < 100.0, "a tall wall is a wall"


# ---------------------------------------------------------------------------
# Waterjump
# ---------------------------------------------------------------------------

def _waterjump_rise(ledge_props=None):
    """Upward velocity after one jump frame pushing at the pool rim.

    Swimming with jump held is itself an upward move, so this is only readable
    against the same scene with no ledge: the waterjump is the difference.
    """
    import glm
    pool = box_brush(name="pool", pos=(0.0, 0.0, 0.0), size=(400.0, 200.0, 400.0),
                     is_water=True)
    brushes = [pool]
    if ledge_props is not None:
        # Top at y=+40: above the feet, inside WATERJUMP_MAX_CLIMB of them, and
        # below the waterline plus WATERJUMP_EDGE_ABOVE_SURFACE.
        brushes.append(box_brush(name="ledge", pos=(260.0, -90.0, 0.0),
                                 size=(120.0, 260.0, 400.0), **ledge_props))
    player = player_at(150.0, 0.0, 0.0)
    player.update(1 / 60.0, glm.vec3(0.0, 0.0, 0.0), False, False, brushes)
    assert player.in_water, "fixture is wrong: the player must start submerged"
    player.update(1 / 60.0, glm.vec3(1.0, 0.0, 0.0), True, False, brushes)
    return float(player.velocity.y)


def test_a_waterjump_launches_the_player_at_a_reachable_ledge():
    """Pushing at a pool rim while in water vaults out of the pool."""
    assert _waterjump_rise({}) > _waterjump_rise(None) + 100.0


@pytest.mark.parametrize("props", [
    {"hidden": True},
    {"is_trigger": True},
    {"is_water": True},
], ids=["hidden", "trigger", "water"])
def test_a_waterjump_ignores_a_ledge_that_is_not_solid(props):
    assert _waterjump_rise(props) == _waterjump_rise(None)


# ---------------------------------------------------------------------------
# Quake-style movement feel
# ---------------------------------------------------------------------------

def _movement_fixture():
    floor = floor_brush()
    player = player_at(0.0, 0.0, 0.0)
    run(player, [floor], steps=60)
    assert player.on_ground
    return player, [floor]


def test_held_sprint_raises_ground_speed_and_releases_cleanly():
    player, world = _movement_fixture()

    run(player, world, steps=60, move=(0.0, 1.0))
    normal_speed = math.hypot(float(player.velocity.x), float(player.velocity.z))
    assert normal_speed > player.speed * 0.9

    run(player, world, steps=60, move=(0.0, 1.0), sprint=True)
    sprint_speed = math.hypot(float(player.velocity.x), float(player.velocity.z))
    assert sprint_speed > normal_speed * 1.4
    assert sprint_speed <= player.speed * 1.5 + 1.0

    run(player, world, steps=60, move=(0.0, 1.0), sprint=False)
    released_speed = math.hypot(float(player.velocity.x), float(player.velocity.z))
    assert released_speed < sprint_speed
    assert released_speed > player.speed * 0.9


def test_ground_movement_accelerates_instead_of_snapping_to_speed():
    player, world = _movement_fixture()

    run(player, world, steps=1, move=(0.0, 1.0))
    first_speed = math.hypot(float(player.velocity.x), float(player.velocity.z))

    run(player, world, steps=1, move=(0.0, 1.0))
    second_speed = math.hypot(float(player.velocity.x), float(player.velocity.z))

    assert 0.0 < first_speed < second_speed < player.speed


def test_ground_friction_brings_the_player_to_rest():
    player, world = _movement_fixture()

    run(player, world, steps=60, move=(0.0, 1.0))
    moving_speed = math.hypot(float(player.velocity.x), float(player.velocity.z))
    assert moving_speed > player.speed * 0.9

    run(player, world, steps=30)
    stopped_speed = math.hypot(float(player.velocity.x), float(player.velocity.z))
    assert stopped_speed < 1.0


def test_air_movement_preserves_forward_momentum_while_strafing():
    player, world = _movement_fixture()

    run(player, world, steps=60, move=(0.0, 1.0))
    forward_speed = math.hypot(float(player.velocity.x), float(player.velocity.z))
    assert forward_speed > player.speed * 0.9

    run(player, world, steps=1, move=(0.0, 1.0), jump=True)
    assert not player.on_ground

    # The old controller overwrote horizontal velocity with the new wish
    # direction. Quake-style air acceleration instead keeps existing momentum
    # and adds a lateral component.
    run(player, world, steps=6, move=(1.0, 0.0))
    assert player.velocity.x > 0.0
    assert player.velocity.z > forward_speed * 0.8


def test_holding_jump_does_not_auto_bunny_hop_on_landing():
    player, world = _movement_fixture()
    import glm

    dt = 1.0 / 60.0
    player.update(dt, glm.vec3(0.0, 0.0, 0.0), True, False, world)
    assert not player.on_ground

    for _ in range(120):
        player.update(dt, glm.vec3(0.0, 0.0, 0.0), True, False, world)

    assert player.on_ground
    assert abs(float(player.velocity.y)) < 0.001

    # Still holding jump must leave the player grounded.
    player.update(dt, glm.vec3(0.0, 0.0, 0.0), True, False, world)
    assert player.on_ground
    assert abs(float(player.velocity.y)) < 0.001

    # Release then press: a new jump is allowed.
    player.update(dt, glm.vec3(0.0, 0.0, 0.0), False, False, world)
    player.update(dt, glm.vec3(0.0, 0.0, 0.0), True, False, world)
    assert not player.on_ground


# ---------------------------------------------------------------------------
# Work per frame
# ---------------------------------------------------------------------------

def test_solidity_is_decided_once_per_frame_not_once_per_pass(monkeypatch):
    """Counts calls rather than time: the point is that the passes stopped
    re-deriving what the frame already knows."""
    import engine.player as player_mod

    calls = []
    real = player_mod._blocks_player
    monkeypatch.setattr(player_mod, "_blocks_player",
                        lambda b: (calls.append(b.get("name")), real(b))[1])

    floor = floor_brush()
    wall = box_brush(name="wall", pos=(100.0, 0.0, 0.0), size=(40.0, 200.0, 400.0))
    player = player_at(0.0, 0.0, 0.0)
    run(player, [floor, wall], steps=1, move=(1.0, 0.0))

    assert sorted(calls) == ["floor", "wall"]
