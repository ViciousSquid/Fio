> Applies to Fio 3.1.0.0611 (CANARY).

<img width="50" height="50" alt="image" src="https://github.com/user-attachments/assets/cd16b58e-d215-4af0-8e57-8ae26089c443" />

# Light

A `Light` is a dynamic point light: a position, a colour, an intensity and a radius. Lights are evaluated per frame by the renderer (there are no lightmaps), can cast cube-map shadows, can be switched, recoloured and faded through the [I/O system](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System), and can be attached to a mover brush.

| | |
| :--- | :--- |
| Class | `editor/things.py: Light` |
| `type` in the map | `light` |
| Render projection | `engine/entity_table.py: EntityTable` columns `light_color` (3 × float32), `light_params` (intensity, radius), `light_enabled`, `light_casts_shadows` |
| Light selection per frame | `engine/renderer/core/visibility.py: _get_active_lights`, `engine/renderer/core/light_stream.py: LightStream` |
| Upload and shadows | `engine/renderer/forward/lighting.py` |
| Input handlers | `editor/io_handlers.py` (`light_*`), logic thread |
| Fades | `engine/logic_timing.py: LogicTiming.start_light_fade`, `update_light_fades` |
| Mover parenting | `engine/logic_parenting.py: LogicParenting._init_parented_lights`, `_update_parented_lights` |
| Editor icon | `assets/sprites/light.png` |
| Place it | 2D view right-click → **Light** |

## Properties

| Key | Type | Default | Editor | Meaning |
| :--- | :--- | :--- | :--- | :--- |
| `colour` | `[r, g, b]`, 0–255 | `[255, 255, 255]` | colour picker | Light colour. The renderer divides by 255 and clamps each channel to 0–1; a malformed value is read as white. The key is spelled `colour` (a stray `color` key, present in some shipped maps, is ignored) |
| `intensity` | float | `1.0` | *Intensity* spin box | Brightness multiplier. `SetBrightness` clamps to 0–10 and Ctrl+wheel to 0.1–10; the spin box itself does not clamp |
| `radius` | float, world units | `512.0` | *Radius* spin box | Distance at which the light's contribution reaches zero |
| `state` | string | `"on"` | *Enabled* checkbox | `"on"` or `"off"`. The renderer treats the strings `1`, `true`, `yes`, `on` (any case) and boolean `true` as on, everything else as off |
| `show_radius` | bool | `false` | *Show Radius* checkbox | Draw the radius circle in the 2D views (editor only). Legacy string values `"True"` / `"False"` are accepted |
| `casts_shadows` | bool | `false` | *Casts Shadows* checkbox | Render depth cube-map shadows from this light. A string value is read with the same truthy words as `state` |
| `parent_mover` | string | `""` | *Attach to Mover* checkbox + *Parent Mover* list | Name of a mover brush the light follows during Play |
| `parent_offset` | `[x, y, z]` | `[0.0, 0.0, 0.0]` | — | Offset from the mover's centre. When empty or all zero at Play start it is computed from the light's current position and written back |
| `hidden` | bool | absent | — | Set by `Hide` / `Show` / `ToggleVisibility` / `Kill`. A hidden light does not light or take a shadow slot **in Play**; in the editor it still lights |
| `name`, `id`, `io_enabled` | | | | Common to all Things |

Editor shortcuts with a Light selected in a 2D view: **Shift + wheel** changes `radius` in steps of 32 (minimum 32); **Ctrl + wheel** changes `intensity` in steps of 0.1 (0.1–10).

`Light.EDITOR_PRIMARY_PROPERTIES` also lists `shadow_map_size` (spin box 64–4096, step 64), but the class does not create that key, so the row only appears on a light whose record already contains it, and no runtime code reads a per-light value. The shadow resolution is global: `settings.ini` `[Renderer] shadow_map_size`, default `384` (`Lighting.SHADOW_MAP_SIZE`).

## Inputs

| Input | Parameter | Effect |
| :--- | :--- | :--- |
| `TurnOn` | — | `state = "on"`; fires `OnTurnedOn` (every time, even if already on) |
| `TurnOff` | — | `state = "off"`; fires `OnTurnedOff` (every time) |
| `Toggle` | — | `TurnOff` if `state == "on"`, otherwise `TurnOn` |
| `SetBrightness` | float; blank = `1.0` | `intensity`, clamped to 0–10. `nan`, `inf` and non-numbers are ignored |
| `SetColor` | `R G B` (space-separated integers) | `colour = [r, g, b]`. Fewer than three values or a non-integer is ignored. Not clamped when stored |
| `EnableShadows` | — | `casts_shadows = true`; fires `OnShadowsEnabled` |
| `DisableShadows` | — | `casts_shadows = false`; fires `OnShadowsDisabled` |
| `ToggleShadows` | — | Whichever of the two applies |
| `FadeIn` | seconds; blank = `1.0` | Fades `intensity` up to the light's nominal intensity and sets `state = "on"`; fires `OnTurnedOn` immediately. A light that was off starts from 0 |
| `FadeOut` | seconds; blank = `1.0` | Fades `intensity` down to 0, then sets `state = "off"` and fires `OnTurnedOff` |
| `Hide` / `Show` / `ToggleVisibility` | — | Set / clear / flip `hidden` |
| `Kill`, `Enable`, `Disable`, `SetTint`, `ClearTint` | | Generic inputs. `Kill` sets `hidden` and `disabled`; `disabled` and `tint` have no effect on a light |

## Outputs

| Output | Fired when |
| :--- | :--- |
| `OnTurnedOn` | `TurnOn`, a `Toggle` that turns it on, or `FadeIn` (at the start of the fade) |
| `OnTurnedOff` | `TurnOff`, a `Toggle` that turns it off, or a `FadeOut` completing |
| `OnShadowsEnabled` | `EnableShadows`, or `ToggleShadows` enabling |
| `OnShadowsDisabled` | `DisableShadows`, or `ToggleShadows` disabling |

## Runtime behaviour

**Fades.** The *nominal intensity* is captured the first time `FadeIn` or `FadeOut` runs on a light in a play session: its current `intensity`, or `1.0` if that is 0. It is held on the instance (`_fade_nominal`), never saved, and cleared at Play start. `LogicTiming.update_light_fades` interpolates `intensity` linearly each logic tick. A duration of 0 (or a negative one) applies the end state at once; in that case `FadeOut` does **not** fire `OnTurnedOff`. Starting a new fade replaces the one in progress and continues from the current intensity. Fades are cleared when Play starts and stops.

After a `FadeOut` the light's `intensity` is 0. `TurnOn` only sets `state`, so it will not bring the light back; use `FadeIn` (a duration of `0` is instant) or `SetBrightness`.

**Which lights are drawn.** Lights and light-emitting Effects share one list (`EntityTable.light_slots`). A light contributes when its `state` is on and, in Play, it is not hidden.

* In the editor every such light is submitted.
* In Play only lights within `ACTIVE_LIGHT_RANGE` = 2048 units of the player are used, nearest first (`LightStream`), so a map may contain more lights than the shaders can evaluate. A lit light stays lit until it is 2304 units away (`DEACTIVATION_RANGE`, hysteresis), and its rendered intensity eases to zero over the last 256 units inside the activation boundary; the authored `intensity` is not changed.
* Shader capacity is `MAX_LIGHTS` = 64 per frame (`engine/shaders.py`); water and terrain shaders use the nearest 8 (`MAX_LIGHTS_WATER`, `MAX_LIGHTS_TERRAIN`), and the main brush shaders 16 (`MAX_LIGHTS_ARM`) when the renderer is in low-power mode.
* Shadows: of the active lights with `casts_shadows`, the `MAX_SHADOW_LIGHTS` = 8 nearest the camera get a depth cube-map each.

**Attached lights.** At Play start each light with a non-empty `parent_mover` is bound to the first mover brush (`is_mover`) of that name; a missing mover prints a warning and the light stays where it is. Each logic tick the light is moved to the mover's centre plus `parent_offset`. When Play stops the light returns to its authored position. `maps/dLight_Test.json` and `maps/dLight2_Test.json` are worked examples.

**Persistence.** I/O inputs write the authored properties (`state`, `intensity`, `colour`, `casts_shadows`). Unless *Settings → Play Modes → Restore the world when leaving Play* is on, the editor keeps those values after Play and a subsequent save writes them.

## In the map file

From `maps/Light_Test.json`:

```json
{
  "type": "light",
  "pos": [288.0, 272.0, -48.0],
  "properties": {
    "type": "light", "name": "Light_1",
    "id": "3d80e810-1675-4595-913a-4a59539733cc",
    "colour": [255, 255, 255], "intensity": 1.0, "radius": 1280.0,
    "state": "on", "show_radius": false, "casts_shadows": true,
    "parent_mover": "", "parent_offset": [0.0, 0.0, 0.0]
  },
  "io_connections": []
}
```

## Known inconsistencies

* The Property editor's *Enabled* checkbox reads `state` with `bool(value)`, so it shows as checked for both `"on"` and `"off"`, and it writes a boolean (`true` / `false`) rather than the string. The renderer accepts the boolean, but the `Toggle` handler compares against the string `"on"`: on a light whose `state` is boolean `true`, the first `Toggle` calls `TurnOn` (no visible change) and only the second turns it off; `FadeIn` likewise treats it as off and restarts from black.
* `shadow_map_size` is listed as an editor property but is neither created by default nor read per light (see above).

## Tests

`tests/renderer/test_light_budget.py` (shader light caps), `tests/renderer/test_light_fade.py` (range fade and streaming), `tests/renderer/test_entity_table.py`, `tests/io/test_io_contract.py`, `tests/console/test_console_undo_and_io.py`.

## See also

[Entities (Things)](https://github.com/ViciousSquid/Fio/wiki/Entities-(Things)) · [Triggers & The I/O Logic System](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System) · [EntityTable](https://github.com/ViciousSquid/Fio/wiki/EntityTable) · [Renderer Technical overview](https://github.com/ViciousSquid/Fio/wiki/Renderer-Technical-overview)
