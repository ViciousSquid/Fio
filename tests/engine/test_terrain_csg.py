"""Terrain AABB CSG regression tests."""

import numpy as np
import pytest

from engine.brush_geometry import is_plain_aabb_brush
from engine.terrain import BiomeConfig, Terrain


def _flat_terrain():
    terrain = Terrain(seed=7)
    terrain.biome = BiomeConfig(
        name="Test Flat",
        base_height=100.0,
        height_scale=0.0,
        hills_intensity=0.0,
        color_gradient=[(0.0, (0.5, 0.5, 0.5)), (1.0, (0.5, 0.5, 0.5))],
    )
    return terrain


def test_terrain_aabb_subtraction_creates_square_plan_hole():
    terrain = _flat_terrain()

    assert terrain.get_height_at(0.0, 0.0) == pytest.approx(100.0)
    assert terrain.subtract_aabb(
        [-16.0, 80.0, -16.0],
        [16.0, 120.0, 16.0],
    )

    # Inside the cutter footprint the heightfield is lowered to the cutter floor.
    assert terrain.get_height_at(0.0, 0.0) == pytest.approx(80.0)

    # Outside the square footprint the original surface remains untouched.
    assert terrain.get_height_at(32.0, 0.0) == pytest.approx(100.0)
    assert terrain.get_height_at(0.0, 32.0) == pytest.approx(100.0)

    # The authored cut is compact and survives serialisation.
    data = terrain.to_dict()
    assert data["csg_subtractions"] == [[-16.0, 80.0, -16.0, 16.0, 120.0, 16.0]]

    restored = _flat_terrain()
    restored.from_dict(data)
    assert restored.get_height_at(0.0, 0.0) == pytest.approx(80.0)
    assert restored.get_height_at(32.0, 0.0) == pytest.approx(100.0)


def test_terrain_csg_scalar_and_batch_sampling_match():
    terrain = _flat_terrain()
    terrain.subtract_aabb([-16.0, 80.0, -16.0], [16.0, 120.0, 16.0])

    world_x = np.asarray([-32.0, -16.0, 0.0, 16.0, 32.0], dtype=np.float32)
    world_z = np.asarray([0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float32)

    batch = terrain._get_raw_heights_batch(world_x, world_z)
    scalar = np.asarray(
        [terrain._get_raw_height_scalar(float(x), float(z))
         for x, z in zip(world_x, world_z)],
        dtype=np.float32,
    )

    np.testing.assert_allclose(batch, scalar, rtol=0.0, atol=1e-5)


def test_terrain_csg_requires_plain_aabb_brush():
    plain = {
        "pos": [0.0, 0.0, 0.0],
        "size": [32.0, 32.0, 32.0],
    }
    custom = {
        **plain,
        "geometry": {"planes": [{"n": [1.0, 0.0, 0.0], "d": 16.0}]},
    }

    assert is_plain_aabb_brush(plain)
    assert not is_plain_aabb_brush(custom)
