"""
engine/glb_loader.py – GLB/glTF 2.0 loader for the renderer

Provides:
    • GLBLoader – parses GLB binary format and extracts mesh data
    • GLB – OpenGL-ready GLB model (mirrors OBJ interface)
    • Support for embedded textures, PBR materials, and node hierarchies

The GLB class provides the same interface as OBJ for renderer compatibility:
    - is_loaded
    - vao, vbo
    - vertex_count
    - groups (for material-based rendering)
    - materials
    - cpu_vertices (for 2D view projection)
"""

import struct
import json
import os
import math
import numpy as np
import OpenGL.GL as gl
from engine.renderer.core import gl_state
from typing import List, Tuple, Optional, Dict, Any


# Models arrive inside shared maps and .fiopak packages, so their sizes and
# indices are untrusted. A .glb (or an external .bin it names) larger than
# this is refused rather than read whole into memory.
MAX_GLB_FILE_BYTES = 256 * 1024 * 1024
#: Most bytes of accessor data one model may decode to (see load_bytes).
MAX_DECODED_BYTES = 1024 * 1024 * 1024


def _non_negative_int(value) -> Optional[int]:
    """*value* as an int when it is a non-negative JSON integer, else None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _finite_float(value, default: float) -> float:
    """*value* as a finite float when it is a JSON number, else *default*."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = float(value)
    return value if math.isfinite(value) else default


def _finite_floats(value, default: List[float]) -> List[float]:
    """A material factor as ``len(default)`` finite floats.

    A factor of the wrong type or length (``[1]``, ``"red"``, ``["r", ...]``)
    would load and then raise from ``float(colour[1])`` on every frame, so a
    bad component falls back to its default, as the MTL path's ``Kd`` does.
    """
    if not isinstance(value, (list, tuple)):
        return list(default)
    return [_finite_float(value[i] if i < len(value) else None, d)
            for i, d in enumerate(default)]


def flat_shaded(positions, indices=None, uvs=None):
    """A triangle primitive without normals, made flat-shaded.

    glTF 2.0 (3.7.2.1): "When normals are not specified, client
    implementations MUST calculate flat normals." Each triangle gets its own
    three corners carrying its face normal, so a box stays a box rather than
    being smoothed into a ball. Returns ``(positions, normals, uvs, indices)``
    for the unshared corners (*uvs* None when none were given), in the order
    of the triangles. The Android player does the same
    (player/render/models.py).

    A model with no normals used to be drawn with every vertex facing up
    (0, 1, 0): lit as a flat colour whatever way a face pointed, and -- a
    spinning reel turning that "up" with it -- pulsing dark and light once
    a turn.
    """
    positions = np.asarray(positions, dtype=np.float32)[:, :3]
    tri = (np.arange(len(positions), dtype=np.int64) if indices is None
           else np.asarray(indices, dtype=np.int64).reshape(-1))
    tri = tri[:len(tri) - len(tri) % 3]
    out_positions = positions[tri]
    out_uvs = None
    if uvs is not None and len(uvs) >= len(positions):
        out_uvs = np.asarray(uvs, dtype=np.float32)[tri]
    return (out_positions, flat_normals(out_positions), out_uvs,
            np.arange(len(out_positions), dtype=np.uint32))


def flat_normals(corners):
    """Per-vertex normals for unshared triangle corners (``(3T [+ r], 3)``):
    each corner its triangle's face normal. A degenerate triangle, and any
    trailing vertices short of a whole one, keep the default (0, 1, 0)."""
    corners = np.asarray(corners, dtype=np.float32)[:, :3]
    normals = np.zeros((len(corners), 3), dtype=np.float32)
    normals[:, 1] = 1.0
    whole = len(corners) - len(corners) % 3
    tris = corners[:whole].reshape(-1, 3, 3).astype(np.float64)
    face = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    length = np.linalg.norm(face, axis=1, keepdims=True)
    face = np.where(length > 1e-12, face / np.maximum(length, 1e-30),
                    np.array((0.0, 1.0, 0.0)))
    normals[:whole] = np.repeat(face, 3, axis=0)
    return normals


def _read_bounded(path: str) -> Optional[bytes]:
    """A regular file's bytes, or None if missing, special, or too large.

    Requiring a regular file keeps a crafted path such as ``/dev/zero`` from
    being read forever.
    """
    try:
        if not os.path.isfile(path):
            # An archived asset (assets/<category>.fiopak), read in place.
            from engine.asset_archives import asset_bytes
            data = asset_bytes(path)
            return data if data is not None and len(data) <= MAX_GLB_FILE_BYTES else None
        if os.path.getsize(path) > MAX_GLB_FILE_BYTES:
            return None
        with open(path, 'rb') as f:
            data = f.read(MAX_GLB_FILE_BYTES + 1)
    except OSError:
        return None
    return data if len(data) <= MAX_GLB_FILE_BYTES else None


# ---------------------------------------------------------------------------
# GLB Binary Format Parser
# ---------------------------------------------------------------------------

class GLBChunkType:
    JSON = 0x4E4F534A  # "JSON"
    BIN = 0x004E4942   # "BIN\0"


class GLBHeader:
    """12-byte GLB header parser."""
    __slots__ = ('magic', 'version', 'length')

    def __init__(self, data: bytes):
        if len(data) < 12:
            raise ValueError("GLB header too short (need 12 bytes)")
        self.magic, self.version, self.length = struct.unpack('<III', data[:12])
        if self.magic != 0x46546C67:  # "glTF"
            raise ValueError(f"Invalid GLB magic: 0x{self.magic:08X} (expected 0x46546C67)")


class GLBChunk:
    """Single GLB chunk: length, type, data."""
    __slots__ = ('length', 'chunk_type', 'data')

    def __init__(self, data: bytes, offset: int):
        if offset + 8 > len(data):
            raise ValueError("Chunk header too short")
        self.length, self.chunk_type = struct.unpack('<II', data[offset:offset+8])
        end = offset + 8 + self.length
        if end > len(data):
            raise ValueError(f"Chunk data extends beyond file ({end} > {len(data)})")
        self.data = data[offset+8:end]


class GLBLoader:
    """
    Loads GLB binary files and extracts:
      - JSON scene description
      - Binary buffer data
      - Mesh vertices, normals, UVs, indices
      - Materials (PBR base color, metallic, roughness, normal maps, emissive)
      - Node transforms
    """

    def __init__(self):
        self.json_data: Optional[Dict[str, Any]] = None
        self.binary_blob: Optional[bytes] = None
        self.meshes: List[Dict[str, Any]] = []
        self.materials: Dict[str, Dict[str, Any]] = {}
        self.nodes: List[Dict[str, Any]] = []
        self.scenes: List[Dict[str, Any]] = []
        self.default_scene: int = 0
        self.buffers: List[bytes] = []
        self.buffer_views: List[Dict[str, Any]] = []
        self.accessors: List[Dict[str, Any]] = []
        self.images: List[Dict[str, Any]] = []
        self.textures: List[Dict[str, Any]] = []
        self.samplers: List[Dict[str, Any]] = []
        self.skin: List[Dict[str, Any]] = []
        self.animations: List[Dict[str, Any]] = []

    def load(self, filepath: str) -> bool:
        """Load a GLB file from disk; False if it cannot be read or parsed."""
        data = self._read_file(filepath)
        if data is None:
            print(f"[GLBLoader] Failed to load: {filepath}")
            return False

        return self.load_bytes(data, filepath)

    def load_bytes(self, data: bytes, source="", read_asset=None) -> bool:
        """Parse a streamed model; external buffers use the same asset source."""
        if len(data) > MAX_GLB_FILE_BYTES:
            return False
        self._filepath_hint = source
        self._asset_reader = read_asset
        # Every accessor read is charged against one budget: primitives that
        # all name the same accessor would otherwise each decode a fresh copy,
        # turning a 1 MiB file into gigabytes. A real model decodes to about
        # its own size (at most 4x, for byte data widened to int32).
        self._decode_budget = min(MAX_DECODED_BYTES,
                                  max(64 * 1024 * 1024, 4 * len(data)))
        try:
            if str(source).lower().endswith('.gltf'):
                # A .gltf is the same JSON without the binary container; its
                # buffers are external files or data URIs.
                self.json_data = json.loads(data)
            else:
                self._parse_glb(data)
            if not isinstance(self.json_data, dict):
                raise ValueError("the glTF document is not a JSON object")
            self._parse_json_structure()
            return True
        except Exception as e:
            print(f"[GLBLoader] Parse error: {e}")
            return False

    def _read_file(self, filepath: str) -> Optional[bytes]:
        """The file's bytes, or None when it is missing, unreadable or too large."""
        return _read_bounded(filepath)

    def _parse_glb(self, data: bytes):
        """Parse GLB header and chunks."""
        header = GLBHeader(data)
        offset = 12

        # First chunk MUST be JSON
        json_chunk = GLBChunk(data, offset)
        if json_chunk.chunk_type != GLBChunkType.JSON:
            raise ValueError(f"First chunk must be JSON, got 0x{json_chunk.chunk_type:08X}")
        self.json_data = json.loads(json_chunk.data.decode('utf-8'))
        offset += 8 + json_chunk.length
        # Align to 4-byte boundary
        if json_chunk.length % 4 != 0:
            offset += 4 - (json_chunk.length % 4)

        # Second chunk MAY be BIN
        if offset < header.length:
            bin_chunk = GLBChunk(data, offset)
            if bin_chunk.chunk_type == GLBChunkType.BIN:
                self.binary_blob = bin_chunk.data

    def _parse_json_structure(self):
        """Extract all glTF structures from JSON."""
        if not self.json_data:
            return

        self.asset = self.json_data.get('asset', {})
        self.default_scene = self.json_data.get('scene', 0)
        self.scenes = self.json_data.get('scenes', [])
        self.nodes = self.json_data.get('nodes', [])
        self.meshes_json = self.json_data.get('meshes', [])
        self.materials_json = self.json_data.get('materials', [])
        self.buffers_json = self.json_data.get('buffers', [])
        self.buffer_views = self.json_data.get('bufferViews', [])
        self.accessors = self.json_data.get('accessors', [])
        self.images = self.json_data.get('images', [])
        self.textures = self.json_data.get('textures', [])
        self.samplers = self.json_data.get('samplers', [])
        self.skins = self.json_data.get('skins', [])
        self.animations = self.json_data.get('animations', [])

        # Resolve buffer data
        self._resolve_buffers()

        # Parse materials
        self._parse_materials()

        # Parse meshes with resolved accessors
        self._parse_meshes()

    def _resolve_buffers(self):
        """Resolve buffer data from binary blob or external URIs."""
        self.buffers = []
        for i, buf in enumerate(self.buffers_json):
            if not isinstance(buf, dict):
                buf = {}
            uri = buf.get('uri', '')
            if not isinstance(uri, str):
                uri = ''
            byte_length = _non_negative_int(buf.get('byteLength', 0)) or 0

            if not uri and i == 0 and self.binary_blob is not None:
                # First buffer with no URI → GLB binary chunk
                self.buffers.append(self.binary_blob[:byte_length])
            elif uri.startswith('data:application/octet-stream;base64,'):
                import base64
                b64 = uri.split(',', 1)[1]
                self.buffers.append(base64.b64decode(b64))
            elif uri.startswith('data:image/'):
                # Skip image data URIs for buffer resolution
                self.buffers.append(b'')
            elif not uri or os.path.isabs(uri) or ':' in uri:
                # glTF buffer URIs are relative references; an absolute path,
                # a drive letter or another scheme is not resolved.
                self.buffers.append(b'')
            else:
                # External file reference, relative to the model and confined
                # to the model's assets folder: no '..' escape, UNC or drive.
                from engine.asset_paths import companion_path, warn_refused
                ext_path = companion_path(self._filepath_hint or '', uri)
                if ext_path is None:
                    warn_refused(uri, 'model buffer')
                    self.buffers.append(b'')
                    continue
                reader = getattr(self, "_asset_reader", None)
                self.buffers.append((reader(ext_path) if reader else _read_bounded(ext_path)) or b'')
        # The budget was set from the file's own size (load_bytes). A .gltf's
        # data is all in buffers like these, so a real model's budget scales
        # with them the same way.
        budget = getattr(self, '_decode_budget', None)
        if budget is not None:
            self._decode_budget = min(MAX_DECODED_BYTES, max(
                budget, 4 * sum(len(b) for b in self.buffers)))

    def _parse_materials(self):
        """Extract material properties (PBR, textures, colors).

        Factors are model data the renderer reads every frame: each one is
        coerced to finite floats of the right length here, so a malformed
        material draws with defaults instead of loading and then faulting
        every paintGL.
        """
        self._material_names = []
        materials = self.materials_json if isinstance(self.materials_json, list) else []
        for i, mat in enumerate(materials):
            if not isinstance(mat, dict):
                mat = {}
            name = mat.get('name', f'material_{i}')
            if not isinstance(name, str):
                name = f'material_{i}'
            self._material_names.append(name)
            parsed = {
                'name': name,
                'color': [0.8, 0.8, 0.8],
                'metallic': 0.0,
                'roughness': 0.5,
                'emissive': [0.0, 0.0, 0.0],
                'alpha': 1.0,
                'double_sided': mat.get('doubleSided', False) is True,
                'texture': None,
                'normal_texture': None,
                'metallic_roughness_texture': None,
                'emissive_texture': None,
                'occlusion_texture': None,
            }

            def _tex(key, source=mat):
                info = source.get(key)
                return self._resolve_texture(info.get('index')) if isinstance(info, dict) and info else None

            # PBR metallic-roughness
            pbr = mat.get('pbrMetallicRoughness', {})
            if isinstance(pbr, dict) and pbr:
                base_color = _finite_floats(pbr.get('baseColorFactor', [1.0, 1.0, 1.0, 1.0]),
                                            [1.0, 1.0, 1.0, 1.0])
                parsed['color'] = base_color[:3]
                parsed['alpha'] = max(0.0, min(1.0, base_color[3]))
                parsed['metallic'] = _finite_float(pbr.get('metallicFactor', 0.0), 0.0)
                parsed['roughness'] = _finite_float(pbr.get('roughnessFactor', 0.5), 0.5)

                # Base color texture
                parsed['texture'] = _tex('baseColorTexture', pbr)

                # Metallic-roughness texture
                parsed['metallic_roughness_texture'] = _tex('metallicRoughnessTexture', pbr)

            # Normal map
            parsed['normal_texture'] = _tex('normalTexture')

            # Emissive
            parsed['emissive'] = _finite_floats(mat.get('emissiveFactor', [0.0, 0.0, 0.0]),
                                                [0.0, 0.0, 0.0])
            parsed['emissive_texture'] = _tex('emissiveTexture')

            # Occlusion
            parsed['occlusion_texture'] = _tex('occlusionTexture')

            self.materials[name] = parsed

    def _resolve_texture(self, texture_index: Optional[int]) -> Optional[str]:
        """Resolve texture index to image source path or data URI."""
        texture_index = _non_negative_int(texture_index)
        if texture_index is None or texture_index >= len(self.textures):
            return None
        tex = self.textures[texture_index]
        source_idx = _non_negative_int(tex.get('source')) if isinstance(tex, dict) else None
        if source_idx is None or source_idx >= len(self.images):
            return None
        img = self.images[source_idx]
        if not isinstance(img, dict):
            return None
        uri = img.get('uri')
        if uri and isinstance(uri, str):
            return uri
        # Check for bufferView-based image (embedded in GLB)
        bv_idx = _non_negative_int(img.get('bufferView'))
        if bv_idx is not None and bv_idx < len(self.buffer_views):
            # Return a marker that the renderer can use to extract from buffer
            return f"__glb_embedded__{bv_idx}__{img.get('mimeType', 'image/png')}"
        return None

    def _parse_meshes(self):
        """Parse all meshes and their primitives."""
        for mesh_idx, mesh in enumerate(self.meshes_json):
            mesh_data = {
                'name': mesh.get('name', f'mesh_{mesh_idx}'),
                'primitives': []
            }
            for prim in mesh.get('primitives', []):
                prim_data = self._parse_primitive(prim)
                if prim_data:
                    mesh_data['primitives'].append(prim_data)
            self.meshes.append(mesh_data)

    def _parse_primitive(self, prim: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Parse a single mesh primitive into vertex data."""
        attrs = prim.get('attributes', {})

        # Required: POSITION
        pos_acc_idx = attrs.get('POSITION')
        if pos_acc_idx is None:
            return None

        positions = self._read_accessor(pos_acc_idx)
        if positions is None or len(positions) == 0 or positions.shape[1] != 3:
            return None

        # Optional attributes
        normals = self._read_accessor(attrs.get('NORMAL'))
        uvs = self._read_accessor(attrs.get('TEXCOORD_0'))
        colors = self._read_accessor(attrs.get('COLOR_0'))
        tangents = self._read_accessor(attrs.get('TANGENT'))
        joints = self._read_accessor(attrs.get('JOINTS_0'))
        weights = self._read_accessor(attrs.get('WEIGHTS_0'))

        # Indices
        indices = None
        if 'indices' in prim:
            indices = self._read_accessor(prim['indices'], is_index=True)
            # An index past the vertex data would have the GPU read outside
            # the vertex buffer; such a primitive is dropped, not drawn.
            if indices is not None and len(indices) and (
                    int(indices.min()) < 0 or int(indices.max()) >= len(positions)):
                print("[GLBLoader] Skipping primitive with out-of-range indices")
                return None

        # Material
        # The name _parse_materials gave that material (its own, when usable).
        mat_idx = _non_negative_int(prim.get('material'))
        names = getattr(self, '_material_names', ())
        material_name = names[mat_idx] if mat_idx is not None and mat_idx < len(names) else None

        # Mode: 0=POINTS, 1=LINES, 2=LINE_LOOP, 3=LINE_STRIP, 4=TRIANGLES, 5=TRIANGLE_STRIP, 6=TRIANGLE_FAN
        mode = prim.get('mode', 4)

        return {
            'positions': positions,
            'normals': normals,
            'uvs': uvs,
            'colors': colors,
            'tangents': tangents,
            'joints': joints,
            'weights': weights,
            'indices': indices,
            'material': material_name,
            'mode': mode,
        }

    # componentType -> little-endian dtype
    _COMPONENT_DTYPES = {
        5120: np.dtype('<i1'),   # BYTE
        5121: np.dtype('<u1'),   # UNSIGNED_BYTE
        5122: np.dtype('<i2'),   # SHORT
        5123: np.dtype('<u2'),   # UNSIGNED_SHORT
        5125: np.dtype('<u4'),   # UNSIGNED_INT
        5126: np.dtype('<f4'),   # FLOAT
    }

    _TYPE_COMPONENTS = {
        'SCALAR': 1,
        'VEC2': 2,
        'VEC3': 3,
        'VEC4': 4,
        'MAT2': 4,
        'MAT3': 9,
        'MAT4': 16,
    }

    def _read_accessor(self, accessor_idx: Optional[int], is_index: bool = False) -> Optional[np.ndarray]:
        """Read an accessor as an ``(count, components)`` array.

        Float accessors come back as float32, integer ones as int32. Index
        accessors are flattened to 1-D. Elements that would run past the end
        of their buffer view or buffer are dropped, as is an accessor with an
        invalid offset, view length or stride.
        """
        accessor_idx = _non_negative_int(accessor_idx)
        if accessor_idx is None or accessor_idx >= len(self.accessors):
            return None

        acc = self.accessors[accessor_idx]
        bv_idx = _non_negative_int(acc.get('bufferView'))
        if bv_idx is None or bv_idx >= len(self.buffer_views):
            return None

        bv = self.buffer_views[bv_idx]
        buf_idx = _non_negative_int(bv.get('buffer', 0))
        if buf_idx is None or buf_idx >= len(self.buffers):
            return None

        buf = self.buffers[buf_idx]
        bv_offset = _non_negative_int(bv.get('byteOffset', 0))
        bv_length = _non_negative_int(bv.get('byteLength'))
        acc_offset = _non_negative_int(acc.get('byteOffset', 0))
        count = _non_negative_int(acc.get('count', 0))
        if bv_offset is None or bv_length is None or acc_offset is None or count is None:
            return None
        byte_offset = bv_offset + acc_offset

        dtype = self._COMPONENT_DTYPES.get(acc.get('componentType', 5126),
                                           self._COMPONENT_DTYPES[5126])
        num_comps = self._TYPE_COMPONENTS.get(acc.get('type', 'SCALAR'), 1)
        elem_size = num_comps * dtype.itemsize

        stride = _non_negative_int(bv.get('byteStride', 0))
        if stride is None:
            return None
        if stride == 0:
            stride = elem_size
        elif stride < elem_size:
            # Overlapping elements are invalid glTF and can amplify a bounded
            # file into a much larger decoded allocation (for example MAT4
            # elements advancing by only one byte).
            return None

        # A view may share a buffer with unrelated vertices, indices or images.
        # Even a malformed count must not decode the following view's bytes.
        view_end = min(len(buf), bv_offset + bv_length)
        available = view_end - byte_offset - elem_size
        if available < 0 or count == 0:
            return None
        count = min(count, available // stride + 1)

        decoded = count * num_comps * (8 if is_index else 4)
        budget = getattr(self, '_decode_budget', None)
        if budget is not None:
            if decoded > budget:
                raise ValueError("model data decodes to more than its budget "
                                 f"of {MAX_DECODED_BYTES >> 20} MiB")
            self._decode_budget = budget - decoded
        view = np.ndarray(shape=(count, num_comps), dtype=dtype, buffer=buf,
                          offset=byte_offset, strides=(stride, dtype.itemsize))
        if is_index:
            # int64 so a large UNSIGNED_INT index stays large (and is then
            # rejected as out of range) instead of wrapping negative.
            return view.astype(np.int64).reshape(-1)
        return view.astype(np.float32 if dtype.kind == 'f' else np.int32)

    def _primitives(self):
        for mesh in self.meshes:
            yield from mesh['primitives']

    def get_flattened_vertices(self) -> List[Tuple[float, float, float]]:
        """Get all vertex positions flattened for CPU storage / 2D projection."""
        vertices = self._vertex_array()
        if vertices is None:
            return []
        return [tuple(v) for v in vertices.tolist()]

    def _vertex_array(self) -> Optional[np.ndarray]:
        """:meth:`get_flattened_vertices` as one ``(N, 3)`` array, or None."""
        blocks = [prim['positions'] for prim in self._primitives()
                  if prim['positions'] is not None]
        if not blocks:
            return None
        return np.concatenate(blocks)[:, :3]

    def get_flattened_triangles(self) -> List[Tuple[int, int, int]]:
        """Get all triangle indices flattened (complete triangles only)."""
        triangles = self._triangle_array()
        if triangles is None:
            return []
        return [tuple(t) for t in triangles.tolist()]

    def _triangle_array(self) -> Optional[np.ndarray]:
        """:meth:`get_flattened_triangles` as one ``(T, 3)`` array, or None."""
        blocks = []
        base = 0
        for prim in self._primitives():
            pos = prim['positions']
            n_verts = len(pos) if pos is not None else 0
            indices = prim['indices']
            if indices is not None:
                tri = np.asarray(indices, dtype=np.int64).reshape(-1)
            else:
                # Non-indexed: sequential triangles
                tri = np.arange(n_verts, dtype=np.int64)
            tri = tri[:len(tri) - len(tri) % 3]
            if len(tri):
                blocks.append(tri.reshape(-1, 3) + base)
            base += n_verts
        if not blocks:
            return None
        return np.concatenate(blocks)


# ---------------------------------------------------------------------------
# OpenGL-Ready GLB Model (mirrors OBJ interface)
# ---------------------------------------------------------------------------

class GLB:
    """
    OpenGL-ready GLB model that wraps GLBLoader.
    Provides VAO, vertex buffers, and the interface expected by the renderer.

    Matches the OBJ interface:
        - is_loaded
        - vao, vbo
        - vertex_count
        - groups (for material-based rendering)
        - materials
        - cpu_vertices (for 2D view projection)
        - filepath
    """

    def __init__(self, filepath: str):
        self.filepath = filepath
        self.is_loaded = False
        self.vao = None
        self.vbo = None
        self.ebo = None  # Element buffer for indexed rendering
        self.vertex_count = 0
        self.index_count = 0
        self.groups = []
        self.materials = {}
        self.cpu_vertices = None  # np.array of shape (N, 3) for 2D view projection
        self.cpu_triangles = None  # List of (i0, i1, i2) tuples for wireframe drawing
        self.has_indices = False

        loader = GLBLoader()
        loader._filepath_hint = filepath
        if not loader.load(filepath):
            print(f"[GLB] Failed to load: {filepath}")
            return

        self.materials = loader.materials
        self._build_gl_buffers(loader)
        self.is_loaded = True
        print(f"[GLB] Loaded {self.vertex_count} vertices ({self.index_count} indices) from {filepath}")

    def get_bounds(self):
        """Return axis-aligned bounding box as (min_v, max_v) or None."""
        if self.cpu_vertices is None or len(self.cpu_vertices) == 0:
            return None
        min_v = [float(self.cpu_vertices[:, i].min()) for i in range(3)]
        max_v = [float(self.cpu_vertices[:, i].max()) for i in range(3)]
        return min_v, max_v

    def get_collision_triangles(self):
        """Return list of local-space triangles for mesh-accurate collision.
        Each triangle is ((v0, v1, v2), normal) where v* are (x,y,z)."""
        if self.cpu_vertices is None or len(self.cpu_vertices) == 0:
            return []
        
        tris = []
        for i0, i1, i2 in self.cpu_triangles:
            v0 = tuple(self.cpu_vertices[i0])
            v1 = tuple(self.cpu_vertices[i1])
            v2 = tuple(self.cpu_vertices[i2])
            # Compute face normal
            e1 = (v1[0]-v0[0], v1[1]-v0[1], v1[2]-v0[2])
            e2 = (v2[0]-v0[0], v2[1]-v0[1], v2[2]-v0[2])
            nx = e1[1]*e2[2] - e1[2]*e2[1]
            ny = e1[2]*e2[0] - e1[0]*e2[2]
            nz = e1[0]*e2[1] - e1[1]*e2[0]
            length = math.sqrt(nx*nx + ny*ny + nz*nz)
            if length > 0.001:
                normal = (nx/length, ny/length, nz/length)
            else:
                normal = (0, 1, 0)
            tris.append(((v0, v1, v2), normal))
        return tris

    def _build_gl_buffers(self, loader: GLBLoader):
        """Build OpenGL VAO/VBO/EBO from parsed GLB data."""
        import ctypes

        # Build interleaved vertex data: position(3) + normal(3) + texcoord(2)
        vertex_blocks = []
        index_blocks = []
        total_indices = 0
        vertex_offset = 0

        for mesh in loader.meshes:
            for prim in mesh['primitives']:
                pos = prim['positions']
                nrm = prim['normals']
                uvs = prim['uvs']
                idx = prim['indices']
                mat_name = prim['material']
                mode = prim['mode']

                if pos is None or len(pos) == 0:
                    continue

                if (nrm is None or nrm.shape[1] < 3) and mode == 4:
                    if idx is None:
                        # Its corners are unshared already: the same
                        # vertices, each with its triangle's normal.
                        nrm = flat_normals(pos)
                    else:
                        pos, nrm, uvs, idx = flat_shaded(pos, idx, uvs)
                        if not len(pos):
                            continue

                prim_vertex_count = len(pos)
                block = np.zeros((prim_vertex_count, 8), dtype=np.float32)
                block[:, 0:3] = pos[:, :3]
                block[:, 4] = 1.0  # default normal (0, 1, 0)
                if nrm is not None and nrm.shape[1] >= 3:
                    m = min(prim_vertex_count, len(nrm))
                    block[:m, 3:6] = nrm[:m, :3]
                if uvs is not None and uvs.shape[1] >= 2:
                    m = min(prim_vertex_count, len(uvs))
                    block[:m, 6:8] = uvs[:m, :2]
                vertex_blocks.append(block)

                # Indices
                if idx is not None and len(idx) > 0:
                    group_start = total_indices
                    index_blocks.append(idx + vertex_offset)
                    prim_index_count = len(idx)
                    total_indices += prim_index_count
                    self.has_indices = True
                    indexed = True
                else:
                    # Non-indexed: sequential
                    group_start = vertex_offset
                    prim_index_count = prim_vertex_count
                    indexed = False

                # Register group for material-based rendering
                self.groups.append({
                    'material': mat_name or 'default',
                    'start': group_start,
                    'count': prim_index_count,
                    'mode': mode,
                    'indexed': indexed,
                })

                vertex_offset += prim_vertex_count

        if vertex_blocks:
            interleaved = np.concatenate(vertex_blocks)
            vertex_data = interleaved.reshape(-1)
            # Store cpu_vertices as (N, 3) numpy array for 2D view projection.
            # The model's own vertices, as cpu_triangles numbers them -- not
            # the GL buffer's, which a flat-shaded primitive has unshared.
            self.cpu_vertices = np.ascontiguousarray(
                loader._vertex_array(), dtype=np.float32)
        else:
            vertex_data = np.zeros(0, dtype=np.float32)
            self.cpu_vertices = np.array([], dtype=np.float32)
        index_data = (np.concatenate(index_blocks).astype(np.uint32)
                      if index_blocks else np.zeros(0, dtype=np.uint32))

        self.vertex_count = vertex_offset
        self.index_count = len(index_data)

        # Store flattened triangles for wireframe rendering
        self.cpu_triangles = loader.get_flattened_triangles()

        if not len(vertex_data):
            print(f"[GLB] No vertices generated for {self.filepath}")
            return

        # Create GL buffers
        self.vao = gl.glGenVertexArrays(1)
        self.vbo = gl.glGenBuffers(1)

        gl_state.glBindVertexArray(self.vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, vertex_data.nbytes, vertex_data, gl.GL_STATIC_DRAW)

        stride = 32  # 3*4 + 3*4 + 2*4 = 32 bytes

        # Position attribute (location 0)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)

        # Normal attribute (location 1)
        gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(12))
        gl.glEnableVertexAttribArray(1)

        # Texcoord attribute (location 2)
        gl.glVertexAttribPointer(2, 2, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(24))
        gl.glEnableVertexAttribArray(2)

        # Element buffer if indexed
        if self.has_indices and len(index_data):
            self.ebo = gl.glGenBuffers(1)
            gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, self.ebo)
            gl.glBufferData(gl.GL_ELEMENT_ARRAY_BUFFER, index_data.nbytes, index_data, gl.GL_STATIC_DRAW)

        gl_state.glBindVertexArray(0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)

    def cleanup(self):
        """Release OpenGL resources."""
        if self.ebo:
            gl.glDeleteBuffers(1, [self.ebo])
            self.ebo = None
        if self.vbo:
            gl.glDeleteBuffers(1, [self.vbo])
            self.vbo = None
        if self.vao:
            gl_state.glDeleteVertexArrays(1, [self.vao])
            self.vao = None
        self.is_loaded = False


# ---------------------------------------------------------------------------
# Thumbnail renderer for Asset Browser (mirrors OBJ thumbnail)
# ---------------------------------------------------------------------------

def render_glb_thumbnail(filepath: str, width: int, height: int):
    """Generate a wireframe thumbnail from a GLB file for the asset browser.

    Thin guard around :func:`_render_glb_thumbnail_impl` -- a thumbnail failure
    (a malformed mesh, a NumPy-2 scalar conversion, a GL edge case) must never
    propagate, because the asset browser would otherwise abort loading the rest
    of the folder. The offending file is named on stdout so the failure is
    diagnosable, and the caller falls back to a plain "GLB" placeholder.
    """
    try:
        return _render_glb_thumbnail_impl(filepath, width, height)
    except Exception as exc:
        print(f"[GLB] thumbnail skipped for {filepath}: {exc}")
        return None


#: Most triangles a thumbnail draws. A thumbnail is at most 90 pixels across,
#: so past a few thousand triangles more lines only fill it in; drawing every
#: edge of a 400,000-triangle model one Python call at a time froze the editor
#: for over three seconds. A larger mesh draws an evenly spaced sample.
MAX_THUMBNAIL_TRIANGLES = 20_000


def _thumbnail_lines(vertices, triangles, width, height):
    """The wireframe's lines as an ``(L, 4)`` array of ``x1, y1, x2, y2``.

    The model is centred, scaled to fit and turned to an isometric-ish view
    (45 degrees about Y, then 30 about X); each triangle gives its three
    edges, in order. Edges with an endpoint out of range or not finite are
    dropped. None when there is nothing to draw.

    The arithmetic is the per-vertex loop it replaced, in the same order on
    the same float64 values, so a model draws exactly as it did.
    """
    vertices = np.asarray(vertices, dtype=np.float64).reshape(-1, 3)
    finite = np.isfinite(vertices).all(axis=1)
    if not finite.any():
        return None
    bounds = vertices[finite]
    min_v, max_v = bounds.min(axis=0), bounds.max(axis=0)
    center = (min_v + max_v) / 2
    scale = max(0.0, float(((max_v - min_v) / 2).max()))
    if scale == 0:
        scale = 1

    angle_y = math.radians(45)
    angle_x = math.radians(30)
    cos_y, sin_y = math.cos(angle_y), math.sin(angle_y)
    cos_x, sin_x = math.cos(angle_x), math.sin(angle_x)

    x = (vertices[:, 0] - center[0]) / scale
    y = (vertices[:, 1] - center[1]) / scale
    z = (vertices[:, 2] - center[2]) / scale
    rx = x * cos_y - z * sin_y
    rz = x * sin_y + z * cos_y
    ry = y * cos_x - rz * sin_x
    screen = np.empty((len(vertices), 2))
    screen[:, 0] = width / 2 + rx * (width * 0.4)
    screen[:, 1] = height / 2 - ry * (height * 0.4)

    if triangles is None or not len(triangles):
        return None
    triangles = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
    if len(triangles) > MAX_THUMBNAIL_TRIANGLES:
        triangles = triangles[::-(-len(triangles) // MAX_THUMBNAIL_TRIANGLES)]
    # Edges (0, 1), (1, 2), (2, 0) of each triangle, triangle by triangle.
    edges = np.stack((triangles, np.roll(triangles, -1, axis=1)), axis=2).reshape(-1, 2)
    count = len(vertices)
    usable = ((edges >= 0) & (edges < count)).all(axis=1)
    edges = edges[usable]
    drawable = np.isfinite(screen).all(axis=1)
    edges = edges[drawable[edges].all(axis=1)]
    if not len(edges):
        return None
    return np.concatenate((screen[edges[:, 0]], screen[edges[:, 1]]), axis=1)


def _render_glb_thumbnail_impl(filepath: str, width: int, height: int):
    """
    Generate a wireframe thumbnail from a GLB file for the asset browser.
    A mesh of more than MAX_THUMBNAIL_TRIANGLES draws an evenly spaced sample
    of its triangles (see _thumbnail_lines).
    """
    from PyQt5.QtGui import QPixmap, QColor, QPainter, QPen
    from PyQt5.QtCore import QLineF

    loader = GLBLoader()
    loader._filepath_hint = filepath
    if not loader.load(filepath):
        return None

    vertices = loader._vertex_array()
    if vertices is None or not len(vertices):
        return None
    lines = _thumbnail_lines(vertices, loader._triangle_array(), width, height)

    # Draw
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(50, 50, 60))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    # Draw wireframe edges from triangles
    pen = QPen(QColor(0, 200, 255))
    pen.setWidthF(1.0)
    painter.setPen(pen)
    if lines is not None:
        painter.drawLines([QLineF(*line) for line in lines.tolist()])

    painter.end()
    return pixmap
