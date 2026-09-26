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
import time
from itertools import chain

import glm
import numpy as np

from .portal_transform import basis_from_rotation

# Defensive, as everywhere else in engine/: editor.things pulls in PyQt5, and
# the standalone player tier does not have it.  A tier without the classes
# classifies every entity as a plain Thing, which is what it is there.
try:
    from editor.things import (Thing, PathNode, Portal, Pickup, Prop, Monster,
                               LogicGate, LogicRelay, LogicTimer, LevelChanger,
                               Light, LogicSpawner, LogicCamera, Effect)
except ImportError:                                   # pragma: no cover
    Thing = PathNode = Portal = Pickup = Prop = Monster = Effect = None
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
#: Procedural Effect primitive; FIRE and EXPLOSION share one render path.
ENT_EFFECT          = 1 << 13
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

# Numeric portal direction codes.  3 means both directions.
PORTAL_DIRECTION_FORWARD = 1
PORTAL_DIRECTION_REVERSE = 2
PORTAL_DIRECTION_BOTH = 3

#: Indexable for debug text and test failure messages.
BIT_NAMES = (
    (ENT_SKIP, 'SKIP'), (ENT_ALWAYS_SPRITE, 'ALWAYS_SPRITE'),
    (ENT_HAS_MODEL, 'HAS_MODEL'), (ENT_MODE_MODEL, 'MODE_MODEL'),
    (ENT_MODE_BILLBOARD, 'MODE_BILLBOARD'), (ENT_HAS_SPRITE, 'HAS_SPRITE'),
    (ENT_PICKUP, 'PICKUP'), (ENT_ENTITY_SPRITE, 'ENTITY_SPRITE'),
    (ENT_PROP, 'PROP'), (ENT_LIGHT, 'LIGHT'), (ENT_MONSTER, 'MONSTER'),
    (ENT_PORTAL, 'PORTAL'), (ENT_EFFECT, 'EFFECT'),
    (ENT_SPRITE_WARM, 'SPRITE_WARM'),
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
    """The monster sprite branch, expressed as numeric texture candidates.

    Monsters reach the renderer as render-snapshot dicts, so this reads the
    same four state fields that branch reads and builds the same ``msprite_``
    cache key.  The recipe also carries the fallback behaviour of
    ``Monster.get_sprite_path()``: a missing dead/shoot frame falls back to
    idle rather than making the monster disappear.  The renderer tries the
    candidates in order and caches the first one that actually loads, so this
    stays GL-free and does not add per-frame filesystem checks.
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

    candidates = []
    if custom:
        clean = custom.replace('assets/', '', 1)
        candidates.append(
            (key, os.path.basename(clean), os.path.dirname(clean), True))

    filename = '%s.png' % sprite_type
    base_folder = 'sprites/monsters/%s' % mtype
    if variant and variant != '<None>':
        variant_folder = '%s/%s' % (base_folder, variant)
        # Match Monster.get_sprite_path(): variant first, then base.
        candidates.append((key, filename, variant_folder, True))
        candidates.append((key, filename, base_folder, True))
        # get_sprite_path() falls back to the chosen idle frame when the
        # requested dead/shoot frame does not exist.
        if sprite_type != 'idle':
            candidates.append((key, 'idle.png', variant_folder, True))
            candidates.append((key, 'idle.png', base_folder, True))
    else:
        candidates.append((key, filename, base_folder, True))
        if sprite_type != 'idle':
            candidates.append((key, 'idle.png', base_folder, True))

    return tuple(candidates)



def _split_asset_path(path):
    """Return ``(filename, subfolder)`` for an authored ``assets/`` path."""
    rel = str(path).replace('assets/', '', 1)
    return os.path.basename(rel), os.path.dirname(rel)

def sprite_candidates(thing):
    """How this entity's sprite texture is found, as an ordered candidate list.

    Reproduces two chains that between them decide every sprite Fio draws:
    the dense sprite projection, which resolves the per-entity override
    and class texture recipe before the renderer reaches OpenGL.  Returns ``None`` for a row the
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


def _model_recipe(thing):
    """Return the cold model draw recipe for one entity, or ``None``."""
    props = _props_of(thing)
    model_path = props.get('model_path')
    if not model_path:
        return None
    model_path = str(model_path)
    manual_texture = props.get('texture')
    if not manual_texture:
        return (model_path, None, None)
    colour = props.get('color', [0.8, 0.8, 0.8])
    try:
        colour = (float(colour[0]), float(colour[1]), float(colour[2]))
    except (TypeError, ValueError, IndexError):
        colour = (0.8, 0.8, 0.8)
    return (model_path, str(manual_texture), colour)


def _model_transform_columns(thing):
    """Resolve a model's cold rotation/scale matrices with zero translation."""
    props = _props_of(thing)
    rot = props.get('rotation', [0.0, 0.0, 0.0])
    scale = props.get('scale', 1.0)
    scale_vec = (scale, scale, scale) if isinstance(scale, (int, float)) else scale
    try:
        mat = glm.rotate(glm.mat4(1.0), glm.radians(float(rot[1])), glm.vec3(0, 1, 0))
        mat = glm.rotate(mat, glm.radians(float(rot[0])), glm.vec3(1, 0, 0))
        mat = glm.rotate(mat, glm.radians(float(rot[2])), glm.vec3(0, 0, 1))
        mat = glm.scale(mat, glm.vec3(*scale_vec))
        normal = glm.transpose(glm.inverse(glm.mat3(mat)))
    except Exception:
        mat = glm.mat4(1.0)
        normal = glm.mat3(1.0)
    model = np.array([
        mat[0][0], mat[0][1], mat[0][2], 0.0,
        mat[1][0], mat[1][1], mat[1][2], 0.0,
        mat[2][0], mat[2][1], mat[2][2], 0.0,
        mat[3][0], mat[3][1], mat[3][2], 1.0,
    ], dtype=np.float32)
    normal_np = np.array([
        normal[0][0], normal[0][1], normal[0][2], 0.0,
        normal[1][0], normal[1][1], normal[1][2], 0.0,
        normal[2][0], normal[2][1], normal[2][2], 0.0,
    ], dtype=np.float32)
    return model, normal_np


def _light_props(thing):
    props = getattr(thing, 'properties', thing if isinstance(thing, dict) else {})
    return props if isinstance(props, dict) else {}


def _light_float(thing, key, default):
    try:
        return float(_light_props(thing).get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _light_bool(thing, key, default=False):
    value = _light_props(thing).get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def _light_color_of(thing):
    value = _light_props(thing).get('colour', [255, 255, 255])
    try:
        rgb = np.asarray(value[:3], dtype=np.float32)
        if rgb.size != 3:
            raise ValueError
        return np.clip(rgb / 255.0, 0.0, 1.0)
    except (TypeError, ValueError, IndexError):
        return np.asarray((1.0, 1.0, 1.0), dtype=np.float32)


def _effect_float(props, key, default):
    try:
        return float(props.get(key, default))
    except (TypeError, ValueError):
        return float(default)


def _effect_colour(props, key, default):
    value = props.get(key, default)
    try:
        rgb = np.asarray(value[:3], dtype=np.float32)
        if rgb.size != 3:
            raise ValueError
        return np.clip(rgb / 255.0, 0.0, 1.0)
    except (TypeError, ValueError, IndexError):
        return np.asarray(default, dtype=np.float32) / 255.0


def _effect_flicker(seed, elapsed):
    """Deterministic scalar noise shared conceptually with the Effect shader."""
    phase = np.asarray(elapsed, dtype=np.float32) * 10.0 + np.asarray(seed, dtype=np.float32) * 0.013
    cell = np.floor(phase)
    frac = phase - cell
    smooth = frac * frac * (3.0 - 2.0 * frac)
    a = np.mod(np.sin((cell + seed) * 12.9898) * 43758.5453123, 1.0)
    b = np.mod(np.sin((cell + 1.0 + seed) * 12.9898) * 43758.5453123, 1.0)
    return a * (1.0 - smooth) + b * smooth


class EntityTable:
    """A dense, disposable projection of a Thing list.

    Rows are addressed by ``slot`` and named by ``properties['id']``.  Build
    one, :meth:`begin_frame` it once per frame, and read its columns.
    """

    __slots__ = ('generation', 'count', 'ids', 'slot_of_id', 'things',
                 'pos', 'class_bits', 'light_slots', 'light_color',
                 'light_params', 'light_enabled', 'light_casts_shadows',
                 'portal_slots', 'portal_target_slot', 'portal_active',
                 'portal_direction', 'portal_width_height', 'portal_basis',
                 'portal_fade', 'portal_color', 'portal_show_rim',
                 'monster_slots', 'pickup_slots', 'effect_slots',
                 'effect_type', 'effect_params', 'effect_color',
                 'effect_light_color', 'effect_lifetime', 'effect_seed',
                 'effect_spawn_time', 'effect_elapsed', 'effect_alive',
                 'sprite_size', 'sprite_key_id',
                 'model_recipe_id', 'model_base_matrix', 'model_normal_matrix',
                 '_sprite_ids', '_sprite_recipes', '_model_ids', '_model_recipes',
                 '_epoch', '_hidden_buf')

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
        #: Dense portal topology/render columns. Target links are resolved to
        #: integer entity slots at reconcile; transforms are refreshed only for
        #: portal rows so moving/parented portals stay numeric in the renderer.
        self.portal_slots = np.empty(0, dtype=np.int32)
        self.portal_target_slot = np.full((0,), -1, dtype=np.int32)
        self.portal_active = np.zeros((0,), dtype=bool)
        self.portal_direction = np.zeros((0,), dtype=np.uint8)
        self.portal_width_height = np.zeros((0, 2), dtype=np.float32)
        self.portal_basis = np.zeros((0, 3, 3), dtype=np.float64)
        self.portal_fade = np.zeros((0,), dtype=np.float32)
        self.portal_color = np.ones((0, 3), dtype=np.float32)
        self.portal_show_rim = np.zeros((0,), dtype=bool)

        #: Slots of the Lights -- the frame's light list, without a scan.
        self.light_slots = np.empty(0, dtype=np.int32)
        #: Per-light GL colour, normalized to 0..1. Warm: I/O may change it.
        self.light_color = np.zeros((0, 3), dtype=np.float32)
        #: Per-light (intensity, radius). Warm because gameplay can mutate both.
        self.light_params = np.zeros((0, 2), dtype=np.float32)
        #: Live on/off state, kept numeric so GL never needs the Light object.
        self.light_enabled = np.zeros((0,), dtype=bool)
        #: Shadow participation, normalized from bool/string authored state.
        self.light_casts_shadows = np.zeros((0,), dtype=bool)
        #: Slots of the Monsters, whose published reference is a fresh snapshot
        #: each frame.  The entity half of ``RenderTable.dynamic_slots``.
        self.monster_slots = np.empty(0, dtype=np.int32)
        #: Slots of the Pickups -- the only rows a collected-pickup filter has
        #: to consider, so that filter costs pickups rather than entities.
        self.pickup_slots = np.empty(0, dtype=np.int32)

        #: Dense procedural Effect state. Authored data is cold; elapsed/alive
        #: are runtime columns and the renderer never touches Effect objects.
        self.effect_slots = np.empty(0, dtype=np.int32)
        self.effect_type = np.zeros((0,), dtype=np.uint8)
        self.effect_params = np.zeros((0, 4), dtype=np.float32)
        self.effect_color = np.ones((0, 3), dtype=np.float32)
        self.effect_light_color = np.ones((0, 3), dtype=np.float32)
        self.effect_lifetime = np.full((0,), 0.5, dtype=np.float32)
        self.effect_seed = np.ones((0,), dtype=np.float32)
        self.effect_spawn_time = np.zeros((0,), dtype=np.float64)
        self.effect_elapsed = np.zeros((0,), dtype=np.float32)
        self.effect_alive = np.zeros((0,), dtype=bool)

        #: The billboard's world size. Cold: it comes from authored properties.
        self.sprite_size = np.zeros((0, 2), dtype=np.float32)
        #: Dense sprite recipe id per entity slot.  -1 means no sprite.
        self.sprite_key_id = np.full((0,), SPRITE_NONE, dtype=np.int32)
        #: Dense model recipe id per entity slot.  -1 means no model.
        self.model_recipe_id = np.full((0,), -1, dtype=np.int32)
        #: Cold model transform, flattened as a mat4 per entity slot.
        self.model_base_matrix = np.zeros((0, 16), dtype=np.float32)
        #: Cold normal transform, flattened as a 3x3 matrix padded to 12 floats.
        self.model_normal_matrix = np.zeros((0, 12), dtype=np.float32)
        #: Interned sprite identity, :data:`SPRITE_NONE` for a row that draws
        #: none. Authored sprite identity is cold; Monster snapshots update it
        #: directly when the logic thread publishes them.
        # Candidate-list intern table.  GL-free, like the brush table's texture
        # names: these are ids for *recipes*, and the renderer maps them to GL
        # texture ids once per unique recipe on the thread that has a context.
        self._sprite_ids: dict = {}
        self._sprite_recipes: list = []
        #: slot -> the state tuple its sprite identity was last resolved from.
        #: A plain list: it is compared per warm row per frame and never
        #: indexed numerically.
        self._model_ids = {}
        self._model_recipes = []

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
        """Interned candidate lists, indexed by id."""
        return self._sprite_recipes

    def intern_model_recipe(self, recipe) -> int:
        if recipe is None:
            return -1
        mid = self._model_ids.get(recipe)
        if mid is None:
            mid = len(self._model_recipes)
            self._model_ids[recipe] = mid
            self._model_recipes.append(recipe)
        return mid

    def model_recipes(self) -> list:
        """Interned model recipes, indexed by dense entity column id."""
        return self._model_recipes

    def update_monster_snapshot(self, slot, snapshot):
        """Publish a Monster render snapshot into numeric sprite columns."""
        self.sprite_size[slot] = sprite_size(snapshot)
        self.sprite_key_id[slot] = self.intern_sprite(sprite_candidates(snapshot))

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
        grown = max(16, len(self.pos) * 2, n)

        pos = np.zeros((grown, 3), dtype=np.float64)
        if len(self.pos):
            pos[:len(self.pos)] = self.pos
        self.pos = pos

        bits = np.zeros((grown,), dtype=np.uint16)
        if len(self.class_bits):
            bits[:len(self.class_bits)] = self.class_bits
        self.class_bits = bits

        light_color = np.zeros((grown, 3), dtype=np.float32)
        if len(self.light_color):
            light_color[:len(self.light_color)] = self.light_color
        self.light_color = light_color
        light_params = np.zeros((grown, 2), dtype=np.float32)
        if len(self.light_params):
            light_params[:len(self.light_params)] = self.light_params
        self.light_params = light_params
        light_enabled = np.zeros((grown,), dtype=bool)
        if len(self.light_enabled):
            light_enabled[:len(self.light_enabled)] = self.light_enabled
        self.light_enabled = light_enabled
        light_casts = np.zeros((grown,), dtype=bool)
        if len(self.light_casts_shadows):
            light_casts[:len(self.light_casts_shadows)] = self.light_casts_shadows
        self.light_casts_shadows = light_casts
        effect_type = np.zeros((grown,), dtype=np.uint8)
        if len(self.effect_type):
            effect_type[:len(self.effect_type)] = self.effect_type
        self.effect_type = effect_type

        effect_params = np.zeros((grown, 4), dtype=np.float32)
        if len(self.effect_params):
            effect_params[:len(self.effect_params)] = self.effect_params
        self.effect_params = effect_params

        effect_color = np.ones((grown, 3), dtype=np.float32)
        if len(self.effect_color):
            effect_color[:len(self.effect_color)] = self.effect_color
        self.effect_color = effect_color

        effect_light_color = np.ones((grown, 3), dtype=np.float32)
        if len(self.effect_light_color):
            effect_light_color[:len(self.effect_light_color)] = self.effect_light_color
        self.effect_light_color = effect_light_color

        effect_lifetime = np.full((grown,), 0.5, dtype=np.float32)
        if len(self.effect_lifetime):
            effect_lifetime[:len(self.effect_lifetime)] = self.effect_lifetime
        self.effect_lifetime = effect_lifetime

        effect_seed = np.ones((grown,), dtype=np.float32)
        if len(self.effect_seed):
            effect_seed[:len(self.effect_seed)] = self.effect_seed
        self.effect_seed = effect_seed

        effect_spawn = np.zeros((grown,), dtype=np.float64)
        if len(self.effect_spawn_time):
            effect_spawn[:len(self.effect_spawn_time)] = self.effect_spawn_time
        self.effect_spawn_time = effect_spawn

        effect_elapsed = np.zeros((grown,), dtype=np.float32)
        if len(self.effect_elapsed):
            effect_elapsed[:len(self.effect_elapsed)] = self.effect_elapsed
        self.effect_elapsed = effect_elapsed

        effect_alive = np.zeros((grown,), dtype=bool)
        if len(self.effect_alive):
            effect_alive[:len(self.effect_alive)] = self.effect_alive
        self.effect_alive = effect_alive

        size = np.zeros((grown, 2), dtype=np.float32)
        if len(self.sprite_size):
            size[:len(self.sprite_size)] = self.sprite_size
        self.sprite_size = size

        keys = np.full((grown,), SPRITE_NONE, dtype=np.int32)
        if len(self.sprite_key_id):
            keys[:len(self.sprite_key_id)] = self.sprite_key_id
        self.sprite_key_id = keys

        model_ids = np.full((grown,), -1, dtype=np.int32)
        if len(self.model_recipe_id):
            model_ids[:len(self.model_recipe_id)] = self.model_recipe_id
        self.model_recipe_id = model_ids

        base = np.zeros((grown, 16), dtype=np.float32)
        if len(self.model_base_matrix):
            base[:len(self.model_base_matrix)] = self.model_base_matrix
        self.model_base_matrix = base

        normal = np.zeros((grown, 12), dtype=np.float32)
        if len(self.model_normal_matrix):
            normal[:len(self.model_normal_matrix)] = self.model_normal_matrix
        self.model_normal_matrix = normal

        ptarget = np.full((grown,), -1, dtype=np.int32)
        if len(self.portal_target_slot):
            ptarget[:len(self.portal_target_slot)] = self.portal_target_slot
        self.portal_target_slot = ptarget

        pactive = np.zeros((grown,), dtype=bool)
        if len(self.portal_active):
            pactive[:len(self.portal_active)] = self.portal_active
        self.portal_active = pactive

        pdirection = np.zeros((grown,), dtype=np.uint8)
        if len(self.portal_direction):
            pdirection[:len(self.portal_direction)] = self.portal_direction
        self.portal_direction = pdirection

        psize = np.zeros((grown, 2), dtype=np.float32)
        if len(self.portal_width_height):
            psize[:len(self.portal_width_height)] = self.portal_width_height
        self.portal_width_height = psize

        pbasis = np.zeros((grown, 3, 3), dtype=np.float64)
        if len(self.portal_basis):
            pbasis[:len(self.portal_basis)] = self.portal_basis
        self.portal_basis = pbasis

        pfade = np.zeros((grown,), dtype=np.float32)
        if len(self.portal_fade):
            pfade[:len(self.portal_fade)] = self.portal_fade
        self.portal_fade = pfade

        pcolor = np.ones((grown, 3), dtype=np.float32)
        if len(self.portal_color):
            pcolor[:len(self.portal_color)] = self.portal_color
        self.portal_color = pcolor

        prim = np.zeros((grown,), dtype=bool)
        if len(self.portal_show_rim):
            prim[:len(self.portal_show_rim)] = self.portal_show_rim
        self.portal_show_rim = prim

    # -- synchronisation ---------------------------------------------------

    def needs_reconcile(self, things, epoch=None) -> bool:
        """Whether the next :meth:`begin_frame` will rebuild the row mapping.

        Two O(1) comparisons, so a caller that has to stamp ids before a
        reconcile can ask rather than stamping unconditionally every frame.
        """
        return epoch is None or epoch != self._epoch or len(things) != self.count

    def begin_frame(self, things, epoch=None, dirty_objects=None,
                    effect_runtime=False):
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
            self._reconcile(things, dirty_objects=dirty_objects)
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

        # Authored sprite identity is cold. Dynamic Monster sprite identity is
        # published from the existing snapshot path, avoiding a second object walk.
        # Portal state is the other small live entity family; keep it numeric so
        # secondary render views never need Portal objects.
        self._refresh_portal_live(things)

        # Light state is render state, not renderer metadata. Ordinary Lights
        # retain their existing warm object-backed state; Effect rows stay fully
        # numeric and derive animated light from the dense effect columns.
        if len(self.light_slots):
            ls = self.light_slots
            effect_mask = (self.class_bits[ls] & ENT_EFFECT) != 0
            normal_ls = ls[~effect_mask]
            effect_ls = ls[effect_mask]

            if len(normal_ls):
                light_rows = [things[int(i)] for i in normal_ls]
                self.light_color[normal_ls] = np.asarray(
                    [_light_color_of(t) for t in light_rows], dtype=np.float32)
                self.light_params[normal_ls] = np.asarray(
                    [[_light_float(t, 'intensity', 1.0),
                      _light_float(t, 'radius', 512.0)] for t in light_rows],
                    dtype=np.float32)
                self.light_enabled[normal_ls] = np.asarray(
                    [_light_bool(t, 'state', True) for t in light_rows], dtype=bool)
                self.light_casts_shadows[normal_ls] = np.asarray(
                    [_light_bool(t, 'casts_shadows', False) for t in light_rows],
                    dtype=bool)

            if len(effect_ls):
                now = float(time.perf_counter())
                unset = self.effect_spawn_time[effect_ls] <= 0.0
                if effect_runtime and np.any(unset):
                    self.effect_spawn_time[effect_ls[unset]] = now

                explosion = self.effect_type[effect_ls] == 1
                if effect_runtime:
                    elapsed = np.maximum(
                        now - self.effect_spawn_time[effect_ls], 0.0
                    ).astype(np.float32, copy=False)
                else:
                    # FIRE animates in the editor; EXPLOSION previews at t=0
                    # instead of consuming its lifetime before Play.
                    elapsed = np.where(
                        explosion, 0.0, max(now, 0.0)
                    ).astype(np.float32, copy=False)

                self.effect_elapsed[effect_ls] = elapsed
                lifetime = np.maximum(self.effect_lifetime[effect_ls], 0.01)
                t = np.clip(elapsed / lifetime, 0.0, 1.0)
                alive = (~explosion) | (elapsed < lifetime)
                if not effect_runtime:
                    alive[:] = True
                self.effect_alive[effect_ls] = alive

                flicker = _effect_flicker(
                    self.effect_seed[effect_ls], elapsed
                ).astype(np.float32, copy=False)
                base_intensity = self.effect_params[effect_ls, 2]
                base_radius = self.effect_params[effect_ls, 3]
                self.light_color[effect_ls] = self.effect_light_color[effect_ls]
                self.light_enabled[effect_ls] = alive & (base_intensity > 0.0)

                intensity = base_intensity * (0.80 + 0.20 * flicker)
                radius = base_radius.copy()
                if np.any(explosion):
                    burst = np.exp(-t * t * 48.0)
                    envelope = 1.0 - np.clip(
                        (t - 0.40) / 0.60, 0.0, 1.0
                    )
                    intensity *= (1.0 + burst * 2.2) * envelope
                    radius *= 1.0 + burst * 1.4 + t * 0.5
                self.light_params[effect_ls, 0] = intensity
                self.light_params[effect_ls, 1] = np.maximum(radius, 0.01)

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

    def sync(self, things, epoch=None, dirty_objects=None) -> bool:
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
            self._reconcile(things, dirty_objects=dirty_objects)
            self._epoch = epoch
        return self.generation != before

    def _reconcile(self, things, dirty_objects=None):
        """Rebuild slot mapping while preserving untouched cold entity rows."""
        n = len(things)
        self._resize(max(n, 16))

        old_slot_of_id = self.slot_of_id
        old_things = self.things
        old_count = len(old_things)
        ids = [None] * n
        survivors = set()
        move_src, move_dst = [], []

        for slot, thing in enumerate(things):
            eid = _props_of(thing).get('id')
            ids[slot] = eid
            if eid is None:
                continue
            if dirty_objects is None or id(thing) in dirty_objects:
                continue
            old = old_slot_of_id.get(eid)
            if old is None or old >= old_count or old_things[old] is not thing:
                continue
            survivors.add(slot)
            if old != slot:
                move_src.append(old)
                move_dst.append(slot)

        if move_src:
            src = np.asarray(move_src, dtype=np.intp)
            dst = np.asarray(move_dst, dtype=np.intp)
            for arr in (self.class_bits, self.light_color, self.light_params,
                        self.light_enabled, self.light_casts_shadows,
                        self.sprite_size, self.sprite_key_id,
                        self.model_recipe_id, self.model_base_matrix,
                        self.model_normal_matrix, self.effect_type,
                        self.effect_params, self.effect_color,
                        self.effect_light_color, self.effect_lifetime,
                        self.effect_seed, self.effect_spawn_time,
                        self.effect_elapsed, self.effect_alive,
                        self.portal_target_slot,
                        self.portal_active, self.portal_direction,
                        self.portal_width_height, self.portal_basis,
                        self.portal_fade, self.portal_color,
                        self.portal_show_rim):
                arr[dst] = arr[src]

        for slot, thing in enumerate(things):
            self.pos[slot] = _pos_of(thing)
            if slot not in survivors:
                self._resolve_entity_cold(slot, thing)

        self.ids = ids
        self.slot_of_id = {eid: slot for slot, eid in enumerate(ids)
                           if eid is not None}
        self.things = list(things)
        self.count = n
        bits = self.class_bits[:n]
        self.effect_slots = np.flatnonzero(
            bits & ENT_EFFECT
        ).astype(np.int32)
        self.light_slots = np.flatnonzero(
            bits & (ENT_LIGHT | ENT_EFFECT)
        ).astype(np.int32)
        self.portal_slots = np.flatnonzero(bits & ENT_PORTAL).astype(np.int32)
        self.monster_slots = np.flatnonzero(bits & ENT_MONSTER).astype(np.int32)
        self.pickup_slots = np.flatnonzero(bits & ENT_PICKUP).astype(np.int32)
        self._resolve_portal_links(things)
        self.generation += 1


    def _resolve_portal_links(self, things):
        """Resolve authored portal names to integer entity slots."""
        self.portal_target_slot[:self.count] = -1
        if not len(self.portal_slots):
            return
        name_to_slot = {
            _props_of(thing).get('name'): slot
            for slot, thing in enumerate(things)
            if _props_of(thing).get('name')
        }
        for slot_value in self.portal_slots:
            slot = int(slot_value)
            target = _props_of(things[slot]).get('portal_target', '')
            target_slot = name_to_slot.get(target, -1)
            if (target_slot >= 0
                    and (self.class_bits[target_slot] & ENT_PORTAL)):
                self.portal_target_slot[slot] = int(target_slot)

    @staticmethod
    def _portal_direction_code(value):
        value = str(value or 'both').strip().lower()
        if value == 'forward':
            return PORTAL_DIRECTION_FORWARD
        if value == 'reverse':
            return PORTAL_DIRECTION_REVERSE
        if value == 'both':
            return PORTAL_DIRECTION_BOTH
        return 0

    def _refresh_portal_live(self, things):
        """Refresh only runtime-mutated portal state."""
        if not len(self.portal_slots):
            return
        for slot_value in self.portal_slots:
            slot = int(slot_value)
            thing = things[slot]
            props = _props_of(thing)
            self.portal_active[slot] = _bool_property(
                props.get('active', True), True)
            self.portal_fade[slot] = float(
                max(0.0, min(1.0, _float_property(
                    getattr(thing, '_fade_alpha', 1.0), 1.0))))
            # Parent movers can change a portal's world orientation at runtime.
            # Authored dimensions, direction, rim and colour remain cold.
            self.portal_basis[slot] = np.asarray(
                basis_from_rotation(props.get(
                    'rotation', [props.get('angle', 0.0), 0.0, 0.0])),
                dtype=np.float64)

    def _resolve_entity_cold(self, slot, thing):
        """Resolve authored render state for one entity row."""
        self.class_bits[slot] = _entity_class_bits(thing)

        # Sprite identity/size is cold for authored entities.  This must be
        # resolved at the same edit boundary as class_bits/model_recipe_id:
        # switching a Prop model -> billboard changes the render class and the
        # sprite recipe without changing the entity row itself.
        self.sprite_size[slot] = sprite_size(thing)
        self.sprite_key_id[slot] = self.intern_sprite(sprite_candidates(thing))

        # Model rendering is part of the dense entity projection too. The
        # classifier already sends Model-mode entities here, so their cold
        # recipe and transform columns must be populated at the same cache
        # boundary. Leaving model_recipe_id at its sentinel value (-1) makes
        # draw_models_instanced silently skip an otherwise valid model slot.
        recipe_id = self.intern_model_recipe(_model_recipe(thing))
        self.model_recipe_id[slot] = recipe_id
        if recipe_id >= 0:
            model, normal = _model_transform_columns(thing)
            self.model_base_matrix[slot] = model
            self.model_normal_matrix[slot] = normal
        else:
            # Clear stale state when an edited entity loses its model_path.
            self.model_base_matrix[slot].fill(0.0)
            self.model_base_matrix[slot, 15] = 1.0
            self.model_normal_matrix[slot].fill(0.0)
            self.model_normal_matrix[slot, 0] = 1.0
            self.model_normal_matrix[slot, 5] = 1.0
            self.model_normal_matrix[slot, 10] = 1.0

        if self.class_bits[slot] & ENT_PORTAL:
            props = _props_of(thing)
            self.portal_direction[slot] = self._portal_direction_code(
                props.get('portal_direction', 'both'))
            self.portal_width_height[slot] = (
                max(16.0, _float_property(props.get('width', 128.0), 128.0)),
                max(16.0, _float_property(props.get('height', 256.0), 256.0)),
            )
            colour = props.get('color', [255, 255, 255])
            try:
                rgb = np.asarray(colour[:3], dtype=np.float32) / 255.0
                if rgb.size != 3:
                    raise ValueError
                self.portal_color[slot] = np.clip(rgb, 0.0, 1.0)
            except (TypeError, ValueError, IndexError):
                self.portal_color[slot] = 1.0
            self.portal_show_rim[slot] = _bool_property(
                props.get('show_rim', True), True)
            self.portal_basis[slot] = np.asarray(
                basis_from_rotation(props.get(
                    'rotation', [props.get('angle', 0.0), 0.0, 0.0])),
                dtype=np.float64)

    def refresh_rows(self, things, slots):
        """Re-resolve cold render columns for *slots* after an editor change."""
        for slot in slots:
            slot = int(slot)
            self._resolve_entity_cold(slot, things[slot])


_EMPTY: dict = {}


def _props_of(thing) -> dict:
    """The property dict of a Thing-like object or a raw dict, never None."""
    props = getattr(thing, 'properties', None)
    if isinstance(props, dict):
        return props
    return thing if isinstance(thing, dict) else _EMPTY


def _bool_property(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, str):
        return value.strip().lower() not in ('false', '0', 'no')
    return bool(value)


def _float_property(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


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
