# HD_High: what it costs

Measured with `tools/bench_hd.py`, which draws stock maps with both renderers from the map's PlayerStart into an offscreen framebuffer and reports medians over 20 frames after a warm-up.

    xvfb-run -a python tools/bench_hd.py --maps Office_Corridor,_SHOWCASE,MonsterTest,Terrain_Test_small2 --frames 20

**Read this first.** The numbers below were taken on **llvmpipe (LLVM 20.1.2, 256 bits) | 4.5 (Core Profile) Mesa 25.2.8-0ubuntu0.24.04.5**: Mesa's software rasteriser, on a machine with no GPU. Every "GPU" millisecond here is a CPU drawing pixels, many times slower than any real graphics card, so the absolute times say nothing about frame rates on real hardware. What does carry over: the **ratio** between the renderers at a given size, the **split** of HD_High's frame between its passes, and how each scales with resolution. Run the tool on the machine you care about for real numbers.

## Classic against HD_High

| Map | Size | Renderer | GPU ms | CPU ms | Frame ms | Extra VRAM |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| Office_Corridor | 1280x720 | Classic | 8.3 | 2.3 | 9.8 | - |
| Office_Corridor | 1280x720 | HD_High | 98.0 | 91.7 | 99.4 | 34 MB |
| Office_Corridor | 1920x1080 | Classic | 15.4 | 3.3 | 17.6 | - |
| Office_Corridor | 1920x1080 | HD_High | 222.5 | 206.0 | 223.9 | 74 MB |
| _SHOWCASE | 1280x720 | Classic | 95.7 | 99.2 | 100.4 | - |
| _SHOWCASE | 1280x720 | HD_High | 252.4 | 249.1 | 253.6 | 90 MB |
| _SHOWCASE | 1920x1080 | Classic | 208.0 | 215.3 | 215.8 | - |
| _SHOWCASE | 1920x1080 | HD_High | 559.7 | 543.9 | 561.3 | 130 MB |
| MonsterTest | 1280x720 | Classic | 27.3 | 28.3 | 29.2 | - |
| MonsterTest | 1280x720 | HD_High | 134.8 | 128.2 | 135.7 | 36 MB |
| MonsterTest | 1920x1080 | Classic | 53.7 | 54.8 | 56.1 | - |
| MonsterTest | 1920x1080 | HD_High | 302.0 | 287.8 | 303.1 | 77 MB |
| Terrain_Test_small2 | 1280x720 | Classic | 0.0 | 0.3 | 3.3 | - |
| Terrain_Test_small2 | 1280x720 | HD_High | 60.2 | 56.8 | 60.8 | 34 MB |
| Terrain_Test_small2 | 1920x1080 | Classic | 0.1 | 0.5 | 7.4 | - |
| Terrain_Test_small2 | 1920x1080 | HD_High | 150.3 | 134.0 | 151.4 | 74 MB |

* *GPU ms* is the whole `render_scene` in a `GL_TIME_ELAPSED` query; *CPU ms* is the wall time of the call (Python and submission; under a software rasteriser it includes the rasterising, so it tracks GPU ms here); *Frame ms* adds `glFinish`.
* `Terrain_Test_small2` has no brushes and the benchmark does not load terrain, so its rows are an empty scene under a sky: Classic draws almost nothing, and HD_High's row is the fixed cost of its post-processing chain at that size.
* *Extra VRAM* is what HD_High allocates beyond Classic, counted from sizes and formats.

## Where HD_High's frame goes

GPU milliseconds per pass (GL timestamps between the passes):

| Map | Size | Sun shadows | Scene | Ambient occlusion | Reflections | Blended | Bloom | Sun shafts | Compose |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Office_Corridor | 1280x720 | 0.0 | 24.0 | 14.6 | 23.4 | 0.0 | 18.2 | 0.0 | 16.8 |
| Office_Corridor | 1920x1080 | 0.0 | 51.9 | 35.3 | 55.8 | 0.0 | 39.4 | 0.0 | 40.4 |
| _SHOWCASE | 1280x720 | 0.0 | 169.3 | 14.8 | 24.1 | 9.4 | 18.1 | 0.0 | 16.1 |
| _SHOWCASE | 1920x1080 | 0.0 | 375.8 | 33.8 | 52.7 | 18.7 | 38.9 | 0.0 | 38.3 |
| MonsterTest | 1280x720 | 0.0 | 48.3 | 15.0 | 23.5 | 13.7 | 18.3 | 0.0 | 16.4 |
| MonsterTest | 1920x1080 | 0.0 | 109.6 | 34.5 | 53.3 | 24.6 | 38.3 | 0.0 | 39.3 |
| Terrain_Test_small2 | 1280x720 | 0.0 | 9.4 | 7.2 | 10.4 | 0.0 | 17.0 | 0.0 | 16.0 |
| Terrain_Test_small2 | 1920x1080 | 0.0 | 22.0 | 19.2 | 25.9 | 0.0 | 42.2 | 0.0 | 42.7 |

* **Scene** (the opaque world, physically shaded) scales with what is on screen and with resolution; it is the pass that grows with a map's complexity.
* **Ambient occlusion, reflections, bloom and compose** (tone mapping, grade, FXAA) are full-screen passes: a fixed cost per pixel whatever the map, about two and a quarter times as much at 1920x1080 as at 1280x720.
* **Sun shadows** and **sun shafts** read 0.0 because none of these maps has a sun (their sky is `none`, or a panorama with the sun off), and because the sun's shadow map is not redrawn on a still frame.

## Video memory

At 1920x1080, by what it is for:

| Map | Frame targets | Bloom | Sun shadow maps | Sky cover map | Environment maps | Material maps |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Office_Corridor | 70.0 MB | 2.6 MB | 0.0 MB | 0.0 MB | 1.3 MB | 0.0 MB |
| _SHOWCASE | 70.0 MB | 2.6 MB | 0.0 MB | 16.0 MB | 1.3 MB | 40.2 MB |
| MonsterTest | 70.0 MB | 2.6 MB | 0.0 MB | 0.0 MB | 1.3 MB | 2.7 MB |
| Terrain_Test_small2 | 70.0 MB | 2.6 MB | 0.0 MB | 0.0 MB | 1.3 MB | 0.0 MB |

The frame targets scale with resolution; material maps with how many different textures a map shows (two maps per texture, at the texture's own size).

## Moving casters and the sun's shadow map

Levitate redrew the sun's whole shadow map whenever any caster moved. HD_High keeps the casters that stand still in a depth map of their own and draws the movers, doors and models over a copy of it. With a door swinging among 62 casters for 40 frames:

| | Full redraws | Moving-layer redraws | Casters drawn |
| --- | ---: | ---: | ---: |
| HD_High | 1 | 40 | 101 |
| Redrawing everything each frame | 40 | - | 2480 |

## Forward+

With 12 or more lamps in a frame HD_High bins them into world-space clusters and each fragment loops over its cluster's lamps only (`engine/renderer/hd_high/environment.py`, `light_grid`). Of these maps only _SHOWCASE has that many: its 21 lamps came to 1.5 per occupied cluster on average and 9 at most, so a fragment there tests one or two lamps where the plain loop tests all 21.
