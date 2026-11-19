"""Soak: dormant-body activation and the change journal over many ticks.

The 3.5.0.1611 physics changes (dormant activation, journalled hidden/disabled
transitions, ``_pending_settle``, the filtered physics journal sink) moved
work from "every tick, every body" to "only what changed". These soaks hold
that over thousands of ticks: toggling flags on a crowd of mostly sleeping
bodies, repacking between, and building and discarding whole worlds -- the
per-tick cost, the traced heap and the journal's subscriber list stay flat,
and no body is left with placement work pending.

No Qt and no GPU: runs in the headless tier when ``--run-soak`` is given.
"""

import gc

import numpy as np
import pytest

from engine.change_journal import JOURNAL, touch
from engine.physics.soma import Soma
from engine.spatial_grid import SpatialGrid
from engine.prop_entity import Prop
from engine.spatial import set_authored_flag
from tests.helpers.soak import Soak, cycles

pytestmark = [pytest.mark.soak, pytest.mark.slow]

DRUM_SIZE = (45.64271, 65.181947, 45.64271)
DT = 1.0 / 60.0


def _floor():
    return {'pos': [0.0, -50.0, 0.0], 'size': [4000.0, 100.0, 4000.0],
            'is_trigger': False}


def _prop(pos, **props):
    properties = {
        'physics_enabled': True, 'no_collision': False, 'mass': 1.0,
        'gravity': True, 'friction': 0.55, 'linear_damping': 0.08,
        'rotation': [0.0, 0.0, 0.0], 'drop_angular_velocity': [0.0, 0.0, 0.0],
    }
    properties.update(props)
    return Prop(pos=list(pos), properties=properties)


def _body(prop):
    x, y, z = prop.pos
    return {'pos': [x, y + DRUM_SIZE[1] * 0.5, z], 'size': list(DRUM_SIZE),
            '_physics_body': True, '_physics_entity': prop,
            '_collision_mode': 'aabb'}


def _crowd(side):
    """side x side drums; every fourth starts dormant, a few start sunk."""
    props = []
    for i in range(side * side):
        x = (i % side - side / 2) * 90.0
        z = (i // side - side / 2) * 90.0
        y = -DRUM_SIZE[1] * 0.5 if i % 11 == 0 else 0.0
        flag = ('hidden' if i % 8 == 0 else 'disabled') if i % 4 == 0 else None
        props.append(_prop((x, y, z), **({flag: True} if flag else {})))
    return props


def _world():
    grid = SpatialGrid(cell_size=512.0)
    grid.populate([_floor()])
    return Soma(grid)


def _live_sinks():
    return sum(1 for sink in JOURNAL._live if sink.alive)


def test_flag_churn_on_a_sleeping_crowd_stays_sparse_and_flat():
    props = _crowd(16)
    brushes = [_body(p) for p in props]
    world = _world()
    world.rebuild(brushes)
    for _ in range(120):            # let the awake ones come to rest
        world.step(DT)

    rng = np.random.default_rng(1611)
    total = cycles(3000, minimum=900)
    # Warm-up spans two repacks: the first one fills the static query cache.
    with Soak("physics flag churn (256 bodies)", total, warmup=520) as soak:
        for tick in range(total):
            for index in rng.choice(len(props), size=6, replace=False):
                prop = props[int(index)]
                flag = 'hidden' if index % 2 else 'disabled'
                set_authored_flag(prop, flag, not prop.properties.get(flag, False))
            if tick % 97 == 0:
                # A plain STATE touch that changes nothing must stay cheap.
                touch(props[tick % len(props)])
            world.step(DT)
            if tick % 250 == 249:
                world.rebuild(brushes)      # repack keeps history by identity
            soak.cycle_done(counters={
                'live journal sinks': _live_sinks(),
                'journal walk list': len(JOURNAL._live),
            })

    soak.assert_time_flat(factor=2.0, slack_seconds=0.001)
    soak.assert_heap_flat(per_cycle_bytes=64)
    soak.assert_counter_flat('live journal sinks')
    soak.assert_counter_flat('journal walk list')

    # Wake everything: nothing may be left pending, sunk or non-finite.
    for prop in props:
        set_authored_flag(prop, 'hidden', False)
        set_authored_flag(prop, 'disabled', False)
    for _ in range(90):
        world.step(DT)
    assert not world._pending_settle.any()
    positions = np.array([p.pos for p in props], dtype=np.float64)
    assert np.isfinite(positions).all()
    assert positions[:, 1].min() > -1.0, "a re-enabled body was left sunk"


def test_building_and_discarding_worlds_leaves_no_journal_subscribers():
    """Each Soma subscribes to the process-wide journal. Worlds come
    and go with every Play session and level change; their sinks must go with
    them, or every later ``touch`` walks the dead ones."""
    gc.collect()
    baseline_live = _live_sinks()
    total = cycles(400)
    with Soak("physics world churn", total, warmup=20) as soak:
        for _ in range(total):
            props = [_prop(((i % 4) * 90.0, 0.0, (i // 4) * 90.0),
                           **({'disabled': True} if i % 3 == 0 else {}))
                     for i in range(16)]
            world = _world()
            world.rebuild([_body(p) for p in props])
            for _ in range(3):
                world.step(DT)
            set_authored_flag(props[0], 'disabled', False)
            world.step(DT)
            world.clear()
            del world, props
            soak.cycle_done(counters={'live journal sinks': _live_sinks(),
                                      'journal walk list': len(JOURNAL._live)})

    gc.collect()
    assert _live_sinks() <= baseline_live + 1
    # Dead sinks are pruned on the next subscribe/drain, so the list a touch
    # walks stays bounded by the live subscribers plus at most one generation.
    soak.assert_counter_flat('journal walk list', slack=2)
    soak.assert_heap_flat(per_cycle_bytes=128)
    soak.assert_time_flat(factor=2.0, slack_seconds=0.002)
