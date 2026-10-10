# `engine/`

| File | What it is |
| --- | --- |
| `__init__.py` | Package marker. |
| `actor_pick.py` | Finds the actor under a screen ray, using the published tables. |
| `animated_texture.py` | Decodes animated GIF textures and picks the frame for a play time. |
| `brush_geometry.py` | Convex brush geometry from planes: faces, collision meshes, 2D outlines, bounds, component edits. |
| `camera.py` | Camera state and view/projection matrices. |
| `change_journal.py` | Records entity changes so the dense tables refresh only changed rows. |
| `constants.py` | Shared engine constants: sizes, render modes, physics and movement tuning. |
| `cutscene_runtime.py` | Cutscene playback: camera and actor tracks, timed events, world restore. |
| `effect_entity.py` | The Effect entity (FIRE, ORB, EXPLOSION, CUSTOM). |
| `effect_table.py` | Dense runtime state for Effects. |
| `entity_table.py` | Dense table of entities for rendering. |
| `fileio.py` | Atomic file writes for maps, autosaves and saves. |
| `floating_windows.py` | Draggable, collapsible overlay windows drawn in the 3D view (SysMon style). |
| `glasses.py` | Player glasses styles and their sprite paths. |
| `glb_loader.py` | GLB/glTF model loader. |
| `items.py` | Item definitions for the four item slots (`gun1`, `gun2`, `custom1`, `custom2`). |
| `level_validation.py` | Checks the structure of maps and save games before loading them. |
| `logic_camera.py` | LogicThread runtime: editor and play camera, overhead view, frustum. |
| `logic_collision.py` | LogicThread runtime: brush and model collision data. |
| `logic_combat.py` | LogicThread runtime: shooting, projectiles, bullet marks, noise events. |
| `logic_editor.py` | LogicThread runtime: editor-mode camera input. |
| `logic_interaction.py` | LogicThread runtime: door and LevelChanger use checks and prompts. |
| `logic_movers.py` | LogicThread runtime: movers and doors. |
| `logic_parenting.py` | LogicThread runtime: lights and portals that follow movers. |
| `logic_player.py` | LogicThread runtime: player 1 and player 2 movement and input, water sounds. |
| `logic_portals.py` | LogicThread runtime: portal fade and transit. |
| `logic_render.py` | LogicThread runtime: publishes the render tables each tick, minus filtered/visgroup/cordon rows. |
| `logic_session.py` | LogicThread runtime: play start/stop, save/load, world pause. |
| `logic_thread.py` | The fixed-timestep simulation loop that runs the runtimes in order. |
| `logic_timing.py` | LogicThread runtime: logic timers and light fades. |
| `logic_triggers.py` | LogicThread runtime: triggers. |
| `logic_world.py` | LogicThread runtime: entity lookup indexes for a play session. |
| `monster_ai.py` | Monster behaviour: sight, movement, attacks, death, pathfinding. |
| `monster_constants.py` | Monster tuning and sprite constants. |
| `monster_table.py` | Dense per-monster data for MonsterAI. |
| `mover_table.py` | Dense mover and door motion data. |
| `obj_loader.py` | OBJ/MTL model loader; turns Z-up models upright. |
| `overhead_sprite.py` | Player sprite for the overhead camera mode. |
| `pause_menu.py` | Pause menu pages, items and cursor (no Qt). |
| `pause_menu_window.py` | The pause menu's floating window. |
| `physics.py` | Collision, spatial grid and dynamic-body physics; holds the world gravity. |
| `player.py` | Player movement and collision (Quake 2 movement rules). |
| `player_starts.py` | Picks the PlayerStart the player spawns at. |
| `portal_transform.py` | Portal point/direction transforms. |
| `projectile_table.py` | Dense storage for live monster projectiles. |
| `prop_entity.py` | The Prop entity (model or billboard; carryable, collectable). |
| `prop_runtime.py` | Prop carrying, collecting, dropping and respawning. |
| `qt_game_view.py` | The 3D view widget: drawing, input, Play mode, HUD, pause menu, audio. |
| `render_cull.py` | Distance culling over dense positions. |
| `render_keys.py` | Packs draw state into sortable keys and finds equal-key runs. |
| `render_table.py` | Dense table of brushes for rendering. |
| `savegame.py` | Play-session save and load (`.fiosave`). |
| `shaders.py` | Shader sources and light limits. |
| `soa.py` | Growth helpers for the dense arrays. |
| `spatial.py` | World cell maths and the cell index. |
| `speaker_audio.py` | Rules for which files a Speaker may play (`assets/sounds`, `assets/music`). |
| `sprite_layers.py` | Entity sprites as layers of one texture array. |
| `sysmon.py` | The system monitor overlay. |
| `terrain.py` | Terrain generation, meshes, drawing and height queries. |
| `terrain_style.py` | Terrain appearance options. |
| `terrain_table.py` | Dense terrain chunk data. |
| `textures.py` | A small OpenGL texture cache; not used by Fio. |
| `threaded_game_state.py` | Hands state between the logic thread and the 3D view. |
| `view_distance.py` | View distance and distance fog settings. |
| `view_filters.py` | What the editor hides: filter groups, user visgroups, the cordon. |

## `renderer/`

| File | What it is |
| --- | --- |
| `__init__.py` | The renderer package's public names. |
| `api.py` | The `Renderer` protocol and its data types. |
| `registry.py` | Renderer names to factories. |
| `core/__init__.py` | Exports `RendererCore`. |
| `core/base.py` | `RendererCore`, optional shared renderer infrastructure. |
| `core/diagnostics.py` | Pass timing. |
| `core/geometry.py` | Shared GPU geometry (cube, sprite quad, angled-brush meshes). |
| `core/overlays.py` | Editor and debug overlays. |
| `core/resources.py` | Texture, model, effect-frame and shader loading; animates GIF textures. |
| `core/tables.py` | Lookups into the render and entity tables. |
| `core/visibility.py` | Culling, pass classification, light selection, portal cameras. |
| `forward/__init__.py` | Exports `ForwardRenderer`. |
| `forward/distance.py` | The Wireframe and Points display modes. |
| `forward/instancing.py` | Instance buffers. |
| `forward/lighting.py` | Lights and shadow maps. |
| `forward/passes.py` | Draw passes. |
| `forward/portals.py` | Stencil portals. |
| `forward/renderer.py` | `ForwardRenderer`, the built-in renderer. |
| `forward/shaders.py` | The forward renderer's shaders. |
