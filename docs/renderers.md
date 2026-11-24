# Fio's renderers: Classic and HD_High

Fio draws through a renderer contract. The host (`QtGameView`) builds a frame
input from the dense tables the logic thread publishes and hands it to
whichever renderer is active; it knows a renderer only by the members of
`engine.renderer.api.Renderer`. Renderers are registered by name
(`engine/renderer/registry.py`) and can be replaced while Fio runs.

Two renderers ship with Fio. They are separate packages that share only
`engine/renderer/core` (GPU resources, culling, terrain upkeep); neither
imports the other.

| | Classic | HD_High |
| --- | --- | --- |
| Package | `engine/renderer/forward` | `engine/renderer/hd_high` |
| Class | `ForwardRenderer` | `HDHighRenderer` |
| Registry name | `Forward` (alias `Classic`) | `HD_High` |
| Shipped `settings.ini` starts with | | yes |
| Used when no renderer is set, or HD_High cannot start | yes | |
| Low-power ARM mode | yes (its reduced shaders) | never chosen |
| Android player | yes | no |

## Choosing a renderer

* **Settings > Display > Renderer**: Classic or HD_High. Applied at once.
* **`settings.ini`**: `[Renderer] renderer = Classic` or `HD_High`. This is the
  renderer the editor starts with. Any other value, or none, is Classic.
* **Console**: `r_renderer` lists the registered renderers; `r_renderer Classic`
  and `r_renderer HD_High` switch, in the editor or in Play.
* **In code**: `view.switch_renderer("HD_High")`; `create_renderer(name, config)`.

Swapping needs no restart: the frame input is rebuilt every frame, the new
renderer is created before the old one is retired, and a renderer that cannot
start leaves the current one running. If HD_High cannot start at launch, Fio
starts Classic.

---

## Classic

Fio's original renderer, and the one every map was built against. OpenGL 3.3
core, designed for low-power hardware.

**Lighting.** Each lit surface is its texture multiplied by light, in display
space: `(0.1 + ambient) + sum over lamps of intensity * falloff * N.L * colour`,
with `falloff = (1 - distance / radius)^2`. Up to 128 lamps per frame (fewer in
low-power mode), the eight nearest shadow-casting ones with depth cube-map
shadows, light cookies, emissive brushes as lights. Terrain has a fixed sun
and sky tint of its own.

**What it draws.** Textured, Solid Lit, Wireframe and Overlay display modes;
box and convex brushes (instanced and batched); OBJ and GLB models; sprites,
monsters, effects and particles; projected decals (wet ones reflect); water
(cheap, or with depth, caustics and screen-space reflections); glass; fog
volumes and distance fog; a sky panorama; stencil world portals; reflection
probes; the terrain and its grass; the editor's overlays.

**Settings** (`[Renderer]`): `shadows_enabled`, `shadow_map_size`,
`sky_cubemap_size`, `occlusion_culling`, `lowpower_mode`, `geo_batching`.

**Locked.** Classic must not change because HD_High exists:

* `tests/visual/test_classic_golden.py` draws the stock maps from fixed
  cameras, in the editor and in Play, and compares every pixel with frames
  stored before HD_High existed, and every program's final fragment source
  with the stored text.
* `tests/visual/test_classic_lock.py` checks the digest of every file Classic
  is made of (`forward/`, `core/`, `engine/shaders.py`), that none of them
  mentions HD_High, that importing or drawing with Classic touches nothing of
  HD_High's, and that Classic is the default.
* `tools/hd_golden.py --check` runs the comparison by hand; `--base DIR`
  compares this checkout with another on whatever GPU is present; after a
  *deliberate* change to Classic, `--update` stores new frames and
  `--lock-sources` records the new digests.

---

## HD_High

A physically based renderer. It draws everything Classic draws (its passes
began as a copy of Classic's) and replaces how surfaces are lit and how the
frame is finished.

### The frame

1. **Sun shadow map** (when the map has a sun): the casters that stand still
   in a depth map of their own, redrawn only when one changes; movers, doors
   and models over a copy of it. A filtered 2048 map; in a world larger than
   the shadow range it follows the view in coarse steps.
2. **Sky cover map** (when the map has a sky): the standing casters seen from
   straight above. Surfaces read from it whether the sky is overhead.
3. The frame is drawn into a **floating-point target**: colour, the shading
   normal with sky visibility and roughness, depth.
4. **Opaque world**, physically shaded (`surface.py`): albedo decoded to
   linear light, GGX specular and energy-conserving diffuse, roughness /
   metalness / ambient occlusion / emission from material maps, normal
   mapping and parallax with the tangent frame taken from screen-space
   derivatives (no mesh needs tangents), the sun with shadow and cloud
   shadows, sky light, the map's lamps with their cube-map shadows, haze or
   fog.
5. **Forward+**: with 10 or more lamps in a frame they are binned on the CPU
   into world-space clusters (32 x 32 over the floor plan, 4 layers high) and
   a fragment loops over its cluster's lamps only. The clusters are of the
   world, not the view, so they are rebuilt when lamps change, not when the
   camera moves. With fewer lamps, or a cluster more lamps reach than its
   list holds, every lamp is tested, as in Classic.
6. **Ambient occlusion** from the depth buffer and **reflections**: the frame
   itself where a reflected ray finds it (surfaces smooth enough to show a
   picture), otherwise the nearest ReflectionProbe, else the sky, else the
   map's ambient.
7. **Blended things** (sprites, particles, effects, water, glass) over the
   result. These are not physically shaded: each keeps the colour it has in
   Classic, converted so that the tone curve brings it back (`hdr.py`).
8. **Bloom, sun shafts, exposure, tone mapping** (an extended Reinhard curve:
   straight through the shadows, so a low-lit map stays as dark as it was
   lit and no darker), **grade, vignette, grain, FXAA**, into the
   framebuffer the view asked for.

### Light, by kind of map

* **No sky** (`hd_sky = none`, the default without a sky image). Lit by the
  map's lamps and ambient and nothing else. A lamp's level
  (`intensity * falloff`) is decoded like a colour, so light falls off across
  a room exactly as Classic shows it; the exposure is held within a quarter
  either way of the value at which a fully lit texture shows at its own
  brightness. The map keeps its mood.
* **Panorama sky** (the default with a sky image). The sky image is drawn as
  the background, as in Classic, and is also the light: it is measured on the
  CPU when it loads and lights what lies under open sky as daylight. The sun
  is off unless the author switches it on and aims it (a panorama has no
  known sun).
* **Procedural sky.** An analytic atmosphere under a moving cloud layer, a sun
  on by default, cloud shadows, haze.

Under any sky, **sky light lands only where the sky is overhead**, and the
**exposure follows the view**: an outdoor value from the sky's and sun's
light, an indoor value from the lamps, and between them by how much sky is
over the view point (rays against the casters' boxes on the CPU), eased over
about a second. Nothing is read back from the GPU for any of this.

A map with **distance fog** switched on is fogged by it; the sky's haze is
then left out. **Terrain** under a sky is shaded like any surface, by the
frame's sun or, where the sky has none, by the fixed sun Classic lights
terrain with; without a sky, and in the stylised terrain looks, it is drawn
as Classic draws it. **Portal views** are shaded inside the HD frame.
**Reflection probes** are captured with the classic shading this renderer
still carries, and read as display pictures.

### What the map says

The `hd_*` properties of the primary PlayerStart (the **HD Sky** section of
its property page; listed in the README). They are resolved like `sky_image`:
the primary's value wins, a value left on another start by an older map is
recovered, and the first edit moves everything to the primary. Classic keeps
them with the map and never reads them. Nothing about the look is saved in a
save game.

### Material maps

Every physically shaded surface has a normal/height map and a material map
(format in the README). Stock textures have them baked by
`tools/make_material_maps.py`; a texture without them has them generated when
first drawn, on a worker thread, and kept in the user's cache by the
picture's content (`engine/material_maps.py`). Tool textures are flat on
purpose. **Tools > HD Readiness Report** shows where each texture a map uses
gets its maps. GLB roughness and metalness factors and an MTL's `Pr`, `Pm`
and `Ke` are used.

### Its one configuration

`engine.hd_graphics.HD_HIGH`: shadow map size, ambient occlusion and
reflection sample counts, and which effects are on. There are no quality
tiers: Fio has two renderers.

### Cost

See [`hd_high_performance.md`](hd_high_performance.md) and `tools/bench_hd.py`.

### Known limits

* GLB materials' own normal and metallic-roughness *textures* are not read
  yet; a model's base-colour texture gets generated maps instead.
* Decals are drawn by the classic decal program (colours preserved), without
  material maps of their own.
* A model's untextured groups take the default roughness and metalness.
* The block-mesh terrain and the grass are drawn as Classic draws them.
* Sky light is all-or-nothing by what is overhead: daylight does not spread
  sideways into a room through a window (the sun, with its shadow map, does).

---

## Writing another renderer

`docs/examples/deferred_renderer` is a complete renderer that shares nothing
with either of these, and the wiki's Renderer Development Guide walks through
it. HD_High is the larger example: a second full renderer registered beside
the first.
