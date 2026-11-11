> Applies to Fio 3.1.0.0611 (CANARY).

# Renderer Technical Overview

Fio draws through a replaceable renderer. The package `engine.renderer` defines the contract (`Renderer`, a `typing.Protocol`), a name -> factory registry, optional shared infrastructure (`engine.renderer.core.RendererCore`) and the built-in implementation, `engine.renderer.forward.ForwardRenderer`, registered as `"Forward"`. `ForwardRenderer` is a forward OpenGL 3.3 core renderer that consumes two dense NumPy projections of the world — `RenderTable` (brushes) and `EntityTable` (entities) — published by the logic thread, and submits them as instanced runs.

This page describes how a frame is produced, published and drawn, and everything `ForwardRenderer` does with it. To write or replace a renderer see the [Renderer Development Guide](https://github.com/ViciousSquid/Fio/wiki/Renderer-Development-Guide). Related pages: [RenderTable](https://github.com/ViciousSquid/Fio/wiki/RenderTable), [EntityTable](https://github.com/ViciousSquid/Fio/wiki/EntityTable), [Cell Occlusion Culling](https://github.com/ViciousSquid/Fio/wiki/Cell-Occlusion-Culling), [Portal Rendering - Technical Overview](https://github.com/ViciousSquid/Fio/wiki/Portal-Rendering-‐-Technical-Overview), [Split-Screen](https://github.com/ViciousSquid/Fio/wiki/Split‐Screen), [Terrain](https://github.com/ViciousSquid/Fio/wiki/Terrain), [Debug Tables](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables), [Console commands](https://github.com/ViciousSquid/Fio/wiki/Console-commands).

```text
EditorState ─► LogicThread ─► RenderTable / EntityTable ─► engine.renderer (contract) ─► active renderer ─► OpenGL
 (authored)    (logic thread)   (RenderState, double-buffered)      ▲
                                                                    └─ QtGameView (host, GUI thread)
```

`engine/renderer_core.py` and `Renderer_F` no longer exist; nothing outside `engine/renderer/` imports an implementation module.

**Contents**

1. [Requirements and platform paths](#1-requirements-and-platform-paths)
2. [Package layout](#2-package-layout)
3. [The frame: logic thread](#3-the-frame-logic-thread)
4. [The frame: publication](#4-the-frame-publication)
5. [The frame: host paint](#5-the-frame-host-paint)
6. [ForwardRenderer: passes in order](#6-forwardrenderer-passes-in-order)
7. [Culling stages](#7-culling-stages)
8. [Brushes](#8-brushes)
9. [Entities: models, sprites, effects](#9-entities-models-sprites-effects)
10. [Lighting](#10-lighting)
11. [Shadows](#11-shadows)
12. [View distance, distance fog and ambient](#12-view-distance-distance-fog-and-ambient)
13. [Water, glass and fog volumes](#13-water-glass-and-fog-volumes)
14. [Terrain](#14-terrain)
15. [Portals and split-screen](#15-portals-and-split-screen)
16. [Display modes and render modes](#16-display-modes-and-render-modes)
17. [Overlays, post-scene drawing and HUD](#17-overlays-post-scene-drawing-and-hud)
18. [Shader set and uniform conventions](#18-shader-set-and-uniform-conventions)
19. [Settings and console commands](#19-settings-and-console-commands)
20. [Diagnostics](#20-diagnostics)
21. [Source files and tests](#21-source-files-and-tests)

---

## 1. Requirements and platform paths

| Requirement | Value | Where set |
|---|---|---|
| OpenGL version / profile | 3.3, core profile | `main.py` (`QSurfaceFormat.setDefaultFormat`) and `QtGameView.__init__` (`setFormat`) |
| Depth / stencil | 24-bit depth, 8-bit stencil (portals need the stencil) | same |
| Swap interval | 1 if `[Display] vsync` (default `True`), else 0; read once at start-up | `main.py` |
| Shading language | `#version 330 core` for every program | `engine/shaders.py`, `forward/occlusion.py`, `forward/distance.py` |
| Rendering thread | the GUI thread that owns the `QOpenGLWidget` context | `QtGameView.paintGL` |

Features the renderer relies on beyond plain 3.3 are probed, not assumed: the occlusion query target ([Cell Occlusion Culling](https://github.com/ViciousSquid/Fio/wiki/Cell-Occlusion-Culling)), line-width and point-size ranges (`OverlaysMixin._set_line_width` / `_set_point_size` clamp to `GL_ALIASED_LINE_WIDTH_RANGE` / `GL_ALIASED_POINT_SIZE_RANGE`, queried once), and each instanced program (a driver that rejects one leaves the per-object path in use).

OpenGL 3.3 has no base-instance draw, no explicit UBO binding qualifier and no dynamic sampler-array indexing. The code works within that: instance attribute pointers are re-aimed per run, the light block is bound with `glUniformBlockBinding`, and the shadow cube lookup is an `if` ladder.

### PyOpenGL error checking

`main.py` sets `PYOPENGL_ERROR_CHECKING=0` before `OpenGL.GL` is imported unless `FIO_GL_DEBUG=1`. With checks off, `QtGameView._report_gl_errors` reads `glGetError` after each of the first three paints and logs any error to the Debug Console. `FIO_GL_DEBUG=1` keeps per-call checks and routes the raw per-row uniform uploads through PyOpenGL's wrappers.

### Low-power path

`engine.shaders.detect_low_power_arm()` returns `(is_low_power, reason)`:

| Machine | Result |
|---|---|
| `FIO_ARM_MODE` = `1`/`true`/`yes`/`on` | low-power (forced) |
| `FIO_ARM_MODE` = `0`/`false`/`no`/`off` | not low-power (forced) |
| x86 / x86-64 | not low-power |
| ARM on macOS (Apple Silicon) | not low-power |
| ARM whose CPU name contains `x elite`, `x1e`, `x plus`, `x1p`, `oryon`, `ampere`, `graviton` or `neoverse` | not low-power |
| any other ARM (for example Snapdragon 8cx / SQ3) | low-power |

What it changes in `ForwardRenderer`:

| Setting | Default | Low-power |
|---|---|---|
| `lowpower_mode` (overridable: `[Renderer] lowpower_mode`) | `False` | `True` |
| lit/textured programs | `lit.*`, `textured.*` | `lit_arm.*`, `textured_arm.*` (no precision qualifiers, ambient 0.12 instead of 0.1) |
| light cap of the lit/textured/instanced programs | 64 | 16 (`MAX_LIGHTS_ARM`) |
| `shadows_enabled` default (overridable: `[Renderer] shadows_enabled`) | `True` | `False` |
| sprite texture-array layer size | up to 512 px | up to 256 px |
| occlusion probe ceiling / thresholds | 8 probes | 4 probes, stricter governor |

The Settings window's renderer tab uses the same function for its defaults.

### macOS

- The context is requested as 3.3 core on every platform; nothing in the renderer uses the compatibility profile. Line widths and point sizes above the driver's reported range are clamped (above), because a core profile need only support width 1.
- Apple's GLSL compiler assigns uniform location 0 to real uniforms (`normalMatrix`). Every optional-uniform test in the renderer compares with `-1`; `tests/renderer/test_uniform_location_checks.py` fails the build on any `> 0` / `>= 1` comparison of a location.
- A built `Fio.app` is read-only: `ShaderLoader` only writes the editable shader copies into `assets/shaders` when the directory is writable, and every program is compiled from the sources embedded in `engine.shaders.DEFAULT_SHADERS`.
- Apple Silicon is never treated as low-power.

---

## 2. Package layout

```text
engine/renderer/
├── __init__.py      public surface: Renderer, RenderStats, SelectionOverlay, EffectBillboard,
│                    FRAME_INPUT, WATER_QUALITIES, DEFAULT_RENDERER, register_renderer,
│                    create_renderer, available_renderers, renderer_factory
├── api.py           the Renderer protocol, FRAME_INPUT, RenderStats, selection descriptors
├── registry.py      name -> factory ("Forward" built in, resolved by import path on first use)
├── core/            optional renderer-independent infrastructure
│   ├── base.py          RendererCore (state, ready, cleanup)
│   ├── resources.py     texture cache, animated GIF textures, OBJ/GLB models, effect frames,
│   │                    ShaderLoader, UniformCache, the `simple` overlay program
│   ├── geometry.py      cube and sprite VAOs, convex (angled) brush mesh cache
│   ├── tables.py        batched model matrices, per-table texture ids/sizes, sprite recipes, model radii
│   ├── visibility.py    distance/frustum culls, pass classification, light and shadow-caster
│   │                    selection, fog/ambient values, portal camera maths, RenderView
│   ├── light_stream.py  LightStream: lights near the player, with hysteresis and fade (no GL)
│   ├── overlays.py      grid, gizmo, selection outlines, portal wireframes, connection lines,
│   │                    face highlight, component overlay, collision boxes, bullet marks
│   ├── uniform_rows.py  per-row uniform uploads by raw address
│   └── diagnostics.py   timed_pass
└── forward/         ForwardRenderer
    ├── renderer.py      __init__, render_scene (frame orchestration), cleanup
    ├── passes.py        brush, terrain, model, sprite, effect, water, glass, fog passes
    ├── lighting.py      light UBO, fog/ambient upload, shadow cube-maps
    ├── portals.py       stencil portals and the portal virtual scene
    ├── occlusion.py     cell-group occlusion queries
    ├── distance.py      Points / Wireframe distance-colour pass
    ├── instancing.py    instance buffers, VAOs, packing, face batches
    └── shaders.py       program compilation, instanced variants, terrain binding, sprite layers
```

`ForwardRenderer` is `class ForwardRenderer(PassesMixin, LightingMixin, PortalsMixin, InstancingMixin, DistanceMixin, OcclusionMixin, ShadersMixin, RendererCore)`. Importing `engine.renderer` loads no OpenGL; `engine.renderer.core` loads `RendererCore` lazily so `light_stream` and `timed_pass` import without PyOpenGL.

---

## 3. The frame: logic thread

`LogicThread` (`engine/logic_thread.py`, a daemon thread, `TICK_RATE = 60`) runs `_step_frame`: every whole fixed tick the accumulator holds, then `LogicRender.prepare_render_state()` (`engine/logic_render.py`), all under one acquisition of the tick lock. `_publish_frame` then asks `ThreadedGameState.request_swap()`.

`prepare_render_state` fills the **write** `RenderState`:

1. **Camera.** In Play: the cutscene camera if a cutscene supplies `cam_pos`, else a camera-mode transition blend, else the overhead camera, else first person (`player.pos + view_height`). Outside Play: the editor camera. Result: `camera_view_matrix`, plus `player_pos/angle/pitch` or `editor_camera_*`.
2. **HUD and gameplay scalars** (health, ammo, armour, keys, HUD alphas, underwater flags, muzzle flash, weapon), `projectiles` (an `(N, 3)` float32 array), `bullet_marks`, monster debug rays.
3. **Frustum.** `glm.perspective(fov, logic.camera.frustum_aspect, 1.0, view_distance.far_plane)` times the view matrix; six normalised planes from `extract_frustum_planes`.
4. **RenderTable.** `table.begin_frame(brushes, world_epoch, dirty_objects, edited, peer, peer_dirty)` re-resolves only rows the editor's render-dirty journal or `engine.change_journal` names, the rows being dragged, or — when the row set changed — reconciles. In Play, `MoverTable.publish` then writes mover/door positions and spins straight into `bounds` / `rot`.
5. **Brush visibility.**
   - `keep, all_slots = table.shown()` — rows not `hidden`.
   - Outside Play, the editor's view filters remove rows (`view_filters.hidden_brush_rows`).
   - `visible_brush_slots = flatnonzero(keep & aabb_in_frustum_bounds(planes, table.bounds))` when `LogicRender.culling_enabled` (default `True`), else `all_slots`. The test is one matrix product: `(weights @ bounds.T >= -d).all(axis=0)` with `weights = [n | abs(n)]` (the positive-vertex AABB test).
6. **EntityTable.** `etable.begin_frame(things, ...)` returns the live `thing_hidden` mask. `visible_thing_slots` is `etable.all_slots`, minus rows the view filters hide (editor), minus collected Props (Play). Entities are **not** frustum-culled here: lights, portals and effects need every row; the renderer culls sprites and models per view.
7. **Split-screen.** With a second player, `player2_pos`, `player2_view_matrix`, P2 health and `splitscreen_active = True`.
8. `prepare_ms` — the logic-thread milliseconds this took.

If `prepare_render_state` raises, the half-built buffer is not published.

---

## 4. The frame: publication

`ThreadedGameState` (`engine/threaded_game_state.py`) holds exactly two persistent `RenderState` objects, each owning its own `RenderTable` and `EntityTable`.

| Call | Thread | Behaviour |
|---|---|---|
| `get_write_state()` | logic | the buffer being built; clears `_write_ready` |
| `peer_state()` | logic | the other buffer, read-only (used to adopt its tables instead of re-resolving every row) |
| `request_swap()` | logic | swaps read/write unless the read buffer is leased; a declined swap sets `_write_ready`, increments `declined_swaps` and returns `False` |
| `try_swap()` | GUI | `True` once per newly published frame |
| `get_render_state()` | GUI | leases the read buffer and returns a shallow snapshot (`__dict__` copy) whose arrays and tables still belong to it |
| `release_render_state(snap)` | GUI | ends the lease; if it was the last lease and a finished frame was held back, publishes it immediately |
| `published(name, default)` | GUI | one field of the published frame under the lock, without a lease |

Invariants:

- The renderer never reads a `RenderState` the logic thread is writing. While a paint holds the lease, publication waits; it is never forced.
- There is no third buffer. A swap resets the old read buffer's scalars and empties its slot vectors but keeps its tables, so no table is reallocated per frame.
- Each table has an integer `generation` that changes when slot addresses change; renderer caches keyed by a table use `(id(table), generation)` or the table's own name list.
- A lease is also released by a `weakref.finalize` on the snapshot; `_OwnedLock` lets a finalizer running on the lock-holding thread defer instead of deadlocking.

`published_frames` and `declined_swaps` are shown as rates by Debug Tables.

---

## 5. The frame: host paint

`QtGameView` (`engine/qt_game_view.py`) is the host: a `QOpenGLWidget` that owns the GL context, the active renderer, the shared `ViewDistance` and the host-side sprite texture table.

**Scheduling.** A 16 ms `QTimer` runs `update_loop`. With the logic thread running it calls `update()` only when `game_state.try_swap()` reports a new frame — repainting the frame already on screen would hold the lease and starve publication. Input that changes only editor overlays calls `update()` itself.

**`paintGL`.** Returns if the renderer is missing or not `ready`. Otherwise: lease the frame, `_animate_textures()` (calls the renderer's optional `animate_textures(clock)` with seconds of unpaused Play, 0 outside Play), `_paint_frame(render_state)`, and release the lease in `finally`. `paint_ms` records the CPU time.

**`_paint_frame`**, in order:

1. `QPainter(self)`, `beginNativePainting()`.
2. Cache HUD scalars from the frame; `renderer.set_grid(world_size, grid_size)` if the grid changed.
3. Camera and projection: near plane 0.1 (overhead Play: `max(1.0, 0.1 * overhead height)`), far plane `max(view_distance.far_plane, near + 1)`, `glm.perspective(camera.fov, aspect, near, far)`. `glClearColor` is set to `view_distance.fog_color`.
4. Build the frame input (`self._render_config`) — see the [Development Guide](https://github.com/ViciousSquid/Fio/wiki/Renderer-Development-Guide) for every key. Notable derivations:
   - `brush_display_mode`: the Display box while editing; in Play it is forced to `Textured` unless the box shows `Wireframe`, `Points` or `Overlay`.
   - `primary_selection`: `editor.selection_overlay.describe_selection(...)`, a `SelectionOverlay`.
   - `grid_visible`: the editor grid toggle and not Play.
   - `terrain`: the editor's terrain, or `None` while editing with the terrain view filter off.
   - `light_focus`: the player position in Play, else `None`; `light_fade_range` from the frame.
   - `splitscreen_active`: Play and split-screen mode and the frame's `splitscreen_active`.
5. Plugin event `render.pre_scene` (only if it has listeners).
6. **Single view:** `renderer.render_scene(projection, view, camera_pos, selection, config, brush_slots=visible_brush_slots)`; then the overhead player sprite, spectator glasses, `draw_collision_visualization` (when `showcollision` is on), `draw_bullet_marks`, `draw_billboards` (projectiles), monster debug rays and the spatial-grid debug lines (host's own debug program).
   **Split-screen:** see [section 15](#15-portals-and-split-screen).
7. Editor-only: `draw_connection_lines` (I/O links, when enabled), `draw_face_highlight` (face mode), `draw_component_overlay` (component mode).
8. `sysmon.update_stats(visible, culled, total)` from the logic thread's frustum result.
9. Plugin event `render.post_scene`.
10. `restore_default_pixel_store()`; disable scissor, depth test, stencil test, blend and face culling; full-widget viewport; `endNativePainting()`.
11. 2D overlay with the `QPainter`: underwater tint, FPS counter, HUD (single or split-screen), death screen, level-complete overlay, view messages, SysMon, floating windows, terrain brush cursor, plugin event `render.overlay`, pause menu.

---

## 6. ForwardRenderer: passes in order

`ForwardRenderer.render_scene(projection, view, camera_pos, primary_selection, config, clear=True, brush_slots=None)`. It raises `RuntimeError` if `brush_slots`, `render_table`, `entity_table`, `visible_thing_slots` or `thing_hidden` is missing — there is no object-list fallback.

`mode` below is `config['render_mode']` (`RENDER_MODE_LIT` 0, `UNLIT` 1, `WIREFRAME` 2, `VERTEX` 3); `display` is `config['brush_display_mode']`; `look` is `'Points'` or `'Wireframe'` when `display` is one of those, `mode` is LIT and this is not a portal scene.

| # | Step | Method | Condition | Reads | Draws / does |
|---|---|---|---|---|---|
| 1 | Frame setup | `render_scene` | always | — | depth test `GL_LESS`; clear colour+depth+stencil if `clear` (clear colour is the host's fog colour, or black when `look`); `render_stats.reset()`; `_begin_geo_frame()`; reset per-frame light state; polygon mode from `mode` |
| 2 | Grid | `draw_grid` (`grid`) | not `look`; `grid_visible`; not Play | grid VAO | grey lines, `simple` program |
| 3 | Brush distance cull | `_distance_cull_slots` | `camera_distance_cull` (default: `play_mode`) | `table.center` | narrows `brush_slots` by XZ distance |
| 4 | Occlusion filter | `_occlusion_filter_slots` | Play, not split-screen, enabled | cell index, probe book | drops hidden cells' slots |
| 5 | Brush classification | `_classify_brush_slots` | always | `table.class_bits` | `opaque`, `textured`, `solid`, `transparent`, `water`, `glass`, `fog`, `glow`; transparent/water/glass sorted far-to-near by XZ |
| 6 | Entity selection | `_distance_cull_thing_slots`, `entity_table.classify_slots`, `_cull_entity_rows` | always | `etable.pos`, `class_bits`, `sprite_size`, model radii | model slots, sprite slots, effect slots; fills `entity_candidates` / `culled_entities` |
| 7 | Light selection | `_get_active_lights` | always | `etable.light_*`, `light_focus` | `(table, light slots)` nearest-first in Play |
| 8 | Shadow maps | `render_shadow_maps` (`shadow maps`) | `mode` LIT and `shadows_enabled` and a selected light casts shadows | `all_brush_slots`, shadow-caster bits, model slots | depth cube-maps, only for lights whose signature changed |
| 9 | Terrain | `render_terrain` (`terrain`) | `terrain` present and enabled | terrain, lights, frustum planes | heightfield chunks through the terrain's own draw code |
| 10 | Portals | `draw_portals` (`portals (incl. their views)`) | Play and portal GL ready and portal rows exist | `etable.portal_*`, `all_brush_slots` | stencil-masked virtual scenes, depth flattened to the aperture |
| 11 | Opaque brushes | `draw_textured_brushes_optimized` (`textured brushes`), `draw_lit_brushes_optimized` (`lit brushes`), or `draw_brush_edges` (`brush edges`) | see [section 16](#16-display-modes-and-render-modes) | brush columns | instanced runs; back-face culling on in LIT/UNLIT |
| 12 | Glow brushes | `draw_glow_brushes` (`glow brushes`) | any | `table.glow_colour` | per-row draws, `lit` program |
| 13 | Opaque models | `draw_models_instanced` (`models`) | model rows with `render_alpha >= 1` | `etable.model_*` | instanced per recipe; editor-only models (`ENT_EDITOR_MODEL`) last, and not when the camera is inside one |
| 14 | Triangle overlay | `draw_triangle_overlay` (`triangle overlay`) | `display == 'Overlay'` and `mode` LIT | opaque + glow slots | every brush triangle as a white line |
| 15 | Distance colour | `draw_distance_pass` (`distance colour`) | `look` | depth copy | one full-screen triangle; then the grid for Wireframe |
| 16 | Path nodes | `draw_path_node_cubes` | not Play | `etable.path_node_slots` | editor cubes |
| 17 | Portal outlines | `draw_portal_wireframes` | portal rows in `visible_thing_slots` with `portal_show_rim` | `etable.portal_*` | outline and normal lines, `simple` program |
| — | State change | | | | blend on, depth writes off |
| 18 | Fading models | `draw_models_instanced` | model rows with `render_alpha < 1` | | blended instances |
| 19 | Effects | `draw_effects_instanced` (`effects`, `fire effects`) | effect rows | `etable.effect_*` | FIRE/ORB/CUSTOM billboards, EXPLOSION instances |
| 20 | Sprites | `draw_sprites_instanced` (`sprites`) | sprite rows | `etable.pos`, `sprite_*`, `render_alpha` | one instanced draw, back to front |
| 21 | Trigger volumes | `draw_lit_brushes_optimized(is_transparent_pass=True)` (UNLIT: textured pass) | editor only (`transparent` is empty in Play) | | cyan, alpha 0.3; filled if `show_triggers_as_solid`, else lines |
| 22 | Water | `draw_water_brushes` (`water`) | `mode` LIT | `table.water_*` | scene colour (and depth) copy, per-row draws |
| 23 | Glass | `draw_glass_brushes` (`glass`) | `mode` LIT | `table.glass_*` | scene colour copy, per-row draws |
| 24 | Fog volumes | `draw_fog_volumes` (`fog`) | `mode` LIT | `table.fog_*`, 3D noise | ray-marched boxes, front and back faces |
| 25 | Occlusion probes | `_occlusion_end_of_frame` | probe pass ready | finished depth | polls queries, issues probe boxes (no colour/depth writes) |
| 26 | Selection overlays | `_draw_selection_overlays` | `primary_selection` | `SelectionOverlay` | outline, dashed bounds, Effect box, gizmo; depth test off |
| 27 | Tail | | | | depth test on, blend off, program 0 |

Names in parentheses are the `timed_pass` keys that appear in `RenderStats.pass_ms`. Under `look`, steps 9–13 are drawn with colour writes off (depth only), glow/water/glass brushes are folded into the opaque set, and fog volumes are dropped.

Rows 11–24 inside a portal's virtual scene are issued by `PortalsMixin._draw_portal_scene`, not by a recursive `render_scene`.

---

## 7. Culling stages

| Stage | Where | Thread | Applies to | Test |
|---|---|---|---|---|
| Hidden / filtered | `LogicRender.prepare_render_state` | logic | brushes, entities | `RenderTable.hidden`; editor view filters; collected Props |
| Brush frustum | `LogicRender.aabb_in_frustum_bounds` | logic | main-view brushes | AABB positive-vertex test against six planes; far plane = view distance |
| Brush distance | `VisibilityMixin._distance_cull_slots` | GUI | main-view brushes | `dx² + dz² <= view_distance.distance_sq` on brush centres; only when `camera_distance_cull` (default: Play) |
| Cell occlusion | `OcclusionMixin._occlusion_filter_slots` | GUI | main-view box brushes | GPU-verified hidden cell groups; Play, no split-screen |
| Entity distance | `VisibilityMixin._distance_cull_thing_slots` | GUI | sprites, models, effects | same XZ test on `etable.pos`; rows with `ENT_CULL_EXEMPT` (`ENT_LIGHT | ENT_PORTAL`) always survive; only when `camera_distance_cull` |
| Entity frustum | `VisibilityMixin._cull_entity_rows` | GUI | sprites, models | bounding sphere against this view's planes: half the billboard diagonal, or mesh radius × longest model axis |
| Portal view | `_portal_numeric_scene_inputs` | GUI | each portal's virtual scene | starts from `all_brush_slots` and every entity row; bounding-sphere test against the oblique virtual frustum |
| Shadow casters | `_shadow_caster_slots`, `_casters_in_reach` | GUI | each shadow light | `CLASS_SHADOW_CASTER` rows of `all_brush_slots` whose bounding sphere the light reaches |
| Terrain | `Terrain.advance` (host, before the frame) then `TerrainGL.render` | GUI | terrain chunks | the main view's frustum planes |
| Far plane | projection | GPU | fragments | the view distance, per fragment |
| Back faces | `_portal_begin_cull` | GPU | opaque brushes | see [section 8](#8-brushes) |

How they compose for the main view's brushes:

```text
live rows ─► hidden/filter mask ─► frustum (logic) ═ visible_brush_slots
          ─► XZ distance (Play) ─► occlusion (Play, single view) ─► classification ─► passes
```

Rules worth knowing:

- The distance cull is an approximation on brush **centres**; the far plane is what guarantees nothing is drawn past the view distance. It is off in the editor so a large brush whose centre is out of range does not blink out while being built.
- Portal and shadow passes never inherit the main view's narrowed set; they start from `all_brush_slots`.
- Split-screen: player 1's view uses `visible_brush_slots`; player 2's view is passed `all_brush_slots` (the logic thread computed the frustum for player 1 only), so it is narrowed only by the distance cull and the far plane.
- There is no level-of-detail stage for brushes or models. Terrain has its own chunk LOD ([Terrain](https://github.com/ViciousSquid/Fio/wiki/Terrain)).

---

## 8. Brushes

### Classification

`engine/render_table.py` resolves each brush once per edit into `class_bits` (`uint16`):

| Bit | Value | Meaning |
|---|---:|---|
| `CLASS_HIDDEN_AUTHORED` | `1 << 0` | authored hidden (not the live `hidden` column) |
| `CLASS_WATER` | `1 << 1` | water brush |
| `CLASS_FOG` | `1 << 2` | fog volume (`is_fog` or shader `Fog`) |
| `CLASS_GLASS` | `1 << 3` | shader `Glass` |
| `CLASS_GLOW` | `1 << 4` | shader `Glow` |
| `CLASS_TRIGGER` | `1 << 5` | trigger volume |
| `CLASS_SUBTRACT` | `1 << 6` | subtract brush |
| `CLASS_HAS_GEOMETRY` | `1 << 7` | convex plane set: drawn from its own mesh |
| `CLASS_TEXTURED` | `1 << 8` | at least one face has a texture other than `default.png` / `caulk.jpg` |
| `CLASS_SHADOW_CASTER` | `1 << 9` | none of hidden-authored/trigger/fog/water/glass/glow/subtract |
| `CLASS_DYNAMIC` | `1 << 10` | mover or door |
| `CLASS_TEXTURE_TILING` | `1 << 11` | brush-wide constant texel size |

Water, fog, glass, glow and trigger are mutually exclusive, tested in that order. `CLASS_NON_OPAQUE` is their union. `_classify_brush_slots` returns:

| Group | Mask |
|---|---|
| `opaque` | no `CLASS_NON_OPAQUE` bit |
| `textured` / `solid` | `opaque` with / without `CLASS_TEXTURED` |
| `transparent` | `CLASS_TRIGGER`, editor only (empty in Play) |
| `water`, `glass`, `fog`, `glow` | the respective bit |

### Geometry

- **Boxes** use the shared cube VAO (`vaos['cube']`): 36 vertices of `pos3 | normal3 | uv2` (stride 32), six per face in the order `south, north, west, east, down, top` (`render_table.CUBE_FACE_KEYS`; face *i* starts at vertex `i * 6`). The cube is wound clockwise-outward.
- **Convex (angled) brushes** draw from `BrushGeoMesh` objects built from `RenderTable.geometry_records[geometry_id]` (`GeometryMixin._build_geo_mesh`). Meshes are cached by geometry signature, so they survive table reconciles and are shared by both render buffers; `_begin_geo_frame` frees meshes unused for more than 240 frames (swept every 240 frames). Convex meshes are wound counter-clockwise-outward and also carry an edge VAO for outlines.
- Model and normal matrices come from `render_table.model_matrices(table, slots)` in one batch: `M = T(centre) · R(axis, angle) · S(size)`, column-major float32 `(V, 16)` and `(V, 9)`.

### Render keys and runs

`engine/render_keys.py`: `KeyLayout` packs per-item fields into a sortable `int64` (at most 63 bits; out-of-range values raise). `sort_into_runs(keys)` is a stable argsort plus run boundaries; `runs_in_order(keys)` reports runs without reordering, for blended passes whose order is their output.

Rule: what cannot vary within one draw is in the key; everything else is instance data.

| Pass | Key | Instance payload |
|---|---|---|
| textured brush faces | `BRUSH_RUN_KEY = KeyLayout([('texture', 32), ('face', 3)])` | UV scale + shift; UV angle in `iNormal0.w` |
| lit (flat-colour) boxes | same layout, both fields 0 (one run) | colour + alpha |
| shadow depth | `SHADOW_RUN_KEY = KeyLayout([('face', 3)])` — the six runs share one instance array | none |
| models | `model_recipe_id` | — (own layout) |
| sprites | none (texture is a layer of one array) | layer index |

### Brush instance layout

One buffer, one VAO (`_brush_instance_vbo` / `_brush_instance_vao`) and one layout serve the textured, lit and shadow passes. 32 floats per instance (`BRUSH_INSTANCE_FLOATS`), attribute divisor 1:

| Location | Attribute | Content |
|---|---|---|
| 0–2 | `aPos`, `aNormal`, `aTexCoords` | the cube |
| 3–6 | `iModel0..3` | model matrix |
| 7–9 | `iNormal0..2` | normal matrix columns in `xyz`; `iNormal0.w` is a spare scalar |
| 10 | `iPayload` | pass-defined `vec4` |

A run that starts part-way into the buffer is reached by re-aiming the eight attribute pointers (`_point_brush_instances_at`), through the raw `glVertexAttribPointer` entry point.

### Textured pass (`draw_textured_brushes_optimized`)

`_build_face_batches` expands box rows into faces from `tex_name_id[:, 6]`, dropping faces whose texture id is `TEX_ID_SKIP` (`caulk.jpg`, never drawn), `TEX_ID_NODRAW` in Play (`nodraw.jpg`, editor-only), or `TEX_NONE`. Texture-name ids are local to a table; `TablesMixin._gl_texture_ids(table)` maps them to GL names once per new name. Each face's UV scale is: the authored `uv_scale` if present; a constant-texel "natural" scale (`face extent / texture size`) for faces flagged `uv_natural` or for unscaled faces of a `CLASS_TEXTURE_TILING` brush; else 1. Faces are sorted into `(texture, face)` runs and each run is one `glDrawArraysInstanced(GL_TRIANGLES, face * 6, 6, n)` with the `brush_instanced` program. Convex brushes then draw per texture run of their own mesh with the `textured` program. Without the instanced program the pass falls back to one draw per face.

### Lit pass (`draw_lit_brushes_optimized`)

All box rows go out as one `glDrawArraysInstanced(..., 0, 36, n)` with `lit_brush_instanced`. `lit_instance_payload` chooses the colour, last write wins: the brush's `colour`; red `(1, 0, 0)` for subtract brushes; yellow `(1, 1, 0)` for the selected brush; cyan `(0, 1, 1)` with alpha 0.3 for triggers. Convex rows draw one at a time with the `lit` program and the same precedence (trigger, selected, subtract, own colour).

### Back-face culling

`ForwardRenderer.cull_opaque_back_faces` (default `True`) sets `_opaque_cull_pass` around step 11 when `mode` is LIT or UNLIT. While it — or `_portal_scene_pass` — is set, `_portal_begin_cull` / `_portal_set_cull` enable `GL_CULL_FACE` and cull `GL_FRONT` for cubes and `GL_BACK` for convex meshes (they wind oppositely); `_portal_end_cull` restores culling off. It is not applied in wireframe or vertex modes, nor to glow, trigger, water or fog draws. In portal scenes the same mechanism hides interior faces exposed by the oblique near plane.

### Per-row uniform uploads

Passes that draw one row at a time (convex lit/textured rows, glow, water, glass, fog, brush edges, non-instanced shadow casters) upload `mat4` / `mat3` / `vec3` rows through `core.uniform_rows.uniform_rows(kind, rows)`, which calls PyOpenGL's own resolved entry point with a raw pointer into the array. The first upload of each kind is read back with `glGetUniformfv` and compared; a mismatch, an unresolvable entry point or `FIO_GL_DEBUG=1` falls back to the wrapper.

---

## 9. Entities: models, sprites, effects

`engine/entity_table.py` classifies each entity into `class_bits`: `ENT_SKIP`, `ENT_ALWAYS_SPRITE`, `ENT_HAS_MODEL`, `ENT_MODE_MODEL`, `ENT_MODE_BILLBOARD`, `ENT_HAS_SPRITE`, `ENT_EDITOR_MODEL`, `ENT_ENTITY_SPRITE`, `ENT_PROP`, `ENT_LIGHT`, `ENT_MONSTER`, `ENT_PORTAL`, `ENT_PATH_NODE`, `ENT_EFFECT`; `ENT_CULL_EXEMPT = ENT_LIGHT | ENT_PORTAL`. `classify_slots(table, slots, hidden, is_play, show_sprites)` returns `(model_slots, sprite_slots)` through a precomputed truth table: a visible model goes to the model pass; ordinary editor sprites are drawn only while editing or with `show_sprites_in_play_mode`.

Every entity pass draws only rows in `visible_thing_slots`. A light left out still lights.

### Models (`draw_models_instanced`)

- Rows are sorted into runs by `model_recipe_id`. A recipe is `(model_path, manual_texture, override_color, tint)` from `etable.model_recipes()`.
- `RendererCore.load_model` loads OBJ (`engine/obj_loader.py`) or GLB (`engine/glb_loader.py`) on first use, relative to `assets/models`; a failed path is retried no more often than every 5 s.
- Instance layout: 29 floats — model matrix (16, translation overwritten from `etable.pos`), normal matrix (12), `render_alpha` (1) — at attribute locations 3–10 added to the model's own VAO.
- Programs: `textured_instanced` for textured material groups (optional per-model `model_tint`), `lit_instanced` for flat-colour groups. Material groups with `alpha < 1` are drawn after opaque groups with blending and no depth writes.
- A single opaque model takes the non-instanced path (`_draw_dense_model_single`, programs `textured` / `lit`).
- Face culling is disabled for the pass (OBJ winding is per asset) and restored afterwards.

### Sprites (`draw_sprites_instanced`)

- Texture per row: `monster_facing.sprite_keys_for_view` picks the sprite key (eight-direction monster sprites depend on the eye), `_sprite_gl_ids` maps it to a GL texture; rows resolving to 0 are dropped.
- Order: back to front by XZ distance to the camera, with `sprite_overlay` as a tie-break so an attached flag paints over its carrier. The pass is blended with depth writes off, so the order is part of the picture.
- With the `sprite_layered` program every sprite image is a layer of one `GL_TEXTURE_2D_ARRAY` (`engine/sprite_layers.py`, `SpriteLayers`), filled by CPU readback and `glTexSubImage3D`, and the whole pass is **one** `glDrawArraysInstanced(GL_TRIANGLE_STRIP, 0, 4, n)`. Without the array (unsupported, or more images than layers) the pass draws one instanced call per equal-texture stretch of the depth-ordered sequence with `sprite_instanced`.
- Instance layout, 8 floats (`SPRITE_INSTANCE_FLOATS`): location 1 centre (3), 2 world size (2), 3 fixed yaw (1; values `<= -9999` mean camera-facing), 4 alpha (1), 5 array layer (1). The staging array is shared by the sprite, FIRE/ORB and billboard paths, so every path writes every column.

### Effects (`draw_effects_instanced`)

`etable.effect_slots` restricted to drawn, live, alive rows. `effect_type`: 0 FIRE, 1 EXPLOSION, 2 ORB, 3 CUSTOM.

- FIRE, ORB and CUSTOM are animated GIFs decoded once per renderer into GL textures (five authored variants each for FIRE and ORB; any path for CUSTOM). The frame is chosen on the render thread from `time.perf_counter() - effect_spawn_time`, plus `effect_phase`, so animation is not quantised to the logic rate. CUSTOM honours its loop flag (non-looping holds the last frame). Drawn through `sprite_instanced`, back to front.
- EXPLOSION uses the `effect_instanced` program with 16 floats per instance and the 5×4 explosion atlas (`assets/textures/effects/explosion.png`, clamped, no mipmaps), back to front.

---

## 10. Lighting

Forward, per-fragment, point lights only. In `lit.frag` / `textured.frag`: `result = (0.1 + uAmbient) * albedo`, plus for each light within its radius `diffuse * colour * intensity * (1 - dist / radius)² * (1 - shadow) * albedo`.

### Selection (`VisibilityMixin._get_active_lights`)

- Candidates: `etable.light_slots` with `light_enabled`; in Play also not `thing_hidden` (Big World parks out-of-range lights that way).
- Without `light_focus` (editor): all of them, in table order.
- With `light_focus` (Play): `LightStream.select` — lights within `ACTIVE_LIGHT_RANGE` (2048) light; a lit light stays lit out to `DEACTIVATION_RANGE` (2304); candidates are regathered over the whole map only on a 512-unit cell crossing, a table renumbering, or every `REFRESH_SECONDS` (0.5 s) within `CANDIDATE_RADIUS` (2816). Result is nearest first, with a render-only smoothstep intensity weight over the last `FADE_DISTANCE` (256) units (ending at `light_fade_range` if that is nearer). `light_range` in the frame input overrides the activation range.

### Capacity

| Program | Cap | Constant |
|---|---:|---|
| `lit`, `textured`, and their instanced variants | 64 | `shaders.MAX_LIGHTS` |
| the same in low-power mode | 16 | `shaders.MAX_LIGHTS_ARM` |
| `water` | 8 | `shaders.MAX_LIGHTS_WATER` |
| `terrain` | 8 (nearest to the camera) | `shaders.MAX_LIGHTS_TERRAIN` |

`_upload_lights_once(shader_name, lights)` sets `active_lights = min(len(slots), cap)`; every shader loop is also clamped to its own array size. Because the list is nearest-first in Play, a cap keeps the closest lights.

### Light UBO

Authored shader sources declare `struct Light { position; color; intensity; radius; shadowIndex; }` and `uniform Light lights[N]`. `shaders.light_ubo_source()` rewrites every fragment shader at compile time to a shared std140 block:

```glsl
struct Light { highp vec4 position; vec4 color; vec4 params; /* x=intensity, y=radius */ ivec4 indices; /* x=shadow index */ };
layout(std140) uniform FioLightBlock { Light lights[N]; };
```

The CPU mirror is `LightingMixin.LIGHT_UBO_DTYPE` (four 16-byte fields, 64 bytes per light). The block is bound to binding point `shaders.LIGHT_UBO_BINDING` (2) with `glUniformBlockBinding` for `lit`, `textured`, `lit_instanced`, `textured_instanced`, `brush_instanced`, `lit_brush_instanced`, `water` and `terrain`. The buffer holds the frame's first `MAX_LIGHTS` lights and is uploaded at most once per distinct `(table, generation, slots, weights)` key, by respecifying the whole store (orphaning). `indices.x` is the light's shadow cube-map slot or -1.

---

## 11. Shadows

Omnidirectional point-light shadows as depth cube-maps (`LightingMixin`).

| Constant | Value |
|---|---|
| `MAX_SHADOW_LIGHTS` | 8 (`shaders.MAX_SHADOW_LIGHTS`) |
| `SHADOW_MAP_SIZE` | 384 px per face (default of `[Renderer] shadow_map_size`, clamped to 256–2048) |
| `SHADOW_TEXTURE_UNIT_BASE` | 4 (cube-maps on texture units 4–11) |
| format | `GL_DEPTH_COMPONENT24`, nearest filtering, clamp to edge |

`render_shadow_maps` runs before any scene geometry when `mode` is LIT and `shadows_enabled`:

1. Shadow lights are the selected lights with `light_casts_shadows`. More than eight: the eight nearest to the camera.
2. Each light keeps its cube-map slot while it stays selected (`_shadow_slot_owner`).
3. Casters per light: `CLASS_SHADOW_CASTER` rows of `all_brush_slots` whose bounding sphere the light's radius reaches, and model rows within twice the radius (`ENT_HAS_MODEL` and `ENT_MODE_MODEL`, not skipped/editor/always-sprite/hidden).
4. A light's signature is its rounded position and radius plus the bytes of its casters' slots, bounds and rotations (and models' recipes, positions and matrices). A slot whose signature is unchanged is **not** re-rendered, so static lights cost nothing after the first frame.
5. Rendering a light: six faces, 90° perspective, near `max(0.002 * radius, 1)`, far = radius. Box casters go out as one instanced draw per face (`depth_cube_instanced`, the shared brush instance buffer packed once per light); convex brushes and models are drawn individually with `depth_cube`. The fragment shader writes `gl_FragDepth = length(FragPos - lightPos) / far_plane`.
6. FBO, viewport, scissor, cull, blend and program state are restored afterwards.

Receivers use `SHADOW_GLSL` (`calcPointShadow`): 20-tap PCF over a disk of radius `0.004 * far * (1 + depth / far)` with a slope-scaled bias. Draws are counted in `RenderStats.shadow_draw_calls`.

---

## 12. View distance, distance fog and ambient

`engine/view_distance.py: ViewDistance` — one instance shared by `QtGameView`, the renderer and `LogicRender` (the host adopts the logic thread's instance in `_start_logic_thread` and assigns it to each renderer in `_sync_view_distance`).

| Property | Default | Notes |
|---|---|---|
| `distance` | 4096 (`DEFAULT_VIEW_DISTANCE`) | clamped to 500–20000 (`MIN_`/`MAX_VIEW_DISTANCE`) |
| `limit` | `None` | a world-imposed horizon (Big World sets its activation radius during Play); only ever narrows |
| `effective_distance` | = `distance` | with a limit: `min(distance, limit / 0.92)` with fog on, `min(distance, limit)` with fog off |
| `far_plane`, `distance_sq` | from `effective_distance` | the projection far plane and the XZ cull radius are the same number |
| `fog_enabled` | `True` | |
| `fog_start`, `fog_end` | `None` (automatic) | automatic = 0.60 and 0.92 of `effective_distance` |
| `fog_density` | 0.0 | 0 = linear ramp; > 0 adds an exponential-squared curve |
| `fog_color` | `(0.10, 0.10, 0.15)` | also the frame's clear colour |
| `ambient` | `(0, 0, 0)` | added to each lit shader's own ambient constant |

`resolve()` returns the `(start, end)` the shaders use and enforces: `end <= 0.98 * effective_distance` (`MAX_FOG_END_FRAC`), `end <= limit`, and `start <= end - 1` (`MIN_FOG_BAND`). Fog is therefore always opaque before the far plane; with the frame cleared to the fog colour the clip is invisible.

`FOG_GLSL` is spliced into every fragment shader that draws world geometry. Uniforms (`LightingMixin.ENV_UNIFORMS`): `uFogEnabled`, `uFogColor`, `uFogStart`, `uFogEnd`, `uFogDensity`, `uFogCamPos`, `uAmbient`. Distance is radial (`length(FragPos - uFogCamPos)`), not view depth. `fogFactor` is the linear ramp, or the larger of it and `1 - exp(-(density * (d - start))²)`. `_upload_env_uniforms` writes all seven unconditionally whenever a program is prepared, which is why console and spinbox changes show on the next frame. `ViewDistance.fog_factor(dist)` is the GL-free reference implementation.

There is no sky pass. The background is the fog colour; the water shader's reflection falls back to a procedural `skyColor()`.

---

## 13. Water, glass and fog volumes

All three run only when `mode` is LIT, after the sprites, with blending on and depth writes off, one draw per brush, sorted far to near (water and glass).

### Scene captures

| Texture | Unit | Made by | Used by |
|---|---:|---|---|
| scene colour, `GL_RGBA8`, viewport-sized | 2 | `_capture_glass_scene` — `glCopyTexSubImage2D` of the current framebuffer | water and glass refraction / transmission |
| scene depth, `GL_DEPTH_COMPONENT24`, viewport-sized | 3 | `_capture_scene_depth` — only if the read framebuffer has a single-sampled depth buffer | high-quality water; the distance-colour pass |

Each copy is made once per pass, before any surface of that pass is drawn; textures are reallocated only when the viewport size changes.

### Water (`draw_water_brushes`)

- Per-brush columns: `water_tint[3]`, `water_params[7]` = opacity, fresnel/reflectivity, wave height, waves enabled, distortion, refraction index, roughness; `water_plane`; `water_high_quality`.
- Geometry: the four side faces of the cube (`0..24`) unless `water_plane`, and the top as a 64×64 tessellated grid (`vaos['water_surface']`) that the vertex shader displaces with Gerstner waves. Wave amplitude: `wave_height * 30` when waves are enabled (values above 2 are treated as percentages), else 1.2; capped at 45 % of the brush height and 30 units. Convex water brushes draw their sides and a flat top grid or their own top faces.
- **Quality** is per brush. A brush with `water_high_quality` additionally gets depth-based colour, shoreline foam, caustics and screen-space reflections (`hasSceneDepth`, `ssrEnabled`); the depth copy is made only if some brush in the pass wants it and the framebuffer allows it. `renderer.water_quality` (`engine.renderer.WATER_QUALITIES = ('cheap', 'expensive')`, default `'expensive'`) is a session-only cap: `'cheap'` forces every brush cheap.
- Lit by up to 8 lights; texture unit 0 is `water_normal.png`.

### Glass (`draw_glass_brushes`)

Columns `glass_color[3]`, `glass_params[5]` = opacity, distortion, refraction, roughness, fresnel. Samples the scene colour copy; drawn with `GL_CULL_FACE` enabled (`GL_BACK`). The glass program is optional: if a driver rejects it the pass is skipped and everything else still renders.

### Fog volumes (`draw_fog_volumes`)

Columns `fog_color[3]`, `fog_params[2]` = density, noise scale. The fragment shader ray-marches the brush's box in local space (`inverseModel`, 16 steps, 3D noise texture `assets/noise_3d.bin` on unit 1, animated by `time`) — the `fog_arm.frag` variant is used on every platform. Both the front and the back faces of all six sides are drawn so the volume works from inside, including over the floor. A polygon offset (`-1, -1`) lets faces coplanar with bounding walls win the depth tie.

Volumetric fog brushes are unrelated to the distance fog of section 12.

---

## 14. Terrain

`render_terrain` draws the published terrain: the frame input's `terrain` is an immutable `engine.terrain_frame.TerrainFrame` (the host called `Terrain.advance` and `Terrain.publish` before the frame), never the `Terrain` object. The renderer keeps a `engine.renderer.core.terrain_gl.TerrainGL` under the frame's key and calls `TerrainGL.render(frame, projection, view, camera_pos, frustum_planes, active_lights_count, shadow_cubemaps, shadow_unit_base, env_uniforms)`. `TerrainGL` owns the buffers and draw code; the renderer supplies:

- its `terrain` program and uniform table (`setup_terrain_shader`), rebound whenever `terrain.shader_program` is not this renderer's program (new terrain, or a renderer swap);
- the four ground textures (`grass.jpg`, `rock.jpg`, `sand.jpg`, `snow.jpg` from `assets/textures/terrain`);
- the 8 lights nearest the camera, through the shared light UBO;
- the shadow cube-maps and index map (when `shadows_enabled`);
- the fog/ambient values as a dict (`env_uniform_values()`), because the terrain uploads its own uniforms;
- the main view's frustum planes, whose far plane is the view distance.

Before a renderer is retired the host calls `Terrain.release_renderer_resources()`, which zeroes the program, uniform table and texture names the renderer gave it. See [Terrain](https://github.com/ViciousSquid/Fio/wiki/Terrain).

---

## 15. Portals and split-screen

### Portals

Full detail: [Portal Rendering - Technical Overview](https://github.com/ViciousSquid/Fio/wiki/Portal-Rendering-‐-Technical-Overview). Placement in the frame:

- Drawn in Play only, after terrain and **before** the main view's opaque brushes (step 10).
- Candidates (`_portal_candidate_slots`): portal rows with a target, active or still fading (`portal_fade > 0.01`), within `PORTAL_RENDER_DISTANCE` (2048) of the camera.
- Limits: `MAX_PORTALS = 4` apertures per frame; `MAX_PORTAL_RECURSION = 1`.
- Per aperture (`_draw_one_portal`): scissor to the aperture's screen rectangle; write the aperture (inset by `PORTAL_APERTURE_INSET` = 4 units) into the stencil buffer; push its depth to the far plane; build the virtual camera from `EntityTable` portal columns and an oblique near-plane projection clipped at the destination; draw `_draw_portal_scene` where stencil equals the level; draw the rim and fade; then `_portal_flatten_depth` replaces the virtual scene's depth with the aperture plane's so later main-view passes depth-test against the portal, not the destination. Within `PORTAL_NEAR_STRADDLE` (24 units) of the plane the mask is drawn full-screen so the near plane cannot clip it.
- The virtual scene runs the same brush, model, glow, effect, sprite, trigger, water, glass and fog passes on slots narrowed by the virtual frustum, with back-face culling on, and ends with the player glasses.
- Portal scenes do not run shadow-map updates, terrain, the occlusion hooks or the distance-colour pass.

### Split-screen

Full detail: [Split-Screen](https://github.com/ViciousSquid/Fio/wiki/Split‐Screen). The host, not the renderer, implements it (`_paint_frame`): it clears the framebuffer once, then for the left and right halves sets scissor and viewport and calls `render_scene(..., clear=False)` followed by that view's post-scene draws (bullet marks, projectiles, debug rays, spatial grid, player glasses). Both halves use one projection with aspect `(width // 2) / height` and near plane 0.1. Player 1's call receives `visible_brush_slots`; player 2's receives `all_brush_slots` and `player2_view_matrix`. `splitscreen_active` is `True` in the frame input, which disables cell occlusion culling for both views. The renderer reads `GL_VIEWPORT` for its scene copies, so water, glass and portals work per half.

---

## 16. Display modes and render modes

Two independent inputs select how brushes are drawn.

**`brush_display_mode`** — the editor's **Display** box in the bottom bar (`MainWindow.display_mode_combobox`; items `Wireframe`, `Points`, `Solid Lit`, `Textured`, `Overlay`; default **`Textured`**). The console's `r_wireframe` drives the same box. In Play the host publishes `Textured` unless the box shows `Wireframe`, `Points` or `Overlay`.

| Display | ForwardRenderer (with `render_mode` LIT) |
|---|---|
| `Textured` | textured brushes through the textured pass, untextured ones through the lit pass |
| `Solid Lit` | every opaque brush through the lit pass: flat colour, lit, no textures |
| `Overlay` | `Textured`, then every opaque and glow brush triangle as a white line (polygon offset `-1, -1`, `GL_LEQUAL`, no depth writes) |
| `Wireframe` | brushes as their true edges (12 per box; a convex brush's real edges), models and terrain as line meshes, all depth-only on black, then coloured by distance |
| `Points` | geometry depth-only on black, then a world-anchored point cloud coloured by distance |

**Distance-colour pass** (`forward/distance.py`): after the depth-only geometry, the depth buffer is copied and one full-screen triangle rebuilds each pixel's world position (`uInvViewProj`) and colours it by distance from the eye along a ramp red → orange → yellow → green → cyan → blue over `SCAN_RANGE` = 1500 units, dimming beyond. In `Points` only pixels on a scan point light up: a jittered grid with `SCAN_SPACING` 9, core radius `SCAN_POINT_RADIUS` 2 and `SCAN_DENSITY` 0.5, laid on each surface in world space. If the program or the depth copy is unavailable, brushes are drawn as edges in their own colours instead. Grid: drawn after the pass for `Wireframe`, never for `Points`.

**`render_mode`** — `QtGameView.current_render_mode`, default `RENDER_MODE_LIT`. The `[Shortcuts] key_toggle_wireframe` key (default `F2`) toggles it between `RENDER_MODE_LIT` and `RENDER_MODE_WIREFRAME`.

| `render_mode` | Effect |
|---|---|
| `RENDER_MODE_LIT` (0) | everything above; shadows, water, glass and fog volumes are drawn only in this mode |
| `RENDER_MODE_UNLIT` (1) | opaque brushes through the textured and lit passes; no shadows, water, glass or fog volumes |
| `RENDER_MODE_WIREFRAME` (2) | `glPolygonMode(GL_LINE)`; all opaque brushes through the lit pass; no back-face culling |
| `RENDER_MODE_VERTEX` (3) | `glPolygonMode(GL_POINT)`, point size 4 (clamped to the driver's range) |

The host also contains an in-Play render-mode menu (`[1] Lit [2] Unlit [3] Wire [4] Vert`), but nothing in the code sets `show_render_menu` to `True`, so `UNLIT` and `VERTEX` are reachable only by assigning `view_3d.current_render_mode`.

---

## 17. Overlays, post-scene drawing and HUD

Drawn by the renderer inside `render_scene` (all from `core/overlays.py`, `simple` program):

| Overlay | When |
|---|---|
| grid (`set_grid`, `draw_grid`) | editing, grid visible |
| path-node cubes | editing |
| portal wireframes and normals | portal rows in `visible_thing_slots` with `portal_show_rim` |
| selection (`_draw_selection_overlays`) | `primary_selection`: `outline` (or the brush's real edges for `brush_id`), `dashed_bounds`, `effect_billboard` preview box, `gizmo_pos` — depth test off |

Drawn by the host after `render_scene`, through protocol methods:

| Method | Host use |
|---|---|
| `draw_billboards(projection, view, positions, size, tex_id)` | monster projectiles: one instanced draw, size `(40, 40)`, sprite `projectile` (fallback `Monster`) |
| `draw_player_glasses(projection, view, positions, width=40, height=18, lift=0, sprites=())` | players as glasses billboards (split-screen, spectators); portals draw them inside `_draw_portal_scene` |
| `draw_bullet_marks(projection, view, marks)` | impact marks: 2-unit black cubes |
| `draw_connection_lines` | editor I/O links |
| `draw_face_highlight` | face mode hover |
| `draw_component_overlay(projection, view, overlay, version)` | vertex/edge/face handles; buffer refilled only when `version` changes |
| `draw_collision_visualization` | `showcollision` debug boxes |

The host's own GL (monster debug rays, the `sg` spatial-grid lines) uses a small debug program it compiles itself in `_init_debug_resources`.

The HUD, FPS counter, SysMon, messages, pause menu and plugin `render.overlay` drawing are 2D `QPainter` work after `endNativePainting()`; they are not part of any renderer. The host restores viewport, scissor, depth/stencil/blend/cull enables and pixel-store alignment before the painter draws.

---

## 18. Shader set and uniform conventions

Sources are Python strings in `engine/shaders.py` (`DEFAULT_SHADERS`), compiled with `ShaderLoader.compile_from_source` (program validation off). `ShaderLoader` also writes them to `assets/shaders/` as an editable copy when that directory is writable; the renderer compiles the embedded strings (`_shader_source` reads a loose file only for a name missing from `DEFAULT_SHADERS`).

### Programs (`renderer.shaders[name]`)

| Name | Sources / derivation | Used for |
|---|---|---|
| `simple` | `simple.vert/frag` | overlays, brush edges, triangle overlay, bullet marks |
| `lit` | `lit.*` (`lit_arm.*` in low-power) | convex lit brushes, glow, triggers, single flat models |
| `textured` | `textured.*` (`textured_arm.*`) | convex textured brushes, single textured models |
| `lit_brush_instanced` | `lit` with model/normal from instance attributes and colour/alpha from `iPayload` | box brushes, flat colour |
| `brush_instanced` | `textured` with UV scale/shift from `iPayload`, UV angle from `iNormal0.w` | box brush faces |
| `lit_instanced`, `textured_instanced` | `lit` / `textured` with model attributes and `iInstanceAlpha` | models |
| `sprite` | `sprite.*` | player glasses |
| `sprite_instanced` | `sprite` with centre/size/alpha per instance | FIRE/ORB/CUSTOM, projectiles, sprite fallback |
| `sprite_layered` | `sprite_instanced` sampling `sampler2DArray sprite_layers` | entity sprites |
| `effect_instanced` | `effect.*` | EXPLOSION |
| `depth_cube`, `depth_cube_instanced` | `depth_cube.*` | shadow cube-maps |
| `water` | `water.*` | water |
| `glass` | `glass.*` (optional) | glass |
| `fog` | `fog.vert` + `fog_arm.frag` | fog volumes |
| `terrain` | `terrain.*` | terrain |
| `distance` | `forward/distance.py` | Points / Wireframe |
| `occlusion_probe` | `forward/occlusion.py` | occlusion probes |

The portal mask, rim and fade programs (`portal_mask.*`, `portal_rim.*`, `portal_fade.frag`) are held in `_portal_*_shader` attributes, not in `shaders`. `DEFAULT_SHADERS` also contains `terrain_mesh.vert` and `grass.*`, compiled by the terrain.

Instanced variants are **derived from the base source by text rewriting** (`ShadersMixin._instanced_vertex_source` and friends), so their lighting, fog and UV maths cannot drift from the path they accelerate. A rewrite that does not find the expected lines, or a compile failure, leaves the program absent and the pass uses its per-object path. Failure of a required program sets `_shader_init_failed`, so `ready` is `False`.

### Conventions

- **Matrices** are column-major; uniforms are named `projection`, `view`, `model`, `normalMatrix` (`mat3`, `transpose(inverse(mat3(model)))`).
- **Uniform locations** are cached per program in `UniformCache` (`renderer.uniforms[name]`). `cache[name]` queries on first use; `cache.get(name, -1)` reads only what was preloaded. A location is valid from 0; only `-1` means absent.
- **Vertex attributes**: 0 position, 1 normal, 2 texture coordinate; instance attributes from 3 (brush and model layouts) or 1 (sprite and effect layouts).
- **Fragment output** is `FragColor`; lit shaders output `vec4(applyFog(result, FragPos), alpha)`.
- **Lights**: `active_lights` (`int`) plus the `FioLightBlock` UBO at binding 2.
- **Fog/ambient**: the seven `u*` uniforms of section 12.
- **Texture units**:

| Unit | Content |
|---:|---|
| 0 | `texture_diffuse`, `sprite_texture`, `sprite_layers`, water `normalMap`, `explosion_texture`, portal `behind` |
| 1 | fog `noiseTexture` (3D) |
| 2 | `sceneColor` (water, glass) |
| 3 | `sceneDepth` (water), `uDepth` (distance pass) |
| 4–11 | `shadowMaps[0..7]` (cube) |

Sampler units for `shadowMaps[i]` are assigned once per program; the cube-maps (or texture 0) are bound on every lit pass because a `sampler2D` and a `samplerCube` may not share a unit at draw time.

- **Portability**: no shader may define a function named like a GLSL built-in (`noise2` broke Intel) or use an identifier reserved by later GLSL / GLSL ES; `tests/renderer/test_shader_portability.py` enforces it and, where `glslangValidator` is installed, compiles every shader as desktop GLSL 3.30 and — through `player.gles_shaders.translate` — as GLSL ES 3.00.

---

## 19. Settings and console commands

### `settings.ini`

| Section / key | Default | Read by | Effect |
|---|---|---|---|
| `[Renderer] lowpower_mode` | detected (section 1) | `ForwardRenderer.__init__` | low-power shader set and light cap |
| `[Renderer] shadows_enabled` | `True` (`False` on low-power) | `ForwardRenderer.__init__` | initial `shadows_enabled` |
| `[Renderer] shadow_map_size` | 384 | `ForwardRenderer.__init__` | cube-map face size, clamped 256–2048 |
| `[Renderer] occlusion_culling` | `True` | `OcclusionMixin._init_occlusion` | cell occlusion culling |
| `[Display] vsync` | `True` | `main.py` at start-up | swap interval |
| `[Display] show_fps` | `False` if absent | `QtGameView._paint_frame` | FPS counter |
| `[Display] show_hud` | `True` | `QtGameView._paint_frame` | HUD |
| `[Display] show_glasses` | `True` | `QtGameView.__init__` | player glasses |
| `[Shortcuts] key_toggle_wireframe` | `F2` | `QtGameView.keyPressEvent` | LIT ↔ WIREFRAME render mode |

Renderer settings are read when a `ForwardRenderer` is constructed; changing the file takes effect on the next start (or renderer swap).

### Environment variables

| Variable | Effect |
|---|---|
| `FIO_ARM_MODE` | force low-power detection on (`1`) or off (`0`) |
| `FIO_GL_DEBUG=1` | keep PyOpenGL per-call error checks; wrapper uniform uploads |

### Console commands

Registered in `editor/console_commands.py`. All view-distance and fog commands write the shared `ViewDistance` and are on screen the next frame; none is saved.

| Command (aliases) | Arguments | Effect |
|---|---|---|
| `r_list`, `r_info` | — | renderer name, Display mode, shadows, water quality cap, view distance and fog |
| `r_renderer` | `[name]` | list registered renderers, or swap live (case-insensitive) |
| `r_wireframe` (`wireframe`) | `[on\|off]` | sets the Display box to `Wireframe` / back to the previous mode; no argument toggles |
| `r_shadows` (`shadows`) | — | toggles `renderer.shadows_enabled` |
| `r_waterquality` (`waterquality`) | `[cheap\|expensive]` | session cap on water quality; no argument toggles |
| `r_vsync` (`vsync`) | — | flips `[Display] vsync` in `settings.ini` (applies at next start) |
| `r_clearcolor` | `r g b` (0–1) | sets `renderer.clear_color`; nothing reads that attribute — the frame is cleared to the fog colour (`r_fogcolor`) |
| `r_viewdistance` (`r_culldistance`, `r_cullfogdist`, `r_cullfogdistance`, `viewdistance`, `culldistance`, `cullfogdist`, `cullfogdistance`, `farplane`) | `[units]` | view distance through `MainWindow.set_cull_distance` (keeps the **Cull Dist** spinbox in step); clamped to 500–20000; no argument prints it |
| `r_distancefog` (`distancefog`) | `[on\|off]` | distance fog; no argument toggles |
| `r_fogdistance` (`fogdistance`, `fogdist`) | `<start> <end>` \| `<end>` \| `auto` | pin or release the fog band |
| `r_fogstart` (`fogstart`) | `<units>` \| `auto` | fog start |
| `r_fogend` (`fogend`) | `<units>` \| `auto` | fog end (clamped before the far plane, with a warning) |
| `r_fogdensity` (`fogdensity`) | `<value>` | exponential-squared coefficient; 0 = linear |
| `r_fogcolor` (`r_fogcolour`, `fogcolor`, `fogcolour`) | `<r> <g> <b>` (0–255 or 0–1) | fog and background colour |
| `r_ambient` (`ambient`) | `<level>` \| `<r> <g> <b>` \| `off` | global ambient light |
| `showcollision` (`collisionvis`, `collision`) | see Console commands | collision debug boxes |
| `sg` | — | spatial-grid debug lines (Play) |

`r_renderer`, `r_vsync` and `vsync` are in `USER_ONLY_COMMANDS`: a map's `logic_command` entity cannot run them. The other commands can be fired from map logic (for example `r_fogcolor 40 30 60`).

The commands `r_fog`, `r_water`, `r_glass`, `r_lighting` and `r_deferred` do not exist.

### Editor controls

| Control | Location | Maps to |
|---|---|---|
| **Display** | bottom bar | `QtGameView.brush_display_mode` → `brush_display_mode` |
| **Cull Dist** | bottom bar; 500–20000, step 250, default 4096 | `MainWindow.set_cull_distance` → `ViewDistance.distance` |
| Low-power Mode (reduced shaders), Enable Dynamic Shadows | Settings window | `[Renderer] lowpower_mode`, `shadows_enabled` |
| `F3` in Play | `MainWindow.keyPressEvent` | toggles `show_sprites_in_play_mode` |

---

## 20. Diagnostics

`RenderStats` (`engine/renderer/api.py`), reset at the start of each `render_scene`:

| Field | Filled by ForwardRenderer |
|---|---|
| `total_brushes` | `len(all_brush_slots)` |
| `visible_brushes` | brushes submitted by the lit, textured and edge passes |
| `draw_calls` | incremented by: brush runs and per-row brush draws (lit, textured, glow, edges, triangle overlay), models, sprites, effects, water, glass, the distance pass, path-node cubes and `draw_billboards`. Not incremented by: shadow maps (counted separately), the grid, selection overlays, fog volumes, portal masks/rims, occlusion probes, player glasses, bullet marks |
| `shadow_draw_calls` | every shadow-map draw |
| `batched_draws` | texture binds / instanced batches |
| `visible_tris` | triangles submitted by brush, model and terrain passes |
| `entity_candidates`, `culled_entities` | sprite+model rows offered to the main view, and how many its frustum rejected |
| `pass_ms` | CPU ms per `timed_pass` name (inclusive of nested passes) |
| `details` | `'sprite texture array'` and `'occlusion'` lines (callables, evaluated when shown); not cleared by `reset()` |
| `culled_brushes`, `total_tris` | not filled by `ForwardRenderer` |

In split-screen the stats describe the last view drawn.

Consumers: [SysMon](https://github.com/ViciousSquid/Fio/wiki/System-Monitor-(SysMon)) reads `draw_calls` and `visible_tris` (its brush counts come from the logic thread's frustum result). [Debug Tables](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables) shows the registry name of the active renderer, the counters, `pass_ms`, the `details` lines, `prepare_ms`, `paint_ms`, publication rates and the rows each table read.

Tools: `tools/bench_compare.py` (scripted end-to-end frame benchmark through the real host), `tools/occlusion_compare.py` (A/B report for occlusion), `tools/profile_uniform_uploads.py` (per-pass uniform upload cost).

---

## 21. Source files and tests

| File | Role |
|---|---|
| `engine/renderer/` | see [section 2](#2-package-layout) |
| `engine/shaders.py` | shader sources, light capacities, `light_ubo_source`, `SHADOW_GLSL`, `FOG_GLSL`, low-power detection |
| `engine/render_table.py`, `engine/entity_table.py` | dense brush and entity projections |
| `engine/change_journal.py` | runtime change notification for the tables |
| `engine/mover_table.py` | mover/door state published into `RenderTable` in Play |
| `engine/logic_render.py` | `LogicRender.prepare_render_state`, frustum |
| `engine/threaded_game_state.py` | `RenderState`, double-buffered publication |
| `engine/qt_game_view.py` | host: context, frame input, paint, renderer swap, HUD |
| `engine/view_distance.py` | view distance, fog, ambient |
| `engine/render_cull.py` | GL-free distance-cull and visible-XZ-box helpers |
| `engine/render_keys.py` | `KeyLayout`, `sort_into_runs`, `runs_in_order` |
| `engine/cell_cull.py` | cell groups, probe book, probe governor |
| `engine/sprite_layers.py` | sprite texture array |
| `engine/terrain.py` | terrain rendering |
| `engine/sysmon.py`, `tools/debug_tables.py` | instrumentation |

Tests (see the [Development Guide](https://github.com/ViciousSquid/Fio/wiki/Renderer-Development-Guide) for how to run each tier):

| Directory | Covers |
|---|---|
| `tests/renderer/` | tables, journals, render keys, culling, light budget and fade, instance payloads, the contract and import boundary, shader portability, uniform-location rule, occlusion bookkeeping; GL-marked files for real submission and line limits |
| `tests/visual/` | real-GL pictures: lit scene, distance fog, fog floor, opaque face culling, sprite layers, water quality, scene-capture reuse, projection equivalence, terrain |
| `tests/e2e/test_renderer_hot_swap.py`, `test_renderer_gl_lifetime.py`, `test_deferred_renderer_example.py`, `test_debug_tables_oracle.py`, `test_raw_uniform_uploads.py` | the host path: live swap, GL resource lifetime, the example renderer, counters against measured GL draws, raw uniform uploads |
| `tests/performance/test_renderer_benchmark.py` | opt-in timing report |
