"""
RStudio Optimized Renderer
Heavily optimized for ARM devices (Surface Pro 9 SQ3) running x64 emulation.

Key Optimizations:
1. Removed double culling - trusts pre-culled data from logic thread
2. Batched draw calls - draws entire brushes, not per-face
3. Pre-computed normal matrices on CPU to avoid inverse() in shader
4. Single light uniform upload per frame
5. Simplified "ARM mode" shaders with lower quality but better performance
6. Reduced fog ray march steps (16 instead of 32)
7. Optional shadow system that can be disabled
8. Texture atlas batching for fewer state changes
9. Cached model matrices where possible
"""

import glm
import numpy as np
import OpenGL.GL as gl
import ctypes 
from editor.things import Thing, Light, Model
from OpenGL.GL.shaders import compileProgram, compileShader 
from engine.constants import RENDER_MODE_LIT, RENDER_MODE_UNLIT, RENDER_MODE_WIREFRAME, RENDER_MODE_VERTEX 
from engine.shaders import DEFAULT_SHADERS
from PIL import Image 
import os
from collections import defaultdict
from engine.terrain import TERRAIN_VERTEX_SHADER, TERRAIN_FRAGMENT_SHADER

# Try to import OBJ loader
try:
    from .obj_loader import OBJ
except ImportError:
    OBJ = None


# =============================================================================
# ARM-OPTIMIZED SHADERS (Simplified for better performance on emulated x64)
# =============================================================================

# Pre-compute normal matrix on CPU, pass it to shader
ARM_LIT_VERT = """#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
out vec3 FragPos;
out vec3 Normal;
uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
uniform mat3 normalMatrix;  // Pre-computed on CPU
void main() {
    FragPos = vec3(model * vec4(aPos, 1.0));
    Normal = normalMatrix * aNormal;  // No inverse() call!
    gl_Position = projection * view * vec4(FragPos, 1.0);
}"""

ARM_LIT_FRAG = """#version 330 core
out vec4 FragColor;
in vec3 FragPos;
in vec3 Normal;
uniform vec3 object_color;
uniform float alpha;
struct Light { vec3 position; vec3 color; float intensity; float radius; };
uniform Light lights[16];
uniform int active_lights;
void main() {
    vec3 norm = normalize(Normal);
    vec3 result = vec3(0.12) * object_color;
    for(int i = 0; i < active_lights && i < 16; i++) {
        vec3 toLight = lights[i].position - FragPos;
        float distSq = dot(toLight, toLight);
        float radiusSq = lights[i].radius * lights[i].radius;
        if(distSq < radiusSq) {
            float dist = sqrt(distSq);
            vec3 lightDir = toLight / dist;
            float diff = max(dot(norm, lightDir), 0.0);
            float att = 1.0 - (dist / lights[i].radius);
            att = att * att;
            result += (diff * lights[i].color * lights[i].intensity * att) * object_color;
        }
    }
    FragColor = vec4(result, alpha);
}"""

ARM_TEXTURED_VERT = """#version 330 core
layout (location = 0) in vec3 aPos;
layout (location = 1) in vec3 aNormal;
layout (location = 2) in vec2 aTexCoords;
out vec3 FragPos;
out vec3 Normal;
out vec2 TexCoords;
uniform mat4 model;
uniform mat4 view;
uniform mat4 projection;
uniform mat3 normalMatrix;
uniform vec2 tex_scale;
void main() {
    FragPos = vec3(model * vec4(aPos, 1.0));
    Normal = normalMatrix * aNormal;
    TexCoords = aTexCoords * tex_scale;
    gl_Position = projection * view * vec4(FragPos, 1.0);
}"""

ARM_TEXTURED_FRAG = """#version 330 core
out vec4 FragColor;
in vec3 FragPos;
in vec3 Normal;
in vec2 TexCoords;
uniform sampler2D texture_diffuse;
struct Light { vec3 position; vec3 color; float intensity; float radius; };
uniform Light lights[16];
uniform int active_lights;
void main() {
    vec4 texColor = texture(texture_diffuse, TexCoords);
    if(texColor.a < 0.1) discard;
    vec3 norm = normalize(Normal);
    vec3 result = vec3(0.12) * texColor.rgb;
    for(int i = 0; i < active_lights && i < 16; i++) {
        vec3 toLight = lights[i].position - FragPos;
        float distSq = dot(toLight, toLight);
        float radiusSq = lights[i].radius * lights[i].radius;
        if(distSq < radiusSq) {
            float dist = sqrt(distSq);
            vec3 lightDir = toLight / dist;
            float diff = max(dot(norm, lightDir), 0.0);
            float att = 1.0 - (dist / lights[i].radius);
            att = att * att;
            result += (diff * lights[i].color * lights[i].intensity * att) * texColor.rgb;
        }
    }
    FragColor = vec4(result, texColor.a);
}"""

# Simplified fog with 16 steps instead of 32
ARM_FOG_FRAG = """#version 330 core
out vec4 FragColor;
in vec3 localPos;
uniform mat4 model;
uniform vec3 viewPos;
uniform float density;
uniform vec3 fogColor;
uniform sampler3D noiseTexture;
uniform float noiseScale;
uniform float time;

vec2 intersectBox(vec3 rayOrigin, vec3 rayDir) {
    vec3 tMin = (-0.5 - rayOrigin) / rayDir;
    vec3 tMax = (0.5 - rayOrigin) / rayDir;
    vec3 t1 = min(tMin, tMax);
    vec3 t2 = max(tMin, tMax);
    float tNear = max(max(t1.x, t1.y), t1.z);
    float tFar = min(min(t2.x, t2.y), t2.z);
    return vec2(tNear, tFar);
}

void main() {
    vec3 fragWorldPos = vec3(model * vec4(localPos, 1.0));
    vec3 rayDirWorld = normalize(fragWorldPos - viewPos);
    mat4 inverseModel = inverse(model);
    vec3 rayOriginLocal = (inverseModel * vec4(viewPos, 1.0)).xyz;
    vec3 rayDirLocal = normalize((inverseModel * vec4(rayDirWorld, 0.0)).xyz);
    vec2 t = intersectBox(rayOriginLocal, rayDirLocal);
    float tNear = t.x;
    float tFar = t.y;
    if (tNear >= tFar) discard;
    tNear = max(0.0, tNear);
    
    // ARM OPTIMIZATION: 16 steps instead of 32
    int num_steps = 16;
    float stepSize = (tFar - tNear) / float(num_steps);
    vec4 accumulatedColor = vec4(0.0);
    
    for (int i = 0; i < num_steps; ++i) {
        float currentT = tNear + float(i) * stepSize;
        vec3 samplePos = rayOriginLocal + rayDirLocal * currentT;
        vec3 noiseCoord = samplePos * noiseScale + vec3(0.0, 0.0, time * 0.1);
        float noiseValue = texture(noiseTexture, noiseCoord).r;
        float stepDensity = density * noiseValue;
        float transmittance = exp(-stepDensity * stepSize);
        accumulatedColor.rgb += fogColor * (1.0 - transmittance) * (1.0 - accumulatedColor.a);
        accumulatedColor.a += (1.0 - transmittance);
        if (accumulatedColor.a > 0.95) break;
    }
    accumulatedColor.a = clamp(accumulatedColor.a, 0.0, 1.0);
    FragColor = accumulatedColor;
}"""


class ShaderLoader:
    def __init__(self, shader_dir='assets/shaders'):
        self.shader_dir = shader_dir
        if not os.path.exists(self.shader_dir):
            try:
                os.makedirs(self.shader_dir)
            except OSError:
                if os.path.exists('shaders'):
                    self.shader_dir = 'shaders'
        self._ensure_defaults()

    def _ensure_defaults(self):
        for filename, source in DEFAULT_SHADERS.items():
            filepath = os.path.join(self.shader_dir, filename)
            try:
                with open(filepath, 'w') as f:
                    f.write(source)
            except Exception as e:
                print(f"Error generating shader {filename}: {e}")

    def _read_source(self, filename):
        filepath = os.path.join(self.shader_dir, filename)
        if not os.path.exists(filepath):
            if os.path.exists(filename):
                filepath = filename
            else:
                raise FileNotFoundError(f"Shader file not found: {filename}")
        with open(filepath, 'r') as f:
            return f.read()

    def compile_shader_program(self, vertex_file, fragment_file, geometry_file=None):
        try:
            vertex_src = self._read_source(vertex_file)
            fragment_src = self._read_source(fragment_file)
            vs = compileShader(vertex_src, gl.GL_VERTEX_SHADER)
            fs = compileShader(fragment_src, gl.GL_FRAGMENT_SHADER)
            if geometry_file:
                geometry_src = self._read_source(geometry_file)
                gs = compileShader(geometry_src, gl.GL_GEOMETRY_SHADER)
                program = compileProgram(vs, fs, gs)
            else:
                program = compileProgram(vs, fs)
            return program
        except Exception as e:
            print(f"Error compiling shader ({vertex_file}, {fragment_file}): {e}")
            raise
    
    def compile_from_source(self, vertex_src, fragment_src):
        """Compile shader from source strings directly."""
        try:
            vs = compileShader(vertex_src, gl.GL_VERTEX_SHADER)
            fs = compileShader(fragment_src, gl.GL_FRAGMENT_SHADER)
            return compileProgram(vs, fs)
        except Exception as e:
            print(f"Error compiling shader from source: {e}")
            raise


class RenderStats:
    __slots__ = ('total_brushes', 'culled_brushes', 'visible_brushes', 'draw_calls', 
                 'shadow_draw_calls', 'total_tris', 'visible_tris', 'batched_draws')
    def __init__(self): 
        self.reset()
    def reset(self): 
        self.total_brushes = self.culled_brushes = self.visible_brushes = 0
        self.draw_calls = self.shadow_draw_calls = self.batched_draws = 0
        self.total_tris = self.visible_tris = 0


class LODManager:
    __slots__ = ('full_dist_sq', 'cull_dist_sq')
    LOD_FULL, LOD_REDUCED, LOD_CULLED = 0, 1, 2
    def __init__(self, full_dist=500.0, cull_dist=2000.0):
        self.full_dist_sq = full_dist * full_dist
        self.cull_dist_sq = cull_dist * cull_dist
    def get_lod_level(self, brush_pos, camera_pos):
        if isinstance(brush_pos, (list, tuple)):
            dx, dy, dz = brush_pos[0] - camera_pos.x, brush_pos[1] - camera_pos.y, brush_pos[2] - camera_pos.z
        else:
            dx, dy, dz = brush_pos.x - camera_pos.x, brush_pos.y - camera_pos.y, brush_pos.z - camera_pos.z
        dist_sq = dx*dx + dy*dy + dz*dz
        if dist_sq < self.full_dist_sq: return self.LOD_FULL
        elif dist_sq < self.cull_dist_sq: return self.LOD_REDUCED
        return self.LOD_CULLED


class UniformCache:
    __slots__ = ('program', '_cache')
    def __init__(self, shader_program):
        self.program = shader_program
        self._cache = {}
    def __getitem__(self, name):
        loc = self._cache.get(name)
        if loc is None:
            loc = gl.glGetUniformLocation(self.program, name)
            self._cache[name] = loc
        return loc
    def preload(self, names):
        for name in names:
            if name not in self._cache: 
                self._cache[name] = gl.glGetUniformLocation(self.program, name)
    def get(self, name, default=-1):
        return self._cache.get(name, default)


class ShadowBatch:
    __slots__ = ('positions', 'scales', 'rotations', 'alphas', 'count', 'capacity')
    def __init__(self, capacity=1024):
        self.capacity = capacity
        self.positions = np.zeros((capacity, 3), dtype=np.float32)
        self.scales = np.zeros((capacity, 3), dtype=np.float32)
        self.rotations = np.zeros(capacity, dtype=np.float32)
        self.alphas = np.zeros(capacity, dtype=np.float32)
        self.count = 0
    def reset(self): 
        self.count = 0
    def add(self, pos, scale, rotation, alpha):
        if self.count >= self.capacity: return False
        i = self.count
        self.positions[i], self.scales[i], self.rotations[i], self.alphas[i] = pos, scale, rotation, alpha
        self.count += 1
        return True


class Renderer:
    MAX_LIGHTS = 16
    
    def __init__(self, texture_loader, initial_grid_size, initial_world_size, config=None):
        self.texture_manager = {}
        self.loaded_models = {} 
        self.load_texture_callback = texture_loader
        self._identity_mat4 = glm.mat4(1.0)
        self._identity_mat3 = glm.mat3(1.0)
        self.render_stats = RenderStats()
        self.lod_manager = LODManager()
        
        # PERFORMANCE FLAGS - Read from config with auto-detected defaults
        is_arm = self._detect_arm_platform()
        
        if config is not None:
            self.arm_mode = config.getboolean('Renderer', 'arm_mode', fallback=True)
            self.shadows_enabled = config.getboolean('Renderer', 'shadows_enabled', fallback=not is_arm)
        else:
            # No config provided - use auto-detected defaults
            self.arm_mode = True  # Always beneficial
            self.shadows_enabled = not is_arm  # Off on ARM, On otherwise
        
        self.fog_quality = 'low'  # 'low' = 16 steps, 'high' = 32 steps
        self.skip_culling_in_renderer = True  # Trust pre-culled data from logic thread
        
        self._model_matrix = glm.mat4(1.0)
        self._floor_shadow_batch = ShadowBatch(2048)
        self._wall_shadow_batch = ShadowBatch(1024)
        
        # Cached per-frame data
        self._frame_lights = []
        self._frame_lights_uploaded = False
        self._current_shader = None
        
        # Pre-allocated arrays for batching
        self._batch_matrices = []
        self._batch_colors = []

        try:
            self.shader_loader = ShaderLoader()
            self.shaders = {}
            self.uniforms = {}
            
            # Compile ARM-optimized shaders if arm_mode is enabled
            if self.arm_mode:
                print("ARM Mode: Compiling optimized shaders...")
                self._compile_arm_shaders()
            else:
                self._compile_standard_shaders()
            
            # Common shaders (always use file-based)
            for name, files in [('simple', ('simple.vert', 'simple.frag')),
                                ('sprite', ('sprite.vert', 'sprite.frag')),
                                ('shadow_volume', ('shadow_volume.vert', 'shadow_volume.frag')),
                                ('water', ('water.vert', 'water.frag')),
                                ('glass', ('glass.vert', 'glass.frag'))]:
                shader = self.shader_loader.compile_shader_program(*files)
                self.shaders[name] = shader
                self.uniforms[name] = UniformCache(shader)

            self.uniforms['simple'].preload(['projection', 'view', 'model', 'color', 'alpha'])
            self.uniforms['sprite'].preload(['projection', 'view', 'sprite_texture', 'sprite_pos_world', 'sprite_size'])
            self.uniforms['shadow_volume'].preload(['projection', 'view', 'model', 'light_pos'])
            
            self._preload_lit_uniforms('water')
            self.uniforms['water'].preload(['time', 'viewPos', 'normalMap', 'waterOpacity', 'waterReflectivity', 
                                           'waterTint', 'useWaveDisplacement', 'waveStrength'])
            self.water_normal_id = self.load_texture('water_normal.png', 'textures')
            
            self.uniforms['glass'].preload(['projection', 'view', 'model', 'viewPos', 'waterColor', 
                                           'distortionStrength', 'causticStrength', 'glassOpacity', 
                                           'refractionIndex', 'roughness'])

            # Terrain shader
            try:
                terrain_vs = compileShader(TERRAIN_VERTEX_SHADER, gl.GL_VERTEX_SHADER)
                terrain_fs = compileShader(TERRAIN_FRAGMENT_SHADER, gl.GL_FRAGMENT_SHADER)
                terrain_program = compileProgram(terrain_vs, terrain_fs)
                self.shaders['terrain'] = terrain_program
                self.uniforms['terrain'] = UniformCache(terrain_program)
                self.uniforms['terrain'].preload([
                    'projection', 'view', 'active_lights',
                    'texGrass', 'texRock', 'texSand', 'texSnow',
                    'biomeWeights', 'terrainHeightScale'
                ])
                for i in range(self.MAX_LIGHTS):
                    self.uniforms['terrain'].preload([
                        f'lights[{i}].position', f'lights[{i}].color',
                        f'lights[{i}].intensity', f'lights[{i}].radius'
                    ])
                print("Terrain shader loaded")
            except Exception as e:
                print(f"Terrain shader error: {e}")
                self.shaders['terrain'] = None
            
            print(f"Shaders loaded from: {self.shader_loader.shader_dir}")
            print(f"ARM Mode: {self.arm_mode}, Shadows: {self.shadows_enabled}")
        except Exception as e:
            print(f"FATAL: Shader Error: {e}")
            return
        
        self.vaos = {'cube': self._create_cube_vao(), 'sprite': self._create_sprite_vao(), 'grid': None}
        self.grid_indices_count = 0
        self._create_gizmo_buffers()
        self.update_grid_buffers(initial_world_size, initial_grid_size)
        self.noise_texture_id = self._load_3d_texture('assets/noise_3d.bin')
        self.sprite_textures = {}
        self.instance_textures = {}
        self.load_texture('default.png', 'textures')
        self.load_texture('caulk', 'textures')
        self._proj_ptr = None
        self._view_ptr = None
        self._edge_vao = None
    
    def _detect_arm_platform(self):
        """Detect if running on ARM or under x64 emulation."""
        import platform
        import sys
        machine = platform.machine().lower()
        
        # Direct ARM detection
        if 'arm' in machine or 'aarch' in machine:
            return True
        
        # Check for Windows ARM emulation markers
        if sys.platform == 'win32':
            if os.environ.get('PROCESSOR_ARCHITECTURE', '').upper() == 'ARM64':
                return True
            if os.environ.get('PROCESSOR_ARCHITEW6432', '').upper() == 'ARM64':
                return True
            proc_id = os.environ.get('PROCESSOR_IDENTIFIER', '').lower()
            if 'qualcomm' in proc_id or 'snapdragon' in proc_id or 'arm' in proc_id:
                return True
        
        return False

    def _compile_arm_shaders(self):
        """Compile ARM-optimized shaders with pre-computed normal matrices."""
        # Lit shader (ARM optimized)
        lit_shader = self.shader_loader.compile_from_source(ARM_LIT_VERT, ARM_LIT_FRAG)
        self.shaders['lit'] = lit_shader
        self.uniforms['lit'] = UniformCache(lit_shader)
        self._preload_lit_uniforms('lit')
        self.uniforms['lit'].preload(['normalMatrix'])
        
        # Textured shader (ARM optimized)
        textured_shader = self.shader_loader.compile_from_source(ARM_TEXTURED_VERT, ARM_TEXTURED_FRAG)
        self.shaders['textured'] = textured_shader
        self.uniforms['textured'] = UniformCache(textured_shader)
        self._preload_lit_uniforms('textured')
        self.uniforms['textured'].preload(['texture_diffuse', 'tex_scale', 'normalMatrix'])
        
        # Fog shader (ARM optimized with fewer ray march steps)
        fog_vert = DEFAULT_SHADERS.get('fog.vert', '')
        fog_shader = self.shader_loader.compile_from_source(fog_vert, ARM_FOG_FRAG)
        self.shaders['fog'] = fog_shader
        self.uniforms['fog'] = UniformCache(fog_shader)
        self._preload_lit_uniforms('fog')
        self.uniforms['fog'].preload(['viewPos', 'time', 'noiseTexture', 'density', 'fogColor', 
                                      'noiseScale', 'object_color', 'alpha'])
        
        print("ARM-optimized shaders compiled successfully")

    def _compile_standard_shaders(self):
        """Compile standard file-based shaders."""
        for name, files in [('lit', ('lit.vert', 'lit.frag')),
                            ('textured', ('textured.vert', 'textured.frag')),
                            ('fog', ('fog.vert', 'fog.frag'))]:
            shader = self.shader_loader.compile_shader_program(*files)
            self.shaders[name] = shader
            self.uniforms[name] = UniformCache(shader)
        
        self._preload_lit_uniforms('lit')
        self._preload_lit_uniforms('textured')
        self.uniforms['textured'].preload(['texture_diffuse', 'tex_scale'])
        self._preload_lit_uniforms('fog')
        self.uniforms['fog'].preload(['viewPos', 'time', 'noiseTexture', 'density', 'fogColor', 
                                      'noiseScale', 'object_color', 'alpha'])

    def _preload_lit_uniforms(self, shader_name):
        uniforms = self.uniforms[shader_name]
        uniforms.preload(['projection', 'view', 'model', 'object_color', 'alpha', 'active_lights'])
        for i in range(self.MAX_LIGHTS):
            uniforms.preload([f'lights[{i}].position', f'lights[{i}].color', 
                            f'lights[{i}].intensity', f'lights[{i}].radius'])

    def _compute_normal_matrix(self, model_matrix):
        """Pre-compute normal matrix on CPU to avoid expensive inverse() in shader."""
        # Extract the upper-left 3x3 and compute transpose of inverse
        mat3 = glm.mat3(model_matrix)
        # For uniform scaling, we can just use the mat3 directly
        # For non-uniform scaling, we need the full inverse transpose
        try:
            return glm.transpose(glm.inverse(mat3))
        except:
            return self._identity_mat3

    # =========================================================================
    # TEXTURE MANAGEMENT
    # =========================================================================

    def update_grid_buffers(self, world_size, grid_size):
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
    
    def set_instance_textures(self, textures): 
        self.instance_textures = textures

    def load_model(self, filename):
        if OBJ is None:
            return None
        if filename in self.loaded_models:
            return self.loaded_models[filename]
        full_path = os.path.join('assets', 'models', filename)
        if not os.path.exists(full_path):
            full_path = filename
        if os.path.exists(full_path):
            print(f"Loading model: {full_path}")
            model = OBJ(full_path)
            if model.is_loaded:
                self.loaded_models[filename] = model
                return model
        print(f"Failed to load model: {filename}")
        return None

    def load_texture(self, texture_name, subfolder):
        tex_cache_name = os.path.join(subfolder, texture_name)
        if tex_cache_name in self.texture_manager: 
            return self.texture_manager[tex_cache_name]
        if texture_name == 'default.png':
            tex_id = gl.glGenTextures(1)
            self.texture_manager[tex_cache_name] = tex_id
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, 1, 1, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, 
                           (gl.GLubyte * 4)(255, 255, 255, 255))
            return tex_id
        if texture_name == 'caulk':
            tex_id = gl.glGenTextures(1)
            self.texture_manager[tex_cache_name] = tex_id
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, 2, 2, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, 
                           (gl.GLubyte * 16)(255, 0, 255, 255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 0, 255, 255))
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            return tex_id
        texture_path = os.path.join('assets', subfolder, texture_name)
        if not os.path.exists(texture_path): 
            return self.load_texture('default.png', 'textures')
        try:
            img = Image.open(texture_path).convert("RGBA")
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
            tex_id = gl.glGenTextures(1)
            self.texture_manager[tex_cache_name] = tex_id
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, img.width, img.height, 0, 
                           gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, img.tobytes())
            gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
            return tex_id
        except Exception as e:
            print(f"Error loading texture '{texture_name}': {e}")
            return self.load_texture('default.png', 'textures')

    def preload_level_textures(self, brushes):
        texture_set = set()
        for brush in brushes:
            for face_tex in brush.get('textures', {}).values():
                if face_tex and face_tex != 'caulk.jpg': 
                    texture_set.add(face_tex)
        for tex_name in texture_set: 
            self.load_texture(tex_name, 'textures')

    def _load_3d_texture(self, filepath, size=32):
        try:
            with open(filepath, 'rb') as f: 
                data = f.read()
            if len(data) != size ** 3: 
                return 0
            texture_id = gl.glGenTextures(1)
            gl.glBindTexture(gl.GL_TEXTURE_3D, texture_id)
            for param in [(gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT), (gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT), 
                          (gl.GL_TEXTURE_WRAP_R, gl.GL_REPEAT), (gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR), 
                          (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)]:
                gl.glTexParameteri(gl.GL_TEXTURE_3D, *param)
            gl.glTexImage3D(gl.GL_TEXTURE_3D, 0, gl.GL_R8, size, size, size, 0, gl.GL_RED, gl.GL_UNSIGNED_BYTE, data)
            return texture_id
        except: 
            return 0

    # =========================================================================
    # LIGHT MANAGEMENT - UPLOAD ONCE PER FRAME
    # =========================================================================

    def _upload_lights_once(self, shader_name, lights):
        """Upload light uniforms once per frame, track which shader they're uploaded to."""
        if shader_name not in self.uniforms: 
            return
        uniforms = self.uniforms[shader_name]
        num_lights = min(len(lights), self.MAX_LIGHTS)
        gl.glUniform1i(uniforms['active_lights'], num_lights)
        for i in range(num_lights):
            light = lights[i]
            gl.glUniform3fv(uniforms[f'lights[{i}].position'], 1, light.pos)
            gl.glUniform3fv(uniforms[f'lights[{i}].color'], 1, light.get_color())
            gl.glUniform1f(uniforms[f'lights[{i}].intensity'], light.get_intensity())
            gl.glUniform1f(uniforms[f'lights[{i}].radius'], light.get_radius())

    # =========================================================================
    # TERRAIN
    # =========================================================================

    def setup_terrain_shader(self, terrain):
        if 'terrain' not in self.shaders or not self.shaders['terrain']:
            return
        terrain.shader_program = self.shaders['terrain']
        terrain.uniforms = {
            'projection': self.uniforms['terrain']['projection'],
            'view': self.uniforms['terrain']['view'],
            'active_lights': self.uniforms['terrain']['active_lights'],
            'texGrass': self.uniforms['terrain']['texGrass'],
            'texRock': self.uniforms['terrain']['texRock'],
            'texSand': self.uniforms['terrain']['texSand'],
            'texSnow': self.uniforms['terrain']['texSnow'],
            'biomeWeights': self.uniforms['terrain']['biomeWeights'],
            'terrainHeightScale': self.uniforms['terrain']['terrainHeightScale'],
        }
        for i in range(self.MAX_LIGHTS):
            terrain.uniforms[f'lights[{i}].position'] = self.uniforms['terrain'][f'lights[{i}].position']
            terrain.uniforms[f'lights[{i}].color'] = self.uniforms['terrain'][f'lights[{i}].color']
            terrain.uniforms[f'lights[{i}].intensity'] = self.uniforms['terrain'][f'lights[{i}].intensity']
            terrain.uniforms[f'lights[{i}].radius'] = self.uniforms['terrain'][f'lights[{i}].radius']

    def _ensure_terrain_textures(self, terrain):
        mappings = [('grass_tex', 'grass.jpg'), ('rock_tex', 'rock.jpg'), 
                    ('sand_tex', 'sand.jpg'), ('snow_tex', 'snow.jpg')]
        self.load_texture('default.png', 'textures')
        for attr, filename in mappings:
            current_id = getattr(terrain, attr, 0)
            if not current_id or current_id == -1:
                new_id = self.load_texture(filename, 'textures/terrain')
                setattr(terrain, attr, new_id)

    def render_terrain(self, projection, view, camera_pos, terrain, lights, frustum_planes=None):
        if terrain is None or not terrain.enabled:
            return
        self._ensure_terrain_textures(terrain)
        if not terrain.shader_program:
            self.setup_terrain_shader(terrain)
        active_lights_count = len(lights) if lights else 0
        gl.glDisable(gl.GL_CULL_FACE)
        if hasattr(terrain, 'get_tri_count'):
            self.render_stats.visible_tris += terrain.get_tri_count()
        terrain.update_and_render(projection, view, camera_pos, frustum_planes, lights, active_lights_count)

    # =========================================================================
    # MAIN RENDER SCENE - OPTIMIZED
    # =========================================================================

    def render_scene(self, projection, view, camera_pos, brushes, things, selected_object, config):
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LESS)
        gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)
        self._proj_ptr = glm.value_ptr(projection)
        self._view_ptr = glm.value_ptr(view)
        current_mode = config.get('render_mode', RENDER_MODE_LIT)

        # Reset per-frame stats
        self.render_stats.reset()
        self.render_stats.total_brushes = len(brushes)
        self._frame_lights_uploaded = False
        self._current_shader = None

        if current_mode == RENDER_MODE_WIREFRAME: 
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)
        elif current_mode == RENDER_MODE_VERTEX: 
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_POINT)
            gl.glPointSize(4.0)
        else: 
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)

        self.draw_grid(projection, view, self.grid_indices_count, 
                      config.get('play_mode', False), config.get('grid_visible', True))
        
        # Sort objects ONCE
        opaque_brushes, transparent_brushes, sprite_things, fog_volumes, water_brushes, glass_brushes = \
            self._sort_objects(brushes, things, config)
        
        # Split opaque brushes: textured vs solid
        textured_opaque = []
        solid_opaque = []
        for b in opaque_brushes:
            is_textured = False
            if 'textures' in b:
                for t_name in b['textures'].values():
                    if t_name and t_name not in ['default.png', 'caulk.jpg']:
                        is_textured = True
                        break
            if is_textured:
                textured_opaque.append(b)
            else:
                solid_opaque.append(b)

        # Separate models from sprites
        models_to_render = []
        final_sprites = []
        for thing in sprite_things:
            if isinstance(thing, Thing) and thing.properties.get('model_path'):
                models_to_render.append(thing)
            else:
                final_sprites.append(thing)

        # Get active lights ONCE
        lights = [t for t in things if isinstance(t, Light) and t.properties.get('state', 'on') == 'on']
        self._frame_lights = lights

        # Terrain
        terrain = config.get('terrain', None)
        if terrain and terrain.enabled:
            self.render_terrain(projection, view, camera_pos, terrain, lights)

        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_BLEND)
        
        brush_display_mode = config.get('brush_display_mode', 'Textured')

        if current_mode == RENDER_MODE_UNLIT: 
            self.draw_textured_brushes_optimized(projection, view, camera_pos, textured_opaque, lights, config)
            self.draw_lit_brushes_optimized(projection, view, camera_pos, solid_opaque, lights, config)
        elif current_mode == RENDER_MODE_LIT:
            if brush_display_mode == 'Textured' or brush_display_mode == 'Solid Lit':
                self.draw_textured_brushes_optimized(projection, view, camera_pos, textured_opaque, lights, config)
                self.draw_lit_brushes_optimized(projection, view, camera_pos, solid_opaque, lights, config)
            else:
                self.draw_lit_brushes_optimized(projection, view, camera_pos, opaque_brushes, lights, config)
        else:
            self.draw_lit_brushes_optimized(projection, view, camera_pos, opaque_brushes, lights, config)

        # Models
        if models_to_render:
            self.draw_models(projection, view, camera_pos, models_to_render, lights, config)

        # Shadows (optional - disabled by default for ARM)
        if current_mode == RENDER_MODE_LIT and self.shadows_enabled:
            shadow_lights = [l for l in lights if l.properties.get('casts_shadows')]
            if shadow_lights:
                all_brushes = config.get('all_brushes', brushes)
                self.render_projected_shadows_optimized(projection, view, camera_pos, all_brushes, shadow_lights)

        # Sort transparent objects by distance ONCE
        if transparent_brushes: 
            transparent_brushes.sort(key=lambda b: -self._distance_sq(b['pos'], camera_pos))
        if water_brushes: 
            water_brushes.sort(key=lambda b: -self._distance_sq(b['pos'], camera_pos))
        if glass_brushes: 
            glass_brushes.sort(key=lambda b: -self._distance_sq(b['pos'], camera_pos))
        if final_sprites: 
            final_sprites.sort(key=lambda s: -self._distance_sq(s.pos, camera_pos))
            
        gl.glEnable(gl.GL_BLEND)
        gl.glDepthMask(gl.GL_FALSE)
        
        self.draw_sprites(projection, view, final_sprites, self.sprite_textures, self.instance_textures)
        
        # Transparent pass
        if current_mode == RENDER_MODE_UNLIT:
            self.draw_textured_brushes_optimized(projection, view, camera_pos, transparent_brushes, lights, config)
        elif current_mode == RENDER_MODE_LIT:
            self.draw_lit_brushes_optimized(projection, view, camera_pos, transparent_brushes, lights, config, is_transparent_pass=True)
        else:
            self.draw_lit_brushes_optimized(projection, view, camera_pos, transparent_brushes, lights, config, is_transparent_pass=True)
            
        if current_mode == RENDER_MODE_LIT:
            self.draw_water_brushes(projection, view, camera_pos, water_brushes, lights, config)
            self.draw_glass_brushes(projection, view, camera_pos, glass_brushes, lights, config)
            self.draw_fog_volumes(projection, view, camera_pos, fog_volumes, lights, config)

        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        
        if selected_object:
            if isinstance(selected_object, dict):
                self.draw_selected_brush_outline(projection, view, selected_object)
                if not selected_object.get('lock', False): 
                    self.render_gizmo(projection, view, selected_object['pos'])
            elif isinstance(selected_object, Thing): 
                self.render_gizmo(projection, view, selected_object.pos)
        
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glUseProgram(0)

    # =========================================================================
    # OPTIMIZED DRAWING METHODS - NO DOUBLE CULLING
    # =========================================================================

    def draw_lit_brushes_optimized(self, projection, view, camera_pos, brushes, lights, config, is_transparent_pass=False):
        """Optimized lit brush drawing - trusts pre-culled data, batches where possible."""
        if not brushes or 'lit' not in self.shaders: 
            return
        
        # OPTIMIZATION: Skip culling if data is pre-culled from logic thread
        visible = brushes  # Trust pre-culled data
        self.render_stats.visible_brushes += len(visible)
        
        if not visible: 
            return
        
        shader, uniforms = self.shaders['lit'], self.uniforms['lit']
        
        # Always ensure correct shader is active (removed faulty caching)
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_lights_once('lit', lights)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, self._proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, self._view_ptr)
        
        gl.glBindVertexArray(self.vaos['cube'])
        
        display_mode = config.get('brush_display_mode', 'Textured')
        show_triggers_solid = config.get('show_triggers_as_solid', False)
        selected = config.get('selected_object')
        model_loc, color_loc, alpha_loc = uniforms['model'], uniforms['object_color'], uniforms['alpha']
        
        # ARM mode: get normal matrix location (only valid for ARM shaders)
        # Check both that ARM mode is on AND that the uniform actually exists in the current shader
        normal_mat_loc = -1
        if self.arm_mode:
            loc = uniforms.get('normalMatrix', -1)
            if loc is not None and loc != -1:
                normal_mat_loc = loc
        
        fill_mode = (gl.GL_FILL if show_triggers_solid else gl.GL_LINE) if is_transparent_pass else \
                   (gl.GL_FILL if display_mode != "Wireframe" else gl.GL_LINE)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, fill_mode)
        
        for brush in visible:
            self.render_stats.visible_tris += 12
            
            # Build model matrix
            pos = brush['pos']
            size = brush['size']
            model_matrix = glm.scale(glm.translate(self._identity_mat4, glm.vec3(*pos)), glm.vec3(*size))
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
            
            # ARM mode: upload pre-computed normal matrix (only if uniform exists and is valid)
            if normal_mat_loc > 0:
                normal_mat = self._compute_normal_matrix(model_matrix)
                gl.glUniformMatrix3fv(normal_mat_loc, 1, gl.GL_FALSE, glm.value_ptr(normal_mat))
            
            # Determine color
            if brush.get('is_trigger'): 
                color, alpha = [0.0, 1.0, 1.0], 0.3
            elif brush is selected: 
                color, alpha = [1.0, 1.0, 0.0], 1.0
            elif brush.get('operation') == 'subtract': 
                color, alpha = [1.0, 0.0, 0.0], 1.0
            else:
                brush_colour = brush.get('colour')
                if brush_colour and isinstance(brush_colour, (list, tuple)) and len(brush_colour) >= 3:
                    color = [c / 255.0 if c > 1.0 else c for c in brush_colour[:3]]
                else: 
                    color = [0.8, 0.8, 0.8]
                alpha = 1.0
            
            gl.glUniform3fv(color_loc, 1, color)
            gl.glUniform1f(alpha_loc, alpha)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
            self.render_stats.draw_calls += 1
        
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl.glBindVertexArray(0)

    def draw_textured_brushes_optimized(self, projection, view, camera_pos, brushes, lights, config):
        """Optimized textured brush drawing with better batching."""
        if not brushes or 'textured' not in self.shaders: 
            return
        
        # OPTIMIZATION: Skip culling - trust pre-culled data
        visible = brushes
        self.render_stats.visible_brushes += len(visible)
        
        if not visible: 
            return
        
        shader, uniforms = self.shaders['textured'], self.uniforms['textured']
        
        # Always ensure correct shader is active
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_lights_once('textured', lights)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, self._proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, self._view_ptr)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(uniforms['texture_diffuse'], 0)
        
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl.glBindVertexArray(self.vaos['cube'])
        
        model_loc = uniforms['model']
        tex_scale_loc = uniforms.get('tex_scale', -1)
        if tex_scale_loc == -1:
            tex_scale_loc = gl.glGetUniformLocation(shader, "tex_scale")
        
        # ARM mode: get normal matrix location (only if it exists in the shader)
        normal_mat_loc = -1
        if self.arm_mode:
            loc = uniforms.get('normalMatrix', -1)
            if loc is not None and loc != -1:
                normal_mat_loc = loc
        
        # OPTIMIZATION: Batch by texture to minimize state changes
        batches = defaultdict(list)
        is_play = config.get('play_mode', False)

        for brush in visible:
            for i, key in enumerate(['south', 'north', 'west', 'east', 'down', 'top']):
                tex_name = brush.get('textures', {}).get(key, 'default.png')
                if tex_name == 'caulk.jpg': 
                    continue
                if is_play and tex_name == 'nodraw.jpg': 
                    continue
                tex_id = self.texture_manager.get(os.path.join('textures', tex_name)) or \
                        self.load_texture_callback(tex_name, 'textures')
                batches[tex_id].append((brush, i))
        
        current_tex = None
        for tex_id, items in batches.items():
            if tex_id != current_tex: 
                gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                current_tex = tex_id
                self.render_stats.batched_draws += 1
            
            for brush, face_idx in items:
                self.render_stats.visible_tris += 2
                
                pos = brush['pos']
                size = brush['size']
                model_matrix = glm.scale(glm.translate(self._identity_mat4, glm.vec3(*pos)), glm.vec3(*size))
                gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
                
                # ARM mode: upload normal matrix
                if normal_mat_loc > 0:
                    normal_mat = self._compute_normal_matrix(model_matrix)
                    gl.glUniformMatrix3fv(normal_mat_loc, 1, gl.GL_FALSE, glm.value_ptr(normal_mat))
                
                if tex_scale_loc != -1:
                    if brush.get('texture_tiling', False):
                        tex_unit_size = 128.0 
                        if face_idx == 0 or face_idx == 1:
                            scale_x, scale_y = size[0] / tex_unit_size, size[1] / tex_unit_size
                        elif face_idx == 2 or face_idx == 3:
                            scale_x, scale_y = size[2] / tex_unit_size, size[1] / tex_unit_size
                        else:
                            scale_x, scale_y = size[0] / tex_unit_size, size[2] / tex_unit_size
                        gl.glUniform2f(tex_scale_loc, scale_x, scale_y)
                    else:
                        gl.glUniform2f(tex_scale_loc, 1.0, 1.0)
                
                gl.glDrawArrays(gl.GL_TRIANGLES, face_idx * 6, 6)
                self.render_stats.draw_calls += 1
        
        gl.glBindVertexArray(0)

    # =========================================================================
    # OTHER DRAWING METHODS (water, glass, fog, models, sprites)
    # =========================================================================

    def draw_models(self, projection, view, camera_pos, models, lights, config):
        if not models: 
            return
        
        lit_shader = self.shaders.get('lit')
        textured_shader = self.shaders.get('textured')
        current_shader = None
        
        gl.glDisable(gl.GL_CULL_FACE) 

        for thing in models:
            model_file = thing.properties.get('model_path')
            if not model_file: 
                continue
            
            obj = self.load_model(model_file)
            if not obj or not obj.is_loaded: 
                continue
            
            self.render_stats.visible_tris += (obj.vertex_count // 3)
            
            pos = thing.pos
            scale = thing.properties.get('scale', 1.0)
            if isinstance(scale, (int, float)): 
                scale = [scale, scale, scale]
            rot = thing.properties.get('rotation', [0, 0, 0])
            
            mat = glm.translate(self._identity_mat4, glm.vec3(*pos))
            mat = glm.rotate(mat, glm.radians(rot[1]), glm.vec3(0, 1, 0))
            mat = glm.rotate(mat, glm.radians(rot[0]), glm.vec3(1, 0, 0))
            mat = glm.rotate(mat, glm.radians(rot[2]), glm.vec3(0, 0, 1))
            mat = glm.scale(mat, glm.vec3(*scale))
            
            gl.glBindVertexArray(obj.vao)
            manual_texture = thing.properties.get('texture')
            
            if obj.groups and not manual_texture:
                for group in obj.groups:
                    mat_name = group['material']
                    material = obj.materials.get(mat_name, {'color': [0.8, 0.8, 0.8], 'texture': None})
                    use_texture = material.get('texture')
                    
                    if use_texture and textured_shader:
                        if current_shader != textured_shader:
                            gl.glUseProgram(textured_shader)
                            current_shader = textured_shader
                            u = self.uniforms['textured']
                            gl.glUniformMatrix4fv(u['projection'], 1, gl.GL_FALSE, self._proj_ptr)
                            gl.glUniformMatrix4fv(u['view'], 1, gl.GL_FALSE, self._view_ptr)
                            self._upload_lights_once('textured', lights)
                            gl.glActiveTexture(gl.GL_TEXTURE0)
                            gl.glUniform1i(u['texture_diffuse'], 0)
                        
                        tex_id = 0
                        path_in_textures = os.path.join('assets', 'textures', use_texture)
                        if os.path.exists(path_in_textures):
                            tex_id = self.load_texture(use_texture, 'textures')
                        else:
                            tex_id = self.load_texture(use_texture, 'models')
                        
                        gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                        gl.glUniformMatrix4fv(self.uniforms['textured']['model'], 1, gl.GL_FALSE, glm.value_ptr(mat))
                        
                    elif lit_shader:
                        if current_shader != lit_shader:
                            gl.glUseProgram(lit_shader)
                            current_shader = lit_shader
                            u = self.uniforms['lit']
                            gl.glUniformMatrix4fv(u['projection'], 1, gl.GL_FALSE, self._proj_ptr)
                            gl.glUniformMatrix4fv(u['view'], 1, gl.GL_FALSE, self._view_ptr)
                            self._upload_lights_once('lit', lights)

                        color = material.get('color', [0.8, 0.8, 0.8])
                        gl.glUniform3fv(self.uniforms['lit']['object_color'], 1, color)
                        gl.glUniform1f(self.uniforms['lit']['alpha'], 1.0)
                        gl.glUniformMatrix4fv(self.uniforms['lit']['model'], 1, gl.GL_FALSE, glm.value_ptr(mat))

                    gl.glDrawArrays(gl.GL_TRIANGLES, group['start'], group['count'])
            else:
                tex_name = manual_texture
                target_shader = textured_shader if tex_name else lit_shader
                
                if target_shader == textured_shader:
                    if current_shader != textured_shader:
                        gl.glUseProgram(textured_shader)
                        current_shader = textured_shader
                        u = self.uniforms['textured']
                        gl.glUniformMatrix4fv(u['projection'], 1, gl.GL_FALSE, self._proj_ptr)
                        gl.glUniformMatrix4fv(u['view'], 1, gl.GL_FALSE, self._view_ptr)
                        self._upload_lights_once('textured', lights)
                        gl.glActiveTexture(gl.GL_TEXTURE0)
                        gl.glUniform1i(u['texture_diffuse'], 0)
                    
                    tex_id = self.load_texture(tex_name, 'textures')
                    gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                    gl.glUniformMatrix4fv(self.uniforms['textured']['model'], 1, gl.GL_FALSE, glm.value_ptr(mat))
                    
                elif lit_shader:
                    if current_shader != lit_shader:
                        gl.glUseProgram(lit_shader)
                        current_shader = lit_shader
                        u = self.uniforms['lit']
                        gl.glUniformMatrix4fv(u['projection'], 1, gl.GL_FALSE, self._proj_ptr)
                        gl.glUniformMatrix4fv(u['view'], 1, gl.GL_FALSE, self._view_ptr)
                        self._upload_lights_once('lit', lights)
                    
                    col = thing.properties.get('color', [0.8, 0.8, 0.8])
                    gl.glUniform3fv(self.uniforms['lit']['object_color'], 1, col)
                    gl.glUniform1f(self.uniforms['lit']['alpha'], 1.0)
                    gl.glUniformMatrix4fv(self.uniforms['lit']['model'], 1, gl.GL_FALSE, glm.value_ptr(mat))

                gl.glDrawArrays(gl.GL_TRIANGLES, 0, obj.vertex_count)
            
            self.render_stats.draw_calls += 1
            
        gl.glBindVertexArray(0)
        gl.glEnable(gl.GL_CULL_FACE)

    def _distance_sq(self, pos1, pos2):
        if isinstance(pos1, (list, tuple)): 
            return (pos1[0]-pos2.x)**2 + (pos1[1]-pos2.y)**2 + (pos1[2]-pos2.z)**2
        return (pos1.x-pos2.x)**2 + (pos1.y-pos2.y)**2 + (pos1.z-pos2.z)**2

    def _sort_objects(self, brushes, things, config):
        opaque, transparent, sprites, fog, water, glass = [], [], [], [], [], []
        is_play, show_sprites = config.get('play_mode', False), config.get('show_sprites_in_play_mode', False)
        
        for brush in brushes:
            if brush.get('hidden'): 
                continue
            if brush.get('is_water', False) or brush.get('shader') == 'Water' or \
               any('water' in (t or '').lower() for t in brush.get('textures', {}).values()): 
                water.append(brush)
            elif brush.get('is_fog') or brush.get('shader') == 'Fog': 
                fog.append(brush)
            elif brush.get('shader') == 'Glass': 
                glass.append(brush)
            elif brush.get('is_trigger'): 
                if not is_play: 
                    transparent.append(brush)
            else: 
                opaque.append(brush)
        
        if not is_play:
            sprites = [t for t in things if isinstance(t, Thing)]
        else:
            from editor.things import Pickup
            for t in things:
                if isinstance(t, Thing):
                    if isinstance(t, Pickup):
                        sprites.append(t)
                    elif show_sprites:
                        sprites.append(t)
        
        return opaque, transparent, sprites, fog, water, glass

    def draw_water_brushes(self, projection, view, camera_pos, brushes, lights, config):
        if not brushes: 
            return
        shader, uniforms = self.shaders['water'], self.uniforms['water']
        gl.glUseProgram(shader)
        self._upload_lights_once('water', lights)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, self._proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, self._view_ptr)
        gl.glUniform3fv(uniforms['viewPos'], 1, glm.value_ptr(camera_pos))
        gl.glUniform1f(uniforms['time'], config.get('time', 0.0))
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.water_normal_id)
        gl.glUniform1i(uniforms['normalMap'], 0)
        
        opacity_loc = uniforms['waterOpacity']
        reflectivity_loc = uniforms['waterReflectivity']
        tint_loc, model_loc = uniforms['waterTint'], uniforms['model']
        wave_enable_loc = uniforms['useWaveDisplacement']
        wave_str_loc = uniforms['waveStrength']
        
        gl.glBindVertexArray(self.vaos['cube'])
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        
        for brush in brushes:
            model_matrix = glm.scale(glm.translate(self._identity_mat4, glm.vec3(*brush['pos'])), glm.vec3(*brush['size']))
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
            gl.glUniform1f(opacity_loc, brush.get('water_opacity', 0.5))
            gl.glUniform1f(reflectivity_loc, brush.get('water_reflectivity', 0.5))
            gl.glUniform3fv(tint_loc, 1, brush.get('water_tint', [0.0, 0.4, 0.6]))
            gl.glUniform1i(wave_enable_loc, int(brush.get('water_wave_enabled', False)))
            gl.glUniform1f(wave_str_loc, brush.get('water_wave_height', 0.5))
            
            if brush.get('water_plane', False):
                gl.glDrawArrays(gl.GL_TRIANGLES, 30, 6)
            else:
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 24)
                gl.glDrawArrays(gl.GL_TRIANGLES, 30, 6)
            self.render_stats.draw_calls += 1
        gl.glBindVertexArray(0)

    def draw_glass_brushes(self, projection, view, camera_pos, brushes, lights, config):
        if not brushes or 'glass' not in self.shaders: 
            return
        
        shader, uniforms = self.shaders['glass'], self.uniforms['glass']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, self._proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, self._view_ptr)
        gl.glUniform3fv(uniforms['viewPos'], 1, glm.value_ptr(camera_pos))
        
        model_loc = uniforms['model']
        water_color_loc = uniforms['waterColor']
        distortion_loc = uniforms['distortionStrength']
        caustic_loc = uniforms['causticStrength']
        opacity_loc = uniforms['glassOpacity']
        refraction_loc = uniforms['refractionIndex']
        roughness_loc = uniforms['roughness']
        
        gl.glBindVertexArray(self.vaos['cube'])
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glEnable(gl.GL_CULL_FACE)
        gl.glCullFace(gl.GL_BACK)
        
        for brush in brushes:
            model_matrix = glm.scale(glm.translate(self._identity_mat4, glm.vec3(*brush['pos'])), glm.vec3(*brush['size']))
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
            
            glass_color = brush.get('glass_color', [0.7, 0.85, 0.95])
            opacity = brush.get('glass_opacity', 0.3)
            distortion = brush.get('glass_distortion', 0.5)
            refraction = brush.get('glass_refraction', 1.5)
            roughness = brush.get('glass_roughness', 0.0)
            fresnel = brush.get('glass_fresnel', 0.5)
            
            gl.glUniform3fv(water_color_loc, 1, glass_color)
            gl.glUniform1f(distortion_loc, distortion)
            gl.glUniform1f(caustic_loc, fresnel)
            gl.glUniform1f(opacity_loc, opacity)
            gl.glUniform1f(refraction_loc, refraction)
            gl.glUniform1f(roughness_loc, roughness)
            
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
            self.render_stats.draw_calls += 1
        
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glBindVertexArray(0)

    def draw_fog_volumes(self, projection, view, camera_pos, brushes, lights, config):
        if not brushes or 'fog' not in self.shaders: 
            return
        
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        shader, uniforms = self.shaders['fog'], self.uniforms['fog']
        gl.glUseProgram(shader)
        self._upload_lights_once('fog', lights)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, self._proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, self._view_ptr)
        gl.glUniform3fv(uniforms['viewPos'], 1, glm.value_ptr(camera_pos))
        gl.glUniform1f(uniforms['time'], config.get('time', 0.0))
        gl.glActiveTexture(gl.GL_TEXTURE1)
        gl.glBindTexture(gl.GL_TEXTURE_3D, self.noise_texture_id)
        gl.glUniform1i(uniforms['noiseTexture'], 1)
        gl.glBindVertexArray(self.vaos['cube'])
        gl.glEnable(gl.GL_CULL_FACE)
        model_loc = uniforms['model']
        density_loc = uniforms['density']
        fog_color_loc = uniforms['fogColor']
        noise_scale_loc = uniforms['noiseScale']
        object_color_loc = uniforms['object_color']
        alpha_loc = uniforms['alpha']
        
        for brush in brushes:
            model_matrix = glm.scale(glm.translate(self._identity_mat4, glm.vec3(*brush['pos'])), glm.vec3(*brush['size']))
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
            f_color = brush.get('fog_color', [0.5, 0.6, 0.7])
            gl.glUniform1f(density_loc, brush.get('fog_density', 0.01))
            gl.glUniform3fv(fog_color_loc, 1, f_color)
            gl.glUniform1f(noise_scale_loc, brush.get('fog_noise_scale', 0.01))
            gl.glUniform3fv(object_color_loc, 1, f_color)
            gl.glUniform1f(alpha_loc, 0.4)
            
            gl.glCullFace(gl.GL_FRONT)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 24)
            gl.glDrawArrays(gl.GL_TRIANGLES, 30, 6)
            
            gl.glCullFace(gl.GL_BACK)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 24)
            gl.glDrawArrays(gl.GL_TRIANGLES, 30, 6)

        gl.glDisable(gl.GL_CULL_FACE)
        gl.glBindVertexArray(0)
        gl.glActiveTexture(gl.GL_TEXTURE0)

    def draw_grid(self, projection, view, grid_indices_count, play_mode=False, grid_visible=True):
        if not self.vaos['grid'] or play_mode or not grid_visible or 'simple' not in self.shaders: 
            return
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, self._proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, self._view_ptr)
        gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(self._identity_mat4))
        gl.glUniform3f(uniforms['color'], 0.2, 0.2, 0.2)
        gl.glUniform1f(uniforms['alpha'], 1.0)
        gl.glBindVertexArray(self.vaos['grid'])
        gl.glDrawArrays(gl.GL_LINES, 0, grid_indices_count)
        gl.glBindVertexArray(0)

    def draw_sprites(self, projection, view, things_to_draw, sprite_textures, instance_textures=None):
        if not things_to_draw or 'sprite' not in self.shaders: 
            return
        shader, uniforms = self.shaders['sprite'], self.uniforms['sprite']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, self._proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, self._view_ptr)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(uniforms['sprite_texture'], 0)
        pos_loc, size_loc = uniforms['sprite_pos_world'], uniforms['sprite_size']
        gl.glBindVertexArray(self.vaos['sprite'])
        current_tex = None
        for thing in things_to_draw:
            tex_id = None
            if instance_textures:
                tex_id = instance_textures.get(id(thing))
            if tex_id is None:
                tex_id = sprite_textures.get(thing.__class__.__name__)
            if tex_id:
                if tex_id != current_tex: 
                    gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                    current_tex = tex_id
                gl.glUniform3fv(pos_loc, 1, thing.pos)
                gl.glUniform2f(size_loc, 16.0 if isinstance(thing, Light) else 32.0, 
                              16.0 if isinstance(thing, Light) else 32.0)
                gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)
        gl.glBindVertexArray(0)

    # =========================================================================
    # SHADOW RENDERING (Optional - disabled by default for ARM)
    # =========================================================================

    def render_projected_shadows_optimized(self, projection, view, camera_pos, brushes, shadow_lights):
        if not brushes or not shadow_lights or 'lit' not in self.shaders: 
            return
        shader, uniforms = self.shaders['lit'], self.uniforms['lit']
        gl.glUseProgram(shader)
        gl.glUniform1i(uniforms['active_lights'], 0)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, self._proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, self._view_ptr)
        gl.glBindVertexArray(self.vaos['cube'])
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        model_loc, color_loc, alpha_loc = uniforms['model'], uniforms['object_color'], uniforms['alpha']
        solid_brushes = [b for b in brushes if not b.get('is_trigger')]
        
        for light in shadow_lights:
            lx, ly, lz = light.pos[0], light.pos[1], light.pos[2]
            light_radius, light_radius_sq = light.get_radius(), light.get_radius()**2
            floor_y = None
            for b in solid_brushes:
                top_y = b['pos'][1] + b['size'][1] * 0.5
                if top_y < ly and (floor_y is None or top_y > floor_y): 
                    floor_y = top_y
            if floor_y is None: 
                continue
            shadow_y, light_height = floor_y + 0.1, ly - floor_y
            if light_height <= 0: 
                continue
            
            shadow_casters = []
            for brush in solid_brushes:
                bx, by, bz = brush['pos']
                sx, sy, sz = brush['size']
                brush_top = by + sy * 0.5
                if abs(brush_top - floor_y) < 0.1 or brush_top <= floor_y or sx > 500 or sz > 500: 
                    continue
                if (bx-lx)**2 + (by-ly)**2 + (bz-lz)**2 > light_radius_sq * 1.5: 
                    continue
                shadow_casters.append(brush)
            if not shadow_casters: 
                continue
            
            self._floor_shadow_batch.reset()
            gl.glEnable(gl.GL_STENCIL_TEST)
            gl.glStencilMask(0xFF)
            gl.glClear(gl.GL_STENCIL_BUFFER_BIT)
            gl.glStencilFunc(gl.GL_ALWAYS, 1, 0xFF)
            gl.glStencilOp(gl.GL_KEEP, gl.GL_KEEP, gl.GL_REPLACE)
            gl.glColorMask(gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)
            gl.glDepthMask(gl.GL_FALSE)
            gl.glDisable(gl.GL_DEPTH_TEST)
            
            for brush in shadow_casters:
                bx, by, bz = brush['pos']
                sx, sy, sz = brush['size']
                brush_diag = ((sx*sx + sz*sz) ** 0.5) * 0.5
                footprint_size = brush_diag * 2.5 + 2.0
                footprint_mat = glm.translate(self._identity_mat4, glm.vec3(bx, shadow_y, bz))
                footprint_mat = glm.scale(footprint_mat, glm.vec3(footprint_size, 0.01, footprint_size))
                gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(footprint_mat))
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
            
            gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
            gl.glStencilFunc(gl.GL_EQUAL, 0, 0xFF)
            gl.glStencilOp(gl.GL_KEEP, gl.GL_KEEP, gl.GL_KEEP)
            gl.glStencilMask(0x00)
            gl.glEnable(gl.GL_BLEND)
            gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
            gl.glEnable(gl.GL_DEPTH_TEST)
            gl.glDepthFunc(gl.GL_LEQUAL)
            gl.glEnable(gl.GL_POLYGON_OFFSET_FILL)
            gl.glPolygonOffset(-1.0, -1.0)
            gl.glUniform3f(color_loc, 0.0, 0.0, 0.0)
            
            for brush in shadow_casters:
                bx, by, bz = brush['pos']
                sx, sy, sz = brush['size']
                ratio = (by + sy * 0.5 - floor_y) / light_height
                dir_x, dir_z = bx - lx, bz - lz
                dir_len = (dir_x**2 + dir_z**2)**0.5
                if dir_len > 0.001: 
                    dir_x /= dir_len
                    dir_z /= dir_len
                else: 
                    dir_x, dir_z = 1, 0
                total_len = ((bx + dir_x * dir_len * ratio - bx)**2 + (bz + dir_z * dir_len * ratio - bz)**2) ** 0.5
                if total_len < 0.1: 
                    continue
                total_len = min(total_len, light_radius * 0.75)
                brush_diagonal = ((sx*sx + sz*sz) ** 0.5) * 0.5
                shadow_len = total_len + brush_diagonal * 3
                shadow_cx = bx + dir_x * (shadow_len * 0.5 - brush_diagonal)
                shadow_cz = bz + dir_z * (shadow_len * 0.5 - brush_diagonal)
                dist = ((bx-lx)**2 + (by-ly)**2 + (bz-lz)**2)**0.5
                shadow_alpha = max(0.4, min(0.7, 0.6 * (1.0 - (dist / light_radius) * 0.3)))
                self._floor_shadow_batch.add((shadow_cx, shadow_y, shadow_cz), 
                                            (brush_diagonal * 2.5, 0.01, shadow_len), 
                                            glm.atan(dir_x, dir_z), shadow_alpha)
            
            for i in range(self._floor_shadow_batch.count):
                pos = self._floor_shadow_batch.positions[i]
                scale = self._floor_shadow_batch.scales[i]
                rot = self._floor_shadow_batch.rotations[i]
                alpha = self._floor_shadow_batch.alphas[i]
                final_mat = glm.translate(self._identity_mat4, glm.vec3(*pos))
                final_mat = glm.rotate(final_mat, rot, glm.vec3(0, 1, 0))
                final_mat = glm.scale(final_mat, glm.vec3(*scale))
                gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(final_mat))
                gl.glUniform1f(alpha_loc, alpha)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
                self.render_stats.shadow_draw_calls += 1
            
            gl.glDisable(gl.GL_POLYGON_OFFSET_FILL)
            gl.glDisable(gl.GL_STENCIL_TEST)
        
        gl.glDepthFunc(gl.GL_LESS)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_BLEND)
        gl.glBindVertexArray(0)

    # =========================================================================
    # SELECTION / GIZMO / FACE HIGHLIGHT
    # =========================================================================

    def draw_face_highlight(self, projection, view, brush, face_name):
        if 'simple' not in self.shaders: 
            return
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, self._proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, self._view_ptr)
        gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(self._identity_mat4))
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glUniform3f(uniforms['color'], 0.8, 0.2, 0.9) 
        gl.glUniform1f(uniforms['alpha'], 0.4) 
        
        pos, size = brush['pos'], brush['size']
        hx, hy, hz = size[0]/2, size[1]/2, size[2]/2
        cx, cy, cz = pos[0], pos[1], pos[2]
        bias = 0.5 
        
        verts = []
        if face_name == 'north':
            z = cz + hz + bias
            verts = [cx-hx, cy-hy, z,  cx+hx, cy-hy, z,  cx+hx, cy+hy, z,
                     cx-hx, cy-hy, z,  cx+hx, cy+hy, z,  cx-hx, cy+hy, z]
        elif face_name == 'south':
            z = cz - hz - bias
            verts = [cx+hx, cy-hy, z,  cx-hx, cy-hy, z,  cx-hx, cy+hy, z,
                     cx+hx, cy-hy, z,  cx-hx, cy+hy, z,  cx+hx, cy+hy, z]
        elif face_name == 'east':
            x = cx + hx + bias
            verts = [x, cy-hy, cz+hz,  x, cy-hy, cz-hz,  x, cy+hy, cz-hz,
                     x, cy-hy, cz+hz,  x, cy+hy, cz-hz,  x, cy+hy, cz+hz]
        elif face_name == 'west':
            x = cx - hx - bias
            verts = [x, cy-hy, cz-hz,  x, cy-hy, cz+hz,  x, cy+hy, cz+hz,
                     x, cy-hy, cz-hz,  x, cy+hy, cz+hz,  x, cy+hy, cz-hz]
        elif face_name == 'top':
            y = cy + hy + bias
            verts = [cx-hx, y, cz+hz,  cx+hx, y, cz+hz,  cx+hx, y, cz-hz,
                     cx-hx, y, cz+hz,  cx+hx, y, cz-hz,  cx-hx, y, cz-hz]
        elif face_name == 'down':
            y = cy - hy - bias
            verts = [cx-hx, y, cz-hz,  cx+hx, y, cz-hz,  cx+hx, y, cz+hz,
                     cx-hx, y, cz-hz,  cx+hx, y, cz+hz,  cx-hx, y, cz+hz]
            
        if not verts: 
            return

        v_data = np.array(verts, dtype=np.float32)
        
        if not hasattr(self, 'face_highlight_vao'):
            self.face_highlight_vao = gl.glGenVertexArrays(1)
            self.face_highlight_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(self.face_highlight_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.face_highlight_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, 6 * 3 * 4, None, gl.GL_DYNAMIC_DRAW) 
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            gl.glEnableVertexAttribArray(0)
            gl.glBindVertexArray(0)
            
        gl.glBindVertexArray(self.face_highlight_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.face_highlight_vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, v_data.nbytes, v_data)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 6)
        
        gl.glUniform3f(uniforms['color'], 1.0, 1.0, 1.0)
        gl.glUniform1f(uniforms['alpha'], 1.0)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 6)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        
        gl.glBindVertexArray(0)
        gl.glDisable(gl.GL_BLEND)
        gl.glUseProgram(0)

    def draw_selected_brush_outline(self, projection, view, brush):
        if 'simple' not in self.shaders: 
            return
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, self._proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, self._view_ptr)
        model_matrix = glm.scale(glm.translate(self._identity_mat4, glm.vec3(*brush['pos'])), glm.vec3(*brush['size']))
        gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
        gl.glUniform3f(uniforms['color'], 1.0, 1.0, 0.0)
        gl.glUniform1f(uniforms['alpha'], 1.0)
        
        if not hasattr(self, '_edge_vao') or self._edge_vao is None:
            edge_vertices = np.array([
                -0.5, -0.5, -0.5,  0.5, -0.5, -0.5,  0.5, -0.5, -0.5,  0.5, -0.5,  0.5,
                 0.5, -0.5,  0.5, -0.5, -0.5,  0.5, -0.5, -0.5,  0.5, -0.5, -0.5, -0.5,
                -0.5,  0.5, -0.5,  0.5,  0.5, -0.5,  0.5,  0.5, -0.5,  0.5,  0.5,  0.5,
                 0.5,  0.5,  0.5, -0.5,  0.5,  0.5, -0.5,  0.5,  0.5, -0.5,  0.5, -0.5,
                -0.5, -0.5, -0.5, -0.5,  0.5, -0.5,  0.5, -0.5, -0.5,  0.5,  0.5, -0.5,
                 0.5, -0.5,  0.5,  0.5,  0.5,  0.5, -0.5, -0.5,  0.5, -0.5,  0.5,  0.5,
            ], dtype=np.float32)
            self._edge_vao = gl.glGenVertexArrays(1)
            gl.glBindVertexArray(self._edge_vao)
            vbo = gl.glGenBuffers(1)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, edge_vertices.nbytes, edge_vertices, gl.GL_STATIC_DRAW)
            gl.glEnableVertexAttribArray(0)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            gl.glBindVertexArray(0)
        
        gl.glLineWidth(1.0)
        gl.glBindVertexArray(self._edge_vao)
        gl.glDrawArrays(gl.GL_LINES, 0, 24) 
        gl.glBindVertexArray(0)

    # =========================================================================
    # VAO CREATION
    # =========================================================================

    def _create_cube_vao(self):
        vertices = np.array([
            -0.5,-0.5,-0.5, 0,0,-1, 0,0,  0.5,-0.5,-0.5, 0,0,-1, 1,0,  0.5,0.5,-0.5, 0,0,-1, 1,1,
            0.5,0.5,-0.5, 0,0,-1, 1,1,  -0.5,0.5,-0.5, 0,0,-1, 0,1,  -0.5,-0.5,-0.5, 0,0,-1, 0,0,
            -0.5,-0.5,0.5, 0,0,1, 0,0,  0.5,0.5,0.5, 0,0,1, 1,1,  0.5,-0.5,0.5, 0,0,1, 1,0,
            0.5,0.5,0.5, 0,0,1, 1,1,  -0.5,-0.5,0.5, 0,0,1, 0,0,  -0.5,0.5,0.5, 0,0,1, 0,1,
            -0.5,0.5,0.5, -1,0,0, 1,0,  -0.5,-0.5,-0.5, -1,0,0, 0,1,  -0.5,0.5,-0.5, -1,0,0, 1,1,
            -0.5,-0.5,-0.5, -1,0,0, 0,1,  -0.5,0.5,0.5, -1,0,0, 1,0,  -0.5,-0.5,0.5, -1,0,0, 0,0,
            0.5,0.5,0.5, 1,0,0, 1,0,  0.5,0.5,-0.5, 1,0,0, 1,1,  0.5,-0.5,-0.5, 1,0,0, 0,1,
            0.5,-0.5,-0.5, 1,0,0, 0,1,  0.5,-0.5,0.5, 1,0,0, 0,0,  0.5,0.5,0.5, 1,0,0, 1,0,
            -0.5,-0.5,-0.5, 0,-1,0, 0,1,  0.5,-0.5,0.5, 0,-1,0, 1,0,  0.5,-0.5,-0.5, 0,-1,0, 1,1,
            0.5,-0.5,0.5, 0,-1,0, 1,0,  -0.5,-0.5,-0.5, 0,-1,0, 0,1,  -0.5,-0.5,0.5, 0,-1,0, 0,0,
            -0.5,0.5,-0.5, 0,1,0, 0,1,  0.5,0.5,-0.5, 0,1,0, 1,1,  0.5,0.5,0.5, 0,1,0, 1,0,
            0.5,0.5,0.5, 0,1,0, 1,0,  -0.5,0.5,0.5, 0,1,0, 0,0,  -0.5,0.5,-0.5, 0,1,0, 0,1
        ], dtype=np.float32)
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
        vbo = gl.glGenBuffers(1)
        gl.glBindVertexArray(self.vao_gizmo_lines)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, axis_verts.nbytes, axis_verts, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 12, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        cone_verts = []
        for i in range(12):
            t1, t2 = (i/12)*2*np.pi, ((i+1)/12)*2*np.pi
            cone_verts.extend([0,0,0, np.cos(t2)*0.05,0,np.sin(t2)*0.05, np.cos(t1)*0.05,0,np.sin(t1)*0.05])
            cone_verts.extend([0,0.2,0, np.cos(t1)*0.05,0,np.sin(t1)*0.05, np.cos(t2)*0.05,0,np.sin(t2)*0.05])
        self.gizmo_cone_v_count = len(cone_verts)//3
        cone_verts = np.array(cone_verts, dtype=np.float32)
        self.vao_gizmo_cone = gl.glGenVertexArrays(1)
        vbo2 = gl.glGenBuffers(1)
        gl.glBindVertexArray(self.vao_gizmo_cone)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo2)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, cone_verts.nbytes, cone_verts, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        gl.glBindVertexArray(0)

    def render_gizmo(self, projection, view, position):
        if 'simple' not in self.shaders: 
            return
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, self._proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, self._view_ptr)
        pos_vec = glm.vec3(*position) if isinstance(position, (list, tuple)) else position
        base = glm.scale(glm.translate(self._identity_mat4, pos_vec), glm.vec3(32.0))
        model_loc, color_loc = uniforms['model'], uniforms['color']
        gl.glUniform1f(uniforms['alpha'], 1.0)
        
        gl.glBindVertexArray(self.vao_gizmo_lines)
        gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(base))
        for i, c in enumerate([(1,0,0), (0,1,0), (0,0,1)]):
            gl.glUniform3f(color_loc, *c)
            gl.glDrawArrays(gl.GL_LINES, i*2, 2)
        gl.glBindVertexArray(self.vao_gizmo_cone)
        for axis, c, rot in [((1,0,0), (1,0,0), glm.rotate(base, glm.radians(-90), glm.vec3(0,0,1))), 
                            ((0,1,0), (0,1,0), base), 
                            ((0,0,1), (0,0,1), glm.rotate(base, glm.radians(90), glm.vec3(1,0,0)))]:
            m = glm.translate(rot if axis[1] else glm.translate(base, glm.vec3(*axis)), 
                             glm.vec3(0,1,0) if axis[1] else glm.vec3(0,0,0))
            if axis[0]: 
                m = glm.translate(glm.rotate(base, glm.radians(-90), glm.vec3(0,0,1)), glm.vec3(0,1,0))
            if axis[2]: 
                m = glm.translate(glm.rotate(base, glm.radians(90), glm.vec3(1,0,0)), glm.vec3(0,1,0))
            if axis[1]: 
                m = glm.translate(base, glm.vec3(0,1,0))
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(m))
            gl.glUniform3f(color_loc, *c)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, self.gizmo_cone_v_count)
        gl.glBindVertexArray(0)
