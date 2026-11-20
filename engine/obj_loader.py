from array import array
from typing import List, Tuple, Optional
import os
import math
import numpy as np

# OBJLoader parses CPU data without a graphics runtime. Only OBJ's GPU
# resource methods need PyOpenGL; keep the shared module cached once loaded.
gl = None


# Models arrive inside shared maps and .fiopak packages; an OBJ or MTL beyond
# this is refused rather than read whole into memory.
MAX_OBJ_FILE_BYTES = 256 * 1024 * 1024
#: Most triangles one OBJ may hold once its faces are fanned out. A model at
#: the cap peaks at about 1.6 GB while its buffers are built (the packed faces,
#: the interleaved vertex data and its index arrays); a real model of this
#: size, with texture and normal indices, is already near MAX_OBJ_FILE_BYTES.
MAX_OBJ_TRIANGLES = 4_000_000


def _read_text(path: str) -> Optional[str]:
    """A regular file's UTF-8 text, or None if missing, special or too large.

    Requiring a regular file keeps a crafted ``mtllib /dev/zero`` from being
    read forever.
    """
    try:
        if not os.path.isfile(path):
            # An archived asset (assets/<category>.fiopak), read in place.
            from engine.asset_archives import asset_bytes
            data = asset_bytes(path)
            if data is None or len(data) > MAX_OBJ_FILE_BYTES:
                return None
            return data.decode('utf-8')
        if os.path.getsize(path) > MAX_OBJ_FILE_BYTES:
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


# ---------------------------------------------------------------------------
# Faces
# ---------------------------------------------------------------------------
# A face used to be a dict holding a list of (v, vt, vn) tuples: about 520
# bytes for a triangle, so an 8 MB file of "f 1 2 3" lines peaked at half a
# gigabyte and one at the triangle cap at about 8 GB. The faces are packed
# into flat int arrays instead (44 bytes for a triangle); ``OBJLoader.faces``
# still reads as the list of dicts it was, built one face at a time.

class _FaceTable:
    """An OBJ's faces, packed: every corner's ``(v, vt, vn)`` indices in one
    flat array, where each face starts in it, and each face's material."""

    __slots__ = ('corners', 'starts', 'materials', 'material_names', '_material_index')

    def __init__(self):
        #: (v, vt, vn) per corner, flat; -1 for an index that is missing.
        self.corners = array('i')
        #: Face k's corners are corners ``starts[k]`` to ``starts[k + 1]``
        #: (counted in corners, not ints).
        self.starts = array('i', [0])
        #: Per face, an index into material_names.
        self.materials = array('i')
        #: Material names (None for "no usemtl yet"), in order of first use.
        self.material_names: list = []
        self._material_index: dict = {}

    def __len__(self):
        return len(self.materials)

    def material_id(self, name) -> int:
        index = self._material_index.get(name)
        if index is None:
            index = self._material_index[name] = len(self.material_names)
            self.material_names.append(name)
        return index

    def add(self, corners, material) -> None:
        for corner in corners:
            self.corners.extend(corner)
        self.starts.append(len(self.corners) // 3)
        self.materials.append(self.material_id(material))

    def face(self, k) -> dict:
        first, end = self.starts[k], self.starts[k + 1]
        flat = self.corners[first * 3:end * 3]
        return {'vertices': [tuple(flat[i:i + 3]) for i in range(0, len(flat), 3)],
                'material': self.material_names[self.materials[k]]}

    def triangles(self):
        """``(corners, material_ids)``: every face fanned into triangles, as a
        ``(T, 3, 3)`` int array of (v, vt, vn) corners and the ``(T,)``
        material index of each, in file order -- what fanning the face dicts
        one by one gave, without a Python object per corner."""
        starts = np.frombuffer(self.starts, dtype=np.intc).astype(np.int64)
        per_face = np.maximum(np.diff(starts) - 2, 0)
        total = int(per_face.sum())
        corners = np.frombuffer(self.corners, dtype=np.intc).reshape(-1, 3)
        if total == 0:
            return (np.zeros((0, 3, 3), dtype=np.intc),
                    np.zeros(0, dtype=np.int64))
        face_of = np.repeat(np.arange(len(per_face)), per_face)
        # k = 1 .. count - 2 within each face: triangle (0, k, k + 1).
        k = np.arange(total) - np.repeat(np.cumsum(per_face) - per_face, per_face) + 1
        first = starts[:-1][face_of]
        index = np.stack((first, first + k, first + k + 1), axis=1)
        materials = np.frombuffer(self.materials, dtype=np.intc).astype(np.int64)
        return corners[index], materials[face_of]


class _FaceList:
    """``OBJLoader.faces``: the packed faces read as the list of
    ``{'vertices': [(v, vt, vn), ...], 'material': name}`` dicts they were
    parsed from, each built when it is read."""

    __slots__ = ('_table',)

    def __init__(self, table):
        self._table = table

    def __len__(self):
        return len(self._table)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self._table.face(k) for k in range(*index.indices(len(self)))]
        count = len(self)
        if index < 0:
            index += count
        if not 0 <= index < count:
            raise IndexError('face index out of range')
        return self._table.face(index)

    def __iter__(self):
        face = self._table.face
        for k in range(len(self)):
            yield face(k)

    def __eq__(self, other):
        if isinstance(other, (list, tuple, _FaceList)):
            return len(self) == len(other) and list(self) == list(other)
        return NotImplemented

    def __repr__(self):
        return f'<{len(self)} OBJ faces>'

    def append(self, face) -> None:
        self._table.add(face['vertices'], face.get('material'))


class OBJLoader:
    """
    Wavefront OBJ/MTL loader, reading straight from the filesystem like the
    rest of the renderer.
    """
    
    def __init__(self):
        self.vertices: List[Tuple[float, float, float]] = []
        self.texcoords: List[Tuple[float, float]] = []
        self.normals: List[Tuple[float, float, float]] = []
        self._faces = _FaceTable()
        self.materials: dict = {}
        #: The up axis the file was authored in; a 'z' file was turned Y-up.
        self.up_axis = 'y'

    @property
    def faces(self) -> _FaceList:
        """The parsed faces, as ``{'vertices': [(v, vt, vn), ...],
        'material': name}`` dicts (packed underneath; see _FaceTable)."""
        return _FaceList(self._faces)

    @faces.setter
    def faces(self, faces) -> None:
        table = _FaceTable()
        for face in faces:
            table.add(face['vertices'], face.get('material'))
        self._faces = table

    def material_names(self) -> list:
        """The materials the faces name (None for faces before any usemtl),
        in order of first use."""
        return list(self._faces.material_names)
    
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
        return self.load_text(text, filepath)

    def load_text(self, text: str, filepath="", read_text=None) -> bool:
        """Parse archive-backed OBJ/MTL text without materializing files."""
        self._text_reader = read_text
        lines = text.splitlines()
        current_material = None
        mtl_lib_name = None
        up_tag = None
        triangles = 0
        faces = self._faces
        add_corner = faces.corners.extend
        resolve = self._resolve_index
        material_id = None

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
                # A face is fanned into len-2 triangles when it is built. One
                # line of a million indices costs a few MB of text but would
                # build gigabytes of vertex data: the total is capped.
                triangles += len(parts) - 3
                if triangles > MAX_OBJ_TRIANGLES:
                    print(f"[OBJLoader] {os.path.basename(filepath)} has more than "
                          f"{MAX_OBJ_TRIANGLES} triangles; not loaded")
                    self._faces = _FaceTable()
                    return False
                n_v, n_vt, n_vn = len(self.vertices), len(self.texcoords), len(self.normals)
                for fp in parts[1:]:
                    indices = fp.split('/')
                    add_corner((
                        resolve(indices[0] if indices else '', n_v),
                        resolve(indices[1] if len(indices) > 1 else '', n_vt),
                        resolve(indices[2] if len(indices) > 2 else '', n_vn),
                    ))
                faces.starts.append(len(faces.corners) // 3)
                if material_id is None:
                    # Named on first use by a face, as the material groups
                    # are ordered (see OBJ._build_gl_buffers).
                    material_id = faces.material_id(current_material)
                faces.materials.append(material_id)
            elif keyword == 'usemtl' and len(parts) > 1:
                current_material = ' '.join(parts[1:])
                material_id = None
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
        # The name is model data: it may point beside the OBJ or elsewhere in
        # the same assets folder, never to an absolute, drive, UNC or
        # '..'-escaping path (engine.asset_paths).
        from engine.asset_paths import companion_path, warn_refused
        mtl_path = companion_path(obj_path, mtl_name)
        if mtl_path is None:
            warn_refused(mtl_name, 'material library')
            self._discover_base_color_texture(obj_path)
            return
        mtl_path = os.path.normpath(mtl_path)
        mtl_dir = os.path.dirname(mtl_path)
        
        reader = getattr(self, "_text_reader", None)
        mtl_text = reader(mtl_path) if reader else _read_text(mtl_path)
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
                    'texture': None,
                    'alpha': 1.0,
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
                elif keyword in ('d', 'Tr') and len(parts) > 1:
                    try:
                        alpha = float(parts[1])
                    except ValueError:
                        continue
                    if math.isfinite(alpha):
                        mtl['alpha'] = max(0.0, min(1.0, 1.0 - alpha if keyword == 'Tr' else alpha))
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

        from engine.asset_archives import asset_exists, asset_isdir, asset_listdir
        candidates = []
        for entry in asset_listdir(obj_dir):
            entry_path = os.path.join(obj_dir, entry)
            if entry.lower() == expected and asset_exists(entry_path):
                candidates.append(entry_path)
            elif asset_isdir(entry_path):
                for child in asset_listdir(entry_path):
                    child_path = os.path.join(entry_path, child)
                    if child.lower() == expected and asset_exists(child_path):
                        candidates.append(child_path)

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

        material_names = {name for name in self._faces.material_names if name}
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
        global gl
        if gl is None:
            import OpenGL.GL as gl
        import ctypes
        
        # Fan-triangulate every face into corner index triples, noting each
        # triangle's material (in order of first appearance).
        # (The packed table's material ids are already that order.)
        corner_idx, tri_material = loader._faces.triangles()
        material_rank = {name: rank for rank, name
                         in enumerate(loader._faces.material_names)}

        # Lay triangles out material by material, so each group is one
        # contiguous range even when the file switches back to a material it
        # used earlier (a stable sort keeps the file order within a group).
        order = np.argsort(tri_material, kind='stable')
        corner_idx = corner_idx[order].reshape(-1, 3)

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
        global gl
        if (self.vbo or self.vao) and gl is None:
            import OpenGL.GL as gl
        if self.vbo:
            gl.glDeleteBuffers(1, [self.vbo])
            self.vbo = None
        if self.vao:
            gl.glDeleteVertexArrays(1, [self.vao])
            self.vao = None
        self.is_loaded = False
