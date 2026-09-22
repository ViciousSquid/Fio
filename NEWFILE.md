## Renderer Technical Overview

Fio's renderer is a forward OpenGL 3.3 Core renderer designed for low-power hardware, with the Snapdragon 8cx Gen 3 / Adreno class of hardware as an important baseline.

The renderer deliberately separates the flexible, object-oriented world representation from the dense numerical representation used by the rendering hot path.

### Architecture

```
QtGameView
    │
    ▼
Renderer_F
    │
    ▼
Numerical Render Projection
    │
    ├── numerical visibility / classification
    ├── numerical render keys
    ├── stable sorting
    └── equal-key render runs
    │
    ▼
BaseRenderer / OpenGL backend
    │
    ▼
OpenGL 3.3 Core
```

The important boundary is **before GPU submission**:

```
authoritative world
    ↓
Python objects / dictionaries
    ↓
dense numerical render state
    ↓
culling / classification
    ↓
material and draw-key sorting
    ↓
render runs
    ↓
packed instance payloads
    ↓
OpenGL
```

The renderer therefore does not require the entire game world to become a NumPy array. Instead, it projects the authoritative world into a dense numerical representation before entering the renderer's hot path.

---

## Renderer Layers

### World representation

The authoritative world remains the normal Fio scene/entity representation.

This is intentionally flexible. Entities can contain gameplay state, editor metadata, component data and other information that does not belong in the renderer's numerical representation.

The render projection is derived from this state; it is not a second source of truth.

### Numerical render projection

`engine/render_table.py` contains the dense render projection.

The projection stores render-relevant state in parallel NumPy arrays rather than requiring the renderer to repeatedly walk Python objects during visibility, classification and batching.

The principal columns are:

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

The exact projection can evolve independently of the world model.

### OpenGL backend

The OpenGL layer receives already-classified, sorted and packed data.

Its job is therefore primarily GPU-facing work:

- shader management
- texture and material binding
- VAO/VBO management
- instance-buffer uploads
- draw submission
- lighting
- shadows
- portals
- terrain
- models and sprites
- transparent/special materials
- editor overlays
- render statistics

---

## QtGameView

`engine/qt_game_view.py` owns the Qt/OpenGL viewport and frame orchestration.

It is responsible for the GL context, viewport state and invoking the selected renderer.

It does not contain the renderer's core batching algorithm.

Split-screen rendering invokes the renderer once per camera while reusing the same numerical world/render representation.

---

## Renderer_F

`Renderer_F` is Fio's production forward renderer.

It implements the actual frame passes while relying on `BaseRenderer` for shared infrastructure.

The main brush paths include:

- `draw_lit_brushes_optimized()`
- `draw_textured_brushes_optimized()`
- `draw_glow_brushes()`

The renderer is registered through the renderer registry:

```python
_RENDERER_CLASSES = {
    "Forward": Renderer_F,
}
```

Additional renderer implementations can be registered without changing the world representation.

---

## BaseRenderer

`BaseRenderer` contains shared rendering infrastructure rather than acting as a strict abstract base class.

The shared responsibilities include:

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
- editor helpers
- sorting and LOD support
- render statistics
- resource cleanup

The base implementation is deliberately practical rather than enforcing an artificial renderer interface.

`render_scene()` raises `NotImplementedError` when a concrete renderer must provide the frame implementation.

---

## Numerical Render Culling

Visibility testing is performed against the dense numerical representation.

`engine/render_cull.py` operates on contiguous position data and performs bulk distance/frustum arithmetic using NumPy.

The result is a dense set of visible/classified indices rather than a Python object list that must be repeatedly traversed by later renderer stages.

Reusable numerical buffers are retained where possible to avoid per-frame allocation.

The important architectural point is that culling happens **after the representation boundary has moved into numerical data**.

---

## Render Keys and Runs

`engine/render_keys.py` contains the numerical draw-key machinery.

`KeyLayout` describes the fields that make up a pass's key and packs dense columns into sortable `int64` values.

Fields are packed most-significant-first because that is the required sort order.

The renderer then performs a stable sort and identifies the boundaries between equal-key stretches with vectorized operations.

Conceptually:

```
dense render items
       ↓
key columns
       ↓
packed int64 key
       ↓
stable sort
       ↓
equal-key runs
       ↓
one draw submission per run
```

The governing rule is:

> **Everything that cannot vary within one draw belongs in the render key. Everything that can vary within the draw travels as instance data.**

This is the central batching algebra of the renderer.

---

## Instanced Brush Rendering

Textured brush faces are represented through parallel numerical arrays.

A typical draw key can contain values such as:

```
(texture, face/material class)
```

while data that can vary between instances remains in the instance payload:

- transform
- normal matrix information
- UV scale
- UV rotation
- UV shift
- colour
- other per-instance parameters

The renderer therefore does not need one Python-level draw operation per object simply because object transforms or UV parameters differ.

Objects with the same immutable draw requirements are grouped into a render run and submitted together.

This is the same fundamental idea as a conventional material/state sort, but the grouping decision is made from dense numerical data rather than Python object traversal.

---

## Frame Rendering Order

The broad frame structure is:

1. Clear framebuffer.
2. Draw editor grid where required.
3. Build/use numerical visibility and classification results.
4. Render terrain.
5. Process portals.
6. Render opaque geometry.
   - textured brushes
   - lit brushes
7. Render glow materials.
8. Render models.
9. Render shadow resources/passes where required.
10. Render transparent and special materials.
    - triggers
    - sprites
    - water
    - glass
    - fog
11. Render editor overlays.
    - path nodes
    - portal wireframes
    - selection outline
    - gizmo

Exact pass ordering can vary where a pass requires a specific depth, stencil or blending state.

---

## Shader System

Shaders are managed through `ShaderLoader`, with shader sources provided through the engine shader system and runtime shader assets.

The renderer uses:

- cached shader programs
- cached uniform locations
- per-frame light-upload caching
- explicit shader variants for ARM/Adreno hardware where required

The target is OpenGL 3.3 Core rather than OpenGL fixed-function rendering.

---

## Lighting

Fio uses forward lighting rather than a G-buffer/deferred pipeline.

The shared renderer configuration currently supports:

```
MAX_LIGHTS = 32
```

Light data is uploaded through the shared lighting path, with caching used to avoid unnecessary repeated uniform work.

This keeps the renderer comparatively simple and is appropriate for the low-power hardware target.

---

## Portals

Portal rendering uses the stencil buffer.

Current limits are:

```
MAX_PORTALS = 4
MAX_PORTAL_RECURSION = 1
```

Portal rendering is therefore bounded rather than allowing unbounded recursive scene traversal.

---

## Shadows

Point-light shadowing uses depth cube maps.

Current defaults include:

```
MAX_SHADOW_LIGHTS = 8
SHADOW_MAP_SIZE = 384
```

Shadow resources are cached and reused.

Projected floor shadows are a separate path and are disabled by default on ARM hardware.

---

## Terrain

Terrain has a dedicated rendering path and shader/buffer resources rather than being forced through the ordinary brush batching path.

This keeps terrain-specific representation and GPU state separate from the general brush renderer.

---

## Models and Sprites

The renderer supports model and sprite paths including OBJ and GLB assets.

Sprites use camera-facing geometry where appropriate and can carry per-instance texture state.

These paths remain distinct from brush instancing because their geometry and material requirements differ.

---

## Special Materials

The renderer contains dedicated handling for:

- water
- glass
- volumetric fog
- trigger/editor geometry
- other transparent or state-sensitive materials

These cannot necessarily be reduced to the same opaque brush batching rule because blending, depth and shader state can vary.

---

## LOD and Distance Handling

The renderer uses explicit LOD/culling classifications:

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

The exact values are configuration rather than architectural constants.

Distance and visibility decisions are made numerically where possible so that large populations do not require Python scalar loops.

---

## ARM / Adreno Rendering

Fio targets low-power ARM-class hardware as a first-class constraint.

The renderer includes ARM-oriented shader variants and configuration.

The ARM path uses measures such as:

- simplified shader variants
- reduced fog complexity
- explicit texture filtering
- cached uniforms
- reduced shadow cost
- projected shadows disabled by default
- `arm_mode` hardware selection

The goal is not a separate renderer architecture for ARM. The same numerical render representation feeds the same renderer with lower-cost GPU paths selected where appropriate.

---

## Split-Screen

Multiple cameras can render the same world representation.

`QtGameView` invokes the renderer for each camera, while the numerical render projection and world data remain shared.

This avoids duplicating the authoritative world simply to support multiple views.

---

## Renderer Extension

The renderer registry allows additional renderer implementations:

```python
register_renderer("Deferred", Renderer_D)
```

A future renderer can therefore consume the same world representation and establish its own GPU pass structure without requiring the entity/world system to change.

---

## Performance Design

The renderer's performance strategy is built around moving expensive repeated decisions out of Python object traversal and into dense numerical operations.

Key mechanisms include:

- dense NumPy render projections
- batched NumPy visibility/culling
- pre-resolved render flags
- dense texture identifiers
- numerical render keys
- vectorized run-boundary detection
- stable sorting
- instanced brush rendering
- packed instance payloads
- cached matrices and uniforms
- texture batching
- convex mesh caching
- per-frame light caching
- LOD
- distance culling
- cached shadow maps
- ARM shader variants
- preloaded textures
- reusable VAOs and buffers
- minimized OpenGL state changes

The important distinction is that NumPy is not being used merely as a faster replacement for individual Python operations.

It is the **execution representation** for the render hot path.

---

## Configuration

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

These values are implementation/configuration defaults, not limits imposed by the renderer architecture itself.

---

## Source Files

| File | Role |
|---|---|
| `engine/renderer_core.py` | Shared renderer infrastructure |
| `engine/renderer_F.py` | Forward renderer |
| `engine/render_table.py` | Dense numerical render projection |
| `engine/render_keys.py` | Numerical render-key packing and run generation |
| `engine/render_cull.py` | Batched numerical visibility/culling |
| `engine/qt_game_view.py` | Qt/OpenGL viewport and frame orchestration |
| `engine/shaders.py` | Embedded shader sources/system |
| `assets/shaders/` | Runtime shader sources |

---

## Design Summary

Fio's renderer now has a deliberate representation boundary:

```
Authoritative world
      │
      ▼
Python objects / dictionaries
      │
      ▼
Dense numerical render projection
      │
      ├── visibility
      ├── classification
      ├── material state
      └── geometry state
      │
      ▼
Numerical draw keys
      │
      ▼
Stable sort
      │
      ▼
Equal-key render runs
      │
      ▼
Packed instance payloads
      │
      ▼
OpenGL 3.3
      │
      ▼
GPU
```

The architectural principle is simple:

> **The world remains flexible and object-oriented, while the execution representation becomes dense, numerical and GPU-oriented before entering the rendering hot path.**

That boundary is what allows Fio's existing Python world model to coexist with aggressively vectorised rendering without requiring the entire engine to become a C/C++ engine or a monolithic array structure.
