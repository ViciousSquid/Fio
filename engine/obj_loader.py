from typing import List, Tuple, Optional
import os
import numpy as np
import OpenGL.GL as gl


# Models arrive inside shared maps and .fiopak packages; an OBJ or MTL beyond
# this is refused rather than read whole into memory.
MAX_OBJ_FILE_BYTES = 256 * 1024 * 1024


def _read_text(path: str) -> Optional[str]:
    """A regular file's UTF-8 text, or None if missing, special or too large.

    Requiring a regular file keeps a crafted ``mtllib /dev/zero`` from being
    read forever.
    """
    try:
        if not os.path.isfile(path) or os.path.getsize(path) > MAX_OBJ_FILE_BYTES:
            return None
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read(MAX_OBJ_FILE_BYTES + 1)
    except (OSError, UnicodeDecodeError):
        return None
    return text if len(text) <= MAX_OBJ_FILE_BYTES else None


# ---------------------------------------------------------------------------
# Up axis
# ---------------------------------------------------------------------------
# Fio is Y-up. An OBJ file does not say which way is up, and some exporters
# write Z-up (the model then appears on its side). A model is taken as Z-up
# when it stands on the z = 0 plane, has real depth along Z, and hangs well
# below y = 0 -- a Y-up model standing on y = 0, or one centred on both, is
# left as it is. A comment in the file settles it either way:
#     # fio:up=z      or      # fio:up=y

UP_AXIS_TAG = 'fio:up='
_BASE_TOLERANCE = 0.01          # of the model's extent along that axis


def up_axis_tag(comment):
    """'y' or 'z' from a ``# fio:up=...`` comment line, else None."""
    text = comment.lstrip('#').strip().lower().replace(' ', '')
    if text.startswith(UP_AXIS_TAG):
        value = text[len(UP_AXIS_TAG):][:1]
        if value in ('y', 'z'):
            return value
    return None


def detect_up_axis(vertices, tag=None):
    """'z' for a model authored Z-up, else 'y' (see above)."""
    if tag in ('y', 'z'):
        return tag
    if not vertices:
        return 'y'
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    y_min, y_size = min(ys), max(ys) - min(ys)
    z_min, z_size = min(zs), max(zs) - min(zs)
    if z_size <= 0.0 or y_size <= 0.0:
        return 'y'
    stands_on_z = abs(z_min) <= _BASE_TOLERANCE * z_size
    stands_on_y = abs(y_min) <= _BASE_TOLERANCE * y_size
    hangs_below_y = y_min < -_BASE_TOLERANCE * y_size
    deep_along_z = z_size >= 0.5 * y_size
    return 'z' if stands_on_z and not stands_on_y and hangs_below_y and deep_along_z else 'y'


def z_up_to_y_up(v):
    """A Z-up point or normal in Fio's Y-up: -90 degrees about X (a rotation,
    so faces keep their winding)."""
    return (v[0], v[2], -v[1])


class OBJLoader:
    """
    Wavefront OBJ/MTL loader, reading straight from the filesystem like the
    rest of the renderer.
    """
    
    def __init__(self):
        self.vertices: List[Tuple[float, float, float]] = []
        self.texcoords: List[Tuple[float, float]] = []
        self.normals: List[Tuple[float, float, float]] = []
        self.faces: List[dict] = []
        self.materials: dict = {}
        #: The up axis the file was authored in; a 'z' file was turned Y-up.
        self.up_axis = 'y'
    
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
        """Load model from filesystem path.

        A coordinate that does not parse loads as zeros, so one bad line costs
        one vertex rather than the whole model, and later face indices still
        point where the file meant.
        """
        text = _read_text(filepath)
        if text is None:
            print(f"[OBJLoader] Failed to load: {filepath}")
            return False
        
        lines = text.splitlines()
        current_material = None
        mtl_lib_name = None
        up_tag = None
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if line.startswith('#'):
                up_tag = up_axis_tag(line) or up_tag
                continue
            
            parts = line.split()
            if not parts:
                continue
            
            keyword = parts[0]
            
            if keyword == 'v' and len(parts) >= 4:
                try:
                    self.vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError:
                    self.vertices.append((0.0, 0.0, 0.0))
            elif keyword == 'vt' and len(parts) >= 3:
                try:
                    self.texcoords.append((float(parts[1]), float(parts[2])))
                except ValueError:
                    self.texcoords.append((0.0, 0.0))
            elif keyword == 'vn' and len(parts) >= 4:
                try:
                    self.normals.append((float(parts[1]), float(parts[2]), float(parts[3])))
                except ValueError:
                    self.normals.append((0.0, 0.0, 0.0))
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
        
        self.up_axis = detect_up_axis(self.vertices, up_tag)
        if self.up_axis == 'z':
            self.vertices = [z_up_to_y_up(v) for v in self.vertices]
            self.normals = [z_up_to_y_up(n) for n in self.normals]
            print(f"[OBJLoader] {os.path.basename(filepath)} is Z-up; turned to Fio's Y-up")

        if mtl_lib_name:
            self._load_mtl(filepath, mtl_lib_name)
        
        return True
    
    def _load_mtl(self, obj_path: str, mtl_name: str) -> None:
        """Resolve MTL path relative to OBJ location and load."""
        obj_dir = os.path.dirname(obj_path)
        normalized_name = str(mtl_name).strip().strip('"').replace('\\', os.sep).replace('/', os.sep)
        mtl_path = os.path.normpath(os.path.join(obj_dir, normalized_name))
        mtl_dir = os.path.dirname(mtl_path)
        
        mtl_text = _read_text(mtl_path)
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
                if keyword in ('Kd', 'Ka', 'Ks') and len(parts) >= 4:
                    try:
                        colour = (float(parts[1]), float(parts[2]), float(parts[3]))
                    except ValueError:
                        continue
                    if keyword == 'Kd':
                        mtl['diffuse'] = colour
                        mtl['color'] = colour
                    elif keyword == 'Ka':
                        mtl['ambient'] = colour
                    else:
                        mtl['specular'] = colour
                elif keyword in ('map_Kd', 'map_Ka') and len(parts) > 1:
                    texture = self._parse_texture_map(parts[1:])
                    if texture:
                        mtl['texture'] = texture
                        mtl['mtl_dir'] = mtl_dir


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

        self.is_loaded = True
    
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
        
        # Fan-triangulate every face into corner index triples, noting each
        # triangle's material (in order of first appearance).
        corners = []
        tri_material = []
        material_rank = {}
        for face in loader.faces:
            mat_name = face.get('material', None)
            rank = material_rank.setdefault(mat_name, len(material_rank))
            face_verts = face['vertices']
            first = face_verts[0]
            for i in range(1, len(face_verts) - 1):
                corners.append(first)
                corners.append(face_verts[i])
                corners.append(face_verts[i + 1])
                tri_material.append(rank)

        # Lay triangles out material by material, so each group is one
        # contiguous range even when the file switches back to a material it
        # used earlier (a stable sort keeps the file order within a group).
        corner_idx = np.asarray(corners, dtype=np.int64).reshape(-1, 3)
        tri_material = np.asarray(tri_material, dtype=np.int64)
        order = np.argsort(tri_material, kind='stable')
        corner_idx = corner_idx.reshape(-1, 3, 3)[order].reshape(-1, 3)

        n = len(corner_idx)
        vertex_data = np.zeros((n, 8), dtype=np.float32)
        vertex_data[:, 4] = 1.0  # default normal (0, 1, 0)
        # (corner slot, source table, first column, width); a corner whose
        # index is missing keeps the default above.
        for slot, source, column, width in ((0, loader.vertices, 0, 3),
                                            (2, loader.normals, 3, 3),
                                            (1, loader.texcoords, 6, 2)):
            if n == 0 or not source:
                continue
            table = np.asarray(source, dtype=np.float32).reshape(-1, width)
            idx = corner_idx[:, slot]
            valid = (idx >= 0) & (idx < len(table))
            vertex_data[valid, column:column + width] = table[idx[valid]]

        self.vertex_count = n
        self.cpu_triangles = [(i, i + 1, i + 2) for i in range(0, n, 3)]

        self.groups = []
        counts = np.bincount(tri_material, minlength=len(material_rank)) * 3
        start = 0
        for mat_name, rank in material_rank.items():
            count = int(counts[rank])
            if count > 0:
                self.groups.append({
                    'material': mat_name or 'default',
                    'start': start,
                    'count': count
                })
            start += count

        self.cpu_vertices = (np.ascontiguousarray(vertex_data[:, 0:3]) if n
                             else np.array([], dtype=np.float32))

        if not n:
            print(f"[OBJ] No vertices generated for {self.filepath}")
            return
        
        vertex_data = vertex_data.reshape(-1)

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