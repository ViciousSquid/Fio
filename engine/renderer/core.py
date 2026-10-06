"""The renderer: shared state, frame orchestration and GL lifecycle.

:class:`Renderer` is Fio's one renderer, for the editor viewport and play mode
alike.  Its behaviour is split by responsibility across the mixins of this
package; this module owns only what they share -- the renderer state created
in :meth:`Renderer.__init__`, the per-frame sequencing in
:meth:`Renderer.render_scene`, and :meth:`Renderer.cleanup`.
"""

import os

import glm
import numpy as np
import OpenGL.GL as gl

from engine import entity_table as entity_projection
from engine import shaders
from engine.constants import (
    RENDER_MODE_LIT, RENDER_MODE_UNLIT, RENDER_MODE_WIREFRAME, RENDER_MODE_VERTEX)
from engine.view_distance import ViewDistance

# Camera render-distance cull. The pure per-object geometry lives in
# engine.render_cull (GL-free, so it is unit-testable without a GL context) and
# the live radius on self.view_distance (engine.view_distance); render_scene
# applies the pair to the MAIN camera pass only -- never to the shadow or
# portal passes, which keep using the full scene.
from engine.render_cull import camera_xz as _cull_camera_xz

from .debug import DebugMixin, RenderStats
from .geometry import GeometryMixin
from .lighting import LightingMixin
from .materials import MaterialsMixin, ShaderLoader
from .passes import PassesMixin
from .tables import TablesMixin
from .visibility import LODManager, VisibilityMixin


def restore_default_pixel_store():
    """Put the pixel-store state Qt's painter relies on back to GL defaults.

    The renderer shares its context with the QPainter that draws the HUD,
    and Qt uploads text glyphs into a texture assuming 4-byte row alignment
    and no row length. Any pass that changes those for its own uploads and
    leaves them changed shears every glyph that is not a multiple of four
    pixels wide -- small HUD and ``message`` text came out garbled. Called
    once before the painter opens, so no pass can leak into it.
    """
    gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
    gl.glPixelStorei(gl.GL_PACK_ALIGNMENT, 4)
    gl.glPixelStorei(gl.GL_UNPACK_ROW_LENGTH, 0)
    gl.glPixelStorei(gl.GL_UNPACK_SKIP_ROWS, 0)
    gl.glPixelStorei(gl.GL_UNPACK_SKIP_PIXELS, 0)


class Renderer(PassesMixin, LightingMixin, VisibilityMixin, TablesMixin,
               GeometryMixin, MaterialsMixin, DebugMixin):
    """Fio's forward OpenGL 3.3 renderer, shared by the editor and play mode.

    Consumes the dense ``RenderTable``/``EntityTable`` projections published
    by the logic thread and draws them; it never walks authored Brush/Thing
    objects on the hot path.
    """

    def __init__(self, texture_loader, initial_grid_size, initial_world_size, config=None):
        self.texture_manager = {}
        self.loaded_models = {}
        #: model path -> perf_counter() of its last failed load.
        self._failed_models = {}

        # Glass samples the already-rendered scene for screen-space transmission.
        # Kept lazy because most frames contain no glass at all.
        self._glass_scene_texture = 0
        self._glass_scene_size = (0, 0)
        self._glass_scene_texture_unit = 2
        # 'expensive' water also samples the scene's depth (copied the same
        # way) for depth absorption, soft shores, caustics and screen-space
        # reflections; 'cheap' water skips the copy and the reflection trace.
        # Chosen per water brush (its "High quality" property).
        self._water_depth_texture = 0
        self._water_depth_size = (0, 0)
        self._water_depth_texture_unit = 3


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
            self.lowpower_mode = config.getboolean(
                'Renderer', 'lowpower_mode', fallback=is_low_power)
            self.shadows_enabled = config.getboolean('Renderer', 'shadows_enabled', fallback=not is_low_power)
            try:
                shadow_size = config.getint('Renderer', 'shadow_map_size', fallback=self.SHADOW_MAP_SIZE)
            except Exception:
                shadow_size = self.SHADOW_MAP_SIZE
        else:
            self.lowpower_mode = is_low_power
            self.shadows_enabled = not is_low_power
            shadow_size = self.SHADOW_MAP_SIZE
        #: Session-only debug cap on water quality (see WATER_QUALITIES).
        #: Quality itself is per water brush (``water_high_quality``);
        #: 'expensive' lets each brush choose, 'cheap' forces every brush
        #: cheap. Set by the console's r_waterquality, never saved.
        self.water_quality = 'expensive'
        # Clamp to a sane, power-of-two-ish range. Lower = faster, blockier.
        self.shadow_map_size = max(256, min(2048, int(shadow_size)))

        self.fog_quality = 'low'      # 'low' = 16 steps, 'high' = 32 steps
        # OpenGL diagnostics are deliberately opt-in. Keep GL state probing out
        # of the normal textured-brush hot path.
        self.debug_gl_state = False
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
            (0, self.SPRITE_INSTANCE_FLOATS), dtype=np.float32)
        #: Entity sprite images as layers of one texture array; created on
        #: first use, on the thread that owns the context.
        self._sprite_layers = None
        #: Mesh bounding radius per interned model recipe (entity frustum cull).
        self._model_radius_recipes_seen = None
        self._model_radius_by_recipe = np.zeros(0, dtype=np.float64)
        # GPU-instanced EXPLOSION buffer. FIRE uses the ordinary instanced
        # billboard texture path, with one draw per animated texture frame.
        self._effect_instance_vbo = None
        self._effect_instance_vao = None
        self._effect_instance_capacity = 0
        self._effect_instance_data = np.empty((0, 16), dtype=np.float32)
        self._effect_order_scratch = np.empty(0, dtype=np.int32)
        self._effect_depth_scratch = np.empty(0, dtype=np.float64)
        self._effect_depth_aux_scratch = np.empty(0, dtype=np.float64)
        self._effect_expand_slots_scratch = np.empty(0, dtype=np.int32)
        self._effect_expand_particle_scratch = np.empty(0, dtype=np.float32)
        # Decoded animated Effect GIF frames, indexed by dense variant.
        # FIRE and ORB share this normal-instanced billboard path.
        self.effect_fire_frames = {}
        self.effect_fire_cumulative = {}
        self.effect_orb_frames = {}
        self.effect_orb_cumulative = {}
        self.effect_custom_frames = {}
        self.effect_custom_cumulative = {}
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
        self._model_instance_data = np.empty((0, 29), dtype=np.float32)
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
        # Dense texture ids are local to a RenderTable. With double-buffered
        # projections the two tables may have discovered different names first,
        # so a single name_id -> GL-id array is no longer a valid cache boundary.
        # Cache the resolved arrays per table; each array still grows only when
        # that table interns a new name.
        self._gl_tex_by_table = {}
        self._tex_size_by_table = {}

        self._proj_ptr = None
        self._view_ptr = None

        # VAOs and buffers (initialised after shaders compile)
        self.vaos = {'cube': None, 'sprite': None, 'grid': None}
        self.grid_indices_count = 0
        self.sprite_textures = {}
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

        # Convex geometry meshes, owned by geometry signature. The signature
        # carries the brush's geometry epoch, which every change to its shape
        # or face mapping bumps, so it names the mesh's content exactly -- and
        # it survives a table reconcile and is the same in both render
        # buffers' tables. Keying by (table generation, geometry id) rebuilt
        # every convex mesh, once per buffer, after any structural edit.
        # Meshes are dropped after going unused for a while (_begin_geo_frame).
        self._geo_mesh_cache = {}
        #: id(GeometryRecord) -> mesh: the per-frame lookup, which does not
        #: hash the signature. Validated against the record's signature, so a
        #: recycled id cannot alias; cleared with each stale-mesh sweep.
        self._geo_mesh_by_record = {}
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
        #: Cull the faces of opaque brushes that face away from the camera in
        #: the main view, as the portal pass always has. A closed opaque solid
        #: never shows its inside, so this changes no pixel; it halves what
        #: reaches the rasteriser. Switchable so the effect can be measured.
        self.cull_opaque_back_faces = True
        #: True while the main view's opaque brush passes draw (render_scene).
        self._opaque_cull_pass = False
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

        # EXPLOSION keeps its dedicated effect shader. FIRE is a plain animated
        # texture and is rendered through the normal instanced sprite shader.
        if not self._shader_init_failed:
            self._compile_instanced_effect_shader()

        # Terrain normal map (water)
        self.water_normal_id = self.load_texture('water_normal.png', 'textures')
        self.noise_texture_id = 0

        # Explosion animation atlas. The sheet is a 5x4 grid with 16 actual RGBA frames.
        # Keep it un-mipmapped and clamp to the sheet edge so linear filtering
        # cannot bleed neighbouring frames through transparent borders.
        self.effect_explosion_texture = 0
        explosion_path = os.path.join('assets', 'textures', 'effects', 'explosion.png')
        if os.path.exists(explosion_path):
            self.effect_explosion_texture = self.load_texture(
                'explosion.png', 'textures/effects')
            if self.effect_explosion_texture:
                gl.glBindTexture(gl.GL_TEXTURE_2D, self.effect_explosion_texture)
                gl.glTexParameteri(
                    gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
                gl.glTexParameteri(
                    gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
                gl.glTexParameteri(
                    gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
                gl.glTexParameteri(
                    gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)

        # Animated FIRE/ORB texture sets. Each GIF is decoded once into
        # individual GL textures; the render path only selects the current frame.
        self._load_fire_effect_textures()
        self._load_orb_effect_textures()

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

    def render_scene(self, projection, view, camera_pos,
                     primary_selection, config, clear=True, brush_slots=None):
        """Draw one view.

        *brush_slots* is the visibility result as integer slots into the dense
        render projection (config['render_table']).  When it is supplied,
        the brush half of the frame -- distance cull, classification into
        passes, depth ordering -- is done with masks over the projection's
        columns.  The portal virtual views consume the same projection too:
        their virtual frustum narrows all_brush_slots numerically before
        the normal numeric brush/entity passes run.
        """
        current_mode = config.get('render_mode', RENDER_MODE_LIT)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LESS)
        if clear:
            # FIX: Don't clear color when rendering a portal virtual view
            if getattr(self, '_portal_scene_pass', False):
                gl.glClear(gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)
            else:
                gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)
        self._proj_ptr = glm.value_ptr(projection)
        self._view_ptr = glm.value_ptr(view)
        # Every fog calculation this frame measures from here. Cached because
        # the unlit passes (sprites) and the terrain are not handed a camera.
        self._frame_camera_pos = self._camera_xyz(camera_pos)
        self.render_stats.reset()
        self.render_stats.total_brushes = len(config.get('all_brush_slots', ()))
        self._begin_geo_frame()
        self._frame_lights_uploaded.clear()
        self._light_ubo_key = None
        self._current_shader = None
        if current_mode == RENDER_MODE_WIREFRAME:
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)
        elif current_mode == RENDER_MODE_VERTEX:
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_POINT)
            # Clamped: a point size the driver does not support is a GL error,
            # not a silent clamp, and would take the whole frame with it.
            self._set_point_size(4.0)
        else:
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        self.draw_grid(projection, view, self.grid_indices_count,
                      config.get('play_mode', False), config.get('grid_visible', True))
        # Broad-phase distance cull (main camera pass only): feed the main
        # camera's slot/object classification a range-limited view of the scene,
        # on top of the frustum cull it already applies downstream. The original
        # Brush/Thing lists remain intact only for systems that still require
        # authoring/runtime objects; portal scene contents consume the published
        # dense tables exclusively.
        # default; a caller can force it on/off via 'camera_distance_cull'.
        #
        # This is the cheap *approximation* of the view distance -- it drops an
        # object by the distance to its centre, so it is deliberately not what
        # guarantees "nothing renders past the far plane". The projection's far
        # plane does that, per fragment, in the editor as well as in play; this
        # pass only saves the CPU from sorting and submitting what that plane
        # would have thrown away. Leaving it off in the editor keeps a large
        # brush whose centre is out of range but whose near end is in shot from
        # blinking out while it is being built.
        table = config.get('render_table')
        if brush_slots is None or table is None:
            raise RuntimeError(
                "Fio 2.5 renderer requires dense RenderTable brush slots")

        etable = config.get('entity_table')
        thing_slots = config.get('visible_thing_slots')
        thing_hidden = config.get('thing_hidden')
        if etable is None or thing_slots is None or thing_hidden is None:
            raise RuntimeError(
                "Fio 2.5 renderer requires dense EntityTable state")

        numeric = True
        sprite_slots = None
        numeric_model_slots = None
        effect_slots = None

        cx = cz = None
        if camera_pos is not None:
            cx, cz = _cull_camera_xz(camera_pos)

        # Brushes: masks over the dense RenderTable. No Brush objects are
        # materialised for classification, culling, sorting, or submission.
        slots = brush_slots
        if (config.get('camera_distance_cull', config.get('play_mode', False))
                and cx is not None):
            slots = self._distance_cull_slots(
                table, slots, cx, cz, self.view_distance.distance_sq)
        groups = self._classify_brush_slots(table, slots, config)
        if cx is not None:
            for key in ('transparent', 'water', 'glass'):
                groups[key] = self._sort_slots_by_distance(
                    table, groups[key], cx, cz)

        opaque_brushes = groups['opaque']
        textured_opaque = groups['textured']
        solid_opaque = groups['solid']
        transparent_brushes = groups['transparent']
        glow_brushes = groups['glow']
        water_brushes = groups['water']
        glass_brushes = groups['glass']
        fog_volumes = groups['fog']

        # Entities: classification, distance cull and submission all consume
        # EntityTable columns. No entity_refs -> Thing materialisation exists.
        tslots = thing_slots
        if (config.get('camera_distance_cull', config.get('play_mode', False))
                and cx is not None):
            tslots = self._distance_cull_thing_slots(
                etable, tslots, cx, cz, self.view_distance.distance_sq)
        numeric_model_slots, sprite_slots = entity_projection.classify_slots(
            etable, tslots, thing_hidden,
            config.get('play_mode', False),
            config.get('show_sprites_in_play_mode', False))
        # Frustum, against this view's own camera. The logic thread publishes
        # every entity row, because lights, portals and effects need them all;
        # the sprite and model passes only need what this camera can see, and
        # every row they skip is a quad or a mesh instance never packed,
        # uploaded or rasterised.
        entity_planes = self._frustum_planes(projection * view)
        candidates = len(sprite_slots) + len(numeric_model_slots)
        sprite_slots = self._cull_entity_rows(etable, sprite_slots, entity_planes)
        numeric_model_slots = self._cull_entity_rows(
            etable, numeric_model_slots, entity_planes, models=True)
        self.render_stats.entity_candidates = candidates
        self.render_stats.culled_entities = candidates - (
            len(sprite_slots) + len(numeric_model_slots))
        # Effects own a dedicated dense slot vector. Do not derive this
        # transient render pass from the generic Thing classification; a newly
        # authored Effect must become visible as soon as the EntityTable row exists.
        effect_slots = etable.effect_slots
        if (config.get('camera_distance_cull', config.get('play_mode', False))
                and cx is not None and len(effect_slots)):
            effect_slots = self._distance_cull_thing_slots(
                etable, effect_slots, cx, cz, self.view_distance.distance_sq)

        _tbl = table
        lights = self._get_active_lights(config)
        self._frame_lights = lights

        # --- Depth cube-map shadow pass -------------------------------------
        # Render shadow-casting point lights into their cube-maps *before* any
        # scene geometry so every lit/textured/terrain draw can sample them.
        self._light_shadow_index = {}
        if current_mode == RENDER_MODE_LIT and self.shadows_enabled:
            light_table, light_slots = lights
            shadow_slots = light_slots[
                light_table.light_casts_shadows[light_slots]]
            if len(shadow_slots):
                self.render_shadow_maps(
                    (light_table, shadow_slots), config, camera_pos)

        terrain = config.get('terrain', None)
        if terrain and terrain.enabled:
            # The same planes the entity passes cull against. Their far plane
            # is the view distance, so terrain beyond it is never submitted --
            # without them every resident chunk was drawn, and pulling the
            # view distance in did nothing for the terrain's cost.
            self.render_terrain(projection, view, camera_pos, terrain, lights,
                                frustum_planes=entity_planes)
        if (config.get('play_mode', False)
                and self._portal_gl_ready):
            # Portal discovery is a numeric EntityTable selection. No Thing
            # scan, name dictionary, or Portal object materialisation occurs
            # on the render hot path.
            portal_table = config.get('entity_table')
            portal_slots = (
                portal_table.portal_slots
                if portal_table is not None and hasattr(portal_table, 'portal_slots')
                else np.empty(0, dtype=np.int32)
            )
            if len(portal_slots):
                try:
                    self.draw_portals(
                        portal_table,
                        portal_slots,
                        projection,
                        view,
                        camera_pos,
                        config,
                        lambda view_state, cfg: self._draw_portal_scene(
                            view_state, cfg, camera_pos),
                    )
                    self._proj_ptr = glm.value_ptr(projection)
                    self._view_ptr = glm.value_ptr(view)
                except Exception as _pe:
                    print(f"[Portal] render error: {_pe}")
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_BLEND)
        brush_display_mode = config.get('brush_display_mode', 'Textured')
        # Filled modes only: in wireframe and vertex modes the far edges and
        # corners are part of what the editor shows.
        self._opaque_cull_pass = (self.cull_opaque_back_faces and current_mode
                                  in (RENDER_MODE_LIT, RENDER_MODE_UNLIT))
        try:
            if current_mode == RENDER_MODE_UNLIT:
                self.draw_textured_brushes_optimized(projection, view, camera_pos, textured_opaque, lights, config, _tbl)
                self.draw_lit_brushes_optimized(projection, view, camera_pos, solid_opaque, lights, config, table=_tbl)
            elif current_mode == RENDER_MODE_LIT:
                if brush_display_mode == 'Textured' or brush_display_mode == 'Solid Lit':
                    self.draw_textured_brushes_optimized(projection, view, camera_pos, textured_opaque, lights, config, _tbl)
                    self.draw_lit_brushes_optimized(projection, view, camera_pos, solid_opaque, lights, config, table=_tbl)
                else:
                    self.draw_lit_brushes_optimized(projection, view, camera_pos, opaque_brushes, lights, config, table=_tbl)
            else:
                self.draw_lit_brushes_optimized(projection, view, camera_pos, opaque_brushes, lights, config, table=_tbl)
        finally:
            self._opaque_cull_pass = False
        if len(glow_brushes):
            self.draw_glow_brushes(projection, view, camera_pos, glow_brushes, lights, config, table=_tbl)
        fading_model_slots = np.empty(0, dtype=np.int32)
        if len(numeric_model_slots):
            if not (self.shaders.get('lit_instanced')
                    or self.shaders.get('textured_instanced')):
                raise RuntimeError(
                    "Fio 2.5 requires instanced model shaders for dense entity rendering")
            fading_model_mask = etable.render_alpha[numeric_model_slots] < 1.0
            opaque_model_slots = numeric_model_slots[~fading_model_mask]
            fading_model_slots = numeric_model_slots[fading_model_mask]
            if len(opaque_model_slots):
                self.draw_models_instanced(
                    projection, view, camera_pos, etable, opaque_model_slots,
                    lights, config)
        if not config.get('play_mode', False):
            self.draw_path_node_cubes(projection, view, etable)
        if etable is not None:
            self.draw_portal_wireframes(
                projection, view, etable, etable.portal_slots,
                config.get('play_mode', False))
        gl.glEnable(gl.GL_BLEND)
        gl.glDepthMask(gl.GL_FALSE)
        # The sprite renderer has one path: dense EntityTable columns -> GL
        # instanced draws. Missing projection data is a caller error, not a
        # reason to resurrect the object renderer.
        if len(fading_model_slots):
            self.draw_models_instanced(
                projection, view, camera_pos, etable, fading_model_slots,
                lights, config)
        if effect_slots is not None and len(effect_slots):
            self.draw_effects_instanced(
                projection, view, etable, effect_slots,
                hidden=thing_hidden,
                play_mode=config.get('play_mode', False),
                editor_time=config.get('time', 0.0),
                camera_pos=camera_pos)

        if len(sprite_slots):
            self.draw_sprites_instanced(
                projection, view, etable, sprite_slots, camera_pos=camera_pos)
        if current_mode == RENDER_MODE_UNLIT:
            self.draw_textured_brushes_optimized(projection, view, camera_pos, transparent_brushes, lights, config, _tbl)
        elif current_mode == RENDER_MODE_LIT:
            self.draw_lit_brushes_optimized(projection, view, camera_pos, transparent_brushes, lights, config, is_transparent_pass=True, table=_tbl)
        else:
            self.draw_lit_brushes_optimized(projection, view, camera_pos, transparent_brushes, lights, config, is_transparent_pass=True, table=_tbl)
        if current_mode == RENDER_MODE_LIT:
            self.draw_water_brushes(
                projection, view, camera_pos, water_brushes, lights, config,
                table=_tbl)
            self.draw_glass_brushes(projection, view, camera_pos, glass_brushes, lights, config,
                                     table=_tbl)
            self.draw_fog_volumes(projection, view, camera_pos, fog_volumes, lights, config,
                                  table=_tbl)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        if primary_selection:
            self._draw_selection_overlays(
                projection, view, primary_selection, _tbl)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glUseProgram(0)


    # --------------------------------------------------------------------------
    # Cleanup
    # --------------------------------------------------------------------------
    def cleanup(self):
        """Release all OpenGL resources owned by the renderer."""
        if self._sprite_layers is not None:
            self._sprite_layers.cleanup()
            self._sprite_layers = None
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
        for attr in ('_glass_scene_texture', '_water_depth_texture'):
            tex = getattr(self, attr, 0)
            if tex:
                gl.glDeleteTextures([tex])
                setattr(self, attr, 0)
        self._glass_scene_size = (0, 0)
        self._water_depth_size = (0, 0)
        for mesh in self._geo_mesh_cache.values():
            self._delete_geo_mesh(mesh)
        self._geo_mesh_cache.clear()
        self._geo_mesh_by_record.clear()
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
        print("[Renderer] Cleaned up GL resources.")
