> Applies to Fio 3.1.0.0611 (CANARY).

A Fio map is one UTF-8 JSON document. It is the serialised form of `editor/editor_state.py: EditorState` and is loaded directly by the editor, by Play and by the standalone player; there is no compiled or baked level format. This page is the schema of that document: every top-level key, the brush and entity records, what is checked on load and what is rewritten on save.

## Reading and writing

| Operation | Code | Notes |
| --- | --- | --- |
| Save | `MainWindow.save_level` → `engine.fileio.write_json_atomic(path, EditorState.get_level_data(), indent=4)` | The document is serialised first, written to a `.fio-<hex>.tmp` sibling, fsynced and moved over the destination with `os.replace`. A failed save never truncates the previous file. |
| Autosave | `MainWindow._write_autosave` | Writes `maps/<map name>_autosave.json` (`untitled_autosave.json` for an unsaved level) when there are unsaved changes, every `[Editor] autosave_interval` minutes (default 10) while `[Editor] autosave_enabled` is true (default). Also written when a level change during Play replaces a level with unsaved edits. |
| Load | `MainWindow.load_level_file` → `_load_level` → `EditorState.validate_level_data` → `EditorState.load_from_data` | The whole document is validated and parsed before the open level is touched. A document that fails leaves the current level, its file path and its unsaved flag unchanged. |
| Validate | `engine/level_validation.py: validate_level(level_data, *, partial=False, where="map")` | Structural checks only; raises `ValueError` naming the field. No Qt or editor imports, so `engine.savegame` uses it too. |

Coordinates are world units in a right-handed, Y-up system. The face names used throughout are: `east`/`west` = +X/−X, `top`/`down` = +Y/−Y, `north`/`south` = +Z/−Z (`engine/brush_geometry.py: _BOX_FACES`).

## Top-level keys

| Key | Type | Written | Read | Meaning |
| --- | --- | --- | --- | --- |
| `version` | int | Always, as `3` | Never | Written by `get_level_data` and by the generators. No loader inspects it; compatibility with older maps is handled per field (see [Compatibility applied on load](#compatibility-applied-on-load)). |
| `brushes` | list of objects | Always | Yes | World geometry. Absent or `null` is treated as empty. |
| `things` | list of objects | Always | Yes | Entities. Absent or `null` is treated as empty. |
| `created` | string | Always | Yes | ISO-8601 timestamp (seconds precision) of the first save. Set once and carried forward. Absent in older maps (kept as `''` until the next save sets it). |
| `items` | object | Only when a custom item slot differs from its shipped default | Yes | Definitions of the custom item slots, keyed `custom1` / `custom2`. See [Items](https://github.com/ViciousSquid/Fio/wiki/Items). |
| `terrain_data` | object | Only when the map has terrain | Yes | The terrain's configuration and edits, passed to `engine.terrain.Terrain.from_dict`. See [Terrain](https://github.com/ViciousSquid/Fio/wiki/Terrain). |
| `visgroups` | list of objects | Only when the map has visgroups or an enabled cordon | Yes | Editor visibility groups. See [View Filters and VisGroups](https://github.com/ViciousSquid/Fio/wiki/View-Filters-and-VisGroups). |
| `cordon` | object | Together with `visgroups` | Yes | The editor cordon box. |
| `lightmap_scene_hash` | string | Only when a bake has produced a hash | Yes | Restored into `BakeState.scene_hash`. A freshly loaded scene is still marked dirty, so the bake runs at least once before the first Play. |
| `logic_graph` | object | Only when node positions exist | Yes | Layout of the [Logic Graph Editor](https://github.com/ViciousSquid/Fio/wiki/Logic-Graph-Editor). Editor data only; it does not affect I/O behaviour. |

| `required_plugins` | list of strings | Never (not written by the editor) | Fio Player only | Plugin names the standalone player must enable before building the world (`player/world.py: PlayerWorld.__init__`). A name that is not installed stops the load with `This world requires an installed plugin: <name>`. |
| `plugin_config` | object | Never | Fio Player only | Per-plugin settings, keyed by plugin name, merged into that plugin's `config` when it is listed in `required_plugins`. |

Unknown top-level keys are ignored by the editor on load and are not written back. This includes `required_plugins` and `plugin_config`: a map carrying them loses them when it is saved from the editor.

### Top-level validation

`validate_level` refuses the document when:

* it is not a JSON object;
* `brushes` or `things` is present and is not a list, or an element is not an object;
* `items` is present and is not an object, has a key that is not `custom1`/`custom2` (the built-in `gun1`/`gun2` cannot be redefined), or a definition is not an object. A definition whose settings are wrong still loads: it is kept as authored and its slot resolves to no item until fixed.

## Brush records

A brush is a plain JSON object. In memory it is the same `dict`, with runtime caches added under `_`-prefixed keys.

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
| `pos` | `[x, y, z]` finite numbers | Yes | — | Centre of the brush. For an angled brush, the centre of its bounding box. |
| `size` | `[x, y, z]` finite numbers | Yes | — | Full extents. For an angled brush, the extents of its bounding box (kept in sync by `brush_geometry.sync_brush_bounds`). |
| `id` | string | No | A new UUID4 is assigned on load | Stable identity. I/O connections, undo, save games and the render tables key on it. |
| `name` | string | No | none | Name used by I/O targets and console commands. Must not be a list or object. |
| `textures` | object | No | none | Texture per face tag (`north`, `south`, `east`, `west`, `top`, `down`), as a path relative to `assets/textures/` (`"default.png"`, `"Dev/512.jpg"`). `caulk.jpg` is never drawn; `nodraw.jpg` is drawn in the editor and skipped in Play (`engine/render_table.py: TEX_SKIP`, `TEX_NODRAW`). |
| `uv_scale` | object | No | scale `[1, 1]` | Per face tag, `[u_repeat, v_repeat]`. |
| `uv_shift` | object | No | `[0, 0]` | Per face tag, `[u, v]` texture shift. |
| `uv_angle` | object | No | `0` | Per face tag, texture rotation in degrees. |
| `uv_natural` | object | No | `{}` | Per face tag, `true` when the face is in Natural mode: repeats are derived from the face's current size (one texel per world unit) every time it is drawn, instead of the stored `uv_scale`. |
| `geometry` | object | No | none | Present on an angled (convex) brush. See below. |
| `operation` | string | No | none | `"subtract"` marks a subtractive brush; the editor's CSG tools write `"add"` on the pieces they create. A subtract brush is excluded from collision and rendering as a solid (`engine/logic_collision.py`, `engine/render_table.py`). |
| `io_connections` | list of objects | No | `[]` | Output connections; see [I/O connections](#io-connections). Written only when the brush has at least one. |

Validation of a brush record (`_validate_record`): `pos` and `size` must both be present and be 3-element lists of finite, non-boolean numbers; `textures`, `uv_scale`, `uv_angle`, `uv_shift`, `uv_offset`, `uv_natural` and `geometry`, when present, must be objects; `door_speed`, `door_distance`, `door_lip`, `speed` and `distance`, when present, must be finite numbers; any key ending in `_pos` (for example `original_pos`) must be a 3-vector or `null`; `direction` must be a 3-vector or `null`; `name` and `id` must not be a list or object; `io_connections` must be a list of objects.

### Angled brushes: `geometry`

A brush that has been clipped, rotated or edited in vertex/edge/face mode carries a plane set. The planes are the source of truth; the render mesh, the 2D outline, the collision mesh and `pos`/`size` are derived from them.

```json
"geometry": {
    "planes": [
        {"n": [1.0, 0.0, 0.0], "d": -200.0, "texture": "Dev/256.jpg", "face": "east"},
        {"n": [0.7071, 0.7071, 0.0], "d": 120.0, "texture": "Dev/256.jpg",
         "uv_scale": [1.0, 1.0], "uv_shift": [0.0, 0.0], "uv_angle": 0.0}
    ]
}
```

| Plane field | Type | Meaning |
| --- | --- | --- |
| `n` | `[x, y, z]` | Outward unit normal. |
| `d` | number | Plane offset: the plane is `dot(n, p) == d`. A point is inside the half-space when `dot(n, p) <= d`; the solid is the intersection of all half-spaces. |
| `texture` | string | Texture of the face this plane produces. Omitted when unset. |
| `face` | string | Box tag (`east`, `top`, ...) carried over from the original box side. A plane with a tag reads its UV transform from the brush's per-tag `uv_*` objects; a plane without one (a cut face) carries its own. |
| `uv_scale` | `[u, v]` | Repeat factors for this plane. Omitted when unset. |
| `uv_shift` | `[u, v]` | Texture shift, cut faces. |
| `uv_angle` | number | Texture rotation in degrees, cut faces. |
| `uv_natural` | `true` | Natural mode, cut faces. Written only when true. |
| `uv_u`, `uv_v` | `[x, y, z]` | The face's texture basis as world vectors. Present only on faces that have been rotated; otherwise the basis is derived from the dominant axis of the normal (`brush_geometry.render_uv_axes`). |

In the editor a cut face is addressed as `"#<plane index>"` and a tagged face by its tag (`brush_geometry.face_key`). A geometry brush whose planes form an axis-aligned box can be reduced back to plain `pos`/`size` (`EditorState.simplify_brush_geometry`).

### Behaviour and appearance fields

These keys are optional. The table gives the value the runtime uses when the key is absent. Their behaviour is described on the pages for [triggers, movers and doors](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System) and the [Property editor](https://github.com/ViciousSquid/Fio/wiki/Property-editor).

| Field | Type | Runtime default | Read by |
| --- | --- | --- | --- |
| `hidden` | bool | `false` | Rendering and collision. |
| `lock` | bool | `false` | Editor only: the brush cannot be selected or edited in the viewports. |
| `disabled` | bool | `false` | Triggers and player interaction skip a disabled brush. |
| `colour` | `[r, g, b]` floats 0..1 | `[0.8, 0.8, 0.8]` | Flat brush colour. |
| `tint` | `[r, g, b]` ints 0..255 | none | Tint applied over the brush (`tint` console command). |
| `texture_tiling` | bool | `false` | `engine/render_table.py`. |
| `lightmap_static` | bool | `false` | Participates in lightmap baking (`EditorState.get_static_brushes`). |
| `brush_group` | string | `''` | Group name shared by a door/mover and its companion brushes. |
| `shader` | string | `'<None>'` | One of `<None>`, `Glass`, `Glow`, `Water`, `Fog`. |
| `is_fog` | bool | `false` | Fog volume (also selected by `shader == "Fog"`). |
| `fog_density`, `fog_color`, `fog_noise_scale` | number, `[r,g,b]` 0..1, number | `2.0`, `[0.5, 0.6, 0.7]`, `0.01` | Fog volumes. |
| `glass_opacity`, `glass_distortion`, `glass_refraction`, `glass_roughness`, `glass_fresnel`, `glass_color` | numbers, `[r,g,b]` 0..1 | `0.3`, `0.5`, `1.5`, `0.0`, `0.5`, `[0.7, 0.85, 0.95]` | Glass shader (`engine/render_table.py`). |
| `water_opacity`, `water_distortion`, `water_refraction`, `water_roughness`, `water_fresnel`, `water_tint` | numbers, `[r,g,b]` 0..1 | `0.5`, `0.5`, `1.333`, `0.0`, `0.65`, `[0.0, 0.4, 0.6]` | Water shader. `water_fresnel` falls back to the older key `water_reflectivity`. |
| `water_plane`, `water_wave_enabled`, `water_wave_height`, `water_high_quality` | bool, bool, number, bool | `false`, `true`, `0.5`, `true` | Water surface options. |
| `glow_intensity` | number | `10.0` | Glow shader. The emitted colour is `tint`, else `colour`, multiplied by the intensity (each channel capped at 10). |
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
| `original_pos` | `[x,y,z]` | none | A mover's rest position. |
| `is_door` | bool | `false` | Door brush. |
| `door_direction` | string | `'up'` | `up`, `down`, `north`, `south`, `east`, `west`. |
| `door_distance`, `door_speed`, `door_lip` | numbers | `distance` (128.0), `speed` (64.0), `0.0` | |
| `door_auto_open`, `door_locked`, `door_needs_key`, `door_key_name` | bool, bool, bool, string | `false`, `false`, `false`, `''` | `engine/logic_interaction.py`. |
| `target` | string | none | Legacy pre-I/O target name. Still honoured; new maps use `io_connections`. |

Any other key on a brush is preserved verbatim through load and save, because the record is copied as a whole.

### Keys never written to a map

`EditorState._serialize_brushes` copies each brush and removes the runtime caches in `_RENDERER_PRIVATE_KEYS`: `_mat_cache_key`, `_mat_cache`, `_nmat_cache_key`, `_nmat_cache`, `_render_mesh`, `_render_sig`, the geometry keys in `brush_geometry.GEO_RUNTIME_KEYS` (`_geo_cache`, `_geo_cache_sig`, `_geo_epoch`, `_collision_mode`, `_mesh_triangles`, `_mesh_bounds`, `_mesh_planes`, `_box_shape`, `_box_shape_sig`) and `engine.constants.AABB_RUNTIME_KEYS` (`_aabb_sig`, `_aabb_bounds`). The in-memory `_io_connections` list of `OutputConnection` objects is written as `io_connections`. A brush without an `id` is given one at this point.

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

## Compatibility applied on load

`load_from_data` and `Thing.from_dict` repair older documents field by field. The next save writes the repaired form.

| Condition | Action |
| --- | --- |
| Brush without `id` | A UUID4 is assigned. |
| Two objects share an `id` | Every holder after the first gets a new UUID (`_dedupe_loaded_ids`), so connections aimed at the id keep their original target. |
| Entity `type` is `model` | Loaded as a `Prop` with `engine.prop_entity.legacy_model_properties`; I/O written against the old Model inputs is re-aimed (`io_system.retarget_legacy_model_inputs`). |
| Entity property stored as text (`"0.5"`, `"True"`) where the class declares a non-string default | Parsed back with `ast.literal_eval` (`_heal_legacy_strings`). Properties the class declares as text, or does not declare, are left as authored. |
| Linked portals disagree about `active` | Normalised so each linked group shares one state (`editor.portal_links.normalize_portal_groups`). |
| PlayerStarts without `primary` / `creation_index` | Settled as described above. |
| Item pickups (`prop` with `collect_type: "item"`) | `sprite_path` is re-pointed at the item definition's `world_sprite` (`sync_item_props`). |
| `created` absent | Left empty until the next save. |

## Maps opened by Big World streaming

For a map containing a `bigworldsettings` entity the editor can keep only the cells near the camera live (`plugins/bigworld/editor_streaming.py`). The file format is unchanged: on save `get_level_data` merges the live objects back into the full record set, so `brushes` and `things` always hold the whole world.

## A minimal valid map

This document passes `validate_level` and loads; every omitted field takes the defaults described above (ids, names, `primary` and `creation_index` are assigned on load and written on the next save).

```json
{
    "version": 3,
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

`{}` is also accepted: it loads as an empty level. Play requires at least one `playerstart`.

## Generating maps from code

Any tool may write this format directly. The repository's own writers are `editor/procedural_generator.py: create_map_data` (see [Procedural level generator](https://github.com/ViciousSquid/Fio/wiki/Procedural-level-generator)), `plugins/bigworld/tools/generate_world.py` and `plugins/tidy/tools/make_example_map.py`. The shipped maps in `maps/` are real examples of every section; `maps/README.md` lists what each demonstrates.

## Tests

`tests/persistence/`: `test_map_round_trip.py`, `test_level_loading.py`, `test_level_validation.py`, `test_malformed_map_refusal.py`, `test_unknown_entity_preservation.py`, `test_logic_round_trip.py`, `test_atomic_saves.py`, `test_atomic_output_hardening.py`, `test_shipped_map_references.py`. Brush geometry serialisation is covered by `tests/geometry/`.
