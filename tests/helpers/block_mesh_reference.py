"""Frozen CANARY block-mesh construction, for exact output/callback parity.

Retained independently of production helpers so allocation changes must keep
vertex ordering, float32 rounding, UVs, face colours, and callback sequencing.
"""

from typing import Callable, Optional, Tuple

import numpy as np

def reference_build_block_mesh(world_x: float, world_z: float, size: float, cell: float,
                     origin_x: float, origin_z: float,
                     heights_fn: Callable[[np.ndarray, np.ndarray], np.ndarray],
                     colors_fn: Callable[[np.ndarray], np.ndarray],
                     bounds: Tuple[float, float, float, float],
                     floor_height: Optional[float],
                     tiling: float, max_cells: int = 128):
    """Vertex data (N, 14) for a chunk of square land columns.

    ``heights_fn`` gives the (already stepped) height of a block from its
    centre. A block belongs to the chunk that holds its centre, so blocks on
    a chunk seam are built exactly once. A wall is emitted on every side
    where the neighbouring column is lower; beyond the terrain ``bounds``
    the neighbour is ``floor_height`` (a solid skirt), or no wall at all when
    that is ``None``.

    Layout per vertex: position(3) normal(3) colour(3) uv(2) smooth normal(3).
    Returns ``(vertices, min_y, max_y)``.
    """
    cells_across = size / cell
    if cells_across > max_cells:
        cell = size / max_cells
    k0x = int(np.ceil((world_x - origin_x) / cell - 0.5))
    k1x = int(np.ceil((world_x + size - origin_x) / cell - 0.5))
    k0z = int(np.ceil((world_z - origin_z) / cell - 0.5))
    k1z = int(np.ceil((world_z + size - origin_z) / cell - 0.5))
    nx, nz = k1x - k0x, k1z - k0z
    if nx <= 0 or nz <= 0:
        return np.zeros((0, 14), dtype=np.float32), 0.0, 0.0

    # Heights on a grid padded by one block for the neighbour tests.
    kx = np.arange(k0x - 1, k1x + 1)
    kz = np.arange(k0z - 1, k1z + 1)
    kxg, kzg = np.meshgrid(kx, kz, indexing='ij')
    cx = origin_x + (kxg + 0.5) * cell
    cz = origin_z + (kzg + 0.5) * cell
    heights = np.asarray(heights_fn(cx.ravel().astype(np.float32),
                                    cz.ravel().astype(np.float32)),
                         dtype=np.float64).reshape(cx.shape)
    min_x, max_x, min_z, max_z = bounds
    outside = (cx < min_x) | (cx > max_x) | (cz < min_z) | (cz > max_z)
    if floor_height is None:
        # No skirt: an outside neighbour never makes a wall.
        heights = np.where(outside, np.inf, heights)
    else:
        heights = np.where(outside, min(floor_height, np.nanmin(heights)), heights)

    inner = (slice(1, -1), slice(1, -1))
    h = heights[inner]
    x0 = origin_x + kxg[inner] * cell
    z0 = origin_z + kzg[inner] * cell
    x1 = x0 + cell
    z1 = z0 + cell
    inside = ~outside[inner]

    parts = []

    # Tops.
    hx0, hz0, hx1, hz1, hy = (a[inside] for a in (x0, z0, x1, z1, h))
    if hy.size:
        cols = colors_fn(hy.astype(np.float32))
        up = np.array([0.0, 1.0, 0.0])
        corners = [(hx0, hz0), (hx1, hz0), (hx0, hz1),
                   (hx1, hz0), (hx1, hz1), (hx0, hz1)]
        parts.append(_quad_vertices(
            [np.stack([a, hy, b], -1) for a, b in corners], up, cols,
            [np.stack([a / tiling, b / tiling], -1) for a, b in corners]))

    # Walls. (neighbour slice, outward normal, wall edge as two xz points)
    sides = [
        ((slice(2, None), slice(1, -1)), (1.0, 0.0, 0.0), (x1, z0), (x1, z1)),
        ((slice(0, -2), slice(1, -1)), (-1.0, 0.0, 0.0), (x0, z1), (x0, z0)),
        ((slice(1, -1), slice(2, None)), (0.0, 0.0, 1.0), (x1, z1), (x0, z1)),
        ((slice(1, -1), slice(0, -2)), (0.0, 0.0, -1.0), (x0, z0), (x1, z0)),
    ]
    for nb_slice, normal, (ax, az), (bx, bz) in sides:
        nb = heights[nb_slice]
        mask = inside & np.isfinite(nb) & (nb < h - 1e-6)
        if not np.any(mask):
            continue
        top = h[mask]
        bottom = nb[mask]
        pax, paz, pbx, pbz = ax[mask], az[mask], bx[mask], bz[mask]
        cols = colors_fn(top.astype(np.float32))
        along_a = (pax + paz) / tiling
        along_b = (pbx + pbz) / tiling
        corners = [(pax, bottom, paz, along_a), (pbx, bottom, pbz, along_b),
                   (pax, top, paz, along_a), (pbx, bottom, pbz, along_b),
                   (pbx, top, pbz, along_b), (pax, top, paz, along_a)]
        parts.append(_quad_vertices(
            [np.stack([cx_, cy_, cz_], -1) for cx_, cy_, cz_, _ in corners],
            np.array(normal), cols,
            [np.stack([u, cy_ / tiling], -1) for _, cy_, _, u in corners]))

    if not parts:
        return np.zeros((0, 14), dtype=np.float32), 0.0, 0.0
    verts = np.concatenate(parts, axis=0)
    ys = verts[:, 1]
    return verts, float(ys.min()), float(ys.max())


def _quad_vertices(positions, normal, colors, uvs) -> np.ndarray:
    """Interleave six-vertex quads: positions/uvs are lists of (n, k) arrays."""
    n = positions[0].shape[0]
    out = np.zeros((n, 6, 14), dtype=np.float32)
    for i in range(6):
        out[:, i, 0:3] = positions[i]
        out[:, i, 3:6] = normal
        out[:, i, 6:9] = colors
        out[:, i, 9:11] = uvs[i]
        out[:, i, 11:14] = normal
    return out.reshape(-1, 14)
