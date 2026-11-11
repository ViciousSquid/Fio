> Applies to Fio 3.5.0.1111.

**Big World** (`plugins/bigworld/`) lets one map hold a very large world while only the region around the player is resident, active and simulated. It is a plugin over Fio's existing spatial grid, physics, renderer, terrain and save system -- not a new engine mode, world format or renderer -- and costs nothing on maps that do not opt in. Test map: `maps/BigWorld_streaming_test.json`.

## Opting in

Place one **BigWorldSettings** entity (`bigworldsettings`):

| Property | Default | Meaning |
| --- | --- | --- |
| `enabled` | true | Master switch. |
| `activation_radius` | 2048 | Cells within this distance of the player become resident. |
| `deactivation_radius` | 2304 | Cells beyond this are released; the gap is the hysteresis that stops thrashing at a boundary. |
| `sim_near_radius` | 1024 | Inside this, simulation is full. |
| `editor_streaming` | true | Stream cells around the 3D camera while editing. |
| `show_cell_debug` | true | The debug panel and active-cell minimap. |
| `terrain_stream`, `terrain_stream_radius`, `terrain_fill`, `terrain_infinite` | false, 0, false, false | Terrain streaming and procedural terrain. |
| `disk_streaming` | false | Experimental: free unloaded cells' objects from memory. |

## How it works

- **Cells** are 512 x 512 world units on X/Z, the same as the engine's [spatial grid](https://github.com/ViciousSquid/Fio/wiki/Spatial-Grid).
- **Residency** follows the player (not the camera), in a circular region, and is updated incrementally only when the player crosses a cell boundary. An object spanning several cells is reference-counted by UUID.
- **Parking**: brushes, entities and lights of non-resident cells are parked (the runtime keys `_bw_parked_hidden` / `_bw_parked_disabled`), so the rest of the engine skips them.
- **Tiers** (`engine.spatial`): `TIER_NEAR` (0) full simulation, `TIER_ACTIVE` (1), `TIER_DISTANT` (2) reduced, `TIER_DORMANT` (3) outside the active region. The monster AI and the mover table read an object's tier; a parked mover sleeps with its I/O still live and catches up on demand.
- **Persistent entities** can stay active wherever the player is.
- **Persistence**: per-cell gameplay deltas, integrated with normal [save games](https://github.com/ViciousSquid/Fio/wiki/Save-Games) (the `delta` save mode).

## In the editor

With editor streaming, only cells around the 3D camera are loaded; **View > Show whole world** (`[Display] show_whole_world`) loads every cell instead. Streamed-out records are still part of the map and are saved with it.

## Modules

`plugin.py` (registration), `manager.py` and `runtime.py` (residency), `cell.py`, `tiers.py`, `streaming.py`, `editor_streaming.py`, `persistence.py`, `entities.py`, `config.py`. Tests: `plugins/bigworld/tests/`. In the Android player its panel is drawn by `player/plugin_ui/bigworld.py`.
