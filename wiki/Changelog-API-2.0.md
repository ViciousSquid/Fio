## Plugin API 2.0

API 2.0 is API 1.9 with additions. Nothing in 1.9 is changed or removed, every plugin written for 1.x loads and behaves as it did, and the surface is locked at this version.

**Soma, the physics engine.** Physics is now a subsystem of the engine behind a contract, as the renderer is, and it has a name. Plugins communicate with it through the API:

- `RuntimeAPI.physics` (and `RuntimeAPI.soma`), `body`, `impulse`, `explode`, `bodies_near`, `bodies_in_box`, `raycast_bodies`, `spatial_grid`
- events `physics.rest`, `physics.landed`, `physics.contact`, `physics.toppled`
- a physics Prop spawned during play gets a body
- `EditorAPI.register_physics` / `select_physics`: the physics engine can be swapped out the way the renderer can

**The player.** `RuntimeAPI.player` and `RuntimeAPI.player2`: position, velocity, teleport, view, health, armour, damage, speed, noclip, keys, weapons, what is carried.

**LogicState.** `RuntimeAPI.state(store)`: typed values, and writes that fire the map's `OnValueSet` / `OnValueChanged`. Every change is also the `state_changed` event.

**Flags.** `set_flag` / `clear_flag` are how a plugin changes an entity's `hidden` or `disabled`.

**One thing to check in an existing plugin.** A plugin that writes `entity.properties['hidden']` or `['disabled']` directly still loads, but physics no longer notices such a write on its own. Use `set_flag` / `clear_flag`; the physics engine prints a warning naming an entity changed any other way.

**Physics, for everyone.** Props collide with each other, stack and knock each other on; they stand on slopes, angled brushes and terrain; barrels fall over, roll and are picked back up; a body stops at the wall it meets and cannot pass through a thin one. A physics step costs between a half and a tenth of what it did.
