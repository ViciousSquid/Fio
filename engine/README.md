# `engine/`

### `audio_manager.py`
Sound effect loading and playback via pygame. Routes through `ResourceManager` for `.fiopak` package compatibility (streams from ZIP in package mode, reads from filesystem otherwise). Caches loaded sounds.

### `brush_geometry.py`
Convex brush geometry module for angled/clipped brushes. Represents brushes as intersections of half-space planes (Quake/Radiant-style), computes surface polygons, builds collision meshes, and provides 2D silhouette and bounds derivation. The plane set is the brush's only source of truth — vertices, windings, render mesh, collision triangles and bounds are all derived and cached from it, keyed on a `geometry_signature` that folds in a monotonic per-brush epoch so any invalidation is visible to every consumer (including caches keyed on `id(brush)`, which CPython reuses after a free). A clip that bounds no surface, or that duplicates a plane the brush already has, is refused rather than appended — mirroring Radiant's `Brush_RemoveEmptyFaces`, since the winding solve is O(planes²) and redundant planes would tax every later rebuild. Also hosts the component-edit primitives (`convex_hull_planes`, `rebuild_brush_from_points`, `offset_brush_planes`), which validate every edit before committing it so a drag can never leave a degenerate brush. Dependency-light (NumPy only) for headless testing and use from the logic thread.

### `camera.py`
Camera class managing position, yaw, pitch, FOV, and view/projection matrix computation. Provides the view matrix for the renderer and the projection matrix for 3D perspective.

### `constants.py`
Shared engine constants: window defaults, tile/wall dimensions, render mode enums (lit, unlit, wireframe, vertex), physics tuning (gravity, jump strength, terminal velocity), and water physics parameters (swim speed, drag, waterjump limits).

### `floating_windows.py`
A small floating-window manager for `QtGameView` overlays, generalising the SysMon popup so the 3D view can host several draggable, collapsible windows at once with correct z-ordering and mouse routing. `FloatingWindow` is the reusable chrome (title bar, drag, collapse, close); `WindowManager` owns the stack and routes events topmost-first; `CallbackWindow` lets a caller supply its own body painter without subclassing. Holds no engine, editor or game imports — it needs only a QPainter and Qt mouse events.

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
Collision detection and spatial partitioning. Implements `SpatialGrid` for O(1) cell-based brush lookup (used by player physics and monster AI), AABB-vs-brush collision, raycast, line-of-sight checks, and water volume overlap queries. The grid's cell convention and bucketing come from `engine.spatial`, shared with Big World's streaming, so the two cannot disagree about which cell a brush is in; `self.cells` is that index's bucket dict, read directly by the query methods so no wrapper sits on the collision hot path. `populate` tests `spatial.authored_hidden` rather than `hidden`, so rebuilding the grid mid-play does not permanently drop brushes a streaming layer has parked.

### `player.py`
Player controller. Handles first-person movement (walk, strafe, sprint), noclip mode, gravity and jump physics, water swimming and waterjump, mesh-based collision with angled brushes, step climbing, and input key mapping.

### `qt_game_view.py`
Qt `QOpenGLWidget` that hosts the renderer and drives the game loop. Manages the paint/update cycle, keyboard and mouse input dispatch, play-mode toggling, HUD drawing, split-screen viewport layout, and swappable renderer registration.

### `render_cull.py`
Camera render-distance cull, kept apart from the renderer as pure GL-free geometry. Drops any object whose centre lies farther than `CAMERA_RENDER_CULL_DISTANCE` from the camera on the XZ plane, comparing squared distances so no square root runs per object, and writing into a persistent scratch buffer so the per-frame path allocates nothing. Runs as a broad phase before the frustum cull and sort; the shadow and portal passes deliberately skip it and keep operating on the full scene.

### `renderer_core.py`
`BaseRenderer` class with shared rendering logic inherited by all renderer backends. Provides texture management, grid drawing, sprite rendering, model loading and drawing, water/glass/fog volume rendering, terrain rendering, editor helpers (gizmo, selection outline, face highlight, connection lines, path nodes, portal wireframes), projected shadows, VAO creation, and shader compilation with hot-reload. Its dynamic-light budget (`MAX_LIGHTS`) is taken from `engine.shaders` rather than written down again, and `_shader_light_cap` clamps each shader's `active_lights` to the array that shader actually declares — the renderer must never tell a shader about more lights than it has room for. `lowpower_mode` (formerly `arm_mode`) selects the cheaper lighting shaders and defaults from the hardware probe in `engine.shaders`.

### `renderer_F.py`
Forward renderer (`Renderer_F`), inheriting from `BaseRenderer`. Implements the forward lighting pass with per-face texture batching, omnidirectional point-light shadow mapping (depth cube-maps), portal virtual-view rendering with distance culling, and render-mode switching (lit, unlit, wireframe, vertex).

### `savegame.py`
Native play-session save/load. Serialises a live play session — player state, entity and mover positions, trigger/pickup progress and I/O state — to `saves/*.fiosave`, and restores it on top of a freshly loaded map. Driven from the debug console's `save`/`load`/`quicksave`/`quickload` commands.

### `resource_manager.py`
Singleton asset provider that transparently serves files from either a standard directory tree or a mounted `.fiopak` ZIP archive. Handles path resolution, byte/text asset loading, stream access for audio, asset caching, and manifest reading in package mode.

### `shaders.py`
Shader source management, compilation, and uniform binding. Loads GLSL files from the `shaders/` directory, defines shadow mapping GLSL snippets (omnidirectional point-light depth cube-maps), and provides the `DEFAULT_SHADERS` dict used by the renderer and terrain system. Also the single place the dynamic-light capacity is defined — `MAX_LIGHTS` (64), `MAX_LIGHTS_ARM` (16), `MAX_LIGHTS_WATER` and `MAX_LIGHTS_TERRAIN` — with the shader sources built from those constants and every light loop clamped to its own array, so the renderer's budget and the shader's capacity cannot drift apart. `detect_low_power_arm()` lives here too, alongside the shader variants it chooses between: it answers "is this low-power hardware?", not "is this ARM?", so Apple Silicon and Snapdragon X Elite get the full shaders while an 8cx-class part gets the cheap ones. `FIO_ARM_MODE` overrides the guess.

### `spatial.py`
World locality: the one 512-unit cell convention Fio partitions space with. Defines `CELL_SIZE`, the `floor(coord / cell_size)` cell maths (`cell_of_point`, `cells_for_aabb`, `cell_bounds`, `cell_distance_sq`, `cells_within`) and `CellIndex`, the bucketing primitive that files an object under every cell its XZ footprint overlaps. `engine.physics.SpatialGrid` and the Big World plugin's streaming both read it, so there is exactly one implementation of "which cells does this box touch?". Also owns `PARKED_HIDDEN_KEY` / `authored_hidden`, which let code that builds durable structures from the `hidden` flag tell "the mapper hid this" from "a streaming layer parked it a moment ago". Stdlib-only — no NumPy, PyGLM or Qt — so the standalone player, a plugin and a headless test can all use it.

### `sysmon.py`
System monitor overlay widget. Displays a draggable, expandable HUD with real-time FPS graph (pre-allocated ring buffer), frame time tracking, visible/culled brush and triangle counts, GPU memory queries (NVX/ATI extensions), and per-second stats text caching.

### `terrain.py`
Chunked terrain mesh generation and rendering. Implements Perlin noise heightmap generation, chunk-based LOD mesh building with per-vertex normals, multi-texture blending, and terrain collision queries.

### `textures.py`
`TextureManager` for OpenGL texture loading and binding. Loads images via `QImage`, converts to RGBA, uploads to GPU, and caches texture IDs. Supports `ResourceManager` for package-mode asset streaming.

### `threaded_game_state.py`
Thread-safe bridge between the logic thread and the renderer. `ThreadedGameState` synchronises game state updates behind locks; `RenderState` is a per-frame snapshot (camera matrices, player state, visible brushes/things, HUD data, split-screen state) copied atomically for the render thread.
