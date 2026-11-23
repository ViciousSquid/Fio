"""ForwardRenderer: Fio's built-in renderer implementation.

A forward renderer over the dense ``RenderTable``/``EntityTable`` frame input:
lit, textured and glow brush passes, instanced models and sprites, effects,
water/glass/fog volumes, point-light shadow cube-maps and stencil portals. It
implements :class:`engine.renderer.api.Renderer` and builds on
:class:`engine.renderer.core.RendererCore`.
"""

import os

import glm
import weakref

import numpy as np
import OpenGL.GL as gl

from engine.renderer.api import RENDERER_CONTRACT_VERSION

from engine import entity_table as entity_projection
from engine import shaders
from engine.constants import (
    PLAYER_HULL_SIZE,
    RENDER_MODE_LIT, RENDER_MODE_UNLIT, RENDER_MODE_WIREFRAME, RENDER_MODE_VERTEX)
from engine.entity_models import editor_models_around

# Camera render-distance cull. The pure per-object geometry lives in
# engine.render_cull (GL-free, so it is unit-testable without a GL context) and
# the live radius on self.view_distance (engine.view_distance); render_scene
# applies the pair to the MAIN camera pass only -- never to the shadow or
# portal passes, which keep using the full scene.
from engine.render_cull import camera_xz as _cull_camera_xz

from ..core import RendererCore
from .instancing import InstancingMixin
from .lighting import LightingMixin
from .passes import PassesMixin
from .portals import PortalsMixin
from .occlusion import OcclusionMixin
from .decals import DecalsMixin
from .textured import TexturedMixin
from .solid_lit import SolidLitMixin
from .wireframe import WireframeMixin
from .overlay import OverlayMixin
from .shaders import ShadersMixin




#: Entries a per-(view, table) kept result holds at most: views (main,
#: split-screen, portal, mirror) times the two render buffers, with room.
KEPT_PER_TABLE_LIMIT = 32


def _forget_dead_tables(kept, ref_index):
    """Drop *kept* entries whose table (``entry[ref_index]``, a weakref) is
    gone, then the oldest while over :data:`KEPT_PER_TABLE_LIMIT`."""
    for key in [k for k, entry in kept.items() if entry[ref_index]() is None]:
        del kept[key]
    while len(kept) >= KEPT_PER_TABLE_LIMIT:
        del kept[next(iter(kept))]


class ForwardRenderer(TexturedMixin, SolidLitMixin, WireframeMixin, OverlayMixin,
                      PassesMixin, LightingMixin, PortalsMixin, InstancingMixin, DecalsMixin,
                      OcclusionMixin, ShadersMixin, RendererCore):
    """Fio's forward OpenGL 3.3 renderer, registered as ``"Forward"``.

    *config* is the application ConfigParser (or None). *texture_loader*
    optionally replaces how unseen brush textures are resolved; the GL tests
    use it to serve a fixed texture.
    """

    #: The renderer contract Forward is written against.
    contract_version = RENDERER_CONTRACT_VERSION

    #: Texture unit of the angled brushes' transform buffer texture, read by
    #: the batched convex path's vertex shader. Units in use elsewhere: 0
    #: diffuse, 2 glass scene, 3 water depth, 4..11 shadow cube-maps, 12..13
    #: terrain height and paint (terrain program only).
    GEO_TRANSFORM_UNIT = 14
    #: Default per-face resolution of the sky's cube map ([Renderer]
    #: sky_cubemap_size), as SHADOW_MAP_SIZE is the shadow cube maps'.
    SKY_CUBEMAP_SIZE = 384
    #: Per-face resolution of a reflection probe's snapshot. Water ripples
    #: it, so it needs far less than the sky does.
    PROBE_FACE_SIZE = 128

    def __init__(self, config=None, *, texture_loader=None):
        super().__init__(texture_loader)
        self.render_stats.details['sprite texture array'] = self._describe_sprite_layers

        # Glass samples the already-rendered scene for screen-space transmission.
        # Kept lazy because most frames contain no glass at all.
        #: Screen-space reflections -- water, wet decals -- on or off (the
        #: ``reflections`` console command). They fade out with distance
        #: either way (engine.shaders.REFLECTION_FADE_START / END).
        self.reflections_enabled = True
        #: How far from the camera wet decals reflect (the ``decalfade``
        #: console command): full to two thirds of it, none from it on.
        from engine.shaders import REFLECTION_FADE_END
        self.decal_reflection_fade = REFLECTION_FADE_END
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
        #: The sky panorama, which the water reflects. GL 3.3 and GLES 3.0
        #: both guarantee 16 fragment texture units.
        self._panorama_texture_unit = 15

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
        # How angled brushes' faces are batched (PassesMixin.GEO_BATCHING):
        # driver-dependent, so measured rather than assumed, and settable.
        # (``settings``, not ``config``: the frame-input audit reads every
        # ``config.get`` in the package as a per-frame key.)
        settings = config
        batching = (settings.get('Renderer', 'geo_batching', fallback='elements')
                    if settings is not None else 'elements')
        batching = str(batching or '').strip().lower()
        self.geo_batching = batching if batching in self.GEO_BATCHING else 'elements'
        # Per-face resolution of the sky's cube map (_build_sky_cube), like
        # shadow_map_size for the shadow cube maps. Fixed whatever the
        # panorama's size: 384 is half the widest shipped sky's width, so
        # small detail such as stars survives being resampled.
        try:
            sky_size = (settings.getint('Renderer', 'sky_cubemap_size',
                                        fallback=self.SKY_CUBEMAP_SIZE)
                        if settings is not None else self.SKY_CUBEMAP_SIZE)
        except (TypeError, ValueError):
            sky_size = self.SKY_CUBEMAP_SIZE
        self.sky_cubemap_size = max(64, min(2048, int(sky_size)))

        self.fog_quality = 'low'      # 'low' = 16 steps, 'high' = 32 steps
        # OpenGL diagnostics are deliberately opt-in. Keep GL state probing out
        # of the normal textured-brush hot path.
        self.debug_gl_state = False
        self.skip_culling_in_renderer = True   # trust pre‑culled data

        self._model_matrix = glm.mat4(1.0)

        # Optional 360-degree background. Decode/upload lazily while this
        # renderer owns the current GL context; release it in cleanup().
        self._background_image_path = ""
        self._background_failed_path = None
        self._background_image_texture = 0      # equirectangular: the water reads it
        self._background_cube_texture = 0       # the same sky as a cube map: the sky pass
        self._sky_cube_program = 0              # draws the cube from the panorama, at load
        #: Reflection probes (editor.things.ReflectionProbe): snapshot per
        #: (position, radius), the probes this frame's water may use, and
        #: whether the frame being drawn is a snapshot's.
        self._probe_textures = {}
        self._probe_frame = None
        self._probe_capture_pass = False
        self._probe_slot_cache = (None, -1, -1, None)
        self._probe_equirect_program = 0
        self._background_image_program = 0
        self._background_image_vao = 0
        self._background_image_uniforms = None

        # GPU-instanced model data. One persistent VBO is shared by all model
        # VAOs; each instance carries a model matrix and normal matrix (112 B).
        # Box brush instances: one stream (buffer, VAO, CPU mirror) per pass,
        # per view, per use in the frame, written only where it changed
        # (instancing.BrushStream; one layout, BRUSH_INSTANCE_ATTRS).
        self._brush_streams = {}
        self._stream_calls = {}
        #: The view the passes are drawing: None for the main view, a portal's
        #: (aperture, depth) while its virtual view draws.
        self._instance_view = None
        #: Bytes uploaded to box instance streams (tests and tools).
        self.instance_bytes_uploaded = 0
        self._sprite_instance_vbo = None
        self._sprite_instance_vao = None
        self._sprite_instance_capacity = 0
        self._sprite_instance_base = 0
        self._sprite_instance_data = np.empty(
            (0, self.SPRITE_INSTANCE_FLOATS), dtype=np.float32)
        #: Entity sprite images as layers of one texture array; created on
        #: first use, on the thread that owns the context.
        self._sprite_layers = None
        #: id(recipe list) -> (list, length) last warmed (_warm_sprite_layers).
        self._sprite_warm_seen = {}
        # GPU-instanced EXPLOSION buffer. FIRE uses the ordinary instanced
        # billboard texture path, with one draw per animated texture frame.
        # Explosion rows hold no time, so they are uploaded only when they
        # change (_explosion_uploaded: the rows last uploaded, or None).
        self._effect_instance_vbo = None
        self._effect_instance_vao = None
        self._effect_instance_capacity = 0
        self._effect_instance_data = np.empty(
            (0, self.EFFECT_INSTANCE_FLOATS), dtype=np.float32)
        self._explosion_uploaded = None
        # Stateless particle emitters: one row per emitter, uploaded only when
        # a row or the draw order changes (draw_particles_instanced).
        self._particle_instance_vbo = None
        self._particle_instance_vao = None
        self._particle_instance_capacity = 0
        self._particle_instance_data = np.empty(
            (0, self.PARTICLE_INSTANCE_FLOATS), dtype=np.float32)
        self._particle_uploaded = None
        self._particle_presets_sent = None
        #: The scene depth copy (_capture_scene_depth) holds this frame's
        #: opaque depth: made by the decal pass, reused by the particles.
        self._scene_depth_current = False
        #: Depth copies made so far (_capture_scene_depth), to tell whether
        #: the decal pass made one.
        self._scene_depth_copies = 0
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
        # Model VAOs whose instance attributes are enabled; the buffers are
        # per draw call (_model_instance_stream).
        self._model_instanced_vaos = set()
        self._model_instance_streams = {}
        self._model_recipe_scratch = np.empty(0, dtype=np.int32)
        self._model_sorted_slots_scratch = np.empty(0, dtype=np.int32)

        # Shared std140 light UBO. One upload feeds every lighting shader.
        # Light data now travels through the shared std140 UBO; no per-slot
        # uniform-name table is needed on the render path.
        self._light_ubo = None
        self._light_ubo_capacity = 0
        self._light_ubo_key = None
        self._light_ubo_lights = None
        #: Whether ``_light_ubo`` is bound to ``LIGHT_UBO_BINDING``; see
        #: :meth:`_ensure_light_ubo`.
        self._light_ubo_bound = False
        self._light_ubo_dtype = self.LIGHT_UBO_DTYPE
        self._light_ubo_data = np.zeros(self.MAX_LIGHTS, dtype=self._light_ubo_dtype)
        #: The shared light-cookie texture array (engine.light_cookies),
        #: created the first time a light with a cookie is uploaded.
        self._light_cookies = None
        # Depth cube-map shadow-mapping state (created lazily once GL is ready).
        self._shadow_fbo = None
        self._shadow_cubemaps = []          # texture ids, one cube-map per shadow slot
        self._init_decals()
        #: Effects also write depth this frame (see _write_effect_depth), and
        #: the nearest few's centre and size, for the water pass to reflect.
        self._effects_write_depth = False
        self._reflected_effects = None
        #: (publication key, TerrainGL): the GL side of the terrain drawn.
        self._terrain_gl_state = None
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

        self._proj_ptr = None
        self._view_ptr = None
        self._water_surface_vbo = None
        self._water_surface_ebo = None
        self._water_surface_index_count = 0

        # Portal specific GL resources (initialised later)
        self._portal_mask_shader = None
        self._portal_rim_shader = None
        self._portal_quad_vao = None
        self._portal_quad_vbo = None
        self._portal_fade_shader = None
        #: The colour behind a fading portal's aperture, copied before its
        #: view is drawn (see PortalsMixin._capture_behind_portal).
        self._portal_behind_texture = 0
        self._portal_behind_size = (0, 0)
        self._portal_gl_ready = False
        # Failing portal frames in a row, when the last was printed, and
        # whether they have turned portals off for the session (portals.py).
        self._portal_failures = 0
        self._portal_failure_reported = None
        self._portals_disabled = False
        # Set at the start of each view by render_scene (see there).
        self._frame_viewport = None
        self._frame_read_fbo = None
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
        # Compile common shaders (simple, sprite, depth_cube, water, glass, fog, terrain)
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
        from engine.asset_archives import asset_exists
        if asset_exists(explosion_path):
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

        # Animated FIRE/ORB texture sets are decoded when a map first holds
        # an effect that uses them (ensure_effect_frames), not here.

        # Create VAOs after shaders are ready
        if not self._shader_init_failed:
            self.vaos['cube'] = self._create_cube_vao()
            self.vaos['water_surface'] = self._create_water_surface_vao()
            self.vaos['sprite'] = self._create_sprite_vao()
            self.vaos['grid'] = None
            self._create_gizmo_buffers()
            self.noise_texture_id = self._load_3d_texture('assets/noise_3d.bin')
            self.load_texture('default.png', 'textures')
            self.load_texture('caulk', 'textures')
            self._init_portal_gl()
            self._init_shadow_resources()
            self._init_distance()
            self._init_occlusion(config)

    def _kept_distance_cull(self, table, slots, cx, cz, limit_sq):
        """:meth:`_distance_cull_slots`, its result kept per view (M4): the
        same read-only array while the visible set (the same immutable
        array), the camera's position, the limit and the table's transforms
        stand; a result equal to the last keeps the last array.

        Kept per view *and table*: the two double-buffered render tables take
        turns frame by frame, so one entry per view was replaced every frame
        and a still frame in Play handed every pass after it a new array
        (the classify memo then compared all its rows)."""
        key = (self._instance_view, id(table))
        held = self._distance_culls.get(key)
        inputs = (cx, cz, limit_sq, table.generation, table.transform_version)
        if (held is not None and held[0] is slots and not slots.flags.writeable
                and held[1]() is table and held[2] == inputs):
            return held[3]
        culled = self._distance_cull_slots(table, slots, cx, cz, limit_sq)
        if held is not None and held[1]() is table and np.array_equal(held[3], culled):
            culled = held[3]
        else:
            culled.flags.writeable = False
        if held is None:
            _forget_dead_tables(self._distance_culls, 1)
        self._distance_culls[key] = (slots, weakref.ref(table), inputs, culled)
        return culled

    def set_background_image(self, path):
        """Use *path* as the camera-locked 360-degree panorama.

        Paths may name loose project assets, members of a mounted .fiopak, or
        absolute files selected outside the project. Upload is deferred until
        render_scene, while this renderer owns the GL context.
        """
        path = str(path or "").strip()
        if path == self._background_image_path:
            return
        self._background_image_path = path
        self._background_failed_path = None
        if self._background_image_texture:
            gl.glDeleteTextures([self._background_image_texture])
            self._background_image_texture = 0
        if self._background_cube_texture:
            gl.glDeleteTextures([self._background_cube_texture])
            self._background_cube_texture = 0

    def _load_background_image(self):
        """Decode and upload the selected panorama once for this renderer."""
        path = self._background_image_path
        if not path or path == self._background_failed_path:
            return False
        # The path comes from the map. Outside files are allowed (the user
        # can pick one), but a UNC or device path is never probed: on Windows
        # that alone opens an SMB connection to the named host.
        normal = path.replace('\\', '/')
        if normal[:2] == '//' or '://' in normal:
            from engine.asset_paths import warn_refused
            warn_refused(path, 'sky image')
            self._background_failed_path = path
            return False
        from engine.asset_archives import asset_source
        source = asset_source(path)
        if source is None:
            project_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), os.pardir,
                             os.pardir, os.pardir)
            )
            source = asset_source(os.path.join(project_root, path))
        if source is None:
            print(f"[Renderer] Background panorama not found: {path}")
            self._background_failed_path = path
            return False

        texture = 0
        try:
            from PIL import Image
            with Image.open(source) as image:
                rgba = image.convert("RGBA")
            texture = int(gl.glGenTextures(1))
            gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
            try:
                gl.glTexImage2D(
                    gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, rgba.width, rgba.height, 0,
                    gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, rgba.tobytes())
            finally:
                gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
            self._background_cube_texture = self._build_sky_cube(texture)
            self._background_image_texture = texture
            return True
        except Exception as exc:
            if texture:
                gl.glDeleteTextures([texture])
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
            print(f"[Renderer] Could not load background panorama '{path}': {exc}")
            self._background_failed_path = path
            return False

    def _build_sky_cube(self, panorama):
        """The equirectangular *panorama* (GL texture) drawn into a cube map
        of ``sky_cubemap_size`` texels a face, once, for the sky pass to read
        with no trigonometry. Six full-screen draws into an offscreen
        framebuffer; the framebuffer, viewport and the state they touch are
        put back, and nothing is left allocated if it fails."""
        from OpenGL.GL.shaders import compileProgram, compileShader
        size = self.sky_cubemap_size
        if not self._sky_cube_program:
            self._sky_cube_program = compileProgram(
                compileShader(shaders.DEFAULT_SHADERS['sky_cube.vert'], gl.GL_VERTEX_SHADER),
                compileShader(shaders.DEFAULT_SHADERS['sky_cube.frag'], gl.GL_FRAGMENT_SHADER),
                validate=False)
        program = self._sky_cube_program
        previous_fbo = int(gl.glGetIntegerv(gl.GL_FRAMEBUFFER_BINDING))
        previous_viewport = [int(v) for v in gl.glGetIntegerv(gl.GL_VIEWPORT)]
        enabled = {cap: bool(gl.glIsEnabled(cap)) for cap in (
            gl.GL_DEPTH_TEST, gl.GL_BLEND, gl.GL_CULL_FACE, gl.GL_SCISSOR_TEST,
            gl.GL_STENCIL_TEST)}
        cube = int(gl.glGenTextures(1))
        fbo = int(gl.glGenFramebuffers(1))
        vao = int(gl.glGenVertexArrays(1))
        try:
            gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, cube)
            for name, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR),
                                (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                                (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                                (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE),
                                (gl.GL_TEXTURE_WRAP_R, gl.GL_CLAMP_TO_EDGE)):
                gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, name, value)
            for face in range(6):
                gl.glTexImage2D(gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + face, 0, gl.GL_RGBA8,
                                size, size, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
            gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
            gl.glViewport(0, 0, size, size)
            for cap in enabled:
                gl.glDisable(cap)
            gl.glUseProgram(program)
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(gl.GL_TEXTURE_2D, panorama)
            gl.glUniform1i(gl.glGetUniformLocation(program, 'u_panorama'), 0)
            face_loc = gl.glGetUniformLocation(program, 'u_face')
            gl.glBindVertexArray(vao)
            for face in range(6):
                gl.glFramebufferTexture2D(
                    gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                    gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + face, cube, 0)
                gl.glUniform1i(face_loc, face)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        except Exception:
            gl.glDeleteTextures([cube])
            raise
        finally:
            gl.glBindVertexArray(0)
            gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
            gl.glUseProgram(0)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, previous_fbo)
            gl.glViewport(*previous_viewport)
            for cap, on in enabled.items():
                if on:
                    gl.glEnable(cap)
            gl.glDeleteFramebuffers(1, [fbo])
            gl.glDeleteVertexArrays(1, [vao])
        return cube

    # -- reflection probes ----------------------------------------------

    def invalidate_reflection_probes(self):
        """Retake every probe's snapshot on the next frame (the map loaded,
        Play started: what is around the probes may have changed)."""
        for texture in self._probe_textures.values():
            if texture:
                gl.glDeleteTextures([texture])
        self._probe_textures = {}
        self._probe_frame = None

    def _probe_slots(self, table):
        """The table's ReflectionProbe rows, found once per table generation."""
        from engine.entity_table import ENT_PROBE
        cached_table, generation, count, slots = self._probe_slot_cache
        if (cached_table is not table or generation != table.generation
                or count != table.count):
            slots = np.flatnonzero(
                table.class_bits[:table.count] & ENT_PROBE).astype(np.int32)
            self._probe_slot_cache = (table, table.generation, table.count, slots)
        return slots

    def _update_reflection_probes(self, config):
        """Before the frame: a snapshot for every probe in the map, taken
        the first time it is seen and again only when it moves or its radius
        changes. ``_probe_frame`` is what this frame's water may use."""
        table = config.get('entity_table')
        slots = self._probe_slots(table) if table is not None else ()
        if not len(slots):
            self._probe_frame = None
            if self._probe_textures:
                self.invalidate_reflection_probes()
            return
        positions = np.array(table.pos[slots], dtype=np.float64)
        radii = np.array(table.probe_radius[slots], dtype=np.float64)
        keys = [(tuple(np.round(p, 2).tolist()), round(float(r), 2))
                for p, r in zip(positions, radii)]
        wanted = set(keys)
        for key in [k for k in self._probe_textures if k not in wanted]:
            texture = self._probe_textures.pop(key)
            if texture:
                gl.glDeleteTextures([texture])
        self._probe_frame = None
        for key, position in zip(keys, positions):
            if key not in self._probe_textures:
                self._probe_textures[key] = self._capture_reflection_probe(
                    position, config)
        textures = np.array([self._probe_textures[key] for key in keys],
                            dtype=np.int64)
        self._probe_frame = (positions, radii, textures)

    def _capture_reflection_probe(self, position, config):
        """The 360-degree snapshot from *position*: six 90-degree views of
        the map into a cube map, drawn out as an equirectangular texture
        (``probe_equirect.frag``) that water reads like the sky panorama.

        Drawn as Play draws it -- no grid, no editor icons -- without what
        moves (monsters, effects) or portal views: it is not retaken every
        frame. Returns the texture, or 0 if it could not be made."""
        from OpenGL.GL.shaders import compileProgram, compileShader
        from engine.entity_table import ENT_EFFECT, ENT_MONSTER, ENT_PROBE
        size = self.PROBE_FACE_SIZE
        table = config['entity_table']
        things = np.asarray(config['visible_thing_slots'], dtype=np.int32)
        still = (table.class_bits[things] & (ENT_MONSTER | ENT_EFFECT | ENT_PROBE)) == 0
        capture = dict(config, play_mode=True, grid_visible=False,
                       show_sprites_in_play_mode=False,
                       visible_thing_slots=things[still])
        eye = glm.vec3(*[float(v) for v in position])
        far = float(getattr(self.view_distance, 'far_plane', 8000.0))
        projection = glm.perspective(glm.radians(90.0), 1.0, 1.0, far)
        faces = ((glm.vec3(1, 0, 0), glm.vec3(0, -1, 0)),
                 (glm.vec3(-1, 0, 0), glm.vec3(0, -1, 0)),
                 (glm.vec3(0, 1, 0), glm.vec3(0, 0, 1)),
                 (glm.vec3(0, -1, 0), glm.vec3(0, 0, -1)),
                 (glm.vec3(0, 0, 1), glm.vec3(0, -1, 0)),
                 (glm.vec3(0, 0, -1), glm.vec3(0, -1, 0)))
        previous_fbo = int(gl.glGetIntegerv(gl.GL_FRAMEBUFFER_BINDING))
        previous_viewport = [int(v) for v in gl.glGetIntegerv(gl.GL_VIEWPORT)]
        saved = (self._instance_view, self.occlusion_culling, self._portal_gl_ready)
        cube = int(gl.glGenTextures(1))
        depth = int(gl.glGenRenderbuffers(1))
        fbo = int(gl.glGenFramebuffers(1))
        equirect = 0
        try:
            gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, cube)
            for name, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR),
                                (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                                (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                                (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE),
                                (gl.GL_TEXTURE_WRAP_R, gl.GL_CLAMP_TO_EDGE)):
                gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, name, value)
            for face in range(6):
                gl.glTexImage2D(gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + face, 0, gl.GL_RGBA8,
                                size, size, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
            gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
            gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, depth)
            gl.glRenderbufferStorage(gl.GL_RENDERBUFFER, gl.GL_DEPTH24_STENCIL8, size, size)
            gl.glBindRenderbuffer(gl.GL_RENDERBUFFER, 0)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
            gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_STENCIL_ATTACHMENT,
                                         gl.GL_RENDERBUFFER, depth)
            # Its own instance streams, no occlusion probes and no portal
            # views: the main view's state is left as it was.
            self._probe_capture_pass = True
            self._instance_view = ('reflection probe',)
            self.occlusion_culling = False
            self._portal_gl_ready = False
            for face, (forward, up) in enumerate(faces):
                gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                          gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + face, cube, 0)
                gl.glViewport(0, 0, size, size)
                view = glm.lookAt(eye, eye + forward, up)
                self.render_scene(projection, view, eye, None, capture,
                                  brush_slots=capture.get('all_brush_slots'))
            equirect = self._probe_cube_to_equirect(cube, size, fbo,
                                                     compileProgram, compileShader)
        except Exception as exc:
            print(f"[Renderer] Reflection probe snapshot failed: {exc}")
            if equirect:
                gl.glDeleteTextures([equirect])
            equirect = 0
        finally:
            self._probe_capture_pass = False
            self._instance_view, self.occlusion_culling, self._portal_gl_ready = saved
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, previous_fbo)
            gl.glViewport(*previous_viewport)
            gl.glDeleteFramebuffers(1, [fbo])
            gl.glDeleteRenderbuffers(1, [depth])
            gl.glDeleteTextures([cube])
        return equirect

    def _probe_cube_to_equirect(self, cube, size, fbo, compileProgram, compileShader):
        """*cube* (the bound *fbo* free to reuse) drawn out as a 4*size by
        2*size equirectangular texture, wrapped like the sky panorama."""
        if not self._probe_equirect_program:
            self._probe_equirect_program = compileProgram(
                compileShader(shaders.DEFAULT_SHADERS['sky_cube.vert'], gl.GL_VERTEX_SHADER),
                compileShader(shaders.DEFAULT_SHADERS['probe_equirect.frag'], gl.GL_FRAGMENT_SHADER),
                validate=False)
        width, height = 4 * size, 2 * size
        texture = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, texture)
        for name, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR),
                            (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                            (gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT),
                            (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)):
            gl.glTexParameteri(gl.GL_TEXTURE_2D, name, value)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height, 0,
                        gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        enabled = {cap: bool(gl.glIsEnabled(cap)) for cap in (
            gl.GL_DEPTH_TEST, gl.GL_BLEND, gl.GL_CULL_FACE, gl.GL_SCISSOR_TEST,
            gl.GL_STENCIL_TEST)}
        vao = int(gl.glGenVertexArrays(1))
        try:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                      gl.GL_TEXTURE_2D, texture, 0)
            gl.glFramebufferRenderbuffer(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_STENCIL_ATTACHMENT,
                                         gl.GL_RENDERBUFFER, 0)
            gl.glViewport(0, 0, width, height)
            for cap in enabled:
                gl.glDisable(cap)
            gl.glUseProgram(self._probe_equirect_program)
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, cube)
            gl.glUniform1i(gl.glGetUniformLocation(self._probe_equirect_program, 'u_cube'), 0)
            gl.glBindVertexArray(vao)
            gl.glEnable(gl.GL_TEXTURE_CUBE_MAP_SEAMLESS)
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
            gl.glDisable(gl.GL_TEXTURE_CUBE_MAP_SEAMLESS)
        finally:
            gl.glBindVertexArray(0)
            gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
            gl.glUseProgram(0)
            for cap, on in enabled.items():
                if on:
                    gl.glEnable(cap)
            gl.glDeleteVertexArrays(1, [vao])
        return texture

    def _background_panorama_texture(self):
        """The sky panorama's GL texture, loaded on first use; 0 without one."""
        if not self._background_image_path:
            return 0
        if not self._background_image_texture:
            self._load_background_image()
        return self._background_image_texture

    def _draw_background_image(self, projection, view, behind=False):
        """Draw the panorama at infinite distance: over the whole frame, or,
        *behind*, only where nothing has been drawn yet (depth still 1.0)."""
        if not self._background_image_path or self._portal_scene_pass:
            return
        if not self._background_image_texture and not self._load_background_image():
            return
        if not self._background_image_program:
            from OpenGL.GL.shaders import compileProgram, compileShader
            vertex = compileShader(
                shaders.DEFAULT_SHADERS["void_background.vert"], gl.GL_VERTEX_SHADER)
            fragment = compileShader(
                shaders.DEFAULT_SHADERS["void_background.frag"], gl.GL_FRAGMENT_SHADER)
            self._background_image_program = compileProgram(
                vertex, fragment, validate=False)
            self._background_image_vao = int(gl.glGenVertexArrays(1))
            program = self._background_image_program
            self._background_image_uniforms = (
                gl.glGetUniformLocation(program, "u_inverse_projection"),
                gl.glGetUniformLocation(program, "u_inverse_view"),
                gl.glGetUniformLocation(program, "u_sky"),
            )

        inverse_projection = glm.inverse(projection)
        inverse_view = glm.inverse(view)
        inv_projection_loc, inv_view_loc, sky_loc = self._background_image_uniforms
        if behind:
            # The triangle is at the far plane: depth 1.0, as cleared.
            gl.glEnable(gl.GL_DEPTH_TEST)
            gl.glDepthFunc(gl.GL_LEQUAL)
        else:
            gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glDepthMask(gl.GL_FALSE)
        gl.glDisable(gl.GL_BLEND)
        gl.glUseProgram(self._background_image_program)
        gl.glUniformMatrix4fv(
            inv_projection_loc, 1, gl.GL_FALSE, glm.value_ptr(inverse_projection))
        gl.glUniformMatrix4fv(
            inv_view_loc, 1, gl.GL_FALSE, glm.value_ptr(inverse_view))
        gl.glUniform1i(sky_loc, 0)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, self._background_cube_texture)
        gl.glBindVertexArray(self._background_image_vao)
        # Filtered across the cube's face edges for this draw only: it is
        # global state, and the shadow cube maps are left as they were.
        gl.glEnable(gl.GL_TEXTURE_CUBE_MAP_SEAMLESS)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl.glDisable(gl.GL_TEXTURE_CUBE_MAP_SEAMLESS)
        gl.glBindVertexArray(0)
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, 0)
        gl.glUseProgram(0)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LESS)

    def _cleanup_background_image(self):
        """Release panorama GL objects with the rest of this renderer."""
        if self._background_image_texture:
            gl.glDeleteTextures([self._background_image_texture])
        if self._background_cube_texture:
            gl.glDeleteTextures([self._background_cube_texture])
        if self._background_image_vao:
            gl.glDeleteVertexArrays(1, [self._background_image_vao])
        if self._background_image_program:
            gl.glDeleteProgram(self._background_image_program)
        if self._sky_cube_program:
            gl.glDeleteProgram(self._sky_cube_program)
        if self._probe_equirect_program:
            gl.glDeleteProgram(self._probe_equirect_program)
        self.invalidate_reflection_probes()
        self._background_image_texture = 0
        self._background_cube_texture = 0
        self._background_image_vao = 0
        self._background_image_program = 0
        self._sky_cube_program = 0
        self._probe_equirect_program = 0
        self._background_image_uniforms = None

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
        if not self._probe_capture_pass and not self._portal_scene_pass:
            self._update_reflection_probes(config)
        if not self._portal_scene_pass:
            # The view's viewport and read framebuffer, asked once: the
            # passes read these instead of asking again. A portal view
            # shares its main view's.
            self._frame_viewport = tuple(
                int(v) for v in gl.glGetIntegerv(gl.GL_VIEWPORT))
            self._frame_read_fbo = int(
                gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
        current_mode = config.get('render_mode', RENDER_MODE_LIT)
        # 'Wireframe': geometry drawn as lines for depth, then coloured by
        # distance in one pass over the frame (draw_distance_pass).
        look = self._distance_look(config, current_mode)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LESS)
        if look:
            gl.glClearColor(0.0, 0.0, 0.0, 1.0)     # what was not hit is black
        if clear:
            # FIX: Don't clear color when rendering a portal virtual view
            if getattr(self, '_portal_scene_pass', False):
                gl.glClear(gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)
            else:
                gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT | gl.GL_STENCIL_BUFFER_BIT)
        # The sky is drawn behind the opaque world, once the brushes and the
        # terrain have hidden most of it (below, before the models), so a
        # pixel they cover never shades it. It is drawn first, as it always
        # was, in a frame that may draw portal views (their empty parts show
        # the sky already on screen), in the line and point render modes (the
        # frame's polygon mode is theirs by then) and in the Wireframe look
        # (draw_distance_pass leaves the sky where nothing was hit).
        portal_table = config.get('entity_table')
        sky_behind = not (
            look or current_mode not in (RENDER_MODE_LIT, RENDER_MODE_UNLIT)
            or (config.get('play_mode', False) and self._portal_gl_ready
                and len(getattr(portal_table, 'portal_slots', ()))))
        if not sky_behind:
            self._draw_background_image(projection, view)
        self._proj_ptr = glm.value_ptr(projection)
        self._view_ptr = glm.value_ptr(view)
        # Every fog calculation this frame measures from here. Cached because
        # the unlit passes (sprites) and the terrain are not handed a camera.
        self._frame_camera_pos = self._camera_xyz(camera_pos)
        self.render_stats.reset()
        # Set only by this frame's decal pass (and spent by the particles):
        # a portal view drawn before it must not read last frame's copy.
        self._scene_depth_current = False
        self.render_stats.total_brushes = len(config.get('all_brush_slots', ()))
        if not getattr(self, '_portal_scene_pass', False):
            self._stream_calls.clear()
        self._begin_geo_frame()
        self._frame_lights_uploaded.clear()
        self._light_ubo_key = None
        self._light_ubo_lights = None
        # Texture units are not assumed bound from the last frame.
        self.shadow_units_changed()
        self._light_ubo_bound = False
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
        if not look:            # Wireframe draws it after
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
            slots = self._kept_distance_cull(table, slots, cx, cz,
                                             self.view_distance.distance_sq)
        # GPU-verified hidden cells drop out here, before the passes classify,
        # sort and pack the surviving rows (renderer/forward/occlusion.py).
        slots = self._occlusion_filter_slots(table, slots, config, camera_pos,
                                             view)
        # Kept while the visible rows and their classification are unchanged
        # (a copy: the depth sort below replaces some of its arrays).
        groups = dict(self._pass_memo(
            ('classify', self._instance_view), table, slots,
            (bool(config.get('play_mode', False)),), (),
            lambda: self._classify_brush_slots(table, slots, config)))
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
        self._warm_sprite_layers(etable, thing_slots, config.get('play_mode', False),
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
        # Every entity pass draws only rows this view publishes as visible
        # (the editor's filters leave rows out); lights still light.
        drawn = np.zeros(etable.count, dtype=bool)
        drawn[thing_slots] = True

        def drawn_rows(slots):
            return slots[drawn[slots]]

        effect_slots = drawn_rows(etable.effect_slots)
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

        if look:
            # Everything opaque up to draw_distance_pass is drawn for its depth
            # only, as lines. Water and glass are brushes like any other here.
            gl.glColorMask(gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE, gl.GL_FALSE)
            opaque_brushes = np.concatenate(
                (opaque_brushes, glow_brushes, water_brushes, glass_brushes))
            glow_brushes = water_brushes = glass_brushes = fog_volumes = (
                np.empty(0, dtype=np.int32))
        if look == 'Wireframe':
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)   # terrain
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
                # Contains its own failures: the state it touched is put back
                # and an error is reported rate-limited (portals.py).
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
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_BLEND)
        brush_display_mode = config.get('brush_display_mode', 'Textured')
        #: 'Solid Lit': nothing textured -- brushes, models and terrain are
        #: shaded in their colours, and decals are not drawn.
        self._solid_look = (current_mode == RENDER_MODE_LIT
                            and brush_display_mode == 'Solid Lit')
        # Filled modes only: in wireframe and vertex modes the far edges and
        # corners are part of what the editor shows.
        self._opaque_cull_pass = (self.cull_opaque_back_faces and current_mode
                                  in (RENDER_MODE_LIT, RENDER_MODE_UNLIT))
        try:
            if current_mode == RENDER_MODE_UNLIT:
                self.draw_textured_brushes_optimized(projection, view, camera_pos, textured_opaque, lights, config, _tbl)
                self.draw_lit_brushes_optimized(projection, view, camera_pos, solid_opaque, lights, config, table=_tbl)
            elif current_mode == RENDER_MODE_LIT:
                # 'Solid Lit' shades every brush with its colour, no textures;
                # 'Wireframe' draws each brush's true edges.
                if look == 'Wireframe':
                    self.draw_brush_edges(projection, view, opaque_brushes, config, _tbl)
                elif brush_display_mode in ('Textured', 'Overlay'):
                    self.draw_textured_brushes_optimized(projection, view, camera_pos, textured_opaque, lights, config, _tbl)
                    self.draw_lit_brushes_optimized(projection, view, camera_pos, solid_opaque, lights, config, table=_tbl)
                else:
                    # 'Solid Lit': every brush shaded in its colour, no
                    # textures at all (a nodraw face included); water too --
                    # a shaded brush drawn with the solid ones (see the
                    # water pass below).
                    solid = opaque_brushes
                    if len(water_brushes):
                        solid = np.concatenate((solid, water_brushes))
                    self.draw_lit_brushes_optimized(projection, view, camera_pos, solid, lights, config, table=_tbl)
            else:
                self.draw_lit_brushes_optimized(projection, view, camera_pos, opaque_brushes, lights, config, table=_tbl)
        finally:
            self._opaque_cull_pass = False
        if len(glow_brushes):
            self.draw_glow_brushes(projection, view, camera_pos, glow_brushes, lights, config, table=_tbl)
        if sky_behind:
            # Before the models: their clear materials blend over it without
            # writing depth (_model_material).
            self._draw_background_image(projection, view, behind=True)
        fading_model_slots = np.empty(0, dtype=np.int32)
        if len(numeric_model_slots):
            if not (self.shaders.get('lit_instanced')
                    or self.shaders.get('textured_instanced')):
                raise RuntimeError(
                    "Fio 2.5 requires instanced model shaders for dense entity rendering")
            fading_model_mask = etable.render_alpha[numeric_model_slots] < 1.0
            if look:
                fading_model_mask[:] = False     # distance colour has no alpha
            opaque_model_slots = numeric_model_slots[~fading_model_mask]
            fading_model_slots = numeric_model_slots[fading_model_mask]
            # The private spawn sign has clear acrylic. Draw it after other
            # opaque models so they are visible through its panel.
            editor_mask = (etable.class_bits[opaque_model_slots]
                           & entity_projection.ENT_EDITOR_MODEL) != 0
            editor_model_slots = opaque_model_slots[editor_mask]
            opaque_model_slots = opaque_model_slots[~editor_mask]
            if len(editor_model_slots):
                # Not from inside: the camera loads at the spawn, and the
                # sign's panels would cover the whole view.
                inside = editor_models_around(
                    etable.pos[editor_model_slots],
                    (camera_pos[0], camera_pos[1], camera_pos[2]),
                    [size / 2.0 for size in PLAYER_HULL_SIZE])
                editor_model_slots = editor_model_slots[~inside]
            if len(opaque_model_slots):
                if look == 'Wireframe':
                    gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_LINE)
                self.draw_models_instanced(
                    projection, view, camera_pos, etable, opaque_model_slots,
                    lights, config)
            if len(editor_model_slots):
                self.draw_models_instanced(
                    projection, view, camera_pos, etable, editor_model_slots,
                    lights, config)
        if brush_display_mode == 'Overlay' and current_mode == RENDER_MODE_LIT:
            # The textured, lit frame with its triangles drawn over it.
            self.draw_triangle_overlay(
                projection, view,
                np.concatenate((opaque_brushes, glow_brushes)), _tbl)
        if look:
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
            gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
            if not self.draw_distance_pass(projection, view, camera_pos):
                # No depth to read back: the brushes' edges in their colours.
                self.draw_brush_edges(projection, view, opaque_brushes, config, _tbl)
            if look == 'Wireframe':
                self.draw_grid(projection, view, self.grid_indices_count,
                               config.get('play_mode', False), config.get('grid_visible', True))
        if not config.get('play_mode', False):
            self.draw_path_node_cubes(projection, view, etable,
                                      drawn_rows(etable.path_node_slots))
        if etable is not None:
            self.draw_portal_wireframes(
                projection, view, etable, drawn_rows(etable.portal_slots),
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
        if etable is not None and len(etable.effect_slots):
            self.ensure_effect_frames(etable)
        # Projected decals (blood): onto the opaque world just drawn, before
        # anything blended, so they lie under water and glass. A depth copy
        # they make is still this frame's opaque depth when the particles
        # draw (nothing between writes depth), so those reuse it.
        copies = self._scene_depth_copies
        if not self._solid_look:            # 'Solid Lit': no painted images
            self.draw_decals(projection, view, camera_pos, config.get('decals'))
        self._scene_depth_current = self._scene_depth_copies != copies
        # High-quality water reflects what is in the depth buffer: in a frame
        # that has some, effects write their solid part there too.
        self._reflected_effects = None
        self._effects_write_depth = bool(
            current_mode == RENDER_MODE_LIT and len(water_brushes)
            and getattr(self, 'water_quality', 'expensive') == 'expensive'
            and getattr(self, 'water_enabled', True)
            and bool(_tbl.water_high_quality[water_brushes].any()))
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
        if current_mode == RENDER_MODE_LIT and brush_display_mode == 'Solid Lit':
            pass        # water was drawn as a solid, shaded brush (above)
        elif current_mode == RENDER_MODE_LIT:
            self.draw_water_brushes(
                projection, view, camera_pos, water_brushes, lights, config,
                table=_tbl)
            self.draw_glass_brushes(projection, view, camera_pos, glass_brushes, lights, config,
                                     table=_tbl)
            self.draw_fog_volumes(projection, view, camera_pos, fog_volumes, lights, config,
                                  table=_tbl)
        # The frame's opaque depth is finished: poll the chained occlusion
        # probes and issue this frame's, against exactly the geometry the
        # brushes were hidden behind (renderer/forward/occlusion.py).
        self._occlusion_end_of_frame(projection, view, camera_pos, config)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        if primary_selection:
            self._draw_selection_overlays(
                projection, view, primary_selection, _tbl)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glUseProgram(0)

    def cleanup(self):
        """Release ForwardRenderer's own GL resources, then RendererCore's."""
        self._cleanup_background_image()
        self._cleanup_occlusion()
        if self._sprite_layers is not None:
            self._sprite_layers.cleanup()
            self._sprite_layers = None
        for attr in ('_glass_scene_texture', '_water_depth_texture'):
            tex = getattr(self, attr, 0)
            if tex:
                gl.glDeleteTextures([tex])
                setattr(self, attr, 0)
        self._glass_scene_size = (0, 0)
        self._water_depth_size = (0, 0)
        # Shadow resources. These are owned by this renderer alone and nothing
        # outside it holds their names, so they have to be released here or a
        # renderer rebuild (a render-mode or shadow-quality change) strands the
        # whole cube-map pool -- MAX_SHADOW_LIGHTS cube-maps at shadow_map_size,
        # tens of megabytes of VRAM, every time.
        try:
            self._cleanup_decals()
        except Exception:
            pass
        if self._terrain_gl_state is not None:
            try:
                self._terrain_gl_state[1].cleanup()
            except Exception:
                pass
            self._terrain_gl_state = None
        if self._shadow_cubemaps:
            try:
                gl.glDeleteTextures(self._shadow_cubemaps)
                self.shadow_units_changed()
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
        if self._water_surface_vbo:
            gl.glDeleteBuffers(1, [self._water_surface_vbo])
        if self._water_surface_ebo:
            gl.glDeleteBuffers(1, [self._water_surface_ebo])
        self._release_model_instance_streams()
        if self._light_ubo:
            gl.glDeleteBuffers(1, [self._light_ubo])
            self._light_ubo = None
            self._light_ubo_capacity = 0
            self._light_ubo_key = None
            self._light_ubo_lights = None
            self._light_ubo_bound = False
        if self._light_cookies is not None:
            self._light_cookies.close()
            self._light_cookies = None
        cookie_cache = self.__dict__.get('_cookie_gl_cache')
        if cookie_cache:
            from engine.light_cookies import release_placeholder
            release_placeholder(gl, cookie_cache)
        # Instance streams: one buffer and one vertex array per pass.
        self._release_brush_streams()
        self._slot_transform_caches.clear()
        self._pass_memos.clear()
        self._selected_payloads.clear()
        self._distance_culls.clear()
        self.__dict__.pop('_occlusion_kept', None)
        self.__dict__.pop('_portal_culls', None)
        for vao in (self._sprite_instance_vao, self._effect_instance_vao,
                    self._particle_instance_vao):
            if vao:
                gl.glDeleteVertexArrays(1, [vao])
        for vbo in (self._sprite_instance_vbo, self._effect_instance_vbo,
                    self._particle_instance_vbo):
            if vbo:
                gl.glDeleteBuffers(1, [vbo])
        self._sprite_instance_vao = None
        self._effect_instance_vao = None
        self._particle_instance_vao = None
        self._sprite_instance_vbo = None
        self._effect_instance_vbo = None
        self._effect_instance_capacity = 0
        self._explosion_uploaded = None
        self._particle_instance_vbo = None
        self._particle_instance_capacity = 0
        self._particle_uploaded = None
        # Portal resources. The mask and rim programs are not in ``shaders``.
        if self._portal_quad_vao:
            gl.glDeleteVertexArrays(1, [self._portal_quad_vao])
        if self._portal_quad_vbo:
            gl.glDeleteBuffers(1, [self._portal_quad_vbo])
        for program in (self._portal_mask_shader, self._portal_rim_shader,
                        self._portal_fade_shader):
            if program:
                gl.glDeleteProgram(program)
        self._portal_mask_shader = self._portal_rim_shader = None
        self._portal_fade_shader = None
        if self._portal_behind_texture:
            gl.glDeleteTextures([self._portal_behind_texture])
        self._portal_behind_texture = 0
        self._portal_behind_size = (0, 0)
        self._portal_gl_ready = False
        # The 3D noise texture is not in the texture cache RendererCore frees.
        if self.noise_texture_id:
            gl.glDeleteTextures([self.noise_texture_id])
            self.noise_texture_id = 0
        super().cleanup()
