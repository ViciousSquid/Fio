from typing import List, Tuple, Optional, Iterator
import os
import numpy as np
import OpenGL.GL as gl


class OBJLoader:
    """
    Wavefront OBJ/MTL loader.
    Uses direct filesystem access (like the rest of the renderer) with
    optional ResourceManager fallback for package mode.
    """
    
    def __init__(self):
        self.vertices: List[Tuple[float, float, float]] = []
        self.texcoords: List[Tuple[float, float]] = []
        self.normals: List[Tuple[float, float, float]] = []
        self.faces: List[dict] = []
        self.materials: dict = {}
    
    @staticmethod
    def _resolve_index(value: str, length: int) -> int:
        """Resolve a 1-based OBJ index, including OBJ's negative-index form."""
        if not value:
            return -1
        try:
            index = int(value)
        except (TypeError, ValueError):
            return -1

        if index > 0:
            index -= 1
        elif index < 0:
            index = length + index
        else:
            return -1

        return index if 0 <= index < length else -1

    def load(self, filepath: str) -> bool:
        """
        Load model from filesystem path.
        Falls back to ResourceManager for package mode.
        """
        text = None
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    text = f.read()
            except (IOError, UnicodeDecodeError):
                pass
        
        if text is None:
            try:
                from engine.resource_manager import ResourceManager
                rm = ResourceManager()
                text = rm.get_text_asset(filepath)
            except ImportError:
                pass
        
        if text is None:
            print(f"[OBJLoader] Failed to load: {filepath}")
            return False
        
        lines = text.splitlines()
        current_material = None
        mtl_lib_name = None
        
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            parts = line.split()
            if not parts:
                continue
            
            keyword = parts[0]
            
            if keyword == 'v' and len(parts) >= 4:
                self.vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif keyword == 'vt' and len(parts) >= 3:
                self.texcoords.append((float(parts[1]), float(parts[2])))
            elif keyword == 'vn' and len(parts) >= 4:
                self.normals.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif keyword == 'f' and len(parts) >= 4:
                face = {'vertices': [], 'material': current_material}
                for fp in parts[1:]:
                    indices = fp.split('/')
                    v_idx = self._resolve_index(
                        indices[0] if indices else '',
                        len(self.vertices),
                    )
                    vt_idx = self._resolve_index(
                        indices[1] if len(indices) > 1 else '',
                        len(self.texcoords),
                    )
                    vn_idx = self._resolve_index(
                        indices[2] if len(indices) > 2 else '',
                        len(self.normals),
                    )
                    face['vertices'].append((v_idx, vt_idx, vn_idx))
                self.faces.append(face)
            elif keyword == 'usemtl' and len(parts) > 1:
                current_material = ' '.join(parts[1:])
            elif keyword == 'mtllib' and len(parts) > 1:
                mtl_lib_name = ' '.join(parts[1:])
        
        if mtl_lib_name:
            self._load_mtl(filepath, mtl_lib_name)
        
        return True
    
    def _load_mtl(self, obj_path: str, mtl_name: str) -> None:
        """Resolve MTL path relative to OBJ location and load."""
        obj_dir = os.path.dirname(obj_path)
        normalized_name = str(mtl_name).strip().strip('"').replace('\\', os.sep).replace('/', os.sep)
        mtl_path = os.path.normpath(os.path.join(obj_dir, normalized_name))
        mtl_dir = os.path.dirname(mtl_path)
        
        mtl_text = None
        if os.path.exists(mtl_path):
            try:
                with open(mtl_path, 'r', encoding='utf-8') as f:
                    mtl_text = f.read()
            except (IOError, UnicodeDecodeError):
                pass
        
        if mtl_text is None:
            try:
                from engine.resource_manager import ResourceManager
                rm = ResourceManager()
                mtl_text = rm.get_text_asset(mtl_path.replace(os.sep, '/'))
            except ImportError:
                pass
        
        if mtl_text is None:
            print(f"[OBJLoader] MTL not found: {mtl_path}")
            self._discover_base_color_texture(obj_path)
            return
        
        current_mtl = None
        loaded_materials = 0
        for line in mtl_text.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            parts = line.split()
            if not parts:
                continue
            
            keyword = parts[0]
            
            if keyword == 'newmtl' and len(parts) > 1:
                current_mtl = ' '.join(parts[1:])
                self.materials[current_mtl] = {
                    'diffuse': (0.8, 0.8, 0.8),
                    'color': (0.8, 0.8, 0.8),
                    'ambient': (0.2, 0.2, 0.2),
                    'specular': (0.0, 0.0, 0.0),
                    'texture': None
                }
                loaded_materials += 1
            elif current_mtl:
                mtl = self.materials[current_mtl]
                if keyword == 'Kd' and len(parts) >= 4:
                    mtl['diffuse'] = (float(parts[1]), float(parts[2]), float(parts[3]))
                    mtl['color'] = mtl['diffuse']
                elif keyword == 'Ka' and len(parts) >= 4:
                    mtl['ambient'] = (float(parts[1]), float(parts[2]), float(parts[3]))
                elif keyword == 'Ks' and len(parts) >= 4:
                    mtl['specular'] = (float(parts[1]), float(parts[2]), float(parts[3]))
                elif keyword in ('map_Kd', 'map_Ka') and len(parts) > 1:
                    texture = self._parse_texture_map(parts[1:])
                    if texture:
                        mtl['texture'] = texture
                        mtl['mtl_dir'] = mtl_dir

        print(
            f"[OBJLoader] Loaded MTL: {mtl_path} "
            f"({loaded_materials} material{'s' if loaded_materials != 1 else ''})"
        )

    @staticmethod
    def _parse_texture_map(parts: List[str]) -> Optional[str]:
        """Extract the texture filename while ignoring MTL map options."""
        if not parts:
            return None

        options_with_args = {
            '-blendu': 1,
            '-blendv': 1,
            '-boost': 1,
            '-mm': 2,
            '-o': 3,
            '-s': 3,
            '-t': 3,
            '-texres': 1,
            '-clamp': 1,
            '-bm': 1,
            '-imfchan': 1,
            '-type': 1,
        }

        i = 0
        while i < len(parts):
            token = parts[i]
            if not token.startswith('-'):
                return ' '.join(parts[i:]).strip().strip('"').replace('\\', os.sep).replace('/', os.sep)

            option = token.lower()
            arg_count = options_with_args.get(option)
            if arg_count is None:
                # Unknown map option: skip the option itself and continue.
                i += 1
                continue

            i += 1 + arg_count

        return None

    def _discover_base_color_texture(self, obj_path: str) -> None:
        """Discover a conventional base-colour texture when an MTL is missing.

        Some exported OBJ assets contain UVs and a texture set but omit the
        companion MTL file. Fio only needs the base-colour map for its current
        OBJ material path, so look for <model>_BaseColor.png in the model
        directory and its immediate subdirectories.
        """
        obj_dir = os.path.dirname(obj_path) or '.'
        stem = os.path.splitext(os.path.basename(obj_path))[0].lower()
        expected = f"{stem}_basecolor.png"

        candidates = []
        try:
            entries = os.listdir(obj_dir)
        except OSError:
            entries = []

        for entry in entries:
            entry_path = os.path.join(obj_dir, entry)
            if os.path.isfile(entry_path) and entry.lower() == expected:
                candidates.append(entry_path)
            elif os.path.isdir(entry_path):
                try:
                    for child in os.listdir(entry_path):
                        child_path = os.path.join(entry_path, child)
                        if os.path.isfile(child_path) and child.lower() == expected:
                            candidates.append(child_path)
                except OSError:
                    continue

        if not candidates:
            return

        texture_path = os.path.normpath(candidates[0])
        material = {
            'diffuse': (1.0, 1.0, 1.0),
            'color': (1.0, 1.0, 1.0),
            'ambient': (0.2, 0.2, 0.2),
            'specular': (0.0, 0.0, 0.0),
            'texture': os.path.basename(texture_path),
            'mtl_dir': os.path.dirname(texture_path),
        }

        material_names = {
            face.get('material')
            for face in self.faces
            if face.get('material')
        }
        if material_names:
            for material_name in material_names:
                self.materials[material_name] = material.copy()
        else:
            self.materials['default'] = material

        print(f"[OBJLoader] Using discovered base-color texture: {texture_path}")


class OBJ:
    """
    OpenGL-ready OBJ model that wraps OBJLoader.
    Provides VAO, vertex buffers, and the interface expected by the renderer.
    """
    
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.is_loaded = False
        self.vao = None
        self.vbo = None
        self.vertex_count = 0
        self.groups = []
        self.materials = {}
        self.cpu_vertices = None
        self.cpu_triangles = []
        self.origin_offset = np.zeros(3, dtype=np.float32)
        self.centered_for_import = False
        self.source_bounds = None
        
        loader = OBJLoader()
        if not loader.load(filepath):
            print(f"[OBJ] Failed to load: {filepath}")
            return
        
        self.source_bounds = self._repair_import_origin(loader)
        self.materials = loader.materials
        self._build_gl_buffers(loader)

        if self.source_bounds is not None:
            source_min, source_max = self.source_bounds
            print(
                f"[OBJ] Source bounds: min={source_min.tolist()} "
                f"max={source_max.tolist()}"
            )
        if self.centered_for_import:
            print(
                f"[OBJ] Recentered mesh by offset="
                f"{self.origin_offset.tolist()}"
            )

        if self.cpu_vertices is not None and len(self.cpu_vertices):
            mins = self.cpu_vertices.min(axis=0)
            maxs = self.cpu_vertices.max(axis=0)
            centre = (mins + maxs) * 0.5
            size = maxs - mins
            print(
                f"[OBJ] Bounds: min={mins.tolist()} "
                f"max={maxs.tolist()} "
                f"size={size.tolist()} "
                f"centre={centre.tolist()}"
            )

        self.is_loaded = True
        print(f"[OBJ] Loaded {self.vertex_count} vertices from {filepath}")
    
    def _repair_import_origin(self, loader: OBJLoader):
        """Repair obviously broken imported pivots while preserving normal pivots.

        Downloaded OBJ files frequently contain mesh coordinates that are far
        from their object origin. Fio places a model entity at its origin, so
        an extreme offset can make an otherwise valid model appear to be
        missing. Only axes whose bounding-box centre is clearly detached from
        the mesh are corrected; ordinary base/side pivots are left untouched.
        """
        if not loader.vertices:
            return None

        source_vertices = np.asarray(loader.vertices, dtype=np.float32)
        source_min = source_vertices.min(axis=0)
        source_max = source_vertices.max(axis=0)
        source_centre = (source_min + source_max) * 0.5
        half_extent = (source_max - source_min) * 0.5

        threshold = np.maximum(half_extent * 4.0, 2.0)
        offset = np.where(
            np.abs(source_centre) > threshold,
            source_centre,
            0.0,
        ).astype(np.float32)

        if np.any(np.abs(offset) > 0.0):
            corrected = source_vertices - offset
            loader.vertices = [tuple(vertex) for vertex in corrected]
            self.origin_offset = offset
            self.centered_for_import = True

        return source_min, source_max

    def _build_gl_buffers(self, loader: OBJLoader):
        """Build OpenGL VAO/VBO from parsed OBJ data."""
        import ctypes
        
        vertices = []
        cpu_verts = []
        material_groups = {}
        current_group_start = 0
        
        for face in loader.faces:
            mat_name = face.get('material', None)
            if mat_name not in material_groups:
                material_groups[mat_name] = {
                    'start': current_group_start,
                    'count': 0,
                    'material': mat_name
                }
            
            face_verts = face['vertices']
            for i in range(1, len(face_verts) - 1):
                triangle_start = len(cpu_verts)
                for idx in [0, i, i + 1]:
                    v_idx, vt_idx, vn_idx = face_verts[idx]
                    
                    if 0 <= v_idx < len(loader.vertices):
                        vx, vy, vz = loader.vertices[v_idx]
                    else:
                        vx, vy, vz = 0.0, 0.0, 0.0
                    
                    if 0 <= vn_idx < len(loader.normals):
                        nx, ny, nz = loader.normals[vn_idx]
                    else:
                        nx, ny, nz = 0.0, 1.0, 0.0
                    
                    if 0 <= vt_idx < len(loader.texcoords):
                        u, v = loader.texcoords[vt_idx]
                    else:
                        u, v = 0.0, 0.0
                    
                    vertices.extend([vx, vy, vz, nx, ny, nz, u, v])
                    cpu_verts.append((vx, vy, vz))
                self.cpu_triangles.append((triangle_start, triangle_start + 1, triangle_start + 2))
                material_groups[mat_name]['count'] += 3
                current_group_start += 3
        
        self.vertex_count = len(cpu_verts)
        
        self.groups = []
        for mat_name, group_info in material_groups.items():
            if group_info['count'] > 0:
                self.groups.append({
                    'material': mat_name or 'default',
                    'start': group_info['start'],
                    'count': group_info['count']
                })
        
        self.cpu_vertices = np.array(cpu_verts, dtype=np.float32)
        
        if not vertices:
            print(f"[OBJ] No vertices generated for {self.filepath}")
            return
        
        vertex_data = np.array(vertices, dtype=np.float32)
        
        self.vao = gl.glGenVertexArrays(1)
        self.vbo = gl.glGenBuffers(1)
        
        gl.glBindVertexArray(self.vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, vertex_data.nbytes, vertex_data, gl.GL_STATIC_DRAW)
        
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        
        gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(12))
        gl.glEnableVertexAttribArray(1)
        
        gl.glVertexAttribPointer(2, 2, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(24))
        gl.glEnableVertexAttribArray(2)
        
        gl.glBindVertexArray(0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
    
    def cleanup(self):
        """Release OpenGL resources."""
        if self.vbo:
            gl.glDeleteBuffers(1, [self.vbo])
            self.vbo = None
        if self.vao:
            gl.glDeleteVertexArrays(1, [self.vao])
            self.vao = None
        self.is_loaded = False