> Applies to Fio 3.1.0.0611 (CANARY).

Every brush and every entity carries a stable identifier in `id` (a brush dict's `id`; a thing's `properties['id']`): a UUID4 string assigned once and kept for the life of the object, across saves, renames and reordering.

## Where ids come from

| When | What happens | Where |
| --- | --- | --- |
| A thing is created | `properties['id'] = str(uuid.uuid4())` unless one is given | `editor/things.py: Thing.__init__` |
| A thing is duplicated | The clone gets a new id | `Thing` clone |
| A map is loaded | A brush record without an id gets one (`setdefault`) | `EditorState._deserialize_brushes` |
| Ids are missing or duplicated | `ensure_entity_ids()` assigns fresh ones | `editor/editor_state.py` |
| Paste / duplicate of brushes | New ids | `editor/editor_state.py` |

## What refers to them

- **I/O connections**: `OutputConnection.target_id`. The runtime resolves a target by id first and falls back to `target_name` for older maps, so renaming a target does not break its connections.
- **Save games**: live state is overlaid onto the running world by matching ids, and a `delta` save lists changed objects by id ([Save Games](https://github.com/ViciousSquid/Fio/wiki/Save-Games)).
- **Render tables**: `slot_of_id` maps an id to its dense row.
- **LogicState** values of type `uuid`.
- **Undo/redo and BigWorld streaming** identify records by id.

Names are for people and may repeat; ids may not. The console's entity lookups take a name or an id.
