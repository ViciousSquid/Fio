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
from typing import List, Tuple, Optional, Dict, Any


# Models arrive inside shared maps and .fiopak packages, so their sizes and
# indices are untrusted. A .glb (or an external .bin it names) larger than
# this is refused rather than read whole into memory.
MAX_GLB_FILE_BYTES = 256 * 1024 * 1024


def _non_negative_int(value) -> Optional[int]:
    """*value* as an int when it is a non-negative JSON integer, else None."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _read_bounded(path: str) -> Optional[bytes]:
    """A regular file's bytes, or None if missing, special, or too large.

    Requiring a regular file keeps a crafted path such as ``/dev/zero`` from
    being read forever.
    """
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > MAX_GLB_FILE_BYTES:
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

        try:
            self._parse_glb(data)
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
                # External file reference, relative to the model.
                ext_path = os.path.join(os.path.dirname(self._filepath_hint or ''), uri)
                self.buffers.append(_read_bounded(ext_path) or b'')

    def _parse_materials(self):
        """Extract material properties (PBR, textures, colors)."""
        for i, mat in enumerate(self.materials_json):
            name = mat.get('name', f'material_{i}')
            parsed = {
                'name': name,
                'color': [0.8, 0.8, 0.8],
                'metallic': 0.0,
                'roughness': 0.5,
                'emissive': [0.0, 0.0, 0.0],
                'alpha': 1.0,
                'double_sided': mat.get('doubleSided', False),
                'texture': None,
                'normal_texture': None,
                'metallic_roughness_texture': None,
                'emissive_texture': None,
                'occlusion_texture': None,
            }

            # PBR metallic-roughness
            pbr = mat.get('pbrMetallicRoughness', {})
            if pbr:
                base_color = pbr.get('baseColorFactor', [1.0, 1.0, 1.0, 1.0])
                parsed['color'] = base_color[:3]
                parsed['alpha'] = base_color[3] if len(base_color) > 3 else 1.0
                parsed['metallic'] = pbr.get('metallicFactor', 0.0)
                parsed['roughness'] = pbr.get('roughnessFactor', 0.5)

                # Base color texture
                bc_tex = pbr.get('baseColorTexture', {})
                if bc_tex:
                    parsed['texture'] = self._resolve_texture(bc_tex.get('index'))

                # Metallic-roughness texture
                mr_tex = pbr.get('metallicRoughnessTexture', {})
                if mr_tex:
                    parsed['metallic_roughness_texture'] = self._resolve_texture(mr_tex.get('index'))

            # Normal map
            normal_tex = mat.get('normalTexture', {})
            if normal_tex:
                parsed['normal_texture'] = self._resolve_texture(normal_tex.get('index'))

            # Emissive
            parsed['emissive'] = mat.get('emissiveFactor', [0.0, 0.0, 0.0])
            emissive_tex = mat.get('emissiveTexture', {})
            if emissive_tex:
                parsed['emissive_texture'] = self._resolve_texture(emissive_tex.get('index'))

            # Occlusion
            occ_tex = mat.get('occlusionTexture', {})
            if occ_tex:
                parsed['occlusion_texture'] = self._resolve_texture(occ_tex.get('index'))

            self.materials[name] = parsed

    def _resolve_texture(self, texture_index: Optional[int]) -> Optional[str]:
        """Resolve texture index to image source path or data URI."""
        if texture_index is None or texture_index >= len(self.textures):
            return None
        tex = self.textures[texture_index]
        source_idx = tex.get('source')
        if source_idx is None or source_idx >= len(self.images):
            return None
        img = self.images[source_idx]
        uri = img.get('uri')
        if uri:
            return uri
        # Check for bufferView-based image (embedded in GLB)
        bv_idx = img.get('bufferView')
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
        mat_idx = prim.get('material')
        material_name = None
        if mat_idx is not None and mat_idx < len(self.materials_json):
            material_name = self.materials_json[mat_idx].get('name', f'material_{mat_idx}')

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
        of the buffer are dropped, as is an accessor with a negative offset or
        stride.
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
        acc_offset = _non_negative_int(acc.get('byteOffset', 0))
        count = _non_negative_int(acc.get('count', 0))
        if bv_offset is None or acc_offset is None or count is None:
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

        # Whole elements that fit in the buffer.
        available = len(buf) - byte_offset - elem_size
        if available < 0 or count == 0:
            return None
        count = min(count, available // stride + 1)

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
        blocks = [prim['positions'] for prim in self._primitives()
                  if prim['positions'] is not None]
        if not blocks:
            return []
        return [tuple(v) for v in np.concatenate(blocks)[:, :3].tolist()]

    def get_flattened_triangles(self) -> List[Tuple[int, int, int]]:
        """Get all triangle indices flattened (complete triangles only)."""
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
            return []
        return [tuple(t) for t in np.concatenate(blocks).tolist()]


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
                    index_blocks.append(idx + vertex_offset)
                    prim_index_count = len(idx)
                    total_indices += prim_index_count
                    self.has_indices = True
                else:
                    # Non-indexed: sequential
                    prim_index_count = prim_vertex_count

                # Register group for material-based rendering
                self.groups.append({
                    'material': mat_name or 'default',
                    'start': total_indices - prim_index_count if self.has_indices else vertex_offset,
                    'count': prim_index_count,
                    'mode': mode,
                    'indexed': self.has_indices and idx is not None,
                })

                vertex_offset += prim_vertex_count

        if vertex_blocks:
            interleaved = np.concatenate(vertex_blocks)
            vertex_data = interleaved.reshape(-1)
            # Store cpu_vertices as (N, 3) numpy array for 2D view projection
            self.cpu_vertices = np.ascontiguousarray(interleaved[:, 0:3])
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

        gl.glBindVertexArray(self.vao)
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

        gl.glBindVertexArray(0)
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
            gl.glDeleteVertexArrays(1, [self.vao])
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


def _render_glb_thumbnail_impl(filepath: str, width: int, height: int):
    """
    Generate a wireframe thumbnail from a GLB file for the asset browser.
    Falls back to bounding-box preview if mesh is too complex.
    """
    from PyQt5.QtGui import QPixmap, QColor, QPainter, QPen
    from PyQt5.QtCore import QPointF
    import math

    loader = GLBLoader()
    loader._filepath_hint = filepath
    if not loader.load(filepath):
        return None

    vertices = loader.get_flattened_vertices()
    if not vertices:
        return None

    # Normalize vertices to -1..1 range
    min_v = [float('inf')] * 3
    max_v = [float('-inf')] * 3
    for v in vertices:
        for i in range(3):
            if v[i] < min_v[i]: min_v[i] = v[i]
            if v[i] > max_v[i]: max_v[i] = v[i]

    center = [(min_v[i] + max_v[i]) / 2 for i in range(3)]
    scale = 0
    for i in range(3):
        scale = max(scale, (max_v[i] - min_v[i]) / 2)
    if scale == 0: scale = 1

    # 3D Transformation (isometric-ish view)
    angle_y = math.radians(45)
    angle_x = math.radians(30)
    cos_y, sin_y = math.cos(angle_y), math.sin(angle_y)
    cos_x, sin_x = math.cos(angle_x), math.sin(angle_x)

    projected_points = []
    for v in vertices:
        x = (v[0] - center[0]) / scale
        y = (v[1] - center[1]) / scale
        z = (v[2] - center[2]) / scale

        rx = x * cos_y - z * sin_y
        rz = x * sin_y + z * cos_y
        ry = y * cos_x - rz * sin_x

        screen_x = width/2 + rx * (width * 0.4)
        screen_y = height/2 - ry * (height * 0.4)
        projected_points.append(QPointF(screen_x, screen_y))

    # Draw
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(50, 50, 60))

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    # Draw wireframe edges from triangles
    pen = QPen(QColor(0, 200, 255))
    pen.setWidthF(1.0)
    painter.setPen(pen)

    tris = loader.get_flattened_triangles()
    for tri in tris:
        for j in range(3):
            i1 = tri[j]
            i2 = tri[(j + 1) % 3]
            if 0 <= i1 < len(projected_points) and 0 <= i2 < len(projected_points):
                painter.drawLine(projected_points[i1], projected_points[i2])

    painter.end()
    return pixmap