Commands can be entered directly into the debug console or invoked through the I/O system using a [`logic_command`](https://github.com/ViciousSquid/Fio/wiki/LogicCommand) entity.

## General

| Command | Description                        |
| ------- | ---------------------------------- |
| `help`  | Display available console commands |

## Entities

| Command                   | Description                      |
| ------------------------- | -------------------------------- |
| `list`                    | List entities                    |
| `entities`                | Alias for `list`                 |
| `ents`                    | Alias for `list`                 |
| `ls`                      | Alias for `list`                 |
| `ent <name>`              | Show information about an entity |
| `info <name>`             | Alias for `ent`                  |
| `spawn ...`               | Spawn an entity                  |
| `delete <name>`           | Delete an entity                 |
| `kill <name>`             | Alias for `delete`               |
| `hide <name>`             | Hide an entity                   |
| `show <name>`             | Show a hidden entity             |
| `tint <name> <R> <G> <B>` | Apply a tint                     |
| `tint <name> clear`       | Clear an entity's tint           |

## Entity Properties

| Command                             | Description             |
| ----------------------------------- | ----------------------- |
| `setprop <name> <property> <value>` | Set an entity property  |
| `set <name> <property> <value>`     | Alias for `setprop`     |
| `getprop <name> <property>`         | Read an entity property |
| `get <name> <property>`             | Alias for `getprop`     |

## I/O

| Command                      | Description                   |
| ---------------------------- | ----------------------------- |
| `fire <entity> <output>`     | Fire an entity output         |
| `ent_fire ...`               | Alias for `fire`              |
| `trigger <entity>`           | Trigger an entity             |
| `send <entity> <input> ...`  | Send an input to an entity    |
| `toggle <entity> <property>` | Toggle an entity property     |
| `outputs <entity>`           | List available entity outputs |
| `inputs <entity>`            | List available entity inputs  |
| `connect ...`                | Create an I/O connection      |
| `disconnect ...`             | Remove an I/O connection      |
| `list_connections`           | List I/O connections          |
| `connections`                | Alias for `list_connections`  |

## Monsters

| Command                 | Description              |
| ----------------------- | ------------------------ |
| `monster_kill <name>`   | Kill a named monster     |
| `kill_monster <name>`   | Alias for `monster_kill` |
| `monster_revive <name>` | Revive a named monster   |
| `monster_revive_all`    | Revive all monsters      |

## Play Mode

| Command          | Description                                              |
| ---------------- | -------------------------------------------------------- |
| `physics ...`    | Control physics debugging/state                          |
| `setpos ...`     | Set the player position                                  |
| `teleport ...`   | Alias for `setpos`                                       |
| `ss ...`         | Control split-screen                                     |
| `cam ...`        | Control the camera                                       |
| `camera ...`     | Alias for `cam`                                          |
| `message "text"` | Display a temporary message directly in the 3D play view |
| `noclip`         | Toggle noclip movement                                   |
| `god`            | Toggle god mode                                          |
| `buddha`         | Toggle Buddha mode                                       |
| `notarget`       | Toggle whether monsters target the player                |

### `message`

`message "text"` displays text directly in the `QtGameView` play screen.

The message:

* Is limited to 50 characters.
* Appears one line above the row used for held items/keys.
* Fades in over 1 second.
* Remains fully visible for 5 seconds.
* Fades out over 1 second.
* Remains on screen for 7 seconds in total.

Only one message is displayed at a time. If another `message` command arrives while a message is already being displayed, the new message is placed in a FIFO queue and is shown automatically after the current message has completely faded out.

For example:

```text
message "You found the blue key"
```

## Level and Save Games

| Command        | Description                   |
| -------------- | ----------------------------- |
| `map ...`      | Load/change the current map   |
| `save ...`     | Save the current play session |
| `savegame ...` | Alias for `save`              |
| `load ...`     | Load a saved play session     |
| `loadgame ...` | Alias for `load`              |
| `quicksave`    | Create a quicksave            |
| `qs`           | Alias for `quicksave`         |
| `quickload`    | Load the quicksave            |
| `ql`           | Alias for `quickload`         |
| `saves`        | List available saves          |
| `listsaves`    | Alias for `saves`             |

## Rendering

| Command                          | Description                                                                 |
| -------------------------------- | --------------------------------------------------------------------------- |
| `r_list`                         | Show all current render settings                                            |
| `r_info`                         | Same as `r_list`                                                            |
| `r_renderer [name]`              | List the registered renderers, or switch to one live                        |
| `r_wireframe [on\|off]`          | Wireframe display (the Display box); off returns to the previous mode       |
| `r_shadows`                      | Toggle shadows                                                              |
| `r_waterquality [cheap\|expensive]` | Debug cap on water quality; no argument toggles                          |
| `r_vsync`                        | Toggle VSync                                                                |
| `r_clearcolor <r> <g> <b>`       | Set the background colour                                                   |
| `r_viewdistance <units>`         | Set the maximum render/cull/fog distance (the far plane)                    |
| `r_distancefog [on\|off]`        | Toggle far-plane distance fog                                               |
| `r_fogdistance <start> <end>`    | Where fog ramps up and goes opaque (`auto` to track the view distance)      |
| `r_fogstart <units>`             | Where fog begins (`auto`)                                                   |
| `r_fogend <units>`               | Where fog is fully opaque (`auto`)                                          |
| `r_fogdensity <value>`           | 0 = linear ramp; higher thickens the near half                              |
| `r_fogcolor <R> <G> <B>`         | Fog colour, and the sky behind it                                           |
| `ambient <level>`                | Global omnidirectional light (`<R> <G> <B>` or `off` also accepted)         |

The earlier `r_fog`, `r_water`, `r_glass`, `r_lighting` and `r_deferred` toggles (and their aliases `fog`, `water`, `glass`, `lighting`, `deferred`) were removed in 3.0: they toggled settings no renderer had.

### `r_renderer`

With no argument, `r_renderer` lists the registered renderers, marking the active one `(active)`. For example, with the example deferred-renderer plugin installed:

```text
r_renderer
Renderers: Forward (active), Deferred
```

`r_renderer <name>` switches to that renderer **live**, in the editor or in Play. Names match case-insensitively. If the new renderer fails to start, the current one keeps running and the console reports the error. Renderers come from the built-in registry (`Forward`) and from plugins; see the [Renderer Development Guide](https://github.com/ViciousSquid/Fio/wiki/Renderer-Development-Guide).

`r_renderer` is a user-only command: map logic (a [`logic_command`](https://github.com/ViciousSquid/Fio/wiki/LogicCommand) entity) cannot run it, because a renderer swap may start plugin code.

### `r_wireframe`

`r_wireframe [on|off]` (alias `wireframe`) drives the editor's **Display** box, so the two always agree:

* `on` selects **Wireframe**.
* `off` returns to the mode that was showing before (**Solid Lit** if there was none).
* No argument toggles.

It is not renderer-specific: it only changes the editor's display mode, which every renderer receives as frame input. The other Display modes (Points, Solid Lit, Textured, Overlay) are chosen from the Display box.

### `r_waterquality`

`r_waterquality cheap|expensive` (alias `waterquality`) is a session-only debug cap over each water brush's own **High quality** flag. `expensive` lets every brush choose; `cheap` forces all water to the cheap path. With no argument it toggles. It is never saved.

### `r_list`

`r_list` shows the active **Renderer** (its registry name) and the current **Display** mode, followed by shadows, the water-quality cap, and the view-distance and fog settings.

### View Distance and Fog

`r_viewdistance` controls the maximum rendering distance. Fio also performs distance-based geometry culling, preventing objects beyond the configured distance from being submitted for rendering.

Distance fog can be used to hide the transition to the far-plane boundary. Fog and view distance are independent: disabling fog does not change the configured view distance.

For example:

```text
r_viewdistance 4096
r_distancefog on
```

sets a 4096-unit rendering horizon with distance fog enabled.

```text
r_viewdistance 6000
r_distancefog off
```

sets a 6000-unit rendering horizon while leaving fog disabled. The far-plane boundary will therefore remain visible.

`r_distancefog on` and `r_distancefog off` explicitly set the fog state rather than toggling it, making them safe to invoke repeatedly through bindings or the I/O system.

## Rendering aliases

The following shorter aliases are provided:

| Alias                                                                  | Equivalent       |
| ---------------------------------------------------------------------- | ---------------- |
| `wireframe`                                                            | `r_wireframe`    |
| `shadows`                                                              | `r_shadows`      |
| `waterquality`                                                         | `r_waterquality` |
| `vsync`                                                                | `r_vsync`        |
| `viewdistance`, `culldistance`, `cullfogdist`, `cullfogdistance`, `farplane`, `r_culldistance`, `r_cullfogdist`, `r_cullfogdistance` | `r_viewdistance` |
| `distancefog`                                                          | `r_distancefog`  |
| `fogdistance`, `fogdist`                                               | `r_fogdistance`  |
| `fogstart`                                                             | `r_fogstart`     |
| `fogend`                                                               | `r_fogend`       |
| `fogdensity`                                                           | `r_fogdensity`   |
| `fogcolor`, `fogcolour`, `r_fogcolour`                                 | `r_fogcolor`     |
| `r_ambient`                                                            | `ambient`        |

## Debugging

| Command         | Description                    |
| --------------- | ------------------------------ |
| `sg`            | Toggle spatial-grid debugging  |
| `showcollision` | Show collision geometry        |
| `collisionvis`  | Alias for `showcollision`      |
| `collision`     | Alias for `showcollision`      |
| `fps`           | Toggle/display FPS information |
| `clear`         | Clear the debug console        |

## Portals

| Command          | Description       |
| ---------------- | ----------------- |
| `portal_list`    | List portals      |
| `portal_create`  | Create a portal   |
| `portal_link`    | Link portals      |
| `portal_color`   | Set portal colour |
| `portal_enable`  | Enable a portal   |
| `portal_disable` | Disable a portal  |
| `portal_delete`  | Delete a portal   |

## Plugin Commands

The native command handler also provides an extension point for plugins.

Plugins can register their own console commands through the Fio plugin API. When the console receives an unknown command, it checks the plugin manager before reporting the command as unknown.

Therefore:

> **The list above is the complete built-in command set, but not necessarily the complete command set of a Fio installation.**

Installed plugins may add additional commands at runtime.
