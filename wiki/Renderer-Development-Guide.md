# Renderer Development Guide

> **Fio 3.0 with plugin API 1.6 is a major architectural shift from 1.5.**
> The renderer is now a plugin boundary: Fio draws through whichever renderer is
> active, and any object that satisfies the `engine.renderer.Renderer` protocol
> can be that renderer — registered by name, and swapped in **live**, in the
> editor or in Play.

**You do not need to understand or inherit the `ForwardRenderer` to implement
another renderer.** Fio's built-in renderer is one implementation of a public
contract. Yours is another. This page is the contract, the rules around it, and
a complete worked example: a `DeferredRenderer`.

**Contents**

- [Where a renderer sits](#where-a-renderer-sits)
- [The contract in ten steps](#the-contract-in-ten-steps)
- [Resource lifetime](#resource-lifetime)
- [GL state](#gl-state)
- [Display modes](#display-modes)
- [Worked example: DeferredRenderer](#worked-example-deferredrenderer)
- [Hot swapping](#hot-swapping)
- [Checking your renderer: Debug Tables and tests](#checking-your-renderer-debug-tables-and-tests)
- [Optional: reusing `RendererCore`](#optional-reusing-renderercore)
- [Compatibility with plugins written for 1.3–1.5](#compatibility-with-plugins-written-for-13-15)
- [Reference](#reference)

---

## Where a renderer sits

```text
EditorState ─► LogicThread ─► RenderTable / EntityTable ─► engine.renderer (the contract) ─► active renderer ─► OpenGL
                                 (published, double-buffered)        ▲
                                                                     └── QtGameView (the host) drives it
```

The logic thread projects the world into two dense NumPy tables —
[RenderTable](https://github.com/ViciousSquid/Fio/wiki/RenderTable) for brushes and
[EntityTable](https://github.com/ViciousSquid/Fio/wiki/EntityTable) for lights,
sprites, models, effects and portals — and publishes them every frame. The host,
`QtGameView`, hands them to the active renderer as the **frame input**. A
renderer never sees editor objects, the logic thread, or another renderer.

The public surface is the package `engine.renderer`, and only it:

| Name | What it is |
|------|-----------|
| `Renderer` | The protocol every renderer satisfies (`typing.Protocol`, runtime-checkable). |
| `FRAME_INPUT` | The documented keys of the per-frame `config` mapping. |
| `RenderStats` | Per-frame counters the renderer fills and tools read. |
| `SelectionOverlay`, `EffectBillboard` | What to draw for the editor's selection. |
| `WATER_QUALITIES` | `('cheap', 'expensive')`. |
| `register_renderer`, `create_renderer`, `available_renderers`, `DEFAULT_RENDERER` | The registry (`"Forward"` is built in). |

Importing `engine.renderer` loads no OpenGL, so you can write and unit-test
against the contract without a GL context.

---

## The contract in ten steps

### 1. Implement the `Renderer` protocol

A plain class. No base class is required.

| Group | Members |
|-------|---------|
| Settings the host sets | `view_distance` (the shared `engine.view_distance.ViewDistance`), `shadows_enabled: bool`, `water_quality: str` |
| Diagnostics | `render_stats: RenderStats` |
| Lifecycle | `ready` (property: `False` if start-up failed), `cleanup()` |
| The frame | `render_scene(projection, view, camera_pos, primary_selection, config, clear=True, brush_slots=None)`, `set_grid(world_size, grid_size)` |
| Resources | `load_texture(texture_name, subfolder) -> int`, `set_sprite_textures(textures)`, `get_loaded_model(filename)` |
| Host drawing after the scene | `draw_billboards`, `draw_player_glasses`, `draw_bullet_marks`, `draw_connection_lines`, `draw_face_highlight`, `draw_component_overlay`, `draw_collision_visualization` |

The full signatures and docstrings are in
[`engine/renderer/api.py`](https://github.com/ViciousSquid/Fio/blob/3.0.0.0510_PreRelease/engine/renderer/api.py).
`isinstance(obj, engine.renderer.Renderer)` checks that every member is present.

### 2. Register a factory

A factory is any callable `factory(config)` that returns a renderer — usually the
class itself, or a small function that imports it lazily:

```python
from engine.renderer import register_renderer
register_renderer("Deferred", DeferredRenderer)
```

From a plugin, register in `register(api)` with `api.register_renderer(name, factory)`
(or `host.register_renderer` from `connect(host)`). `register` runs UI-free in every
process that loads plugins, so keep OpenGL out of the import path until the
factory is called — see the [example plugin](#the-plugin).

### 3. Declare API version 1.6

```python
class DeferredRendererPlugin(FioPlugin):
    api_version = "1.6.0"
```

A host older than 1.6.0 constructs renderers differently; declaring the version
makes it refuse your plugin with a clear message instead of failing inside a frame.

### 4. Receive a ConfigParser or `None`

The factory is called as `factory(config)` **with the host's GL context
current**. `config` is the application's `ConfigParser` (from `settings.ini`), or
`None` (tests, tools). Read your own settings from it if you like; never require it.
Create your GL objects here or lazily on the first frame.

### 5. Implement `render_scene()`

```python
def render_scene(self, projection, view, camera_pos, primary_selection,
                 config, clear=True, brush_slots=None): ...
```

- `projection`, `view` — column-major `glm.mat4`; `camera_pos` — `(x, y, z)`.
- `brush_slots` — the `RenderTable` slots the logic thread found visible for this camera.
- `clear` — `False` when the host shares one framebuffer between views
  (split-screen) and has cleared it itself; don't clear it again.
- Draw into the **currently bound** framebuffer and viewport. Qt renders into
  its own framebuffer, which is not `0`: read `GL_DRAW_FRAMEBUFFER_BINDING` and
  `GL_VIEWPORT` at the start of the call if you bind anything else (a deferred
  renderer always does).
- Called once per view, every frame. Reset and fill `render_stats`.

### 6. Consume `FRAME_INPUT`

`config` holds exactly the keys documented in `engine.renderer.FRAME_INPUT`.
Ignore any you don't need; never require one outside the set.

| Key | Meaning |
|-----|---------|
| `render_table` | the dense brush projection (`engine.render_table.RenderTable`) |
| `all_brush_slots` | every live brush slot (not hidden, not filtered out by the editor's [view filters](https://github.com/ViciousSquid/Fio/wiki/View-Filters)) |
| `entity_table` | the dense entity projection (lights, sprites, models, effects, portals, path nodes) |
| `visible_thing_slots` | entity rows to draw in this view. Every entity pass — sprites, models, effects, path nodes, portal outlines — draws only these; the editor's view filters leave rows out. A light left out still lights |
| `thing_hidden` | per entity row: hidden/collected in the running world |
| `terrain` | `engine.terrain.Terrain` or `None` |
| `primary_selection` | the same `SelectionOverlay` (or `None`) as the argument |
| `play_mode` | running the game rather than editing |
| `render_mode` | `engine.constants.RENDER_MODE_*` (lit, unlit, wireframe, vertex) |
| `brush_display_mode` | the editor's Display box — see [Display modes](#display-modes) |
| `show_triggers_as_solid`, `show_sprites_in_play_mode`, `grid_visible`, `camera_distance_cull`, `time` | view settings |
| `show_glasses`, `player_glasses_positions`, `player_glasses_sprites` | player presentation |

The tables are columns. A few you will use first:

```python
table = config['render_table']
table.class_bits[slots]          # CLASS_* bits: water, glass, trigger, fog, has-geometry, textured ...
table.colour[slots]              # flat colour, 0..1
table.center[slots], table.half[slots]
models, normals = engine.render_table.model_matrices(table, slots)   # column-major 4x4 / 3x3 per row

entities = config['entity_table']
entities.light_slots             # rows that are lights
entities.light_enabled, entities.pos, entities.light_color, entities.light_params  # (intensity, radius)
```

Read the column catalogues on the [RenderTable](https://github.com/ViciousSquid/Fio/wiki/RenderTable)
and [EntityTable](https://github.com/ViciousSquid/Fio/wiki/EntityTable) pages, and
inspect them live in [Debug Tables](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables).

### 7. Implement the host's overlay and resource methods

- **Resources.** `load_texture(name, subfolder)` loads `assets/<subfolder>/<name>`
  and returns **your** GL texture name (0 on failure). The host loads its sprite
  textures through it and hands the table back with `set_sprite_textures(...)`.
  `get_loaded_model(filename)` returns an already-loaded model or `None` — it never
  loads; the 2D view falls back to a box on `None`.
- **Post-scene drawing.** After `render_scene` the host asks for projectiles
  (`draw_billboards`), players (`draw_player_glasses`), impact marks, editor I/O
  links, face highlights, component-edit handles and collision debug boxes. They
  must exist; how much of each you draw is your choice. A minimal renderer may
  implement them as no-ops.
- **Selection.** `primary_selection` is a `SelectionOverlay`: `brush_id`,
  `outline (pos, size)`, `dashed_bounds`, `effect_billboard`, `gizmo_pos`. Draw
  whichever parts are present. You never need the selected editor object.

### 8. Do not import `editor` or the logic thread

A renderer reads the frame input and nothing else. `engine.renderer` itself
imports nothing from `editor`, and the world, logic and editor-state layers never
import the renderer; Fio's tests enforce both. Yours should keep the same shape.

### 9. Do not access `ForwardRenderer` internals

Not its shaders, uniforms, VAOs, passes or caches — and the host never does
either: it calls only protocol members (also enforced by a test). Nothing in the
contract mentions a forward technique, so a G-buffer, a ray marcher or a
software rasteriser are all equally valid.

### 10. Register under a renderer name

The name is how people choose it: `r_renderer` lists the registry, and
`r_renderer Deferred` swaps to it live. Debug Tables shows it, upper-cased, as the
active renderer's label. `"Forward"` is taken by the built-in renderer;
registering an existing name replaces that entry.

---

## Resource lifetime

Resource lifetime is part of the contract, because renderers are replaced while
Fio runs.

> **No GL resource ID survives the lifetime of the renderer that owns it.**

- **A renderer instance owns its GL resources** — programs, buffers, vertex
  arrays, framebuffers, render targets, and every texture and model it loads,
  including the names it returns from `load_texture`.
- **The host discards renderer-produced GL handles before `cleanup()`.** On a
  swap the host drops its sprite table and the terrain's program and texture
  bindings, then calls the old renderer's `cleanup()`, which must delete
  everything it made.
- **A replacement renderer receives fresh resource tables.** The host reloads
  its sprite textures through the new renderer's `load_texture` and passes them
  with `set_sprite_textures`; the terrain binds the new renderer's resources the
  first time it is drawn.

What this means when you write one:

- Never assume a texture name, buffer or program from another renderer — or from
  an earlier instance of your own — is still valid. It has been deleted.
- Don't keep GL names in module-level or class-level caches that outlive an
  instance.
- `cleanup()` deletes everything, including objects created lazily mid-frame
  (G-buffers resized with the window, instance buffers grown on demand). Free a
  resized resource when you replace it.
- If `ready` is `False` the host calls `cleanup()` on your half-built renderer at
  once and keeps the current one. Make `cleanup()` safe on a partially
  initialised instance.

Fio checks this on its own renderer and on the example below: a test records
every `glGen*` / `glCreate*` made on a renderer's behalf, swaps the renderer out,
and asserts that none of those names is still a live GL object, and that the host
holds none of them (`tests/e2e/test_renderer_gl_lifetime.py`).

---

## GL state

A renderer sets whatever state it needs at the start of each call and may leave
any state behind. It must not rely on state persisting between its calls, and the
host never manages state on its behalf. The host restores only what its own Qt
painter relies on (viewport, scissor, depth/stencil/blend/cull enables, the default
pixel-store alignment) before painting the 2D overlay.

---

## Display modes

`brush_display_mode` is the editor's **Display** box. The host publishes it
every frame; changing the box (or `r_wireframe`) repaints immediately, and the
renderer simply receives the new value on the next frame. How each mode looks is
the renderer's choice:

| Value | Meaning | ForwardRenderer draws |
|-------|---------|-----------------------|
| `Textured` | everything as normal | textured, lit |
| `Solid Lit` | no textures | brushes shaded with their colour |
| `Wireframe` | the world as lines | true brush edges on black, coloured by distance from the eye |
| `Points` | the world as points | a laser-scan point cloud on black, coloured by distance (after Scanner Sombre) |
| `Overlay` | Textured with the wireframe over it | the lit, textured frame with every brush triangle in white |

In Play the host publishes `Textured` unless Wireframe, Points or Overlay is
selected. A renderer that has no special treatment for a mode should draw it as
`Textured`.

---

## Worked example: DeferredRenderer

A complete, working deferred renderer ships with Fio as an example plugin:
[`docs/examples/deferred_renderer/`](https://github.com/ViciousSquid/Fio/tree/3.0.0.0510_PreRelease/docs/examples/deferred_renderer).
Copy the package into `plugins/` and start Fio; `r_renderer Deferred` switches
to it live. It imports only `engine.renderer` and `engine.render_table` — nothing
from the built-in renderer — and Fio's tests run it: swapped in live, lit from
the tables, read by Debug Tables, no GL names left behind
(`tests/e2e/test_deferred_renderer_example.py`).

It is deliberately small: brushes drawn as their boxes in their flat colour, every
enabled light accumulated in one pass, no textures, models or sprites. Those are
additions to this file, not to Fio.

### The plugin

```python
# plugins/deferred_renderer/__init__.py
from plugins.api import FioPlugin


def _create_deferred_renderer(config):
    # Imported when the host creates the renderer -- with its GL context
    # current -- so loading the plugin needs no OpenGL.
    from .renderer import DeferredRenderer
    return DeferredRenderer(config)


class DeferredRendererPlugin(FioPlugin):
    name = "deferred_renderer"
    version = "1.0.0"
    description = "Example: a deferred renderer (r_renderer Deferred)"
    api_version = "1.6.0"          # the Renderer protocol contract

    def register(self, api):
        api.register_renderer("Deferred", _create_deferred_renderer)


PLUGIN = DeferredRendererPlugin()
```

### Construction: settings, diagnostics, owned resources

```python
from engine import render_table
from engine.renderer import RenderStats

class DeferredRenderer:
    def __init__(self, config=None):
        # The settings the host sets; renderer-independent.
        self.view_distance = None
        self.shadows_enabled = True          # this example casts no shadows
        self.water_quality = 'expensive'     # nor distinguishes water cost
        self.render_stats = RenderStats()
        # Shown by Debug Tables under the frame counters.
        self.render_stats.details['G-buffer'] = self._describe_gbuffer

        # GL resources -- every one deleted in cleanup().
        self._textures = {}                  # (subfolder, name) -> GL name
        self._gbuffer = None                 # made on the first frame, at the viewport's size
        self._ready = False
        try:
            self._geometry = _program(_GEOMETRY_VS, _GEOMETRY_FS)
            self._lighting = _program(_LIGHTING_VS, _LIGHTING_FS)
        except Exception as exc:              # a driver that refuses the shaders
            print(f"[DeferredRenderer] shaders failed: {exc}")
            self._geometry = self._lighting = 0
            return                           # ready stays False: the host keeps its renderer
        ...                                  # the unit-cube VAO/VBO and an empty VAO
        self._ready = True

    @property
    def ready(self):
        return self._ready
```

### The frame: geometry pass, then lighting pass

```python
    def render_scene(self, projection, view, camera_pos, primary_selection,
                     config, clear=True, brush_slots=None):
        stats = self.render_stats
        stats.reset()
        table = config['render_table']
        slots = np.asarray(brush_slots if brush_slots is not None else (), dtype=np.int32)

        # Where the host wants the picture: its framebuffer (Qt's, not 0)
        # and its viewport (split-screen views share one framebuffer).
        target = int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        x, y, width, height = (int(v) for v in gl.glGetIntegerv(gl.GL_VIEWPORT))
        if clear:
            gl.glClear(gl.GL_COLOR_BUFFER_BIT | gl.GL_DEPTH_BUFFER_BIT)
        self._ensure_gbuffer(width, height)

        # 1. Geometry pass: brushes into the G-buffer (position, normal, albedo).
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._gbuffer[0])
        gl.glViewport(0, 0, width, height)
        ...                                  # clear, depth test on, geometry program, matrices
        surfaces = slots[(table.class_bits[slots] & _NOT_SURFACES) == 0]   # no trigger/fog volumes
        models, normals = render_table.model_matrices(table, surfaces)
        selected = (table.slot_of_id.get(primary_selection.brush_id)
                    if primary_selection is not None and primary_selection.brush_id else None)
        for row, slot in enumerate(surfaces):
            colour = SELECTED_TINT if slot == selected else table.colour[slot]
            gl.glUniformMatrix4fv(model_loc, 1, gl.GL_FALSE, models[row])
            gl.glUniformMatrix3fv(normal_loc, 1, gl.GL_FALSE, normals[row])
            gl.glUniform3f(albedo_loc, *(float(c) for c in colour))
            gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
            stats.draw_calls += 1

        # 2. Lighting pass: the G-buffer, lit, into the host's framebuffer.
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, target)
        gl.glViewport(x, y, width, height)
        ...                                  # bind the three G-buffer textures
        positions, colours, params = self._lights(config)
        ...                                  # upload the light arrays, draw one full-screen triangle
        stats.draw_calls += 1
```

The lights come straight from the `EntityTable` light columns — the same rows
Fio's own renderer reads:

```python
    @staticmethod
    def _lights(config):
        entities = config['entity_table']
        slots = entities.light_slots
        if len(slots):
            keep = entities.light_enabled[slots]
            hidden = config.get('thing_hidden')
            if config.get('play_mode') and hidden is not None:
                keep = keep & ~np.asarray(hidden)[slots]   # out of the running world
            slots = slots[keep][:MAX_LIGHTS]
        return (np.ascontiguousarray(entities.pos[slots], dtype=np.float32),
                np.ascontiguousarray(entities.light_color[slots], dtype=np.float32),
                np.ascontiguousarray(entities.light_params[slots], dtype=np.float32))
```

The lighting shader discards pixels the geometry pass never wrote, so the host's
clear colour — the fog colour, as the view distance sets it — stays the background:

```glsl
vec4 position = texture(gPosition, vUV);
if (position.w == 0.0) discard;           // background: keep the host's clear
vec3 normal = texture(gNormal, vUV).xyz;
vec3 albedo = texture(gAlbedo, vUV).rgb;
vec3 light = uAmbient;
for (int i = 0; i < uLightCount; ++i) {
    vec3 to_light = uLightPos[i] - position.xyz;
    float distance = length(to_light);
    float reach = clamp(1.0 - distance / max(uLightParams[i].y, 1.0), 0.0, 1.0);
    float diffuse = max(dot(normal, to_light / max(distance, 1e-4)), 0.0);
    light += uLightColor[i] * uLightParams[i].x * reach * reach * diffuse;
}
FragColor = vec4(albedo * light, 1.0);
```

### Resources it owns, and gives back

```python
    def load_texture(self, texture_name, subfolder):
        key = (subfolder, texture_name)
        if key not in self._textures:
            self._textures[key] = self._upload_image(
                os.path.join('assets', subfolder, texture_name))
        return self._textures[key]

    def set_sprite_textures(self, textures):
        self._sprite_textures = dict(textures)   # names this renderer made

    def get_loaded_model(self, filename):
        return None                              # loads no models; the 2D view draws a box

    def _ensure_gbuffer(self, width, height):
        if self._gbuffer is not None and self._gbuffer_size == (width, height):
            return
        self._delete_gbuffer()                   # a resize frees the old one first
        ...

    def cleanup(self):
        """Delete every GL name this renderer made. It is not used again."""
        self._delete_gbuffer()
        names = [t for t in self._textures.values() if t]
        if names:
            gl.glDeleteTextures(names)
        self._textures.clear()
        ...                                      # VAOs, VBO, both programs
        self._ready = False
```

### The host's post-scene drawing

```python
    def draw_billboards(self, projection, view, positions, size, tex_id):
        return 0                                 # this example draws none of these

    def draw_player_glasses(self, projection, view, positions,
                            width=40.0, height=18.0, lift=0.0, sprites=()):
        pass
    # ... draw_bullet_marks, draw_connection_lines, draw_face_highlight,
    #     draw_component_overlay, draw_collision_visualization likewise
```

### Where to take it next

- **Textures.** `table.tex_name_id` gives each brush face a texture-name id;
  `table.texture_names()` maps ids to names you load with your own `load_texture`.
- **Angled brushes.** Rows with `CLASS_HAS_GEOMETRY` carry a convex plane set in
  `table.geometry_records`; build a mesh from it rather than drawing the box.
- **Models and sprites.** `entity_table` holds model recipes, sprite keys and
  transforms; `etable.effect_slots` are the effects.
- **Shadows.** `entities.light_casts_shadows` marks shadow casters; honour
  `self.shadows_enabled`.
- **Depth for later passes.** If your post-scene drawing needs depth, blit it
  from the G-buffer into the host's framebuffer after lighting.

---

## Hot swapping

`QtGameView.switch_renderer(name)` replaces the renderer live, in the editor and
in Play (`r_renderer <name>` on the console):

1. Create the new renderer with the view's GL context current. If its factory
   raises, or `ready` is `False` (it is cleaned up at once), the current renderer
   keeps running and the call returns `False`.
2. Carry the session settings across: `shadows_enabled`, `water_quality`, and the
   shared `view_distance`.
3. Drop everything the host held from the old renderer: the sprite table and the
   terrain's renderer bindings.
4. Reload the host's sprites and level textures through the new renderer.
5. Call the old renderer's `cleanup()`.

The frame input is rebuilt every frame, so a renderer needs nothing from its
predecessor.

---

## Checking your renderer: Debug Tables and tests

[**Debug Tables**](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables)
(**Debug → Debug Tables**) works unchanged with any renderer. It shows:

- the active renderer's name as a label — **FORWARD**, **DEFERRED** — in Fio orange;
- exactly what your renderer is given: the `RenderTable` and `EntityTable`
  columns, the visible slots, and (follow an object by selecting it, in the
  viewport or the Scene Hierarchy) every column of its rows;
- what your renderer reports: `render_stats.draw_calls`, `shadow_draw_calls`,
  `batched_draws`, `visible_tris`, pass timings, and any lines you publish in
  `render_stats.details`.

Use it as an **oracle, but don't trust it on its own**: it reports what the
renderer says it did. Fio's own test pairs it with a measurement that never opens
it — wrapping `OpenGL.GL`'s draw calls and reading the published tables directly —
and requires the two to agree, frame by frame, across a renderer swap
(`tests/e2e/test_debug_tables_oracle.py`). For your renderer:

- `draw_calls + shadow_draw_calls` should equal the GL draws your `render_scene`
  issues, leaving out editor overlays (grid, selection outline, gizmo);
- `tests/helpers/renderers.py` has `StubRenderer` (the whole contract, no GL) and
  `FlatRenderer` (one flat colour) to copy from;
- `tests/e2e/test_renderer_gl_lifetime.py` has the `GLObjectLedger` that checks
  [resource lifetime](#resource-lifetime) — point it at your class.

---

## Optional: reusing `RendererCore`

`engine.renderer.core.RendererCore` is renderer-independent infrastructure:
texture and model loading, table lookups and transforms, culling, convex-brush
meshes and the editor overlays (grid, gizmo, outlines). Inherit it if it saves you
work; nothing requires it, and it never reaches into the forward renderer. If you
use it, its `cleanup()` frees what it made — call it from yours.

---

## Compatibility with plugins written for 1.3–1.5

- **Plugins that don't use the renderer contract load and behave unchanged** —
  entities, I/O, runtime hooks, editor extensions, events.
- **Renderer implementations written for 1.3–1.5 are not compatible.** The old
  factory (`cls(texture_loader, grid_size, world_size, config)`) and the old
  forward-renderer interface no longer exist, and there is no compatibility shim.
  Port such a renderer to the protocol above, and declare `api_version = "1.6.0"`.
- Code that reached into the old `renderer_core` / `Renderer_F` internals has to
  move to the public contract too.

---

## Reference

- [`engine/renderer/api.py`](https://github.com/ViciousSquid/Fio/blob/3.0.0.0510_PreRelease/engine/renderer/api.py) — the contract, with docstrings
- [`engine/renderer/registry.py`](https://github.com/ViciousSquid/Fio/blob/3.0.0.0510_PreRelease/engine/renderer/registry.py) — the registry
- [`docs/examples/deferred_renderer/`](https://github.com/ViciousSquid/Fio/tree/3.0.0.0510_PreRelease/docs/examples/deferred_renderer) — the worked example
- [Plugin API](https://github.com/ViciousSquid/Fio/wiki/Plugin-API) — registering from a plugin
- [Renderer Technical overview](https://github.com/ViciousSquid/Fio/wiki/Renderer-Technical-overview) — how the built-in renderer works
- [Debug Tables](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables) — inspect what a renderer is given
- [Console commands](https://github.com/ViciousSquid/Fio/wiki/Console-commands) — `r_renderer`, `r_wireframe`
