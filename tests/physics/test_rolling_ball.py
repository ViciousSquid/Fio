"""The Ball: a rolling, bouncing, tintable Prop (engine.rolling)."""
import glm
import numpy as np
import pytest

from engine import rolling
from engine.entity_table import _model_recipe, _model_transform_columns
from engine.obj_loader import OBJLoader
from engine.physics import PhysicsWorld, SpatialGrid
from engine.player import Player
from engine.prop_entity import BALL_MODEL_PATH, Prop
from engine.shaders import DEFAULT_SHADERS

FRAME = 1.0 / 60.0


def _floor():
    return {'pos': [0.0, -50.0, 0.0], 'size': [2000.0, 100.0, 2000.0],
            'is_trigger': False}


def _wall(x=120.0):
    return {'pos': [x, 100.0, 0.0], 'size': [40.0, 200.0, 400.0],
            'is_trigger': False}


def _ball_world(pos=(0.0, 25.0, 0.0), statics=None, **props):
    grid = SpatialGrid(cell_size=512.0)
    grid.populate(statics or [_floor()])
    world = PhysicsWorld(grid)
    ball = Prop.for_ball(pos=list(pos), properties=props)
    world.rebuild([{
        'pos': list(pos), 'size': [50.0, 50.0, 50.0],
        '_physics_body': True, '_physics_entity': ball,
        '_collision_mode': 'aabb',
    }])
    return world, ball


def _player(x, vx=0.0):
    player = Player(x, 0.0)
    player.pos = glm.vec3(x, 50.0, 0.0)
    player.velocity = glm.vec3(vx, 0.0, 0.0)
    player.width, player.height, player.depth = 50.0, 100.0, 50.0
    return player


def _run(world, frames, player=None):
    for _ in range(frames):
        world.step(FRAME, player)


# -- the model ---------------------------------------------------------------

def test_ball_model_is_a_golf_ball_half_the_player_centred_and_textured():
    loader = OBJLoader()
    assert loader.load(BALL_MODEL_PATH)
    vertices = np.asarray(loader.vertices)
    size = vertices.max(axis=0) - vertices.min(axis=0)
    # The player is 100 units tall; the ball is half that, on every axis.
    assert np.allclose(size, 50.0, atol=1e-3)
    assert np.allclose(vertices.max(axis=0) + vertices.min(axis=0), 0.0, atol=1e-3)
    assert loader.up_axis == 'y'
    assert all(material['texture'] for material in loader.materials.values())
    # Low-poly: an icosahedron with every edge split in two.
    assert len(loader.faces) == 80
    radii = np.linalg.norm(vertices, axis=1)
    assert np.allclose(radii, 25.0, atol=1e-3)


def test_ball_defaults_are_a_rolling_solid_physics_prop():
    ball = Prop.for_ball(pos=[1, 2, 3])
    p = ball.properties
    assert p['render_mode'] == 'model' and p['model_path'] == BALL_MODEL_PATH
    assert p['physics_enabled'] and not p['no_collision']
    assert p['collision_shape'] == 'aabb'
    assert p['rolls'] and p['bounciness'] > 0.0
    assert p['mass'] == 1.0


# -- tint ----------------------------------------------------------------------

def test_tint_rides_in_the_model_recipe_and_white_is_no_tint():
    assert _model_recipe(Prop.for_ball())[3] is None
    red = Prop.for_ball(properties={'tint': [1.0, 0.0, 0.0]})
    assert _model_recipe(red) == (BALL_MODEL_PATH, None, None, (1.0, 0.0, 0.0))
    bytes_tint = Prop.for_ball(properties={'tint': [255, 128, 0]})
    assert _model_recipe(bytes_tint)[3] == pytest.approx((1.0, 128 / 255, 0.0))


@pytest.mark.parametrize('name', ['textured.frag', 'textured_arm.frag'])
def test_textured_shaders_take_a_model_tint_that_defaults_to_none(name):
    source = DEFAULT_SHADERS[name]
    assert 'uniform vec4 model_tint;' in source
    # An unset uniform is all zeros: alpha 0 must leave brushes untinted.
    assert 'mix(vec3(1.0), model_tint.rgb, model_tint.a)' in source


# -- orientation ---------------------------------------------------------------

def test_quaternion_round_trip_matches_how_models_are_drawn():
    rng = np.random.default_rng(3)
    rotations = rng.uniform(-170.0, 170.0, size=(20, 3))
    rotations[:, 0] = rng.uniform(-85.0, 85.0, size=20)
    back = rolling.rotation_from_quat(rolling.quat_from_rotation(rotations))
    for before, after in zip(rotations, back):
        a = Prop(properties={'model_path': BALL_MODEL_PATH, 'rotation': list(before)})
        b = Prop(properties={'model_path': BALL_MODEL_PATH, 'rotation': list(after)})
        assert np.allclose(_model_transform_columns(a)[0],
                           _model_transform_columns(b)[0], atol=1e-5)


# -- rolling -------------------------------------------------------------------

def test_a_ball_turns_by_the_distance_it_rolls():
    world, ball = _ball_world()
    world.wake(ball, [200.0, 0.0, 0.0])
    start_x = ball.pos[0]
    _run(world, 20)
    rolled = ball.pos[0] - start_x
    # Rolling along +X turns the ball about -Z by distance / radius.
    expected = -np.degrees(rolled / 25.0)
    quat = rolling.quat_from_rotation(ball.properties['rotation'])[0]
    angle = np.degrees(2.0 * np.arctan2(np.linalg.norm(quat[1:]), quat[0]))
    assert angle == pytest.approx(abs(expected), rel=0.02)
    assert np.sign(quat[3]) == np.sign(expected)
    assert abs(quat[1]) < 1e-6 and abs(quat[2]) < 1e-6


def test_a_ball_rolls_further_than_the_same_prop_slides():
    rolled_world, ball = _ball_world()
    slid_world, crate = _ball_world(rolls=False)
    for world, prop in ((rolled_world, ball), (slid_world, crate)):
        world.wake(prop, [300.0, 0.0, 0.0])
        _run(world, 60)
    assert ball.pos[0] > crate.pos[0] * 2.0


# -- bounce and mass -------------------------------------------------------------

def _bounce_peaks(mass):
    world, ball = _ball_world(pos=(0.0, 200.0, 0.0), mass=mass, bounciness=0.6)
    world.wake(ball)
    heights = []
    for _ in range(400):
        world.step(FRAME)
        heights.append(ball.pos[1])
    peaks = [heights[i] for i in range(1, len(heights) - 1)
             if heights[i - 1] < heights[i] >= heights[i + 1]]
    return peaks, heights[-1], world.get_body(ball).awake


def test_a_light_ball_bounces_lower_each_time_then_rests():
    peaks, final, awake = _bounce_peaks(1.0)
    assert len(peaks) >= 2
    assert all(later < earlier for earlier, later in zip(peaks, peaks[1:]))
    assert final == pytest.approx(25.0) and not awake


def test_mass_takes_the_bounce_away():
    light, _, _ = _bounce_peaks(1.0)
    medium, _, _ = _bounce_peaks(5.0)
    heavy, final, _ = _bounce_peaks(rolling.HEAVY_MASS)
    assert medium[0] < light[0]
    assert heavy == [] and final == pytest.approx(25.0)
    assert rolling.restitution(0.6, rolling.HEAVY_MASS) == 0.0
    assert rolling.restitution(0.6, 1000.0) == 0.0


# -- the player ----------------------------------------------------------------

def test_the_player_pushes_a_light_ball():
    world, ball = _ball_world(pos=(40.0, 25.0, 0.0))
    player = _player(-30.0, 120.0)
    for _ in range(30):
        player.pos.x += player.velocity.x * FRAME
        world.step(FRAME, player)
    assert ball.pos[0] > 60.0


def test_a_heavy_ball_is_not_pushed_it_blocks_the_player():
    world, ball = _ball_world(pos=(40.0, 25.0, 0.0), mass=rolling.HEAVY_MASS)
    player = _player(-30.0, 120.0)
    for _ in range(30):
        player.pos.x += player.velocity.x * FRAME
        world.step(FRAME, player)
    assert ball.pos[0] == pytest.approx(40.0)
    # Never further in than touching the ball.
    assert player.pos.x <= 40.0 - 50.0 + 1.0


def test_a_rolling_heavy_ball_shoves_the_player_along():
    world, ball = _ball_world(pos=(-200.0, 25.0, 0.0), mass=50.0)
    world.wake(ball, [300.0, 0.0, 0.0])
    player = _player(0.0)
    for _ in range(60):
        player.pos.x += player.velocity.x * FRAME
        world.step(FRAME, player)
    assert player.pos.x > 100.0
    assert player.velocity.x > 200.0
    assert not world.player_crushed


def test_a_heavy_ball_crushes_a_player_pinned_against_a_wall():
    world, ball = _ball_world(pos=(-150.0, 25.0, 0.0), mass=50.0,
                              statics=[_floor(), _wall()])
    world.wake(ball, [300.0, 0.0, 0.0])
    player = _player(60.0)
    crushed = False
    for _ in range(90):
        world.step(FRAME, player)
        crushed |= world.player_crushed
    assert crushed


@pytest.mark.parametrize('mass, speed', [(5.0, 300.0), (50.0, 0.0)])
def test_only_a_moving_heavy_ball_crushes(mass, speed):
    world, ball = _ball_world(pos=(-150.0, 25.0, 0.0), mass=mass,
                              statics=[_floor(), _wall()])
    world.wake(ball, [speed, 0.0, 0.0])
    player = _player(60.0)
    for _ in range(90):
        world.step(FRAME, player)
        assert not world.player_crushed


# -- cost ------------------------------------------------------------------------

def test_a_world_without_a_ball_never_enters_the_rolling_code(monkeypatch):
    world, crate = _ball_world(rolls=False, bounciness=0.0)
    assert not world._any_rolls and not world._any_bounce

    def fail(*args, **kwargs):
        raise AssertionError('rolling code ran without a ball')

    monkeypatch.setattr(world, '_roll', fail)
    monkeypatch.setattr(world, '_heavy_balls_push', fail)
    monkeypatch.setattr(rolling, 'bounce_speed', fail)
    world.wake(crate, [300.0, 0.0, 0.0])
    _run(world, 30, _player(-200.0, 100.0))
