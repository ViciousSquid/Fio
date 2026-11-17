> Applies to Fio Canary 1611 and later (plugin API 2.0).

# Soma

Soma is Fio's physics engine. *Soma* is Greek for "body", and that is the whole of its job: **Soma simulates bodies** — the [Props](https://github.com/ViciousSquid/Fio/wiki/Prop) whose **Physics** box is ticked. They fall, slide, roll, bounce, stack, knock each other about, stand on slopes and terrain, and are pushed and carried by the player.

Soma is a subsystem of the engine, not a plugin. It sits behind a contract, the way the renderer does, so it can be worked on — or swapped out — without the rest of the engine knowing.

| | |
| :--- | :--- |
| What is a body | A Prop with `physics_enabled` set. Everything else is scenery |
| Where it lives | `engine/physics/soma.py` (the engine); `engine/physics/api.py` (the contract); `engine/physics/registry.py` |
| The world it stands in | `engine/spatial_grid.py: SpatialGrid` — the single source of truth for where the world is. Soma keeps no copy of it |
| Switch it off | **Settings → Play Modes → Enable physics**. Off, Soma is not created and not loaded, and physics Props are scenery |
| Console | `phys_gravity`, `phys_timescale`, `phys_friction`, `phys_damping`, `phys_sleep`, `phys_info`, `phys_reset`, `phys_engine` |
| Plugins | [Plugin API 2.0](https://github.com/ViciousSquid/Fio/wiki/Plugin-API): `api.physics`, `api.impulse`, `api.explode`, the `physics.*` events |
| Benchmark | `tools/bench_physics.py` |
| Tests | `tests/physics/` |

## What a body does

| | |
| :--- | :--- |
| **Falls and lands** | By the world's gravity (800 units/s², Quake 2's). It rests on the highest ground under five points of its footprint, so it does not drop through the seam between two floor brushes |
| **Meets walls** | It stops *at* the wall — 1/32 unit short of the face it reaches — and the test covers the whole of its move, so a fast body cannot step over a thin wall. A door or mover is a wall where it is *now* |
| **Stands on slopes** | An angled brush is its true surface, not the box around it. A slope up to 45° is climbed and followed back down; a steeper one is in the way |
| **Slides and rolls downhill** | It slides where the slope is steeper than its friction (rise/run greater than `friction`). A Prop that **rolls** rolls on almost any slope |
| **Stands on terrain** | Where the map's terrain is solid, on the surface that is drawn |
| **Meets other bodies** | Bodies stack, block each other and knock each other on by the momentum they share: a heavy body barely notices a light one. What rests on a body falls when that body is knocked away, carried off or hidden |
| **Is pushed by the player** | Walking into a body pushes it. A body of mass 10 or more is *heavy*: the player cannot push or carry it, it pushes the player, and it crushes a player it pins against a wall |
| **Sleeps** | A body at rest stops being simulated until something touches it. A sleeping world costs almost nothing |

Bodies placed overlapping in the map stay free of each other. Hidden, disabled, carried and non-solid (`no_collision`) bodies are in nobody's way.

## Barrels

A Prop that **falls over** — the oil barrels do by default — is knocked over, lies on its side, rolls, and can be picked back up.

| | |
| :--- | :--- |
| Goes over when | It is moving along the ground faster than 200 units/s — a walking push slides it upright, a running one knocks it over, and so does another body hitting it; or it lands hard while moving; or it stands on ground steeper than about 35° |
| Which way | The way it was going (or downhill), if there is room to lie there |
| On its side | It rolls across its length and slides along it. It rolls down slopes it would have stood on |
| Picked up | Carrying it stands it upright again, in the hands |
| Saved games | A barrel lying when the game was saved is lying when it is loaded |

Nothing about tipping is simulated: going over is a decision, a swap of two of the box's dimensions, and a quarter turn played over a quarter of a second. A barrel costs what any other body costs.

## Prop properties

Set in the [Property editor](https://github.com/ViciousSquid/Fio/wiki/Property-editor), under the Prop's physics settings.

| Key | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `physics_enabled` | bool | `false` | Simulate this Prop as a body |
| `no_collision` | bool | `true` | Pass through walls, the player and other bodies |
| `mass` | float | `1.0` | Scales pushes, knocks and bounce. 10 or more is heavy |
| `gravity` | bool | `true` | Falls |
| `friction` | float 0–1 | `0.55` | Grip on the ground. Also the steepest slope (rise/run) it will sit on |
| `linear_damping` | float | `0.08` | Share of velocity lost per second, in the air or on the ground |
| `bounciness` | float 0–1 | `0.0` | Share of impact speed kept; fades to nothing as mass approaches heavy |
| `rolls` | bool | `false` | Rolls like a ball: turns by the distance it travels and keeps far more of its speed |
| `topples` | bool | barrels: `true` | Falls over like a barrel (see above). A Prop that `rolls` does not |
| `drop_angular_velocity` | degrees/s | `[0, 0, 0]` | A constant spin |
| `hidden`, `disabled` | bool | `false` | While set, the body is dormant |

## How it is built

**The world is the grid's.** `SpatialGrid` holds where everything in the world is — solid brushes and their true surfaces, movers and doors where they are now, the terrain, water — and the player, the monster AI, line of sight and Soma all ask it. Soma asks **once a step**, about each awake body's whole move, and reads walls on all three axes and the floor out of that one answer.

**Bodies are rows.** A body's state is a row in NumPy arrays. A step gathers the *awake* rows, works on them, and puts them back; nothing in a step is the size of the world.

**Tables hear through the journal.** Soma keeps its own account of which entities are hidden, disabled or past live simulation, and hears of a change through the engine's change journal and nowhere else. It never re-reads an entity to find out. A plugin changes those flags with `set_flag` / `clear_flag`; a value written straight into `entity.properties` is not heard, and Soma prints a warning naming the entity.

**Simulation tiers.** With [Big World](https://github.com/ViciousSquid/Fio/wiki/Big-World), a body in a cell that is *distant* or further is held exactly as it is and carries on when the cell is near again.

## Measuring

`python tools/bench_physics.py` — a floor with 1,500 pillars and N bodies sliding among them, never allowed to sleep. Median step, in microseconds, on the development container (the machine moves the numbers; the ratio much less):

| Awake bodies | Before Soma | Soma |
| ---: | ---: | ---: |
| 1 | 256 | ~180 |
| 16 | 874 | ~480 |
| 64 | 3,680 | ~580 |
| 256 | 10,363 | ~1,150 |
| 1,024 | 21,409 | ~3,100 |
| 4,096 | 35,954 | ~15,400 |

A world of 4,096 sleeping bodies costs about 7 µs a step, and about 25 µs with the player in it.

## For plugins

A plugin communicates with Soma through [plugin API 2.0](https://github.com/ViciousSquid/Fio/wiki/Plugin-API):

```python
def connect(self, host):
    host.on("physics.contact", self.on_contact)       # recorded only while subscribed

def on_contact(self, event):
    if event.data["speed"] > 400.0:
        self.api.explode(event.data["entity"].pos, 300.0, 600.0)
```

| | |
| :--- | :--- |
| `api.physics` | The session's physics engine: world controls, `wake`, `set_kinematic`, `sync_entity_position`, `on_rest` |
| `api.impulse(entity, vector)`, `api.explode(centre, radius, strength)` | Push one body, or everything near a point |
| `api.bodies_near(point, radius)`, `api.bodies_in_box(lo, hi)`, `api.raycast_bodies(origin, direction, reach)` | Find bodies |
| `api.body(entity)` | One body, live: `velocity`, `position`, `mass`, `friction`, `awake`, `grounded`, `resting_on`, `toppled` |
| `api.soma.topple(entity, direction)`, `api.soma.stand(entity)` | Knock a barrel over; stand it up |
| Events | `physics.rest`, `physics.landed`, `physics.contact`, `physics.toppled` |

A physics Prop created with `api.spawn` during play gets a body.

**With physics switched off** (or outside Play) nothing a plugin asks fails: `api.physics` is a stand-in that does nothing — a query finds no bodies, a push pushes nothing, a control is accepted and forgotten — and `api.impulse`, `api.explode`, `api.bodies_near`, `api.bodies_in_box` and `api.raycast_bodies` answer `False`, `[]` or `None`. `if api.physics:` and `api.physics_enabled` say whether an engine is running.

## Swapping the physics engine

The engine drives whichever physics is registered and selected through the contract in `engine/physics/api.py` — the `Physics` and `Body` protocols — and nothing else. Another engine is registered with `EditorAPI.register_physics(name, factory)` and chosen with `select_physics(name)` or `phys_engine <name>` in the console; it takes effect the next time Play starts. `tests/helpers/physics.py` holds the smallest conforming implementation. Two rules come with the contract: the world is the `SpatialGrid` the factory is given, asked live; and a moved body is handed back by *assigning* `entity.pos`.
