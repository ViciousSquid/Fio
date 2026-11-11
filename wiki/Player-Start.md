> Applies to Fio 3.1.0.0611 (CANARY).

A **Player Start** (`type: "playerstart"`, class `editor.things.PlayerStart`) is where the player spawns when Play begins or a level is entered. A level needs at least one: with none, Play is refused with a "No Player Start" message and the editor stays as it was. In the editor it is drawn as a player-sized model (`engine/entity_models.py`, `docs/playerstart-model.md`), never in Play.

## Properties

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | generated | Identifier. A [LevelChanger](https://github.com/ViciousSquid/Fio/wiki/LevelChanger)'s `destination_spawn` refers to it. |
| `angle` | float (degrees) | `0.0` | Direction the player faces on spawning. |
| `primary` | bool | `false` | The start Play begins at. Exactly one start per level is primary. |
| `creation_index` | int | assigned | Order the start was created in its level; used to break ties. |
| `io_enabled` | bool | `true` | Whether its outputs fire. |

## Which start is used

The rules are in `engine/player_starts.py` and are shared by the editor, the logic thread and the standalone player:

- With a name (a level change that gives `destination_spawn`): the start of that name. If none has that name the change fails; another start is never substituted.
- Without a name: the primary start. If no start is marked primary (an older map), the earliest-created one.
- `settle()` keeps the invariant after every edit: every start has a unique `creation_index`, and exactly one is primary -- the earliest-created claimant, or the earliest-created start when none claims it (the primary was deleted).

## Outputs

| Output | Fired |
| --- | --- |
| `OnPlayerSpawn` | When the player spawns at this start. |
| `OnPlayerDeath` | When the player dies. |

It has no inputs.

## In a map

```json
{"type": "playerstart", "pos": [0, 40, 0],
 "properties": {"name": "Start", "angle": 0.0, "primary": true}}
```

Tests: `tests/editor/test_player_start_editor.py`, `tests/engine/test_player_start_rules.py`, `tests/e2e/test_level_changer_spawns.py`.
