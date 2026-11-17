"""What the engine and plugins can ask of Soma beyond stepping it.

Impulses, bodies that come and go while the world runs, listeners for rest,
events for whoever subscribes, queries over the bodies, and control of a
body's state -- the contract's members (``engine/physics/api.py``).
"""
import numpy as np
import pytest

from engine.spatial import set_live_flag
from tests.physics.test_dynamic_bodies import (
    DRUM_SIZE, _drum_brush, _floor, _player, _prop, _run, _world)

WIDTH, HEIGHT = DRUM_SIZE[0], DRUM_SIZE[1]
SLIDES = {'friction': 0.0, 'linear_damping': 0.0}


def _build(*props):
    world = _world(_floor())
    world.rebuild([_drum_brush(prop) for prop in props])
    world.step(1.0 / 60.0)
    return world


# -- impulses -----------------------------------------------------------------

def test_an_impulse_moves_a_heavy_body_less_than_a_light_one():
    light = _prop(**SLIDES)
    heavy = _prop((0.0, 0.0, 300.0), mass=5.0, **SLIDES)
    world = _build(light, heavy)

    assert world.impulse(light, [100.0, 0.0, 0.0]) is True
    assert world.impulse(heavy, [100.0, 0.0, 0.0]) is True
    assert world.get_body(light).velocity[0] == pytest.approx(100.0)
    assert world.get_body(heavy).velocity[0] == pytest.approx(20.0)
    assert world.get_body(light).awake and world.get_body(heavy).awake

    world.impulse(light, [50.0, 0.0, 0.0])                     # it adds
    assert world.get_body(light).velocity[0] == pytest.approx(150.0)


def test_an_impulse_is_refused_where_there_is_nothing_to_push():
    prop = _prop()
    stranger = _prop((500.0, 0.0, 0.0))
    world = _build(prop)

    assert world.impulse(stranger, [100.0, 0.0, 0.0]) is False     # no body
    assert world.impulse(prop, [float('nan'), 0.0, 0.0]) is False  # not a push
    world.set_kinematic(prop, True)
    assert world.impulse(prop, [100.0, 0.0, 0.0]) is False         # carried
    world.set_kinematic(prop, False)
    set_live_flag(prop, 'disabled', True)
    world.step(1.0 / 60.0)
    assert world.impulse(prop, [100.0, 0.0, 0.0]) is False         # dormant
    assert world.get_body(prop).velocity == [0.0, 0.0, 0.0]


def test_a_radial_impulse_pushes_bodies_away_and_harder_the_nearer_they_are():
    near = _prop((40.0, 0.0, 0.0), **SLIDES)
    far = _prop((-150.0, 0.0, 0.0), **SLIDES)
    outside = _prop((0.0, 0.0, 600.0), **SLIDES)
    world = _build(near, far, outside)

    pushed = world.impulse_radial([0.0, HEIGHT * 0.5, 0.0], 200.0, 400.0)

    assert {id(p) for p in pushed} == {id(near), id(far)}
    assert world.get_body(near).velocity[0] > 0.0 > world.get_body(far).velocity[0]
    assert abs(world.get_body(near).velocity[0]) > abs(world.get_body(far).velocity[0])
    assert world.get_body(outside).velocity == [0.0, 0.0, 0.0]
    assert world.get_body(outside).awake is False


# -- being told ---------------------------------------------------------------

def test_any_number_listen_for_rest_and_none_replaces_the_engines_own():
    prop = _prop((0.0, 200.0, 0.0))
    world = _build(prop)
    told = []
    world.set_rest_callback(prop, lambda e: told.append('engine'))    # OnRest
    first, second = (lambda e: told.append('first')), (lambda e: told.append('second'))
    world.on_rest(prop, first)
    world.on_rest(prop, second)
    world.on_rest(prop, second)                                # once, however often
    world.wake(prop)
    _run(world, 300)
    assert told == ['engine', 'first', 'second']

    world.off_rest(prop, first)
    world.wake(prop, [0.0, 200.0, 0.0])
    _run(world, 300)
    assert told == ['engine', 'first', 'second', 'engine', 'second']


def test_events_are_recorded_only_while_wanted():
    prop = _prop((0.0, 200.0, 0.0))
    world = _build(prop)
    world.wake(prop)
    _run(world, 300)
    assert world.drain_events() == []                          # nobody asked

    world.events_wanted = True
    world.wake(prop, [0.0, 300.0, 0.0])
    _run(world, 300)
    events = world.drain_events()
    kinds = [kind for kind, _entity, _data in events]
    assert kinds == ['landed', 'rest']
    assert all(entity is prop for _kind, entity, _data in events)
    assert events[0][2]['speed'] > 100.0
    assert world.drain_events() == []                          # drained


def test_a_contact_event_names_what_was_met():
    mover = _prop(**SLIDES)
    other = _prop((200.0, 0.0, 0.0), **SLIDES)
    wall = _floor((500.0 + 50.0, 200.0, 0.0), (100.0, 400.0, 400.0))
    world = _world(_floor(), wall)
    world.rebuild([_drum_brush(mover), _drum_brush(other)])
    world.step(1.0 / 60.0)
    world.events_wanted = True
    world.wake(mover, [300.0, 0.0, 0.0])
    _run(world, 240)

    contacts = [(entity, data) for kind, entity, data in world.drain_events()
                if kind == 'contact']
    met_body = [data for entity, data in contacts if entity is mover and data['other'] is other]
    met_wall = [data for entity, data in contacts if data['other'] is None]
    assert met_body and met_body[0]['axis'] == 0 and met_body[0]['speed'] == pytest.approx(300.0)
    assert met_wall                                            # one of them reached it


def test_a_toppled_event_says_which_way():
    barrel = _prop(topples=True)
    world = _build(barrel)
    world.events_wanted = True
    world.wake(barrel, [0.0, 0.0, -300.0])
    _run(world, 30)
    toppled = [data for kind, entity, data in world.drain_events() if kind == 'toppled']
    assert toppled == [{'axis': 2, 'sign': -1.0}]


# -- bodies that come and go --------------------------------------------------

def test_adding_a_body_leaves_the_others_as_they_are():
    """A physics Prop spawned in play: the bodies in motion keep moving."""
    slider = _prop(**SLIDES)
    faller = _prop((0.0, 400.0, 300.0))
    below = _prop((300.0, 0.0, 300.0))
    above = _prop((300.0, HEIGHT, 300.0))
    world = _build(slider, faller, below, above)
    world.wake(slider, [90.0, 0.0, 0.0])
    world.wake(faller)
    _run(world, 20)
    before = (list(slider.pos), world.get_body(slider).velocity,
              list(faller.pos), world.get_body(faller).velocity)

    spawned = _prop((-300.0, 200.0, -300.0))
    body = world.add_body(_drum_brush(spawned))
    assert body is world.get_body(spawned)

    assert (list(slider.pos), world.get_body(slider).velocity,
            list(faller.pos), world.get_body(faller).velocity) == before
    assert world.get_body(slider).awake and world.get_body(faller).awake
    assert world.get_body(above).resting_on is below           # links renumbered
    world.wake(spawned)
    _run(world, 300)
    assert spawned.pos[1] == pytest.approx(0.0, abs=1e-3)      # it is simulated
    assert slider.pos[0] > before[0][0]                        # and so are they


def test_removing_a_body_drops_what_stood_on_it_and_keeps_the_rest():
    below = _prop()
    above = _prop((0.0, HEIGHT, 0.0))
    slider = _prop((0.0, 0.0, 400.0), **SLIDES)
    world = _build(below, above, slider)
    world.wake(slider, [90.0, 0.0, 0.0])
    _run(world, 20)

    assert world.remove_body(below) is True
    assert world.get_body(below) is None
    assert world.remove_body(below) is False
    assert world.get_body(slider).velocity[0] == pytest.approx(90.0)
    _run(world, 300)
    assert above.pos[1] == pytest.approx(0.0, abs=1e-3)

    assert world.remove_body(above) and world.remove_body(slider)   # the last ones
    world.step(1.0 / 60.0)                                     # an empty world steps


def test_a_body_registered_again_starts_afresh():
    prop = _prop(**SLIDES)
    world = _build(prop)
    world.wake(prop, [90.0, 0.0, 0.0])
    _run(world, 10)
    prop.pos = [0.0, 0.0, 0.0]
    world.add_body(_drum_brush(prop))                          # re-registered
    assert world.get_body(prop).velocity == [0.0, 0.0, 0.0]
    assert world.get_body(prop).awake is False


# -- asking -------------------------------------------------------------------

def test_bodies_are_found_by_box_by_radius_and_by_ray():
    a = _prop((0.0, 0.0, 0.0))
    b = _prop((200.0, 0.0, 0.0))
    c = _prop((0.0, 0.0, 900.0))
    hidden = _prop((100.0, 0.0, 0.0), hidden=True)
    world = _build(a, b, c, hidden)

    def ids(found):
        return {id(e) for e in found}

    assert ids(world.bodies_in_box([-50, -10, -50], [250, 100, 50])) == ids([a, b])
    assert ids(world.bodies_near([0.0, HEIGHT * 0.5, 0.0], 60.0)) == ids([a])
    assert ids(world.bodies_near([0.0, HEIGHT * 0.5, 0.0], 1000.0)) == ids([a, b, c])

    eye = [-300.0, HEIGHT * 0.5, 0.0]
    entity, distance = world.raycast(eye, [1.0, 0.0, 0.0], 1000.0)
    assert entity is a and distance == pytest.approx(300.0 - WIDTH * 0.5)
    assert world.raycast(eye, [1.0, 0.0, 0.0], 100.0) is None          # out of reach
    assert world.raycast(eye, [-1.0, 0.0, 0.0], 1000.0) is None        # behind it
    assert world.raycast(eye, [0.0, 0.0, 0.0], 1000.0) is None         # no direction
    entity, _distance = world.raycast([200.0, 500.0, 0.0], [0.0, -1.0, 0.0], 1000.0)
    assert entity is b                                         # straight down


def test_a_bodys_state_is_read_and_its_material_changed_while_it_runs():
    below = _prop()
    above = _prop((0.0, 200.0, 0.0))
    world = _build(below, above)
    world.wake(above)
    _run(world, 300)
    body = world.get_body(above)

    assert body.position == pytest.approx([0.0, HEIGHT, 0.0], abs=1e-3)
    assert body.grounded is True
    assert body.resting_on is below
    assert world.get_body(below).resting_on is None
    assert body.toppled is False

    body.mass = 4.0
    world.impulse(above, [100.0, 0.0, 0.0])
    assert body.velocity[0] == pytest.approx(25.0)
    body.friction = 0.0
    assert body.friction == 0.0
    body.gravity = False                                       # and it wakes
    assert body.gravity is False and body.awake is True


def test_a_barrel_is_knocked_over_and_stood_up_on_request():
    barrel = _prop(topples=True)
    crate = _prop((300.0, 0.0, 0.0))
    world = _build(barrel, crate)

    assert world.topple(crate) is False                        # does not topple
    assert world.topple(barrel, [0.0, 0.0, -1.0]) is True
    assert world.get_body(barrel).toppled is True
    assert world.topple(barrel) is False                       # already over
    _run(world, 60)
    assert world.get_body(barrel).half_extents == pytest.approx(
        (WIDTH * 0.5, WIDTH * 0.5, HEIGHT * 0.5))

    assert world.stand(barrel) is True
    assert world.get_body(barrel).toppled is False
    assert barrel.properties['rotation'] == [0.0, 0.0, 0.0]
    assert world.stand(barrel) is False
    _run(world, 120)
    assert barrel.pos[1] == pytest.approx(0.0, abs=1e-3)       # standing on the floor


# -- the player ---------------------------------------------------------------

def test_a_player_whose_physics_is_off_is_not_handed_to_physics():
    """Noclip, a spectator's free camera: the engine passes no player, so it
    pushes nothing. (What the deathmatch spectator used to patch ``step``
    for.)"""
    source = open(__import__('engine.logic_thread', fromlist=['x']).__file__, encoding='utf-8').read()
    assert "if not getattr(player, 'physics_enabled', True):" in source

    prop = _prop()
    world = _build(prop)
    player = _player()
    world.step(1.0 / 60.0, None)                               # as the engine does
    assert prop.pos[0] == 0.0 and world.get_body(prop).awake is False
    world.step(1.0 / 60.0, player)
    assert prop.pos[0] > 0.0


def test_queries_agree_with_reading_every_body_whatever_their_size():
    """The index is an optimisation: a box of a few cells, of many columns,
    or of the whole world finds exactly the bodies whose boxes it overlaps."""
    rng = np.random.default_rng(11)
    props = [_prop((float(x), 0.0, float(z)))
             for x, z in rng.uniform(-6000.0, 6000.0, size=(150, 2))]
    world = _build(*props)
    centre = world._position + world._offset
    half = world._half
    for _ in range(60):
        middle = rng.uniform(-6000.0, 6000.0, size=3) * (1.0, 0.0, 1.0)
        reach = float(rng.choice([30.0, 400.0, 3000.0, 40000.0, 1e9]))
        lo = (middle - reach).astype(np.float32)
        hi = (middle + reach).astype(np.float32)
        expected = np.flatnonzero(np.all((centre + half > lo) & (centre - half < hi), axis=1))
        found = world.bodies_in_box(lo, hi)
        assert [id(e) for e in found] == [id(props[i]) for i in expected]   # in row order
        near = np.flatnonzero(np.linalg.norm(centre - middle, axis=1) <= reach)
        assert {id(e) for e in world.bodies_near(middle, reach)} >= {
            id(props[i]) for i in near if np.linalg.norm(centre[i] - middle) < reach - 1.0}


def test_a_query_that_makes_no_sense_finds_nothing_and_raises_nothing():
    world = _build(_prop((0.0, 0.0, 0.0)))
    nan, inf = float('nan'), float('inf')
    assert world.bodies_in_box([nan, 0, 0], [1, 1, 1]) == []
    assert world.bodies_in_box([50, 0, 0], [-50, 100, 100]) == []      # inside out
    assert world.bodies_near([nan, 0.0, 0.0], 100.0) == []
    assert len(world.bodies_in_box([-inf] * 3, [inf] * 3)) == 1        # everywhere
    assert world.raycast([nan, 0, 0], [1, 0, 0], 100.0) is None
    assert world.raycast([0, 0, 0], [inf, 0, 0], 100.0) is None
    assert world.raycast([0, 0, 0], [1, 0, 0], nan) is None
    assert world.raycast([0, 0], [1, 0, 0], 100.0) is None
    assert world.raycast('here', [1, 0, 0], 100.0) is None
    entity, _d = world.raycast([-300.0, HEIGHT * 0.5, 0.0], [1, 0, 0], inf)
    assert entity is not None                                          # no end to it


def test_a_ray_meets_the_nearest_of_the_bodies_along_it_at_any_angle():
    rng = np.random.default_rng(5)
    props = [_prop((float(x), 0.0, float(z)))
             for x, z in rng.uniform(-1500.0, 1500.0, size=(80, 2))]
    world = _build(*props)
    centre = (world._position + world._offset).astype(np.float64)
    half = world._half.astype(np.float64)
    for _ in range(80):
        origin = rng.uniform(-1500.0, 1500.0, size=3) * (1.0, 0.0, 1.0) + (0.0, HEIGHT * 0.5, 0.0)
        ray = rng.normal(size=3) * (1.0, 0.05, 1.0)
        ray /= np.linalg.norm(ray)
        with np.errstate(divide='ignore', invalid='ignore'):
            near, far = (centre - half - origin) / ray, (centre + half - origin) / ray
        enter, leave = np.minimum(near, far).max(axis=1), np.maximum(near, far).min(axis=1)
        hit = (enter <= leave) & (leave >= 0.0) & (enter <= 2000.0)
        found = world.raycast(origin, ray, 2000.0)
        if not hit.any():
            assert found is None
            continue
        distance = np.where(hit, np.maximum(enter, 0.0), np.inf)
        assert found is not None and found[1] == pytest.approx(distance.min(), abs=1e-3)
