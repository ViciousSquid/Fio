# Big World — large persistent worlds for Fio

**Big World** is Fio's optional large-world runtime layer. It allows a single map to contain a very large number of brushes and entities while keeping only the region around the player **resident, active, and simulated**.

It is designed for **large persistent cell-based worlds and open-world games** without introducing a separate ECS, BSP/PVS compile, renderer replacement, or new world format.

Big World is a **plugin, not a new engine mode**. It sits on top of Fio's existing spatial grid, physics, renderer, entity system, terrain system, savegame/delta system, and plugin API.

> **Core idea:** don't make the engine process the whole world every frame. Divide the world into cells and only make the cells near the player live.

---

## What Big World provides

- Fixed **512 × 512 world cells**, matching Fio's existing `SpatialGrid`.
- Player-driven cell residency.
- Circular activation regions rather than square streaming regions.
- Activation/deactivation hysteresis to prevent boundary thrashing.
- Incremental streaming only when the player crosses a cell boundary.
- Multi-cell objects with UUID-based reference counting.
- Runtime parking of distant brushes, entities, and lights.
- Four cell simulation tiers:
  - **NEAR** — full simulation.
  - **ACTIVE** — resident and active, but outside the full-simulation radius.
  - **DISTANT** — resident but reduced/inactive simulation.
  - **DORMANT** — outside the active simulation region.
- Camera-independent residency and simulation.
- Persistent entities that remain active regardless of player location.
- Persistent per-cell gameplay deltas.
- Normal Fio save/load integration.
- Optional terrain streaming.
- Optional infinite procedural terrain.
- Experimental disk streaming that can actually free unloaded cell objects from memory.
- A runtime debug panel and active-cell minimap.
- No Big World runtime cost on maps that do not opt in.

The system is deliberately built so ordinary Fio maps remain ordinary Fio maps.

---

## The architecture

```text
                         Fio Map
                            │
                ┌───────────┴───────────┐
                │ Fio SpatialGrid       │
                │ 512 × 512 X/Z cells   │
                └───────────┬───────────┘
                            │
                     Big World Plugin
                            │
              ┌─────────────┴─────────────┐
              │     Residency Manager      │
              └─────────────┬─────────────┘
                            │
                    Player world position
                            │
                 ┌──────────┴──────────┐
                 │ activation radius   │
                 │ + hysteresis        │
                 └──────────┬──────────┘
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
       NEAR              ACTIVE             outside
        │                   │                   │
 Full simulation       Resident/live       parked/distant
```

Big World does **not** replace Fio's spatial grid.

The existing `engine.physics.SpatialGrid` already divides the world into 512-unit X/Z cells. Big World uses the same coordinate convention so there is only one underlying spatial partitioning model.

Objects are indexed by their existing **stable UUIDs**. A brush spanning several cells is referenced from each relevant cell but remains one object.

---

# Quick start

## 1. Opt a map into Big World

Add a **Big World Settings** entity to the map:

**Plugins → Big World → Big World Settings**

The presence of this entity is the map-level opt-in.

A map without one does not start the Big World runtime.

The entity contains the streaming configuration:

```text
enabled
activation_radius
deactivation_radius
sim_near_radius
show_cell_debug
terrain_fill
terrain_infinite
terrain_stream_radius
disk_streaming
```

## 2. Enter Play mode

Big World builds its cell index and activates the region around the player.

Moving within the same cell requires no streaming calculation.

Crossing a cell boundary causes the residency calculation to run and produces an incremental activation/deactivation delta.

## 3. Watch the debug overlay

When `show_cell_debug` is enabled, the play view displays:

- player cell
- active cells
- loaded cells
- activation radius
- active/total brushes
- active/total entities
- active/total lights
- terrain chunk residency when terrain streaming is enabled

A small top-down cell minimap shows the loaded and active region around the player.

---

# Cell residency

## 512-unit cells

Big World uses integer cell coordinates:

```text
(cell_x, cell_z)
```

derived from:

```text
floor(world_x / 512)
floor(world_z / 512)
```

This is deliberately identical to Fio's existing `SpatialGrid`.

Cell coordinates are therefore stable and deterministic across sessions.

---

## Circular activation

The activation radius is measured from the player rather than simply activating a square of cells.

The implementation first obtains the inexpensive square range of candidate cell coordinates, then rejects cells whose nearest edge lies outside the circular activation radius.

This means a 2048-unit radius does not accidentally become a 4096 × 4096 square of active cells.

---

## Hysteresis

Activation and deactivation use separate radii.

For example:

```text
activation_radius   = 2048
deactivation_radius = 2304
```

A cell becomes active when it enters the 2048-unit activation region, but remains active until it is outside the 2304-unit deactivation region.

This prevents cells repeatedly entering and leaving the active set when the player is near a boundary.

---

## No work while stationary

The streaming manager remembers the player's current cell.

If the player has not crossed a cell boundary:

```text
update()
    ↓
same cell?
    ↓
return immediately
```

There is no repeated global cell scan and no repeated tier classification simply because another game tick occurred.

This is important for low-power CPUs.

The expensive part of changing residency happens when residency actually needs to change.

---

# Simulation tiers

Big World separates **residency** from **simulation fidelity**.

A cell can remain resident without receiving full simulation.

The four tiers are:

| Tier | Meaning |
|---|---|
| **NEAR** | Full simulation fidelity. |
| **ACTIVE** | Resident and active, but outside the NEAR radius. |
| **DISTANT** | Resident but outside the active simulation region; reduced/inactive simulation. |
| **DORMANT** | Outside the active world region and effectively parked. |

The exact consumer of a tier remains an existing Fio system or game/plugin responsibility. Big World supplies the spatial classification; it does not create a parallel entity/AI/physics framework.

## NEAR

`sim_near_radius` defines the region receiving the highest simulation fidelity.

The default is:

```text
1024 units
```

The NEAR radius is clamped to the activation radius so the simulation region cannot accidentally extend beyond the streamed world.

## ACTIVE

Cells inside the resident region but outside the NEAR radius are classified as ACTIVE.

They remain resident and available to the runtime, but can be treated differently by simulation systems.

## DISTANT

Further resident cells can be classified DISTANT.

This gives game systems a way to retain coarse world state without giving distant objects the same simulation cost as nearby objects.

Note that DISTANT only appears where a cell is kept resident well past the
activation radius. The tier model's own hysteresis stretches ACTIVE out to
`activation_radius × (1 + TIER_HYSTERESIS)`, which with the shipped 2048/2304
radii lands exactly on the deactivation radius — so on the defaults a resident
cell goes straight from ACTIVE to DORMANT. A map that wants a distant band sets
a deactivation radius more than an eighth beyond its activation radius. The
model errs towards *more* simulation, which is the safe direction.

## DORMANT

Cells outside the active/resident region are DORMANT.

They remain represented by the world/index/persistence system but do not participate in ordinary nearby runtime work.

### Tier calculation is not a global per-frame scan

Tier classification occurs as part of residency changes.

The system does **not** iterate over every cell or every entity every frame.

The amount of work therefore depends on the region whose residency changed, rather than on the total population of the world.

---

# Camera independence

Big World residency is driven by the **player/world position**, not the camera.

This is intentional.

Changing from:

- first-person
- top-down
- another camera angle

does not move the streamed world around or change simulation tiers.

The camera determines what Fio renders.

The player/world position determines what Big World considers resident.

This is particularly important for Fio because the same engine supports both first-person and top-down games.

A top-down camera looking far across the map does not suddenly cause distant cells to stream in.

Likewise, moving the camera independently of the player cannot be used to change the simulation region.

---

# Objects spanning cells

An object can overlap multiple cells.

For example:

```text
        Cell A       Cell B
      ┌─────────┬─────────┐
      │         │         │
      │     ┌──────────┐  │
      │     │  Brush   │  │
      │     └──────────┘  │
      │         │         │
      └─────────┴─────────┘
```

The object is **not duplicated**.

Each cell references the same UUID.

Big World maintains reference counts so that an object is only parked/freed after its final relevant cell leaves residency.

This is particularly important for large floors, terrain structures, walls, and other geometry crossing cell boundaries.

---

# Runtime integration

Big World deliberately reuses Fio's existing systems.

| System | Big World integration |
|---|---|
| **Rendering** | Distant brushes use Fio's existing `hidden` mechanism. |
| **Physics** | Fio's existing spatial-grid collision queries continue to determine nearby potential colliders. |
| **Entities** | Distant entities use the existing `disabled`/`hidden` mechanisms. |
| **Lights** | Distant lights are hidden rather than introducing a second lighting system. |
| **Simulation** | Big World exposes cell simulation tiers to existing/game systems. |
| **Terrain** | Big World drives Fio's existing terrain streaming support. |
| **Persistence** | Uses Fio's existing save/delta machinery with per-cell bucketing. |
| **Debugging** | Uses the plugin API's `render.overlay` event. |

There is no Big World replacement for:

- Fio physics
- Fio rendering
- Fio entities
- Fio savegames
- Fio terrain
- Fio spatial partitioning

That separation is deliberate.

---

# Runtime parking

The default Big World implementation does not need to destroy distant objects.

Instead, it parks them:

```text
ACTIVE
  ↓
hidden / disabled / parked
  ↓
INACTIVE
```

When the cell returns:

```text
INACTIVE
  ↓
unpark
  ↓
ACTIVE
```

Play-stop reverses the runtime changes so the editor returns to its authored state.

Fio distinguishes authored-hidden geometry from Big World runtime parking so that parking an object does not accidentally make it disappear from collision.

---

# Persistent entities

Some entities should exist regardless of where the player is.

Examples include:

- world managers
- global state controllers
- quest controllers
- global script controllers

An entity can be marked persistent with:

```text
bw_persistent = true
```

Some global entity types are persistent by default.

Persistent entities are not treated as ordinary streamed world objects.

---

# Terrain

Big World can drive Fio's procedural terrain system.

## Terrain fill

With:

```text
terrain_fill = true
```

Big World expands terrain coverage to the world represented by the indexed cells and enables terrain chunk streaming.

The entire terrain does **not** need to be tessellated up front.

Only terrain chunks around the player are resident.

Because Fio's procedural terrain is deterministic from world position, a chunk can be regenerated when needed without changing the shape of the world.

---

## Infinite terrain

With:

```text
terrain_fill = true
terrain_infinite = true
```

terrain generation is no longer restricted to the authored world bounds.

The player can continue moving and new terrain is generated around them indefinitely.

Only nearby terrain chunks remain resident.

This provides the basis for very large or effectively endless procedural worlds without keeping an infinite mesh in memory.

---

# Persistence

Big World uses Fio's existing UUID and delta infrastructure rather than introducing a separate save format.

## Stable identity

Objects retain their existing Fio UUIDs:

```text
brush["id"]
thing.properties["id"]
```

Cell membership is derived from world position.

It is not stored as permanent object identity.

### Entities that move

A brush stays where the mapper put it, so its cell membership is settled the
moment the world is indexed. An entity walks, and the cell it was *authored* in
stops describing where it is.

Resident entities are therefore re-filed on each cell crossing, alongside the
residency work that crossing already does:

```text
player crosses a cell boundary
    ↓
resident entities that changed cell are re-filed   O(active entities)
    ↓
residency recomputed
    ↓
tiers re-evaluated
```

Only **resident** entities are considered, and that is what keeps the pass
bounded: a parked entity carries `disabled`, so nothing simulates it and it
cannot have moved. The cost never mentions the world's population.

Without this a monster that chased the player two cells from home was parked —
hidden and disabled — in the middle of a fight, while one that wandered towards
the player stayed dormant standing next to them.

Like residency itself, the pass runs on crossings rather than per frame, so an
entity's filing is accurate to within one player cell movement. That errs
towards keeping a mover resident, which is the same safe direction the tier
model chooses.

---

## Big World play-session saves

When a Big World map is saved during play:

```text
Ordinary Fio map
    → full save

Big World map
    → delta save
```

The delta is relative to the map's base state.

Big World maintains a persistent registry of changes associated with world cells and stable UUIDs.

Conceptually:

```text
cell
 ├── changed brush UUIDs
 └── changed entity UUIDs
```

A change remains recorded even after its cell leaves the player's active region.

When the cell returns, its delta is applied to the freshly restored base state.

This means the player can:

```text
modify cell
    ↓
walk away
    ↓
cell leaves residency
    ↓
modify another region
    ↓
return later
    ↓
original modification is still present
```

The registry represents **current state relative to base**, rather than an ever-growing history of changes.

If something is changed and subsequently returned to its original state, its delta can disappear.

---

# Experimental disk streaming

The normal Big World session keeps world objects resident in memory and parks them when inactive.

`disk_streaming` is an experimental mode that goes further:

```text
active cell
    ↓
commit changes
    ↓
remove objects from live scene
    ↓
free cell objects
```

When the cell is needed again:

```text
cell source
    ↓
instantiate pristine base objects
    ↓
apply saved UUID-keyed delta
    ↓
activate cell
```

This changes the memory model from:

```text
entire world resident
```

to:

```text
loaded cells resident
```

The cell's base state is captured the first time it is loaded, because the complete world is not necessarily resident at once.

Changes are committed before a cell is freed.

---

## Cell sources

Two source implementations are provided.

### `MemoryCellSource`

Creates a pristine in-memory representation of the map.

This is useful for testing the complete free/reload lifecycle without requiring a separate asset pipeline.

### `DirectoryCellSource`

Reads cell data from files named:

```text
cell_<cx>_<cz>.json
```

This provides the foundation for a genuinely disk-backed large-world workflow.

---

## Current disk-streaming status

The disk-streaming architecture and persistence logic are implemented and tested.

The remaining limitation is **live engine integration**: removing and recreating objects from Fio's live `things`/`brushes` collections requires the renderer and physics caches to be invalidated correctly while play mode is running.

Therefore:

```text
disk_streaming = false
```

is the default.

The experimental path fails back to the normal in-RAM Big World session if startup encounters an error.

---

# Editor vs. Play mode

Big World is primarily a runtime scalability layer.

The editor continues to expose the complete authored world.

You can:

- select distant objects
- move them
- duplicate them
- delete them
- edit properties
- inspect UUIDs
- work on the whole map

Big World does not permanently hide distant content from the editor.

Runtime parking occurs during Play mode and is reversed when Play mode stops.

This preserves Fio's central editor/runtime model: the map remains the same world rather than being converted into a separate compiled representation.

---

# Configuration

The `BigWorldSettings` entity provides:

| Property | Default | Description |
|---|---:|---|
| `enabled` | `true` | Enable Big World for this map. |
| `activation_radius` | `2048` | Radius within which cells become resident/active. |
| `deactivation_radius` | `2304` | Radius beyond which active cells may be removed. Provides hysteresis. |
| `sim_near_radius` | `1024` | Radius used for NEAR/full simulation fidelity. |
| `show_cell_debug` | `true` | Display the Big World debug panel/minimap. |
| `terrain_fill` | `false` | Expand procedural terrain to cover the streamed world. |
| `terrain_infinite` | `false` | Continue generating terrain beyond authored bounds. Requires terrain fill. |
| `terrain_stream_radius` | `0` | Terrain residency radius. `0` derives it from activation radius. |
| `disk_streaming` | `false` | Experimental mode that frees unloaded cell objects and restores them from a cell source. |

The properties are registered through Fio's normal typed plugin property API, so they appear as normal editor properties rather than requiring custom UI.

---

# Performance model

Big World is designed around one rule:

> **Runtime work should scale with the active region, not the total world population.**

For example, a world might contain:

```text
500,000 brushes
```

while the player is surrounded by only:

```text
~500 active brushes
```

The renderer, physics queries, entity processing, and lighting therefore do not need to treat all 500,000 objects as active gameplay objects.

The stationary path is especially cheap:

```text
player remains in same cell
        ↓
no residency change
        ↓
early-out
```

Crossing a cell boundary performs the bounded residency update.

The implementation includes tests specifically intended to prevent accidental reintroduction of a global per-frame scan.

---

# Performance measurements

The original large-world benchmark demonstrated approximately constant active population and flat streaming-update costs as world size increased:

```text
brushes     active brushes
10,000           ~501
50,000           ~501
100,000          ~501
250,000          ~501
500,000          ~501
```

The benchmark also showed stationary update cost remaining around a few microseconds and cell-crossing work around the millisecond range on the reference machine.

These numbers are **architecture measurements, not hardware guarantees**. They depend on Python version, hardware, map distribution, object complexity, and the exact benchmark configuration.

The important result is the scaling behaviour:

```text
World size increases
        │
        ├── total indexed objects increases
        │
        └── nearby active population remains bounded
```

Big World therefore makes very large maps practical without requiring the entire world to participate in every frame.

---

# Plugin isolation

Big World is deliberately optional.

The plugin declares itself:

```python
enabled = False
```

and uses the presence of a `BigWorldSettings` entity as the map opt-in.

More importantly, the plugin's heavy runtime modules are **lazy-imported**.

An ordinary Fio map therefore does not import:

- the Big World manager
- the Big World runtime
- the Big World persistence implementation
- the disk-streaming implementation

just because the plugin exists in the installation.

The core engine also does not contain a distributed collection of:

```python
if bigworld_enabled:
    ...
```

checks.

The boundary is:

```text
Fio core
   │
   └── Plugin API
          │
          └── Big World
```

rather than:

```text
Fio core
   │
   └── Big World
          │
          └── everything else
```

This is important for Fio's smaller maps: a normal map should not become a Big World map merely because the plugin is installed.

---

# Plugin API integration

Big World is implemented through Fio's plugin API.

It uses:

### `register`

Registers:

- `BigWorldSettings`
- the Big World property schema

### `on_play_start`

Checks whether the map opts in before importing the runtime implementation.

If it does not, the method returns without starting Big World.

### `on_tick`

Advances the active session.

The session itself performs the cheap same-cell early-out.

### `on_play_stop`

Stops the session and restores the world.

### `connect`

Hooks:

```text
render.overlay
```

for the optional debug display and exposes the active session through the:

```text
bigworld
```

plugin service.

Other plugins and tools can therefore obtain the active session through the normal plugin service mechanism rather than importing Big World internals.

Current plugin API requirement:

```text
api_version = "1.2.0"
```

---

# Testing

Big World has dedicated headless tests covering the major invariants of the system.

Important cases include:

- cell coordinate calculation
- object indexing
- spanning brushes
- active-set calculation
- circular activation
- hysteresis
- same-cell early-out
- bounded work when crossing cells
- runtime parking/restoration
- persistent entities
- simulation tier classification
- camera-independent residency
- save/load
- persistent cell deltas
- re-streaming of modified cells
- terrain streaming
- disk-streaming lifecycle
- base-world identity validation

One particularly important invariant is that **camera movement must not alter the resident cell set or simulation tiers**.

Another is that stationary ticks must not perform tier/residency work.

These tests exist to protect the scaling model, not merely the individual functions.

---

# Generating large test worlds

Synthetic worlds can be generated and benchmarked with:

```bash
python -m plugins.bigworld.tools.generate_world benchmark
```

To generate a large map:

```bash
python -m plugins.bigworld.tools.generate_world generate \
    --brushes 100000 \
    --out maps/bigworld_100k.json
```

Generated maps contain a Big World Settings entity and therefore opt themselves into the plugin.

---

# Files

| File | Purpose |
|---|---|
| `cell.py` | Cell representation, cell states, 512-unit coordinate system. |
| `config.py` | The one field table a map's Big World settings are described by: defaults, editor property schema and runtime coercion all derive from it. |
| `manager.py` | UUID index, cell assignment, residency calculation, hysteresis and active-set management. |
| `runtime.py` | Runtime integration, parking/restoration, simulation-tier handling and persistent session state. |
| `streaming.py` | Experimental disk streaming, cell sources, freeing/recreating cell objects. |
| `entities.py` | `BigWorldSettings` entity. |
| `persistence.py` | Configuration, save hygiene, UUID identity, cell delta registry and streaming normalisation. |
| `plugin.py` | Fio plugin lifecycle, service registration and debug overlay. |
| `tools/generate_world.py` | Large-world generator and benchmark harness. |
| `tests/` | Big World test suite. |

---

# Design principles

Big World deliberately avoids turning Fio into a conventional large-world engine with an entirely separate runtime architecture.

It does **not** introduce:

- an ECS
- a second physics system
- a second entity system
- a second spatial database
- a BSP/PVS compile
- a mandatory asset-baking stage
- a renderer rewrite
- a new world format

Instead it asks Fio's existing systems to operate on a **smaller live subset of the world**.

That is the fundamental scalability mechanism.

The world can be enormous because the engine does not have to pretend the entire world is nearby.

---

# Roadmap

The main remaining large-world work is making disk streaming a fully integrated production path.

In particular:

1. Complete safe live removal/recreation of scene objects.
2. Proper renderer-cache invalidation when cells are freed.
3. Proper physics-cache invalidation/rebuild when cells are freed.
4. Asynchronous cell I/O.
5. Avoid whole-world startup indexing when using genuinely disk-backed worlds.
6. Profile memory and I/O behaviour at substantially larger world sizes.
7. Continue validating the system on Fio's low-power CPU targets.

The important architectural pieces are already separated: cell residency, simulation tiers, persistence, cell sources, runtime integration, and the plugin boundary.

---

## Summary

Big World gives Fio a way to represent worlds much larger than the region that is currently being played.

```text
                 HUGE PERSISTENT WORLD
                         │
              ┌──────────┴──────────┐
              │    512-unit cells   │
              └──────────┬──────────┘
                         │
                   Player position
                         │
              ┌──────────┴──────────┐
              │  residency radius  │
              └──────────┬──────────┘
                         │
             ┌───────────┴───────────┐
             │                       │
          LIVE REGION            Distant world
             │                       │
     render / physics /       stored / parked /
       simulation               persistent
```

The world remains one Fio world.

Big World simply makes the engine **care about the part of it that is currently relevant**.
