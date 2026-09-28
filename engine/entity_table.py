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
Prop has been collected -- is dynamic for brushes too, and the brush table
already has the discipline for it.  So this module applies the same one.

What it is, and is not
----------------------
Identical in kind to :class:`engine.render_table.RenderTable`, deliberately:

* it stores nothing authored.  Every column resolves something the entity
  already says, so rebuilding the table from ``EditorState.things`` gives
  identical bits;
* it is never written back to.  (A ``glm`` vector assigned to ``pos`` is
  stored as a list by the setter, where it is assigned, not repaired here.)
* identity stays the entity's own UUID.  ``slot`` is an *address*, valid within
  one :attr:`generation`; ``properties['id']`` is the *name*.  Frame code
  indexes columns by slot and never looks a name up.

Refresh discipline
------------------
Nothing is re-read per frame. A row is resolved when the row set changes or
the editor's world epoch moves, and again whenever :mod:`engine.change_journal`
names its entity:

* assigning ``Thing.pos`` journals the move itself -- the AI, physics,
  parented lights and portals, plugins and the editor all move entities by
  assignment;
* runtime render state that is not authored -- a carried Prop's yaw, a respawn
  fade, a portal's fade, an Effect's playback clock -- is a
  :class:`~engine.change_journal.TrackedAttribute` and journals itself too;
* everything else a row reads (``hidden``, a Monster's ``dead`` and
  ``is_shooting``, a Light's state) is written by code that calls ``touch``:
  the I/O dispatcher after every input, the parking-aware flag writer, the
  monster AI when a flag actually changes, the console, save restores.

The one per-frame computation is the Effect clock -- elapsed time, liveness and
the flickering light -- and it is column arithmetic over the Effect rows.
A row whose object cannot journal (a raw dict standing in for a Thing) has its
position and ``hidden`` read per frame instead.

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

from .change_journal import JOURNAL, OVERFLOW, STATE, is_tracked
from .portal_transform import basis_from_rotation

# Defensive, as everywhere else in engine/: editor.things pulls in PyQt5, and
# the standalone player tier does not have it.  A tier without the classes
# classifies every entity as a plain Thing, which is what it is there.
try:
    from editor.things import (Thing, PathNode, Portal, Prop, Monster,
                               LogicGate, LogicRelay, LogicTimer, LevelChanger,
                               Light, LogicSpawner, LogicCamera, Effect)
except ImportError:                                   # pragma: no cover
    Thing = PathNode = Portal = Prop = Monster = Effect = None
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
#: Drawn as a sprite whatever else is true -- a Portal or a Monster.
ENT_ALWAYS_SPRITE   = 1 << 1
#: Carries a ``model_path``.
ENT_HAS_MODEL       = 1 << 2
#: ``render_mode`` resolves to ``'model'`` (the default when unset).
ENT_MODE_MODEL      = 1 << 3
#: ``render_mode`` resolves to ``'billboard'``.
ENT_MODE_BILLBOARD  = 1 << 4
#: Carries a ``sprite_path``.
ENT_HAS_SPRITE      = 1 << 5
#: Monster / LogicGate / LogicRelay / LogicTimer / LevelChanger -- the classes
#: ``_thing_render_kind`` calls ``entity_sprite``.
ENT_ENTITY_SPRITE   = 1 << 7
#: A Prop.  Drawn as a sprite only when it is a billboard with a sprite.
ENT_PROP            = 1 << 8
#: A Light.  Collected into the frame's light list; never a sprite in play.
ENT_LIGHT           = 1 << 9
#: A Monster.  Its row is re-resolved when the AI journals a change to the
#: flags its sprite is chosen from.
ENT_MONSTER         = 1 << 10
#: A Portal.  Distinct from :data:`ENT_ALWAYS_SPRITE`, which a monster also
#: carries, because the distance cull exempts Portals and Lights and must not
#: exempt monsters.
ENT_PORTAL          = 1 << 11
#: Procedural Effect primitive; FIRE and EXPLOSION share one render path.
ENT_EFFECT          = 1 << 13

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
    (ENT_ENTITY_SPRITE, 'ENTITY_SPRITE'),
    (ENT_PROP, 'PROP'), (ENT_LIGHT, 'LIGHT'), (ENT_MONSTER, 'MONSTER'),
    (ENT_PORTAL, 'PORTAL'), (ENT_EFFECT, 'EFFECT'),
)


def describe(bits) -> str:
    """The set bits of a classification word, for a readable assertion."""
    return '|'.join(name for bit, name in BIT_NAMES if bits & bit) or 'NONE'


# --------------------------------------------------------------------------
# Sprite identity
# --------------------------------------------------------------------------
#
# A monster's sprite follows `dead` and `is_shooting`, a logic gate's its type
# and a Prop's its representation.  All of it is resolved with the rest of the
# row -- at reconcile, and whenever the entity is journalled as changed -- so a
# frame in which no sprite changed builds no recipe.
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

    Reads the same four state fields that branch reads, from a Monster's
    properties, and builds the same ``msprite_`` cache key.  The recipe also carries the fallback behaviour of
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
        return None                # a raw dict row is never drawn
    if Portal is not None and isinstance(thing, Portal):
        return None                # the sprite pass has always skipped Portals
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
    elif LevelChanger is not None and isinstance(thing, LevelChanger):
        return (('LevelChanger', 'levelchanger.png', 'sprites', True),)
    elif LogicRelay is not None and isinstance(thing, LogicRelay):
        return (('LogicRelay', 'logic_relay.png', 'sprites', True),)
    elif LogicTimer is not None and isinstance(thing, LogicTimer):
        return (('LogicTimer', 'logic_timer.png', 'sprites', True),)

    # -- and then the class's shared sprite, which draw_sprites falls back to -
    class_name = type(thing).__name__
    if LogicSpawner is not None and isinstance(thing, LogicSpawner):
        out.append(('LogicSpawner', 'logic_spawner.png', 'sprites', True))
    elif LogicCamera is not None and isinstance(thing, LogicCamera):
        out.append(('LogicCamera', 'logic_camera.png', 'sprites', True))
    elif props.get('sprite_path'):
        filename, subfolder = _split_asset_path(props.get('sprite_path'))
        out.append((class_name, filename, subfolder, True))
    else:
        out.append((class_name,) + _LOOKUP_ONLY[:2] + (False,))
    return tuple(out)


def sprite_size(thing):
    """The billboard's world size, in the order ``draw_sprites`` decides it."""
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
    if isinstance(thing, dict):
        return ENT_SKIP
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

    entity_sprite_types = tuple(
        c for c in (Monster, LogicGate, LogicRelay, LogicTimer, LevelChanger)
        if c is not None)
    if entity_sprite_types and isinstance(thing, entity_sprite_types):
        bits |= ENT_ENTITY_SPRITE
    if Prop is not None and isinstance(thing, Prop):
        bits |= ENT_PROP
    if Light is not None and isinstance(thing, Light):
        bits |= ENT_LIGHT
    if Effect is not None and isinstance(thing, Effect):
        bits |= ENT_EFFECT
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


def _effect_bool(props, key, default=True):
    value = props.get(key, default)
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


_EFFECT_FIRE_TEXTURES = tuple(
    f"assets/textures/effects/fire{i:02d}.gif" for i in range(1, 6)
)
_EFFECT_FIRE_TEXTURE_TO_INDEX = {
    path: index for index, path in enumerate(_EFFECT_FIRE_TEXTURES)
}
_EFFECT_ORB_TEXTURES = tuple(
    f"assets/textures/effects/orb{i:02d}.gif" for i in range(1, 6)
)
_EFFECT_ORB_TEXTURE_TO_INDEX = {
    path: index for index, path in enumerate(_EFFECT_ORB_TEXTURES)
}

# FIRE's emitted light follows the dominant colour of the selected texture.
# These are authored by the effect variant, not by a global ambient setting.
_EFFECT_FIRE_LIGHT_COLOURS = np.asarray((
    (0xE4, 0x92, 0x34),  # fire01 #e49234
    (0xFF, 0x9A, 0x00),  # fire02 #ff9a00
    (0xFC, 0x24, 0x00),  # fire03 #fc2400
    (0xFE, 0xAC, 0x1D),  # fire04 #feac1d
), dtype=np.float32) / 255.0

_EFFECT_ORB_LIGHT_COLOUR = np.asarray(
    (0x4A, 0x9B, 0xFF), dtype=np.float32
) / 255.0

# EXPLOSION's atlas is 16 frames, numbered 1..16 for authoring.
# The editor preview is intentionally locked to frame 10 (atlas index 9).
_EXPLOSION_FRAME_COUNT = 16.0
_EXPLOSION_PREVIEW_FRAME = 10

def _effect_fire_variant(props):
    value = str(
        props.get("fire_texture", _EFFECT_FIRE_TEXTURES[0])
    ).replace("\\", "/")
    return _EFFECT_FIRE_TEXTURE_TO_INDEX.get(value, 0)


def _effect_orb_variant(props):
    value = str(
        props.get("orb_texture", _EFFECT_ORB_TEXTURES[0])
    ).replace("\\", "/")
    return _EFFECT_ORB_TEXTURE_TO_INDEX.get(value, 0)


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


def _carry_sprite_yaw(thing):
    """Return the numeric carry yaw, or the sentinel for a free billboard."""
    value = getattr(thing, '_carry_sprite_yaw', -10000.0)
    try:
        return -10000.0 if value is None else float(value)
    except (TypeError, ValueError):
        return -10000.0


def _render_alpha(thing):
    """Return the runtime render opacity for an entity."""
    try:
        return max(0.0, min(1.0, float(
            getattr(thing, '_respawn_fade_alpha', 1.0)
        )))
    except (TypeError, ValueError):
        return 1.0


#: Every per-row column: ``(name, trailing shape, dtype, fill)``. One list, so
#: growing the table and moving surviving rows at a reconcile cannot miss one.
_COLUMNS = (
    # float64 to match the brush table's centre column, so the two can be
    # compared and combined without a cast.
    ('pos', (3,), np.float64, 0.0),
    ('class_bits', (), np.uint16, 0),
    #: The authored ``hidden`` flag (Big World parks through it).
    ('hidden', (), bool, False),
    #: Per-light GL colour 0..1, (intensity, radius), on/off, shadows.
    ('light_color', (3,), np.float32, 0.0),
    ('light_params', (2,), np.float32, 0.0),
    ('light_enabled', (), bool, False),
    ('light_casts_shadows', (), bool, False),
    #: Portal topology/render columns. Target links are resolved to integer
    #: entity slots at reconcile.
    ('portal_target_slot', (), np.int32, -1),
    ('portal_active', (), bool, False),
    ('portal_direction', (), np.uint8, 0),
    ('portal_width_height', (2,), np.float32, 0.0),
    ('portal_basis', (3, 3), np.float64, 0.0),
    ('portal_fade', (), np.float32, 0.0),
    ('portal_color', (3,), np.float32, 1.0),
    ('portal_show_rim', (), bool, False),
    #: Procedural Effect state. Authored data and the playback runtime the
    #: Effect owns are resolved per row; elapsed/alive are advanced per frame
    #: from those, numerically.
    ('effect_type', (), np.uint8, 0),
    ('effect_fire_variant', (), np.uint8, 0),
    ('effect_custom_id', (), np.int32, 0),
    ('effect_custom_loop', (), bool, True),
    ('effect_preview', (), bool, False),
    ('effect_params', (4,), np.float32, 0.0),
    ('effect_color', (3,), np.float32, 1.0),
    ('effect_light_color', (3,), np.float32, 1.0),
    ('effect_light_enabled', (), bool, False),
    ('effect_lifetime', (), np.float32, 0.5),
    ('effect_seed', (), np.float32, 1.0),
    #: When the animation started: the Effect's playback start, or the shared
    #: clock origin for one that has none (see :data:`_CLOCK_ORIGIN`).
    ('effect_spawn_time', (), np.float64, 0.0),
    # Fraction of the animation cycle at which this Effect starts.
    ('effect_phase', (), np.float32, 0.0),
    ('effect_elapsed', (), np.float32, 0.0),
    ('effect_active', (), bool, False),
    ('effect_alive', (), bool, False),
    #: The billboard's world size.
    ('sprite_size', (2,), np.float32, 0.0),
    #: Runtime opacity (a respawning Prop fades in).
    ('render_alpha', (), np.float32, 1.0),
    #: Locked world-facing yaw for a carried billboard; -10000 means an
    #: ordinary camera-facing billboard.
    ('sprite_fixed_yaw', (), np.float32, -10000.0),
    #: Interned sprite recipe id; :data:`SPRITE_NONE` for a row that draws none.
    ('sprite_key_id', (), np.int32, SPRITE_NONE),
    #: Interned model recipe id; -1 means no model.
    ('model_recipe_id', (), np.int32, -1),
    #: Model transform, a flattened mat4, and its normal matrix padded to 12.
    ('model_base_matrix', (16,), np.float32, 0.0),
    ('model_normal_matrix', (12,), np.float32, 0.0),
)

#: Seconds since import: the clock an Effect with no playback origin animates
#: on. Both render buffers read the same one, and it stays small enough for the
#: float32 elapsed column to keep millisecond precision.
_CLOCK_ORIGIN = time.perf_counter()


class EntityTable:
    """A dense projection of a Thing list, kept current by change, not polling.

    Rows are addressed by ``slot`` and named by ``properties['id']``. Build
    one, :meth:`begin_frame` it once per frame, and read its columns.
    """

    __slots__ = (tuple(name for name, *_ in _COLUMNS) + (
        'generation', 'count', 'ids', 'slot_of_id', 'things',
        'all_slots', 'light_slots', 'portal_slots', 'monster_slots',
        'effect_slots',
        '_sprite_ids', '_sprite_recipes', '_model_ids', '_model_recipes',
        '_effect_custom_ids', '_effect_custom_paths',
        '_epoch', '_slot_of_obj', '_poll_slots', '_row_tuple', 'rows_read',
        '__weakref__'))

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
        #: ``id(entity)`` -> slot, for applying the change journal.
        self._slot_of_obj: dict = {}
        #: Rows whose object does not journal its changes (a raw dict standing
        #: in for a Thing); their position and ``hidden`` are read per frame.
        self._poll_slots = np.empty(0, dtype=np.intp)
        for name, shape, dtype, fill in _COLUMNS:
            setattr(self, name, np.full((0,) + shape, fill, dtype=dtype))

        #: Every row, as a slot vector; rebuilt only when the rows change.
        self.all_slots = np.empty(0, dtype=np.int32)
        self.light_slots = np.empty(0, dtype=np.int32)
        self.portal_slots = np.empty(0, dtype=np.int32)
        self.monster_slots = np.empty(0, dtype=np.int32)
        self.effect_slots = np.empty(0, dtype=np.int32)
        # Recipe intern tables.  GL-free, like the brush table's texture
        # names: these are ids for *recipes*, and the renderer maps them to GL
        # objects once per unique recipe on the thread that has a context.
        self._sprite_ids: dict = {}
        self._sprite_recipes: list = []
        self._model_ids = {}
        self._model_recipes = []
        #: Interned CUSTOM GIF paths; these remain cold data outside numeric rows.
        self._effect_custom_ids = {}
        self._effect_custom_paths = []

        self._epoch = None
        #: The row set as a tuple, for :meth:`needs_reconcile`.
        self._row_tuple = ()
        #: Rows whose entity the last :meth:`begin_frame` read (Debug Tables).
        self.rows_read = 0
        JOURNAL.subscribe(self)

    # -- recipe interning --------------------------------------------------

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

    def intern_effect_custom_path(self, path) -> int:
        """Intern a CUSTOM Effect GIF path as a stable dense id."""
        value = str(path or "").strip().replace("\\", "/")
        if not value:
            return 0
        custom_id = self._effect_custom_ids.get(value)
        if custom_id is None:
            custom_id = len(self._effect_custom_paths) + 1
            self._effect_custom_ids[value] = custom_id
            self._effect_custom_paths.append(value)
        return custom_id

    def effect_custom_path(self, custom_id: int) -> str:
        """Resolve a dense CUSTOM GIF id without touching entity objects."""
        index = int(custom_id) - 1
        if index < 0 or index >= len(self._effect_custom_paths):
            return ""
        return self._effect_custom_paths[index]

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
        capacity = len(self.pos)
        if n <= capacity:
            return
        grown = max(16, capacity * 2, n)
        for name, shape, dtype, fill in _COLUMNS:
            old = getattr(self, name)
            new = np.full((grown,) + shape, fill, dtype=dtype)
            new[:capacity] = old
            setattr(self, name, new)

    # -- synchronisation ---------------------------------------------------

    def needs_reconcile(self, things, epoch=None) -> bool:
        """Whether the next :meth:`begin_frame` will rebuild the row mapping.

        The epoch says the editor changed something; the row comparison
        catches what changes the entity list without one -- a Kill and a
        spawn in the same frame leave the count alone. Comparing two tuples
        of the same objects is an identity check per element in C, a few
        nanoseconds a row, and reads nothing from any entity.
        """
        if epoch is None or epoch != self._epoch:
            return True
        return tuple(things) != self._row_tuple

    def begin_frame(self, things, epoch=None, dirty_objects=None,
                    effect_runtime=False):
        """Bring the table into line with *things*; return the ``hidden`` mask.

        Nothing here visits an entity that has not changed. The row set is
        reconciled when the editor's world epoch moves; after that, the rows
        that change are the ones :mod:`engine.change_journal` names -- entities
        that moved (``pos`` assignment journals itself) and entities whose
        render state changed (``touch``). The only per-frame work over the
        whole table is the Effect clock, which is column arithmetic.
        """
        # The logic thread normally hands us EditorState.things, a live list
        # the editor thread can also append to. Freeze it at the frame
        # boundary so every step below sees the same rows.
        things = tuple(things)
        n = len(things)
        changes = JOURNAL.drain(self)
        self.rows_read = 0
        resolved_all = False
        if self.needs_reconcile(things, epoch):
            resolved_all = self._reconcile(things, dirty_objects=dirty_objects)
            self._epoch = epoch
        if changes is OVERFLOW:
            if not resolved_all:
                self.refresh_rows(things, range(n))
        elif changes:
            self._apply_changes(things, changes)
        if len(self._poll_slots):
            self._poll(things)
        if len(self.effect_slots):
            self._advance_effects(effect_runtime)
        return self.hidden[:n]

    def _apply_changes(self, things, changes):
        """Re-read the rows the change journal names, and nothing else."""
        slot_of = self._slot_of_obj
        moved = []
        state = []
        for oid, flags in changes.items():
            slot = slot_of.get(oid)
            if slot is None:
                continue
            if flags & STATE:
                state.append(slot)
            else:
                moved.append(slot)
        if moved:
            self._read_positions(things, moved)
        if state:
            self.refresh_rows(things, state)

    def _read_positions(self, things, slots):
        self.rows_read += len(slots)
        slots = np.asarray(slots, dtype=np.intp)
        self.pos[slots] = np.fromiter(
            chain.from_iterable(_pos_of(things[s]) for s in slots.tolist()),
            dtype=np.float64, count=len(slots) * 3).reshape(-1, 3)

    def _poll(self, things):
        """Rows whose objects cannot journal: read what may have changed."""
        slots = self._poll_slots
        self._read_positions(things, slots)
        self.hidden[slots] = [
            bool(_props_of(things[s]).get('hidden', False)) for s in slots.tolist()]

    def _advance_effects(self, effect_runtime):
        """The Effect clock: elapsed time, liveness and light, as columns.

        Everything here is derived from columns resolved from the Effect's own
        runtime state (:attr:`effect_spawn_time`, :attr:`effect_active`), so
        the two render buffers compute the same frame from the same entity.
        """
        effect_ls = self.effect_slots
        now = time.perf_counter()
        explosion = self.effect_type[effect_ls] == 1
        fire = ~explosion
        origin = self.effect_spawn_time[effect_ls]
        lifetime = np.maximum(self.effect_lifetime[effect_ls], 0.01)

        if effect_runtime:
            elapsed = np.maximum(now - origin, 0.0).astype(np.float32)
            explosion_active = (explosion & self.effect_active[effect_ls]
                                & (elapsed < lifetime))
            preview_explosion = np.zeros_like(explosion)
        else:
            # The editor animates FIRE and leaves EXPLOSION dormant; with
            # preview on, an EXPLOSION is shown on its atlas preview frame.
            elapsed = np.where(
                fire, np.float32(now - _CLOCK_ORIGIN), np.float32(0.0)
            ).astype(np.float32)
            explosion_active = np.zeros_like(explosion)
            preview_explosion = explosion & self.effect_preview[effect_ls]

        # Effect lights: FIRE keeps its authored light; an EXPLOSION gets a
        # short decaying flash only when actually triggered at runtime.
        self.light_enabled[effect_ls] = self.effect_light_enabled[effect_ls]
        self.light_params[effect_ls, 0] = self.effect_params[effect_ls, 2]
        self.light_params[effect_ls, 1] = self.effect_params[effect_ls, 3]
        if explosion.any():
            explosion_slots = effect_ls[explosion]
            if effect_runtime:
                explosion_elapsed = elapsed[explosion]
                flash_active = (explosion_active[explosion]
                                & (explosion_elapsed
                                   < np.minimum(lifetime[explosion], 0.12)))
                decay = np.exp(-explosion_elapsed / 0.035).astype(np.float32)
                self.light_enabled[explosion_slots] = (
                    self.effect_light_enabled[explosion_slots] & flash_active)
                self.light_params[explosion_slots, 0] = (
                    self.effect_params[explosion_slots, 2] * (1.0 + 2.0 * decay))
            else:
                self.light_enabled[explosion_slots] = False

        if preview_explosion.any():
            preview_t = ((float(_EXPLOSION_PREVIEW_FRAME) - 0.5)
                         / _EXPLOSION_FRAME_COUNT)
            elapsed = np.where(preview_explosion,
                               (lifetime * preview_t).astype(np.float32),
                               elapsed)
        self.effect_elapsed[effect_ls] = elapsed

        # FIRE light flicker, from the same seed/clock family as the
        # procedural flame. Modulates the live light column only.
        if fire.any():
            fire_slots = effect_ls[fire]
            flicker = _effect_flicker(
                self.effect_seed[fire_slots],
                elapsed[fire] + self.effect_phase[fire_slots])
            self.light_params[fire_slots, 0] = (
                self.effect_params[fire_slots, 2] * (0.78 + 0.38 * flicker))

        self.effect_alive[effect_ls] = fire | explosion_active | preview_explosion

    def sync(self, things, epoch=None, dirty_objects=None) -> bool:
        """Reconcile without the frame's journal step.  Returns whether it did.

        :meth:`begin_frame` is what the render path calls; this is for callers
        bringing the columns up to date on their own schedule -- tests, and
        anything preparing a pass outside the frame loop.
        """
        before = self.generation
        if self.needs_reconcile(things, epoch):
            self._reconcile(things, dirty_objects=dirty_objects)
            self._epoch = epoch
        return self.generation != before

    def _reconcile(self, things, dirty_objects=None) -> bool:
        """Rebuild the slot mapping, keeping surviving rows' resolved columns.

        Returns whether every row was re-resolved.
        """
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
            for name, *_ in _COLUMNS:
                column = getattr(self, name)
                column[dst] = column[src]

        self.ids = ids
        self.slot_of_id = {eid: slot for slot, eid in enumerate(ids)
                           if eid is not None}
        self._slot_of_obj = {id(thing): slot for slot, thing in enumerate(things)}
        self.things = list(things)
        self._row_tuple = tuple(things)
        self.count = n
        # Rows before the class columns below are recomputed: a row resolved
        # here has its own class bits by the time the slot vectors are built.
        self.refresh_rows(things, [slot for slot in range(n)
                                   if slot not in survivors])
        bits = self.class_bits[:n]
        self.all_slots = np.arange(n, dtype=np.int32)
        self.effect_slots = np.flatnonzero(bits & ENT_EFFECT).astype(np.int32)
        self.light_slots = np.flatnonzero(
            bits & (ENT_LIGHT | ENT_EFFECT)).astype(np.int32)
        self.portal_slots = np.flatnonzero(bits & ENT_PORTAL).astype(np.int32)
        self.monster_slots = np.flatnonzero(bits & ENT_MONSTER).astype(np.int32)
        self._poll_slots = np.asarray(
            [slot for slot, thing in enumerate(things) if not is_tracked(thing)],
            dtype=np.intp)
        self._resolve_portal_links(things)
        self.generation += 1
        return not survivors

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


    def _resolve_row(self, slot, thing):
        """Resolve authored render state for one entity row."""
        self.class_bits[slot] = _entity_class_bits(thing)
        props = _props_of(thing)
        self.hidden[slot] = bool(props.get('hidden', False))

        # Sprite identity/size is cold for authored entities.  This must be
        # resolved at the same edit boundary as class_bits/model_recipe_id:
        # switching a Prop model -> billboard changes the render class and the
        # sprite recipe without changing the entity row itself.
        self.sprite_size[slot] = sprite_size(thing)
        self.render_alpha[slot] = _render_alpha(thing)
        carry_yaw = _carry_sprite_yaw(thing)
        self.sprite_fixed_yaw[slot] = carry_yaw
        self.sprite_key_id[slot] = self.intern_sprite(sprite_candidates(thing))

        if self.class_bits[slot] & ENT_EFFECT:
            props = _props_of(thing)
            effect_type = str(props.get('effect_type', 'FIRE')).strip().upper()
            self.effect_type[slot] = (
                1 if effect_type == 'EXPLOSION'
                else 2 if effect_type == 'ORB'
                else 3 if effect_type == 'CUSTOM'
                else 0
            )
            self.effect_fire_variant[slot] = (
                _effect_orb_variant(props)
                if effect_type == 'ORB'
                else _effect_fire_variant(props)
                if effect_type == 'FIRE'
                else 0
            )
            self.effect_custom_id[slot] = (
                self.intern_effect_custom_path(props.get('custom_gif', ''))
                if effect_type == 'CUSTOM'
                else 0
            )
            self.effect_custom_loop[slot] = _effect_bool(
                props, 'custom_loop', True
            )
            self.effect_preview[slot] = _effect_bool(
                props, 'preview', False
            )
            width = max(0.01, _effect_float(props, 'width', 32.0))
            height = max(
                0.01,
                _effect_float(
                    props,
                    'height',
                    32.0 if effect_type == 'ORB' else 46.0,
                ),
            )
            visual_intensity = max(0.0, _effect_float(props, 'intensity', 1.0))
            light_intensity = max(0.0, _effect_float(props, 'light_intensity', 2.5))
            light_radius = max(0.01, _effect_float(props, 'light_radius', 128.0))
            lifetime = max(0.01, _effect_float(props, 'lifetime', 0.5))
            try:
                seed = float((int(props.get('effect_seed', 1)) % 1000003) + 1)
            except (TypeError, ValueError):
                seed = 1.0

            self.effect_params[slot] = (
                width, visual_intensity, light_intensity, light_radius
            )
            self.effect_color[slot] = _effect_colour(
                props, 'colour', [255, 110, 25]
            )
            if effect_type == 'FIRE' and self.effect_fire_variant[slot] < len(_EFFECT_FIRE_LIGHT_COLOURS):
                self.effect_light_color[slot] = _EFFECT_FIRE_LIGHT_COLOURS[
                    self.effect_fire_variant[slot]
                ]
            elif effect_type == 'ORB':
                self.effect_light_color[slot] = _EFFECT_ORB_LIGHT_COLOUR
            else:
                self.effect_light_color[slot] = _effect_colour(
                    props, 'light_colour', [255, 165, 70]
                )
            self.effect_light_enabled[slot] = _effect_bool(
                props, 'light_enabled', True
            )
            self.effect_lifetime[slot] = lifetime
            self.effect_seed[slot] = seed
            # Playback runtime is the Effect's own (Explode, SetType and a
            # reset all write it there and journal the change), so both
            # render buffers resolve the same origin from it.
            # The column is the animation's origin: an Effect with no
            # playback start (a looping FIRE) runs on the shared clock.
            spawn = _float_property(getattr(thing, '_effect_spawn_time', 0.0), 0.0)
            self.effect_spawn_time[slot] = spawn if spawn > 0.0 else _CLOCK_ORIGIN
            self.effect_phase[slot] = max(0.0, min(1.0, _float_property(
                getattr(thing, '_effect_animation_phase', 0.0), 0.0)))
            active = bool(getattr(thing, '_effect_active',
                                  effect_type != 'EXPLOSION'))
            self.effect_elapsed[slot] = 0.0
            self.effect_active[slot] = active
            self.effect_alive[slot] = effect_type != 'EXPLOSION' or active

            self.sprite_size[slot] = (width, height)
            self.light_color[slot] = self.effect_light_color[slot]
            self.light_params[slot] = (light_intensity, light_radius)
            self.light_enabled[slot] = _effect_bool(
                props, 'light_enabled', True
            )
            self.light_casts_shadows[slot] = False
        else:
            self.effect_type[slot] = 0
            self.effect_fire_variant[slot] = 0
            self.effect_custom_id[slot] = 0
            self.effect_custom_loop[slot] = True
            self.effect_preview[slot] = False
            self.effect_params[slot].fill(0.0)
            self.effect_color[slot] = 1.0
            self.effect_light_color[slot] = 1.0
            self.effect_light_enabled[slot] = False
            self.effect_lifetime[slot] = 0.5
            self.effect_seed[slot] = 1.0
            self.effect_spawn_time[slot] = 0.0
            self.effect_phase[slot] = 0.0
            self.effect_elapsed[slot] = 0.0
            self.effect_active[slot] = False
            self.effect_alive[slot] = False

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

        if (self.class_bits[slot] & (ENT_LIGHT | ENT_EFFECT)) == ENT_LIGHT:
            self.light_color[slot] = _light_color_of(thing)
            self.light_params[slot] = (_light_float(thing, 'intensity', 1.0),
                                       _light_float(thing, 'radius', 512.0))
            self.light_enabled[slot] = _light_bool(thing, 'state', True)
            self.light_casts_shadows[slot] = _light_bool(
                thing, 'casts_shadows', False)

        if self.class_bits[slot] & ENT_PORTAL:
            self.portal_active[slot] = _bool_property(
                props.get('active', True), True)
            self.portal_fade[slot] = max(0.0, min(1.0, _float_property(
                getattr(thing, '_fade_alpha', 1.0), 1.0)))
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
        """Re-resolve every column of *slots* from their entities."""
        slots = list(slots)
        self.rows_read += len(slots)
        for slot in slots:
            slot = int(slot)
            thing = things[slot]
            self.pos[slot] = _pos_of(thing)
            self._resolve_row(slot, thing)


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
    4. otherwise the entity's *kind* decides -- the
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
    effect = (bits & ENT_EFFECT) != 0
    skip = skip | effect
    always_sprite = (bits & ENT_ALWAYS_SPRITE) != 0

    has_model = (bits & ENT_HAS_MODEL) != 0
    mode_model = (bits & ENT_MODE_MODEL) != 0
    mode_billboard = (bits & ENT_MODE_BILLBOARD) != 0
    has_sprite = (bits & ENT_HAS_SPRITE) != 0
    entity_sprite = (bits & ENT_ENTITY_SPRITE) != 0
    prop = (bits & ENT_PROP) != 0

    # Step 3. `model_visible` in the loop; the only place the live flag is read.
    model = ~skip & ~always_sprite & has_model & mode_model & ~is_hidden

    # Step 4's `kind`, in the order _thing_render_kind tries the classes.
    kind_sprite = ~entity_sprite & mode_billboard & has_sprite

    remainder = ~skip & ~always_sprite & ~model
    # A Prop is a sprite only as a billboard with a sprite, and is never
    # reached by the clauses below -- the loop returns through its own branch.
    prop_sprite = remainder & prop & mode_billboard & kind_sprite
    others = remainder & ~prop
    sprite = (
        prop_sprite
        | (others & entity_sprite)
        # `elif model_path and render_mode == 'model'` draws nothing: that is
        # the hidden model, already excluded from `model` above.
        | (others & ~entity_sprite & ~(has_model & mode_model)
           & (kind_sprite | (not is_play or show_sprites)))
    )
    return slots[model], slots[sprite | (~skip & always_sprite)]
