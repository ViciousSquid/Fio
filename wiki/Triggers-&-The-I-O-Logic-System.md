> Applies to Fio 3.5.0.1111.

Fio's map logic is an **Input/Output (I/O) event system**: an entity fires a named **output**, the dispatcher follows the **connections** stored on that entity, and each connection calls a named **input** on a target entity, optionally after a delay and with a parameter. Brushes (triggers, doors, movers, plain brushes), [Things](https://github.com/ViciousSquid/Fio/wiki/Entities-(Things)) and [Logic Entities](https://github.com/ViciousSquid/Fio/wiki/Logic-Entities) all take part. There is no scripting language; the connections in the map file are the program.

| | |
| :--- | :--- |
| Declarations (what the editor offers) | `editor/io_system.py`: `IO_REGISTRY`, `register_default_io()` |
| Dispatcher | `editor/io_system.py`: `IOManager` |
| Input implementations | `editor/io_handlers.py`: `register_all_input_handlers()` |
| Trigger volumes | `engine/logic_triggers.py`: `LogicTriggers` |
| Timers and light fades | `engine/logic_timing.py`: `LogicTiming` |
| Use-key interactions (doors, LevelChangers) | `engine/logic_interaction.py`: `LogicInteraction` |
| Entity lookup caches | `engine/logic_world.py`: `LogicWorld` |
| Design notes shipped with the code | `editor/LOGIC.md` |

## Authoring connections

All four methods edit the same `OutputConnection` records.

1. **Property editor, I/O tab.** Select the source entity and open its **I/O** tab. *Add*, *Edit*, *Remove* and *Copy* (duplicate) manage rows; double-click edits; a right-click menu offers the same actions. The dialog (`editor/io_editor_widget.py: IOConnectionDialog`) has *My Output*, *Target Entity* (name or UUID, with completion and a list button), *Target Input*, *Parameter*, *Delay* (0–999 s, step 0.1) and *Only fire once per play session*. Both combo boxes are editable, so an undeclared name can be typed. Things also have an **I/O Enabled** checkbox on this tab (`io_enabled`). Brushes get the tab only when they are a trigger, door or mover. Below the table the tab lists the inputs the selected entity accepts.
2. **[Logic Graph Editor](https://github.com/ViciousSquid/Fio/wiki/Logic-Graph-Editor)** (`Ctrl+L`, Tools → Logic Graph Editor…): drag from an output pin to an input pin, then press **Apply**.
3. **Logic Wizard** (`Ctrl+Shift+W`, Tools → Logic Wizard…): pick a scenario, assign scene entities to its roles, set per-connection delay and fire-once, and the wizard adds the connections (`editor/logic_wizard.py`). Scenarios:
   * **Monster Encounters** — Locked Room, Alarm System, Chain Reaction, Death Trap, Kill the Lights, Timed Patrol, Ambush Swarm
   * **Doors & Movers** — Button Door, Timed Door, Locked Door Hint, Prop Unlocks Path, Elevator, Light Switch, Respawning Prop
   * **Environment & Audio** — Flickering Light, Ambient Soundscape, Trap Corridor, Scripted Reveal, Timed Hazard
   * **Complex Logic** — Arena Battle, Security System, Puzzle Door (Multi-Switch), Gauntlet Sequence, Level Exit Sequence, Relay Chain, Disable on Trigger

   The wizard adds its connections as wires to a Logic Graph scene (`LogicGraphScene.add_connection_by_name`) and they reach the map when **Apply** is pressed in the Logic Graph window; a connection the graph cannot represent (an unnamed entity, a pin the type does not declare) is written straight onto the source entity instead. Run the wizard from the Logic Graph window's **Wizard…** button, or from the Tools menu after the Logic Graph has been opened for this map: started from the Tools menu before that, the wires go to a temporary scene that is never applied, and the Logic Graph window that opens afterwards is rebuilt from the unchanged entities (see *Known limitations*).
4. **Entity linking** (`Ctrl+K`, `editor/entity_linking.py`): select exactly one source entity, press `Ctrl+K`, drag to a destination in any 2D or 3D view and release (`Esc` or right-click cancels). Not available in Play. Both entities must be unlocked. The link created depends on the pair:

   | Source → destination | What is written |
   | :--- | :--- |
   | PathNode → PathNode | `next_node` = destination name |
   | Monster → PathNode | `patrol_target` = name, `patrol` = `true` |
   | LogicCamera → PathNode | `path_target` = name |
   | LogicSpawner → PathNode | `target_node` = name |
   | Trigger with `trigger_action: "teleport"` → PathNode | `target_node` = name |
   | Portal → Portal | `portal_target` = name (and the pair's active state is synchronised) |
   | DeathmatchNode → DeathmatchNode | destination name appended to `links` |
   | anything else | one I/O connection |

   For an I/O connection the output is the first match among `OnTrigger`, `OnTimer`, `OnPressed`, `OnUse`, `OnDamaged`, `OnDeath`, else the source type's first declared output. The input is chosen from the target's **parameter-free** inputs: `Open` for doors and movers, `Wake` for monsters, `TurnOn` for lights, `Trigger` for relays, `Enable` for timers, then `Trigger`, `Toggle`, `Enable`, `TurnOn`, `Open`, `Wake`, `Start`, `Show`, else the first one. The connection is stored with the target's UUID. An unnamed destination is given a name (`<type>_<n>`); name-based links require the destination's name to be unique. The operation is one undo step and is refused with a toast if no link is possible or it already exists.

**Tools → Validate All Connections…** (`MainWindow.validate_io_connections` → `io_system.validate_all_scene_connections`) reports every connection with a missing target (`missing_target`), an input the target type does not accept (`unknown_input`) or an output the source type does not declare (`unknown_output`), plus PathNode `next_node` links that are missing or do not point at a PathNode. Types with no registry entry are not judged.

Copying and pasting an entity drops its connections (`MainWindow` paste removes `_io_connections`); `Thing.duplicate()` keeps them.

## Connection data model

`editor/io_system.py: OutputConnection` (a dataclass). Connections live on the **source** entity.

| In memory | In the map | Type | Default | Meaning |
| :--- | :--- | :--- | :--- | :--- |
| `output_name` | `output` | string | — | Output on the source that triggers this connection |
| `target_name` | `target` | string | — | Target entity name (display, and fallback lookup) |
| `target_id` | `target_id` | string | `""` | Target entity UUID. Written only when non-empty |
| `input_name` | `input` | string | — | Input to call on the target |
| `parameter` | `parameter` | string | `""` | Value passed to the input |
| `delay` | `delay` | float, seconds | `0.0` | Wait before delivery |
| `fire_once` | `fire_once` | bool | `false` | Deliver at most once per play session |
| `_fired` | not saved | bool | `false` | Set when the connection fires; cleared at Play start |

Storage:

* **Things**: memory `thing.properties['_io_connections']`; map: `io_connections` on the thing record, beside `type`, `pos` and `properties` (always written, even when empty).
* **Brushes**: memory `brush['_io_connections']`; map: `io_connections` on the brush record, written only when the brush has connections.

```json
{
  "type": "monster",
  "pos": [-1487.8, 102.25, 819.0],
  "properties": {"type": "monster", "name": "Monster_1", "id": "a5fb6340-6ea6-47c2-9277-fb243fb6debd"},
  "io_connections": [
    {"output": "OnDeath", "target": "Portal_1", "input": "Enable",
     "parameter": "", "delay": 1.0, "fire_once": false,
     "target_id": "2255ec35-6b25-41ef-ba13-cda9f5e355b7"}
  ]
}
```

(from `maps/_SHOWCASE.json`; a name-only connection without `target_id` is in `maps/MonsterTest.json`.)

Any mutation outside `add_connection` / `remove_connection` / `set_connections` must call `io_system.bump_io_revision()`: the reverse index behind the Property editor's **Targeted by** line and `count_incoming_connections` is cached against that counter.

Every entity also carries `io_enabled` (bool, default `true`). When `false` the entity neither fires outputs nor receives inputs.

## Dispatch semantics

`IOManager.fire_output(source_entity, output_name, value=None, activator_entity=None)`:

1. Returns immediately if the source has `io_enabled: false`.
2. If called from a thread other than the one running the play tick while Play is active, the call is appended to `_foreign_outputs` and delivered, in order, at the start of the next `update()`. The monster AI thread's outputs (`OnDeath`, `OnSeePlayer`, `OnAttack`, patrol events) arrive this way.
3. With `IO_DEBUG_ENABLED` (default `True`), firing an output the source's registered type does not declare logs an `Error` line; it is still dispatched.
4. The source's connections are walked **in stored order**. Output names match case-insensitively.
5. A connection with `fire_once` whose `_fired` is already set is skipped (logged `SKIPPED (fire_once)`). Otherwise `_fired` is set — at this moment, not when a delayed event is later delivered.
6. **Parameter**: the connection's own `parameter` if it is non-empty; otherwise the `value` the output was fired with, if any (pass-through). An authored parameter always wins.
7. `delay > 0`: a `PendingEvent` with `fire_time = current_time + delay` is appended to `pending_events`. Otherwise the input is executed **synchronously**, inside the `fire_output` call. Chains are therefore depth-first: everything a zero-delay connection causes finishes before the next connection of the same output is considered.

`IOManager._execute_input(target_name, input_name, parameter, source_name, target_id, …)`:

1. **Target resolution happens at delivery time**, never when the event was queued: by `target_id` through `LogicWorld.find_entity_by_id`, then by `target_name` through `find_entity_by_name`. In Play both read caches built by `LogicWorld.build_entity_caches()`; a name shared by several entities resolves to the last one indexed (things after brushes). Outside Play the scene is scanned (things first, newest first).
2. A target that cannot be found logs `I/O: Target '<name>' (id=<id>) not found!` under `Error` and the event is dropped. There are no special target names (no `!activator`, `!self`, wildcards or classname targeting): a target is one entity, by UUID or exact name.
3. A target with `io_enabled: false` drops the event silently.
4. The handler key is `(entity type, input name)`, both lower-cased. The entity type is `properties['type']` for a Thing; for a brush it is the first set flag of `is_trigger` → `trigger`, `is_door` → `door`, `is_mover` → `mover`, `is_water` → `water`, `is_fog` → `fog`, else `brush`.
5. With a registered handler, `handler(entity, parameter, logic_thread)` runs. An exception is caught, logged as `<type>.<input> handler failed: …` and its traceback printed; dispatch continues.
6. Without a handler the **generic inputs** apply (below). An input that is neither is ignored without a message.
7. Afterwards the target is journalled for the render projection (`engine.change_journal.touch`), and if a brush's authored `hidden` flag changed the collision set is marked dirty.

`IOManager.update(delta)` runs once per logic tick: it records the tick thread, drains `_foreign_outputs`, advances `current_time`, then delivers every pending event whose `fire_time` has been reached, in queue order. Delays are therefore quantised to the 60 Hz tick.

**Loops.** The dispatcher has no cycle detection and no depth limit. A zero-delay cycle recurses on the Python stack until `RecursionError`, which the handler wrapper catches and logs; a cycle that contains a delay re-queues itself indefinitely (which is how a repeating sequence can be built deliberately).

**Source and activator.** While a handler runs, `IOManager.current_source()` / `current_source_id()` give the entity whose output is being delivered, and `current_activator()` / `current_activator_id()` the entity that began the chain. The activator is the `activator_entity` passed to `fire_output` (trigger brushes pass the player, Prop or Monster that touched them; Props pass their collector), carried across every hop including delayed ones; with none given it is the first source of the chain. Outside a handler all four are empty. [LogicState](https://github.com/ViciousSquid/Fio/wiki/LogicState) object-local inputs and unnamed [LogicGate](https://github.com/ViciousSquid/Fio/wiki/Logic-Entities) signals use them.

### Generic inputs

`IOManager.GENERIC_INPUTS`, implemented by `_try_generic_input` for any entity whose type registers no handler for that input:

| Input | Parameter | Effect |
| :--- | :--- | :--- |
| `Enable` | — | `disabled = false` |
| `Disable` | — | `disabled = true` |
| `Kill` | — | `hidden = true` and `disabled = true`. Nothing is deleted from the scene |
| `Hide` | — | `hidden = true` |
| `Show` | — | `hidden = false` |
| `ToggleVisibility` | — | flips `hidden` |
| `SetTint` | `R G B` (0–255, space-separated, clamped) | `tint = [r, g, b]`; fewer than three numbers or a non-integer is ignored |
| `ClearTint` | — | removes `tint` |

`hidden` and `disabled` are written through `engine.spatial.set_authored_flag`, so an input delivered to an entity that Big World streaming has parked changes the authored value and survives the unpark.

### Lifetime and threading

* The `IOManager` is created in `LogicThread.__init__` (`engine/logic_thread.py`) and all handlers run on the **logic thread** (`LogicThread`, a daemon `threading.Thread` ticking at `TICK_RATE = 60`), under its tick lock.
* Tick order in `LogicThread._tick_play_mode`: movers and doors → parented lights and portals → `io_manager.update(delta)` → `update_logic_timers` → `update_light_fades` → camera transition and cinematic camera → player movement → `interaction_runtime.handle` (doors, LevelChangers) → `prop_runtime.tick` → physics step → `trigger_runtime._handle_triggers` → plugin tick.
* Delayed events, timers and light fades keep running during a cinematic, while the player is dead and while the level-complete screen is up (those return later in the tick). They stop while the world is paused (pause menu), because the paused tick returns before `io_manager.update`.
* Handlers must not touch Qt. Work for the UI thread is queued: `game_state.queue_sound(...)` and `game_state.queue_console_command(...)`.
* Console commands run on the UI thread; `ent_fire` takes the logic thread's tick lock before calling `_execute_input`.
* **Play start** (`LogicSession`): `io_manager.reset()` clears pending events, foreign outputs, LogicGate signals, the clock and the source/activator context; every connection's `_fired` is cleared; entity caches are built; then `play_on_start` Speakers receive `PlaySound`, the spawn PlayerStart fires `OnPlayerSpawn`, and `start_on` timers are armed.
* **Play stop**: `io_manager.reset()` again. By default the editor world keeps whatever Play changed (lights switched off, hidden brushes, dead monsters); *Settings → Play Modes → Restore the world when leaving Play* (`[Settings] restore_world_on_stop`) puts the pre-Play world back.
* **Save games** (`engine/savegame.py`): the pending queue is saved as `runtime.pending_io_events` (time remaining, target name/id, input, parameter, source and activator ids) and timer countdowns as `runtime.timer_states`. LogicGate signal sets and connection `_fired` flags are not part of a save. See [Save Games](https://github.com/ViciousSquid/Fio/wiki/Save-Games).

## Triggers

A trigger is a brush with `is_trigger: true` (Property editor → *Is Trigger*). `LogicTriggers` samples occupancy on a scheduler that wakes every `TRIGGER_POLL_TICK = 0.25` s and polls each trigger at its own interval; nothing scans triggers at 60 Hz except the use-prompt.

| Brush key | Default | Values | Meaning |
| :--- | :--- | :--- | :--- |
| `trigger_type` | `multiple` at runtime (the editor's combo starts at `Once`) | `Once`, `Multiple` (case-insensitive) | A `Once` trigger is spent after its first activation for the session |
| `trigger_activation` | `touch` | `touch`, `use` | Touch: an activator inside the brush AABB. Use: the player presses Use within `use_radius` while facing the centre |
| `trigger_action` | `target` | `target`, `teleport`, `hurt` | What an activation does (below) |
| `trigger_filters` | `["player"]` | any of `player`, `props`, `monsters` (list, or a comma/space-separated string) | Which entity classes can activate it |
| `trigger_poll_interval` | `1.0` | snapped to the nearest of `1.0`, `0.5`, `0.25` s | Occupancy sampling period |
| `use_radius` | `96.0` | world units | Radius of a use trigger's sphere around the brush `pos` |
| `use_label` | `""` → `Activate` | text | HUD prompt `[E] <label>` |
| `target_node` | `""` | PathNode name | Destination for `teleport` |
| `hurt_amount` (else `damage`) | `10` | integer ≥ 0 | Damage of a `hurt` trigger |
| `trigger_save` | `none` | `none`, `quicksave`, `quickload` | Queues that console command on activation |
| `disabled` | `false` | bool | A disabled trigger is not polled |

Behaviour:

* **Touch triggers** fire on the *change* of occupancy. Each entering activator calls `_on_trigger_enter`; each leaving one fires `OnEndTouch`. Because occupancy is sampled, entry is noticed up to one poll interval late. Disabled Props and Monsters are not counted.
* **Use triggers** never fire `OnStartTouch`/`OnEndTouch` from occupancy. A Use press activates one when the player is inside the sphere and the direction to its centre is within 60° of the view heading (`dot > 0.5`); the press is evaluated at the trigger's next poll. Only the player can use.
* On activation, by `trigger_action`:
  * `target` — fires `OnStartTouch`, then `OnTrigger`, with the activator as chain activator.
  * `teleport` — moves the activator to `target_node` (the player's velocity is zeroed), then fires `OnTeleport`. `OnTrigger` is **not** fired.
  * `hurt` — damages the player by `hurt_amount` on entry and again while the player stays inside: a countdown of `HURT_INTERVAL = 0.5` s is reduced by the trigger's poll interval at each poll and damage is applied when it reaches zero (so once per poll at the default 1.0 s interval, every 0.5 s at 0.5 s or 0.25 s). No output is fired; Props and Monsters are unaffected. Damage goes through armor first and is ignored in god mode.
* Then the `trigger_save` command is queued, the plugin event `trigger_enter` is emitted, and a `Once` trigger is recorded in `fired_once_triggers`.

## I/O reference by entity type

Input and output names are case-insensitive at runtime. "generic" marks an input served by the generic table above. Types with their own page carry the full contract there.

### `trigger` (brush)

| Input | Parameter | Effect |
| :--- | :--- | :--- |
| `Enable` / `Disable` / `Toggle` | — | Clear / set / flip `disabled` |
| `TouchTest` | — | Fires `OnTrigger` if the **player's** position is inside the brush AABB right now |
| `Teleport` | PathNode name; blank uses `target_node` | Teleports the **player** to that node, zeroes velocity, fires `OnTeleport` |
| `SetTargetNode` | PathNode name | Sets `target_node` (blank is ignored) |
| `Hide` / `Show` / `ToggleVisibility` | — | Set / clear / flip `hidden`; collision is rebuilt |
| `SetTint` | `R G B` | Sets `tint` |
| `ClearTint` | — | Removes `tint` |

| Output | Fired when |
| :--- | :--- |
| `OnStartTouch` | A `target` trigger is activated (before `OnTrigger`) |
| `OnTrigger` | A `target` trigger is activated, or `TouchTest` succeeds |
| `OnEndTouch` | A filtered activator leaves a touch trigger (any action) |
| `OnTeleport` | A `teleport` trigger moved an activator, or the `Teleport` input ran |

### `door` (brush)

| Input | Parameter | Effect |
| :--- | :--- | :--- |
| `Open` | — | From `closed`, `closing` or `stopped`: start opening, fire `OnOpen` |
| `Close` | — | From `open`, `opening` or `stopped`: start closing, fire `OnClose` |
| `Toggle` | — | `closed` → `Open`; `open` → `Close`; ignored while moving |
| `Stop` | — | A moving door holds where it is (`stopped`) |
| `Reverse` | — | Opening → `Close`; closing → `Open`; stopped → whichever end is further (`progress ≥ 0.5` closes); at rest → `Toggle` |
| `Lock` / `Unlock` | — | Set / clear `door_locked` |
| `SetSpeed` | float; blank = `128` | Sets `speed` |
| `Hide`, `Show`, `ToggleVisibility`, `SetTint`, `ClearTint` | as trigger | |
| `Enable`, `Disable`, `Kill` | — | generic |

| Output | Fired when |
| :--- | :--- |
| `OnOpen` | The door starts opening (input, Use key or auto-open) |
| `OnClose` | The door starts closing (input, or its open timer expiring) |
| `OnFullyOpen` | It reaches the open position |
| `OnFullyClosed` | It reaches the closed position |
| `OnLockedUse` | The player presses Use on a closed door with `door_locked` |

### `mover` (brush)

| Input | Parameter | Effect |
| :--- | :--- | :--- |
| `Open` | — | `start_on = true`, direction forward |
| `Close` | — | `start_on = true`, direction backward |
| `Toggle` | — | Flips `start_on` (runs / halts) |
| `Enable` / `Disable` | — | `start_on = true` / `false` |
| `Stop` | — | `start_on = false`; progress and direction are kept |
| `Reverse` | — | Flips the direction flag |
| `SetPosition` | float 0–1 (clamped) | Sets progress along the travel |
| `SetSpeed` | float ≥ 0; blank = `64` | Sets `speed` |
| `FollowPath` | PathNode name; blank uses `path_target` | Switches to PathNode-chain movement from that node |
| `StopPath` | — | Drops path following; the mover holds position |
| `SetPathTarget` | PathNode name | Sets `path_target` (blank ignored) |
| `Hide`, `Show`, `ToggleVisibility`, `SetTint`, `ClearTint` | as trigger | |
| `Kill` | — | generic |

| Output | Payload | Fired when |
| :--- | :--- | :--- |
| `OnFullyOpen` | — | Progress reaches the end position; also on arrival at each PathNode |
| `OnFullyClosed` | — | Progress returns to the start position; also when a PathNode chain ends |
| `OnPathNodeReached` | node name | The mover arrives at a PathNode |

### `brush` (plain brush)

| Input | Parameter | Effect |
| :--- | :--- | :--- |
| `Toggle` | — | Flips `disabled` |
| `Hide`, `Show`, `ToggleVisibility`, `SetTint`, `ClearTint` | as trigger | |
| `Enable`, `Disable`, `Kill` | — | generic |

No outputs. A brush with `is_fog` (set by choosing the *Fog* shader) or `is_water` resolves to the runtime type `fog` / `water`. Neither type is in `IO_REGISTRY` and neither registers handlers, so such a brush accepts the generic inputs only: `Toggle` does nothing on it even though the editor (whose `get_entity_type_for_io` classifies it as `brush`) offers it, and `Hide` / `Show` go through the generic path. The editor's *Water* shader sets `shader: "Water"` but not `is_water`, so an ordinary water brush is dispatched as `brush`.

### Things

| Type | Inputs | Outputs | Page |
| :--- | :--- | :--- | :--- |
| `light` | `TurnOn`, `TurnOff`, `Toggle`, `SetBrightness` (float 0–10), `SetColor` (`R G B`), `EnableShadows`, `DisableShadows`, `ToggleShadows`, `FadeIn` (seconds), `FadeOut` (seconds), `Hide`, `Show`, `ToggleVisibility` | `OnTurnedOn`, `OnTurnedOff`, `OnShadowsEnabled`, `OnShadowsDisabled` | [Light](https://github.com/ViciousSquid/Fio/wiki/Light) |
| `speaker` | `PlaySound`, `StopSound`, `Toggle`, `SetVolume` (float 0–1), `Hide`, `Show`, `ToggleVisibility` | `OnSoundStarted`, `OnSoundFinished` | [Speaker](https://github.com/ViciousSquid/Fio/wiki/Speaker) |
| `prop` | `Enable`, `Disable`, `Collect`, `Respawn`, `SetValue` (int), `Drop`, `Wake`, `Hide`, `Show`, `ToggleVisibility` | `OnCarried`, `OnDropped`, `OnRest`, `OnCollected`, `OnRespawn` | [Prop](https://github.com/ViciousSquid/Fio/wiki/Prop) |
| `playerstart` | — | `OnPlayerSpawn`, `OnPlayerDeath` | [Player Start](https://github.com/ViciousSquid/Fio/wiki/Player-Start) |
| `levelchanger` | `Trigger`, `ChangeLevel` (optional map name) | `OnUse` | [LevelChanger](https://github.com/ViciousSquid/Fio/wiki/LevelChanger) |
| `monster` | `Enable`, `Disable`, `Kill`, `SetTarget` (entity name; blank = player), `Wake`, `Sleep`, `SetHealth` (int), `Respawn` (optional int health), `Hide`, `Show`, `ToggleVisibility` | `OnDeath`, `OnDamaged`, `OnSeePlayer`, `OnLostPlayer`, `OnAttack`, `OnRespawn` | [Monster](https://github.com/ViciousSquid/Fio/wiki/Monster) |
| `path_node` | `Enable`, `Disable` (generic), `Toggle` | `OnMonsterArrived`, `OnMonsterLeft`, `OnWaitStart`, `OnWaitEnd`, `OnCameraArrived` | [PathNodes](https://github.com/ViciousSquid/Fio/wiki/PathNodes) |
| `portal` | `Enable`, `Disable`, `Toggle`, `SetColor` (`R G B`), `SetTarget` (portal name), `ShowRim`, `HideRim`, `SetWidth` (float ≥ 16), `SetHeight` (float ≥ 16) | `OnEnabled`, `OnDisabled`, `OnToggled`, `OnTeleport`, `OnPlayerEnter` | [Portal](https://github.com/ViciousSquid/Fio/wiki/Portal) |
| `effect` | `SetType` (`FIRE`/`ORB`/`EXPLOSION`/`CUSTOM`), `SetFireTexture` (1–5), `SetOrbTexture` (1–5), `SetCustomGif` (path), `SetLoop` (bool), `Explode`, `Hide`, `Show`, `ToggleVisibility` (generic) | `OnChanged` (payload: the new value) | — |
| `logic_relay` | `Trigger`, `Enable`, `Disable`, `Toggle`, `Reset`, `CancelPending` | `OnTrigger` | [Logic Entities](https://github.com/ViciousSquid/Fio/wiki/Logic-Entities) |
| `logic_gate` | `Trigger` (signal name), `ClearInput` (signal name), `ToggleInput` (signal name), `Reset`, `Evaluate`, `Enable`, `Disable`, `Toggle` | `OnTrigger`, `OnFalse` | [Logic Entities](https://github.com/ViciousSquid/Fio/wiki/Logic-Entities) |
| `logic_timer` | `Enable`, `Disable`, `Toggle`, `FireTimer`, `Start`, `Stop`, `SetTime` (seconds), `ResetTimer` | `OnTimer`, `OnFinished` | [LogicTimer](https://github.com/ViciousSquid/Fio/wiki/LogicTimer) |
| `logic_command` | `RunCommand` (command line), `Trigger` (command line), `SetCommand` (command line), `Enable`, `Disable` | `OnCommand` (payload: the command line) | [LogicCommand](https://github.com/ViciousSquid/Fio/wiki/LogicCommand) |
| `logic_camera` | `Start`, `Stop`, `Pause`, `Resume`, `SetSpeed` (float ≥ 1), `LookAt` (entity name or UUID) | `OnStart`, `OnReachNode`, `OnFinished` | [Camera](https://github.com/ViciousSquid/Fio/wiki/Camera) |
| `logic_spawner` | `Spawn`, `Enable`, `Disable`, `SetTargetNode` (PathNode name) | `OnSpawn`, `OnMaxReached` | [Spawner](https://github.com/ViciousSquid/Fio/wiki/Spawner) |
| `logic_state` | `SetValue`, `GetValue`, `ClearKey`, `ClearAll`, `CopyFrom`, `CopyValue`, `Increment`, `Decrement`, `Add`, `Subtract`, `Multiply`, `Divide`, `Min`, `Max`, `Clamp`, `Toggle`, `Compare`, `Exists`, `Missing`, `TestValue`, `SetObjectValue`, `GetObjectValue`, `ClearObjectState` | `OnValueSet`, `OnValueChanged`, `OnValueRead`, `OnValueCleared`, `OnKeyCleared`, `OnStoreFull`, `OnKeyNotFound`, `OnTrue`, `OnFalse`, `OnCompareTrue`, `OnCompareFalse` | [LogicState](https://github.com/ViciousSquid/Fio/wiki/LogicState) |

Every type additionally accepts the generic inputs it does not handle itself (for example `Kill` on a light, `SetTint` on a speaker).

Plugins register further types when loaded: the Deathmatch plugin (`deathmatchsettings`: inputs `Restart`, `EndMatch`, outputs `OnMatchStart`, `OnFrag`, `OnMatchEnd`; `deathmatchflag`: outputs `OnTaken`, `OnDrop`, `OnReturn`, `OnCapture`; `deathmatchbot`: outputs `OnDamaged`, `OnDeath`) and the Tidy plugin (`tidyreceptacle`: inputs `Reset`, `Enable`, `Disable`, outputs `OnObjectPlaced`, `OnFull`; `tidygoal`: inputs `Enable`, `Disable`, outputs `OnProgress`, `OnComplete`; and it extends `prop` with input `Reset` and output `OnTidied`). See [Deathmatch Plugin](https://github.com/ViciousSquid/Fio/wiki/Deathmatch-Plugin) and [Tidy Plugin](https://github.com/ViciousSquid/Fio/wiki/Tidy-Plugin).

## Extending the system

Declaration and implementation are two structures with different lifetimes: `IO_REGISTRY` is global and filled at import; the handler table belongs to one `IOManager`.

```python
from editor.io_system import IODef, register_io

register_io('my_type',
    inputs=[IODef('Activate', 'Do the thing'),
            IODef('SetLevel', 'Set level', 'float')],   # param_type: "", float, int, string, bool, color
    outputs=[IODef('OnActivated', 'Fired after Activate')])

def my_activate(entity, parameter, logic):          # runs on the logic thread
    entity.properties['active'] = True
    logic.io_manager.fire_output(entity, 'OnActivated', value='1')

io_manager.register_input_handler('my_type', 'activate', my_activate)
```

* `register_io` **replaces** a type's definitions. Plugins use `api.register_io(...)` for their own types and `api.extend_io(entity_type, inputs=, outputs=)` to add to a core type (see [Plugin API](https://github.com/ViciousSquid/Fio/wiki/Plugin-API)).
* Numeric parameters should be parsed with `io_handlers._finite`, which rejects `nan` and `inf`.
* `audit_io_coverage(io_manager)` returns `unimplemented_inputs` (declared, no handler, not generic), `undeclared_inputs` (handler without a declaration) and `unknown_types` (handler for an unregistered type). Entries in `ABSTRACT_IO` (empty in this release) are exempt.
* Non-handler code can read and write state with `IOManager.query_keyvalue(store_name, key, default)` and `set_keyvalue(store_name, key, value)`.

## Console commands

Run from the [Debug console](https://github.com/ViciousSquid/Fio/wiki/Debug-console); full list on [Console commands](https://github.com/ViciousSquid/Fio/wiki/Console-commands).

| Command | Effect |
| :--- | :--- |
| `ent_fire <entity> <input> [parameter]` (alias `fire`) | Finds the entity by name and calls `IOManager._execute_input(name, input, parameter, "console", target_id=<its UUID>)` under the logic thread's tick lock: the registered handler or the generic fallback runs exactly as for a connection. Works in the editor and in Play. Only if no `IOManager` is available does it fall back to the entity's own `on_input()` (LevelChanger implements one) |
| `send <entity> <input> [param]` | The same dispatch, addressed by name only (no UUID) |
| `toggle <entity>` | `send <entity> Toggle` |
| `inputs <entity>` / `outputs <entity>` | Lists the declared input / output names of the entity's type (`get_entity_type_for_io`); generic inputs are not listed |
| `connections` (alias `list_connections`) | Lists every connection in the scene |
| `connect <source> <output> <target> <input> [delay] [param…]` | Adds a connection (`fire_once` false) with the target's UUID when the target exists; a missing target only warns. A non-numeric delay is refused. One undo step |
| `disconnect <source> [output] [target] [input]` | Removes the source's connections matching every filter given (case-insensitive; all of them with none). One undo step |

Console-dispatched inputs have no source and no activator: `current_source()` is `None`, so an unnamed LogicGate signal is filed as `anonymous` and LogicState object-local inputs log an error.

I/O activity is logged to the debug console under the `IO` category (`io_system.io_log`); errors use the `Error` category.

## Known limitations

* `LogicRelay.CancelPending` replaces the manager's pending list; when it is itself delivered as a delayed event (inside `IOManager.update`), the update loop finishes over the old list and writes it back, so nothing is cancelled. It works when delivered with zero delay from a trigger, an interaction or the console.
* A trigger's `Teleport` and `TouchTest` inputs act on the player only, regardless of `trigger_filters`.
* Tools → Logic Wizard… started while no Logic Graph window exists for the map (`MainWindow.open_logic_wizard`) builds a temporary `LogicGraphScene`, adds the scenario's wires to it and never calls `apply_to_entities()`; the connections are lost. Open the Logic Graph first (`Ctrl+L`) or use its **Wizard…** button.

## Tests

`tests/io/` (`test_connections.py`, `test_io_contract.py`, `test_io_reverse_index.py`, `test_io_validation_and_dormancy.py`), `tests/logic/` (`test_io_dispatch.py`, `test_logic_primitives.py`, `test_logic_composition.py`, `test_logic_triggers.py`, `test_trigger_filters.py`, `test_logic_state.py`, `test_state_values.py`), `tests/console/test_console_undo_and_io.py`, `tests/editor/test_entity_linking.py`, `tests/persistence/test_logic_round_trip.py`.

## See also

* [Logic Entities](https://github.com/ViciousSquid/Fio/wiki/Logic-Entities), [LogicState](https://github.com/ViciousSquid/Fio/wiki/LogicState), [LogicTimer](https://github.com/ViciousSquid/Fio/wiki/LogicTimer), [LogicCommand](https://github.com/ViciousSquid/Fio/wiki/LogicCommand)
* [Logic Graph Editor](https://github.com/ViciousSquid/Fio/wiki/Logic-Graph-Editor), [Property editor](https://github.com/ViciousSquid/Fio/wiki/Property-editor)
* [Logic Thread](https://github.com/ViciousSquid/Fio/wiki/Logic-Thread), [Stable UUIDs](https://github.com/ViciousSquid/Fio/wiki/Stable-UUIDs), [Map format (.json)](https://github.com/ViciousSquid/Fio/wiki/Map-format-(.json))
