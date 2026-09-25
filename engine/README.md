# `engine/`

The runtime engine: world simulation, physics, resource/package loading, numerical render projection, OpenGL rendering, shaders, terrain, models, sprites and the Qt game viewport.

The engine's authoritative gameplay/world state remains object-oriented. Performance-sensitive execution paths increasingly project that state into dense NumPy representations before entering hot loops. Rendering and physics both use this pattern: Python objects provide the API and world model; contiguous numerical arrays provide the execution representation.

---

### `audio_manager.py`
Sound effect loading and playback via pygame. Routes through `ResourceManager` for `.fiopak` package compatibility and caches loaded sounds.

### `brush_geometry.py`
Convex brush geometry based on intersections of half-space planes. Computes surface windings, collision meshes, 2D silhouettes and bounds. The plane set is the source of truth; derived geometry is cached using a geometry signature and invalidation epoch. Also provides component-edit geometry operations including convex-hull reconstruction and plane/point dragging primitives. Uses NumPy without depending on Qt or the editor.

### `camera.py`
Camera state and view/projection matrix computation for first-person and other 3D views.

### `constants.py`
Shared engine constants covering window defaults, dimensions, render modes, physics tuning and water physics.

### `floating_windows.py`
Reusable Qt floating-window infrastructure used by `QtGameView` overlays such as SysMon. Provides draggable/collapsible window chrome, stacking and event routing without importing the editor or game systems.

### `glb_loader.py`
GLB/glTF 2.0 binary model loader. Extracts mesh geometry, normals, UVs, indices, PBR materials, embedded textures and node hierarchies and exposes an OpenGL-ready model interface.

### `logic_thread.py`
Fixed-timestep game simulation thread. Handles player movement and physics, entity interaction, trigger evaluation, I/O dispatch, movers, pickups, death, portals and monster AI.

### `monster_ai.py`
Monster behaviour, movement, sight, pursuit, attacks, projectile spawning, death handling and spatial-grid pathfinding.

### `monster_constants.py`
Monster AI tuning and asset constants: sight range, movement, attacks, projectile parameters, sprite frames, billboard dimensions and physics values.

### `obj_loader.py`
Wavefront OBJ/MTL model loader. Parses vertices, UVs, normals, faces and materials and builds the OpenGL buffers used by the renderer. Uses `ResourceManager` in package mode.

### `overhead_sprite.py`
Top-down player sprite controller and renderer for Overhead camera mode. Animation state is separated from drawing; the renderer draws the selected frame as a ground quad oriented to the player's heading.

### `physics.py`
Collision detection, spatial partitioning and dynamic-body physics. `PhysicsWorld` stores dynamic state in contiguous NumPy arrays and performs gravity, damping, pushing, horizontal motion, floor resolution and sleeping in batched operations. `PhysicsBody` is a lightweight handle into that numerical state rather than a separate per-body simulation store.

`SpatialGrid` supplies cell-based brush lookup and collision/raycast/line-of-sight/water queries. Its cell convention comes from `engine.spatial`, shared with Big World streaming.

### `player.py`
Player movement and collision controller: first-person movement, noclip, gravity, jumping, swimming, waterjump, angled-brush collision and step climbing.

### `qt_game_view.py`
Qt `QOpenGLWidget` that owns the GL viewport and frame/update orchestration. Handles input dispatch, play-mode switching, HUD drawing, split-screen layout and renderer selection.

### `entity_table.py`
Dense numerical projection of the entity list — the entity half of `render_table.py`. One row per `Thing`, addressed by an integer slot and named by the entity's existing UUID, with a `uint16` class column resolving what the renderer used to re-derive per entity per frame (PathNode / Portal / Pickup / Light / Prop / the entity-sprite classes, plus `model_path`, `render_mode` and `sprite_path`) and a position column refreshed in bulk.

Split by change frequency, exactly as the brush table is: the class column moves only when the world epoch does, positions are re-read every frame, and `hidden` is never cached — Big World parks entities by writing it with no notification, so every per-frame consumer has to see it live.

`classify_slots` is the array form of `_sort_objects`' Thing half: the model and sprite passes come out as slot arrays, from masks over the class column and the live hidden mask, with no entity touched.

Sprites add three more columns, and are where the cold/warm split is made explicit rather than assumed. Position is warm and size is cold, both straightforwardly; a sprite's *texture* is neither, because a monster's frame follows `dead`/`is_shooting` and a gate's, a pickup's and a prop's follow properties that change without an edit. Those rows — and only those — re-resolve their sprite identity every frame; everything else resolves once. The identity is a *name*, interned to a dense integer exactly as `render_table` interns face textures, and the renderer turns it into a GL texture id on the thread that has a context.

This is deliberately not pushed into `render_table.py`, whose texture column is wholly cold. Two projections with two refresh disciplines is the honest shape; one projection that had to explain when its texture column could be trusted would not be.

### `render_cull.py`
GL-free numerical render-distance culling. Operates on contiguous position data, uses squared-distance arithmetic and reusable scratch buffers, and forms the broad phase before frustum/classification and draw-key processing. Shadow and portal paths can deliberately bypass this broad-phase cull.

### `render_keys.py`
Numerical draw-key machinery. `KeyLayout` declares which dense fields cannot vary within a draw, packs those fields into sortable `int64` keys, and `sort_into_runs` finds equal-key stretches after stable sorting.

The rule is:

> Everything that cannot vary within one draw belongs in the render key. Everything that can vary within the draw travels as instance data.

### `render_table.py`
Dense numerical render projection. Converts render-relevant world state into parallel NumPy arrays such as centres, extents, rotations, render-class flags, texture IDs, UV transforms, colours and geometry epochs.

This is a derived execution representation, not a second source of truth. It exists so visibility, classification, batching and instance construction do not repeatedly traverse Python objects.

### `renderer_core.py`
`BaseRenderer`, the shared OpenGL rendering infrastructure used by renderer backends. Provides shader and texture management, VAOs/VBOs, terrain, models, sprites, water, glass, fog, portals, lighting, shadows, editor helpers, LOD support, statistics and cleanup. `render_scene()` is the concrete-renderer entry point rather than an artificial abstract interface.

Dynamic-light capacity comes from `engine.shaders`; shader light limits are clamped to the capacity actually declared by each shader.

Shadow rendering is part of the dense execution boundary. `render_shadow_maps(shadow_lights, config, camera_pos=None)` receives a dense `(EntityTable, light_slots)` light set and selects brush casters from `RenderTable` slots and model casters from `EntityTable` slots. The shadow pass does not traverse authored `Brush`, `Thing` or `Light` collections.

### `renderer_F.py`
Fio's production forward renderer. Implements the frame passes and brush batching, including lit/textured/glow brush paths, forward lighting, point-light shadow cube maps, portal virtual views and render-mode switching.

The renderer consumes the dense numerical render representation and turns equal-key runs into GPU submissions. Billboards go the same way: `draw_sprites_instanced` reads position, size and texture from the entity projection's columns, packs one instance row per sprite and submits one `glDrawArraysInstanced` per texture run. The object-level sprite renderer has been removed; editor, portal and split-screen views all consume the same dense EntityTable sprite representation.

### `savegame.py`
Native play-session save/load. Serialises player state, entity/mover state, trigger/pickup progress and I/O state to `.fiosave` files and restores it on a freshly loaded map.

### `resource_manager.py`
Singleton asset provider for ordinary filesystem projects and mounted `.fiopak` archives. Handles path resolution, byte/text loading, streams, caching and package manifests.

### `shaders.py`
Shader source management, compilation and uniform binding. Owns the shared dynamic-light capacities and low-power hardware detection used to select cheaper shader variants.

`detect_low_power_arm()` is a hardware-cost decision, not an architectural assumption that all ARM hardware is slow; `FIO_ARM_MODE` can override detection.

### `spatial.py`
The single world-cell convention used by Fio. Defines the 512-unit cell maths, AABB-to-cell operations, cell distance helpers and `CellIndex`. Physics and Big World streaming share this implementation so they cannot disagree about spatial locality.

Also owns the distinction between mapper-authored hidden state and streaming-parked state.

### `sysmon.py`
System monitor overlay. Tracks FPS, frame time, visible/culled geometry, triangle counts and GPU-memory information using reusable buffers and cached display text.

### `terrain.py`
Chunked terrain generation and rendering, including Perlin-noise heightmaps, chunk LOD meshes, normals, texture blending and collision queries.

### `textures.py`
OpenGL texture manager. Loads images through `QImage`, converts them to RGBA, uploads them and caches texture IDs, with package-mode access through `ResourceManager`.

### `threaded_game_state.py`
Thread-safe bridge between simulation and rendering. `ThreadedGameState` synchronises updates; `RenderState` provides the per-frame render snapshot containing camera, player, visible-world and HUD state.

## Numerical execution architecture

The performance-sensitive parts of the engine increasingly follow this shape:

```
authoritative world
      ↓
Python objects / dictionaries
      ↓
dense numerical execution representation
      ↓
batched visibility / classification
      ↓
numerical keys and stable sorting
      ↓
equal-key runs / packed payloads
      ↓
OpenGL
```

The renderer therefore is not merely a collection of Python draw calls with NumPy sprinkled around it. `render_table.py`, `entity_table.py`, `render_cull.py` and `render_keys.py` form a numerical frontend between the flexible world model and the GPU backend. Brushes and entities are projected the same way and on the same refresh discipline, so neither half of the world is re-interrogated object by object once a frame starts.

The same principle is used by `physics.py`: simulation state is dense and contiguous while `PhysicsBody` remains a convenient object/API handle.

The representation boundary is deliberately selective. Small scalar operations and stateful systems remain ordinary Python where vectorisation would add overhead without removing meaningful work; large homogeneous populations and repeated numerical decisions are moved into arrays.

## Rendering stack

```
QtGameView
    ↓
Renderer_F
    ↓
render_table / entity_table / render_cull / render_keys
    ↓
BaseRenderer / OpenGL resources
    ↓
OpenGL 3.3 Core
    ↓
GPU
```

Fio uses programmable OpenGL 3.3 Core shaders, not the legacy fixed-function pipeline.

The renderer is forward rather than deferred/G-buffer based. It supports texture batching and instanced brush paths, terrain, models, sprites, water, glass, fog, portals, point-light shadow cube maps and editor overlays.

Low-power hardware is handled through cheaper shader variants and reduced-cost effects rather than a separate renderer architecture.

