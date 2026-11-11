> Applies to Fio 3.5.0.1111.

A Fio plugin is a Python package under `plugins/` that adds entity types, I/O, console commands, menus, HUD text, renderers and per-tick behaviour without editing the editor or the engine. The same package is loaded by the editor, by Play Mode and by the standalone Android player. This page is the reference for the plugin API, version **1.9.0** (`plugins.api.API_VERSION`).

| Module | What it owns |
| --- | --- |
| `plugins/api.py` | Everything a plugin imports: `FioPlugin`, `EditorAPI`, `RuntimeAPI`, `TickContext`, `PropertySpec`, `GlobalStore`, `placement`, `io_def`, `prop`, `key_code`, `version_tuple`, `API_VERSION`, `API_VERSION_INFO` |
| `plugins/host.py` | `PluginHost`, `EventBus`, `Event` |
| `plugins/manager.py` | `PluginManager` (discovery, loading, enable state, dispatch), `get_manager()`, `load_plugins()` |
| `plugins/entitybase.py` | `Thing`: the entity base used when the `editor` package cannot be imported |
| `plugins/__init__.py` | Re-exports `FioPlugin`, `EditorAPI`, `RuntimeAPI`, `TickContext`, `io_def`, `get_manager`, `load_plugins` |

The shipped plugins are documented on their own pages: [Deathmatch](https://github.com/ViciousSquid/Fio/wiki/Deathmatch-Plugin), [BigWorld](https://github.com/ViciousSquid/Fio/wiki/BigWorld-Plugin) and [Tidy](https://github.com/ViciousSquid/Fio/wiki/Tidy-Plugin).

## Contents

- [Discovery and layout](#discovery-and-layout)
- [A minimal working plugin](#a-minimal-working-plugin)
- [Lifecycle and threads](#lifecycle-and-threads)
- [Enabling, disabling and auto-enable](#enabling-disabling-and-auto-enable)
- [Failure isolation](#failure-isolation)
- [API versions](#api-versions)
- [`FioPlugin`](#fioplugin)
- [`EditorAPI`](#editorapi)
- [`RuntimeAPI`](#runtimeapi)
- [`TickContext` and the HUD](#tickcontext-and-the-hud)
- [`PluginHost`, `EventBus`, `Event`](#pluginhost-eventbus-event)
- [Engine events](#engine-events)
- [Entities](#entities)
- [Property schemas](#property-schemas)
- [`GlobalStore`](#globalstore)
- [Module helpers](#module-helpers)
- [Reading a moving brush: `placement`](#reading-a-moving-brush-placement)
- [Console commands](#console-commands)
- [Menus and the pause menu](#menus-and-the-pause-menu)
- [World pause and actor pick](#world-pause-and-actor-pick)
- [Renderers](#renderers)
- [Per-map configuration and packages](#per-map-configuration-and-packages)
- [Plugins in the standalone player](#plugins-in-the-standalone-player)
- [`PluginManager` reference](#pluginmanager-reference)
- [Tests](#tests)

## Discovery and layout

`PluginManager.discover_and_load()` scans one directory, the installed `plugins/` package, with `pkgutil.iter_modules`. A `.fiopak` is never a source of plugin code.

```text
plugins/
  myplugin/
    __init__.py      # must expose PLUGIN (a FioPlugin instance) or get_plugin()
    plugin.py        # by convention: the FioPlugin subclass
    entities.py      # by convention: Thing subclasses
    ...
```

Rules applied to each entry, in name order (the load order is alphabetical and deterministic):

| Rule | Result when it fails |
| --- | --- |
| Must be a package (a directory with `__init__.py`) | ignored |
| Name must not start with `_` | ignored |
| Name (lower-cased) must not be in `FIO_DISABLED_PLUGINS` | skipped |
| `import plugins.<name>` must succeed | logged, skipped |
| Module must expose `PLUGIN`, or a callable `get_plugin()` that returns the instance | logged, skipped |
| The instance must be a `FioPlugin` | logged, skipped |
| `plugin.name` (lower-cased) must not be in `FIO_DISABLED_PLUGINS` | skipped |
| `plugin.name` must not equal an already loaded plugin's name (case-insensitive) | the first one loaded stays; the copy is ignored |
| `version_tuple(plugin.api_version) <= version_tuple(API_VERSION)` | logged (`needs API vX but this host provides vY`), skipped |
| `plugin.register(EditorAPI)` must not raise | logged, skipped (the plugin is not added) |

After `register` the manager calls `describe_properties()` and `menu_entries()`, appends the plugin to `manager.plugins`, and, once every package is loaded, checks `requires`: a plugin whose requirement (a plugin `name` or package directory name) is not loaded stays loaded but is disabled, with a logged message.

`load_plugins()` is idempotent. It is called when the `editor` package is first imported (`editor/__init__.py`) and again from `engine.logic_thread.LogicThread.__init__`; only the first call does work. A plugin package that is still mid-import when discovery reaches it (its own import triggered discovery) is deferred and picked up by the next `load_plugins()` call rather than dropped.

Environment variables:

| Variable | Effect |
| --- | --- |
| `FIO_DISABLED_PLUGINS` | Comma-separated plugin names or package directory names that are never loaded. Read once, when the manager is created. |
| `FIO_NO_PLUGINS` | `1`, `true`, `yes` or `on`: `LogicThread` sets `self.plugins = None`. Nothing is attached, bound, ticked or emitted to. |
| `FIO_PLUGIN_DEBUG` | Any value: informational plugin messages (loaded plugins, auto-enable, `api.log`, `host.log`) are shown. Failures are always shown. |

Messages go to the editor's debug console under the `Plugins` category, or to stdout as `[Plugins] ...` when the editor is absent.

## A minimal working plugin

This plugin adds a `Beacon` entity. The player lights one by looking at it from within `reach` and pressing Use; it has two inputs, one output and a console command. It loads and runs against API 1.9.0 as written.

`plugins/beacon/__init__.py`:

```python
from .plugin import BeaconPlugin

PLUGIN = BeaconPlugin()
```

`plugins/beacon/plugin.py`:

```python
import weakref

from plugins.api import FioPlugin, io_def, prop

try:                                    # editor: the full Thing
    from editor.things import Thing
except ImportError:                     # a host without the editor package
    from plugins.entitybase import Thing


class Beacon(Thing):
    """Lit by walking up to it and pressing Use."""

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties["type"] = "beacon"          # == normalised class name
        self.properties.setdefault("reach", 96.0)
        self.properties.setdefault("lit", False)


class BeaconPlugin(FioPlugin):
    name = "beacon"
    version = "1.0.0"
    description = "Beacons the player lights with Use."
    category = "Beacon"
    api_version = "1.4.0"               # register_console_command
    enabled = False                     # auto-enabled by a map that has a Beacon

    def __init__(self):
        self._lit = weakref.WeakKeyDictionary()     # logic -> count

    # load time: no Qt / OpenGL
    def register(self, api):
        api.register_entity(Beacon, menu_label="Beacon")
        api.register_properties("beacon", [
            prop("reach", "float", "Use reach", default=96.0, min=16, max=1024,
                 help="How close the player must be to light it."),
            prop("lit", "bool", "Starts lit", default=False),
        ])
        api.register_io("beacon",
                        inputs=[io_def("Light", "Light the beacon"),
                                io_def("Extinguish", "Put the beacon out")],
                        outputs=[io_def("OnLit", "Fired when the beacon is lit")])
        api.register_console_command("beacons", self._cmd_beacons,
                                     "beacons — how many beacons are lit")

    # once per logic thread
    def register_runtime(self, api):
        self._api = api
        api.register_input_handler("beacon", "Light", self._light)
        api.register_input_handler("beacon", "Extinguish", self._extinguish)

    def connect(self, host):
        host.on("player_death", lambda ev: host.log("player died"))

    # play session
    def on_play_start(self, logic):
        self._authored = [(b, b.properties.get("lit", False))
                          for b in self._api.entities_of_type("beacon")]
        self._lit[logic] = sum(1 for _b, lit in self._authored if lit)

    def on_play_stop(self, logic):
        for beacon, lit in self._authored:          # restore what play changed
            beacon.properties["lit"] = lit
        self._lit.pop(logic, None)

    def on_tick(self, logic, ctx):                  # logic thread
        beacon = self._api.raycast_from_crosshair(
            reach=1024.0, type_name="beacon",
            predicate=lambda b: not b.properties.get("lit"))
        if beacon is None:
            return
        eye = self._api.player_eye()
        near = sum((a - b) ** 2 for a, b in zip(eye, beacon.pos)) ** 0.5 \
            <= float(beacon.properties.get("reach", 96.0))
        if not near:
            return
        ctx.set_prompt("[E] Light the beacon")
        if ctx.use_pressed:
            self._light(beacon, "", logic)
            ctx.toast("Beacon lit", seconds=2.0)

    # I/O input handlers: handler(entity, parameter, logic), logic thread
    def _light(self, entity, parameter, logic):
        if not entity.properties.get("lit"):
            entity.properties["lit"] = True
            self._lit[logic] = self._lit.get(logic, 0) + 1
            self._api.fire_output(entity, "OnLit")

    def _extinguish(self, entity, parameter, logic):
        if entity.properties.get("lit"):
            entity.properties["lit"] = False
            self._lit[logic] = max(0, self._lit.get(logic, 0) - 1)

    # console: callback(args, main_window, logic, play_mode), UI thread
    def _cmd_beacons(self, args, main_window, logic, play_mode):
        if not play_mode:
            return "Enter Play first."
        return f"{self._lit.get(logic, 0)} beacon(s) lit."
```

What happens with it installed: the editor lists **Plugins > Beacon** with an **Enabled** toggle (off) and **Add entity (at origin): Beacon**; loading a map that contains a `beacon` entity enables the plugin for that map; in Play, `beacons` works in the console and is listed by `help` under *Plugin Commands*.

## Lifecycle and threads

```text
load_plugins()
  register(EditorAPI)            once per process
  describe_properties()          once, right after register
  menu_entries()                 once, right after describe_properties
LogicThread.__init__
  register_runtime(RuntimeAPI)   once per LogicThread, every loaded plugin
  connect(PluginHost)            once per LogicThread, every loaded plugin
Play starts   on_play_start(logic)       enabled plugins that override it
each tick     on_tick(logic, ctx)        enabled plugins that override it
Play stops    on_play_stop(logic)        enabled plugins that override it
toggle        on_enabled_changed(bool)   on a change of enabled state
```

| Call | Called from | Thread | Notes |
| --- | --- | --- | --- |
| `register`, `describe_properties`, `menu_entries` | `PluginManager._load_one` | The thread that first calls `load_plugins()`: the main thread, when the `editor` package is imported | Must not touch Qt or OpenGL: the same call happens in headless tools and in the Android player. |
| `register_runtime`, `connect` | `LogicThread.__init__` → `manager.attach_runtime(self)`, `manager.bind_host(self, kind="engine")` | The thread that constructs the `LogicThread` (the UI thread in the editor) | Every loaded plugin, enabled or not. `attach_runtime` is skipped when the logic thread has no `io_manager`. The editor builds one `LogicThread`; the player builds one per level it loads. |
| `on_play_start`, `on_play_stop` | `engine/logic_session.py: LogicSession._apply_play_mode_unlocked` → `dispatch_play_start` / `dispatch_play_stop` | The thread that toggles Play (the UI thread in the editor), holding `logic._tick_lock` | Runs after the core session is coherent; followed by the `play_start` / `play_stop` event. |
| `on_tick` | `LogicThread._tick_play_mode` → `manager.tick(...)` | The logic thread (the app thread in the player) | Last step of the gameplay sequence, after triggers. Not called during a cutscene, while the player is dead, or on the level-complete screen. Still called while a world pause is held. |
| I/O input handlers | `IOManager` input dispatch | The logic thread | `handler(entity, parameter, logic)`. |
| Event handlers | `EventBus.emit` | The thread that emits: see [Engine events](#engine-events) | |
| Console command callbacks | `editor/console_commands.py: _dispatch_plugin_command` | UI thread | Take `logic._tick_lock` before mutating the scene. |
| Menu, Tools and pause-menu callbacks, wizards, property tabs/sections, inspectors | Editor UI | UI thread | |
| `on_enabled_changed` | `PluginManager.set_enabled` | The caller's thread (UI thread for the menu and for level loading) | |

The tick is gated: `LogicThread` calls `manager.wants_tick()` first, which is true only when an enabled plugin overrides `on_tick` or something subscribed to the `tick` event. The answer is cached and recomputed when the enabled set or the subscription set changes. A hook a plugin does not override is never dispatched.

A plugin that changes entities or engine state during Play must restore them in `on_play_stop`: Play runs on the authored world, and what a plugin leaves behind is saved with the map.

## Enabling, disabling and auto-enable

`FioPlugin.enabled` is the live switch. A disabled plugin stays loaded (its entity classes, schemas and I/O definitions stay registered so maps still open), but:

- `on_play_start`, `on_tick` and `on_play_stop` are not dispatched to it;
- its input handlers and `host.on` subscriptions do not run (each is wrapped in a check of `plugin.enabled`);
- its console commands are not dispatched and not listed by `help`;
- its pause-menu items, property sections, key suggestions and entity inspectors are left out;
- its menu actions are hidden and its entities cannot be placed.

Always change the state through `PluginManager.set_enabled(plugin_or_name, enabled, auto=False)`: it bumps the generation the dispatch caches are keyed on and calls `on_enabled_changed`.

**Shipping disabled.** A plugin with `enabled = False` on its class is inert until a map needs it. All three shipped plugins do this.

**Auto-enable for a map.** `editor/editor_state.py` calls `get_manager().auto_enable_for_map(level_data)` when a level is loaded, and `player/world.py` does the same for a played package. `required_plugins_for_map(map_data)` returns:

1. every plugin that owns the `type` of an entry in `map_data["things"]` (ownership is recorded by `register_entity`, keyed by the class name lower-cased with underscores removed), then
2. every plugin that overrides `map_uses_plugin(map_data)` and returns true for this map (for plugins that attach behaviour to core entities: Tidy enables itself for a `prop` with a `tidy_category`).

Each of those that is disabled is enabled with `auto=True`. `EditorState.clear_scene` (File > New, and before another level loads) calls `disable_auto_enabled()`, which turns off exactly the plugins that were auto-enabled. A manual toggle clears the auto mark, so a plugin the user enabled by hand is never auto-disabled. Auto-enable never writes `settings.ini`.

**The editor's toggle.** **Plugins > *Plugin* > Enabled** calls `set_enabled` and writes the package names of all disabled plugins to `settings.ini`, `[Plugins] disabled` (comma-separated). At startup `editor/ui.py: _build_plugins_menu` disables every plugin named there. The menu re-reads the live state each time it opens, so an auto-enabled plugin shows as checked without being persisted.

## Failure isolation

Every call into plugin code is wrapped. A raise is logged with its traceback and the host carries on:

| Where | On exception |
| --- | --- |
| package import, `get_plugin()`, `register` | The plugin is not loaded. |
| `describe_properties`, `menu_entries` | Logged; the plugin still loads. |
| `register_runtime`, `connect` | Logged; the other plugins still attach. |
| `on_play_start`, `on_play_stop`, `on_tick` | Logged per call; the other plugins still run. A plugin that raises every tick logs every tick. |
| Event handler | Logged; the remaining handlers still run. |
| Input handler | Caught and logged by `IOManager` (`<type>.<input> handler failed`); the rest of the I/O dispatch continues. |
| Console command | Logged; the command counts as handled and prints nothing. |
| Pause-menu item | Logged; play continues. |
| `kv_suggestions` provider, entity inspector | Logged; the next provider is asked. |
| `on_enabled_changed`, `map_uses_plugin` | Logged. |
| `host.wrap` wrapper | Logged; the original method is called with the same arguments. |

## API versions

`API_VERSION = "1.9.0"`, `API_VERSION_INFO = (1, 9, 0)`. `FioPlugin.api_version` is the minimum a plugin needs (default `"1.0.0"`); a plugin that needs a newer API than the host is refused at load.

```python
def version_tuple(value: str) -> tuple
```

Parses a dotted version into a tuple of ints; a non-numeric component becomes `0` (`version_tuple("1.3.x") == (1, 3, 0)`).

| Version | What it added or changed |
| --- | --- |
| 1.2.0 | `PluginHost`, the event bus, the `connect(host)` hook. |
| 1.3.0 | `render.*` events, `register_renderer`, `register_extra_fields`, `register_property_tab`, the `FIO_NO_PLUGINS` switch. |
| 1.4.0 | `register_tools_action`, `register_console_command`. |
| 1.5.0 | `register_property_section`, `register_kv_suggestions`, `register_entity_inspector`; world pause (`session_runtime.set_world_paused`) and actor pick (`main_window.begin_actor_pick`). Additive. |
| 1.6.0 | Fio 3.0. `register_renderer` takes a factory called as `factory(config)` whose result satisfies the `engine.renderer.Renderer` protocol, and renderers swap live. Plugins written for 1.3–1.5 that register no renderer load unchanged; renderers written for 1.3–1.5 (`cls(texture_loader, grid_size, world_size, config)`) are not compatible and there is no shim. |
| 1.7.0 | `register_pause_menu_item`, the `pause_menu_opened` / `pause_menu_closed` events, `main_window.open_pause_menu()` / `close_pause_menu()`. Additive. |
| 1.8.0 | `placement(brush)`. Behaviour change: a mover's or door's `pos` is its authored placement and no longer changes in Play; the keys `original_pos`, `_rot_angle` and `rotation_yaw` are no longer written into brushes or saved. Read `placement(brush).centre`, `.authored` and `.spin` instead. |
| 1.9.0 | The renderer contract takes published tables only. The frame input's `terrain` is an `engine.terrain_frame.TerrainFrame`, never the `Terrain` object; `draw_face_highlight(projection, view, triangles)`, `draw_connection_lines(projection, view, segments, colours)` and `draw_collision_visualization(projection, view, vertices, runs)` take vertex arrays; `RenderStats` gains `terrain_visible_chunks`, `terrain_culled_chunks`, `terrain_triangles`, `terrain_drawn_slots`; `render_refs` / `entity_refs` are no longer in the frame input. Only renderer implementations are affected. |

## `FioPlugin`

Subclass it and expose one instance as the package's `PLUGIN`.

### Class attributes

| Attribute | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | `str` | `"unnamed"` | Unique identifier: logs, `find_plugin`, `FIO_DISABLED_PLUGINS`, `requires`, the 2D view's **Plugins > *name*** submenu. The menu bar shows it with the first letter capitalised. |
| `version` | `str` | `"0.0.0"` | Shown in the plugin's **About…** box. |
| `description` | `str` | `""` | Shown in **About…**. |
| `category` | `str` | `"Plugins"` | Default `ENTITY_CATEGORIES` group for `register_entity`. |
| `enabled` | `bool` | `True` | Live switch. Set `False` on the class to ship disabled. |
| `api_version` | `str` | `"1.0.0"` | Minimum API the plugin needs. |
| `requires` | `List[str]` | `[]` | Plugin names or package names that must be loaded, or this plugin is disabled. |

### Hooks

```python
def register(self, api: EditorAPI) -> None
def describe_properties(self) -> dict            # {entity_type: [PropertySpec, ...]}
def menu_entries(self) -> List[Tuple[str, type]] # extra (label, ThingClass) placement entries
def register_runtime(self, api: RuntimeAPI) -> None
def connect(self, host) -> None                  # host: plugins.host.PluginHost
def map_uses_plugin(self, map_data: dict) -> bool
def on_play_start(self, logic) -> None
def on_play_stop(self, logic) -> None
def on_tick(self, logic, ctx: TickContext) -> None
def on_enabled_changed(self, enabled: bool) -> None
```

| Hook | When | Purpose |
| --- | --- | --- |
| `register` | Load | Declare entities, I/O, schemas, menus, commands, renderers. |
| `describe_properties` | Load, after `register` | Alternative to `register_properties`: return `{entity_type: [PropertySpec]}`. |
| `menu_entries` | Load, after `describe_properties` | Extra placement entries beyond those `register_entity` adds. |
| `register_runtime` | Each `LogicThread` construction | Register input handlers; keep the `RuntimeAPI` for scene queries. |
| `connect` | Each `LogicThread` construction | Subscribe to events, publish or look up services. |
| `map_uses_plugin` | Level load | Return true when this map needs the plugin although it holds none of its entity types. The base method returns `None`. Only consulted when overridden. |
| `on_play_start` | Play starts | Build per-session state. |
| `on_play_stop` | Play stops | Restore everything the plugin changed. |
| `on_tick` | Every Play tick | Per-frame work; keep it cheap. |
| `on_enabled_changed` | The enabled state changes | Acquire or release resources. Not called for the initial state. |

`logic` is the `engine.logic_thread.LogicThread`. Its runtimes are the engine's own objects, not a plugin facade: `logic.editor_state` (`things`, `brushes`), `logic.player_runtime`, `logic.session_runtime`, `logic.combat_runtime`, `logic.interaction_runtime`, `logic.prop_runtime`, `logic.trigger_runtime`, `logic.collision_runtime`, `logic.mover_runtime`, `logic.world_runtime`, `logic.render_runtime`, `logic.cutscene_runtime`, `logic.camera`, `logic.io_manager`, `logic.game_state`, `logic.monster_ai`, `logic.plugins` (the manager, or `None`).

## `EditorAPI`

Handed to `register`. Every method is safe in a process without the editor: the parts that need `editor.things` or `editor.io_system` are skipped there.

### Entities and I/O

```python
def register_entity(self, cls: type, category: Optional[str] = None,
                    menu_label: Optional[str] = None, placeable: bool = True) -> type
```

Records the plugin as the owner of `cls` (key: class name lower-cased, underscores removed), adds `cls` to `editor.things.ENTITY_TYPES` under its class name and to `ENTITY_CATEGORIES[category or plugin.category]`, picks up a `PROPERTY_SCHEMA` class attribute if the class has one, and, when `placeable`, adds a placement entry labelled `menu_label or cls.__name__`. Returns `cls`.

```python
def register_io(self, entity_type: str, inputs: List[Any], outputs: List[Any])
```

Sets the I/O definitions of `entity_type` (the entity's `properties["type"]`) in `editor.io_system`. This **replaces** whatever the type declared; `None` entries are dropped. Use it only for types the plugin owns.

```python
def extend_io(self, entity_type: str, inputs=(), outputs=())
```

Adds definitions to a type the plugin does not own (a core entity). Names already declared, compared case-insensitively, are left alone, so the call is idempotent.

Both calls are recorded, and `PluginManager.reapply_registrations()` replays them (and re-adds the entity classes to `ENTITY_TYPES` / `ENTITY_CATEGORIES`) if the process-wide registries are reset.

```python
def register_singleton_entity(self, entity_type: str) -> None
```

At most one instance per map. Placement (`editor/view_2d.py: _singleton_blocked`) refuses a second, selects the existing one and shows a toast.

```python
def register_entity_wizard(self, entity_type: str, factory) -> None
```

`factory(parent) -> dict | None` runs when the entity is placed and returns its initial properties, or `None` to cancel the placement.

### Properties

```python
def register_properties(self, entity_type: str, specs: List[PropertySpec])
def register_extra_fields(self, entity_type: str, specs)
def register_property_tab(self, label: str, factory, entity_type=None)
def register_property_section(self, label: str, factory, entity_type=None,
                              expanded: bool = False) -> None        # API 1.5.0
def register_kv_suggestions(self, provider) -> None                  # API 1.5.0
def register_entity_inspector(self, provider, entity_type=None) -> None  # API 1.5.0
```

| Method | Contract |
| --- | --- |
| `register_properties` | The typed schema of a plugin entity's whole panel (see [Property schemas](#property-schemas)). An empty list is ignored. A later call for the same type replaces the earlier one. |
| `register_extra_fields` | Fields appended after the stock rows of any entity, built-in ones included. Calls accumulate. |
| `register_property_tab` | `factory(thing) -> QWidget`: a whole tab in the property panel, for `entity_type` or, with `None`, for every entity. |
| `register_property_section` | Same factory contract; a collapsible section at the end of the Properties tab. A collapsed section's factory does not run until it is first opened. Dropped while the plugin is disabled. |
| `register_kv_suggestions` | `provider(store) -> rows` of `(label, key, default_value)` or `(label, key, default_value, tooltip)`, offered in the LogicState editor's **Preset key** picker. `store` is the LogicState entity (`store.properties["store_name"]` tells stores apart). Rows shorter than three items, and keys already offered by an earlier provider, are skipped. |
| `register_entity_inspector` | `provider(entity, logic) -> dict | None`; `logic` is the running logic thread or `None` outside Play. The first provider that returns a non-empty document is shown; with none the inspector lists the entity's public properties. |

An inspection document:

```python
{"title":    "Gate Keeper",
 "subtitle": "patrolling · awake",
 "sections": [("Vitals", [("Health", 80), ("Speed", 1.5)]),
              ("Goals",  [("Patrol", "", 0.9), ("Rest", "", 0.2)])]}
```

A row is `(label, value)`, or `(label, value, fraction)` to draw a 0..1 bar. Open the inspector with `main_window.show_entity_inspector(entity)`.

### Menus, commands, renderers

```python
def register_menu_action(self, label: str, callback: Callable, tooltip: str = "") -> None
def register_tools_action(self, label: str, callback: Callable, tooltip: str = "") -> None   # API 1.4.0
def register_console_command(self, name: str, callback: Callable, help_text: str = "") -> None  # API 1.4.0
def register_pause_menu_item(self, label: str, callback: Callable,
                             close_menu: bool = True) -> None        # API 1.7.0
def register_renderer(self, name: str, cls) -> bool                  # API 1.6.0
```

See [Menus and the pause menu](#menus-and-the-pause-menu), [Console commands](#console-commands) and [Renderers](#renderers).

`register_tools_action` records `(plugin, label, callback, tooltip)`, readable through `PluginManager.tools_actions()`. The editor in this version does not build menu items from that list; use `register_menu_action` for an action the user can reach.

### Globals and logging

```python
@property
def globals(self) -> GlobalStore
def global_get(self, key, default=None, store: str = "plugins")
def global_set(self, key, value, store: str = "plugins")
def log(self, message: str)          # shown only with FIO_PLUGIN_DEBUG
```

## `RuntimeAPI`

Handed to `register_runtime`. `api.logic` is the logic thread and `api.io_manager` its `IOManager`. The object stays valid for the life of that logic thread; keep it.

```python
def register_input_handler(self, entity_type: str, input_name: str, handler: Callable)
def fire_output(self, entity, output_name: str, value: Optional[str] = None)
def entities_of_type(self, type_name: str) -> List
def things_near(self, pos, radius: float, type_name: Optional[str] = None) -> List
def player_eye(self)                 # (x, y, z)
def player_forward(self)             # (x, y, z), includes pitch
def raycast_from_crosshair(self, reach: float = 160.0, aim_dot: float = 0.86,
                           type_name: Optional[str] = None,
                           predicate: Optional[Callable] = None)
def spawn(self, cls, pos=None, properties: Optional[dict] = None)
def despawn(self, entity) -> bool
@property
def globals(self) -> GlobalStore
def global_get(self, key, default=None, store: str = "plugins")
def global_set(self, key, value, store: str = "plugins")
def log(self, message: str)
```

| Method | Behaviour |
| --- | --- |
| `register_input_handler` | Registers `handler(entity, parameter, logic)` for `(entity_type, input_name)`. The I/O manager keys handlers by the lower-cased pair, one handler per pair: a second registration replaces the first. The handler runs only while the plugin is enabled. |
| `fire_output` | `io_manager.fire_output(entity, output_name, value)`: `value` is passed to connections whose authored parameter is blank. |
| `entities_of_type` | Entities of `logic.editor_state.things` whose `properties["type"]`, lower-cased with underscores removed, equals `type_name` normalised the same way. A linear scan. |
| `things_near` | Entities within `radius` of `pos` (3D distance), optionally of one type. A linear scan. |
| `player_eye` | `player.pos` raised by `player.camera_height` (40 when the attribute is missing). `(0, 0, 0)` with no player. |
| `player_forward` | `(sin a · cos p, sin p, cos a · cos p)` for `player.angle` `a` and `player.pitch` `p` (radians). `(0, 0, 1)` with no player. |
| `raycast_from_crosshair` | The nearest entity whose distance from the eye is in `(0.001, reach]` and whose direction has a dot product of at least `aim_dot` with the look vector, filtered by `type_name` and `predicate(entity)`. It is a cone test on entity positions: geometry does not occlude it. Returns `None` when nothing qualifies. |
| `spawn` | `cls(pos=list(pos) or [0, 0, 0], properties=dict(properties or {}))`, appended to `logic.editor_state.things`; rebuilds the session's entity caches under `logic._tick_lock` when Play is running; emits `entity_spawned`. Returns the entity, or `None` if construction raised. |
| `despawn` | Removes the entity from `things` and rebuilds the caches. `False` if it was not in the scene. |

Entities spawned during Play belong to the plugin: remove them in `on_play_stop`.

## `TickContext` and the HUD

One instance per tick, shared by every plugin's `on_tick` and by `tick` event handlers.

| Field | Type | Meaning |
| --- | --- | --- |
| `delta` | `float` | Seconds this tick covers. |
| `use_pressed` | `bool` | The Use key's press edge for this tick. |
| `keys` | `frozenset` | Held keys as Qt key codes. |
| `interaction_consumed` | `bool` | True when core interaction code already set a HUD prompt this tick. |
| `logic` | | The logic thread. |

```python
def key_down(self, name) -> bool
def set_prompt(self, text: str, priority: int = 0) -> bool
def toast(self, text: str, seconds: float = 2.0) -> None
```

`key_down` accepts a raw code or a name resolved by [`key_code`](#module-helpers).

The HUD has one prompt line, `logic.interaction_runtime.current_hud_message`:

- `set_prompt(text, priority)` writes it and returns `True`, unless the core already claimed the line this tick and `priority <= 0`, or a plugin already set a prompt with a higher priority this tick. Equal priority overwrites: the later plugin wins.
- `toast(text, seconds)` stores a timed message on the session (`logic._plugin_hud`), one at a time. After every plugin has ticked, the manager shows the live toast only if no prompt was set this tick, the core did not claim the line, and the line is empty.

Anything beyond one line of text is drawn in a `render.overlay` handler with the event's `QPainter`.

## `PluginHost`, `EventBus`, `Event`

`connect(host)` receives one `PluginHost` per plugin per logic thread. `host.kind` is `"engine"` for a `LogicThread` (the editor and the Android player) and `"player"` for `player/plugin_host.py`.

| Member | Returns |
| --- | --- |
| `host.logic`, `host.engine` | The logic object the host is bound to. |
| `host.scene` | The live `logic.editor_state.things` list. |
| `host.io` | `logic.io_manager`, or `None`. |
| `host.globals` | The `GlobalStore`. |
| `host.game_state` | `logic.game_state`, or `None`. |
| `host.monster_ai` | `logic.monster_ai`, or `None`. |
| `host.editor` | The editor main window when the `editor` package is importable and the target exposes one, else `None`. |
| `host.events` | The process-wide `EventBus`. |
| any other attribute | Read from the logic object; `AttributeError` if it has none. |

```python
def get(self, path: str, default=None)               # dotted lookup; default if a step is None/missing
def on(self, event: str, handler: Callable, priority: int = 0) -> Callable
def off(self, event: str, handler: Callable) -> None
def emit(self, event: str, **data)                   # returns the Event, or None with no subscribers
def provide(self, name: str, value) -> None          # publish a service
def service(self, name: str, default=None)           # look one up
def wrap(self, obj, method_name: str, wrapper: Callable) -> bool
def register_renderer(self, name: str, cls) -> bool
def log(self, message: str) -> None                  # shown only with FIO_PLUGIN_DEBUG
```

- `on` wraps the handler so it runs only while the plugin is enabled, and returns the wrapper's registration. `off` takes the original handler.
- `provide` / `service` use one dictionary on the manager (`manager.services`), shared by every plugin and every logic thread. A session-scoped service must be cleared when the session ends (BigWorld sets `bigworld` back to `None` in `on_play_stop`).
- `wrap` replaces `obj.method_name` with a function that calls `wrapper(original, *args, **kwargs)`. It returns `False` when the attribute is not callable. The host does not undo a wrap: restore the original in `on_play_stop`.
- When the manager binds to a different logic object than last time, it clears the event bus and the host table first, so subscriptions never stack across sessions.

`EventBus`:

```python
def on(self, event: str, handler: Callable, priority: int = 0) -> Callable
def off(self, event: str, handler: Callable) -> None
def has(self, event: str) -> bool
def emit(self, event: str, **data) -> Optional[Event]
def clear(self) -> None
gen: int        # bumped on every subscription change
```

Handlers run highest priority first; equal priorities run in subscription order. `emit` returns `None` without building an `Event` when the name has no subscribers. Subscribing to a name nothing emits is allowed.

`Event` has `name`, `data` (the keyword payload), `get(key, default=None)`, item access (`ev["key"]`, `None` when missing), attribute access (`ev.key`, `AttributeError` when missing) and `stop()`, which keeps the event from reaching the remaining handlers. A handler may write into `ev.data`; the emitter receives the same `Event` back.

## Engine events

Engine events go through `LogicThread._plugin_emit(event, **data)`, which adds `logic=<the logic thread>` to every payload.

| Event | Payload (besides `logic`) | Emitted by | Thread |
| --- | --- | --- | --- |
| `play_start`, `play_stop` | | `LogicSession._apply_play_mode_unlocked`, after the lifecycle hooks | Thread toggling Play |
| `player_spawn` | `start` (the PlayerStart) | `engine/logic_session.py`, after `OnPlayerSpawn` fires | Thread toggling Play |
| `world_rebuilt` | | `engine/logic_world.py`, at the end of the entity-cache rebuild (session start, spawn, despawn) | Caller of the rebuild |
| `tick` | `ctx`, `delta`, `use_pressed` | `PluginManager.tick`, after every `on_tick` | Logic |
| `pause_menu_opened`, `pause_menu_closed` | | `LogicThread._announce_pause_menu` | Logic |
| `player_shoot` | `weapon` (the active weapon id) | `engine/logic_combat.py` | Logic |
| `noise` | `pos`, `source`, `loudness` | `engine/logic_combat.py` | Logic |
| `player_damage` | `damage`, `health` | `engine/logic_triggers.py: _apply_player_damage` | The thread that applied the damage (logic, or monster AI) |
| `player_death` | | same, when the damage killed the player | same |
| `trigger_enter` | `trigger` (brush), `action`, `trigger_id`, `activator_type` | `engine/logic_triggers.py` | Logic |
| `trigger_exit` | `trigger`, `trigger_id`, `activator_type` | `engine/logic_triggers.py` | Logic |
| `door_open` | `door` (brush), `door_idx` | `engine/logic_movers.py` | Logic |
| `portal_transit` | `portal_from`, `portal_to` | `engine/logic_portals.py` | Logic |
| `prop_collected` | `prop`, `collect_type`, `value`, and `collector` when an actor other than the player collected it | `engine/prop_runtime.py` | Logic |
| `entity_spawned` | `entity`, `by` (plugin name) | `RuntimeAPI.spawn` | Caller |
| `render.pre_scene` | `viewport`, `renderer`, `projection`, `view`, `camera_pos`, `play_mode` | `engine/qt_game_view.py`, before the scene is drawn | UI (GL context current) |
| `render.post_scene` | same | after the 3D scene, before the 2D painter opens | UI (GL context current) |
| `render.overlay` | `viewport`, `painter` (`QPainter`), `width`, `height`, `play_mode` | last thing before the frame's painter closes; the pause menu draws over it | UI |

The `render.*` events carry no `logic` key; reach it through `ev.viewport.logic_thread`. They are emitted in the editor as well as in Play (check `play_mode`), and only by the Qt viewport: the Android player has no `QPainter` and never emits them. Each `render.*` emit is skipped entirely when `has_listeners` is false.

Plugins emit their own events with `host.emit`; the Deathmatch plugin's `deathmatch.*` events are an example.

## Entities

A plugin entity is a `Thing` subclass: `editor.things.Thing` in the editor, `plugins.entitybase.Thing` where the editor package cannot be imported. Import it with the `try` / `except ImportError` shown in the minimal plugin. To build on a core Prop, subclass `engine.prop_entity.Prop` (also reachable as `plugins.entitybase.Prop`).

The contract:

- The constructor is `__init__(self, pos=None, properties=None)` and must accept an empty call: `register_entity` and the placement path both build instances this way.
- `properties["type"]` must equal the class name lower-cased with underscores removed. `Thing.from_dict` finds the class from a map record by that rule, and `plugin_for_type` / `entity_class_for_type` use the same key.
- State that must be saved lives in `properties` (JSON values). `Thing` supplies `name`, a stable `id` (UUID) and `_io_connections`.
- `pixmap_path` (class attribute) is the 2D view icon.
- An optional `on_editor_placed(self, state)` method is called by the editor right after the entity is placed (`state` is the `EditorState`).
- `PROPERTY_SCHEMA` (class attribute, a list of `PropertySpec`) is registered automatically by `register_entity`.

`plugins.entitybase.Thing` provides `pos` (a `TrackedPosition`), `properties`, `name`, `get_io_connections()`, `to_dict()` and the `from_dict` classmethod, and nothing else: no pixmaps, no Qt.

## Property schemas

```python
@dataclass
class PropertySpec:
    name: str
    type: str = "string"       # int, float, bool, string, enum, vec3, asset
    label: str = ""
    default: Any = None
    min: Optional[float] = None
    max: Optional[float] = None
    choices: Optional[List[Any]] = None    # enum
    help: str = ""
    group: str = ""            # section heading for consecutive specs

    def apply_default(self, properties: dict) -> None
    def coerce(self, value)

def prop(name, type="string", label="", default=None, min=None, max=None,
         choices=None, help="", group="") -> PropertySpec
```

`apply_default` sets the default when the key is missing and the default is not `None`. `coerce` never raises:

| `type` | `coerce` result |
| --- | --- |
| `int` | `int(float(value))`, clamped to `min` / `max` |
| `float` | `float(value)`, clamped |
| `bool` | a `bool` as it is; otherwise true for the strings `1`, `true`, `yes`, `on` (any case) |
| `enum` | `str(value)` if it is one of `choices`, else the default, else the first choice |
| others | the value unchanged |

An uncastable `int` / `float` falls back to the default, or to `0` / `0.0`.

The schema is metadata: the stored value is whatever is in `properties`. A schema does not seed defaults on a new entity; the entity's constructor does (`setdefault`).

## `GlobalStore`

Cross-level string storage, grouped by store name (default `"plugins"`).

```python
def get(self, key, default=None, store: str = "plugins")
def set(self, key, value, store: str = "plugins") -> None
def delete(self, key, store: str = "plugins") -> bool
def all(self, store: str = "plugins") -> dict
def keys(self, store: str = "plugins") -> list
```

With the editor package present it reads and writes `editor.things.LogicState._persistent_registry`, the registry map `LogicState` entities use, so passing a map's `store_name` shares values with map logic. Without it, a process-local dictionary is used. `set` stores `str(value)`; `get` and `all` return strings (a typed value a map stored is formatted on the way out). The store lasts for the process, across level loads; it is not written to disk by this class.

## Module helpers

```python
def io_def(name: str, description: str = "", param_type: str = "")
```

Builds an `editor.io_system.IODef`. `param_type` is one of `""`, `"float"`, `"int"`, `"string"`, `"bool"`, `"color"`. Returns `None` when the I/O system cannot be imported; `register_io` and `extend_io` drop `None` entries.

```python
def key_code(name) -> Optional[int]
```

An `int` passes through. A single character maps to its upper-case ASCII code (`"w"` → `0x57`). Named keys: `space`, `shift`, `ctrl` / `control`, `alt`, `tab`, `backspace`, `return`, `enter`, `escape` / `esc`, `up`, `down`, `left`, `right`. Anything else is `None`.

## Reading a moving brush: `placement`

```python
from plugins.api import placement

def placement(brush)        # -> engine.brush_placement.Placement
```

Since API 1.8.0 a brush's `pos` is where it was authored and does not change while a mover or door moves it. `placement(brush)` (implemented by `engine/brush_placement.py: placement`) answers where the brush is:

| Field | Value |
| --- | --- |
| `authored` | The authored centre `(x, y, z)`: a box's `pos`, an angled brush's planes' centre. |
| `centre` | Where it is now: the authored centre carried by the mover's or door's runtime transform. Equal to `authored` for anything that is not moving. |
| `spin` | `(axis_x, axis_y, axis_z, angle_degrees)` while a mover spins, else `None`. |
| `half` | The shape's unturned half-extents. |
| `bounds` | The derived world AABB `(lo_x, lo_y, lo_z, hi_x, hi_y, hi_z)`, a spinning brush's turned box included: what culling, collision and picking use. |

`Placement` is a tuple subclass, so it also unpacks in that order.

```python
p = placement(lift_brush)
top_of_lift = p.centre[1] + p.half[1]
travelled = [c - a for c, a in zip(p.centre, p.authored)]
```

`plugins/deathmatch/moving_navigation.py: live_centre` is the shipped use: bots board lifts at `placement(brush).centre`.

## Console commands

```python
api.register_console_command(name, callback, help_text="")
callback(args: str, main_window, logic, play_mode: bool) -> str | None
```

- `name` is stored stripped and lower-cased. A second registration of a name replaces the first, whichever plugin made it.
- The console tries its built-in commands first; an unknown name is offered to `PluginManager.dispatch_console_command`. A plugin command cannot replace a built-in one.
- `args` is the rest of the line, unsplit. `logic` is the viewport's logic thread or `None`. `play_mode` is whether the 3D view is in Play.
- A truthy return value is printed to the console as an Info line.
- The callback runs on the UI thread. Take `logic._tick_lock` before mutating the scene.
- `help` lists enabled plugins' commands under **Plugin Commands** with their `help_text`.
- In the Android player there is no console; `player/plugin_ui/arena.py` calls the same callbacks through `dispatch_console_command` from its pause menu.

## Menus and the pause menu

**Menu bar > Plugins** (`editor/ui.py: _build_plugins_menu`, inserted before Help) has one submenu per loaded plugin, in load order:

1. the plugin's `register_menu_action` entries (hidden while the plugin is off): `callback(main_window)`;
2. **Enabled** (checkable);
3. **Add entity (at origin):** one entry per placement entry, which places the entity at `(0, 40, 0)`;
4. **About…**

**2D view right-click > Plugins > *plugin.name*** lists the placement entries of enabled plugins and places the entity at the click. Both paths go through `View2D._place_plugin_entity`: enabled check, singleton refusal, entity wizard, undo checkpoint, `on_editor_placed`, selection.

**Pause menu** (API 1.7.0). Esc in Play opens it; it holds the world pause owner `"pause_menu"` while open. Its first page is Resume, Save Game, Load Game, Options, then every enabled plugin's items in registration order, then Exit to Editor (Quit in a played package).

```python
api.register_pause_menu_item(label, callback, close_menu=True)
callback(main_window, logic)        # UI thread, world paused
main_window.open_pause_menu()       # what Esc does; False outside Play
main_window.close_pause_menu()      # resume; False when it was not open
main_window.view_3d.pause_menu_active
logic.session_runtime.pause_menu_open
```

An item with an empty label is not recorded. With `close_menu=True` the menu closes after the callback; pass `False` for an item that opens its own window and call `main_window.close_pause_menu()` from there. Opening and closing emit `pause_menu_opened` / `pause_menu_closed` on the logic thread.

## World pause and actor pick

Part of API 1.5.0, called on the engine objects a plugin already holds:

```python
logic.session_runtime.set_world_paused(owner, paused=True)   # hold or release a pause for owner
logic.session_runtime.world_paused                           # True while any owner holds one
logic.session_runtime.world_pause_owners()                   # frozenset of owners
main_window.begin_actor_pick(on_pick=None)                   # arm click-to-pick of an actor
main_window.view_3d.actor_pick_active
```

While any owner holds a pause, a Play tick advances nothing in the world (player, movers, doors, I/O timers, triggers, props, physics, projectiles, monster AI), and plugins still tick. Each owner releases only its own request; entering or leaving Play drops them all. `begin_actor_pick` pauses the world, frees the cursor and calls `on_pick(entity)` for the next actor clicked (default: open the Entity Inspector); Esc or a right-click cancels.

## Renderers

```python
api.register_renderer(name, factory) -> bool      # also host.register_renderer
```

Adds `factory` to the renderer registry (`engine/renderer/registry.py`), replacing any entry of the same name. `"Forward"` is the built-in entry and `DEFAULT_RENDERER`. Returns `False`, registering nothing, in a process where `engine.qt_game_view` cannot be imported (the Android player).

The host calls `factory(config)` with its GL context current; `config` is the application's `ConfigParser` or `None`. The result must satisfy `engine.renderer.Renderer` (`engine/renderer/api.py`):

| Group | Members |
| --- | --- |
| Lifecycle | `ready` (property), `cleanup()` |
| Frame | `render_scene(projection, view, camera_pos, primary_selection, config, clear=True, brush_slots=None)` |
| Settings | `view_distance`, `shadows_enabled`, `water_quality` (`'cheap'` or `'expensive'`), `set_grid(world_size, grid_size)` |
| Resources | `load_texture(texture_name, subfolder) -> int`, `set_sprite_textures(textures)`, `get_loaded_model(filename)` |
| Diagnostics | `render_stats` (an `engine.renderer.RenderStats`) |
| Post-scene | `draw_billboards`, `draw_player_glasses`, `draw_bullet_marks`, `draw_component_overlay`, `draw_face_highlight(projection, view, triangles)`, `draw_connection_lines(projection, view, segments, colours)`, `draw_collision_visualization(projection, view, vertices, runs)` |
| Optional | `animate_textures(clock)` |

The frame input (`config`, documented key by key in `engine.renderer.FRAME_INPUT`) is tables only: `render_table` / `entity_table` with their slot arrays, `terrain` (a `TerrainFrame` or `None`), `decals`, the selection and the view settings. A renderer receives no authored `Brush`, `Thing` or `Terrain` object and writes nothing on what it is handed.

Resource lifetime: no GL name survives the renderer that created it. The host discards the handles it was given before calling `cleanup()`, and a replacement renderer receives fresh resource tables.

`r_renderer` lists the registered renderers and `r_renderer <name>` swaps live (`QtGameView.switch_renderer`), in the editor or in Play. If the factory raises or the new renderer is not `ready`, the current one keeps running. Map logic cannot run `r_renderer`.

`engine.renderer.core.RendererCore` is optional reusable infrastructure; a renderer need not inherit from anything in Fio. The contract in full, GL state rules and a walk through a complete renderer are in the [Renderer Development Guide](https://github.com/ViciousSquid/Fio/wiki/Renderer-Development-Guide); the code is `docs/examples/deferred_renderer/` (copy the package into `plugins/` and run `r_renderer Deferred`). A renderer that draws the terrain or implements the three array-taking members declares `api_version = "1.9.0"`.

## Per-map configuration and packages

**Configuration in an entity.** The supported way to give a plugin per-map settings is a singleton entity whose properties are the settings: `deathmatchsettings` and `bigworldsettings` are both this. It is saved, loaded, undone and packaged by the ordinary entity paths, its type auto-enables the plugin, and `register_singleton_entity` keeps it unique.

**Top-level map keys read by the player.** `player/world.py: PlayerWorld.__init__` also reads two optional keys of the map JSON:

```json
{
  "required_plugins": ["tidy"],
  "plugin_config": {"tidy": {"some_key": 1}}
}
```

Each name in `required_plugins`, and in the package manifest's `plugins` list (`FioPackage.required_plugins`), is looked up with `find_plugin`; a missing one raises `PackageError("This world requires an installed plugin: <name>")`, otherwise the plugin is enabled. For a name that has a dictionary under `plugin_config`, the player calls `plugin.config.update(<that dictionary>)`: the plugin must define a `config` dict attribute (`FioPlugin` does not, and none of the shipped plugins do). The editor does not read either key.

**Packages.** A `.fiopak` never carries plugin code. `FioPackage` refuses to open an archive that has any entry under a top-level `plugins/` directory, compared case-insensitively and after resolving `..`. A world names the plugins it needs; they must be installed where it is played.

## Plugins in the standalone player

The [Android player](https://github.com/ViciousSquid/Fio/wiki/Fio-Player-(Android)) runs the engine's own `LogicThread`, so plugins load and run through the same manager calls as in the editor: `load_plugins()` and `attach_runtime` / `bind_host(kind="engine")` in `LogicThread.__init__`, `auto_enable_for_map` in `PlayerWorld.__init__`, the lifecycle hooks from `apply_play_mode`, and `on_tick` from `LogicThread._tick`, which the player calls from its app thread at a fixed 1/60 s step. The APK ships the `plugins/` directory, with `plugins/entitybase.py` replaced by an alias of the engine's full `Thing`.

What differs from the editor:

| Area | In the player |
| --- | --- |
| Qt | Not installed. `main_window` is `None` wherever a callback receives it; `render.*` events are never emitted; menu actions, Tools actions, property tabs/sections, wizards and inspectors are never invoked. |
| HUD | `current_hud_message` (so `set_prompt` and `toast`) is drawn as the HUD prompt. Richer plugin UI is reimplemented in `player/plugin_ui/` for the shipped plugins. |
| Console commands | No console. `player/plugin_ui/arena.py` calls `dispatch_console_command(name, args, logic, None, True)` for Deathmatch's commands from the pause menu. |
| Pause-menu items | `manager.pause_menu_items()` is read by `player/plugin_ui/arena.py` to find Deathmatch's restart item and call its callback with `(None, logic)`. |
| Renderers | `register_renderer` returns `False`. |
| Plugin data files | Images, models and sounds inside the installed `plugins/` directory are served by `player/runtime/plugin_assets.py: PluginAssetPackage` when the world package does not have them. |
| Level data | `logic.editor_state.get_level_data()` returns a deep copy of the loaded map JSON. |

`player/plugin_host.py: PlayerPluginHost` is a second, lighter host that drives the lifecycle against a camera bridge (`host.kind == "player"`, a no-op I/O manager, the `camera.player_view` event). The player application does not use it; it is exercised by `player/tests/test_plugin_host_security.py` and `plugins/tidy/tests/`.

## `PluginManager` reference

```python
from plugins.manager import get_manager, load_plugins
```

| Method | Purpose |
| --- | --- |
| `discover_and_load()` | Discover and load every plugin package; idempotent. |
| `plugins` | Loaded plugins, in load order. |
| `find_plugin(name)` | By `name` or package directory name, case-insensitive. |
| `is_enabled(plugin)`, `set_enabled(plugin_or_name, enabled, auto=False)` | Enabled state. |
| `plugin_for_type(type)`, `entity_class_for_type(type)` | Owner and class of an entity `type`. |
| `required_plugins_for_types(types)`, `required_plugins_for_map(map_data)` | What a map needs. |
| `auto_enable_for_types(types)`, `auto_enable_for_map(map_data)`, `disable_auto_enabled()` | Level-driven enabling; each returns the plugins it changed. |
| `plugin_package_dir(plugin)`, `plugin_package_name(plugin)` | Where the plugin lives. |
| `menu_entries()`, `menu_actions()`, `tools_actions()`, `pause_menu_items()` | What plugins registered for the UI. |
| `property_schema_for(type)`, `extra_fields_for(type)`, `property_tabs_for(type)`, `property_sections_for(type)` | Property panel data. |
| `kv_suggestions(store)`, `inspect_entity(entity, logic=None)` | LogicState presets and inspector documents. |
| `entity_wizard_for(type)`, `is_singleton_entity(type)` | Placement rules. |
| `has_console_command(name)`, `console_commands()`, `dispatch_console_command(name, args, logic=None, main_window=None, play_mode=False)` | Console; dispatch returns `(handled, reply)`. |
| `attach_runtime(logic)`, `bind_host(target, kind="engine")`, `host_for(plugin)` | Session wiring. |
| `dispatch_play_start(logic)`, `dispatch_play_stop(logic)`, `dispatch_tick(logic, ctx)`, `tick(logic, use_pressed=False, interaction_consumed=False, delta=0.0, keys=None)`, `wants_tick()` | Lifecycle dispatch. `keys` may be a callable returning the held keys; it is called only when a context is built. |
| `emit(event, **data)`, `has_listeners(event)`, `events`, `services`, `global_store` | Events, services, globals. |
| `reapply_registrations()` | Replay recorded I/O registrations and entity classes into the process-wide registries; returns the number of I/O registrations replayed. |
| `register_builtin_game(game)`, `builtin_games()`, `builtin_menu_entries()` | A built-in layer: an object with the plugin hooks that is always on, never listed in the Plugins menu and never version-gated. Nothing in this repository registers one. |

## Tests

| Path | Covers |
| --- | --- |
| `tests/plugins/test_plugin_contract.py` | Discovery and the load gates (no `PLUGIN`, wrong type, newer API, `get_plugin`, `register` raising, `FIO_DISABLED_PLUGINS`), entity ownership and type normalisation, auto-enable and its revert, the event bus (priority, `stop`, a raising handler, unsubscribe), host binding and rebinding, services, `wants_tick`, failure isolation of the lifecycle hooks, runtime attach for disabled plugins, the global store, and that importing the API pulls in neither Qt nor OpenGL. |
| `tests/plugins/test_api_1_5_extensions.py` | Property sections, key suggestions, entity inspectors, a 1.4 plugin loading unchanged, a plugin that needs a newer API being refused, extensions dropping out while their plugin is disabled. |
| `tests/plugins/test_registration_replay.py` | `reapply_registrations` after the I/O registry is reset; `extend_io` merging. |
| `tests/plugins/test_plugin_menu_visibility.py` | The Plugins menu: capitalised names, actions hidden while a plugin is off, a persisted disable, the toggle following an auto-enable, **Add entity (at origin)**, the 2D context menu's grouping. |
| `tests/plugins/test_editor_patches.py`, `tests/plugins/test_bigworld_singleton.py` | That the editor's plugin entry points are native methods (no monkey patches), and singleton placement. |
| `tests/plugins/fixtures/` | The fixture plugins (`well_behaved`, `raises_on_register`, `raises_on_tick`, `future_api`, `disabled_by_default`, ...). `tests/plugins/conftest.py: plugin_manager` builds a fresh `PluginManager` whose discovery is pointed at them. |
| `plugins/<name>/tests/` | Each shipped plugin's own suite. |

To test a plugin of your own, build a fresh `PluginManager()` rather than using the process singleton, load the package, and drive `attach_runtime`, `bind_host`, `dispatch_play_start`, `tick` and `dispatch_play_stop` against a logic object. See [Testing and CI](https://github.com/ViciousSquid/Fio/wiki/Testing-and-CI) for how the suites are run.
