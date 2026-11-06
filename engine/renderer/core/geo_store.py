"""Shared GPU storage for angled-brush meshes: vertex pages and transforms.

The renderer used to give every angled brush its own vertex buffer and vertex
array, and the textured pass drew each brush's faces with its transform sent
as uniforms -- thousands of binds, uploads and draws a frame. This module
holds that geometry in a few shared buffers instead, so faces of many brushes
can be drawn by one call:

* **Vertex pages.** Each mesh is written once, when it is built, into a range
  of a shared vertex buffer (a *page*), first fit. Pages start small and grow
  geometrically up to :data:`PAGE_VERTICES`; a full page means a new page, so
  nothing is ever copied or repacked, and an edit uploads only the edited
  mesh's own range. A released mesh's range goes back to its page's free
  list, and a page left empty is deleted -- one empty page is kept as a
  spare, so churn at a page boundary does not create and delete pages. Every pass that draws one mesh at a time still binds a per-mesh
  vertex array -- pointing into the page at the mesh's offset -- so the
  vertices exist once.
* **Transform slots.** OpenGL 3.3 has no ``gl_DrawID``, so a draw covering
  many brushes cannot tell the vertex shader whose transform to use. The
  association is explicit data: every vertex of a mesh carries the mesh's
  *slot* (an integer attribute, written with the vertices), and the brush's
  model and normal matrices live at that slot in a buffer texture, seven
  RGBA32F texels per slot. Slots belong to meshes, not to table rows, so a
  table reconcile that renumbers its rows never touches them. A CPU mirror of
  the buffer means an unchanged transform is never uploaded again.
* **Index buffers.** For indexed submission, each (texture, page) group's
  visible vertex indices -- page-local, so 16-bit -- are cached per group, a
  few variants each (a main view and a portal view see different sets). A
  still view uploads none, and an edit re-uploads only the groups that hold
  the edited brush's faces.

Locality is the point throughout: one brush's edit uploads that brush's
vertices, its transform slot and the index lists of its own groups -- never a
whole page, the whole transform buffer or every index list.

GL objects only; all decisions about *what* to draw stay in the passes.
"""

import ctypes

import numpy as np
import OpenGL.GL as gl

#: Largest page, in vertices: page-local indices then fit 16 bits.
PAGE_VERTICES = 1 << 16
#: The first page's size; each new page doubles the last, up to the cap.
FIRST_PAGE_VERTICES = 1 << 12

#: Bytes per vertex: position (3f), normal (3f), UV (2f), face UV scale (2f),
#: rotation (1f), shift (2f), transform slot (1i).
STRIDE = 56
#: The float layout of one vertex: the mesh's own 8 floats, then the face's 5.
VERTEX_FLOATS = 8
FACE_FLOATS = 5

#: Attribute locations: 0..2 the vertex, 3..5 the face's UV state (as the
#: geo_textured program reads them), 6 the transform slot.
_FLOAT_ATTRIBUTES = ((0, 3, 0), (1, 3, 12), (2, 2, 24),
                     (3, 2, 32), (4, 1, 40), (5, 2, 44))
SLOT_LOCATION = 6
_SLOT_OFFSET = 52

#: Texels per transform slot: the model matrix's four columns, then the
#: normal matrix's three (xyz).
TEXELS_PER_SLOT = 7

#: Index-list variants kept per (texture, page) group (a main view, a portal
#: view, a split screen see different sets of the same group).
INDEX_VARIANTS = 4
#: Passes an index list may go unused before its buffer is deleted.
INDEX_IDLE_PASSES = 600
#: Changed transform slots closer than this are uploaded as one range.
TRANSFORM_GAP = 16


class _Page:
    __slots__ = ('vbo', 'vao', 'capacity', 'free', 'used')

    def __init__(self, capacity):
        self.capacity = capacity
        self.used = 0
        #: ``[start, length]`` ranges, sorted by start, coalesced.
        self.free = [[0, capacity]]
        self.vbo = gl.glGenBuffers(1)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.vbo)
        gl.glBufferData(gl.GL_ARRAY_BUFFER, capacity * STRIDE, None,
                        gl.GL_STATIC_DRAW)
        self.vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(self.vao)
        _point_attributes(0, slot=True)
        gl.glBindVertexArray(0)

    def take(self, count):
        """First fit: the start of a free range of *count*, or None."""
        for entry in self.free:
            if entry[1] >= count:
                start = entry[0]
                entry[0] += count
                entry[1] -= count
                if not entry[1]:
                    self.free.remove(entry)
                self.used += count
                return start
        return None

    def give(self, start, count):
        """Return a range, merging it with its free neighbours."""
        free = self.free
        index = 0
        while index < len(free) and free[index][0] < start:
            index += 1
        free.insert(index, [start, count])
        if index + 1 < len(free) and start + count == free[index + 1][0]:
            free[index][1] += free.pop(index + 1)[1]
        if index and free[index - 1][0] + free[index - 1][1] == start:
            free[index - 1][1] += free.pop(index)[1]
        self.used -= count


def _point_attributes(base, slot=False, face=True):
    """Aim the bound VAO's attributes at vertex *base* of the bound page."""
    origin = base * STRIDE
    for location, size, offset in _FLOAT_ATTRIBUTES:
        if location >= 3 and not face:
            break
        gl.glVertexAttribPointer(location, size, gl.GL_FLOAT, gl.GL_FALSE,
                                 STRIDE, ctypes.c_void_p(origin + offset))
        gl.glEnableVertexAttribArray(location)
    if slot:
        gl.glVertexAttribIPointer(SLOT_LOCATION, 1, gl.GL_INT, STRIDE,
                                  ctypes.c_void_p(origin + _SLOT_OFFSET))
        gl.glEnableVertexAttribArray(SLOT_LOCATION)


class GeoStore:
    """The pages, transform slots and index buffers of one renderer."""

    def __init__(self):
        #: Pages by index; None where a page was released (indices of live
        #: pages never change, since meshes hold them).
        self.pages = []
        self._free_slots = []
        self._next_slot = 0
        #: CPU mirror of the transform buffer, ``(slots, 7, 4)`` float32.
        self.mirror = np.zeros((0, TEXELS_PER_SLOT, 4), dtype=np.float32)
        self.transform_buffer = 0
        self.transform_texture = 0
        #: Bytes uploaded, by kind -- what the tests and benchmarks read.
        self.uploads = {'geometry': 0, 'transforms': 0, 'indices': 0}
        #: (texture, page) -> [[ebo, first, count, last used], ...]
        self._index_lists = {}
        self._clock = 0

    @property
    def uploaded(self):
        """Bytes uploaded so far, all kinds together."""
        return sum(self.uploads.values())

    # -- vertex pages ------------------------------------------------------

    def place(self, vertices, faces, slot):
        """Store one mesh: ``(page, start)``, or None for an empty mesh.

        *vertices* is ``(n, 8)`` float32, *faces* ``(n, 5)`` float32 -- the
        per-vertex face UV state -- and *slot* the mesh's transform slot. A
        mesh larger than :data:`PAGE_VERTICES` gets a page of its own, which
        :meth:`batchable` excludes (16-bit indices cannot address it); the
        per-mesh path draws it.
        """
        count = len(vertices)
        if not count:
            return None
        for index, page in enumerate(self.pages):
            if page is None:
                continue
            start = page.take(count)
            if start is not None:
                break
        else:
            # Sized by what is live: each new page about doubles the total,
            # so a world that emptied its pages (and filled them again) gets
            # the same small-to-large sequence, not one maximum-size page.
            live = sum(p.used for p in self.pages if p is not None)
            capacity = FIRST_PAGE_VERTICES
            while capacity < max(count, live) and capacity < PAGE_VERTICES:
                capacity *= 2
            capacity = max(capacity, count)
            page = _Page(capacity)
            if None in self.pages:
                index = self.pages.index(None)
                self.pages[index] = page
            else:
                self.pages.append(page)
                index = len(self.pages) - 1
            start = page.take(count)
        block = np.empty((count, STRIDE // 4), dtype=np.float32)
        block[:, :VERTEX_FLOATS] = vertices
        block[:, VERTEX_FLOATS:VERTEX_FLOATS + FACE_FLOATS] = faces
        block.view(np.int32)[:, STRIDE // 4 - 1] = slot
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.pages[index].vbo)
        gl.glBufferSubData(gl.GL_ARRAY_BUFFER, start * STRIDE, block.nbytes, block)
        self.uploads['geometry'] += block.nbytes
        return index, start

    def release(self, page, start, count):
        held = self.pages[page]
        held.give(start, count)
        if held.used:
            return
        # Empty: delete it, unless it is the only empty page (the spare).
        if any(other is not None and other is not held and not other.used
               for other in self.pages):
            gl.glDeleteVertexArrays(1, [held.vao])
            gl.glDeleteBuffers(1, [held.vbo])
            self.pages[page] = None

    def mesh_vao(self, page, start, face):
        """A VAO over one mesh's vertices in its page (offset *start*).

        ``face`` adds the per-vertex face UV state, for the textured pass's
        per-mesh fallback; without it the VAO is the plain vertex layout every
        other per-mesh pass binds.
        """
        vao = gl.glGenVertexArrays(1)
        gl.glBindVertexArray(vao)
        gl.glBindBuffer(gl.GL_ARRAY_BUFFER, self.pages[page].vbo)
        _point_attributes(start, slot=False, face=face)
        gl.glBindVertexArray(0)
        return vao

    def page_vao(self, page):
        return self.pages[page].vao

    def batchable(self, pages):
        """Per entry of *pages*: whether that page can be drawn batched."""
        sizes = np.array([p.capacity if p is not None else 0 for p in self.pages]
                         or [0], dtype=np.int64)
        return sizes[np.asarray(pages)] <= PAGE_VERTICES

    def vertex_bytes(self):
        return sum(page.capacity for page in self.pages if page is not None) * STRIDE

    # -- transform slots ---------------------------------------------------

    def take_slot(self):
        if self._free_slots:
            return self._free_slots.pop()
        slot = self._next_slot
        self._next_slot += 1
        if slot >= len(self.mirror):
            grown = np.zeros((max(64, 2 * len(self.mirror)), TEXELS_PER_SLOT, 4),
                             dtype=np.float32)
            grown[:len(self.mirror)] = self.mirror
            self.mirror = grown
            self._respecify_transforms()
        return slot

    def give_slot(self, slot):
        self._free_slots.append(slot)

    def _respecify_transforms(self):
        if not self.transform_buffer:
            self.transform_buffer = gl.glGenBuffers(1)
            self.transform_texture = gl.glGenTextures(1)
        gl.glBindBuffer(gl.GL_TEXTURE_BUFFER, self.transform_buffer)
        gl.glBufferData(gl.GL_TEXTURE_BUFFER, self.mirror.nbytes, self.mirror,
                        gl.GL_DYNAMIC_DRAW)
        self.uploads['transforms'] += self.mirror.nbytes
        gl.glBindTexture(gl.GL_TEXTURE_BUFFER, self.transform_texture)
        gl.glTexBuffer(gl.GL_TEXTURE_BUFFER, gl.GL_RGBA32F, self.transform_buffer)
        gl.glBindTexture(gl.GL_TEXTURE_BUFFER, 0)
        gl.glBindBuffer(gl.GL_TEXTURE_BUFFER, 0)

    def write_transforms(self, slots, models, normals):
        """Set *slots*' transforms; upload only what changed.

        *models* ``(n, 16)`` and *normals* ``(n, 9)`` are column-major float32
        rows, as ``render_table.model_matrices`` builds them. Returns the
        bytes uploaded (0 for a still world).
        """
        if not len(slots):
            return 0
        fresh = np.zeros((len(slots), TEXELS_PER_SLOT, 4), dtype=np.float32)
        fresh[:, 0:4, :] = models.reshape(-1, 4, 4)
        fresh[:, 4:7, 0:3] = normals.reshape(-1, 3, 3)
        changed = np.any((self.mirror[slots] != fresh).reshape(len(slots), -1),
                         axis=1)
        if not changed.any():
            return 0
        moved = np.sort(slots[changed])
        self.mirror[slots[changed]] = fresh[changed]
        # One upload per run of changed slots: two movers far apart in the
        # buffer must not upload everything between them.
        breaks = np.flatnonzero(np.diff(moved) > TRANSFORM_GAP) + 1
        bounds = zip(np.r_[0, breaks].tolist(), np.r_[breaks, len(moved)].tolist())
        sent = 0
        gl.glBindBuffer(gl.GL_TEXTURE_BUFFER, self.transform_buffer)
        for a, b in bounds:
            lo, hi = int(moved[a]), int(moved[b - 1]) + 1
            span = self.mirror[lo:hi]
            gl.glBufferSubData(gl.GL_TEXTURE_BUFFER, lo * TEXELS_PER_SLOT * 16,
                               span.nbytes, span)
            sent += span.nbytes
        gl.glBindBuffer(gl.GL_TEXTURE_BUFFER, 0)
        self.uploads['transforms'] += sent
        return sent

    def transform_bytes(self):
        return self.mirror.nbytes

    # -- index lists -------------------------------------------------------

    def begin_pass(self):
        """Advance the index lists' clock; free lists long unused."""
        self._clock += 1
        if self._clock % INDEX_IDLE_PASSES:
            return
        for key in list(self._index_lists):
            kept = []
            for entry in self._index_lists[key]:
                if self._clock - entry[3] > INDEX_IDLE_PASSES:
                    gl.glDeleteBuffers(1, [entry[0]])
                else:
                    kept.append(entry)
            if kept:
                self._index_lists[key] = kept
            else:
                del self._index_lists[key]

    def index_buffer(self, key, first, count):
        """The element buffer of one (texture, page) group's 16-bit indices.

        Reused while the group's ranges are unchanged; otherwise this group's
        list alone is rebuilt and uploaded, into its least recently used
        variant.
        """
        variants = self._index_lists.setdefault(key, [])
        for entry in variants:
            if (len(entry[1]) == len(first) and np.array_equal(entry[1], first)
                    and np.array_equal(entry[2], count)):
                entry[3] = self._clock
                return entry[0]
        total = int(count.sum())
        indices = np.arange(total, dtype=np.int64)
        indices += np.repeat(first.astype(np.int64) - (np.cumsum(count) - count),
                             count)
        indices = indices.astype(np.uint16)
        if len(variants) < INDEX_VARIANTS:
            entry = [gl.glGenBuffers(1), None, None, 0]
            variants.append(entry)
        else:
            entry = min(variants, key=lambda e: e[3])
        entry[1], entry[2], entry[3] = first.copy(), count.copy(), self._clock
        gl.glBindBuffer(gl.GL_ELEMENT_ARRAY_BUFFER, entry[0])
        gl.glBufferData(gl.GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices,
                        gl.GL_STREAM_DRAW)
        self.uploads['indices'] += indices.nbytes
        return entry[0]

    def index_bytes(self):
        return sum(2 * int(entry[2].sum()) for variants in self._index_lists.values()
                   for entry in variants)

    # -- lifetime ----------------------------------------------------------

    def delete(self):
        try:
            for page in self.pages:
                if page is not None:
                    gl.glDeleteVertexArrays(1, [page.vao])
                    gl.glDeleteBuffers(1, [page.vbo])
            for variants in self._index_lists.values():
                for entry in variants:
                    gl.glDeleteBuffers(1, [entry[0]])
            if self.transform_buffer:
                gl.glDeleteBuffers(1, [self.transform_buffer])
                gl.glDeleteTextures([self.transform_texture])
        except Exception:
            pass  # GL context may already be gone during shutdown
        self.__init__()
