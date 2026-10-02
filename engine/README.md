# `engine/`

### `audio_manager.py`
Sound effect loading and playback via pygame. Routes through `ResourceManager` for `.fiopak` package compatibility (streams from ZIP in package mode, reads from filesystem otherwise). Caches loaded sounds.

### `brush_geometry.py`
Convex brush geometry module for angled/clipped brushes. Represents brushes as intersections of half-space planes (Quake/Radiant-style), computes surface polygons, builds collision meshes, and provides 2D silhouette and bounds derivation. Dependency-light (NumPy only) for headless testing and use from the logic thread.

### `camera.py`
Camera class managing position, yaw, pitch, FOV, and view/projection matrix computation. Provides the view matrix for the renderer and the projection matrix for 3D perspective.

### `constants.py`
Shared engine constants: window defaults, tile/wall dimensions, render mode enums (lit, unlit, wireframe, vertex), physics tuning (gravity, jump strength, terminal velocity), and water physics parameters (swim speed, drag, waterjump limits).

### `glb_loader.py`
GLB/glTF 2.0 binary model loader. Parses the GLB container, extracts mesh geometry (vertices, normals, UVs, indices), PBR materials, embedded textures, and node hierarchies. The `GLB` class provides an OpenGL-ready interface (VAO, VBO, vertex count, material groups) matching the `OBJ` class for renderer compatibility.

### `logic_thread.py`
Game logic thread running at a fixed 60 Hz timestep. Handles player movement and physics, entity interactions and trigger evaluation, I/O event dispatching, mover/door animations, pickup collection, player death, portal transit, and monster AI ticking. Also drives the play-mode camera.

### `monster_ai.py`
Monster behaviour, movement, and pathfinding. Implements sight-range detection, pursuit, attack cooldowns, shoot animations, projectile spawning, death handling, and pathfinding using the spatial grid.

### `monster_constants.py`
Monster AI constants. Defines sight range, shoot interval, move speed, stop distance, sprite frame filenames, per-type billboard sizes, variant folder names, projectile speed/range/size, and physics/collision parameters.

### `obj_loader.py`
Wavefront OBJ/MTL model loader. Parses vertex positions, texture coordinates, normals, face indices, and material references. The `OBJ` class builds OpenGL buffers (VAO/VBO) and provides material groups for the renderer. Falls back to `ResourceManager` for package mode.

### `overhead_sprite.py`
Top-down player sprite for the Overhead camera mode. Split into `SpriteController` (pure animation state machine: idle, walk cycle, armed/shoot poses, facing) and `OverheadSpriteRenderer` (draws the chosen frame as a textured ground quad rotated to the player's heading). Assets live under `assets/sprites/topdown/`.

### `physics.py`
Collision detection and spatial partitioning. Implements `SpatialGrid` for O(1) cell-based brush lookup (used by player physics and monster AI), AABB-vs-brush collision, raycast, line-of-sight checks, and water volume overlap queries.

### `player.py`
Player controller. Handles first-person movement (walk, strafe, sprint), noclip mode, gravity and jump physics, water swimming and waterjump, mesh-based collision with angled brushes, step climbing, and input key mapping.

### `qt_game_view.py`
Qt `QOpenGLWidget` that hosts the renderer and drives the game loop. Manages the paint/update cycle, keyboard and mouse input dispatch, play-mode toggling, HUD drawing, split-screen viewport layout, and swappable renderer registration.

### `renderer_core.py`
`BaseRenderer` class with shared rendering logic inherited by all renderer backends. Provides texture management, grid drawing, sprite rendering, model loading and drawing, water/glass/fog volume rendering, terrain rendering, editor helpers (gizmo, selection outline, face highlight, connection lines, path nodes, portal wireframes), projected shadows, VAO creation, and shader compilation with hot-reload.

### `renderer_F.py`
Forward renderer (`Renderer_F`), inheriting from `BaseRenderer`. Implements the forward lighting pass with per-face texture batching, omnidirectional point-light shadow mapping (depth cube-maps), portal virtual-view rendering with distance culling, and render-mode switching (lit, unlit, wireframe, vertex).

### `savegame.py`
Native play-session save/load. 

### `resource_manager.py`
Singleton asset provider that transparently serves files from either a standard directory tree or a mounted `.fiopak` ZIP archive. Handles path resolution, byte/text asset loading, stream access for audio, asset caching, and manifest reading in package mode.

### `shaders.py`
Shader source management, compilation, and uniform binding. Loads GLSL files from the `shaders/` directory, defines shadow mapping GLSL snippets (omnidirectional point-light depth cube-maps), and provides the `DEFAULT_SHADERS` dict used by the renderer and terrain system.

### `sysmon.py`
System monitor overlay widget. Displays a draggable, expandable HUD with real-time FPS graph (pre-allocated ring buffer), frame time tracking, visible/culled brush and triangle counts, GPU memory queries (NVX/ATI extensions), and per-second stats text caching.

### `terrain.py`
Chunked terrain mesh generation and rendering. Implements Perlin noise heightmap generation, chunk-based LOD mesh building with per-vertex normals, multi-texture blending, and terrain collision queries.

### `textures.py`
`TextureManager` for OpenGL texture loading and binding. Loads images via `QImage`, converts to RGBA, uploads to GPU, and caches texture IDs. Supports `ResourceManager` for package-mode asset streaming.

### `threaded_game_state.py`
Thread-safe bridge between the logic thread and the renderer. `ThreadedGameState` synchronises game state updates behind locks; `RenderState` is a per-frame snapshot (camera matrices, player state, visible brushes/things, HUD data, split-screen state) copied atomically for the render thread.
