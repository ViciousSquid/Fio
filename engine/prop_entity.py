"""The one and only Prop entity class.

A Prop is the sole generic world-object primitive. It exists identically in the
editor, editor play mode and the standalone player / Android build.

A Prop may independently carry, collect, use physics, and render as either a
billboard or a model. Collection is an optional gameplay behaviour on the Prop;
it is not a separate entity type.
"""

from __future__ import annotations

try:
    from editor.things import Model as _ModelBase
    EDITOR_TIER = True
except ImportError:  # standalone player / Android: no PyQt5
    from plugins.entitybase import Model as _ModelBase
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

COLLECT_TYPES = ('health', 'ammo', 'weapon', 'key', 'custom')

# Authored defaults. Mutable values are copied per Prop instance.
PROP_DEFAULTS = {
    'render_mode': 'billboard',
    'sprite_path': 'assets/sprites/pickup.png',
    'sprite_size': [32.0, 32.0],

    # Carry behaviour.
    'carry_enabled': True,
    'carry_reach': 110.0,
    'carry_distance': 55.0,
    'carry_offset': [0.0, -6.0, 0.0],
    'drop_velocity': 0.0,
    'drop_angular_velocity': [0.0, 0.0, 0.0],

    # Collection behaviour.
    'collect_enabled': False,
    'collect_type': 'health',
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
}



class Prop(_ModelBase):
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

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'prop'
        # Runtime-only render state; never serialized into the map.
        self._respawn_fade_alpha = 1.0

        authored_render_mode = 'render_mode' in self.properties
        authored_sprite_path = 'sprite_path' in self.properties
        authored_collect_value = 'collect_value' in self.properties
        for key, value in PROP_DEFAULTS.items():
            self.properties.setdefault(
                key, list(value) if isinstance(value, list) else value)

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

        # Collected Props are not useful as carry targets.
        if self.properties.get('collect_collected'):
            self.properties['carry_enabled'] = False

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
            return 'assets/sprites/ammo.png'
        if collect_type == 'health':
            return self.properties.get(
                'collect_custom_sprite') or 'assets/sprites/health.png'
        return self.properties.get(
            'collect_custom_sprite') or self.get_sprite_path()

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
