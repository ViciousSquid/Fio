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
        """Load a GLB file from disk or ResourceManager fallback."""
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
        """Try direct file read, then ResourceManager fallback."""
        if os.path.exists(filepath):
            try:
                with open(filepath, 'rb') as f:
                    return f.read()
            except IOError:
                pass

        # Fallback to ResourceManager (package mode)
        try:
            from engine.resource_manager import ResourceManager
            rm = ResourceManager()
            return rm.get_binary_asset(filepath)
        except ImportError:
            pass

        return None

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
            byte_length = buf.get('byteLength', 0)

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
            else:
                # External file reference
                ext_path = os.path.join(os.path.dirname(self._filepath_hint or ''), uri)
                if os.path.exists(ext_path):
                    with open(ext_path, 'rb') as f:
                        self.buffers.append(f.read())
                else:
                    self.buffers.append(b'')

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
        if positions is None or len(positions) == 0:
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

    def _read_accessor(self, accessor_idx: Optional[int], is_index: bool = False) -> Optional[np.ndarray]:
        """Read data from an accessor into a numpy array."""
        if accessor_idx is None or accessor_idx >= len(self.accessors):
            return None

        acc = self.accessors[accessor_idx]
        bv_idx = acc.get('bufferView')
        if bv_idx is None or bv_idx >= len(self.buffer_views):
            return None

        bv = self.buffer_views[bv_idx]
        buf_idx = bv.get('buffer', 0)
        if buf_idx >= len(self.buffers):
            return None

        buf = self.buffers[buf_idx]
        byte_offset = bv.get('byteOffset', 0) + acc.get('byteOffset', 0)
        count = acc.get('count', 0)
        component_type = acc.get('componentType', 5126)  # FLOAT default
        accessor_type = acc.get('type', 'SCALAR')

        # Determine component size and numpy dtype
        type_map = {
            5120: ('b', 1),   # BYTE
            5121: ('B', 1),   # UNSIGNED_BYTE
            5122: ('h', 2),   # SHORT
            5123: ('H', 2),   # UNSIGNED_SHORT
            5125: ('I', 4),   # UNSIGNED_INT
            5126: ('f', 4),   # FLOAT
        }

        fmt, comp_size = type_map.get(component_type, ('f', 4))

        # Determine number of components per element
        type_components = {
            'SCALAR': 1,
            'VEC2': 2,
            'VEC3': 3,
            'VEC4': 4,
            'MAT2': 4,
            'MAT3': 9,
            'MAT4': 16,
        }
        num_comps = type_components.get(accessor_type, 1)

        # Calculate stride
        stride = bv.get('byteStride', 0)
        if stride == 0:
            stride = num_comps * comp_size

        # Read data
        data = []
        for i in range(count):
            offset = byte_offset + i * stride
            if offset + num_comps * comp_size > len(buf):
                break
            vals = struct.unpack(f'<{num_comps}{fmt}', buf[offset:offset + num_comps * comp_size])
            data.append(vals)

        if not data:
            return None

        arr = np.array(data, dtype=np.float32 if fmt == 'f' else np.int32)
        if arr.ndim == 1 and num_comps > 1:
            arr = arr.reshape(-1, num_comps)
        return arr

    def get_flattened_vertices(self) -> List[Tuple[float, float, float]]:
        """Get all vertex positions flattened for CPU storage / 2D projection."""
        verts = []
        for mesh in self.meshes:
            for prim in mesh['primitives']:
                pos = prim['positions']
                if pos is not None:
                    for v in pos:
                        verts.append((float(v[0]), float(v[1]), float(v[2])))
        return verts

    def get_flattened_triangles(self) -> List[Tuple[int, int, int]]:
        """Get all triangle indices flattened."""
        tris = []
        base = 0
        for mesh in self.meshes:
            for prim in mesh['primitives']:
                indices = prim['indices']
                if indices is not None:
                    for i in range(0, len(indices), 3):
                        tris.append((int(indices[i]) + base, int(indices[i+1]) + base, int(indices[i+2]) + base))
                else:
                    # Non-indexed: generate sequential triangles
                    pos = prim['positions']
                    if pos is not None:
                        for i in range(0, len(pos), 3):
                            tris.append((base + i, base + i + 1, base + i + 2))
                if prim['positions'] is not None:
                    base += len(prim['positions'])
        return tris


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
        vertices = []
        cpu_verts = []  # List of (x,y,z) tuples for 2D projection
        indices = []
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
                prim_index_count = 0

                # Build interleaved vertices
                for i in range(prim_vertex_count):
                    # Position
                    vx, vy, vz = pos[i]
                    vertices.extend([vx, vy, vz])
                    cpu_verts.append((vx, vy, vz))

                    # Normal
                    if nrm is not None and i < len(nrm):
                        nx, ny, nz = nrm[i]
                        vertices.extend([nx, ny, nz])
                    else:
                        vertices.extend([0.0, 1.0, 0.0])

                    # Texcoord
                    if uvs is not None and i < len(uvs):
                        tu, tv = uvs[i]
                        vertices.extend([tu, tv])
                    else:
                        vertices.extend([0.0, 0.0])

                # Indices
                if idx is not None and len(idx) > 0:
                    for ix in idx:
                        indices.append(int(ix) + vertex_offset)
                    prim_index_count = len(idx)
                    self.has_indices = True
                else:
                    # Non-indexed: sequential
                    prim_index_count = prim_vertex_count

                # Register group for material-based rendering
                self.groups.append({
                    'material': mat_name or 'default',
                    'start': len(indices) - prim_index_count if self.has_indices else vertex_offset,
                    'count': prim_index_count,
                    'mode': mode,
                    'indexed': self.has_indices and idx is not None,
                })

                vertex_offset += prim_vertex_count

        self.vertex_count = len(cpu_verts)
        self.index_count = len(indices)

        # Store cpu_vertices as (N, 3) numpy array for 2D view projection
        self.cpu_vertices = np.array(cpu_verts, dtype=np.float32)
        
        # Store flattened triangles for wireframe rendering
        self.cpu_triangles = loader.get_flattened_triangles()

        if not vertices:
            print(f"[GLB] No vertices generated for {self.filepath}")
            return

        # Create GL buffers
        vertex_data = np.array(vertices, dtype=np.float32)

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
        if self.has_indices and indices:
            self.ebo = gl.glGenBuffers(1)
            index_data = np.array(indices, dtype=np.uint32)
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
    from PyQt5.QtGui import QPixmap, QColor, QPainter, QFont, QPen, QPolygonF
    from PyQt5.QtCore import Qt, QRect, QPointF
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