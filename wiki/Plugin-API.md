Plugins add new gameplay to Fio — new placeable entity types, their I/O, runtime
behaviour, and editor tooling — **without editing the core editor or engine**.
Drop a package into the `plugins/` directory and it is discovered automatically
at startup. The same plugin can run in the editor's Play mode, the standalone
`.fiopak` desktop player, and the Android build.

This page is the reference for plugin authors. For a guided introduction see the
in-repo [`plugins/README.md`](https://github.com/ViciousSquid/Fio/blob/3.0.0.0510_PreRelease/plugins/README.md);
for the fully-documented source of every class and method see
[`plugins/api.py`](https://github.com/ViciousSquid/Fio/blob/3.0.0.0510_PreRelease/plugins/api.py).

> **API version: 1.6.0** (`plugins.api.API_VERSION`), Fio 3.0. Everything on this page
> ships today. **Fio 3.0 with API 1.6 is a major architectural shift from 1.5:**
> the renderer is now a plugin boundary — see [Swappable renderers](#swappable-renderers)
> and the [Renderer Development Guide](https://github.com/ViciousSquid/Fio/wiki/Renderer-Development-Guide). A plugin can declare the minimum API it needs via `api_version`;
> the loader refuses a plugin that needs a newer host, with a clear message,
> instead of failing inside a hook — see
> [API versioning & dependencies](#api-versioning--dependencies).

> **The big idea.** Beyond the curated surface (entities, I/O, lifecycle hooks,
> and editor extensions), a plugin gets a **[host](#extending-the-engine-host--events)**
> — one object that reaches the engine — and an event bus the engine emits into.
> New behaviour is added by *listening and reacting*, not by editing the engine.
> New engine signals are a one-line `emit()`; plugins can subscribe to events
> that don't exist yet. This is the intended path for extension.

---

## Contents

- [Concepts at a glance](#concepts-at-a-glance)
- [Lifecycle](#lifecycle)
- [Extending the engine: host & events](#extending-the-engine-host--events)
- [Event catalog](#event-catalog)
- [Render hooks](#render-hooks)
- [Swappable renderers](#swappable-renderers)
- [Editor-UI extensions](#editor-ui-extensions)
- [Performance & the kill-switch](#performance--the-kill-switch)
- [Quick start: a complete plugin](#quick-start-a-complete-plugin)
- [API reference](#api-reference)
  - [`FioPlugin`](#fioplugin)
  - [`EditorAPI`](#editorapi-load-time)
  - [`RuntimeAPI`](#runtimeapi-per-play-session)
  - [`TickContext`](#tickcontext)
  - [`io_def`](#io_def)
- [Runtime services](#runtime-services)
- [The HUD](#the-hud)
- [The global key/value store](#the-global-keyvalue-store)
- [Property schemas](#property-schemas)
- [Reading held keys](#reading-held-keys)
- [API versioning & dependencies](#api-versioning--dependencies)
- [Reacting to enable/disable](#reacting-to-enabledisable)
- [Entity classes](#entity-classes)
- [The entity `type` contract](#the-entity-type-contract)
- [Enabling & disabling plugins](#enabling--disabling-plugins)
- [Packaging & distribution](#packaging--distribution)
- [Running outside the editor](#running-outside-the-editor)
- [Testing a plugin](#testing-a-plugin)
- [Design principles](#design-principles)
- [Shipped](#shipped)
- [Roadmap / proposed enhancements](#roadmap--proposed-enhancements)

---

## Concepts at a glance

| Term | What it is |
|------|-----------|
| **Plugin** | A Python sub-package of `plugins/` exposing a `FioPlugin` instance as module-level `PLUGIN` (or a `get_plugin()` factory). |
| **Entity** | An ordinary `Thing`/`Model` subclass the plugin registers so it can be placed, serialized, edited and rendered like a built-in entity. |
| **I/O** | Fio's input/output logic system. A plugin declares the inputs and outputs its entities expose, and registers handlers for the inputs. |
| **Manager** | The process-wide `PluginManager` singleton that discovers, loads, gates and dispatches plugins. |
| **Host object** | The `PluginHost` handed to each plugin's `connect()` — one reach into the engine plus its event bus. |
| **Events** | Named signals the engine emits (`play_start`, `player_damage`, …). Plugins subscribe via `host.on(...)`. |
| **Play host** | Whatever drives the play loop — the engine's `LogicThread` in the editor or `PlayerPluginHost` in the standalone player. |
| **Editor action** | A plugin-provided command exposed through Fio's editor/plugin menus. |
| **Entity wizard** | A plugin-provided creation dialog that gathers initial properties before an entity is placed. |
| **Singleton entity** | An entity type for which only one instance is allowed in a map. |

Everything a plugin touches at gameplay time hangs off one **`logic`** object:
`logic.things` (the scene entities), `logic.player` (camera/eye/angle/pitch),
`logic.io_manager` (fire outputs / register inputs) and
`logic.current_hud_message` (a single HUD prompt line).

---

## Lifecycle

```text
             load_plugins()                 (editor + engine + player, once)
                   │
                   ▼
        register(EditorAPI)  ── entities, I/O, properties, editor extensions
                   │
   play ──►  register_runtime(RuntimeAPI)  ── I/O input handlers
                   │
             on_play_start(logic)          ── build per-session state
                   │
             on_tick(logic, ctx)           ── every tick, after core interactions
                   │
             on_play_stop(logic)           ── restore anything you mutated
```

> The rest of the reference — every class, method and field — is
> [`plugins/API.md`](https://github.com/ViciousSquid/Fio/blob/3.0.0.0510_PreRelease/plugins/API.md)
> in the repository.

---

## Swappable renderers

A plugin can ship a whole renderer. Fio draws through whichever renderer is
active; any object that satisfies the `engine.renderer.Renderer` protocol can be
it, registered by name and swapped in live (`r_renderer <name>`), in the editor
or in Play. **You do not need to understand or inherit the `ForwardRenderer` to
implement another renderer.**

```python
from plugins.api import FioPlugin


def _create(config):
    from .renderer import DeferredRenderer      # OpenGL only once the host asks
    return DeferredRenderer(config)


class DeferredRendererPlugin(FioPlugin):
    name = "deferred_renderer"
    api_version = "1.6.0"                        # the Renderer protocol contract

    def register(self, api):
        api.register_renderer("Deferred", _create)
```

`api.register_renderer(name, factory)` (or `host.register_renderer` from
`connect`) adds *factory* to Fio's renderer registry. The host calls it as
`factory(config)` — `config` is the application `ConfigParser` or `None` — with its
GL context current, and what it returns must satisfy the protocol: lifecycle
(`ready`, `cleanup`), the frame (`render_scene` over the dense `RenderTable` /
`EntityTable` frame input), settings, resources, diagnostics (`render_stats`) and
the host's post-scene drawing operations. It returns `True` when registered and
`False` in a player process with no viewport.

The renderer owns its GL resources: **no GL resource ID survives the lifetime of
the renderer that owns it.** The host discards every handle it was given before
calling `cleanup()`, and a replacement renderer receives fresh resource tables.

Inspect what your renderer is given and what it reports with
[Debug Tables](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables)
(**Debug → Debug Tables**), which labels the active renderer — **FORWARD**,
**DEFERRED** — and works unchanged with any renderer.

The contract, resource lifetime, GL state rules, display modes and a complete
working `DeferredRenderer` are in the
[Renderer Development Guide](https://github.com/ViciousSquid/Fio/wiki/Renderer-Development-Guide).

---

## API versioning & dependencies

A plugin declares the minimum API it needs with `api_version`; a host older than
that refuses it with a clear message instead of failing inside a hook. Plugins
that don't set it default to `"1.0.0"` and always load. `requires` lists other
plugins a plugin depends on.

| Version | Introduced |
|---------|-----------|
| 1.2.0 | `PluginHost`, the engine event bus, `connect(host)` |
| 1.3.0 | render hooks (`render.*` events), renderer registration, editor-UI extensions, the `FIO_NO_PLUGINS` kill-switch |
| 1.4.0 | Tools-menu actions and console commands for developer plugins |
| 1.5.0 | editor content extensions (collapsible property sections, LogicState preset keys, entity inspectors), world pause and actor pick |
| **1.6.0** | **Fio 3.0.** Renderer registration takes the `engine.renderer.Renderer` protocol: a factory is called as `factory(config)` |

**Compatibility with 1.3–1.5.** Fio 3.0 / API 1.6 is a major architectural shift
from 1.5, but it is narrow in what it breaks:

- **Plugins that don't use the renderer contract load and behave unchanged** —
  entities, I/O, runtime hooks, events, editor extensions.
- **Renderer implementations written for 1.3–1.5 are not compatible.** The old
  factory, `cls(texture_loader, grid_size, world_size, config)`, and the old
  forward-renderer interface are gone, and there is no compatibility shim. Port
  such a renderer to the protocol and declare `api_version = "1.6.0"`.
- Code that reached into the old `renderer_core` / `Renderer_F` internals has to
  move to the public contract too.
