> Applies to Fio 3.1.0.0611 (CANARY).

# Monster AI Thread

Monster behaviour — waking, target selection, chasing, attacking, patrol, sound investigation, falling — is implemented by `engine/monster_ai.py: MonsterAI` and driven by its own thread, `MonsterAIThread`, at 30 Hz while a play session is running. The logic thread never calls `MonsterAI.update`; the two meet only through `LogicSession.monster_lock`, the spatial grid, and the I/O manager's deferred-output queue. This page is the reference for the AI: scheduling, the per-tick state machine, perception, patrol, attacks and the dense table it computes over. The authoring side is on [Monster](https://github.com/ViciousSquid/Fio/wiki/Monster) and [PathNodes](https://github.com/ViciousSquid/Fio/wiki/PathNodes).

## Thread and scheduling

| | |
| --- | --- |
| Class | `MonsterAIThread(threading.Thread)`, `daemon=True`, `name="MonsterAIThread"` |
| Created by | `LogicSession.start_monster_ai()` — the last step of entering Play (after spawn I/O and timers). Any previous thread is stopped first. |
| Stopped by | `LogicSession.stop_monster_ai()` — the first step of leaving Play, and `LogicThread.stop()`. `stop()` sets `running = False` and an `Event` that wakes a sleeping thread; the caller joins with a 2 s timeout. |
| Rate | `tick_rate=30` → `tick_duration = 1/30` s |
| Lock | `LogicSession.monster_lock` (an `RLock`), held for the whole of each `MonsterAI.update(tick_duration)` |

The loop is a fixed-step accumulator like the logic thread's: elapsed time is clamped to 0.25 s per iteration, `update` runs once per whole `tick_duration`, and the thread then waits on its stop event for 90 % of the remaining tick. Each `update` receives exactly `tick_duration` as `delta`.

* **World pause.** While `session_runtime.world_paused` the accumulator is zeroed every iteration: monsters stand still and the paused time is dropped, not caught up.
* **Faults.** An exception in `update` is logged in full under `MonsterAI` and the thread carries on with the next tick.
* **Instrumentation.** `update_ms` (last update including lock wait) and `lock_wait_ms`, shown by Debug Tables.

`update` returns immediately when there is no player, or when `player_runtime.player_dead` — monsters freeze once the player is dead.

### Relationship to the logic tick

The AI runs at half the logic rate and is not phase-locked to it. Consequences that are part of the contract:

* **Positions** are written to the `Monster` objects by the AI thread (`set_positions` / `thing.pos = [...]`, both journalled) and read by the logic thread's render projection on its next frame.
* **Outputs** the AI fires (`OnSeePlayer`, `OnLostPlayer`, `OnAttack`, `OnDamaged`, `OnDeath`, `OnPlayerDeath`, PathNode outputs) are not dispatched on the AI thread: `IOManager.fire_output` queues outputs from any thread other than the tick thread and delivers them, in order, at the start of the logic thread's next `io_manager.update`.
* **Player damage** goes through `LogicTriggers._apply_player_damage`, which serialises on `LogicPlayer.damage_lock`.
* **The logic thread takes `monster_lock`** for the player's hitscan against monsters, for adding and advancing monster projectiles, and when rebuilding entity caches or collision. While it holds the lock the AI waits; the wait is `lock_wait_ms`.
* **Projectiles** are spawned by the AI (`combat_runtime._add_monster_projectile`) but flown by the logic thread at 60 Hz.

## State owned by the AI

| State | Where | Notes |
| --- | --- | --- |
| `MonsterAI.monster_states` | dict `id(monster) -> dict` | Created on a monster's first awake tick. Keys: `shoot_timer` (starts at `MONSTER_SHOOT_INTERVAL`), `anim_timer`, `in_sight`, `vel_y`, `investigating_sound`, plus patrol keys (below). Saved in save games keyed by UUID, without `investigating_sound`. |
| `MonsterAI.table` | `engine/monster_table.py: MonsterTable` | The dense projection of one tick. |
| Monster properties | on the `Monster` object | `awake`, `dead`, `is_shooting`, `angle`, `health`, `_aggro_target`, `_kill`, `_vel_y`, `target_name` — see [Monster](https://github.com/ViciousSquid/Fio/wiki/Monster). |
| `_debug_rays`, `monster_debug_active` | on `MonsterAI` | The F7 debug view. |

`MonsterAI.forget_monsters()` (called under the lock at every session start and stop) drops every reference to the previous session's monsters.

## Constants

All in `engine/monster_constants.py`; distances in world units, times in seconds.

| Constant | Value | Meaning |
| --- | --- | --- |
| `MONSTER_SIGHT_RANGE` | `1024` | Range within which a target is "in sight" (wake, chase, attack). Applies to every monster. |
| `MONSTER_SHOOT_INTERVAL` | `1.5` | Cooldown between attacks, and the initial delay before the first. |
| `MONSTER_SHOOT_ANIM_TIME` | `0.35` | How long `is_shooting` stays set after an attack. |
| `MONSTER_MOVE_SPEED` | `90.0` | Units per second while chasing, investigating and patrolling (patrol multiplies by the node's `speed`). |
| `MONSTER_STOP_DISTANCE` | `85.0` | A chasing monster stops closing at this distance. |
| `MONSTER_TERMINAL_VEL` | `-500.0` | Fastest fall. |
| `MONSTER_MIN_WIDTH` / `MONSTER_WALL_MARGIN` | `200.0` / `100.0` | Collision half-width used against walls. |
| `MONSTER_STUCK_THRESHOLD` | `30` | Consecutive blocked patrol ticks before a detour is sought (1 s at the AI's 30 Hz). |
| `MONSTER_DETOUR_RANGE` | `1024.0` | Search radius for a detour node. |
| `MONSTER_BITE_DISTANCE` | `80.0` | A flying monster this close to the player bites instead of shooting. |
| `MONSTER_BITE_DAMAGE_MULT` | `2.0` | Bite damage multiplier. |
| `MONSTER_PROJECTILE_SPEED` | `512.0` | Flying-monster projectile speed. |
| `MONSTER_PROJECTILE_MAX_DIST` | `2048` | Projectile range; lifetime is `2048 / 512 = 4` s. |
| `MONSTER_PROJECTILE_SPRITE_SIZE` | `(40.0, 40.0)` | Projectile billboard size. |
| `MONSTER_SHOOT_SOUNDS` | `human: shoot.wav`, `flying: shoot_flying.wav` (default `shoot.wav`) | Attack sound, queued at volume 0.6. |
| `MONSTER_TYPES` | `('human', 'flying')` | The complete type vocabulary. |

Gravity is not a monster constant: monsters fall by `engine.physics.world_gravity(session.physics_world)` — the live `PhysicsWorld.gravity`, −800 by default and changed by `phys_gravity` ([Physics](https://github.com/ViciousSquid/Fio/wiki/Physics)).

## One update

`MonsterAI.update(delta)`:

1. Clear debug rays and invalidate the nearest-enemy batch.
2. Take the monster list from `world_runtime.monster_things`. With a Big World session fitted to an overhead camera (`_view_rect()` returns the session's `tiers.near_rect`), monsters whose simulation tier is `TIER_DORMANT` are left out of the tick altogether.
3. Choose the path with `_fallback_reason()` and record it in `table.path`.
4. Compute off-screen throttling (`_offscreen_rows`, Big World overhead only).
5. Run `_update_dense(...)`, or `_update_monster(...)` per monster on the fallback path.
6. If `player_health <= 0` and the player is not yet marked dead: set `player_runtime.player_dead = True` and fire `OnPlayerDeath` on the Player Start the session spawned at (`session_runtime.spawn_start`).

### Dense path and per-monster path

There are two implementations of the same decisions. `_update_monster` is the per-object reference; `_update_dense` makes the same decisions for every row at once over `MonsterTable`, and sends irregular rows to `_update_monster`. `table.path` is `'dense'` or the reason the whole tick fell back:

| `table.path` | When |
| --- | --- |
| `dense` | Normal play. |
| `per-monster (DENSE_UPDATE off)` | `MonsterAI.DENSE_UPDATE = False` (the switch tests use to run the reference). |
| `per-monster (no spatial grid)` | No `SpatialGrid` set (`set_spatial_grid(None)`). |
| `per-monster (notarget)` | The `notarget` cheat is on. |
| `per-monster (F7 debug view)` | `monster_debug_active` — every monster's sight ray is drawn. |

Within a dense tick a row runs `_update_monster` (mode `scalar`) when: it is asleep with `wake_on_sight` false; it wakes this tick; it has a pending `_kill`; it has a `target_name`; or its `_aggro_target` no longer resolves to a living monster.

One ordering difference is deliberate: in the dense pass every monster's step is applied before any shot, so a monster killed by another monster's shot this tick has already taken that tick's step.

### Row modes

`MonsterTable.mode` records what each row did (`MODE_NAMES`):

| Code | Name | Meaning |
| --- | --- | --- |
| 0 | `skipped` | `hidden` or `disabled` (or sitting the tick out off screen). |
| 1 | `dead` | Dead: falls to the ground. |
| 2 | `asleep` | Not awake and nothing woke it. |
| 3 | `chase` | Target within sight range: faces it, may move, may attack. |
| 4 | `search` | Target out of range: investigate a noise or patrol. |
| 5 | `scalar` | Irregular row, run through `_update_monster`. |
| 6 | `waiting` | Asleep with `triggered` set: waits for its `Wake` input. |

## State machine

Evaluated top to bottom each tick for each monster.

**1. Hidden / disabled.** `hidden` → `is_shooting` removed, nothing else. `disabled` → nothing.

**2. Dead.** `is_shooting` removed. The corpse drops: with ground below (`raycast_down` from its position), target height is `ground + sprite_height / 2`; above it by more than 1 unit it accelerates by gravity (clamped to `MONSTER_TERMINAL_VEL`) using `_vel_y`, otherwise it snaps to the target height.

**3. Asleep** (`awake` false):

| Condition | Result |
| --- | --- |
| `triggered` true | Stays asleep; ignores sight and sound. Only the `Wake` input wakes it. |
| `wake_on_sight` false | Wakes immediately. |
| Player within `MONSTER_SIGHT_RANGE` (straight-line distance; no line-of-sight test) | Wakes. |
| It has a `team` and a living, visible monster of another team is within `MONSTER_SIGHT_RANGE` | Wakes. |
| `can_hear` and a player noise from the last 2 s is within hearing range | Wakes. |
| Otherwise | Stays asleep. |

Being damaged by another monster also wakes it (`_apply_monster_damage` sets `awake`).

**4. Kill input.** `_kill` (set by the `Kill` input) → `dead = True`, tick ends.

**5. Gravity** (ground monsters only; `monster_type != 'flying'`). Ground is sampled 10 units above the monster's centre. Feet are `pos.y - sprite_height / 2`. More than 1 unit above ground → fall with `vel_y`; otherwise snap the centre to `ground + sprite_height / 2` if it is off by more than 1 unit and zero `vel_y`. The rest of the tick starts from the post-gravity position.

**6. Notarget.** With the cheat on: `is_shooting` false; a `can_hear` monster investigates sounds, any other patrols; nothing chases or attacks.

**7. Target.** In priority order:

1. `target_name` (set by the `SetTarget` input): the named entity's position. An empty string or an unresolved name removes the override.
2. `_aggro_target`: the monster that last damaged this one, while it is alive.
3. Team targeting: with a non-empty `team`, the nearest living, non-hidden monster of a different non-empty team within `MONSTER_SIGHT_RANGE`.
4. The player.

**8. In sight** (squared distance to the target ≤ `MONSTER_SIGHT_RANGE²`) → *chase*:

* Face the target (`monster_facing.face_direction` writes `angle`).
* On the tick sight is gained: `in_sight = True`; if the target is the player, fire `OnSeePlayer`.
* If farther than `MONSTER_STOP_DISTANCE`: step `MONSTER_MOVE_SPEED * delta` toward the target (ground monsters move in XZ only; a target directly above or below gives no step). If the full step would overlap a wall, try the X-only step, then the Z-only step; if all three are blocked, do not move.
* `shoot_timer -= delta`. When it reaches zero a line-of-sight ray is cast from the monster's eye (64 units above its origin) to the target's eye (64 above a monster's origin; `camera_height` above the player's position). Clear → attack, `shoot_timer = MONSTER_SHOOT_INTERVAL`, `anim_timer = MONSTER_SHOOT_ANIM_TIME`. Blocked → `shoot_timer = 0.1` (re-check soon).
* `is_shooting` is true while `anim_timer > 0`.

Line of sight is needed only to attack. A monster chases and wakes on distance alone, through walls.

**9. Out of sight** → *search*:

* On the tick sight is lost: `in_sight = False`; if the target was the player, fire `OnLostPlayer`.
* `is_shooting` false, `anim_timer = 0`, `_aggro_target` dropped.
* `_search`: a `can_hear` monster first tries `_investigate_sounds`; if it is not investigating (or cannot hear), `_update_monster_patrol`.

## Hearing

Noise events are recorded by the logic thread in `LogicCombat._emit_noise_event(pos, source, loudness)` and kept for 3 s (`prune_noise_events(3.0)`):

| Source tag | Emitted by | Loudness |
| --- | --- | --- |
| `gunfire` | A player shot, at the shot origin | The weapon's `noise` |
| `water_enter`, `water_exit` | The player entering or leaving water | `0.7` |

Only monsters with `can_hear` true hear. A monster's hearing range is its **`sight` property** (default `MONSTER_SIGHT_RANGE` when the property is absent) multiplied by the event's loudness; the nearest audible event is chosen.

* **Waking** (`_hears_noise`) considers events up to 2 s old.
* **Investigating** (`_investigate_sounds`) considers events up to 3 s old, but stops once the chosen event is older than 2 s or the monster is within 64 units of it. While investigating the monster faces and walks toward the event position at `MONSTER_MOVE_SPEED`, with the same wall sliding as a chase. Returning `False` lets patrol run instead.

## Patrol

`_update_monster_patrol` runs only when the target is out of sight (or under `notarget`) and only when the monster has `patrol` true and a non-empty `patrol_target`.

**Chain.** `_build_patrol_chain(patrol_target, monster_type)` follows `next_node` links from the named PathNode, stopping at a missing node, a node that does not accept this `monster_type`, or a node already visited. The chain is rebuilt when `patrol_target` changes. An empty chain logs once per target under `Pathfinding` (target not found, or rejected by `affects_type`).

**Per-monster patrol state** (in `monster_states[id]`): `patrol_chain`, `patrol_chain_built_from`, `patrol_chain_idx`, `patrol_chain_dir`, `patrol_at_target`, `patrol_waiting`, `patrol_wait_remaining`, `patrol_finished`, `patrol_walking_to`, `patrol_blocked_count`, `detour_node`, `patrol_warn_missing`.

**Each tick:**

1. If waiting at a node: count `patrol_wait_remaining` down; when it expires fire the node's `OnWaitEnd` and advance.
2. Distance to the current node — full 3D for flying monsters, XZ only for ground monsters.
3. Within the node's `radius`: on first arrival fire `OnMonsterArrived`; if the node's `wait_time > 0` start waiting and fire `OnWaitStart`; otherwise advance to the next index.
4. Having left a node's radius: fire that node's `OnMonsterLeft`.
5. Step toward the current node (or the detour node) at `MONSTER_MOVE_SPEED * node.speed * delta`, facing the direction of travel, with the same full / X / Z wall-slide fallback.
6. If all three steps are blocked, `patrol_blocked_count` increments. At `MONSTER_STUCK_THRESHOLD` with no detour in progress, `_find_nearby_detour_node` picks the nearest PathNode that is named, not the blocked node, not `disabled`, accepts the monster type, lies between 1 and `MONSTER_DETOUR_RANGE` units away, and toward which a first small step is not blocked. The monster walks to it; on reaching its radius the detour is cleared and patrol resumes toward the original node. A detour node that disappears or becomes `disabled` is abandoned.

**Advancing** (`_advance_patrol_index`), by the monster's `patrol_mode`:

| Mode | At the end of the chain |
| --- | --- |
| `loop` (default; also any unrecognised value) | Wraps to the first node. |
| `ping_pong` | Reverses direction. |
| `once` | Sets `patrol_finished` and holds at the last node. |

`OnMonsterLeft` is fired on the node left behind only when the index actually changes, so a one-node chain or a finished `once` patrol does not emit a leave/arrive pair every tick.

Patrol is suspended, not reset, by a chase: when the target goes out of range again the monster continues toward the same chain index.

## Attacks

`_monster_attack` is what one shot does; the caller owns the timers. `damage` is the monster's `damage` property (read with a default of 20 when absent).

| Monster type | Target | Effect |
| --- | --- | --- |
| `human` | An aggro or enemy-team monster | Instant hit: `_apply_monster_damage(target, damage, attacker)`. |
| `human` | The player | Instant hit, unless a monster stands in the line of fire: the ray from eye to player eye is tested against every other living, visible monster not on the shooter's team (a sphere of radius 80 centred 64 above its origin); the nearest one hit takes the damage instead (crossfire). Otherwise the player takes `damage`. |
| `flying` | The player, within `MONSTER_BITE_DISTANCE` | Bite: instant `damage × MONSTER_BITE_DAMAGE_MULT`. |
| `flying` | Anything else | A projectile from 64 above the monster's origin toward the target **position at the moment of firing**, at `MONSTER_PROJECTILE_SPEED`. It can be dodged. |

After any attack: the type's shoot sound is queued and `OnAttack` fires. Before damaging the player the AI sets `player_runtime.damage_source` to the attacker's position, which becomes a directional damage mark on the HUD.

**Monster damage** (`_apply_monster_damage(victim, damage, attacker)`): subtracts from `health`, fires `OnDamaged`; at `health <= 0` sets `dead`, clears `is_shooting` and `_aggro_target`, fires `OnDeath`. A surviving victim with a known attacker sets `_aggro_target = id(attacker)` and `awake = True` — it retaliates.

**Projectiles** are rows of `LogicCombat._monster_projectiles`, advanced by the logic thread (`_update_monster_projectiles`, a scalar path below `PROJECTILE_DENSE_THRESHOLD = 100` projectiles and a batched path above). Each tick a projectile moves, may transit a portal, and is removed when it has travelled `MONSTER_PROJECTILE_MAX_DIST`, its lifetime runs out, or it hits:

* the player — within `PROJECTILE_PLAYER_RADIUS = 32.0` of the player's position, unless `god_mode` or dead: player damage, with the projectile's position as the damage source;
* a monster — within `PROJECTILE_MONSTER_RADIUS = 64.0` of a point `PROJECTILE_MONSTER_LIFT = 64.0` above its origin; never its owner, never a monster on the owner's team, never a dead or hidden one. The victim takes damage with no attacker, so it does not acquire an aggro target;
* a solid world brush — the projectile's point is inside the brush's bounds.

## Teams

`team` is a free string. An empty team is neutral: a neutral monster never team-targets and is never team-targeted. Monsters with different non-empty teams are enemies and are preferred over the player; same-team monsters never hit each other by crossfire or projectile. The nearest-enemy search is answered for all monsters at once per tick (`_enemy_batch` → `_nearest_enemy_rows`): a single N×N distance matrix for small or many-team populations (up to 1024 monsters), per-team blocks otherwise.

## MonsterTable

`engine/monster_table.py: MonsterTable` is the dense projection. Rows are monsters in `LogicWorld.monster_things` order. The monster objects stay authoritative: columns are gathered from them at the start of a tick and results scattered back at the end. Arrays grow by doubling (minimum capacity 16) and are valid for `[:count]`.

| Column | Shape / dtype | Fill | Source or meaning |
| --- | --- | --- | --- |
| `pos` | `(3,)` float64 | 0 | `monster.pos` |
| `team` | int32 | −1 | Per-tick code for the `team` string; −1 = no team |
| `hidden`, `disabled`, `dead`, `awake`, `triggered` | bool | False | Same-named properties |
| `wake_on_sight` | bool | True | Property |
| `can_hear` | bool | False | Property |
| `flying` | bool | False | `monster_type == 'flying'` |
| `sprite_height` | float64 | 128.0 | Property |
| `shoot_timer`, `anim_timer` | float64 | 0 | From `monster_states` |
| `in_sight` | bool | False | From `monster_states` |
| `vel_y` | float64 | 0 | From `monster_states` |
| `target` | int32 | −2 | `TARGET_NONE = -2`, `TARGET_PLAYER = -1`, or a monster row |
| `target_pos` | `(3,)` float32 | 0 | Resolved target position |
| `dist_sq` | float32 | 0 | Squared distance to the target |
| `ground_y` | float64 | NaN | Ground height found this tick |
| `mode` | uint8 | 0 | Row mode (table above) |
| `ray_cast` | bool | False | A line-of-sight ray was cast for the row this tick |
| `fired` | bool | False | The row attacked this tick |

Other fields: `count`, `generation` (bumped when the row set changes), `monsters`, `props` (each monster's property dict), `row_of` (`id(monster) -> row`), `slot_of_id` (`properties['id'] -> row`), `team_names`, `rows_read`, `rays_cast`, `python_rows`, `phase_ms` (milliseconds for `gather`, `fall`, `wake`, `classify`, `chase`, `events`), `path`.

The row set is reconciled by comparing the monster list element by element by identity — not by list identity or length — so a rebuilt list of the same length is noticed.

`MonsterTable.crossfire(shooter_row, ray_start, ray_end, radius=80.0)` answers the crossfire query from the tick's columns: a scalar walk below `CROSSFIRE_NUMPY_MIN = 32` rows, a NumPy query above. Who is alive is read once per tick and re-checked for the row a shot would hit.

## Spatial queries

With a `SpatialGrid` set (always, in Play), the AI's world queries are grid queries (`engine/physics.py: SpatialGrid`):

| AI call | Grid method | Used for |
| --- | --- | --- |
| `_monster_raycast_down` | `raycast_down`, `raycast_down_batch` | Ground height under a monster |
| `_monster_overlaps_wall` | `overlaps_wall`, `overlaps_wall_batch` | A box of half-width `MONSTER_WALL_MARGIN` and height 128 against solid brushes |
| `_has_line_of_sight` | `has_line_of_sight` | The shot ray |

The dense pass issues one batched ground query and one batched wall query (three candidate positions per moving monster) per tick, and casts a ray only for rows whose shot is due. Without a grid each falls back to a scan of every solid world brush.

## Simulation tiers and off-screen throttling

`engine/spatial.py` defines `TIER_NEAR = 0`, `TIER_ACTIVE = 1`, `TIER_DISTANT = 2`, `TIER_DORMANT = 3`, stamped on an object under the property key `_sim_tier` by the Big World plugin's classifier and read with `tier_of(obj)`, which defaults to `TIER_NEAR`. An ordinary map never stamps tiers, so every monster is fully simulated.

The AI uses tiers only when a Big World session exposes an overhead simulation rectangle (`tiers.near_rect`, fitted overhead camera):

* `TIER_DORMANT` monsters are excluded from the tick and from enemy and crossfire searches.
* Monsters outside the rectangle (off screen) run once every `MonsterAI.OFFSCREEN_INTERVAL = 0.2` s instead of every tick. A row that sits out accumulates the time it is owed and receives it as its `delta` when it next runs, so no simulation time is lost.

In first person `_view_rect()` returns `None` and every monster ticks at 30 Hz.

## Debug view

`F7` in Play toggles `monster_ai.monster_debug_active` (reset to off when Play stops). While on, the tick uses the per-monster path, each awake monster's eye-to-target ray is published in `RenderState.monster_debug_rays` (`green` = clear, `red` = blocked, `orange` = heading to a sound), and wake, engage, attack, crossfire, infighting and death lines are logged under `MonsterAI`. Patrol events are always logged under `Pathfinding`.

Console commands that act on monsters: `monster_kill <name>` (alias `kill_monster`), `monster_revive <name>`, `monster_revive_all`, `notarget`.

## Tests

`tests/monster_ai/`: `test_ai_thread.py` (thread lifecycle, fixed delta, lock, fault policy), `test_monster_behaviour.py`, `test_dense_update.py` and `test_dense_delta.py` (dense pass against the per-monster reference), `test_patrol.py`, `test_nearest_enemy.py`, `test_crossfire_queries.py`, `test_projectile_batch.py`. Also `tests/engine/test_monster_ai_kernel.py`, `tests/engine/test_monster_facing.py`, `tests/physics/test_line_of_sight.py`, and the pause behaviour in `tests/engine/test_world_pause.py`.

### Source

`engine/monster_ai.py`, `engine/monster_table.py`, `engine/monster_constants.py`, `engine/monster_facing.py`, `engine/logic_combat.py` (projectiles, noise), `engine/logic_session.py` (thread lifetime).
