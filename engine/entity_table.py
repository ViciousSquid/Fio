"""The dense entity projection -- the other half of T3.

:mod:`engine.render_table` gave Fio's *brushes* a dense projection: one row per
brush, columns of plain NumPy, so the renderer stopped asking every visible
brush what it was on every frame.  Entities did not get one.  The comment in
``renderer_F.render_scene`` says so plainly --

    # Things keep the object path: they are not projected, and their
    # render kinds are entity semantics rather than material state.

-- and the cost of that is two Python walks over every entity in the level,
every frame:

* the logic thread's publish loop, which reads ``thing.pos``, stores two floats
  into a NumPy buffer *one element at a time*, and runs three ``isinstance``
  tests, to produce the position buffer, the light list and the visible set;
* ``_sort_objects``' Thing half, which runs four more ``isinstance`` tests, a
  ``str().lower()``, a tuple build and a tuple compare per entity to decide
  which pass draws it.

Measured at 961 entities that is 0.65 ms and 0.81 ms per frame -- together
about a tenth of a 60 Hz budget, spent re-deriving facts that are fixed for an
entity's lifetime.

"Entity semantics rather than material state" is true and is not a reason to
keep them in Python.  *Which pass draws an entity* is a resolution of its class
and four authored properties; it is as static as a brush's shader.  What is
genuinely dynamic -- where the entity is, whether it is hidden, whether a
pickup has been collected -- is dynamic for brushes too, and the brush table
already has the discipline for it.  So this module applies the same one.

What it is, and is not
----------------------
Identical in kind to :class:`engine.render_table.RenderTable`, deliberately:

* it stores nothing authored.  Every column resolves something the entity
  already says, so rebuilding the table from ``EditorState.things`` gives
  identical bits;
* it is never written back to.  The single exception is the ``glm``-vector
  normalisation the publish loop has always done, which repairs an entity whose
  ``pos`` is not a list -- and which this module does exactly when the old loop
  did, so the projection stays read-only with respect to everything else;
* identity stays the entity's own UUID.  ``slot`` is an *address*, valid within
  one :attr:`generation`; ``properties['id']`` is the *name*.  Frame code
  indexes columns by slot and never looks a name up.

Refresh discipline, from the brush table
----------------------------------------
``cold``
    :attr:`class_bits` -- the entity's class and its representation
    (``model_path`` / ``render_mode`` / ``sprite_path``).  Resolved when the
    row set changes or the world epoch moves, never per frame.  Nothing in Fio
    writes those three at runtime: the property panel, the console and the
    model importer all write them through an editor gesture, which bumps the
    epoch.

``warm``
    :attr:`pos` -- one bulk store per frame.  Entities move constantly and have
    no per-entity notification, exactly like movers and doors.

``live``
    ``hidden``, read per frame and never cached, because Big World parks
    entities by writing it directly and relies on every per-frame consumer
    seeing it (:data:`engine.spatial.PARKED_HIDDEN_KEY`).  The same field the
    brush table singles out, for the same reason.

The gap this leaves is the brush table's gap: an entity mutated in place by
something that bumps no counter and changes no row is not noticed.  Every path
Fio itself has goes through an editor gesture or changes the entity set.

Entity classes are imported defensively, the way :mod:`engine.logic_thread`
imports them, so a tier without ``editor.things`` -- the standalone player, a
head-less test -- still imports this module.
"""

from __future__ import annotations

from itertools import chain

import numpy as np

# Defensive, as everywhere else in engine/: editor.things pulls in PyQt5, and
# the standalone player tier does not have it.  A tier without the classes
# classifies every entity as a plain Thing, which is what it is there.
try:
    from editor.things import (Thing, PathNode, Portal, Pickup, Prop, Monster,
                               LogicGate, LogicRelay, LogicTimer, LevelChanger,
                               Light)
except ImportError:                                   # pragma: no cover
    Thing = PathNode = Portal = Pickup = Prop = Monster = None
    LogicGate = LogicRelay = LogicTimer = LevelChanger = Light = None


# --------------------------------------------------------------------------
# Classification bits
# --------------------------------------------------------------------------
#
# One uint16 per entity, replacing the isinstance chain and property reads
# ``_sort_objects`` ran per visible entity per frame.  The bits record the
# *inputs* to that chain rather than its verdict, because the verdict also
# depends on two per-frame flags (play mode, show-sprites) and on the live
# hidden flag -- and reproducing the chain as mask arithmetic over the inputs
# is what makes the two paths provably the same answer.

#: Never drawn by the Thing passes: a PathNode (it has its own debug pass), or
#: an object that is not a Thing at all.
ENT_SKIP            = 1 << 0
#: Drawn as a sprite whatever else is true -- a Portal, or the render-snapshot
#: dict the logic thread publishes in place of a live Monster.
ENT_ALWAYS_SPRITE   = 1 << 1
#: Carries a ``model_path``.
ENT_HAS_MODEL       = 1 << 2
#: ``render_mode`` resolves to ``'model'`` (the default when unset).
ENT_MODE_MODEL      = 1 << 3
#: ``render_mode`` resolves to ``'billboard'``.
ENT_MODE_BILLBOARD  = 1 << 4
#: Carries a ``sprite_path``.
ENT_HAS_SPRITE      = 1 << 5
#: A Pickup.  Tested first by ``_thing_render_kind``, so it wins over the bits
#: below even for an entity that would otherwise classify as a sprite.
ENT_PICKUP          = 1 << 6
#: Monster / LogicGate / LogicRelay / LogicTimer / LevelChanger -- the classes
#: ``_thing_render_kind`` calls ``entity_sprite``.
ENT_ENTITY_SPRITE   = 1 << 7
#: A Prop.  Drawn as a sprite only when it is a billboard with a sprite.
ENT_PROP            = 1 << 8
#: A Light.  Collected into the frame's light list; never a sprite in play.
ENT_LIGHT           = 1 << 9
#: A Monster, whose published reference is refreshed from
#: ``get_render_snapshot()`` every frame -- the entity half of the brush
#: table's dynamic rows.
ENT_MONSTER         = 1 << 10
#: A Portal.  Distinct from :data:`ENT_ALWAYS_SPRITE`, which a monster snapshot
#: also carries, because the distance cull exempts Portals and Lights and must
#: not exempt monsters.
ENT_PORTAL          = 1 << 11

#: Never dropped by the broad-phase distance cull, whatever its distance --
#: ``Renderer_F._cull_keep_thing``'s predicate, as bits.  Lighting and portal
#: rendering are unaffected by the cull, which is deliberate and predates this
#: projection.
ENT_CULL_EXEMPT = ENT_LIGHT | ENT_PORTAL

#: Indexable for debug text and test failure messages.
BIT_NAMES = (
    (ENT_SKIP, 'SKIP'), (ENT_ALWAYS_SPRITE, 'ALWAYS_SPRITE'),
    (ENT_HAS_MODEL, 'HAS_MODEL'), (ENT_MODE_MODEL, 'MODE_MODEL'),
    (ENT_MODE_BILLBOARD, 'MODE_BILLBOARD'), (ENT_HAS_SPRITE, 'HAS_SPRITE'),
    (ENT_PICKUP, 'PICKUP'), (ENT_ENTITY_SPRITE, 'ENTITY_SPRITE'),
    (ENT_PROP, 'PROP'), (ENT_LIGHT, 'LIGHT'), (ENT_MONSTER, 'MONSTER'),
    (ENT_PORTAL, 'PORTAL'),
)


def describe(bits) -> str:
    """The set bits of a classification word, for a readable assertion."""
    return '|'.join(name for bit, name in BIT_NAMES if bits & bit) or 'NONE'


def _entity_class_bits(thing) -> int:
    """The classification word for one entity.

    Runs once per entity per *edit*, in place of the per-frame chain in
    ``_sort_objects``.  Reads the same classes and the same property keys that
    chain read, in the same order, so the projection cannot disagree with it.
    """
    # A monster render snapshot is a plain dict carrying 'monster_type'; the
    # renderer's chain tests for exactly that before it tests for a Thing.
    if isinstance(thing, dict):
        return ENT_ALWAYS_SPRITE if 'monster_type' in thing else ENT_SKIP
    if PathNode is not None and isinstance(thing, PathNode):
        return ENT_SKIP
    if Portal is not None and isinstance(thing, Portal):
        return ENT_ALWAYS_SPRITE | ENT_PORTAL
    if Thing is not None and not isinstance(thing, Thing):
        return ENT_SKIP

    bits = 0
    # A live Monster is published as its render snapshot, so its row classifies
    # as what will actually be handed over -- a dict with 'monster_type'.
    if Monster is not None and isinstance(thing, Monster):
        bits |= ENT_MONSTER | ENT_ALWAYS_SPRITE

    props = getattr(thing, 'properties', None)
    if not isinstance(props, dict):
        props = {}

    if props.get('model_path'):
        bits |= ENT_HAS_MODEL
    if props.get('sprite_path'):
        bits |= ENT_HAS_SPRITE
    render_mode = str(props.get('render_mode', 'model')).lower()
    if render_mode == 'model':
        bits |= ENT_MODE_MODEL
    elif render_mode == 'billboard':
        bits |= ENT_MODE_BILLBOARD

    if Pickup is not None and isinstance(thing, Pickup):
        bits |= ENT_PICKUP
    entity_sprite_types = tuple(
        c for c in (Monster, LogicGate, LogicRelay, LogicTimer, LevelChanger)
        if c is not None)
    if entity_sprite_types and isinstance(thing, entity_sprite_types):
        bits |= ENT_ENTITY_SPRITE
    if Prop is not None and isinstance(thing, Prop):
        bits |= ENT_PROP
    if Light is not None and isinstance(thing, Light):
        bits |= ENT_LIGHT
    return bits


class EntityTable:
    """A dense, disposable projection of a Thing list.

    Rows are addressed by ``slot`` and named by ``properties['id']``.  Build
    one, :meth:`begin_frame` it once per frame, and read its columns.
    """

    __slots__ = ('generation', 'count', 'ids', 'slot_of_id', 'things',
                 'pos', 'class_bits', 'light_slots', 'monster_slots',
                 'pickup_slots', '_epoch', '_hidden_buf')

    def __init__(self):
        self.generation = 0
        self.count = 0
        #: slot -> the entity's stable UUID.  Never indexed in the frame loop.
        self.ids: list = []
        #: UUID -> slot.  Cache-boundary mechanism, not a per-frame lookup.
        self.slot_of_id: dict = {}
        #: slot -> the live entity.  A reference; the object's data still lives
        #: in exactly one place.
        self.things: list = []

        # float64 to match the brush table's centre column, so the two can be
        # compared and combined without a cast.
        self.pos = np.zeros((0, 3), dtype=np.float64)
        self.class_bits = np.zeros((0,), dtype=np.uint16)

        #: Slots of the Lights -- the frame's light list, without a scan.
        self.light_slots = np.empty(0, dtype=np.int32)
        #: Slots of the Monsters, whose published reference is a fresh snapshot
        #: each frame.  The entity half of ``RenderTable.dynamic_slots``.
        self.monster_slots = np.empty(0, dtype=np.int32)
        #: Slots of the Pickups -- the only rows a collected-pickup filter has
        #: to consider, so that filter costs pickups rather than entities.
        self.pickup_slots = np.empty(0, dtype=np.int32)

        self._epoch = None
        self._hidden_buf = np.empty(0, dtype=bool)

    @property
    def center(self):
        """``pos`` under the name :class:`engine.render_table.RenderTable` uses.

        The slot helpers on the renderer -- ``_distance_cull_slots``,
        ``_sort_slots_by_distance`` -- are written against a projection with a
        ``center`` column, and an entity's position *is* its centre.  Exposing
        the same name is what lets one implementation of "narrow these slots to
        a radius" and "depth-order these slots" serve both halves of the world
        rather than growing a second copy.
        """
        return self.pos

    # -- capacity ----------------------------------------------------------

    def _resize(self, n):
        if n <= len(self.pos):
            return
        grown = np.zeros((n, 3), dtype=np.float64)
        if len(self.pos):
            grown[:len(self.pos)] = self.pos
        self.pos = grown
        bits = np.zeros((n,), dtype=np.uint16)
        if len(self.class_bits):
            bits[:len(self.class_bits)] = self.class_bits
        self.class_bits = bits

    # -- synchronisation ---------------------------------------------------

    def needs_reconcile(self, things, epoch=None) -> bool:
        """Whether the next :meth:`begin_frame` will rebuild the row mapping.

        Two O(1) comparisons, so a caller that has to stamp ids before a
        reconcile can ask rather than stamping unconditionally every frame.
        """
        return epoch is None or epoch != self._epoch or len(things) != self.count

    def begin_frame(self, things, epoch=None):
        """Bring the table into line with *things*; return the live hidden mask.

        The whole of the projection's per-frame Python cost: one comprehension
        reading ``pos`` and one reading ``hidden``.  Both are bulk-stored into
        their columns -- assigning a NumPy array element by element from Python
        costs several times as much, and this runs over every entity in the
        level every frame.

        ``hidden`` is the one field that cannot be cached, for the reason the
        brush table gives: Big World parks through it with no notification.
        """
        n = len(things)
        if self.needs_reconcile(things, epoch):
            self._reconcile(things)
            self._epoch = epoch

        if n:
            try:
                rows = [t.pos for t in things]
            except AttributeError:
                # A row standing in as a raw dict rather than a Thing.  The
                # retry keeps the ordinary path a bare attribute read; see the
                # same shape below for `hidden`.
                rows = [_pos_of(t) for t in things]
            # Flattened rather than stored as a list of triples: NumPy's
            # nested-sequence conversion walks and type-checks every inner
            # sequence, and feeding it one flat stream instead is twice as
            # fast over the whole entity list.  Measured, not assumed --
            # 0.218 ms against 0.096 ms at 961 entities.
            self.pos[:n].reshape(-1)[:] = np.fromiter(
                chain.from_iterable(rows), dtype=np.float64, count=n * 3)
            # The publish loop this replaces also normalised a glm vector back
            # onto the entity.  The store above is already correct either way,
            # so this repairs the entity, and only when one is actually
            # present.  `set(map(type, ...))` is a native scan, so the ordinary
            # case pays no Python-level branch per entity.
            if not set(map(type, rows)) <= {list}:
                for i, p in enumerate(rows):
                    if type(p) is not list:
                        things[i].pos = [float(p[0]), float(p[1]), float(p[2])]

        if len(self._hidden_buf) < n:
            self._hidden_buf = np.empty(max(n, 16), dtype=bool)
        hidden = self._hidden_buf[:n]
        if n:
            try:
                hidden[:] = [t.properties.get('hidden', False) for t in things]
            except AttributeError:
                # A row that is not a Thing -- a raw dict standing in for one.
                # Rare enough to be worth the retry rather than a per-entity
                # getattr on the ordinary path, which costs nearly twice as much.
                hidden[:] = [_props_of(t).get('hidden', False) for t in things]
        return hidden

    def sync(self, things, epoch=None) -> bool:
        """Reconcile without reading ``hidden``.  Returns whether it did.

        :meth:`begin_frame` is what the render path calls; this is for callers
        bringing the columns up to date on their own schedule -- tests, and
        anything preparing a pass outside the frame loop.
        """
        before = self.generation
        structural = self.needs_reconcile(things, epoch)
        if not structural:
            # The identity check :meth:`begin_frame` deliberately does not pay
            # for: an entity replaced in place, at the same index, by something
            # that bumped no counter.  Off the frame path it is affordable, and
            # it is what lets a test drive the table without an epoch.
            for i, thing in enumerate(things):
                if self.things[i] is not thing:
                    structural = True
                    break
        if structural:
            self._reconcile(things)
            self._epoch = epoch
        return self.generation != before

    def _reconcile(self, things):
        """Rebuild the row mapping and re-resolve the cold column."""
        n = len(things)
        self._resize(max(n, 16))

        ids = [None] * n
        for slot, thing in enumerate(things):
            props = getattr(thing, 'properties', None)
            ids[slot] = props.get('id') if isinstance(props, dict) else None
            self.class_bits[slot] = _entity_class_bits(thing)
            self.pos[slot] = _pos_of(thing)

        self.ids = ids
        self.slot_of_id = {eid: slot for slot, eid in enumerate(ids)
                           if eid is not None}
        self.things = list(things)
        self.count = n
        bits = self.class_bits[:n]
        self.light_slots = np.flatnonzero(bits & ENT_LIGHT).astype(np.int32)
        self.monster_slots = np.flatnonzero(bits & ENT_MONSTER).astype(np.int32)
        self.pickup_slots = np.flatnonzero(bits & ENT_PICKUP).astype(np.int32)
        self.generation += 1

    def refresh_rows(self, things, slots):
        """Re-resolve the cold column for *slots* after a semantic change."""
        for slot in slots:
            self.class_bits[slot] = _entity_class_bits(things[slot])


_EMPTY: dict = {}


def _props_of(thing) -> dict:
    """The property dict of a Thing-like object or a raw dict, never None."""
    props = getattr(thing, 'properties', None)
    if isinstance(props, dict):
        return props
    return thing if isinstance(thing, dict) else _EMPTY


def _pos_of(thing):
    """The position of a Thing-like object or a raw dict.

    The table is built over the authoritative Thing list, so the ordinary row
    has a ``pos`` attribute.  A caller projecting a *published* list instead --
    where every Monster row is a render-snapshot dict -- gets the same answer
    rather than an AttributeError.
    """
    pos = getattr(thing, 'pos', None)
    if pos is not None:
        return pos
    if isinstance(thing, dict):
        return thing.get('pos') or (0.0, 0.0, 0.0)
    return (0.0, 0.0, 0.0)


def classify_slots(table, slots, hidden, is_play, show_sprites):
    """Split entity slots into the model pass and the sprite pass, numerically.

    The array form of ``_sort_objects``' Thing half.  That loop asked every
    visible entity what it was, once per frame, to reach a verdict that only
    changes when the entity is edited; the projection resolved the inputs into
    :attr:`EntityTable.class_bits`, so the same split is a handful of masks.

    *hidden* is the live flag, indexed by slot -- not gathered for *slots*,
    because the caller holds the whole-table mask :meth:`EntityTable.begin_frame`
    returned.

    The chain being reproduced, in its original order:

    1. a PathNode, or a non-Thing, is skipped;
    2. a Portal, or a monster snapshot, is a sprite;
    3. a visible model (``model_path``, ``render_mode == 'model'``, not hidden)
       goes to the model pass;
    4. otherwise the entity's *kind* decides -- Pickup first, then the
       entity-sprite classes, then billboard-with-a-sprite, then ordinary --
       with a Prop drawn only as a billboard, a model-but-hidden entity drawn
       nowhere, and an ordinary entity drawn only while editing or with
       show-sprites on.

    Returns ``(model_slots, sprite_slots)`` as slot arrays.  No entity is
    materialised here.
    """
    if not len(slots):
        return slots, slots

    bits = table.class_bits[slots]
    is_hidden = np.asarray(hidden)[slots]

    skip = (bits & ENT_SKIP) != 0
    always_sprite = (bits & ENT_ALWAYS_SPRITE) != 0

    has_model = (bits & ENT_HAS_MODEL) != 0
    mode_model = (bits & ENT_MODE_MODEL) != 0
    mode_billboard = (bits & ENT_MODE_BILLBOARD) != 0
    has_sprite = (bits & ENT_HAS_SPRITE) != 0
    pickup = (bits & ENT_PICKUP) != 0
    entity_sprite = (bits & ENT_ENTITY_SPRITE) != 0
    prop = (bits & ENT_PROP) != 0

    # Step 3. `model_visible` in the loop; the only place the live flag is read.
    model = ~skip & ~always_sprite & has_model & mode_model & ~is_hidden

    # Step 4's `kind`, in the order _thing_render_kind tries the classes.
    kind_sprite = ~pickup & ~entity_sprite & mode_billboard & has_sprite

    remainder = ~skip & ~always_sprite & ~model
    # A Prop is a sprite only as a billboard with a sprite, and is never
    # reached by the clauses below -- the loop returns through its own branch.
    prop_sprite = remainder & prop & mode_billboard & kind_sprite
    others = remainder & ~prop
    sprite = (
        prop_sprite
        | (others & (pickup | entity_sprite))
        # `elif model_path and render_mode == 'model'` draws nothing: that is
        # the hidden model, already excluded from `model` above.
        | (others & ~(pickup | entity_sprite) & ~(has_model & mode_model)
           & (kind_sprite | (not is_play or show_sprites)))
    )
    return slots[model], slots[sprite | (~skip & always_sprite)]
