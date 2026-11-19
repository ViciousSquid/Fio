# engine/renderer

Fio's renderers draw the world from the dense tables the logic thread
publishes each frame. The editor and the game talk to a renderer only through
the contract in `api.py`.

| Part | What it is |
|---|---|
| `api.py` | The renderer contract: the `Renderer` protocol and `FRAME_INPUT`, every key a frame hands a renderer. |
| `registry.py` | Registers renderers by name (`r_renderer` switches between them). |
| `core/` | Shared, renderer-independent pieces: GPU geometry and resources, culling and visibility, terrain, overlays. |
| `forward/` | The built-in renderer, `ForwardRenderer` (registered as `"Forward"`). |

Inside `forward/`:

| Part | What it is |
|---|---|
| `renderer.py` | `ForwardRenderer`: the frame, pass by pass. |
| `textured/` | The **Textured** display mode (and the Unlit render mode). |
| `solid_lit/` | The **Solid Lit** display mode, and every untextured brush. |
| `wireframe/` | The **Wireframe** display mode: brush edges coloured by distance. |
| `overlay/` | The **Overlay** display mode: Textured with the triangles drawn over it. |
| `passes.py` | Passes every mode shares: glow, terrain, models, sprites, effects, water, glass, fog. |
| `instancing.py`, `lighting.py`, `shaders.py`, `decals.py`, `portals.py`, `occlusion.py` | Instance buffers, lights and shadows, shaders, decals, portals, occlusion queries. |
| `explosion_rows.py` | EXPLOSION instance rows, laid out by `engine.shaders.EXPLOSION_INSTANCE_LAYOUT` (which also generates the shader's attributes); a loop for a handful, NumPy for many. |

More detail is on the wiki:

* [Renderer Technical overview](https://github.com/ViciousSquid/Fio/wiki/Renderer-Technical-overview)
* [Renderer Development Guide](https://github.com/ViciousSquid/Fio/wiki/Renderer-Development-Guide)
