# obj_loader.py
import os
import numpy as np
from PyQt5.QtWidgets import QMessageBox
from OpenGL.GL import *
from OpenGL.arrays import vbo

class OBJ:
    """
    Loads a .obj file, normalizes its geometry, and prepares it for rendering
    with an interleaved VBO.
    """
    def __init__(self, filename):
        self.vbo = None
        self.element_buffer = None
        self.vertex_count = 0
        self.is_loaded = False
        self.load(filename)
    
    def load(self, filename):
        if not os.path.exists(filename):
            print(f"Error: OBJ file not found at '{filename}'")
            return
        
        temp_vertices, temp_normals, face_data = [], [], []
        try:
            with open(filename, "r") as f:
                for line in f:
                    if line.startswith('#'): continue
                    values = line.split()
                    if not values: continue
                    
                    if values[0] == 'v':
                        temp_vertices.append(list(map(float, values[1:4])))
                    elif values[0] == 'vn':
                        temp_normals.append(list(map(float, values[1:4])))
                    elif values[0] == 'f':
                        face_row = []
                        for v in values[1:]:
                            try:
                                w = v.split('/')
                                v_idx = int(w[0]) - 1
                                n_idx = int(w[2]) - 1 if len(w) > 2 and w[2] else -1
                                face_row.append((v_idx, n_idx))
                            except (ValueError, IndexError):
                                continue
                        face_data.append(face_row)

            if not temp_vertices or not face_data:
                QMessageBox.warning(None, "Load Error", f"No vertex or face data found in {os.path.basename(filename)}.")
                return

            min_coord = np.min(temp_vertices, axis=0)
            max_coord = np.max(temp_vertices, axis=0)
            center = (min_coord + max_coord) / 2.0
            size = np.max(max_coord - min_coord)
            scale_factor = 1.0 / size if size > 0 else 1.0
            normalized_vertices = [((v - center) * scale_factor).tolist() for v in temp_vertices]
            
            final_vbo_data, final_indices, vertex_map = [], [], {}
            for face in face_data:
                for i in range(1, len(face) - 1):
                    indices = [face[0], face[i], face[i+1]]
                    for v_idx, n_idx in indices:
                        if (v_idx, n_idx) not in vertex_map:
                            vertex_map[(v_idx, n_idx)] = len(final_vbo_data)
                            pos = normalized_vertices[v_idx]
                            norm = temp_normals[n_idx] if n_idx != -1 and n_idx < len(temp_normals) else [0, 1, 0]
                            final_vbo_data.append(pos + norm)
                        final_indices.append(vertex_map[(v_idx, n_idx)])
            
            if not final_vbo_data or not final_indices:
                QMessageBox.warning(None, "Load Error", f"Could not generate valid render data from {os.path.basename(filename)}.")
                return

            self.vbo = vbo.VBO(np.array(final_vbo_data, dtype=np.float32))
            self.element_buffer = vbo.VBO(np.array(final_indices, dtype=np.uint32), target=GL_ELEMENT_ARRAY_BUFFER)
            self.vertex_count = len(final_indices)
            self.is_loaded = True

        except Exception as e:
            QMessageBox.critical(None, "Load Error", f"An error occurred while loading the OBJ file:\n{e}")
            self.is_loaded = False

    def render(self):
        if not self.is_loaded: return
        try:
            self.vbo.bind()
            self.element_buffer.bind()
            stride = 24
            glEnableClientState(GL_VERTEX_ARRAY)
            glVertexPointer(3, GL_FLOAT, stride, self.vbo)
            glEnableClientState(GL_NORMAL_ARRAY)
            glNormalPointer(GL_FLOAT, stride, self.vbo + 12)
            glDrawElements(GL_TRIANGLES, self.vertex_count, GL_UNSIGNED_INT, None)
        finally:
            self.element_buffer.unbind()
            self.vbo.unbind()
            glDisableClientState(GL_VERTEX_ARRAY)
            glDisableClientState(GL_NORMAL_ARRAY)
            
    def cleanup(self):
        if self.vbo: self.vbo.delete()
        if self.element_buffer: self.element_buffer.delete()