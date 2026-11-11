> Applies to Fio 3.1.0.0611 (CANARY).

A **saved game** captures a play session: the level as it now is plus the live state the map format never stores. Module: `engine/savegame.py`; the session operations (`save_session` / `load_session`) belong to `engine/logic_session.py`. It is engine-native: no plugin is involved.

## Using it

| Console command | Effect |
| --- | --- |
| `save <name>` (`savegame`) | Write `saves/<name>.fiosave`. |
| `load <name>` (`loadgame`) | Restore it. |
| `quicksave` (`qs`) / `quickload` (`ql`) | The slot `saves/quicksave.fiosave`. |
| `saves` | List the saves. |

All are Play-mode commands and work from the Debug Console and from the in-game console. The pause menu (Esc in Play) has Save Game and Load Game slots. Saves live in `saves/` under the project root and are written atomically (`engine.fileio.write_json_atomic`).

## Format

A JSON document marked `"fio_savegame": true` (so a stray JSON file is never taken for a save), with `save_version` (currently **3**), `save_mode`, `saved_at`, `map`, and:

| Section | Contents |
| --- | --- |
| `level` | `EditorState.get_level_data()`: every brush and thing with its current properties and position (health, dead, collected, hidden ...). |
| `player`, `player2` | Position, angle, pitch, velocity, camera height, physics/ground/water state. |
| `runtime` | Cheat flags (`god_mode`, `buddha_mode`, `notarget`), camera mode, `active_weapon`, `weapons`, `player_ammo`, health/armour/dead, `collected_keys`, door, mover and monster states, pending I/O events. |

Version history: v2 added `save_mode`; v3 stores the weapons held (`runtime.weapons`) in place of v2's `gun2_obtained`. Version 1 and 2 saves still load.

## Save modes

Set by `[Settings] save_mode` in `settings.ini`:

| Mode | Stores | Needs the base map to load |
| --- | --- | --- |
| `full` (default) | The whole level plus player/runtime. | No |
| `delta` | Only the entities and brushes that differ from the base map, matched by UUID. | Yes |
| `both` | The delta and a complete fallback. | No (falls back to full) |

Loading is automatic (`restore_auto`): the mode is read from the file, a save with no mode is treated as `full`, and the base map is classified `exact`, `related` or `incompatible` (`classify_base_map`) so the loader only asks when no safe decision exists.

## Restore

A save is applied as an **overlay** onto a live, already-playing session; the scene is never rebuilt mid-flight (that would invalidate the logic thread's caches, the spatial grid and object identities). Live state is matched back to entities by their [stable UUIDs](https://github.com/ViciousSquid/Fio/wiki/Stable-UUIDs). A saved game names its map by basename inside `maps/` only. Transient keys (`_vel_y`, `_aggro_target`, `_kill`) are not saved. BigWorld's parked state is saved and restored with the world.

Tests: `tests/persistence/`, `tests/e2e/test_save_restore_in_play.py`.
