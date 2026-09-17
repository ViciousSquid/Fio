# `editor/`

Fio's event-driven world model — how `LogicState`, `LogicRelay`, `LogicGate`,
`LogicTimer` and the I/O system compose into gameplay without a scripting
language — is documented in **[`LOGIC.md`](LOGIC.md)**

### `__init__.py`
Package initialiser. Bootstraps the plugin system before any map is loaded or the main window is built, so plugin-provided entity types, I/O definitions, and editor integrations are available everywhere.

### `asset_browser.py`
Texture and model browser with live-rendered thumbnails (OBJ wireframe and GLB previews), FIT / TILE / FACE texture actions, and drag-and-drop support.

### `component_edit.py`
The shared object/face/edge/vertex model behind Radiant-style direct editing — the single place Fio answers what is under the cursor, which brush side a press is asking to stretch, how dragging a component changes the geometry, and which brushes an area selection catches. `ComponentRef` gives a component an identity that survives a geometry rebuild (a face by its plane index, a vertex or edge by quantised position, each qualified by its brush); `ComponentController` holds the mode, hover, selection and in-flight drag that the 2D views and the 3D viewport both drive, including the press policy so a click means the same thing in either. `PlaneDrag` slides whole face planes and `PointDrag` moves corners and re-derives the plane set from their hull, both snapshotting at mouse-down and recomputing from the total delta so there is one rebuild per mouse move and no accumulated drift. Deliberately Qt-free (NumPy only) so the interaction rules can be tested headlessly; nothing here is called from a render loop, and overlay geometry is cached behind a version counter.

### `console_commands.py`
Debug console command handler. Implements built-in commands (noclip, map, fps, clear, `cam`, etc.) dispatched by the debug console.

### `debug_console.py`
Quake-style drop-down debug console with category filtering, entity-name hyperlinks, I/O event tracing, adjustable font size, command history, and a singleton logger (`debug_log`) used throughout the codebase.

### `editor_state.py`
Central editor model. Stores brushes, things, terrain data, the selection, undo/redo history, and handles serialisation

### `face_texture.py`
Reading and writing one brush face's texture transform, wherever that face happens to keep it: a tagged box side stores its mapping in the brush's per-tag dicts (because the renderer draws those from the shared cube VAO), while a cut face on an angled plane set stores it on the plane itself. Presents both as one thing, so the Surface Inspector and anything else that touches a face's mapping need not care which kind it is.

### `io_editor_widget.py`
"Output Connections" panel (Hammer-style) for adding and editing entity I/O connections — target entity, input name, parameter, delay, and fire-once flag.

### `io_handlers.py`
Registers all input handlers for the I/O system. Defines what happens when an input is called on an entity (e.g. `TurnOn`, `Open`, `Kill`, `SetBrightness`)

### `state_values.py`
Fio's state type system, and nothing else: `string`, `int`, `float`, `bool`, `null` and `uuid`, with the parsing that types an I/O parameter, the formatting that puts a value back on the wire, comparison, arithmetic and deterministic store serialisation. Imports nothing — no Qt, no engine, no entity model — so what a stored value *means* is testable on a bare Python install. Legacy string-only stores are never rewritten on load, because comparison and arithmetic already understand them.

### `io_system.py`
Core I/O framework inspired by Half-Life 2's Hammer Editor. Defines `IODef` (input/output definitions), the `IO_REGISTRY` (per-entity-type I/O schema), `OutputConnection` (target + input + delay + parameter), and `IOManager` (runtime dispatcher with delayed firing and fire-once tracking). Also holds the reverse lookup — "what points at this entity?" — as a cached index rebuilt only when a revision counter moves. 

### `logic_graph_widget.py`
Visual node-graph editor for entity I/O connections. 

### `logic_wizard.py`
Guided QWizard for wiring up common I/O scenarios without touching the raw connection editor. Provides ~26 built-in scenarios.

### `main_window.py`
Main editor window. Docks all UI panels (2D view, 3D view, property editor, scene hierarchy, asset browser, debug console), builds the menu bar and toolbar, manages play-mode toggling, and displays toast notifications. Owns the shared `ComponentController` both viewports drive, the component-mode switch (object / face / edge / vertex), Radiant's area-selection operations (touching, inside, partial and complete tall), and the clip/split tool. After an undo or redo it re-points everything that holds an object reference — component handles, the Surface Inspector's bound face, the property editor's cached pages and the I/O reverse index — since a history step replaces the objects themselves.

### `monster_customise_dialog.py`
Dialog for assigning custom PNG sprites (idle, shoot, dead frames) and 3D billboard size to an individual Monster entity. Sprite paths are stored relative to the project root under `assets/sprites/monsters/`.

### `package_dialog.py`
Package export metadata dialog. Collects title, author, version, description, banner image, and shows a dependency preview before exporting a `.fiopak` archive.

### `package_exporter.py`
`.fiopak` assembler. Performs recursive map dependency resolution, crawls referenced assets (textures, models, sounds), and generates a ZIP archive with a JSON manifest.

### `procedural_generator.py`
Procedural map generation with via A* pathfinding.

### `procedural_map_gen.py`
Command-line interface for procedural map generation. Wraps the core generation logic from `procedural_generator.py` and outputs map data as a JSON file

### `property_editor.py`
Per-object property panel. Displays and edits position, size, texture/shader, colour, I/O connections, and type-specific properties for the currently selected brush or entity. Built pages are parked in an LRU cache and handed back when the same object is reselected unchanged; the cache key is a signature of everything the panel would read, deliberately excluding position, geometry and the geometry layer's runtime bookkeeping so a drag, rotate or clip costs no rebuild. 

### `scene_hierarchy.py`
Tree-view widget listing all brushes and entities in the scene. 

### `SettingsWindow.py`
Application settings dialog

### `shortcuts.py`
Gathers every keyboard shortcut the editor answers to

### `shortcuts_window.py`
Help > Keys: a resizable, scrollable, searchable window listing every shortcut, built from `editor.shortcuts` so nothing is written down twice. 

### `surface_inspector.py`
Radiant-style floating Surface Inspector for tuning per-face texture mapping in Face mode. 

### `terrain_editor.py`
Dedicated terrain parameter editor panel for configuring terrain chunk settings (noise seed, scale, amplitude, texturing).

### `things.py`
Entity class definitions for all placeable entities ("things"). Each class defines default properties and I/O registrations.

### `tooltips.py`
Showing and hiding a panel's tooltips (Settings > Editor > Tooltips). Qt keeps a tooltip on the widget it belongs to, so switching them off means taking the text away and being able to give it back; the original is parked in a Qt dynamic property that travels with the widget, so a panel rebuilt around a stashed widget still knows what its tooltip said. 

### `ui.py`
Shared UI helper widgets and utilities used across the editor (common dialogs, styled components, layout helpers).

### `view_2d.py`
Orthographic 2D top-down, front and side editor views. 
