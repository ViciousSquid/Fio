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


#: Light-array capacity of each lighting shader, so the renderer can never set
#: ``active_lights`` higher than the shader has room for.  Anything absent from
#: this map holds the full ``Renderer.MAX_LIGHTS``.
_SHADER_LIGHT_CAPS = {
    'water': shaders.MAX_LIGHTS_WATER,
    'terrain': shaders.MAX_LIGHTS_TERRAIN,
}


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

    SHADOW_MAP_SIZE = 384         # per-face resolution of each depth cube-map

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
                                                  'lit_brush_instanced'):
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
        # One upload serves every pass: the buffer holds the frame's light
        # prefix up to MAX_LIGHTS, and each shader reads its own first
        # ``active_lights`` entries of it. Uploading per shader cap re-sent
        # the same lights once per pass.
        self._upload_light_ubo(lights, min(len(slots), self.MAX_LIGHTS))

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
        cubemaps = self._shadow_cubemaps
        count = len(cubemaps)
        for i in slots:
            gl.glActiveTexture(gl.GL_TEXTURE0 + base + i)
            gl.glBindTexture(gl.GL_TEXTURE_CUBE_MAP,
                             cubemaps[i] if i < count else 0)
        gl.glActiveTexture(gl.GL_TEXTURE0)

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
