# Renderer Technical Overview

Fio's built-in renderer, `ForwardRenderer`, is a forward OpenGL 3.3 Core renderer designed for low-power hardware, with Snapdragon 8cx Gen 3 / Adreno-class hardware as a development and validation target.

> **Fio 3.0 / plugin API 1.6 is a major architectural shift from 1.5.** The renderer is no longer one fixed implementation (`engine/renderer_core.py` + `Renderer_F`). It is a **plugin boundary**: the `engine/renderer` package defines a `Renderer` protocol, a registry of named renderers, optional shared infrastructure (`core`) and the built-in `ForwardRenderer` (`forward`). The host draws through whichever renderer is active and can swap it live (`r_renderer`). Renderer implementations written for API 1.3–1.5 are not compatible (there is no shim); plugins that do not use the renderer contract load and run unchanged.
>
> To write your own renderer, see the **[Renderer Development Guide](https://github.com/ViciousSquid/Fio/wiki/Renderer-Development-Guide)**.

The renderer deliberately separates two concerns:

- **Authoring and world authority:** flexible, object-oriented Python world state.
- **Execution state:** persistent dense numerical projections used for visibility, classification, sorting, batching, simulation hand-off and GPU submission.

The renderer does not treat the authored Python object graph as its primary frame-time execution representation.

The defining boundary is:

```
authoritative world
      │
      ▼
object-oriented world state
      │
      ├── RenderTable
      └── EntityTable
              │
              ▼
      dense numerical execution state
              │
        ┌─────┴─────┐
        ▼           ▼
   visibility   classification
        │           │
        └─────┬─────┘
              ▼
        render keys / runs
              │
              ▼
      packed instance payloads
              │
              ▼
        OpenGL 3.3 Core
```

This is the defining characteristic of the current renderer.

Since Fio 3.0 the path from the tables to OpenGL runs through one public boundary:

```
EditorState → LogicThread → RenderTable / EntityTable → Renderer API (engine.renderer) → active renderer → OpenGL
```

Everything a renderer needs arrives through the dense tables and the per-frame input; it never needs editor objects, the logic thread, or another renderer's internals.

---

# 1. Representation Boundary

Fio's renderer is a sequence of representation changes:

```
world authority
    ↓
Python objects / authored dictionaries
    ↓
persistent dense execution projections
    ↓
change-driven row updates
    ↓
visibility / classification
    ↓
numerical render keys
    ↓
stable sorting
    ↓
contiguous runs
    ↓
packed GPU instance data
    ↓
OpenGL submission
```

The dense tables are **derived execution representations**.

They do not replace the authoritative world.

A brush, `Thing`, entity or other authored object remains the source of truth. The renderer projects only the information it needs into compact numerical columns.

The important design rule is:

> **The authoring model is object-oriented. The execution model is data-oriented.**

The renderer therefore is not simply an object renderer with NumPy added around it. Once the relevant state has crossed the projection boundary, the expensive path stays numerical wherever practical.

---

# 2. Change-Driven Execution

The current renderer is **change-driven**, not poll-driven.

Fio maintains a runtime change journal for render-relevant mutations.

Conceptually:

```
world mutation
     ↓
change journal
     ↓
affected object / slot
     ↓
row-local projection update
     ↓
dense execution state
```

There are three important update classes:

- **Row-set changes:** a full reconciliation is required when brushes/entities are added, removed or replaced.
- **Journalled changes:** only affected rows are refreshed when an object's render-relevant state changes.
- **Continuously dynamic rows:** mover/door transforms are maintained by dense runtime state and published to the render projection as they move.

Editor transactions can also refresh only the affected rows when the row set is unchanged.

This distinction matters because a dense table that reread every Python object every frame would merely hide object traversal behind array construction.

The intended hot-path behaviour is:

```
unchanged world
     ↓
little or no row resolution

changed world
     ↓
resolve affected rows

moving runtime state
     ↓
update the genuinely dynamic rows
```

The Debug Tables instrumentation exposes how many rows were actually read during preparation.

---

# 3. RenderTable

`engine/render_table.py` contains the dense render projection for brush and geometry-oriented state.

It is one row per brush and is addressed by an integer slot.

Representative columns include:

| Column | Purpose |
|---|---|
| `bounds[N,6]` | contiguous centre + half-extent data used by culling |
| `rot[N,4]` | rotation axis/angle information |
| `hidden[N]` | live visibility state |
| `class_bits[N]` | dense render classification |
| `tex_name_id[N,6]` | interned texture-name IDs |
| `uv_scale[N,6,2]` | per-face UV scale |
| `uv_angle[N,6]` | per-face UV rotation |
| `uv_shift[N,6,2]` | per-face UV offsets |
| `uv_natural[N,6]` | natural-scale flags |
| `uv_has_scale[N,6]` | explicit scale flags |
| `colour[N,3]` | resolved base colour |
| `glow_colour[N,3]` | resolved glow colour |
| `geo_epoch[N]` | geometry invalidation epoch |
| `geometry_id[N]` | dense convex-geometry record ID |
| `water_tint[N,3]` | resolved water tint |
| `water_params[N,7]` | water material parameters |
| `glass_color[N,3]` | resolved glass colour |
| `glass_params[N,5]` | glass material parameters |
| `fog_color[N,3]` | fog-volume colour |
| `fog_params[N,2]` | fog-volume parameters |

The exact columns can evolve.

The architectural rule does not:

> `RenderTable` is a derived execution projection, not a second source of truth.

It also maintains execution-side structures such as:

- stable brush IDs
- slot mappings
- dense references
- dynamic mover/door slots
- interned texture names
- convex geometry records
- cached shown-slot masks

These are execution structures, not authored world state.

---

# 4. RenderTable Classification

Brush classification is resolved into compact bit fields.

Representative flags include:

```
CLASS_HIDDEN_AUTHORED
CLASS_WATER
CLASS_FOG
CLASS_GLASS
CLASS_GLOW
CLASS_TRIGGER
CLASS_SUBTRACT
CLASS_HAS_GEOMETRY
CLASS_TEXTURED
CLASS_SHADOW_CASTER
CLASS_DYNAMIC
CLASS_TEXTURE_TILING
```

This replaces repeated frame-time dictionary/string/material tests with numerical masks.

Examples:

```
class_bits & CLASS_NON_OPAQUE
class_bits & CLASS_TEXTURED
class_bits & CLASS_DYNAMIC
class_bits & CLASS_SHADOW_CASTER
```

The same projection also records geometry identity. Ordinary boxes use shared cube geometry; convex brushes can use dense geometry records keyed by `geometry_id`.

Convex geometry invalidation is tracked separately through `geo_epoch`, so reshaping a convex brush can refresh the affected geometry rows without rebuilding unrelated rows.

---

# 5. EntityTable

`engine/entity_table.py` provides the dense entity projection.

It is the entity half of the same execution architecture.

Representative state includes:

- position
- class bits
- hidden state
- light colour and parameters
- light enable/shadow state
- portal target slot
- portal activity/direction
- portal dimensions and basis
- portal fade/colour/rim state
- effect type and playback state
- sprite identity and size
- fixed billboard yaw
- render alpha
- model recipe identity
- model transforms
- model normal matrices

The table also maintains dense slot vectors such as:

```
all_slots
light_slots
portal_slots
monster_slots
path_node_slots
effect_slots
```

Classification is represented through compact bits such as:

```
ENT_SKIP
ENT_ALWAYS_SPRITE
ENT_HAS_MODEL
ENT_MODE_MODEL
ENT_MODE_BILLBOARD
ENT_HAS_SPRITE
ENT_ENTITY_SPRITE
ENT_PROP
ENT_LIGHT
ENT_MONSTER
ENT_PORTAL
ENT_PATH_NODE
ENT_EFFECT
ENT_CULL_EXEMPT
```

This lets the renderer select model, sprite, effect, portal and other execution paths with numerical masks instead of repeatedly traversing Python entity objects.

Dynamic state is still represented correctly: moved entities, hidden/collected Props, respawn fades, portal fades, effect state and other render-relevant runtime changes update only the rows that need them.

---

# 6. Why There Are Two Tables

The two projections represent different execution domains:

```
authoritative world
       │
       ├──────────────► RenderTable
       │                 brush / material / geometry state
       │
       └──────────────► EntityTable
                         entity / light / portal / effect / model state
```

They are not redundant copies of the world.

The authoring model does not need to be redesigned merely because the renderer benefits from a different memory layout.

The projection is deliberately narrower than the authoritative world.

---

# 7. Threaded Frame Publication

Since 2.5.10, Fio uses **strict double-buffered render publication**.

There are two persistent `RenderState` objects:

```
logic thread
     │
     ▼
write buffer
     │
     │ completed frame
     ▼
 publish
     │
     ▼
read buffer
     │
     └── borrowed by renderer for one paint
```

The renderer acquires a lease on the published read buffer for the duration of `paintGL()`.

If the read buffer is still borrowed when the logic thread completes another frame, the swap is declined. The completed write buffer remains intact until the current paint releases its borrow.

This gives the key invariant:

> **The renderer never reads a render state that the logic thread is simultaneously rewriting.**

There is no third spare render buffer.

The same persistent RenderTable/EntityTable projections therefore survive buffer recycling rather than being reconstructed from scratch merely because a frame was published.

UI code that only needs an isolated scalar or flag can use the published-value path without borrowing the complete frame.

---

# 8. QtGameView

`engine/qt_game_view.py` owns the Qt/OpenGL viewport and frame orchestration.

Responsibilities include:

- OpenGL context ownership
- viewport state
- camera state
- projection state
- frame acquisition
- release of render-state leases
- editor/runtime view coordination
- split-screen layout
- HUD and editor overlays
- renderer selection and live renderer replacement
- the host's own resources: the sprite texture table and level textures, loaded *through* the active renderer

`QtGameView` is the **host**. It depends only on what `engine.renderer` exports and uses only `Renderer` protocol members (`tests/renderer/test_renderer_boundary.py` checks this). Each frame it builds the frame input (`FRAME_INPUT`) and calls `render_scene(...)` once per view, then its post-scene operations (`draw_billboards`, `draw_player_glasses`, `draw_bullet_marks`, `draw_connection_lines`, …) for the same view.

When threading is enabled, `paintGL()` borrows the published render state and releases it in a `finally` block when the paint completes.

The active view-distance configuration is also owned at the camera/view layer rather than being treated as a world-streaming limit. The host assigns its one shared `ViewDistance` instance to the renderer, so a renderer swap keeps it.

### Display mode

The editor's **Display** box (bottom bar) is published to the renderer as the frame input `brush_display_mode`:

| Display | Meaning |
|---|---|
| `Wireframe` | the world as lines |
| `Points` | the world as points |
| `Solid Lit` (default) | brushes shaded with their colour, **no textures** |
| `Textured` | everything drawn as normal |
| `Overlay` | `Textured`, with the world's wireframe drawn over it |

Changing the box repaints immediately; the renderer simply receives the new value with the next frame. How each mode looks is the renderer's choice. In Play the host publishes `Textured` unless `Wireframe`, `Points` or `Overlay` is selected — those carry over into Play as debug views. The console's `r_wireframe` drives the same box.

### Renderer hot swap

`QtGameView.renderer_mode` is the registry name of the active renderer. `QtGameView.switch_renderer(name)` replaces it **live**, in the editor or in Play (the console's `r_renderer <name>`):

1. The new renderer is created with the view's GL context current.
2. If creation raises, or the new renderer's `ready` is `False` (it is then cleaned up and discarded), the current renderer keeps running and the call returns `False`.
3. Session settings carry over: `shadows_enabled`, `water_quality` and the shared `view_distance`.
4. The host drops every GL name it held from the old renderer: the terrain's renderer bindings are released (`Terrain.release_renderer_resources()`), the sprite texture table is rebuilt, and assets, sprites and level textures are reloaded through the new renderer.
5. Only then is `old.cleanup()` called.

It returns `True` when `name` is the active renderer afterwards. Nothing else needs carrying across: the frame input is rebuilt every frame, so a renderer needs nothing from its predecessor.

---

# 9. The `engine/renderer` Package

Before Fio 3.0 the renderer was a single implementation: `engine/renderer_core.py` plus the `Renderer_F` forward renderer. In 3.0 it is the `engine/renderer` package:

```
engine/renderer/
├── __init__.py   public surface: Renderer, RenderStats, SelectionOverlay, EffectBillboard,
│                 FRAME_INPUT, WATER_QUALITIES, DEFAULT_RENDERER, register_renderer,
│                 create_renderer, available_renderers, renderer_factory
├── api.py        the Renderer protocol (the contract), FRAME_INPUT, RenderStats,
│                 selection descriptors, the GL-state rule
├── registry.py   name → factory
├── core/         optional, renderer-independent infrastructure (RendererCore)
└── forward/      ForwardRenderer, Fio's built-in renderer
```

Importing `engine.renderer` loads no OpenGL: the contract can be imported, implemented and tested without a GL context.

### The `Renderer` protocol

`engine/renderer/api.py` defines `Renderer`, a `runtime_checkable` `typing.Protocol`. A renderer is any object that satisfies it; it inherits from nothing in Fio. The contract covers:

- **settings:** `view_distance`, `shadows_enabled`, `water_quality` (one of `WATER_QUALITIES`: `'cheap'`, `'expensive'`)
- **diagnostics:** `render_stats` (`RenderStats`)
- **lifecycle:** `ready`, `cleanup()`
- **the frame:** `render_scene(projection, view, camera_pos, primary_selection, config, clear=True, brush_slots=None)`, `set_grid(world_size, grid_size)`
- **resources:** `load_texture`, `set_sprite_textures`, `get_loaded_model`
- **host drawing operations:** `draw_billboards`, `draw_player_glasses`, `draw_bullet_marks`, `draw_connection_lines`, `draw_face_highlight`, `draw_component_overlay`, `draw_collision_visualization`

`config` is the frame input: the keys documented in `FRAME_INPUT` (`render_table`, `all_brush_slots`, `entity_table`, `visible_thing_slots`, `thing_hidden`, `terrain`, `primary_selection`, `play_mode`, `render_mode`, `brush_display_mode`, …). A renderer may ignore any key; it must not require keys outside the set. The primary selection arrives as a `SelectionOverlay` descriptor built by the editor, so a renderer needs nothing else about the selected object.

**GL state rule:** a renderer sets whatever state it needs at the start of each call and may leave any state behind. The host restores only what its own Qt painter relies on before painting the 2D overlay.

### The boundary

The boundary is enforced on the import graph by `tests/renderer/test_renderer_boundary.py`:

- `engine.renderer` imports nothing from `editor`
- the public surface (`api`, `registry`, `__init__`) imports no OpenGL, Qt, glm or implementation
- `core` never imports `forward`
- code outside the package imports only `engine.renderer` itself, never an implementation module
- the world, logic and editor-state layers do not import the renderer at all
- the host uses only the protocol, and every frame-input key the built-in renderer reads is documented in `FRAME_INPUT`

### Registry

`engine/renderer/registry.py` maps names to factories. `"Forward"` is built in (and is `DEFAULT_RENDERER`); it is named by import path and resolved on first use. A factory is called as `factory(config)`, with the host's GL context current, where `config` is the application `ConfigParser` or `None` — usually the factory is the renderer class itself.

Plugins add renderers with `api.register_renderer(name, factory)` in `register(api)` (or `host.register_renderer`), declaring `api_version = "1.6.0"`. `docs/examples/deferred_renderer/` is a complete worked example. See the [Renderer Development Guide](https://github.com/ViciousSquid/Fio/wiki/Renderer-Development-Guide).

### `core` — RendererCore

`engine/renderer/core` is optional infrastructure a renderer may reuse (by subclassing `RendererCore`) or ignore: texture/model/effect-frame/shader resources, the cube and sprite primitives and the angled-brush mesh cache, dense-table lookups, culling and pass classification, light/shadow-caster selection, portal camera maths, editor and debug overlays, and the `timed_pass` decorator. None of it assumes a lighting model or pass structure.

### `forward` — ForwardRenderer

`ForwardRenderer` (`engine/renderer/forward`) is Fio's production forward renderer, registered as `"Forward"`. It implements the protocol and builds on `RendererCore`; its technique-specific parts are split into `renderer.py` (`render_scene` orchestration), `passes.py`, `lighting.py`, `portals.py`, `instancing.py`, `shaders.py` and `distance.py`.

The main scene requires the dense projections:

- dense RenderTable brush slots
- dense EntityTable state
- published visibility selections
- numerical classification data

The main frame pipeline is:

```
RenderTable slots
      ↓
camera distance selection where enabled
      ↓
dense brush classification
      ↓
depth ordering for state-sensitive passes
      ↓
batched / instanced submission

EntityTable slots
      ↓
camera distance selection where enabled
      ↓
dense entity classification
      ↓
model / sprite / effect selection
      ↓
instanced submission
```

The authored Brush/Thing lists remain available to systems that genuinely require world objects.

They are not the primary render batching input.

The renderer does not fall back to the old object-level sprite or brush renderer when dense execution state is available.

---

# 10. Camera Distance Culling

`engine/view_distance.py` owns the active camera view-distance setting.

Current defaults are:

```
default: 4096 world units
minimum: 500
maximum: 20000
```

The current view distance also defines the projection far plane.

`engine/render_cull.py` contains the GL-free distance broad-phase.

The test uses squared X/Z distance, avoiding per-object square roots.

Where contiguous position arrays are available, the arithmetic is vectorised.

The broad-phase is an optimisation. The projection far plane remains the final fragment-level clipping boundary.

Large objects can therefore survive the broad-phase because their centres remain within the configured radius while their geometry extends beyond it.

The main camera can enable or disable the CPU distance broad-phase independently of other views. Portal and shadow rendering use their own scene selection and do not simply inherit the main camera's distance-filtered set.

---

# 11. Frustum Culling

Frustum tests operate directly on the dense projections.

Brushes use:

```
RenderTable.center
RenderTable.half
```

against the current view-frustum planes.

The resulting mask is numerical and produces slot indices for later classification and drawing.

Portal virtual views apply the same principle from a different camera.

Entity frustum selection operates directly on EntityTable positions and projected bounds.

The important rule is:

> **Once data has crossed into the execution projection, repeated visibility work remains numerical wherever practical.**

The renderer therefore does not need to rebuild a list of Python objects merely to answer whether those objects are inside a view.

---

# 12. Visibility and GPU Culling

Fio has several distinct visibility stages:

```
camera distance broad-phase
            ↓
frustum visibility
            ↓
hidden / collected state
            ↓
material / entity classification
            ↓
pass-specific filtering
            ↓
GPU depth + stencil + face culling
```

These stages have different purposes.

Distance culling reduces CPU-side preparation.

Frustum culling rejects objects outside the current view volume.

Dense state masks remove objects that should not enter a pass.

GPU depth, stencil and back-face tests reject work that remains after CPU-side selection.

---

# 13. Opaque Back-Face Culling

The main opaque filled brush passes now use OpenGL back-face culling where it is safe.

The culling path is enabled for filled lit/unlit opaque rendering and deliberately remains disabled for:

- transparent rendering
- wireframe mode
- vertex mode

The renderer restores the GL state after the opaque pass.

This is a GPU-work reduction, not merely a CPU classification optimisation.

On the 24k-brush stress map, profiling found that roughly half or more of submitted box faces could be facing away from the camera. Measurements under Mesa llvmpipe showed fewer depth-passing fragments and lower opaque-pass timings in several camera poses.

Visual regression tests cover:

- ordinary boxes
- clipped convex brushes
- lit mode
- unlit mode
- culling on/off equivalence
- transparent/wireframe/vertex exclusions
- post-pass GL state

---

# 14. Render Keys

`engine/render_keys.py` provides the numerical draw-key machinery.

`KeyLayout` declares fields that cannot vary inside one draw and packs them into sortable signed `int64` keys.

Conceptually:

```
dense render state
       ↓
key fields
       ↓
packed int64 key
       ↓
stable sort
       ↓
equal-key runs
```

The governing rule is:

> **Everything that cannot vary within one draw belongs in the render key. Everything that can vary within the draw travels as instance data.**

The packing code validates field ranges so an oversized value cannot silently collide with another key field.

---

# 15. Equal-Key Runs

Equal keys become contiguous runs after sorting.

A run represents a set of items that can share the GPU state encoded by that key.

The renderer therefore works toward:

```
dense items
    ↓
numeric sort
    ↓
run A ─────────► one submission
run B ─────────► one submission
run C ─────────► one submission
```

The remaining Python control flow is concentrated around:

- render runs
- GPU state changes
- pass boundaries
- bounded irregular collections such as lights or portals

The objective is not to remove Python completely.

The objective is:

> **No unnecessary Python loop whose cost scales with world population.**

---

# 16. Brush Rendering

Brush rendering consumes RenderTable slots.

The renderer classifies brushes into groups such as:

- opaque
- textured opaque
- solid opaque
- transparent
- glow
- water
- glass
- fog

The textured brush path uses a packed key consisting of:

```
texture
face
```

The six cube faces map to fixed vertex ranges in the shared cube VAO.

Per-instance data carries values that may vary within the draw, while the key identifies state that must remain shared.

The submission path is:

```
RenderTable
    ↓
visible/classified slots
    ↓
texture + face key
    ↓
stable numerical sort
    ↓
equal-key runs
    ↓
packed instance data
    ↓
glDrawArraysInstanced
```

Reusable staging arrays and buffers reduce per-frame allocation pressure.

---

# 17. Sprite Rendering

Sprite rendering has a single dense execution path.

```
EntityTable
    ↓
sprite slots
    ↓
sprite recipe / texture IDs
    ↓
optional camera-depth calculation
    ↓
stable lexicographic sort
    ↓
texture runs
    ↓
packed instance buffer
    ↓
glDrawArraysInstanced
```

The sprite pass reads dense values such as:

- position
- sprite size
- fixed billboard yaw
- render alpha
- sprite texture identity

It does not touch the corresponding Python entity objects.

Texture is the primary run key.

When camera depth is supplied, depth is the secondary numeric sort key, so back-to-front ordering can be established within texture runs without a separate object-level sorting phase.

Reusable scratch arrays and `out=` NumPy writes avoid repeatedly creating staging arrays simply to feed the GPU.

---

# 18. Model Rendering

Model rendering is a distinct execution path because models have different resource and geometry requirements.

EntityTable stores:

- model recipe identity
- model transform
- normal matrix
- render alpha

The actual model assets remain cached resources.

The relationship is:

```
EntityTable model identity
        ↓
model recipe
        ↓
cached model resource
        ↓
GPU geometry
```

This keeps heavyweight model resources out of the per-frame projection.

The production renderer uses the instanced model path rather than reviving a per-object legacy renderer.

---

# 19. Procedural Effects

The `Effect` primitive has a dedicated dense entity path.

Effect rows are tracked through `EntityTable.effect_slots`.

Examples include:

- FIRE
- EXPLOSION
- ORB
- CUSTOM effect content where supported

Runtime playback state is represented numerically and advanced as column data.

The renderer submits effect billboards through an instanced path rather than rebuilding effect objects for every frame.

---

# 20. Portals

Fio's native portal renderer uses stencil-buffer masking and bounded virtual views.

Current limits include:

```
MAX_PORTALS = 4
MAX_PORTAL_RECURSION = 1
```

Portal topology and render state are part of EntityTable.

A portal view conceptually performs:

```
portal EntityTable row
       ↓
destination / basis
       ↓
virtual camera
       ↓
dense RenderTable frustum selection
       ↓
dense EntityTable frustum selection
       ↓
normal numeric material/entity passes
       ↓
stencil-masked portal image
```

Portal scenes do not fall back to reconstructing a Python object render list.

A portal is another camera over the same dense world projection.

Recursion is explicitly bounded so frame cost cannot grow through uncontrolled recursive scene traversal.

---

# 21. Lighting

`ForwardRenderer` uses forward lighting.

The shared lighting path uses a dense CPU representation matching a GLSL std140 light block.

Current shader capacities are:

| Shader group | Light capacity |
|---|---:|
| General | 64 |
| ARM variants | 16 |
| Water | 8 |
| Terrain | 8 |

The renderer uploads the active light block once per frame and the lit shaders consume that shared data.

This avoids repeatedly constructing and uploading essentially the same light state for individual draw calls.

The renderer clamps active-light counts to the capacity declared by the shader actually being used.

---

# 22. Shadows

Point-light shadows use depth cube maps.

Current defaults include:

```
MAX_SHADOW_LIGHTS = 8
SHADOW_MAP_SIZE   = 384
```

Shadow resources are cached and reused.

The shadow pass consumes dense light state from EntityTable and dense shadow-caster classification from RenderTable.

It does not traverse authored Brush/Thing/Light collections simply to rediscover which objects cast shadows.

Shadow rendering therefore shares the same execution boundary as the main renderer.

---

# 23. Terrain

Terrain maintains its own rendering/resource path rather than being forced into ordinary brush batching.

It can share:

- camera state
- lighting
- fog/environment state
- renderer resource management

while keeping terrain-specific GPU buffers and shader logic.

The general rule is:

> **Dense execution is the common strategy; one universal representation is not.**

---

# 24. Water, Glass and Fog

These are state-sensitive specialised passes.

They may depend on blending, depth, stencil, scene-transmission textures and specialised shader parameters, so they are not forced through the ordinary opaque batching path.

### Water

The old planar water-reflection capture system has been removed.

The current water path uses screen-space scene transmission/refraction and optional environment/cubemap resources.

Maps that still contain the older saved `water_reflections` property remain loadable, but that obsolete property no longer drives a planar reflection pass.

### Glass

Glass can use the already-rendered scene as a transmission source and has its own state/resources.

### Fog

Fog volumes use dedicated volume rendering.

The current pass renders all six faces of a box volume where required, including the bottom face when the camera is inside the volume, preventing the floor from remaining visibly unfogged.

These specialised passes still consume dense RenderTable state.

---

# 25. Frame Passes

The renderer is organised as a collection of passes rather than one universal draw path.

A frame broadly contains:

1. framebuffer and view setup
2. grid/editor rendering
3. main-camera distance selection where enabled
4. dense brush classification
5. dense entity classification
6. shadow-map generation when enabled
7. terrain rendering
8. portal virtual views in play mode
9. opaque brush passes
10. instanced model rendering
11. editor path-node rendering where required
12. portal/editor overlays
13. transparent model/effect/sprite passes
14. water/glass/fog passes
15. selection and gizmo overlays

Exact ordering is constrained by depth, stencil, blending and resource dependencies.

The important architectural property is that these passes can consume the same dense projections without each pass rediscovering the world through Python object traversal.

This is `ForwardRenderer`'s frame. Another renderer is free to organise its frame differently; the host only calls `render_scene` and the post-scene operations of the `Renderer` contract.

### Display modes

The host publishes the editor's **Display** box as `brush_display_mode`. `ForwardRenderer` draws each value as follows:

| Display | ForwardRenderer |
|---|---|
| `Textured` | everything as normal |
| `Solid Lit` | brushes shaded with their colour and lit, **no textures** (before 3.0, Solid Lit drew exactly the same frame as Textured) |
| `Wireframe` | true brush edges — 12 per box, the real convex edges for angled brushes, never the triangles a filled mesh is cut into; models and terrain as line meshes — on black, coloured by distance |
| `Points` | a Scanner Sombre-style laser-scan point cloud on black: world-anchored, jittered points on every surface, coloured by distance |
| `Overlay` | the `Textured` lit frame with every brush triangle drawn over it as white lines (the Quake `r_showtris` look) |

### Distance-colour pass

`Wireframe` and `Points` share one technique (`engine/renderer/forward/distance.py`):

```
opaque geometry (brushes incl. water/glass, models, terrain)
        ↓
drawn for depth only, on black
   (as lines for Wireframe, as surfaces for Points)
        ↓
one full-screen pass
        ↓
world position rebuilt from depth
        ↓
coloured by distance from the eye
```

The colour ramp runs red/orange near → yellow → green → cyan → blue far. In `Points`, the pass lights only pixels that fall on a scan point; points sit on a jittered grid laid across each surface in world space, so they stay put as the camera moves, are hidden by whatever is in front of them and shrink with distance. If the distance program is unavailable, brushes fall back to their edges in their own colours.

Before 3.0 the instanced lit brush path forced `GL_FILL`, so Wireframe silently drew filled brushes; the edge pass fixes this.

---

# 26. Shader System

Fio targets:

```
OpenGL 3.3 Core
```

rather than fixed-function OpenGL.

Shared shader infrastructure includes:

- program caching
- uniform-location caching
- shared light UBO handling
- environment/fog state
- texture resources
- instanced shader variants
- low-power hardware variants

ARM-oriented variants can reduce shader-side light capacity and other costs without requiring a separate renderer architecture.

The representation boundary remains:

```
world
  → dense execution state
  → shader inputs
  → GPU
```

---

# 27. Distance, Fog and BigWorld

The renderer's view distance is a camera/rendering setting.

It is not itself a simulation residency limit.

The current default is 4096 world units and the projection far plane follows the active setting.

Distance fog can resolve a useful visual horizon before the far plane.

BigWorld can consume that visual horizon to decide how far world content needs to remain visually resident, but BigWorld's simulation tiers remain a separate system.

This distinction is important:

```
camera view distance
       ≠
render frustum
       ≠
BigWorld simulation residency
```

They interact, but they are not the same limit.

---

# 28. LOD

Earlier releases carried an `LODManager` (full/reduced/culled thresholds) on `BaseRenderer`. It never drove the forward frame pipeline, and it is gone in Fio 3.0 along with `BaseRenderer`.

`ForwardRenderer` relies on:

- camera distance culling
- frustum culling
- dense classification
- pass-specific filtering
- GPU depth and face culling

There is no active LOD stage for brushes. (Terrain has its own per-chunk LOD grids.)

---

# 29. Split-Screen and Multiple Views

Multiple camera views consume the same authoritative world and dense projections.

```
                    ┌── Camera A
                    │
World → dense data ─┼── Camera B
                    │
                    └── Camera C
```

Each view maintains its own:

- camera transform
- projection
- viewport
- view-dependent visibility
- depth/stencil context
- portal state

The world and persistent dense projections do not need to be duplicated merely because another camera exists.

---

# 30. Resource Lifetime

Fio separates three broad lifetimes:

```
authoritative world state
          ≠
persistent GPU resources
          ≠
transient per-frame numerical state
```

Persistent resources include:

- shader programs
- textures
- VAOs/VBOs
- model resources
- geometry caches
- shadow maps/resources
- terrain resources
- scene-transmission resources

Transient execution data includes:

- visible slots
- masks
- sorted indices
- render keys
- run boundaries
- instance payloads
- reusable scratch arrays

The RenderTable and EntityTable themselves are persistent execution projections. Their arrays survive frame publication and are refreshed according to their change/update discipline.

### GL resource ownership

Because renderers can be replaced live, GL resource lifetime is part of the `Renderer` contract. The invariant is:

> **No GL resource ID survives the lifetime of the renderer that owns it.**

The rules:

- **The renderer owns its GL resources:** its programs, buffers, VAOs, FBOs, the textures it loads (including through `load_texture`) and the models it loads. `cleanup()` deletes all of them; the renderer is not used afterwards. `cleanup()` is also called on a renderer whose `ready` is `False`, so it must cope with a partly initialised instance.
- **The host holds no renderer-produced handle past `cleanup()`.** Before the old renderer is cleaned up, the host discards what it was given: the sprite texture table, and the terrain's renderer bindings (program and ground textures, dropped by `Terrain.release_renderer_resources()`; the next renderer binds its own the first time it draws the terrain).
- **A replacement renderer starts with fresh resource tables.** The host reloads its sprites through the new renderer's `load_texture` and hands them over with `set_sprite_textures`; level textures are reloaded the same way.

Textures cross the contract as GL texture names in the host's context, so a texture name from a retired renderer is a dangling handle, not a shared asset.

The invariant is enforced by `tests/e2e/test_renderer_gl_lifetime.py`. It installs a **ledger** that records every `glGen*` / `glCreate*` made on a renderer's behalf, reading the owner from the call stack (the innermost caller that is a renderer, the terrain or the view). It draws frames in every display mode, with a selection and in Play through linked portals, swaps the renderer out, and asserts that none of the renderer's names is still a live GL object and that the host holds none of them. A renderer that fails to start must leave no GL names behind either.

This test found and fixed several leaks in the built-in renderer: gizmo VAOs, the AABB VAO/VBO, the component-overlay VAO/VBO, the brush/sprite/effect instance VBOs and VAOs, the portal mask and rim programs, and an instanced program compiled twice under one name.

---

# 31. Debugging and Observability

The [Debug Tables](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables) tool is part of the current renderer architecture, not just an external diagnostic.

It can expose:

- RenderTable row counts
- EntityTable row counts
- rows read during preparation
- simulation tick timing
- monster AI timing
- render preparation timing
- paint timing
- per-pass CPU timing
- published-frame rates
- declined swaps
- dense row/classification information
- the active renderer's registry name (`FORWARD`, `DEFERRED`, …)
- scene draw calls and shadow-map draws
- renderer-specific diagnostic lines

Debug Tables reads only the published dense tables and the `Renderer` contract (`render_stats` and the registry name), so it works unchanged with any renderer.

`RenderStats` is the renderer's half of this. A renderer resets and fills it each frame:

| Field | Meaning |
|---|---|
| `draw_calls` | scene draws (excludes shadow maps and editor overlays such as the grid, selection outline and gizmo) |
| `shadow_draw_calls` | shadow-map draws, counted at every shadow draw site |
| `batched_draws`, `visible_tris`, `total_brushes`, `culled_brushes`, `visible_brushes`, `entity_candidates`, `culled_entities` | diagnostics a renderer fills where they apply |
| `pass_ms` | CPU milliseconds per pass, filled by the `timed_pass` decorator |
| `details` | renderer-published diagnostic lines: `name → text`, or a zero-argument callable evaluated only when a tool shows it; not cleared by `reset()` |

`ForwardRenderer` publishes a `sprite texture array` detail line; the deferred example publishes `G-buffer`.

For the built-in renderer, `draw_calls + shadow_draw_calls` equals the GL draws issued inside `render_scene` minus editor overlays. `tests/e2e/test_debug_tables_oracle.py` checks Debug Tables against an independent measurement that wraps `OpenGL.GL` and counts the draws actually issued, across Forward → Flat → Forward, and with a renderer it has never heard of (a test stand-in registered as "Deferred").

This makes the data-oriented architecture measurable.

A large map can contain tens of thousands of rows while having a small preparation workload when the dense projections are unchanged.

The useful performance question is therefore not only:

> How many objects exist?

but also:

> How many rows had to be resolved or reread this frame?

---

# 32. Performance Design

Fio's renderer is designed around one central observation:

> **Don't make the CPU repeatedly do work that can be expressed as bulk data transformation.**

The important mechanisms are:

- persistent dense RenderTable
- persistent dense EntityTable
- change-driven row reconciliation
- numerical classification masks
- interned resource IDs
- numerical distance culling
- numerical frustum culling
- packed render keys
- stable sorting
- contiguous render runs
- GPU instancing
- packed instance payloads
- reusable NumPy scratch buffers
- reusable GPU buffers
- shared per-frame light UBO
- cached shader/uniform state
- cached geometry/model resources
- bounded portal recursion
- opaque back-face culling
- ARM-oriented shader variants

NumPy is not being used merely as a faster spelling of a Python loop.

The dense arrays are the execution representation of the hot path.

The intended flow is:

```
dense world projection
       ↓
dense classification
       ↓
dense visibility
       ↓
dense sorting
       ↓
dense run generation
       ↓
dense instance packing
       ↓
GPU
```

Small Python loops remain where they are naturally appropriate, especially around:

- GPU run submission
- GL state transitions
- small bounded light/portal sets
- irregular control flow that is genuinely cheaper to express procedurally

The goal is not "no Python loops".

The goal is:

> **No unnecessary Python loop whose cost scales with world population.**

---

# 33. What the Renderer Is Not

The current renderer should not be described as:

- a renderer that walks every Thing every frame
- a renderer that classifies every brush by Python dictionary/string inspection every frame
- a renderer with one Python draw operation per entity
- a renderer that rebuilds dense tables every frame
- a renderer that requires authored maps to be compiled into a separate runtime representation
- a renderer that replaces the authoritative world with a giant global array
- a renderer with a separate ARM execution architecture

Instead:

```
Authoring model
    = flexible and object-oriented

Execution model
    = persistent, dense and data-oriented

GPU interface
    = packed numerical state
```

---

# 34. Source Files

| File | Role |
|---|---|
| `engine/renderer/__init__.py` | the public renderer package: everything outside code may import; loads no OpenGL |
| `engine/renderer/api.py` | the `Renderer` protocol, `FRAME_INPUT`, `RenderStats`, `SelectionOverlay` / `EffectBillboard`, the GL-state rule |
| `engine/renderer/registry.py` | renderer registry: name → factory (`"Forward"` built in) |
| `engine/renderer/core/base.py` | `RendererCore`: optional reusable infrastructure, and `cleanup()` of what it owns |
| `engine/renderer/core/resources.py` | texture cache, sprite table, OBJ/GLB models, effect frames, shader loading |
| `engine/renderer/core/geometry.py` | cube and sprite primitives, angled-brush mesh cache |
| `engine/renderer/core/tables.py` | RenderTable/EntityTable lookups: matrices, texture ids, sprite recipes |
| `engine/renderer/core/visibility.py` | culling, pass classification, light/shadow-caster selection, portal camera maths |
| `engine/renderer/core/overlays.py` | editor and debug overlays: grid, gizmo, selection, portal wireframes, connection lines, … |
| `engine/renderer/core/diagnostics.py` | `timed_pass` pass timing |
| `engine/renderer/forward/renderer.py` | `ForwardRenderer`: `render_scene` frame/pass orchestration and `cleanup()` |
| `engine/renderer/forward/passes.py` | opaque/textured/glow brushes, edges and triangle overlay, terrain, models, sprites, effects, water/glass/fog |
| `engine/renderer/forward/lighting.py` | light UBO, fog/ambient upload, point-light cube-map shadows |
| `engine/renderer/forward/portals.py` | stencil portals: aperture masks, recursion, portal virtual scene |
| `engine/renderer/forward/instancing.py` | instance VBO/VAO layouts and packing from table columns |
| `engine/renderer/forward/shaders.py` | forward shader set, instanced and low-power variants, sprite texture array |
| `engine/renderer/forward/distance.py` | Wireframe/Points distance-colour pass |
| `engine/render_table.py` | dense brush/render projection |
| `engine/entity_table.py` | dense entity/light/portal/effect/model projection |
| `engine/change_journal.py` | runtime change notification for dense projections |
| `engine/render_cull.py` | distance-culling and visibility helpers |
| `engine/render_keys.py` | packed numerical draw keys, stable sorting and run detection |
| `engine/view_distance.py` | live camera view-distance and fog-distance settings |
| `engine/threaded_game_state.py` | double-buffered render-state publication and ownership |
| `engine/qt_game_view.py` | Qt/OpenGL viewport and frame orchestration |
| `engine/mover_table.py` | dense mover/door runtime state used by simulation and render publication |
| `engine/shaders.py` | shader sources, compilation and shared light-capacity definitions |
| `tools/debug_tables.py` | live dense-table/render instrumentation |

---

# Design Summary

Fio's current renderer is a **data transformation pipeline between world authority and the GPU**.

```
                    AUTHORING
                        │
                        ▼
              authoritative world
                        │
              Python objects/data
                        │
            ┌───────────┴───────────┐
            ▼                       ▼
       RenderTable             EntityTable
            │                       │
            └───────────┬───────────┘
                        ▼
              dense execution state
                        │
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
      visibility   classification   runtime state
          │             │             │
          └─────────────┼─────────────┘
                        ▼
                    render keys
                        ▼
                 stable sorting
                        ▼
                 equal-key runs
                        ▼
              packed instance data
                        ▼
                 OpenGL 3.3 Core
                        ▼
                       GPU
```

The fundamental principle is:

> **The authoring model is object-oriented. The execution model is data-oriented.**

Fio does not require the entire engine to become one monolithic array structure.

Instead, it projects the information needed by each execution domain into dense representations and keeps that information numerical through the expensive part of the frame.

The current renderer therefore is not merely an OpenGL layer placed on top of the world model.

It is:

```
world
  → project
  → journaled update
  → classify
  → cull
  → sort
  → batch
  → pack
  → GPU
```

The GPU is the final stage of that pipeline, not the place where the renderer's fundamental decisions begin.
