"""RenderTable / EntityTable -> GPU preparation.

Everything that turns dense table columns into what a draw consumes: batched
model/normal matrices, the brush/sprite/effect/model instance buffers and the
VAOs over them, render-key runs, per-table texture-id resolution and the
recipe-list -> GL id caches.  No pass logic lives here.
"""

import ctypes

import numpy as np
import OpenGL.GL as gl
from OpenGL.raw.GL.VERSION.GL_2_0 import (
    glVertexAttribPointer as _raw_vertex_attrib_pointer)

from engine import render_table
from engine.render_keys import KeyLayout, sort_into_runs

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


#: Recipe lists whose GL resolution a renderer keeps parked: the two render
#: buffers' entity tables, the editor's own, and a spare for a table replaced
#: by a new play session.
_PARKED_RECIPE_LISTS = 4


def _swap_recipe_cache(parked, current, recipes, fresh):
    """Park *current* (``(list, *state)``) and return *recipes*' state.

    A renderer resolves each interned recipe list once, but the lists come
    from several tables that take turns. Keyed by the list's identity, and
    the entry holds the list, so a recycled ``id`` cannot alias another.
    """
    if current[0] is not None:
        if len(parked) >= _PARKED_RECIPE_LISTS:
            parked.clear()
        parked[id(current[0])] = current
    entry = parked.pop(id(recipes), None)
    if entry is not None and entry[0] is recipes:
        return entry
    return (recipes,) + tuple(fresh)


class TablesMixin:
    """Dense-table -> GPU preparation for :class:`engine.renderer.Renderer`."""

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

    def _model_recipe_radii(self, table):
        """Bounding-sphere radius of each interned model recipe's mesh.

        Model space, measured once per recipe from the vertices the mesh was
        uploaded from. A recipe whose mesh is not loaded yet is infinite, so
        it is never culled before its bounds are known.
        """
        recipes = table.model_recipes()
        if recipes is not self._model_radius_recipes_seen:
            # As for sprites: park the other buffer's radii, do not re-measure
            # every mesh each time the buffers alternate.
            (self._model_radius_recipes_seen,
             self._model_radius_by_recipe) = _swap_recipe_cache(
                self.__dict__.setdefault('_model_radius_parked', {}),
                (self._model_radius_recipes_seen, self._model_radius_by_recipe),
                recipes, (np.zeros(0, dtype=np.float64),))
        radii = self._model_radius_by_recipe
        if len(radii) < len(recipes):
            grown = np.full(len(recipes), np.inf, dtype=np.float64)
            grown[:len(radii)] = radii
            self._model_radius_by_recipe = radii = grown
        for recipe_id in np.flatnonzero(np.isinf(radii)):
            obj = self.load_model(recipes[int(recipe_id)][0])
            vertices = getattr(obj, 'cpu_vertices', None) if obj else None
            if obj is None or not getattr(obj, 'is_loaded', False):
                continue
            if vertices is None or not len(vertices):
                radii[recipe_id] = 0.0
                continue
            radii[recipe_id] = float(np.sqrt(
                (np.asarray(vertices, dtype=np.float64)[:, :3] ** 2)
                .sum(axis=1).max()))
        return radii

    def _sprite_gl_ids(self, table):
        """``sprite id -> GL texture id``, for every recipe the table interned.

        The entity projection is GL-free, so it interns sprite *recipes* --
        ordered candidate cache keys and how to load each -- and the resolution
        to a GL id happens here, once per unique recipe, on the thread that has
        a context.  Exactly the shape :meth:`_gl_texture_ids` has for
        brush face textures.

        Each candidate is tried in the order the object path tried it: look the
        key up in the shared sprite-texture cache, and on a miss load the file
        if the recipe names one.  A recipe no candidate satisfies resolves to
        0, which is how the object path's "this sprite has no texture, draw
        nothing" is said numerically.
        """
        recipes = table.sprite_recipes()
        if recipes is not self._sprite_recipes_seen:
            # A different projection, so a different id space. The two render
            # buffers' tables alternate every frame, each with its own list,
            # so the other list's resolution is parked rather than dropped --
            # dropping it re-resolved every recipe on every frame, retrying
            # the file load of any sprite that is missing.
            (self._sprite_recipes_seen, self._sprite_gl_by_id,
             self._sprite_gl_resolved) = _swap_recipe_cache(
                self.__dict__.setdefault('_sprite_gl_parked', {}),
                (self._sprite_recipes_seen, self._sprite_gl_by_id,
                 self._sprite_gl_resolved),
                recipes, (np.zeros(0, dtype=np.int32), 0))
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

    def _gl_texture_ids(self, table):
        """Return the dense texture-id array for one RenderTable projection.

        Texture ids are projection-local, so the renderer cache is keyed by
        table identity rather than by numeric id alone. A table resolves only
        names appended since its previous use; steady-state brush drawing still
        reads an array in the hot path.
        """
        names = table.texture_names()
        key = id(table)
        entry = self._gl_tex_by_table.get(key)
        # Valid only for the same table *and* the same name list: a table
        # that adopts another's state takes a copy of its list, whose ids need
        # not match the prefix this cache resolved.
        if entry is None or entry[0] is not table or entry[1] is not names:
            cached = np.zeros(0, dtype=np.int32)
        else:
            cached = entry[2]
        if len(cached) == len(names):
            return cached
        grown = np.zeros(len(names), dtype=np.int32)
        if len(cached):
            grown[:len(cached)] = cached
        for name_id in range(len(cached), len(names)):
            name = names[name_id]
            grown[name_id] = (
                self.texture_manager.get(self._tex_cache_path(name))
                or self.load_texture_callback(name, 'textures') or 0)
        self._gl_tex_by_table[key] = (table, names, grown)
        return grown

    def _texture_sizes_by_name_id(self, table):
        """Return texture dimensions using the same projection-local boundary."""
        names = table.texture_names()
        key = id(table)
        entry = self._tex_size_by_table.get(key)
        if entry is None or entry[0] is not table or entry[1] is not names:
            cached = np.zeros((0, 2), dtype=np.float32)
        else:
            cached = entry[2]
        if len(cached) == len(names):
            return cached
        grown = np.full((len(names), 2), 128.0, dtype=np.float32)
        if len(cached):
            grown[:len(cached)] = cached
        dims = getattr(self, '_texture_dimensions', {})
        for name_id in range(len(cached), len(names)):
            w, h = dims.get(self._tex_cache_path(names[name_id]), (128, 128))
            grown[name_id] = (w, h)
        self._tex_size_by_table[key] = (table, names, grown)
        return grown

    @staticmethod
    def _selected_slot(table, config):
        """The slot of the selected brush, or -1.

        One dictionary lookup per pass, so the per-brush ``brush is selected``
        identity compare becomes an integer compare.
        """
        selected = config.get('primary_selection')
        if table is None or not isinstance(selected, dict):
            return -1
        slot = table.slot_of_id.get(selected.get('id'))
        return -1 if slot is None else int(slot)

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

    def entities_are_numeric(self, config, brush_slots=None):
        """Whether dense EntityTable state is available for this renderer."""
        etable = config.get('entity_table')
        thing_slots = config.get('visible_thing_slots')
        thing_hidden = config.get('thing_hidden')
        return (etable is not None and thing_slots is not None
                and thing_hidden is not None and len(thing_hidden) >= etable.count)
