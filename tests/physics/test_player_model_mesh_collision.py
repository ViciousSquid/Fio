"""Player contact with Solid Prop model collision meshes.

Model Props are arbitrary triangle surfaces, not convex solids: the player must
land on deck/tread triangles without filling the gaps between them.
"""
import glm
import pytest

from editor.editor_state import EditorState
from engine.logic_thread import LogicThread
from engine.player import Player, _gather
from engine.prop_entity import Prop
from engine.threaded_game_state import ThreadedGameState

pytestmark = pytest.mark.qt


def _quad(a, b, c, d, normal):
    return [((a, b, c), normal), ((a, c, d), normal)]


def _platform(y=0.0, x0=-100.0, x1=100.0):
    a, b = (x0, y, -100.0), (x0, y, 100.0)
    c, d = (x1, y, 100.0), (x1, y, -100.0)
    # Winding gives the top face an upward normal.
    return _quad(a, b, c, d, (0.0, 1.0, 0.0))


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

