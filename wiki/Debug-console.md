> Applies to Fio 3.1.0.0611 (CANARY).

The Debug Console is Fio's log viewer and command line. It shows every message the engine, the I/O system, the AI and the plugins log through `editor.debug_console.debug_log`, and it accepts the commands listed on the [Console commands](https://github.com/ViciousSquid/Fio/wiki/Console-commands) page. It is a tab in the Properties/Console dock in the editor and a one-line overlay at the bottom of the 3D view during Play.

<img src="https://github.com/user-attachments/assets/86e1932d-bbda-49bc-b342-ecf08d436d15" width="800">

## Using it

### Opening the console

| Where | How | What opens |
| --- | --- | --- |
| Editor | The console key, or click the **Debug Console** tab | The **Debug Console** tab of the Properties/Console dock. The dock is made visible if it was closed. Pressing the console key again switches back to the **Properties** tab. |
| Play | The console key | A 36-pixel command line across the bottom of the 3D view (`QtGameView._open_console_overlay`) with the mouse cursor released. Enter runs the command and closes it; Esc or the console key closes it without running anything. |

Entering Play switches the dock to the Debug Console tab automatically and restores the previous tab when Play ends (`MainWindow._store_and_switch_to_debug_console` / `_restore_properties_tab`). With `[Display] always_show_io_debug = True` (Settings > Display > "Always Show IO Debug Console") the editor also starts on the console tab.

### The console key

The console key is the physical key below Escape, whatever character the keyboard layout prints on it. `editor/console_key.py: is_console_key(event)` recognises it by:

| Platform | Test |
| --- | --- |
| macOS | Native virtual key `0x32` (ANSI grave) or `0x0A` (ISO section key on European Macs) |
| Windows | Native scan code `0x29` |
| Linux | Native scan code `49` (evdev `KEY_GRAVE` + 8) |
| Any | Qt key `Key_QuoteLeft`, `Key_AsciiTilde` or `Key_section`, or typed text `` ` ``, `~`, `§`, `±` |

`ConsoleShortcutFilter` is installed on the `QApplication` as an event filter. It intercepts `ShortcutOverride`, `KeyPress` and `KeyRelease` for the console key on any widget that belongs to the main window, so the key toggles the console even while a text field or the pause menu has focus, and it never types a character into the field. Auto-repeat presses are ignored. Widgets in other top-level windows are not affected.

During Play, `QtGameView.keyPressEvent` additionally honours a key named by `[Shortcuts] key_console` in `settings.ini` (default `` ` ``).

### Entering commands

* Type a command at the `]` prompt and press Enter. Empty lines are ignored.
* **Up / Down** walk the command history. Down past the newest entry clears the line.
* History is one list, `editor.debug_console.COMMAND_HISTORY`, shared by the console tab and the in-play command line, so a command typed in one can be recalled in the other. A command identical to the previous entry is not added again. History is held in memory only and is not saved between sessions.
* There is no tab completion. `help` prints the command summary.

### Reading the output

Every message has a category, shown as a `[Category]` prefix and used for colour and filtering:

| Category | Colour | Typical source |
| --- | --- | --- |
| `IO` | light blue | I/O dispatch: outputs fired, inputs executed |
| `Speaker` | light green | Speaker entities |
| `Trigger` | orange | Trigger brushes |
| `Door` | purple | Door brushes |
| `Timer` | pink | Logic timers |
| `MonsterAI` | deep orange | Monster combat, sight and attack decisions |
| `Pathfinding` | teal | Monster patrol and navigation |
| `Plugins` | blue; green for load messages, red for failures | Plugin loading and plugin log output |
| `Cutscene` | green; red when the message starts with `[CUTSCENE] FAILED:` | Cutscene loading |
| `Error` | red | Errors, including unhandled exceptions |
| `Warning` | yellow | Warnings |
| `Info` | white | Command replies and general messages |

A message logged under any other category name is shown in white and appears only under the **All** filter.

Toolbar controls:

| Control | Behaviour |
| --- | --- |
| **Filter** | `All` or one category from the table above. Selecting an `Entity: <name>` entry (added by clicking an entity name) shows only messages containing that name. |
| **Auto-scroll** | Keep the view on the newest line (on by default). |
| **Filter Empty** | Hide messages containing `(0 connections)` or `(no connections for output` (on by default). |
| **Size − / +** | Font size, 6 to 24 pt, default 9. Applies to the log and the input line. |
| **Clear** | Empties the view and the logger's buffer. Same as the `clear` command. |
| **☰** | Shows or hides the **Hide** column on the right: Movers, Triggers, Doors, Monsters, Pathfinding, Cutscenes, Plugins. |

The Hide checkboxes work as follows. **Monsters**, **Pathfinding**, **Cutscenes** and **Plugins** hide the `MonsterAI`, `Pathfinding`, `Cutscene` and `Plugins` categories. **Movers**, **Triggers** and **Doors** hide `[IO]` lines whose source entity name starts with `Mover`, `Trigger` or `Door`; **Triggers** also hides `[IO]` lines from an unnamed source.

Entity names in a message (`Name.Input`, `Name (type=...)`, `'Name'`) are rendered as links. Clicking one filters the console to that entity; clicking it again returns to `All`. `http(s)://` and `github.com/...` URLs open in the system browser.

## Developer reference

### Components

| Object | Module | Role |
| --- | --- | --- |
| `DebugLogger` | `editor/debug_console.py` | Process-wide `QObject` singleton. Holds the last 1000 messages in a `deque` and emits `message_logged(category, message)`. |
| `debug_log(category, message)` | `editor/debug_console.py` | Logs `"[category] message"`. The standard way to write to the console. |
| `debug_log_raw(message)` | `editor/debug_console.py` | Logs a line with no prefix. It is filtered as `Info`. |
| `get_debug_logger()` | `editor/debug_console.py` | Returns the logger, rebuilding it if its Qt object was destroyed. `set_enabled(False)` drops all logging. |
| `install_excepthook()` | `editor/debug_console.py` | Installs a `sys.excepthook` that prints the traceback to stderr and logs it as `Error: Unhandled exception:` instead of letting PyQt abort the process. |
| `DebugConsole` | `editor/debug_console.py` | The widget. Singleton via `DebugConsole.get_instance(parent, logo_path)`. Emits `command_issued(str)`. |
| `CommandInput` | `editor/debug_console.py` | `QLineEdit` with history. Used by the console tab and by `QtGameView._console_input`. |
| `ConsoleShortcutFilter`, `is_console_key` | `editor/console_key.py` | The layout-independent console key. |
| `ConsoleCommandHandler` | `editor/console_commands.py` | Parses and runs command lines. |

### Wiring

`Ui_MainWindow.setupUi` (`editor/ui.py`) creates the console and adds it as the second tab of `MainWindow.properties_tab_widget`. `MainWindow.__init__` then creates the handler and connects them:

```python
self.debug_console = DebugConsole.get_instance(self)
self.console_handler = ConsoleCommandHandler(self)
self.debug_console.command_issued.connect(self.console_handler.handle_command)
```

The in-play command line does not call the handler directly. `QtGameView._submit_console_command` copies the text into the console tab's input and calls `DebugConsole._on_command_entered`, so both entry points share one path: add to history, clear the line, emit `command_issued`.

Commands are also run without the widget by key bindings (`MainWindow.key_bindings`, from `[KeyBindings]` in `settings.ini`) and by map logic (`QtGameView._process_console_command_queue`, which passes `from_map=True`). See [Console commands](https://github.com/ViciousSquid/Fio/wiki/Console-commands) for the parsing rules and the user-only restriction.

### Logging from engine code

```python
from editor.debug_console import debug_log

debug_log("IO", f"{source}.{output} -> {target}.{input}")
debug_log("Error", "something failed")
```

`debug_log` may be called from any thread. The logger emits a Qt signal, which is delivered to the widget on the UI thread as a queued connection. The message is kept in the logger's 1000-entry buffer whether or not a console widget exists, and a console created later loads the buffer.

Message text is inserted as HTML. Tags already in the message are preserved, so a caller can colour part of a line or include its own `<a href="filter:Name">` link. To get an entity name highlighted and clickable automatically, write it as `Name.Something`, `Name (type=...)` or `'Name'`; names are matched as `[a-zA-Z0-9_]+`.

### Registering commands

* Engine code: add a `cmd_<name>(self, args)` method to `ConsoleCommandHandler` and map the command name (and any aliases) to it in the `commands` dict built in `__init__`. Reply with `debug_log`.
* Plugins: call `api.register_console_command(name, callback, help_text="")` from the plugin's `register()`. The callback receives `(args, main_window, logic, play_mode)` and may return a string, which is logged as `Info`. See the [Plugin API](https://github.com/ViciousSquid/Fio/wiki/Plugin-API).

### Output batching and limits

Messages from the logic and AI threads can arrive hundreds of times a second, so the widget batches them (`DebugConsole._on_message` / `_flush_pending`):

| Constant | Value | Meaning |
| --- | --- | --- |
| `DebugLogger._buffer` | `maxlen=1000` | Messages kept for reload after a filter or font change. |
| `FLUSH_INTERVAL_MS` | 100 | An isolated message is shown at once and opens a 100 ms window; messages arriving inside it are queued and inserted together when it closes. The window stays open while a burst lasts. |
| `MAX_LINES_PER_FLUSH` | 200 | A flush shows at most this many lines (the newest) and prints how many were skipped. Skipped lines remain in the buffer. |
| `MAX_DOCUMENT_LINES` | 5000 | Past this many lines the document is rebuilt from the buffer. |
| `FONT_SIZE_MIN` / `MAX` / `DEFAULT` | 6 / 24 / 9 | Font size limits, in points. |

Changing a filter or the font size clears the view and re-renders the buffer, so a filter applies retroactively to the last 1000 messages only. The message counter shows the number of lines currently passing the filters.

Closing the widget hides it instead of destroying it (`closeEvent`).

### Tests

`tests/editor/test_debug_console.py` (widget, filters, batching, singleton rebuild), `tests/editor/test_console_history.py` (shared history), `tests/editor/test_console_key.py` (per-platform key detection and the event filter), and `tests/console/` for the commands.
