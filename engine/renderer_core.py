"""
engine/renderer_core.py  –  Base renderer with shared logic for Forward/Deferred

Provides:
    • Texture management (load_texture, preload_level_textures)
    • Grid drawing (update_grid_buffers, draw_grid)
    • Sprite rendering (draw_sprites, with per‑instance textures)
    • Model loading & drawing (draw_models)
    • Water / Glass / Fog volume rendering
    • Terrain rendering
    • Editor helpers (gizmo, selection outline, face highlight, connection lines,
      path node cubes, portal wireframes)
    • Projected shadows
    • Sorting / splitting helpers
    • VAO creation for cube, sprite, grid, gizmo, etc.
    • Shader management (compilation, hot‑reload, light upload)

Both Renderer_F and Renderer_D inherit from BaseRenderer.
"""

import ctypes
import math
import os
from collections import defaultdict

import glm
import numpy as np
import OpenGL.GL as gl
from OpenGL.GL.shaders import compileProgram, compileShader

from engine.constants import RENDER_MODE_LIT, RENDER_MODE_UNLIT, RENDER_MODE_WIREFRAME, RENDER_MODE_VERTEX, is_water_brush
from engine import brush_geometry
from engine.shaders import DEFAULT_SHADERS
from engine.terrain import TERRAIN_VERTEX_SHADER, TERRAIN_FRAGMENT_SHADER
from editor.things import (
    Thing, PathNode, Portal, Pickup, Monster, LogicGate, LogicRelay,
    LogicTimer, LevelChanger, Light, LogicSpawner, LogicCamera,
)

# Try to import OBJ and GLB loaders
try:
    from .obj_loader import OBJ
except ImportError:
    OBJ = None

try:
    from .glb_loader import GLB
except ImportError:
    GLB = None


# ---------- Utility classes ----------
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
        if dist_sq < self.full_dist_sq:
            return self.LOD_FULL
        elif dist_sq < self.cull_dist_sq:
            return self.LOD_REDUCED
        return self.LOD_CULLED


class RenderStats:
    __slots__ = ('total_brushes', 'culled_brushes', 'visible_brushes', 'draw_calls',
                 'shadow_draw_calls', 'total_tris', 'visible_tris', 'batched_draws')
    def __init__(self):
        self.reset()
    def reset(self):
        self.total_brushes = self.culled_brushes = self.visible_brushes = 0
        self.draw_calls = self.shadow_draw_calls = self.batched_draws = 0
        self.total_tris = self.visible_tris = 0


class BrushGeoMesh:
    """GPU mesh for one angled (convex-geometry) brush.

    Vertices are stored in the brush's local unit-cube space — world
    coordinates mapped through the brush's AABB (``pos``/``size``, which
    ``brush_geometry.sync_brush_bounds`` keeps in sync with the plane set).
    Every existing draw path can therefore keep its translate*scale model
    matrix, per-brush uniforms and shaders unchanged: the mesh simply binds
    in place of the shared unit-cube VAO.

    ``runs`` holds one entry per face (dicts with ``face`` tag, plane
    ``texture``/``uv_scale``, ``first``/``count`` vertex range and the face's
    projected world-space ``extent``) so the textured path can draw each face
    with its own texture, exactly like the per-face cube batches.

    Faces are ordered sides-first, any flat top face last (``side_count``
    marks the split) so the water path can draw walls and surface separately,
    mirroring its box path.
    """
    __slots__ = ('vao', 'vbo', 'edge_vao', 'edge_vbo', 'count', 'side_count',
                 'edge_count', 'runs', 'has_flat_top', 'key', 'frame')

    def __init__(self):
        self.vao = self.vbo = self.edge_vao = self.edge_vbo = None
        self.count = self.side_count = self.edge_count = 0
        self.runs = []
        self.has_flat_top = False
        self.key = None
        self.frame = 0


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
                program = compileProgram(vs, fs, gs, validate=False)
            else:
                program = compileProgram(vs, fs, validate=False)
            return program
        except Exception as e:
            print(f"Error compiling shader ({vertex_file}, {fragment_file}): {e}")
            raise

    def compile_from_source(self, vertex_src, fragment_src):
        try:
            vs = compileShader(vertex_src, gl.GL_VERTEX_SHADER)
            fs = compileShader(fragment_src, gl.GL_FRAGMENT_SHADER)
            return compileProgram(vs, fs, validate=False)
        except Exception as e:
            print(f"Error compiling shader from source: {e}")
            raise


# ---------- Helper ----------
def normalize_color(rgb, default=None):
    """Normalise an RGB colour to 0.0-1.0 floats.
    Accepts [0-255] int or [0.0-1.0] float components.
    Returns *default* (or [0.8, 0.8, 0.8]) if rgb is None or malformed.
    """
    if default is None:
        default = [0.8, 0.8, 0.8]
    if not rgb or not isinstance(rgb, (list, tuple)) or len(rgb) < 3:
        return list(default)
    return [c / 255.0 if c > 1.0 else c for c in rgb[:3]]


# ---------- Base Renderer ----------
class BaseRenderer:
    MAX_LIGHTS = 32
    MAX_PORTALS = 4      # maximum portal apertures rendered per frame

    # How many times a portal may be seen recursively through another portal.
    # 1 = classic single virtual view (default; identical to the original
    # behaviour). Raise to 2-3 for a bounded "infinite corridor" effect — that
    # path is wired up but costs an extra full scene pass per level of depth, so
    # verify performance/appearance in-engine before shipping it enabled.
    MAX_PORTAL_RECURSION = 1

    # Within this many world units of a portal plane the aperture mask is drawn
    # across the screen instead of as world geometry, so the camera's near plane
    # can't clip the mask and reveal the wall behind the portal during the last
    # step before transit. Set to 0.0 to disable (exact original behaviour).
    PORTAL_NEAR_STRADDLE = 24.0

    # --- Depth cube-map shadow mapping (omnidirectional point-light shadows) ---
    MAX_SHADOW_LIGHTS = 8          # number of point lights that can cast shadows at once
    SHADOW_MAP_SIZE = 384         # per-face resolution of each depth cube-map
    SHADOW_TEXTURE_UNIT_BASE = 4   # shadow cube-maps bind to units 4..(4+MAX_SHADOW_LIGHTS-1)

    def __init__(self, texture_loader, initial_grid_size, initial_world_size, config=None):
        self.texture_manager = {}
        self.loaded_models = {}
        self.load_texture_callback = texture_loader
        self._identity_mat4 = glm.mat4(1.0)
        self._identity_mat3 = glm.mat3(1.0)
        self.render_stats = RenderStats()
        self.lod_manager = LODManager()

        # Performance flags
        is_arm = self._detect_arm_platform()
        if config is not None:
            self.arm_mode = config.getboolean('Renderer', 'arm_mode', fallback=True)
            self.shadows_enabled = config.getboolean('Renderer', 'shadows_enabled', fallback=not is_arm)
            try:
                shadow_size = config.getint('Renderer', 'shadow_map_size', fallback=self.SHADOW_MAP_SIZE)
            except Exception:
                shadow_size = self.SHADOW_MAP_SIZE
        else:
            self.arm_mode = True
            self.shadows_enabled = not is_arm
            shadow_size = self.SHADOW_MAP_SIZE
        # Clamp to a sane, power-of-two-ish range. Lower = faster, blockier.
        self.shadow_map_size = max(256, min(2048, int(shadow_size)))

        self.fog_quality = 'low'      # 'low' = 16 steps, 'high' = 32 steps
        self.skip_culling_in_renderer = True   # trust pre‑culled data

        self._model_matrix = glm.mat4(1.0)

        # Depth cube-map shadow-mapping state (created lazily once GL is ready).
        self._shadow_fbo = None
        self._shadow_cubemaps = []          # texture ids, one cube-map per shadow slot
        self._light_shadow_index = {}       # id(light) -> shadow slot index for this frame
        # Per-slot cache so a light's cube-map is only re-rendered when it (or one
        # of its in-range casters) actually moves — static lights become ~free.
        self._shadow_slot_owner = [None] * self.MAX_SHADOW_LIGHTS   # id(light) per slot
        self._shadow_slot_sig = [None] * self.MAX_SHADOW_LIGHTS     # last-rendered signature

        # Per‑frame caches
        self._frame_lights = []
        # shader_name -> tuple of light ids uploaded this frame; cleared at
        # the start of every render_scene() so animated lights stay fresh.
        self._frame_lights_uploaded = {}
        self._current_shader = None

        # PERF: memoized monster-sprite texture-key strings, keyed by the
        # (type, variant, sprite_type, custom) tuple that determines them —
        # avoids rebuilding the same f-string every frame for every visible
        # monster (draw_sprites runs once per visible monster per frame).
        self._sprite_tex_key_cache = {}

        # PERF: precomputed GLSL uniform names for each light slot. Building
        # these f-strings on the hot path meant up to MAX_LIGHTS*5 string
        # allocations per shader per frame inside _upload_lights_once; the
        # names never change, so build them once here.
        self._light_uniform_names = [
            ('lights[%d].position' % i, 'lights[%d].color' % i,
             'lights[%d].intensity' % i, 'lights[%d].radius' % i,
             'lights[%d].shadowIndex' % i)
            for i in range(self.MAX_LIGHTS)
        ]

        # PERF: cache of texture-name -> "textures/<name>" cache-key path.
        # draw_textured_brushes_optimized resolves this for every drawn face
        # every frame in play mode; os.path.join is comparatively expensive,
        # so memoize the join per unique texture name.
        self._tex_path_cache = {}

        self._proj_ptr = None
        self._view_ptr = None

        # VAOs and buffers (initialised after shaders compile)
        self.vaos = {'cube': None, 'sprite': None, 'grid': None}
        self.grid_indices_count = 0
        self.sprite_textures = {}
        self.instance_textures = {}
        self._edge_vao = None
        self._edge_vbo = None
        self._gizmo_lines_vbo = None
        self._gizmo_cone_vbo = None
        self._portal_outline_vao = None
        self._portal_outline_vbo = None
        self._portal_normal_vao = None
        self._portal_normal_vbo = None
        self._conn_line_vao = None
        self._conn_line_vbo = None
        self.face_highlight_vao = None
        self.face_highlight_vbo = None
        self._cube_vbo = None
        self._sprite_vbo = None
        self._grid_vbo = None
        self._water_surface_vbo = None
        self._water_surface_ebo = None
        self._water_surface_index_count = 0

        # Angled-brush (convex geometry) meshes, keyed by id(brush).  Entries
        # are rebuilt when a brush's plane set changes and dropped after going
        # unused for a while (see _begin_geo_frame).
        self._geo_mesh_cache = {}
        self._geo_mesh_frame = 0

        self._shader_init_failed = False

        # Shaders (will be filled by subclasses or base helpers)
        self.shaders = {}
        self.uniforms = {}

        # Portal specific GL resources (initialised later)
        self._portal_mask_shader = None
        self._portal_rim_shader = None
        self._portal_quad_vao = None
        self._portal_quad_vbo = None
        self._portal_gl_ready = False
        # True only while the opaque brush passes are drawing a portal's virtual
        # scene. The oblique near-plane clip slices solid brushes open at the
        # destination portal, so the brush passes enable back-face culling for
        # this flag to hide the exposed interior faces (see the brush draws).
        self._portal_scene_pass = False
        self._portal_mask_proj_loc = None
        self._portal_mask_view_loc = None
        self._portal_rim_proj_loc = None
        self._portal_rim_view_loc = None
        self._portal_rim_color_loc = None
        # Cached inverse of the main projection matrix, reused across every
        # oblique-clip computation in a frame (the projection is constant, only
        # the per-portal view changes).
        self._portal_proj_inv_sig = None
        self._portal_proj_inv = None

        # Compile common shaders (simple, sprite, depth_cube, water, glass, fog, terrain)
        self.shader_loader = ShaderLoader()
        self._compile_common_shaders()

        # Terrain normal map (water)
        self.water_normal_id = self.load_texture('water_normal.png', 'textures')
        self.noise_texture_id = 0

        # Create VAOs after shaders are ready
        if not self._shader_init_failed:
            self.vaos['cube'] = self._create_cube_vao()
            self.vaos['water_surface'] = self._create_water_surface_vao()
            self.vaos['sprite'] = self._create_sprite_vao()
            self.vaos['grid'] = None
            self.update_grid_buffers(initial_world_size, initial_grid_size)
            self._create_gizmo_buffers()
            self.noise_texture_id = self._load_3d_texture('assets/noise_3d.bin')
            self.load_texture('default.png', 'textures')
            self.load_texture('caulk', 'textures')
            self._init_portal_gl()
            self._init_shadow_resources()

    # --------------------------------------------------------------------------
    # Platform detection
    # --------------------------------------------------------------------------
    def _detect_arm_platform(self):
        import platform
        import sys
        machine = platform.machine().lower()
        if 'arm' in machine or 'aarch' in machine:
            return True
        if sys.platform == 'win32':
            if os.environ.get('PROCESSOR_ARCHITECTURE', '').upper() == 'ARM64':
                return True
            if os.environ.get('PROCESSOR_ARCHITEW6432', '').upper() == 'ARM64':
                return True
            proc_id = os.environ.get('PROCESSOR_IDENTIFIER', '').lower()
            if 'qualcomm' in proc_id or 'snapdragon' in proc_id or 'arm' in proc_id:
                return True
        return False

    # --------------------------------------------------------------------------
    # Shader compilation helpers
    # --------------------------------------------------------------------------
    def _compile_common_shaders(self):
        """Compile shaders that are shared by both forward and deferred paths."""
        try:
            # simple (for grid, outlines, lines)
            vs_src = DEFAULT_SHADERS.get('simple.vert', '')
            fs_src = DEFAULT_SHADERS.get('simple.frag', '')
            self.shaders['simple'] = self.shader_loader.compile_from_source(vs_src, fs_src)
            self.uniforms['simple'] = UniformCache(self.shaders['simple'])
            self.uniforms['simple'].preload(['projection', 'view', 'model', 'color', 'alpha'])

            # sprite (billboards)
            vs_src = DEFAULT_SHADERS.get('sprite.vert', '')
            fs_src = DEFAULT_SHADERS.get('sprite.frag', '')
            self.shaders['sprite'] = self.shader_loader.compile_from_source(vs_src, fs_src)
            self.uniforms['sprite'] = UniformCache(self.shaders['sprite'])
            self.uniforms['sprite'].preload(['projection', 'view', 'sprite_texture', 'sprite_pos_world', 'sprite_size'])

            # depth_cube – renders scene depth into a point light's cube-map for
            # omnidirectional shadow mapping (replaces the old projected shadows).
            vs_src = DEFAULT_SHADERS.get('depth_cube.vert', '')
            fs_src = DEFAULT_SHADERS.get('depth_cube.frag', '')
            self.shaders['depth_cube'] = self.shader_loader.compile_from_source(vs_src, fs_src)
            self.uniforms['depth_cube'] = UniformCache(self.shaders['depth_cube'])
            self.uniforms['depth_cube'].preload(['model', 'lightSpaceMatrix', 'lightPos', 'far_plane'])

            # water
            vs_src = DEFAULT_SHADERS.get('water.vert', '')
            fs_src = DEFAULT_SHADERS.get('water.frag', '')
            self.shaders['water'] = self.shader_loader.compile_from_source(vs_src, fs_src)
            self.uniforms['water'] = UniformCache(self.shaders['water'])
            self._preload_water_uniforms()

            # glass
            vs_src = DEFAULT_SHADERS.get('glass.vert', '')
            fs_src = DEFAULT_SHADERS.get('glass.frag', '')
            self.shaders['glass'] = self.shader_loader.compile_from_source(vs_src, fs_src)
            self.uniforms['glass'] = UniformCache(self.shaders['glass'])
            self.uniforms['glass'].preload(['projection', 'view', 'model', 'viewPos', 'waterColor',
                                            'distortionStrength', 'causticStrength', 'glassOpacity',
                                            'refractionIndex', 'roughness', 'normalMatrix'])

            # fog – use ARM‑optimised fragment shader (works everywhere)
            fog_vert = DEFAULT_SHADERS.get('fog.vert', '')
            fog_frag = DEFAULT_SHADERS.get('fog_arm.frag', DEFAULT_SHADERS.get('fog.frag', ''))
            self.shaders['fog'] = self.shader_loader.compile_from_source(fog_vert, fog_frag)
            self.uniforms['fog'] = UniformCache(self.shaders['fog'])
            self._preload_fog_uniforms()

            # terrain
            try:
                terrain_vs = compileShader(TERRAIN_VERTEX_SHADER, gl.GL_VERTEX_SHADER)
                terrain_fs = compileShader(TERRAIN_FRAGMENT_SHADER, gl.GL_FRAGMENT_SHADER)
                terrain_program = compileProgram(terrain_vs, terrain_fs, validate=False)
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

            # lit and textured shaders (needed for forward fallback in Deferred)
            if self.arm_mode:
                self._compile_arm_shaders()
            else:
                self._compile_standard_shaders()

            print("Base renderer shaders compiled successfully.")
        except Exception as e:
            print(f"FATAL: Shader Error in BaseRenderer: {e}")
            self._shader_init_failed = True

    def _compile_arm_shaders(self):
        lit_vert = DEFAULT_SHADERS.get('lit_arm.vert', '')
        lit_frag = DEFAULT_SHADERS.get('lit_arm.frag', '')
        lit_shader = self.shader_loader.compile_from_source(lit_vert, lit_frag)
        self.shaders['lit'] = lit_shader
        self.uniforms['lit'] = UniformCache(lit_shader)
        self._preload_lit_uniforms('lit')
        self.uniforms['lit'].preload(['normalMatrix'])

        tex_vert = DEFAULT_SHADERS.get('textured_arm.vert', '')
        tex_frag = DEFAULT_SHADERS.get('textured_arm.frag', '')
        tex_shader = self.shader_loader.compile_from_source(tex_vert, tex_frag)
        self.shaders['textured'] = tex_shader
        self.uniforms['textured'] = UniformCache(tex_shader)
        self._preload_lit_uniforms('textured')
        self.uniforms['textured'].preload(['texture_diffuse', 'tex_scale', 'tex_angle', 'tex_shift', 'normalMatrix'])

    def _compile_standard_shaders(self):
        lit_shader = self.shader_loader.compile_shader_program('lit.vert', 'lit.frag')
        self.shaders['lit'] = lit_shader
        self.uniforms['lit'] = UniformCache(lit_shader)
        self._preload_lit_uniforms('lit')
        self.uniforms['lit'].preload(['normalMatrix'])

        tex_shader = self.shader_loader.compile_shader_program('textured.vert', 'textured.frag')
        self.shaders['textured'] = tex_shader
        self.uniforms['textured'] = UniformCache(tex_shader)
        self._preload_lit_uniforms('textured')
        self.uniforms['textured'].preload(['texture_diffuse', 'tex_scale', 'tex_angle', 'tex_shift', 'normalMatrix'])

    def _preload_lit_uniforms(self, shader_name):
        uniforms = self.uniforms[shader_name]
        uniforms.preload(['projection', 'view', 'model', 'object_color', 'alpha', 'active_lights'])
        for i in range(self.MAX_LIGHTS):
            uniforms.preload([f'lights[{i}].position', f'lights[{i}].color',
                              f'lights[{i}].intensity', f'lights[{i}].radius'])

    def _preload_water_uniforms(self):
        uniforms = self.uniforms['water']
        uniforms.preload(['projection', 'view', 'model', 'time', 'viewPos', 'normalMap', 'waterOpacity',
                          'waterReflectivity', 'waterTint', 'normalMatrix', 'waveAmp', 'brushSize'])

    def _preload_fog_uniforms(self):
        uniforms = self.uniforms['fog']
        uniforms.preload(['projection', 'view', 'model', 'viewPos', 'time', 'noiseTexture',
                          'density', 'fogColor', 'noiseScale', 'object_color', 'alpha', 'inverseModel'])

    # --------------------------------------------------------------------------
    # Texture management
    # --------------------------------------------------------------------------
    def load_texture(self, texture_name, subfolder):
        tex_cache_name = os.path.join(subfolder, texture_name)
        if tex_cache_name in self.texture_manager:
            return self.texture_manager[tex_cache_name]

        # Initialize texture dimensions storage
        if not hasattr(self, '_texture_dimensions'):
            self._texture_dimensions = {}

        if texture_name == 'default.png':
            tex_id = gl.glGenTextures(1)
            self.texture_manager[tex_cache_name] = tex_id
            self._texture_dimensions[tex_cache_name] = (1, 1)
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, 1, 1, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE,
                           (gl.GLubyte * 4)(255, 255, 255, 255))
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
            return tex_id

        if texture_name == 'caulk':
            tex_id = gl.glGenTextures(1)
            self.texture_manager[tex_cache_name] = tex_id
            self._texture_dimensions[tex_cache_name] = (2, 2)
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
            from PIL import Image
            img = Image.open(texture_path).convert("RGBA")
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
            tex_id = gl.glGenTextures(1)
            self.texture_manager[tex_cache_name] = tex_id
            self._texture_dimensions[tex_cache_name] = (img.width, img.height)
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
            # Angled brushes can carry per-plane textures (e.g. on cut faces).
            geo = brush.get('geometry')
            if geo:
                for plane in geo.get('planes', []):
                    tex = plane.get('texture')
                    if tex and tex != 'caulk.jpg':
                        texture_set.add(tex)
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
            gl.glTexImage3D(gl.GL_TEXTURE_3D, 0, gl.GL_R8, size, size, size, 0,
                            gl.GL_RED, gl.GL_UNSIGNED_BYTE, data)
            return texture_id
        except Exception:
            return 0

    # --------------------------------------------------------------------------
    # Grid
    # --------------------------------------------------------------------------
    def update_grid_buffers(self, world_size, grid_size):
        if self.vaos.get('grid') is None and self.vaos.get('cube') is None:
            if grid_size <= 0 or self._shader_init_failed:
                return
        if grid_size <= 0:
            if self.vaos['grid']:
                gl.glDeleteVertexArrays(1, [self.vaos['grid']])
                if hasattr(self, '_grid_vbo') and self._grid_vbo:
                    gl.glDeleteBuffers(1, [self._grid_vbo])
                    self._grid_vbo = None
                self.vaos['grid'] = None
            return
        s, g = world_size, grid_size
        lines = [[-s, 0, i, s, 0, i, i, 0, -s, i, 0, s] for i in range(-s, s+1, g)]
        grid_vertices = np.array(lines, dtype=np.float32).flatten()
        self.grid_indices_count = len(grid_vertices) // 3
        if self.vaos['grid']:
            gl.glDeleteVertexArrays(1, [self.vaos['grid']])
        if hasattr(self, '_grid_vbo') and self._grid_vbo:
            gl.glDeleteBuffers(1, [self._grid_vbo])
        vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(vao)
        vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, grid_vertices.nbytes, grid_vertices, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        gl.glBindVertexArray(0)
        self._grid_vbo = vbo
        self.vaos['grid'] = vao

    def draw_grid(self, projection, view, grid_indices_count, play_mode=False, grid_visible=True):
        if not self.vaos['grid'] or play_mode or not grid_visible or 'simple' not in self.shaders:
            return
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(self._identity_mat4))
        gl.glUniform3f(uniforms['color'], 0.2, 0.2, 0.2)
        gl.glUniform1f(uniforms['alpha'], 1.0)
        gl.glBindVertexArray(self.vaos['grid'])
        gl.glDrawArrays(gl.GL_LINES, 0, grid_indices_count)
        gl.glBindVertexArray(0)

    # --------------------------------------------------------------------------
    # Terrain
    # --------------------------------------------------------------------------
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
        terrain.update_and_render(
            projection, view, camera_pos, frustum_planes, lights, active_lights_count,
            shadow_cubemaps=(self._shadow_cubemaps if self.shadows_enabled else None),
            shadow_index_map=self._light_shadow_index,
            shadow_unit_base=self.SHADOW_TEXTURE_UNIT_BASE,
        )

    # --------------------------------------------------------------------------
    # Models
    # --------------------------------------------------------------------------
    def load_model(self, filename):
        """Load a 3D model (OBJ or GLB)."""
        if filename in self.loaded_models:
            return self.loaded_models[filename]

        full_path = os.path.join('assets', 'models', filename)
        if not os.path.exists(full_path):
            full_path = filename

        if not os.path.exists(full_path):
            print(f"Failed to load model: {filename}")
            return None

        print(f"Loading model: {full_path}")

        # Determine format by extension
        ext = os.path.splitext(filename)[1].lower()

        if ext == '.glb':
            if GLB is None:
                print(f"[Renderer] GLB support not available (glb_loader not found)")
                return None
            model = GLB(full_path)
        elif ext in ('.obj', ''):
            if OBJ is None:
                print(f"[Renderer] OBJ support not available (obj_loader not found)")
                return None
            model = OBJ(full_path)
        else:
            print(f"[Renderer] Unsupported model format: {ext}")
            return None

        if model.is_loaded:
            self.loaded_models[filename] = model
            return model

        print(f"Failed to load model: {filename}")
        return None

    def draw_models(self, projection, view, camera_pos, models, lights, config):
            if not models:
                return

            lit_shader = self.shaders.get('lit')
            textured_shader = self.shaders.get('textured')
            current_shader = None
            cull_was_enabled = gl.glIsEnabled(gl.GL_CULL_FACE)
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
                        material = obj.materials.get(mat_name, {'color': [0.8,0.8,0.8], 'texture': None})
                        use_texture = material.get('texture')
                        if use_texture and textured_shader:
                            if current_shader != textured_shader:
                                gl.glUseProgram(textured_shader)
                                current_shader = textured_shader
                                u = self.uniforms['textured']
                                gl.glUniformMatrix4fv(u['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
                                gl.glUniformMatrix4fv(u['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
                                self._upload_lights_once('textured', lights)
                                gl.glActiveTexture(gl.GL_TEXTURE0)
                                gl.glUniform1i(u['texture_diffuse'], 0)
                                # Models use their own UVs — clear any brush
                                # face transform left in the shared uniforms.
                                if u.get('tex_angle', -1) != -1:
                                    gl.glUniform1f(u['tex_angle'], 0.0)
                                if u.get('tex_shift', -1) != -1:
                                    gl.glUniform2f(u['tex_shift'], 0.0, 0.0)
                            # Resolve texture path relative to MTL directory first
                            resolved_path = self._resolve_model_texture_path(material, use_texture)
                            if resolved_path and os.path.exists(resolved_path):
                                # Load from resolved absolute path
                                tex_cache_name = f"model_tex:{resolved_path}"
                                if tex_cache_name in self.texture_manager:
                                    tex_id = self.texture_manager[tex_cache_name]
                                else:
                                    from PIL import Image
                                    img = Image.open(resolved_path).convert("RGBA")
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
                            else:
                                tex_id = self.load_texture(use_texture, 'textures')
                            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                            gl.glUniformMatrix4fv(self.uniforms['textured']['model'], 1, gl.GL_FALSE, glm.value_ptr(mat))
                            # Upload normal matrix for correct lighting
                            normal_mat = self._compute_normal_matrix(mat)
                            normal_mat_loc = self.uniforms['textured'].get('normalMatrix', -1)
                            if normal_mat_loc >= 0:
                                gl.glUniformMatrix3fv(normal_mat_loc, 1, gl.GL_FALSE, glm.value_ptr(normal_mat))
                        elif lit_shader:
                            if current_shader != lit_shader:
                                gl.glUseProgram(lit_shader)
                                current_shader = lit_shader
                                u = self.uniforms['lit']
                                gl.glUniformMatrix4fv(u['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
                                gl.glUniformMatrix4fv(u['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
                                self._upload_lights_once('lit', lights)
                            color = material.get('color', [0.8,0.8,0.8])
                            gl.glUniform3fv(self.uniforms['lit']['object_color'], 1, color)
                            gl.glUniform1f(self.uniforms['lit']['alpha'], 1.0)
                            gl.glUniformMatrix4fv(self.uniforms['lit']['model'], 1, gl.GL_FALSE, glm.value_ptr(mat))
                            # Upload normal matrix for correct lighting
                            normal_mat = self._compute_normal_matrix(mat)
                            normal_mat_loc = self.uniforms['lit'].get('normalMatrix', -1)
                            if normal_mat_loc >= 0:
                                gl.glUniformMatrix3fv(normal_mat_loc, 1, gl.GL_FALSE, glm.value_ptr(normal_mat))
                        # Draw the group - indexed or non-indexed
                        if group.get('indexed', False) and getattr(obj, 'ebo', None) is not None:
                            gl.glDrawElements(gl.GL_TRIANGLES, group['count'], gl.GL_UNSIGNED_INT,
                                            ctypes.c_void_p(group['start'] * 4))
                        else:
                            gl.glDrawArrays(gl.GL_TRIANGLES, group['start'], group['count'])
                else:
                    tex_name = manual_texture
                    target_shader = textured_shader if tex_name else lit_shader
                    if target_shader == textured_shader:
                        if current_shader != textured_shader:
                            gl.glUseProgram(textured_shader)
                            current_shader = textured_shader
                            u = self.uniforms['textured']
                            gl.glUniformMatrix4fv(u['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
                            gl.glUniformMatrix4fv(u['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
                            self._upload_lights_once('textured', lights)
                            gl.glActiveTexture(gl.GL_TEXTURE0)
                            gl.glUniform1i(u['texture_diffuse'], 0)
                            # Models use their own UVs — clear any brush face
                            # transform left in the shared uniforms.
                            if u.get('tex_angle', -1) != -1:
                                gl.glUniform1f(u['tex_angle'], 0.0)
                            if u.get('tex_shift', -1) != -1:
                                gl.glUniform2f(u['tex_shift'], 0.0, 0.0)
                        resolved_path = self._resolve_model_texture_path({'texture': tex_name}, tex_name)
                        if resolved_path and os.path.exists(resolved_path) and not resolved_path.startswith('assets'):
                            # Load from resolved absolute path
                            tex_cache_name = f"model_tex:{resolved_path}"
                            if tex_cache_name in self.texture_manager:
                                tex_id = self.texture_manager[tex_cache_name]
                            else:
                                from PIL import Image
                                img = Image.open(resolved_path).convert("RGBA")
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
                        else:
                            tex_id = self.load_texture(tex_name, 'textures')
                        gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                        gl.glUniformMatrix4fv(self.uniforms['textured']['model'], 1, gl.GL_FALSE, glm.value_ptr(mat))
                        # Upload normal matrix for correct lighting
                        normal_mat = self._compute_normal_matrix(mat)
                        normal_mat_loc = self.uniforms['textured'].get('normalMatrix', -1)
                        if normal_mat_loc >= 0:
                            gl.glUniformMatrix3fv(normal_mat_loc, 1, gl.GL_FALSE, glm.value_ptr(normal_mat))
                    elif lit_shader:
                        if current_shader != lit_shader:
                            gl.glUseProgram(lit_shader)
                            current_shader = lit_shader
                            u = self.uniforms['lit']
                            gl.glUniformMatrix4fv(u['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
                            gl.glUniformMatrix4fv(u['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
                            self._upload_lights_once('lit', lights)
                        col = thing.properties.get('color', [0.8, 0.8, 0.8])
                        gl.glUniform3fv(self.uniforms['lit']['object_color'], 1, col)
                        gl.glUniform1f(self.uniforms['lit']['alpha'], 1.0)
                        gl.glUniformMatrix4fv(self.uniforms['lit']['model'], 1, gl.GL_FALSE, glm.value_ptr(mat))
                        # Upload normal matrix for correct lighting
                        normal_mat = self._compute_normal_matrix(mat)
                        normal_mat_loc = self.uniforms['lit'].get('normalMatrix', -1)
                        if normal_mat_loc >= 0:
                            gl.glUniformMatrix3fv(normal_mat_loc, 1, gl.GL_FALSE, glm.value_ptr(normal_mat))
                    gl.glDrawArrays(gl.GL_TRIANGLES, 0, obj.vertex_count)
                self.render_stats.draw_calls += 1

            gl.glBindVertexArray(0)
            if cull_was_enabled:
                gl.glEnable(gl.GL_CULL_FACE)
            else:
                gl.glDisable(gl.GL_CULL_FACE)

    def set_instance_textures(self, textures):
        self.instance_textures = textures

    def draw_sprites(self, projection, view, things_to_draw, sprite_textures, instance_textures=None):
        if not things_to_draw or 'sprite' not in self.shaders:
            return

        shader, uniforms = self.shaders['sprite'], self.uniforms['sprite']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(uniforms['sprite_texture'], 0)
        pos_loc, size_loc = uniforms['sprite_pos_world'], uniforms['sprite_size']
        gl.glBindVertexArray(self.vaos['sprite'])

        current_tex = None
        for thing in things_to_draw:
            if Portal is not None and isinstance(thing, Portal):
                continue

            # Monster snapshot dict
            if isinstance(thing, dict) and 'dead' in thing:
                if thing.get('dead'):
                    custom = thing.get('custom_dead', '')
                    sprite_type = 'dead'
                elif thing.get('is_shooting'):
                    custom = thing.get('custom_shoot', '')
                    sprite_type = 'shoot'
                else:
                    custom = thing.get('custom_idle', '')
                    sprite_type = 'idle'

                mtype = thing.get('monster_type', 'human')
                variant = thing.get('variant', '<None>')
                key_tuple = (mtype, variant, sprite_type, custom)
                tex_key = self._sprite_tex_key_cache.get(key_tuple)
                if tex_key is None:
                    tex_key = f"msprite_{mtype}_{variant}_{sprite_type}_{custom}"
                    self._sprite_tex_key_cache[key_tuple] = tex_key
                tex_id = sprite_textures.get(tex_key)
                if tex_id is None:
                    if custom:
                        custom_clean = custom.replace('assets/', '', 1)
                        subfolder = os.path.dirname(custom_clean)
                        filename = os.path.basename(custom_clean)
                    else:
                        if variant and variant != '<None>':
                            subfolder = f"sprites/monsters/{mtype}/{variant}"
                        else:
                            subfolder = f"sprites/monsters/{mtype}"
                        filename = f"{sprite_type}.png"
                    tex_id = self.load_texture(filename, subfolder)
                    if not tex_id and variant and variant != '<None>':
                        subfolder = f"sprites/monsters/{mtype}"
                        tex_id = self.load_texture(filename, subfolder)
                    if tex_id:
                        self.sprite_textures[tex_key] = tex_id

                if tex_id and tex_id != current_tex:
                    gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                    current_tex = tex_id

                gl.glUniform3fv(pos_loc, 1, thing['pos'])
                w = thing.get('sprite_width', 128)
                h = thing.get('sprite_height', 128)
                gl.glUniform2f(size_loc, float(w), float(h))
                gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)
                continue

            tex_id = None
            if instance_textures:
                tex_id = instance_textures.get(id(thing))


            if tex_id is None:
                class_name = thing.__class__.__name__
                tex_id = sprite_textures.get(class_name)
                if tex_id is None:
                    if isinstance(thing, LogicSpawner):
                        tex_id = self.load_texture('logic_spawner.png', 'sprites')
                        if tex_id: self.sprite_textures['LogicSpawner'] = tex_id
                    elif isinstance(thing, LogicCamera):
                        tex_id = self.load_texture('logic_camera.png', 'sprites')
                        if tex_id: self.sprite_textures['LogicCamera'] = tex_id

                    elif isinstance(thing, Pickup):
                        sprite_path = thing.get_sprite_path()
                        if sprite_path:
                            subfolder = os.path.dirname(sprite_path.replace('assets/', '', 1))
                            filename = os.path.basename(sprite_path)
                            tex_id = self.load_texture(filename, subfolder)
                            if tex_id:
                                sprite_textures[class_name] = tex_id
                    elif isinstance(thing, Monster):
                        sprite_path = thing.get_sprite_path()
                        if sprite_path:
                            subfolder = os.path.dirname(sprite_path.replace('assets/', '', 1))
                            filename = os.path.basename(sprite_path)
                            tex_id = self.load_texture(filename, subfolder)
                            if tex_id:
                                sprite_textures[class_name] = tex_id

                    else:
                        tex_id = sprite_textures.get(class_name)

            if tex_id:
                if tex_id != current_tex:
                    gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                    current_tex = tex_id
                gl.glUniform3fv(pos_loc, 1, thing.pos)
                if isinstance(thing, Light):
                    gl.glUniform2f(size_loc, 16.0, 16.0)
                elif isinstance(thing, (LogicSpawner, LogicCamera)):
                    gl.glUniform2f(size_loc, 32.0, 32.0)
                else:
                    gl.glUniform2f(size_loc, 32.0, 32.0)
                gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)

        gl.glBindVertexArray(0)

    # --------------------------------------------------------------------------
    # Water / Glass / Fog
    # --------------------------------------------------------------------------
    @staticmethod
    def _water_wave_amplitude(brush):
        """World-space wave amplitude for a brush, from its editor settings.

        water_wave_height is stored as a 0..1 fraction (legacy maps stored raw
        slider ints up to 200 — treat anything > 2 as a percentage). Even with
        waves disabled a whisper of swell remains so the surface never reads
        as a frozen slab. Amplitude is capped so the surface stays inside the
        brush volume.
        """
        size = brush.get('size', [64, 64, 64])
        h = float(brush.get('water_wave_height', 0.5))
        if h > 2.0:
            h = h / 100.0
        if brush.get('water_wave_enabled', True):
            amp = h * 30.0
        else:
            amp = 1.2
        return min(amp, size[1] * 0.45, 30.0)

    def draw_water_brushes(self, projection, view, camera_pos, brushes, lights, config):
        if not brushes or 'water' not in self.shaders:
            return
        if not getattr(self, 'water_enabled', True):
            return
        shader, uniforms = self.shaders['water'], self.uniforms['water']
        gl.glUseProgram(shader)
        self._upload_lights_once('water', lights)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform3fv(uniforms['viewPos'], 1, glm.value_ptr(camera_pos))
        gl.glUniform1f(uniforms['time'], config.get('time', 0.0))
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.water_normal_id)
        gl.glUniform1i(uniforms['normalMap'], 0)

        opacity_loc = uniforms['waterOpacity']
        reflectivity_loc = uniforms['waterReflectivity']
        tint_loc, model_loc = uniforms['waterTint'], uniforms['model']
        normal_mat_loc = uniforms['normalMatrix']
        wave_amp_loc = uniforms['waveAmp']
        brush_size_loc = uniforms['brushSize']

        surface_vao = self.vaos.get('water_surface')
        cube_vao = self.vaos['cube']
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)

        for brush in brushes:
            model_matrix = self._brush_model_matrix(brush)
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
            if normal_mat_loc >= 0:
                normal_mat = self._compute_normal_matrix(model_matrix, brush)
                gl.glUniformMatrix3fv(normal_mat_loc, 1, gl.GL_FALSE, glm.value_ptr(normal_mat))
            gl.glUniform1f(opacity_loc, brush.get('water_opacity', 0.5))
            gl.glUniform1f(reflectivity_loc, brush.get('water_reflectivity', 0.5))
            gl.glUniform3fv(tint_loc, 1, brush.get('water_tint', [0.0, 0.4, 0.6]))
            size = brush.get('size', [64, 64, 64])
            gl.glUniform3f(brush_size_loc, float(size[0]), float(size[1]), float(size[2]))
            gl.glUniform1f(wave_amp_loc, self._water_wave_amplitude(brush))

            mesh = self._get_geo_mesh(brush)
            if mesh is not None:
                # Angled water: draw the real convex faces.  When the top face
                # is flat and spans the full AABB footprint the tessellated
                # wave grid still caps it exactly; otherwise the mesh's own
                # top faces are used (flat surface, shader-animated normals).
                top_count = mesh.count - mesh.side_count
                gl.glBindVertexArray(mesh.vao)
                if not brush.get('water_plane', False):
                    gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.side_count)
                if mesh.has_flat_top and surface_vao:
                    gl.glBindVertexArray(surface_vao)
                    gl.glDrawElements(gl.GL_TRIANGLES, self._water_surface_index_count,
                                      gl.GL_UNSIGNED_INT, None)
                elif top_count:
                    gl.glDrawArrays(gl.GL_TRIANGLES, mesh.side_count, top_count)
                elif brush.get('water_plane', False):
                    gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)
                self.render_stats.draw_calls += 1
                continue

            # Side walls first for full water volumes (edge-pinned waves keep
            # the displaced surface meeting these exactly), then the surface
            # composites over them for the common above-water view.
            if not brush.get('water_plane', False):
                gl.glBindVertexArray(cube_vao)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 24)

            # Tessellated top surface (the part the waves displace)
            if surface_vao:
                gl.glBindVertexArray(surface_vao)
                gl.glDrawElements(gl.GL_TRIANGLES, self._water_surface_index_count,
                                  gl.GL_UNSIGNED_INT, None)
            else:
                gl.glBindVertexArray(cube_vao)
                gl.glDrawArrays(gl.GL_TRIANGLES, 30, 6)
            self.render_stats.draw_calls += 1
        gl.glBindVertexArray(0)

    def draw_glass_brushes(self, projection, view, camera_pos, brushes, lights, config):
        if not brushes or 'glass' not in self.shaders:
            return
        shader, uniforms = self.shaders['glass'], self.uniforms['glass']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform3fv(uniforms['viewPos'], 1, glm.value_ptr(camera_pos))

        model_loc = uniforms['model']
        water_color_loc = uniforms['waterColor']
        distortion_loc = uniforms['distortionStrength']
        caustic_loc = uniforms['causticStrength']
        opacity_loc = uniforms['glassOpacity']
        refraction_loc = uniforms['refractionIndex']
        roughness_loc = uniforms['roughness']
        normal_mat_loc = uniforms.get('normalMatrix', -1)
        if normal_mat_loc is None: normal_mat_loc = -1

        gl.glBindVertexArray(self.vaos['cube'])
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glEnable(gl.GL_CULL_FACE)
        gl.glCullFace(gl.GL_BACK)

        for brush in brushes:
            model_matrix = self._brush_model_matrix(brush)
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
            if normal_mat_loc > 0:
                normal_mat = self._compute_normal_matrix(model_matrix, brush)
                gl.glUniformMatrix3fv(normal_mat_loc, 1, gl.GL_FALSE, glm.value_ptr(normal_mat))
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

            mesh = self._get_geo_mesh(brush)
            if mesh is not None:
                gl.glBindVertexArray(mesh.vao)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)
                gl.glBindVertexArray(self.vaos['cube'])
            else:
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
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform3fv(uniforms['viewPos'], 1, glm.value_ptr(camera_pos))
        gl.glUniform1f(uniforms['time'], config.get('time', 0.0))
        gl.glActiveTexture(gl.GL_TEXTURE1)
        gl.glBindTexture(gl.GL_TEXTURE_3D, self.noise_texture_id)
        gl.glUniform1i(uniforms['noiseTexture'], 1)
        gl.glBindVertexArray(self.vaos['cube'])
        gl.glEnable(gl.GL_CULL_FACE)

        model_loc = uniforms['model']
        inv_model_loc = uniforms['inverseModel']
        density_loc = uniforms['density']
        fog_color_loc = uniforms['fogColor']
        noise_scale_loc = uniforms['noiseScale']
        object_color_loc = uniforms['object_color']
        alpha_loc = uniforms['alpha']

        for brush in brushes:
            model_matrix = self._brush_model_matrix(brush)
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
            inv_matrix = glm.inverse(model_matrix)
            gl.glUniformMatrix4fv(inv_model_loc, 1, gl.GL_FALSE, glm.value_ptr(inv_matrix))
            f_color = brush.get('fog_color', [0.5, 0.6, 0.7])
            gl.glUniform1f(density_loc, brush.get('fog_density', 0.01))
            gl.glUniform3fv(fog_color_loc, 1, f_color)
            gl.glUniform1f(noise_scale_loc, brush.get('fog_noise_scale', 0.01))
            gl.glUniform3fv(object_color_loc, 1, f_color)
            gl.glUniform1f(alpha_loc, 0.4)

            mesh = self._get_geo_mesh(brush)
            if mesh is not None:
                gl.glBindVertexArray(mesh.vao)
                gl.glCullFace(gl.GL_FRONT)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)
                gl.glCullFace(gl.GL_BACK)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)
                gl.glBindVertexArray(self.vaos['cube'])
            else:
                gl.glCullFace(gl.GL_FRONT)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 24)
                gl.glDrawArrays(gl.GL_TRIANGLES, 30, 6)
                gl.glCullFace(gl.GL_BACK)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 24)
                gl.glDrawArrays(gl.GL_TRIANGLES, 30, 6)

        gl.glDisable(gl.GL_CULL_FACE)
        gl.glBindVertexArray(0)
        gl.glActiveTexture(gl.GL_TEXTURE0)

    # --------------------------------------------------------------------------
    # Helpers for sorting and matrix utilities
    # --------------------------------------------------------------------------
    def _sort_objects(self, brushes, things, config):
        opaque, transparent, sprites, fog, water, glass, glow = [], [], [], [], [], [], []
        is_play, show_sprites = config.get('play_mode', False), config.get('show_sprites_in_play_mode', False)

        for brush in brushes:
            if brush.get('hidden'):
                continue
            # Hoist the shader lookup: it was fetched up to three times per
            # brush per frame for the Fog/Glass/Glow branches below.
            shader = brush.get('shader')
            if is_water_brush(brush):
                water.append(brush)
            elif brush.get('is_fog') or shader == 'Fog':
                fog.append(brush)
            elif shader == 'Glass':
                glass.append(brush)
            elif shader == 'Glow':
                glow.append(brush)
            elif brush.get('is_trigger'):
                if not is_play:
                    transparent.append(brush)
            else:
                opaque.append(brush)

        if not is_play:
            sprites = [t for t in things if (isinstance(t, Thing) or (isinstance(t, dict) and 'monster_type' in t))
                       and not (PathNode is not None and isinstance(t, PathNode))]
        else:
            for t in things:
                if PathNode is not None and isinstance(t, PathNode):
                    continue
                if Portal is not None and isinstance(t, Portal):
                    sprites.append(t)
                    continue
                if isinstance(t, dict) and 'monster_type' in t:
                    sprites.append(t)
                elif isinstance(t, Thing):
                    if isinstance(t, Pickup):
                        sprites.append(t)
                    elif isinstance(t, (Monster, LogicGate, LogicRelay, LogicTimer, LevelChanger)):
                        sprites.append(t)
                    elif getattr(t, 'properties', {}).get('model_path'):
                        # Always render models in play mode (3D geometry, not just editor sprites)
                        sprites.append(t)
                    elif show_sprites:
                        sprites.append(t)
        return opaque, transparent, sprites, fog, water, glass, glow

    def _split_opaque(self, brushes):
        textured, solid = [], []
        for b in brushes:
            if any(t and t not in ('default.png', 'caulk.jpg') for t in b.get('textures', {}).values()):
                textured.append(b)
            else:
                solid.append(b)
        return textured, solid

    def _brush_model_matrix(self, brush):
        pos = brush.get('pos', [0, 0, 0])
        size = brush.get('size', [64, 64, 64])
        mat = glm.translate(self._identity_mat4, glm.vec3(*pos))
        angle = brush.get('_rot_angle')
        if angle:
            axis_raw = brush.get('rot_axis', [0, 1, 0])
            axis = glm.vec3(*axis_raw)
            if glm.length(axis) > 0.001:
                mat = glm.rotate(mat, glm.radians(float(angle)), glm.normalize(axis))
        mat = glm.scale(mat, glm.vec3(*size))
        return mat

    def _compute_normal_matrix(self, model_matrix, brush=None):
        # brush parameter is accepted for API compatibility with Renderer_F's
        # caching override, but not used at the base-class level.
        mat3 = glm.mat3(model_matrix)
        try:
            return glm.transpose(glm.inverse(mat3))
        except Exception:
            return self._identity_mat3

    def _distance_sq(self, pos1, pos2):
        if isinstance(pos1, (list, tuple)):
            return (pos1[0]-pos2.x)**2 + (pos1[1]-pos2.y)**2 + (pos1[2]-pos2.z)**2
        return (pos1.x-pos2.x)**2 + (pos1.y-pos2.y)**2 + (pos1.z-pos2.z)**2

    def _upload_lights_once(self, shader_name, lights):
        if shader_name not in self.uniforms:
            return
        # Skip if this shader already received this exact light list this
        # frame (portal passes may use a different list, so key on ids).
        key = tuple(map(id, lights[:self.MAX_LIGHTS]))
        if self._frame_lights_uploaded.get(shader_name) == key:
            return
        self._frame_lights_uploaded[shader_name] = key
        uniforms = self.uniforms[shader_name]
        num_lights = min(len(lights), self.MAX_LIGHTS)
        gl.glUniform1i(uniforms['active_lights'], num_lights)
        shadow_index_map = self._light_shadow_index
        light_names = self._light_uniform_names
        for i in range(num_lights):
            light = lights[i]
            n_pos, n_col, n_int, n_rad, n_shadow = light_names[i]
            gl.glUniform3fv(uniforms[n_pos], 1, light.pos)
            gl.glUniform3fv(uniforms[n_col], 1, light.get_color())
            gl.glUniform1f(uniforms[n_int], light.get_intensity())
            gl.glUniform1f(uniforms[n_rad], light.get_radius())
            loc = uniforms[n_shadow]
            if loc != -1:
                gl.glUniform1i(loc, shadow_index_map.get(id(light), -1))
        # Bind the depth cube-maps so the shadow test can sample them.
        self._bind_shadow_maps(uniforms)

    # --------------------------------------------------------------------------
    # Depth cube-map shadow mapping
    # --------------------------------------------------------------------------
    def _init_shadow_resources(self):
        """Allocate the FBO and the pool of depth cube-maps used for
        omnidirectional point-light shadows.  Called once, after the GL context
        and shaders are ready."""
        if 'depth_cube' not in self.shaders:
            return
        try:
            prev_fbo = int(gl.glGetIntegerv(gl.GL_FRAMEBUFFER_BINDING))
            self._shadow_fbo = int(gl.glGenFramebuffers(1))
            self._shadow_cubemaps = []
            size = self.shadow_map_size
            for _ in range(self.MAX_SHADOW_LIGHTS):
                cm = int(gl.glGenTextures(1))
                gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, cm)
                for face in range(6):
                    gl.glTexImage2D(gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + face, 0,
                                    gl.GL_DEPTH_COMPONENT24, size, size, 0,
                                    gl.GL_DEPTH_COMPONENT, gl.GL_FLOAT, None)
                gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
                gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
                gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
                gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
                gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_R, gl.GL_CLAMP_TO_EDGE)
                self._shadow_cubemaps.append(cm)
            gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)

            # Depth-only FBO: validate completeness with the first face attached.
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._shadow_fbo)
            gl.glDrawBuffer(gl.GL_NONE)
            gl.glReadBuffer(gl.GL_NONE)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_ATTACHMENT,
                                      gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X, self._shadow_cubemaps[0], 0)
            status = gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, prev_fbo)
            if status != gl.GL_FRAMEBUFFER_COMPLETE:
                print(f"[Shadow] depth cube-map FBO incomplete (0x{status:x}); shadows disabled")
                self._shadow_fbo = None
                self._shadow_cubemaps = []
            else:
                print(f"[Shadow] depth cube-map shadows ready "
                      f"({self.MAX_SHADOW_LIGHTS} lights @ {size}px)")
        except Exception as e:
            print(f"[Shadow] initialisation failed: {e}")
            self._shadow_fbo = None
            self._shadow_cubemaps = []

    def _thing_model_matrix(self, thing):
        """Model matrix for a model-carrying Thing, matching draw_models()."""
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
        return mat

    def _bind_shadow_maps(self, uniforms):
        """Bind every depth cube-map to its reserved texture unit and point the
        matching ``shadowMaps[i]`` sampler at it.  Unused slots are still bound
        so the samplers stay valid; the shaders simply never sample a slot whose
        ``shadowIndex`` no light references."""
        if not self._shadow_cubemaps:
            return
        base = self.SHADOW_TEXTURE_UNIT_BASE
        for i, cm in enumerate(self._shadow_cubemaps):
            loc = uniforms[f'shadowMaps[{i}]']
            if loc != -1:
                gl.glActiveTexture(gl.GL_TEXTURE0 + base + i)
                gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, cm)
                gl.glUniform1i(loc, base + i)
        gl.glActiveTexture(gl.GL_TEXTURE0)

    def _collect_shadow_casters(self, brushes, models, lx, ly, lz, reach):
        """Return the brushes/models within *reach* of a light plus a hashable
        signature of their transforms (used to detect when a cube-map is stale).
        Filtering once per light — rather than once per cube face — also cuts the
        non-cached path's CPU work by 6x."""
        in_brushes, bkeys = [], []
        for b in brushes:
            pos = b.get('pos', (0, 0, 0))
            size = b.get('size', (64, 64, 64))
            br = 0.5 * max(size[0], size[1], size[2])
            dx = pos[0] - lx; dy = pos[1] - ly; dz = pos[2] - lz
            limit = reach + br
            if (dx * dx + dy * dy + dz * dz) > limit * limit:
                continue
            in_brushes.append(b)
            axis = b.get('rot_axis')
            bkeys.append((pos[0], pos[1], pos[2], size[0], size[1], size[2],
                          b.get('_rot_angle'), tuple(axis) if axis else None))

        in_models, mkeys = [], []
        reach4_sq = reach * reach * 4.0
        for t in models:
            pos = t.pos
            dx = pos[0] - lx; dy = pos[1] - ly; dz = pos[2] - lz
            if (dx * dx + dy * dy + dz * dz) > reach4_sq:
                continue
            in_models.append(t)
            props = t.properties
            scale = props.get('scale', 1.0)
            scale_key = scale if isinstance(scale, (int, float)) else tuple(scale)
            mkeys.append((pos[0], pos[1], pos[2], props.get('model_path'),
                          tuple(props.get('rotation', (0, 0, 0))), scale_key))

        return in_brushes, in_models, (tuple(bkeys), tuple(mkeys))

    def render_shadow_maps(self, shadow_lights, brushes, things, config, camera_pos=None):
        """Refresh the depth cube-map for each shadow-casting point light.

        Cube-maps are cached per slot: a light's map is only re-rendered when the
        light or one of its in-range casters actually moves.  A fully static scene
        therefore does *zero* GPU shadow work after the first frame — only the
        cheap CPU signature check runs.  Populates ``self._light_shadow_index``
        (id(light) -> slot) every frame so the lighting shaders sample correctly.
        """
        self._light_shadow_index = {}
        if not self._shadow_cubemaps or 'depth_cube' not in self.shaders:
            return
        lights = list(shadow_lights)
        if not lights:
            # Release every slot so a light enabled later re-renders cleanly.
            for s in range(self.MAX_SHADOW_LIGHTS):
                self._shadow_slot_owner[s] = None
                self._shadow_slot_sig[s] = None
            return

        # Over budget? Keep the shadow lights nearest the camera.
        if len(lights) > self.MAX_SHADOW_LIGHTS:
            if camera_pos is not None:
                cx, cy, cz = float(camera_pos.x), float(camera_pos.y), float(camera_pos.z)
                lights.sort(key=lambda l: (l.pos[0] - cx) ** 2 + (l.pos[1] - cy) ** 2 + (l.pos[2] - cz) ** 2)
            lights = lights[:self.MAX_SHADOW_LIGHTS]

        # ---- Stable slot assignment (a light keeps its slot across frames) ---
        current_ids = {id(l) for l in lights}
        for s in range(self.MAX_SHADOW_LIGHTS):
            if self._shadow_slot_owner[s] not in current_ids:
                self._shadow_slot_owner[s] = None
                self._shadow_slot_sig[s] = None
        light_slot = {}
        for l in lights:                       # lights that already own a slot keep it
            for s in range(self.MAX_SHADOW_LIGHTS):
                if self._shadow_slot_owner[s] == id(l):
                    light_slot[id(l)] = s
                    break
        for l in lights:                       # remaining lights grab free slots
            if id(l) in light_slot:
                continue
            for s in range(self.MAX_SHADOW_LIGHTS):
                if self._shadow_slot_owner[s] is None:
                    self._shadow_slot_owner[s] = id(l)
                    self._shadow_slot_sig[s] = None
                    light_slot[id(l)] = s
                    break

        # ---- Filter casters once & decide which lights are dirty ------------
        caster_brushes = []
        for b in brushes:
            if b.get('hidden') or b.get('is_trigger') or b.get('is_fog') or b.get('operation') == 'subtract':
                continue
            if is_water_brush(b) or b.get('shader') in ('Fog', 'Glass', 'Glow'):
                continue
            caster_brushes.append(b)
        caster_models = [t for t in things
                         if isinstance(t, Thing) and t.properties.get('model_path')]

        to_render = []   # (light, slot, in_brushes, in_models)
        for l in lights:
            slot = light_slot.get(id(l))
            if slot is None:
                continue
            lx, ly, lz = float(l.pos[0]), float(l.pos[1]), float(l.pos[2])
            radius = max(float(l.get_radius()), 1.0)
            in_brushes, in_models, caster_keys = self._collect_shadow_casters(
                caster_brushes, caster_models, lx, ly, lz, radius)
            sig = (round(lx, 3), round(ly, 3), round(lz, 3), round(radius, 3), caster_keys)
            self._light_shadow_index[id(l)] = slot
            if self._shadow_slot_sig[slot] == sig:
                continue                       # cube-map still valid -> skip GPU work
            to_render.append((l, slot, in_brushes, in_models, sig))

        if not to_render:
            return                             # everything cached: no GL work this frame

        # ---- Save GL state we are about to clobber -------------------------
        prev_fbo = int(gl.glGetIntegerv(gl.GL_FRAMEBUFFER_BINDING))
        prev_vp = gl.glGetIntegerv(gl.GL_VIEWPORT)
        scissor_was = bool(gl.glIsEnabled(gl.GL_SCISSOR_TEST))
        cull_was = bool(gl.glIsEnabled(gl.GL_CULL_FACE))
        blend_was = bool(gl.glIsEnabled(gl.GL_BLEND))

        shader = self.shaders['depth_cube']
        u = self.uniforms['depth_cube']
        gl.glUseProgram(shader)
        self._current_shader = shader
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._shadow_fbo)
        gl.glDrawBuffer(gl.GL_NONE)
        gl.glReadBuffer(gl.GL_NONE)
        size = self.shadow_map_size
        gl.glViewport(0, 0, size, size)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDepthFunc(gl.GL_LESS)
        # No face culling: brush cube winding isn't guaranteed and models may be
        # single-sided/open. The shader-side depth bias handles self-shadowing.
        gl.glDisable(gl.GL_CULL_FACE)

        model_loc = u['model']
        lsm_loc = u['lightSpaceMatrix']
        lightpos_loc = u['lightPos']
        far_loc = u['far_plane']
        cube_vao = self.vaos['cube']

        # Cube-face look-at basis (standard GL cube-map orientation).
        face_dirs = (
            (glm.vec3( 1, 0, 0), glm.vec3(0, -1,  0)),
            (glm.vec3(-1, 0, 0), glm.vec3(0, -1,  0)),
            (glm.vec3( 0, 1, 0), glm.vec3(0,  0,  1)),
            (glm.vec3( 0,-1, 0), glm.vec3(0,  0, -1)),
            (glm.vec3( 0, 0, 1), glm.vec3(0, -1,  0)),
            (glm.vec3( 0, 0,-1), glm.vec3(0, -1,  0)),
        )

        for light, slot, in_brushes, in_models, sig in to_render:
            lx, ly, lz = float(light.pos[0]), float(light.pos[1]), float(light.pos[2])
            center = glm.vec3(lx, ly, lz)
            far_plane = max(float(light.get_radius()), 1.0)
            near_plane = max(far_plane * 0.002, 1.0)
            proj = glm.perspective(glm.radians(90.0), 1.0, near_plane, far_plane)
            cubemap = self._shadow_cubemaps[slot]

            gl.glUniform3f(lightpos_loc, lx, ly, lz)
            gl.glUniform1f(far_loc, far_plane)

            # Resolve model objects once (not once per face).
            resolved_models = []
            for t in in_models:
                obj = self.load_model(t.properties.get('model_path'))
                if obj and obj.is_loaded:
                    resolved_models.append((t, obj))

            for face in range(6):
                gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_ATTACHMENT,
                                          gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + face, cubemap, 0)
                gl.glClear(gl.GL_DEPTH_BUFFER_BIT)
                lsm = proj * glm.lookAt(center, center + face_dirs[face][0], face_dirs[face][1])
                gl.glUniformMatrix4fv(lsm_loc, 1, gl.GL_FALSE, glm.value_ptr(lsm))

                # Brush casters (shared unit cube VAO, or the brush's own
                # convex mesh for angled brushes), pre-filtered by reach.
                gl.glBindVertexArray(cube_vao)
                for b in in_brushes:
                    gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE,
                                          glm.value_ptr(self._brush_model_matrix(b)))
                    mesh = self._get_geo_mesh(b)
                    if mesh is not None:
                        gl.glBindVertexArray(mesh.vao)
                        gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)
                        gl.glBindVertexArray(cube_vao)
                    else:
                        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)

                # Model casters.
                for t, obj in resolved_models:
                    gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE,
                                          glm.value_ptr(self._thing_model_matrix(t)))
                    gl.glBindVertexArray(obj.vao)
                    if getattr(obj, 'ebo', None) is not None and getattr(obj, 'index_count', 0):
                        gl.glDrawElements(gl.GL_TRIANGLES, obj.index_count, gl.GL_UNSIGNED_INT, None)
                    else:
                        gl.glDrawArrays(gl.GL_TRIANGLES, 0, obj.vertex_count)

            # Mark the slot valid only once its 6 faces are actually drawn.
            self._shadow_slot_sig[slot] = sig

        # ---- Restore state -------------------------------------------------
        gl.glBindVertexArray(0)
        gl.glCullFace(gl.GL_BACK)
        if cull_was:
            gl.glEnable(gl.GL_CULL_FACE)
        else:
            gl.glDisable(gl.GL_CULL_FACE)
        if blend_was:
            gl.glEnable(gl.GL_BLEND)
        else:
            gl.glDisable(gl.GL_BLEND)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, prev_fbo)
        gl.glViewport(int(prev_vp[0]), int(prev_vp[1]), int(prev_vp[2]), int(prev_vp[3]))
        if scissor_was:
            gl.glEnable(gl.GL_SCISSOR_TEST)
        self._current_shader = None

    def _resolve_model_texture_path(self, material, texture_name):
        """
        Resolve a texture path from an MTL material.
        Checks in order:
          1. Relative to the MTL file's directory (correct for MTL references)
          2. assets/textures/ (global fallback)
          3. assets/models/ (legacy fallback)
        Returns the resolved path or None if not found.
        """
        if not texture_name:
            return None

        # 1. Try relative to the MTL file's directory (most correct for MTL refs)
        mtl_dir = material.get('mtl_dir', '')
        if mtl_dir:
            resolved = os.path.join(mtl_dir, texture_name)
            if os.path.exists(resolved):
                return resolved

        # 2. Try assets/textures/ (global fallback)
        resolved = os.path.join('assets', 'textures', texture_name)
        if os.path.exists(resolved):
            return resolved

        # 3. Try assets/models/ (legacy fallback)
        resolved = os.path.join('assets', 'models', texture_name)
        if os.path.exists(resolved):
            return resolved

        # 4. Return as-is and let the loader handle errors
        return texture_name

    # --------------------------------------------------------------------------
    # Editor helpers (outlines, gizmo, etc.)
    # --------------------------------------------------------------------------
    def draw_selected_brush_outline(self, projection, view, brush):
        if 'simple' not in self.shaders:
            return
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        pos = brush.get('pos', [0, 0, 0])
        size = brush.get('size', [64, 64, 64])
        model_matrix = glm.scale(glm.translate(self._identity_mat4, glm.vec3(*pos)), glm.vec3(*size))
        gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
        gl.glUniform3f(uniforms['color'], 1.0, 1.0, 0.0)
        gl.glUniform1f(uniforms['alpha'], 1.0)
        if not hasattr(self, '_edge_vao') or self._edge_vao is None:
            edge_vertices = np.array([
                -0.5,-0.5,-0.5,  0.5,-0.5,-0.5,  0.5,-0.5,-0.5,  0.5,-0.5, 0.5,
                 0.5,-0.5, 0.5, -0.5,-0.5, 0.5, -0.5,-0.5, 0.5, -0.5,-0.5,-0.5,
                -0.5, 0.5,-0.5,  0.5, 0.5,-0.5,  0.5, 0.5,-0.5,  0.5, 0.5, 0.5,
                 0.5, 0.5, 0.5, -0.5, 0.5, 0.5, -0.5, 0.5, 0.5, -0.5, 0.5,-0.5,
                -0.5,-0.5,-0.5, -0.5, 0.5,-0.5,  0.5,-0.5,-0.5,  0.5, 0.5,-0.5,
                 0.5,-0.5, 0.5,  0.5, 0.5, 0.5, -0.5,-0.5, 0.5, -0.5, 0.5, 0.5,
            ], dtype=np.float32)
            self._edge_vao = gl.glGenVertexArrays(1)
            gl.glBindVertexArray(self._edge_vao)
            vbo = gl.glGenBuffers(1)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, edge_vertices.nbytes, edge_vertices, gl.GL_STATIC_DRAW)
            gl.glEnableVertexAttribArray(0)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            gl.glBindVertexArray(0)
            self._edge_vbo = vbo
        gl.glLineWidth(1.0)
        mesh = self._get_geo_mesh(brush)
        if mesh is not None and mesh.edge_count:
            # Angled brush: outline its real convex edges instead of the AABB.
            gl.glBindVertexArray(mesh.edge_vao)
            gl.glDrawArrays(gl.GL_LINES, 0, mesh.edge_count)
        else:
            gl.glBindVertexArray(self._edge_vao)
            gl.glDrawArrays(gl.GL_LINES, 0, 24)
        gl.glBindVertexArray(0)

    def draw_face_highlight(self, projection, view, brush, face_name):
        if 'simple' not in self.shaders:
            return
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(self._identity_mat4))
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glUniform3f(uniforms['color'], 0.8, 0.2, 0.9)
        gl.glUniform1f(uniforms['alpha'], 0.4)

        # Angled (clipped) brushes: highlight the real convex face polygon so
        # the sloped cut face lights up under the cursor, not an AABB side.
        if brush_geometry.brush_has_geometry(brush):
            verts = self._geo_face_highlight_verts(brush, face_name)
            if not verts:
                gl.glUseProgram(0)
                return
            self._draw_face_highlight_verts(verts, uniforms)
            gl.glDisable(gl.GL_BLEND)
            gl.glUseProgram(0)
            return

        pos, size = brush['pos'], brush['size']
        hx, hy, hz = size[0]/2, size[1]/2, size[2]/2
        cx, cy, cz = pos[0], pos[1], pos[2]
        bias = 0.5
        verts = []
        if face_name == 'north':
            z = cz + hz + bias
            verts = [cx-hx, cy-hy, z, cx+hx, cy-hy, z, cx+hx, cy+hy, z,
                     cx-hx, cy-hy, z, cx+hx, cy+hy, z, cx-hx, cy+hy, z]
        elif face_name == 'south':
            z = cz - hz - bias
            verts = [cx+hx, cy-hy, z, cx-hx, cy-hy, z, cx-hx, cy+hy, z,
                     cx+hx, cy-hy, z, cx-hx, cy+hy, z, cx+hx, cy+hy, z]
        elif face_name == 'east':
            x = cx + hx + bias
            verts = [x, cy-hy, cz+hz, x, cy-hy, cz-hz, x, cy+hy, cz-hz,
                     x, cy-hy, cz+hz, x, cy+hy, cz-hz, x, cy+hy, cz+hz]
        elif face_name == 'west':
            x = cx - hx - bias
            verts = [x, cy-hy, cz-hz, x, cy-hy, cz+hz, x, cy+hy, cz+hz,
                     x, cy-hy, cz-hz, x, cy+hy, cz+hz, x, cy+hy, cz-hz]
        elif face_name == 'top':
            y = cy + hy + bias
            verts = [cx-hx, y, cz+hz, cx+hx, y, cz+hz, cx+hx, y, cz-hz,
                     cx-hx, y, cz+hz, cx+hx, y, cz-hz, cx-hx, y, cz-hz]
        elif face_name == 'down':
            y = cy - hy - bias
            verts = [cx-hx, y, cz-hz, cx+hx, y, cz-hz, cx+hx, y, cz+hz,
                     cx-hx, y, cz-hz, cx+hx, y, cz+hz, cx-hx, y, cz+hz]
        if not verts:
            return

        self._draw_face_highlight_verts(verts, uniforms)
        gl.glDisable(gl.GL_BLEND)
        gl.glUseProgram(0)

    @staticmethod
    def _geo_face_highlight_verts(brush, face_name):
        """Triangle-fan positions (world space, nudged outward) for one convex
        face of an angled brush, or ``None`` when the face can't be resolved."""
        convex = brush_geometry.get_convex(brush)
        if convex is None or not convex.is_valid:
            return None
        face = None
        for f in convex.faces:
            if brush_geometry.face_key(f) == face_name:
                face = f
                break
        if face is None:
            return None
        idx = face['indices']
        if len(idx) < 3:
            return None
        ring = convex.verts[idx] + np.array(face['normal']) * 0.5   # bias off surface
        out = []
        v0 = ring[0]
        for k in range(1, len(idx) - 1):
            for p in (v0, ring[k], ring[k + 1]):
                out.extend((float(p[0]), float(p[1]), float(p[2])))
        return out

    def _draw_face_highlight_verts(self, verts, uniforms):
        """Upload and draw a fill+wire face highlight for arbitrary geometry."""
        v_data = np.asarray(verts, dtype=np.float32)
        n = len(v_data) // 3
        if self.face_highlight_vao is None:
            self.face_highlight_vao = gl.glGenVertexArrays(1)
            self.face_highlight_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(self.face_highlight_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.face_highlight_vbo)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            gl.glEnableVertexAttribArray(0)
            gl.glBindVertexArray(0)
        gl.glBindVertexArray(self.face_highlight_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.face_highlight_vbo)
        # Orphan-and-refill so the buffer resizes for any face vertex count.
        gl.glBufferData(gl.GL_ARRAY_BUFFER, v_data.nbytes, v_data, gl.GL_DYNAMIC_DRAW)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, n)
        gl.glUniform3f(uniforms['color'], 1.0, 1.0, 1.0)
        gl.glUniform1f(uniforms['alpha'], 1.0)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, n)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl.glBindVertexArray(0)

    def draw_path_node_cubes(self, projection, view, things):
        if 'simple' not in self.shaders:
            return
        nodes = [t for t in things if isinstance(t, PathNode)]
        if not nodes:
            return

        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform3f(uniforms['color'], 1.0, 0.5, 0.0)
        gl.glUniform1f(uniforms['alpha'], 1.0)
        cube_size = 16.0
        gl.glBindVertexArray(self.vaos['cube'])
        for node in nodes:
            pos = node.pos
            model_matrix = glm.scale(glm.translate(self._identity_mat4,
                                                   glm.vec3(float(pos[0]), float(pos[1]), float(pos[2]))),
                                     glm.vec3(cube_size, cube_size, cube_size))
            gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(model_matrix))
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
            self.render_stats.draw_calls += 1
        gl.glBindVertexArray(0)
        gl.glUseProgram(0)

    def draw_portal_wireframes(self, projection, view, things, play_mode=False):
        if 'simple' not in self.shaders:
            return

        portal_things = [t for t in things if isinstance(t, Portal) and t.properties.get('show_rim', True)]
        if not portal_things:
            return

        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform1f(uniforms['alpha'], 1.0)

        # Outline VAO
        if self._portal_outline_vao is None:
            self._portal_outline_vao = gl.glGenVertexArrays(1)
            self._portal_outline_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(self._portal_outline_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._portal_outline_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, 4*3*4, None, gl.GL_DYNAMIC_DRAW)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 12, ctypes.c_void_p(0))
            gl.glEnableVertexAttribArray(0)
            gl.glBindVertexArray(0)
        # Normal arrow VAO
        if self._portal_normal_vao is None:
            self._portal_normal_vao = gl.glGenVertexArrays(1)
            self._portal_normal_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(self._portal_normal_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._portal_normal_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, 2*3*4, None, gl.GL_DYNAMIC_DRAW)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 12, ctypes.c_void_p(0))
            gl.glEnableVertexAttribArray(0)
            gl.glBindVertexArray(0)

        gl.glLineWidth(1.0)
        model_loc = uniforms['model']
        color_loc = uniforms['color']
        gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(self._identity_mat4))

        for portal in portal_things:
            raw = portal.properties.get('color', [255, 255, 255])
            color = normalize_color(raw, default=[1.0,1.0,1.0])
            r,g,b = color
            if not portal.is_active():
                r,g,b = r*0.4, g*0.4, b*0.4

            # Red wireframe for unlinked or broken portal pairs
            target_name = portal.properties.get('portal_target', '')
            target_exists = target_name and any(
                isinstance(t, Portal) and t.properties.get('name') == target_name
                for t in things
                if t is not portal
            )
            if not target_exists:
                r, g, b = 0.86, 0.24, 0.24  # red — no valid target
            corners = portal.get_corners_world()
            vdata = np.array(corners, dtype=np.float32).flatten()
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._portal_outline_vbo)
            gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, vdata.nbytes, vdata)
            gl.glUniform3f(color_loc, r, g, b)
            gl.glBindVertexArray(self._portal_outline_vao)
            gl.glDrawArrays(gl.GL_LINE_LOOP, 0, 4)

            # normal arrow
            cx = float(portal.pos[0]); cy = float(portal.pos[1]); cz = float(portal.pos[2])
            nx, ny, nz = portal.get_normal()
            arrow_len = portal.get_width() * 0.4
            nline = np.array([cx, cy, cz, cx+nx*arrow_len, cy+ny*arrow_len, cz+nz*arrow_len], dtype=np.float32)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._portal_normal_vbo)
            gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, nline.nbytes, nline)
            gl.glUniform3f(color_loc, min(1.0, r*1.6), min(1.0, g*1.6), min(1.0, b*1.6))
            gl.glBindVertexArray(self._portal_normal_vao)
            gl.glDrawArrays(gl.GL_LINES, 0, 2)

        gl.glLineWidth(1.0)
        gl.glBindVertexArray(0)
        gl.glUseProgram(0)

    def draw_connection_lines(self, projection, view, connections):
        if not connections or 'simple' not in self.shaders:
            return
        line_data = []
        line_colors = []
        for conn in connections:
            sx,sy,sz = conn['src']
            dx,dy,dz = conn['dst']
            line_data.extend([float(sx), float(sy), float(sz), float(dx), float(dy), float(dz)])
            line_colors.append(conn.get('color', (0.0,1.0,1.0)))
        if not line_data:
            return
        vertices = np.array(line_data, dtype=np.float32)
        if self._conn_line_vao is None:
            self._conn_line_vao = gl.glGenVertexArrays(1)
            self._conn_line_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(self._conn_line_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._conn_line_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, 1024*1024, None, gl.GL_DYNAMIC_DRAW)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            gl.glEnableVertexAttribArray(0)
            gl.glBindVertexArray(0)
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(self._identity_mat4))
        gl.glUniform1f(uniforms['alpha'], 1.0)
        gl.glBindVertexArray(self._conn_line_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._conn_line_vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, vertices.nbytes, vertices)
        color_loc = uniforms['color']
        for i, (r,g,b) in enumerate(line_colors):
            gl.glUniform3f(color_loc, r, g, b)
            gl.glDrawArrays(gl.GL_LINES, i*2, 2)
        gl.glBindVertexArray(0)
        gl.glUseProgram(0)

    def draw_collision_visualization(self, projection, view, brushes):
        """
        Draw wireframe overlays for collision meshes and AABB boxes.
        Helps debug why collision doesn't match the visual model.
        """
        if 'simple' not in self.shaders:
            return
        
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(self._identity_mat4))
        gl.glUniform1f(uniforms['alpha'], 1.0)
        
        # Query and clamp line width to supported range
        # glLineWidth > 1.0 is deprecated in core profile
        widths = (gl.GLfloat * 2)()
        gl.glGetFloatv(gl.GL_ALIASED_LINE_WIDTH_RANGE, widths)
        min_width, max_width = float(widths[0]), float(widths[1])
        desired_width = 2.0
        clamped_width = max(min_width, min(max_width, desired_width))
        gl.glLineWidth(clamped_width)
        
        for brush in brushes:
            if not brush.get('_model_collision'):
                continue
            
            mode = brush.get('_collision_mode', 'aabb')
            
            if mode == 'aabb':
                # Draw yellow wireframe box
                pos = brush.get('pos', [0, 0, 0])
                size = brush.get('size', [64, 64, 64])
                self._draw_wireframe_box(pos, size, (1.0, 1.0, 0.0), uniforms)
                
            elif mode == 'mesh':
                # Draw cyan wireframe triangles
                mesh_tris = brush.get('_mesh_triangles', [])
                gl.glUniform3f(uniforms['color'], 0.0, 1.0, 1.0)  # Cyan
                
                # Build line data from triangles
                line_verts = []
                for (v0, v1, v2), normal in mesh_tris:
                    line_verts.extend([v0[0], v0[1], v0[2], v1[0], v1[1], v1[2]])
                    line_verts.extend([v1[0], v1[1], v1[2], v2[0], v2[1], v2[2]])
                    line_verts.extend([v2[0], v2[1], v2[2], v0[0], v0[1], v0[2]])
                
                if line_verts:
                    verts = np.array(line_verts, dtype=np.float32)
                    # Use dynamic VAO
                    vao = gl.glGenVertexArrays(1)
                    vbo = gl.glGenBuffers(1)
                    gl.glBindVertexArray(vao)
                    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
                    gl.glBufferData(gl.GL_ARRAY_BUFFER, verts.nbytes, verts, gl.GL_DYNAMIC_DRAW)
                    gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
                    gl.glEnableVertexAttribArray(0)
                    
                    gl.glDrawArrays(gl.GL_LINES, 0, len(line_verts) // 3)
                    
                    gl.glBindVertexArray(0)
                    gl.glDeleteVertexArrays(1, [vao])
                    gl.glDeleteBuffers(1, [vbo])
        
        gl.glLineWidth(1.0)
        gl.glUseProgram(0)

    def _draw_wireframe_box(self, pos, size, color, uniforms):
        """Draw a wireframe AABB box at position with size."""
        hx, hy, hz = size[0]/2, size[1]/2, size[2]/2
        cx, cy, cz = pos[0], pos[1], pos[2]
        
        # 12 edges of a box
        edges = [
            # Bottom face
            (cx-hx, cy-hy, cz-hz, cx+hx, cy-hy, cz-hz),
            (cx+hx, cy-hy, cz-hz, cx+hx, cy-hy, cz+hz),
            (cx+hx, cy-hy, cz+hz, cx-hx, cy-hy, cz+hz),
            (cx-hx, cy-hy, cz+hz, cx-hx, cy-hy, cz-hz),
            # Top face
            (cx-hx, cy+hy, cz-hz, cx+hx, cy+hy, cz-hz),
            (cx+hx, cy+hy, cz-hz, cx+hx, cy+hy, cz+hz),
            (cx+hx, cy+hy, cz+hz, cx-hx, cy+hy, cz+hz),
            (cx-hx, cy+hy, cz+hz, cx-hx, cy+hy, cz-hz),
            # Vertical edges
            (cx-hx, cy-hy, cz-hz, cx-hx, cy+hy, cz-hz),
            (cx+hx, cy-hy, cz-hz, cx+hx, cy+hy, cz-hz),
            (cx+hx, cy-hy, cz+hz, cx+hx, cy+hy, cz+hz),
            (cx-hx, cy-hy, cz+hz, cx-hx, cy+hy, cz+hz),
        ]
        
        verts = []
        for e in edges:
            verts.extend(e)
        
        if not verts:
            return
        
        v_data = np.array(verts, dtype=np.float32)
        gl.glUniform3f(uniforms['color'], *color)
        
        vao = gl.glGenVertexArrays(1)
        vbo = gl.glGenBuffers(1)
        gl.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, v_data.nbytes, v_data, gl.GL_DYNAMIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        
        gl.glDrawArrays(gl.GL_LINES, 0, len(verts) // 3)
        
        gl.glBindVertexArray(0)
        gl.glDeleteVertexArrays(1, [vao])
        gl.glDeleteBuffers(1, [vbo])

    def render_gizmo(self, projection, view, position):
        if 'simple' not in self.shaders:
            return
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        pos_vec = glm.vec3(*position) if isinstance(position, (list, tuple)) else position
        base = glm.scale(glm.translate(self._identity_mat4, pos_vec), glm.vec3(32.0))
        model_loc, color_loc = uniforms['model'], uniforms['color']
        gl.glUniform1f(uniforms['alpha'], 1.0)
        gl.glBindVertexArray(self.vao_gizmo_lines)
        gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(base))
        for i,c in enumerate([(1,0,0), (0,1,0), (0,0,1)]):
            gl.glUniform3f(color_loc, *c)
            gl.glDrawArrays(gl.GL_LINES, i*2, 2)
        gl.glBindVertexArray(self.vao_gizmo_cone)
        for axis, c, rot in [((1,0,0), (1,0,0), glm.rotate(base, glm.radians(-90), glm.vec3(0,0,1))),
                             ((0,1,0), (0,1,0), base),
                             ((0,0,1), (0,0,1), glm.rotate(base, glm.radians(90), glm.vec3(1,0,0)))]:
            m = glm.translate(rot if axis[1] else glm.translate(base, glm.vec3(*axis)), glm.vec3(0,1,0) if axis[1] else glm.vec3(0,0,0))
            if axis[0]: m = glm.translate(glm.rotate(base, glm.radians(-90), glm.vec3(0,0,1)), glm.vec3(0,1,0))
            if axis[2]: m = glm.translate(glm.rotate(base, glm.radians(90), glm.vec3(1,0,0)), glm.vec3(0,1,0))
            if axis[1]: m = glm.translate(base, glm.vec3(0,1,0))
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, glm.value_ptr(m))
            gl.glUniform3f(color_loc, *c)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, self.gizmo_cone_v_count)
        gl.glBindVertexArray(0)

    # --------------------------------------------------------------------------
    # Portal rendering (used by deferred/forward as needed)
    # --------------------------------------------------------------------------
    def _init_portal_gl(self):
        mask_vert = DEFAULT_SHADERS.get('portal_mask.vert', '')
        mask_frag = DEFAULT_SHADERS.get('portal_mask.frag', '')
        rim_vert  = DEFAULT_SHADERS.get('portal_rim.vert', '')
        rim_frag  = DEFAULT_SHADERS.get('portal_rim.frag', '')
        try:
            self._portal_mask_shader = self.shader_loader.compile_from_source(mask_vert, mask_frag)
            self._portal_rim_shader = self.shader_loader.compile_from_source(rim_vert, rim_frag)
        except Exception as e:
            print(f"[Portal] Shader compile error: {e}")
            return
        self._portal_quad_vao = gl.glGenVertexArrays(1)
        self._portal_quad_vbo = gl.glGenBuffers(1)
        gl.glBindVertexArray(self._portal_quad_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._portal_quad_vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, 4*3*4, None, gl.GL_DYNAMIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 12, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        gl.glBindVertexArray(0)
        self._portal_mask_proj_loc = gl.glGetUniformLocation(self._portal_mask_shader, 'projection')
        self._portal_mask_view_loc = gl.glGetUniformLocation(self._portal_mask_shader, 'view')
        self._portal_rim_proj_loc  = gl.glGetUniformLocation(self._portal_rim_shader,  'projection')
        self._portal_rim_view_loc  = gl.glGetUniformLocation(self._portal_rim_shader,  'view')
        self._portal_rim_color_loc = gl.glGetUniformLocation(self._portal_rim_shader,  'rim_color')
        self._portal_gl_ready = True
        print("[Portal] GL resources initialised")

    def _portal_begin_cull(self, is_geo=False):
        """Enable back-face culling for the portal virtual scene, culling the
        interior face of the geometry type currently being drawn.

        The oblique near-plane clip slices solid brushes open at the destination
        portal; with culling off the exposed interior/underside back-faces show
        through the aperture as a dark strip along the bottom. Culling the
        interior faces keeps solids solid, exactly as they look in the main
        view. Cube brushes are wound clockwise-outward (interior == GL_FRONT)
        while generated convex-geometry meshes are counter-clockwise-outward
        (interior == GL_BACK), so the caller says which it is drawing. No-op
        outside the portal pass, so the main scene is left untouched."""
        if not getattr(self, '_portal_scene_pass', False):
            return
        gl.glEnable(gl.GL_CULL_FACE)
        gl.glCullFace(gl.GL_BACK if is_geo else gl.GL_FRONT)

    def _portal_set_cull(self, is_geo):
        """Switch the culled face mid-pass (cube batches vs. convex-geometry
        meshes wind oppositely). No-op outside the portal pass."""
        if not getattr(self, '_portal_scene_pass', False):
            return
        gl.glCullFace(gl.GL_BACK if is_geo else gl.GL_FRONT)

    def _portal_end_cull(self):
        """Restore the default (culling off, GL_BACK) after a portal brush pass."""
        if not getattr(self, '_portal_scene_pass', False):
            return
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glCullFace(gl.GL_BACK)

    def draw_portals(self, portal_things, projection, main_view, camera_pos,
                     brushes, things, lights, config, draw_scene_fn):
        if not self._portal_gl_ready or not portal_things:
            return
        by_name = {}
        for p in portal_things:
            name = p.properties.get('name', '')
            if name:
                by_name[name] = p
        # Pre-multiplied view-projection for screen-space scissor rects.
        pv = projection * main_view
        rendered = 0
        for portal_a in portal_things:
            # Render while any opacity remains (covers both fading-in and fading-out)
            if getattr(portal_a, '_fade_alpha', 1.0) <= 0.01:
                continue
            target_name = portal_a.properties.get('portal_target', '')
            if not target_name:
                continue
            portal_b = by_name.get(target_name)
            if portal_b is None:
                continue

            a_direction = portal_a.properties.get('portal_direction', 'both')
            # Forward: portal_a sees out of portal_b (render B's view into A's aperture)
            render_forward = a_direction in ('forward', 'both')
            # Reverse: portal_b sees out of portal_a (render A's view into B's aperture)
            render_reverse = a_direction in ('reverse', 'both')

            if render_forward:
                if rendered >= self.MAX_PORTALS:
                    break
                self._draw_one_portal(portal_a, portal_b, projection, main_view, camera_pos,
                                      brushes, things, lights, config, draw_scene_fn,
                                      pv, portal_things, by_name, depth=1)
                rendered += 1
            if render_reverse:
                if rendered >= self.MAX_PORTALS:
                    break
                self._draw_one_portal(portal_b, portal_a, projection, main_view, camera_pos,
                                      brushes, things, lights, config, draw_scene_fn,
                                      pv, portal_things, by_name, depth=1)
                rendered += 1

        # Restore global state and wipe the whole stencil buffer for the passes
        # that follow (scissor is already off — each portal disables it).
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_STENCIL_TEST)
        gl.glStencilMask(0xFF)
        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glClear(gl.GL_STENCIL_BUFFER_BIT)

    def _draw_one_portal(self, portal_a, portal_b, projection, main_view, camera_pos,
                         brushes, things, lights, config, draw_scene_fn,
                         pv, portal_things, by_name, depth=1):
        """Render portal_a's aperture showing the view out of portal_b.

        Stencil is level-based: a fragment inside the aperture at recursion
        ``depth`` carries stencil value ``depth``.  The mask pass increments
        from the parent level (depth-1) to ``depth`` — so at depth 1 it goes
        0→1 exactly like the original REPLACE(1) scheme, and nested portals
        (depth>1) stack cleanly without wiping their parent's mask.
        """
        corners_a = portal_a.get_corners_world()
        proj_ptr = glm.value_ptr(projection)
        view_ptr = glm.value_ptr(main_view)
        fade_a = getattr(portal_a, '_fade_alpha', 1.0)

        # --- Scissor to the aperture's screen rect (skips whole-screen overdraw
        #     of the virtual scene). None => straddling the near plane. ---
        rect = self._portal_screen_rect(corners_a, pv)
        if rect is not None:
            if rect[2] <= 0 or rect[3] <= 0:
                return  # aperture is entirely off-screen
            gl.glEnable(gl.GL_SCISSOR_TEST)
            gl.glScissor(*rect)

        # --- Near-plane straddle: within PORTAL_NEAR_STRADDLE of the plane the
        #     world-space mask quad would be near-clipped, revealing the wall
        #     behind the portal. Cover the screen in NDC instead. ---
        nrm = portal_a.get_normal()
        cam_d = ((camera_pos.x - portal_a.pos[0]) * nrm[0] +
                 (camera_pos.y - portal_a.pos[1]) * nrm[1] +
                 (camera_pos.z - portal_a.pos[2]) * nrm[2])
        straddle = (depth == 1 and self.PORTAL_NEAR_STRADDLE > 0.0 and
                    abs(cam_d) < self.PORTAL_NEAR_STRADDLE and
                    portal_a.contains_point(camera_pos.x - cam_d * nrm[0],
                                            camera_pos.y - cam_d * nrm[1],
                                            camera_pos.z - cam_d * nrm[2],
                                            margin=0.0))
        if straddle:
            gl.glDisable(gl.GL_SCISSOR_TEST)  # cover the whole screen
            mask_quad = [(-1.0, -1.0, 0.0), (1.0, -1.0, 0.0),
                         (1.0, 1.0, 0.0), (-1.0, 1.0, 0.0)]
            id_ptr = glm.value_ptr(self._identity_mat4)
            mask_proj_ptr, mask_view_ptr = id_ptr, id_ptr
        else:
            mask_quad = corners_a
            mask_proj_ptr, mask_view_ptr = proj_ptr, view_ptr

        parent_level = depth - 1
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glEnable(gl.GL_STENCIL_TEST)
        gl.glStencilMask(0xFF)
        if depth == 1:
            # Only the top level clears; nested levels must keep the parent mask.
            gl.glClear(gl.GL_STENCIL_BUFFER_BIT)

        # mask pass: stamp `depth` where the aperture is (and, for depth>1, only
        # inside the parent aperture where stencil already == parent_level).
        gl.glColorMask(gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)
        gl.glDepthMask(gl.GL_FALSE)
        if straddle:
            gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glStencilFunc(gl.GL_EQUAL, parent_level, 0xFF)
        gl.glStencilOp(gl.GL_KEEP, gl.GL_KEEP, gl.GL_INCR)
        self._portal_upload_quad(mask_quad)
        gl.glUseProgram(self._portal_mask_shader)
        gl.glUniformMatrix4fv(self._portal_mask_proj_loc, 1, gl.GL_FALSE, mask_proj_ptr)
        gl.glUniformMatrix4fv(self._portal_mask_view_loc, 1, gl.GL_FALSE, mask_view_ptr)
        gl.glBindVertexArray(self._portal_quad_vao)
        gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, 4)
        if straddle:
            gl.glEnable(gl.GL_DEPTH_TEST)

        # depth prime to far, only where this level's mask was written
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDepthFunc(gl.GL_ALWAYS)
        gl.glStencilFunc(gl.GL_EQUAL, depth, 0xFF)
        gl.glStencilOp(gl.GL_KEEP, gl.GL_KEEP, gl.GL_KEEP)
        gl.glDepthRange(1.0, 1.0)
        self._portal_upload_quad(mask_quad)
        gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, 4)
        gl.glDepthRange(0.0, 1.0)
        gl.glDepthFunc(gl.GL_LESS)
        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)

        # --- virtual scene through portal_b ---
        virtual_view, virtual_cam = self._portal_build_virtual_view(portal_a, portal_b, main_view, camera_pos)
        # Oblique-clip away the wall behind portal B.
        clip_proj = self._calculate_oblique_projection(projection, virtual_view, portal_b.pos, portal_b.get_normal())
        self._portal_virtual_view = virtual_view
        self._portal_virtual_proj = clip_proj

        gl.glStencilFunc(gl.GL_EQUAL, depth, 0xFF)
        gl.glStencilOp(gl.GL_KEEP, gl.GL_KEEP, gl.GL_KEEP)
        gl.glStencilMask(0x00)
        old_proj_ptr = self._proj_ptr
        old_view_ptr = self._view_ptr
        self._proj_ptr = glm.value_ptr(self._portal_virtual_proj)
        self._view_ptr = glm.value_ptr(self._portal_virtual_view)
        self._current_shader = None
        self._portal_scene_pass = True
        try:
            draw_scene_fn(clip_proj, virtual_view, virtual_cam, brushes, things, lights, config)
        finally:
            self._portal_scene_pass = False
        self._proj_ptr = old_proj_ptr
        self._view_ptr = old_view_ptr
        self._current_shader = None
        gl.glStencilMask(0xFF)

        # --- recursion: portals visible from the virtual camera ---
        if depth < self.MAX_PORTAL_RECURSION:
            self._draw_nested_portals(portal_a, portal_b, clip_proj, virtual_view, virtual_cam,
                                      brushes, things, lights, config, draw_scene_fn,
                                      portal_things, by_name, depth + 1)

        # rim glow (border only) — drawn with real geometry/matrices
        if portal_a.properties.get('show_rim', True):
            gl.glEnable(gl.GL_STENCIL_TEST)
            gl.glStencilFunc(gl.GL_EQUAL, depth, 0xFF)
            gl.glStencilOp(gl.GL_KEEP, gl.GL_KEEP, gl.GL_KEEP)
            gl.glStencilMask(0x00)
            gl.glEnable(gl.GL_BLEND)
            gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE)
            raw_col = portal_a.properties.get('color', [255, 255, 255])
            r, g, b = normalize_color(raw_col, default=[1.0, 1.0, 1.0])
            self._portal_upload_quad(corners_a)
            gl.glUseProgram(self._portal_rim_shader)
            gl.glUniformMatrix4fv(self._portal_rim_proj_loc, 1, gl.GL_FALSE, proj_ptr)
            gl.glUniformMatrix4fv(self._portal_rim_view_loc, 1, gl.GL_FALSE, view_ptr)
            gl.glUniform4f(self._portal_rim_color_loc, r, g, b, 0.55 * fade_a)
            gl.glBindVertexArray(self._portal_quad_vao)
            gl.glDrawArrays(gl.GL_LINE_LOOP, 0, 4)
            gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
            gl.glDisable(gl.GL_BLEND)

        # Fade overlay: black quad over the aperture, alpha = 1 − fade_a.
        if fade_a < 0.999:
            gl.glEnable(gl.GL_STENCIL_TEST)
            gl.glStencilFunc(gl.GL_EQUAL, depth, 0xFF)
            gl.glStencilOp(gl.GL_KEEP, gl.GL_KEEP, gl.GL_KEEP)
            gl.glStencilMask(0x00)
            gl.glEnable(gl.GL_BLEND)
            gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
            self._portal_upload_quad(corners_a)
            gl.glUseProgram(self._portal_rim_shader)
            gl.glUniformMatrix4fv(self._portal_rim_proj_loc, 1, gl.GL_FALSE, proj_ptr)
            gl.glUniformMatrix4fv(self._portal_rim_view_loc, 1, gl.GL_FALSE, view_ptr)
            gl.glUniform4f(self._portal_rim_color_loc, 0.0, 0.0, 0.0, 1.0 - fade_a)
            gl.glBindVertexArray(self._portal_quad_vao)
            gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, 4)
            gl.glDisable(gl.GL_BLEND)

        gl.glDisable(gl.GL_STENCIL_TEST)
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glBindVertexArray(0)

    def _draw_nested_portals(self, from_a, from_b, projection, view, cam,
                             brushes, things, lights, config, draw_scene_fn,
                             portal_things, by_name, depth):
        """Render portals visible from a virtual camera, one recursion deeper.

        Experimental (only reached when MAX_PORTAL_RECURSION > 1). Kept behind
        that constant because it costs an extra scene pass per level and has not
        been validated on-GPU in this build.
        """
        pv = projection * view
        for portal_a in portal_things:
            if getattr(portal_a, '_fade_alpha', 1.0) <= 0.01:
                continue
            # Skip the portal we are currently looking out of, to avoid an
            # immediate degenerate self-reflection.
            if portal_a is from_b:
                continue
            target_name = portal_a.properties.get('portal_target', '')
            if not target_name:
                continue
            portal_b = by_name.get(target_name)
            if portal_b is None:
                continue
            self._draw_one_portal(portal_a, portal_b, projection, view, cam,
                                  brushes, things, lights, config, draw_scene_fn,
                                  pv, portal_things, by_name, depth=depth)

    def _portal_screen_rect(self, corners, pv):
        """Screen-space integer AABB (x, y, w, h) of the aperture, clamped to the
        viewport, for use as a scissor rect.  Returns None when any corner is at
        or behind the near plane (the rect would be unreliable — caller falls
        back to no scissor / straddle handling)."""
        vp = gl.glGetIntegerv(gl.GL_VIEWPORT)
        vx, vy, vw, vh = int(vp[0]), int(vp[1]), int(vp[2]), int(vp[3])
        minx = miny = float('inf')
        maxx = maxy = float('-inf')
        for c in corners:
            clip = pv * glm.vec4(float(c[0]), float(c[1]), float(c[2]), 1.0)
            if clip.w <= 1e-5:
                return None
            sx = vx + (clip.x / clip.w * 0.5 + 0.5) * vw
            sy = vy + (clip.y / clip.w * 0.5 + 0.5) * vh
            minx = min(minx, sx); maxx = max(maxx, sx)
            miny = min(miny, sy); maxy = max(maxy, sy)
        pad = 2.0
        minx = max(vx, minx - pad)
        miny = max(vy, miny - pad)
        maxx = min(vx + vw, maxx + pad)
        maxy = min(vy + vh, maxy + pad)
        if maxx <= minx or maxy <= miny:
            return (vx, vy, 0, 0)  # off-screen
        return (int(minx), int(miny),
                int(math.ceil(maxx - minx)), int(math.ceil(maxy - miny)))

    @staticmethod
    def _frustum_planes(m):
        """Six normalized frustum planes (a,b,c,d) from a view-projection matrix
        ``m`` (column-major, m[col][row]).  A point is inside when
        a*x+b*y+c*z+d >= 0 for every plane."""
        def norm(a, b, c, d):
            l = math.sqrt(a * a + b * b + c * c)
            if l < 1e-8:
                return (0.0, 0.0, 0.0, 0.0)
            return (a / l, b / l, c / l, d / l)
        return (
            norm(m[0][3] + m[0][0], m[1][3] + m[1][0], m[2][3] + m[2][0], m[3][3] + m[3][0]),
            norm(m[0][3] - m[0][0], m[1][3] - m[1][0], m[2][3] - m[2][0], m[3][3] - m[3][0]),
            norm(m[0][3] + m[0][1], m[1][3] + m[1][1], m[2][3] + m[2][1], m[3][3] + m[3][1]),
            norm(m[0][3] - m[0][1], m[1][3] - m[1][1], m[2][3] - m[2][1], m[3][3] - m[3][1]),
            norm(m[0][3] + m[0][2], m[1][3] + m[1][2], m[2][3] + m[2][2], m[3][3] + m[3][2]),
            norm(m[0][3] - m[0][2], m[1][3] - m[1][2], m[2][3] - m[2][2], m[3][3] - m[3][2]),
        )

    @staticmethod
    def _brush_visible_in_frustum(planes, brush):
        """Conservative bounding-sphere frustum test for a brush dict.  Sphere
        (not AABB) so it stays correct for rotated brushes without inflating the
        box."""
        pos = brush.get('pos', (0.0, 0.0, 0.0))
        size = brush.get('size', (64.0, 64.0, 64.0))
        cx, cy, cz = float(pos[0]), float(pos[1]), float(pos[2])
        radius = 0.5 * math.sqrt(float(size[0]) ** 2 + float(size[1]) ** 2 + float(size[2]) ** 2)
        for a, b, c, d in planes:
            if a * cx + b * cy + c * cz + d < -radius:
                return False
        return True

    def _portal_upload_quad(self, corners):
        # corners should be a list of 4 [x,y,z] points
        vdata = np.array(corners, dtype=np.float32).flatten()
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._portal_quad_vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, vdata.nbytes, vdata)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, 0)

    def _calculate_oblique_projection(self, projection, view, plane_pos, plane_normal):
        # 1. Define the clipping plane in world space
        # The normal faces OUT of the destination portal, keeping everything in front of it.
        normal = glm.vec3(*plane_normal)
        pos = glm.vec3(*plane_pos)

        # Nudge the plane slightly backward into the wall to prevent z-fighting with the portal itself
        pos -= normal * 0.05

        dist = -glm.dot(normal, pos)
        plane_world = glm.vec4(normal.x, normal.y, normal.z, dist)

        # 2. Transform the plane to view space
        inv_trans_view = glm.transpose(glm.inverse(view))
        plane_view = inv_trans_view * plane_world

        # 3. Modify the projection matrix using Lengyel's oblique near-plane algorithm
        q = glm.vec4(
            1.0 if plane_view.x >= 0.0 else -1.0,
            1.0 if plane_view.y >= 0.0 else -1.0,
            1.0,
            1.0
        )

        # The projection is constant for the whole frame, so cache its inverse
        # instead of recomputing it for every portal (up to MAX_PORTALS*2 times).
        proj_inv = self._get_cached_proj_inverse(projection)
        q_view = proj_inv * q
        c = plane_view * (2.0 / glm.dot(plane_view, q_view))

        oblique_proj = glm.mat4(projection)
        # Replace the third column (Z-mapping) of the projection matrix
        oblique_proj[0][2] = c.x - oblique_proj[0][3]
        oblique_proj[1][2] = c.y - oblique_proj[1][3]
        oblique_proj[2][2] = c.z - oblique_proj[2][3]
        oblique_proj[3][2] = c.w - oblique_proj[3][3]

        return oblique_proj

    def _get_cached_proj_inverse(self, projection):
        # Cheap signature from the entries that actually vary between frames.
        sig = (float(projection[0][0]), float(projection[1][1]),
               float(projection[2][2]), float(projection[2][3]),
               float(projection[3][2]))
        if sig != self._portal_proj_inv_sig:
            self._portal_proj_inv = glm.inverse(projection)
            self._portal_proj_inv_sig = sig
        return self._portal_proj_inv

    def _portal_build_virtual_view(self, portal_a, portal_b, current_view, camera_pos):
        """Virtual camera for looking through portal_a out of portal_b.

        Uses the SAME shared link transform (Portal.map_point / map_direction)
        that the logic thread's teleport uses, so the view rendered through the
        aperture and the frame the player lands in can never disagree — and
        pitched / floor portals are handled because the up vector is mapped too,
        not assumed to be (0,1,0)."""
        vc = portal_a.map_point(portal_b, float(camera_pos.x), float(camera_pos.y), float(camera_pos.z))
        virtual_cam = glm.vec3(*vc)
        fwd = (-float(current_view[0][2]), -float(current_view[1][2]), -float(current_view[2][2]))
        up  = ( float(current_view[0][1]),  float(current_view[1][1]),  float(current_view[2][1]))
        nf = portal_a.map_direction(portal_b, fwd[0], fwd[1], fwd[2])
        nu = portal_a.map_direction(portal_b, up[0], up[1], up[2])
        new_fwd = glm.normalize(glm.vec3(*nf))
        new_up = glm.vec3(*nu)
        return glm.lookAt(virtual_cam, virtual_cam + new_fwd, new_up), virtual_cam


    # --------------------------------------------------------------------------
    # Angled brushes (convex geometry meshes)
    # --------------------------------------------------------------------------
    def _begin_geo_frame(self):
        """Advance the angled-brush mesh cache clock and drop stale meshes.

        Called once per rendered frame; meshes whose brush hasn't been drawn
        for a few hundred frames (deleted / undone / hidden brushes) get their
        GL buffers released.
        """
        self._geo_mesh_frame += 1
        if self._geo_mesh_frame % 240 or not self._geo_mesh_cache:
            return
        stale = [k for k, m in self._geo_mesh_cache.items()
                 if self._geo_mesh_frame - m.frame > 240]
        for k in stale:
            self._delete_geo_mesh(self._geo_mesh_cache.pop(k))

    @staticmethod
    def _delete_geo_mesh(mesh):
        try:
            if mesh.vao:
                gl.glDeleteVertexArrays(1, [mesh.vao])
            if mesh.vbo:
                gl.glDeleteBuffers(1, [mesh.vbo])
            if mesh.edge_vao:
                gl.glDeleteVertexArrays(1, [mesh.edge_vao])
            if mesh.edge_vbo:
                gl.glDeleteBuffers(1, [mesh.edge_vbo])
        except Exception:
            pass  # GL context may already be gone during shutdown

    def _get_geo_mesh(self, brush):
        """Cached :class:`BrushGeoMesh` for an angled brush, or ``None``.

        Returns ``None`` for plain box brushes (callers fall back to the
        shared cube VAO) and for degenerate plane sets.
        """
        if not brush_geometry.brush_has_geometry(brush):
            return None
        key = brush_geometry.geometry_signature(brush)
        mesh = self._geo_mesh_cache.get(id(brush))
        if mesh is not None and mesh.key == key:
            mesh.frame = self._geo_mesh_frame
            return mesh
        new = None
        convex = brush_geometry.get_convex(brush)
        if convex is not None and convex.is_valid:
            try:
                new = self._build_geo_mesh(brush, convex, key)
            except Exception as e:
                print(f"[GeoMesh] build failed: {e}")
        if mesh is not None:
            self._delete_geo_mesh(mesh)
            self._geo_mesh_cache.pop(id(brush), None)
        if new is not None:
            new.frame = self._geo_mesh_frame
            self._geo_mesh_cache[id(brush)] = new
        return new

    @staticmethod
    def _geo_uv_axes(n):
        """World axes a face's planar UVs project onto, by dominant normal
        axis.  Matches the cube VAO's orientation (v runs up walls)."""
        ax, ay, az = abs(n[0]), abs(n[1]), abs(n[2])
        if ay >= ax and ay >= az:                        # floor / ceiling
            return (1.0, 0.0, 0.0), (0.0, 0.0, -1.0)
        if ax >= az:                                     # X-facing wall
            return (0.0, 0.0, 1.0), (0.0, 1.0, 0.0)
        return (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)          # Z-facing wall

    def _build_geo_mesh(self, brush, convex, key):
        pos = brush.get('pos', [0, 0, 0])
        size = brush.get('size', [64, 64, 64])
        origin = np.array([float(pos[0]), float(pos[1]), float(pos[2])])
        scale = np.array([max(abs(float(s)), 1e-6) for s in size])

        # A "top" face (flat, at the AABB top) is emitted last so water can
        # draw walls and surface separately, like its box path does.
        def is_top(face):
            if face['normal'][1] < 0.999:
                return False
            ring_y = convex.verts[face['indices']][:, 1]
            return bool(np.all((ring_y - origin[1]) / scale[1] > 0.5 - 1e-3))

        flags = [is_top(f) for f in convex.faces]
        ordered = ([(f, False) for f, t in zip(convex.faces, flags) if not t] +
                   [(f, True) for f, t in zip(convex.faces, flags) if t])

        data = []
        runs = []
        vert_count = 0
        side_count = 0
        top_area = 0.0
        for face, top in ordered:
            idx = face['indices']
            ring_w = convex.verts[idx]                    # world space
            ring_l = (ring_w - origin) / scale            # local unit-cube space
            n = np.array(face['normal'])
            # Local-space normal chosen so normalMatrix (inverse-transpose of
            # the translate*scale model matrix) maps it back to the world one.
            ln = n * scale
            ll = math.sqrt(float(ln @ ln))
            ln = ln / ll if ll > 1e-9 else n
            ua, va = self._geo_uv_axes(n)
            us = ring_w @ np.array(ua)
            vs = ring_w @ np.array(va)
            u0, eu = float(us.min()), max(float(us.max() - us.min()), 1e-6)
            v0, ev = float(vs.min()), max(float(vs.max() - vs.min()), 1e-6)
            first = vert_count
            for k in range(1, len(idx) - 1):
                for j in (0, k, k + 1):
                    p = ring_l[j]
                    data.extend((p[0], p[1], p[2], ln[0], ln[1], ln[2],
                                 (us[j] - u0) / eu, (vs[j] - v0) / ev))
            vert_count += (len(idx) - 2) * 3
            runs.append({'face': face.get('face'), 'texture': face.get('texture'),
                         'uv_scale': face.get('uv_scale'), 'plane': face.get('plane'),
                         'first': first, 'count': vert_count - first,
                         'extent': (eu, ev)})
            if top:
                x, z = ring_l[:, 0], ring_l[:, 2]
                top_area += 0.5 * abs(float(
                    np.dot(x, np.roll(z, -1)) - np.dot(np.roll(x, -1), z)))
            else:
                side_count = vert_count

        if not data:
            return None

        mesh = BrushGeoMesh()
        mesh.key = key
        mesh.count = vert_count
        mesh.side_count = side_count
        mesh.runs = runs
        # Full unit-square footprint (area 1) means the tessellated water
        # surface grid still caps this brush exactly.
        mesh.has_flat_top = top_area >= 0.999

        arr = np.asarray(data, dtype=np.float32)
        mesh.vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(mesh.vao)
        mesh.vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, mesh.vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, arr.nbytes, arr, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(12))
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(2, 2, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(24))
        gl.glEnableVertexAttribArray(2)

        # Edge lines (local space) for the selection outline.
        edge_set = set()
        for face in convex.faces:
            idx = face['indices']
            for a, b in zip(idx, idx[1:] + idx[:1]):
                edge_set.add((a, b) if a < b else (b, a))
        everts = []
        for a, b in edge_set:
            pa = (convex.verts[a] - origin) / scale
            pb = (convex.verts[b] - origin) / scale
            everts.extend((pa[0], pa[1], pa[2], pb[0], pb[1], pb[2]))
        earr = np.asarray(everts, dtype=np.float32)
        mesh.edge_count = 2 * len(edge_set)
        mesh.edge_vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(mesh.edge_vao)
        mesh.edge_vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, mesh.edge_vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, earr.nbytes, earr, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        gl.glBindVertexArray(0)
        return mesh

    @staticmethod
    def _geo_run_plane(brush, run):
        """Live plane dict backing this run, or ``None``.

        Reading the plane live (rather than the value baked into the mesh at
        build time) lets the Face tool's texture / scale edits on a cut face
        show immediately — the mesh signature ignores texture, so it isn't
        rebuilt on a texture change.
        """
        pidx = run.get('plane')
        if pidx is None:
            return None
        planes = brush.get('geometry', {}).get('planes')
        if planes and 0 <= pidx < len(planes):
            return planes[pidx]
        return None

    @staticmethod
    def _geo_run_texture(brush, run):
        """Texture name for one face of an angled brush.

        The brush's live ``textures`` dict wins for faces that kept their box
        face tag (so editor texture changes apply immediately); cut faces read
        the texture stored live on their plane, then any brush texture.
        """
        tag = run['face']
        if tag:
            return brush.get('textures', {}).get(tag) or run['texture'] or 'default.png'
        plane = BaseRenderer._geo_run_plane(brush, run)
        tex = (plane.get('texture') if plane else None) or run['texture']
        if not tex:
            # Untagged cut face with no stored texture: borrow any brush
            # texture rather than showing the default checkerboard.
            for t in brush.get('textures', {}).values():
                if t:
                    tex = t
                    break
        return tex or 'default.png'

    def _geo_run_tex_scale(self, brush, run, tex_name):
        """UV repeat factors for one face, mirroring the box-face priorities:
        live per-face uv_scale, then the plane's stored uv_scale, then
        texture_tiling (1px = 1 world unit over the face's extent), then FIT."""
        tag = run['face']
        uv = brush.get('uv_scale', {}).get(tag) if tag else None
        if uv is None and not tag:
            # Cut face: read its plane's uv_scale live so Surface Inspector
            # edits apply without a mesh rebuild.
            plane = BaseRenderer._geo_run_plane(brush, run)
            if plane is not None:
                uv = plane.get('uv_scale')
        if uv is None:
            uv = run['uv_scale']
        if uv is not None:
            return float(uv[0]), float(uv[1])
        if brush.get('texture_tiling', False):
            tex_cache_name = os.path.join('textures', tex_name)
            tex_w, tex_h = getattr(self, '_texture_dimensions', {}).get(tex_cache_name, (128, 128))
            eu, ev = run['extent']
            return eu / max(tex_w, 1), ev / max(tex_h, 1)
        return 1.0, 1.0

    # --------------------------------------------------------------------------
    # VAO creation
    # --------------------------------------------------------------------------
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
        self._cube_vbo = vbo
        return vao

    def _create_water_surface_vao(self, subdivisions=64):
        """Tessellated unit-square grid on the cube's top face (y = +0.5).

        The water vertex shader needs real geometry to displace with Gerstner
        waves — the 2-triangle cube top gave it nothing to work with, which is
        why water used to look like a solid slab. Same attribute layout as the
        cube VAO (pos, normal, uv) so both bind to the water shader.
        """
        n = subdivisions
        verts = np.zeros(((n + 1) * (n + 1), 8), dtype=np.float32)
        idx = 0
        for j in range(n + 1):
            z = j / n - 0.5
            for i in range(n + 1):
                x = i / n - 0.5
                verts[idx] = (x, 0.5, z, 0.0, 1.0, 0.0, i / n, j / n)
                idx += 1

        indices = np.zeros(n * n * 6, dtype=np.uint32)
        k = 0
        for j in range(n):
            row = j * (n + 1)
            for i in range(n):
                a = row + i
                c = a + (n + 1)
                indices[k:k + 6] = (a, c, a + 1, a + 1, c, c + 1)
                k += 6

        vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(vao)
        vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, verts.nbytes, verts, gl.GL_STATIC_DRAW)
        ebo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, ebo)
        gl.glBufferData(gl.GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(0))
        gl.glEnableVertexAttribArray(0)
        gl.glVertexAttribPointer(1, 3, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(12))
        gl.glEnableVertexAttribArray(1)
        gl.glVertexAttribPointer(2, 2, gl.GL_FLOAT, gl.GL_FALSE, 32, ctypes.c_void_p(24))
        gl.glEnableVertexAttribArray(2)
        gl.glBindVertexArray(0)
        self._water_surface_vbo = vbo
        self._water_surface_ebo = ebo
        self._water_surface_index_count = len(indices)
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
        self._sprite_vbo = vbo
        return vao

    def _create_gizmo_buffers(self):
        axis_verts = np.array([0,0,0, 1,0,0, 0,0,0, 0,1,0, 0,0,0, 0,0,1], dtype=np.float32)
        self.vao_gizmo_lines = gl.glGenVertexArrays(1)
        vbo = gl.glGenBuffers(1)
        self._gizmo_lines_vbo = vbo
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
        self._gizmo_cone_vbo = vbo2
        gl.glBindVertexArray(self.vao_gizmo_cone)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo2)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, cone_verts.nbytes, cone_verts, gl.GL_STATIC_DRAW)
        gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        gl.glBindVertexArray(0)

    # --------------------------------------------------------------------------
    # Cleanup
    # --------------------------------------------------------------------------
    def cleanup(self):
        """Release all OpenGL resources owned by the base renderer."""
        # Delete VAOs and VBOs
        for name, vao in self.vaos.items():
            if vao:
                gl.glDeleteVertexArrays(1, [vao])
        if self._edge_vao:
            gl.glDeleteVertexArrays(1, [self._edge_vao])
        if self._edge_vbo:
            gl.glDeleteBuffers(1, [self._edge_vbo])
        if self._gizmo_lines_vbo:
            gl.glDeleteBuffers(1, [self._gizmo_lines_vbo])
        if self._gizmo_cone_vbo:
            gl.glDeleteBuffers(1, [self._gizmo_cone_vbo])
        if self._portal_outline_vao:
            gl.glDeleteVertexArrays(1, [self._portal_outline_vao])
        if self._portal_outline_vbo:
            gl.glDeleteBuffers(1, [self._portal_outline_vbo])
        if self._portal_normal_vao:
            gl.glDeleteVertexArrays(1, [self._portal_normal_vao])
        if self._portal_normal_vbo:
            gl.glDeleteBuffers(1, [self._portal_normal_vbo])
        if self._conn_line_vao:
            gl.glDeleteVertexArrays(1, [self._conn_line_vao])
        if self._conn_line_vbo:
            gl.glDeleteBuffers(1, [self._conn_line_vbo])
        if self.face_highlight_vao:
            gl.glDeleteVertexArrays(1, [self.face_highlight_vao])
        if self.face_highlight_vbo:
            gl.glDeleteBuffers(1, [self.face_highlight_vbo])
        for mesh in self._geo_mesh_cache.values():
            self._delete_geo_mesh(mesh)
        self._geo_mesh_cache.clear()
        if self._cube_vbo:
            gl.glDeleteBuffers(1, [self._cube_vbo])
        if self._sprite_vbo:
            gl.glDeleteBuffers(1, [self._sprite_vbo])
        if self._grid_vbo:
            gl.glDeleteBuffers(1, [self._grid_vbo])
        if self._water_surface_vbo:
            gl.glDeleteBuffers(1, [self._water_surface_vbo])
        if self._water_surface_ebo:
            gl.glDeleteBuffers(1, [self._water_surface_ebo])
        # Portal resources
        if self._portal_quad_vao:
            gl.glDeleteVertexArrays(1, [self._portal_quad_vao])
        if self._portal_quad_vbo:
            gl.glDeleteBuffers(1, [self._portal_quad_vbo])
        for prog in self.shaders.values():
            if prog:
                try:
                    gl.glDeleteProgram(prog)
                except Exception:
                    pass
        self.shaders.clear()
        self.uniforms.clear()
        self._shader_init_failed = True
        print("[BaseRenderer] Cleaned up GL resources.")

    # --------------------------------------------------------------------------
    # Abstract method (must be overridden by Forward/Deferred)
    # --------------------------------------------------------------------------
    def render_scene(self, projection, view, camera_pos, brushes, things, selected_object, config):
        raise NotImplementedError("Derived renderer must implement render_scene()")