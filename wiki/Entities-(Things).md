> Applies to Fio 3.5.0.1111.

An **entity** ("thing") is a placed object with a position and properties: `{"type": ..., "pos": [x, y, z], "properties": {...}}` in a map, an `editor.things.Thing` subclass in memory. Every entity has `name`, a stable `id` ([Stable UUIDs](https://github.com/ViciousSquid/Fio/wiki/Stable-UUIDs)) and `io_enabled`, and takes part in the [I/O system](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System). Place one from the 2D views' context menu. The registry is `editor.things.ENTITY_TYPES`; an entity whose type is not registered (a missing plugin, a legacy type) is kept as an inert `UnresolvedThing` and saved back untouched.

## Core entities

| Entity | `type` | What it is |
| --- | --- | --- |
| [Player Start](https://github.com/ViciousSquid/Fio/wiki/Player-Start) | `playerstart` | Where the player spawns. Required to play a level. |
| [Light](https://github.com/ViciousSquid/Fio/wiki/Light) | `light` | Point light, optionally shadow-casting. |
| [Speaker](https://github.com/ViciousSquid/Fio/wiki/Speaker) | `speaker` | Positional or global sound. |
| [Monster](https://github.com/ViciousSquid/Fio/wiki/Monster) | `monster` | Hostile actor. |
| [Prop](https://github.com/ViciousSquid/Fio/wiki/Prop) | `prop` | Sprite or model; can be carried, collected or simulated. Replaces the old [Pickup](https://github.com/ViciousSquid/Fio/wiki/Pickup). |
| [Portal](https://github.com/ViciousSquid/Fio/wiki/Portal) | `portal` | Walk-through window to another place. |
| [LevelChanger](https://github.com/ViciousSquid/Fio/wiki/LevelChanger) | `levelchanger` | Takes the player to another map. |
| [PathNode](https://github.com/ViciousSquid/Fio/wiki/PathNodes) | `path_node` | A point on a path for monsters, cameras and movers. |
| [LogicCamera](https://github.com/ViciousSquid/Fio/wiki/Camera) | `logic_camera` | Cinematic camera; plays [cutscenes](https://github.com/ViciousSquid/Fio/wiki/Cutscenes). |
| [LogicSpawner](https://github.com/ViciousSquid/Fio/wiki/Spawner) | `logic_spawner` | Creates entities on demand. |
| Effect | `effect` | Fire, orb, explosion or custom animated effect, with an optional light. |

## Logic entities

See [Logic Entities](https://github.com/ViciousSquid/Fio/wiki/Logic-Entities).

| Entity | `type` | What it is |
| --- | --- | --- |
| LogicRelay | `logic_relay` | Forwards a trigger; can fire once. |
| LogicGate | `logic_gate` | AND / OR style gate over named inputs. |
| [LogicTimer](https://github.com/ViciousSquid/Fio/wiki/LogicTimer) | `logic_timer` | Fires on an interval or once. |
| [LogicCommand](https://github.com/ViciousSquid/Fio/wiki/LogicCommand) | `logic_command` | Runs a console command. |
| [LogicState](https://github.com/ViciousSquid/Fio/wiki/LogicState) | `logic_state` | A typed key/value store. |

## Brush entities

A brush becomes an entity through flags on its record ([Brushes](https://github.com/ViciousSquid/Fio/wiki/Basic-concept:--Brushes)): **trigger** (`is_trigger`), **door** (`is_door`), **mover** (`is_mover`), **water** (`is_water`) and **fog** (`is_fog`). Triggers, doors and movers have I/O; a plain brush accepts `Enable`, `Disable`, `Toggle`, `Kill`, `Hide`, `Show`, `ToggleVisibility`, `SetTint` and `ClearTint`.

## Plugin entities

Registered by the shipped plugins; a map that uses one turns its plugin on.

| Entity | Plugin |
| --- | --- |
| `DeathmatchSettings`, `DeathmatchSpawn`, `DeathmatchNode`, `DeathmatchFlag`, `DeathmatchBot` | [Deathmatch](https://github.com/ViciousSquid/Fio/wiki/Deathmatch-Plugin) |
| `BigWorldSettings` | [BigWorld](https://github.com/ViciousSquid/Fio/wiki/BigWorld-Plugin) |
| `TidyReceptacle`, `TidyGoal` | [Tidy](https://github.com/ViciousSquid/Fio/wiki/Tidy-Plugin) |

## Common inputs

Most visible entities accept `Hide`, `Show` and `ToggleVisibility`; most switchable ones `Enable`, `Disable` and `Toggle`. The console lists the real set for any type: `inputs <entity>` and `outputs <entity>`.
