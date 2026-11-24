"""The Solid Lit display mode: brushes shaded in their colours, no textures.

Also the pass every mode uses for brushes that carry no texture worth binding
(and for the transparent pass: triggers, selected and subtract brushes). Box
brushes are drawn instanced; angled brushes one mesh at a time.
"""


import glm
import numpy as np
import OpenGL.GL as gl
from engine.renderer.core import gl_state

from engine import render_table
from engine.render_keys import sort_into_runs
from ...core.diagnostics import timed_pass
from ...core.uniform_rows import uniform_rows
from ..instancing import (BRUSH_RUN_KEY, _OVERRIDE_COLOURS, _SELECTED_ROW,
                          _SUBTRACT_ROW, _TRIGGER_ROW)
from ...core.raw_calls import uniform_matrix4


class SolidLitMixin:
    """The Solid Lit display mode: brushes shaded in their colours, no textures."""

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
        gl_state.glUseProgram(shader)
        self._current_shader = shader
        self._upload_lights_once('lit', lights)
        emissive_loc = uniforms['emissive_factor']
        if emissive_loc is not None and emissive_loc != -1:
            gl_state.glUniform3f(emissive_loc, 0.0, 0.0, 0.0)
        # Cache value_ptr results – avoids redundant ctypes work per draw call
        proj_ptr = glm.value_ptr(projection)
        view_ptr = glm.value_ptr(view)
        uniform_matrix4(uniforms['projection'], proj_ptr)
        uniform_matrix4(uniforms['view'], view_ptr)
        gl_state.glBindVertexArray(self.vaos['cube'])
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
        gl_state.glPolygonMode(gl.GL_FRONT_AND_BACK, fill_mode)

        indices = range(len(visible))
        selected_slot = self._selected_slot(table, config)
        key = self._stream_key('lit')
        # What this pass derives from the table for these rows -- classes,
        # colours, the box rows' instance payload and run order -- kept while
        # the rows and their material are unchanged (M3.1). The selection is
        # applied to the kept payload as it is drawn, so selecting a brush
        # does not derive the pass again.
        lit = self._pass_memo(key, table, visible, (), (),
                              lambda: self._lit_derived(table, visible))
        bits, colours, geometry = lit['bits'], lit['colours'], lit['geometry']
        # Resolve convex meshes at the dense-table/cache boundary once for
        # the geometry rows in this pass. The draw loop stays integer-only:
        # geometry_id -> prepared mesh, with no slot -> Brush lookup.
        geo_meshes = (
            self._prepare_geo_meshes(table, lit['geo_slots'])
            if lit['geo_rows'] else {}
        )
        if ('lit_brush_instanced' in self.shaders
                and self._cube_vbo is not None and lit['item_slots'] is not None):
            # Every plain box brush in one submission; the angled minority
            # still needs its own mesh, so it falls through to the loop.
            # Interior faces culled as in the textured pass (a no-op outside
            # the opaque passes): from inside a brush, 'Solid Lit' showed its
            # back faces where 'Textured' showed the room beyond.
            self._portal_begin_cull(is_geo=False)
            self._draw_lit_brushes_instanced(
                projection, view, lights, table, key, lit, selected_slot,
                fill_mode)
            self._portal_end_cull()
            gl_state.glUseProgram(shader)
            self._current_shader = shader
            uniform_matrix4(uniforms['projection'], proj_ptr)
            uniform_matrix4(uniforms['view'], view_ptr)
            gl_state.glBindVertexArray(self.vaos['cube'])
            gl_state.glPolygonMode(gl.GL_FRONT_AND_BACK, fill_mode)
            indices = lit['geo_rows']

        cube_vao = self.vaos['cube']
        bound_vao = cube_vao
        # Portal virtual scene: cull each brush's interior faces so the oblique
        # clip can't expose their dark back-faces. Cube and convex-geometry
        # meshes wind oppositely, so the culled face is switched alongside the
        # VAO below. No-op in the main pass.
        self._portal_begin_cull(is_geo=False)
        # The rows drawn one at a time (angled brushes, or everything without
        # the instanced program): their matrices, by position in *visible*.
        if len(indices):
            models, normals = self._frame_transforms(table, visible)
            upload_model = uniform_rows('mat4', models)
            upload_normal = uniform_rows('mat3', normals)
            upload_colour = uniform_rows('vec3', colours)
            upload_override = uniform_rows('vec3', _OVERRIDE_COLOURS)
        for index in indices:
            slot = int(visible[index])
            upload_model(model_loc, index)
            if normal_mat_loc != -1:
                upload_normal(normal_mat_loc, index)
            row = int(bits[index])
            if row & render_table.CLASS_TRIGGER:
                upload_override(color_loc, _TRIGGER_ROW)
                alpha = 0.3
            elif slot == selected_slot:
                upload_override(color_loc, _SELECTED_ROW)
                alpha = 1.0
            elif row & render_table.CLASS_SUBTRACT:
                upload_override(color_loc, _SUBTRACT_ROW)
                alpha = 1.0
            else:
                upload_colour(color_loc, index)
                alpha = 1.0
            has_geometry = bool(row & render_table.CLASS_HAS_GEOMETRY)

            gl_state.glUniform1f(alpha_loc, alpha)
            # An angled brush draws from its own convex mesh, which is built
            # from the brush's plane set -- the one thing no column holds, and
            # the only place the numeric path needs an object.
            mesh = None
            if has_geometry:
                mesh = geo_meshes.get(int(table.geometry_id[slot]))
            if mesh is not None:
                if bound_vao != mesh.vao:
                    gl_state.glBindVertexArray(mesh.vao)
                    bound_vao = mesh.vao
                    self._portal_set_cull(is_geo=True)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)
                self.render_stats.visible_tris += mesh.count // 3
            else:
                if bound_vao != cube_vao:
                    gl_state.glBindVertexArray(cube_vao)
                    bound_vao = cube_vao
                    self._portal_set_cull(is_geo=False)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
                self.render_stats.visible_tris += 12
            self.render_stats.draw_calls += 1

        self._portal_end_cull()
        gl_state.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl_state.glBindVertexArray(0)

    def _lit_derived(self, table, visible):
        """The lit pass's view of *visible*: class bits and colours by row,
        which rows are angled (drawn one at a time) and, for the box rows,
        the slot each instance draws, its payload (with no selection: see
        :meth:`lit_payload_selecting`) and the run order.

        The lit pass binds no texture and draws the whole cube, so both key
        fields are zero for every brush and the sort yields exactly one run.
        That is worth doing through the same machinery rather than short-cut
        to a single draw: the run count falls out of the data, so when
        something does start varying per run the pass needs a field in the key
        and nothing else."""
        bits = table.class_bits[visible]
        geometry = (bits & render_table.CLASS_HAS_GEOMETRY) != 0
        geo_rows = [int(i) for i in np.flatnonzero(geometry)]
        out = {'bits': bits, 'colours': table.colour[visible],
               'geometry': geometry, 'geo_rows': geo_rows,
               'geo_slots': visible[geometry], 'item_slots': None}
        cube_rows = np.flatnonzero(~geometry).astype(np.int32)
        count = len(cube_rows)
        if count:
            payload = self.lit_instance_payload(table, visible[cube_rows])
            zeros = np.zeros(count, dtype=np.int64)
            keys = BRUSH_RUN_KEY.pack(texture=zeros, face=zeros)
            order, run_starts = sort_into_runs(keys)
            out['item_slots'] = visible[cube_rows[order]]
            out['payload'] = payload[order]
            out['run_starts'] = run_starts
            out['run_texture'], out['run_first'] = self._run_descriptors(
                zeros, zeros, run_starts)
        return out

    def _draw_lit_brushes_instanced(self, projection, view, lights, table,
                                    key, lit, selected_slot=-1,
                                    fill_mode=gl.GL_FILL):
        """Bring the lit pass's instances up to date (*lit*: what
        :meth:`_lit_derived` made of the rows) and hand its runs to the GPU."""
        payload = lit['payload']
        if selected_slot >= 0:
            # The selection applied to the kept payload, kept in turn while
            # both stand (one read-only array: the stream knows it by sight).
            held = self._selected_payloads.get(key)
            if held is not None and held[0] is payload and held[1] == selected_slot:
                payload = held[2]
            else:
                chosen = self.lit_payload_selecting(table, lit['item_slots'],
                                                    payload, selected_slot)
                chosen.flags.writeable = False
                self._selected_payloads[key] = (payload, selected_slot, chosen)
                payload = chosen
        stream = self._brush_stream(key, table, lit['item_slots'], 0.0, payload)
        self._submit_brush_runs('lit_brush_instanced', projection, view,
                                lights, lit['run_starts'], lit['run_texture'],
                                lit['run_first'], 36, fill_mode, stream)
        return len(lit['item_slots'])
