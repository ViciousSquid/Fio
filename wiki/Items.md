Fio has four item slots, each with a stable id. The player selects a weapon they are carrying with the number keys **1–4** in Play.

| Key | Id        | Ships as   | Behaviour                                   |
| --- | --------- | ---------- | ------------------------------------------- |
| 1   | `gun1`    | Pistol     | Hitscan, 25 damage, never runs out of ammo  |
| 2   | `gun2`    | Shotgun    | Hitscan, 75 damage, 1 s cooldown, 8 shells on first pickup, 1 per shot |
| 3   | `custom1` | Cigarette  | Held and shown, never fires                 |
| 4   | `custom2` | Wine Glass | Held and shown, never fires                 |

A number key only switches to a weapon the player has picked up; otherwise it does nothing. When it switches, the weapon's sprite shows briefly in the bottom-right corner; collected key icons move left to make room while it shows. The player keeps their weapons and ammunition through a LevelChanger.

## Custom items

**Tools → Custom Items…** configures `custom1` and `custom2` for the open map. The tabs are titled by slot and name, for example *Custom 1 — Cigarette*. Each slot can be:

* **a weapon**, with a mode (*None* holds it without firing; *Hitscan*; *Projectile*, fired into the same projectile system monsters use; *Melee*, a short hitscan that leaves no bullet mark), damage, range, cooldown, pellets and spread, ammunition given on first pickup, ammunition per shot (0 = infinite), sound, noise (how loudly monsters hear it), muzzle-flash sprite and projectile speed; or
* **a pickup**, collected by walking over it or pressing Use, which gives health, ammo, armor, a key, or one of the four weapons, and can respawn.

Every slot has a name, a world sprite (the pickup in the level; 60×60 recommended), a HUD sprite (held in the player's view) with its position (right hand, centre, left hand) and height. *Reset to Default* restores the shipped definition. Applying is one undo step.

A Prop gives an item with **Collect as: Item** and the item chosen by name. The Prop references the item by id, so it always shows and gives what the definition says; editing the definition updates every such Prop.

There is one ammunition pool, shared by every weapon. Armor absorbs damage before health, from every source of damage, up to 100.

## In the map file

Definitions that differ from the shipped ones are saved in the map's top-level `items` object, keyed by slot id. `gun1` and `gun2` cannot be redefined. A definition that does not validate is kept as written and reported, but its item does nothing in play: a Prop that gives it is not collected, and it never turns into another item. Props save `collect_type: "item"` and `collect_item: "<id>"`; maps written with the older `collect_type: "weapon"` / `collect_weapon` load as items.

```json
"items": {
  "custom1": {
    "name": "Ember", "kind": "weapon",
    "world_sprite": "assets/sprites/custom1.png",
    "hud_sprite": "assets/sprites/custom1HUD.png",
    "hud_align": "right", "hud_height": 200,
    "weapon": {"mode": "projectile", "damage": 40, "ammo": 10,
               "ammo_per_shot": 1, "projectile_speed": 600, "sound": "shoot2.wav"}
  }
}
```

Settings left out take the defaults of an unarmed, display-only item, never another slot's values.

The `.map` exporter writes `gun1` and `gun2` pickups as its historical Quake weapons, a custom pickup as the Quake item it gives, and anything without a Quake equivalent as `info_notnull`.
