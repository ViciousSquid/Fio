> Applies to Fio 3.1.0.0611 (CANARY).

This page is the complete reference for every console command in Fio: the built-in commands registered by `editor/console_commands.py: ConsoleCommandHandler`, and the commands registered by the bundled plugins. Commands are typed into the [Debug Console](https://github.com/ViciousSquid/Fio/wiki/Debug-console), into the in-play command line, bound to a key with `bind`, or queued by map logic through a [`logic_command`](https://github.com/ViciousSquid/Fio/wiki/LogicCommand) entity.

Fio has no separate cvar system. Every setting reachable from the console is a command; a command that takes a value usually prints the current value when run with no argument.

## How a command line is parsed

`ConsoleCommandHandler.handle_command(cmd_string, *, from_map=False)`:

1. The line is stripped and split once on whitespace. The first word, lower-cased, is the command name; the remainder (stripped) is passed to the handler as one string. There is no quoting, no `;` chaining and no variable expansion at this level; each handler splits its own arguments.
2. The name is looked up in `ConsoleCommandHandler.commands` (a plain `dict` built in `__init__`: 124 names mapping to 81 handlers). Command names are case-insensitive; **entity names are case-sensitive** and are resolved with `EditorState.find_entity_by_name` (exact match; brushes are searched before things).
3. A name that is not built in is offered to the plugin manager (`PluginManager.has_console_command` / `dispatch_console_command`). A built-in name therefore always wins over a plugin command of the same name.
4. Otherwise the console prints `Unknown command: <name>. Type 'help' for list.`

All handlers run on the Qt UI thread. Handlers that touch a running play session take the logic thread's tick lock (`LogicThread._tick_lock`) first, so the change lands between ticks.

### Commands from map logic

A `logic_command` entity's `RunCommand` input calls `ThreadedGameState.queue_console_command`; `QtGameView._process_console_command_queue` drains the queue on the UI thread and calls `handle_command(cmd, from_map=True)`. Two rules apply to those calls:

* The names in `ConsoleCommandHandler.USER_ONLY_COMMANDS` are refused with `'<name>' cannot be run by map logic.`: `bind`, `botdebug`, `quit`, `r_renderer`, `r_vsync`, `vsync`.
* Commands that normally persist a setting to `settings.ini` (`hudopacity`, `hudfade`, `fps`) apply it for the session only.

### Argument conventions

| Convention | Accepted by | Values |
| --- | --- | --- |
| Switch | `r_distancefog` | `on 1 true yes enable enabled` / `off 0 false no disable disabled`; anything else (or nothing) toggles |
| Switch | `physics`, `phys_sleep`, `showglasses` | `on 1 true` / `off 0 false`; no argument toggles |
| Switch | `r_wireframe` | `on 1 true yes` / `off 0 false no`; no argument toggles |
| Colour, 0-255 integers | `tint`, `portal_color` | Three integers, each clamped to 0..255 |
| Colour, either form | `r_fogcolor`, `ambient` (three-value form) | Three numbers. If any component is above 1.0 all three are treated as 0-255 bytes and divided by 255; otherwise they are 0.0-1.0 floats. Clamped to 0..1 |
| Colour, floats | `r_clearcolor` | Three floats, each clamped to 0.0..1.0 |

"Play only" in the tables below means the handler calls `_require_play_mode` and prints `Command '<name>' can only be used in Play Mode.` in the editor.

## General

| Command | Arguments | Effect |
| --- | --- | --- |
| `help` | none | Prints the built-in command summary, followed by a "Plugin Commands" section listing, in name order, every command registered by an enabled plugin with its help text. |
| `clear` | none | Clears the Debug Console view **and** the logger's message buffer (`DebugConsole.clear`). |
| `bind` | `<key> <command...>` | Binds a key to a console command line. The binding is stored in `MainWindow.key_bindings` and written to the `[KeyBindings]` section of `settings.ini`. If the key is already bound a confirmation dialog asks before overwriting. User-only. |
| `bind` | none | Opens the "Bind Key" dialog (a key-sequence field and a command field). |
| `fps` | none | Toggles `[Display] show_fps` and repaints the 3D view. Saved to `settings.ini` unless run by map logic. |
| `map` | `<name>` | Opens `maps/<name>.json` (`.json` is appended if missing) through `MainWindow.open_level_file`. The resolved path must stay inside `maps/`; anything else is refused with `map: '<name>' is outside the maps folder`. While a played package is open, the package's own `maps/` is searched first. In the editor the unsaved-changes prompt applies; during Play the level changes without leaving Play. |
| `quit` | none | Closes Fio immediately via `MainWindow.quit_immediately`, with no unsaved-changes prompt. User-only. |
| `pos` | none | Opens the live camera-position window (same as **Debug > Position (P)**). |

`bind <key> <command>` parses the key with `QKeySequence` and stores it under Qt's own spelling (`f5` becomes `F5`, `ctrl+k` becomes `Ctrl+K`); a name that is not exactly one key combination is refused with `bind: '<key>' is not a key`. A key press is looked up as `QKeySequence(key | modifiers).toString()` in `MainWindow.key_bindings`. On disk the key name is percent-encoded (`urllib.parse.quote(key, safe='+')`) and a `%` in the command is doubled, so any key and any command survive `configparser`. Bindings are checked in `MainWindow.keyPressEvent`, in the editor and in Play, whenever the Debug Console's input line does not have focus, and only for a key press that the editor's own shortcuts and key handlers have not already consumed (for example `G`, `[`, `]`, `Delete` in the editor, or `E`, `F1`, `F3`, `F12` in Play).

## Entities

| Command | Aliases | Arguments | Effect |
| --- | --- | --- | --- |
| `list` | `entities`, `ents`, `ls` | none | Lists every brush (`name [Trigger\|Mover\|Door\|Brush]`) and every thing (`name (type=...)`). |
| `ent` | `info` | `<name>` | Brush: prints its first 20 keys and its I/O connections. Thing: prints every property. |
| `spawn` | | `prop <health\|ammo\|gun1\|gun2\|custom1\|custom2\|key> [value]` | Adds a collectible Prop at `[0, 0, 0]` named `Prop_<item>_<n>`. `value` (default `25`) is stored as `collect_value`. `gun1`/`gun2`/`custom1`/`custom2` spawn that [item](https://github.com/ViciousSquid/Fio/wiki/Items)'s pickup (`collect_type = item`, sprite from the map's item definitions). |
| `spawn` | | `light` | Adds a Light at `[0, 100, 0]` named `Light_<n>`. |
| `spawn` | | `levelchanger` | Adds a LevelChanger at `[0, 40, 0]` named `LevelChanger_<n>` with `target_map = "Simple_Map_Test.json"`. |
| `delete` | `kill` | `<name>` | Removes one brush or thing. |
| `delete` | `kill` | `all <type>` | Removes every entity of a type, after a Yes/No confirmation dialog. The type is compared with punctuation and case removed, against a thing's class name or `properties.type`, or for brushes `trigger`/`mover`/`door` (from the `is_*` flags), `type` or `classname`. |
| `hide` | | `<name>` | Sets the authored `hidden` flag (through `engine.spatial.set_authored_flag`, so a Big World-parked object is handled). For a brush in Play the collision grid is marked dirty. |
| `show` | | `<name>` | Clears `hidden`, same path as `hide`. |
| `tint` | | `<name> <R> <G> <B>` | Sets `tint` to `[R, G, B]` (0-255 integers) on a brush or a thing's properties. |
| `tint` | | `<name> clear` | Removes `tint`. |
| `setprop` | `set` | `<name> <key> <value...>` | Writes a property (brush dict key or thing property). The value is stored as text, except that `true`/`false` become booleans when the current value is a boolean or absent. `hidden` and `disabled` booleans go through the parking-aware writer; changing `active`, `portal_target` or `name` on a Portal re-syncs its linked group. The running logic thread's entity caches are rebuilt. |
| `getprop` | `get` | `<name> <key>` | Prints the value, or `<not found>`. Takes exactly two words. |

`spawn`, `delete`, `setprop`, `connect`, `disconnect` and the `portal_*` editing commands push an undo checkpoint (`EditorState.save_state`) before changing the scene. `delete` during Play performs the removal and the cache rebuild under the tick lock.

## I/O

| Command | Aliases | Arguments | Effect |
| --- | --- | --- | --- |
| `fire` | `ent_fire` | `<name> <input> [parameter...]` | Fires an **input** on an entity. In Play it calls `IOManager._execute_input` with the entity's UUID, exactly as a runtime connection would. With no play session, an entity that implements `on_input()` handles it directly; any other entity reports `inputs require Play Mode`. |
| `send` | | `<name> <input> [parameter...]` | Dispatches an input through the play session's `IOManager` by name only (no UUID). Outside Play nothing is dispatched, although `Sent input ...` is still printed. |
| `trigger` | | `<name>` | A door or mover brush receives `Toggle` (Play only; nothing is dispatched in the editor); anything else receives `Trigger` (via `fire`). |
| `toggle` | | `<name>` | Equivalent to `send <name> Toggle`. |
| `outputs` | | `<name>` | Lists the outputs registered for the entity's I/O type. |
| `inputs` | | `<name>` | Lists the inputs registered for the entity's I/O type. |
| `connect` | | `<source> <output> <target> <input> [delay] [parameter...]` | Adds an `OutputConnection` to the source. `delay` is seconds (default `0`; a non-number is an error). The target's UUID is recorded when the target exists; a missing target only warns. `fire_once` is always false. |
| `disconnect` | | `<source> [output] [target] [input]` | Removes the source's connections that match every filter supplied (case-insensitive). With no filter, removes all of them. |
| `list_connections` | `connections` | none | Prints every connection in the level as `source.Output → target.Input` and a total. Any argument is ignored. |

## Monsters and AI

| Command | Aliases | Arguments | Effect |
| --- | --- | --- | --- |
| `monster_kill` | `kill_monster` | `<name>` | Sets `health = 0` and `dead = True` on a Monster and, in Play, fires its `OnDeath` output. |
| `monster_revive` | | `<name>` | Restores a Monster: health to its current value if positive, otherwise `100`; clears `dead`, `hidden`, `awake` and `is_shooting`; in Play drops its AI state under the monster lock. |
| `monster_revive_all` | | none | The same for every Monster in the level. |
| `botdebug` | | `[on\|off]` | Opens (`on` or no argument) or closes (`off`) the floating bot-decision window (`editor/bot_debug.py`). User-only. |
| `notarget` | | none | Play only. Toggles `player_runtime.notarget`; monsters ignore the player. |

## Player and play session

| Command | Aliases | Arguments | Effect |
| --- | --- | --- | --- |
| `god` | | none | Play only. Toggles `player_runtime.god_mode`; turning it on turns `buddha` off. |
| `buddha` | | none | Play only. Toggles `player_runtime.buddha_mode` (health cannot drop below 2); turning it on turns `god` off. |
| `noclip` | | none | Play only. Toggles `player.physics_enabled`. The toast and log line report the new value of `physics_enabled` under the label "Noclip", so `Noclip: OFF` is printed when collision physics has just been switched off. |
| `physics` | | `[on\|off]` | Play only. Sets or toggles the same `player.physics_enabled` flag. |
| `setpos` | `teleport` | `<x> <y> <z>` | Play only. Moves the player to a finite world position, zeroes velocity and tells the portal runtime the player teleported. |
| `cam` | `camera` | `[overhead\|fp] [seconds]` | Play only. Tweens the play camera between Overhead and First Person. A numeric token is the tween duration (default `1`; `0` switches instantly). Mode words: `overhead top topdown top-down td down`, or `fp first firstperson first-person fps person`. With no mode word it toggles. Unknown words are ignored with a warning. |
| `ss` | | none | Play only. Toggles [split-screen](https://github.com/ViciousSquid/Fio/wiki/Split‐Screen) (same as F9). |
| `showglasses` | | `[on\|off\|toggle]` | Play only. Shows or hides the player glasses billboards (`QtGameView.show_glasses`). |
| `message` | | `"text"` | Play only. Shows a transient message on the first play-view message line. |
| `message2` | | `"text"` | Play only. The same on the second line. |
| `message3` | | `"text"` | Play only. The same on the third line, drawn in the Rushford font. |

`message`, `message2` and `message3` strip one pair of matching outer quotes, truncate the text to 50 characters, and show it for 7 seconds with a 1-second fade at each end (`QtGameView._draw_queued_view_message`). Each line has its own FIFO queue: a message that arrives while that line is busy is shown after the current one finishes.

## Physics (dynamic bodies)

All are Play only and act on the session's `PhysicsWorld` (`engine/physics.py`); see [Physics](https://github.com/ViciousSquid/Fio/wiki/Physics). The value commands require an argument, clamp it to the range shown and wake every body.

| Command | Arguments | Range | Default | Effect |
| --- | --- | --- | --- | --- |
| `phys_gravity` | `<units/s²>` | -5000..5000 | -800 | Global gravity; `0` disables it. |
| `phys_timescale` | `<multiplier>` | 0..4 | 1 | Simulation speed; `0` pauses bodies. |
| `phys_friction` | `<multiplier>` | 0..4 | 1 | Scales authored friction. |
| `phys_damping` | `<multiplier>` | 0..4 | 1 | Scales authored damping. |
| `phys_sleep` | `[on\|off]` | | on | Automatic body sleeping; no argument toggles. |
| `phys_info` (alias `phys`) | none | | | Prints the five values above. |
| `phys_reset` | none | | | Restores the defaults above. |

## HUD

| Command | Aliases | Arguments | Effect |
| --- | --- | --- | --- |
| `hudstyle` | `hudtext` | `0\|1\|2\|3\|4 [font]` | Selects the HUD style for the session: `0` hidden, `1` Rushford, `2` O.K. Retro, `3` HornetDisplay, `4` LCD/custom. A font family name is accepted only with style `4`. No argument prints the current style. Not saved. |
| `hudopacity` | | `0..100` | HUD opacity in percent. No argument prints it. Saved as `[Display] hudopacity`. |
| `hudfade` | | `0\|1` | Disables or enables the damage-driven health fade. No argument prints it. Saved as `[Display] hudfade`. |

## Save games

Saves live in `saves/` under the install root with the extension `.fiosave`. A save name is reduced to its base name (no directories) and the extension is added if missing; with no name the slot is `quicksave`. See [Save Games](https://github.com/ViciousSquid/Fio/wiki/Save-Games).

| Command | Aliases | Arguments | Effect |
| --- | --- | --- | --- |
| `save` | `savegame` | `[name]` | Play only. Writes the session with `session_runtime.save_session`, using the mode in `[Settings] save_mode` (`full`, `delta` or `both`). |
| `load` | `loadgame` | `[name]` | In Play, applies the save to the running session. From the editor, opens the save's map (looked up by base name in `maps/` only), enters Play, then applies it. A delta save made on a different base map prompts before being force-applied. |
| `quicksave` | `qs` | none | `save quicksave`. |
| `quickload` | `ql` | none | `load quicksave`. |
| `saves` | `listsaves` | none | Lists the `.fiosave` files with their map and save time. |

## Rendering

| Command | Aliases | Arguments | Effect |
| --- | --- | --- | --- |
| `r_list` | | none | Prints the active renderer, the Display mode, shadows, the water-quality cap and the view-distance/fog/ambient rows from `ViewDistance.describe()`. |
| `r_info` | | none | Identical to `r_list`. |
| `r_renderer` | | `[name]` | With no argument lists the registered renderers and marks the active one. With a name (case-insensitive) switches to it live via `QtGameView.switch_renderer`; if the new renderer fails to start, the current one keeps running. User-only. The built-in registry contains `Forward`; plugins add more through `register_renderer` (see the [Renderer Development Guide](https://github.com/ViciousSquid/Fio/wiki/Renderer-Development-Guide)). |
| `r_wireframe` | `wireframe` | `[on\|off]` | Drives the status bar's **Display** box. `on` selects `Wireframe` and remembers the previous mode; `off` returns to that mode, or to `Textured` if none was remembered. No argument toggles. |
| `r_shadows` | `shadows` | none | Toggles `renderer.shadows_enabled` for the session. Arguments are ignored. |
| `r_waterquality` | `waterquality` | `[cheap\|expensive]` | Session-only cap over each water brush's own High quality flag: `cheap` forces every water brush onto the cheap path, `expensive` lets each brush decide. No argument toggles. Never saved. |
| `r_vsync` | `vsync` | none | Flips `[Display] vsync` in `settings.ini`. It changes only the stored preference. User-only. |
| `r_clearcolor` | | `<r> <g> <b>` | Sets `renderer.clear_color`; three floats in 0.0..1.0. |

### View distance, fog and ambient light

These write the shared `engine.view_distance.ViewDistance` object held by the viewport, the renderer and the logic thread. They change what is drawn, not what is loaded or simulated. None of them is saved with the map or in `settings.ini`; a map sets them at run time by firing them from a `logic_command`.

| Command | Aliases | Arguments | Effect |
| --- | --- | --- | --- |
| `r_viewdistance` | `viewdistance`, `r_culldistance`, `culldistance`, `r_cullfogdist`, `cullfogdist`, `r_cullfogdistance`, `cullfogdistance`, `farplane` | `[units]` | Sets the far plane / cull distance through `MainWindow.set_cull_distance`, which also updates the status bar's **Cull Dist** spinner. Clamped to 500..20000 (default 4096); a clamped request prints a warning. No argument prints the distance and the fog band. |
| `r_distancefog` | `distancefog` | `[on\|off]` | Enables or disables far-plane fog (default on). No argument toggles. Turning it off warns that geometry will pop at the far plane. |
| `r_fogdistance` | `fogdistance`, `fogdist` | `<start> <end>` \| `<end>` \| `auto` | Pins where fog starts and where it becomes opaque. One number sets only the end. `auto` un-pins both. No argument prints the band. |
| `r_fogstart` | `fogstart` | `<units>` \| `auto` | Pins or un-pins the fog start. |
| `r_fogend` | `fogend` | `<units>` \| `auto` | Pins or un-pins the fog end. |
| `r_fogdensity` | `fogdensity` | `[value]` | `0` (default) is a linear ramp from start to end; a positive value adds an exponential-squared curve, `1 - exp(-(density * (dist - start))²)`, and the larger of the two is used. |
| `r_fogcolor` | `fogcolor`, `r_fogcolour`, `fogcolour` | `[<r> <g> <b>]` | Fog colour, which is also the background past the far plane. Default `(0.10, 0.10, 0.15)`. |
| `ambient` | `r_ambient` | `<level>` \| `<r> <g> <b>` \| `off` | Level-wide omnidirectional light added on top of each shader's own ambient; no entity is created. A single number is a grey level (a value above 1 is read as 0-255). `off` or `none` sets it to zero. No argument prints it. |

How the fog band is resolved (`ViewDistance.resolve`): an un-pinned start is 60% of the view distance and an un-pinned end is 92%. The end is clamped to at most 98% of the view distance (and to `ViewDistance.limit` if one is set), and the start is kept at least 1 unit below the end. `r_fogdistance` and `r_fogend` print a warning when the requested end had to be pulled back.

## Debug overlays

| Command | Aliases | Arguments | Effect |
| --- | --- | --- | --- |
| `sg` | | none | Play only. Toggles the [spatial grid](https://github.com/ViciousSquid/Fio/wiki/Spatial-Grid) overlay (`QtGameView.show_spatial_grid`). |
| `showcollision` | `collisionvis`, `collision` | `[on\|all\|off\|mesh\|aabb\|toggle]` | Sets the collision visualisation mode (`QtGameView._collision_vis_mode`): `on` or `all` shows both kinds, `mesh` shows mesh collision triangles, `aabb` shows AABB boxes, `off` hides them. `toggle` or no argument cycles `off → all → mesh → aabb`. Works in the editor and in Play. |

## Portals

All portal commands address a [Portal](https://github.com/ViciousSquid/Fio/wiki/Portal) entity by its `name`.

| Command | Arguments | Effect |
| --- | --- | --- |
| `portal_list` | none | Lists every portal: name, `portal_target`, active state, position and rim colour. |
| `portal_create` | `<name1> <name2> [x y z]` | Creates two portals linked to each other, both active, 64 units either side of the position on X. Without a position the 3D camera position is used. |
| `portal_link` | `<name> <target>` | Sets `portal_target` and re-syncs the linked group. The target need not exist yet. |
| `portal_color` | `<name> <R> <G> <B>` | Sets the rim `color` (0-255 integers) on every portal with that name. |
| `portal_enable` | `<name>` | Activates the portal and its linked group (`editor.portal_links.set_group_active`), as the `Enable` input does. |
| `portal_disable` | `<name>` | Deactivates the portal and its linked group. |
| `portal_delete` | `<name> [and_pair]` | Removes the portal; with the literal word `and_pair` also removes the portal named by its `portal_target`. |

## Plugin commands

A plugin registers a command in its `register()` with `EditorAPI.register_console_command(name, callback, help_text="")` (`plugins/api.py`; see the [Plugin API](https://github.com/ViciousSquid/Fio/wiki/Plugin-API)). The name is lower-cased and stored in `PluginManager._console_commands`. The callback signature is:

```python
def callback(args: str, main_window, logic, play_mode: bool) -> str | None
```

`args` is the rest of the line, `logic` is the running `LogicThread` (or `None`), and a returned string is printed to the console in the `Info` category. An exception in the callback is logged in the `Plugins` category and the command still counts as handled. A plugin command only exists while its plugin is enabled, and it cannot replace a built-in name. `help` lists the enabled plugins' commands from `PluginManager.console_commands()`, a `dict` of `name -> {"plugin": ..., "help": ...}`.

### Deathmatch plugin (`plugins/deathmatch/plugin.py`)

These require a running Deathmatch session in Play; otherwise they reply `Start a Deathmatch arena in Play mode first.` `addbot`, `removebot` and `fraglimit` must be run on the LAN host when the session is a network client. See [Deathmatch Plugin](https://github.com/ViciousSquid/Fio/wiki/Deathmatch-Plugin).

| Command | Arguments | Effect |
| --- | --- | --- |
| `addbot` | `["name"] [easy\|normal\|hard] [red\|blue\|auto]` | Adds a match bot. Arguments are parsed with `shlex`, so a name with spaces is quoted. Skill defaults to `normal`, team to `auto`. |
| `removebot` | `["name"\|id]` | Removes a bot; with no argument, the last bot. |
| `fraglimit` | `[number]` | With no argument prints the frag limit. Otherwise sets it; a whole number 0..10000, `0` meaning unlimited. |
| `dflaglight` | `[on\|off]` | CTF only. Enables or disables shadows on the flag lights; no argument means `on`. Also accepts `1`/`0`. |
| `spectate` | `[free\|off\|next\|prev\|<player name>]` | With no argument opens the spectator menu. `free` enters free spectating, `off` leaves it, `next`/`prev` cycle the followed player, and a name follows that player. |

The Big World and Tidy plugins register no console commands.

## Removed commands

`r_fog`, `r_water`, `r_glass`, `r_lighting`, `r_deferred` and their aliases `fog`, `water`, `glass`, `lighting`, `deferred` were removed in 3.0.0. They toggled settings no renderer had. Typing one now prints the unknown-command message.

## Extending and testing

* To add a built-in command, add a `cmd_*` method to `ConsoleCommandHandler` and an entry in the `commands` dict in `__init__`; add the name to `USER_ONLY_COMMANDS` if map logic must not run it, and add a line to the text in `cmd_help`.
* To add a command from a plugin, use `register_console_command` as above.
* Tests: `tests/console/` (`test_console_undo_and_io.py`, `test_delete.py`, `test_hud.py`, `test_map_command.py`, `test_message.py`, `test_monster_kill.py`, `test_render_commands.py`), `tests/editor/test_view_distance_commands.py`, `tests/editor/test_portal_console_fade.py`, and `plugins/deathmatch/tests/test_deathmatch.py` for the plugin commands.
