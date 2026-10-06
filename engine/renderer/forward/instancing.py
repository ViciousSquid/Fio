"""ForwardRenderer's instance buffers.

Instance VBO/VAO layouts matching the forward shaders' attributes, packing
from dense table columns, render-key runs and the textured-face batches.
"""

import ctypes

import numpy as np
import OpenGL.GL as gl
from OpenGL.raw.GL.VERSION.GL_2_0 import (
    glVertexAttribPointer as _raw_vertex_attrib_pointer)

from engine import render_table
from engine.render_keys import KeyLayout, sort_into_runs

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


class InstancingMixin:
    """ForwardRenderer's instance buffers and packing."""

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
        # The raw entry point: the offset is a plain integer into the bound
        # buffer, so PyOpenGL's array handling and the pointer bookkeeping it
        # keeps per context (a dict write per call) buy nothing here -- and
        # they were most of the cost of a brush pass.
        for location in range(3, 11):
            _raw_vertex_attrib_pointer(
                location, 4, gl.GL_FLOAT, gl.GL_FALSE, stride,
                ctypes.c_void_p(origin + (location - 3) * 16))

    def _pack_brush_instances(self, models, normals, rows, spare, payload):
        """Pack one instance row per draw item, straight from existing arrays.

        The layout is :data:`Renderer.BRUSH_INSTANCE_ATTRS`: the model
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
        for location, size, offset in (
            (1, 3, 0), (2, 2, 12), (3, 1, 20), (4, 1, 24), (5, 1, 28)
        ):
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
        # The raw entry point, as for brush runs: an integer offset into the
        # bound buffer needs none of the wrapper's array handling.
        for location, size, offset in (
            (1, 3, 0), (2, 2, 12), (3, 1, 20), (4, 1, 24), (5, 1, 28)
        ):
            _raw_vertex_attrib_pointer(location, size, gl.GL_FLOAT, gl.GL_FALSE,
                                       stride, ctypes.c_void_p(origin + offset))

    def _ensure_effect_instance_buffer(self, count):
        if self._effect_instance_vbo is None:
            self._effect_instance_vbo = gl.glGenBuffers(1)
        if count <= self._effect_instance_capacity:
            return
        capacity = max(count, 64, self._effect_instance_capacity * 2)
        self._effect_instance_capacity = capacity
        self._effect_instance_data = np.empty(
            (capacity, self.EFFECT_INSTANCE_FLOATS), dtype=np.float32
        )
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._effect_instance_vbo)
        gl.glBufferData(
            gl.GL_ARRAY_BUFFER,
            self._effect_instance_data.nbytes,
            None,
            gl.GL_DYNAMIC_DRAW,
        )

    def _ensure_effect_instance_vao(self):
        if self._effect_instance_vao is not None:
            return self._effect_instance_vao
        self._ensure_effect_instance_buffer(1)
        vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._sprite_vbo)
        gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._effect_instance_vbo)
        stride = self.EFFECT_INSTANCE_FLOATS * 4
        for location, size, offset in (
            (1, 3, 0),
            (2, 4, 12),
            (3, 4, 28),
            (4, 4, 44),
            (5, 1, 60),
        ):
            gl.glVertexAttribPointer(
                location, size, gl.GL_FLOAT, gl.GL_FALSE,
                stride, ctypes.c_void_p(offset)
            )
            gl.glEnableVertexAttribArray(location)
            gl.glVertexAttribDivisor(location, 1)
        gl.glBindVertexArray(0)
        self._effect_instance_vao = vao
        return vao

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
            self._model_instance_data = np.empty((capacity, 29), dtype=np.float32)
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
        stride = 29 * 4
        offsets = (0, 16, 32, 48, 64, 80, 96)
        for location, offset in zip(range(3, 10), offsets):
            gl.glVertexAttribPointer(
                location, 4, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(offset))
            gl.glEnableVertexAttribArray(location)
            gl.glVertexAttribDivisor(location, 1)
        gl.glVertexAttribPointer(
            10, 1, gl.GL_FLOAT, gl.GL_FALSE, stride, ctypes.c_void_p(112))
        gl.glEnableVertexAttribArray(10)
        gl.glVertexAttribDivisor(10, 1)
        gl.glBindVertexArray(0)
        self._model_instanced_vaos.add(key)

    def _fill_model_instance_buffer_numeric(self, table, slots):
        """Gather model transforms directly from dense entity columns."""
        count = len(slots)
        self._ensure_model_instance_buffer(count)
        out = self._model_instance_data[:count]
        np.take(table.model_base_matrix, slots, axis=0, out=out[:, :16])
        np.take(table.model_normal_matrix, slots, axis=0, out=out[:, 16:28])
        np.take(table.render_alpha, slots, out=out[:, 28])
        np.take(table.pos[:, 0], slots, out=out[:, 12])
        np.take(table.pos[:, 1], slots, out=out[:, 13])
        np.take(table.pos[:, 2], slots, out=out[:, 14])
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._model_instance_vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, 0, out)

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
