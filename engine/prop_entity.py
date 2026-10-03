"""The one and only Prop entity class.

A Prop is the sole generic world-object primitive. It exists identically in the
editor, editor play mode and the standalone player / Android build.

A Prop may independently carry, collect, use physics, and render as either a
billboard or a model. Collection is an optional gameplay behaviour on the Prop;
it is not a separate entity type -- and neither is a model. Every 3D model in a
Fio world is a Prop with ``render_mode='model'``; the old ``Model`` entity type
is read from maps as one (see :data:`LEGACY_MODEL_DEFAULTS`).
"""

from __future__ import annotations

from engine.change_journal import TrackedAttribute

try:
    from editor.things import Thing as _ThingBase
    EDITOR_TIER = True
except ImportError:  # standalone player / Android: no PyQt5
    from plugins.entitybase import Thing as _ThingBase
    EDITOR_TIER = False


DEFAULT_MODEL_PATH = 'assets/models/Oil_Drum.obj'

KEY_SPRITES = {
    'blue_key': 'assets/sprites/bluekey.png',
    'red_key': 'assets/sprites/redkey.png',
    'yellow_key': 'assets/sprites/yellowkey.png',
}
KEY_NAMES = tuple(KEY_SPRITES)
DEFAULT_KEY_NAME = 'blue_key'

GUN_SPRITES = {
    'gun1': 'assets/sprites/gun1.png',
    'gun2': 'assets/sprites/gun2.png',
    'cig': 'assets/sprites/cig.png',
}
GUN_NAMES = tuple(GUN_SPRITES)

COLLECT_TYPES = ('health', 'ammo', 'weapon', 'key')

AMMO_SPRITE = 'assets/sprites/ammo.png'
HEALTH_SPRITE = 'assets/sprites/health.png'


def normalise_collect_type(properties):
    """Make ``collect_type`` one of :data:`COLLECT_TYPES`, in place.

    There used to be a fifth, ``custom``: a pickup that was consumed and gave
    the player nothing. Maps still carry it -- the sample map's shotgun was
    migrated from the old Pickup entity as ``custom`` with the gun's sprite, so
    walking over it made it vanish and handed over no gun. A pickup's sprite
    says what it is meant to be, so an unrecognised type is read from it: a
    gun's sprite collects that gun, a key's that key, the ammo box ammo, and
    anything else health.
    """
    kind = str(properties.get('collect_type', 'health') or 'health').lower()
    if kind in COLLECT_TYPES:
        properties['collect_type'] = kind
        return kind
    sprite = str(properties.get('collect_custom_sprite')
                 or properties.get('sprite_path') or '').replace('\\', '/')
    for weapon, path in GUN_SPRITES.items():
        if sprite == path:
            properties['collect_weapon'] = weapon
            kind = 'weapon'
            break
    else:
        for key_name, path in KEY_SPRITES.items():
            if sprite == path:
                properties['collect_key_name'] = key_name
                kind = 'key'
                break
        else:
            kind = 'ammo' if sprite == AMMO_SPRITE else 'health'
    if kind != 'health':
        # Only health pickups take a custom sprite; the rest look like what
        # they give.
        properties['collect_custom_sprite'] = ''
    properties['collect_type'] = kind
    return kind

# Authored defaults. Mutable values are copied per Prop instance.
PROP_DEFAULTS = {
    'render_mode': 'billboard',
    'sprite_path': 'assets/sprites/pickup.png',
    'sprite_size': [32.0, 32.0],

    # Carry behaviour. Off unless the author turns it on: a Prop is scenery by
    # default -- neither carryable nor solid (see no_collision below).
    'carry_enabled': False,
    'carry_reach': 110.0,
    'carry_distance': 55.0,
    'carry_offset': [0.0, -6.0, 0.0],
    'drop_velocity': 0.0,
    'drop_angular_velocity': [0.0, 0.0, 0.0],

    # Collection behaviour.
    'collect_enabled': False,
    'collect_type': 'weapon',
    'collect_value': 25,
    'collect_activation': 'walk_over',
    'collect_collected': False,
    'collect_respawns': False,
    'collect_respawn_time': 20.0,
    'collect_key_name': DEFAULT_KEY_NAME,
    'collect_weapon': 'gun1',
    'collect_custom_sprite': '',

    # Physics.
    'mass': 1.0,
    'collision_size': [0.0, 0.0, 0.0],
    'physics_enabled': False,
    'no_collision': True,
    'collision_shape': 'auto',
    'gravity': True,
    'friction': 0.55,
    'linear_damping': 0.08,
    'angular_damping': 0.12,

    'disabled': False,
    'io_enabled': True,

    # Model representation. model_path is deliberately not a default mesh: a
    # billboard Prop must not collide as one (see __init__).
    'rotation': [0, 0, 0],
    'scale': [1, 1, 1],
}

#: What a Prop placed to show a model starts as: the Prop defaults (neither
#: carryable nor solid) in model representation.
MODEL_PROP_DEFAULTS = {
    'render_mode': 'model',
}

#: What a map saved with the old ``Model`` entity meant by one: solid, and not
#: something the player picks up. Applied under whatever the record authors,
#: so a saved map keeps playing as it was saved.
LEGACY_MODEL_DEFAULTS = {
    'render_mode': 'model',
    'no_collision': False,
    'carry_enabled': False,
}


def legacy_model_properties(properties):
    """A pre-Prop ``Model`` record's properties, read as a Prop's.

    Every loader that meets the old ``model`` type token -- the editor's, the
    plugin fallback base's, the standalone player's -- reads it through this,
    so a model is the same Prop wherever the map is opened.
    """
    return {**LEGACY_MODEL_DEFAULTS, **dict(properties or {}), 'type': 'prop'}



class Prop(_ThingBase):
    """A generic world object with optional carry and collect behaviour."""

    DEFAULT_MODEL_PATH = DEFAULT_MODEL_PATH
    KEY_SPRITES = KEY_SPRITES
    KEY_NAMES = KEY_NAMES
    DEFAULT_KEY_NAME = DEFAULT_KEY_NAME
    GUN_SPRITES = GUN_SPRITES
    GUN_NAMES = GUN_NAMES
    COLLECT_TYPES = COLLECT_TYPES

    pixmap_path = "assets/sprites/pickup.png"
    # These are serialized implementation fields, not an inspector checklist.
    # The Prop editor presents them as Representation / Interaction /
    # Collection / Carry settings and only reveals type-relevant controls.
    EDITOR_PRIMARY_PROPERTIES = (
        'disabled',
    )
    EDITOR_ADVANCED_PROPERTIES = (
        'carry_reach',
        'carry_distance',
        'carry_offset',
        'drop_velocity',
        'drop_angular_velocity',
    )

    #: Runtime render state the projection resolves; assignment journals it.
    _respawn_fade_alpha = TrackedAttribute(1.0)
    _carry_sprite_yaw = TrackedAttribute(None)

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'prop'
        # Runtime-only render state; never serialized into the map.
        self._respawn_fade_alpha = 1.0

        authored_render_mode = 'render_mode' in self.properties
        authored_sprite_path = 'sprite_path' in self.properties
        authored_collect_value = 'collect_value' in self.properties
        # Present on every Prop, empty unless it shows a model.
        self.properties.setdefault('model_path', '')
        for key, value in PROP_DEFAULTS.items():
            self.properties.setdefault(
                key, list(value) if isinstance(value, list) else value)
        normalise_collect_type(self.properties)

        if not authored_render_mode:
            self.properties['render_mode'] = self._implied_render_mode()

        # Model representation must always have a usable mesh, even for a map
        # authored with render_mode="model" but no model_path. This keeps the
        # representation switch a complete editor operation rather than a blank
        # entity waiting for an implementation detail to be filled in.
        if (
            self.properties.get('render_mode') == 'model'
            and not self.properties.get('model_path')
        ):
            self.properties['model_path'] = self.DEFAULT_MODEL_PATH

        # Ammo boxes have a stock amount just like the stock health/weapon/key
        # pickups have a stock appearance. An explicitly authored amount wins.
        if (self.properties.get('collect_type') == 'ammo'
                and not authored_collect_value):
            self.properties['collect_value'] = 8

        # A collectible Prop with no explicit appearance follows its collection
        # payload. An authored sprite_path always wins.
        if (self.properties.get('collect_enabled', False)
                and not authored_sprite_path):
            self.properties['sprite_path'] = self.get_collect_sprite_path()

        # Collected Props are not useful as carry targets. What the author set
        # is kept, so resetting the collection restores it rather than forcing
        # every Prop carryable.
        self._carry_before_collect = bool(
            self.properties.get('carry_enabled', False))
        if self.properties.get('collect_collected'):
            self.properties['carry_enabled'] = False

    def reset_collection(self):
        """Un-collect this Prop, restoring the carry setting it was authored with."""
        if self.properties.get('collect_collected'):
            self.properties['carry_enabled'] = self._carry_before_collect
        self.properties['collect_collected'] = False

    @classmethod
    def for_model(cls, model_path, pos=None, properties=None):
        """A Prop showing the model at *model_path*, with the Prop defaults.

        The one way a model enters a world from the editor -- the Asset
        Browser and the 2D view's Add Model both come here -- so a model is
        always a Prop, never a separate kind of entity. Like any Prop it is
        neither carryable nor solid until the author says so.
        """
        props = dict(MODEL_PROP_DEFAULTS)
        props.update(properties or {})
        props['model_path'] = str(model_path).replace('\\', '/')
        return cls(pos=list(pos) if pos is not None else [0, 0, 0],
                   properties=props)

    def _implied_render_mode(self):
        """Resolve representation from authored assets when no mode was saved."""
        if self.properties.get('model_path'):
            return 'model'
        if self.properties.get('sprite_path'):
            return 'billboard'
        return PROP_DEFAULTS['render_mode']

    def get_sprite_path(self):
        """Return the authored billboard texture path."""
        return self.properties.get('sprite_path', '')

    def get_collect_sprite_path(self):
        """Return the conventional sprite for the current collection payload."""
        collect_type = self.properties.get('collect_type', 'health')
        if collect_type == 'key':
            return self.KEY_SPRITES.get(
                self.properties.get('collect_key_name', self.DEFAULT_KEY_NAME),
                'assets/sprites/pickup.png')
        if collect_type == 'weapon':
            return self.GUN_SPRITES.get(
                self.properties.get('collect_weapon', 'gun1'),
                self.GUN_SPRITES['gun1'])
        if collect_type == 'ammo':
            return AMMO_SPRITE
        return self.properties.get('collect_custom_sprite') or HEALTH_SPRITE

    def get_instance_pixmap(self):
        """2D editor icon from the authored Prop sprite."""
        loader = getattr(self, '_pixmap_for_path', None)
        sprite_path = self.get_sprite_path()
        if loader is not None and sprite_path:
            return loader(sprite_path)
        base = getattr(super(), 'get_instance_pixmap', None)
        return base() if base is not None else None

    @classmethod
    def get_key_sprite_path(cls, key_name):
        return cls.KEY_SPRITES.get(key_name, 'assets/sprites/pickup.png')

    @classmethod
    def get_key_pixmap(cls, key_name):
        """Editor-only key icon helper used by the HUD."""
        if not EDITOR_TIER:
            return None
        sprite_path = cls.get_key_sprite_path(key_name)
        try:
            from PyQt5.QtGui import QPixmap
            import os
            project_root = os.path.abspath(
                os.path.join(os.path.dirname(__file__), os.pardir))
            absolute_path = os.path.join(project_root, sprite_path)
            pixmap = QPixmap(absolute_path)
            return None if pixmap.isNull() else pixmap
        except Exception:
            return None

    @classmethod
    def clear_sprite_cache(cls):
        """Keep the old editor invalidation seam for the unified Prop."""
        cache = getattr(cls, '_pixmap_cache', None)
        if isinstance(cache, dict):
            cache.clear()
