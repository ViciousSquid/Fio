# editor/obj_loader.py
import os
import numpy as np
from OpenGL.GL import *
from OpenGL.arrays import vbo

class OBJ:
    """
    A more robust loader for .obj files that handles vertex normals and uses a single
    interleaved VBO for efficient rendering. It also normalizes the model's size.
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
        
        temp_vertices = []
        temp_normals = []
        face_data = []

        try:
            # First pass: read all data from the file
            with open(filename, "r") as f:
                for line in f:
                    if line.startswith('#'): continue
                    values = line.split()
                    if not values: continue
                    
                    if values[0] == 'v':
                        temp_vertices.append(list(map(float, values[1:4])))
                    elif values[0] == 'vn':
                        temp_normals.append(list(map(float, values[1:3])))
                    elif values[0] == 'f':
                        face_row = []
                        for v in values[1:]:
                            try:
                                w = v.split('/')
                                # Ensure we handle faces without normals gracefully
                                v_idx = int(w[0]) - 1
                                n_idx = int(w[2]) - 1 if len(w) > 2 and w[2] else -1
                                face_row.append((v_idx, n_idx))
                            except (ValueError, IndexError):
                                continue
                        face_data.append(face_row)

            if not temp_vertices or not face_data:
                print(f"Warning: No vertex or face data loaded from {filename}")
                return

            # --- Model Normalization ---
            # 1. Calculate the bounding box and center of the raw vertices
            min_coord = np.min(temp_vertices, axis=0)
            max_coord = np.max(temp_vertices, axis=0)
            center = (min_coord + max_coord) / 2.0
            size = max_coord - min_coord
            
            # 2. Determine the scaling factor to fit the model into a 1x1x1 cube
            scale_factor = 1.0 / np.max(size)

            # 3. Apply the transformation to a new list of vertices
            normalized_vertices = [((v - center) * scale_factor).tolist() for v in temp_vertices]
            
            # --- VBO Preparation ---
            # Re-index the vertices to create the final interleaved VBO data
            final_vbo_data = []
            final_indices = []
            vertex_map = {}
            
            for face in face_data:
                # Triangulate polygon faces (e.g., quads)
                for i in range(1, len(face) - 1):
                    indices = [face[0], face[i], face[i+1]]
                    for v_idx, n_idx in indices:
                        if (v_idx, n_idx) not in vertex_map:
                            vertex_map[(v_idx, n_idx)] = len(final_vbo_data)
                            
                            pos = normalized_vertices[v_idx]
                            norm = temp_normals[n_idx] if n_idx != -1 and n_idx < len(temp_normals) else [0, 1, 0] # Use a default up-vector if no normal
                            
                            final_vbo_data.append(pos + norm)
                        
                        final_indices.append(vertex_map[(v_idx, n_idx)])
            
            if not final_vbo_data or not final_indices:
                print(f"Warning: No valid data generated for VBOs from {filename}")
                return

            # Create VBO from the final, interleaved data
            vertex_data = np.array(final_vbo_data, dtype=np.float32)
            index_data = np.array(final_indices, dtype=np.uint32)

            self.vbo = vbo.VBO(vertex_data)
            self.element_buffer = vbo.VBO(index_data, target=GL_ELEMENT_ARRAY_BUFFER)
            self.vertex_count = len(final_indices)

            print(f"Successfully normalized and loaded {filename} into VBOs.")
            self.is_loaded = True

        except Exception as e:
            import traceback
            print(f"Error processing OBJ file '{filename}': {e}")
            traceback.print_exc()
            self.is_loaded = False

    def render(self):
        """Renders the loaded model using VBOs."""
        if not self.is_loaded:
            return

        try:
            self.vbo.bind()
            self.element_buffer.bind()
            
            # Stride is 6 floats (3 for position, 3 for normal)
            stride = 24 # 6 floats * 4 bytes/float
            
            # Set up the pointers for position and normals
            glEnableClientState(GL_VERTEX_ARRAY)
            glVertexPointer(3, GL_FLOAT, stride, self.vbo)
            
            glEnableClientState(GL_NORMAL_ARRAY)
            glNormalPointer(GL_FLOAT, stride, self.vbo + 12) # Offset by 3 floats (12 bytes)

            # Draw the elements
            glDrawElements(GL_TRIANGLES, self.vertex_count, GL_UNSIGNED_INT, None)

        finally:
            # Unbind buffers and disable client states
            self.element_buffer.unbind()
            self.vbo.unbind()
            glDisableClientState(GL_VERTEX_ARRAY)
            glDisableClientState(GL_NORMAL_ARRAY)