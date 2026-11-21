"""
Public API surface for Fio plugins.

Everything a plugin author needs is here: the :class:`FioPlugin` base class to
subclass, and the "API" objects the manager hands to a plugin at defined moments
(:class:`EditorAPI` at load time, :class:`RuntimeAPI` when a play session's logic
thread spins up). The open-ended engine seam — :class:`~plugins.host.PluginHost`
and the event bus — lives alongside in :mod:`plugins.host`.

For the flat, human-readable reference (every class, method and signature with
examples) see ``plugins/API.md``; for the narrative introduction see
``plugins/README.md``. This module is the annotated source those docs mirror.

The surface at a glance
-----------------------
==========================  ========================================================
Object / helper             Purpose
==========================  ========================================================
:class:`FioPlugin`          Base class you subclass; expose an instance as ``PLUGIN``.
:class:`EditorAPI`          Load-time: register entities, I/O, schemas, UI, renderers.
:class:`RuntimeAPI`         Per-session: I/O handlers, scene queries, spawn, raycast,
                            the physics engine (Soma) and the player.
:class:`PlayerState`        The player: position, motion, view, health and armour.
:class:`Physics`            The physics contract Soma answers to (:class:`Body`:
                            one simulated body).
:class:`TickContext`        Per-tick: read input, drive the HUD.
:class:`PropertySpec`       Declare a typed, self-documenting entity property.
:class:`GlobalStore`        Cross-level key/value storage (shared with map KV stores).
:func:`io_def`              Build an I/O port definition without importing io_system.
:func:`prop`                Terse :class:`PropertySpec` constructor.
:func:`key_code`            Resolve a key name/code to a Qt key code.
==========================  ========================================================

Design goals
------------
* **Loose coupling.** The core editor/engine call *into* the manager; plugins
  never import editor internals except through helpers exposed here. The
  editor/engine modules are imported lazily inside methods so that importing
  this module never drags in PyQt or OpenGL (it is imported in headless tools
  and tests too).
* **Fail safe.** A plugin that raises during registration or a hook must never
  crash the host. The manager wraps every call; this module keeps the surface
  small and defensive.
* **Host-agnostic.** The same plugin loads in the editor, the desktop player and
  the Android APK. The runtime path pulls in neither PyGLM nor PyQt; entities
  fall back to :mod:`plugins.entitybase` when the editor package is absent.

API versioning
--------------
:data:`API_VERSION` is the version of this surface. A plugin can declare the
minimum it needs via :attr:`FioPlugin.api_version`; the manager refuses to load
a plugin that needs a newer API than the host provides, with a clear message,
rather than letting it fail deep inside a hook.
"""

from __future__ import annotations

import math
import time as _time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

# The physics contract (API 2.0). ``engine.physics`` is the boundary package:
# stdlib-only, so this pulls in neither NumPy, PyGLM nor Qt.
from engine.physics import (  # noqa: F401 -- re-exported for plugins
    CRUSH_DAMAGE, HEAVY_MASS, NO_PHYSICS, PHYSICS_CONTRACT_VERSION, PHYSICS_EVENTS,
    SV_GRAVITY, Body, Physics, world_gravity)
from engine.spatial import clear_live_flag as _clear_live_flag
from engine.spatial import set_live_flag as _set_live_flag


#: Version of the plugin API surface this module implements. Compare against
#: :attr:`FioPlugin.api_version` (see :func:`version_tuple`).
#:
#: * 1.2.0 — the open-ended extension surface: :class:`plugins.host.PluginHost`,
#:   the engine event bus, and the ``connect(host)`` hook.
#: * 1.3.0 — render hooks, swappable-renderer registration, and editor-UI extensions.
#: * 1.4.0 — optional editor Tools actions and console-command registration.
#: * 1.5.0 — collapsible property sections, LogicState key suggestions and
#:   entity inspectors.
#: * 1.6.0 — Fio 3.0, a major architectural shift from 1.5: renderer
#:   registration takes the :class:`engine.renderer.Renderer` protocol (a
#:   factory is called as ``factory(config)``) and renderers swap live.
#:   Plugins written for 1.3–1.5 that do not use the renderer contract load and
#:   behave unchanged; renderer implementations written for 1.3–1.5
#:   (``cls(texture_loader, grid, world, config)`` and the old forward-renderer
#:   interface) are not compatible, and there is no shim.
#: * 1.7.0 — the pause menu: :meth:`EditorAPI.register_pause_menu_item`, the
#:   ``pause_menu_opened`` / ``pause_menu_closed`` events, and
#:   ``main_window.open_pause_menu()`` / ``close_pause_menu()``.
#: * 1.8.0 — :func:`placement`: where a brush is (authored and live), its spin
#:   and its derived bounds. A moving brush's ``pos`` is its *authored*
#:   placement and no longer changes in play; ``original_pos``, ``_rot_angle``
#:   and ``rotation_yaw`` are no longer written. A plugin that read a mover's
#:   live position from ``brush['pos']`` reads :func:`placement` instead.
#: * 1.9.0 — the renderer contract takes published tables only. The frame
#:   input's ``terrain`` is an ``engine.terrain_frame.TerrainFrame`` (never the
#:   Terrain object); ``draw_face_highlight``, ``draw_connection_lines`` and
#:   ``draw_collision_visualization`` take vertex arrays. Only renderer
#:   implementations are affected; every other plugin loads unchanged.
#: * 2.0.0 — **1.9 extended to expose Soma, the player and LogicState.**
#:   Nothing in 1.9 is changed or removed: every plugin written for 1.x loads
#:   and behaves as it did. The surface is locked at this version
#:   (``tests/plugins/test_api_2_0.py`` pins both the whole of 1.9 and the
#:   whole of 2.0). Soma is Fio's physics engine -- a subsystem of the
#:   engine, as the Forward renderer is, not a plugin -- and what is added is
#:   how a plugin communicates with it, with the player and with map state:
#:
#:   - the physics engine: :attr:`RuntimeAPI.physics` (:attr:`RuntimeAPI.soma`
#:     when it is Soma), :meth:`RuntimeAPI.body`, :meth:`RuntimeAPI.impulse`,
#:     :meth:`RuntimeAPI.explode`, :meth:`RuntimeAPI.bodies_near`,
#:     :meth:`RuntimeAPI.bodies_in_box`, :meth:`RuntimeAPI.raycast_bodies`,
#:     :attr:`RuntimeAPI.spatial_grid`; the events in :data:`PHYSICS_EVENTS`
#:     on the bus; a physics Prop spawned in play gets a body. With physics
#:     switched off none of it fails: ``physics`` is :data:`NO_PHYSICS`, on
#:     which everything does nothing (:attr:`RuntimeAPI.physics_enabled`);
#:   - the player: :attr:`RuntimeAPI.player` and :attr:`RuntimeAPI.player2`,
#:     each a :class:`PlayerState`;
#:   - LogicState: :meth:`RuntimeAPI.state` (a typed :class:`StateStore`
#:     whose writes fire the map's outputs), :meth:`RuntimeAPI.state_stores`,
#:     and the ``state_changed`` event;
#:   - flags: :func:`set_flag` / :func:`clear_flag`, the only way to change
#:     an entity's ``hidden`` or ``disabled`` (they are journalled);
#:   - the physics contract, importable from here: :class:`Physics`,
#:     :class:`Body`, :data:`HEAVY_MASS`, :data:`CRUSH_DAMAGE`,
#:     :data:`SV_GRAVITY`, :func:`world_gravity`;
#:   - :meth:`EditorAPI.register_physics` / :meth:`EditorAPI.select_physics`:
#:     the physics engine can be swapped out the way the renderer can.
#:
#:   ``SpatialGrid`` lives in ``engine.spatial_grid`` and Soma's classes in
#:   ``engine.physics.soma``; ``engine.physics`` exports the boundary only.
API_VERSION = "2.0.0"
API_VERSION_INFO = (2, 0, 0)


def version_tuple(value: str) -> tuple:
    """Parse a dotted version string into a comparable int tuple.

    Non-numeric components degrade to ``0`` so a malformed version never raises.
    """
    parts = []
    for chunk in str(value).split("."):
        try:
            parts.append(int(chunk))
        except (TypeError, ValueError):
            parts.append(0)
    return tuple(parts) or (0,)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def placement(brush):
    """Where *brush* is: a :class:`engine.brush_placement.Placement` with

    * ``authored`` -- its authored centre ``(x, y, z)``: a box's ``pos``, an
      angled brush's planes' centre;
    * ``centre`` -- where it is now: the authored centre carried by a mover's
      or door's runtime transform (the same as ``authored`` for anything
      that is not moving);
    * ``spin`` -- ``(axis_x, axis_y, axis_z, angle_degrees)`` while a mover
      spins, else ``None``;
    * ``half`` -- the shape's unturned half-extents;
    * ``bounds`` -- the derived world AABB
      ``(lo_x, lo_y, lo_z, hi_x, hi_y, hi_z)``, a spinning brush's turned box
      included: what culling, collision and picking use.

    Since API 1.8.0 a brush's ``pos`` is its authored placement and does not
    change while a mover moves it; read a moving brush through this.
    """
    from engine.brush_placement import placement as _placement
    return _placement(brush)


def set_flag(entity, flag: str, value) -> bool:
    """Set an entity's ``hidden`` or ``disabled`` flag (API 2.0).

    **This is how a plugin changes those two flags.** Physics and the render
    projection keep their own account of which entities are hidden or
    disabled, and hear of a change through the engine's change journal and
    nowhere else -- so that neither has to look at every entity every tick.
    Writing ``entity.properties['disabled'] = True`` directly is not heard:
    the entity goes on being simulated and drawn as it was, and the physics
    engine prints a warning naming it.

    Returns True if the entity has properties to write.
    """
    return _set_live_flag(entity, flag, value)


def clear_flag(entity, flag: str) -> bool:
    """Remove an entity's ``hidden`` or ``disabled`` flag, so it reads as
    its default again (API 2.0): the journalled ``properties.pop(flag)``.
    See :func:`set_flag`."""
    return _clear_live_flag(entity, flag)


def io_def(name: str, description: str = "", param_type: str = ""):
    """Build an :class:`editor.io_system.IODef`.

    Provided as a convenience so plugins can describe inputs/outputs with plain
    tuples/args and not import ``io_system`` themselves. Returns ``None`` if the
    I/O system is unavailable (headless contexts), which the manager tolerates.
    """
    try:
        from editor.io_system import IODef
    except Exception:
        return None
    return IODef(name, description, param_type)


def _normalise_type(type_name) -> str:
    """Match editor.things.from_dict: lowercased, underscores stripped."""
    return str(type_name).replace("_", "").lower()


def _type_of(thing) -> str:
    """Normalised ``properties['type']`` of an entity, or ``''``."""
    props = getattr(thing, "properties", None)
    if isinstance(props, dict):
        return _normalise_type(props.get("type", ""))
    return ""


# -- tiny vector helpers (plain tuples; no PyGLM/NumPy, player-safe) ----------
def _xyz(p):
    return (float(p[0]), float(p[1]), float(p[2]))


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _length(a):
    return math.sqrt(_dot(a, a))


def _player_forward(player):
    """Full look direction (includes pitch), matching the engine camera.

    Mirrors the convention plugin runtimes already use: forward is
    ``(sin a·cos p, sin p, cos a·cos p)`` for yaw ``a`` and pitch ``p``.
    """
    if player is None:
        return (0.0, 0.0, 1.0)
    a = getattr(player, "angle", 0.0)
    p = getattr(player, "pitch", 0.0)
    return (math.sin(a) * math.cos(p), math.sin(p), math.cos(a) * math.cos(p))


def _player_eye(player):
    """Eye position: the player origin raised by ``camera_height`` (default 40)."""
    if player is None:
        return (0.0, 0.0, 0.0)
    return _add(_xyz(player.pos), (0.0, getattr(player, "camera_height", 40.0), 0.0))


# ---------------------------------------------------------------------------
# Key names (for TickContext.key_down / ctx.keys)
# ---------------------------------------------------------------------------
#
# The engine host fills ``TickContext.keys`` with raw Qt key codes. Letters and
# digits are their ASCII codes (``ord('W') == Qt.Key_W``), so a name→code map is
# only needed for the special keys below. ``key_down`` accepts either a raw int
# code or a friendly name, keeping plugins host-agnostic.
_SPECIAL_KEYS = {
    "space": 0x20, "shift": 0x01000020, "ctrl": 0x01000021, "control": 0x01000021,
    "alt": 0x01000023, "tab": 0x01000001, "backspace": 0x01000003,
    "return": 0x01000004, "enter": 0x01000005, "escape": 0x01000000, "esc": 0x01000000,
    "up": 0x01000013, "down": 0x01000015, "left": 0x01000012, "right": 0x01000014,
}


def key_code(name) -> Optional[int]:
    """Resolve a key *name* (or raw int code) to a Qt key code, or ``None``.

    A single character maps to its uppercase ASCII code (``'w'`` → ``0x57``);
    named special keys (``'space'``, ``'shift'``, ``'up'`` …) use the table
    above; an ``int`` passes through unchanged.
    """
    if isinstance(name, int):
        return name
    s = str(name).strip().lower()
    if not s:
        return None
    if s in _SPECIAL_KEYS:
        return _SPECIAL_KEYS[s]
    if len(s) == 1:
        return ord(s.upper())
    return None


# ---------------------------------------------------------------------------
# Property schema (typed, self-documenting entity properties)
# ---------------------------------------------------------------------------

@dataclass
class PropertySpec:
    """Declares one editable property of a plugin entity.

    A plugin can publish these (via :meth:`FioPlugin.describe_properties` or
    :meth:`EditorAPI.register_properties`) so the editor renders a fitting
    widget — an enum dropdown, a ranged spin box, a checkbox — with a label and
    tooltip, and so values can be coerced/validated. The untyped ``properties``
    dict stays the storage format; the schema is metadata layered over it.
    """

    name: str
    #: One of ``int``, ``float``, ``bool``, ``string``, ``enum``, ``vec3``, ``asset``.
    type: str = "string"
    label: str = ""
    default: Any = None
    min: Optional[float] = None
    max: Optional[float] = None
    choices: Optional[List[Any]] = None
    help: str = ""
    #: Optional section name. When an editor renders a schema it may group
    #: consecutive specs under a heading, turning a flat property list into an
    #: organised, form-like panel. Empty means "no heading".
    group: str = ""

    def apply_default(self, properties: dict) -> None:
        """Set this property's default on *properties* if it is missing."""
        if self.default is not None and self.name not in properties:
            properties[self.name] = self.default

    def coerce(self, value):
        """Best-effort cast of *value* to this spec's type, clamped to range.

        Never raises: an uncastable value falls back to the default (or a
        type-appropriate zero/empty), so a bad hand-edited map can't crash the
        editor.
        """
        t = (self.type or "string").lower()
        try:
            if t == "int":
                # int(inf) raises OverflowError (caught below with the rest).
                v = int(float(value))
                if self.min is not None:
                    v = max(int(self.min), v)
                if self.max is not None:
                    v = min(int(self.max), v)
                return v
            if t == "float":
                v = float(value)
                if self.min is not None:
                    v = max(float(self.min), v)
                if self.max is not None:
                    v = min(float(self.max), v)
                return v
            if t == "bool":
                if isinstance(value, bool):
                    return value
                return str(value).strip().lower() in ("1", "true", "yes", "on")
            if t == "enum":
                s = str(value)
                if self.choices and s not in [str(c) for c in self.choices]:
                    return self.default if self.default is not None else self.choices[0]
                return s
        except (TypeError, ValueError, OverflowError):
            if self.default is not None:
                return self.default
            return {"int": 0, "float": 0.0, "bool": False}.get(t, "")
        return value


def prop(name: str, type: str = "string", label: str = "", default: Any = None,
         min: Optional[float] = None, max: Optional[float] = None,
         choices: Optional[List[Any]] = None, help: str = "",
         group: str = "") -> PropertySpec:
    """Terse constructor for a :class:`PropertySpec` (keyword-friendly)."""
    return PropertySpec(name=name, type=type, label=label, default=default,
                        min=min, max=max, choices=choices, help=help,
                        group=group)


# ---------------------------------------------------------------------------
# LogicState: typed values, and the events a write makes (API 2.0)
def _unstorable(value) -> bool:
    """Whether *value* is a number a LogicState store refuses to hold."""
    try:
        from editor.state_values import out_of_range
    except Exception:  # pragma: no cover - a host without the editor package
        if isinstance(value, float):
            return not math.isfinite(value)
        return False
    return out_of_range(value)


# ---------------------------------------------------------------------------
class StateStore:
    """One LogicState store, by name, as the map sees it (API 2.0).

    :class:`GlobalStore` reaches the same registry but speaks strings and
    writes silently. This is the store itself: values are **typed** (an
    integer counter is an ``int``, a flag a ``bool``), and a write goes
    through the map's own ``LogicState`` entity for the store when there is
    one -- so it fires ``OnValueSet`` / ``OnValueChanged`` and the map's logic
    reacts, exactly as if an I/O connection had written it. With no such
    entity in the level the value is written to the registry directly.

    Every change to any store, whoever makes it, is also the engine event
    ``state_changed`` (``store``, ``key``, ``value``, ``entity``) on the bus.
    Reached as :meth:`RuntimeAPI.state`.
    """

    __slots__ = ('_logic', 'name', '_found', '_found_in', '_found_at')

    def __init__(self, logic, name):
        self._logic = logic
        self.name = str(name)
        self._found = None          # the store's entity, as last found
        self._found_in = None       # ... in this things list
        self._found_at = -1         # ... at this index (or its length, if none)

    @staticmethod
    def _registry() -> dict:
        try:
            from editor.things import LogicState
            return LogicState._persistent_registry
        except Exception:
            return GlobalStore._fallback

    def _entity(self):
        """The level's LogicState entity for this store, or None.

        Found once and remembered: a read or a write must not cost a walk
        over every thing in the level. What was found is checked each time
        -- the entity is still at its place in the same list and still
        names this store; or, where there was none, the list is the same
        list at the same length -- and looked for again when it is not.
        """
        things = getattr(getattr(self._logic, 'editor_state', None), 'things', None)
        if things is self._found_in and things is not None:
            found, at = self._found, self._found_at
            if found is None:
                if len(things) == at:
                    return None
            elif (at < len(things) and things[at] is found
                    and found.store_name == self.name):
                return found
        self._found, self._found_in, self._found_at = None, things, len(things or ())
        for at, thing in enumerate(things or ()):
            if getattr(thing, 'store_name', None) == self.name and hasattr(thing, 'apply_value'):
                self._found, self._found_at = thing, at
                return thing
        return None

    def _data(self) -> dict:
        entity = self._entity()
        if entity is not None:
            return getattr(entity, '_runtime_data', {})
        return self._registry().get(self.name, {})

    def get(self, key, default=None):
        """The typed value under *key*, or *default*."""
        return self._data().get(str(key), default)

    def set(self, key, value, value_type=None) -> Tuple[bool, bool]:
        """Store *value* under *key*: ``(accepted, changed)``.

        Text is typed as an I/O parameter would be (``"5"`` is an integer)
        unless *value_type* names the type; any other Python value is stored
        as it is. Refused when the key is blank, the store is full, the
        text is not of *value_type*, or the value is a number no store can
        hold: ``inf``, ``nan`` (which is never equal to itself, so it would
        report a change on every write) or an integer beyond 64 bits. Such a
        number cannot be saved with the level, so it never goes in.
        """
        entity = self._entity()
        if str(value_type or "").strip().lower() != "string" and _unstorable(value):
            if entity is not None:
                try:
                    from editor.io_handlers import report_state_write
                    report_state_write(entity, self._logic, str(key).strip(),
                                       False, False, None)
                except Exception:
                    pass
            return False, False
        if entity is None:
            key = str(key).strip()
            if not key:
                return False, False
            data = self._registry().setdefault(self.name, {})
            changed = key not in data or data[key] != value or type(data[key]) is not type(value)
            data[key] = value
            if changed:
                emit = getattr(self._logic, '_plugin_emit', None)
                if emit is not None:
                    emit('state_changed', store=self.name, key=key, value=value, entity=None)
            return True, changed
        ok, changed, stored = entity.apply_value(key, value, value_type)
        try:
            from editor.io_handlers import report_state_write
            report_state_write(entity, self._logic, str(key).strip(), ok, changed, stored)
        except Exception:
            pass
        return ok, changed

    def delete(self, key) -> bool:
        """Remove *key*. True if it was there."""
        entity = self._entity()
        if entity is not None:
            return bool(entity.clear_key(key))
        data = self._registry().get(self.name)
        if data and str(key) in data:
            del data[str(key)]
            return True
        return False

    def keys(self) -> list:
        return list(self._data().keys())

    def items(self) -> list:
        return list(self._data().items())

    def __contains__(self, key) -> bool:
        return str(key) in self._data()

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<StateStore {self.name!r} {dict(self._data())!r}>"


# ---------------------------------------------------------------------------
# Global key/value store (cross-level, shared with map LogicState entities)
# ---------------------------------------------------------------------------

class GlobalStore:
    """Process-wide, cross-level key/value storage for plugins.

    When the editor package is present this binds to the *same* persistent
    registry the map ``LogicState`` entities use, so a plugin's globals
    live alongside — and can share stores with — map state (persisting across
    level loads within a session). In the dependency-light player the editor is
    absent, so it falls back to a plain process-local dict-of-dicts with the
    same API. Values are read back as strings; a map store may hold them
    typed, and this converts on the way out rather than keeping a copy.

    Keys are grouped by *store* name (default ``"plugins"``); pass a store name
    a map's ``LogicState`` uses to read/write the exact same values.
    """

    #: Fallback registry used when the editor store is unavailable (player).
    _fallback: dict = {}

    def _registry(self) -> dict:
        try:
            from editor.things import LogicState
            return LogicState._persistent_registry
        except Exception:
            return GlobalStore._fallback

    @staticmethod
    def _as_text(value):
        """A stored value as the string this API has always returned.

        Map stores hold typed values as of 2.4 — an integer counter really is
        an ``int`` — but this API's contract is strings, and a plugin written
        against it would break on a value a map happened to set.  Converting on
        the way out keeps that contract without needing a second copy of the
        data: there is still exactly one registry.
        """
        if value is None or isinstance(value, str):
            return value
        try:
            from editor.state_values import format_value
            return format_value(value)
        except Exception:
            return str(value)

    def get(self, key, default=None, store: str = "plugins"):
        data = self._registry().get(str(store), {})
        if str(key) not in data:
            return default
        return self._as_text(data[str(key)])

    def set(self, key, value, store: str = "plugins") -> None:
        self._registry().setdefault(str(store), {})[str(key)] = str(value)

    def delete(self, key, store: str = "plugins") -> bool:
        d = self._registry().get(str(store))
        if d and str(key) in d:
            del d[str(key)]
            return True
        return False

    def all(self, store: str = "plugins") -> dict:
        return {k: self._as_text(v)
                for k, v in self._registry().get(str(store), {}).items()}

    def keys(self, store: str = "plugins") -> list:
        return list(self._registry().get(str(store), {}).keys())


# ---------------------------------------------------------------------------
# Editor-time API (passed to FioPlugin.register)
# ---------------------------------------------------------------------------

class EditorAPI:
    """Handed to :meth:`FioPlugin.register` exactly once when the plugin loads.

    This phase runs in *both* the editor process and any process that imports
    the engine, so it must be UI-free. Use it to declare entity types, their
    I/O, property schemas and editor palette entries.
    """

    def __init__(self, manager, plugin: "FioPlugin"):
        self._manager = manager
        self._plugin = plugin

    # -- entity registration ------------------------------------------------
    def register_entity(
        self,
        cls: type,
        category: Optional[str] = None,
        menu_label: Optional[str] = None,
        placeable: bool = True,
    ) -> type:
        """Register a ``Thing`` subclass as a placeable editor entity.

        * Adds ``cls`` to ``editor.things.ENTITY_TYPES`` (keyed by class name),
          so the property editor, spawner and serializer can find it.
        * Adds it to ``ENTITY_CATEGORIES`` under *category* (default: the
          plugin's :attr:`~FioPlugin.category`).
        * If *placeable*, records a right-click menu entry the editor's 2D view
          exposes under a "Plugins ▸ <plugin>" submenu.

        Returns *cls* unchanged so it can be used as a decorator.
        """
        cat = category or self._plugin.category
        label = menu_label or cls.__name__

        # Record ownership first — this has no dependencies and must work even
        # in the PyQt-free player (where the editor palette below is skipped),
        # so the package exporter and the player host can map map entity 'type'
        # strings back to the owning plugin and its class.
        self._manager._record_entity_owner(cls, self._plugin)

        # Editor palette registration (needs editor.things → PyQt). Absent in
        # the standalone player; skip quietly there.
        try:
            from editor.things import ENTITY_TYPES, ENTITY_CATEGORIES
            ENTITY_TYPES[cls.__name__] = cls
            ENTITY_CATEGORIES.setdefault(cat, [])
            if cls.__name__ not in ENTITY_CATEGORIES[cat]:
                ENTITY_CATEGORIES[cat].append(cls.__name__)
        except Exception:
            pass

        # A class may carry its own PROPERTY_SCHEMA; pick it up automatically.
        schema = getattr(cls, "PROPERTY_SCHEMA", None)
        if schema:
            try:
                self._manager._record_property_schema(
                    cls().properties.get("type", cls.__name__), schema)
            except Exception:
                # Instantiating just to read the type may fail for some classes;
                # fall back to the lowercased class name.
                self._manager._record_property_schema(cls.__name__, schema)

        if placeable:
            self._manager._add_menu_entry(self._plugin, label, cls)
        return cls

    # -- I/O registration ---------------------------------------------------
    def register_io(self, entity_type: str, inputs: List[Any], outputs: List[Any]):
        """Register input/output definitions for *entity_type*.

        *entity_type* must equal the entity's ``properties['type']`` string.
        *inputs*/*outputs* are lists of :func:`io_def` results (``IODef``);
        ``None`` entries (produced when I/O is unavailable) are filtered out.
        """
        try:
            from editor.io_system import register_io
        except Exception:
            return
        defs_in = [d for d in inputs if d is not None]
        defs_out = [d for d in outputs if d is not None]
        register_io(entity_type, defs_in, defs_out)
        # Remember the call so the manager can put it back. IO_REGISTRY is a
        # module-level singleton that something else may reset (the test
        # harness does; a future plugin reload would), and plugin registration
        # is otherwise a once-per-process side effect with no way to replay it.
        self._manager._record_io_registration(
            self._plugin, 'set', entity_type, defs_in, defs_out)

    def extend_io(self, entity_type: str, inputs=(), outputs=()):
        """Add I/O to an entity type this plugin does not own.

        A plugin that extends a *core* entity -- Tidy adding ``Reset`` and
        ``OnTidied`` to ``prop``, say -- cannot use :meth:`register_io`, which
        replaces the type's declarations outright and would strip the core
        ones. Without this the only way to do it was to reach past the plugin
        API into ``editor.io_system`` and perform the read-merge-write by hand,
        which is both easy to get wrong and invisible to the manager, so the
        registration could not be replayed.

        Definitions already declared for *entity_type* are left alone, matched
        case-insensitively by name, so this is idempotent.
        """
        try:
            from editor.io_system import get_inputs, get_outputs, register_io
        except Exception:
            # Headless/player processes do not expose the editor I/O registry.
            return

        merged_in = list(get_inputs(entity_type))
        merged_out = list(get_outputs(entity_type))
        have_in = {d.name.lower() for d in merged_in}
        have_out = {d.name.lower() for d in merged_out}

        added_in = [d for d in inputs if d is not None and d.name.lower() not in have_in]
        added_out = [d for d in outputs if d is not None and d.name.lower() not in have_out]
        merged_in.extend(added_in)
        merged_out.extend(added_out)

        register_io(entity_type, merged_in, merged_out)
        # Recorded as an *extension*, not a replacement, so replaying it
        # re-merges against whatever the registry holds at that point rather
        # than pinning the core declarations as they were at load.
        self._manager._record_io_registration(
            self._plugin, 'extend', entity_type,
            [d for d in inputs if d is not None],
            [d for d in outputs if d is not None])

    # -- property schema ----------------------------------------------------
    def register_properties(self, entity_type: str, specs: List[PropertySpec]):
        """Declare the editable-property schema for *entity_type*.

        *specs* is a list of :class:`PropertySpec` (see :func:`prop`). The editor
        uses it to render typed widgets — enum dropdowns, ranged spin boxes,
        checkboxes — with labels and tooltips, instead of guessing from the
        stored value's Python type. Optional and additive: properties without a
        spec still render generically.
        """
        self._manager._record_property_schema(entity_type, specs)

    # -- editor-UI extensions ----------------------------------------------
    def register_extra_fields(self, entity_type: str, specs):
        """Append extra editable fields to *entity_type*'s property panel.

        Works for any entity, **including built-in ones** — the fields are added
        after the stock rows, never replacing them (see
        :meth:`register_properties` for a plugin entity's whole panel).
        """
        self._manager.register_extra_fields(entity_type, specs, plugin=self._plugin)

    def register_property_tab(self, label: str, factory, entity_type=None):
        """Add a custom tab to the property panel.

        ``factory(thing)`` builds and returns the tab's widget. If *entity_type*
        is given the tab appears only for that type, otherwise for every entity.
        """
        self._manager.register_property_tab(label, factory, entity_type,
                                            plugin=self._plugin)

    def register_property_section(self, label: str, factory, entity_type=None,
                                  expanded: bool = False) -> None:
        """Add a collapsible section to the property panel's Properties tab
        (API 1.5.0).

        The same ``factory(thing) -> widget`` contract as
        :meth:`register_property_tab`; the difference is placement. A section
        sits among the entity's own properties, under a titled header that
        collapses, rather than in a tab of its own. Use it for a small editor
        that belongs with the other properties, and a tab for one that needs
        the room. *expanded* is the section's initial state; a collapsed
        section's factory does not run until the section is first opened. If
        *entity_type* is given the section appears only for that type,
        otherwise for every entity.
        """
        self._manager._record_property_section(
            self._plugin, label, factory, entity_type, expanded)

    def register_kv_suggestions(self, provider) -> None:
        """Offer ready-made keys in the LogicState editor (API 1.5.0).

        ``provider(store)`` is called when a LogicState panel is built, with
        that LogicState entity, and returns rows of
        ``(label, key, default_value)`` or ``(label, key, default_value,
        tooltip)``. The editor lists them in a "Preset key" picker; inserting
        one adds *key* with *default_value* as a designer default. Return
        ``[]`` for stores the plugin has nothing to say about (the store's
        ``store_name`` property tells them apart).
        """
        self._manager._record_kv_suggestions(self._plugin, provider)

    def register_entity_inspector(self, provider, entity_type=None) -> None:
        """Supply the live contents of the entity inspector (API 1.5.0).

        ``provider(entity, logic) -> dict | None``. *logic* is the running
        :class:`~engine.logic_thread.LogicThread`, or None outside Play Mode.
        The returned dict is an inspection document::

            {"title":    "Gate Keeper",
             "subtitle": "patrolling · awake",
             "sections": [("Vitals", [("Health", 80), ("Speed", 1.5)]),
                          ("Goals",  [("Patrol", "", 0.9), ("Rest", "", 0.2)])]}

        A row is ``(label, value)``, or ``(label, value, fraction)`` to draw a
        0..1 bar beside it. The first provider that returns a non-empty
        document for an entity is shown; with none, the inspector shows the
        entity's public properties. If *entity_type* is given the provider is
        asked only about entities of that type.
        """
        self._manager._record_entity_inspector(self._plugin, provider, entity_type)

    def register_singleton_entity(self, entity_type: str) -> None:
        """Mark *entity_type* as a per-map singleton (at most one instance).

        Placement paths refuse to add a second one and select the existing
        instance instead.
        """
        self._manager.register_singleton_entity(entity_type)

    def register_entity_wizard(self, entity_type: str, factory) -> None:
        """Register a creation wizard for *entity_type*.

        ``factory(parent) -> dict | None`` runs a dialog and returns the initial
        properties for the new entity, or None to cancel placement. Lets an
        entity that needs configuring be authored properly instead of dropping
        the user into raw properties.
        """
        self._manager.register_entity_wizard(entity_type, factory)

    def register_renderer(self, name: str, cls) -> bool:
        """Register a renderer under *name* in Fio's renderer registry.

        *cls* is a factory called as ``cls(config)`` with the viewport's GL
        context current -- usually the renderer class itself. What it returns
        must satisfy :class:`engine.renderer.Renderer`: lifecycle, the frame
        (``render_scene`` over the dense ``RenderTable``/``EntityTable`` frame
        input), resources, diagnostics and the host's post-scene drawing
        operations. How it draws -- shaders, passes, lighting, shadows, a
        G-buffer -- is entirely its own; it need not inherit anything from
        Fio, though :class:`engine.renderer.core.RendererCore` offers reusable
        infrastructure.

        An implementation of the current renderer contract (the tables-only
        one API 1.9.0 introduced; :data:`engine.renderer.RENDERER_CONTRACT_VERSION`
        1.0.0) requires ``api_version = "1.9.0"``: a plugin that registers
        one declares it, and the renderer its ``contract_version``. Renderers
        written for 1.6-1.8 (which read the ``Terrain`` object and authored
        brushes) or for the 1.3-1.5 construction are not compatible. The
        registry checks what the factory returns before the host paints with
        it (engine.renderer.registry.create_renderer).

        The renderer owns its GL resources: no GL resource id survives the
        lifetime of the renderer that owns it. The host discards every handle
        it was given before calling ``cleanup()``, and a replacement renderer
        receives fresh resource tables. See the wiki's Renderer Development
        Guide and ``docs/examples/deferred_renderer``.

        Returns True if registered (False in a headless/player context with no
        viewport). This is how a whole new renderer — e.g. a deferred one —
        ships as a plugin.
        """
        try:
            from engine.qt_game_view import QtGameView  # noqa: F401 -- the viewport that uses renderers
        except Exception:
            return False
        from engine.renderer import register_renderer
        return register_renderer(name, cls)

    def register_physics(self, name: str, cls) -> bool:
        """Register a physics engine under *name* in Fio's physics registry
        (API 2.0), so it can be swapped in the way a renderer can
        (:meth:`register_renderer`).

        Soma is the physics engine Fio ships: a subsystem of the engine, as
        the Forward renderer is, and what every map runs on unless another
        is selected. Both sit behind a contract so that either can be
        replaced without the rest of the engine knowing.

        *cls* is a factory called as ``cls(spatial_grid)`` once per play
        session -- usually the physics class itself. What it returns must
        satisfy :class:`Physics` (``engine/physics/api.py`` is the whole
        contract): the world controls, the lifecycle (``rebuild`` over the
        collision runtime's body brushes, ``clear``), the tick (``step``) and
        the per-body operations the engine's Prop, trigger and cutscene code
        call. How it simulates is entirely its own. It may start from Soma
        (``from engine.physics.soma import Soma``, its full tier, and
        subclass it) or share nothing with it.

        Two rules come with the contract. The world is the ``SpatialGrid``
        the factory is given, asked live -- a physics keeps no copy of it.
        And a moved body is handed back by assigning ``entity.pos``, which is
        what the renderer and every index hear.

        Registering makes it available; :meth:`select_physics` (the console's
        ``phys_engine``) makes it the one the next play session uses. Works
        in the editor and the standalone player alike. Returns True.
        """
        from engine.physics import register_physics
        return register_physics(name, cls)

    def select_physics(self, name: str) -> bool:
        """Make the physics engine registered as *name* the one the next play
        session is created with (API 2.0); ``"Soma"`` is the built-in one. A
        running session keeps its own. Returns False for a name nothing is
        registered under."""
        from engine.physics import select_physics
        try:
            select_physics(name)
        except KeyError:
            return False
        return True

    def register_tools_action(self, label: str, callback: Callable, tooltip: str = "") -> None:
        """Register a Tools-menu action for an editor/developer plugin."""
        self._manager._record_tools_action(self._plugin, label, callback, tooltip)

    def register_menu_action(self, label: str, callback: Callable, tooltip: str = "") -> None:
        """Register an action at the top of this plugin's editor menu."""
        self._manager._record_menu_action(self._plugin, label, callback, tooltip)

    def register_console_command(self, name: str, callback: Callable, help_text: str = "") -> None:
        """Register a plugin-owned debug console command."""
        self._manager._register_console_command(self._plugin, name, callback, help_text)

    def register_pause_menu_item(self, label: str, callback: Callable,
                                 close_menu: bool = True) -> None:
        """Add an item to the Play Mode pause menu (API 1.7.0).

        Esc in Play opens the pause menu: Resume, Save Game, Load Game and
        Options, then every enabled plugin's items in registration order, then
        Exit to Editor (Quit in a played package). Choosing this item calls
        ``callback(main_window, logic)`` on the UI thread with the world
        paused; with *close_menu* the menu then closes and play resumes,
        otherwise it stays open (for an item that opens a screen of its own;
        call ``main_window.close_pause_menu()`` when done).
        """
        self._manager._record_pause_menu_item(self._plugin, label, callback, close_menu)

    # -- global store -------------------------------------------------------
    @property
    def globals(self) -> GlobalStore:
        """The cross-level :class:`GlobalStore` (shared with map KV stores)."""
        return self._manager.global_store

    def global_get(self, key, default=None, store: str = "plugins"):
        return self._manager.global_store.get(key, default, store)

    def global_set(self, key, value, store: str = "plugins"):
        self._manager.global_store.set(key, value, store)

    def log(self, message: str):
        # Informational by default (silent unless FIO_PLUGIN_DEBUG); plugins
        # should not spam the console on a normal launch.
        self._manager._debug(f"[{self._plugin.name}] {message}")


# ---------------------------------------------------------------------------
# Runtime API (passed to FioPlugin.register_runtime)
# ---------------------------------------------------------------------------

def _like(old, xyz):
    """*xyz* as whatever type *old* is, so a write keeps the host's vector
    type (``glm.vec3`` on the desktop, a list where there is no PyGLM)."""
    x, y, z = float(xyz[0]), float(xyz[1]), float(xyz[2])
    kind = type(old)
    if kind in (list, tuple) or old is None:
        return [x, y, z]
    try:
        return kind(x, y, z)
    except TypeError:
        return kind((x, y, z))


class PlayerState:
    """The player, as a plugin reads and changes it (API 2.0).

    A live view, not a snapshot: every member reads the engine's state now
    and every assignment changes it, on the logic thread. Positions,
    velocities and directions are plain ``(x, y, z)`` tuples -- no PyGLM --
    whatever the host stores them as. Reached as :attr:`RuntimeAPI.player`.

    * where and how it moves -- :attr:`position`, :attr:`velocity`,
      :attr:`on_ground`, :attr:`ground`, :attr:`ducked`, :attr:`in_water`,
      :attr:`swimming`, :attr:`size`, :meth:`teleport`
    * where it looks -- :attr:`yaw`, :attr:`pitch`, :attr:`eye`, :attr:`forward`
    * how it is -- :attr:`health`, :attr:`max_health`, :attr:`armor`,
      :attr:`max_armor`, :attr:`dead`, :attr:`god_mode`, :attr:`notarget`,
      :meth:`damage`, :meth:`give_armor`
    * how it moves -- :attr:`speed`, :attr:`noclip`
    * what it carries -- :attr:`holding`, :meth:`drop`, :attr:`keys`,
      :meth:`has_key`, :meth:`give_key`, :meth:`take_key`, :attr:`weapons`

    The second, split-screen player is :attr:`RuntimeAPI.player2`: the same
    members, where the engine keeps them per player (position, motion, view,
    health); armour, keys, weapons and what is carried are the first
    player's.

    :attr:`object` is the engine's own ``Player`` for anything not here; what
    is here keeps its meaning when the engine's internals change.
    """

    __slots__ = ('_logic', '_second')

    def __init__(self, logic, second=False):
        self._logic = logic
        #: The second, split-screen player rather than the first.
        self._second = bool(second)

    @property
    def _runtime(self):
        return self._logic.player_runtime

    @property
    def object(self):
        """The engine's ``Player`` object, or None before play starts (or,
        for the second player, when nobody is playing split-screen)."""
        runtime = self._runtime
        return runtime.player2 if self._second else runtime.player

    @property
    def index(self) -> int:
        """0 for the first player, 1 for the second."""
        return 1 if self._second else 0

    @property
    def exists(self) -> bool:
        return self.object is not None

    # -- where it is and how it moves ---------------------------------------
    @property
    def position(self) -> Tuple[float, float, float]:
        """The centre of the player's hull, in world units."""
        player = self.object
        return _xyz(player.pos) if player is not None else (0.0, 0.0, 0.0)

    @position.setter
    def position(self, value):
        player = self.object
        if player is not None:
            player.pos = _like(player.pos, value)

    @property
    def velocity(self) -> Tuple[float, float, float]:
        """Units per second."""
        player = self.object
        return _xyz(player.velocity) if player is not None else (0.0, 0.0, 0.0)

    @velocity.setter
    def velocity(self, value):
        player = self.object
        if player is not None:
            player.velocity = _like(player.velocity, value)

    def teleport(self, position, velocity=(0.0, 0.0, 0.0)) -> None:
        """Put the player at *position*, moving at *velocity* (at rest by
        default), standing on nothing until the next tick finds the floor."""
        player = self.object
        if player is None:
            return
        self.position = position
        self.velocity = velocity
        player.on_ground = False
        player.ground_object = None

    @property
    def on_ground(self) -> bool:
        return bool(getattr(self.object, 'on_ground', False))

    @property
    def ground(self):
        """The brush the player stands on, or None."""
        return getattr(self.object, 'ground_object', None)

    @property
    def ducked(self) -> bool:
        return bool(getattr(self.object, 'ducked', False))

    @property
    def in_water(self) -> bool:
        """Wading or deeper."""
        return bool(getattr(self.object, 'in_water', False))

    @property
    def swimming(self) -> bool:
        """Deep enough that swimming has taken over from walking."""
        return bool(getattr(self.object, 'swimming', False))

    @property
    def size(self) -> Tuple[float, float, float]:
        """The hull's full ``(width, height, depth)``, as it is now (a ducked
        player is shorter)."""
        player = self.object
        if player is None:
            return (0.0, 0.0, 0.0)
        return (float(player.width), float(player.height), float(player.depth))

    # -- where it looks -----------------------------------------------------
    @property
    def yaw(self) -> float:
        """Heading in radians; :attr:`forward` is ``(sin yaw, ., cos yaw)``."""
        return float(getattr(self.object, 'angle', 0.0))

    @yaw.setter
    def yaw(self, value):
        if self.object is not None:
            self.object.angle = float(value)

    @property
    def pitch(self) -> float:
        """Radians above the horizon."""
        return float(getattr(self.object, 'pitch', 0.0))

    @pitch.setter
    def pitch(self, value):
        if self.object is not None:
            self.object.pitch = float(value)

    @property
    def eye(self) -> Tuple[float, float, float]:
        """Where the camera is: the position raised by the eye height."""
        return _player_eye(self.object)

    @property
    def forward(self) -> Tuple[float, float, float]:
        """The look direction, pitch included."""
        return _player_forward(self.object)

    # -- how it moves ---------------------------------------------------------
    @property
    def speed(self) -> float:
        """Walking speed, in units per second."""
        return float(getattr(self.object, 'speed', 0.0))

    @speed.setter
    def speed(self, value):
        if self.object is not None:
            self.object.speed = max(0.0, float(value))

    @property
    def noclip(self) -> bool:
        """Whether the player's own physics is off: it flies, passes through
        the world, and neither pushes nor is pushed by physics bodies."""
        player = self.object
        return player is not None and not getattr(player, 'physics_enabled', True)

    @noclip.setter
    def noclip(self, value):
        player = self.object
        if player is not None:
            player.physics_enabled = not bool(value)
            if value:
                player.on_ground = False
                player.ground_object = None

    # -- what it carries ------------------------------------------------------
    @property
    def holding(self):
        """The Prop the first player is carrying, or None."""
        if self._second:
            return None
        return getattr(self._logic.prop_runtime, 'held', None)

    def drop(self) -> bool:
        """Make the player put down what it is carrying, on the next tick.
        True if it was carrying something."""
        held = self.holding
        if held is None:
            return False
        held.properties['_drop_requested'] = True
        return True

    @property
    def keys(self) -> frozenset:
        """The names of the keys the player has collected."""
        return frozenset(self._runtime.collected_keys)

    def has_key(self, name) -> bool:
        return str(name) in self._runtime.collected_keys

    def give_key(self, name) -> None:
        self._runtime.collected_keys.add(str(name))

    def take_key(self, name) -> bool:
        """Take a key away. True if the player had it."""
        keys = self._runtime.collected_keys
        if str(name) in keys:
            keys.discard(str(name))
            return True
        return False

    @property
    def weapons(self) -> frozenset:
        """The weapons the player holds, by item id."""
        return frozenset(getattr(self._logic.combat_runtime, 'weapons', ()))

    @property
    def notarget(self) -> bool:
        """Whether monsters ignore the player."""
        return bool(self._runtime.notarget)

    @notarget.setter
    def notarget(self, value):
        self._runtime.notarget = bool(value)

    # -- how it is ------------------------------------------------------------
    @property
    def health(self) -> int:
        runtime = self._runtime
        return runtime.player2_health if self._second else runtime.player_health

    @health.setter
    def health(self, value):
        runtime = self._runtime
        if self._second:
            runtime.player2_health = max(0, min(int(value), int(runtime.player2_max_health)))
        else:
            runtime.player_health = max(0, min(int(value), int(runtime.player_max_health)))

    @property
    def max_health(self) -> int:
        runtime = self._runtime
        return runtime.player2_max_health if self._second else runtime.player_max_health

    @max_health.setter
    def max_health(self, value):
        if self._second:
            self._runtime.player2_max_health = max(1, int(value))
        else:
            self._runtime.player_max_health = max(1, int(value))

    @property
    def armor(self) -> int:
        return self._runtime.player_armor

    @armor.setter
    def armor(self, value):
        runtime = self._runtime
        runtime.player_armor = max(0, min(int(value), int(runtime.player_max_armor)))

    @property
    def max_armor(self) -> int:
        return self._runtime.player_max_armor

    @property
    def dead(self) -> bool:
        runtime = self._runtime
        return bool(runtime.player2_dead if self._second else runtime.player_dead)

    @property
    def god_mode(self) -> bool:
        return bool(self._runtime.god_mode)

    @god_mode.setter
    def god_mode(self, value):
        self._runtime.god_mode = bool(value)

    def damage(self, amount, source=None) -> None:
        """Hurt the player by *amount*, exactly as the world does: armour
        takes it first, god mode stops it, and dying is the engine's to
        handle. *source* is an optional ``(x, y, z)`` the hit came from, for
        the damage indicator."""
        runtime = self._runtime
        if source is not None:
            runtime.damage_source = _xyz(source)
        self._logic.trigger_runtime._apply_player_damage(amount)

    def give_armor(self, amount) -> None:
        self._runtime.give_armor(amount)

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (f"<PlayerState pos={self.position} health={self.health} "
                f"armor={self.armor}>")


class RuntimeAPI:
    """Handed to :meth:`FioPlugin.register_runtime` when a logic thread starts.

    Beyond registering I/O *input handlers*, this exposes a small **services**
    layer over the play session's ``logic`` object so plugins don't each
    re-implement crosshair ray-casting, entity queries and vector math:

    * scene queries — :meth:`entities_of_type`, :meth:`things_near`,
      :meth:`raycast_from_crosshair`
    * the player — :attr:`player` and :attr:`player2` (a :class:`PlayerState`
      each), and the older :meth:`player_eye`, :meth:`player_forward`
    * LogicState — :meth:`state` (a typed :class:`StateStore`),
      :meth:`state_stores`
    * the physics engine — :attr:`physics` (:attr:`soma`, when it is Soma;
      never None: see :attr:`physics_enabled`), :meth:`body`, :meth:`impulse`, :meth:`explode`, :meth:`bodies_near`,
      :meth:`bodies_in_box`, :meth:`raycast_bodies`, :attr:`spatial_grid`
    * runtime spawn/despawn — :meth:`spawn`, :meth:`despawn`
    * cross-level storage — :meth:`global_get` / :meth:`global_set`
    """

    def __init__(self, manager, logic, plugin: "FioPlugin"):
        self._manager = manager
        self._plugin = plugin
        self.logic = logic
        self.io_manager = logic.io_manager
        # Views, made once: they read the engine live.
        self._player = PlayerState(logic)
        self._player2 = PlayerState(logic, second=True)

    # -- I/O ----------------------------------------------------------------
    def register_input_handler(self, entity_type: str, input_name: str, handler: Callable):
        """Register ``handler(entity, parameter, logic)`` for an entity input.

        The handler is registered once, when the logic thread attaches, but is
        gated by the plugin's *live* ``enabled`` state: it runs only while the
        plugin is on. That lets the manager attach every loaded plugin up front
        (so a plugin enabled later — e.g. a disabled-by-default one auto-enabled
        when its level loads — has working inputs immediately) while a disabled
        plugin's inputs stay inert without needing to re-attach.

        Registering an input the engine (or another plugin) already handles
        overrides it only while this plugin is enabled: while it is disabled
        the input is delivered to the handler it replaced (or, for one of the
        generic inputs on a type the plugin does not own, to the generic
        handling), exactly as if the plugin were not installed. Two plugins claiming the same input is
        logged once; the one registered last wins while it is enabled.
        """
        if self.io_manager is None:
            return
        plugin = self._plugin
        io_manager = self.io_manager
        key = (str(entity_type).lower(), str(input_name).lower())

        # The registry holds one handler per (type, input), so registering
        # replaces whatever was there: a core handler, another plugin's, or
        # nothing (the generic inputs).  That replacement must only be felt
        # while this plugin is on, so the wrapper keeps what it displaced and
        # hands the input back to it while the plugin is disabled.
        previous = getattr(io_manager, "_input_handlers", {}).get(key)
        # The same plugin registering the key again replaces its own handler
        # rather than stacking on it: "the last registration wins".
        while (previous is not None
               and getattr(previous, "_fio_plugin", None) is plugin):
            previous = getattr(previous, "_fio_previous", None)
        other = getattr(previous, "_fio_plugin", None)
        if other is not None:
            collisions = getattr(self._manager, "_input_collisions", None)
            if collisions is None:
                collisions = set()
                try:
                    self._manager._input_collisions = collisions
                except Exception:
                    pass
            # Named by plugin, not by object, so a logic thread rebuilt for a
            # new session does not report the same collision again.
            mark = (key, getattr(other, "name", ""), getattr(plugin, "name", ""))
            if mark not in collisions:
                collisions.add(mark)
                try:
                    self._manager._log(
                        "Plugins '%s' and '%s' both handle the input %s.%s; "
                        "'%s' registered last and wins while it is enabled."
                        % (getattr(other, "name", "?"),
                           getattr(plugin, "name", "?"),
                           entity_type, input_name,
                           getattr(plugin, "name", "?")))
                except Exception:
                    pass

        manager = self._manager

        def owns_type():
            try:
                owner = manager._entity_owner.get(
                    manager._normalise_type(entity_type))
            except Exception:
                return True
            return owner is plugin

        def gated(entity, parameter, logic):
            if getattr(plugin, "enabled", True):
                return handler(entity, parameter, logic)
            if previous is not None:
                return previous(entity, parameter, logic)
            # Nothing was displaced. On the plugin's own entity types its
            # inputs stay inert, as documented. On anyone else's type, an
            # input every entity accepts (Enable, Hide, ...) is the engine's
            # and must keep working.
            if (key[1] in getattr(io_manager, "GENERIC_INPUTS", ())
                    and not owns_type()):
                return io_manager._try_generic_input(entity, input_name, parameter)

        gated._fio_plugin = plugin
        gated._fio_previous = previous
        io_manager.register_input_handler(entity_type, input_name, gated)

    def fire_output(self, entity, output_name: str, value: Optional[str] = None):
        """Fire an output from *entity* through the I/O system (if available)."""
        if self.io_manager is not None:
            self.io_manager.fire_output(entity, output_name, value)

    # -- scene queries ------------------------------------------------------
    def _things(self):
        return self.logic.editor_state.things

    def entities_of_type(self, type_name: str) -> List:
        """All scene entities whose ``type`` matches *type_name*."""
        norm = _normalise_type(type_name)
        return [t for t in self._things() if _type_of(t) == norm]

    def things_near(self, pos, radius: float, type_name: Optional[str] = None) -> List:
        """Entities within *radius* of *pos* (optionally filtered by type)."""
        px, py, pz = _xyz(pos)
        r2 = float(radius) * float(radius)
        norm = _normalise_type(type_name) if type_name else None
        out = []
        for t in self._things():
            if norm is not None and _type_of(t) != norm:
                continue
            tp = getattr(t, "pos", None)
            if tp is None:
                continue
            dx, dy, dz = float(tp[0]) - px, float(tp[1]) - py, float(tp[2]) - pz
            if dx * dx + dy * dy + dz * dz <= r2:
                out.append(t)
        return out

    # -- the player (API 2.0) -------------------------------------------------
    @property
    def player(self) -> PlayerState:
        """The player's state, live: see :class:`PlayerState`."""
        return self._player

    @property
    def player2(self) -> PlayerState:
        """The second, split-screen player's state; its ``exists`` is False
        when nobody is playing split-screen."""
        return self._player2

    # -- LogicState (API 2.0) -------------------------------------------------
    def state(self, store: str) -> "StateStore":
        """The LogicState store named *store*: typed values, and writes the
        map's logic hears. See :class:`StateStore`."""
        return StateStore(self.logic, store)

    def state_stores(self) -> list:
        """The names of every LogicState store that holds anything."""
        return sorted(StateStore._registry().keys())

    # -- the physics engine (API 2.0) -----------------------------------------
    @property
    def physics(self) -> Physics:
        """The physics engine of this play session -- Soma, unless another
        was registered and selected.

        This is how a plugin communicates with it. Read or set the world
        controls (``gravity``, ``time_scale``, ``friction_scale``,
        ``damping_scale``, ``sleep_enabled``); ``wake`` a body at a velocity;
        take one out of the simulation with ``set_kinematic`` and hand it
        back; tell it you moved one with ``sync_entity_position``; ask to be
        told when one comes to rest. Its members are the :class:`Physics`
        contract, whichever engine it is.

        **It is never None.** With physics switched off (Settings > Enable
        physics), or outside play, this is :data:`NO_PHYSICS`: every member
        is there and does nothing, quietly -- a query finds no bodies, a push
        pushes nothing, a control is accepted and forgotten -- so a plugin
        need not ask first, and cannot fail for want of asking. It is falsy
        (``if api.physics:``), and :attr:`physics_enabled` says the same.

        Fetch it when you use it rather than keeping it: each play session
        has its own.
        """
        physics = getattr(self.logic.session_runtime, 'physics_world', None)
        return physics if physics is not None else NO_PHYSICS

    @property
    def physics_enabled(self) -> bool:
        """Whether a physics engine is running: False with physics switched
        off, and outside play."""
        return getattr(self.logic.session_runtime, 'physics_world', None) is not None

    @property
    def soma(self) -> Physics:
        """:attr:`physics` when the session's physics engine is Soma, for
        what is Soma's own rather than the contract's (``topple``,
        ``stand``); otherwise :data:`NO_PHYSICS`, on which those too do
        nothing."""
        physics = self.physics
        if physics and type(physics).__module__.startswith('engine.physics.soma'):
            return physics
        return NO_PHYSICS

    def impulse(self, entity, impulse) -> bool:
        """Push a physics Prop: its velocity changes by ``impulse / mass``,
        so the same push moves a heavy one less, and it wakes. False if it
        has no body, is being carried, or play is not running."""
        return bool(self.physics.impulse(entity, impulse))

    def explode(self, centre, radius: float, strength: float) -> list:
        """Push every physics Prop within *radius* of *centre* away from it,
        by an impulse of *strength* at the centre falling to nothing at the
        radius. Returns the entities pushed."""
        return list(self.physics.impulse_radial(centre, radius, strength))

    def bodies_near(self, point, radius: float) -> list:
        """The physics Props whose body is within *radius* of *point*, found
        through the physics engine's own index rather than by scanning every
        entity."""
        return list(self.physics.bodies_near(point, radius))

    def bodies_in_box(self, lo, hi) -> list:
        """The physics Props whose body overlaps the box ``lo..hi``."""
        return list(self.physics.bodies_in_box(lo, hi))

    def raycast_bodies(self, origin, direction, reach: float = 4096.0):
        """The first physics Prop a ray meets within *reach*:
        ``(entity, distance)``, or None. Bodies only -- for the world, ask
        :attr:`spatial_grid`."""
        return self.physics.raycast(origin, direction, reach)

    def body(self, entity) -> Optional[Body]:
        """The simulated :class:`Body` of *entity* (a physics Prop), or None
        if it has none or play is not running. A live handle: ``velocity``,
        ``mass``, ``half_extents``, ``awake``, ``kinematic``."""
        return self.physics.get_body(entity)

    @property
    def spatial_grid(self):
        """The session's :class:`engine.spatial_grid.SpatialGrid`: where the
        world is -- solid brushes, movers and doors where they are now,
        water -- or None outside play. The same grid the physics asks."""
        return getattr(self.logic.session_runtime, 'spatial_grid', None)

    def player_eye(self):
        """The player's eye position as an ``(x, y, z)`` tuple."""
        return _player_eye(self.logic.player_runtime.player)

    def player_forward(self):
        """The player's full look direction (with pitch) as a unit-ish tuple."""
        return _player_forward(self.logic.player_runtime.player)

    def raycast_from_crosshair(self, reach: float = 160.0, aim_dot: float = 0.86,
                               type_name: Optional[str] = None,
                               predicate: Optional[Callable] = None):
        """The nearest entity under the crosshair, or ``None``.

        Considers entities within *reach* whose direction from the eye is within
        *aim_dot* of the look vector (``1.0`` = dead-centre, lower = wider cone).
        Optionally filter by *type_name* and/or an arbitrary *predicate(entity)*.
        This is the query most interaction plugins need — it replaces the
        hand-rolled "what am I looking at" loop.
        """
        player = self.logic.player_runtime.player
        if player is None:
            return None
        eye = _player_eye(player)
        fwd = _player_forward(player)
        norm = _normalise_type(type_name) if type_name else None
        best = None
        best_d = float(reach)
        for t in self._things():
            if norm is not None and _type_of(t) != norm:
                continue
            if predicate is not None and not predicate(t):
                continue
            tp = getattr(t, "pos", None)
            if tp is None:
                continue
            to = _sub(_xyz(tp), eye)
            dist = _length(to)
            if dist > float(reach) or dist < 1e-3:
                continue
            if _dot(fwd, _scale(to, 1.0 / dist)) < float(aim_dot):
                continue
            if dist < best_d:
                best_d = dist
                best = t
        return best

    # -- runtime spawn / despawn -------------------------------------------
    def spawn(self, cls, pos=None, properties: Optional[dict] = None):
        """Instantiate *cls* and add it to the live scene; returns the entity.

        The new entity is appended to ``logic.editor_state.things`` so gameplay queries
        and rendering consume the same authoritative world. Returns
        ``None`` if construction fails.
        """
        try:
            ent = cls(pos=list(pos) if pos is not None else [0, 0, 0],
                      properties=dict(properties or {}))
        except Exception:
            self._manager._log(f"spawn() failed for '{self._plugin.name}'")
            return None
        self.logic.editor_state.things.append(ent)
        self._entities_changed()
        try:
            self._manager.emit("entity_spawned", logic=self.logic, entity=ent,
                               by=self._plugin.name)
        except Exception:
            pass
        return ent

    def despawn(self, entity) -> bool:
        """Remove *entity* from the live scene. Returns True if it was present."""
        try:
            self.logic.editor_state.things.remove(entity)
        except ValueError:
            return False
        self._entities_changed()
        return True

    def _entities_changed(self):
        """Tell a running session its thing list changed.

        The logic thread indexes entities (monsters for the AI, names and ids
        for I/O) when play starts; without a rebuild a spawned monster had no
        AI and a despawned one kept being simulated -- and shooting -- from
        the stale index. Outside play there is no index to rebuild.
        """
        logic = self.logic
        runtime = logic.world_runtime
        if runtime is None or not logic.session_runtime.play_mode:
            return
        lock = logic._tick_lock

        def rebuild():
            runtime.build_entity_caches()
            # A physics Prop that was spawned gets its body, and one that was
            # despawned loses it; the bodies already there carry on.
            sync = getattr(logic.collision_runtime, 'sync_bodies', None)
            if sync is not None:
                sync()

        if lock is None:
            rebuild()
        else:
            with lock:
                rebuild()

    # -- global store -------------------------------------------------------
    @property
    def globals(self) -> GlobalStore:
        """The cross-level :class:`GlobalStore` (shared with map KV stores)."""
        return self._manager.global_store

    def global_get(self, key, default=None, store: str = "plugins"):
        return self._manager.global_store.get(key, default, store)

    def global_set(self, key, value, store: str = "plugins"):
        self._manager.global_store.set(key, value, store)

    def log(self, message: str):
        self._manager._debug(f"[{self._plugin.name}] {message}")


# ---------------------------------------------------------------------------
# HUD service (structured, timed on-screen text)
# ---------------------------------------------------------------------------

class _HudState:
    """Per-session HUD state carried on the ``logic`` object as ``_plugin_hud``."""

    __slots__ = ("toast_text", "toast_expiry")

    def __init__(self):
        self.toast_text = ""
        self.toast_expiry = 0.0


def _hud_state(logic) -> _HudState:
    st = getattr(logic, "_plugin_hud", None)
    if st is None:
        st = _HudState()
        try:
            logic._plugin_hud = st
        except Exception:
            pass
    return st


# ---------------------------------------------------------------------------
# Per-tick context
# ---------------------------------------------------------------------------

@dataclass
class TickContext:
    """State passed to :meth:`FioPlugin.on_tick`, once per play-mode tick.

    ``use_pressed`` is the edge-triggered "use/interact" key for this tick
    (already consumed by the logic thread), so a plugin can treat a ``True``
    value as a single press. ``keys`` is the raw held-key set (Qt key codes on
    the engine host); prefer :meth:`key_down` to test it by name.

    HUD helpers replace poking ``logic.interaction_runtime.current_hud_message`` directly:
    :meth:`set_prompt` writes a contextual line only if nothing higher-priority
    (including the core's own prompt) already claimed it this tick, and
    :meth:`toast` shows a timed message for a few seconds.
    """
    delta: float = 0.0
    use_pressed: bool = False
    keys: frozenset = field(default_factory=frozenset)
    # True if the core interaction code already set a HUD prompt / consumed the
    # use press this tick (door, pickup, level-changer). Plugins should avoid
    # clobbering it unless they own a target under the crosshair.
    interaction_consumed: bool = False
    #: The play session's logic object (set by the manager). Lets the HUD
    #: helpers reach ``current_hud_message`` without the plugin passing it.
    logic: Any = None

    _prompt_priority: int = field(default=-1, repr=False)
    _prompt_set: bool = field(default=False, repr=False)

    # -- input --------------------------------------------------------------
    def key_down(self, name) -> bool:
        """True if *name* (a key name like ``'w'``/``'space'`` or a raw code) is held."""
        code = key_code(name)
        return code is not None and code in self.keys

    # -- HUD ----------------------------------------------------------------
    def set_prompt(self, text: str, priority: int = 0) -> bool:
        """Show a contextual HUD line this tick, respecting priority.

        Does nothing (returns ``False``) if the core already claimed the line
        (``interaction_consumed``) and *priority* isn't above it, or if another
        plugin already set a higher-priority prompt this tick. This removes the
        manual "don't clobber the HUD" dance.
        """
        if self.logic is None:
            return False
        if self.interaction_consumed and priority <= 0:
            return False
        if priority < self._prompt_priority:
            return False
        try:
            self.logic.interaction_runtime.current_hud_message = str(text)
        except Exception:
            return False
        self._prompt_priority = priority
        self._prompt_set = True
        return True

    def toast(self, text: str, seconds: float = 2.0) -> None:
        """Show *text* for *seconds*, as long as no prompt claims the line.

        The message persists across ticks until it expires, so a plugin can fire
        it once (e.g. on an event) instead of re-setting it every frame.
        """
        if self.logic is None:
            return
        st = _hud_state(self.logic)
        st.toast_text = str(text)
        st.toast_expiry = _time.monotonic() + max(0.0, float(seconds))

    def _finalize_hud(self) -> None:
        """Apply any live toast to the HUD if nothing else claimed the line.

        Called by the manager after every plugin's ``on_tick`` has run.
        """
        if self.logic is None or self._prompt_set or self.interaction_consumed:
            return
        st = getattr(self.logic, "_plugin_hud", None)
        if st is None or not st.toast_text:
            return
        if _time.monotonic() >= st.toast_expiry:
            st.toast_text = ""
            return
        try:
            if not self.logic.interaction_runtime.current_hud_message:
                self.logic.interaction_runtime.current_hud_message = st.toast_text
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Plugin base class
# ---------------------------------------------------------------------------

class _OwnList:
    """Class-attribute default of ``[]`` that is never one shared list.

    ``requires: List[str] = []`` on the base class was a single list object
    every plugin inherited, so ``self.requires.append("x")`` in one plugin
    silently gave *every* plugin that requirement. Read through this
    descriptor, each plugin instance gets (and keeps) its own empty list, and
    the class itself reads as a fresh ``[]``. A plugin that declares
    ``requires = [...]`` on its class or instance overrides it as before.
    """

    def __set_name__(self, owner, name):
        self._name = name

    def __get__(self, instance, owner=None):
        if instance is None:
            return []
        own = []
        try:
            instance.__dict__[self._name] = own
        except Exception:
            pass
        return own


class FioPlugin:
    """Base class for all Fio plugins.

    Subclass this, set the metadata attributes, and override the lifecycle
    methods you need. Expose an instance as the module-level ``PLUGIN`` of your
    package's ``__init__`` (or a ``get_plugin()`` factory) so the manager can
    find it.

    Lifecycle (each method is called by the manager and fully guarded)::

        register(EditorAPI)          once, at load — editor *and* engine, UI-free
        describe_properties()        once, right after register (optional schemas)
          … a play session starts …
        register_runtime(RuntimeAPI) once per session — I/O handlers, services
        connect(PluginHost)          once per session — events, services, wraps
        on_play_start(logic)         entering play mode
        on_tick(logic, TickContext)  every tick, after core gameplay
        on_play_stop(logic)          leaving play mode — restore what you mutated

    ``register_runtime`` and ``connect`` run for *every* loaded plugin, so a
    plugin enabled mid-session already has working inputs and event hooks; those
    hooks self-gate on the live :attr:`enabled` flag. A disabled-by-default
    plugin (``enabled = False``) is auto-enabled by the manager when a level
    referencing its entities loads. See ``plugins/API.md`` for full signatures.
    """

    #: Short unique identifier (used in logs and the menu).
    name: str = "unnamed"
    #: Human-readable version string.
    version: str = "0.0.0"
    #: One-line description shown in tooling.
    description: str = ""
    #: Default editor category / menu grouping for this plugin's entities.
    category: str = "Plugins"
    #: Whether the plugin is active. Toggled from the editor's Plugins menu;
    #: the manager skips a disabled plugin's runtime attach and lifecycle/tick
    #: dispatch, so it becomes inert without being unloaded. A plugin may set
    #: this to ``False`` to ship disabled-by-default; the manager then
    #: auto-enables it when a level referencing its entities is loaded (see
    #: :meth:`PluginManager.auto_enable_for_map`).
    enabled: bool = True

    #: Minimum plugin-API version this plugin needs. The manager refuses to load
    #: a plugin whose ``api_version`` is newer than the host's :data:`API_VERSION`,
    #: with a clear message, instead of failing later inside a hook. Existing
    #: plugins that don't set this default to ``"1.0.0"`` and always load.
    api_version: str = "1.0.0"
    #: Names (or package names) of other plugins this one depends on. If a
    #: requirement isn't loaded, the manager disables this plugin and logs why.
    requires: List[str] = _OwnList()

    # -- load-time (editor + engine) ---------------------------------------
    def register(self, api: EditorAPI) -> None:
        """Register entities, I/O definitions and palette entries.

        Runs once, UI-free, in every process that loads plugins. Do not touch
        Qt/OpenGL here.
        """

    def describe_properties(self) -> dict:
        """Optional property schemas, as ``{entity_type: [PropertySpec, ...]}``.

        Collected by the manager right after :meth:`register`. An alternative to
        calling :meth:`EditorAPI.register_properties` per type; return an empty
        dict (the default) to declare none.
        """
        return {}

    # -- runtime attach (per logic thread) ---------------------------------
    def register_runtime(self, api: RuntimeAPI) -> None:
        """Register I/O input handlers against a play session's IOManager."""

    def connect(self, host) -> None:
        """Connect to the live engine through the :class:`~plugins.host.PluginHost`.

        Called once, alongside :meth:`register_runtime`, when a session's logic
        thread is built — for *every* loaded plugin, so a plugin enabled later is
        already wired in (its event hooks self-gate on ``enabled``). This is the
        open-ended extension point: subscribe to engine events with
        ``host.on("player_damage", ...)``, publish a service with
        ``host.provide(...)``, reach any subsystem via ``host.get(...)``, or
        emit your own events for other plugins. See :class:`plugins.host.PluginHost`.
        """

    # -- play lifecycle -----------------------------------------------------
    def map_uses_plugin(self, map_data: dict) -> bool:
        """Return whether *map_data* needs this plugin to be active.

        This is an optional activation hook for plugins whose runtime behaviour
        attaches to core entities rather than defining a bespoke entity type.
        """

    def on_play_start(self, logic) -> None:
        """Called when the user enters play mode. Initialise per-session state."""

    def on_play_stop(self, logic) -> None:
        """Called when leaving play mode. Restore any edited entity state."""

    def on_tick(self, logic, ctx: TickContext) -> None:
        """Called every play-mode tick after core gameplay handling."""

    # -- enable/disable notification ---------------------------------------
    def on_enabled_changed(self, enabled: bool) -> None:
        """Called when this plugin is toggled on/off (menu or level auto-enable).

        Override to acquire/release resources or reset state. Not called for the
        plugin's initial state, only on a change.
        """

    # -- optional editor menu customisation --------------------------------
    def menu_entries(self) -> List[Tuple[str, type]]:
        """Extra ``(label, ThingClass)`` placement entries.

        Most plugins rely on :meth:`EditorAPI.register_entity` instead; override
        only for bespoke menu layouts. Returning an empty list is fine.
        """
        return []

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<FioPlugin {self.name} v{self.version}>"