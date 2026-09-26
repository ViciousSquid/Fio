<img src="https://github.com/user-attachments/assets/946ba690-52e7-4f7c-bbd2-f92a7442bca8" width="375">

<img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="MIT License">  <img src="https://img.shields.io/badge/status-Active%20Development-orange?style=for-the-badge" alt="Status">

### Unified World Editor, Real-Time Engine & Game Creation Toolkit

**Fio is a real-time world machine for creating, editing and running first-person, top-down and large-scale worlds.**

Build a world, press **Play**, and the world you just edited becomes the running simulation.

There is no import, compile or bake stage between authoring and execution. **The editor and runtime operate on the same world state.**

Designed on low-power ARM hardware with an efficiency-first philosophy.

### What makes Fio different


* **Edit and play in the same runtime** — no compile, bake or scene-import pipeline.
* **The world is executable** — entities, state, spatial relationships and gameplay logic are part of the live world.
* **Classic brush/CSG editing** with arbitrary convex polyhedra.
* **Entity I/O gameplay** inspired by Source, providing a composable alternative to traditional scripting.
* **LogicState** — persistent, typed state attached directly to world objects.
* **Large-world simulation** with spatial residency and distance-aware simulation
* **Dense numerical processing** using NumPy for bulk spatial, transformation and simulation operations.
* **Native world portals** — non-Euclidean connections between arbitrary locations without BSP/VIS preprocessing.
* **Procedural world generation** and experimental map-generation tools.
* **Continuous camera system** with first-person and top-down modes.
* **Physics, movers, triggers, timers, logic gates and pathfinding** as native world systems.
* **Fully extensible plugin architecture** with example plugins included.
* **Portable world packaging** through `.fiopak`.
* **Local split-screen multiplayer.**
* **Designed to bring back the immediacy of classic Radiant/Worldcraft workflows.**

### [💾 Download Binaries for Windows/macOS/Linux](https://github.com/ViciousSquid/Fio/releases)

### Documentation: [Wiki](https://github.com/ViciousSquid/Fio/wiki/) | [Changelog](https://github.com/ViciousSquid/Fio/wiki/changelog)

#### Or [run from source](https://github.com/ViciousSquid/Fio#-quickstart) or use the included Dockerfile.

<img src="https://github.com/user-attachments/assets/a68a33ac-1da7-4626-8796-46a6435cf95c" width="800">

---

##  The World Machine

a modern data-oriented engine wearing the skin of a 1998 GtkRadiant workflow, with Source-style entity I/O instead of a scripting language — deliberately recreating the immediacy of classic Radiant/Worldcraft, but where pressing Play just... runs the world you're editing.

A Fio world contains:

* geometry
* entities
* spatial relationships
* physical state
* gameplay state
* logic connections
* simulation state
* rendering state

The editor operates directly on that world.

When you press **Play**, Fio does not export the map into another representation and hand it to a separate game runtime. **The world continues running.**

```text
                    FIO WORLD
                       │
       ┌───────────────┼────────────────┐
       │               │                │
     EDIT           SIMULATE          RENDER
       │               │                │
       └───────────────┼────────────────┘
                       │
                  SAME WORLD
```

This is the core idea behind Fio:

> **The map is the program. The editor is the engine.**

---

## Why Fio exists

Fio explores what happens when the immediacy of classic Radiant/Worldcraft-style editing is combined with a modern real-time simulation architecture.

The goal is to reduce the distance between **authoring a world and experiencing it**.

Fio is designed for rapid experimentation with:

* first-person worlds
* top-down worlds
* large/open worlds
* procedural environments
* experimental gameplay systems
* non-Euclidean spaces
* reactive environments
* simulation-heavy maps

There is no requirement to build a conventional gameplay codebase before a world can become interactive.

Gameplay can be assembled from **entities, inputs, outputs, state and spatial relationships**.

---

## Logic & Gameplay

Fio uses an entity-based gameplay model built around **I/O and LogicState**.

Objects can send inputs to other objects, react to events, maintain persistent typed state and participate in larger gameplay systems.

### Native systems include

* LogicState
* Entity I/O
* Logic gates
* Relays
* Timers
* Triggers
* Movers and doors
* Physics
* Props
* Pathfinding
* Player starts
* Procedural terrain
* World portals
* BigWorld simulation-distance management

LogicState provides object-local typed state including strings, integers, floats, booleans, null values and UUIDs.

Rather than requiring a central quest or scripting runtime, gameplay can emerge from **small composable world behaviours**.

---

## Large Worlds

Fio is designed to make large worlds practical on relatively constrained hardware.

The spatial system maintains world locality through a grid-based spatial index. Systems can use that information for:

* visibility
* distance queries
* simulation residency
* physics synchronisation
* entity lookup
* culling

BigWorld builds on this infrastructure to control how much of the world needs to remain actively simulated.

World entities can move between different simulation-distance states rather than requiring every object in a large world to receive identical processing every frame.

This makes large worlds a **simulation problem**, not simply a rendering problem.

---

## Numerical Core

Fio is deliberately designed around bulk numerical processing where the workload benefits from it.

Large collections of homogeneous world data can be represented as dense arrays and transformed together using NumPy rather than repeatedly traversing thousands of Python objects.

This architecture is used for areas such as:

* spatial calculations
* distance/culling operations
* transformations
* physics synchronisation
* entity indexing
* bulk world-state operations

The goal is simple:

> **Don't make the CPU repeatedly perform work that can be expressed as a bulk data transformation.**

Scalar Python remains appropriate for small, irregular or highly stateful operations. Fio does not attempt to turn every part of the engine into an array.

---

##  Rendering

- Lean **OpenGL 3.3 Core** renderer designed with low-power hardware in mind.
- Operates directly on NumpY arrays

Features include:

* dynamic lighting
* dynamic shadows
* fog
* glass
* water
* frustum culling
* distance culling
* world portals
* GLB assets
* first-person and top-down camera modes

### Native world portals

Fio supports non-Euclidean world connections between arbitrary locations using portal rendering techniques including stencil-buffer masking and oblique near-plane clipping.

There is no requirement for BSP, VIS/PVS preprocessing or offline visibility compilation.

---

##  Under the Hood

### Shared runtime state

The editor and simulation operate on the same live world representation.

Pressing **Play** does not require Fio to compile or bake the level into another runtime representation.

### Data-oriented where it matters

Fio combines Python's high-level flexibility with C-accelerated numerical processing through NumPy.

Large homogeneous datasets are processed in bulk, while irregular gameplay and editor operations remain straightforward Python.

### Spatially aware simulation

The spatial system provides a common foundation for visibility, culling, entity lookup and large-world simulation.

BigWorld uses this infrastructure to determine which parts of a world need full, reduced or dormant simulation.

### Extensible architecture

Fio is open-source and modular, with a plugin API for extending the editor and runtime.

The goal is not to hide the engine behind layers of abstraction, but to make experimentation with the world machine straightforward.

---

## 📦 Portable Worlds

Fio can package worlds and their associated resources into `.fiopak` archives.

The eventual goal is simple:

> **A complete Fio world should be something you can hand to another Fio runtime and run.**

The same engine architecture is also being developed toward a dedicated player for portable platforms.

---

## 🚀 Quickstart

Python 3.10+ is required.

```bash
git clone https://github.com/ViciousSquid/Fio.git
cd Fio
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
python main.py
```

---

## 🤝 Contributing

Contributions, feedback and experiments are welcome.

Fio is an active research-and-development project as much as it is a game creation toolkit. If you're interested in world editing, procedural generation, simulation, rendering, spatial systems or unusual gameplay architectures, check the issues and discussions.

---

<img src="https://github.com/user-attachments/assets/c6c6b036-2425-4508-a2fe-05816429303f" width="800"><br>

<img src="https://github.com/ViciousSquid/Fio/blob/2.2.0.2408/assets/__portal.gif" width="600">

[<img src="https://img.youtube.com/vi/ANNXNGgn_wo/hqdefault.jpg" width="700" height="550"
/>](https://www.youtube.com/embed/ANNXNGgn_wo)

<img src="https://github.com/user-attachments/assets/22283623-21a2-4776-a2ae-71649f5276f0" width="700">
