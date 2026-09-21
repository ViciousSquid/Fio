# Subsystem audit — Prop and physics

## The duplication question (brief §6) — answered

`editor.things.Prop` and `plugins.entitybase.Prop` are **not** intended to be
separate, and on the published snapshot they are not separate at all.
`engine/prop_entity.py` defines one class and resolves its base at import
(`editor.things.Model` when the editor tier is importable, else
`plugins.entitybase.Model`). Verified at runtime:

    editor.things.Prop is engine.prop_entity.Prop      -> True
    plugins.entitybase.Prop is engine.prop_entity.Prop -> True
    default key sets equal: True ; differing values: none

At 2.4.2 they were two hand-synchronised classes with value-identical defaults.
During the 559-commit branch they drifted and were patched by hand (commit 466,
"Keep plugin Prop render mode in sync"). The single-class resolution is the
correct fix and closes the question.

## Lifecycle, stage by stage

| Stage | Owner | Notes |
|---|---|---|
| spawn | `RuntimeAPI.spawn` / map load | `Thing.from_dict` resolves by subclass walk, so `Prop` loads despite being absent from `ENTITY_TYPES` |
| init | `PropSession.start` | snapshots `_prop_home_pos`; clears `_physics_awake`, `_drop_requested` |
| collision | `PhysicsWorld` | `collision_shape` = auto / aabb / mesh; `[0,0,0]` size = derive from mesh |
| gravity | `PhysicsWorld.step` | `GRAVITY = -900`, `dt` clamped to 0.05 |
| velocity | SoA `_velocity` | horizontal motion is new in 2.5 |
| angular velocity | `_angular_velocity` | still integrates the **constant** `drop_angular_velocity`; there is no torque and no angular damping applied, so `angular_damping` remains an authored-but-unused key, as at 2.4.2 |
| friction | `PhysicsWorld` | Coulomb, grounded only: `a = mu*g`, clamped against reversal |
| pickup | `PropSession._pick_in_view` | aim dot > 0.86, nearest within `pickup_reach`, skips `disabled` / `pickup_enabled=False` |
| carry | `PropSession._carry` | body set kinematic; prompt now published only while still carried |
| drop | `PropSession._carry` | `_prop_drop_interceptor` may consume it; body un-kinematic + woken |
| handoff | `PhysicsWorld.set_kinematic` / `wake` | position synced on pickup/drop (474) |
| resting | `_batch_floor` | snaps to `floor - offset_y + half_y`, zeroes vertical velocity |
| sleeping | `_awake` mask | integration masked to awake bodies |
| **disabled** | `PhysicsWorld.step` | **was ignored — REG-04, fixed** |
| save/load | generic `Thing` serialisation | no special format |
| destruction | `PropSession.is_empty` | prunes by `id()`; clears `held` |
| stop | `PropSession.stop` | restores `_prop_home_pos` — the editor play-stop contract, preserved |
| I/O | `Enable`/`Disable`/`Drop`/`Wake` in, `OnPickedUp`/`OnDropped`/`OnRest` out | `Hide`/`Show`/`ToggleVisibility` worked but were **undeclared** until this audit |

## Editor / runtime / player parity

All three now run the **same** `PropSession`: editor play mode via
`LogicThread`, and the standalone player via `PlayerPluginHost` (commit 522).
That is a real parity improvement over 2.4.2, where the player had no Prop
interaction at all.

**Remaining parity gap:** `player/plugin_host.py`'s `_NullIO.fire_output`
silently swallows every output. So in the standalone player a Prop fires
`OnPickedUp` / `OnDropped` / `OnRest` into nothing. Any map whose logic depends
on those outputs works in the editor and not in the shipped player. This
predates 2.5, but 2.5 makes it matter more by making Prop a core primitive
whose outputs are the documented way to react to it.

## Findings

* **REG-04** — disabled/parked bodies kept being integrated. Fixed.
* **REG-07** — `PhysicsBody` lost `size`/`offset`/material accessors, and
  `_index` did not pack. Fixed.
* **`angular_damping` is still inert.** Authored on every Prop, read by
  nothing. Either apply it to `_angular_velocity` or drop it from the defaults;
  shipping a control that does nothing is worse than not shipping it.
* **Interceptor contract is fragile** — `_carry` returns without clearing
  `self.held` when a plugin consumes the drop, relying on the plugin to do it.
  The core should clear `held` and let the interceptor choose only the
  destination.
* **`PhysicsWorld` reads `SpatialGrid.cells` and `.cell_size` directly** rather
  than using `get_potential_colliders`. Fast, but couples the two classes'
  internals — and is what broke every physics test stub.
