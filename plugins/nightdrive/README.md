# Night Drive — a top-down driving game made in Fio

A playable **arcade driving / courier prototype** inspired by the user's
GTA 2-style overhead-city reference. It is a game **inside Fio**, not a
Pygame renderer or a replacement for Fio's existing first-person controller.

![Status: playable prototype](https://img.shields.io/badge/status-playable%20prototype-informational)

## Play

1. Start this checkout with **CPython 3.14** and Fio's normal dependencies:
   `python main.py`.
2. In Fio, choose **Plugins → nightdrive → Load Night Drive city**.
3. Press **Play**. The game switches the native Fio camera to **Overhead**
   (fixed-north, 560 world units high) and uses an in-world 3D Prop as the car.
4. Drive through the neon industrial city and reach the delivery beacons,
   in numbered order. Collecting each delivery gives **$500**. Hitting a
   building or another car costs **$75**.

| Input | Action |
| --- | --- |
| W / Up | Accelerate |
| S / Down | Brake and reverse |
| A / Left | Steer left |
| D / Right | Steer right |
| Space | Handbrake / fast turns |
| R | Recover vehicle at starting point |
| Esc | Fio's normal pause menu |

The Fio HUD displays speed, cash and distance to the next delivery.
`1 world unit = 0.1 m` is the game's speed/distance convention.

## What is included

- **Editable map**: [NightDrive_City.json](NightDrive_City.json) contains
  eight broad road corridors, 16 intersections, painted lanes and crossings,
  36 solid city buildings, rooftop detail, coloured lighting and neon.
- **Native Fio models**: [assets/car.obj](assets/car.obj) and
  [assets/beacon.obj](assets/beacon.obj). The player and all 12 moving traffic
  cars are ordinary `Prop` entities with `render_mode=model`; material colour
  comes from Fio's existing model pipeline.
- **Arcade handling**: [vehicle.py](vehicle.py) contains deterministic
  fixed-timestep acceleration, reversing, speed-dependent steering,
  handbrake/friction and stepped collision detection.
- **Traffic and missions**: [plugin.py](plugin.py) updates lightweight
  four-way road routes, detects collisions with authored `nightdrive_solid`
  brush footprints and cycles five deliveries. It writes **only** transforms
  of moving Props, using Fio's regular change journal.
- **Tests**: [tests/test_vehicle.py](tests/test_vehicle.py) can run without
  the editor, Qt, GPU or even NumPy:
  `python -m unittest discover -s plugins/nightdrive/tests -v`.

## Why this is implemented as a plugin

Fio is a generic game toolkit. The engine remains unchanged. The game is
auto-enabled **only** on levels with a `Prop` whose
`drive_role == "player_vehicle"`. The existing 60 Hz logic thread calls
the game plugin once per frame. For that map alone, the plugin wraps
`LogicPlayer.update_primary` to replace pedestrian movement with driving.
Outside it, the original Quake-style movement code runs as normal.

The camera is **Fio's own top-down camera**. Walls, ground, lights, vehicle
models, visibility/culling and GPU draw calls all remain in the native OpenGL
3.3 core renderer. The new occlusion governor on the parent branch is
preserved; this plugin adds **no occlusion probes, draw pass or renderer**.

A car is updated using its `Thing.pos` tracked assignment, plus
`touch(car, STATE)` when its rotation changes. The plugin does not scan or
rebuild the whole renderer per tick. Traffic is limited to 12 cars for the
Snapdragon 8cx Gen 3 / Adreno baseline; streetlights do not cast shadow maps.

## Edit and extend

The level is an ordinary editable Fio JSON map. Change the roads, buildings,
streetlights or vehicle positions in the editor and save.

A core building brush must keep its `nightdrive_solid: true` property to
block the player car; rooftops, floor markings and props are intentionally
not collision barriers. Traffic is described by plain `Prop` properties:

- `drive_role: traffic`, `drive_route: [[x,z], ...]`,
  `drive_next_waypoint` and `drive_speed`.
- `drive_role: delivery` and `drive_order` on stationary beacons.
- `drive_role: player_vehicle` on the unique player vehicle.

All models and the level belong to this game plugin; **no RPG-specific or
vehicle-specific behaviour is introduced into core Fio**.

## Prototype limitations

This is an arcade courier sandbox, not a full GTA clone: traffic follows
pre-authored loops without police, damage deformation, or pedestrian AI.
Headlamps are geometric car details, not extra moving point lights.
Collision uses ground-plane circles and building footprints rather than a
full rigid-body vehicle simulation. The plugin tracks score in memory for
the current Play session; persistent high scores are not yet implemented.

Open via the **Plugins** menu rather than manually opening the JSON file:
the menu gives the game access to the Fio viewport so it can hide the
engine's built-in *pedestrian* overhead sprite while drawing the player car.
Stopping Play restores the previous camera and sprite state.
