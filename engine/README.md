# `engine/`

The runtime engine: world simulation, physics, resource loading, dense execution projections, OpenGL rendering, shaders, terrain, models, sprites, portals, cutscenes and the Qt game viewport.

The authoritative gameplay/world state remains object-oriented. Performance-sensitive execution paths project that state into dense NumPy representations before entering hot loops. Python objects provide the world/API model; contiguous numerical arrays provide the execution representation.

## Engine modules

### `actor_pick.py`
Numerical actor picking from the published execution tables. Screen rays test dense monster rows against visible solid brush occluders, so picking follows the same projected world the renderer drew rather than walking authored objects.

### `brush_geometry.py`
Convex brush geometry based on intersections of half-space planes. Computes surface windings, collision meshes, 2D silhouettes and bounds. The plane set is the source of truth; derived geometry is cached using a geometry signature and invalidation epoch. Also provides component-edit geometry operations and uses NumPy without depending on Qt.

### `camera.py`
Camera state and view/projection matrix computation for first-person and other 3D views.

### `change_journal.py`
Runtime change notification for dense projections. Tracks transform, state and visibility changes so RenderTable and EntityTable refresh only affected rows while retaining an overflow path that forces a complete refresh when a subscriber cannot keep up.

### `constants.py`
Shared engine constants covering window defaults, dimensions, render modes, physics tuning and water physics.

### `cutscene_runtime.py`
Dedicated cutscene playback state machine. `LogicThread` remains the simulation orchestrator; this module owns cinematic playback state, track sampling, temporary actor lifetime, timed I/O/events, camera control and restoration of overridden world state.

Cutscene files are resolved only beneath the project's `cutscenes/` directory. Camera tracks support position, yaw, pitch, FOV, interpolation, teleport and look-at behaviour. Actor tracks can temporarily remove existing monsters from normal simulation, spawn authored temporary actors and restore the captured world when playback ends. Timed fights and cinematic messages are also handled here.

This separation is intentional: `LogicThread` schedules the runtime update, while `CutsceneRuntime` owns the cutscene state machine. The public compatibility wrappers on `LogicThread` preserve the existing integration surface for editor I/O and tests.

### `effect_entity.py`
Unified Effect world primitive. FIRE, ORB, EXPLOSION and CUSTOM are behaviours of the same authored entity, with procedural animation and optional intrinsic dynamic light. Runtime/render state is projected into EntityTable rather than maintained as a second effect renderer.

### `entity_table.py`
Dense numerical projection of the entity list. Rows are addressed by integer slot while retaining the entity's UUID identity. Classification, position, model/sprite information and live visibility are represented in columns so renderer passes can operate on arrays rather than re-interrogating Things.

The table deliberately separates cold state from warm/live state. Sprite texture identity is re-resolved only for rows whose visual state can change without an authoring edit, such as monster animation and relevant Prop/gate state.

### `fileio.py`
Crash-safe JSON writes for maps, autosaves and saved games: documents are serialised first, written beside the destination and moved over it so a failed save cannot truncate the previous file.

### `floating_windows.py`
Reusable Qt floating-window infrastructure used by QtGameView overlays such as SysMon. Provides draggable/collapsible window chrome, stacking and event routing without importing the editor or game systems.

### `glasses.py`
Player glasses appearance definitions and path/texture-key normalisation. Player 1 can choose a style in Settings; player 2 uses the default style.

### `glb_loader.py`
GLB/glTF 2.0 binary model loader. Extracts mesh geometry, normals, UVs, indices, PBR materials, embedded textures and node hierarchies and exposes an OpenGL-ready model interface.

### `logic_thread.py`
Fixed-timestep game simulation orchestrator. Handles player movement and physics, entity interaction, trigger evaluation, I/O dispatch, movers, Props, death, portals and MonsterAI.

Cutscene playback is deliberately delegated to `CutsceneRuntime`; LogicThread owns the simulation tick and integration points rather than the cinematic state machine itself.

Runtime entity creation is treated as a world mutation, not a renderer-side operation: relevant derived caches are rebuilt at the mutation boundary, including the prefiltered monster list used by MonsterAI.

### `monster_ai.py`
Monster behaviour, movement, sight, pursuit, attacks, projectile spawning, death handling and spatial-grid pathfinding.

### `monster_constants.py`
Monster AI tuning and asset constants: sight range, movement, attacks, projectile parameters, sprite frames, billboard dimensions and physics values.

### `monster_table.py`
Dense numerical execution representation for MonsterAI. Gathers per-monster positions, flags, targets, distances, ground heights and per-tick outcomes into columns, allowing the high-volume AI work to be vectorised while Monster objects remain authoritative.

The table records execution modes and debug-visible per-tick results without deciding behaviour itself; `MonsterAI.update` remains the behavioural authority.

### `mover_table.py`
Dense runtime representation for movers and doors. Closed-form motion is advanced in NumPy columns while preserving synchronous I/O sequence points and compatibility with existing mover/door state mappings. Authoritative brush parameters remain on the brushes.

### `obj_loader.py`
Wavefront OBJ/MTL model loader. Parses vertices, UVs, normals, faces and materials and builds the OpenGL buffers used by the renderer.

### `overhead_sprite.py`
Top-down player sprite controller and renderer for Overhead camera mode. Animation state is separated from drawing; the renderer draws the selected frame as a ground quad oriented to the player's heading.

### `physics.py`
Collision detection, spatial partitioning and dynamic-body physics. `PhysicsWorld` stores dynamic state in contiguous NumPy arrays and performs gravity, damping, pushing, horizontal motion, floor resolution and sleeping in batched operations. `PhysicsBody` is a lightweight handle into that numerical state rather than a separate per-body simulation store.

`SpatialGrid` supplies cell-based brush lookup and collision/raycast/line-of-sight/water queries. Its cell convention comes from `engine.spatial`, shared with Big World streaming.

### `player.py`
Player movement and collision controller: first-person movement, noclip, gravity, jumping, swimming, waterjump, angled-brush collision and step climbing.

### `portal_transform.py`
GL-free portal spatial algebra. Provides the single point/direction transform used by portal rendering and gameplay, including portal pairing and mirror transforms.

### `prop_entity.py`
Unified Prop world primitive. A Prop can independently be a model or billboard and can optionally be carryable, collectable or a weapon/key/ammo/health interaction. There is no separate Pickup or Model authoring primitive in the current world model; legacy model/pickup data is normalised into Prop behaviour.

### `prop_runtime.py`
Runtime interaction domain for Props: carrying, collecting, dropping, collection respawns and the Prop spatial index. Physics remains in `physics.py`; the authoritative Thing data remains the world model.

### `qt_game_view.py`
Qt `QOpenGLWidget` that owns the GL viewport and frame/update orchestration. Handles input dispatch, play-mode switching, HUD drawing, split-screen layout, actor picking and renderer selection.

### `render_cull.py`
GL-free numerical render-distance culling. Operates on contiguous position data, uses squared-distance arithmetic and reusable scratch buffers, and forms the broad phase before frustum/classification and draw-key processing. Shadow and portal paths can deliberately bypass this broad-phase cull.

### `render_keys.py`
Numerical draw-key machinery. `KeyLayout` declares which dense fields cannot vary within a draw, packs those fields into sortable `int64` keys, and `sort_into_runs` finds equal-key stretches after stable sorting.

> Everything that cannot vary within one draw belongs in the render key. Everything that can vary within the draw travels as instance data.

### `render_table.py`
Dense numerical render projection. Converts render-relevant brush state into parallel NumPy arrays such as centres, extents, rotations, render-class flags, texture IDs, UV transforms, colours and geometry epochs.

This is a derived execution representation, not a second source of truth. It exists so visibility, classification, batching and instance construction do not repeatedly traverse Python objects.

### `renderer_core.py`
`BaseRenderer`, the shared OpenGL rendering infrastructure used by renderer backends. Provides shader and texture management, VAOs/VBOs, terrain, models, sprites, water, glass, fog, portals, lighting, shadows, editor helpers, LOD support, statistics and cleanup.

Dynamic-light capacity comes from `engine.shaders`; shader light limits are clamped to the capacity actually declared by each shader.

Shadow rendering is part of the dense execution boundary. `render_shadow_maps()` consumes dense light/table data and selects brush/model casters from projection slots rather than traversing authored Brush/Thing/Light collections.

### `renderer_F.py`
Fio's production forward renderer. Implements brush batching, lit/textured/glow paths, forward lighting, point-light shadow cube maps, portal virtual views, render-mode switching and instanced sprite/model submission.

`draw_sprites_instanced` consumes EntityTable position, size, yaw and texture identity and submits one instanced draw per texture run.

### `savegame.py`
Native play-session save/load. Serialises player state, entity/mover state, trigger/collection progress and I/O state to `.fiosave` files and restores it on a freshly loaded map.

### `shaders.py`
Shader source management, compilation and uniform binding. Owns shared dynamic-light capacities and low-power hardware detection used to select cheaper shader variants.

`detect_low_power_arm()` is a hardware-cost decision, not an architectural assumption that all ARM hardware is slow; `FIO_ARM_MODE` can override detection.

### `spatial.py`
The single world-cell convention used by Fio. Defines the 512-unit cell maths, AABB-to-cell operations, cell distance helpers and `CellIndex`. Physics and Big World streaming share this implementation.

It also owns the distinction between mapper-authored hidden state and streaming-parked state.

### `sprite_layers.py`
Entity sprite images as layers of one `GL_TEXTURE_2D_ARRAY`. The billboard pass is blended with depth writes off and is therefore drawn back to front. Image identity travels as per-instance layer data rather than draw state.

### `sysmon.py`
System monitor overlay and machine-readable performance snapshot.

SysMon's FPS value is Fio's runtime FPS over the latest one-second interval maintained by QtGameView. Frame-time values come from the current frame and rolling 60-frame ring buffer, including rolling p95. Geometry counts, draw calls, TPS and VRAM are read from live renderer/runtime state.

### `terrain.py`
Chunked terrain generation and rendering, including Perlin-noise heightmaps, chunk LOD meshes, normals, texture blending and collision queries.

### `terrain_style.py`
Terrain appearance options saved with the map: height-based texture layers, slope-aware rock, terracing, colour sources, contour/grid overlays, cliff tint and stripes, shrubs, ground patches, banded lighting and dithered colour depth. Pure NumPy and testable headlessly.

### `terrain_table.py`
Dense terrain execution data used to keep chunk/terrain work in numerical form rather than repeatedly traversing terrain objects.

### `textures.py`
OpenGL texture manager. Loads images through QImage, converts them to RGBA, uploads them and caches texture IDs.

### `threaded_game_state.py`
Thread-safe bridge between simulation and rendering. `ThreadedGameState` synchronises updates; `RenderState` provides the per-frame render snapshot containing camera, player, visible-world and HUD state.

### `view_distance.py`
GL-free camera view-distance and far-plane fog state. Keeps renderer visibility separate from Big World residency/simulation. The public distance setting drives broad-phase culling, camera far plane and distance fog while maintaining a fog-before-clip margin.

## Numerical execution architecture

```
authoritative world
      ↓
Python objects / dictionaries
      ↓
dense numerical execution representation
      ↓
batched visibility / classification / simulation
      ↓
numerical keys and stable sorting
      ↓
equal-key runs / packed payloads
      ↓
OpenGL / GPU
```

The renderer is not merely a collection of Python draw calls with NumPy sprinkled around it. `render_table.py`, `entity_table.py`, `render_cull.py` and `render_keys.py` form a numerical frontend between the flexible world model and the GPU backend. The same boundary now exists for high-volume MonsterAI and mover/door updates through `monster_table.py` and `mover_table.py`.

The tables are derived execution representations, never authoring stores. `change_journal.py` provides precise runtime invalidation so dense tables do not need to rediscover changes by walking every object every frame.

The representation boundary is deliberately selective. Small scalar operations and stateful systems remain ordinary Python where vectorisation would add overhead without removing meaningful work; large homogeneous populations and repeated numerical decisions are moved into arrays.

## Simulation and cutscene boundary

```
LogicThread
    ↓
fixed-timestep world update
    ├── physics / player
    ├── I/O / movers / Props
    ├── MonsterAI
    └── CutsceneRuntime.update()
             ├── camera tracks
             ├── actor tracks
             ├── timed I/O/events
             └── capture / restore
```

`LogicThread` is intentionally the orchestrator rather than the owner of every gameplay subsystem. CutsceneRuntime is the dedicated cinematic state machine; it owns playback state and restoration while using LogicThread's existing world, I/O and entity systems.

## Rendering stack

```
QtGameView
    ↓
Renderer_F
    ↓
render_table / entity_table / monster_table
    ↓
render_cull / render_keys
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

## Runtime baseline

Fio CANARY requires **CPython 3.14, GIL-enabled**. The supported dependency baseline is maintained in the repository root `requirements.txt`:

- NumPy 2.5.3
- pygame-ce 2.5.8
- PyQt5 5.15.11
- PyOpenGL 3.1.10
- PyGLM 2.8.3
- Pillow 11.3.0

The OpenGL dependency version is the Python binding version; the renderer itself targets the OpenGL 3.3 Core API/profile.

## Observability

Debug Tables exposes the live dense projections, packed render keys/ranges and related counters. This is the primary execution-boundary instrumentation: it inspects the production numerical path rather than maintaining a diagnostic renderer or duplicate world representation.
