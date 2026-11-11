> Applies to Fio 3.1.0.0611 (CANARY).

How to change or replace Fio's renderer. Read [Renderer Technical overview](https://github.com/ViciousSquid/Fio/wiki/Renderer-Technical-overview) first for what the built-in renderer does; this page is about the boundary. The authoritative contract text is `plugins/API.md` (API 1.9.0) and `engine/renderer/api.py`.

## The boundary

`engine.renderer` is a package with a small public surface and private implementations:

| Part | Contents |
| --- | --- |
| `engine.renderer` (public) | `Renderer` (the protocol), `FRAME_INPUT`, `RenderStats`, `SelectionOverlay`, `EffectBillboard`, `WATER_QUALITIES`, and the registry: `register_renderer`, `create_renderer`, `available_renderers`, `renderer_factory`, `DEFAULT_RENDERER`. Importing it needs no OpenGL. |
| `engine.renderer.forward` | Fio's built-in forward renderer. |
| `engine.renderer.core` | Optional infrastructure a renderer may reuse: `RendererCore` (textures and models, table lookups, culling, editor overlays) and `terrain_gl.TerrainGL`. |

The host (`QtGameView` and the `engine/view_*.py` classes it delegates to) depends only on the public surface.

## Tables all the way to GL

A renderer is handed **published tables**, never authored objects:

- brushes: [RenderTable](https://github.com/ViciousSquid/Fio/wiki/RenderTable); entities, lights, portals, effects: [EntityTable](https://github.com/ViciousSquid/Fio/wiki/EntityTable);
- terrain: an immutable `engine.terrain_frame.TerrainFrame` (the dense `TerrainTable`, per-slot grass and block-mesh arrays, paint maps, appearance as numbers). The renderer owns every GL resource it builds from it; `TerrainGL` is the built-in implementation and is reusable as it stands;
- decals: two arrays a frame from `engine/decal_table.py`;
- overlays: `draw_face_highlight(projection, view, triangles)`, `draw_connection_lines(projection, view, segments, colours)`, `draw_collision_visualization(projection, view, vertices, runs)` take vertex arrays (the host builds them in `engine/overlay_geometry.py`).

`RenderTable.row_changes_since(serial)` tells a reader which rows were refreshed; nothing in a renderer assigns an attribute of a table. What remains by design is GPU resource building on a cache miss (a model's draw groups, a convex brush's mesh).

`FRAME_INPUT` in `engine/renderer/api.py` names every key of the per-frame configuration with its meaning; `RenderStats` is what a renderer reports back (draw calls, triangles, `terrain_visible_chunks`, `terrain_culled_chunks`, `terrain_triangles`, `terrain_drawn_slots` ...).

## Writing a renderer

1. Implement the `Renderer` protocol. You do not need to inherit or understand `ForwardRenderer`. `docs/examples/deferred_renderer/` is a complete worked example.
2. Register it by name from a plugin (`register_renderer` through the [Plugin API](https://github.com/ViciousSquid/Fio/wiki/Plugin-API)). Registration returns False in a headless or player context with no viewport.
3. Select it with the console: `r_renderer` lists the registered renderers and `r_renderer <name>` swaps live, in the editor or in Play (user-only: map logic cannot run it). The host creates the new renderer first; if its factory raises or it is not `ready`, the current one keeps running. Session settings (shadows, water quality ...) are carried over.

## Rules the tests hold (`tests/renderer/test_renderer_boundary.py`)

- The renderer imports nothing from the editor.
- The public surface needs no GL and no implementation module.
- `core` never reaches into `forward`.
- Host, editor and plugin code import only `engine.renderer`. A renderer implementation outside the package (the standalone player's, in `player/render`) may reuse `engine.renderer.core`, but not `forward` or the private modules.
- World logic and editor state do not import the renderer.
- The protocol exposes no forward-renderer internals.

## Changing the built-in renderer

- **A new column**: add it to the table (`engine/render_table.py` / `engine/entity_table.py`), fill it on the logic thread, and confirm it in [Debug Tables](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables), which reads the same published snapshot and is the oracle for any column.
- **A new pass or shader**: shader sources live in `engine/shaders.py` (`DEFAULT_SHADERS`); passes in `engine/renderer/forward/passes.py`. Shaders must compile under GL 3.3 core and translate to GLSL ES 3.00 for the player: a uniform shared by both stages needs the same precision in both, and a location may be 0, so never test a location for truth.
- **GL lifetime**: every GL name a renderer makes it deletes, with its context current; nothing GL is created on the logic thread.

## Guarding a change

| Check | Command |
| --- | --- |
| Renderer logic, no GL | `python -m pytest tests/renderer -q` |
| Pixels under a software rasteriser | `LIBGL_ALWAYS_SOFTWARE=1 xvfb-run -a python -m pytest -m gl -q` |
| Every shader through the Khronos compiler | `FIO_REQUIRE_GLSLANG=1 python -m pytest tests/renderer/test_shader_portability.py -q` |
| The player's GLES translation | the staged `player/tests` and `player.tools.smoke_gles` ([Testing and CI](https://github.com/ViciousSquid/Fio/wiki/Testing-and-CI)) |
| Before/after equality | `tools/oracle_capture.py` and `tools/oracle_diff.py` |
| Cost | `tools/bench_*.py`, `tests/performance/test_renderer_benchmark.py --run-benchmarks` |

Design records: `docs/renderer-boundary-audit.md`, `docs/renderer-m2-shared-geometry.md`, `docs/renderer-m3-profiling.md`, `docs/renderer-m4.md`, `docs/performance-audit.md`.
