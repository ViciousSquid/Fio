> Applies to Fio 3.1.0.0611 (CANARY).

<img width="50" height="50" src="https://github.com/user-attachments/assets/ba2fec59-e3cb-421e-9632-9fa09e568323" />

A **LogicSpawner** (`type: "logic_spawner"`, class `editor.things.LogicSpawner`) creates an entity at its position when it receives `Spawn`. Example: `maps/Spawner_Test.json`. Handler: `editor/io_handlers.py`.

## Properties

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `spawn_type` | string | `Monster` | Entity class to create, looked up in `editor.things.ENTITY_TYPES` (case-insensitive). An unknown type logs an error and spawns nothing. |
| `spawn_properties` | object | `{}` | Properties given to the new entity. For a Monster, `"random": true` picks a random `monster_type` and `variant`. |
| `target_node` | string | `""` | PathNode handed to the spawned entity. |
| `max_spawn` | int | `0` | Limit on spawns; `0` is unlimited. |

## Inputs and outputs

| Input | Effect |
| --- | --- |
| `Spawn` | Create one entity (unless the limit is reached). |
| `Enable` / `Disable` | Allow / refuse spawning. |
| `SetTargetNode` (string) | Change `target_node`. |

| Output | Fired |
| --- | --- |
| `OnSpawn` | After an entity is created. |
| `OnMaxReached` | A `Spawn` arrived with `max_spawn` already reached. |

The spawned entity is appended to the world's things and the name/id caches are rebuilt, so it can be targeted by I/O at once. The count is kept in the runtime property `_spawn_count`.
