> Applies to Fio 3.5.0.1111.

<img width="50" height="50" alt="image" src="https://github.com/user-attachments/assets/49fd7ffe-a11a-4edd-bc8e-d58bb1aa591d" />

# LogicTimer

`LogicTimer` is a [logic entity](https://github.com/ViciousSquid/Fio/wiki/Logic-Entities) that counts down `interval` seconds while it is enabled and fires `OnTimer` when the countdown reaches zero. By default it then starts again; with `one_shot` it switches itself off and fires `OnFinished`. It is the only logic entity that is advanced by the clock rather than by an incoming event.

| | |
| :--- | :--- |
| Class | `editor/things.py: LogicTimer` |
| `type` in the map | `logic_timer` |
| Countdown runtime | `engine/logic_timing.py: LogicTiming` (`arm_timer`, `init_logic_timers`, `update_logic_timers`) |
| Input handlers | `editor/io_handlers.py` (`timer_enable`, `timer_disable`, `timer_toggle`, `timer_fire`, `timer_set_time`, `timer_reset`) |
| Thread | Logic thread, once per 60 Hz tick, after `IOManager.update` |
| Editor icon | `assets/sprites/logic_timer.png` |
| Place it | 2D view right-click → Logic Entities → **LogicTimer** |

## Properties

| Key | Type | Default | Editor | Meaning |
| :--- | :--- | :--- | :--- | :--- |
| `interval` | float, seconds | `1.0` | spin box (3 decimals, step 0.1) | Countdown length. The runtime clamps it to a minimum of `0.01`; a value that is not a number is read as `1.0` |
| `timer_enabled` | bool | `false` | checkbox | Whether the countdown is running. This is the live state: `Enable` / `Disable` write it, a one-shot timer clears it |
| `start_on` | bool | `false` | checkbox | Arm the timer when Play starts |
| `one_shot` | bool | `false` | checkbox | Fire once, then disable and fire `OnFinished` |
| `name`, `id`, `io_enabled` | | | | Common to all Things |

## Inputs

| Input | Parameter | Effect |
| :--- | :--- | :--- |
| `Enable` | — | `timer_enabled = true` and the countdown restarts from the full `interval` |
| `Start` | — | Same handler as `Enable` |
| `Disable` | — | `timer_enabled = false`. The remaining time is kept but not used: the next `Enable` restarts from the full interval |
| `Stop` | — | Same handler as `Disable` |
| `Toggle` | — | `Disable` if `timer_enabled`, otherwise `Enable` |
| `FireTimer` | — | Fires `OnTimer` immediately. Works whether or not the timer is enabled, does not touch the countdown, and never fires `OnFinished` |
| `SetTime` | float, seconds | Sets `interval` (minimum `0.01`; `nan`, `inf` and non-numbers are ignored). A countdown already in progress keeps the interval it was armed with until it is re-armed by `Enable`, `Start` or `ResetTimer` |
| `ResetTimer` | — | Restarts the countdown from the full (current) `interval` without firing. Does not change `timer_enabled` |

The generic inputs (`Hide`, `Show`, `ToggleVisibility`, `Kill`, `SetTint`, `ClearTint`) are accepted and only set flags.

## Outputs

| Output | Fired when |
| :--- | :--- |
| `OnTimer` | The countdown reaches zero, or `FireTimer` is called |
| `OnFinished` | A `one_shot` timer has fired `OnTimer` and disabled itself (fired immediately after that `OnTimer`) |

Neither output carries a value.

## Runtime behaviour

* **Play start.** `LogicSession` clears every countdown, then `LogicTiming.init_logic_timers()` arms each timer whose `start_on` is true: it sets `timer_enabled = true` and a countdown of `interval`. This happens after the spawn PlayerStart has fired `OnPlayerSpawn`.
* **A timer authored with `timer_enabled: true` but `start_on: false`** also runs: `update_logic_timers` creates a countdown for any enabled timer that has none. `start_on` is the supported way to say it.
* **Each tick** (`update_logic_timers(delta)`), for every timer with `timer_enabled`: `remaining -= delta`; when `remaining <= 0` it fires `OnTimer`, then either (repeating) sets `remaining` back to the armed interval, or (`one_shot`) sets `timer_enabled = false`, discards the countdown and fires `OnFinished`.
* **Resolution.** The countdown is advanced by the 60 Hz logic tick and fires at most once per tick. The overshoot past zero is discarded rather than carried into the next period, so the real period is `interval` rounded up to a whole number of ticks, and an interval shorter than one tick (1/60 s) fires every tick.
* **Pausing.** Countdowns run during cutscenes, while the player is dead and while the level-complete screen is up. They stop while the world is paused, and do not run outside Play.
* **Identity.** Countdowns are kept in `LogicTiming.timer_states`, keyed by the timer's UUID (its name if it has none): `{remaining, interval}`.
* **Save games** store `timer_states`, so a countdown resumes where it was (`engine/savegame.py`, `runtime.timer_states`). The map file stores only the properties; a level saved from the editor after Play (without *Restore the world when leaving Play*) keeps whatever `timer_enabled` and `interval` the session left.
* **Play stop** clears all countdowns.

## In the map file

```json
{
  "type": "logic_timer",
  "pos": [0.0, 0.0, 0.0],
  "properties": {
    "type": "logic_timer", "io_enabled": true,
    "name": "LogicTimer_1", "id": "88c3504e-4528-4ba7-8785-e7c01b004db8",
    "interval": 2.0, "timer_enabled": false, "start_on": true, "one_shot": false
  },
  "io_connections": [
    {"output": "OnTimer", "target": "Light_1", "input": "Toggle",
     "parameter": "", "delay": 0.0, "fire_once": false,
     "target_id": "3d80e810-1675-4595-913a-4a59539733cc"}
  ]
}
```

## Patterns

```text
Repeating      start_on = true                 OnTimer → Light.Toggle
Delayed once   one_shot = true                 Trigger.OnTrigger → Timer.Enable ;  Timer.OnTimer → Door.Open
Restartable    one_shot = true                 Button.OnTrigger → Timer.Enable   (each press restarts the full interval)
Stop a loop                                    Trigger.OnTrigger → Timer.Disable
```

A single delayed action does not need a timer: every [connection](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System) has its own `delay`. Use a timer when the delay must be cancellable (`Disable`), restartable (`Enable`), changeable at run time (`SetTime`) or repeating. The Logic Wizard's *Timed Door*, *Timed Patrol*, *Flickering Light* and *Timed Hazard* scenarios build timer networks.

## Tests

`tests/logic/test_logic_primitives.py` (enable, one-shot, `OnFinished`, `SetTime`, `ResetTimer`), `tests/logic/test_logic_composition.py`, `tests/persistence/test_logic_round_trip.py`, `tests/io/test_io_contract.py` (every declared input has a handler).

## See also

[Logic Entities](https://github.com/ViciousSquid/Fio/wiki/Logic-Entities) · [Triggers & The I/O Logic System](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System) · [Logic Thread](https://github.com/ViciousSquid/Fio/wiki/Logic-Thread) · [Save Games](https://github.com/ViciousSquid/Fio/wiki/Save-Games)
