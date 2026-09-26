# Renderer Technical Overview

Fio's renderer is a forward OpenGL 3.3 Core renderer designed for low-power hardware, with Snapdragon 8cx Gen 3 / Adreno-class hardware as an important development and validation target.

The renderer deliberately separates two concerns:

- **Authoring and world state:** flexible, object-oriented Python structures.
- **Execution state:** dense numerical projections designed for culling, classification, sorting, batching and GPU submission.

The renderer therefore does not treat the Python world objects themselves as its primary execution representation.

The important architectural boundary is:

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
              ├── visibility
              ├── classification
              ├── render keys
              ├── stable sorting
              └── contiguous runs
              │
              ▼
      packed GPU payloads
              │
              ▼
        OpenGL 3.3 Core
```

This is the defining characteristic of the current renderer.

---

# The Representation Boundary

Fio's renderer can be understood as a sequence of representation changes:

```
World authority
    ↓
Python objects / dictionaries
    ↓
Dense execution projection
    ↓
Visibility / classification
    ↓
Render keys
    ↓
Stable numerical sort
    ↓
Contiguous render runs
    ↓
Packed GPU instance data
    ↓
OpenGL submission
```

The important distinction is that the dense tables are **derived execution representations**.

They do not replace the authoritative world.

A `Thing`, brush, entity or other authored object remains the source of truth. The renderer consumes numerical projections of the information it actually needs.

This avoids forcing gameplay state, editor metadata, arbitrary properties and authoring structure into a renderer-specific memory layout.

---

# RenderTable

`engine/render_table.py` contains the dense render projection used for brush and geometry-oriented rendering.

The table is a numerical representation of render-relevant world state.

Typical columns include:

| Column | Purpose |
|---|---|
| `center[N,3]` | Object/world-space centre |
| `half[N,3]` | Half extents |
| `rot[N,4]` | Rotation quaternion |
| `class_bits[N]` | Render classification flags |
| `tex_name_id[N,6]` | Dense texture identifiers per face |
| `uv_scale[N,6,2]` | Per-face UV scale |
| `uv_angle[N,6]` | Per-face UV rotation |
| `uv_shift[N,6,2]` | Per-face UV offset |
| `uv_natural[N,6]` | Natural UV information |
| `uv_has_scale[N,6]` | UV-scale presence flags |
| `colour[N,3]` | Base colour |
| `glow_colour[N,3]` | Glow colour |
| `geo_epoch[N]` | Geometry/cache invalidation epoch |

The exact set of columns can evolve as the renderer evolves.

The architectural rule does not change:

> `RenderTable` is a derived execution representation, not a second source of truth.

---

# EntityTable

`engine/entity_table.py` provides the dense entity projection used by entity-oriented rendering and execution paths.

It is deliberately analogous to `RenderTable`.

Instead of repeatedly asking Python entity objects what they are during rendering, `EntityTable` stores the relevant facts in dense columns.

This includes:

- position
- entity classification
- render representation
- sprite identity
- model representation
- pickup/monster/prop/light/portal classification
- dynamic visibility state
- other render-relevant entity state

Classification is represented through compact bit fields.

Examples include:

```
ENT_SKIP
ENT_ALWAYS_SPRITE
ENT_HAS_MODEL
ENT_MODE_MODEL
ENT_MODE_BILLBOARD
ENT_HAS_SPRITE
ENT_PICKUP
ENT_ENTITY_SPRITE
ENT_PROP
ENT_LIGHT
ENT_MONSTER
ENT_PORTAL
ENT_SPRITE_WARM
ENT_CULL_EXEMPT
```

The important property is not the particular bit names.

The important property is that frame-time classification can be expressed as numerical mask operations rather than repeated Python `isinstance` checks and property lookups.

Some classification facts are stable and can be resolved when the table is reconciled.

Other values are intentionally refreshed every frame when runtime state can change.

This gives the renderer a dense execution representation without pretending that every piece of entity state is static.

---

# Why There Are Two Tables

`RenderTable` and `EntityTable` represent different execution domains.

They are not redundant copies of the world.

Conceptually:

```
Authoritative world
       │
       ├──────────────► RenderTable
       │                  geometry/render state
       │
       └──────────────► EntityTable
                          entity/render state
```

This separation allows each representation to contain exactly the columns required by its execution paths.

The authoring model therefore does not need to be redesigned merely because a renderer path benefits from a different numerical layout.

---

# QtGameView

`engine/qt_game_view.py` owns the Qt/OpenGL viewport and frame orchestration.

Its responsibilities include:

- OpenGL context ownership
- viewport state
- camera/view state
- projection state
- invoking the renderer
- coordinating editor and runtime views
- handling multiple views

It does not implement the renderer's core batching algorithm.

For split-screen rendering, multiple cameras can consume the same world and dense execution representations.

View-specific state remains separate:

- camera position
- view matrix
- projection matrix
- viewport
- depth/culling decisions
- view-specific rendering state

The underlying world and dense projections do not need to be duplicated merely because several cameras are active.

---

# Renderer_F

`Renderer_F` is Fio's production forward renderer.

It establishes the frame's rendering passes while using shared infrastructure supplied by `BaseRenderer`.

Representative brush paths include:

```
draw_lit_brushes_optimized()
draw_textured_brushes_optimized()
draw_glow_brushes()
```

The important distinction is that these paths consume the numerical render representation rather than treating the authored Python object graph as their primary batching structure.

The renderer is registered through the renderer registry:

```python
_RENDERER_CLASSES = {
    "Forward": Renderer_F,
}
```

Additional renderer implementations can therefore consume the same world/execution boundary while establishing different GPU pass structures.

---

# BaseRenderer

`BaseRenderer` contains shared rendering infrastructure rather than functioning as a purely abstract interface.

Shared infrastructure includes areas such as:

- shader management
- texture management
- VAOs and buffers
- terrain rendering
- model rendering
- sprite rendering
- water and glass
- fog
- portals
- lighting
- shadows
- editor rendering helpers
- sorting support
- LOD support
- render statistics
- resource cleanup

The base implementation is intentionally practical.

Fio does not impose an artificial renderer abstraction merely for architectural purity.

`render_scene()` remains the concrete renderer's frame-level implementation boundary.

---

# Numerical Visibility and Culling

`engine/render_cull.py` operates on dense numerical position data.

Distance and frustum calculations can therefore be performed using bulk NumPy operations instead of repeatedly traversing Python objects.

The output is a numerical set of visible/classified indices that can be consumed directly by later execution stages.

Reusable scratch storage is retained where practical to avoid turning vectorisation into repeated allocation.

The architectural rule is:

> Once data has crossed into the dense execution representation, repeated frame-time classification should remain numerical wherever practical.

This does not mean every operation must be forced through NumPy.

Small control-flow loops are still appropriate where they operate on inherently small sets, such as render runs or a bounded number of lights/portals.

---

# Render Keys

`engine/render_keys.py` contains the renderer's numerical draw-key machinery.

`KeyLayout` defines the fields participating in a particular batching key and packs them into sortable integer representations.

The renderer can therefore turn multiple categorical render decisions into one sortable numerical key.

Conceptually:

```
dense render state
       ↓
key fields
       ↓
packed int64 key
       ↓
stable numerical sort
       ↓
equal-key runs
```

The governing rule is:

> **Everything that cannot vary within one draw belongs in the render key. Everything that can vary within the draw travels as instance data.**

This is the renderer's batching algebra.

It replaces a large amount of object-level “is this compatible with the previous object?” logic with a numerical operation:

```
classify → encode → sort → run
```

---

# Equal-Key Runs

After sorting, equal keys form contiguous runs.

A run represents a group of render items that can share the state encoded by that key.

Instead of:

```
object
  ↓
choose state
  ↓
draw
  ↓
object
  ↓
choose state
  ↓
draw
```

the renderer works toward:

```
dense items
    ↓
sort
    ↓
run A ────────────► one submission
run B ────────────► one submission
run C ────────────► one submission
```

The Python control flow that remains is therefore concentrated around **runs and GPU submissions**, rather than around every authored object.

The renderer is not attempting to eliminate Python completely.

It is eliminating unnecessary Python work at the scale where the number of world objects would otherwise dominate frame time.

---

# Brush Rendering

Brush geometry uses dense numerical records and batching information.

Render keys encode state that must be shared by a draw.

Per-instance information can remain in the instance payload.

Depending on the pass, this can include:

- transform
- orientation
- UV parameters
- colour
- material parameters
- other per-instance state

The result is that objects with identical draw requirements can be submitted together without requiring a separate Python-level draw operation for every object.

The representation boundary therefore looks like:

```
world brushes
     ↓
RenderTable
     ↓
visibility/classification
     ↓
render key
     ↓
sorted runs
     ↓
instance payload
     ↓
GPU draw
```

---

# Sprite Rendering

Sprite rendering is also a dense execution path.

The current sprite boundary is:

```
EntityTable
     ↓
sprite slots
     ↓
texture IDs
     ↓
numerical sorting
     ↓
texture runs
     ↓
instance buffer
     ↓
glDrawArraysInstanced
```

The sprite pass operates on dense EntityTable columns.

For each sprite row it reads numerical data such as:

- position
- sprite size
- sprite texture identity

It does not walk the corresponding Python entity objects.

Texture identity is used as the primary grouping key.

When camera depth is relevant, depth is used as the secondary numerical sort key so sprites within a texture run can maintain the required ordering.

The resulting run is submitted using instancing:

```
one texture run
      ↓
one packed instance range
      ↓
glDrawArraysInstanced(...)
```

The renderer also uses reusable scratch arrays and reusable instance storage.

The implementation deliberately avoids creating temporary object-oriented collections or repeated `(N,3)` / `(N,2)` staging arrays merely to reach the GPU.

This is an important detail of the current data-oriented boundary:

> The numerical representation is retained through the staging step instead of repeatedly converting objects into arrays and then immediately discarding those arrays.

---

# Models

Model rendering is a separate execution path because model geometry and resource requirements differ from brush and sprite rendering.

The renderer supports model assets including OBJ and GLB representations.

Model state can be represented in the dense entity projection while the actual model resources remain cached GPU/CPU resources.

The renderer therefore separates:

```
entity/model identity
        ↓
dense execution state
        ↓
resource lookup
        ↓
GPU resources
```

rather than storing heavyweight model objects inside the per-frame execution representation.

---

# Frame Rendering

The renderer is organised as a collection of passes rather than one universal draw path.

Broadly, a frame contains:

1. framebuffer and view setup
2. editor/grid rendering where required
3. numerical visibility/classification work
4. terrain rendering
5. portal processing
6. opaque geometry
7. glow/special opaque materials
8. model rendering
9. shadow resources/passes where required
10. transparent and state-sensitive materials
11. editor overlays

Representative special passes include:

- sprites
- water
- glass
- fog
- triggers
- editor geometry
- portal overlays
- selection/gizmo rendering

Exact ordering is determined by the depth, stencil, blending and resource requirements of each pass.

The important architectural property is that the individual passes can consume the same dense world projections without requiring each pass to rediscover the world through Python object traversal.

---

# Shader System

Shaders are managed through Fio's shader system and cached for reuse.

The renderer uses:

- cached shader programs
- cached uniform locations
- cached per-frame lighting state
- explicit shader variants where required by the hardware target

The renderer targets:

```
OpenGL 3.3 Core
```

rather than fixed-function OpenGL.

The low-power target is therefore treated as a constraint on the actual GPU execution path rather than as justification for introducing a second renderer architecture.

---

# Forward Lighting

Fio uses forward lighting rather than a G-buffer/deferred architecture.

The shared renderer configuration supports:

```
MAX_LIGHTS = 32
```

Light data uses the renderer's shared lighting path, with caching used to avoid unnecessary repeated uniform work.

Forward rendering keeps the GPU pipeline comparatively compact and avoids introducing a deferred renderer solely to accommodate a much larger lighting model than the target hardware requires.

---

# Portals

Portal rendering uses the stencil buffer.

The current bounded configuration includes:

```
MAX_PORTALS = 4
MAX_PORTAL_RECURSION = 1
```

Portal recursion is therefore explicitly bounded.

The renderer does not allow arbitrary recursive scene traversal to determine frame cost.

Portal rendering also remains view-specific, which is important for split-screen and multiple-camera operation.

---

# Shadows

Point-light shadowing uses depth cube maps.

Current defaults include:

```
MAX_SHADOW_LIGHTS = 8
SHADOW_MAP_SIZE = 384
```

Shadow resources are cached and reused.

Projected floor shadows are a separate path and can be disabled for ARM-oriented configurations.

The renderer therefore treats shadow resources as persistent GPU resources rather than recreating the underlying allocation structure every frame.

---

# Terrain

Terrain has its own rendering path and resource representation.

It is not forced through ordinary brush batching simply for architectural uniformity.

This is consistent with Fio's broader execution model:

> Dense data is the common execution strategy; it does not require every subsystem to share one universal data structure.

Terrain can therefore maintain terrain-specific GPU resources and shader state while still participating in the same overall renderer/frame architecture.

---

# Special Materials

Fio contains dedicated paths for materials and effects whose state requirements differ from ordinary opaque geometry.

These include:

- water
- glass
- fog
- triggers
- editor geometry
- other transparent or state-sensitive materials

These paths cannot necessarily be reduced to the ordinary opaque brush batching rule because blending, depth, stencil or shader state can impose different ordering requirements.

The dense execution model still applies where useful, but the renderer does not force incompatible passes into a single abstraction.

---

# LOD and Distance Handling

Distance and visibility decisions are represented numerically where practical.

Representative classifications include:

```
LOD_FULL
LOD_REDUCED
LOD_CULLED
```

Representative defaults are approximately:

```
Full: 500
Cull: 2000
```

These values are configuration rather than architectural constants.

The important property is that large populations can be classified using dense numerical operations rather than requiring one Python decision chain per object.

---

# ARM / Adreno Rendering

Fio treats low-power ARM-class hardware as a first-class constraint.

The renderer does not maintain a separate ARM renderer architecture.

Instead, the same dense execution representation feeds the same renderer while lower-cost GPU configurations and shader paths can be selected where appropriate.

ARM-oriented measures include:

- simplified shader variants
- reduced fog complexity
- explicit texture filtering
- cached uniforms
- reduced shadow cost
- projected shadows disabled by default
- hardware-oriented `arm_mode` selection

The objective is to keep the representation and execution architecture unified while allowing GPU cost to scale down for constrained hardware.

---

# Split-Screen and Multiple Views

Fio supports multiple cameras rendering the same world.

This is an important consequence of the representation boundary.

The world does not need to be duplicated for every camera:

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
- depth/culling context
- view-dependent state

while sharing the underlying authoritative world and dense execution representations.

This is especially important for hardening because multiple views must not accidentally mutate shared render state or leak camera-specific state between submissions.

---

# Renderer Extension

The renderer registry allows additional renderer implementations to be registered.

For example:

```python
register_renderer("Deferred", Renderer_D)
```

A future renderer can therefore consume the same world representation and establish a different GPU pass structure.

The important boundary is that renderer implementations should not need to redefine the authoritative world model merely because their GPU execution strategy differs.

The dense projections form the useful interface between world state and rendering execution.

---

# Resource Lifetime

GPU resources are deliberately treated separately from transient numerical frame data.

Long-lived resources include:

- shader programs
- textures
- VAOs
- VBOs
- geometry caches
- shadow resources
- terrain resources
- model resources

Transient execution data includes:

- visible indices
- sorted indices
- render keys
- run boundaries
- instance payloads
- temporary numerical masks

Reusable scratch buffers are preferred where their lifetime and capacity make reuse beneficial.

The renderer therefore distinguishes:

```
persistent GPU resources
        ≠
per-frame execution data
        ≠
authoritative world state
```

Keeping these lifetimes separate is important both for performance and for renderer hardening.

---

# Performance Design

Fio's renderer is designed around one central observation:

> **Don't make the CPU repeatedly do work that can be expressed as bulk data transformation.**

The important mechanisms are therefore:

- dense `RenderTable`
- dense `EntityTable`
- numerical visibility/culling
- pre-resolved classification bits
- dense resource identifiers
- packed numerical render keys
- stable sorting
- vectorised run detection
- contiguous render runs
- GPU instancing
- packed instance payloads
- reusable NumPy scratch buffers
- reusable GPU buffers
- cached matrices and uniforms
- texture/material batching
- cached geometry
- cached shadow resources
- LOD and distance culling
- ARM-oriented shader paths
- minimized OpenGL state changes

NumPy is not being used merely as a faster spelling of a Python loop.

The dense arrays are the **execution representation** of the render hot path.

That distinction matters.

A design such as:

```
Python objects
    ↓
create temporary arrays
    ↓
do one vector operation
    ↓
convert back to objects
```

would not provide the same architectural benefit.

Fio instead attempts to keep data numerical across the complete execution path:

```
dense world projection
       ↓
dense classification
       ↓
dense sorting
       ↓
dense run generation
       ↓
dense instance packing
       ↓
GPU
```

Small Python loops remain where they are naturally appropriate, particularly around bounded render runs, GPU state changes and other inherently small control-flow sets.

The goal is therefore not “no Python loops.”

The goal is:

> **No unnecessary Python loop over the size of the world population.**

---

# What the Renderer Is Not

The current renderer should not be described as:

- a renderer that walks every `Thing` every frame
- a renderer that performs material compatibility checks object by object
- a renderer with one Python draw operation per entity
- a giant global NumPy representation of the entire game world
- a renderer that requires the authoring model to become data-oriented
- a compiled render pipeline that requires maps to be converted into a separate executable representation

Instead:

```
Authoring model
    = flexible and object-oriented

Execution model
    = dense and data-oriented

GPU interface
    = packed numerical state
```

This separation is deliberate.

---

# Configuration

Representative renderer defaults include:

| Setting | Current value |
|---|---:|
| `MAX_LIGHTS` | 32 |
| `MAX_PORTALS` | 4 |
| `MAX_PORTAL_RECURSION` | 1 |
| `MAX_SHADOW_LIGHTS` | 8 |
| `SHADOW_MAP_SIZE` | 384 |
| `fog_quality` | low |
| `arm_mode` | Auto |
| `shadows_enabled` | Disabled on ARM by default |
| `vsync` | Enabled |

These are implementation/configuration defaults.

They are not fundamental limits of Fio's world model or dense execution architecture.

---

# Source Files

| File | Role |
|---|---|
| `engine/renderer_core.py` | Shared renderer infrastructure and GPU-facing facilities |
| `engine/renderer_F.py` | Production forward renderer |
| `engine/render_table.py` | Dense numerical render projection |
| `engine/entity_table.py` | Dense numerical entity projection |
| `engine/render_keys.py` | Numerical render-key packing, sorting and run generation |
| `engine/render_cull.py` | Batched numerical visibility/culling |
| `engine/qt_game_view.py` | Qt/OpenGL viewport and frame orchestration |
| `engine/shaders.py` | Shader system / shader sources |
| `assets/shaders/` | Runtime shader assets |

---

# Design Summary

Fio's renderer is built around a deliberate boundary between **world authority** and **execution representation**.

```
                    AUTHORING
                        │
                        ▼
              Authoritative world
                        │
              Python objects/data
                        │
            ┌───────────┴───────────┐
            ▼                       ▼
       RenderTable             EntityTable
            │                       │
            └───────────┬───────────┘
                        ▼
              Dense execution state
                        │
          ┌─────────────┼─────────────┐
          ▼             ▼             ▼
      visibility   classification   resources
          │             │
          └─────────────┘
                        ▼
                   render keys
                        ▼
                  stable sort
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

Instead, it projects the information needed for a particular execution domain into dense representations and keeps that representation numerical through the expensive part of the frame.

That is what allows Fio's flexible Python world model to coexist with aggressively vectorised rendering on low-power hardware.

The renderer is therefore not merely an OpenGL layer placed on top of the world model.

It is a **data transformation pipeline**:

```
world
  → projection
  → classification
  → sorting
  → batching
  → packing
  → GPU
```

The GPU is the final stage of that pipeline, not the place where the renderer's fundamental decisions begin.
