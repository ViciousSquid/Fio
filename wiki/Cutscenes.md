> Applies to Fio 3.1.0.0611 (CANARY).

# Cutscenes

A cutscene is a timed camera sequence played in Play mode by a **LogicCamera** entity (`type: logic_camera`). While one is active the logic thread takes the view away from the player, discards player input and hides the HUD. There are two kinds:

- **JSON cutscene** — a file under `cutscenes/` with camera keyframes, optional actor tracks and timed events (HUD messages, I/O, monster fights). Authored with the Cutscene Wizard. Used when the LogicCamera's `cutscene_file` is set.
- **PathNode camera** (legacy) — the camera travels along a chain of [PathNodes](https://github.com/ViciousSquid/Fio/wiki/PathNodes) at a fixed speed. Used when `cutscene_file` is empty.

| Concern | Owner |
|---|---|
| Playback state machine, track sampling, actors, timed events, restore | `engine/cutscene_runtime.py`: `CutsceneRuntime` (one per `LogicThread`, as `logic.cutscene_runtime`) |
| Start / Stop / Pause / Resume / SetSpeed / LookAt inputs | `editor/io_handlers.py` (`camera_start`, `camera_stop`, …) |
| I/O declaration | `editor/io_system.py`: `register_io('logic_camera', …)` |
| Entity class and defaults | `editor/things.py`: `LogicCamera` |
| Tick integration, input suppression | `engine/logic_thread.py`: `LogicThread._tick_play_mode` |
| View matrix, FOV, HUD fade | `engine/logic_render.py`: `prepare_render_state` |
| Authoring UI | `editor/cutscene_wizard.py`: `CutsceneWizard` |
| Packaging | `editor/package_exporter.py` |

## The LogicCamera entity

### Properties

| Key | Type | Default | Meaning |
|---|---|---|---|
| `cutscene_file` | string | `''` | Path of a JSON cutscene relative to the project root, e.g. `cutscenes/showcase.json`. When set it takes precedence over `path_target` |
| `path_target` | string | `''` | Name of the first PathNode of the chain (legacy mode) |
| `speed` | float | 200.0 | World units per second along the chain (legacy mode) |
| `fov_override` | float | 0.0 | FOV in degrees during a legacy sequence; 0 uses 90 |
| `look_ahead` | bool | True | Legacy mode: face the node after the one being approached; when False, face the node being approached |
| `lookat_return_time` | float | 5.0 | Seconds an explicit `LookAt` holds before the path target resumes; 0 holds indefinitely |
| `cutscene_restore_actors` | bool | True | Fallback for `settings.restore_actors` when the JSON file does not set it |
| `cutscene_io_events` | list | — | Timed I/O events for the **legacy** mode (same schema as JSON `io` events). Written by the wizard's *Apply* |
| `cutscene_trigger_mode`, `cutscene_trigger_radius`, `cutscene_once`, `cutscene_stop_on_escape` | — | — | Written by the wizard's *Apply*. No runtime code reads them |

The property editor shows these in the *Cinematic Camera* group (*Cutscene File*, *Path Target*, *Speed*, *FOV Override*, look-ahead, *LookAt Return*); the *Cutscene File* combo lists `cutscenes/*.json`.

### Inputs

| Input | Parameter | Effect |
|---|---|---|
| `Start` | — | Ends any active JSON cutscene (without `OnFinished`), then starts this camera: the JSON cutscene if `cutscene_file` is set, otherwise the PathNode chain. Fires `OnStart` and any event at time 0 |
| `Stop` | — | Ends whatever cutscene is active and returns the view to the player. Does not fire `OnFinished` |
| `Pause` | — | Freezes the active cutscene (`state['paused'] = True`) |
| `Resume` | — | Continues a paused cutscene |
| `SetSpeed` | float | Legacy mode: sets travel speed (minimum 1.0). No effect on a JSON cutscene |
| `LookAt` | entity name or UUID | Legacy mode: smoothly face that entity for `lookat_return_time` seconds. Ignored (with a log line) unless this camera is the active one. A JSON cutscene does not read it; use keyframe `look_at` instead |

`Stop`, `Pause` and `Resume` act on the active cutscene regardless of which LogicCamera receives them.

### Outputs

| Output | Fired |
|---|---|
| `OnStart` | When the sequence begins |
| `OnReachNode` | Legacy mode: each time the camera arrives at a PathNode (the node itself fires `OnCameraArrived` first) |
| `OnFinished` | When the sequence reaches its natural end: the JSON duration elapses, the last PathNode is reached, or a PathNode in the chain cannot be found |

## Triggering a cutscene

A cutscene starts only when its LogicCamera receives the `Start` input:

- **I/O connection** from any entity output, e.g. a trigger's `OnTrigger` or a PlayerStart's `OnPlayerSpawn` (see [Triggers & The I/O Logic System](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System)).
- **Console**: `ent_fire <camera name> Start` (alias `fire`), or `send <camera name> Start`. The same commands deliver `Stop`, `Pause`, `Resume`.
- **Another cutscene's** timed `io` event.

The wizard's *Trigger* choices ("Player enters trigger radius", "Start when Play Mode begins", "I/O only"), *Radius* and *Play once per game session* are stored in the file and on the LogicCamera but are not implemented by the runtime: every cutscene behaves as "I/O only". To start on spawn, connect `PlayerStart.OnPlayerSpawn → <camera>.Start`; to start on proximity, use a trigger brush.

## What happens while a cutscene plays

`LogicThread._tick_play_mode` (logic thread) runs, in order: movers, doors, parented lights and portals, delayed I/O events, LogicTimers, light fades, the first-person/overhead camera transition, then `CutsceneRuntime._update_cinematic_camera(delta)`. If a cutscene is still active after that, the tick consumes and discards the mouse delta, the use key, the fire request and weapon-slot keys, and **returns**. Skipped for the duration:

- player movement and physics (both players in split-screen), water sounds;
- use-key interaction, props, the physics world step, trigger handling;
- the plugin tick, portal transit, weapon selection and shooting, bullet-mark and monster-projectile updates.

Other effects:

- **View**: `prepare_render_state` builds the view matrix from `cam_pos`, `cam_angle`, `cam_pitch` and uses `fov` (90 when unset). The player entity itself is not moved.
- **HUD**: not drawn while a cutscene is active (`render_state.cinematic_camera_active`); when it ends, `hud_alpha` ramps from 0 to 1 over 4 seconds. The overhead player sprite is hidden too.
- **Pause menu**: Esc opens the pause menu as usual. While the world is paused the tick returns before the cutscene update, so the cutscene is frozen with it.
- **No skip**: there is no key that skips or stops a cutscene. The *Escape stops the cutscene* checkbox (`settings.stop_on_escape`, `cutscene_stop_on_escape`) is stored but never read. A cutscene ends by reaching its duration, by a `Stop` input, by another `Start`, or when Play stops.
- **Leaving Play** finishes an active JSON cutscene without `OnFinished` (actors restored, spawned actors removed) and clears the state. Entering Play clears it as well.
- **Save games**: `engine/savegame.py` has no cutscene handling; playback position is not part of a save.

## JSON cutscene file

Files live in `<project>/cutscenes/`. `CutsceneRuntime._cutscene_file_path` resolves `cutscene_file` against the played package's root first (`logic.package_root`), then the project root, and accepts it only if the real path is inside that root's `cutscenes/` directory. A file is rejected (logged under the `Cutscene` category as `FAILED: …`) when it is missing, outside `cutscenes/`, not valid JSON, not an object, or has no `camera` list. A successful load logs `LOADED: '<file>'`.

### Top level

| Key | Type | Read by runtime | Meaning |
|---|---|---|---|
| `version` | int | no | The wizard writes 2 |
| `id` | string | no | A fresh UUID on every save |
| `name` | string | no | Display name (wizard) |
| `actors` | list | yes | Actor declarations |
| `camera` | list | yes (required) | Camera keyframes |
| `actor_tracks` | object | yes | `{actor id: [keyframe, …]}` |
| `events` | list | yes | Timed events |
| `map.file` | string | no | Map file name the cutscene was authored in; the wizard asks before loading it into a different map |
| `settings.restore_actors` | bool | yes | Put map actors back where they were when the cutscene ends. Default: the LogicCamera's `cutscene_restore_actors`, else true |
| `settings.stop_on_escape`, `settings.trigger_mode`, `settings.trigger_radius`, `settings.trigger_once` | — | no | Wizard state only |

Numbers that are missing, non-numeric or non-finite read as their default (0, or 90 for `fov`); a `pos` that is not three finite numbers reads as `[0, 0, 0]`. Rows that are not objects are ignored.

### `camera` keyframes

| Key | Type | Default | Meaning |
|---|---|---|---|
| `time` | float, seconds | 0 | |
| `pos` | `[x, y, z]` | `[0, 0, 0]` | Camera position |
| `yaw`, `pitch` | float, **degrees** | 0 | Editor-camera angles, converted to radians once at start. The view direction is `(cos yaw · cos pitch, sin pitch, sin yaw · cos pitch)` |
| `fov` | float, degrees | 90 | Clamped to 1..179 |
| `teleport` | bool | false | Cut to this keyframe at its time instead of moving to it |
| `look_at` | `{"actor": "<entity id>"}` | — | Aim at that entity (a cutscene actor, or any map entity by id) while this keyframe is the earlier one of the current segment |

### `actors`

| Key | Type | Meaning |
|---|---|---|
| `id` | string | Entity id. Required; rows without one are skipped |
| `name` | string | Display name (wizard) |
| `spawn` | bool | The actor is temporary and is created from `definition` if no entity with this id exists in the map |
| `definition` | object | `{"type": "<entity type>", "pos": [x, y, z], "yaw": float, "properties": {…}}`. `type` defaults to `monster` and is looked up in `ENTITY_TYPES` (case-insensitively as a fallback) |

At start, for each row the runtime looks the id up in the map; if it is absent and the row has `spawn` and a `definition`, it instantiates the entity (properties get the `id` and `_cutscene_runtime = True`), appends it to `editor_state.things` and rebuilds the entity caches. An actor that is neither present nor spawnable is logged and its track ignored. Every actor gets `disabled = True` and `_cutscene_runtime = True` for the duration; its position, yaw and previous `disabled` value are snapshotted.

### `actor_tracks`

`{ "<actor id>": [ {"time": float, "pos": [x, y, z], "yaw": float, "teleport": bool}, … ] }`

The wizard also writes `action` (`move` / `attack`), `target_id` and `duration` on waypoint rows; the runtime does not read them (an attack waypoint takes effect through the `fight` event the wizard adds alongside it).

Before its first keyframe an actor stays where it was. Each tick the sampled position is written to `actor.pos` and the physics world is told (`physics_world.sync_entity_position(actor, wake=True)`). Yaw is written to the entity's `angle` attribute if it has one, otherwise to `properties['yaw']`.

### `events`

Every event has `time` (seconds) and `type`. Events are sorted by time and each fires once, on the first tick whose elapsed time is ≥ its time.

| `type` | Fields | Effect |
|---|---|---|
| `message` | `line`: `message`, `message2` or `message3` (default `message`); `text` | Shows `text` (trimmed, first 50 characters) on that HUD message line. Queued on the logic thread and shown by the 3D view (`consume_cinematic_messages`) |
| `io` (direct input) | `target_id` and/or `target_name`, `input`, optional `parameter` | Executes the input on the target directly (`io_manager._execute_input(target_name, input, parameter, "Cutscene", target_id=…)`). If only `target_id` is given the entity's name is looked up |
| `io` (legacy output) | `source_id` and/or `source_name`, `output`, optional `parameter` | Used only when the event has none of `target_id`, `target_name`, `input`. Fires that output on the source entity, so the map's own connections run |
| `fight` | `duration`, `attackers`: [ids], `defenders`: [ids], `style` | For `time ≤ t < time + duration`, every attacker that is a Monster gets `disabled = False`, `awake = True` and `_aggro_target` set to the first defender (its `target_name` is removed), handing it to the monster AI. The previous values are restored when the window ends or the cutscene finishes. `style` is not read |

An `io` event may stop or replace the cutscene; the runtime checks after each one and abandons the rest of the tick if the state changed. Events of any other type are ignored but still count toward the duration.

### Timing and interpolation

- **Duration** is the largest `time` over camera keyframes, actor keyframes and events (for a `fight`, `time + duration`). A cutscene with camera keyframes and zero duration is kept alive for one tick.
- `elapsed` advances by the logic tick's `delta`. When `elapsed ≥ duration` the cutscene finishes: fights are restored, map actors get their `disabled` value back (and their position and yaw if `restore_actors`), spawned actors are removed, the state is cleared and `OnFinished` fires.
- `CutsceneRuntime._cutscene_sample(rows, elapsed)` picks the two keyframes bracketing `elapsed` and interpolates **linearly**: `pos`, `pitch` and `fov` component-wise, `yaw` along the shortest arc. Before the first keyframe the first one is used; after the last, the last. For equal timestamps the last equal-time keyframe is the left one and the first equal-time keyframe the right one.
- A keyframe with `teleport: true` holds the previous keyframe unchanged until its own time, then cuts.
- `look_at`: when the segment's left keyframe has `look_at` and the target exists and is more than 0.01 units away, the runtime replaces the sampled angles with `cam_angle = atan2(dx, dz)` and `cam_pitch = asin(dy / distance)`, where `d` is target − camera.

`cam_angle` computed by `look_at` (and by the legacy PathNode camera, which uses the same `atan2(dx, dz)`) is fed to the same view-direction formula as keyframe yaw, `(cos, ·, sin)`. The two conventions differ by a swap of X and Z, so the resulting view direction is the target direction mirrored across the X = Z diagonal; it points at the target only when `dx == dz`.

### Worked example: `cutscenes/showcase.json`

The shipped showcase cutscene is a 20-second fly-through with no actors:

```json
{
  "version": 2,
  "id": "789de7b8-31ea-4e99-9481-763438020b5a",
  "name": "showcase",
  "actors": [],
  "camera": [
    {"time": 0.0, "pos": [-2089.09, 188.13, -1054.93], "yaw": -692.25, "pitch": 6.45, "fov": 90.0},
    {"time": 2.0, "pos": [-2063.57, 188.13, -592.43], "yaw": -744.6, "pitch": 7.95, "fov": 90.0},
    "… 12 more keyframes, the last at time 20.0 …"
  ],
  "actor_tracks": {},
  "events": [
    {"time": 3.0, "type": "message", "line": "message", "text": "ViciousSquid presents..."},
    {"time": 5.0, "type": "message", "line": "message3", "text": "Fio"},
    {"time": 8.0, "type": "message", "line": "message3", "text": "Fio"}
  ],
  "map": {"file": "_SHOWCASE.json"},
  "settings": {
    "restore_actors": true,
    "stop_on_escape": true,
    "trigger_mode": "manual",
    "trigger_radius": 180.0,
    "trigger_once": true
  }
}
```

In `maps/_SHOWCASE.json` a LogicCamera named `showcase` has `cutscene_file: "cutscenes/showcase.json"`, and `PlayerStart_1` carries the connection `OnPlayerSpawn → showcase.Start`. Entering Play therefore starts the cutscene: 14 camera keyframes are interpolated over 20 seconds, three HUD messages appear at 3, 5 and 8 seconds, and at 20 seconds the view returns to the player and `OnFinished` fires. Yaw values outside ±360° are valid; interpolation takes the shortest arc between consecutive keyframes.

## PathNode camera (legacy mode)

With `cutscene_file` empty, `Start` looks up `path_target`; if no PathNode has that name the start is aborted with a log line. Otherwise the camera starts at that node's position and, each tick, moves toward the current node at `speed` units per second. It faces the next node (`look_ahead`) or the current one, with exponential smoothing (`1 − e^(−8·delta)`) so the bearing does not jump at node boundaries. On arrival the node fires `OnCameraArrived`, the camera fires `OnReachNode`, and the sequence continues with the node's next node or finishes with `OnFinished`. `cutscene_io_events` on the entity fire at their times, in either `io` schema.

## The Cutscene Wizard

*Tools → Cutscenes...* (`Ctrl+Shift+C`) or the *Cutscenes* toolbar button opens `CutsceneWizard`, a modeless tool window; the editor stays fully usable. If the loaded map has a LogicCamera whose `cutscene_file` exists, the first such cutscene is loaded when the wizard opens.

| Tab | Controls |
|---|---|
| **Setup** | *Name*, *File* (saved as `cutscenes/<file>.json`; any directory part is stripped and `.json` appended), *Trigger*, *Radius*, *Play once per game session*, *Restore existing actors after cutscene*, *Escape stops the cutscene*. Only *Restore existing actors* affects playback |
| **Actors & Waypoints** | *+ Actor* creates a temporary `human` Monster 256 units in front of the editor camera, marked `_cutscene_temporary`. *+ Selected* registers the selected map monsters as actors. *Remove* deletes the actor and every reference to it (tracks, `look_at` keyframes, fights). *Focus* frames it. Waypoints: choose *At time*, *Action* (MOVE or ATTACK), optional *Target*, then *Capture* (or *Position*, which does the same) to record the selected actor's current position. An ATTACK waypoint needs a target and an *Attack for* duration and also adds a `fight` event. The time field then advances by 1 s (or by the attack duration) |
| **Keyframes** | Move the editor camera, set *At time*, optionally *Look at* an actor and *Teleport*, then *Capture camera position* (records the editor camera's position, yaw, pitch and FOV). *Selected time* / *Set* re-times a keyframe; *Remove* deletes it |
| **Events** | *Message* (time, line, text) and *I/O Input* (time, target entity, input, optional parameter) sub-tabs; the event list (double-click to edit, *Remove selected event*); *Advanced: exact actor keyframes* captures all selected monsters at an exact time |

**Preview** (*Rewind*, *Play*/*Pause*, *Fast Forward*) moves the real actors and the editor camera on a 33 ms timer at 1× or ±2× and restores them when it stops or reaches either end. It does not fire events. The preview eases each segment with a smoothstep and interpolates yaw directly, whereas playback is linear with shortest-arc yaw, so motion between keyframes is not identical.

**Footer**

- *Load* — open a cutscene JSON (must contain a `camera` list). Temporary actors are recreated from their definitions.
- *Save* — validates, asks before overwriting, writes the file, removes temporary actors from the map and closes.
- *Apply* — validates, writes the file, and creates or updates the LogicCamera whose `cutscene_file` matches (as one undo step), placing it at the first camera keyframe and selecting it. Temporary actors are removed from the map.
- *Cancel* / closing — prompts to Save / Discard / Cancel when there are unsaved changes, then removes temporary actors.

Validation requires at least one camera keyframe and at least one actor or event.

Temporary actors are ordinary editor entities while authoring, so they can be selected, moved and edited normally. They are never written to the map: `EditorState.get_level_data` filters out anything with `_cutscene_temporary`. Their definitions are embedded in the cutscene file (`actors[].definition`, without `_io_connections` or `_cutscene_*` properties) and re-created at playback.

## Packaging

*Export Game Package* collects every `cutscene_file` referenced by the exported maps, resolves it the way the runtime does (inside the project's `cutscenes/`), adds it to the package, and also bundles the assets named by the entities those cutscenes spawn. A reference outside `cutscenes/` or to a missing file is reported.

## Debugging

- The debug console logs under category `Cutscene` (`LOADED: …` in green, `FAILED: …` in red) and has a *Cutscenes* filter checkbox to hide them. Actor and I/O problems are logged under `IO`.
- `logic.cutscene_runtime.state` is `None` when idle; otherwise a dict with `active`, `paused`, `entity`, `elapsed`, `cam_pos`, `cam_angle`, `cam_pitch`, `fov`, and for JSON cutscenes `json_cutscene`, `cutscene_file`, `duration`, `camera_keys`, `actor_tracks`, `actors`, `actor_initial`, `spawned_actors`, `restore_actors`, the event lists and their cursors.

## Tests

| File | Covers |
|---|---|
| `tests/logic/test_cutscene_io.py` | Timed I/O events fire once at their times, an event can stop the camera, teleport and plain interpolation |
| `tests/logic/test_cutscene_sample_equivalence.py` | `_cutscene_sample` against the frozen stable-sort sampler in `tests/helpers/cutscene_reference.py` (equal timestamps, malformed rows, random tracks, live track edits) |
| `tests/logic/test_io_dispatch.py` | LogicCamera inputs and outputs, JSON actors, `look_at`, `OnFinished` |
| `tests/editor/test_cutscene_preview_restore.py` | Wizard preview restore, actor removal clean-up, discovery of the map's cutscene |
| `tests/editor/test_level_replacement_guards.py` | A packaged cutscene resolves from the package root |
| `tests/engine/test_world_pause.py` | A cutscene, like death and level-complete, returns before the plugin tick |
| `tests/persistence/test_package_export.py`, `test_shipped_map_references.py` | Cutscenes in exported packages; shipped maps reference existing cutscenes |
| `tests/performance/test_cutscene_sample_benchmark.py` | Opt-in sampling benchmark |

`tests/helpers/cutscene_reference.py` is a frozen copy of the earlier sampler and must stay independent of the production code.
