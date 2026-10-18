"""Pin the block mesh to the original builder, including callback behaviour."""

import numpy as np
import pytest

from engine.terrain_style import build_block_mesh
from tests.helpers.block_mesh_reference import reference_build_block_mesh


def _height_values(pattern, x, z):
    if pattern == 'flat':
        return np.full_like(x, 23.125)
    if pattern == 'steps':
        return np.floor(x / 11.25) * 7.125 + np.floor(z / 19.5) * 2.75
    if pattern == 'checker':
        return ((np.floor(x / 8) + np.floor(z / 8)) % 2) * 31.25 - 12.5
    return np.sin(x / 7) * 3 + np.cos(z / 11) * 13


def _build(builder, pattern='steps', *, color_shape='rows', dtype=np.float64,
           mutate_source=False, **overrides):
    calls = []
    height_source = None
    color_buffer = np.empty((128 * 128, 3), dtype=dtype)

    def heights(x, z):
        nonlocal height_source
        calls.append(('heights', x.copy(), z.copy(), x.dtype, z.dtype))
        height_source = _height_values(pattern, x, z).astype(dtype)
        return height_source

    def colors(h):
        calls.append(('colors', h.copy(), h.dtype, h.flags.c_contiguous))
        if mutate_source:
            # The original builder computes each later side after its previous
            # colour callback. Keep that ordering even for aliased height data.
            height_source[:] += 1.125
        color_buffer[:len(h)] = np.column_stack((h / 127, h / 63, -h / 31))
        if color_shape == 'rows':
            # Reuse the same storage each call: emitted faces must own colours.
            return color_buffer[:len(h)]
        if color_shape == 'column':
            return color_buffer[:len(h), :1]
        if color_shape == 'list':
            return color_buffer[:len(h)].tolist()
        if color_shape == 'constant':
            return [0.1, 0.7, -0.0]
        return 0.25

    kwargs = dict(world_x=-19.625, world_z=8.375, size=67.25, cell=8.0,
                  origin_x=1.375, origin_z=-6.625, bounds=(-9, 74, -24, 71),
                  floor_height=None, tiling=13.7, max_cells=128)
    kwargs.update(overrides)
    return builder(heights_fn=heights, colors_fn=colors, **kwargs), calls


def _assert_same(old, new):
    (expected, *expected_bounds), old_calls = old
    (actual, *actual_bounds), new_calls = new
    assert actual.dtype == expected.dtype == np.float32
    assert actual.flags.c_contiguous
    # Byte parity covers float32 rounding and the sign of zero in every field.
    assert actual.shape == expected.shape
    assert actual.tobytes() == expected.tobytes()
    np.testing.assert_array_equal(actual_bounds, expected_bounds)
    assert len(new_calls) == len(old_calls)
    for actual_call, expected_call in zip(new_calls, old_calls):
        assert actual_call[0] == expected_call[0]
        for actual_arg, expected_arg in zip(actual_call[1:], expected_call[1:]):
            if isinstance(actual_arg, np.ndarray):
                assert actual_arg.dtype == expected_arg.dtype
                assert actual_arg.tobytes() == expected_arg.tobytes()
            else:
                assert actual_arg == expected_arg


@pytest.mark.parametrize('pattern', ['flat', 'steps', 'checker', 'smooth'])
@pytest.mark.parametrize('floor', [None, -73.125, 1000.0])
@pytest.mark.parametrize('dtype', [np.float32, np.float64])
def test_exact_block_vertices_bounds_and_callback_order(pattern, floor, dtype):
    kwargs = dict(pattern=pattern, floor_height=floor, dtype=dtype)
    _assert_same(_build(reference_build_block_mesh, **kwargs),
                 _build(build_block_mesh, **kwargs))


@pytest.mark.parametrize('color_shape', ['rows', 'column', 'list', 'constant', 'scalar'])
@pytest.mark.parametrize('mutate_source', [False, True])
def test_color_broadcasting_reused_buffers_and_mutating_callbacks(color_shape, mutate_source):
    kwargs = dict(color_shape=color_shape, mutate_source=mutate_source)
    _assert_same(_build(reference_build_block_mesh, **kwargs),
                 _build(build_block_mesh, **kwargs))


@pytest.mark.parametrize('overrides', [
    dict(world_x=17.3, world_z=-42.9, size=31.7, cell=9.125,
         origin_x=-83.35, origin_z=41.8),
    dict(world_x=100_000_000.5, world_z=-100_000_000.25,
         origin_x=100_000_000.125, origin_z=-100_000_000.875,
         bounds=(-1e12, 1e12, -1e12, 1e12), tiling=0.125),
    dict(world_x=2048, world_z=2048, bounds=(-128, 128, -128, 128)),
    dict(size=0),
    dict(size=0.25, cell=32),
    dict(size=133, cell=1, max_cells=7),
    dict(tiling=-7.75),
])
def test_boundaries_empty_chunks_capped_cells_and_float_precision(overrides):
    _assert_same(_build(reference_build_block_mesh, **overrides),
                 _build(build_block_mesh, **overrides))


def test_nonaligned_chunk_seams_preserve_exact_face_ownership():
    for world_x in (-71.375, -4.125, 63.125):
        for world_z in (-58.875, 8.375, 75.625):
            kwargs = dict(pattern='checker', world_x=world_x, world_z=world_z,
                          bounds=(-1000, 1000, -1000, 1000))
            _assert_same(_build(reference_build_block_mesh, **kwargs),
                         _build(build_block_mesh, **kwargs))
