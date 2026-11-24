# engine/renderer

Fio's renderers draw the world from the dense tables the logic thread
publishes each frame. The editor and the game talk to a renderer only through
the contract in `api.py`.

| Part | What it is |
|---|---|
| `api.py` | The renderer contract: the `Renderer` protocol, `RendererOptions` (what a renderer may offer beyond it), `FRAME_INPUT` (every key a frame hands a renderer) and `contract_violations`, the check of an object against all of it. |
| `registry.py` | Registers renderers by name (`r_renderer` switches between them). `create_renderer` refuses, and cleans up, whatever does not satisfy the contract. |
| `core/` | Shared, renderer-independent pieces: GPU geometry and resources, culling and visibility, terrain, overlays. |
| `forward/` | **Classic**: Fio's original renderer, `ForwardRenderer` (registered as `"Forward"`; `"Classic"` is an alias). The default. |
| `hd_high/` | **HD_High**: a separate, physically based renderer, `HDHighRenderer` (registered as `"HD_High"`). |

Fio ships two renderers, held to one contract. What a factory returns is
checked before the host paints with it: every member, each method's arguments
(names, order, defaults), each attribute's type and permitted values, a
declared `contract_version`, and no property that raises. The same
conformance tests run against both (`tests/visual/test_renderer_conformance.py`,
`tests/renderer/test_built_in_renderer_classes.py`); the malformed
implementations the registry must refuse are in
`tests/renderer/test_renderer_contract_validation.py`.

They share `core/` and nothing else: neither imports
the other, each is created by `create_renderer(name, config)` and handed the
same frame input, and the host swaps them while it runs
(`QtGameView.switch_renderer`; Settings > Display > Renderer; the console's
`r_renderer Classic` / `r_renderer HD_High`). `[Renderer] renderer` in
`settings.ini` names the one the editor starts with. Each is documented in
[`docs/renderers.md`](../../docs/renderers.md).

Classic is locked. Its package, `core/` and `engine/shaders.py` are checked
against recorded digests (`tests/visual/test_classic_lock.py`), and its frames
and final shader sources against a stored set drawn before HD_High existed
(`tests/visual/test_classic_golden.py`). After a deliberate change to any of
those files, check the picture with `tools/hd_golden.py --check` (or `--base`
against another checkout) and record it with `tools/hd_golden.py --lock-sources`.

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

Inside `hd_high/` (its passes began as a copy of Classic's, so that it draws
everything Fio draws; what makes it HD_High is in the first five files):

| Part | What it is |
|---|---|
| `renderer.py` | `HDHighRenderer`: the frame, pass by pass, with the HD frame around it. |
| `environment.py` | The sun and its shadow map (static and moving layers), the sky light volume (`skylight.py`: daylight that spreads through windows and doors), sky modes (none / panorama / procedural), exposure, the reflection fallback, material maps, Forward+ light clusters. |
| `surface.py` | Physically based shading, added to the lit programs' and the terrain's fragment sources when they are compiled. |
| `postfx.py` | The HDR target, ambient occlusion, screen-space reflections, bloom, sun shafts, tone mapping, grading, FXAA; the benchmark's pass timestamps. |
| `hdr.py` | How the programs that are not physically shaded (sprites, particles, water, glass, the sky image) keep their colours through the tone curve. |
| `core_ext.py` | What HD_High needs beyond `core/`, added on its own side: model draw groups with roughness and metalness, and the terrain's own programs compiled the HD way. |
| the rest | The same parts as in `forward/`. |

`engine/hd_graphics.py` (no OpenGL) holds what is data: which renderer the
settings ask for, the PlayerStart's `hd_*` sky properties and how they are
resolved, HD_High's configuration, and the exposure arithmetic.
`engine/material_maps.py` generates and finds material maps.

More detail is on the wiki:

* [Renderer Technical overview](https://github.com/ViciousSquid/Fio/wiki/Renderer-Technical-overview)
* [Renderer Development Guide](https://github.com/ViciousSquid/Fio/wiki/Renderer-Development-Guide)
