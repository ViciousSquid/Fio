"""The dense render projection of Fio's world -- T3.

Fio already pays to describe its world numerically: the render-table cull projection
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

import math
import os

import numpy as np

from engine.constants import is_water_brush, normalize_color, water_high_quality
from engine.spatial import authored_hidden
import functools
import itertools
from collections import deque

from engine import brush_faces, brush_geometry, brush_placement, change_detection
from engine.change_journal import JOURNAL, OVERFLOW, STATE

#: Process-wide sources of :attr:`RenderTable.geometry_version` values (two
#: tables never share one unless one copied the other's records) and of
#: :attr:`GeometryRecord.serial`.
_geometry_versions = itertools.count(1)
_geometry_serials = itertools.count(1)

# --------------------------------------------------------------------------
# Classification bits
# --------------------------------------------------------------------------

def _frozen(value):
    """A hashable, exact copy of an authored plane value."""
    if isinstance(value, dict):
        return tuple(sorted((key, _frozen(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_frozen(item) for item in value)
    return value


class GeometryRecord:
    """Dense cold render record for one convex geometry-table entry.

    Immutable once built, so the two render buffers' tables can share it.
    ``serial`` is a number no other record has had: a consumer that keeps
    state per geometry handle (the renderer's GPU meshes) recognises a record
    it has seen -- wherever a reconcile moved its handle -- by its serial.

    ``origin``/``scale`` are the record's *anchor*: the AABB the mesh is
    expressed in (Fio's angled brushes keep the AABB brushes' unit-cube
    transform, so their vertices are stored relative to it). The anchor is
    the shape's own bounds (:attr:`ConvexGeometry.shape_box`, the box
    :mod:`engine.brush_placement` derives for the brush), so it is a function
    of the planes alone: the row's transform -- those bounds, displaced by a
    mover and turned by its spin -- is what carries the mesh to where it is,
    and the dict's ``pos``/``size`` copy is never read for it.

    ``materials`` maps each convex face (by ``id``) to the
    :class:`engine.brush_faces.Material` it is drawn with -- its one material
    record, resolved once here (a legacy brush's per-tag entries by
    :data:`engine.brush_faces.PRECEDENCE`, brush-wide tiling applied).
    ``natural_scale`` is the same record's Natural flag per face.

    ``owner`` is the brush's stable ``id``. Since Stage 2d the planes are in
    the brush's local frame, so two brushes of the same shape and materials
    would otherwise have the same surface and share one GPU mesh -- and with
    it one GPU transform slot, which the batched path cannot draw (it falls
    back to a draw per mesh). Keying by owner too keeps M2's one mesh and one
    slot per brush, while a move or an undo of a move -- the same brush, the
    same surface -- still finds its mesh and builds nothing.

    ``surface`` is the complete input of the mesh built from this record, as
    one exact, hashable value -- every plane as authored (offset, normal,
    texture, UV mapping, texture basis, face tag), each face's resolved
    material, the anchor and the owner -- and nothing else. So a cache keyed
    by it holds the invariant both ways: equal keys build identical vertex data, whatever
    the brush's current position, and any change to what the mesh is built
    from is a different key. It carries no geometry epoch (an epoch belongs to
    a dict, and an undo restores every brush as a new dict), so a restored
    but unchanged brush finds its mesh again.
    """
    __slots__ = ('signature', 'convex', 'origin', 'scale', 'materials',
                 'serial', 'surface', 'owner')

    def __init__(self, signature, convex, origin, scale, materials, owner=None):
        self.signature = signature
        self.convex = convex
        self.origin = origin
        self.scale = scale
        self.materials = materials
        self.owner = owner
        self.serial = next(_geometry_serials)
        self.surface = (
            tuple(_frozen(plane) for plane in convex.planes),
            tuple(materials[id(face)] for face in convex.faces),
            tuple(origin.tolist()), tuple(scale.tolist()), owner)

    @property
    def natural_scale(self):
        """``{id(face): bool}``: each face's Natural flag (from
        :attr:`materials`)."""
        return {key: material.natural for key, material in self.materials.items()}

    def describes(self, signature, convex, materials, owner=None):
        """Whether this record already describes that surface.

        ``pos``/``size`` are not compared: the anchor is derived from the
        planes, so it cannot differ while the surface is unchanged, and a
        placement or a mover's displacement is a transform.
        """
        return (self.convex is convex
                and self.signature == signature
                and self.materials == materials
                and self.owner == owner)

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
#: start at ``i * 6``.  Mirrors ``engine.renderer.core.geometry._CUBE_FACE_KEYS``; the two must
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


def _num(value, default):
    """An authored number as a finite float, or *default* if it is not one.

    Shader parameters are free-form in the property editor, the console's
    setprop and hand-edited maps; one ``"abc"`` must fall back to the value the
    shader would have used, not take the frame (and the logic thread) down.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(default)
    return value if math.isfinite(value) else float(default)


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
        # A convex brush's faces are its planes: textured if any face's one
        # material record names a real texture (engine.brush_faces).
        if brush_faces.is_textured(brush):
            bits |= CLASS_TEXTURED
    else:
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
    #: "Wave Spacing": 0 the tight, choppy waves, 1 the broad ones (the
    #: default); the water shaders blend their wavelengths between the two.
    ('water_wave_spread', (), np.float32, 0.0),
    #: The brush's "High quality" flag: depth-based colour, shoreline foam,
    #: caustics and screen-space reflections (see engine.renderer.WATER_QUALITIES).
    ('water_high_quality', (), bool, False),
    ('glass_color', (3,), np.float32, 0.0),
    #: opacity, distortion, refraction, roughness, fresnel
    ('glass_params', (5,), np.float32, 0.0),
    ('fog_color', (3,), np.float32, 0.0),
    #: density, noise_scale
    ('fog_params', (2,), np.float32, 0.0),
)


#: Transform writes a table remembers (M4): a reader that last caught up
#: further back than this many writes is told to treat every row as written.
TRANSFORM_LOG = 64

#: The transform columns: read-only outside the table's write methods, so a
#: write that bypasses the change log raises instead of going unseen (M4).
_SEALED_COLUMNS = ('bounds', 'rot')

#: Whether the transform columns are sealed. A check for tests and
#: debugging -- the suite turns it on (conftest.py: ``FIO_SEAL_COLUMNS``), as
#: does the oracle -- not paid in play: toggling the two columns' writeable
#: flags around every write cost the mover publication ~1.5 us a frame.
SEAL_COLUMNS = change_detection.ORACLE or bool(os.environ.get('FIO_SEAL_COLUMNS'))

_NO_ROWS = np.empty(0, dtype=np.intp)
_NO_ROWS.flags.writeable = False

#: A row's centre within ``bounds`` and its rotation within ``rot``, as
#: offsets into the flattened row (RenderTable.transform_index).
_CENTRE = np.arange(3, dtype=np.intp)
_ROTATION = np.arange(4, dtype=np.intp)


def _writes(method):
    """Mark a RenderTable method as one that may write the transform columns:
    they are writable while the outermost such method runs, read-only
    otherwise. Every such method logs the rows it wrote."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        if not SEAL_COLUMNS:
            return method(self, *args, **kwargs)
        if not self._writing:
            self._seal(False)
        self._writing += 1
        try:
            return method(self, *args, **kwargs)
        finally:
            self._writing -= 1
            if not self._writing:
                self._seal(True)
    return wrapper


#: The columns a row's classification and material are made of: every one
#: but the warm transform and visibility (RenderTable.material_version).
_MATERIAL_COLUMNS = tuple(name for name, *_ in _COLUMNS
                          if name not in ('bounds', 'rot', 'hidden'))


def _can_adopt(peer, rows, epoch, dirty_objects, peer_dirty):
    """Whether a table that must re-resolve every row can copy *peer* instead.

    The peer must hold exactly *rows*, and be either at *epoch* or behind it by
    a precisely journalled set of objects (*peer_dirty*, from the editor's
    render-dirty history since the peer's epoch).
    """
    if peer is None or dirty_objects is not None or epoch is None:
        return False
    if peer._epoch is None or peer._row_tuple != rows:
        return False
    return peer._epoch == epoch or peer_dirty is not None


class RenderTable:
    """A dense projection of a brush list, kept current by change, not polling.

    Rows are addressed by ``slot`` and named by ``brush['id']``.  Build one and
    :meth:`begin_frame` it once per frame: it reconciles when the editor's
    world epoch moves, re-reads the rows :mod:`engine.change_journal` names,
    and re-reads the transforms of the movers and doors, which move every tick.
    """

    __slots__ = (tuple(name for name, *_ in _COLUMNS) + (
        'generation', 'count', 'ids', 'slot_of_id', 'brushes',
        'dynamic_slots', 'geometry_records', 'geometry_serial',
        'geometry_version',
        '_tex_ids', '_tex_names',
        '_epoch', '_row_tuple', '_slot_of_obj', 'rows_read', 'refs',
        '_shown_mask', '_shown_slots', '_shown_stale',
        '_row_changes', 'row_change_serial', '_row_change_base', 'turning',
        'material_version', 'extent_version', 'transform_version',
        '_transform_log', '_writing', 'shown_version', '_row_list',
        '_row_list_version', '__weakref__'))

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
        #: Move whenever a row's classification or material columns
        #: (everything but bounds, rot and hidden) or its half-extents
        #: actually change -- not when a mover's centre is written every
        #: tick -- so a renderer can keep what it derives from them (pass
        #: groups, payloads, face batches) until they do. A reconcile or an
        #: adopt moves both, with :attr:`generation`.
        self.material_version = 0
        self.extent_version = 0
        #: Moves with every write to the transform columns (``bounds``,
        #: ``rot``), which logs the rows written (M4): a reader that knows
        #: the version it last caught up to asks :meth:`transforms_since`
        #: for exactly the rows written since, instead of comparing columns.
        self.transform_version = 0
        #: ``(version, rows)`` per logged write; created on the first one.
        self._transform_log = None
        self._writing = 0
        #: Moves whenever :meth:`shown` recomputes (a ``hidden`` value or the
        #: row set changed).
        self.shown_version = 0
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
        #: ``geometry_records[h].serial`` per handle: which records changed,
        #: and where a reconcile moved the survivors, as one array compare.
        self.geometry_serial = np.zeros(0, dtype=np.int64)
        #: Moves whenever ``geometry_records`` does -- a reconcile, a row-local
        #: refresh that replaced a record, an adopt -- and at no other time: a
        #: brush whose surface did not change keeps its record. A consumer
        #: holding state per geometry handle checks this one integer per frame
        #: and, when it moved, ``geometry_serial`` for the handles that did.
        self.geometry_version = next(_geometry_versions)
        #: slot -> the brush dict, retained only for cache-boundary resolution
        #: and resource/object lookup. Frame rendering consumes the dense
        #: numeric columns; this reference array is rebuilt only when rows change.
        self.refs = np.empty(0, dtype=object)
        #: ``~hidden`` and its slots, cached until a ``hidden`` value changes.
        self._shown_mask = np.empty(0, dtype=bool)
        self._shown_slots = np.empty(0, dtype=np.intp)
        self._shown_stale = True
        # Lazy: an editor that never enables occlusion never builds a cell
        # index or pays to track changed rows. When used, the publisher
        # journals only the rows it was going to refresh anyway.
        # The rows refreshed since, in batches, for a reader that keeps
        # something per row (the renderer's occlusion index): it asks
        # row_changes_since(the serial it last saw). Nothing of the reader's
        # is kept here.
        self._row_changes = []
        self.row_change_serial = 0
        self._row_change_base = 0
        #: Whether any row has ever been given a spin (a non-zero ``rot``
        #: angle) -- by this table's row resolution or the mover table's
        #: publication, the column's writers. Never cleared: until it is set,
        #: no row turns and a row's AABB is its ``bounds`` (the frustum cull
        #: skips the resolver's turned bounds).
        self.turning = False
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
        #: The live row list the row set was last taken from, when it says
        #: when its rows change (engine.row_list.RowList), and the version it
        #: was at: the same list at the same version is the same rows, with
        #: no row compared (M4).
        self._row_list = None
        self._row_list_version = None
        #: ``id(brush)`` -> slot, for applying the change journals.
        self._slot_of_obj: dict = {}
        #: Rows whose brush dict the last :meth:`begin_frame` read (Debug
        #: Tables shows it: on a still frame it is the movers and doors).
        self.rows_read = 0
        if SEAL_COLUMNS:
            self._seal(True)
        JOURNAL.subscribe(self)

    # -- transform change records (M4) -------------------------------------

    def _seal(self, sealed):
        self.bounds.flags.writeable = not sealed      # _SEALED_COLUMNS
        self.rot.flags.writeable = not sealed

    def _note_transforms(self, rows):
        """Log that *rows* (slot indices) had their transform written."""
        if len(rows):
            self.transform_version += 1
            if self._transform_log is None:
                self._transform_log = deque(maxlen=TRANSFORM_LOG)
            self._transform_log.append((self.transform_version, rows))

    #: Batches of refreshed rows kept for :meth:`row_changes_since`.
    ROW_CHANGE_LOG = 128

    def _note_row_changes(self, slots):
        """Log one batch of refreshed rows (a copy: O(the batch), no per-row
        Python)."""
        log = self._row_changes
        log.append(np.array(slots, dtype=np.intp))
        self.row_change_serial += 1
        if len(log) > self.ROW_CHANGE_LOG:
            drop = len(log) // 2
            del log[:drop]
            self._row_change_base += drop

    def _forget_row_changes(self):
        """Every row may have changed (a reconcile, an adopt): readers
        further back than now find no log and rebuild."""
        self._row_changes = []
        self.row_change_serial += 1
        self._row_change_base = self.row_change_serial

    def row_changes_since(self, serial):
        """The rows refreshed (``refresh_transforms``, ``refresh_rows``)
        after *serial* -- slot indices, possibly repeated -- or ``None``
        when the log does not reach back that far and the caller must treat
        every row as changed. The caller then records
        :attr:`row_change_serial`. Readers also check :attr:`generation`."""
        if serial >= self.row_change_serial:
            return _NO_ROWS
        if serial < self._row_change_base:
            return None
        batches = self._row_changes[serial - self._row_change_base:]
        return batches[0] if len(batches) == 1 else np.concatenate(batches)

    def _forget_transforms(self):
        """Every row is to be treated as written (a reconcile, an adopt):
        readers further back than now find no log and rebuild."""
        self.transform_version += 1
        self._transform_log = None

    def transforms_since(self, version):
        """The rows whose transform was written after *version* (slot
        indices in no particular order, possibly repeated), or ``None`` when
        the log does not reach back that far and the caller must treat every
        row as written.

        Readers also check :attr:`generation`: a reconcile renumbers slots.
        Costs the rows written since, not the table's size."""
        if version >= self.transform_version:
            return _NO_ROWS
        log = self._transform_log
        if not log or log[0][0] > version + 1:
            return None
        parts = [rows for written, rows in log if written > version]
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    def transform_index(self, slots):
        """``(centre, rotation)`` flat indices of *slots* into ``bounds`` and
        ``rot`` for :meth:`write_transforms_many`: a store through them is a
        one-dimensional fancy store, about three times cheaper than
        ``bounds[slots, :3] = ...``. They depend on the slots alone, not on
        the columns' capacity, so a caller keeps them as long as the slots."""
        slots = np.asarray(slots, dtype=np.intp)[:, None]
        return ((slots * self.bounds.shape[1] + _CENTRE).ravel(),
                (slots * self.rot.shape[1] + _ROTATION).ravel())

    def write_transforms_many(self, writes):
        """Store ``(slots, centre, rot, index)`` for each of *writes* in order,
        each logged: the mover table's publication, once a frame, so the seal
        is handled here rather than by the decorator. *slots* is an intp
        array; *rot* an array, a float for every value, or None; *index* the
        slots' :meth:`transform_index`, or None. The caller sets
        :attr:`turning` when a *rot* turns a row (it knows already)."""
        opened = SEAL_COLUMNS and not self._writing
        if opened:
            self._seal(False)
        try:
            for slots, centre, rot, index in writes:
                if index is None:
                    self.bounds[slots, :3] = centre
                    if rot is not None:
                        self.rot[slots] = rot
                else:
                    # The columns are whole C-ordered arrays: reshape is a view.
                    flat = self.bounds.reshape(-1)
                    flat[index[0]] = centre.reshape(-1)
                    if rot is not None:
                        self.rot.reshape(-1)[index[1]] = (
                            rot if type(rot) is float else rot.reshape(-1))
                    if change_detection.ORACLE and not np.shares_memory(flat, self.bounds):
                        change_detection.missed("bounds written through a copy")
                self._note_transforms(slots)
        finally:
            if opened:
                self._seal(True)

    @_writes
    def write_transforms(self, slots, centre=None, rot=None, half=None):
        """Store runtime transforms for *slots*: the way a writer outside the
        table -- the mover table's publication -- writes them, so the rows
        are logged (M4). *centre* ``(k, 3)``, *rot* ``(k, 4)`` (axis, angle in
        degrees), *half* ``(k, 3)``; any may be omitted."""
        slots = np.asarray(slots, dtype=np.intp)
        if not len(slots):
            return
        if centre is not None:
            self.bounds[slots, :3] = centre
        if half is not None:
            was = self.bounds[slots, 3:]
            self.bounds[slots, 3:] = half
            if not np.array_equal(was, self.bounds[slots, 3:]):
                self.extent_version += 1
        if rot is not None:
            self.rot[slots] = rot
            if not self.turning and (self.rot[slots, 3] != 0).any():
                self.turning = True     # the column's other writer
        self._note_transforms(slots)

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

    def _geometry_record(self, slot, brush, previous=None):
        """The dense cold geometry record for one row, or ``None`` for a box.

        Returned rather than stored: where it goes depends on the caller.  A
        reconcile collects records by slot and then compacts them into the
        dense :attr:`geometry_records`; a row-local refresh replaces the row's
        existing dense entry in place.

        *previous* is the row's record before this refresh. When the surface
        it describes is unchanged -- the brush was recoloured, renamed,
        toggled or only moved, not reshaped or retextured -- it is returned as
        it is, anchor included, so nothing downstream sees a geometry change.
        """
        if not (self.class_bits[slot] & CLASS_HAS_GEOMETRY):
            return None
        convex = brush_geometry.local_convex(brush)
        if convex is None or not convex.is_valid:
            return None
        # The anchor is the shape's own bounds in its local frame (Stage 2d:
        # centred on the origin), never the dict's size copy: so the mesh --
        # and its cache key -- is a function of the planes and their
        # materials alone, the same wherever the brush is placed, and the
        # row's transform (its placement, carried by a mover) puts it there.
        cx, cy, cz, hx, hy, hz = convex.shape_box
        origin = np.array((cx, cy, cz), dtype=np.float64)
        scale = np.array((hx * 2.0, hy * 2.0, hz * 2.0), dtype=np.float64)
        materials = brush_faces.convex_materials(
            convex, brush.get('texture_tiling', False))
        signature = brush_geometry.geometry_signature(brush)
        owner = brush.get('id')
        if previous is not None and previous.describes(signature, convex, materials, owner):
            return previous
        return GeometryRecord(signature, convex, origin, scale, materials, owner)

    # -- row resolution ----------------------------------------------------

    def _resolve_warm(self, slot, brush):
        """Transform and visibility for one row.  Cheap; per tick for movers."""
        hidden = bool(brush.get('hidden', False))
        if hidden != self.hidden[slot]:
            self.hidden[slot] = hidden
            self._shown_stale = True
        self.bounds[slot] = brush_placement.placed_box(brush)
        spin = brush_placement.runtime_spin(brush)
        if spin is not None:
            self.rot[slot] = spin
            self.turning = True
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
            if ('geometry' in brush or brush.get('is_mover')
                    or brush.get('is_door')):
                bounds[row] = brush_placement.placed_box(brush)
                spin = brush_placement.runtime_spin(brush)
                if spin is not None:
                    rot[row] = spin
                    self.turning = True
            else:
                # brush_placement's box rule, inline: this loop runs over
                # every row of a world on a cold build, and a box that is
                # neither a mover nor a door has no runtime transform.
                pos = brush.get('pos') or (0.0, 0.0, 0.0)
                size = brush.get('size') or (64.0, 64.0, 64.0)
                bounds[row] = (pos[0], pos[1], pos[2],
                               size[0] * 0.5, size[1] * 0.5, size[2] * 0.5)
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
            intensity = _num(brush.get('glow_intensity'), 10.0)
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
    _SPECIAL_COLUMNS = ('water_tint', 'water_params', 'water_plane', 'water_wave_spread',
                        'water_high_quality', 'glass_color', 'glass_params', 'fog_color', 'fog_params')

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
                    _num(brush.get('water_opacity'), 0.5),
                    _num(brush.get('water_fresnel', brush.get('water_reflectivity', 0.65)), 0.65),
                    _num(brush.get('water_wave_height'), 0.5),
                    1.0 if brush.get('water_wave_enabled', True) else 0.0,
                    _num(brush.get('water_distortion'), 0.5),
                    _num(brush.get('water_refraction'), 1.333),
                    _num(brush.get('water_roughness'), 0.0),
                )
                self.water_plane[slot] = bool(brush.get('water_plane', False))
                self.water_wave_spread[slot] = min(max(
                    _num(brush.get('water_wave_spread'), 1.0), 0.0), 1.0)
                self.water_high_quality[slot] = water_high_quality(brush)
            if b & CLASS_GLASS:
                self.glass_color[slot] = normalize_color(
                    brush.get('glass_color', [0.7, 0.85, 0.95]))
                self.glass_params[slot] = (
                    _num(brush.get('glass_opacity'), 0.3),
                    _num(brush.get('glass_distortion'), 0.5),
                    _num(brush.get('glass_refraction'), 1.5),
                    _num(brush.get('glass_roughness'), 0.0),
                    _num(brush.get('glass_fresnel'), 0.5),
                )
            if b & CLASS_FOG:
                self.fog_color[slot] = normalize_color(
                    brush.get('fog_color', [0.5, 0.6, 0.7]))
                self.fog_params[slot] = (
                    _num(brush.get('fog_density'), 0.01),
                    _num(brush.get('fog_noise_scale'), 0.01),
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
                    edited=(), peer=None, peer_dirty=None):
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
        *peer_dirty* is what the editor journal says changed since the peer's
        own epoch, when it can say so precisely: the peer is then adopted even
        though it is an edit or two behind, and just those rows re-resolved.
        Without it, one checkpoint landing between the two buffers' frames
        made the second buffer rebuild every row as well.
        """
        started = change_detection.now()
        version = getattr(brushes, 'row_version', None)
        if (version is not None and brushes is self._row_list
                and version == self._row_list_version):
            # The same RowList at the same version: the same rows (M4),
            # whatever the epoch says about what is in them. The frame works
            # on the table's own frozen tuple of them.
            if change_detection.ORACLE and tuple(brushes) != self._row_tuple:
                change_detection.missed(
                    "the row list changed without moving its version")
            brushes = self._row_tuple
            n = len(brushes)
            rows_known = True
            same_rows = epoch is not None and epoch == self._epoch
            examined = 0
        else:
            live = brushes
            brushes = tuple(brushes)
            n = len(brushes)
            rows_known = False
            # Compared with the rows last reconciled: an identity test a row,
            # in C.
            same_rows = (epoch is not None and epoch == self._epoch
                         and brushes == self._row_tuple)
            examined = n
            # Whatever happens below, the table then holds this row set.
            self._row_list = live if version is not None else None
            self._row_list_version = version
        change_detection.DETECT.add('render_table.rows', examined,
                                    change_detection.now() - started)
        changes = JOURNAL.drain(self)
        self.rows_read = 0
        resolved_all = False
        if not same_rows:
            cold_dirty = epoch is None or epoch != self._epoch
            if self._refresh_in_place(brushes, n, epoch, dirty_objects, rows_known):
                pass
            elif _can_adopt(peer, brushes, epoch, dirty_objects, peer_dirty):
                self.adopt(peer)
                if (peer_dirty and not self._refresh_in_place(
                        brushes, n, epoch, peer_dirty)):
                    self._reconcile(brushes, True, peer_dirty)
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
            # Published as they are: the same objects until the next
            # recompute, never written in place (M4).
            self._shown_mask.flags.writeable = False
            self._shown_slots.flags.writeable = False
            self._shown_stale = False
            self.shown_version += 1
        return self._shown_mask, self._shown_slots

    @_writes
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
        self.geometry_serial = peer.geometry_serial.copy()
        self.geometry_version = peer.geometry_version   # same records
        self._tex_ids = dict(peer._tex_ids)
        self._tex_names = list(peer._tex_names)
        self._epoch = peer._epoch
        self._row_tuple = peer._row_tuple
        self._row_list = None               # compared once, next frame
        self._slot_of_obj = dict(peer._slot_of_obj)
        self._shown_stale = True
        self._forget_row_changes()
        self.generation += 1
        self.material_version += 1
        self.extent_version += 1
        self._forget_transforms()

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
        self._row_list = None               # rows taken as given, next frame compares
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

    @_writes
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
            # The same brush re-resolved because it is in the dirty journal:
            # its old record may still describe its surface exactly.
            old = old_slot_of_id.get(new_ids[slot])
            previous = (old_geometry_by_slot[old]
                        if old is not None and old < old_count
                        and old_brushes[old] is brushes[slot] else None)
            new_geometry_records[slot] = self._geometry_record(
                slot, brushes[slot], previous)

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
        self.geometry_serial = np.fromiter(
            (record.serial for record in dense_records), dtype=np.int64,
            count=len(dense_records))
        self.geometry_version = next(_geometry_versions)

        self.ids = new_ids
        self.slot_of_id = {bid: slot for slot, bid in enumerate(new_ids)
                           if bid is not None}
        # Publish exactly the row set reconciled above. The live list may grow
        # concurrently (insertion from another thread); the next
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
        self._forget_row_changes()    # mapping or row set may have changed
        self.generation += 1
        self.material_version += 1
        self.extent_version += 1
        self._forget_transforms()
        return not survivors

    @_writes
    def refresh_transforms(self, brushes, slots):
        """Re-read the warm columns for *slots*: transform and ``hidden``.

        What a journalled move or a streaming park/unpark changes. A few rows
        are read one at a time; a batch (a Big World cell crossing parks
        thousands) is read with one column store each.
        """
        self._note_row_changes(slots)
        idx = np.asarray(slots, dtype=np.intp)
        half = self.bounds[idx, 3:]
        if len(slots) > 16:
            self._resolve_warm_rows(slots, brushes)
        else:
            for slot in slots:
                self._resolve_warm(slot, brushes[slot])
        if not np.array_equal(self.bounds[idx, 3:], half):
            self.extent_version += 1
        self._note_transforms(idx)
        self.rows_read += len(slots)

    @_writes
    def refresh_rows(self, brushes, slots):
        """Re-resolve the cold columns for *slots* after a semantic change.

        Row-local: the row set must be the one the table last reconciled.  A
        row whose change adds or removes convex geometry changes the dense
        geometry layout, so that case falls back to a full reconcile.
        """
        slots = sorted({int(slot) for slot in slots})
        self._note_row_changes(slots)
        self.rows_read += len(slots)
        # What the rows held, to tell whether this edit changed any of it
        # (an edit-time cost: a handful of rows).
        before = [getattr(self, name)[slots] for name in _MATERIAL_COLUMNS]
        half = self.bounds[slots, 3:]
        self._resolve_warm_rows(slots, brushes)
        self._resolve_cold_rows(slots, brushes)
        self._note_transforms(np.asarray(slots, dtype=np.intp))
        if not np.array_equal(self.bounds[slots, 3:], half):
            self.extent_version += 1
        if not all(np.array_equal(getattr(self, name)[slots], was)
                   for name, was in zip(_MATERIAL_COLUMNS, before)):
            self.material_version += 1
        replaced = False
        for slot in slots:
            brush = brushes[slot]
            gid = int(self.geometry_id[slot])
            had_record = gid >= 0
            previous = self.geometry_records[gid] if had_record else None
            record = self._geometry_record(slot, brush, previous)
            if (record is not None) != had_record:
                self._reconcile(brushes, True, {id(brushes[s]) for s in slots})
                return
            if record is not None and record is not previous:
                self.geometry_records[gid] = record
                self.geometry_serial[gid] = record.serial
                replaced = True
        if replaced:
            self.geometry_version = next(_geometry_versions)
        if slots:
            self.dynamic_slots = np.flatnonzero(
                self.class_bits[:self.count] & CLASS_DYNAMIC).astype(np.int32)

    def _refresh_in_place(self, brushes, n, epoch, dirty_objects, rows_known=False):
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
        if not rows_known and tuple(brushes) != self._row_tuple:
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
    rotated, r = rotation_matrices(table.rot[slots])
    if len(rotated):
        rs = size[rotated]
        ri = inv_size[rotated]
        for col in range(3):
            for row in range(3):
                models[rotated, col * 4 + row] = r[:, row, col] * rs[:, col]
                normals[rotated, col * 3 + row] = r[:, row, col] * ri[:, col]

    return models, normals


#: Shared with every consumer that must see a brush where the renderer drew
#: it; defined by the placement resolver (GL-free) and re-exported here.
rotation_matrices = brush_placement.rotation_matrices
