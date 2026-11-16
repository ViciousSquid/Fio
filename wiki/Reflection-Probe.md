> Applies to Fio 3.5.0.1611.

<img width="50" height="50" alt="Reflection Probe" src="https://raw.githubusercontent.com/ViciousSquid/Fio/Canary_1611/assets/sprites/reflection_probe.png" />

# Reflection Probe

Fio's cubemap entity. [Water](https://github.com/ViciousSquid/Fio/wiki/Water) whose centre is inside a probe's radius reflects a 360-degree snapshot taken from the probe — the room around it — instead of the map's sky.

It is optional. Water reflects the sky with no setup, because the sky looks the same from everywhere, and high-quality water reflects whatever is on screen live. The gap is water under a roof: where the on-screen reflection misses (the ceiling behind or above the camera), a map with a sky shows sky. A probe fills that gap with the room. Place one in rooms with water; leave it out everywhere else. A map without probes renders exactly as it does without this entity.

Unlike Source's `env_cubemap` there is no build step: the renderer takes the snapshot itself, once, when it is needed.

| | |
| :--- | :--- |
| Class | `editor/things.py: ReflectionProbe` |
| `type` in the map | `reflectionprobe` |
| Table row | `engine/entity_table.py`: class bit `ENT_PROBE`, column `probe_radius` — the renderer finds probes in the [EntityTable](https://github.com/ViciousSquid/Fio/wiki/EntityTable) and never walks Things |
| Snapshot | `engine/renderer/forward/renderer.py`: `_update_reflection_probes`, `_capture_reflection_probe`, `_probe_cube_to_equirect`; shader `probe_equirect.frag` in `engine/shaders.py` |
| Water lookup | `engine/renderer/forward/passes.py: _water_probe_textures`, inside `draw_water_brushes` |
| Retaken on load / Play | `Renderer.invalidate_reflection_probes()` (`engine/renderer/api.py`), called by `QtGameView.preload_level_textures` |
| Editor icon | `assets/sprites/reflection_probe.png` |
| Place it | 2D view right-click → **Reflection Probe** |

## Properties

| Key | Type | Default | Editor | Meaning |
| :--- | :--- | :--- | :--- | :--- |
| `radius` | float, world units | `512.0` | *Radius* spin box | Water brushes whose **centre** lies within this distance of the probe reflect its snapshot. Below 16 is read as 16; a value that is not a number, as 512 |
| `show_radius` | bool | `true` | *Show Radius* checkbox | Draw the radius circle (light blue) in the 2D views. Editor only |
| `name`, `id`, `io_enabled` | | | | Common to all Things |

The probe has no inputs or outputs of its own and is not drawn in Play.

## Placing one

- Put it where a camera would see the room as the water should reflect it: over the water, in the middle of the room, at about head height.
- Make the radius reach the **centre** of each water brush it should serve. A water brush is assigned as a whole, by its centre, not pixel by pixel.
- Where several probes reach one water brush, the nearest wins.
- One probe per room is enough. Water outside every probe keeps reflecting the sky.

## How it works

1. **Snapshot.** On the first frame that finds a probe in the map, before anything is drawn, the renderer draws the map six times from the probe's position — 90-degree views along ±X, ±Y and ±Z, `PROBE_FACE_SIZE` (128) texels square — into a cube map. These views are drawn as Play draws: no grid, no editor icons. Monsters, effects, other probes and portal views are left out: the snapshot is not retaken every frame, so anything that moves would be frozen into it. Brushes, models, terrain, lights and the sky are in it.
2. **Unfolded like a sky image.** The cube is drawn out into a 512 × 256 equirectangular texture — the layout of a sky image — and freed. The lower half is the upper half mirrored about the horizon: water only ever looks up into it, and a reflection that a wave tips just below the horizon then finds the walls just above it, not the floor or the pool itself.
3. **Read by the water.** Each frame, every water brush whose centre is inside a probe's radius gets that probe's snapshot bound where it would have had the sky image. The water shader reads it exactly as it reads the sky (`panoramaUV`, worked out per vertex): one texture read per pixel, no new shader code, no extra pass. In high-quality water the live reflection of the world still comes first; the snapshot fills in where it misses — which is what a probe is for.
4. **Retaken** when the map loads, when Play starts, when the renderer is switched, and when a probe is moved or its radius changed. Deleting a probe frees its snapshot. Editing the brushes around a probe in the editor does **not** retake it — press Play, or nudge the probe, to see the change (Source's `buildcubemaps`, without the command).

The capture leaves the main view exactly as it found it: it draws into its own framebuffer with its own instance streams, issues no occlusion queries, and puts the framebuffer and viewport back. `tests/visual/test_reflection_probe_gl.py` checks this by drawing water outside every probe pixel-for-pixel as before.

## Cost

| | |
| :--- | :--- |
| Per snapshot | Six 128 × 128 draws of the map, once — not per frame |
| Memory | 512 KB of texture per probe (512 × 256 RGBA) |
| Per frame | Nothing to draw. The water reads the snapshot in place of the sky: the same single texture read |

## Limits

- **Static.** What moves is not in the snapshot, and editor changes appear only when it is retaken (see step 4).
- **Taken from one point.** Reflections are right near the probe and drift (parallax) further from it. Rippled water hides this, which is why the snapshot can be small.
- **Water only.** Glass and wet decals do not use probes; wet decals reflect the screen, as before.
- **Desktop renderer only.** The [Android player](https://github.com/ViciousSquid/Fio/wiki/Fio-Player-(Android)) has its own renderer, which ignores probes: its water reflects the sky as before.

## In the map file

```json
{
  "type": "reflectionprobe",
  "pos": [0.0, 300.0, 0.0],
  "properties": {
    "name": "ReflectionProbe_1",
    "id": "3f9c2a71-0d4e-4b8a-9a52-6c1e7f0b2d18",
    "radius": 900.0,
    "type": "reflectionprobe",
    "io_enabled": true,
    "show_radius": true
  },
  "io_connections": []
}
```

## Tests

`tests/visual/test_reflection_probe_gl.py` — water in a probe's radius shows the room, not the sky (high quality and cheap); water outside every probe is unchanged; a snapshot is taken once and retaken only when needed; the snapshot is the right way up and mirrored below the horizon. `tests/renderer/test_entity_table.py` (`test_a_reflection_probe_is_a_probe_row_carrying_its_radius`) — the class bit, the radius column, the save/load round trip, the registry.

## See also

[Water](https://github.com/ViciousSquid/Fio/wiki/Water) · [Entities (Things)](https://github.com/ViciousSquid/Fio/wiki/Entities-(Things)) · [Player Start](https://github.com/ViciousSquid/Fio/wiki/Player-Start) (the sky image) · [EntityTable](https://github.com/ViciousSquid/Fio/wiki/EntityTable) · [Renderer Technical overview](https://github.com/ViciousSquid/Fio/wiki/Renderer-Technical-overview)
