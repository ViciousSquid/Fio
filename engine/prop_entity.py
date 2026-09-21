"""The one and only Prop entity class.

A Prop is a core engine primitive: a carryable world object with optional
physics. It must exist identically in all three execution contexts:

* the editor (``editor.things`` available, PyQt5 loaded),
* editor play mode (same process as the editor),
* the standalone ``.fiopak`` player / Android build (no PyQt5).

There is therefore exactly one ``Prop`` class, defined here. Its base class is
resolved once, at import, by the same rule every plugin entity uses:
``editor.things.Model`` when the editor tier can be imported, otherwise the
dependency-free :class:`plugins.entitybase.Model`. Within any one process there
is a single ``Prop`` object; ``editor.things.Prop`` and ``plugins.entitybase.Prop``
are lazy aliases of it, never separate implementations.

This module itself imports no Qt, OpenGL or NumPy. Editor-only behaviour
(the 2D billboard pixmap) is reached through a helper on the editor ``Thing``
base and is simply absent when that base is the headless fallback.
"""

from __future__ import annotations

try:
    from editor.things import Model as _ModelBase
    EDITOR_TIER = True
except ImportError:  # standalone player / Android: no PyQt5
    from plugins.entitybase import Model as _ModelBase
    EDITOR_TIER = False


#: Authored defaults, applied with ``setdefault`` so saved values always win.
PROP_DEFAULTS = {
    'render_mode': 'model',
    'sprite_path': 'assets/sprites/pickup.png',
    'sprite_size': [32.0, 32.0],
    'mass': 1.0,
    'collision_size': [0.0, 0.0, 0.0],
    'physics_enabled': False,
    'no_collision': True,
    'collision_shape': 'auto',
    'gravity': True,
    'friction': 0.55,
    'linear_damping': 0.08,
    'angular_damping': 0.12,
    'pickup_enabled': True,
    'pickup_reach': 110.0,
    'carry_distance': 55.0,
    'carry_offset': [0.0, -6.0, 0.0],
    'drop_velocity': 0.0,
    'drop_angular_velocity': [0.0, 0.0, 0.0],
    'disabled': False,
    'io_enabled': True,
}


class Prop(_ModelBase):
    """A generic carryable world object.

    A prop uses ``model_path`` when ``render_mode`` is ``'model'``; otherwise
    ``sprite_path`` is rendered as a camera-facing billboard. All state is
    plain data in ``properties``, so maps serialize through the base ``Thing``
    without a special format.
    """

    pixmap_path = "assets/sprites/pickup.png"
    EDITOR_PRIMARY_PROPERTIES = (
        'render_mode',
        'sprite_path',
        'sprite_size',
        'pickup_enabled',
        'pickup_reach',
        'carry_distance',
        'carry_offset',
        'drop_velocity',
        'drop_angular_velocity',
        'disabled',
    )

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'prop'
        # Read before the defaults are applied: self.properties IS the dict
        # that was passed in, so setdefault below would otherwise make every
        # record look as though it had authored a render_mode.
        authored_render_mode = 'render_mode' in self.properties
        for key, value in PROP_DEFAULTS.items():
            # Copy mutable defaults so instances never share a list.
            self.properties.setdefault(
                key, list(value) if isinstance(value, list) else value)
        if not authored_render_mode:
            self.properties['render_mode'] = self._implied_render_mode()

    def _implied_render_mode(self):
        """The representation this prop's authored assets imply.

        ``render_mode`` is authoritative, and a record that states one is never
        second-guessed. A record that states none needs a default that can
        actually draw something, and a fixed one cannot: PROP_DEFAULTS ships a
        ``sprite_path`` but no ``model_path``, so defaulting to ``'model'``
        produced a prop classified as a model with no model to draw -- it
        reached neither the model list nor the sprite list and was rendered by
        nothing at all. That is what the editor's "add Prop" action created.

        So the default follows the assets: a model when one is set, otherwise
        the billboard whose texture is always present. Resolved once here
        rather than in the renderer, which keeps ``render_mode`` a single
        authoritative property everywhere downstream and adds no per-frame
        branch to a hot path.
        """
        if self.properties.get('model_path'):
            return 'model'
        if self.properties.get('sprite_path'):
            return 'billboard'
        return PROP_DEFAULTS['render_mode']

    def get_sprite_path(self):
        """Return the authored billboard texture path, if this prop has one."""
        return self.properties.get('sprite_path', '')

    def get_instance_pixmap(self):
        """2D editor icon: the authored billboard, else the class sprite.

        Only meaningful on the editor tier; the headless base has no pixmaps.
        """
        loader = getattr(self, '_pixmap_for_path', None)
        sprite_path = self.get_sprite_path()
        if loader is not None and sprite_path:
            return loader(sprite_path)
        base = getattr(super(), 'get_instance_pixmap', None)
        return base() if base is not None else None
