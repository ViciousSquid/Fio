> Applies to Fio 3.5.0.1111.

# View Filters and VisGroups

View filters decide what the editor's views show while editing. There are three independent mechanisms, all held by one `ViewFilters` object (`engine/view_filters.py`, GL-free and Qt-free) at `EditorState.view_filters`:

- **Filter groups** ("auto visgroups") — every brush and entity belongs to exactly one kind (world brushes, triggers, lights, …) and each kind can be shown or hidden.
- **User visgroups** — named sets of brushes and entities, by stable id, each shown or hidden.
- **The cordon** — a box; while it is on, only what reaches into it is shown.

They hide objects from the 3D view, the 2D views, picking and connection lines. They never change the map's objects and they never apply in Play: Play always shows the world as it is. They are separate from the per-object `hidden` and `lock` flags, which are authored map state (see [Per-object hidden and lock flags](#per-object-hidden-and-lock-flags)).

| Concern | Owner |
|---|---|
| State, classification, masks, save/load | `engine/view_filters.py`: `ViewFilters`, `Visgroup`, `Cordon`, `brush_group`, `entity_group` |
| Window | `editor/visgroups_window.py`: `VisgroupsWindow` |
| Filter menu, View menu entries, toolbar eye | `editor/ui.py` (`_build_filter_menu`), `editor/main_window.py` (`set_view_filter`, `show_all_view_filters`, `set_visgroups_window_visible`, `add_to_visgroup`, …) |
| Scene hierarchy *Visgroup* submenu | `editor/scene_hierarchy.py`: `_add_visgroup_menu` |
| 3D publication | `engine/logic_render.py`: `prepare_render_state` |
| 2D drawing and hit-testing | `editor/view_2d.py` (`_filtered`, `_hidden`, `draw_cordon`) |
| 3D picking | `engine/view_picking.py`: `ViewPicking` (delegated from `QtGameView`) |
| Connection lines | `engine/view_debug_draw.py`: `ViewDebugDraw._gather_io_connections` |
| Terrain | `engine/qt_game_view.py` (the frame's terrain is `None` while the `terrain` group is hidden and the view is not in Play) |

## Using it

- **Filter menu** (menu bar): one checkable entry per kind of object, ticked while it shows, then *Show All*.
- **Visgroups & Cordon window**: the eye button on the toolbar, or *View → Visgroups / Cordon* (checkable; both follow the window's visibility).
- **Scene hierarchy**: right-click objects → *Visgroup* → *New Visgroup from Selection...*, add to / remove from an existing visgroup, or *Manage Visgroups...*.

### Filter groups

`FILTERS` in menu order:

| Key | Menu label | Members |
|---|---|---|
| `world` | World brushes | Every brush that is none of the kinds below (including Glow and subtract brushes) |
| `movers` | Movers & doors | Brushes with `is_mover` or `is_door` |
| `triggers` | Triggers | Brushes with `is_trigger` (unless they are water, fog, glass or glow), and `Trigger` entities |
| `water` | Water | Water brushes (`engine.constants.is_water_brush`) |
| `glass` | Glass | Brushes with shader `Glass` |
| `fog` | Fog | Brushes with `is_fog` or shader `Fog` |
| `terrain` | Terrain | The map's terrain |
| `lights` | Lights | Light entities |
| `pathnodes` | Path nodes | `PathNode` entities |
| `monsters` | Monsters | `Monster` entities |
| `props` | Props & models | Props and any entity with a model (not the private editor model of e.g. a player start) |
| `effects` | Effects | Effect entities |
| `portals` | Portals | `Portal` entities |
| `logic` | Logic entities | `LogicRelay`, `LogicGate`, `LogicTimer`, `LogicCommand`, `LogicCamera`, `LogicSpawner`, `LogicState`, `LevelChanger` |
| `sounds` | Speakers | `Speaker` entities |
| `player` | Player starts | `PlayerStart` entities |
| `entities` | Other entities | Every other entity |

A brush's group comes from its `RenderTable` class bits (`render_table._brush_class_bits`), tested in this order: trigger, dynamic (mover/door), water, glass, fog, else world. An entity's group comes from its `EntityTable` class bits (`entity_table._entity_class_bits`), tested in this order: effect, portal, path node, monster, light, prop/model, then by class name. Using the tables' own classification means a filter cannot disagree with what the renderer was told an object is.

Filter-group state is session state: it is not written to the map or to `settings.ini`, and loading a map does not reset it.

### The Visgroups & Cordon window

`VisgroupsWindow` is a separate top-level window with three tabs. The padlock button keeps it above all other windows (`Qt.WindowStaysOnTopHint`); that choice is stored as `[Visgroups] always_on_top` in `settings.ini`.

**User tab** — a list of visgroups with a checkbox (shown/hidden) and the number of members that still exist in the map.

| Button | Effect |
|---|---|
| New from Selection | Creates `Visgroup N` holding the selected objects |
| Add Selection / Remove Selection | Adds the selection to / removes it from the current visgroup |
| Select Members | Selects every brush and entity in the current visgroup |
| Delete | Deletes the visgroup; its objects stay |

Double-click a name to rename it; untick a visgroup to hide its members. An object can be in several visgroups and is hidden if any of them is hidden.

**Auto tab** — the same seventeen filter groups as the Filter menu, as a checklist, plus *Show All*. It calls `MainWindow.set_view_filter`, so the menu ticks follow.

**Cordon tab** — *Cordon on: show only what is inside the box*, Min/Max X, Y, Z (whole units, step 16, range ±1 000 000) and *Set from Selection*, which fits the box round the selected objects (brush bounds, entity positions) and turns the cordon on. The default box is −512..512 on every axis. Corners are reordered so min ≤ max.

Changing visgroups or the cordon marks the map as modified; changing filter groups does not.

## What a filter does

An object is filtered out (`ViewFilters.hides(obj)`) when any of these holds:

1. its filter group is hidden;
2. its id is in a hidden visgroup;
3. the cordon is on and the object does not reach into it — a brush is tested by its derived bounds (`engine.brush_placement.world_bounds`: a box's `pos ± size / 2`, an angled brush's planes at its `pos`, a turned brush's turned box) touching the cordon, an entity by its position being inside it (bounds inclusive). The dense path (`hidden_brush_rows`) tests the `RenderTable` bounds turned by each row's rotation (`brush_placement.turned_bounds`).

| Where | Effect | Code |
|---|---|---|
| 3D view | Filtered brushes and entities are not published to the renderer: the logic thread drops their rows while building the render state (`hidden_brush_rows`, `hidden_entity_rows`). No renderer has filter code. A filtered light still lights the editor view | `engine/logic_render.py` |
| Terrain | With the `terrain` group hidden the 3D view passes no terrain to the renderer. The cordon and visgroups do not apply to terrain | `engine/qt_game_view.py` |
| 2D views | Filtered objects are not drawn and cannot be hit by clicks or the rubber band; path-node links, monster and portal overlays skip them. The cordon is drawn as a dashed orange rectangle labelled "Cordon" | `editor/view_2d.py` |
| 3D picking | `get_object_at_3d` skips filtered brushes and entities; face picking (`get_brush_face_at_coords`) skips filtered brushes | `engine/view_picking.py` |
| Connection lines | An I/O or path link is drawn only between objects the filters show; the cache is keyed on `ViewFilters.version` | `engine/view_debug_draw.py: _gather_io_connections` |
| Selection | Filtering does not deselect anything. A filtered object simply cannot be picked in the views; it can still be selected from the scene hierarchy or with *Select Members* |
| Play | Ignored. `prepare_render_state` filters only when `view_filters.active and not play_mode`, and the terrain is always passed in Play |
| Saving, export | Unaffected: filtered objects are saved and exported like any other |

`ViewFilters.active` is true when any group is hidden, any hidden visgroup has members, or the cordon is on. `ViewFilters.version` increases on every change so views and caches can tell.

## Storage in the map

`EditorState.get_level_data` adds two top-level keys when the map has at least one visgroup or the cordon is on (`ViewFilters.to_level_data`):

```json
{
  "visgroups": [
    {"name": "Upper floor", "visible": true, "ids": ["<brush or entity id>", "…"]}
  ],
  "cordon": {"enabled": false, "min": [-512.0, -512.0, -512.0], "max": [512.0, 512.0, 512.0]}
}
```

| Key | Meaning |
|---|---|
| `visgroups[].name` | Display name; entries without a name are skipped on load |
| `visgroups[].visible` | Shown (default true) |
| `visgroups[].ids` | Stable ids of members, sorted. Ids of objects that no longer exist are dropped when saving |
| `cordon.enabled` | Default false |
| `cordon.min`, `cordon.max` | Box corners; default ±512. Malformed values fall back to the default cordon |

`ViewFilters.load_level_data(level)` replaces the visgroups and cordon from a loaded map (a map without the keys gets none) and is called with `{}` when the editor state is cleared. Membership is by id, so it survives undo/redo and renames; see [Stable UUIDs](https://github.com/ViciousSquid/Fio/wiki/Stable-UUIDs). Visgroup and cordon edits are not undo steps.

## Per-object hidden and lock flags

These are map data, not view filters, but they interact with what the views show:

| Flag | Stored | Effect |
|---|---|---|
| `hidden` | `brush['hidden']`, `thing.properties['hidden']` | The object is hidden in the editor views and in Play. It is the same flag the I/O `Hide` / `Show` inputs write. The 2D views treat an object as hidden when it is `hidden` *or* filtered (`View2D._hidden`); 3D picking skips hidden brushes |
| `lock` | `brush['lock']`, `thing.properties['lock']` | The object cannot be dragged or nudged in the 2D views and is skipped by 3D click-picking. With `[Display] locked_not_selectable_2d` on, locked objects also cannot be selected in the 2D views |

Both are toggled from the scene hierarchy context menu (*Lock* / *Unlock*, *Hide* / *Show*; on a section header *Lock All*, *Unlock All*, *Hide All*, *Show All*) and shown there as an icon before the name (hidden takes precedence over lock). There is no tag system for objects.

`hidden` also has keyboard shortcuts, handled in `MainWindow.keyPressEvent` and mirrored by *Edit → Hide Selected* / *Unhide All*:

| Key | Effect | Code |
|---|---|---|
| `H` | Sets `hidden` on every selected brush and entity, then clears the selection. One undo step. Does nothing without a selection | `MainWindow.hide_selected_brush` |
| `Shift+H` | Clears `hidden` on every brush and entity in the map, whether or not anything is selected. One undo step; does nothing when nothing is hidden | `MainWindow.unhide_all_brushes` |

Tests: `tests/editor/test_hide_shortcuts.py`.

## View menu entries

| Entry | Effect |
|---|---|
| Dock toggles | Show or hide the Scene Hierarchy, 3D view, 2D views and Properties docks |
| Asset Browser, *Thumbnail Size* | Asset browser visibility and thumbnail size |
| Surface Inspector (T) | Show or hide the Surface Inspector |
| Visgroups / Cordon | Checkable; shows or hides the Visgroups & Cordon window |
| Connection Links | Checkable; I/O connection links in the editor views (F1 by default) |
| Show whole world | Checkable; see below |
| Save Layout / Restore Layout | Dock layout |

**Show whole world** applies to Big World maps that are camera-streamed in the editor (`EditorState.editor_stream`). Ticked (the default, `[Display] show_whole_world = True`), every editor cell is loaded. Cleared, only the cells around the 3D camera are resident and they follow the camera (`EditorState.stream_editor_camera`); selected objects are kept resident. `MainWindow.set_show_whole_world` stores the setting, then calls `EditorState.set_editor_whole_world`, which switches residency without replacing objects or adding undo entries. The entry is disabled in Play and has no effect on maps that are not streamed. See [BigWorld Plugin](https://github.com/ViciousSquid/Fio/wiki/BigWorld-Plugin).

## Console commands

No console command changes filter groups, visgroups or the cordon. The related commands act on the per-object `hidden` flag:

| Command | Effect |
|---|---|
| `hide <name>` | Sets `hidden` on the named brush or entity (as the I/O `Hide` input does; in Play the collision grid is rebuilt for a brush) |
| `show <name>` | Clears `hidden` |

See [Console commands](https://github.com/ViciousSquid/Fio/wiki/Console-commands).

## API

```python
filters = main_window.state.view_filters

filters.set_shown('triggers', False)        # hide a filter group; KeyError for an unknown key
filters.shows('triggers')                   # -> False
filters.show_all()

group = filters.add_visgroup("Upper floor", objects)   # brush dicts and/or entities
filters.set_visgroup_visible(group, False)
filters.add_to_visgroup(group, more_objects)
filters.remove_from_visgroup(group, some_objects)
filters.visgroups_of(obj)                   # visgroups an object is in
filters.rename_visgroup(group, "Roof")
filters.remove_visgroup(group)

filters.set_cordon(enabled=True, lo=(-256, 0, -256), hi=(256, 512, 256))

filters.hides(obj)                          # brush dict or entity
filters.hidden_brush_rows(table.class_bits[:n], table)   # bool mask over RenderTable rows
filters.hidden_entity_rows(entity_table)                 # bool mask over EntityTable rows
```

Call these on the GUI thread and follow a change with `MainWindow.update_views()` (and `mark_as_modified()` for visgroups and the cordon); the `MainWindow` wrappers `set_view_filter`, `show_all_view_filters`, `add_to_visgroup` and `remove_from_visgroup` do both and keep the menu and window in step. `hidden_brush_rows` without the `table` argument applies filter groups only. `hidden_entity_rows` caches its group/visgroup mask per `(version, table, generation, count)` and re-tests the cordon against current positions on every call, because entities move without a new table generation.

To add a filter group, add its `(key, label)` to `FILTERS` and return the key from `brush_group` or `entity_group` (and from `_hidden_brush_groups` for a brush kind); the Filter menu and the Auto tab are built from `FILTERS`.

## Tests

| File | Covers |
|---|---|
| `tests/editor/test_view_filters.py` | Every kind of brush and entity has a group; dense masks agree with the per-object predicates; filtered rows are not published while editing; Play ignores the filters; the Filter menu; 2D views and picking skip filtered objects; hiding path nodes hides their links |
| `tests/editor/test_visgroups_cordon.py` | Hidden visgroups hide members by id; cordon tests; save and load with the map; the toolbar eye and View menu entry; the padlock; the window's User, Auto and Cordon tabs; the scene hierarchy *Visgroup* menu |

Both are in the `qt` tier; see [Testing and CI](https://github.com/ViciousSquid/Fio/wiki/Testing-and-CI).
