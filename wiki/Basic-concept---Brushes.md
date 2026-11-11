> Applies to Fio 3.1.0.0611 (CANARY).

A **brush** is a convex solid: the building block of walls, floors, doors and volumes. In a map it is a record in `brushes`; in memory a dict shared by the editor, the logic thread, the renderer and plugins. Modules: `engine/brush_geometry.py` (shape), `engine/brush_faces.py` (materials), `engine/brush_placement.py` (where it is).

## Two forms

- **Box**: `pos` (centre) and `size`. Its six faces are named `north`, `south`, `east`, `west`, `top`, `down`, and each face's material is kept per tag (`textures`, `uv_scale`, `uv_shift`, `uv_angle`, `uv_natural`).
- **Angled (convex)**: a box that has been clipped, rotated or had its vertices moved gains `geometry.planes`. The planes are in the **brush's own frame, centred on `pos`**, and each plane carries its face's whole material record (`texture`, scale, shift, angle, `uv_u` / `uv_v`). The per-tag copies are dropped, and `size` is a derived copy of the shape's bounds. This is map version 5; older maps stored world-space planes and are converted on load.

`box_to_geometry` promotes a box, `drop_geometry` returns a convex brush that is still a box to the compact form. `get_convex(brush)` gives the shape in world space, `local_convex(brush)` in its own frame.

## Editing

- **Brush tool**: drag in a 2D view to draw. A plain click starts a new brush even on top of one; Shift+click selects.
- **Move / resize** with the handles; arrow keys nudge by a grid step. `G`, `[` and `]` control the grid.
- **Clip**: cut a brush with a plane; Enter keeps one side, Shift+Enter both.
- **Component modes** (Shift+V, Shift+E, Shift+F, or Q to step): move vertices, edges and faces. The result stays convex.
- **Subtract**: a brush with `operation: "subtract"` carves others (CSG); Hollow turns a brush into a room.
- **Textures** per brush or per face from the [Asset Browser](https://github.com/ViciousSquid/Fio/wiki/Asset-Browser), adjusted in the Surface Inspector. `nodraw` hides a face ([Autocaulk](https://github.com/ViciousSquid/Fio/wiki/Autocaulk)).
- Hide (H), lock, tag and group brushes from the [Scene Hierarchy](https://github.com/ViciousSquid/Fio/wiki/Scene-Hierarchy).

## Brush entities

Flags on the record give a brush behaviour ([Property editor](https://github.com/ViciousSquid/Fio/wiki/Property-editor)):

| Flag | Becomes | Notes |
| --- | --- | --- |
| `is_trigger` | Trigger volume | Not solid. `trigger_type`, `trigger_activation` (`touch` / `use`). Outputs `OnTrigger`, `OnStartTouch`, `OnEndTouch`. |
| `is_door` | Door | `door_direction`, `door_distance`, `door_lip`, `door_speed`, `door_locked`. Inputs `Open`, `Close`, `Toggle`, `Lock`, `Unlock` ... |
| `is_mover` | Mover | Moves or follows a [PathNode](https://github.com/ViciousSquid/Fio/wiki/PathNodes) chain. Inputs `Open`, `Close`, `SetPosition`, `FollowPath` ... |
| `is_water` | Water | Not solid; swimmable; drawn by the water pass. |
| `is_fog` | Fog volume | Not solid. |

A brush's `pos` is where it was authored and does not change in play; a moving door or mover is placed by the mover table, and code asks `engine.brush_placement.placement(brush)` for where it is now ([Physics](https://github.com/ViciousSquid/Fio/wiki/Physics)). I/O connections on a brush are stored in `io_connections` ([I/O system](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System)).

Tests: `tests/geometry/`, `tests/brush_model/`; design record: `docs/brush-model-stage2.md`.
