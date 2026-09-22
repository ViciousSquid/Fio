# `editor/`

Fio's editor is the authoring side of the same executable-world system as the engine. It edits the authoritative scene/entity representation directly; there is no separate compile or bake stage between editing and play.

The editor's gameplay model is based on entity I/O and declarative logic primitives rather than an embedded scripting language. `LogicState`, `LogicRelay`, `LogicGate`, `LogicTimer` and the rest of the I/O system compose into executable map behaviour.

The event-driven world model is documented in **[`LOGIC.md`](LOGIC.md)**.

---

### `__init__.py`
Package initialiser. Bootstraps plugins before maps or the main window are built so plugin-provided entity types, I/O definitions and editor integrations are available throughout the application.

### `asset_browser.py`
Texture and model browser with live thumbnails, OBJ/GLB previews, FIT / TILE / FACE texture actions and drag-and-drop support.

### `component_edit.py`
Shared Radiant-style component editing model for object, face, edge and vertex editing. Defines component identity, hover/selection state, press policy and drag operations used by both the 2D views and 3D viewport.

`PlaneDrag` moves whole face planes; `PointDrag` moves vertices and re-derives the brush plane set. Edits are validated before committing and are based on the total gesture delta, avoiding accumulated drag drift. The module is deliberately Qt-free and uses NumPy for geometry operations.

### `console_commands.py`
Debug console command handler for commands such as `noclip`, `map`, `fps` and `cam`. Also provides save/load, quicksave/quickload and save-list commands and allows `LogicCommand` map entities to invoke console commands through I/O.

### `debug_console.py`
Quake-style drop-down debug console with category filtering, entity-name links, I/O tracing, font controls, command history and the shared `debug_log` logger.

### `editor_state.py`
Central editor model. Owns brushes, Things, terrain data, selection, undo/redo and scene serialisation.

Undo/redo replaces scene objects rather than mutating historical objects in place, so references held by the UI are re-resolved by stable identity after a history operation. Gesture tools checkpoint at mouse-down and preserve the redo branch correctly when a gesture is discarded.

### `face_texture.py`
Unified face-texture mapping access for box sides and angled brush planes. Reads/writes texture shift, scale and rotation through one interface and invalidates derived brush geometry when a mapping changes.

### `io_editor_widget.py`
Hammer-style Output Connections panel for creating and editing entity I/O connections: target, input, parameter, delay and fire-once behaviour.

### `io_handlers.py`
Registers the runtime input handlers used by the I/O system. Covers gameplay entities, movers, lights, monsters, triggers, pickups, speakers and logic entities.

`LogicState` operations are parameterised (for example, `Increment killed,1`) rather than requiring a separate input for every possible state mutation. This keeps the I/O vocabulary small while allowing maps to express new combinations of behaviour.

### `state_values.py`
Fio's typed state-value system: `string`, `int`, `float`, `bool`, `null` and `uuid`. Handles parsing, formatting, comparison, arithmetic and deterministic serialisation without importing Qt, the engine or the entity model.

### `io_system.py`
Core I/O framework. Defines input/output declarations, the per-entity I/O registry, output connections and the runtime dispatcher with delayed firing and fire-once tracking.

It also owns the reverse lookup for “what points at this entity?”, scene connection validation and the declaration/implementation audit that checks registered inputs against the handlers that actually execute them.

### `logic_graph_widget.py`
Visual node-graph editor for entity I/O connections. Displays entities as nodes and I/O connections as wires; supports creating, deleting and editing connection delay/parameters.

### `logic_wizard.py`
Guided wizard for common I/O setups, covering monster encounters, doors/movers, environment/audio and more complex logic combinations.

### `main_window.py`
Main editor window. Hosts the 2D and 3D views, property editor, scene hierarchy, asset browser and debug console, and controls play mode, menus, toolbars and notifications.

It also owns the shared `ComponentController`, component modes, Radiant-style area selection and clip/split tools, and rebinds UI references after undo/redo replaces scene objects.

### `monster_customise_dialog.py`
Dialog for assigning custom Monster sprites and billboard dimensions. Paths are stored relative to the project asset tree.

### `package_dialog.py`
Package-export metadata dialog. Collects title, author, version, description and banner data and previews package dependencies.

### `package_exporter.py`
Builds `.fiopak` archives. Resolves map dependencies, crawls referenced assets and writes the package manifest.

### `procedural_generator.py`
Procedural map-generation UI and core generation logic. Builds playable liminal maps with configurable rooms, dimensions, textures, spawns, multiple floors and corridor connectivity.

### `procedural_map_gen.py`
Command-line front end for procedural map generation. Wraps the generator and writes map JSON using configurable size, room, seed, monster and floor parameters.

### `property_editor.py`
Per-object property panel for position, size, texture/shader, colour, I/O and type-specific properties.

Pages are cached using a signature of the data they actually display, so geometry/position changes do not rebuild unrelated property pages. I/O revision is included because the “Targeted by” information depends on the rest of the scene.

### `scene_hierarchy.py`
Scene tree listing brushes and entities. Supports sorting, selection synchronisation and context actions.

### `SettingsWindow.py`
Application settings dialog covering editor, display, play modes, controls, keyboard and split-screen configuration. Renderer settings expose low-power mode and dynamic shadows; low-power defaults come from the engine hardware probe rather than a duplicate editor-side detection system.

### `shortcuts.py`
Single inventory of editor keyboard shortcuts. Combines shortcuts discoverable from Qt actions/widgets, user-configured bindings and explicitly declared shortcuts for event handlers that Qt cannot enumerate.

### `shortcuts_window.py`
Help > Keys window showing the shortcut inventory from `editor.shortcuts`. It is searchable and can remain open while the user works.

### `surface_inspector.py`
Radiant-style Surface Inspector for per-face texture mapping. Edits shift, scale and rotation, supports grid-snap and multi-face application, and coalesces repeated spin-box changes into a single undo step.

### `terrain_editor.py`
Terrain parameter editor for noise seed, scale, amplitude and texturing/chunk settings.

### `things.py`
Definitions for placeable entities including `PlayerStart`, `Light`, `Model`, `Speaker`, `Pickup`, `Monster`, `PathNode`, `Portal`, `LevelChanger`, `LogicGate`, `LogicRelay`, `LogicTimer`, `LogicCommand`, `LogicSpawner`, `LogicCamera`, `LogicState` and `TriggerBrush`.

Entity classes provide defaults and I/O registration. `LogicState` is the persistent typed-state primitive shared by maps and plugins. `LogicCommand` provides a deliberate bridge from declarative I/O into an explicitly requested debug/play command rather than becoming a general-purpose scripting runtime.

### `tooltips.py`
Per-area tooltip enable/disable support. Original tooltip text is retained on Qt widgets so it can be restored without maintaining a second description table.

### `ui.py`
Shared Qt UI widgets, dialogs, styling helpers and layout utilities.

### `view_2d.py`
Orthographic top, front and side editing views. Handles brush drawing, selection, transforms, grid snapping, marquee selection, rotation, cloning, entity placement and the clip tool's screen-to-world mapping.

Component-mode editing is delegated to `component_edit`, so the 2D view supplies interaction mapping while the shared model owns the actual editing rules.

## Editor architecture

The editor is the authoring interface to the same world that the engine executes:

```
editor UI
    ↓
EditorState / Things / brush geometry
    ↓
authoritative map representation
    ↓
engine runtime
    ↓
play mode / standalone player
```

The important separation is between **authoring state** and **execution state**. The editor owns the authoritative scene model; the engine derives runtime representations from it as required.

For rendering, this means editor-authored brushes and entities ultimately feed the engine's dense numerical render projection rather than requiring the editor to maintain a separate renderer-specific world.

For gameplay, the editor's I/O connections are the program: entity outputs fire entity inputs, logic primitives transform and route those events, and `LogicState` supplies persistent typed state. No central quest graph or per-frame map scripting runtime is required.

## Design principles

- **Map is the program.** Entity placement, properties and I/O connections describe executable behaviour.
- **The editor is an engine mode.** Play Mode executes the same authored world rather than exporting to an intermediate compiled representation.
- **World state has one authority.** Derived caches and numerical projections do not become competing sources of truth.
- **Direct manipulation is first-class.** Brush geometry can be edited as objects, faces, edges and vertices.
- **I/O is compositional.** Small primitives and parameterised operations combine instead of requiring a large scripting vocabulary.
- **The hot path is allowed to be numerical.** The editor remains flexible and object-oriented while the engine projects large homogeneous data into dense execution representations.
