> Applies to Fio 3.5.0.1111.

# EntityTable

`EntityTable` (`engine/entity_table.py`) is the dense, numeric projection of the entity list (`EditorState.things`) that renderers draw from: one row per entity, addressed by an integer **slot**, with NumPy columns for position, a classification word, the live `hidden` flag, light state, portal topology and frame, Effect playback, sprite recipe and model recipe and transform. Like [RenderTable](https://github.com/ViciousSquid/Fio/wiki/RenderTable) it stores nothing authored, is never written back to the world, imports no OpenGL, and is kept current by change notification rather than by re-reading entities every frame.

Related pages: [RenderTable](https://github.com/ViciousSquid/Fio/wiki/RenderTable), [Renderer Technical Overview](https://github.com/ViciousSquid/Fio/wiki/Renderer-Technical-overview), [Renderer Development Guide](https://github.com/ViciousSquid/Fio/wiki/Renderer-Development-Guide), [Portal Rendering - Technical Overview](https://github.com/ViciousSquid/Fio/wiki/Portal-Rendering-‐-Technical-Overview), [Debug Tables](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables).

## Ownership and threads

| Who | Thread | Access |
|---|---|---|
| `LogicRender.prepare_render_state` | logic | `etable.begin_frame(things, world_epoch, dirty_objects=..., effect_runtime=play_mode, effect_store=session_runtime.effect_store, peer=..., peer_dirty=...)` on the write buffer's table |
| active renderer, `QtGameView` | GUI | reads the read buffer's table under the frame lease; writes nothing |
| Debug Tables | GUI | copies the leased table |

One table lives in each of the two `RenderState` buffers (`engine/threaded_game_state.py`). The frame input carries it as `entity_table`, with `visible_thing_slots` (rows to draw in this view) and `thing_hidden` (the `hidden[:count]` mask `begin_frame` returned).

## Identity and slot vectors

| Attribute | Meaning |
|---|---|
| `count`, `generation` | live rows; `generation` increments on every reconcile and adopt |
| `ids`, `slot_of_id` | slot ↔ `properties['id']` (the entity's UUID); cache-boundary only |
| `things`, `refs` | slot → the live entity (list / object array). `refs` is published as `RenderState.entity_refs` for the host and is never frame input |
| `all_slots` | `arange(count)`, `int32` |
| `light_slots` | rows with `ENT_LIGHT` **or** `ENT_EFFECT` (an Effect carries a light) |
| `portal_slots` | rows with `ENT_PORTAL` |
| `monster_slots` | rows with `ENT_MONSTER` |
| `path_node_slots` | rows with `ENT_PATH_NODE` |
| `effect_slots` | rows with `ENT_EFFECT`; row order matches the Effect execution rows |
| `center` | property: alias of `pos`, so slot helpers written for `RenderTable` work on both |
| `rows_read` | entities read by the last `begin_frame` (Debug Tables) |

The slot vectors are rebuilt only at a reconcile (or copied by `adopt`).

## Columns

Declared in `entity_table._COLUMNS` as `(name, trailing shape, dtype, fill)`. Capacity grows as `max(16, capacity * 2, n)`.

### Common

| Column | dtype | Shape | Meaning |
|---|---|---|---|
| `pos` | `float64` | `(N, 3)` | world position |
| `class_bits` | `uint16` | `(N,)` | classification, below |
| `hidden` | `bool` | `(N,)` | `properties['hidden']`, streaming parks included |
| `render_alpha` | `float32` | `(N,)` | runtime opacity 0..1 from `_respawn_fade_alpha` (a respawning Prop fades in); 1.0 otherwise |

### Lights (rows in `light_slots`)

| Column | dtype | Shape | Meaning |
|---|---|---|---|
| `light_color` | `float32` | `(N, 3)` | Light: `colour` / 255, default white. Effect: its `effect_light_color` |
| `light_params` | `float32` | `(N, 2)` | `(intensity, radius)`; Light defaults 1.0 and 512.0; Effect: `light_intensity` (2.5) and `light_radius` (128.0) |
| `light_enabled` | `bool` | `(N,)` | Light: `state` (default on). Effect: `light_enabled`, and for an EXPLOSION only during its flash |
| `light_casts_shadows` | `bool` | `(N,)` | Light: `casts_shadows` (default off); always `False` for an Effect |

A reused slot that is neither a Light nor an Effect has these zeroed.

### Portals (rows in `portal_slots`)

| Column | dtype | Shape | Meaning |
|---|---|---|---|
| `portal_target_slot` | `int32` | `(N,)` | slot of the entity whose `name` equals this portal's `portal_target`, if that entity is a Portal; else -1. Resolved by name at reconcile and whenever a portal row is re-resolved |
| `portal_active` | `bool` | `(N,)` | `active` (default `True`) |
| `portal_direction` | `uint8` | `(N,)` | `portal_direction`: 1 `forward`, 2 `reverse`, 3 `both` (default), 0 unrecognised |
| `portal_width_height` | `float32` | `(N, 2)` | `width` (128) and `height` (256), each at least 16 |
| `portal_basis` | `float64` | `(N, 3, 3)` | `engine.portal_transform.basis_from_rotation(rotation)`; `rotation` defaults to `[angle, 0, 0]` |
| `portal_fade` | `float32` | `(N,)` | runtime `_fade_alpha` clamped 0..1 (default 1) |
| `portal_color` | `float32` | `(N, 3)` | `color` / 255, default white |
| `portal_show_rim` | `bool` | `(N,)` | `show_rim` (default `True`) |
| `portal_glasses` | `bool` | `(N,)` | `glasses` (default `True`): looking into it shows the viewer's own glasses |

### Effects (rows in `effect_slots`)

| Column | dtype | Shape | Meaning |
|---|---|---|---|
| `effect_type` | `uint8` | `(N,)` | 0 FIRE, 1 EXPLOSION, 2 ORB, 3 CUSTOM (`engine.effect_table.FAMILY_*`) |
| `effect_fire_variant` | `uint8` | `(N,)` | index of `fire_texture` in `assets/textures/effects/fire01..05.gif` (FIRE) or of `orb_texture` in `orb01..05.gif` (ORB); 0 otherwise |
| `effect_custom_id` | `int32` | `(N,)` | interned `custom_gif` path for CUSTOM (1-based; 0 = none); resolve with `effect_custom_path(id)` |
| `effect_custom_loop` | `bool` | `(N,)` | `custom_loop` (default `True`) |
| `effect_preview` | `bool` | `(N,)` | `preview`: show an EXPLOSION's preview frame while editing |
| `effect_params` | `float32` | `(N, 4)` | `(width, intensity, light_intensity, light_radius)`; `width` default 32 |
| `effect_color` | `float32` | `(N, 3)` | `colour` / 255, default `[255, 110, 25]` |
| `effect_light_color` | `float32` | `(N, 3)` | FIRE: fixed colour of its variant; ORB: `#4A9BFF`; otherwise `light_colour` / 255 (default `[255, 165, 70]`) |
| `effect_light_enabled` | `bool` | `(N,)` | `light_enabled` (default `True`) |
| `effect_lifetime` | `float32` | `(N,)` | `lifetime` seconds, at least 0.01, default 0.5 |
| `effect_seed` | `float32` | `(N,)` | flicker seed; holds its fill value 1.0 for every row (see Notes) |
| `effect_store_index` | `int32` | `(N,)` | the Effect's row in the `EffectStore`, or -1 |
| `effect_spawn_time` | `float64` | `(N,)` | `perf_counter` time playback started, or the module's `_CLOCK_ORIGIN` when the Effect has none |
| `effect_phase` | `float32` | `(N,)` | 0..1 offset into the animation cycle |
| `effect_elapsed` | `float32` | `(N,)` | advanced every frame |
| `effect_active` | `bool` | `(N,)` | the store's `active` flag (an EXPLOSION that has been triggered) |
| `effect_alive` | `bool` | `(N,)` | drawn this frame: FIRE/ORB/CUSTOM always; EXPLOSION while active and `elapsed < lifetime` (Play) or while `effect_preview` (editing) |

For an Effect row `sprite_size` is `(width, height)` (`height` default 46, or 32 for ORB).

### Sprites

| Column | dtype | Shape | Meaning |
|---|---|---|---|
| `sprite_size` | `float32` | `(N, 2)` | world size: Monster `sprite_width`/`sprite_height` (128); Light `(16, 16)`; an entity with `sprite_path`: `sprite_size` (default `[32, 32]`); else `(32, 32)` |
| `sprite_key_id` | `int32` | `(N,)` | interned sprite recipe; `SPRITE_NONE` (-1) draws nothing (Portals, private editor models, raw dicts) |
| `sprite_direction_ids` | `int32` | `(N, 8)` | for a live Monster, the recipe for each of the eight view rotations (1..8); `SPRITE_NONE` otherwise. The renderer picks per view with `engine.monster_facing.sprite_keys_for_view` |
| `monster_angle` | `float64` | `(N,)` | `angle` property |
| `sprite_fixed_yaw` | `float32` | `(N,)` | `_carry_sprite_yaw` for a carried billboard; -10000 means an ordinary camera-facing billboard |
| `sprite_overlay` | `bool` | `(N,)` | `_sprite_overlay`: sorts just after a coplanar body (a carried icon) |
| `sprite_wound` | `float32` | `(N,)` | blood on the sprite: `2 * (min(15, _wounds) + 16 * pattern)` with `pattern = (id(entity) >> 4) % 16`, or 0 with no wounds. Always even: the sprite pass adds it to the alpha it sends and the shader separates the two |

A sprite **recipe** is an ordered tuple of candidates `(cache_key, filename, subfolder, cache)`. The renderer tries each in order: look `cache_key` up in the host's sprite table, and on a miss load `filename` from `subfolder`, storing the result under `cache_key` when `cache` is true (`TablesMixin._sprite_gl_ids`). `sprite_recipes()` returns the interned list. How recipes are chosen (`sprite_candidates`):

| Entity | Candidates |
|---|---|
| Monster | key `msprite_<type>_<variant>_<idle\|shoot\|dead>_<custom>`: the custom image, then `sprites/monsters/<type>/<variant>/<state>.png`, then the base folder, then `idle.png` fallbacks; directional recipes append `_rotation_<n>` |
| LogicGate | `logic_<logic_type>.png` in `sprites`, then the class sprite |
| Prop in `billboard` mode | its `get_sprite_path()` under key `propsprite__...` |
| LevelChanger, LogicRelay, LogicTimer, LogicSpawner, LogicCamera | their fixed PNG in `sprites` |
| any entity with `sprite_path` | that file, keyed by class name |
| a class with `editor_sprite_path` | that file |
| anything else | a lookup of the class name in the host's sprite table |

### Models

| Column | dtype | Shape | Meaning |
|---|---|---|---|
| `model_recipe_id` | `int32` | `(N,)` | interned recipe `(model_path, manual_texture, colour, tint)`; -1 = no model. `model_recipes()` returns the list |
| `model_base_matrix` | `float32` | `(N, 16)` | column-major rotation × scale with zero translation (yaw about Y, then pitch about X, then roll about Z, then scale); the renderer adds `pos` |
| `model_normal_matrix` | `float32` | `(N, 12)` | its normal matrix, three columns padded to `vec4` |

In a recipe, `manual_texture` is the entity's `texture` property (`None` when unset), `colour` its `color` fallback (default 0.8 grey) and `tint` its `tint` as 0..1 floats (values above 1 are read as 0..255; white is `None`). A private editor model (`editor_model_path`) has `(path, None, None, None)`.

### `class_bits`

| Bit | Value | Set for |
|---|---:|---|
| `ENT_SKIP` | `1 << 0` | a PathNode, a raw dict, or a non-`Thing` |
| `ENT_ALWAYS_SPRITE` | `1 << 1` | Portal, Monster |
| `ENT_HAS_MODEL` | `1 << 2` | `engine.entity_models.model_path(thing)` is set |
| `ENT_MODE_MODEL` | `1 << 3` | `render_mode` is `model` (the default; forced for editor models) |
| `ENT_MODE_BILLBOARD` | `1 << 4` | `render_mode` is `billboard` |
| `ENT_HAS_SPRITE` | `1 << 5` | has `sprite_path` |
| `ENT_EDITOR_MODEL` | `1 << 6` | has `editor_model_path` (a private editor model such as the Player Start sign) |
| `ENT_ENTITY_SPRITE` | `1 << 7` | Monster, LogicGate, LogicRelay, LogicTimer, LevelChanger |
| `ENT_PROP` | `1 << 8` | Prop |
| `ENT_LIGHT` | `1 << 9` | Light |
| `ENT_MONSTER` | `1 << 10` | Monster |
| `ENT_PORTAL` | `1 << 11` | Portal |
| `ENT_PATH_NODE` | `1 << 12` | PathNode |
| `ENT_EFFECT` | `1 << 13` | Effect |

`ENT_CULL_EXEMPT = ENT_LIGHT | ENT_PORTAL`: never dropped by the renderer's distance cull. `describe(bits)` returns the names for assertions and Debug Tables. `ENT_PICKUP` and `ENT_SPRITE_WARM` no longer exist.

## Pass classification

`classify_slots(table, slots, hidden, is_play, show_sprites)` returns `(model_slots, sprite_slots)` from a precomputed truth table (`_RENDER_KIND`, indexed by ordinary-sprite policy, the live hidden flag and the classification word; eight rows or fewer use scalar lookup). The rules, in order:

1. `ENT_SKIP` and `ENT_EFFECT` rows are in neither list (Effects have their own pass; PathNodes their overlay). `ENT_EDITOR_MODEL` rows are skipped unless ordinary sprites are shown.
2. `ENT_ALWAYS_SPRITE` rows (Portal, Monster) are sprites. A Portal's `sprite_key_id` is `SPRITE_NONE`, so the sprite pass draws nothing for it.
3. A row with a model, in model mode and **not hidden**, goes to the model pass.
4. Otherwise: a Prop is a sprite only as a billboard with a sprite; the entity-sprite classes are sprites; other entities are sprites when they are billboards with a sprite, or when ordinary sprites are shown.

"Ordinary sprites are shown" means `not is_play or show_sprites` (the frame input's `show_sprites_in_play_mode`). The live hidden flag gates **only the model verdict** in this function: a hidden Monster, entity-sprite class or billboard Prop is still returned in `sprite_slots` (`tests/renderer/test_entity_table.py` holds the truth table to the reference chain, hidden flag included). Elsewhere the forward renderer tests `thing_hidden` for lights (in Play), Effects and shadow-casting models; the logic thread leaves collected Props and view-filtered rows out of `visible_thing_slots`.

## Bringing the table up to date

`begin_frame(things, epoch=None, dirty_objects=None, effect_runtime=False, peer=None, peer_dirty=None, effect_store=None)` returns `hidden[:n]`.

1. **Row set**: the same `RowList` at the same version and the same epoch → nothing compared; otherwise `needs_reconcile` compares the row tuple and epoch.
2. `JOURNAL.drain(self)` and `JOURNAL.drain_positions(self)`.
3. **Reconcile needed**: `_refresh_in_place` (same rows, precise dirty set), else `adopt(peer)` (plus the peer's dirty rows), else `_reconcile` — survivors (same object, same id, not dirty) keep their columns at their new slot; other rows go through `refresh_rows` → `_resolve_row`; slot vectors and portal links are rebuilt; `generation` moves.
4. **Journal** (`_apply_changes`):

| Flag (`engine.change_journal`) | Recorded by | Effect on the row |
|---|---|---|
| `MOVED` (1) | assigning `Thing.pos` (`TrackedPosition`), `change_journal.moved`, `set_positions` | `pos` written from the position payload carried with the notification, without touching the entity; Effect rows also update `EffectStore.pos` |
| `STATE` (2) | `change_journal.touch(obj)`; `TrackedAttribute` writes (`_respawn_fade_alpha`, a carried sprite's yaw, a portal's fade, an Effect's playback clock); the I/O dispatcher after every input; the monster AI when a sprite flag changes; console; save restore | the whole row re-resolved; portal links re-resolved if a portal row was among them |
| `VISIBILITY` (4) | Big World parking (`touch(thing, VISIBILITY)`) | only `hidden` re-read |
| `OVERFLOW` | a subscriber that stopped draining past `PENDING_LIMIT` (131072 entries) | every row re-resolved |

5. **Polled rows** (`_poll_slots`): rows whose object cannot journal (`change_journal.is_tracked` is false — a raw dict standing in for a Thing) have `pos` and `hidden` read every frame.
6. **Effect clock** (`_advance_effects`), the only per-frame work over rows: `effect_elapsed`, `effect_alive`, an EXPLOSION's flash light (on for the first `min(lifetime, 0.12)` s, intensity × `(1 + 2·e^(−t/0.035))`) and a FIRE's flicker (`intensity × (0.78 + 0.38·noise)`). Outside Play (`effect_runtime=False`) FIRE animates on the shared clock, explosions are not alive unless previewed, and a previewed explosion is pinned to frame 10 of 16.

Unlike `RenderTable`, `EntityTable` has no transform log, row-change log or material version: readers key on `generation` and the slot arrays. `sync(things, epoch, dirty_objects)` reconciles outside the frame loop.

### Effect execution state

`engine/effect_table.py: EffectStore` (owned by `logic.session_runtime.effect_store`, logic thread) holds Effect runtime as structure-of-arrays columns — `pos (n,3) float32`, `lifetime float32`, `phase float32`, `family_id uint8`, `spawn_time float64`, `active bool` — grown with `engine.soa.grow_soa_arrays`. Rows are paired with table rows by object identity (`index_of`), never by position. `_publish_effect_rows` copies `family_id`, `lifetime`, `spawn_time`, `phase` and `active` into the table only when the store's `generation` or the table's Effect rows have moved; each of the two tables remembers the generation it last saw.

## Related dense stores

These are logic-thread stores, not frame input, but follow the same conventions (columns in `_COLUMNS` or SoA arrays, geometric growth):

| Store | Module | Published to the renderer as |
|---|---|---|
| `ProjectileStore` — `pos`, `vel` `(n,3) float32`; `lifetime`, `distance`, `damage` `float32`; `owner_id int64` | `engine/projectile_table.py` | `RenderState.projectiles`, an `(N, 3)` float32 array the host passes to `Renderer.draw_billboards` |
| `DecalTable` — a ring of `CAPACITY` = 128 rows: `model (n,16) float32`, `variant`, `kind` (`WALL` 0 / `FLOOR` 1), `flip` `float32`, `born float64` | `engine/decal_table.py` | frame input `decals`: `(models (n,16), params (n,4))` with `params = (variant, opacity, kind, mirrored)`, oldest first, opacity fading over the last part of a decal's life |
| `MonsterTable` — the AI's per-tick columns (`pos`, `team`, `mode`, `target`, `dist_sq`, `ground_y`, ...) | `engine/monster_table.py` | nothing; shown by Debug Tables' **MonsterTable** tab. Monsters reach the renderer through their `EntityTable` rows |
| `MoverTable` | `engine/mover_table.py` | writes `RenderTable.bounds` / `rot` (see [RenderTable](https://github.com/ViciousSquid/Fio/wiki/RenderTable)) |
| `TerrainTable` | `engine/terrain_table.py` | inside the published `TerrainFrame` (see [Terrain](https://github.com/ViciousSquid/Fio/wiki/Terrain)) |

## Reading the table in a renderer

```python
from engine import entity_table as et

etable = config['entity_table']
hidden = config['thing_hidden']
model_slots, sprite_slots = et.classify_slots(
    etable, config['visible_thing_slots'], hidden,
    config['play_mode'], config['show_sprites_in_play_mode'])
lights = etable.light_slots[etable.light_enabled[etable.light_slots]]
centres = etable.pos[sprite_slots]                # (k, 3) float64
```

Never read `things` / `refs` in a frame, never write a column, and never intern a recipe from the render thread.

## Adding a column

1. Add it to `_COLUMNS`; growth, reconcile moves and `adopt` cover it.
2. Resolve it in `_resolve_row`, including the reset branch for rows that are not of the relevant class (slots are reused).
3. Make sure whatever changes the source value journals the entity: authored edits through `EditorState.mark_world_changed`, runtime writes through `touch` or a `TrackedAttribute`.
4. Extend `tests/renderer/test_entity_table.py` and check the column in Debug Tables (**EntityTable** tab).

## Notes

- `effect_seed` is declared and read (`_publish_effect_rows` gathers it for the FIRE flicker; the forward renderer's EXPLOSION instance data takes it) but `_resolve_row` never stores the per-Effect seed it computes from the `effect_seed` property, so every row holds 1.0.

## Tests

| File | Covers |
|---|---|
| `tests/renderer/test_entity_table.py` | classification against the reference chain, sprite and model recipes, lights, portals, effects, reconcile/adopt, journal-driven refresh |
| `tests/renderer/test_dense_position_batch.py` | batched `MOVED` payloads |
| `tests/renderer/test_table_boundary_equivalence.py`, `tests/visual/test_render_projection_equivalence.py` | the table equals a rebuild from the same world |
| `tests/renderer/test_portal_fade.py`, `test_portal_glasses.py` | portal columns as the renderer consumes them |
| `tests/e2e/test_change_records.py` | every write path reaches the published table |
