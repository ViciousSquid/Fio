import os 
import numpy as np 
import ctypes
from OpenGL.GL import *

class OBJ: 
    """ Loads a .obj file, parses vertices, normals, texcoords, and materials. """
    def __init__(self, filename): 
        self.vao = None 
        self.vbo = None 
        self.vertex_count = 0 
        self.is_loaded = False 
        self.filename = filename 
        self.cpu_vertices = [] 
        
        # Materials data: {'mat_name': {'color': [r,g,b], 'texture': 'path.png'}}
        self.materials = {} 
        
        # Rendering groups: [{'material': 'mat_name', 'start': 0, 'count': 0}, ...]
        self.groups = []
        
        self.load(filename)

    def load(self, filename):
        if not os.path.exists(filename):
            print(f"Error: OBJ file not found at '{filename}'")
            return
        
        positions = []
        texcoords = []
        normals = []
        faces = []
        
        # Temporary storage to track material groups during parsing
        current_material = "default"
        # Stores (start_index, face_count) for each material section
        raw_groups = [] 
        face_counter = 0
        
        model_dir = os.path.dirname(filename)

        try:
            with open(filename, "r") as f:
                for line in f:
                    if line.startswith('#'): continue
                    values = line.split()
                    if not values: continue
                    
                    if values[0] == 'v':
                        positions.append(list(map(float, values[1:4])))
                    elif values[0] == 'vt':
                        texcoords.append(list(map(float, values[1:3])))
                    elif values[0] == 'vn':
                        normals.append(list(map(float, values[1:4])))
                    
                    elif values[0] == 'usemtl':
                        mat_name = values[1]
                        # If we have previous faces, finalize the previous group
                        if face_counter > 0:
                            if not raw_groups:
                                # First group was using default/previous material
                                raw_groups.append({'material': current_material, 'count': face_counter})
                            else:
                                # Update count of the *previous* group entry
                                last_count = sum(g['count'] for g in raw_groups)
                                raw_groups.append({'material': current_material, 'count': face_counter - last_count})
                        current_material = mat_name
                        
                    elif values[0] == 'f':
                        face_verts = []
                        for v in values[1:]:
                            w = v.split('/')
                            p_idx = int(w[0]) - 1 if w[0] else -1
                            t_idx = int(w[1]) - 1 if len(w) > 1 and w[1] else -1
                            n_idx = int(w[2]) - 1 if len(w) > 2 and w[2] else -1
                            face_verts.append((p_idx, t_idx, n_idx))
                        
                        # Triangulate
                        for i in range(1, len(face_verts) - 1):
                            faces.append(face_verts[0])
                            faces.append(face_verts[i])
                            faces.append(face_verts[i+1])
                            face_counter += 1
                    
                    elif values[0] == 'mtllib':
                        mtl_filename = " ".join(values[1:])
                        self._load_mtl(mtl_filename, model_dir)

            if not positions or not faces:
                print(f"Warning: No geometry found in {os.path.basename(filename)}")
                return

            # Finalize the last group
            last_count = sum(g['count'] for g in raw_groups)
            if face_counter > last_count:
                raw_groups.append({'material': current_material, 'count': face_counter - last_count})

            # Calculate actual start indices for OpenGL (vertex count = face count * 3)
            current_start = 0
            for grp in raw_groups:
                vert_count = grp['count'] * 3
                if vert_count > 0:
                    self.groups.append({
                        'material': grp['material'],
                        'start': current_start,
                        'count': vert_count
                    })
                    current_start += vert_count

            # Normalize geometry (Center it)
            np_pos = np.array(positions)
            min_coord = np.min(np_pos, axis=0)
            max_coord = np.max(np_pos, axis=0)
            center = (min_coord + max_coord) / 2.0
            
            interleaved_data = []
            self.cpu_vertices = []

            # Build the interleaved vertex buffer
            for p_idx, t_idx, n_idx in faces:
                # Position
                pos = np_pos[p_idx] - center
                interleaved_data.extend(pos)
                self.cpu_vertices.append(pos)
                
                # Normal
                if n_idx != -1 and n_idx < len(normals):
                    interleaved_data.extend(normals[n_idx])
                else:
                    interleaved_data.extend([0, 1, 0])
                
                # TexCoord
                if t_idx != -1 and t_idx < len(texcoords):
                    interleaved_data.extend([texcoords[t_idx][0], 1.0 - texcoords[t_idx][1]])
                else:
                    interleaved_data.extend([0.0, 0.0])

            vertex_data = np.array(interleaved_data, dtype=np.float32)
            self.vertex_count = len(faces) * 3
            
            # OpenGL Setup
            self.vao = glGenVertexArrays(1)
            glBindVertexArray(self.vao)
            
            self.vbo = glGenBuffers(1)
            glBindBuffer(GL_ARRAY_BUFFER, self.vbo)
            glBufferData(GL_ARRAY_BUFFER, vertex_data.nbytes, vertex_data, GL_STATIC_DRAW)
            
            stride = 8 * 4 
            glEnableVertexAttribArray(0)
            glVertexAttribPointer(0, 3, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(0))
            glEnableVertexAttribArray(1)
            glVertexAttribPointer(1, 3, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(12))
            glEnableVertexAttribArray(2)
            glVertexAttribPointer(2, 2, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(24))
            
            glBindVertexArray(0)
            self.is_loaded = True
            print(f"Loaded OBJ: {filename} ({self.vertex_count} vertices, {len(self.groups)} material groups)")

        except Exception as e:
            print(f"Error loading OBJ {filename}: {e}")
            import traceback
            traceback.print_exc()
            self.is_loaded = False

    def _load_mtl(self, mtl_filename, model_dir):
        path = os.path.join(model_dir, mtl_filename)
        if not os.path.exists(path):
            path = os.path.join('assets', 'models', mtl_filename)
        
        if os.path.exists(path):
            current_mtl = None
            try:
                with open(path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith('#'): continue
                        
                        parts = line.split()
                        
                        if parts[0] == 'newmtl':
                            current_mtl = parts[1]
                            self.materials[current_mtl] = {'color': [0.8, 0.8, 0.8], 'texture': None}
                            
                        elif parts[0] == 'Kd' and current_mtl:
                            # Parse Diffuse Color (RGB)
                            self.materials[current_mtl]['color'] = [float(parts[1]), float(parts[2]), float(parts[3])]
                            
                        elif parts[0] == 'map_Kd' and current_mtl:
                            # Parse Texture Map
                            self.materials[current_mtl]['texture'] = os.path.basename(" ".join(parts[1:]))
            except Exception as e:
                print(f"Failed to parse MTL {path}: {e}")

    def cleanup(self):
        if self.vao: glDeleteVertexArrays(1, [self.vao])
        if self.vbo: glDeleteBuffers(1, [self.vbo])