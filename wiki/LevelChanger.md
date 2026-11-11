> Applies to Fio 3.5.0.1111.

<img width="50" height="50" alt="image" src="https://github.com/user-attachments/assets/c13839a3-4a4a-4ace-a20b-872e3bf3f4c1" />

A **LevelChanger** (`type: "levelchanger"`, class `editor.things.LevelChanger`) takes the player to another map, optionally to a named [Player Start](https://github.com/ViciousSquid/Fio/wiki/Player-Start) there. Examples: `maps/Office_Corridor.json`, `maps/Simple_Map_Test_Extended.json`.

## Properties

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `target_map` | string | `maps/Simple_Map_Test.json` | Map to load, relative to the project root. |
| `destination_spawn` | string | `""` | Name of the Player Start to arrive at; empty uses the destination's primary start. A name that does not exist there fails the change and leaves the current level playing. |
| `usable` | bool | `false` | Whether the player can use it directly (see below). When false it only responds to I/O. |
| `radius` | float | `128.0` | Reach for direct use. |
| `show_radius` | bool | `false` | Draw the radius in the editor. |
| `delay` | float (s) | `0.0` | Wait before changing. |
| `fade_time` | float (s) | `0.5` | Fade-out before changing. |
| `generate_level` | bool | `false` | Continue into a newly generated level instead of loading `target_map`. |

## Using it directly

With `usable` true, a player inside `radius` and facing the entity (the direction to it within 60 degrees of the view direction) sees the prompt **[E] Complete Level**. Pressing Use fires `OnUse` and opens the level-complete screen; **E** continues to the target, **Esc** cancels (`engine/logic_interaction.py`, `engine/view_hud.py`). The player's loadout is carried across.

## Inputs and outputs

| Input | Effect |
| --- | --- |
| `Trigger` | Change level. |
| `ChangeLevel` | Change level. |

| Output | Fired |
| --- | --- |
| `OnUse` | When the player uses it directly. |

## Inside a package

While a `.fiopak` is being played, `target_map` is looked up in the package first (`MainWindow._package_map_path`, and `FioPackage.resolve_map` in the player), so linked levels travel with the world. The exporter follows `target_map` to include every reachable map. See [.fiopak archive](https://github.com/ViciousSquid/Fio/wiki/.fiopak-archive).

```json
{"type": "levelchanger", "pos": [-400, 40, -400],
 "properties": {"name": "ToAnnex", "target_map": "maps/annex.json",
                "destination_spawn": "AnnexStart", "usable": true, "radius": 96.0}}
```

Tests: `tests/e2e/test_level_changer_spawns.py`.
