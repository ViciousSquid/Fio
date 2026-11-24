# HD_High performance audit (PR 145)

An audit of the HD_High renderer as it stood at `7828755` (Canary), what was
changed after it, and what is left. Every number below was measured with the
tools in `tools/` (`bench_hd.py`) and the scripts described under *Method*.

**About the numbers.** They come from Mesa's software rasteriser on a 4-thread
x86 container (`llvmpipe (LLVM 20.1.2, 256 bits) | 4.5 Core | Mesa 25.2.8`), the
same driver `docs/hd_high_performance.md` used. A "GPU" millisecond there is a
CPU drawing pixels, so absolute times say nothing about a real graphics card.
The ratios, the split between passes and the CPU-side (Python) costs carry over.
Run `tools/bench_hd.py` on the hardware you care about.

## Summary

HD_High is a carefully built renderer. Python per-frame overhead is already low:
about 3,700 Python calls and ~1–3 ms a frame. Brush and model submission is
shared with Classic and is no heavier there. What HD_High adds is GPU fill
cost: the physically based scene shader, and a fixed chain of full-screen
passes worth about 25 ms at 720p on llvmpipe even for an empty frame. On top of
that are a few CPU stalls and memory costs that a GPU-bound benchmark does not
show. These are the findings, ranked:

| # | Finding | Where | Kind | Status |
| --- | --- | --- | --- | --- |
| 1 | **Overdraw pays full PBR shading.** No depth prepass; brushes are sorted by texture, not depth. Measured overdraw on the opaque brushes: Office_Corridor 1.0, MonsterTest 1.9, _SHOWCASE 2.3–2.5. | `renderer.py` opaque passes | GPU | **Fixed**: adaptive depth prepass |
| 2 | **Point-light shadow array sized at the largest light, in 24-bit depth.** Every slot gets six layers at the biggest `shadow_map_size` any shadow light asks for, and slots are counted up to the highest slot index. 8 slots at 1024 = 192 MB; one light at 2048 makes it 768 MB. Classic sizes each light's cube on its own. It is not counted in `hd_memory()`. | `lighting.py` `_ensure_shadow_array` | VRAM | **Halved** (16-bit, which `depth_cube.frag`'s linear distance needs no more than) and now reported; per-light sizing left as a recommendation |
| 3 | **Material maps are decoded and uploaded on the render thread, inside the draw loop**, the first time each texture is bound: two lossless-WebP decodes and two mip-mapped uploads. That is ~13 ms per 512² texture and ~48 ms per 1024² on this machine, and it comes back whenever new textures come into view. | `environment.py` `_material_for` | CPU hitch | **Fixed**: decoded on worker threads, at most 2 textures uploaded a frame |
| 4 | **Textures whose maps are still being generated re-probe the filesystem on every bind of every frame** (up to 7 `asset_exists` calls and a sidecar read each), for as long as the single worker takes. | `environment.py` `_material_for` | CPU | **Fixed**: the in-flight job is remembered |
| 5 | **Fixed post-processing chain ~25 ms at 720p on llvmpipe**, map-independent: compose 7.1, AO 5.3, resolve 4.6, FXAA 3.6, bloom 4.2 (down 2.6 + up 1.6), AO blur 1.9. | `postfx.py` | GPU | Trimmed (below); the rest are recommendations |
| 6 | **A `glGetError` drain every frame** after the depth blit. On threaded drivers (Mesa glthread, NVIDIA threaded optimisation) each one is a pipeline sync. | `postfx.py` `_postfx_end` | CPU stall | **Fixed**: checked once per framebuffer and size |
| 7 | **Bloom's nine passes run at bloom 0; the FXAA pass runs as a plain copy with FXAA off.** | `postfx.py` | GPU | **Fixed** |
| 8 | **The Forward+ grid is rebuilt every frame in play with 12+ lamps**: its key is the light block's, which changes with each streamed lamp's fade weight. The rebuild is a NumPy reach test and a 128 KB `glTexImage2D` re-specification. | `environment.py` `_env_light_grid` | CPU | **Fixed**: keyed on the lamps' places, reach and order; `glTexSubImage2D` |
| 9 | **`uMaterial` re-sent on every texture switch**: it was compared by object identity, which differs for every texture, though the value rarely does. | `environment.py` `_bind_material_maps` | CPU | **Fixed**: compared by value |
| 10 | **Parallax step count ignores the mip level** it samples: distant and glancing surfaces take 8–20 steps out to 1100 units for detail the sampled level cannot show. | `surface.py` `fioParallax` | GPU | **Fixed**: steps per texel of the sampled level |
| 11 | Cloud-shadow noise (3 fBm taps) evaluated where the sun's shadow map already says "fully shadowed". | `surface.py` `fioShade` | GPU | **Fixed** |
| 12 | Sun shafts: an `exp()` per step, 28 a pixel. | `postfx.py` `SHAFTS_FRAG` | GPU | **Fixed**: a two-multiply recurrence |
| 13 | A slot list turned into a tuple on every program bind, for a record nothing reads (`_frame_lights_uploaded`). | `lighting.py` | CPU | **Fixed** |
| 14 | **`settings.ini` ships `renderer = HD_High`** (commit `7d9f7dd`), but low-power detection only covers ARM. Celeron, Atom and old-iGPU x86 machines start in HD_High, at 3–12× Classic's GPU cost and +33–75 MB of VRAM, and the startup also compiles the HD programs. `hd_graphics.DEFAULT_RENDERER` and `docs/renderers.md` still describe Classic as the default. | `settings.ini` | Product | Left as is (your call); see recommendations |
| 15 | **The editor's 3D view repaints every 16 ms** (`QtGameView.update_loop`), and View > HD preview defaults on, so an idle editor runs the whole HD chain at 60 Hz. The repaint loop predates HD_High; HD only makes it costlier. | `qt_game_view.py` | CPU/GPU | Recommendation |
| 16 | Sun shadow "moving" layer: redrawn whenever any model moves anywhere on an unstreamed map (the area is the whole map). Each redraw is a 2048² depth blit plus a draw per model. | `environment.py` `_env_sun_shadows` | GPU/CPU | Recommendation |
| 17 | Sky light volume rebuilt on the main thread (a Python loop of ~12k small NumPy calls on a 1.4M-cell grid), and on large maps again each time the camera crosses a 256-unit window step. | `skylight.py` | CPU hitch | Recommendation |
| 18 | Glass and water copy the HDR frame (RGBA16F) into an RGBA8 texture, which clamps bright refractions to 1.0. | `passes.py` `_capture_glass_scene` | Quality | Recommendation |
| 19 | The PR adds 32 MB of lossless `_n`/`_m` WebP maps (+62% of `assets/`, more than the diffuse images they belong to). The Android/GLES package exporter ships them too, though that player never reads them. | `assets/`, `editor/package_exporter.py` | Size | Recommendation |


## Before and after

`tools/bench_hd.py`, 1280x720, 12 frames, same machine and driver. The "before" column is `7828755`. GPU ms is
the whole frame.

| Map | GPU ms before | GPU ms after | Change | Scene pass before → after |
| --- | ---: | ---: | ---: | ---: |
| Office_Corridor | 58.6 | 53.8 | −8% | 24.9 → 21.7 |
| _SHOWCASE | 148.1 | 113.9 | −23% | 106.6 → 74.5 (−30%) |
| MonsterTest | 87.6 | 68.4 | −22% | 44.9 → 27.9 (−38%) |
| Terrain_Test_small2 | 31.5 | 30.1 | −4% | 6.8 → 6.0 |

At 160x90, where what is left is mostly CPU, frames were within noise: Office_Corridor 5.6 → 4.9 ms, _SHOWCASE
13.0 → 13.3, MonsterTest 9.0 → 9.1, Terrain 4.8 → 4.3. The prepass's second brush submission costs about what the
other CPU trims save.

The "extra VRAM" column of `bench_hd.py` grows on maps with shadow-casting point lights (_SHOWCASE 75 → 87 MB)
because the point shadow array is now counted at all. Its real size halved.

Frames against the unchanged renderer (Office_Corridor, _SHOWCASE, MonsterTest, Terrain_Test_small2, Light_Test,
Glass_Test): mean difference 0.41–0.66 / 255, at most 45 pixels above 8 in a 1280x720 frame, none above 24. That is
the same as two frames of the unchanged renderer.

## What changed

* **Adaptive depth prepass** (`renderer.py` `_draw_opaque_brushes_hd`, `surface.py` mode 4).
  In an HD frame the opaque brushes are first drawn for depth alone, with colour writes off. In that pass the
  textured program samples only the alpha that decides whether a fragment exists, and only for textures that
  have an alpha channel (`uMaterial.w`). It uses the same parallax-shifted coordinate as the shaded pass, so both
  passes keep exactly the same fragments. The shaded pass then runs with `GL_LEQUAL` and depth writes off, so
  early-Z holds even for the shader's `discard`. A `GL_SAMPLES_PASSED` query, read back a frame or more later and
  never waited on, measures overdraw. `depth_prepass = 'auto'` turns the prepass on above 1.3× and off below 1.15×;
  `'on'` and `'off'` force it. Images match the unchanged renderer within the frame-to-frame noise floor.
  Scene GPU time at 720p: MonsterTest 77.6 → 63.8 ms (−18%), _SHOWCASE 140.6 → 112.2 ms (−20%).
  Office_Corridor (overdraw 1.0) would lose 7%, so auto leaves it off there.
* **Point shadow array in `GL_DEPTH_COMPONENT16`**: half the memory and half the traffic. A step is the light's
  radius / 65536, far finer than the texel-sized bias the lookup applies. `hd_memory()` now reports it.
* **Material maps off the render thread** (`environment.py` `_material_collect`). Files are resolved on the
  render thread, because the archives are not asked from another. Pixels are decoded on a 2-worker pool shared
  with the generator, and at most `MATERIAL_UPLOADS_PER_FRAME = 2` textures are uploaded a frame. An in-flight
  texture is remembered, so nothing is probed again. Render-thread time in `_material_for` over a fresh
  renderer's first 40 frames: _SHOWCASE 213 → 75 ms, worst single call 36 → 9 ms; MonsterTest 29 → 7 ms,
  28 → 5 ms. What is left is the uploads and their mipmaps, spread over frames. A surface draws without its maps
  for the few frames until they arrive, as generated maps already did.
* **Post-processing**: bloom skipped at strength 0; compose writes straight into the view when FXAA is off;
  the depth blit's error check happens once per framebuffer and size, not as a `glGetError` drain every frame;
  sun shafts use a recurrence for their Gaussian.
* **Surface shader**: parallax steps are counted per texel of the mip level actually sampled, and cloud shadows
  are only evaluated where the sun reaches.
* **Forward+ grid**: rebuilt only when the lamps' places, reach or order change; updated in place.
* **CPU trims**: `uMaterial` compared by value; the unused per-bind record dropped.
* `tools/bench_hd.py` warms up until every material map has arrived (they now arrive over frames).


## Forward+ threshold

HD_High binned lamps into world-space clusters (Forward+) from 12 lamps in a frame. Most Fio maps have few
lamps: of the 25 stock maps the median has 2, and only _SHOWCASE has more than 9. So the question is the count
below which the plain loop (each fragment tests every lamp, as Classic does) is as fast or faster.

Measured: scene-pass GPU time (GL timestamps) at 1280x720, best of two rounds of 9 frames, with
`FORWARD_PLUS_MIN_LIGHTS` forced to 1 (grid) and to 10^6 (plain loop), depth prepass off. Lamps cast no shadows,
so only the light loop differs. The script is described under *Method*.

| Lamps in the frame | Spread (420 apart, reach 520): grid / loop | One room (all overlap, reach 700): grid / loop |
| ---: | ---: | ---: |
| 1 | 1.00 | 1.03 |
| 2 | 0.95 | 0.99 |
| 3 | 0.95 | 1.02 |
| 4 | 0.93 | 1.01 |
| 6 | 0.92 | 0.91 |
| 8 | 0.87 | 0.88 |
| 10 | 0.82 | 0.88 |
| 12 | 0.82 | 0.89 |
| 16 | 0.80 | 0.83 |
| 24 | 0.78 | 0.80 |
| 32 | 0.65 | 0.74 |

| Stock map | Lamps in the frame | Grid / loop |
| --- | ---: | ---: |
| Office_Corridor | 7 | 0.96 |
| MonsterTest | 8 | 1.01 |
| _SHOWCASE | 21 | 0.88 |

Frame-to-frame noise is about ±3%.

**N = 10**: the plain loop below 10 lamps, Forward+ from 10 (`EnvironmentMixin.FORWARD_PLUS_MIN_LIGHTS`, was 12).

* At 10 lamps and above the grid won in every layout, by 12–35%. That margin is wide enough to hold on real GPUs.
* Below 6 it won nothing where lamps overlap (a room, the common case in Fio maps). It gained only 5–7% where they
  are spread out, and most of that is fragments outside every lamp's reach skipping the loop.
* The real maps of 7 and 8 lamps gained 4% and 0%.
* The software rasteriser favours the grid at low counts: there, a lamp index fetched per fragment costs the same
  as any other load. On a GPU the plain loop indexes the light block uniformly across a warp (cheap, broadcast
  loads). The grid's index differs from fragment to fragment, a divergent uniform-buffer index that many GPUs
  serialise. So the crossover on hardware will be at or above what llvmpipe shows. That is why N is the first
  count with a clear win in every layout (10, where 8 already shows one in the synthetic layouts), and not the
  first count where any layout ties. If `tools/bench_hd.py` on target hardware shows a clear win at 8, lower it.
* The count is lamps in the frame's light block (after streaming and the per-frame cap), not lamps in the map
  file. 24 of the 25 stock maps now take the plain loop.


## Not changed (recommendations)

In rough order of value for the effort:

1. **Default renderer.** Ship `renderer = Classic` (or leave the key out), or pick HD_High only on machines that pass a
   GPU/VRAM check. Low-power detection only covers ARM, so weak x86 laptops now start in HD_High.
2. **Editor idle repaint.** Repaint the editor view only when something changed (or throttle it while idle) when HD
   preview is on. An idle editor currently runs the full HD chain at 60 Hz.
3. **Point shadows per light.** Size each slot to its own light (an atlas, or one array per size class) and shrink
   the array when lights go away, instead of every slot at the largest light's size.
4. **Sun shadow moving layer.** Only redraw for movers inside the view frustum extended towards the sun, and draw
   models instanced. Or keep movers in a smaller map of their own and drop the 2048² blit.
5. **Sky light volume off the main thread**, built on a worker and swapped in when ready. When its window moves,
   shift the existing volume and fill only the new strip.
6. **Post-processing at lower cost:**
   * A shared half-resolution linear depth for AO, the reflection march and the shafts (they read the
     full-resolution D24S8 today).
   * Apply AO with a multiplicative blend when reflections are off.
   * Store the HDR target linear rather than gamma-encoded: every pass decodes and re-encodes with `pow()`, and
     linear storage would allow hardware blending.
   * `data` target as RGBA8 or RGB10A2 instead of RGBA16F.
7. **Light loop:** precompute each lamp's linear colour and `intensity^2.2` (cookies and the source fade make this
   per-lamp rather than a pure UBO change). Pack 4 lamp indices per grid texel (RGBA8UI).
8. **Glass/water copy** into an RGBA16F (or R11G11B10F) texture in HD frames, so bright refractions are not clipped.
9. **Assets:** lossy WebP (q≈90) for the `_n` maps, and keep companion maps out of GLES/Android packages.
10. **Reflection probes:** capture one face a frame, pool the GL objects, and do not recapture probes on movers
    every frame.
11. **Quality preset knobs** (a product decision, not changed here): `ao_taps` 12 → 8 and `ssr_steps` 32 → 24 in
    `HD_HIGH` would cut AO and reflection cost by about a third.


## Method

* `tools/bench_hd.py --maps Office_Corridor,_SHOWCASE,MonsterTest,Terrain_Test_small2 --sizes 1280x720,160x90 --frames 12`,
  run before (worktree at `7828755`) and after, on the same machine.
  160x90 is there to make rasterising negligible, so what is left is CPU-side cost.
* Pixel-identity checks: the same frame drawn with a change on and off, with
  film grain off. The noise floor from frame to frame (the clock moves monsters
  and clouds) is a mean difference of ~0.5–0.7 / 255 and a maximum of ~12–14;
  each change was held to that floor.
* Overdraw: `GL_SAMPLES_PASSED` around the opaque brush, model and terrain
  passes, divided by the frame's pixels.
* Full-screen pass costs: a `GL_TIME_ELAPSED` query around every
  `_env_fullscreen` draw, keyed by program.
* Render-thread material-map cost: wall time inside `_material_for` over a
  fresh renderer's first 40 frames.
* Forward+ threshold: lamp fields built with `tests.helpers.worlds` (a 6000-unit floor, n lamps on a square
  grid), and the stock maps from their PlayerStart, each drawn by a fresh renderer with
  `FORWARD_PLUS_MIN_LIGHTS` forced either way. `light_grid_stats` confirmed the grid was or was not in use.
