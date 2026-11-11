> Applies to Fio 3.5.0.1111.

A Fio map is one UTF-8 JSON document. It is the serialised form of `editor/editor_state.py: EditorState` and is loaded directly by the editor, by Play and by the standalone player; there is no compiled or baked level format. This page is the schema of **map version 5**, the version this build writes: every top-level key, the brush and entity records, what is checked on load, what loading migrates from older versions, and what is rewritten on save.

## Versions

| `version` | Constant | What changed |
| --- | --- | --- |
| absent, 1, 2 | | Treated as "older than 3"; every migration below applies. |
| 3 | | Stable entity ids and the logic graph layout. An angled brush's tagged faces may carry their material twice (on the plane and in the brush's per-tag objects). Planes are in world space. All maps shipped in `maps/` are version 3 files and are migrated when loaded. |
| 4 | `engine.brush_faces.CANONICAL_MAP_VERSION` | One material record per face: a box face's in the per-tag objects, an angled face's on its plane only. Planes are still in world space. **This is the version written into a `.fiopak` package** (`engine.brush_placement.export_world_level`). |
| 5 | `engine.brush_placement.MAP_VERSION` | An angled brush's planes are stored in the brush's **local frame**, centred on their own bounds; `pos` places them. Play-state keys are no longer written or read. Written by every save. |

A missing, non-numeric or zero `version` is read as 0 (`EditorState.is_legacy_face_layout`, `is_world_plane_layout`). There is no upper check: a document with a higher number is read as version 5.

## Reading and writing

| Operation | Code | Notes |
| --- | --- | --- |
| Save | `MainWindow.save_level` → `engine.fileio.write_json_atomic(path, EditorState.get_level_data(), indent=4)` | The document is serialised first, written to a `.fio-<hex>.tmp` sibling, fsynced and moved over the destination with `os.replace`. A failed save never truncates the previous file. |
| Autosave | `MainWindow._write_autosave` | Writes `maps/<map name>_autosave.json` (`untitled_autosave.json` for an unsaved level) every `[Editor] autosave_interval` minutes (default 10) while `[Editor] autosave_enabled` is true (default). Also written when a level change during Play replaces a level with unsaved edits. The level's own file and its unsaved flag are left alone. |
| Load | `MainWindow.load_level_file` → `_load_level` → `EditorState.validate_level_data` → `_apply_level_data` → `EditorState.load_from_data` | The whole document is validated and parsed before the open level is touched. A document that fails leaves the current level, its file path and its unsaved flag unchanged. |
| Validate | `engine/level_validation.py: validate_level(level_data, *, partial=False, where="map")` | Structural checks only; raises `ValueError` naming the field. No Qt or editor imports, so `engine.savegame` and the player use it too. `partial=True` (save-game deltas) requires nothing but checks whatever is present. |
| Package export | `editor/package_exporter.py: PackageExporter._player_layout` | A version-5 map is written into the package as its version-4 layout. See [.fiopak archive](https://github.com/ViciousSquid/Fio/wiki/.fiopak-archive). |
| Export for older builds | `engine.brush_faces.export_legacy_level(level)` | Returns `(level, lossy)`: a version-3 copy (through version 4 first). `lossy` lists, per brush id, what version 3 cannot express (`'#i.uv_natural'` for a Natural cut face, `'tiling'` for an angled brush with `texture_tiling`). Not used by any save path. |

Coordinates are world units in a right-handed, Y-up system. The face names used throughout are: `east`/`west` = +X/−X, `top`/`down` = +Y/−Y, `north`/`south` = +Z/−Z (`engine/brush_geometry.py: _BOX_FACES`).

## Top-level keys

| Key | Type | Written | Read | Meaning |
| --- | --- | --- | --- | --- |
| `version` | int | Always, as `5` | Yes | Selects the load-time migrations (see [Versions](#versions) and [What loading migrates](#what-loading-migrates)). |
| `brushes` | list of objects | Always | Yes | World geometry. Absent or `null` is treated as empty. |
| `things` | list of objects | Always | Yes | Entities. Absent or `null` is treated as empty. Entities marked `_cutscene_temporary` (live Cutscene Wizard actors) are never written. |
| `created` | string | Always | Yes | ISO-8601 timestamp (seconds precision) of the first save. Set once and carried forward. Absent in older maps (kept as `''` until the next save sets it). |
| `items` | object | Only when a custom item slot differs from its shipped default | Yes | Definitions of the custom item slots, keyed `custom1` / `custom2`. See [Items](https://github.com/ViciousSquid/Fio/wiki/Items). |
| `terrain_data` | object | Only when the map has terrain | Yes | The terrain's configuration and edits. See [Terrain data](#terrain-data). |
| `visgroups` | list of objects | Only when the map has visgroups or an enabled cordon | Yes | Editor visibility groups. See [View Filters and VisGroups](https://github.com/ViciousSquid/Fio/wiki/View-Filters-and-VisGroups). |
| `cordon` | object | Together with `visgroups` | Yes | The editor cordon box. |
| `lightmap_scene_hash` | string | Only when a bake has produced a hash | Yes | Restored into `BakeState.scene_hash`. A freshly loaded scene is still marked dirty, so the bake runs at least once before the first Play. |
| `logic_graph` | object | Only when node positions exist | Yes | Layout of the [Logic Graph Editor](https://github.com/ViciousSquid/Fio/wiki/Logic-Graph-Editor). Editor data only; it does not affect I/O behaviour. |
| `required_plugins` | list of strings | Never (not written by the editor) | Fio Player only | Plugin names the standalone player must enable before building the world (`player/world.py: PlayerWorld.__init__`), in addition to the package manifest's `plugins`. A name that is not installed stops the load with `This world requires an installed plugin: <name>`. |
| `plugin_config` | object | Never | Fio Player only | Per-plugin settings, keyed by plugin name, merged into that plugin's `config` when it is required. |

Unknown top-level keys are ignored by the editor on load and are not written back. This includes `required_plugins` and `plugin_config`: a map carrying them loses them when it is saved from the editor.

### Top-level validation

`validate_level` refuses the document when:

* it is not a JSON object;
* `brushes` or `things` is present and is not a list, or an element is not an object;
* `items` is present and is not an object, has a key that is not `custom1`/`custom2` (the built-in `gun1`/`gun2` cannot be redefined), or a definition is not an object. A definition whose settings are wrong still loads: it is kept as authored and its slot resolves to no item until fixed.

## Brush records

A brush is a plain JSON object. In memory it is the same `dict`, with runtime caches added under `_`-prefixed keys. There are two shapes:

* a **box**: `pos` (centre) and `size` (full extents), six faces named by tag;
* an **angled (convex) brush**: `pos` and a `geometry.planes` list. The solid is the intersection of the planes' half-spaces.

```json
{
    "id": "78c5f272-ced6-41fa-a61f-3eb40576479c",
    "pos": [0.0, -8.0, 0.0],
    "size": [1600.0, 48.0, 1600.0],
    "textures": {
        "north": "Dev/512.jpg", "south": "Dev/512.jpg",
        "east": "Dev/512.jpg", "west": "Dev/512.jpg",
        "top": "Dev/512.jpg", "down": "Dev/512.jpg"
    }
}
```

### Structural fields

| Field | Type | Required | Default when absent | Meaning |
| --- | --- | --- | --- | --- |
| `pos` | `[x, y, z]` finite numbers | Yes | — | The authored placement. A box's centre; for an angled brush, the origin of its local frame (the centre of its planes' bounds). Play never writes it: a mover's or door's current position is runtime state (`engine/mover_table.py`), not map data. |
| `size` | `[x, y, z]` finite numbers | Yes for a box; optional when `geometry.planes` is non-empty | — | A box's full extents. For an angled brush it is a derived copy of the planes' bounds: it is written on save, ignored on load and re-derived from the planes (`brush_placement.normalise_loaded` → `brush_geometry.sync_brush_bounds`). |
| `id` | string | No | A new UUID4 is assigned on load | Stable identity. I/O connections, undo, save games and the render tables key on it. |
| `name` | string | No | none | Name used by I/O targets and console commands. Must not be a list or object. |
| `geometry` | object | No | none | `{"planes": [...]}`. Present on an angled brush. See below. |
| `textures` | object | No | none | **Box only.** Texture per face tag (`north`, `south`, `east`, `west`, `top`, `down`), as a path relative to `assets/textures/` (`"default.png"`, `"Dev/512.jpg"`). A face with no entry is drawn with `default.png`. |
| `uv_scale` | object | No | fitted | **Box only.** Per face tag, `[u_repeat, v_repeat]`. A face with no entry is fitted (one repeat across the face) unless it is Natural or the brush has `texture_tiling`. |
| `uv_shift` | object | No | `[0, 0]` | **Box only.** Per face tag, `[u, v]` texture shift. |
| `uv_angle` | object | No | `0` | **Box only.** Per face tag, texture rotation in degrees. |
| `uv_natural` | object | No | `{}` | **Box only.** Per face tag, `true` when the face is in Natural mode: repeats are derived from the face's size and the texture's pixel size (a constant texel size) instead of a stored scale. |
| `texture_tiling` | bool | No | `false` | Brush-wide: every face with no scale of its own is treated as Natural. Applies to boxes and angled brushes. |
| `operation` | string | No | none | `"subtract"` marks a subtractive brush; the editor's CSG tools write `"add"` on the pieces they create. |
| `io_connections` | list of objects | No | `[]` | Output connections; see [I/O connections](#io-connections). Written only when the brush has at least one. |

The special texture names: `caulk.jpg` (`engine/render_table.py: TEX_SKIP`) is never drawn; `nodraw.jpg` (`TEX_NODRAW`) is drawn in the editor and skipped in Play. A brush whose faces are all `default.png` or `caulk.jpg` is "untextured" and drawn in its `colour` (`engine.brush_faces.is_textured`).

On an angled brush the per-tag objects are not a face's record. Entries for tags that one of its planes carries are removed on load (`engine.brush_faces.drop_tag_copies`) without being consulted. Entries for tags no plane carries are left alone; they come back into use if the brush is reduced to a box.

Validation of a brush record (`_validate_record`): `pos` must be present and `size` must be present unless `geometry.planes` is a non-empty list; both, when present, must be 3-element lists of finite, non-boolean numbers. `textures`, `uv_scale`, `uv_angle`, `uv_shift`, `uv_offset`, `uv_natural` and `geometry`, when present, must be objects. `door_speed`, `door_distance`, `door_lip`, `speed` and `distance`, when present, must be finite numbers. Any key ending in `_pos` must be a 3-vector or `null`; `direction` must be a 3-vector or `null`. `name` and `id` must not be a list or object. `io_connections` must be a list of objects. The contents of `geometry.planes` are not validated structurally: a plane set that does not enclose a solid is "invalid" and the brush falls back to its `pos`/`size` box wherever its shape is needed (`engine.brush_placement.shape_convex`).

### Angled brushes: `geometry.planes`

A brush that has been clipped, rotated or edited in vertex/edge/face mode carries a plane set. The planes are the source of truth for its shape; the render mesh, the 2D outline, the collision mesh and `size` are derived from them.

**Local frame.** In version 5 each plane is expressed relative to the brush's `pos`. A world point `p` is inside the brush when, for every plane, `dot(n, p - pos) <= d`. The planes are centred on their own bounds, so the centre of the local shape is the origin and `pos` is the centre of the brush's world bounding box. Moving the brush changes `pos` only; the planes (and the solved shape cached from them) are untouched. The module that owns placement is `engine/brush_placement.py`; the shape solver is `engine/brush_geometry.py: ConvexGeometry`; the design record is `docs/brush-model-stage2.md`.

This wedge is a 400 x 200 x 300 box at `[-400, 100, 0]` cut by one sloped plane, as the editor saves it:

```json
{
    "id": "37d98a48-0f7d-46a5-88cb-9cb28dca42aa",
    "pos": [-400.0, 100.0, 0.0],
    "size": [400.0, 200.0, 300.0],
    "geometry": {
        "planes": [
            {"n": [1.0, 0.0, 0.0],  "d": 200.0, "texture": "Dev/256.jpg", "face": "east"},
            {"n": [-1.0, 0.0, 0.0], "d": 200.0, "texture": "Dev/256.jpg", "face": "west"},
            {"n": [0.0, 1.0, 0.0],  "d": 100.0, "texture": "Dev/256.jpg", "face": "top"},
            {"n": [0.0, -1.0, 0.0], "d": 100.0, "texture": "Dev/256.jpg", "face": "down"},
            {"n": [0.0, 0.0, 1.0],  "d": 150.0, "texture": "Dev/256.jpg", "face": "north"},
            {"n": [0.0, 0.0, -1.0], "d": 150.0, "texture": "Dev/256.jpg", "face": "south"},
            {"n": [-0.4472135955, 0.894427191, 0.0], "d": 0.0,
             "texture": "Dev/256.jpg", "uv_scale": [0.0078125, 0.0078125]}
        ]
    }
}
```

The same brush in a version 3 or 4 file has world-space offsets (`east` `d = -200`, `west` `d = 600`, `top` `d = 200`, `down` `d = 0`, the cut `d = 268.328...`).

Plane record fields (the keys `brush_geometry._plane_to_json` writes whenever the editor commits a brush's planes):

| Plane field | Type | Written | Meaning |
| --- | --- | --- | --- |
| `n` | `[x, y, z]` | Always | Outward unit normal. |
| `d` | number | Always | Plane offset in the brush's local frame: the plane is `dot(n, q) == d` for local points `q`; the solid is the side `dot(n, q) <= d`. |
| `face` | string | When set | The box tag (`east`, `top`, ...) of the box side this plane came from. A plane without one is a *cut face*. The tag is identity only: it names the face in the editor and says where the material goes if the brush is reduced to a box. |
| `texture` | string | When set | The face's texture. Absent means `default.png`. |
| `uv_scale` | `[u, v]` | When set | Repeat factors. Absent means fitted, or Natural under `texture_tiling`. |
| `uv_shift` | `[u, v]` | When set | Texture shift. |
| `uv_angle` | number | When set | Texture rotation in degrees. |
| `uv_natural` | `true` | Only when true | Natural mode for this face. |
| `uv_u`, `uv_v` | `[x, y, z]` | When set | The face's texture basis vectors. Present on the two X-facing sides of a converted box (so they keep the box's texture orientation) and on faces that have been rotated; otherwise the basis is derived from the dominant axis of the normal (`brush_geometry.render_uv_axes`, `plane_uv_axes`). |

Every face of an angled brush, tagged or cut, has exactly one material record, and it is on its plane (`engine/brush_faces.py`; `FIELDS` = `texture`, `uv_scale`, `uv_shift`, `uv_angle`, `uv_natural`). Code reads and writes a face through `brush_faces.face_material(brush, key)` / `set_face_material(brush, key, ...)`, where `key` is a box tag or, for a cut face, `"#<plane index>"` (`brush_geometry.face_key`).

Conversions keep the look of the brush: `brush_geometry.box_to_geometry` moves each tag's record onto its plane and drops the per-tag copies; `brush_geometry.drop_geometry` (used by `EditorState.reset_brush_to_box` and `simplify_brush_geometry` when the planes form an axis-aligned box) moves the tagged planes' records back into the per-tag objects.

Limits enforced by the geometry kernel when a component edit rebuilds a brush: at most `MAX_HULL_POINTS` = 40 corners, and no extent beyond `MAX_BRUSH_EXTENT` = 32768 units. `EPS` = 1e-4 world units is the weld and on-plane tolerance.

### Behaviour and appearance fields

These keys are optional. The table gives the value the runtime uses when the key is absent. Their behaviour is described on the pages for [triggers, movers and doors](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System) and the [Property editor](https://github.com/ViciousSquid/Fio/wiki/Property-editor).

| Field | Type | Runtime default | Read by |
| --- | --- | --- | --- |
| `hidden` | bool | `false` | Rendering and collision. |
| `lock` | bool | `false` | Editor only: the brush cannot be selected or edited in the viewports. |
| `disabled` | bool | `false` | Triggers and player interaction skip a disabled brush. |
| `colour` | `[r, g, b]` floats 0..1 | `[0.8, 0.8, 0.8]` | Flat brush colour. |
| `tint` | `[r, g, b]` ints 0..255 | none | Tint applied over the brush (`tint` console command). |
| `lightmap_static` | bool | `false` | Participates in lightmap baking (`EditorState.get_static_brushes`). |
| `brush_group` | string | `''` | Group name shared by a door/mover and its companion brushes. |
| `shader` | string | `'<None>'` | One of `<None>`, `Glass`, `Glow`, `Water`, `Fog`. |
| `is_fog` | bool | `false` | Fog volume (also selected by `shader == "Fog"`). |
| `fog_density`, `fog_color`, `fog_noise_scale` | number, `[r,g,b]` 0..1, number | `0.01`, `[0.5, 0.6, 0.7]`, `0.01` | Fog volumes (`RenderTable._resolve_special_rows`). The Property editor writes `fog_density: 2.0` when the Fog shader is selected, so an editor-made fog brush always carries the key. |
| `glass_opacity`, `glass_distortion`, `glass_refraction`, `glass_roughness`, `glass_fresnel`, `glass_color` | numbers, `[r,g,b]` 0..1 | `0.3`, `0.5`, `1.5`, `0.0`, `0.5`, `[0.7, 0.85, 0.95]` | Glass shader (`engine/render_table.py`). |
| `water_opacity`, `water_distortion`, `water_refraction`, `water_roughness`, `water_fresnel`, `water_tint` | numbers, `[r,g,b]` 0..1 | `0.5`, `0.5`, `1.333`, `0.0`, `0.65`, `[0.0, 0.4, 0.6]` | Water shader. `water_fresnel` falls back to the older key `water_reflectivity`. |
| `water_plane`, `water_wave_enabled`, `water_wave_height`, `water_wave_spread`, `water_high_quality` | bool, bool, number, number 0..1, bool | `false`, `true`, `0.5`, `1.0`, `true` | Water surface options. `water_wave_spread` (wave spacing) is clamped to 0..1. |
| `glow_intensity` | number | `10.0` | Glow shader. The emitted colour is `tint`, else `colour` (white when neither is set), multiplied by the intensity, each channel capped at 10 (`engine/render_table.py`). The Property editor's slider shows `3.0` for a brush without the key. |
| `glow_color` | `[r,g,b]` 0..1 | — | Written by the Property editor's Shader tab (editor default `[1.0, 0.9, 0.7]`). The runtime does not read it; see `glow_intensity`. |
| `is_trigger` | bool | `false` | Trigger volume. |
| `trigger_type` | string | `'multiple'` | `Once` or `Multiple` (compared case-insensitively). |
| `trigger_activation` | string | `'touch'` | `touch` or `use`. |
| `trigger_action` | string | `'target'` | `target`, `hurt` or `teleport`. |
| `trigger_filters` | list of strings | `['player']` | Which activators the trigger accepts. |
| `trigger_poll_interval` | number (s) | `1.0` | |
| `trigger_save` | string | `'none'` | Checkpoint save performed when the trigger fires. |
| `use_label`, `use_radius` | string, number | `''`, `96.0` | Prompt and reach for `use` triggers. |
| `hurt_amount` | number | `10` | Damage dealt by a trigger whose `trigger_action` is `hurt`. Falls back to the older key `damage`. |
| `hurt` | bool | — | Written by the Property editor's "Hurts on contact" checkbox. The runtime does not read it; damage is governed by `trigger_action`. |
| `target_node` | string | `''` | PathNode name for a `teleport` trigger. |
| `is_mover` | bool | `false` | Moving brush. |
| `speed`, `distance`, `direction` | number, number, `[x,y,z]` | `64.0`, `128.0`, `[0, 1, 0]` | Mover travel. For a rotating mover `speed` is the spin speed, default `45.0`. |
| `start_on`, `move_once`, `open_time` | bool, bool, number (s) | `false`, `false`, `3.0` | `engine/mover_table.py`. |
| `path_target` | string | `''` | First PathNode of a mover path. |
| `rotate`, `rot_axis` | bool, `[x,y,z]` | `false`, `[0, 1, 0]` | Continuous rotation. |
| `is_door` | bool | `false` | Door brush. |
| `door_direction` | string | `'up'` | `up`, `down`, `north`, `south`, `east`, `west`. |
| `door_distance`, `door_speed`, `door_lip` | numbers | `distance` (128.0), `speed` (64.0), `0.0` | |
| `door_auto_open`, `door_locked`, `door_needs_key`, `door_key_name` | bool, bool, bool, string | `false`, `false`, `false`, `''` | `engine/logic_interaction.py`. |
| `target` | string | none | Legacy pre-I/O target name. Still honoured; new maps use `io_connections`. |

### Keys never written to a map

`EditorState._serialize_brushes` shallow-copies each brush and removes everything in `_RENDERER_PRIVATE_KEYS`:

| Group | Keys |
| --- | --- |
| Renderer caches | `_mat_cache_key`, `_mat_cache`, `_nmat_cache_key`, `_nmat_cache`, `_render_mesh`, `_render_sig` |
| `brush_geometry.GEO_RUNTIME_KEYS` | `_geo_cache`, `_geo_cache_sig`, `_geo_epoch`, `_world_cache`, `_collision_mode`, `_mesh_triangles`, `_mesh_bounds`, `_mesh_planes`, `_box_shape`, `_box_shape_sig` |
| `engine.constants.AABB_RUNTIME_KEYS` | `_aabb_sig`, `_aabb_bounds`, `_aabb_box`, `_aabb_runtime`, `_world_bounds`, `_aabb_turn` |
| `brush_placement.RUNTIME_CACHE_KEYS` | `_collision_entry`, `_collision_box`, `_travel_filed` |
| `brush_placement.PLAY_STATE_KEYS` | `original_pos`, `_rot_angle`, `rotation_yaw` (see [What loading migrates](#what-loading-migrates)) |

The in-memory `_io_connections` list of `OutputConnection` objects is written as `io_connections`. A brush without an `id` is given one at this point. Any other key on a brush, including ones this page does not list, is preserved verbatim through load and save because the record is copied as a whole.

## Entity records (`things`)

```json
{
    "type": "light",
    "pos": [32.0, 160.0, -16.0],
    "properties": {
        "type": "light",
        "name": "Light_2",
        "id": "53e6fc4d-aef0-4109-98cf-7695c304ee85",
        "io_enabled": true,
        "colour": [255, 255, 255],
        "intensity": 1.0,
        "radius": 512.0,
        "state": "on",
        "show_radius": false,
        "casts_shadows": false
    },
    "io_connections": []
}
```

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `type` | string | Yes | Type token. A record with no `type` (or an empty one) is skipped on load and therefore lost on the next save. |
| `pos` | `[x, y, z]` finite numbers | No | World position; `[0, 0, 0]` when absent. |
| `properties` | object | No | Entity properties. A value that is not an object is replaced by `{}`. |
| `io_connections` | list of objects | No | Output connections. Always written, even when empty. |
| `runtime_data` | object | No | `logic_state` records only: the entity's stored values, written by `LogicState.to_dict` so a map carries its initial state. See [LogicState](https://github.com/ViciousSquid/Fio/wiki/LogicState). |

### Type resolution

`Thing.from_dict` (`editor/things.py`) lower-cases the token and removes underscores, then matches it against every `Thing` subclass by the class's `map_type` attribute or, when it has none, its lower-cased class name. `"logic_command"`, `"logiccommand"` and `"LogicCommand"` therefore all resolve to `LogicCommand`. The token written on save is `properties.type`.

| Written token | Class | Written token | Class |
| --- | --- | --- | --- |
| `playerstart` | `PlayerStart` | `logic_relay` | `LogicRelay` |
| `light` | `Light` | `logic_gate` | `LogicGate` |
| `speaker` | `Speaker` | `logic_timer` | `LogicTimer` |
| `monster` | `Monster` | `logic_command` | `LogicCommand` |
| `prop` | `Prop` (`engine/prop_entity.py`) | `logic_camera` | `LogicCamera` |
| `effect` | `Effect` (`engine/effect_entity.py`) | `logic_spawner` | `LogicSpawner` |
| `levelchanger` | `LevelChanger` | `logic_state` | `LogicState` |
| `path_node` | `PathNode` | `portal` | `Portal` |

The token `trigger` also resolves, to `editor.things.Trigger` (one property, `action`, default `on_enter`); that class is not in `editor.things.ENTITY_TYPES`, so the editor offers no way to place one. Trigger volumes are brushes with `is_trigger`.

Plugin entities are ordinary `things` records whose token is the plugin class's name: the bundled plugins use `bigworldsettings`, `deathmatchsettings`, `deathmatchspawn`, `deathmatchbot`, `deathmatchnode`, `deathmatchflag`, `tidyreceptacle` and `tidygoal`. Plugins have no separate section in the map; all plugin data is stored in the properties of their entities. Loading a map calls `PluginManager.auto_enable_for_map`, which enables the plugins that the map's entity types require.

A token that resolves to no class is not dropped. The record is kept as an `UnresolvedThing` holding the original record verbatim; it can be moved or deleted in the editor, has no runtime behaviour, and is written back unchanged except for `pos`. A map that uses a plugin which is missing or disabled therefore survives a load/save round trip.

### Properties common to every entity

| Property | Type | Default | Meaning |
| --- | --- | --- | --- |
| `type` | string | class name, lower-cased | Mirrors the record's `type`. |
| `name` | string | `<ClassName>_<n>` | Assigned from a per-class counter when absent or empty. After a load the counters are advanced past every name in the map (`update_all_counters_from_entities`). |
| `id` | string | new UUID4 | Stable identity. |
| `io_enabled` | bool | `true` | |

The remaining properties are defined by the entity class; see the page for each entity type and [EntityTable](https://github.com/ViciousSquid/Fio/wiki/EntityTable). On save, a property value that is not a string, number, boolean, list, object or `null` is written as `str(value)`. The in-memory `_io_connections` property is never written inside `properties`.

Validation of an entity record: `pos`, when present, must be a 3-vector; `properties`, when present, must be an object; any property ending in `_pos` must be a 3-vector or `null`; `name` and `id` must not be a list or object; `colour`/`color`, when present, must be a list of at least three finite numbers. For `playerstart`, `primary` must be a boolean and `creation_index` a whole number; for `levelchanger`, `destination_spawn` must be a string.

### PlayerStart invariants

After every load `EditorState.settle_player_starts` applies `engine.player_starts.settle`: every PlayerStart gets a unique integer `creation_index` (those without one are numbered after the others, in file order), and exactly one has `primary: true` — the earliest-created start that claims it, or the earliest-created start when none does. A level change to a named start that does not exist in the target map is refused; it is never replaced by the primary.

## I/O connections

Used by both brushes and entities.

```json
{
    "output": "OnTrigger",
    "target": "main_door",
    "input": "Open",
    "parameter": "",
    "delay": 0.0,
    "fire_once": false,
    "target_id": "0f6c1c1e-6a0b-4d2c-9a56-0d2f6a1c9b11"
}
```

| Field | Type | Default on load | Meaning |
| --- | --- | --- | --- |
| `output` | string | `''` | Output on the owning object that fires this connection. |
| `target` | string | `''` | Target name. Display value and fallback lookup. |
| `target_id` | string | `''` | Target UUID. The runtime prefers it over `target`, so a rename does not break the connection. Written only when non-empty. |
| `input` | string | `''` | Input invoked on the target. |
| `parameter` | string | `''` | Passed to the input. |
| `delay` | number (seconds) | `0.0` | Converted with `float()`. |
| `fire_once` | bool | `false` | The connection fires a single time per play session. |

Source: `editor/io_system.py: OutputConnection.to_dict` / `from_dict`. See [Triggers & The I/O Logic System](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System).

## Editor-only sections

### `visgroups` and `cordon`

```json
"visgroups": [
    {"name": "Upper floor", "visible": true, "ids": ["<uuid>", "<uuid>"]}
],
"cordon": {"enabled": false, "min": [-512.0, -512.0, -512.0], "max": [512.0, 512.0, 512.0]}
```

`ids` are brush and entity UUIDs, sorted; ids of objects that no longer exist are dropped on save. A visgroup without a `name` is skipped on load; `visible` defaults to `true`. A malformed `cordon` loads as the disabled default box. Source: `engine/view_filters.py: ViewFilters.to_level_data` / `load_level_data`. The Filter menu's show/hide state is view state and is not saved.

### `logic_graph`

```json
"logic_graph": {
    "node_positions": {
        "<entity uuid>": {"x": 320.0, "y": 180.0}
    }
}
```

Written from `EditorState._logic_graph_positions`, which the graph window updates (`LogicGraphScene.store_positions`). A `logic_graph` or `node_positions` value that is not an object is ignored.

## Terrain data

`terrain_data` is the dictionary produced by `engine/terrain.py: Terrain.to_dict` and consumed by `Terrain.from_dict`; the editor keeps it in `EditorState.terrain_data` and writes it only when it is non-empty. The meaning of each setting is on the [Terrain](https://github.com/ViciousSquid/Fio/wiki/Terrain) page; the keys are:

| Keys | Written |
| --- | --- |
| `enabled`, `solid`, `seed`, `biome` (name), `chunk_size`, `mesh_scale`, `base_resolution`, `offset_x`, `offset_y`, `offset_z`, `min_chunk_x`, `max_chunk_x`, `min_chunk_z`, `max_chunk_z`, `use_textures`, `flat_mode` | Always |
| `grass_enabled`, `grass_density`, `grass_height`, `grass_color`, `grass_color_custom`, `grass_tip_color` (list or `null`), `grass_height_range` (list or `null`) | Always |
| `appearance` (object), `custom_biome` (object) | Always |
| `sculpt_offsets` (list of `[gx, gz, offset]`), `sculpt_grid_resolution` | When the terrain has been sculpted |
| `csg_subtractions` (list of lists) | When terrain cuts exist |
| `texture_paint`, `texture_paint_resolution` | When the terrain has been painted |
| `paint_stamps` (list of objects), `texture_paint_base` | When paint stamps exist |
| `heightmap_blob` (base64 of a NumPy `.npy` array), `heightmap_strength`, `heightmap_blend` | When a heightmap is loaded |

## Plugin data

Plugins have no section of their own in a map. A plugin's data is the properties of its entities, which are ordinary `things` records (see [Type resolution](#type-resolution)). The two top-level keys that concern plugins, `required_plugins` and `plugin_config`, are read by the Fio Player only and are not written by the editor.

## What loading migrates

`EditorState.load_from_data` (and, for the same brush steps, `player/world.py: PlayerWorld.__init__`) repairs older documents record by record. The input document is never modified; the next save writes the repaired, version-5 form.

### By `version`

| Applies when | Step | Code |
| --- | --- | --- |
| Any version | **Play state dropped.** `original_pos`, `_rot_angle` and `rotation_yaw` are removed from every brush. A box that was saved mid-motion (it has an `original_pos`) is put back at `original_pos`. | `brush_placement.drop_play_state` |
| `version` < 4, angled brushes | **Face materials settled.** For each tagged plane, each field's authoritative value is chosen by `brush_faces.PRECEDENCE` and written to the plane; the per-tag copies for tags a plane carries are removed. `texture`: the plane's value. `uv_scale`, `uv_shift`, `uv_angle`: the per-tag entry when there is one, else the plane's. `uv_natural`: true if set in either place. Cut faces and boxes are untouched. | `brush_faces.migrate_legacy` |
| `version` >= 4, angled brushes | Stray per-tag entries for tags a plane carries are dropped without being consulted. | `brush_faces.drop_tag_copies` |
| `version` < 5, angled brushes | **Planes localised.** The world-space planes are re-expressed about the centre `c` of their own bounds (`d' = d - dot(n, c)`) and `pos` becomes `c`, so the brush is drawn exactly where it was even if its stored `pos`/`size` were stale. `size` is re-derived. A plane set that is not a valid solid keeps its `pos`, and its planes are taken relative to it. | `brush_placement.localise_legacy` |
| `version` >= 5, angled brushes | `size` is re-derived from the planes; `pos` is left alone. | `brush_placement.normalise_loaded` |

For a Big World map opened with editor streaming, the same steps are applied to every record of the streamed world at load, resident or not, and the cell index is rebuilt (`EditorCellStream.reindex`).

### Regardless of `version`

| Condition | Action |
| --- | --- |
| Brush without `id` | A UUID4 is assigned. |
| Two objects share an `id` | Every holder after the first gets a new UUID (`_dedupe_loaded_ids`), so connections aimed at the id keep their original target. |
| Entity `type` is `model` | Loaded as a `Prop` with `engine.prop_entity.legacy_model_properties`; I/O written against the old Model inputs is re-aimed (`io_system.retarget_legacy_model_inputs`). |
| Entity property stored as text (`"0.5"`, `"True"`) where the class declares a non-string default | Parsed back to the declared type (`editor/things.py: _heal_legacy_strings`), on load and again on save. Properties the class declares as text, or does not declare, are left as authored. |
| Linked portals disagree about `active` | Normalised so each linked group shares one state (`editor.portal_links.normalize_portal_groups`). |
| PlayerStarts without `primary` / `creation_index` | Settled as described above. |
| Item pickups (`prop` with `collect_type: "item"`) | Re-synchronised with the map's item definitions (`EditorState.sync_item_props`). |
| `created` absent | Left empty until the next save. |

Loading also clears the selection and the undo/redo stacks, advances the entity name counters past every name in the map, and calls `PluginManager.auto_enable_for_map`.

## Maps opened by Big World streaming

For a map containing a `bigworldsettings` entity the editor can keep only the cells near the camera live (`plugins/bigworld/editor_streaming.py`). The file format is unchanged: on save `get_level_data` merges the live objects back into the full record set, so `brushes` and `things` always hold the whole world.

## A minimal valid map

This document passes `validate_level` and loads; every omitted field takes the defaults described above (ids, names, `primary` and `creation_index` are assigned on load and written on the next save).

```json
{
    "version": 5,
    "brushes": [
        {
            "pos": [0, -8, 0],
            "size": [512, 16, 512],
            "textures": {
                "north": "default.png", "south": "default.png",
                "east": "default.png", "west": "default.png",
                "top": "default.png", "down": "default.png"
            }
        }
    ],
    "things": [
        {"type": "playerstart", "pos": [0, 40, 0], "properties": {"angle": 0.0}},
        {"type": "light", "pos": [0, 160, 0], "properties": {}}
    ]
}
```

`{}` is also accepted: it loads as an empty level. Play requires at least one `playerstart`. A hand-written map that contains angled brushes must either declare `"version": 5` and give local planes, or declare an older version (or none) and give world-space planes; declaring 5 over world-space planes displaces every angled brush by its own `pos`.

## Generating maps from code

Any tool may write this format directly. The repository's own writers are `editor/procedural_generator.py: create_map_data` (see [Procedural level generator](https://github.com/ViciousSquid/Fio/wiki/Procedural-level-generator)), `plugins/bigworld/tools/generate_world.py` and `plugins/tidy/tools/make_example_map.py`. The shipped maps in `maps/` are real examples of every section; `maps/README.md` lists what each demonstrates.

## Undo and redo

Undo checkpoints are not map documents but use the same record forms: `EditorState.save_state` pushes `EditorState.snapshot()` (the brushes with the same private keys stripped, the things and the selection) before an operation changes the scene, and `restore_state` rebuilds the scene from one. `undo()` always keeps the bottom entry of the stack (the base written by `load_from_data`). Tests: `tests/undo_redo/`.

## Tests

| Area | Files |
| --- | --- |
| Round trip, loading, refusal | `tests/persistence/test_map_round_trip.py`, `test_level_loading.py`, `test_level_validation.py`, `test_malformed_map_refusal.py`, `test_unknown_entity_preservation.py`, `test_logic_round_trip.py`, `test_restore_invalidation.py`, `test_shipped_map_references.py` |
| Atomic writes | `tests/persistence/test_atomic_saves.py`, `test_atomic_output_hardening.py` |
| Version migration | `tests/brush_model/test_map_compatibility.py` (versions 3, 4 and 5), `test_face_precedence.py` (the `PRECEDENCE` rule), `test_local_frame.py`, `test_placement.py`, `test_runtime_transform.py` (play state never written) |
| Plane geometry | `tests/geometry/` |
