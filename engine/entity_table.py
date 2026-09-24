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

import os
from itertools import chain

import numpy as np

# Defensive, as everywhere else in engine/: editor.things pulls in PyQt5, and
# the standalone player tier does not have it.  A tier without the classes
# classifies every entity as a plain Thing, which is what it is there.
try:
    from editor.things import (Thing, PathNode, Portal, Pickup, Prop, Monster,
                               LogicGate, LogicRelay, LogicTimer, LevelChanger,
                               Light, LogicSpawner, LogicCamera)
except ImportError:                                   # pragma: no cover
    Thing = PathNode = Portal = Pickup = Prop = Monster = None
    LogicGate = LogicRelay = LogicTimer = LevelChanger = Light = None
    LogicSpawner = LogicCamera = None


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
#: This row's *sprite identity* can change without an edit, so it is re-resolved
#: every frame.  Exactly the classes ``update_instance_textures`` re-hashes per
#: frame -- a monster's sprite follows ``dead``/``is_shooting``, a gate's its
#: type, a pickup's its item, a prop's its representation.  Everything else
#: resolves its sprite once, at reconcile.
ENT_SPRITE_WARM     = 1 << 12

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
    (ENT_PORTAL, 'PORTAL'), (ENT_SPRITE_WARM, 'SPRITE_WARM'),
)


def describe(bits) -> str:
    """The set bits of a classification word, for a readable assertion."""
    return '|'.join(name for bit, name in BIT_NAMES if bits & bit) or 'NONE'


# --------------------------------------------------------------------------
# Sprite identity -- the warm half
# --------------------------------------------------------------------------
#
# A sprite's *position* is warm and its *size* is cold, both straightforwardly.
# Its texture is neither: a monster's sprite is chosen from `dead` and
# `is_shooting` every frame, and a logic gate's, a pickup's and a prop's from
# properties the renderer already re-reads every frame to decide whether its
# instance-texture cache is stale.  So sprite identity is resolved per frame for
# those rows and at reconcile for everything else -- the same cold/warm split
# the brush projection makes, drawn in a different place because entities are
# a different kind of thing.  It is deliberately NOT pushed into
# :mod:`engine.render_table`, whose texture column is wholly cold.
#
# What is resolved here is a *name*, never a GL id: a tuple of candidate cache
# keys and the recipe for loading each, interned to a dense integer exactly as
# :meth:`engine.render_table.RenderTable.intern_texture` interns face textures.
# The renderer turns that integer into a GL texture id on its own thread.

#: Interned id meaning "this row draws no sprite" -- a Portal (which the sprite
#: pass has always skipped) or an entity with no sprite at all.
SPRITE_NONE = -1

#: A candidate is ``(cache_key, filename, subfolder, cache)``.  The renderer
#: tries each in order: look ``cache_key`` up in its sprite-texture cache, and
#: if that misses and ``filename`` is set, load it -- storing the result under
#: ``cache_key`` when ``cache`` is true.  The ordered list is how the object
#: path's "instance-texture override, else the class's shared sprite" is said
#: as data rather than as control flow.
_LOOKUP_ONLY = ('', '', False)


def _monster_sprite_candidates(props):
    """The monster branch of ``draw_sprites``, as candidates.

    Monsters reach the renderer as render-snapshot dicts, so this reads the
    same four fields that branch reads and builds the same ``msprite_`` cache
    key -- which is what makes the two paths resolve to one texture rather than
    to two that happen to look alike.
    """
    if props.get('dead'):
        custom, sprite_type = props.get('custom_dead', ''), 'dead'
    elif props.get('is_shooting'):
        custom, sprite_type = props.get('custom_shoot', ''), 'shoot'
    else:
        custom, sprite_type = props.get('custom_idle', ''), 'idle'

    mtype = props.get('monster_type', 'human')
    variant = props.get('variant', '<None>')
    key = 'msprite_%s_%s_%s_%s' % (mtype, variant, sprite_type, custom)

    if custom:
        clean = custom.replace('assets/', '', 1)
        return ((key, os.path.basename(clean), os.path.dirname(clean), True),)

    filename = '%s.png' % sprite_type
    if variant and variant != '<None>':
        # The variant folder first, the base folder as the fallback -- the two
        # load attempts the object path makes, in the order it makes them.
        return ((key, filename, 'sprites/monsters/%s/%s' % (mtype, variant), True),
                (key, filename, 'sprites/monsters/%s' % mtype, True))
    return ((key, filename, 'sprites/monsters/%s' % mtype, True),)


def _split_asset_path(path):
    """``(filename, subfolder)`` for an authored ``assets/``-relative path."""
    rel = str(path).replace('assets/', '', 1)
    return os.path.basename(rel), os.path.dirname(rel)


def sprite_candidates(thing):
    """How this entity's sprite texture is found, as an ordered candidate list.

    Reproduces two chains that between them decide every sprite Fio draws:
    ``QtGameView.update_instance_textures``, which resolves the per-entity
    override, and ``BaseRenderer.draw_sprites``, which falls back to the
    texture shared by everything of that class.  Returns ``None`` for a row the
    sprite pass draws nothing for.
    """
    if isinstance(thing, dict):
        return _monster_sprite_candidates(thing) if 'monster_type' in thing else None
    if Portal is not None and isinstance(thing, Portal):
        return None            # the sprite pass has always skipped Portals
    props = _props_of(thing)
    if Monster is not None and isinstance(thing, Monster):
        return _monster_sprite_candidates(props)

    out = []
    # -- the per-entity override, in update_instance_textures' own order ----
    if LogicGate is not None and isinstance(thing, LogicGate):
        ltype = str(props.get('logic_type', 'and')).lower()
        out.append(('logic_%s' % ltype, 'logic_%s.png' % ltype, 'sprites', True))
    elif Prop is not None and isinstance(thing, Prop):
        if str(props.get('render_mode', 'model')).lower() == 'billboard':
            path = str(props.get('sprite_path', '') or '')
            if path:
                key = 'propsprite__%s' % path.replace('/', '__').replace('.', '_')
                filename, subfolder = _split_asset_path(path)
                out.append((key, filename, subfolder, True))
    elif Pickup is not None and isinstance(thing, Pickup):
        if thing.is_key():
            # Lookup only: update_instance_textures never loads this one, so a
            # key sprite that was never registered falls through to the class
            # texture rather than being loaded here.
            out.append(('key_%s' % thing.get_key_name(),) + _LOOKUP_ONLY[:2]
                       + (False,))
        elif props.get('custom_sprite'):
            custom = str(props.get('custom_sprite'))
            filename = os.path.basename(custom.replace('\\', '/'))
            # Loaded but not cached under a key of its own, as the object path
            # does -- load_texture has its own cache, so this is not a re-read.
            out.append(('', filename, 'sprites', False))
    elif LevelChanger is not None and isinstance(thing, LevelChanger):
        out.append(('LevelChanger',) + _LOOKUP_ONLY[:2] + (False,))
        out.append(('logic_relay',) + _LOOKUP_ONLY[:2] + (False,))

    # -- and then the class's shared sprite, which draw_sprites falls back to -
    class_name = type(thing).__name__
    if LogicSpawner is not None and isinstance(thing, LogicSpawner):
        out.append(('LogicSpawner', 'logic_spawner.png', 'sprites', True))
    elif LogicCamera is not None and isinstance(thing, LogicCamera):
        out.append(('LogicCamera', 'logic_camera.png', 'sprites', True))
    elif Pickup is not None and isinstance(thing, Pickup):
        path = thing.get_sprite_path()
        if path:
            filename, subfolder = _split_asset_path(path)
            out.append((class_name, filename, subfolder, True))
        else:
            out.append((class_name,) + _LOOKUP_ONLY[:2] + (False,))
    elif props.get('sprite_path'):
        filename, subfolder = _split_asset_path(props.get('sprite_path'))
        out.append((class_name, filename, subfolder, True))
    else:
        out.append((class_name,) + _LOOKUP_ONLY[:2] + (False,))
    return tuple(out)


def sprite_state(thing):
    """The mutable inputs a warm row's sprite identity is derived from.

    :func:`sprite_candidates` is not cheap -- it formats cache keys and splits
    asset paths -- and running it every frame for every warm row costs ten
    times what checking whether its inputs moved costs (2.39 ms against
    0.195 ms at 961 warm rows).  Almost every frame the answer is that nothing
    moved.

    So this is the question asked first, and it is deliberately the same set of
    fields ``update_instance_textures`` hashes to decide whether *its* overrides
    are stale: the two were always answering the same question, and the answer
    belongs next to the column it guards.  ``None`` for a row that is not warm.
    """
    if isinstance(thing, dict):
        if 'monster_type' not in thing:
            return None
        return (thing.get('dead'), thing.get('is_shooting'),
                thing.get('monster_type'), thing.get('variant'),
                thing.get('custom_idle'), thing.get('custom_shoot'),
                thing.get('custom_dead'))
    props = _props_of(thing)
    if Monster is not None and isinstance(thing, Monster):
        return (props.get('dead'), props.get('is_shooting'),
                props.get('monster_type'), props.get('variant'),
                props.get('custom_idle'), props.get('custom_shoot'),
                props.get('custom_dead'))
    if Pickup is not None and isinstance(thing, Pickup):
        # get_sprite_path() reads all three, through is_key()/is_gun().
        return (props.get('item_type'), props.get('key_name'),
                props.get('custom_sprite'))
    if LogicGate is not None and isinstance(thing, LogicGate):
        return (props.get('logic_type'),)
    if Prop is not None and isinstance(thing, Prop):
        return (props.get('render_mode'), props.get('sprite_path'))
    return None


def sprite_size(thing):
    """The billboard's world size, in the order ``draw_sprites`` decides it."""
    if isinstance(thing, dict):
        return (float(thing.get('sprite_width', 128)),
                float(thing.get('sprite_height', 128)))
    props = _props_of(thing)
    if Monster is not None and isinstance(thing, Monster):
        return (float(props.get('sprite_width', 128)),
                float(props.get('sprite_height', 128)))
    if Light is not None and isinstance(thing, Light):
        return (16.0, 16.0)
    if props.get('sprite_path'):
        size = props.get('sprite_size', [32.0, 32.0])
        try:
            return (float(size[0]), float(size[1]))
        except (TypeError, ValueError, IndexError):
            return (32.0, 32.0)
    return (32.0, 32.0)


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
    warm_types = tuple(c for c in (Monster, LogicGate, Pickup, Prop)
                       if c is not None)
    if warm_types and isinstance(thing, warm_types):
        bits |= ENT_SPRITE_WARM
    return bits


class EntityTable:
    """A dense, disposable projection of a Thing list.

    Rows are addressed by ``slot`` and named by ``properties['id']``.  Build
    one, :meth:`begin_frame` it once per frame, and read its columns.
    """

    __slots__ = ('generation', 'count', 'ids', 'slot_of_id', 'things',
                 'pos', 'class_bits', 'light_slots', 'monster_slots',
                 'pickup_slots', 'sprite_size', 'sprite_key_id',
                 'warm_sprite_slots', '_sprite_ids', '_sprite_recipes',
                 '_sprite_state', '_epoch', '_hidden_buf')

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

        #: The billboard's world size.  Cold: it comes from authored properties.
        self.sprite_size = np.zeros((0, 2), dtype=np.float32)
        #: Interned sprite identity, :data:`SPRITE_NONE` for a row that draws
        #: none.  Cold for most rows and re-resolved every frame for
        #: :attr:`warm_sprite_slots`; see the module's sprite-identity section.
        self.sprite_key_id = np.full((0,), SPRITE_NONE, dtype=np.int32)
        #: Rows whose sprite identity is re-resolved per frame.
        self.warm_sprite_slots = np.empty(0, dtype=np.int32)

        # Candidate-list intern table.  GL-free, like the brush table's texture
        # names: these are ids for *recipes*, and the renderer maps them to GL
        # texture ids once per unique recipe on the thread that has a context.
        self._sprite_ids: dict = {}
        self._sprite_recipes: list = []
        #: slot -> the state tuple its sprite identity was last resolved from.
        #: A plain list: it is compared per warm row per frame and never
        #: indexed numerically.
        self._sprite_state: list = []

        self._epoch = None
        self._hidden_buf = np.empty(0, dtype=bool)

    # -- sprite identity interning ----------------------------------------

    def intern_sprite(self, candidates) -> int:
        """The dense id for a candidate list, assigning one on first sight."""
        if not candidates:
            return SPRITE_NONE
        sid = self._sprite_ids.get(candidates)
        if sid is None:
            sid = len(self._sprite_recipes)
            self._sprite_ids[candidates] = sid
            self._sprite_recipes.append(candidates)
        return sid

    def sprite_recipes(self) -> list:
        """Interned candidate lists, indexed by id.

        The renderer walks this once per new recipe to build its ``sprite id ->
        GL texture id`` array.  Tens of entries in a level, not thousands, and
        never touched per sprite.
        """
        return self._sprite_recipes

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
        size = np.zeros((n, 2), dtype=np.float32)
        if len(self.sprite_size):
            size[:len(self.sprite_size)] = self.sprite_size
        self.sprite_size = size
        keys = np.full((n,), SPRITE_NONE, dtype=np.int32)
        if len(self.sprite_key_id):
            keys[:len(self.sprite_key_id)] = self.sprite_key_id
        self.sprite_key_id = keys

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

        # The warm half of sprite identity: a monster's frame, a gate's type,
        # a pickup's item, a prop's representation.  Bounded by the rows that
        # can actually change -- and within those, by the rows that actually
        # did, because re-deriving the recipe is ten times the cost of asking
        # whether its inputs moved.
        state_cache = self._sprite_state
        for slot in self.warm_sprite_slots:
            slot = int(slot)
            thing = things[slot]
            state = sprite_state(thing)
            if state == state_cache[slot]:
                continue
            state_cache[slot] = state
            self.sprite_key_id[slot] = self.intern_sprite(
                sprite_candidates(thing))

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
        states = [None] * n
        for slot, thing in enumerate(things):
            props = getattr(thing, 'properties', None)
            ids[slot] = props.get('id') if isinstance(props, dict) else None
            self.class_bits[slot] = _entity_class_bits(thing)
            self.pos[slot] = _pos_of(thing)
            self.sprite_size[slot] = sprite_size(thing)
            self.sprite_key_id[slot] = self.intern_sprite(
                sprite_candidates(thing))
            states[slot] = sprite_state(thing)

        self._sprite_state = states
        self.ids = ids
        self.slot_of_id = {eid: slot for slot, eid in enumerate(ids)
                           if eid is not None}
        self.things = list(things)
        self.count = n
        bits = self.class_bits[:n]
        self.light_slots = np.flatnonzero(bits & ENT_LIGHT).astype(np.int32)
        self.monster_slots = np.flatnonzero(bits & ENT_MONSTER).astype(np.int32)
        self.pickup_slots = np.flatnonzero(bits & ENT_PICKUP).astype(np.int32)
        self.warm_sprite_slots = np.flatnonzero(
            bits & ENT_SPRITE_WARM).astype(np.int32)
        self.generation += 1

    def refresh_rows(self, things, slots):
        """Re-resolve the cold columns for *slots* after a semantic change."""
        for slot in slots:
            slot = int(slot)
            thing = things[slot]
            self.class_bits[slot] = _entity_class_bits(thing)
            self.sprite_size[slot] = sprite_size(thing)
            self.sprite_key_id[slot] = self.intern_sprite(
                sprite_candidates(thing))
            self._sprite_state[slot] = sprite_state(thing)


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
