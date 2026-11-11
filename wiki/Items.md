> Applies to Fio 3.1.0.0611 (CANARY).

An **item** is something the player holds or collects. Fio has four stable item slots whose ids never change; maps refer to them (`Prop.collect_item`) and the loadout carries them. Module: `engine/items.py` (no GL, no Qt); editor: `editor/item_editor.py`.

| Slot (key) | Id | Default definition |
| --- | --- | --- |
| 1 | `gun1` | Pistol -- hitscan, infinite ammo |
| 2 | `gun2` | Shotgun -- hitscan, 1 s cooldown, uses ammo |
| 3 | `custom1` | Cigarette -- held and shown, never fires |
| 4 | `custom2` | Wine Glass -- held and shown, never fires |

`gun1` and `gun2` are built in. `custom1` and `custom2` can be redefined per map; their definitions are saved in the map's top-level `items`.

## Two forms

- **Authoring**: plain dicts (`DEFAULT_DEFINITIONS`), held by `ItemDefinitions` on the editor state and saved with the map.
- **Runtime**: frozen `Item` objects compiled once by `compile_definition` and looked up through `ItemRegistry` (`combat_runtime.items.resolve(id)`). Nothing reads the authoring dicts during play. A definition that does not validate does not compile: its id resolves to `None`, the error is kept on the registry, and it never stands in for another item.

## Schema

`Item`: `id`, `name`, `kind` (`weapon` or `pickup`), `world_sprite`, `hud_sprite`, `hud_align` (`right`, `center`, `left`), `hud_height` (on a 600-pixel-tall view), and one of:

| `WeaponSpec` field | Meaning |
| --- | --- |
| `mode` | `none` (held, never fires), `hitscan`, `projectile`, `melee` |
| `damage` | Whole hit points |
| `range`, `cooldown` | Reach; seconds between shots |
| `pellets`, `spread` | Pellets per shot; cone half-angle in degrees |
| `ammo` | Ammunition granted the first time it is picked up |
| (ammo per shot) | Ammunition each shot spends; 0 never runs out |

| `PickupSpec` field | Meaning |
| --- | --- |
| `activation` | `walk_over` or `use` |
| `effect` | `health`, `ammo`, `armor`, `weapon`, `key` |
| `amount` | How much |
| `item_id` | The weapon an `effect: weapon` pickup gives |
| `key_name` | The key an `effect: key` pickup gives |
| `respawns`, `respawn_time` | Whether and when it returns |

## Giving items

A collectable [Prop](https://github.com/ViciousSquid/Fio/wiki/Prop) with `collect_type: "item"` and `collect_item: "<id>"` gives the item. `combat_runtime.weapons` is the set of weapons the player has; number keys 1-4 select by slot. All pellets of one hitscan shot trace the same instant of the world and their hits are applied afterwards.

Tests: `tests/e2e/test_custom_items.py`.
