> Applies to Fio 3.5.0.1111.

<img width="50" height="50" alt="image" src="https://github.com/user-attachments/assets/4e08659a-720d-40af-a127-483dec2171dc" />

# LogicCommand

`LogicCommand` is a [logic entity](https://github.com/ViciousSquid/Fio/wiki/Logic-Entities) that runs a [console command](https://github.com/ViciousSquid/Fio/wiki/Console-commands) when one of its inputs fires. It is the bridge from the [I/O system](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System) to everything the console can do: camera modes (`cam 2`), HUD (`hudstyle 3`, `message …`), fog and ambient (`r_fogcolor 40 30 60`), saves (`quicksave`), level changes (`map …`) and plugin commands.

| | |
| :--- | :--- |
| Class | `editor/things.py: LogicCommand` |
| `type` in the map | `logic_command` |
| Handlers | `editor/io_handlers.py`: `command_run`, `command_set` |
| Queue | `engine/threaded_game_state.py: queue_console_command` → `engine/qt_game_view.py: QtGameView._process_console_command_queue` |
| Editor icon | `assets/sprites/logic_command.png` |
| Place it | 2D view right-click → Logic Entities → **LogicCommand** |

## Properties

| Key | Type | Default | Editor | Meaning |
| :--- | :--- | :--- | :--- | :--- |
| `command` | string | `""` | *Command* text field | Command line run when the input carries no parameter |
| `disabled` | bool | `false` | *Disabled* checkbox | When set, `RunCommand` / `Trigger` do nothing |
| `name`, `id`, `io_enabled` | | | | Common to all Things |

## Inputs

| Input | Parameter | Effect |
| :--- | :--- | :--- |
| `RunCommand` | command line (optional) | Queues the parameter, or `command` when the parameter is blank, then fires `OnCommand` |
| `Trigger` | command line (optional) | Same handler as `RunCommand`, so a generic trigger chain can drive the entity |
| `SetCommand` | command line | Sets `command` (stripped; a blank parameter clears it). Runs nothing |
| `Enable` | — | `disabled = false` |
| `Disable` | — | `disabled = true` |

## Outputs

| Output | Payload | Fired when |
| :--- | :--- | :--- |
| `OnCommand` | the command line that was queued | After a command has been queued |

## Behaviour

1. If `disabled` is set, nothing happens.
2. The command line is the connection's parameter if non-empty, otherwise the `command` property; leading and trailing whitespace is stripped.
3. If the result is empty, `LogicCommand '<name>': no command to run.` is logged under `IO` and `OnCommand` is **not** fired.
4. Otherwise the line is appended to the game state's console-command queue and `OnCommand` fires at once, on the logic thread, with the line as its value.
5. The UI thread drains the queue once per rendered frame and calls `ConsoleCommandHandler.handle_command(cmd, from_map=True)`. The command therefore runs up to one frame after the input, on the main thread, where touching Qt widgets and editor state is safe.

Consequences:

* `OnCommand` means *queued*, not *finished*. A connection hanging off it runs before the command does.
* Because a blank connection parameter inherits the value of the output that fired it (parameter pass-through), wiring a value-carrying output such as `LogicState.OnValueRead` into `RunCommand` or `Trigger` runs that value as the command line. Give the connection an explicit parameter, or leave it blank on purpose to run a stored command.
* An unknown command logs `Unknown command: <cmd>` and is otherwise harmless; an exception inside a command is caught and printed.

### Commands a map may not run

`ConsoleCommandHandler.USER_ONLY_COMMANDS`: `bind`, `r_renderer`, `vsync`, `r_vsync`, `quit` and `botdebug` are refused when they come from map logic (`'<cmd>' cannot be run by map logic.`). Commands that normally write `settings.ini` (`hudstyle`, `hudopacity`, `hudfade`, `fps`) apply for the session without saving the preference when a map runs them. The `map` command issued by a map is treated like a LevelChanger's level change.

## In the map file

```json
{
  "type": "logic_command",
  "pos": [-448.0, -96.0, -176.0],
  "properties": {
    "type": "logic_command", "io_enabled": true,
    "name": "LogicCommand_1", "id": "402c3c0e-4aa7-46f2-915d-16817c6d9e9f",
    "command": "hudstyle 3", "disabled": false
  },
  "io_connections": []
}
```

This is from `maps/Key_Test.json`, where the PlayerStart's `OnPlayerSpawn` is connected to `LogicCommand_1.RunCommand` with a blank parameter, so `hudstyle 3` runs when the level starts. `maps/MonsterTest.json`, `maps/Topdown_example.json` and `maps/_SHOWCASE.json` also use the entity.

## Example

```text
TriggerBrush.OnTrigger → CameraCmd.RunCommand   (parameter: "cam 2")
```

or leave the parameter blank and set `command` to `cam 2`. `Disable` silences the entity; `Enable` restores it.

## Tests

`tests/console/test_map_command.py`, `tests/editor/test_view_distance_commands.py` (commands driven through a `logic_command`), `tests/io/test_io_contract.py` (declared inputs have handlers).
