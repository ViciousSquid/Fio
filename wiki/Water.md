> Applies to Fio 3.5.0.1611.

# Water

A water brush is a brush the player swims in, drawn with moving waves, a tinted and refracted view of what lies under it, and reflections. Each water brush is either **high quality** (the default) or **cheap**. Both reflect the map's sky when it has one; water inside a [Reflection Probe](https://github.com/ViciousSquid/Fio/wiki/Reflection-Probe)'s radius reflects the probe's snapshot of the room instead.

| | |
| :--- | :--- |
| What makes a brush water | `is_water: true`, or `shader: "Water"`, or any face textured with a water texture — `engine/constants.py: is_water_brush`, which the renderer, physics and AI all use, so a brush can never look like water but collide like a solid |
| Make one | Select a brush → [Property editor](https://github.com/ViciousSquid/Fio/wiki/Property-editor) → **Shader: Water**. Its settings appear under **Water Properties**, filled with the defaults below |
| Quality flag | `engine/constants.py: water_high_quality` (default on) |
| Render columns | `engine/render_table.py`: `water_tint`, `water_params`, `water_plane`, `water_wave_spread`, `water_high_quality` |
| Pass | `engine/renderer/forward/passes.py: draw_water_brushes` — one draw per brush, blended, after the opaque world |
| Shaders | `engine/shaders.py`: `water.vert`, `water.frag` — shared with the [Android player](https://github.com/ViciousSquid/Fio/wiki/Fio-Player-(Android)) |
| Benchmark | `tools/bench_water.py` (see *Measuring*) |
| Worked examples | `maps/DevTest.json`, `maps/MonsterTest.json`, `maps/_SHOWCASE.json` (one water brush each) |

## Properties

| Key | Type | Default | Editor | Meaning |
| :--- | :--- | :--- | :--- | :--- |
| `water_high_quality` | bool | `true` | *High quality* checkbox | See *High quality and cheap*. This brush only |
| `water_plane` | bool | `false` | *Draw top surface only* checkbox | Draw the top surface alone, for water whose sides are buried in the floor and walls. Off, the four sides are drawn too |
| `water_tint` | RGB 0–1 | `[0.0, 0.4, 0.6]` | *Water Tint* colour button | Colour of the body of water |
| `water_opacity` | float 0–1 | `0.5` | *Opacity* slider | How much the water's own colour covers the view through it |
| `water_distortion` | float 0–1 | `0.5` | *Warp Strength* slider | How much the waves warp the view through the water |
| `water_refraction` | float 1.00–2.50 | `1.333` | *Refraction* slider | Index of refraction: 1.00 air, 1.333 water, 1.50 glass |
| `water_roughness` | float 0–1 | `0.0` | *Roughness* slider | 0 is clear. Higher values blur the reflection of the world towards the sky |
| `water_wave_enabled` | bool | `true` | *Enable Waves* checkbox | Off leaves a gentle 1.2-unit ripple, not a dead-flat surface |
| `water_wave_height` | float 0–1 | `0.5` | *Wave Height* slider | Wave amplitude: `height × 30` units, never more than 30 units or 45 % of the brush's own height. Values above 2 (older maps) are read as percentages |
| `water_wave_spread` | float 0–1 | `1.0` | *Wave Spacing* slider | Left: tight, choppy water. Right: a broad, calm swell |
| `water_fresnel` | float 0–1 | `0.65` | set when the shader is chosen; not on the panel | How strongly the surface reflects, most at grazing angles. `water_reflectivity` is the older name, read when `water_fresnel` is absent |

## High quality and cheap

| | High quality | Cheap |
| :--- | :---: | :---: |
| Waves, refraction of what is under the surface, tint, lighting (up to 8 lights) | ✓ | ✓ |
| The sky (or a [Reflection Probe](https://github.com/ViciousSquid/Fio/wiki/Reflection-Probe)'s snapshot) reflected | ✓ | ✓ |
| Depth-based colour: deeper water reads darker and more tinted | ✓ | |
| Shoreline foam where the water meets geometry | ✓ | |
| Caustics on what is under the surface | ✓ | |
| Reflections of the world — walls, props, monsters, the player | ✓ | |
| Fires and other effects above the water reflected | ✓ | |

High quality copies the frame's depth once per frame (when any high-quality water is drawn) and traces a reflection ray per pixel of the top surface; cheap water does neither. Quality is chosen per brush, so a map can mix both — a decorative fountain cheap, the main pool high quality.

Two [console commands](https://github.com/ViciousSquid/Fio/wiki/Console-commands) override it for the session:

| Command | Effect |
| :--- | :--- |
| `r_waterquality cheap` (`waterquality`) | Every water brush is drawn cheap. `expensive` gives the choice back to each brush; no argument toggles |
| `reflections 0` | Screen-space reflections off — water and wet decals. `reflections 1` turns them back on |

## What the water reflects

For each pixel, in this order:

1. **The world** (high quality, top surface only). The reflected ray is traced through this frame's depth; where it lands on something on screen, that is what the water shows — live, so a door that opens or a monster walking past is reflected as it happens. Nothing is baked and nothing has to be placed.
2. **Everywhere else, the sky behind it:**
   1. inside a [Reflection Probe](https://github.com/ViciousSquid/Fio/wiki/Reflection-Probe)'s radius: the probe's snapshot of its surroundings;
   2. otherwise the map's sky image (see *The sky*);
   3. otherwise the built-in procedural sky.

The world reflection can show only what is on screen. Whatever is behind the camera, or above the top edge of the view, cannot be reflected, and the water shows the sky there instead. Outdoors that is what you expect. **Indoors, in a map with a sky, a pool can show sky where the ceiling should be** — place a [Reflection Probe](https://github.com/ViciousSquid/Fio/wiki/Reflection-Probe) in the room.

### Why Fio needs no cubemap above every pool

Source places `env_cubemap` entities so its compiler can photograph each spot (`buildcubemaps`), because shiny surfaces and cheap water there reflect the nearest photograph. Fio does not need one by default:

- **The sky needs no placement.** It is infinitely far away, so it looks the same from everywhere in the map; one sky image is right for all water. The texel each water vertex reflects is worked out per vertex, with the same mapping the sky pass uses (`panoramaUV`), so a pixel reads one texel with no trigonometry and the reflection lines up with the sky. Custom skies work the same way.
- **Nearby things come from the frame itself** (step 1 above), with no bake to go stale.

A probe covers the one case neither handles: water under a roof.

## The sky

The map's sky is the **Sky image** of the primary [Player Start](https://github.com/ViciousSquid/Fio/wiki/Player-Start) (`sky_image`): any equirectangular image (PNG, JPEG, WebP, …). Fio ships `assets/backgrounds/cloudy-sky-panorama.webp` and `assets/backgrounds/void-panorama-q80.webp`; a map without one shows the clear colour behind the world, and its water reflects the procedural sky.

The sky behind the world is drawn from a cube map made once when the image loads — a single texture read per pixel — with `settings.ini` `[Renderer] sky_cubemap_size` texels per face (default `384`, clamped to 64–2048). It is drawn after the opaque world, depth-tested at the far plane, so pixels the world covers never shade it. The water reads the original image, not the cube.

## Performance

- One draw per water brush.
- High-quality box water does not shade the side-wall pixels its own top surface hides (39 % of the water's fragments looking down into DevTest's pool). They write alpha 0 rather than `discard`, so the water keeps early depth testing.
- Caustics and foam are worked out on the top surface only, where they show.
- The sky reflection, and a probe's, cost one texture read per pixel.
- Choose cheap for water the player never looks at closely; it skips the depth copy and the reflection trace.

### Measuring

`tools/bench_water.py` draws fixed scenes from fixed cameras at a fixed water clock, and reports what the water pass costs, the fragments it shades and its overdraw:

```
xvfb-run -a python tools/bench_water.py --scenes devtest,monster,showcase,pool --sky cloudy --quality expensive
xvfb-run -a python tools/bench_water.py --images before/          # one PNG per camera
xvfb-run -a python tools/bench_water.py --diff before/            # compare against them
```

`--sky` is `cloudy`, `space` or `none`; `--quality cheap` caps every brush cheap, as `r_waterquality cheap` does.

## In the map file

From `maps/DevTest.json` (texture keys trimmed):

```json
{
  "id": "554437a8-e828-4992-8fed-993fcbb506fe",
  "pos": [0.0, 96.0, -360.0],
  "size": [768.0, 320.0, 208.0],
  "shader": "Water",
  "water_opacity": 0.5,
  "water_reflectivity": 0.5,
  "water_tint": [0.0, 0.4, 0.6],
  "water_wave_enabled": true,
  "water_wave_height": 0.6,
  "water_plane": false
}
```

Keys left out take the defaults in the table above; `water_high_quality` absent means on.

## Tests

`tests/visual/test_water_quality.py`, `tests/visual/test_water_sky_reflection_gl.py` (the sky reflected by both qualities, custom skies, the covered-wall skip changing no pixel), `tests/renderer/test_water_sky_reflection.py` (the shader contract), `tests/visual/test_reflection_probe_gl.py`, `tests/visual/test_sky_panorama_gl.py`, `tests/editor/test_water_quality_checkbox.py`, `tests/renderer/test_shader_portability.py` (every shader compiled for desktop GL and GLES).

## See also

[Reflection Probe](https://github.com/ViciousSquid/Fio/wiki/Reflection-Probe) · [Basic concept: Brushes](https://github.com/ViciousSquid/Fio/wiki/Basic-concept:--Brushes) · [Player Start](https://github.com/ViciousSquid/Fio/wiki/Player-Start) · [Renderer Technical overview](https://github.com/ViciousSquid/Fio/wiki/Renderer-Technical-overview) · [Console commands](https://github.com/ViciousSquid/Fio/wiki/Console-commands) · [Physics](https://github.com/ViciousSquid/Fio/wiki/Physics)
