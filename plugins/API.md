# Fio Plugin API Reference

This is the complete reference for everything a Fio plugin can call. It documents
the public surface exported from [`plugins/api.py`](api.py) and
[`plugins/host.py`](host.py) — the classes, methods, helpers and lifecycle a
plugin builds against.

For a narrative introduction ("what is a plugin, how do I write one"), start with
[`plugins/README.md`](README.md). This document is the flat reference you keep
open while writing code.

- [At a glance](#at-a-glance)
- [API versioning](#api-versioning)
- [`FioPlugin` — the base class](#fioplugin--the-base-class)
- [The plugin lifecycle](#the-plugin-lifecycle)
- [`EditorAPI` — load-time registration](#editorapi--load-time-registration)
- [`RuntimeAPI` — per-session services](#runtimeapi--per-session-services)
- [`TickContext` — the per-tick object](#tickcontext--the-per-tick-object)
- [`PluginHost` — the open-ended engine seam](#pluginhost--the-open-ended-engine-seam)
- [`EventBus` and `Event`](#eventbus-and-event)
- [`PropertySpec` and `prop()`](#propertyspec-and-prop)
- [`GlobalStore` — cross-level storage](#globalstore--cross-level-storage)
- [Helpers: `io_def`, `key_code`, `prop`](#helpers-io_def-key_code-prop)
- [The manager entry points](#the-manager-entry-points)
- [Play-session save / load (native)](#play-session-save--load-native)
- [Threading and safety contract](#threading-and-safety-contract)

---

## At a glance

A plugin is a Python sub-package of `plugins/` that exposes a module-level
`PLUGIN` instance (a `FioPlugin` subclass). Everything else flows from four
objects the manager hands you at defined moments:

| Object | Handed to | When | Use it for |
|--------|-----------|------|------------|
| [`EditorAPI`](#editorapi--load-time-registration) | `register(api)` | once, at load (editor **and** engine) | declare entity types, I/O, property schemas, editor-UI extensions, renderers |
| [`RuntimeAPI`](#runtimeapi--per-session-services) | `register_runtime(api)` | once per play session | register I/O input handlers; query the scene; spawn/despawn; raycast |
| [`PluginHost`](#pluginhost--the-open-ended-engine-seam) | `connect(host)` | once per play session | subscribe to engine events; reach any subsystem; publish/consume services |
| [`TickContext`](#tickcontext--the-per-tick-object) | `on_tick(logic, ctx)` | every play-mode tick | read input; drive the HUD |

Import everything from `plugins.api` (and `plugins.host` for the host types):

```python
from plugins.api import (
    FioPlugin, EditorAPI, RuntimeAPI, TickContext,
    PropertySpec, GlobalStore, io_def, prop, key_code,
    API_VERSION, version_tuple,
)
from plugins.host import PluginHost, EventBus, Event
```

---

## API versioning

`plugins.api.API_VERSION` is the version string of the API surface the host
implements; `API_VERSION_INFO` is the same value as an `(int, int, int)` tuple.

| Value | Introduced |
|-------|------------|
| `API_VERSION` | `"1.3.0"` |
| `API_VERSION_INFO` | `(1, 3, 0)` |

History:

- **1.2.0** — the open-ended extension surface: [`PluginHost`](#pluginhost--the-open-ended-engine-seam),
  the engine [event bus](#eventbus-and-event), and the `connect(host)` hook.
- **1.3.0** — render hooks (`render.*` events), swappable-renderer registration
  (`register_renderer`), editor-UI extensions (extra property fields on any
  entity, custom property tabs), and the `FIO_NO_PLUGINS` kill-switch.

A plugin declares the minimum it needs with `FioPlugin.api_version`. If that is
**newer** than the host's `API_VERSION`, the manager refuses to load the plugin
with a clear message instead of letting it fail deep inside a hook. Plugins that
don't set `api_version` default to `"1.0.0"` and always load.

```python
def version_tuple(value: str) -> tuple
```

Parse a dotted version string into a comparable int tuple. Non-numeric
components degrade to `0`, so a malformed version never raises
(`version_tuple("1.3.x") == (1, 3, 0)`).

---

## `FioPlugin` — the base class

Subclass `FioPlugin`, set the metadata attributes, override the lifecycle
methods you need, and expose an instance as your package's module-level
`PLUGIN`.

### Metadata attributes

| Attribute | Type | Default | Meaning |
|-----------|------|---------|---------|
| `name` | `str` | `"unnamed"` | Short unique id, shown in logs and the menu. |
| `version` | `str` | `"0.0.0"` | Human-readable version string. |
| `description` | `str` | `""` | One-line description shown in tooling / the *About* entry. |
| `category` | `str` | `"Plugins"` | Default editor category / menu grouping for this plugin's entities. |
| `enabled` | `bool` | `True` | Whether the plugin is active. Set `False` to ship disabled-by-default (auto-enabled when a level using its entities loads). |
| `api_version` | `str` | `"1.0.0"` | Minimum API version this plugin needs (see [API versioning](#api-versioning)). |
| `requires` | `List[str]` | `[]` | Names (or package names) of other plugins this one depends on. A missing requirement disables this plugin with a logged reason. |

### Lifecycle methods (override what you need)

```python
def register(self, api: EditorAPI) -> None
```
Register entities, I/O definitions and palette entries. Runs **once, UI-free**,
in every process that loads plugins (editor and engine). Do **not** touch
Qt/OpenGL here. → see [`EditorAPI`](#editorapi--load-time-registration).

```python
def describe_properties(self) -> dict
```
Optional property schemas, as `{entity_type: [PropertySpec, ...]}`. Collected by
the manager right after `register`. An alternative to calling
`EditorAPI.register_properties` per type. Default: `{}`.

```python
def register_runtime(self, api: RuntimeAPI) -> None
```
Register I/O input handlers against a play session's `IOManager`, and use the
runtime services. Called once per play session. → see [`RuntimeAPI`](#runtimeapi--per-session-services).

```python
def connect(self, host) -> None
```
Connect to the live engine through the [`PluginHost`](#pluginhost--the-open-ended-engine-seam).
Called once, alongside `register_runtime`, for **every** loaded plugin (so a
plugin enabled later is already wired in; its event hooks self-gate on
`enabled`). Subscribe to events, publish services, reach any subsystem here.

```python
def on_play_start(self, logic) -> None
def on_play_stop(self, logic) -> None
def on_tick(self, logic, ctx: TickContext) -> None
```
The play lifecycle. `on_play_start` initialises per-session state; `on_tick`
runs every tick after core gameplay handling; `on_play_stop` restores any edited
entity state so the map is left unchanged. → `on_tick` receives a
[`TickContext`](#tickcontext--the-per-tick-object).

```python
def on_enabled_changed(self, enabled: bool) -> None
```
Called when the plugin is toggled on/off (menu or level auto-enable). Override
to acquire/release resources or reset state. **Not** called for the initial
state — only on a change.

```python
def menu_entries(self) -> List[Tuple[str, type]]
```
Extra `(label, ThingClass)` placement entries. Most plugins rely on
`EditorAPI.register_entity` instead; override only for bespoke menu layouts.
Default: `[]`.

### Minimal plugin

```python
# plugins/coins/plugin.py
from plugins.api import FioPlugin, io_def
from editor.things import Thing

class Coin(Thing):
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'coin'           # the I/O + serialization key
        self.properties.setdefault('value', 1)

class CoinsPlugin(FioPlugin):
    name = "coins"
    version = "1.0.0"
    category = "Coins"

    def register(self, api):
        api.register_entity(Coin, menu_label="Coin")
        api.register_io('coin',
            inputs=[io_def('Collect', "Force-collect this coin")],
            outputs=[io_def('OnCollected', "Fired when collected")])

    def register_runtime(self, api):
        def collect(entity, param, logic):
            entity.properties['collected'] = True
        api.register_input_handler('coin', 'collect', collect)
```

```python
# plugins/coins/__init__.py
from .plugin import CoinsPlugin
PLUGIN = CoinsPlugin()
```

---

## The plugin lifecycle

```
 load_plugins()                          (editor + engine, once at startup)
       │
       ▼
 register(EditorAPI)      ── entity types, I/O defs, schemas, menu entries
 describe_properties()    ── (optional) extra schemas collected here
       │
 play mode / play session starts
       │
       ├─ register_runtime(RuntimeAPI)   ── I/O input handlers + services
       ├─ connect(PluginHost)            ── event subscriptions, services
       │
       ├─ on_play_start(logic)
       │
       ├─ on_tick(logic, TickContext)    ── every tick, after core interactions
       │        …
       │
       └─ on_play_stop(logic)            ── restore anything you mutated
```

Every call into plugin code is wrapped by the manager: a plugin that raises logs
an error to the debug console instead of crashing the editor or the play
session. `register_runtime` / `connect` run for **all** loaded plugins so a
plugin enabled mid-session already has working inputs and event hooks — those
hooks self-gate on the plugin's live `enabled` flag.

---

## `EditorAPI` — load-time registration

Handed to `FioPlugin.register` exactly once when the plugin loads. Runs in
**both** the editor and any engine process, so it must be UI-free.

### Entity registration

```python
def register_entity(self, cls, category=None, menu_label=None, placeable=True) -> type
```
Register a `Thing` subclass as a placeable editor entity. It:

- adds `cls` to `editor.things.ENTITY_TYPES` (keyed by class name) so the
  property editor, spawner and serializer find it;
- adds it to `ENTITY_CATEGORIES` under *category* (default: the plugin's
  `category`);
- if *placeable*, records a right-click menu entry under **Plugins ▸ &lt;plugin&gt;**;
- auto-detects a class-level `PROPERTY_SCHEMA` and records it as the type's
  property schema.

Returns `cls` unchanged, so it works as a decorator. In the PyQt-free player the
editor-palette step is skipped quietly, but ownership is always recorded so the
package exporter and player host can map a map's entity `type` back to its
owning plugin.

### I/O registration

```python
def register_io(self, entity_type: str, inputs: List, outputs: List) -> None
```
Register input/output definitions for *entity_type* — which **must equal** the
entity's `properties['type']` string. *inputs*/*outputs* are lists of
[`io_def`](#helpers-io_def-key_code-prop) results; `None` entries (produced when
the I/O system is unavailable) are filtered out.

### Property schema

```python
def register_properties(self, entity_type: str, specs: List[PropertySpec]) -> None
```
Declare the editable-property schema for *entity_type*. The editor renders typed
widgets — enum dropdowns, ranged spin boxes, checkboxes — with labels and
tooltips instead of guessing from the stored value's Python type. Optional and
additive: properties without a spec still render generically. → see
[`PropertySpec`](#propertyspec-and-prop).

### Editor-UI extensions (API 1.3.0)

```python
def register_extra_fields(self, entity_type: str, specs) -> None
```
Append extra editable fields to *entity_type*'s property panel — works for any
entity, **including built-in ones**. Fields are added after the stock rows,
never replacing them.

```python
def register_property_tab(self, label: str, factory, entity_type=None) -> None
```
Add a custom tab to the property panel. `factory(thing)` builds and returns the
tab's widget. With *entity_type* the tab appears only for that type; otherwise
for every entity.

```python
def register_renderer(self, name: str, cls) -> bool
```
Register a swappable renderer class under *name*. Fio's viewport selects its
renderer from a class registry; this drops *cls* in so it appears as a render
mode. *cls* must implement the renderer interface (`render_scene`,
`draw_models`, `cleanup`, a `lod_manager`, …). Returns `True` if registered,
`False` in a headless/player context with no viewport. This is how a whole new
renderer ships as a plugin.

### Global store & logging

```python
@property
def globals(self) -> GlobalStore
def global_get(self, key, default=None, store="plugins")
def global_set(self, key, value, store="plugins")
def log(self, message: str) -> None
```
`globals` is the cross-level [`GlobalStore`](#globalstore--cross-level-storage).
`log` is informational — silent unless `FIO_PLUGIN_DEBUG` is set — so plugins
don't spam the console on a normal launch.

---

## `RuntimeAPI` — per-session services

Handed to `FioPlugin.register_runtime` when a logic thread starts. Beyond
registering I/O input handlers, it exposes a small **services** layer over the
play session's `logic` object so plugins don't each re-implement ray-casting,
entity queries and vector math.

Attributes: `api.logic` (the play session's logic object) and `api.io_manager`
(its `IOManager`, or `None`).

### I/O

```python
def register_input_handler(self, entity_type: str, input_name: str, handler) -> None
```
Register `handler(entity, parameter, logic)` for an entity input. Registered
once (when the logic thread attaches) but **gated on the plugin's live
`enabled`** — it runs only while the plugin is on. That lets the manager attach
every loaded plugin up front (so one enabled later has working inputs
immediately) while a disabled plugin's inputs stay inert without a re-attach.

```python
def fire_output(self, entity, output_name: str, value=None) -> None
```
Fire an output from *entity* through the I/O system (no-op if unavailable).

### Scene queries

```python
def entities_of_type(self, type_name: str) -> List
```
All scene entities whose normalised `type` matches *type_name*.

```python
def things_near(self, pos, radius: float, type_name=None) -> List
```
Entities within *radius* of *pos* (an `(x, y, z)`), optionally filtered by type.

```python
def raycast_from_crosshair(self, reach=160.0, aim_dot=0.86,
                           type_name=None, predicate=None)
```
The nearest entity under the crosshair, or `None`. Considers entities within
*reach* whose direction from the eye is within *aim_dot* of the look vector
(`1.0` = dead-centre, lower = wider cone). Optionally filter by *type_name*
and/or an arbitrary `predicate(entity)`. This is the "what am I looking at"
query most interaction plugins need.

### Player geometry

```python
def player_eye(self)      # -> (x, y, z) eye position (origin + camera_height)
def player_forward(self)  # -> (x, y, z) full look direction, includes pitch
```

### Runtime spawn / despawn

```python
def spawn(self, cls, pos=None, properties=None)
```
Instantiate *cls*, append it to `logic.things`, emit an `entity_spawned` event,
and return the entity (or `None` if construction fails).

```python
def despawn(self, entity) -> bool
```
Remove *entity* from the live scene. Returns `True` if it was present.

### Global store & logging

```python
@property
def globals(self) -> GlobalStore
def global_get(self, key, default=None, store="plugins")
def global_set(self, key, value, store="plugins")
def log(self, message: str) -> None
```

---

## `TickContext` — the per-tick object

Passed to `FioPlugin.on_tick` once per play-mode tick.

### Fields

| Field | Type | Meaning |
|-------|------|---------|
| `delta` | `float` | Seconds since the previous tick. |
| `use_pressed` | `bool` | The edge-triggered "use/interact" key for this tick (already consumed by the logic thread). Treat `True` as a single press. |
| `keys` | `frozenset` | The raw held-key set (Qt key codes on the engine host). Prefer `key_down`. |
| `interaction_consumed` | `bool` | `True` if the core already set a HUD prompt / consumed the use press this tick (door, pickup, level-changer). Avoid clobbering unless you own a crosshair target. |
| `logic` | `Any` | The play session's logic object (set by the manager). |

### Input

```python
def key_down(self, name) -> bool
```
`True` if *name* is held. *name* is a key name (`'w'`, `'space'`, `'shift'`,
`'up'` …) or a raw int code. → see [`key_code`](#helpers-io_def-key_code-prop).

### HUD

```python
def set_prompt(self, text: str, priority: int = 0) -> bool
```
Show a contextual HUD line this tick, respecting priority. Returns `False`
(does nothing) if the core already claimed the line (`interaction_consumed`)
and *priority* isn't above it, or if another plugin already set a
higher-priority prompt this tick. This replaces the manual "don't clobber the
HUD" dance and poking `logic.current_hud_message` directly.

```python
def toast(self, text: str, seconds: float = 2.0) -> None
```
Show *text* for *seconds* as long as no prompt claims the line. The message
persists across ticks until it expires, so fire it once (e.g. on an event)
instead of re-setting it every frame.

---

## `PluginHost` — the open-ended engine seam

While `EditorAPI`/`RuntimeAPI` are a *curated*, deliberately small surface, the
`PluginHost` (from `plugins.host`) is the *open-ended* one. It exists so the
engine can be extended in ways the API never anticipated **without changing the
engine**. Handed to `FioPlugin.connect`, one per plugin per session.

`host.kind` is `"engine"` (editor play mode) or `"player"` (standalone player).

### Reaching the live objects

Named accessors — each returns `None` (or `[]` for `scene`) when the subsystem
isn't present on this host, rather than raising:

| Accessor | Returns |
|----------|---------|
| `host.logic` / `host.engine` | the play session's logic object |
| `host.scene` | the live `things` list (the *actual* list — mutating it affects the scene) |
| `host.player` | the player object |
| `host.io` | the `IOManager` |
| `host.globals` | the [`GlobalStore`](#globalstore--cross-level-storage) |
| `host.game_state` | the game-state object |
| `host.monster_ai` | the monster AI subsystem |
| `host.terrain` | the terrain object |
| `host.editor` | the editor main window if running in the editor, else `None` |

For anything without a named accessor — including subsystems added later:

```python
def get(self, path: str, default=None)
```
Dotted lookup from the host target: `host.get('game_state')`,
`host.get('editor_state.things')`. Returns *default* if any step is missing.
Unknown attributes also fall through: `host.<whatever>` reads straight off the
target — the "touch any part of the engine" escape hatch.

### Events

```python
@property
def events(self) -> EventBus                # the raw bus (advanced)
def on(self, event: str, handler, priority=0) -> Callable
def off(self, event: str, handler) -> None
def emit(self, event: str, **data)
```
`on` subscribes to an engine (or plugin) event; the handler is **gated on this
plugin's live `enabled` flag**, so a disabled plugin's hooks go quiet without
unsubscribing. `emit` fires a custom event other plugins can observe. → see
[`EventBus`](#eventbus-and-event).

### Cross-plugin services & extension

```python
def provide(self, name: str, value) -> None     # publish a named service
def service(self, name: str, default=None)      # look one up
def wrap(self, obj, method_name: str, wrapper) -> bool
def register_renderer(self, name: str, cls) -> bool
def log(self, message: str) -> None
```
`wrap` is a guarded monkey-patch helper for the rare extension that must
intercept an existing engine call rather than react to an event: it replaces
`obj.method_name` with `wrapper(original, *args, **kwargs)`, and if the wrapper
raises, the original is called so behaviour degrades to stock. Returns `True` if
installed.

Example `connect`:

```python
def connect(self, host):
    self._host = host
    host.on("player_damage", self._on_damage)     # react to an engine event
    host.on("render.overlay", self._draw_overlay) # draw a HUD overlay
    host.provide("my_service", self)              # share with other plugins
```

---

## `EventBus` and `Event`

The engine emits named events at every meaningful moment; a plugin subscribes to
any of them — including events that don't exist yet (a subscription to an unknown
event is simply dormant until something emits it). Adding a new engine signal
later is a single `emit()` call: no API change.

### `Event`

Handlers receive one `Event`. Payload fields are reachable three ways, so
handlers stay terse:

```python
ev.data['player']     # dict access
ev.get('player')      # .get with default
ev.player             # attribute access (falls through to the payload)
```

`ev.name` is the event name; `ev.logic` is always present (the play session's
logic/host object). Call `ev.stop()` to halt propagation to lower-priority
handlers.

### `EventBus`

A tiny, fail-safe publish/subscribe bus. Handlers are called highest-priority
first; ties keep subscription order. A handler that raises is logged and
skipped. `emit` early-outs when an event has no subscribers, so instrumenting
the engine with unlistened emit points costs almost nothing.

```python
def on(self, event: str, handler, priority=0) -> Callable   # returns handler (decorator-friendly)
def off(self, event: str, handler) -> None
def has(self, event: str) -> bool
def emit(self, event: str, **data) -> Optional[Event]       # None if nobody listening
def clear(self) -> None
```

`bus.gen` is bumped whenever the subscription set changes, so a hot-path caller
(the engine's per-frame tick gate) can detect "did anything change?" with a
single int compare.

Common event names include `play_start`, `tick`, `player_damage`,
`portal_transit`, `pickup_collected`, `entity_spawned`, and the render hooks
`render.overlay` / `render.*`. Prefer subscribing through `host.on(...)` (which
gates on `enabled`) over the raw bus.

---

## `PropertySpec` and `prop()`

Declares one editable property of a plugin entity so the editor renders a
fitting widget and can coerce/validate values. The untyped `properties` dict
stays the storage format; the schema is metadata layered over it.

### Fields

| Field | Type | Meaning |
|-------|------|---------|
| `name` | `str` | The `properties` key this describes. |
| `type` | `str` | One of `int`, `float`, `bool`, `string`, `enum`, `vec3`, `asset`. |
| `label` | `str` | Human-readable label in the panel. |
| `default` | `Any` | Default value (applied if the key is missing). |
| `min` / `max` | `float?` | Range clamp for numeric types. |
| `choices` | `List?` | Allowed values for `enum`. |
| `help` | `str` | Tooltip text. |

### Methods

```python
def apply_default(self, properties: dict) -> None
```
Set this property's default on *properties* if it is missing.

```python
def coerce(self, value)
```
Best-effort cast of *value* to this spec's type, clamped to `min`/`max`. **Never
raises** — an uncastable value falls back to the default (or a type-appropriate
zero/empty), so a bad hand-edited map can't crash the editor.

### `prop()`

```python
def prop(name, type="string", label="", default=None,
         min=None, max=None, choices=None, help="") -> PropertySpec
```
Terse, keyword-friendly constructor for a `PropertySpec`. Example:

```python
api.register_properties("bigworldsettings", [
    prop("enabled", "bool", "Streaming enabled", default=True,
         help="Turn cell streaming on for this map."),
    prop("activation_radius", "float", "Activation radius", default=2048.0,
         min=256.0, max=65536.0,
         help="Cells within this distance of the player become active."),
])
```

---

## `GlobalStore` — cross-level storage

Process-wide, cross-level key/value storage for plugins. When the editor package
is present it binds to the **same** persistent registry that map
`LogicKeyValueStore` entities use, so a plugin's globals live alongside — and can
share stores with — map state, persisting across level loads within a session.
In the dependency-light player it falls back to a plain process-local
dict-of-dicts with the same API. Values are stored as **strings**, matching the
map store.

Keys are grouped by *store* name (default `"plugins"`). Pass a store name a map's
`LogicKeyValueStore` uses to read/write the exact same values.

```python
def get(self, key, default=None, store="plugins")
def set(self, key, value, store="plugins") -> None      # value stored as str(value)
def delete(self, key, store="plugins") -> bool
def all(self, store="plugins") -> dict
def keys(self, store="plugins") -> list
```

Reach it via `api.globals` / `host.globals`, or the `global_get` / `global_set`
shortcuts on `EditorAPI` and `RuntimeAPI`.

---

## Helpers: `io_def`, `key_code`, `prop`

```python
def io_def(name: str, description: str = "", param_type: str = "")
```
Build an `editor.io_system.IODef` without importing `io_system` yourself.
Returns `None` if the I/O system is unavailable (headless), which the manager
tolerates and filters out. `param_type` (e.g. `"int"`, `"string"`) types an I/O
port's parameter.

```python
def key_code(name) -> Optional[int]
```
Resolve a key *name* (or raw int code) to a Qt key code, or `None`. A single
character maps to its uppercase ASCII code (`'w'` → `0x57`); named special keys
(`'space'`, `'shift'`, `'ctrl'`, `'alt'`, `'tab'`, `'return'`, `'enter'`,
`'escape'`/`'esc'`, `'up'`/`'down'`/`'left'`/`'right'`, `'backspace'`) use the
built-in table; an `int` passes through unchanged. `TickContext.key_down` calls
this for you.

`prop` is documented under [`PropertySpec`](#propertyspec-and-prop).

---

## The manager entry points

Most plugins never touch the manager directly, but these are the seams the host
uses and that tools/tests call:

```python
from plugins.manager import get_manager, load_plugins
```

```python
def load_plugins() -> None      # discover + load every plugin sub-package (call once)
def get_manager() -> PluginManager
```

Useful `PluginManager` methods (see [`manager.py`](manager.py) for the full set):

| Method | Purpose |
|--------|---------|
| `discover_and_load()` | discover and load all plugin packages |
| `menu_entries()` | `(plugin, label, cls)` placement entries for the editor menus |
| `find_plugin(name)` / `is_enabled(plugin)` | lookup / state |
| `set_enabled(plugin_or_name, enabled, auto=False)` | toggle a plugin |
| `plugin_for_type(type)` / `entity_class_for_type(type)` | map an entity `type` back to its plugin/class |
| `auto_enable_for_map(map_data)` | enable disabled-by-default plugins a map needs |
| `disable_auto_enabled()` | revert level-driven auto-enables (e.g. File ▸ New) |
| `attach_runtime(logic)` | wire every plugin's runtime into a logic thread |
| `bind_host(target, kind)` | build the `PluginHost` and call each `connect` |
| `emit(event, **data)` / `has_listeners(event)` | drive the event bus |
| `dispatch_play_start/stop(logic)`, `dispatch_tick(logic, ctx)` | lifecycle dispatch |

The engine wires `attach_runtime`, the play-start/stop dispatch and the cached
per-tick dispatch **natively** from `engine.logic_thread.LogicThread`; the editor
hooks are installed as small guarded monkey-patches in
[`integration.py`](integration.py). See [`README.md`](README.md#integration-points-in-the-core).

---

## Play-session save / load (native)

Saving and loading a **play session** is an engine-native capability, not part
of the plugin API surface — adding it did **not** bump `API_VERSION` (still
`1.3.0`). It is documented here because it builds directly on the same
serialization a plugin already relies on, and because a plugin can drive it
through the [`PluginHost`](#pluginhost--the-open-ended-engine-seam).

The editor already serializes a *level* (`EditorState.get_level_data` — brushes,
things with their live properties, terrain). [`engine/savegame.py`](../engine/savegame.py)
builds a *saved game* on top of that: the serialized level **plus** a `runtime`
block for the state the level format never stores — the player transform and
stats, the cheat flags (`god` / `buddha` / `notarget`), the collected-key set,
and the door / mover / monster animation state. Save files are JSON with a
`.fiosave` extension, written under a `saves/` directory.

Restore is applied as an **overlay** onto a live, already-playing session:
entities are matched back by the stable UUID every brush and thing carries, so
the scene is never rebuilt mid-flight (object identities, caches and the spatial
grid all stay valid). Cached, underscore-prefixed runtime fields (e.g. a mover's
`_direction_np`) are never persisted — the engine recomputes them on the next
tick.

**Save modes (`save_version` 2, still no `API_VERSION` bump).** A `save_mode`
metadata key selects one of three strategies, set by the *Play-session Save
Mode* editor setting (`[Settings] save_mode`, default `full`):

- **`full`** — the self-contained snapshot above. Needs no base map.
- **`delta`** — only the entities/brushes that differ from the base map,
  matched by UUID, plus player/runtime state and a `base_map` identity block.
  Smallest, but the base map must be present to load; it restores by overlaying
  just the changed records through the very same UUID overlay.
- **`both`** — the compact delta *and* a full fallback snapshot in one file.

Loading is automatic — `restore_auto` reads `save_mode` (a legacy v1 file with
no `save_mode` loads as `full`) and, for delta/both, grades the current map
against the stored identity (exact → apply; related → apply by UUID skipping
missing entities; incompatible → a `both` save uses its full fallback, a
delta-only save reports it can't). Only a delta-only save on a clearly
incompatible map ever prompts. `save_version` remains independent of the app
and plugin versions; a save newer than the build understands is rejected by
`read`.

### The native surface

`engine.logic_thread.LogicThread` exposes two methods, each returning
`(ok: bool, message: str)` and requiring an active play session:

```python
# capture the live session → path (save_mode: "full" | "delta" | "both";
# delta/both also want base_level, the normalized original map to diff against)
logic.save_session(path, map_name="", save_mode="full", base_level=None)
# auto-detect the save's mode and overlay it onto the running session
logic.load_session(path, map_name="")
```

Lower level, in [`engine/savegame.py`](../engine/savegame.py):

```python
build_snapshot(logic, map_name="", save_mode="full", base_level=None) -> dict
compute_delta_level(base_level, live_level) -> dict   # changed-only partial level
normalize_base_level(raw_level) -> dict               # re-serialize an on-disk map
restore_snapshot(logic, data) -> None                 # apply a full snapshot
restore_delta(logic, data) -> None                    # overlay a delta onto a fresh base
classify_base_map(data, current_level, name="") -> str  # exact | related | incompatible
restore_auto(logic, data, current_map_name="") -> dict  # pick the path automatically
write(path, snapshot) -> None                # JSON dump (.fiosave), NumPy/glm-safe
read(path) -> dict                           # read + validate a Fio save
```

### Console commands

Driven from the editor's debug console (see
[`editor/console_commands.py`](../editor/console_commands.py)):

| Command | Effect |
|---------|--------|
| `save [name]` | Save the current session to `saves/<name>.fiosave` (Play Mode only). |
| `quicksave` / `qs` | Save to the quicksave slot. |
| `load [name]` | Load a save. In Play Mode it overlays the running session; from the editor it loads the save's map, enters Play Mode, then applies. |
| `quickload` / `ql` | Load the quicksave slot. |
| `saves` | List available save files. |

### Reaching it from a plugin

The `PluginHost` exposes the live logic object, so a plugin can save or load
without any new API:

```python
def connect(self, host):
    self._host = host

def on_tick(self, logic, ctx):
    if ctx.key_down("f5"):
        ok, msg = self._host.logic.save_session("saves/plugin_quick.fiosave")
        ctx.toast(msg)
```

---

## Threading and safety contract

- **`register` is UI-free and host-agnostic.** It runs in the editor, the
  engine, headless tools and the player. No Qt, no OpenGL, no assumptions about
  a display.
- **Everything is fail-safe.** The manager wraps every call into plugin code; a
  raise is logged (to the debug console) and swallowed, never crashing the host
  or another plugin. The event bus, `host.wrap`, and the debug overlay path are
  all individually guarded.
- **Do per-frame work in `on_tick` / event handlers, and keep it cheap.** The
  per-tick dispatch is cached and gated so a map whose active plugins don't tick
  pays almost nothing.
- **Restore what you mutate.** If you move, hide or disable entities during play,
  put them back in `on_play_stop` so the edited map is unchanged (see
  `TidySession.stop` and `BigWorldSession.stop`).
- **Never make an OpenGL call off the render thread.** If a change needs GL work,
  defer it to a render frame (BigWorld defers its terrain chunk prune this way).
- **The player runtime is dependency-free.** No PyGLM, no PyQt in the runtime
  path — plugin entities fall back to `plugins/entitybase.py` when the editor
  package is absent, so the same plugin loads in the editor, the desktop player
  and the Android APK.

---

## See also

- [`plugins/README.md`](README.md) — the plugin-system overview and walkthrough.
- [`plugins/api.py`](api.py) — the annotated source these docs mirror.
- [`plugins/host.py`](host.py) — the `PluginHost` / `EventBus` source.
- [`plugins/tidy/`](tidy/) — a complete worked gameplay plugin.
- [`plugins/bigworld/README.md`](bigworld/README.md) — a runtime-scalability
  plugin that uses the event bus, services and host wrapping.
