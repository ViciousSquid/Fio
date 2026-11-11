> Applies to Fio 3.5.0.1111.

<img width="45" height="45" alt="LogicRelay" title="LogicRelay" src="https://github.com/user-attachments/assets/5e794714-b5f9-4a05-a3a8-0af16e819a10" />
<img width="45" height="45" alt="LogicGate" title="LogicGate" src="https://github.com/user-attachments/assets/8ceb4c08-1524-4d53-95cc-5245ce3270df" />
<img width="45" height="45" alt="LogicTimer" title="LogicTimer" src="https://github.com/user-attachments/assets/49fd7ffe-a11a-4edd-bc8e-d58bb1aa591d" />
<img width="45" height="45" alt="LogicCommand" title="LogicCommand" src="https://github.com/user-attachments/assets/4e08659a-720d-40af-a127-483dec2171dc" />
<img width="45" height="45" alt="LogicCamera" title="LogicCamera" src="https://github.com/user-attachments/assets/c1462757-f244-4df0-a6ab-fe2e1abf2b9b" />
<img width="45" height="45" alt="Camera" title="Spawner" src="https://github.com/user-attachments/assets/ba2fec59-e3cb-421e-9632-9fa09e568323" />
<img width="45" height="45" alt="LogicState" title="LogicState" src="https://github.com/user-attachments/assets/fec15fb9-afa8-4b7b-8bd2-b3bf3e7e723f" />

**Logic entities** are Things with no presence in the game world. They exist only as nodes in the [I/O system](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System): they receive inputs, hold a little state, and fire outputs. None of them is evaluated per frame except the timer's countdown; everything else happens because a connection delivered an input.

| Entity | Class (`editor/things.py`) | `type` in the map | Purpose | Reference |
| :--- | :--- | :--- | :--- | :--- |
| LogicRelay | `LogicRelay` | `logic_relay` | Pass an event on, gated by enabled / fire-once | this page |
| LogicGate | `LogicGate` | `logic_gate` | Combine several signals with AND / OR / XOR / NAND / NOR | this page |
| LogicTimer | `LogicTimer` | `logic_timer` | Fire after an interval, once or repeatedly | [LogicTimer](https://github.com/ViciousSquid/Fio/wiki/LogicTimer) |
| LogicCommand | `LogicCommand` | `logic_command` | Run a console command | [LogicCommand](https://github.com/ViciousSquid/Fio/wiki/LogicCommand) |
| LogicState | `LogicState` | `logic_state` | Typed, persistent key/value state | [LogicState](https://github.com/ViciousSquid/Fio/wiki/LogicState) |
| LogicCamera | `LogicCamera` | `logic_camera` | Cinematic camera along PathNodes, or a JSON cutscene | [Camera](https://github.com/ViciousSquid/Fio/wiki/Camera), [Cutscenes](https://github.com/ViciousSquid/Fio/wiki/Cutscenes) |
| LogicSpawner | `LogicSpawner` | `logic_spawner` | Create entities at runtime | [Spawner](https://github.com/ViciousSquid/Fio/wiki/Spawner) |

## Placing and wiring

Right-click an empty spot in a 2D view (`editor/view_2d.py`):

* **Logic Entities** submenu — *LogicRelay*, *LogicTimer*, *LogicGate*, *LogicCommand*, *State Store* (a LogicState).
* **Nodes** submenu — *_Camera* (a LogicCamera) and *PathNode*.
* **Spawner** is in the top level of the menu.

Wire them from the Property editor's **I/O** tab, the [Logic Graph Editor](https://github.com/ViciousSquid/Fio/wiki/Logic-Graph-Editor) (`Ctrl+L`), the Logic Wizard (`Ctrl+Shift+W`) or `Ctrl+K` linking. A connection has an output, a target (name and UUID), an input, a parameter, a delay and a fire-once flag — see [Triggers & The I/O Logic System](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System).

All Things share these keys: `name`, `id` (UUID), `type`, `io_enabled` (default `true`; when `false` the entity sends and receives nothing). Logic entities also honour the generic inputs `Hide`, `Show`, `ToggleVisibility`, `Kill`, `SetTint` and `ClearTint`, which only set flags on them.

Handlers for every input below are in `editor/io_handlers.py: register_all_input_handlers` and run on the logic thread.

------------------------------------------------

## <img width="45" height="45" alt="LogicRelay" title="LogicRelay" src="https://github.com/user-attachments/assets/5e794714-b5f9-4a05-a3a8-0af16e819a10" /> LogicRelay

Fires `OnTrigger` whenever its `Trigger` input is called, unless it is disabled or has already fired as a one-shot. Use it to fan one event out to many targets, to switch a whole chain on and off from one place, or to add a named hop that `CancelPending` can cancel.

| Property | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `disabled` | bool | `false` | A disabled relay drops `Trigger` |
| `fire_once` | bool | `false` | The first `Trigger` passes; later ones are dropped until `Reset` |

| Input | Parameter | Effect |
| :--- | :--- | :--- |
| `Trigger` | any (optional) | If enabled and not latched, fires `OnTrigger`, forwarding the parameter as the output's value |
| `Enable` | — | `disabled = false` |
| `Disable` | — | `disabled = true` |
| `Toggle` | — | Flips `disabled` |
| `Reset` | — | Clears the fire-once latch |
| `CancelPending` | — | Removes every queued delayed event whose source name equals this relay's name |

| Output | Payload | Fired when |
| :--- | :--- | :--- |
| `OnTrigger` | the `Trigger` parameter, if any | `Trigger` passes |

Notes:

* The relay is transparent to parameters: a value arriving on `Trigger` is passed through to connections on `OnTrigger` whose own parameter is blank.
* The fire-once latch is the property `_relay_fired`. It is separate from a *connection's* `fire_once` flag, and unlike that flag it is **not** cleared when Play starts: a one-shot relay that has fired stays latched until it receives `Reset` (and the key is written to the map if the level is saved in that state).
* `CancelPending` matches by source **name**, so it also cancels events queued by another entity that shares the relay's name. It has no effect when it is itself delivered through a delayed connection (see *Known limitations* on the I/O page).

```json
{"type": "logic_relay", "pos": [0, 0, 0],
 "properties": {"name": "Relay_1", "id": "…", "type": "logic_relay",
                "io_enabled": true, "disabled": false, "fire_once": false},
 "io_connections": []}
```

------------------------------------------------

## <img width="45" height="45" alt="LogicGate" title="LogicGate" src="https://github.com/user-attachments/assets/8ceb4c08-1524-4d53-95cc-5245ce3270df" /> LogicGate

Holds a **set of asserted input signals** and, every time a signal changes, tests that set against its logic type and fires `OnTrigger` (condition met) or `OnFalse` (not met).

| Property | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `logic_type` | string | `AND` | `AND`, `OR`, `XOR`, `NAND` or `NOR` (case-insensitive) |
| `initial_state` | string | `off` | Stored and shown in the editor; not read by the runtime |
| `disabled` | bool | absent (`false`) | Set by `Enable` / `Disable` / `Toggle` |

The 2D icon follows the type (`assets/sprites/logic_<type>.png`).

**Signals.** Each signal has a name: the input's parameter, or — when the parameter is blank — the UUID of the entity that fired the connection (`IOManager.current_source_id()`), or `anonymous` when there is none (for example `ent_fire` from the console). Two sources wired without parameters are therefore two distinct signals.

**Expected count.** `expected = max(1, N)` where `N` is the number of connections in the scene that call this gate's `Trigger` input (addressed by UUID or by name; counted from the cached reverse index, `io_system.count_incoming_connections`). Connections to `ClearInput`, `ToggleInput`, `Reset` and the rest are not counted.

| `logic_type` | `OnTrigger` when (`active` = number of asserted signals) |
| :--- | :--- |
| `AND` | `active >= expected` |
| `OR` | `active > 0` |
| `XOR` | `active == 1` |
| `NAND` | `active < expected` |
| `NOR` | `active == 0` |

Any other value is treated as `OR` and logs an error. The editor's *Logic Type* combo also lists `NOT` and `XNOR`; neither is implemented, so both behave as `OR`.

| Input | Parameter | Effect |
| :--- | :--- | :--- |
| `Trigger` | signal name (optional) | Asserts the signal (idempotent), then evaluates |
| `ClearInput` | signal name (optional) | De-asserts the signal, then evaluates |
| `ToggleInput` | signal name (optional) | Flips the signal, then evaluates |
| `Evaluate` | — | Re-tests without changing any signal |
| `Reset` | — | De-asserts every signal. Does not evaluate and fires nothing |
| `Enable` / `Disable` / `Toggle` | — | Clear / set / flip `disabled` |

| Output | Fired when |
| :--- | :--- |
| `OnTrigger` | An evaluation finds the condition met |
| `OnFalse` | An evaluation finds it not met |

Notes:

* Evaluation happens on **every** `Trigger`, `ClearInput`, `ToggleInput` and `Evaluate`, so an `OR` gate fires `OnTrigger` each time any source triggers it, and an `AND` gate fires `OnFalse` for each signal that arrives before the last one. Put a fire-once connection or a fire-once LogicRelay after the gate when the result must act once.
* A disabled gate ignores `Trigger`, `ClearInput`, `ToggleInput` and `Evaluate` (signals are not recorded). `Reset` works regardless.
* If two `Trigger` connections send the **same** explicit signal name, they assert one signal but count as two expected inputs, so an `AND` gate cannot close.
* Signal sets are kept in `IOManager.gate_inputs`, keyed by the gate's UUID. They are session state: cleared when Play starts and stops, and not written to maps or save games.
* Worked example: the Logic Wizard's *Puzzle Door (Multi-Switch)* and *Arena Battle* scenarios wire triggers or monster deaths into an `AND` gate that opens a door.

```json
{"type": "logic_gate", "pos": [0, 0, 0],
 "properties": {"name": "Gate_1", "id": "…", "type": "logic_gate",
                "io_enabled": true, "logic_type": "AND", "initial_state": "off"},
 "io_connections": [
   {"output": "OnTrigger", "target": "BossDoor", "input": "Open",
    "parameter": "", "delay": 0.0, "fire_once": true, "target_id": "…"}]}
```

------------------------------------------------

## <img width="45" height="45" alt="LogicTimer" title="LogicTimer" src="https://github.com/user-attachments/assets/49fd7ffe-a11a-4edd-bc8e-d58bb1aa591d" /> LogicTimer

Counts down `interval` seconds and fires `OnTimer`, repeating unless `one_shot` is set. Full reference: **[LogicTimer](https://github.com/ViciousSquid/Fio/wiki/LogicTimer)**.

| | |
| :--- | :--- |
| Properties | `interval` (1.0), `timer_enabled` (false), `start_on` (false), `one_shot` (false) |
| Inputs | `Enable`, `Start`, `Disable`, `Stop`, `Toggle`, `FireTimer`, `SetTime`, `ResetTimer` |
| Outputs | `OnTimer`, `OnFinished` |

------------------------------------------------

## <img width="45" height="45" alt="LogicCommand" title="LogicCommand" src="https://github.com/user-attachments/assets/4e08659a-720d-40af-a127-483dec2171dc" /> LogicCommand

Queues a [console command](https://github.com/ViciousSquid/Fio/wiki/Console-commands) for the UI thread. Full reference: **[LogicCommand](https://github.com/ViciousSquid/Fio/wiki/LogicCommand)**.

| | |
| :--- | :--- |
| Properties | `command` (`""`), `disabled` (false) |
| Inputs | `RunCommand`, `Trigger`, `SetCommand`, `Enable`, `Disable` |
| Outputs | `OnCommand` |

------------------------------------------------

## <img width="45" height="45" alt="LogicState" title="LogicState" src="https://github.com/user-attachments/assets/fec15fb9-afa8-4b7b-8bd2-b3bf3e7e723f" /> LogicState

Typed key/value state shared by `store_name` across levels, with arithmetic, comparison and change events. Full reference: **[LogicState](https://github.com/ViciousSquid/Fio/wiki/LogicState)**.

| | |
| :--- | :--- |
| Properties | `store_name` (the entity name), `initial_data` (`{}`), optional `capacity` (25) |
| Inputs | `SetValue`, `GetValue`, `ClearKey`, `ClearAll`, `CopyFrom`, `CopyValue`, `Increment`, `Decrement`, `Add`, `Subtract`, `Multiply`, `Divide`, `Min`, `Max`, `Clamp`, `Toggle`, `Compare`, `Exists`, `Missing`, `TestValue`, `SetObjectValue`, `GetObjectValue`, `ClearObjectState` |
| Outputs | `OnValueSet`, `OnValueChanged`, `OnValueRead`, `OnValueCleared`, `OnKeyCleared`, `OnStoreFull`, `OnKeyNotFound`, `OnTrue`, `OnFalse`, `OnCompareTrue`, `OnCompareFalse` |

------------------------------------------------

## <img width="45" height="45" alt="LogicCamera" title="LogicCamera" src="https://github.com/user-attachments/assets/c1462757-f244-4df0-a6ab-fe2e1abf2b9b" /> LogicCamera

Takes the view away from the player and either plays an authored JSON cutscene (`cutscene_file`, which takes precedence) or glides along a chain of [PathNodes](https://github.com/ViciousSquid/Fio/wiki/PathNodes) starting at `path_target`. Player input is consumed while it is active. Full reference: **[Camera](https://github.com/ViciousSquid/Fio/wiki/Camera)** and **[Cutscenes](https://github.com/ViciousSquid/Fio/wiki/Cutscenes)**; `maps/_SHOWCASE.json` contains one.

| Property | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `cutscene_file` | string | `""` | Cutscene JSON to play; when set, PathNode playback is not used |
| `path_target` | string | `""` | Name of the first PathNode of the chain |
| `speed` | float | `200.0` | World units per second along the chain |
| `fov_override` | float | `0.0` | Field of view during the sequence; `0` keeps the player's |
| `look_ahead` | bool | `true` | Look at the next node (`true`) or along the path direction (`false`) |
| `lookat_return_time` | float | `5.0` | Seconds a `LookAt` focus is held before the path facing resumes; `0` holds it indefinitely |

| Input | Parameter | Effect |
| :--- | :--- | :--- |
| `Start` | — | Begins the sequence (replacing any JSON cutscene already playing). Aborts with a log line if `path_target` is not a PathNode |
| `Stop` | — | Ends the sequence and returns the view to the player; `OnFinished` is not fired |
| `Pause` / `Resume` | — | Freeze / continue the active sequence |
| `SetSpeed` | float, minimum 1 | Overrides the travel speed of the active sequence |
| `LookAt` | entity name or UUID | Faces that entity; ignored unless this camera is the active one |

| Output | Fired when |
| :--- | :--- |
| `OnStart` | The sequence begins |
| `OnReachNode` | The camera arrives at a PathNode (the node itself fires `OnCameraArrived`) |
| `OnFinished` | The sequence reaches its end |

------------------------------------------------

## <img width="45" height="45" alt="Camera" title="Spawner" src="https://github.com/user-attachments/assets/ba2fec59-e3cb-421e-9632-9fa09e568323" /> LogicSpawner

Creates a new entity each time `Spawn` is called. Full reference: **[Spawner](https://github.com/ViciousSquid/Fio/wiki/Spawner)**; demonstrated in `maps/Spawner_Test.json`.

| Property | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `spawn_type` | string | `Monster` | A key of `editor.things.ENTITY_TYPES` (matched case-insensitively as a fallback) |
| `target_node` | string | `""` | PathNode whose position is used; if blank or not found the spawner's own position is used |
| `max_spawn` | int | `0` | Total spawns allowed; `0` is unlimited |
| `spawn_properties` | object | `{}` | Properties given to each spawned entity. For `Monster`, `"random": true` picks a random monster type and variant |
| `disabled` | bool | absent (`false`) | Set by `Enable` / `Disable`; a disabled spawner ignores `Spawn` |

| Input | Parameter | Effect |
| :--- | :--- | :--- |
| `Spawn` | — | Spawns one entity, appends it to the world and rebuilds the entity caches |
| `Enable` / `Disable` | — | Clear / set `disabled` |
| `SetTargetNode` | PathNode name | Sets `target_node` (blank ignored) |

| Output | Fired when |
| :--- | :--- |
| `OnSpawn` | An entity was spawned |
| `OnMaxReached` | `Spawn` was called with the `max_spawn` limit already reached |

The running count is the property `_spawn_count`. It is not reset when Play starts and is saved with the map.

------------------------------------------------

## Developer notes

* **Declarations** are in `editor/io_system.py: register_default_io`; **handlers** in `editor/io_handlers.py`. `io_system.audit_io_coverage` reconciles the two and `tests/io/test_io_contract.py` asserts on it.
* Enabled state for relays, gates, spawners and commands is the shared `disabled` flag, written through `engine.spatial.set_authored_flag` so it survives Big World parking.
* Session-only state: gate signals (`IOManager.gate_inputs`), timer countdowns (`LogicTiming.timer_states`), the camera sequence (`cutscene_runtime.state`).
* Tests: `tests/logic/test_logic_primitives.py` (relay, gate, timer), `tests/logic/test_logic_composition.py`, `tests/logic/test_logic_camera.py`, `tests/logic/test_cutscene_io.py`, `tests/logic/test_logic_state.py`, `tests/persistence/test_logic_round_trip.py`.

## Related

* [Triggers & The I/O Logic System](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System) — connections, dispatch order, parameters and delays
* [Logic Graph Editor](https://github.com/ViciousSquid/Fio/wiki/Logic-Graph-Editor) — the visual wiring editor
* [PathNodes](https://github.com/ViciousSquid/Fio/wiki/PathNodes) — waypoints used by LogicCamera and LogicSpawner
* [Console commands](https://github.com/ViciousSquid/Fio/wiki/Console-commands) — what LogicCommand can run
