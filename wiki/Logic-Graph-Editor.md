> Applies to Fio 3.1.0.0611 (CANARY).

# Logic Graph Editor

The Logic Graph Editor is a node-graph view of the map's [I/O connections](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System). Every named entity is a node, its declared outputs and inputs are pins, and every `OutputConnection` is a wire. It edits the same connection records as the Property editor's I/O tab; it is a second editor for them, not a second data model.

Open it with **Ctrl+L** or **Tools → Logic Graph Editor…**. It is a separate, non-modal window (`Logic Graph Editor`).

<img src="https://github.com/user-attachments/assets/22283623-21a2-4776-a2ae-71649f5276f0" width="800">

> Changes made in the graph are **not** written to the entities until you press **Apply** (or **Ctrl+S** inside the graph window). Until then the scene, Play and the I/O tab still see the old connections.

## Using it

| Action | How |
| :--- | :--- |
| Create a connection | Drag from an **output pin** (orange, right side of a node) to an **input pin** (blue, left side) of another node |
| Create a connection without aiming at a pin | Drag from an output pin and release anywhere on another node: a menu lists that node's inputs (`●` marks inputs already wired; tooltips show the input's description) |
| Edit parameter, delay, fire-once | Right-click the wire → **Edit delay / parameter…** (Delay 0–999 s, step 0.1, 2 decimals; *Fire once*) |
| Delete a connection | Right-click the wire → **Delete connection**, or select the wire and press **Delete** |
| Remove every wire touching a node | Right-click the node → **Clear all connections from this node** (incoming and outgoing) |
| Show or hide a node's unused pins | Double-click the node, or right-click → **Show all N pins** / **Show only connected pins** |
| Move a node | Drag it |
| Select several | Rubber-band drag on the background |
| Pan | Middle mouse button drag |
| Zoom | Mouse wheel, or the **+** / **−** buttons in the top-right corner (factor 1.15 per step) |
| Find an entity | **Ctrl+F**, type part of a name or type; matches are selected; **Enter** frames them |
| Frame the selection (or everything) | **F** |
| Fit the whole graph | **Home** |
| Write the graph to the map | **Apply** button or **Ctrl+S** |

Toolbar:

| Control | Effect |
| :--- | :--- |
| *Find entity…* | Search by name or type (case-insensitive substring) |
| **Wire info** (on by default) | Draws each wire's parameter, delay (`2s`) and `once` flag on the wire |
| **All pins** (off by default) | Expands every node to show all declared pins, not only connected ones |
| **Arrange** | Lays the graph out left to right by signal flow and fits it in view |
| **Reload** | Rebuilds the graph from the scene; asks first if there are unapplied changes |
| **Wizard…** | Opens the Logic Wizard against this graph; its connections appear as wires and still need **Apply** |
| **Apply** | Writes the graph to the entities. The button turns amber while there are unapplied changes |

Rules the graph enforces when wiring: a wire starts at an output and ends at an input; source and target must be different nodes (an entity cannot be wired to itself here, although the I/O tab allows it); a second wire between the same two pins is ignored. New wires are created with an empty parameter, no delay, `fire_once` false, and both the target's name and UUID.

Closing the window with unapplied changes asks **Apply / Discard / Cancel**.

## What is shown

* **Nodes.** One per Thing or brush that has a non-empty `name`. Unnamed entities have no node. The header colour follows the I/O type: `monster` red, `light` yellow, `door` and `mover` blue, `speaker` and `portal` and `logic_camera` purple, `trigger` orange, `logic_relay` / `logic_gate` / `logic_timer` / `prop` / `logic_spawner` green, `playerstart` and `path_node` teal, `levelchanger` pink, `logic_state` ochre, plain `brush` grey-blue; any other type (plugin entities, `effect`, `logic_command`) grey.
* **Pins.** The names registered for the entity's type in `io_system.IO_REGISTRY` (`get_output_names` / `get_input_names`). A collapsed node shows only pins that carry a wire, plus an "N more…" row; all other nodes' inputs are revealed while a wire is being dragged so there is always somewhere to drop it. The [generic inputs](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System) (`Kill`, `SetTint` on a Thing, and so on) are not pins unless the type declares them.
* **Wires.** Resolved by `target_id` first, then by `target_name` (first node with that name).
* **Connections that cannot be drawn.** A connection whose target is not in the map, whose output or input name is not a declared pin of the type, that lives on an unnamed entity, or that is still a raw dict from an old map has no wire. The status bar reports the count (`N connections cannot be shown … click for details`) and clicking it lists them. They are preserved: **Apply** writes them back unchanged after the drawn ones.

## Layout and what is saved

* Node positions are stored per entity UUID in the map file:

  ```json
  "logic_graph": {
    "node_positions": {
      "402c3c0e-4aa7-46f2-915d-16817c6d9e9f": {"x": 260.0, "y": 0.0}
    }
  }
  ```

  The key is written only when at least one position exists. Positions are published to `EditorState._logic_graph_positions` when **Arrange** or **Apply** is used and when the window closes; the map save reads them from there (`EditorState._collect_logic_graph_positions`).
* A node without a saved position is auto-placed: on a grid, with a source swapped to the left of its target, then packed into columns by real node height. Nodes with saved positions are never moved automatically.
* **Arrange** assigns each node the column of the longest connection chain that reaches it (cycles are handled by limiting the walk), sorts each column by name, and stores the result.
* The graph is rebuilt automatically after a map load or New Map (`MainWindow._refresh_logic_graph`): an open window reloads, a hidden one is discarded.

## Apply semantics

`LogicGraphScene.apply_to_entities()`:

1. `LogicGraphWindow.about_to_apply` is emitted first; `MainWindow` checkpoints the scene (`EditorState.save_state()`), so one **Ctrl+Z** in the editor undoes the whole Apply.
2. For every entity that has a node, and every entity holding undrawable connections, `_io_connections` is replaced by: the wires leaving its node, in the order they exist in the graph, followed by its undrawable connections in their original order. Entities with neither are not touched.
3. `io_system.bump_io_revision()` invalidates the reverse-target index (the Property editor's *Targeted by* line, LogicGate expected-input counts).
4. Node positions are stored, `applied` is emitted, and `MainWindow` marks the map modified and invalidates its entity caches.

Because step 2 rewrites whole lists from the graph's state at the time it was loaded, connection edits made elsewhere (I/O tab, `connect` console command, `Ctrl+K` linking) while the graph has unapplied changes are overwritten by Apply. Press **Reload** first to pick them up.

The Logic Wizard (`editor/logic_wizard.py`) adds its connections as wires through `LogicGraphScene.add_connection_by_name`; only a connection the graph cannot represent (an entity without a node, a pin the type does not declare) is written straight onto the source entity. Opened from this window's **Wizard…** button, or from the Tools menu while a Logic Graph window exists for the map, the wires land in this graph and still need **Apply**. Opened from the Tools menu when no Logic Graph window exists, `MainWindow.open_logic_wizard` gives it a temporary scene that is never applied, then opens a new graph window built from the unchanged entities, so those wires are lost.

## Developer reference

| | |
| :--- | :--- |
| Module | `editor/logic_graph_widget.py` |
| Window | `LogicGraphWindow(editor_state, parent)` — signals `about_to_apply`, `applied`, `select_entity(object)` |
| Scene | `LogicGraphScene` — `_nodes` (keyed by entity UUID, or `str(id(entity))` without one), `_connections`, `_undrawable`; `apply_to_entities()`, `add_connection_by_name(src, out, dst, in, …)`, `auto_arrange()`, `store_positions()`, `find_nodes(text)`, signal `connections_changed` |
| Items | `EntityNodeItem`, `PinItem`, `ConnectionItem` (holds the live `OutputConnection` as `conn_obj`), `DragLine` |
| View | `LogicGraphView` — pan, zoom, `fit_all()`, `frame_selected()` |
| Opened by | `MainWindow.open_logic_graph()` (`editor/ui.py: logic_graph_action`, `Ctrl+L`) |
| Persistence | `EditorState._logic_graph_positions` ↔ map key `logic_graph.node_positions` |
| Thread | Qt UI thread only; the graph never runs during dispatch |
| Tests | `tests/persistence/test_logic_round_trip.py` |

Notes:

* A new entity type appears in the graph as soon as it is registered with `io_system.register_io` (plugins: `api.register_io`); add a `TYPE_HDR` entry to give it a header colour.
* The node context menu's **Select in editor** emits `LogicGraphWindow.select_entity`; nothing in the editor is connected to that signal, so the item currently has no effect.
* Wires edit the `OutputConnection` objects that the entities already hold: changing a wire's delay, parameter or fire-once flag in the dialog mutates the live connection immediately, before Apply. Creating and deleting wires does not touch the entities until Apply.

## See also

[Triggers & The I/O Logic System](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System) · [Logic Entities](https://github.com/ViciousSquid/Fio/wiki/Logic-Entities) · [Property editor](https://github.com/ViciousSquid/Fio/wiki/Property-editor) · [Map format (.json)](https://github.com/ViciousSquid/Fio/wiki/Map-format-(.json)) · [Stable UUIDs](https://github.com/ViciousSquid/Fio/wiki/Stable-UUIDs)
