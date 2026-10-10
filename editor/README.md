# `editor/`

| File | What it is |
| --- | --- |
| `__init__.py` | Loads the plugin registry before the editor starts. |
| `LOGIC.md` | Documentation of the entity I/O and logic system. |
| `SettingsWindow.py` | The Settings dialog. |
| `asset_browser.py` | The Asset Browser: Textures, Models, Maps and Audio tabs. |
| `component_edit.py` | Face, edge and vertex editing of brushes. |
| `console_commands.py` | The console commands. |
| `cutscene_wizard.py` | The Cutscene Wizard panel. |
| `debug_console.py` | The debug console, its command input and history, and `debug_log`. |
| `editor_state.py` | The scene: brushes, entities, terrain, selection, undo/redo, map save/load. |
| `entity_inspector.py` | The Entity Inspector window. |
| `face_texture.py` | Reads and writes a face's texture mapping. |
| `io_editor_widget.py` | The I/O connections panel. |
| `io_handlers.py` | The input handlers for every entity type. |
| `io_system.py` | The I/O system: declarations, connections, dispatch. |
| `item_editor.py` | The Custom Items dialog. |
| `logic_graph_widget.py` | The Logic Graph node editor. |
| `logic_wizard.py` | The Logic Wizard for common I/O setups. |
| `main_window.py` | The main editor window. |
| `monster_customise_dialog.py` | The monster sprite dialog. |
| `package_dialog.py` | The package export dialog. |
| `package_exporter.py` | Builds `.fiopak` packages. |
| `procedural_generator.py` | The procedural level generator and its dialog. |
| `procedural_map_gen.py` | Command-line procedural level generator. |
| `project_overview.py` | The Project Overview window. |
| `property_editor.py` | The Properties panel. |
| `scene_hierarchy.py` | The Scene Hierarchy panel. |
| `selection_overlay.py` | Turns the editor selection into the renderer's selection overlay. |
| `shortcuts.py` | The list of keyboard shortcuts. |
| `shortcuts_window.py` | The Help > Keys window. |
| `state_values.py` | Typed values for LogicState. |
| `surface_inspector.py` | The Surface Inspector. |
| `terrain_editor.py` | The terrain editor panel. |
| `things.py` | The placeable entity classes. |
| `tooltips.py` | Turns tooltips on and off per area. |
| `ui.py` | Builds the main window's menus, toolbar and docks. |
| `version.txt` | The editor version string. |
| `view_2d.py` | The 2D top, front and side views. |
| `visgroups_window.py` | The Visgroups & Cordon window. |
