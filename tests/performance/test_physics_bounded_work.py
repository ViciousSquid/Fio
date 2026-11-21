"""Soma's broad phase and repack do work bounded by what changed.

Two costs that grew with the wrong thing:

* The broad phase walks every grid cell a body's swept box crosses, once a
  cell, so a body's speed cost as its square: a step at ten million units a
  second took seconds. A body is now held to ``Soma.MAX_SPEED``, and a box
  wider than ``MAX_WALK_CELLS`` (``Soma._WALK_CELLS`` for bodies) -- a vast
  body, bad data -- is answered by testing the filed cells against it.
* Removing a body repacked every row at once, so a level despawning fifty
  bodies of four thousand paid fifty repacks; and a repack re-read every
  body's descriptor in Python. Removals now wait for the next repack, and a
  repack reads only the descriptors of bodies registered since.

These count the work, not the time (see test_hot_path_integrity.py); what
the game sees is held by ``tests/physics/test_speed_limit_and_removal.py``.
"""
import numpy as np
import pytest

from engine import spatial_grid as grid_module
from engine.spatial_grid import SpatialGrid, _CellRows
from tests.physics.test_dynamic_bodies import _drum_brush, _floor, _prop, _world

pytestmark = pytest.mark.perf

STEP = 1.0 / 60.0


def _build(*props):
    world = _world(_floor(size=(40000.0, 100.0, 40000.0)))
    world.rebuild([_drum_brush(prop) for prop in props])
    world.step(STEP)
    return world


@pytest.fixture
def cell_lookups(monkeypatch):
    """Every batch of cell lookups the grid's broad phase makes."""
    calls = []
    real = _CellRows._pairs_filed

    def counted(self, cx, cz):
        calls.append(len(cx))
        return real(self, cx, cz)

    monkeypatch.setattr(_CellRows, '_pairs_filed', counted)
    return calls


def _count_repacks(monkeypatch, world):
    """How many times *world*'s rows are actually rebuilt (a clean _pack is
    free)."""
    calls = []
    real = world._pack

    def counted():
        if world._dirty:
            calls.append(len(world._entities))
        return real()

    monkeypatch.setattr(world, '_pack', counted)
    return calls


@pytest.mark.parametrize("push", [1e4, 1e7, 1e30])
def test_a_step_walks_as_many_cells_however_fast_a_body_goes(push, cell_lookups):
    prop = _prop(friction=0.0, linear_damping=0.0)
    world = _build(prop)
    world.impulse(prop, [push, 0.0, push])
    cell_lookups.clear()
    world.step(STEP)
    world.step(STEP)
    # MAX_SPEED is a column a 60 Hz tick: a few lookups a step, at most.
    assert len(cell_lookups) <= 2 * 9


def test_a_box_wider_than_the_walk_limit_is_not_walked(cell_lookups):
    brushes = [{'pos': [float(x), 0.0, 0.0], 'size': [64.0, 64.0, 64.0],
                'is_trigger': False} for x in range(-20000, 20001, 1000)]
    grid = SpatialGrid(cell_size=512.0)
    grid.populate(brushes)
    lo = np.array([[-1e7, -10.0, -1e7], [0.0, 0.0, 0.0]])
    hi = np.array([[1e7, 10.0, 1e7], [600.0, 10.0, 600.0]])
    query, row, _rows = grid.box_pairs(lo, hi)
    assert len(cell_lookups) <= grid_module.MAX_WALK_CELLS
    # And finds every one (once a cell it is filed in, as a walk would).
    assert len(set(row[query == 0].tolist())) == len(brushes)


def test_a_vast_body_does_not_make_every_body_walk_its_reach(monkeypatch):
    props = [_prop((float(i % 10) * 80, 200.0, float(i // 10) * 80)) for i in range(50)]
    world = _world(_floor(size=(40000.0, 100.0, 40000.0)))
    vast = _prop((-15000.0, 0.0, -15000.0))
    brush = _drum_brush(vast)
    brush['size'] = [10000.0, 50.0, 10000.0]             # its reach is every body's
    world.rebuild([_drum_brush(p) for p in props] + [brush])
    world.step(STEP)
    for prop in props:
        world.wake(prop, [30.0, 0.0, 0.0])

    scanned = []
    real = world._bodies_spanned

    def counted(wide, *args):
        scanned.append(len(wide))
        return real(wide, *args)

    monkeypatch.setattr(world, '_bodies_spanned', counted)
    world.step(STEP)
    # Each awake body's box, grown by the vast one's reach, is about twenty
    # columns square: tested against the bodies, not walked cell by cell.
    assert scanned and scanned[0] >= len(props)


def test_removing_many_bodies_repacks_once(monkeypatch):
    props = [_prop((float(i % 20) * 80, 0.0, float(i // 20) * 80)) for i in range(200)]
    world = _build(*props)
    repacks = _count_repacks(monkeypatch, world)

    for prop in props[:50]:
        assert world.remove_body(prop) is True
    assert repacks == []

    world.step(STEP)
    assert repacks == [200]                              # the list before it drops them
    assert len(world._entities) == 150


def test_a_repack_reads_only_the_descriptors_registered_since():
    props = [_prop((float(i % 20) * 80, 0.0, float(i // 20) * 80)) for i in range(200)]
    world = _build(*props)
    reads = []

    class Counted(tuple):
        def __iter__(self):
            reads.append(1)
            return super().__iter__()

    for body in world.bodies.values():
        body._initial = Counted(body._initial)

    world.add_body(_drum_brush(_prop((0.0, 0.0, 5000.0))))
    world.remove_body(props[7])
    world.step(STEP)
    assert reads == []
    assert len(world._entities) == 200
