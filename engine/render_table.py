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
A row is re-read when something says it changed, never to find out whether it
did:

* **editor transactions** move the world epoch and journal their objects
  (:meth:`EditorState.mark_world_changed`); those rows are re-resolved, in
  place when the row set is unchanged;
* **runtime changes** -- I/O inputs, console commands, Big World parking, a
  save restore -- go through :mod:`engine.change_journal`, which each table
  drains once per frame;
* **movers and doors** move every tick with no notification, so their
  transforms (:attr:`RenderTable.dynamic_slots`) are re-read every frame;
* **an editor drag** writes the selection's dicts in place for many frames
  after one undo checkpoint, so the frame passes the selection as *edited* and
  only those rows' transforms are re-read.

``hidden`` is a column like any other. Big World parks through it and I/O
Hide/Show toggles it, both via :func:`engine.spatial.set_authored_flag`, which
journals the object. ``class_bits`` separately carries the *authored* value
(:func:`engine.spatial.authored_hidden`), as the collision grid does.

On a still frame the table reads nothing but the movers; Debug Tables shows
the count (``rows read last frame``).
"""

from __future__ import annotations

import numpy as np

from engine.constants import is_water_brush, normalize_color
from engine.spatial import authored_hidden
from engine import brush_geometry
from engine.change_journal import JOURNAL, OVERFLOW, STATE

# --------------------------------------------------------------------------
# Classification bits
# --------------------------------------------------------------------------

class GeometryRecord:
    """Dense cold render record for one convex geometry-table entry."""
    __slots__ = ('signature', 'convex', 'origin', 'scale', 'natural_scale')

    def __init__(self, signature, convex, origin, scale, natural_scale):
        self.signature = signature
        self.convex = convex
        self.origin = origin
        self.scale = scale
        self.natural_scale = natural_scale

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


#: Every per-row column: ``(name, trailing shape, dtype, fill)``. One list, so
#: growing the table and moving surviving rows at a reconcile cannot miss one.
_COLUMNS = (
    # -- warm: the transform and visibility, re-read for the rows that change.
    #: ``[centre xyz | half-extent xyz]`` in one contiguous block, float64:
    #: the frustum test is then a single (N, 6) x (6, 6) product with no
    #: per-frame gather. :attr:`RenderTable.center` and
    #: :attr:`RenderTable.half` are views of it.
    ('bounds', (6,), np.float64, 0.0),
    #: ``[axis_x, axis_y, axis_z, angle_degrees]``; angle 0 for the common
    #: unrotated brush, which is what lets the instance-matrix build stay a
    #: vectorised translate+scale for almost every row.
    ('rot', (4,), np.float32, 0.0),
    #: The live ``hidden`` flag, Big World parking included.
    ('hidden', (), bool, False),
    # -- cold: classification and material.
    ('class_bits', (), np.uint16, 0),
    ('tex_name_id', (6,), np.int32, TEX_NONE),
    ('uv_scale', (6, 2), np.float32, 0.0),
    ('uv_angle', (6,), np.float32, 0.0),
    ('uv_shift', (6, 2), np.float32, 0.0),
    #: Per face: the texture keeps a constant texel size, recomputed from the
    #: brush's live extent every time it is drawn.  A *mode*, so it cannot be
    #: baked into uv_scale -- but whether the mode is on is material state.
    ('uv_natural', (6,), bool, False),
    #: Per face: an explicit uv_scale was authored.  Distinguishes "no scale
    #: set, fall back to FIT" from a scale that happens to be zero.
    ('uv_has_scale', (6,), bool, False),
    #: The flat-shaded colour, normalised to 0..1: the tint if it has one,
    #: else its colour, else the default grey.
    ('colour', (3,), np.float32, 0.0),
    #: The Glow pass's overbright colour -- base colour times intensity,
    #: clamped.
    ('glow_colour', (3,), np.float32, 0.0),
    #: The brush's geometry epoch when the row was resolved, so a stale mesh
    #: can be told from a live one without re-deriving the signature.
    ('geo_epoch', (), np.int64, 0),
    #: Index into :attr:`RenderTable.geometry_records` for a convex row; -1
    #: draws the shared box.
    ('geometry_id', (), np.int32, -1),
    # Special-brush render state: narrow numeric projections of the authored
    # dictionaries the water/glass/fog shaders consume.
    ('water_tint', (3,), np.float32, 0.0),
    #: opacity, reflectivity/fresnel, wave_height, wave_enabled, distortion,
    #: refraction IOR, roughness
    ('water_params', (7,), np.float32, 0.0),
    ('water_plane', (), bool, False),
    ('glass_color', (3,), np.float32, 0.0),
    #: opacity, distortion, refraction, roughness, fresnel
    ('glass_params', (5,), np.float32, 0.0),
    ('fog_color', (3,), np.float32, 0.0),
    #: density, noise_scale
    ('fog_params', (2,), np.float32, 0.0),
)


class RenderTable:
    """A dense projection of a brush list, kept current by change, not polling.

    Rows are addressed by ``slot`` and named by ``brush['id']``.  Build one and
    :meth:`begin_frame` it once per frame: it reconciles when the editor's
    world epoch moves, re-reads the rows :mod:`engine.change_journal` names,
    and re-reads the transforms of the movers and doors, which move every tick.
    """

    __slots__ = (tuple(name for name, *_ in _COLUMNS) + (
        'generation', 'count', 'ids', 'slot_of_id', 'brushes',
        'dynamic_slots', 'geometry_records', '_tex_ids', '_tex_names',
        '_epoch', '_row_tuple', '_slot_of_obj', 'rows_read', 'refs',
        '_shown_mask', '_shown_slots', '_shown_stale', '__weakref__'))

    @property
    def center(self):
        """The AABB centre per row: a view of :attr:`bounds`."""
        return self.bounds[:, :3]

    @property
    def half(self):
        """The AABB half-extent per row: a view of :attr:`bounds`."""
        return self.bounds[:, 3:]

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
        #: Dense geometry records keyed directly by geometry_id. AABB
        #: brushes have no record; their geometry_id stays -1.
        self.geometry_records: list = []
        #: slot -> the brush dict, as an object array, for publishing rows as
        #: objects on demand (``PublishedBrushes``). Rebuilt only when the rows
        #: change.
        self.refs = np.empty(0, dtype=object)
        #: ``~hidden`` and its slots, cached until a ``hidden`` value changes.
        self._shown_mask = np.empty(0, dtype=bool)
        self._shown_slots = np.empty(0, dtype=np.intp)
        self._shown_stale = True
        for name, shape, dtype, fill in _COLUMNS:
            setattr(self, name, np.full((0,) + shape, fill, dtype=dtype))

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
        #: The row set as a tuple of the brush dicts it was reconciled from.
        #: The table holds a reference to every row's dict, so the identities
        #: in here stay unique; comparing it with the live list is how an
        #: unannounced change to the row set is recognised, in one C compare.
        self._row_tuple = ()
        #: ``id(brush)`` -> slot, for applying the change journals.
        self._slot_of_obj: dict = {}
        #: Rows whose brush dict the last :meth:`begin_frame` read (Debug
        #: Tables shows it: on a still frame it is the movers and doors).
        self.rows_read = 0
        JOURNAL.subscribe(self)

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
        for name, shape, dtype, fill in _COLUMNS:
            new = np.full((grown,) + shape, fill, dtype=dtype)
            new[:capacity] = getattr(self, name)
            setattr(self, name, new)

    def _geometry_record(self, slot, brush):
        """The dense cold geometry record for one row, or ``None`` for a box.

        Returned rather than stored: where it goes depends on the caller.  A
        reconcile collects records by slot and then compacts them into the
        dense :attr:`geometry_records`; a row-local refresh replaces the row's
        existing dense entry in place.
        """
        if not (self.class_bits[slot] & CLASS_HAS_GEOMETRY):
            return None
        convex = brush_geometry.get_convex(brush)
        if convex is None or not convex.is_valid:
            return None
        pos = brush.get('pos') or (0.0, 0.0, 0.0)
        size = brush.get('size') or (64.0, 64.0, 64.0)
        origin = np.asarray(pos, dtype=np.float64).copy()
        scale = np.asarray([max(abs(float(s)), 1e-6) for s in size], dtype=np.float64)
        natural = {id(face): bool(brush_geometry.face_uses_natural_scale(
            brush, face.get('face'), face)) for face in convex.faces}
        return GeometryRecord(
            brush_geometry.geometry_signature(brush), convex, origin, scale, natural)

    # -- row resolution ----------------------------------------------------

    def _resolve_warm(self, slot, brush):
        """Transform and visibility for one row.  Cheap; per tick for movers."""
        hidden = bool(brush.get('hidden', False))
        if hidden != self.hidden[slot]:
            self.hidden[slot] = hidden
            self._shown_stale = True
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

    def _resolve_warm_rows(self, slots, brushes):
        """:meth:`_resolve_warm` for many rows, one column store each."""
        if not len(slots):
            return
        idx = np.asarray(slots, dtype=np.intp)
        hidden = np.empty(len(idx), dtype=bool)
        bounds = np.empty((len(idx), 6), dtype=np.float64)
        rot = np.zeros((len(idx), 4), dtype=np.float32)
        for row, slot in enumerate(idx.tolist()):
            brush = brushes[slot]
            hidden[row] = bool(brush.get('hidden', False))
            pos = brush.get('pos') or (0.0, 0.0, 0.0)
            size = brush.get('size') or (64.0, 64.0, 64.0)
            bounds[row] = (pos[0], pos[1], pos[2],
                           size[0] * 0.5, size[1] * 0.5, size[2] * 0.5)
            angle = brush.get('_rot_angle') or 0.0
            if angle:
                axis = brush.get('rot_axis') or (0.0, 1.0, 0.0)
                rot[row] = (axis[0], axis[1], axis[2], angle)
        if not np.array_equal(self.hidden[idx], hidden):
            self._shown_stale = True
        self.hidden[idx] = hidden
        self.bounds[idx] = bounds
        self.rot[idx] = rot

    def _resolve_cold_rows(self, slots, brushes):
        """Classification and material columns for *slots*.

        Expensive by design -- this is where ``is_water_brush``'s string search,
        the texture-name resolution and the UV lookups happen.  Running it here,
        at edit frequency, is the point of the whole table.  The dict work is
        per brush, but each column is stored once for all the rows: a single
        NumPy element store costs as much as the lookup that produced it, and a
        row has some forty of them.
        """
        slots = [int(slot) for slot in slots]
        if not slots:
            return
        count = len(slots)
        bits = np.empty(count, dtype=np.uint16)
        tex = np.empty((count, 6), dtype=np.int32)
        uv_scale = np.empty((count, 6, 2), dtype=np.float32)
        uv_angle = np.empty((count, 6), dtype=np.float32)
        uv_shift = np.empty((count, 6, 2), dtype=np.float32)
        uv_natural = np.empty((count, 6), dtype=bool)
        uv_has = np.empty((count, 6), dtype=bool)
        colour = np.empty((count, 3), dtype=np.float32)
        glow = np.empty((count, 3), dtype=np.float32)
        geo_epoch = np.empty(count, dtype=np.int64)
        special = []                     # (row, slot, brush, class bits)
        intern = self.intern_texture
        natural_scale = brush_geometry.face_uses_natural_scale

        for row, slot in enumerate(slots):
            brush = brushes[slot]
            b = _brush_class_bits(brush)
            bits[row] = b
            textures = brush.get('textures') or {}
            scales = brush.get('uv_scale') or {}
            angles = brush.get('uv_angle') or {}
            shifts = brush.get('uv_shift') or {}
            tex[row] = [intern(textures.get(face, TEX_DEFAULT))
                        for face in CUBE_FACE_KEYS]
            row_scale = []
            row_has = []
            row_shift = []
            for face in CUBE_FACE_KEYS:
                scale = scales.get(face)
                row_has.append(scale is not None)
                row_scale.append((1.0, 1.0) if scale is None
                                 else (scale[0], scale[1]))
                shift = shifts.get(face) or (0.0, 0.0)
                row_shift.append((shift[0], shift[1]))
            uv_scale[row] = row_scale
            uv_has[row] = row_has
            uv_shift[row] = row_shift
            uv_angle[row] = [angles.get(face, 0.0) for face in CUBE_FACE_KEYS]
            uv_natural[row] = [natural_scale(brush, face) for face in CUBE_FACE_KEYS]

            tint = brush.get('tint')
            colour[row] = (normalize_color(tint) if tint
                           else normalize_color(brush.get('colour')))
            intensity = float(brush.get('glow_intensity', 10.0))
            glow_base = normalize_color(tint or brush.get('colour'),
                                        default=[1.0, 1.0, 1.0])
            glow[row] = [min(c * intensity, 10.0) for c in glow_base]

            # The brush's own geometry epoch, which every change to its shape
            # bumps (brush_geometry._invalidate) whether or not anything marks
            # the world changed; see refresh_edited. Not assigned for a box.
            geo_epoch[row] = (brush_geometry._brush_epoch(brush)
                              if b & CLASS_HAS_GEOMETRY
                              else brush.get('_geo_epoch') or 0)
            if b & (CLASS_WATER | CLASS_GLASS | CLASS_FOG):
                special.append((slot, brush, b))

        idx = np.asarray(slots, dtype=np.intp)
        self.class_bits[idx] = bits
        self.tex_name_id[idx] = tex
        self.uv_scale[idx] = uv_scale
        self.uv_angle[idx] = uv_angle
        self.uv_shift[idx] = uv_shift
        self.uv_natural[idx] = uv_natural
        self.uv_has_scale[idx] = uv_has
        self.colour[idx] = colour
        self.glow_colour[idx] = glow
        self.geo_epoch[idx] = geo_epoch
        self._resolve_special_rows(idx, special)

    #: The special-shader columns, and the fill a row of no special class
    #: holds (nothing reads them for such a row, but a rebuild gives it too).
    _SPECIAL_COLUMNS = ('water_tint', 'water_params', 'water_plane',
                        'glass_color', 'glass_params', 'fog_color', 'fog_params')

    def _resolve_special_rows(self, idx, special):
        """Water, glass and fog shader state -- only for rows of those classes.

        Defaults deliberately match the renderer's former brush.get(...)
        fallbacks.
        """
        for name in self._SPECIAL_COLUMNS:
            getattr(self, name)[idx] = 0
        for slot, brush, b in special:
            if b & CLASS_WATER:
                self.water_tint[slot] = normalize_color(
                    brush.get('water_tint', [0.0, 0.4, 0.6]))
                self.water_params[slot] = (
                    float(brush.get('water_opacity', 0.5)),
                    float(brush.get('water_fresnel',
                                    brush.get('water_reflectivity', 0.5))),
                    float(brush.get('water_wave_height', 0.5)),
                    1.0 if brush.get('water_wave_enabled', True) else 0.0,
                    float(brush.get('water_distortion', 0.5)),
                    float(brush.get('water_refraction', 1.333)),
                    float(brush.get('water_roughness', 0.0)),
                )
                self.water_plane[slot] = bool(brush.get('water_plane', False))
            if b & CLASS_GLASS:
                self.glass_color[slot] = normalize_color(
                    brush.get('glass_color', [0.7, 0.85, 0.95]))
                self.glass_params[slot] = (
                    float(brush.get('glass_opacity', 0.3)),
                    float(brush.get('glass_distortion', 0.5)),
                    float(brush.get('glass_refraction', 1.5)),
                    float(brush.get('glass_roughness', 0.0)),
                    float(brush.get('glass_fresnel', 0.5)),
                )
            if b & CLASS_FOG:
                self.fog_color[slot] = normalize_color(
                    brush.get('fog_color', [0.5, 0.6, 0.7]))
                self.fog_params[slot] = (
                    float(brush.get('fog_density', 0.01)),
                    float(brush.get('fog_noise_scale', 0.01)),
                )

    # -- synchronisation ---------------------------------------------------

    def needs_reconcile(self, brushes, epoch=None):
        """Whether the next :meth:`begin_frame` will rebuild the row mapping.

        A caller that has to prepare something before a reconcile -- stamping
        ids onto brushes that have not got one -- asks this rather than doing
        that work unconditionally every frame.
        """
        if epoch is None or epoch != self._epoch:
            return True
        return tuple(brushes) != self._row_tuple

    def begin_frame(self, brushes, epoch=None, dirty_objects=None,
                    edited=(), peer=None):
        """Bring the table into line with *brushes*; return the ``hidden`` mask.

        Nothing here visits a brush that has not changed:

        * when the editor's world *epoch* moves, the journalled rows
          (*dirty_objects*) are re-resolved -- in place when the row set is
          unchanged, by a reconcile when it is not;
        * the rows :mod:`engine.change_journal` names since the last frame --
          I/O, the console, Big World parking, a save restore -- are
          re-resolved;
        * the movers and doors, which move every tick with no notification,
          have their transforms re-read;
        * *edited* names the objects an editor tool may be changing in place
          right now (the selection, during a drag); their transforms are
          re-read and their geometry epochs compared.

        An unannounced change to the row set -- the same count, different
        dicts -- is caught by comparing the row tuple, an identity check per
        row in C that reads nothing from any brush.

        *peer* is the other buffer's table. When this one would have to
        re-resolve every row and the peer already holds exactly this row set
        at this epoch -- the frame after a load, an undo, any global
        invalidation -- its columns are copied instead of re-derived.
        """
        brushes = tuple(brushes)
        n = len(brushes)
        changes = JOURNAL.drain(self)
        self.rows_read = 0
        resolved_all = False
        if epoch is None or epoch != self._epoch or brushes != self._row_tuple:
            cold_dirty = epoch is None or epoch != self._epoch
            if self._refresh_in_place(brushes, n, epoch, dirty_objects):
                pass
            elif (peer is not None and dirty_objects is None and epoch is not None
                    and peer._epoch == epoch and peer._row_tuple == brushes):
                self.adopt(peer)
            else:
                resolved_all = self._reconcile(brushes, cold_dirty, dirty_objects)
            self._epoch = epoch
        if resolved_all:
            pass            # every row was just read from its brush
        elif changes is OVERFLOW:
            self.refresh_rows(brushes, range(n))
        elif changes:
            slot_of = self._slot_of_obj
            state = []
            moved = []
            for oid, flags in changes.items():
                slot = slot_of.get(oid)
                if slot is not None:
                    (state if flags & STATE else moved).append(slot)
            if state:
                self.refresh_rows(brushes, state)
            if moved:
                self.refresh_transforms(brushes, moved)
        # Movers and doors are not polled: in play the logic thread's dense
        # MoverTable stores their positions (MoverTable.publish); anything else
        # that moves one journals it.
        if edited:
            self.refresh_edited(brushes, edited)
        return self.hidden[:n]

    def shown(self):
        """``(mask, slots)`` of the rows not hidden, over the live rows.

        Recomputed only when a ``hidden`` value or the row set changed: on a
        still frame this is two attribute reads, not two passes over the
        table.
        """
        if self._shown_stale:
            self._shown_mask = ~self.hidden[:self.count]
            self._shown_slots = np.flatnonzero(self._shown_mask)
            self._shown_stale = False
        return self._shown_mask, self._shown_slots

    def adopt(self, peer):
        """Become a copy of *peer*: its rows, columns and intern tables.

        The slot addresses change meaning, so :attr:`generation` moves on.
        """
        for name, *_ in _COLUMNS:
            setattr(self, name, getattr(peer, name).copy())
        self.count = peer.count
        self.ids = list(peer.ids)
        self.slot_of_id = dict(peer.slot_of_id)
        self.brushes = list(peer.brushes)
        self.refs = peer.refs.copy()
        self.dynamic_slots = peer.dynamic_slots.copy()
        self.geometry_records = list(peer.geometry_records)
        self._tex_ids = dict(peer._tex_ids)
        self._tex_names = list(peer._tex_names)
        self._epoch = peer._epoch
        self._row_tuple = peer._row_tuple
        self._slot_of_obj = dict(peer._slot_of_obj)
        self._shown_stale = True
        self.generation += 1

    def epoch_is_current(self, epoch):
        """Whether the table was last reconciled at *epoch*."""
        return epoch is not None and epoch == self._epoch

    def sync(self, brushes, epoch=None, dirty_objects=None):
        """Bring the table up to date outside the frame loop.

        Returns whether anything was refreshed: a reconcile (which moves
        :attr:`generation`) or a row-local refresh of journalled rows (which
        does not, because every slot keeps its address).

        :meth:`begin_frame` is what the render path calls; this is for callers
        that want the columns brought up to date on their own schedule (tests,
        and anything preparing a pass outside the frame loop).
        """
        brushes = tuple(brushes)
        if not self.needs_reconcile(brushes, epoch):
            return False
        cold_dirty = epoch is None or epoch != self._epoch
        if self._refresh_in_place(brushes, len(brushes), epoch, dirty_objects):
            # Slots are unchanged, so ``generation`` rightly stays put.
            self._epoch = epoch
            return True
        before = self.generation
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
        old_geometry_records = self.geometry_records
        old_geometry_ids = self.geometry_id[:old_count].copy()
        old_geometry_by_slot = [None] * old_count
        for old_slot, gid in enumerate(old_geometry_ids):
            gid = int(gid)
            if 0 <= gid < len(old_geometry_records):
                old_geometry_by_slot[old_slot] = old_geometry_records[gid]

        new_ids = [None] * n
        survivors = set()          # slots whose cold columns are already right
        move_src, move_dst = [], []

        for slot in range(n):
            brush = brushes[slot]
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
            for name, *_ in _COLUMNS:
                column = getattr(self, name)
                column[dst] = column[src]

        # Reconstruct the temporary slot-indexed geometry view needed while
        # cold rows are being resolved. The published representation below is
        # compact: only actual convex rows occupy geometry-record slots.
        new_geometry_records = [None] * n
        for slot in survivors:
            old = old_slot_of_id.get(new_ids[slot])
            if old is not None and old < len(old_geometry_by_slot):
                new_geometry_records[slot] = old_geometry_by_slot[old]
        self.geometry_records = new_geometry_records

        self._resolve_warm_rows(range(n), brushes)
        fresh = [slot for slot in range(n) if slot not in survivors]
        self._resolve_cold_rows(fresh, brushes)
        for slot in fresh:
            new_geometry_records[slot] = self._geometry_record(slot, brushes[slot])

        # Publish a genuinely dense geometry index. geometry_id is an index
        # into geometry_records, not a RenderTable row number. This keeps AABB
        # brushes completely out of the geometry record table.
        self.geometry_id[:n] = -1
        geo_slots = np.flatnonzero(
            self.class_bits[:n] & CLASS_HAS_GEOMETRY).astype(np.int32)
        dense_records = []
        for slot in geo_slots:
            record = self.geometry_records[int(slot)]
            if record is None:
                continue
            self.geometry_id[int(slot)] = len(dense_records)
            dense_records.append(record)
        self.geometry_records = dense_records

        self.ids = new_ids
        self.slot_of_id = {bid: slot for slot, bid in enumerate(new_ids)
                           if bid is not None}
        # Publish exactly the row set reconciled above. The live list may grow
        # concurrently during benchmark/editor stress insertion; the next
        # frame will reconcile any newly appended rows.
        self.brushes = list(brushes[:n])
        self._row_tuple = tuple(self.brushes)
        refs = np.empty(n, dtype=object)
        for slot, brush in enumerate(self.brushes):
            refs[slot] = brush
        self.refs = refs
        self._shown_stale = True
        self._slot_of_obj = {id(brush): slot
                             for slot, brush in enumerate(self.brushes)}
        self.count = n
        self.dynamic_slots = np.flatnonzero(
            self.class_bits[:n] & CLASS_DYNAMIC).astype(np.int32)
        self.generation += 1
        return not survivors

    def refresh_transforms(self, brushes, slots):
        """Re-read the warm columns for *slots*: transform and ``hidden``.

        What a journalled move or a streaming park/unpark changes. A few rows
        are read one at a time; a batch (a Big World cell crossing parks
        thousands) is read with one column store each.
        """
        if len(slots) > 16:
            self._resolve_warm_rows(slots, brushes)
        else:
            for slot in slots:
                self._resolve_warm(slot, brushes[slot])
        self.rows_read += len(slots)

    def refresh_rows(self, brushes, slots):
        """Re-resolve the cold columns for *slots* after a semantic change.

        Row-local: the row set must be the one the table last reconciled.  A
        row whose change adds or removes convex geometry changes the dense
        geometry layout, so that case falls back to a full reconcile.
        """
        slots = sorted({int(slot) for slot in slots})
        self.rows_read += len(slots)
        self._resolve_warm_rows(slots, brushes)
        self._resolve_cold_rows(slots, brushes)
        for slot in slots:
            brush = brushes[slot]
            had_record = self.geometry_id[slot] >= 0
            record = self._geometry_record(slot, brush)
            if (record is not None) != had_record:
                self._reconcile(brushes, True, {id(brushes[s]) for s in slots})
                return
            if record is not None:
                self.geometry_records[int(self.geometry_id[slot])] = record
        if slots:
            self.dynamic_slots = np.flatnonzero(
                self.class_bits[:self.count] & CLASS_DYNAMIC).astype(np.int32)

    def _refresh_in_place(self, brushes, n, epoch, dirty_objects):
        """Apply a precise journal without a reconcile, when that is exact.

        A reconcile walks every row -- matching ids, re-reading every
        transform, rebuilding the slot maps -- which for one retextured brush
        on a 20 000-brush map is tens of milliseconds.  When the row set is
        exactly the one already reconciled (same dict at every slot) and the
        journal names the changed objects, the only rows whose cold columns can
        be wrong are those objects' rows, so only they are re-resolved.

        Returns ``False`` (having changed nothing) whenever that reasoning does
        not hold, and the caller reconciles.
        """
        if epoch is None or dirty_objects is None or n != self.count:
            return False
        if tuple(brushes) != self._row_tuple:
            return False
        slots = [self._slot_of_obj[obj_id] for obj_id in dirty_objects
                 if obj_id in self._slot_of_obj]
        for slot in slots:
            if brushes[slot].get('id') != self.ids[slot]:
                return False               # renamed row: the id map moves
        self.refresh_rows(brushes, slots)
        return True

    def refresh_edited(self, brushes, objects):
        """Re-read the rows an editor tool may be changing in place.

        A drag moves, resizes or reshapes the selection for hundreds of frames
        after its one undo checkpoint, writing the brush dicts directly. Those
        rows -- and only those -- have their transforms re-read, and any whose
        shape changed (its ``_geo_epoch`` moved) are re-resolved.
        """
        slot_of = self._slot_of_obj
        slots = [slot for slot in map(slot_of.get, map(id, objects))
                 if slot is not None]
        if not slots:
            return
        self.refresh_transforms(brushes, slots)
        stale = [slot for slot in slots
                 if (brushes[slot].get('_geo_epoch') or 0) != self.geo_epoch[slot]]
        if stale:
            self.refresh_rows(brushes, stale)


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
