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
    
    def load(self, filepath: str) -> bool:
        """
        Load model from filesystem path.
        Falls back to ResourceManager for package mode.
        """
        # Try direct file read first (editor mode)
        text = None
        if os.path.exists(filepath):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    text = f.read()
            except (IOError, UnicodeDecodeError):
                pass
        
        # Fallback to ResourceManager (package mode)
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
        
        # Parse OBJ data
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
                    # Parse "v/vt/vn" format
                    indices = fp.split('/')
                    v_idx = int(indices[0]) - 1 if indices[0] else 0
                    vt_idx = int(indices[1]) - 1 if len(indices) > 1 and indices[1] else -1
                    vn_idx = int(indices[2]) - 1 if len(indices) > 2 and indices[2] else -1
                    face['vertices'].append((v_idx, vt_idx, vn_idx))
                self.faces.append(face)
            elif keyword == 'usemtl' and len(parts) > 1:
                current_material = parts[1]
            elif keyword == 'mtllib' and len(parts) > 1:
                # Join all remaining parts to handle spaces in filenames
                mtl_lib_name = ' '.join(parts[1:])
        
        # Load companion MTL if referenced
        if mtl_lib_name:
            self._load_mtl(filepath, mtl_lib_name)
        
        return True
    
    def _load_mtl(self, obj_path: str, mtl_name: str) -> None:
        """Resolve MTL path relative to OBJ location and load."""
        # Derive MTL path from OBJ directory
        obj_dir = os.path.dirname(obj_path)
        mtl_path = os.path.join(obj_dir, mtl_name)
        mtl_dir = os.path.dirname(mtl_path)  # Directory containing the MTL file
        
        mtl_text = None
        
        # Try direct file read first
        if os.path.exists(mtl_path):
            try:
                with open(mtl_path, 'r', encoding='utf-8') as f:
                    mtl_text = f.read()
            except (IOError, UnicodeDecodeError):
                pass
        
        # Fallback to ResourceManager
        if mtl_text is None:
            try:
                from engine.resource_manager import ResourceManager
                rm = ResourceManager()
                mtl_text = rm.get_text_asset(mtl_path)
            except ImportError:
                pass
        
        if mtl_text is None:
            print(f"[OBJLoader] MTL not found: {mtl_path}")
            return
        
        current_mtl = None
        for line in mtl_text.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            parts = line.split()
            if not parts:
                continue
            
            keyword = parts[0]
            
            if keyword == 'newmtl' and len(parts) > 1:
                current_mtl = parts[1]
                self.materials[current_mtl] = {
                    'diffuse': (0.8, 0.8, 0.8),
                    'ambient': (0.2, 0.2, 0.2),
                    'specular': (0.0, 0.0, 0.0),
                    'texture': None
                }
            elif current_mtl:
                mtl = self.materials[current_mtl]
                if keyword == 'Kd' and len(parts) >= 4:
                    mtl['diffuse'] = (float(parts[1]), float(parts[2]), float(parts[3]))
                elif keyword == 'Ka' and len(parts) >= 4:
                    mtl['ambient'] = (float(parts[1]), float(parts[2]), float(parts[3]))
                elif keyword == 'Ks' and len(parts) >= 4:
                    mtl['specular'] = (float(parts[1]), float(parts[2]), float(parts[3]))
                elif keyword in ('map_Kd', 'map_Ka') and len(parts) > 1:
                    # Join all remaining parts to handle spaces in filenames
                    mtl['texture'] = ' '.join(parts[1:])
                    mtl['mtl_dir'] = mtl_dir  # Store MTL directory for texture path resolution


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
        self.cpu_vertices = None  # np.array of shape (N, 3) for 2D view projection
        
        loader = OBJLoader()
        if not loader.load(filepath):
            print(f"[OBJ] Failed to load: {filepath}")
            return
        
        self.materials = loader.materials
        self._build_gl_buffers(loader)
        self.is_loaded = True
        print(f"[OBJ] Loaded {self.vertex_count} vertices from {filepath}")
    
    def _build_gl_buffers(self, loader: OBJLoader):
        """Build OpenGL VAO/VBO from parsed OBJ data."""
        import ctypes
        
        # Build interleaved vertex data: position(3) + normal(3) + texcoord(2)
        vertices = []
        cpu_verts = []  # List of (x,y,z) tuples for 2D projection
        
        # Group faces by material
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
            # Triangulate if needed (fan triangulation for n-gons)
            for i in range(1, len(face_verts) - 1):
                # Triangle: 0, i, i+1
                for idx in [0, i, i + 1]:
                    v_idx, vt_idx, vn_idx = face_verts[idx]
                    
                    # Position
                    if 0 <= v_idx < len(loader.vertices):
                        vx, vy, vz = loader.vertices[v_idx]
                    else:
                        vx, vy, vz = 0.0, 0.0, 0.0
                    
                    # Normal
                    if 0 <= vn_idx < len(loader.normals):
                        nx, ny, nz = loader.normals[vn_idx]
                    else:
                        nx, ny, nz = 0.0, 1.0, 0.0
                    
                    # Texcoord
                    if 0 <= vt_idx < len(loader.texcoords):
                        u, v = loader.texcoords[vt_idx]
                    else:
                        u, v = 0.0, 0.0
                    
                    vertices.extend([vx, vy, vz, nx, ny, nz, u, v])
                    cpu_verts.append((vx, vy, vz))
                
                material_groups[mat_name]['count'] += 3
                current_group_start += 3
        
        self.vertex_count = len(cpu_verts)
        
        # Build groups list for material-based rendering
        self.groups = []
        for mat_name, group_info in material_groups.items():
            if group_info['count'] > 0:
                self.groups.append({
                    'material': mat_name or 'default',
                    'start': group_info['start'],
                    'count': group_info['count']
                })
        
        # Store cpu_vertices as (N, 3) numpy array for 2D view projection
        self.cpu_vertices = np.array(cpu_verts, dtype=np.float32)
        
        if not vertices:
            print(f"[OBJ] No vertices generated for {self.filepath}")
            return
        
        # Create GL buffers
        vertex_data = np.array(vertices, dtype=np.float32)
        
        self.vao = gl.glGenVertexArrays(1)
        self.vbo = gl.glGenBuffers(1)
        
        gl.glBindVertexArray(self.vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, vertex_data.nbytes, vertex_data, gl.GL_STATIC_DRAW)
        
        # Position attribute (location 0)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        
        # Normal attribute (location 1)
        gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(12))
        gl.glEnableVertexAttribArray(1)
        
        # Texcoord attribute (location 2)
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