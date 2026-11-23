"""ForwardRenderer's lighting model.

The std140 light UBO and per-shader light caps, the fog/ambient uniform
upload, and omnidirectional point-light shadows as depth cube-maps.
"""

import glm
import numpy as np
import OpenGL.GL as gl

from engine import render_table
from engine import shaders
from engine.render_keys import KeyLayout
from ..core.diagnostics import timed_pass
from ..core.uniform_rows import uniform_rows


#: Light-array capacity of each lighting shader, so the renderer can never set
#: ``active_lights`` higher than the shader has room for.  Anything absent from
#: this map holds the full ``Renderer.MAX_LIGHTS``.
_SHADER_LIGHT_CAPS = {
    'water': shaders.MAX_LIGHTS_WATER,
    'terrain': shaders.MAX_LIGHTS_TERRAIN,
}


def _split_casters(table, slots):
    """``(box slots, convex geometry slots)`` of the caster *slots*."""
    geometry = (table.class_bits[slots] & render_table.CLASS_HAS_GEOMETRY) != 0
    return slots[~geometry], slots[geometry]


class LightingMixin:
    """ForwardRenderer's lights and shadows."""

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

    # --- Depth cube-map shadow mapping (omnidirectional point-light shadows) ---
    MAX_SHADOW_LIGHTS = shaders.MAX_SHADOW_LIGHTS  # authoritative point-light shadow budget

    #: Per-face resolution of each depth cube-map ([Renderer] shadow_map_size).
    #: A cube-map is only sized when a shadow-casting light takes its slot
    #: (_ensure_shadow_cube_size), so this costs 6 * size^2 depth texels per
    #: light actually casting shadows -- about 24 MB at 1024 -- not per slot.
    SHADOW_MAP_SIZE = 1024

    SHADOW_TEXTURE_UNIT_BASE = 4   # shadow cube-maps bind to units 4..(4+MAX_SHADOW_LIGHTS-1)

    #: Uniform names of the shared distance-fog / global-ambient block
    #: (engine.shaders.FOG_GLSL). Preloaded for every shader that splices it in,
    #: and uploaded together by :meth:`_upload_env_uniforms`.
    ENV_UNIFORMS = ('uFogEnabled', 'uFogColor', 'uFogStart', 'uFogEnd',
                    'uFogDensity', 'uFogCamPos', 'uAmbient')

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
                                                  'lit_brush_instanced', 'geo_textured',
                                                  'geo_batched'):
            cap = min(cap, shaders.MAX_LIGHTS_ARM)
        return cap

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
        # Sent when they change, per program (M4): the values are compared
        # every frame, so a change still reaches the next draw. The terrain's
        # program belongs to the terrain, which may set them itself.
        # The camera moves nearly every frame, the rest almost never: they
        # are compared separately, so a moving camera re-sends one uniform
        # per program rather than all seven.
        values = (vd.fog_enabled, tuple(vd.fog_color), start, end,
                  vd.fog_density, tuple(vd.ambient))
        cam_values = (cam[0], cam[1], cam[2])
        if shader_name != 'terrain':
            previous = uniforms.env
            if previous is not None and previous[0] == values:
                if previous[1] != cam_values:
                    uniforms.env = (values, cam_values)
                    gl.glUniform3f(uniforms['uFogCamPos'], *cam_values)
                return
            uniforms.env = (values, cam_values)
        gl.glUniform1i(uniforms['uFogEnabled'], 1 if vd.fog_enabled else 0)
        gl.glUniform3f(uniforms['uFogColor'], *vd.fog_color)
        gl.glUniform1f(uniforms['uFogStart'], start)
        gl.glUniform1f(uniforms['uFogEnd'], end)
        gl.glUniform1f(uniforms['uFogDensity'], vd.fog_density)
        gl.glUniform3f(uniforms['uFogCamPos'], cam[0], cam[1], cam[2])
        gl.glUniform3f(uniforms['uAmbient'], *vd.ambient)

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
                     'brush_instanced', 'lit_brush_instanced', 'geo_textured',
                     'geo_batched', 'water', 'terrain'):
            self._configure_light_ubo_program(name)
        self._ensure_light_ubo(self.MAX_LIGHTS)

    def _ensure_light_ubo(self, capacity):
        """Make the shared light UBO hold *capacity* lights, bound to its slot.

        The indexed binding is context state and names the buffer object, not
        its store: respecifying the store (growth here, orphaning in
        :meth:`_upload_light_ubo`) leaves it in place. So the buffer is bound
        to ``LIGHT_UBO_BINDING`` once per frame at most -- the frame start
        clears ``_light_ubo_bound`` in case anything else touched the slot --
        rather than once or twice in every lit pass, which re-issued the same
        binding three times per shader per frame.
        """
        capacity = max(1, int(capacity))
        if self._light_ubo is None:
            self._light_ubo = gl.glGenBuffers(1)
            self._light_ubo_bound = False
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
        if not getattr(self, '_light_ubo_bound', False):
            gl.glBindBufferBase(
                gl.GL_UNIFORM_BUFFER,
                shaders.LIGHT_UBO_BINDING,
                self._light_ubo,
            )
            self._light_ubo_bound = True

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
        # Every lit pass of a frame hands over the same (table, slots) pair:
        # the first one built the key and uploaded, so the rest return at
        # once instead of rebuilding the key (tuple conversions, a weights
        # pass, the cookie lookup) four to seven times a frame. The pair is
        # held, so its id cannot be reused; render_scene drops it each frame.
        if (lights is getattr(self, '_light_ubo_lights', None)
                and count == self._light_ubo_count
                and self._light_ubo_key is not None):
            return
        table, slots = lights
        active_lights = slots[:count]
        # Streamed play lights ease out before their cutoff (LightStream).
        fade = getattr(self, '_light_fade', None)
        weights = (1.0,) * count
        if fade is not None and fade[0] is table:
            weights = tuple(float(fade[1].get(slot, 1.0)) for slot in active_lights)
        # indices.z: the light's cookie layer + 1, or 0 (no image is sampled).
        cookie_layers = self._cookie_layers(table, active_lights)
        key = ('dense', id(table), table.generation, count,
               tuple(active_lights.tolist()), weights,
               None if cookie_layers is None else tuple(cookie_layers.tolist()))
        if self._light_ubo_key == key:
            self._light_ubo_lights, self._light_ubo_count = lights, count
            return

        active = self._light_ubo_data[:count]
        active['position'].fill(0.0)
        active['color'].fill(0.0)
        active['params'].fill(0.0)
        active['indices'].fill(0)

        active['position'][:, :3] = table.pos[active_lights].astype(np.float32, copy=False)
        active['position'][:, 3] = 1.0
        active['color'][:, :3] = table.light_color[active_lights]
        active['color'][:, 3] = 1.0
        active['params'][:, :2] = table.light_params[active_lights]
        # z: the source's size (an emissive brush); 0 for a point, and for
        # a light table that has no such column.
        sizes = getattr(table, 'light_size', None)
        if sizes is not None:
            active['params'][:, 2] = sizes[active_lights]
        # Shared by brush, model, terrain and water shaders. Never change the
        # table's authored intensity (which also drives flicker and I/O).
        active['params'][:, 0] *= weights
        shadow_indices = np.fromiter(
            (self._light_shadow_index.get(int(slot), -1) for slot in active_lights),
            dtype=np.int32,
            count=count,
        )

        active['indices'][:, 0] = shadow_indices
        if cookie_layers is not None:
            active['indices'][:, 2] = cookie_layers
            # w: how bright a cookie light's image stays in its own shadow.
            active['params'][:, 3] = table.light_cookie_floor[active_lights]

        # Respecify the whole store rather than sub-updating it: earlier draws
        # still queued against the old contents would otherwise make the
        # driver wait for them before the write. A fresh store (orphaning)
        # lets them finish on the old one.
        gl.glBindBuffer(gl.GL_UNIFORM_BUFFER, self._light_ubo)
        gl.glBufferData(
            gl.GL_UNIFORM_BUFFER,
            self._light_ubo_data.nbytes,
            self._light_ubo_data,
            gl.GL_DYNAMIC_DRAW,
        )
        self._light_ubo_key = key
        self._light_ubo_lights, self._light_ubo_count = lights, count

    def _cookie_layers(self, table, slots):
        """``layer + 1`` of each light's cookie in the shared array, 0 where
        it has none; None when no light in *slots* has one (the common case:
        one ``any`` over an int column, and nothing else happens). With a
        cookie in play the array is bound to its unit here, once per lit
        pass -- never per light or per draw."""
        cookies = getattr(table, 'light_cookie', None)
        ids = cookies[slots] if cookies is not None and len(slots) else None
        if ids is None or not ids.any():
            self._cookie_array_bound = False
            return None
        array = self._light_cookies
        if array is None:
            from engine.light_cookies import (
                COOKIE_SIZE, COOKIE_SIZE_LOW_POWER, CookieArray)
            array = self._light_cookies = CookieArray(
                gl, size=COOKIE_SIZE_LOW_POWER if self.lowpower_mode else COOKIE_SIZE)
        layers = array.layers(ids)
        # False when no image could be read: the placeholder stands in.
        self._cookie_array_bound = array.bind(shaders.COOKIE_TEXTURE_UNIT)
        # Whatever array.bind did, the unit no longer holds the placeholder.
        self._cookie_placeholder_bound = False
        return layers

    def _bind_cookie_placeholder(self):
        """A complete 1x1 array on the cookie unit while there is no cookie
        array bound (engine.light_cookies.bind_placeholder)."""
        from engine.light_cookies import bind_placeholder
        cache = self.__dict__.setdefault('_cookie_gl_cache', {})
        bind_placeholder(gl, shaders.COOKIE_TEXTURE_UNIT, cache)

    def _bind_light_cookies(self, uniforms):
        """Point a lighting program's cookie sampler at its own unit, with a
        complete texture there.

        Like the shadow samplers: a sampler2DArray left on unit 0 would share
        it with the diffuse sampler2D and fail the draw, and one whose unit
        holds nothing usable may be refused at draw time (macOS: "unloadable
        ... using zero texture"). The cookie array is bound to the unit when
        the light UBO is packed, while some light has a cookie; otherwise the
        placeholder is bound here, beside the shadow samplers, not by the
        packing (which stays free of texture work). The sampler's unit is
        program state, set once per program.
        """
        if not getattr(self, '_cookie_array_bound', False):
            # Once a frame, not once a pass: nothing else binds the unit.
            if not getattr(self, '_cookie_placeholder_bound', False):
                self._bind_cookie_placeholder()
                self._cookie_placeholder_bound = True
        if uniforms.cookie_unit:
            return
        loc = uniforms['fioCookies']
        if loc != -1:
            gl.glUniform1i(loc, shaders.COOKIE_TEXTURE_UNIT)
        uniforms.cookie_unit = True

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

        # _upload_light_ubo below makes sure the buffer exists and is bound.
        self._frame_lights_uploaded[shader_name] = (
            id(table), table.generation,
            tuple(slots[:cap].tolist()))
        program = self.uniforms[shader_name]
        if shader_name == 'terrain' or program.active_lights != num_lights:
            gl.glUniform1i(program['active_lights'], num_lights)
            program.active_lights = num_lights
        # One upload serves every pass: the buffer holds the frame's light
        # prefix up to MAX_LIGHTS, and each shader reads its own first
        # ``active_lights`` entries of it. Uploading per shader cap re-sent
        # the same lights once per pass.
        self._upload_light_ubo(lights, min(len(slots), self.MAX_LIGHTS))

        # Keep sampler2D and samplerCube uniforms on distinct texture units.
        # This is one shader-pass operation, never part of the per-draw loop.
        if shader_name in ('lit', 'textured', 'lit_instanced',
                           'textured_instanced', 'brush_instanced',
                           'lit_brush_instanced', 'geo_textured', 'geo_batched',
                           'terrain'):
            self._bind_shadow_maps(self.uniforms[shader_name])
            self._bind_light_cookies(self.uniforms[shader_name])

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
            self.shadow_units_changed()
            size = self.shadow_map_size
            # Every slot starts as a 1x1 placeholder -- complete, so each
            # shadow sampler always has a valid cube bound -- and is given its
            # full size only when a shadow-casting light takes it.
            self._shadow_cube_sizes = []
            for _ in range(self.MAX_SHADOW_LIGHTS):
                cm = int(gl.glGenTextures(1))
                gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, cm)
                self._specify_shadow_cube(1)
                self._shadow_cube_sizes.append(1)
                gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
                gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
                gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
                gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
                gl.glTexParameteri(gl.GL_TEXTURE_CUBE_MAP, gl.GL_TEXTURE_WRAP_R, gl.GL_CLAMP_TO_EDGE)
                self._shadow_cubemaps.append(cm)
            gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
            self.shadow_units_changed()

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

    @staticmethod
    def _specify_shadow_cube(size):
        """(Re)allocate the six depth faces of the bound cube-map at *size*."""
        for face in range(6):
            gl.glTexImage2D(gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + face, 0,
                            gl.GL_DEPTH_COMPONENT24, size, size, 0,
                            gl.GL_DEPTH_COMPONENT, gl.GL_FLOAT, None)

    #: The range a Light's own shadow_map_size is clamped to.
    SHADOW_MAP_SIZE_MIN = 64
    SHADOW_MAP_SIZE_MAX = 4096

    def _shadow_filter_array(self):
        """Per shadow slot ``(softness, bias)`` for the lit passes' uShadowFilter
        (0: the shader's default), filled by render_shadow_maps."""
        array = getattr(self, '_shadow_filter', None)
        if array is None:
            array = self._shadow_filter = np.zeros(
                (self.MAX_SHADOW_LIGHTS, 2), dtype=np.float32)
        return array

    def _ensure_shadow_cube_size(self, slot, size=None):
        """Give shadow slot *slot*'s cube-map *size* (default: the renderer's
        shadow_map_size) before it is rendered into; returns whether it was
        (re)allocated."""
        size = int(self.shadow_map_size if size is None else size)
        sizes = getattr(self, '_shadow_cube_sizes', None)
        if sizes is None or slot >= len(sizes) or sizes[slot] == size:
            return False
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, self._shadow_cubemaps[slot])
        self._specify_shadow_cube(size)
        gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, 0)
        sizes[slot] = size
        self.shadow_units_changed()
        return True

    def _bind_shadow_maps(self, uniforms):
        """Bind shadow samplers to dedicated texture units.

        Lighting shaders contain both sampler2D and samplerCube uniforms.
        OpenGL requires sampler uniforms of different types to reference
        different texture units at draw time, even when no shadowing light
        is active. Keep the cube samplers on their reserved units and bind
        texture 0 when shadow resources are unavailable.

        A sampler's unit is program state: it holds from the first assignment
        until the program is deleted, and a recompiled program gets a new
        UniformCache. So the units are assigned on the program's first pass
        only; every pass still binds the cube-maps, since the texture units
        are shared context state (terrain binds its own there).
        """
        base = self.SHADOW_TEXTURE_UNIT_BASE
        slots = uniforms.shadow_slots
        if slots is None:
            live = []
            for i in range(self.MAX_SHADOW_LIGHTS):
                loc = uniforms[f'shadowMaps[{i}]']
                if loc != -1:
                    gl.glUniform1i(loc, base + i)
                    live.append(i)
            slots = uniforms.shadow_slots = tuple(live)
        loc = uniforms['uShadowFilter[0]']
        if loc != -1:
            gl.glUniform2fv(loc, self.MAX_SHADOW_LIGHTS, self._shadow_filter_array())
        cubemaps = self._shadow_cubemaps
        count = len(cubemaps)
        # What each shadow unit holds is remembered for the frame: every lit
        # pass used to bind all of them again (two GL calls a unit, ~80 calls
        # a frame, a third of the frame's GL calls on a map with no shadows).
        # The record is dropped at the start of each frame, after the terrain
        # pass (which binds its own there) and whenever the cube-maps are
        # made or deleted (shadow_units_changed).
        bound = getattr(self, '_shadow_units_bound', None)
        if bound is None:
            bound = self._shadow_units_bound = {}
        for i in slots:
            texture = cubemaps[i] if i < count else 0
            if bound.get(i) != texture:
                gl.glActiveTexture(gl.GL_TEXTURE0 + base + i)
                gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP, texture)
                bound[i] = texture
        gl.glActiveTexture(gl.GL_TEXTURE0)

    def shadow_units_changed(self):
        """Forget what the shadow and cookie units hold: something other
        than _bind_shadow_maps / _bind_light_cookies bound or deleted a
        texture there (or a new frame starts, where nothing is assumed)."""
        self._shadow_units_bound = None
        self._cookie_placeholder_bound = False

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

    def _prepare_shadow_instances(self, table, in_brushes, instanced,
                                  key=('shadow', 0)):
        """Bring a light's box casters' instance stream (*key*) up to date
        and return ``(cube slots, convex geometry slots, stream)``.

        All inputs are RenderTable slots. No Brush reference is reconstructed
        here; geometry rows remain integer geometry_id handles and AABB rows
        use the instanced cube buffer.
        """
        if table is None or not len(in_brushes):
            return (np.empty(0, dtype=np.int32), np.empty(0, dtype=np.int32),
                    None)

        slots = np.asarray(in_brushes, dtype=np.int32)
        # The casters split into boxes and convex meshes, kept while the
        # light's casters and their classes are unchanged (M3.1).
        cube_slots, geo_slots = self._pass_memo(
            key, table, slots, (), (), lambda: _split_casters(table, slots))

        stream = None
        if instanced and len(cube_slots):
            stream = self._brush_stream(key, table, cube_slots, normals=False)

        return cube_slots, geo_slots, stream

    @timed_pass('shadow maps')
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

        caster_slots = self._kept_shadow_casters(table, caster_slots)
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

        # Each slot's filter for the lit passes (uShadowFilter), and each
        # light's cube-map size, from the Light that owns the slot.
        shadow_columns = getattr(light_table, 'light_shadow', None)
        shadow_filter = self._shadow_filter_array()
        shadow_filter[:] = 0.0
        slot_size = {}
        for key, s in light_slot.items():
            size = self.shadow_map_size
            if shadow_columns is not None:
                softness, bias, authored = (float(v) for v in shadow_columns[int(key)])
                shadow_filter[s] = (softness, bias)
                if authored > 0.0:
                    size = int(max(self.SHADOW_MAP_SIZE_MIN,
                                   min(self.SHADOW_MAP_SIZE_MAX, authored)))
            slot_size[s] = int(size)

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

            in_slots, brush_keys = self._kept_casters_in_reach(
                shadow_slot, table, caster_slots, lx, ly, lz, radius)
            in_models, model_keys = self._collect_dense_shadow_models(
                entity_table, dense_model_slots, lx, ly, lz, radius)

            sig = (
                round(lx, 3), round(ly, 3), round(lz, 3),
                round(radius, 3), brush_keys, model_keys,
                slot_size[shadow_slot],
            )
            self._light_shadow_index[light_slot_value] = shadow_slot
            if self._shadow_slot_sig[shadow_slot] == sig:
                continue
            to_render.append((
                light_slot_value, shadow_slot, in_slots, in_models, sig,
                lx, ly, lz, radius,
            ))

        if not to_render:
            # Every cube-map is current: nothing to bind, so no GL state to
            # save and restore either (six queries, which stall a driver).
            return

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
        inst_lsm_loc = inst_lightpos_loc = inst_far_loc = -1
        if depth_instanced is not None and self._cube_vbo is not None:
            iu = self.uniforms['depth_cube_instanced']
            inst_lsm_loc = iu['lightSpaceMatrix']
            inst_lightpos_loc = iu['lightPos']
            inst_far_loc = iu['far_plane']
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
            size = slot_size.get(slot, self.shadow_map_size)
            self._ensure_shadow_cube_size(slot, size)
            gl.glViewport(0, 0, size, size)
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
            # Each caster's model matrix, built once per light rather than
            # once per cube face: the authored base with the live position.
            model_slots = np.array([s for s, _ in resolved_models], dtype=np.intp)
            model_bases = entity_table.model_base_matrix[model_slots]
            model_bases[:, 12:15] = entity_table.pos[model_slots]
            upload_base = uniform_rows('mat4', model_bases)

            cube_slots, geo_slots, stream = self._prepare_shadow_instances(
                table, in_brushes, depth_instanced is not None,
                key=('shadow', slot))

            for face in range(6):
                gl.glFramebufferTexture2D(
                    gl.GL_FRAMEBUFFER, gl.GL_DEPTH_ATTACHMENT,
                    gl.GL_TEXTURE_CUBE_MAP_POSITIVE_X + face, cubemap, 0)
                gl.glClear(gl.GL_DEPTH_BUFFER_BIT)
                lsm = proj * glm.lookAt(
                    center, center + face_dirs[face][0], face_dirs[face][1])
                gl.glUniformMatrix4fv(
                    lsm_loc, 1, gl.GL_FALSE, glm.value_ptr(lsm))

                if len(cube_slots) and stream is not None:
                    gl.glUseProgram(depth_instanced)
                    gl.glUniformMatrix4fv(
                        inst_lsm_loc, 1, gl.GL_FALSE, glm.value_ptr(lsm))
                    gl.glUniform3f(inst_lightpos_loc, lx, ly, lz)
                    gl.glUniform1f(inst_far_loc, far_plane)
                    gl.glBindVertexArray(stream.vao)
                    gl.glBindBuffer(gl.GL_ARRAY_BUFFER, stream.vbo)
                    self._point_brush_instances_at(0)
                    gl.glDrawArraysInstanced(
                        gl.GL_TRIANGLES, 0, 36, len(cube_slots))
                    self.render_stats.shadow_draw_calls += 1
                    gl.glUseProgram(shader)
                    gl.glUniformMatrix4fv(
                        lsm_loc, 1, gl.GL_FALSE, glm.value_ptr(lsm))
                elif len(cube_slots):
                    cube_models, _ = self._frame_transforms(table, cube_slots)
                    upload_model = uniform_rows('mat4', cube_models)
                    gl.glBindVertexArray(cube_vao)
                    for i in range(len(cube_slots)):
                        upload_model(model_loc, i)
                        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
                        self.render_stats.shadow_draw_calls += 1

                gl.glBindVertexArray(cube_vao)
                if len(geo_slots):
                    geo_meshes = self._prepare_geo_meshes(table, geo_slots)
                    geo_models, _geo_normals = self._frame_transforms(
                        table, geo_slots)
                    upload_model = uniform_rows('mat4', geo_models)
                    for geo_i, slot_value in enumerate(geo_slots):
                        mesh = geo_meshes.get(
                            int(table.geometry_id[int(slot_value)]))
                        if mesh is None:
                            continue
                        upload_model(model_loc, geo_i)
                        gl.glBindVertexArray(mesh.vao)
                        gl.glDrawArrays(
                            gl.GL_TRIANGLES, 0, mesh.count)
                        self.render_stats.shadow_draw_calls += 1
                        gl.glBindVertexArray(cube_vao)

                for index, (slot_value, obj) in enumerate(resolved_models):
                    upload_base(model_loc, index)
                    gl.glBindVertexArray(obj.vao)
                    if (getattr(obj, 'ebo', None) is not None
                            and getattr(obj, 'index_count', 0)):
                        gl.glDrawElements(
                            gl.GL_TRIANGLES, obj.index_count,
                            gl.GL_UNSIGNED_INT, None)
                    else:
                        gl.glDrawArrays(
                            gl.GL_TRIANGLES, 0, obj.vertex_count)
                    self.render_stats.shadow_draw_calls += 1

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
