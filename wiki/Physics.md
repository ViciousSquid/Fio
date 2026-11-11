> Applies to Fio 3.1.0.0611 (CANARY).

Fio has one physics: the player, monsters, props and physics bodies share one world and one gravity. Modules: `engine/player.py` (player movement), `engine/physics.py` (the `PhysicsWorld`, bodies, queries), `engine/logic_collision.py` (collision rows), `engine/brush_placement.py` and `engine/mover_table.py` (where a brush is), `engine/constants.py` (the numbers).

## Player movement (Quake 2's rules on Fio's collision)

| Constant | Value | Meaning |
| --- | --- | --- |
| `CL_FORWARDSPEED`, `CL_SIDESPEED` | 200 | Walk speed; a diagonal adds up (283). |
| `PM_MAXSPEED` (`PM_SPRINT_SCALE` 1.5) | 300 | Sprint. |
| `PM_DUCKSPEED` | 100 | Crouched. |
| `PM_ACCELERATE` / `PM_AIRACCELERATE` | 10 / 1 | Ground / air acceleration. |
| `PM_FRICTION`, `PM_STOPSPEED` | 6, 100 | Ground friction. |
| `JUMP_STRENGTH` | 270 u/s | About 46 units high, about 64 with the step-up. |
| `TERMINAL_VELOCITY` | -800 | Fastest fall. |
| `PM_GROUND_NORMAL` | 0.7 | A surface steeper than about 45 degrees is not ground. |
| `PM_LAND_SPEED` / `PM_LAND_TIME` | -200 / 0.144 s | A landing faster than 200 u/s holds off the next jump. |
| `PM_HARD_LAND_SPEED` / `PM_HARD_LAND_TIME` | -400 / 0.2 s | Likewise, harder. |
| `PLAYER_HULL_SIZE` | 50 x 100 x 50 | Standing hull. |
| `PM_DUCK_HEIGHT_FRACTION` | 0.5 | Crouching halves the hull from the top. |

Jump must be released between jumps; a press in the air jumps on landing. Steps up to 18 units are taken on the ground and in the air. Crouching fits under 50-unit gaps, and the player stands up only where there is room. The forward wish is shortened by a third of the view pitch.

## Gravity

Gravity is 800 and has one authority, the live `PhysicsWorld` (`engine.physics.world_gravity`). The player, monsters, dropped props and bodies all read it there, so `phys_gravity` changes how everything falls.

## Collision

The world is brushes filed in the [spatial grid](https://github.com/ViciousSquid/Fio/wiki/Spatial-Grid) (512-unit cells). A box brush collides as its box; an angled brush as its convex shape, in the brush's own frame. Triggers, fog and water are not solid; a hidden brush is not solid (`engine.constants.is_solid_world_brush` is the one rule). Terrain is collided against its height field.

## Moving brushes

A brush's `pos` is its **authored** placement and never changes in play. Doors and movers are moved by the mover table, which owns their runtime transforms; anything that needs to know where a brush is now -- collision, the renderer, plugins -- asks `engine.brush_placement.placement(brush)` (`authored`, live centre, `spin`, bounds). A mover outside the simulated area is parked: it costs nothing per tick, its I/O stays live, and its state catches up when it is next needed.

## Props and bodies

A [Prop](https://github.com/ViciousSquid/Fio/wiki/Prop) with `physics_enabled` is a dynamic body (`mass`, `friction`, `linear_damping`, `angular_damping`, `gravity`, `collision_shape`). Bodies sleep when at rest (`OnRest`).

## Console (Play only)

| Command | Effect |
| --- | --- |
| `physics` | Turn dynamic-body physics on or off. |
| `phys_gravity <units/s^2>` | Global gravity, -5000 to 5000; 0 disables it. |
| `phys_timescale <multiplier>` | Scale simulation time. |
| `phys_friction <multiplier>` | Scale every body's friction. |
| `phys_damping <multiplier>` | Scale every body's damping. |
| `phys_sleep on|off|toggle` | Automatic sleeping. |
| `phys_info` (`phys`) | Show the global controls. |
| `phys_reset` | Restore the defaults. |

Also `noclip`, `god`, `buddha`, `notarget`, `setpos x y z`. `[Settings] physics` in `settings.ini` is the start-up default. Tests: `tests/physics/`, `tests/brush_model/`.
