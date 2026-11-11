> Applies to Fio 3.5.0.1111.

The **Scene Hierarchy** is a tree of everything in the level: brushes, entities and the terrain. Module: `editor/scene_hierarchy.py`; its dock, `editor/hierarchy_dock.py`, is 200 px wide on every platform.

<img width="169" height="568" alt="image" src="https://github.com/user-attachments/assets/0dfad4c8-d0e1-4f52-a8c5-d9835e973bd3" />

- **Selection** is kept in step with the 2D and 3D views in both directions.
- **Names**: a brush shows its `name`, or `Brush N` / `Trigger N` when it has none.
- **Indicators** for tag, hidden and locked are drawn left of the name.
- **Search** box: filters and highlights matches by name; the clear button empties it.
- **Sort** button cycles `Default`, `A-Z`, `By Type`.

## Context menu

<img width="266" height="266" alt="image" src="https://github.com/user-attachments/assets/7c551a43-73f0-415f-ab27-ae5eb38cab79" />

Right-click an item to focus the camera on it, show or hide it, lock or unlock it, tag it, or put it in a visgroup (New Visgroup from Selection, Add to / Remove from a group, Manage Visgroups). On a category: Hide All, Show All, Lock All, Unlock All. On the terrain: Edit Terrain, Delete Terrain.

- A **hidden** object is not drawn and is not solid. **H** hides the selection and **Shift+H** shows everything hidden.
- A **locked** object cannot be selected in the 2D views (`[Display] locked_not_selectable_2d`).
- A **tag** is a marker for keeping track of an object; tagged objects show their tag in the 3D view too.

See [View Filters and VisGroups](https://github.com/ViciousSquid/Fio/wiki/View-Filters-and-VisGroups). The refresh is guarded by tests that count work rather than time (`tests/editor/`, `tests/performance/test_hierarchy_refresh_benchmark.py`).
