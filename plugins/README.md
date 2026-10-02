# The Fio Plugin System

Plugins add new gameplay to Fio — new placeable entity types, their I/O, and
runtime behaviour — **without editing the core editor or engine**. Drop a Python
package into this `plugins/` directory and it is discovered automatically at
startup, wired into the editor's menus, property panel, I/O editor, serializer
and 3D renderer, and dispatched through the play lifecycle in both the editor and
the standalone `.fiopak` player.

> **New here?** Skip to [Writing a plugin](#writing-a-plugin) for the 30-line
> version, then keep [`API.md`](API.md) open as the flat reference.

**Contents**

- [What ships](#what-ships)
- [The idea in one picture](#the-idea-in-one-picture)
- [How discovery and dispatch work](#how-discovery-and-dispatch-work)
- [Integration points in the core](#integration-points-in-the-core)
- [Writing a plugin](#writing-a-plugin)
- [The four API objects](#the-four-api-objects)
- [Rules of thumb](#rules-of-thumb)
- [Finding and toggling plugins in the editor](#finding-and-toggling-plugins-in-the-editor)
- [Enabling / disabling plugins](#enabling--disabling-plugins)
- [Packaging plugins into a `.fiopak`](#packaging-plugins-into-a-fiopak)
- [Running plugins outside the editor](#running-plugins-outside-the-editor)
- [Android APK](#android-apk)
- [Debugging and tests](#debugging-and-tests)
- [Reference map](#reference-map)

---

## What ships

Two example plugins live in this directory, and they are deliberately different
in kind:

| Plugin | Kind | What it demonstrates |
|--------|------|----------------------|
| [`tidy`](tidy/) | **Gameplay** | Pick-up-and-put-away games (books back on the shelf, tidy the museum, sort the warehouse). Entities, I/O ports, a carry/place runtime, a HUD goal. |
| [`bigworld`](bigworld/) | **Runtime layer** | Cell streaming that keeps only the area around the player active in maps of hundreds of thousands of brushes. Uses the event bus, cross-plugin services, and host wrapping rather than adding entities to place. See its own [README](bigworld/README.md). |

Between them they exercise nearly the whole API: entity registration and I/O
(`tidy`), and the open-ended [`PluginHost`](API.md#pluginhost--the-open-ended-engine-seam)
extension seam (`bigworld`).

---

## The idea in one picture

The core editor and engine call *into* a manager; plugins register *with* it and
never import editor internals except through the API helpers. That inversion is
the whole design — it keeps large editor/engine source files untouched and lets a
build with no `plugins/` directory run completely unchanged.

```
      Core editor / engine                 Plugin package (yours)
      ───────────────────                  ──────────────────────
         load_plugins()  ───────────────▶  register(EditorAPI)
                                              entity types, I/O, schemas, menus
         enter play mode ───────────────▶  register_runtime(RuntimeAPI)
                                              I/O input handlers, scene services
                          ───────────────▶  connect(PluginHost)
                                              engine events, services, wraps
         every tick      ───────────────▶  on_tick(logic, TickContext)
         leave play mode ───────────────▶  on_play_stop(logic)
```

Everything else — the property panel, the I/O editor, serialization, 3D model
rendering — works for plugin entities **for free**, because plugin entities are
ordinary `Thing` subclasses.

---

## How discovery and dispatch work

A plugin is a Python **sub-package of `plugins/`** that exposes a module-level
`PLUGIN` instance (a `FioPlugin` subclass) — or a `get_plugin()` factory — from
its `__init__.py`.

At startup the editor (and any process that imports the engine) calls
`plugins.manager.load_plugins()`. The `PluginManager` then:

1. **Discovers** every sub-package of `plugins/`.
2. **Loads** each one and calls `register(EditorAPI)` — where the plugin declares
   entity types, I/O definitions, property schemas and editor palette entries.
   This runs in the editor *and* the engine, so it must be **UI-free**.
3. **Attaches** to each play session's logic thread via
   `register_runtime(RuntimeAPI)` and `connect(PluginHost)` — I/O input handlers,
   scene services, engine-event subscriptions.
4. **Dispatches** the play lifecycle: `on_play_start`, `on_tick`, `on_play_stop`.

Every call into plugin code is wrapped: a plugin that raises logs an error to the
debug console instead of crashing the editor or a play session.

**Runtime attach + enable gating.** Every loaded plugin's I/O handlers and event
hooks are registered once, when the logic thread is built, but each one
**self-gates on its plugin's live `enabled` flag**. So a plugin enabled *after*
startup (e.g. a disabled-by-default one auto-enabled when its level loads) has
working inputs and hooks with no re-attach, while a disabled plugin's inputs stay
inert. Play-start/tick/stop dispatch is likewise gated and, on the hot per-tick
path, served from a cache that only rebuilds when the enabled set changes — a map
whose active plugins don't tick pays almost nothing per frame.

**Kill switch.** Set `FIO_NO_PLUGINS=1` to disable the entire plugin system for a
launch (nothing is discovered or loaded).

---

## Integration points in the core

The **engine** play lifecycle is wired **natively**:
`engine.logic_thread.LogicThread` calls the plugin manager directly —
`attach_runtime` in `__init__`, `dispatch_play_start`/`dispatch_play_stop` in
`set_play_mode`, and the cached, early-out `tick()` in `_tick_play_mode`. Each
call is guarded, so a build without the `plugins/` package runs unchanged.

The **editor** integrations stay as small, guarded monkey-patches in
[`integration.py`](integration.py) (cold paths only — menus, load hooks, export),
so the large editor source files are left untouched. The only editor edit is a
tiny bootstrap in `editor/__init__.py`.

| File | Role |
|------|------|
| `engine/logic_thread.py` | **Native** plugin hooks: `attach_runtime` (`__init__`), play-start/stop (`set_play_mode`), per-tick dispatch (`_tick_play_mode`). All guarded and optional. |
| `editor/__init__.py` | Bootstrap: `load_plugins()` + `integration.apply()`, run once when the editor package is first imported (before any map loads). |
| [`integration.py`](integration.py) | Installs the editor hooks: auto-enable/disable of disabled-by-default plugins onto `EditorState` (`load_from_data` enables for a level's entities, `clear_scene` reverts on File ▸ New); a **Plugins ▸ &lt;plugin&gt;** submenu onto `View2D`'s right-click menu; and a top-level **Plugins** menu onto `Ui_MainWindow`. |
| `editor/package_exporter.py` | **Native** plugin bundling: `PackageExporter.export` calls `plugins.packaging.augment_fiopak` as a first-class final step once the base `.fiopak` is written. Guarded, so a build without the plugin system just skips it. |
| [`packaging.py`](packaging.py) | Bundles the plugins a `.fiopak`'s maps depend on (code + assets + manifest) so exported packages are self-contained. |

> The right-click **Plugins ▸ &lt;plugin&gt;** submenu is injected by temporarily
> swapping `QMenu.exec_` on the class while the 2D view builds its menu. That
> swap must be reversed precisely — using the class's raw `exec_` **descriptor**,
> not the unbound-method wrapper — or every later `menu.exec_(pos)` in the app
> breaks. See the comments in [`integration.py`](integration.py) if you touch it.

---

## Writing a plugin

Create `plugins/myplugin/plugin.py` and `plugins/myplugin/__init__.py`:

```python
# plugins/myplugin/plugin.py
from plugins.api import FioPlugin, io_def
from editor.things import Thing

class Coin(Thing):
    pixmap_path = "assets/sprites/pickup.png"
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties['type'] = 'coin'          # the I/O + serialization key
        self.properties.setdefault('value', 1)

class MyPlugin(FioPlugin):
    name = "myplugin"
    version = "1.0.0"
    category = "My Stuff"

    def register(self, api):
        api.register_entity(Coin, menu_label="Coin")
        api.register_io('coin',
            inputs=[io_def('Collect', "Force-collect this coin")],
            outputs=[io_def('OnCollected', "Fired when collected")])

    def register_runtime(self, api):
        def collect(entity, param, logic):
            entity.properties['collected'] = True
        api.register_input_handler('coin', 'collect', collect)

    def on_tick(self, logic, ctx):
        ...   # your per-tick gameplay
```

```python
# plugins/myplugin/__init__.py
from .plugin import MyPlugin
PLUGIN = MyPlugin()
```

That's it. Restart the editor and **Coin** appears under **Plugins ▸ myplugin**
in the 2D view's right-click menu, with a Properties panel and an I/O tab, and
the `LogicSpawner` can spawn it at runtime.

---

## The four API objects

The manager hands your plugin four objects at defined moments. Full signatures
are in [`API.md`](API.md); here is what each is *for*:

| Object | Handed to | Use it for |
|--------|-----------|------------|
| [`EditorAPI`](API.md#editorapi--load-time-registration) | `register(api)` | declare entity types, I/O, property schemas, extra property fields/tabs, renderers |
| [`RuntimeAPI`](API.md#runtimeapi--per-session-services) | `register_runtime(api)` | register I/O input handlers; query the scene (`entities_of_type`, `things_near`, `raycast_from_crosshair`); `spawn`/`despawn` |
| [`PluginHost`](API.md#pluginhost--the-open-ended-engine-seam) | `connect(host)` | subscribe to engine events (`host.on(...)`); reach any subsystem (`host.get(...)`); publish/consume services; guarded `host.wrap(...)` |
| [`TickContext`](API.md#tickcontext--the-per-tick-object) | `on_tick(logic, ctx)` | read input (`ctx.use_pressed`, `ctx.key_down('e')`); drive the HUD (`ctx.set_prompt`, `ctx.toast`) |

Entity properties can be made self-documenting with
[`PropertySpec`/`prop()`](API.md#propertyspec-and-prop), and plugins share
cross-level state through the [`GlobalStore`](API.md#globalstore--cross-level-storage).

---

## Rules of thumb

- **The entity `type` string is the contract.** `properties['type']` must match
  the keys you pass to `register_io` and `register_input_handler`. If you
  subclass `Thing` directly, the base defaults `type` to the lowercased class
  name; set it explicitly to be safe.
- **Want 3D geometry in play mode?** Subclass the engine's `Model` (as `tidy`'s
  `TidyObject` does) or set a `model_path` property — any `Thing` with a
  `model_path` is rendered by the existing model pipeline. Things without one are
  editor-only sprites.
- **Keep `register()` UI-free.** It runs in headless/engine contexts too — no Qt,
  no OpenGL.
- **Do per-tick work in `on_tick`, and keep it cheap.** `ctx.use_pressed` is the
  edge-triggered interact key for that tick; `ctx.interaction_consumed` tells you
  whether the core already claimed the HUD/use this tick.
- **Use the HUD helpers, not `logic.current_hud_message`.** `ctx.set_prompt`
  respects priority and won't clobber the core's prompt; `ctx.toast` shows a
  timed message.
- **Restore what you mutate.** If you move, hide or disable entities during play,
  put them back in `on_play_stop` so the edited map is unchanged (see
  `TidySession.stop`).
- **Reach the wider engine through the host, not imports.** `host.on(...)`,
  `host.get(...)`, `host.provide(...)` keep you decoupled and fail-safe.

---

## Finding and toggling plugins in the editor

Loaded plugins appear two ways:

- **Menu bar → Plugins** — a submenu per plugin with an **Enabled** checkbox to
  toggle it on/off, its placeable entities (click one to drop it at the origin,
  then drag it into place), and an *About* entry. The base of the menu has an
  *About the plugin system* summary.
- **2D view right-click → Plugins ▸ &lt;plugin&gt;** — place an entity exactly
  where you click (only shown for enabled plugins).

Plugin entities also get a Properties panel and an I/O tab automatically.

Loading is **silent** by default — nothing about plugins appears in the console.
Set `FIO_PLUGIN_DEBUG=1` to see informational load/registration messages (errors
are always shown).

---

## Enabling / disabling plugins

- **Disabled by default + auto-enable on load.** A plugin can set
  `enabled = False` on its class to ship inert — ordinary maps never pay for
  gameplay they don't use. When a level whose `things` reference the plugin's
  entity types is loaded, the manager turns it on automatically
  (`PluginManager.auto_enable_for_map`, wired into level loading in the editor
  and the standalone player). Both example plugins ship this way: `tidy` stays
  off until you open a map like `maps/Tidy_Test.json`, and `bigworld` until a map
  contains a `BigWorldSettings` entity. The flip is symmetric — clearing the
  scene (**File ▸ New**, or loading a map that doesn't use the plugin) reverts a
  level-driven auto-enable via `PluginManager.disable_auto_enabled`, so an empty
  map starts clean. This is a runtime, per-session flip: it never rewrites the
  persisted `[Plugins] disabled` list, and a plugin you enabled by hand from the
  menu is never auto-disabled underneath you.
- **Per plugin, in the editor.** Toggle **Enabled** in the Plugins menu. A
  disabled plugin stops its gameplay and greys out placement; the choice is saved
  to `settings.ini` (`[Plugins] disabled`) and restored next launch. (Entity
  *registration* isn't undone live, so a re-enable is instant while a full unload
  happens on restart.)
- **At startup, globally.** Set `FIO_DISABLED_PLUGINS` to a comma-separated list
  of plugin/package names so they never load, or `FIO_NO_PLUGINS=1` to disable
  the whole system:

  ```bash
  FIO_DISABLED_PLUGINS=tidy python main.py
  FIO_NO_PLUGINS=1 python main.py
  ```

---

## Packaging plugins into a `.fiopak`

`.fiopak` exports are **plugin-aware**. When you export a package (File →
Export…), the exporter scans the maps it bundles, works out which plugins their
entities come from, and injects those plugins — **code and assets** — plus the
plugin-system core into the archive, recording them in `metadata.json` under
`"plugins"`. The package is then self-contained and loads on another machine.

- Plugin assets keep their repo-relative paths (e.g.
  `plugins/tidy/assets/tidy_object.obj`), so a map's `model_path` resolves
  straight out of the package — no rewriting.
- Packages that use no plugin entities are unaffected (the step is a no-op)
  unless a **global plugin** is in play (below).
- **Global plugins** (no placeable entities — which sets `global_plugin = True`)
  can't be found from a map's `things`. They are bundled when they are *enabled*
  at export time, or when a map names them under a top-level
  `"required_plugins": [...]` (with optional `"plugin_config": {name: {...}}`).
  The exporter bundles them, records them in the manifest, and bakes
  `required_plugins` / `plugin_config` into each map so the standalone player
  (which only sees map data) enables and configures them without any entity to
  trigger auto-enable.
- The player side exposes the dependency: `FioPackage.required_plugins` reads the
  manifest list, and `plugins.packaging.load_package_plugins(root)` loads the
  bundled plugins from an extracted package.

The mechanics live in [`packaging.py`](packaging.py) (`augment_fiopak`,
`load_package_plugins`). Bundling is a native step of
[`editor/package_exporter.py`](../editor/package_exporter.py) —
`PackageExporter.export` calls `augment_fiopak` itself once the base archive is
written; it is **not** monkey-patched on by `integration.py`.

---

## Running plugins outside the editor

Plugin gameplay runs in **both** hosts:

- **Editor Play mode** — the logic thread dispatches the plugin lifecycle/tick
  (via `plugins.integration`).
- **Standalone `.fiopak` player** (`player/`, incl. the Android build) — the
  `player.plugin_host.PlayerPluginHost` loads the package's plugins, builds
  entity instances from the map, and drives the same lifecycle/tick from the
  player's frame loop against a camera→player bridge (USE = interact).

To make this work everywhere, the plugin runtime is **dependency-free**: no
PyGLM (plain-Python vector math) and no PyQt. Plugin entities normally subclass
the editor's `Thing`/`Model`, but when the editor package is absent (the player)
they fall back to [`entitybase.py`](entitybase.py), a tiny PyQt-free base. So the
same plugin loads in the editor, the desktop player, and the APK.

> **Renderer note.** The player's renderer is still bringing up map-model
> drawing, so plugin gameplay *runs* (state, HUD text via `render_state["hud"]`)
> ahead of the models being visible on screen. The host exposes `things` and
> `hud_message` for the renderer to consume once it draws dynamic models.

---

## Android APK

`player/buildozer.spec` includes `plugins/*`, so the plugin system + bundled
plugins (code and `.obj`/`.mtl` assets) ship inside the APK. The **Android Player
Build** workflow (`.github/workflows/android-build.yml`) bundles
`maps/Tidy_Test.json` as the sample `game.fiopak` (self-contained — the plugin
travels with it), so the on-device build exercises the plugin loader and runtime.
Trigger it from **Actions → Android Player Build → Run workflow**; the APK is
uploaded as the `fio-player-debug-apk` artifact.

---

## Debugging and tests

Set `FIO_PLUGIN_DEBUG=1` for informational load/registration logging (errors are
always shown regardless).

The tests are headless — no display / OpenGL required:

```bash
# Tidy plugin (gameplay)
QT_QPA_PLATFORM=offscreen python plugins/tidy/tests/test_smoke.py       # runtime + integration
QT_QPA_PLATFORM=offscreen python plugins/tidy/tests/test_packaging.py   # .fiopak bundling
python plugins/tidy/tests/test_player.py                                # player path (editor/PyQt/glm blocked)

# Big World plugin (runtime scalability)
python -m plugins.bigworld.tests.test_bigworld
```

---

## Reference map

| Want to… | Read |
|----------|------|
| Understand the whole system | this file |
| Look up a class/method/signature | [`API.md`](API.md) |
| See the annotated API source | [`api.py`](api.py) |
| Use the open-ended engine seam | [`host.py`](host.py) / [API §PluginHost](API.md#pluginhost--the-open-ended-engine-seam) |
| Read a complete gameplay plugin | [`tidy/`](tidy/) |
| Read a runtime-layer plugin | [`bigworld/README.md`](bigworld/README.md) |
| Understand editor wiring | [`integration.py`](integration.py) |
| Understand `.fiopak` bundling | [`packaging.py`](packaging.py) |
| Write for the PyQt-free player | [`entitybase.py`](entitybase.py) |
