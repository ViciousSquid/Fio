"""
engine/renderer_F.py  –  Forward renderer, inherits shared logic from BaseRenderer
"""

import ctypes
import glm
import OpenGL.GL as gl
import numpy as np
from collections import defaultdict
import math
import os

from .renderer_core import BaseRenderer, normalize_color
from engine import render_table
from engine import entity_table as entity_projection
from engine.render_keys import KeyLayout, sort_into_runs
from engine.brush_geometry import (brush_has_geometry, face_uses_natural_scale,
                                   geometry_signature, natural_repeats)
from engine.constants import RENDER_MODE_LIT, RENDER_MODE_UNLIT, RENDER_MODE_WIREFRAME, RENDER_MODE_VERTEX
from editor.things import Thing, Light

# Camera render-distance cull. The pure per-object geometry lives in
# engine.render_cull (GL-free, so it is unit-testable without a GL context) and
# the live radius on self.view_distance (engine.view_distance); this module
# applies the pair to the MAIN camera pass only -- never to the shadow or portal
# passes, which keep using the full scene. See _camera_distance_cull below.
from engine.render_cull import camera_xz as _cull_camera_xz

# Cube face order — index maps to the face's 6-vertex run in the cube VAO
# (face_idx * 6). Kept as a module constant so the per-frame texture batch
# build doesn't allocate a fresh list for every brush.
_CUBE_FACE_KEYS = ('south', 'north', 'west', 'east', 'down', 'top')

# The render key every brush pass sorts by. Two fields, because two things
# cannot vary inside one draw: the bound texture, and which of the cube's six
# faces the draw covers. A pass with no texture packs zero and gets one run,
# which is the honest answer rather than a special case -- see
# engine.render_keys for why the key is the boundary at all.
BRUSH_RUN_KEY = KeyLayout([('texture', 32), ('face', 3)])

# The three fixed colours the lit pass overrides a brush's own colour with.
# Module constants so the per-brush branch does not build a list every draw.
_TRIGGER_COLOR = (0.0, 1.0, 1.0)
_SELECTED_COLOR = (1.0, 1.0, 0.0)
_SUBTRACT_COLOR = (1.0, 0.0, 0.0)


class Renderer_F(BaseRenderer):
    def __init__(self, texture_loader, initial_grid_size, initial_world_size, config=None):
        super().__init__(texture_loader, initial_grid_size, initial_world_size, config)
        self._current_shader = None
        self._frame_lights_uploaded = {}

        # OpenGL diagnostics are deliberately opt-in. Keep GL state probing out
        # of the normal textured-brush hot path.
        self.debug_gl_state = False

        # Texture batch cache for draw_textured_brushes_optimized.
        # Key: tuple of (brush_id, sorted_tex_items) per brush.
        # Storing None initially forces a build on the first frame.
        self._tex_batch_cache     = None   # (defaultdict(list), geo_brush_list) | None
        self._tex_batch_cache_key = None   # last key tuple | None

        # Reused by render_scene so model discovery does not allocate a new
        # list or perform a second Python walk over the visible Thing set.
        self._model_render_buf = []

        # Persistent numeric buffers for the camera distance-cull output.
        # They stay aligned with the returned brush/Thing lists, so later
        # classification and depth sorting never have to recover positions from
        # Python objects.
        # Reusable model/normal matrix buffers for the batched transform build.
        self._brush_mat_buf = np.empty((0, 16), dtype=np.float32)
        # name id -> GL texture id / (w, h), grown as the projection interns
        # names. Resolved once per unique name, never per brush.
        self._gl_tex_by_name_id = np.zeros(0, dtype=np.int32)
        self._tex_size_by_name_id = np.zeros((0, 2), dtype=np.float32)
        self._brush_nmat_buf = np.empty((0, 9), dtype=np.float32)


    # ------------------------------------------------------------------
    # Matrix helpers – cached on the brush dict itself
    # ------------------------------------------------------------------

    def _brush_model_matrix(self, brush):
        """Return the model matrix for *brush*, recomputing only when the
        brush transform actually changes.  Result is stored directly on the
        brush dict so it survives across frames with zero extra bookkeeping.
        """
        pos   = brush.get('pos',  [0, 0, 0])
        size  = brush.get('size', [64, 64, 64])
        angle = brush.get('_rot_angle')
        axis  = tuple(brush.get('rot_axis', [0, 1, 0])) if angle else None
        key   = (pos[0], pos[1], pos[2],
                 size[0], size[1], size[2],
                 angle, axis)

        if brush.get('_mat_cache_key') == key:
            return brush['_mat_cache']

        mat = glm.translate(self._identity_mat4, glm.vec3(*pos))
        if angle:
            av = glm.vec3(*axis)
            if glm.length(av) > 0.001:
                mat = glm.rotate(mat, glm.radians(float(angle)), glm.normalize(av))
        mat = glm.scale(mat, glm.vec3(*size))
        brush['_mat_cache_key'] = key
        brush['_mat_cache']     = mat
        return mat

    def _tex_cache_path(self, tex_name):
        """Return the ``textures/<name>`` cache key for *tex_name*, memoizing the
        os.path.join. Called for every drawn face every frame in play mode, so
        the join is done once per unique texture name and reused thereafter."""
        path = self._tex_path_cache.get(tex_name)
        if path is None:
            path = os.path.join('textures', tex_name)
            self._tex_path_cache[tex_name] = path
        return path

    def set_sprite_textures(self, textures):
        self.sprite_textures = textures

    def set_instance_textures(self, textures):
        self.instance_textures = textures

    def _compute_normal_matrix(self, model_matrix, brush=None):
        """Compute the normal matrix.

        If *brush* is provided the result is cached under the same cache
        key as the model matrix, so it is only recomputed when the brush
        transform changes.  Falls back to uncached behaviour when brush is
        None (e.g. calls from base-class code that don't have a brush ref).
        """
        if brush is not None:
            mk = brush.get('_mat_cache_key')
            if mk is not None and brush.get('_nmat_cache_key') == mk:
                return brush['_nmat_cache']
            try:
                nmat = glm.transpose(glm.inverse(glm.mat3(model_matrix)))
            except Exception:
                nmat = self._identity_mat3
            brush['_nmat_cache_key'] = mk
            brush['_nmat_cache']     = nmat
            return nmat
        # No brush supplied – uncached path (should be rare)
        try:
            return glm.transpose(glm.inverse(glm.mat3(model_matrix)))
        except Exception:
            return self._identity_mat3

    # ------------------------------------------------------------------

    @staticmethod
    def _selected_slot(table, config):
        """The slot of the selected brush, or -1.

        One dictionary lookup per pass, so the per-brush ``brush is selected``
        identity compare becomes an integer compare.
        """
        selected = config.get('selected_object')
        if table is None or not isinstance(selected, dict):
            return -1
        slot = table.slot_of_id.get(selected.get('id'))
        return -1 if slot is None else int(slot)

    def draw_lit_brushes_optimized(self, projection, view, camera_pos, brushes,
                                   lights, config, is_transparent_pass=False,
                                   table=None, refs=None):
        """Flat-shaded brushes.

        With *table* and *refs*, ``brushes`` is an array of slots into the
        dense render projection and nothing here reads a brush dict except to
        fetch an angled brush's mesh.  Colour, trigger/subtract state and the
        transform all come from columns; selection is an integer compare.
        Without them it walks brush dicts, which is now limited to the
        non-threaded editor and other callers that explicitly lack a projection.
        """
        if len(brushes) == 0 or 'lit' not in self.shaders:
            return
        if table is None:
            raise RuntimeError("draw_textured_brushes_optimized requires RenderTable")
        numeric = True
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
        selected            = config.get('selected_object')
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
        if numeric:
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
            if numeric:
                slot = int(visible[index])
                brush = None
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
            else:
                brush = visible[index]
                model_matrix = self._brush_model_matrix(brush)
                gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE,
                                      glm.value_ptr(model_matrix))
                if normal_mat_loc > 0:
                    nmat = self._compute_normal_matrix(model_matrix, brush)
                    gl.glUniformMatrix3fv(normal_mat_loc, 1, gl.GL_FALSE,
                                          glm.value_ptr(nmat))
                if brush.get('is_trigger'):
                    color, alpha = _TRIGGER_COLOR, 0.3
                elif brush is selected:
                    color, alpha = _SELECTED_COLOR, 1.0
                elif brush.get('operation') == 'subtract':
                    color, alpha = _SUBTRACT_COLOR, 1.0
                else:
                    brush_tint   = brush.get('tint')
                    brush_colour = brush.get('colour')
                    color = normalize_color(brush_tint) if brush_tint \
                        else normalize_color(brush_colour)
                    alpha = 1.0
                has_geometry = brush_has_geometry(brush)
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

    def _debug_textured_brush_gl_state(self):
        """Print the VAO/program state used by the textured-brush pass.

        This is an opt-in diagnostic path only. It is intentionally called once
        before the face submission loop rather than from the per-face hot path.
        """
        print(
            '[Renderer_F] textured-brush GL state: '
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
                f'[Renderer_F] attrib{attrib}: '
                f'enabled={bool(enabled)}, '
                f'buffer={buffer}, '
                f'stride={stride}, '
                f'type=0x{attr_type:x}'
            )

    def _gl_texture_ids(self, table):
        """``name id -> GL texture id``, for every name the projection interned.

        The projection is GL-free, so it interns texture *names* to dense ints
        and the resolution to a GL id happens here, once per unique name, on
        the thread that has a context.  Tens of entries, not thousands, and the
        per-face path then reads it as an array.
        """
        names = table.texture_names()
        cached = self._gl_tex_by_name_id
        if len(cached) >= len(names):
            return cached
        grown = np.zeros(len(names), dtype=np.int32)
        grown[:len(cached)] = cached
        for name_id in range(len(cached), len(names)):
            name = names[name_id]
            grown[name_id] = (
                self.texture_manager.get(self._tex_cache_path(name))
                or self.load_texture_callback(name, 'textures') or 0)
        self._gl_tex_by_name_id = grown
        return grown

    def _texture_sizes_by_name_id(self, table):
        """``name id -> (width, height)``, for the NATURAL scale calculation.

        Same shape as :meth:`_gl_texture_ids`: resolved once per unique name so
        the per-face path is an array read rather than a dict lookup.
        """
        names = table.texture_names()
        cached = self._tex_size_by_name_id
        if len(cached) >= len(names):
            return cached
        grown = np.full((len(names), 2), 128.0, dtype=np.float32)
        grown[:len(cached)] = cached
        dims = getattr(self, '_texture_dimensions', {})
        for name_id in range(len(cached), len(names)):
            w, h = dims.get(self._tex_cache_path(names[name_id]), (128, 128))
            grown[name_id] = (w, h)
        self._tex_size_by_name_id = grown
        return grown

    @staticmethod
    def lit_instance_payload(table, row_slots, selected_slot=-1):
        """Colour and alpha per brush, as the lit pass's instance payload.

        The per-brush path chose these with an if/elif chain: trigger first,
        then the selected object, then a subtract brush, then the brush's own
        colour. Here they are masks over one array, so the *write order* is
        what encodes that priority -- lowest precedence first, because the last
        write wins. A selected trigger must still read as a trigger.

        Pure NumPy and static, so the priority rules are testable without a GL
        context; the visual tests then confirm the result actually reaches the
        screen.
        """
        count = len(row_slots)
        payload = np.ones((count, 4), dtype=np.float32)
        if not count:
            return payload
        bits = table.class_bits[row_slots]
        payload[:, 0:3] = table.colour[row_slots]

        subtract = (bits & render_table.CLASS_SUBTRACT) != 0
        if subtract.any():
            payload[subtract, 0:3] = _SUBTRACT_COLOR
        if selected_slot >= 0:
            chosen = row_slots == selected_slot
            if chosen.any():
                payload[chosen, 0:3] = _SELECTED_COLOR
        trigger = (bits & render_table.CLASS_TRIGGER) != 0
        if trigger.any():
            payload[trigger, 0:3] = _TRIGGER_COLOR
            payload[trigger, 3] = 0.3
        return payload

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

    @staticmethod
    def _run_descriptors(sorted_texture, sorted_face, run_starts):
        """Per-run GPU state, read off the first item of each run.

        Every item in a run has the same key by construction, so the first one
        speaks for all of them. Keeping this separate from the instance arrays
        is the point: these are the things that cannot vary within a draw.
        """
        heads = run_starts[:-1]
        if not len(heads):
            empty = np.empty(0, dtype=np.int32)
            return empty, empty
        return (sorted_texture[heads].astype(np.int32),
                (sorted_face[heads] * 6).astype(np.int32))

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

    def _build_face_batches(self, table, slots, config):
        """Every drawable cube face of *slots*, ordered so texture binds run out.

        The per-frame work this replaces built a Python tuple ``(brush, face
        index, face key)`` for each of up to six faces of every visible brush,
        into a dict of lists keyed by GL texture id -- and in play mode rebuilt
        all of it every frame, because its cache key was a tuple of ``id(b)``
        and a mover's snapshot copy changes identity each tick.

        Here the same grouping is a gather and an argsort over columns that
        already exist.  Returns ``(rows, faces, gl_tex, scale)``: parallel
        arrays, one entry per face to draw, ordered by texture id so that
        binding on change is all the batching that is needed.  ``rows`` indexes
        into *slots* (so the matrix arrays line up), ``faces`` is the cube face
        index whose six vertices start at ``faces * 6``.
        """
        bits = table.class_bits[slots]
        cube_rows = np.flatnonzero(
            (bits & render_table.CLASS_HAS_GEOMETRY) == 0).astype(np.int32)
        if not len(cube_rows):
            empty_i = np.empty(0, dtype=np.int32)
            return (empty_i, empty_i, empty_i,
                    np.empty((0, 2), dtype=np.float32), empty_i)

        cube_slots = slots[cube_rows]
        name_ids = table.tex_name_id[cube_slots]            # (R, 6)

        drawn = name_ids != render_table.TEX_ID_SKIP        # caulk never draws
        if config.get('play_mode', False):
            drawn &= name_ids != render_table.TEX_ID_NODRAW  # nodraw is editor-only
        drawn &= name_ids >= 0

        row_idx, face_idx = np.nonzero(drawn)
        if not len(row_idx):
            empty_i = np.empty(0, dtype=np.int32)
            return (empty_i, empty_i, empty_i,
                    np.empty((0, 2), dtype=np.float32), empty_i)

        face_names = name_ids[row_idx, face_idx]
        gl_tex = self._gl_texture_ids(table)[face_names]

        # The key says what a run must share: the texture, because binding one
        # is the expensive state change, and the cube face, because a face is
        # six consecutive vertices addressed by a per-draw parameter rather
        # than a per-instance one. Everything else that used to vary per face --
        # the transform, the UV scale, rotation and shift -- is instance data,
        # so a run is one submission however many brushes are in it.
        keys = BRUSH_RUN_KEY.pack(texture=gl_tex, face=face_idx)
        order, run_starts = sort_into_runs(keys)
        row_idx = row_idx[order]
        face_idx = face_idx[order].astype(np.int32)
        gl_tex = gl_tex[order]
        face_names = face_names[order]

        scale = self._face_uv_scales(table, cube_slots, row_idx, face_idx,
                                     face_names)
        return cube_rows[row_idx], face_idx, gl_tex, scale, run_starts

    def _face_uv_scales(self, table, cube_slots, row_idx, face_idx, face_names):
        """The ``tex_scale`` uniform for each face, as one (F, 2) array.

        Three modes, in the priority the per-face branch used: NATURAL keeps a
        constant texel size and so is recomputed from the brush's live extent;
        an authored ``uv_scale`` is used as given; otherwise the texture is
        stretched 0..1 over the face.
        """
        sel_slots = cube_slots[row_idx]
        natural = table.uv_natural[sel_slots, face_idx]
        has_scale = table.uv_has_scale[sel_slots, face_idx]
        tiling = (table.class_bits[sel_slots]
                  & render_table.CLASS_TEXTURE_TILING) != 0
        natural = natural | (~has_scale & tiling)

        scale = np.where(
            has_scale[:, None],
            table.uv_scale[sel_slots, face_idx],
            np.float32(1.0)).astype(np.float32)

        if natural.any():
            size = (table.half[sel_slots] * 2.0).astype(np.float32)
            # Face order is (south, north, west, east, down, top): the first
            # pair spans X by Y, the second Z by Y, the third X by Z.
            extent = np.empty((len(row_idx), 2), dtype=np.float32)
            side = face_idx < 2
            end = face_idx > 3
            mid = ~side & ~end
            extent[side] = size[side][:, (0, 1)]
            extent[mid] = size[mid][:, (2, 1)]
            extent[end] = size[end][:, (0, 2)]
            tex_size = self._texture_sizes_by_name_id(table)[face_names]
            np.copyto(scale, extent / np.maximum(tex_size, 1.0),
                      where=natural[:, None])
        return scale

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

    def draw_glow_brushes(self, projection, view, camera_pos, brushes, lights,
                          config, table):
        """Overbright brushes.

        With *table* and *refs*, ``brushes`` is an array of slots: the
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

        if numeric:
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

    def _get_active_lights(self, things, config):
        """Return active lights directly from the dense EntityTable."""
        table = config.get('entity_table')
        if table is None or not hasattr(table, 'light_color'):
            raise RuntimeError("dense EntityTable is required for light rendering")
        slots = table.light_slots
        if len(slots):
            slots = slots[table.light_enabled[slots]]
        return (table, slots)

    def entities_are_numeric(self, config, brush_slots=None):
        """Whether dense EntityTable state is available for this renderer."""
        etable = config.get('entity_table')
        thing_slots = config.get('visible_thing_slots')
        thing_hidden = config.get('thing_hidden')
        return (etable is not None and thing_slots is not None
                and thing_hidden is not None and len(thing_hidden) >= etable.count)

    def will_instance_sprites(self, config, brush_slots):
        """Whether the billboard pass will read columns rather than objects.

        Keep this predicate identical to the renderer's actual sprite-path
        requirements. The Qt view uses it to avoid rebuilding per-entity
        texture overrides when the dense EntityTable path will resolve its own
        textures. A missing predicate here must never force an object-based sprite
        path back into the frame; dense EntityTable instancing is the only
        supported renderer path.
        """
        return (self.entities_are_numeric(config, brush_slots)
                and 'sprite_instanced' in self.shaders)

    def _portal_numeric_scene_inputs(self, projection, view, config):
        """Resolve a portal virtual scene entirely from the dense projections.

        Portal topology, transforms and aperture geometry all come from
        EntityTable columns. The world seen through that camera never falls back
        to _sort_objects or reconstructs a brush or entity list.
        all_brush_slots is already the live-hidden-filtered
        world projection published by the logic thread; the virtual frustum
        is applied as a vector mask over RenderTable.center/half.
        EntityTable supplies the entity classification and live hidden
        filtering for the sprite pass.
        """
        table = config.get('render_table')
        if table is None:
            raise RuntimeError("Portal virtual view requires RenderTable")

        slots = config.get('all_brush_slots')
        if slots is None:
            slots = np.empty(0, dtype=np.int32)
        else:
            slots = np.asarray(slots, dtype=np.int32)

        planes = np.asarray(
            self._frustum_planes(projection * view),
            dtype=np.float64,
        )
        if len(slots):
            centres = table.center[slots]
            radii = np.linalg.norm(table.half[slots], axis=1)
            distances = centres @ planes[:, :3].T + planes[:, 3]
            slots = slots[np.all(distances >= -radii[:, None], axis=1)]

        groups = self._classify_brush_slots(table, slots, config)

        etable = config.get('entity_table')
        thing_hidden = config.get('thing_hidden')
        # Portal cameras see the world from a different frustum.  Their entity
        # input therefore starts from the dense world slot set, not the main
        # camera's already-published visible selection.  Hidden/collected rows
        # are filtered numerically; no Thing objects are materialised.
        if etable is not None and thing_hidden is not None:
            thing_slots = np.arange(etable.count, dtype=np.int32)
            if len(thing_slots):
                entity_planes = planes
                centres = etable.pos[thing_slots]
                distances = centres @ entity_planes[:, :3].T + entity_planes[:, 3]
                radii = np.maximum(
                    etable.sprite_size[thing_slots].max(axis=1) * 0.5, 1.0)
                model_rows = etable.model_recipe_id[thing_slots] >= 0
                radii[model_rows] = np.maximum(radii[model_rows], 128.0)
                entity_visible = np.all(
                    distances >= -radii[:, None], axis=1)
                thing_slots = thing_slots[entity_visible]
            model_slots, sprite_slots = entity_projection.classify_slots(
                etable,
                thing_slots,
                thing_hidden,
                config.get('play_mode', False),
                config.get('show_sprites_in_play_mode', False),
            )
        else:
            model_slots = np.empty(0, dtype=np.int32)
            sprite_slots = np.empty(0, dtype=np.int32)

        lights = self._get_active_lights((), config)
        return table, groups, model_slots, sprite_slots, lights

    def render_scene(self, projection, view, camera_pos, brushes, things,
                     selected_object, config, clear=True, brush_slots=None):
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
        self.render_stats.total_brushes = len(brushes)
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
        refs = config.get('render_refs')
        if (brush_slots is None or table is None or refs is None
                or len(refs) < table.count):
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

        _tbl = table
        _refs = refs
        lights = self._get_active_lights(things, config)
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
            self.render_terrain(projection, view, camera_pos, terrain, lights)
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
                    def _portal_draw_scene(view_state, cfg):
                        # Fog and all scene classification remain driven by the
                        # virtual camera and the same dense tables as the main view.
                        proj = view_state.projection
                        vw = view_state.view
                        cam = view_state.camera_pos
                        saved_cam = self._frame_camera_pos
                        self._frame_camera_pos = self._camera_xyz(cam)
                        try:
                            (portal_table, portal_groups,
                             portal_model_slots, portal_sprite_slots,
                             portal_lights) = self._portal_numeric_scene_inputs(
                                 proj, vw, cfg)
                            mode = cfg.get('brush_display_mode', 'Textured')
                            p_refs = cfg.get('render_refs')

                            # Match the main numeric scene pipeline: every brush
                            # material class consumes the same projected slots,
                            # narrowed only by the virtual camera frustum.
                            if mode in ('Textured', 'Solid Lit'):
                                self.draw_textured_brushes_optimized(
                                    proj, vw, cam,
                                    portal_groups['textured'], portal_lights, cfg,
                                    portal_table)
                                self.draw_lit_brushes_optimized(
                                    proj, vw, cam,
                                    portal_groups['solid'], portal_lights, cfg,
                                    table=portal_table, refs=p_refs)
                            else:
                                self.draw_lit_brushes_optimized(
                                    proj, vw, cam,
                                    portal_groups['opaque'], portal_lights, cfg,
                                    table=portal_table, refs=p_refs)

                            # Entity models use the same dense recipe/transform
                            # projection as the main camera.  No erefs[...] and no
                            # Thing list are materialised for the portal scene.
                            if len(portal_model_slots):
                                self.draw_models_instanced(
                                    proj, vw, cam,
                                    cfg.get('entity_table'),
                                    portal_model_slots,
                                    portal_lights, cfg)

                            # Match the remaining dense material passes.
                            if len(portal_groups['glow']):
                                self.draw_glow_brushes(
                                    proj, vw, cam,
                                    portal_groups['glow'], portal_lights, cfg,
                                    table=portal_table, refs=p_refs)

                            if len(portal_sprite_slots):
                                self.draw_sprites_instanced(
                                    proj, vw, cfg.get('entity_table'),
                                    portal_sprite_slots, camera_pos=cam)

                            gl.glEnable(gl.GL_BLEND)
                            gl.glDepthMask(gl.GL_FALSE)
                            if mode == RENDER_MODE_UNLIT:
                                self.draw_textured_brushes_optimized(
                                    proj, vw, cam,
                                    portal_groups['transparent'], portal_lights, cfg,
                                    portal_table)
                            else:
                                self.draw_lit_brushes_optimized(
                                    proj, vw, cam,
                                    portal_groups['transparent'], portal_lights, cfg,
                                    is_transparent_pass=True,
                                    table=portal_table, refs=p_refs)
                            self.draw_water_brushes(
                                proj, vw, cam, portal_groups['water'], portal_lights, cfg,
                                table=portal_table, refs=p_refs)
                            self.draw_glass_brushes(
                                proj, vw, cam, portal_groups['glass'], portal_lights, cfg,
                                table=portal_table, refs=p_refs)
                            self.draw_fog_volumes(
                                proj, vw, cam, portal_groups['fog'], portal_lights, cfg,
                                table=portal_table, refs=p_refs)
                            gl.glDepthMask(gl.GL_TRUE)
                        finally:
                            self._frame_camera_pos = saved_cam
                            self._frame_lights_uploaded.clear()

                    self.draw_portals(
                        portal_table,
                        portal_slots,
                        projection,
                        view,
                        camera_pos,
                        config,
                        _portal_draw_scene,
                    )
                    self._proj_ptr = glm.value_ptr(projection)
                    self._view_ptr = glm.value_ptr(view)
                except Exception as _pe:
                    print(f"[Portal] render error: {_pe}")
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_BLEND)
        brush_display_mode = config.get('brush_display_mode', 'Textured')
        if current_mode == RENDER_MODE_UNLIT:
            self.draw_textured_brushes_optimized(projection, view, camera_pos, textured_opaque, lights, config, _tbl)
            self.draw_lit_brushes_optimized(projection, view, camera_pos, solid_opaque, lights, config, table=_tbl)
        elif current_mode == RENDER_MODE_LIT:
            if brush_display_mode == 'Textured' or brush_display_mode == 'Solid Lit':
                self.draw_textured_brushes_optimized(projection, view, camera_pos, textured_opaque, lights, config, _tbl)
                self.draw_lit_brushes_optimized(projection, view, camera_pos, solid_opaque, lights, config, table=_tbl, refs=_refs)
            else:
                self.draw_lit_brushes_optimized(projection, view, camera_pos, opaque_brushes, lights, config, table=_tbl, refs=_refs)
        else:
            self.draw_lit_brushes_optimized(projection, view, camera_pos, opaque_brushes, lights, config, table=_tbl, refs=_refs)
        if len(glow_brushes):
            self.draw_glow_brushes(projection, view, camera_pos, glow_brushes, lights, config, table=_tbl, refs=_refs)
        if len(numeric_model_slots):
            if not (self.shaders.get('lit_instanced')
                    or self.shaders.get('textured_instanced')):
                raise RuntimeError(
                    "Fio 2.5 requires instanced model shaders for dense entity rendering")
            self.draw_models_instanced(
                projection, view, camera_pos, etable, numeric_model_slots,
                lights, config)
        if not config.get('play_mode', False):
            self.draw_path_node_cubes(projection, view, things)
        if etable is not None:
            self.draw_portal_wireframes(
                projection, view, etable, etable.portal_slots,
                config.get('play_mode', False))
        gl.glEnable(gl.GL_BLEND)
        gl.glDepthMask(gl.GL_FALSE)
        # The sprite renderer has one path: dense EntityTable columns -> GL
        # instanced draws. Missing projection data is a caller error, not a
        # reason to resurrect the object renderer.
        if len(sprite_slots):
            self.draw_sprites_instanced(
                projection, view, etable, sprite_slots, camera_pos=camera_pos)
        if current_mode == RENDER_MODE_UNLIT:
            self.draw_textured_brushes_optimized(projection, view, camera_pos, transparent_brushes, lights, config, _tbl)
        elif current_mode == RENDER_MODE_LIT:
            self.draw_lit_brushes_optimized(projection, view, camera_pos, transparent_brushes, lights, config, is_transparent_pass=True, table=_tbl, refs=_refs)
        else:
            self.draw_lit_brushes_optimized(projection, view, camera_pos, transparent_brushes, lights, config, is_transparent_pass=True, table=_tbl, refs=_refs)
        if current_mode == RENDER_MODE_LIT:
            self.draw_water_brushes(projection, view, camera_pos, water_brushes, lights, config,
                                     table=_tbl, refs=_refs)
            self.draw_glass_brushes(projection, view, camera_pos, glass_brushes, lights, config,
                                     table=_tbl, refs=_refs)
            self.draw_fog_volumes(projection, view, camera_pos, fog_volumes, lights, config,
                                  table=_tbl, refs=_refs)
        gl.glDepthMask(gl.GL_TRUE)
        gl.glDisable(gl.GL_DEPTH_TEST)
        gl.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        if selected_object:
            if isinstance(selected_object, dict):
                self.draw_selected_brush_outline(projection, view, selected_object)
                if selected_object.get('is_trigger', False) and selected_object.get('show_aabb_bounds', False):
                    self.draw_aabb_bounds(projection, view, selected_object)
                pos = selected_object.get('pos')
                if pos is not None and not selected_object.get('lock', False):
                    self.render_gizmo(projection, view, pos)
            elif isinstance(selected_object, Thing):
                self.render_gizmo(projection, view, selected_object.pos)
        gl.glEnable(gl.GL_DEPTH_TEST)
        gl.glDisable(gl.GL_BLEND)
        gl.glUseProgram(0)