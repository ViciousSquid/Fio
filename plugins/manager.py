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
        # Package names already loaded, so a retried discovery pass cannot
        # register the same plugin twice.
        self._loaded_modules: set = set()
        # Package names that were mid-import when discovery reached them (see
        # _load_one). While this is non-empty the manager does not consider
        # itself loaded, so the next load_plugins() call finishes the job.
        self._deferred: set = set()
        # (plugin, label, ThingClass) placement entries for the editor menu.
        self._menu_entries: List[Tuple[FioPlugin, str, type]] = []
        # Built-in layers installed natively by the application bootstrap rather
        # than discovered on disk. Unlike a plugin these are always on, never
        # toggled, never api-version gated and never listed in the Plugins menu,
        # but they still take part in the generic extension surface (entity /
        # property / IO registration, runtime attach, host bind and the
        # play-lifecycle and per-tick dispatch). This lets a branch ship a
        # first-class layer without wearing the plugin machinery.
        self._builtin_games: list = []
        # Placement entries declared by a built-in layer, kept apart from the
        # plugin entries above so they can surface under their own menu rather
        # than the "Plugins" submenu.
        self._builtin_menu_entries: List[Tuple[object, str, type]] = []
        # Normalised entity-type -> creation wizard factory (see
        # register_entity_wizard). Used by the editor to configure an entity that
        # needs setup instead of dropping the user into raw properties.
        self._entity_wizards: dict = {}
        # Normalised entity-type names that may exist at most once per map.
        # Placement paths consult this.
        self._singleton_types: set = set()
        # Normalised entity-type name -> owning plugin (for package export).
        # Keyed the same way editor.things.from_dict matches: class name,
        # lowercased, underscores stripped.
        self._entity_owner: dict = {}
        # Every I/O registration the loaded plugins have made, so they can be
        # replayed if the process-wide IO_REGISTRY is reset out from under
        # them. See reapply_registrations().
        self._io_registrations: list = []
        self._replaying_registrations = False
        # Normalised entity-type name -> entity class. Lets the player host
        # instantiate plugin entities from map data without the editor palette.
        self._entity_classes: dict = {}
        # Normalised entity-type name -> list[PropertySpec]. Optional typed
        # schemas plugins declare for their entities' editable properties.
        self._property_schemas: dict = {}
        # Cross-level key/value store shared with the map LogicState entities in the
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
        self._tools_actions: list = []
        self._menu_actions: list = []
        self._console_commands: dict = {}
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
    def discover_and_load(self, extra_roots=()):
        """Find and load all plugins. Safe to call repeatedly.

        *extra_roots* are additional directories to scan for plugin packages,
        used when a ``.fiopak`` brings its own. Passing any re-opens discovery
        on an already-loaded manager, because the point is to pick up packages
        that were not on disk the first time round.

        A plugin package already loaded is skipped by name whichever root it
        came from, so a second package cannot hot-swap a plugin the session is
        already running -- the first one loaded wins, and the rest of the
        session keeps the classes it already has live objects for.
        """
        if extra_roots:
            self._loaded = False
        if self._loaded or self._loading:
            return
        self._loading = True

        package_dir = os.path.dirname(os.path.abspath(__file__))
        roots = [package_dir]
        for root in extra_roots:
            bundled = os.path.join(root, "plugins")
            if os.path.isdir(bundled) and bundled not in roots:
                roots.append(bundled)
        found = 0
        self._deferred.clear()
        # Sort by name, not by the ModuleInfo tuple: a tuple compare starts on
        # the finder, and two roots mean two different FileFinders, which do
        # not order.  Name order is what was wanted anyway -- a deterministic
        # load sequence.
        for entry in sorted(pkgutil.iter_modules(roots), key=lambda e: e.name):
            mod_name = entry.name
            if not entry.ispkg:
                continue
            if mod_name.startswith("_"):
                continue
            if mod_name.lower() in self._disabled:
                self._debug(f"Skipping disabled plugin package '{mod_name}'")
                continue
            if mod_name in self._loaded_modules or \
                    mod_name.lower() in {m.lower() for m in self._loaded_modules}:
                continue          # already loaded by an earlier, partial pass
            self._load_one(mod_name)
            found += 1

        self._loading = False
        # A plugin whose own import triggered this discovery could not be read
        # yet (see _load_one). Leaving _loaded False means the next call - the
        # one the outer import makes once it has finished - picks it up, instead
        # of the plugin being dropped for the life of the process.
        self._loaded = not self._deferred
        if self._deferred:
            self._debug("Deferring %s until their import completes"
                        % ", ".join(sorted(self._deferred)))
            return

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

        # A plugin package whose own import reached back into the host and
        # started discovery (tidy's entities import `editor`, whose package
        # initialiser calls load_plugins) is still mid-import here: Python hands
        # back the partially-initialised module, which has no PLUGIN yet.
        # Skipping it quietly would drop that plugin for the life of the
        # process, so it is recorded and retried by the outer call instead.
        if getattr(getattr(module, "__spec__", None), "_initializing", False):
            self._deferred.add(mod_name)
            self._debug(f"Plugin '{mod_name}' is still importing; will retry")
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

        # Name gate: one plugin per name, first one loaded wins.  The module
        # guard above is keyed on the package directory, which is not the same
        # question -- a .fiopak can carry a plugin under a differently-spelled
        # directory, and registering it a second time would give the session
        # two plugins answering to one name, with live entity classes split
        # between them.  Nothing is hot-swapped: the running one stays.
        existing = {p.name.lower() for p in self.plugins}
        if str(getattr(plugin, "name", "")).lower() in existing:
            self._debug(
                f"Plugin '{plugin.name}' is already loaded; keeping the "
                f"running one and ignoring the copy in '{mod_name}'.")
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
        self._loaded_modules.add(mod_name)
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
    def _record_tools_action(self, plugin, label: str, callback, tooltip: str = "") -> None:
        if callable(callback):
            self._tools_actions.append((plugin, str(label), callback, str(tooltip or "")))

    def tools_actions(self):
        return list(self._tools_actions)

    def _record_menu_action(self, plugin, label: str, callback, tooltip: str = "") -> None:
        if callable(callback):
            self._menu_actions.append((plugin, str(label), callback, str(tooltip or "")))

    def menu_actions(self):
        return list(self._menu_actions)

    def _register_console_command(self, plugin, name: str, callback, help_text: str = "") -> None:
        key = str(name).strip().lower()
        if key and callable(callback):
            self._console_commands[key] = (plugin, callback, str(help_text or ""))

    def has_console_command(self, name: str) -> bool:
        entry = self._console_commands.get(str(name).strip().lower())
        return bool(entry and self.is_enabled(entry[0]))

    def console_commands(self):
        return {
            name: {"plugin": plugin, "help": help_text}
            for name, (plugin, _callback, help_text) in self._console_commands.items()
            if self.is_enabled(plugin)
        }

    def dispatch_console_command(self, name: str, args: str, logic=None,
                                 main_window=None, play_mode: bool = False):
        entry = self._console_commands.get(str(name).strip().lower())
        if entry is None:
            return False, None
        plugin, callback, _help_text = entry
        if not self.is_enabled(plugin):
            return False, None
        try:
            return True, callback(args, main_window, logic, bool(play_mode))
        except Exception:
            self._log(
                f"console command '{name}' failed for '{plugin.name}':\n"
                f"{traceback.format_exc()}"
            )
            return True, None

    def _record_io_registration(self, plugin, kind, entity_type, inputs, outputs):
        """Remember an I/O registration so it can be replayed.

        *kind* is 'set' for a type the plugin owns and 'extend' for additions
        to a type it does not. See :meth:`reapply_registrations`.
        """
        if getattr(self, '_replaying_registrations', False):
            return
        self._io_registrations.append(
            (plugin, kind, entity_type, inputs, outputs))

    def reapply_registrations(self):
        """Re-issue every registration this manager's plugins have made.

        Plugin registration happens once per process: ``discover_and_load``
        early-outs on ``self._loaded``, so a plugin's ``register()`` runs on
        first load and never again. The registries it writes into --
        ``editor.io_system.IO_REGISTRY`` and ``editor.things.ENTITY_TYPES`` --
        are module-level singletons, so anything that resets one of them
        silently strips the plugins' declarations with no way to get them back:
        the plugin is still loaded and still enabled, but its I/O and its
        entity types have vanished.

        This replays the *recorded* registrations rather than re-running
        ``register()``, so it cannot re-trigger whatever else a plugin does at
        registration time, and it is safe to call at any point.

        Returns the number of I/O registrations replayed.
        """
        try:
            from editor.io_system import register_io
        except Exception:
            return 0

        # Snapshot, and suppress recording: replaying through the API would
        # otherwise append to the very list being walked.
        pending = list(self._io_registrations)
        self._replaying_registrations = True
        try:
            self._replay_io(pending, register_io)
        finally:
            self._replaying_registrations = False

        try:
            from editor.things import ENTITY_TYPES, ENTITY_CATEGORIES
        except Exception:
            return len(pending)

        for key, cls in list(self._entity_classes.items()):
            if not isinstance(cls, type):
                continue
            plugin = self._entity_owner.get(key)
            ENTITY_TYPES.setdefault(cls.__name__, cls)
            category = getattr(plugin, 'category', None) or 'Plugins'
            ENTITY_CATEGORIES.setdefault(category, [])
            if cls.__name__ not in ENTITY_CATEGORIES[category]:
                ENTITY_CATEGORIES[category].append(cls.__name__)

        return len(pending)

    def _replay_io(self, pending, register_io):
        for entry in pending:
            plugin, kind, entity_type, inputs, outputs = entry
            try:
                if kind == 'extend':
                    # Re-merge against the live registry rather than pinning
                    # the core declarations as they were at load time.
                    api = EditorAPI(self, plugin)
                    api.extend_io(entity_type, inputs, outputs)
                else:
                    register_io(entity_type, list(inputs), list(outputs))
            except Exception:
                self._log(f"could not replay I/O registration for '{entity_type}'")

    def _record_property_schema(self, entity_type: str, specs):
        """Store a typed property schema for *entity_type* (normalised key)."""
        if not specs:
            return
        self._property_schemas[self._normalise_type(entity_type)] = list(specs)

    def property_schema_for(self, type_name: str):
        """Return the list of ``PropertySpec`` for *type_name*, or ``None``."""
        return self._property_schemas.get(self._normalise_type(type_name))

    # -- built-in layer registration ----------------------------------------
    def register_builtin_game(self, game) -> None:
        """Install a built-in layer (see :attr:`_builtin_games`).

        The *game* is any object exposing the same lifecycle surface a plugin
        does (``register``/``register_runtime``/``connect``/``on_play_start``/
        ``on_tick``/``on_play_stop``) plus ``name``/``version``/``category``
        attributes, but it is **not** a :class:`FioPlugin`: it carries no
        ``enabled``/``requires``/``api_version`` and is never discovered,
        toggled or shown in the Plugins menu. Registration is idempotent.

        Intended to be called once by an application bootstrap, after the editor
        package is fully constructed.
        """
        for existing in self._builtin_games:
            if existing is game or type(existing) is type(game):
                return
        # Mark it so the shared registries can route its menu entries to the
        # built-in list instead of the plugin list.
        try:
            game.is_builtin_game = True
        except Exception as exc:
            self._log(f"could not tag built-in layer "
                      f"'{getattr(game, 'name', '?')}': {exc}")
        self._builtin_games.append(game)
        try:
            game.register(EditorAPI(self, game))
        except Exception:
            self._log(f"register() failed for built-in layer "
                      f"'{getattr(game, 'name', '?')}':\n{traceback.format_exc()}")
        # Pick up any property schemas the layer declares for its entities.
        describe = getattr(game, "describe_properties", None)
        if callable(describe):
            try:
                for entity_type, specs in (describe() or {}).items():
                    self._record_property_schema(entity_type, specs)
            except Exception:
                self._log(f"describe_properties() failed for built-in layer "
                          f"'{getattr(game, 'name', '?')}':\n{traceback.format_exc()}")
        self._enabled_generation += 1

    def builtin_games(self) -> list:
        """The registered built-in layers."""
        return list(self._builtin_games)

    def builtin_menu_entries(self) -> List[Tuple[object, str, type]]:
        """Placement entries a built-in layer declared, for its own menu."""
        return list(self._builtin_menu_entries)

    def register_entity_wizard(self, type_name: str, factory) -> None:
        """Register a creation *wizard* for an entity type.

        ``factory(parent) -> dict | None`` runs a dialog and returns the initial
        properties for a new entity (or ``None`` if cancelled). When present, the
        editor launches it instead of dropping the user straight into raw
        properties for an entity that needs configuring.
        """
        self._entity_wizards[self._normalise_type(type_name)] = factory

    def entity_wizard_for(self, type_name: str):
        """The creation wizard registered for *type_name*, or None."""
        return self._entity_wizards.get(self._normalise_type(type_name))

    def register_singleton_entity(self, type_name: str) -> None:
        """Mark an entity type as a per-map singleton (at most one instance).

        Placement paths check :meth:`is_singleton_entity` and refuse a second
        one, selecting the existing instance instead.
        """
        self._singleton_types.add(self._normalise_type(type_name))

    def is_singleton_entity(self, type_name: str) -> bool:
        return self._normalise_type(type_name) in self._singleton_types

    def _participants(self, hook: str) -> list:
        """Enabled plugins + built-in layers that implement *hook*.

        Built-in layers are always active (no enable gate, no api gate); a
        plugin must be enabled. Both must actually define the hook so an idle
        one costs nothing on the per-tick path.
        """
        out = [p for p in self.plugins
               if self.is_enabled(p) and self._overrides(p, hook)]
        for game in self._builtin_games:
            if getattr(type(game), hook, None) is not None:
                out.append(game)
        return out

    # -- editor menu --------------------------------------------------------
    def _add_menu_entry(self, plugin: FioPlugin, label: str, cls: type):
        # A built-in layer's entities surface under their own menu, not the
        # "Plugins" submenu, so keep them in a separate list.
        if getattr(plugin, "is_builtin_game", False):
            self._builtin_menu_entries.append((plugin, label, cls))
        else:
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

    def required_plugins_for_map(self, map_data) -> List[FioPlugin]:
        """Plugins required by a map's entity types or plugin activation hook."""
        if not isinstance(map_data, dict):
            return []
        types = []
        for t in map_data.get("things", []) or []:
            if not isinstance(t, dict):
                continue
            typ = t.get("type") or t.get("properties", {}).get("type")
            if typ:
                types.append(typ)

        seen = set()
        out = []
        for plugin in self.required_plugins_for_types(types):
            if id(plugin) not in seen:
                seen.add(id(plugin))
                out.append(plugin)

        for plugin in self.plugins + self._builtin_games:
            if id(plugin) in seen or not self._overrides(plugin, "map_uses_plugin"):
                continue
            try:
                if plugin.map_uses_plugin(map_data):
                    seen.add(id(plugin))
                    out.append(plugin)
            except Exception:
                self._log(
                    f"map_uses_plugin() failed for '{plugin.name}':\n"
                    f"{traceback.format_exc()}"
                )
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
        """Enable plugins required by entity types and map-specific activation hooks."""
        newly: List[FioPlugin] = []
        for plugin in self.required_plugins_for_map(map_data):
            if not self.is_enabled(plugin):
                self.set_enabled(plugin, True, auto=True)
                self._debug(
                    f"Auto-enabled plugin '{plugin.name}' for loaded level")
                newly.append(plugin)
        return newly

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
        for plugin in self.plugins + self._builtin_games:
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
        for plugin in self.plugins + self._builtin_games:
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
            cached = self._participants(hook)
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


def load_plugins(extra_roots=()):
    """Discover and load all plugins (idempotent). Call this early at startup.

    *extra_roots* forwards to :meth:`PluginManager.discover_and_load` for the
    case of a package that carries its own plugins.
    """
    get_manager().discover_and_load(extra_roots=extra_roots)