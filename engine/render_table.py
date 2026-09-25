"""The dense render projection of Fio's world -- T3.

Fio already pays to describe its world numerically: :meth:`LogicThread._build_cull_cache`
keeps AABB centres and half-extents in NumPy so the frustum test can run as two
matmuls instead of a per-brush Python loop.  What it does not keep is anything
about *what* a brush is -- its shader class, whether it is water, which texture
sits on each face -- so the moment culling finishes, the renderer has to go back
to the brush dicts and rediscover all of it, every frame, for every visible
object.  ``is_water_brush`` alone is six ``str.lower()`` calls and six substring
searches per brush per frame, answering a question that only changes when
somebody retextures the brush in the editor.

This module holds that missing half.  A :class:`RenderTable` is a *projection*
of the brush list: one row per brush, columns of plain NumPy, addressed by an
integer ``slot``.  It is emphatically **not** a second world --

* it stores nothing authored.  Every column is a resolution of something the
  brush dict already says, so deleting the whole table and rebuilding it from
  ``EditorState.brushes`` yields identical bits;
* it is never written back to.  The projection is read-only with respect to the
  world; the one write the world needs (stamping a brush's UUID) lives in
  :meth:`EditorState.ensure_entity_ids`, in the module that owns the world;
* it holds no identity of its own.  Rows are named by the brush's existing
  ``brush['id']`` UUID -- the same id undo uses to re-point the selection at the
  dict that replaced it.  A ``slot`` is an *address*, valid only within one
  :attr:`generation`; the id is the *name*, and the map between them is a
  cache-boundary mechanism, not a per-frame lookup.

That last distinction is the whole point.  Frame code does
``class_bits[visible_slots]``, never ``slot_of_id[some_id]``.  Replacing a
Python object lookup with a Python dict lookup would move the cost sideways, not
remove it.

Deliberately GL-free.  Texture *names* are interned to dense integers here; the
renderer maps those to GL texture ids on its own thread, where a GL context
exists (see :meth:`RenderTable.texture_names`).  That keeps the whole module
unit-testable headlessly, the way :mod:`engine.render_cull` is, and keeps the
logic thread from ever needing a context.

Refresh discipline
------------------
Columns are grouped by *how often they change*, which turns out to line up with
*how expensive they are to recompute*:

``cold``
    classification and material -- ``class_bits``, ``tex_name_id``, the UV
    columns, ``geo_epoch``.  Expensive (dict walks, string searches) and changed
    only by an editor edit or an I/O handler, both of which have a choke point.
    Refreshed when the world epoch moves, never per frame.

``warm``
    ``center``, ``half``, ``rot``.  Cheap (a handful of float copies) and
    changed every tick by movers and doors, which have no per-tick notification
    and should not grow one.  Refreshed unconditionally for the rows that move.

The one field deliberately left out of ``cold`` is the *live* ``hidden`` flag.
Big World parks objects by writing ``hidden`` directly and relies on every
per-frame consumer reading it live -- see :data:`engine.spatial.PARKED_HIDDEN_KEY`
-- so caching it would break streaming.  ``class_bits`` therefore carries the
*authored* value via :func:`engine.spatial.authored_hidden`, exactly as the
collision grid does, and the live flag stays a per-frame read.
"""

from __future__ import annotations

import numpy as np

from engine.constants import is_water_brush, normalize_color
from engine.spatial import authored_hidden
from engine import brush_geometry

# --------------------------------------------------------------------------
# Classification bits
# --------------------------------------------------------------------------
#
# One uint16 per brush replacing the chain of dict lookups, string comparisons
# and substring searches ``_sort_objects`` and ``_split_opaque`` run per visible
# brush per frame.  Order is not meaningful; the values are.

#: The mapper (or an I/O Show/Hide) hid this brush.  NOT the live ``hidden``
#: flag -- see the module docstring on Big World parking.
CLASS_HIDDEN_AUTHORED = 1 << 0
CLASS_WATER           = 1 << 1
CLASS_FOG             = 1 << 2
CLASS_GLASS           = 1 << 3
CLASS_GLOW            = 1 << 4
CLASS_TRIGGER         = 1 << 5
CLASS_SUBTRACT        = 1 << 6
#: Carries a convex plane set, so it draws from its own mesh rather than the
#: shared unit cube.
CLASS_HAS_GEOMETRY    = 1 << 7
#: At least one face carries a texture worth binding -- the test
#: ``_split_opaque`` runs to decide the textured pass from the solid one.
CLASS_TEXTURED        = 1 << 8
#: Eligible to cast a shadow: solid world geometry, not a trigger/fog/water/
#: glass/glow volume and not a subtract brush.
CLASS_SHADOW_CASTER   = 1 << 9
#: A mover or a door: its transform changes every tick, so it is the row set
#: whose warm columns are re-read per frame.  Replaces the ``dynamic_rows``
#: list ``_build_cull_cache`` kept, and comes from the same two flags.
CLASS_DYNAMIC         = 1 << 10
#: Brush-wide ``texture_tiling``: every face without an explicit scale keeps a
#: constant texel size.  Read per face per frame by the textured pass, so it is
#: resolved here with the rest of the material state.
CLASS_TEXTURE_TILING  = 1 << 11

#: The classes that make a brush something other than plain opaque geometry.
#: A row with none of these bits set goes in the opaque pass.
CLASS_NON_OPAQUE = (CLASS_WATER | CLASS_FOG | CLASS_GLASS |
                    CLASS_GLOW | CLASS_TRIGGER)

#: Face order of the shared cube VAO -- index *i* is the face whose six vertices
#: start at ``i * 6``.  Mirrors ``renderer_F._CUBE_FACE_KEYS``; the two must
#: agree, because ``tex_name_id[:, i]`` is what selects that run's texture.
CUBE_FACE_KEYS = ('south', 'north', 'west', 'east', 'down', 'top')

#: Texture names that mean "do not draw this face".  ``caulk`` is never drawn;
#: ``nodraw`` is dropped in play but kept visible while editing, so the two are
#: interned separately and the play-mode filter is applied by the renderer.
TEX_SKIP = 'caulk.jpg'
TEX_NODRAW = 'nodraw.jpg'
TEX_DEFAULT = 'default.png'

#: Interned id meaning "no texture on this face".
TEX_NONE = -1

#: Fixed interned ids for the names every table pre-interns, so a consumer can
#: test for "never draw this face" and "drop this face in play" numerically.
TEX_ID_DEFAULT = 0
TEX_ID_SKIP = 1
TEX_ID_NODRAW = 2


def _brush_class_bits(brush) -> int:
    """The classification word for one brush.

    Runs once per brush per *edit*, in place of the per-frame chain in
    ``_sort_objects``/``_split_opaque``.  Deliberately reads the same predicates
    those did -- :func:`engine.constants.is_water_brush` above all -- so the
    projection cannot disagree with the renderer about what a brush is.
    """
    bits = 0
    if authored_hidden(brush):
        bits |= CLASS_HIDDEN_AUTHORED

    shader = brush.get('shader')
    if is_water_brush(brush):
        bits |= CLASS_WATER
    elif brush.get('is_fog') or shader == 'Fog':
        bits |= CLASS_FOG
    elif shader == 'Glass':
        bits |= CLASS_GLASS
    elif shader == 'Glow':
        bits |= CLASS_GLOW
    elif brush.get('is_trigger'):
        bits |= CLASS_TRIGGER

    if brush.get('operation') == 'subtract':
        bits |= CLASS_SUBTRACT
    if brush.get('is_mover', False) or brush.get('is_door', False):
        bits |= CLASS_DYNAMIC
    if brush.get('texture_tiling', False):
        bits |= CLASS_TEXTURE_TILING
    if brush_geometry.brush_has_geometry(brush):
        bits |= CLASS_HAS_GEOMETRY

    textures = brush.get('textures') or {}
    for tex in textures.values():
        if tex and tex not in (TEX_DEFAULT, TEX_SKIP):
            bits |= CLASS_TEXTURED
            break

    # The shadow pass's caster filter, hoisted out of its per-frame Python walk.
    if not (bits & (CLASS_HIDDEN_AUTHORED | CLASS_TRIGGER | CLASS_FOG |
                    CLASS_WATER | CLASS_GLASS | CLASS_GLOW | CLASS_SUBTRACT)):
        bits |= CLASS_SHADOW_CASTER

    return bits


class RenderTable:
    """A dense, disposable projection of a brush list.

    Rows are addressed by ``slot`` and named by ``brush['id']``.  Build one,
    :meth:`sync` it against the live brush list whenever the world epoch moves,
    and :meth:`refresh_transforms` the handful of rows that move per tick.
    """

    __slots__ = ('generation', 'count', 'ids', 'slot_of_id', 'brushes',
                 'center', 'half', 'rot', 'class_bits', 'tex_name_id',
                 'uv_scale', 'uv_angle', 'uv_shift', 'uv_natural',
                 'uv_has_scale', 'colour', 'glow_colour', 'geo_epoch',
                 'dynamic_slots', '_tex_ids', '_tex_names', '_epoch',
                 '_hidden_buf')

    def __init__(self):
        self.generation = 0
        self.count = 0
        #: slot -> the brush's stable UUID.  A plain list: never indexed in the
        #: frame loop, only when a cache boundary has to be crossed.
        self.ids: list = []
        #: UUID -> slot.  Cache-boundary mechanism; see the module docstring.
        self.slot_of_id: dict = {}
        #: slot -> the live brush dict.  A reference, exactly as
        #: :class:`engine.spatial.CellIndex` holds references: the object's data
        #: still lives in exactly one place.  Frame code does not walk this.
        self.brushes: list = []
        #: Slots of the movers and doors -- the rows whose warm columns are
        #: re-read per frame.  Recomputed whenever the table reconciles, from
        #: :data:`CLASS_DYNAMIC`, so it cannot drift from the classification.
        self.dynamic_slots = np.empty(0, dtype=np.int32)

        # float64 deliberately: this is exactly what _build_cull_cache held,
        # and the frustum batch casts to float64 internally -- matching the
        # dtype keeps the cull bit-identical and saves the per-frame cast.
        self.center = np.zeros((0, 3), dtype=np.float64)
        self.half = np.zeros((0, 3), dtype=np.float64)
        #: ``[axis_x, axis_y, axis_z, angle_degrees]``; angle 0 for the common
        #: unrotated brush, which is what lets the instance-matrix build stay a
        #: vectorised translate+scale for almost every row.
        self.rot = np.zeros((0, 4), dtype=np.float32)

        self.class_bits = np.zeros((0,), dtype=np.uint16)
        self.tex_name_id = np.full((0, 6), TEX_NONE, dtype=np.int32)
        self.uv_scale = np.zeros((0, 6, 2), dtype=np.float32)
        self.uv_angle = np.zeros((0, 6), dtype=np.float32)
        self.uv_shift = np.zeros((0, 6, 2), dtype=np.float32)
        #: Per face: the texture keeps a constant texel size, recomputed from
        #: the brush's live extent every time it is drawn.  A *mode*, so it
        #: cannot be baked into uv_scale -- but whether the mode is on is
        #: material state, and that is what lives here.
        self.uv_natural = np.zeros((0, 6), dtype=bool)
        #: Per face: an explicit uv_scale was authored.  Distinguishes "no
        #: scale set, fall back to FIT" from a scale that happens to be zero.
        self.uv_has_scale = np.zeros((0, 6), dtype=bool)
        #: The brush's flat-shaded colour, already normalised to 0..1: the
        #: tint if it has one, else its colour, else the default grey. Read
        #: per brush per frame by the lit pass, and changed only by an edit.
        self.colour = np.zeros((0, 3), dtype=np.float32)
        #: The Glow pass's overbright colour -- base colour times intensity,
        #: clamped -- resolved here for the same reason.
        self.glow_colour = np.zeros((0, 3), dtype=np.float32)
        #: The brush's geometry epoch at the time the row was resolved, so a
        #: consumer caching GPU data per row can tell a stale mesh from a live
        #: one without re-deriving ``geometry_signature``.  0 for box brushes.
        self.geo_epoch = np.zeros((0,), dtype=np.int64)

        # Texture-name intern table.  GL-free: these are ids for *names*, and
        # the renderer maps them to GL texture ids once per unique name.
        self._tex_ids: dict = {}
        self._tex_names: list = []
        # Interned up front so their ids are fixed constants the renderer can
        # compare against. It must never intern a name itself: the table is
        # written on the logic thread only.
        for _name in (TEX_DEFAULT, TEX_SKIP, TEX_NODRAW):
            self.intern_texture(_name)
        self._epoch = None
        self._hidden_buf = np.empty(0, dtype=bool)

    # -- texture name interning -------------------------------------------

    def intern_texture(self, name) -> int:
        """The dense id for a texture *name*, assigning one on first sight."""
        if not name:
            return TEX_NONE
        tid = self._tex_ids.get(name)
        if tid is None:
            tid = len(self._tex_names)
            self._tex_ids[name] = tid
            self._tex_names.append(name)
        return tid

    def texture_names(self) -> list:
        """Interned names, indexed by id.

        The renderer walks this once per new name to build its ``name id -> GL
        texture id`` array; it is tens of entries, not thousands, and it is
        never touched per brush.
        """
        return self._tex_names

    # -- capacity ----------------------------------------------------------

    def _resize(self, n):
        """Grow capacity geometrically; never resize for an ordinary append."""
        capacity = len(self.center)
        if n <= capacity:
            return
        grown = max(16, capacity * 2, n)

        def grow(arr, fill=0):
            shape = (grown,) + arr.shape[1:]
            new = np.full(shape, fill, dtype=arr.dtype)
            if len(arr):
                new[:len(arr)] = arr
            return new

        self.center = grow(self.center)
        self.half = grow(self.half)
        self.rot = grow(self.rot)
        self.class_bits = grow(self.class_bits)
        self.tex_name_id = grow(self.tex_name_id, TEX_NONE)
        self.uv_scale = grow(self.uv_scale)
        self.uv_angle = grow(self.uv_angle)
        self.uv_shift = grow(self.uv_shift)
        self.uv_natural = grow(self.uv_natural)
        self.uv_has_scale = grow(self.uv_has_scale)
        self.colour = grow(self.colour)
        self.glow_colour = grow(self.glow_colour)
        self.geo_epoch = grow(self.geo_epoch)

    # -- row resolution ----------------------------------------------------

    def _resolve_warm(self, slot, brush):
        """Transform columns for one row.  Cheap; runs per frame for movers."""
        pos = brush.get('pos') or (0.0, 0.0, 0.0)
        size = brush.get('size') or (64.0, 64.0, 64.0)
        self.center[slot] = pos
        self.half[slot] = (size[0] * 0.5, size[1] * 0.5, size[2] * 0.5)
        angle = brush.get('_rot_angle') or 0.0
        if angle:
            axis = brush.get('rot_axis') or (0.0, 1.0, 0.0)
            self.rot[slot] = (axis[0], axis[1], axis[2], angle)
        elif self.rot[slot, 3]:
            self.rot[slot] = 0.0

    def _resolve_cold(self, slot, brush):
        """Classification and material columns for one row.

        Expensive by design -- this is where ``is_water_brush``'s string search,
        the texture-name resolution and the UV lookups happen.  Running it here,
        at edit frequency, is the point of the whole table.
        """
        self.class_bits[slot] = _brush_class_bits(brush)

        textures = brush.get('textures') or {}
        uv_scale = brush.get('uv_scale') or {}
        uv_angle = brush.get('uv_angle') or {}
        uv_shift = brush.get('uv_shift') or {}
        for i, face in enumerate(CUBE_FACE_KEYS):
            self.tex_name_id[slot, i] = self.intern_texture(
                textures.get(face, TEX_DEFAULT))
            scale = uv_scale.get(face)
            self.uv_has_scale[slot, i] = scale is not None
            if scale is None:
                self.uv_scale[slot, i, 0] = 1.0
                self.uv_scale[slot, i, 1] = 1.0
            else:
                self.uv_scale[slot, i, 0] = scale[0]
                self.uv_scale[slot, i, 1] = scale[1]
            self.uv_natural[slot, i] = brush_geometry.face_uses_natural_scale(
                brush, face)
            self.uv_angle[slot, i] = uv_angle.get(face, 0.0)
            shift = uv_shift.get(face) or (0.0, 0.0)
            self.uv_shift[slot, i, 0] = shift[0]
            self.uv_shift[slot, i, 1] = shift[1]

        tint = brush.get('tint')
        base = normalize_color(tint) if tint else normalize_color(
            brush.get('colour'))
        self.colour[slot] = base
        intensity = float(brush.get('glow_intensity', 10.0))
        glow_base = normalize_color(tint or brush.get('colour'),
                                    default=[1.0, 1.0, 1.0])
        for k in range(3):
            self.glow_colour[slot, k] = min(glow_base[k] * intensity, 10.0)

        if self.class_bits[slot] & CLASS_HAS_GEOMETRY:
            self.geo_epoch[slot] = brush_geometry._brush_epoch(brush)
        else:
            self.geo_epoch[slot] = 0

    # -- synchronisation ---------------------------------------------------

    def needs_reconcile(self, brushes, epoch=None):
        """Whether the next :meth:`begin_frame` will rebuild the row mapping.

        Two O(1) comparisons.  A caller that has to prepare something before a
        reconcile -- stamping ids onto brushes that have not got one -- asks
        this rather than doing that work unconditionally every frame.
        """
        return epoch is None or epoch != self._epoch or len(brushes) != self.count

    def begin_frame(self, brushes, epoch=None, dirty_objects=None):
        """Bring the table into line with *brushes* and return the live hidden mask.

        This is the whole of the projection's per-frame Python cost: one pass
        reading the **live** ``hidden`` flag.  Big World parks objects by
        writing it directly, with no notification, precisely because every
        per-frame consumer already reads it
        (:data:`engine.spatial.PARKED_HIDDEN_KEY`), so it is the one field that
        cannot be cached -- and at one dict lookup per brush it is also the
        cheapest, which is what makes paying for it per frame the right trade.

        Everything else -- classification, texture resolution, UVs, colour --
        is behind *epoch*, the world's coarse change counter.  When it moves,
        the cold columns are re-resolved: O(N) once per editor gesture, never
        per frame.

        The row set is re-derived when the epoch moves or the brush count
        changes. When an editor transaction supplies its dirty-object journal,
        only those rows lose their cold columns; unrelated survivors keep their
        cached classification/material state.

        Rows are matched by ``brush['id']``, so a structural change costs a set
        diff rather than a full re-resolution: surviving rows keep the columns
        they had.  Call :meth:`EditorState.ensure_entity_ids` first when
        :meth:`needs_reconcile` says so.
        """
        n = len(brushes)
        if len(self._hidden_buf) < n:
            self._hidden_buf = np.empty(max(n, 16), dtype=bool)
        hidden = self._hidden_buf[:n]

        cold_dirty = epoch is None or epoch != self._epoch
        if cold_dirty or n != self.count:
            self._reconcile(brushes, cold_dirty, dirty_objects)
            self._epoch = epoch

        # One list comprehension and one bulk store. Assigning a NumPy array
        # element by element from Python costs several times as much, and this
        # runs over every brush in the level every frame.
        hidden[:] = [b.get('hidden', False) for b in brushes]
        return hidden

    def sync(self, brushes, epoch=None, dirty_objects=None):
        """Reconcile without reading ``hidden``.  Returns whether it did.

        :meth:`begin_frame` is what the render path calls; this is for callers
        that want the columns brought up to date on their own schedule (tests,
        and anything preparing a pass outside the frame loop).
        """
        before = self.generation
        cold_dirty = epoch is None or epoch != self._epoch
        structural = cold_dirty or len(brushes) != self.count
        if not structural:
            for i, b in enumerate(brushes):
                if self.brushes[i] is not b:
                    structural = True
                    break
        if structural:
            self._reconcile(brushes, cold_dirty, dirty_objects)
            self._epoch = epoch
        return self.generation != before

    def _reconcile(self, brushes, cold_dirty, dirty_objects=None):
        """Rebuild the slot mapping, preserving the cold columns that survive.

        A row *survives* when the brush now at some slot is the same object,
        under the same id, and is not in the transaction dirty-object journal.
        Structural changes therefore move untouched rows without re-resolving
        their materials or classification.
        """
        n = len(brushes)
        self._resize(max(n, 16))

        old_slot_of_id = self.slot_of_id
        old_brushes = self.brushes
        old_count = len(old_brushes)

        new_ids = [None] * n
        survivors = set()          # slots whose cold columns are already right
        move_src, move_dst = [], []

        for slot, brush in enumerate(brushes):
            bid = brush.get('id')
            new_ids[slot] = bid
            if bid is None:
                continue
            if dirty_objects is None:
                if cold_dirty:
                    continue
            elif id(brush) in dirty_objects:
                continue
            old = old_slot_of_id.get(bid)
            if old is None or old >= old_count or old_brushes[old] is not brush:
                continue                      # new row, or the id was reused
            survivors.add(slot)
            if old != slot:
                move_src.append(old)
                move_dst.append(slot)

        if move_src:
            # Fancy indexing materialises the source before the store, so a row
            # moving down the list cannot clobber one not yet copied.
            src = np.asarray(move_src, dtype=np.intp)
            dst = np.asarray(move_dst, dtype=np.intp)
            for arr in (self.class_bits, self.tex_name_id, self.uv_scale,
                        self.uv_angle, self.uv_shift, self.uv_natural,
                        self.uv_has_scale, self.colour, self.glow_colour,
                        self.geo_epoch):
                arr[dst] = arr[src]

        for slot, brush in enumerate(brushes):
            self._resolve_warm(slot, brush)
            if slot not in survivors:
                self._resolve_cold(slot, brush)

        self.ids = new_ids
        self.slot_of_id = {bid: slot for slot, bid in enumerate(new_ids)
                           if bid is not None}
        self.brushes = list(brushes)
        self.count = n
        self.dynamic_slots = np.flatnonzero(
            self.class_bits[:n] & CLASS_DYNAMIC).astype(np.int32)
        self.generation += 1

    def refresh_transforms(self, brushes, slots):
        """Re-read the warm columns for *slots* (movers and doors, per tick)."""
        for slot in slots:
            self._resolve_warm(slot, brushes[slot])

    def refresh_rows(self, brushes, slots):
        """Re-resolve the cold columns for *slots* after a semantic change."""
        for slot in slots:
            self._resolve_cold(slot, brushes[slot])

    # -- derived views -----------------------------------------------------

    def live_hidden(self, brushes):
        """The live ``hidden`` flag per row, without reconciling.

        The frame path gets this from :meth:`begin_frame`, which reads it in
        the same pass it checks the row set in.  This is for everything else.
        """
        n = len(brushes)
        if len(self._hidden_buf) < n:
            self._hidden_buf = np.empty(max(n, 16), dtype=bool)
        out = self._hidden_buf[:n]
        for i, b in enumerate(brushes):
            out[i] = b.get('hidden', False)
        return out


def model_matrices(table, slots, out_model=None, out_normal=None):
    """Model and normal matrices for *slots*, built as arrays.

    ``M = T(centre) * R(axis, angle) * S(size)`` -- the same matrix
    ``Renderer_F._brush_model_matrix`` memoises on each brush dict, except that
    this builds the whole batch at once from columns that already exist instead
    of asking every object for its transform.

    Almost every brush in a Fio level is unrotated, and there that collapses to
    a diagonal plus a translation: six stores per row and no trigonometry, for
    the entire visible set in a handful of NumPy operations.  The rotated
    minority is filled in afterwards, one row at a time, which is what it costs
    anywhere.

    Returns ``(models (V, 16), normals (V, 9))`` as float32, laid out
    column-major -- the layout ``glUniformMatrix4fv`` and ``glUniformMatrix3fv``
    read directly, and the layout an instance buffer wants.

    The normal matrix is ``transpose(inverse(mat3(M)))``, which for ``R * S``
    is ``R * S**-1``; a zero extent degenerates to zero rather than infinity,
    matching the identity fallback the scalar path used on a singular matrix.
    """
    count = len(slots)
    if out_model is not None and len(out_model) >= count:
        models = out_model[:count]
        models.fill(0.0)
    else:
        models = np.zeros((count, 16), dtype=np.float32)
    if out_normal is not None and len(out_normal) >= count:
        normals = out_normal[:count]
        normals.fill(0.0)
    else:
        normals = np.zeros((count, 9), dtype=np.float32)
    if not count:
        return models, normals

    centre = table.center[slots]
    size = table.half[slots] * 2.0
    inv_size = np.divide(1.0, size, out=np.zeros_like(size), where=size != 0.0)

    # --- the unrotated case, for every row ---------------------------------
    models[:, 0] = size[:, 0]
    models[:, 5] = size[:, 1]
    models[:, 10] = size[:, 2]
    models[:, 12:15] = centre
    models[:, 15] = 1.0
    normals[:, 0] = inv_size[:, 0]
    normals[:, 4] = inv_size[:, 1]
    normals[:, 8] = inv_size[:, 2]

    # --- and the rotated rows on top ---------------------------------------
    rot = table.rot[slots]
    rotated = np.flatnonzero(rot[:, 3] != 0.0)
    if len(rotated):
        axis = rot[rotated, :3].astype(np.float64)
        length = np.linalg.norm(axis, axis=1)
        # A degenerate axis means no rotation, exactly as the scalar path's
        # `if glm.length(axis) > 0.001` guard decided.
        usable = length > 0.001
        rotated = rotated[usable]
        if len(rotated):
            axis = axis[usable] / length[usable, None]
            angle = np.radians(rot[rotated, 3].astype(np.float64))
            cos_a = np.cos(angle)[:, None]
            sin_a = np.sin(angle)[:, None]
            one_c = 1.0 - cos_a
            ux, uy, uz = axis[:, 0:1], axis[:, 1:2], axis[:, 2:3]
            # Rodrigues, as (rows, cols) of the 3x3 rotation.
            r = np.empty((len(rotated), 3, 3), dtype=np.float64)
            r[:, 0, 0] = (cos_a + ux * ux * one_c)[:, 0]
            r[:, 0, 1] = (ux * uy * one_c - uz * sin_a)[:, 0]
            r[:, 0, 2] = (ux * uz * one_c + uy * sin_a)[:, 0]
            r[:, 1, 0] = (uy * ux * one_c + uz * sin_a)[:, 0]
            r[:, 1, 1] = (cos_a + uy * uy * one_c)[:, 0]
            r[:, 1, 2] = (uy * uz * one_c - ux * sin_a)[:, 0]
            r[:, 2, 0] = (uz * ux * one_c - uy * sin_a)[:, 0]
            r[:, 2, 1] = (uz * uy * one_c + ux * sin_a)[:, 0]
            r[:, 2, 2] = (cos_a + uz * uz * one_c)[:, 0]

            rs = size[rotated]
            ri = inv_size[rotated]
            for col in range(3):
                for row in range(3):
                    models[rotated, col * 4 + row] = r[:, row, col] * rs[:, col]
                    normals[rotated, col * 3 + row] = r[:, row, col] * ri[:, col]

    return models, normals
