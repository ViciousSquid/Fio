"""Soma (Greek: "body") simulates bodies.

That is the whole of what it does. The world is not Soma's: walls and floors
are asked of the :class:`engine.spatial_grid.SpatialGrid` it is created with.
The game's rules are not Soma's either: what is heavy and what crushing
costs are the contract's. Soma holds the bodies and moves the awake ones.

Fio's built-in physics, registered as ``"Soma"``
(:mod:`engine.physics.registry`) and reached by the engine only through the
contract (:class:`engine.physics.Physics`).

Tiers
-----
Soma comes in tiers: how much it does for a body, chosen once for a play
session.

``full`` (the default)
    Everything: true surfaces (slopes, the terrain), bodies meeting bodies,
    swept contact. :class:`engine.physics.soma.full.Soma`.

``crude``
    Fio's physics exactly as it was before Soma, kept unchanged: boxes
    against boxes, nothing more. :class:`engine.physics.soma.crude.SomaCrude`.

The tier is :func:`select_tier`'s to set -- from ``settings.ini``
(``[Physics] tier``), the ``FIO_PHYSICS_TIER`` environment variable
(:func:`configure_tier`) or the console's ``phys_tier`` -- and takes effect
with the next play session: a running one keeps the tier it was created
with. A tier is loaded when it is first used, so the one a host never
selects costs it nothing, not even an import.
"""

import importlib

#: Tier name -> where its class lives, resolved on first use.
TIERS = {
    'full': 'engine.physics.soma.full:Soma',
    'crude': 'engine.physics.soma.crude:SomaCrude',
}

#: The tier a session gets unless another is selected.
DEFAULT_TIER = 'full'

_selected = DEFAULT_TIER


def available_tiers():
    """Soma's tier names."""
    return list(TIERS)


def tier_class(name):
    """The class that is tier *name*. Raises KeyError for an unknown tier."""
    target = TIERS[str(name).strip().lower()]
    if isinstance(target, str):
        module_name, _, attr = target.partition(':')
        target = getattr(importlib.import_module(module_name), attr)
        TIERS[str(name).strip().lower()] = target
    return target


def select_tier(name):
    """Make *name* the tier the next play session's Soma is. Raises KeyError
    for an unknown tier; names match in any letter case."""
    global _selected
    name = str(name).strip().lower()
    if name not in TIERS:
        raise KeyError(f"Soma has no tier {name!r}")
    _selected = name


def selected_tier():
    """The tier :func:`create` makes by default."""
    return _selected


def configure_tier(config=None, environ=None):
    """Select the tier a host was asked for, and return it.

    ``FIO_PHYSICS_TIER`` in the environment wins; then ``[Physics] tier`` in
    *config* (the application's ConfigParser); otherwise the selection is
    left as it is. A value that names no tier is ignored: a typing mistake in
    a settings file must not stop Play.
    """
    import os
    wanted = (os.environ if environ is None else environ).get('FIO_PHYSICS_TIER')
    if not wanted and config is not None:
        try:
            wanted = config.get('Physics', 'tier', fallback=None)
        except Exception:
            wanted = None
    if wanted and str(wanted).strip().lower() in TIERS:
        select_tier(wanted)
    return selected_tier()


def create(spatial_grid, tier=None):
    """Soma, at *tier* (the selected one by default), over *spatial_grid*:
    the factory the physics registry holds as ``"Soma"``."""
    return tier_class(selected_tier() if tier is None else tier)(spatial_grid)


#: Names that live in a tier's module, importable from here (on first use).
_MOVED = {
    'Soma': 'engine.physics.soma.full',
    'PhysicsWorld': 'engine.physics.soma.full',
    'PhysicsBody': 'engine.physics.soma.full',
    'quat_from_rotation': 'engine.physics.soma.full',
    'rotation_from_quat': 'engine.physics.soma.full',
    'SomaCrude': 'engine.physics.soma.crude',
}


def __getattr__(name):
    home = _MOVED.get(name)
    if home is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(home), name)
    globals()[name] = value
    return value
