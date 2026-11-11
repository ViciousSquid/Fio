> Applies to Fio 3.1.0.0611 (CANARY).

A **Prop** (`type: "prop"`, `engine/prop_entity.py`, runtime in `engine/prop_runtime.py`) is a world object drawn as a billboard sprite or a model. Optionally it can be carried, collected, or simulated as a physics body. By default it is scenery: not solid, not carryable, not collectable.

## Properties

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `render_mode` | `billboard` / `model` | `billboard` | How it is drawn. |
| `sprite_path` | string | `assets/sprites/pickup.png` | Billboard image. |
| `sprite_size` | [w, h] | `[32, 32]` | Billboard size in world units. |
| `model_path` | string | `""` | OBJ / GLB model. The property panel offers presets (Oil Barrel, Ball) and Browse. |
| `rotation`, `scale` | [x, y, z] | `[0,0,0]`, `[1,1,1]` | Model transform. |
| `carry_enabled` | bool | `false` | The player can pick it up and carry it. |
| `carry_reach`, `carry_distance` | float | `110`, `55` | Pick-up reach; holding distance. |
| `carry_offset` | [x, y, z] | `[0,-6,0]` | Held offset. |
| `drop_velocity`, `drop_angular_velocity` | float, [x, y, z] | `0`, zeros | Motion given on dropping. |
| `collect_enabled` | bool | `false` | It can be collected. |
| `collect_type` | `item` / `health` / `key` / ... | `item` | What collecting gives. |
| `collect_item` | item id | `gun1` | The [item](https://github.com/ViciousSquid/Fio/wiki/Items) given when `collect_type` is `item`. |
| `collect_value` | int | `25` | Amount (health, etc.). |
| `collect_key_name` | string | `blue_key` | Key given when `collect_type` is `key`. |
| `collect_activation` | `walk_over` / `use` | `walk_over` | Touch it, or look at it and press Use. |
| `collect_respawns`, `collect_respawn_time` | bool, float (s) | `false`, `20` | Whether and when it comes back. |
| `collect_custom_sprite` | string | `""` | Sprite override for a collectable. |
| `collect_collected` | bool | `false` | Runtime state (saved in save games). |
| `physics_enabled` | bool | `false` | Simulate as a dynamic body. |
| `no_collision` | bool | `true` | Not solid. |
| `collision_shape`, `collision_size` | string, [x, y, z] | `auto`, zeros (derive) | Collision volume. |
| `mass`, `gravity`, `friction`, `linear_damping`, `angular_damping` | | `1.0`, `true`, `0.55`, `0.08`, `0.12` | Physics parameters. |
| `disabled` | bool | `false` | Inactive. |

## Inputs

`Enable`, `Disable`, `Collect`, `Respawn`, `SetValue` (int), `Drop`, `Wake`, `Hide`, `Show`, `ToggleVisibility`, `Reset`.

## Outputs

| Output | Fired |
| --- | --- |
| `OnCarried` / `OnDropped` | The player picks it up / lets go. |
| `OnRest` | A physics Prop comes to rest. |
| `OnCollected` | It is collected. The activator is the collector (the player, or a plugin's actor). |
| `OnRespawn` | A collected Prop returns. |
| `OnTidied` | Fired by the Tidy plugin. |

## For plugins

`PropSession.collect_prop(prop, grant=..., collector=...)` collects a Prop for a non-player actor: `grant(kind, amount, item)` applies the reward and may refuse; the Prop domain still owns consumption, physics, I/O and respawn. The Deathmatch bots use it. Call it on the logic thread.

```json
{"type": "prop", "pos": [60, 16, 40],
 "properties": {"name": "Shotgun", "collect_enabled": true,
                "collect_type": "item", "collect_item": "gun2"}}
```

Tests: `tests/physics/`, `tests/e2e/test_custom_items.py`, `tests/logic/`.
