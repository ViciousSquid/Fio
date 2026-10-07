## PlayerStarts

A level can have several **PlayerStarts**. Each has a **name** (the entity name in the Property editor) and a **Primary** flag.

* At most one PlayerStart in a level is primary. **Play starts at the primary.**
* The first PlayerStart created in a level becomes primary; later ones, and copies of the primary, do not.
* To move the primary, select another PlayerStart and tick **Primary**. The old primary is cleared. This is one undo step.
* If the primary is deleted, the earliest-created remaining PlayerStart becomes primary. Deleting every PlayerStart leaves none (none is created for you), and Play then asks you to add one.
* The order entities are listed or saved in never changes which start is primary.

The 2D views label a PlayerStart under its sprite: its name and, for the primary, **Primary**.

## LevelChangers

A LevelChanger sends the player to **Target Map**. **Destination Spawn** picks where they arrive:

* `<Primary>` (the default): the target map's primary PlayerStart.
* A name: the PlayerStart of that name in the target map, primary or not. The list offers the named starts found in the target map.

The player arrives directly at that start, keeping their weapons and ammunition. If the named start is not in the target map, the level change does not happen: the console and a notification say `LevelChanger: destination spawn 'DungeonEntrance' was not found in 'maps/hub.json'`, and the current level carries on unchanged. It never falls back to the primary.

A `ChangeLevel` input whose parameter names a *different* map enters that map at its primary start, since the Destination Spawn belongs to Target Map.

### Example: a hub

| Map     | PlayerStarts                              | LevelChanger                                |
| ------- | ----------------------------------------- | ------------------------------------------- |
| Hub     | `HubStart` (primary), `DungeonEntrance`   | Target Map Dungeon, Destination Spawn `DungeonStart` |
| Dungeon | `DungeonStart` (primary)                  | Target Map Hub, Destination Spawn `DungeonEntrance`  |

Play in the Hub starts at `HubStart`; entering the dungeon arrives at `DungeonStart`; leaving it returns to `DungeonEntrance`, outside the dungeon's door.

## In the map file

A PlayerStart saves `"primary": true|false` and `"creation_index"` (its creation order in the level). A LevelChanger saves `"destination_spawn"` (a PlayerStart name, or `""` for the primary). Maps written before these existed load with their first PlayerStart as primary, which was the spawn before.
