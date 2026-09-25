"""
engine/renderer_core.py  –  Base renderer with shared logic for Forward/Deferred

Provides:
    • Texture management (load_texture, preload_level_textures)
    • Grid drawing (update_grid_buffers, draw_grid)
    • Sprite rendering (dense instanced EntityTable path)
    • Model loading & dense instanced drawing
    • Water / Glass / Fog volume rendering
    • Terrain rendering
    • Editor helpers (gizmo, selection outline, face highlight, connection lines,
      path node cubes, portal wireframes)
    • Projected shadows
    • Dense slot classification / sorting helpers
    • VAO creation for cube, sprite, grid, gizmo, etc.
    • Shader management (compilation, hot‑reload, light upload)

Both Renderer_F and Renderer_D inherit from BaseRenderer.
"""

import ctypes
import re
import math
import os
from dataclasses import dataclass

import glm
import numpy as np
import OpenGL.GL as gl
from OpenGL.GL.shaders import compileProgram, compileShader

from engine.constants import (is_water_brush, brush_aabb_bounds,
                              normalize_color)
from engine import brush_geometry
from engine import render_table
from engine import entity_table as entity_projection
from engine.render_keys import KeyLayout, sort_into_runs
from engine import shaders

_BASE_RENDERER_PREFIX = "\x1b[38;2;240;128;0m[BaseRenderer]\x1b[0m"
from engine.shaders import DEFAULT_SHADERS
from engine.terrain import TERRAIN_VERTEX_SHADER, TERRAIN_FRAGMENT_SHADER
from engine.view_distance import ViewDistance
from engine.portal_transform import (
    map_point as _portal_map_point,
    map_direction as _portal_map_direction,
    corners as _portal_corners,
    contains_point as _portal_contains_point,
)
from editor.things import (
    Thing, PathNode, Pickup, Prop, Monster, LogicGate, LogicRelay,
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


@dataclass(frozen=True)
class RenderView:
    """Camera state for a secondary render view.

    The main camera can use the same shape later; portals already use it so
    aperture/clip/recursion state is carried alongside the camera instead of
    being implicit renderer globals.
    """
    projection: object
    view: object
    camera_pos: object
    aperture_slot: int = -1
    clip_slot: int = -1
    recursion_depth: int = 0

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
#: Re-exported so ``from engine.renderer_core import normalize_color`` keeps
#: working; it lives in engine.constants because the GL-free render projection
#: needs it too.  See :func:`engine.constants.normalize_color`.
normalize_color = normalize_color


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
    # CPU mirror of the std140 GLSL `struct Light` (shaders.py): four 16-byte
    # fields per light, 64 bytes total. One upload feeds every lit shader.
    LIGHT_UBO_DTYPE = np.dtype([
        ('position', '<f4', (4,)),
        ('color', '<f4', (4,)),
        ('params', '<f4', (4,)),
        ('indices', '<i4', (4,)),
    ])
    MAX_PORTALS = 4      # maximum portal apertures rendered per frame
    PORTAL_RENDER_DISTANCE = 2048.0
    # Render-only inset for the portal aperture.  Physical portal size and
    # transit geometry remain authored; this keeps coplanar floor/wall edges
    # from bleeding into the portal image at its boundary.
    PORTAL_APERTURE_INSET = 4.0

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
    WATER_REFLECTION_SIZE = 256
    WATER_REFLECTION_PROBE_HEIGHT = 0.5
    WATER_REFLECTION_TEXTURE_UNIT = 3

    #: Uniform names of the shared distance-fog / global-ambient block
    #: (engine.shaders.FOG_GLSL). Preloaded for every shader that splices it in,
    #: and uploaded together by :meth:`_upload_env_uniforms`.
    ENV_UNIFORMS = ('uFogEnabled', 'uFogColor', 'uFogStart', 'uFogEnd',
                    'uFogDensity', 'uFogCamPos', 'uAmbient')

    def __init__(self, texture_loader, initial_grid_size, initial_world_size, config=None):
        self.texture_manager = {}
        self.loaded_models = {}

        # Glass samples the already-rendered scene for screen-space transmission.
        # Kept lazy because most frames contain no glass at all.
        self._glass_scene_texture = 0
        self._glass_scene_size = (0, 0)
        self._glass_scene_texture_unit = 2

        # Water reflections are fully lazy. The checkbox creates one
        # 256x256 RGBA cubemap per reflected water slot, plus one shared
        # 256x256 depth target used while filling all six faces.
        self._water_reflection_fbo = None
        self._water_reflection_depth = None
        self._water_reflection_cubemaps = {}

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
        # The shared brush-instance buffer and its VAO. One layout serves every
        # pass that submits runs (see BRUSH_INSTANCE_ATTRS), so it lives here
        # rather than on the forward renderer.
        self._brush_instance_vbo = None
        self._brush_instance_vao = None
        self._sprite_instance_vbo = None
        self._sprite_instance_vao = None
        self._sprite_instance_capacity = 0
        self._sprite_gl_by_id = np.zeros(0, dtype=np.int32)
        self._sprite_gl_resolved = 0
        self._sprite_instance_base = 0
        self._sprite_recipes_seen = None
        self._sprite_instance_data = np.empty(
            (0, 5), dtype=np.float32)
        # Capacity-stable scratch for the numeric sprite filter. The renderer
        # owns these arrays so steady-state drawing does not allocate key/mask/
        # texture arrays per frame.
        self._sprite_key_scratch = np.empty(0, dtype=np.int32)
        self._sprite_texture_scratch = np.empty(0, dtype=np.int32)
        self._sprite_draw_mask = np.empty(0, dtype=bool)
        self._sprite_depth_scratch = np.empty(0, dtype=np.float64)
        self._sprite_depth_aux_scratch = np.empty(0, dtype=np.float64)
        self._sprite_sorted_slots_scratch = np.empty(0, dtype=np.int32)
        self._brush_instance_capacity = 0
        self._brush_instance_data = np.empty((0, 32), dtype=np.float32)
        # Reusable model/normal matrix buffers for the batched transform build.
        self._brush_mat_buf = np.empty((0, 16), dtype=np.float32)
        self._brush_nmat_buf = np.empty((0, 9), dtype=np.float32)
        self._model_instance_vbo = None
        self._model_instance_capacity = 0
        self._model_instance_data = np.empty((0, 28), dtype=np.float32)
        self._model_instanced_vaos = set()
        self._model_recipe_scratch = np.empty(0, dtype=np.int32)
        self._model_sorted_slots_scratch = np.empty(0, dtype=np.int32)

        # Shared std140 light UBO. One upload feeds every lighting shader.
        self._light_ubo = None
        self._light_ubo_capacity = 0
        self._light_ubo_key = None
        self._light_ubo_dtype = self.LIGHT_UBO_DTYPE
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

        # Per‑frame caches
        self._frame_lights = []        # shader_name -> tuple of light ids uploaded this frame; cleared at
        # the start of every render_scene() so animated lights stay fresh.
        self._frame_lights_uploaded = {}
        self._current_shader = None

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

        # Convex geometry meshes, keyed by (RenderTable generation, geometry_id).
        # Entries are rebuilt when the dense geometry signature changes and
        # dropped after going unused for a while (see _begin_geo_frame).
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
                                            'distortionStrength', 'fresnelIntensity', 'glassOpacity',
                                            'refractionIndex', 'roughness', 'normalMatrix',
                                            'sceneColor', 'screenSize'])
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
        self._compile_instanced_brush_shader(tex_vert, tex_frag)
        self._compile_instanced_lit_brush_shader(lit_vert, lit_frag)
        self._compile_instanced_depth_shader()
        self._compile_instanced_sprite_shader()

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
        self._compile_instanced_brush_shader(tex_vert, tex_frag)
        self._compile_instanced_lit_brush_shader(lit_vert, lit_frag)
        self._compile_instanced_depth_shader()
        self._compile_instanced_sprite_shader()

    #: Floats per brush-face instance: a mat4 model matrix, a mat3 normal
    #: matrix padded to three vec4 (with the face's UV rotation tucked into the
    #: first spare w), and one vec4 of UV scale and shift.  Eight vec4s, so
    #: attribute locations 3..10 -- 0..2 are the cube's own position, normal and
    #: texture coordinate.
    BRUSH_INSTANCE_FLOATS = 32

    #: Attribute locations 3..10 carry one instance of a brush draw run, and
    #: mean the same thing for every pass that uses them:
    #:
    #:   3..6   mat4 model
    #:   7..9   mat3 normal matrix, padded to vec4; ``iNormal0.w`` is a spare
    #:          scalar a pass may use (the textured pass puts the face's UV
    #:          rotation there)
    #:   10     vec4 payload, whose meaning is the pass's own (UV scale and
    #:          shift for textured, colour and alpha for lit)
    #:
    #: One layout, one buffer and one VAO serve both passes, which is what
    #: makes "a run describes the invariant GPU state, the instance array
    #: describes everything that varies within it" a property of the renderer
    #: rather than of one pass.
    BRUSH_INSTANCE_ATTRS = """layout (location = 3) in vec4 iModel0;
layout (location = 4) in vec4 iModel1;
layout (location = 5) in vec4 iModel2;
layout (location = 6) in vec4 iModel3;
layout (location = 7) in vec4 iNormal0;
layout (location = 8) in vec4 iNormal1;
layout (location = 9) in vec4 iNormal2;
layout (location = 10) in vec4 iPayload;

"""

    #: Vertex-shader uniforms the instanced variants drop, because the value is
    #: per instance now rather than per draw.
    _INSTANCED_VERT_DROP = ('uniform mat4 model;', 'uniform mat3 normalMatrix;',
                            'uniform vec2 tex_scale', 'uniform float tex_angle',
                            'uniform vec2 tex_shift')

    def _instanced_vertex_source(self, vert, preamble, extra_out=''):
        """A vertex shader rewritten to take its transform from instance data.

        Shared by the textured and lit brush passes: both start from the
        ordinary shader and differ only in what they pull out of the payload,
        so neither can drift from the pass it accelerates.
        """
        kept = [line for line in vert.splitlines()
                if not line.strip().startswith(self._INSTANCED_VERT_DROP)]
        source = '\n'.join(kept)
        if 'out vec3 FragPos;' not in source or 'void main() {' not in source:
            return None
        source = source.replace(
            'out vec3 FragPos;',
            self.BRUSH_INSTANCE_ATTRS + extra_out + 'out vec3 FragPos;', 1)
        source = source.replace(
            'void main() {',
            'void main() {\n'
            '    mat4 instanceModel = mat4(iModel0, iModel1, iModel2, iModel3);\n'
            '    mat3 instanceNormal = mat3(iNormal0.xyz, iNormal1.xyz, iNormal2.xyz);\n'
            + preamble, 1)
        source = source.replace('model * vec4(aPos, 1.0)',
                                'instanceModel * vec4(aPos, 1.0)')
        source = source.replace('normalMatrix * aNormal',
                                'instanceNormal * aNormal')
        return source

    def _register_instanced_shader(self, name, vertex_source, fragment_source,
                                   extra_uniforms=()):
        """Compile one instanced brush program, or leave it absent.

        Absence is a supported state, not a failure: a driver that rejects the
        attribute interface simply keeps the per-object path, which every pass
        retains.
        """
        if not vertex_source or not fragment_source:
            return False
        try:
            program = self.shader_loader.compile_from_source(vertex_source,
                                                             fragment_source)
        except Exception as exc:
            print(f'[BaseRenderer] {name} instancing disabled: {exc}')
            return False
        self.shaders[name] = program
        self.uniforms[name] = UniformCache(program)
        self._preload_lit_uniforms(name)
        if extra_uniforms:
            self.uniforms[name].preload(list(extra_uniforms))
        return True

    def _compile_instanced_depth_shader(self):
        """Compile the shadow depth shader with an instanced model matrix.

        The depth pass is the purest run/instance split in the renderer: the
        only thing that varies per caster is its model matrix, and the only
        thing that varies per run is the cube face's ``lightSpaceMatrix``. So
        the matrix becomes instance data and the face stays a uniform, and one
        light's six faces cost six draws instead of six times its caster count.

        The vertex shader takes the same instance attributes as the brush
        passes, so the same buffer and the same VAO serve it; the normal and
        payload slots go unused here, which costs a little upload bandwidth and
        buys one layout for the whole renderer.
        """
        vert = DEFAULT_SHADERS.get('depth_cube.vert', '')
        frag = DEFAULT_SHADERS.get('depth_cube.frag', '')
        if not vert or not frag:
            return
        vertex = self._instanced_vertex_source(vert, preamble='')
        if vertex is None:
            return
        if self._register_instanced_shader(
                'depth_cube_instanced', vertex, frag,
                extra_uniforms=['lightSpaceMatrix', 'lightPos', 'far_plane']):
            print(f'{_BASE_RENDERER_PREFIX} Shadow depth instancing shader compiled successfully.')

    #: Per-instance attributes the sprite pass carries: the billboard's centre
    #: and its world size.  Five floats, against the two uniform uploads and
    #: the draw call each sprite used to cost.
    SPRITE_INSTANCE_FLOATS = 5

    def _compile_instanced_sprite_shader(self):
        """Compile the billboard shader with its centre and size per instance.

        The sprite pass was the last one submitting per object: one
        ``glDrawArrays`` and two uniform uploads for every billboard in range,
        where the brush passes had long since collapsed to one draw per state
        run.  Instancing it is the same trade they made -- what cannot vary
        within a draw (the texture) stays a bind, and what does (where the
        billboard is and how big) becomes instance data.

        Derived from ``sprite.vert`` by rewriting the two uniforms into
        attributes rather than written out again, so the billboard's
        camera-facing maths cannot drift from the path it accelerates.
        """
        vert = DEFAULT_SHADERS.get('sprite.vert', '')
        frag = DEFAULT_SHADERS.get('sprite.frag', '')
        if not vert or not frag:
            return
        kept = [line for line in vert.splitlines()
                if not line.strip().startswith(('uniform vec3 sprite_pos_world',
                                                'uniform vec2 sprite_size'))]
        source = '\n'.join(kept)
        if 'out vec2 TexCoords;' not in source:
            return
        source = source.replace(
            'out vec2 TexCoords;',
            'layout (location = 1) in vec3 iSpritePos;\n'
            'layout (location = 2) in vec2 iSpriteSize;\n'
            'out vec2 TexCoords;', 1)
        source = source.replace('sprite_pos_world', 'iSpritePos')
        source = source.replace('sprite_size.x', 'iSpriteSize.x')
        source = source.replace('sprite_size.y', 'iSpriteSize.y')
        if 'iSpritePos' not in source or 'iSpriteSize.x' not in source:
            # The shader did not look the way this rewrite assumes; leaving the
            # program absent keeps the per-sprite path, which every caller has.
            return
        if self._register_instanced_shader('sprite_instanced', source, frag,
                                           extra_uniforms=['projection', 'view',
                                                           'sprite_texture']):
            print(f'{_BASE_RENDERER_PREFIX} Sprite instancing shader compiled successfully.')

    def _ensure_sprite_instance_buffer(self, count):
        """Grow the sprite instance VBO and its staging array to *count* rows."""
        if self._sprite_instance_vbo is None:
            self._sprite_instance_vbo = gl.glGenBuffers(1)
        if count <= self._sprite_instance_capacity:
            return
        capacity = max(count, 256, self._sprite_instance_capacity * 2)
        self._sprite_instance_capacity = capacity
        self._sprite_instance_data = np.empty(
            (capacity, self.SPRITE_INSTANCE_FLOATS), dtype=np.float32)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._sprite_instance_vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, self._sprite_instance_data.nbytes,
                        None, gl.GL_DYNAMIC_DRAW)
        # As with the brush buffer, the VAO is left alone: glBufferData keeps
        # the buffer's name and the VAO's pointers reference the name.

    def _ensure_sprite_instance_vao(self):
        """A VAO over the shared billboard quad plus the instance buffer.

        Separate from ``vaos['sprite']`` for the reason the brush pass keeps
        its own: the per-sprite path shares that one, and giving it two enabled
        divisor-1 attributes would have every ordinary billboard draw read an
        instance buffer it does not use.
        """
        if self._sprite_instance_vao is not None:
            return self._sprite_instance_vao
        self._ensure_sprite_instance_buffer(1)
        vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._sprite_vbo)
        gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._sprite_instance_vbo)
        stride = self.SPRITE_INSTANCE_FLOATS * 4
        for location, size, offset in ((1, 3, 0), (2, 2, 12)):
            gl.glVertexAttribPointer(location, size, gl.GL_FLOAT, gl.GL_FALSE,
                                     stride, ctypes.c_void_p(offset))
            gl.glEnableVertexAttribArray(location)
            gl.glVertexAttribDivisor(location, 1)
        gl.glBindVertexArray(0)
        self._sprite_instance_vao = vao
        return vao

    def _point_sprite_instances_at(self, base):
        """Re-aim the sprite instance attributes at instance *base*.

        OpenGL 3.3 has no ``glDrawArraysInstancedBaseInstance``, so a run that
        starts part way through the buffer is reached by moving the pointers --
        the same two calls per run the brush pass makes eight of.
        """
        stride = self.SPRITE_INSTANCE_FLOATS * 4
        base = int(base)
        #: Which instance the attributes currently point at. Read by the
        #: submission tests to recover what a run actually drew.
        self._sprite_instance_base = base
        origin = base * stride
        for location, size, offset in ((1, 3, 0), (2, 2, 12)):
            gl.glVertexAttribPointer(location, size, gl.GL_FLOAT, gl.GL_FALSE,
                                     stride, ctypes.c_void_p(origin + offset))

    def _compile_instanced_lit_brush_shader(self, lit_vert, lit_frag):
        """Compile the flat-shaded brush shader with instanced colour.

        The lit pass had no texture to batch by, so every brush was its own
        draw carrying four uniform uploads -- model, normal, colour, alpha.
        Colour and alpha are read in the *fragment* stage, so instancing them
        means carrying them across as a varying; the rest of the lighting,
        shadowing and fog code is untouched.
        """
        if not lit_vert or not lit_frag:
            return
        vertex = self._instanced_vertex_source(
            lit_vert,
            preamble='    vInstanceColor = iPayload;\n',
            extra_out='out vec4 vInstanceColor;\n')
        if vertex is None:
            return

        kept = [line for line in lit_frag.splitlines()
                if not line.strip().startswith(('uniform vec3 object_color;',
                                                'uniform float alpha;'))]
        fragment = '\n'.join(kept)
        if 'in vec3 Normal;' not in fragment:
            return
        fragment = fragment.replace('in vec3 Normal;',
                                    'in vec3 Normal;\nin vec4 vInstanceColor;', 1)
        fragment, colours = re.subn(r'\bobject_color\b', 'vInstanceColor.rgb',
                                    fragment)
        fragment, alphas = re.subn(r'\balpha\b', 'vInstanceColor.a', fragment)
        if not colours or not alphas:
            # The shader did not look the way this rewrite assumes; leaving the
            # program absent keeps the per-brush path rather than compiling
            # something subtly wrong.
            return
        if self._register_instanced_shader('lit_brush_instanced', vertex,
                                           fragment):
            print(f'{_BASE_RENDERER_PREFIX} Lit brush instancing shader compiled successfully.')

    def _frame_transforms(self, table, slots):
        """Model and normal matrices for *slots*, into reusable buffers.

        One batched build per pass instead of one memoised glm matrix per brush
        object.  The buffers are grown geometrically and never shrunk, so a
        steady-state frame allocates nothing.
        """
        count = len(slots)
        if len(self._brush_mat_buf) < count:
            capacity = max(count, 16, len(self._brush_mat_buf) * 2)
            self._brush_mat_buf = np.empty((capacity, 16), dtype=np.float32)
            self._brush_nmat_buf = np.empty((capacity, 9), dtype=np.float32)
        return render_table.model_matrices(
            table, slots, self._brush_mat_buf, self._brush_nmat_buf)

    

    def _ensure_brush_instance_buffer(self, count):
        """Grow the per-face instance VBO and its staging array to *count* rows."""
        if self._brush_instance_vbo is None:
            self._brush_instance_vbo = gl.glGenBuffers(1)
        if count <= self._brush_instance_capacity:
            return
        capacity = max(count, 256, self._brush_instance_capacity * 2)
        self._brush_instance_capacity = capacity
        self._brush_instance_data = np.empty(
            (capacity, self.BRUSH_INSTANCE_FLOATS), dtype=np.float32)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._brush_instance_vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, self._brush_instance_data.nbytes,
                        None, gl.GL_DYNAMIC_DRAW)
        # The VAO is deliberately left alone. glBufferData reallocates the data
        # store but keeps the buffer's name, and a VAO's attribute pointers
        # reference the name with a stride and offset that have not changed --
        # so the VAO stays valid across a growth. Deleting it here would also
        # invalidate any handle a caller is holding, which the shadow pass does
        # across its six faces.

    def _ensure_brush_instance_vao(self):
        """A VAO over the shared cube VBO plus the per-face instance buffer.

        Deliberately separate from ``vaos['cube']``: the non-instanced path
        shares that one, and giving it eight enabled divisor-1 attributes would
        have every ordinary cube draw read an instance buffer it does not use.
        """
        if self._brush_instance_vao is not None:
            return self._brush_instance_vao
        # The shadow pass asks for the VAO during its setup, before anything
        # has packed instances, so the buffer may not exist yet.
        self._ensure_brush_instance_buffer(1)
        vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._cube_vbo)
        for location, size, offset in ((0, 3, 0), (1, 3, 12), (2, 2, 24)):
            gl.glVertexAttribPointer(location, size, gl.GL_FLOAT, gl.GL_FALSE,
                                     32, ctypes.c_void_p(offset))
            gl.glEnableVertexAttribArray(location)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._brush_instance_vbo)
        stride = self.BRUSH_INSTANCE_FLOATS * 4
        for location in range(3, 11):
            gl.glVertexAttribPointer(
                location, 4, gl.GL_FLOAT, gl.GL_FALSE, stride,
                ctypes.c_void_p((location - 3) * 16))
            gl.glEnableVertexAttribArray(location)
            gl.glVertexAttribDivisor(location, 1)
        gl.glBindVertexArray(0)
        self._brush_instance_vao = vao
        return vao

    def _point_brush_instances_at(self, base):
        """Re-aim the instance attributes at instance *base*.

        OpenGL 3.3 has no ``glDrawArraysInstancedBaseInstance``, so a run that
        starts part way through the buffer is reached by moving the attribute
        pointers instead. Eight calls per run, against six vertices' worth of
        draw -- and there are at most six runs per texture.
        """
        stride = self.BRUSH_INSTANCE_FLOATS * 4
        origin = int(base) * stride
        for location in range(3, 11):
            gl.glVertexAttribPointer(
                location, 4, gl.GL_FLOAT, gl.GL_FALSE, stride,
                ctypes.c_void_p(origin + (location - 3) * 16))

    def _pack_brush_instances(self, models, normals, rows, spare, payload):
        """Pack one instance row per draw item, straight from existing arrays.

        The layout is :data:`BaseRenderer.BRUSH_INSTANCE_ATTRS`: the model
        matrix, the normal matrix padded to three vec4 with one spare scalar,
        and a payload vec4 whose meaning belongs to the calling pass.  Every
        field is a vectorised take -- no Python loop, and no going back to an
        object for a transform that already exists as a column.

        *rows* selects which of *models* / *normals* each instance uses, so one
        brush appearing as six faces costs six instance rows and one matrix
        build.
        """
        count = len(rows)
        self._ensure_brush_instance_buffer(count)
        data = self._brush_instance_data[:count]
        np.take(models, rows, axis=0, out=data[:, 0:16])
        if payload is None:
            payload = 0.0
        if normals is None:
            # A pass that writes only depth has no normal to carry; leaving the
            # slots zero keeps one instance layout for the whole renderer at
            # the cost of a little upload bandwidth.
            data[:, 16:28] = 0.0
        else:
            item_normals = normals[rows]
            data[:, 16:19] = item_normals[:, 0:3]
            data[:, 19] = spare
            data[:, 20:23] = item_normals[:, 3:6]
            data[:, 23] = 0.0
            data[:, 24:27] = item_normals[:, 6:9]
            data[:, 27] = 0.0
        data[:, 28:32] = payload
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._brush_instance_vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, data)
        return count

    def _begin_instanced_pass(self, name, projection, view, lights):
        """Bind an instanced brush program and its per-pass uniform state."""
        program = self.shaders[name]
        uniforms = self.uniforms[name]
        gl.glUseProgram(program)
        self._current_shader = program
        self._upload_lights_once(name, lights)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE,
                              glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE,
                              glm.value_ptr(view))
        return uniforms

    def _compile_instanced_brush_shader(self, tex_vert, tex_frag):
        """Compile the textured-brush shader with per-face instanced attributes.

        The per-face state the pass used to upload as uniforms -- model matrix,
        normal matrix, UV scale, rotation and shift -- becomes instance data,
        so every face sharing a texture and a cube face index is one
        ``glDrawArraysInstanced`` instead of one ``glDrawArrays`` and three or
        four ``glUniform`` calls each.

        The UV scale and shift ride in the payload vec4 and the rotation in the
        spare ``iNormal0.w``; declaring locals of the shader's original uniform
        names leaves the UV rotate/scale/shift maths in the body byte for byte
        what the non-instanced path runs.  Both the desktop and ARM variants
        have the same vertex interface, so one derivation serves both.
        """
        vertex = self._instanced_vertex_source(
            tex_vert,
            preamble=('    vec2 tex_scale = iPayload.xy;\n'
                      '    vec2 tex_shift = iPayload.zw;\n'
                      '    float tex_angle = iNormal0.w;\n'))
        if vertex is None:
            return
        if self._register_instanced_shader('brush_instanced', vertex, tex_frag,
                                           extra_uniforms=['texture_diffuse']):
            print(f'{_BASE_RENDERER_PREFIX} Brush face instancing shader compiled successfully.')

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
            print(f'{_BASE_RENDERER_PREFIX} GPU model instancing shaders compiled successfully.')
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
        uniforms.preload([
            'projection', 'view', 'model', 'time', 'viewPos',
            'normalMap', 'sceneColor', 'reflectionCube', 'reflectionEnabled',
            'screenSize', 'waterOpacity', 'waterReflectivity',
            'waterTint', 'distortionStrength', 'refractionIndex',
            'roughness', 'fresnelIntensity', 'normalMatrix',
            'waveAmp', 'brushSize'
        ])
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
        if not (isinstance(lights, tuple) and len(lights) == 2
                and hasattr(lights[0], 'light_color')):
            raise TypeError("terrain rendering requires (LightTable, slots)")
        light_table, light_slots = lights
        max_terrain_lights = shaders.MAX_LIGHTS_TERRAIN
        terrain_lights = (light_table, light_slots)
        if len(light_slots) > max_terrain_lights:
            cx, cy, cz = self._camera_xyz(camera_pos)
            dx = light_table.pos[light_slots, 0] - cx
            dy = light_table.pos[light_slots, 1] - cy
            dz = light_table.pos[light_slots, 2] - cz
            order = np.argsort(dx * dx + dy * dy + dz * dz, kind='stable')
            terrain_lights = (light_table, light_slots[order[:max_terrain_lights]])

        self._current_shader = terrain.shader_program
        self._upload_lights_once('terrain', terrain_lights)
        active_lights_count = len(terrain_lights[1])
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

    def _fill_model_instance_buffer_numeric(self, table, slots):
        """Gather model transforms directly from dense entity columns."""
        count = len(slots)
        self._ensure_model_instance_buffer(count)
        out = self._model_instance_data[:count]
        np.take(table.model_base_matrix, slots, axis=0, out=out[:, :16])
        np.take(table.model_normal_matrix, slots, axis=0, out=out[:, 16:28])
        np.take(table.pos[:, 0], slots, out=out[:, 12])
        np.take(table.pos[:, 1], slots, out=out[:, 13])
        np.take(table.pos[:, 2], slots, out=out[:, 14])
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._model_instance_vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, out)


    def _draw_dense_model_single(self, projection, view, table, slot, lights):
        """Draw one dense model row through the proven uniform model path.

        A single model does not benefit from instancing. More importantly, this
        keeps the one-model case independent of instanced-attribute driver
        quirks while preserving the dense EntityTable boundary: no Thing is
        materialised just to submit the draw.
        """
        slot = int(slot)
        recipe_id = int(table.model_recipe_id[slot])
        recipes = table.model_recipes()
        if recipe_id < 0 or recipe_id >= len(recipes):
            return 0

        model_path, manual_texture, override_color = recipes[recipe_id]
        obj = self.load_model(model_path)
        if not obj or not obj.is_loaded:
            return 0

        groups = obj.groups or []
        if manual_texture:
            shader_kind = (
                'textured' if self.shaders.get('textured') else
                'lit' if self.shaders.get('lit') else None)
            if shader_kind is None:
                return 0
            shader_name = shader_kind
            current_shader = self._prepare_model_shader(
                shader_name, projection, view, lights, None)
            if current_shader != shader_name:
                return 0
            u = self.uniforms[shader_name]
            if shader_kind == 'textured':
                gl.glBindTexture(
                    gl.GL_TEXTURE_2D,
                    self._model_texture_id(manual_texture, manual=True))
            else:
                gl.glUniform3fv(
                    u['object_color'], 1, override_color or (0.8, 0.8, 0.8))
                gl.glUniform1f(u['alpha'], 1.0)
            groups_to_draw = (None,)
        elif groups:
            groups_to_draw = groups
        else:
            groups_to_draw = (None,)

        base = np.asarray(table.model_base_matrix[slot], dtype=np.float32).copy()
        base[12:15] = np.asarray(table.pos[slot], dtype=np.float32)
        normal = np.asarray(
            table.model_normal_matrix[slot].reshape(3, 4)[:, :3],
            dtype=np.float32,
        ).reshape(-1).copy()

        current_shader = None
        draws = 0
        for group in groups_to_draw:
            material = (
                obj.materials.get(
                    group['material'],
                    {'color': [0.8, 0.8, 0.8], 'texture': None})
                if group is not None else
                {'color': override_color or [0.8, 0.8, 0.8],
                 'texture': manual_texture}
            )
            use_texture = material.get('texture')
            shader_kind = (
                'textured' if use_texture and self.shaders.get('textured') else
                'lit' if self.shaders.get('lit') else None)
            if shader_kind is None:
                continue

            shader_name = shader_kind
            current_shader = self._prepare_model_shader(
                shader_name, projection, view, lights, current_shader)
            if current_shader != shader_name:
                continue

            u = self.uniforms[shader_name]
            if shader_kind == 'textured':
                gl.glBindTexture(
                    gl.GL_TEXTURE_2D,
                    self._model_texture_id(
                        use_texture, material, manual=manual_texture is not None))
            else:
                color = tuple(material.get('color', [0.8, 0.8, 0.8]))
                gl.glUniform3fv(u['object_color'], 1, color)
                gl.glUniform1f(u['alpha'], 1.0)

            gl.glUniformMatrix4fv(
                u['model'], 1, gl.GL_FALSE, base)
            normal_loc = u.get('normalMatrix', -1)
            if normal_loc >= 0:
                gl.glUniformMatrix3fv(
                    normal_loc, 1, gl.GL_FALSE, normal)

            gl.glBindVertexArray(obj.vao)
            if group is None:
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, obj.vertex_count)
            elif group.get('indexed', False) and getattr(obj, 'ebo', None) is not None:
                gl.glDrawElements(
                    gl.GL_TRIANGLES, group['count'], gl.GL_UNSIGNED_INT,
                    ctypes.c_void_p(group['start'] * 4))
            else:
                gl.glDrawArrays(
                    gl.GL_TRIANGLES, group['start'], group['count'])
            self.render_stats.draw_calls += 1
            draws += 1

        gl.glBindVertexArray(0)
        return draws


    def draw_models_instanced(self, projection, view, camera_pos, table, slots,
                              lights, config=None):
        """Render model instances from dense EntityTable columns.

        Entity objects are not touched here. Model resources are resolved once
        per distinct cold recipe; instance transforms and visibility remain
        numeric all the way to the reusable GPU staging buffer.
        """
        if not len(slots):
            return 0
        if not (self.shaders.get('lit_instanced') or self.shaders.get('textured_instanced')):
            return 0

        # Model rendering has always been double-sided at the renderer level.
        # Brush passes are free to leave face culling enabled for their own
        # geometry, but OBJ winding is authored per asset and must not make a
        # valid model vanish in the entity pass.
        cull_was_enabled = gl.glIsEnabled(gl.GL_CULL_FACE)
        gl.glDisable(gl.GL_CULL_FACE)

        count = len(slots)
        if count == 1:
            # Keep the singleton path numeric but use the already-proven uniform
            # model submission instead of depending on instanced vertex
            # attributes for a draw that gains nothing from instancing.
            drawn = 1 if self._draw_dense_model_single(
                projection, view, table, slots[0], lights) else 0
            if cull_was_enabled:
                gl.glEnable(gl.GL_CULL_FACE)
            else:
                gl.glDisable(gl.GL_CULL_FACE)
            return drawn
        if len(self._model_recipe_scratch) < count:
            grown = max(64, len(self._model_recipe_scratch) * 2, count)
            self._model_recipe_scratch = np.empty(grown, dtype=np.int32)
            self._model_sorted_slots_scratch = np.empty(grown, dtype=np.int32)

        recipe_ids = self._model_recipe_scratch[:count]
        np.take(table.model_recipe_id, slots, out=recipe_ids)
        order, starts = sort_into_runs(recipe_ids)
        sorted_slots = self._model_sorted_slots_scratch[:count]
        np.take(slots, order, out=sorted_slots)

        recipes = table.model_recipes()
        current_shader = None
        for start, end in zip(starts[:-1], starts[1:]):
            if start == end:
                continue
            recipe_id = int(recipe_ids[order[start]])
            if recipe_id < 0 or recipe_id >= len(recipes):
                continue
            model_path, manual_texture, override_color = recipes[recipe_id]
            obj = self.load_model(model_path)
            if not obj or not obj.is_loaded:
                continue

            run_slots = sorted_slots[start:end]
            self.render_stats.visible_tris += (obj.vertex_count // 3) * len(run_slots)
            self._fill_model_instance_buffer_numeric(table, run_slots)
            self._ensure_model_instance_vao(obj.vao)
            gl.glBindVertexArray(obj.vao)

            groups = obj.groups or []
            if manual_texture:
                shader_kind = (
                    'textured' if self.shaders.get('textured_instanced')
                    else 'lit' if self.shaders.get('lit_instanced') else None)
                if not shader_kind:
                    continue
                shader_name = shader_kind + '_instanced'
                current_shader = self._prepare_model_shader(
                    shader_name, projection, view, lights, current_shader)
                if current_shader != shader_name:
                    continue
                u = self.uniforms[shader_name]
                gl.glBindTexture(
                    gl.GL_TEXTURE_2D,
                    self._model_texture_id(manual_texture, manual=True))
                if shader_kind != 'textured':
                    colour = override_color or (0.8, 0.8, 0.8)
                    gl.glUniform3fv(u['object_color'], 1, colour)
                    gl.glUniform1f(u['alpha'], 1.0)
                gl.glDrawArraysInstanced(
                    gl.GL_TRIANGLES, 0, obj.vertex_count, len(run_slots))
                self.render_stats.draw_calls += 1
                self.render_stats.batched_draws += 1
                continue

            if not groups:
                if not self.shaders.get('lit_instanced'):
                    continue
                shader_kind = 'lit'
                shader_name = 'lit_instanced'
                current_shader = self._prepare_model_shader(
                    shader_name, projection, view, lights, current_shader)
                if current_shader != shader_name:
                    continue
                u = self.uniforms[shader_name]
                if shader_kind == 'lit':
                    gl.glUniform3fv(u['object_color'], 1, (0.8, 0.8, 0.8))
                    gl.glUniform1f(u['alpha'], 1.0)
                gl.glDrawArraysInstanced(
                    gl.GL_TRIANGLES, 0, obj.vertex_count, len(run_slots))
                self.render_stats.draw_calls += 1
                self.render_stats.batched_draws += 1
                continue

            for group in groups:
                material = obj.materials.get(
                    group['material'],
                    {'color': [0.8, 0.8, 0.8], 'texture': None})
                use_texture = material.get('texture')
                shader_kind = (
                    'textured' if use_texture and self.shaders.get('textured_instanced')
                    else 'lit' if self.shaders.get('lit_instanced') else None)
                if not shader_kind:
                    continue
                shader_name = shader_kind + '_instanced'
                current_shader = self._prepare_model_shader(
                    shader_name, projection, view, lights, current_shader)
                if current_shader != shader_name:
                    continue
                u = self.uniforms[shader_name]
                if shader_kind == 'textured':
                    gl.glBindTexture(
                        gl.GL_TEXTURE_2D,
                        self._model_texture_id(use_texture, material, manual=False))
                else:
                    colour = tuple(material.get('color', [0.8, 0.8, 0.8]))
                    gl.glUniform3fv(u['object_color'], 1, colour)
                    gl.glUniform1f(u['alpha'], 1.0)
                if group.get('indexed', False) and getattr(obj, 'ebo', None) is not None:
                    gl.glDrawElementsInstanced(
                        gl.GL_TRIANGLES, group['count'], gl.GL_UNSIGNED_INT,
                        ctypes.c_void_p(group['start'] * 4), len(run_slots))
                else:
                    gl.glDrawArraysInstanced(
                        gl.GL_TRIANGLES, group['start'], group['count'], len(run_slots))
                self.render_stats.draw_calls += 1
                self.render_stats.batched_draws += 1

        gl.glBindVertexArray(0)
        if cull_was_enabled:
            gl.glEnable(gl.GL_CULL_FACE)
        else:
            gl.glDisable(gl.GL_CULL_FACE)
        return count


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

    def set_instance_textures(self, textures):
        self.instance_textures = textures

    def _sprite_gl_ids(self, table):
        """``sprite id -> GL texture id``, for every recipe the table interned.

        The entity projection is GL-free, so it interns sprite *recipes* --
        ordered candidate cache keys and how to load each -- and the resolution
        to a GL id happens here, once per unique recipe, on the thread that has
        a context.  Exactly the shape :meth:`Renderer_F._gl_texture_ids` has for
        brush face textures.

        Each candidate is tried in the order the object path tried it: look the
        key up in the shared sprite-texture cache, and on a miss load the file
        if the recipe names one.  A recipe no candidate satisfies resolves to
        0, which is how the object path's "this sprite has no texture, draw
        nothing" is said numerically.
        """
        recipes = table.sprite_recipes()
        if recipes is not self._sprite_recipes_seen:
            # A different projection, so a different id space -- a new play
            # session builds a new EntityTable while the renderer outlives it.
            # Identity of the recipe list is the cheapest way to notice, and
            # the list outlives nothing: holding it does not keep the table.
            self._sprite_recipes_seen = recipes
            self._sprite_gl_by_id = np.zeros(0, dtype=np.int32)
            self._sprite_gl_resolved = 0
        cached = self._sprite_gl_by_id
        resolved = self._sprite_gl_resolved
        recipe_count = len(recipes)
        if resolved < recipe_count:
            if len(cached) < recipe_count:
                capacity = max(16, len(cached) * 2, recipe_count)
                grown = np.zeros(capacity, dtype=np.int32)
                if resolved:
                    grown[:resolved] = cached[:resolved]
                self._sprite_gl_by_id = grown
                cached = grown
            for sprite_id in range(resolved, recipe_count):
                cached[sprite_id] = self._resolve_sprite_recipe(recipes[sprite_id])
            self._sprite_gl_resolved = recipe_count
        return cached

    def _resolve_sprite_recipe(self, candidates):
        """The GL texture id for one interned candidate list, or 0."""
        for key, filename, subfolder, cache in candidates:
            if key:
                tex_id = self.sprite_textures.get(key)
                if tex_id:
                    return int(tex_id)
            if not filename:
                continue
            tex_id = self.load_texture(filename, subfolder)
            if tex_id:
                if cache and key:
                    self.sprite_textures[key] = tex_id
                return int(tex_id)
        return 0

    def draw_sprites_instanced(self, projection, view, table, slots,
                               gl_ids=None, camera_pos=None):
        """The sprite pass over dense columns: one draw per texture run.

        *slots* are rows of an :class:`engine.entity_table.EntityTable`, already
        classified into the sprite pass.  Everything this needs is a column
        read: the centre from ``pos``, the size from ``sprite_size``, the
        texture from ``sprite_key_id`` through :meth:`_sprite_gl_ids`.  No
        entity is touched.

        Rows whose texture resolves to 0 are dropped, which is what the object
        path's ``if tex_id:`` did.  The rest are sorted numerically by texture,
        with camera depth as a secondary key when a camera is supplied.  The
        stable lexicographic sort keeps each texture run back-to-front without
        a separate depth sort, and each run is one ``glDrawArraysInstanced``
        over a slice of the packed buffer.

        Returns the number of sprites submitted, so a caller can tell an empty
        pass from a skipped one.
        """
        if 'sprite_instanced' not in self.shaders or not len(slots):
            return 0
        if gl_ids is None:
            gl_ids = self._sprite_gl_ids(table)

        slot_count = len(slots)
        if len(self._sprite_key_scratch) < slot_count:
            grown = max(64, len(self._sprite_key_scratch) * 2, slot_count)
            self._sprite_key_scratch = np.empty(grown, dtype=np.int32)
            self._sprite_texture_scratch = np.empty(grown, dtype=np.int32)
            self._sprite_draw_mask = np.empty(grown, dtype=bool)
            self._sprite_depth_scratch = np.empty(grown, dtype=np.float64)
            self._sprite_depth_aux_scratch = np.empty(grown, dtype=np.float64)
            self._sprite_sorted_slots_scratch = np.empty(grown, dtype=np.int32)

        key_ids = self._sprite_key_scratch[:slot_count]
        textures = self._sprite_texture_scratch[:slot_count]
        drawn = self._sprite_draw_mask[:slot_count]

        np.take(table.sprite_key_id, slots, out=key_ids)
        drawn[:] = key_ids >= 0
        if len(gl_ids):
            np.maximum(key_ids, 0, out=key_ids)
            np.take(gl_ids, key_ids, out=textures)
            drawn &= textures > 0
        else:
            drawn.fill(False)

        valid_count = int(np.count_nonzero(drawn))
        if valid_count == 0:
            return 0
        if valid_count != slot_count:
            slots = slots[drawn]
            textures = textures[drawn]

        # Texture is the draw key. The old path depth-sorted the slots and
        # then stable-sorted those same slots again by texture. Keep both
        # requirements numeric and let one stable lexicographic sort establish
        # texture runs with back-to-front depth order inside each run.
        if camera_pos is None:
            order, run_starts = sort_into_runs(textures)
        else:
            cx, _, cz = self._camera_xyz(camera_pos)
            sprite_count = len(slots)
            depth_sq = self._sprite_depth_scratch[:sprite_count]
            depth_aux = self._sprite_depth_aux_scratch[:sprite_count]
            np.take(table.pos[:, 0], slots, out=depth_sq)
            np.subtract(depth_sq, cx, out=depth_sq)
            np.square(depth_sq, out=depth_sq)
            np.take(table.pos[:, 2], slots, out=depth_aux)
            np.subtract(depth_aux, cz, out=depth_aux)
            np.square(depth_aux, out=depth_aux)
            np.add(depth_sq, depth_aux, out=depth_sq)
            np.negative(depth_sq, out=depth_aux)
            order, run_starts = sort_into_runs(
                textures, secondary=depth_aux)

        count = len(order)
        self._ensure_sprite_instance_buffer(count)
        data = self._sprite_instance_data[:count]
        sorted_slots = self._sprite_sorted_slots_scratch[:count]
        np.take(slots, order, out=sorted_slots)
        # Gather directly into the reusable GPU staging buffer.  The explicit
        # out= avoids a temporary (N,3)/(N,2) array on every sprite frame.
        np.take(table.pos, sorted_slots, axis=0, out=data[:, 0:3])
        np.take(table.sprite_size, sorted_slots, axis=0, out=data[:, 3:5])
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._sprite_instance_vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, data)

        shader, uniforms = (self.shaders['sprite_instanced'],
                            self.uniforms['sprite_instanced'])
        gl.glUseProgram(shader)
        self._current_shader = shader
        # Billboards are unlit, so they never reach _upload_lights_once -- they
        # still need fogging, exactly as the per-sprite path does.
        self._upload_env_uniforms('sprite_instanced')
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE,
                              glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE,
                              glm.value_ptr(view))
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(uniforms['sprite_texture'], 0)
        gl.glBindVertexArray(self._ensure_sprite_instance_vao())

        current_tex = None
        for run in range(len(run_starts) - 1):
            begin = int(run_starts[run])
            length = int(run_starts[run + 1]) - begin
            if length <= 0:
                continue
            tex_id = int(textures[order[run_starts[run]]])
            if tex_id != current_tex:
                gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                current_tex = tex_id
                self.render_stats.batched_draws += 1
            self._point_sprite_instances_at(begin)
            gl.glDrawArraysInstanced(gl.GL_TRIANGLE_STRIP, 0, 4, length)
            self.render_stats.draw_calls += 1
        gl.glBindVertexArray(0)
        return count

    # --------------------------------------------------------------------------
    # Water / Glass / Fog
    # --------------------------------------------------------------------------
    def draw_water_brushes(self, projection, view, camera_pos, brushes, lights, config,
                           table):
        """Draw water from dense RenderTable state.

        The pass captures the opaque scene once for screen-space transmission.
        Optional environment cubemaps are already rendered by Renderer_F before
        this method is called; the hot loop consumes only dense columns and GL
        texture handles.
        """
        if len(brushes) == 0 or 'water' not in self.shaders:
            return
        if table is None:
            raise RuntimeError("draw_water_brushes requires RenderTable")
        if not getattr(self, 'water_enabled', True):
            return

        shader, uniforms = self.shaders['water'], self.uniforms['water']
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_lights_once('water', lights)
        gl.glUniformMatrix4fv(
            uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(
            uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform3fv(
            uniforms['viewPos'], 1, glm.value_ptr(camera_pos))
        gl.glUniform1f(uniforms['time'], config.get('time', 0.0))

        # Water uses the same scene capture as Glass, but captures before any
        # water surface is submitted so transmission never contains the water
        # itself.
        scene_size = self._capture_glass_scene()
        if scene_size is None:
            viewport = gl.glGetIntegerv(gl.GL_VIEWPORT)
            scene_size = (
                max(int(viewport[2]), 1),
                max(int(viewport[3]), 1),
            )
        scene_width, scene_height = scene_size

        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self.water_normal_id)
        gl.glUniform1i(uniforms['normalMap'], 0)

        scene_unit = self._glass_scene_texture_unit
        gl.glActiveTexture(gl.GL_TEXTURE0 + scene_unit)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._glass_scene_texture)
        gl.glUniform1i(uniforms['sceneColor'], scene_unit)
        gl.glUniform2f(
            uniforms['screenSize'],
            float(scene_width),
            float(scene_height),
        )

        reflection_unit = self.WATER_REFLECTION_TEXTURE_UNIT
        gl.glActiveTexture(gl.GL_TEXTURE0 + reflection_unit)
        gl.glUniform1i(uniforms['reflectionCube'], reflection_unit)

        opacity_loc = uniforms['waterOpacity']
        reflectivity_loc = uniforms['waterReflectivity']
        fresnel_loc = uniforms['fresnelIntensity']
        distortion_loc = uniforms['distortionStrength']
        refraction_loc = uniforms['refractionIndex']
        roughness_loc = uniforms['roughness']
        reflection_enabled_loc = uniforms['reflectionEnabled']
        tint_loc = uniforms['waterTint']
        model_loc = uniforms['model']
        normal_mat_loc = uniforms.get('normalMatrix', -1)
        wave_amp_loc = uniforms['waveAmp']
        brush_size_loc = uniforms['brushSize']

        models, normals = self._frame_transforms(table, brushes)
        sizes = table.half[brushes] * 2.0
        params = table.water_params[brushes]
        tints = table.water_tint[brushes]
        planes = table.water_plane[brushes]
        reflection_flags = table.water_reflections[brushes]
        bits = table.class_bits[brushes]
        geo = (bits & render_table.CLASS_HAS_GEOMETRY) != 0
        geo_meshes = self._prepare_geo_meshes(table, brushes)

        surface_vao = self.vaos.get('water_surface')
        cube_vao = self.vaos['cube']

        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)

        for i, slot_value in enumerate(brushes):
            slot = int(slot_value)
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, models[i])
            if normal_mat_loc >= 0:
                gl.glUniformMatrix3fv(
                    normal_mat_loc, 1, gl.GL_FALSE, normals[i])

            opacity = float(params[i, 0])
            fresnel = float(params[i, 1])
            gl.glUniform1f(opacity_loc, opacity)
            gl.glUniform1f(reflectivity_loc, fresnel)
            gl.glUniform1f(fresnel_loc, fresnel)
            gl.glUniform1f(distortion_loc, float(params[i, 4]))
            gl.glUniform1f(refraction_loc, max(float(params[i, 5]), 1.0))
            gl.glUniform1f(roughness_loc, min(max(float(params[i, 6]), 0.0), 1.0))
            gl.glUniform3fv(tint_loc, 1, tints[i])
            gl.glUniform3f(
                brush_size_loc,
                float(sizes[i, 0]),
                float(sizes[i, 1]),
                float(sizes[i, 2]),
            )

            h = float(params[i, 2])
            if h > 2.0:
                h /= 100.0
            amp = h * 30.0 if params[i, 3] != 0.0 else 1.2
            amp = min(amp, float(sizes[i, 1]) * 0.45, 30.0)
            gl.glUniform1f(wave_amp_loc, amp)

            reflection_tex = (
                self._water_reflection_texture(slot)
                if bool(reflection_flags[i]) else 0
            )
            if reflection_tex:
                gl.glBindTexture(
                    gl.GL_TEXTURE_CUBE_MAP,
                    reflection_tex,
                )
                gl.glUniform1i(reflection_enabled_loc, 1)
            else:
                gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
                gl.glUniform1i(reflection_enabled_loc, 0)

            mesh = (
                geo_meshes.get(int(table.geometry_id[slot]))
                if geo[i] else None
            )
            if mesh is not None:
                top_count = mesh.count - mesh.side_count
                gl.glBindVertexArray(mesh.vao)
                if not bool(planes[i]):
                    gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.side_count)
                if mesh.has_flat_top and surface_vao:
                    gl.glBindVertexArray(surface_vao)
                    gl.glDrawElements(
                        gl.GL_TRIANGLES,
                        self._water_surface_index_count,
                        gl.GL_UNSIGNED_INT,
                        None,
                    )
                elif top_count:
                    gl.glDrawArrays(
                        gl.GL_TRIANGLES,
                        mesh.side_count,
                        top_count,
                    )
                elif bool(planes[i]):
                    gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)
                self.render_stats.draw_calls += 1
                continue

            if not bool(planes[i]):
                gl.glBindVertexArray(cube_vao)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 24)
            if surface_vao:
                gl.glBindVertexArray(surface_vao)
                gl.glDrawElements(
                    gl.GL_TRIANGLES,
                    self._water_surface_index_count,
                    gl.GL_UNSIGNED_INT,
                    None,
                )
            else:
                gl.glBindVertexArray(cube_vao)
                gl.glDrawArrays(gl.GL_TRIANGLES, 30, 6)
            self.render_stats.draw_calls += 1

        gl.glBindVertexArray(0)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        return

    def _capture_glass_scene(self):
        """Copy the current framebuffer into the glass transmission texture.

        The copy happens once before the glass pass, so every glass surface
        samples the same scene behind it. GL 3.3 supports this without adding
        another permanent render target to the forward pipeline.
        """
        viewport = gl.glGetIntegerv(gl.GL_VIEWPORT)
        if viewport is None or len(viewport) < 4:
            return None
        x, y, width, height = (int(viewport[0]), int(viewport[1]),
                               int(viewport[2]), int(viewport[3]))
        if width <= 0 or height <= 0:
            return None

        if not self._glass_scene_texture:
            self._glass_scene_texture = int(gl.glGenTextures(1))

        unit = gl.GL_TEXTURE0 + self._glass_scene_texture_unit
        gl.glActiveTexture(unit)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._glass_scene_texture)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)

        if self._glass_scene_size != (width, height):
            gl.glTexImage2D(
                gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height, 0,
                gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
            self._glass_scene_size = (width, height)

        gl.glCopyTexSubImage2D(
            gl.GL_TEXTURE_2D, 0, 0, 0, x, y, width, height)
        return width, height

    def draw_glass_brushes(self, projection, view, camera_pos, brushes, lights, config,
                           table):
        if len(brushes) == 0 or 'glass' not in self.shaders:
            return
        if table is None:
            raise RuntimeError("draw_glass_brushes requires RenderTable")
        shader, uniforms = self.shaders['glass'], self.uniforms['glass']
        gl.glUseProgram(shader)
        self._upload_env_uniforms('glass')
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform3fv(uniforms['viewPos'], 1, glm.value_ptr(camera_pos))

        # Capture once, before any glass surface is drawn.
        scene_size = self._capture_glass_scene()
        if scene_size is None:
            return
        scene_width, scene_height = scene_size
        scene_tex_unit = self._glass_scene_texture_unit
        gl.glActiveTexture(gl.GL_TEXTURE0 + scene_tex_unit)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._glass_scene_texture)
        gl.glUniform1i(uniforms['sceneColor'], scene_tex_unit)
        gl.glUniform2f(uniforms['screenSize'],
                       float(scene_width), float(scene_height))
        gl.glActiveTexture(gl.GL_TEXTURE0)

        model_loc = uniforms['model']
        water_color_loc = uniforms['waterColor']
        distortion_loc = uniforms['distortionStrength']
        fresnel_loc = uniforms['fresnelIntensity']
        opacity_loc = uniforms['glassOpacity']
        refraction_loc = uniforms['refractionIndex']
        roughness_loc = uniforms['roughness']
        normal_mat_loc = uniforms.get('normalMatrix', -1)
        if normal_mat_loc is None:
            normal_mat_loc = -1
        gl.glBindVertexArray(self.vaos['cube'])
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        gl.glEnable(gl.GL_CULL_FACE)
        gl.glCullFace(gl.GL_BACK)

        models, normals = render_table.model_matrices(table, brushes)
        colors = table.glass_color[brushes]
        params = table.glass_params[brushes]
        geo = ((table.class_bits[brushes] & render_table.CLASS_HAS_GEOMETRY) != 0)
        geo_meshes = self._prepare_geo_meshes(table, brushes)
        for i, slot_value in enumerate(brushes):
            slot = int(slot_value)
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, models[i])
            if normal_mat_loc > 0:
                gl.glUniformMatrix3fv(normal_mat_loc, 1, gl.GL_FALSE, normals[i])
            gl.glUniform3fv(water_color_loc, 1, colors[i])
            gl.glUniform1f(distortion_loc, float(params[i, 1]))
            gl.glUniform1f(fresnel_loc, float(params[i, 4]))
            gl.glUniform1f(opacity_loc, float(params[i, 0]))
            gl.glUniform1f(refraction_loc, float(params[i, 2]))
            gl.glUniform1f(roughness_loc, float(params[i, 3]))
            mesh = geo_meshes.get(int(table.geometry_id[slot])) if geo[i] else None
            if mesh is not None:
                gl.glBindVertexArray(mesh.vao)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)
                gl.glBindVertexArray(self.vaos['cube'])
            else:
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
            self.render_stats.draw_calls += 1
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glBindVertexArray(0)
        return
    def draw_fog_volumes(self, projection, view, camera_pos, brushes, lights, config, *, table):
        if len(brushes) == 0 or 'fog' not in self.shaders:
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

        models, _ = render_table.model_matrices(table, brushes)
        # model_matrices is column-major for GL; transpose into conventional
        # matrices, invert the batch, then transpose back for glUniform.
        mats = models.reshape(-1, 4, 4).transpose(0, 2, 1)
        inv = np.linalg.inv(mats).transpose(0, 2, 1).reshape(-1, 16).astype(np.float32)
        colors = table.fog_color[brushes]
        params = table.fog_params[brushes]
        geo = ((table.class_bits[brushes] & render_table.CLASS_HAS_GEOMETRY) != 0)
        geo_meshes = self._prepare_geo_meshes(table, brushes)
        for i, slot_value in enumerate(brushes):
            slot = int(slot_value)
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, models[i])
            gl.glUniformMatrix4fv(inv_model_loc, 1, gl.GL_FALSE, inv[i])
            gl.glUniform3fv(fog_color_loc, 1, colors[i])
            gl.glUniform1f(density_loc, float(params[i, 0]))
            gl.glUniform1f(noise_scale_loc, float(params[i, 1]))
            gl.glUniform3fv(object_color_loc, 1, colors[i])
            gl.glUniform1f(alpha_loc, 0.4)
            mesh = geo_meshes.get(int(table.geometry_id[slot])) if geo[i] else None
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
        return
    def _classify_brush_slots(self, table, slots, config):
        """Split visible brush slots into the render passes, numerically.

        The array form of ``_sort_objects``' brush half.  That loop asked every
        visible brush what it was, once per frame -- ``is_water_brush`` alone is
        six ``str.lower()`` calls and six substring searches per brush -- to
        reach a verdict that only changes when the brush is edited.  The
        projection resolved it at edit time into ``class_bits``, so the same
        split is one mask per class.

        The classes are mutually exclusive in the same order the Python chain
        tried them (water, then fog, then glass, then glow, then trigger), so
        ``_brush_class_bits`` sets at most one of them and the masks cannot
        disagree with what the loop used to decide.

        Returns int32 slot arrays -- no objects are materialised here.
        """
        empty = slots[:0]
        if not len(slots):
            return {'opaque': empty, 'textured': empty, 'solid': empty,
                    'transparent': empty, 'water': empty, 'glass': empty,
                    'fog': empty, 'glow': empty}

        bits = table.class_bits[slots]
        opaque_mask = (bits & render_table.CLASS_NON_OPAQUE) == 0
        textured_mask = (bits & render_table.CLASS_TEXTURED) != 0
        # A trigger volume is drawn as a wireframe while editing and not at all
        # in play, which is what the old loop's `if not is_play` meant.
        if config.get('play_mode', False):
            trigger_mask = np.zeros(len(slots), dtype=bool)
        else:
            trigger_mask = (bits & render_table.CLASS_TRIGGER) != 0

        return {
            'opaque': slots[opaque_mask],
            'textured': slots[opaque_mask & textured_mask],
            'solid': slots[opaque_mask & ~textured_mask],
            'transparent': slots[trigger_mask],
            'water': slots[(bits & render_table.CLASS_WATER) != 0],
            'glass': slots[(bits & render_table.CLASS_GLASS) != 0],
            'fog': slots[(bits & render_table.CLASS_FOG) != 0],
            'glow': slots[(bits & render_table.CLASS_GLOW) != 0],
        }

    @staticmethod
    def _distance_cull_thing_slots(table, slots, cx, cz, limit_sq):
        """:meth:`_distance_cull_slots` with the Thing pass's exemption.

        Lights and Portals survive the dense entity cull at any distance.
        The object-path predicate is no longer part of Portal rendering;
        the dense EntityTable ENT_CULL_EXEMPT mask is authoritative.
        """
        if not len(slots):
            return slots
        dx = table.pos[slots, 0] - cx
        dz = table.pos[slots, 2] - cz
        near = (dx * dx + dz * dz) <= limit_sq
        exempt = (table.class_bits[slots]
                  & entity_projection.ENT_CULL_EXEMPT) != 0
        return slots[near | exempt]

    @staticmethod
    def _distance_cull_slots(table, slots, cx, cz, limit_sq):
        """Narrow *slots* to those within *limit_sq* on the XZ plane.

        The broad-phase distance cull, as a mask rather than a compaction.  It
        used to run over a Python list and rebuild another one an element at a
        time (``render_cull.cull_by_distance``); the surviving slots are the
        same answer with no object touched.
        """
        if not len(slots):
            return slots
        dx = table.center[slots, 0] - cx
        dz = table.center[slots, 2] - cz
        return slots[(dx * dx + dz * dz) <= limit_sq]

    @staticmethod
    def _sort_slots_by_distance(table, slots, cx, cz, reverse=True):
        """Depth-order *slots* from the projection's centres.

        Replaces reconstructing an array from a list of row views and then
        rebuilding an object list from the sort order: the positions are
        already dense, so the sort is one argsort over a gathered distance
        vector and the result is still slots.
        """
        if len(slots) < 2:
            return slots
        dx = table.center[slots, 0] - cx
        dz = table.center[slots, 2] - cz
        distances = dx * dx + dz * dz
        order = np.argsort(-distances if reverse else distances, kind="stable")
        return slots[order]

    def _shader_light_cap(self, shader_name):
        """How many lights ``shader_name``'s ``lights[]`` array actually holds.

        The shader loops to ``active_lights``, so uploading a larger count than
        the array can hold used to make it index past the end — undefined
        behaviour, and the surplus lights never worked anyway.  Water and
        terrain are sized smaller on purpose; the ARM variants trade array size
        for uniform storage.
        """
        cap = _SHADER_LIGHT_CAPS.get(shader_name, self.MAX_LIGHTS)
        if self.lowpower_mode and shader_name in ('lit', 'textured', 'lit_instanced',
                                                  'textured_instanced', 'brush_instanced',
                                                  'lit_brush_instanced'):
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
                     'brush_instanced', 'lit_brush_instanced', 'water', 'terrain'):
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
        """Pack the active light slice once and upload it to the shared UBO."""
        count = min(int(count), self.MAX_LIGHTS)
        self._ensure_light_ubo(count)
        if count <= 0:
            self._light_ubo_key = ()
            return

        if not (isinstance(lights, tuple) and len(lights) == 2
                and hasattr(lights[0], 'light_color')):
            raise TypeError("light upload requires (LightTable, slots)")
        table, slots = lights
        key = ('dense', id(table), table.generation, count, tuple(int(x) for x in slots[:count]))
        if self._light_ubo_key == key:
            return

        active = self._light_ubo_data[:count]
        active['position'].fill(0.0)
        active['color'].fill(0.0)
        active['params'].fill(0.0)
        active['indices'].fill(0)

        table, slots = lights
        active_lights = slots[:count]
        active['position'][:, :3] = table.pos[active_lights].astype(np.float32, copy=False)
        active['position'][:, 3] = 1.0
        active['color'][:, :3] = table.light_color[active_lights]
        active['color'][:, 3] = 1.0
        active['params'][:, :2] = table.light_params[active_lights]
        shadow_indices = np.fromiter(
            (self._light_shadow_index.get(int(slot), -1) for slot in active_lights),
            dtype=np.int32,
            count=count,
        )

        active['indices'][:, 0] = shadow_indices

        gl.glBindBuffer(gl.GL_UNIFORM_BUFFER, self._light_ubo)
        gl.glBufferSubData(
            gl.GL_UNIFORM_BUFFER,
            0,
            active,
        )
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
        if not (isinstance(lights, tuple) and len(lights) == 2
                and hasattr(lights[0], 'light_color')):
            raise TypeError("light upload requires (LightTable, slots)")
        table, slots = lights
        cap = self._shader_light_cap(shader_name)
        num_lights = min(len(slots), cap)

        self._ensure_light_ubo(num_lights)
        gl.glBindBufferBase(
            gl.GL_UNIFORM_BUFFER,
            shaders.LIGHT_UBO_BINDING,
            self._light_ubo,
        )
        self._frame_lights_uploaded[shader_name] = (
            id(table), table.generation,
            tuple(int(x) for x in slots[:cap]))
        gl.glUniform1i(self.uniforms[shader_name]['active_lights'], num_lights)
        self._upload_light_ubo(lights, num_lights)

        # Keep sampler2D and samplerCube uniforms on distinct texture units.
        # This is one shader-pass operation, never part of the per-draw loop.
        if shader_name in ('lit', 'textured', 'lit_instanced',
                           'textured_instanced', 'brush_instanced',
                           'lit_brush_instanced', 'terrain'):
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

    def _ensure_water_reflection_resources(self):
        """Create the shared render target used by high-quality water reflections."""
        if self._water_reflection_fbo and self._water_reflection_depth:
            return True
        try:
            size = self.WATER_REFLECTION_SIZE
            self._water_reflection_fbo = int(gl.glGenFramebuffers(1))
            self._water_reflection_depth = int(gl.glGenRenderbuffers(1))

            gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, self._water_reflection_depth)
            gl.glRenderbufferStorage(
                gl.GL_RENDERBUFFER, gl.GL_DEPTH_COMPONENT24, size, size)

            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._water_reflection_fbo)
            gl.glFramebufferRenderbuffer(
                gl.GL_FRAMEBUFFER,
                gl.GL_DEPTH_ATTACHMENT,
                gl.GL_RENDERBUFFER,
                self._water_reflection_depth,
            )
            # No colour attachment exists until a specific water cubemap face
            # is selected. The capture path validates completeness after it
            # attaches that face.
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, 0)
            gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, 0)
            return True
        except Exception as exc:
            print(f"[Water] reflection target initialisation failed: {exc}")
            self._water_reflection_depth = None
            if self._water_reflection_fbo:
                try:
                    gl.glDeleteFramebuffers(1, [self._water_reflection_fbo])
                except Exception:
                    pass
            self._water_reflection_fbo = None
            return False

    def _ensure_water_reflection_cubemap(self, slot):
        """Return the 256x256 RGBA cubemap owned by dense water slot."""
        slot = int(slot)
        existing = self._water_reflection_cubemaps.get(slot)
        if existing:
            return existing
        tex = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, tex)
        size = self.WATER_REFLECTION_SIZE
        for face in range(6):
            gl.glTexImage2D(
                gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + face,
                0,
                gl.GL_RGBA8,
                size,
                size,
                0,
                gl.GL_RGBA,
                gl.GL_UNSIGNED_BYTE,
                None,
            )
        gl.glTexParameteri(
            gl.GL_TEXTURE_CUBE_MAP,
            gl.GL_TEXTURE_MIN_FILTER,
            gl.GL_LINEAR_MIPMAP_LINEAR,
        )
        gl.glTexParameteri(
            gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(
            gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(
            gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(
            gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_R, gl.GL_CLAMP_TO_EDGE)
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
        self._water_reflection_cubemaps[slot] = tex
        return tex

    def _water_reflection_texture(self, slot):
        return self._water_reflection_cubemaps.get(int(slot), 0)

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

    @staticmethod
    def _shadow_caster_slots(table, all_slots):
        """The brushes eligible to cast a shadow, as slots.

        This filter used to be a Python walk over every brush in the level --
        five dict lookups and ``is_water_brush``'s six-string search each --
        run every frame, before anything had checked whether a single cube-map
        actually needed re-rendering.  The verdict changes only when a brush is
        edited, so the projection resolved it at edit time; here it is one mask.
        """
        if not len(all_slots):
            return all_slots
        bits = table.class_bits[all_slots]
        return all_slots[(bits & render_table.CLASS_SHADOW_CASTER) != 0]

    @staticmethod
    def _casters_in_reach(table, slots, lx, ly, lz, reach):
        """Caster slots within *reach* of a light, and a signature of them.

        Replaces rebuilding ``np.asarray([b['pos'] for b in brushes])`` and
        ``[b['size'] ...]`` from the brush dicts every frame: the projection
        already holds both, so the whole per-light test is
        ``center[slots]`` and one comparison.

        The signature is the caster geometry itself rather than a tuple
        reconstructed per brush.  A light's cube-map is valid exactly while the
        casters in reach of it have not moved or changed shape, which is what
        these bytes say -- and comparing them is a memcmp over a few kilobytes
        instead of building thousands of Python tuples.
        """
        if not len(slots):
            return slots, ()
        center = table.center[slots]
        dx = center[:, 0] - lx
        dy = center[:, 1] - ly
        dz = center[:, 2] - lz
        # A brush counts when the light reaches its bounding sphere, whose
        # radius is the largest half-extent -- the same test as before.
        limit = reach + table.half[slots].max(axis=1)
        sel = slots[(dx * dx + dy * dy + dz * dz) <= limit * limit]
        if not len(sel):
            return sel, ()
        geometry = np.concatenate((table.center[sel].ravel(),
                                   table.half[sel].ravel(),
                                   table.rot[sel].ravel().astype(np.float64)))
        return sel, (sel.tobytes(), geometry.tobytes())

    @staticmethod
    def _dense_shadow_model_slots(table, hidden=None):
        """Return model-entity slots eligible to cast shadows.

        This is the entity-table equivalent of the old Thing model scan.
        Model identity, representation and transforms have already been
        resolved at the dense projection boundary; the shadow pass only needs
        slot masks here.
        """
        if table is None or not table.count:
            return np.empty(0, dtype=np.int32)
        bits = table.class_bits[:table.count]
        mask = (
            ((bits & entity_projection.ENT_SKIP) == 0)
            & ((bits & entity_projection.ENT_ALWAYS_SPRITE) == 0)
            & ((bits & entity_projection.ENT_HAS_MODEL) != 0)
            & ((bits & entity_projection.ENT_MODE_MODEL) != 0)
        )
        if hidden is not None:
            mask &= ~np.asarray(hidden[:table.count], dtype=bool)
        return np.flatnonzero(mask).astype(np.int32)

    @staticmethod
    def _collect_dense_shadow_models(table, model_slots, lx, ly, lz, reach):
        """Return dense model caster slots in light range plus a cache key.

        The key contains only numerical projection state: slots, model recipe
        identity, translation and the precomputed rotation/scale matrix. No
        Thing or authored property dictionary is touched.
        """
        if not len(model_slots):
            return model_slots, ()
        positions = table.pos[model_slots]
        dx = positions[:, 0] - lx
        dy = positions[:, 1] - ly
        dz = positions[:, 2] - lz
        # Preserve the existing model-caster broad phase: models were treated
        # as having a radius of 2 * light reach.
        visible = (dx * dx + dy * dy + dz * dz) <= (reach * reach * 4.0)
        indices = np.flatnonzero(visible)
        slots = model_slots[indices]
        if not len(slots):
            return slots, ()
        recipe_ids = table.model_recipe_id[slots]
        positions = table.pos[slots]
        transforms = table.model_base_matrix[slots]
        signature = (
            slots.tobytes(),
            recipe_ids.tobytes(),
            positions.tobytes(),
            transforms.tobytes(),
        )
        return slots, signature

    #: The shadow pass's render key. One field, because one thing cannot vary
    #: within a depth draw: which cube face is being rendered, since that is
    #: the light-space matrix. Everything else -- each caster's transform --
    #: is instance data.
    #:
    #: Its items are its *runs*, which is the one place this differs from the
    #: brush passes. There an item is a face of a brush and the key partitions
    #: items into runs; here the caster set is identical for all six faces, so
    #: the six runs share one instance array rather than carving it up. Packing
    #: the casters once and drawing them six times is the whole saving.
    SHADOW_RUN_KEY = KeyLayout([('face', 3)])

    def _shadow_face_runs(self):
        """The six cube faces, as sorted runs.

        Trivial today -- six items, one field, already in order -- and that is
        the point of routing it through the same machinery rather than a bare
        ``range(6)``: the ordering is a property of the key, so a second field
        (batching two lights into one pass, say) changes the layout and nothing
        else.
        """
        keys = self.SHADOW_RUN_KEY.pack(face=np.arange(6, dtype=np.int64))
        order, starts = sort_into_runs(keys)
        return self.SHADOW_RUN_KEY.field(keys[order], 'face'), starts

    def _prepare_shadow_instances(self, table, in_brushes, instanced):
        """Pack dense brush casters and return dense convex geometry slots.

        All inputs are RenderTable slots. No Brush reference is reconstructed
        here; geometry rows remain integer geometry_id handles and AABB rows
        use the shared instanced cube buffer.
        """
        if table is None or not len(in_brushes):
            return np.empty(0, dtype=np.int32), np.empty(0, dtype=np.int32)

        slots = np.asarray(in_brushes, dtype=np.int32)
        geometry = (
            (table.class_bits[slots] & render_table.CLASS_HAS_GEOMETRY) != 0
        )
        cube_slots = slots[~geometry]
        geo_slots = slots[geometry]

        if instanced and len(cube_slots):
            models, _normals = self._frame_transforms(table, cube_slots)
            rows = np.arange(len(cube_slots), dtype=np.int32)
            self._pack_brush_instances(models, None, rows, 0.0, 0.0)

        return cube_slots, geo_slots
    def render_shadow_maps(self, shadow_lights, config, camera_pos=None):
        """Refresh depth cube-maps from dense RenderTable/EntityTable state.

        The shadow renderer has one data boundary: authored objects are
        projected first, then shadow selection, cache signatures and draw
        transforms operate only on integer slots and NumPy columns.
        """
        self._light_shadow_index = {}
        if not self._shadow_cubemaps or 'depth_cube' not in self.shaders:
            return

        if (not isinstance(shadow_lights, tuple)
                or len(shadow_lights) != 2):
            raise RuntimeError(
                "Fio 2.5.6 shadow rendering requires dense EntityTable lights")
        light_table, light_slots = shadow_lights
        if not hasattr(light_table, 'light_color'):
            raise RuntimeError(
                "Fio 2.5.6 shadow rendering requires EntityTable light state")
        lights = np.asarray(light_slots, dtype=np.int32)

        if not len(lights):
            for s in range(self.MAX_SHADOW_LIGHTS):
                self._shadow_slot_owner[s] = None
                self._shadow_slot_sig[s] = None
            return

        table = config.get('render_table')
        caster_slots = config.get('all_brush_slots')
        entity_table = config.get('entity_table')
        entity_hidden = config.get('thing_hidden')
        if table is None or caster_slots is None:
            raise RuntimeError(
                "Fio 2.5.6 shadow rendering requires RenderTable state")
        if entity_table is None:
            raise RuntimeError(
                "Fio 2.5.6 shadow rendering requires EntityTable state")

        caster_slots = self._shadow_caster_slots(
            table, np.asarray(caster_slots, dtype=np.int32))
        dense_model_slots = self._dense_shadow_model_slots(
            entity_table, entity_hidden)

        if len(lights) > self.MAX_SHADOW_LIGHTS:
            if camera_pos is not None:
                cx, cy, cz = self._camera_xyz(camera_pos)
                dx = light_table.pos[lights, 0] - cx
                dy = light_table.pos[lights, 1] - cy
                dz = light_table.pos[lights, 2] - cz
                order = np.argsort(
                    dx * dx + dy * dy + dz * dz, kind='stable')
                lights = lights[order[:self.MAX_SHADOW_LIGHTS]]
            else:
                lights = lights[:self.MAX_SHADOW_LIGHTS]

        light_keys = [int(s) for s in lights]
        current_ids = set(light_keys)
        for s in range(self.MAX_SHADOW_LIGHTS):
            if self._shadow_slot_owner[s] not in current_ids:
                self._shadow_slot_owner[s] = None
                self._shadow_slot_sig[s] = None

        light_slot = {}
        for key in light_keys:
            for s in range(self.MAX_SHADOW_LIGHTS):
                if self._shadow_slot_owner[s] == key:
                    light_slot[key] = s
                    break

        for key in light_keys:
            if key in light_slot:
                continue
            for s in range(self.MAX_SHADOW_LIGHTS):
                if self._shadow_slot_owner[s] is None:
                    self._shadow_slot_owner[s] = key
                    self._shadow_slot_sig[s] = None
                    light_slot[key] = s
                    break

        to_render = []
        for light_key in light_keys:
            shadow_slot = light_slot.get(light_key)
            if shadow_slot is None:
                continue

            light_slot_value = int(light_key)
            lx = float(light_table.pos[light_slot_value, 0])
            ly = float(light_table.pos[light_slot_value, 1])
            lz = float(light_table.pos[light_slot_value, 2])
            radius = max(
                float(light_table.light_params[light_slot_value, 1]), 1.0)

            in_slots, brush_keys = self._casters_in_reach(
                table, caster_slots, lx, ly, lz, radius)
            in_models, model_keys = self._collect_dense_shadow_models(
                entity_table, dense_model_slots, lx, ly, lz, radius)

            sig = (
                round(lx, 3), round(ly, 3), round(lz, 3),
                round(radius, 3), brush_keys, model_keys,
            )
            self._light_shadow_index[light_slot_value] = shadow_slot
            if self._shadow_slot_sig[shadow_slot] == sig:
                continue
            to_render.append((
                light_slot_value, shadow_slot, in_slots, in_models, sig,
                lx, ly, lz, radius,
            ))

        prev_fbo = int(gl.glGetIntegerv(gl.GL_FRAMEBUFFER_BINDING))
        prev_vp = gl.glGetIntegerv(gl.GL_VIEWPORT)
        scissor_was = bool(gl.glIsEnabled(gl.GL_SCISSOR_TEST))
        cull_was = bool(gl.glIsEnabled(gl.GL_CULL_FACE))
        blend_was = bool(gl.glIsEnabled(gl.GL_BLEND))
        prev_program = int(gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM))
        prev_shader = self._current_shader
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
        gl.glDisable(gl.GL_CULL_FACE)

        model_loc = u['model']
        lsm_loc = u['lightSpaceMatrix']
        depth_instanced = self.shaders.get('depth_cube_instanced')
        instance_vao = None
        inst_lsm_loc = inst_lightpos_loc = inst_far_loc = -1
        if depth_instanced is not None and self._cube_vbo is not None:
            iu = self.uniforms['depth_cube_instanced']
            inst_lsm_loc = iu['lightSpaceMatrix']
            inst_lightpos_loc = iu['lightPos']
            inst_far_loc = iu['far_plane']
            instance_vao = self._ensure_brush_instance_vao()
        lightpos_loc = u['lightPos']
        far_loc = u['far_plane']
        cube_vao = self.vaos['cube']

        face_dirs = (
            (glm.vec3( 1, 0, 0), glm.vec3(0, -1,  0)),
            (glm.vec3(-1, 0, 0), glm.vec3(0, -1,  0)),
            (glm.vec3( 0, 1, 0), glm.vec3(0,  0,  1)),
            (glm.vec3( 0,-1, 0), glm.vec3(0,  0, -1)),
            (glm.vec3( 0, 0, 1), glm.vec3(0, -1,  0)),
            (glm.vec3( 0, 0,-1), glm.vec3(0, -1,  0)),
        )

        for (light_identity, slot, in_brushes, in_models, sig,
             lx, ly, lz, far_plane) in to_render:
            center = glm.vec3(lx, ly, lz)
            far_plane = max(float(far_plane), 1.0)
            near_plane = max(far_plane * 0.002, 1.0)
            proj = glm.perspective(
                glm.radians(90.0), 1.0, near_plane, far_plane)
            cubemap = self._shadow_cubemaps[slot]

            gl.glUniform3f(lightpos_loc, lx, ly, lz)
            gl.glUniform1f(far_loc, far_plane)

            recipes = entity_table.model_recipes()
            resolved_models = []
            for slot_value in in_models:
                slot_value = int(slot_value)
                recipe_id = int(entity_table.model_recipe_id[slot_value])
                if recipe_id < 0 or recipe_id >= len(recipes):
                    continue
                model_path = recipes[recipe_id][0]
                obj = self.load_model(model_path)
                if obj and obj.is_loaded:
                    resolved_models.append((slot_value, obj))

            cube_slots, geo_slots = self._prepare_shadow_instances(
                table, in_brushes, depth_instanced is not None)

            for face in range(6):
                gl.glFramebufferTexture2D(
                    gl.GL_FRAMEBUFFER, gl.GL_DEPTH_ATTACHMENT,
                    gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + face, cubemap, 0)
                gl.glClear(gl.GL_DEPTH_BUFFER_BIT)
                lsm = proj * glm.lookAt(
                    center, center + face_dirs[face][0], face_dirs[face][1])
                gl.glUniformMatrix4fv(
                    lsm_loc, 1, gl.GL_FALSE, glm.value_ptr(lsm))

                if len(cube_slots) and depth_instanced is not None:
                    gl.glUseProgram(depth_instanced)
                    gl.glUniformMatrix4fv(
                        inst_lsm_loc, 1, gl.GL_FALSE, glm.value_ptr(lsm))
                    gl.glUniform3f(inst_lightpos_loc, lx, ly, lz)
                    gl.glUniform1f(inst_far_loc, far_plane)
                    gl.glBindVertexArray(instance_vao)
                    self._point_brush_instances_at(0)
                    gl.glDrawArraysInstanced(
                        gl.GL_TRIANGLES, 0, 36, len(cube_slots))
                    gl.glUseProgram(shader)
                    gl.glUniformMatrix4fv(
                        lsm_loc, 1, gl.GL_FALSE, glm.value_ptr(lsm))
                elif len(cube_slots):
                    cube_models, _ = self._frame_transforms(table, cube_slots)
                    gl.glBindVertexArray(cube_vao)
                    for i in range(len(cube_slots)):
                        gl.glUniformMatrix4fv(
                            model_loc, 1, gl.GL_FALSE, cube_models[i])
                        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)

                gl.glBindVertexArray(cube_vao)
                if len(geo_slots):
                    geo_meshes = self._prepare_geo_meshes(table, geo_slots)
                    geo_models, _geo_normals = self._frame_transforms(
                        table, geo_slots)
                    for geo_i, slot_value in enumerate(geo_slots):
                        mesh = geo_meshes.get(
                            int(table.geometry_id[int(slot_value)]))
                        if mesh is None:
                            continue
                        gl.glUniformMatrix4fv(
                            model_loc, 1, gl.GL_FALSE, geo_models[geo_i])
                        gl.glBindVertexArray(mesh.vao)
                        gl.glDrawArrays(
                            gl.GL_TRIANGLES, 0, mesh.count)
                        gl.glBindVertexArray(cube_vao)

                for slot_value, obj in resolved_models:
                    slot_value = int(slot_value)
                    base = np.asarray(
                        entity_table.model_base_matrix[slot_value],
                        dtype=np.float32).copy()
                    base[12:15] = np.asarray(
                        entity_table.pos[slot_value], dtype=np.float32)
                    gl.glUniformMatrix4fv(
                        model_loc, 1, gl.GL_FALSE, base)
                    gl.glBindVertexArray(obj.vao)
                    if (getattr(obj, 'ebo', None) is not None
                            and getattr(obj, 'index_count', 0)):
                        gl.glDrawElements(
                            gl.GL_TRIANGLES, obj.index_count,
                            gl.GL_UNSIGNED_INT, None)
                    else:
                        gl.glDrawArrays(
                            gl.GL_TRIANGLES, 0, obj.vertex_count)

            self._shadow_slot_sig[slot] = sig

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
        gl.glViewport(
            int(prev_vp[0]), int(prev_vp[1]),
            int(prev_vp[2]), int(prev_vp[3]))
        if scissor_was:
            gl.glEnable(gl.GL_SCISSOR_TEST)
        gl.glUseProgram(prev_program)
        self._current_shader = prev_shader
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

    def draw_portal_wireframes(self, projection, view, portal_table,
                               portal_slots, play_mode=False):
        """Draw portal editor wireframes directly from EntityTable columns.

        Portal rendering has one numerical source of truth: topology, aperture
        geometry, active/fade state, colour and rim visibility all live in the
        dense entity projection. This overlay deliberately does not accept a
        Thing collection, so the renderer cannot fall back to the old
        Portal-object walk.
        """
        if 'simple' not in self.shaders or portal_table is None:
            return
        if portal_slots is None:
            return
        portal_slots = np.asarray(portal_slots, dtype=np.int32)
        if not len(portal_slots):
            return

        show = portal_table.portal_show_rim[portal_slots]
        slots = portal_slots[show]
        if not len(slots):
            return

        shader, uniforms = self.shaders['simple'], self.uniforms['simple']
        gl.glUseProgram(shader)
        gl.glUniformMatrix4fv(
            uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(
            uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glUniform1f(uniforms['alpha'], 1.0)

        if self._portal_outline_vao is None:
            self._portal_outline_vao = gl.glGenVertexArrays(1)
            self._portal_outline_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(self._portal_outline_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._portal_outline_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, 4 * 3 * 4,
                            None, gl.GL_DYNAMIC_DRAW)
            gl.glVertexAttribPointer(
                0, 3, gl.GL_FLOAT, gl.GL_FALSE, 12, ctypes.c_void_p(0))
            gl.glEnableVertexAttribArray(0)
            gl.glBindVertexArray(0)

        if self._portal_normal_vao is None:
            self._portal_normal_vao = gl.glGenVertexArrays(1)
            self._portal_normal_vbo = gl.glGenBuffers(1)
            gl.glBindVertexArray(self._portal_normal_vao)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._portal_normal_vbo)
            gl.glBufferData(gl.GL_ARRAY_BUFFER, 2 * 3 * 4,
                            None, gl.GL_DYNAMIC_DRAW)
            gl.glVertexAttribPointer(
                0, 3, gl.GL_FLOAT, gl.GL_FALSE, 12, ctypes.c_void_p(0))
            gl.glEnableVertexAttribArray(0)
            gl.glBindVertexArray(0)

        gl.glLineWidth(1.0)
        model_loc = uniforms['model']
        color_loc = uniforms['color']
        gl.glUniformMatrix4fv(
            model_loc, 1, gl.GL_FALSE, glm.value_ptr(self._identity_mat4))

        for slot_value in slots:
            slot = int(slot_value)
            r, g, b = portal_table.portal_color[slot]
            if not bool(portal_table.portal_active[slot]):
                r, g, b = r * 0.4, g * 0.4, b * 0.4

            # The target link is already resolved to an integer slot. A missing
            # target is one scalar comparison, not a second Portal object scan.
            if int(portal_table.portal_target_slot[slot]) < 0:
                r, g, b = 0.86, 0.24, 0.24

            corners = self._portal_slot_corners(portal_table, slot)
            vdata = np.asarray(corners, dtype=np.float32).reshape(-1)
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._portal_outline_vbo)
            gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, vdata.nbytes, vdata)
            gl.glUniform3f(color_loc, float(r), float(g), float(b))
            gl.glBindVertexArray(self._portal_outline_vao)
            gl.glDrawArrays(gl.GL_LINE_LOOP, 0, 4)

            basis = portal_table.portal_basis[slot]
            normal = basis[2]
            pos = portal_table.pos[slot]
            arrow_len = float(portal_table.portal_width_height[slot, 0]) * 0.4
            nline = np.asarray(
                (
                    float(pos[0]), float(pos[1]), float(pos[2]),
                    float(pos[0] + normal[0] * arrow_len),
                    float(pos[1] + normal[1] * arrow_len),
                    float(pos[2] + normal[2] * arrow_len),
                ),
                dtype=np.float32,
            )
            gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._portal_normal_vbo)
            gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, nline.nbytes, nline)
            gl.glUniform3f(
                color_loc,
                min(1.0, float(r) * 1.6),
                min(1.0, float(g) * 1.6),
                min(1.0, float(b) * 1.6),
            )
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


    def _portal_candidate_slots(self, table, slots, camera_pos):
        """Return active, linked portal slots near a camera using table columns."""
        if table is None or slots is None or not len(slots):
            return np.empty(0, dtype=np.int32)
        slots = np.asarray(slots, dtype=np.int32)
        target = table.portal_target_slot[slots]
        keep = (target >= 0) & table.portal_active[slots] & (table.portal_fade[slots] > 0.01)
        if camera_pos is not None:
            delta = table.pos[slots] - np.asarray((float(camera_pos.x), float(camera_pos.y), float(camera_pos.z)), dtype=np.float64)
            keep &= np.einsum('ij,ij->i', delta, delta) <= (self.PORTAL_RENDER_DISTANCE * self.PORTAL_RENDER_DISTANCE)
        return slots[keep]

    @staticmethod
    def _portal_slot_basis(table, slot):
        basis = table.portal_basis[int(slot)]
        return basis[0], basis[1], basis[2]

    @classmethod
    def _portal_slot_corners(cls, table, slot, inset=0.0):
        width = float(table.portal_width_height[int(slot), 0])
        height = float(table.portal_width_height[int(slot), 1])
        inset = max(0.0, float(inset))
        # Keep the render aperture valid even for unusually small authored portals.
        max_inset = max(0.0, 0.5 * min(width, height) - 8.0)
        inset = min(inset, max_inset)
        return _portal_corners(
            table.pos[int(slot)],
            cls._portal_slot_basis(table, slot),
            width - inset * 2.0,
            height - inset * 2.0,
        )

    @staticmethod
    def _portal_slot_contains(table, slot, point):
        return _portal_contains_point(table.pos[int(slot)], BaseRenderer._portal_slot_basis(table, slot),
                                     table.portal_width_height[int(slot), 0], table.portal_width_height[int(slot), 1], point)

    def _portal_direction(self, table, slot):
        return int(table.portal_direction[int(slot)])

    def draw_portals(self, portal_table, portal_slots, projection, main_view, camera_pos, config, draw_scene_fn):
        """Render portal views from dense EntityTable topology."""
        if not self._portal_gl_ready:
            return
        portal_slots = self._portal_candidate_slots(portal_table, portal_slots, camera_pos)
        if not len(portal_slots):
            return
        pv = projection * main_view
        rendered = 0
        for portal_a in portal_slots:
            portal_a = int(portal_a)
            portal_b = int(portal_table.portal_target_slot[portal_a])
            if portal_b < 0:
                continue
            direction = self._portal_direction(portal_table, portal_a)
            if direction in (entity_projection.PORTAL_DIRECTION_FORWARD, entity_projection.PORTAL_DIRECTION_BOTH) and rendered < self.MAX_PORTALS:
                self._draw_one_portal(portal_table, portal_a, portal_b, projection, main_view, camera_pos, config, draw_scene_fn, pv, portal_slots, depth=1)
                rendered += 1
            if direction in (entity_projection.PORTAL_DIRECTION_REVERSE, entity_projection.PORTAL_DIRECTION_BOTH) and rendered < self.MAX_PORTALS:
                self._draw_one_portal(portal_table, portal_b, portal_a, projection, main_view, camera_pos, config, draw_scene_fn, pv, portal_slots, depth=1)
                rendered += 1
        gl.glDisable(gl.GL_SCISSOR_TEST)
        gl.glDisable(gl.GL_STENCIL_TEST)
        gl.glStencilMask(0xFF)
        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glClear(gl.GL_STENCIL_BUFFER_BIT)

    def _portal_flatten_depth(self, corners, projection, view, stencil_level):
        """Make the completed portal image occupy the source aperture's depth plane.

        The virtual scene needs its own depth buffer while it is rendered, but that
        depth is in the *destination* camera space.  Leaving it in the main depth
        buffer makes arbitrary destination geometry occlude main-world objects
        such as a carried prop.  Once the portal colour is complete, replace those
        virtual depths with the real source-aperture depth so the later main-scene
        passes compare against the portal plane, not the destination scene.
        """
        gl.glEnable(gl.GL_STENCIL_TEST)
        gl.glStencilFunc(gl.GL_EQUAL, stencil_level, 0xFF)
        gl.glStencilOp(gl.GL_KEEP, gl.GL_KEEP, gl.GL_KEEP)
        gl.glStencilMask(0x00)
        gl.glColorMask(gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDepthFunc(gl.GL_ALWAYS)
        gl.glDepthRange(0.0, 1.0)

        self._portal_upload_quad(corners)
        gl.glUseProgram(self._portal_mask_shader)
        gl.glUniformMatrix4fv(
            self._portal_mask_proj_loc, 1, gl.GL_FALSE, glm.value_ptr(projection))
        gl.glUniformMatrix4fv(
            self._portal_mask_view_loc, 1, gl.GL_FALSE, glm.value_ptr(view))
        gl.glBindVertexArray(self._portal_quad_vao)
        gl.glDrawArrays(gl.GL_TRIANGLE_FAN, 0, 4)

        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
        gl.glDepthFunc(gl.GL_LESS)


    def _draw_one_portal(self, portal_table, portal_a, portal_b, projection, main_view, camera_pos, config, draw_scene_fn, pv, portal_slots, depth=1):
        corners_a = self._portal_slot_corners(
            portal_table, portal_a, self.PORTAL_APERTURE_INSET)
        proj_ptr = glm.value_ptr(projection)
        view_ptr = glm.value_ptr(main_view)
        fade_a = float(portal_table.portal_fade[portal_a])
        rect = self._portal_screen_rect(corners_a, pv)
        if rect is not None:
            if rect[2] <= 0 or rect[3] <= 0:
                return
            gl.glEnable(gl.GL_SCISSOR_TEST)
            gl.glScissor(*rect)
        nrm = portal_table.portal_basis[portal_a, 2]
        apos = portal_table.pos[portal_a]
        cam_d = ((float(camera_pos.x)-apos[0])*nrm[0] + (float(camera_pos.y)-apos[1])*nrm[1] + (float(camera_pos.z)-apos[2])*nrm[2])
        straddle = (depth == 1 and self.PORTAL_NEAR_STRADDLE > 0.0 and abs(cam_d) < self.PORTAL_NEAR_STRADDLE and
                    self._portal_slot_contains(portal_table, portal_a,
                        (float(camera_pos.x)-cam_d*nrm[0], float(camera_pos.y)-cam_d*nrm[1], float(camera_pos.z)-cam_d*nrm[2])))
        if straddle:
            gl.glDisable(gl.GL_SCISSOR_TEST)
            mask_quad=[(-1.0,-1.0,0.0),(1.0,-1.0,0.0),(1.0,1.0,0.0),(-1.0,1.0,0.0)]
            id_ptr=glm.value_ptr(self._identity_mat4); mask_proj_ptr,mask_view_ptr=id_ptr,id_ptr
        else:
            mask_quad=corners_a; mask_proj_ptr,mask_view_ptr=proj_ptr,view_ptr
        parent_level=depth-1
        gl.glDisable(gl.GL_CULL_FACE); gl.glEnable(gl.GL_STENCIL_TEST); gl.glStencilMask(0xFF)
        if depth==1: gl.glClear(gl.GL_STENCIL_BUFFER_BIT)
        gl.glColorMask(gl.GL_FALSE,gl.GL_FALSE,gl.GL_FALSE,gl.GL_FALSE); gl.glDepthMask(gl.GL_FALSE)
        if straddle: gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glStencilFunc(gl.GL_EQUAL,parent_level,0xFF); gl.glStencilOp(gl.GL_KEEP,gl.GL_KEEP,gl.GL_INCR)
        self._portal_upload_quad(mask_quad); gl.glUseProgram(self._portal_mask_shader)
        gl.glUniformMatrix4fv(self._portal_mask_proj_loc,1,gl.GL_FALSE,mask_proj_ptr); gl.glUniformMatrix4fv(self._portal_mask_view_loc,1,gl.GL_FALSE,mask_view_ptr)
        gl.glBindVertexArray(self._portal_quad_vao); gl.glDrawArrays(gl.GL_TRIANGLE_FAN,0,4)
        if straddle: gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(gl.GL_TRUE); gl.glDepthFunc(gl.GL_ALWAYS); gl.glStencilFunc(gl.GL_EQUAL,depth,0xFF); gl.glStencilOp(gl.GL_KEEP,gl.GL_KEEP,gl.GL_KEEP); gl.glDepthRange(1.0,1.0)
        self._portal_upload_quad(mask_quad); gl.glDrawArrays(gl.GL_TRIANGLE_FAN,0,4)
        gl.glDepthRange(0.0,1.0); gl.glDepthFunc(gl.GL_LESS); gl.glColorMask(gl.GL_TRUE,gl.GL_TRUE,gl.GL_TRUE,gl.GL_TRUE)
        virtual_view,virtual_cam=self._portal_build_virtual_view(portal_table,portal_a,portal_b,main_view,camera_pos)
        clip_proj=self._calculate_oblique_projection(projection,virtual_view,portal_table.pos[portal_b],portal_table.portal_basis[portal_b,2])
        gl.glStencilFunc(gl.GL_EQUAL,depth,0xFF); gl.glStencilOp(gl.GL_KEEP,gl.GL_KEEP,gl.GL_KEEP); gl.glStencilMask(0x00)
        old_proj_ptr,old_view_ptr=self._proj_ptr,self._view_ptr; self._proj_ptr=glm.value_ptr(clip_proj); self._view_ptr=glm.value_ptr(virtual_view); self._current_shader=None; self._portal_scene_pass=True
        try:
            draw_scene_fn(
                RenderView(
                    clip_proj, virtual_view, virtual_cam,
                    aperture_slot=portal_a,
                    clip_slot=portal_b,
                    recursion_depth=depth,
                ),
                config,
            )
        finally:
            self._portal_scene_pass=False
        self._proj_ptr=old_proj_ptr; self._view_ptr=old_view_ptr; self._current_shader=None; gl.glStencilMask(0xFF)
        if depth < self.MAX_PORTAL_RECURSION:
            self._draw_nested_portals(portal_table,portal_a,portal_b,clip_proj,virtual_view,virtual_cam,config,draw_scene_fn,portal_slots,depth+1)

        if bool(portal_table.portal_show_rim[portal_a]):
            gl.glEnable(gl.GL_STENCIL_TEST); gl.glStencilFunc(gl.GL_EQUAL,depth,0xFF); gl.glStencilOp(gl.GL_KEEP,gl.GL_KEEP,gl.GL_KEEP); gl.glStencilMask(0x00); gl.glEnable(gl.GL_BLEND); gl.glBlendFunc(gl.GL_SRC_ALPHA,gl.GL_ONE)
            r,g,b=portal_table.portal_color[portal_a]; self._portal_upload_quad(corners_a); gl.glUseProgram(self._portal_rim_shader)
            gl.glUniformMatrix4fv(self._portal_rim_proj_loc,1,gl.GL_FALSE,proj_ptr); gl.glUniformMatrix4fv(self._portal_rim_view_loc,1,gl.GL_FALSE,view_ptr); gl.glUniform4f(self._portal_rim_color_loc,float(r),float(g),float(b),0.55*fade_a)
            gl.glBindVertexArray(self._portal_quad_vao); gl.glDrawArrays(gl.GL_LINE_LOOP,0,4); gl.glBlendFunc(gl.GL_SRC_ALPHA,gl.GL_ONE_MINUS_SRC_ALPHA); gl.glDisable(gl.GL_BLEND)
        if fade_a < 0.999:
            gl.glEnable(gl.GL_STENCIL_TEST); gl.glStencilFunc(gl.GL_EQUAL,depth,0xFF); gl.glStencilOp(gl.GL_KEEP,gl.GL_KEEP,gl.GL_KEEP); gl.glStencilMask(0x00); gl.glEnable(gl.GL_BLEND); gl.glBlendFunc(gl.GL_SRC_ALPHA,gl.GL_ONE_MINUS_SRC_ALPHA)
            self._portal_upload_quad(corners_a); gl.glUseProgram(self._portal_rim_shader); gl.glUniformMatrix4fv(self._portal_rim_proj_loc,1,gl.GL_FALSE,proj_ptr); gl.glUniformMatrix4fv(self._portal_rim_view_loc,1,gl.GL_FALSE,view_ptr); gl.glUniform4f(self._portal_rim_color_loc,0.0,0.0,0.0,1.0-fade_a)
            gl.glBindVertexArray(self._portal_quad_vao); gl.glDrawArrays(gl.GL_TRIANGLE_FAN,0,4); gl.glDisable(gl.GL_BLEND)

        # Replace destination-camera depth with the real source aperture depth
        # after all portal colour/rim/fade work is complete.
        self._portal_flatten_depth(corners_a, projection, main_view, depth)
        gl.glDisable(gl.GL_STENCIL_TEST); gl.glDisable(gl.GL_SCISSOR_TEST); gl.glBindVertexArray(0)

    def _draw_nested_portals(self, portal_table, from_a, from_b, projection, view, cam, config, draw_scene_fn, portal_slots, depth):
        candidates=self._portal_candidate_slots(portal_table,portal_slots,cam); pv=projection*view
        for portal_a in candidates:
            portal_a=int(portal_a)
            if portal_a==int(from_b): continue
            portal_b=int(portal_table.portal_target_slot[portal_a])
            if portal_b<0: continue
            self._draw_one_portal(portal_table,portal_a,portal_b,projection,view,cam,config,draw_scene_fn,pv,portal_slots,depth=depth)
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


    def _portal_build_virtual_view(self, portal_table, portal_a, portal_b, current_view, camera_pos):
        """Build a portal camera from dense EntityTable columns."""
        src_basis=self._portal_slot_basis(portal_table,portal_a)
        dst_basis=self._portal_slot_basis(portal_table,portal_b)
        virtual_cam=glm.vec3(*_portal_map_point(portal_table.pos[portal_a],src_basis,portal_table.pos[portal_b],dst_basis,
                                                (float(camera_pos.x),float(camera_pos.y),float(camera_pos.z))))
        fwd=(-float(current_view[0][2]),-float(current_view[1][2]),-float(current_view[2][2]))
        up=(float(current_view[0][1]),float(current_view[1][1]),float(current_view[2][1]))
        nf=_portal_map_direction(src_basis,dst_basis,fwd); nu=_portal_map_direction(src_basis,dst_basis,up)
        new_fwd=glm.normalize(glm.vec3(*nf)); new_up=glm.normalize(glm.vec3(*nu))
        return glm.lookAt(virtual_cam,virtual_cam+new_fwd,new_up),virtual_cam

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

    def _prepare_geo_meshes(self, table, slots):
        """Prepare convex meshes from dense geometry records."""
        if table is None or slots is None or not len(slots):
            return {}
        slots = np.asarray(slots, dtype=np.int32)
        gids = table.geometry_id[slots]
        gids = gids[gids >= 0]
        if not len(gids):
            return {}
        meshes = {}
        records = table.geometry_records
        for gid in np.unique(gids):
            gid = int(gid)
            if gid >= len(records):
                continue
            record = records[gid]
            if record is None:
                continue
            mesh = self._get_geo_mesh_record(record, geometry_id=gid,
                                             geometry_generation=table.generation)
            if mesh is not None:
                meshes[gid] = mesh
        return meshes

    def _get_geo_mesh_record(self, record, geometry_id=None, geometry_generation=None):
        """Get or build a GPU mesh from a dense GeometryRecord."""
        if record is None or record.convex is None or not record.convex.is_valid:
            return None
        key = record.signature
        cache_key = ((geometry_generation, int(geometry_id))
                     if geometry_id is not None else id(record))
        mesh = self._geo_mesh_cache.get(cache_key)
        if mesh is not None and mesh.key == key:
            mesh.frame = self._geo_mesh_frame
            return mesh
        try:
            new = self._build_geo_mesh(record, record.convex, key)
        except Exception as e:
            print(f"[GeoMesh] build failed: {e}")
            new = None
        if mesh is not None:
            self._delete_geo_mesh(mesh)
            self._geo_mesh_cache.pop(cache_key, None)
        if new is not None:
            new.frame = self._geo_mesh_frame
            self._geo_mesh_cache[cache_key] = new
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

    def _build_geo_mesh(self, record, convex, key):
        origin = record.origin
        scale = record.scale

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
            plane_meta = convex.planes[face['plane']]
            first = vert_count
            for k in range(1, len(idx) - 1):
                for j in (0, k, k + 1):
                    p = ring_l[j]
                    data.extend((p[0], p[1], p[2], ln[0], ln[1], ln[2],
                                 (us[j] - u0) / eu, (vs[j] - v0) / ev))
            vert_count += (len(idx) - 2) * 3
            runs.append({'face': face.get('face'), 'texture': face.get('texture'),
                         'uv_scale': face.get('uv_scale') or plane_meta.get('uv_scale'), 'plane': face.get('plane'),
                         'uv_angle': plane_meta.get('uv_angle', 0.0),
                         'uv_shift': plane_meta.get('uv_shift', (0.0, 0.0)),
                         'natural_scale': bool(record.natural_scale.get(id(face), False)),
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
    def _geo_run_texture(run):
        """Texture name baked into this cold geometry run."""
        return run.get('texture') or 'default.png'

    @staticmethod
    def _geo_run_tex_transform(run):
        """``(angle_radians, shift_u, shift_v)`` from cold run data."""
        shift = run.get('uv_shift') or (0.0, 0.0)
        return (math.radians(float(run.get('uv_angle', 0.0))),
                float(shift[0]), float(shift[1]))

    def _geo_run_tex_scale(self, run, tex_name):
        """UV repeat factors from cold convex-face data."""
        if run.get('natural_scale'):
            return brush_geometry.natural_repeats(
                run['extent'][0], run['extent'][1],
                self._texture_pixel_size(tex_name))
        uv = run.get('uv_scale')
        if uv is not None:
            return float(uv[0]), float(uv[1])
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

        if self._water_reflection_cubemaps:
            try:
                gl.glDeleteTextures(list(self._water_reflection_cubemaps.values()))
            except Exception:
                pass
            self._water_reflection_cubemaps.clear()
        if self._water_reflection_depth:
            try:
                gl.glDeleteRenderbuffers(1, [self._water_reflection_depth])
            except Exception:
                pass
            self._water_reflection_depth = None
        if self._water_reflection_fbo:
            try:
                gl.glDeleteFramebuffers(1, [self._water_reflection_fbo])
            except Exception:
                pass
            self._water_reflection_fbo = None

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
    def render_scene(self, projection, view, camera_pos, brushes, things,
                     selected_object, config, clear=True, brush_slots=None):
        raise NotImplementedError("Derived renderer must implement render_scene()")