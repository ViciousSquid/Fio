> Applies to Fio 3.1.0.0611 (CANARY).

<img width="50" height="50" src="https://github.com/user-attachments/assets/e3c10063-cbcf-4e7d-9531-7fb1a22864fb" />

A **Monster** (`type: "monster"`, class `editor.things.Monster`) is a hostile actor simulated by the [Monster AI Thread](https://github.com/ViciousSquid/Fio/wiki/Monster-AI-Thread). It is drawn as a billboard sprite with idle, shoot and dead states.

## Properties

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `monster_type` | `human` / `flying` | `human` | Sprite family and behaviour (`MONSTER_TYPES`). |
| `variant` | string | `<None>` | Sprite variant folder (`assets/sprites/monsters/<type>/<variant>/`). |
| `angle` | float (degrees) | `0.0` | Authored facing. |
| `health` | int | `100` | Hit points. |
| `damage` | int | `10` | Damage per attack. |
| `sprite_width`, `sprite_height` | int | `128`, `192` | Sprite size in world units. |
| `wake_on_sight` | bool | `true` | Wakes when it sees the player. |
| `awake` | bool | `false` | Starts awake. |
| `triggered` | bool | `false` | Waits for a `Wake` input. |
| `patrol` | bool | `false` | Walks a [PathNode](https://github.com/ViciousSquid/Fio/wiki/PathNodes) chain. |
| `patrol_target` | string | `""` | First PathNode of the patrol. |
| `patrol_mode` | `loop` / `ping_pong` / `once` | `loop` | What happens at the end of the chain. |

The Customise dialog (`editor/monster_customise_dialog.py`) sets custom sprites (`custom_idle`, `custom_shoot`, `custom_dead`).

## Sprites and facing

Each state has one image (`idle.png`, `shoot.png`, `dead.png`) and, optionally, eight directional images `idle_1.png` ... `idle_8.png` (and likewise `shoot_N`). With directional images the monster shows the one that matches the angle between its facing and the viewer. Facing is persistent: `engine/monster_facing.py: face_direction` turns the monster towards what it is chasing, hearing or walking to, and it keeps that facing until it turns again (`docs/monster-facing.md`). A wounded monster shows blood on its sprite unless gore is off (`EntityTable.sprite_wound`).

## Constants (`engine/monster_constants.py`)

| Constant | Value |
| --- | --- |
| `MONSTER_SIGHT_RANGE` | 1024 |
| `MONSTER_MOVE_SPEED` | 90 units/s |
| `MONSTER_STOP_DISTANCE` | 85 |
| `MONSTER_BITE_DISTANCE`, `MONSTER_BITE_DAMAGE_MULT` | 80, 2.0 |
| `MONSTER_SHOOT_INTERVAL`, `MONSTER_SHOOT_ANIM_TIME` | 1.5 s, 0.35 s |
| `MONSTER_PROJECTILE_SPEED`, `MONSTER_PROJECTILE_MAX_DIST` | 512 units/s, 2048 |
| `MONSTER_DETOUR_RANGE` | 1024 |

## Inputs

`Enable`, `Disable`, `Kill`, `SetTarget` (string), `Wake`, `Sleep`, `SetHealth` (int), `Respawn` (int), `Hide`, `Show`, `ToggleVisibility`.

## Outputs

`OnDeath`, `OnDamaged`, `OnSeePlayer`, `OnLostPlayer`, `OnAttack`, `OnRespawn`.

Console: `monster_kill <name>`, `monster_revive <name>`, `monster_revive_all`. Tests: `tests/monster_ai/`. Example: `maps/MonsterTest.json`.
