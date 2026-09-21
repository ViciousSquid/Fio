# `editor/`

Fio's event-driven world model — how `LogicState`, `LogicRelay`, `LogicGate`,
`LogicTimer` and the I/O system compose into gameplay without a scripting
language — is documented in **[`LOGIC.md`](LOGIC.md)**, along with the 2.4
architectural review and the comparative notes on TrenchBroom and Source.

### `__init__.py`
Package initialiser. Bootstraps the plugin system before any map is loaded or the main window is built, so plugin-provided entity types, I/O definitions, and editor integrations are available everywhere.

### `asset_browser.py`
Texture and model browser with live-rendered thumbnails (OBJ wireframe and GLB previews), FIT / TILE / FACE texture actions, and drag-and-drop support.

### `component_edit.py`
The shared object/face/edge/vertex model behind Radiant-style direct editing — the single place Fio answers what is under the cursor, which brush side a press is asking to stretch, how dragging a component changes the geometry, and which brushes an area selection catches. `ComponentRef` gives a component an identity that survives a geometry rebuild (a face by its plane index, a vertex or edge by quantised position, each qualified by its brush); `ComponentController` holds the mode, hover, selection and in-flight drag that the 2D views and the 3D viewport both drive, including the press policy so a click means the same thing in either. `PlaneDrag` slides whole face planes and `PointDrag` moves corners and re-derives the plane set from their hull, both snapshotting at mouse-down and recomputing from the total delta so there is one rebuild per mouse move and no accumulated drift. Deliberately Qt-free (NumPy only) so the interaction rules can be tested headlessly; nothing here is called from a render loop, and overlay geometry is cached behind a version counter.

### `console_commands.py`
Debug console command handler. Implements built-in commands (noclip, map, fps, clear, `cam`, etc.) dispatched by the debug console. `cam [overhead|fp] [seconds]` smoothly tweens the play-mode camera between overhead and first person (default 1s; `cam 2` for 2s, `cam 0` for instant). `save`/`load` (with `quicksave`/`qs` and `quickload`/`ql` for the quicksave slot, and `saves` to list them) serialize and restore a play session to `saves/*.fiosave` via the engine's native `savegame` module — `save` requires Play Mode, while `load` from the editor reloads the save's map and enters Play Mode before applying the saved state. Console commands can also be driven from map logic via the `LogicCommand` entity's `RunCommand` input.

### `debug_console.py`
Quake-style drop-down debug console with category filtering, entity-name hyperlinks, I/O event tracing, adjustable font size, command history, and a singleton logger (`debug_log`) used throughout the codebase.

### `editor_state.py`
Central editor model. Stores brushes, things, terrain data, the selection, undo/redo history, and handles serialisation (JSON v2 with I/O connections), lightmap dirty tracking, and legacy format migration. Undo and redo *replace* every brush dict and `Thing` rather than editing them, so the selection is re-resolved by stable id on every restore — both `selected_object` and the `selected_objects` list the rest of the editor acts on. Tools checkpoint at the *start* of a gesture, so `undo()` snapshots the live scene onto the redo stack rather than the checkpoint it pops; `discard_last_checkpoint()` is how a gesture that turned out to change nothing drops its checkpoint without also destroying the redo branch `save_state()` cleared.

### `face_texture.py`
Reading and writing one brush face's texture transform, wherever that face happens to keep it: a tagged box side stores its mapping in the brush's per-tag dicts (because the renderer draws those from the shared cube VAO), while a cut face on an angled plane set stores it on the plane itself. Presents both as one thing, so the Surface Inspector and anything else that touches a face's mapping need not care which kind it is. Writes through this module invalidate the brush's derived geometry, since a face winding carries its texture, UV scale and texture basis as well as its shape. Qt-free, so the behaviour is testable without a window.

### `io_editor_widget.py`
"Output Connections" panel (Hammer-style) for adding and editing entity I/O connections — target entity, input name, parameter, delay, and fire-once flag.

### `io_handlers.py`
Registers all input handlers for the I/O system. Defines what happens when an input is called on an entity (e.g. `TurnOn`, `Open`, `Kill`, `SetBrightness`) for every entity type — lights, doors, movers, monsters, triggers, speakers, pickups, logic entities, and more. `LogicState`'s operations are parameterised (`Increment  killed,1`) rather than each having their own input, so the set stays small and a map can express something the engine was never told about.

### `state_values.py`
Fio's state type system, and nothing else: `string`, `int`, `float`, `bool`, `null` and `uuid`, with the parsing that types an I/O parameter, the formatting that puts a value back on the wire, comparison, arithmetic and deterministic store serialisation. Imports nothing — no Qt, no engine, no entity model — so what a stored value *means* is testable on a bare Python install. Legacy string-only stores are never rewritten on load, because comparison and arithmetic already understand them.

### `io_system.py`
Core I/O framework inspired by Half-Life 2's Hammer Editor. Defines `IODef` (input/output definitions), the `IO_REGISTRY` (per-entity-type I/O schema), `OutputConnection` (target + input + delay + parameter), and `IOManager` (runtime dispatcher with delayed firing and fire-once tracking). Also holds the reverse lookup — "what points at this entity?" — as a cached index rebuilt only when a revision counter moves. The index files source *entities*, never their names, and `find_targeting_sources` reads names off the live objects at lookup time, so a rename (which invalidates no connection) can never leave it quoting a name nothing in the scene answers to. Also validates a map's connections (`validate_scene_connections`), reporting a target that is not there, an input the target's type does not accept, and an output the source does not have — the checking lives here, next to the dispatcher whose resolution rules it has to agree with, rather than being reimplemented by each caller. `audit_io_coverage` reconciles the *declaration* (this registry) against the *implementation* (an `IOManager`'s handler table): the two are necessarily separate structures with different lifetimes, so the only way to know a declared input still runs something is to ask. `ABSTRACT_IO` is the one sanctioned exception list, and `tests/io/test_io_contract.py` asserts zero drift — by invoking every declared input for real, not just by comparing tables.

### `logic_graph_widget.py`
Visual node-graph editor for entity I/O connections. Each named entity becomes a node with output pins (right, orange) and input pins (left, blue); drag-connecting pins creates wiring. Existing `_io_connections` are drawn on open. Supports right-click to delete or edit connection delay/parameter, and Apply (Ctrl+S) to write changes back.

### `logic_wizard.py`
Guided QWizard for wiring up common I/O scenarios without touching the raw connection editor. Provides ~26 built-in scenarios across four categories: Monster Encounters, Doors & Movers, Environment & Audio, and Complex Logic.

### `main_window.py`
Main editor window. Docks all UI panels (2D view, 3D view, property editor, scene hierarchy, asset browser, debug console), builds the menu bar and toolbar, manages play-mode toggling, and displays toast notifications. Owns the shared `ComponentController` both viewports drive, the component-mode switch (object / face / edge / vertex), Radiant's area-selection operations (touching, inside, partial and complete tall), and the clip/split tool. After an undo or redo it re-points everything that holds an object reference — component handles, the Surface Inspector's bound face, the property editor's cached pages and the I/O reverse index — since a history step replaces the objects themselves.

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
Per-object property panel. Displays and edits position, size, texture/shader, colour, I/O connections, and type-specific properties for the currently selected brush or entity. Built pages are parked in an LRU cache and handed back when the same object is reselected unchanged; the cache key is a signature of everything the panel would read, deliberately excluding position, geometry and the geometry layer's runtime bookkeeping so a drag, rotate or clip costs no rebuild. The I/O revision is folded in, because the "Targeted by" line depends on other entities rather than on this one — which is also why renaming an entity bumps that revision.

### `scene_hierarchy.py`
Tree-view widget listing all brushes and entities in the scene. Supports sorting (default, alphabetical, by type), selection synchronisation with the viewport, and context-menu actions.

### `SettingsWindow.py`
Application settings dialog with tabbed pages for Editor (autosave, grid), Display (resolution, render settings), Play Modes, Controls (mouse sensitivity), Keyboard (rebindable shortcuts), and Split Screen configuration. Includes Apply & Restart. The Renderer section reports what hardware was detected and exposes Low-power Mode (the former `arm_mode`, still read from the old settings key) and dynamic shadows, both defaulting from `engine.shaders.detect_low_power_arm()` rather than a second copy of the probe.

### `shortcuts.py`
Gathers every keyboard shortcut the editor answers to, from the three different places Fio binds keys: QAction and QPushButton shortcuts read live off the menus and toolbar, the user's editable bindings in the config file's `[Shortcuts]` and `[KeyBindings]` sections, and the `keyPressEvent` chains in the main window and the views, which nothing can enumerate and so are declared in `DECLARED`. Discovered entries win over declared ones, so a binding that becomes a real QAction stops being described by a stale declaration.

### `shortcuts_window.py`
Help > Keys: a resizable, scrollable, searchable window listing every shortcut, built from `editor.shortcuts` so nothing is written down twice. A real top-level window rather than a message box, so it can stay open beside the editor while a key is looked up.

### `surface_inspector.py`
Radiant-style floating Surface Inspector for tuning per-face texture mapping in Face mode. Edits horizontal/vertical shift, horizontal/vertical stretch, and rotation for the selected brush face, with configurable step increments and grid-snap, scoped to one face or every face of the selection. A run of spin-box clicks coalesces into a single undo step. It binds to a `(brush, face key)` pair, so the main window re-points it at the live brush after an undo or redo rather than letting it edit an object that is no longer in the scene.

### `terrain_editor.py`
Dedicated terrain parameter editor panel for configuring terrain chunk settings (noise seed, scale, amplitude, texturing).

### `things.py`
Entity class definitions for all placeable Things: `PlayerStart`, `Light`, `Model`, `Speaker`, `Pickup`, `Monster`, `PathNode`, `Portal`, `LevelChanger`, `LogicGate`, `LogicRelay`, `LogicTimer`, `LogicCommand`, `LogicSpawner`, `LogicCamera`, `LogicState`, and the `TriggerBrush` mixin. Each class defines default properties and I/O registrations. `LogicCommand` runs a console command (from its connection parameter or `command` property) when its `RunCommand` input fires — e.g. a trigger brush wired to run `cam 2`. `LogicState` is Fio's persistent state primitive: typed named values, read, compared and mutated through I/O, persisting across level transitions and shared with plugins through one registry. A class may declare `map_type` when its serialised token differs from its class name.

### `tooltips.py`
Showing and hiding a panel's tooltips (Settings > Editor > Tooltips). Qt keeps a tooltip on the widget it belongs to, so switching them off means taking the text away and being able to give it back; the original is parked in a Qt dynamic property that travels with the widget, so a panel rebuilt around a stashed widget still knows what its tooltip said. Per-area rather than global, because toolbar tooltips stop being wanted long before the Property Editor's do.

### `ui.py`
Shared UI helper widgets and utilities used across the editor (common dialogs, styled components, layout helpers).

### `view_2d.py`
Orthographic 2D top-down, front and side editor views. Provides brush drawing, selection, moving, resizing, entity placement, grid snapping, multi-select and marquee selection, free rotation, clone-and-place, and the clip tool's cut line. In component mode the same press-and-drag gesture grabs a face, edge or vertex, and in object mode dragging near a brush side stretches it directly — both through the shared `component_edit` model, so this file owns only the screen-to-world mapping, the cursor and the undo checkpoint.
