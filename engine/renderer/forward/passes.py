"""ForwardRenderer's draw passes.

Opaque lit/textured/glow brushes, terrain, models, sprites, glasses,
billboards, effects, and the transparent water/glass/fog passes.
"""

import ctypes
import time

import glm
import numpy as np
import OpenGL.GL as gl

from engine import render_table
from engine import shaders
from engine.render_keys import runs_in_order, sort_into_runs
from ..api import WATER_QUALITIES
from ..core.diagnostics import timed_pass
from .instancing import BRUSH_RUN_KEY, _SELECTED_COLOR, _SUBTRACT_COLOR, _TRIGGER_COLOR


class PassesMixin:
    """ForwardRenderer's draw passes."""

    #: Water rendering tiers. 'cheap': refraction, waves, sky reflection and
    #: foam at the brush edges - no extra copies. 'expensive': additionally
    #: copies the depth buffer once per water pass for depth-based colour,
    #: shoreline foam, caustics and screen-space reflections.
    WATER_QUALITIES = WATER_QUALITIES

    @classmethod
    def normalize_water_quality(cls, value) -> str:
        value = str(value or '').strip().lower()
        return value if value in cls.WATER_QUALITIES else 'expensive'

    # --------------------------------------------------------------------------
    # Brush passes
    # --------------------------------------------------------------------------
    @timed_pass('lit brushes')
    def draw_lit_brushes_optimized(self, projection, view, camera_pos, brushes,
                                   lights, config, table,
                                   is_transparent_pass=False):
        """Draw lit brush slots from dense RenderTable columns.

        Transforms, material state, selection and geometry IDs all come from
        dense render data; authored Brush objects are never touched here.
        """
        if len(brushes) == 0 or 'lit' not in self.shaders:
            return
        visible = brushes
        self.render_stats.visible_brushes += len(visible)
        shader, uniforms = self.shaders['lit'], self.uniforms['lit']
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_lights_once('lit', lights)
        # Cache value_ptr results – avoids redundant ctypes work per draw call
        proj_ptr = glm.value_ptr(projection)
        view_ptr = glm.value_ptr(view)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'],       1, gl.GL_FALSE, view_ptr)
        gl.glBindVertexArray(self.vaos['cube'])
        display_mode        = config.get('brush_display_mode', 'Textured')
        show_triggers_solid = config.get('show_triggers_as_solid', False)
        selected            = config.get('primary_selection')
        model_loc      = uniforms['model']
        color_loc      = uniforms['object_color']
        alpha_loc      = uniforms['alpha']
        normal_mat_loc = uniforms.get('normalMatrix', -1)
        if normal_mat_loc is None:
            normal_mat_loc = -1

        if is_transparent_pass:
            fill_mode = gl.GL_FILL if show_triggers_solid else gl.GL_LINE
        else:
            fill_mode = gl.GL_FILL if display_mode != "Wireframe" else gl.GL_LINE
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, fill_mode)

        indices = range(len(visible))
        models, normals = self._frame_transforms(table, visible)
        bits = table.class_bits[visible]
        colours = table.colour[visible]
        selected_slot = self._selected_slot(table, config)
        geometry = (bits & render_table.CLASS_HAS_GEOMETRY) != 0
        # Resolve convex meshes at the dense-table/cache boundary once for
        # the geometry rows in this pass. The draw loop stays integer-only:
        # geometry_id -> prepared mesh, with no slot -> Brush lookup.
        geo_meshes = (
            self._prepare_geo_meshes(table, visible[geometry])
            if geometry.any() else {}
        )
        if ('lit_brush_instanced' in self.shaders
                and self._cube_vbo is not None and (~geometry).any()):
            # Every plain box brush in one submission; the angled minority
            # still needs its own mesh, so it falls through to the loop.
            self._draw_lit_brushes_instanced(
                projection, view, lights, table, visible,
                np.flatnonzero(~geometry).astype(np.int32), models, normals,
                config, selected_slot)
            gl.glUseProgram(shader)
            self._current_shader = shader
            gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, proj_ptr)
            gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, view_ptr)
            gl.glBindVertexArray(self.vaos['cube'])
            gl.glPolygonMode(gl.GL_FRONT_AND_BACK, fill_mode)
            indices = [int(i) for i in np.flatnonzero(geometry)]

        cube_vao = self.vaos['cube']
        bound_vao = cube_vao
        # Portal virtual scene: cull each brush's interior faces so the oblique
        # clip can't expose their dark back-faces. Cube and convex-geometry
        # meshes wind oppositely, so the culled face is switched alongside the
        # VAO below. No-op in the main pass.
        self._portal_begin_cull(is_geo=False)
        for index in indices:
            slot = int(visible[index])
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, models[index])
            if normal_mat_loc > 0:
                gl.glUniformMatrix3fv(normal_mat_loc, 1, gl.GL_FALSE,
                                      normals[index])
            row = int(bits[index])
            if row & render_table.CLASS_TRIGGER:
                color, alpha = _TRIGGER_COLOR, 0.3
            elif slot == selected_slot:
                color, alpha = _SELECTED_COLOR, 1.0
            elif row & render_table.CLASS_SUBTRACT:
                color, alpha = _SUBTRACT_COLOR, 1.0
            else:
                color, alpha = colours[index], 1.0
            has_geometry = bool(row & render_table.CLASS_HAS_GEOMETRY)

            gl.glUniform3fv(color_loc, 1, color)
            gl.glUniform1f(alpha_loc, alpha)
            # An angled brush draws from its own convex mesh, which is built
            # from the brush's plane set -- the one thing no column holds, and
            # the only place the numeric path needs an object.
            mesh = None
            if has_geometry:
                mesh = geo_meshes.get(int(table.geometry_id[slot]))
            if mesh is not None:
                if bound_vao != mesh.vao:
                    gl.glBindVertexArray(mesh.vao)
                    bound_vao = mesh.vao
                    self._portal_set_cull(is_geo=True)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)
                self.render_stats.visible_tris += mesh.count // 3
            else:
                if bound_vao != cube_vao:
                    gl.glBindVertexArray(cube_vao)
                    bound_vao = cube_vao
                    self._portal_set_cull(is_geo=False)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
                self.render_stats.visible_tris += 12
            self.render_stats.draw_calls += 1

        self._portal_end_cull()
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl.glBindVertexArray(0)

    def _draw_lit_brushes_instanced(self, projection, view, lights, table,
                                    slots, cube_rows, models, normals, config,
                                    selected_slot):
        """Pack the lit pass's instances and hand its runs to the GPU.

        The lit pass binds no texture and draws the whole cube, so both key
        fields are zero for every brush and the sort yields exactly one run.
        That is worth doing through the same machinery rather than short-cut
        to a single draw: the run count falls out of the data, so when
        something does start varying per run the pass needs a field in the key
        and nothing else.
        """
        count = len(cube_rows)
        if not count:
            return 0
        row_slots = slots[cube_rows]
        payload = self.lit_instance_payload(table, row_slots, selected_slot)

        zeros = np.zeros(count, dtype=np.int64)
        keys = BRUSH_RUN_KEY.pack(texture=zeros, face=zeros)
        order, run_starts = sort_into_runs(keys)
        rows = cube_rows[order]
        payload = payload[order]

        self._pack_brush_instances(models, normals, rows, np.float32(0.0),
                                   payload)
        run_texture, run_first = self._run_descriptors(
            zeros, zeros, run_starts)
        self._submit_brush_runs('lit_brush_instanced', projection, view,
                                lights, run_starts, run_texture, run_first, 36)
        return count

    def _submit_brush_runs(self, program_name, projection, view, lights,
                           run_starts, run_texture, run_first, vertex_count):
        """Submit sorted brush runs. The one place brush geometry reaches GL.

        A *run* is a stretch of items whose render key is equal, so everything
        it contains shares the GPU state that key encodes.  That state is
        established once here -- the texture bind, and the vertex range the
        cube face occupies -- and everything that differs inside the run
        travels as instance data, already packed into the shared buffer.

        What is left between draws is exactly what changed: a texture bind when
        this run's texture is not the one already bound, and the instance
        attribute base, which stands in for the base-instance offset OpenGL 3.3
        does not have.

        *run_texture* may be zero for a pass that binds no texture; the lit
        pass is such a pass, and passing zero is how it says so rather than by
        taking a different route to the GPU.
        """
        self._begin_instanced_pass(program_name, projection, view, lights)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl.glBindVertexArray(self._ensure_brush_instance_vao())

        current_tex = None
        triangles = vertex_count // 3
        for run in range(len(run_starts) - 1):
            begin = int(run_starts[run])
            length = int(run_starts[run + 1]) - begin
            if length <= 0:
                continue
            tex_id = int(run_texture[run])
            if tex_id and tex_id != current_tex:
                gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                current_tex = tex_id
                self.render_stats.batched_draws += 1
            self._point_brush_instances_at(begin)
            gl.glDrawArraysInstanced(gl.GL_TRIANGLES, int(run_first[run]),
                                     vertex_count, length)
            self.render_stats.draw_calls += 1
            self.render_stats.visible_tris += triangles * length
        gl.glBindVertexArray(0)

    def _draw_face_runs_instanced(self, projection, view, lights, models,
                                  normals, rows, faces, gl_tex, scales, shifts,
                                  angles, run_starts):
        """Pack the textured pass's instances and hand its runs to the GPU."""
        payload = np.empty((len(rows), 4), dtype=np.float32)
        payload[:, 0:2] = scales
        payload[:, 2:4] = shifts
        self._pack_brush_instances(models, normals, rows, angles, payload)
        run_texture, run_first = self._run_descriptors(gl_tex, faces, run_starts)
        self._submit_brush_runs('brush_instanced', projection, view, lights,
                                run_starts, run_texture, run_first, 6)
        gl.glActiveTexture(gl.GL_TEXTURE0)

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

    @timed_pass('textured brushes')
    def draw_textured_brushes_optimized(self, projection, view, camera_pos,
                                        brushes, lights, config,
                                        table):
        """Textured brushes from the dense RenderTable projection.

        The 2.5 render path has no Brush-object fallback here.  Face batches,
        transforms, material ids and convex geometry handles all come from
        dense numerical columns.
        """
        if len(brushes) == 0 or 'textured' not in self.shaders:
            return
        if table is None:
            raise RuntimeError(
                "draw_textured_brushes_optimized requires RenderTable")

        slots = brushes
        self.render_stats.visible_brushes += len(slots)
        shader, uniforms = self.shaders['textured'], self.uniforms['textured']
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_lights_once('textured', lights)

        proj_ptr = glm.value_ptr(projection)
        view_ptr = glm.value_ptr(view)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE, view_ptr)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(uniforms['texture_diffuse'], 0)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl.glBindVertexArray(self.vaos['cube'])
        model_loc = uniforms['model']

        tex_scale_loc = uniforms.get('tex_scale', -1)
        if tex_scale_loc == -1:
            loc = gl.glGetUniformLocation(shader, "tex_scale")
            uniforms._cache['tex_scale'] = loc
            tex_scale_loc = loc

        tex_angle_loc = uniforms.get('tex_angle', -1)
        if tex_angle_loc == -1:
            loc = gl.glGetUniformLocation(shader, "tex_angle")
            uniforms._cache['tex_angle'] = loc
            tex_angle_loc = loc

        tex_shift_loc = uniforms.get('tex_shift', -1)
        if tex_shift_loc == -1:
            loc = gl.glGetUniformLocation(shader, "tex_shift")
            uniforms._cache['tex_shift'] = loc
            tex_shift_loc = loc

        normal_mat_loc = uniforms.get('normalMatrix', -1)
        if normal_mat_loc is None:
            normal_mat_loc = -1

        is_play = config.get('play_mode', False)
        rows, faces, gl_tex, scales, run_starts = self._build_face_batches(
            table, slots, config)
        models, normals = self._frame_transforms(table, slots)
        sel_slots = slots[rows]
        angles = np.radians(table.uv_angle[sel_slots, faces])
        shifts = table.uv_shift[sel_slots, faces]

        self._portal_begin_cull(is_geo=False)
        if self.debug_gl_state:
            self._debug_textured_brush_gl_state()

        current_tex = None
        instanced = (len(rows) > 0 and 'brush_instanced' in self.shaders
                     and self._cube_vbo is not None)
        if instanced:
            self._draw_face_runs_instanced(
                projection, view, lights, models, normals, rows, faces,
                gl_tex, scales, shifts, angles, run_starts)
            gl.glUseProgram(shader)
            self._current_shader = shader
            gl.glActiveTexture(gl.GL_TEXTURE0)
            gl.glBindVertexArray(self.vaos['cube'])
        else:
            last_row = -1
            for i in range(len(rows)):
                tex_id = int(gl_tex[i])
                if tex_id != current_tex:
                    gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                    current_tex = tex_id
                    self.render_stats.batched_draws += 1
                row = int(rows[i])
                if row != last_row:
                    gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, models[row])
                    if normal_mat_loc > 0:
                        gl.glUniformMatrix3fv(
                            normal_mat_loc, 1, gl.GL_FALSE, normals[row])
                    last_row = row
                if tex_angle_loc != -1:
                    gl.glUniform1f(tex_angle_loc, float(angles[i]))
                if tex_shift_loc != -1:
                    gl.glUniform2f(
                        tex_shift_loc, float(shifts[i, 0]), float(shifts[i, 1]))
                if tex_scale_loc != -1:
                    gl.glUniform2f(
                        tex_scale_loc, float(scales[i, 0]), float(scales[i, 1]))
                gl.glDrawArrays(gl.GL_TRIANGLES, int(faces[i]) * 6, 6)
                self.render_stats.visible_tris += 2
                self.render_stats.draw_calls += 1

        # Convex/custom geometry is addressed only by the dense geometry handle.
        self._portal_set_cull(is_geo=True)
        geo_slots = slots[
            (table.class_bits[slots] & render_table.CLASS_HAS_GEOMETRY) != 0
        ]
        geo_meshes = self._prepare_geo_meshes(table, geo_slots)
        geo_rows = np.flatnonzero(
            (table.class_bits[slots] & render_table.CLASS_HAS_GEOMETRY) != 0
        )

        for geo_i, slot_value in enumerate(geo_slots):
            gid = int(table.geometry_id[int(slot_value)])
            mesh = geo_meshes.get(gid)
            if mesh is None:
                continue
            row = int(geo_rows[geo_i])
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, models[row])
            if normal_mat_loc > 0:
                gl.glUniformMatrix3fv(
                    normal_mat_loc, 1, gl.GL_FALSE, normals[row])
            gl.glBindVertexArray(mesh.vao)

            for run in mesh.runs:
                tex_name = self._geo_run_texture(run)
                if tex_name == 'caulk.jpg':
                    continue
                if is_play and tex_name == 'nodraw.jpg':
                    continue

                tex_id = self.texture_manager.get(
                    self._tex_cache_path(tex_name)
                ) or self.load_texture_callback(tex_name, 'textures')
                if tex_id != current_tex:
                    gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                    current_tex = tex_id

                if tex_scale_loc != -1:
                    su, sv = self._geo_run_tex_scale(run, tex_name)
                    gl.glUniform2f(tex_scale_loc, su, sv)
                if tex_angle_loc != -1 or tex_shift_loc != -1:
                    angle, shift_u, shift_v = self._geo_run_tex_transform(run)
                    if tex_angle_loc != -1:
                        gl.glUniform1f(tex_angle_loc, angle)
                    if tex_shift_loc != -1:
                        gl.glUniform2f(tex_shift_loc, shift_u, shift_v)

                gl.glDrawArrays(
                    gl.GL_TRIANGLES, run['first'], run['count'])
                self.render_stats.visible_tris += run['count'] // 3
                self.render_stats.draw_calls += 1

        self._portal_end_cull()
        gl.glBindVertexArray(0)

    @timed_pass('glow brushes')
    def draw_glow_brushes(self, projection, view, camera_pos, brushes, lights,
                          config, table):
        """Overbright brushes.

        With *table*, ``brushes`` is an array of slots: the
        overbright colour was resolved into ``glow_colour`` when the brush was
        edited, so the per-brush ``[min(c * intensity, 10.0) for c in base]``
        list comprehension no longer runs per frame.
        """
        if len(brushes) == 0 or 'lit' not in self.shaders:
            return
        shader, uniforms = self.shaders['lit'], self.uniforms['lit']
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_lights_once('lit', lights)
        proj_ptr = glm.value_ptr(projection)
        view_ptr = glm.value_ptr(view)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE, proj_ptr)
        gl.glUniformMatrix4fv(uniforms['view'],       1, gl.GL_FALSE, view_ptr)
        gl.glBindVertexArray(self.vaos['cube'])
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        model_loc      = uniforms['model']
        color_loc      = uniforms['object_color']
        alpha_loc      = uniforms['alpha']
        normal_mat_loc = uniforms.get('normalMatrix', -1)
        if normal_mat_loc is None:
            normal_mat_loc = -1
        cube_vao = self.vaos['cube']
        bound_vao = cube_vao

        models, normals = self._frame_transforms(table, brushes)
        colours = table.glow_colour[brushes]
        geometry = (table.class_bits[brushes]
                    & render_table.CLASS_HAS_GEOMETRY) != 0
        geo_meshes = self._prepare_geo_meshes(table, brushes)


        for index in range(len(brushes)):
            self.render_stats.visible_tris += 12
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, models[index])
            if normal_mat_loc > 0:
                gl.glUniformMatrix3fv(normal_mat_loc, 1, gl.GL_FALSE, normals[index])
            gl.glUniform3fv(color_loc, 1, colours[index])
            has_geometry = bool(geometry[index])
            gl.glUniform1f(alpha_loc, 1.0)
            mesh = None
            if has_geometry:
                mesh = geo_meshes.get(int(table.geometry_id[int(brushes[index])]))
            if mesh is not None:
                if bound_vao != mesh.vao:
                    gl.glBindVertexArray(mesh.vao)
                    bound_vao = mesh.vao
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)
            else:
                if bound_vao != cube_vao:
                    gl.glBindVertexArray(cube_vao)
                    bound_vao = cube_vao
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
            self.render_stats.draw_calls += 1
        gl.glBindVertexArray(0)

    def _debug_textured_brush_gl_state(self):
        """Print the VAO/program state used by the textured-brush pass.

        This is an opt-in diagnostic path only. It is intentionally called once
        before the face submission loop rather than from the per-face hot path.
        """
        print(
            '[Renderer] textured-brush GL state: '
            f'program={int(gl.glGetIntegerv(gl.GL_CURRENT_PROGRAM))}, '
            f'vao={int(gl.glGetIntegerv(gl.GL_VERTEX_ARRAY_BINDING))}, '
            f'array_buffer={int(gl.glGetIntegerv(gl.GL_ARRAY_BUFFER_BINDING))}, '
            f'element_buffer={int(gl.glGetIntegerv(gl.GL_ELEMENT_ARRAY_BUFFER_BINDING))}, '
            f'tf_active={bool(int(gl.glGetBooleanv(gl.GL_TRANSFORM_FEEDBACK_ACTIVE)))}, '
            f'tf_paused={bool(int(gl.glGetBooleanv(gl.GL_TRANSFORM_FEEDBACK_PAUSED)))}, '
            f'rasterizer_discard={bool(int(gl.glGetBooleanv(gl.GL_RASTERIZER_DISCARD)))}'
        )

        def _scalar(value):
            return int(np.asarray(value).reshape(-1)[0])

        for attrib in (0, 1, 2):
            enabled = _scalar(
                gl.glGetVertexAttribiv(
                    attrib, gl.GL_VERTEX_ATTRIB_ARRAY_ENABLED
                )
            )
            buffer = _scalar(
                gl.glGetVertexAttribiv(
                    attrib, gl.GL_VERTEX_ATTRIB_ARRAY_BUFFER_BINDING
                )
            )
            stride = _scalar(
                gl.glGetVertexAttribiv(
                    attrib, gl.GL_VERTEX_ATTRIB_ARRAY_STRIDE
                )
            )
            attr_type = _scalar(
                gl.glGetVertexAttribiv(
                    attrib, gl.GL_VERTEX_ATTRIB_ARRAY_TYPE
                )
            )
            print(
                f'[Renderer] attrib{attrib}: '
                f'enabled={bool(enabled)}, '
                f'buffer={buffer}, '
                f'stride={stride}, '
                f'type=0x{attr_type:x}'
            )

    # --------------------------------------------------------------------------
    # Terrain
    # --------------------------------------------------------------------------
    @timed_pass('terrain')
    def render_terrain(self, projection, view, camera_pos, terrain, lights, frustum_planes=None):
        if terrain is None or not terrain.enabled:
            return
        self._ensure_terrain_textures(terrain)
        # A Terrain compiles its own program when it is built with a context
        # current; bind this renderer's program whenever the frame's terrain
        # does not hold it (new, reloaded, or bound by a previous renderer).
        if terrain.shader_program != self.shaders.get('terrain'):
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

        # _upload_lights_once() writes regular uniforms as well as the shared
        # light UBO, so the terrain program must be current before that call.
        # Without this, glUniform1i/uFogEnabled can raise GL_INVALID_OPERATION
        # when terrain follows a pass that has left another program bound.
        gl.glUseProgram(terrain.shader_program)
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
    @timed_pass('models')
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
        if count == 1 and float(table.render_alpha[int(slots[0])]) >= 1.0:
            # Opaque singletons do not benefit from instancing. A fading row must
            # stay on the instanced path because opacity is per-instance data.
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
                gl.glUniform1f(u['alpha'], 1.0)
                if shader_kind != 'textured':
                    colour = override_color or (0.8, 0.8, 0.8)
                    gl.glUniform3fv(u['object_color'], 1, colour)
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

    # --------------------------------------------------------------------------
    # Sprites, billboards and glasses
    # --------------------------------------------------------------------------
    @timed_pass('sprites')
    def draw_sprites_instanced(self, projection, view, table, slots,
                               gl_ids=None, camera_pos=None):
        """The sprite pass over dense columns: one draw, back to front.

        *slots* are rows of an :class:`engine.entity_table.EntityTable`, already
        classified into the sprite pass.  Everything this needs is a column
        read: the centre from ``pos``, the size from ``sprite_size``, the
        texture from ``sprite_key_id`` through :meth:`_sprite_gl_ids`.  No
        entity is touched.

        Rows whose texture resolves to 0 are dropped, which is what the object
        path's ``if tex_id:`` did.  The rest are ordered back to front by XZ
        distance when a camera is supplied -- the pass is blended with depth
        writes off, so that order is part of the picture -- and each sprite's
        image is a layer of one texture array (:mod:`engine.sprite_layers`), so
        the ordered set is a single ``glDrawArraysInstanced``.  Grouping by
        texture instead would draw a far sprite over a near one wherever two
        different images overlap.

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

        # Back-to-front, by the XZ distance the pass has always used. This is
        # the order the pass *must* draw in -- it is blended with depth writes
        # off -- so nothing below is allowed to reorder it.
        count = len(slots)
        if camera_pos is None:
            order = np.arange(count, dtype=np.intp)
        else:
            cx, _, cz = self._camera_xyz(camera_pos)
            depth_sq = self._sprite_depth_scratch[:count]
            depth_aux = self._sprite_depth_aux_scratch[:count]
            np.take(table.pos[:, 0], slots, out=depth_sq)
            np.subtract(depth_sq, cx, out=depth_sq)
            np.square(depth_sq, out=depth_sq)
            np.take(table.pos[:, 2], slots, out=depth_aux)
            np.subtract(depth_aux, cz, out=depth_aux)
            np.square(depth_aux, out=depth_aux)
            np.add(depth_sq, depth_aux, out=depth_sq)
            np.negative(depth_sq, out=depth_aux)
            order = np.argsort(depth_aux, kind='stable')
        ordered_textures = textures[order]

        # The texture leaves the draw state when every image is a layer of one
        # array: then the whole pass is a single draw in exactly that order.
        layers = None
        if 'sprite_layered' in self.shaders:
            layers = self._sprite_layer_array().layers_for(ordered_textures)

        self._ensure_sprite_instance_buffer(count)
        data = self._sprite_instance_data[:count]
        sorted_slots = self._sprite_sorted_slots_scratch[:count]
        np.take(slots, order, out=sorted_slots)
        # Gather directly into the reusable GPU staging buffer. The explicit
        # out= avoids a temporary (N,3)/(N,2) array on every sprite frame.
        np.take(table.pos, sorted_slots, axis=0, out=data[:, 0:3])
        np.take(table.sprite_size, sorted_slots, axis=0, out=data[:, 3:5])
        np.take(table.sprite_fixed_yaw, sorted_slots, out=data[:, 5])
        np.take(table.render_alpha, sorted_slots, out=data[:, 6])
        if layers is not None:
            data[:, 7] = layers
        else:
            data[:, 7] = 0.0
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._sprite_instance_vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, data)

        program = 'sprite_layered' if layers is not None else 'sprite_instanced'
        shader, uniforms = self.shaders[program], self.uniforms[program]
        gl.glUseProgram(shader)
        self._current_shader = shader
        # Billboards are unlit, so they never reach _upload_lights_once -- they
        # still need fogging, exactly as the per-sprite path does.
        self._upload_env_uniforms(program)
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE,
                              glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE,
                              glm.value_ptr(view))
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(uniforms['use_fixed_facing'], 1)
        gl.glBindVertexArray(self._ensure_sprite_instance_vao())

        if layers is not None:
            gl.glUniform1i(uniforms['sprite_layers'], 0)
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, self._sprite_layers.texture)
            self._point_sprite_instances_at(0)
            gl.glDrawArraysInstanced(gl.GL_TRIANGLE_STRIP, 0, 4, count)
            gl.glBindTexture(gl.GL_TEXTURE_2D_ARRAY, 0)
            self.render_stats.draw_calls += 1
            self.render_stats.batched_draws += 1
        else:
            # No array (a driver without the blit path, or more sprite images
            # than it allows layers): one draw per equal-texture stretch *of
            # the depth-ordered sequence*, which costs draws but never order.
            gl.glUniform1i(uniforms['sprite_texture'], 0)
            run_starts = runs_in_order(ordered_textures)
            for run in range(len(run_starts) - 1):
                begin = int(run_starts[run])
                length = int(run_starts[run + 1]) - begin
                gl.glBindTexture(gl.GL_TEXTURE_2D, int(ordered_textures[begin]))
                self.render_stats.batched_draws += 1
                self._point_sprite_instances_at(begin)
                gl.glDrawArraysInstanced(gl.GL_TRIANGLE_STRIP, 0, 4, length)
                self.render_stats.draw_calls += 1
        gl.glBindVertexArray(0)
        return count

    def draw_billboards(self, projection, view, positions, size, tex_id):
        """Camera-facing billboards sharing one texture and size: one draw.

        For the dense runtime populations that are not entities -- monster
        projectiles -- which used to cost three GL calls each per frame.
        """
        positions = np.asarray(positions, dtype=np.float32).reshape(-1, 3)
        count = len(positions)
        if not count or 'sprite_instanced' not in self.shaders or not tex_id:
            return 0
        self._ensure_sprite_instance_buffer(count)
        data = self._sprite_instance_data[:count]
        data[:, 0:3] = positions
        data[:, 3] = float(size[0])
        data[:, 4] = float(size[1])
        data[:, 5] = -10000.0
        data[:, 6] = 1.0
        data[:, 7] = 0.0
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._sprite_instance_vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, data)
        shader, uniforms = (self.shaders['sprite_instanced'],
                            self.uniforms['sprite_instanced'])
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_env_uniforms('sprite_instanced')
        gl.glUniformMatrix4fv(uniforms['projection'], 1, gl.GL_FALSE,
                              glm.value_ptr(projection))
        gl.glUniformMatrix4fv(uniforms['view'], 1, gl.GL_FALSE,
                              glm.value_ptr(view))
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(uniforms['sprite_texture'], 0)
        gl.glUniform1i(uniforms['use_fixed_facing'], 0)
        gl.glBindTexture(gl.GL_TEXTURE_2D, int(tex_id))
        gl.glBindVertexArray(self._ensure_sprite_instance_vao())
        self._point_sprite_instances_at(0)
        gl.glDrawArraysInstanced(gl.GL_TRIANGLE_STRIP, 0, 4, count)
        gl.glBindVertexArray(0)
        self.render_stats.draw_calls += 1
        return count

    def draw_player_glasses(self, projection, view, positions,
                           width=40.0, height=18.0, lift=0.0, sprites=()):
        """Draw the player as the fixed glasses billboard.

        Player bodies are deliberately not EntityTable rows, so this is the
        small non-entity billboard path used only for player representation
        (split-screen and portal virtual scenes). It reuses the existing sprite
        shader/VAO and performs at most two draws in a normal split-screen view.
        *positions* are the published ``player_glasses_positions`` and are
        already at eye height. *lift* is an optional caller-supplied offset;
        the default is zero so portal and split-screen views do not apply a
        second eye-height offset. *sprites*
        holds the ``sprite_textures`` key each position wears (player 1's
        chosen style, see :mod:`engine.glasses`); missing or unloaded keys
        fall back to the default pair.
        """
        if not positions or 'sprite' not in self.shaders:
            return 0
        default_tex = self.sprite_textures.get('Glasses')
        if not default_tex:
            return 0
        vao = self.vaos.get('sprite')
        if not vao:
            return 0

        shader = self.shaders['sprite']
        uniforms = self.uniforms['sprite']
        gl.glUseProgram(shader)
        self._current_shader = shader
        gl.glUniformMatrix4fv(
            uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection)
        )
        gl.glUniformMatrix4fv(
            uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view)
        )
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(uniforms['sprite_texture'], 0)
        gl.glBindVertexArray(vao)

        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDepthFunc(gl.GL_LESS)
        gl.glDepthMask(gl.GL_FALSE)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        pos_loc = uniforms['sprite_pos_world']
        size_loc = uniforms['sprite_size']

        count = 0
        sprites = tuple(sprites or ())
        bound = None
        try:
            for i, pos in enumerate(positions):
                key = sprites[i] if i < len(sprites) else None
                tex_id = int(self.sprite_textures.get(key) or default_tex)
                if tex_id != bound:
                    gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                    bound = tex_id
                try:
                    px, py, pz = float(pos.x), float(pos.y), float(pos.z)
                except AttributeError:
                    px, py, pz = float(pos[0]), float(pos[1]), float(pos[2])
                gl.glUniform3f(pos_loc, px, py + float(lift), pz)
                gl.glUniform2f(size_loc, float(width), float(height))
                gl.glDrawArrays(gl.GL_TRIANGLE_STRIP, 0, 4)
                count += 1
        finally:
            gl.glDepthMask(gl.GL_TRUE)
            gl.glDisable(gl.GL_BLEND)
            gl.glBindVertexArray(0)
        return count

    # --------------------------------------------------------------------------
    # Effects
    # --------------------------------------------------------------------------
    @timed_pass('effects')
    def draw_effects_instanced(
        self, projection, view, table, slots, hidden=None,
        play_mode=True, editor_time=0.0, camera_pos=None,
    ):
        """Draw FIRE/ORB as animated billboards and EXPLOSION through its existing shader."""
        if not len(slots):
            return 0

        slots = np.asarray(slots, dtype=np.int32)
        if hidden is not None:
            live = ~np.asarray(hidden, dtype=bool)[slots]
            slots = slots[live]
        if not len(slots):
            return 0

        fire_count = self.draw_fire_effects_instanced(
            projection, view, table, slots, hidden=None, camera_pos=camera_pos
        )

        explosion = table.effect_type[slots] == 1
        alive = table.effect_alive[slots]
        slots = slots[explosion & alive]
        if not len(slots) or 'effect_instanced' not in self.shaders:
            return fire_count

        count = len(slots)
        if len(self._effect_order_scratch) < count:
            grown = max(64, len(self._effect_order_scratch) * 2, count)
            self._effect_order_scratch = np.empty(grown, dtype=np.int32)
            self._effect_depth_scratch = np.empty(grown, dtype=np.float64)
            self._effect_depth_aux_scratch = np.empty(grown, dtype=np.float64)

        depth = self._effect_depth_scratch[:count]
        if camera_pos is None:
            order = np.arange(count, dtype=np.int32)
        else:
            cx, _, cz = self._camera_xyz(camera_pos)
            np.take(table.pos[:, 0], slots, out=depth)
            np.subtract(depth, cx, out=depth)
            np.square(depth, out=depth)
            aux = self._effect_depth_aux_scratch[:count]
            np.take(table.pos[:, 2], slots, out=aux)
            np.subtract(aux, cz, out=aux)
            np.square(aux, out=aux)
            np.add(depth, aux, out=depth)
            order = np.argsort(depth, kind='stable')[::-1]

        sorted_slots = slots[order]
        self._ensure_effect_instance_buffer(count)
        data = self._effect_instance_data[:count]

        np.take(table.pos, sorted_slots, axis=0, out=data[:, 0:3])
        np.take(table.effect_params[:, :2], sorted_slots, axis=0,
               out=data[:, 3:5])
        np.take(table.effect_elapsed, sorted_slots, out=data[:, 5])
        np.take(table.effect_lifetime, sorted_slots, out=data[:, 6])
        np.take(table.effect_seed, sorted_slots, out=data[:, 7])
        data[:, 8] = 1.0
        data[:, 9:11] = 0.0
        np.take(table.sprite_size[:, 1], sorted_slots, out=data[:, 10])
        np.take(table.effect_color, sorted_slots, axis=0,
               out=data[:, 11:14])
        data[:, 14] = 1.0
        data[:, 15] = table.effect_preview[sorted_slots]

        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._effect_instance_vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, data)

        blend_was = bool(gl.glIsEnabled(gl.GL_BLEND))
        cull_was = bool(gl.glIsEnabled(gl.GL_CULL_FACE))
        if not blend_was:
            gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        if cull_was:
            gl.glDisable(gl.GL_CULL_FACE)

        shader = self.shaders['effect_instanced']
        uniforms = self.uniforms['effect_instanced']
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_env_uniforms('effect_instanced')
        gl.glUniformMatrix4fv(
            uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection)
        )
        gl.glUniformMatrix4fv(
            uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view)
        )
        prev_active_texture = gl.glGetIntegerv(gl.GL_ACTIVE_TEXTURE)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        if self.effect_explosion_texture:
            gl.glBindTexture(gl.GL_TEXTURE_2D, self.effect_explosion_texture)
        gl.glUniform1i(uniforms['explosion_texture'], 0)

        gl.glBindVertexArray(self._ensure_effect_instance_vao())
        gl.glDrawArraysInstanced(
            gl.GL_TRIANGLE_STRIP, 0, 4, count
        )
        self.render_stats.draw_calls += 1
        self.render_stats.batched_draws += 1
        gl.glBindVertexArray(0)
        gl.glActiveTexture(prev_active_texture)
        if cull_was:
            gl.glEnable(gl.GL_CULL_FACE)
        if not blend_was:
            gl.glDisable(gl.GL_BLEND)
        return fire_count + count

    @timed_pass('fire effects')
    def draw_fire_effects_instanced(
        self, projection, view, table, slots, hidden=None, camera_pos=None,
    ):
        """Draw FIRE and ORB as normal instanced billboards using decoded GIF frames."""
        if 'sprite_instanced' not in self.shaders or not len(slots):
            return 0

        slots = np.asarray(slots, dtype=np.int32)
        if hidden is not None:
            live = ~np.asarray(hidden, dtype=bool)[slots]
            slots = slots[live]
        if not len(slots):
            return 0

        effect_types = table.effect_type[slots]
        alive = table.effect_alive[slots]
        animated = (effect_types != 1) & alive
        slots = slots[animated]
        effect_types = effect_types[animated]
        if not len(slots):
            return 0

        count = len(slots)
        if len(self._sprite_texture_scratch) < count:
            grown = max(64, len(self._sprite_texture_scratch) * 2, count)
            self._sprite_texture_scratch = np.empty(grown, dtype=np.int32)
            self._sprite_draw_mask = np.empty(grown, dtype=bool)
            self._sprite_depth_scratch = np.empty(grown, dtype=np.float64)
            self._sprite_depth_aux_scratch = np.empty(grown, dtype=np.float64)
            self._sprite_sorted_slots_scratch = np.empty(grown, dtype=np.int32)

        textures = self._sprite_texture_scratch[:count]
        drawn = self._sprite_draw_mask[:count]
        textures.fill(0)

        variants = table.effect_fire_variant[slots]
        custom_ids = table.effect_custom_id[slots]
        custom_loops = table.effect_custom_loop[slots]
        # Animated GIFs are visual-time data. Sample the clock on the render
        # thread rather than consuming the logic thread's per-frame elapsed
        # snapshot. That removes visible frame quantisation when render and
        # logic rates differ, while keeping the dense EntityTable as the source
        # of the animation's spawn timestamps.
        elapsed = np.maximum(
            time.perf_counter() - table.effect_spawn_time[slots], 0.0
        ).astype(np.float32, copy=False)

        # There are only five authored variants per animated Effect family.
        # This bounded 10-way loop replaces an entity-by-entity Python loop
        # while allowing FIRE and ORB to use separate GIF sets.
        for effect_kind, frame_store, cumulative_store in (
            (0, self.effect_fire_frames, self.effect_fire_cumulative),
            (2, self.effect_orb_frames, self.effect_orb_cumulative),
        ):
            kind_mask = effect_types == effect_kind
            if not np.any(kind_mask):
                continue
            kind_variants = variants[kind_mask]
            kind_elapsed = elapsed[kind_mask]
            kind_positions = np.flatnonzero(kind_mask)
            for variant in range(5):
                mask = kind_variants == variant
                if not np.any(mask):
                    continue
                frames = frame_store.get(variant, ())
                cumulative = cumulative_store.get(variant)
                if not frames or cumulative is None or not len(cumulative):
                    continue

                local_elapsed = np.mod(
                    kind_elapsed[mask]
                    + table.effect_phase[slots][kind_mask][mask] * cumulative[-1],
                    cumulative[-1]
                )
                frame_indices = np.searchsorted(
                    cumulative, local_elapsed, side='right'
                )
                frame_indices = np.minimum(
                    frame_indices, len(frames) - 1
                ).astype(np.int32, copy=False)
                positions = kind_positions[np.flatnonzero(mask)]
                textures[positions] = np.asarray(
                    frames, dtype=np.int32
                )[frame_indices]

        # CUSTOM can use any GIF path. Iterate only over unique authored GIFs,
        # never over entities; each path is decoded and uploaded once per renderer.
        custom_mask = effect_types == 3
        if np.any(custom_mask):
            custom_positions = np.flatnonzero(custom_mask)
            custom_values = custom_ids[custom_mask]
            custom_elapsed = elapsed[custom_mask]
            for custom_id in np.unique(custom_values):
                custom_id = int(custom_id)
                if custom_id <= 0:
                    continue
                frames = self.effect_custom_frames.get(custom_id)
                cumulative = self.effect_custom_cumulative.get(custom_id)
                if frames is None:
                    path = table.effect_custom_path(custom_id)
                    if path:
                        frames, cumulative = self._load_fire_gif(path)
                    else:
                        frames, cumulative = (), np.empty(0, dtype=np.float32)
                    self.effect_custom_frames[custom_id] = tuple(frames)
                    self.effect_custom_cumulative[custom_id] = cumulative
                if not frames or cumulative is None or not len(cumulative):
                    continue

                mask = custom_values == custom_id
                loop_values = custom_loops[mask]
                raw_elapsed = custom_elapsed[mask]
                # CUSTOM follows its authored Loop flag. When looping is off,
                # hold the final GIF frame instead of wrapping to frame 1.
                phase_values = table.effect_phase[slots][custom_mask][mask]
                local_elapsed = np.where(
                    loop_values,
                    np.mod(raw_elapsed + phase_values * cumulative[-1], cumulative[-1]),
                    np.minimum(raw_elapsed + phase_values * cumulative[-1], cumulative[-1]),
                )
                frame_indices = np.searchsorted(
                    cumulative, local_elapsed, side='right'
                )
                frame_indices = np.minimum(
                    frame_indices, len(frames) - 1
                ).astype(np.int32, copy=False)
                positions = custom_positions[np.flatnonzero(mask)]
                textures[positions] = np.asarray(
                    frames, dtype=np.int32
                )[frame_indices]

        drawn[:] = textures > 0
        valid_count = int(np.count_nonzero(drawn))
        if valid_count == 0:
            return 0
        if valid_count != count:
            slots = slots[drawn]
            textures = textures[drawn]

        if camera_pos is None:
            order, run_starts = sort_into_runs(textures)
            ordered_textures = textures[order]
        else:
            # Blended with depth writes off, like the entity sprites: the
            # draw order is back to front and runs are only the equal-frame
            # stretches that order happens to contain.
            cx, _, cz = self._camera_xyz(camera_pos)
            fire_count = len(slots)
            depth_sq = self._sprite_depth_scratch[:fire_count]
            depth_aux = self._sprite_depth_aux_scratch[:fire_count]
            np.take(table.pos[:, 0], slots, out=depth_sq)
            np.subtract(depth_sq, cx, out=depth_sq)
            np.square(depth_sq, out=depth_sq)
            np.take(table.pos[:, 2], slots, out=depth_aux)
            np.subtract(depth_aux, cz, out=depth_aux)
            np.square(depth_aux, out=depth_aux)
            np.add(depth_sq, depth_aux, out=depth_sq)
            np.negative(depth_sq, out=depth_aux)
            order = np.argsort(depth_aux, kind='stable')
            ordered_textures = textures[order]
            run_starts = runs_in_order(ordered_textures)

        count = len(order)
        self._ensure_sprite_instance_buffer(count)
        data = self._sprite_instance_data[:count]
        sorted_slots = self._sprite_sorted_slots_scratch[:count]
        np.take(slots, order, out=sorted_slots)
        np.take(table.pos, sorted_slots, axis=0, out=data[:, 0:3])
        np.take(table.sprite_size, sorted_slots, axis=0, out=data[:, 3:5])
        # The staging array is shared with the entity sprite pass: columns
        # this pass does not use must still be written, or each flame would
        # inherit whatever yaw and opacity the last sprite at that index had.
        data[:, 5] = -10000.0
        data[:, 6] = 1.0
        data[:, 7] = 0.0

        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._sprite_instance_vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, data)

        shader, uniforms = (
            self.shaders['sprite_instanced'],
            self.uniforms['sprite_instanced'],
        )
        gl.glUseProgram(shader)
        self._current_shader = shader
        self._upload_env_uniforms('sprite_instanced')
        gl.glUniform1i(uniforms['use_fixed_facing'], 0)
        gl.glUniformMatrix4fv(
            uniforms['projection'], 1, gl.GL_FALSE, glm.value_ptr(projection)
        )
        gl.glUniformMatrix4fv(
            uniforms['view'], 1, gl.GL_FALSE, glm.value_ptr(view)
        )
        gl.glActiveTexture(gl.GL_TEXTURE0)
        gl.glUniform1i(uniforms['sprite_texture'], 0)
        gl.glBindVertexArray(self._ensure_sprite_instance_vao())

        current_tex = None
        for run in range(len(run_starts) - 1):
            begin = int(run_starts[run])
            length = int(run_starts[run + 1]) - begin
            if length <= 0:
                continue
            tex_id = int(ordered_textures[begin])
            if tex_id != current_tex:
                gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                current_tex = tex_id
                self.render_stats.batched_draws += 1
            self._point_sprite_instances_at(begin)
            gl.glDrawArraysInstanced(
                gl.GL_TRIANGLE_STRIP, 0, 4, length
            )
            self.render_stats.draw_calls += 1

        gl.glBindVertexArray(0)
        return count

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

    # --------------------------------------------------------------------------
    # Water / Glass / Fog
    # --------------------------------------------------------------------------
    @timed_pass('water')
    def draw_water_brushes(self, projection, view, camera_pos, brushes, lights, config,
                           table):
        """Draw water from dense RenderTable state.

        The pass captures the opaque scene once for screen-space transmission.
        Optional environment cubemaps are already rendered by render_scene before
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
        # Quality is per brush ("High quality" in the property editor). The
        # depth copy is made once, and only if some brush in the pass wants
        # it; the renderer's water_quality is a session-only debug cap
        # (r_waterquality cheap forces every brush cheap).
        high_quality = table.water_high_quality[brushes]
        if getattr(self, 'water_quality', 'expensive') != 'expensive':
            high_quality = np.zeros_like(high_quality)
        has_depth = bool(high_quality.any()) and self._capture_scene_depth()
        depth_unit = self._water_depth_texture_unit
        gl.glActiveTexture(gl.GL_TEXTURE0 + depth_unit)
        gl.glBindTexture(gl.GL_TEXTURE_2D,
                         self._water_depth_texture if has_depth else 0)
        gl.glUniform1i(uniforms['sceneDepth'], depth_unit)
        has_depth_loc = uniforms['hasSceneDepth']
        ssr_loc = uniforms['ssrEnabled']
        brush_depth = None
        if not has_depth:
            gl.glUniform1i(has_depth_loc, 0)
            gl.glUniform1i(ssr_loc, 0)
        # Keep the inverse alive: value_ptr() only borrows its storage.
        inv_projection = glm.inverse(projection)
        gl.glUniformMatrix4fv(
            uniforms['invProjection'], 1, gl.GL_FALSE,
            glm.value_ptr(inv_projection))

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

        opacity_loc = uniforms['waterOpacity']
        reflectivity_loc = uniforms['waterReflectivity']
        fresnel_loc = uniforms['fresnelIntensity']
        distortion_loc = uniforms['distortionStrength']
        refraction_loc = uniforms['refractionIndex']
        roughness_loc = uniforms['roughness']
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
            if has_depth:
                wants_depth = bool(high_quality[i])
                if wants_depth != brush_depth:
                    gl.glUniform1i(has_depth_loc, 1 if wants_depth else 0)
                    gl.glUniform1i(ssr_loc, 1 if wants_depth else 0)
                    brush_depth = wants_depth

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

    def _read_framebuffer_has_depth(self):
        """Whether the bound read framebuffer has a single-sampled depth buffer.

        Asked up front rather than by provoking a GL error, so an unrelated
        error already queued by an earlier pass is never swallowed here.
        """
        try:
            if int(gl.glGetIntegerv(gl.GL_SAMPLE_BUFFERS)) != 0:
                return False
            fbo = int(gl.glGetIntegerv(gl.GL_READ_FRAMEBUFFER_BINDING))
            attachment = gl.GL_DEPTH if fbo == 0 else gl.GL_DEPTH_ATTACHMENT
            kind = gl.glGetFramebufferAttachmentParameteriv(
                gl.GL_READ_FRAMEBUFFER, attachment,
                gl.GL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE)
            if int(kind) == gl.GL_NONE:
                return False
            bits = gl.glGetFramebufferAttachmentParameteriv(
                gl.GL_READ_FRAMEBUFFER, attachment,
                gl.GL_FRAMEBUFFER_ATTACHMENT_DEPTH_SIZE)
            return int(bits) > 0
        except Exception:
            return False

    def _capture_scene_depth(self):
        """Copy the current depth buffer into the water depth texture.

        Plain GL 3.3 core: one ``glCopyTexSubImage2D`` into a depth texture,
        like the colour copy in :meth:`_capture_glass_scene`. Returns False
        (and the water keeps its depth-less look) when there is no
        single-sampled depth buffer to copy from.
        """
        viewport = gl.glGetIntegerv(gl.GL_VIEWPORT)
        if viewport is None or len(viewport) < 4:
            return False
        x, y, width, height = (int(viewport[0]), int(viewport[1]),
                               int(viewport[2]), int(viewport[3]))
        if width <= 0 or height <= 0 or not self._read_framebuffer_has_depth():
            return False

        if not self._water_depth_texture:
            self._water_depth_texture = int(gl.glGenTextures(1))
        gl.glActiveTexture(gl.GL_TEXTURE0 + self._water_depth_texture_unit)
        gl.glBindTexture(gl.GL_TEXTURE_2D, self._water_depth_texture)
        if self._water_depth_size != (width, height):
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_COMPARE_MODE, gl.GL_NONE)
            gl.glTexImage2D(
                gl.GL_TEXTURE_2D, 0, gl.GL_DEPTH_COMPONENT24, width, height, 0,
                gl.GL_DEPTH_COMPONENT, gl.GL_FLOAT, None)
            self._water_depth_size = (width, height)
        gl.glCopyTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, x, y, width, height)
        return True

    @timed_pass('glass')
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

    @timed_pass('fog')
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

        # A fog volume's faces usually lie exactly on the floor, walls and
        # ceiling that bound it. Pulled a hair towards the camera, they win
        # that depth tie instead of fighting it pixel by pixel; a surface
        # really in front of a face still hides it.
        offset = getattr(self, 'fog_face_offset', True)
        if offset:
            gl.glEnable(gl.GL_POLYGON_OFFSET_FILL)
            gl.glPolygonOffset(-1.0, -1.0)

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
                # All six faces. The bottom used to be left out (a pattern
                # copied from the water pass, where the floor hides it), so
                # from inside a fog volume nothing covered the floor and it
                # showed through unfogged while the walls and ceiling fogged.
                gl.glCullFace(gl.GL_FRONT)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
                gl.glCullFace(gl.GL_BACK)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
        if offset:
            gl.glDisable(gl.GL_POLYGON_OFFSET_FILL)
        gl.glDisable(gl.GL_CULL_FACE)
        gl.glBindVertexArray(0)
        gl.glActiveTexture(gl.GL_TEXTURE0)
        return
