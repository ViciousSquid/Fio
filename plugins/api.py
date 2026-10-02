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
:class:`RuntimeAPI`         Per-session: I/O handlers, scene queries, spawn, raycast.
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
from typing import Any, Callable, List, Optional, Tuple, Type


#: Version of the plugin API surface this module implements. Compare against
#: :attr:`FioPlugin.api_version` (see :func:`version_tuple`).
#:
#: * 1.2.0 — the open-ended extension surface: :class:`plugins.host.PluginHost`,
#:   the engine event bus, and the ``connect(host)`` hook.
#: * 1.3.0 — render hooks (``render.*`` events), swappable-renderer registration
#:   (``register_renderer``), editor-UI extensions (extra property fields on any
#:   entity, custom property tabs), and the ``FIO_NO_PLUGINS`` kill-switch.
API_VERSION = "1.3.0"
API_VERSION_INFO = (1, 3, 0)


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
        except (TypeError, ValueError):
            if self.default is not None:
                return self.default
            return {"int": 0, "float": 0.0, "bool": False}.get(t, "")
        return value


def prop(name: str, type: str = "string", label: str = "", default: Any = None,
         min: Optional[float] = None, max: Optional[float] = None,
         choices: Optional[List[Any]] = None, help: str = "") -> PropertySpec:
    """Terse constructor for a :class:`PropertySpec` (keyword-friendly)."""
    return PropertySpec(name=name, type=type, label=label, default=default,
                        min=min, max=max, choices=choices, help=help)


# ---------------------------------------------------------------------------
# Global key/value store (cross-level, shared with map LogicKeyValueStores)
# ---------------------------------------------------------------------------

class GlobalStore:
    """Process-wide, cross-level key/value storage for plugins.

    When the editor package is present this binds to the *same* persistent
    registry the map ``LogicKeyValueStore`` entities use, so a plugin's globals
    live alongside — and can share stores with — map state (persisting across
    level loads within a session). In the dependency-light player the editor is
    absent, so it falls back to a plain process-local dict-of-dicts with the
    same API. Values are stored as strings, matching the map store.

    Keys are grouped by *store* name (default ``"plugins"``); pass a store name
    a map's ``LogicKeyValueStore`` uses to read/write the exact same values.
    """

    #: Fallback registry used when the editor store is unavailable (player).
    _fallback: dict = {}

    def _registry(self) -> dict:
        try:
            from editor.things import LogicKeyValueStore
            return LogicKeyValueStore._persistent_registry
        except Exception:
            return GlobalStore._fallback

    def get(self, key, default=None, store: str = "plugins"):
        return self._registry().get(str(store), {}).get(str(key), default)

    def set(self, key, value, store: str = "plugins") -> None:
        self._registry().setdefault(str(store), {})[str(key)] = str(value)

    def delete(self, key, store: str = "plugins") -> bool:
        d = self._registry().get(str(store))
        if d and str(key) in d:
            del d[str(key)]
            return True
        return False

    def all(self, store: str = "plugins") -> dict:
        return dict(self._registry().get(str(store), {}))

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
        register_io(
            entity_type,
            [d for d in inputs if d is not None],
            [d for d in outputs if d is not None],
        )

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
        self._manager.register_extra_fields(entity_type, specs)

    def register_property_tab(self, label: str, factory, entity_type=None):
        """Add a custom tab to the property panel.

        ``factory(thing)`` builds and returns the tab's widget. If *entity_type*
        is given the tab appears only for that type, otherwise for every entity.
        """
        self._manager.register_property_tab(label, factory, entity_type)

    def register_renderer(self, name: str, cls) -> bool:
        """Register a swappable renderer class under *name*.

        Fio's viewport already selects its renderer from a class registry; this
        drops *cls* in so it appears as a render mode and can be activated. *cls*
        must implement the renderer interface (``render_scene``, ``draw_models``,
        ``cleanup``, a ``lod_manager``, …). Returns True if registered (False in
        a headless/player context with no viewport). This is how a whole new
        renderer — e.g. a deferred one — ships as a plugin.
        """
        try:
            from engine.qt_game_view import register_renderer
        except Exception:
            return False
        return register_renderer(name, cls)

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

class RuntimeAPI:
    """Handed to :meth:`FioPlugin.register_runtime` when a logic thread starts.

    Beyond registering I/O *input handlers*, this exposes a small **services**
    layer over the play session's ``logic`` object so plugins don't each
    re-implement crosshair ray-casting, entity queries and vector math:

    * scene queries — :meth:`entities_of_type`, :meth:`things_near`,
      :meth:`raycast_from_crosshair`
    * player geometry — :meth:`player_eye`, :meth:`player_forward`
    * runtime spawn/despawn — :meth:`spawn`, :meth:`despawn`
    * cross-level storage — :meth:`global_get` / :meth:`global_set`
    """

    def __init__(self, manager, logic, plugin: "FioPlugin"):
        self._manager = manager
        self._plugin = plugin
        self.logic = logic
        self.io_manager = getattr(logic, "io_manager", None)

    # -- I/O ----------------------------------------------------------------
    def register_input_handler(self, entity_type: str, input_name: str, handler: Callable):
        """Register ``handler(entity, parameter, logic)`` for an entity input.

        The handler is registered once, when the logic thread attaches, but is
        gated by the plugin's *live* ``enabled`` state: it runs only while the
        plugin is on. That lets the manager attach every loaded plugin up front
        (so a plugin enabled later — e.g. a disabled-by-default one auto-enabled
        when its level loads — has working inputs immediately) while a disabled
        plugin's inputs stay inert without needing to re-attach.
        """
        if self.io_manager is None:
            return
        plugin = self._plugin

        def gated(entity, parameter, logic):
            if getattr(plugin, "enabled", True):
                return handler(entity, parameter, logic)

        self.io_manager.register_input_handler(entity_type, input_name, gated)

    def fire_output(self, entity, output_name: str, value: Optional[str] = None):
        """Fire an output from *entity* through the I/O system (if available)."""
        if self.io_manager is not None:
            self.io_manager.fire_output(entity, output_name, value)

    # -- scene queries ------------------------------------------------------
    def _things(self):
        return getattr(self.logic, "things", None) or []

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

    def player_eye(self):
        """The player's eye position as an ``(x, y, z)`` tuple."""
        return _player_eye(getattr(self.logic, "player", None))

    def player_forward(self):
        """The player's full look direction (with pitch) as a unit-ish tuple."""
        return _player_forward(getattr(self.logic, "player", None))

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
        player = getattr(self.logic, "player", None)
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

        The new entity is appended to ``logic.things`` so gameplay queries and
        (once the host draws dynamic entities) rendering pick it up. Returns
        ``None`` if construction fails.
        """
        try:
            ent = cls(pos=list(pos) if pos is not None else [0, 0, 0],
                      properties=dict(properties or {}))
        except Exception:
            self._manager._log(f"spawn() failed for '{self._plugin.name}'")
            return None
        things = getattr(self.logic, "things", None)
        if things is not None:
            try:
                things.append(ent)
            except Exception:
                pass
        try:
            self._manager.emit("entity_spawned", logic=self.logic, entity=ent,
                               by=self._plugin.name)
        except Exception:
            pass
        return ent

    def despawn(self, entity) -> bool:
        """Remove *entity* from the live scene. Returns True if it was present."""
        things = getattr(self.logic, "things", None)
        if things is None:
            return False
        try:
            things.remove(entity)
            return True
        except ValueError:
            return False

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

    HUD helpers replace poking ``logic.current_hud_message`` directly:
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
            self.logic.current_hud_message = str(text)
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
            if not getattr(self.logic, "current_hud_message", ""):
                self.logic.current_hud_message = st.toast_text
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Plugin base class
# ---------------------------------------------------------------------------

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
    requires: List[str] = []

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
