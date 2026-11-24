"""The Textured display mode: brushes drawn with their textures.

Box brushes go through the instanced face-run path (one instance per face,
grouped into runs by texture); angled brushes draw their mesh's face runs,
batched where the geometry store allows. The Unlit render mode and the
textured part of Overlay draw through here too.
"""


import glm
import numpy as np
import OpenGL.GL as gl
from engine.renderer.core import gl_state

from engine import render_table
from engine.render_keys import sort_into_runs
from ...core.diagnostics import timed_pass
from ...core.uniform_rows import uniform_rows
from ...core.geometry import RUN_NODRAW, RUN_SKIP
from ..instancing import GEO_BATCH_KEY, GEO_RUN_KEY
from ...core.raw_calls import draw_elements, uniform_matrix4


class TexturedMixin:
    """The Textured display mode: brushes drawn with their textures."""

    def _face_derived(self, table, slots, config):
        """The textured pass's box faces for *slots* (see
        :meth:`_build_face_batches`), with each face's slot, UV shift and
        rotation, its instance payload and the per-run draw state."""
        rows, faces, gl_tex, scales, run_starts = self._build_face_batches(
            table, slots, config)
        item_slots = slots[rows]
        shifts = table.uv_shift[item_slots, faces]
        payload = np.empty((len(item_slots), 4), dtype=np.float32)
        payload[:, 0:2] = scales
        payload[:, 2:4] = shifts
        run_texture, run_first = self._run_descriptors(gl_tex, faces, run_starts)
        bits = table.class_bits[slots]
        return {'rows': rows, 'faces': faces, 'gl_tex': gl_tex, 'scales': scales,
                'run_starts': run_starts, 'item_slots': item_slots,
                'shifts': shifts,
                'angles': np.radians(table.uv_angle[item_slots, faces]),
                'payload': payload, 'run_texture': run_texture,
                'run_first': run_first,
                'has_geometry': bool(
                    ((bits & render_table.CLASS_HAS_GEOMETRY) != 0).any())}

    def _draw_face_runs_instanced(self, projection, view, lights, table, key,
                                  derived):
        """Bring the textured pass's instances up to date (one per face
        drawn; *derived*: what :meth:`_face_derived` made of the rows) and
        hand its runs to the GPU."""
        stream = self._brush_stream(key, table, derived['item_slots'],
                                    derived['angles'], derived['payload'])
        self._submit_brush_runs('brush_instanced', projection, view, lights,
                                derived['run_starts'], derived['run_texture'],
                                derived['run_first'], 6, stream=stream)
        gl_state.glActiveTexture(gl.GL_TEXTURE0)

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
        gl_state.glUseProgram(shader)
        self._current_shader = shader
        self._upload_lights_once('textured', lights)
        emissive_loc = uniforms['emissive_factor']
        if emissive_loc is not None and emissive_loc != -1:
            gl_state.glUniform3f(emissive_loc, 0.0, 0.0, 0.0)

        proj_ptr = glm.value_ptr(projection)
        view_ptr = glm.value_ptr(view)
        uniform_matrix4(uniforms['projection'], proj_ptr)
        uniform_matrix4(uniforms['view'], view_ptr)
        gl_state.glActiveTexture(gl.GL_TEXTURE0)
        gl_state.glUniform1i(uniforms['texture_diffuse'], 0)
        gl_state.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl_state.glBindVertexArray(self.vaos['cube'])
        model_loc = uniforms['model']

        tex_scale_loc = uniforms.get('tex_scale', -1)
        if tex_scale_loc == -1:
            loc = gl.glGetUniformLocation(shader, "tex_scale")
            uniforms['tex_scale'] = loc
            tex_scale_loc = loc

        tex_angle_loc = uniforms.get('tex_angle', -1)
        if tex_angle_loc == -1:
            loc = gl.glGetUniformLocation(shader, "tex_angle")
            uniforms['tex_angle'] = loc
            tex_angle_loc = loc

        tex_shift_loc = uniforms.get('tex_shift', -1)
        if tex_shift_loc == -1:
            loc = gl.glGetUniformLocation(shader, "tex_shift")
            uniforms['tex_shift'] = loc
            tex_shift_loc = loc

        normal_mat_loc = uniforms.get('normalMatrix', -1)
        if normal_mat_loc is None:
            normal_mat_loc = -1

        is_play = config.get('play_mode', False)
        key = self._stream_key('textured')
        # Every box face this pass draws -- texture, run order, UV scale,
        # shift and rotation -- kept while the rows, their material and
        # extents, and the texture ids and sizes are unchanged (M3.1): the
        # face state of a box is resolved once per edit, as an angled
        # brush's is once per mesh build.
        derived = self._pass_memo(
            key, table, slots, (bool(is_play),),
            (self._gl_texture_ids(table), self._texture_sizes_by_name_id(table)),
            lambda: self._face_derived(table, slots, config))
        rows, faces, gl_tex = derived['rows'], derived['faces'], derived['gl_tex']
        scales, shifts, angles = derived['scales'], derived['shifts'], derived['angles']
        sel_slots = derived['item_slots']

        self._portal_begin_cull(is_geo=False)
        if self.debug_gl_state:
            self._debug_textured_brush_gl_state()

        current_tex = None
        instanced = (len(rows) > 0 and 'brush_instanced' in self.shaders
                     and self._cube_vbo is not None)
        # The kept matrices (SlotTransforms), gathered only for what reads
        # them: the convex half (_draw_geo_runs) and the per-face fallback.
        if derived['has_geometry'] or (len(rows) and not instanced):
            models, normals = self._frame_transforms(table, slots)
        else:
            models = normals = None
        if instanced:
            self._draw_face_runs_instanced(
                projection, view, lights, table, key, derived)
            gl_state.glUseProgram(shader)
            self._current_shader = shader
            gl_state.glActiveTexture(gl.GL_TEXTURE0)
            gl_state.glBindVertexArray(self.vaos['cube'])
        elif len(rows):
            # No box faces (every visible brush angled, or all caulk/nodraw):
            # nothing to draw here, and no matrices were gathered for it.
            last_row = -1
            upload_model = uniform_rows('mat4', models)
            upload_normal = uniform_rows('mat3', normals)
            for i in range(len(rows)):
                tex_id = int(gl_tex[i])
                if tex_id != current_tex:
                    gl_state.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                    current_tex = tex_id
                    self.render_stats.batched_draws += 1
                row = int(rows[i])
                if row != last_row:
                    upload_model(model_loc, row)
                    if normal_mat_loc != -1:
                        upload_normal(normal_mat_loc, row)
                    last_row = row
                if tex_angle_loc != -1:
                    gl_state.glUniform1f(tex_angle_loc, float(angles[i]))
                if tex_shift_loc != -1:
                    gl_state.glUniform2f(
                        tex_shift_loc, float(shifts[i, 0]), float(shifts[i, 1]))
                if tex_scale_loc != -1:
                    gl_state.glUniform2f(
                        tex_scale_loc, float(scales[i, 0]), float(scales[i, 1]))
                gl.glDrawArrays(gl.GL_TRIANGLES, int(faces[i]) * 6, 6)
                self.render_stats.visible_tris += 2
                self.render_stats.draw_calls += 1

        # Convex geometry, from the per-face state resolved when each mesh was
        # built (see _geo_run_draw_list and _draw_geo_runs).
        self._portal_set_cull(is_geo=True)
        self._draw_geo_runs(projection, view, lights, table, slots, models,
                            normals, uniforms, is_play, current_tex)
        self._portal_end_cull()
        gl_state.glBindVertexArray(0)

    def _geo_visible_faces(self, table, slots, is_play):
        """Every convex face the textured pass draws for *slots*, unsorted.

        Column arithmetic only: the visible rows' geometry handles index the
        renderer's per-table handle arrays, which give each mesh's face rows
        in the face-run arena (resolved when the mesh was built); those are
        expanded and filtered (caulk always, nodraw in play) by a bit test.
        Returns ``None`` or ``(geo, runs, rows, handles)``: the handle arrays,
        then per face its arena row, the row of *slots*' transforms and its
        geometry handle.
        """
        bits = table.class_bits[slots]
        geo_rows = np.flatnonzero(bits & render_table.CLASS_HAS_GEOMETRY)
        if not len(geo_rows):
            return None
        handles = table.geometry_id[slots[geo_rows]]
        drawable = handles >= 0
        if not drawable.all():
            geo_rows, handles = geo_rows[drawable], handles[drawable]
        if not len(handles):
            return None
        geo = self._geo_handles(table)
        built = geo.vao[handles] != 0
        if not built.all():
            geo_rows, handles = geo_rows[built], handles[built]
        lengths = geo.run_len[handles]
        total = int(lengths.sum())
        if not total:
            return None
        # CSR expansion: each face's arena row is its mesh's base plus its
        # offset within the mesh.
        owner = np.repeat(np.arange(len(handles), dtype=np.intp), lengths)
        runs = np.arange(total, dtype=np.intp)
        runs += np.repeat(geo.run_base[handles] - (np.cumsum(lengths) - lengths),
                          lengths)
        arena = self._geo_runs()
        drop = RUN_SKIP | (RUN_NODRAW if is_play else 0)
        keep = (arena.bits[runs] & drop) == 0
        if not keep.all():
            runs, owner = runs[keep], owner[keep]
        if not len(runs):
            return None
        return geo, runs, geo_rows[owner], handles[owner]

    def _geo_run_draw_list(self, table, slots, is_play):
        """The per-mesh path's draws for *slots*: :meth:`_geo_visible_faces`
        sorted by (texture, geometry), with the group boundaries -- draws in
        one group share their texture and their mesh."""
        faces = self._geo_visible_faces(table, slots, is_play)
        if faces is None:
            return None
        geo, runs, rows, handles = faces
        return (geo,) + self._sort_geo_faces(runs, rows, handles)

    def _sort_geo_faces(self, runs, rows, handles):
        order, groups = sort_into_runs(GEO_RUN_KEY.pack(
            texture=self._geo_runs().texture[runs], geometry=handles))
        return runs[order], rows[order], handles[order], groups

    def _begin_geo_face_pass(self, projection, view, lights, name='geo_textured'):
        """Bind a convex-face program and its per-pass state; its uniforms."""
        program = self.shaders[name]
        uniforms = self.uniforms[name]
        gl_state.glUseProgram(program)
        self._current_shader = program
        self._upload_lights_once(name, lights)
        uniform_matrix4(uniforms['projection'], glm.value_ptr(projection))
        uniform_matrix4(uniforms['view'], glm.value_ptr(view))
        gl_state.glActiveTexture(gl.GL_TEXTURE0)
        gl_state.glUniform1i(uniforms['texture_diffuse'], 0)
        return uniforms

    def _draw_geo_runs(self, projection, view, lights, table, slots, models,
                       normals, uniforms, is_play, current_tex):
        """Submit the textured pass's convex faces.

        Batched (:attr:`geo_batching`) when ``geo_batched`` exists: every face
        whose mesh is in the shared store and drawn by exactly one row goes
        out in one draw per (texture, page). Anything else -- a mesh two rows
        share (one transform slot cannot hold two transforms), batching off,
        a driver without the program -- takes the per-mesh path.
        """
        faces = self._geo_visible_faces(table, slots, is_play)
        if faces is None:
            return
        geo, runs, rows, handles = faces
        if self.geo_batching != 'off' and 'geo_batched' in self.shaders:
            batched = self._geo_batchable(geo, handles, self._geo_store())
            if batched.any():
                current_tex = self._draw_geo_batched(
                    projection, view, lights, geo, runs[batched], rows[batched],
                    handles[batched], models, normals, current_tex)
            if batched.all():
                return
            rest = ~batched
            runs, rows, handles = runs[rest], rows[rest], handles[rest]
        self._draw_geo_per_mesh(projection, view, lights, geo,
                                *self._sort_geo_faces(runs, rows, handles),
                                models, normals, uniforms, current_tex)

    @staticmethod
    def _geo_batchable(geo, handles, store):
        """Faces the batched path can draw: stored in a page it can index,
        and their mesh's transform slot used by no other visible row."""
        pages = geo.page[handles]
        stored = pages >= 0
        if stored.any():
            stored[stored] = store.batchable(pages[stored])
        unique_handles = np.unique(handles[stored])
        slots = geo.slot[unique_handles]
        values, counts = np.unique(slots, return_counts=True)
        if (counts > 1).any():
            shared = values[counts > 1]
            stored &= ~np.isin(geo.slot[handles], shared)
        return stored

    def _draw_geo_batched(self, projection, view, lights, geo, runs, rows,
                          handles, models, normals, current_tex):
        """One draw per (texture, page): the shared store's batched path.

        Per pass: the visible brushes' transforms are written to their slots
        (uploaded only where they changed), the faces are sorted by (texture,
        page) and vertex offset, and ranges that touch -- a mesh's adjacent
        faces, adjacent meshes in a page -- become one. The loop then binds a
        texture or a page when it changes and issues the group's single draw.
        """
        store = self._geo_store()
        arena = self._geo_runs()
        unique_handles, first_face = np.unique(handles, return_index=True)
        store.write_transforms(geo.slot[unique_handles],
                               models[rows[first_face]], normals[rows[first_face]])
        page = geo.page[handles]
        first = geo.start[handles] + arena.first[runs]
        count = arena.count[runs].astype(np.int64)
        texture = arena.texture[runs]
        order, groups = sort_into_runs(
            GEO_BATCH_KEY.pack(texture=texture, page=page), secondary=first)
        first, count = first[order], count[order]
        texture, page = texture[order], page[order]
        follows = np.zeros(len(first), dtype=bool)
        follows[1:] = first[1:] == first[:-1] + count[:-1]
        follows[groups[:-1]] = False
        starts = np.flatnonzero(~follows)
        first = first[starts].astype(np.int32)
        count = np.add.reduceat(count, starts).astype(np.int32)
        bounds = np.searchsorted(starts, groups)

        self._begin_geo_face_pass(projection, view, lights, 'geo_batched')
        gl_state.glActiveTexture(gl.GL_TEXTURE0 + self.GEO_TRANSFORM_UNIT)
        gl_state.glBindTexture(gl.GL_TEXTURE_BUFFER, store.transform_texture)
        gl_state.glUniform1i(self.uniforms['geo_batched']['uTransforms'],
                       self.GEO_TRANSFORM_UNIT)
        gl_state.glActiveTexture(gl.GL_TEXTURE0)
        heads = groups[:-1]
        group_tex = texture[heads].tolist()
        group_page = page[heads].tolist()
        group_vao = [store.page_vao(p) for p in group_page]
        ends = bounds[1:].tolist()
        elements = self.geo_batching == 'elements'
        if elements:
            store.begin_pass()
        bound_vao = None
        begin = 0
        for group, end in enumerate(ends):
            tex_id = group_tex[group]
            if tex_id != current_tex:
                gl_state.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                current_tex = tex_id
            vao = group_vao[group]
            if vao != bound_vao:
                gl_state.glBindVertexArray(vao)
                bound_vao = vao
            if elements:
                # Element-array binding is VAO state: bound after the VAO.
                gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, store.index_buffer(
                    (tex_id, group_page[group]), first[begin:end], count[begin:end]))
                draw_elements(gl.GL_TRIANGLES, int(count[begin:end].sum()),
                              gl.GL_UNSIGNED_SHORT, 0)
            else:
                gl.glMultiDrawArrays(gl.GL_TRIANGLES, first[begin:end],
                                     count[begin:end], end - begin)
            begin = end
        gl_state.glBindVertexArray(0)
        self.render_stats.draw_calls += len(ends)
        self.render_stats.visible_tris += int(count.sum()) // 3
        return current_tex

    def _draw_geo_per_mesh(self, projection, view, lights, geo, runs, rows,
                           handles, groups, models, normals, uniforms,
                           current_tex):
        """The per-mesh path: per (texture, mesh) group a VAO bind and a
        transform upload, then the group's draws.

        With ``geo_textured`` (the usual case) a face's UV state is in its
        mesh's vertex data, so a draw is just a vertex range -- and faces of
        one mesh that share a texture and sit next to each other in its
        buffer are one range, one draw. Without it, each face's three UV
        uniforms are uploaded from the arena's columns before its draw. Every
        value is read by index from columns; nothing is resolved here.
        """
        arena = self._geo_runs()
        first = arena.first[runs]
        count = arena.count[runs]
        texture = arena.texture[runs]
        per_vertex = 'geo_textured' in self.shaders
        if per_vertex:
            uniforms = self._begin_geo_face_pass(projection, view, lights)
            vaos = geo.face_vao[handles]
            # A draw continues the one before it when both are in the same
            # group and its range starts where that one's ended: merge them.
            follows = np.zeros(len(first), dtype=bool)
            follows[1:] = first[1:] == first[:-1] + count[:-1]
            follows[groups[:-1]] = False
            starts = np.flatnonzero(~follows)
            first = first[starts]
            count = np.add.reduceat(count, starts)
            draw_groups = np.searchsorted(starts, groups)
        else:
            vaos = geo.vao[handles]
            draw_groups = groups
            upload_scale = uniform_rows('vec2', arena.scale[runs])
            upload_angle = uniform_rows('float', arena.angle[runs])
            upload_shift = uniform_rows('vec2', arena.shift[runs])
            scale_loc = uniforms.get('tex_scale', -1)
            angle_loc = uniforms.get('tex_angle', -1)
            shift_loc = uniforms.get('tex_shift', -1)
        model_loc = uniforms['model']
        normal_loc = uniforms.get('normalMatrix', -1)
        if normal_loc is None:
            normal_loc = -1
        upload_model = uniform_rows('mat4', models)
        upload_normal = uniform_rows('mat3', normals)
        heads = groups[:-1]
        group_tex = texture[heads].tolist()
        group_vao = vaos[heads].tolist()
        group_row = rows[heads].tolist()
        ends = draw_groups[1:].tolist()
        first_list = first.tolist()
        count_list = count.tolist()
        bound_vao = None
        begin = 0
        for group, end in enumerate(ends):
            tex_id = group_tex[group]
            if tex_id != current_tex:
                gl_state.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
                current_tex = tex_id
            vao = group_vao[group]
            if vao != bound_vao:
                gl_state.glBindVertexArray(vao)
                bound_vao = vao
            row = group_row[group]
            upload_model(model_loc, row)
            if normal_loc != -1:
                upload_normal(normal_loc, row)
            if per_vertex:
                for draw in range(begin, end):
                    gl.glDrawArrays(gl.GL_TRIANGLES, first_list[draw],
                                    count_list[draw])
            else:
                for draw in range(begin, end):
                    if scale_loc != -1:
                        upload_scale(scale_loc, draw)
                    if angle_loc != -1:
                        upload_angle(angle_loc, draw)
                    if shift_loc != -1:
                        upload_shift(shift_loc, draw)
                    gl.glDrawArrays(gl.GL_TRIANGLES, first_list[draw],
                                    count_list[draw])
            begin = end
        # Counted once per pass: every run is a whole number of triangles.
        self.render_stats.draw_calls += len(count_list)
        self.render_stats.visible_tris += sum(count_list) // 3
        return current_tex

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
