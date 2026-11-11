> Applies to Fio 3.1.0.0611 (CANARY).

# Terrain

A map can carry one procedural heightfield terrain. Its shape is a pure function of world position (a seeded Perlin-noise biome, plus an optional heightmap image, sculpt offsets and box cut-outs), so nothing but the parameters is stored in the map file. At run time the terrain is cut into square chunks; each resident chunk holds one height grid that the renderer draws on the GPU and that player collision interpolates, so the player stands on exactly the surface that is on screen.

| Concern | Owner |
|---|---|
| Height function, sculpt, heightmap, CSG cuts, paint, grass, LOD, drawing, save/load | `engine/terrain.py`: `Terrain`, `BiomeConfig`, `BIOMES`, `PerlinNoise`, `TerrainFeatures` |
| Per-chunk dense state (residency, LOD, the height grid) | `engine/terrain_table.py`: `TerrainTable` |
| Look options, presets, palettes, terracing maths, block mesh | `engine/terrain_style.py`: `TerrainAppearance` |
| `.stamp` files | `engine/terrain_stamp.py` |
| Editor panel | `editor/terrain_editor.py`: `TerrainEditorPanel` |
| 3D brush (raycast, sculpt, stamp select/drag) | `engine/qt_game_view.py` (`raycast_terrain`, `_apply_sculpt_at_mouse`, `_press_terrain_stamp`, …) |
| Lifetime in the editor, undo resync, CSG subtract, Big World preview | `editor/main_window.py` |
| Render pass | `engine/renderer/forward/passes.py`: `render_terrain` |
| Shaders | `engine/shaders.py`: `terrain.vert`, `terrain_mesh.vert`, `terrain.frag`, `grass.vert`, `grass.frag` |

A map has at most one terrain. `MainWindow.terrain` is the live `Terrain` (or `None`); `EditorState.terrain_data` is its serialised dict and is what the map file, the scene hierarchy and the undo history see.

## Using it

- **Create / open**: *Tools → Terrain Generator…*, or the *Procedural Tools* toolbar button → *Terrain Generator…* (`MainWindow.open_terrain_editor`). If the map has no terrain a `Terrain(seed=42)` is created (loading `state.terrain_data` if present). The editor opens as an overlay in the Properties dock, not as a floating window.
- **Scene hierarchy**: a map with terrain shows a *Terrain* section with one item, `Terrain (<biome name>)`. Double-click or right-click → *Edit Terrain...* opens the panel; right-click → *Delete Terrain* removes the terrain from the map.
- **Hide in the editor**: the `terrain` class in the View filters (see [View Filters and VisGroups](https://github.com/ViciousSquid/Fio/wiki/View-Filters-and-VisGroups)). It is always drawn in Play.
- **2D views**: the Top view draws the terrain bounds as a brown rectangle with its chunk grid and the label `TERRAIN` (`TERRAIN (∞)` when the bounds span more than 100000 chunks); Front/Side views draw a height profile through the centre of the terrain. A solid terrain is outlined with a solid line, a non-solid one with a dashed line (`editor/view_2d.py: View2D.draw_terrain`).
- **Cut a hole**: select a plain axis-aligned box brush and run CSG Subtract with no target brush; the box is also subtracted from the terrain (see [CSG subtraction](#csg-subtraction)).

## Data model

### Coordinates and chunking

- Chunk `(cx, cz)` covers world X `[cx * chunk_size + offset_x, (cx + 1) * chunk_size + offset_x]` and the same in Z.
- The terrain exists for chunk indices `min_chunk_x..max_chunk_x` × `min_chunk_z..max_chunk_z` (inclusive). `Terrain.get_terrain_bounds()` returns the world rectangle `((min_x, max_x), (min_z, max_z))`.
- `chunk_size` is `Terrain.DEFAULT_CHUNK_SIZE` (256.0) × `mesh_scale`. `Terrain.set_mesh_scale(factor)` (minimum 0.01) sets both, frees every chunk and rebuilds. Scaling is uniform: the generator is sampled in terrain space (`(world - offset) / mesh_scale`) and the resulting height is multiplied by `mesh_scale`, so relief keeps its proportions.

### Height function

`Terrain._get_raw_heights_batch` (vectorised) and `_get_raw_height_scalar` (one point) compute, in this order:

1. `x, z = (world - offset) / mesh_scale`.
2. `h = rolling_hills(x, z, hills_scale) * hills_intensity`
   `+ mountains(x, z, mountains_scale, mountains_sharpness) * mountains_intensity` (if `mountains_enabled`)
   `+ valleys(x, z, valleys_scale, valleys_depth)` (if `valleys_enabled`; the value is negative)
   `+ plateaus(x, z, plateaus_scale, plateaus_flatness) * plateaus_intensity` (if `plateaus_enabled`).
   The feature functions are `TerrainFeatures.get_*_batch`; they use three `PerlinNoise` generators seeded `seed`, `seed + 1000` and `seed + 2000`.
3. `h = clamp((h + 1) / 2, 0, 1)`; `base = base_height + h * height_scale + offset_y`.
4. `base += heightmap sample` (see [Heightmap overlay](#heightmap-overlay)).
5. `base += sculpt offset` (bilinear over the sculpt grid).
6. `base *= mesh_scale`.
7. CSG cuts are applied in world units.

`_get_heights_batch` / `_get_height_scalar` then apply terracing from the appearance (`terrace_mode`); that is the surface that is built, drawn and collided with.

### Biomes

`BiomeConfig` is a dataclass; `BIOMES` holds the 13 built-in presets. A new terrain starts as `DEFAULT_BIOME = 'rocky_mountains'`.

| Field | Default | Meaning |
|---|---|---|
| `name` | — | Display name |
| `base_height` | 0.0 | Height of the lowest ground, terrain-space units |
| `height_scale` | 200.0 | Height range above `base_height` |
| `hills_scale`, `hills_intensity` | 0.005, 1.0 | Rolling-hills frequency and weight |
| `mountains_enabled`, `mountains_scale`, `mountains_intensity`, `mountains_sharpness` | False, 0.002, 1.0, 1.0 | Ridge-noise mountains |
| `valleys_enabled`, `valleys_scale`, `valleys_depth` | False, 0.003, 0.5 | Valleys (subtractive) |
| `plateaus_enabled`, `plateaus_scale`, `plateaus_intensity`, `plateaus_flatness` | False, 0.004, 0.5, 0.7 | Plateaus / mesas |
| `color_gradient` | 4 stops | `[(height 0..1, (r, g, b)), …]`; the untextured ground colour. The heightfield shader uses at most `Terrain.MAX_GRADIENT_STOPS` = 32 stops |
| `trees_enabled`, `tree_density`, `blend_weights`, `terrain_height_scale` | False, 0.0, (1.0, 0.3, 0.0, 0.0), 0.003 | Saved with the biome. `trees_enabled`/`tree_density` are read only by `Terrain.generate_tree_data()`, which nothing in the editor or runtime calls; `blend_weights` and `terrain_height_scale` are not read anywhere |

Built-in presets (the last column is the default sand→grass, grass→rock, rock→snow texture boundaries from `terrain_style.BIOME_LAYER_HEIGHTS`):

| Key | Name | `base_height` | `height_scale` | Hills (scale / intensity) | Mountains (scale / intensity / sharpness) | Valleys (scale / depth) | Plateaus (scale / intensity / flatness) | Layer heights |
|---|---|---|---|---|---|---|---|---|
| `grassy_hills` | Grassy Hills | 0 | 200 | 0.005 / 1.2 | off | off | off | 0.06, 0.7, 1 |
| `low_poly_valley` | Low Poly Valley | 10 | 250 | 0.004 / 0.8 | 0.006 / 1.2 / 0.2 | 0.003 / 0.2 | off | 0.05, 0.62, 0.92 |
| `dark_cliffs` | Dark Cliffs | 0 | 350 | 0.008 / 0.5 | 0.005 / 1.8 / 0.9 | off | 0.01 / 0.5 / 0.7 | 0, 0.25, 0.9 |
| `desert_canyon` | Desert Canyon | 20 | 200 | 0.002 / 0.3 | off | 0.004 / 0.4 | 0.005 / 1.5 / 0.95 | 0.72, 0.72, 1 |
| `jagged_peaks` | Jagged Peaks | 50 | 500 | 0.005 / 0.2 | 0.004 / 1.5 / 1 | 0.002 / 0.3 | off | 0.02, 0.22, 0.72 |
| `desert` | Desert | 0 | 100 | 0.008 / 0.8 | off | off | off | 0.8, 0.8, 1 |
| `mountains` | Mountains | 0 | 500 | 0.005 / 1 | 0.002 / 1.5 / 1 | off | off | 0, 0.1, 0.65 |
| `gentle_meadow` | Gentle Meadow | 0 | 80 | 0.003 / 0.8 | off | 0.002 / 0.15 | off | 0.04, 0.85, 1 |
| `rolling_highlands` | Rolling Highlands | 20 | 150 | 0.004 / 0.7 | 0.006 / 0.5 / 0.3 | 0.003 / 0.25 | off | 0.05, 0.6, 0.92 |
| `rocky_mountains` | Rocky Mountains | 50 | 300 | 0.003 / 0.4 | 0.008 / 1 / 0.6 | 0.004 / 0.35 | off | 0, 0.22, 0.75 |
| `desert_mesas` | Desert Mesas | 10 | 180 | 0.004 / 0.5 | off | 0.003 / 0.2 | 0.005 / 0.9 / 0.85 | 0.6, 0.6, 1 |
| `alpine_forest` | Alpine Forest | 30 | 220 | 0.004 / 0.6 | 0.007 / 0.8 / 0.45 | 0.003 / 0.3 | off | 0, 0.45, 0.8 |
| `coastal_cliffs` | Coastal Cliffs | 0 | 120 | 0.005 / 0.5 | 0.01 / 0.6 / 0.7 | off | 0.008 / 0.4 / 0.6 | 0.1, 0.4, 1 |

`Terrain.set_biome(key)` assigns `BIOMES[key]` itself (not a copy), resets `appearance.layer_heights` to the biome default and marks every chunk dirty. The editor's Height and Features controls write straight into `terrain.biome`, so after `set_biome` they edit the shared preset object for the rest of the session. A terrain loaded from a map uses the private `BiomeConfig` built from its `custom_biome`.

### Sculpt offsets

`Terrain.sculpt_offsets` is a sparse dict `{(gx, gz): offset}` on a grid of `sculpt_grid_resolution` (4.0) world units; grid point `(gx, gz)` is at world `(gx * 4, gz * 4)` (not relative to the terrain offset). Sampling is bilinear with missing points reading 0. For batch sampling the dict is packed once into a dense float32 array over its bounding box (`_dense_sculpt`; up to `MAX_DENSE_SCULPT_CELLS` = 16 M cells, beyond which it falls back to per-point lookups).

| Method | Effect |
|---|---|
| `apply_sculpt_at(x, z, radius, strength)` | Adds `strength * (1 - d/radius)²` to every grid point within `radius`; negative `strength` lowers |
| `smooth_sculpt_at(x, z, radius, strength)` | Moves each point toward the mean of itself and its four neighbours by `strength * (1 - d/radius)` |
| `flatten_sculpt_at(x, z, radius, strength)` | Multiplies each stored offset by `1 - strength * (1 - d/radius)`; offsets below 0.01 in magnitude are deleted |
| `clear_sculpt()` | Removes every offset |

Each call marks the chunks overlapping the brush dirty (`TerrainTable.mark_dirty_region`); the built heights stay in place until the rebuild, so collision keeps matching the surface on screen.

### Heightmap overlay

`Terrain.load_heightmap(path)` loads an image as greyscale (Pillow, falling back to `QImage` when Pillow is missing), normalised to 0..1 float32, into `heightmap_data`. The image is stretched over `get_terrain_bounds()` (columns along X, rows along Z) and sampled bilinearly; points outside the bounds get no contribution.

| `heightmap_blend` | Contribution added in step 4 |
|---|---|
| `'additive'` (default) | `(pixel - 0.5) * heightmap_strength` |
| `'replace'` | `base_height + pixel * heightmap_strength + offset_y - procedural`, i.e. the image replaces the procedural height |

`heightmap_strength` defaults to 100.0. The pixel array itself is saved in the map (`heightmap_blob`), not the image path.

### CSG subtraction

`Terrain.csg_subtractions` is a list of world-space boxes `[min_x, min_y, min_z, max_x, max_y, max_z]`. Because the terrain is a heightfield, a cut can only lower the surface: a sample inside the box's XZ footprint whose current height is strictly between `min_y` and `max_y` is lowered to `min_y`.

`Terrain.subtract_aabb(min_corner, max_corner)` adds a cut and returns `False` (adding nothing) when the box has no volume, does not overlap the terrain bounds in XZ, or is already present. The editor calls it from the CSG Subtract operation when there is no target brush and the selected brush is a plain axis-aligned box (`brush_geometry.is_plain_aabb_brush`); a brush with custom convex geometry shows the toast "Terrain CSG requires a plain axis-aligned box brush" and is not applied to the terrain. There is no API or UI to remove a single cut other than undo.

### Texture paint and stamps

Paint is baked into the terrain: each painted chunk owns one RGBA8 map of `TERRAIN_PAINT_RESOLUTION`² = 128×128 texels (`texture_paint_maps[(cx, cz)]`), blended over the ground colour by its alpha in `terrain.frag`.

- A **stamp** is a record `{id, texture, x, z, radius, angle, feather, opacity}` (`Terrain.make_paint_stamp`; `feather` and `opacity` are clamped to 0..1, `id` defaults to a new `uuid4().hex`, the texture path uses forward slashes). The stamp is an ellipse-in-square footprint of the source image, centred at `(x, z)`, rotated by `angle` radians, with a smoothstep edge over `radius * feather` world units.
- `paint_stamps` keeps the records so the editor can select and move them; their baked result is in `texture_paint_maps`, which is what renders and what the game loads.
- `texture_paint_base` is the paint no stamp record owns (free paint, or all the paint of a map saved before stamp records existed). A chunk's paint is the base composited with every stamp over it, in placement order (`_compose_paint_chunk`).

| Method | Effect |
|---|---|
| `add_paint_stamp(x, z, radius, texture, feather=0.25, opacity=1.0, angle=0.0)` | Places and bakes a stamp on top; returns the record, or `None` when the record is invalid or the image cannot be loaded |
| `paint_texture_at(...)` (alias `stamp_texture_at`) | Bakes free paint into the base layer without a record |
| `paint_stamp_at(x, z)` | Id of the topmost stamp whose circle covers the point, or `None` |
| `set_selected_stamps(ids)` | Selected stamps are drawn tinted (`STAMP_SELECT_TINT`: orange, 45 %) from an editor-only display copy |
| `move_paint_stamps(ids, dx, dz, defer=False)` | Moves stamps and rebakes the chunks they leave and enter; `defer=True` updates only the display copy until `finish_stamp_edit()` |
| `merge_paint_stamps(records)` | Add or update by id; idempotent; returns the number changed |
| `clear_texture_paint()` (alias `clear_texture_stamps`) | Removes all paint and all stamp records |

Source images are loaded through `QImage` (`_load_texture_rgba`), so baking paint needs PyQt5; up to 64 images are cached per terrain.

**Stamp files** (`engine/terrain_stamp.py`) store only the stamp records, so they can be re-applied after the map's heights changed:

```json
{
  "format": "terrain_stamp",
  "version": 1,
  "stamps": [
    {"id": "3f2c…", "texture": "assets/textures/moss.png",
     "x": 128.0, "z": -64.0, "radius": 32.0,
     "angle": 0.0, "feather": 0.25, "opacity": 1.0}
  ]
}
```

`save_stamp_file(terrain, path)` writes atomically; `load_stamp_file(path)` raises `StampError` for unreadable JSON, a wrong `format`, a `version` outside 1..`STAMP_VERSION`, or a non-list `stamps`; `apply_stamp(terrain, data)` calls `merge_paint_stamps`. The default file name is `<map path without extension>.stamp`, or `untitled.stamp` for an unsaved map.

### Appearance

`Terrain.appearance` is a `terrain_style.TerrainAppearance`. `sanitize()` clamps every field on load and after every change, so bad map data cannot break a draw.

| Field | Default | Range / values | Meaning |
|---|---|---|---|
| `preset` | `'natural'` | preset key or `'custom'` | Name of the preset the options came from |
| `terrace_mode` | `'none'` | `none`, `smooth`, `sharp`, `blocks` | Shape of the surface; changes collision too |
| `terrace_step` | 12.0 | 1..200 | Terrain-space height of one step / block level |
| `terrace_ramp` | 0.35 | 0.05..1 | Share of each step spent on the riser (`smooth`, `sharp`) |
| `block_size` | 16.0 | 2..128 | Terrain-space footprint of a column (`blocks`) |
| `skirt` | True | bool | `blocks`: solid side walls at the terrain edge |
| `color_mode` | `'natural'` | `natural`, `palette`, `bands` | Biome gradient / textures, palette graded by height, or strata bands |
| `palette` | `'meadow'` | `forest_tiles`, `meadow`, `dusty_tiles`, `highland_strata`, `island_strata`, `canyon_strata`, `alpine`, `autumn`, `mono`, `custom` | Palette for `palette` / `bands` |
| `custom_palette` | None | 2..8 RGB colours | Colours of the `custom` palette |
| `band_height` | 12.0 | 1..200 | Terrain-space height of a colour band and contour interval |
| `layer_heights` | None | three sorted 0..1 values | Sand→grass, grass→rock, rock→snow boundaries as fractions of the terrain's height range; `None` uses the biome default |
| `layer_blend` | 0.05 | 0.005..0.3 | Half-width of each layer transition |
| `slope_rock` | 0.8 | 0..1 | How strongly steep ground turns to rock |
| `contour_lines`, `contour_width` | 0.0, 1.5 | 0..1, 0.5..6 px | Contour line darkness and width |
| `grid_lines`, `grid_size` | 0.0, 16.0 | 0..1, 1..256 | Tile grid darkness and terrain-space tile size |
| `cell_variation` | 0.0 | 0..1 | Per-tile brightness jitter |
| `wall_color`, `wall_amount`, `wall_stripes` | (0.78, 0.45, 0.16), 0.0, 0.0 | colour, 0..1, 0..1 | Cliff tint and stripes |
| `speckle_color`, `speckle_amount` | (0.12, 0.30, 0.10), 0.0 | colour, 0..1 | Shrub dots |
| `patch_color`, `patch_amount` | (0.50, 0.55, 0.30), 0.0 | colour, 0..1 | Ground patches |
| `smooth_shading` | False | bool | Light with the smooth normal instead of the flat face normal |
| `light_steps` | 0 | 0..8 | Quantised lighting bands; 0 = continuous |
| `dither_levels` | 0 | 0..32 | Dithered colour levels per channel; 0 = off |

Lengths given in terrain space (`terrace_step`, `block_size`, `band_height`, `grid_size`) are multiplied by `mesh_scale` before use.

- `Terrain.set_appearance(**options)` changes options by name (unknown names raise `AttributeError`). Changing anything other than `layer_heights`, `layer_blend` or `slope_rock` without also passing `preset` sets `preset = 'custom'`. If `shape_key()` changed, every chunk is rebuilt; otherwise the change is uniforms only.
- `Terrain.apply_appearance_preset(name)` resets every option to the preset, keeping the three texture-layer options. Presets (`terrain_style.PRESETS`): `natural`, `voxel_blocks`, `retro_tiles`, `painted_strata`, `island_terraces`, `canyon_strata`.
- `Terrain.set_palette_color(index, rgb)` and `resize_palette(count)` (2..8) turn the current palette into a custom copy.

**Terracing** (`terrain_style.terrace_heights`): for `smooth` and `sharp`, each step of height `step` is flat for `1 - ramp` of its span and climbs to the next level over the remaining `ramp` (smoothstep for `smooth`, linear for `sharp`); the result is continuous. `blocks` samples the height at the centre of the `block_size` cell containing the point and floors it to whole steps.

**Texture layers**: with *Use Textures* on and `color_mode == 'natural'`, `terrain.frag` blends `sand.jpg`, `grass.jpg`, `rock.jpg` and `snow.jpg` from `assets/textures/terrain/` by normalised height (a partition of unity built from three smoothsteps, mirrored in `terrain_style.layer_weights`), with rock on steep ground. A boundary at ≤ 0.001 or ≥ 0.999 is pushed off the range so that layer never appears. The height range used for normalisation is estimated once from a 48×48 sample grid over the authored bounds (`Terrain._layer_height_range`) and cached until the whole terrain's shape changes; sculpting does not move the layers. Textures repeat every `Terrain.TILING_SCALE` = 64 world units. Without textures the ground uses the biome `color_gradient`, normalised per chunk by that chunk's own height range.

`flat_mode` draws the terrain in uniform grey (0.7) and forces textures off; heights and collision are unchanged.

### Grass

Grass is a separate instanced pass. For each chunk the CPU scatters tufts deterministically (seeded from the terrain seed and the chunk coordinate), keeps those on ground flatter than `GRASS_MIN_NORMAL_Y` = 0.78, inside the grass height range and outside broad noise clearings, and uploads one 9-float record per blade (`x, y, z, size, yaw, tint, ground r, g, b`); the vertex shader builds and animates the blade.

| Setting | Default | Notes |
|---|---|---|
| `grass_enabled` | True on a new terrain (`DEFAULT_GRASS_ENABLED`) | |
| `grass_density` | 0.02 tufts per square world unit | Clamped to 0..`MAX_GRASS_DENSITY` (0.24) |
| `grass_height` | 5.0 world units (`DEFAULT_GRASS_HEIGHT`) | Clamped to `GRASS_HEIGHT_LIMITS` = 0.1..256; a shader uniform, no rebuild |
| `grass_color` / `grass_color_custom` | Ground-matched | With `grass_color_custom` False each blade takes the colour of the ground under it |
| `grass_tip_color` | None | None derives a sun-bleached shade of the blade colour |
| `grass_height_range` | None | `(lowest, highest)` as fractions of the height range; None follows the grass texture layer (`layer_heights[0]..layer_heights[1]`) |

Fixed parameters (instance attributes of `Terrain`): 5 blades per tuft within a 2.5-unit radius, at most 4096 tufts per chunk (the cap scales up proportionally for densities above `LEGACY_GRASS_DENSITY_MAX` = 0.06), blade half-width 0.35, detailed blades (5 segments) nearer than 700 units morphing to a single triangle from 350, fade-out from 2400 and no grass beyond 3072 units, wind strength 0.65. `Terrain.set_grass(enabled, density=None, color=None, tip_color=None, height_range=None, height=None)` is the one entry point; `color='ground'`, `tip_color='auto'` and `height_range='auto'` return to the defaults.

## Storage in the map

The terrain is the top-level `terrain_data` object of the map file (see [Map format](https://github.com/ViciousSquid/Fio/wiki/Map-format-(.json))), written by `Terrain.to_dict()` and read by `Terrain.from_dict()`. The "default on load" column is what a map that omits the key gets, which is deliberately not always the default of a newly created terrain.

| Key | Type | Default on load | Meaning |
|---|---|---|---|
| `enabled` | bool | true | Drawn and collidable at all |
| `solid` | bool | true | Player collides with it |
| `seed` | int | 42 | Noise seed |
| `biome` | string | — | Display name of the biome (informational; shown in the scene hierarchy). Only used as a preset key if it matches a key of `BIOMES` and no `custom_biome` is present |
| `custom_biome` | object | — | Full `BiomeConfig.to_dict()`; always written, and replaces the biome on load |
| `chunk_size` | float | 256.0 | World size of a chunk |
| `mesh_scale` | float | `chunk_size / 256` | Uniform scale |
| `base_resolution` | int | 48 | Saved and loaded, but not read by the mesher (see [Editor panel](#editor-panel)) |
| `offset_x`, `offset_y`, `offset_z` | float | 0.0 | World offset |
| `min_chunk_x`, `max_chunk_x`, `min_chunk_z`, `max_chunk_z` | int | -2, 2, -2, 2 | Inclusive chunk bounds |
| `use_textures` | bool | false | Height-layered textures (a new terrain starts with true) |
| `flat_mode` | bool | false | Grey untextured look |
| `grass_enabled` | bool | false | (a new terrain starts with true) |
| `grass_density` | float | 0.02 | |
| `grass_height` | float | 5.0 | |
| `grass_color` | `[r, g, b]` | biome's first gradient colour | |
| `grass_color_custom` | bool | true if `grass_color` is present | |
| `grass_tip_color` | `[r, g, b]` or null | null | |
| `grass_height_range` | `[lo, hi]` or null | null | |
| `appearance` | object | all defaults | `TerrainAppearance.to_dict()` |
| `sculpt_offsets` | `[[gx, gz, offset], …]` | none | Written only when non-empty |
| `sculpt_grid_resolution` | float | 4.0 | |
| `csg_subtractions` | `[[min_x, min_y, min_z, max_x, max_y, max_z], …]` | none | Entries that are not six numbers or have no volume are dropped on load |
| `texture_paint_resolution` | int | — | Always 128 when written; not read back |
| `texture_paint` | `[{"coord": [cx, cz], "data": "<base64 of zlib of 128·128·4 RGBA bytes>"}, …]` | none | Baked paint; chunks with no alpha are not written; an entry that does not decode to exactly 65536 bytes is skipped |
| `paint_stamps` | list of stamp records | none | |
| `texture_paint_base` | same shape as `texture_paint` | — | Written only when there are stamp records; without records the whole of `texture_paint` becomes the base layer |
| `texture_stamps` | legacy list of `{texture, bounds: [min_x, min_z, max_x, max_z], angle, feather, opacity}` | — | Read-only migration: baked into the paint once on load, never written |
| `heightmap_blob` | string | none | base64 of a `.npy` file holding the 2-D heightmap array. Loaded with `allow_pickle=False`; an unreadable, non-numeric or non-2-D blob drops the heightmap, not the map |
| `heightmap_strength`, `heightmap_blend` | float, string | 100.0, `additive` | Written only with a heightmap |

While the editor previews a Big World "fill world with terrain", `to_dict()` writes the *authored* bounds and `enabled` flag, never the widened preview.

**Undo**: editor history checkpoints carry only four terrain fields — `csg_subtractions`, `texture_paint`, `paint_stamps` and `texture_paint_base` (`editor/editor_state.py`). After an undo/redo, `MainWindow._resync_terrain_after_history` reloads the live terrain with `from_dict` if any of them differ. Sculpting, biome, size, heightmap and appearance changes are not undo steps.

## Editor panel

`TerrainEditorPanel` (`editor/terrain_editor.py`) edits the live `Terrain` directly and emits `terrain_changed`; `MainWindow.on_terrain_changed` then copies `terrain.to_dict()` into `state.terrain_data` and repaints. A 500 ms timer refreshes the stats line (`Visible: N chunks | Culled: N | Triangles: N`).

**Top row**: *Regenerate* (marks every chunk dirty), *Reset Defaults*, *Close*.

*Reset Defaults* restores the default biome, textures on, grass on, bounds −2..2 and `chunk_size` 256. It also sets the seed and offset spin boxes to 42 / 0, but does so with change handling suppressed, so the terrain's `seed`, offsets and `mesh_scale` keep their previous values until one of those controls is next edited.

**Option checkboxes**

| Control | Writes |
|---|---|
| Use Textures | `Terrain.set_use_textures` |
| Wireframe | `terrain.wireframe` (not saved) |
| Flat Mode | `terrain.flat_mode` |
| Solid | `terrain.solid` |

**Biome tab**: *Biome Preset* combo (`set_biome`) with a gradient preview; *Base Height* (−500..500, step 5) and *Height Scale* (10..500, step 10) write `biome.base_height` / `biome.height_scale`; *Random Seed* (0..999999) with *Randomize* (`set_seed`).

**Appearance tab**: one control per `TerrainAppearance` option.

| Group | Controls |
|---|---|
| Look | *Preset* (the six presets plus *Custom*) |
| Shape | *Terracing* (Off, Smooth Terraces, Sharp Terraces, Blocks), *Step Height*, *Riser Share* (%), *Block Size*, *Solid sides at the terrain edge (Blocks)* |
| Colour | *Colour Source* (Natural (biome / textures), Palette by Height, Strata Bands), *Palette*, up to eight colour swatches with −/+ to change the count, *Band Height* |
| Texture Height Layers | *Sand → Grass*, *Grass → Rock*, *Rock → Snow* (%), *Blend Width*, *Rock on Slopes*, *Biome Defaults* (sets `layer_heights` back to None) |
| Surface Detail | *Contour Lines*, *Line Width (px)*, *Tile Grid*, *Tile Size*, *Tile Variation*, *Cliff Colour / Tint / Stripes*, *Shrub Colour / Dots*, *Patch Colour*, *Ground Patches* |
| Shading | *Smooth shading*, *Light Bands*, *Colour Depth* |

**Features tab**: *Grass* (Enable Grass; Height; Density slider in steps of 0.0001 shown as a percentage of the 0.02 default; Blade Colour / Match Ground; Tip Colour / Auto; Lowest / Highest growth height; Follow Texture Layers), then *Rolling Hills* (Scale 0.001..0.05, Intensity 0..1.5), *Mountains* (enable, Scale 0.001..0.05, Intensity 0..2, Sharpness 0..1), *Valleys* (enable, Scale 0.001..0.02, Depth 0..1), *Plateaus / Mesas* (enable, Scale 0.001..0.02, Intensity 0..1.5, Flatness 0..1). These write the corresponding `BiomeConfig` fields and mark every chunk dirty.

**Size tab**

- *World Size (Chunks)*: Min/Max X and Z, each −20..20 → `Terrain.set_bounds`. *Size Presets*: Tiny (1×1), Small (3×3), Medium (5×5), Large (7×7), Huge (11×11).
- *Triangle Size → Vertices per Chunk* (4..64) and its presets (Very Chunky 8, Chunky 12, Medium 20, Smooth 32) write `terrain.base_resolution`. The mesher does not read `base_resolution`: chunk resolution comes only from `Terrain.LOD_RESOLUTIONS`, so this control marks chunks dirty and changes the saved value but not the geometry. The spin box is always initialised to 16 when the panel opens.
- While Big World "Fill world with terrain" owns the bounds, the bounds controls and size presets are disabled and a note is shown (`set_bigworld_managed`).

**Scale tab**: *Physical Mesh Scale* buttons 1×, 2×, 4×, 8×, 16× → `set_mesh_scale`. *Tiling Scale* buttons multiply the current chunk bounds by 2, 4 or 8 (limited by the −20..20 spin range); *Reset Grid* applies −2..2.

**Position tab**: *X Offset*, *Z Offset* (−10000..10000, step 50), *Y Offset* (−500..500, step 5).

**Heightmap tab**: *Load Image…* (png, jpg, jpeg, bmp, tif, tiff), *Clear*, *Strength* (1..2000), *Blend Mode* (Additive / Replace).

**Sculpt/Stamp tab**. The 3D brush is active only while this tab is the visible tab of the visible panel and the editor is not in Play (`_sync_terrain_brush_activation` → `QtGameView.set_terrain_sculpt_active`). The cursor becomes a cross and an orange circle shows the brush footprint where the mouse ray hits the terrain.

| Control | Range / default | Effect |
|---|---|---|
| Mode | Sculpt / Stamp | Selects the tool |
| Brush Size | 8..512, default 64 | World-space *diameter*; the radius passed to the terrain is `max(4, size / 2)` |
| Tool (Sculpt) | Raise / Lower / Flatten | |
| Strength (Sculpt) | 1..200, default 20 | Raise/Lower: `apply_sculpt_at(±strength)`. Flatten: `flatten_sculpt_at(min(strength / 20, 1))` |
| Clear Sculpt | | `clear_sculpt()` |
| Feather (Stamp) | 0..100 %, default 25 | Stamp edge softness |
| Clear Texture Paint | | `clear_texture_paint()` as one undo step |

Mouse and keys in the 3D view while the brush is active (`engine/qt_game_view.py`):

- **Sculpt**: each left click applies the tool once at the hit point. Holding and dragging does not repeat it.
- **Stamp**: left click bakes the texture selected in the Asset Browser as a new stamp (radius = brush radius, opacity 1, angle 0). The texture is stored relative to the project root when it lies inside it. With no texture selected a toast asks to select one. Each click is one undo step.
- **Select stamps**: Shift+click a stamp adds it to the selection; Shift+click a selected stamp (without dragging) removes it; Shift+click empty ground clears the selection.
- **Move stamps**: press on a selected stamp and drag; all selected stamps move together and the drag is one undo step. Arrow keys nudge the selection by one grid step of the active 2D view (16 units when its grid is hidden), ×5 with Shift, Up/Down along −Z/+Z, snapping the first selected stamp to the grid when the grid is visible; a run of key repeats within 350 ms is one undo step (`MainWindow._nudge_terrain_stamps`).
- The disk button on the tab offers *Import stamp…* and *Export stamp…* (`.stamp` files). Import is one undo step and reports "Stamp already applied — nothing changed" when nothing was merged.

`QtGameView.raycast_terrain` marches the mouse ray up to 5000 units (4-unit steps, 8 beyond 200, 16 beyond 500) and refines the first crossing with 12 bisection steps. The view also implements a `'smooth'` brush mode (`smooth_sculpt_at`), but no panel control selects it.

## Runtime

### TerrainTable

`TerrainTable` is the dense, GL-free projection of the terrain: one row (slot) per resident chunk, in parallel NumPy columns. It is a cache — discarding it and streaming again reproduces it exactly.

| Column | dtype | Meaning |
|---|---|---|
| `coord` | int64 ×2 | Chunk index `(cx, cz)` |
| `key` | int64 | `coord` packed (`pack_keys`) for vectorised membership tests |
| `live` | bool | Slot in use |
| `order` | int64 | Allocation counter (tie-break for the build queue) |
| `world` | float64 ×2 | World origin of the chunk, frozen at allocation |
| `size` | float64 | Edge length, frozen at allocation |
| `dirty` | bool | Heights no longer match the height function |
| `built` | bool | A grid has been built |
| `lod` | int8 | Index into `LOD_RESOLUTIONS` the grid was built at |
| `target_res`, `stable` | int32 | LOD hysteresis state |
| `grid_res` | int32 | Quads per side of the built grid |
| `min_y`, `max_y` | float64 | Height span of the drawn grid |
| `version` | int64 | Bumped on every build; the renderer re-uploads a slot whose version it has not seen |
| `seq` | int64 | Sequence lock; odd while the row is being written or is free |
| `grass_dirty` | bool | Grass instances need rebuilding |
| `heights` | float32 51×51 | The height grid. Drawn point `(i, k)` is `heights[i + 1, k + 1]`; the one-sample border ring (`GRID_BORDER` = 1) exists only so normals at chunk edges match the neighbour |

`MAX_GRID` = 49 (LOD 0 has 48 quads per side), `STORED_GRID` = 51. `slot_of_coord` maps `(cx, cz)` to a slot. `generation` is bumped when the columns are reallocated. Rows whose frozen `world`/`size` no longer match the terrain's `chunk_size`/offset are released each frame by `prune_stale_geometry` and re-created.

Threading: the table has one writer (the thread that builds chunks, i.e. the render thread inside `Terrain.update_and_render`). `TerrainTable.height_at` is safe from any thread: it reads `seq` and `generation` before and after and returns `None` if either changed, if the row is being written, or if the chunk is not built.

### Residency, LOD and culling

All of this runs inside `Terrain.update_and_render` each frame:

1. **Residency.** Without streaming, `TerrainTable.ensure_bounds` makes every chunk inside the bounds resident. With `Terrain.streaming` on, `TerrainTable.stream` keeps every in-bounds chunk whose nearest point is within `stream_radius` (default 4096) of the camera in XZ and evicts chunks beyond `stream_radius + stream_evict_padding` (512).
2. **Culling.** `TerrainTable.visible` tests each chunk's box (measured height span ± 10 for built chunks; unbounded height for unbuilt ones) against the frustum planes the renderer passes in. Those are the same planes the entity passes cull against and their far plane is the view distance. Culling decides only what is drawn; mesh upkeep runs for every resident chunk.
3. **LOD.** `TerrainTable.schedule` picks a target resolution per chunk from the squared XZ distance to the chunk's nearest point:

   | Distance to nearest point | Quads per side |
   |---|---|
   | inside the protected zone (`NEAR_DETAIL_RADIUS` 4096 + one chunk size; capped to `stream_radius` when streaming) | 48 |
   | < 4608 | 48 |
   | 4608 – 6144 | 32 |
   | 6144 – 8192 | 16 |
   | ≥ 8192 | 8 |

   A change of LOD outside the protected zone must hold for `LOD_HYSTERESIS_FRAMES` = 10 consecutive frames; a promotion into the protected zone is immediate.
4. **Build queue.** Dirty or unbuilt chunks first, then visible ones, then nearest. At most `MAX_UPDATES_PER_FRAME` = 2 chunks are built per frame, and the second only if less than `UPDATE_BUDGET_MS` = 4 ms has elapsed; one is always built. Grass has its own queue with the same per-frame cap.

In the editor, a camera-streamed (Big World) map turns streaming on at load with the stream's activation radius. In Play, the Big World plugin drives `set_world_extent` / `set_streaming` and restores the authored values on stop; see [BigWorld Plugin](https://github.com/ViciousSquid/Fio/wiki/BigWorld-Plugin).

### Rendering path

`ForwardRenderer` draws the terrain in `render_terrain` (`engine/renderer/forward/passes.py`) when `config['terrain']` is a `Terrain` with `enabled` set. The 3D view passes `None` there when the `terrain` view-filter class is hidden outside Play.

1. The renderer binds its own `terrain` program to the terrain (`setup_terrain_shader`) and loads the four ground textures. `Terrain.release_renderer_resources()` drops those names before a renderer is retired.
2. Lights: the terrain shader takes at most `shaders.MAX_LIGHTS_TERRAIN` = 8 lights; when more are in range the 8 nearest the camera are used. Up to `MAX_SHADOW_LIGHTS` = 8 shadow cube maps are bound (a 1×1 placeholder fills unused units). Fog and ambient values are handed over as `env_uniforms`.
3. `Terrain.update_and_render` draws every visible built chunk.
   - **Heightfield** (all modes except `blocks`): there is no vertex buffer. Each chunk's height grid is one layer of an `R32F` `GL_TEXTURE_2D_ARRAY` page (up to 512 layers per page, 51×51 texels), and the chunk is drawn with `glDrawArrays(GL_TRIANGLES, 0, 6 * res * res)` on an empty VAO; `terrain.vert` rebuilds position, flat normal, colour, UV and smooth normal from the grid. Paint maps live in `RGBA8` array pages of 128×128.
   - **Blocks**: each chunk has a 14-float-per-vertex mesh (`terrain_style.build_block_mesh`: position 3, normal 3, colour 3, uv 2, smooth normal 3) drawn with `terrain_mesh.vert`.
4. Chunk builds for this frame, then the grass pass.

`Terrain.gl_context` is set by the 3D view to a callable that makes its GL context current, because the terrain deletes GL objects from the GUI thread (opening a map, the terrain editor). A `Terrain` can be constructed with no GL context; its programs are compiled at first draw. Wireframe draws the terrain with `GL_LINE` polygon mode.

After drawing, `terrain.visible_chunks`, `culled_chunks`, `total_triangles` and `drawn_slots` describe the frame. The System Monitor adds terrain triangles to its totals, and the Debug Tables tool can show the `TerrainTable` (see [Debug Tables](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables)).

### Collision and physics

| API | Returns |
|---|---|
| `Terrain.is_solid()` | `enabled and solid` |
| `Terrain.get_height_at(x, z)` | Height of the built surface at a world point: the table's height grid interpolated over the same two triangles per quad the renderer draws. Falls back to the height function when the chunk is not resident or not built. In `blocks` mode the point is first moved to its block centre |
| `Terrain.get_height_at_safe(x, z)` | `None` when the terrain is disabled or the point is outside the bounds, else `get_height_at` |

The only engine consumer is `engine/player.py: Player.update`, called from the logic thread with `logic.world_runtime.terrain` (`engine/logic_player.py`, for both players in split-screen). After brush collision, if the terrain `is_solid()` and the player's feet are below `get_height_at_safe(pos.x, pos.z)`, the player is placed on the surface, vertical velocity is zeroed, `on_ground` is set and `ground_object` is the terrain. This is a vertical clamp only: there is no sideways collision with steep terrain. Monsters, dynamic physics bodies, hitscan and projectiles do not test against the terrain. The Deathmatch plugin additionally samples `get_height_at_safe` for bot navigation (`plugins/deathmatch/navigation.py`).

The editor assigns the terrain to `logic_thread.world_runtime.terrain` whenever it creates, loads or deletes one.

## Extending

- A new biome is a new `BiomeConfig` entry in `BIOMES` plus a row in `terrain_style.BIOME_LAYER_HEIGHTS`; the editor combo is filled from `BIOMES`.
- A new look preset is an entry in `terrain_style.PRESETS` and `PRESET_LABELS`; a new palette is an entry in `PALETTES` and `PALETTE_LABELS`.
- A new appearance option needs a `TerrainAppearance` field (clamped in `sanitize`), and if the shader reads it, a name in `terrain_style.UNIFORM_NAMES` and a value in `shader_uniforms`. Options that change geometry must be part of `shape_key()`.
- Any change to the height function must go through both the scalar and the batch path; they are required to agree.

## Sample maps

| Map | Terrain |
|---|---|
| `maps/Terrain_Test_small.json` | Alpine Forest, 5×5 chunks of 256, textures and grass on, smooth terraces with strata bands |
| `maps/Terrain_Test_small2.json` | Rocky Mountains, 5×5 chunks of 256, textures and grass on, natural look, baked texture paint on 13 chunks |
| `maps/Terrain_Test_medium.json` | Desert Canyon, 9×9 chunks at `mesh_scale` 2 (chunk size 512), blocks with a height palette |
| `maps/Terrain_Test_medium2.json` | Desert Canyon, 9×9 chunks at `mesh_scale` 2, sharp terraces with a height palette |
| `maps/Topdown_example.json` | Low Poly Valley, 11×11 chunks, `flat_mode` |
| `maps/BigWorld_streaming_test.json` | Grassy Hills with sculpt offsets, used with Big World streaming |

`tools/generate_noise.py` and `tools/generate_tilemap.py` are unrelated to terrain: the first writes a 3-D noise volume to `assets/noise_3d.bin`, the second builds a tile map from a level's brushes.

## Tests

| File | Covers |
|---|---|
| `tests/engine/test_terrain_style.py` | Terracing, layer weights, look options (headless) |
| `tests/engine/test_terrain_csg.py` | AABB cuts |
| `tests/engine/test_terrain_kernel_equivalence.py`, `tests/engine/test_block_mesh_equivalence.py` | Optimised kernels against frozen references (`tests/helpers/block_mesh_reference.py`) |
| `tests/engine/test_terrain_appearance.py` | Terraced collision, grass placement, save/load |
| `tests/engine/test_terrain_stamp.py`, `test_terrain_stamp_oracle.py` | Stamp records, `.stamp` files, an independent compositing oracle |
| `tests/engine/test_terrain_load_hardening.py`, `test_heightmap_image_loading.py`, `test_terrain_without_context.py` | Damaged records, image import, construction without GL |
| `tests/renderer/test_terrain_table.py` | `TerrainTable` against the frozen per-chunk pipeline in `tests/helpers/terrain_reference.py` |
| `tests/renderer/test_terrain_defaults.py`, `test_terrain_sculpt_sampling.py`, `test_terrain_streaming_cull.py` | Defaults, batched sculpt sampling, streaming and culling (`gl`) |
| `tests/visual/test_terrain_heightfield_parity.py`, `test_terrain_seamless_normals.py`, `test_terrain_textures.py`, `test_terrain_appearance_render.py`, `test_terrain_grass.py` | Rendered output on a real GL context |
| `tests/editor/test_terrain_panel_layout.py`, `test_terrain_sculpt_cursor.py`, `test_terrain_stamp_editing.py`, `test_grass_controls.py`, `test_hierarchy_terrain_double_click.py` | The panel and the 3D brush |
| `plugins/bigworld/tests/test_bigworld_terrain_stream.py` | Big World terrain streaming |
| `tests/performance/test_terrain_kernel_benchmark.py`, `test_block_mesh_benchmark.py`, `test_heightmap_loading_benchmark.py` | Opt-in benchmarks |

`tests/helpers/terrain_reference.py` is a frozen port of the pre-`TerrainTable` pipeline (CPU chunk mesh builder, the old pass-through vertex shader, per-chunk residency/visibility/LOD logic). Parity tests compare the live engine against it and it must not be "fixed". See [Testing and CI](https://github.com/ViciousSquid/Fio/wiki/Testing-and-CI) for how to run each tier.
