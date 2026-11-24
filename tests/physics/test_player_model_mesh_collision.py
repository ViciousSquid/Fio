"""Player contact with Solid Prop model collision meshes.

Model Props are arbitrary triangle surfaces, not convex solids: the player must
land on deck/tread triangles without filling the gaps between them.
"""
import math

import glm
import pytest

from editor.editor_state import EditorState
from engine.logic_thread import LogicThread
from engine.player import Player, _gather
from engine.prop_entity import Prop
from engine.spatial_grid import SpatialGrid
from engine.threaded_game_state import ThreadedGameState

pytestmark = pytest.mark.qt


def _quad(a, b, c, d, normal):
    return [((a, b, c), normal), ((a, c, d), normal)]


def _platform(y=0.0, x0=-100.0, x1=100.0):
    a, b = (x0, y, -100.0), (x0, y, 100.0)
    c, d = (x1, y, 100.0), (x1, y, -100.0)
    # Winding gives the top face an upward normal.
    return _quad(a, b, c, d, (0.0, 1.0, 0.0))


def _staircase():
    """Three 12-unit risers with real vertical faces and horizontal treads."""
    triangles = _platform(y=0.0, x0=-200.0, x1=0.0)
    tread_depth = 60.0
    tread_rise = 12.0
    for index in range(3):
        x = index * tread_depth
        lower = index * tread_rise
        upper = lower + tread_rise
        triangles += _quad(
            (x, lower, -100.0), (x, upper, -100.0),
            (x, upper, 100.0), (x, lower, 100.0),
            (1.0, 0.0, 0.0),
        )
        triangles += _platform(
            y=upper, x0=x, x1=x + tread_depth
        )
    triangles += _platform(y=36.0, x0=180.0, x1=400.0)
    return triangles


def _model_collision_brushes(triangles, *, solid=True):
    """Build the real LogicThread collision proxy with CPU model data stubbed."""
    prop = Prop(pos=[0.0, 0.0, 0.0], properties={
        'render_mode': 'model',
        'model_path': 'assets/models/test_catwalk.obj',
        'scale': [1.0, 1.0, 1.0],
        'rotation': [0.0, 0.0, 0.0],
        'collision_shape': 'mesh',
        'collision_size': [0.0, 0.0, 0.0],
        'physics_enabled': False,
        'no_collision': not solid,
    })
    state = EditorState()
    state.brushes = []
    state.things = [prop]
    thread = LogicThread(ThreadedGameState(), state)
    thread.collision_runtime.compute_model_collision_mesh = (
        lambda *_args: triangles
    )
    return thread.collision_runtime.build_model_collision_brushes()


def _player_at(x=0.0, y=40.0, z=0.0):
    player = Player(x, z)
    player.pos = glm.vec3(x, y, z)
    player.velocity = glm.vec3(0.0, -10.0, 0.0)
    player.on_ground = False
    player.ground_object = None
    return player


def _resolve_model_mesh(player, brush):
    aabb, meshes, shapes = _gather([brush], [])
    assert not aabb
    assert meshes == [brush]
    assert len(shapes) == 1 and not shapes[0][0] and shapes[0][1]
    player._resolve_mesh_capsule(meshes, shapes)


def test_solid_model_prop_lands_on_its_actual_mesh_surface():
    brushes = _model_collision_brushes(_platform(), solid=True)
    assert len(brushes) == 1
    brush = brushes[0]
    assert brush['_model_collision'] is True
    assert brush['_physics_body'] is False

    player = _player_at(y=40.0)
    _resolve_model_mesh(player, brush)

    # The player's feet (centre minus half-height) rest on y=0.
    assert player.pos.y == pytest.approx(50.0, abs=1e-3)
    assert player.on_ground
    assert player.ground_object is brush
    assert player.velocity.y == 0.0


def test_non_solid_model_prop_is_not_added_to_player_collision():
    assert _model_collision_brushes(_platform(), solid=False) == []


def test_model_mesh_keeps_real_openings_in_grating():
    # Two deck strips with a 80-unit opening between them. The broad mesh
    # bounds cover the player, but no actual triangle is within capsule reach.
    triangles = (
        _platform(x0=-100.0, x1=-40.0)
        + _platform(x0=40.0, x1=100.0)
    )
    brush = _model_collision_brushes(triangles, solid=True)[0]
    player = _player_at(x=0.0, y=40.0)

    _resolve_model_mesh(player, brush)

    assert player.pos.y == pytest.approx(40.0)
    assert not player.on_ground


def test_model_mesh_stops_the_player_at_a_vertical_surface():
    a = (0.0, -100.0, -100.0)
    b = (0.0, 200.0, -100.0)
    c = (0.0, 200.0, 100.0)
    d = (0.0, -100.0, 100.0)
    wall = _quad(a, b, c, d, (1.0, 0.0, 0.0))
    brush = _model_collision_brushes(wall, solid=True)[0]
    player = _player_at(x=10.0, y=50.0)
    player.velocity = glm.vec3(-20.0, 0.0, 0.0)

    _resolve_model_mesh(player, brush)

    assert player.pos.x == pytest.approx(25.0, abs=1e-3)
    assert player.velocity.x == 0.0


def test_a_reversed_deck_face_still_supports_the_player():
    # Same deck with reversed winding: the visible upper surface must support
    # the player regardless of which way its OBJ triangles are authored.
    triangles = [
        (tuple(reversed(face)), (0.0, -1.0, 0.0))
        for face, _normal in _platform()
    ]
    brush = _model_collision_brushes(triangles, solid=True)[0]
    player = _player_at(y=40.0)

    _resolve_model_mesh(player, brush)

    assert player.pos.y == pytest.approx(50.0, abs=1e-3)
    assert player.on_ground

def test_degenerate_triangle_is_treated_as_a_segment_without_dividing_by_zero():
    line = [(((0.0, 0.0, -100.0),
              (0.0, 0.0, -100.0),
              (0.0, 0.0, 100.0)), (0.0, 1.0, 0.0))]
    brush = _model_collision_brushes(line, solid=True)[0]
    player = _player_at(x=10.0, y=0.0)

    _resolve_model_mesh(player, brush)

    assert player.pos.x == pytest.approx(25.0, abs=1e-3)


def test_deck_edge_contact_pushes_away_from_the_edge():
    brush = _model_collision_brushes(
        _platform(x0=-100.0, x1=0.0), solid=True
    )[0]
    player = _player_at(x=24.0, y=25.0)

    _resolve_model_mesh(player, brush)

    assert player.pos.x > 24.0
    assert not player.on_ground


def test_coverage_retry_restores_the_complete_player_entry_state():
    brush = _model_collision_brushes(_platform(), solid=True)[0]
    player = _player_at(y=40.0)
    player.velocity = glm.vec3(3.0, -10.0, 4.0)
    player.on_ground = False
    player.ground_object = None

    class LimitedCover:
        full = ([], [], [])

        @staticmethod
        def holds(x, y, z):
            return y < 45.0

    _aabb, meshes, shapes = _gather([brush], [])
    retried = player._resolve_mesh_capsule(meshes, shapes, LimitedCover())

    assert retried is True
    assert tuple(player.pos) == pytest.approx((0.0, 40.0, 0.0))
    assert tuple(player.velocity) == pytest.approx((3.0, -10.0, 4.0))
    assert player.on_ground is False
    assert player.ground_object is None


def test_model_correction_is_rechecked_against_convex_floor():
    player = _player_at(y=40.0)
    floor = {'pos': [0.0, -50.0, 0.0], 'size': [2000.0, 100.0, 2000.0]}
    floor_planes = [
        (1.0, 0.0, 0.0, 1000.0), (-1.0, 0.0, 0.0, 1000.0),
        (0.0, 1.0, 0.0, 0.0), (0.0, -1.0, 0.0, 100.0),
        (0.0, 0.0, 1.0, 1000.0), (0.0, 0.0, -1.0, 1000.0),
    ]
    floor_shape = (
        floor_planes, [],
        ([-1000.0, -100.0, -1000.0], [1000.0, 0.0, 1000.0]),
        None,
    )
    normal = (0.8, -0.6, 0.0)
    v0 = (0.0, 54.1666666667, -1000.0)
    v1 = (-300.0, -345.8333333333, 0.0)
    v2 = (0.0, 54.1666666667, 1000.0)
    triangles = [((v0, v1, v2), normal)]
    model_bounds = (
        [min(v[i] for v in (v0, v1, v2)) for i in range(3)],
        [max(v[i] for v in (v0, v1, v2)) for i in range(3)],
    )
    model = {'_model_collision': True, 'pos': [0.0, 0.0, 0.0],
             'size': [1.0, 1.0, 1.0]}
    model_shape = ([], triangles, model_bounds, None)

    player._resolve_mesh_capsule(
        [floor, model], [floor_shape, model_shape]
    )

    assert player.pos.y >= 50.0 - 1e-3
    assert player.on_ground
    assert player.ground_object is floor


@pytest.mark.parametrize(
    ("start_x", "direction"),
    ((25.0, -1.0), (-25.0, 1.0)),
)
def test_player_update_sweeps_against_a_model_wall_from_either_side(start_x, direction):
    a = (0.0, -100.0, -100.0)
    b = (0.0, 200.0, -100.0)
    c = (0.0, 200.0, 100.0)
    d = (0.0, -100.0, 100.0)
    wall = _model_collision_brushes(
        _quad(a, b, c, d, (1.0, 0.0, 0.0)), solid=True
    )[0]
    player = _player_at(x=start_x, y=50.0)
    player.velocity = glm.vec3(direction * 300.0, 0.0, 0.0)

    player.update(
        0.1, glm.vec3(direction, 0.0, 0.0),
        False, False, [wall], gravity=0.0,
    )

    assert player.pos.x == pytest.approx(start_x, abs=1e-2)
    assert player.velocity.x == pytest.approx(0.0, abs=1e-3)




@pytest.mark.parametrize("use_grid", [False, True], ids=["no-grid", "grid"])
def test_player_can_climb_individual_stair_treads_in_a_model_mesh(use_grid):
    brush = _model_collision_brushes(_staircase(), solid=True)[0]
    player = _player_at(x=-80.0, y=50.0)
    player.angle = 0.0  # Positive strafe must move toward the staircase (+X).
    player.velocity = glm.vec3(0.0, 0.0, 0.0)
    player.on_ground = True
    player.ground_object = brush

    spatial = SpatialGrid() if use_grid else None
    if spatial is not None:
        spatial.populate([brush])

    move = glm.vec3(1.0, 0.0, 0.0)
    saw_raised_tread = False
    for _ in range(240):
        player.update(
            1.0 / 60.0, move, False, False, [brush],
            spatial_grid=spatial, gravity=-800.0,
        )
        if player.pos.y > 50.5:
            saw_raised_tread = True
            assert player.on_ground, "a mesh tread must support the player immediately"
            assert player.ground_object is brush
        if player.pos.x >= 220.0:
            break

    assert saw_raised_tread, "the fixture must exercise mesh step-up"
    assert player.pos.x >= 200.0, "the player should climb all three mesh treads"
    assert player.pos.y == pytest.approx(86.0, abs=1.0)
    assert player.on_ground
    assert player.ground_object is brush


@pytest.mark.parametrize("use_grid", [False, True], ids=["no-grid", "grid"])
def test_model_tread_footprint_support_survives_30hz_and_allows_jump(use_grid):
    brush = _model_collision_brushes(_staircase(), solid=True)[0]
    # The player's centre is 20 units before the first tread edge; the 25-unit
    # horizontal capsule radius overlaps the tread and should support the hull.
    player = _player_at(x=-20.0, y=62.0)
    player.velocity = glm.vec3(0.0, 0.0, 0.0)
    player.on_ground = True
    player.ground_object = brush

    spatial = SpatialGrid() if use_grid else None
    if spatial is not None:
        spatial.populate([brush])

    idle = glm.vec3(0.0, 0.0, 0.0)
    player.update(
        1.0 / 30.0, idle, False, False, [brush],
        spatial_grid=spatial, gravity=-800.0,
    )

    assert player.on_ground, "gravity at 30 Hz must not drop footprint tread support"
    assert player.ground_object is brush
    assert player.pos.y == pytest.approx(62.0, abs=1e-3)

    player.update(
        1.0 / 30.0, idle, True, False, [brush],
        spatial_grid=spatial, gravity=-800.0,
    )
    assert player.velocity.y > 0.0, "the supported player must be able to jump"
    assert not player.on_ground, "upward separation must detach from the tread"


def test_jump_leaving_a_model_deck_is_not_cancelled_by_the_face_sweep():
    brush = _model_collision_brushes(_platform(), solid=True)[0]
    player = _player_at(y=50.0)
    player.velocity = glm.vec3(0.0, 0.0, 0.0)
    player.on_ground = True
    player.ground_object = brush

    player.update(
        0.1, glm.vec3(0.0), True, False, [brush], gravity=0.0,
    )

    assert player.pos.y > 50.0
    assert player.velocity.y > 0.0
    assert not player.on_ground
    assert player.ground_object is None


def test_stationary_player_is_not_ejected_below_a_reversed_deck():
    triangles = [
        (tuple(reversed(face)), (0.0, -1.0, 0.0))
        for face, _normal in _platform()
    ]
    brush = _model_collision_brushes(triangles, solid=True)[0]
    player = _player_at(y=25.0)
    player.velocity = glm.vec3(0.0, 0.0, 0.0)

    _resolve_model_mesh(player, brush)

    assert player.pos.y == pytest.approx(50.0, abs=1e-3)
    assert player.on_ground
    assert player.ground_object is brush


def test_swept_edge_contact_does_not_leave_player_grounded_after_sliding_off():
    brush = _model_collision_brushes(
        _platform(x0=-100.0, x1=0.0), solid=True
    )[0]
    player = _player_at(x=20.0, y=50.0)
    player.velocity = glm.vec3(50.0, 0.0, 0.0)

    _aabb, meshes, shapes = _gather([brush], [])
    player._resolve_mesh_capsule(
        meshes, shapes, sweep_start=(-30.0, 50.0, 0.0)
    )

    assert player.pos.x == pytest.approx(20.0, abs=1e-2)
    assert not player.on_ground
    assert player.ground_object is None


def test_mesh_slide_is_rechecked_against_aabb_boxes():
    a = (0.0, -100.0, -500.0)
    b = (0.0, 200.0, -500.0)
    c = (0.0, 200.0, 500.0)
    d = (0.0, -100.0, 500.0)
    wall = _model_collision_brushes(
        _quad(a, b, c, d, (1.0, 0.0, 0.0)), solid=True
    )[0]
    # The wall slide redirects the player into this neighbouring box. The
    # original integrated endpoint is clear of it, so only a post-mesh AABB
    # pass can keep the player out.
    box = {"pos": [25.0, 115.0, 85.0], "size": [50.0, 50.0, 50.0]}
    player = _player_at(x=100.0, y=50.0, z=-100.0)
    player.velocity = glm.vec3(-1500.0, 0.0, 1500.0)

    player.update(
        0.1, glm.vec3(0.0), False, False, [wall, box], gravity=0.0,
    )

    p_min = player.pos - player._half
    p_max = player.pos + player._half
    b_min = [box["pos"][i] - box["size"][i] * 0.5 for i in range(3)]
    b_max = [box["pos"][i] + box["size"][i] * 0.5 for i in range(3)]
    assert not (
        p_max.x > b_min[0] and p_min.x < b_max[0]
        and p_max.y > b_min[1] and p_min.y < b_max[1]
        and p_max.z > b_min[2] and p_min.z < b_max[2]
    )
    # It leaves the box the short way -- its head was ten units into the
    # underside -- and stays against the wall it slid along. An X pass moved
    # it 50 units sideways, out of the box's far face.
    assert player.pos.x == pytest.approx(25.0, abs=1e-2)
    assert player.pos.z == pytest.approx(50.0, abs=1e-2)
    assert player.pos.y == pytest.approx(40.0, abs=1e-2)


@pytest.mark.parametrize("high, low", [(160.0, 40.0), (200.0, 0.0), (110.0, 90.0)])
def test_walking_under_a_sloping_mesh_does_not_throw_the_player_off_the_floor(high, low):
    """A mesh that slopes down over a box floor (the underside of a model
    staircase, a leaning panel) presses the hull a fraction of a unit into
    the floor. The box re-check ran an X pass first, which took that for an
    X overlap and moved the player out of the floor brush's nearest side:
    a thousand units in one tick, and then a fall into the void."""
    a, b = (100.0, high, -200.0), (300.0, low, -200.0)
    c, d = (300.0, low, 200.0), (100.0, high, 200.0)
    slope = math.hypot(200.0, high - low)
    normal = (-(high - low) / slope, -200.0 / slope, 0.0)
    panel = _model_collision_brushes(_quad(a, b, c, d, normal), solid=True)[0]
    floor = {"pos": [0.0, -50.0, 0.0], "size": [2000.0, 100.0, 2000.0]}   # top at y=0
    brushes = [floor, panel]
    player = Player(0.0, 0.0, angle=math.pi / 2)                          # facing +x
    player.pos = glm.vec3(0.0, 50.0, 0.0)
    forward = type("Input", (), {"x": 0, "z": 1})()

    for _ in range(360):
        before = glm.vec3(player.pos)
        player.update(1.0 / 60.0, forward, False, False, brushes)
        assert glm.distance(player.pos, before) < 20.0, (before, player.pos)

    assert player.on_ground and player.pos.y == pytest.approx(50.0, abs=0.5)
    assert 100.0 < player.pos.x < 300.0, "stopped under the slope, on the floor"
    assert player.pos.z == pytest.approx(0.0, abs=1.0)


def test_model_ceiling_correction_keeps_the_capsule_below_the_surface():
    triangles = [
        (tuple(reversed(face)), (0.0, -1.0, 0.0))
        for face, _normal in _platform(y=100.0)
    ]
    brush = _model_collision_brushes(triangles, solid=True)[0]
    player = _player_at(y=80.0)
    player.velocity = glm.vec3(0.0, 0.0, 0.0)

    _resolve_model_mesh(player, brush)

    assert player.pos.y + player.height * 0.5 <= 100.0 + 1e-3
    assert player.pos.y == pytest.approx(50.0, abs=1e-3)


def test_final_convex_penetration_check_measures_an_embedded_capsule():
    player = _player_at(x=90.0, y=50.0)
    wall = {'pos': [90.0, 50.0, 0.0], 'size': [20.0, 300.0, 200.0]}
    planes = [
        (-1.0, 0.0, 0.0, -80.0), (1.0, 0.0, 0.0, 100.0),
        (0.0, -1.0, 0.0, 100.0), (0.0, 1.0, 0.0, 200.0),
        (0.0, 0.0, -1.0, 100.0), (0.0, 0.0, 1.0, 100.0),
    ]
    shape = [wall, planes, ([80.0, -100.0, -100.0],
                            [100.0, 200.0, 100.0]), None, None]
    offsets = [(0.0, -25.0, 0.0), (0.0, 0.0, 0.0),
               (0.0, 25.0, 0.0)]

    assert player._convex_max_penetration([shape], offsets, 25.0) > 30.0

    player.pos.x = 50.0
    assert player._convex_max_penetration([shape], offsets, 25.0) == 0.0


def test_support_clearance_reaches_the_triangle_distance_test():
    brush = _model_collision_brushes(_platform(), solid=True)[0]
    player = _player_at(y=50.2)
    player.velocity = glm.vec3(0.0, 0.0, 0.0)

    _resolve_model_mesh(player, brush)

    assert player.pos.y == pytest.approx(50.2)
    assert player.on_ground
    assert player.ground_object is brush


def test_model_relaxation_does_not_push_a_player_sideways_off_aabb_floor():
    a = (300.0, -100.0, -100.0)
    b = (300.0, 200.0, -100.0)
    c = (300.0, 200.0, 100.0)
    d = (300.0, -100.0, 100.0)
    wall = _model_collision_brushes(
        _quad(a, b, c, d, (1.0, 0.0, 0.0)), solid=True
    )[0]
    floor = {'pos': [0.0, -50.0, 0.0],
             'size': [1000.0, 100.0, 1000.0]}
    player = _player_at(y=50.0)
    player.velocity = glm.vec3(0.0, 0.0, 0.0)
    player.on_ground = True
    player.ground_object = floor

    _aabb, meshes, shapes = _gather([wall], [])
    player._resolve_mesh_capsule(
        meshes, shapes, sweep_start=(0.0, 50.0, 0.0),
        aabb_brushes=[floor],
    )

    assert tuple(player.pos) == pytest.approx((0.0, 50.0, 0.0))
    assert player.on_ground
    assert player.ground_object is floor


def test_model_relaxation_clears_stale_aabb_ground_state():
    a = (300.0, -100.0, -100.0)
    b = (300.0, 200.0, -100.0)
    c = (300.0, 200.0, 100.0)
    d = (300.0, -100.0, 100.0)
    wall = _model_collision_brushes(
        _quad(a, b, c, d, (1.0, 0.0, 0.0)), solid=True
    )[0]
    floor = {'pos': [0.0, -50.0, 0.0],
             'size': [1000.0, 100.0, 1000.0]}
    player = _player_at(y=200.0)
    player.velocity = glm.vec3(0.0, 0.0, 0.0)
    player.on_ground = True
    player.ground_object = floor

    _aabb, meshes, shapes = _gather([wall], [])
    player._resolve_mesh_capsule(
        meshes, shapes, sweep_start=(0.0, 200.0, 0.0),
        aabb_brushes=[floor],
    )

    assert not player.on_ground
    assert player.ground_object is None


def test_spatial_grid_includes_static_meshes_reached_by_high_player_velocity():
    wall_x = 100.0
    a = (wall_x, -100.0, -100.0)
    b = (wall_x, 200.0, -100.0)
    c = (wall_x, 200.0, 100.0)
    d = (wall_x, -100.0, 100.0)
    wall = _model_collision_brushes(
        _quad(a, b, c, d, (1.0, 0.0, 0.0)), solid=True
    )[0]
    bounds = wall['_mesh_bounds']

    class Grid:
        water_brushes = []

        def __init__(self):
            self.queried = False

        def get_potential_colliders(self, player_min, player_max):
            self.queried = True
            overlaps = not (
                player_max.x < bounds[0][0] or player_min.x > bounds[1][0]
                or player_max.y < bounds[0][1] or player_min.y > bounds[1][1]
                or player_max.z < bounds[0][2] or player_min.z > bounds[1][2]
            )
            return [wall] if overlaps else []

    grid = Grid()
    player = _player_at(x=0.0, y=50.0)
    player.velocity = glm.vec3(1500.0, 0.0, 0.0)

    player.update(
        0.1, glm.vec3(0.0), False, False, [wall],
        spatial_grid=grid, gravity=0.0,
    )

    assert grid.queried
    assert player.pos.x == pytest.approx(75.0, abs=1e-2)


def test_nonfinite_model_bounds_do_not_publish_a_nan_collision_proxy():
    prop = Prop(pos=[0.0, 0.0, 0.0], properties={
        'render_mode': 'model',
        'model_path': 'assets/models/test_catwalk.obj',
        'scale': [1.0, 1.0, 1.0],
        'rotation': [0.0, 0.0, 0.0],
        'collision_shape': 'mesh',
        'collision_size': [0.0, 0.0, 0.0],
        'physics_enabled': False,
        'no_collision': False,
    })
    state = EditorState()
    state.brushes = []
    state.things = [prop]
    thread = LogicThread(ThreadedGameState(), state)
    thread.collision_runtime.compute_model_collision_mesh = lambda *_args: None
    thread.collision_runtime.compute_model_bounds = lambda *_args: (
        [float('nan'), 0.0, 0.0], [float('nan'), 10.0, 10.0]
    )

    assert thread.collision_runtime.build_model_collision_brushes() == []


def test_constraint_cycle_rolls_back_instead_of_publishing_convex_penetration():
    # The model mesh is present in this resolve, but does not touch the
    # endpoint. The convex wall pushes x=90 to x=55; the adjacent AABB then
    # pushes it back to x=75, which re-enters the convex wall. The relaxation
    # must detect that cycle and reject the endpoint.
    mesh_wall = _model_collision_brushes(
        _quad((500.0, -100.0, -500.0),
              (500.0, 200.0, -500.0),
              (500.0, 200.0, 500.0),
              (500.0, -100.0, 500.0),
              (1.0, 0.0, 0.0)),
        solid=True
    )[0]
    aabb = {'pos': [25.0, 50.0, 0.0], 'size': [50.0, 100.0, 200.0]}
    convex = {'pos': [90.0, 50.0, 0.0], 'size': [20.0, 300.0, 200.0]}
    planes = [
        (-1.0, 0.0, 0.0, -80.0), (1.0, 0.0, 0.0, 100.0),
        (0.0, -1.0, 0.0, 100.0), (0.0, 1.0, 0.0, 200.0),
        (0.0, 0.0, -1.0, 100.0), (0.0, 0.0, 1.0, 100.0),
    ]
    convex_shape = (
        planes, [],
        ([80.0, -100.0, -100.0], [100.0, 200.0, 100.0]),
        None,
    )
    mesh_shape = ([], mesh_wall['_mesh_triangles'],
                  mesh_wall['_mesh_bounds'], None)
    player = _player_at(x=90.0, y=50.0, z=0.0)

    _aabb, _meshes, _shapes = _gather([mesh_wall], [])
    player._resolve_mesh_capsule(
        [convex, mesh_wall], [convex_shape, mesh_shape],
        sweep_start=(150.0, 50.0, 0.0),
        aabb_brushes=[aabb],
    )

    assert tuple(player.pos) == pytest.approx((150.0, 50.0, 0.0))
    p_min = player.pos - player._half
    p_max = player.pos + player._half
    assert not (
        p_max.x > 80.0 and p_min.x < 100.0
        and p_max.y > -100.0 and p_min.y < 200.0
        and p_max.z > -100.0 and p_min.z < 100.0
    )

def test_convex_ceiling_contact_is_validated_after_relaxation():
    ceiling = {'pos': [0.0, 110.0, 0.0], 'size': [2000.0, 20.0, 2000.0]}
    planes = [
        (-1.0, 0.0, 0.0, 1000.0), (1.0, 0.0, 0.0, 1000.0),
        (0.0, -1.0, 0.0, -100.0), (0.0, 1.0, 0.0, 120.0),
        (0.0, 0.0, -1.0, 1000.0), (0.0, 0.0, 1.0, 1000.0),
    ]
    shape = (planes, [],
             ([-1000.0, 100.0, -1000.0],
              [1000.0, 120.0, 1000.0]), None)
    player = _player_at(y=80.0)
    player.velocity = glm.vec3(0.0, 100.0, 0.0)

    player._resolve_mesh_capsule([ceiling], [shape])

    assert player.pos.y == pytest.approx(50.0, abs=1e-3)
    assert player.velocity.y == 0.0
    assert player._convex_max_penetration(
        [[ceiling, planes, shape[2], None, None]],
        [(0.0, -25.0, 0.0), (0.0, 0.0, 0.0), (0.0, 25.0, 0.0)],
        25.0,
    ) <= 1e-4


def test_slope_following_velocity_remains_grounded_but_jump_separates():
    normal = (-0.6, 0.8, 0.0)
    triangle = (
        (-200.0, -150.0, -200.0),
        (200.0, 150.0, -200.0),
        (0.0, 0.0, 200.0),
    )
    brush = {"_model_collision": True}
    bounds = ((-200.0, -150.0, -200.0), (200.0, 150.0, 200.0))
    model_meshes = [(brush, [(triangle, normal)], bounds)]
    player = _player_at(y=56.25)
    offsets, radius = player._capsule_offsets()

    # This velocity is tangent to the slope: it has positive world-Y motion,
    # but no component separating the capsule from the supporting surface.
    player.velocity = glm.vec3(64.0, 48.0, 0.0)
    player._apply_final_ground_state(model_meshes, [], [], offsets, radius)

    assert player.on_ground
    assert player.ground_object is brush

    # A real jump has a positive component along the slope's outward normal.
    player.velocity = glm.vec3(0.0, 100.0, 0.0)
    player._apply_final_ground_state(model_meshes, [], [], offsets, radius)

    assert not player.on_ground
    assert player.ground_object is None


def test_model_headroom_uses_triangles_not_the_enclosing_bounds():
    deck = _platform()
    railing = _quad(
        (90.0, 0.0, -100.0), (90.0, 150.0, -100.0),
        (90.0, 150.0, 100.0), (90.0, 0.0, 100.0),
        (1.0, 0.0, 0.0),
    )
    brush = _model_collision_brushes(deck + railing, solid=True)[0]
    player = _player_at()

    # The model AABB spans the railing at x=90, but the standing capsule at
    # x=0 has clear headroom above the deck.
    clear_min = glm.vec3(-25.0, 0.0, -25.0)
    clear_max = glm.vec3(25.0, 100.0, 25.0)
    assert player._has_headroom(brush, clear_min, clear_max)

    # Moving the standing capsule beside that same railing must still block.
    blocked_min = glm.vec3(55.0, 0.0, -25.0)
    blocked_max = glm.vec3(105.0, 100.0, 25.0)
    assert not player._has_headroom(brush, blocked_min, blocked_max)


@pytest.mark.parametrize("ceiling_bottom, lifted", [(111.8, False), (113.0, True)],
                         ids=["no-room", "room"])
def test_tread_lift_needs_headroom(ceiling_bottom, lifted):
    """The footprint lift onto a tread is checked against what is above it.

    At 30 Hz a player standing with its centre 20 units before the first
    tread sinks 0.89 units under gravity, and the footprint support lifts it
    back onto the tread. With a ceiling 111.8 high that lift would put the
    head (feet + 100) 0.2 units into it: the lift is refused and the tread
    gives no support. With room above, the lift stands.
    """
    from tests.helpers.worlds import box_brush
    brush = _model_collision_brushes(_staircase(), solid=True)[0]
    ceiling = box_brush("ceiling", (-20.0, ceiling_bottom + 8.0, 0.0),
                        (300.0, 16.0, 300.0))
    player = _player_at(x=-20.0, y=62.0)
    player.velocity = glm.vec3(0.0, 0.0, 0.0)
    player.on_ground = True
    player.ground_object = brush

    player.update(1.0 / 30.0, glm.vec3(0.0, 0.0, 0.0), False, False,
                  [brush, ceiling], gravity=-800.0)

    head = player.pos.y + player._half.y
    assert head <= ceiling_bottom + 1e-3, "the hull must not end inside the ceiling"
    if lifted:
        assert player.pos.y == pytest.approx(62.0, abs=1e-3)
        assert player.on_ground and player.ground_object is brush
    else:
        assert player.pos.y < 62.0 - 0.5, "the refused lift leaves the player sinking"
        assert not player.on_ground


def test_step_up_checks_the_lift_where_it_starts():
    """The step lifts the capsule where it stands, then sweeps it forward.

    A model ceiling 100.5 high ending at x=-41.1 clears the lifted hull at
    the destination (x=-20) but not at the start (x=-23.3): the top sphere
    would be lifted 2.7 units into its edge and then swept out of it, a
    crossing nothing afterwards can see. That step is refused.
    """
    ceiling = [((
        (-400.0, 100.5, -100.0), (-41.1, 100.5, -100.0), (-41.1, 100.5, 100.0)),
        (0.0, -1.0, 0.0)), ((
        (-400.0, 100.5, -100.0), (-41.1, 100.5, 100.0), (-400.0, 100.5, 100.0)),
        (0.0, -1.0, 0.0))]
    open_brush = _model_collision_brushes(_staircase(), solid=True)[0]
    low_brush = _model_collision_brushes(_staircase() + ceiling, solid=True)[0]
    start = (-23.333, 50.0 - 800.0 / 3600.0, 0.0)

    lifted = {}
    for name, brush in (("open", open_brush), ("low", low_brush)):
        player = _player_at(x=-20.0, y=start[1])
        player.velocity = glm.vec3(200.0, -800.0 / 60.0, 0.0)
        player.on_ground = True
        _aabb, meshes, shapes = _gather([brush], [])
        player._resolve_mesh_capsule(meshes, shapes, sweep_start=start)
        lifted[name] = player.pos.y

    assert lifted["open"] == pytest.approx(62.0, abs=0.05), "an open stair still steps"
    assert lifted["low"] < 55.0, "the lift would cross the ceiling's edge"


def test_the_lift_at_the_start_is_checked_from_the_start_height():
    """Where the capsule starts higher than it ends (it fell this tick), the
    lift there is checked from its own height plus the rise -- not from the
    destination's lifted height, which would test a hull lower than the one
    that crosses the start. A ceiling edge 106 high over the start clears
    the lower hull but not the real one: the step is refused (CodeRabbit,
    PR 143)."""
    ceiling = [((
        (-400.0, 106.0, -100.0), (-41.1, 106.0, -100.0), (-41.1, 106.0, 100.0)),
        (0.0, -1.0, 0.0)), ((
        (-400.0, 106.0, -100.0), (-41.1, 106.0, 100.0), (-400.0, 106.0, 100.0)),
        (0.0, -1.0, 0.0))]
    destination_y = 50.0 - 800.0 / 3600.0
    lifted = {}
    for name, start_y, extra in (("level", destination_y, []),
                                 ("fell", destination_y + 5.0, []),
                                 ("fell-low-ceiling", destination_y + 5.0, ceiling)):
        brush = _model_collision_brushes(_staircase() + extra, solid=True)[0]
        player = _player_at(x=-20.0, y=destination_y)
        player.velocity = glm.vec3(200.0, -800.0 / 60.0, 0.0)
        player.on_ground = True
        _aabb, meshes, shapes = _gather([brush], [])
        player._resolve_mesh_capsule(meshes, shapes, sweep_start=(-23.333, start_y, 0.0))
        lifted[name] = player.pos.y

    assert lifted["level"] == pytest.approx(62.0, abs=0.05)
    assert lifted["fell"] == pytest.approx(62.0, abs=0.05), "open: still steps"
    assert lifted["fell-low-ceiling"] < 55.0, "the start's real lift meets the ceiling"
