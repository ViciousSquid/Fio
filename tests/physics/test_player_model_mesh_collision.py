"""Player contact with Solid Prop model collision meshes.

Model Props are arbitrary triangle surfaces, not convex solids: the player must
land on deck/tread triangles without filling the gaps between them.
"""
from types import SimpleNamespace

import glm
import pytest

from engine.logic_collision import LogicCollision
from engine.player import Player, _gather


def _quad(a, b, c, d, normal):
    return [((a, b, c), normal), ((a, c, d), normal)]


def _platform(y=0.0, x0=-100.0, x1=100.0):
    a, b = (x0, y, -100.0), (x0, y, 100.0)
    c, d = (x1, y, 100.0), (x1, y, -100.0)
    # Winding gives the top face an upward normal.
    return _quad(a, b, c, d, (0.0, 1.0, 0.0))


def _model_collision_brushes(triangles, *, solid=True):
    thing = SimpleNamespace(
        pos=[0.0, 0.0, 0.0],
        properties={
            'render_mode': 'model',
            'model_path': 'assets/models/test_catwalk.obj',
            'scale': [1.0, 1.0, 1.0],
            'rotation': [0.0, 0.0, 0.0],
            'collision_shape': 'mesh',
            'collision_size': [0.0, 0.0, 0.0],
            'physics_enabled': False,
            'no_collision': not solid,
        },
    )
    logic = SimpleNamespace(
        editor_state=SimpleNamespace(things=[thing]),
        session_runtime=SimpleNamespace(physics_enabled=True),
        explosion_runtime=None,
        package=None,
    )
    runtime = LogicCollision(logic)
    # The test supplies world-space triangles, as the CPU model loader does
    # after applying the Prop transform.
    runtime.compute_model_collision_mesh = lambda *_args: triangles
    return runtime.build_model_collision_brushes()


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
