> Applies to Fio 3.5.0.1111.

Tidy is a shipped plugin (`plugins/tidy/`, plugin name `tidy`, version 2.0.0) that adds put-it-away gameplay: the player carries ordinary Props to a receptacle, which arranges them into slots, and a goal counts progress and fires an output when the target is reached. Tidy owns no carryable object and no physics. A tidyable object is a core [Prop](https://github.com/ViciousSquid/Fio/wiki/Prop) with a non-empty `tidy_category`; the engine's `PropSession` (`engine/prop_runtime.py`) still does the carrying, dropping and simulation, and Tidy only intercepts the drop.

Tidy is written against the [Plugin API](https://github.com/ViciousSquid/Fio/wiki/Plugin-API) and is the smallest complete example of a gameplay plugin in the repository.

## Using it

1. **Plugins > Tidy > Load Demo map** loads `plugins/tidy/Tidy_Test.json` (after the unsaved-changes check).
2. Enter Play. Look at a book and press Use (**E**) to pick it up; aim at the shelf from within its reach and press **E** again to put it away.
3. The HUD line reads `Tidied: N/M`, and `Tidied: N/M  — All done!` once the goal completes.

To build your own:

1. Place a normal Prop and set `tidy_category` (the **Tidy category** field Tidy adds to the Prop panel) to any non-empty text, for example `book`.
2. Place **Tidy Receptacle (shelf/bin)** where the objects should line up, usually just above a shelf or table. It is an anchor, not geometry.
3. Optionally place a **Tidy Goal** and connect its `OnComplete` output to a door, a LevelChanger or anything else with an input.

## Activation

The plugin class sets `enabled = False`. It is switched on for a map by `PluginManager.auto_enable_for_map` when either of these is true:

- the map contains a `tidyreceptacle` or `tidygoal` entity (the plugin owns those types), or
- `TidyPlugin.map_uses_plugin(map_data)` finds a `prop` (or legacy `model`-free `prop` record) whose `properties["tidy_category"]` is non-empty after stripping.

A map of plain Props never enables Tidy. Clearing the scene reverts the auto-enable. It can also be enabled by hand from **Plugins > Tidy > Enabled**.

## Entities

### Prop (core) with `tidy_category`

| Property | Type | Default | Meaning |
| --- | --- | --- | --- |
| `tidy_category` | string | `""` | Non-empty: this Prop is a Tidy object of that category. Registered with `register_extra_fields("prop", ...)` under the group **Tidy**. |

While a Prop is stowed, Tidy writes `_tidy_previous_carry_enabled` (the Prop's `carry_enabled` before stowing) and sets `carry_enabled = False`. Both are restored when the Prop is reset or Play stops.

### `tidyreceptacle` — Tidy Receptacle

Class `plugins/tidy/entities.py: TidyReceptacle`. 2D icon `plugins/tidy/assets/tidyreceptacle.png`.

| Property | Type | Default | Range | Meaning |
| --- | --- | --- | --- | --- |
| `accepts` | string | `"any"` | | Category accepted. `""`, `any` and `*` accept everything; otherwise compared with the Prop's category case-insensitively. |
| `capacity` | int | `24` | 1–100000 | Maximum number of Props held. A full receptacle is not a placement target. |
| `slot_cols` | int | `6` | 1–1000 | Slots per row. |
| `slot_spacing` | vec3 | `[28.0, 40.0, 0.0]` | | Step between slots: `x` between columns, `y` and `z` between rows. |
| `slot_offset` | vec3 | `[0.0, 0.0, 0.0]` | | Offset of the slot grid from the receptacle's position. |
| `reach` | float | `140.0` | 1–10000 | Maximum distance from the player's eye to the receptacle for a placement. |
| `disabled` | bool | `False` | | Refuses new objects. Set by the `Enable` / `Disable` inputs. |

Slot `index` (0-based, in placement order) is at:

```text
col = index % slot_cols          row = index // slot_cols
x = pos.x + slot_offset.x + (col - (slot_cols - 1) / 2) * slot_spacing.x
y = pos.y + slot_offset.y + row * slot_spacing.y
z = pos.z + slot_offset.z + row * slot_spacing.z
```

Rows are centred on the receptacle in X. The index of a new object is the receptacle's current fill count, so after a single object is reset its slot is reused by the next placement only if it was the last one.

### `tidygoal` — Tidy Goal

Class `plugins/tidy/entities.py: TidyGoal`. 2D icon `plugins/tidy/assets/tidygoal.png`.

| Property | Type | Default | Meaning |
| --- | --- | --- | --- |
| `target` | string | `"all"` | `all`, `""` or `*`: every matching object. A number: that many, clamped to `0..matching total` (when the map has no matching object the number is used as it is). Unparseable text counts as `all`. |
| `category` | string | `"any"` | `""`, `any` or `*`: every Tidy object. Otherwise only that category. |
| `show_hud` | bool | `True` | Show this goal's counter on the HUD line. |
| `disabled` | bool | `False` | Stop counting and stop firing outputs. Set by the `Enable` / `Disable` inputs. |

The goal's category is lower-cased before it is looked up, while object categories are counted exactly as typed (stripped only). A goal therefore only matches categories written in lower case: `category = book` counts Props with `tidy_category = book`, and a Prop with `tidy_category = Book` is counted by no category-specific goal. Receptacle matching is case-insensitive.

## I/O

| Entity | Inputs | Outputs |
| --- | --- | --- |
| `prop` (added with `extend_io`, alongside the core Prop I/O) | `Reset` — return this Tidy Prop to its authored position | `OnTidied` — fired when this Prop is put away |
| `tidyreceptacle` | `Reset` — send every held Prop home; `Enable`; `Disable` | `OnObjectPlaced` (int: the receptacle's new count); `OnFull` (once each time capacity is reached) |
| `tidygoal` | `Enable`; `Disable` | `OnProgress` (string `done/need`); `OnComplete` (once per Play session) |

`OnProgress` is fired for every goal that is neither complete nor disabled each time any object is stowed or reset. `OnFull` can fire again after a reset empties a slot.

`Reset` on a Prop that is not a Tidy object does nothing. For a Tidy object it releases it if held, removes it from its receptacle's count, restores `carry_enabled`, moves it to the Prop session's recorded home position (`_prop_home_pos`), and hands it back to physics awake.

## Runtime

`plugins/tidy/plugin.py: TidyPlugin` keeps one `TidySession` (`plugins/tidy/runtime.py`) per logic thread in a `WeakKeyDictionary`.

| Hook | What it does |
| --- | --- |
| `register` | Menu action **Load Demo map**; the `tidy_category` extra field on `prop`; the two entities, their schemas and I/O; `extend_io("prop", ...)`. |
| `register_runtime` | Input handlers: `prop.reset`, `tidyreceptacle.reset/enable/disable`, `tidygoal.enable/disable`. |
| `on_play_start` | Builds the session: collects Tidy Props, receptacles and goals from `logic.editor_state.things`, counts totals per category. Installs `logic.prop_runtime.drop_interceptor`, chaining to any interceptor already there. |
| `on_tick` | HUD line (below). |
| `on_play_stop` | Restores `carry_enabled` on every Tidy Prop and puts the previous drop interceptor back. Positions are restored by the core Prop session. |

**Placement.** When the player presses Use while carrying a Prop, `PropSession` calls the drop interceptor before dropping. `TidySession.consume_drop(prop)` looks for the nearest receptacle that is enabled, accepts the category, is not full, lies within its own `reach` of the player's eye, and whose direction from the eye has a dot product of at least `PLACE_AIM_DOT = 0.55` with the look vector. With one found, `_place`:

1. clears `prop_runtime.held`;
2. moves the Prop to the next slot and calls `prop_runtime.moved(prop)`;
3. stashes and clears `carry_enabled`, and makes the physics body kinematic (`physics_world.set_kinematic(prop, True)`);
4. fires `OnTidied` on the Prop, `OnObjectPlaced` on the receptacle, `OnFull` if it just filled, then re-evaluates the goals.

With none found the interceptor returns false and the core drop happens as usual. The receptacle test is a cone on positions: geometry between the player and the receptacle does not block it.

**HUD.** Each tick, if the held Prop is a Tidy object and a valid receptacle is in view, the prompt is `[E] Put away (<receptacle name>)`. Otherwise, when nothing else has set the HUD line, it shows the first goal that has `show_hud` and is not disabled as `Tidied: done/need`; with no such goal and at least one Tidy object, `Tidied: tidied/total`.

**Saved data.** Tidy stores nothing beyond the entity properties in the tables above. Session state (fill counts, completed goals) is per Play session and is not part of a save game.

## Data files

| Path | What |
| --- | --- |
| `plugins/tidy/Tidy_Test.json` | The demo map: 7 brushes, 42 Props with `tidy_category = book`, one receptacle named `Shelf` (`capacity` 48, `slot_cols` 12, `slot_spacing` `[30, 44, 0]`, `slot_offset` `[0, 6, 0]`, `reach` 180), one goal (`target = all`) with one output connection, a PlayerStart and a light. |
| `plugins/tidy/assets/book.obj`, `book.mtl`, `covers/cover_01.png` … `cover_12.png` | The book model and its twelve covers (`entities.py: BOOK_MODEL`, `COVERS`). |
| `plugins/tidy/assets/tidyreceptacle.png`, `tidygoal.png` | Editor icons. |
| `plugins/tidy/tools/make_example_map.py` | Regenerates the demo map (`QT_QPA_PLATFORM=offscreen python plugins/tidy/tools/make_example_map.py`). |
| `plugins/tidy/tools/make_books.py`, `make_sprites.py` | Regenerate the book assets and the two icons. |

In the [Android player](https://github.com/ViciousSquid/Fio/wiki/Fio-Player-(Android)) the book model and covers are read from the installed plugin directory (`player/runtime/plugin_assets.py`), because a `.fiopak` cannot carry anything under `plugins/`. The **Load Demo map** action is an editor command: export the demo from desktop Fio to play it on a device.

## Module map

| Module | Contents |
| --- | --- |
| `plugins/tidy/__init__.py` | Exposes `PLUGIN`. |
| `plugins/tidy/plugin.py` | `TidyPlugin`: registration, activation rule, input handlers, drop interceptor, lifecycle. |
| `plugins/tidy/entities.py` | `TidyReceptacle` (`accepts_category`, `slot_world_pos`), `TidyGoal` (`target_count`, `matches`), asset constants. |
| `plugins/tidy/runtime.py` | `TidySession`: `start`, `stop`, `consume_drop`, `reset_object`, `reset_receptacle`, `tick`, `hud_line`; `PLACE_AIM_DOT`. |

## Tests

`plugins/tidy/tests/` (headless):

| File | Covers |
| --- | --- |
| `test_smoke.py` | Loading and registration, the demo loader honouring unsaved changes, core Prop carry followed by a Tidy placement, slot positions and category filtering, plain Props being ignored. |
| `test_auto_enable.py` | Disabled by default, auto-enable for Tidy maps only, the revert on scene clear, auto-enable through `PlayerPluginHost`. |
| `test_engine_integration.py` | Input handlers attaching while the plugin is disabled, the tick cache and early-out, hook-override detection. |
| `test_packaging.py` | Required-plugin resolution, a `.fiopak` with a `plugins/` payload being rejected, plugin names in package metadata being allowed. |
| `test_player.py` | Loading without the editor or PyQt; a core Prop and Tidy session run through `PlayerPluginHost`. |
