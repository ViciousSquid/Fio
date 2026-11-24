"""HDHighRenderer's instance buffers.

Instance VBO/VAO layouts matching the forward shaders' attributes, packing
from dense table columns, render-key runs and the textured-face batches.
"""

import ctypes
import weakref

import numpy as np
import OpenGL.GL as gl
from engine.renderer.core import gl_state
from ..core.raw_calls import buffer_orphan, buffer_sub_data, vertex_attrib_pointer

from engine import change_detection, render_table
from engine.render_keys import KeyLayout, sort_into_runs
from engine.shaders import EXPLOSION_INSTANCE_LAYOUT, PARTICLES_PER_EMITTER

# The render key every brush pass sorts by. Two fields, because two things
# cannot vary inside one draw: the bound texture, and which of the cube's six
# faces the draw covers. A pass with no texture packs zero and gets one run,
# which is the honest answer rather than a special case -- see
# engine.render_keys for why the key is the boundary at all.
BRUSH_RUN_KEY = KeyLayout([('texture', 32), ('face', 3)])

# The render key of the textured pass's convex half. Each face of an angled
# brush is one draw from its own mesh, so the key says what draws can share:
# the texture first (a bind is the expensive change), then the geometry (one
# VAO bind and one transform upload for consecutive faces of the same brush).
# The texture field is the face's GL texture name, resolved when its mesh was
# built, as BRUSH_RUN_KEY's is for box faces.
GEO_RUN_KEY = KeyLayout([('texture', 32), ('geometry', 28)])

# The batched convex path's key: faces of every brush in one shared vertex
# page are one draw per texture, so (texture, page) is all that cannot vary;
# the vertex offset is the tie-break that makes adjacent ranges adjacent.
GEO_BATCH_KEY = KeyLayout([('texture', 32), ('page', 16)])

# The three fixed colours the lit pass overrides a brush's own colour with.
# Module constants so the per-brush branch does not build a list every draw.
_TRIGGER_COLOR = (0.0, 1.0, 1.0)
_SELECTED_COLOR = (1.0, 1.0, 0.0)
_SUBTRACT_COLOR = (1.0, 0.0, 0.0)


#: No rows.
_NO_ROWS = np.empty(0, dtype=np.intp)


#: "Every row": what a whole rewrite uploads (no index array is built).
_ALL_ROWS = np.empty(0, dtype=np.intp)


# The lit pass's override colours as float32 rows, so they upload through the
# same per-row path as the table's own colours (same float32 values the tuples
# converted to). Used by the Solid Lit pass and the Wireframe edges.
_OVERRIDE_COLOURS = np.array(
    [_TRIGGER_COLOR, _SELECTED_COLOR, _SUBTRACT_COLOR], dtype=np.float32)
_TRIGGER_ROW, _SELECTED_ROW, _SUBTRACT_ROW = 0, 1, 2


def _pick(value, rows):
    """*value*'s entries for *rows*: a per-row array indexed, a scalar as
    it is."""
    return value[rows] if np.ndim(value) else value


class BrushStream:
    """One pass's box instances on the GPU (renderer M3): a buffer that pass
    alone writes, its CPU mirror, and what each row was built from -- the
    slot it draws, its spare scalar and payload -- so a frame uploads only
    the rows that differ from what is there.

    ``seen`` maps ``id(SlotTransforms)`` to ``[weak ref, version, rows
    written since by another table's frame, normals]``: the version of that
    table's matrices this buffer last matched. The two double-buffered
    render tables take turns, so each keeps its own entry."""

    __slots__ = ('vbo', 'vao', 'run_vaos', 'capacity', 'mirror', 'count', 'slots', 'spare',
                 'payload', 'consts', 'seen', 'uploaded', 'index', 'given')

    def __init__(self):
        self.vbo = None
        self.vao = None
        #: Vertex arrays over this buffer by the instance a run starts at
        #: (:meth:`InstancingMixin._brush_run_vao`); ``vao`` is the one at 0.
        self.run_vaos = {}
        self.capacity = 0
        self.mirror = np.zeros((0, 32), dtype=np.float32)
        self.count = 0
        self.slots = np.zeros(0, dtype=np.int32)
        self.spare = np.zeros(0, dtype=np.float32)
        self.payload = np.zeros((0, 4), dtype=np.float32)
        self.seen = {}
        #: ``(spare, payload)`` when both were last given as scalars (the
        #: arrays are then not kept), else None.
        self.consts = None
        #: Bytes this stream has uploaded (tests and tools).
        self.uploaded = 0
        #: ``(sorted slots, their positions)`` of the current items, built
        #: when first needed and kept while the items stand (M4).
        self.index = None
        #: ``(slots, spare, payload)`` as last handed in, when each is a
        #: read-only array or a scalar: the next frame's are recognised by
        #: identity, with no look at a row (M4).
        self.given = None

    def same_as_given(self, slots, spare, payload):
        """Whether *slots*, *spare* and *payload* are what was last handed
        in: the same read-only arrays (or equal scalars)."""
        given = self.given
        if given is None or given[0] is not slots or slots.flags.writeable:
            return False
        for old, new in ((given[1], spare), (given[2], payload)):
            if isinstance(new, np.ndarray) and new.ndim:
                if old is not new or new.flags.writeable:
                    return False
            elif isinstance(old, np.ndarray) and old.ndim:
                return False
            elif float(old) != float(new):
                return False
        return True

    def positions_of(self, rows, upto):
        """The positions (before *upto*) of the items that draw any of the
        slots *rows*, sorted and unique: what a change record of k rows
        costs, k searches, rather than a look at every item."""
        index = self.index
        if index is None:
            items = self.slots[:self.count]
            order = np.argsort(items, kind='stable')
            index = self.index = (items[order], order)
        ordered, order = index
        lo = np.searchsorted(ordered, rows, 'left')
        counts = np.searchsorted(ordered, rows, 'right') - lo
        total = int(counts.sum())
        if not total:
            return _NO_ROWS
        # Each row's run of positions, expanded (CSR).
        starts = np.repeat(lo - (np.cumsum(counts) - counts), counts)
        positions = order[starts + np.arange(total)]
        return np.unique(positions[positions < upto])

    def same_payload(self, count, spare, payload):
        """Whether *spare* and *payload* (scalars or per-row arrays) are what
        the rows hold."""
        scalar_spare = np.ndim(spare) == 0
        scalar_payload = np.ndim(payload) == 0
        if scalar_spare and scalar_payload:
            return self.consts == (float(spare), float(payload))
        if self.consts is not None:
            return False
        if scalar_spare:
            if not (self.spare[:count] == spare).all():
                return False
        elif not np.array_equal(spare, self.spare[:count]):
            return False
        if scalar_payload:
            return bool((self.payload[:count] == payload).all())
        return np.array_equal(payload, self.payload[:count])

    def keep_payload(self, count, spare, payload):
        """Remember what the rows now hold (*spare*, *payload*: scalars or
        per-row arrays)."""
        self.spare[:count] = spare
        self.payload[:count] = payload
        if np.ndim(spare) == 0 and np.ndim(payload) == 0:
            self.consts = (float(spare), float(payload))
        else:
            self.consts = None


class ModelInstanceStream:
    """One draw_models_instanced call's instance buffer: its GL buffer, the
    staging rows, a copy of what was last uploaded, and counts of uploads
    made and skipped (for the tests)."""

    __slots__ = ('vbo', 'data', 'held', 'uploads', 'skipped', 'plan', 'run_vaos')

    def __init__(self):
        self.vbo = None
        self.data = np.empty((0, 29), dtype=np.float32)
        self.held = None
        self.uploads = 0
        self.skipped = 0
        #: The :class:`ModelPlan` last built for this stream, while every
        #: model it draws was loaded (PassesMixin._model_plan).
        self.plan = None
        #: ``(model vertex array, first row) -> vertex array`` reading that
        #: model's vertices and this buffer's rows from that row.
        self.run_vaos = {}

    def ensure(self, count, floats):
        """Room for *count* rows in the staging array (grown geometrically);
        a grown array is a new buffer size, so what was uploaded is void."""
        if count <= len(self.data):
            return
        capacity = max(16, len(self.data))
        while capacity < count:
            capacity *= 2
        self.data = np.empty((capacity, floats), dtype=np.float32)
        self.held = None


class ModelPlan:
    """What one model pass submits, prepared: the rows in draw order and each
    run's vertex array and draws. See ``PassesMixin._model_plan``."""

    __slots__ = ('slots', 'slot_bytes', 'recipe_bytes', 'solid', 'programs', 'vbo',
                 'runs', 'sources', 'triangles')

    def __init__(self, slots, slot_bytes, recipe_bytes, solid, programs, vbo):
        #: The pass's rows, sorted into runs: what the instance buffer holds.
        self.slots = slots
        # What the plan was made from; it is used again while these hold.
        self.slot_bytes = slot_bytes
        self.recipe_bytes = recipe_bytes
        self.solid = solid
        self.programs = programs
        self.vbo = vbo
        #: ``(vertex array, instances, draws)`` per run; a draw is ``(program
        #: name, textured, emissive, texture, colour, tint, alpha, indexed,
        #: start, vertices)``.
        self.runs = []
        #: ``(recipe id, recipe, model, its draw groups, manual texture)`` per
        #: run: what each run's draws were resolved from.
        self.sources = []
        self.triangles = 0


class InstancingMixin:
    """HDHighRenderer's instance buffers and packing."""

    # -- box instance streams (M3) ----------------------------------------------

    def _stream_key(self, name):
        """The stream a pass called *name* draws from: per view (the main
        view, or a portal's), and per use within the frame -- the textured
        and lit passes each draw more than one brush group a frame -- so a
        group finds the buffer it filled last frame."""
        calls = self._stream_calls
        key = (name, self._instance_view)
        n = calls.get(key, 0)
        calls[key] = n + 1
        return (name, self._instance_view, n)


    def _brush_stream(self, key, table, slots, spare=0.0, payload=0.0,
                      normals=True):
        """Bring pass *key*'s instance buffer up to these instances and
        return it (:class:`BrushStream`): one row per item, *slots* the
        render-table slot each item draws, *spare* and *payload* scalars or
        per-item arrays (see :attr:`BRUSH_INSTANCE_ATTRS`), *normals* false
        for a pass that writes only depth.

        Each pass (per view) has its own buffer, so a frame that draws what
        the last one drew uploads nothing. What is rewritten is told without
        comparing rows: the items before the first that differs from last
        time keep their rows unless the table's :class:`SlotTransforms`
        stamps say their transform changed since this buffer last matched
        that table, another table's frame wrote them meanwhile, or their
        payload changed; everything from the first changed item on is
        rewritten; and when most of the buffer changes it is rewritten
        whole, as before M3. Uploads are coalesced ranges
        (:meth:`_upload_stream_rows`). Vectorised throughout: no per-row
        Python."""
        streams = self._brush_streams
        stream = streams.get(key)
        if stream is None:
            stream = streams[key] = BrushStream()
        cache = self._slot_transforms(table)
        cache.sync(table)
        slots = np.asarray(slots)
        count = len(slots)
        grew = self._ensure_stream_capacity(stream, count)
        seen = stream.seen
        entry = seen.get(id(cache))
        if entry is not None and (entry[0]() is not cache or entry[3] != normals):
            entry = None
        known = stream.count
        compared = not grew and known == count
        if compared and stream.same_as_given(slots, spare, payload):
            same_items = same_payload = True    # the same immutable inputs
            if change_detection.ORACLE and not (
                    np.array_equal(stream.slots[:count], slots)
                    and stream.same_payload(count, spare, payload)):
                change_detection.missed(
                    "stream %r: an immutable input changed" % (key,))
        else:
            started = change_detection.now()
            same_items = compared and change_detection.same_rows(
                stream.slots[:count], slots)
            same_payload = stream.same_payload(count, spare, payload)
            change_detection.DETECT.add(
                'stream.items',
                (count if compared else 0) + (count if np.ndim(payload) else 0),
                change_detection.now() - started)
        # Remembered for the next frame's identity test (read-only arrays
        # and scalars only: anything else is compared).
        stream.given = (slots, spare, payload) if not slots.flags.writeable else None
        if (same_items and same_payload and entry is not None
                and entry[1] == cache.version and not entry[2]):
            return stream               # nothing changed: nothing to do
        if np.ndim(spare):
            spare = np.asarray(spare, dtype=np.float32)
        else:
            spare = np.float32(spare)
        if np.ndim(payload):
            payload = np.asarray(payload, dtype=np.float32)
        else:
            payload = np.float32(payload)
        mirror = stream.mirror
        large = count >= self.STREAM_PATCH_FROM
        rows = None
        if large and same_items and entry is not None:
            # The same items: only rows whose transform changed since this
            # buffer last matched this table, that another table's frame
            # wrote meanwhile, or whose payload changed.
            head = self._stream_head(stream, cache, entry, slots, count,
                                     spare, payload, same_payload, logged=True)
            if len(head) * 2 <= count:
                if len(head):
                    mirror[head] = self._stream_block(
                        cache, slots[head], _pick(spare, head), _pick(payload, head),
                        normals)
                rows = head
        elif large and same_items:
            # A table this buffer has not matched (the other of the two
            # double-buffered tables, the first time): compared once, so
            # both keep their place.
            block = self._stream_block(cache, slots, spare, payload, normals)
            differ = np.flatnonzero((block != mirror[:count]).ravel())
            rows = np.unique(differ // self.BRUSH_INSTANCE_FLOATS)
            mirror[:count] = block
        elif large and entry is not None and known and not grew:
            # The items changed (the visible set moved, a brush was shown
            # or hidden): rows before the first changed item are kept unless
            # they changed; everything from it on is rewritten.
            kept = min(known, count)
            differ = np.flatnonzero(stream.slots[:kept] != slots[:kept])
            first = int(differ[0]) if len(differ) else kept
            if first * 2 >= count:
                head = self._stream_head(stream, cache, entry, slots, first,
                                         spare, payload, same_payload)
                if len(head):
                    mirror[head] = self._stream_block(
                        cache, slots[head], _pick(spare, head), _pick(payload, head),
                        normals)
                mirror[first:count] = self._stream_block(
                    cache, slots[first:], _pick(spare, slice(first, None)),
                    _pick(payload, slice(first, None)), normals)
                tail = np.arange(first, count, dtype=np.intp)
                rows = tail if not len(head) else np.concatenate((head, tail))
        if rows is None:
            # A small buffer, or most of one changing: rewritten whole, as
            # before M3 (comparing would cost more than it saves).
            self._stream_block(cache, slots, spare, payload, normals,
                               out=mirror[:count])
            rows = _ALL_ROWS
        if not same_items:
            stream.slots[:count] = slots
            stream.index = None
            seen.clear()
        elif rows is _ALL_ROWS:
            for other_key in [k for k in seen if k != id(cache)]:
                del seen[other_key]         # every row rewritten
        elif len(rows):
            for other_key, other in list(seen.items()):
                if other[0]() is None:
                    del seen[other_key]
                elif other_key != id(cache):
                    other[2].append(rows)
                    if sum(len(r) for r in other[2]) * 2 > count:
                        other[1] = -1           # cheaper to look at every row
                        other[2] = []
        stream.keep_payload(count, spare, payload)
        stream.count = count
        seen[id(cache)] = [weakref.ref(cache), cache.version, [], normals]
        self._upload_stream_rows(stream, rows, count)
        return stream

    @staticmethod
    def _stream_head(stream, cache, entry, slots, upto, spare, payload,
                     same_payload, logged=False):
        """Rows before *upto* that must be rewritten although their item is
        unchanged: moved since *entry* matched, written by another table's
        frame since, or with a changed payload.

        *logged* (the items are the stream's own): the moved rows come from
        the matrices' rebuild log, found among the items by search (M4);
        otherwise every item's stamp is looked at."""
        parts = []
        if entry[1] < cache.version:
            rows = cache.rows_since(entry[1]) if logged else None
            if rows is not None:
                moved = stream.positions_of(rows, upto)
            else:
                started = change_detection.now()
                moved = np.flatnonzero(cache.stamp[slots[:upto]] > entry[1])
                change_detection.DETECT.add('stream.stamps', upto,
                                            change_detection.now() - started)
            if len(moved):
                parts.append(moved)
        for written in entry[2]:
            parts.append(written[written < upto])
        if not same_payload:
            changed = ((_pick(payload, slice(0, upto)) != stream.payload[:upto]).any(axis=1)
                       | (_pick(spare, slice(0, upto)) != stream.spare[:upto]))
            parts.append(np.flatnonzero(changed))
        if not parts:
            return _NO_ROWS
        if len(parts) == 1:
            return parts[0]
        return np.unique(np.concatenate(parts))

    @staticmethod
    def _stream_block(cache, slots, spare, payload, normals, out=None):
        """Instance rows for *slots* in :attr:`BRUSH_INSTANCE_ATTRS` layout:
        the model matrix, the normal matrix padded to three vec4 with *spare*
        in the first, and *payload* -- gathered from the slot matrices, into
        *out* when given."""
        block = np.empty((len(slots), 32), dtype=np.float32) if out is None else out
        np.take(cache.models, slots, axis=0, out=block[:, 0:16])
        if normals:
            item = cache.normals[slots]
            block[:, 16:19] = item[:, 0:3]
            block[:, 20:23] = item[:, 3:6]
            block[:, 24:27] = item[:, 6:9]
            block[:, 23] = 0.0
            block[:, 27] = 0.0
        else:
            block[:, 16:28] = 0.0
        block[:, 19] = spare
        block[:, 28:32] = payload
        return block

    #: Streams of at least this many rows are patched row by row; smaller
    #: ones are rewritten whole whenever anything in them changes. Patching
    #: costs no more once the changed rows go up in few calls (below), so
    #: every stream is patched.
    STREAM_PATCH_FROM = 0

    #: Rows of unchanged data a coalesced upload may carry to save a call:
    #: an upload costs ~6 us a call and ~0.02 us a KB here (llvmpipe), and a
    #: call's fixed cost is what dominates on a mobile driver too.
    STREAM_GAP_ROWS = 128
    #: Ranges above which one upload covering all of them is cheaper.
    STREAM_MAX_RANGES = 4

    def _upload_stream_rows(self, stream, rows, count):
        """Upload *rows* (sorted, unique) of *stream*'s mirror: close rows
        coalesced into one range, many ranges into one."""
        if stream.vbo is None or not count:
            return
        stride = self.BRUSH_INSTANCE_FLOATS * 4
        if rows is _ALL_ROWS:
            bounds = ((0, count),)
        elif not len(rows):
            return
        elif int(rows[-1]) + 1 - int(rows[0]) == len(rows):
            bounds = ((int(rows[0]), int(rows[-1]) + 1),)      # one block
        else:
            breaks = np.flatnonzero(np.diff(rows) > self.STREAM_GAP_ROWS) + 1
            if len(breaks) >= self.STREAM_MAX_RANGES:
                bounds = ((int(rows[0]), int(rows[-1]) + 1),)
            else:
                ends = breaks.tolist()
                firsts = [0] + ends
                lasts = ends + [len(rows)]
                bounds = [(int(rows[a]), int(rows[b - 1]) + 1)
                          for a, b in zip(firsts, lasts)]
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, stream.vbo)
        for lo, hi in bounds:
            buffer_sub_data(gl.GL_ARRAY_BUFFER, lo * stride, stream.mirror[lo:hi])
            stream.uploaded += (hi - lo) * stride
            self.instance_bytes_uploaded += (hi - lo) * stride

    def _ensure_stream_capacity(self, stream, count):
        """Grow *stream*'s buffer and mirror to hold *count* rows; True when
        they were (re)allocated, so every row is written again."""
        if stream.vbo is None:
            stream.vbo = gl.glGenBuffers(1)
        if count <= stream.capacity and stream.vao is not None:
            return False
        capacity = max(count, 256, stream.capacity * 2)
        stream.capacity = capacity
        stream.mirror = np.zeros((capacity, self.BRUSH_INSTANCE_FLOATS),
                                 dtype=np.float32)
        stream.slots = np.full(capacity, -1, dtype=np.int32)
        stream.spare = np.zeros(capacity, dtype=np.float32)
        stream.payload = np.zeros((capacity, 4), dtype=np.float32)
        stream.count = 0
        stream.index = None
        stream.seen.clear()
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, stream.vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, stream.mirror.nbytes, None,
                        gl.GL_DYNAMIC_DRAW)
        if stream.vao is None:
            stream.vao = stream.run_vaos[0] = self._brush_stream_vao(stream.vbo)
        # A grown store keeps the buffer's name: the vertex array's pointers
        # (name, stride, offset) are still right.
        return True

    #: The most vertex arrays one stream keeps for its runs. A stable scene
    #: draws the same runs every frame; one that keeps changing would grow
    #: the set without end, so past this it starts again.
    RUN_VAOS_PER_STREAM = 256

    def _brush_run_vao(self, stream, base):
        """The vertex array that reads *stream*'s instances from row *base*.

        OpenGL 3.3 has no ``glDrawArraysInstancedBaseInstance``: a run that
        starts part way through the buffer is reached by where the instance
        attributes point. They were re-aimed for every run of every frame --
        eight calls a run, 300 a frame on the showcase, though the same runs
        start at the same rows from one frame to the next. Each start has its
        own vertex array now, made the first time a run starts there and
        bound thereafter: one call a run.
        """
        vaos = stream.run_vaos
        vao = vaos.get(base)
        if vao is None:
            if len(vaos) >= self.RUN_VAOS_PER_STREAM:
                spare = [name for row, name in vaos.items() if row]
                gl_state.glDeleteVertexArrays(len(spare), spare)
                vaos.clear()
                vaos[0] = stream.vao
                if not base:
                    return stream.vao
            vao = vaos[base] = self._brush_stream_vao(stream.vbo, base)
        return vao

    def _brush_stream_vao(self, vbo, base=0):
        """A VAO over the shared cube VBO plus one instance stream's buffer,
        its instances read from row *base*.

        Deliberately separate from ``vaos['cube']``: the non-instanced path
        shares that one, and giving it eight enabled divisor-1 attributes would
        have every ordinary cube draw read an instance buffer it does not use.
        """
        vao = gl.glGenVertexArrays(1)
        gl_state.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._cube_vbo)
        for location, size, offset in ((0, 3, 0), (1, 3, 12), (2, 2, 24)):
            vertex_attrib_pointer(location, size, gl.GL_FLOAT, gl.GL_FALSE, 32, offset)
            gl.glEnableVertexAttribArray(location)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, vbo)
        stride = self.BRUSH_INSTANCE_FLOATS * 4
        origin = int(base) * stride
        for location in range(3, 11):
            vertex_attrib_pointer(location, 4, gl.GL_FLOAT, gl.GL_FALSE, stride,
                                  origin + (location - 3) * 16)
            gl.glEnableVertexAttribArray(location)
            gl.glVertexAttribDivisor(location, 1)
        gl_state.glBindVertexArray(0)
        return vao

    def _release_brush_streams(self):
        """Delete every instance stream's buffer and vertex array (renderer
        cleanup, or a context about to be lost)."""
        for stream in self._brush_streams.values():
            if stream.run_vaos:
                names = list(stream.run_vaos.values())
                gl_state.glDeleteVertexArrays(len(names), names)
                stream.run_vaos.clear()
            if stream.vbo:
                gl.glDeleteBuffers(1, [stream.vbo])
        self._brush_streams.clear()

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

    def _upload_sprite_instances(self, data):
        """Upload *data* to the sprite instance buffer, into fresh storage.

        Entity sprites, billboards and flames share the buffer, and each
        portal view draws its sprites before the main view does: written in
        place, every upload after the first in a frame waited for the draws
        still reading the last (an implicit sync, worst on tiled mobile GPUs).
        Orphaning it first (glBufferData with no data) gives the driver new
        storage to write while those draws keep the old."""
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._sprite_instance_vbo)
        buffer_orphan(gl.GL_ARRAY_BUFFER, self._sprite_instance_data.nbytes,
                      gl.GL_DYNAMIC_DRAW)
        buffer_sub_data(gl.GL_ARRAY_BUFFER, 0, data)

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
        gl_state.glBindVertexArray(vao)
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
        gl_state.glBindVertexArray(0)
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
            vertex_attrib_pointer(location, size, gl.GL_FLOAT, gl.GL_FALSE,
                                  stride, origin + offset)

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
        # The buffer was reallocated: what it held is gone.
        self._explosion_uploaded = None

    def _ensure_effect_instance_vao(self):
        if self._effect_instance_vao is not None:
            return self._effect_instance_vao
        self._ensure_effect_instance_buffer(1)
        vao = gl.glGenVertexArrays(1)
        gl_state.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._sprite_vbo)
        gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._effect_instance_vbo)
        # One vec4 per entry of EXPLOSION_INSTANCE_LAYOUT, from location 1:
        # the layout effect.vert's declarations are generated from.
        stride = self.EFFECT_INSTANCE_FLOATS * 4
        for vec in range(len(EXPLOSION_INSTANCE_LAYOUT)):
            location = vec + 1
            gl.glVertexAttribPointer(
                location, 4, gl.GL_FLOAT, gl.GL_FALSE,
                stride, ctypes.c_void_p(vec * 16)
            )
            gl.glEnableVertexAttribArray(location)
            gl.glVertexAttribDivisor(location, 1)
        gl_state.glBindVertexArray(0)
        self._effect_instance_vao = vao
        return vao

    def _ensure_particle_instance_buffer(self, count):
        """Grow the emitter instance VBO and its staging array to *count*
        emitter rows (each feeds PARTICLES_PER_EMITTER particle instances)."""
        if self._particle_instance_vbo is None:
            self._particle_instance_vbo = gl.glGenBuffers(1)
        if count <= self._particle_instance_capacity:
            return
        capacity = max(count, 32, self._particle_instance_capacity * 2)
        self._particle_instance_capacity = capacity
        self._particle_instance_data = np.empty(
            (capacity, self.PARTICLE_INSTANCE_FLOATS), dtype=np.float32
        )
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._particle_instance_vbo)
        gl.glBufferData(
            gl.GL_ARRAY_BUFFER,
            self._particle_instance_data.nbytes,
            None,
            gl.GL_DYNAMIC_DRAW,
        )
        # The buffer was reallocated: what it held is gone.
        self._particle_uploaded = None

    def _ensure_particle_instance_vao(self):
        """The billboard quad plus the emitter rows, each row stepping once
        per PARTICLES_PER_EMITTER instances: one row is one emitter, and
        gl_InstanceID % PARTICLES_PER_EMITTER is the particle."""
        if self._particle_instance_vao is not None:
            return self._particle_instance_vao
        self._ensure_particle_instance_buffer(1)
        vao = gl.glGenVertexArrays(1)
        gl_state.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._sprite_vbo)
        gl.glVertexAttribPointer(0, 2, gl.GL_FLOAT, gl.GL_FALSE, 0, None)
        gl.glEnableVertexAttribArray(0)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self._particle_instance_vbo)
        stride = self.PARTICLE_INSTANCE_FLOATS * 4
        for location in range(1, 6):
            gl.glVertexAttribPointer(
                location, 4, gl.GL_FLOAT, gl.GL_FALSE,
                stride, ctypes.c_void_p((location - 1) * 16)
            )
            gl.glEnableVertexAttribArray(location)
            gl.glVertexAttribDivisor(location, PARTICLES_PER_EMITTER)
        gl_state.glBindVertexArray(0)
        self._particle_instance_vao = vao
        return vao

    #: Floats per model instance row: model matrix (16), normal matrix as
    #: three vec4 rows (12) and alpha.
    MODEL_INSTANCE_FLOATS = 29

    def _model_instance_stream(self, key):
        """The model instance buffer a draw_models_instanced call (*key*, from
        :meth:`_stream_key`) packs into. Each use per view has its own, as the
        brush passes' streams do: every run of one call is packed into it and
        uploaded once, where all calls used to share one buffer rewritten
        with glBufferSubData once per recipe between draws still reading it
        (an implicit sync per run on tiled mobile GPUs)."""
        streams = self.__dict__.setdefault('_model_instance_streams', {})
        stream = streams.get(key)
        if stream is None:
            stream = streams[key] = ModelInstanceStream()
        return stream

    def _fill_model_instance_stream(self, stream, table, slots):
        """Gather *slots*' model rows from the dense entity columns into
        *stream* and upload them -- once, whole, into fresh storage (orphaned,
        so no draw still reading the old rows waits) -- unless they are what
        the buffer already holds.

        The entity table keeps no version for its transform columns, so
        "unchanged" is told by comparing the gathered rows with the last
        upload: a memcmp over 116 bytes a model, against a buffer upload."""
        count = len(slots)
        stream.ensure(count, self.MODEL_INSTANCE_FLOATS)
        out = stream.data[:count]
        np.take(table.model_base_matrix, slots, axis=0, out=out[:, :16])
        np.take(table.model_normal_matrix, slots, axis=0, out=out[:, 16:28])
        np.take(table.render_alpha, slots, out=out[:, 28])
        if count <= 512:
            # One dense XYZ gather wins for ordinary mesh runs. At large N,
            # measured strided-write cost favours the three column gathers.
            np.take(table.pos, slots, axis=0, out=out[:, 12:15])
        else:
            np.take(table.pos[:, 0], slots, out=out[:, 12])
            np.take(table.pos[:, 1], slots, out=out[:, 13])
            np.take(table.pos[:, 2], slots, out=out[:, 14])
        if stream.vbo is None:
            stream.vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, stream.vbo)
        held = stream.held
        if held is not None and held.shape == out.shape and np.array_equal(held, out):
            stream.skipped += 1
            return
        buffer_orphan(gl.GL_ARRAY_BUFFER, stream.data.nbytes, gl.GL_DYNAMIC_DRAW)
        buffer_sub_data(gl.GL_ARRAY_BUFFER, 0, out)
        stream.held = out.copy()
        stream.uploads += 1

    #: The most vertex arrays one model stream keeps for its runs; past it
    #: the set starts again.
    MODEL_RUN_VAOS = 256

    def _model_run_vao(self, stream, obj, base):
        """A vertex array over model *obj*'s vertices and *stream*'s instance
        rows read from row *base*.

        The model's own vertex array used to be given the instance
        attributes and re-aimed at each run's first row, eight pointer calls
        a run every frame (OpenGL 3.3 has no base-instance draw). A run has
        its own now -- the model's vertex layout (position, normal,
        texcoord: 32 bytes a vertex in both loaders) and its element buffer,
        plus the stream's rows from *base* -- and the model's is left as its
        loader made it.
        """
        vaos = stream.run_vaos
        key = (int(obj.vao), int(base))
        vao = vaos.get(key)
        if vao is not None:
            return vao
        if len(vaos) >= self.MODEL_RUN_VAOS:
            names = list(vaos.values())
            gl_state.glDeleteVertexArrays(len(names), names)
            vaos.clear()
            stream.plan = None              # it names the arrays just deleted
        vao = gl.glGenVertexArrays(1)
        gl_state.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, obj.vbo)
        for location, size, offset in ((0, 3, 0), (1, 3, 12), (2, 2, 24)):
            vertex_attrib_pointer(location, size, gl.GL_FLOAT, gl.GL_FALSE, 32, offset)
            gl.glEnableVertexAttribArray(location)
        ebo = getattr(obj, 'ebo', None)
        if ebo is not None:
            gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, ebo)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, stream.vbo)
        stride = self.MODEL_INSTANCE_FLOATS * 4
        origin = int(base) * stride
        for location in range(3, 10):
            vertex_attrib_pointer(location, 4, gl.GL_FLOAT, gl.GL_FALSE, stride,
                                  origin + (location - 3) * 16)
            gl.glEnableVertexAttribArray(location)
            gl.glVertexAttribDivisor(location, 1)
        vertex_attrib_pointer(10, 1, gl.GL_FLOAT, gl.GL_FALSE, stride, origin + 112)
        gl.glEnableVertexAttribArray(10)
        gl.glVertexAttribDivisor(10, 1)
        gl_state.glBindVertexArray(0)
        vaos[key] = vao
        return vao

    def _release_model_instance_streams(self):
        """Delete every model instance buffer (renderer cleanup)."""
        streams = self.__dict__.get('_model_instance_streams') or {}
        for stream in streams.values():
            if stream.run_vaos:
                names = list(stream.run_vaos.values())
                gl_state.glDeleteVertexArrays(len(names), names)
                stream.run_vaos.clear()
            stream.plan = None
            if stream.vbo:
                gl.glDeleteBuffers(1, [stream.vbo])
        streams.clear()

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
    def lit_payload_selecting(table, row_slots, payload, selected_slot):
        """*payload* -- :meth:`lit_instance_payload` made with no selection --
        as it reads with *selected_slot* selected: the same array when the
        selection is not among *row_slots* (or is a trigger, which reads as a
        trigger even when selected), else a copy with the selected rows'
        colour. The lit pass keeps its payload across selection changes
        (M3.1) and applies the selection this way, every frame, for the cost
        of one comparison over the rows."""
        if selected_slot < 0:
            return payload
        chosen = np.flatnonzero(row_slots == selected_slot)
        if len(chosen):
            bits = table.class_bits[row_slots[chosen]]
            chosen = chosen[(bits & render_table.CLASS_TRIGGER) == 0]
        if not len(chosen):
            return payload
        payload = payload.copy()
        payload[chosen, 0:3] = _SELECTED_COLOR
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
