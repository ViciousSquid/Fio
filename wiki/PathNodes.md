> Applies to Fio 3.1.0.0611 (CANARY).

<img width="50" height="50" src="https://github.com/user-attachments/assets/9a9603f0-a1cb-4547-98e6-f6bdb125d33d" />

A **PathNode** (`type: "path_node"`, class `editor.things.PathNode`) is a point on a path. Nodes are chained through `next_node`. Monsters patrol chains, a [LogicCamera](https://github.com/ViciousSquid/Fio/wiki/Camera) flies along them, and a mover can follow one (`FollowPath`).

## Properties

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `next_node` | string | `""` | Name of the next node in the chain. |
| `radius` | float | `256.0` | Arrival radius. |
| `show_radius` | bool | `false` | Draw the radius in the editor. |
| `affects_type` | string | `both` | Which monster types may use the node; a monster of another type is rejected. |
| `wait_time` | float (s) | `0.0` | How long a traveller waits on arrival. |
| `speed` | float | `1.0` | Speed multiplier for the leg that leaves this node. |

The legacy key `patrol_speed` is no longer read; maps must use `speed`.

## Inputs and outputs

Inputs: `Enable`, `Disable`, `Toggle`.

| Output | Fired |
| --- | --- |
| `OnMonsterArrived` / `OnMonsterLeft` | A patrolling monster reaches / leaves the node. |
| `OnWaitStart` / `OnWaitEnd` | A wait begins / ends. |
| `OnCameraArrived` | A LogicCamera reaches the node. |

A monster's `patrol_mode` (`loop`, `ping_pong`, `once`) decides what happens at the end of a chain. The Deathmatch plugin's bot navigation uses its own waypoint entities, not PathNodes ([Deathmatch Plugin](https://github.com/ViciousSquid/Fio/wiki/Deathmatch-Plugin)).
