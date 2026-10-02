# `editor/`

### `__init__.py`
Package initialiser. Bootstraps the plugin system before any map is loaded or the main window is built, so plugin-provided entity types, I/O definitions, and editor integrations are available everywhere.

### `asset_browser.py`
Texture and model browser with live-rendered thumbnails (OBJ wireframe and GLB previews), FIT / TILE / FACE texture actions, and drag-and-drop support.

### `console_commands.py`
Debug console command handler. Implements built-in commands (noclip, map, fps, clear, `cam`, etc.) dispatched by the debug console. `cam [overhead|fp] [seconds]` smoothly tweens the play-mode camera between overhead and first person (default 1s; `cam 2` for 2s, `cam 0` for instant). `save`/`load` (with `quicksave`/`qs` and `quickload`/`ql` for the quicksave slot, and `saves` to list them) serialize and restore a play session to `saves/*.fiosave` via the engine's native `savegame` module — `save` requires Play Mode, while `load` from the editor reloads the save's map and enters Play Mode before applying the saved state. Console commands can also be driven from map logic via the `LogicCommand` entity's `RunCommand` input.

### `debug_console.py`
Quake-style drop-down debug console with category filtering, entity-name hyperlinks, I/O event tracing, adjustable font size, command history, and a singleton logger (`debug_log`) used throughout the codebase.

### `editor_state.py`
Central editor model. Stores brushes, things, terrain data, undo/redo history, and handles serialisation (JSON v2 with I/O connections), lightmap dirty tracking, and legacy format migration.

### `io_editor_widget.py`
"Output Connections" panel (Hammer-style) for adding and editing entity I/O connections — target entity, input name, parameter, delay, and fire-once flag.

### `io_handlers.py`
Registers all input handlers for the I/O system. Defines what happens when an input is called on an entity (e.g. `TurnOn`, `Open`, `Kill`, `SetBrightness`) for every entity type — lights, doors, movers, monsters, triggers, speakers, pickups, logic entities, and more.

### `io_system.py`
Core I/O framework inspired by Half-Life 2's Hammer Editor. Defines `IODef` (input/output definitions), the `IO_REGISTRY` (per-entity-type I/O schema), `OutputConnection` (target + input + delay + parameter), and `IOManager` (runtime dispatcher with delayed firing and fire-once tracking).

### `logic_graph_widget.py`
Visual node-graph editor for entity I/O connections. Each named entity becomes a node with output pins (right, orange) and input pins (left, blue); drag-connecting pins creates wiring. Existing `_io_connections` are drawn on open. Supports right-click to delete or edit connection delay/parameter, and Apply (Ctrl+S) to write changes back.

### `logic_wizard.py`
Guided QWizard for wiring up common I/O scenarios without touching the raw connection editor. Provides ~26 built-in scenarios across four categories: Monster Encounters, Doors & Movers, Environment & Audio, and Complex Logic.

### `main_window.py`
Main editor window. Docks all UI panels (2D view, 3D view, property editor, scene hierarchy, asset browser, debug console), builds the menu bar and toolbar, manages play-mode toggling, and displays toast notifications.

### `monster_customise_dialog.py`
Dialog for assigning custom PNG sprites (idle, shoot, dead frames) and 3D billboard size to an individual Monster entity. Sprite paths are stored relative to the project root under `assets/sprites/monsters/`.

### `package_dialog.py`
Package export metadata dialog. Collects title, author, version, description, banner image, and shows a dependency preview before exporting a `.fiopak` archive.

### `package_exporter.py`
`.fiopak` assembler. Performs recursive map dependency resolution, crawls referenced assets (textures, models, sounds), and generates a ZIP archive with a JSON manifest.

### `procedural_generator.py`
Procedural map generation UI widget and core generation logic. Builds fully-playable liminal maps with configurable room count, size, wall/floor textures, monster/health spawns, multi-floor support with stairs, and corridor connectivity via A* pathfinding.

### `procedural_map_gen.py`
Command-line interface for procedural map generation. Wraps the core generation logic from `procedural_generator.py` and outputs map data as a JSON file, with arguments for size presets, room count, seed, monster count, and floor options.

### `property_editor.py`
Per-object property panel. Displays and edits position, size, texture/shader, colour, I/O connections, and type-specific properties for the currently selected brush or entity.

### `scene_hierarchy.py`
Tree-view widget listing all brushes and entities in the scene. Supports sorting (default, alphabetical, by type), selection synchronisation with the viewport, and context-menu actions.

### `SettingsWindow.py`
Application settings dialog with tabbed pages for Editor (autosave, grid), Display (resolution, render settings), Play Modes, Controls (mouse sensitivity), Keyboard (rebindable shortcuts), and Split Screen configuration. Includes Apply & Restart.

### `surface_inspector.py`
Radiant-style floating Surface Inspector for tuning per-face texture mapping in Face mode. Edits horizontal/vertical shift, horizontal/vertical stretch, and rotation for the selected brush face, with configurable step increments and grid-snap.

### `terrain_editor.py`
Dedicated terrain parameter editor panel for configuring terrain chunk settings (noise seed, scale, amplitude, texturing).

### `things.py`
Entity class definitions for all placeable Things: `PlayerStart`, `Light`, `Model`, `Speaker`, `Pickup`, `Monster`, `PathNode`, `Portal`, `LevelChanger`, `LogicGate`, `LogicRelay`, `LogicTimer`, `LogicCommand`, `LogicSpawner`, `LogicCamera`, and the `TriggerBrush` mixin. Each class defines default properties and I/O registrations. `LogicCommand` runs a console command (from its connection parameter or `command` property) when its `RunCommand` input fires — e.g. a trigger brush wired to run `cam 2`.

### `ui.py`
Shared UI helper widgets and utilities used across the editor (common dialogs, styled components, layout helpers).

### `version.txt`
Editor version string (currently `2.2.0.1908`).

### `view_2d.py`
Orthographic 2D top-down and side editor view. Provides brush drawing, selection, moving, resizing, entity placement, grid snapping, and multi-select.
