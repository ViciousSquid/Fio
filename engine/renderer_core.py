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

from engine.constants import RENDER_MODE_LIT, RENDER_MODE_UNLIT, RENDER_MODE_WIREFRAME, RENDER_MODE_VERTEX, is_water_brush, brush_aabb_bounds
from engine import brush_geometry
from engine import shaders
from engine.shaders import DEFAULT_SHADERS
from engine.terrain import TERRAIN_VERTEX_SHADER, TERRAIN_FRAGMENT_SHADER
from engine.view_distance import ViewDistance
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
            fragment_src = shaders.light_ubo_source(self._read_source(fragment_file))
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
            fragment_src = shaders.light_ubo_source(fragment_src)
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


#: Light-array capacity of each lighting shader, so the renderer can never set
#: ``active_lights`` higher than the shader has room for.  Anything absent from
#: this map holds the full ``BaseRenderer.MAX_LIGHTS``.
_SHADER_LIGHT_CAPS = {
    'water': shaders.MAX_LIGHTS_WATER,
    'terrain': shaders.MAX_LIGHTS_TERRAIN,
}


# ---------- Base Renderer ----------
class BaseRenderer:
    # The dynamic-light budget, taken from the shaders rather than written down
    # again here: the renderer must never tell a shader about more lights than
    # that shader declared room for.  Per-shader caps below cover the ones that
    # are deliberately smaller (water, terrain, the ARM variants).
    MAX_LIGHTS = shaders.MAX_LIGHTS
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

    #: Uniform names of the shared distance-fog / global-ambient block
    #: (engine.shaders.FOG_GLSL). Preloaded for every shader that splices it in,
    #: and uploaded together by :meth:`_upload_env_uniforms`.
    ENV_UNIFORMS = ('uFogEnabled', 'uFogColor', 'uFogStart', 'uFogEnd',
                    'uFogDensity', 'uFogCamPos', 'uAmbient')

    def __init__(self, texture_loader, initial_grid_size, initial_world_size, config=None):
        self.texture_manager = {}
        self.loaded_models = {}
        self.load_texture_callback = texture_loader
        self._identity_mat4 = glm.mat4(1.0)
        self._identity_mat3 = glm.mat3(1.0)
        self.render_stats = RenderStats()
        self.lod_manager = LODManager()

        # How far this camera draws, and the fog that hides its far plane.
        # A renderer/camera setting, never a world-streaming one: it changes
        # what is on screen and nothing about what is loaded or simulated.
        # The viewport replaces this with the instance it shares with the
        # editor spinbox and the console, so a change there reaches the next
        # frame with no rebuild -- see QtGameView._sync_view_distance.
        self.view_distance = ViewDistance()
        # Camera position for the current frame, cached by render_scene so the
        # passes that do not receive one (sprites) can still fog correctly.
        self._frame_camera_pos = (0.0, 0.0, 0.0)

        # Performance flags.  `lowpower_mode` picks the cheaper lighting shaders, and
        # it defaults from the hardware rather than being pinned on: it used to
        # default to True everywhere, so an x86-64 desktop ran the low-power
        # shaders (and their smaller light budget) for no reason.  settings.ini
        # still overrides the guess either way.
        is_low_power, _ = shaders.detect_low_power_arm()
        if config is not None:
            # `arm_mode` is the setting's old name; an existing settings.ini
            # keeps whatever its owner chose.
            legacy = config.getboolean('Renderer', 'arm_mode', fallback=is_low_power)
            self.lowpower_mode = config.getboolean('Renderer', 'lowpower_mode',
                                                   fallback=legacy)
            self.shadows_enabled = config.getboolean('Renderer', 'shadows_enabled', fallback=not is_low_power)
            try:
                shadow_size = config.getint('Renderer', 'shadow_map_size', fallback=self.SHADOW_MAP_SIZE)
            except Exception:
                shadow_size = self.SHADOW_MAP_SIZE
        else:
            self.lowpower_mode = is_low_power
            self.shadows_enabled = not is_low_power
            shadow_size = self.SHADOW_MAP_SIZE
        # Clamp to a sane, power-of-two-ish range. Lower = faster, blockier.
        self.shadow_map_size = max(256, min(2048, int(shadow_size)))

        self.fog_quality = 'low'      # 'low' = 16 steps, 'high' = 32 steps
        self.skip_culling_in_renderer = True   # trust pre‑culled data

        self._model_matrix = glm.mat4(1.0)

        # GPU-instanced model data. One persistent VBO is shared by all model
        # VAOs; each instance carries a model matrix and normal matrix (112 B).
        self._model_instance_vbo = None
        self._model_instance_capacity = 0
        self._model_instance_data = np.empty((0, 28), dtype=np.float32)
        self._model_instanced_vaos = set()

        # Shared std140 light UBO. One upload feeds every lighting shader.
        self._light_ubo = None
        self._light_ubo_capacity = 0
        self._light_ubo_key = None
        self._light_ubo_dtype = np.dtype([
            ('position', '<f4', (4,)),
            ('color', '<f4', (4,)),
            ('params', '<f4', (4,)),
            ('indices', '<i4', (4,)),
        ])
        self._light_ubo_data = np.zeros(self.MAX_LIGHTS, dtype=self._light_ubo_dtype)

        # Depth cube-map shadow-mapping state (created lazily once GL is ready).
        self._shadow_fbo = None
        self._shadow_cubemaps = []          # texture ids, one cube-map per shadow slot
        self._light_shadow_index = {}       # id(light) -> shadow slot index for this frame
        # Per-slot cache so a light's cube-map is only re-rendered when it (or one
        # of its in-range casters) actually moves — static lights become ~free.
        self._shadow_slot_owner = [None] * self.MAX_SHADOW_LIGHTS   # id(light) per slot
        self._shadow_slot_sig = [None] * self.MAX_SHADOW_LIGHTS     # last-rendered signature

        # Cached editor-mode light collection. Threaded/play mode supplies
        # an authoritative all_lights list through RenderState; this fallback
        # avoids rescanning every Thing on every editor frame.
        self._light_collection_key = None
        self._light_collection = []

        # Per‑frame caches
        self._frame_lights = []        # shader_name -> tuple of light ids uploaded this frame; cleared at
        # the start of every render_scene() so animated lights stay fresh.
        self._frame_lights_uploaded = {}
        self._current_shader = None

        # PERF: memoized monster-sprite texture-key strings, keyed by the
        # (type, variant, sprite_type, custom) tuple that determines them —
        # avoids rebuilding the same f-string every frame for every visible
        # monster (draw_sprites runs once per visible monster per frame).
        self._sprite_tex_key_cache = {}

        # Light data now travels through the shared std140 UBO; no per-slot
        # uniform-name table is needed on the render path.

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
        # Component-edit handle overlay (editor only).  The buffer is refilled
        # only when the editor's overlay version changes, never per frame.
        self._component_overlay_vao = None
        self._component_overlay_vbo = None
        self._component_overlay_data = None
        self._component_overlay_counts = None
        self._component_overlay_version = None
        self._component_overlay_dirty = False
        # Driver limits for wide lines / big points, queried once on first use
        # (they need a live context, and glGetFloatv stalls the pipeline).
        self._line_width_range = None
        self._point_size_range = None
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
    @staticmethod
    def _detect_lowpower_platform():
        """Whether this machine wants the low-power lighting shaders.

        Delegates to :func:`engine.shaders.detect_low_power_arm`, which is where
        the rule lives now — the renderer and the Settings window used to detect
        this separately and could reach different answers about one machine.
        """
        return shaders.detect_low_power_arm()[0]

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
            self.uniforms['sprite'].preload(self.ENV_UNIFORMS)

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
            self.uniforms['glass'].preload(self.ENV_UNIFORMS)
            # fog – use ARM‑optimised fragment shader (works everywhere)
            fog_vert = DEFAULT_SHADERS.get('fog.vert', '')
            fog_frag = DEFAULT_SHADERS.get('fog_arm.frag', DEFAULT_SHADERS.get('fog.frag', ''))
            self.shaders['fog'] = self.shader_loader.compile_from_source(fog_vert, fog_frag)
            self.uniforms['fog'] = UniformCache(self.shaders['fog'])
            self._preload_fog_uniforms()

            # terrain
            try:
                terrain_vs = compileShader(TERRAIN_VERTEX_SHADER, gl.GL_VERTEX_SHADER)
                terrain_fs = compileShader(
                    shaders.light_ubo_source(TERRAIN_FRAGMENT_SHADER),
                    gl.GL_FRAGMENT_SHADER,
                )
                terrain_program = compileProgram(terrain_vs, terrain_fs, validate=False)
                self.shaders['terrain'] = terrain_program
                self.uniforms['terrain'] = UniformCache(terrain_program)
                self.uniforms['terrain'].preload([
                    'projection', 'view', 'active_lights',
                    'texGrass', 'texRock', 'texSand', 'texSnow',
                    'biomeWeights', 'terrainHeightScale'
                ])
                self.uniforms['terrain'].preload(self.ENV_UNIFORMS)
                print("Terrain shader loaded")
            except Exception as e:
                print(f"Terrain shader error: {e}")
                self.shaders['terrain'] = None

            # lit and textured shaders (needed for forward fallback in Deferred)
            if self.lowpower_mode:
                self._compile_arm_shaders()
            else:
                self._compile_standard_shaders()

            self._configure_light_ubo_programs()
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
        self._compile_instanced_model_shaders(lit_vert, lit_frag, tex_vert, tex_frag)

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
        lit_vert = DEFAULT_SHADERS.get('lit.vert', '')
        lit_frag = DEFAULT_SHADERS.get('lit.frag', '')
        tex_vert = DEFAULT_SHADERS.get('textured.vert', '')
        tex_frag = DEFAULT_SHADERS.get('textured.frag', '')
        self._compile_instanced_model_shaders(lit_vert, lit_frag, tex_vert, tex_frag)

    def _compile_instanced_model_shaders(self, lit_vert, lit_frag, tex_vert, tex_frag):
        """Compile GL 3.3 model shaders whose transforms come from instanced attributes."""
        instance_attrs = """layout (location = 3) in vec4 iModel0;
layout (location = 4) in vec4 iModel1;
layout (location = 5) in vec4 iModel2;
layout (location = 6) in vec4 iModel3;
layout (location = 7) in vec4 iNormal0;
layout (location = 8) in vec4 iNormal1;
layout (location = 9) in vec4 iNormal2;

"""

        def make_vertex(source):
            if not source:
                raise ValueError('missing model vertex shader source')
            source = source.replace('uniform mat4 model;\n', '')
            source = source.replace('uniform mat3 normalMatrix;\n', '')
            if 'out vec3 FragPos;' not in source:
                raise ValueError('unexpected model vertex shader interface')
            source = source.replace('out vec3 FragPos;', instance_attrs + 'out vec3 FragPos;', 1)
            source = source.replace(
                'void main() {',
                'void main() {\n'
                '    mat4 instanceModel = mat4(iModel0, iModel1, iModel2, iModel3);\n'
                '    mat3 instanceNormal = mat3(iNormal0.xyz, iNormal1.xyz, iNormal2.xyz);\n',
                1,
            )
            source = source.replace('model * vec4(aPos, 1.0)', 'instanceModel * vec4(aPos, 1.0)')
            source = source.replace('normalMatrix * aNormal', 'instanceNormal * aNormal')
            return source
        try:
            self.shaders['lit_instanced'] = self.shader_loader.compile_from_source(
                make_vertex(lit_vert), lit_frag)
            self.uniforms['lit_instanced'] = UniformCache(self.shaders['lit_instanced'])
            self._preload_lit_uniforms('lit_instanced')

            self.shaders['textured_instanced'] = self.shader_loader.compile_from_source(
                make_vertex(tex_vert), tex_frag)
            self.uniforms['textured_instanced'] = UniformCache(self.shaders['textured_instanced'])
            self._preload_lit_uniforms('textured_instanced')
            self.uniforms['textured_instanced'].preload(
                ['texture_diffuse', 'tex_scale', 'tex_angle', 'tex_shift', 'normalMatrix'])
            print('[BaseRenderer] GPU model instancing shaders compiled successfully.')
        except Exception as exc:
            # The ordinary model shaders remain authoritative if an older/quirky
            # driver rejects the instanced attribute interface.
            for name in ('lit_instanced', 'textured_instanced'):
                self.uniforms.pop(name, None)
                program = self.shaders.pop(name, None)
                if program:
                    try:
                        gl.glDeleteProgram(program)
                    except Exception:
                        pass
            print(f'[BaseRenderer] GPU model instancing disabled: {exc}')

    def _preload_lit_uniforms(self, shader_name):
        uniforms = self.uniforms[shader_name]
        uniforms.preload(['projection', 'view', 'model', 'object_color', 'alpha', 'active_lights'])
        uniforms.preload(self.ENV_UNIFORMS)

    def _preload_water_uniforms(self):
        uniforms = self.uniforms['water']
        uniforms.preload(['projection', 'view', 'model', 'time', 'viewPos', 'normalMap', 'waterOpacity',
                          'waterReflectivity', 'waterTint', 'normalMatrix', 'waveAmp', 'brushSize'])
        uniforms.preload(self.ENV_UNIFORMS)

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
        terrain_lights = list(lights) if lights else []
        max_terrain_lights = shaders.MAX_LIGHTS_TERRAIN
        if len(terrain_lights) > max_terrain_lights:
            cx, cy, cz = self._camera_xyz(camera_pos)
            terrain_lights.sort(
                key=lambda light: (
                    (float(light.pos[0]) - cx) ** 2 +
                    (float(light.pos[1]) - cy) ** 2 +
                    (float(light.pos[2]) - cz) ** 2
                )
            )
            terrain_lights = terrain_lights[:max_terrain_lights]
        self._upload_lights_once('terrain', terrain_lights)
        active_lights_count = len(terrain_lights)
        gl.glDisable(gl.GL_CULL_FACE)
        if hasattr(terrain, 'get_tri_count'):
            self.render_stats.visible_tris += terrain.get_tri_count()
        terrain.update_and_render(
            projection, view, camera_pos, frustum_planes, terrain_lights, active_lights_count,
            shadow_cubemaps=(self._shadow_cubemaps if self.shadows_enabled else None),
            shadow_index_map=self._light_shadow_index,
            shadow_unit_base=self.SHADOW_TEXTURE_UNIT_BASE,
            env_uniforms=self.env_uniform_values(),
        )

    # --------------------------------------------------------------------------
    # Models
    # --------------------------------------------------------------------------
    def load_model(self, filename):
        """Load a 3D model (OBJ or GLB).

        The normal render-time case is an already-loaded model. Keep that path
        to a single dictionary lookup; path normalisation and filesystem work
        belong exclusively to cache misses.
        """
        if not filename:
            return None

        # HOT PATH: model_path values are normally identical strings frame to
        # frame, so this is the entire lookup on the common render path.
        model = self.loaded_models.get(filename)
        if model is not None:
            return model

        # Cache miss only: normalise alternate slash/absolute-path spellings
        # so editor/package/file-dialog paths still collapse to one resource.
        original_filename = str(filename)
        normalized_filename = os.path.normpath(
            original_filename.replace('/', os.sep).replace('\\', os.sep)
        )
        cache_key = os.path.normcase(normalized_filename)

        model = self.loaded_models.get(cache_key)
        if model is not None:
            # Alias this exact authored path so subsequent frames stay on the
            # one-dictionary-lookup path above.
            self.loaded_models[filename] = model
            return model

        full_path = normalized_filename
        if not os.path.isabs(full_path):
            candidate = os.path.join('assets', 'models', full_path)
            if os.path.exists(candidate):
                full_path = candidate
            elif os.path.exists(original_filename):
                full_path = original_filename

        if not os.path.exists(full_path):
            print(f"Failed to load model: {filename}")
            return None

        print(f"Loading model: {full_path}")

        # Determine format by extension
        ext = os.path.splitext(full_path)[1].lower()

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
            self.loaded_models[cache_key] = model
            self.loaded_models[filename] = model
            return model

        print(f"Failed to load model: {filename}")
        return None

    def get_loaded_model(self, filename):
        """Return a model already loaded by this renderer without touching GL."""
        if not filename:
            return None

        model = self.loaded_models.get(filename)
        if model is not None:
            return model

        normalized_filename = os.path.normpath(
            str(filename).replace('/', os.sep).replace('\\', os.sep)
        )
        cache_key = os.path.normcase(normalized_filename)
        model = self.loaded_models.get(cache_key)
        if model is not None:
            self.loaded_models[filename] = model
        return model

    def _ensure_model_instance_buffer(self, count):
        if count <= 0:
            return
        if self._model_instance_vbo is None:
            self._model_instance_vbo = gl.glGenBuffers(1)
        if count > self._model_instance_capacity:
            capacity = max(16, self._model_instance_capacity)
            while capacity < count:
                capacity *= 2
            self._model_instance_capacity = capacity
            self._model_instance_data = np.empty((capacity, 28), dtype=np.float32)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._model_instance_vbo)
            gl.glBufferData(
                gl.GL_ARRAY_BUFFER,
                self._model_instance_data.nbytes,
                None,
                gl.GL_DYNAMIC_DRAW,
            )

    def _ensure_model_instance_vao(self, vao):
        key = int(vao)
        if key in self._model_instanced_vaos:
            return
        if self._model_instance_vbo is None:
            self._ensure_model_instance_buffer(1)
        gl.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._model_instance_vbo)
        stride = 28 * 4
        offsets = (0, 16, 32, 48, 64, 80, 96)
        for location, offset in zip(range(3, 10), offsets):
            gl.glVertexAttribPointer(
                location, 4, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(offset))
            gl.glEnableVertexAttribArray(location)
            gl.glVertexAttribDivisor(location, 1)
        gl.glBindVertexArray(0)
        self._model_instanced_vaos.add(key)

    def _fill_model_instance_buffer(self, things):
        count = len(things)
        self._ensure_model_instance_buffer(count)
        out = self._model_instance_data[:count]
        for i, thing in enumerate(things):
            mat = self._thing_model_matrix(thing)
            model_np = getattr(thing, '_render_model_mat_np_cache', None)
            normal_np = getattr(thing, '_render_model_nmat_np_cache', None)
            if model_np is None or normal_np is None:
                normal = getattr(thing, '_render_model_nmat_cache', self._identity_mat3)
                model_np = np.array([
                    mat[0][0], mat[0][1], mat[0][2], mat[0][3],
                    mat[1][0], mat[1][1], mat[1][2], mat[1][3],
                    mat[2][0], mat[2][1], mat[2][2], mat[2][3],
                    mat[3][0], mat[3][1], mat[3][2], mat[3][3],
                ], dtype=np.float32)
                normal_np = np.array([                    normal[0][0], normal[0][1], normal[0][2], 0.0,
                    normal[1][0], normal[1][1], normal[1][2], 0.0,
                    normal[2][0], normal[2][1], normal[2][2], 0.0,
                ], dtype=np.float32)
                thing._render_model_mat_np_cache = model_np
                thing._render_model_nmat_np_cache = normal_np
            out[i, :16] = model_np
            out[i, 16:28] = normal_np
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._model_instance_vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, out)

    def _model_texture_id(self, tex_name, material=None, manual=False):
        if not tex_name:
            return 0
        resolved_path = self._resolve_model_texture_path(
            material or {'texture': tex_name}, tex_name)
        use_direct = bool(
            resolved_path and os.path.exists(resolved_path) and
            (not manual or not resolved_path.startswith('assets'))
        )
        if not use_direct:
            return self.load_texture(tex_name, 'textures')
        tex_cache_name = f'model_tex:{resolved_path}'
        tex_id = self.texture_manager.get(tex_cache_name)
        if tex_id is not None:
            return tex_id
        try:
            from PIL import Image
            img = Image.open(resolved_path).convert('RGBA')
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
            tex_id = gl.glGenTextures(1)
            self.texture_manager[tex_cache_name] = tex_id
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexImage2D(
                gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, img.width, img.height, 0,
                gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, img.tobytes())
            gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
            return tex_id
        except Exception as exc:
            print(f'[Renderer] Model texture load failed for {resolved_path}: {exc}')
            return self.load_texture(tex_name, 'textures')

    def _prepare_model_shader(self, shader_name, projection, view, lights, current_shader):
        program = self.shaders.get(shader_name)
        if not program:
            return current_shader
        if current_shader != shader_name:
            gl.glUseProgram(program)
            u = self.uniforms[shader_name]
            gl.glUniformMatrix4fv(u['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
            gl.glUniformMatrix4fv(u['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
            self._upload_lights_once(shader_name, lights)
            if shader_name.startswith('textured'):
                gl.glActiveTexture(gl.GL_TEXTURE0)
                gl.glUniform1i(u['texture_diffuse'], 0)
                if u.get('tex_scale', -1) != -1:
                    gl.glUniform2f(u['tex_scale'], 1.0, 1.0)
                if u.get('tex_angle', -1) != -1:
                    gl.glUniform1f(u['tex_angle'], 0.0)
                if u.get('tex_shift', -1) != -1:
                    gl.glUniform2f(u['tex_shift'], 0.0, 0.0)
            current_shader = shader_name
        return current_shader

    def _draw_model_batch_instanced(self, batch, projection, view, lights, current_shader):
        shader_name = batch['shader'] + '_instanced'
        current_shader = self._prepare_model_shader(
            shader_name, projection, view, lights, current_shader)
        if current_shader != shader_name:
            return current_shader

        obj = batch['obj']
        things = batch['things']
        u = self.uniforms[shader_name]
        if batch['shader'] == 'textured':
            gl.glBindTexture(gl.GL_TEXTURE_2D, batch.get('texture_id', 0))
        else:
            color = batch.get('color', (0.8, 0.8, 0.8))
            gl.glUniform3fv(u['object_color'], 1, color)
            gl.glUniform1f(u['alpha'], 1.0)

        self._fill_model_instance_buffer(things)
        self._ensure_model_instance_vao(obj.vao)
        gl.glBindVertexArray(obj.vao)
        group = batch.get('group')
        if group is not None:
            if group.get('indexed', False) and getattr(obj, 'ebo', None) is not None:
                gl.glDrawElementsInstanced(
                    gl.GL_TRIANGLES, group['count'], gl.GL_UNSIGNED_INT,
                    ctypes.c_void_p(group['start'] * 4), len(things))
            else:
                gl.glDrawArraysInstanced(
                    gl.GL_TRIANGLES, group['start'], group['count'], len(things))
        else:
            gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, obj.vertex_count, len(things))
        self.render_stats.draw_calls += 1
        self.render_stats.batched_draws += 1
        return current_shader

    def _draw_model_batch_single(self, batch, projection, view, lights, current_shader):
        shader_name = batch['shader']
        current_shader = self._prepare_model_shader(
            shader_name, projection, view, lights, current_shader)
        if current_shader != shader_name:
            return current_shader

        obj = batch['obj']
        thing = batch['things'][0]
        mat = self._thing_model_matrix(thing)
        normal_mat = getattr(thing, '_render_model_nmat_cache', self._identity_mat3)
        u = self.uniforms[shader_name]
        if shader_name == 'textured':
            gl.glBindTexture(gl.GL_TEXTURE_2D, batch.get('texture_id', 0))
        else:
            color = batch.get('color', (0.8, 0.8, 0.8))
            gl.glUniform3fv(u['object_color'], 1, color)
            gl.glUniform1f(u['alpha'], 1.0)
        gl.glUniformMatrix4fv(u['model'], 1, gl.GL_FALSE, glm.value_ptr(mat))
        normal_loc = u.get('normalMatrix', -1)
        if normal_loc >= 0:
            gl.glUniformMatrix3fv(normal_loc, 1, gl.GL_FALSE, glm.value_ptr(normal_mat))
        gl.glBindVertexArray(obj.vao)
        group = batch.get('group')
        if group is not None:
            if group.get('indexed', False) and getattr(obj, 'ebo', None) is not None:
                gl.glDrawElements(gl.GL_TRIANGLES, group['count'], gl.GL_UNSIGNED_INT,
                                  ctypes.c_void_p(group['start'] * 4))
            else:
                gl.glDrawArrays(gl.GL_TRIANGLES, group['start'], group['count'])
        else:
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, obj.vertex_count)
        self.render_stats.draw_calls += 1
        return current_shader

    def draw_models(self, projection, view, camera_pos, models, lights, config):
        if not models:
            return

        lit_shader = self.shaders.get('lit')
        textured_shader = self.shaders.get('textured')
        if not lit_shader and not textured_shader:
            return

        cull_was_enabled = gl.glIsEnabled(gl.GL_CULL_FACE)
        gl.glDisable(gl.GL_CULL_FACE)

        # Build material/mesh batches once. Identical model geometry and material
        # state shares one instanced draw instead of one GL draw per Thing.
        batches = {}
        for thing in models:
            props = getattr(thing, 'properties', {})
            model_file = props.get('model_path')
            if not model_file:
                continue
            obj = self.load_model(model_file)
            if not obj or not obj.is_loaded:
                continue

            self.render_stats.visible_tris += obj.vertex_count // 3
            manual_texture = props.get('texture')

            if obj.groups and not manual_texture:
                for group in obj.groups:
                    material = obj.materials.get(
                        group['material'],
                        {'color': [0.8, 0.8, 0.8], 'texture': None})
                    use_texture = material.get('texture')
                    shader_kind = (
                        'textured' if use_texture and textured_shader
                        else 'lit' if lit_shader else None)
                    if not shader_kind:
                        continue
                    color = tuple(material.get('color', [0.8, 0.8, 0.8]))
                    key = (
                        id(obj), group.get('start', 0), group.get('count', 0),
                        bool(group.get('indexed', False)), shader_kind,
                        str(use_texture) if shader_kind == 'textured' else color,
                    )
                    batch = batches.get(key)
                    if batch is None:
                        batch = {
                            'obj': obj, 'group': group, 'things': [],
                            'shader': shader_kind, 'material': material,
                            'texture_name': use_texture, 'color': color,
                        }
                        batches[key] = batch
                    batch['things'].append(thing)
            else:
                tex_name = manual_texture
                shader_kind = (
                    'textured' if tex_name and textured_shader
                    else 'lit' if lit_shader else None)
                if not shader_kind:
                    continue
                color = tuple(props.get('color', [0.8, 0.8, 0.8]))
                key = (
                    id(obj), 0, obj.vertex_count, False, shader_kind,
                    str(tex_name) if shader_kind == 'textured' else color,
                )
                batch = batches.get(key)
                if batch is None:
                    batch = {
                        'obj': obj, 'group': None, 'things': [],
                        'shader': shader_kind, 'material': None,
                        'texture_name': tex_name, 'color': color,
                    }
                    batches[key] = batch
                batch['things'].append(thing)

        current_shader = None
        for batch in batches.values():
            tex_name = batch.get('texture_name')
            if tex_name:
                batch['texture_id'] = self._model_texture_id(
                    tex_name, batch.get('material'),
                    manual=bool(batch.get('material') is None))

            things = batch['things']
            instanced_shader = self.shaders.get(batch['shader'] + '_instanced')
            if len(things) >= 2 and instanced_shader:
                current_shader = self._draw_model_batch_instanced(
                    batch, projection, view, lights, current_shader)
            else:
                current_shader = self._draw_model_batch_single(
                    batch, projection, view, lights, current_shader)

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
        # Billboards are unlit, so they never reach _upload_lights_once — they
        # still need fogging, or a distant monster would hang un-faded in front
        # of fully fogged geometry.
        self._upload_env_uniforms('sprite')
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
                    elif getattr(thing, 'properties', {}).get('sprite_path'):
                        sprite_path = thing.properties.get('sprite_path')
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
                elif getattr(thing, 'properties', {}).get('sprite_path'):
                    size = thing.properties.get('sprite_size', [32.0, 32.0])
                    try:
                        gl.glUniform2f(size_loc, float(size[0]), float(size[1]))
                    except (TypeError, ValueError, IndexError):
                        gl.glUniform2f(size_loc, 32.0, 32.0)
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
        # Glass lights itself from the view angle rather than from the light
        # list, so it never reaches _upload_lights_once — but a distant pane
        # still has to fog with everything around it.
        self._upload_env_uniforms('glass')
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
    @staticmethod
    def _thing_render_kind(thing):
        """Cache the type-derived render category of a Thing."""
        props = getattr(thing, 'properties', {})
        key = (type(thing), bool(props.get('sprite_path')))
        cached = getattr(thing, '_render_kind_cache', None)
        if cached is not None and cached[0] == key:
            return cached[1]

        if isinstance(thing, Pickup):
            kind = 'pickup'
        elif isinstance(thing, (Monster, LogicGate, LogicRelay, LogicTimer, LevelChanger)):
            kind = 'entity_sprite'
        elif key[1]:
            kind = 'sprite'
        else:
            kind = 'ordinary'

        thing._render_kind_cache = (key, kind)
        return kind

    def _sort_objects(self, brushes, things, config, model_out=None,
                      brush_positions=None, thing_positions=None,
                      collect_sort_positions=False):
        opaque, transparent, sprites, fog, water, glass, glow = [], [], [], [], [], [], []
        is_play = config.get('play_mode', False)
        show_sprites = config.get('show_sprites_in_play_mode', False)

        if collect_sort_positions:
            transparent_pos, water_pos = [], []
            glass_pos, sprite_pos = [], []
        else:
            transparent_pos = water_pos = glass_pos = sprite_pos = None

        for i, brush in enumerate(brushes):
            if brush.get('hidden'):
                continue
            shader = brush.get('shader')
            pos = brush_positions[i] if brush_positions is not None else None
            if is_water_brush(brush):
                water.append(brush)
                if water_pos is not None:
                    water_pos.append(pos)
            elif brush.get('is_fog') or shader == 'Fog':
                fog.append(brush)
            elif shader == 'Glass':
                glass.append(brush)
                if glass_pos is not None:
                    glass_pos.append(pos)
            elif shader == 'Glow':
                glow.append(brush)
            elif brush.get('is_trigger'):
                if not is_play:
                    transparent.append(brush)
                    if transparent_pos is not None:
                        transparent_pos.append(pos)
            else:
                opaque.append(brush)

        for i, t in enumerate(things):
            pos = thing_positions[i] if thing_positions is not None else None
            if PathNode is not None and isinstance(t, PathNode):
                continue
            if Portal is not None and isinstance(t, Portal):
                sprites.append(t)
                if sprite_pos is not None:
                    sprite_pos.append(pos)
                continue
            if isinstance(t, dict) and 'monster_type' in t:
                sprites.append(t)
                if sprite_pos is not None:
                    sprite_pos.append(pos)
                continue
            if not isinstance(t, Thing):
                continue

            props = getattr(t, 'properties', {})
            model_path = props.get('model_path')
            model_visible = bool(model_path and not props.get('hidden', False))
            if model_out is not None and model_visible:
                model_out.append(t)
                continue

            kind = self._thing_render_kind(t)
            if kind == 'pickup' or kind == 'entity_sprite':
                sprites.append(t)
            elif model_path:
                sprites.append(t)
            elif kind == 'sprite':
                sprites.append(t)
            elif not is_play or show_sprites:
                sprites.append(t)

            if sprite_pos is not None and sprites and sprites[-1] is t:
                sprite_pos.append(pos)

        result = (opaque, transparent, sprites, fog, water, glass, glow)
        if collect_sort_positions:
            return result + ({
                'transparent': transparent_pos,
                'water': water_pos,
                'glass': glass_pos,
                'sprites': sprite_pos,
            },)
        return result

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

    def _shader_light_cap(self, shader_name):
        """How many lights ``shader_name``'s ``lights[]`` array actually holds.

        The shader loops to ``active_lights``, so uploading a larger count than
        the array can hold used to make it index past the end — undefined
        behaviour, and the surplus lights never worked anyway.  Water and
        terrain are sized smaller on purpose; the ARM variants trade array size
        for uniform storage.
        """
        cap = _SHADER_LIGHT_CAPS.get(shader_name, self.MAX_LIGHTS)
        if self.lowpower_mode and shader_name in ('lit', 'textured', 'lit_instanced', 'textured_instanced'):
            cap = min(cap, shaders.MAX_LIGHTS_ARM)
        return cap

    def env_uniform_values(self):
        """The fog/ambient block as a ``{uniform_name: value}`` dict.

        For subsystems that own their GL program and uniform table rather than
        going through :attr:`uniforms` — the terrain is the one that does.
        Types are meaningful: ``int`` uploads as ``glUniform1i``, ``float`` as
        ``glUniform1f``, a 3-sequence as ``glUniform3f``.
        """
        vd = self.view_distance
        start, end = vd.resolve()
        return {
            'uFogEnabled': 1 if vd.fog_enabled else 0,
            'uFogColor': tuple(vd.fog_color),
            'uFogStart': float(start),
            'uFogEnd': float(end),
            'uFogDensity': float(vd.fog_density),
            'uFogCamPos': tuple(self._frame_camera_pos),
            'uAmbient': tuple(vd.ambient),
        }

    def _upload_env_uniforms(self, shader_name):
        """Upload the distance-fog and global-ambient block to *shader_name*.

        Cheap and unconditional -- seven uniform writes for a shader that reads
        them, and seven no-ops (location -1) for one that does not, which is
        what makes it safe to call for every shader without tracking which
        splice in ``FOG_GLSL``. Being unconditional is also what makes the
        editor spinbox update the fog live: there is no cached state between
        :class:`~engine.view_distance.ViewDistance` and the next frame's draw.
        """
        uniforms = self.uniforms.get(shader_name)
        if uniforms is None:
            return
        vd = self.view_distance
        start, end = vd.resolve()
        cam = self._frame_camera_pos
        gl.glUniform1i(uniforms['uFogEnabled'], 1 if vd.fog_enabled else 0)
        gl.glUniform3f(uniforms['uFogColor'], *vd.fog_color)
        gl.glUniform1f(uniforms['uFogStart'], start)
        gl.glUniform1f(uniforms['uFogEnd'], end)
        gl.glUniform1f(uniforms['uFogDensity'], vd.fog_density)
        gl.glUniform3f(uniforms['uFogCamPos'], cam[0], cam[1], cam[2])
        gl.glUniform3f(uniforms['uAmbient'], *vd.ambient)

    @staticmethod
    def _camera_xyz(camera_pos):
        """``(x, y, z)`` from a glm vec, a sequence, or ``None``."""
        if camera_pos is None:
            return (0.0, 0.0, 0.0)
        if hasattr(camera_pos, 'x'):
            return (float(camera_pos.x), float(camera_pos.y), float(camera_pos.z))
        return (float(camera_pos[0]), float(camera_pos[1]), float(camera_pos[2]))

    def _configure_light_ubo_program(self, shader_name):
        """Bind a compiled lighting shader's std140 block to the shared slot."""
        program = self.shaders.get(shader_name)
        if not program:
            return False
        block_index = gl.glGetUniformBlockIndex(program, 'FioLightBlock')
        invalid = getattr(gl, 'GL_INVALID_INDEX', 0xFFFFFFFF)
        if block_index == invalid:
            return False
        gl.glUniformBlockBinding(program, block_index, shaders.LIGHT_UBO_BINDING)
        return True

    def _configure_light_ubo_programs(self):
        for name in ('lit', 'textured', 'lit_instanced', 'textured_instanced',
                     'water', 'terrain'):
            self._configure_light_ubo_program(name)
        self._ensure_light_ubo(self.MAX_LIGHTS)

    def _ensure_light_ubo(self, capacity):
        capacity = max(1, int(capacity))
        if self._light_ubo is None:
            self._light_ubo = gl.glGenBuffers(1)
        if capacity > self._light_ubo_capacity:
            self._light_ubo_capacity = max(self.MAX_LIGHTS, capacity)
            self._light_ubo_data = np.zeros(self._light_ubo_capacity,
                                            dtype=self._light_ubo_dtype)
            gl.glBindBuffer(gl.GL_UNIFORM_BUFFER, self._light_ubo)
            gl.glBufferData(
                gl.GL_UNIFORM_BUFFER,
                self._light_ubo_data.nbytes,
                None,
                gl.GL_DYNAMIC_DRAW,
            )
        gl.glBindBufferBase(
            gl.GL_UNIFORM_BUFFER,
            shaders.LIGHT_UBO_BINDING,
            self._light_ubo,
        )

    def _upload_light_ubo(self, lights, count):
        """Pack the active light slice with NumPy and upload it once per render pass.

        render_scene() clears _light_ubo_key at the start of each pass, so
        moving lights and animated light properties are repacked before the
        first lighting draw. The ID key only suppresses duplicate uploads
        for subsequent lighting shader passes in the same render_scene().
        """
        count = min(int(count), self.MAX_LIGHTS)
        self._ensure_light_ubo(count)
        if count <= 0:
            self._light_ubo_key = ()
            return

        key = tuple(id(light) for light in lights[:count])
        if self._light_ubo_key == key:
            return

        active = self._light_ubo_data[:count]
        active[...] = 0
        active_lights = lights[:count]

        # Light counts are capped at 64 (16 on the low-power shader path).
        # Object values are gathered once; contiguous std140 packing is then
        # handled by NumPy rather than a Python loop over every UBO field.
        positions = np.asarray([light.pos for light in active_lights], dtype=np.float32)
        colors = np.asarray([light.get_color() for light in active_lights], dtype=np.float32)
        params = np.asarray(
            [[light.get_intensity(), light.get_radius()] for light in active_lights],
            dtype=np.float32,
        )
        shadow_indices = np.fromiter(
            (self._light_shadow_index.get(id(light), -1) for light in active_lights),
            dtype=np.int32,
            count=count,
        )

        active['position'][:, :3] = positions
        active['position'][:, 3] = 1.0
        active['color'][:, :3] = colors
        active['color'][:, 3] = 1.0
        active['params'][:, :2] = params
        active['indices'][:, 0] = shadow_indices

        gl.glBindBuffer(gl.GL_UNIFORM_BUFFER, self._light_ubo)
        gl.glBufferSubData(gl.GL_UNIFORM_BUFFER, 0, active[:count])
        self._light_ubo_key = key

    # --------------------------------------------------------------------------
    # Light upload
    # --------------------------------------------------------------------------
    def _upload_lights_once(self, shader_name, lights):
        if shader_name not in self.uniforms:
            return

        # Fog/ambient remain regular uniforms because they are not shared light
        # state.
        self._upload_env_uniforms(shader_name)
        cap = self._shader_light_cap(shader_name)
        num_lights = min(len(lights), cap)

        # Bind the shared block for this program even when the light list itself
        # is unchanged. The UBO contents are uploaded only when the light IDs
        # change for the frame/pass.
        self._ensure_light_ubo(num_lights)
        gl.glBindBufferBase(
            gl.GL_UNIFORM_BUFFER,
            shaders.LIGHT_UBO_BINDING,
            self._light_ubo,
        )
        self._frame_lights_uploaded[shader_name] = tuple(map(id, lights[:cap]))
        gl.glUniform1i(self.uniforms[shader_name]['active_lights'], num_lights)
        self._upload_light_ubo(lights, num_lights)

        # Keep sampler2D and samplerCube uniforms on distinct texture units.
        # This is one shader-pass operation, never part of the per-draw loop.
        if shader_name in ('lit', 'textured', 'lit_instanced',
                           'textured_instanced', 'terrain'):
            self._bind_shadow_maps(self.uniforms[shader_name])
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
        """Return a cached model matrix for a model-carrying Thing."""
        props = getattr(thing, 'properties', {})
        pos = thing.pos
        rot = props.get('rotation', [0, 0, 0])
        scale = props.get('scale', 1.0)
        scale_key = (scale, scale, scale) if isinstance(scale, (int, float)) else tuple(scale)
        key = (pos[0], pos[1], pos[2], tuple(rot), scale_key)
        if getattr(thing, '_render_model_mat_key', None) == key:
            return thing._render_model_mat_cache

        scale_vec = (scale, scale, scale) if isinstance(scale, (int, float)) else scale
        mat = glm.translate(self._identity_mat4, glm.vec3(*pos))
        mat = glm.rotate(mat, glm.radians(rot[1]), glm.vec3(0, 1, 0))
        mat = glm.rotate(mat, glm.radians(rot[0]), glm.vec3(1, 0, 0))
        mat = glm.rotate(mat, glm.radians(rot[2]), glm.vec3(0, 0, 1))
        mat = glm.scale(mat, glm.vec3(*scale_vec))
        try:
            normal = glm.transpose(glm.inverse(glm.mat3(mat)))
        except Exception:
            normal = self._identity_mat3
        thing._render_model_mat_key = key
        thing._render_model_mat_cache = mat
        thing._render_model_nmat_cache = normal
        # Numpy copies are generated only when the transform changes; the
        # instanced renderer can then copy cached arrays into one GPU batch.
        thing._render_model_mat_np_cache = np.array([
            mat[0][0], mat[0][1], mat[0][2], mat[0][3],
            mat[1][0], mat[1][1], mat[1][2], mat[1][3],
            mat[2][0], mat[2][1], mat[2][2], mat[2][3],
            mat[3][0], mat[3][1], mat[3][2], mat[3][3],
        ], dtype=np.float32)
        thing._render_model_nmat_np_cache = np.array([
            normal[0][0], normal[0][1], normal[0][2], 0.0,
            normal[1][0], normal[1][1], normal[1][2], 0.0,
            normal[2][0], normal[2][1], normal[2][2], 0.0,
        ], dtype=np.float32)
        return mat

    def _bind_shadow_maps(self, uniforms):
        """Bind shadow samplers to dedicated texture units.

        Lighting shaders contain both sampler2D and samplerCube uniforms.
        OpenGL requires sampler uniforms of different types to reference
        different texture units at draw time, even when no shadowing light
        is active. Keep the cube samplers on their reserved units and bind
        texture 0 when shadow resources are unavailable.
        """
        base = self.SHADOW_TEXTURE_UNIT_BASE
        cubemaps = self._shadow_cubemaps
        for i in range(self.MAX_SHADOW_LIGHTS):
            loc = uniforms[f'shadowMaps[{i}]']
            if loc == -1:
                continue
            gl.glActiveTexture(gl.GL_TEXTURE0 + base + i)
            cm = cubemaps[i] if i < len(cubemaps) else 0
            gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, cm)
            gl.glUniform1i(loc, base + i)
        gl.glActiveTexture(gl.GL_TEXTURE0)

    def _prepare_shadow_caster_batch(self, brushes, models):
        """Build one numeric caster snapshot shared by every shadow light."""
        if brushes:
            brush_positions = np.asarray(
                [b.get('pos', (0.0, 0.0, 0.0)) for b in brushes],
                dtype=np.float64,
            )
            brush_sizes = np.asarray(
                [b.get('size', (64.0, 64.0, 64.0)) for b in brushes],
                dtype=np.float64,
            )
            brush_radii = 0.5 * np.max(brush_sizes, axis=1)
            brush_keys = [
                (
                    tuple(b.get('pos', (0.0, 0.0, 0.0))),
                    tuple(b.get('size', (64.0, 64.0, 64.0))),
                    b.get('_rot_angle'),
                    tuple(b.get('rot_axis')) if b.get('rot_axis') else None,
                )
                for b in brushes
            ]
        else:
            brush_positions = np.empty((0, 3), dtype=np.float64)
            brush_radii = np.empty((0,), dtype=np.float64)
            brush_keys = []

        if models:
            model_positions = np.asarray(
                [tuple(t.pos) for t in models],
                dtype=np.float64,
            )
            model_keys = []
            for t in models:
                props = t.properties
                scale = props.get('scale', 1.0)
                scale_key = scale if isinstance(scale, (int, float)) else tuple(scale)
                model_keys.append(
                    (
                        tuple(t.pos),
                        props.get('model_path'),
                        tuple(props.get('rotation', (0, 0, 0))),
                        scale_key,
                    )
                )
        else:
            model_positions = np.empty((0, 3), dtype=np.float64)
            model_keys = []

        return (
            brush_positions, brush_radii, brush_keys,
            model_positions, model_keys,
        )

    def _collect_shadow_casters(self, brushes, models, lx, ly, lz, reach,
                                batch=None):
        """Return casters within reach, using one NumPy distance pass per light."""
        if batch is not None:
            (brush_positions, brush_radii, brush_keys,
             model_positions, model_keys) = batch

            in_brushes = []
            if len(brushes):
                dx = brush_positions[:, 0] - lx
                dy = brush_positions[:, 1] - ly
                dz = brush_positions[:, 2] - lz
                limit = reach + brush_radii
                visible = (dx * dx + dy * dy + dz * dz) <= (limit * limit)
                indices = np.flatnonzero(visible)
                in_brushes = [brushes[int(i)] for i in indices]
                bkeys = tuple(brush_keys[int(i)] for i in indices)
            else:
                bkeys = ()

            in_models = []
            if len(models):
                dx = model_positions[:, 0] - lx
                dy = model_positions[:, 1] - ly
                dz = model_positions[:, 2] - lz
                visible = (dx * dx + dy * dy + dz * dz) <= (reach * reach * 4.0)
                indices = np.flatnonzero(visible)
                in_models = [models[int(i)] for i in indices]
                mkeys = tuple(model_keys[int(i)] for i in indices)
            else:
                mkeys = ()

            return in_brushes, in_models, (bkeys, mkeys)

        # Legacy/API-compatible scalar path for callers that don't provide the
        # shared batch snapshot.
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
            bkeys.append((pos[0], pos[1], pos[2], size[0], size[1], size[2],
                          b.get('_rot_angle'),
                          tuple(b.get('rot_axis')) if b.get('rot_axis') else None))

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

        # Build numeric caster positions once; every dirty light reuses them.
        caster_batch = self._prepare_shadow_caster_batch(
            caster_brushes, caster_models)

        to_render = []   # (light, slot, in_brushes, in_models)
        for l in lights:
            slot = light_slot.get(id(l))
            if slot is None:
                continue
            lx, ly, lz = float(l.pos[0]), float(l.pos[1]), float(l.pos[2])
            radius = max(float(l.get_radius()), 1.0)
            in_brushes, in_models, caster_keys = self._collect_shadow_casters(
                caster_brushes, caster_models, lx, ly, lz, radius,
                batch=caster_batch)
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

        # Normalise separators from MTL files authored on another platform.
        texture_name = (
            str(texture_name)
            .strip()
            .strip('"')
            .replace('\\', os.sep)
            .replace('/', os.sep)
        )

        # 1. Try relative to the MTL file's directory (most correct for MTL refs)
        mtl_dir = material.get('mtl_dir', '')
        if mtl_dir:
            mtl_dir = (
                str(mtl_dir)
                .replace('\\', os.sep)
                .replace('/', os.sep)
            )
            resolved = os.path.normpath(os.path.join(mtl_dir, texture_name))
            if os.path.exists(resolved):
                return resolved

        # 2. Try assets/textures/ (global fallback)
        resolved = os.path.normpath(os.path.join('assets', 'textures', texture_name))
        if os.path.exists(resolved):
            return resolved

        # 3. Try assets/models/ (legacy fallback)
        resolved = os.path.normpath(os.path.join('assets', 'models', texture_name))
        if os.path.exists(resolved):
            return resolved

        # 4. Return as-is and let the loader handle errors
        return texture_name

    # --------------------------------------------------------------------------
    # Editor helpers (outlines, gizmo, etc.)
    # --------------------------------------------------------------------------
    def _set_line_width(self, width):
        """Set the line width, clamped to what this driver actually supports.

        A core profile only has to support a width of 1.0, and plenty of
        hardware reports exactly ``[1, 1]`` for ``GL_ALIASED_LINE_WIDTH_RANGE``
        — asking for 2.0 there raises ``GL_INVALID_VALUE`` and takes the frame
        with it.  The range is queried once and cached, since ``glGetFloatv``
        stalls the pipeline and the limit never changes for a context.

        Returns the width actually set, so callers can tell when they did not
        get the emphasis they asked for.
        """
        if self._line_width_range is None:
            try:
                values = (gl.GLfloat * 2)()
                gl.glGetFloatv(gl.GL_ALIASED_LINE_WIDTH_RANGE, values)
                low, high = float(values[0]), float(values[1])
                if not (high >= low > 0.0):
                    low = high = 1.0
            except Exception:
                low = high = 1.0
            self._line_width_range = (low, high)
        low, high = self._line_width_range
        clamped = max(low, min(high, float(width)))
        try:
            gl.glLineWidth(clamped)
        except Exception:
            # A driver that refuses even the clamped value: keep drawing at
            # whatever width it is already using rather than losing the frame.
            return 1.0
        return clamped

    def _set_point_size(self, size):
        """Set the point size, clamped to the driver's supported range.

        Same story as :meth:`_set_line_width`: the guaranteed range is narrow
        and an out-of-range value is a GL error, not a silent clamp.
        """
        if self._point_size_range is None:
            try:
                values = (gl.GLfloat * 2)()
                gl.glGetFloatv(gl.GL_ALIASED_POINT_SIZE_RANGE, values)
                low, high = float(values[0]), float(values[1])
                if not (high >= low > 0.0):
                    low = high = 1.0
            except Exception:
                low = high = 1.0
            self._point_size_range = (low, high)
        low, high = self._point_size_range
        clamped = max(low, min(high, float(size)))
        try:
            gl.glPointSize(clamped)
        except Exception:
            return 1.0
        return clamped

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

    def draw_aabb_bounds(self, projection, view, brush):
        """Draw the exact world-space trigger AABB as orange dashed lines."""
        if 'simple' not in self.shaders:
            return
        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))

        lo_x, lo_y, lo_z, hi_x, hi_y, hi_z = brush_aabb_bounds(brush)
        corners = np.array([
            [lo_x, lo_y, lo_z], [hi_x, lo_y, lo_z],
            [hi_x, hi_y, lo_z], [lo_x, hi_y, lo_z],
            [lo_x, lo_y, hi_z], [hi_x, lo_y, hi_z],
            [hi_x, hi_y, hi_z], [lo_x, hi_y, hi_z],
        ], dtype=np.float32)
        edges = ((0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),(0,4),(1,5),(2,6),(3,7))
        dash, gap = 8.0, 5.0
        vertices = []
        for i0, i1 in edges:
            a, b = corners[i0], corners[i1]
            delta = b - a
            length = float(np.linalg.norm(delta))
            if length <= 1e-6:
                continue
            direction = delta / length
            cursor = 0.0
            while cursor < length:
                end = min(cursor + dash, length)
                p0, p1 = a + direction * cursor, a + direction * end
                vertices.extend((float(p0[0]), float(p0[1]), float(p0[2]),
                                 float(p1[0]), float(p1[1]), float(p1[2])))
                cursor += dash + gap
        if not vertices:
            return
        data = np.asarray(vertices, dtype=np.float32)
        vao = getattr(self, '_aabb_vao', None)
        vbo = getattr(self, '_aabb_vbo', None)
        if vao is None:
            vao = self._aabb_vao = gl.glGenVertexArrays(1)
            vbo = self._aabb_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
            gl.glEnableVertexAttribArray(0)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            gl.glBindVertexArray(0)
        gl.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data, gl.GL_DYNAMIC_DRAW)
        gl.glUniform3f(uniforms['color'], 1.0, 140.0 / 255.0, 0.0)
        gl.glUniform1f(uniforms['alpha'], 1.0)
        self._set_line_width(1.0)
        gl.glDrawArrays(gl.GL_LINES, 0, len(vertices) // 3)
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

    def draw_component_overlay(self, projection, view, overlay, version=None):
        """Draw vertex/edge/face handles for the brushes being component-edited.

        ``overlay`` is the dict the editor's ComponentController hands over:
        ``points`` / ``hot_points`` (N, 3) and ``lines`` / ``hot_lines``
        (M, 2, 3), already ``float32``.  Nothing is computed here — the arrays
        are built by the editor when the selection or geometry changes and are
        uploaded again only when ``version`` moves, so hovering costs one
        integer comparison and a draw call rather than a geometry rebuild.
        """
        if 'simple' not in self.shaders or not overlay:
            return
        points = overlay.get('points')
        hot_points = overlay.get('hot_points')
        lines = overlay.get('lines')
        hot_lines = overlay.get('hot_lines')
        if not (len(points) or len(hot_points) or len(lines) or len(hot_lines)):
            return

        if version is None or self._component_overlay_version != version:
            # One interleaved buffer for the whole overlay: four contiguous
            # runs (cold lines, hot lines, cold points, hot points) so the
            # draw below is four glDrawArrays with no per-handle work.
            def _flat(arr):
                return (np.asarray(arr, dtype=np.float32).reshape(-1)
                        if len(arr) else np.zeros(0, dtype=np.float32))
            cold_l, hot_l = _flat(lines), _flat(hot_lines)
            cold_p, hot_p = _flat(points), _flat(hot_points)
            data = np.concatenate((cold_l, hot_l, cold_p, hot_p)) \
                if (len(cold_l) or len(hot_l) or len(cold_p) or len(hot_p)) \
                else np.zeros(0, dtype=np.float32)
            self._component_overlay_data = data
            self._component_overlay_counts = (
                len(cold_l) // 3, len(hot_l) // 3,
                len(cold_p) // 3, len(hot_p) // 3)
            self._component_overlay_version = version
            self._component_overlay_dirty = True

        counts = self._component_overlay_counts
        if not counts or not any(counts):
            return

        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniformMatrix4fv(uniforms['model'], 1, gl.GL_FALSE, glm.value_ptr(self._identity_mat4))
        gl.glUniform1f(uniforms['alpha'], 1.0)

        if self._component_overlay_vao is None:
            self._component_overlay_vao = gl.glGenVertexArrays(1)
            self._component_overlay_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(self._component_overlay_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._component_overlay_vbo)
            gl.glVertexAttribPointer(0, 3, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
            gl.glEnableVertexAttribArray(0)
            gl.glBindVertexArray(0)

        gl.glBindVertexArray(self._component_overlay_vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._component_overlay_vbo)
        if self._component_overlay_dirty:
            data = self._component_overlay_data
            gl.glBufferData(gl.GL_ARRAY_BUFFER, data.nbytes, data,
                            gl.GL_DYNAMIC_DRAW)
            self._component_overlay_dirty = False

        cold_lines, hot_lines_n, cold_points, hot_points_n = counts
        # Handles are editor furniture: they must stay visible through the
        # geometry they belong to, so the depth test is off for this pass.
        depth_was_on = gl.glIsEnabled(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_DEPTH_TEST)
        offset = 0
        if cold_lines:
            gl.glUniform3f(uniforms['color'], 0.45, 0.78, 1.0)
            self._set_line_width(1.0)
            gl.glDrawArrays(gl.GL_LINES, offset, cold_lines)
        offset += cold_lines
        if hot_lines_n:
            gl.glUniform3f(uniforms['color'], 1.0, 0.66, 0.16)
            # Hardware that cannot draw a wide line reports a range of [1, 1],
            # and the highlight then reads by colour alone — which is why the
            # hot and cold colours are far apart rather than two shades of one.
            self._set_line_width(2.0)
            gl.glDrawArrays(gl.GL_LINES, offset, hot_lines_n)
        offset += hot_lines_n
        if cold_points:
            gl.glUniform3f(uniforms['color'], 0.45, 0.78, 1.0)
            self._set_point_size(6.0)
            gl.glDrawArrays(gl.GL_POINTS, offset, cold_points)
        offset += cold_points
        if hot_points_n:
            gl.glUniform3f(uniforms['color'], 1.0, 0.66, 0.16)
            self._set_point_size(9.0)
            gl.glDrawArrays(gl.GL_POINTS, offset, hot_points_n)
        self._set_line_width(1.0)
        self._set_point_size(1.0)
        if depth_was_on:
            gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glBindVertexArray(0)
        gl.glUseProgram(0)

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
        
        # Clamp the line width to what the driver supports (a core profile is
        # only required to offer 1.0).  The helper caches the queried range,
        # so this no longer stalls the pipeline with a glGetFloatv per call.
        self._set_line_width(2.0)
        
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
        if mesh is not None and mesh.key == key:            mesh.frame = self._geo_mesh_frame
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
        axis.  Matches the cube VAO's orientation (v runs up walls).

        The rule itself lives in brush_geometry so the geometry layer can
        materialise the same basis when it locks a face's texture to a
        rotation; this stays as the renderer's name for it.
        """
        return brush_geometry.render_uv_axes(n)

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
            # Projection along the face's own texture basis when it has one
            # (a rotated face carries a basis that turned with the brush), and
            # along world axes when it does not — exactly as before.
            us, vs, (u0, eu), (v0, ev) = brush_geometry.face_uv_projection(
                ring_w, face)
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

    def _geo_run_tex_transform(self, brush, run):
        """``(angle_radians, shift_u, shift_v)`` for one angled-brush face.

        Same precedence as :meth:`_geo_run_tex_scale`: a tagged side reads the
        brush's per-tag dicts, a cut face reads its own plane live so Surface
        Inspector edits show up without rebuilding the mesh.  Angled faces used
        to have both of these forced to zero, which is why rotating or shifting
        a texture did nothing once a brush stopped being a box.
        """
        tag = run['face']
        if tag:
            angle = brush.get('uv_angle', {}).get(tag, 0.0)
            shift = brush.get('uv_shift', {}).get(tag, (0.0, 0.0))
        else:
            plane = BaseRenderer._geo_run_plane(brush, run)
            if plane is None:
                return 0.0, 0.0, 0.0
            angle = plane.get('uv_angle', 0.0)
            shift = plane.get('uv_shift', (0.0, 0.0))
        return math.radians(float(angle)), float(shift[0]), float(shift[1])

    def _geo_run_tex_scale(self, brush, run, tex_name):
        """UV repeat factors for one face, mirroring the box-face priorities:
        live per-face uv_scale, then the plane's stored uv_scale, then
        texture_tiling (1px = 1 world unit over the face's extent), then FIT."""
        tag = run['face']
        plane = None if tag else BaseRenderer._geo_run_plane(brush, run)

        # Natural is a live mode: the repeats come from the face's *current*
        # extent every frame, so resizing the brush shows more of the texture
        # at the same texel size rather than stretching it.  It therefore wins
        # over any stored uv_scale (which is only kept as a fallback).
        if brush_geometry.face_uses_natural_scale(brush, tag, plane):
            return brush_geometry.natural_repeats(
                run['extent'][0], run['extent'][1],
                self._texture_pixel_size(tex_name))

        uv = brush.get('uv_scale', {}).get(tag) if tag else None
        if uv is None and plane is not None:
            # Cut face: read its plane's uv_scale live so Surface Inspector
            # edits apply without a mesh rebuild.
            uv = plane.get('uv_scale')
        if uv is None:
            uv = run['uv_scale']
        if uv is not None:
            return float(uv[0]), float(uv[1])
        if brush.get('texture_tiling', False):
            eu, ev = run['extent']
            return brush_geometry.natural_repeats(
                eu, ev, self._texture_pixel_size(tex_name))
        return 1.0, 1.0

    def _texture_pixel_size(self, tex_name):
        """Pixel dimensions of a loaded texture, with the usual 128 fallback."""
        cache_name = os.path.join('textures', tex_name)
        return getattr(self, '_texture_dimensions', {}).get(cache_name, (128, 128))

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
        # Shadow resources. These are owned by this renderer alone and nothing
        # outside it holds their names, so they have to be released here or a
        # renderer rebuild (a render-mode or shadow-quality change) strands the
        # whole cube-map pool -- MAX_SHADOW_LIGHTS cube-maps at shadow_map_size,
        # tens of megabytes of VRAM, every time.
        if self._shadow_cubemaps:
            try:
                gl.glDeleteTextures(self._shadow_cubemaps)
            except Exception:
                pass
            self._shadow_cubemaps = []
        if self._shadow_fbo:
            try:
                gl.glDeleteFramebuffers(1, [self._shadow_fbo])
            except Exception:
                pass
            self._shadow_fbo = None
        self._shadow_slot_owner = [None] * self.MAX_SHADOW_LIGHTS
        self._shadow_slot_sig = [None] * self.MAX_SHADOW_LIGHTS
        self._light_shadow_index = {}
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
        if self._model_instance_vbo:
            gl.glDeleteBuffers(1, [self._model_instance_vbo])
            self._model_instance_vbo = None
            self._model_instance_capacity = 0
            self._model_instanced_vaos.clear()
        if self._light_ubo:
            gl.glDeleteBuffers(1, [self._light_ubo])
            self._light_ubo = None
            self._light_ubo_capacity = 0
            self._light_ubo_key = None
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