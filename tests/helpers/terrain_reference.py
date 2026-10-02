"""The terrain pipeline as it was before the TerrainTable -- frozen, for parity.

Everything here is a verbatim port of code that used to live in
:mod:`engine.terrain`: the CPU chunk mesh builder (``_generate_chunk_mesh`` and
``_get_colors_batch``), the pass-through vertex shader that drew it, and the
per-chunk residency / visibility / LOD / build-queue logic that walked a
``dict`` of ``TerrainChunk`` records every frame.

It is kept so the new pipeline can be proven against the *old behaviour*, not
against itself. Do not "fix" anything in this module: the downward flat
normal, the per-chunk colour normalisation, the ``iz``-only variation and the
LOD-dependent colours are the behaviour being preserved. A deliberate visual
change to the engine is made in the engine, and the parity test that pins it
is updated alongside, on purpose.

The one dependency on the live engine is the height source,
``terrain._get_heights_batch``: both pipelines are fed the same heights, which
is exactly the claim under test ("same inputs, same output").
"""

import math

import numpy as np

#: The terrain vertex shader the CPU mesh was drawn with: every attribute came
#: from the 14-float vertex, nothing was computed on the GPU.
OLD_TERRAIN_VERT = """#version 330 core
precision highp float;
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
layout (location = 2) in vec3 aColor;
layout (location = 3) in vec2 aTexCoord;
layout (location = 4) in vec3 aSmoothNormal;

out vec3 FragPos;
out mediump vec3 Normal;
out mediump vec3 VertexColor;
out vec2 TexCoords;
out mediump vec3 SmoothNormal;

uniform mat4 projection;
uniform mat4 view;

void main() {
    FragPos      = aPos;
    Normal       = aNormal;
    VertexColor  = aColor;
    TexCoords    = aTexCoord;
    SmoothNormal = aSmoothNormal;
    gl_Position  = projection * view * vec4(aPos, 1.0);
}"""

#: Floats per vertex in the old mesh: pos(3) normal(3) colour(3) uv(2) smooth(3).
FLOATS_PER_VERTEX = 14


# ---------------------------------------------------------------------------
# CPU mesh (was Terrain._generate_chunk_mesh / Terrain._get_colors_batch)
# ---------------------------------------------------------------------------

def reference_colors(biome, heights, normalized_heights):
    colors = biome.color_gradient
    if not colors:
        return np.full((len(heights), 3), 0.5, dtype=np.float32)
    h = np.clip(normalized_heights, 0.0, 1.0)
    result = np.zeros((len(h), 3), dtype=np.float32)
    t = np.zeros(len(h), dtype=np.float32)
    for i in range(len(colors) - 1):
        h0, c0 = colors[i]
        h1, c1 = colors[i + 1]
        mask = (h >= h0) & (h <= h1)
        if not np.any(mask): continue
        t[:] = 0.0
        if h1 > h0:
            t[mask] = (h[mask] - h0) / (h1 - h0)
        for j in range(3):
            result[mask, j] = c0[j] + t[mask] * (c1[j] - c0[j])
    last_h, last_c = colors[-1]
    above_mask = h > last_h
    if np.any(above_mask):
        result[above_mask] = last_c
    return result


def reference_chunk_mesh(terrain, world_x, world_z, size, resolution):
    """``(vertices (6*res*res, 14) float32, min_y, max_y)`` for one chunk."""
    step = size / resolution
    base_x = world_x
    base_z = world_z

    ix_vals = np.arange(resolution + 1, dtype=np.float32)
    iz_vals = np.arange(resolution + 1, dtype=np.float32)
    ix_grid, iz_grid = np.meshgrid(ix_vals, iz_vals, indexing='ij')

    wx = base_x + ix_grid * step
    wz = base_z + iz_grid * step

    wx_flat = wx.flatten().astype(np.float32)
    wz_flat = wz.flatten().astype(np.float32)

    heights_flat = terrain._get_heights_batch(wx_flat, wz_flat)
    heights = heights_flat.reshape((resolution + 1, resolution + 1))

    grad_x, grad_z = np.gradient(heights, step)
    sn_x = -grad_x
    sn_y = np.ones_like(grad_x)
    sn_z = -grad_z
    len_sn = np.sqrt(sn_x**2 + sn_y**2 + sn_z**2)
    sn_x /= len_sn
    sn_y /= len_sn
    sn_z /= len_sn
    smooth_normals = np.stack([sn_x, sn_y, sn_z], axis=-1)

    min_height = float(heights.min())
    max_height = float(heights.max())
    height_range = max_height - min_height if max_height > min_height else 1.0

    y00 = heights[:-1, :-1]
    y10 = heights[1:, :-1]
    y01 = heights[:-1, 1:]
    y11 = heights[1:, 1:]

    sn00 = smooth_normals[:-1, :-1]
    sn10 = smooth_normals[1:, :-1]
    sn01 = smooth_normals[:-1, 1:]
    sn11 = smooth_normals[1:, 1:]

    ix_q = np.arange(resolution, dtype=np.float32)
    iz_q = np.arange(resolution, dtype=np.float32)
    ix_qg, iz_qg = np.meshgrid(ix_q, iz_q, indexing='ij')

    x0_grid = base_x + ix_qg * step
    x1_grid = base_x + (ix_qg + 1) * step
    z0_grid = base_z + iz_qg * step
    z1_grid = base_z + (iz_qg + 1) * step

    num_quads = resolution * resolution

    t1_v0 = np.stack([x0_grid, y00, z0_grid], axis=-1).reshape(-1, 3)
    t1_v1 = np.stack([x1_grid, y10, z0_grid], axis=-1).reshape(-1, 3)
    t1_v2 = np.stack([x0_grid, y01, z1_grid], axis=-1).reshape(-1, 3)
    t1_sn0 = sn00.reshape(-1, 3)
    t1_sn1 = sn10.reshape(-1, 3)
    t1_sn2 = sn01.reshape(-1, 3)

    edge1_t1 = t1_v1 - t1_v0
    edge2_t1 = t1_v2 - t1_v0
    n1 = np.cross(edge1_t1, edge2_t1)
    n1_len = np.linalg.norm(n1, axis=1, keepdims=True)
    n1_len[n1_len == 0] = 1
    n1 = n1 / n1_len

    if terrain.flat_mode:
        colors1 = np.full((num_quads, 3), 0.7, dtype=np.float32)
    else:
        centroid_y1 = (y00 + y10 + y01).flatten() / 3.0
        norm_h1 = (centroid_y1 - min_height) / height_range
        colors1 = reference_colors(terrain.biome, centroid_y1, norm_h1)
        var_seed1 = (ix_qg.flatten() * 1000 + iz_qg.flatten()) % 100
        variation1 = (var_seed1 / 100.0 - 0.5) * 0.08
        colors1 = np.clip(colors1 + variation1[:, np.newaxis], 0, 1)

    t2_v0 = np.stack([x1_grid, y10, z0_grid], axis=-1).reshape(-1, 3)
    t2_v1 = np.stack([x1_grid, y11, z1_grid], axis=-1).reshape(-1, 3)
    t2_v2 = np.stack([x0_grid, y01, z1_grid], axis=-1).reshape(-1, 3)
    t2_sn0 = sn10.reshape(-1, 3)
    t2_sn1 = sn11.reshape(-1, 3)
    t2_sn2 = sn01.reshape(-1, 3)

    edge1_t2 = t2_v1 - t2_v0
    edge2_t2 = t2_v2 - t2_v0
    n2 = np.cross(edge1_t2, edge2_t2)
    n2_len = np.linalg.norm(n2, axis=1, keepdims=True)
    n2_len[n2_len == 0] = 1
    n2 = n2 / n2_len

    if terrain.flat_mode:
        colors2 = np.full((num_quads, 3), 0.7, dtype=np.float32)
    else:
        centroid_y2 = (y10 + y11 + y01).flatten() / 3.0
        norm_h2 = (centroid_y2 - min_height) / height_range
        colors2 = reference_colors(terrain.biome, centroid_y2, norm_h2)
        var_seed2 = ((ix_qg.flatten() + 1000) * 1000 + iz_qg.flatten() + 1000) % 100
        variation2 = (var_seed2 / 100.0 - 0.5) * 0.08
        colors2 = np.clip(colors2 + variation2[:, np.newaxis], 0, 1)

    ux0 = x0_grid.flatten() / terrain.TILING_SCALE
    uz0 = z0_grid.flatten() / terrain.TILING_SCALE
    ux1 = x1_grid.flatten() / terrain.TILING_SCALE
    uz1 = z1_grid.flatten() / terrain.TILING_SCALE

    vertices = np.zeros((num_quads * 6, 14), dtype=np.float32)
    vertices[0::6, 0:3] = t1_v0;  vertices[0::6, 3:6] = n1;  vertices[0::6, 6:9] = colors1;  vertices[0::6, 9:11] = np.stack([ux0, uz0], axis=1); vertices[0::6, 11:14] = t1_sn0
    vertices[1::6, 0:3] = t1_v1;  vertices[1::6, 3:6] = n1;  vertices[1::6, 6:9] = colors1;  vertices[1::6, 9:11] = np.stack([ux1, uz0], axis=1); vertices[1::6, 11:14] = t1_sn1
    vertices[2::6, 0:3] = t1_v2;  vertices[2::6, 3:6] = n1;  vertices[2::6, 6:9] = colors1;  vertices[2::6, 9:11] = np.stack([ux0, uz1], axis=1); vertices[2::6, 11:14] = t1_sn2
    vertices[3::6, 0:3] = t2_v0;  vertices[3::6, 3:6] = n2;  vertices[3::6, 6:9] = colors2;  vertices[3::6, 9:11] = np.stack([ux1, uz0], axis=1); vertices[3::6, 11:14] = t2_sn0
    vertices[4::6, 0:3] = t2_v1;  vertices[4::6, 3:6] = n2;  vertices[4::6, 6:9] = colors2;  vertices[4::6, 9:11] = np.stack([ux1, uz1], axis=1); vertices[4::6, 11:14] = t2_sn1
    vertices[5::6, 0:3] = t2_v2;  vertices[5::6, 3:6] = n2;  vertices[5::6, 6:9] = colors2;  vertices[5::6, 9:11] = np.stack([ux0, uz1], axis=1); vertices[5::6, 11:14] = t2_sn2

    return vertices, min_height, max_height


# ---------------------------------------------------------------------------
# Per-chunk bookkeeping (was the TerrainChunk dict walk in Terrain)
# ---------------------------------------------------------------------------

class RefChunk:
    """The fields of the old ``TerrainChunk`` dataclass the frame logic read."""

    def __init__(self, cx, cz, world_x, world_z, size):
        self.chunk_x = cx
        self.chunk_z = cz
        self.world_x = world_x
        self.world_z = world_z
        self.size = size
        self.vertex_count = 0
        self.min_y = 0.0
        self.max_y = 0.0
        self.center = (0.0, 0.0, 0.0)
        self.lod_level = 0
        self.target_lod = 0
        self.lod_stable_frames = 0
        self.is_dirty = True
        self.is_uploaded = False


class ReferenceChunkModel:
    """The old chunk residency / visibility / LOD / queue logic, frame by frame.

    Holds the same ``{(cx, cz): chunk}`` dict the old ``Terrain`` did and runs
    the same loops over it, so a test can drive it and a ``TerrainTable`` with
    the same camera path and compare every frame's decisions.
    """

    NEAR_DETAIL_RADIUS = 4096.0
    LOD_DISTANCES_SQ = [4608**2, 6144**2, 8192**2, 12288**2]
    LOD_RESOLUTIONS = [48, 32, 16, 8]
    LOD_HYSTERESIS_FRAMES = 10

    def __init__(self, chunk_size=256.0, offset_x=0.0, offset_z=0.0,
                 bounds=(-2, 2, -2, 2)):
        self.chunk_size = chunk_size
        self.offset_x = offset_x
        self.offset_z = offset_z
        self.min_chunk_x, self.max_chunk_x, self.min_chunk_z, self.max_chunk_z = bounds
        self.chunks = {}
        self.streaming = False
        self.stream_radius = 4096.0
        self.stream_evict_padding = 512.0

    def _ensure_chunk(self, cx, cz):
        key = (cx, cz)
        if key not in self.chunks:
            world_x = cx * self.chunk_size + self.offset_x
            world_z = cz * self.chunk_size + self.offset_z
            self.chunks[key] = RefChunk(cx, cz, world_x, world_z, self.chunk_size)
        return self.chunks[key]

    def remove_out_of_bounds(self):
        for key in [k for k, c in self.chunks.items()
                    if (c.chunk_x < self.min_chunk_x or c.chunk_x > self.max_chunk_x or
                        c.chunk_z < self.min_chunk_z or c.chunk_z > self.max_chunk_z)]:
            del self.chunks[key]

    def stream_chunks(self, cam_world_x, cam_world_z):
        cam_x = float(cam_world_x) - self.offset_x
        cam_z = float(cam_world_z) - self.offset_z
        cs = self.chunk_size
        radius = self.stream_radius
        keep = radius + self.stream_evict_padding
        r2 = radius * radius
        keep2 = keep * keep

        cam_cx = int(math.floor(cam_x / cs))
        cam_cz = int(math.floor(cam_z / cs))
        reach = int(math.ceil(radius / cs)) + 1
        lo_x = max(self.min_chunk_x, cam_cx - reach)
        hi_x = min(self.max_chunk_x, cam_cx + reach)
        lo_z = max(self.min_chunk_z, cam_cz - reach)
        hi_z = min(self.max_chunk_z, cam_cz + reach)

        def _nearest_dist_sq(cx, cz):
            chunk_min_x = cx * cs
            chunk_min_z = cz * cs
            nx = min(max(cam_x, chunk_min_x), chunk_min_x + cs)
            nz = min(max(cam_z, chunk_min_z), chunk_min_z + cs)
            dx = nx - cam_x
            dz = nz - cam_z
            return dx * dx + dz * dz

        for cx in range(lo_x, hi_x + 1):
            for cz in range(lo_z, hi_z + 1):
                if _nearest_dist_sq(cx, cz) <= r2:
                    self._ensure_chunk(cx, cz)

        to_evict = [key for key, chunk in self.chunks.items()
                    if _nearest_dist_sq(chunk.chunk_x, chunk.chunk_z) > keep2]
        for key in to_evict:
            del self.chunks[key]

    def ensure_bounds(self):
        for cz in range(self.min_chunk_z, self.max_chunk_z + 1):
            for cx in range(self.min_chunk_x, self.max_chunk_x + 1):
                self._ensure_chunk(cx, cz)

    def near_detail_radius(self):
        if self.streaming and self.stream_radius > 0.0:
            return min(self.NEAR_DETAIL_RADIUS, float(self.stream_radius))
        return self.NEAR_DETAIL_RADIUS

    def _get_lod_resolution(self, dist_sq):
        for i, threshold in enumerate(self.LOD_DISTANCES_SQ):
            if dist_sq < threshold:
                return self.LOD_RESOLUTIONS[i]
        return self.LOD_RESOLUTIONS[-1]

    @staticmethod
    def nearest_dist_sq(chunk, cam_x, cam_z):
        min_x = chunk.world_x
        max_x = min_x + chunk.size
        min_z = chunk.world_z
        max_z = min_z + chunk.size
        nx = min(max(float(cam_x), min_x), max_x)
        nz = min(max(float(cam_z), min_z), max_z)
        dx = nx - float(cam_x)
        dz = nz - float(cam_z)
        return dx * dx + dz * dz

    @staticmethod
    def is_visible(chunk, frustum_planes):
        if frustum_planes is None: return True
        half_size = chunk.size / 2
        if chunk.is_uploaded:
            cx, cy, cz = chunk.center
            half_y = (chunk.max_y - chunk.min_y) / 2 + 10
        else:
            cx = chunk.world_x + half_size
            cz = chunk.world_z + half_size
            cy = 0.0
            half_y = 1.0e6
        for plane in frustum_planes:
            a, b, c, d = plane
            px = cx + half_size if a >= 0 else cx - half_size
            py = cy + half_y if b >= 0 else cy - half_y
            pz = cz + half_size if c >= 0 else cz - half_size
            if a * px + b * py + c * pz + d < 0: return False
        return True

    def frame(self, cam_x, cam_z, frustum_planes):
        """One frame's decisions: ``(drawn keys, sorted build queue)``.

        *drawn* is what the old draw loop would have submitted, in order;
        the queue is ``[(key, resolution), ...]`` after the old sort.
        """
        near_detail_radius = self.near_detail_radius()
        chunks_to_update = []
        drawn = []
        for key, chunk in self.chunks.items():
            visible = self.is_visible(chunk, frustum_planes)
            dist_sq = self.nearest_dist_sq(chunk, cam_x, cam_z)
            prewarm_radius = near_detail_radius + chunk.size
            protected = dist_sq <= prewarm_radius * prewarm_radius
            target_resolution = (
                self.LOD_RESOLUTIONS[0]
                if protected
                else self._get_lod_resolution(dist_sq)
            )
            current_resolution = self.LOD_RESOLUTIONS[chunk.lod_level] if chunk.is_uploaded else 0
            needs_update = chunk.is_dirty or not chunk.is_uploaded
            if not needs_update and current_resolution != target_resolution:
                if protected:
                    needs_update = True
                    chunk.target_lod = target_resolution
                    chunk.lod_stable_frames = 0
                elif chunk.target_lod != target_resolution:
                    chunk.target_lod = target_resolution
                    chunk.lod_stable_frames = 0
                else:
                    chunk.lod_stable_frames += 1
                    if chunk.lod_stable_frames >= self.LOD_HYSTERESIS_FRAMES:
                        needs_update = True
                        chunk.lod_stable_frames = 0
            if needs_update:
                chunks_to_update.append((key, target_resolution, dist_sq, visible))
            if not visible:
                continue
            if chunk.vertex_count > 0:
                drawn.append(key)
        chunks_to_update.sort(
            key=lambda x: (not self.chunks[x[0]].is_dirty, not x[3], x[2]))
        return drawn, [(k, r) for k, r, _, _ in chunks_to_update]

    def build(self, key, resolution, heights):
        """Record what the old ``_upload_chunk`` left on the chunk."""
        chunk = self.chunks[key]
        min_height = float(heights.min())
        max_height = float(heights.max())
        chunk.min_y = min_height
        chunk.max_y = max_height
        chunk.center = (chunk.world_x + chunk.size / 2, (min_height + max_height) / 2,
                        chunk.world_z + chunk.size / 2)
        chunk.vertex_count = resolution * resolution * 6
        chunk.is_dirty = False
        chunk.is_uploaded = True
        chunk.lod_level = (self.LOD_RESOLUTIONS.index(resolution)
                           if resolution in self.LOD_RESOLUTIONS else 0)


# ---------------------------------------------------------------------------
# Deliberate change: seamless smooth normals
# ---------------------------------------------------------------------------
#
# The one intentional departure from the frozen mesh above. The old builder
# took np.gradient's one-sided differences at a chunk's edge, which kinked the
# smooth normal by a few degrees at every chunk border -- a seam in the
# texture blend, the only consumer of the smooth normal. The engine now stores
# a one-sample border around each chunk and takes central differences
# everywhere. Every other attribute is still the frozen behaviour.

def seamless_smooth_normals(terrain, world_x, world_z, size, resolution):
    """Per-vertex smooth normals as the engine now computes them.

    Returns an array aligned with :func:`reference_chunk_mesh`'s vertices.
    """
    step = size / resolution
    g = np.arange(-1, resolution + 2, dtype=np.float32)
    ix, iz = np.meshgrid(g, g, indexing='ij')
    wx = (world_x + ix * step).flatten().astype(np.float32)
    wz = (world_z + iz * step).flatten().astype(np.float32)
    h = terrain._get_heights_batch(wx, wz).reshape(resolution + 3, resolution + 3)
    span = np.float32(2 * step)
    gx = (h[2:, 1:-1] - h[:-2, 1:-1]) / span          # (res+1)^2, grid (i, k)
    gz = (h[1:-1, 2:] - h[1:-1, :-2]) / span
    length = np.sqrt(gx ** 2 + np.float32(1) + gz ** 2)
    sn = np.stack([-gx / length, np.float32(1) / length, -gz / length], axis=-1)
    q = np.arange(resolution * resolution)
    i, k = q // resolution, q % resolution
    out = np.zeros((resolution * resolution * 6, 3), dtype=np.float32)
    corners = ((0, 0), (1, 0), (0, 1), (1, 0), (1, 1), (0, 1))
    for c, (di, dk) in enumerate(corners):
        out[c::6] = sn[i + di, k + dk]
    return out


def reference_chunk_mesh_seamless(terrain, world_x, world_z, size, resolution):
    """The frozen mesh with only its smooth normals replaced (see above)."""
    vertices, lo, hi = reference_chunk_mesh(terrain, world_x, world_z, size, resolution)
    vertices = vertices.copy()
    vertices[:, 11:14] = seamless_smooth_normals(terrain, world_x, world_z, size, resolution)
    return vertices, lo, hi
