> Applies to Fio 3.5.0.1111.

**Tools > Procedural Generator** builds a complete, playable level -- rooms, corridors, a Player Start, monsters and health -- as an ordinary Fio map that can be played at once or edited like any other. Module: `editor/procedural_generator.py`; command line: `tools/procedural_map_gen.py`.

<img src="https://github.com/user-attachments/assets/41fe367d-34dc-4ae6-89ac-35d794f80385" width="850">

## Pipeline

1. **Grid.** The world is divided into logical cells of 64 x 64 units (`CELL_SIZE`).
2. **Rooms** are placed on the grid (`GridMap.add_room`).
3. **Corridors** connect them with A* (`connect_rooms`, `astar`), optionally through extra waypoints.
4. **Dead ends** are added for variation (`add_dead_ends`).
5. **Brushes** are generated from the open cells (`generate_brushes_from_grid`): floors, walls, ceilings.
6. **Upper floors** (optional): a mezzanine in some rooms, reached by steps that rise 16 units each (`STEP_RISE`, under the player's 18-unit step height).
7. **Entities**: Player Start, lights, monsters and health, placed inside rooms away from walls (`create_map_data`).

The generator first decides where the player can walk, then turns that into geometry.

## Parameters

Values from outside (a map's LevelChanger, the command line) pass through `sanitize_params`: unknown keys are dropped, numbers are clamped, the world is snapped to one of the three sizes, and texture names must stay inside `assets/textures`.

| Parameter | Default | Range |
| --- | --- | --- |
| `world_width`, `world_height` | 2048 | 1024, 2048 or 4096 |
| `room_count` | 14 | 8-24 |
| `min_room` | 256 | 192-320 |
| `max_room` | 384 | 256-512 (never below `min_room`) |
| `spawn_monsters`, `monster_count` | True, 4 | 1-64 |
| `spawn_health`, `health_count` | True, 6 | 1-64 |
| `wall_tex`, `floor_tex` | `default.png` | a texture name |
| `random_wall_texture`, `random_floor_texture` | False | |
| `enable_floors`, `floor_room_count`, `floor_height` | False, 2, 128 | 0-16; 16-512 |

Each generation in the editor uses a fresh random seed.

## Generated levels in play

A [LevelChanger](https://github.com/ViciousSquid/Fio/wiki/LevelChanger) with `generate_level` set continues into a newly generated level, built from its stored generator parameters, instead of loading a map.

## Command line

```bash
python tools/procedural_map_gen.py -o mymap.json --size large --room-count 20 --monster-count 8 --seed 123
```

`--size` is `small` (1024), `medium` (2048, default) or `large` (4096); `--width` / `--height` set a size directly. With `--seed` the same parameters reproduce the same level; without it a seed is chosen and printed.
