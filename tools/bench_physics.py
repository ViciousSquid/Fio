"""What dynamic bodies (physics Props) cost a tick.

    python tools/bench_physics.py [TREE] [--json OUT.json]

A floor with 1500 pillars scattered over it and N physics bodies sliding
among them, never allowed to sleep, so every one pays the whole step: the
wall test on each axis, the floor query, the integration and the hand-back
to its entity. Three more rows bound the range: every body asleep (what a
world at rest costs), the same with the player in it (what a session pays
every tick: the player is tested against the bodies), and one body awake
among 4096 asleep (what touching one costs in a full world).

Reports the median step in microseconds, and microseconds a body. Run the
trees to compare interleaved (``taskset -c 3``): the numbers move with the
machine, the ratio between two trees much less.
"""
import argparse
import gc
import json
import os
import statistics
import sys
import time

DT = 1.0 / 60.0
COUNTS = (1, 4, 16, 64, 256, 1024, 4096)


def _box(pos, size, **extra):
    brush = {'pos': [float(v) for v in pos], 'size': [float(v) for v in size],
             'is_trigger': False}
    brush.update(extra)
    return brush


def level(rng):
    brushes = [_box((0, -50, 0), (20000, 100, 20000))]
    for _ in range(1500):
        brushes.append(_box((rng.uniform(-6000, 6000), 60, rng.uniform(-6000, 6000)),
                            (rng.uniform(40, 300), 120, rng.uniform(40, 300))))
    return brushes


def build(count, rng, statics):
    from engine.physics.soma import Soma
    from engine.spatial_grid import SpatialGrid
    from engine.prop_entity import Prop

    grid = SpatialGrid(cell_size=512.0)
    grid.populate(statics)
    world = Soma(grid)
    bodies = []
    for _ in range(count):
        prop = Prop(pos=[rng.uniform(-5000, 5000), 0.0, rng.uniform(-5000, 5000)],
                    properties={
                        'physics_enabled': True, 'no_collision': False,
                        'mass': 1.0, 'gravity': True, 'friction': 0.0,
                        'linear_damping': 0.0, 'rotation': [0.0, 0.0, 0.0],
                        'drop_angular_velocity': [0.0, 0.0, 0.0]})
        x, y, z = prop.pos
        bodies.append({'pos': [x, y + 20.0, z], 'size': [40.0, 40.0, 40.0],
                       '_physics_body': True, '_physics_entity': prop})
    world.rebuild(bodies)
    return world, [b['_physics_entity'] for b in bodies]


def _player():
    import glm
    from engine.player import Player

    player = Player(9000.0, 9000.0)
    player.pos = glm.vec3(9000.0, 50.0, 9000.0)
    player.velocity = glm.vec3(0.0, 0.0, 0.0)
    return player


def _median_step(world, steps, warm, player=None):
    for _ in range(warm):
        world.step(DT, player)
    samples = []
    gc.collect()
    gc.disable()
    try:
        for _ in range(steps):
            start = time.perf_counter()
            world.step(DT, player)
            samples.append(time.perf_counter() - start)
    finally:
        gc.enable()
    return statistics.median(samples) * 1e6


def run():
    import random

    rows = []
    for count in COUNTS:
        rng = random.Random(7)
        world, props = build(count, rng, level(rng))
        world.sleep_enabled = False
        for prop in props:
            world.wake(prop, [rng.uniform(-200, 200), 0.0, rng.uniform(-200, 200)])
        steps = 400 if count <= 256 else 60
        micros = _median_step(world, steps, 10)
        rows.append({'scenario': 'awake', 'bodies': count, 'awake': count,
                     'step_us': micros, 'body_us': micros / count})

    rng = random.Random(7)
    world, props = build(4096, rng, level(rng))
    world.step(DT)
    micros = _median_step(world, 400, 10)
    rows.append({'scenario': 'asleep', 'bodies': 4096, 'awake': 0,
                 'step_us': micros, 'body_us': micros / 4096})
    micros = _median_step(world, 400, 10, _player())
    rows.append({'scenario': 'asleep+player', 'bodies': 4096, 'awake': 0,
                 'step_us': micros, 'body_us': micros / 4096})
    world.sleep_enabled = False
    world.wake(props[0], [150.0, 0.0, 90.0])
    micros = _median_step(world, 400, 10)
    rows.append({'scenario': 'one-awake', 'bodies': 4096, 'awake': 1,
                 'step_us': micros, 'body_us': micros})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('tree', nargs='?', default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), os.pardir))
    parser.add_argument('--json')
    args = parser.parse_args()
    sys.path.insert(0, os.path.abspath(args.tree))

    rows = run()
    print('%-14s %7s %7s %12s %10s' % ('scenario', 'bodies', 'awake',
                                       'step (us)', 'us/body'))
    for row in rows:
        print('%-14s %7d %7d %12.1f %10.2f' % (
            row['scenario'], row['bodies'], row['awake'], row['step_us'],
            row['body_us']))
    if args.json:
        with open(args.json, 'w') as handle:
            json.dump(rows, handle, indent=2)


if __name__ == '__main__':
    main()
