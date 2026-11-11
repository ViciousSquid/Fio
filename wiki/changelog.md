## 3.5.0.1111

Developed on the `3.1.0.0611_CANARY` branch. Maps saved by this build are **map version 5**: 3.0.0
maps load and are converted, but 3.0.0 does not understand version 5 (it
would misplace angled brushes). Exported `.fiopak` packages still carry
version 4 and play in both.

### Breaking changes

- **Map format version 5.** An angled (convex) brush stores its planes in the
  brush's own frame, centred on its `pos`, with one material record per face
  on the plane; the per-tag copies and the play-state keys older builds wrote
  into maps (`original_pos`, `rotation_yaw`) are gone. Older maps are
  converted when loaded. Package export writes the version 4 layout
  (`engine.brush_placement.export_world_level`).
- **Plugin API 1.9.0.** 1.8.0: a brush's `pos` is its authored placement and
  never moves in play; plugins read a moving brush through
  `engine.brush_placement.placement(brush)`. 1.9.0: the renderer contract takes
  published tables only -- the frame's `terrain` is an immutable
  `TerrainFrame`, and `draw_face_highlight`, `draw_connection_lines` and
  `draw_collision_visualization` take vertex arrays. Only plugins that move
  brushes or implement a renderer are affected; see `plugins/API.md`.
- **Packages and asset archives are read in place.** Tools > Play Game Package
  no longer extracts a `.fiopak` to a temporary folder. The
  `assets/*.fiopak` category archives are no longer committed to the
  repository; assets are loose files there.

### New

- **Deathmatch plugin**: free-for-all, team play and Capture the Flag with
  bots (difficulty levels, weapon and pickup seeking, a waypoint navigation
  graph with automatic links, Ctrl-K manual links and mover/door routes), a
  bot diagnostics window (`botdebug`), spectators with a spectator menu,
  glasses, and LAN play with lobby discovery and a host-authoritative game.
  Two arenas ship with it. See `plugins/deathmatch/README.md` and
  `NAVIGATION_GUIDE.md`.
- **Android player parity**: Deathmatch (LAN included), Bigworld and Tidy run
  in the Android player, with their menus and scoreboard.
- **Cell-group occlusion culling** in Play: hardware occlusion queries over
  the spatial cells, with a probe budget that pays for itself or parks.
  `[Renderer] occlusion_culling` turns it off.
- **Gore**: projected blood decals where a shot actor's blood meets the world,
  blood on a wounded actor's sprite, and a gore level (Off, Normal, Heavy,
  Verhoeven) in Settings > Play Modes.
- **Directional damage indicators** at the edges of the view
  (`[Display] damage_indicators`, on by default).
- **Eight-direction monster sprites** with persistent facing.
- **Terrain paint and stamps**: paint maps at 256 texels a chunk, stamp
  textures tiled at a fixed world scale, cheaper stamp placing and dragging,
  and no grass on painted ground. Maps with older paint are converted.
- **Water**: a Wave Spacing slider, broader waves, and high-quality water
  reflects the effects above it.
- **Parked movers sleep**: a mover outside the simulated area costs nothing
  per tick; its I/O stays live and its state catches up on demand.
- Editor: **H** hides the selection (entities included) and **Shift+H** shows
  everything hidden; the scene hierarchy shows tag, hidden and lock
  indicators left of the name and is 200 px wide on every platform; loading
  shows a small floating panel; the Player Start is a player-sized model;
  SysMon is smaller and keeps its frame-time graph when collapsed.
- `tools/package_e2e/`: an end-to-end check that builds a world in the editor,
  exports it, and plays the package in a pristine install and in the staged
  Android runtime, comparing the engine's dense tables.

### Engine

- **Brush model, Stage 2**: one placement and bounds resolver
  (`engine/brush_placement.py`), one material record per face
  (`engine/brush_faces.py`), the mover table owns runtime transforms, and
  collision uses a brush's shape in its own frame.
- **Renderer**: tables all the way to GL. Angled brushes share GPU storage
  with resident transforms; passes keep what they derive from the tables and
  re-upload only what changed; the terrain is published as tables and its GL
  is owned by the renderer (`engine/renderer/core/terrain_gl.py`), with height
  grids built off the paint thread; models are drawn from resolved draw
  groups; overlays are fed arrays.
- `QtGameView` is broken up into the classes it delegates to
  (`engine/view_audio.py`, `view_components.py`, `view_debug_draw.py`,
  `view_hud.py`, `view_messages.py`, `view_pause.py`, `view_picking.py`,
  `view_terrain_edit.py`).
- Logic ticks are paced to their due time; garbage collection is settled at
  load and at Play changes rather than during play.

### Performance

- A map's textures are loaded with the map rather than every texture at
  start-up; FIRE and ORB animations load when a map's effects use them.
- The editor's 2D views repaint at 10 Hz while a game is played.
- Movers publish only what moved; the player's movers come from a dense broad
  phase over the mover table; a stationary scene asks the placement resolver
  nothing per frame.
- The terrain heightfield is drawn indexed.

### Fixes

- **Android player builds again.** Its engine adapter no longer applied to
  the reworked engine, so the Android workflow failed on every push: packaged
  cutscenes, model collision from a package and terrain (now drawn through
  the engine's `TerrainGL`) are restored, the water program links under GLSL
  ES again, and angled brushes are loaded in the engine's local-frame layout
  instead of displaced by their own position.
- Console: `help` raised whenever an enabled plugin had registered commands;
  `showcollision` raised on its first toggle and refused `all`; `bind` stored
  keys as typed (so `bind f5 ...` never fired), accepted anything as a key,
  and keys such as `Ctrl+=` or commands containing `%` left `settings.ini`
  bindings unwritable.
- Debug > Position (P) raised instead of opening the position window.
- F1 in Play raised when the main window, not the 3D view, had the keyboard.
- Play refused for lack of a Player Start switched the side panel to the
  Debug Console and never switched it back.
- SysMon asks for GPU memory only where the driver advertises the query.
- Stopping Play with a brush that is both a mover and a door.
- Duplicate plugin submenus in the 2D context menu.
- Deathmatch opponents rendering below the floor.
- Angled brushes and water unlit on macOS (a uniform at location 0).

## 3.0.0.3010 (Milestone 12)

* Toolbar defaults to the right and scales to available space

* Toggle editor freelook with `Z`

* BigWorld now works in both `editor` and `play` modes

* NEW: Fio UI font family with normalised metrics intended to match across Windows and macOS.

* NEW: `LightStream` handles lights spatially instead of reconsidering every light every frame.

* Faster loading of large maps

* Scene Hierarchy collapses into an edge strip

* Asset Browser thumbnails have 3 size settings

* Native build correctness fixes for Android Player

* GitHub Actions workflow tests editor presentation on native Windows and macOS runners at different display scales

---

## 3.0.0.1810 (Milestone 11)

* Fio Player for Android ([see here](https://github.com/ViciousSquid/Fio/tree/3.0.0.1810/player))

Export standalone worlds as `.fiopak` files from desktop and play them on mobile (Tools > Export game package)

* Further optimisation passes over `engine` and `editor` - aggressively removed unnecessary Python work

* Fixed MacOS mouse sensitivity issues

* Terrain grass height can be adjusted

* NEW: Filters, Cordons and Visgroups

* NEW: Display modes; Solid Lit, true-edge Wireframe, Points and Overlay

* Fio can read directly from [`.fiopak`](https://github.com/ViciousSquid/Fio/wiki/.fiopak-archive) archives in the `assets` folder

* Full releases for Windows X64 and ARM64, MacOS, Linux and Android (player only)

## 3.0.0.1010

Fio 3.0 is a substantial engine and editor revision, not just a feature update. It preserves Fio's live-world workflow and dense numerical execution model while restructuring runtime ownership, establishing a renderer plugin boundary, adding gameplay and authoring systems, tightening input validation, and expanding automated coverage.
### Major changes
**Engine architecture and runtime**
- Split the monolithic logic loop into owned runtimes. Responsibilities such as player control, combat, collision, movers, portals, parenting, rendering, session lifecycle and world logic now live in separate runtime modules. LogicThread coordinates them instead of owning all those systems directly.
- Extend the dense data-oriented core. Add Effect and Projectile stores and supporting structure-of-arrays utilities. Existing dense tables and published render state remain central to the execution path.
- Unify world gravity. Player, monsters, dropped props and physics bodies read the live physics world's gravity rather than maintaining conflicting per-system values.
- Add runtime cutscenes. JSON-authored sequences run through a LogicCamera and support camera keyframes, temporary actors, timed I/O events and editor preview.
- Add map and save validation. Malformed input is rejected with field-specific feedback before it can leave the runtime in an invalid state.
Renderer architecture
- Replace the old renderer-core module layout with engine.renderer. The package separates common renderer facilities from the forward renderer and exposes a named renderer protocol/registry as the plugin boundary.
- Support live renderer switching in the editor and Play mode. r_renderer lists registered renderers and selects one by name; a renderer that fails to start leaves the current renderer running.
- Include a deferred-renderer example under docs/examples/deferred_renderer/ and document renderer development.
- Expand display modes. Points mode draws a distance-coloured point-cloud view; Overlay combines textured surfaces with brush-triangle edges; Wireframe draws true brush edges. The r_wireframe command is tied to the Display control.
- Add renderer-independent diagnostics. Debug Tables identifies the active renderer, can display renderer-published details and shadow-map draw counts, and exposes selected-object table rows.
- Improve rendering correctness and resource lifetime. Fix terrain shader compilation and repeated compilation attempts, terrain paint/height texture binding, portal fade opacity, Solid Lit/Wireframe behaviour, and GPU-resource cleanup when renderers are replaced.
Gameplay, physics and items
- Move player movement toward Quake II rules: walk at 200 units/s, sprint at 300, crouch at 100, Quake II-style acceleration and friction, air control, slope limits, crouch clearance and step handling. Gravity is unified across the simulation.
- Add sprint (hold Shift) and a revised jump impulse. Jump height is intentionally lower than in 2.5.10.
- Introduce data-driven item definitions. gun1, gun2, custom1 and custom2 are item definitions. The new Custom Items editor can define hitscan, projectile or melee weapons with configurable damage, range, cooldown, pellets, ammunition, sound, noise and sprites; it can also define health, ammo, armour, key and weapon pickups.
- Add weapon slots 1–4, HUD weapon feedback and weapon/ammunition carry-over through LevelChanger transitions.
- Add armour pickups and damage absorption before health is reduced.
- Add HUD styles and controls for opacity and damage fade, including hudstyle, hudopacity, hudfade and the hudtext alias.
- Improve spawn and level-transition authoring. PlayerStarts can be explicitly Primary; LevelChangers can name a destination PlayerStart in the target map. Missing destinations and missing target-map starts produce authoring/runtime feedback instead of silently spawning somewhere else.
- Make generated-level transitions reusable. A LevelChanger can generate another level with the same generator settings and a fresh seed.
- Add a pause menu opened with Esc in Play, with Resume, Save/Load Game, audio and display options, plugin-provided entries and Exit. Save games use version 3; version 2 saves remain loadable.
Editor and authoring workflow
- Add Visgroups & Cordon in a dedicated window: create and manage named groups, show/hide members, and limit the visible/editable area to a box. These authoring filters do not change Play simulation.
- Add editor view filters for brushes, movers, triggers, terrain, water, glass, fog, lights, path nodes, monsters, props, effects, portals, logic entities, speakers, PlayerStarts and other entity types. Filtering is applied consistently to 3D/2D views, picking and I/O lines.
- Add a Custom Items editor and improve property editing, item synchronisation and selection overlays.
- Expand the Asset Browser with an Audio tab for assets/sounds and assets/music, speaker assignment, animated GIF texture support and texture type/dimension information on hover.
- Add a large-level load indicator that remains animated while the editor is occupied parsing/building a level and is terminated with the editor.
- Improve terrain tools with persistent axis-aligned-box CSG subtraction, texture stamping and related authoring fixes.
- Improve 2D interaction. Shift-click selects a brush; a plain click with the Brush tool starts drawing, matching the empty-space workflow. The Q shortcut now cycles through the complete tool sequence.
- Improve unsaved-change protection. Replacing a level through Recent Files, console commands or procedural generation prompts before discarding edits. A level change requested during Play saves the edited world where required by the restore-world setting.
Audio, textures, models and packaging
- Support animated GIF textures on brushes/faces in Play, with pause-aware animation and optional renderer animation hooks. Editor previews and asset-browser thumbnails remain safe to display.
- Add music playback from assets/music, independently controlled from sound volume. Speakers are restricted to the supported sound/music asset folders.
- Correct OBJ orientation and parsing. Detect Z-up models and orient them upright by default, with # fio:up=y / # fio:up=z overrides. Correct material-group index ranges and tolerate non-numeric OBJ/MTL coordinates as zero.
- Fix GLB loading with NumPy 2, including indexed models, and decode accessors, interleaved vertices and triangle lists in bulk. The engineering audit reports a 200,000-vertex model parsing in about 13 ms versus about 490 ms before the bulk-decoding change.
- Improve game-package export. Include cutscenes and assets required by actors they spawn; resolve package map transitions against the package's own map set. Package builds no longer accidentally depend on the developer's local maps/cutscenes.
- Add a native Windows ARM64 build workflow and update the standalone/Android player's GLES shader handling and packaging support.
Reliability, security and performance
- Validate maps and save games when loading, and reject malformed files before they corrupt the live session.
- Bound .fiopak extraction by the player's asset-size budget; reject manifests/maps over 64 MiB before inflation.
- Restrict GLB external buffers to relative paths to regular files, cap model size, and discard primitives with invalid indices/offsets. Bound OBJ/MTL reads to regular files up to 256 MiB.
- Write settings.ini atomically. Prevent map logic from invoking VSync settings writes and prevent the map fps toggle from mutating the settings file.
- Fix Play lifecycle handling so a failed logic tick uses the normal Stop/restore path.
- Fix GL resource cleanup across renderer swaps, map-replacement edge cases, notifications, console history, cutscene export, terrain bounds, portal fades and several editor/Qt object-lifetime issues.
- Improve performance in measured hot paths, including shadow-sampler caching, empty mover groups, terrain chunk-uniform calculation and bulk GLB decoding.
- Expand end-to-end testing across real editor/runtime paths: input-to-render state, edit/undo/save/reload, Play/Stop cycles, renderer hot-swapping and GL resource lifetime.
- Update CI so the Qt/offscreen tier runs on pushes and the GL job requires the reference shader compiler instead of silently skipping that check. CI workflows default to a read-only GitHub token; only the release job needs repository write access.
AUDIT_3.0.md records a same-interpreter, same-dependency comparison against 2.5.10. In the audit snapshot, the non-GL suite reached 4,327 passed, 0 failed, and the Mesa llvmpipe GL tier reached 161 passed. The audit notes that no physical GPU was available, so those GL results verify real contexts, shader compilation and draws under software rendering, not all hardware-driver behaviour. These are audit-snapshot figures, not a claim that this document has independently re-run CI for the current branch head.
Breaking changes and upgrade notes
- Python: CPython 3.14 or newer with the GIL enabled is required. Free-threaded Python is refused at startup; Python 3.10–3.12 is no longer the supported baseline.
- Jumping: jump impulse changes from 355 to 270 units/s. Ledges previously reachable in the roughly 64–79 unit range may no longer be reachable.
- Cosmetic weapons: cig is renamed custom1; custom2 is added. Update map pickups and asset references using the old name/path.
- PathNodes: patrol_speed is no longer migrated to speed; author the speed key.
- Settings: [Renderer] arm_mode is no longer read; use lowpower_mode.
- Legacy pickups: pre-2.5.10 pickup entities load as preserved inert unknown entities. Re-author them as collectible Props.
- Renderer plugins: the renderer API is now 1.6.0. Renderer implementations using the old 1.3–1.5 factory/forward-renderer interface are not compatible; there is no compatibility shim. Ordinary plugins that do not implement a renderer and do not depend on removed internals are less affected.
- LogicThread internals: code that accessed moved LogicThread attributes directly must use the runtime that now owns that responsibility.
- Console toggles: r_fog, r_water, r_glass, r_lighting, r_deferred and their unprefixed aliases are removed because they toggled attributes no renderer consumed.
- Items and saves: weapon pickups now use collect_type: "item" and collect_item; legacy map fields are migrated on load. The old gun2_obtained runtime field and weapon constant tables are removed in favour of combat_runtime.weapons and item definitions. Version 2 saves still load; new saves use version 3.
- OBJ orientation: Z-up models may now load upright automatically. Remove any map-authored X rotation that was only compensating for a Z-up export; use the model's fio:up comment to override detection.
- 2D Brush tool: a plain click on a brush begins drawing rather than selecting it; use Shift-click to select.
- Esc in Play: Esc opens the pause menu rather than leaving Play, except when the player is dead.

---

## 2.5.10.110 (Milestone 10)

* **Dense execution:** Major performance and architecture pass across the pipeline, making table updates change-driven rather than rescanning scene objects every frame. Journalled rows are refreshed in place, table references and visibility masks are retained between reconciliations, and unnecessary per-frame allocations and object walks were removed.
* **Render publication:** Hardened `ThreadedGameState` frame publication and borrowing so published render state remains consistent across the renderer, UI, Debug Tables, and simulation threads. Buffer ownership and lifetime rules were tightened against concurrent access and alternating-buffer hazards.
* **Large-map performance:** Reduced frame-preparation overhead substantially on large stress maps. A 23,928-brush / 3,595-entity profile reduced solo prepare time from **4.3 ms to 1.7 ms** and CPU-only editor paint from **25 ms to 8 ms**, while a 4,393-brush stress case reduced prepare from **26 ms to 0.7 ms** in the editor. Journal overflow, visibility parking, peer-buffer adoption, and redundant reconciliation work were also eliminated or reduced.
* **Loads, undo & invalidation:** Optimised row resolution, checkpoints, undo, restore, and global invalidation. Tables can adopt already-valid data from their peer buffer instead of rebuilding everything, reducing second-buffer post-load work from **1.8 s to 22 ms**. Editor checkpoints avoid unnecessary deep copies and UUID generation, while large-world stop/restore paths avoid repeated table reconstruction.
* **Movers & doors:** Added a dense `MoverTable` so mover and door state advances as vectorised columns rather than through a per-row Python loop. Rendering consumes published mover positions directly, while I/O sequencing, save/load behaviour, riders, paths, twin movers, easing, and state restoration remain regression-tested.
* **Renderer performance:** Enabled back-face culling for the main view's opaque filled passes, eliminating large amounts of away-facing geometry submission while leaving transparent, wireframe, vertex, and portal-specific behaviour intact. Mesa llvmpipe measurements showed lower depth-passing fragment counts and significant reductions in several opaque-view timings. A grid-assisted frustum-culling experiment was also profiled and removed after failing to provide a worthwhile gain.
* **Frustum & render-state efficiency:** Consolidated brush bounds into contiguous numerical storage, removed unused position buffers, reduced attribute-wrapper overhead in instanced rendering, cached visibility masks, and moved light uploads to a single per-frame buffer update. Debug Tables exposes preparation, paint, per-pass, publication, and table-read timings for direct inspection of the runtime pipeline.
* **Renderer cache correctness:** Hardened alternating-buffer caches for sprites, models, movers, convex geometry, and other render resources. Convex mesh reuse now tracks actual geometry signatures and invalidates correctly when structural or texture-transform edits change the generated mesh.
* **Threading & simulation stability:** Serialised logic ticks with play-mode transitions and save/load snapshots, hardened AI thread startup and shutdown, ensured AI failures do not silently terminate monster updates, and fixed cross-thread publication and session-lifetime hazards. Long-running pipeline tests cover concurrent entity churn and published-frame consistency.
* **Lifecycle & session cleanup:** Audited runtime ownership across Play/Stop, restore-on-stop, map loading, entity deletion, BigWorld parking, MonsterAI, LogicThread, I/O queues, collision state, renderer caches, and plugins. Session-owned objects and queued connections are now released correctly so terminated maps cannot remain reachable through runtime caches.
* **Persistence & save/load:** Hardened full saves, delta saves, quicksave/quickload, map restoration, mover/door transforms, monster state, and in-session restores. Delta loads now correctly reset entities and properties absent from the saved delta instead of accidentally retaining live-session state. Full and delta save/load oracles were exercised across all shipped maps and large monster populations.
* **Persistence & package safety:** Hardened map loading, saving, `.fiopak` export/import, and level transitions. Malformed maps are validated before replacing the current scene, saves are atomic, package exports are assembled safely before replacement, project-boundary checks prevent path traversal, and map-originated console commands can no longer persist key bindings into the user's settings.
* **`.fiopak` architecture:** Completed removal of obsolete bundled-plugin execution paths and unused resource/audio managers, tightened package rejection and import behaviour, and fixed plugin lifecycle and ownership edge cases. Package handling now has clearer separation between portable world data and installed plugin code.
* **BigWorld:** Hardened visibility parking and restoration for large worlds, including warm refresh paths that avoid rebuilding cold table columns for parked rows. Runtime row sets, monster caches, and visibility state are reconciled correctly when entities are spawned, deleted, hidden, shown, or moved between world states.
* **I/O & entity lifecycle:** Fixed console connection handling, I/O reverse-index invalidation, portal-link cache refresh, entity deletion/spawn cache consistency, monster revival, and stale monster state. Undo checkpoints now occur before mutations so console operations correctly participate in undo/redo.
* **Gameplay & Props:** Completed the Pickup-to-Prop migration fixes, removed the obsolete `custom` collection type, repaired the showcase `gun2` pickup, and preserved the player's active weapon, secondary weapon state, and ammunition across level changes. Property-editor lifetime handling was hardened against deleted Qt layouts and stale transient widgets.
* **HUD & presentation:** Refined the health/ammo HUD position and behaviour, including independent health opacity timing, faster fade-in, darker low-health tinting, corrected colour/opacity ramps, and bottom-edge alignment for the main and split-screen views. Transient message positioning and startup/version presentation were also cleaned up.
* **Fog & water:** Fixed fog volumes so their bottom faces correctly fog the floor when viewed from inside the volume, with visual regression coverage. Completed removal of obsolete planar water-reflection machinery while retaining compatibility with maps containing the old saved property.
* **Audio:** Prevented repeated audio-device probing from stalling the UI whenever a sound was played without a working audio device. Failed initialisation is now backed off and retried periodically instead of blocking on every sound request.
* **Editor & correctness hardening:** Removed unreachable renderer/editor APIs, invalid attribute probes, dead culling toggles, duplicate renderer methods, overly broad exception handlers, and other stale paths uncovered by the audit. Fixed save-version validation, entity property type preservation, transactional level changes, duplicate entity IDs in shipped maps, stale collision/spatial state, and several latent play-mode lifecycle bugs.
* **Debug Tables & verification:** Strengthened Debug Tables into an independent dense-state correctness oracle. Fresh tables rebuilt from authoritative world state can now be compared against live tables column-by-column, catching stale or missing journal updates that ordinary behavioural tests can miss.
* **Fuzzing & stress testing:** Added real console-command fuzzing across editor and Play modes, exercising thousands of command combinations while pumping the actual Qt event loop. Large-world, monster, persistence, lifecycle, renderer, and concurrent mutation stress tests exercise the threaded runtime rather than only isolated units.
* **Testing & regression coverage:** Expanded regression, visual-equivalence, performance, persistence, threading, mover/I/O equivalence, audio, HUD, fog, Prop, lifecycle, cache, and renderer tests. The hardening pass found and fixed numerous latent bugs that were not exposed by the previous unit-test suite.
* **Release validation:** The release build was tested on the target **Snapdragon SQ3** hardware across all shipped maps, validating the complete packaged runtime on the low-power ARM target. Fio 2.5.10.2909 continues to deliver roughly **35–40 FPS average** on the SQ3 in representative gameplay workloads.
* **Release polish:** Finalised the **2.5.10.2909** version presentation, including non-clickable startup version/help text, corrected console handling of version numbers, fixed version-banner formatting and escaping, and added regression coverage for the release banner.

2.5.10.3009 is a major performance, hardening, correctness, and architectural consolidation release, bringing the 2.5 data-oriented runtime, renderer, simulation, persistence, and editor pipeline together into a thoroughly exercised release-quality system.


---

## 2.5.7.2709

* **Dense execution & rendering:** Major expansion of the dense [`EntityTable`](https://github.com/ViciousSquid/Fio/wiki/EntityTable) / [`RenderTable`](https://github.com/ViciousSquid/Fio/wiki/RenderTable) architecture, moving more renderer state and entity data into contiguous numerical tables and reducing reliance on per-object rendering paths.
* **Render stability:** Improved frame snapshots, stable dense references, live table appends, render-state handling, geometry handles, instance payloads, and concurrent modification behaviour.
* **[Renderer](https://github.com/ViciousSquid/Fio/wiki/Renderer-Technical-overview):** Substantial renderer and shader refactoring, with improvements to batching, instancing, lighting, water reflections, terrain, fog, and render-state management.
* **Effects:** Added the new `Effect` primitive with animated GIF effects, fire/orb variants, custom effects, visibility and looping controls, numeric I/O variants, sprites, and associated sounds.
* **Terrain:** Expanded terrain editing and rendering capabilities, including improved editor interaction and water-reflection support.
* **Benchmarking:** Major overhaul of the optional Benchmark plugin with dedicated stress tests, live SysMon measurements, monster and brush workloads, controlled benchmark sessions, and improved result reporting.
* **Benchmark reports:** Added one-click HTML benchmark report export and improved completion/status presentation and metric definitions.
* **Debugging & observability:** Added the [**Debug Tables** tool](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables) for directly inspecting `EntityTable` and `RenderTable` contents, dense rows, keys, and render data from the editor.
* **Console messages:** Added timed `message` and `message2` commands, queued view messages, separate message lines, expiry ordering, and improved 3D-view positioning.
* **Editor:** Continued improvements to the property editor, terrain editor, 2D view, toolbar, settings, and editor UI.
* **[I/O system](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System):** Expanded I/O handling and reverse-index support, including additional entity inputs and outputs for effects and gameplay systems.
* **Logic & gameplay:** Improved movers, speakers, trigger filtering, pickups, monster behaviour, and related logic tests.
* **Testing:** Added substantial regression coverage for dense tables, renderer snapshots, effects, pickups, terrain, water reflections, messages, I/O, and performance-sensitive paths.
* **CI & builds:** Hardened the test and Nuitka build workflows, improved release automation, and fixed release/build validation issues.
* **Documentation:** Updated engine/editor architecture documentation and documented dense execution observability and related debugging facilities.

**2.5.7.2709** is a substantial continuation of the 2.5 architecture, with particular emphasis on **dense data execution, renderer observability, effects, benchmarking, and making the engine's numerical state directly inspectable.**


---

### v2.5.6.2509

### Renderer & Numerical Core
- Continued migration from authored `Thing`/`Brush` objects to dense NumPy render tables.
- Expanded [`RenderTable`](https://github.com/ViciousSquid/Fio/wiki/RenderTable) / [`EntityTable`](https://github.com/ViciousSquid/Fio/wiki/EntityTable) usage across rendering and batching.
- Added packed render keys and contiguous render-run sorting.
- Moved convex geometry to dense `geometry_id` records, removing the `RenderTable → Brush` geometry lookup.
- Expanded GPU instancing and dense light/shadow data paths.
- Removed legacy object-based rendering paths for sprites, brushes, lights, water, glass, fog, and glow.
- Tightened the renderer toward an **arrays → batching → OpenGL** pipeline.

### World & Runtime
- Continued separation of authored/editor state from runtime numerical state.
- Expanded Prop/physics/runtime synchronization and spatial processing.
- Continued portal, BigWorld, visibility, and world-streaming work.
- Improved procedural world-generation and runtime infrastructure.

### Editor & Tools
- Continued EditorState, hierarchy/property editing, procedural authoring, and I/O tooling improvements.
- Expanded plugin infrastructure and moved benchmarking into the optional benchmark plugin.
- Continued `.fiopak` packaging, import/export, and persistence work.

### Testing & Cleanup
- Added regression coverage for render tables, render keys, geometry IDs, instanced payloads, lights, persistence, physics, and editor/runtime behaviour.
- Removed obsolete compatibility, bridge, and duplicate runtime paths where the new architecture superseded them.
- Updated renderer, engine, plugin, and API documentation alongside the architectural changes.

---

### v2.5.5.2209 | Milestone 10

**ENGINE / ARCHITECTURE**
- **NEW:** Dense numerical runtime architecture for world state, simulation data, and render-facing state, using persistent NumPy arrays and bulk operations where workloads are naturally data-parallel.
- **IMPROVED:** Engine hot paths increasingly operate on contiguous dense batches rather than repeatedly traversing Python objects and dictionaries.
- **IMPROVED:** Persistent dense projections reduce repeated extraction, classification, lookup, and reconstruction of derived runtime state during ticks and rendering.
- **IMPROVED:** Runtime systems distinguish between dense workloads suited to vectorised processing and genuinely sparse or small workloads where scalar processing remains more efficient.
- **IMPROVED:** Dense runtime data now forms explicit boundaries between world state, simulation, culling, batching, and rendering, allowing each stage to operate on numerical data without repeatedly crossing back into object-level representations.
- **PERFORMANCE:** Continued optimisation for low-power CPUs, including ARM-based systems, with substantial reductions in Python-level per-item work across simulation and rendering paths.

**WORLD SIMULATION**
- **NEW:** Expanded vectorised distance and visibility processing for large numbers of entities.
- **IMPROVED:** Entity distance culling uses batched NumPy X/Z calculations rather than entity-by-entity Python distance checks.
- **IMPROVED:** Simulation, visibility and spatial systems increasingly operate from cached numerical representations of the world.
- **IMPROVED:** Physics, movers, doors and other periodic simulation work can run at lower update frequencies where 60 Hz provides no meaningful benefit.
- **IMPROVED:** Runtime work is increasingly separated into appropriate update rates rather than forcing every system to execute at the same frequency.

**LOGIC / I/O**
- **IMPROVED:** Entity I/O now carries the original activator through event chains, allowing downstream entities to identify what initiated an event.
- **NEW:** Per-entity `IO enabled` control allows individual entities to participate in or opt out of the I/O system.
- **IMPROVED:** Logic primitives continue to compose through the existing event-driven I/O model without introducing a separate scripting runtime.
- **IMPROVED:** LogicState and I/O remain part of the live world simulation rather than a separate scripting layer.

**ENTITIES**
- **NEW:** First-class `Prop` engine primitive — a generic physical world object that can be picked up and carried.
- **IMPROVED:** Prop configuration supports models or sprites, optional mass/collision behaviour and explicit I/O participation.
- **IMPROVED:** Trigger filtering and runtime containment handling provide more precise control over which entities interact with trigger volumes.

**BIGWORLD / STREAMING**
- **IMPROVED:** BigWorld streaming integrates with the numerical runtime and simulation-distance architecture.
- **IMPROVED:** World entities retain stable UUID identity across streaming, persistence and simulation.
- **IMPROVED:** Dormant entities remain represented by persistent world state while expensive runtime work is avoided outside the active simulation range.
- **PERFORMANCE:** Distance-based streaming and visibility decisions increasingly use batched numerical operations.

**RENDERING / CULLING**
- **PERFORMANCE:** Batched distance culling reduces Python overhead when processing large entity populations.
- **IMPROVED:** Far-plane and distance culling prevent unnecessary render preparation for distant world objects.
- **IMPROVED:** Renderer and simulation visibility systems share configured world-distance limits.
- **IMPROVED:** Fog and far-plane behaviour reduce unnecessary work beyond the playable/renderable world range.

**EDITOR**
- **NEW:** `Show AABB` property on trigger entities displays the actual runtime containment box.
- **IMPROVED:** Validate Connections reports I/O contract and connection problems more clearly.
- **IMPROVED:** Asset Browser layout is now width-aware.
- **IMPROVED:** Thing sprites have been refined in both 2D and 3D editor views.
- **IMPROVED:** Property Editor uses explicit property groupings for clearer entity configuration.
- **IMPROVED:** Empty or irrelevant property sections are suppressed to reduce inspector noise.
- **IMPROVED:** PlayerStart angle configuration is exposed directly with the primary entity properties.

**LOGIC GRAPH**
- **IMPROVED:** Logic Graph remains a graphical representation and editor for the underlying I/O connection model.
- **IMPROVED:** Graph editing does not introduce a second runtime or independent programming system.
- **IMPROVED:** Existing connections, parameters and runtime semantics remain authoritative in the underlying world model.

**QUALITY / TESTING**
- **NEW:** Expanded regression coverage for vectorised distance processing and numerical runtime paths.
- **NEW:** Regression coverage for Prop lifecycle, persistence and I/O behaviour.
- **NEW:** Regression coverage for trigger AABBs and containment behaviour.
- **NEW:** Regression coverage for I/O activator propagation and per-entity I/O enablement.
- **IMPROVED:** Performance benchmarks exercise production runtime paths rather than separate benchmark-only implementations.
- **IMPROVED:** Continued cross-system regression testing of editor, runtime, persistence, BigWorld, rendering, spatial and I/O behaviour.

**DIRECTION**
- **ARCHITECTURE:** Fio is evolving from a traditional level-editor architecture toward a unified real-time world editor and simulation engine.
- **ARCHITECTURE:** The editor and runtime operate on the same live world model with no compile or bake stage.
- **ARCHITECTURE:** World state, entity behaviour and I/O remain directly executable in the editor.
- **PERFORMANCE:** The engine is increasingly designed around transforming world data in bulk rather than repeatedly performing identical work in Python for individual entities.
- **DESIGN:** Fio remains focused on first-person and top-down interactive worlds, including large persistent environments.

---

### v2.4.2.1709

- NEW: I/O now carries the original activator
- NEW: `IO enabled` per-entity - toggles participation in I/O
- NEW: first-class engine primitive: `Prop` - a generic object that can be picked up and carried
- Entity distance culling now does X/Z distance via NumPy batch instead of entity-by-entity in Python
- NEW: `Show AABB` property on triggers displays the actual runtime containment box.
- Improved "Validate connections" tool
- asset browser is now width aware
- improved thing sprites in 2d and 3d views
- property editor explicit groupings


---

### v**2.4.1.1609** | Milestone 9

**LOGIC / I/O**
- **NEW:** `LogicState` — typed persistent world state supporting `string`, `int`, `float`, `bool`, `null` and `uuid` values.
- **NEW:** Parameterised state operations including `SetValue`, `GetValue`, `Compare`, `Exists`, `Missing`, `Increment`, `Decrement`, `Add`, `Subtract`, `Multiply`, `Divide`, `Min`, `Max`, `Clamp`, `Toggle` and value copying.
- **NEW:** State-derived events including value-set, value-changed, value-read, comparison and key-missing/cleared outputs.
- **NEW:** UUID-keyed object-local state.
- **IMPROVED:** I/O parameters can carry values between connected entities, allowing LogicState to act as a reusable state primitive rather than a collection of fixed-purpose counters.
- **IMPROVED:** I/O source/activator propagation through multi-hop event chains.
- **IMPROVED:** Delayed events and timer state are persistent, UUID-addressed and restored correctly.
- **IMPROVED:** Generic I/O writes to `hidden`/`disabled` preserve authored state while entities are BigWorld-dormant.
- **FIXED:** LogicRelay `fire_once` behaviour and reset/re-arm handling.
- **FIXED:** LogicGate input counting, UUID identity and repeated Trigger behaviour.
- **FIXED:** LogicTimer state previously keyed by Python object identity rather than persistent entity UUID.
- **IMPROVED:** Door and mover `Stop` / `Reverse` and motion interruption behaviour.
- **NEW:** Additional Monster inputs including `Sleep`, `SetHealth` and `Respawn`.
- **NEW:** I/O conformance auditing verifies that declared inputs have real implementations and that emitted outputs are actually declared.
- **NEW:** Scene connection validation reports missing targets, unknown inputs and unknown outputs.
- **FIXED:** Multiple previously silent I/O contract failures, including an undeclared `LogicCommand.Trigger` input.

**LOGIC GRAPH**
- **IMPROVED:** Logic Graph now acts as a view/editor over the existing I/O connection model rather than a second programming system.
- **IMPROVED:** Graph Apply no longer destroys connections it cannot represent.
- **IMPROVED:** Connections on unnamed entities and connections to missing targets survive graph round-trips.
- **IMPROVED:** Logic Graph node positions are now persisted correctly.
- **NEW:** Wire labels display connection parameters, delays and `fire_once` state.
- **NEW:** Wire tooltips expose complete connection information.
- **NEW:** Name-addressed legacy connections are visually distinguished from UUID-addressed connections.
- **IMPROVED:** Only connected pins are shown by default, dramatically reducing oversized nodes such as `LogicState`; all pins remain available when expanded or during connection.
- **NEW:** Find, frame-selection, fit and automatic left-to-right graph arrangement tools.
- **IMPROVED:** Graph Apply participates correctly in undo/redo and invalidates the I/O reverse index.
- **FIXED:** Graph wire labels were clipped and therefore invisible.
- **FIXED:** Graph node headers and variable-pin indicators could overlap.
- **FIXED:** Automatic graph layout now accounts for actual node heights rather than assuming a fixed node size.

**PERSISTENCE**
- **NEW:** Comprehensive Logic persistence regression coverage covering typed state, UUID targets, legacy name targets, parameters, delays, `fire_once`, object-local state and graph positions.
- **NEW:** Save/load round-trip validation for complex logic scenes.
- **NEW:** Deterministic round-trip testing at approximately 120 entities and 600 connections.
- **FIXED:** Logic Graph Apply could silently lose connections during a save/edit cycle.
- **FIXED:** Delays and `fire_once` values being at risk of changing type during persistence.
- **FIXED:** Restore-time visibility changes now invalidate the appropriate renderer/collision caches after the restored state has actually been applied.

**BIGWORLD / STREAMING**
- **FIXED:** Restored `hidden` and `disabled` state on dormant entities could previously be overwritten when the entity was unparked.
- **FIXED:** Entities that moved between BigWorld cells could remain registered with their original cell and be parked incorrectly.
- **NEW:** Shared BigWorld configuration schema used by runtime defaults, editor properties and configuration conversion.
- **IMPROVED:** Streaming visibility invalidation distinguishes cheap drawable-set changes from expensive authored collision-state changes.
- **IMPROVED:** BigWorld save/restore handling now preserves authored state independently of temporary streaming state.
- **IMPROVED:** BigWorld tier/cell behaviour and persistence regression coverage.
- **QUALITY:** Added regression guards for plugin lifecycle ownership and previously reported BigWorld architectural failure modes that were verified not to exist in the current tree.

**VIEW DISTANCE / FOG**
- **NEW:** Shared view-distance system controlling camera draw distance, broad-phase visibility, projection limits and runtime logic visibility.
- **NEW:** Distance fog controls including start/end distance, density, colour and ambient contribution.
- **IMPROVED:** Automatic fog placement derives from the configured view distance and ends before the far plane.
- **NEW:** Console/editor controls for view-distance configuration.
- **QUALITY:** Dedicated view-distance and distance-fog regression coverage.

**EDITOR / SCENE**
- **NEW:** Scene Hierarchy search with partial matching by entity name and type.
- **NEW:** Search results select matching objects in the 2D and 3D views.
- **NEW:** Scene Hierarchy **Focus on** command frames selected objects in both views while preserving the 3D camera orientation.
- **NEW:** Tools → Project Overview report.
- **IMPROVED:** Project Overview reports doors as movers, and includes dynamic lights and portals.
- **NEW:** Map creation and last-modified information in Project Overview.
- **IMPROVED:** Asset Browser inspector/add-to-scene layout.
- **IMPROVED:** Logic State property panel removes redundant headings and captions.

**CONSOLE / TOOLS**
- **IMPROVED:** Console command handling and integration with the event-driven I/O system.
- **IMPROVED:** Runtime/editor tooling around state, I/O and world inspection.

**RENDERING / ENGINE**
- **IMPROVED:** Renderer and culling caches are explicitly invalidated when streaming changes the drawable world.
- **IMPROVED:** Authored visibility changes rebuild collision data only when necessary.
- **IMPROVED:** View-distance integration across renderer, culling and terrain systems.
- **IMPROVED:** Distance/fog shader integration.
- **QUALITY:** Expanded renderer regression coverage for distance limits and fog behaviour.

**QUALITY / TESTING**
- **NEW:** Full I/O contract tests exercise declared inputs through the real dispatcher rather than merely comparing registry tables.
- **NEW:** Output declarations are checked against actual emission sites.
- **NEW:** Logic composition tests covering event-driven chains between primitives.
- **NEW:** LogicState and state-value test suites.
- **NEW:** Persistence round-trip tests for complex logic scenes.
- **NEW:** BigWorld cell-membership, configuration, save/restore and tier tests.
- **NEW:** View-distance and distance-fog test suites.
- **NEW:** Regression coverage for plugin lifecycle ownership and restore invalidation.
- **FIXED:** Test isolation leaks involving the process-wide I/O registry and related singletons.
- **QUALITY:** The I/O conformance probe was corrected to supply parameters by **entity type and input**, rather than assuming identically named inputs have identical semantics.
- **QUALITY:** 133 of 176 declared inputs visibly mutate a fresh entity under the conformance probe; the remaining inputs were verified as implemented but have no observable effect on an otherwise pristine test entity.

---

### v**2.4**.0.0 | Milestone 8

**ARCHITECTURE / ENGINE**
- **NEW:** Unified, UUID-aware spatial and world-state infrastructure underpinning editor, runtime, persistence and BigWorld systems.
- **NEW:** BigWorld simulation tiers and persistent cell residency infrastructure integrated into the core engine/plugin boundary.
- **NEW:** Stable UUID-based identity is carried through spatial, I/O, persistence and runtime systems.
- **NEW:** Expanded automated test architecture covering editor, geometry, I/O, logic, persistence, rendering, spatial systems, plugins, threading and undo/redo.
- **IMPROVED:** Editor and runtime state share the same live world model without introducing a compile/bake stage.
- **IMPROVED:** BigWorld remains an opt-in extension while its engine-facing infrastructure stays isolated from ordinary maps.

**EDITOR**
- **IMPROVED:** GtkRadiant-style component editing for faces, edges and vertices.
- **IMPROVED:** Selection, dragging, cloning, splitting and rotation workflows.
- **NEW:** Help → Keys window with dynamically generated keyboard shortcuts.
- **IMPROVED:** Editor tooltips and interaction feedback.
- **IMPROVED:** Default editor layout and workspace behaviour.
- **IMPROVED:** Editor state/history handling and object lifetime safety.
- **NEW:** Expanded regression coverage for component identity, selection modes, view interaction and undo/redo.

**INSPECTORS / UI**
- **IMPROVED:** Property Editor caching and rebuild performance.
- **IMPROVED:** Surface Inspector selection and editing workflow.
- **IMPROVED:** Debug Console lifecycle, sizing and scrolling.
- **IMPROVED:** Dark-theme consistency across editor tools.
- **IMPROVED:** Floating editor-window lifetime and Qt object ownership.
- **IMPROVED:** Settings and workspace management.

**GEOMETRY**
- **IMPROVED:** Convex brush geometry and derived-geometry caching.
- **IMPROVED:** Component editing reliability across clipping, splitting, cloning and undo/redo.
- **IMPROVED:** Geometry invariants and transform handling.
- **FIXED:** Stale component, property and I/O references after undo/redo and object replacement.
- **NEW:** Property-based and invariant-driven geometry regression coverage.

**I/O**
- **IMPROVED:** Target/source lookup with cached reverse indexing.
- **IMPROVED:** UUID-based target identity with name-based fallback for legacy maps.
- **FIXED:** I/O cache invalidation after rename, deletion, duplication and undo/redo.
- **NEW:** Connection validation and I/O regression coverage.

**SPATIAL / BIGWORLD**
- **NEW:** Spatial-grid infrastructure integrated with the engine.
- **NEW:** BigWorld cell streaming and simulation-distance tier model.
- **IMPROVED:** BigWorld runtime/cell management and engine integration.
- **IMPROVED:** Persistent-world handling and streaming-aware entity management.
- **NEW:** BigWorld engine-integration and tier regression tests.

**RENDERING / PHYSICS**
- **IMPROVED:** Renderer state management and culling architecture.
- **IMPROVED:** Brush geometry and spatial queries.
- **IMPROVED:** Renderer line/light limits and rendering-state correctness.
- **IMPROVED:** Physics and AABB handling.
- **NEW:** Expanded rendering, physics and spatial regression suites.

**MONSTER AI**
- **IMPROVED:** Monster AI architecture and integration with the unified runtime.
- **IMPROVED:** AI behaviour, patrol and threading coverage.
- **NEW:** Dedicated regression coverage for monster behaviour and AI-thread lifecycle.

**PERFORMANCE**
- **PERFORMANCE:** Reduced unnecessary geometry reconstruction and Qt/UI work.
- **PERFORMANCE:** Expanded hot-path regression and integrity tests.
- **PERFORMANCE:** Preserved NumPy-based architecture and low-power CPU focus.
- **PERFORMANCE:** Added benchmark coverage for renderer and engine hot paths.

**QUALITY**
- **NEW:** Comprehensive automated regression suite and CI test infrastructure.
- **NEW:** Tests covering editor state, component identity, geometry, I/O, logic dispatch, persistence, plugins, rendering, spatial systems, threading and undo/redo.
- **FIXED:** Multiple cross-system lifecycle, caching and state-consistency defects identified through regression testing.

---

### v**2.3**.0.0709

- **Updated Wiki** (40 pages including technical overviews of [logic thread](https://github.com/ViciousSquid/Fio/wiki/Logic-Thread) and [portal renderer](https://github.com/ViciousSquid/Fio/wiki/Portal-Rendering-%E2%80%90-Technical-Overview))

**ENGINE / PERFORMANCE**
- PERFORMANCE: Added cached brush AABBs, eliminating repeated bounding-box calculations across physics, collision and visibility systems.
- PERFORMANCE: Optimized spatial-grid queries with fast paths for single-cell lookups.
- PERFORMANCE: Inlined ray/AABB intersection in line-of-sight checks, substantially reducing temporary allocations and Python call overhead.
- PERFORMANCE: Replaced per-tick NumPy allocations in door and mover interpolation with scalar arithmetic.
- PERFORMANCE: Added lightweight XZ distance culling for the main camera render path.
- PERFORMANCE: Cached renderer matrix uniform pointers within textured batches.
- PERFORMANCE: Optimized terrain lighting by selecting the nearest lights supported by the terrain shader.
- PERFORMANCE: Reduced property-editor rebuild overhead through deferred Qt updates and lazy plugin-tab construction.

**EDITOR**
- NEW: Collapsible property-editor sections for improved inspector organisation.
- NEW: Advanced property tab for less frequently used/dynamic properties.
- NEW: Editable KeyValue defaults in the property editor.
- NEW: Property grouping support for plugin-defined properties.
- NEW: Generic floating-window infrastructure for editor and diagnostic tools.
- NEW: Plugin entity-wizard hooks for custom entity creation workflows.
- NEW: Singleton entity support for plugin-defined entity types.
- IMPROVED: Entity type lookup can now use an explicit `map_type`, decoupling map serialization from Python class names.
- IMPROVED: Asset browser failures are isolated to individual assets instead of aborting an entire scan.

**PLUGIN SYSTEM**
- NEW: Expanded plugin API with property groups, entity wizards and singleton entity types.
- IMPROVED: Plugin custom-property tabs are constructed lazily, reducing unnecessary editor initialization work.
- IMPROVED: Plugin registration infrastructure now supports additional built-in extension layers while remaining backwards-compatible with existing plugins.

**AUDIO**
- FIXED: Speaker `looping` property was not being honoured by game-view audio playback.
- FIXED: `StopSound` now correctly stops active speaker channels.
- IMPROVED: Per-entity audio channel tracking prevents duplicate looping channels when sounds are retriggered.

**ROBUSTNESS**
- FIXED: Logic-thread exceptions are now logged with full tracebacks instead of silently terminating the update loop.
- FIXED: Terrain shader setup no longer reports misleading failures when invoked before a valid OpenGL context exists.
- FIXED: Terrain lighting no longer attempts to address more light uniforms than are declared by the terrain shader.
- IMPROVED: GLB thumbnail generation reports failures without destabilising the asset browser.
- IMPROVED: Runtime-only renderer caches are excluded from map serialization and undo state.

**RENDERING**
- IMPROVED: Main-camera rendering can discard distant objects before expensive render preparation while leaving lights, portals and shadow/portal source collections unaffected.
- IMPROVED: Reduced repeated uniform-pointer lookup during textured brush rendering.

**QUALITY**
- NEW: Extensive regression and equivalence tests covering AABB caching, physics fast paths, ray intersection, mover interpolation, rendering culling, audio, floating windows and plugin infrastructure.
- FIXED: Multiple edge cases identified through cross-codebase regression testing.

---

### v2.2.0.2408a

* **PERFORMANCE:** Gated some unnecessary lookups in monster vision system
* Backported IO widget from [MiniWind](https://github.com/ViciousSquid/MiniWind)


---

### v2.2.0.2408

* Fixes and improvements:
   * FIXED: Property editor crash with integers >32 bits (backported from `miniwind` branch)
   * Textures can now be applied to angled brush faces via the FACE button


---


### v2.2.0.2108 | MILESTONE 7

* **Everything needed to make & distribute First person, Top-Down and Open-World games**
* **NEW:** [`LogicCommand`](https://github.com/ViciousSquid/Fio/wiki/LogicCommand) — Executes a console command when triggered.
* Added native Play-mode load/save functionality to console and API
* **FIXED:** ad-hoc signing to macOS release build _(Thanks Carson)_

---

### v2.2.0.1908

* UI fixes and performance enhancements
* **NEW:** [`BigWorld`](https://github.com/ViciousSquid/Fio/tree/2.2.0.1908_Latest/plugins/bigworld) Plugin for massive worlds; streams only the area around the player.

---

### v2.2.0.1508

* Improved random level generator
* Property editor now exposes UUIDs and the I/O system can reference them
* **NEW:** `Topdown` camera mode and assets (work in progress)
* **FIXED:** Keyboard nudging was re-serialising the entire scene every keypress
* **PERFORMANCE:** Persistent play-mode cull cache (`_build_cull_cache`). Built once at play-start:
   - _(N,3) NumPy arrays of brush AABB centers and half-sizes_
   - _an object array of per-brush render refs_
   - _the list of mover/door row indices (the only brushes that move)_
   - Per frame, the fast path now does **almost no Python!**

---

### v2.1.0.0 | MILESTONE 6

* **NEW:** **Plugin** API v1.3 provides primitives and extension points ([Documented here](https://github.com/ViciousSquid/Fio/wiki/Plugin-API)) 
* **NEW:** [`Tidy`](https://github.com/ViciousSquid/Fio/tree/2.2.0.1908_Latest/plugins/tidy) Plugin for building "put everything away" games
* **FIXED:** Improved package map discovery and ensured plugin metadata is not treated as playable maps.
* ****I/O System:****
  * **FIXED:** Added missing inputs/outputs in IO manager
  * **FIXED:** Console commands `fire` and `ent_fire` were broken
  * **FIXED:** `levelchanger.OnUse` was broken

---


### v2.0.1.0

* **NEW:** [Fio Player for Android](https://github.com/ViciousSquid/Fio/tree/2.2.0.1908_Latest/player) - (under development)
* No changes to `editor` or `engine`

---

### v2.0.0.0 | MILESTONE 5

* **NEW:** Convex polyhedra brushes
* **NEW:** Scissor (clip) tool - Shortcut `X`
* **NEW:** Per-face surface inspector
* **NEW:** `cig` pickup entity
* **FIXED:** Portal rendering pipeline bug
* **PERFORMANCE:** Hot code paths aggressively optimized across core rendering and spatial subsystems

---

### v1.3.2.0 | MILESTONE 4

* **NEW:** Dynamic lights cast real-time cubemap shadows
* **NEW:** Water volume physics
* **PERFORMANCE:** Improved terrain generation speed via NumPy batching
* **PERFORMANCE:** Portals now utilize frustum culling for significant FPS gains
* **AI:** NPCs now hear nearby sounds and actively investigate source locations
* **FIXED:** OpenGL crash when loading/generating large terrain
* **FIXED:** Linked portal yaw/pitch/roll orientation calculations

---

### v1.3.1.0

* **NEW:** [LogicKeyValueStore](https://github.com/ViciousSquid/Fio/wiki/LogicKeyValueStore) — persists up to 25 key/value pairs across map transitions

**Optimizations:**
* Vectorized brush frustum culling with NumPy (replaced per-brush Python loop)
* Cached collision-brush lists instead of rebuilding every tick
* Precomputed trigger, pickup, level-changer, and monster entity lists
* Cached mover/door brush lists and normalized direction vectors once at load time
* AI now iterates a precomputed monster list/ID-map instead of rescanning all entities
* Batched spatial-grid and monster-ray debug visualizers into a single draw call
* Memoized monster sprite texture-key strings to eliminate per-frame allocations

---

### v1.3.0.0

* **NEW:** [Autocaulk](https://github.com/ViciousSquid/Fio/wiki/Autocaulk) tool for automated face optimization
* **NEW:** Added GLB model support
* **NEW:** Added "Save layout", "Restore layout", and "Reset layout" workspace controls
* **NEW:** Taskbar and Dock icons for packaged Mac/Windows binaries
* **ENGINE:** PyGame integrated for audio spatialization and gamepad support
* **AI:** Monster AI offloaded to a dedicated background thread; increased monster hitboxes
* **UI/UX:**
  * Improved Asset and Model Browsing via `☰` menu
  * Faster Property Editor response times
  * Enhanced [Scene Hierarchy](https://github.com/ViciousSquid/Fio/wiki/Scene-Hierarchy) overview
  * Improved 2D view wireframes with clearer labelling
* **PERFORMANCE:** Optimized renderer batching to reduce draw calls
* **FIXED:** Selecting `<None>` shader type now properly reverts brush properties
* **FIXED:** Issue where the Play button could display an incorrect state
* **FIXED:** CSG subtraction failures on complex geometries

---

### v1.2.6.51 | MILESTONE 3

* **NEW:** [`Fiopak`](https://github.com/ViciousSquid/Fio/wiki/.fiopak-archive) archive import/export system fully operational
* **UI/UX:** Added dedicated Tools menu and enhanced Property Editor performance
* **ASSETS:** Updated Asset Browser with distinct `Maps` and `Packages` views
* **IMPROVEMENTS:** Upgraded OBJ loader and general code refactoring

---

### v1.2.6.31

* **NEW:** [Procedural Liminal map generator](https://github.com/ViciousSquid/Fio/wiki/Procedural-level-generator) in Tools Menu — automatically generates playable maps in one click

---

### v1.2.6.30

* **NEW:** [Split-screen](https://github.com/ViciousSquid/Fio/wiki/Split%E2%80%90Screen) support in Play Mode (`ss` console command or `F9`) — Player 2 maps to arrow keys or connected gamepad

---

### v1.2.6.2

* **FIXED:** Corruption issue when exporting certain `.fiopak` archives
* **ENGINE:** Initial work on split-screen viewport architecture

---

### v1.2.6.1

* **FIXED:** `LogicSpawner` failing to instantiate Monster entities
* **MAPS:** Added `Spawner_Test` map for I/O validation

---

### v1.2.6.0 | MILESTONE 2

* **NEW (BETA):** **World [Portal](https://github.com/ViciousSquid/Fio/wiki/Portal)** entity for seamless non-Euclidean level design
* **NEW:** `.fiopak` archive packaging format and "Play Game Package" launcher
* **NEW:** Fullscreen toggle (`F12` during Play mode)
* **TOOLS:** `tools/fio_to_map.py` exporter converts Fio JSON maps to Quake/Valve 220 format
* **EDITOR:** Arrow key nudging for selected entities (`Shift` for 5x multiplier)
* **EDITOR:** Real-time 2D view camera tracking during Play mode
* **FIXED:** Adreno GPU flickering issues on multi-monitor configurations
* **FIXED:** Mover distances decreasing by 'lip' value on repeated cycles
* **FIXED:** Monster hit registration accuracy

---

### v1.2.5.5 | MILESTONE 1

* **NEW:** [Camera](https://github.com/ViciousSquid/Fio/wiki/Camera) and [Spawner](https://github.com/ViciousSquid/Fio/wiki/Spawner) entities
* **NEW:** Teleporter brush mechanics fully functional
* **MOVERS:** Movers can now interpolate along sequential [PathNodes](https://github.com/ViciousSquid/Fio/wiki/PathNodes)
* **FIXED:** `Preview Movement` toggle for doors and movers

---

### v1.2.5.1 – v1.2.5.3

* **NEW:** Native macOS build and precompiled binary release
* **FIXED:** Qt GUI crashes on macOS *(special thanks to zc1036)*
* **FIXED:** `Monster.get_sprite_path` performing redundant filesystem queries every frame
* **FIXED:** Missing overlay draw calls in Face Mode
* **CLEANUP:** Removed redundant `obj_loader.py` module

---

### v1.2.5.0

* **NEW:** [`PathNode`](https://github.com/ViciousSquid/Fio/wiki/PathNodes) entity for monsters, movers, and teleporters
* **NEW:** [`SpatialGrid`](https://github.com/ViciousSquid/Fio/wiki/Spatial-Grid) partition visualization (`sg` console command)
* **NEW:** Real-time 2D viewport rendering during Play Mode
* **NEW:** `notarget` console command (makes player invisible to AI)
* **ENGINE:** Dedicated `engine/monster_ai.py` logic core

---

### v1.2.4.0

* **NEW:** Monster AI behaviors and console debugging suite
* **NEW:** Dynamic moving light sources (see `maps/dLight_Test.json`)
* **NEW:** Mouse sculpting mode for terrain generation
* **WEAPONS:** Added secondary weapon type (Shotgun)
* **I/O:** Named brushes can now be toggled via I/O events (visibility, collision, color). *Hidden brushes bypass render calls entirely.*
* **FIXED:** Broken I/O import references and glow brush rendering

---

### v1.2.0.0 (Stable)

* **NEW:** Visual [Logic Graph Editor](https://github.com/ViciousSquid/Fio/wiki/Logic-Entities) (`CTRL+L`) and Logic Wizard (`CTRL+SHIFT+W`) with 25 pre-built templates
* **NEW:** Initial Monster entity implementation
* **MAP FORMAT:** Upgraded to Map Format v3 with UUID-based entity identifiers (preserves I/O connections through renames)
* **TERRAIN:** Heightmap loading and mouse sculpting support
* **FIXED:** YZ viewport dragging direction flipped
* **FIXED:** Frame-pacing race condition causing visual flickering

---

### v1.1.0.0

* **NEW:** [`LevelChanger`](https://github.com/ViciousSquid/Fio/wiki/LevelChanger) entity for inter-map transitions
* **ENGINE:** Refactored level loading pipeline utilizing Qt signals for safe state changes
* **CONSOLE:** Expanded rendering engine console commands
* **FIXED:** Edge cases in [Logic Gate](https://github.com/ViciousSquid/Fio/wiki/Logic-Entities) evaluation

---

### v1.0.17.0

* **SHADERS:** Implemented explicit mixed-precision shader qualifiers (`highp` for vertex positions, `mediump` for lighting and color calculations)
* **CONSOLE:** Added interactive `help` command and expanded runtime triggers

---

### v1.0.16.0

* **NEW:** In-engine [Debug Console](https://github.com/ViciousSquid/Fio/wiki/Debug-console) (toggle with `~`)
* **ARCHITECTURE:** Refactored and summarized module structure across `/editor` and `/engine` directories

---

### v1.0.15.0

* **SHADERS:** Shader optimizations in preparation for deferred rendering pipeline
* **DISPLAY:** High-DPI display scaling support

---

### v1.0.14.0

* **PERFORMANCE:** CPU path optimizations specifically targeted for ARM processors
* **SHADERS:** Upgraded shaders to `#version 330 core`

---

### v1.0.13.0

* **NEW:** Source/Half-Life 2 style Entity I/O event system with real-time debugger
* **NEW:** Logic Entities (timers, gates, and switches)
* **NEW:** Per-face texture application
* **SYSTEM:** Auto-saving and recent file history

---

### v1.0.12.5

* **TERRAIN:** Expanded procedural terrain generation limits by 16x
* **VISUALS:** Enhanced untextured lo-fi geometry rendering modes

---

### v1.0.12.0 | *Verdant_Meadow*

* **NEW:** Terrain generator tool and UI
* **ENGINE:** Expanded core rendering pipeline

---

### v1.0.11.0 | *Delicious-Toast*

* **NEW:** Initial non-beta public release
* **UI:** Added Toast notification system to the editor UI

---

> "Fio was born from a simple frustration: waiting hours for BSP, VIS and RAD just to test a level. Every design decision follows one principle—edit, press Play, and iterate instantly."