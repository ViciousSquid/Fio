"""
Plugin discovery, loading and hook dispatch.

The :class:`PluginManager` is a process-wide singleton. It:

  1. **Discovers** plugins by scanning this package's directory for
     sub-packages that expose a ``PLUGIN`` instance or ``get_plugin()``
     factory.
  2. **Loads** each plugin once, calling ``register(EditorAPI)``. This is
     idempotent — :func:`load_plugins` may be called from several entry points
     (editor window, logic thread, headless tools) and only acts the first
     time.
  3. **Attaches** to each play session's logic thread via
     :meth:`attach_runtime`, calling ``register_runtime(RuntimeAPI)``.
  4. **Dispatches** the ``on_play_start`` / ``on_tick`` / ``on_play_stop``
     lifecycle hooks.

Every call into plugin code is wrapped so a misbehaving plugin logs an error
instead of taking down the editor or a play session.
"""

from __future__ import annotations

import importlib
import os
import pkgutil
import traceback
from typing import List, Optional, Tuple

from .api import (API_VERSION, EditorAPI, FioPlugin, GlobalStore, RuntimeAPI,
                  TickContext, version_tuple)
from .host import EventBus, PluginHost


def _log(message: str):
    """Emit a plugin message (errors/warnings) to the console.

    Routes through the editor debug console when present, else prints. Reserved
    for things the user should see — failures. Informational chatter goes
    through :func:`_debug` instead, which is silent unless FIO_PLUGIN_DEBUG is
    set, so a normal launch shows nothing about plugins loading.
    """
    try:
        from editor.debug_console import debug_log
        debug_log("Plugins", message)
    except Exception:
        print(f"[Plugins] {message}")


def _debug(message: str):
    """Emit an informational plugin message only when debugging is enabled.

    Enable with the ``FIO_PLUGIN_DEBUG`` environment variable. Kept quiet by
    default so successful plugin loading does not clutter the console.
    """
    if os.environ.get("FIO_PLUGIN_DEBUG"):
        _log(message)


class PluginManager:
    def __init__(self):
        self.plugins: List[FioPlugin] = []
        self._loaded = False
        self._loading = False
        # (plugin, label, ThingClass) placement entries for the editor menu.
        self._menu_entries: List[Tuple[FioPlugin, str, type]] = []
        # Normalised entity-type name -> owning plugin (for package export).
        # Keyed the same way editor.things.from_dict matches: class name,
        # lowercased, underscores stripped.
        self._entity_owner: dict = {}
        # Normalised entity-type name -> entity class. Lets the player host
        # instantiate plugin entities from map data without the editor palette.
        self._entity_classes: dict = {}
        # Normalised entity-type name -> list[PropertySpec]. Optional typed
        # schemas plugins declare for their entities' editable properties.
        self._property_schemas: dict = {}
        # Cross-level key/value store shared with map LogicKeyValueStores in the
        # editor, and a process-local dict in the dependency-light player.
        self.global_store = GlobalStore()
        # The open-ended extension surface: a process-wide event bus the engine
        # emits into, a cross-plugin service registry, and the per-plugin host
        # objects bound to the live session (see bind_host).
        self.events = EventBus(log=self._log)
        self.services: dict = {}
        self._hosts: dict = {}   # plugin -> PluginHost
        self._host_target = None
        self._host_kind = "engine"
        # Cached answer to "does any plugin need the per-frame tick?" so the
        # engine can gate its per-frame call with one int compare (see
        # wants_tick). Invalidated by the enabled-generation and the event bus's
        # subscription generation.
        self._tick_work = False
        self._tick_work_gen = -1
        self._tick_work_sub_gen = -1
        # Editor-UI extensions plugins register (consumed by integration.py):
        # extra property fields appended to an entity's panel, and whole custom
        # property tabs. Both keyed/filtered by normalised entity type.
        self._extra_fields: dict = {}       # type -> list[PropertySpec]
        self._property_tabs: list = []      # list[(label, factory, type_or_None)]
        # Disabled plugin names (by directory or plugin.name). Populated from
        # the FIO_DISABLED_PLUGINS env var, comma-separated.
        self._disabled = {
            n.strip().lower()
            for n in os.environ.get("FIO_DISABLED_PLUGINS", "").split(",")
            if n.strip()
        }
        # Plugins switched on by a loaded level (auto_enable_*), as opposed to
        # a manual menu toggle. Tracked so an empty/new scene can revert exactly
        # those without touching a plugin the user enabled by hand.
        self._auto_enabled: set = set()
        # Bumped whenever the set of enabled plugins (or the plugin list) changes,
        # so the per-hook dispatch caches below can invalidate cheaply. This keeps
        # the hot per-tick path from re-scanning every plugin each frame.
        self._enabled_generation = 0
        self._active_cache: dict = {}       # hook name -> list of active plugins
        self._active_cache_gen = -1

    # -- logging ------------------------------------------------------------
    def _log(self, message: str):
        """Console-visible message (failures)."""
        _log(message)

    def _debug(self, message: str):
        """Informational message, silent unless FIO_PLUGIN_DEBUG is set."""
        _debug(message)

    # -- discovery + load ---------------------------------------------------
    def discover_and_load(self):
        """Find and load all plugins. Safe to call repeatedly."""
        if self._loaded or self._loading:
            return
        self._loading = True

        package_dir = os.path.dirname(os.path.abspath(__file__))
        found = 0
        for entry in sorted(pkgutil.iter_modules([package_dir])):
            mod_name = entry.name
            if not entry.ispkg:
                continue
            if mod_name.startswith("_"):
                continue
            if mod_name.lower() in self._disabled:
                self._debug(f"Skipping disabled plugin package '{mod_name}'")
                continue
            self._load_one(mod_name)
            found += 1

        self._loaded = True
        self._loading = False

        self._verify_requirements()

        if self.plugins:
            names = ", ".join(f"{p.name} v{p.version}" for p in self.plugins)
            self._debug(f"Loaded {len(self.plugins)} plugin(s): {names}")
        elif found == 0:
            self._debug("No plugins found.")

    def _load_one(self, mod_name: str):
        try:
            module = importlib.import_module(f"plugins.{mod_name}")
        except Exception:
            self._log(f"Failed to import plugin '{mod_name}':\n{traceback.format_exc()}")
            return

        plugin = getattr(module, "PLUGIN", None)
        if plugin is None:
            factory = getattr(module, "get_plugin", None)
            if callable(factory):
                try:
                    plugin = factory()
                except Exception:
                    self._log(f"get_plugin() failed for '{mod_name}':\n{traceback.format_exc()}")
                    return

        if plugin is None:
            self._log(f"Plugin package '{mod_name}' exposes no PLUGIN or get_plugin(); skipping.")
            return
        if not isinstance(plugin, FioPlugin):
            self._log(f"Plugin '{mod_name}' PLUGIN is not a FioPlugin; skipping.")
            return
        if plugin.name.lower() in self._disabled:
            self._debug(f"Skipping disabled plugin '{plugin.name}'")
            return

        # API-compatibility gate: refuse a plugin that needs a newer API than
        # this host provides, with a clear message, rather than letting it fail
        # deep inside a hook later.
        needs = getattr(plugin, "api_version", "1.0.0")
        if version_tuple(needs) > version_tuple(API_VERSION):
            self._log(
                f"Plugin '{plugin.name}' needs API v{needs} but this host "
                f"provides v{API_VERSION}; skipping. Update Fio to use it.")
            return

        try:
            plugin.register(EditorAPI(self, plugin))
        except Exception:
            self._log(f"register() failed for '{plugin.name}':\n{traceback.format_exc()}")
            return

        # Collect any property schemas the plugin publishes for its entities.
        try:
            schemas = plugin.describe_properties() or {}
            for entity_type, specs in schemas.items():
                self._record_property_schema(entity_type, specs)
        except Exception:
            self._log(f"describe_properties() failed for '{plugin.name}':\n{traceback.format_exc()}")

        # Pick up any bespoke menu entries the plugin declares directly.
        try:
            for label, cls in (plugin.menu_entries() or []):
                self._add_menu_entry(plugin, label, cls)
        except Exception:
            self._log(f"menu_entries() failed for '{plugin.name}':\n{traceback.format_exc()}")

        self.plugins.append(plugin)
        self._enabled_generation += 1

    def _verify_requirements(self):
        """Disable any plugin whose declared ``requires`` aren't all loaded.

        Keeps the plugin loaded (its entities stay known so maps still open) but
        turns it off with a clear message, instead of letting it half-run
        against a missing dependency.
        """
        available = set()
        for p in self.plugins:
            available.add(p.name.lower())
            available.add(self.plugin_package_name(p).lower())
        for plugin in self.plugins:
            reqs = getattr(plugin, "requires", None) or []
            missing = [r for r in reqs if str(r).lower() not in available]
            if missing:
                self._log(
                    f"Plugin '{plugin.name}' requires missing plugin(s): "
                    f"{', '.join(missing)}; disabling it.")
                self.set_enabled(plugin, False)

    # -- property schema ----------------------------------------------------
    def _record_property_schema(self, entity_type: str, specs):
        """Store a typed property schema for *entity_type* (normalised key)."""
        if not specs:
            return
        self._property_schemas[self._normalise_type(entity_type)] = list(specs)

    def property_schema_for(self, type_name: str):
        """Return the list of ``PropertySpec`` for *type_name*, or ``None``."""
        return self._property_schemas.get(self._normalise_type(type_name))

    # -- editor menu --------------------------------------------------------
    def _add_menu_entry(self, plugin: FioPlugin, label: str, cls: type):
        self._menu_entries.append((plugin, label, cls))

    def menu_entries(self) -> List[Tuple[FioPlugin, str, type]]:
        """All placement entries as ``(plugin, label, ThingClass)`` tuples."""
        return list(self._menu_entries)

    def has_plugins(self) -> bool:
        return bool(self.plugins)

    # -- enable / disable ---------------------------------------------------
    def find_plugin(self, name: str) -> Optional[FioPlugin]:
        low = str(name).lower()
        for p in self.plugins:
            if p.name.lower() == low or self.plugin_package_name(p).lower() == low:
                return p
        return None

    def is_enabled(self, plugin) -> bool:
        return bool(getattr(plugin, "enabled", True))

    def set_enabled(self, plugin_or_name, enabled: bool, auto: bool = False):
        """Enable/disable a plugin at runtime.

        A disabled plugin stays loaded (its already-registered entity types
        remain known) but is skipped for runtime attach and lifecycle/tick
        dispatch, so its gameplay stops. Placement of its entities is greyed out
        in the editor menus.

        *auto* distinguishes a level-driven enable (see :meth:`auto_enable_for_map`)
        from a manual menu toggle. Only auto-enables are remembered so that a
        New/empty scene can revert them; any manual call clears that memory, so
        a plugin the user turned on by hand is never auto-disabled underneath
        them.
        """
        plugin = plugin_or_name
        if isinstance(plugin_or_name, str):
            plugin = self.find_plugin(plugin_or_name)
        if plugin is None:
            return
        was = bool(getattr(plugin, "enabled", True))
        plugin.enabled = bool(enabled)
        if was != bool(enabled):
            self._enabled_generation += 1
            # Notify the plugin so it can acquire/release resources on toggle.
            try:
                plugin.on_enabled_changed(bool(enabled))
            except Exception:
                self._log(f"on_enabled_changed() failed for '{plugin.name}':\n{traceback.format_exc()}")
        if auto and enabled:
            self._auto_enabled.add(plugin)
        else:
            self._auto_enabled.discard(plugin)

    # -- entity ownership / packaging --------------------------------------
    @staticmethod
    def _normalise_type(type_name: str) -> str:
        """Match editor.things.from_dict: lowercased, underscores stripped."""
        return str(type_name).replace("_", "").lower()

    def _record_entity_owner(self, cls: type, plugin: FioPlugin):
        key = cls.__name__.lower()
        self._entity_owner[key] = plugin
        self._entity_classes[key] = cls

    def plugin_for_type(self, type_name: str) -> Optional[FioPlugin]:
        """Return the plugin that owns *type_name* (a map entity 'type'), or None."""
        return self._entity_owner.get(self._normalise_type(type_name))

    def entity_class_for_type(self, type_name: str) -> Optional[type]:
        """Return the entity class registered for *type_name*, or None.

        Base-agnostic: works whether the class subclasses the editor's ``Thing``
        or the PyQt-free fallback, so the player host can build instances from
        map data without the editor.
        """
        return self._entity_classes.get(self._normalise_type(type_name))

    def required_plugins_for_types(self, type_names) -> List[FioPlugin]:
        """Plugins needed to load entities of the given map 'type' strings."""
        seen, out = set(), []
        for t in type_names:
            plugin = self.plugin_for_type(t)
            if plugin is not None and id(plugin) not in seen:
                seen.add(id(plugin))
                out.append(plugin)
        return out

    # -- auto-enable on level load -----------------------------------------
    def auto_enable_for_types(self, type_names) -> List[FioPlugin]:
        """Enable any disabled plugin that owns one of *type_names*.

        A plugin can ship disabled-by-default (``enabled = False``) so it stays
        inert for maps that don't use it. When a level referencing its entities
        is loaded, call this to switch it on so its gameplay actually runs.

        This is a runtime, per-session flip only: it never touches the persisted
        Plugins ``disabled`` list, so a plugin the user deliberately turned off
        stays off next launch unless a level re-triggers it. Returns the plugins
        that were newly enabled (empty if all were already on).
        """
        newly: List[FioPlugin] = []
        for plugin in self.required_plugins_for_types(type_names):
            if not self.is_enabled(plugin):
                self.set_enabled(plugin, True, auto=True)
                self._debug(
                    f"Auto-enabled plugin '{plugin.name}' for loaded level")
                newly.append(plugin)
        return newly

    def disable_auto_enabled(self) -> List[FioPlugin]:
        """Turn off any plugin a loaded level auto-enabled.

        Called when the scene is reset to an empty state — File ▸ New, or just
        before a different level loads — so a disabled-by-default plugin that was
        switched on only to run a specific map reverts to off and a fresh map
        starts clean. A following :meth:`auto_enable_for_map` re-enables it if
        the new level actually uses it. Plugins the user enabled by hand (a
        manual menu toggle) are never in this set, so they are left untouched.
        Returns the plugins that were disabled.
        """
        disabled: List[FioPlugin] = []
        for plugin in list(self._auto_enabled):
            if self.is_enabled(plugin):
                disabled.append(plugin)
                self._debug(
                    f"Auto-disabled plugin '{plugin.name}' for cleared level")
            self.set_enabled(plugin, False)
        return disabled

    def auto_enable_for_map(self, map_data) -> List[FioPlugin]:
        """Enable plugins whose entity types appear in *map_data*.

        *map_data* is a loaded level dict; its ``things`` are scanned for the
        same ``type`` strings the editor/player use to build entities. A no-op
        (returns ``[]``) for maps that reference no plugin-owned entities.
        """
        types: List[str] = []
        things = map_data.get("things", []) if isinstance(map_data, dict) else []
        for t in things:
            if not isinstance(t, dict):
                continue
            typ = t.get("type") or t.get("properties", {}).get("type")
            if typ:
                types.append(typ)
        return self.auto_enable_for_types(types)

    def plugin_package_dir(self, plugin: FioPlugin) -> Optional[str]:
        """Absolute filesystem directory of a plugin's package, or None."""
        import inspect
        try:
            return os.path.dirname(os.path.abspath(inspect.getfile(type(plugin))))
        except Exception:
            return None

    def plugin_package_name(self, plugin: FioPlugin) -> str:
        """The plugin's package basename (e.g. 'tidy')."""
        d = self.plugin_package_dir(plugin)
        return os.path.basename(d) if d else plugin.name

    # -- runtime attach -----------------------------------------------------
    def attach_runtime(self, logic):
        """Register every loaded plugin's I/O handlers for this logic thread.

        All plugins attach, not just the currently-enabled ones: their input
        handlers are gated by live ``enabled`` state (see
        :meth:`RuntimeAPI.register_input_handler`), so a plugin enabled later —
        e.g. a disabled-by-default plugin auto-enabled when its level loads —
        has working inputs without a re-attach. The logic thread is created
        once, before any level loads, so this is the only chance to attach.
        """
        for plugin in self.plugins:
            try:
                plugin.register_runtime(RuntimeAPI(self, logic, plugin))
            except Exception:
                self._log(f"register_runtime() failed for '{plugin.name}':\n{traceback.format_exc()}")

    # -- host binding + events ---------------------------------------------
    def bind_host(self, target, kind: str = "engine"):
        """Bind the plugin :class:`~plugins.host.PluginHost` to a live session.

        Called once, alongside :meth:`attach_runtime`, when the session's logic
        thread is built. Gives every plugin a host object reaching the whole
        engine and calls its ``connect(host)`` hook so it can subscribe to
        events, publish services or install extensions. Every plugin connects
        (not just enabled ones) because its event subscriptions self-gate on the
        live ``enabled`` flag — so a plugin enabled later is already wired in.
        """
        # Re-binding to a fresh session (e.g. a new play run) must not stack a
        # second copy of every subscription: drop the previous session's event
        # handlers before connect() re-registers them.
        if self._hosts and target is not self._host_target:
            self.events.clear()
            self._hosts.clear()
        self._host_target = target
        self._host_kind = kind
        for plugin in self.plugins:
            host = PluginHost(self, target, plugin, kind=kind)
            self._hosts[plugin] = host
            try:
                plugin.connect(host)
            except Exception:
                self._log(f"connect() failed for '{plugin.name}':\n{traceback.format_exc()}")

    def host_for(self, plugin) -> Optional[PluginHost]:
        """The bound :class:`~plugins.host.PluginHost` for *plugin*, or None."""
        return self._hosts.get(plugin)

    def emit(self, event: str, **data):
        """Emit an engine event to subscribed plugins (see :class:`EventBus`).

        A thin, always-safe pass-through: the bus early-outs when *event* has no
        subscribers, so engine emit points cost almost nothing when unused.
        """
        return self.events.emit(event, **data)

    def has_listeners(self, event: str) -> bool:
        """True if any plugin is subscribed to *event*."""
        return self.events.has(event)

    def wants_tick(self) -> bool:
        """Whether anything needs the per-frame :meth:`tick` this session.

        The engine's hot path calls this to decide whether to invoke
        :meth:`tick` at all — so a session with no ticking plugin and no
        ``tick`` event listener skips the call (and its argument packing)
        entirely. O(1) amortised: it recomputes only when the enabled set or the
        event subscriptions actually change, otherwise it's two int compares.
        """
        if (self._tick_work_gen != self._enabled_generation
                or self._tick_work_sub_gen != self.events.gen):
            self._tick_work = bool(self._active_for("on_tick")) or self.events.has("tick")
            self._tick_work_gen = self._enabled_generation
            self._tick_work_sub_gen = self.events.gen
        return self._tick_work

    # -- editor-UI extensions (consumed by plugins.integration) -------------
    def register_extra_fields(self, entity_type: str, specs):
        """Append extra editable fields to *entity_type*'s property panel.

        Unlike a full property schema (which drives a plugin entity's whole
        panel), these are *appended* after an entity's stock rows — so a plugin
        can add fields to any entity, including built-in ones, without
        disturbing the existing UI.
        """
        if not specs:
            return
        key = self._normalise_type(entity_type)
        self._extra_fields.setdefault(key, []).extend(specs)

    def extra_fields_for(self, entity_type: str):
        """Extra field specs registered for *entity_type* (possibly empty)."""
        return self._extra_fields.get(self._normalise_type(entity_type), [])

    def register_property_tab(self, label: str, factory, entity_type=None):
        """Register a custom property-panel tab.

        *factory(thing)* returns a widget; *label* names the tab. If
        *entity_type* is given the tab shows only for that type, else for every
        entity. Consumed by the editor integration when it builds a panel.
        """
        self._property_tabs.append(
            (label, factory,
             self._normalise_type(entity_type) if entity_type else None))

    def property_tabs_for(self, entity_type: str):
        """List of ``(label, factory)`` custom tabs that apply to *entity_type*."""
        norm = self._normalise_type(entity_type)
        return [(label, factory) for (label, factory, t) in self._property_tabs
                if t is None or t == norm]

    # -- lifecycle dispatch -------------------------------------------------
    @staticmethod
    def _overrides(plugin: FioPlugin, hook: str) -> bool:
        """True if *plugin*'s class actually overrides the *hook* method.

        A plugin that doesn't implement a hook inherits the empty ``FioPlugin``
        method; skipping those means an idle hook costs nothing, and a map whose
        active plugins don't tick pays no per-frame dispatch at all.
        """
        return getattr(type(plugin), hook, None) is not getattr(FioPlugin, hook, None)

    def _active_for(self, hook: str) -> List[FioPlugin]:
        """Cached list of enabled plugins that override *hook*.

        Rebuilt only when the enabled set (or plugin list) changes, tracked by
        ``_enabled_generation``. On the hot per-tick path this is a dict lookup
        and a generation compare rather than a full scan every frame.
        """
        if self._active_cache_gen != self._enabled_generation:
            self._active_cache = {}
            self._active_cache_gen = self._enabled_generation
        cached = self._active_cache.get(hook)
        if cached is None:
            cached = [p for p in self.plugins
                      if self.is_enabled(p) and self._overrides(p, hook)]
            self._active_cache[hook] = cached
        return cached

    def dispatch_play_start(self, logic):
        for plugin in self._active_for("on_play_start"):
            try:
                plugin.on_play_start(logic)
            except Exception:
                self._log(f"on_play_start() failed for '{plugin.name}':\n{traceback.format_exc()}")

    def dispatch_play_stop(self, logic):
        for plugin in self._active_for("on_play_stop"):
            try:
                plugin.on_play_stop(logic)
            except Exception:
                self._log(f"on_play_stop() failed for '{plugin.name}':\n{traceback.format_exc()}")

    def dispatch_tick(self, logic, ctx: TickContext):
        if getattr(ctx, "logic", None) is None:
            ctx.logic = logic
        for plugin in self._active_for("on_tick"):
            try:
                plugin.on_tick(logic, ctx)
            except Exception:
                self._log(f"on_tick() failed for '{plugin.name}':\n{traceback.format_exc()}")
        try:
            ctx._finalize_hud()
        except Exception:
            pass

    def tick(self, logic, use_pressed: bool = False,
             interaction_consumed: bool = False, delta: float = 0.0, keys=None):
        """Per-frame entry point: dispatch ``on_tick`` to active plugins.

        Early-outs before building a :class:`TickContext` when nothing is
        listening, so a play session with no ticking plugin costs almost nothing
        each frame. Preferred over building a context and calling
        :meth:`dispatch_tick` at every call site.

        *keys* is the host's set of currently-held keys (Qt key codes on the
        engine); it populates :attr:`TickContext.keys` so plugins can read held
        input via :meth:`TickContext.key_down`. It may also be a **callable**
        returning that set — the manager invokes it only after deciding a
        context is needed, so a host can defer any per-frame cost (a lock, a
        copy) to the frames where a plugin is actually listening.
        """
        tickers = self._active_for("on_tick")
        wants_event = self.events.has("tick")
        if not tickers and not wants_event:
            return
        # Resolve keys lazily: only now that we know a plugin will see them.
        if callable(keys):
            keys = keys()
        ctx = TickContext(
            delta=delta,
            use_pressed=bool(use_pressed),
            interaction_consumed=bool(interaction_consumed),
            keys=frozenset(keys) if keys else frozenset(),
            logic=logic,
        )
        for plugin in tickers:
            try:
                plugin.on_tick(logic, ctx)
            except Exception:
                self._log(f"on_tick() failed for '{plugin.name}':\n{traceback.format_exc()}")
        # Event-bus 'tick' for plugins that hook via host.on('tick', ...) rather
        # than overriding on_tick. Fires after the hook so both see the same ctx.
        if wants_event:
            self.events.emit("tick", logic=logic, ctx=ctx, delta=delta,
                             use_pressed=bool(use_pressed))
        # Apply any timed HUD toast a plugin set, if nothing claimed the line.
        try:
            ctx._finalize_hud()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Module-level singleton helpers
# ---------------------------------------------------------------------------

_MANAGER: Optional[PluginManager] = None


def get_manager() -> PluginManager:
    """Return the process-wide :class:`PluginManager`, creating it on first use."""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = PluginManager()
    return _MANAGER


def load_plugins():
    """Discover and load all plugins (idempotent). Call this early at startup."""
    get_manager().discover_and_load()
