import glm
import numpy as np
import OpenGL.GL as gl
import ctypes
from editor.things import Thing, Light
from engine import shaders
from OpenGL.GL.shaders import compileProgram, compileShader
from engine.constants import RENDER_MODE_LIT, RENDER_MODE_UNLIT, RENDER_MODE_WIREFRAME, RENDER_MODE_VERTEX
from PIL import Image
import os
import time

class Renderer:
    """Handles all modern OpenGL drawing operations for the editor."""

    def __init__(self, texture_loader, initial_grid_size, initial_world_size):
        self.texture_manager = {}
        self.load_texture_callback = texture_loader

        # 1. Compile Shaders
        try:
            shader_simple = compileProgram(compileShader(shaders.VERTEX_SHADER_SIMPLE, gl.GL_VERTEX_SHADER), compileShader(shaders.FRAGMENT_SHADER_SIMPLE, gl.GL_FRAGMENT_SHADER))
            shader_lit = compileProgram(compileShader(shaders.VERTEX_SHADER_LIT, gl.GL_VERTEX_SHADER), compileShader(shaders.FRAGMENT_SHADER_LIT, gl.GL_FRAGMENT_SHADER))
            shader_textured = compileProgram(compileShader(shaders.VERTEX_SHADER_TEXTURED, gl.GL_VERTEX_SHADER), compileShader(shaders.FRAGMENT_SHADER_TEXTURED, gl.GL_FRAGMENT_SHADER))
            shader_sprite = compileProgram(compileShader(shaders.VERTEX_SHADER_SPRITE, gl.GL_VERTEX_SHADER), compileShader(shaders.FRAGMENT_SHADER_SPRITE, gl.GL_FRAGMENT_SHADER))
            shader_shadow_volume = compileProgram(compileShader(shaders.SHADOW_VOLUME_VERTEX_SHADER, gl.GL_VERTEX_SHADER), compileShader(shaders.SHADOW_VOLUME_FRAGMENT_SHADER, gl.GL_FRAGMENT_SHADER))
            shader_fog = compileProgram(compileShader(shaders.VERTEX_SHADER_FOG, gl.GL_VERTEX_SHADER), compileShader(shaders.FRAGMENT_SHADER_FOG, gl.GL_FRAGMENT_SHADER))
            self.shaders = {
                'simple': shader_simple,
                'lit': shader_lit,
                'textured': shader_textured,
                'sprite': shader_sprite,
                'shadow_volume': shader_shadow_volume,
                'fog': shader_fog,
            }
        except Exception as e:
            print(f"FATAL: Shader Compilation Error: {e}")
            return
        
        # 2. Create Vertex Buffers (VAOs)
        self.vaos = {
            'cube': self._create_cube_vao(),
            'sprite': self._create_sprite_vao(),
            'grid': None,
        }
        self.grid_indices_count = 0
        self._create_gizmo_buffers()
        self.update_grid_buffers(initial_world_size, initial_grid_size)

        # 3. Load Essential Textures
        self.noise_texture_id = self._load_3d_texture('assets/noise_3d.bin')
        self.sprite_textures = {}
        self.load_texture('default.png', 'textures')
        self.load_texture('caulk', 'textures')

    def update_grid_buffers(self, world_size, grid_size):
        """Creates or updates the grid VAO."""
        if grid_size <= 0:
            if self.vaos['grid']:
                gl.glDeleteVertexArrays(1, [self.vaos['grid']])
                self.vaos['grid'] = None
            return

        s, g = world_size, grid_size
        lines = [[-s, 0, i, s, 0, i, i, 0, -s, i, 0, s] for i in range(-s, s + 1, g)]
        grid_vertices = np.array(lines, dtype=np.float32).flatten()
        self.grid_indices_count = len(grid_vertices) // 3
        
        if self.vaos['grid']:
            gl.glDeleteVertexArrays(1, [self.vaos['grid']])
        
        vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(vao)
        vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, grid_vertices.nbytes, grid_vertices, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        gl.glBindVertexArray(0)
        self.vaos['grid'] = vao
    
    def set_sprite_textures(self, textures):
        self.sprite_textures = textures

    def load_texture(self, texture_name, subfolder):
        tex_cache_name = os.path.join(subfolder, texture_name)
        if tex_cache_name in self.texture_manager: return self.texture_manager[tex_cache_name]
        if texture_name == 'default.png':
            tex_id = gl.glGenTextures(1); self.texture_manager[tex_cache_name] = tex_id
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id); pixels = [255, 255, 255, 255]
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, 1, 1, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, (gl.GLubyte * 4)(*pixels))
            return tex_id
        if texture_name == 'caulk':
            tex_id = gl.glGenTextures(1); self.texture_manager[tex_cache_name] = tex_id
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id); pixels = [255,0,255,255, 0,0,0,255, 0,0,0,255, 255,0,255,255]
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, 2, 2, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, (gl.GLubyte * 16)(*pixels))
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            return tex_id
        texture_path = os.path.join('assets', subfolder, texture_name)
        if not os.path.exists(texture_path): return self.load_texture('default.png', 'textures')
        try:
            img = Image.open(texture_path).convert("RGBA"); img_data = img.tobytes()
            tex_id = gl.glGenTextures(1); self.texture_manager[tex_cache_name] = tex_id
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT); gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR); gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, img.width, img.height, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, img_data)
            gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
            return tex_id
        except Exception as e: print(f"Error loading texture '{texture_name}': {e}"); return self.load_texture('default.png', 'textures')

    def _load_3d_texture(self, filepath, size=32):
        try:
            with open(filepath, 'rb') as f:
                data = f.read()
            
            if len(data) != size * size * size:
                print(f"Error: 3D texture data size mismatch in {filepath}. Expected {size*size*size} bytes, got {len(data)}.")
                return 0

            texture_id = gl.glGenTextures(1)
            gl.glBindTexture(gl.GL_TEXTURE_3D, texture_id)
            gl.glTexParameteri(gl.GL_TEXTURE_3D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_3D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_3D, gl.GL_TEXTURE_WRAP_R, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_3D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_3D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexImage3D(gl.GL_TEXTURE_3D, 0, gl.GL_R8, size, size, size, 0, gl.GL_RED, gl.GL_UNSIGNED_BYTE, data)
            return texture_id
        except FileNotFoundError:
            print(f"Error: 3D noise texture not found at '{filepath}'. Please run noise_generator.py.")
            return 0
        except Exception as e:
            print(f"An error occurred loading the 3D texture: {e}")
            return 0
    
    def render_scene(self, projection, view, camera_pos, brushes, things, selected_object, config):
        """Main entry point to render a complete scene."""
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LESS)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)
        gl.glEnable(gl.GL_PROGRAM_POINT_SIZE) # Enable resizing points for Vertex mode

        # --- Determine Render Mode ---
        # Default to Lit if not specified
        current_mode = config.get('render_mode', RENDER_MODE_LIT)

        # Handle global Polygon Modes (Wireframe/Vertex)
        if current_mode == RENDER_MODE_WIREFRAME:
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)
        elif current_mode == RENDER_MODE_VERTEX:
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_POINT)
            gl.glPointSize(4.0) # Make vertices visible
        else:
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)

        # Draw Grid (Always visible unless explicitly hidden, handled internally)
        self.draw_grid(projection, view, self.grid_indices_count)

        # --- Prepare object lists ---
        opaque_brushes, transparent_brushes, sprites, fog_volumes = self._sort_objects(brushes, things, config)
        
        lights = [t for t in things if isinstance(t, Light) and t.properties.get('state', 'on') == 'on']

        # --- 1. Opaque Pass ---
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_BLEND)
        
        # Select drawing method based on mode
        if current_mode == RENDER_MODE_UNLIT:
            # Fullbright (Textured, no lighting calculations)
            self.draw_textured_brushes(projection, view, opaque_brushes, lights, config)
        elif current_mode == RENDER_MODE_LIT:
            # Phong (Lit)
            self.draw_lit_brushes(projection, view, opaque_brushes, lights, config)
        else:
            # For Wireframe/Vertex, we generally want a simple color or the Lit shader without filling
            # Using 'Lit' shader usually looks best for wireframe geometry
            self.draw_lit_brushes(projection, view, opaque_brushes, lights, config)

        # --- Shadow Pass (Only relevant in Lit mode) ---
        if current_mode == RENDER_MODE_LIT:
            shadow_casting_lights = [light for light in lights if light.properties.get('casts_shadows')]
            if shadow_casting_lights:
                self.render_shadows(projection, view, opaque_brushes, shadow_casting_lights)

        # --- 2. Transparent Pass ---
        # Sort back-to-front
        transparent_brushes.sort(key=lambda b: -glm.distance(glm.vec3(b['pos']), camera_pos))
        sprites.sort(key=lambda s: -glm.distance(glm.vec3(s.pos), camera_pos))
        
        gl.glEnable(gl.GL_BLEND)
        gl.glDepthMask(gl.GL_FALSE)

        # Draw Sprites (Always unlit/textured usually)
        self.draw_sprites(projection, view, sprites, self.sprite_textures)
        
        # Transparent brushes
        if current_mode == RENDER_MODE_UNLIT:
            self.draw_textured_brushes(projection, view, transparent_brushes, lights, config)
        else:
            self.draw_lit_brushes(projection, view, transparent_brushes, lights, config, is_transparent_pass=True)

        if current_mode == RENDER_MODE_LIT:
            self.draw_fog_volumes(projection, view, fog_volumes, lights, camera_pos, config)

        # --- 3. Overlays ---
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_DEPTH_TEST)
        
        # Ensure we return to Fill mode for Gizmos/UI if we were in Wireframe
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)

        if selected_object:
             # Logic for selection outlines remains...
             if isinstance(selected_object, dict):
                 self.draw_selected_brush_outline(projection, view, selected_object)
                 if not selected_object.get('lock', False):
                     self.render_gizmo(projection, view, selected_object['pos'])
             elif isinstance(selected_object, Thing):
                 self.render_gizmo(projection, view, selected_object.pos)

        # --- Reset GL State ---
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glUseProgram(0)

    def draw_fog_volumes(self, projection, view, brushes, lights, camera_pos, config):
        """Draws brushes as fog volumes."""
        if not brushes:
            return

        # Set the correct blending mode for transparency
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)

        shader = self.shaders['fog']
        gl.glUseProgram(shader)

        # Set shader uniforms that are the same for all fog brushes
        self._set_light_uniforms(shader, lights)
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "projection"), 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "view"), 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform3fv(gl.glGetUniformLocation(shader, "viewPos"), 1, glm.value_ptr(camera_pos))
        gl.glUniform1f(gl.glGetUniformLocation(shader, "time"), config.get('time', 0.0))

        # Bind the 3D noise texture to texture unit 1
        gl.glActiveTexture(gl.GL_TEXTURE1)
        gl.glBindTexture(gl.GL_TEXTURE_3D, self.noise_texture_id)
        gl.glUniform1i(gl.glGetUniformLocation(shader, "noiseTexture"), 1)

        gl.glBindVertexArray(self.vaos['cube'])

        # Render the fog cube in two passes for correct transparency
        gl.glEnable(gl.GL_CULL_FACE)

        for brush in brushes:
            model_matrix = glm.translate(glm.mat4(1.0), glm.vec3(brush['pos'])) * glm.scale(glm.mat4(1.0), glm.vec3(brush['size']))
            gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "model"), 1, gl.GL_FALSE, glm.value_ptr(model_matrix))

            density = brush.get('fog_density', 0.01)
            fog_color = brush.get('fog_color', [0.5, 0.6, 0.7])
            noise_scale = brush.get('fog_noise_scale', 0.01)

            gl.glUniform1f(gl.glGetUniformLocation(shader, "density"), density)
            gl.glUniform3fv(gl.glGetUniformLocation(shader, "fogColor"), 1, fog_color)
            gl.glUniform1f(gl.glGetUniformLocation(shader, "noiseScale"), noise_scale)

            # 1. First Pass: Draw the back faces of the cube
            gl.glCullFace(gl.GL_FRONT)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)

            # 2. Second Pass: Draw the front faces of the cube
            gl.glCullFace(gl.GL_BACK)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)

        # Restore default culling state
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glBindVertexArray(0)
        # This line must be inside the function
        gl.glActiveTexture(gl.GL_TEXTURE0) # Reset active texture unit

    def render_shadows(self, projection, view, brushes, lights):
        gl.glEnable(gl.GL_STENCIL_TEST)
        gl.glEnable(gl.GL_DEPTH_CLAMP)
        gl.glDisable(gl.GL_CULL_FACE)

        for light in lights:
            gl.glClear(gl.GL_STENCIL_BUFFER_BIT)
            
            gl.glColorMask(gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)
            gl.glDepthMask(gl.GL_FALSE)
            gl.glStencilFunc(gl.GL_ALWAYS, 0, 0xFF)
            gl.glStencilOpSeparate(gl.GL_BACK, gl.GL_KEEP, gl.GL_INCR_WRAP, gl.GL_KEEP)
            gl.glStencilOpSeparate(gl.GL_FRONT, gl.GL_KEEP, gl.GL_DECR_WRAP, gl.GL_KEEP)

            shader = self.shaders['shadow_volume']
            gl.glUseProgram(shader)
            gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "projection"), 1, gl.GL_FALSE, glm.value_ptr(projection))
            gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "view"), 1, gl.GL_FALSE, glm.value_ptr(view))
            
            light_pos_vec3 = glm.vec3(light.pos)
            gl.glUniform3fv(gl.glGetUniformLocation(shader, "light_pos"), 1, glm.value_ptr(light_pos_vec3))
            
            gl.glBindVertexArray(self.vaos['cube'])
            shadow_casters = [b for b in brushes if not b.get('is_trigger', False)]
            for brush in shadow_casters:
                model_matrix = glm.translate(glm.mat4(1.0), glm.vec3(brush['pos'])) * glm.scale(glm.mat4(1.0), glm.vec3(brush['size']))
                gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "model"), 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
            
            gl.glDepthMask(gl.GL_TRUE)
            gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
            gl.glStencilFunc(gl.GL_NOTEQUAL, 0, 0xFF)
            gl.glStencilOp(gl.GL_KEEP, gl.GL_KEEP, gl.GL_KEEP)

            gl.glEnable(gl.GL_BLEND)
            gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
            gl.glDepthFunc(gl.GL_LEQUAL)

            shader = self.shaders['lit']
            gl.glUseProgram(shader)
            gl.glUniform1i(gl.glGetUniformLocation(shader, "active_lights"), 0)
            gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "projection"), 1, gl.GL_FALSE, glm.value_ptr(projection))
            gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "view"), 1, gl.GL_FALSE, glm.value_ptr(view))

            gl.glBindVertexArray(self.vaos['cube'])
            for brush in shadow_casters:
                model_matrix = glm.translate(glm.mat4(1.0), glm.vec3(brush['pos'])) * glm.scale(glm.mat4(1.0), glm.vec3(brush['size']))
                gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "model"), 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
                gl.glUniform3fv(gl.glGetUniformLocation(shader, "object_color"), 1, [0.0, 0.0, 0.0])
                gl.glUniform1f(gl.glGetUniformLocation(shader, "alpha"), 0.5)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
                
            gl.glDepthFunc(gl.GL_LESS)
            gl.glDisable(gl.GL_BLEND)

        gl.glDisable(gl.GL_STENCIL_TEST)
        gl.glDisable(gl.GL_DEPTH_CLAMP)

    def _sort_objects(self, brushes, things, config):
        """Sorts scene objects into opaque, transparent, and sprite lists."""
        opaque_brushes, transparent_brushes, sprites, fog_volumes = [], [], [], []
        is_play_mode = config.get('play_mode', False)
        show_sprites_in_play_mode = config.get('show_sprites_in_play_mode', False)

        for brush in brushes:
            if brush.get('hidden', False): continue
            if brush.get('is_fog', False): fog_volumes.append(brush)
            elif brush.get('is_trigger', False):
                if not is_play_mode: transparent_brushes.append(brush)
            else: opaque_brushes.append(brush)
        
        if not is_play_mode or show_sprites_in_play_mode:
            sprites.extend([t for t in things if isinstance(t, Thing)])
            
        return opaque_brushes, transparent_brushes, sprites, fog_volumes

    def draw_grid(self, projection, view, grid_indices_count):
        shader = self.shaders['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "projection"), 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "view"), 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "model"), 1, gl.GL_FALSE, glm.value_ptr(glm.mat4(1.0)))
        gl.glUniform3f(gl.glGetUniformLocation(shader, "color"), 0.2, 0.2, 0.2)
        
        gl.glBindVertexArray(self.vaos['grid'])
        gl.glDrawArrays(gl.GL_LINES, 0, grid_indices_count)
        gl.glBindVertexArray(0)

    def draw_lit_brushes(self, projection, view, brushes, lights, config, is_transparent_pass=False):
        if not brushes: return
        shader = self.shaders['lit']
        gl.glUseProgram(shader)

        self._set_light_uniforms(shader, lights)
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "projection"), 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "view"), 1, gl.GL_FALSE, glm.value_ptr(view))
        
        gl.glBindVertexArray(self.vaos['cube'])
        
        display_mode = config.get('brush_display_mode', 'Textured')
        show_triggers_solid = config.get('show_triggers_as_solid', False)

        for brush in brushes:
            if is_transparent_pass:
                gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL if show_triggers_solid else gl.GL_LINE)
            else:
                gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL if display_mode != "Wireframe" else gl.GL_LINE)

            model_matrix = glm.translate(glm.mat4(1.0), glm.vec3(brush['pos'])) * glm.scale(glm.mat4(1.0), glm.vec3(brush['size']))
            gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "model"), 1, gl.GL_FALSE, glm.value_ptr(model_matrix))

            is_selected, is_subtract = (brush is config.get('selected_object')), (brush.get('operation') == 'subtract')
            color, alpha = [0.8, 0.8, 0.8], 1.0
            if brush.get('is_trigger', False): color, alpha = [0.0, 1.0, 1.0], 0.3
            elif is_selected: color = [1.0, 1.0, 0.0]
            elif is_subtract: color = [1.0, 0.0, 0.0]

            gl.glUniform3fv(gl.glGetUniformLocation(shader, "object_color"), 1, color)
            gl.glUniform1f(gl.glGetUniformLocation(shader, "alpha"), alpha)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)

        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl.glBindVertexArray(0)

    def draw_textured_brushes(self, projection, view, brushes, lights, config):
        if not brushes: return
        shader = self.shaders['textured']
        gl.glUseProgram(shader)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)

        self._set_light_uniforms(shader, lights)
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "projection"), 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "view"), 1, gl.GL_FALSE, glm.value_ptr(view))

        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(gl.glGetUniformLocation(shader, "texture_diffuse"), 0)

        gl.glBindVertexArray(self.vaos['cube'])
        show_caulk = config.get('show_caulk', True)
        face_keys = ['south', 'north', 'west', 'east', 'bottom', 'top']

        for brush in brushes:
            model_matrix = glm.translate(glm.mat4(1.0), glm.vec3(brush['pos'])) * glm.scale(glm.mat4(1.0), glm.vec3(brush['size']))
            gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "model"), 1, gl.GL_FALSE, glm.value_ptr(model_matrix))

            textures = brush.get('textures', {})
            for i, face_key in enumerate(face_keys):
                tex_name = textures.get(face_key, 'default.png')
                if tex_name == 'caulk.jpg':
                    continue # Skip rendering this face
                tex_id = self.load_texture_callback(tex_name, 'textures')
                gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                gl.glDrawArrays(gl.GL_TRIANGLES, i * 6, 6)
        gl.glBindVertexArray(0)

    def draw_selected_brush_outline(self, projection, view, brush):
        shader = self.shaders['simple']
        gl.glUseProgram(shader)

        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "projection"), 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "view"), 1, gl.GL_FALSE, glm.value_ptr(view))
        model_matrix = glm.translate(glm.mat4(1.0), glm.vec3(brush['pos'])) * glm.scale(glm.mat4(1.0), glm.vec3(brush['size']))
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "model"), 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
        gl.glUniform3f(gl.glGetUniformLocation(shader, "color"), 1.0, 1.0, 0.0)

        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)
        gl.glLineWidth(1)
        
        gl.glBindVertexArray(self.vaos['cube'])
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
        
        gl.glLineWidth(1)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl.glBindVertexArray(0)

    def draw_sprites(self, projection, view, things_to_draw, sprite_textures):
        if not things_to_draw: return
        shader = self.shaders['sprite']
        gl.glUseProgram(shader)

        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "projection"), 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "view"), 1, gl.GL_FALSE, glm.value_ptr(view))
        
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(gl.glGetUniformLocation(shader, "sprite_texture"), 0)
        
        gl.glBindVertexArray(self.vaos['sprite'])
        for thing in things_to_draw:
            thing_type = thing.__class__.__name__
            if thing_type in sprite_textures:
                gl.glBindTexture(gl.GL_TEXTURE_2D, sprite_textures[thing_type])
                gl.glUniform3fv(gl.glGetUniformLocation(shader, "sprite_pos_world"), 1, thing.pos)
                size = 16.0 if isinstance(thing, Light) else 32.0
                gl.glUniform2f(gl.glGetUniformLocation(shader, "sprite_size"), size, size)
                gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)
        gl.glBindVertexArray(0)

    def _set_light_uniforms(self, shader, lights):
        gl.glUniform1i(gl.glGetUniformLocation(shader, "active_lights"), len(lights))
        for i, light in enumerate(lights):
            gl.glUniform3fv(gl.glGetUniformLocation(shader, f"lights[{i}].position"), 1, light.pos)
            gl.glUniform3fv(gl.glGetUniformLocation(shader, f"lights[{i}].color"), 1, light.get_color())
            gl.glUniform1f(gl.glGetUniformLocation(shader, f"lights[{i}].intensity"), light.get_intensity())
            gl.glUniform1f(gl.glGetUniformLocation(shader, f"lights[{i}].radius"), light.get_radius())

    def _create_gizmo_buffers(self):
        axis_verts = np.array([0,0,0, 1,0,0, 0,0,0, 0,1,0, 0,0,0, 0,0,1], dtype=np.float32)
        self.vao_gizmo_lines = gl.glGenVertexArrays(1)
        vbo_gizmo_lines = gl.glGenBuffers(1)
        gl.glBindVertexArray(self.vao_gizmo_lines)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo_gizmo_lines)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, axis_verts.nbytes, axis_verts, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 12, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)

        cone_verts, num_segments, radius, height = [], 12, 0.05, 0.2
        for i in range(num_segments):
            theta1, theta2 = (i/num_segments)*2*np.pi, ((i+1)/num_segments)*2*np.pi
            cone_verts.extend([0,0,0, np.cos(theta2)*radius,0,np.sin(theta2)*radius, np.cos(theta1)*radius,0,np.sin(theta1)*radius])
            cone_verts.extend([0,height,0, np.cos(theta1)*radius,0,np.sin(theta1)*radius, np.cos(theta2)*radius,0,np.sin(theta2)*radius])

        self.gizmo_cone_v_count = len(cone_verts) // 3
        cone_verts = np.array(cone_verts, dtype=np.float32)
        
        self.vao_gizmo_cone = gl.glGenVertexArrays(1)
        vbo_gizmo_cone = gl.glGenBuffers(1)
        gl.glBindVertexArray(self.vao_gizmo_cone)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo_gizmo_cone)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, cone_verts.nbytes, cone_verts, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)

        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glBindVertexArray(0)

    def render_gizmo(self, projection, view, position):
        shader = self.shaders['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "projection"), 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "view"), 1, gl.GL_FALSE, glm.value_ptr(view))
        
        base_model = glm.translate(glm.mat4(1.0), position) * glm.scale(glm.mat4(1.0), glm.vec3(32.0))

        gl.glLineWidth(1)
        gl.glBindVertexArray(self.vao_gizmo_lines)
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "model"), 1, gl.GL_FALSE, glm.value_ptr(base_model))
        gl.glUniform3f(gl.glGetUniformLocation(shader, "color"), 1, 0, 0)
        gl.glDrawArrays(gl.GL_LINES, 0, 2)
        gl.glUniform3f(gl.glGetUniformLocation(shader, "color"), 0, 1, 0)
        gl.glDrawArrays(gl.GL_LINES, 2, 2)
        gl.glUniform3f(gl.glGetUniformLocation(shader, "color"), 0, 0, 1)
        gl.glDrawArrays(gl.GL_LINES, 4, 2)
        gl.glLineWidth(1)

        gl.glBindVertexArray(self.vao_gizmo_cone)
        model_x = base_model * glm.translate(glm.mat4(1.0), glm.vec3(1,0,0)) * glm.rotate(glm.mat4(1.0), glm.radians(-90), glm.vec3(0,0,1))
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "model"), 1, gl.GL_FALSE, glm.value_ptr(model_x))
        gl.glUniform3f(gl.glGetUniformLocation(shader, "color"), 1, 0, 0)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, self.gizmo_cone_v_count)

        model_y = base_model * glm.translate(glm.mat4(1.0), glm.vec3(0,1,0))
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "model"), 1, gl.GL_FALSE, glm.value_ptr(model_y))
        gl.glUniform3f(gl.glGetUniformLocation(shader, "color"), 0, 1, 0)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, self.gizmo_cone_v_count)
        
        model_z = base_model * glm.translate(glm.mat4(1.0), glm.vec3(0,0,1)) * glm.rotate(glm.mat4(1.0), glm.radians(90), glm.vec3(1,0,0))
        gl.glUniformMatrix4fv(gl.glGetUniformLocation(shader, "model"), 1, gl.GL_FALSE, glm.value_ptr(model_z))
        gl.glUniform3f(gl.glGetUniformLocation(shader, "color"), 0, 0, 1)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, self.gizmo_cone_v_count)

        gl.glBindVertexArray(0)

    def _create_cube_vao(self):
        # fmt: off
        vertices = np.array([
            # Positions           # Normals           # Tex Coords
            # Back Face (-Z) - South
            -0.5, -0.5, -0.5,  0.0,  0.0, -1.0,  0.0, 0.0,
             0.5, -0.5, -0.5,  0.0,  0.0, -1.0,  1.0, 0.0,
             0.5,  0.5, -0.5,  0.0,  0.0, -1.0,  1.0, 1.0,
             0.5,  0.5, -0.5,  0.0,  0.0, -1.0,  1.0, 1.0,
            -0.5,  0.5, -0.5,  0.0,  0.0, -1.0,  0.0, 1.0,
            -0.5, -0.5, -0.5,  0.0,  0.0, -1.0,  0.0, 0.0,
            # Front Face (+Z) - North
            -0.5, -0.5,  0.5,  0.0,  0.0,  1.0,  0.0, 0.0,
             0.5,  0.5,  0.5,  0.0,  0.0,  1.0,  1.0, 1.0,
             0.5, -0.5,  0.5,  0.0,  0.0,  1.0,  1.0, 0.0,
             0.5,  0.5,  0.5,  0.0,  0.0,  1.0,  1.0, 1.0,
            -0.5, -0.5,  0.5,  0.0,  0.0,  1.0,  0.0, 0.0,
            -0.5,  0.5,  0.5,  0.0,  0.0,  1.0,  0.0, 1.0,
            # Left Face (-X) - West
            -0.5,  0.5,  0.5, -1.0,  0.0,  0.0,  1.0, 0.0,
            -0.5, -0.5, -0.5, -1.0,  0.0,  0.0,  0.0, 1.0,
            -0.5,  0.5, -0.5, -1.0,  0.0,  0.0,  1.0, 1.0,
            -0.5, -0.5, -0.5, -1.0,  0.0,  0.0,  0.0, 1.0,
            -0.5,  0.5,  0.5, -1.0,  0.0,  0.0,  1.0, 0.0,
            -0.5, -0.5,  0.5, -1.0,  0.0,  0.0,  0.0, 0.0,
            # Right Face (+X) - East
             0.5,  0.5,  0.5,  1.0,  0.0,  0.0,  1.0, 0.0,
             0.5,  0.5, -0.5,  1.0,  0.0,  0.0,  1.0, 1.0,
             0.5, -0.5, -0.5,  1.0,  0.0,  0.0,  0.0, 1.0,
             0.5, -0.5, -0.5,  1.0,  0.0,  0.0,  0.0, 1.0,
             0.5, -0.5,  0.5,  1.0,  0.0,  0.0,  0.0, 0.0,
             0.5,  0.5,  0.5,  1.0,  0.0,  0.0,  1.0, 0.0,
            # Bottom Face (-Y)
            -0.5, -0.5, -0.5,  0.0, -1.0,  0.0,  0.0, 1.0,
             0.5, -0.5,  0.5,  0.0, -1.0,  0.0,  1.0, 0.0,
             0.5, -0.5, -0.5,  0.0, -1.0,  0.0,  1.0, 1.0,
             0.5, -0.5,  0.5,  0.0, -1.0,  0.0,  1.0, 0.0,
            -0.5, -0.5, -0.5,  0.0, -1.0,  0.0,  0.0, 1.0,
            -0.5, -0.5,  0.5,  0.0, -1.0,  0.0,  0.0, 0.0,
            # Top Face (+Y)
            -0.5,  0.5, -0.5,  0.0,  1.0,  0.0,  0.0, 1.0,
             0.5,  0.5, -0.5,  0.0,  1.0,  0.0,  1.0, 1.0,
             0.5,  0.5,  0.5,  0.0,  1.0,  0.0,  1.0, 0.0,
             0.5,  0.5,  0.5,  0.0,  1.0,  0.0,  1.0, 0.0,
            -0.5,  0.5,  0.5,  0.0,  1.0,  0.0,  0.0, 0.0,
            -0.5,  0.5, -0.5,  0.0,  1.0,  0.0,  0.0, 1.0
        ], dtype=np.float32)
        # fmt: on
        vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(vao)
        vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, vertices.nbytes, vertices, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(12))
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(2, 2, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(24))
        gl.glEnableVertexAttribArray(2)
        gl.glBindVertexArray(0)
        return vao

    def _create_sprite_vao(self):
        vertices = np.array([-0.5, -0.5, 0.5, -0.5, -0.5, 0.5, 0.5, 0.5], dtype=np.float32)
        vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(vao)
        vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, vertices.nbytes, vertices, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        gl.glBindVertexArray(0)
        return vao

    def _create_gizmo_buffers(self):
        axis_verts = np.array([0,0,0, 1,0,0, 0,0,0, 0,1,0, 0,0,0, 0,0,1], dtype=np.float32)
        self.vao_gizmo_lines = gl.glGenVertexArrays(1)
        vbo_gizmo_lines = gl.glGenBuffers(1)
        gl.glBindVertexArray(self.vao_gizmo_lines)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo_gizmo_lines)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, axis_verts.nbytes, axis_verts, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 12, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)

        cone_verts, num_segments, radius, height = [], 12, 0.05, 0.2
        for i in range(num_segments):
            theta1, theta2 = (i/num_segments)*2*np.pi, ((i+1)/num_segments)*2*np.pi
            cone_verts.extend([0,0,0, np.cos(theta2)*radius,0,np.sin(theta2)*radius, np.cos(theta1)*radius,0,np.sin(theta1)*radius])
            cone_verts.extend([0,height,0, np.cos(theta1)*radius,0,np.sin(theta1)*radius, np.cos(theta2)*radius,0,np.sin(theta2)*radius])

        self.gizmo_cone_v_count = len(cone_verts) // 3
        cone_verts = np.array(cone_verts, dtype=np.float32)
        
        self.vao_gizmo_cone = gl.glGenVertexArrays(1)
        vbo_gizmo_cone = gl.glGenBuffers(1)
        gl.glBindVertexArray(self.vao_gizmo_cone)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo_gizmo_cone)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, cone_verts.nbytes, cone_verts, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)

        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)
        gl.glBindVertexArray(0)